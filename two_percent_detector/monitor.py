"""2% Detector — multi-platform chat monitor (read-only).

Connects to one or more live-stream chat platforms (Twitch, Kick, Rumble) and raises a
terminal alert whenever a user sends the same (or substantially similar) message
multiple times within a rolling detection window.

Platform support:
- `Twitch` — anonymous IRC WebSocket.
Twitch-native emotes are stripped via IRC tag positions; 7TV, FFZ, and BTTV emotes are
stripped via the cached emote-word list.
- `Kick`   — anonymous Pusher WebSocket.
- `Rumble` — anonymous SSE stream.

Emoji characters are stripped on all platforms.
Moderators, the broadcaster, and known bots are automatically ignored.

Alerts are delayed by 2 seconds; if the user is timed-out or banned (detected via the
platform's moderation events) within that window, the alert is suppressed.

The bot never posts anything to chat.
No OAuth token is required.

Interactive commands (while running):
- `ss`                — show global + per-platform stats.
- `s`                 — show global stats only.
- `t` / `k` / `r`     — show Twitch / Kick / Rumble stats.
- `start t [channel]` — start (or restart) a platform, optionally with a new channel.
- `stop t`            — stop a platform.
- `status`            — show which platforms are running.
- `h`                 — show help.

Usage::

    uv run monitor --twitch zackrawrr --kick asmongold --rumble Asmongold
    uv run monitor --twitch zackrawrr --kick asmongold
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from two_percent_detector.config import (
    DEFAULT_KICK_CHANNEL,
    DEFAULT_KICK_ID,
    DEFAULT_RUMBLE_CHANNEL,
    DEFAULT_TWITCH_CHANNEL,
    DEFAULT_TWITCH_ID,
    KNOWN_BOTS,
)
from two_percent_detector.core.chat_types import (
    KICK,
    PLATFORM_COLOUR,
    PLATFORM_LABEL,
    RUMBLE,
    TWITCH,
    ClearChatEvent,
    Platform,
    PlatformClient,
)
from two_percent_detector.core.detector import MessageDetector
from two_percent_detector.core.stats import ChatStats
from two_percent_detector.platforms.kick import KickChat, lookup_kick
from two_percent_detector.platforms.rumble import RumbleChat, lookup_rumble
from two_percent_detector.platforms.twitch import TwitchChat, lookup_twitch
from two_percent_detector.ui.terminal import (
    console,
    print_alert,
    print_cleanup,
    print_clearchat,
    print_ready,
    print_stats,
)
from two_percent_detector.utils.emotes import strip_emojis, strip_invisible

if TYPE_CHECKING:
    from logging import Logger

    from two_percent_detector.core.chat_types import (
        ChatMessage,
        Platform,
        PlatformClient,
    )

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
        `run()` → connect all platforms → `_on_message` / `_on_clearchat`
    """

    __slots__ = (
        "_cfg",
        "_channel_names",
        "_clients",
        "_detector",
        "_platform_stats",
        "_platform_tasks",
        "_stats",
    )

    def __init__(self, cfg: PlatformConfig) -> None:
        """Initialise the monitor with platform clients based on config."""
        self._cfg: PlatformConfig = cfg
        self._stats = ChatStats()
        self._detector = MessageDetector()
        self._clients: dict[Platform, PlatformClient] = {}
        self._platform_stats: dict[Platform, ChatStats] = {}
        self._platform_tasks: dict[Platform, asyncio.Task[None]] = {}

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
                on_clearchat=self._on_clearchat,
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
                "[/bold red]",
            )
            return

        for platform, client in self._clients.items():
            task: asyncio.Task[None] = asyncio.create_task(
                coro=client.run(),
                name=f"Monitor:{platform}",
            )
            self._platform_tasks[platform] = task

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

        await asyncio.gather(*self._platform_tasks.values())

    # Startup
    def _print_ready(self) -> None:
        """Build platform descriptions and delegate to `ui.print_ready`."""
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
        """Record ban stats and delegate display to `ui.print_clearchat`."""
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

    # Platform start/stop
    def _is_platform_running(self, platform: Platform) -> bool:
        """Return whether the given platform task is currently running."""
        task: asyncio.Task[None] | None = self._platform_tasks.get(platform)
        return task is not None and not task.done()

    async def _stop_platform(self, platform: Platform) -> None:
        """Cancel a running platform task."""
        task: asyncio.Task[None] | None = self._platform_tasks.get(platform)
        if task is None or task.done():
            label: str = PLATFORM_LABEL.get(platform, platform)
            console.print(f"[yellow]  {label} is not running.[/yellow]")
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        label = PLATFORM_LABEL.get(platform, platform)
        console.print(f"[bold red]  {label} stopped.[/bold red]")

    async def _start_platform(self, platform: Platform, *, channel: str = "") -> None:
        """Start (or restart) a platform by creating a new client and task."""
        if self._is_platform_running(platform=platform):
            label: str = PLATFORM_LABEL.get(platform, platform)
            console.print(f"[yellow]  {label} is already running.[/yellow]")
            return

        client, channel_name = await _resolve_platform_client(
            platform=platform,
            cfg=self._cfg,
            on_message=self._on_message,
            on_clearchat=self._on_clearchat,
            channel=channel,
        )
        if client is None:
            return

        self._clients[platform] = client
        self._channel_names[platform] = channel_name
        if platform not in self._platform_stats:
            self._platform_stats[platform] = ChatStats()
            self._platform_stats[platform].start()

        task: asyncio.Task[None] = asyncio.create_task(
            coro=client.run(),
            name=f"Monitor:{platform}",
        )
        self._platform_tasks[platform] = task

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                fut=client.connected.wait(),
                timeout=_CONNECTION_TIMEOUT_SECONDS,
            )

        label = PLATFORM_LABEL.get(platform, platform)
        colour: str = PLATFORM_COLOUR.get(platform, "white")
        console.print(
            f"[bold {colour}]  {label}[/bold {colour}] started (#{channel_name}).",
        )

    # Interactive command loop
    async def _stdin_loop(self) -> None:
        """Listen for typed commands on stdin and dispatch them."""
        while True:
            line: str = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            cmd: str = line.strip().lower()
            if cmd:
                await self._dispatch_command(cmd=cmd)

    async def _dispatch_command(self, cmd: str) -> None:
        """Route a single interactive command."""
        if cmd in {"ss", "all stats"}:
            self._show_all_stats()
            return
        if cmd in {"s", "stats"}:
            print_stats(stats=self._stats, tracked_users=self._detector.tracked_users)
            return
        plat_id: Platform | None = _PLATFORM_ALIASES.get(cmd)
        if plat_id is not None and plat_id in self._platform_stats:
            print_stats(stats=self._platform_stats[plat_id], platform=plat_id)
            return
        if await self._try_start_stop(cmd=cmd):
            return
        if cmd in {"h", "help", "?"}:
            _print_help()
            return
        if cmd == "status":
            self._print_status()

    def _show_all_stats(self) -> None:
        """Print global stats plus every running platform's stats."""
        print_stats(stats=self._stats, tracked_users=self._detector.tracked_users)
        for plat, ps in self._platform_stats.items():
            if self._is_platform_running(platform=plat):
                print_stats(stats=ps, platform=plat)

    async def _try_start_stop(self, cmd: str) -> bool:
        """Try to parse and execute a start/stop command.

        Returns:
            `True` if the command was handled, `False` otherwise.
        """
        parts: list[str] = cmd.split()
        if not _START_STOP_MIN_PARTS <= len(parts) <= _START_STOP_MAX_PARTS:
            return False
        if parts[0] not in {"start", "stop"}:
            return False
        target: Platform | None = _PLATFORM_ALIASES.get(parts[1])
        if target is None:
            return False
        channel: str = parts[2] if len(parts) == _START_STOP_MAX_PARTS else ""
        if parts[0] == "start":
            await self._start_platform(platform=target, channel=channel)
        else:
            await self._stop_platform(platform=target)
        return True

    def _print_status(self) -> None:
        """Print a summary of which platforms are running or stopped."""
        lines: list[str] = []
        for plat in (TWITCH, KICK, RUMBLE):
            label: str = PLATFORM_LABEL[plat]
            colour: str = PLATFORM_COLOUR[plat]
            channel: str = self._channel_names.get(plat, "")
            if self._is_platform_running(platform=plat):
                status: str = f"[bold green]running[/bold green]  #{channel}"
            elif plat in self._platform_tasks:
                status = "[bold red]stopped[/bold red]"
            else:
                status = "[dim]not configured[/dim]"
            lines.append(f"  [{colour}]{label:8s}[/{colour}]  {status}")
        console.print("\n[bold]Platform Status:[/bold]")
        for ln in lines:
            console.print(ln)
        console.print()


