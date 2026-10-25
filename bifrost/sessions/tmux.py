"""The tmux runner: a Claude Code TUI in a detached tmux session, on the HUD's socket.

The calls are the HUD's `lib/tasks.js` (startTmux, acceptTrust, sendKeys, paneState, killTmux) moved here with the
same behavior. Reading the screen stays the HUD's job (`lib/screen.js`): this runner returns the raw pane text and
takes keys. The socket is `hud` (config `tmux_socket`), or `HUD_TMUX_SOCKET` when set: a test instance must never
share the live server, it once typed into real sessions (2026-10-11).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from .. import store
from ..claude import exe
from .base import BadRequest, NotFound, SessionError, session_env

KEYS = {"esc": "Escape", "btab": "BTab", "enter": "Enter", "up": "Up", "down": "Down", "right": "Right",
        "left": "Left", "space": "Space"}
TRUST_YES = "Yes, I trust this folder"


def socket() -> str:
    return os.environ.get("HUD_TMUX_SOCKET") or str((store.load_config().get("sessions") or {}).get("tmux_socket") or "hud")


def _tmux(*args: str, input_text: str | None = None, timeout: float = 15) -> subprocess.CompletedProcess:
    cmd = ["tmux", "-L", socket(), *args]
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _must(*args: str, input_text: str | None = None) -> None:
    """Run a tmux command that has to work: a vanished session is a NotFound for the caller, not a silent no-op."""
    r = _tmux(*args, input_text=input_text)
    if r.returncode != 0:
        msg = r.stderr.strip()
        raise (NotFound if "can't find" in msg or "no server" in msg else SessionError)(f"tmux: {msg}")


def argv(rec: dict, spec: dict) -> list[str]:
    """The claude command line: what the HUD passes for a new, an extra and a resumed task."""
    cmd = [exe() or "claude", "--model", rec["model"]]
    sid = spec.get("session_id")
    if spec.get("resume"):
        cmd += ["--resume", sid] if sid else ["--continue"]
    else:
        if sid:
            cmd += ["--session-id", sid]
        if spec.get("title"):
            cmd += ["--name", spec["title"]]
        if spec.get("prompt"):
            cmd.append(spec["prompt"])
    return cmd


def count() -> int:
    return len([n for n in _tmux("list-sessions", "-F", "#{session_name}").stdout.split("\n") if n])


def status(rec: dict) -> str:
    """'alive' | 'dead' (claude exited, the pane is kept) | 'gone'."""
    if _tmux("has-session", "-t", rec["name"]).returncode != 0:
        return "gone"
    dead = _tmux("display-message", "-p", "-t", rec["name"], "#{pane_dead}").stdout.strip() == "1"
    return "dead" if dead else "alive"


def start(rec: dict, spec: dict) -> dict:
    _tmux("start-server")
    _tmux("set-option", "-g", "window-size", "latest")
    _tmux("set-option", "-g", "history-limit", "20000")
    env = session_env(rec)
    r = _tmux("new-session", "-d", "-s", rec["name"], "-x", "160", "-y", "45", "-c", rec["cwd"],
              "-e", f"CLAUDE_CONFIG_DIR={env['CLAUDE_CONFIG_DIR']}", "-e", f"HUD_TASK_ID={rec['id']}",
              "-e", f"PATH={env['PATH']}", "-e", "TERM=xterm-256color", "--", *argv(rec, spec))
    if r.returncode != 0:
        raise SessionError(f"tmux: {r.stderr.strip()}")
    _tmux("set-option", "-t", rec["name"], "remain-on-exit", "on")  # the last screen stays readable after claude exits
    if spec.get("trust", True):
        threading.Thread(target=accept_trust, args=(rec["name"], rec["cwd"]), daemon=True).start()
    return {}


def accept_trust(target: str, folder: str, wait_s: float = 30) -> str:
    """Claude Code asks once per folder whether to trust it ("Accessing workspace: <path>", "No, exit" preselected).
    A worktree is a fresh checkout of a repository Anze picked, so answer Yes, but only for that exact path, and press
    Enter only once the highlight is on the Yes line. Claude Code records the trust itself."""
    until = time.time() + wait_s
    while time.time() < until:
        r = _tmux("capture-pane", "-p", "-J", "-t", target, timeout=5)
        if r.returncode != 0:
            return "gone"
        lines = r.stdout.split("\n")
        asking = next((i for i, ln in enumerate(lines) if "Accessing workspace:" in ln), -1)
        if asking >= 0:
            if next((ln.strip() for ln in lines[asking + 1:] if ln.strip()), None) != folder:
                return "other-folder"
            yes = next((ln for ln in lines if TRUST_YES in ln), None)
            if yes is None:
                return "unknown-prompt"
            if "❯" in yes:
                _tmux("send-keys", "-t", target, "Enter")
                return "trusted"
            _tmux("send-keys", "-t", target, "Down")
        elif any(ln.lstrip().startswith("❯ ") or "? for shortcuts" in ln or "bypass permissions" in ln
                 or "mode on" in ln for ln in lines):
            return "no-prompt"  # the prompt box is up: the folder was already trusted
        time.sleep(0.4)
    return "timeout"


def send(rec: dict, text: str) -> None:
    msg = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in msg:
        # Several lines go in as one bracketed paste, so a newline does not submit early.
        buf = f"gw-{rec['id']}"
        _must("load-buffer", "-b", buf, "-", input_text=msg)
        _must("paste-buffer", "-p", "-d", "-b", buf, "-t", rec["name"])
        time.sleep(0.25)
    else:
        _must("send-keys", "-t", rec["name"], "-l", msg)
    # Claude Code reads keys that arrive with the text as part of a paste, where Enter is a newline.
    time.sleep(0.15)
    _must("send-keys", "-t", rec["name"], "Enter")


def press(rec: dict, key: str) -> None:
    if key not in KEYS:
        raise BadRequest(f"unknown key {key!r}")
    _must("send-keys", "-t", rec["name"], KEYS[key])


def cancel(rec: dict) -> None:
    press(rec, "esc")  # the TUI's interrupt


def screen(rec: dict, lines: int | None = None) -> dict:
    """The pane as text, wrapped lines joined; `lines` of scrollback above the screen when given."""
    args = ["capture-pane", "-p", "-J", "-t", rec["name"]]
    if lines:
        args += ["-S", f"-{int(lines)}"]
    r = _tmux(*args, timeout=5)
    if r.returncode != 0:
        raise SessionError(f"no screen: {r.stderr.strip() or 'the tmux session is gone'}")
    return {"text": r.stdout}


def state(rec: dict) -> dict:
    return {"runner": "tmux", "alive": status(rec)}


def stop(rec: dict) -> None:
    if status(rec) != "gone":
        _tmux("kill-session", "-t", rec["name"])
