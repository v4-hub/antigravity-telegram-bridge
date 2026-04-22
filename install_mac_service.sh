#!/bin/bash
# ============================================================
# macOS LaunchAgent Installer for Antigravity Telegram Bridge
# ============================================================

set -e
cd "$(dirname "$0")"
BRIDGE_DIR="$(pwd)"
PLIST_NAME="com.antigravity.telegram-bridge.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "📦 Installing macOS background service..."

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$HOME/Library/LaunchAgents"

# Create the plist file
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.antigravity.telegram-bridge</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BRIDGE_DIR/run_bridge.sh</string>
    </array>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$BRIDGE_DIR/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>$BRIDGE_DIR/bridge.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF

echo "✅ Created $PLIST_PATH"

# Load the service
echo "🚀 Loading service into launchctl..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo "=========================================================="
echo "🎉 Done! The bridge is now running in the background."
echo "   It will automatically start whenever you log in."
echo ""
echo "   To view logs: tail -f $BRIDGE_DIR/bridge.log"
echo "   To stop:      launchctl unload $PLIST_PATH"
echo "=========================================================="
