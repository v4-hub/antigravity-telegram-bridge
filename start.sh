#!/bin/bash
# Start the Antigravity Telegram Bridge
# Make sure Antigravity is running with: antigravity --remote-debugging-port=9233

cd "$(dirname "$0")"
source venv/bin/activate
python3 bridge.py
