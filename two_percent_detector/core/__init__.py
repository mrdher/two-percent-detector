"""Core business logic for the 2% Detector.

Provides three modules consumed throughout the rest of the package.

Modules:
- `chat_types`: Contains the `ChatMessage` and `ClearChatEvent` dataclasses, the
`PlatformClient` protocol, platform-identifier constants (`TWITCH`, `KICK`, `RUMBLE`),
and the shared JSON type aliases.
- `detector`: Contains `MessageDetector`, the per-user rolling-window fuzzy-similarity
engine that decides when to fire an alert.
Tuneable constants `WINDOW_SECONDS`, `REPEAT_COUNT`, and `SIMILARITY_THRESHOLD` are also
exported so callers (e.g. the UI layer) can display them without re-importing the
detector module directly.
- `stats`: Contains `ChatStats`, a session-scoped counter that tracks message rates,
unique users, bans, and per-minute sparkline data.
"""

from two_percent_detector.core.chat_types import (
    HTTP_NOT_FOUND,
    KICK,
    PLATFORM_COLOUR,
    PLATFORM_LABEL,
    RECONNECT_DELAY,
    RUMBLE,
    TWITCH,
    ChatMessage,
    ClearChatEvent,
    JsonObj,
    JsonValue,
    Platform,
    PlatformClient,
    check_recent_ban,
)
from two_percent_detector.core.detector import (
    MAX_HISTORY_PER_USER,
    REPEAT_COUNT,
    SIMILARITY_THRESHOLD,
    WINDOW_SECONDS,
    MessageDetector,
)
from two_percent_detector.core.stats import ChatStats

__all__ = [
    "HTTP_NOT_FOUND",
    "KICK",
    "MAX_HISTORY_PER_USER",
    "PLATFORM_COLOUR",
    "PLATFORM_LABEL",
    "RECONNECT_DELAY",
    "REPEAT_COUNT",
    "RUMBLE",
    "SIMILARITY_THRESHOLD",
    "TWITCH",
    "WINDOW_SECONDS",
    "ChatMessage",
    "ChatStats",
    "ClearChatEvent",
    "JsonObj",
    "JsonValue",
    "MessageDetector",
    "Platform",
    "PlatformClient",
    "check_recent_ban",
]
