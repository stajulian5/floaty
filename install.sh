#!/bin/bash
# Floaty installer — https://github.com/stajulian5/floaty
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
ok()  { echo -e "  ${GREEN}✓${NC} $*"; }
err() { echo -e "  ${RED}✗ $*${NC}"; exit 1; }
say() { echo -e "  $*"; }

echo ""
echo -e "${BOLD}  Floaty${NC} — Google Calendar floating widget for macOS"
echo "  ──────────────────────────────────────────────────"
echo ""

# ── macOS check ──────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || err "Floaty only runs on macOS."
MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[[ "$MACOS_MAJOR" -ge 12 ]] || err "Floaty requires macOS 12 (Monterey) or later. You have $(sw_vers -productVersion)."
ok "macOS $(sw_vers -productVersion)"

# ── Python 3 check ───────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON=$(command -v "$candidate")
        break
    fi
done
[[ -n "$PYTHON" ]] || err "Python 3 not found. Install via Homebrew:  brew install python3"

PY_VER=$("$PYTHON" --version 2>&1 | awk '{print $2}')
ok "Python $PY_VER ($PYTHON)"

# ── PyObjC check / install ───────────────────────────────────────────────────
echo -n "  Checking PyObjC… "
if "$PYTHON" -c "import AppKit" 2>/dev/null; then
    echo -e "${GREEN}already installed${NC}"
else
    echo -e "${YELLOW}not found — installing (this takes 2–5 min)…${NC}"
    "$PYTHON" -m pip install --quiet --user pyobjc 2>&1 | grep -v "^$" | sed 's/^/    /' || true
    "$PYTHON" -c "import AppKit" 2>/dev/null || err "PyObjC install failed. Try:  pip3 install pyobjc"
    ok "PyObjC installed"
fi

# ── Download Floaty ──────────────────────────────────────────────────────────
mkdir -p ~/TaskFloat
echo -n "  Downloading Floaty… "
curl -fsSL \
    "https://raw.githubusercontent.com/stajulian5/floaty/main/taskfloat.py" \
    -o ~/TaskFloat/taskfloat.py
chmod +x ~/TaskFloat/taskfloat.py
echo -e "${GREEN}done${NC}"

# ── Write config (shared OAuth credentials) ──────────────────────────────────
mkdir -p ~/.taskfloat
if [[ -f ~/.taskfloat/config.json ]]; then
    say "${DIM}Config already exists — keeping it.${NC}"
else
    cat > ~/.taskfloat/config.json << 'CONFIG'
{
  "client_id": "430885845082-490mq3coi76c66joc21sceo4fq36p4b7.apps.googleusercontent.com",
  "client_secret": "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"
}
CONFIG
    chmod 600 ~/.taskfloat/config.json
    ok "Config written"
fi

# ── LaunchAgent (auto-start at login) ────────────────────────────────────────
PLIST="$HOME/Library/LaunchAgents/com.taskfloat.plist"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.taskfloat</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$HOME/TaskFloat/taskfloat.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/taskfloat.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/taskfloat-error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
ok "Floaty will launch automatically at login"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}${GREEN}All done!${NC}"
echo ""
echo -e "  Floaty is starting now. Your browser will open to sign in with Google."
echo -e "  After you authorize, the widget appears on your screen."
echo ""
echo -e "  ${DIM}To uninstall:  curl -fsSL https://raw.githubusercontent.com/stajulian5/floaty/main/uninstall.sh | bash${NC}"
echo ""
