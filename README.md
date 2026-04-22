# Antigravity Telegram Bridge 🤖🔗

> Control your [Antigravity IDE](https://antigravity.dev) remotely via Telegram. Send text or voice messages — the bridge injects them into your IDE chat and streams AI responses back.

```
Phone (Telegram) → Bot → bridge.py → CDP WebSocket → Antigravity Chat
                                    ← DOM Polling   ← AI Response
```

## ✨ Features

- **Text Messages** → forwarded to Antigravity, AI response sent back
- **Voice Messages** → local Whisper transcription → forwarded to Antigravity
- **Smart Launcher** → actively monitors the IDE and injects the CDP port automatically if missing
- **Interactive Approvals** → native support for English/Chinese inline buttons (Allow/Run/允许/运行) directly in Telegram
- **Progress streaming** → shows live thinking status in Telegram
- **Long response splitting** → handles multi-message AI responses

## 🚀 One-Command Install

```bash
git clone https://github.com/v4-hub/antigravity-telegram-bridge.git
cd antigravity-telegram-bridge
bash install.sh
```

The installer will:
1. Create a Python virtual environment
2. Install all dependencies (including `faster-whisper` for voice)
3. Install `ffmpeg` if missing
4. Generate a `.env` config file for you to fill in

## ⚙️ Setup

### 1. Create a Telegram Bot

1. Open Telegram, search for `@BotFather`
2. Send `/newbot`, follow the prompts
3. Copy the **bot token** (looks like `123456:ABC-DEF...`)

### 2. Get Your Telegram User ID

1. Search for `@userinfobot` on Telegram
2. Start a chat, it will show your numeric **User ID**

### 3. Configure

Edit the `.env` file:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USER_IDS=your_user_id_here
```

### 4. Setup Background Service (Recommended)

To ensure the bridge and IDE are always connected, use the smart launcher:

**For macOS (One-click):**
```bash
bash install_mac_service.sh
```

**For Linux / Manual Start:**
```bash
bash run_bridge.sh
```
*(Note: `run_bridge.sh` will automatically open Antigravity with the correct `--remote-debugging-port=9233` flag)*

## 📱 Usage

| Action | What Happens |
|--------|-------------|
| Send text message | Forwarded to Antigravity AI |
| Send voice message | Transcribed locally → forwarded |
| Click Inline Buttons | Proxies clicks to IDE approval dialogs (e.g., Run/Allow/运行) |
| `/status` | Check CDP connection status |
| `/reconnect` | Reconnect to Antigravity |
| `/start` or `/help` | Show help |

## 🕳️ Troubleshooting & Pitfalls Avoided

- **Pitfall 1: Clicking the Dock Icon breaks the connection**
  *Issue:* Normal launches (via Launchpad/Dock) don't include the `--remote-debugging-port` flag, leaving the bot disconnected.
  *Solution:* The new `run_bridge.sh` acts as an active monitor. If it detects the IDE running without the port, it gracefully restarts it and injects the port automatically.
- **Pitfall 2: Cannot click "Allow" / "Run" via Telegram**
  *Issue:* Previous versions only recognized English buttons ("Allow", "Deny").
  *Solution:* We've added comprehensive regex patterns for Chinese environments (`允许`, `运行`, `始终允许`, `拒绝`). Inline Telegram buttons will seamlessly proxy your clicks to the IDE.
- **Pitfall 3: Background memory leaks from orphaned processes**
  *Issue:* Restarting the bridge sometimes orphaned IDE renderer processes.
  *Solution:* `bridge.py` now includes a cgroup-based cleanup mechanism that kills ghost instances before reconnecting.

## 🏗️ How It Works

The bridge connects to Antigravity's Electron window via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) (CDP):

1. **Discovery** — Connects to port 9233 for the Antigravity workbench page
2. **Injection** — Focuses the chat input (`div[role="textbox"]`), types the message via `Input.insertText`, presses Enter
3. **Monitoring** — Polls the DOM for `.rendered-markdown` content every 2 seconds
4. **Completion** — Detects when the stop button disappears and text stabilizes
5. **Response** — Sends the AI response back to Telegram

Voice messages are transcribed locally using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (no cloud API needed).

## 🔄 Auto-Restart with Systemd (Recommended)

Without systemd, the Antigravity CDP instance and the bridge will stop if the system reboots, the process crashes, or the terminal closes. Setting up systemd user services ensures **everything auto-starts and auto-restarts**.

### Install the Services

```bash
mkdir -p ~/.config/systemd/user

# Service 1: Keep Antigravity CDP instance alive
cat > ~/.config/systemd/user/antigravity-cdp.service << 'EOF'
[Unit]
Description=Antigravity IDE with CDP (for Telegram Bridge)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/opt/antigravity/antigravity --user-data-dir=%h/.config/Antigravity-CDP --remote-debugging-port=9233
Restart=always
RestartSec=10
# Adjust these to match your display environment (run `echo $DISPLAY` to check)
Environment=DISPLAY=:1
Environment=WAYLAND_DISPLAY=wayland-1
Environment=XDG_RUNTIME_DIR=/run/user/%U
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus

[Install]
WantedBy=default.target
EOF

# Service 2: Keep the Telegram Bridge alive
cat > ~/.config/systemd/user/telegram-bridge.service << 'EOF'
[Unit]
Description=Antigravity Telegram Bridge
After=antigravity-cdp.service
Wants=antigravity-cdp.service

[Service]
Type=simple
WorkingDirectory=%h/work/telegram-bridge
ExecStartPre=/bin/sleep 15
ExecStart=%h/work/telegram-bridge/venv/bin/python3 %h/work/telegram-bridge/bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
```

> **Note**: Adjust the `DISPLAY` and `WAYLAND_DISPLAY` values if your setup differs. Run `echo $DISPLAY` in a terminal to check.

### Enable and Start

```bash
systemctl --user daemon-reload
systemctl --user enable antigravity-cdp.service telegram-bridge.service
systemctl --user start antigravity-cdp.service telegram-bridge.service

# Optional: allow services to run even after logout
sudo loginctl enable-linger $USER
```

### Management Commands

```bash
# Check status
systemctl --user status telegram-bridge
systemctl --user status antigravity-cdp

# View live logs
journalctl --user -u telegram-bridge -f

# Restart
systemctl --user restart telegram-bridge

# Stop everything
systemctl --user stop telegram-bridge antigravity-cdp
```

## 📋 Requirements

- Python 3.10+
- Antigravity IDE (Electron-based)
- ffmpeg (for voice message support)
- ~150MB disk space (for Whisper model, downloaded on first voice use)

## 🤝 Contributing

Contributions welcome! Some ideas:

- [ ] Image/screenshot support  
- [ ] Multi-workspace switching
- [ ] Inline keyboard for model/mode selection
- [ ] Auto-approval of tool calls
- [ ] File attachment support
- [x] ~~Systemd service for auto-start~~

## 📄 License

MIT License — see [LICENSE](LICENSE)
