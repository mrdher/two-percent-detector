"""Platform-specific anonymous chat clients.

Each module in this subpackage handles one platform's transport layer — connection
lifecycle, message parsing, emote stripping, and ban detection — and exposes a class
that satisfies the `core.chat_types.PlatformClient` protocol plus a convenience lookup
helper.

All three clients require no OAuth token and connect anonymously in read-only mode.

Modules:
- `twitch`: Contains `TwitchChat` (anonymous IRC WebSocket) and `lookup_twitch` /
`fetch_twitch_user` for resolving a login name to a numeric user ID via the public
ivr.fi API.
- `kick`: Contains `KickChat` (Pusher WebSocket) and `lookup_kick` /
`fetch_channel_info` for resolving a channel slug to its chatroom ID via the Kick REST
API.
- `rumble`: Contains `RumbleChat` (Server-Sent Events) and `lookup_rumble` /
`fetch_rumble_stream_id` for resolving a channel name to a live-stream numeric ID by
scraping the Rumble channel page and calling the oembed + embed JS APIs.
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
