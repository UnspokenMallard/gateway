"""A tool-using `claude -p` session (was Jarvis's `jobcommon.run_claude_job`, and Forge's implementer call).

dontAsk permissions, the explicit tool list and allow rules, the shared deny list (secrets,
credentials, transcripts), the per-run cap, Budget's job-budget gate before every attempt
(optional jobs stop at 80% of the week or at the daily cap), retries only on transient network
errors, and every attempt recorded in Budget's `job_calls`. The model is the surface's
(`job:<job>` in `[surfaces]`, or `surface=` when several jobs share one). Jarvis keeps the job
lock, the logs and the artifact checks.
"""
from __future__ import annotations

import difflib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import claude, local, proc, store
from ._budget import ledger, limits

HOME = Path.home()
TRANSIENT = ("ENOTFOUND", "ECONNRESET", "ETIMEDOUT", "Unable to connect to API", "fetch failed", "socket hang up")


def rule_path(path: Path | str) -> str:
    """An absolute path in the `//abs/path` form permission rules need (a single leading `/` is
    relative to the settings file's project, not the filesystem)."""
    p = Path(path).as_posix()
    return "/" + p if p.startswith("/") and not p.startswith("//") else p


DENY_TOOLS = [
    f"Read({rule_path(HOME / '.jarvis' / 'secrets')}/**)",
    f"Read({rule_path(HOME / '.herald' / 'secrets')}/**)",  # channel credentials, in Herald since 2026-10-17
    f"Read({rule_path(HOME / '.claude-personal' / '.credentials.json')})",
    f"Read({rule_path(HOME / '.claude-work' / '.credentials.json')})",
    f"Read({rule_path(HOME / '.claude' / '.credentials.json')})",
    f"Read({rule_path(HOME / '.ssh')}/**)",
    f"Read({rule_path(HOME / '.git-credentials')})",
    f"Read({rule_path(HOME / '.claude.json')})",
    f"Read({rule_path(HOME / '.claude-personal' / '.claude.json')})",
    f"Read({rule_path(HOME / '.claude-work' / '.claude.json')})",
    f"Read({rule_path(HOME / '.claude-personal' / 'projects')}/**/*.jsonl)",
    f"Read({rule_path(HOME / '.claude-work' / 'projects')}/**/*.jsonl)",
    f"Read({rule_path(HOME / '.claude' / 'projects')}/**/*.jsonl)",
    "mcp__serena__*",
    "mcp__headroom__*",
]


def _log(log_file: Path | None, text: str) -> None:
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(text.rstrip("\n") + "\n")


def command(exe_path: str, model: str, session: str, tools: str, allow: list[str] | None, budget_usd: float,
            scope: str = "personal", flags: list[str] | None = None, deny: bool = True) -> list[str]:
    """The job session's argument list. The prompt goes on stdin.

    `--strict-mcp-config` is always on: a job session starts no MCP servers, so the account's
    connectors never put their tool schemas (~170k tokens, 2026-10-19) in turn 1. Callers opt out
    of nothing; a caller that needs an MCP server must say so in code here, not per call."""
    own_spill = f"Read({rule_path(claude.CONFIG_DIRS[scope] / 'projects')}/*/{session}/**)"
    args = [exe_path, "-p", "--model", model, "--session-id", session, "--strict-mcp-config", *(flags or []),
            "--permission-mode", "dontAsk", "--tools", tools, "--allowedTools", *(allow or []), own_spill]
    if deny:
        args += ["--disallowedTools", *DENY_TOOLS]
    return args + ["--max-budget-usd", f"{budget_usd:g}", "--output-format", "json"]


# The process runner; Jarvis's selftest replaces it so nothing reaches a model.
RUN = proc.run


def _result(output: str) -> dict | None:
    """The CLI's final `{"type": "result", ...}` object, read from the end of the output."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("type") == "result":
                return doc
    return None


def gate(scope: str, optional: bool = False) -> tuple[bool, str]:
    """Budget's job-budget gate: (go ahead, why)."""
    try:
        b = ledger.job_budget(scope=scope)
    except Exception as exc:  # noqa: BLE001 - refuse rather than spend unmetered
        return False, f"the weekly LLM job budget could not be checked ({exc})"
    stop = b["paused_for_optional"] if optional else b["over"]
    text = f"{scope}: ${b['spent']:.2f} of ${b['ceiling']:.2f} this week"
    return (False, f"the LLM job budget for the {scope} account is reached ({text})") if stop else (True, text)


