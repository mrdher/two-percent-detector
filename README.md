# 2% Detector

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A multi-platform chat monitor that watches **Twitch**, **Kick**, and **Rumble** live-stream chats and alerts you in the terminal whenever someone sends the same message repeatedly — a common pattern in spam, botting, and coordinated harassment.

**The bot never writes anything to chat.** It only reads and alerts you locally.

**No accounts, OAuth tokens, or API keys required.** All connections are anonymous and read-only.

---

## What it does

- Watches every message across one or more platforms in real time
- If the same user sends a very similar message **3 times within 15 minutes**, an alert appears on the screen
- "Similar" means 82% alike — it catches things like:
  - `buy followers cheap` → `BUY FOLLOWERS CHEAP` → `buy followers cheap!!`
  - Minor typos, extra punctuation, all caps vs lowercase
- Messages from other users in between **do not reset the count**
- Emotes and emoji are stripped — messages made up only of emotes won't trigger an alert
  - Twitch: native emotes (via IRC tags), 7TV, FFZ, BTTV
  - All platforms: emoji characters
- Bans and timeouts are detected — if a moderator already acted, the alert is suppressed
- Alerts can optionally be forwarded to a **Discord channel** via webhook
- Per-platform and global **session statistics** with keyboard shortcuts

### Supported platforms

| Platform | Transport                | Auth |
| -------- | ------------------------ | ---- |
| Twitch   | Anonymous IRC WebSocket  | None |
| Kick     | Pusher WebSocket         | None |
| Rumble   | Server-Sent Events (SSE) | None |

---

## Requirements

- A computer running **Windows, macOS, or Linux**
- An internet connection

You do **not** need any accounts, API keys, or to install Python manually.

---

## Setup — step by step

### Step 1 — Install uv

`uv` is the tool that manages Python and all dependencies for you.

**Windows** — open PowerShell and run:

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux** — open Terminal and run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen your terminal after installation.

---

### Step 2 — Install Python 3.14

```bash
uv python install 3.14
```

This downloads and installs Python automatically.

---

### Step 3 — Download the project files

Download all files in this project into a folder on your computer (e.g. `C:\2pct-detector` on Windows or `~/2pct-detector` on Mac/Linux).

Open a terminal and navigate to that folder:

```bash
cd C:\2pct-detector
```

---

### Step 4 — Install dependencies

```bash
uv sync
```

This creates an isolated environment and installs everything the bot needs. Only needed once.

---

### Step 5 — Start the monitor

Monitor one or more platforms at the same time:

```bash
# Twitch only (pass a channel name)
uv run monitor --twitch zackrawrr

# Twitch only (use saved default — omit the value)
uv run monitor --twitch

# Twitch + Kick
uv run monitor --twitch zackrawrr --kick asmongold

# Twitch + Kick using saved defaults
uv run monitor --twitch --kick

# All three
uv run monitor --twitch zackrawrr --kick asmongold --rumble Asmongold
```

> **Saved defaults** — edit the `DEFAULT_*` constants near the top of
> `two_percent_detector/config.py` to set your own channel names. When a platform flag is
> passed without a value (e.g. `--twitch` instead of `--twitch zackrawrr`),
> the saved default is used.

You'll see a green panel confirming the monitor is online and watching chat.

---

## Keyboard shortcuts

While the monitor is running, type a letter and press **Enter**:

| Key | Action                                                         |
| --- | -------------------------------------------------------------- |
| `s` | Show **global** session statistics (all platforms combined)    |
| `t` | Show **Twitch** statistics (only if Twitch is being monitored) |
| `k` | Show **Kick** statistics (only if Kick is being monitored)     |
| `r` | Show **Rumble** statistics (only if Rumble is being monitored) |

Platform shortcuts only work when the corresponding platform is active.

---

## What an alert looks like

```text
╭─  20:51:19 UTC   ALERT #1  [Twitch]  -  MrDher  (id: 56904146) ─╮
│ "Baldo"                                                         │
│ Similar message sent 3x in the last 10 min                      │
│ https://tv.supa.sh/logs?c=zackrawrr&u=MrDher                    │
╰─────────────────────────────────────────────────────────────────╯
```

---

## Stopping the monitor

Press **Ctrl + C** in the terminal window.

---

## Restarting after a reboot

No tokens or state files to worry about. Just run:

```bash
uv run monitor --twitch
```

---

## Adjusting sensitivity

If you're getting too many alerts or missing spam, open `two_percent_detector/core/detector.py` in a text editor and change these values near the top:

| Setting                | Default | Meaning                                                    |
| ---------------------- | ------- | ---------------------------------------------------------- |
| `WINDOW_SECONDS`       | `600`   | Time window in seconds (600 = 10 min)                      |
| `REPEAT_COUNT`         | `3`     | How many similar messages before alerting                  |
| `SIMILARITY_THRESHOLD` | `0.82`  | How similar messages must be (0.0 = anything, 1.0 = exact) |

---

## Discord alerts (optional)

You can forward every alert to a Discord channel so you (and your mods) see them without watching the terminal.

### 1. Create a webhook in Discord

1. Open your Discord server settings → **Integrations** → **Webhooks**.
2. Click **New Webhook**, pick a channel, and copy the webhook URL.

### 2. Paste the URL

Open `two_percent_detector/integrations/discord.py` and replace the empty string on the `WEBHOOK_URL` line:

```python
WEBHOOK_URL: Final[str] = "https://discord.com/api/webhooks/..."
```

### 3. Restart

```bash
uv run monitor --twitch
```

Alerts will now appear both in the terminal and in your Discord channel as embeds. If the webhook URL is left empty, no Discord requests are made.

---

## File overview

All source code lives inside the `two_percent_detector/` package.

| File / Folder             | Purpose                                                                     |
| ------------------------- | --------------------------------------------------------------------------- |
| `monitor.py`              | Main entry point — run with `uv run monitor`                                |
| `__main__.py`             | Enables `python -m two_percent_detector`                                    |
| `config.py`               | Channel defaults and known-bot whitelist                                    |
| **core/**                 |                                                                             |
| `core/chat_types.py`      | Shared types: `Platform`, `ChatMessage`, `ClearChatEvent`, `PlatformClient` |
| `core/detector.py`        | Spam detection logic (rolling window + similarity)                          |
| `core/stats.py`           | Session statistics tracking and sparkline rendering                         |
| **data/**                 |                                                                             |
| `data/common_words.json`  | Filler words ignored during spam detection                                  |
| `data/known_bots.json`    | Bot usernames that are always ignored                                       |
| **integrations/**         |                                                                             |
| `integrations/discord.py` | Optional Discord webhook integration                                        |
| **platforms/**            |                                                                             |
| `platforms/twitch.py`     | Twitch IRC WebSocket client + channel ID lookup                             |
| `platforms/kick.py`       | Kick Pusher WebSocket client + channel ID lookup                            |
| `platforms/rumble.py`     | Rumble SSE client + channel ID lookup                                       |
| **ui/**                   |                                                                             |
| `ui/terminal.py`          | Rich terminal rendering (alerts, stats panels, banners)                     |
| **utils/**                |                                                                             |
| `utils/emotes.py`         | Downloads and caches emote lists (7TV, FFZ, BTTV)                           |
| `utils/user_agent.py`     | Chrome User-Agent fetcher for HTTP requests                                 |

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Credits

Created by [Dher](https://github.com/mrdher).
