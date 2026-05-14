#!/bin/bash
# Floaty launcher — finds a real Python, installs PyObjC if needed, then runs.

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLED_SCRIPT="$APP_DIR/Resources/taskfloat.py"
SCRIPT="$HOME/.taskfloat/taskfloat.py"   # runs from here — outside sealed bundle
LOG="$HOME/Library/Logs/Floaty.log"

mkdir -p "$HOME/Library/Logs"

# ── Find a real Python 3 ─────────────────────────────────────────────────────
# /usr/bin/python3 on a fresh Mac is a tiny stub (~167 bytes) that pops an
# Xcode install dialog. Detect by file size: stub < 10 KB, real binary > 100 KB.

PYTHON=""

_is_real_python() {
    local p="$1"
    [[ -x "$p" ]] || return 1
    local sz
    sz=$(stat -f%z "$p" 2>/dev/null || echo 0)
    [[ "$sz" -gt 10000 ]] || return 1
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

# ── No Python found — try to auto-install via Homebrew, else guide the user ──
if [[ -z "$PYTHON" ]]; then

    # Try Homebrew first — silent auto-install, no user action needed
    BREW=""
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [[ -x "$b" ]] && BREW="$b" && break
    done

    if [[ -n "$BREW" ]]; then
        osascript -e 'display notification "Installing Python — about 1 minute…" with title "🚀 Floaty"' 2>/dev/null
        "$BREW" install python3 --quiet >> "$LOG" 2>&1
        for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
            if _is_real_python "$candidate"; then
                PYTHON="$candidate"
                break
            fi
        done
    fi

    # Still no Python — show a friendly, non-technical guide
    if [[ -z "$PYTHON" ]]; then
        BTN=$(osascript << 'APPLESCRIPT' 2>/dev/null
display dialog "Floaty needs one free tool called Python 3 before it can run.

Here's what to do — it only takes 2 minutes:

  1.  Click  \"Download Python\"  below
  2.  Open the downloaded file and click Continue → Install
  3.  Come back and open Floaty again

That's it — you'll never need to do this again!" \
with title "Quick one-time setup" \
buttons {"Not now", "Download Python →"} \
default button "Download Python →" \
with icon note
return button returned of result
APPLESCRIPT
)
        if [[ "$BTN" == *"Download Python"* ]]; then
            # Link directly to the macOS installer page
            open "https://www.python.org/downloads/macos/"
        fi
        exit 1
    fi
fi

# ── Ensure pip is available ───────────────────────────────────────────────────
"$PYTHON" -m ensurepip --upgrade >> "$LOG" 2>&1 || true   # --quiet not supported on Python ≤3.9

# ── Install PyObjC if needed (one-time, ~2 min) ───────────────────────────────
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

    # Show a friendly progress dialog in the background.
    # It auto-dismisses after 5 min (giving up after 300) or when we kill it.
    osascript << 'APPLESCRIPT' &
display dialog "Getting Floaty ready — this only happens once ⏳

Downloading a few components in the background.
Takes about 2 minutes depending on your internet speed.

This window will close automatically when done.
Feel free to use your Mac while you wait!" \
with title "🚀 Floaty — First-time setup" \
buttons {"OK"} default button "OK" \
giving up after 300 \
with icon note
APPLESCRIPT
    DIALOG_PID=$!

    # Install PyObjC — try each method in order
    "$PYTHON" -m pip install --quiet --user pyobjc >> "$LOG" 2>&1 || \
    "$PYTHON" -m pip install --quiet --user --break-system-packages pyobjc >> "$LOG" 2>&1 || \
    "$PYTHON" -m pip install --quiet pyobjc >> "$LOG" 2>&1 || true

    # Dismiss the dialog
    kill "$DIALOG_PID" 2>/dev/null
    wait "$DIALOG_PID" 2>/dev/null

    if ! _can_import_appkit; then
        osascript << 'APPLESCRIPT' 2>/dev/null
display dialog "Floaty couldn't finish setting up.

The most common fix is simply to try opening Floaty again.

If it keeps happening, email sta.julian@gmail.com — I'll sort it out quickly!" \
with title "Setup didn't complete" \
buttons {"OK"} default button "OK" \
with icon caution
APPLESCRIPT
        exit 1
    fi

    osascript -e 'display notification "Right-click the widget anytime to add tasks, refresh, or access settings." with title "✅ Floaty is ready!"' 2>/dev/null
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
# Not auto-registered — macOS Ventura/Sonoma shows a "background item added"
# System Settings popup when any plist lands in LaunchAgents. Users opt in
# via the "Launch at Login" toggle in Floaty Settings instead.

# ── Bootstrap script outside the sealed app bundle ───────────────────────────
mkdir -p "$HOME/.taskfloat"
if [[ ! -f "$SCRIPT" ]]; then
    cp "$BUNDLED_SCRIPT" "$SCRIPT"
fi

exec "$PYTHON" "$SCRIPT" >> "$LOG" 2>&1
