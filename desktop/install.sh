#!/bin/zsh
# Installs Sonosphere as a Mac app:
#   ~/Applications/Sonosphere.app             the window (Dock icon, Cmd-Q, Spotlight)
#   ~/Library/LaunchAgents/com.jimmy.sonosphere.plist   the server, started at login and kept alive
# Re-run after pulling changes. Log: ~/Library/Logs/Sonosphere.log
set -e
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
UID_=$(id -u)

# Python 3.10+ venv for the server
if [ ! -x "$ROOT/venv/bin/python" ]; then
  PY=$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3.10 || true)
  [ -z "$PY" ] && [ -x ~/.local/bin/python3.11 ] && PY=~/.local/bin/python3.11
  [ -z "$PY" ] && { echo "Need Python 3.10+"; exit 1; }
  "$PY" -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install -q -r "$ROOT/requirements.txt"
"$ROOT/venv/bin/pip" install -q --upgrade yt-dlp

# The app bundle
APP=~/Applications/Sonosphere.app
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -o "$APP/Contents/MacOS/Sonosphere" Sonosphere.swift
cp Sonosphere.icns "$APP/Contents/Resources/Sonosphere.icns"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.jimmy.sonosphere.app</string>
<key>CFBundleName</key><string>Sonosphere</string>
<key>CFBundleDisplayName</key><string>Sonosphere</string>
<key>CFBundleExecutable</key><string>Sonosphere</string>
<key>CFBundleIconFile</key><string>Sonosphere</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>2.0</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
<key>NSLocalNetworkUsageDescription</key><string>Sonosphere controls the Sonos speakers on your network.</string>
</dict></plist>
PL
codesign -s - --force "$APP" >/dev/null 2>&1 || true
touch "$APP"

# The server, at login
PLIST=~/Library/LaunchAgents/com.jimmy.sonosphere.plist
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.jimmy.sonosphere</string>
<key>ProgramArguments</key><array>
  <string>$ROOT/venv/bin/python</string><string>-u</string><string>$ROOT/desktop/server.py</string>
</array>
<key>WorkingDirectory</key><string>$ROOT</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>LimitLoadToSessionType</key><string>Aqua</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
<key>StandardOutPath</key><string>$HOME/Library/Logs/Sonosphere.log</string>
<key>StandardErrorPath</key><string>$HOME/Library/Logs/Sonosphere.log</string>
</dict></plist>
PL
launchctl bootout gui/$UID_/com.jimmy.sonosphere 2>/dev/null || true
for i in 1 2 3 4 5; do launchctl bootstrap gui/$UID_ "$PLIST" 2>/dev/null && break; sleep 1; done  # bootout finishes async
echo "installed: $APP (server: com.jimmy.sonosphere on :8888)"