# Module-level aliases & helpers used by the Monitor class.
_PLATFORM_ALIASES: Final[dict[str, Platform]] = {
    "t": TWITCH,
    "twitch": TWITCH,
    "k": KICK,
    "kick": KICK,
    "r": RUMBLE,
    "rumble": RUMBLE,
}

_START_STOP_MIN_PARTS: Final[int] = 2
_START_STOP_MAX_PARTS: Final[int] = 3


def _print_help() -> None:
    """Print available interactive commands."""
    console.print(
        "\n[bold]Commands:[/bold]\n"
        "  [bold]ss[/bold]          All stats (global + each platform)\n"
        "  [bold]s[/bold]           Global stats\n"
        "  [bold]t[/bold] / [bold]k[/bold] / [bold]r[/bold]   "
        "Twitch / Kick / Rumble stats\n"
        "  [bold]start t[/bold] [dim]\\[channel][/dim]  "
        "Start Twitch  "
        "(also: [bold]start k[/bold], [bold]start r[/bold])\n"
        "  [bold]stop t[/bold]      Stop Twitch   "
        "(also: [bold]stop k[/bold], [bold]stop r[/bold])\n"
        "  [bold]status[/bold]      Show which platforms are running\n"
        "  [bold]h[/bold]           Show this help\n"
        "  [bold]Ctrl+C[/bold]      Quit\n",
    )


