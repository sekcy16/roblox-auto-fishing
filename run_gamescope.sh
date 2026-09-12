#!/bin/sh
# Launcher script to run Flatpak Sober inside Gamescope on Monitor 1
# Keeps the game on an isolated nested display so the auto-fishing bot can operate
# without moving the desktop cursor or interfering with user work on Monitor 2.

set -eu

# Discover monitor 1 resolution or default to 1920x1080
WIDTH="${GAMESCOPE_WIDTH:-1920}"
HEIGHT="${GAMESCOPE_HEIGHT:-1080}"
DISPLAY_INDEX="${GAMESCOPE_DISPLAY_INDEX:-0}"
REFRESH="${GAMESCOPE_REFRESH:-60}"

echo "Starting Roblox (Sober) in Gamescope..."
echo "Output: Display index $DISPLAY_INDEX (${WIDTH}x${HEIGHT} @ ${REFRESH}Hz)"
echo "Press Super+F if you need to toggle fullscreen."

exec gamescope \
    -W "$WIDTH" \
    -H "$HEIGHT" \
    -r "$REFRESH" \
    -o "$REFRESH" \
    --display-index "$DISPLAY_INDEX" \
    -f \
    -- \
    flatpak run org.vinegarhq.Sober "$@"
