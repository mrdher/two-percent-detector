"""External service integrations.

Optional Discord webhook forwarding for spam-detection alerts.
Set `WEBHOOK_URL` to enable; leave it empty (the default) for terminal-only mode.
"""

from two_percent_detector.integrations.discord import (
    WEBHOOK_URL,
    AlertContext,
    send_alert,
)

__all__ = [
    "WEBHOOK_URL",
    "AlertContext",
    "send_alert",
]
