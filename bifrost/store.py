"""Bifrost's paths: state files, logs, config.toml. Bifrost keeps no database: what a model call costs is
written to Fafnir's ledger, which lives in Mimir.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.CONFIG`), as Fafnir's store does.
Stdlib only: imported by Jarvis's venv, Mimir's, Brokkr's and Bifrost's own.
"""
from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

log = logging.getLogger("bifrost")

ROOT = Path(os.environ.get("BIFROST_HOME", Path.home() / ".gateway"))
STATE = ROOT / "state"
CONFIG = ROOT / "config.toml"
# The last config.toml that parsed. A later malformed file returns this instead of raising.
_last_good: dict = {}


def set_root(root: Path) -> None:
    """Point every path at `root` (tests, Jarvis's selftest)."""
    global ROOT, STATE, CONFIG
    ROOT = Path(root)
    STATE = ROOT / "state"
    CONFIG = ROOT / "config.toml"


def load_config() -> dict:
    """~/.gateway/config.toml; missing means every surface is off.

    A malformed file (invalid TOML, or text that is not valid UTF-8) logs one warning and
    returns the last config that parsed, or {} when none has yet. A missing file stays {}.
    """
    global _last_good
    try:
        with open(CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except OSError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log.warning("malformed config %s (%s); keeping the last good config", CONFIG, exc)
        return _last_good
    _last_good = cfg
    return cfg
