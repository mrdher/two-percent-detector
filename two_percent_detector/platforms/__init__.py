"""Platform-specific anonymous chat clients.

Each module handles one platform's transport layer — connection lifecycle, message
parsing, emote stripping, and ban detection — and exposes a class that satisfies the
``PlatformClient`` protocol.
All three clients connect anonymously in read-only mode; no OAuth token is required.
"""

from two_percent_detector.platforms.kick import (
    KickChat,
    fetch_channel_info,
    lookup_kick,
)
from two_percent_detector.platforms.rumble import (
    RumbleChat,
    fetch_rumble_stream_id,
    lookup_rumble,
)
from two_percent_detector.platforms.twitch import (
    TwitchChat,
    fetch_twitch_user,
    lookup_twitch,
)

__all__ = [
    "KickChat",
    "RumbleChat",
    "TwitchChat",
    "fetch_channel_info",
    "fetch_rumble_stream_id",
    "fetch_twitch_user",
    "lookup_kick",
    "lookup_rumble",
    "lookup_twitch",
]
