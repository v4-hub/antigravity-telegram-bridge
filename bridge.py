#!/usr/bin/env python3
"""
Telegram Bridge for Antigravity IDE
Connects a Telegram Bot to Antigravity's chat via Chrome DevTools Protocol (CDP).

Usage:
  1. Launch Antigravity with CDP:  antigravity --remote-debugging-port=9222
  2. Run this script:              python bridge.py

Environment variables:
  TELEGRAM_BOT_TOKEN   - Your Telegram bot token from @BotFather
  ALLOWED_USER_IDS     - Comma-separated Telegram user IDs (security whitelist)
"""

import os
import sys
import json
import asyncio
import logging
import signal
import html
import tempfile
import urllib.request
from typing import Optional

import websockets
from telegram import Update
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
)

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env loading is optional

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    print("❌ TELEGRAM_BOT_TOKEN not set. Please configure your .env file.")
    print("   See .env.example for reference.")
    sys.exit(1)

_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = set(
    int(x.strip()) for x in _allowed.split(",") if x.strip()
)
CDP_PORTS = [9222, 9223, 9333, 9444, 9555, 9666]
POLL_INTERVAL = 2.0        # seconds between DOM polls
STABLE_ROUNDS = 3          # how many unchanged polls before we declare "complete"
MAX_WAIT = 600             # max seconds to wait for a response
MAX_MSG_LEN = 4096         # Telegram message length limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

# ── CDP Helper ────────────────────────────────────────────────────────────────

