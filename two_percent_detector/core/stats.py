"""Chat statistics tracking for the monitor.

Maintains rolling-window counters for message rates and per-user totals for the duration
of a session.
All state is in-memory and resets when the process restarts.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from typing import Final

# Sparkline block characters (ascending fill level).
_SPARK_CHARS: Final[str] = "▁▂▃▄▅▆▇█"

# Rolling window durations for rate calculations.
_RATE_WINDOW: Final[float] = 60.0
_RATE_WINDOW_5M: Final[float] = 300.0
_HOUR: Final[float] = 3600.0
_MINUTE: Final[float] = 60.0


class ChatStats:
    """Session-scoped chat statistics tracker.

    Call `record_message` for every incoming chat message.
    The class maintains rolling-window timestamps for rate calculation and per-user
    counters for leaderboard queries.
    """

    __slots__ = (
        "_current_minute_count",
        "_current_minute_start",
        "_message_timestamps",
        "_minute_buckets",
        "_unique_users",
        "_user_counts",
        "_user_names",
        "alert_count",
        "ban_count",
        "start_time",
        "timeout_count",
        "total_messages",
    )

    def __init__(self) -> None:
        """Initialise all counters and rolling windows to zero."""
        self.start_time: float = 0.0
        self.total_messages: int = 0
        self.alert_count: int = 0
        self.ban_count: int = 0
        self.timeout_count: int = 0

        self._unique_users: set[str] = set[str]()
        self._user_counts: Counter[str] = Counter[str]()
        self._user_names: dict[str, str] = {}

        # Timestamps of recent messages (kept for up to 1 hour).
        self._message_timestamps: deque[float] = deque[float]()

        # Per-minute message counts for sparkline rendering.
        self._minute_buckets: list[int] = []
        self._current_minute_start: float = 0.0
        self._current_minute_count: int = 0

    # Lifecycle
    def start(self) -> None:
        """Record the session start time.

        Call once when the monitor is ready to receive messages.
        """
        now: float = time.monotonic()
        self.start_time = now
        self._current_minute_start = now

    # Recording
    def record_message(self, *, user_id: str, username: str = "") -> None:
        """Record an incoming chat message.

        Updates the total counter, per-user counters, rolling-window timestamps, and
        per-minute buckets.

        Args:
            user_id: Platform-specific user ID of the sender.
            username: Login name of the sender (used for display in leaderboards).
            May be empty.
        """
        now: float = time.monotonic()
        self.total_messages += 1
        self._unique_users.add(user_id)
        self._user_counts[user_id] += 1
        if username:
            self._user_names[user_id] = username
        self._message_timestamps.append(now)

        # Roll the minute bucket when the current minute is complete.
        if now - self._current_minute_start >= _MINUTE:
            self._minute_buckets.append(self._current_minute_count)
            self._current_minute_count = 0
            self._current_minute_start = now
        self._current_minute_count += 1

        # Trim timestamps older than 1 hour.
        cutoff: float = now - _HOUR
        while self._message_timestamps and self._message_timestamps[0] < cutoff:
            self._message_timestamps.popleft()

    def record_alert(self) -> None:
        """Increment the alert counter."""
        self.alert_count += 1

    def record_ban(self, *, permanent: bool) -> None:
        """Increment the ban or timeout counter.

        Args:
            permanent: `True` for permanent bans, `False` for timeouts.
        """
        if permanent:
            self.ban_count += 1
        else:
            self.timeout_count += 1

    # Queries
    @property
    def unique_users(self) -> int:
        """Number of distinct users seen this session."""
        return len(self._unique_users)

    def _rate(self, *, window: float, scale: float) -> float:
        """Compute a scaled message rate over a rolling window.

        Args:
            window: Rolling window duration in seconds.
            scale: Multiplier to convert from per-second to the desired unit (1.0 for
            msg/s, 60.0 for msg/min, etc.).

        Returns:
            Scaled message rate, or `0.0` if no time has elapsed.
        """
        now: float = time.monotonic()
        cutoff: float = now - window
        count: int = sum(1 for ts in self._message_timestamps if ts >= cutoff)
        elapsed: float = min(now - self.start_time, window)
        return (count / elapsed) * scale if elapsed > 0 else 0.0

    @property
    def messages_per_second(self) -> float:
        """Average msg/s over the last 60 seconds (or since start)."""
        return self._rate(window=_RATE_WINDOW, scale=1.0)

    @property
    def messages_per_minute(self) -> float:
        """Average msg/min over the last 5 minutes (or since start)."""
        return self._rate(window=_RATE_WINDOW_5M, scale=_MINUTE)

    @property
    def messages_per_hour(self) -> float:
        """Average msg/hour over the last hour (or since start)."""
        return self._rate(window=_HOUR, scale=_HOUR)

    @property
    def avg_messages_per_second(self) -> float:
        """Overall average msg/s since session start."""
        elapsed: float = time.monotonic() - self.start_time
        return self.total_messages / elapsed if elapsed > 0 else 0.0

    @property
    def elapsed_str(self) -> str:
        """Human-readable session duration (e.g. `1h23m45s`)."""
        elapsed: int = int(time.monotonic() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h{m:02d}m{s:02d}s"
        return f"{m}m{s:02d}s"

    def top_chatters(self, n: int = 5) -> list[tuple[str, int]]:
        """Return the `n` most active users.

        Args:
            n: Number of users to return.

        Returns:
            List of `(username, message_count)` pairs sorted by descending activity.
        """
        return [
            (self._user_names.get(uid, uid), count)
            for uid, count in self._user_counts.most_common(n)
        ]

    def sparkline(self, width: int = 30) -> str:
        """Build a Unicode sparkline of messages-per-minute.

        Each character represents one minute.
        The most recent `width` minutes are shown using Unicode block elements for fill
        level.

        Args:
            width: Maximum number of minute buckets to display.

        Returns:
            Sparkline string, or an empty string if no data is available.
        """
        buckets: list[int] = list[int](self._minute_buckets)
        if self._current_minute_count > 0:
            buckets.append(self._current_minute_count)
        if not buckets:
            return ""

        data: list[int] = buckets[-width:]
        max_val: int = max(data)
        if max_val == 0:
            return _SPARK_CHARS[0] * len(data)

        last_idx: int = len(_SPARK_CHARS) - 1
        return "".join(
            _SPARK_CHARS[min(int(v / max_val * last_idx), last_idx)] for v in data
        )
