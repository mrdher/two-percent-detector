"""Rich terminal rendering for the 2% Detector.

A single module, `terminal`, owns all console output so that the rest of the codebase
never imports Rich directly.
Functions accept plain data arguments and delegate all formatting decisions here.

Attributes:
- `console`: The shared `rich.console.Console` instance (`highlight=False`).
Import this whenever a module needs to print something outside the standard
alert/stats/banner flow.

Functions:
- `print_alert`: Renders the red alert panel and fires the Discord webhook as a
fire-and-forget task.
- `print_stats`: Renders the session statistics panel, optionally coloured per platform.
- `print_ready`: Renders the green startup banner shown once all platforms have
connected.
- `print_clearchat`: Prints a dim one-liner for every ban or timeout event.
- `print_cleanup`: Prints a dim one-liner after a periodic stale-user purge.
"""

from two_percent_detector.ui.terminal import (
    console,
    print_alert,
    print_cleanup,
    print_clearchat,
    print_ready,
    print_stats,
)

__all__ = [
    "console",
    "print_alert",
    "print_cleanup",
    "print_clearchat",
    "print_ready",
    "print_stats",
]
