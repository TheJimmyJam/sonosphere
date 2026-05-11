#!/bin/bash
# ──────────────────────────────────────────────────────────────
#  Sonosphere — Launcher
#  Double-click this file to start the player.
#  First run installs everything automatically (~1 min).
# ──────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

echo ""
echo "  ♪  Sonosphere"
echo "  ───────────────────────────────────────"

# ── Python check ─────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  osascript -e 'display alert "Python 3 not found" message "Please install Python 3.10 or later from https://python.org, then try again." as critical buttons {"Open python.org", "Cancel"} default button "Open python.org"' \
    && open "https://www.python.org/downloads/"
  exit 1
fi

# Check Python version >= 3.10
PY_VER=$(python3 -c "import sys; print(sys.version_info.minor + sys.version_info.major * 100)")
if [ "$PY_VER" -lt "310" ]; then
  CURRENT=$(python3 --version 2>&1)
  osascript -e "display alert \"Python 3.10+ required\" message \"You have $CURRENT. Please install Python 3.10 or later from https://python.org.\" as critical buttons {\"Open python.org\", \"Cancel\"} default button \"Open python.org\"" \
    && open "https://www.python.org/downloads/"
  exit 1
fi

echo "  ✓ Python $(python3 --version | cut -d' ' -f2)"

# ── Virtual environment ───────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "  Setting up Python environment (first run only — takes ~1 min)…"
  python3 -m venv venv
  if [ $? -ne 0 ]; then
    osascript -e 'display alert "Setup failed" message "Could not create Python environment. Try running: python3 -m venv venv in Terminal." as critical'
    exit 1
  fi
fi

source venv/bin/activate

# ── Dependencies ──────────────────────────────────────────────
echo "  Checking dependencies…"
pip install -q --upgrade pip
pip install -q -r requirements.txt
if [ $? -ne 0 ]; then
  echo ""
  echo "  ✗ Dependency install failed. Check your internet connection and try again."
  read -p "  Press Enter to close…"
  exit 1
fi

# Always keep yt-dlp current (YouTube changes their format frequently)
echo "  Updating yt-dlp…"
pip install -q --upgrade yt-dlp

echo "  ✓ Dependencies ready"

# ── Port check ────────────────────────────────────────────────
if lsof -i :8888 &>/dev/null; then
  echo ""
  echo "  ℹ  Player is already running — bringing window to front."
  # Try to focus an existing Chrome app-mode window
  osascript -e 'tell application "Google Chrome" to activate' 2>/dev/null || true
  exit 0
fi

# ── Launch ────────────────────────────────────────────────────
echo ""
echo "  Starting Sonosphere…"
echo "  (A window will open shortly)"
echo ""
echo "  ─────────────────────────────────────────"
echo "  If prompted for YouTube login, open the"
echo "  URL shown below in Chrome and enter the"
echo "  code. Press Enter here when done."
echo "  ─────────────────────────────────────────"
echo ""

python3 app.py
