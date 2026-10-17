"""One `claude -p` call: the argument list, the account, the run, the parse, the limit and the ledger row.

Moved here from Jarvis's `brain.command()` / `brain.work()` (the call half; brain keeps its caps,
breaker and `brain_calls`), the HUD's three Haiku calls, Mind's session/memory/vault summaries and
improve evals, and Forge's criteria scoring (2026-10-17).

Flags, each of them load-bearing:

- `--output-format json` gives `total_cost_usd` and `stop_reason`, so spend is metered on what
  the call reported.
- `--strict-mcp-config` with no `--mcp-config` starts no MCP servers (the 2026-08-28 wedge: MCP
  servers outliving `claude -p` held the output handle for 59 minutes).
- `isolated=True` (every brain call): `--safe-mode` skips CLAUDE.md, skills, plugins and hooks,
  `--tools` is the complete tool set (none by default) and `dontAsk` refuses anything not
  pre-approved. Exactly brain's flags before the move, so the G15-measured proposer call is unchanged.
- `bare=True` (every `ask()`): also `--setting-sources ''` (no settings files) and
  `--no-session-persistence` (no transcript), as the HUD's and Mind's calls had. Measured
  2026-10-17 on both accounts: about 2.9k input tokens for a Haiku call.
- `--max-budget-usd` hands the per-call ceiling to the CLI itself.

Usage limits: a failed call's output is checked against Budget's limit patterns; a hit is recorded
in Budget's window here and returned as `limit`, and the caller raises the alert (Gateway never
alerts). A call that got through proves the window reopened (`limits.note_success`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from . import local, proc, store
from ._budget import ledger, limits

log = logging.getLogger("gateway.claude")

HOME = Path.home()
CONFIG_DIRS = {"personal": HOME / ".claude-personal", "work": HOME / ".claude-work"}
# Quota provider per account; personal keeps the historical name for Budget's limit files.
PROVIDERS = {"personal": "claude", "work": "claude-work"}
SCOPES = tuple(CONFIG_DIRS)

READONLY_TOOLS = ["Read", "Grep", "Glob"]
ALWAYS_DENIED = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"]

CLI_CANDIDATES = [HOME / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude")]


def exe() -> str | None:
    for cand in (shutil.which("claude"), *map(str, CLI_CANDIDATES)):
        if cand and Path(cand).exists():
            return cand
    return None


def env_for(scope: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The process environment with CLAUDE_CONFIG_DIR set for the account."""
    env = dict(os.environ if base is None else base)
    env["CLAUDE_CONFIG_DIR"] = str(CONFIG_DIRS[scope])
    return env


def provider_of(scope: str) -> str:
    return PROVIDERS[scope]


def configured(surface: str, cfg: Mapping[str, Any] | None = None) -> str | None:
    """The surface's raw mode from `[surfaces]`, or None when it is not configured."""
    cfg = cfg if cfg is not None else store.load_config()
    value = (cfg.get("surfaces") or {}).get(surface)
    return None if value is None else str(value)


def model_for(surface: str, cfg: Mapping[str, Any] | None = None) -> str | None:
    """`haiku` for a surface set to `claude:haiku`; None for a local-model surface or an unknown one."""
    value = configured(surface, cfg) or ""
    return value.split(":", 1)[1] if value.startswith("claude:") and value[7:] else None


def command(exe_path: str, model: str, budget_usd: float | None, *, prompt: str | None = None,
            allowed_tools: list[str] | None = None, isolated: bool = False, bare: bool = False,
            system: str | None = None, system_file: str | None = None) -> list[str]:
    """The `claude -p` argument list (was `brain.command`, also used by `g15-replay.py`).

    With `isolated`, `allowed_tools` is the complete tool set (none when empty). Without it the
    user's profile loads and two lists guard the tools: `--allowedTools` (default Read/Grep/Glob)
    and `--disallowedTools`, so a typo in the allowlist never silently grants Bash. Pass the prompt
    as `input_text` to the runner (prompt=None) when it may be long.
    """
    cmd = [exe_path, "-p"]
    if prompt is not None:
        cmd.append(prompt)
    cmd += ["--model", model, "--output-format", "json", "--strict-mcp-config"]
    if isolated:
        tools = list(allowed_tools or [])
        cmd += ["--safe-mode"]
        if bare:
            cmd += ["--setting-sources", "", "--no-session-persistence"]
        cmd += ["--tools", ",".join(tools), "--permission-mode", "dontAsk"]
        if tools:
            cmd += ["--allowedTools", " ".join(tools)]
    else:
        tools = allowed_tools if allowed_tools is not None else READONLY_TOOLS
        cmd += ["--allowedTools", " ".join(tools),
                "--disallowedTools", " ".join(t for t in ALWAYS_DENIED if t not in tools)]
    if system:
        cmd += ["--system-prompt", system]
    if system_file:
        cmd += ["--append-system-prompt-file", str(system_file)]
    if budget_usd is not None:
        cmd += ["--max-budget-usd", f"{budget_usd:.2f}"]
    return cmd


def _record(surface: str, job: str | None, ok: bool, duration: float, cost: float | None, model: str,
            detail: str, scope: str) -> None:
    try:
        if job:
            ledger.record_job(job, ok, duration, cost, model, detail[:200], scope=scope)
        else:
            ledger.record_call(surface, ok=ok, duration_s=duration, cost_usd=cost, model=model, detail=detail,
                               scope=scope)
    except Exception as exc:  # noqa: BLE001 - a ledger hiccup must not lose the answer
        log.warning("gateway: could not record %s: %s", surface, exc)


