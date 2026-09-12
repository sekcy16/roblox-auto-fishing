"""Screen based Roblox fishing helper for visible, focused game windows.

The module is import safe: it does not capture screens, register hotkeys, or
send input until the UI or an explicit API call starts it.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import numpy as np
except ImportError:  # pragma: no cover - requirements supplies numpy
    np = None

try:
    import cv2
except ImportError:  # pragma: no cover - pure logic remains usable
    cv2 = None


ROOT = Path(__file__).resolve().parent
APP_VERSION = "0.0.1"


def user_data_dir(system: str | None = None, environ: dict[str, str] | None = None) -> Path:
    """Return a writable persistent directory for user-created settings."""
    if (system or platform.system()) == "Windows":
        environment = os.environ if environ is None else environ
        return Path(environment.get("LOCALAPPDATA") or Path.home()) / "RobloxAutoFishing"
    return ROOT


DATA_DIR = user_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
TEMPLATE_FILE = DATA_DIR / "bite-template.png"
MODE_CONFIGS = {
    "rod": {
        "name": "เบ็ดตกปลา",
        "description": "เบ็ดตกปลา — โดยทั่วไปปลากินภายในประมาณ 30 วินาที",
    },
    "net": {
        "name": "แห",
        "description": "แห — อาจใช้เวลารอประมาณ 30–60 วินาที และช่วงดึงปลาอาจนานกว่า",
    },
}
DEFAULT_SETTINGS = {
    "fishing_mode": "rod",
    "cast_seconds": 2.0,
    "right_on_hold": False,
    "margin": 3.0,
    "lead": 0.0,
    "bite_threshold": 0.85,
    "bite_ack": True,
    "rois": {"bar": None, "bite": None},
}

_pyautogui = None
_mouse_held = False
_input_lock = threading.RLock()
_input_pause_before_run = None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_settings(data: dict[str, Any], screen_bounds: tuple[int, int, int, int]) -> dict[str, Any]:
    """Validate user settings and return a normalized copy."""
    if not isinstance(data, dict) or len(screen_bounds) != 4:
        raise ValueError("ต้องมีข้อมูลตั้งค่าและขอบเขตหน้าจอ")
    sx, sy, sw, sh = map(int, screen_bounds)
    if sw <= 0 or sh <= 0:
        raise ValueError("ขอบเขตหน้าจอต้องมีขนาดมากกว่าศูนย์")
    result = dict(DEFAULT_SETTINGS)
    result.update({k: v for k, v in data.items() if k != "rois"})
    mode = result.get("fishing_mode", "rod")
    if not isinstance(mode, str) or mode not in MODE_CONFIGS:
        raise ValueError("ประเภทอุปกรณ์ตกปลาต้องเป็น 'rod' หรือ 'net'")
    result["fishing_mode"] = str(mode)
    for key, low, high in (("cast_seconds", 0.05, 10.0), ("lead", 0.0, 0.5),
                           ("margin", 0.0, None), ("bite_threshold", 0.0, 1.0)):
        value = result.get(key)
        if not _finite(value) or float(value) < low or (high is not None and float(value) > high):
            raise ValueError(f"ค่า {key} อยู่นอกช่วงที่กำหนด")
        result[key] = float(value)
    for key in ("right_on_hold", "bite_ack"):
        if key in result and not isinstance(result.get(key), bool):
            raise ValueError(f"ค่า {key} ต้องเป็น true หรือ false")
    rois = dict(DEFAULT_SETTINGS["rois"])
    rois.update(data.get("rois") or {})
    if rois.get("bar") is None:
        raise ValueError("ต้องกำหนดพื้นที่แถบมินิเกม")
    if rois.get("bite") is None:
        raise ValueError("ต้องกำหนดพื้นที่สัญญาณปลากินเบ็ด")
    for name, roi in rois.items():
        if roi is None:
            continue
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            raise ValueError(f"พื้นที่ {name} ต้องเป็น [x, y, width, height]")
        x, y, w, h = roi
        if not all(_finite(v) for v in (x, y, w, h)) or int(w) <= 0 or int(h) <= 0:
            raise ValueError(f"ขนาดพื้นที่ {name} ไม่ถูกต้อง")
        x, y, w, h = map(int, (x, y, w, h))
        if x < sx or y < sy or x + w > sx + sw or y + h > sy + sh:
            raise ValueError(f"พื้นที่ {name} อยู่นอกหน้าต่างเกมที่เลือก")
        rois[name] = [x, y, w, h]
    result["rois"] = rois
    return result


def detect_bar(bgr: Any) -> tuple[str, float | None, tuple[float, float] | None]:
    """Pair a purple horizontal target with an aligned white vertical marker.

    Do not locate the black track first: a dark scene merges with it. HSV
    tolerates the blue-purple target seen in the actual night-game capture.
    """
    if (cv2 is None or np is None or not isinstance(bgr, np.ndarray)
            or bgr.ndim != 3 or bgr.shape[2] < 3 or not bgr.size):
        return "absent", None, None
    image = np.ascontiguousarray(bgr[:, :, :3], dtype=np.uint8)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    purple = cv2.inRange(hsv, (115, 35, 50), (165, 255, 255))
    white = cv2.inRange(hsv, (0, 0, 200), (179, 45, 255))

    def components(mask):
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        return [tuple(map(int, row)) for row in stats[1:]]

    targets = [(x, y, w, h) for x, y, w, h, area in components(purple)
               if 4 <= h <= 64 and w >= 12 and w >= h * 1.3
               and area >= w * h * 0.6]
    markers = [(x, y, w, h) for x, y, w, h, area in components(white)
               if 6 <= h <= 80 and 3 <= w <= h
               and area >= w * h * 0.55]
    if not targets:
        return "absent", None, None

    def aligned(a, b):
        _, y, _, h = a
        _, other_y, _, other_h = b
        return (0.5 <= h / other_h <= 2.0
                and abs(y + h / 2 - other_y - other_h / 2) <= max(3, min(h, other_h) * 0.3))

    # Join target halves only when the white marker visibly covers their gap.
    targets.sort()
    merged = []
    for target in targets:
        x, y, w, h = target
        joined = False
        for index, previous in enumerate(merged):
            px, py, pw, ph = previous
            gap_left, gap_right = px + pw, x
            if aligned(previous, target) and 0 <= gap_right - gap_left <= max(h, ph) * 1.5:
                covering = any(aligned(marker, target) and mx <= gap_left + 2
                               and mx + mw >= gap_right - 2
                               for marker in markers for mx, my, mw, mh in [marker])
                if covering:
                    top, bottom = min(py, y), max(py + ph, y + h)
                    merged[index] = (px, top, x + w - px, bottom - top)
                    joined = True
                    break
        if not joined:
            merged.append(target)

    matches = []
    for target in merged:
        x, y, w, h = target
        if w < h * 1.5:
            continue
        for marker in markers:
            if aligned(target, marker):
                mx, my, mw, mh = marker
                y_center_diff = abs((y + h / 2.0) - (my + mh / 2.0))
                h_diff = abs(h - mh)
                score = y_center_diff * 2.0 + h_diff
                matches.append((score, (mx + (mw - 1) / 2), (float(x), float(x + w))))
    if not matches:
        return "absent", None, None
    if len(matches) == 1:
        return "valid", matches[0][1], matches[0][2]
    matches.sort(key=lambda m: m[0])
    best_score, best_marker, best_target = matches[0]
    second_score = matches[1][0]
    if second_score - best_score >= 1.5:
        return "valid", best_marker, best_target
    return "ambiguous", None, None


def detect_bite(bgr: Any, template: Any, threshold: float = 0.85) -> bool:
    """Match a captured bite template inside its configured ROI."""
    if cv2 is None or np is None or not isinstance(bgr, np.ndarray) or not isinstance(template, np.ndarray):
        return False
    if bgr.size == 0 or template.size == 0 or template.ndim < 2 or bgr.ndim < 2:
        return False
    if (template.shape[0] > bgr.shape[0] or template.shape[1] > bgr.shape[1]
            or not _has_spatial_variation(template)):
        return False
    result = cv2.matchTemplate(bgr, template, cv2.TM_CCOEFF_NORMED)
    return bool(result.size and float(result.max()) >= float(threshold))


def _has_spatial_variation(template: Any) -> bool:
    if np is None or not isinstance(template, np.ndarray) or template.ndim < 2:
        return False
    return bool(np.std(template.astype(np.float32), axis=(0, 1)).max() > 1e-6)


def choose_hold(x: float, target: tuple[float, float], velocity: float, lead: float,
                margin: float, right_on_hold: bool, held: bool) -> bool:
    values = (x, velocity, lead, margin, *target)
    if not all(_finite(value) for value in values) or target[0] >= target[1] or margin < 0:
        raise ValueError("invalid tracking values")
    predicted = x + velocity * lead
    error = (target[0] + target[1]) / 2 - predicted
    if abs(error) <= margin:
        return held
    return (error > 0) == right_on_hold


class Controller:
    """Pure timing and control state machine; it never talks to the OS."""

    def __init__(self, settings: dict[str, Any], wait_timeout: float | None = None,
                 track_timeout: float | None = None):
        self.settings = settings
        self.state = "Idle"
        self.reason = ""
        self.held = False
        self.cast_started = self.wait_started = self.absent_started = None
        self.valid_streak = self.bite_streak = self.bite_clear_streak = 0
        self.bite_latched = False
        self.retry_count = 0
        self.ambiguous_streak = 0
        self.previous_x = self.previous_time = None
        self.velocity = 0.0

    def start(self, now: float) -> None:
        self.state, self.reason = "Cast", ""
        self.cast_started = float(now)
        self.wait_started = self.absent_started = None
        self.valid_streak = self.bite_streak = self.bite_clear_streak = 0
        self.ambiguous_streak = 0
        self.bite_latched = False
        self.held = False
        self.previous_x = self.previous_time = None
        self.velocity = 0.0

    def stop(self, reason: str = "stopped by user") -> str:
        action = "release" if self.held else "none"
        self.held = False
        self.state, self.reason = "Paused", reason
        return action

    def _pause(self, reason: str) -> str:
        action = "release" if self.held else "none"
        self.held = False
        self.state, self.reason = "Paused", reason
        return action

    def _enter_cast(self, now: float) -> str:
        self.state, self.cast_started = "Cast", float(now)
        self.wait_started = self.absent_started = None
        self.valid_streak = self.bite_streak = self.bite_clear_streak = 0
        self.ambiguous_streak = 0
        self.bite_latched = False
        self.held = True
        return "hold"

    def _bar(self, observation: dict[str, Any]) -> tuple[str, float | None, tuple[float, float] | None]:
        value = observation.get("bar") if isinstance(observation, dict) else None
        return value if isinstance(value, tuple) and len(value) == 3 else ("absent", None, None)

    def _track_step(self, now: float, x: float, target: tuple[float, float]) -> str:
        entering = (self.state != "Track")
        self.state = "Track"
        self.absent_started = None
        self.ambiguous_streak = 0
        if not entering and self.previous_x is not None and self.previous_time is not None and now > self.previous_time:
            self.velocity = (x - self.previous_x) / (now - self.previous_time)
        else:
            self.velocity = 0.0
        self.previous_x, self.previous_time = x, now
        margin = min(float(self.settings["margin"]), max(0.0, (target[1] - target[0]) / 2))
        desired = choose_hold(x, target, self.velocity, float(self.settings["lead"]), margin,
                              bool(self.settings["right_on_hold"]), self.held)
        if entering:
            self.held = desired
            return "hold" if desired else "release"
        if desired != self.held:
            self.held = desired
            return "hold" if desired else "release"
        return "none"

    def step(self, now: float, observation: dict[str, Any] | None, focused: bool) -> str:
        now = float(now)
        if not focused:
            return self._pause("focus lost; press Start to resume")
        if observation is None:
            return self._pause("screen capture failed")
        status, x, target = self._bar(observation)
        if self.state in ("Idle", "Paused"):
            return "none"
        if self.state == "Cast":
            if not self.held:
                self.held = True
                return "hold"
            if now - float(self.cast_started) >= float(self.settings["cast_seconds"]):
                self.held = False
                self.state, self.wait_started = "Wait", now
                return "release"
            return "none"
        bite = bool(observation.get("bite", False))
        if bite:
            self.bite_streak += 1
            self.bite_clear_streak = 0
        else:
            self.bite_clear_streak += 1
            self.bite_streak = 0
            if self.bite_clear_streak >= 3:
                self.bite_latched = False
        if self.state == "Wait":
            if self.settings.get("bite_ack", True) and self.bite_streak >= 3 and not self.bite_latched:
                self.bite_latched = True
                return "click"
            if status == "valid" and x is not None and target is not None:
                return self._track_step(now, x, target)
            return "none"
        if self.state == "End":
            if status == "valid" and x is not None and target is not None:
                return self._track_step(now, x, target)
            if self.absent_started is None:
                self.absent_started = now
            if now - self.absent_started >= 1.0:
                return self._enter_cast(now)
            return "none"
        if self.state == "Track":
            if status == "ambiguous":
                self.ambiguous_streak += 1
                if self.ambiguous_streak >= 30:
                    return self._pause("bar detection is ambiguous")
                return "none"
            self.ambiguous_streak = 0
            if status != "valid" or x is None or target is None:
                self.previous_x = self.previous_time = None
                self.absent_started = self.absent_started if self.absent_started is not None else now
                action = "release" if self.held else "none"
                self.held = False
                self.state, self.absent_started = "End", now
                return action
            return self._track_step(now, x, target)
        return "none"


def _load_pyautogui():
    global _pyautogui
    if _pyautogui is None:
        import pyautogui
        _pyautogui = pyautogui
    return _pyautogui


def release_mouse() -> None:
    """Release the left button, tolerating cleanup during focus loss/shutdown."""
    global _mouse_held
    with _input_lock:
        try:
            pyautogui = _load_pyautogui()
            old_failsafe = getattr(pyautogui, "FAILSAFE", True)
            try:
                pyautogui.FAILSAFE = False
                pyautogui.mouseUp(button="left")
            finally:
                pyautogui.FAILSAFE = old_failsafe
        except Exception:
            pass
        finally:
            _mouse_held = False


def _inside(point: tuple[int, int], bounds: tuple[int, int, int, int]) -> bool:
    x, y, w, h = bounds
    return x <= point[0] < x + w and y <= point[1] < y + h


def apply_action(action: str, target_snapshot: tuple[int, tuple[int, int, int, int]] | None) -> bool:
    """Apply one state transition only when the target window still matches."""
    global _mouse_held
    with _input_lock:
        if action in ("none", ""):
            return True
        if action == "release":
            release_mouse()
            return True
        if target_snapshot is None or window_snapshot() != target_snapshot:
            release_mouse()
            return False
        try:
            pyautogui = _load_pyautogui()
            if not _inside(tuple(pyautogui.position()), target_snapshot[1]):
                release_mouse()
                return False
            if action == "hold":
                if not _mouse_held:
                    pyautogui.mouseDown(button="left")
                    _mouse_held = True
                return True
            if action == "click":
                pyautogui.click(button="left")
                return True
        except Exception:
            release_mouse()
            return False
        return False


def _windows_snapshot():
    user32 = ctypes.windll.user32
    hwnd_t = ctypes.c_void_p
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], hwnd_t
    user32.GetClientRect.argtypes, user32.GetClientRect.restype = [hwnd_t, ctypes.POINTER(RECT)], ctypes.c_int
    user32.ClientToScreen.argtypes, user32.ClientToScreen.restype = [hwnd_t, ctypes.POINTER(POINT)], ctypes.c_int
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = RECT()
    point = POINT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)) or not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    hwnd_value = hwnd.value if isinstance(hwnd, ctypes.c_void_p) else hwnd
    return int(hwnd_value or 0), (point.x, point.y, rect.right - rect.left, rect.bottom - rect.top)


def _x11_snapshot():
    if not os.environ.get("DISPLAY") or (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        return None
    try:
        from Xlib import X, display
        dpy = display.Display()
        root = dpy.screen().root
        atom = dpy.intern_atom("_NET_ACTIVE_WINDOW")
        prop = root.get_full_property(atom, X.AnyPropertyType)
        if not prop or not prop.value:
            dpy.close()
            return None
        wid = int(prop.value[0])
        client = dpy.create_resource_object("window", wid)
        geometry = client.get_geometry()
        translated = root.translate_coords(client, 0, 0)
        result = (wid, (int(translated.x), int(translated.y), int(geometry.width), int(geometry.height)))
        dpy.close()
        return result
    except Exception:
        return None


def window_snapshot() -> tuple[int, tuple[int, int, int, int]] | None:
    if platform.system() == "Windows":
        return _windows_snapshot()
    return _x11_snapshot()


def primary_screen_bounds() -> tuple[int, int, int, int]:
    """Return the primary monitor bounds without assuming the virtual desktop."""
    if platform.system() != "Windows" and os.environ.get("DISPLAY"):
        try:
            output = subprocess.run(["xrandr", "--query"], capture_output=True, text=True,
                                    timeout=1, check=False).stdout
            match = re.search(r"^\S+ connected primary .*?(\d+x\d+\+[-\d]+\+[-\d]+)", output, re.MULTILINE)
            if match:
                geometry = re.match(r"(\d+)x(\d+)\+([-\d]+)\+([-\d]+)", match.group(1))
                if geometry:
                    w, h, x, y = map(int, geometry.groups())
                    return x, y, w, h
        except Exception:
            pass
    try:
        import mss
        with mss.mss() as capture:
            monitors = capture.monitors
            if len(monitors) > 1:
                monitor = monitors[1]
                return tuple(int(monitor[k]) for k in ("left", "top", "width", "height"))
    except Exception:
        pass
    if platform.system() == "Windows":
        user32 = ctypes.windll.user32
        return (0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    raise RuntimeError("could not determine the primary monitor bounds")


def set_dpi_awareness() -> None:
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _set_fast_input_timing() -> None:
    global _input_pause_before_run
    try:
        pyautogui = _load_pyautogui()
        _input_pause_before_run = getattr(pyautogui, "PAUSE", None)
        pyautogui.PAUSE = 0.01
    except Exception:
        _input_pause_before_run = None


def _restore_input_timing() -> None:
    global _input_pause_before_run
    if _input_pause_before_run is None:
        return
    try:
        _load_pyautogui().PAUSE = _input_pause_before_run
    except Exception:
        pass
    _input_pause_before_run = None


def register_stop(callback: Callable[[], None]) -> Callable[[], None]:
    """Register F8 globally and return an idempotent cleanup callback."""
    if platform.system() == "Windows":
        return _register_windows_stop(callback)
    return _register_x11_stop(callback)


def _register_windows_stop(callback):
    import ctypes.wintypes
    user32 = ctypes.windll.user32
    # RegisterHotKey is process-global; a private worker owns its message loop.
    ready = threading.Event()
    error: list[BaseException] = []
    stop = threading.Event()
    hotkey_id = 0xA0F8
    worker_tid = [0]

    def loop():
        worker_tid[0] = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, hotkey_id, 0, 0x77):
            error.append(OSError("RegisterHotKey(F8) failed"))
            ready.set()
            return
        ready.set()
        msg = ctypes.wintypes.MSG()
        while not stop.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam == hotkey_id:
                callback()
        user32.UnregisterHotKey(None, hotkey_id)

    worker = threading.Thread(target=loop, name="roblox-f8", daemon=True)
    worker.start()
    if not ready.wait(1.0):
        stop.set()
        if worker_tid[0]:
            user32.PostThreadMessageW(worker_tid[0], 0x0012, 0, 0)
        worker.join(1.0)
        raise OSError("RegisterHotKey(F8) worker did not become ready")
    if not worker.is_alive() and not error:
        raise OSError("RegisterHotKey(F8) worker exited during setup")
    if error:
        raise error[0]

    def cleanup():
        if stop.is_set():
            return
        stop.set()
        if worker_tid[0]:
            user32.PostThreadMessageW(worker_tid[0], 0x0012, 0, 0)
        worker.join(1.0)
    return cleanup


def _register_x11_stop(callback):
    if (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") == "wayland") or not os.environ.get("DISPLAY"):
        raise RuntimeError("F8 requires an X11 DISPLAY; Wayland is unsupported")
    dpy = None
    try:
        from Xlib import X, XK, display
        from Xlib.error import BadAccess, CatchError
        dpy = display.Display()
        root = dpy.screen().root
        keycode = dpy.keysym_to_keycode(XK.string_to_keysym("F8"))
        if not keycode:
            raise RuntimeError("X11 could not resolve F8")
        masks = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
        catcher = CatchError(BadAccess)
        dpy.set_error_handler(catcher)
        for mask in masks:
            root.grab_key(keycode, mask, True, X.GrabModeAsync, X.GrabModeAsync)
        dpy.sync()
        dpy.set_error_handler(None)
        if catcher.get_error() is not None:
            raise RuntimeError("F8 is already registered by another application")
    except BadAccess as exc:
        raise RuntimeError("F8 is already registered by another application") from exc
    except Exception:
        try:
            dpy.close()
        except Exception:
            pass
        raise
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                event = dpy.next_event()
                if event.type == X.KeyPress and event.detail == keycode:
                    callback()
            except Exception:
                break

    worker = threading.Thread(target=loop, name="roblox-f8", daemon=True)
    worker.start()

    def cleanup():
        if stop.is_set():
            return
        stop.set()
        for mask in masks:
            try:
                root.ungrab_key(keycode, mask)
            except Exception:
                pass
        try:
            dpy.sync()
            dpy.close()
        except Exception:
            pass
    return cleanup


def save_settings(settings: dict[str, Any], path: Path = SETTINGS_FILE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_settings(path: Path = SETTINGS_FILE) -> dict[str, Any]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


class ScreenCapture:
    def __init__(self):
        import mss
        self._capture = mss.mss()

    def grab(self, roi: tuple[int, int, int, int]):
        if np is None:
            raise RuntimeError("numpy is required for capture")
        x, y, width, height = roi
        shot = self._capture.grab({"left": x, "top": y, "width": width, "height": height})
        return np.asarray(shot)[:, :, :3]

    def close(self):
        self._capture.close()


class FishingApp:
    """Small Tkinter controller with frozen-screen ROI selection."""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.root.title(f"Roblox Auto Fishing {APP_VERSION}")
        self.root.resizable(False, False)
        self.settings = load_settings()
        self.target = None
        self.running = False
        self.controller = None
        self.capture = None
        self.hotkey_cleanup = None
        self.stop_requested = threading.Event()
        self.start_pending = False
        self.pending_start_id = None
        self.test_hold_pending = False
        self.test_hold_active = False
        self.test_hold_id = None
        self.area_dialog = None
        self.template = None
        self.fps_count = 0
        self.fps_started = time.monotonic()
        mode = self.settings.get("fishing_mode", "rod")
        if mode not in MODE_CONFIGS:
            mode = "rod"
        self.current_mode = mode
        self.vars = {
            "fishing_mode": tk.StringVar(value=mode),
            "cast_seconds": tk.StringVar(value=str(self.settings.get("cast_seconds", DEFAULT_SETTINGS["cast_seconds"]))),
            "margin": tk.StringVar(value=str(self.settings.get("margin", DEFAULT_SETTINGS["margin"]))),
            "lead": tk.StringVar(value=str(self.settings.get("lead", DEFAULT_SETTINGS["lead"]))),
            "threshold": tk.StringVar(value=str(self.settings.get("bite_threshold", DEFAULT_SETTINGS["bite_threshold"]))),
            "right": tk.BooleanVar(value=bool(self.settings.get("right_on_hold", DEFAULT_SETTINGS["right_on_hold"]))),
            "ack": tk.BooleanVar(value=bool(self.settings.get("bite_ack", DEFAULT_SETTINGS["bite_ack"]))),
        }
        self.status = tk.StringVar(value="พร้อม — เลือกหน้าต่างเกมและพื้นที่ตรวจจับ")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _on_mode_change(self):
        if self.running or getattr(self, "start_pending", False):
            self.vars["fishing_mode"].set(self.current_mode)
            return
        new_mode = self.vars["fishing_mode"].get()
        if new_mode in MODE_CONFIGS:
            self.current_mode = new_mode

    def _set_mode_widgets_state(self, state: str):
        if hasattr(self, "mode_rod_rb") and hasattr(self, "mode_net_rb"):
            try:
                self.mode_rod_rb.configure(state=state)
                self.mode_net_rb.configure(state=state)
            except Exception:
                pass

    def _build(self):
        self.root.title(f"ตกปลาอัตโนมัติ Roblox — เวอร์ชัน {APP_VERSION}")
        self.root.geometry("720x860")
        self.root.minsize(720, 860)
        self.root.maxsize(760, 1050)
        self.advanced_visible = False
        self.ui_font = "Noto Sans Thai Looped"
        try:
            families = set(self.root.tk.call("font", "families"))
            if self.ui_font not in families:
                self.ui_font = "Noto Sans Thai"
        except Exception:
            self.ui_font = "TkDefaultFont"
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#f7f4ff")
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#ded5f3", borderwidth=1, relief="groove")
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#4b3b76", font=(self.ui_font, 11, "bold"))
        style.configure("TLabel", background="#f7f4ff", foreground="#30283d", font=(self.ui_font, 11))
        style.configure("Card.TLabel", background="#ffffff", foreground="#30283d", font=(self.ui_font, 11))
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TCheckbutton", background="#ffffff", font=(self.ui_font, 11))
        style.configure("TRadiobutton", background="#ffffff", font=(self.ui_font, 11))
        style.configure("Title.TLabel", background="#f7f4ff", foreground="#3d2c68", font=(self.ui_font, 18, "bold"))
        style.configure("Subtitle.TLabel", background="#f7f4ff", foreground="#6b607d", font=(self.ui_font, 10))
        style.configure("Start.TButton", font=(self.ui_font, 13, "bold"), foreground="#ffffff", background="#7452b8", padding=(18, 9))
        style.configure("Stop.TButton", font=(self.ui_font, 12, "bold"), foreground="#ffffff", background="#b44164", padding=(18, 9))
        style.configure("Step.TButton", font=(self.ui_font, 10, "bold"), padding=(12, 4))
        footer = self.ttk.Frame(self.root, style="TFrame", padding=(16, 4, 16, 8))
        footer.pack(side="bottom", fill="x")
        self.ttk.Button(footer, text="เริ่มตกปลา", style="Start.TButton", command=self.start).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.ttk.Button(footer, text="หยุด  (F8)", style="Stop.TButton", command=self.stop).pack(side="left", fill="x", expand=True, padx=(6, 0))
        outer = self.ttk.Frame(self.root, padding=(16, 8, 16, 6))
        outer.pack(fill="both", expand=True)
        self.ttk.Label(outer, text="ตกปลาอัตโนมัติ Roblox", style="Title.TLabel").pack(anchor="w")
        self.ttk.Label(outer, text="ตั้งค่าให้ครบทีละขั้น แล้วเริ่มทำงานเมื่อเกมอยู่ด้านหน้า", style="Subtitle.TLabel").pack(anchor="w", pady=(0, 6))

        step1 = self.ttk.LabelFrame(outer, text=" 1  เลือกหน้าต่างเกม ", style="Card.TLabelframe", padding=(10, 6))
        step1.pack(fill="x", pady=2)
        row = self.ttk.Frame(step1, style="Card.TFrame")
        row.pack(fill="x")
        self.ttk.Button(row, text="เลือกหน้าต่างเกม", style="Step.TButton", command=self.select_window).pack(side="left")
        self.target_summary = self.ttk.Label(row, text="ยังไม่เลือกเกม", style="Card.TLabel")
        self.target_summary.pack(side="left", padx=12)
        self.ttk.Label(step1, text="สลับไปที่เกมภายใน 3 วินาทีหลังจากกดปุ่ม", style="Card.TLabel").pack(anchor="w", pady=(4, 0))

        step2 = self.ttk.LabelFrame(outer, text=" 2  เลือกพื้นที่ตรวจจับ ", style="Card.TLabelframe", padding=(10, 6))
        step2.pack(fill="x", pady=2)
        row = self.ttk.Frame(step2, style="Card.TFrame")
        row.pack(fill="x")
        self.ttk.Button(row, text="เลือกพื้นที่บนภาพเกม", style="Step.TButton", command=self.select_rois).pack(side="left")
        self.roi_summary = self.ttk.Label(row, text="ยังไม่กำหนดแถบมินิเกม", style="Card.TLabel")
        self.roi_summary.pack(side="left", padx=12)
        self.ttk.Label(step2, text="จำเป็นต้องกำหนดทั้งแถบมินิเกมและพื้นที่สัญญาณปลากินเบ็ด รวมถึง bite-template.png", style="Card.TLabel").pack(anchor="w", pady=(4, 0))

        step3 = self.ttk.LabelFrame(outer, text=" 3  ทดลองก่อนเริ่ม ", style="Card.TLabelframe", padding=(10, 6))
        step3.pack(fill="x", pady=2)
        actions = self.ttk.Frame(step3, style="Card.TFrame")
        actions.pack(fill="x")
        self.ttk.Button(actions, text="ตรวจจับแถบ", style="Step.TButton", command=self.preview).pack(side="left")
        self.ttk.Button(actions, text="ทดลองกดค้าง 0.1 วินาที", style="Step.TButton", command=self.test_hold).pack(side="left", padx=(8, 0))
        self.ttk.Label(step3, text="ตรวจภาพก่อน แล้วค่อยทดลองกดค้าง • มีเวลา 3 วินาทีให้กลับเกม", style="Card.TLabel", wraplength=660, justify="left").pack(anchor="w", pady=(5, 0))

        step4 = self.ttk.LabelFrame(outer, text=" 4  ตั้งค่าและเริ่มทำงาน ", style="Card.TLabelframe", padding=(10, 6))
        step4.pack(fill="x", pady=2)

        mode_frame = self.ttk.Frame(step4, style="Card.TFrame")
        mode_frame.pack(fill="x", pady=(0, 3))
        self.ttk.Label(mode_frame, text="ประเภทอุปกรณ์ตกปลา", style="Card.TLabel", font=(self.ui_font, 11, "bold")).pack(anchor="w", pady=(0, 2))
        self.mode_rod_rb = self.ttk.Radiobutton(
            mode_frame,
            text=MODE_CONFIGS["rod"]["description"],
            variable=self.vars["fishing_mode"],
            value="rod",
            command=self._on_mode_change,
        )
        self.mode_rod_rb.pack(anchor="w", padx=4, pady=1)
        self.mode_net_rb = self.ttk.Radiobutton(
            mode_frame,
            text=MODE_CONFIGS["net"]["description"],
            variable=self.vars["fishing_mode"],
            value="net",
            command=self._on_mode_change,
        )
        self.mode_net_rb.pack(anchor="w", padx=4, pady=1)

        self.ttk.Separator(step4, orient="horizontal").pack(fill="x", pady=(3, 6))

        tuning = self.ttk.Frame(step4, style="Card.TFrame")
        tuning.pack(fill="x")
        self.ttk.Label(tuning, text="เวลากดค้างตอนเหวี่ยงเบ็ด (วินาที)", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        cast_box = self.ttk.Combobox(tuning, textvariable=self.vars["cast_seconds"], values=("0.5", "0.8", "1.0", "1.5", "2.0", "3.0"), width=7)
        cast_box.grid(row=0, column=1, sticky="w", padx=(8, 18))
        self.ttk.Label(tuning, text="เมื่อกดเมาส์ค้าง ตัวเลื่อนสีขาวไปทาง", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.ttk.Radiobutton(tuning, text="ขวา", variable=self.vars["right"], value=True).grid(row=1, column=1, sticky="w", padx=8, pady=(5, 0))
        self.ttk.Radiobutton(tuning, text="ซ้าย", variable=self.vars["right"], value=False).grid(row=1, column=2, sticky="w", pady=(5, 0))
        self.advanced_button = self.ttk.Button(step4, text="ตั้งค่าละเอียด  ▸", command=self.toggle_advanced)
        self.advanced_button.pack(anchor="w", pady=(6, 0))
        self.advanced_frame = self.ttk.Frame(step4, style="Card.TFrame")
        self.ttk.Label(self.advanced_frame, text="ระยะเผื่อกลางช่องม่วง (พิกเซล)", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        self.ttk.Entry(self.advanced_frame, textvariable=self.vars["margin"], width=8).grid(row=0, column=1, padx=8)
        self.ttk.Label(self.advanced_frame, text="ชดเชยการเคลื่อนที่ (วินาที)", style="Card.TLabel").grid(row=0, column=2, sticky="w", pady=2)
        self.ttk.Entry(self.advanced_frame, textvariable=self.vars["lead"], width=8).grid(row=0, column=3, padx=8)
        self.ttk.Label(self.advanced_frame, text="ความเข้มงวดสัญญาณ (0–1)", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.ttk.Entry(self.advanced_frame, textvariable=self.vars["threshold"], width=8).grid(row=1, column=1, padx=8)
        self.ttk.Checkbutton(self.advanced_frame, text="คลิกเมื่อปลากินเบ็ด", variable=self.vars["ack"]).grid(row=1, column=2, columnspan=2, sticky="w")

        status_frame = self.ttk.LabelFrame(outer, text=" สถานะ ", style="Card.TLabelframe", padding=(10, 6))
        status_frame.pack(fill="x", pady=3)
        self.ttk.Label(status_frame, textvariable=self.status, style="Card.TLabel", wraplength=660, justify="left").pack(anchor="w", fill="x")
        self.ttk.Label(status_frame, text="กด F8 เพื่อหยุดระหว่างทำงาน", style="Card.TLabel").pack(anchor="w", pady=(5, 0))

        self._update_readiness()

    def toggle_advanced(self):
        if self.advanced_visible:
            self.advanced_frame.pack_forget()
            self.advanced_button.configure(text="ตั้งค่าละเอียด  ▸")
        else:
            self.advanced_frame.pack(fill="x", pady=(4, 0))
            self.advanced_button.configure(text="ตั้งค่าละเอียด  ▾")
        self.advanced_visible = not self.advanced_visible
        height = 920 if self.advanced_visible else 860
        self.root.minsize(720, height)
        self.root.geometry(f"720x{height}")

    def _set_status(self, text):
        state_names = {"Idle": "พร้อม", "Cast": "เหวี่ยงเบ็ด", "Wait": "รอปลา",
                       "Track": "คุมแถบ", "End": "รอยืนยันจบรอบ", "Paused": "พักการทำงาน"}
        reason_names = {
            "tracking": "กำลังอ่านภาพเกม",
            "stopped by user": "หยุดโดยผู้ใช้",
            "focus lost; press Start to resume": "ไม่พบโฟกัสเกม กดเริ่มใหม่เมื่อพร้อม",
            "screen capture failed": "อ่านภาพหน้าจอไม่สำเร็จ",
            "bar detection is ambiguous": "พบแถบหลายตำแหน่ง จึงหยุดเพื่อความปลอดภัย",
            "target window moved or pointer left it": "หน้าต่างเกมย้ายตำแหน่งหรือตัวชี้ออกนอกพื้นที่",
        }
        display = str(text)
        if display in reason_names:
            display = reason_names[display]
        elif display.startswith("Start blocked:"):
            display = "เริ่มไม่ได้: " + display.split(":", 1)[1].strip()
        elif display.startswith("Preview failed:"):
            display = "ดูตัวอย่างไม่ได้: " + display.split(":", 1)[1].strip()
        elif display.startswith("ROI capture failed:"):
            display = "จับภาพพื้นที่ไม่ได้: " + display.split(":", 1)[1].strip()
        elif display.startswith("error:"):
            display = "เกิดข้อผิดพลาด: " + display.split(":", 1)[1].strip()
        else:
            state, separator, detail = display.partition(":")
            if state in state_names:
                detail = detail.strip() if separator else "กำลังทำงาน"
                if "|" in detail:
                    reason, fps = detail.split("|", 1)
                    detail = reason_names.get(reason.strip(), reason.strip()) + " ·" + fps
                else:
                    detail = reason_names.get(detail, detail)
                mode = getattr(self, "current_mode", "rod")
                mode_name = MODE_CONFIGS.get(mode, MODE_CONFIGS["rod"])["name"]
                display = f"โหมด{mode_name}  ·  {state_names[state]}  ·  {detail}"
        self.status.set(display)
        self._update_readiness()

    def _update_readiness(self):
        if not hasattr(self, "target_summary"):
            return
        if self.target:
            bounds = self.target[1]
            self.target_summary.configure(text=f"เลือกแล้ว  •  {bounds[2]} × {bounds[3]} px")
        else:
            self.target_summary.configure(text="ยังไม่เลือกเกม")
        rois = self.settings.get("rois", {}) if isinstance(self.settings, dict) else {}
        bar_ready = bool(rois.get("bar"))
        bite_ready = bool(rois.get("bite"))
        template_ready = TEMPLATE_FILE.exists()
        if bar_ready and bite_ready and template_ready:
            parts = ["พร้อมเริ่มทำงาน"]
            if rois.get("cast"):
                parts.append("แถบเหวี่ยง")
            self.roi_summary.configure(text="  •  ".join(parts))
        elif bar_ready:
            missing = []
            if not bite_ready:
                missing.append("ขาด ROI bite")
            if not template_ready:
                missing.append("ขาด bite-template.png")
            self.roi_summary.configure(text="ยังไม่พร้อม  •  " + "  •  ".join(missing))
        else:
            self.roi_summary.configure(text="ยังไม่กำหนดแถบมินิเกม")

    def _read_ui(self):
        return {
            "fishing_mode": str(self.vars["fishing_mode"].get()),
            "cast_seconds": float(self.vars["cast_seconds"].get()),
            "margin": float(self.vars["margin"].get()),
            "lead": float(self.vars["lead"].get()),
            "bite_threshold": float(self.vars["threshold"].get()),
            "right_on_hold": bool(self.vars["right"].get()),
            "bite_ack": bool(self.vars["ack"].get()),
            "rois": self.settings.get("rois", {}),
        }

    def select_window(self):
        if self.running or self.start_pending or self.test_hold_pending or self.test_hold_active:
            self._set_status("หยุดการทำงานก่อนเปลี่ยนหน้าต่างเกม")
            return
        self._set_status("โฟกัสหน้าต่างเกม — จะบันทึกภาพใน 3 วินาที")
        self.root.after(3000, self._finish_window_select)

    def _finish_window_select(self):
        self.target = window_snapshot()
        helper_id = int(self.root.winfo_id())
        helper_bounds = None
        try:
            helper_bounds = (int(self.root.winfo_rootx()), int(self.root.winfo_rooty()),
                             int(self.root.winfo_width()), int(self.root.winfo_height()))
        except Exception:
            pass
        same_bounds = helper_bounds and self.target and self.target[1] == helper_bounds
        if self.target and (self.target[0] == helper_id or same_bounds):
            self.target = None
        self._set_status("เลือกหน้าต่างเกมแล้ว" if self.target else "กรุณาโฟกัสเกม ไม่ใช่หน้าต่างช่วยเหลือนี้")

    def select_rois(self):
        if self.running or self.start_pending or self.test_hold_pending or self.test_hold_active:
            self._set_status("หยุดการทำงานก่อนเปลี่ยนพื้นที่ตรวจจับ")
            return
        if self.area_dialog and self.area_dialog.winfo_exists():
            self.area_dialog.lift()
            self.area_dialog.focus_set()
            return
        dialog = self.tk.Toplevel(self.root)
        self.area_dialog = dialog
        dialog.title("เลือกพื้นที่ทีละส่วน")
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self._close_area_dialog)
        dialog.lift()
        dialog.focus_set()
        self.ttk.Label(dialog, text="หน้าต่างนี้จะซ่อนก่อนจับภาพใหม่ทุกครั้ง", style="Card.TLabel").pack(padx=12, pady=(12, 6))
        for name, label in (("bar", "พื้นที่แถบมินิเกม (จำเป็น)"), ("cast", "พื้นที่แถบเหวี่ยงเบ็ด (ตัวเลือกเสริม)"),
                            ("bite", "พื้นที่ค้นหาสัญญาณปลากินเบ็ด (ตัวเลือกเสริม)"), ("template", "ครอบภาพตัวอย่างสัญญาณ (ตัวเลือกเสริม)")):
            self.ttk.Button(dialog, text=label, command=lambda n=name: self._begin_area_selection(n)).pack(fill="x", padx=12, pady=3)
        self.ttk.Button(dialog, text="ปิด", command=self._close_area_dialog).pack(pady=(6, 12))

    def _close_area_dialog(self):
        dialog, self.area_dialog = self.area_dialog, None
        if dialog and dialog.winfo_exists():
            dialog.destroy()

    def _begin_area_selection(self, name):
        self._close_area_dialog()
        self.root.withdraw()
        area_names = {"bar": "แถบมินิเกม", "cast": "แถบเหวี่ยงเบ็ด", "bite": "สัญญาณปลากินเบ็ด", "template": "ภาพตัวอย่างสัญญาณ"}
        self._set_status(f"กำลังเตรียมจับภาพ {area_names.get(name, name)}…")
        self.root.after(700, lambda: self._capture_area(name))

    def _capture_area(self, name):
        area_names = {"bar": "แถบมินิเกม", "cast": "แถบเหวี่ยงเบ็ด", "bite": "สัญญาณปลากินเบ็ด", "template": "ภาพตัวอย่างสัญญาณ"}
        try:
            bounds = primary_screen_bounds()
            capture = ScreenCapture()
            image = capture.grab(bounds)
            capture.close()
        except Exception as exc:
            self.root.deiconify()
            self._set_status(f"จับภาพพื้นที่ไม่ได้: {exc}")
            return
        self.root.deiconify()
        picker = self.tk.Toplevel(self.root)
        picker.title(f"ลากกรอบพื้นที่ {area_names.get(name, name)}")
        self.ttk.Label(picker, text="ลากกรอบให้ครอบเฉพาะส่วนที่ต้องการ แล้วปล่อยเมาส์", style="Card.TLabel").pack(anchor="w", padx=10, pady=(8, 4))
        max_width, max_height = 1100, 700
        scale = min(1.0, max_width / image.shape[1], max_height / image.shape[0])
        canvas = self.tk.Canvas(picker, width=int(image.shape[1] * scale), height=int(image.shape[0] * scale), cursor="crosshair")
        canvas.pack()
        try:
            from PIL import Image, ImageTk
            photo = ImageTk.PhotoImage(Image.fromarray(image[:, :, ::-1]).resize((canvas.winfo_reqwidth(), canvas.winfo_reqheight())))
            canvas.create_image(0, 0, image=photo, anchor="nw")
            canvas._photo = photo
        except Exception:
            canvas.configure(background="#555")
        start = [None]
        def down(event):
            start[0] = (event.x, event.y)
        def up(event):
            if start[0] is None:
                return
            x0, y0 = start[0]
            x1, y1 = event.x, event.y
            x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
            if w < 2 or h < 2:
                return
            canvas.create_rectangle(x, y, x + w, y + h, outline="red", width=2)
            area = [bounds[0] + int(x / scale), bounds[1] + int(y / scale), int(w / scale), int(h / scale)]
            if name == "template":
                try:
                    crop = image[int(y / scale):int(y / scale) + area[3], int(x / scale):int(x / scale) + area[2]]
                    if cv2 is not None:
                        TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(TEMPLATE_FILE), crop)
                except Exception:
                    pass
                self._set_status("บันทึกภาพตัวอย่างสัญญาณปลากินเบ็ดแล้ว")
            else:
                self.settings.setdefault("rois", {})[name] = area
                self._set_status(f"เลือกพื้นที่ {area_names.get(name, name)} แล้ว")
            picker.after(250, picker.destroy)
        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<ButtonRelease-1>", up)
        self.ttk.Button(picker, text="ยกเลิก", command=picker.destroy).pack(anchor="e", padx=10, pady=(4, 8))

    def _settings(self):
        return validate_settings(self._read_ui(), primary_screen_bounds())

    def preview(self):
        if self.running or self.start_pending or self.test_hold_pending or self.test_hold_active:
            self._set_status("หยุดการทำงานก่อนตรวจจับแถบ")
            return
        try:
            settings = self._settings()
            cap = ScreenCapture()
            result = detect_bar(cap.grab(settings["rois"]["bar"]))
            cap.close()
            labels = {"valid": "พบแถบพร้อมติดตาม", "absent": "ยังไม่พบแถบ", "ambiguous": "ภาพไม่ชัดเจน"}
            self._set_status(f"ผลตรวจจับ: {labels.get(result[0], result[0])} • ตัวเลื่อน {result[1]} • ช่องเป้าหมาย {result[2]}")
        except Exception as exc:
            self._set_status(f"ตรวจจับแถบไม่ได้: {exc}")

    def test_hold(self):
        if self.running or self.start_pending or self.test_hold_pending or self.test_hold_active:
            self._set_status("หยุดการทำงานก่อนทดลองกดค้าง")
            return
        if not self.target:
            self._set_status("ทดลองกดค้างไม่ได้: ต้องโฟกัสเกมและวางตัวชี้ไว้ในหน้าต่างเกม")
            return
        try:
            self.stop_requested.clear()
            self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self.test_hold_pending = True
            self._set_status("สลับไปที่เกมภายใน 3 วินาทีเพื่อทดลองกดค้าง")
            self.test_hold_id = self.root.after(3000, self._begin_test_hold)
        except Exception as exc:
            self.test_hold_pending = False
            release_mouse()
            self._set_status(f"ทดลองกดค้างไม่ได้: {exc}")

    def _begin_test_hold(self):
        if not self.test_hold_pending:
            return
        self.test_hold_pending = False
        self.test_hold_id = None
        if self.stop_requested.is_set() or window_snapshot() != self.target:
            self.stop("หยุดฉุกเฉินด้วย F8" if self.stop_requested.is_set() else "หยุดการทำงาน")
            return
        if not apply_action("hold", self.target):
            self.stop("ทดลองกดค้างไม่ได้: โฟกัสหรือตัวชี้เปลี่ยนตำแหน่ง")
            return
        self.test_hold_active = True
        self._set_status("กำลังกดค้าง 0.1 วินาที")
        self.test_hold_id = self.root.after(100, self._finish_test_hold)

    def _finish_test_hold(self):
        self.test_hold_active = False
        self.test_hold_id = None
        release_mouse()
        if self.hotkey_cleanup:
            self.hotkey_cleanup()
            self.hotkey_cleanup = None
        self._set_status("หยุดฉุกเฉินด้วย F8" if self.stop_requested.is_set() else "ทดลองกดค้างเสร็จแล้ว")

    def start(self):
        if (self.running or getattr(self, "start_pending", False)
                or getattr(self, "test_hold_pending", False)
                or getattr(self, "test_hold_active", False)):
            return
        try:
            if not self.target:
                raise RuntimeError("กรุณาเลือกหน้าต่างเกมที่โฟกัสอยู่ก่อน")
            settings = self._settings()
            if not settings["rois"].get("bite"):
                raise RuntimeError("จำเป็นต้องกำหนดพื้นที่สัญญาณปลากินเบ็ด (ROI bite)")
            template_path = TEMPLATE_FILE
            if not template_path.exists():
                raise RuntimeError("ไม่พบไฟล์ bite-template.png")
            self.start_pending = True
            self._set_mode_widgets_state("disabled")
            self._set_status("สลับไปที่เกมภายใน 3 วินาทีเพื่อเริ่มทำงาน")
            self.pending_start_id = self.root.after(3000, lambda: self._start_now(settings))
        except Exception as exc:
            self._set_mode_widgets_state("normal")
            release_mouse()
            self._set_status(f"เริ่มไม่ได้: {exc}")

    def _start_now(self, settings):
        if not getattr(self, "start_pending", False):
            return
        self.start_pending = False
        self.pending_start_id = None
        try:
            target = window_snapshot()
            if not target or target != self.target:
                raise RuntimeError("กรุณาโฟกัสหน้าต่างเกมเดิมที่เลือกไว้")
            settings = validate_settings(settings, target[1])
            self.target = target
            self.stop_requested.clear()
            self.settings = settings
            self.template = None
            template_path = TEMPLATE_FILE
            if not template_path.exists():
                raise RuntimeError("ไม่พบไฟล์ bite-template.png")
            if cv2 is not None:
                self.template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
            if self.template is None:
                raise RuntimeError("ไม่สามารถอ่านไฟล์ bite-template.png ได้")
            if not settings["rois"].get("bite"):
                raise RuntimeError("จำเป็นต้องกำหนดพื้นที่สัญญาณปลากินเบ็ด (ROI bite)")
            bite_roi = settings["rois"]["bite"]
            if (self.template.ndim != 3 or self.template.shape[2] < 3
                    or self.template.shape[0] > bite_roi[3]
                    or self.template.shape[1] > bite_roi[2]
                    or not _has_spatial_variation(self.template)):
                raise RuntimeError("ภาพตัวอย่างสัญญาณต้องมีรายละเอียดและขนาดไม่เกินพื้นที่สัญญาณปลากินเบ็ด")
            save_settings(settings)
            self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self.controller = Controller(settings)
            self.controller.start(time.monotonic())
            self.capture = ScreenCapture()
            _set_fast_input_timing()
            self.running = True
            self.fps_count, self.fps_started = 0, time.monotonic()
            mode = settings.get("fishing_mode", "rod")
            mode_name = MODE_CONFIGS.get(mode, MODE_CONFIGS["rod"])["name"]
            self._set_status(f"กำลังทำงาน — โหมด{mode_name} — กด F8 เพื่อหยุดทันที")
            self.root.after(0, self._pump)
        except Exception as exc:
            self._set_mode_widgets_state("normal")
            release_mouse()
            if self.hotkey_cleanup:
                self.hotkey_cleanup()
                self.hotkey_cleanup = None
            self._set_status(f"เริ่มไม่ได้: {exc}")

    def _pump(self):
        if not self.running:
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return
        now = time.monotonic()
        try:
            if window_snapshot() != self.target:
                observation = None
                focused = False
            else:
                bar = detect_bar(self.capture.grab(self.settings["rois"]["bar"]))
                bite = False
                if self.template is not None and self.settings["rois"].get("bite"):
                    bite = detect_bite(self.capture.grab(self.settings["rois"]["bite"]), self.template,
                                       self.settings["bite_threshold"])
                observation = {"bar": bar, "bite": bite}
                self.fps_count += 1
                focused = True
            if self.stop_requested.is_set():
                self.stop("หยุดฉุกเฉินด้วย F8")
                return
            action = self.controller.step(now, observation, focused)
            if not apply_action(action, self.target):
                self.controller.stop("target window moved or pointer left it")
            elapsed = now - self.fps_started
            fps = self.fps_count / elapsed if elapsed > 0 else 0.0
            self._set_status(f"{self.controller.state}: {self.controller.reason or 'tracking'} | {fps:.1f} FPS")
            if self.controller.state == "Paused":
                self.stop()
                return
        except Exception as exc:
            self.controller.stop(f"error: {exc}")
            release_mouse()
            self._set_status(f"พักการทำงาน: {exc}")
            self.stop()
            return
        self.root.after(33, self._pump)

    def stop(self, reason: str | None = None):
        cancelled = False
        if getattr(self, "start_pending", False):
            cancelled = True
            self.start_pending = False
            if getattr(self, "pending_start_id", None) is not None:
                try:
                    self.root.after_cancel(self.pending_start_id)
                except Exception:
                    pass
            self.pending_start_id = None
        if getattr(self, "test_hold_pending", False) or getattr(self, "test_hold_active", False):
            cancelled = True
            self.test_hold_pending = False
            self.test_hold_active = False
            if getattr(self, "test_hold_id", None) is not None:
                try:
                    self.root.after_cancel(self.test_hold_id)
                except Exception:
                    pass
            self.test_hold_id = None
        was_running = self.running
        self.running = False
        self._set_mode_widgets_state("normal")
        controller = getattr(self, "controller", None)
        previous_reason = controller.reason if controller else ""
        if controller:
            controller.stop(reason or previous_reason or "stopped by user")
        release_mouse()
        _restore_input_timing()
        capture = getattr(self, "capture", None)
        if capture:
            capture.close()
            self.capture = None
        hotkey_cleanup = getattr(self, "hotkey_cleanup", None)
        if hotkey_cleanup:
            hotkey_cleanup()
            self.hotkey_cleanup = None
        if was_running or reason or cancelled:
            self._set_status(reason or previous_reason or "หยุดการทำงานแล้ว")

    def close(self):
        self.stop()
        self.root.destroy()


def replay_frames(directory: str | os.PathLike[str]) -> list[tuple[str, float | None, tuple[float, float] | None]]:
    """Analyze PNG/JPEG frames without capturing or sending input."""
    if cv2 is None:
        raise RuntimeError("opencv-python is required for replay mode")
    results = []
    for path in sorted(Path(directory).glob("*")):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
            continue
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if frame is not None:
            results.append(detect_bar(frame))
    return results


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Screen based Roblox fishing helper")
    parser.add_argument("--replay", metavar="DIR", help="analyze saved frames without mouse input")
    args = parser.parse_args()
    if args.replay:
        for index, result in enumerate(replay_frames(args.replay), 1):
            print(f"{index}: state=vision-{result[0]} marker={result[1]} target={result[2]}")
        return
    set_dpi_awareness()
    try:
        import tkinter as tk
    except ImportError as exc:
        raise SystemExit("Tkinter is required to run the control window") from exc
    root = tk.Tk()
    FishingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
