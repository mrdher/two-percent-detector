r"""Kick chat WebSocket client (anonymous, read-only).

Connects to a Kick chatroom via the public Pusher WebSocket endpoint.
No OAuth token is required for read-only access.

The client:
1. Fetches the chatroom ID from the Kick REST API.
2. Opens a WebSocket to the Pusher cluster.
3. Subscribes to the chatroom channel.
4. Dispatches parsed chat messages via the `on_message` callback.

Pusher protocol details:
- Connection URL: `wss://ws-us2.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0`
- Subscribe: `{"event":"pusher:subscribe","data":{"channel":"chatrooms.{id}.v2"}}`
- Chat events: `App\Events\ChatMessageEvent`
- Ping/pong: `pusher:ping` / `pusher:pong`
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Final, cast

from niquests import AsyncSession, ReadTimeout, Session

from two_percent_detector.core.chat_types import (
    KICK,
    RECONNECT_DELAY,
    ChatMessage,
    check_recent_ban,
)
from two_percent_detector.ui.terminal import console
from two_percent_detector.utils.emotes import EmoteCache, strip_kick_emotes
from two_percent_detector.utils.user_agent import chrome_user_agent

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger

    from niquests.models import Response
    from urllib3_future.contrib.webextensions._async.ws import (
        AsyncWebSocketExtensionFromHTTP,
    )

    from two_percent_detector.core.chat_types import JsonObj, JsonValue

logger: Logger = logging.getLogger(name=__name__)

# Pusher WebSocket endpoint template.
# Public value used by Kick for all users, not secret
_PUSHER_KEY: Final[str] = "32cbd69e4b950bf97679"
_PUSHER_URL: Final[str] = (
    "wss://ws-us2.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
)

# Kick REST API for channel lookup.
_KICK_CHANNEL_API: Final[str] = "https://kick.com/api/v2/channels/{slug}"

# Request timeout for the channel info lookup (seconds).
_CHANNEL_LOOKUP_TIMEOUT: Final[int] = 10

# Read timeout per poll cycle in seconds.
_POLL_TIMEOUT: Final[float] = 1.0

# Pusher chat event names that carry messages.
_CHAT_EVENTS: Final[frozenset[str]] = frozenset[str]({
    r"App\Events\ChatMessageEvent",
    r"App\Events\MessageSentEvent",
})


def fetch_channel_info(slug: str) -> tuple[int, int]:
    """Look up Kick chatroom ID and user ID for a channel slug.

    Args:
        slug: Kick channel username / slug (lowercase).

    Returns:
        Tuple of `(chatroom_id, user_id)`.

    Raises:
        TypeError: If the channel is not found or the response is malformed.
    """
    url: str = _KICK_CHANNEL_API.format(slug=slug.lower().strip())
    ua: str = chrome_user_agent()
    with Session(happy_eyeballs=True) as session:
        # Prime the session with a Cloudflare cookie before hitting the API.
        session.get(
            url="https://kick.com",
            headers={"User-Agent": ua},
            timeout=_CHANNEL_LOOKUP_TIMEOUT,
        )
        resp: Response = session.get(
            url=url,
            headers={"Accept": "application/json", "User-Agent": ua},
            timeout=_CHANNEL_LOOKUP_TIMEOUT,
        )
        resp.raise_for_status()
        data: JsonValue = resp.json()
    if not isinstance(data, dict):
        msg: str = f"Unexpected Kick API response for {slug}"
        raise TypeError(msg)
    chatroom: JsonValue = data.get("chatroom")
    if not isinstance(chatroom, dict):
        msg = f"No chatroom data for Kick channel {slug}"
        raise TypeError(msg)
    chatroom_id = chatroom.get("id")
    if not isinstance(chatroom_id, int):
        msg = f"Missing chatroom ID for Kick channel {slug}"
        raise TypeError(msg)
    user_id: JsonValue = data.get("user_id")
    user_id_int: int = user_id if isinstance(user_id, int) else 0
    return chatroom_id, user_id_int


def lookup_kick(slug: str) -> tuple[int, int]:
    """Resolve a Kick channel slug to `(chatroom_id, user_id)`.

    Wraps `fetch_channel_info` with console output and `sys.exit(1)` on failure.

    Args:
        slug: Kick channel slug (username).

    Returns:
        Tuple of `(chatroom_id, user_id)`.
    """
    try:
        chatroom_id, user_id = fetch_channel_info(slug=slug)
    except OSError, TypeError:
        console.print(f"[red]Kick channel [bold]{slug}[/bold] not found.[/red]")
        sys.exit(1)
    console.print(
        f"[bold cyan]{slug}[/bold cyan]  \u2192  chatroom [bold yellow]{chatroom_id}"
        "[/bold yellow]",
    )
    return chatroom_id, user_id


def _parse_pusher_data(raw: JsonValue | None) -> JsonObj | None:
    """Parse the `data` field of a Pusher message.

    Pusher wraps per-event data as a JSON-encoded string inside the outer JSON frame.

    Args:
        raw: The `data` value from the outer Pusher message.

    Returns:
        Parsed dict, or `None` if parsing fails.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed: JsonValue = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_message(data: JsonObj) -> ChatMessage | None:
    """Convert a Kick chat event payload into a `ChatMessage`.

    Args:
        data: Parsed event data from a Pusher chat event.

    Returns:
        A `ChatMessage` or `None` if required fields are missing.
    """
    # Message text.
    content: JsonValue = data.get("content") or data.get("message") or data.get("text")
    if not isinstance(content, str) or not content.strip():
        return None

    # Sender identity.
    sender_raw: JsonValue = data.get("sender")
    if not isinstance(sender_raw, dict):
        return None

    username: JsonValue = (
        sender_raw.get("username") or sender_raw.get("slug") or sender_raw.get("name")
    )
    if not isinstance(username, str):
        return None

    user_id: JsonValue = sender_raw.get("id")
    user_id_str: str = str(user_id) if user_id is not None else ""

    # Badges / roles.
    identity_raw: JsonValue = sender_raw.get("identity")
    badges_raw: list[JsonValue] = []
    if isinstance(identity_raw, dict):
        b: JsonValue = identity_raw.get("badges")
        if isinstance(b, list):
            badges_raw = b

    badge_types: set[str] = set[str]()
    for badge in badges_raw:
        if isinstance(badge, dict):
            badge_type: JsonValue = badge.get("type")
            if isinstance(badge_type, str):
                badge_types.add(badge_type.lower())

    return ChatMessage(
        platform=KICK,
        user_id=user_id_str,
        username=username.lower(),
        display_name=username,
        text=content,
        is_mod="moderator" in badge_types,
        is_broadcaster="broadcaster" in badge_types or "channel_host" in badge_types,
        is_subscriber="subscriber" in badge_types or "sub_gifter" in badge_types,
        is_vip="vip" in badge_types,
    )