def run_agent(job: str, prompt: str, tools: str, allow: list[str] | None = None, budget_usd: float = 1.0, *,
              scope: str = "personal", cwd: Path | str | None = None, optional: bool = False,
              log_file: Path | None = None, surface: str | None = None, flags: list[str] | None = None,
              session: str | None = None, max_attempts: int = 3, backoff_s: int = 120,
              timeout_s: float | None = None, deny: bool = True, note: str = "",
              cfg: Mapping[str, Any] | None = None, shadow: bool = True) -> dict:
    """One job session under its declared limits. Never raises.

    `{"code", "ok", "subtype", "cost_usd", "turns", "result", "denials", "session", "limit",
    "output", "duration_s", "model", "reason", "ledger_id"}` for the last attempt (`denials` is the list of
    denied tool names, `ledger_id` the Budget row to hand to `reject`); `code` is the exit code
    (1 when the gate refused or the cap stopped it). The caller still asserts its artifact.
    `flags` are extra CLI flags (the implementer's `--safe-mode --restricted`); `deny=False`
    drops the shared deny list for a caller whose allow rules are already a closed set.
    A job named in `[shadow.jobs]` also gets a read-only Haiku shadow run after a successful live run
    (`_shadow`); `shadow=False` is how that run avoids shadowing itself.
    """
    cfg = cfg if cfg is not None else store.load_config()
    surface = surface or f"job:{job}"
    model = claude.model_for(surface, cfg)
    out: dict = {"code": 1, "ok": False, "subtype": "not-started", "cost_usd": 0.0, "turns": None, "result": "",
                 "denials": [], "session": None, "limit": None, "output": "", "duration_s": 0.0, "model": model or ""}
    if not model:
        out["reason"] = f"surface {surface!r} is not a Claude surface in ~/.gateway/config.toml"
        _log(log_file, f"=== FAILED: not starting {job} - {out['reason']} ===")
        return out
    exe_path = claude.exe()
    if exe_path is None:
        out["reason"] = "claude CLI not found"
        _log(log_file, f"=== FAILED: not starting {job} - claude CLI not found ===")
        return out
    scope = scope if scope in claude.CONFIG_DIRS else "personal"
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            _log(log_file, f"--- retry {attempt}/{max_attempts} after {backoff_s}s ---")
            time.sleep(backoff_s)
        go, why = gate(scope, optional)
        if not go:
            out.update(code=1, reason=why)
            _log(log_file, f"=== FAILED: not starting {job} - {why}. See [llm_jobs] in ~/.budget/config.toml ===")
            return out
        sid = session if session and attempt == 1 else str(uuid.uuid4())
        args = command(exe_path, model, sid, tools, allow, budget_usd, scope, flags, deny)
        started = time.time()
        try:
            code, output = RUN(args, timeout_s=timeout_s, cwd=str(cwd or HOME / ".jarvis"), input_text=prompt,
                                    env=claude.env_for(scope), sink=log_file)
        except OSError as exc:
            code, output = 127, f"could not start claude: {exc}"
        duration = round(time.time() - started, 1)
        result = _result(output)
        subtype = str(result.get("subtype")) if result else "no-result"
        cost = float(result["total_cost_usd"]) if result and result.get("total_cost_usd") is not None else None
        turns = result.get("num_turns") if result else None
        denials = [d.get("tool_name", "?") for d in (result or {}).get("permission_denials") or []]
        ok = code == 0 and subtype == "success" and not (result or {}).get("is_error")
        hit = limits.detect(output, claude.provider_of(scope)) if not ok else None
        if hit is not None:
            limits.record(hit)
        elif ok:
            limits.note_success(claude.provider_of(scope))
        detail = f"{note + ' ' if note else ''}{subtype} turns={turns if turns is not None else '?'} attempt={attempt}"
        if denials:
            detail += f" denials={len(denials)}[{','.join(denials)}]"
        row = None
        try:
            # `ok` is only "the call succeeded"; a failed call's subtype is its `reason`, and
            # `reject()` adds one later when the job refuses the output of a call that succeeded.
            row = ledger.record_job(job, ok, duration, cost, model, detail, scope=scope,
                                    reason="" if ok else subtype, attempt=attempt)
        except Exception as exc:  # noqa: BLE001
            _log(log_file, f"(could not record the job call in Budget: {exc})")
        cost_s = f"{cost:.4f}".rstrip("0").rstrip(".") if cost is not None else "-"
        _log(log_file, f"--- claude[{scope}] {model}: {subtype}, ${cost_s} list price, "
                       f"{turns if turns is not None else '?'} turn(s), {duration}s, session {sid} ---")
        if result and result.get("result"):
            _log(log_file, str(result["result"]))
        out.update(code=code, ok=ok, subtype=subtype, cost_usd=cost or 0.0, turns=turns,
                   result=str((result or {}).get("result") or ""), denials=denials, session=sid, limit=hit,
                   output=output, ledger_id=row, duration_s=duration, reason=subtype)
        if hit is not None:
            out["reason"] = f"usage limit: {hit.evidence}"
            return out
        if subtype == "error_max_budget_usd":
            _log(log_file, f"=== FAILED: {job} stopped at its per-run cap of ${budget_usd} - not retried ===")
            out["code"] = code or 1
            return out
        transient = any(t in output for t in TRANSIENT)
        if code == 0 and not transient:
            if ok and shadow:
                _shadow(job, prompt, out, scope, cwd, allow, budget_usd, log_file, cfg)
            return out
        if not transient:
            return out
        _log(log_file, f"attempt {attempt} hit a transient network error")
    _log(log_file, f"=== FAILED: still failing after {max_attempts} attempts ===")
    out["code"] = out["code"] or 1
    return out


