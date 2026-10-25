"""The ACP runner: one `claude-agent-acp` adapter process per session, spoken to over stdio in Agent Client Protocol v1
(NDJSON JSON-RPC), as the 2026-10-25 spike did (`~/.jarvis/spike/acp/FINDINGS.md`, `client.mjs`).

No client library: a reader thread routes responses to their waiting call, turns the adapter's requests
(`session/request_permission`, `elicitation/create` = AskUserQuestion) into an `ask` that `answer()` closes later, and
keeps the updates as a bounded event list. v1 has no state event, so the state is derived: a prompt pending is running,
an open request is waiting, otherwise idle. v1 has no heartbeat either: `quiet_for` is the seconds since the last
update, and the caller decides when quiet means silent.

Stdlib only. The adapter is pinned in `acp/package.json` (v2 of the protocol is a draft; stay on v1).
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import IO, Any

from .. import store
from ..proc import _kill_tree
from .base import BadRequest, NotFound, SessionError, session_env

EVENTS_KEPT = 2000
LIVE: dict[str, "Session"] = {}


def command(cfg: dict | None = None) -> list[str]:
    """The adapter's command line: `[sessions] acp_command`, else the one installed in `~/.gateway/acp`."""
    cfg = cfg if cfg is not None else store.load_config()
    custom = (cfg.get("sessions") or {}).get("acp_command")
    return [str(c) for c in custom] if custom else [str(store.ROOT / "acp" / "node_modules" / ".bin" / "claude-agent-acp")]


def _context(call: dict) -> list[str]:
    """The lines under a permission question: what the tool is about to do."""
    raw = call.get("rawInput") or {}
    lines = [str(raw[k])[:300] for k in ("command", "file_path", "path", "url", "description") if raw.get(k)]
    if raw.get("plan"):
        lines += str(raw["plan"]).split("\n")[:40]
    return lines


def _permission_ask(params: dict) -> tuple[dict, list]:
    call = params.get("toolCall") or {}
    options = params.get("options") or []
    return {"kind": "plan" if call.get("kind") == "switch_mode" else "permission", "header": call.get("kind"),
            "question": call.get("title") or "Permission requested", "context": _context(call), "multi": False,
            "custom": False, "options": [{"n": i + 1, "label": o.get("name"), "description": None, "on": i == 0}
                                         for i, o in enumerate(options)]}, [o.get("optionId") for o in options]


def _questions(params: dict) -> list[dict]:
    """The form's questions: `question_N` fields, each a single choice (`oneOf`) or a multiple one (array of `anyOf`)."""
    props = (params.get("requestedSchema") or {}).get("properties") or {}
    out = []
    for key, spec in props.items():
        if key.endswith("_custom"):
            continue
        multi = spec.get("type") == "array"
        choices = (spec.get("items") or {}).get("anyOf") if multi else spec.get("oneOf")
        out.append({"key": key, "header": spec.get("title"), "multi": multi, "custom": f"{key}_custom" in props,
                    "question": spec.get("description") or params.get("message") or spec.get("title") or key,
                    "options": [{"label": c.get("const"), "description": c.get("description")} for c in choices or []]})
    return out


