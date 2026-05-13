#!/bin/bash
# Floaty launcher — finds a real Python, installs PyObjC if needed, then runs.

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLED_SCRIPT="$APP_DIR/Resources/taskfloat.py"
SCRIPT="$HOME/.taskfloat/taskfloat.py"   # runs from here — outside sealed bundle
LOG="$HOME/Library/Logs/Floaty.log"
PLIST="$HOME/Library/LaunchAgents/com.floaty.plist"

mkdir -p "$HOME/Library/Logs"

# ── Find a real Python 3 ─────────────────────────────────────────────────────
# /usr/bin/python3 on a fresh Mac is a tiny Apple stub (~167 bytes) that shows
# the Xcode CLT install dialog instead of running. We detect it by file size:
# the stub is < 10 KB; a real Python is > 100 KB.
# We prefer Homebrew / pyenv installs first, then fall back to the system one
# only if it's actually real.

PYTHON=""

_is_real_python() {
    local p="$1"
    [[ -x "$p" ]] || return 1
    local sz
    sz=$(stat -f%z "$p" 2>/dev/null || echo 0)
    [[ "$sz" -gt 10000 ]] || return 1   # stub is ~167 B; real binary > 100 KB
    "$p" -c "import sys; exit(0)" 2>/dev/null || return 1
    return 0
}

for candidate in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$HOME/.pyenv/shims/python3" \
    /usr/bin/python3; do
    if _is_real_python "$candidate"; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    choice=$(osascript -e 'display alert "Floaty needs Python 3" message "Python 3 was not found on your Mac.\n\nClick \"Install Python\" to open the download page, install it, then reopen Floaty." as critical buttons {"Cancel", "Install Python"} default button "Install Python"' 2>/dev/null)
    if echo "$choice" | grep -q "Install Python"; then
        open "https://www.python.org/downloads/"
    fi
    exit 1
fi

# ── Ensure pip is available (quietly) ────────────────────────────────────────
"$PYTHON" -m ensurepip --upgrade >> "$LOG" 2>&1 || true   # --quiet not supported on Python ≤3.9

# ── Install PyObjC if needed (one-time, ~2 min) ───────────────────────────────
# Use -S (no user-site) then add user site-packages explicitly so we detect
# --user installs reliably regardless of the Python distribution.
_can_import_appkit() {
    USER_SITE=$("$PYTHON" -m site --user-site 2>/dev/null)
    "$PYTHON" -c "
import sys
user_site = '$USER_SITE'
if user_site and user_site not in sys.path:
    sys.path.insert(0, user_site)
import AppKit
" 2>/dev/null
}

if ! _can_import_appkit; then
    osascript -e 'display notification "Setting up Floaty for the first time — this takes about 2 minutes. Please wait…" with title "🚀 Floaty"'

    # Try --user first; fall back to --break-system-packages (Python 3.11+)
    "$PYTHON" -m pip install --quiet --user pyobjc >> "$LOG" 2>&1 || \
    "$PYTHON" -m pip install --quiet --user --break-system-packages pyobjc >> "$LOG" 2>&1 || \
    "$PYTHON" -m pip install --quiet pyobjc >> "$LOG" 2>&1 || true

    if ! _can_import_appkit; then
        ERR=$(tail -6 "$LOG" 2>/dev/null | tr '\n' ' ')
        osascript -e "display alert \"Floaty couldn't start\" message \"Setup failed. This sometimes fixes itself — try reopening Floaty.\n\nIf it keeps happening, email sta.julian@gmail.com and paste this:\n${ERR}\" as critical"
        exit 1
    fi

    osascript -e 'display notification "Right-click the widget anytime for options." with title "✅ Floaty is ready!"'
fi

# ── Write shared OAuth config if not already present ─────────────────────────
mkdir -p "$HOME/.taskfloat"
CONFIG_FILE="$HOME/.taskfloat/config.json"
if [[ ! -f "$CONFIG_FILE" ]]; then
    cat > "$CONFIG_FILE" << 'CONFIG'
{
  "client_id": "430885845082-490mq3coi76c66joc21sceo4fq36p4b7.apps.googleusercontent.com",
  "client_secret": "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"
}
CONFIG
    chmod 600 "$CONFIG_FILE"
fi

# ── Launch at Login ───────────────────────────────────────────────────────────
# We do NOT auto-register a LaunchAgent on first run — macOS Ventura/Sonoma (13+)
# shows a surprise "A background item was added" System Settings dialog whenever a
# new plist lands in ~/Library/LaunchAgents, which confuses first-time users.
# Instead, the "Launch at Login" toggle in Floaty Settings writes/removes the plist
# and calls `launchctl bootstrap/bootout` explicitly so the user is always in control.

# ── Bootstrap script outside the sealed app bundle ───────────────────────────
# The signed bundle must never be modified after signing (breaks Gatekeeper).
# We copy taskfloat.py to ~/.taskfloat/ on first run; auto-updates write there.
mkdir -p "$HOME/.taskfloat"
if [[ ! -f "$SCRIPT" ]]; then
    cp "$BUNDLED_SCRIPT" "$SCRIPT"
fi

exec "$PYTHON" "$SCRIPT" >> "$LOG" 2>&1
