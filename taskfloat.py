#!/usr/bin/env python3
"""
TaskFloat — floating Google Calendar widget for macOS.
Shows the currently active event, or the next upcoming one.
Floats on all Spaces, draggable, position persists across restarts.

Usage:
    python3 ~/TaskFloat/taskfloat.py

Requires ~/.taskfloat/config.json with client_id and client_secret.
See README.md for Google Cloud setup instructions.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ctypes

import AppKit
import Foundation
import objc
import Security
try:
    import WebKit
except ImportError:
    WebKit = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".taskfloat" / "config.json"
KEYCHAIN_SERVICE = "com.taskfloat"
VERSION = "1.3.4"  # single source of truth — keep in sync with setup.py plist

# Self-healing crash detection
_CLEAN_EXIT_FILE = Path("/tmp/floaty_exit_clean")   # written on graceful quit
_FLOATY_LOG      = Path.home() / "Library/Logs/Floaty.log"

# Bundled OAuth credentials — auto-create config.json on first run.
# These are public-scope Desktop app credentials (not a server secret).  # nosec
BUNDLED_CLIENT_ID     = "430885845082-490mq3coi76c66joc21sceo4fq36p4b7.apps.googleusercontent.com"
BUNDLED_CLIENT_SECRET = "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"  # nosec


def _ping_launch() -> None:
    """Anonymous launch count — no personal data. FIX 18: decoupled from _auto_update."""
    def _ping():
        try:
            urllib.request.urlopen(
                "https://floaty.goatcounter.com/count?p=/launch",
                timeout=5,
            )
        except Exception:
            pass
    threading.Thread(target=_ping, daemon=True).start()


# ---------------------------------------------------------------------------
# Self-healing crash recovery
# ---------------------------------------------------------------------------

def _mark_clean_exit() -> None:
    """Call on any graceful quit so the next launch knows it was intentional."""
    try:
        _CLEAN_EXIT_FILE.write_text(str(os.getpid()))
    except Exception:
        pass


def _extract_crash_context(log_text: str, source_code: str) -> str:
    """Pull the traceback + relevant source lines from the log."""
    import re
    src_lines = source_code.splitlines()

    # Grab the last Python traceback block
    tb_matches = list(re.finditer(r'Traceback \(most recent call last\).*?(?=\n\n|\Z)', log_text, re.DOTALL))
    traceback_block = tb_matches[-1].group(0) if tb_matches else log_text[-3000:]

    # Find line numbers mentioned in taskfloat.py
    line_nums = [int(n) for n in re.findall(r'taskfloat\.py", line (\d+)', traceback_block)]

    code_snippets = []
    for ln in sorted(set(line_nums)):
        start = max(0, ln - 20)
        end   = min(len(src_lines), ln + 20)
        snippet = "\n".join(f"{i+1:4d}: {src_lines[i]}" for i in range(start, end))
        code_snippets.append(f"[taskfloat.py around line {ln}]\n{snippet}")

    return traceback_block + "\n\n" + "\n\n".join(code_snippets)


def _call_anthropic(api_key: str, prompt: str) -> str:
    """Minimal Anthropic Messages API call — no SDK required."""
    body = json.dumps({
        "model": "claude-opus-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":          api_key,
            "anthropic-version":  "2023-06-01",
            "content-type":       "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


def _apply_fix_to_bundle(find_str: str, replace_str: str) -> bool:
    """Patch taskfloat.py inside the running bundle (no rebuild needed)."""
    bundle_py = Path(Foundation.NSBundle.mainBundle().resourcePath()) / "taskfloat.py"
    if not bundle_py.exists():
        return False
    src = bundle_py.read_text()
    if find_str not in src:
        return False
    bundle_py.write_text(src.replace(find_str, replace_str, 1))

    # Also patch the project source so the next build inherits the fix
    project_py = Path(__file__)
    if project_py.exists():
        psrc = project_py.read_text()
        if find_str in psrc:
            project_py.write_text(psrc.replace(find_str, replace_str, 1))
    return True


def _check_and_heal_crash() -> None:
    """
    Called at startup (background thread).

    If the previous run did NOT write _CLEAN_EXIT_FILE we assume it crashed.
    We read the log, ask Claude for a minimal patch, apply it to the bundle,
    then relaunch so the fix is live on the very next run.
    """
    # First-ever run: no marker yet — not a crash
    if not _CLEAN_EXIT_FILE.exists() and not _FLOATY_LOG.exists():
        return

    clean = _CLEAN_EXIT_FILE.exists()
    if _CLEAN_EXIT_FILE.exists():
        try:
            _CLEAN_EXIT_FILE.unlink()
        except Exception:
            pass

    if clean:
        return  # graceful quit — nothing to fix

    # ── Crash detected ────────────────────────────────────────────────────
    try:
        log_text = _FLOATY_LOG.read_text(errors="replace")
    except Exception:
        return

    # Only act if there's a real Python traceback
    if "Traceback (most recent call last)" not in log_text:
        return

    try:
        config = load_config()
    except Exception:
        config = {}

    api_key = config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return  # no key configured — skip silently

    try:
        source_code = (
            Path(Foundation.NSBundle.mainBundle().resourcePath()) / "taskfloat.py"
        ).read_text()
    except Exception:
        return

    context = _extract_crash_context(log_text, source_code)

    prompt = f"""You are reviewing a crash in Floaty, a macOS Python/PyObjC app (taskfloat.py).

CRASH LOG (last traceback + relevant source lines):
{context}

Your task:
1. Identify the root cause in 1-2 sentences.
2. Produce the MINIMAL safe fix — prefer wrapping in try/except or adding a None-check over restructuring.
3. Return ONLY valid JSON, no markdown, no explanation outside the JSON:

{{
  "analysis": "<one sentence>",
  "find": "<exact string to find in taskfloat.py, including indentation>",
  "replace": "<replacement string, same indentation>"
}}

If you cannot determine a safe fix, return: {{"analysis": "<reason>", "find": null, "replace": null}}
"""

    def _heal():
        try:
            response = _call_anthropic(api_key, prompt)
            # Strip markdown fences if present
            response = response.strip().strip("```json").strip("```").strip()
            fix = json.loads(response)

            if not fix.get("find") or not fix.get("replace"):
                return

            applied = _apply_fix_to_bundle(fix["find"], fix["replace"])
            if not applied:
                return

            # Notify and relaunch with the patched bundle
            analysis_short = fix["analysis"][:80]
            subprocess.run([
                "osascript", "-e",
                f'display notification "Auto-fixed: {analysis_short}" '
                f'with title "🚀 Floaty self-healed"'
            ], check=False)
            time.sleep(1.5)

            bundle_path = Foundation.NSBundle.mainBundle().bundlePath()
            _CLEAN_EXIT_FILE.write_text("pre-relaunch")  # avoid re-trigger
            subprocess.Popen(["open", "-a", bundle_path],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            AppKit.NSApp.terminate_(None)

        except Exception:
            pass

    threading.Thread(target=_heal, daemon=True).start()


def _auto_update() -> None:
    """Check GitHub releases and silently install if a newer version is found.

    Flow:
      1. Hit GitHub API for latest release
      2. If newer: download the DMG asset to /tmp
      3. Mount the DMG, copy Floaty.app over the running copy
      4. Unmount, launch a background script that relaunches after we quit
      5. NSApp.terminate — new version takes over
    """
    def _check():
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/stajulian5/floaty/releases/latest",
                headers={"User-Agent": "Floaty/" + VERSION, "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            tag = data.get("tag_name", "").lstrip("v")
            if not tag:
                return
            def ver_tuple(v): return tuple(int(x) for x in v.split("."))
            if ver_tuple(tag) <= ver_tuple(VERSION):
                return  # already up to date

            # Find the .dmg asset
            dmg_url = None
            for asset in data.get("assets", []):
                if asset.get("name", "").endswith(".dmg"):
                    dmg_url = asset.get("browser_download_url")
                    break
            if not dmg_url:
                return

            # ── Download ──────────────────────────────────────────────────
            dmg_path = Path("/tmp/Floaty_update.dmg")
            with urllib.request.urlopen(dmg_url, timeout=120) as resp:
                dmg_path.write_bytes(resp.read())

            # ── Mount ─────────────────────────────────────────────────────
            result = subprocess.run(
                ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-quiet",
                 "-mountpoint", "/tmp/FloatyUpdateMount"],
                capture_output=True,
            )
            if result.returncode != 0:
                return

            # ── Find Floaty.app on the mounted volume ─────────────────────
            mounted_app = Path("/tmp/FloatyUpdateMount/Floaty.app")
            if not mounted_app.exists():
                subprocess.run(["hdiutil", "detach", "/tmp/FloatyUpdateMount", "-quiet"],
                               capture_output=True)
                return

            # ── Stage new app next to current one ────────────────────────
            current_app = Path(
                Foundation.NSBundle.mainBundle().bundlePath()
            )
            staged_app = Path("/tmp/Floaty_staged.app")
            if staged_app.exists():
                subprocess.run(["rm", "-rf", str(staged_app)])
            subprocess.run(["cp", "-R", str(mounted_app), str(staged_app)])
            subprocess.run(["hdiutil", "detach", "/tmp/FloatyUpdateMount", "-quiet"],
                           capture_output=True)

            # ── Write a tiny launcher script that swaps and relaunches ────
            relaunch_script = f"""#!/bin/bash
# Wait for old Floaty to exit
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done
# Swap in new app
rm -rf "{current_app}"
cp -R "{staged_app}" "{current_app}"
rm -rf "{staged_app}"
# Remove lock so new instance can start
rm -f /tmp/taskfloat.lock
# Relaunch
open -a "{current_app}"
"""
            script_path = Path("/tmp/floaty_relaunch.sh")
            script_path.write_text(relaunch_script)
            script_path.chmod(0o755)
            subprocess.Popen(["bash", str(script_path)],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

            # ── Notify user then quit so the script can take over ─────────
            def _finish():
                try:
                    subprocess.run([
                        "osascript", "-e",
                        f'display notification "Installing v{tag} — back in a moment." '
                        f'with title "🚀 Floaty updating…"'
                    ], check=False)
                except Exception:
                    pass
                time.sleep(1.5)
                _mark_clean_exit()
                AppKit.NSApp.terminate_(None)

            Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(_finish)

        except Exception:
            pass

    threading.Thread(target=_check, daemon=True).start()


def _register_login_item() -> None:
    """Ensure the LaunchAgent plist exists so Floaty starts at login.
    The plist is written by the .app launcher; this is a safety net only.
    We never call launchctl load — that can pop open System Preferences on newer macOS."""
    pass  # plist is managed by the launcher script; nothing to do here


# FIX 7: OAuth port fallback — try 8765-8768 before giving up
OAUTH_PORTS = [8765, 8766, 8767, 8768]
_oauth_port: int = 8765  # resolved at runtime by _find_oauth_port()
REDIRECT_URI = "http://localhost:8765/callback"  # updated dynamically per _oauth_port
OAUTH_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/tasks"
)
WINDOW_ORIGIN_KEY = "taskfloat.windowOrigin"
REFRESH_INTERVAL = 60  # seconds
GIF_DIR = Path.home() / ".taskfloat" / "gifs"

# NOTE: "LIVDSRZULELA" is Tenor's public demo key — for production use, register a free
# key at https://developers.google.com/tenor/guides/quickstart and replace this value.
_TENOR_API_KEY = "LIVDSRZULELA"  # Tenor public demo key (v1 API)
_TENOR_SEARCH_TERMS = [
    "success celebration", "victory dance", "job done", "yes winning",
    "awesome reaction", "nailed it", "mission accomplished", "crushing it",
    "happy dance", "high five",
]


def _ensure_hype_gifs() -> None:
    GIF_DIR.mkdir(parents=True, exist_ok=True)
    # Remove stale tiny files (e.g. old error images)
    for bad in GIF_DIR.glob("hype_*.gif"):
        if bad.stat().st_size < 20_000:
            bad.unlink(missing_ok=True)
    existing = [g for g in GIF_DIR.glob("hype_*.gif") if g.stat().st_size >= 20_000]
    if len(existing) >= 30:
        return
    idx = 0
    for term in _TENOR_SEARCH_TERMS:
        try:
            api_url = (
                "https://api.tenor.com/v1/search"
                f"?q={urllib.parse.quote(term)}"
                f"&key={_TENOR_API_KEY}&limit=5&media_filter=minimal&contentfilter=high"
            )
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            for result in data.get("results", []):
                gif_url = result["media"][0]["gif"]["url"]
                dest = GIF_DIR / f"hype_{idx}.gif"
                idx += 1
                if dest.exists() and dest.stat().st_size >= 20_000:
                    continue
                try:
                    req2 = urllib.request.Request(gif_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req2, timeout=15) as r2:
                        dest.write_bytes(r2.read())
                except Exception:
                    pass
        except Exception:
            pass


def _random_hype_gif() -> "Path | None":
    if not GIF_DIR.exists():
        return None
    # Exclude tiny (broken) and huge (>4 MB, causes UI freeze) files
    candidates = [
        g for g in GIF_DIR.glob("hype_*.gif")
        if 20_000 <= g.stat().st_size <= 4_000_000
    ]
    return random.choice(candidates) if candidates else None

# ---------------------------------------------------------------------------
# Global hotkey  (⌥⌘+  →  open Add Task dialog, requires Accessibility)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Keychain helpers
# ---------------------------------------------------------------------------

def keychain_write(key: str, value: str) -> None:
    query = {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: KEYCHAIN_SERVICE,
        Security.kSecAttrAccount: key,
    }
    data = value.encode("utf-8")
    attrs = dict(query)
    attrs[Security.kSecValueData] = data
    attrs[Security.kSecAttrAccessible] = Security.kSecAttrAccessibleAfterFirstUnlock
    raw = Security.SecItemAdd(attrs, None)
    status = raw[0] if isinstance(raw, tuple) else raw
    if status == -25299:  # errSecDuplicateItem — update instead
        update_attrs = {
            Security.kSecValueData: data,
            Security.kSecAttrAccessible: Security.kSecAttrAccessibleAfterFirstUnlock,
        }
        raw2 = Security.SecItemUpdate(query, update_attrs)
        status = raw2[0] if isinstance(raw2, tuple) else raw2
    if status != Security.errSecSuccess:
        print(f"[keychain] write failed for '{key}': {status}", file=sys.stderr)


def keychain_read(key: str) -> str | None:
    query = {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: KEYCHAIN_SERVICE,
        Security.kSecAttrAccount: key,
        Security.kSecReturnData: True,
        Security.kSecMatchLimit: Security.kSecMatchLimitOne,
    }
    status, result = Security.SecItemCopyMatching(query, None)
    if status == Security.errSecSuccess and result:
        return bytes(result).decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    # FIX 1: auto-create config.json with bundled credentials on first run
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        default = {"client_id": BUNDLED_CLIENT_ID, "client_secret": BUNDLED_CLIENT_SECRET}
        CONFIG_PATH.write_text(json.dumps(default, indent=2))
        CONFIG_PATH.chmod(0o600)
        return default
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)
    if "client_id" not in cfg or "client_secret" not in cfg:
        raise ValueError(f"{CONFIG_PATH} must contain 'client_id' and 'client_secret'")
    return cfg


def save_config(cfg: dict) -> None:
    """Write the full config dict back to CONFIG_PATH."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w") as f:
        json.dump(cfg, f, indent=2)


