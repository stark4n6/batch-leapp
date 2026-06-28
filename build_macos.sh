#!/usr/bin/env bash
#
# Build a self-contained macOS .app of the Batch LEAPP GUI.
#
# Requirements:  python3 with tkinter, and PyInstaller (pip install pyinstaller)
# Usage:         ./build_macos.sh
# Output:        dist/Batch LEAPP.app   (≈30 MB, bundles Python + Tk + the app)
#
# The app is UNSIGNED. On the machine that built it, it opens normally. If you
# zip and share it, the recipient must right-click → Open the first time (or run
# `xattr -dr com.apple.quarantine "Batch LEAPP.app"`) to clear Gatekeeper.
#
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "PyInstaller is not installed. Run:  python3 -m pip install pyinstaller" >&2
    exit 1
fi

python3 -m PyInstaller --noconfirm --windowed --clean \
    --name "Batch LEAPP" \
    --icon leapps.icns \
    --osx-bundle-identifier org.leapps.batch-leapp \
    batch_leapp_gui.py

echo
echo "Built: dist/Batch LEAPP.app"
echo "Drag it to /Applications, or zip it for sharing:"
echo "  ditto -c -k --sequesterRsrc --keepParent 'dist/Batch LEAPP.app' Batch-LEAPP-macos.zip"