def call(surface: str, prompt: str, *, scope: str = "personal", model: str | None = None,
         budget_usd: float | None = None, timeout_s: float = 300, cwd: str | None = None,
         tools: list[str] | None = None, isolated: bool = True, bare: bool = False, system: str | None = None,
         system_file: str | None = None, job: str | None = None,
         cfg: Mapping[str, Any] | None = None) -> dict:
    """Run one Claude call for `surface` and report what happened. Never raises.

    `{"ok", "text", "cost_usd", "duration_s", "model", "reason", "limit", "code", "output"}`.
    The model is the surface's (`claude:<model>` in `[surfaces]`); `model` overrides it only for
    a caller whose model is the thing under test (Mind's improve evals). The call is recorded in
    Budget's ledger as `surface` (provider_calls), or as `job` (job_calls, the weekly job budget).
    """
    scope = scope if scope in CONFIG_DIRS else "personal"
    model = model or model_for(surface, cfg)
    base = {"ok": False, "text": "", "cost_usd": 0.0, "duration_s": 0.0, "model": model or "", "limit": None,
            "code": None, "output": ""}
    if not model:
        return {**base, "reason": f"surface {surface!r} is not a Claude surface in ~/.gateway/config.toml"}
    exe_path = exe()
    if exe_path is None:
        return {**base, "reason": "claude CLI not found"}
    cmd = command(exe_path, model, budget_usd, allowed_tools=tools, isolated=isolated, bare=bare, system=system,
                  system_file=system_file)
    started = time.time()
    try:
        code, output = proc.run(cmd, timeout_s=timeout_s, cwd=cwd or str(store.STATE), input_text=prompt,
                                env=env_for(scope))
    except OSError as exc:
        code, output = 127, f"could not start claude: {exc}"
    duration = time.time() - started
    base.update(code=code, output=output, duration_s=round(duration, 3))

    if code != 0:
        hit = limits.detect(output, provider_of(scope))
        if hit is not None:
            limits.record(hit)
            _record(surface, job, False, duration, 0.0, model, f"usage limit: {hit.evidence}", scope)
            return {**base, "limit": hit, "reason": f"usage limit: {hit.evidence}"}
        detail = f"claude exit={code}: {output[-400:]}"
        _record(surface, job, False, duration, 0.0, model, detail, scope)
        return {**base, "reason": detail}

    try:
        parsed, _ = json.JSONDecoder().raw_decode(output, output.index("{"))
    except ValueError as exc:
        detail = f"unparseable CLI output ({exc}): {output[:300]}"
        _record(surface, job, False, duration, 0.0, model, detail, scope)
        return {**base, "reason": detail}

    cost = float(parsed.get("total_cost_usd") or 0.0)
    text = str(parsed.get("result") or parsed.get("text") or "")
    stop = str(parsed.get("stop_reason") or "")
    ok = not parsed.get("is_error")
    reason = f"stop_reason={stop}" if ok else f"{parsed.get('subtype')}: {text[:200]}"
    _record(surface, job, ok, duration, cost, model, f"stop_reason={stop} {text[:200]}" if ok else reason, scope)
    limits.note_success(provider_of(scope))
    return {**base, "ok": ok, "text": text, "cost_usd": cost, "reason": reason}


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_json(text: str) -> Any:
    """The JSON value in a model answer, fenced or not; None when there is none."""
    body = _FENCE.sub("", (text or "").strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = body.find(opener), body.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                continue
    return None


def ask(surface: str, prompt: str, schema: dict | None = None, scope: str = "personal", *,
        system: str | None = None, system_file: str | None = None, budget_usd: float | None = None,
        timeout_s: float = 300, job: str | None = None, model: str | None = None,
        cfg: Mapping[str, Any] | None = None) -> dict | None:
    """A small judgment: no tools, no session files. Never raises.

    A local-model surface (`off`, `shadow`, `local`): the local model's answer, or None when the
    surface is off or the local call did not answer (the caller asks Claude through its own surface,
    as before). A Claude surface: `call()`'s result plus `answer`, the parsed JSON when `schema` is
    given (None when unreadable) or the text.
    """
    cfg = cfg if cfg is not None else store.load_config()
    how = configured(surface, cfg)
    if how is None and model is None:
        return {"ok": False, "answer": None, "text": "", "cost_usd": 0.0, "duration_s": 0.0, "model": "",
                "limit": None, "reason": f"unknown surface {surface!r}"}
    if how in (local.OFF, local.SHADOW, local.LOCAL):
        started = time.time()
        answer = local.judge_sync(surface, prompt, schema or {"type": "object"}, cfg, scope=scope)
        if answer is None:
            return None
        return {"ok": True, "answer": answer, "text": json.dumps(answer), "cost_usd": 0.0,
                "duration_s": round(time.time() - started, 3), "model": local._cfg(cfg)["model"], "limit": None,
                "reason": "local"}
    out = call(surface, prompt, scope=scope, model=model, budget_usd=budget_usd, timeout_s=timeout_s, bare=True,
               system=system, system_file=system_file, job=job, cfg=cfg)
    out["answer"] = (parse_json(out["text"]) if schema is not None else out["text"]) if out["ok"] else None
    return out


async def acall(surface: str, prompt: str, **kw) -> dict:
    import asyncio
    return await asyncio.to_thread(call, surface, prompt, **kw)


async def aask(surface: str, prompt: str, schema: dict | None = None, scope: str = "personal", **kw) -> dict | None:
    import asyncio
    return await asyncio.to_thread(ask, surface, prompt, schema, scope, **kw)
