"""Shared types and protocols for cross-platform chat monitoring.

Defines the common :class:`ChatMessage` and :class:`ClearChatEvent` data classes, and
the :class:`PlatformClient` protocol implemented by all platform monitors (Twitch, Kick,
Rumble).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    import asyncio

# Platform identifiers.
type Platform = Literal["twitch", "kick", "rumble"]

# Recursive type for parsed JSON values.
type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)

# Convenience alias for parsed JSON objects (used by Kick & Rumble monitors).
type JsonObj = dict[str, JsonValue]

TWITCH: Final[Platform] = "twitch"
KICK: Final[Platform] = "kick"
RUMBLE: Final[Platform] = "rumble"

# Display labels for console output.
PLATFORM_LABEL: Final[dict[Platform, str]] = {
    TWITCH: "Twitch",
    KICK: "Kick",
    RUMBLE: "Rumble",
}

# Colours for Rich console output.
PLATFORM_COLOUR: Final[dict[Platform, str]] = {
    TWITCH: "purple",
    KICK: "green",
    RUMBLE: "dark_green",
}

# HTTP 404 status code (shared by emote and Rumble modules).
HTTP_NOT_FOUND: Final[int] = 404

# Delay before reconnecting after a disconnection (seconds).
RECONNECT_DELAY: Final[float] = 5.0


def check_recent_ban(bans: dict[str, float], user_id: str, within: float) -> bool:
    """Check whether *user_id* was banned within the last *within* seconds.

    Args:
        bans: Mapping of user ID to ``time.monotonic()`` ban timestamp.
        user_id: User ID to look up.
        within: Maximum age in seconds.

    Returns:
        bool: ``True`` if the user has a ban entry newer than *within* seconds.
    """
    ts: float | None = bans.get(user_id)
    return ts is not None and (time.monotonic() - ts) < within


@dataclass(slots=True, kw_only=True)
class ChatMessage:
    """Normalised chat message from any supported platform.

    Attributes:
        platform: Source platform identifier.
        user_id: Unique user identifier (platform-specific format).
        username: Login name (lowercase where applicable).
        display_name: Human-readable display name.
        text: Raw message text.
        is_mod: ``True`` if the sender has moderator privileges.
        is_broadcaster: ``True`` if the sender is the channel owner.
        is_subscriber: ``True`` if the sender is a subscriber.
        is_vip: ``True`` if the sender has VIP status.
        emote_ranges: Twitch-native emote character positions.
        Empty tuple for non-Twitch platforms.
    """

    platform: Platform
    user_id: str
    username: str
    display_name: str
    text: str
    is_mod: bool
    is_broadcaster: bool
    is_subscriber: bool = False
    is_vip: bool = False
    emote_ranges: tuple[tuple[int, int], ...] = ()

    def text_without_emotes(self) -> str:
        """Return the message text with Twitch-native emote substrings removed.

        Uses the character positions from the ``emotes`` IRC tag to mask emote words,
        then collapses whitespace.
        Returns the original text unchanged for non-Twitch platforms (empty
        ``emote_ranges``).

        Returns:
            str: Cleaned text with emotes removed and whitespace collapsed.
        """
        if not self.emote_ranges:
            return self.text
        skip: set[int] = set[int]()
        for start, end in self.emote_ranges:
            skip.update(range(start, end + 1))
        chars: list[str] = [c for i, c in enumerate[str](self.text) if i not in skip]
        return " ".join("".join(chars).split())


@dataclass(slots=True, kw_only=True)
class ClearChatEvent:
    """Moderation event (ban/timeout/message deletion).

    Attributes:
        platform: Source platform identifier.
        username: Target user login name (empty for full chat clears).
        user_id: Target user ID, or ``""`` if unavailable.
        duration: Timeout duration in seconds (``0`` for permanent bans).
        permanent: ``True`` for permanent bans, ``False`` for timeouts.
        ts: ``time.monotonic()`` value when the event was received.
    """

    platform: Platform
    username: str
    user_id: str
    duration: int
    permanent: bool
    ts: float


class PlatformClient(Protocol):
    """Interface contract for platform-specific chat clients.

    All platform monitors (Twitch, Kick, Rumble) implement this protocol.
    The :class:`~monitor.Monitor` orchestrator interacts with clients exclusively
    through this interface.

    Attributes:
        connected: Signalled once the client has joined the channel.
    """

    connected: asyncio.Event

    async def run(self) -> None:
        """Connect and listen indefinitely, auto-reconnecting on failure."""
        ...

    def was_recently_banned(self, *, user_id: str, within: float) -> bool:
        """Check whether *user_id* was moderated within *within* seconds."""
        ...

    def clean_text(self, msg: ChatMessage) -> str:
        """Strip platform-specific emote tokens from a message."""
        ...
