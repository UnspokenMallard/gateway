"""Small judgments on a local model (Ollama on this Mac) instead of `claude -p --model haiku`.

Moved from `~/.jarvis/daemon/local_llm.py` (2026-10-17); TRIAGE-MODEL-DESIGN.md in `~/.jarvis`.
Decided by Anze 2026-10-16: the Mac hosts it, anzes-pc may be added later as another `host`.

A local-model surface is in one of three modes, from `[surfaces]` in config.toml:

- `off` (default): nothing is called.
- `shadow`: the local model answers next to Haiku, Haiku's answer is used, and both answers are
  stored in `local_llm_shadow` so agreement can be measured (design step 2).
- `local`: the local answer is used. Any failure falls back to Haiku, and so does a "nothing
  to see" answer on text the keyword rules flag (`needs_second_opinion`): the local model may
  skip an escalation, it may never suppress one by itself.

`judge()` never raises and returns None for "no local answer". Callers then do what they did
before. A call is also skipped when the host is under memory pressure, read from the sample
`jobs/local-llm-perf.py` writes every 5 min (design section 4).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from . import store
from ._budget import ledger

log = logging.getLogger("gateway.local")

OFF, SHADOW, LOCAL = "off", "shadow", "local"
PERF_MAX_AGE_S = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_llm_shadow (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    surface  TEXT NOT NULL,
    local    TEXT,
    remote   TEXT,
    agree    INTEGER NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_local_llm_shadow_ts ON local_llm_shadow(surface, ts);
"""

# Words that make a "not urgent" local verdict go to Haiku anyway (English and Slovene).
_ALARM = re.compile(r"\b(urgent|asap|blocker|blocking|critical|outage|down|broken|security|data loss|"
                    r"nujno|takoj|ne dela|izpad)\b", re.I)


def perf_sample() -> Path:
    return store.STATE / "local-llm-perf.json"


def ensure_schema() -> None:
    with store.connect() as conn:
        conn.executescript(_SCHEMA)


def _cfg(cfg: Mapping[str, Any] | None = None) -> dict:
    cfg = cfg if cfg is not None else store.load_config()
    section = dict(cfg.get("local") or {})
    section.setdefault("enabled", False)
    section.setdefault("host", "http://127.0.0.1:11434")
    section.setdefault("model", "")
    section.setdefault("timeout_s", 10)
    section.setdefault("keep_alive", "10m")
    section.setdefault("min_memlevel", 30)
    section["surfaces"] = dict(cfg.get("surfaces") or {})
    return section


def mode(surface: str, cfg: Mapping[str, Any] | None = None) -> str:
    section = _cfg(cfg)
    if not section["enabled"] or not section["model"]:
        return OFF
    value = str(section["surfaces"].get(surface, OFF))
    return value if value in (SHADOW, LOCAL) else OFF


def pressure_reason(section: Mapping[str, Any], now: float | None = None) -> str | None:
    """Why the host is too loaded for a local call, or None. A stale or missing sample is a reason."""
    try:
        doc = json.loads(perf_sample().read_text(encoding="utf-8"))
        latest = doc["latest"]
    except (OSError, ValueError, KeyError):
        return "no perf sample"
    if (now or time.time()) - float(latest.get("ts") or 0) > PERF_MAX_AGE_S:
        return "perf sample is stale"
    level = latest.get("memlevel")
    if level is not None and level < int(section["min_memlevel"]):
        return f"memory pressure (level {level})"
    return None


def needs_second_opinion(text: str) -> bool:
    return bool(_ALARM.search(text or ""))


