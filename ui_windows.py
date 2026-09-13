"""Roblox Auto Fishing - Windows UI.

Supports:
1. Normal Desktop mode (single monitor, Roblox foreground, shared mouse).
2. Dual-screen mode (experimental, Roblox visible on monitor 2, background PostMessage control).
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import platform
import sys
import threading
import time
try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover
    tk = None
    ttk = None
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import auto_fishing
from auto_fishing import (
    APP_VERSION,
    BaseFishingApp,
    ScreenCapture,
    detect_bar,
    detect_bite,
    register_stop,
    save_settings,
    set_dpi_awareness,
)

# Win32 Constants
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


def get_user32() -> Any:
    try:
        return ctypes.windll.user32
    except (AttributeError, OSError):
        return None


def init_win32_api(user32: Any) -> None:
    if not user32:
        return
    try:
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND

        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        user32.GetClientRect.restype = wintypes.BOOL

        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL

        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL

        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL

        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL

        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL

        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int

        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
    except Exception:
        pass


def find_roblox_windows() -> list[dict[str, Any]]:
    """Scan top-level windows for Roblox client."""
    user32 = get_user32()
    if not user32:
        return []
    init_win32_api(user32)

    found = []

    def enum_proc(hwnd: int, lparam: int) -> bool:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        title_buf = ctypes.create_unicode_buffer(512)
        class_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        user32.GetClassNameW(hwnd, class_buf, 512)
        title = title_buf.value
        cls_name = class_buf.value

        if "roblox" in title.lower() or cls_name == "WINDOWSCLIENT":
            rect = RECT()
            pt = POINT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            found.append({
                "hwnd": hwnd,
                "title": title,
                "class_name": cls_name,
                "client_x": pt.x,
                "client_y": pt.y,
                "width": w,
                "height": h,
                "is_iconic": bool(user32.IsIconic(hwnd)),
            })
        return True

    ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(ENUM_PROC(enum_proc), 0)
    return found


def make_lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def send_background_click(hwnd: int, x: int, y: int, hold_seconds: float = 0.08) -> bool:
    user32 = get_user32()
    if not user32 or not user32.IsWindow(hwnd):
        return False
    init_win32_api(user32)
    lparam = make_lparam(x, y)
    ret_down = user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    if not ret_down:
        return False
    if hold_seconds > 0:
        time.sleep(hold_seconds)
    ret_up = user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    return bool(ret_up)


def send_background_hold(hwnd: int, x: int, y: int) -> bool:
    user32 = get_user32()
    if not user32 or not user32.IsWindow(hwnd):
        return False
    init_win32_api(user32)
    lparam = make_lparam(x, y)
    return bool(user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam))


def send_background_release(hwnd: int, x: int, y: int) -> bool:
    user32 = get_user32()
    if not user32 or not user32.IsWindow(hwnd):
        return False
    init_win32_api(user32)
    lparam = make_lparam(x, y)
    return bool(user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam))


def emergency_release_hwnd(hwnd: int | None) -> None:
    if hwnd:
        user32 = get_user32()
        if user32:
            try:
                user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, 0)
            except Exception:
                pass


class WindowsFishingApp(BaseFishingApp):
    """Windows-specific Auto Fishing GUI.

    Features:
    - Normal Desktop mode (single monitor, Roblox focused, shared mouse).
    - Experimental Dual-Screen mode (Roblox on monitor 2, background PostMessage control).
    - Verification and testing controls to prove Roblox input acceptance while unfocused.
    """

    def __init__(self, root: tk.Tk):
        self.target_hwnd: int | None = None
        self.target_client_size: tuple[int, int] | None = None
        self.target_title: str = ""
        self.mouse_held: bool = False
        self.last_action: str = "none"
        self._test_pulse_thread: threading.Thread | None = None
        super().__init__(root)
        self.root.title(f"Roblox Auto Fishing {APP_VERSION} (Windows)")
        self.root.geometry("680x880")
        self.root.minsize(580, 760)

    def _init_vars(self) -> None:
        super()._init_vars()
        win_cfg = self.settings.get("windows", {}) if isinstance(self.settings, dict) else {}
        env = win_cfg.get("env_mode", "desktop")
        if env not in ("desktop", "dual_screen"):
            env = "desktop"
        self.env_mode = env
        self.vars["env_mode"] = self.tk.StringVar(value=env)

    def _build_mode_panel(self, outer: tk.Frame) -> None:
        """Render mode selection: Normal Desktop vs Experimental Dual-Screen."""
        BG_DARK = "#121118"
        BG_CARD = "#1e1b29"
        BG_CARD_LIGHT = "#272336"
        BORDER_COLOR = "#322c44"
        TEXT_MAIN = "#f3f0fb"
        TEXT_MUTED = "#9d96b0"
        AMBER_WARN = "#fbbf24"
        GREEN_READY = "#34d399"
        PURPLE_LIGHT = "#a78bfa"

        # Mode Selection Card
        self.mode_card = self.tk.Frame(
            outer,
            bg=BG_CARD,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
        )
        self.mode_card.pack(fill="x", pady=(0, 8))

        m_hdr = self.tk.Frame(self.mode_card, bg=BG_CARD)
        m_hdr.pack(fill="x", pady=(0, 6))
        self.tk.Label(
            m_hdr,
            text="โหมดการทำงาน (Windows)",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 11, "bold"),
        ).pack(side="left")

        self.mode_status_badge = self.tk.Label(
            m_hdr,
            text="● โหมดปกติ (จอเดียว)" if self.env_mode == "desktop" else "⚠️ โหมดสองจอ (ทดลอง)",
            fg=GREEN_READY if self.env_mode == "desktop" else AMBER_WARN,
            bg="#064e3b" if self.env_mode == "desktop" else "#451a03",
            padx=8,
            pady=2,
            font=(self.ui_font, 8, "bold"),
        )
        self.mode_status_badge.pack(side="right")

        m_row = self.tk.Frame(self.mode_card, bg=BG_CARD)
        m_row.pack(fill="x", pady=(0, 6))
        self.mode_desktop_rb = self.ttk.Radiobutton(
            m_row,
            text="🖥️  โหมดปกติ (จอเดียว - แชร์เมาส์และโฟกัส)",
            variable=self.vars["env_mode"],
            value="desktop",
            command=self._on_env_mode_change,
        )
        self.mode_desktop_rb.pack(side="left", padx=(0, 16))

        self.mode_dual_rb = self.ttk.Radiobutton(
            m_row,
            text="🧪  ทดสอบโหมดสองจอ (ไม่แย่งเมาส์ / จอ 2)",
            variable=self.vars["env_mode"],
            value="dual_screen",
            command=self._on_env_mode_change,
        )
        self.mode_dual_rb.pack(side="left")

        # --- Sub-panel 1: Normal Desktop Mode Notice ---
        self.panel_desktop = self.tk.Frame(self.mode_card, bg=BG_CARD)
        desc_desktop = (
            "📌 ข้อกำหนดโหมดปกติ: ต้องเปิดหน้าต่าง Roblox ค้างไว้ด้านหน้าตลอดเวลาที่ทำงาน "
            "โดยบอทจะควบคุมเมาส์ร่วมกับผู้ใช้ (กรุณาไม่ขยับเมาส์ขณะกำลังตกปลา)"
        )
        self.tk.Label(
            self.panel_desktop,
            text=desc_desktop,
            fg=AMBER_WARN,
            bg=BG_CARD,
            font=(self.ui_font, 9),
            wraplength=580,
            justify="left",
        ).pack(anchor="w")

        # --- Sub-panel 2: Dual-Screen Experimental Controls ---
        self.panel_dual = self.tk.Frame(self.mode_card, bg=BG_CARD)

        row_win = self.tk.Frame(self.panel_dual, bg=BG_CARD)
        row_win.pack(fill="x", pady=(2, 6))

        self.lbl_target_window = self.tk.Label(
            row_win,
            text="หน้าต่างเกม: ยังไม่ได้ตรวจหา",
            fg=TEXT_MAIN,
            bg=BG_CARD,
            font=(self.ui_font, 9, "bold"),
        )
        self.lbl_target_window.pack(side="left")

        self.btn_detect_roblox = self.ttk.Button(
            row_win,
            text="🔍 ตรวจหาหน้าต่าง Roblox (จอ 2)",
            style="Secondary.TButton",
            command=self._find_and_select_roblox_window,
        )
        self.btn_detect_roblox.pack(side="right")

        row_test = self.tk.Frame(self.panel_dual, bg=BG_CARD)
        row_test.pack(fill="x", pady=(2, 6))

        self.btn_test_input = self.ttk.Button(
            row_test,
            text="⚡ ทดสอบส่ง Input เบื้องหลัง (3 ครั้ง)",
            style="Secondary.TButton",
            command=self._test_background_input,
        )
        self.btn_test_input.pack(side="left", padx=(0, 8))

        self.lbl_input_test_result = self.tk.Label(
            row_test,
            text="สถานะอินพุต: ยังไม่ได้ทดสอบ",
            fg=TEXT_MUTED,
            bg=BG_CARD,
            font=(self.ui_font, 9),
        )
        self.lbl_input_test_result.pack(side="left")

        desc_dual = (
            "📌 ข้อกำหนดโหมดสองจอ (ทดลอง): ส่งการคลิกตรงเข้าหน้าต่าง Roblox ด้วย PostMessage "
            "โดยไม่ขยับเมาส์จริงและไม่แย่งโฟกัสจากจอแรก — โปรดกด 'ทดสอบส่ง Input' "
            "ขณะใช้เบราว์เซอร์บนจอแรก เพื่อสังเกตว่าเกมตอบสนองจริงหรือไม่ก่อนเริ่มบอท"
        )
        self.tk.Label(
            self.panel_dual,
            text=desc_dual,
            fg="#9d96b0",
            bg=BG_CARD,
            font=(self.ui_font, 8),
            wraplength=580,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        # Show active sub-panel based on initial env_mode
        if self.env_mode == "dual_screen":
            self.panel_dual.pack(fill="x", pady=(4, 0))
        else:
            self.panel_desktop.pack(fill="x", pady=(4, 0))

    def _build_advanced_extra(self, advanced_frame: tk.Frame) -> None:
        """Add research notice for future Windows dual-screen feasibility."""
        self.exp_frame = self.tk.Frame(
            advanced_frame,
            bg="#171520",
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground="#322c44",
        )
        self.exp_frame.pack(fill="x", pady=(8, 4))

        self.tk.Label(
            self.exp_frame,
            text="🧪 กำลังวิจัยโหมดสองจอ (Windows Dual-Screen Feasibility)",
            fg="#a78bfa",
            bg="#171520",
            font=(self.ui_font, 9, "bold"),
        ).pack(anchor="w")

        exp_text = (
            "• โหมดนี้ส่งคำสั่งกด-ปล่อยเมาส์ซ้ายตรงไปยัง HWND ของ Roblox โดยคำนวณพิกัด Client Area\n"
            "• ผู้ใช้สามารถทำงานหรือใช้งานเบราว์เซอร์บนจอแรกได้โดยไม่ถูกแย่งเคอร์เซอร์\n"
            "• สถานะปัจจุบัน: เตรียมระบบทดสอบและส่ง PostMessage พร้อมแล้ว — "
            "ยังต้องพิสูจน์การตอบสนองบน Roblox จริงเนื่องจากข้อจำกัดการรับอินพุตเบื้องหลังของเอนจินเกม"
        )
        self.tk.Label(
            self.exp_frame,
            text=exp_text,
            fg="#9d96b0",
            bg="#171520",
            font=(self.ui_font, 8),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

    def _on_env_mode_change(self) -> None:
        if self.running or getattr(self, "start_pending", False):
            self.vars["env_mode"].set(self.env_mode)
            return
        new_env = self.vars["env_mode"].get()
        if new_env in ("desktop", "dual_screen"):
            self.env_mode = new_env
            if "windows" not in self.settings or not isinstance(self.settings["windows"], dict):
                self.settings["windows"] = {}
            self.settings["windows"]["env_mode"] = new_env
            save_settings(self.settings)

            GREEN_READY = "#34d399"
            AMBER_WARN = "#fbbf24"
            if new_env == "dual_screen":
                self.panel_desktop.pack_forget()
                self.panel_dual.pack(fill="x", pady=(4, 0))
                self.mode_status_badge.configure(
                    text="⚠️ โหมดสองจอ (ทดลอง)", fg=AMBER_WARN, bg="#451a03"
                )
                self._set_status("สลับเป็นโหมดสองจอ (ทดลอง) — กรุณาตรวจหาหน้าต่าง Roblox บนจอ 2")
                self._find_and_select_roblox_window()
            else:
                self.panel_dual.pack_forget()
                self.panel_desktop.pack(fill="x", pady=(4, 0))
                self.mode_status_badge.configure(
                    text="● โหมดปกติ (จอเดียว)", fg=GREEN_READY, bg="#064e3b"
                )
                self._set_status("สลับกลับเป็นโหมดปกติ (จอเดียว — ต้องโฟกัสเกม)")
            self._update_readiness()

    def _find_and_select_roblox_window(self) -> None:
        """Locate Roblox client window on Windows."""
        windows = find_roblox_windows()
        if not windows:
            if hasattr(self, "lbl_target_window"):
                self.lbl_target_window.configure(
                    text="หน้าต่างเกม: 🔴 ตรวจไม่พบ Roblox (กรุณาเปิดเกมบนจอ 2)",
                    fg="#fca5a5",
                )
            self.target_hwnd = None
            self.target_client_size = None
            self.target_title = ""
            self._update_readiness()
            return

        target = windows[0]
        self.target_hwnd = target["hwnd"]
        self.target_client_size = (target["width"], target["height"])
        self.target_title = target["title"]

        if target["is_iconic"]:
            if hasattr(self, "lbl_target_window"):
                self.lbl_target_window.configure(
                    text=f"หน้าต่างเกม: 🟡 {target['title']} [HWND: {target['hwnd']}] [ถูกย่อลง Taskbar]",
                    fg="#fbbf24",
                )
        else:
            if hasattr(self, "lbl_target_window"):
                self.lbl_target_window.configure(
                    text=f"หน้าต่างเกม: 🟢 {target['title']} [HWND: {target['hwnd']}] ({target['width']}×{target['height']} px)",
                    fg="#34d399",
                )
        self._update_readiness()

    def _test_background_input(self) -> None:
        """Send a test pulse of PostMessage clicks to target window."""
        if not self.target_hwnd:
            self._find_and_select_roblox_window()
            if not self.target_hwnd:
                self._set_status("กรุณาเปิดเกม Roblox ก่อนทดสอบส่ง Input")
                return

        if self._test_pulse_thread and self._test_pulse_thread.is_alive():
            return

        def _worker():
            hwnd = self.target_hwnd
            w, h = self.target_client_size or (800, 600)
            cx, cy = w // 2, h // 2
            success_count = 0
            for i in range(3):
                if send_background_click(hwnd, cx, cy, hold_seconds=0.08):
                    success_count += 1
                time.sleep(0.15)

            def _update_ui():
                if success_count == 3:
                    if hasattr(self, "lbl_input_test_result"):
                        self.lbl_input_test_result.configure(
                            text="✅ ส่ง PostMessage สำเร็จ 3/3 — สังเกตว่าเกมบนจอ 2 ตอบสนองหรือไม่",
                            fg="#34d399",
                        )
                    self._set_status("ส่งคำสั่งทดสอบครบ 3 ครั้ง — ตรวจดูการขยับของคันเบ็ดในเกมบนจอ 2")
                else:
                    if hasattr(self, "lbl_input_test_result"):
                        self.lbl_input_test_result.configure(
                            text=f"⚠️ ส่งสำเร็จ {success_count}/3 ครั้ง",
                            fg="#fbbf24",
                        )

            try:
                self.root.after(0, _update_ui)
            except Exception:
                pass

        self._test_pulse_thread = threading.Thread(target=_worker, name="win32-test-input", daemon=True)
        self._test_pulse_thread.start()

    def _check_target_valid_windows(self) -> tuple[bool, str]:
        """Verify Roblox window still exists, is visible, and has not resized."""
        user32 = get_user32()
        if not user32 or not self.target_hwnd:
            return False, "ยังไม่ได้เลือกหน้าต่างเกม Roblox"
        init_win32_api(user32)

        if not user32.IsWindow(self.target_hwnd):
            return False, "หน้าต่างเกม Roblox ถูกปิดแล้ว"
        if user32.IsIconic(self.target_hwnd):
            return False, "หน้าต่างเกม Roblox ถูกย่อลง Taskbar (กรุณาเปิดค้างไว้บนจอ 2)"
        if not user32.IsWindowVisible(self.target_hwnd):
            return False, "หน้าต่างเกม Roblox ถูกซ่อนอยู่"

        rect = RECT()
        if user32.GetClientRect(self.target_hwnd, ctypes.pointer(rect)):
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if self.target_client_size and (abs(w - self.target_client_size[0]) > 20 or abs(h - self.target_client_size[1]) > 20):
                return False, f"ขนาดหน้าต่างเกมเปลี่ยนไป ({w}×{h} px) — กรุณาเลือกพื้นที่ใหม่"

        return True, "OK"

    def _apply_background_action(self, action: str) -> bool:
        """Inject mouse action directly into the target window using Win32 PostMessage."""
        if action in ("none", ""):
            return True
        if action == "release":
            if self.mouse_held and self.target_hwnd:
                w, h = self.target_client_size or (800, 600)
                send_background_release(self.target_hwnd, w // 2, h // 2)
            self.mouse_held = False
            return True

        valid, reason = self._check_target_valid_windows()
        if not valid:
            self.mouse_held = False
            emergency_release_hwnd(self.target_hwnd)
            return False

        w, h = self.target_client_size or (800, 600)
        cx, cy = w // 2, h // 2

        if action == "hold":
            if not self.mouse_held:
                ret = send_background_hold(self.target_hwnd, cx, cy)
                if ret:
                    self.mouse_held = True
                return ret
            return True

        if action == "click":
            return send_background_click(self.target_hwnd, cx, cy, hold_seconds=0.08)

        return False

    def _update_readiness(self) -> None:
        if not hasattr(self, "target_summary"):
            return

        if self.env_mode == "dual_screen":
            if hasattr(self, "btn_select_window"):
                self.btn_select_window.pack_forget()
            if hasattr(self, "btn_select_roi"):
                self.btn_select_roi.configure(text="🎯  เลือกพื้นที่บนหน้าจอ (จอ 2)")

            if self.target_hwnd:
                self.target_summary.configure(text=f"จอ 2: {self.target_title[:18] or 'Roblox'}")
            else:
                self.target_summary.configure(text="ยังไม่พบ Roblox บนจอ 2")

            rois = self.settings.get("windows", {}).get("rois", {}) if isinstance(self.settings, dict) else {}
            bar_roi = rois.get("bar") or self.settings.get("rois", {}).get("bar")
            if bar_roi:
                self.roi_summary.configure(text=f"แถบจอ 2: {bar_roi[2]} × {bar_roi[3]} px")
                if hasattr(self, "roi_size_label"):
                    self.roi_size_label.configure(text=f"ขนาดพื้นที่: {bar_roi[2]} × {bar_roi[3]} px")
            else:
                self.roi_summary.configure(text="ยังไม่กำหนดแถบมินิเกม")
                if hasattr(self, "roi_size_label"):
                    self.roi_size_label.configure(text="ขนาดพื้นที่: ยังไม่ได้กำหนด")

            if hasattr(self, "main_action_btn") and not self.running and not getattr(self, "start_pending", False):
                self.main_action_btn.configure(text="▶  เริ่ม Auto สองจอ (F8)", style="Primary.TButton")
        else:
            if hasattr(self, "btn_select_window"):
                self.btn_select_window.pack(side="right")
            if hasattr(self, "btn_select_roi"):
                self.btn_select_roi.configure(text="🎯  เลือกพื้นที่บนหน้าจอ")
            super()._update_readiness()

    def _set_mode_widgets_state(self, state: str) -> None:
        super()._set_mode_widgets_state(state)
        for w in ("mode_desktop_rb", "mode_dual_rb", "btn_detect_roblox", "btn_test_input"):
            if hasattr(self, w):
                try:
                    getattr(self, w).configure(state=state)
                except Exception:
                    pass

    def start(self) -> None:
        if (
            self.running
            or getattr(self, "start_pending", False)
            or getattr(self, "test_hold_pending", False)
            or getattr(self, "test_hold_active", False)
        ):
            return

        if self.env_mode == "dual_screen":
            self._start_dual_screen()
            return

        super().start()

    def _start_dual_screen(self) -> None:
        """Start auto-fishing loop targeting Roblox in the background on monitor 2."""
        try:
            if not self.target_hwnd:
                self._find_and_select_roblox_window()
            valid, reason = self._check_target_valid_windows()
            if not valid:
                raise RuntimeError(f"หน้าต่างเกมไม่พร้อม: {reason}")

            bar_roi = self.settings.get("windows", {}).get("rois", {}).get("bar") or self.settings.get("rois", {}).get("bar")
            if not bar_roi:
                raise RuntimeError("กรุณาเลือกพื้นที่แถบมินิเกมบนจอ 2 ก่อนเริ่ม")

            self.running = True
            self.stop_requested.clear()
            if not getattr(self, "_global_hotkey_cleanup", None):
                self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self._set_mode_widgets_state("disabled")
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="■  หยุด Auto (F8)", style="Danger.TButton")

            from auto_fishing import FishingController, MODE_CONFIGS, TEMPLATE_FILE
            mode = self.settings.get("fishing_mode", "rod")
            mode_name = MODE_CONFIGS.get(mode, MODE_CONFIGS["rod"])["name"]
            self.controller = FishingController(self._read_ui())
            self.capture = ScreenCapture()
            self.template = None
            if TEMPLATE_FILE.exists() and auto_fishing.cv2 is not None:
                self.template = auto_fishing.cv2.imread(str(TEMPLATE_FILE), auto_fishing.cv2.IMREAD_COLOR)

            self.fps_count = 0
            self.fps_started = time.monotonic()
            self._set_status(f"กำลังทำงานในโหมดสองจอ — {mode_name} — ใช้งานจอ 1 ได้ตามปกติ")
            self.root.after(20, self._pump_dual_screen)
        except Exception as exc:
            self.stop(f"เริ่มไม่ได้: {exc}")

    def _pump_dual_screen(self) -> None:
        """Main loop for Windows dual-screen background auto-fishing."""
        if not self.running or self.env_mode != "dual_screen":
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return

        valid, reason = self._check_target_valid_windows()
        if not valid:
            self.stop(f"หยุดการทำงาน: {reason}")
            return

        capture_time = time.monotonic()
        try:
            bar_roi = self.settings.get("windows", {}).get("rois", {}).get("bar") or self.settings.get("rois", {}).get("bar")
            bar_image = None
            try:
                bar_image = self.capture.grab(bar_roi)
            except Exception:
                bar_image = None

            if bar_image is None or bar_image.size == 0:
                self.mouse_held = False
                emergency_release_hwnd(self.target_hwnd)
                self.capture_fail_streak = getattr(self, "capture_fail_streak", 0) + 1
                if self.capture_fail_streak >= 5:
                    self.stop("จับภาพพื้นที่ Track ล้มเหลวต่อเนื่อง")
                    return
                self._set_status("จับภาพไม่ได้ — กำลังลองใหม่ (จอ 2)")
                self.root.after(16, self._pump_dual_screen)
                return
            self.capture_fail_streak = 0

            bar = detect_bar(bar_image)
            bite = False
            mode = self.settings.get("fishing_mode", "rod")
            bite_roi = self.settings.get("windows", {}).get("rois", {}).get("bite") or self.settings.get("rois", {}).get("bite")
            if mode != "net" and self.template is not None and bite_roi:
                bite = detect_bite(self.capture.grab(bite_roi), self.template, self.settings.get("bite_threshold", 0.85))

            observation = {"bar": bar, "bite": bite}
            self.fps_count += 1

            # Live preview throttling
            now_t = time.monotonic()
            if now_t - self._preview_throttle > 0.15:
                self._preview_throttle = now_t
                try:
                    self._update_preview_display(bar_image, bar)
                except Exception:
                    pass

            if self.stop_requested.is_set():
                self.stop("หยุดฉุกเฉินด้วย F8")
                return

            now = time.monotonic()
            action = self.controller.step(now, observation, focused=True, capture_time=capture_time)
            if not self._apply_background_action(action):
                self.controller.stop("ส่งคำสั่งอินพุตเบื้องหลังไม่สำเร็จ")
                self.stop("ส่งคำสั่งอินพุตเบื้องหลังไม่สำเร็จ")
                return

            elapsed = now - self.fps_started
            fps = self.fps_count / elapsed if elapsed > 0 else 0.0
            if self.settings.get("debug") and getattr(self.controller, "telemetry", None) and self.controller.state == "Track":
                t = self.controller.telemetry
                self._set_status(
                    f"[ดีบัก-สองจอ] ช่อง:[{t['target_left']:.0f},{t['target_right']:.0f}] "
                    f"กลาง:{t['target_center']:.0f} • ตัวชี้:{t['marker_x']:.0f} v:{t['velocity']:+.0f}px/s • สั่ง:{t['action']}"
                )
            elif observation.get("bar") and observation["bar"][0] == "absent" and self.controller.state in ("Track", "End"):
                self._set_status("ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่ (จอ 2)")
            else:
                self._set_status(f"{self.controller.state}: {self.controller.reason or 'tracking'} | {fps:.1f} FPS (จอ 2)")

            if self.controller.state == "Paused":
                self.stop()
                return
        except Exception as exc:
            self.stop(f"ข้อผิดพลาด: {exc}")
            return

        self.root.after(12, self._pump_dual_screen)

    def stop(self, reason: str | None = None) -> None:
        emergency_release_hwnd(self.target_hwnd)
        self.mouse_held = False
        super().stop(reason)
        if getattr(self, "env_mode", "desktop") == "dual_screen" and hasattr(self, "main_action_btn"):
            self.main_action_btn.configure(text="▶  เริ่ม Auto สองจอ (F8)", style="Primary.TButton")

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
        self.settings["windows"]["env_mode"] = self.env_mode
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