class KickChat:
    """Anonymous Kick chat client using Pusher WebSocket.

    Fetches the chatroom ID for a channel slug, connects to the Pusher WebSocket,
    subscribes, and dispatches messages via a callback.

    Example::

        kick = KickChat(
            channel_name="streamer",
            on_message=handle_chat,
        )
        await kick.run()  # blocks until cancelled

    Attributes:
        connected: `asyncio.Event` set once the client has subscribed to the chatroom
        and is receiving events.
    """

    __slots__ = (
        "_channel_name",
        "_chatroom_id",
        "_emote_cache",
        "_ext",
        "_on_message",
        "_recent_bans",
        "connected",
    )

    def __init__(
        self,
        *,
        channel_name: str,
        chatroom_id: int = 0,
        on_message: Callable[[ChatMessage], None] | None = None,
    ) -> None:
        """Initialise the Kick chat client.

        Args:
            channel_name: Kick channel slug (username).
            chatroom_id: Pre-resolved chatroom ID (skips the API lookup in `run` when
            non-zero).
            on_message: Sync callback invoked for each chat message.
        """
        self._channel_name: str = channel_name.lower().strip()
        self._on_message: Callable[[ChatMessage], None] | None = on_message
        self._chatroom_id: int = chatroom_id
        self._emote_cache = EmoteCache()
        self._recent_bans: dict[str, float] = {}
        self._ext: AsyncWebSocketExtensionFromHTTP | None = None
        self.connected = asyncio.Event()

    def was_recently_banned(self, *, user_id: str, within: float) -> bool:
        """Check whether a user was banned recently.

        Kick does not expose anonymous ban events via Pusher, so this always returns
        `False` unless future moderation event support is added.

        Args:
            user_id: Kick user ID to look up.
            within: Maximum age in seconds.

        Returns:
            `True` if a moderation event targeting this user was received within the
            given time window.
        """
        return check_recent_ban(bans=self._recent_bans, user_id=user_id, within=within)

    def clean_text(self, msg: ChatMessage) -> str:
        """Strip Kick emote tokens and 7TV emotes from a message.

        Removes native `[emote:ID:NAME]` tokens first, then strips any words matching
        cached 7TV emote names.

        Args:
            msg: The chat message to clean.

        Returns:
            Message text with emote tokens removed.
        """
        text: str = strip_kick_emotes(text=msg.text)
        return self._emote_cache.strip_emotes(text=text)

    async def run(self) -> None:
        """Resolve the chatroom ID, connect, and listen indefinitely.

        Automatically reconnects on connection loss.

        Raises:
            asyncio.CancelledError: Propagated when the task is cancelled externally.
        """
        if not self._chatroom_id:
            self._chatroom_id, kick_user_id = await asyncio.to_thread(
                fetch_channel_info,
                self._channel_name,
            )
            logger.info(
                "Kick chatroom ID for %s: %d (user %d)",
                self._channel_name,
                self._chatroom_id,
                kick_user_id,
            )
            await self._emote_cache.start(
                channel_id=str(kick_user_id),
                platform=KICK,
            )

        while True:
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except OSError:
                logger.warning(
                    "Kick WS disconnected; reconnecting in 5 s",
                    exc_info=True,
                )
                self.connected.clear()
                await asyncio.sleep(RECONNECT_DELAY)

    async def _listen(self) -> None:
        """Execute a single connection lifecycle: connect, subscribe, listen.

        Raises:
            ConnectionError: If the WebSocket handshake fails.
        """
        self.connected.clear()
        ws_url: str = _PUSHER_URL.format(key=_PUSHER_KEY)

        async with AsyncSession(timeout=_POLL_TIMEOUT, happy_eyeballs=True) as session:
            resp: Response = await session.get(ws_url)

            ext: AsyncWebSocketExtensionFromHTTP | None = cast(
                "AsyncWebSocketExtensionFromHTTP | None",
                resp.extension,
            )

            if ext is None:
                msg: str = (
                    f"Kick Pusher WebSocket handshake failed (status "
                    f"{resp.status_code})"
                )
                raise ConnectionError(msg)

            self._ext = ext

            try:
                # Subscribe to the chatroom channel.
                channel: str = f"chatrooms.{self._chatroom_id}.v2"
                subscribe_msg: str = json.dumps(
                    obj={
                        "event": "pusher:subscribe",
                        "data": {"channel": channel, "auth": ""},
                    }
                )
                await ext.send_payload(buf=subscribe_msg)
                logger.info("Kick: subscribed to %s", channel)

                while not ext.closed:
                    try:
                        payload: str | bytes | None = await ext.next_payload()
                    except ReadTimeout:
                        continue

                    if payload is None:
                        break

                    if isinstance(payload, bytes):
                        payload = payload.decode(errors="replace")

                    self._handle_frame(raw=payload)
            finally:
                self._ext = None
                if not ext.closed:
                    await ext.close()

    def _handle_frame(self, raw: str) -> None:
        """Parse and dispatch a single Pusher WebSocket frame.

        Args:
            raw: Raw JSON text received from the WebSocket.
        """
        try:
            frame_parsed: JsonValue = json.loads(raw)
        except json.JSONDecodeError:
            return

        if not isinstance(frame_parsed, dict):
            return

        event: JsonValue = frame_parsed.get("event")
        if not isinstance(event, str):
            return

        self._dispatch_event(event=event, frame=frame_parsed)

    def _dispatch_event(self, event: str, frame: JsonObj) -> None:
        """Route a Pusher event to the appropriate handler.

        Args:
            event: Pusher event name.
            frame: Parsed Pusher JSON frame.
        """
        # Pusher ping/pong keepalive.
        if event == "pusher:ping":
            if self._ext is not None:
                asyncio.get_running_loop().create_task(
                    coro=self._ext.send_payload(
                        buf=json.dumps(obj={"event": "pusher:pong", "data": "{}"})
                    ),
                    name="KickChat:pong",
                )
            return

        if event == "pusher:connection_established":
            logger.info("Kick Pusher connection established")
            return

        if event == "pusher_internal:subscription_succeeded":
            self.connected.set()
            logger.info("Kick: subscription succeeded")
            return

        # Chat message events.
        if event in _CHAT_EVENTS:
            data: JsonObj | None = _parse_pusher_data(raw=frame.get("data"))
            if data is None:
                return
            msg: ChatMessage | None = _extract_message(data=data)
            if msg is not None and self._on_message is not None:
                self._on_message(msg)
