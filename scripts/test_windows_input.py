"""Standalone Win32 Background Input Test Tool for Roblox on Windows.

Tests whether Roblox receives WM_LBUTTONDOWN / WM_LBUTTONUP messages via PostMessage
while unfocused, without moving the physical cursor or stealing window focus.
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

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
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL

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


# Track currently held hwnd for emergency cleanup
_active_held_hwnd: int | None = None


def emergency_release() -> None:
    global _active_held_hwnd
    if _active_held_hwnd is not None:
        user32 = get_user32()
        if user32:
            try:
                user32.PostMessageW(_active_held_hwnd, WM_LBUTTONUP, 0, 0)
            except Exception:
                pass
        _active_held_hwnd = None


atexit.register(emergency_release)


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
    """Encode client coordinates into Win32 LPARAM."""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def send_background_click(
    hwnd: int, x: int, y: int, hold_seconds: float = 0.08
) -> bool:
    """Send left button down and up directly to target window via PostMessage."""
    global _active_held_hwnd
    user32 = get_user32()
    if not user32 or not user32.IsWindow(hwnd):
        return False
    init_win32_api(user32)

    lparam = make_lparam(x, y)
    _active_held_hwnd = hwnd

    # 1. Post Mouse Down
    ret_down = user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    if not ret_down:
        _active_held_hwnd = None
        return False

    if hold_seconds > 0:
        time.sleep(hold_seconds)

    # 2. Post Mouse Up
    ret_up = user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    _active_held_hwnd = None
    return bool(ret_up)


def send_background_hold(hwnd: int, x: int, y: int) -> bool:
    """Send left button down without releasing."""
    global _active_held_hwnd
    user32 = get_user32()
    if not user32 or not user32.IsWindow(hwnd):
        return False
    init_win32_api(user32)
    lparam = make_lparam(x, y)
    _active_held_hwnd = hwnd
    return bool(user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam))


def send_background_release(hwnd: int, x: int, y: int) -> bool:
    """Send left button up."""
    global _active_held_hwnd
    user32 = get_user32()
    if not user32 or not user32.IsWindow(hwnd):
        return False
    init_win32_api(user32)
    lparam = make_lparam(x, y)
    ret = user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    _active_held_hwnd = None
    return bool(ret)


def run_interactive_test() -> dict[str, Any]:
    """Execute complete verification suite on a live Windows machine."""
    user32 = get_user32()
    if not user32:
        print("❌ ข้อผิดพลาด: สคริปต์นี้ต้องรันบนระบบปฏิบัติการ Windows ที่มี ctypes.windll.user32")
        return {"status": "error", "error": "Not running on Windows"}

    init_win32_api(user32)
    print("=" * 65)
    print(" 🧪 Roblox Auto Fishing — Win32 Background Input Test Tool")
    print("=" * 65)
    print("เป้าหมาย: พิสูจน์ว่า Roblox รับคำสั่ง PostMessage ขณะผู้ใช้ใช้งานจออื่นหรือไม่")
    print("เงื่อนไข: ห้ามขยับเมาส์จริง ห้ามแย่งโฟกัส และห้ามใช้ SendInput")
    print("-" * 65)

    windows = find_roblox_windows()
    if not windows:
        print("⚠️ ไม่พบหน้าต่าง Roblox ที่เปิดอยู่!")
        print("   กรุณาเปิด Roblox และเข้าเกมตกปลา จากนั้นเปิดหน้าต่างเกมค้างไว้บนจอที่ 2")
        return {"status": "error", "error": "No Roblox window found"}

    print(f"พบหน้าต่าง Roblox {len(windows)} หน้าต่าง:")
    for idx, w in enumerate(windows, 1):
        print(f"  [{idx}] HWND: {w['hwnd']} | '{w['title']}' ({w['class_name']})")
        print(f"      ขนาด Client Area: {w['width']} x {w['height']} px ที่พิกัด ({w['client_x']}, {w['client_y']})")
        if w["is_iconic"]:
            print("      ⚠️ หน้าต่างนี้ถูกย่อลง Taskbar (Minimized)")

    target = windows[0]
    hwnd = target["hwnd"]
    cw, ch = target["width"], target["height"]
    cx, cy = cw // 2, ch // 2

    if target["is_iconic"]:
        print("❌ หน้าต่าง Roblox ถูกย่ออยู่ กรุณาขยายหน้าต่างขึ้นมาบนจอที่ 2 ก่อนทดสอบ")
        return {"status": "error", "error": "Target window is minimized"}

    print("-" * 65)
    print(f"เป้าหมายทดสอบ: HWND {hwnd} ที่พิกัดกึ่งกลาง Client ({cx}, {cy})")
    print("\n👉 คำแนะนำก่อนเริ่มทดสอบ:")
    print("1. วางหน้าต่าง Roblox ไว้บนจอที่สอง (มองเห็นเกมชัดเจน)")
    print("2. คลิกไปที่หน้าต่างเบราว์เซอร์หรือโปรแกรมอื่นบนจอแรก (เพื่อให้ Roblox เสียโฟกัส)")
    print("3. ลองขยับเมาส์หรือพิมพ์ข้อความในเบราว์เซอร์บนจอแรกตามปกติ")
    print("4. สังเกตว่าตัวละครหรือคันเบ็ดในหน้าต่าง Roblox ตอบสนองหรือไม่")
    input("\n[กด Enter เมื่อพร้อมเริ่มการทดสอบ 3 รอบ]... ")

    results = []

    # Check state before
    pt_start = POINT()
    user32.GetCursorPos(ctypes.byref(pt_start))
    fg_before = user32.GetForegroundWindow()

    # --- Test 1: Single Click ---
    print("\n[การทดสอบที่ 1] ส่งคลิกสั้น (Single Click)...")
    t1_ret = send_background_click(hwnd, cx, cy, hold_seconds=0.1)
    time.sleep(0.5)

    pt_after1 = POINT()
    user32.GetCursorPos(ctypes.byref(pt_after1))
    fg_after1 = user32.GetForegroundWindow()

    m_moved1 = (pt_start.x != pt_after1.x) or (pt_start.y != pt_after1.y)
    stole_focus1 = (fg_after1 == hwnd) and (fg_before != hwnd)
    print(f"  • ส่งข้อความสำเร็จ (PostMessage): {t1_ret}")
    print(f"  • เมาส์จริงของผู้ใช้ถูกขยับหรือไม่: {'❌ มีการขยับ' if m_moved1 else '✅ ไม่ขยับ (ผ่าน)'}")
    print(f"  • แย่งโฟกัสจากหน้าต่างปัจจุบันหรือไม่: {'❌ แย่งโฟกัส' if stole_focus1 else '✅ ไม่แย่งโฟกัส (ผ่าน)'}")

    # --- Test 2: Long Hold ---
    print("\n[การทดสอบที่ 2] ส่งการกดค้าง 2.5 วินาที (Simulating Cast Hold)...")
    send_background_hold(hwnd, cx, cy)
    for i in range(5):
        time.sleep(0.5)
        print(f"  ... กำลังกดค้าง {(i + 1) * 0.5:.1f}s")
    send_background_release(hwnd, cx, cy)
    time.sleep(0.5)

    pt_after2 = POINT()
    user32.GetCursorPos(ctypes.byref(pt_after2))
    fg_after2 = user32.GetForegroundWindow()
    m_moved2 = (pt_start.x != pt_after2.x) or (pt_start.y != pt_after2.y)
    stole_focus2 = (fg_after2 == hwnd) and (fg_before != hwnd)

    # --- Test 3: Rapid Pulse ---
    print("\n[การทดสอบที่ 3] ส่งการเคาะเร็ว 3 ครั้ง (Rapid Clicks)...")
    for _ in range(3):
        send_background_click(hwnd, cx, cy, hold_seconds=0.06)
        time.sleep(0.12)

    emergency_release()

    print("\n" + "=" * 65)
    print("📊 สรุปผลการส่งข้อความอินพุตระดับ Win32:")
    print(f"  1. PostMessage ส่งได้สมบูรณ์: {t1_ret}")
    print(f"  2. ไม่รบกวนเมาส์จริงบนจอ 1: {not (m_moved1 or m_moved2)}")
    print(f"  3. ไม่แย่งโฟกัสจากเบราว์เซอร์: {not (stole_focus1 or stole_focus2)}")
    print("=" * 65)

    ans = input("\n❓ จากการสังเกตจอที่สอง เกม Roblox มีการตอบสนองจริง (เหวี่ยงเบ็ด/ขยับ) หรือไม่? [y/N]: ").strip().lower()
    game_responded = (ans == "y" or ans == "yes")

    report = {
        "timestamp": time.time(),
        "hwnd": hwnd,
        "title": target["title"],
        "post_message_success": t1_ret,
        "mouse_isolated": not (m_moved1 or m_moved2),
        "focus_isolated": not (stole_focus1 or stole_focus2),
        "game_responded": game_responded,
        "passed": bool(t1_ret and not m_moved1 and not stole_focus1 and game_responded),
    }

    if game_responded:
        print("\n🎉 ผ่านการทดสอบ! Roblox รับอินพุตเบื้องหลังผ่าน PostMessage ได้จริง")
    else:
        print("\n⚠️ ไม่ผ่าน: PostMessage ส่งถึงหน้าต่างได้ แต่เกม Roblox ไม่ตอบสนองขณะเสียโฟกัส")
        print("   สาเหตุที่เป็นไปได้: Roblox ใช้ RawInput / DirectInput หรือเอนจินเกมตรวจเช็ก Active Window ภายใน")

    out_file = Path(__file__).resolve().parent / "windows_input_test_result.json"
    try:
        out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nบันทึกผลการทดสอบลงไฟล์: {out_file}")
    except Exception:
        pass

    return report


if __name__ == "__main__":
    run_interactive_test()
