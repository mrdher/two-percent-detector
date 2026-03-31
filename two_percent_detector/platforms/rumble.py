"""Rumble chat SSE client (anonymous, read-only).

Connects to a Rumble livestream chat via the internal Server-Sent Events (SSE) endpoint.
No authentication is required for read-only access.

The client:

1. Opens an SSE connection to ``https://web7.rumble.com/chat/api/chat/{stream_id}/stream``.
2. Parses ``init`` and ``messages`` event types.
3. Dispatches parsed chat messages via the ``on_message`` callback.

The SSE reader runs in a background thread (via ``asyncio.to_thread``) because the
underlying HTTP transport is synchronous.
Events are dispatched back to the event loop using ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from typing import TYPE_CHECKING, Final

from niquests import Session

from two_percent_detector.core.chat_types import (
    HTTP_NOT_FOUND,
    RECONNECT_DELAY,
    RUMBLE,
    ChatMessage,
    check_recent_ban,
)
from two_percent_detector.ui.terminal import console
from two_percent_detector.utils.emotes import strip_rumble_emotes
from two_percent_detector.utils.user_agent import chrome_user_agent

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger

    from niquests.models import Response

    from two_percent_detector.core.chat_types import JsonObj, JsonValue

logger: Logger = logging.getLogger(name=__name__)

# Maximum title length for the console lookup display.
_TITLE_MAX_LENGTH: Final[int] = 60

# Rumble internal chat SSE endpoint.
_SSE_URL: Final[str] = "https://web7.rumble.com/chat/api/chat/{stream_id}/stream"

# Rumble channel page URL (the bare ``/{channel}`` path avoids Cloudflare).
_CHANNEL_URL: Final[str] = "https://rumble.com/{channel}"

# Rumble embed JS endpoint (public, no auth required).
_EMBED_URL: Final[str] = (
    "https://rumble.com/embedJS/u3/?request=video&ver=2&v={embed_id}"
)

# Rumble oembed endpoint for resolving a video page to an embed ID.
_OEMBED_URL: Final[str] = "https://rumble.com/api/Media/oembed.json?url={video_url}"

# Regex to find the first live video link in the channel page HTML.
# Matches a ``<div … thumbnail__thumb--live">`` block followed by the first
# ``<a … href="/v…-.html…">`` within the same thumbnail container.
_LIVE_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r'thumbnail__thumb--live".*?href="(/v[a-z0-9]+-[^"]+\.html)',
    re.DOTALL,
)

# Regex to extract the embed video ID from an oembed ``html`` field.
_EMBED_ID_RE: Final[re.Pattern[str]] = re.compile(r"/embed/(v[a-z0-9]+)")

# Request timeout for the channel lookup (seconds).
_CHANNEL_LOOKUP_TIMEOUT: Final[int] = 15

# SSE event data prefix.
_DATA_PREFIX: Final[str] = "data:"


def _find_live_video_path(
    session: Session,
    headers: dict[str, str],
    channel: str,
) -> str:
    """Fetch the channel page and return the live video path.

    Returns:
        str: The video page path (e.g. ``/video-abc123.html``).

    Raises:
        LookupError: If the channel is not found or not live.
    """
    page_resp: Response = session.get(
        url=_CHANNEL_URL.format(channel=channel),
        headers=headers,
        timeout=_CHANNEL_LOOKUP_TIMEOUT,
    )
    if page_resp.status_code == HTTP_NOT_FOUND:
        msg: str = f"Rumble channel {channel!r} not found"
        raise LookupError(msg)
    page_resp.raise_for_status()

    html: str | None = page_resp.text
    if html is None:
        msg = f"Empty response from Rumble for channel {channel!r}"
        raise LookupError(msg)

    match: re.Match[str] | None = _LIVE_LINK_RE.search(html)
    if match is None:
        msg = f"Rumble channel {channel!r} is not currently live"
        raise LookupError(msg)
    return match.group(1).split(sep="?")[0]


def _resolve_embed_id(
    session: Session,
    headers: dict[str, str],
    video_path: str,
    channel: str,
) -> str:
    """Use the oembed API to resolve a video page path to an embed ID.

    Returns:
        str: The embed video ID string.

    Raises:
        TypeError: If the oembed response is malformed.
    """
    resp: Response = session.get(
        url=_OEMBED_URL.format(video_url=f"https://rumble.com{video_path}"),
        headers=headers,
        timeout=_CHANNEL_LOOKUP_TIMEOUT,
    )
    resp.raise_for_status()
    data: JsonValue = resp.json()
    if not isinstance(data, dict):
        msg: str = f"Unexpected oembed response for {channel!r}"
        raise TypeError(msg)
    html_field: JsonValue = data.get("html")
    if not isinstance(html_field, str):
        msg = f"Missing embed HTML in oembed response for {channel!r}"
        raise TypeError(msg)
    match: re.Match[str] | None = _EMBED_ID_RE.search(html_field)
    if match is None:
        msg = f"Could not extract embed ID from oembed for {channel!r}"
        raise TypeError(msg)
    return match.group(1)


def fetch_rumble_stream_id(channel: str) -> tuple[str, str]:
    """Look up the live-stream numeric ID for a Rumble channel.

    The lookup is a three-step process:

    1. Fetch ``rumble.com/{channel}`` (the bare path bypasses Cloudflare).
    2. Scrape the HTML for the first ``thumbnail__thumb--live`` element and extract the
    video page link.
    3. Call the Rumble oembed API to resolve the embed video ID, then call the embed JS
    API with that ID to get the numeric stream ID and title.

    Args:
        channel: Rumble channel name / slug (e.g. ``"Asmongold"``).

    Returns:
        tuple[str, str]: Tuple of ``(stream_id, title)``.

    Raises:
        TypeError: If the API response is malformed.
    """
    stripped: str = channel.strip()
    ua: str = chrome_user_agent()
    headers: dict[str, str] = {"User-Agent": ua}

    with Session(happy_eyeballs=True) as session:
        video_path: str = _find_live_video_path(
            session=session,
            headers=headers,
            channel=stripped,
        )
        embed_id: str = _resolve_embed_id(
            session=session,
            headers=headers,
            video_path=video_path,
            channel=stripped,
        )

        resp: Response = session.get(
            url=_EMBED_URL.format(embed_id=embed_id),
            headers=headers,
            timeout=_CHANNEL_LOOKUP_TIMEOUT,
        )
        resp.raise_for_status()
        data: JsonValue = resp.json()
    if not isinstance(data, dict):
        msg: str = f"Unexpected embed API response for {stripped!r}"
        raise TypeError(msg)

    vid: JsonValue = data.get("vid")
    if not isinstance(vid, int):
        msg = f"Missing stream ID in Rumble API response for {stripped!r}"
        raise TypeError(msg)

    title: JsonValue = data.get("title")
    title_str: str = str(title) if isinstance(title, str) else ""
    return str(vid), title_str


def lookup_rumble(channel: str) -> tuple[str, str]:
    """Resolve a Rumble channel name to ``(stream_id, title)``.

    Wraps :func:`fetch_rumble_stream_id` with console output and
    ``sys.exit(1)`` on failure.

    Args:
        channel: Rumble channel name (case-insensitive).

    Returns:
        tuple[str, str]: Tuple of ``(stream_id, title)``.
    """
    try:
        stream_id, title = fetch_rumble_stream_id(channel=channel)
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    short_title: str = (
        title[:_TITLE_MAX_LENGTH] + "\u2026"
        if len(title) > _TITLE_MAX_LENGTH
        else title
    )
    console.print(
        f"[bold yellow]{channel}[/bold yellow]"
        f"  \u2192  stream [bold yellow]{stream_id}[/bold yellow]"
        f"  [dim]{short_title}[/dim]",
    )
    return stream_id, title


def _cache_users(raw: JsonValue | None, cache: dict[str, JsonObj]) -> None:
    """Update the user cache from a ``users`` array in an SSE event.

    Args:
        raw: Value of ``data["users"]`` from the SSE JSON.
        cache: Mutable user cache keyed by user-ID string.
    """
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        uid: JsonValue = entry.get("id")
        if uid is not None:
            cache[str(uid)] = entry


def _dispatch_messages(
    raw: JsonValue | None,
    *,
    parse: Callable[[JsonObj], ChatMessage | None],
    on_message: Callable[[ChatMessage], None],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Parse and dispatch messages from an SSE event payload.

    Args:
        raw: Value of ``data["messages"]`` from the SSE JSON.
        parse: Converts a message JSON object to a ``ChatMessage``.
        on_message: Callback invoked on the event loop per message.
        loop: Target event loop for ``call_soon_threadsafe``.
    """
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        msg: ChatMessage | None = parse(entry)
        if msg is not None:
            loop.call_soon_threadsafe(on_message, msg)


