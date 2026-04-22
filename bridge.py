#!/usr/bin/env python3
"""
Telegram Bridge for Antigravity IDE
Connects a Telegram Bot to Antigravity's chat via Chrome DevTools Protocol (CDP).

Usage:
  1. Launch Antigravity with CDP:  antigravity --remote-debugging-port=9333
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
import subprocess
import urllib.request
import atexit
from typing import Optional

import websockets
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
try:
    from funasr import AutoModel
    FUNASR_AVAILABLE = True
except ImportError:
    FUNASR_AVAILABLE = False
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
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
CDP_PORTS = [9233]  # Only try 9233 as explicitly requested
POLL_INTERVAL = 2.0        # seconds between DOM polls
STABLE_ROUNDS = 3          # how many unchanged polls before we declare "complete"
MAX_WAIT = 600             # max seconds to wait for a response
MAX_MSG_LEN = 4096         # Telegram message length limit
PID_LOCK_FILE = "/tmp/telegram-bridge.pid"  # singleton guard
CONTEXT_CACHE_SIZE = 5     # number of recent exchanges to cache for restart recovery

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")


# ── PID Lock (Singleton Guard) ────────────────────────────────────────────────

def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def acquire_pid_lock():
    """Acquire PID lock file. Exit if another instance is already running."""
    if os.path.exists(PID_LOCK_FILE):
        try:
            with open(PID_LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if _is_pid_alive(old_pid) and old_pid != os.getpid():
                print(f"❌ Another bridge instance is already running (PID {old_pid}). Exiting.")
                sys.exit(1)
            else:
                log.info(f"Stale PID lock found (PID {old_pid} is dead). Taking over.")
        except (ValueError, FileNotFoundError):
            pass  # corrupt or removed between check and read

    with open(PID_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(release_pid_lock)
    log.info(f"PID lock acquired: {os.getpid()}")


def release_pid_lock():
    """Release PID lock file on exit."""
    try:
        if os.path.exists(PID_LOCK_FILE):
            with open(PID_LOCK_FILE, "r") as f:
                stored_pid = int(f.read().strip())
            if stored_pid == os.getpid():
                os.remove(PID_LOCK_FILE)
                log.info("PID lock released.")
    except Exception:
        pass

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
                        # Use a short timeout so a stale WS page doesn't block discovery
                        await asyncio.wait_for(self._connect_ws(ws_url), timeout=8)
                        return True
                    except asyncio.TimeoutError:
                        log.warning(f"Connection to {ws_url} timed out (stale page?), trying next target")
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
        """Evaluate JavaScript in the browser and return the result value.
        
        Note: context_id=None means 'use default (main page) context'.
        This is the correct behaviour for modern Antigravity where the chat UI
        lives in the main workbench page DOM, not a separate cascade-panel frame.
        """
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
        # Only add contextId if explicitly provided — omitting it uses the default context
        if context_id is not None:
            params["contextId"] = context_id
        result = await self.call("Runtime.evaluate", params)
        if result and "result" in result:
            if result["result"].get("type") == "undefined":
                return None
            # Handle exception info
            if result.get("exceptionDetails"):
                log.debug(f"JS exception: {result['exceptionDetails'].get('text')}")
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


# ── Conversation Cache (for restart context recovery) ─────────────────────────

class ConversationCache:
    """Ring buffer that caches recent user↔AI exchanges for context restoration."""

    def __init__(self, max_size: int = CONTEXT_CACHE_SIZE):
        self.max_size = max_size
        self._exchanges: list[dict] = []  # [{"user": ..., "ai_summary": ...}, ...]

    def record(self, user_msg: str, ai_response: str):
        """Record a user→AI exchange. AI response is truncated to a summary."""
        # Keep only first 200 chars of AI response as summary
        summary = ai_response[:200].strip()
        if len(ai_response) > 200:
            summary += "..."
        self._exchanges.append({"user": user_msg[:200], "ai_summary": summary})
        if len(self._exchanges) > self.max_size:
            self._exchanges = self._exchanges[-self.max_size:]

    def build_resume_prompt(self) -> str:
        """Build a context-restoration prompt from cached exchanges."""
        if not self._exchanges:
            return ""
        lines = []
        for ex in self._exchanges:
            lines.append(f"User: {ex['user']}")
            lines.append(f"AI: {ex['ai_summary']}")
        history = "\n".join(lines)
        return (
            f"[CONTEXT RECOVERY] This is a new session after a remote restart. "
            f"Below is a summary of the recent conversation before restart:\n\n"
            f"{history}\n\n"
            f"Please acknowledge you have this context and continue assisting. "
            f"Do NOT repeat the previous answers — just confirm you're caught up "
            f"in one short sentence."
        )

    def clear(self):
        self._exchanges.clear()

    @property
    def has_context(self) -> bool:
        return len(self._exchanges) > 0


# Module-level singleton — survives across bridge reconnections
conversation_cache = ConversationCache()


# ── Antigravity Interaction ───────────────────────────────────────────────────

class AntigravityBridge:
    """High-level interface for injecting messages and reading responses."""

    CHAT_INPUT_SELECTOR = 'div[role="textbox"]:not(.xterm-helper-textarea)'

    RESPONSE_SCRIPT = r"""
    (() => {
        const panel = document.querySelector('.antigravity-agent-side-panel') || document;
        // Try multiple selectors in order of preference
        const selectors = [
            '.rendered-markdown',
            '.leading-relaxed.select-text',
            '[class*="rendered-markdown"]',
            '[class*="select-text"]',
            '.prose',
        ];
        let lastNode = null;
        for (const sel of selectors) {
            const nodes = panel.querySelectorAll(sel);
            if (nodes.length > 0) { lastNode = nodes[nodes.length - 1]; break; }
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
        // Check for cancel/stop button by tooltip id
        const cancelBtn = panel.querySelector('[data-tooltip-id="input-send-button-cancel-tooltip"]');
        if (cancelBtn) return true;
        // Check for stop icon button (SVG stop icon)
        const svgStopBtn = panel.querySelector('button svg[class*="stop"], button [class*="stop-icon"]');
        if (svgStopBtn) return true;
        // Check for any button with stop-like text
        const buttons = panel.querySelectorAll('button, [role="button"]');
        for (const btn of buttons) {
            const text = (btn.textContent || '').toLowerCase().trim();
            if (/^(stop|stop generating|stop response|cancel)$/.test(text)) return true;
        }
        return false;
    })()
    """

    def __init__(self, cdp: CdpConnection):
        self.cdp = cdp
        self._lock = asyncio.Lock()  # prevent concurrent message injection

    async def _find_cascade_context(self) -> Optional[int]:
        """Find the best execution context for DOM operations.
        
        In modern Antigravity, the chat UI lives directly in the main workbench
        page DOM — there is no separate cascade-panel execution context exposed
        via CDP. Returning None here causes evaluate() to use the default
        (main page) context, which is exactly what we want.
        
        We keep the cascade-panel search as an optimistic fast-path for older
        builds that do expose it, but never force a context that can't find
        the chat input.
        """
        # Fast path: historic cascade-panel context
        for ctx in self.cdp.contexts:
            if "cascade-panel" in ctx.get("url", ""):
                return ctx["id"]
        
        # Modern Antigravity: chat UI is in the main page — use default context
        # Verify the textbox is reachable without a specific contextId
        try:
            result = await self.cdp.evaluate(
                f'!!document.querySelector("{self.CHAT_INPUT_SELECTOR}")',
                None  # explicitly use default/main context
            )
            if result:
                log.debug("Chat input found in default page context (modern Antigravity mode)")
                return None  # None → evaluate uses main context
        except Exception as e:
            log.debug(f"Default context check failed: {e}")
        
        # Last resort: try each available context
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
        
        log.warning("Chat input not found in any context — returning None (will use default)")
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


# ── Approval Detection (ported from Remoat approvalDetector.ts) ───────────────

DETECT_APPROVAL_SCRIPT = r"""
(() => {
    const ALLOW_ONCE_PATTERNS = ['allow once', 'allow one time', '允许一次', '单次允许'];
    const ALWAYS_ALLOW_PATTERNS = [
        'allow this conversation', 'allow this chat', 'always allow', '在此对话中允许', '总是允许',
    ];
    const ALLOW_PATTERNS = ['allow', 'permit', 'approve', 'run', '允许', '运行', '批准', '同意', '执行'];
    const DENY_PATTERNS = ['deny', 'decline', 'reject', 'cancel', '拒绝', '取消'];

    const normalize = (text) => (text || '').toLowerCase().replace(/\s+/g, ' ').trim();

    const allButtons = Array.from(document.querySelectorAll('button'))
        .filter(btn => btn.offsetParent !== null);

    let approveBtn = allButtons.find(btn => {
        const t = normalize(btn.textContent || '');
        return ALLOW_ONCE_PATTERNS.some(p => t.includes(p));
    }) || null;

    if (!approveBtn) {
        approveBtn = allButtons.find(btn => {
            const t = normalize(btn.textContent || '');
            const isAlways = ALWAYS_ALLOW_PATTERNS.some(p => t.includes(p));
            return !isAlways && ALLOW_PATTERNS.some(p => t.includes(p));
        }) || null;
    }

    if (!approveBtn) return null;

    const container = approveBtn.closest('[role="dialog"], .modal, .dialog, .approval-container, .permission-dialog')
        || approveBtn.parentElement?.parentElement
        || approveBtn.parentElement
        || document.body;

    const containerButtons = Array.from(container.querySelectorAll('button'))
        .filter(btn => btn.offsetParent !== null);

    const denyBtn = containerButtons.find(btn => {
        const t = normalize(btn.textContent || '');
        return DENY_PATTERNS.some(p => t.includes(p));
    }) || null;

    if (!denyBtn) return null;

    const alwaysAllowBtn = containerButtons.find(btn => {
        const t = normalize(btn.textContent || '');
        return ALWAYS_ALLOW_PATTERNS.some(p => t.includes(p));
    }) || null;

    const approveText = (approveBtn.textContent || '').trim();
    const alwaysAllowText = alwaysAllowBtn ? (alwaysAllowBtn.textContent || '').trim() : '';
    const denyText = (denyBtn.textContent || '').trim();

    let description = '';
    const dialog = container;
    if (dialog) {
        const descEl = dialog.querySelector('p, .description, [data-testid="description"]');
        if (descEl) {
            description = (descEl.textContent || '').trim();
        }
    }
    if (!description) {
        const parent = approveBtn.parentElement?.parentElement || approveBtn.parentElement;
        if (parent) {
            const clone = parent.cloneNode(true);
            const buttons = clone.querySelectorAll('button');
            buttons.forEach(b => b.remove());
            const parentText = (clone.textContent || '').trim();
            if (parentText.length > 5 && parentText.length < 500) {
                description = parentText;
            }
        }
    }

    return { approveText, alwaysAllowText, denyText, description };
})()
"""

EXPAND_ALWAYS_ALLOW_SCRIPT = r"""
(() => {
    const ALLOW_ONCE_PATTERNS = ['allow once', 'allow one time', '允许一次', '单次允许'];
    const ALWAYS_ALLOW_PATTERNS = [
        'allow this conversation', 'allow this chat', 'always allow', '在此对话中允许', '总是允许',
    ];
    const normalize = (text) => (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const visibleButtons = Array.from(document.querySelectorAll('button'))
        .filter(btn => btn.offsetParent !== null);

    const directAlways = visibleButtons.find(btn => {
        const t = normalize(btn.textContent || '');
        return ALWAYS_ALLOW_PATTERNS.some(p => t.includes(p));
    });
    if (directAlways) return { ok: true, reason: 'already-visible' };

    const allowOnceBtn = visibleButtons.find(btn => {
        const t = normalize(btn.textContent || '');
        return ALLOW_ONCE_PATTERNS.some(p => t.includes(p));
    });
    if (!allowOnceBtn) return { ok: false, error: 'allow-once button not found' };

    const container = allowOnceBtn.closest('[role="dialog"], .modal, .dialog, .approval-container, .permission-dialog')
        || allowOnceBtn.parentElement?.parentElement
        || allowOnceBtn.parentElement
        || document.body;

    const containerButtons = Array.from(container.querySelectorAll('button'))
        .filter(btn => btn.offsetParent !== null);

    const toggleBtn = containerButtons.find(btn => {
        if (btn === allowOnceBtn) return false;
        const text = normalize(btn.textContent || '');
        const aria = normalize(btn.getAttribute('aria-label') || '');
        const hasPopup = btn.getAttribute('aria-haspopup');
        if (hasPopup === 'menu' || hasPopup === 'listbox') return true;
        if (text === '') return true;
        return /menu|more|expand|options|dropdown|chevron|arrow/.test(aria);
    });

    if (toggleBtn) {
        toggleBtn.click();
        return { ok: true, reason: 'toggle-button' };
    }
    return { ok: false, error: 'no toggle found' };
})()
"""

def build_click_script(button_text: str) -> str:
    """Generate JS that clicks a button by its text content."""
    safe_text = json.dumps(button_text)
    return f"""(() => {{
        const normalize = (text) => (text || '').toLowerCase().replace(/\\s+/g, ' ').trim();
        const wanted = normalize({safe_text});
        const allButtons = Array.from(document.querySelectorAll('button'));
        const target = allButtons.find(btn => {{
            if (!btn.offsetParent) return false;
            const buttonText = normalize(btn.textContent || '');
            const ariaLabel = normalize(btn.getAttribute('aria-label') || '');
            return buttonText === wanted || ariaLabel === wanted ||
                buttonText.includes(wanted) || ariaLabel.includes(wanted);
        }});
        if (!target) return {{ ok: false, error: 'Button not found: ' + {safe_text} }};
        target.click();
        return {{ ok: true }};
    }})()"""


class ApprovalMonitor:
    """Background poller that detects approval dialogs and notifies via Telegram."""

    POLL_INTERVAL = 1.5  # seconds

    def __init__(self, cdp_conn: CdpConnection, bot_app, chat_id: int):
        self.cdp = cdp_conn
        self.bot_app = bot_app
        self.chat_id = chat_id
        self._task: Optional[asyncio.Task] = None
        self._last_key: Optional[str] = None
        self._last_info: Optional[dict] = None
        self._msg_id: Optional[int] = None  # Telegram message with buttons
        self.auto_accept = False

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop())
        log.info("ApprovalMonitor started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("ApprovalMonitor stopped")

    async def _poll_loop(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if "WebSocket" not in str(e):
                    log.debug(f"ApprovalMonitor poll error: {e}")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _poll_once(self):
        if not self.cdp.connected:
            return

        ctx_id = None
        for ctx in self.cdp.contexts:
            if "cascade-panel" in ctx.get("url", ""):
                ctx_id = ctx["id"]
                break

        params = {"expression": DETECT_APPROVAL_SCRIPT, "returnByValue": True, "awaitPromise": False}
        if ctx_id is not None:
            params["contextId"] = ctx_id

        try:
            result = await self.cdp.call("Runtime.evaluate", params, timeout=5)
        except Exception:
            return

        info = None
        if result and "result" in result:
            val = result["result"].get("value")
            if isinstance(val, dict) and "approveText" in val:
                info = val

        if info:
            key = f"{info.get('approveText', '')}::{info.get('description', '')}"
            if key != self._last_key:
                self._last_key = key
                self._last_info = info

                if self.auto_accept:
                    # Auto-accept mode: click allow immediately
                    await self._click_button(info.get("approveText", "Allow"), ctx_id)
                    log.info(f"Auto-accepted: {info.get('description', '?')}")
                    try:
                        await self.bot_app.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"⚡ Auto-allowed: {info.get('description', '(action)')}",
                        )
                    except Exception:
                        pass
                else:
                    await self._send_approval_buttons(info)
        else:
            if self._last_key is not None:
                self._last_key = None
                self._last_info = None
                # Clean up old button message
                if self._msg_id:
                    try:
                        await self.bot_app.bot.edit_message_text(
                            chat_id=self.chat_id,
                            message_id=self._msg_id,
                            text="✅ Approval resolved.",
                        )
                    except Exception:
                        pass
                    self._msg_id = None

    async def _send_approval_buttons(self, info: dict):
        desc = info.get("description", "(unknown action)")
        approve_text = info.get("approveText", "Allow")
        deny_text = info.get("denyText", "Deny")

        text = f"🔐 *Approval Required*\n\n{desc}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ {approve_text}", callback_data="approval_allow"),
                InlineKeyboardButton("✅ Allow All", callback_data="approval_always"),
            ],
            [
                InlineKeyboardButton(f"❌ {deny_text}", callback_data="approval_deny"),
            ],
        ])

        try:
            msg = await self.bot_app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            self._msg_id = msg.message_id
            log.info(f"Sent approval request: {desc}")
        except Exception as e:
            log.error(f"Failed to send approval message: {e}")

    async def _click_button(self, button_text: str, ctx_id: int = None) -> bool:
        script = build_click_script(button_text)
        params = {"expression": script, "returnByValue": True, "awaitPromise": False}
        if ctx_id is not None:
            params["contextId"] = ctx_id
        try:
            result = await self.cdp.call("Runtime.evaluate", params, timeout=5)
            val = result.get("result", {}).get("value", {})
            return val.get("ok", False) if isinstance(val, dict) else False
        except Exception as e:
            log.error(f"Click button error: {e}")
            return False

    async def handle_callback(self, action: str) -> str:
        """Handle a Telegram inline button callback. Returns status message."""
        info = self._last_info
        if not info:
            return "⚠️ No pending approval found."

        ctx_id = None
        for ctx in self.cdp.contexts:
            if "cascade-panel" in ctx.get("url", ""):
                ctx_id = ctx["id"]
                break

        if action == "approval_allow":
            ok = await self._click_button(info.get("approveText", "Allow"), ctx_id)
            return "✅ Allowed!" if ok else "❌ Failed to click Allow button."

        elif action == "approval_always":
            # Try to expand dropdown and click "Allow This Conversation"
            expand_params = {"expression": EXPAND_ALWAYS_ALLOW_SCRIPT, "returnByValue": True, "awaitPromise": False}
            if ctx_id is not None:
                expand_params["contextId"] = ctx_id
            try:
                await self.cdp.call("Runtime.evaluate", expand_params, timeout=5)
                await asyncio.sleep(0.3)
            except Exception:
                pass

            candidates = [
                info.get("alwaysAllowText", ""),
                "Allow This Conversation", "Allow This Chat", "Always Allow",
                "在此对话中允许", "总是允许",
            ]
            for candidate in candidates:
                if candidate and await self._click_button(candidate, ctx_id):
                    return "✅ Allowed for this conversation!"

            # Fallback to regular allow
            ok = await self._click_button(info.get("approveText", "Allow"), ctx_id)
            return "✅ Allowed (fallback to single allow)!" if ok else "❌ Failed to click Allow button."

        elif action == "approval_deny":
            ok = await self._click_button(info.get("denyText", "Deny"), ctx_id)
            return "❌ Denied!" if ok else "❌ Failed to click Deny button."

        return "⚠️ Unknown action."


# ── Telegram Bot ──────────────────────────────────────────────────────────────

cdp = CdpConnection()
bridge: Optional[AntigravityBridge] = None
approval_monitor: Optional[ApprovalMonitor] = None


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
        "/reconnect — Reconnect to Antigravity\n"
        "/restart — Save work \u0026 restart Antigravity\n"
        "/memstat — Check Antigravity memory usage\n"
        "/autoaccept — Toggle auto-accept mode\n",
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
        await update.message.reply_text("❌ Failed to connect. Is Antigravity natively running (the CDP port 9233 should be open)?")


AG_SERVICE_NAME = "antigravity-cdp.service"
AG_RESTART_TIMEOUT = 60  # seconds to wait for systemd restart cycle
_restart_in_progress = False  # debounce flag for /restart


def _get_antigravity_main_pid() -> Optional[int]:
    """Find the main Antigravity process PID."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "antigravity.*--remote-debugging-port"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split("\n")
        return int(pids[0]) if pids and pids[0] else None
    except Exception:
        return None


def _get_service_active_state() -> str:
    """Check if Antigravity is running."""
    return "active" if _get_antigravity_main_pid() else "unknown"


def _kill_orphaned_antigravity_in_bridge_cgroup():
    """Kill any Antigravity processes that ended up in the bridge's cgroup from previous bad restarts."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "antigravity.*--user-data-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if not result.stdout.strip():
            return 0
        killed = 0
        for pid_str in result.stdout.strip().split("\n"):
            pid = int(pid_str)
            # Check if this process is in the telegram-bridge cgroup (orphan from previous restart)
            try:
                cgroup_path = f"/proc/{pid}/cgroup"
                with open(cgroup_path, "r") as f:
                    cgroup_content = f.read()
                if "telegram-bridge" in cgroup_content:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                    log.info(f"Killed orphaned Antigravity process {pid} in bridge cgroup")
            except (FileNotFoundError, ProcessLookupError):
                continue
        return killed
    except Exception as e:
        log.warning(f"Error cleaning orphan processes: {e}")
        return 0


def _get_antigravity_rss_mb() -> int:
    """Get total RSS of all Antigravity-related processes in MB."""
    try:
        main_pid = _get_antigravity_main_pid()
        if not main_pid:
            return 0
        result = subprocess.run(
            ["pstree", "-p", str(main_pid)],
            capture_output=True, text=True, timeout=5,
        )
        import re
        pids = re.findall(r'\((\d+)\)', result.stdout)
        total_kb = 0
        for pid in set(pids):
            try:
                rss_result = subprocess.run(
                    ["ps", "-o", "rss=", "-p", pid],
                    capture_output=True, text=True, timeout=3,
                )
                rss = rss_result.stdout.strip()
                if rss:
                    total_kb += int(rss)
            except Exception:
                continue
        return total_kb // 1024
    except Exception:
        return 0


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gracefully restart Antigravity via systemd: systemctl restart → reconnect CDP."""
    if not authorized(update.effective_user.id):
        return

    global bridge, approval_monitor, _restart_in_progress

    # Debounce: prevent concurrent restart commands
    if _restart_in_progress:
        await update.message.reply_text(
            "⚠️ A restart is already in progress. Please wait.",
        )
        return
    _restart_in_progress = True

    try:
        await _do_restart(update)
    finally:
        _restart_in_progress = False


async def _do_restart(update: Update):
    """Internal restart implementation, called by cmd_restart with debounce guard."""
    global bridge, approval_monitor
    status_msg = await update.message.reply_text(
        "🔄 *Restarting Antigravity...*\n\n1️⃣ Checking service...",
        parse_mode="Markdown",
    )

    # 0. Clean up orphaned Antigravity processes from previous bad restarts
    orphans_killed = _kill_orphaned_antigravity_in_bridge_cgroup()
    if orphans_killed:
        log.info(f"Cleaned {orphans_killed} orphaned Antigravity process(es) from bridge cgroup")

    # 1. Check current state
    old_pid = _get_antigravity_main_pid()
    state = _get_service_active_state()

    if state == "unknown":
        await status_msg.edit_text(
            "❌ `antigravity-cdp.service` not found.\n"
            "Make sure it is installed as a user systemd service.",
            parse_mode="Markdown",
        )
        return

    # 2. Close CDP connection before restart
    await cdp.close()
    bridge = None

    await status_msg.edit_text(
        f"🔄 *Restarting Antigravity...*\n\n"
        f"1️⃣ Status: `{state}` (PID {old_pid or 'N/A'})\n"
        f"2️⃣ Restarting via macOS process... (hot-exit → relaunch)",
        parse_mode="Markdown",
    )

    # 3. Kill and Relaunch
    try:
        if old_pid:
            os.kill(old_pid, signal.SIGTERM)
            await asyncio.sleep(2)
            try:
                os.kill(old_pid, 0) # check if still alive
                os.kill(old_pid, signal.SIGKILL)
            except OSError:
                pass
        
        # Relaunch using nohup
        # Assuming antigravity is in PATH. If not, consider providing a full path.
        subprocess.Popen(
            "nohup antigravity --remote-debugging-port=9333 > /dev/null 2>&1 &",
            shell=True,
            start_new_session=True
        )
        await asyncio.sleep(3) # Give it a moment to start
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to restart service: `{e}`", parse_mode="Markdown")
        return

    await status_msg.edit_text(
        "🔄 *Restarting Antigravity...*\n\n"
        "1️⃣ Process restarted ✅\n"
        "2️⃣ Work saved (hot-exit) ✅\n"
        "3️⃣ Waiting for startup (reconnecting CDP)...",
        parse_mode="Markdown",
    )

    # 4. Wait for CDP to become available
    connected = False
    for attempt in range(8):
        await asyncio.sleep(5)
        try:
            ok = await cdp.discover_and_connect()
            if ok:
                bridge = AntigravityBridge(cdp)
                connected = True
                break
        except Exception:
            pass
        log.info(f"CDP reconnect attempt {attempt + 1}/8...")

    if connected:
        new_pid = _get_antigravity_main_pid()
        rss_mb = _get_antigravity_rss_mb()

        # Restart approval monitor
        if approval_monitor:
            approval_monitor.cdp = cdp
            approval_monitor.start()

        # Inject context recovery prompt if we have cached conversation history
        context_restored = False
        if conversation_cache.has_context:
            resume_prompt = conversation_cache.build_resume_prompt()
            log.info(f"Injecting context recovery prompt ({len(resume_prompt)} chars)")
            await status_msg.edit_text(
                "✅ *Antigravity Restarted Successfully!*\n\n"
                f"New PID: `{new_pid}`\n"
                f"Memory: {rss_mb} MB\n"
                f"CDP: 🟢 Connected\n"
                f"🔄 Restoring conversation context...",
                parse_mode="Markdown",
            )
            # Wait a bit for the UI to fully load before injecting
            await asyncio.sleep(3)
            try:
                if await bridge.inject_message(resume_prompt):
                    # Wait for AI acknowledgement (short timeout)
                    ack = await bridge.wait_for_response()
                    if ack:
                        context_restored = True
                        log.info("Context recovery acknowledged by AI")
            except Exception as e:
                log.warning(f"Context recovery injection failed: {e}")

        restored_label = "Context restored ✅" if context_restored else "New session (no prior context)"
        await status_msg.edit_text(
            "✅ *Antigravity Restarted Successfully!*\n\n"
            f"New PID: `{new_pid}`\n"
            f"Memory: {rss_mb} MB\n"
            f"CDP: 🟢 Connected\n"
            f"Session: {restored_label}",
            parse_mode="Markdown",
        )
    else:
        await status_msg.edit_text(
            "⚠️ *Antigravity relaunched but CDP not yet available*\n\n"
            "Antigravity may still be loading. Try `/reconnect` in a moment.",
            parse_mode="Markdown",
        )


async def cmd_memstat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current Antigravity memory usage."""
    if not authorized(update.effective_user.id):
        return

    main_pid = _get_antigravity_main_pid()
    if not main_pid:
        await update.message.reply_text("⚠️ Antigravity is not running.")
        return

    rss_mb = _get_antigravity_rss_mb()

    # Get top 5 memory consumers among Antigravity processes
    try:
        result = subprocess.run(
            ["bash", "-c",
             "ps aux --sort=-%mem | grep -i antigravity | grep -v grep | head -5 | "
             "awk '{printf \"%s MB — %s\\n\", int($6/1024), $11}'"  
            ],
            capture_output=True, text=True, timeout=5,
        )
        top_procs = result.stdout.strip()
    except Exception:
        top_procs = "(unable to list)"

    # Warn level
    if rss_mb >= 40960:
        emoji = "🔴"
        level = "CRITICAL"
    elif rss_mb >= 20480:
        emoji = "🟡"
        level = "WARNING"
    else:
        emoji = "🟢"
        level = "Healthy"

    await update.message.reply_text(
        f"{emoji} *Antigravity Memory: {level}*\n\n"
        f"Total RSS: `{rss_mb} MB`\n"
        f"Main PID: `{main_pid}`\n\n"
        f"*Top processes:*\n```\n{top_procs}\n```",
        parse_mode="Markdown",
    )


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
            await status_msg.edit_text("❌ Cannot connect to Antigravity.\nMake sure it's running with `--remote-debugging-port=9333`")
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

    # Cache the exchange for context recovery after restart
    if response and not response.startswith("❌"):
        conversation_cache.record(user_text, response)

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

# ── Speech to Text (FunASR) ───────────────────────────────────────────────────

funasr_model = None

def get_funasr_model():
    global funasr_model
    if funasr_model is None:
        log.info("Loading FunASR Paraformer-large (paraformer-zh) model...")
        funasr_model = AutoModel(
            model="paraformer-zh", 
            vad_model="fsmn-vad", 
            punc_model="ct-punc-c", 
            disable_update=True,
            log_level="ERROR"
        )
        log.info("Local FunASR model loaded successfully.")
    return funasr_model

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
        
        # 2. Transcribe with FunASR
        await status_msg.edit_text("🗣️ Transcribing locally (FunASR)...")
        
        def transcribe():
            model = get_funasr_model()
            res = model.generate(input=temp_path)
            if res and len(res) > 0 and 'text' in res[0]:
                return res[0]['text'].strip()
            return ""

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
                await status_msg.edit_text("❌ Cannot connect to Antigravity.\nMake sure it's running with `--remote-debugging-port=9333`")
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

        # Cache the exchange for context recovery after restart
        if response and not response.startswith("❌"):
            conversation_cache.record(user_text, response)

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


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button clicks for approval."""
    query = update.callback_query
    if not query or not authorized(query.from_user.id):
        return
    await query.answer()  # acknowledge the callback

    action = query.data
    if not action or not action.startswith("approval_"):
        return

    if not approval_monitor:
        await query.edit_message_text("⚠️ Approval monitor not running.")
        return

    result_text = await approval_monitor.handle_callback(action)
    try:
        await query.edit_message_text(result_text)
    except Exception:
        pass


async def cmd_autoaccept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-accept mode: /autoaccept [on|off]"""
    if not authorized(update.effective_user.id):
        return
    if not approval_monitor:
        await update.message.reply_text("⚠️ Approval monitor not running.")
        return

    args = (context.args[0].lower() if context.args else "").strip()
    if args in ("on", "enable", "true", "1"):
        approval_monitor.auto_accept = True
        await update.message.reply_text("✅ Auto-accept mode *ON*. All approvals will be auto-allowed.", parse_mode="Markdown")
    elif args in ("off", "disable", "false", "0"):
        approval_monitor.auto_accept = False
        await update.message.reply_text("✅ Auto-accept mode *OFF*. Manual approval required.", parse_mode="Markdown")
    else:
        status = "ON 🟢" if approval_monitor.auto_accept else "OFF ⚪"
        await update.message.reply_text(
            f"⚙️ Auto-accept mode: *{status}*\n\nUsage: `/autoaccept on` or `/autoaccept off`",
            parse_mode="Markdown",
        )


async def post_init(application):
    """Connect to CDP when the bot starts."""
    global bridge, approval_monitor
    log.info("Connecting to Antigravity via CDP...")
    ok = await cdp.discover_and_connect()
    if ok:
        bridge = AntigravityBridge(cdp)
        log.info("✅ Bridge ready! Send a message on Telegram.")
    else:
        log.warning("⚠️  CDP not connected yet. Will auto-connect on first message.")

    # Register bot commands menu (keeps Telegram menu in sync with actual handlers)
    from telegram import BotCommand
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Welcome message & help"),
            BotCommand("status", "Check CDP connection status"),
            BotCommand("reconnect", "Reconnect to Antigravity"),
            BotCommand("restart", "Save work & restart Antigravity"),
            BotCommand("memstat", "Check Antigravity memory usage"),
            BotCommand("autoaccept", "Toggle auto-approve mode"),
        ])
        log.info("✅ Bot command menu registered")
    except Exception as e:
        log.warning(f"Failed to register bot commands: {e}")

    # Start approval monitor (uses first allowed user as target chat)
    if ALLOWED_USER_IDS:
        target_chat_id = next(iter(ALLOWED_USER_IDS))
        approval_monitor = ApprovalMonitor(cdp, application, target_chat_id)
        approval_monitor.start()
        log.info(f"✅ ApprovalMonitor started (chat_id={target_chat_id})")


def main():
    # Singleton guard: prevent multiple bridge instances
    acquire_pid_lock()

    log.info("Starting Antigravity Telegram Bridge...")
    log.info(f"Allowed users: {ALLOWED_USER_IDS}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reconnect", cmd_reconnect))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("memstat", cmd_memstat))
    app.add_handler(CommandHandler("autoaccept", cmd_autoaccept))
    app.add_handler(CallbackQueryHandler(handle_approval_callback))
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
