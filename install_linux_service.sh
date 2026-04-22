#!/bin/bash
# ============================================================
# Linux Systemd Installer for Antigravity Telegram Bridge
# ============================================================

set -e
cd "$(dirname "$0")"
BRIDGE_DIR="$(pwd)"
SERVICE_NAME="telegram-bridge.service"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_PATH="$SERVICE_DIR/$SERVICE_NAME"

echo "📦 Installing Linux background service (Systemd)..."

# Check if systemd is available
if ! command -v systemctl &>/dev/null; then
    echo "❌ systemctl not found. This script requires Systemd."
    exit 1
fi

# Create systemd user directory if it doesn't exist
mkdir -p "$SERVICE_DIR"

# Create the service file
cat > "$SERVICE_PATH" << EOF
[Unit]
Description=Antigravity Telegram Bridge (Smart Launcher)
After=network.target

[Service]
Type=simple
WorkingDirectory=$BRIDGE_DIR
ExecStart=/bin/bash $BRIDGE_DIR/run_bridge.sh
Restart=always
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

[Install]
WantedBy=default.target
EOF

echo "✅ Created $SERVICE_PATH"

# Load, enable and start the service
echo "🚀 Loading service into systemd..."
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo "=========================================================="
echo "🎉 Done! The bridge is now running in the background."
echo "   It will automatically start whenever you log in."
echo ""
echo "   To view logs: journalctl --user -u $SERVICE_NAME -f"
echo "   To stop:      systemctl --user stop $SERVICE_NAME"
echo ""
echo "   Optional: If you want it to run even when you're logged out, run:"
echo "   sudo loginctl enable-linger \$USER"
echo "=========================================================="
