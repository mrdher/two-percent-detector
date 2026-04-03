"""Core business logic for the 2% Detector.

Shared types (`ChatMessage`, `ClearChatEvent`, `PlatformClient`), the per-user
rolling-window spam detector, and session statistics tracking.
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
