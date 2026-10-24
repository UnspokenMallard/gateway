"""Bifrost: every model call on this Mac goes through here (DESIGN.md).

`ask()` a small judgment, `call()` one Claude call for a surface (brain's tiers), `run_agent()` a
tool-using job session, `mode()` a surface's mode.
Bifrost makes the call it is asked for and reports what happened: it never alerts, pings or files.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import store
from .agent import run_agent
from .claude import (AUTH_MODES, aask, acall, ask, call, command, configured, env_for, exe,
                     model_for, parse_json)

__all__ = ["AUTH_MODES", "aask", "acall", "ask", "call", "command", "configured", "env_for",
           "exe", "mode", "model_for", "parse_json", "run_agent", "surfaces"]


def mode(surface: str, cfg: Mapping[str, Any] | None = None) -> str:
    """`claude:<model>`; an unconfigured surface is `off`."""
    return configured(surface, cfg) or "off"


def surfaces(cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    cfg = cfg if cfg is not None else store.load_config()
    return {str(k): str(v) for k, v in (cfg.get("surfaces") or {}).items()}
