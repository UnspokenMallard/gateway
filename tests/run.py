"""Run every Bifrost test file and print the pass count.
Run: ~/.gateway/.venv/bin/python ~/.gateway/tests/run.py   (exit 0 only when every test passed)
Each file is its own process: they point BIFROST_HOME at different scratch homes at import."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run_all(ns: dict) -> int:
    """Called from a test file's `__main__`: run every `test_*` in `ns`, print `N passed`."""
    n = 0
    for name, fn in list(ns.items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
    return n


if __name__ == "__main__":
    total, failed = 0, []
    for f in sorted(HERE.glob("test_*.py")):
        r = subprocess.run([sys.executable, str(f)], capture_output=True, text=True)
        passed = [ln for ln in r.stdout.splitlines() if ln.endswith(" passed")]
        n = int(passed[-1].split()[0]) if r.returncode == 0 and passed else 0
        total += n
        print(f"{f.name}: {n} passed" if r.returncode == 0 else f"{f.name}: FAILED\n{r.stdout[-800:]}{r.stderr[-1500:]}")
        if r.returncode != 0:
            failed.append(f.name)
    print(f"total: {total} passed" + (f", failed: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)
