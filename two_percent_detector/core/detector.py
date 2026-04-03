"""Spam-repetition detector for chat messages.

Tracks per-user message history and fires when a user sends the same (or substantially
similar) message a configurable number of times within a rolling time window.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Final

# Tuneable parameters

# Rolling time window in seconds (default: 10 minutes).
WINDOW_SECONDS: Final[float] = 10.0 * 60.0

# Number of similar messages required to trigger an alert.
REPEAT_COUNT: Final[int] = 3

# Minimum `SequenceMatcher` ratio to consider two messages similar.
# Range `0.0` (anything matches) to `1.0` (exact match required).
# The default `0.82` tolerates typos, extra punctuation, and minor wording changes.
SIMILARITY_THRESHOLD: Final[float] = 0.82

# Maximum message records kept per user in memory.
MAX_HISTORY_PER_USER: Final[int] = 20

# Strings at or below this length require exact equality to avoid false positives on
# very short messages like "lol" vs "ok".
_SHORT_TEXT_THRESHOLD: Final[int] = 4

# Matches 3+ consecutive identical characters (e.g. "looool").
_CHAR_REPEAT_PATTERN: Final[re.Pattern[str]] = re.compile(r"(.)\1{2,}")

# Matches any character that is not a Unicode word character or whitespace.
_NON_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", flags=re.UNICODE)

# Matches one or more whitespace characters (for collapsing).
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")

# Collapses every run of identical characters to a single character.
# Used by the common-word checker; more aggressive than `_CHAR_REPEAT_PATTERN`.
_DEDUP_CHAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"(.)\1+")

# Collapses repeated 1-3 character syllables (e.g. "hahahahaha" → "haha").
_SYLLABLE_REPEAT_PATTERN: Final[re.Pattern[str]] = re.compile(r"(.{1,3})\1{2,}")

# Character sets that form common filler/exclamation patterns (e.g. laughter).
# If a normalised message (ignoring spaces) is composed entirely of one of these
# character sets, it is treated as a common word regardless of exact spelling.
_FILLER_CHARSETS: Final[tuple[frozenset[str], ...]] = (
    frozenset[str]("ha"),  # haha, ahaha, hahhaah, ahahhaha, …
    frozenset[str]("he"),  # hehe, heheheh, …
    frozenset[str]("ja"),  # jajaja (Spanish laughter)
    frozenset[str]("ke"),  # kekeke
    frozenset[str]("rs"),  # rsrsrs (Brazilian laughter)
    frozenset[str]("lo"),  # looool, ololol
    frozenset[str]("xd"),  # xdddd, xdxdxd
)

# Minimum length for charset filler detection to avoid matching very short strings
# that might be legitimate short replies.
_FILLER_CHARSET_MIN_LEN: Final[int] = 3

# Common filler words that should not trigger spam alerts on their own.
_COMMON_WORDS: Final[frozenset[str]] = frozenset[str](
    json.loads(
        s=(Path(__file__).parents[1] / "data" / "common_words.json").read_text(
            encoding="utf-8",
        )
    ),
)


# Internal record type
@dataclass(slots=True)
class _MessageRecord:
    """A normalised message with its monotonic arrival timestamp.

    Attributes:
        text_norm: Normalised form of the original message text.
        ts: `time.monotonic()` value at the time of receipt.
    """

    text_norm: str
    ts: float


# Normalisation
def _normalize(text: str) -> str:
    """Return a canonical, comparison-ready form of a chat message.

    Transformations applied in order:

    1. Unicode NFKC normalisation (resolves common homoglyphs).
    2. Case-fold (locale-aware lowercase).
    3. Collapse runs of 3+ identical characters to 2 (`looool` → `lool`).
    4. Strip all non-alphanumeric, non-whitespace characters.
    5. Collapse multiple whitespace characters into a single space.

    Args:
        text: Raw chat message text.

    Returns:
        Normalised string suitable for fuzzy comparison.
    """
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    text = _CHAR_REPEAT_PATTERN.sub(r"\1\1", text)
    text = _NON_WORD_PATTERN.sub("", text)
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def _is_filler_charset(text: str) -> bool:
    """Return `True` if `text` is composed entirely of a known filler character set.

    This catches irregular laughter variants like `"hahahhahahhah"`,
    `"ahahhaahahha"`, `"hhaah"`, etc. that syllable-based dedup cannot handle
    because the repeated units are not uniform.

    Args:
        text: Already-normalised (lowercased, stripped) message text.

    Returns:
        `True` if every character belongs to a single known filler charset.
    """
    chars: frozenset[str] = frozenset[str](text.replace(" ", ""))
    if len(chars) < 1 or len(text) < _FILLER_CHARSET_MIN_LEN:
        return False
    return any(chars <= cs for cs in _FILLER_CHARSETS)


def _is_common_word(normalized: str) -> bool:
    """Check whether `normalized` is a variant of a common filler word.

    Besides an exact lookup the function also tries three relaxations:

    1. Character dedup — collapse every run of identical characters to one
    (`"loll"` → `"lol"`, `"niceee"` → `"nice"`).
    2. Syllable dedup — collapse repeated 1-3 character syllables to two repetitions
    (`"hahahahaha"` → `"haha"`).
    3. Charset filler — check if the message is composed entirely of a known filler
    character set (e.g. only '"h"' and '"a"' for laughter variants).

    Single-character results (e.g. `"2"` from `"2222222222"`) are always treated as
    filler.

    Args:
        normalized: Already-normalised message text.

    Returns:
        `True` if the text should be ignored as filler.
    """
    if normalized in _COMMON_WORDS:
        return True

    deduped: str = _DEDUP_CHAR_PATTERN.sub(r"\1", normalized)
    if deduped in _COMMON_WORDS or len(deduped) <= 1:
        return True

    syllable: str = _SYLLABLE_REPEAT_PATTERN.sub(r"\1\1", normalized)
    if syllable in _COMMON_WORDS:
        return True

    return _is_filler_charset(text=normalized)


# Fuzzy similarity
def _are_similar(a: str, b: str) -> bool:
    """Determine whether two normalised strings are similar enough.

    Special cases:

    - Two empty strings are considered equal.
    - An empty string is never similar to a non-empty one.
    - Very short strings (at most `_SHORT_TEXT_THRESHOLD` characters) require exact
    equality to avoid false positives.

    For longer strings, `SequenceMatcher.quick_ratio` is evaluated first as a cheap
    upper bound.
    The full `ratio` is only computed when the quick bound exceeds the threshold.

    Args:
        a: First normalised message.
        b: Second normalised message.

    Returns:
        `True` if the messages are similar enough to be counted as repetitions.
    """
    if not a or not b:
        return a == b

    if _is_common_word(normalized=a) or _is_common_word(normalized=b):
        return False

    if len(a) <= _SHORT_TEXT_THRESHOLD or len(b) <= _SHORT_TEXT_THRESHOLD:
        return a == b

    matcher: SequenceMatcher[str] = SequenceMatcher[str](None, a, b, autojunk=False)

    # `quick_ratio` is a cheap upper bound; skip the full O(n²)
    # `ratio` if it already falls below the threshold.
    if matcher.quick_ratio() < SIMILARITY_THRESHOLD:
        return False

    return matcher.ratio() >= SIMILARITY_THRESHOLD


# Public detector
class MessageDetector:
    """Per-user repetition detector with fuzzy matching.

    Each call to `process` evaluates whether the supplied message is the n-th similar
    message sent by that user within the rolling `WINDOW_SECONDS` window
    (where n = `REPEAT_COUNT`).

    Messages from other users do not reset any user's history.

    Complexity per call is `O(N)` where N = `MAX_HISTORY_PER_USER` (bounded constant),
    so the detector is safe for high-volume channels.

    Example:
        >>> d = MessageDetector()
        >>> d.process(user_id="u1", text="buy followers cheap")
        0
        >>> d.process(user_id="u1", text="buy followers cheap!!")
        0
        >>> d.process(user_id="u1", text="BUY FOLLOWERS CHEAP")
        3
    """

    __slots__ = ("_history",)

    def __init__(self) -> None:
        """Initialise an empty detector."""
        self._history: dict[str, deque[_MessageRecord]] = defaultdict[
            str, deque[_MessageRecord]
        ](lambda: deque[_MessageRecord](maxlen=MAX_HISTORY_PER_USER))

    def process(self, *, user_id: str, text: str) -> int:
        """Record a message and check for repetition.

        The message is always appended to the user's history regardless of the return
        value.

        Args:
            user_id: Platform-specific user ID of the sender.
            text: Pre-cleaned message text (emotes already stripped).

        Returns:
            Total number of similar messages (including this one) in the window when the
            threshold is met or exceeded, `0` otherwise.
        """
        now: float = time.monotonic()
        cutoff: float = now - WINDOW_SECONDS
        norm: str = _normalize(text=text)

        history: deque[_MessageRecord] = self._history[user_id]

        # Evict expired records (deque is chronological).
        while history and history[0].ts < cutoff:
            history.popleft()

        # Count in-window messages similar to the incoming one before appending, so it
        # is not counted against itself.
        similar_count: int = sum(
            1 for record in history if _are_similar(a=norm, b=record.text_norm)
        )

        history.append(_MessageRecord(text_norm=norm, ts=now))

        total: int = similar_count + 1  # include the current message
        return total if total >= REPEAT_COUNT else 0

    def purge_stale(self) -> int:
        """Remove users whose most recent message has expired.

        Call periodically (e.g. every 5 minutes) to bound memory usage on high-volume
        channels.

        Returns:
            Number of user entries removed.
        """
        now: float = time.monotonic()
        cutoff: float = now - WINDOW_SECONDS

        stale_ids: list[str] = [
            uid
            for uid, history in self._history.items()
            if not history or history[-1].ts < cutoff
        ]

        for uid in stale_ids:
            del self._history[uid]

        return len(stale_ids)

    @property
    def tracked_users(self) -> int:
        """Number of users currently held in memory."""
        return len(self._history)
