"""Emote cache and text-cleaning utilities for chat messages.

Provides two categories of functionality:

1. Third-party emote cache (Twitch and Kick) — fetches emote sets from 7TV, FrankerFaceZ
(FFZ), and BetterTTV (BTTV) and exposes a fast `frozenset`-based lookup to strip emote
words from messages.
7TV is available for both Twitch and Kick; FFZ and BTTV are Twitch-exclusive.
Twitch-native emotes are handled separately via the `emotes` IRC tag and do not require
an API call.

2. Cross-platform text stripping — removes emoji characters, invisible Unicode
codepoints, Kick `[emote:ID:NAME]` tokens, and Rumble `:name:` emote tokens from message
text.

All provider endpoints (global + channel for each) are fetched concurrently using HTTP/2
multiplexed streams over a single `niquests.AsyncSession`.
Requests to the same host share one TCP connection; `session.gather()` resolves all
pending streams in a single batch for maximum throughput.
Servers that advertise `Alt-Svc: h3` (currently BTTV and Kick) are transparently
upgraded to HTTP/3 QUIC on subsequent connections.

Provider failures are logged as warnings and do not crash the monitor.
The cache refreshes every `REFRESH_INTERVAL_SECONDS` in the background.

HTTP is handled by `niquests.AsyncSession`, a modern drop-in replacement for `requests`
with native asyncio, HTTP/2 multiplexing, and HTTP/3 QUIC support.

Note:
`AsyncSession.get()` returns `Response | AsyncResponse`.
In multiplexed mode, `get()` returns a lazy `AsyncResponse` whose body is only resolved
after `await session.gather()`.
The `_json` helper abstracts over both flavours so callers can always `await _json(r)`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import TYPE_CHECKING, Final, cast

from niquests import AsyncSession, RequestException

from two_percent_detector.core.chat_types import HTTP_NOT_FOUND, TWITCH

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger
    from types import CoroutineType
    from typing import Any

    from niquests.models import AsyncResponse, Response

    from two_percent_detector.core.chat_types import JsonValue, Platform

logger: Logger = logging.getLogger(name=__name__)

# Provider endpoint templates.
_7TV_GLOBAL_URL: Final[str] = "https://7tv.io/v3/emote-sets/global"
_7TV_CHANNEL_URL: Final[str] = "https://7tv.io/v3/users/{platform}/{channel_id}"

_FFZ_GLOBAL_URL: Final[str] = "https://api.frankerfacez.com/v1/set/global"
_FFZ_CHANNEL_URL: Final[str] = "https://api.frankerfacez.com/v1/room/id/{channel_id}"

_BTTV_GLOBAL_URL: Final[str] = "https://api.betterttv.net/3/cached/emotes/global"
_BTTV_CHANNEL_URL: Final[str] = (
    "https://api.betterttv.net/3/cached/users/twitch/{channel_id}"
)

# How often the emote cache refreshes from all providers (default: 1 hour).
REFRESH_INTERVAL_SECONDS: Final[int] = 3_600

# Per-session request timeout in seconds.
_REQUEST_TIMEOUT: Final[int] = 10

# Matches emoji characters: emoticons, dingbats, symbols, flags, etc.
_EMOJI_CHARS: Final[str] = (
    "\U0000200d"  # zero-width joiner
    "\U0000fe0f"  # variation selector-16
    "\U000020e3"  # combining enclosing keycap
    "\U00002139"  # information source
    "\U00002194-\U00002199"  # arrows
    "\U000021a9-\U000021aa"  # arrows
    "\U0000231a-\U0000231b"  # watch / hourglass
    "\U00002328"  # keyboard
    "\U000023cf"  # eject
    "\U000023e9-\U000023fa"  # media controls
    "\U000024c2"  # circled M
    "\U000025aa-\U000025ab"  # squares
    "\U000025b6"  # play button
    "\U000025c0"  # reverse button
    "\U000025fb-\U000025fe"  # squares
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U00002934-\U00002935"  # arrows
    "\U00002b05-\U00002b07"  # arrows
    "\U00002b1b-\U00002b1c"  # squares
    "\U00002b50"  # star
    "\U00002b55"  # circle
    "\U00003030"  # wavy dash
    "\U0000303d"  # part alternation mark
    "\U00003297"  # circled ideograph congratulation
    "\U00003299"  # circled ideograph secret
    "\U0001f004"  # mahjong red dragon
    "\U0001f0cf"  # joker
    "\U0001f170-\U0001f171"  # blood types
    "\U0001f17e-\U0001f17f"  # blood types
    "\U0001f18e"  # AB button
    "\U0001f191-\U0001f19a"  # squared symbols
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "\U0001f200-\U0001f251"  # enclosed ideographic
    "\U0001f300-\U0001fbff"  # pictographs, transport, skin tones, extended-a
)
_EMOJI_PATTERN: Final[re.Pattern[str]] = re.compile(rf"[{_EMOJI_CHARS}]+")

# Invisible / zero-width Unicode characters that should be stripped.
# Covers combining marks, joiners, variation selectors, and similar.
_INVISIBLE_CHARS: Final[str] = (
    "\u00ad"  # soft hyphen
    "\u034f"  # combining grapheme joiner
    "\u200b-\u200f"  # zw space, zwnj, zwj, lrm, rlm
    "\u202a-\u202e"  # bidi overrides (LRE, RLE, PDF, LRO, RLO)
    "\u2060-\u2064"  # word joiner & invisible math
    "\u2066-\u206f"  # bidi isolates & deprecated formatting
    "\ufeff"  # BOM / zero-width no-break space
    "\ufff0-\ufff8"  # specials
    "\ufffc"  # object replacement character
    "\U000e0000-\U000e007f"  # tags block
    "\ufe00-\ufe0f"  # variation selectors
    "\U000e0100-\U000e01ef"  # variation selectors supplement
)
_INVISIBLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"[{_INVISIBLE_CHARS}]+",
)

# Kick embeds emotes as `[emote:ID:NAME]` in the message text.
_KICK_EMOTE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\[emote:\d+:[^\]]*\]",
)

# Rumble embeds emotes as `:name:` (e.g. `:r+usa:`, `:laughing:`).
_RUMBLE_EMOTE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r":[\w+]+:",
)


def strip_emojis(text: str) -> str:
    """Remove emoji characters from `text`.

    Args:
        text: Message text (emotes already removed).

    Returns:
        Text with all emoji sequences removed.
        May be empty if the message consisted entirely of emojis.
    """
    return _EMOJI_PATTERN.sub("", text)


def strip_invisible(text: str) -> str:
    """Remove invisible / zero-width Unicode characters from `text`.

    Args:
        text: Message text.

    Returns:
        Text with invisible codepoints removed.
    """
    return _INVISIBLE_PATTERN.sub("", text)


def strip_kick_emotes(text: str) -> str:
    """Remove Kick `[emote:ID:NAME]` tokens from `text`.

    Args:
        text: Raw Kick message text.

    Returns:
        Text with emote tokens removed.
    """
    return _KICK_EMOTE_PATTERN.sub("", text)


def strip_rumble_emotes(text: str) -> str:
    """Remove Rumble `:name:` emote tokens from `text`.

    Args:
        text: Raw Rumble message text.

    Returns:
        Text with emote tokens removed.
    """
    return _RUMBLE_EMOTE_PATTERN.sub("", text)


# Response helpers
async def _json(response: Response | AsyncResponse) -> JsonValue:
    """Await `response.json()` regardless of sync/async flavour.

    `niquests.AsyncSession` may return either `Response` (sync) or `AsyncResponse`
    depending on connection pool configuration.
    This helper normalises the two code paths so callers can always `await _json(r)`.

    Args:
        response: A response object from `niquests.AsyncSession`.

    Returns:
        The parsed JSON body.
    """
    result: CoroutineType[Any, Any, Any] | Any = response.json()
    if inspect.isawaitable(object=result):
        return cast("JsonValue", await result)
    return cast("JsonValue", result)


# Public cache
class EmoteCache:
    """Aggregated emote name set from 7TV, FFZ, and BTTV.

    Provider endpoints are fetched concurrently over HTTP/2 multiplexed streams and
    resolved in a single batch via `session.gather()`.
    A provider failure only removes that provider's contribution; the remaining sets are
    merged normally.

    Example::

        cache = EmoteCache()
        await cache.start(channel_id="123456789")

        clean = cache.strip_emotes(message_text)

        await cache.stop()

    Attributes:
        total_emotes: Number of cached emote names across all providers.
    """

    __slots__ = ("_emotes", "_refresh_task")

    def __init__(self) -> None:
        """Initialise an empty emote cache with no background task."""
        self._emotes: frozenset[str] = frozenset[str]()
        self._refresh_task: asyncio.Task[None] | None = None

    # Lifecycle
    async def start(
        self,
        *,
        channel_id: str,
        platform: Platform = TWITCH,
    ) -> None:
        """Perform the initial fetch and start the background refresh.

        Args:
            channel_id: Numeric user ID on the target platform.
            platform: Platform whose 7TV channel set to fetch.
            FFZ and BTTV are only fetched for Twitch.
        """
        await self._refresh(
            channel_id=channel_id,
            platform=platform,
        )
        self._refresh_task = asyncio.create_task(
            coro=self._refresh_loop(
                channel_id=channel_id,
                platform=platform,
            ),
            name=f"{platform}:emote_refresh",
        )
        logger.info(
            "Emote cache ready (%s). %d emote names loaded.",
            platform,
            self.total_emotes,
        )

    async def stop(self) -> None:
        """Cancel the background refresh task if it is running."""
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            self._refresh_task = None

    # Public API
    def strip_emotes(self, text: str) -> str:
        """Remove known emote words from `text`.

        Words are split on whitespace.
        Any token that exactly matches a cached emote name is dropped.
        The remaining tokens are re-joined with single spaces.
        Emote names are case-sensitive (`KEKW` ≠ `kekw`).

        Args:
            text: Message text with Twitch-native emotes already removed.

        Returns:
            Message text with third-party emote tokens removed.
            May be empty if the message consisted entirely of emotes.
        """
        if not text or not self._emotes:
            return text
        return " ".join(word for word in text.split() if word not in self._emotes)

    @property
    def total_emotes(self) -> int:
        """Total number of cached emote names across all providers."""
        return len(self._emotes)

    # Background refresh
    async def _refresh_loop(
        self,
        *,
        channel_id: str,
        platform: Platform,
    ) -> None:
        """Periodically re-fetch all provider endpoints.

        Args:
            channel_id: Numeric user ID on the target platform.
            platform: Platform identifier.
        """
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            await self._refresh(
                channel_id=channel_id,
                platform=platform,
            )

    async def _refresh(
        self,
        *,
        channel_id: str,
        platform: Platform,
    ) -> None:
        """Fetch provider endpoints and atomically swap the cache.

        Requests are fired concurrently over HTTP/2 multiplexed streams on a single
        `AsyncSession` and resolved in one batch via `session.gather()`.
        Servers advertising `Alt-Svc: h3` are transparently upgraded to HTTP/3 QUIC.

        7TV endpoints are fetched for every platform; FFZ and BTTV are Twitch-exclusive.

        Args:
            channel_id: Numeric user ID on the target platform.
            platform: Platform identifier.
        """
        tasks: list[tuple[str, Callable[[JsonValue], set[str]], bool]] = [
            (_7TV_GLOBAL_URL, self._parse_7tv_global, False),
            (
                _7TV_CHANNEL_URL.format(
                    platform=platform,
                    channel_id=channel_id,
                ),
                self._parse_7tv_channel,
                True,
            ),
        ]
        if platform == TWITCH:
            tasks.extend([
                (_FFZ_GLOBAL_URL, self._parse_ffz, False),
                (
                    _FFZ_CHANNEL_URL.format(channel_id=channel_id),
                    self._parse_ffz,
                    True,
                ),
                (_BTTV_GLOBAL_URL, self._parse_bttv_global, False),
                (
                    _BTTV_CHANNEL_URL.format(channel_id=channel_id),
                    self._parse_bttv_channel,
                    True,
                ),
            ])

        async with AsyncSession(
            multiplexed=True,
            timeout=_REQUEST_TIMEOUT,
            happy_eyeballs=True,
        ) as session:
            # Fire all requests — returns lazy multiplexed responses.
            responses: list[Response | AsyncResponse] = [
                await session.get(url) for url, _, _ in tasks
            ]
            # Resolve all pending HTTP/2 streams in a single batch.
            await session.gather()

            merged: set[str] = set[str]()
            for (url, parser, not_found_ok), response in zip(
                tasks,
                responses,
                strict=True,
            ):
                try:
                    if not_found_ok and response.status_code == HTTP_NOT_FOUND:
                        continue
                    response.raise_for_status()
                    data: JsonValue = await _json(response=response)
                    merged |= parser(data)
                except RequestException, ValueError:
                    logger.warning(
                        "Emote provider fetch failed: %s",
                        url,
                        exc_info=True,
                    )

        previous: int = self.total_emotes
        self._emotes = frozenset[str](merged)
        logger.info(
            "Emote cache refreshed: %d total emotes (%+d).",
            self.total_emotes,
            self.total_emotes - previous,
        )

    # Response parsers — called on the resolved JSON body of each endpoint.

    # 7TV
    @staticmethod
    def _parse_7tv_global(data: JsonValue) -> set[str]:
        """Parse the 7TV global emote set.

        Returns:
            Set of global 7TV emote names.
        """
        if not isinstance(data, dict):
            return set[str]()
        raw_emotes: JsonValue = data.get("emotes", [])
        if not isinstance(raw_emotes, list):
            return set[str]()
        return {
            name
            for e in raw_emotes
            if isinstance(e, dict) and isinstance(name := e.get("name"), str)
        }

    @staticmethod
    def _parse_7tv_channel(data: JsonValue) -> set[str]:
        """Parse the channel-specific 7TV emote set.

        Returns:
            Set of channel 7TV emote names.
        """
        if not isinstance(data, dict):
            return set[str]()
        emote_set: JsonValue = data.get("emote_set")
        if not isinstance(emote_set, dict):
            return set[str]()
        raw_emotes: JsonValue = emote_set.get("emotes", [])
        if not isinstance(raw_emotes, list):
            return set[str]()
        return {
            name
            for e in raw_emotes
            if isinstance(e, dict) and isinstance(name := e.get("name"), str)
        }

    # FrankerFaceZ
    @staticmethod
    def _parse_ffz(data: JsonValue) -> set[str]:
        """Parse emote names from an FFZ API response body.

        FFZ nests emotes under `data["sets"][<set_id>]["emoticons"]`.

        Returns:
            Flat set of emote name strings.
        """
        if not isinstance(data, dict):
            return set[str]()
        emotes: set[str] = set[str]()
        sets: JsonValue = data.get("sets")
        if not isinstance(sets, dict):
            return emotes
        for emote_set in sets.values():
            if not isinstance(emote_set, dict):
                continue
            emoticons: JsonValue = emote_set.get("emoticons")
            if not isinstance(emoticons, list):
                continue
            for emoticon in emoticons:
                if isinstance(emoticon, dict) and isinstance(
                    name := emoticon.get("name"),
                    str,
                ):
                    emotes.add(name)
        return emotes

    # BetterTTV
    @staticmethod
    def _parse_bttv_global(data: JsonValue) -> set[str]:
        """Parse the BTTV global emote set.

        Returns:
            Set of global BTTV emote codes.
        """
        if not isinstance(data, list):
            return set[str]()
        return {
            code
            for e in data
            if isinstance(e, dict) and isinstance(code := e.get("code"), str)
        }

    @staticmethod
    def _parse_bttv_channel(data: JsonValue) -> set[str]:
        """Parse the channel and shared BTTV emote sets.

        Returns:
            Combined set of channel and shared BTTV emote codes.
        """
        if not isinstance(data, dict):
            return set[str]()
        emotes: set[str] = set[str]()
        for key in ("channelEmotes", "sharedEmotes"):
            entries: JsonValue = data.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and isinstance(
                    code := entry.get("code"),
                    str,
                ):
                    emotes.add(code)
        return emotes
