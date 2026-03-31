"""Central configuration — saved channel defaults and user whitelist.

All platform-specific default channel names and pre-resolved IDs live here so that every
entry point (``monitor``, ``twitch_monitor``) shares a single source of truth.

Edit the constants below to change which channels are used when a CLI flag is passed
without an explicit value (e.g. ``uv run monitor --twitch``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

# Twitch
DEFAULT_TWITCH_CHANNEL: Final[str] = "zackrawrr"
DEFAULT_TWITCH_ID: Final[str] = "552120296"

# Kick
DEFAULT_KICK_CHANNEL: Final[str] = "asmongold"
DEFAULT_KICK_ID: Final[int] = 13808

# Rumble
DEFAULT_RUMBLE_CHANNEL: Final[str] = "Asmongold"

# Known bots — well-known bot usernames (lowercase) to always ignore
KNOWN_BOTS: Final[frozenset[str]] = frozenset[str](
    json.loads(
        s=(Path(__file__).parent / "data" / "known_bots.json").read_text(
            encoding="utf-8",
        )
    ),
)
