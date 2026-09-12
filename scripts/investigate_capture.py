"""Investigate Xwayland capture inside Gamescope."""
import os
import sys
import time
import subprocess
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

runtime_lib = ROOT / ".runtime" / "usr" / "lib"
if runtime_lib.is_dir():
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)
    os.environ["TK_LIBRARY"] = str(runtime_lib / "tk8.6")

handshake_file = ROOT / "scripts" / "gs_cap_state.txt"
if handshake_file.exists():
    handshake_file.unlink()

wrapper = f"""
echo "DISP=$DISPLAY" > {handshake_file}
python3 -c '
import tkinter as tk
r = tk.Tk()
r.geometry("600x400+50+50")
r.configure(bg="#ff2255")
tk.Label(r, text="CAPTURE TEST WINDOW", bg="#ff2255", fg="#ffffff", font=("Helvetica", 20)).pack(expand=True)
r.mainloop()
'
"""

gs_proc = subprocess.Popen(["gamescope", "-W", "1280", "-H", "720", "--", "bash", "-c", wrapper])

gs_disp = None
for _ in range(30):
    if handshake_file.exists():
        content = handshake_file.read_text().strip()
        if content.startswith("DISP="):
            gs_disp = content.split("=")[1].strip()
            break
    time.sleep(0.2)

print("Gamescope display:", gs_disp)
time.sleep(2.0)

from Xlib import display, X

d = display.Display(gs_disp)
root = d.screen().root

# 1. Inspect windows
print("\n--- Listing windows on", gs_disp, "---")
app_win = None
for c in root.query_tree().children:
    try:
        attrs = c.get_attributes()
        name = c.get_wm_name()
        print(f"Win ID: {hex(c.id)}, Name: {name}, MapState: {attrs.map_state}")
        if attrs.map_state == X.IsViewable:
            app_win = c
    except Exception as e:
        print("Error getting win:", e)

# 2. Try mss on root
import mss
with mss.mss(display=gs_disp) as sct:
    mon = sct.monitors[0]
    raw = np.asarray(sct.grab(mon))
    print("\nMSS Root Grab min/max/mean:", raw.min(), raw.max(), raw.mean())

# 3. Try XGetImage on root window directly
try:
    img_root = root.get_image(0, 0, 1280, 720, X.ZPixmap, 0xFFFFFFFF)
    arr_root = np.frombuffer(img_root.data, dtype=np.uint8)
    print("XGetImage on Root min/max/mean:", arr_root.min(), arr_root.max(), arr_root.mean())
except Exception as e:
    print("XGetImage on Root failed:", e)

# 4. Try XGetImage on app_win directly!
if app_win:
    try:
        geom = app_win.get_geometry()
        print(f"\nApp Win Geometry: x={geom.x}, y={geom.y}, w={geom.width}, h={geom.height}")
        img_app = app_win.get_image(0, 0, geom.width, geom.height, X.ZPixmap, 0xFFFFFFFF)
        arr_app = np.frombuffer(img_app.data, dtype=np.uint8)
        print("XGetImage on App Window min/max/mean:", arr_app.min(), arr_app.max(), arr_app.mean())
    except Exception as e:
        print("XGetImage on App Window failed:", e)

# 5. Check PipeWire stream from Gamescope
print("\n--- Checking PipeWire video sources ---")
pw_check = subprocess.run(["pw-cli", "list-objects", "Node"], capture_output=True, text=True)
gamescope_nodes = [l for l in pw_check.stdout.splitlines() if "gamescope" in l.lower() or "node.name" in l.lower()]
print("Pipewire nodes matching gamescope:", gamescope_nodes[:10])

gs_proc.terminate()
gs_proc.wait()
