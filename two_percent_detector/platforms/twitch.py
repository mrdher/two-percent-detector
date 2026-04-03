"""Twitch chat monitor (anonymous, read-only).

Connects to a Twitch channel via the public IRC WebSocket endpoint as an anonymous
viewer (`justinfan` nick) and dispatches parsed events via callbacks.
No OAuth token is required.

Two IRC command types are handled:
- `PRIVMSG`: Standard chat messages, dispatched as `ChatMessage` via the `on_message`
callback.
- `CLEARCHAT`: Bans and timeouts, dispatched as `ClearChatEvent` and stored for later
lookup via `TwitchChat.was_recently_banned`.

`PING` keepalives from the server are answered with `PONG` automatically.

IRC message tags (`twitch.tv/tags` capability) provide:
- `mod`, `badges` — moderator / broadcaster / VIP detection.
- `emotes` — Twitch-native emote positions for stripping.
- `user-id`, `display-name` — user identity.
- `ban-duration`, `target-user-id` — ban/timeout metadata.

Third-party emote names (7TV, FFZ, BTTV) are fetched by `emotes.EmoteCache` and stripped
from message text alongside Twitch-native emotes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, cast

from niquests import AsyncSession, ReadTimeout, Session
from urllib3_future.contrib.webextensions._async.ws import (
    AsyncWebSocketExtensionFromHTTP,
)

from two_percent_detector.core.chat_types import (
    RECONNECT_DELAY,
    TWITCH,
    ChatMessage,
    ClearChatEvent,
    JsonValue,
    check_recent_ban,
)
from two_percent_detector.ui.terminal import console
from two_percent_detector.utils.emotes import EmoteCache

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger
    from re import Match

    from niquests.models import Response
    from urllib3_future.contrib.webextensions._async.ws import (
        AsyncWebSocketExtensionFromHTTP,
    )

    from two_percent_detector.core.chat_types import JsonValue

logger: Logger = logging.getLogger(name=__name__)

# Constants
# Twitch anonymous IRC endpoint.
_IRC_WS_URL: Final[str] = "wss://irc-ws.chat.twitch.tv:443"

# Anonymous identity — Twitch allows read-only connections with this pattern.
_ANON_NICK: Final[str] = "justinfan12345"

# Read timeout per poll cycle in seconds.
# Keeps the loop responsive to cancellation without busy-spinning.
_POLL_TIMEOUT: Final[float] = 1.0

# General IRC message regex: [@tags] [:prefix] COMMAND [params].
_IRC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:@(?P<tags>\S+)\s+)?(?::(?P<prefix>\S+)\s+)?(?P<command>\S+)(?:\s+(?P<params>.+))?$",
)

# CTCP ACTION wrapper (\x01ACTION text\x01) used by /me.
_ACTION_PREFIX: Final[str] = "\x01ACTION "
_ACTION_SUFFIX: Final[str] = "\x01"


# ID lookup
# ivr.fi public Twitch user lookup (no auth required).
_IVR_URL: Final[str] = "https://api.ivr.fi/v2/twitch/user"

# Per-request timeout in seconds.
_REQUEST_TIMEOUT: Final[int] = 10


def fetch_twitch_user(login: str) -> tuple[str, str, str]:
    """Resolve a Twitch login to `(login, display_name, user_id)`.

    Uses the public ivr.fi API — no OAuth token required.

    Args:
        login: Twitch login name (case-insensitive).

    Returns:
        Tuple of `(login, display_name, user_id)`.

    Raises:
        LookupError: If the user is not found.
        TypeError: If the API response has an unexpected type.
    """
    with Session() as session:
        resp: Response = session.get(
            url=_IVR_URL,
            params={"login": login.lower().strip()},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        users: JsonValue = resp.json()
        if not isinstance(users, list) or not users:
            msg: str = f"Twitch user {login!r} not found"
            raise LookupError(msg)
        user: JsonValue = users[0]
        if not isinstance(user, dict):
            msg = f"Unexpected API response type for {login!r}"
            raise TypeError(msg)
        uid: JsonValue = user.get("id")
        ulogin: JsonValue = user.get("login")
        display: JsonValue = user.get("displayName")
        uid_str: str = str(uid) if isinstance(uid, str | int) else ""
        login_str: str = str(ulogin) if isinstance(ulogin, str) else login.lower()
        display_str: str = str(display) if isinstance(display, str) else login_str
        return login_str, display_str, uid_str


def lookup_twitch(login: str) -> tuple[str, str]:
    """Resolve a Twitch login name to `(login, user_id)`.

    Wraps `fetch_twitch_user` with console output and error handling.

    Args:
        login: Twitch login name (case-insensitive).

    Returns:
        Tuple of `(login, user_id)`.
    """
    try:
        login_str, display_str, uid_str = fetch_twitch_user(login=login)
    except LookupError:
        console.print(f"[red]Twitch user [bold]{login}[/bold] not found.[/red]")
        sys.exit(1)
    console.print(
        f"[bold cyan]{display_str}[/bold cyan] ({login_str})"
        f"  →  ID [bold yellow]{uid_str}[/bold yellow]",
    )
    return login_str, uid_str


# IRC parsing helpers
def _extract_trailing(params: str) -> str:
    """Extract the trailing parameter from an IRC params string.

    IRC trailing parameters follow a `" :"` delimiter (e.g. `"#channel :message text"`).

    Args:
        params: The raw params portion of an IRC line.

    Returns:
        The text after `" :"`, or an empty string if absent.
    """
    colon_idx: int = params.find(" :")
    return params[colon_idx + 2 :] if colon_idx != -1 else ""


def _parse_tags(raw: str) -> dict[str, str]:
    """Parse an IRCv3 tag string into a mapping.

    Args:
        raw: Semicolon-delimited `key=value` pairs (e.g.
        `"ban-duration=600;target-user-id=123"`).

    Returns:
        Mapping of tag names to their string values.
    """
    result: dict[str, str] = {}
    for pair in raw.split(sep=";"):
        if "=" in pair:
            k, v = pair.split(sep="=", maxsplit=1)
            result[k] = v
        else:
            result[pair] = ""
    return result


def _parse_emote_positions(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse the `emotes` IRC tag into character-position ranges.

    The tag format is `emote_id:start-end,start-end/emote_id:start-end,...`.
    Each range is a pair of inclusive character indices into the message text.

    Args:
        raw: Value of the `emotes` IRC tag (may be empty).

    Returns:
        Tuple of `(start, end)` pairs sorted by start position.
    """
    if not raw:
        return ()
    ranges: list[tuple[int, int]] = []
    for emote_block in raw.split(sep="/"):
        if ":" not in emote_block:
            continue
        _, positions = emote_block.split(sep=":", maxsplit=1)
        for pos in positions.split(sep=","):
            if "-" not in pos:
                continue
            s, e = pos.split(sep="-", maxsplit=1)
            ranges.append((int(s), int(e)))
    ranges.sort()
    return tuple[tuple[int, int], ...](ranges)


