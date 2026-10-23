#!/Users/anzes/.jarvis/.venv/bin/python
"""Host performance samples for the local triage model (TRIAGE-MODEL-DESIGN.md section 4 in ~/.jarvis).

Moved from ~/.jarvis/scripts 2026-10-17; Jarvis still triggers it every 5 min (job `local-llm-perf`).

Detection only. Every run appends one sample to state/local-llm-perf.jsonl and writes the latest
sample plus the budget verdict to state/local-llm-perf.json. Samples taken before Ollama is
installed are the baseline; later samples are compared against it. Needs no sudo.
Usage: local-llm-perf.py            take a sample, exit 1 on a budget breach
       local-llm-perf.py --budget   print the verdict over the trailing 7 days, take no sample
"""
from __future__ import annotations
import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.jarvis/.venv/bin/python")

import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import os  # noqa: E402
STATE = Path(os.environ.get("GATEWAY_HOME", Path.home() / ".gateway")) / "state"
SAMPLES = STATE / "local-llm-perf.jsonl"
ARTIFACT = STATE / "local-llm-perf.json"
DB = Path.home() / ".budget" / "state" / "budget.db"  # provider_calls, in Budget since 2026-10-17
MEM = Path.home() / ".local" / "bin" / "mem"
OLLAMA = "http://127.0.0.1:11434"
HUD_PROBE = "http://127.0.0.1:3022/login"  # static page, no auth: measures the HUD's event loop
RETAIN_S = 60 * 86400
WINDOW_S = 7 * 86400
WORK_HOURS = range(8, 19)  # weekdays only; outside them nobody feels a slowdown
MIN_BASELINE_DAYS = 2  # load follows the workday: two weekdays show whether the hours repeat

# The budget (design section 4). Changing a number here changes what "negligible" means.
MAX_RSS_MB = 4096
MAX_BUSY_FRACTION = 0.03  # was 0.01; a note-writing session hit 1.4% (incident 128)
MAX_SWAP_GROWTH_MB = 512
MAX_MEMLEVEL_DROP = 5
MAX_PROBE_RATIO = 1.2
MIN_LOADED_SAMPLES = 20  # shortcut: p95 over fewer samples is one slow outlier, not a trend
MIN_PROBE_FLOOR_S = 0.1  # a probe breach must also be a felt slowdown of at least this


def _run(argv: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _timed(fn) -> float | None:
    start = time.monotonic()
    try:
        fn()
    except Exception:  # noqa: BLE001 - a failed probe is a missing value, not a crash
        return None
    return round(time.monotonic() - start, 3)


def ollama_rss_mb() -> float | None:
    """Resident memory of all ollama processes; None when Ollama is not installed or not running."""
    rows = re.findall(r"^\s*(\d+)\s+(\S+)", _run(["ps", "-axo", "rss=,comm="]), re.M)
    rss = [int(kb) for kb, comm in rows if Path(comm).name.startswith("ollama")]
    return round(sum(rss) / 1024, 1) if rss else None


def ollama_loaded() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=2) as resp:
            return bool(json.load(resp).get("models"))
    except Exception:  # noqa: BLE001
        return False


def swap_used_mb(text: str) -> float | None:
    m = re.search(r"used = ([\d.]+)M", text)
    return float(m.group(1)) if m else None


def gpu_pct(text: str) -> int | None:
    m = re.search(r'"Device Utilization %"=(\d+)', text)
    return int(m.group(1)) if m else None


def cpu_pct(text: str) -> float | None:
    idle = [float(x) for x in re.findall(r"CPU usage: .*?([\d.]+)% idle", text)][1:]  # first is since boot
    return round(100 - sum(idle) / len(idle), 1) if idle else None


def sample() -> dict:
    now = datetime.now().astimezone()
    level = _run(["sysctl", "-n", "kern.memorystatus_level"]).strip()
    return {
        "ts": time.time(),
        "at": now.isoformat(timespec="seconds"),
        "work_hour": now.weekday() < 5 and now.hour in WORK_HOURS,
        "ollama_rss_mb": ollama_rss_mb(),
        "model_loaded": ollama_loaded(),
        "memlevel": int(level) if level.isdigit() else None,
        "swap_used_mb": swap_used_mb(_run(["sysctl", "-n", "vm.swapusage"])),
        "gpu_pct": gpu_pct(_run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])),
        "cpu_pct": cpu_pct(_run(["top", "-l", "2", "-s", "1", "-n", "0"])),
        "hud_probe_s": _timed(lambda: urllib.request.urlopen(HUD_PROBE, timeout=5).read()),
        "mem_probe_s": _timed(lambda: subprocess.run([str(MEM), "search", "backup", "--scope", "jarvis"],
                                                     capture_output=True, timeout=30, check=True)),
    }


