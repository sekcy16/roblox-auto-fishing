"""Roblox Auto Fishing - Windows UI.

Runs the normal desktop mode for Windows, requiring Roblox to be visible and focused.
Dual-monitor mode is strictly disabled and kept as an experimental research area
until background input injection is proven on real Roblox without SendInput.
"""

from __future__ import annotations

import platform
import sys
try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import auto_fishing
from auto_fishing import (
    APP_VERSION,
    BaseFishingApp,
    save_settings,
    set_dpi_awareness,
)


class WindowsFishingApp(BaseFishingApp):
    """Windows-specific Auto Fishing GUI.

    Features:
    - Normal Desktop mode only.
    - Prominent notice requiring game focus and shared mouse.
    - No Gamescope, Sober, X11, or DISPLAY options.
    - Clearly demarcated experimental area for future background capture research.
    """

    def __init__(self, root: tk.Tk):
        # Enforce Windows-specific settings isolation
        super().__init__(root)
        self.root.title(f"Roblox Auto Fishing {APP_VERSION} (Windows)")
        self.root.geometry("640x760")
        self.root.minsize(560, 660)

    def _build_mode_panel(self, outer: tk.Frame) -> None:
        """Render the Windows Desktop requirement card instead of Gamescope."""
        BG_CARD = "#1e1b29"
        BORDER_COLOR = "#322c44"
        TEXT_MAIN = "#f3f0fb"
        AMBER_WARN = "#fbbf24"
        GREEN_READY = "#34d399"

        card = self.tk.Frame(
            outer,
            bg=BG_CARD,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
        )
        card.pack(fill="x", pady=(0, 8))

        hdr = self.tk.Frame(card, bg=BG_CARD)
        hdr.pack(fill="x", pady=(0, 4))
        self.tk.Label(
            hdr,
            text="โหมดการทำงาน (Windows Desktop)",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 11, "bold"),
        ).pack(side="left")

        badge = self.tk.Label(
            hdr,
            text="● โหมดปกติ (จอเดียว)",
            fg=GREEN_READY,
            bg="#064e3b",
            padx=8,
            pady=2,
            font=(self.ui_font, 8, "bold"),
        )
        badge.pack(side="right")

        desc = (
            "📌 ข้อกำหนดโหมดปกติ: ต้องเปิดหน้าต่าง Roblox ค้างไว้ด้านหน้าตลอดเวลาที่ทำงาน "
            "โดยบอทจะควบคุมเมาส์ร่วมกับผู้ใช้ (กรุณาไม่ขยับเมาส์ขณะกำลังตกปลา)"
        )
        self.tk.Label(
            card,
            text=desc,
            fg=AMBER_WARN,
            bg=BG_CARD,
            font=(self.ui_font, 9),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

    def _build_advanced_extra(self, advanced_frame: tk.Frame) -> None:
        """Add research notice for future Windows dual-screen feasibility without clutter."""
        exp_frame = self.tk.Frame(
            advanced_frame,
            bg="#171520",
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground="#322c44",
        )
        exp_frame.pack(fill="x", pady=(8, 4))

        self.tk.Label(
            exp_frame,
            text="🧪 กำลังวิจัยโหมดสองจอ (Windows Graphics Capture)",
            fg="#a78bfa",
            bg="#171520",
            font=(self.ui_font, 9, "bold"),
        ).pack(anchor="w")

        exp_text = (
            "อยู่ระหว่างศึกษาความเป็นไปได้ (feasibility study) ในการจับภาพหน้าต่าง Roblox โดยเฉพาะ "
            "— ยังไม่เปิดใช้งานในรุ่นนี้ เนื่องจากต้องพิสูจน์ว่า Roblox รับอินพุตขณะไม่อยู่ด้านหน้าได้จริง "
            "โดยห้ามใช้ SendInput เพื่อไม่ให้รบกวนโปรแกรมอื่นของผู้ใช้"
        )
        self.tk.Label(
            exp_frame,
            text=exp_text,
            fg="#9d96b0",
            bg="#171520",
            font=(self.ui_font, 8),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

    def _save_roi_selection(
        self, name: str, area: list[int], bounds: tuple[int, int, int, int], image: Any
    ) -> None:
        super()._save_roi_selection(name, area, bounds, image)
        if "windows" not in self.settings or not isinstance(self.settings["windows"], dict):
            self.settings["windows"] = {}
        if "rois" not in self.settings["windows"]:
            self.settings["windows"]["rois"] = {}
        if name != "template":
            self.settings["windows"]["rois"][name] = area
            save_settings(self.settings)

    def _settings(self) -> dict[str, Any]:
        """Save settings under the 'windows' namespace to prevent cross-OS collisions."""
        settings = super()._settings()
        if "windows" not in self.settings or not isinstance(self.settings["windows"], dict):
            self.settings["windows"] = {}
        if "rois" in settings:
            self.settings["windows"]["rois"] = dict(settings["rois"])
        save_settings(self.settings)
        return settings


if platform.system() == "Windows":
    auto_fishing.FishingApp = WindowsFishingApp


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Roblox Auto Fishing (Windows)")
    parser.add_argument("--replay", metavar="DIR", help="analyze saved frames without mouse input")
    args = parser.parse_args()
    if args.replay:
        for index, result in enumerate(auto_fishing.replay_frames(args.replay), 1):
            print(f"{index}: state=vision-{result[0]} marker={result[1]} target={result[2]}")
        return
    set_dpi_awareness()
    root = tk.Tk()
    app = WindowsFishingApp(root)
    root.mainloop()


if __name__ == "__main__":
    auto_fishing._bootstrap()
    main()
