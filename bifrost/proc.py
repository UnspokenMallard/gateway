"""Run one `claude` process to completion: copied from Jarvis's `core._run_process_blocking`.

Output goes to a file, not a pipe: `claude -p` can leave MCP server processes alive after it
exits, and a pipe is only closed when every holder exits, so `communicate()` would wait for them
(the 2026-08-28 worker wedge). `proc.wait()` returns when the child exits. On timeout the whole
process group is killed and the return code is 124, as coreutils `timeout`. A prompt passed as
`input_text` reaches stdin through a file for the same reason.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import store


def _kill_tree(proc: subprocess.Popen) -> None:
    # The child leads its own process group (start_new_session), which holds its descendants.
    with contextlib.suppress(Exception):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


def run(cmd: list[str], timeout_s: float | None = 900, cwd: str | None = None, input_text: str | None = None,
        env: dict[str, str] | None = None, sink: Path | None = None) -> tuple[int, str]:
    """(returncode, combined output). `sink` also appends the output to that file (a job's log)."""
    proc_dir = store.STATE / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 3600
    for stale in [*proc_dir.glob("gateway-proc-*.out"), *proc_dir.glob("gateway-proc-*.in")]:
        with contextlib.suppress(OSError):
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
    fd, out_name = tempfile.mkstemp(prefix="gateway-proc-", suffix=".out", dir=proc_dir)
    os.close(fd)
    out_path = Path(out_name)
    in_path: Path | None = None
    try:
        if input_text is not None:
            fd, in_name = tempfile.mkstemp(prefix="gateway-proc-", suffix=".in", dir=proc_dir)
            with os.fdopen(fd, "wb") as staged:
                staged.write(input_text.encode("utf-8"))
            in_path = Path(in_name)
        with open(out_path, "wb") as out, (
            open(in_path, "rb") if in_path is not None else open(os.devnull, "rb")
        ) as source:
            proc = subprocess.Popen(cmd, stdin=source, stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                                    start_new_session=True)
            try:
                code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                code, timed_out = 124, True
            else:
                timed_out = False
        output = out_path.read_bytes().decode("utf-8", errors="replace")
        if timed_out:
            output += f"\ntimeout after {timeout_s}s: {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}"
        if sink is not None:
            sink.parent.mkdir(parents=True, exist_ok=True)
            with open(sink, "a", encoding="utf-8") as fh:
                fh.write(output if output.endswith("\n") or not output else output + "\n")
        return code or 0, output
    finally:
        for leftover in (out_path, in_path):
            if leftover is not None:
                with contextlib.suppress(OSError):
                    leftover.unlink()
