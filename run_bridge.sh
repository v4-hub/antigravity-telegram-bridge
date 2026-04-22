#!/bin/bash
# ============================================================
#  Antigravity Telegram Bridge — Active Launcher & Monitor
# ============================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/bridge.log"
CDP_PORT=9233

export http_proxy="http://127.0.0.1:7897"
export https_proxy="http://127.0.0.1:7897"
export PYTHONUNBUFFERED=1
exec >> "$LOG_FILE" 2>&1
echo ""
echo "========================================"
echo "  Bridge Launcher started: $(date)"
echo "========================================"

is_cdp_ready() {
    curl -s --max-time 2 "http://127.0.0.1:${1:-$CDP_PORT}/json/version" > /dev/null 2>&1
}

# 1. Check if Antigravity is running
MAIN_PID=$(pgrep -f "/Applications/Antigravity.app/Contents/MacOS/Electron" | head -n 1)

if [ -n "$MAIN_PID" ]; then
    if is_cdp_ready $CDP_PORT; then
        echo "✅ Antigravity is already running with CDP port $CDP_PORT ready!"
    else
        echo "⚠️ Antigravity is running (PID $MAIN_PID) but CDP port $CDP_PORT is not ready."
        echo "Killing it and relaunching with CDP..."
        kill -15 $MAIN_PID 2>/dev/null || true
        sleep 2
        kill -9 $MAIN_PID 2>/dev/null || true
        open -a "/Applications/Antigravity.app" --args --remote-debugging-port=$CDP_PORT
    fi
else
    echo "🚀 Starting Antigravity with CDP on port $CDP_PORT..."
    open -a "/Applications/Antigravity.app" --args --remote-debugging-port=$CDP_PORT
fi

# 2. Wait for CDP to be ready
echo "⏳ Waiting for CDP port $CDP_PORT to be ready..."
while ! is_cdp_ready $CDP_PORT; do
    sleep 2
done

echo "✅ CDP port $CDP_PORT is ready!"
echo "🤖 Starting bridge.py (CDP port: $CDP_PORT)..."
cd "$SCRIPT_DIR"
source venv/bin/activate
exec python3 bridge.py