def load_samples() -> list[dict]:
    try:
        lines = SAMPLES.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def save_samples(samples: list[dict]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SAMPLES.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(s) + "\n" for s in samples), encoding="utf-8")
    tmp.replace(SAMPLES)


def pct(values: list[float], p: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))]


def busy_fractions(since: float) -> list[float]:
    """Model busy time per working hour, from the local calls in provider_calls (seconds / 3600)."""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        rows = conn.execute("SELECT ts, duration_s FROM provider_calls WHERE surface LIKE 'local%' AND ts >= ?",
                            (since,)).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    per_hour: dict[int, float] = {}
    for ts, dur in rows:
        at = datetime.fromtimestamp(ts).astimezone()
        if at.weekday() >= 5 or at.hour not in WORK_HOURS:  # budget is per working hour (design section 4)
            continue
        per_hour[int(ts // 3600)] = per_hour.get(int(ts // 3600), 0.0) + (dur or 0.0)
    return [s / 3600 for s in per_hour.values()]


def verdict(samples: list[dict], now: float, busy: list[float]) -> dict:
    """Compare the trailing 7 days with the model against the baseline (samples without Ollama)."""
    work = [s for s in samples if s.get("work_hour")]
    base = [s for s in work if s.get("ollama_rss_mb") is None]
    recent = [s for s in work if s["ts"] >= now - WINDOW_S and s.get("ollama_rss_mb") is not None]
    base_days = len({s["at"][:10] for s in base})
    out: dict = {"baseline_samples": len(base), "baseline_days": base_days, "recent_samples": len(recent),
                 "breaches": []}
    if base_days < MIN_BASELINE_DAYS:
        out["state"] = "collecting-baseline"
        return out
    if not recent:
        out["state"] = "no-model-yet"
        return out
    b = out["breaches"]
    rss = max((s["ollama_rss_mb"] for s in recent), default=0)
    if rss > MAX_RSS_MB:
        b.append(f"Ollama RSS peaked at {rss:.0f} MB (budget {MAX_RSS_MB})")
    worst_busy = max(busy, default=0.0)
    if worst_busy > MAX_BUSY_FRACTION:
        b.append(f"model busy {worst_busy:.1%} of one hour (budget {MAX_BUSY_FRACTION:.0%})")
    bs, rs = pct([s.get("swap_used_mb") for s in base], 95), pct([s.get("swap_used_mb") for s in recent], 95)
    if bs is not None and rs is not None and rs > bs + MAX_SWAP_GROWTH_MB:
        b.append(f"swap p95 {rs:.0f} MB vs baseline {bs:.0f} MB (budget +{MAX_SWAP_GROWTH_MB})")
    bl, rl = pct([s.get("memlevel") for s in base], 5), pct([s.get("memlevel") for s in recent], 5)
    if bl is not None and rl is not None and rl < bl - MAX_MEMLEVEL_DROP:
        b.append(f"memory level p5 {rl} vs baseline {bl} (budget -{MAX_MEMLEVEL_DROP})")
    loaded = [s for s in recent if s.get("model_loaded")]
    for key, name in (("hud_probe_s", "HUD probe"), ("mem_probe_s", "mem search probe")):
        bp, lp = pct([s.get(key) for s in base], 95), pct([s.get(key) for s in loaded], 95)
        enough = len([s for s in loaded if s.get(key) is not None]) >= MIN_LOADED_SAMPLES
        if enough and bp and lp is not None and lp > bp * MAX_PROBE_RATIO and lp - bp > MIN_PROBE_FLOOR_S:
            b.append(f"{name} p95 {lp:.3f}s while loaded vs baseline {bp:.3f}s (budget x{MAX_PROBE_RATIO})")
    out.update(state="breach" if b else "within-budget", rss_peak_mb=rss, busy_peak=round(worst_busy, 4),
               swap_p95_mb=[bs, rs], memlevel_p5=[bl, rl])
    return out


def main() -> int:
    now = time.time()
    samples = [s for s in load_samples() if s.get("ts", 0) >= now - RETAIN_S]
    if "--budget" not in sys.argv:
        samples.append(sample())
        save_samples(samples)
    result = verdict(samples, now, busy_fractions(now - WINDOW_S))
    if "--budget" in sys.argv:
        print(json.dumps(result, indent=2))
        return 1 if result["state"] == "breach" else 0
    ARTIFACT.write_text(json.dumps({"checked_at": datetime.now().astimezone().isoformat(),
                                    "latest": samples[-1], "verdict": result}, indent=2), encoding="utf-8")
    if result["state"] == "breach":
        print("local model over its performance budget: " + "; ".join(result["breaches"]), file=sys.stderr)
        return 1
    print(f"local-llm-perf: {result['state']} ({result['baseline_days']} baseline days, "
          f"{result['recent_samples']} samples with the model)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
