"""Rich terminal rendering for the 2% Detector.

Centralises all console output so that the rest of the codebase never imports Rich
directly.
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
