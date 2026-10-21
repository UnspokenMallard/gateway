#!/Users/anzes/.jarvis/.venv/bin/python
"""Stops the local triage model when it slows down (TRIAGE-MODEL-DESIGN.md, Anze 2026-10-21).

Jarvis triggers it every 5 min (job `local-llm-watchdog`). Each run sends PROBES one-token requests to the
model and takes their p95. The baseline is the median of the first BASELINE_CHECKS p95 values, measured here
on the first runs. A check is degraded when its p95 is over TRIP_RATIO x baseline. TRIP_AFTER degraded
checks in a row trip the watchdog: `[local] enabled = false` in config.toml (so shadow calls stop at once,
for every surface), `ollama stop <model>`, exit 1 with the reason (Jarvis raises the alert from the failed job).
A tripped watchdog stays tripped and exits 0 until `--reset` (re-enable by hand: `enabled = true`).

A model that is not loaded is not probed (a probe would measure the load, and would load it): the check is
skipped and the streak is kept. Swap and memory level are recorded with each check and named in the alert.
State: state/local-llm-watchdog.json. Usage: local-llm-watchdog.py [--reset]
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.jarvis/.venv/bin/python")

import json
import os
import re
import statistics
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("GATEWAY_HOME", Path.home() / ".gateway"))
CONFIG = HOME / "config.toml"
STATE = HOME / "state" / "local-llm-watchdog.json"
OLLAMA_BIN = "/opt/homebrew/bin/ollama"

PROBES = 5
BASELINE_CHECKS = 6  # 30 min of runs
TRIP_RATIO = 2.0
TRIP_AFTER = 3
KEEP = 50  # checks kept in the state file


def p95(values: list[float]) -> float:
    vals = sorted(values)
    return vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]


def probe_once(cfg: dict) -> float:
    body = {"model": cfg["model"], "prompt": "ok", "stream": False, "think": False,
            "options": {"num_predict": 1, "temperature": 0}, "keep_alive": cfg.get("keep_alive", "10m")}
    req = urllib.request.Request(f"{cfg['host'].rstrip('/')}/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout_s", 10))) as resp:
        resp.read()
    return time.monotonic() - start


def model_loaded(cfg: dict) -> bool:
    try:
        with urllib.request.urlopen(f"{cfg['host'].rstrip('/')}/api/ps", timeout=2) as resp:
            return any(m.get("name") == cfg["model"] or m.get("model") == cfg["model"]
                       for m in json.load(resp).get("models") or [])
    except Exception:  # noqa: BLE001 - unreachable Ollama: nothing to probe
        return False


def _sys_out(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def pressure() -> dict:
    swap = re.search(r"used = ([\d.]+)M", _sys_out(["sysctl", "-n", "vm.swapusage"]))
    level = _sys_out(["sysctl", "-n", "kern.memorystatus_level"]).strip()
    return {"swap_used_mb": float(swap.group(1)) if swap else None,
            "memlevel": int(level) if level.isdigit() else None}


def stop_model(model: str) -> None:
    subprocess.run([OLLAMA_BIN, "stop", model], capture_output=True, timeout=30)


def disable_shadow(config: Path) -> None:
    """`enabled = true` -> `false` inside [local] only, keeping the file's comments."""
    text = config.read_text(encoding="utf-8")
    head, sep, rest = text.partition("[local]")
    m = re.search(r"(?m)^\[", rest)
    section, tail = (rest[:m.start()], rest[m.start():]) if m else (rest, "")
    new, n = re.subn(r"(?m)^enabled\s*=\s*true\b", "enabled = false", section, count=1)
    if not sep or not n:
        if re.search(r"(?m)^enabled\s*=\s*false\b", section):
            return
        raise RuntimeError("no `enabled = true` under [local] in config.toml")
    tmp = config.with_suffix(".tmp")
    tmp.write_text(head + sep + new + tail, encoding="utf-8")
    tmp.replace(config)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"baseline_s": None, "baseline_samples": [], "streak": 0, "tripped": None, "checks": []}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state["checks"] = state["checks"][-KEEP:]
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def check(state: dict, probes: list[float], press: dict, now: str) -> str | None:
    """Record one check in `state`; return the trip reason when this check trips the watchdog."""
    p = round(p95(probes), 3)
    row = {"at": now, "p95_s": p, **press}
    if state["baseline_s"] is None:
        state["baseline_samples"].append(p)
        if len(state["baseline_samples"]) >= BASELINE_CHECKS:
            state["baseline_s"] = round(statistics.median(state["baseline_samples"]), 3)
        row["status"] = "baseline"
        state["checks"].append(row)
        return None
    degraded = p > TRIP_RATIO * state["baseline_s"]
    state["streak"] = state["streak"] + 1 if degraded else 0
    row["status"] = "degraded" if degraded else "ok"
    state["checks"].append(row)
    if state["streak"] < TRIP_AFTER:
        return None
    last = [c["p95_s"] for c in state["checks"][-TRIP_AFTER:]]
    return (f"probe p95 over {TRIP_RATIO:g}x baseline {state['baseline_s']}s for {TRIP_AFTER} checks in a row "
            f"({', '.join(f'{v}s' for v in last)}); swap {press['swap_used_mb']} MB, memory level {press['memlevel']}")


def run(cfg: dict, config: Path = CONFIG, probe=probe_once, loaded=model_loaded, stop=stop_model,
        press=pressure, now: str | None = None) -> tuple[int, str]:
    """One check. The probe, the loaded test, `ollama stop` and the pressure read are injectable for tests."""
    state = load_state()
    if state["tripped"]:
        return 0, f"local-llm-watchdog: tripped {state['tripped']['at']}, waiting for --reset"
    if not cfg.get("enabled") or not cfg.get("model"):
        return 0, "local-llm-watchdog: local model disabled, nothing to watch"
    if not loaded(cfg):
        return 0, "local-llm-watchdog: model not loaded, check skipped"
    now = now or datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        probes = [probe(cfg) for _ in range(PROBES)]
    except Exception as exc:  # noqa: BLE001 - a probe that fails or times out is as degraded as a slow one
        probes = [float(cfg.get("timeout_s", 10))] * PROBES
        print(f"local-llm-watchdog: probe failed ({exc}); counted as timeout_s", file=sys.stderr)
    reason = check(state, probes, press(), now)
    if reason:
        state["tripped"] = {"at": now, "reason": reason}
        save_state(state)
        disable_shadow(config)
        stop(cfg["model"])
        return 1, f"local model {cfg['model']} stopped by the watchdog and shadow disabled: {reason}"
    save_state(state)
    last = state["checks"][-1]
    return 0, f"local-llm-watchdog: {last['status']} p95 {last['p95_s']}s (baseline {state['baseline_s']}s)"


def main() -> int:
    if "--reset" in sys.argv:
        STATE.unlink(missing_ok=True)
        print("local-llm-watchdog: state cleared; set `enabled = true` under [local] to resume shadow")
        return 0
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f).get("local") or {}
    cfg.setdefault("host", "http://127.0.0.1:11434")
    code, msg = run(cfg)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
