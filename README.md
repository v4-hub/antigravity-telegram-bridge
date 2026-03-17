# Antigravity Telegram Bridge 🤖🔗

> Control your [Antigravity IDE](https://antigravity.dev) remotely via Telegram. Send text or voice messages — the bridge injects them into your IDE chat and streams AI responses back.

```
Phone (Telegram) → Bot → bridge.py → CDP WebSocket → Antigravity Chat
                                    ← DOM Polling   ← AI Response
```

## ✨ Features

- **Text Messages** → forwarded to Antigravity, AI response sent back
- **Voice Messages** → local Whisper transcription → forwarded to Antigravity
- **Auto-connect** → discovers and connects to Antigravity automatically
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

### 4. Launch Antigravity with CDP

Start Antigravity with the Chrome DevTools Protocol port enabled:

```bash
antigravity --remote-debugging-port=9222
```

### 5. Start the Bridge

```bash
bash start.sh
```

## 📱 Usage

| Action | What Happens |
|--------|-------------|
| Send text message | Forwarded to Antigravity AI |
| Send voice message | Transcribed locally → forwarded |
| `/status` | Check CDP connection status |
| `/reconnect` | Reconnect to Antigravity |
| `/start` or `/help` | Show help |

## 🏗️ How It Works

The bridge connects to Antigravity's Electron window via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) (CDP):

1. **Discovery** — Scans ports 9222-9666 for the Antigravity workbench page
2. **Injection** — Focuses the chat input (`div[role="textbox"]`), types the message via `Input.insertText`, presses Enter
3. **Monitoring** — Polls the DOM for `.rendered-markdown` content every 2 seconds
4. **Completion** — Detects when the stop button disappears and text stabilizes
5. **Response** — Sends the AI response back to Telegram

Voice messages are transcribed locally using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (no cloud API needed).

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
- [ ] Systemd service for auto-start

## 📄 License

MIT License — see [LICENSE](LICENSE)
