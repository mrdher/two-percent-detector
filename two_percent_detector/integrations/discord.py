"""Discord webhook integration for forwarding alerts.

Sends spam-detection alerts to a Discord channel via a webhook URL.
The webhook is optional: if `WEBHOOK_URL` is empty the module is a no-op.

Uses `niquests.AsyncSession` to post JSON payloads without adding any extra
dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

from niquests import AsyncSession

from two_percent_detector.core.chat_types import KICK, PLATFORM_LABEL, RUMBLE, TWITCH

if TYPE_CHECKING:
    from logging import Logger

    from niquests.models import Response

    from two_percent_detector.core.chat_types import Platform


class _EmbedField(TypedDict):
    name: str
    value: str
    inline: bool


class _EmbedFooter(TypedDict):
    text: str


class _Embed(TypedDict):
    title: str
    description: str
    color: int
    fields: list[_EmbedField]
    footer: _EmbedFooter


class _WebhookPayload(TypedDict):
    embeds: list[_Embed]


logger: Logger = logging.getLogger(name=__name__)

# Paste your Discord webhook URL here.
# Leave empty to disable.
WEBHOOK_URL: Final[str] = ""

# Discord embed colours per platform (decimal).
_PLATFORM_EMBED_COLOUR: Final[dict[Platform, int]] = {
    TWITCH: 0x9146FF,
    KICK: 0x53FC18,
    RUMBLE: 0x85C742,
}
_FALLBACK_COLOUR: Final[int] = 0xED4245

_REQUEST_TIMEOUT: Final[int] = 10

# HTTP status codes indicating success.
_HTTP_OK: Final[int] = 200
_HTTP_NO_CONTENT: Final[int] = 204


@dataclass(slots=True, frozen=True, kw_only=True)
class AlertContext:
    """Data required to build a Discord alert embed.

    Attributes:
        chatter_name: Display/login name of the offending user.
        chatter_id: User ID of the offending user.
        text: Cleaned message text that triggered the alert.
        count: Number of similar messages detected in the window.
        channel_name: Channel being monitored.
        window_minutes: Rolling detection window in minutes.
        alert_number: Sequential alert counter for this session.
        platform: Source platform (`"twitch"`, `"kick"`, or `"rumble"`).
    """

    chatter_name: str
    chatter_id: str
    text: str
    count: int
    channel_name: str
    window_minutes: int
    alert_number: int
    platform: Platform = TWITCH

    @property
    def logs_url(self) -> str:
        """Supa.sh log viewer URL (Twitch only, empty for others)."""
        if self.platform != TWITCH:
            return ""
        return f"https://tv.supa.sh/logs?c={self.channel_name}&u={self.chatter_name}"

    @property
    def platform_label(self) -> str:
        """Human-readable platform name."""
        return PLATFORM_LABEL.get(self.platform, self.platform)


async def send_alert(ctx: AlertContext) -> None:
    """Post a spam alert to Discord as an embed.

    If `WEBHOOK_URL` is empty the call returns immediately without making any network
    request.

    Args:
        ctx: All data needed to build the alert embed.
    """
    if not WEBHOOK_URL:
        return

    fields: list[_EmbedField] = [
        _EmbedField(
            name="User ID",
            value=ctx.chatter_id,
            inline=True,
        ),
    ]
    if ctx.logs_url:
        fields.append(
            _EmbedField(
                name="Logs",
                value=f"[supa.sh]({ctx.logs_url})",
                inline=True,
            ),
        )

    colour: int = _PLATFORM_EMBED_COLOUR.get(
        ctx.platform,
        _FALLBACK_COLOUR,
    )
    description: str = (
        f'**"{ctx.text}"**\n\n'
        f"Similar message sent **{ctx.count}x** "
        f"in the last **{ctx.window_minutes} min**"
    )
    footer_text: str = f"{ctx.platform_label}  \u2022  #{ctx.channel_name}"

    payload: _WebhookPayload = {
        "embeds": [
            _Embed(
                title=(f"Alert #{ctx.alert_number}  \u2014  {ctx.chatter_name}"),
                description=description,
                color=colour,
                fields=fields,
                footer=_EmbedFooter(text=footer_text),
            ),
        ],
    }

    try:
        async with AsyncSession(
            timeout=_REQUEST_TIMEOUT,
            happy_eyeballs=True,
        ) as session:
            resp: Response = await session.post(WEBHOOK_URL, json=payload)
            if resp.status_code not in {_HTTP_OK, _HTTP_NO_CONTENT}:
                logger.warning(
                    "Discord webhook returned %s: %s",
                    resp.status_code,
                    resp.text,
                )
    except OSError:
        logger.warning("Failed to send Discord webhook", exc_info=True)