class Session:
    def __init__(self, rec: dict, spec: dict, cfg: dict):
        self.rec, self.id, self.spec, self.cfg = rec, rec["id"], spec, cfg
        self.lock = threading.RLock()
        self.next_rpc, self.calls = 1, {}  # rpc id -> Future of a call we made
        self.requests: dict[str, dict] = {}  # ask id -> an open request from the adapter, oldest first
        self.events: list[dict] = []
        self.seq = 0
        self.prompts, self.turn_started = 0, None
        self.last_update = time.time()
        self.ready = False
        self.session_id: str | None = None
        self.mode = self.usage = self.stop_reason = None
        self.log = store.STATE / "logs" / f"acp-{self.id}.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.errlog = open(self.log, "ab")
        try:
            self.proc: subprocess.Popen[str] = subprocess.Popen(
                command(cfg), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errlog, cwd=rec["cwd"],
                env=session_env(rec), start_new_session=True, text=True, encoding="utf-8", bufsize=1)
        except OSError as exc:
            self.errlog.close()
            raise SessionError(f"cannot start the ACP adapter {command(cfg)[0]}: {exc}") from exc
        self.stdin: IO[str] = self.proc.stdin  # type: ignore[assignment]  # piped above
        self.stdout: IO[str] = self.proc.stdout  # type: ignore[assignment]
        threading.Thread(target=self._read, daemon=True, name=f"acp-{self.id}").start()

    # ---- wire
    def _write(self, obj: dict) -> None:
        with self.lock:
            try:
                self.stdin.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
                self.stdin.flush()
            except (OSError, ValueError) as exc:  # the adapter is gone
                raise SessionError(f"the ACP adapter is not running: {exc}") from exc

    def call(self, method: str, params: dict) -> Future:
        fut: Future = Future()
        with self.lock:
            rpc, self.next_rpc = self.next_rpc, self.next_rpc + 1
            self.calls[rpc] = fut
        try:
            self._write({"id": rpc, "method": method, "params": params})
        except SessionError as exc:
            with self.lock:
                self.calls.pop(rpc, None)
            fut.set_exception(exc)
        return fut

    def request(self, method: str, params: dict, timeout: float = 60) -> Any:
        try:
            return self.call(method, params).result(timeout)
        except FutureTimeout as exc:
            raise SessionError(f"the ACP adapter did not answer {method} in {timeout:.0f}s") from exc

    def _read(self) -> None:
        for line in self.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m and "method" not in m:
                with self.lock:
                    fut = self.calls.pop(m["id"], None)
                if fut is not None:
                    err = m.get("error")
                    fut.set_exception(SessionError(f"ACP: {err.get('message')}")) if err else fut.set_result(m.get("result") or {})
            elif "id" in m:
                self._open_request(m)
            elif m.get("method") == "session/update":
                self._update((m.get("params") or {}).get("update") or {})
        with self.lock:  # the adapter exited: fail every call still waiting
            self.ready = False
            for fut in self.calls.values():
                fut.set_exception(SessionError("the ACP adapter exited"))
            self.calls.clear()
            self._push("exit", code=self.proc.poll())

    # ---- what the adapter tells us
    def _push(self, type_: str, **data: Any) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "at": round(time.time(), 3), "type": type_, **data})
            del self.events[:-EVENTS_KEPT]

    def _update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        with self.lock:
            self.last_update = time.time()
            if kind == "agent_message_chunk":
                self._push("text", text=(u.get("content") or {}).get("text") or "")
            elif kind in ("tool_call", "tool_call_update"):
                self._push("tool", id=u.get("toolCallId"), title=u.get("title"), status=u.get("status"), tool=u.get("kind"))
            elif kind == "plan":
                self._push("plan", entries=u.get("entries"))
            elif kind == "usage_update":
                self.usage = {"used": u.get("used"), "size": u.get("size")}
            elif kind == "current_mode_update":
                self.mode = u.get("currentModeId")

    def _open_request(self, m: dict) -> None:
        method, params, rpc = m["method"], m.get("params") or {}, m["id"]
        if method == "session/request_permission":
            ask, option_ids = _permission_ask(params)
            req = {"method": method, "option_ids": option_ids}
        elif method == "elicitation/create":
            questions = _questions(params)
            if not questions:  # nothing to ask: decline rather than hang the turn
                return self._write({"id": rpc, "result": {"action": "cancel"}})
            ask = {"kind": "question"}
            req = {"method": method, "questions": questions, "answers": {}, "done": 0}
        else:
            return self._write({"id": rpc, "error": {"code": -32601, "message": f"unsupported: {method}"}})
        with self.lock:
            req["rpc"] = rpc
            ask["id"] = f"r{rpc}"
            req["ask"] = ask
            self.requests[ask["id"]] = req
            self.last_update = time.time()
            self._push("ask", ask=self._view(req))

    @staticmethod
    def _view(req: dict) -> dict:
        """The ask as the caller sees it: for a form, its first unanswered question, with how many there are."""
        if req["method"] != "elicitation/create":
            return {**req["ask"], "index": 0, "total": 1}
        done = req["done"]
        q = req["questions"][done]
        return {"id": req["ask"]["id"], "kind": "question", "header": q["header"], "question": q["question"], "context": [],
                "multi": q["multi"], "custom": q["custom"], "index": done, "total": len(req["questions"]),
                "options": [{"n": i + 1, **o, "on": i == 0} for i, o in enumerate(q["options"])]}

    def _current(self) -> dict | None:
        return next(iter(self.requests.values()), None)

    def _respond(self, req: dict, result: dict) -> None:
        self.requests.pop(req["ask"]["id"], None)
        self._write({"id": req["rpc"], "result": result})
        self.last_update = time.time()
        self._push("answered", ask=req["ask"]["id"])

    # ---- what the caller does
    def start(self) -> None:
        spec, rec = self.spec, self.rec
        try:
            self.request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "bifrost-sessions", "version": "1"},
                                        "clientCapabilities": {"elicitation": {"form": {}}}}, timeout=30)
            params = {"cwd": rec["cwd"], "mcpServers": []}
            if spec.get("resume"):
                if not spec.get("session_id"):
                    raise BadRequest("an ACP session resumes by its session id: none was given")
                self.session_id = spec["session_id"]
                self.request("session/resume", {"sessionId": self.session_id, **params})
            else:
                self.session_id = self.request("session/new", params)["sessionId"]
            for config_id, value in (("model", rec["model"]), ("mode", spec.get("mode"))):
                if value:
                    self.request("session/set_config_option",
                                 {"sessionId": self.session_id, "configId": config_id, "type": "id", "value": value})
            self.mode = spec.get("mode") or self.mode
        except Exception:
            self.stop()
            raise
        self.ready = True
        if spec.get("prompt") and not spec.get("resume"):
            self.send(spec["prompt"])

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, text: str) -> None:
        if not self.ready or not self.alive():
            raise SessionError("the ACP session is not running")
        with self.lock:
            self.prompts += 1
            self.turn_started = self.turn_started or time.time()
            self.stop_reason = None
            self.last_update = time.time()
        fut = self.call("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": str(text)}]})
        fut.add_done_callback(self._turn_done)

    def _turn_done(self, fut: Future) -> None:
        with self.lock:
            self.prompts -= 1
            self.last_update = time.time()
            if fut.exception() is not None:
                self._push("error", message=str(fut.exception()))
            else:
                out = fut.result()
                self.stop_reason = out.get("stopReason")
                self._push("turn_end", stop_reason=self.stop_reason, usage=out.get("usage"))
            if self.prompts == 0:
                self.turn_started = None

    def answer(self, n: int | None, ns: list[int] | None, text: str | None, label: str | None, cancel: bool,
               ask_id: str | None) -> None:
        with self.lock:
            req = self._current()
            if req is None:
                raise SessionError("no question is open")
            view = self._view(req)
            if ask_id and ask_id != view["id"]:
                raise SessionError("that question is no longer open")  # only answer what is on offer now
            if cancel:
                return self._respond(req, {"action": "cancel"} if req["method"] == "elicitation/create"
                                     else {"outcome": {"outcome": "cancelled"}})
            picks = ns if ns else ([n] if n else [])
            if any(not 1 <= p <= len(view["options"]) for p in picks):
                raise BadRequest(f"option out of range 1..{len(view['options'])}")
            if label is not None and picks and view["options"][picks[0] - 1]["label"] != label:
                raise SessionError("the question changed: that option is not what it was")
            if req["method"] == "session/request_permission":
                if len(picks) != 1:
                    raise BadRequest("pick one option")
                return self._respond(req, {"outcome": {"outcome": "selected", "optionId": req["option_ids"][picks[0] - 1]}})
            q = req["questions"][view["index"]]
            if not picks and not (text and q["custom"]):
                raise BadRequest("pick an option" + (" or give text" if q["custom"] else ""))
            if len(picks) > 1 and not q["multi"]:
                raise BadRequest("this question takes one option")
            labels = [view["options"][p - 1]["label"] for p in picks]
            if labels:  # text alone sets only the `_custom` field: the form allows it
                req["answers"][q["key"]] = labels if q["multi"] else labels[0]
            if text:
                req["answers"][f"{q['key']}_custom"] = str(text)
            req["done"] += 1
            if req["done"] == len(req["questions"]):
                self._respond(req, {"action": "accept", "content": req["answers"]})

    def cancel(self) -> None:
        with self.lock:
            for req in list(self.requests.values()):  # a cancelled turn must not leave a request unanswered
                self._respond(req, {"action": "cancel"} if req["method"] == "elicitation/create"
                              else {"outcome": {"outcome": "cancelled"}})
        self._write({"method": "session/cancel", "params": {"sessionId": self.session_id}})

    def set_mode(self, mode: str) -> None:
        self.request("session/set_config_option", {"sessionId": self.session_id, "configId": "mode", "type": "id", "value": mode})
        self.mode = mode

    def stop(self) -> None:
        _kill_tree(self.proc)  # the adapter leads its process group: its children go with it, as `kill-session` does
        self.errlog.close()

    def state(self) -> dict:
        now = time.time()
        with self.lock:
            req = self._current()
            status = ("stopped" if not self.alive() else "starting" if not self.ready else "waiting" if req
                      else "running" if self.prompts else "idle")
            return {"runner": "acp", "status": status, "alive": "alive" if self.alive() else "dead",
                    "ask": self._view(req) if req else None, "queued_asks": max(len(self.requests) - 1, 0),
                    "busy": self.prompts > 0, "busy_for": round(now - self.turn_started) if self.turn_started else 0,
                    "quiet_for": round(now - self.last_update), "mode": self.mode, "model": self.rec["model"],
                    "session_id": self.session_id, "stop_reason": self.stop_reason, "usage": self.usage, "seq": self.seq}

    def events_after(self, after: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > after]


