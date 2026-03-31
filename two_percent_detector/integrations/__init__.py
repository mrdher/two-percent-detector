"""External service integrations.

Currently contains one module for optional Discord webhook forwarding.
Set `discord.WEBHOOK_URL` to a webhook URL to enable; leave it empty (the default) to
run in terminal-only mode.

Modules:
- `discord`: Handles optional Discord webhook forwarding.
Contains `AlertContext`, a frozen dataclass that carries all data needed to build a
Discord embed (chatter name and ID, cleaned message text, repeat count, channel name,
detection window, alert number, and platform).
Its `logs_url` property generates a Supa.sh log-viewer link for Twitch alerts.
Also contains `send_alert`, an async function that posts the embed via
`niquests.AsyncSession`; errors are logged as warnings and never re-raised, so a Discord
outage cannot crash the monitor.
"""

from .discord import WEBHOOK_URL, AlertContext, send_alert

__all__ = [
    "WEBHOOK_URL",
    "AlertContext",
    "send_alert",
]
