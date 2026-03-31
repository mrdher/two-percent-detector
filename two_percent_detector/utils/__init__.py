"""Shared utilities: emote stripping and the Chrome User-Agent helper.

Two modules live here: `emotes` and `user_agent`.

Modules:
- `emotes`: Contains `EmoteCache`, which fetches and caches emote names from 7TV, FFZ,
and BTTV over HTTP/2 multiplexed streams.
It also provides four standalone stripping functions for cross-platform use:
`strip_emojis`, `strip_invisible`, `strip_kick_emotes`, and `strip_rumble_emotes`.
- `user_agent`: Contains `chrome_user_agent`, which fetches the latest stable Chrome
version from the Chromium release dashboard once per process and returns a realistic
`User-Agent` header string.
Used by the Rumble and Kick clients to avoid bot-detection heuristics.
"""

from two_percent_detector.utils.emotes import (
    REFRESH_INTERVAL_SECONDS,
    EmoteCache,
    strip_emojis,
    strip_invisible,
    strip_kick_emotes,
    strip_rumble_emotes,
)
from two_percent_detector.utils.user_agent import chrome_user_agent

__all__ = [
    "REFRESH_INTERVAL_SECONDS",
    "EmoteCache",
    "chrome_user_agent",
    "strip_emojis",
    "strip_invisible",
    "strip_kick_emotes",
    "strip_rumble_emotes",
]