def fetch_calendar_list(config: dict) -> list:
    """Return list of dicts with id, summary, primary keys."""
    token = get_valid_token(config)
    req = urllib.request.Request(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return [
        {"id": item["id"], "summary": item.get("summary", item["id"]), "primary": item.get("primary", False)}
        for item in data.get("items", [])
    ]


def fetch_task_lists_all(config: dict) -> list:
    """Return list of dicts with id, title keys."""
    token = get_valid_token(config)
    req = urllib.request.Request(
        "https://www.googleapis.com/tasks/v1/users/@me/lists?maxResults=20",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return [{"id": item["id"], "title": item.get("title", item["id"])} for item in data.get("items", [])]


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------

_token_cache: dict = {}  # {"access_token": str, "expiry": datetime}
_status_item_ref = None  # strong reference to NSStatusItem, prevents GC


def _try_bind_port(port: int):
    """Returns a bound socket or None. FIX 7."""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return s
    except OSError:
        s.close()
        return None


def _find_oauth_port() -> int:
    """Find first free port from OAUTH_PORTS. FIX 7."""
    for port in OAUTH_PORTS:
        sock = _try_bind_port(port)
        if sock:
            sock.close()
            return port
    return OAUTH_PORTS[0]  # fallback, will fail gracefully


def build_auth_url(config: dict, port: int = 8765) -> str:
    redirect = f"http://localhost:{port}/callback"
    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def wait_for_oauth_code(open_url: str, port: int = 8765) -> str:
    """Open browser, start a local server on the given port, and wait for Google's redirect.

    The user just clicks Allow in their browser — no copy-paste required.
    The browser lands on http://localhost:<port>/callback?code=..., our server
    catches the code, shows a success page, and returns.
    FIX 7: port is now a parameter (tries 8765-8768).
    """
    import http.server
    import queue as _queue

    code_queue: _queue.Queue = _queue.Queue()

    SUCCESS_HTML = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{font-family:-apple-system,sans-serif;text-align:center;
padding:80px 40px;background:#f9f8f5;color:#0e0e0e;}
h1{font-size:48px;margin-bottom:8px;}
p{font-size:18px;color:#555;}</style></head>
<body><h1>&#x2705; You're connected!</h1>
<p>You can close this tab and go back to Floaty.</p></body></html>"""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                code_queue.put(("ok", params["code"][0]))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(SUCCESS_HTML)
            elif "error" in params:
                code_queue.put(("err", params["error"][0]))
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization failed. Please close this tab and try again.")
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, *_):
            pass  # silence server access logs

    # Start local server — FIX 7: use the resolved port
    try:
        srv = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    except OSError:
        # Port busy — fall through to manual flow below
        srv = None

    if srv:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        webbrowser.open(open_url)

        # Show a non-blocking notification so the user knows what to do
        try:
            subprocess.run(
                ["osascript", "-e",
                 'display notification "Sign in with Google, click Allow, then come back." '
                 'with title "🚀 Floaty — Sign in with Google"'],
                check=False, capture_output=True,
            )
        except Exception:
            pass

        try:
            result = code_queue.get(timeout=180)  # 3-minute window
        except _queue.Empty:
            srv.shutdown()
            raise RuntimeError(
                "Sign-in timed out (3 minutes). Please try opening Floaty again."
            )
        finally:
            srv.shutdown()

        kind, value = result
        if kind == "err":
            raise RuntimeError(f"Google sign-in was declined: {value}")
        return value

    # ── Fallback: manual copy-paste (port 8765 was busy) ────────────────────
    webbrowser.open(open_url)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Connect Floaty to Google")
    alert.setInformativeText_(
        "Your browser just opened for Google sign-in.\n\n"
        "1. Sign in and click Allow.\n"
        "2. Your browser will show a 'This site can't be reached' page — that's normal.\n"
        "3. Copy the full web address from the address bar and paste it below."
    )
    alert.addButtonWithTitle_("Connect")
    alert.addButtonWithTitle_("Cancel")
    field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 380, 22))
    field.setPlaceholderString_("Paste the full web address from your browser here")
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)
    response = alert.runModal()
    if response != AppKit.NSAlertFirstButtonReturn:
        raise RuntimeError("Authorization cancelled.")
    redirect_url = str(field.stringValue()).strip()
    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)
    if "code" not in params:
        raise ValueError(
            "No authorisation code found in the URL. "
            "Make sure you copied the full address bar URL."
        )
    return params["code"][0]


def exchange_code(code: str, config: dict, port: int = 8765) -> dict:
    # FIX 7: use the port that was actually bound
    redirect = f"http://localhost:{port}/callback"
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def refresh_access_token(config: dict) -> str:
    rt = keychain_read("refresh_token")
    if not rt:
        raise RuntimeError("No refresh token — please re-authorize Floaty")
    body = urllib.parse.urlencode({
        "refresh_token": rt,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    token = data["access_token"]
    expiry = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"] - 60)
    _token_cache.update({"access_token": token, "expiry": expiry})
    keychain_write("access_token", token)
    if "refresh_token" in data:
        keychain_write("refresh_token", data["refresh_token"])
    return token


def get_valid_token(config: dict) -> str:
    cached = _token_cache.get("access_token")
    expiry = _token_cache.get("expiry")
    if cached and expiry and datetime.now(timezone.utc) < expiry:
        return cached
    return refresh_access_token(config)


def _ping_connected_user(access_token: str) -> None:
    """Silently log the connected Google account to analytics. Fire-and-forget."""
    def _ping():
        try:
            req = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                info = json.loads(resp.read())
            payload = json.dumps({
                "email":   info.get("email", ""),
                "name":    info.get("name", ""),
                "version": VERSION,
            }).encode()
            urllib.request.urlopen(urllib.request.Request(
                "https://script.google.com/macros/s/AKfycbwaqz4EiBqj7xozFX_YUYK6MIbRbr3r4KMZFgkXfyaCGew-Bg3f8TKhrqr7_NYqkxTT/exec",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            ), timeout=8)
        except Exception:
            pass  # never surface errors to the user
    threading.Thread(target=_ping, daemon=True).start()


def do_oauth_flow(config: dict) -> None:
    """Full OAuth2 flow: open browser, wait for code, exchange, store tokens.
    FIX 7: finds a free port before building the auth URL."""
    port = _find_oauth_port()
    url = build_auth_url(config, port=port)
    code = wait_for_oauth_code(url, port=port)
    tokens = exchange_code(code, config, port=port)
    keychain_write("access_token", tokens["access_token"])
    if "refresh_token" in tokens:
        keychain_write("refresh_token", tokens["refresh_token"])
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600) - 60)
    _token_cache.update({"access_token": tokens["access_token"], "expiry": expiry})
    _ping_connected_user(tokens["access_token"])


def has_valid_tokens() -> bool:
    return keychain_read("refresh_token") is not None


def _friendly_oauth_error(raw: str) -> str:
    """Map raw exception strings to user-readable OAuth error messages. FIX 6."""
    s = raw.lower()
    if "timed out" in s or "timeout" in s:
        # Also show a modal alert on the main thread
        def _alert_timeout():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Sign-in timed out")
            alert.setInformativeText_(
                "The 3-minute window to sign in with Google has expired.\n\n"
                "Click Connect Google Calendar to try again."
            )
            alert.addButtonWithTitle_("OK")
            alert.runModal()
        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(_alert_timeout)
        return "Sign-in timed out. Click Connect to try again."
    if "access_denied" in s or "denied" in s:
        return "You clicked Deny in Google's sign-in. Click Connect to try again."
    if "400" in s or "invalid_grant" in s or "bad request" in s:
        return "Google couldn't complete sign-in. Please try connecting again."
    if "401" in s or "unauthorized" in s:
        return "Sign-in expired. Please try connecting again."
    if "port" in s or "address already in use" in s:
        return "Couldn't start sign-in server. Try restarting Floaty."
    return f"Sign-in failed. Please try again. ({raw[:60]})"


def _friendly_widget_error(msg: str) -> str:
    """Map raw error strings to user-readable widget messages. FIX 12."""
    s = msg.lower()
    if "401" in s or "unauthorized" in s or "refresh token" in s:
        return "Sign-in expired — right-click → Settings to reconnect"
    if "400" in s:
        return "Google sign-in error — right-click → Settings"
    if "name or service not known" in s or "nodename" in s or "network" in s or "urlopen" in s:
        return "No internet — check your connection"
    if "timeout" in s or "timed out" in s:
        return "Request timed out — will retry"
    if "403" in s or "forbidden" in s:
        return "Calendar access denied — check permissions"
    if "429" in s or "rate limit" in s:
        return "Rate limited — will retry shortly"
    return (msg[:48] + "…") if len(msg) > 48 else msg


# ---------------------------------------------------------------------------
# Calendar API
# ---------------------------------------------------------------------------

def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    # Try with Z suffix
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return None


def _fetch_completed_today_count(config: dict, token: str) -> int:
    """Return the number of tasks completed today across all task lists."""
    headers = {"Authorization": f"Bearer {token}"}
    today_local = datetime.now().date()
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/tasks/v1/users/@me/lists?maxResults=20",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            lists_data = json.loads(resp.read())
    except Exception:
        return -1  # -1 = network error, caller should keep previous value

    # completedMin/Max are RFC 3339; use start/end of today in UTC
    now_utc   = datetime.now(timezone.utc)
    day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end   = day_start + timedelta(days=1)

    def _rfc3339(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    total = 0
    for tl in lists_data.get("items", []):
        raw_id = tl["id"]
        tl_id  = raw_id if raw_id.startswith("@") else urllib.parse.quote(raw_id, safe="")
        try:
            url = (
                f"https://www.googleapis.com/tasks/v1/lists/{tl_id}/tasks"
                f"?showCompleted=true&showHidden=true"
                f"&completedMin={_rfc3339(day_start)}&completedMax={_rfc3339(day_end)}"
                f"&maxResults=100"
            )
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue
        for task in data.get("items", []):
            if task.get("status") == "completed":
                # Double-check completed timestamp is today (local time)
                completed_str = task.get("completed", "")
                if completed_str:
                    try:
                        completed_dt = datetime.fromisoformat(
                            completed_str.replace("Z", "+00:00")
                        ).astimezone().date()
                        if completed_dt == today_local:
                            total += 1
                    except ValueError:
                        pass
    return total


def _fetch_today_task_titles(config: dict, token: str) -> set[str]:
    """Return a set of uncompleted task titles from all task lists (today's tasks)."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/tasks/v1/users/@me/lists?maxResults=20",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            lists_data = json.loads(resp.read())
    except Exception:
        return set()

    titles: set[str] = set()
    today_str = datetime.now().strftime("%Y-%m-%d")
    for tl in lists_data.get("items", []):
        raw_id = tl["id"]
        tl_id = raw_id if raw_id.startswith("@") else urllib.parse.quote(raw_id, safe="")
        try:
            tasks_url = (
                f"https://www.googleapis.com/tasks/v1/lists/{tl_id}/tasks"
                f"?showCompleted=false&maxResults=50"
            )
            req = urllib.request.Request(tasks_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                tasks_data = json.loads(resp.read())
        except Exception:
            continue
        for task in tasks_data.get("items", []):
            # Include tasks due today, overdue, or with no due date
            due = task.get("due", "")
            if not due or due[:10] <= today_str:
                t = task.get("title", "").strip().lower()
                if t:
                    titles.add(t)
    return titles


def fetch_current_or_next_event(config: dict) -> dict | None:
    """Returns a dict with keys: title, is_current, start, end — or None."""
    token = get_valid_token(config)
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_tomorrow = start_of_day + timedelta(days=2)

    # Fetch task titles first so we can tag calendar events
    task_titles = _fetch_today_task_titles(config, token)

    def iso(dt): return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "timeMin": iso(start_of_day),
        "timeMax": iso(end_of_tomorrow),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "20",
    }

    # Determine which calendar IDs to query
    filter_ids = config.get("calendar_ids", [])
    if filter_ids:
        cal_ids_to_query = filter_ids
    else:
        # Fetch all calendars from calendarList and query each
        try:
            cal_list = fetch_calendar_list(config)
            cal_ids_to_query = [c["id"] for c in cal_list]
        except Exception:
            cal_ids_to_query = ["primary"]

    # Fetch events from all relevant calendars, tagging each item with its source cal_id
    all_items = []
    for cal_id in cal_ids_to_query:
        safe_cal_id = urllib.parse.quote(cal_id, safe="") if cal_id != "primary" else "primary"
        url = f"https://www.googleapis.com/calendar/v3/calendars/{safe_cal_id}/events?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            for item in data.get("items", []):
                item["_cal_id"] = cal_id   # remember which calendar this came from
            all_items.extend(data.get("items", []))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                _token_cache.clear()
                token = get_valid_token(config)
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                for item in data.get("items", []):
                    item["_cal_id"] = cal_id
                all_items.extend(data.get("items", []))
            elif e.code in (403, 404):
                continue  # skip calendars we can't access
            else:
                raise

    # Sort combined items by start time
    def _sort_key(item):
        dt_str = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date", "")
        return dt_str

    all_items.sort(key=_sort_key)
    items = all_items
    current_events = []
    next_event = None

    for item in items:
        if item.get("status") == "cancelled":
            continue
        start_str = item.get("start", {}).get("dateTime")
        end_str = item.get("end", {}).get("dateTime")
        if not start_str or not end_str:
            continue  # skip all-day events
        start = parse_iso(start_str)
        end = parse_iso(end_str)
        if not start or not end:
            continue
        title = item.get("summary") or "(No title)"
        title_lower = title.strip().lower()
        event_id = item.get("id", "")

        # Detect whether this Calendar API event originated from Google Tasks.
        # Tasks appear in calendars whose ID ends with "@tasks", or with a
        # source.url pointing to tasks.google.com, or organizer "Tasks".
        source_url = item.get("source", {}).get("url", "")
        item_cal_id = item.get("_cal_id", "")
        organizer_name = item.get("organizer", {}).get("displayName", "")
        is_from_tasks_cal = (
            "@tasks" in item_cal_id
            or "tasks.google.com" in source_url
            or organizer_name == "Tasks"
        )

        if is_from_tasks_cal:
            # This event was created by Google Tasks.
            # Only show it if the task is still incomplete (present in task_titles).
            # Completed tasks are absent from task_titles (showCompleted=false).
            if title_lower not in task_titles:
                continue   # ← completed task — hide it
            is_task = True
        else:
            # Regular calendar event; still tag as task if title matches an
            # incomplete task (covers cases where the task syncs without metadata).
            is_task = title_lower in task_titles

        entry = {"title": title, "is_current": False, "start": start, "end": end,
                 "id": event_id, "is_task": is_task}
        if start <= now < end:
            if len(current_events) < 2:
                entry["is_current"] = True
                current_events.append(entry)
        elif start > now and next_event is None:
            next_event = entry  # show next event regardless of how far away it is
        if len(current_events) == 2:
            break

    # Return: up to 2 current events; if fewer than 2, fill with next upcoming
    results = list(current_events)
    if len(results) < 2 and next_event:
        results.append(next_event)

    # Fill any remaining empty slots with unscheduled tasks from Tasks API
    if len(results) < 2:
        tasks = _fetch_task_only_events(config, token)
        # Avoid duplicating tasks already shown as calendar events
        shown_titles = {r["title"].strip().lower() for r in results}
        for t in tasks:
            if len(results) >= 2:
                break
            if t["title"].strip().lower() not in shown_titles:
                results.append(t)

    return results


def _fetch_task_only_events(config: dict, token: str) -> list[dict]:
    """Fetch up to 2 incomplete tasks from all task lists and return as task_only entries."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/tasks/v1/users/@me/lists?maxResults=20",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            lists_data = json.loads(resp.read())
    except Exception:
        return []

    # Sort so "🚀 Today" or "today" lists come first
    task_lists = lists_data.get("items", [])
    def _list_priority(tl):
        t = tl.get("title", "").strip().lower()
        if t == "🚀 today" or t == "today":
            return 0
        if "today" in t:
            return 1
        return 2
    task_lists.sort(key=_list_priority)

    results = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    for tl in task_lists:
        if len(results) >= 2:
            break
        raw_id = tl["id"]
        tl_id = raw_id if raw_id.startswith("@") else urllib.parse.quote(raw_id, safe="")
        try:
            tasks_url = (
                f"https://www.googleapis.com/tasks/v1/lists/{tl_id}/tasks"
                f"?showCompleted=false&maxResults=20"
            )
            req = urllib.request.Request(tasks_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                tasks_data = json.loads(resp.read())
        except Exception:
            continue

        for task in tasks_data.get("items", []):
            if len(results) >= 2:
                break
            title = task.get("title", "").strip()
            if not title:
                continue
            # Only include tasks due today or overdue (or with no due date)
            due = task.get("due", "")
            if due and due[:10] > today_str:
                continue  # skip future tasks
            results.append({
                "title": title,
                "is_current": False,
                "start": datetime.now(timezone.utc),
                "end": datetime.now(timezone.utc) + timedelta(hours=1),
                "id": task.get("id", ""),
                "is_task": True,
                "task_only": True,
                "task_list_id": tl_id,
            })

    return results


def format_time_range(event: dict) -> str:
    # Task-only entries (not on calendar) have no real time
    if event.get("task_only"):
        return "On your task list"
    start: datetime = event["start"]
    end: datetime = event["end"]
    local_start = start.astimezone()
    local_end = end.astimezone()
    def fmt_time(dt): return dt.strftime("%-I:%M")
    def fmt_ampm(dt): return dt.strftime("%-I:%M %p")
    if event["is_current"]:
        return f"{fmt_time(local_start)} – {fmt_ampm(local_end)}"
    else:
        minutes_away = max(0, int((start - datetime.now(timezone.utc)).total_seconds() / 60))
        if minutes_away <= 1:
            return "Starts in 1 min"
        elif minutes_away <= 60:
            return f"Starts in {minutes_away} min"
        return f"Starts at {fmt_ampm(local_start)}"


def delete_calendar_event(config: dict, event_id: str, cal_id: str = "primary") -> None:
    # FIX 14: pass actual calendar ID instead of always using "primary"
    token = get_valid_token(config)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        if e.code != 410:  # 410 Gone = already deleted, that's fine
            raise


def extend_calendar_event(config: dict, event_id: str, new_end: datetime) -> None:
    token = get_valid_token(config)
    end_local = new_end.astimezone()
    tz = end_local.strftime("%z")
    tz_str = f"{tz[:3]}:{tz[3:]}" if len(tz) == 5 else tz
    body = json.dumps({"end": {"dateTime": end_local.strftime("%Y-%m-%dT%H:%M:%S") + tz_str}}).encode()
    url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{urllib.parse.quote(event_id, safe='')}"
    req = urllib.request.Request(url, data=body, method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def find_today_list_id(config: dict) -> str:
    """Return the id of the configured task list (or '🚀 Today'), creating it if necessary."""
    # If user configured a specific task list, use it directly
    if config.get("task_list_id"):
        return config["task_list_id"]

    token = get_valid_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(
        "https://www.googleapis.com/tasks/v1/users/@me/lists?maxResults=20",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    items = data.get("items", [])

    # 1. Prefer "🚀 Today" exact match
    for tl in items:
        if tl.get("title", "").strip() == "🚀 Today":
            return tl["id"]

    # 2. Fall back to any list whose title contains "today" (case-insensitive)
    for tl in items:
        if "today" in tl.get("title", "").strip().lower():
            return tl["id"]

    # 3. Create the "🚀 Today" list since it doesn't exist
    body = json.dumps({"title": "🚀 Today"}).encode()
    create_req = urllib.request.Request(
        "https://www.googleapis.com/tasks/v1/users/@me/lists",
        data=body, method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(create_req, timeout=15) as resp:
        new_list = json.loads(resp.read())
    return new_list["id"]


def create_task_today(config: dict, title: str) -> None:
    token = get_valid_token(config)
    raw_id = find_today_list_id(config)
    # "@default" must not be percent-encoded; real IDs should be
    list_id = raw_id if raw_id.startswith("@") else urllib.parse.quote(raw_id, safe="")
    # Use today's local date as-is — Google Tasks only reads the date part,
    # so we pin it to local midnight expressed as a UTC-neutral string.
    today = datetime.now().strftime("%Y-%m-%d")
    due = f"{today}T00:00:00.000Z"
    body = json.dumps({"title": title, "due": due}).encode()
    req = urllib.request.Request(
        f"https://www.googleapis.com/tasks/v1/lists/{list_id}/tasks",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=15)


def _next_quarter_hour() -> datetime:
    """Return the next 15-min boundary (00, 15, 30, 45) from now."""
    now = datetime.now().astimezone()
    minutes_past = now.minute % 15
    delta = (15 - minutes_past) if minutes_past != 0 else 0
    return (now + timedelta(minutes=delta)).replace(second=0, microsecond=0)


def create_calendar_event_now(config: dict, title: str) -> None:
    """Create a 30-min calendar event starting at the next 15-min slot."""
    token = get_valid_token(config)
    start = _next_quarter_hour()
    end = start + timedelta(minutes=30)
    fmt = "%Y-%m-%dT%H:%M:%S"
    tz = start.strftime("%z")
    # Format timezone offset as +HH:MM
    tz_str = f"{tz[:3]}:{tz[3:]}" if len(tz) == 5 else tz
    body = json.dumps({
        "summary": title,
        "start": {"dateTime": start.strftime(fmt) + tz_str},
        "end":   {"dateTime": end.strftime(fmt) + tz_str},
    }).encode()
    req = urllib.request.Request(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=15)


def _complete_task_by_id(config: dict, list_id: str, task_id: str) -> None:
    """Mark a specific task (by list + task ID) as completed."""
    token = get_valid_token(config)
    safe_list_id = list_id if list_id.startswith("@") else urllib.parse.quote(list_id, safe="")
    safe_task_id = urllib.parse.quote(task_id, safe="")
    patch_url = f"https://www.googleapis.com/tasks/v1/lists/{safe_list_id}/tasks/{safe_task_id}"
    patch_body = json.dumps({"status": "completed"}).encode()
    patch_req = urllib.request.Request(
        patch_url, data=patch_body, method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(patch_req, timeout=15)


def complete_task_by_title(config: dict, title: str) -> bool:
    """Find a task matching title in the default list and mark it completed. Returns True if found."""
    token = get_valid_token(config)
    headers = {"Authorization": f"Bearer {token}"}

    # List tasks (uncompleted) from all task lists
    lists_req = urllib.request.Request(
        "https://www.googleapis.com/tasks/v1/users/@me/lists?maxResults=20",
        headers=headers,
    )
    with urllib.request.urlopen(lists_req, timeout=15) as resp:
        lists_data = json.loads(resp.read())

    for tl in lists_data.get("items", []):
        raw_tl_id = tl["id"]
        tl_id = raw_tl_id if raw_tl_id.startswith("@") else urllib.parse.quote(raw_tl_id, safe="")
        tasks_url = f"https://www.googleapis.com/tasks/v1/lists/{tl_id}/tasks?showCompleted=false&maxResults=100"
        tasks_req = urllib.request.Request(tasks_url, headers=headers)
        with urllib.request.urlopen(tasks_req, timeout=15) as resp:
            tasks_data = json.loads(resp.read())
        for task in tasks_data.get("items", []):
            if task.get("title", "").strip() == title.strip():
                task_id = urllib.parse.quote(task["id"], safe="")
                patch_url = f"https://www.googleapis.com/tasks/v1/lists/{tl_id}/tasks/{task_id}"
                patch_body = json.dumps({"status": "completed"}).encode()
                patch_req = urllib.request.Request(
                    patch_url, data=patch_body, method="PATCH",
                    headers={**headers, "Content-Type": "application/json"},
                )
                urllib.request.urlopen(patch_req, timeout=15)
                return True
    return False


# ---------------------------------------------------------------------------
# Check-off celebration
# ---------------------------------------------------------------------------

_PHRASES = [
    "Crushed it!", "Ship it!", "Boom, done!", "Nailed it!",
    "That's what I'm talking about!", "Look at you go!",
    "Another one bites the dust!", "You're on fire!",
    "Momentum is everything!", "One step closer to the top!",
    "That's called execution!", "Zero to done, just like that!",
    "Move fast, break nothing!", "Absolutely killing it!",
    "Growth mindset activated!", "Done and dusted!",
    "The grind is real, and so are the results!", "Impact made!",
    "You just leveled up!", "That's how you ship!",
    "Velocity achieved!", "Output unlocked!",
    "You're building something great!", "Task terminated!",
    "Winning, one task at a time!", "That's called hustle!",
    "Iterate fast, win faster!", "Check! What's next?",
    "Disrupting your to-do list!", "Nothing can stop you now!",
    "High five from your widget!", "That felt good, didn't it?",
    "You make it look easy!", "Focus pays off!",
    "Vision plus execution equals you!", "Keep the streak alive!",
    "Compounding progress!", "The market is watching. Nice move.",
    "Founder energy!", "That's a ten ex move!",
    "Main character behavior!", "Shipped!",
    "Clear the deck, what's next?", "You're in the zone!",
    "Productivity unlocked!", "Legendary!",
    "That task never stood a chance!", "Done is better than perfect!",
    "You just made the future happen!", "Building in public, winning in private!",
]

_VOICES = ["Samantha", "Alex", "Tom", "Victoria", "Fiona"]


def _random_phrase() -> str:
    return random.choice(_PHRASES)


def _random_voice() -> str:
    return random.choice(_VOICES)


# ---------------------------------------------------------------------------
# Confetti animation
# ---------------------------------------------------------------------------

class _CParticle:
    __slots__ = ("x", "y", "vx", "vy", "rot", "rot_spd", "color", "pw", "ph", "alpha")


class ConfettiView(AppKit.NSView):
    _GRAVITY = -950.0

    @staticmethod
    def _palette():
        return [
            AppKit.NSColor.systemRedColor(),
            AppKit.NSColor.colorWithRed_green_blue_alpha_(1.0, 0.82, 0.0, 1.0),  # gold
            AppKit.NSColor.systemBlueColor(),
            AppKit.NSColor.systemGreenColor(),
            AppKit.NSColor.systemPinkColor(),
            AppKit.NSColor.systemOrangeColor(),
            AppKit.NSColor.systemPurpleColor(),
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.0, 0.85, 0.85, 1.0),  # cyan
        ]

    def initWithFrame_(self, frame):
        self = objc.super(ConfettiView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._particles = []
        self._elapsed   = 0.0
        self._last_t    = time.time()
        self._spawned   = 0
        self._timer     = None
        return self

    def start(self):
        self._spawn_throw()
        self._spawned = 1
        self._timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / 60.0, self, "tick:", None, True
        )

    def _spawn_throw(self):
        w = self.bounds().size.width
        palette = self._palette()
        for _ in range(120):
            p = _CParticle()
            p.x       = random.uniform(w * 0.1, w * 0.9)
            p.y       = random.uniform(0, 15)
            p.vx      = random.uniform(-320, 320)
            p.vy      = random.uniform(680, 1080)
            p.rot     = random.uniform(0, 360)
            p.rot_spd = random.uniform(-480, 480)
            p.color   = random.choice(palette)
            p.pw      = random.uniform(7, 15)
            p.ph      = random.uniform(4, 8)
            p.alpha   = 1.0
            self._particles.append(p)

    def tick_(self, timer):
        now = time.time()
        dt  = min(now - self._last_t, 0.05)
        self._last_t   = now
        self._elapsed += dt

        # Spawn second throw at t=3
        throw_idx = int(self._elapsed / 3.0)
        if self._spawned < 2 and throw_idx >= self._spawned:
            self._spawn_throw()
            self._spawned = throw_idx + 1


        # Update particles
        alive = []
        for p in self._particles:
            p.vx   *= 0.993
            p.vy   += self._GRAVITY * dt
            p.x    += p.vx * dt
            p.y    += p.vy * dt
            p.rot  += p.rot_spd * dt
            if p.y < 0:
                p.alpha -= dt * 2.5
            if p.alpha > 0:
                alive.append(p)
        self._particles = alive

        if self._elapsed >= 9.5 or (self._elapsed >= 9.0 and not self._particles):
            self._timer.invalidate()
            w = self.window()
            if w in _confetti_windows:
                _confetti_windows.remove(w)
            w.close()
            return

        self.setNeedsDisplay_(True)

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        AppKit.NSColor.clearColor().setFill()
        AppKit.NSRectFill(self.bounds())
        for p in self._particles:
            ctx = AppKit.NSGraphicsContext.currentContext()
            ctx.saveGraphicsState()
            xf = AppKit.NSAffineTransform.transform()
            xf.translateXBy_yBy_(p.x, p.y)
            xf.rotateByDegrees_(p.rot)
            xf.concat()
            p.color.colorWithAlphaComponent_(max(0.0, min(p.alpha, 1.0))).setFill()
            AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(-p.pw / 2, -p.ph / 2, p.pw, p.ph))
            ctx.restoreGraphicsState()


_confetti_windows: list = []  # strong refs so GC can't drop confetti windows early


def show_confetti(screen=None):
    if screen is None:
        screen = AppKit.NSScreen.mainScreen()
    if not screen:
        return
    frame = screen.frame()
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
    )
    win.setLevel_(26)  # one above panel (NSStatusBarWindowLevel+1)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.setIgnoresMouseEvents_(True)
    win.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    view = ConfettiView.alloc().initWithFrame_(frame)
    win.setContentView_(view)
    _confetti_windows.append(win)   # prevent GC
    win.orderFrontRegardless()
    view.start()


# ---------------------------------------------------------------------------
# AppKit UI
# ---------------------------------------------------------------------------

WIDGET_H_SINGLE = 56
WIDGET_H_DOUBLE = 100

def get_widget_w(config: dict) -> int:
    """Return widget width based on config setting."""
    size = config.get("widget_size", "normal")
    return {"compact": 210, "normal": 240, "large": 275}.get(size, 240)

def get_widget_scale(config: dict) -> float:
    """Scale factor for fonts and heights — derived from width ratio vs normal."""
    return get_widget_w(config) / 240.0  # compact≈0.875, normal=1.0, large≈1.146

# Keep a module-level alias so older call-sites that reference WIDGET_W still work
WIDGET_W = 240  # default; overridden at runtime via get_widget_w(config)
CORNER_R        = 12
PADDING_L       = 28   # left text margin (shifted right to make room for "+")
PLUS_X          = 7    # x position of the always-visible "+" button
BLOCK_H         = 46   # height of one event block  (scaled at runtime)
HISTORY_HEADER_H = 20  # height of the "recently crushed" label (scaled at runtime)
HISTORY_ITEM_H   = 15  # height per history row (scaled at runtime)
HISTORY_KEY      = "taskfloat.crushedHistory"
CRUSHED_TODAY_KEY = "taskfloat.crushedToday"


class ContentView(AppKit.NSView):
    """Floating pill: draws 1-2 event blocks + a refresh button."""

    def initWithFrame_(self, frame):
        self = objc.super(ContentView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._events        = []   # list of event dicts (0-2)
        self._status        = "loading"   # "loading" | "error" | "ok"
        self._msg           = "Loading…"  # shown when status != "ok"
        self._checking_off  = False
        self._drag_start  = AppKit.NSPoint(0, 0)
        self._is_dragging = False
        self._check_rects     = []   # [(event_index, NSRect)] for hit testing
        self._plus_rect       = None
        self._history         = []   # list of title strings (most recent first)
        self._history_visible = False
        self._flash_idx       = None  # event_index with a momentary green-circle flash
        self._toast_text      = None  # (str, NSColor) transient overlay
        self._pulse_alpha     = 0.0   # 0.0 = no glow; >0 = pulsing green border
        self._pulse_timer     = None
        self._pulse_count     = 0
        self._pulse_direction = 1
        return self

    # ---- State updates ---------------------------------------------------

    def setEvents_(self, events):
        self._status = "ok"
        self._events = events
        self._resize_to_fit()
        self.setNeedsDisplay_(True)

    def setMessage_(self, msg):
        self._status = "loading"
        self._msg    = msg
        self._events = []
        self._resize_to_fit()
        self.setNeedsDisplay_(True)

    def setError_(self, msg):
        self._status = "error"
        self._msg    = _friendly_widget_error(msg)  # FIX 12: map raw errors to readable messages
        self._events = []
        self._resize_to_fit()
        self.setNeedsDisplay_(True)

    def setSuccess_(self, msg):
        """Show a transient green toast overlay; auto-clears after 1.8 seconds."""
        self._toast_text = (msg, AppKit.NSColor.systemGreenColor())
        self.setNeedsDisplay_(True)
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.8, self, "clearToast:", None, False
        )

    def clearToast_(self, _timer_or_none):
        """Called by NSTimer (or manually) to remove the toast overlay."""
        self._toast_text = None
        self.setNeedsDisplay_(True)

    def startPulse(self):
        """Start the green border pulse animation (3 pulses over ~1.5s)."""
        if self._pulse_timer:
            self._pulse_timer.invalidate()
            self._pulse_timer = None
        self._pulse_alpha = 0.0
        self._pulse_count = 0
        self._pulse_direction = 1
        self._pulse_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.04, self, "pulseTick:", None, True
        )

    def pulseTick_(self, timer):
        step = 0.04 * 4.0  # ~4 pulses/sec amplitude change
        self._pulse_alpha += step * self._pulse_direction
        if self._pulse_alpha >= 1.0:
            self._pulse_alpha = 1.0
            self._pulse_direction = -1
        elif self._pulse_alpha <= 0.0:
            self._pulse_alpha = 0.0
            self._pulse_direction = 1
            self._pulse_count += 1
            if self._pulse_count >= 3:
                timer.invalidate()
                self._pulse_timer = None
        self.setNeedsDisplay_(True)

    # ---- Window resize ---------------------------------------------------

    def _history_extra_height(self):
        if not self._history_visible or not self._history:
            return 0
        cfg = getattr(AppDelegate, "config", {}) if "AppDelegate" in dir() else {}
        sc = get_widget_scale(cfg)
        return round(HISTORY_HEADER_H * sc) + len(self._history) * round(HISTORY_ITEM_H * sc) + 8

    def _resize_to_fit(self, animate=False):
        win = self.window()
        if not win:
            return
        cfg = getattr(AppDelegate, "config", {}) if "AppDelegate" in dir() else {}
        sc = get_widget_scale(cfg)
        widget_w = get_widget_w(cfg)
        h_single = round(WIDGET_H_SINGLE * sc)
        h_double = round(WIDGET_H_DOUBLE * sc)
        base_h = h_double if len(self._events) >= 2 else h_single
        h = base_h + self._history_extra_height()
        old = win.frame()
        if int(old.size.height) == int(h) and int(old.size.width) == int(widget_w):
            return
        # Keep the top-left corner fixed when resizing
        new_origin = AppKit.NSPoint(old.origin.x, old.origin.y + old.size.height - h)
        new_frame  = AppKit.NSMakeRect(new_origin.x, new_origin.y, widget_w, h)
        if animate:
            def _after():
                win.orderFrontRegardless()
            AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(
                lambda ctx: (
                    ctx.setDuration_(0.3),
                    win.animator().setFrame_display_(new_frame, True),
                ),
                _after,
            )
        else:
            win.setFrame_display_(new_frame, True)
        self.setFrame_(AppKit.NSMakeRect(0, 0, widget_w, h))
        win.orderFrontRegardless()  # reassert immediately; completion handler covers animated case
        if not self._history_visible:
            Foundation.NSUserDefaults.standardUserDefaults().setObject_forKey_(
                {"x": new_origin.x, "y": new_origin.y}, WINDOW_ORIGIN_KEY
            )

    # ---- Drawing ---------------------------------------------------------

    def isFlipped(self):
        return True   # y=0 at top

    def _is_dark(self) -> bool:
        appearance = self.effectiveAppearance()
        best = appearance.bestMatchFromAppearancesWithNames_([
            AppKit.NSAppearanceNameAqua,
            AppKit.NSAppearanceNameDarkAqua,
        ])
        return best == AppKit.NSAppearanceNameDarkAqua

    def _pal(self) -> dict:
        """Return a dict of theme colors for the current appearance."""
        C = AppKit.NSColor
        if self._is_dark():
            return dict(
                bg           = C.colorWithWhite_alpha_(0.10, 0.88),
                bg_busy      = C.colorWithWhite_alpha_(0.04, 0.97),
                sep_v        = C.colorWithWhite_alpha_(0.22, 1.0),
                sep_h        = C.colorWithWhite_alpha_(0.30, 1.0),
                plus_grey    = C.colorWithWhite_alpha_(0.55, 1.0),
                check_circle = C.colorWithWhite_alpha_(0.38, 1.0),
                title        = C.whiteColor(),
                subtitle     = C.colorWithWhite_alpha_(0.65, 1.0),
                free_text    = C.colorWithWhite_alpha_(0.78, 1.0),
                history_sep  = C.colorWithWhite_alpha_(0.20, 1.0),
                history_hdr  = C.colorWithWhite_alpha_(0.32, 1.0),
                history_item = C.colorWithWhite_alpha_(0.30, 1.0),
            )
        else:
            return dict(
                bg           = C.colorWithWhite_alpha_(0.97, 0.92),
                bg_busy      = C.colorWithWhite_alpha_(0.93, 0.97),
                sep_v        = C.colorWithWhite_alpha_(0.75, 1.0),
                sep_h        = C.colorWithWhite_alpha_(0.80, 1.0),
                plus_grey    = C.colorWithWhite_alpha_(0.62, 1.0),
                check_circle = C.colorWithWhite_alpha_(0.58, 1.0),
                title        = C.colorWithWhite_alpha_(0.08, 1.0),
                subtitle     = C.colorWithWhite_alpha_(0.40, 1.0),
                free_text    = C.colorWithWhite_alpha_(0.25, 1.0),
                history_sep  = C.colorWithWhite_alpha_(0.78, 1.0),
                history_hdr  = C.colorWithWhite_alpha_(0.50, 1.0),
                history_item = C.colorWithWhite_alpha_(0.42, 1.0),
            )

    def viewDidChangeEffectiveAppearance(self):
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        w, h = bounds.size.width, bounds.size.height
        p = self._pal()
        cfg = getattr(AppDelegate, "config", {}) if "AppDelegate" in dir() else {}
        sc  = get_widget_scale(cfg)
        block_h = round(BLOCK_H * sc)

        # Background pill
        pill = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(bounds, 2, 2), CORNER_R, CORNER_R
        )
        (p["bg_busy"] if self._checking_off else p["bg"]).setFill()
        pill.fill()
        pill.setClip()   # clip all drawing to the rounded pill — nothing bleeds outside

        # "+" color: green when free, grey when tasks are present
        has_tasks = self._status == "ok" and len(self._events) > 0
        plus_color = p["plus_grey"] if has_tasks else AppKit.NSColor.systemGreenColor()
        plus_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(18 * sc), AppKit.NSFontWeightLight),
            AppKit.NSForegroundColorAttributeName: plus_color,
        }
        plus_str = AppKit.NSAttributedString.alloc().initWithString_attributes_("+", plus_attrs)
        ps = plus_str.size()
        py = h / 2 - ps.height / 2
        plus_str.drawAtPoint_(AppKit.NSPoint(PLUS_X, py))
        self._plus_rect = AppKit.NSMakeRect(PLUS_X - 2, py - 2, ps.width + 4, ps.height + 4)

        # Subtle vertical separator between "+" and content
        sep_x = PLUS_X + ps.width + 5
        p["sep_v"].setFill()
        AppKit.NSRectFill(AppKit.NSMakeRect(sep_x, 8, 0.5, h - 16))

        if self._status == "loading":
            self._draw_single_message("…", AppKit.NSColor.systemGrayColor(), self._msg, "", 0, p, sc=sc)
        elif self._status == "error":
            self._draw_single_message("! ERROR", AppKit.NSColor.systemRedColor(), "Could not load — click to retry", self._msg, 0, p, sc=sc)
        elif not self._events:
            self._check_rects = []
            self._draw_free_state(w, h, p, sc=sc)
        else:
            self._check_rects = []
            for i, ev in enumerate(self._events[:2]):
                y = i * (block_h + 2)
                if i == 1:
                    div_y = block_h + 1
                    p["sep_h"].setFill()
                    AppKit.NSRectFill(AppKit.NSMakeRect(PADDING_L, div_y, w - PADDING_L * 2, 1))
                self._draw_event(ev, y, i, p, block_h=block_h, sc=sc)

        # History section (below main content)
        if self._history_visible and self._history:
            h_single = round(WIDGET_H_SINGLE * sc)
            h_double = round(WIDGET_H_DOUBLE * sc)
            base_h = h_double if len(self._events) >= 2 else h_single
            self._draw_history_section(base_h, w, p, sc=sc)

        # Toast overlay (e.g. "✓ Task added!") — drawn on top of everything
        if self._toast_text:
            text, color = self._toast_text
            t_attrs = {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(11.5 * sc), AppKit.NSFontWeightSemibold),
                AppKit.NSForegroundColorAttributeName: color,
            }
            ts = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, t_attrs)
            tx = (w - ts.size().width) / 2
            ty = h / 2 - ts.size().height / 2
            ts.drawAtPoint_(AppKit.NSPoint(tx, ty))

        # Pulsating green border glow on event start
        if self._pulse_alpha > 0.001:
            glow_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                AppKit.NSInsetRect(bounds, 2, 2), CORNER_R, CORNER_R
            )
            glow_path.setLineWidth_(3.0)
            AppKit.NSColor.systemGreenColor().colorWithAlphaComponent_(self._pulse_alpha).setStroke()
            glow_path.stroke()

    def _badge_color(self, is_current):
        return AppKit.NSColor.systemGreenColor() if is_current else AppKit.NSColor.systemOrangeColor()

    def _draw_event(self, ev, y, event_idx, p, block_h=BLOCK_H, sc=1.0):
        is_task    = ev.get("is_task", False)
        is_current = ev["is_current"]

        badge_text  = ("🚀 NOW"  if is_task else "📅 NOW")  if is_current else \
                      ("🚀 NEXT" if is_task else "📅 NEXT")
        badge_color = self._badge_color(is_current)

        # Reserve right margin for the check circle on task rows
        right_inset = 36 if is_task else 12
        self._draw_single_message(badge_text, badge_color, ev["title"],
                                  format_time_range(ev), y, p, right_inset=right_inset, sc=sc)

        # Check circle — shown for ALL tasks (NOW and NEXT), never for calendar events
        if is_task:
            w = self.bounds().size.width
            r = 9
            cx = w - 16
            cy = y + block_h // 2
            check_rect = AppKit.NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            self._check_rects.append((event_idx, check_rect))
            circle_path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                AppKit.NSInsetRect(check_rect, 1, 1)
            )
            if self._flash_idx == event_idx:
                AppKit.NSColor.systemGreenColor().setFill()
                circle_path.fill()
            else:
                p["check_circle"].setStroke()
                circle_path.setLineWidth_(1.5)
                circle_path.stroke()

    def _draw_history_section(self, y, w, p, sc=1.0):
        hist_header_h = round(HISTORY_HEADER_H * sc)
        hist_item_h   = round(HISTORY_ITEM_H   * sc)

        # Separator
        p["history_sep"].setFill()
        AppKit.NSRectFill(AppKit.NSMakeRect(PADDING_L, y + 1, w - PADDING_L * 2, 0.5))

        # Header
        hdr_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(8 * sc), AppKit.NSFontWeightSemibold),
            AppKit.NSForegroundColorAttributeName: p["history_hdr"],
            AppKit.NSKernAttributeName: 1.4,
        }
        AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "🚀  RECENTLY CRUSHED", hdr_attrs
        ).drawAtPoint_(AppKit.NSPoint(PADDING_L, y + 6))

        # Items
        item_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(10 * sc), AppKit.NSFontWeightLight),
            AppKit.NSForegroundColorAttributeName: p["history_item"],
        }
        iy = y + hist_header_h + 4
        for title in self._history:
            trunc = (title[:30] + "…") if len(title) > 30 else title
            AppKit.NSAttributedString.alloc().initWithString_attributes_(
                "✓  " + trunc, item_attrs
            ).drawAtPoint_(AppKit.NSPoint(PADDING_L, iy))
            iy += hist_item_h


    def _draw_free_state(self, w, h, p, sc=1.0):
        cy = h / 2
        q_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(10.5 * sc), AppKit.NSFontWeightMedium),
            AppKit.NSForegroundColorAttributeName: p["free_text"],
        }
        q_str = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "🚀  What are we crushing next?", q_attrs
        )
        q_str.drawAtPoint_(AppKit.NSPoint(PADDING_L, cy - q_str.size().height / 2 + 8))

        # FIX 16: add "No events scheduled for today" subline
        ps = AppKit.NSMutableParagraphStyle.alloc().init()
        ps.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        sub_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(round(11 * sc)),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithWhite_alpha_(0.4, 1.0),
            AppKit.NSParagraphStyleAttributeName: ps,
        }
        sub_str = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "No events scheduled for today", sub_attrs
        )
        sub_rect = AppKit.NSMakeRect(PADDING_L, cy - 26, w - PADDING_L * 2, 16)
        sub_str.drawInRect_(sub_rect)

    def _draw_single_message(self, badge, badge_color, title, subtitle, y, p, right_inset=12, sc=1.0):
        w = self.bounds().size.width
        text_w = w - PADDING_L - right_inset   # usable width for title / subtitle

        # Scale vertical offsets proportionally so content stays centred in the block
        y_badge    = round(y + 8  * sc)
        y_title    = round(y + 20 * sc)
        y_subtitle = round(y + 34 * sc)

        # Paragraph style: single line, truncate tail with "…"
        ps = AppKit.NSMutableParagraphStyle.alloc().init()
        ps.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        ps.setMaximumLineHeight_(round(16 * sc))

        badge_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(9 * sc), AppKit.NSFontWeightSemibold),
            AppKit.NSForegroundColorAttributeName: badge_color,
            AppKit.NSKernAttributeName: 1.3,
        }
        title_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(12 * sc), AppKit.NSFontWeightMedium),
            AppKit.NSForegroundColorAttributeName: p["title"],
            AppKit.NSParagraphStyleAttributeName: ps,
        }
        sub_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(round(10 * sc), AppKit.NSFontWeightRegular),
            AppKit.NSForegroundColorAttributeName: p["subtitle"],
            AppKit.NSParagraphStyleAttributeName: ps,
        }

        # Badge (short — drawAtPoint is fine)
        AppKit.NSAttributedString.alloc().initWithString_attributes_(badge, badge_attrs)\
            .drawAtPoint_(AppKit.NSPoint(PADDING_L, y_badge))

        # Title — draw into a bounded rect so NSLineBreakByTruncatingTail kicks in
        title_rect = AppKit.NSMakeRect(PADDING_L, y_title, text_w, round(16 * sc))
        AppKit.NSAttributedString.alloc().initWithString_attributes_(title, title_attrs)\
            .drawInRect_(title_rect)

        if subtitle:
            sub_rect = AppKit.NSMakeRect(PADDING_L, y_subtitle, text_w, round(14 * sc))
            AppKit.NSAttributedString.alloc().initWithString_attributes_(subtitle, sub_attrs)\
                .drawInRect_(sub_rect)

    # ---- Drag ------------------------------------------------------------

    def mouseDown_(self, event):
        self._drag_start  = event.locationInWindow()
        self._is_dragging = False

    def mouseDragged_(self, event):
        self._is_dragging = True
        win = self.window()
        if not win:
            return
        loc = event.locationInWindow()
        dx = loc.x - self._drag_start.x
        dy = loc.y - self._drag_start.y
        orig = win.frame().origin
        new_origin = AppKit.NSPoint(orig.x + dx, orig.y + dy)
        win.setFrameOrigin_(new_origin)
        Foundation.NSUserDefaults.standardUserDefaults().setObject_forKey_(
            {"x": new_origin.x, "y": new_origin.y}, WINDOW_ORIGIN_KEY
        )

    # ---- Click -----------------------------------------------------------

    def mouseUp_(self, event):
        if self._is_dragging:
            return
        if event.clickCount() != 1:
            return
        # Convert window coords → view coords (respects isFlipped)
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)

        # "+" button hit-test (free state)
        if self._plus_rect and AppKit.NSPointInRect(loc, self._plus_rect):
            delegate = AppKit.NSApp.delegate()
            if delegate:
                delegate.showAddTaskDialog()
            return

        # Check circle hit-test
        for idx, crect in self._check_rects:
            if AppKit.NSPointInRect(loc, crect):
                if idx < len(self._events):
                    ev = self._events[idx]
                    delegate = AppKit.NSApp.delegate()
                    if delegate:
                        # Flash circle green briefly, then check off
                        self._flash_idx = idx
                        self.setNeedsDisplay_(True)
                        ev_copy = dict(ev)
                        def _deferred(d=delegate, e=ev_copy):
                            def _on_main():
                                self._flash_idx = None
                                d.checkOffEvent_(e)
                            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_on_main)
                        threading.Timer(0.28, _deferred).start()
                return

        # Free state: clicking anywhere (not "+" which is handled above) opens dialog
        if not self._events:
            delegate = AppKit.NSApp.delegate()
            if delegate:
                delegate.showAddTaskDialog()
            return

        # Error state: clicking retries the refresh
        if self._status == "error":
            delegate = AppKit.NSApp.delegate()
            if delegate:
                delegate.scheduleImmediateRefresh()
            return

        # Busy state: clicking the main body opens Google Calendar
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
                Foundation.NSURL.URLWithString_("https://calendar.google.com")
            )

    def rightMouseDown_(self, event):
        """Show context menu on press (more reliable than rightMouseUp_ for non-activating panels)."""
        delegate = AppKit.NSApp.delegate()
        menu = AppKit.NSMenu.alloc().init()

        ri = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh", "handleRefresh:", "")
        ri.setTarget_(self)
        menu.addItem_(ri)

        ai = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("+ Add Task", "menuAddTask:", "")
        ai.setTarget_(delegate)
        menu.addItem_(ai)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        fi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Send Feedback", "handleFeedback:", "")
        fi.setTarget_(self)
        menu.addItem_(fi)

        bi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Report a Bug", "handleBug:", "")
        bi.setTarget_(self)
        menu.addItem_(bi)

        si = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Settings…", "openSettings:", "")
        si.setTarget_(delegate)
        menu.addItem_(si)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        hi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide for Now", "hideFloaty:", "")
        hi.setTarget_(delegate)
        menu.addItem_(hi)

        qi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "reallyQuit:", "")
        qi.setTarget_(delegate)
        menu.addItem_(qi)

        AppKit.NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)

    def handleRefresh_(self, sender):
        delegate = AppKit.NSApp.delegate()
        if delegate:
            delegate.scheduleImmediateRefresh()

    def handleFeedback_(self, sender):
        import urllib.parse
        subject = urllib.parse.quote("Floaty Feedback")
        body = urllib.parse.quote("Hi Julian,\n\nHere's some feedback about Floaty:\n\n")
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
            AppKit.NSURL.URLWithString_(f"mailto:sta.julian@gmail.com?subject={subject}&body={body}")
        )

    def handleBug_(self, sender):
        import urllib.parse
        subject = urllib.parse.quote("Floaty Bug Report")
        body = urllib.parse.quote("Hi Julian,\n\nI found a bug in Floaty:\n\n**What happened:**\n\n**What I expected:**\n\n**macOS version:**\n")
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
            AppKit.NSURL.URLWithString_(f"mailto:sta.julian@gmail.com?subject={subject}&body={body}")
        )

    def acceptsFirstMouse_(self, event):
        return True

    # ---- Cursor-enter: keep panel on top whenever cursor enters the widget --

    def updateTrackingAreas(self):
        objc.super(ContentView, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        ta = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways,
            self,
            None,
        )
        self.addTrackingArea_(ta)

    def mouseEntered_(self, event):
        win = self.window()
        if win:
            win.orderFrontRegardless()

    def mouseExited_(self, event):
        # Eat the event — do NOT pass it up the responder chain.
        # NSPanel can intercept mouseExited_ and hide itself in some configurations.
        win = self.window()
        if win:
            win.orderFrontRegardless()


class FloatingPanel(AppKit.NSPanel):
    """NSPanel that floats on all Spaces and doesn't steal focus."""

    # When False, orderOut_ calls are silently swallowed so nothing can
    # hide the panel except our own Hide Floaty action.
    _allow_order_out = False

    @classmethod
    def panelWithFrame_(cls, frame):
        style = (
            AppKit.NSWindowStyleMaskNonactivatingPanel
            | AppKit.NSWindowStyleMaskFullSizeContentView
            | AppKit.NSWindowStyleMaskBorderless
        )
        panel = cls.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, AppKit.NSBackingStoreBuffered, False
        )
        panel.setLevel_(25)  # NSStatusBarWindowLevel — above all app windows
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary  # 16: don't hide on Exposé/spaces
        )
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setMovableByWindowBackground_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        return panel

    def orderOut_(self, sender):
        """Block any attempt to hide the panel unless we explicitly allowed it."""
        if self._allow_order_out:
            objc.super(FloatingPanel, self).orderOut_(sender)
        else:
            # Something tried to hide us — resist and reassert
            self.orderFrontRegardless()

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


# ---------------------------------------------------------------------------
# Add-Task dialog with animated GIF
# ---------------------------------------------------------------------------

_DIALOG_TITLES = [
    "What are we crushing next?",
    "What's the move?",
    "Next mission?",
    "Drop the task:",
    "What are we shipping?",
    "Next on the list?",
    "Ready to execute?",
    "What's getting done?",
    "Add to the attack plan:",
    "What needs to happen?",
]


class _HypeDialog(AppKit.NSObject):
    """Modal 'Add Task' dialog with an animated GIF (falls back to emoji)."""

    _FALLBACK_EMOJIS = ["🔥","💪","⚡","🎯","🏆","🌟","💥","🦁","👊","🚀","✨","🎉"]

    def initWithOpenCal_screen_(self, open_cal: bool, screen):
        self = objc.super(_HypeDialog, self).init()
        if self is None:
            return None
        self._title_result  = None
        self._open_cal_result = open_cal

        W, H = 360, 460
        M = 20   # horizontal margin
        self._win = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H),
            AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered, False,
        )
        self._win.setTitle_("Floaty")
        # Center on the screen that contains the widget
        scr = screen or AppKit.NSScreen.mainScreen()
        sf  = scr.visibleFrame()
        ox  = sf.origin.x + (sf.size.width  - W) / 2
        oy  = sf.origin.y + (sf.size.height - H) / 2
        self._win.setFrameOrigin_(AppKit.NSPoint(ox, oy))
        cv = self._win.contentView()

        # ── GIF / emoji area  (fills most of the width, tall) ─────────────
        gif_path = _random_hype_gif() if WebKit else None
        if gif_path:
            # WKWebView animates GIFs natively on all macOS versions.
            # NSImageView.setAnimates_ is deprecated in macOS 14+ and often
            # shows only the first frame.
            gif_frame = AppKit.NSMakeRect(M, 210, W - M * 2, 230)
            wv_config = WebKit.WKWebViewConfiguration.alloc().init()
            wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(
                gif_frame, wv_config
            )
            # Public API (macOS 12+) for transparent background — avoids private KVC key
            wv.setValue_forKey_(AppKit.NSColor.clearColor(), "backgroundColor")
            gif_url = Foundation.NSURL.fileURLWithPath_(str(gif_path))
            wv.loadFileURL_allowingReadAccessToURL_(gif_url, gif_url)
            cv.addSubview_(wv)
        else:
            emoji = random.choice(self._FALLBACK_EMOJIS)
            lbl = AppKit.NSTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(M, 210, W - M * 2, 230)
            )
            lbl.setStringValue_(emoji)
            lbl.setFont_(AppKit.NSFont.systemFontOfSize_(160))
            lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setEditable_(False)
            cv.addSubview_(lbl)

        # ── Title ─────────────────────────────────────────────────────────
        title_lbl = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 178, W - M * 2, 24)
        )
        title_lbl.setStringValue_(random.choice(_DIALOG_TITLES))
        title_lbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(15))
        title_lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
        title_lbl.setBezeled_(False)
        title_lbl.setDrawsBackground_(False)
        title_lbl.setEditable_(False)
        cv.addSubview_(title_lbl)

        # ── Sub-title ─────────────────────────────────────────────────────
        sub_lbl = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 156, W - M * 2, 18)
        )
        sub_lbl.setStringValue_("This will be added to your 🚀 Today task list.")
        sub_lbl.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        sub_lbl.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        sub_lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
        sub_lbl.setBezeled_(False)
        sub_lbl.setDrawsBackground_(False)
        sub_lbl.setEditable_(False)
        cv.addSubview_(sub_lbl)

        # ── Task name field ───────────────────────────────────────────────
        self._field = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 122, W - M * 2, 26)
        )
        self._field.setPlaceholderString_("Type here and press ↵ to add…")
        cv.addSubview_(self._field)

        # ── Checkbox ──────────────────────────────────────────────────────
        self._checkbox = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 92, W - M * 2, 22)
        )
        self._checkbox.setButtonType_(AppKit.NSSwitchButton)
        self._checkbox.setTitle_("Schedule task right away")
        self._checkbox.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._checkbox.setState_(AppKit.NSOnState if open_cal else AppKit.NSOffState)
        cv.addSubview_(self._checkbox)

        # ── Buttons ───────────────────────────────────────────────────────
        cancel_btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 20, 120, 32)
        )
        cancel_btn.setTitle_("Cancel")
        cancel_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        cancel_btn.setKeyEquivalent_("\x1b")
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_("cancel:")
        cv.addSubview_(cancel_btn)

        add_btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(W - M - 120, 20, 120, 32)
        )
        add_btn.setTitle_("Add Task")
        add_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        add_btn.setKeyEquivalent_("\r")
        add_btn.setTarget_(self)
        add_btn.setAction_("addTask:")
        cv.addSubview_(add_btn)

        self._win.setInitialFirstResponder_(self._field)
        return self

    def run(self) -> "tuple[str|None, bool]":
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)
        self._field.selectText_(None)
        AppKit.NSApp.runModalForWindow_(self._win)
        return self._title_result, self._open_cal_result


    def addTask_(self, sender):
        self._title_result    = str(self._field.stringValue()).strip() or None
        self._open_cal_result = (self._checkbox.state() == AppKit.NSOnState)
        AppKit.NSApp.stopModal()
        self._win.close()

    def cancel_(self, sender):
        AppKit.NSApp.stopModal()
        self._win.close()


