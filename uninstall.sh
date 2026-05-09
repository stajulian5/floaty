#!/bin/bash
# Floaty uninstaller
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
ok()  { echo -e "  ${GREEN}✓${NC} $*"; }

echo ""
echo -e "${BOLD}  Floaty uninstaller${NC}"
echo "  ──────────────────"
echo ""

# Stop and remove LaunchAgent
PLIST="$HOME/Library/LaunchAgents/com.taskfloat.plist"
if [[ -f "$PLIST" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    ok "LaunchAgent removed"
fi

# Kill any running process
pkill -f taskfloat.py 2>/dev/null && ok "Floaty stopped" || true

# Remove app files
rm -rf ~/TaskFloat
ok "~/TaskFloat removed"

# Remove config (ask first)
if [[ -d ~/.taskfloat ]]; then
    echo ""
    read -r -p "  Remove ~/.taskfloat (config + cached GIFs)? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        rm -rf ~/.taskfloat
        ok "~/.taskfloat removed"
    else
        echo "  Keeping ~/.taskfloat"
    fi
fi

# Remove Keychain tokens
security delete-generic-password -s "com.taskfloat" 2>/dev/null && ok "Keychain tokens removed" || true

echo ""
echo -e "  ${GREEN}Floaty has been uninstalled.${NC}"
echo ""
