from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .. import core


DB_PATH = core.APP_DIR / "relay.db"
TOKEN_PATH = core.APP_DIR / "extension_token"
EXTENSION_INSTALL_DIR = core.APP_DIR / "extension"
DAEMON_PID_PATH = core.APP_DIR / "prelayd.pid"
DAEMON_LOG_PATH = core.APP_DIR / "prelayd.log"


RELAY_DEFAULTS: dict[str, Any] = {
    "port": 7340,
    "max_cycles": 50,
    "command_timeout_seconds": 1800,
    # Proactive rollover: once prompts + replies in the active
    # conversation exceed this many characters, Relay asks ChatGPT
    # for a handoff and continues in a fresh chat.
    "rollover_char_budget": 350_000,
    # Terminal output is truncated (head + tail) to this many
    # characters before it is sent back to ChatGPT.
    "evidence_max_chars": 12_000,
    # Fallback handoff size when a chat hits the hard length limit
    # before a proactive handoff could be requested.
    "fallback_handoff_cycles": 4,
    # How many times Relay re-asks for exactly one Bash block
    # before stopping for a human.
    "format_nudges": 1,
    # A request whose Send provably never produced a durable turn
    # (0 new turns after a full reload) may be re-issued as a NEW
    # request this many times in a row.
    "unpersisted_resubmits": 1,
    "models": {
        # "" = leave ChatGPT's current model alone.
        "default_label": "",
        # Model picker label used when the watchdog detects a loop.
        "strong_label": "Thinking",
    },
    # Consecutive PROGRESS cycles on the strong model before
    # Relay returns to the default model.
    "deescalate_after_progress": 2,
}


def relay_config(
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Defaults merged with ~/.project-relay/config.json["relay"]."""
    loaded = config if config is not None else core.load_config()
    merged = copy.deepcopy(RELAY_DEFAULTS)
    user = loaded.get("relay") or {}

    for key, value in user.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value

    return merged


def watchdog_config(
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    loaded = config if config is not None else core.load_config()
    return dict(loaded.get("watchdog") or core.default_config()["watchdog"])


def project_root(name: str) -> Path:
    return core.project_from_config(name).root