class CdpConnection:
    """Minimal Chrome DevTools Protocol client over WebSocket."""

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: Optional[asyncio.Task] = None
        self.contexts: list[dict] = []

    async def discover_and_connect(self) -> bool:
        """Scan CDP ports, find the Antigravity workbench page, connect."""
        for port in CDP_PORTS:
            try:
                url = f"http://127.0.0.1:{port}/json/list"
                req = urllib.request.Request(url, headers={"User-Agent": "bridge"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    pages = json.loads(resp.read())
            except Exception:
                continue

            # Find the main workbench page (not Launchpad)
            for page in pages:
                if page.get("type") != "page":
                    continue
                ws_url = page.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                title = page.get("title", "")
                page_url = page.get("url", "")
                if "Launchpad" in title or "workbench-jetski-agent" in page_url:
                    continue
                if "workbench" in page_url or "Antigravity" in title:
                    log.info(f"Found target: \"{title}\" on port {port}")
                    try:
                        await self._connect_ws(ws_url)
                        return True
                    except Exception as e:
                        log.warning(f"Failed to connect to {ws_url}: {e}")

        log.error("No Antigravity CDP target found on any port")
        return False

    async def _connect_ws(self, url: str):
        """Establish WebSocket connection and start receiver loop."""
        if self.ws:
            await self.close()
        self.ws = await websockets.connect(
            url,
            max_size=50 * 1024 * 1024,
            open_timeout=10,
            close_timeout=5,
        )
        self._recv_task = asyncio.create_task(self._receiver())
        # Enable Runtime to get execution contexts
        await self.call("Runtime.enable", {})
        log.info("CDP WebSocket connected")

    async def _receiver(self):
        """Background task that reads CDP messages."""
        try:
            while True:
                try:
                    raw = await self.ws.recv()
                except Exception:
                    break
                data = json.loads(raw)
                # Handle RPC responses
                if "id" in data and data["id"] in self._pending:
                    fut = self._pending.pop(data["id"])
                    if not fut.done():
                        if "error" in data:
                            fut.set_exception(RuntimeError(data["error"].get("message", str(data["error"]))))
                        else:
                            fut.set_result(data.get("result"))
                # Track execution contexts
                if data.get("method") == "Runtime.executionContextCreated":
                    self.contexts.append(data["params"]["context"])
                elif data.get("method") == "Runtime.executionContextDestroyed":
                    ctx_id = data["params"].get("executionContextId")
                    self.contexts = [c for c in self.contexts if c.get("id") != ctx_id]
        except Exception as e:
            log.warning(f"CDP WebSocket receiver ended: {e}")

    async def call(self, method: str, params: dict = None, timeout: float = 30) -> dict:
        """Send a CDP command and wait for the response."""
        if not self.ws or not self.connected:
            raise ConnectionError("CDP not connected")
        self._id += 1
        msg_id = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP call {method} timed out")

    async def evaluate(self, expression: str, context_id: int = None) -> any:
        """Evaluate JavaScript in the browser and return the result value."""
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        if context_id is not None:
            params["contextId"] = context_id
        result = await self.call("Runtime.evaluate", params)
        if result and "result" in result:
            if result["result"].get("type") == "undefined":
                return None
            return result["result"].get("value")
        return None

    @property
    def connected(self) -> bool:
        if self.ws is None:
            return False
        try:
            # websockets v16: check state
            from websockets.protocol import State
            return self.ws.protocol.state == State.OPEN
        except Exception:
            return self.ws is not None

    async def ensure_connected(self) -> bool:
        """Auto-reconnect if WebSocket is dead."""
        if self.connected:
            return True
        log.info("CDP connection lost, attempting to reconnect...")
        await self.close()
        for attempt in range(3):
            ok = await self.discover_and_connect()
            if ok:
                log.info("✅ CDP reconnected!")
                return True
            log.warning(f"Reconnect attempt {attempt + 1}/3 failed, retrying in 5s...")
            await asyncio.sleep(5)
        log.error("❌ Failed to reconnect after 3 attempts")
        return False

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.contexts.clear()


# ── Antigravity Interaction ───────────────────────────────────────────────────

class AntigravityBridge:
    """High-level interface for injecting messages and reading responses."""

    CHAT_INPUT_SELECTOR = 'div[role="textbox"]:not(.xterm-helper-textarea)'

    RESPONSE_SCRIPT = r"""
    (() => {
        const panel = document.querySelector('.antigravity-agent-side-panel') || document;
        const selectors = ['.rendered-markdown', '.leading-relaxed.select-text'];
        let lastNode = null;
        for (const sel of selectors) {
            const nodes = panel.querySelectorAll(sel);
            if (nodes.length > 0) lastNode = nodes[nodes.length - 1];
            if (lastNode) break;
        }
        if (!lastNode) return '';
        // Skip if inside a <details> (thinking block)
        if (lastNode.closest('details')) return '';
        return (lastNode.innerText || lastNode.textContent || '').trim();
    })()
    """

    STOP_BUTTON_SCRIPT = r"""
    (() => {
        const panel = document.querySelector('.antigravity-agent-side-panel') || document;
        // Check for cancel/stop button
        const cancelBtn = panel.querySelector('[data-tooltip-id="input-send-button-cancel-tooltip"]');
        if (cancelBtn) return true;
        // Check for any button with stop-like text
        const buttons = panel.querySelectorAll('button, [role="button"]');
        for (const btn of buttons) {
            const text = (btn.textContent || '').toLowerCase().trim();
            if (/^(stop|stop generating|stop response)$/.test(text)) return true;
        }
        return false;
    })()
    """

    def __init__(self, cdp: CdpConnection):
        self.cdp = cdp
        self._lock = asyncio.Lock()  # prevent concurrent message injection

    async def _find_cascade_context(self) -> Optional[int]:
        """Find the cascade-panel execution context for DOM operations."""
        for ctx in self.cdp.contexts:
            if "cascade-panel" in ctx.get("url", ""):
                return ctx["id"]
        # Fallback: try each context
        for ctx in self.cdp.contexts:
            try:
                result = await self.cdp.evaluate(
                    f'!!document.querySelector("{self.CHAT_INPUT_SELECTOR}")',
                    ctx["id"]
                )
                if result:
                    return ctx["id"]
            except Exception:
                continue
        return None

    async def inject_message(self, text: str) -> bool:
        """Type a message into Antigravity's chat and press Enter."""
        # Find the right context
        ctx_id = await self._find_cascade_context()

        # Focus the chat input
        focus_script = f"""
        (() => {{
            const editors = Array.from(document.querySelectorAll('{self.CHAT_INPUT_SELECTOR}'));
            const visible = editors.filter(el => el.offsetParent !== null);
            const editor = visible[visible.length - 1];
            if (!editor) return false;
            editor.focus();
            return true;
        }})()
        """
        focused = await self.cdp.evaluate(focus_script, ctx_id)
        if not focused:
            log.error("Could not find/focus chat input")
            return False

        # Clear existing text: Ctrl+A then Backspace
        await self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA",
            "modifiers": 2,  # Ctrl on Linux
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
        })
        await self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "modifiers": 2,
            "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
        })
        await self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Backspace", "code": "Backspace",
            "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
        })
        await self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Backspace", "code": "Backspace",
            "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8,
        })
        await asyncio.sleep(0.1)

        # Insert the text
        await self.cdp.call("Input.insertText", {"text": text})
        await asyncio.sleep(0.2)

        # Press Enter to send
        await self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        })
        await self.cdp.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        })

        log.info(f"Injected message ({len(text)} chars)")
        return True

    async def wait_for_response(self, progress_callback=None) -> str:
        """Poll the DOM until the AI response stabilizes."""
        ctx_id = await self._find_cascade_context()
        prev_text = ""
        stable_count = 0
        elapsed = 0

        # Wait a moment for the AI to start generating
        await asyncio.sleep(2)

        while elapsed < MAX_WAIT:
            try:
                # Check if still generating (stop button visible)
                is_generating = await self.cdp.evaluate(self.STOP_BUTTON_SCRIPT, ctx_id)

                # Extract latest response text
                current_text = await self.cdp.evaluate(self.RESPONSE_SCRIPT, ctx_id) or ""

                if current_text and current_text == prev_text:
                    if not is_generating:
                        stable_count += 1
                    else:
                        stable_count = 0  # still generating, reset
                else:
                    stable_count = 0
                    if current_text:
                        prev_text = current_text

                # Send progress updates
                if progress_callback and is_generating and elapsed % 10 < POLL_INTERVAL:
                    snippet = current_text[-200:] if current_text else "..."
                    await progress_callback(f"🧠 Thinking... ({int(elapsed)}s)\n\n...{snippet}")

                if stable_count >= STABLE_ROUNDS:
                    log.info(f"Response stabilized after {int(elapsed)}s ({len(prev_text)} chars)")
                    return prev_text

            except Exception as e:
                log.warning(f"Polling error: {e}")

            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        log.warning("Response wait timed out")
        return prev_text or "(Response timed out)"

    async def send_and_receive(self, text: str, progress_callback=None) -> str:
        """Full cycle: inject message → wait for response → return text."""
        async with self._lock:
            # Auto-reconnect if CDP connection dropped
            if not await self.cdp.ensure_connected():
                return "❌ Cannot connect to Antigravity (CDP not available)"
            if not await self.inject_message(text):
                return "❌ Failed to inject message into Antigravity"
            return await self.wait_for_response(progress_callback)


