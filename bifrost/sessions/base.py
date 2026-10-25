"""What every runner shares: the error type, the environment a session gets, the clock."""
from __future__ import annotations

import os
from pathlib import Path

from ..claude import CONFIG_DIRS

HOME = Path.home()
# launchd hands a bare PATH; a session needs the user's tools (the HUD's list: node for the ACP adapter comes from mise).
PATH = ":".join([str(HOME / ".local/bin"), str(HOME / ".local/share/mise/shims"), "/opt/homebrew/bin",
                 "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


class SessionError(Exception):
    """A session call that cannot be done. `status` is the HTTP code the service answers with."""
    status = 409


class NotFound(SessionError):
    status = 404


class BadRequest(SessionError):
    status = 400


def session_env(rec: dict) -> dict[str, str]:
    """The account, the task id and PATH a session runs with. An API key in the environment is dropped so the
    subscription login is what pays (the same rule as `auth="login"` for a call)."""
    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    env.update(CLAUDE_CONFIG_DIR=str(CONFIG_DIRS[rec["scope"]]), HUD_TASK_ID=rec["id"], PATH=PATH, TERM="xterm-256color")
    return env