# ---- the runner interface (sessions/__init__.py calls these with the registry record)
def _live(rec: dict) -> Session:
    s = LIVE.get(rec["id"])
    if s is None:
        raise NotFound(f"session {rec['id']} has no running ACP adapter: start it again to resume it")
    return s


def count() -> int:
    return sum(1 for s in LIVE.values() if s.alive())


def status(rec: dict) -> str:
    s = LIVE.get(rec["id"])
    return "gone" if s is None else "alive" if s.alive() else "dead"


def start(rec: dict, spec: dict) -> dict:
    old = LIVE.pop(rec["id"], None)
    if old is not None:
        old.stop()
    s = Session(rec, spec, store.load_config())
    s.start()
    LIVE[rec["id"]] = s
    return {"session_id": s.session_id}


def state(rec: dict) -> dict:
    s = LIVE.get(rec["id"])
    if s is None:
        return {"runner": "acp", "status": "stopped", "alive": "gone", "ask": None, "resumable": bool(rec.get("session_id")),
                "session_id": rec.get("session_id")}
    return {**s.state(), "resumable": bool(s.session_id)}


def send(rec: dict, text: str) -> None:
    _live(rec).send(text)


def answer(rec: dict, *, n: int | None = None, ns: list[int] | None = None, text: str | None = None,
           label: str | None = None, cancel: bool = False, ask_id: str | None = None) -> None:
    _live(rec).answer(n, ns, text, label, cancel, ask_id)


def cancel(rec: dict) -> None:
    _live(rec).cancel()


def set_mode(rec: dict, mode: str) -> None:
    _live(rec).set_mode(mode)


def events(rec: dict, after: int = 0) -> dict:
    s = _live(rec)
    return {"events": s.events_after(after), "seq": s.seq}


def stop(rec: dict) -> None:
    s = LIVE.pop(rec["id"], None)
    if s is not None:
        s.stop()
