"""Rich terminal output for the 2% Detector.

All console rendering (alert panels, statistics, startup banners, and inline status
messages) is centralised here.
Functions receive data as explicit arguments — no module-level singletons are accessed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from two_percent_detector.core.chat_types import (
    KICK,
    PLATFORM_COLOUR,
    PLATFORM_LABEL,
    RUMBLE,
    ClearChatEvent,
)
from two_percent_detector.core.detector import (
    REPEAT_COUNT,
    SIMILARITY_THRESHOLD,
    WINDOW_SECONDS,
)
from two_percent_detector.integrations.discord import AlertContext, send_alert

if TYPE_CHECKING:
    from two_percent_detector.core.chat_types import ChatMessage, Platform
    from two_percent_detector.core.stats import ChatStats

# Shared console instance.
console = Console(highlight=False)

# Derived display constants.
_WINDOW_MINUTES: Final[int] = int(WINDOW_SECONDS // 60)
_SIMILARITY_PCT: Final[int] = int(SIMILARITY_THRESHOLD * 100)


# Alert panel
def print_alert(
    *,
    msg: ChatMessage,
    text: str,
    count: int,
    channel_name: str,
    alert_number: int,
) -> None:
    """Render a Rich alert panel and dispatch the Discord webhook.

    Args:
        msg: The chat message that triggered the alert.
        text: Cleaned message text.
        count: Number of similar messages in the window.
        channel_name: Channel being monitored.
        alert_number: Sequential alert counter.
    """
    ctx = AlertContext(
        chatter_name=msg.display_name or msg.username,
        chatter_id=msg.user_id,
        text=text,
        count=count,
        channel_name=channel_name,
        window_minutes=_WINDOW_MINUTES,
        alert_number=alert_number,
        platform=msg.platform,
    )

    ts: str = datetime.now(tz=UTC).strftime(format="%H:%M:%S")
    plat_label: str = PLATFORM_LABEL.get(msg.platform, msg.platform)
    plat_colour: str = PLATFORM_COLOUR.get(msg.platform, "white")

    title = Text()
    title.append(text=f" {ts} UTC ", style="bold white on dark_red")
    title.append(text="  ")
    title.append(text=f"ALERT #{alert_number}", style="bold red")
    title.append(text=f"  [{plat_label}]", style=f"bold {plat_colour}")
    title.append(text="  -  ")
    title.append(text=ctx.chatter_name, style="bold magenta")
    title.append(text=f"  (id: {ctx.chatter_id})", style="dim")

    body = Text()
    body.append(text=f'"{text}"', style="italic yellow")
    body.append(
        text=f"\nSimilar message sent {count}x in the last {_WINDOW_MINUTES} min",
        style="dim",
    )
    if ctx.logs_url:
        body.append(text=f"\n{ctx.logs_url}", style="dim underline")

    console.print()
    console.print(Panel(renderable=body, title=title, border_style="red", expand=False))

    # Fire-and-forget webhook (errors are logged, never raised).
    asyncio.get_running_loop().create_task(
        coro=send_alert(ctx=ctx),
        name="Monitor:discord_webhook",
    )


# Statistics panel
def _format_stats_columns(stats: ChatStats) -> tuple[str, str, str]:
    """Build the aligned messages / bans / rates lines for the stats panel.

    Returns:
        tuple[str, str, str]: Tuple of ``(messages_line, bans_line, rates_line)``.
    """
    col1_m: str = f"{stats.total_messages:,} total"
    col2_m: str = f"{stats.unique_users:,} users"
    col1_b: str = f"{stats.ban_count} bans"
    col2_b: str = f"{stats.timeout_count} timeouts"
    col1_r: str = f"{stats.messages_per_second:.1f} msg/s"
    col2_r: str = f"{stats.messages_per_minute:.0f} msg/min"

    w1: int = max(len(col1_m), len(col1_b), len(col1_r))
    w2: int = max(len(col2_m), len(col2_b), len(col2_r))
    sep: str = "  |  "

    return (
        f"{col1_m:<{w1}}{sep}{col2_m:<{w2}}{sep}{stats.alert_count} alerts",
        f"{col1_b:<{w1}}{sep}{col2_b}",
        f"{col1_r:<{w1}}{sep}{col2_r:<{w2}}{sep}{stats.messages_per_hour:,.0f} msg/h",
    )


def print_stats(
    *,
    stats: ChatStats,
    tracked_users: int | None = None,
    platform: Platform | None = None,
) -> None:
    """Render a detailed session statistics panel.

    Args:
        stats: Session statistics tracker.
        tracked_users: Number of users in the detector (omit for platform-specific
        views).
        platform: If given, colour the panel for this platform.
    """
    spark: str = stats.sparkline()
    top: list[tuple[str, int]] = stats.top_chatters(n=5)
    top_str: str = ", ".join(f"{name}({c})" for name, c in top) if top else "-"
    messages_line, bans_line, rates_line = _format_stats_columns(stats=stats)

    tracked_line: str = (
        f"[bold]Tracked:[/bold]   {tracked_users} users\n"
        if tracked_users is not None
        else ""
    )

    if platform is not None:
        label: str = PLATFORM_LABEL.get(platform, platform)
        colour: str = PLATFORM_COLOUR.get(platform, "white")
        title: str = f"[{colour}]{label} Stats[/{colour}]"
        border: str = colour
    else:
        title = "[dim]Chat Stats[/dim]"
        border = "dim"

    # Kick does not expose moderation events — hide the bans line.
    # Rumble only reports message deletions — label accordingly.
    if platform == KICK:
        bans_section: str = ""
    elif platform == RUMBLE:
        mod_total: int = stats.ban_count + stats.timeout_count
        bans_section = f"[bold]Mod acts:[/bold]  {mod_total} deletions\n"
    else:
        bans_section = f"[bold]Bans:[/bold]      {bans_line}\n"

    console.print()
    console.print(
        Panel(
            renderable=(
                f"[bold]Duration:[/bold]  {stats.elapsed_str}\n"
                f"{tracked_line}"
                f"[bold]Messages:[/bold]  {messages_line}\n"
                f"{bans_section}"
                f"[bold]Rates:[/bold]     {rates_line}\n"
                f"[bold]Avg rate:[/bold]  "
                f"{stats.avg_messages_per_second:.2f} msg/s (session)\n"
                f"[bold]Top 5:[/bold]     {top_str}\n"
                f"[bold]msg/min:[/bold]   {spark}"
            ),
            title=title,
            border_style=border,
            expand=False,
        )
    )


# Startup panel
def print_ready(
    *,
    platforms: list[str],
    active: set[Platform],
    emote_count: int,
    has_twitch: bool,
    bot_count: int,
) -> None:
    """Render the startup information panel.

    Args:
        platforms: Pre-formatted Rich markup lines per platform.
        active: Set of active platform identifiers.
        emote_count: Total cached emote count.
        has_twitch: Whether Twitch emote caching is active.
        bot_count: Number of known bots being ignored.
    """
    platform_str: str = "\n            ".join(platforms)

    emote_info: str = (
        f"{emote_count:,} cached (Twitch + 7TV + FFZ + BTTV)"
        if has_twitch
        else "N/A (no Twitch channel)"
    )

    shortcuts: list[str] = ["[bold]s[/bold] All"]
    if "twitch" in active:
        shortcuts.append("[bold]t[/bold] Twitch")
    if "kick" in active:
        shortcuts.append("[bold]k[/bold] Kick")
    if "rumble" in active:
        shortcuts.append("[bold]r[/bold] Rumble")
    shortcuts_line: str = "  ".join(shortcuts)

    console.print()
    console.print(
        Panel(
            renderable=(
                f"[bold green]Monitor online![/bold green]\n\n"
                f"Platforms:  {platform_str}\n"
                f"Window:     {_WINDOW_MINUTES} minutes\n"
                f"Threshold:  {REPEAT_COUNT}x similar messages\n"
                f"Similarity: {_SIMILARITY_PCT}%\n"
                f"Emotes:     {emote_info}\n"
                f"Ignored:    Broadcaster, mods and {bot_count} known bots\n"
                f"[dim]Listening for messages... Ctrl+C to stop.\n"
                f"Stats: {shortcuts_line}[/dim]"
            ),
            title="[bold]2% Detector[/bold]",
            border_style="green",
            expand=False,
        )
    )


# Inline messages
def print_clearchat(event: ClearChatEvent) -> None:
    """Print a dim one-liner for a ban/timeout event.

    Args:
        event: The moderation event to display.
    """
    if not event.username:
        return
    if event.platform == RUMBLE:
        kind: str = "messages deleted by mod"
    else:
        kind = "banned" if event.permanent else f"timed-out ({event.duration}s)"
    plat: str = PLATFORM_LABEL.get(event.platform, event.platform)
    console.print(
        f"[dim]  \u26a1 [{plat}] {event.username} ({event.user_id}) {kind}[/dim]"
    )


def print_cleanup(*, removed: int, tracked: int) -> None:
    """Print a dim one-liner for a stale-user cleanup.

    Args:
        removed: Number of users purged.
        tracked: Number of users still tracked.
    """
    console.print(
        f"[dim]  Cleanup: {removed} inactive user(s) removed. Tracking {tracked} now."
        "[/dim]"
    )
