#!/bin/bash
# One-command installer for Antigravity Telegram Bridge
set -e

echo "╔══════════════════════════════════════════════╗"
echo "║  Antigravity Telegram Bridge - Installer     ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

cd "$(dirname "$0")"

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 is required but not installed."
    echo "   Install it with: sudo apt install python3 python3-venv"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ Python $PYTHON_VERSION found"

# 2. Check ffmpeg (needed for voice messages)
if ! command -v ffmpeg &>/dev/null; then
    echo "⚠️  ffmpeg not found (needed for voice messages)"
    echo "   Installing ffmpeg..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y ffmpeg
    elif command -v brew &>/dev/null; then
        brew install ffmpeg
    else
        echo "   Please install ffmpeg manually"
    fi
fi
echo "✅ ffmpeg available"

# 3. Create virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi
echo "✅ Virtual environment ready"

# 4. Install dependencies
echo "📦 Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "✅ Dependencies installed"

# 5. Create .env if not exists
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚙️  Created .env file — please edit it with your settings:"
    echo "   nano .env"
    echo ""
    echo "   You need to set:"
    echo "   - TELEGRAM_BOT_TOKEN  (from @BotFather)"
    echo "   - ALLOWED_USER_IDS   (your Telegram user ID)"
else
    echo "✅ .env file already exists"
fi

OS_TYPE=$(uname)
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  ✅ Installation complete!                   ║"
echo "║                                              ║"
echo "║  Next steps:                                 ║"
echo "║  1. Edit .env with your bot token & user ID  ║"

if [ "$OS_TYPE" = "Darwin" ]; then
    echo "║  2. (macOS) Set up background service:       ║"
    echo "║     bash install_mac_service.sh              ║"
else
    echo "║  2. (Linux) Set up background service:       ║"
    echo "║     bash install_linux_service.sh            ║"
fi

echo "║  3. Or run manually (auto-starts IDE):       ║"
echo "║     bash run_bridge.sh                       ║"
echo "╚══════════════════════════════════════════════╝"