class FloatyApp(AppKit.NSApplication):
    """NSApplication subclass that intercepts Carbon hotkey events."""

    def sendEvent_(self, event):
        objc.super(FloatyApp, self).sendEvent_(event)


class AppDelegate(AppKit.NSObject):
    """Application delegate. Owns the panel, coordinates auth and refresh."""

    _crushed_history  = []   # persisted across restarts via UserDefaults
    _crushed_today    = 0    # tasks crushed today (resets at midnight)
    needs_auth        = False  # set in main() when no valid tokens exist

    # Auto-extend: tracks ALL active tasks simultaneously.
    # key = event id, value = {"end": datetime (UTC), "orig_end": datetime (UTC)}
    _tracked_tasks: dict = {}


    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, hasVisibleWindows):
        """Dock icon click — toggle the floating widget on/off."""
        if not hasattr(self, '_panel') or not self._panel:
            return False
        if self._panel.isVisible():
            self._panel.orderOut_(None)
        else:
            self._panel.makeKeyAndOrderFront_(None)
            self._panel.orderFrontRegardless()
        return False

    def applicationDidFinishLaunching_(self, notification):
        self._refresh_timer = None
        _register_login_item()  # ensure Floaty starts at login

        # Restore crushed history and today's count
        ud = Foundation.NSUserDefaults.standardUserDefaults()
        saved = ud.objectForKey_(HISTORY_KEY)
        AppDelegate._crushed_history = list(saved)[:10] if saved else []
        today_str = datetime.now().strftime("%Y-%m-%d")
        saved_today = ud.objectForKey_(CRUSHED_TODAY_KEY) or {}
        if saved_today.get("date") == today_str:
            AppDelegate._crushed_today = int(saved_today.get("count", 0))
        else:
            AppDelegate._crushed_today = 0

        widget_w = get_widget_w(AppDelegate.config)
        sc = get_widget_scale(AppDelegate.config)
        size = AppKit.NSSize(widget_w, round(WIDGET_H_SINGLE * sc))
        origin = self._saved_origin(size)
        frame = AppKit.NSMakeRect(origin.x, origin.y, size.width, size.height)

        self._content_view = ContentView.alloc().initWithFrame_(frame)
        self._panel = FloatingPanel.panelWithFrame_(frame)
        self._panel.setContentView_(self._content_view)
        self._active = True
        self._panel.orderFrontRegardless()

        # Menu bar rocket icon
        self._status_item = AppKit.NSStatusBar.systemStatusBar()\
            .statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._update_status_title()
        status_menu = AppKit.NSMenu.alloc().init()

        refresh_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh", "menuRefresh:", "")
        refresh_item.setTarget_(self)
        status_menu.addItem_(refresh_item)

        add_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("+ Add Task", "menuAddTask:", "")
        add_item.setTarget_(self)
        status_menu.addItem_(add_item)

        settings_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Settings…", "openSettings:", "")
        settings_item.setTarget_(self)
        status_menu.addItem_(settings_item)

        status_menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._hide_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide for Now", "hideFloaty:", "")
        self._hide_item.setTarget_(self)
        status_menu.addItem_(self._hide_item)

        real_quit = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "reallyQuit:", "Q")
        real_quit.setTarget_(self)
        status_menu.addItem_(real_quit)

        self._status_menu = status_menu
        self._status_item.setMenu_(status_menu)

        threading.Thread(target=_ensure_hype_gifs, daemon=True).start()

        # FIX 15: Heartbeat reduced from 20Hz to 1Hz — keep panel always on top
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "keepAlive:", None, True
        )

        # Re-assert panel when any other app activates (cursor moves to another app)
        ws_nc = Foundation.NSWorkspace.sharedWorkspace().notificationCenter()
        ws_nc.addObserver_selector_name_object_(
            self, "appActivated:",
            AppKit.NSWorkspaceDidActivateApplicationNotification,
            None,
        )

        # Re-assert panel when any window closes (catches confetti window closing)
        def_nc = Foundation.NSNotificationCenter.defaultCenter()
        def_nc.addObserver_selector_name_object_(
            self, "anyWindowClosed:",
            AppKit.NSWindowWillCloseNotification,
            None,
        )

        # If first run (no valid tokens), show welcome/auth window instead of starting refresh
        if AppDelegate.needs_auth:
            self._show_welcome_window()
        else:
            # config and tokens already set up in main() before the event loop started
            _auto_update()  # check for updates silently in background
            _ping_launch()  # FIX 18: analytics ping on every normal launch
            self._start_timer()
            threading.Thread(target=self._do_refresh, daemon=True).start()
            # FIX 9: show onboarding tip for existing users who haven't seen it
            ud2 = Foundation.NSUserDefaults.standardUserDefaults()
            if not ud2.boolForKey_("floaty.onboardingShown"):
                ud2.setBool_forKey_(True, "floaty.onboardingShown")
                Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.5, self, "showOnboardingTip:", None, False
                )

    def keepAlive_(self, timer):
        if self._active:
            self._panel.setAlphaValue_(1.0)   # counteract anything setting alpha→0
            self._panel.setLevel_(25)  # NSStatusBarWindowLevel — counteract level changes
            self._panel.orderFrontRegardless()

    def appActivated_(self, notification):
        """Any app activates → immediately re-assert our panel."""
        if self._active:
            self._panel.orderFrontRegardless()

    def anyWindowClosed_(self, notification):
        """Any window closes (confetti, dialogs, etc.) → re-assert our panel."""
        if self._active:
            self._panel.orderFrontRegardless()

    def _start_timer(self):
        if self._refresh_timer:
            self._refresh_timer.invalidate()
        self._refresh_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_INTERVAL, self, "timerFired:", None, True
        )

    def timerFired_(self, timer):
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def hideFloaty_(self, sender):
        """Hide the widget and stop refreshing. Rocket stays in menu bar."""
        self._active = False
        if self._refresh_timer:
            self._refresh_timer.invalidate()
            self._refresh_timer = None
        self._panel._allow_order_out = True
        self._panel.setAlphaValue_(0)
        self._panel.orderOut_(None)
        self._hide_item.setTitle_("Show Floaty")
        self._hide_item.setAction_("showFloaty:")

    def showFloaty_(self, sender):
        """Show the widget again and resume refreshing."""
        self._active = True
        self._panel._allow_order_out = False
        self._panel.setAlphaValue_(1)
        self._panel.orderFrontRegardless()
        self._hide_item.setTitle_("Hide Floaty")
        self._hide_item.setAction_("hideFloaty:")
        self._start_timer()
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def reallyQuit_(self, sender):
        """Completely quit Floaty."""
        _mark_clean_exit()
        AppKit.NSApp.terminate_(self)

    def _update_status_title(self):
        self._status_item.button().setTitle_(f"{AppDelegate._crushed_today}🚀")


    def scheduleImmediateRefresh(self):
        # Show "Refreshing…" so the user knows something is happening
        self._run_on_main(lambda: self._content_view.setMessage_("Refreshing…"))
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        try:
            events = fetch_current_or_next_event(AppDelegate.config)
            now = datetime.now(timezone.utc)

            # ── Sync daily count from Google Tasks (ground truth) ────────────
            # Ask the Tasks API how many tasks were completed today — covers
            # check-offs via Floaty, via Google Tasks app, web, or any client.
            token = get_valid_token(AppDelegate.config)
            completed_count = _fetch_completed_today_count(AppDelegate.config, token)
            if completed_count >= 0:  # -1 means network error; keep previous value
                ud = Foundation.NSUserDefaults.standardUserDefaults()
                today_str = datetime.now().strftime("%Y-%m-%d")
                prev_count = AppDelegate._crushed_today
                AppDelegate._crushed_today = completed_count
                ud.setObject_forKey_(
                    {"date": today_str, "count": completed_count},
                    CRUSHED_TODAY_KEY,
                )
                if completed_count != prev_count:
                    cv = self._content_view
                    self._run_on_main(lambda: cv.setNeedsDisplay_(True))

            # ── Auto-extend: all uncompleted tasks, not just one ──────────────
            # Next non-task calendar event caps all extensions
            next_cal = next(
                (e for e in events if not e.get("is_current") and not e.get("is_task")),
                None
            )
            current_ids = {e["id"] for e in events if e.get("is_current")}
            extended_count = 0

            for task_id, info in list(AppDelegate._tracked_tasks.items()):
                tracked_end = info["end"]
                orig_end    = info["orig_end"]
                # Only extend tasks that have expired AND are no longer in current events
                if tracked_end <= now and task_id not in current_ids:
                    max_end = orig_end + timedelta(hours=2)
                    new_end = tracked_end + timedelta(minutes=15)
                    if next_cal:
                        new_end = min(new_end, next_cal["start"])
                    new_end = min(new_end, max_end)
                    if new_end > now:
                        try:
                            extend_calendar_event(AppDelegate.config, task_id, new_end)
                            AppDelegate._tracked_tasks[task_id]["end"] = new_end
                            extended_count += 1
                        except Exception:
                            pass
                    else:
                        # Reached 2-hour cap — stop tracking this task
                        del AppDelegate._tracked_tasks[task_id]

            if extended_count:
                events = fetch_current_or_next_event(AppDelegate.config)
                current_ids = {e["id"] for e in events if e.get("is_current")}
                cv = self._content_view
                label = f"+15 min on {extended_count} task{'s' if extended_count > 1 else ''} — keep going!"
                self._run_on_main(lambda l=label: cv.setSuccess_(l))

            # ── Update tracking dict with all currently active tasks ──────────
            prev_ids = set(AppDelegate._tracked_tasks.keys())
            for ev in events:
                if ev.get("is_current") and ev.get("is_task"):
                    eid = ev["id"]
                    if eid not in AppDelegate._tracked_tasks:
                        # New task entering NOW state
                        AppDelegate._tracked_tasks[eid] = {
                            "end":      ev["end"],
                            "orig_end": ev["end"],
                        }
                    else:
                        # Update end (may have just been extended)
                        AppDelegate._tracked_tasks[eid]["end"] = ev["end"]

            # Remove tasks that were checked off (no longer appear anywhere in events)
            all_event_ids = {e["id"] for e in events}
            for eid in list(AppDelegate._tracked_tasks.keys()):
                if eid not in all_event_ids:
                    del AppDelegate._tracked_tasks[eid]

            # ── Detect NEXT→NOW transition for pulsation ──────────────────────
            current = next((e for e in events if e.get("is_current")), None)
            prev_id = next(iter(prev_ids), None)
            new_now_transition = (
                current
                and current["id"] not in prev_ids
                and len(prev_ids) > 0  # not first load
            )
            if current and not prev_ids:
                started_recently = (now - current["start"]).total_seconds() < 120
                new_now_transition = started_recently

            do_pulse = new_now_transition and AppDelegate.config.get("pulsation", False)
            cv = self._content_view

            def _update(ev=events, dp=do_pulse):
                cv.setEvents_(ev)
                if dp:
                    cv.startPulse()

            self._run_on_main(_update)
        except Exception as e:
            err = str(e)
            self._run_on_main(lambda: self._content_view.setError_(err))
            # Auto-retry in 10s (faster than the normal 60s interval)
            self._run_on_main(self._schedule_error_retry)

    def _schedule_error_retry(self):
        """Schedule a one-shot 10s retry after a fetch error."""
        existing = getattr(self, "_error_retry_timer", None)
        if existing and existing.isValid():
            return
        self._error_retry_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            10.0, self, "timerFired:", None, False
        )

    _OPEN_CAL_KEY = "taskfloat.openCalendarAfterAdd"

    def showAddTaskDialog(self):
        ud       = Foundation.NSUserDefaults.standardUserDefaults()
        open_cal = bool(ud.boolForKey_(self._OPEN_CAL_KEY))

        # Find the screen that actually contains the panel's centre point.
        # panel.screen() returns nil on secondary displays in some macOS versions.
        screen = None
        if self._panel:
            pf = self._panel.frame()
            cx = pf.origin.x + pf.size.width  / 2
            cy = pf.origin.y + pf.size.height / 2
            for s in AppKit.NSScreen.screens():
                sf = s.frame()
                if (sf.origin.x <= cx <= sf.origin.x + sf.size.width and
                        sf.origin.y <= cy <= sf.origin.y + sf.size.height):
                    screen = s
                    break
        if screen is None:
            screen = AppKit.NSScreen.mainScreen()

        dialog = _HypeDialog.alloc().initWithOpenCal_screen_(open_cal, screen)
        task_title, open_cal = dialog.run()
        AppKit.NSApp.deactivate()
        self._panel.orderFrontRegardless()

        ud.setBool_forKey_(open_cal, self._OPEN_CAL_KEY)
        if task_title:
            self._content_view.setMessage_("Adding task…")
            threading.Thread(
                target=lambda: self._do_add_task(task_title, open_cal),
                daemon=True,
            ).start()

    def _do_add_task(self, title, open_calendar=False):
        try:
            create_task_today(AppDelegate.config, title)
            events = fetch_current_or_next_event(AppDelegate.config)
            # Show events, then overlay a green success toast for 1.8s
            self._run_on_main(lambda: self._content_view.setEvents_(events))
            self._run_on_main(lambda: self._content_view.setSuccess_("✓ Task added!"))
            if open_calendar:
                url = Foundation.NSURL.URLWithString_(
                    "https://calendar.google.com/calendar/u/0/r"
                )
                panel = self._panel
                def _open_url():
                    AppKit.NSWorkspace.sharedWorkspace().openURL_(url)
                    panel.orderFrontRegardless()
                self._run_on_main(_open_url)
            # Always fast-refresh for 30s so the new task appears quickly
            self._run_on_main(lambda: self._start_fast_refresh(duration=30, interval=2))
        except Exception as e:
            err = str(e)
            self._run_on_main(lambda: self._content_view.setError_(err))

    def _start_fast_refresh(self, duration=30, interval=2):
        """Switch to fast refresh for `duration` seconds, then revert."""
        if self._refresh_timer:
            self._refresh_timer.invalidate()
        self._refresh_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, "timerFired:", None, True
        )
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            duration, self, "revertRefreshRate:", None, False
        )

    def revertRefreshRate_(self, timer):
        self._start_timer()  # back to normal 60s

    def checkOffEvent_(self, event):
        ev = dict(event)  # copy so the lambda captures a stable reference
        # Capture screen on the main thread before launching background work
        panel_screen = self._panel.screen() if self._panel else None
        self._run_on_main(lambda: self._content_view.setMessage_("Checking off…"))
        threading.Thread(target=lambda: self._do_check_off(ev, panel_screen), daemon=True).start()


    def _do_check_off(self, event, panel_screen=None):
        self._run_on_main(lambda: setattr(self._content_view, '_checking_off', True))
        self._run_on_main(lambda: self._content_view.setNeedsDisplay_(True))
        # Remove this specific task from auto-extend tracking
        event_id = event.get("id", "")
        AppDelegate._tracked_tasks.pop(event_id, None)
        try:
            is_task_only = event.get("task_only", False)
            event_id = event.get("id", "")
            if not is_task_only and event_id:
                # FIX 14: pass actual calendar ID instead of always using "primary"
                cal_id = event.get("_cal_id", "primary")
                delete_calendar_event(AppDelegate.config, event_id, cal_id=cal_id)
            phrase = _random_phrase()
            voice  = _random_voice()
            def _celebrate(p=phrase, v=voice, scr=panel_screen):
                show_confetti(scr)
            self._run_on_main(_celebrate)
            title = event.get("title", "")
            if title and title != "(No title)":
                # Update persistent history
                AppDelegate._crushed_history = ([title] + AppDelegate._crushed_history)[:10]
                ud = Foundation.NSUserDefaults.standardUserDefaults()
                ud.setObject_forKey_(AppDelegate._crushed_history, HISTORY_KEY)
                # Increment today's count
                AppDelegate._crushed_today += 1
                today_str = datetime.now().strftime("%Y-%m-%d")
                ud.setObject_forKey_({"date": today_str, "count": AppDelegate._crushed_today}, CRUSHED_TODAY_KEY)
                ud.synchronize()  # flush to disk before restart
                self._run_on_main(self._update_status_title)
                try:
                    if is_task_only and event_id and event.get("task_list_id"):
                        # Fast path: we already have the list ID + task ID
                        _complete_task_by_id(
                            AppDelegate.config,
                            event["task_list_id"],
                            event_id,
                        )
                    else:
                        complete_task_by_title(AppDelegate.config, title)
                except Exception:
                    pass  # best-effort
            # FIX 10: soft reload instead of os.execv restart — confetti finishes then refresh
            def _restart():
                time.sleep(10.5)   # confetti lasts ~9.5 s
                self._run_on_main(self.scheduleImmediateRefresh)
            threading.Thread(target=_restart, daemon=True).start()
        except Exception as e:
            err = str(e)
            self._run_on_main(lambda: setattr(self._content_view, '_checking_off', False))
            self._run_on_main(lambda: self._content_view.setError_(err))

    def showOnboardingTip_(self, timer):
        """Show a one-time native notification explaining how to use the widget."""
        try:
            import subprocess
            subprocess.run([
                "osascript", "-e",
                'display notification "Tap the ○ circle on a task to check it off. Right-click the widget for Refresh, Add Task, and more." '
                'with title "👋 Welcome to Floaty!"'
            ], check=False)
        except Exception:
            pass

    def _show_welcome_window(self):
        """Create and show the first-run welcome/auth window."""
        self._welcome_win = WelcomeWindow.alloc().init()
        # FIX 3: activate app so window gets keyboard focus
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._welcome_win.makeKeyAndOrderFront_(None)

    def _on_auth_complete(self):
        """Called on the main thread after OAuth succeeds in WelcomeWindow."""
        if hasattr(self, '_welcome_win') and self._welcome_win:
            self._welcome_win.close()
            self._welcome_win = None
        AppDelegate.needs_auth = False
        _auto_update()
        _ping_launch()  # FIX 18: analytics ping for first-launch users
        self._start_timer()
        threading.Thread(target=self._do_refresh, daemon=True).start()
        # FIX 9: show onboarding tip after auth completes, not during OAuth
        ud = Foundation.NSUserDefaults.standardUserDefaults()
        if not ud.boolForKey_("floaty.onboardingShown"):
            ud.setBool_forKey_(True, "floaty.onboardingShown")
            Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "showOnboardingTip:", None, False
            )

    def menuRefresh_(self, sender):
        self.scheduleImmediateRefresh()

    def menuAddTask_(self, sender):
        self.showAddTaskDialog()

    def openSettings_(self, sender):
        if not hasattr(self, "_settings_win") or not self._settings_win:
            self._settings_win = SettingsWindow.alloc().init()
        self._settings_win.loadData_(AppDelegate.config)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._settings_win.makeKeyAndOrderFront_(None)

    def _apply_widget_size(self):
        """Resize the floating panel to the new width while keeping the same center."""
        new_w = get_widget_w(AppDelegate.config)
        old_frame = self._panel.frame()
        old_cx = old_frame.origin.x + old_frame.size.width / 2
        old_cy = old_frame.origin.y + old_frame.size.height / 2
        new_x = old_cx - new_w / 2
        new_frame = AppKit.NSMakeRect(new_x, old_frame.origin.y, new_w, old_frame.size.height)
        self._panel.setFrame_display_(new_frame, True)
        self._content_view.setFrame_(AppKit.NSMakeRect(0, 0, new_w, old_frame.size.height))
        Foundation.NSUserDefaults.standardUserDefaults().setObject_forKey_(
            {"x": new_x, "y": old_frame.origin.y}, WINDOW_ORIGIN_KEY
        )
        self._panel.orderFrontRegardless()

    def _saved_origin(self, size) -> AppKit.NSPoint:
        ud = Foundation.NSUserDefaults.standardUserDefaults()
        saved = ud.objectForKey_(WINDOW_ORIGIN_KEY)
        if saved and "x" in saved and "y" in saved:
            origin = AppKit.NSPoint(saved["x"], saved["y"])
            # Validate: widget must overlap at least one screen by ≥20×20 px
            wx, wy, ww, wh = origin.x, origin.y, size.width, size.height
            for screen in AppKit.NSScreen.screens():
                sf = screen.visibleFrame()
                ox1, oy1 = sf.origin.x, sf.origin.y
                ox2, oy2 = ox1 + sf.size.width, oy1 + sf.size.height
                overlap_x = max(0, min(wx + ww, ox2) - max(wx, ox1))
                overlap_y = max(0, min(wy + wh, oy2) - max(wy, oy1))
                if overlap_x >= 20 and overlap_y >= 20:
                    return origin
            # Saved position is off all screens — fall through to default
        screen = AppKit.NSScreen.mainScreen()
        if screen:
            sf = screen.frame()
            return AppKit.NSPoint(sf.origin.x + sf.size.width - size.width - 20,
                                  sf.origin.y + sf.size.height - size.height - 40)
        return AppKit.NSPoint(1100, 800)

    @staticmethod
    def _run_on_main(fn):
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


