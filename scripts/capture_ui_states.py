"""Capture screenshots of the redesigned UI in various states and save as artifacts."""
import os
import sys
import time
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set runtime libraries if available
runtime_lib = ROOT / ".runtime" / "usr" / "lib"
if runtime_lib.is_dir():
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)
    os.environ["TK_LIBRARY"] = str(runtime_lib / "tk8.6")

import tkinter as tk
import auto_fishing

ARTIFACTS_DIR = Path("/home/sekcy16/.gemini/antigravity-ide/brain/6e1f4ac9-7f28-4fd1-a617-4d822b257667")
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def capture_widget(widget, filename):
    """Render and capture a Tkinter widget to a PNG file."""
    widget.update_idletasks()
    widget.update()
    time.sleep(0.1)
    
    # Try using pyscreenshot or mss or import xwd
    x = widget.winfo_rootx()
    y = widget.winfo_rooty()
    w = widget.winfo_width()
    h = widget.winfo_height()
    
    try:
        import mss
        with mss.mss() as sct:
            monitor = {"top": y, "left": x, "width": w, "height": h}
            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            img.save(str(filename))
            print(f"Saved: {filename} ({w}x{h})")
            return
    except Exception as e:
        print(f"mss failed: {e}")
        
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        img.save(str(filename))
        print(f"Saved: {filename} ({w}x{h})")
    except Exception as e:
        print(f"ImageGrab failed: {e}")


def main():
    root = tk.Tk()
    app = auto_fishing.FishingApp(root)
    root.update()

    # State 1: Initial state before selecting area
    app._draw_preview_placeholder()
    root.update()
    capture_widget(root, ARTIFACTS_DIR / "ui_state1_initial.png")

    # State 2: Ready state with real minigame bar preview
    fixture_path = ROOT / "tests" / "fixtures" / "night-minigame-crop.png"
    crop = np.asarray(Image.open(fixture_path).convert("RGB"))[:, :, ::-1]
    app._update_preview_display(crop)
    app.settings["rois"]["bar"] = [100, 200, 415, 65]
    app._update_readiness()
    root.update()
    capture_widget(root, ARTIFACTS_DIR / "ui_state2_ready.png")

    # State 3: Collapsible Advanced Settings expanded
    app.toggle_advanced()
    root.update()
    capture_widget(root, ARTIFACTS_DIR / "ui_state3_advanced_expanded.png")
    app.toggle_advanced()  # Fold back

    # State 4: Running / Tracking state simulation
    app.running = True
    app.main_action_btn.configure(text="■  หยุด Auto (F8)", style="Danger.TButton")
    app._set_status("Track: tracking | 62.4 FPS")
    root.update()
    capture_widget(root, ARTIFACTS_DIR / "ui_state4_running.png")
    app.running = False
    app._set_status("พร้อมเริ่ม")

    # State 5: Target lost / Searching state
    app.chip_target.configure(text="🟡  ช่องม่วง: กำลังค้นหาใหม่...", bg="#451a03", fg="#fbbf24")
    app._set_status("ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่")
    root.update()
    capture_widget(root, ARTIFACTS_DIR / "ui_state5_target_lost.png")

    # State 6: Narrow window responsive test (580px width)
    root.geometry("580x760")
    root.update()
    capture_widget(root, ARTIFACTS_DIR / "ui_state6_narrow.png")

    root.destroy()
    print("All UI state captures complete!")


if __name__ == "__main__":
    main()
