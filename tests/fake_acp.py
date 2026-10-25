"""A fake `claude-agent-acp` for the session tests: NDJSON JSON-RPC over stdio, the shapes in
~/.jarvis/spike/acp/FINDINGS.md. The prompt text picks the behavior:
  ask   -> an elicitation form (a single choice with a custom field, then a multiple choice), then reports the answers
  perm  -> a permission request, then reports the chosen option id
  slow  -> a turn that ends only when `session/cancel` arrives
  other -> echoes the prompt
Nothing here calls a model."""
import json
import sys
import threading

out_lock = threading.Lock()
waiting: dict = {}  # id of a request we sent -> [Event, response]
cancelled = threading.Event()
next_id = [1000]


def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
        sys.stdout.flush()


def update(sid, **u):
    send({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def ask_client(method, params):
    next_id[0] += 1
    rid = next_id[0]
    waiting[rid] = [threading.Event(), None]
    send({"id": rid, "method": method, "params": params})
    waiting[rid][0].wait(30)
    return waiting.pop(rid)[1] or {}


def form():
    def one(title, text, labels):
        return {"type": "string", "title": title, "description": text,
                "oneOf": [{"const": c, "title": c, "description": f"the {c} one"} for c in labels]}
    return {"mode": "form", "message": "Questions", "requestedSchema": {"type": "object", "properties": {
        "question_0": one("Colour", "Which colour?", ["Red", "Blue"]),
        "question_0_custom": {"type": "string", "title": "Other"},
        "question_1": {"type": "array", "title": "Letters", "description": "Which letters?",
                       "items": {"anyOf": [{"const": c, "title": c} for c in "AB"]}}}}}


def prompt(rid, p):
    sid, text = p["sessionId"], p["prompt"][0]["text"]
    cancelled.clear()
    if text == "ask":
        got = ask_client("elicitation/create", form())
        reply = "answers " + json.dumps(got.get("content") if got.get("action") == "accept" else got.get("action"), sort_keys=True)
    elif text == "perm":
        got = ask_client("session/request_permission", {"sessionId": sid, "toolCall": {
            "toolCallId": "t1", "title": "Run ls", "kind": "execute", "rawInput": {"command": "ls"}}, "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]})
        reply = "permission " + json.dumps((got.get("outcome") or {}).get("optionId") or (got.get("outcome") or {}).get("outcome"))
    elif text == "slow":
        update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "working"})
        cancelled.wait(30)
        return send({"id": rid, "result": {"stopReason": "cancelled"}})
    else:
        reply = f"echo: {text}"
    update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": reply})
    send({"id": rid, "result": {"stopReason": "end_turn"}})


def main():
    n = 0
    for line in sys.stdin:
        m = json.loads(line)
        if "method" not in m:  # a response to something we asked
            slot = waiting.get(m["id"])
            if slot:
                slot[1] = m.get("result")
                slot[0].set()
            continue
        method, rid = m["method"], m.get("id")
        if method == "initialize":
            send({"id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
        elif method == "session/new":
            n += 1
            send({"id": rid, "result": {"sessionId": f"fake-{n}"}})
        elif method in ("session/resume", "session/set_config_option"):
            send({"id": rid, "result": {}})
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(rid, m["params"]), daemon=True).start()
        elif method == "session/cancel":
            cancelled.set()


main()
