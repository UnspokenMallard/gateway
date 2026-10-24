"""Bifrost's store: state/gateway.db (WAL), state files, config.toml.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.DB_PATH`, `store.CONFIG`), as Fafnir's store does.
Stdlib only: imported by Jarvis's venv, Mimir's, Brokkr's and Bifrost's own.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("BIFROST_HOME", Path.home() / ".gateway"))
STATE = ROOT / "state"
DB_PATH = STATE / "gateway.db"
CONFIG = ROOT / "config.toml"


def set_root(root: Path) -> None:
    """Point every path at `root` (tests, Jarvis's selftest)."""
    global ROOT, STATE, DB_PATH, CONFIG
    ROOT = Path(root)
    STATE = ROOT / "state"
    DB_PATH = STATE / "gateway.db"
    CONFIG = ROOT / "config.toml"


@contextlib.contextmanager
def connect():
    """`with connect() as conn:` commits or rolls back, and always closes (one handle per call)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def load_config() -> dict:
    """~/.gateway/config.toml; missing means every surface is off."""
    try:
        with open(CONFIG, "rb") as f:
            return tomllib.load(f)
    except OSError:
        return {}
