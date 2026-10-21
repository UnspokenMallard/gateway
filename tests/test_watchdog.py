"""Local-model watchdog trip logic against a scratch home: injected probe timings, no Ollama, no load.
Run: ~/.gateway/.venv/bin/python ~/.gateway/tests/test_watchdog.py"""
import importlib.util
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="watchdog-test-"))
os.environ["GATEWAY_HOME"] = str(TMP)  # read at import: never the live state or config
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wd", ROOT / "gateway" / "jobs" / "local-llm-watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

CONFIG = TMP / "config.toml"
CFG = {"enabled": True, "model": "qwen3.5:4b", "host": "http://x", "timeout_s": 10}
STOPPED: list = []


def fresh():
    shutil.copy(ROOT / "config.toml", CONFIG)
    wd.STATE.unlink(missing_ok=True)
    STOPPED.clear()


def step(p95_s, loaded=True):
    return wd.run(CFG, CONFIG, probe=lambda c: p95_s, loaded=lambda c: loaded, stop=STOPPED.append,
                  press=lambda: {"swap_used_mb": 900.0, "memlevel": 40}, now="2026-10-21T10:00:00+02:00")


def baseline(value=0.2):
    for _ in range(wd.BASELINE_CHECKS):
        assert step(value)[0] == 0
    assert wd.load_state()["baseline_s"] == value


def enabled():
    return tomllib.loads(CONFIG.read_text())["local"]["enabled"]


def test_normal_sequence_does_not_trip():
    fresh(); baseline()
    for v in (0.2, 0.3, 0.39, 0.2, 0.25):
        assert step(v)[0] == 0
    assert enabled() and not STOPPED and wd.load_state()["tripped"] is None


def test_three_degraded_in_a_row_trips():
    fresh(); baseline()
    assert step(0.5)[0] == 0 and step(0.5)[0] == 0
    code, msg = step(0.5)
    assert code == 1 and "baseline 0.2s" in msg
    assert not enabled() and STOPPED == ["qwen3.5:4b"] and wd.load_state()["tripped"]
    assert tomllib.loads(CONFIG.read_text())["surfaces"]["inbound-critical"] == "shadow"  # only [local] changed
    assert step(0.2)[0] == 0 and STOPPED == ["qwen3.5:4b"]  # stays tripped, no second stop or alert


def test_streak_resets_on_a_good_check():
    fresh(); baseline()
    for v in (0.5, 0.5, 0.2, 0.5, 0.5):
        assert step(v)[0] == 0
    assert enabled()


def test_probe_failure_counts_degraded():
    fresh(); baseline()
    def boom(c): raise TimeoutError("slow")
    for _ in range(3):
        code, _ = wd.run(CFG, CONFIG, probe=boom, loaded=lambda c: True, stop=STOPPED.append,
                         press=lambda: {"swap_used_mb": None, "memlevel": None}, now="t")
    assert code == 1 and STOPPED


def test_unloaded_model_skips():
    fresh(); baseline()
    for _ in range(5):
        assert step(9.0, loaded=False)[0] == 0
    assert enabled() and wd.load_state()["streak"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
    shutil.rmtree(TMP)
