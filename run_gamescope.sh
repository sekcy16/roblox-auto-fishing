#!/bin/sh
# Launcher script to run Flatpak Sober inside Gamescope
# Keeps the game on an isolated nested display so the auto-fishing bot can operate
# without moving the desktop cursor or interfering with user work on Monitor 2.

set -eu

# Size defaults: 1280x720 for lightweight, fast windowed mode (smaller screen size)
WIDTH="${GAMESCOPE_WIDTH:-1280}"
HEIGHT="${GAMESCOPE_HEIGHT:-720}"
DISPLAY_INDEX="${GAMESCOPE_DISPLAY_INDEX:-0}"
REFRESH="${GAMESCOPE_REFRESH:-60}"
FULLSCREEN="${GAMESCOPE_FULLSCREEN:-0}"

FS_FLAG=""
if [ "$FULLSCREEN" = "1" ] || [ "$FULLSCREEN" = "true" ]; then
    FS_FLAG="-f"
fi

# Sync Roblox target FPS to match REFRESH rate
SOBER_CFG="$HOME/.var/app/org.vinegarhq.Sober/config/sober/config.json"
if [ -f "$SOBER_CFG" ]; then
    sed -i -E "s/\"DFIntTaskSchedulerTargetFps\": [0-9]+/\"DFIntTaskSchedulerTargetFps\": $REFRESH/" "$SOBER_CFG" 2>/dev/null || true
fi

echo "Starting Roblox (Sober) in Gamescope..."
echo "Output: ${WIDTH}x${HEIGHT} @ ${REFRESH}Hz (Display index: $DISPLAY_INDEX)"
echo "Window mode: $([ -n "$FS_FLAG" ] && echo "Fullscreen" || echo "Windowed (Press Super+F to toggle fullscreen)")"

exec gamescope \
    -W "$WIDTH" \
    -H "$HEIGHT" \
    -r "$REFRESH" \
    -o "$REFRESH" \
    --framerate-limit "$REFRESH" \
    --display-index "$DISPLAY_INDEX" \
    --force-grab-cursor \
    $FS_FLAG \
    -- \
    flatpak run \
        org.vinegarhq.Sober "$@"