# ── Telegram Bot ──────────────────────────────────────────────────────────────

cdp = CdpConnection()
bridge: Optional[AntigravityBridge] = None


def authorized(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "🤖 *Antigravity Telegram Bridge*\n\n"
        "Send me any message and I'll forward it to Antigravity.\n\n"
        "Commands:\n"
        "/status — Check CDP connection\n"
        "/reconnect — Reconnect to Antigravity\n",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    status = "🟢 Connected" if cdp.connected else "🔴 Disconnected"
    ctx_count = len(cdp.contexts)
    await update.message.reply_text(
        f"🔧 *Status*\n\nCDP: {status}\nContexts: {ctx_count}",
        parse_mode="Markdown",
    )


async def cmd_reconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    global bridge
    await update.message.reply_text("🔄 Reconnecting...")
    await cdp.close()
    ok = await cdp.discover_and_connect()
    if ok:
        bridge = AntigravityBridge(cdp)
        await update.message.reply_text("✅ Reconnected to Antigravity!")
    else:
        await update.message.reply_text("❌ Failed to connect. Is Antigravity running with --remote-debugging-port=9222?")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not update.message or not update.message.text:
        return

    global bridge
    user_text = update.message.text.strip()
    if not user_text:
        return

    # Auto-connect if needed
    if not cdp.connected:
        status_msg = await update.message.reply_text("🔄 Connecting to Antigravity...")
        ok = await cdp.discover_and_connect()
        if not ok:
            await status_msg.edit_text("❌ Cannot connect to Antigravity.\nMake sure it's running with `--remote-debugging-port=9222`")
            return
        bridge = AntigravityBridge(cdp)
        await status_msg.edit_text("✅ Connected!")

    if not bridge:
        bridge = AntigravityBridge(cdp)

    # Send "thinking" indicator
    thinking_msg = await update.message.reply_text("🧠 Sending to Antigravity...")

    # Progress callback to update the thinking message
    last_progress_text = ""
    async def progress_cb(text: str):
        nonlocal last_progress_text
        if text != last_progress_text:
            last_progress_text = text
            try:
                safe_text = text[:MAX_MSG_LEN]
                await thinking_msg.edit_text(safe_text)
            except Exception:
                pass

    # Send and receive
    try:
        response = await bridge.send_and_receive(user_text, progress_cb)
    except Exception as e:
        log.error(f"Bridge error: {e}")
        await thinking_msg.edit_text(f"❌ Error: {e}")
        return

    if not response:
        await thinking_msg.edit_text("⚠️ No response received")
        return

    # Send the response (split if needed)
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    # Split long messages
    chunks = []
    while response:
        if len(response) <= MAX_MSG_LEN:
            chunks.append(response)
            break
        # Find a good split point
        split_at = response.rfind("\n", 0, MAX_MSG_LEN)
        if split_at < MAX_MSG_LEN // 2:
            split_at = MAX_MSG_LEN
        chunks.append(response[:split_at])
        response = response[split_at:].lstrip()

    for chunk in chunks:
        try:
            await update.message.reply_text(chunk)
        except Exception as e:
            log.error(f"Failed to send chunk: {e}")

# ── Speech to Text (Local Whisper) ────────────────────────────────────────────

whisper_model: Optional[WhisperModel] = None

def get_whisper_model() -> WhisperModel:
    global whisper_model
    if whisper_model is None:
        log.info("Loading faster-whisper model (small) on GPU...")
        # Using "small" model on GPU with float16 for better accuracy & speed
        whisper_model = WhisperModel("small", device="cuda", compute_type="float16")
        log.info("Local Whisper model loaded successfully.")
    return whisper_model

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    if not update.message or not update.message.voice:
        return
        
    status_msg = await update.message.reply_text("🎧 Downloading voice message...")
    
    try:
        # 1. Download the voice file
        voice_file = await update.message.voice.get_file()
        
        # Telegram sends voice as OGG OPUS
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
            temp_path = temp_audio.name
            
        await voice_file.download_to_drive(temp_path)
        
        # 2. Transcribe with Local Whisper
        await status_msg.edit_text("🗣️ Transcribing locally (faster-whisper)...")
        
        def transcribe():
            model = get_whisper_model()
            segments, info = model.transcribe(temp_path, beam_size=5)
            text = "".join([segment.text for segment in segments])
            return text.strip()

        user_text = await asyncio.get_event_loop().run_in_executor(None, transcribe)
        
        # Clean up audio file
        try:
            os.remove(temp_path)
        except Exception:
            pass
            
        if not user_text.strip():
            await status_msg.edit_text("⚠️ Could not transcribe any text from the audio.")
            return
            
        # Append transcription flag and call the main handler logic
        await status_msg.edit_text(f"🎤 *Transcribed:* _{user_text}_\n\n🔄 Connecting to Antigravity...", parse_mode="Markdown")
        
        # Proceed to send to bridge
        global bridge
        
        if not cdp.connected:
            ok = await cdp.discover_and_connect()
            if not ok:
                await status_msg.edit_text("❌ Cannot connect to Antigravity.\nMake sure it's running with `--remote-debugging-port=9222`")
                return
            bridge = AntigravityBridge(cdp)
            await status_msg.edit_text(f"🎤 *Transcribed:* _{user_text}_\n\n✅ Connected!", parse_mode="Markdown")

        if not bridge:
            bridge = AntigravityBridge(cdp)

        await status_msg.edit_text(f"🎤 *Transcribed:* _{user_text}_\n\n🧠 Sending to Antigravity...", parse_mode="Markdown")

        last_progress_text = ""
        async def progress_cb(text: str):
            nonlocal last_progress_text
            if text != last_progress_text:
                last_progress_text = text
                try:
                    safe_text = text[:MAX_MSG_LEN]
                    await status_msg.edit_text(safe_text)
                except Exception:
                    pass

        response = await bridge.send_and_receive(user_text, progress_cb)
        
        if not response:
            await status_msg.edit_text("⚠️ No response received")
            return
            
        try:
            await status_msg.delete()
        except Exception:
            pass
            
        # Split and send response
        chunks = []
        while response:
            if len(response) <= MAX_MSG_LEN:
                chunks.append(response)
                break
            split_at = response.rfind("\n", 0, MAX_MSG_LEN)
            if split_at < MAX_MSG_LEN // 2:
                split_at = MAX_MSG_LEN
            chunks.append(response[:split_at])
            response = response[split_at:].lstrip()

        # Send original transcript as quote if needed, though we already sent it.
        await update.message.reply_text(f"🎤 *You (Voice):* _{user_text}_", parse_mode="Markdown")
            
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk)
            except Exception as e:
                log.error(f"Failed to send chunk: {e}")

    except Exception as e:
        log.error(f"Voice handling error: {e}")
        await status_msg.edit_text(f"❌ Error processing voice message: {e}")


async def post_init(application):
    """Connect to CDP when the bot starts."""
    global bridge
    log.info("Connecting to Antigravity via CDP...")
    ok = await cdp.discover_and_connect()
    if ok:
        bridge = AntigravityBridge(cdp)
        log.info("✅ Bridge ready! Send a message on Telegram.")
    else:
        log.warning("⚠️  CDP not connected yet. Will auto-connect on first message.")


def main():
    log.info("Starting Antigravity Telegram Bridge...")
    log.info(f"Allowed users: {ALLOWED_USER_IDS}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reconnect", cmd_reconnect))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    log.info("Bot polling started...")

    # Graceful shutdown
    loop = asyncio.new_event_loop()

    def shutdown_handler(sig, frame):
        log.info("Shutting down...")
        loop.run_until_complete(cdp.close())
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
