"""Bifrost against a scratch home and a scratch Fafnir on a scratch Mimir: the argument lists, routing by surface, the
parse, the usage-limit record, the ledger rows, run_agent's gate, retries and cap, the service's
guard, and the CLI. Nothing reaches a model: the process runner is replaced.
Run: ~/.gateway/.venv/bin/python ~/.gateway/tests/test_bifrost.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="bifrost-test-"))
os.environ["BIFROST_HOME"] = str(TMP / "bifrost")  # read at import: never the live store
os.environ["FAFNIR_HOME"] = str(TMP / "budget")
os.environ["MEMORY_DB"] = str(TMP / "memory.db")  # Mimir's store, a scratch one
os.environ["MIMIR_OUTBOX"] = str(TMP / "outbox")
ROOT = Path(__file__).resolve().parents[1]
(TMP / "bifrost").mkdir()
shutil.copy(ROOT / "config.toml", TMP / "bifrost" / "config.toml")  # the live surfaces, scratch data
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path.home() / ".mind" / "mimir"))

import mem  # noqa: E402
import mimirrecords as mr  # noqa: E402

mr.use_local(mem.connect(shared=True))  # Fafnir's ledger goes to the scratch store in-process, no HTTP

import bifrost  # noqa: E402
from bifrost import agent, claude, proc, store  # noqa: E402
from bifrost._budget import ledger, limits  # noqa: E402

RAN: list = []


def fake_run(answer: str, code: int = 0):
    def run(cmd, timeout_s=None, cwd=None, input_text=None, env=None, sink=None):
        RAN.append({"cmd": cmd, "input": input_text, "env": env, "cwd": cwd})
        return code, answer
    return run


def rows(stream: str, *fields: str, n: int = 1):
    """The newest `n` rows of one Fafnir ledger stream, each as a tuple of `fields`."""
    got = mr.query(stream=stream, source="fafnir", limit=n, order="desc")
    return [tuple(r["data"].get(f) for f in fields) for r in got]


def test_command():
    iso = bifrost.command("claude", "sonnet", 1.0, allowed_tools=claude.READONLY_TOOLS, isolated=True)
    # Exactly brain's isolated flags before the move: G15 measured the proposer on them.
    assert iso == ["claude", "-p", "--model", "sonnet", "--output-format", "json", "--strict-mcp-config", "--safe-mode",
                   "--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk", "--allowedTools", "Read Grep Glob",
                   "--max-budget-usd", "1.00"], iso
    bare = bifrost.command("claude", "haiku", None, isolated=True, bare=True, system="one line")
    assert bare[bare.index("--setting-sources") + 1] == "" and "--no-session-persistence" in bare
    assert bare[bare.index("--tools") + 1] == "" and "--max-budget-usd" not in bare
    assert bare[bare.index("--system-prompt") + 1] == "one line"
    plain = bifrost.command("claude", "sonnet", 1.0, prompt="hi")
    assert plain[:3] == ["claude", "-p", "hi"] and "Bash" in plain[plain.index("--disallowedTools") + 1]


def test_routing_and_parse():
    assert bifrost.mode("escalation") == "claude:sonnet" and bifrost.mode("nobody") == "off"
    assert bifrost.model_for("job:checkups") == "sonnet" and bifrost.model_for("nobody") is None
    assert bifrost.ask("nobody", "p")["ok"] is False  # an unknown surface never reaches a model
    proc_run, claude.proc.run = claude.proc.run, fake_run(
        json.dumps({"type": "result", "subtype": "success", "result": '```json\n{"project": "jarvis"}\n```',
                    "total_cost_usd": 0.002, "stop_reason": "end_turn"}))
    try:
        out = bifrost.ask("hud-task-classify", "which?", {}, "work", system="JSON only")
        assert out["ok"] and out["answer"] == {"project": "jarvis"} and out["model"] == "haiku", out
        sent = RAN[-1]
        assert sent["input"] == "which?" and sent["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude-work")
        assert "--setting-sources" in sent["cmd"] and sent["cmd"][sent["cmd"].index("--model") + 1] == "haiku"
        assert rows("fafnir.provider_call", "surface", "ok", "cost_usd", "model", "scope") == \
            [("hud-task-classify", 1, 0.002, "haiku", "work")]
        bifrost.ask("mind-session-history", "s", {}, "personal", job="session-history")
        assert rows("fafnir.job_call", "job", "ok", "cost_usd") == [("session-history", 1, 0.002)]
        claude.proc.run = fake_run("API Error: 429 Too Many Requests", code=1)
        out = bifrost.call("escalation", "x", budget_usd=0.5)
        assert out["ok"] is False and out["limit"] is not None and out["reason"].startswith("usage limit")
        assert limits.blocked("claude")[0]  # recorded in Fafnir's window; the caller alerts
        limits.clear("claude")
        claude.proc.run = fake_run("not json at all")
        assert "unparseable" in bifrost.call("escalation", "x")["reason"]
    finally:
        claude.proc.run = proc_run


def test_store_path():
    assert store.STATE == TMP / "bifrost" / "state" and not hasattr(store, "connect")  # no database of its own


def test_run_agent():
    log = TMP / "job.log"
    done = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.4, "num_turns": 3,
                       "result": "wrote it", "permission_denials": [{"tool_name": "Bash"}]})
    saved = agent.RUN
    try:
        agent.RUN = fake_run("noise\n" + done)
        out = bifrost.run_agent("checkups", "do it", "Read,Write", ["Read(//x/**)"], 2.0, log_file=log)
        cmd = RAN[-1]["cmd"]
        assert out["ok"] and out["code"] == 0 and out["cost_usd"] == 0.4 and out["denials"] == ["Bash"], out
        assert cmd[cmd.index("--model") + 1] == "sonnet" and "--disallowedTools" in cmd and RAN[-1]["input"] == "do it"
        assert rows("fafnir.job_call", "job", "ok", "cost_usd", "model") == \
            [("checkups", 1, 0.4, "sonnet")]
        assert rows("fafnir.job_call", "detail", "reason", "attempt") == \
            [("success turns=3 attempt=1 denials=1[Bash]", "", 1)]  # names of the denied tools, not just a count
        agent.reject(out, "no artifact")  # the job refuses the output of a call that succeeded
        # the call row stays as written: the refusal is its own row, joined to the call by uid
        assert rows("fafnir.job_reject", "job_uid", "reason") == [(out["ledger_id"], "no artifact")]
        assert ledger.wasted_spend(7) == [{"job": "checkups", "rows": 1, "usd": 0.4}]  # and Fafnir counts it wasted
        assert "wrote it" in log.read_text()
        n = len(RAN)
        agent.RUN = fake_run("fetch failed", code=1)
        out = bifrost.run_agent("checkups", "p", "Read", None, 1.0, max_attempts=2, backoff_s=0)
        assert out["code"] == 1 and len(RAN) == n + 2  # a transient error is retried, then given up
        assert rows("fafnir.job_call", "ok", "reason", "attempt", n=2) == \
            [(0, "no-result", 2), (0, "no-result", 1)]  # each attempt is a row with its number and why it was wasted
        agent.RUN = fake_run(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "total_cost_usd": 1.0}), 1)
        out = bifrost.run_agent("checkups", "p", "Read", None, 1.0, max_attempts=3, backoff_s=0)
        assert out["subtype"] == "error_max_budget_usd" and len(RAN) == n + 3  # the cap is never retried
        assert bifrost.run_agent("nobody", "p", "Read")["subtype"] == "not-started"  # no surface, no call
        real = ledger.job_budget
        ledger.job_budget = lambda cfg=None, scope="personal": {"spent": 41.0, "ceiling": 40.0, "over": True,
                                                                 "paused_for_optional": True}
        try:
            out = bifrost.run_agent("checkups", "p", "Read")
            assert out["subtype"] == "not-started" and "budget" in out["reason"] and len(RAN) == n + 3
        finally:
            ledger.job_budget = real
    finally:
        agent.RUN = saved



def test_proc_timeout_kills():
    code, out = proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
    assert code == 124 and "timeout" in out
    code, out = proc.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="hi")
    assert code == 0 and out.strip() == "HI"


def test_service_and_cli():
    import app
    from starlette.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:3027")
    assert c.get("/health").json()["ok"]
    assert c.get("/surfaces").json()["hud-whereami"] == "claude:haiku"
    assert c.post("/ask", json={"surface": "hud-x", "prompt": "p"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/ask", json={"surface": "escalation", "prompt": "p"}).status_code == 403  # HUD surfaces only
    assert c.post("/ask", json={}).status_code == 400
    assert TestClient(app.app, base_url="http://evil.example").get("/health").status_code == 400
    proc_run, claude.proc.run = claude.proc.run, fake_run(json.dumps({"result": "Add the thing", "total_cost_usd": 0.001}))
    try:
        r = c.post("/ask", json={"surface": "hud-commit-message", "prompt": "p", "system": "one line"}).json()
        assert r["ok"] and r["text"] == "Add the thing" and r["limited"] is False, r
    finally:
        claude.proc.run = proc_run
    env = {**os.environ, "BIFROST_HOME": str(TMP / "bifrost"), "FAFNIR_HOME": str(TMP / "budget")}
    s = subprocess.run([str(ROOT / "bin" / "gateway"), "surfaces"], capture_output=True, text=True, env=env)
    assert s.returncode == 0 and "job:implementer" in s.stdout


def test_auth_modes():
    set_env = {"ANTHROPIC_API_KEY": "sk-ambient", "ANTHROPIC_AUTH_TOKEN": "tok", "PATH": "/bin"}
    env = claude.env_for("work", set_env, "login")  # login: the subscription, whatever is ambient
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"].endswith(".claude-work")
    assert claude.env_for("work", set_env)["ANTHROPIC_API_KEY"] == "sk-ambient"  # auth=None: unchanged
    assert "HUD_TASK_ID" not in claude.env_for("work", {**set_env, "HUD_TASK_ID": "hodr-9693"})
    assert claude.auth_problem("work", "login") is None and "unknown auth mode" in claude.auth_problem("work", "api-key")
    done = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.1, "num_turns": 1, "result": "ok"})
    saved, proc_run = agent.RUN, claude.proc.run
    try:
        agent.RUN = fake_run(done)
        n = len(RAN)
        out = bifrost.run_agent("proposer", "p", "Read", None, 1.0, scope="personal", auth="login", max_attempts=1)
        assert out["ok"] and "ANTHROPIC_API_KEY" not in RAN[-1]["env"]
        out = bifrost.run_agent("proposer", "p", "Read", None, 1.0, scope="work", auth="api-key")
        assert out["subtype"] == "not-started" and "unknown auth mode" in out["reason"] and len(RAN) == n + 1  # refused before any call
        claude.proc.run = fake_run(json.dumps({"type": "result", "subtype": "success", "result": "hi", "total_cost_usd": 0.01}))
        out = bifrost.ask("hud-task-classify", "q", scope="work", auth="login")
        assert out["ok"] and "ANTHROPIC_API_KEY" not in RAN[-1]["env"]
        assert bifrost.ask("hud-task-classify", "q", scope="work", auth="api-key")["ok"] is False
    finally:
        agent.RUN, claude.proc.run = saved, proc_run


def test_job_session_strict_mcp():
    # Every job session starts no MCP servers by default, and the flag is never doubled.
    args = agent.command("claude", "sonnet", "s1", "Read", None, 1.0)
    assert args.count("--strict-mcp-config") == 1, args
    with_flags = agent.command("claude", "sonnet", "s1", "Read", None, 1.0, flags=["--safe-mode"])
    assert with_flags.count("--strict-mcp-config") == 1 and "--safe-mode" in with_flags, with_flags
    # Launcher guard: no component builds its own job flags. The flag lives in Bifrost only.
    home = Path.home()
    roots = [home / ".jarvis" / "scripts", home / ".jarvis" / "daemon", home / ".mind" / "mind",
             home / ".analyst" / "analyst", home / ".forge" / "forge", home / ".herald", home / ".sentinel",
             home / ".budget" / "budget"]
    offenders = []
    for root in roots:
        for py in root.rglob("*.py"):
            if {"tests", "test", "node_modules", ".worktrees", ".venv"} & set(py.parts):
                continue
            text = py.read_text(encoding="utf-8", errors="replace")
            if '"--strict-mcp-config"' in text or "'--strict-mcp-config'" in text:  # code literal, not a comment
                offenders.append(str(py))
    assert not offenders, offenders


if __name__ == "__main__":
    test_command()
    test_job_session_strict_mcp()
    test_routing_and_parse()
    test_store_path()
    test_run_agent()
    test_auth_modes()
    test_proc_timeout_kills()
    test_service_and_cli()
    print("ok")
