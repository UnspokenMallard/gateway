"""Bifrost sessions against a scratch home, a scratch tmux socket and a fake ACP adapter (tests/fake_acp.py): the
per-kind switch and its pinning, the caps, the tmux runner (a `cat` in place of claude), the ACP runner (asks,
permissions, cancel, resume), and the service's routes. Nothing reaches a model and nothing touches the live `hud`
tmux socket.
Run: ~/.gateway/.venv/bin/python ~/.gateway/tests/test_sessions.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="bifrost-sessions-test-"))
SOCKET = f"gwtest-{os.getpid()}"
os.environ["BIFROST_HOME"] = str(TMP / "bifrost")  # read at import: never the live store
os.environ["HUD_TMUX_SOCKET"] = SOCKET  # a test must never share the live tmux server
ROOT = Path(__file__).resolve().parents[1]
(TMP / "bifrost").mkdir()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path.home() / ".mind" / "mimir"))

from bifrost import sessions, store  # noqa: E402
from bifrost.sessions import acp, tmux  # noqa: E402

WORK = TMP / "work"
WORK.mkdir()


def config(default="tmux", kinds=None, **extra):
    """Write the scratch config.toml: the switch under test and the fake adapter."""
    s = {"default": default, "max": 3, "max_acp": 2, "acp_command": [sys.executable, str(ROOT / "tests" / "fake_acp.py")],
         **extra}
    lines = ["[sessions]"] + [f"{k} = {json.dumps(v)}" for k, v in s.items()]
    lines += ["[sessions.kinds]"] + [f"{k} = {json.dumps(v)}" for k, v in (kinds or {}).items()]
    store.CONFIG.write_text("\n".join(lines) + "\n")


def wait(pred, what, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def refused(fn, *args, status=None, text="", **kw):
    try:
        fn(*args, **kw)
    except sessions.SessionError as exc:
        assert status is None or exc.status == status, (exc.status, str(exc))
        assert text in str(exc), str(exc)
        return exc
    raise AssertionError(f"{getattr(fn, '__name__', fn)} did not raise")


def texts(sid):
    return [e["text"] for e in sessions.events(sid)["events"] if e["type"] == "text"]


def test_switch():
    config(kinds={"task": "acp"})
    assert sessions.runner_for("task") == "acp"
    assert sessions.runner_for("chat") == "tmux"  # not listed: the default
    config(default="acp", kinds={"task": "tmux"})
    assert sessions.runner_for("chat") == "acp" and sessions.runner_for("task") == "tmux"
    config(default="nonsense")
    assert sessions.runner_for("chat") == "tmux"  # an unknown runner falls back to the default, tmux
    store.CONFIG.unlink()
    assert sessions.runner_for("chat") == "tmux" and sessions.switch()["max"] == 10  # no config: all defaults
    config(kinds={"task": "acp"})
    assert sessions.switch() == {"default": "tmux", "kinds": {"task": "acp"}, "max": 3, "max_acp": 2}


def test_bad_requests():
    config()
    refused(sessions.start, "bad id", "task", str(WORK), status=400)
    refused(sessions.start, "ok", "task", str(WORK / "missing"), status=400)
    refused(sessions.start, "ok", "task", str(WORK), scope="nobody", status=400)
    refused(sessions.start, "ok", "", str(WORK), status=400)
    refused(sessions.get, "nope", status=404)
    refused(sessions.start, "nope", "task", str(WORK), resume=True, status=404)
    assert sessions.list_sessions() == []  # a refused start leaves no record


def test_tmux_runner():
    tmux.argv = lambda rec, spec: ["cat"]  # a terminal that echoes what is typed, in place of claude
    config()
    st = sessions.start("t1", "chat", str(WORK), trust=False)
    assert st["runner"] == "tmux" and st["alive"] == "alive" and st["name"] == "gw-t1", st
    assert st["session_id"], "a tmux session gets an id to resume by"
    refused(sessions.start, "t1", "chat", str(WORK), status=409, text="already running")
    sessions.send("t1", "hello gateway")
    wait(lambda: "hello gateway" in sessions.screen("t1")["text"], "typed text on the pane")
    sessions.send("t1", "line one\nline two")  # multi-line goes through the paste buffer
    wait(lambda: "line two" in sessions.screen("t1")["text"], "pasted text on the pane")
    sessions.press("t1", "esc")
    refused(sessions.press, "t1", "f13", status=400)
    sessions.cancel("t1")
    refused(sessions.answer, "t1", n=1, text="not available on the tmux runner")
    refused(sessions.events, "t1", text="not available on the tmux runner")
    stopped = sessions.stop("t1")
    assert stopped["alive"] == "gone" and sessions.get("t1")["stopped_at"], stopped
    refused(sessions.send, "t1", "hi", status=404)  # nothing to type into
    again = sessions.start("t1", "chat", str(WORK), resume=True, trust=False)
    assert again["alive"] == "alive" and again["session_id"] == st["session_id"]
    sessions.forget("t1")
    refused(sessions.get, "t1", status=404)
    assert tmux.status({"name": "gw-t1"}) == "gone"


def test_pinning():
    config(kinds={"task": "tmux"})
    sessions.start("p1", "task", str(WORK), trust=False)
    assert sessions.get("p1")["runner"] == "tmux"
    sessions.stop("p1")
    config(kinds={"task": "acp"})  # the config moves; the session does not
    resumed = sessions.start("p1", "task", str(WORK), resume=True, trust=False)
    assert resumed["runner"] == "tmux", resumed
    sessions.forget("p1")  # the pin goes with the record
    fresh = sessions.start("p1", "task", str(WORK))
    assert fresh["runner"] == "acp", fresh
    sessions.forget("p1")


def test_acp_runner():
    config(kinds={"task": "acp"})
    st = sessions.start("a1", "task", str(WORK), model="sonnet", mode="plan", prompt="first")
    assert st["runner"] == "acp" and st["session_id"] == "fake-1" and st["mode"] == "plan", st
    wait(lambda: "echo: first" in texts("a1"), "the start prompt to run")
    wait(lambda: sessions.state("a1")["status"] == "idle", "idle after the first turn")
    assert sessions.state("a1")["stop_reason"] == "end_turn"
    sessions.send("a1", "hi")
    wait(lambda: "echo: hi" in texts("a1"), "the echo")
    seq = sessions.events("a1")["seq"]
    assert sessions.events("a1", after=seq)["events"] == []  # `after` returns only newer events

    # AskUserQuestion: two questions, answered one after the other
    sessions.send("a1", "ask")
    ask = wait(lambda: sessions.state("a1")["ask"], "the question")
    assert sessions.state("a1")["status"] == "waiting" and ask["kind"] == "question"
    assert (ask["header"], ask["question"], ask["index"], ask["total"], ask["multi"]) == ("Colour", "Which colour?", 0, 2, False)
    assert [(o["n"], o["label"]) for o in ask["options"]] == [(1, "Red"), (2, "Blue")]
    refused(sessions.answer, "a1", n=3, status=400)
    refused(sessions.answer, "a1", n=2, label="Green", text="changed")  # the stale guard: that option moved
    refused(sessions.answer, "a1", n=2, ask_id="r0", text="no longer open")
    sessions.answer("a1", n=2, label="Blue", ask_id=ask["id"], text="a note")
    second = sessions.state("a1")["ask"]
    assert second["index"] == 1 and second["multi"] and second["header"] == "Letters", second
    sessions.answer("a1", ns=[1, 2])
    wait(lambda: sessions.state("a1")["status"] == "idle", "idle after the answers")
    want = {"question_0": "Blue", "question_0_custom": "a note", "question_1": ["A", "B"]}
    assert f"answers {json.dumps(want, sort_keys=True)}" in texts("a1"), texts("a1")
    refused(sessions.answer, "a1", n=1, text="no question is open")

    # a permission request
    sessions.send("a1", "perm")
    ask = wait(lambda: sessions.state("a1")["ask"], "the permission request")
    assert ask["kind"] == "permission" and ask["question"] == "Run ls" and ask["context"] == ["ls"], ask
    assert [o["label"] for o in ask["options"]] == ["Allow once", "Reject"]
    sessions.answer("a1", n=1)
    wait(lambda: 'permission "allow-once"' in texts("a1"), "the permission outcome")
    wait(lambda: sessions.state("a1")["status"] == "idle", "idle after the permission")

    # cancel ends a turn that does not end by itself
    sessions.send("a1", "slow")
    wait(lambda: sessions.state("a1")["status"] == "running", "a running turn")
    assert sessions.state("a1")["busy"]
    sessions.cancel("a1")
    wait(lambda: sessions.state("a1")["status"] == "idle", "idle after cancel")
    assert sessions.state("a1")["stop_reason"] == "cancelled"

    # cancel also closes an open question instead of leaving it pending
    sessions.send("a1", "ask")
    wait(lambda: sessions.state("a1")["ask"], "the question")
    sessions.cancel("a1")
    wait(lambda: sessions.state("a1")["status"] == "idle", "idle after cancelling a question")

    sessions.set_mode("a1", "default")
    assert sessions.state("a1")["mode"] == "default"
    refused(sessions.screen, "a1", text="not available on the acp runner")
    refused(sessions.press, "a1", "esc", text="not available on the acp runner")

    # stop keeps the record; a resume needs the adapter's session id
    sessions.stop("a1")
    assert sessions.state("a1")["status"] == "stopped" and sessions.state("a1")["alive"] == "gone"
    refused(sessions.send, "a1", "hi", status=404)
    back = sessions.start("a1", "task", str(WORK), resume=True)
    assert back["runner"] == "acp" and back["session_id"] == "fake-1" and back["status"] == "idle", back
    sessions.forget("a1")
    assert acp.LIVE == {}


def test_acp_resume_needs_an_id():
    config(kinds={"task": "acp"})
    sessions.start("a2", "task", str(WORK))
    sessions.stop("a2")
    rec = sessions.get("a2")
    rec["session_id"] = None
    sessions._put(rec)
    refused(sessions.start, "a2", "task", str(WORK), resume=True, status=400, text="session id")
    assert sessions.get("a2")["stopped_at"], "a failed resume leaves the record as it was"
    sessions.forget("a2")


def test_caps():
    config(kinds={"task": "acp"})  # max 3 in all, max_acp 2
    sessions.start("c1", "task", str(WORK))
    sessions.start("c2", "task", str(WORK))
    refused(sessions.start, "c3", "task", str(WORK), status=409, text="ACP session cap")
    sessions.start("c3", "chat", str(WORK), trust=False)  # a tmux session still fits under the total
    refused(sessions.start, "c4", "chat", str(WORK), status=409, text="session cap reached")
    assert sorted(s["id"] for s in sessions.list_sessions()) == ["c1", "c2", "c3"]
    for sid in ("c1", "c2", "c3"):
        sessions.forget(sid)
    assert sessions.list_sessions() == [] and acp.count() == 0 and tmux.count() == 0


def test_dead_adapter():
    config(kinds={"task": "acp"})
    sessions.start("d1", "task", str(WORK))
    acp.LIVE["d1"].stop()
    wait(lambda: sessions.state("d1")["status"] == "stopped", "stopped after the adapter died")
    refused(sessions.send, "d1", "hi", text="not running")
    sessions.forget("d1")


def test_failed_adapter_start():
    config(kinds={"task": "acp"}, acp_command=["/nonexistent/claude-agent-acp"])
    refused(sessions.start, "f1", "task", str(WORK), status=409, text="cannot start the ACP adapter")
    assert sessions.list_sessions() == [] and acp.LIVE == {}


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok", name)
    finally:
        subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)
        for s in list(acp.LIVE.values()):
            s.stop()
