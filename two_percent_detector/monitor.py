"""2% Detector — multi-platform chat monitor (read-only).

Connects to one or more live-stream chat platforms (Twitch, Kick, Rumble) and raises a
terminal alert whenever a user sends the same (or substantially similar) message
multiple times within a rolling detection window.

Platform support:

- **Twitch** — anonymous IRC WebSocket.
Twitch-native emotes are stripped via IRC tag positions; 7TV, FFZ, and BTTV emotes are
stripped via the cached emote-word list.
- **Kick** — anonymous Pusher WebSocket.
- **Rumble** — anonymous SSE stream.

Emoji characters are stripped on all platforms.
Moderators, the broadcaster, and known bots are automatically ignored.

Alerts are delayed by 2 seconds; if the user is timed-out or banned (detected via the
platform's moderation events) within that window, the alert is suppressed.

The bot never posts anything to chat.
No OAuth token is required.

Usage::

    uv run monitor --twitch zackrawrr --kick asmongold --rumble Asmongold
    uv run monitor --twitch zackrawrr --kick asmongold
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .chat_types import (
    KICK,
    PLATFORM_COLOUR,
    PLATFORM_LABEL,
    RUMBLE,
    TWITCH,
    ClearChatEvent,
    Platform,
    PlatformClient,
)
from .config import (
    DEFAULT_KICK_CHANNEL,
    DEFAULT_KICK_ID,
    DEFAULT_RUMBLE_CHANNEL,
    DEFAULT_TWITCH_CHANNEL,
    DEFAULT_TWITCH_ID,
    KNOWN_BOTS,
)
from .detector import MessageDetector
from .emotes import strip_emojis, strip_invisible
from .kick_monitor import KickChat, lookup_kick
from .rumble_monitor import RumbleChat, lookup_rumble
from .stats import ChatStats
from .twitch_monitor import TwitchChat, lookup_twitch
from .ui import (
    console,
    print_alert,
    print_cleanup,
    print_clearchat,
    print_ready,
    print_stats,
)

if TYPE_CHECKING:
    from logging import Logger

    from .chat_types import ChatMessage, Platform, PlatformClient

# Logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger: Logger = logging.getLogger(name=__name__)

# Niquests logs verbose connection info at DEBUG/INFO level.
logging.getLogger(name="niquests").setLevel(level=logging.ERROR)


# Constants
_CLEANUP_INTERVAL_SECONDS: Final[int] = 300
_ALERT_DELAY_SECONDS: Final[float] = 2.0
_BAN_GRACE_SECONDS: Final[float] = 10.0
_CONNECTION_TIMEOUT_SECONDS: Final[float] = 15.0


# Platform configuration (populated from CLI args)
@dataclass(slots=True, kw_only=True)
class PlatformConfig:
    """Holds per-platform identifiers resolved from CLI arguments.

    Attributes:
        twitch_channel_id: Twitch numeric user ID (empty to disable).
        twitch_channel_name: Twitch login name (empty to disable).
        kick_channel_id: Kick chatroom ID (zero to auto-resolve).
        kick_channel_name: Kick channel slug (empty to disable).
        rumble_stream_id: Rumble stream ID (empty to disable).
        rumble_channel_name: Rumble channel name for display (empty to disable).
    """

    twitch_channel_id: str = ""
    twitch_channel_name: str = ""
    kick_channel_id: int = 0
    kick_channel_name: str = ""
    rumble_stream_id: str = ""
    rumble_channel_name: str = ""


# Monitor
class Monitor:
    """Read-only multi-platform chat monitor.

    Lifecycle:
        ``run()`` → connect all platforms → ``_on_message`` / ``_on_clearchat``
    """

    __slots__ = (
        "_cfg",
        "_channel_names",
        "_clients",
        "_detector",
        "_platform_stats",
        "_stats",
    )

    def __init__(self, cfg: PlatformConfig) -> None:
        """Initialise the monitor with platform clients based on config."""
        self._cfg: PlatformConfig = cfg
        self._stats = ChatStats()
        self._detector = MessageDetector()
        self._clients: dict[Platform, PlatformClient] = {}
        self._platform_stats: dict[Platform, ChatStats] = {}

        if cfg.twitch_channel_name:
            self._clients[TWITCH] = TwitchChat(
                channel=cfg.twitch_channel_name,
                channel_id=cfg.twitch_channel_id,
                on_message=self._on_message,
                on_clearchat=self._on_clearchat,
            )
        if cfg.kick_channel_name:
            self._clients[KICK] = KickChat(
                channel_name=cfg.kick_channel_name,
                chatroom_id=cfg.kick_channel_id,
                on_message=self._on_message,
            )
        if cfg.rumble_stream_id:
            self._clients[RUMBLE] = RumbleChat(
                stream_id=cfg.rumble_stream_id,
                on_message=self._on_message,
            )

        for platform in self._clients:
            self._platform_stats[platform] = ChatStats()

        # Pre-built mapping for alert channel names and display.
        self._channel_names: dict[Platform, str] = {}
        if cfg.twitch_channel_name:
            self._channel_names[TWITCH] = cfg.twitch_channel_name
        if cfg.kick_channel_name:
            self._channel_names[KICK] = cfg.kick_channel_name
        if cfg.rumble_channel_name:
            self._channel_names[RUMBLE] = cfg.rumble_channel_name

    async def run(self) -> None:
        """Start all configured platform monitors and block until cancelled."""
        if not self._clients:
            console.print(
                "[bold red]No platforms configured. Pass at least one channel."
                "[/bold red]"
            )
            return

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(coro=client.run(), name=f"Monitor:{platform}")
            for platform, client in self._clients.items()
        ]

        # Wait for all platforms to connect (with a generous timeout so a slow platform
        # doesn't block the startup banner forever).
        waiters: list[asyncio.Task[bool]] = [
            asyncio.create_task(coro=client.connected.wait())
            for client in self._clients.values()
        ]
        await asyncio.wait(waiters, timeout=_CONNECTION_TIMEOUT_SECONDS)

        self._stats.start()
        for ps in self._platform_stats.values():
            ps.start()
        self._print_ready()

        asyncio.create_task(coro=self._cleanup_loop(), name="Monitor:cleanup")
        asyncio.create_task(coro=self._stdin_loop(), name="Monitor:stdin")

        await asyncio.gather(*tasks)

    # Startup
    def _print_ready(self) -> None:
        """Build platform descriptions and delegate to :func:`ui.print_ready`."""
        # Pad labels to align channel names.
        max_label: int = max(len(v) for v in PLATFORM_LABEL.values())
        platforms: list[str] = [
            f"[bold {PLATFORM_COLOUR[p]}]{PLATFORM_LABEL[p]}"
            f"[/bold {PLATFORM_COLOUR[p]}]"
            f"{' ' * (max_label - len(PLATFORM_LABEL[p]) + 3)}#{name}"
            for p, name in self._channel_names.items()
        ]
        twitch_client: PlatformClient | None = self._clients.get(TWITCH)
        emote_count: int = (
            twitch_client.total_emotes if isinstance(twitch_client, TwitchChat) else 0
        )
        print_ready(
            platforms=platforms,
            active=set(self._platform_stats.keys()),
            emote_count=emote_count,
            has_twitch=TWITCH in self._clients,
            bot_count=len(KNOWN_BOTS),
        )

    # Message callback
    def _on_message(self, msg: ChatMessage) -> None:
        """Process an incoming chat message from any platform."""
        self._stats.record_message(user_id=msg.user_id, username=msg.username)
        plat: ChatStats | None = self._platform_stats.get(msg.platform)
        if plat is not None:
            plat.record_message(user_id=msg.user_id, username=msg.username)

        if msg.is_mod or msg.is_broadcaster or msg.username in KNOWN_BOTS:
            return

        client: PlatformClient | None = self._clients.get(msg.platform)
        text_clean: str = client.clean_text(msg=msg) if client is not None else msg.text

        text_clean = strip_emojis(text=text_clean)
        text_clean = strip_invisible(text=text_clean)

        if not text_clean.strip():
            return

        count: int = self._detector.process(user_id=msg.user_id, text=text_clean)

        if count:
            asyncio.create_task(
                coro=self._delayed_alert(msg=msg, text=text_clean, count=count),
                name=f"Monitor:alert:{msg.platform}:{msg.user_id}",
            )

    # CLEARCHAT callback
    def _on_clearchat(self, event: ClearChatEvent) -> None:
        """Record ban stats and delegate display to :func:`ui.print_clearchat`."""
        if not event.username:
            return
        self._stats.record_ban(permanent=event.permanent)
        plat: ChatStats | None = self._platform_stats.get(event.platform)
        if plat is not None:
            plat.record_ban(permanent=event.permanent)
        print_clearchat(event=event)

    # Delayed alert with ban-check
    async def _delayed_alert(
        self,
        *,
        msg: ChatMessage,
        text: str,
        count: int,
    ) -> None:
        """Wait briefly, then print the alert if the user was not moderated."""
        await asyncio.sleep(_ALERT_DELAY_SECONDS)

        client: PlatformClient | None = self._clients.get(msg.platform)
        recently_banned: bool = (
            client.was_recently_banned(user_id=msg.user_id, within=_BAN_GRACE_SECONDS)
            if client is not None
            else False
        )

        if recently_banned:
            return

        self._stats.record_alert()
        plat: ChatStats | None = self._platform_stats.get(msg.platform)
        if plat is not None:
            plat.record_alert()

        channel: str = self._channel_names.get(msg.platform, "")

        print_alert(
            msg=msg,
            text=text,
            count=count,
            channel_name=channel,
            alert_number=self._stats.alert_count,
        )

    # Background tasks
    async def _cleanup_loop(self) -> None:
        """Periodically purge stale user history to bound memory usage."""
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            removed: int = self._detector.purge_stale()
            if removed:
                print_cleanup(removed=removed, tracked=self._detector.tracked_users)

    async def _stdin_loop(self) -> None:
        """Listen for typed commands on stdin and dispatch them."""
        while True:
            line: str = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            cmd: str = line.strip().lower()
            if cmd in {"stats", "s"}:
                print_stats(
                    stats=self._stats, tracked_users=self._detector.tracked_users
                )
            elif cmd in {"twitch", "t"} and TWITCH in self._platform_stats:
                print_stats(stats=self._platform_stats[TWITCH], platform=TWITCH)
            elif cmd in {"kick", "k"} and KICK in self._platform_stats:
                print_stats(stats=self._platform_stats[KICK], platform=KICK)
            elif cmd in {"rumble", "r"} and RUMBLE in self._platform_stats:
                print_stats(stats=self._platform_stats[RUMBLE], platform=RUMBLE)


# CLI
def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``monitor`` command.

    Each platform flag accepts an optional value.
    When the flag is present **without** a value the saved default is used.

    Returns:
        argparse.ArgumentParser: Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="monitor",
        description="2%% Detector — multi-platform chat monitor.",
    )

    parser.add_argument(
        "--twitch",
        metavar="CHANNEL",
        nargs="?",
        const=DEFAULT_TWITCH_CHANNEL,
        default=None,
        help=(
            "Twitch channel login name.  "
            f"Omit the value to use the saved default ({DEFAULT_TWITCH_CHANNEL})."
        ),
    )
    parser.add_argument(
        "--kick",
        metavar="CHANNEL",
        nargs="?",
        const=DEFAULT_KICK_CHANNEL,
        default=None,
        help=(
            f"Kick channel slug.  Omit the value to use the saved default "
            f"({DEFAULT_KICK_CHANNEL})."
        ),
    )
    parser.add_argument(
        "--rumble",
        metavar="CHANNEL",
        nargs="?",
        const=DEFAULT_RUMBLE_CHANNEL,
        default=None,
        help=(
            "Rumble channel name (live stream ID is resolved automatically).  "
            f"Omit the value to use the saved default ({DEFAULT_RUMBLE_CHANNEL})."
        ),
    )
    return parser


def _build_config(args: argparse.Namespace) -> PlatformConfig:
    """Resolve CLI arguments into a :class:`PlatformConfig`.

    Performs Twitch ID and Rumble stream-ID lookups as needed.

    Args:
        args: Parsed CLI namespace.

    Returns:
        PlatformConfig: Fully resolved platform configuration.
    """
    twitch_name: str = args.twitch.lower().strip() if args.twitch else ""
    twitch_id: str = ""
    kick_name: str = args.kick.lower().strip() if args.kick else ""
    kick_id: int = 0
    rumble_channel: str = args.rumble.strip() if args.rumble else ""

    # Auto-lookup Twitch ID.
    if twitch_name:
        if twitch_name == DEFAULT_TWITCH_CHANNEL and DEFAULT_TWITCH_ID:
            twitch_id = DEFAULT_TWITCH_ID
        else:
            console.print(
                f"[dim]Looking up Twitch ID for [bold]{twitch_name}[/bold]...[/dim]"
            )
            twitch_name, twitch_id = lookup_twitch(login=twitch_name)

    # Use saved Kick chatroom ID for the default channel.
    if kick_name:
        if kick_name == DEFAULT_KICK_CHANNEL and DEFAULT_KICK_ID:
            kick_id = DEFAULT_KICK_ID
        else:
            console.print(
                f"[dim]Looking up Kick chatroom for [bold]{kick_name}[/bold]...[/dim]"
            )
            kick_id, _kick_user_id = lookup_kick(slug=kick_name)

    # Auto-lookup Rumble stream ID from channel name.
    rumble_stream_id: str = ""
    rumble_display: str = ""
    if rumble_channel:
        console.print(
            f"[dim]Looking up Rumble stream for [bold]{rumble_channel}[/bold]...[/dim]"
        )
        rumble_stream_id, _title = lookup_rumble(channel=rumble_channel)
        rumble_display = rumble_channel

    return PlatformConfig(
        twitch_channel_id=twitch_id,
        twitch_channel_name=twitch_name,
        kick_channel_id=kick_id,
        kick_channel_name=kick_name,
        rumble_stream_id=rumble_stream_id,
        rumble_channel_name=rumble_display,
    )


def main() -> None:
    """Entry point for the ``monitor`` console script."""
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args()

    cfg: PlatformConfig = _build_config(args=args)

    active: tuple[tuple[Platform, str, str], ...] = (
        (TWITCH, cfg.twitch_channel_name, cfg.twitch_channel_name),
        (KICK, cfg.kick_channel_name, cfg.kick_channel_name),
        (RUMBLE, cfg.rumble_stream_id, cfg.rumble_channel_name),
    )
    enabled: list[str] = [
        f"{PLATFORM_LABEL[p]} ({name})" for p, gate, name in active if gate
    ]
    plat_str: str = ", ".join(enabled) if enabled else "[red]none configured[/red]"

    console.print(
        f"[bold]2% Detector[/bold]\nPlatforms: {plat_str}  -  Starting up...\n"
    )

    try:
        asyncio.run(main=Monitor(cfg=cfg).run())
    except KeyboardInterrupt:
        console.print(
            "\n[bold yellow]Monitor stopped.[/bold yellow]\nPeace \U0001f49c!\n"
        )


if __name__ == "__main__":
    main()
