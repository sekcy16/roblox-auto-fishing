"""Test launching Flatpak Sober inside Gamescope and capturing its window."""
import os
import sys
import time
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

runtime_lib = ROOT / ".runtime" / "usr" / "lib"
if runtime_lib.is_dir():
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)
    os.environ["TK_LIBRARY"] = str(runtime_lib / "tk8.6")

# Find existing X displays before starting
existing_displays = set(p.name for p in Path("/tmp/.X11-unix").glob("X*"))
print("Existing displays in /tmp/.X11-unix:", existing_displays)

cmd = [
    "gamescope",
    "-W", "1280",
    "-H", "720",
    "--",
    "flatpak", "run", "org.vinegarhq.Sober"
]

print("Launching Gamescope with Sober:", " ".join(cmd))
gs_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

# Wait for new X display
new_disp = None
for _ in range(40):
    current = set(p.name for p in Path("/tmp/.X11-unix").glob("X*"))
    diff = current - existing_displays
    if diff:
        disp_name = sorted(diff)[0]
        new_disp = ":" + disp_name[1:]
        break
    time.sleep(0.25)

if not new_disp:
    print("FAIL: No new X display appeared!")
    gs_proc.terminate()
    sys.exit(1)

print(f"Discovered nested X display: {new_disp}")

# Now connect to new_disp and wait for Sober window
from Xlib import display, X

d = None
for _ in range(30):
    try:
        d = display.Display(new_disp)
        break
    except Exception:
        time.sleep(0.2)

if not d:
    print("FAIL: Could not connect to nested display", new_disp)
    gs_proc.terminate()
    sys.exit(1)

print("Connected to nested X display successfully.")

# Look for Sober window for up to 15 seconds
sober_win = None
root = d.screen().root

for i in range(30):
    d.sync()
    children = root.query_tree().children
    for c in children:
        try:
            name = c.get_wm_name()
            attrs = c.get_attributes()
            print(f"[{i*0.5:.1f}s] Window ID={hex(c.id)} Name={name} MapState={attrs.map_state}")
            if attrs.map_state == X.IsViewable and name and "steamcompmgr" not in name.lower():
                sober_win = c
                break
        except Exception:
            pass
    if sober_win:
        break
    time.sleep(0.5)

if sober_win:
    geom = sober_win.get_geometry()
    print(f"\nSUCCESS: Found Sober window: {sober_win.get_wm_name()} {geom.width}x{geom.height}+{geom.x}+{geom.y}")
    
    # Try capturing Sober's window
    raw = sober_win.get_image(0, 0, geom.width, geom.height, X.ZPixmap, 0xFFFFFFFF)
    img = Image.frombytes("RGB", (geom.width, geom.height), raw.data, "raw", "BGRX")
    out_path = ROOT / "scripts" / "sober_capture.png"
    img.save(str(out_path))
    print(f"SUCCESS: Captured Sober frame saved to {out_path} (size {img.size})")
else:
    print("NOTE: Sober didn't map a window within timeout or needs auth/interaction")

print("\nTerminating Gamescope + Sober test...")
gs_proc.terminate()
try:
    gs_proc.wait(timeout=3)
except Exception:
    gs_proc.kill()

print("Test finished.")
