"""
py2app build script for Floaty.
Run:  python3 setup.py py2app
Output: dist/Floaty.app  (~80-120 MB, fully self-contained — no Python install required)
"""
from setuptools import setup

APP = ["taskfloat.py"]

OPTIONS = {
    "iconfile": "floaty.icns",
    "argv_emulation": False,   # we manage our own NSApp lifecycle
    "semi_standalone": False,  # bundle a full Python interpreter
    "site_packages": True,     # include user site-packages (where PyObjC lives)

    # Packages py2app must copy whole (not just the .pyc files it discovers)
    "packages": [
        "objc",
        "AppKit",
        "Foundation",
        "Security",
        "CoreFoundation",
        "Cocoa",
        "WebKit",
    ],

    # Extra modules not always detected by static analysis
    "includes": [
        "json", "os", "random", "subprocess", "sys", "threading",
        "time", "webbrowser", "urllib.parse", "urllib.request",
        "urllib.error", "datetime", "pathlib", "ctypes", "hashlib",
        "http.server", "socket",
        "objc", "AppKit", "Foundation", "Security",
        "CoreFoundation", "Cocoa",
    ],

    "plist": {
        "CFBundleName":               "Floaty",
        "CFBundleDisplayName":        "Floaty",
        "CFBundleIdentifier":         "com.floaty",
        "CFBundleVersion":            "1.2.8",
        "CFBundleShortVersionString": "1.2.8",
        "NSHumanReadableCopyright":   "© 2025 Julian Stastny",
        "NSAppTransportSecurity":     {"NSAllowsArbitraryLoads": True},
    },
}

setup(
    name="Floaty",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
