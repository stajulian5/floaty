# Floaty

A tiny floating widget for macOS that shows your current Google Calendar event — or the next one coming up. Lives on top of every app, across every Space. Drag it anywhere.

![Floaty widget showing a current event](screenshot.png)

---

## Install

Download `Floaty.dmg` from [Releases](https://github.com/stajulian5/floaty/releases/latest), open it, drag Floaty to Applications, and launch. No Python installation required.

<details>
<summary>Developer / legacy install</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/stajulian5/floaty/main/install.sh | bash
```

The script:
- Installs the Python dependency (PyObjC — macOS native bindings)
- Downloads Floaty
- Sets it to launch automatically at login
- Opens your browser to sign in with Google

</details>

**System requirements:** macOS 12 Monterey or later. No other dependencies.

---

## What it shows

| Badge | Meaning |
|---|---|
| 🚀 **NOW** | A task happening right now |
| 📅 **NOW** | A calendar event happening right now |
| 🚀 **NEXT** | Your next upcoming task |
| 📅 **NEXT** | Your next upcoming calendar event |

---

## How to use

| Action | What happens |
|---|---|
| **Click** | Opens Google Calendar |
| **Click ✓ circle** | Marks the task/event as done (confetti 🎉) |
| **Click +** | Add a new task |
| **Drag** | Move the widget anywhere — position is remembered |
| **Right-click** | Refresh · Add task · History · Hide · Quit |

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/stajulian5/floaty/main/uninstall.sh | bash
```

---

## Privacy

Floaty connects directly from your Mac to Google's APIs. No data passes through any server — it's just your Mac talking to Google. OAuth tokens are stored in your macOS Keychain.

Floaty uses anonymous launch analytics (GoatCounter). On first sign-in, your Google email is stored to count connected users. No data is sold or shared with third parties.

---

## Troubleshooting

**Widget doesn't appear after install** — check `/tmp/taskfloat.log` for errors.

**"Port 8765 in use"** during sign-in:
```bash
lsof -i :8765   # find the process
kill <PID>      # kill it, then re-run Floaty
```

**Re-authorize Google account:**
```bash
# Remove saved tokens from Keychain
security delete-generic-password -s "com.taskfloat"
# Restart Floaty — browser will open for sign-in
launchctl kickstart -k gui/$(id -u)/com.taskfloat
```

**Update to latest version:**

Download the latest `Floaty.dmg` from [Releases](https://github.com/stajulian5/floaty/releases/latest) and drag Floaty to Applications, replacing the existing copy.
