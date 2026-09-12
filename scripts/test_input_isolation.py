"""Test mouse input injection on Xwayland :1 while desktop :0 is focused elsewhere."""
import os
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

handshake_file = ROOT / "scripts" / "gs_input_state.txt"
event_log = ROOT / "scripts" / "gs_input_events.log"
if handshake_file.exists():
    handshake_file.unlink()
if event_log.exists():
    event_log.unlink()

# Window inside gamescope logging all button and motion events
inner_script = f"""
import tkinter as tk
import os

r = tk.Tk()
r.geometry("500x400+50+50")
r.title("IsolatedInputTarget")
log_path = "{event_log}"

def log(evt_type, e):
    with open(log_path, "a") as f:
        f.write(evt_type + " x=" + str(e.x) + " y=" + str(e.y) + " num=" + str(getattr(e, "num", 0)) + " state=" + str(e.state) + "\\n")

r.bind("<ButtonPress>", lambda e: log("PRESS", e))
r.bind("<ButtonRelease>", lambda e: log("RELEASE", e))
r.bind("<Motion>", lambda e: log("MOTION", e))
r.mainloop()
"""

wrapper = f"""
echo "DISP=$DISPLAY" > {handshake_file}
python3 -c '{inner_script}'
"""

# Launch gamescope
gs_proc = subprocess.Popen(["gamescope", "-W", "1280", "-H", "720", "--", "bash", "-c", wrapper])

gs_disp = None
for _ in range(30):
    if handshake_file.exists():
        c = handshake_file.read_text().strip()
        if c.startswith("DISP="):
            gs_disp = c.split("=")[1].strip()
            break
    time.sleep(0.2)

print("Gamescope nested display:", gs_disp)
time.sleep(2.0)

from Xlib import display, X
from Xlib.ext import xtest

d_desktop = display.Display(":0")
d_nested = display.Display(gs_disp)

# Record desktop mouse position initially
d0_pos_start = d_desktop.screen().root.query_pointer()
print(f"Desktop mouse on :0 before test: ({d0_pos_start.root_x}, {d0_pos_start.root_y})")

# Unfocus Gamescope on desktop :0 by giving focus to root or another desktop window
for w in d_desktop.screen().root.query_tree().children:
    try:
        name = w.get_wm_name()
        if name and "gamescope" not in name.lower() and "kwin" not in name.lower():
            attrs = w.get_attributes()
            if attrs.map_state == X.IsViewable:
                print("Setting desktop focus to:", name)
                d_desktop.set_input_focus(w, X.RevertToParent, X.CurrentTime)
                d_desktop.sync()
                break
    except Exception:
        pass

time.sleep(0.5)

# Find target window on nested display
nested_root = d_nested.screen().root
target_win = None
for w in nested_root.query_tree().children:
    try:
        attrs = w.get_attributes()
        if attrs.map_state == X.IsViewable and w.get_wm_name() != "steamcompmgr":
            target_win = w
            print("Found target window on nested display:", w.get_wm_name())
            break
    except Exception:
        pass

if not target_win:
    print("ERROR: Target window not found on nested display!")
else:
    print("\n--- Test 1: XTest ButtonPress and ButtonRelease on nested display ---")
    xtest.fake_input(d_nested, X.ButtonPress, 1)
    d_nested.sync()
    time.sleep(0.15)
    xtest.fake_input(d_nested, X.ButtonRelease, 1)
    d_nested.sync()
    time.sleep(0.5)
    
    d0_pos_after_xtest = d_desktop.screen().root.query_pointer()
    print(f"Desktop mouse on :0 after XTest: ({d0_pos_after_xtest.root_x}, {d0_pos_after_xtest.root_y})")
    
    log1 = event_log.read_text() if event_log.exists() else ""
    print("Event log after XTest:")
    print(log1.strip() or "(No events)")
    
    # Check if press and release were received
    has_press = "PRESS" in log1
    has_release = "RELEASE" in log1
    mouse_isolated = (d0_pos_start.root_x, d0_pos_start.root_y) == (d0_pos_after_xtest.root_x, d0_pos_after_xtest.root_y)
    
    print("\n====================")
    print("ISOLATION RESULT:")
    print("  Nested window received Press:", has_press)
    print("  Nested window received Release:", has_release)
    print("  Desktop :0 mouse moved:", not mouse_isolated)
    print("====================")

gs_proc.terminate()
gs_proc.wait()
