"""Roblox Auto Fishing - Windows UI.

Supports:
1. Normal Desktop mode (single monitor, Roblox foreground, shared mouse).
2. Dual-screen mode (experimental, Roblox visible on monitor 2, background PostMessage control).
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import os
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
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import auto_fishing
from auto_fishing import (
    APP_VERSION,
    ActionResult,
    BaseFishingApp,
    Controller,
    ScreenCapture,
    apply_action,
    detect_bar,
    detect_bite,
    emergency_release_mouse,
    focus_window,
    get_virtual_screen_bounds,
    get_window_bounds,
    is_window_alive,
    primary_screen_bounds,
    raw_release_mouse,
    register_stop,
    save_settings,
    set_dpi_awareness,
    window_snapshot,
    _inside,
    _load_pyautogui,
)

# Win32 Constants
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001

# SendInput Constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000


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


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTunion),
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

        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE

        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
    except Exception:
        pass


def _send_input_mouse(flags: int) -> tuple[bool, int]:
    """Dispatch a mouse event via Win32 SendInput. Returns (success, winerror)."""
    user32 = get_user32()
    if not user32:
        return False, -1
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dx = 0
    inp.u.mi.dy = 0
    inp.u.mi.mouseData = 0
    inp.u.mi.dwFlags = flags
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = 0
    cb = ctypes.sizeof(INPUT)
    try:
        sent = user32.SendInput(1, ctypes.byref(inp), cb)
        if sent == 1:
            return True, 0
        kernel32 = getattr(ctypes.windll, "kernel32", None)
        err = kernel32.GetLastError() if kernel32 else -1
        return False, err
    except Exception:
        return False, -1


class WindowsInputExecutor:
    """Dispatches physical mouse actions on Windows using SendInput (primary) or PyAutoGUI (fallback)."""

    def __init__(self, backend: str = "sendinput"):
        self.backend = backend
        self._actual_held: bool = False
        self._lock = threading.Lock()

    @property
    def actual_held(self) -> bool:
        return self._actual_held

    @actual_held.setter
    def actual_held(self, value: bool) -> None:
        self._actual_held = bool(value)

    def hold(self) -> ActionResult:
        with self._lock:
            if self.backend == "sendinput":
                ok, err = _send_input_mouse(MOUSEEVENTF_LEFTDOWN)
                if ok:
                    self._actual_held = True
                    return ActionResult(True, "ok", "กดค้างสำเร็จ (SendInput)")
                self._actual_held = False
                if err == 5:  # ERROR_ACCESS_DENIED
                    return ActionResult(
                        False,
                        "input_blocked_by_privilege_level",
                        "ส่งคำสั่งเมาส์ไม่ได้: สิทธิ์ไม่เพียงพอ (UIPI / Error 5) — กรุณารันด้วยสิทธิ์เดียวกับ Roblox",
                        winerror=err,
                    )
                return ActionResult(
                    False,
                    "send_input_failed",
                    f"SendInput ล้มเหลว (รหัสข้อผิดพลาด {err})",
                    winerror=err,
                )
            # PyAutoGUI fallback
            try:
                pyautogui = _load_pyautogui()
                old_failsafe = getattr(pyautogui, "FAILSAFE", True)
                try:
                    pyautogui.FAILSAFE = False
                    pyautogui.mouseDown(button="left")
                finally:
                    pyautogui.FAILSAFE = old_failsafe
                self._actual_held = True
                return ActionResult(True, "ok", "กดค้างสำเร็จ (PyAutoGUI)")
            except Exception as exc:
                self._actual_held = False
                return ActionResult(False, "mouse_down_failed", f"PyAutoGUI mouseDown ล้มเหลว: {exc}", exc)

    def release(self) -> ActionResult:
        with self._lock:
            if self.backend == "sendinput":
                ok, err = _send_input_mouse(MOUSEEVENTF_LEFTUP)
                self._actual_held = False
                if ok:
                    return ActionResult(True, "ok", "ปล่อยเมาส์สำเร็จ (SendInput)")
                return ActionResult(
                    False,
                    "send_input_failed",
                    f"SendInput release ล้มเหลว (รหัสข้อผิดพลาด {err})",
                    winerror=err,
                )
            # PyAutoGUI fallback
            res = raw_release_mouse()
            self._actual_held = False
            return res

    def emergency_release(self) -> None:
        with self._lock:
            self._actual_held = False
            try:
                _send_input_mouse(MOUSEEVENTF_LEFTUP)
            except Exception:
                pass
            try:
                emergency_release_mouse()
            except Exception:
                pass

    def click(self) -> ActionResult:
        hold_res = self.hold()
        if not hold_res:
            return hold_res
        time.sleep(0.02)
        rel_res = self.release()
        return rel_res if rel_res else hold_res

    def apply(
        self,
        action: str,
        target_snapshot: tuple[int, tuple[int, int, int, int]] | None,
        safe_move: bool = False,
    ) -> ActionResult:
        if action in ("none", ""):
            return ActionResult(True, "ok", "ไม่มีคำสั่ง")
        if action == "release":
            return self.release()

        if target_snapshot is None:
            self.emergency_release()
            return ActionResult(False, "target_missing", "ยังไม่ได้เลือกหน้าต่างเกม")

        target_wid, target_bounds = target_snapshot
        snap = window_snapshot()
        if snap is None:
            self.emergency_release()
            if not is_window_alive(target_wid):
                return ActionResult(False, "target_closed", "หน้าต่างเกมถูกปิดแล้ว")
            return ActionResult(False, "target_not_foreground", "ไม่พบหน้าต่างที่อยู่ด้านหน้า")

        if snap[0] != target_wid:
            self.emergency_release()
            if not is_window_alive(target_wid):
                return ActionResult(False, "target_closed", "หน้าต่างเกมถูกปิดแล้ว")
            return ActionResult(
                False,
                "foreground_hwnd_mismatch",
                f"หน้าต่างด้านหน้า (HWND {snap[0]}) ไม่ตรงกับเป้าหมาย (HWND {target_wid})",
            )

        current_bounds = snap[1]
        try:
            pyautogui = _load_pyautogui()
        except Exception as exc:
            self.emergency_release()
            return ActionResult(False, "pyautogui_import_failed", f"โหลด PyAutoGUI ไม่สำเร็จ: {exc}", exc)

        try:
            mouse_pos = tuple(pyautogui.position())
        except Exception as exc:
            self.emergency_release()
            return ActionResult(False, "mouse_position_failed", f"อ่านตำแหน่งเมาส์ไม่สำเร็จ: {exc}", exc)

        if not _inside(mouse_pos, current_bounds):
            if safe_move:
                safe_x = current_bounds[0] + max(10, current_bounds[2] // 2)
                safe_y = current_bounds[1] + max(10, current_bounds[3] // 2)
                try:
                    old_failsafe = getattr(pyautogui, "FAILSAFE", True)
                    try:
                        pyautogui.FAILSAFE = False
                        pyautogui.moveTo(safe_x, safe_y)
                    finally:
                        pyautogui.FAILSAFE = old_failsafe
                    mouse_pos = tuple(pyautogui.position())
                except Exception as exc:
                    self.emergency_release()
                    return ActionResult(
                        False,
                        "cursor_outside_target",
                        f"เลื่อนเมาส์เข้าเกมไม่สำเร็จ: {exc}",
                        exc,
                    )

            if not _inside(mouse_pos, current_bounds):
                self.emergency_release()
                return ActionResult(
                    False,
                    "cursor_outside_target",
                    "เมาส์อยู่นอกหน้าต่าง Roblox — กรุณาวางเมาส์ในเกม",
                )

        if action == "hold":
            if not self._actual_held:
                return self.hold()
            return ActionResult(True, "ok", "ถือเมาส์ค้างอยู่แล้ว")
        if action == "click":
            return self.click()

        return ActionResult(False, "unknown_action", f"ไม่รู้จักคำสั่ง: {action}")


def _is_roblox_game_window(title: str, class_name: str, process_id: int | None = None) -> bool:
    """Return True only for a Roblox player window, never this controller or Studio."""
    normalized_title = (title or "").strip().lower()
    normalized_class = (class_name or "").strip().upper()
    if process_id == os.getpid():
        return False
    if "auto fishing" in normalized_title or "roblox studio" in normalized_title:
        return False
    return normalized_class == "WINDOWSCLIENT" or normalized_title in {"roblox", "roblox player"}


def _window_monitor_name(user32: Any, hwnd: int) -> str:
    """Resolve a window to the Windows display name, for example DISPLAY2."""
    try:
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor:
            return ""
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return ""
        return str(info.szDevice).replace("\\\\.\\", "")
    except Exception:
        return ""


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

        process_id = wintypes.DWORD(0)
        try:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        except Exception:
            pass

        if _is_roblox_game_window(title, cls_name, int(process_id.value) or None):
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
                "process_id": int(process_id.value),
                "monitor": _window_monitor_name(user32, hwnd),
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
        self.target: tuple[int, tuple[int, int, int, int]] | None = None
        self.target_hwnd: int | None = None
        self.target_client_size: tuple[int, int] | None = None
        self.target_title: str = ""
        self.target_monitor: str = ""
        self.mouse_held: bool = False
        self.last_action: str = "none"
        self._test_pulse_thread: threading.Thread | None = None
        self.dev_mode: bool = False
        # Roblox has historically accepted the PyAutoGUI/mouse_event path more
        # reliably than SendInput on some client builds. Keep the legacy path
        # for foreground desktop actions and the hold-test; dual-screen mode
        # still uses PostMessage through _apply_background_action().
        self.executor = WindowsInputExecutor(backend="pyautogui")
        super().__init__(root)
        self.root.title(f"Roblox Auto Fishing {APP_VERSION} (Windows)")
        self.root.geometry("680x880")
        self.root.minsize(580, 760)
        try:
            self._find_and_select_roblox_window()
        except Exception:
            pass

    def _init_vars(self) -> None:
        super()._init_vars()
        win_cfg = self.settings.get("windows", {}) if isinstance(self.settings, dict) else {}
        self.dev_mode = bool(win_cfg.get("dev_mode", False) or "--dev" in sys.argv)
        env = win_cfg.get("env_mode", "desktop")
        if env not in ("desktop", "dual_screen") or not self.dev_mode:
            env = "desktop"
        self.env_mode = env
        self.vars["env_mode"] = self.tk.StringVar(value=env)
        self.vars["dev_mode"] = self.tk.BooleanVar(value=self.dev_mode)
        if "auto_restart_30s" in win_cfg:
            self.vars["auto_restart_30s"].set(bool(win_cfg["auto_restart_30s"]))
        if "auto_restart_interval" in win_cfg:
            self.vars["auto_restart_interval"].set(str(win_cfg["auto_restart_interval"]))

    def _build_mode_panel(self, outer: tk.Frame) -> None:
        """Render mode selection: Normal Desktop (default) vs Dev-gated Dual-Screen."""
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
            text="● โหมดปกติ (จอเดียว)" if self.env_mode == "desktop" else "⚠️ โหมดสองจอ (Dev Mode)",
            fg=GREEN_READY if self.env_mode == "desktop" else AMBER_WARN,
            bg="#064e3b" if self.env_mode == "desktop" else "#451a03",
            padx=8,
            pady=2,
            font=(self.ui_font, 8, "bold"),
        )
        self.mode_status_badge.pack(side="right")

        self.m_row = self.tk.Frame(self.mode_card, bg=BG_CARD)
        self.mode_desktop_rb = self.ttk.Radiobutton(
            self.m_row,
            text="🖥️  โหมดปกติ (จอเดียว - แชร์เมาส์และโฟกัส)",
            variable=self.vars["env_mode"],
            value="desktop",
            command=self._on_env_mode_change,
        )
        self.mode_desktop_rb.pack(side="left", padx=(0, 16))

        self.mode_dual_rb = self.ttk.Radiobutton(
            self.m_row,
            text="🧪  ทดสอบโหมดสองจอ (Dev Mode / จอ 2)",
            variable=self.vars["env_mode"],
            value="dual_screen",
            command=self._on_env_mode_change,
        )
        self.mode_dual_rb.pack(side="left")

        if self.dev_mode:
            self.m_row.pack(fill="x", pady=(0, 6))

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
            "📌 ข้อกำหนดโหมดสองจอ (Dev Mode / ทดลอง): ส่งการคลิกตรงเข้าหน้าต่าง Roblox ด้วย PostMessage "
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
        if self.env_mode == "dual_screen" and self.dev_mode:
            self.panel_dual.pack(fill="x", pady=(4, 0))
        else:
            self.panel_desktop.pack(fill="x", pady=(4, 0))

    def _build_advanced_extra(self, advanced_frame: tk.Frame) -> None:
        """Add research notice and dev mode unlock for Windows dual-screen."""
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
            text="🧪 กำลังวิจัยโหมดสองจอ / โหมดผู้พัฒนา (Windows Dual-Screen Feasibility / Dev Mode)",
            fg="#a78bfa",
            bg="#171520",
            font=(self.ui_font, 9, "bold"),
        ).pack(anchor="w")

        self.chk_dev_mode = self.ttk.Checkbutton(
            self.exp_frame,
            text="เปิดใช้งานโหมดผู้พัฒนา (ปลดล็อคตัวเลือกทดสอบสองจอ — ยังไม่พร้อมใช้งานจริง)",
            variable=self.vars["dev_mode"],
            command=self._on_dev_mode_toggle,
        )
        self.chk_dev_mode.pack(anchor="w", pady=(4, 4))

        exp_text = (
            "• โหมดสองจอยังไม่พร้อมใช้งานจริง เนื่องจาก Roblox บน Windows ไม่รับคำสั่งคลิกเมาส์เบื้องหลัง\n"
            "• ตัวเลือกนี้ถูกซ่อนไว้ในโหมดผู้พัฒนาเพื่อป้องกันความสับสน\n"
            "• ผู้ใช้ทั่วไปแนะนำให้ใช้ 'โหมดปกติ (จอเดียว)' โดยเปิดหน้าต่าง Roblox ค้างไว้ด้านหน้า"
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

    def _on_dev_mode_toggle(self) -> None:
        if self.running or getattr(self, "start_pending", False):
            self.vars["dev_mode"].set(self.dev_mode)
            return
        enabled = bool(self.vars["dev_mode"].get())
        self.dev_mode = enabled
        if "windows" not in self.settings or not isinstance(self.settings["windows"], dict):
            self.settings["windows"] = {}
        self.settings["windows"]["dev_mode"] = enabled
        if not enabled:
            self.env_mode = "desktop"
            self.vars["env_mode"].set("desktop")
            self.settings["windows"]["env_mode"] = "desktop"
        save_settings(self.settings)
        self._update_mode_ui_visibility()
        self._on_env_mode_change()

    def _update_mode_ui_visibility(self) -> None:
        if not hasattr(self, "m_row"):
            return
        GREEN_READY = "#34d399"
        AMBER_WARN = "#fbbf24"
        if self.dev_mode:
            target_before = self.panel_desktop if self.panel_desktop.winfo_ismapped() else (self.panel_dual if self.panel_dual.winfo_ismapped() else None)
            if target_before:
                self.m_row.pack(fill="x", pady=(0, 6), before=target_before)
            else:
                self.m_row.pack(fill="x", pady=(0, 6))
            self.mode_status_badge.configure(
                text="⚠️ โหมดสองจอ (Dev Mode)" if self.env_mode == "dual_screen" else "● โหมดปกติ (จอเดียว)",
                fg=AMBER_WARN if self.env_mode == "dual_screen" else GREEN_READY,
                bg="#451a03" if self.env_mode == "dual_screen" else "#064e3b",
            )
        else:
            self.m_row.pack_forget()
            self.mode_status_badge.configure(
                text="● โหมดปกติ (จอเดียว)",
                fg=GREEN_READY,
                bg="#064e3b",
            )
            self.panel_dual.pack_forget()
            self.panel_desktop.pack(fill="x", pady=(4, 0))

    def _on_env_mode_change(self) -> None:
        if self.running or getattr(self, "start_pending", False):
            self.vars["env_mode"].set(self.env_mode)
            return
        new_env = self.vars["env_mode"].get()
        if new_env in ("desktop", "dual_screen"):
            if new_env == "dual_screen" and not self.dev_mode:
                self.dev_mode = True
                self.vars["dev_mode"].set(True)
                self._update_mode_ui_visibility()
            self.env_mode = new_env
            if "windows" not in self.settings or not isinstance(self.settings["windows"], dict):
                self.settings["windows"] = {}
            self.settings["windows"]["env_mode"] = new_env
            save_settings(self.settings)

            # Clear target state across modes to avoid cross-mode confusion
            self._clear_roblox_target()

            GREEN_READY = "#34d399"
            AMBER_WARN = "#fbbf24"
            if new_env == "dual_screen":
                self.panel_desktop.pack_forget()
                self.panel_dual.pack(fill="x", pady=(4, 0))
                self.mode_status_badge.configure(
                    text="⚠️ โหมดสองจอ (Dev Mode)", fg=AMBER_WARN, bg="#451a03"
                )
                self._set_status("สลับเป็นโหมดสองจอ (Dev Mode) — กรุณาตรวจหาหน้าต่าง Roblox บนจอ 2")
            else:
                self.panel_dual.pack_forget()
                self.panel_desktop.pack(fill="x", pady=(4, 0))
                self.mode_status_badge.configure(
                    text="● โหมดปกติ (จอเดียว)", fg=GREEN_READY, bg="#064e3b"
                )
                self._set_status("สลับกลับเป็นโหมดปกติ (จอเดียว — ต้องโฟกัสเกม)")
            self._update_readiness()

    def _clear_roblox_target(self) -> None:
        self.target_hwnd = None
        self.target_client_size = None
        self.target_title = ""
        self.target_monitor = ""
        self.target = None
        self._update_readiness()

    def _check_privilege_hint(self, hwnd: int | None) -> None:
        """Check if target Roblox window is elevated (Admin) while Auto Fishing is not."""
        if not hwnd:
            return
        try:
            user32 = get_user32()
            if not user32:
                return
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
                if not h_proc:
                    if kernel32.GetLastError() == 5:  # ERROR_ACCESS_DENIED
                        self._set_status("⚠️ Roblox รันด้วยสิทธิ์ Administrator — แนะนำให้รัน Auto Fishing ด้วย Run as Administrator")
                else:
                    kernel32.CloseHandle(h_proc)
        except Exception:
            pass

    def _set_roblox_target(self, target: dict[str, Any]) -> None:
        """Apply one explicitly selected Roblox window to the controller."""
        self.target_hwnd = int(target["hwnd"])
        self.target_client_size = (int(target["width"]), int(target["height"]))
        self.target_title = str(target.get("title") or "Roblox")
        self.target_monitor = str(target.get("monitor") or "")

        # Also set self.target with verified client area bounds for desktop mode
        client_x = int(target.get("client_x", 0))
        client_y = int(target.get("client_y", 0))
        self.target = (
            self.target_hwnd,
            (client_x, client_y, self.target_client_size[0], self.target_client_size[1]),
        )

        screen_text = f" [{self.target_monitor}]" if self.target_monitor else ""
        if target.get("is_iconic"):
            if hasattr(self, "lbl_target_window"):
                self.lbl_target_window.configure(
                    text=f"หน้าต่างเกม: 🟡 {self.target_title}{screen_text} [HWND: {self.target_hwnd}] [ถูกย่อลง Taskbar]",
                    fg="#fbbf24",
                )
        elif hasattr(self, "lbl_target_window"):
            self.lbl_target_window.configure(
                text=(f"หน้าต่างเกม: 🟢 {self.target_title}{screen_text} [HWND: {self.target_hwnd}] "
                      f"({self.target_client_size[0]}×{self.target_client_size[1]} px)"),
                fg="#34d399",
            )
        self._check_privilege_hint(self.target_hwnd)
        self._update_readiness()

    def _find_and_select_roblox_window(self) -> None:
        """Locate the best Roblox client automatically for startup and recovery."""
        windows = find_roblox_windows()
        if not windows:
            if hasattr(self, "lbl_target_window"):
                hint = "จอ 2" if getattr(self, "env_mode", "desktop") == "dual_screen" else "คอมพิวเตอร์"
                self.lbl_target_window.configure(
                    text=f"หน้าต่างเกม: 🔴 ตรวจไม่พบ Roblox (กรุณาเปิดเกมบน{hint})",
                    fg="#fca5a5",
                )
            self._clear_roblox_target()
            return

        def _sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
            is_iconic = 1 if bool(item.get("is_iconic")) else 0
            if getattr(self, "env_mode", "desktop") == "dual_screen":
                mon_prio = 0 if str(item.get("monitor", "")).upper() == "DISPLAY2" else 1
            else:
                mon_prio = 0 if str(item.get("monitor", "")).upper() == "DISPLAY1" else 1
            cls_prio = 0 if str(item.get("class_name", "")).upper() == "WINDOWSCLIENT" else 1
            return (is_iconic, mon_prio, cls_prio)

        windows.sort(key=_sort_key)
        self._set_roblox_target(windows[0])

    def _show_roblox_window_picker(self) -> None:
        """Show an explicit Roblox window/display chooser instead of guessing by title."""
        windows = find_roblox_windows()
        if not windows:
            if hasattr(self, "lbl_target_window"):
                self.lbl_target_window.configure(
                    text="หน้าต่างเกม: 🔴 ตรวจไม่พบ Roblox Player (โปรดเปิดเกมก่อน)",
                    fg="#fca5a5",
                )
            self._clear_roblox_target()
            self._set_status("ตรวจไม่พบ Roblox Player — โปรแกรม Auto Fishing จะไม่ถูกเลือกเป็นเกม")
            return

        picker = self.tk.Toplevel(self.root)
        picker.title("เลือกหน้าต่าง Roblox และหน้าจอ")
        picker.transient(self.root)
        picker.resizable(False, False)
        picker.configure(bg="#1e1b29")

        self.tk.Label(
            picker,
            text="เลือกหน้าต่าง Roblox ที่ต้องการให้ Auto Fishing ควบคุม",
            fg="#f3f0fb",
            bg="#1e1b29",
            font=(self.ui_font, 10, "bold"),
        ).pack(anchor="w", padx=14, pady=(14, 8))

        listbox = self.tk.Listbox(
            picker,
            width=72,
            height=max(3, min(8, len(windows))),
            bg="#121118",
            fg="#f3f0fb",
            selectbackground="#7c3aed",
            selectforeground="#ffffff",
            activestyle="none",
            font=(self.ui_font, 9),
        )
        listbox.pack(fill="both", padx=14, pady=(0, 10))

        for index, item in enumerate(windows):
            monitor = item.get("monitor") or "ไม่ทราบจอ"
            minimized = " • ถูกย่อ" if item.get("is_iconic") else ""
            listbox.insert(
                "end",
                f"{monitor} • {item.get('title') or 'Roblox'} • "
                f"{item['width']}×{item['height']} px • HWND {item['hwnd']}{minimized}",
            )
            if item.get("hwnd") == self.target_hwnd:
                listbox.selection_set(index)
        if not listbox.curselection():
            listbox.selection_set(0)
        listbox.activate(listbox.curselection()[0])

        buttons = self.tk.Frame(picker, bg="#1e1b29")
        buttons.pack(fill="x", padx=14, pady=(0, 14))

        def choose_selected(_event=None):
            selected = listbox.curselection()
            if not selected:
                return
            target = windows[int(selected[0])]
            if target.get("is_iconic"):
                self._set_status("หน้าต่างที่เลือกถูกย่ออยู่ — กรุณาเปิด Roblox บนจอที่ต้องการ")
            self._set_roblox_target(target)
            picker.destroy()

        self.ttk.Button(buttons, text="ยกเลิก", command=picker.destroy).pack(side="right")
        self.ttk.Button(buttons, text="เลือกหน้าต่างนี้", command=choose_selected).pack(side="right", padx=(0, 8))
        listbox.bind("<Double-Button-1>", choose_selected)
        picker.protocol("WM_DELETE_WINDOW", picker.destroy)
        picker.update_idletasks()
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - picker.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + 80
        picker.geometry(f"+{x}+{y}")
        picker.grab_set()
        listbox.focus_set()

    def select_window(self) -> None:
        """Allow user to pick Roblox window explicitly in desktop mode."""
        if self.running or getattr(self, "start_pending", False):
            self._set_status("หยุดการทำงานก่อนเปลี่ยนหน้าต่างเกม")
            return
        self._show_roblox_window_picker()

    def select_rois(self) -> None:
        target = getattr(self, "target", None)
        if not target or not is_window_alive(target[0]):
            self._find_and_select_roblox_window()
        super().select_rois()

    def _begin_area_selection(self, name: str) -> None:
        target = getattr(self, "target", None)
        if not target or not is_window_alive(target[0]):
            self._find_and_select_roblox_window()
            target = getattr(self, "target", None)
        if target and getattr(self, "env_mode", "desktop") == "desktop":
            try:
                focus_window(target[0])
            except Exception:
                pass
        super()._begin_area_selection(name)

    def _capture_area(self, name: str) -> None:
        try:
            target = getattr(self, "target", None)
            if not target or not is_window_alive(target[0]):
                self._find_and_select_roblox_window()
                target = getattr(self, "target", None)

            if target and is_window_alive(target[0]):
                cur_bounds = get_window_bounds(target[0]) or target[1]
                bounds = cur_bounds
            else:
                bounds = primary_screen_bounds()

            capture = ScreenCapture()
            image = capture.grab(bounds)
            capture.close()
        except Exception as exc:
            self.root.deiconify()
            self._set_status(f"จับภาพพื้นที่ไม่ได้: {exc}")
            return
        self._open_roi_canvas(image, bounds, name)

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
                released = send_background_release(self.target_hwnd, w // 2, h // 2)
                if not released:
                    # Keep the held flag set so cleanup can retry instead of
                    # falsely reporting a successful release.
                    return False
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

    def _begin_test_hold(self) -> None:
        """Run the hold test through the input path belonging to the active mode."""
        if self.env_mode != "dual_screen":
            # One-screen mode deliberately keeps the legacy foreground mouse
            # behaviour implemented by BaseFishingApp + PyAutoGUI executor.
            super()._begin_test_hold()
            return

        if not getattr(self, "test_hold_pending", False):
            return
        self.test_hold_pending = False
        self.test_hold_id = None
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return

        valid, reason = self._check_target_valid_windows()
        if not valid:
            self.stop(f"ทดลองกดค้างแบบจอแยกไม่ได้: {reason}")
            return
        if not self._apply_background_action("hold"):
            self.stop("ทดลองกดค้างแบบจอแยกล้มเหลว: Roblox ไม่รับ PostMessage")
            return

        self.test_hold_active = True
        duration = getattr(self, "test_hold_duration", 1.0)
        self._set_status(f"กำลังกดค้างผ่าน PostMessage {duration:.1f} วินาที (โหมดจอแยก)...")
        self.test_hold_id = self.root.after(max(50, int(duration * 1000)), self._finish_test_hold)

    def _finish_test_hold(self) -> None:
        """Release with the same input backend that began the hold test."""
        if self.env_mode != "dual_screen":
            super()._finish_test_hold()
            return

        self.test_hold_active = False
        self.test_hold_id = None
        released = self._apply_background_action("release")
        if not released:
            emergency_release_hwnd(self.target_hwnd)
            self.mouse_held = False
        if self.hotkey_cleanup:
            try:
                self.hotkey_cleanup()
            except Exception:
                pass
            self.hotkey_cleanup = None

        if self.stop_requested.is_set():
            self._set_status("หยุดฉุกเฉินด้วย F8")
        elif released:
            self._set_status("✅ ทดลองกดค้างและปล่อยผ่าน PostMessage แล้ว — โปรดดูว่า Roblox ตอบสนองหรือไม่")
        else:
            self._set_status("❌ คำสั่งปล่อย PostMessage ล้มเหลว — ส่งคำสั่งปล่อยฉุกเฉินแล้ว")

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

        windows = find_roblox_windows()
        if len(windows) == 1 and not windows[0].get("is_iconic"):
            self._set_roblox_target(windows[0])
        elif not self.target and len(windows) > 1:
                self._show_roblox_window_picker()
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
            controller_settings = self._read_ui()
            bite_roi = self.settings.get("windows", {}).get("rois", {}).get("bite") or self.settings.get("rois", {}).get("bite")
            controller_settings["rois"] = {"bar": bar_roi, "bite": bite_roi}
            self.controller = FishingController(controller_settings)
            self.controller.start(time.monotonic())
            self.capture = ScreenCapture()
            self.template = None
            if TEMPLATE_FILE.exists() and auto_fishing.cv2 is not None:
                self.template = auto_fishing.cv2.imread(str(TEMPLATE_FILE), auto_fishing.cv2.IMREAD_COLOR)

            self.fps_count = 0
            self.fps_started = time.monotonic()
            self._set_status(f"กำลังทำงานในโหมดสองจอ — {mode_name} — ใช้งานจอ 1 ได้ตามปกติ")
            self._schedule_auto_restart()
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

    def _check_target_status(self) -> tuple[str, tuple[int, int, int, int] | None]:
        """Verify window identity, bounds, ROI containment, and foreground focus for Windows."""
        if not self.target:
            return "no_target", None
        target_wid, target_bounds = self.target

        # Check snapshot from ui_windows or auto_fishing
        if isinstance(window_snapshot, mock.Mock) or getattr(window_snapshot, "_mock_name", None):
            snap = window_snapshot()
        else:
            snap = auto_fishing.window_snapshot()
        alive = is_window_alive(target_wid) or (snap is not None and snap[0] == target_wid)
        if not alive:
            return "closed", None

        cur_bounds = get_window_bounds(target_wid)
        if not cur_bounds:
            cur_bounds = snap[1] if snap is not None else target_bounds

        if cur_bounds != target_bounds:
            old_bounds = target_bounds
            self.target = (target_wid, cur_bounds)
            if hasattr(self, "_relative_rois") and self._relative_rois:
                for name, rel_roi in self._relative_rois.items():
                    rel_x, rel_y, rw, rh = rel_roi
                    new_roi = [cur_bounds[0] + rel_x, cur_bounds[1] + rel_y, rw, rh]
                    if auto_fishing._inside_rect(new_roi, cur_bounds):
                        self.settings.setdefault("rois", {})[name] = new_roi

        bar_roi = self.settings.get("rois", {}).get("bar")
        if bar_roi and not auto_fishing._inside_rect(bar_roi, cur_bounds):
            return "roi_outside", cur_bounds

        if snap is None or snap[0] != target_wid:
            return "not_foreground", cur_bounds

        return "ready", cur_bounds

    def _pump_desktop_legacy(self) -> None:
        """Main loop for Windows normal single-monitor desktop auto-fishing."""
        if not self.running or self.env_mode != "desktop":
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return

        now = time.monotonic()
        capture_time = now

        target_status, current_bounds = self._check_target_status()

        if target_status == "closed":
            self.stop("หน้าต่างเกมถูกปิด")
            return

        if target_status == "not_foreground":
            observation = None
            focused = False
            self._set_status("รอโฟกัสเกม")
        elif target_status == "mouse_outside":
            observation = None
            focused = False
            self._set_status("เมาส์อยู่นอกเกม — เลื่อนเมาส์กลับเพื่อทำงานต่อ")
        elif target_status == "roi_outside":
            observation = None
            focused = False
            self._set_status("พื้นที่ Track อยู่นอกหน้าต่าง")
        elif target_status == "ready":
            bar_roi = self.settings.get("rois", {}).get("bar")
            bar_image = None
            try:
                if bar_roi and self.capture:
                    bar_image = self.capture.grab(bar_roi)
            except Exception:
                bar_image = None

            if bar_image is None or bar_image.size == 0:
                observation = None
                focused = True
                self._set_status("จับภาพไม่ได้ — กำลังลองใหม่")
            else:
                if isinstance(detect_bar, mock.Mock) or getattr(detect_bar, "_mock_name", None):
                    bar = detect_bar(bar_image)
                else:
                    bar = auto_fishing.detect_bar(bar_image)

                bite = False
                mode = self.settings.get("fishing_mode", "rod")
                bite_roi = self.settings.get("rois", {}).get("bite")
                if mode != "net" and self.template is not None and bite_roi:
                    try:
                        bite_crop = self.capture.grab(bite_roi)
                        if isinstance(detect_bite, mock.Mock) or getattr(detect_bite, "_mock_name", None):
                            bite = detect_bite(bite_crop, self.template, self.settings.get("bite_threshold", 0.85))
                        else:
                            bite = auto_fishing.detect_bite(bite_crop, self.template, self.settings.get("bite_threshold", 0.85))
                    except Exception:
                        bite = False
                observation = {"bar": bar, "bite": bite}
                self.fps_count += 1
                focused = True

                now_t = time.monotonic()
                if now_t - self._preview_throttle > 0.15:
                    self._preview_throttle = now_t
                    try:
                        self._update_preview_display(bar_image, bar)
                    except Exception:
                        pass
        else:
            observation = None
            focused = False

        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return

        action = self.controller.step(now, observation, focused, capture_time=capture_time)

        executor = getattr(self, "executor", None)
        actual_held = getattr(executor, "actual_held", None) if executor is not None else None
        if actual_held is None:
            actual_held = getattr(self.controller, "actual_held", False)
        else:
            self.controller.sync_held(actual_held)

        if action == "none" and self.controller.desired_held != actual_held:
            action = "hold" if self.controller.desired_held else "release"

        if isinstance(apply_action, mock.Mock) or getattr(apply_action, "_mock_name", None):
            res = apply_action(action, self.target)
        elif executor is not None and hasattr(executor, "apply"):
            res = executor.apply(action, self.target, safe_move=True)
        else:
            res = apply_action(action, self.target, safe_move=True)

        if action in ("hold", "release"):
            self.controller.acknowledge_action(action, bool(res))
            if executor is not None and hasattr(executor, "actual_held"):
                self.controller.sync_held(executor.actual_held)

        if not res:
            self.input_failure_streak = getattr(self, "input_failure_streak", 0) + 1
            emergency_release_mouse()
            if executor is not None and hasattr(executor, "emergency_release"):
                executor.emergency_release()
            self.controller.sync_held(False)
            if getattr(res, "reason", "") == "cursor_outside_target":
                self._set_status("เมาส์อยู่นอกหน้าต่าง Roblox — กรุณาวางเมาส์ในเกม")
            elif getattr(res, "reason", "") in ("target_not_foreground", "foreground_hwnd_mismatch"):
                self._set_status("รอโฟกัสเกม — หน้าต่าง Roblox ไม่ได้อยู่ด้านหน้า")
            elif getattr(res, "reason", "") == "input_blocked_by_privilege_level":
                self._set_status("ส่งคำสั่งเมาส์ไม่ได้: สิทธิ์ไม่เพียงพอ — แนะนำให้รันด้วยสิทธิ์เดียวกับ Roblox")
            else:
                detail = getattr(res, "detail", "") or getattr(res, "reason", "")
                self._set_status(f"ส่งคำสั่งเมาส์ไม่ได้: {detail}")

            if self.input_failure_streak >= 15:
                self.stop("หยุดเนื่องจากส่งคำสั่งเมาส์ล้มเหลวติดต่อกันเกินกำหนด")
                return
        else:
            self.input_failure_streak = 0

        elapsed = now - self.fps_started
        fps = self.fps_count / elapsed if elapsed > 0 else 0.0

        if target_status == "ready" and observation is not None and bool(res):
            if self.settings.get("debug") and getattr(self.controller, "telemetry", None) and self.controller.state == "Track":
                t = self.controller.telemetry
                self._set_status(
                    f"[ดีบัก] ช่อง:[{t['target_left']:.0f},{t['target_right']:.0f}] กลาง:{t['target_center']:.0f} "
                    f"• ตัวชี้:{t['marker_x']:.0f} คาดการณ์:{t['predicted_x']:.0f} v:{t['velocity']:+.0f}px/s "
                    f"• หน่วง:{t['total_latency_ms']:.0f}ms (ภาพ:{t['frame_age_ms']:.0f}ms) • สั่ง:{t['action']}"
                )
            elif observation.get("bar") and observation["bar"][0] == "absent" and self.controller.state in ("Track", "End"):
                self._set_status("ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่")
            else:
                self._set_status(f"{self.controller.state}: {self.controller.reason or 'tracking'} | {fps:.1f} FPS")

        if self.controller.state == "Paused":
            self.stop()
            return

        self.root.after(12, self._pump)

    def _pump(self) -> None:
        if self.env_mode == "dual_screen":
            self._pump_dual_screen()
        else:
            self._pump_desktop_legacy()

    def stop(self, reason: str | None = None) -> None:
        emergency_release_hwnd(getattr(self, "target_hwnd", None))
        if hasattr(self, "executor") and hasattr(self.executor, "emergency_release"):
            self.executor.emergency_release()
        self.mouse_held = False
        super().stop(reason)
        if getattr(self, "env_mode", "desktop") == "dual_screen" and hasattr(self, "main_action_btn"):
            if reason != "auto_restart_cycle":
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
        if "auto_restart_30s" in settings:
            self.settings["windows"]["auto_restart_30s"] = bool(settings["auto_restart_30s"])
        if "auto_restart_interval" in settings:
            self.settings["windows"]["auto_restart_interval"] = int(settings["auto_restart_interval"])
        save_settings(self.settings)
        return settings


if platform.system() == "Windows":
    auto_fishing.FishingApp = WindowsFishingApp


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Roblox Auto Fishing (Windows)")
    parser.add_argument("--replay", metavar="DIR", help="analyze saved frames without mouse input")
    parser.add_argument("--dev", action="store_true", help="enable developer mode (unlock experimental dual-screen mode)")
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