# ---------------------------------------------------------------------------
# Login-item helpers (used by SettingsWindow)
# ---------------------------------------------------------------------------

_FLOATY_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.floaty.plist"
_FLOATY_PLIST_LABEL = "com.floaty"


def _login_item_enabled() -> bool:
    """Return True if the Floaty LaunchAgent plist exists (= will start at login)."""
    return _FLOATY_PLIST_PATH.exists()


def _set_login_item(enabled: bool, app_dir: str | None = None) -> None:
    """Write/remove the LaunchAgent plist. Does NOT call launchctl bootstrap to avoid
    the macOS Sonoma 'A background item was added' System Settings dialog.
    The plist is picked up silently at the next login."""
    if enabled:
        if _FLOATY_PLIST_PATH.exists():
            return  # already set
        _FLOATY_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Find the Floaty binary path.
        # Priority: explicitly passed app_dir → running bundle → known py2app location
        if not app_dir:
            try:
                bundle = Foundation.NSBundle.mainBundle()
                exec_path = bundle.executablePath() if bundle else None
                # Only trust it if it ends with "/Floaty" (the actual app binary)
                if exec_path and str(exec_path).endswith("/Floaty"):
                    app_dir = str(Path(exec_path).parent)
            except Exception:
                app_dir = None
        if not app_dir:
            # Reliable fallback — the py2app-built app the user actually installed
            candidate = Path.home() / "TaskFloat" / "dist" / "Floaty.app" / "Contents" / "MacOS"
            if candidate.exists():
                app_dir = str(candidate)
        binary = str(Path(app_dir) / "Floaty") if app_dir else "/usr/bin/true"
        log = str(Path.home() / "Library" / "Logs" / "Floaty.log")
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_FLOATY_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""
        _FLOATY_PLIST_PATH.write_text(plist_content)
        # FIX 11: bootstrap immediately so it takes effect without requiring logout
        try:
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(_FLOATY_PLIST_PATH)],
                capture_output=True,
            )
        except Exception:
            pass
    else:
        # Unload from launchd if currently loaded, then remove plist
        try:
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}", str(_FLOATY_PLIST_PATH)],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        try:
            _FLOATY_PLIST_PATH.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Welcome / first-run onboarding window