def _post(url: str, body: dict, timeout_s: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.load(resp)


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    answer = json.loads(match.group(0) if match else text)
    if not isinstance(answer, dict):
        raise ValueError("answer is not an object")
    return answer


def _validate(answer: dict, schema: Mapping[str, Any]) -> dict:
    """Without `format` nothing constrains the reply: require the keys and map enum values (trimmed,
    case-insensitive) to their label. Raises, so the caller falls back to Haiku."""
    for key in schema.get("required", ()):
        if key not in answer:
            raise ValueError(f"answer lacks {key!r}")
    for key, prop in (schema.get("properties") or {}).items():
        options = prop.get("enum")
        if options and key in answer:
            match = next((o for o in options if str(o).lower() == str(answer[key]).strip().lower()), None)
            if match is None:
                raise ValueError(f"{key}={answer[key]!r} is not one of {options}")
            answer[key] = match
    return answer


def _chat(section: Mapping[str, Any], body: dict, schema: dict) -> dict:
    """POST /api/chat. Some models reject `think` (400) or a `format` schema (501, qwen3.5 on Ollama 0.40.7):
    drop what is rejected and ask for the JSON in the prompt instead."""
    url, timeout = f"{section['host'].rstrip('/')}/api/chat", float(section["timeout_s"])
    for drop in ((), ("format",), ("think",), ("think", "format")):
        attempt = {k: v for k, v in body.items() if k not in drop}
        if "format" in drop:
            msg = dict(body["messages"][0])
            msg["content"] += "\n\nAnswer with only a JSON object matching this JSON schema: " + json.dumps(schema)
            attempt["messages"] = [msg]
        try:
            return _post(url, attempt, timeout)
        except urllib.error.HTTPError as exc:
            log.info("local: %s rejected %s (HTTP %s), retrying without it", section["model"], drop or "nothing", exc.code)
            continue
    raise RuntimeError("every request variant was rejected")


def judge_sync(surface: str, prompt: str, schema: dict, cfg: Mapping[str, Any] | None = None,
               *, scope: str = "personal") -> dict | None:
    """The local model's answer as a dict matching `schema`, or None. Never raises."""
    section = _cfg(cfg)
    if mode(surface, cfg) == OFF:
        return None
    why = pressure_reason(section)
    if why:
        log.info("local: %s skipped - %s", surface, why)
        ledger.record_call(f"local:{surface}", False, 0.0, 0.0, section["model"], f"skipped: {why}", scope)
        return None
    body = {"model": section["model"], "messages": [{"role": "user", "content": prompt}], "stream": False,
            "think": False, "format": schema, "options": {"temperature": 0}, "keep_alive": section["keep_alive"]}
    started = time.time()
    try:
        doc = _chat(section, body, schema)
        answer = _validate(_json_object(doc["message"]["content"]), schema)
    except Exception as exc:  # noqa: BLE001 - any failure means "ask Haiku as before"
        ledger.record_call(f"local:{surface}", False, time.time() - started, 0.0, section["model"],
                           f"failed: {exc}"[:300], scope)
        return None
    # Ollama's own timings (nanoseconds), kept for the performance budget.
    timings = {k: round(doc[k] / 1e9, 3) for k in ("total_duration", "load_duration", "prompt_eval_duration",
                                                    "eval_duration") if isinstance(doc.get(k), int)}
    timings.update({k: doc[k] for k in ("prompt_eval_count", "eval_count") if isinstance(doc.get(k), int)})
    ledger.record_call(f"local:{surface}", True, timings.get("total_duration", time.time() - started), 0.0,
                       section["model"], json.dumps(timings), scope)
    return answer


async def judge(surface: str, prompt: str, schema: dict, cfg: Mapping[str, Any] | None = None,
                *, scope: str = "personal") -> dict | None:
    return await asyncio.to_thread(judge_sync, surface, prompt, schema, cfg, scope=scope)


def record_shadow(surface: str, local: Any, remote: Any, agree: bool, detail: str = "") -> None:
    try:
        ensure_schema()
        with store.connect() as conn:
            conn.execute("INSERT INTO local_llm_shadow(ts, surface, local, remote, agree, detail) VALUES(?, ?, ?, ?, ?, ?)",
                         (time.time(), surface, json.dumps(local), json.dumps(remote), 1 if agree else 0, detail[:300]))
    except Exception as exc:  # noqa: BLE001 - measuring must not break the triage it measures
        log.debug("local: could not record shadow row: %s", exc)


def agreement(days: int = 14) -> list[dict]:
    """Per surface: shadow rows and the share where local and Haiku agreed (the gate in design step 3)."""
    ensure_schema()
    with store.connect() as conn:
        rows = conn.execute("SELECT surface, count(*), sum(agree) FROM local_llm_shadow WHERE ts >= ? GROUP BY surface",
                            (time.time() - days * 86400,)).fetchall()
    return [{"surface": s, "n": n, "agree": round((a or 0) / n, 3)} for s, n, a in rows]


def label_agreement(days: int = 14, keys: tuple[str, ...] = ("urgent", "needs_reply")) -> list[dict]:
    """Per surface: rows whose JSON labels match Haiku's exactly, and the CRITICAL misses (Haiku urgent,
    local not). Haiku is the reference. Rows that are not JSON objects count as unparsed, not as misses."""
    ensure_schema()
    with store.connect() as conn:
        rows = conn.execute("SELECT surface, local, remote FROM local_llm_shadow WHERE ts >= ?",
                            (time.time() - days * 86400,)).fetchall()
    out: dict[str, dict] = {}
    for surface, local_raw, remote_raw in rows:
        s = out.setdefault(surface, {"surface": surface, "n": 0, "unparsed": 0, "label_agree": 0, "critical_misses": 0})
        s["n"] += 1
        try:
            local, remote = json.loads(local_raw), json.loads(remote_raw)
        except (TypeError, ValueError):
            local = remote = None
        if not isinstance(local, dict) or not isinstance(remote, dict):
            s["unparsed"] += 1
            continue
        if all(local.get(k) == remote.get(k) for k in keys):
            s["label_agree"] += 1
        if remote.get("urgent") is True and local.get("urgent") is not True:
            s["critical_misses"] += 1
    for s in out.values():
        judged = s["n"] - s["unparsed"]
        s["label_agree_rate"] = round(s["label_agree"] / judged, 3) if judged else None
    return list(out.values())