SHADOW_SURFACE = "job:shadow-haiku"
READ_RULES = ("Read", "Grep", "Glob")


def _shadow(job: str, prompt: str, live: dict, scope: str, cwd: Path | str | None, allow: list[str] | None,
            budget_usd: float, log_file: Path | None, cfg: Mapping[str, Any]) -> None:
    """Haiku answers the prompt of a live job next to the live model (T6, 2026-10-22). `[shadow.jobs]` names
    the jobs and `[shadow] until` (YYYY-MM-DD) ends it. The shadow run is read-only (no Write, no Bash, only
    the Read/Grep/Glob allow rules), billed in Budget as `<job>-shadow`, and its answer is never used:
    both answers go to `local_llm_shadow` (surface `job:<job>`, `local` = Haiku, `remote` = the live
    model; `agree` is a rough text similarity of at least 0.5). Never raises."""
    section = cfg.get("shadow") or {}
    if job not in (section.get("jobs") or {}) or str(datetime.now().date()) > str(section.get("until") or "0"):
        return
    try:
        read_only = [a for a in allow or [] if a.startswith(READ_RULES)]
        sh = run_agent(f"{job}-shadow", prompt, ",".join(READ_RULES), read_only, min(budget_usd, 0.5), scope=scope,
                       cwd=cwd, optional=True, log_file=log_file, surface=SHADOW_SURFACE, max_attempts=1,
                       timeout_s=600, note="shadow", cfg=cfg, shadow=False)
        ratio = difflib.SequenceMatcher(None, live["result"][:4000], sh["result"][:4000]).quick_ratio()
        local.record_shadow(f"job:{job}", sh["result"][:3000], live["result"][:3000], sh["ok"] and ratio >= 0.5,
                            f"{sh['model']} ${sh['cost_usd']:.3f} {sh['turns']}t sim={ratio:.2f} vs {live['model']} "
                            f"${live['cost_usd']:.3f} {live['turns']}t denials={','.join(sh['denials'])}")
    except Exception as exc:  # noqa: BLE001 - the shadow must never break the job it measures
        _log(log_file, f"(shadow run of {job} failed: {exc})")


def reject(run: Mapping[str, Any], why: str) -> None:
    """The caller refuses the output of a `run_agent` result that succeeded: its spend counts as wasted.
    Never raises."""
    ledger.reject_job(run.get("ledger_id"), why)


def stamp(prompt: str) -> str:
    """The two lines every nightly job prompt ends with (jobcommon added them before 2026-10-17)."""
    return (prompt + f"\n\nCurrent local time: {datetime.now().astimezone().isoformat(timespec='seconds')}. "
            "Use it for any timestamp you write - there is no shell to ask."
            "\nIf a tool result is too large and is saved to a file for you instead, Read that file - it is within your limits.")