# TwitchChat (public client — implements PlatformClient protocol)
class TwitchChat:
    """Anonymous Twitch chat client (IRC + third-party emote cache).

    Handles the full Twitch lifecycle: IRC WebSocket connection, message/ban parsing,
    emote cache management, and reconnection.

    Example::

        twitch = TwitchChat(
            channel="channelname",
            channel_id="123456789",
            on_message=handle_chat,
            on_clearchat=handle_ban,
        )
        await twitch.run()  # blocks until cancelled

    Attributes:
        connected: `asyncio.Event` set once the IRC client has joined the channel and is
        ready to receive messages.
    """

    __slots__ = (
        "_channel",
        "_channel_id",
        "_emote_cache",
        "_ext",
        "_on_clearchat",
        "_on_message",
        "_recent_bans",
        "connected",
    )

    def __init__(
        self,
        *,
        channel: str,
        channel_id: str,
        on_message: Callable[[ChatMessage], None] | None = None,
        on_clearchat: Callable[[ClearChatEvent], None] | None = None,
    ) -> None:
        """Initialise the Twitch chat client.

        Args:
            channel: Twitch channel login name (lowercase, no `#`).
            channel_id: Numeric Twitch user ID for emote cache lookups.
            on_message: Sync callback invoked for each chat message.
            on_clearchat: Sync callback invoked for each ban/timeout.
        """
        self._channel: str = channel.lower()
        self._channel_id: str = channel_id
        self._on_message: Callable[[ChatMessage], None] | None = on_message
        self._on_clearchat: Callable[[ClearChatEvent], None] | None = on_clearchat
        self._emote_cache = EmoteCache()
        # user_id → monotonic timestamp of the most recent ban/timeout.
        self._recent_bans: dict[str, float] = {}
        # WebSocket extension reference (set during active connection).
        self._ext: AsyncWebSocketExtensionFromHTTP | None = None
        self.connected: asyncio.Event = asyncio.Event()

    # PlatformClient interface
    def was_recently_banned(self, *, user_id: str, within: float) -> bool:
        """Check whether a user was banned or timed-out recently.

        Args:
            user_id: Twitch user ID to look up.
            within: Maximum age in seconds.

        Returns:
            `True` if a `CLEARCHAT` targeting this user was received within the given
            time window.
        """
        return check_recent_ban(bans=self._recent_bans, user_id=user_id, within=within)

    async def run(self) -> None:
        """Start the emote cache and IRC listener, blocking indefinitely.

        The emote cache is initialised before joining the IRC channel.
        Automatically reconnects on connection loss with a 5-second back-off delay.

        Raises:
            asyncio.CancelledError: Propagated when the task is cancelled externally.
        """
        if self._channel_id:
            await self._emote_cache.start(channel_id=self._channel_id)
        while True:
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except OSError:
                logger.warning("IRC disconnected; reconnecting in 5 s", exc_info=True)
                self.connected.clear()
                await asyncio.sleep(RECONNECT_DELAY)

    def clean_text(self, msg: ChatMessage) -> str:
        """Strip Twitch-native and third-party emotes from a message.

        Args:
            msg: The chat message to clean.

        Returns:
            Message text with all emote tokens removed.
        """
        return self._emote_cache.strip_emotes(text=msg.text_without_emotes())

    # Extra properties (Twitch-specific)
    @property
    def total_emotes(self) -> int:
        """Total number of cached emote names across all providers."""
        return self._emote_cache.total_emotes

    # IRC connection
    async def _listen(self) -> None:
        """Execute a single connection lifecycle: handshake, join, listen.

        Raises:
            ConnectionError: If the WebSocket handshake fails.
        """
        self.connected.clear()
        async with AsyncSession(
            timeout=_POLL_TIMEOUT,
            happy_eyeballs=True,
        ) as session:
            resp: Response = await session.get(_IRC_WS_URL)

            ext: AsyncWebSocketExtensionFromHTTP | None = cast(
                "AsyncWebSocketExtensionFromHTTP | None",
                resp.extension,
            )

            if ext is None:
                msg: str = f"WebSocket handshake failed (status {resp.status_code})"
                raise ConnectionError(msg)

            self._ext = ext

            try:
                await ext.send_payload(buf="CAP REQ :twitch.tv/tags twitch.tv/commands")
                await ext.send_payload(buf=f"NICK {_ANON_NICK}")
                await ext.send_payload(buf=f"JOIN #{self._channel}")

                while not ext.closed:
                    try:
                        payload: str | bytes | None = await ext.next_payload()
                    except ReadTimeout:
                        continue

                    if payload is None:
                        break

                    if isinstance(payload, bytes):
                        payload = payload.decode(errors="replace")

                    for line in payload.splitlines():
                        await self._handle_line(line=line)
            finally:
                self._ext = None
                if not ext.closed:
                    await ext.close()

    # IRC line dispatch
    async def _handle_line(self, line: str) -> None:
        """Parse and dispatch a single raw IRC line.

        Args:
            line: One IRC message (without trailing CRLF).
        """
        if not line:
            return

        match: Match[str] | None = _IRC_RE.match(line)
        if match is None:
            return

        tags_raw: str = match.group("tags") or ""
        prefix: str = match.group("prefix") or ""
        command: str = match.group("command")
        params: str = match.group("params") or ""

        if command == "PING":
            if self._ext is not None:
                await self._ext.send_payload(buf=f"PONG {params}")
            return

        if command == "PRIVMSG":
            self._handle_privmsg(tags_raw=tags_raw, prefix=prefix, params=params)
        elif command == "CLEARCHAT":
            self._handle_clearchat(tags_raw=tags_raw, params=params)
        elif command == "366":
            # End of /NAMES list; channel join is complete.
            self.connected.set()
            logger.info("IRC joined #%s", self._channel)
        elif command == "RECONNECT" and self._ext is not None and not self._ext.closed:
            # Server requested reconnect; close socket to trigger the reconnect loop.
            await self._ext.close()

    # PRIVMSG
    def _handle_privmsg(self, *, tags_raw: str, prefix: str, params: str) -> None:
        """Parse a `PRIVMSG` and invoke the message callback.

        Args:
            tags_raw: Raw IRCv3 tags string.
            prefix: IRC prefix (`user!user@user.tmi.twitch.tv`).
            params: Everything after the command (`#channel :text`).
        """
        tags: dict[str, str] = _parse_tags(raw=tags_raw)

        # Login name from prefix: "user!user@user.tmi.twitch.tv"
        login: str = prefix.split(sep="!", maxsplit=1)[0] if "!" in prefix else ""

        # Message text from params: "#channel :text"
        text: str = _extract_trailing(params=params)

        # Strip /me ACTION wrapper.
        if text.startswith(_ACTION_PREFIX) and text.endswith(_ACTION_SUFFIX):
            text = text[len(_ACTION_PREFIX) : -len(_ACTION_SUFFIX)]

        # Parse badges for broadcaster/VIP detection.
        badges_raw: str = tags.get("badges", "")
        badge_names: set[str] = {
            b.split(sep="/")[0] for b in badges_raw.split(sep=",") if b
        }

        msg = ChatMessage(
            platform=TWITCH,
            user_id=tags.get("user-id", ""),
            username=login,
            display_name=tags.get("display-name", login),
            text=text,
            is_mod=tags.get("mod") == "1",
            is_broadcaster="broadcaster" in badge_names,
            is_subscriber=tags.get("subscriber") == "1",
            is_vip="vip" in badge_names,
            emote_ranges=_parse_emote_positions(raw=tags.get("emotes", "")),
        )

        if self._on_message is not None:
            self._on_message(msg)

    # CLEARCHAT
    def _handle_clearchat(self, *, tags_raw: str, params: str) -> None:
        """Parse a `CLEARCHAT` and invoke the clearchat callback.

        Args:
            tags_raw: Raw IRCv3 tags string.
            params: Everything after the command (`#channel [:username]`).
        """
        tags: dict[str, str] = _parse_tags(raw=tags_raw)

        username: str = _extract_trailing(params=params)

        user_id: str = tags.get("target-user-id", "")
        duration: int = int(tags.get("ban-duration", "0"))

        event = ClearChatEvent(
            platform=TWITCH,
            username=username,
            user_id=user_id,
            duration=duration,
            permanent=duration == 0 and bool(username),
            ts=time.monotonic(),
        )

        # Record for was_recently_banned() lookups.
        if user_id:
            self._recent_bans[user_id] = event.ts

        if self._on_clearchat is not None:
            self._on_clearchat(event)
