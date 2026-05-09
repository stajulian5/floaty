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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".taskfloat" / "config.json"
KEYCHAIN_SERVICE = "com.taskfloat"
UPDATE_URL = "https://raw.githubusercontent.com/stajulian5/floaty/main/taskfloat.py"
VERSION = "1.0.0"  # bump this on every release


def _auto_update() -> None:
    """Check GitHub for a newer version and restart if one is found. Runs in background."""
    def _check():
        try:
            req = urllib.request.Request(
                UPDATE_URL,
                headers={"User-Agent": "Floaty-updater/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                new_src = resp.read().decode("utf-8")
            # Extract VERSION from the downloaded source
            for line in new_src.splitlines():
                if line.startswith("VERSION = "):
                    remote_ver = line.split('"')[1]
                    if remote_ver != VERSION:
                        # Write new version over ourselves then restart
                        this_file = Path(__file__).resolve()
                        this_file.write_text(new_src)
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    break
        except Exception:
            pass  # silently skip — no internet, GitHub down, etc.
    threading.Thread(target=_check, daemon=True).start()
REDIRECT_URI = "http://localhost:8765/callback"
OAUTH_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/tasks"
)
WINDOW_ORIGIN_KEY = "taskfloat.windowOrigin"
REFRESH_INTERVAL = 60  # seconds
GIF_DIR = Path.home() / ".taskfloat" / "gifs"

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
    candidates = [g for g in GIF_DIR.glob("hype_*.gif") if g.stat().st_size >= 20_000]
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
    Security.SecItemDelete(query)
    attrs = dict(query)
    attrs[Security.kSecValueData] = value.encode("utf-8")
    attrs[Security.kSecAttrAccessible] = Security.kSecAttrAccessibleAfterFirstUnlock
    status = Security.SecItemAdd(attrs, None)
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
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH} not found.\n"
            "Create it with your Google OAuth credentials.\n"
            "See README.md for instructions."
        )
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)
    if "client_id" not in cfg or "client_secret" not in cfg:
        raise ValueError(f"{CONFIG_PATH} must contain 'client_id' and 'client_secret'")
    return cfg


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------

_token_cache: dict = {}  # {"access_token": str, "expiry": datetime}
_status_item_ref = None  # strong reference to NSStatusItem, prevents GC


def build_auth_url(config: dict) -> str:
    params = {
        "client_id": config["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def wait_for_oauth_code(open_url: str) -> str:
    """Open browser, show a native dialog asking for the redirect URL."""
    webbrowser.open(open_url)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Connect TaskFloat to Google")
    alert.setInformativeText_(
        "Your browser just opened for Google sign-in.\n\n"
        "After clicking Allow, your browser will show a "
        "'This site can't be reached' page — that's expected.\n\n"
        "Copy the full URL from the address bar and paste it below:"
    )
    alert.addButtonWithTitle_("Connect")
    alert.addButtonWithTitle_("Cancel")
    field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 380, 22))
    field.setPlaceholderString_("http://localhost:8765/callback?code=…")
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)
    response = alert.runModal()
    if response != AppKit.NSAlertFirstButtonReturn:
        raise RuntimeError("Authorization cancelled.")
    redirect_url = str(field.stringValue()).strip()
    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)
    if "code" not in params:
        raise ValueError("No 'code' found in the URL. Make sure you copied the full address bar URL.")
    return params["code"][0]


def exchange_code(code: str, config: dict) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri": REDIRECT_URI,
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
        raise RuntimeError("No refresh token — please re-authorize TaskFloat")
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


def do_oauth_flow(config: dict) -> None:
    """Full OAuth2 flow: open browser, wait for code, exchange, store tokens."""
    url = build_auth_url(config)
    code = wait_for_oauth_code(url)
    tokens = exchange_code(code, config)
    keychain_write("access_token", tokens["access_token"])
    if "refresh_token" in tokens:
        keychain_write("refresh_token", tokens["refresh_token"])
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600) - 60)
    _token_cache.update({"access_token": tokens["access_token"], "expiry": expiry})


