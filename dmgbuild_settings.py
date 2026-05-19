# dmgbuild settings for Floaty installer DMG
# Run: dmgbuild -s dmgbuild_settings.py "Floaty" Floaty.dmg

import os

application = defines.get('app', 'dist/Floaty.app')
appname = os.path.basename(application)

# ── Window appearance ──────────────────────────────────────────────────────────
format = defines.get('format', 'UDZO')
size = None

# Window size and layout
window_rect = ((200, 120), (540, 380))
icon_size = 120
icon_locations = {
    appname: (140, 190),
    'Applications': (400, 190),
}

# Background: branded dark slate image
background = 'dmg_background.png'

# Show icon preview
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
sidebar_width = 180

# Files to include
files = [application]
symlinks = {'Applications': '/Applications'}

# ── Volume badge icon ──────────────────────────────────────────────────────────
# badge_icon uses the app's own icon automatically — no override needed
