"""Sessions: a long-lived Claude Code session, driven through one of two runners.

`tmux` is the TUI in a detached tmux session (the HUD's way until now). `acp` is the `claude-agent-acp` adapter spoken
to over stdio in Agent Client Protocol v1. A session uses one or the other, never both: `config.toml` `[sessions]` names
the runner per session *kind* (`kinds`, else `default`, which is tmux), the first start pins that runner in
`state/sessions.json`, and every later call (and a resume) uses the pinned one, so editing the config never moves a
running or resumable session. `forget()` drops the pin.

Unlike `ask()`/`run_agent()`, these functions raise `SessionError` (it carries the HTTP status the service answers
with): a session call is an action on one named session, and "could not" is the answer the caller needs.
Bifrost makes the call and reports what happened; it never alerts, pings or files.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

from .. import store
from ..claude import SCOPES
from . import acp, tmux
from .base import BadRequest, NotFound, SessionError

__all__ = ["BadRequest", "NotFound", "RUNNERS", "SessionError", "answer", "cancel", "events", "forget", "get",
           "list_sessions", "press", "runner_for", "screen", "send", "set_mode", "start", "state", "stop", "switch"]

RUNNERS = {"tmux": tmux, "acp": acp}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # no '.' or ':': tmux reads them as window and pane
log = logging.getLogger("bifrost.sessions")
_file = threading.RLock()
_starting = threading.Lock()


def _section(cfg: dict | None = None) -> dict:
    return ((cfg if cfg is not None else store.load_config()).get("sessions")) or {}


def runner_for(kind: str, cfg: dict | None = None) -> str:
    """The runner a new session of this kind gets. An unknown value in the config falls back to tmux, the default."""
    s = _section(cfg)
    value = str((s.get("kinds") or {}).get(kind, s.get("default", "tmux")))
    if value not in RUNNERS:
        log.warning("[sessions] %r is not a runner (tmux or acp): using tmux for kind %r", value, kind)
        return "tmux"
    return value


def switch(cfg: dict | None = None) -> dict:
    """The switch as configured, for a caller that wants to show or check it."""
    s = _section(cfg)
    return {"default": runner_for("", cfg), "kinds": {str(k): str(v) for k, v in (s.get("kinds") or {}).items()},
            "max": int(s.get("max", 10)), "max_acp": int(s.get("max_acp", 4))}


# ---- the registry: state/sessions.json, {id: record}
def _path():
    return store.STATE / "sessions.json"


def _load() -> dict[str, dict]:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(reg: dict[str, dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _put(rec: dict) -> None:
    with _file:
        reg = _load()
        reg[rec["id"]] = rec
        _save(reg)


def _drop(sid: str) -> None:
    with _file:
        reg = _load()
        if reg.pop(sid, None) is not None:
            _save(reg)


def get(sid: str) -> dict:
    rec = _load().get(sid)
    if rec is None:
        raise NotFound(f"no session {sid!r}")
    return rec


def _call(sid: str, fn: str, *args, **kw):
    rec = get(sid)
    f = getattr(RUNNERS[rec["runner"]], fn, None)
    if f is None:
        raise SessionError(f"{fn} is not available on the {rec['runner']} runner")
    return f(rec, *args, **kw)


# ---- calls
def start(sid: str, kind: str, cwd: str, *, scope: str = "work", model: str | None = None, title: str | None = None,
          prompt: str | None = None, session_id: str | None = None, resume: bool = False, name: str | None = None,
          trust: bool = True, mode: str | None = None) -> dict:
    """Start (or, with `resume`, resume) the session `sid`, on the runner the config names for `kind`: a resume uses the
    runner the session was pinned to. Returns its state. 409 when it already runs or a cap is reached."""
    cfg = store.load_config()
    name = name or f"gw-{sid}"
    if not NAME.match(sid) or not NAME.match(name):
        raise BadRequest("id and name: letters, digits, '_' and '-', at most 64, starting with a letter or digit")
    if not kind or not os.path.isdir(cwd):
        raise BadRequest("kind is required and cwd must be a directory")
    if scope not in SCOPES:
        raise BadRequest(f"scope must be one of {', '.join(SCOPES)}")
    s = _section(cfg)
    with _starting:  # one start at a time: the caps below count what is running now
        old = _load().get(sid) or {}
        if old and status(old) == "alive":
            raise SessionError(f"session {sid} is already running")
        if resume and not old:
            raise NotFound(f"no session {sid!r} to resume")
        runner = old["runner"] if resume else runner_for(kind, cfg)
        if tmux.count() + acp.count() >= int(s.get("max", 10)):
            raise SessionError(f"session cap reached ({s.get('max', 10)}): stop one first")
        if runner == "acp" and acp.count() >= int(s.get("max_acp", 4)):
            raise SessionError(f"ACP session cap reached ({s.get('max_acp', 4)}): each adapter holds about 0.6 GB")
        sess = session_id or (old.get("session_id") if resume else None)
        if runner == "tmux" and not resume:
            sess = sess or str(uuid.uuid4())  # a tmux session resumes by this id
        rec = {"id": sid, "kind": kind, "runner": runner, "cwd": cwd, "scope": scope, "name": name,
               "model": model or (old.get("model") if resume else None) or str(s.get("model", "sonnet")),
               "title": title, "session_id": sess, "started_at": time.time(), "stopped_at": None}
        spec = {"prompt": prompt, "title": title, "session_id": sess, "resume": resume, "trust": trust, "mode": mode,
                **{k: v for k, v in s.items() if k in ("acp_silent_s",)}}
        out = RUNNERS[runner].start(rec, spec)  # a failed start raises before anything is recorded
        rec["session_id"] = out.get("session_id") or rec["session_id"]
        _put(rec)
    return state(sid)


def status(rec: dict) -> str:
    return RUNNERS[rec["runner"]].status(rec)


def state(sid: str) -> dict:
    rec = get(sid)
    st = RUNNERS[rec["runner"]].state(rec)
    return {"id": sid, "kind": rec["kind"], "runner": rec["runner"], "name": rec["name"], "cwd": rec["cwd"],
            "scope": rec["scope"], "model": rec["model"], "title": rec.get("title"), **st,
            "session_id": st.get("session_id") or rec.get("session_id")}


def list_sessions() -> list[dict]:
    return [state(sid) for sid in sorted(_load())]


def send(sid: str, text: str) -> None:
    if not str(text or "").strip():
        raise BadRequest("nothing to send")
    _call(sid, "send", str(text))


def press(sid: str, key: str) -> None:
    _call(sid, "press", key)


def answer(sid: str, **choice) -> None:
    _call(sid, "answer", **choice)


def cancel(sid: str) -> None:
    _call(sid, "cancel")


def set_mode(sid: str, mode: str) -> None:
    _call(sid, "set_mode", mode)


def screen(sid: str, lines: int | None = None) -> dict:
    return _call(sid, "screen", lines)


def events(sid: str, after: int = 0) -> dict:
    return _call(sid, "events", after)


def stop(sid: str) -> dict:
    """End the session's process. The record stays, so the session can be resumed."""
    rec = get(sid)
    RUNNERS[rec["runner"]].stop(rec)
    rec["stopped_at"] = time.time()
    _put(rec)
    return state(sid)


def forget(sid: str) -> None:
    """Stop the session and drop its record and runner pin."""
    rec = get(sid)
    RUNNERS[rec["runner"]].stop(rec)
    _drop(sid)