def has_valid_tokens() -> bool:
    return keychain_read("refresh_token") is not None


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
            # Only include tasks due today or with no due date
            due = task.get("due", "")
            if not due or due.startswith(today_str):
                t = task.get("title", "").strip()
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
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Force token refresh
            _token_cache.clear()
            token = get_valid_token(config)
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        else:
            raise

    items = data.get("items", [])
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
        event_id = item.get("id", "")
        is_task = title in task_titles
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

    # Fallback: if no timed calendar events, show tasks from the Tasks API directly
    if not results:
        results = _fetch_task_only_events(config, token)

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


def delete_calendar_event(config: dict, event_id: str) -> None:
    token = get_valid_token(config)
    url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{urllib.parse.quote(event_id, safe='')}"
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
    """Return the id of the '🚀 Today' task list, creating it if necessary."""
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

        # Spawn throws 2 and 3 at t=3 and t=6
        throw_idx = int(self._elapsed / 3.0)
        if self._spawned < 3 and throw_idx >= self._spawned:
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

WIDGET_W        = 240
WIDGET_H_SINGLE = 56
WIDGET_H_DOUBLE = 100
CORNER_R        = 12
PADDING_L       = 28   # left text margin (shifted right to make room for "+")
PLUS_X          = 7    # x position of the always-visible "+" button
BLOCK_H         = 46   # height of one event block
HISTORY_HEADER_H = 20  # height of the "recently crushed" label
HISTORY_ITEM_H   = 15  # height per history row
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
        self._msg    = (msg[:48] + "…") if len(msg) > 48 else msg
        self._events = []
        self._resize_to_fit()
        self.setNeedsDisplay_(True)

    def setSuccess_(self, msg, duration=1.8):
        """Show a transient green toast overlay; auto-clears after `duration` seconds."""
        self._toast_text = (msg, AppKit.NSColor.systemGreenColor())
        self.setNeedsDisplay_(True)
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            duration, self, "clearToast:", None, False
        )

    def clearToast_(self, _timer_or_none):
        """Called by NSTimer (or manually) to remove the toast overlay."""
        self._toast_text = None
        self.setNeedsDisplay_(True)

    # ---- Window resize ---------------------------------------------------

    def _history_extra_height(self):
        if not self._history_visible or not self._history:
            return 0
        return HISTORY_HEADER_H + len(self._history) * HISTORY_ITEM_H + 8

    def _resize_to_fit(self, animate=False):
        win = self.window()
        if not win:
            return
        base_h = WIDGET_H_DOUBLE if len(self._events) >= 2 else WIDGET_H_SINGLE
        h = base_h + self._history_extra_height()
        old = win.frame()
        if int(old.size.height) == int(h):
            return
        # Keep the top-left corner fixed when resizing
        new_origin = AppKit.NSPoint(old.origin.x, old.origin.y + old.size.height - h)
        new_frame  = AppKit.NSMakeRect(new_origin.x, new_origin.y, WIDGET_W, h)
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
        self.setFrame_(AppKit.NSMakeRect(0, 0, WIDGET_W, h))
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

        # Background pill
        pill = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(bounds, 2, 2), CORNER_R, CORNER_R
        )
        (p["bg_busy"] if self._checking_off else p["bg"]).setFill()
        pill.fill()

        # "+" color: green when free, grey when tasks are present
        has_tasks = self._status == "ok" and len(self._events) > 0
        plus_color = p["plus_grey"] if has_tasks else AppKit.NSColor.systemGreenColor()
        plus_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(18, AppKit.NSFontWeightLight),
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
            self._draw_single_message("…", AppKit.NSColor.systemGrayColor(), self._msg, "", 0, p)
        elif self._status == "error":
            self._draw_single_message("! ERROR", AppKit.NSColor.systemRedColor(), "Could not load", self._msg, 0, p)
        elif not self._events:
            self._check_rects = []
            self._draw_free_state(w, h, p)
        else:
            self._check_rects = []
            for i, ev in enumerate(self._events[:2]):
                y = i * (BLOCK_H + 2)
                if i == 1:
                    div_y = BLOCK_H + 1
                    p["sep_h"].setFill()
                    AppKit.NSRectFill(AppKit.NSMakeRect(PADDING_L, div_y, w - PADDING_L * 2, 1))
                self._draw_event(ev, y, i, p)

        # History section (below main content)
        if self._history_visible and self._history:
            base_h = WIDGET_H_DOUBLE if len(self._events) >= 2 else WIDGET_H_SINGLE
            self._draw_history_section(base_h, w, p)

        # Toast overlay (e.g. "✓ Task added!") — drawn on top of everything
        if self._toast_text:
            text, color = self._toast_text
            t_attrs = {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(11.5, AppKit.NSFontWeightSemibold),
                AppKit.NSForegroundColorAttributeName: color,
            }
            ts = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, t_attrs)
            tx = (w - ts.size().width) / 2
            ty = h / 2 - ts.size().height / 2
            ts.drawAtPoint_(AppKit.NSPoint(tx, ty))

    def _badge_color(self, is_current):
        return AppKit.NSColor.systemGreenColor() if is_current else AppKit.NSColor.systemOrangeColor()

    def _draw_event(self, ev, y, event_idx, p):
        is_task = ev.get("is_task", False)
        if ev["is_current"]:
            badge_text  = "🚀 NOW" if is_task else "● NOW"
        else:
            badge_text  = "🚀 NEXT" if is_task else "▶ NEXT"
        badge_color = self._badge_color(ev["is_current"])
        title = ev["title"]
        max_title = 30 if is_task else 34
        title = (title[:max_title] + "…") if len(title) > max_title else title
        self._draw_single_message(badge_text, badge_color, title, format_time_range(ev), y, p)

        # Check circle — only for tasks
        if is_task:
            w = self.bounds().size.width
            r = 6
            cx = w - 14
            cy = y + 26  # align with title text (drawn at y+20, ~12pt font)
            check_rect = AppKit.NSMakeRect(cx - r, cy - r, r * 2, r * 2)
            self._check_rects.append((event_idx, check_rect))
            circle_path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                AppKit.NSInsetRect(check_rect, 1, 1)
            )
            if self._flash_idx == event_idx:
                # Momentary filled green circle on click
                AppKit.NSColor.systemGreenColor().setFill()
                circle_path.fill()
            else:
                p["check_circle"].setStroke()
                circle_path.setLineWidth_(1.5)
                circle_path.stroke()

    def _draw_history_section(self, y, w, p):
        # Separator
        p["history_sep"].setFill()
        AppKit.NSRectFill(AppKit.NSMakeRect(PADDING_L, y + 1, w - PADDING_L * 2, 0.5))

        # Header
        hdr_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(8, AppKit.NSFontWeightSemibold),
            AppKit.NSForegroundColorAttributeName: p["history_hdr"],
            AppKit.NSKernAttributeName: 1.4,
        }
        AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "🚀  RECENTLY CRUSHED", hdr_attrs
        ).drawAtPoint_(AppKit.NSPoint(PADDING_L, y + 6))

        # Items
        item_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(10, AppKit.NSFontWeightLight),
            AppKit.NSForegroundColorAttributeName: p["history_item"],
        }
        iy = y + HISTORY_HEADER_H + 4
        for title in self._history:
            trunc = (title[:30] + "…") if len(title) > 30 else title
            AppKit.NSAttributedString.alloc().initWithString_attributes_(
                "✓  " + trunc, item_attrs
            ).drawAtPoint_(AppKit.NSPoint(PADDING_L, iy))
            iy += HISTORY_ITEM_H


    def _draw_free_state(self, w, h, p):
        cy = h / 2
        q_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(10.5, AppKit.NSFontWeightMedium),
            AppKit.NSForegroundColorAttributeName: p["free_text"],
        }
        q_str = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "🚀  What are we crushing next?", q_attrs
        )
        q_str.drawAtPoint_(AppKit.NSPoint(PADDING_L, cy - q_str.size().height / 2))

    def _draw_single_message(self, badge, badge_color, title, subtitle, y, p):
        badge_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(9, AppKit.NSFontWeightSemibold),
            AppKit.NSForegroundColorAttributeName: badge_color,
            AppKit.NSKernAttributeName: 1.3,
        }
        title_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightMedium),
            AppKit.NSForegroundColorAttributeName: p["title"],
        }
        sub_attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(10, AppKit.NSFontWeightRegular),
            AppKit.NSForegroundColorAttributeName: p["subtitle"],
        }
        AppKit.NSAttributedString.alloc().initWithString_attributes_(badge, badge_attrs)\
            .drawAtPoint_(AppKit.NSPoint(PADDING_L, y + 8))
        AppKit.NSAttributedString.alloc().initWithString_attributes_(title, title_attrs)\
            .drawAtPoint_(AppKit.NSPoint(PADDING_L, y + 20))
        if subtitle:
            AppKit.NSAttributedString.alloc().initWithString_attributes_(subtitle, sub_attrs)\
                .drawAtPoint_(AppKit.NSPoint(PADDING_L, y + 34))

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

        # Busy state: clicking the main body opens Google Calendar
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
                Foundation.NSURL.URLWithString_("https://calendar.google.com")
            )

    def rightMouseUp_(self, event):
        delegate = AppKit.NSApp.delegate()
        menu = AppKit.NSMenu.alloc().init()

        ri = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh", "handleRefresh:", "")
        ri.setTarget_(self)
        menu.addItem_(ri)

        ai = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("+ Add Task", "menuAddTask:", "")
        ai.setTarget_(delegate)
        menu.addItem_(ai)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        hi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide Floaty", "hideFloaty:", "")
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
        gif_path = _random_hype_gif()
        if gif_path:
            img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(gif_path))
            iv  = AppKit.NSImageView.alloc().initWithFrame_(
                AppKit.NSMakeRect(M, 210, W - M * 2, 230)
            )
            iv.setImage_(img)
            iv.setAnimates_(True)
            iv.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            cv.addSubview_(iv)
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

    _crushed_history       = []   # persisted across restarts via UserDefaults
    _crushed_today         = 0    # tasks crushed today (resets at midnight)
    _last_current_id       = None # event ID being tracked for auto-extend
    _last_current_end      = None # its current end time (datetime, UTC-aware)
    _last_current_orig_end = None # original end before any extension
    _last_current_is_task  = False

    def applicationDidFinishLaunching_(self, notification):
        self._refresh_timer = None
        _auto_update()  # check for updates silently in background

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

        size = AppKit.NSSize(WIDGET_W, WIDGET_H_SINGLE)
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

        status_menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._hide_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide Floaty", "hideFloaty:", "")
        self._hide_item.setTarget_(self)
        status_menu.addItem_(self._hide_item)

        real_quit = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "reallyQuit:", "Q")
        real_quit.setTarget_(self)
        status_menu.addItem_(real_quit)

        self._status_menu = status_menu
        self._status_item.setMenu_(status_menu)

        # config and tokens already set up in main() before the event loop started
        self._start_timer()
        threading.Thread(target=self._do_refresh, daemon=True).start()
        threading.Thread(target=_ensure_hype_gifs, daemon=True).start()

        # Heartbeat: keep panel always on top regardless of what else happens
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "keepAlive:", None, True
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

            # Auto-extend: only tasks that ended without being checked off
            tracked_id      = AppDelegate._last_current_id
            tracked_end     = AppDelegate._last_current_end
            orig_end        = AppDelegate._last_current_orig_end
            tracked_is_task = AppDelegate._last_current_is_task
            if tracked_id and tracked_is_task and tracked_end and tracked_end <= now:
                current_ids = {e["id"] for e in events if e.get("is_current")}
                if tracked_id not in current_ids:
                    max_end = (orig_end or tracked_end) + timedelta(hours=2)
                    new_end = tracked_end + timedelta(minutes=15)
                    # Cap at the start of the next non-task calendar event
                    next_event = next((e for e in events if not e.get("is_current") and not e.get("is_task")), None)
                    if next_event:
                        new_end = min(new_end, next_event["start"])
                    new_end = min(new_end, max_end)
                    if new_end > now:
                        try:
                            extend_calendar_event(AppDelegate.config, tracked_id, new_end)
                            events = fetch_current_or_next_event(AppDelegate.config)
                            # Brief toast showing extension happened (auto-clears after 1.8s)
                            cv = self._content_view
                            self._run_on_main(lambda: cv.setSuccess_("+15 min — keep going!"))
                        except Exception:
                            pass

            # Track the current event for next cycle
            current = next((e for e in events if e.get("is_current")), None)
            if current:
                if current["id"] != AppDelegate._last_current_id:
                    AppDelegate._last_current_orig_end = current["end"]
                AppDelegate._last_current_id       = current["id"]
                AppDelegate._last_current_end      = current["end"]
                AppDelegate._last_current_is_task  = current.get("is_task", False)
            else:
                AppDelegate._last_current_id       = None
                AppDelegate._last_current_end      = None
                AppDelegate._last_current_orig_end = None
                AppDelegate._last_current_is_task  = False

            self._run_on_main(lambda: self._content_view.setEvents_(events))
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

        screen = self._panel.screen() if self._panel else None
        dialog = _HypeDialog.alloc().initWithOpenCal_screen_(open_cal, screen)
        task_title, open_cal = dialog.run()
        # Restore accessory status so the panel doesn't vanish when another app activates
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
        # Clear auto-extend tracking so we don't try to extend a deleted event
        AppDelegate._last_current_id       = None
        AppDelegate._last_current_end      = None
        AppDelegate._last_current_orig_end = None
        AppDelegate._last_current_is_task  = False
        try:
            is_task_only = event.get("task_only", False)
            event_id = event.get("id", "")
            if not is_task_only and event_id:
                # Calendar event → delete it from Calendar
                delete_calendar_event(AppDelegate.config, event_id)
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
            # Restart after confetti finishes — gives a guaranteed-clean fresh panel.
            # All state is already persisted: position (UserDefaults), tokens (Keychain),
            # crushed history and count (UserDefaults).
            def _restart():
                time.sleep(10.5)   # confetti lasts ~9.5 s
                os.execv(sys.executable, [sys.executable] + sys.argv)
            threading.Thread(target=_restart, daemon=True).start()
        except Exception as e:
            err = str(e)
            self._run_on_main(lambda: setattr(self._content_view, '_checking_off', False))
            self._run_on_main(lambda: self._content_view.setError_(err))

    def menuRefresh_(self, sender):
        self.scheduleImmediateRefresh()

    def menuAddTask_(self, sender):
        self.showAddTaskDialog()

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
# Entry point
# ---------------------------------------------------------------------------

def _alert(title: str, body: str) -> None:
    a = AppKit.NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(body)
    a.runModal()


_PID_FILE = Path("/tmp/taskfloat.pid")


def _ensure_single_instance() -> None:
    """Kill any stale instance so only one Floaty runs at a time."""
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, 15)  # SIGTERM
                time.sleep(0.4)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    _PID_FILE.write_text(str(os.getpid()))


def main():
    _ensure_single_instance()

    # Use FloatyApp so sendEvent_ can intercept Carbon hotkey events
    app = FloatyApp.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    try:
        config = load_config()
    except Exception as e:
        _alert("TaskFloat — Config Error", str(e))
        sys.exit(1)

    if not has_valid_tokens():
        try:
            do_oauth_flow(config)
        except Exception as e:
            _alert("TaskFloat — Auth Failed", str(e))
            sys.exit(1)

    AppDelegate.config = config
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