async def _resolve_platform_client(
    *,
    platform: Platform,
    cfg: PlatformConfig,
    on_message: object,
    on_clearchat: object,
    channel: str = "",
) -> tuple[PlatformClient | None, str]:
    """Build and return a new platform client with its channel name.

    Returns:
        Tuple of `(client, channel_name)`, or `(None, "")` for unknown platforms.
    """
    if platform == TWITCH:
        return await _resolve_twitch(
            cfg=cfg,
            on_message=on_message,
            on_clearchat=on_clearchat,
            channel=channel,
        )
    if platform == KICK:
        return await _resolve_kick(
            cfg=cfg,
            on_message=on_message,
            channel=channel,
        )
    if platform == RUMBLE:
        return await _resolve_rumble(
            cfg=cfg,
            on_message=on_message,
            on_clearchat=on_clearchat,
            channel=channel,
        )
    return None, ""


async def _resolve_twitch(
    *,
    cfg: PlatformConfig,
    on_message: object,
    on_clearchat: object,
    channel: str = "",
) -> tuple[PlatformClient, str]:
    """Resolve Twitch config and build the client.

    Returns:
        Tuple of `(client, channel_name)`.
    """
    name: str = channel or cfg.twitch_channel_name or DEFAULT_TWITCH_CHANNEL
    tid: str = "" if channel else cfg.twitch_channel_id
    if not tid:
        if name == DEFAULT_TWITCH_CHANNEL and DEFAULT_TWITCH_ID:
            tid = DEFAULT_TWITCH_ID
        else:
            console.print(f"[dim]Looking up Twitch ID for [bold]{name}[/bold]...[/dim]")
            name, tid = await asyncio.to_thread(lookup_twitch, login=name)
    cfg.twitch_channel_name = name
    cfg.twitch_channel_id = tid
    return TwitchChat(
        channel=name,
        channel_id=tid,
        on_message=on_message,  # type: ignore[arg-type]
        on_clearchat=on_clearchat,  # type: ignore[arg-type]
    ), name


