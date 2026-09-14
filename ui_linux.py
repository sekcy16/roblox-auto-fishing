"""Roblox Auto Fishing - Linux UI.

Integrates standard Desktop X11 mode and isolated Gamescope dual-monitor mode
for Sober/Roblox on Linux.
"""

from __future__ import annotations

import multiprocessing
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SOBER_CONFIG_RELATIVE_PATH = Path(".var/app/org.vinegarhq.Sober/config/sober/config.json")
LOW_GRAPHICS_BACKUP_PATH = ROOT / "backups" / "sober-config-before-low.json"
LOW_GRAPHICS_OVERRIDES = {
    "DFIntDebugFRMQualityLevelOverride": 1,
    "DFFlagTextureQualityOverrideEnabled": True,
    "DFIntTextureQualityOverride": 0,
}


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text through a sibling temp file, then replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def configure_sober_low_graphics(
    config_path: Path | None = None,
    backup_path: Path | None = None,
    refresh_rate: int | None = None,
) -> None:
    """Apply the supported low graphics overrides before launching Sober."""
    config_path = Path(config_path) if config_path is not None else Path.home() / SOBER_CONFIG_RELATIVE_PATH
    backup_path = Path(backup_path) if backup_path is not None else LOW_GRAPHICS_BACKUP_PATH
    if not config_path.is_file():
        raise ValueError(f"ไม่พบไฟล์ตั้งค่า Sober: {config_path} — เปิด Sober หนึ่งครั้งก่อนลองใหม่")

    original = config_path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^[ \t]*\{", original)
    if match is None:
        raise ValueError("ไฟล์ตั้งค่า Sober ไม่มี JSON ที่อ่านได้")
    json_start = match.end() - 1
    try:
        settings = json.loads(original[json_start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"ไฟล์ตั้งค่า Sober ไม่ถูกต้อง: {exc.msg}") from exc
    if not isinstance(settings, dict):
        raise ValueError("ไฟล์ตั้งค่า Sober ต้องเป็น JSON object")
    fflags = settings.get("fflags", {})
    if not isinstance(fflags, dict):
        raise ValueError("ไฟล์ตั้งค่า Sober ต้องมี fflags เป็น JSON object")
    if refresh_rate is not None and (
        isinstance(refresh_rate, bool) or not isinstance(refresh_rate, int) or not 30 <= refresh_rate <= 360
    ):
        raise ValueError("ค่า FPS ต้องอยู่ระหว่าง 30 ถึง 360")

    if backup_path.exists() and not backup_path.is_file():
        raise OSError(f"ไฟล์สำรองกราฟิกใช้ไม่ได้: {backup_path}")
    if not backup_path.exists():
        _atomic_write_text(backup_path, original)

    fflags.update(LOW_GRAPHICS_OVERRIDES)
    if refresh_rate is not None:
        fflags["DFIntTaskSchedulerTargetFps"] = refresh_rate
    settings["fflags"] = fflags
    settings["graphics_optimization_mode"] = "performance"
    newline = "\r\n" if "\r\n" in original else "\n"
    trailing_newline = newline if original.endswith(("\n", "\r")) else ""
    serialized = json.dumps(settings, ensure_ascii=False, indent=2).replace("\n", newline)
    updated = original[:json_start] + serialized + trailing_newline
    _atomic_write_text(config_path, updated)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    import gamescope_manager as gm
except Exception:  # pragma: no cover
    gm = None

import auto_fishing
from auto_fishing import (
    APP_VERSION,
    DEFAULT_SETTINGS,
    MODE_CONFIGS,
    TEMPLATE_FILE,
    BaseFishingApp,
    ScreenCapture,
    register_stop,
    save_settings,
    set_dpi_awareness,
)


class LinuxFishingApp(BaseFishingApp):
    """Linux-specific Auto Fishing GUI supporting Desktop X11 and Gamescope dual-monitor."""

    def __init__(self, root: tk.Tk):
        self.active_tab = "linux"
        self.gamescope_worker_proc = None
        self.gamescope_pipe = None
        self.gamescope_last_ping = 0.0
        self.gamescope_session = None
        self.spawned_gamescope_proc = None
        self.gamescope_display = None
        self.gamescope_launch_pending = False
        self.gamescope_status_job = None
        self.gamescope_launch_error = None
        super().__init__(root)
        self.root.title(f"Roblox Auto Fishing {APP_VERSION} (Linux)")
        self.root.geometry("680x880")
        self.root.minsize(580, 760)
        self._restore_mode_preview()

    def _init_vars(self) -> None:
        super()._init_vars()
        lin_cfg = self.settings.get("linux", {}) if isinstance(self.settings, dict) else {}
        env = lin_cfg.get("env_mode", self.settings.get("env_mode", "desktop"))
        if env not in ("desktop", "gamescope"):
            env = "desktop"
        self.env_mode = env

        gs_fps_val = str(lin_cfg.get("gamescope_fps", self.settings.get("gamescope_fps", DEFAULT_SETTINGS.get("gamescope_fps", 60))))
        gs_res_val = str(lin_cfg.get("gamescope_res", self.settings.get("gamescope_res", DEFAULT_SETTINGS.get("gamescope_res", "1280x720"))))
        gs_fs_val = bool(lin_cfg.get("gamescope_fullscreen", self.settings.get("gamescope_fullscreen", DEFAULT_SETTINGS.get("gamescope_fullscreen", False))))

        self.vars["env_mode"] = self.tk.StringVar(value=env)
        self.vars["gamescope_fps"] = self.tk.StringVar(value=gs_fps_val)
        self.vars["gamescope_res"] = self.tk.StringVar(value=gs_res_val)
        self.vars["gamescope_fullscreen"] = self.tk.BooleanVar(value=gs_fs_val)

    def _build_mode_panel(self, outer: tk.Frame) -> None:
        """Render tab bar allowing seamless switching between Linux UI and Windows Preview UI."""
        BG_DARK = "#121118"
        BG_CARD = "#1e1b29"
        BG_CARD_LIGHT = "#272336"
        BORDER_COLOR = "#322c44"
        TEXT_MAIN = "#f3f0fb"
        TEXT_MUTED = "#9d96b0"
        PURPLE_PRIMARY = "#7c3aed"
        PURPLE_HOVER = "#6d28d9"
        GREEN_READY = "#34d399"
        AMBER_WARN = "#fbbf24"

        # Tab Navigation Frame
        self.tab_nav_frame = self.tk.Frame(outer, bg=BG_DARK)
        self.tab_nav_frame.pack(fill="x", pady=(0, 6))

        tab_pill_box = self.tk.Frame(
            self.tab_nav_frame,
            bg="#161420",
            padx=3,
            pady=3,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
        )
        tab_pill_box.pack(anchor="w")

        self.tab_linux_btn = self.tk.Button(
            tab_pill_box,
            text="🐧  โหมด Linux (X11 / Gamescope)",
            bg=PURPLE_PRIMARY,
            fg="#ffffff",
            activebackground=PURPLE_HOVER,
            activeforeground="#ffffff",
            font=(self.ui_font, 9, "bold"),
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
            command=lambda: self._switch_os_tab("linux"),
        )
        self.tab_linux_btn.pack(side="left", padx=(0, 2))

        self.tab_win_btn = self.tk.Button(
            tab_pill_box,
            text="🪟  หน้าจอ Windows (ดูตัวอย่าง UI)",
            bg="#161420",
            fg=TEXT_MUTED,
            activebackground=BG_CARD_LIGHT,
            activeforeground=TEXT_MAIN,
            font=(self.ui_font, 9, "bold"),
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
            command=lambda: self._switch_os_tab("windows"),
        )
        self.tab_win_btn.pack(side="left")

        # --- Container 1: Linux Panel ---
        self.linux_panel_container = self.tk.Frame(outer, bg=BG_DARK)
        self.linux_panel_container.pack(fill="x", pady=(0, 8))

        mode_card = self.tk.Frame(
            self.linux_panel_container,
            bg=BG_CARD,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
        )
        mode_card.pack(fill="x")

        m_hdr = self.tk.Frame(mode_card, bg=BG_CARD)
        m_hdr.pack(fill="x", pady=(0, 6))
        self.tk.Label(
            m_hdr,
            text="โหมดหน้าจอ / การควบคุม (Linux)",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 11, "bold"),
        ).pack(side="left")

        self.gamescope_status_badge = self.tk.Label(
            m_hdr,
            text="ตรวจสถานะ Gamescope...",
            fg=TEXT_MUTED,
            bg=BG_CARD_LIGHT,
            padx=8,
            pady=2,
            font=(self.ui_font, 8, "bold"),
        )
        self.gamescope_status_badge.pack(side="right")

        m_row = self.tk.Frame(mode_card, bg=BG_CARD)
        m_row.pack(fill="x")
        self.mode_desktop_rb = self.ttk.Radiobutton(
            m_row,
            text="🖥️  จอเดี่ยว (Desktop X11)",
            variable=self.vars["env_mode"],
            value="desktop",
            command=self._on_env_mode_change,
        )
        self.mode_desktop_rb.pack(side="left", padx=(0, 16))

        self.mode_gamescope_rb = self.ttk.Radiobutton(
            m_row,
            text="🎮  จอแยก 2 จอ (Gamescope + Sober)",
            variable=self.vars["env_mode"],
            value="gamescope",
            command=self._on_env_mode_change,
        )
        self.mode_gamescope_rb.pack(side="left")

        gs_actions = self.tk.Frame(mode_card, bg=BG_CARD)
        gs_actions.pack(fill="x", pady=(6, 0))

        fps_frame = self.tk.Frame(gs_actions, bg=BG_CARD)
        fps_frame.pack(side="left")
        self.tk.Label(
            fps_frame,
            text="⚡ ขนาด:",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 9),
        ).pack(side="left", padx=(0, 2))
        self.gs_res_cb = self.ttk.Combobox(
            fps_frame,
            textvariable=self.vars["gamescope_res"],
            values=("1280x720", "1600x900", "1920x1080", "960x540"),
            width=8,
        )
        self.gs_res_cb.pack(side="left", padx=(0, 6))

        self.tk.Label(
            fps_frame,
            text="FPS:",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 9),
        ).pack(side="left", padx=(0, 2))
        self.gs_fps_cb = self.ttk.Combobox(
            fps_frame,
            textvariable=self.vars["gamescope_fps"],
            values=("60", "90", "120", "144", "165", "240"),
            width=4,
        )
        self.gs_fps_cb.pack(side="left", padx=(0, 6))

        self.gs_fs_cb = self.ttk.Checkbutton(
            fps_frame, text="เต็มจอ", variable=self.vars["gamescope_fullscreen"]
        )
        self.gs_fs_cb.pack(side="left")

        self.btn_refresh_gs = self.ttk.Button(
            gs_actions,
            text="🔄 ตรวจ",
            style="Secondary.TButton",
            command=self._refresh_gamescope_status,
        )
        self.btn_refresh_gs.pack(side="right")

        self.btn_launch_gs = self.ttk.Button(
            gs_actions,
            text="▶ เปิด Sober ใน Gamescope",
            style="Secondary.TButton",
            command=self._launch_gamescope,
        )
        self.btn_launch_gs.pack(side="right", padx=(0, 4))

        # --- Container 2: Windows Panel (Preview) ---
        self.windows_panel_container = self.tk.Frame(outer, bg=BG_DARK)

        win_card = self.tk.Frame(
            self.windows_panel_container,
            bg=BG_CARD,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
        )
        win_card.pack(fill="x")

        w_hdr = self.tk.Frame(win_card, bg=BG_CARD)
        w_hdr.pack(fill="x", pady=(0, 4))
        self.tk.Label(
            w_hdr,
            text="โหมดการทำงาน (Windows Desktop)",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 11, "bold"),
        ).pack(side="left")

        win_badge = self.tk.Label(
            w_hdr,
            text="● โหมดปกติ (จอเดียว)",
            fg=GREEN_READY,
            bg="#064e3b",
            padx=8,
            pady=2,
            font=(self.ui_font, 8, "bold"),
        )
        win_badge.pack(side="right")

        win_desc = (
            "📌 ข้อกำหนดโหมดปกติ: ต้องเปิดหน้าต่าง Roblox ค้างไว้ด้านหน้าตลอดเวลาที่ทำงาน "
            "โดยบอทจะควบคุมเมาส์ร่วมกับผู้ใช้ (กรุณาไม่ขยับเมาส์ขณะกำลังตกปลา)"
        )
        self.tk.Label(
            win_card,
            text=win_desc,
            fg=AMBER_WARN,
            bg=BG_CARD,
            font=(self.ui_font, 9),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(2, 4))

        win_sim_note = (
            "ℹ️ หน้านี้จำลองมุมมอง UI ของเวอร์ชัน Windows — ซ่อนตัวเลือก Gamescope/Sober "
            "และทำงานด้วยโหมด Desktop ร่วมกับเมาส์ตามมาตรฐาน Windows"
        )
        self.tk.Label(
            win_card,
            text=win_sim_note,
            fg=TEXT_MUTED,
            bg=BG_CARD,
            font=(self.ui_font, 8),
            wraplength=560,
            justify="left",
        ).pack(anchor="w")

    def _build_advanced_tuning(self, tuning: tk.Frame) -> None:
        """Add Gamescope FPS to advanced tuning options."""
        BG_CARD = "#1e1b29"
        TEXT_MAIN = "#f3f0fb"
        self.gs_fps_label = self.tk.Label(
            tuning,
            text="Gamescope FPS (Hz)",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 10),
        )
        self.gs_fps_label.grid(row=5, column=0, sticky="w", pady=3)
        self.gs_fps_cb_tuning = self.ttk.Combobox(
            tuning,
            textvariable=self.vars["gamescope_fps"],
            values=("60", "90", "120", "144", "165", "240"),
            width=7,
        )
        self.gs_fps_cb_tuning.grid(row=5, column=1, sticky="w", padx=8, pady=3)

    def _build_advanced_extra(self, advanced_frame: tk.Frame) -> None:
        """Add research notice for Windows dual-screen feasibility (shown when Windows tab is active)."""
        self.win_exp_frame = self.tk.Frame(
            advanced_frame,
            bg="#171520",
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground="#322c44",
        )
        self.tk.Label(
            self.win_exp_frame,
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
            self.win_exp_frame,
            text=exp_text,
            fg="#9d96b0",
            bg="#171520",
            font=(self.ui_font, 8),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))
        # Keep hidden initially because Linux tab is active
        self.win_exp_frame.pack_forget()

    def _switch_os_tab(self, target_os: str) -> None:
        """Toggle between Linux mode view and Windows UI preview."""
        if self.running or getattr(self, "start_pending", False):
            self._set_status("⚠️ กรุณาหยุดการทำงานของบอทก่อนสลับหน้าจอ")
            return
        if target_os == self.active_tab:
            return

        BG_CARD_LIGHT = "#272336"
        TEXT_MAIN = "#f3f0fb"
        TEXT_MUTED = "#9d96b0"
        PURPLE_PRIMARY = "#7c3aed"
        PURPLE_HOVER = "#6d28d9"

        self.active_tab = target_os
        if target_os == "windows":
            self.tab_linux_btn.configure(
                bg="#161420",
                fg=TEXT_MUTED,
                activebackground=BG_CARD_LIGHT,
                activeforeground=TEXT_MAIN,
            )
            self.tab_win_btn.configure(
                bg=PURPLE_PRIMARY,
                fg="#ffffff",
                activebackground=PURPLE_HOVER,
                activeforeground="#ffffff",
            )
            self.linux_panel_container.pack_forget()
            self.windows_panel_container.pack(fill="x", pady=(0, 8), after=self.tab_nav_frame)
            self.root.title(f"Roblox Auto Fishing {APP_VERSION} (Windows Preview)")
            if hasattr(self, "win_exp_frame"):
                self.win_exp_frame.pack(fill="x", pady=(8, 4))
            if hasattr(self, "gs_fps_label") and hasattr(self, "gs_fps_cb_tuning"):
                self.gs_fps_label.grid_remove()
                self.gs_fps_cb_tuning.grid_remove()
            self._update_readiness()
            self._restore_mode_preview()
            self._set_status("สลับเป็นหน้าจอ Windows (ดูตัวอย่าง UI โหมดจอเดี่ยว)")
        else:
            self.tab_linux_btn.configure(
                bg=PURPLE_PRIMARY,
                fg="#ffffff",
                activebackground=PURPLE_HOVER,
                activeforeground="#ffffff",
            )
            self.tab_win_btn.configure(
                bg="#161420",
                fg=TEXT_MUTED,
                activebackground=BG_CARD_LIGHT,
                activeforeground=TEXT_MAIN,
            )
            self.windows_panel_container.pack_forget()
            self.linux_panel_container.pack(fill="x", pady=(0, 8), after=self.tab_nav_frame)
            self.root.title(f"Roblox Auto Fishing {APP_VERSION} (Linux)")
            if hasattr(self, "win_exp_frame"):
                self.win_exp_frame.pack_forget()
            if hasattr(self, "gs_fps_label") and hasattr(self, "gs_fps_cb_tuning"):
                self.gs_fps_label.grid()
                self.gs_fps_cb_tuning.grid()
            self._update_readiness()
            self._restore_mode_preview()
            self._set_status("สลับกลับเป็นหน้าจอ Linux (โหมด X11 / Gamescope)")

    def _on_built(self) -> None:
        """Start polling Gamescope display state."""
        self.gamescope_status_job = self.root.after(0, self._poll_gamescope_status)

    def _on_env_mode_change(self) -> None:
        if self.running or getattr(self, "start_pending", False):
            self.vars["env_mode"].set(self.env_mode)
            return
        new_env = self.vars["env_mode"].get()
        if new_env in ("desktop", "gamescope"):
            self.env_mode = new_env
            self.settings["env_mode"] = new_env
            if "linux" not in self.settings:
                self.settings["linux"] = {}
            self.settings["linux"]["env_mode"] = new_env
            save_settings(self.settings)
            self._update_readiness()
            self._restore_mode_preview()

    def _refresh_gamescope_status(self) -> None:
        if not hasattr(self, "gamescope_status_badge"):
            return
        if gm is None:
            self.gamescope_status_badge.configure(
                text="⚪ ไม่พบ Gamescope", fg="#9d96b0", bg="#272336"
            )
            return
        if self.gamescope_display:
            session = gm.inspect_display(self.gamescope_display)
        else:
            session = gm.find_active_gamescope_session()
        self.gamescope_session = session
        if session and session.get("target_window_id"):
            disp = session.get("display", ":1")
            win_name = session.get("target_window_name") or "Sober / Roblox"
            self.gamescope_status_badge.configure(
                text=f"🟢 {disp} : {win_name[:24]}", fg="#34d399", bg="#064e3b"
            )
        elif session:
            disp = session.get("display", ":1")
            self.gamescope_status_badge.configure(
                text=f"🟡 {disp} : รอเข้าเกม", fg="#fbbf24", bg="#451a03"
            )
        else:
            self.gamescope_status_badge.configure(
                text="⚪ ไม่พบ Gamescope", fg="#9d96b0", bg="#272336"
            )
        self._update_readiness()

    def _poll_gamescope_status(self) -> None:
        self.gamescope_status_job = None
        if getattr(self, "_closing", False):
            return
        owned = self.spawned_gamescope_proc
        if owned is not None and owned.poll() is not None:
            code = owned.poll()
            detail = " ".join(gm.read_process_output(owned).split()) if gm else ""
            if gm:
                gm.stop_process_safely(owned)
            self.spawned_gamescope_proc = None
            self.gamescope_display = None
            self.gamescope_session = None
            self.gamescope_launch_error = f"Gamescope หยุดทำงาน (รหัส {code})" + (
                f": {detail[-4000:]}" if detail else ""
            )
            if hasattr(self, "gamescope_status_badge"):
                self.gamescope_status_badge.configure(
                    text="🔴 Gamescope หยุดทำงาน", fg="#fca5a5", bg="#450a0a"
                )
            self._set_status(f"Gamescope หยุดทำงาน: {self.gamescope_launch_error}")
            self._set_gamescope_launch_button("normal")
            self._update_readiness()
            return
        if self.gamescope_launch_error:
            if owned is not None and owned.poll() is None and gm:
                session = gm.find_active_gamescope_session()
                if session and session.get("display"):
                    self.gamescope_display = session["display"]
                    self.gamescope_session = session
                    self.gamescope_launch_error = None
                    self._set_status(f"เปิด Sober แล้ว ({self.gamescope_display}) — กำลังตรวจหน้าต่างเกม")
                    self._refresh_gamescope_status()
                    self.gamescope_status_job = self.root.after(2000, self._poll_gamescope_status)
                    return
            self._update_readiness()
            self.gamescope_status_job = self.root.after(2000, self._poll_gamescope_status)
            return
        self._refresh_gamescope_status()
        self.gamescope_status_job = self.root.after(2000, self._poll_gamescope_status)

    def _set_gamescope_launch_button(self, state: str = "normal") -> None:
        if not hasattr(self, "btn_launch_gs"):
            return
        text = "⏳ กำลังเปิด Gamescope..." if state == "disabled" else "▶ เปิด Sober ใน Gamescope"
        self.btn_launch_gs.configure(state=state, text=text)

    def _launch_gamescope(self) -> None:
        if getattr(self, "running", False):
            self._set_status("หยุดการทำงานก่อนเปิด Gamescope เพิ่ม")
            return
        if self.gamescope_launch_pending:
            self._set_status("กำลังเปิด Gamescope อยู่ กรุณารอสักครู่")
            return
        owned = self.spawned_gamescope_proc
        if owned is not None and owned.poll() is None:
            self._set_status("Gamescope ที่เปิดจากโปรแกรมกำลังทำงานอยู่")
            self._refresh_gamescope_status()
            return

        # Check if an existing Sober process is already running
        if gm and gm.is_sober_running():
            self._set_status("พบ Sober กำลังทำงานอยู่แล้ว — กรุณาปิดตัวเดิมก่อนเปิดใหม่")
            self._refresh_gamescope_status()
            return

        selected_fps = None
        if hasattr(self, "vars") and "gamescope_fps" in self.vars:
            try:
                candidate = int(self.vars["gamescope_fps"].get())
                if 30 <= candidate <= 360:
                    selected_fps = candidate
            except (TypeError, ValueError):
                pass

        try:
            configure_sober_low_graphics(refresh_rate=selected_fps)
        except (OSError, ValueError) as exc:
            self.gamescope_launch_error = str(exc)
            self._set_status(f"เปิด Sober ไม่ได้: ตั้งค่ากราฟิกต่ำไม่สำเร็จ — {exc}")
            if hasattr(self, "_update_readiness"):
                self._update_readiness()
            return

        self.gamescope_launch_pending = True
        self.gamescope_launch_error = None

        if "linux" not in self.settings or not isinstance(self.settings["linux"], dict):
            self.settings["linux"] = {}

        if hasattr(self, "vars"):
            if "gamescope_fps" in self.vars:
                try:
                    fps_val = int(self.vars["gamescope_fps"].get())
                    self.settings["gamescope_fps"] = fps_val
                    self.settings["linux"]["gamescope_fps"] = fps_val
                except Exception:
                    pass
            if "gamescope_res" in self.vars:
                res_val = str(self.vars["gamescope_res"].get())
                self.settings["gamescope_res"] = res_val
                self.settings["linux"]["gamescope_res"] = res_val
            if "gamescope_fullscreen" in self.vars:
                fs_val = bool(self.vars["gamescope_fullscreen"].get())
                self.settings["gamescope_fullscreen"] = fs_val
                self.settings["linux"]["gamescope_fullscreen"] = fs_val
            save_settings(self.settings)

        self._set_gamescope_launch_button("disabled")
        if hasattr(self, "gamescope_status_badge"):
            self.gamescope_status_badge.configure(
                text="🟡 กำลังเปิด Gamescope...", fg="#fbbf24", bg="#451a03"
            )
        self._set_status("กำลังเปิด Sober ใน Gamescope — กรุณารอการตรวจพบจอแยก")
        threading.Thread(
            target=self._launch_gamescope_worker, name="gamescope-launch", daemon=True
        ).start()

    def _launch_gamescope_worker(self) -> None:
        proc = None
        try:
            fps_val = 60
            if hasattr(self, "vars") and "gamescope_fps" in self.vars:
                try:
                    fps_val = int(self.vars["gamescope_fps"].get())
                except (ValueError, TypeError):
                    fps_val = 60
            elif isinstance(self.settings, dict):
                try:
                    fps_val = int(self.settings.get("gamescope_fps", 60))
                except (ValueError, TypeError):
                    fps_val = 60

            res_str = "1280x720"
            if hasattr(self, "vars") and "gamescope_res" in self.vars:
                res_str = str(self.vars["gamescope_res"].get())
            elif isinstance(self.settings, dict):
                res_str = str(self.settings.get("gamescope_res", "1280x720"))

            w_val, h_val = 1280, 720
            if "x" in res_str:
                try:
                    w_s, h_s = res_str.split("x", 1)
                    w_val, h_val = int(w_s), int(h_s)
                except Exception:
                    w_val, h_val = 1280, 720

            fs_val = False
            if hasattr(self, "vars") and "gamescope_fullscreen" in self.vars:
                fs_val = bool(self.vars["gamescope_fullscreen"].get())
            elif isinstance(self.settings, dict):
                fs_val = bool(self.settings.get("gamescope_fullscreen", False))

            if gm is None:
                raise RuntimeError("ไม่พบโมดูล gamescope_manager บนระบบ")

            prefer_vk = None
            if isinstance(self.settings, dict):
                override = self.settings.get("gamescope_vk_device_override")
                if not override and isinstance(self.settings.get("linux"), dict):
                    override = self.settings["linux"].get("gamescope_vk_device_override")
                if override:
                    prefer_vk = str(override).strip() or None

            proc, discovered = gm.launch_sober_in_gamescope(
                width=w_val,
                height=h_val,
                fullscreen=fs_val,
                refresh_rate=fps_val,
                prefer_vk_device=prefer_vk,
                timeout=30.0,
            )
            if not discovered:
                return_code = proc.poll() if proc is not None else None
                if return_code is not None:
                    out = gm.read_process_output(proc).strip() if gm and proc else ""
                    tail = f" — ข้อความท้าย: {out[-500:]}" if out else ""
                    error = f"Gamescope จบการทำงานก่อนพบ display (exit code {return_code}){tail}"
                elif proc is not None:
                    out = gm.read_process_output(proc).strip() if gm and proc else ""
                    tail = f" — ข้อความวินิจฉัย: {out[-500:]}" if out else ""
                    error = f"ไม่พบ display Gamescope ภายใน 30 วินาที แต่ process ยังทำงานอยู่ (PID {proc.pid}){tail}"
                else:
                    error = "ไม่พบ display Gamescope ภายใน 30 วินาที"
            else:
                error = None
        except Exception as exc:
            discovered = None
            error = str(exc)

        if getattr(self, "_closing", False):
            if proc is not None and gm:
                gm.stop_process_safely(proc)
            return
        try:
            self.root.after(0, self._finish_gamescope_launch, proc, discovered, error)
        except Exception:
            if proc is not None and gm:
                gm.stop_process_safely(proc)

    def _finish_gamescope_launch(
        self, proc: Any, discovered: str | None, error: str | None
    ) -> None:
        if getattr(self, "_closing", False):
            if proc is not None and gm:
                gm.stop_process_safely(proc)
            return
        self.gamescope_launch_pending = False
        self._set_gamescope_launch_button("normal")
        if error:
            proc_running = bool(proc is not None and proc.poll() is None)
            if not proc_running:
                if proc is not None and gm:
                    gm.stop_process_safely(proc)
                self.spawned_gamescope_proc = None
            else:
                self.spawned_gamescope_proc = proc
            self.gamescope_display = None
            self.gamescope_session = None
            self.gamescope_launch_error = error
            if hasattr(self, "gamescope_status_badge"):
                self.gamescope_status_badge.configure(
                    text="🔴 เปิด Gamescope ไม่สำเร็จ", fg="#fca5a5", bg="#450a0a"
                )
            self._set_status(f"เริ่ม Gamescope ไม่ได้: {error}")
            self._update_readiness()
            return
        self.spawned_gamescope_proc = proc
        self.gamescope_display = discovered
        self.gamescope_launch_error = None
        self._set_status(f"เปิด Sober แล้ว ({discovered}) — กำลังตรวจหน้าต่างเกม")
        self._refresh_gamescope_status()

    def _grab_gamescope_rect(self, x: int, y: int, w: int, h: int) -> Any:
        if gm is None:
            return None
        session = self.gamescope_session or gm.find_active_gamescope_session()
        if not session or not session.get("target_window_id"):
            return None
        disp_str = session["display"]
        win_id = session["target_window_id"]
        try:
            from Xlib import X, display
            d = display.Display(disp_str)
            win = d.create_resource_object("window", win_id)
            raw = win.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
            d.close()
            if not raw or not raw.data or np is None:
                return None
            arr = np.frombuffer(raw.data, dtype=np.uint8).reshape((h, w, 4))
            return arr[:, :, :3].copy()
        except Exception:
            return None

    def _capture_gamescope_window(self) -> Any:
        if gm is None:
            raise RuntimeError("ไม่พบโมดูล gamescope_manager บนระบบ")
        session = gm.find_active_gamescope_session()
        if not session or not session.get("target_window_id"):
            raise RuntimeError("ไม่พบหน้าต่างเกมใน Gamescope กรุณาเปิดเกมด้วย run_gamescope.sh ก่อน")
        disp_str = session["display"]
        win_id = session["target_window_id"]
        from Xlib import X, display
        d = display.Display(disp_str)
        win = d.create_resource_object("window", win_id)
        geom = win.get_geometry()
        raw = win.get_image(0, 0, geom.width, geom.height, X.ZPixmap, 0xFFFFFFFF)
        d.close()
        if not raw or not raw.data or np is None:
            raise RuntimeError("จับภาพหน้าต่างเกมใน Gamescope ไม่สำเร็จ")
        arr = np.frombuffer(raw.data, dtype=np.uint8).reshape((geom.height, geom.width, 4))
        return arr[:, :, :3].copy()

    def _restore_mode_preview(self) -> None:
        if getattr(self, "active_tab", "linux") == "windows":
            win_rois = self.settings.get("windows", {}).get("rois", {}) if isinstance(self.settings, dict) else {}
            bar_roi = win_rois.get("bar") or self.settings.get("rois", {}).get("bar")
            if bar_roi:
                try:
                    cap = ScreenCapture()
                    crop = cap.grab(bar_roi)
                    cap.close()
                    self._update_preview_display(crop)
                    return
                except Exception:
                    pass
            self._draw_preview_placeholder()
            return

        if self.env_mode == "gamescope":
            bar_roi = self.settings.get("gamescope_rois", {}).get("bar")
            if bar_roi:
                try:
                    crop = self._grab_gamescope_rect(*bar_roi)
                    if crop is not None:
                        self._update_preview_display(crop)
                        return
                except Exception:
                    pass
            self._draw_preview_placeholder()
        else:
            bar_roi = self.settings.get("rois", {}).get("bar")
            if bar_roi:
                try:
                    cap = ScreenCapture()
                    crop = cap.grab(bar_roi)
                    cap.close()
                    self._update_preview_display(crop)
                    return
                except Exception:
                    pass
            self._draw_preview_placeholder()

    def _set_mode_widgets_state(self, state: str) -> None:
        super()._set_mode_widgets_state(state)
        for tab_btn in ("tab_linux_btn", "tab_win_btn"):
            if hasattr(self, tab_btn):
                try:
                    getattr(self, tab_btn).configure(state=state)
                except Exception:
                    pass
        if hasattr(self, "mode_desktop_rb") and hasattr(self, "mode_gamescope_rb"):
            try:
                self.mode_desktop_rb.configure(state=state)
                self.mode_gamescope_rb.configure(state=state)
            except Exception:
                pass
        if hasattr(self, "btn_launch_gs"):
            self.btn_launch_gs.configure(
                state="disabled" if state == "disabled" or self.gamescope_launch_pending else "normal"
            )
        for w in ("gs_fps_cb", "gs_res_cb", "gs_fs_cb"):
            if hasattr(self, w):
                try:
                    getattr(self, w).configure(state=state)
                except Exception:
                    pass

    def _update_readiness(self) -> None:
        if not hasattr(self, "target_summary"):
            return

        if getattr(self, "active_tab", "linux") == "windows":
            if hasattr(self, "btn_select_window"):
                self.btn_select_window.pack(side="right")
            if hasattr(self, "btn_select_roi"):
                self.btn_select_roi.configure(text="🎯  เลือกพื้นที่บนหน้าจอ")
            win_rois = self.settings.get("windows", {}).get("rois", {}) if isinstance(self.settings, dict) else {}
            bar_roi = win_rois.get("bar") or self.settings.get("rois", {}).get("bar")
            if bar_roi:
                self.roi_summary.configure(text=f"แถบ Windows: {bar_roi[2]} × {bar_roi[3]} px")
                if hasattr(self, "roi_size_label"):
                    self.roi_size_label.configure(text=f"ขนาดพื้นที่: {bar_roi[2]} × {bar_roi[3]} px")
            else:
                self.roi_summary.configure(text="ยังไม่กำหนดแถบมินิเกม")
                if hasattr(self, "roi_size_label"):
                    self.roi_size_label.configure(text="ขนาดพื้นที่: ยังไม่ได้กำหนด")
            self.target_summary.configure(text="จอปกติ (แชร์เมาส์)")
            if hasattr(self, "main_action_btn") and not self.running and not getattr(self, "start_pending", False):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
            return

        if self.env_mode == "gamescope":
            if hasattr(self, "btn_select_window"):
                self.btn_select_window.pack_forget()
            session = self.gamescope_session or (gm.find_active_gamescope_session() if gm else None)
            if session and session.get("target_window_id"):
                disp = session.get("display", ":1")
                win_name = session.get("target_window_name") or "Sober / Roblox"
                self.target_summary.configure(text=f"Gamescope ({disp}): {win_name[:20]}")
            else:
                self.target_summary.configure(text="ยังไม่พบ Gamescope")

            rois = self.settings.get("gamescope_rois", {}) if isinstance(self.settings, dict) else {}
            bar_ready = bool(rois.get("bar"))
            if bar_ready:
                bar_roi = rois["bar"]
                self.roi_summary.configure(text=f"แถบ Gamescope: {bar_roi[2]} × {bar_roi[3]} px")
                if hasattr(self, "roi_size_label"):
                    self.roi_size_label.configure(text=f"ขนาดพื้นที่: {bar_roi[2]} × {bar_roi[3]} px")
                if hasattr(self, "btn_select_roi"):
                    self.btn_select_roi.configure(text="🎯  เลือกพื้นที่จากภาพเกม")
                if hasattr(self, "main_action_btn") and not self.running and not getattr(self, "start_pending", False):
                    self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
            else:
                self.roi_summary.configure(text="ยังไม่กำหนดแถบ Gamescope")
                if hasattr(self, "roi_size_label"):
                    self.roi_size_label.configure(text="ขนาดพื้นที่: ยังไม่ได้กำหนด")
                if hasattr(self, "btn_select_roi"):
                    self.btn_select_roi.configure(text="🎯  เลือกพื้นที่จากภาพเกม")
                if hasattr(self, "main_action_btn") and not self.running and not getattr(self, "start_pending", False):
                    self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
                    if hasattr(self, "action_guidance_lbl") and self.status_text_lbl.cget("text") != "ข้อผิดพลาด":
                        self.action_guidance_lbl.configure(
                            text="⚠️  กรุณาเลือกพื้นที่จากภาพเกมในขั้นตอนที่ 1 ก่อนเริ่ม",
                            fg="#f59e0b",
                        )
        else:
            if hasattr(self, "btn_select_roi"):
                self.btn_select_roi.configure(text="🎯  เลือกพื้นที่บนหน้าจอ")
            super()._update_readiness()

    def _read_ui(self) -> dict[str, Any]:
        result = super()._read_ui()
        fps_val = 60
        if hasattr(self, "vars") and "gamescope_fps" in self.vars:
            try:
                fps_val = int(self.vars["gamescope_fps"].get())
            except (ValueError, TypeError):
                fps_val = 60
        res_val = str(self.vars["gamescope_res"].get()) if "gamescope_res" in self.vars else "1280x720"
        fs_val = bool(self.vars["gamescope_fullscreen"].get()) if "gamescope_fullscreen" in self.vars else False
        result["gamescope_fps"] = fps_val
        result["gamescope_res"] = res_val
        result["gamescope_fullscreen"] = fs_val
        result["env_mode"] = self.env_mode
        if self.env_mode == "gamescope":
            result["rois"] = dict(self.settings.get("gamescope_rois", {}))

        override = None
        if isinstance(self.settings, dict):
            override = self.settings.get("gamescope_vk_device_override")
            if not override and isinstance(self.settings.get("linux"), dict):
                override = self.settings["linux"].get("gamescope_vk_device_override")
        if override:
            result["gamescope_vk_device_override"] = str(override).strip()
        return result

    def _capture_area(self, name: str) -> None:
        if getattr(self, "active_tab", "linux") == "windows":
            super()._capture_area(name)
            return

        if self.env_mode == "gamescope":
            try:
                image = self._capture_gamescope_window()
                bounds = (0, 0, image.shape[1], image.shape[0])
                self._open_roi_canvas(image, bounds, name)
            except Exception as exc:
                self.root.deiconify()
                self._set_status(f"จับภาพไม่สำเร็จ: {exc}")
        else:
            super()._capture_area(name)

    def _save_roi_selection(
        self, name: str, area: list[int], bounds: tuple[int, int, int, int], image: Any
    ) -> None:
        if getattr(self, "active_tab", "linux") == "windows":
            super()._save_roi_selection(name, area, bounds, image)
            if "windows" not in self.settings or not isinstance(self.settings["windows"], dict):
                self.settings["windows"] = {}
            if "rois" not in self.settings["windows"]:
                self.settings["windows"]["rois"] = {}
            if name != "template":
                self.settings["windows"]["rois"][name] = area
                save_settings(self.settings)
            self._update_readiness()
            return

        if self.env_mode == "gamescope":
            if name == "template":
                try:
                    crop = image[area[1] : area[1] + area[3], area[0] : area[0] + area[2]]
                    if auto_fishing.cv2 is not None:
                        TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        auto_fishing.cv2.imwrite(str(TEMPLATE_FILE), crop)
                except Exception:
                    pass
                self._set_status("บันทึกภาพตัวอย่างสัญญาณปลากินเบ็ดแล้ว")
            else:
                self.settings.setdefault("gamescope_rois", {})[name] = area
                if "linux" not in self.settings or not isinstance(self.settings["linux"], dict):
                    self.settings["linux"] = {}
                if "gamescope_rois" not in self.settings["linux"]:
                    self.settings["linux"]["gamescope_rois"] = {}
                self.settings["linux"]["gamescope_rois"][name] = area
                save_settings(self.settings)
                self._set_status("เลือกพื้นที่สำหรับ Gamescope แล้ว")
                if name == "bar":
                    try:
                        crop = image[area[1] : area[1] + area[3], area[0] : area[0] + area[2]]
                        self._update_preview_display(crop)
                    except Exception:
                        pass
            self._update_readiness()
        else:
            super()._save_roi_selection(name, area, bounds, image)
            if "linux" in self.settings and isinstance(self.settings["linux"], dict):
                if "desktop_rois" not in self.settings["linux"]:
                    self.settings["linux"]["desktop_rois"] = {}
                if name != "template":
                    self.settings["linux"]["desktop_rois"][name] = area
                    save_settings(self.settings)

    def start(self) -> None:
        if (
            self.running
            or getattr(self, "start_pending", False)
            or getattr(self, "test_hold_pending", False)
            or getattr(self, "test_hold_active", False)
            or getattr(self, "gamescope_launch_pending", False)
        ):
            return

        if getattr(self, "active_tab", "linux") == "windows":
            super().start()
            return

        if self.env_mode == "gamescope":
            self._start_gamescope()
            return

        super().start()

    def _start_gamescope(self) -> None:
        try:
            rois = self.settings.get("gamescope_rois", {})
            if not rois.get("bar"):
                raise RuntimeError("จำเป็นต้องกำหนดพื้นที่แถบมินิเกมสำหรับโหมด Gamescope")

            if gm is None:
                raise RuntimeError("ไม่พบโมดูล gamescope_manager บนระบบ")

            session = gm.find_active_gamescope_session()
            if not session or not session.get("target_window_id"):
                raise RuntimeError("ไม่พบเกมใน Gamescope กรุณาเปิดเกมด้วย run_gamescope.sh ก่อน")

            disp_str = session["display"]
            win_id = session["target_window_id"]

            from gamescope_worker import worker_process_main

            parent_pipe, child_pipe = multiprocessing.Pipe()
            self.gamescope_pipe = parent_pipe
            self.gamescope_worker_proc = multiprocessing.Process(
                target=worker_process_main,
                args=(child_pipe, disp_str, win_id),
                daemon=True,
            )
            self.gamescope_worker_proc.start()

            if not parent_pipe.poll(timeout=3.5):
                raise RuntimeError("Worker process timed out during initialization")
            init_msg = parent_pipe.recv()
            if init_msg[0] != "CONNECTED":
                raise RuntimeError(f"Worker failed to connect: {init_msg}")

            worker_settings = dict(self._read_ui())
            worker_settings["rois"] = dict(rois)
            worker_settings["template_file"] = str(TEMPLATE_FILE)
            parent_pipe.send(("START", worker_settings))

            self.running = True
            self.stop_requested.clear()
            if not getattr(self, "_global_hotkey_cleanup", None):
                self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self._set_mode_widgets_state("disabled")
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="■  หยุด Auto (F8)", style="Danger.TButton")

            mode = worker_settings.get("fishing_mode", "rod")
            mode_name = MODE_CONFIGS.get(mode, MODE_CONFIGS["rod"])["name"]
            self._set_status(
                f"กำลังทำงานในจอแยก (Gamescope) — โหมด{mode_name} — ใช้งานจอ 2 ได้ตามปกติ"
            )
            self._schedule_auto_restart()
            self.root.after(20, self._pump_gamescope)
        except Exception as exc:
            self.stop(f"เริ่มไม่ได้: {exc}")

    def _pump_gamescope(self) -> None:
        if not self.running or self.env_mode != "gamescope":
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return

        # 1. Heartbeat ping first to guarantee worker is never starved
        now = time.monotonic()
        if now - self.gamescope_last_ping > 1.0:
            self.gamescope_last_ping = now
            try:
                if self.gamescope_pipe:
                    self.gamescope_pipe.send(("PING",))
            except Exception:
                pass

        # 2. Process bounded batch of IPC messages and collapse PREVIEW frames
        packets_processed = 0
        latest_preview = None
        try:
            while self.gamescope_pipe and self.gamescope_pipe.poll() and packets_processed < 25:
                packets_processed += 1
                packet = self.gamescope_pipe.recv()
                msg_type = packet[0]
                if msg_type == "STATUS":
                    st = packet[1]
                    state = st.get("state", "Track")
                    if state == "Track" and getattr(self, "last_worker_state", None) != "Track":
                        self.last_worker_track_start = time.monotonic()
                    self.last_worker_state = state
                    reason = st.get("reason", "")
                    fps = st.get("fps", 0.0)
                    obs = st.get("observation_bar", "")
                    if "waiting for Gamescope" in reason or "frame capture" in reason or "target window" in reason:
                        self._set_status("รอภาพจาก Gamescope")
                    elif self.settings.get("debug") and st.get("telemetry"):
                        t = st["telemetry"]
                        self._set_status(
                            f"[ดีบัก-Gamescope] ช่อง:[{t.get('target_left', 0):.0f},{t.get('target_right', 0):.0f}] "
                            f"กลาง:{t.get('target_center', 0):.0f} • ตัวชี้:{t.get('marker_x', 0):.0f} "
                            f"v:{t.get('velocity', 0):+.0f}px/s • สั่ง:{t.get('action', '')}"
                        )
                    elif obs == "absent" and state in ("Track", "End"):
                        self._set_status("ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่")
                    else:
                        self._set_status(f"{state}: {reason or 'tracking'} | {fps:.1f} FPS")
                elif msg_type == "PREVIEW":
                    # Keep only newest preview frame to avoid UI rendering backlog
                    latest_preview = (packet[1], packet[2])
                elif msg_type == "STOPPED":
                    self.stop(packet[1])
                    return
                elif msg_type == "ERROR":
                    self.stop(f"ข้อผิดพลาด: {packet[1]}")
                    return

            # Render only the latest preview frame if UI window is visible (not iconic/minimized)
            if latest_preview is not None:
                is_iconic = False
                try:
                    is_iconic = (self.root.state() == "iconic")
                except Exception:
                    pass
                if not is_iconic:
                    self._update_preview_display(latest_preview[0], latest_preview[1])
        except Exception as exc:
            self.stop(f"IPC error: {exc}")
            return

        # 3. Check worker alive only after draining pipe
        if not self.gamescope_worker_proc or not self.gamescope_worker_proc.is_alive():
            self.stop("Worker process terminated unexpectedly")
            return

        self.root.after(20, self._pump_gamescope)

    def stop(self, reason: str | None = None) -> None:
        if getattr(self, "gamescope_worker_proc", None):
            try:
                if getattr(self, "gamescope_pipe", None):
                    self.gamescope_pipe.send(("TERMINATE",))
            except Exception:
                pass
            try:
                self.gamescope_worker_proc.join(timeout=0.8)
                if self.gamescope_worker_proc.is_alive():
                    self.gamescope_worker_proc.terminate()
            except Exception:
                pass
            self.gamescope_worker_proc = None
            self.gamescope_pipe = None
        self.last_worker_state = None

        super().stop(reason)

    def close(self) -> None:
        self._closing = True
        if getattr(self, "gamescope_status_job", None) is not None:
            try:
                self.root.after_cancel(self.gamescope_status_job)
            except Exception:
                pass
            self.gamescope_status_job = None
        if getattr(self, "gamescope_worker_proc", None) is not None:
            try:
                self.gamescope_worker_proc.terminate()
            except Exception:
                pass
            self.gamescope_worker_proc = None
        if getattr(self, "spawned_gamescope_proc", None) is not None and gm:
            try:
                gm.stop_process_safely(self.spawned_gamescope_proc)
            except Exception:
                pass
            self.spawned_gamescope_proc = None
        super().close()

    def _settings(self) -> dict[str, Any]:
        """Save settings under the 'linux' namespace."""
        settings = super()._settings()
        if "linux" not in self.settings or not isinstance(self.settings["linux"], dict):
            self.settings["linux"] = {}
        self.settings["linux"]["env_mode"] = self.env_mode
        if self.env_mode == "gamescope":
            gamescope_rois = self.settings.get("gamescope_rois", {})
            self.settings["linux"]["gamescope_rois"] = dict(gamescope_rois)
            self.settings["gamescope_rois"] = dict(gamescope_rois)
        else:
            self.settings["linux"]["desktop_rois"] = dict(settings.get("rois", {}))
        self.settings["env_mode"] = self.env_mode
        override = self.settings.get("gamescope_vk_device_override")
        if not override and isinstance(self.settings.get("linux"), dict):
            override = self.settings["linux"].get("gamescope_vk_device_override")
        if override:
            self.settings["gamescope_vk_device_override"] = str(override).strip()
            self.settings["linux"]["gamescope_vk_device_override"] = str(override).strip()
        save_settings(self.settings)
        return settings


if platform.system() == "Linux":
    auto_fishing.FishingApp = LinuxFishingApp


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Roblox Auto Fishing (Linux)")
    parser.add_argument("--replay", metavar="DIR", help="analyze saved frames without mouse input")
    args = parser.parse_args()
    if args.replay:
        for index, result in enumerate(auto_fishing.replay_frames(args.replay), 1):
            print(f"{index}: state=vision-{result[0]} marker={result[1]} target={result[2]}")
        return
    set_dpi_awareness()
    root = tk.Tk()
    app = LinuxFishingApp(root)
    root.mainloop()


if __name__ == "__main__":
    auto_fishing._bootstrap()
    main()
