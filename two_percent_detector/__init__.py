"""2% Detector — multi-platform chat monitor (read-only).

Watches Twitch, Kick, and Rumble live-stream chats and raises a terminal alert
whenever a user sends the same (or substantially similar) message multiple times
within a rolling detection window.

The top-level `monitor` module is the main entry point; run it with:
    >>> uv run monitor --twitch zackrawrr --kick asmongold --rumble Asmongold

Modules:
- `core`: Shared types (`ChatMessage`, `PlatformClient`), spam detection logic, and
session statistics.
- `platforms`: Anonymous, read-only chat clients for each platform (Twitch IRC
WebSocket, Kick Pusher WebSocket, Rumble SSE).
- `utils`: Emote-stripping utilities and the dynamic Chrome User-Agent helper.
- `ui`: Rich terminal rendering (alert panels, stats, startup banner).
- `integrations`: Optional Discord webhook forwarding.
"""