async def _resolve_kick(
    *,
    cfg: PlatformConfig,
    on_message: object,
    channel: str = "",
) -> tuple[PlatformClient, str]:
    """Resolve Kick config and build the client.

    Returns:
        Tuple of `(client, channel_name)`.
    """
    name: str = channel or cfg.kick_channel_name or DEFAULT_KICK_CHANNEL
    kid: int = 0 if channel else cfg.kick_channel_id
    if not kid:
        if name == DEFAULT_KICK_CHANNEL and DEFAULT_KICK_ID:
            kid = DEFAULT_KICK_ID
        else:
            console.print(
                f"[dim]Looking up Kick chatroom for [bold]{name}[/bold]...[/dim]",
            )
            kid, _ = await asyncio.to_thread(lookup_kick, slug=name)
    cfg.kick_channel_name = name
    cfg.kick_channel_id = kid
    return KickChat(
        channel_name=name,
        chatroom_id=kid,
        on_message=on_message,  # type: ignore[arg-type]
    ), name


async def _resolve_rumble(
    *,
    cfg: PlatformConfig,
    on_message: object,
    on_clearchat: object,
    channel: str = "",
) -> tuple[PlatformClient, str]:
    """Resolve Rumble config and build the client.

    Returns:
        Tuple of `(client, channel_name)`.
    """
    rname: str = channel or cfg.rumble_channel_name or DEFAULT_RUMBLE_CHANNEL
    rsid: str = "" if channel else cfg.rumble_stream_id
    if not rsid:
        console.print(
            f"[dim]Looking up Rumble stream for [bold]{rname}[/bold]...[/dim]",
        )
        rsid, _ = await asyncio.to_thread(lookup_rumble, channel=rname)
    cfg.rumble_channel_name = rname
    cfg.rumble_stream_id = rsid
    return RumbleChat(
        stream_id=rsid,
        on_message=on_message,  # type: ignore[arg-type]
        on_clearchat=on_clearchat,  # type: ignore[arg-type]
    ), rname


# CLI
def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the `monitor` command.

    Each platform flag accepts an optional value.
    When the flag is present without a value the saved default is used.

    Returns:
        Configured argument parser.
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
    """Resolve CLI arguments into a `PlatformConfig`.

    Performs Twitch ID and Rumble stream-ID lookups as needed.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Fully resolved platform configuration.
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
                f"[dim]Looking up Twitch ID for [bold]{twitch_name}[/bold]...[/dim]",
            )
            twitch_name, twitch_id = lookup_twitch(login=twitch_name)

    # Use saved Kick chatroom ID for the default channel.
    if kick_name:
        if kick_name == DEFAULT_KICK_CHANNEL and DEFAULT_KICK_ID:
            kick_id = DEFAULT_KICK_ID
        else:
            console.print(
                f"[dim]Looking up Kick chatroom for [bold]{kick_name}[/bold]...[/dim]",
            )
            kick_id, _kick_user_id = lookup_kick(slug=kick_name)

    # Auto-lookup Rumble stream ID from channel name.
    rumble_stream_id: str = ""
    rumble_display: str = ""
    if rumble_channel:
        console.print(
            f"[dim]Looking up Rumble stream for [bold]{rumble_channel}[/bold]...[/dim]",
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
    """Entry point for the `monitor` console script."""
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
        f"[bold]2% Detector[/bold]\nPlatforms: {plat_str}  -  Starting up...\n",
    )

    try:
        asyncio.run(main=Monitor(cfg=cfg).run())
    except KeyboardInterrupt:
        console.print(
            "\n[bold yellow]Monitor stopped.[/bold yellow]\nPeace \U0001f49c!\n",
        )


if __name__ == "__main__":
    main()
