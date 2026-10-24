"""Bifrost's paths: state files, logs, config.toml. Bifrost keeps no database: what a model call costs is
written to Fafnir's ledger, which lives in Mimir.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.CONFIG`), as Fafnir's store does.
Stdlib only: imported by Jarvis's venv, Mimir's, Brokkr's and Bifrost's own.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("BIFROST_HOME", Path.home() / ".gateway"))
STATE = ROOT / "state"
CONFIG = ROOT / "config.toml"


def set_root(root: Path) -> None:
    """Point every path at `root` (tests, Jarvis's selftest)."""
    global ROOT, STATE, CONFIG
    ROOT = Path(root)
    STATE = ROOT / "state"
    CONFIG = ROOT / "config.toml"


def load_config() -> dict:
    """~/.gateway/config.toml; missing means every surface is off."""
    try:
        with open(CONFIG, "rb") as f:
            return tomllib.load(f)
    except OSError:
        return {}
