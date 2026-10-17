"""Gateway against a scratch home and a scratch Budget: the argument lists, routing by surface, the
parse, the usage-limit record, the ledger rows, run_agent's gate, retries and cap, the service's
guard, and the CLI. Nothing reaches a model: the process runner is replaced.
Run: ~/.gateway/.venv/bin/python ~/.gateway/tests/test_gateway.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="gateway-test-"))
os.environ["GATEWAY_HOME"] = str(TMP / "gateway")  # read at import: never the live store
os.environ["BUDGET_HOME"] = str(TMP / "budget")
ROOT = Path(__file__).resolve().parents[1]
(TMP / "gateway").mkdir()
shutil.copy(ROOT / "config.toml", TMP / "gateway" / "config.toml")  # the live surfaces, scratch data
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import gateway  # noqa: E402
from gateway import agent, claude, local, proc, store  # noqa: E402
from gateway._budget import ledger, limits  # noqa: E402

RAN: list = []


def fake_run(answer: str, code: int = 0):
    def run(cmd, timeout_s=None, cwd=None, input_text=None, env=None, sink=None):
        RAN.append({"cmd": cmd, "input": input_text, "env": env, "cwd": cwd})
        return code, answer
    return run


def rows(sql: str):
    import sqlite3
    with sqlite3.connect(TMP / "budget" / "state" / "budget.db") as conn:
        return conn.execute(sql).fetchall()


def test_command():
    iso = gateway.command("claude", "sonnet", 1.0, allowed_tools=claude.READONLY_TOOLS, isolated=True)
    # Exactly brain's isolated flags before the move: G15 measured the proposer on them.
    assert iso == ["claude", "-p", "--model", "sonnet", "--output-format", "json", "--strict-mcp-config", "--safe-mode",
                   "--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk", "--allowedTools", "Read Grep Glob",
                   "--max-budget-usd", "1.00"], iso
    bare = gateway.command("claude", "haiku", None, isolated=True, bare=True, system="one line")
    assert bare[bare.index("--setting-sources") + 1] == "" and "--no-session-persistence" in bare
    assert bare[bare.index("--tools") + 1] == "" and "--max-budget-usd" not in bare
    assert bare[bare.index("--system-prompt") + 1] == "one line"
    plain = gateway.command("claude", "sonnet", 1.0, prompt="hi")
    assert plain[:3] == ["claude", "-p", "hi"] and "Bash" in plain[plain.index("--disallowedTools") + 1]


def test_routing_and_parse():
    assert gateway.mode("escalation") == "claude:sonnet" and gateway.mode("nobody") == "off"
    assert gateway.model_for("job:checkups") == "sonnet" and gateway.model_for("inbound-critical") is None
    assert gateway.ask("inbound-critical", "p", {"type": "object"}) is None  # local surface, local model off
    assert gateway.ask("nobody", "p")["ok"] is False  # an unknown surface never reaches a model
    proc_run, claude.proc.run = claude.proc.run, fake_run(
        json.dumps({"type": "result", "subtype": "success", "result": '```json\n{"project": "jarvis"}\n```',
                    "total_cost_usd": 0.002, "stop_reason": "end_turn"}))
    try:
        out = gateway.ask("hud-task-classify", "which?", {}, "work", system="JSON only")
        assert out["ok"] and out["answer"] == {"project": "jarvis"} and out["model"] == "haiku", out
        sent = RAN[-1]
        assert sent["input"] == "which?" and sent["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude-work")
        assert "--setting-sources" in sent["cmd"] and sent["cmd"][sent["cmd"].index("--model") + 1] == "haiku"
        assert rows("SELECT surface, ok, cost_usd, model, scope FROM provider_calls ORDER BY id DESC LIMIT 1") == \
            [("hud-task-classify", 1, 0.002, "haiku", "work")]
        gateway.ask("mind-session-history", "s", {}, "personal", job="session-history")
        assert rows("SELECT job, ok, cost_usd FROM job_calls ORDER BY id DESC LIMIT 1") == [("session-history", 1, 0.002)]
        claude.proc.run = fake_run("API Error: 429 Too Many Requests", code=1)
        out = gateway.call("escalation", "x", budget_usd=0.5)
        assert out["ok"] is False and out["limit"] is not None and out["reason"].startswith("usage limit")
        assert limits.blocked("claude")[0]  # recorded in Budget's window; the caller alerts
        limits.clear("claude")
        claude.proc.run = fake_run("not json at all")
        assert "unparseable" in gateway.call("escalation", "x")["reason"]
    finally:
        claude.proc.run = proc_run


def test_local_shadow():
    local.record_shadow("s", "NORMAL", "NORMAL", True)
    local.record_shadow("s", "NORMAL", "CRITICAL", False)
    assert {a["surface"]: a for a in gateway.agreement()}["s"] == {"surface": "s", "n": 2, "agree": 0.5}
    assert store.DB_PATH == TMP / "gateway" / "state" / "gateway.db"


def test_run_agent():
    log = TMP / "job.log"
    done = json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.4, "num_turns": 3,
                       "result": "wrote it", "permission_denials": [{"tool_name": "Bash"}]})
    saved = agent.RUN
    try:
        agent.RUN = fake_run("noise\n" + done)
        out = gateway.run_agent("checkups", "do it", "Read,Write", ["Read(//x/**)"], 2.0, log_file=log)
        cmd = RAN[-1]["cmd"]
        assert out["ok"] and out["code"] == 0 and out["cost_usd"] == 0.4 and out["denials"] == ["Bash"], out
        assert cmd[cmd.index("--model") + 1] == "sonnet" and "--disallowedTools" in cmd and RAN[-1]["input"] == "do it"
        assert rows("SELECT job, ok, cost_usd, model FROM job_calls ORDER BY id DESC LIMIT 1") == \
            [("checkups", 1, 0.4, "sonnet")]
        assert "wrote it" in log.read_text()
        n = len(RAN)
        agent.RUN = fake_run("fetch failed", code=1)
        out = gateway.run_agent("checkups", "p", "Read", None, 1.0, max_attempts=2, backoff_s=0)
        assert out["code"] == 1 and len(RAN) == n + 2  # a transient error is retried, then given up
        agent.RUN = fake_run(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "total_cost_usd": 1.0}), 1)
        out = gateway.run_agent("checkups", "p", "Read", None, 1.0, max_attempts=3, backoff_s=0)
        assert out["subtype"] == "error_max_budget_usd" and len(RAN) == n + 3  # the cap is never retried
        assert gateway.run_agent("nobody", "p", "Read")["subtype"] == "not-started"  # no surface, no call
        real = ledger.job_budget
        ledger.job_budget = lambda cfg=None, scope="personal": {"spent": 41.0, "ceiling": 40.0, "over": True,
                                                                 "paused_for_optional": True}
        try:
            out = gateway.run_agent("checkups", "p", "Read")
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
    env = {**os.environ, "GATEWAY_HOME": str(TMP / "gateway"), "BUDGET_HOME": str(TMP / "budget")}
    s = subprocess.run([str(ROOT / "bin" / "gateway"), "surfaces"], capture_output=True, text=True, env=env)
    assert s.returncode == 0 and "job:implementer" in s.stdout
    s = subprocess.run([str(ROOT / "bin" / "gateway"), "ask", "inbound-critical", "p"], capture_output=True, text=True, env=env)
    assert s.returncode == 0 and json.loads(s.stdout)["answer"] is None  # off: nothing called


if __name__ == "__main__":
    test_command()
    test_routing_and_parse()
    test_local_shadow()
    test_run_agent()
    test_proc_timeout_kills()
    test_service_and_cli()
    print("ok")