# ---------------------------------------------------------------------------

class WelcomeWindow(AppKit.NSPanel):
    """First-run welcome panel that walks the user through Google OAuth."""

    # FIX 2+5: height increased to 400 to accommodate privacy label and retry button
    _W, _H = 440, 400

    def init(self):
        W, H = self._W, self._H
        # FIX 3: titled, closable window with keyboard focus support
        self = objc.super(WelcomeWindow, self).initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H),
            (
                AppKit.NSWindowStyleMaskTitled
                | AppKit.NSWindowStyleMaskClosable
                | AppKit.NSWindowStyleMaskFullSizeContentView
            ),
            AppKit.NSBackingStoreBuffered,
            False,
        )
        if self is None:
            return None

        self.setReleasedWhenClosed_(False)
        self.setHasShadow_(True)
        self.setLevel_(AppKit.NSFloatingWindowLevel)
        self.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)

        # Dark background
        bg_color = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.10, 0.10, 0.12, 0.95)
        self.setBackgroundColor_(bg_color)
        self.setOpaque_(False)

        # Center on screen
        screen = AppKit.NSScreen.mainScreen()
        if screen:
            sf = screen.frame()
            x = sf.origin.x + (sf.size.width - W) / 2
            y = sf.origin.y + (sf.size.height - H) / 2
            self.setFrameOrigin_(AppKit.NSPoint(x, y))

        cv = self.contentView()

        def label(text, size, bold=False, color=None, alignment=AppKit.NSTextAlignmentCenter):
            lbl = AppKit.NSTextField.labelWithString_(text)
            lbl.setEditable_(False)
            lbl.setBezeled_(False)
            lbl.setDrawsBackground_(False)
            lbl.setAlignment_(alignment)
            font = AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size)
            lbl.setFont_(font)
            if color:
                lbl.setTextColor_(color)
            else:
                lbl.setTextColor_(AppKit.NSColor.whiteColor())
            lbl.setTranslatesAutoresizingMaskIntoConstraints_(False)
            return lbl

        white  = AppKit.NSColor.whiteColor()
        gray   = AppKit.NSColor.colorWithWhite_alpha_(0.65, 1.0)
        green  = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.35, 0.85, 0.45, 1.0)

        title_lbl = label("🚀 Floaty", 28, bold=True, color=white)
        sub_lbl   = label("Your calendar, floating on every screen.", 14, color=gray)

        bullets = [
            "✓  Always on top, across all Spaces",
            "✓  Tasks 🚀 and meetings 📅 — clearly labeled",
            "✓  Check off tasks right from the widget",
            "✓  Draggable — stays where you put it",
        ]
        bullet_lbls = [label(b, 13, color=green, alignment=AppKit.NSTextAlignmentLeft) for b in bullets]

        # Connect button
        btn = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W - 60, 44))
        btn.setTitle_("Connect Google Calendar →")
        btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_("connectClicked:")
        btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
        if hasattr(AppKit.NSColor, 'systemBlueColor'):
            btn.setContentTintColor_(AppKit.NSColor.systemBlueColor())
        self._connect_btn = btn

        # Status label
        status_lbl = label("", 12, color=gray)
        self._status_lbl = status_lbl

        # Add all subviews
        for v in [title_lbl, sub_lbl] + bullet_lbls + [btn, status_lbl]:
            cv.addSubview_(v)

        # Auto-layout using constraints
        M = 30  # horizontal margin
        views = {"title": title_lbl, "sub": sub_lbl, "btn": btn, "status": status_lbl}
        for i, bl in enumerate(bullet_lbls):
            views[f"b{i}"] = bl

        # Anchor everything horizontally
        for key, view in views.items():
            cv.addConstraints_(AppKit.NSLayoutConstraint.constraintsWithVisualFormat_options_metrics_views_(
                f"H:|-{M}-[{key}]-{M}-|", 0, None, {key: view}
            ))

        # Vertical layout (y=0 is bottom in AppKit)
        # We'll use manual setFrame after layout — simpler than full VFL for a fixed-size panel
        # We'll place items top-down with fixed offsets from top (H - offset)
        items = [
            (title_lbl,   H - 55,  28),
            (sub_lbl,     H - 90,  20),
        ]
        for i, bl in enumerate(bullet_lbls):
            items.append((bl, H - 130 - i * 26, 20))
        items += [
            (btn,         100,  44),   # FIX 2+5: shifted up to make room for labels below
            (status_lbl,  76,   18),
        ]

        for view, y, h in items:
            view.setTranslatesAutoresizingMaskIntoConstraints_(True)
            view.setFrame_(AppKit.NSMakeRect(M, y, W - M * 2, h))

        # Bullet labels left-aligned — already set above; adjust x for indent
        for bl in bullet_lbls:
            f = bl.frame()
            bl.setFrame_(AppKit.NSMakeRect(M + 10, f.origin.y, f.size.width - 10, f.size.height))

        # FIX 5: "Open browser again" retry button (initially hidden)
        self._retry_btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(120, 50, 200, 24)
        )
        self._retry_btn.setTitle_("Open browser again →")
        self._retry_btn.setBezelStyle_(AppKit.NSBezelStyleInline)
        self._retry_btn.setTarget_(self)
        self._retry_btn.setAction_("retryBrowser:")
        self._retry_btn.setHidden_(True)
        cv.addSubview_(self._retry_btn)
        self._auth_url = None

        # FIX 2: privacy disclosure label
        privacy_lbl = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(20, 18, 400, 28)
        )
        privacy_lbl.setStringValue_(
            "Floaty uses anonymous launch analytics. Your email is stored to count connected users. No data is sold."
        )
        privacy_lbl.setBezeled_(False)
        privacy_lbl.setDrawsBackground_(False)
        privacy_lbl.setEditable_(False)
        privacy_lbl.setSelectable_(False)
        privacy_lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
        privacy_lbl.setFont_(AppKit.NSFont.systemFontOfSize_(9))
        privacy_lbl.setTextColor_(AppKit.NSColor.colorWithWhite_alpha_(0.45, 1.0))
        privacy_lbl.setWantsLayer_(True)
        cv.addSubview_(privacy_lbl)

        return self

    def connectClicked_(self, sender):
        self._connect_btn.setEnabled_(False)
        # FIX 4: set "Opening your browser…" BEFORE starting the thread
        self._status_lbl.setStringValue_("Opening your browser…")

        def _run_oauth():
            try:
                # FIX 7: resolve port and build URL so we can store it for retry
                port = _find_oauth_port()
                url = build_auth_url(AppDelegate.config, port=port)
                self._auth_url = url  # FIX 5: store for retry button
                webbrowser.open(url)
                # FIX 4: update status to "Waiting…" only after browser is opened
                Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: self._status_lbl.setStringValue_("Waiting for sign-in… (check your browser)")
                )
                # FIX 5: show retry button now that browser is open
                Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: self._retry_btn.setHidden_(False)
                )
                code = wait_for_oauth_code(url, port=port)
                tokens = exchange_code(code, AppDelegate.config, port=port)
                keychain_write("access_token", tokens["access_token"])
                if "refresh_token" in tokens:
                    keychain_write("refresh_token", tokens["refresh_token"])
                expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600) - 60)
                _token_cache.update({"access_token": tokens["access_token"], "expiry": expiry})
                _ping_connected_user(tokens["access_token"])
                # Success — dispatch back to main thread
                Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: AppKit.NSApp.delegate()._on_auth_complete()
                )
            except Exception as e:
                # FIX 6: map raw errors to user-readable messages
                err_msg = _friendly_oauth_error(str(e))
                def _on_err(msg=err_msg):
                    self._status_lbl.setStringValue_(msg)
                    self._connect_btn.setEnabled_(True)
                    self._retry_btn.setHidden_(True)
                Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(_on_err)

        threading.Thread(target=_run_oauth, daemon=True).start()

    def retryBrowser_(self, sender):
        """FIX 5: reopen the browser for the current OAuth flow."""
        if self._auth_url:
            webbrowser.open(self._auth_url)


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

