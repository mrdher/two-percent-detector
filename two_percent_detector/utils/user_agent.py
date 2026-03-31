"""Dynamic Chrome User-Agent string.

Fetches the latest stable Chrome version from the Chromium release dashboard and builds
a realistic User-Agent header.
The result is cached at module level so the network call happens at most once per
process.

If the API call fails the module falls back to a hardcoded recent version so the monitor
can still start.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import TYPE_CHECKING, Final

from niquests import Session

if TYPE_CHECKING:
    from logging import Logger

    from niquests.models import Response

    from two_percent_detector.core.chat_types import JsonValue

logger: Logger = logging.getLogger(name=__name__)

# Chromium release dashboard API (returns a JSON array).
_RELEASES_URL: Final[str] = (
    "https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows&num=1"
)

_REQUEST_TIMEOUT: Final[int] = 10

# Fallback version used when the API is unreachable.
_FALLBACK_VERSION: Final[str] = "146.0.7680.166"

# Template matches a standard Chrome-on-Windows UA string.
_UA_TEMPLATE: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{version} Safari/537.36"
)


@functools.cache
def chrome_user_agent() -> str:
    """Return a Chrome User-Agent string with the latest stable version.

    The first call fetches the version from the Chromium release dashboard.
    Subsequent calls return the cached result.

    Returns:
        str: A realistic Chrome User-Agent header value.
    """
    version: str = _FALLBACK_VERSION
    try:
        with Session() as session:
            resp: Response = session.get(url=_RELEASES_URL, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data: JsonValue = resp.json()
            if isinstance(data, list) and data:
                first: JsonValue = data[0]
                if isinstance(first, dict):
                    ver: JsonValue = first.get("version")
                    if isinstance(ver, str) and ver:
                        version = ver
    except OSError, json.JSONDecodeError:
        logger.warning(
            "Could not fetch Chrome version; using fallback %s", _FALLBACK_VERSION
        )

    return _UA_TEMPLATE.format(version=version)