def _parse_sse_data(block: str) -> str:
    """Extract concatenated ``data:`` payload from an SSE block.

    Args:
        block: Raw SSE event text (one or more ``data:`` lines).

    Returns:
        str: Combined data string (empty if no ``data:`` lines found).
    """
    parts: list[str] = []
    for line in block.splitlines():
        stripped: str = line.strip()
        if stripped.startswith(_DATA_PREFIX):
            parts.append(stripped[len(_DATA_PREFIX) :].strip())
    return "\n".join(parts)


class RumbleChat:
    """Anonymous Rumble chat client using SSE.

    Connects to a Rumble livestream's internal chat SSE endpoint and dispatches messages
    via a callback.

    Example::

        rumble = RumbleChat(
            stream_id="abcdef",
            on_message=handle_chat,
        )
        await rumble.run()  # blocks until cancelled

    Attributes:
        connected: :class:`asyncio.Event` set once the SSE connection is established and
        the initial ``init`` event is processed.
    """

    __slots__ = (
        "_on_message",
        "_recent_bans",
        "_sse_url",
        "_stream_id",
        "_users",
        "connected",
    )

    def __init__(
        self,
        *,
        stream_id: str,
        on_message: Callable[[ChatMessage], None] | None = None,
    ) -> None:
        """Initialise the Rumble chat client.

        Args:
            stream_id: Rumble stream ID (base-10 numeric string).
            on_message: Sync callback invoked for each chat message.
        """
        self._stream_id: str = stream_id.strip()
        self._sse_url: str = _SSE_URL.format(stream_id=self._stream_id)
        self._on_message: Callable[[ChatMessage], None] | None = on_message
        self._recent_bans: dict[str, float] = {}
        self._users: dict[str, JsonObj] = {}
        self.connected = asyncio.Event()

    def was_recently_banned(self, *, user_id: str, within: float) -> bool:
        """Check whether a user was recently banned or had messages deleted.

        Args:
            user_id: Rumble user ID to look up.
            within: Maximum age in seconds.

        Returns:
            bool: ``True`` if a moderation event targeting this user was received within
            the given time window.
        """
        return check_recent_ban(bans=self._recent_bans, user_id=user_id, within=within)

    def clean_text(self, msg: ChatMessage) -> str:
        """Strip Rumble ``:name:`` emote tokens from a message.

        Args:
            msg: The chat message to clean.

        Returns:
            str: Message text with emote tokens removed.
        """
        return strip_rumble_emotes(text=msg.text)

    async def run(self) -> None:
        """Connect and listen indefinitely.

        Automatically reconnects on connection loss.

        Raises:
            asyncio.CancelledError: Propagated when the task is cancelled externally.
        """
        while True:
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except OSError:
                logger.warning(
                    "Rumble SSE disconnected; reconnecting in 5 s",
                    exc_info=True,
                )
                self.connected.clear()
                await asyncio.sleep(RECONNECT_DELAY)

    async def _listen(self) -> None:
        """Execute a single SSE connection lifecycle.

        Runs the blocking SSE reader in a thread and dispatches events back to the event
        loop.
        """
        self.connected.clear()
        self._users.clear()

        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        await asyncio.to_thread(self._blocking_sse_read, loop)

    def _blocking_sse_read(self, loop: asyncio.AbstractEventLoop) -> None:
        """Read SSE events from the Rumble chat endpoint.

        This method runs in a background thread.
        Events are dispatched back to the event loop via ``call_soon_threadsafe``.

        Args:
            loop: The asyncio event loop to dispatch callbacks on.
        """
        with Session() as session:
            resp: Response = session.get(
                url=self._sse_url,
                headers={
                    "Accept": "text/event-stream",
                    "User-Agent": chrome_user_agent(),
                },
                stream=True,
            )
            resp.raise_for_status()

            buffer: str = ""
            for chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
                buffer += str(chunk)

                # SSE events are delimited by double newlines.
                while "\n\n" in buffer:
                    event_str, buffer = buffer.split(sep="\n\n", maxsplit=1)
                    self._process_sse_block(block=event_str, loop=loop)

    def _process_sse_block(self, block: str, loop: asyncio.AbstractEventLoop) -> None:
        """Parse a single SSE event block and dispatch it.

        Args:
            block: Raw SSE event text (one or more ``data:`` lines).
            loop: Event loop for thread-safe callbacks.
        """
        data_str: str = _parse_sse_data(block=block)
        if not data_str:
            return

        try:
            raw: JsonValue = json.loads(s=data_str)
        except json.JSONDecodeError:
            return

        if not isinstance(raw, dict):
            return

        event_type: JsonValue = raw.get("type")
        if not isinstance(event_type, str):
            return

        if event_type in {"init", "messages"}:
            self._process_event_data(event_data=raw, loop=loop)
            if event_type == "init":
                loop.call_soon_threadsafe(callback=self.connected.set)
                logger.info("Rumble SSE connected (stream %s)", self._stream_id)
        elif event_type in {"delete_messages", "delete_non_rant_messages"}:
            self._handle_deletions(event_data=raw)

    def _process_event_data(
        self,
        event_data: JsonObj,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Extract, cache users, and dispatch messages from an SSE event.

        Args:
            event_data: Parsed JSON from an SSE event.
            loop: Event loop for thread-safe callbacks.
        """
        data_raw: JsonValue = event_data.get("data")
        if not isinstance(data_raw, dict):
            return

        _cache_users(raw=data_raw.get("users"), cache=self._users)

        if self._on_message is not None:
            _dispatch_messages(
                raw=data_raw.get("messages"),
                parse=self._parse_message,
                on_message=self._on_message,
                loop=loop,
            )

    def _handle_deletions(self, event_data: JsonObj) -> None:
        """Process message deletion events for ban tracking.

        Args:
            event_data: Parsed JSON from a deletion event.
        """
        data_raw: JsonValue = event_data.get("data")
        if not isinstance(data_raw, dict):
            return

        now: float = time.monotonic()
        user_id: JsonValue = data_raw.get("user_id")
        if isinstance(user_id, int | str):
            self._recent_bans[str(user_id)] = now

    def _parse_message(self, msg_data: JsonObj) -> ChatMessage | None:
        """Convert a Rumble message JSON block into a :class:`ChatMessage`.

        Args:
            msg_data: Single message block from the SSE payload.

        Returns:
            ChatMessage | None: ``ChatMessage`` or ``None`` if required fields are
            missing.
        """
        text: JsonValue = msg_data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        user_id_raw: JsonValue = msg_data.get("user_id")
        user_id_str: str = str(user_id_raw) if user_id_raw is not None else ""

        # Resolve user info from the cache.
        cached_user: JsonObj = self._users.get(user_id_str, {})
        username: JsonValue = cached_user.get("username")
        username_str: str = str(username) if isinstance(username, str) else user_id_str

        # Extract badges from cached user data.
        badges: set[str] = set[str]()
        badges_raw: JsonValue = cached_user.get("badges")
        if isinstance(badges_raw, list):
            badges = {str(b) for b in badges_raw if isinstance(b, str)}

        return ChatMessage(
            platform=RUMBLE,
            user_id=user_id_str,
            username=username_str.lower(),
            display_name=username_str,
            text=text,
            is_mod="moderator" in badges,
            is_broadcaster="admin" in badges,
        )