class SettingsWindow(AppKit.NSPanel):
    """Settings panel for Floaty. Regular NSPanel (not floating) so it gets keyboard focus."""

    # Layout constants (AppKit: y=0 is bottom of window, y increases upward)
    # FIX 13: height increased from 500 to 600 to accommodate up to 8 calendar checkboxes
    _W, _H = 420, 600
    _M = 20   # horizontal margin

    def init(self):
        W, H, M = self._W, self._H, self._M
        self = objc.super(SettingsWindow, self).initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        if self is None:
            return None
        self.setTitle_("Floaty Settings")
        self.setReleasedWhenClosed_(False)
        # Center on main screen
        screen = AppKit.NSScreen.mainScreen()
        if screen:
            sf = screen.visibleFrame()
            ox = sf.origin.x + (sf.size.width - W) / 2
            oy = sf.origin.y + (sf.size.height - H) / 2
            self.setFrameOrigin_(AppKit.NSPoint(ox, oy))

        cv = self.contentView()
        FW = W - M * 2   # usable field width

        def _lbl(text, y, bold=False, size=12):
            f = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(M, y, FW, 18))
            f.setStringValue_(text)
            f.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold
                       else AppKit.NSFont.systemFontOfSize_(size))
            f.setBezeled_(False); f.setDrawsBackground_(False); f.setEditable_(False)
            cv.addSubview_(f)
            return f

        # ── Widget Size  (top section) ────────────────────────────────────
        # FIX 13: all Y coords shifted +100 to match new height of 600
        _lbl("Widget Size", 564, bold=True)
        self._seg = AppKit.NSSegmentedControl.alloc().initWithFrame_(AppKit.NSMakeRect(M, 536, FW, 26))
        self._seg.setSegmentCount_(3)
        self._seg.setLabel_forSegment_("Compact", 0)
        self._seg.setLabel_forSegment_("Regular", 1)
        self._seg.setLabel_forSegment_("Large", 2)
        self._seg.setTarget_(self)
        self._seg.setAction_("sizeChanged:")
        cv.addSubview_(self._seg)

        # ── Calendar ──────────────────────────────────────────────────────
        _lbl("Calendar", 504, bold=True)
        # Status label occupies the checkbox zone while data loads / on error
        self._cal_status_label = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 310, FW, 190))
        self._cal_status_label.setStringValue_("Loading calendars…")
        self._cal_status_label.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._cal_status_label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        self._cal_status_label.setBezeled_(False)
        self._cal_status_label.setDrawsBackground_(False)
        self._cal_status_label.setEditable_(False)
        cv.addSubview_(self._cal_status_label)
        self._cal_checkboxes = []   # list of (NSButton, calendar_id)
        # _CAL_TOP_Y: y of first (topmost) checkbox slot, stepping downward
        # FIX 13: 8 checkboxes × 24px = 192px, starting at 478 → bottom at 286
        self._CAL_TOP_Y = 478

        # ── Task List ─────────────────────────────────────────────────────
        _lbl("Task List", 278, bold=True)
        self._task_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 250, FW, 26))
        self._task_popup.addItemWithTitle_("(Any — search for 🚀 Today)")
        self._task_popup.setTarget_(self)
        self._task_popup.setAction_("taskListChanged:")
        cv.addSubview_(self._task_popup)

        # ── Pulsation ─────────────────────────────────────────────────────
        _lbl("Pulsation when event starts", 218, bold=True)
        self._puls_btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 194, FW, 22))
        self._puls_btn.setButtonType_(AppKit.NSSwitchButton)
        self._puls_btn.setTitle_("Animate green border glow when an event transitions to NOW")
        self._puls_btn.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._puls_btn.setTarget_(self)
        self._puls_btn.setAction_("pulsationChanged:")
        cv.addSubview_(self._puls_btn)

        # ── Launch at Login ───────────────────────────────────────────────
        _lbl("Launch at Login", 162, bold=True)
        self._login_btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 138, FW, 22))
        self._login_btn.setButtonType_(AppKit.NSSwitchButton)
        self._login_btn.setTitle_("Start Floaty automatically when you log in")
        self._login_btn.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._login_btn.setTarget_(self)
        self._login_btn.setAction_("loginItemChanged:")
        cv.addSubview_(self._login_btn)

        # FIX 11: updated hint — launchctl bootstrap is called immediately
        hint = _lbl("Floaty will start automatically. Note: it won't appear in System Settings → Login Items — this is normal.", 118, size=10)
        hint.setTextColor_(AppKit.NSColor.tertiaryLabelColor())

        # ── Anthropic API Key (Self-Healing) ──────────────────────────────
        _lbl("Anthropic API Key", 108, bold=True)
        hint_ai = _lbl(
            "Used for self-healing: Floaty analyzes crashes and auto-patches itself.",
            92, size=10,
        )
        hint_ai.setTextColor_(AppKit.NSColor.tertiaryLabelColor())
        self._anthropic_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(M, 63, FW, 24)
        )
        self._anthropic_field.setPlaceholderString_("sk-ant-api03-…  (optional)")
        self._anthropic_field.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._anthropic_field.setTarget_(self)
        self._anthropic_field.setAction_("anthropicKeyChanged:")
        cv.addSubview_(self._anthropic_field)

        # ── Separator ─────────────────────────────────────────────────────
        sep = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(M, 55, FW, 1))
        sep.setBoxType_(AppKit.NSBoxSeparator)
        cv.addSubview_(sep)

        # ── Version label ─────────────────────────────────────────────────
        ver_lbl = _lbl(f"Floaty  v{VERSION}", 16, size=10)
        ver_lbl.setTextColor_(AppKit.NSColor.tertiaryLabelColor())

        # ── Done button ───────────────────────────────────────────────────
        done_btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(W - M - 80, 14, 80, 28))
        done_btn.setTitle_("Done")
        done_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        done_btn.setKeyEquivalent_("\r")
        done_btn.setTarget_(self)
        done_btn.setAction_("doneClicked:")
        cv.addSubview_(done_btn)

        self._config = {}
        return self

    def loadData_(self, config):
        """Populate controls from config and fetch calendar/task list data in background."""
        self._config = config

        # Widget size
        size = config.get("widget_size", "normal")
        idx = {"compact": 0, "normal": 1, "large": 2}.get(size, 1)
        self._seg.setSelectedSegment_(idx)

        # Pulsation
        self._puls_btn.setState_(
            AppKit.NSOnState if config.get("pulsation", False) else AppKit.NSOffState
        )

        # Launch at Login
        self._login_btn.setState_(
            AppKit.NSOnState if _login_item_enabled() else AppKit.NSOffState
        )

        # Anthropic API key
        key = config.get("anthropic_api_key", "")
        self._anthropic_field.setStringValue_(key)

        # Fetch calendars and task lists in background
        def _fetch():
            try:
                calendars = fetch_calendar_list(config)
                task_lists = fetch_task_lists_all(config)
                def _populate():
                    self._populate_calendars(calendars)
                    self._populate_task_lists(task_lists)
                AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_populate)
            except Exception as e:
                def _error():
                    self._cal_status_label.setStringValue_(f"Connect Google first ({e})")
                AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(_error)
        threading.Thread(target=_fetch, daemon=True).start()

    def _populate_calendars(self, calendars):
        # Remove old checkboxes
        for btn, _ in self._cal_checkboxes:
            btn.removeFromSuperview()
        self._cal_checkboxes = []

        filter_ids = self._config.get("calendar_ids", [])
        M = self._M
        FW = self._W - M * 2

        if not calendars:
            self._cal_status_label.setStringValue_("No calendars found.")
            return

        self._cal_status_label.setStringValue_("")
        # FIX 13: Up to 8 checkboxes, stepping downward from _CAL_TOP_Y
        y = self._CAL_TOP_Y
        for cal in calendars[:8]:
            btn = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(M, y, FW, 22))
            btn.setButtonType_(AppKit.NSSwitchButton)
            label = cal["summary"]
            if cal.get("primary"):
                label += "  (primary)"
            btn.setTitle_(label)
            btn.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            if not filter_ids or cal["id"] in filter_ids:
                btn.setState_(AppKit.NSOnState)
            else:
                btn.setState_(AppKit.NSOffState)
            btn.setTarget_(self)
            btn.setAction_("calendarToggled:")
            self.contentView().addSubview_(btn)
            self._cal_checkboxes.append((btn, cal["id"]))
            y -= 24

    def _populate_task_lists(self, task_lists):
        self._task_popup.removeAllItems()
        self._task_popup.addItemWithTitle_("(Any — search for 🚀 Today)")
        self._task_popup.itemAtIndex_(0).setRepresentedObject_("")
        for tl in task_lists:
            self._task_popup.addItemWithTitle_(tl["title"])
            self._task_popup.lastItem().setRepresentedObject_(tl["id"])

        configured_id = self._config.get("task_list_id", "")
        if configured_id:
            for i in range(self._task_popup.numberOfItems()):
                item = self._task_popup.itemAtIndex_(i)
                if item.representedObject() == configured_id:
                    self._task_popup.selectItemAtIndex_(i)
                    break

    def calendarToggled_(self, sender):
        checked_ids = [
            cal_id for btn, cal_id in self._cal_checkboxes
            if btn.state() == AppKit.NSOnState
        ]
        total = len(self._cal_checkboxes)
        self._config["calendar_ids"] = [] if len(checked_ids) == total else checked_ids
        save_config(self._config)
        delegate = AppKit.NSApp.delegate()
        if delegate:
            delegate.scheduleImmediateRefresh()

    def taskListChanged_(self, sender):
        selected = self._task_popup.selectedItem()
        if selected:
            list_id = selected.representedObject() or ""
            self._config["task_list_id"] = list_id
            save_config(self._config)
            delegate = AppKit.NSApp.delegate()
            if delegate:
                delegate.scheduleImmediateRefresh()

    def sizeChanged_(self, sender):
        idx = self._seg.selectedSegment()
        size = ["compact", "normal", "large"][idx]
        self._config["widget_size"] = size
        save_config(self._config)
        delegate = AppKit.NSApp.delegate()
        if delegate:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
                lambda: delegate._apply_widget_size()
            )

    def pulsationChanged_(self, sender):
        self._config["pulsation"] = (self._puls_btn.state() == AppKit.NSOnState)
        save_config(self._config)

    def loginItemChanged_(self, sender):
        enabled = (self._login_btn.state() == AppKit.NSOnState)
        try:
            bundle = Foundation.NSBundle.mainBundle()
            app_dir = str(Path(bundle.executablePath()).parent) if bundle else None
        except Exception:
            app_dir = None
        threading.Thread(
            target=lambda: _set_login_item(enabled, app_dir),
            daemon=True,
        ).start()

    def anthropicKeyChanged_(self, sender):
        key = self._anthropic_field.stringValue().strip()
        self._config["anthropic_api_key"] = key
        save_config(self._config)

    def doneClicked_(self, sender):
        # Save Anthropic key on close too (in case user tabbed away without Enter)
        key = self._anthropic_field.stringValue().strip()
        if key != self._config.get("anthropic_api_key", ""):
            self._config["anthropic_api_key"] = key
            save_config(self._config)
        self.orderOut_(None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _alert(title: str, body: str) -> None:
    a = AppKit.NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(body)
    a.runModal()


_LOCK_FILE = Path("/tmp/taskfloat.lock")
_lock_fd   = None   # held open for the lifetime of the process


def _ensure_single_instance() -> None:
    """Guarantee exactly one Floaty process using an exclusive flock.

    The lock fd stays open for the entire process lifetime; the OS releases
    it automatically on exit — no stale-PID races are possible.
    """
    import fcntl
    import atexit
    global _lock_fd
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another instance already holds the lock — exit silently.
        sys.exit(0)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
    atexit.register(lambda: _lock_fd.close() if _lock_fd else None)


def main():
    _ensure_single_instance()
    _check_and_heal_crash()   # detects crashes from prior run; no-op on clean exit

    # Use FloatyApp so sendEvent_ can intercept Carbon hotkey events
    app = FloatyApp.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    # Explicitly set the dock icon from the bundle — overrides any stale icon cache
    _bundle_resources = Foundation.NSBundle.mainBundle().resourcePath()
    _icon_path = str(Foundation.NSString.stringWithFormat_(
        "%@/floaty.icns", _bundle_resources
    ))
    _icon = AppKit.NSImage.alloc().initWithContentsOfFile_(_icon_path)
    if _icon:
        app.setApplicationIconImage_(_icon)

    try:
        config = load_config()
    except Exception as e:
        _alert("Floaty — Config Error", str(e))
        sys.exit(1)

    if not has_valid_tokens():
        AppDelegate.needs_auth = True

    AppDelegate.config = config
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
