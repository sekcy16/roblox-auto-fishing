"""Capture screenshot of the Dimmed ROI selection overlay."""
import os
import sys
import time
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

import tkinter as tk
from scripts.capture_ui_states import capture_widget, ARTIFACTS_DIR

def main():
    root = tk.Tk()
    root.withdraw()

    picker = tk.Toplevel(root)
    picker.title("ลากกรอบพื้นที่ แถบมินิเกม")
    picker.configure(bg="#121118")

    # Banner
    banner = tk.Frame(picker, bg="#1e1b29", padx=14, pady=8)
    banner.pack(fill="x")
    tk.Label(banner, text="📌  คลิกค้างแล้วลากเพื่อเลือกพื้นที่ • Esc เพื่อยกเลิก",
             fg="#f3f0fb", bg="#1e1b29", font=("Noto Sans Thai", 11, "bold")).pack(side="left")

    canvas_w, canvas_h = 800, 500
    canvas = tk.Canvas(picker, width=canvas_w, height=canvas_h, bg="#1a1824", highlightthickness=0)
    canvas.pack()

    # Load or generate a mock game background
    bg_img = Image.new("RGB", (canvas_w, canvas_h), (35, 30, 48))
    # Draw some mock game UI elements
    from PIL import ImageDraw
    draw = ImageDraw.Draw(bg_img)
    draw.rectangle([50, 50, 750, 450], fill=(24, 22, 34))
    # Simulated minigame bar
    draw.rectangle([220, 230, 580, 270], fill=(15, 14, 20), outline=(60, 55, 80))
    draw.rectangle([340, 230, 430, 270], fill=(139, 92, 246))  # purple bar
    draw.line([400, 225, 400, 275], fill=(255, 255, 255), width=3)  # pointer

    from PIL import ImageTk
    photo = ImageTk.PhotoImage(bg_img)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas._photo = photo

    # Now apply our Dimmed ROI Overlay during drag
    # Selection box around the minigame bar: (200, 220) to (600, 280)
    x0, y0, x1, y1 = 200, 220, 600, 280
    w, h = x1 - x0, y1 - y0

    # 4 Dimming rectangles outside
    canvas.create_rectangle(0, 0, canvas_w, y0, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
    canvas.create_rectangle(0, y1, canvas_w, canvas_h, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
    canvas.create_rectangle(0, y0, x0, y1, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
    canvas.create_rectangle(x1, y0, canvas_w, y1, fill="#000000", stipple="gray50", outline="", tags="roi_dim")

    # Double purple border
    canvas.create_rectangle(x0, y0, x1, y1, outline="#2e1065", width=3, tags="roi_border")
    canvas.create_rectangle(x0, y0, x1, y1, outline="#d8b4fe", width=1, tags="roi_border")

    # Corner brackets
    c_len = 14
    canvas.create_line(x0, y0 + c_len, x0, y0, x0 + c_len, y0, fill="#c084fc", width=2, tags="roi_border")
    canvas.create_line(x1 - c_len, y0, x1, y0, x1, y0 + c_len, fill="#c084fc", width=2, tags="roi_border")
    canvas.create_line(x0, y1 - c_len, x0, y1, x0 + c_len, y1, fill="#c084fc", width=2, tags="roi_border")
    canvas.create_line(x1 - c_len, y1, x1, y1, x1 - c_len, y1, fill="#c084fc", width=2, tags="roi_border")

    # Floating dimension badge
    badge_x, badge_y = (x0 + x1) / 2.0, y0 - 14
    half_badge = 50.0
    canvas.create_rectangle(badge_x - half_badge, badge_y - 10, badge_x + half_badge, badge_y + 10,
                            fill="#1e1b29", outline="#8b5cf6", width=1, tags="roi_badge")
    canvas.create_text(badge_x, badge_y, text="400 × 60 px", fill="#ffffff",
                       font=("Noto Sans Thai", 9, "bold"), tags="roi_badge")

    # Bottom bar
    bottom_bar = tk.Frame(picker, bg="#1e1b29", padx=14, pady=8)
    bottom_bar.pack(fill="x")
    tk.Label(bottom_bar, text="เลือก: กว้าง 400 × สูง 60 px", fg="#9d96b0", bg="#1e1b29",
             font=("Noto Sans Thai", 10)).pack(side="left")

    btn_box = tk.Frame(bottom_bar, bg="#1e1b29")
    btn_box.pack(side="right")
    tk.Button(btn_box, text="ยกเลิก (Esc)", bg="#272336", fg="#f3f0fb", relief="flat", padx=10, pady=4).pack(side="right", padx=(6, 0))
    tk.Button(btn_box, text="ใช้พื้นที่นี้ (Enter)", bg="#7c3aed", fg="#ffffff", relief="flat", padx=12, pady=4).pack(side="right")

    picker.update()
    capture_widget(picker, ARTIFACTS_DIR / "ui_state7_dimmed_selection_overlay.png")
    root.destroy()
    print("Overlay capture complete!")

if __name__ == "__main__":
    main()
