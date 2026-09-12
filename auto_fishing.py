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
APP_VERSION = "0.0.3"


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
    "debug": False,
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
    for key in ("right_on_hold", "bite_ack", "debug"):
        if key in result and not isinstance(result.get(key), bool):
            raise ValueError(f"ค่า {key} ต้องเป็น true หรือ false")
    rois = dict(DEFAULT_SETTINGS["rois"])
    rois.update(data.get("rois") or {})
    if rois.get("bar") is None:
        raise ValueError("ต้องกำหนดพื้นที่แถบมินิเกม")
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

    Tolerates transparency, varying brightness, small target sizes, and partial
    occlusion caused by the white marker overlapping the purple target.
    """
    if (cv2 is None or np is None or not isinstance(bgr, np.ndarray)
            or bgr.ndim != 3 or bgr.shape[2] < 3 or not bgr.size):
        return "absent", None, None
    image = np.ascontiguousarray(bgr[:, :, :3], dtype=np.uint8)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    purple = cv2.inRange(hsv, (110, 25, 40), (168, 255, 255))
    white = cv2.inRange(hsv, (0, 0, 195), (179, 50, 255))

    def components(mask):
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        return [tuple(map(int, row)) for row in stats[1:]]

    markers = [(x, y, w, h) for x, y, w, h, area in components(white)
               if 6 <= h <= 80 and 3 <= w <= h * 1.2
               and area >= w * h * 0.50]

    raw_targets = [(x, y, w, h) for x, y, w, h, area in components(purple)
                   if 4 <= h <= 64 and w >= 3
                   and area >= min(6, w * h * 0.35)]
    if not raw_targets:
        return "absent", None, None

    def aligned(a, b):
        _, y, _, h = a
        _, other_y, _, other_h = b
        return (0.4 <= h / other_h <= 2.5
                and abs(y + h / 2.0 - other_y - other_h / 2.0) <= max(3.0, min(h, other_h) * 0.35))

    raw_targets.sort()
    merged = []
    for target in raw_targets:
        x, y, w, h = target
        joined = False
        for index, previous in enumerate(merged):
            px, py, pw, ph = previous
            gap_left, gap_right = px + pw, x
            if aligned(previous, target):
                if 0 <= gap_right - gap_left <= 4:
                    top, bottom = min(py, y), max(py + ph, y + h)
                    merged[index] = (px, top, x + w - px, bottom - top)
                    joined = True
                    break
                elif 0 <= gap_right - gap_left <= max(h, ph) * 2.0:
                    covering = any(aligned(marker, target) and mx <= gap_left + 3
                                   and mx + mw >= gap_right - 3
                                   for marker in markers for mx, my, mw, mh in [marker])
                    if covering:
                        top, bottom = min(py, y), max(py + ph, y + h)
                        merged[index] = (px, top, x + w - px, bottom - top)
                        joined = True
                        break
        if not joined:
            merged.append(target)

    valid_targets = [t for t in merged if t[2] >= 8]
    if not valid_targets:
        return "absent", None, None

    matches = []
    for target in valid_targets:
        x, y, w, h = target
        for marker in markers:
            if aligned(target, marker):
                mx, my, mw, mh = marker
                y_center_diff = abs((y + h / 2.0) - (my + mh / 2.0))
                h_diff = abs(h - mh)
                score = y_center_diff * 2.0 + h_diff
                matches.append((score, (mx + (mw - 1) / 2.0), (float(x), float(x + w))))

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
    target_center = (target[0] + target[1]) / 2.0
    predicted = x + velocity * lead
    error = target_center - predicted

    # Deadband hysteresis around center
    if abs(error) <= margin:
        # If moving fast towards/through the center, brake to prevent overshoot
        if right_on_hold:
            if velocity > 40.0 and predicted >= target_center:
                return False
            elif velocity < -40.0 and predicted <= target_center:
                return True
        else:
            if velocity < -40.0 and predicted <= target_center:
                return False
            elif velocity > 40.0 and predicted >= target_center:
                return True
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
        configured_lead = float(self.settings.get("lead", 0.0))
        self.calibrated_latency = configured_lead if configured_lead > 0.0 else 0.04
        self.last_switch_action = None
        self.last_switch_time = None
        self.last_switch_x = None
        self.telemetry: dict[str, Any] = {}

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
        self.last_switch_action = None
        self.last_switch_time = None
        self.last_switch_x = None
        self.telemetry = {}

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

    def _on_turnaround(self, x: float, target: tuple[float, float]) -> None:
        """Fine-tune calibrated latency based on physical turnaround location relative to target."""
        if getattr(self, "last_switch_action", None) is None:
            return
        configured_lead = float(self.settings.get("lead", 0.0))
        if configured_lead > 0.0:
            return
        target_left, target_right = target
        right_on_hold = bool(self.settings.get("right_on_hold", False))
        if (self.last_switch_action == "release" and right_on_hold) or (self.last_switch_action == "hold" and not right_on_hold):
            if x < target_left:
                self.calibrated_latency = max(0.0, self.calibrated_latency - 0.005)
            elif x > target_right + 2.0:
                self.calibrated_latency = min(0.20, self.calibrated_latency + 0.005)
        elif (self.last_switch_action == "hold" and right_on_hold) or (self.last_switch_action == "release" and not right_on_hold):
            if x > target_right:
                self.calibrated_latency = max(0.0, self.calibrated_latency - 0.005)
            elif x < target_left - 2.0:
                self.calibrated_latency = min(0.20, self.calibrated_latency + 0.005)

    def _track_step(self, now: float, x: float, target: tuple[float, float],
                    capture_time: float | None = None) -> str:
        entering = (self.state != "Track")
        self.state = "Track"
        self.absent_started = None
        self.ambiguous_streak = 0
        t_obs = capture_time if capture_time is not None else now
        frame_age = max(0.0, now - t_obs)

        if not entering and self.previous_x is not None and self.previous_time is not None and t_obs > self.previous_time:
            dt = t_obs - self.previous_time
            if 0.001 < dt < 0.25:
                v_instant = (x - self.previous_x) / dt
                if self.velocity != 0.0 and (v_instant * self.velocity < -200.0):
                    self._on_turnaround(x, target)
                    self.velocity = v_instant
                else:
                    self.velocity = 0.70 * v_instant + 0.30 * self.velocity
            else:
                self.velocity = 0.0
        else:
            self.velocity = 0.0

        self.previous_x = x
        self.previous_time = t_obs

        target_width = target[1] - target[0]
        half_width = target_width / 2.0
        target_center = (target[0] + target[1]) / 2.0

        prop_margin = target_width * 0.18
        configured_margin = float(self.settings.get("margin", 3.0))
        safe_max = max(1.0, half_width * 0.45)
        safe_min = min(1.2, max(0.6, half_width * 0.30))
        margin = min(configured_margin, prop_margin)
        margin = max(safe_min, min(margin, safe_max))

        configured_lead = float(self.settings.get("lead", 0.0))
        lead_delay = configured_lead if configured_lead > 0.0 else self.calibrated_latency
        total_latency = frame_age + lead_delay
        predicted = x + self.velocity * total_latency

        desired = choose_hold(x, target, self.velocity, total_latency, margin,
                              bool(self.settings["right_on_hold"]), self.held)

        action = "none"
        if entering:
            self.held = desired
            action = "hold" if desired else "release"
        elif desired != self.held:
            self.held = desired
            action = "hold" if desired else "release"
            self.last_switch_action = action
            self.last_switch_time = now
            self.last_switch_x = x

        error = target_center - predicted
        self.telemetry = {
            "target_left": round(float(target[0]), 1),
            "target_right": round(float(target[1]), 1),
            "target_center": round(float(target_center), 1),
            "marker_x": round(float(x), 1),
            "predicted_x": round(float(predicted), 1),
            "velocity": round(float(self.velocity), 1),
            "frame_age_ms": round(float(frame_age * 1000.0), 1),
            "total_latency_ms": round(float(total_latency * 1000.0), 1),
            "calibrated_lead_ms": round(float(lead_delay * 1000.0), 1),
            "margin": round(float(margin), 1),
            "error": round(float(error), 1),
            "action": action,
            "held": self.held,
        }

        return action

    def step(self, now: float, observation: dict[str, Any] | None, focused: bool,
             capture_time: float | None = None) -> str:
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
            is_net = (self.settings.get("fishing_mode") == "net")
            if not is_net and self.settings.get("bite_ack", True) and self.bite_streak >= 3 and not self.bite_latched:
                self.bite_latched = True
                return "click"
            if status == "valid" and x is not None and target is not None:
                return self._track_step(now, x, target, capture_time=capture_time)
            return "none"
        if self.state == "End":
            if status == "valid" and x is not None and target is not None:
                return self._track_step(now, x, target, capture_time=capture_time)
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
            return self._track_step(now, x, target, capture_time=capture_time)
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
        mss_cls = getattr(mss, "MSS", getattr(mss, "mss", None))
        with mss_cls() as capture:
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
        pyautogui.PAUSE = 0.0
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
        mss_cls = getattr(mss, "MSS", getattr(mss, "mss", None))
        self._capture = mss_cls()

    def grab(self, roi: tuple[int, int, int, int]):
        if np is None:
            raise RuntimeError("numpy is required for capture")
        x, y, width, height = roi
        shot = self._capture.grab({"left": x, "top": y, "width": width, "height": height})
        return np.asarray(shot)[:, :, :3]

    def close(self):
        self._capture.close()


class FishingApp:
    """Modern dark charcoal Tkinter controller with live preview and frozen-screen ROI selection."""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.root.title(f"Roblox Auto Fishing {APP_VERSION}")
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
        self._preview_photo = None
        self._preview_throttle = 0.0
        self.advanced_visible = False
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
            "debug": tk.BooleanVar(value=bool(self.settings.get("debug", DEFAULT_SETTINGS.get("debug", False)))),
        }
        self.status = tk.StringVar(value="พร้อม — เลือกพื้นที่บนหน้าจอเพื่อเริ่ม")
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
        self.root.title(f"Roblox Auto Fishing — Auto Tracking {APP_VERSION}")
        self.root.geometry("680x880")
        self.root.minsize(580, 760)
        self.root.resizable(True, True)

        # Dark Charcoal Color System
        BG_DARK = "#121118"
        BG_CARD = "#1e1b29"
        BG_CARD_LIGHT = "#272336"
        BORDER_COLOR = "#322c44"
        TEXT_MAIN = "#f3f0fb"
        TEXT_MUTED = "#9d96b0"
        TEXT_HINT = "#6e6784"
        PURPLE_PRIMARY = "#7c3aed"
        PURPLE_HOVER = "#6d28d9"
        PURPLE_LIGHT = "#a78bfa"
        GREEN_READY = "#10b981"
        AMBER_WARN = "#f59e0b"
        RED_DANGER = "#ef4444"

        self.root.configure(background=BG_DARK)

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

        style.configure("TFrame", background=BG_DARK)
        style.configure("Card.TLabelframe", background=BG_CARD, bordercolor=BORDER_COLOR, borderwidth=1, relief="groove")
        style.configure("Card.TLabelframe.Label", background=BG_CARD, foreground=PURPLE_LIGHT, font=(self.ui_font, 11, "bold"))
        style.configure("TLabel", background=BG_DARK, foreground=TEXT_MAIN, font=(self.ui_font, 11))
        style.configure("Card.TLabel", background=BG_CARD, foreground=TEXT_MAIN, font=(self.ui_font, 10))
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure("TCheckbutton", background=BG_CARD, foreground=TEXT_MAIN, font=(self.ui_font, 10))
        style.configure("TRadiobutton", background=BG_CARD, foreground=TEXT_MAIN, font=(self.ui_font, 10))
        style.configure("Title.TLabel", background=BG_DARK, foreground=TEXT_MAIN, font=(self.ui_font, 16, "bold"))
        style.configure("Subtitle.TLabel", background=BG_DARK, foreground=TEXT_MUTED, font=(self.ui_font, 9))
        style.configure("TSeparator", background=BORDER_COLOR)

        style.configure("Primary.TButton", font=(self.ui_font, 11, "bold"), foreground="#ffffff", background=PURPLE_PRIMARY, padding=(16, 9))
        style.map("Primary.TButton", background=[("active", PURPLE_HOVER), ("disabled", "#38304c")], foreground=[("disabled", "#6e6784")])

        style.configure("Danger.TButton", font=(self.ui_font, 11, "bold"), foreground="#ffffff", background="#dc2626", padding=(16, 9))
        style.map("Danger.TButton", background=[("active", "#b91c1c"), ("disabled", "#38304c")])

        style.configure("Secondary.TButton", font=(self.ui_font, 9, "bold"), foreground=TEXT_MAIN, background=BG_CARD_LIGHT, padding=(10, 5))
        style.map("Secondary.TButton", background=[("active", "#36314a")])

        style.configure("Start.TButton", font=(self.ui_font, 11, "bold"), foreground="#ffffff", background=PURPLE_PRIMARY, padding=(16, 9))
        style.configure("Stop.TButton", font=(self.ui_font, 11, "bold"), foreground="#ffffff", background="#dc2626", padding=(16, 9))
        style.configure("Step.TButton", font=(self.ui_font, 9, "bold"), foreground=TEXT_MAIN, background=BG_CARD_LIGHT, padding=(10, 5))

        # Hotkey support
        self.root.bind("<F8>", lambda event: self._toggle_start_stop())

        # Responsive scrollable container for narrow/small window support
        container = self.tk.Frame(self.root, bg=BG_DARK)
        container.pack(fill="both", expand=True)

        canvas = self.tk.Canvas(container, bg=BG_DARK, highlightthickness=0)
        scrollbar = self.ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = self.tk.Frame(canvas, bg=BG_DARK)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        def _on_mousewheel(event):
            if canvas.winfo_exists():
                delta = -1 if getattr(event, "delta", 0) < 0 or getattr(event, "num", 0) == 5 else 1
                canvas.yview_scroll(delta, "units")

        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        outer = self.tk.Frame(scrollable_frame, bg=BG_DARK, padx=16, pady=12)
        outer.pack(fill="both", expand=True)

        # 1. Header with dynamic Status Pill
        header = self.tk.Frame(outer, bg=BG_DARK)
        header.pack(fill="x", pady=(0, 10))

        title_box = self.tk.Frame(header, bg=BG_DARK)
        title_box.pack(side="left", fill="y")
        self.tk.Label(title_box, text="Roblox Auto Fishing", fg=TEXT_MAIN, bg=BG_DARK,
                      font=(self.ui_font, 15, "bold")).pack(anchor="w")
        self.tk.Label(title_box, text="Auto Tracking ช่องม่วง • ตรวจจับความเร็วและชดเชยความหน่วง",
                      fg=TEXT_MUTED, bg=BG_DARK, font=(self.ui_font, 9)).pack(anchor="w", pady=(2, 0))

        self.status_pill = self.tk.Frame(header, bg=BG_CARD_LIGHT, padx=10, pady=5,
                                         highlightthickness=1, highlightbackground=BORDER_COLOR)
        self.status_pill.pack(side="right", anchor="e")
        self.status_dot = self.tk.Canvas(self.status_pill, width=10, height=10, bg=BG_CARD_LIGHT, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=AMBER_WARN, outline="")
        self.status_text_lbl = self.tk.Label(self.status_pill, text="ยังไม่ได้เลือกพื้นที่",
                                             fg=AMBER_WARN, bg=BG_CARD_LIGHT, font=(self.ui_font, 9, "bold"))
        self.status_text_lbl.pack(side="left")

        # 2. Main Live Preview Card
        preview_card = self.tk.Frame(outer, bg=BG_CARD, padx=12, pady=10,
                                     highlightthickness=1, highlightbackground=BORDER_COLOR)
        preview_card.pack(fill="x", pady=(0, 8))

        prev_hdr = self.tk.Frame(preview_card, bg=BG_CARD)
        prev_hdr.pack(fill="x", pady=(0, 6))
        self.tk.Label(prev_hdr, text="ภาพแสดงผลการตรวจจับ (Live Preview)", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 11, "bold")).pack(side="left")
        self.preview_badge = self.tk.Label(prev_hdr, text="● พักตรวจจับ", fg=TEXT_MUTED, bg=BG_CARD_LIGHT,
                                           padx=8, pady=2, font=(self.ui_font, 8, "bold"))
        self.preview_badge.pack(side="right")

        self.preview_canvas = self.tk.Canvas(preview_card, height=140, bg="#15131d",
                                             highlightthickness=1, highlightbackground="#2e2a3e")
        self.preview_canvas.pack(fill="x", pady=4)

        prev_footer = self.tk.Frame(preview_card, bg=BG_CARD)
        prev_footer.pack(fill="x", pady=(4, 0))
        self.roi_size_label = self.tk.Label(prev_footer, text="ขนาดพื้นที่: ยังไม่ได้กำหนด",
                                            fg=TEXT_MUTED, bg=BG_CARD, font=(self.ui_font, 9))
        self.roi_size_label.pack(side="left")
        self.preview_detection_label = self.tk.Label(prev_footer, text="สถานะ: รอเลือกพื้นที่",
                                                    fg=TEXT_MUTED, bg=BG_CARD, font=(self.ui_font, 9))
        self.preview_detection_label.pack(side="right")

        # 3. Control Panel (3 Steps)

        # Step 1: เลือกพื้นที่
        step1_card = self.tk.Frame(outer, bg=BG_CARD, padx=12, pady=10,
                                   highlightthickness=1, highlightbackground=BORDER_COLOR)
        step1_card.pack(fill="x", pady=(0, 8))

        s1_hdr = self.tk.Frame(step1_card, bg=BG_CARD)
        s1_hdr.pack(fill="x", pady=(0, 6))
        self.tk.Label(s1_hdr, text="ขั้นตอนที่ 1 : เลือกพื้นที่", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 11, "bold")).pack(side="left")
        self.roi_summary = self.tk.Label(s1_hdr, text="ยังไม่กำหนดแถบมินิเกม", fg=TEXT_MUTED, bg=BG_CARD,
                                         font=(self.ui_font, 9))
        self.roi_summary.pack(side="right")

        s1_row = self.tk.Frame(step1_card, bg=BG_CARD)
        s1_row.pack(fill="x")
        self.btn_select_roi = self.ttk.Button(s1_row, text="🎯  เลือกพื้นที่บนหน้าจอ",
                                              style="Primary.TButton", command=self._quick_select_bar)
        self.btn_select_roi.pack(side="left", padx=(0, 8))

        self.target_summary = self.tk.Label(s1_row, text="ยังไม่เลือกเกม", fg=TEXT_MAIN, bg=BG_CARD,
                                            font=(self.ui_font, 10))
        self.target_summary.pack(side="left", padx=(4, 10))

        self.btn_reselect = self.ttk.Button(s1_row, text="🔄  เลือกใหม่",
                                            style="Secondary.TButton", command=self._quick_select_bar)
        self.btn_reselect.pack(side="right", padx=(6, 0))

        self.btn_select_window = self.ttk.Button(s1_row, text="🖥️  หน้าต่างเกม",
                                                 style="Secondary.TButton", command=self.select_window)
        self.btn_select_window.pack(side="right")

        # Step 2: ตรวจจับ
        step2_card = self.tk.Frame(outer, bg=BG_CARD, padx=12, pady=10,
                                   highlightthickness=1, highlightbackground=BORDER_COLOR)
        step2_card.pack(fill="x", pady=(0, 8))

        s2_hdr = self.tk.Frame(step2_card, bg=BG_CARD)
        s2_hdr.pack(fill="x", pady=(0, 6))
        self.tk.Label(s2_hdr, text="ขั้นตอนที่ 2 : ตรวจจับ", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 11, "bold")).pack(side="left")

        s2_row = self.tk.Frame(step2_card, bg=BG_CARD)
        s2_row.pack(fill="x")

        self.chip_target = self.tk.Label(s2_row, text="⚪  ช่องม่วง: ยังไม่ตรวจจับ",
                                         bg=BG_CARD_LIGHT, fg=TEXT_MUTED, padx=10, pady=5,
                                         font=(self.ui_font, 9, "bold"))
        self.chip_target.pack(side="left", padx=(0, 8))

        self.chip_marker = self.tk.Label(s2_row, text="⚪  ตัวชี้: ยังไม่ตรวจจับ",
                                         bg=BG_CARD_LIGHT, fg=TEXT_MUTED, padx=10, pady=5,
                                         font=(self.ui_font, 9, "bold"))
        self.chip_marker.pack(side="left", padx=(0, 8))

        self.btn_check_live = self.ttk.Button(s2_row, text="🔍  ตรวจสอบภาพสด",
                                              style="Secondary.TButton", command=self.preview)
        self.btn_check_live.pack(side="right")

        # Step 3: เริ่มทำงาน (Fixed position primary button)
        step3_card = self.tk.Frame(outer, bg=BG_CARD, padx=14, pady=12,
                                   highlightthickness=1, highlightbackground="#4c3d70")
        step3_card.pack(fill="x", pady=(0, 8))

        s3_hdr = self.tk.Frame(step3_card, bg=BG_CARD)
        s3_hdr.pack(fill="x", pady=(0, 6))
        self.tk.Label(s3_hdr, text="ขั้นตอนที่ 3 : เริ่มทำงาน", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 11, "bold")).pack(side="left")
        self.tk.Label(s3_hdr, text="กด F8 เพื่อเริ่ม / หยุดทันที", fg=TEXT_MUTED, bg=BG_CARD,
                      font=(self.ui_font, 9)).pack(side="right")

        self.main_action_btn = self.ttk.Button(step3_card, text="▶  เริ่ม Auto (F8)",
                                               style="Primary.TButton", command=self._toggle_start_stop)
        self.main_action_btn.pack(fill="x", pady=(4, 6))

        self.action_guidance_lbl = self.tk.Label(step3_card, text="กรุณาเลือกพื้นที่บนหน้าจอในขั้นตอนที่ 1 ก่อนเริ่ม",
                                                 fg=AMBER_WARN, bg=BG_CARD, font=(self.ui_font, 10), wraplength=600, justify="left")
        self.action_guidance_lbl.pack(anchor="w")

        # 4. Collapsible Advanced Settings (Folded by default)
        adv_card = self.tk.Frame(outer, bg=BG_CARD, padx=12, pady=10,
                                 highlightthickness=1, highlightbackground=BORDER_COLOR)
        adv_card.pack(fill="x", pady=(0, 8))

        self.advanced_btn = self.ttk.Button(adv_card, text="⚙️  ตั้งค่าขั้นสูง  ▸",
                                            style="Secondary.TButton", command=self.toggle_advanced)
        self.advanced_btn.pack(anchor="w")

        self.advanced_frame = self.tk.Frame(adv_card, bg=BG_CARD)

        # Advanced Settings content
        mode_box = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        mode_box.pack(fill="x", pady=(6, 4))
        self.tk.Label(mode_box, text="ประเภทอุปกรณ์ตกปลา", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10, "bold")).pack(anchor="w", pady=(0, 2))
        self.mode_rod_rb = self.ttk.Radiobutton(
            mode_box,
            text=MODE_CONFIGS["rod"]["description"],
            variable=self.vars["fishing_mode"],
            value="rod",
            command=self._on_mode_change,
        )
        self.mode_rod_rb.pack(anchor="w", padx=4, pady=1)
        self.mode_net_rb = self.ttk.Radiobutton(
            mode_box,
            text=MODE_CONFIGS["net"]["description"],
            variable=self.vars["fishing_mode"],
            value="net",
            command=self._on_mode_change,
        )
        self.mode_net_rb.pack(anchor="w", padx=4, pady=1)

        self.ttk.Separator(self.advanced_frame, orient="horizontal").pack(fill="x", pady=(4, 6))

        tuning = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        tuning.pack(fill="x")

        self.tk.Label(tuning, text="เวลากดค้างตอนเหวี่ยงเบ็ด (วินาที)", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10)).grid(row=0, column=0, sticky="w", pady=3)
        cast_box = self.ttk.Combobox(tuning, textvariable=self.vars["cast_seconds"],
                                     values=("0.5", "0.8", "1.0", "1.5", "2.0", "3.0"), width=7)
        cast_box.grid(row=0, column=1, sticky="w", padx=(8, 16), pady=3)

        self.tk.Label(tuning, text="เมื่อกดเมาส์ค้าง ตัวเลื่อนสีขาวไปทาง", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10)).grid(row=1, column=0, sticky="w", pady=3)
        dir_frame = self.tk.Frame(tuning, bg=BG_CARD)
        dir_frame.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        self.ttk.Radiobutton(dir_frame, text="ขวา", variable=self.vars["right"], value=True).pack(side="left", padx=(0, 8))
        self.ttk.Radiobutton(dir_frame, text="ซ้าย", variable=self.vars["right"], value=False).pack(side="left")

        self.tk.Label(tuning, text="ระยะเผื่อกลางช่องม่วง (พิกเซล)", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10)).grid(row=2, column=0, sticky="w", pady=3)
        self.ttk.Entry(tuning, textvariable=self.vars["margin"], width=8).grid(row=2, column=1, sticky="w", padx=8, pady=3)

        self.tk.Label(tuning, text="ชดเชยการเคลื่อนที่ (วินาที)", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10)).grid(row=3, column=0, sticky="w", pady=3)
        self.ttk.Entry(tuning, textvariable=self.vars["lead"], width=8).grid(row=3, column=1, sticky="w", padx=8, pady=3)

        self.tk.Label(tuning, text="ความเข้มงวดสัญญาณปลากิน (0–1)", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10)).grid(row=4, column=0, sticky="w", pady=3)
        self.ttk.Entry(tuning, textvariable=self.vars["threshold"], width=8).grid(row=4, column=1, sticky="w", padx=8, pady=3)

        cb_frame = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        cb_frame.pack(fill="x", pady=(4, 4))
        self.ttk.Checkbutton(cb_frame, text="คลิกยืนยันเมื่อปลากินเบ็ด", variable=self.vars["ack"]).pack(anchor="w", pady=1)
        self.ttk.Checkbutton(cb_frame, text="แสดงข้อมูลติดตามละเอียดบนภาพ Preview (ดีบัก)", variable=self.vars["debug"]).pack(anchor="w", pady=1)

        extra_row = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        extra_row.pack(fill="x", pady=(6, 2))
        self.ttk.Button(extra_row, text="ทดลองกดค้าง 0.1 วินาที", style="Secondary.TButton", command=self.test_hold).pack(side="left")
        self.ttk.Button(extra_row, text="เลือกพื้นที่เพิ่มเติม...", style="Secondary.TButton", command=self.select_rois).pack(side="right")

        self._draw_preview_placeholder()
        self._update_readiness()

    def toggle_advanced(self):
        if self.advanced_visible:
            self.advanced_frame.pack_forget()
            self.advanced_btn.configure(text="⚙️  ตั้งค่าขั้นสูง  ▸")
        else:
            self.advanced_frame.pack(fill="x", pady=(8, 0))
            self.advanced_btn.configure(text="⚙️  ตั้งค่าขั้นสูง  ▾")
        self.advanced_visible = not self.advanced_visible

    def _toggle_start_stop(self):
        if self.running or getattr(self, "start_pending", False):
            self.stop()
        else:
            self.start()

    def _quick_select_bar(self):
        self._begin_area_selection("bar")

    def _draw_preview_placeholder(self):
        try:
            self.preview_canvas.delete("all")
            self.preview_canvas.update_idletasks()
            w = max(240, self.preview_canvas.winfo_width())
            h = max(80, self.preview_canvas.winfo_height())
            cx, cy = w // 2, h // 2
            self.preview_canvas.create_rectangle(cx - 150, cy - 26, cx + 150, cy + 26,
                                                 outline="#322c44", dash=(4, 4), width=1)
            self.preview_canvas.create_text(cx, cy - 6, text="🎯  ยังไม่ได้เลือกพื้นที่แถบมินิเกม",
                                           fill="#a78bfa", font=(self.ui_font, 10, "bold"))
            self.preview_canvas.create_text(cx, cy + 13, text="กดปุ่ม 'เลือกพื้นที่บนหน้าจอ' ในขั้นตอนที่ 1 ด้านล่างเพื่อเริ่มต้น",
                                           fill="#6e6784", font=(self.ui_font, 9))
            if hasattr(self, "preview_badge"):
                self.preview_badge.configure(text="● พักตรวจจับ", fg="#9d96b0", bg="#272336")
        except Exception:
            pass

    def _update_preview_display(self, crop, detection_result=None):
        if crop is None or crop.size == 0:
            self._draw_preview_placeholder()
            return
        try:
            from PIL import Image, ImageTk
            img_h, img_w = crop.shape[0], crop.shape[1]
            if detection_result is None:
                detection_result = detect_bar(crop)
            det_status, marker, target = detection_result

            # Update Step 2 chips
            if hasattr(self, "chip_target"):
                if target is not None:
                    self.chip_target.configure(text=f"🟣  ช่องม่วง: พบ [{target[0]:.0f}–{target[1]:.0f} px]",
                                               bg="#2e1065", fg="#d8b4fe")
                else:
                    self.chip_target.configure(text="🟡  ช่องม่วง: ยังไม่พบ",
                                               bg="#451a03", fg="#fbbf24")

            if hasattr(self, "chip_marker"):
                if marker is not None:
                    self.chip_marker.configure(text=f"⚪  ตัวชี้: พบ [x={marker:.0f} px]",
                                               bg="#262335", fg="#ffffff")
                else:
                    self.chip_marker.configure(text="🟡  ตัวชี้: ยังไม่พบ",
                                               bg="#451a03", fg="#fbbf24")

            self.preview_canvas.update_idletasks()
            canv_w = max(240, self.preview_canvas.winfo_width())
            canv_h = max(80, self.preview_canvas.winfo_height())
            self.preview_canvas.delete("all")

            # Scale to fit nicely centered in canvas
            target_h = min(canv_h - 40, max(28, img_h * 2))
            scale = target_h / max(1, img_h)
            disp_w = int(round(img_w * scale))
            disp_h = int(round(img_h * scale))

            if disp_w > canv_w - 20:
                scale = (canv_w - 20) / max(1, img_w)
                disp_w = int(round(img_w * scale))
                disp_h = int(round(img_h * scale))

            pos_x = (canv_w - disp_w) // 2
            pos_y = (canv_h - disp_h) // 2

            # Render image
            rgb = crop[:, :, ::-1] if crop.shape[2] >= 3 else crop
            pil_img = Image.fromarray(rgb).resize((disp_w, disp_h), Image.Resampling.NEAREST)
            photo = ImageTk.PhotoImage(pil_img)
            self._preview_photo = photo
            self.preview_canvas.create_image(pos_x, pos_y, image=photo, anchor="nw")

            # Minigame bar frame
            self.preview_canvas.create_rectangle(pos_x - 1, pos_y - 1, pos_x + disp_w, pos_y + disp_h,
                                                 outline="#3d3752", width=1)

            # Overlay: Purple Target Zone
            if target is not None:
                tx0 = pos_x + int(round(target[0] * scale))
                tx1 = pos_x + int(round(target[1] * scale))
                t_center = (tx0 + tx1) // 2
                self.preview_canvas.create_rectangle(tx0, pos_y, tx1, pos_y + disp_h,
                                                     outline="#a78bfa", width=2)
                self.preview_canvas.create_text(t_center, pos_y - 9, text="▼ ช่องม่วง",
                                                fill="#c084fc", font=(self.ui_font, 8, "bold"))
                self.preview_canvas.create_line(t_center, pos_y, t_center, pos_y + disp_h,
                                                fill="#38bdf8", dash=(2, 2), width=1)

            # Overlay: White Pointer Marker
            if marker is not None:
                mx = pos_x + int(round(marker * scale))
                self.preview_canvas.create_line(mx, pos_y - 3, mx, pos_y + disp_h + 3,
                                                fill="#ffffff", width=2)
                self.preview_canvas.create_polygon(mx - 4, pos_y + disp_h + 8,
                                                   mx + 4, pos_y + disp_h + 8,
                                                   mx, pos_y + disp_h + 2,
                                                   fill="#ffffff")
                self.preview_canvas.create_text(mx, pos_y + disp_h + 15, text="ตัวชี้",
                                                fill="#ffffff", font=(self.ui_font, 8, "bold"))

            # Debug Telemetry overlay if enabled
            if self.vars["debug"].get() and getattr(self, "controller", None) and getattr(self.controller, "telemetry", None):
                t = self.controller.telemetry
                dbg_text = f"v={t['velocity']:+.0f}px/s • latency={t['total_latency_ms']:.0f}ms • คำสั่ง={t['action']}"
                self.preview_canvas.create_text(pos_x, pos_y - 10, text=dbg_text,
                                                anchor="w", fill="#9d96b0", font=(self.ui_font, 8))

            if hasattr(self, "roi_size_label"):
                self.roi_size_label.configure(text=f"ขนาดพื้นที่: {img_w} × {img_h} px")
            if hasattr(self, "preview_detection_label"):
                status_map = {"valid": "🟢 ตรวจพบแถบและช่องม่วงสมบูรณ์", "absent": "🟡 ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่", "ambiguous": "⚠️ ภาพไม่ชัดเจน"}
                self.preview_detection_label.configure(text=status_map.get(det_status, f"สถานะ: {det_status}"))
            if hasattr(self, "preview_badge"):
                self.preview_badge.configure(text="● ภาพสด", fg="#34d399", bg="#064e3b")
        except Exception:
            pass

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

        # Update visual status pill and guidance text
        if hasattr(self, "status_pill") and hasattr(self, "status_text_lbl") and hasattr(self, "status_dot"):
            if any(k in display for k in ("เริ่มไม่ได้", "เกิดข้อผิดพลาด", "ไม่ได้", "error")):
                self.status_pill.configure(bg="#450a0a")
                self.status_dot.configure(bg="#450a0a")
                self.status_dot.itemconfig(self.status_dot_id, fill="#ef4444")
                self.status_text_lbl.configure(text="ข้อผิดพลาด", fg="#f87171")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text=f"🔴  {display}", fg="#ef4444")
            elif any(k in display for k in ("กำลังทำงาน", "คุมแถบ", "เหวี่ยงเบ็ด", "รอปลา")):
                self.status_pill.configure(bg="#064e3b")
                self.status_dot.configure(bg="#064e3b")
                self.status_dot.itemconfig(self.status_dot_id, fill="#10b981")
                self.status_text_lbl.configure(text="กำลังทำงาน", fg="#34d399")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text="🟢  กำลังทำงาน: ติดตามช่องม่วงอัตโนมัติ • กด F8 หรือคลิกหยุดเมื่อจบรอบ", fg="#10b981")
            elif any(k in display for k in ("ตรวจไม่พบเป้าหมาย", "กำลังค้นหาใหม่", "ยังไม่พบแถบ", "สลับไปที่เกม")):
                self.status_pill.configure(bg="#451a03")
                self.status_dot.configure(bg="#451a03")
                self.status_dot.itemconfig(self.status_dot_id, fill="#f59e0b")
                self.status_text_lbl.configure(text="กำลังค้นหาเป้าหมาย", fg="#fbbf24")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text=f"🟡  {display}", fg="#f59e0b")
            elif any(k in display for k in ("เลือกพื้นที่แล้ว", "พร้อม")):
                self.status_pill.configure(bg="#064e3b")
                self.status_dot.configure(bg="#064e3b")
                self.status_dot.itemconfig(self.status_dot_id, fill="#10b981")
                self.status_text_lbl.configure(text="พร้อมเริ่ม", fg="#34d399")
                if hasattr(self, "action_guidance_lbl") and not self.running:
                    self.action_guidance_lbl.configure(text="🟢  พบช่องม่วงและตัวชี้ พร้อมเริ่ม • กดปุ่มหรือกด F8 (มีเวลา 3 วินาทีสลับเข้าเกม)", fg="#10b981")
            elif "หยุด" in display:
                self.status_pill.configure(bg="#272336")
                self.status_dot.configure(bg="#272336")
                self.status_dot.itemconfig(self.status_dot_id, fill="#9d96b0")
                self.status_text_lbl.configure(text="หยุดทำงาน", fg="#9d96b0")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text="กดเริ่ม Auto หรือ F8 เพื่อเริ่มต้นใหม่", fg="#9d96b0")

        self._update_readiness()

    def _update_readiness(self):
        if not hasattr(self, "target_summary"):
            return
        if self.target:
            bounds = self.target[1]
            self.target_summary.configure(text=f"เกม: {bounds[2]} × {bounds[3]} px")
        else:
            self.target_summary.configure(text="ยังไม่เลือกเกม")

        rois = self.settings.get("rois", {}) if isinstance(self.settings, dict) else {}
        bar_ready = bool(rois.get("bar"))
        if bar_ready:
            bar_roi = rois["bar"]
            self.roi_summary.configure(text=f"แถบมินิเกม: {bar_roi[2]} × {bar_roi[3]} px")
            if hasattr(self, "roi_size_label"):
                self.roi_size_label.configure(text=f"ขนาดพื้นที่: {bar_roi[2]} × {bar_roi[3]} px")
            if hasattr(self, "main_action_btn") and not self.running and not getattr(self, "start_pending", False):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
        else:
            self.roi_summary.configure(text="ยังไม่กำหนดแถบมินิเกม")
            if hasattr(self, "roi_size_label"):
                self.roi_size_label.configure(text="ขนาดพื้นที่: ยังไม่ได้กำหนด")
            if hasattr(self, "main_action_btn") and not self.running and not getattr(self, "start_pending", False):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text="⚠️  กรุณาเลือกพื้นที่บนหน้าจอในขั้นตอนที่ 1 ก่อนเริ่ม", fg="#f59e0b")

    def _read_ui(self):
        return {
            "fishing_mode": str(self.vars["fishing_mode"].get()),
            "cast_seconds": float(self.vars["cast_seconds"].get()),
            "margin": float(self.vars["margin"].get()),
            "lead": float(self.vars["lead"].get()),
            "bite_threshold": float(self.vars["threshold"].get()),
            "right_on_hold": bool(self.vars["right"].get()),
            "bite_ack": bool(self.vars["ack"].get()),
            "debug": bool(self.vars["debug"].get()),
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
        dialog.title("เลือกพื้นที่ตรวจจับ")
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self._close_area_dialog)
        dialog.configure(bg="#121118")
        dialog.lift()
        dialog.focus_set()
        self.ttk.Label(dialog, text="เลือกพื้นที่ที่ต้องการกำหนด:", style="Card.TLabel").pack(padx=16, pady=(12, 8))
        self.ttk.Button(dialog, text="🎯  เลือกพื้นที่แถบมินิเกม (จำเป็น)",
                        command=lambda: self._begin_area_selection("bar")).pack(fill="x", padx=16, pady=4)
        self.ttk.Separator(dialog, orient="horizontal").pack(fill="x", padx=12, pady=10)
        self.ttk.Label(dialog, text="พื้นที่เพิ่มเติม (ตัวเลือกเสริม):", style="Card.TLabel").pack(anchor="w", padx=16, pady=(0, 4))
        opt_frame = self.ttk.Frame(dialog, style="Card.TFrame")
        opt_frame.pack(fill="x", padx=16, pady=2)
        optional_options = [
            ("cast", "พื้นที่แถบเหวี่ยงเบ็ด"),
            ("bite", "พื้นที่ค้นหาสัญญาณปลากินเบ็ด"),
            ("template", "ครอบภาพตัวอย่างสัญญาณ"),
        ]
        combo_labels = [label for _, label in optional_options]
        opt_combo = self.ttk.Combobox(opt_frame, values=combo_labels, state="readonly", width=25)
        opt_combo.current(0)
        opt_combo.pack(side="left", padx=(0, 6))

        def pick_optional():
            idx = opt_combo.current()
            if 0 <= idx < len(optional_options):
                self._begin_area_selection(optional_options[idx][0])

        self.ttk.Button(opt_frame, text="เลือกพื้นที่นี้", command=pick_optional).pack(side="left")
        self.ttk.Button(dialog, text="ปิด", command=self._close_area_dialog).pack(pady=(14, 12))

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
        picker.transient(self.root)
        picker.configure(bg="#121118")

        # Top instruction banner
        banner = self.tk.Frame(picker, bg="#1e1b29", padx=14, pady=8)
        banner.pack(fill="x")
        self.tk.Label(banner,
                      text="📌  คลิกค้างแล้วลากเพื่อเลือกพื้นที่ • Esc เพื่อยกเลิก",
                      fg="#f3f0fb", bg="#1e1b29", font=(self.ui_font, 11, "bold")).pack(side="left")

        max_width, max_height = 1100, 700
        img_h, img_w = image.shape[0], image.shape[1]
        scale = min(1.0, max_width / max(1, img_w), max_height / max(1, img_h))
        canvas_w = int(round(img_w * scale))
        canvas_h = int(round(img_h * scale))
        canvas = self.tk.Canvas(picker, width=canvas_w, height=canvas_h, cursor="crosshair",
                                highlightthickness=0, bg="#121118")
        canvas.pack()
        try:
            from PIL import Image, ImageTk
            photo = ImageTk.PhotoImage(Image.fromarray(image[:, :, ::-1]).resize((canvas_w, canvas_h)))
            canvas.create_image(0, 0, image=photo, anchor="nw")
            canvas._photo = photo
        except Exception:
            canvas.configure(background="#1a1824")

        start = [None]
        is_dragging = [False]
        current_rect = [None]
        selected_area = [None]

        def clear_overlay():
            canvas.delete("roi_dim")
            canvas.delete("roi_border")
            canvas.delete("roi_badge")

        def draw_selection(x0, y0, x1, y1):
            clear_overlay()
            w, h = x1 - x0, y1 - y0
            if w <= 0 or h <= 0:
                return

            # Dimmed effect: Darken area outside the selection box
            # 1. Top
            if y0 > 0:
                canvas.create_rectangle(0, 0, canvas_w, y0, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
            # 2. Bottom
            if y1 < canvas_h:
                canvas.create_rectangle(0, y1, canvas_w, canvas_h, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
            # 3. Left
            if x0 > 0:
                canvas.create_rectangle(0, y0, x0, y1, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
            # 4. Right
            if x1 < canvas_w:
                canvas.create_rectangle(x1, y0, canvas_w, y1, fill="#000000", stipple="gray50", outline="", tags="roi_dim")

            # High-contrast double purple border visible on both dark and bright backgrounds
            canvas.create_rectangle(x0, y0, x1, y1, outline="#2e1065", width=3, tags="roi_border")
            canvas.create_rectangle(x0, y0, x1, y1, outline="#d8b4fe", width=1, tags="roi_border")

            # Corner brackets
            c_len = min(14, max(4, int(min(w, h) / 3)))
            canvas.create_line(x0, y0 + c_len, x0, y0, x0 + c_len, y0, fill="#c084fc", width=2, tags="roi_border")
            canvas.create_line(x1 - c_len, y0, x1, y0, x1, y0 + c_len, fill="#c084fc", width=2, tags="roi_border")
            canvas.create_line(x0, y1 - c_len, x0, y1, x0 + c_len, y1, fill="#c084fc", width=2, tags="roi_border")
            canvas.create_line(x1 - c_len, y1, x1, y1, x1 - c_len, y1, fill="#c084fc", width=2, tags="roi_border")

            # Actual size in screen pixels
            actual_w = max(1, int(round(w * img_w / canvas_w)))
            actual_h = max(1, int(round(h * img_h / canvas_h)))
            dim_text = f"{actual_w} × {actual_h} px"

            # Badge position near box without clipping
            if y0 >= 26:
                badge_y = y0 - 13
            elif y1 <= canvas_h - 26:
                badge_y = y1 + 13
            else:
                badge_y = y0 + 13

            badge_x = (x0 + x1) / 2.0
            half_badge = 50.0
            badge_x = max(half_badge + 4, min(canvas_w - half_badge - 4, badge_x))

            canvas.create_rectangle(
                badge_x - half_badge, badge_y - 10,
                badge_x + half_badge, badge_y + 10,
                fill="#1e1b29",
                outline="#8b5cf6",
                width=1,
                tags="roi_badge"
            )
            canvas.create_text(
                badge_x, badge_y,
                text=dim_text,
                fill="#ffffff",
                font=(self.ui_font, 9, "bold"),
                tags="roi_badge"
            )

        def down(event):
            sx = max(0, min(canvas_w, event.x))
            sy = max(0, min(canvas_h, event.y))
            start[0] = (sx, sy)
            is_dragging[0] = True
            clear_overlay()
            current_rect[0] = None
            selected_area[0] = None
            confirm_btn.configure(state="disabled")
            info_label.configure(text="กำลังลากเลือกพื้นที่…")

        def motion(event):
            if not is_dragging[0] or start[0] is None:
                return
            cur_x = max(0, min(canvas_w, event.x))
            cur_y = max(0, min(canvas_h, event.y))
            x0, y0 = min(start[0][0], cur_x), min(start[0][1], cur_y)
            x1, y1 = max(start[0][0], cur_x), max(start[0][1], cur_y)
            current_rect[0] = (x0, y0, x1, y1)
            draw_selection(x0, y0, x1, y1)

        def up(event):
            if not is_dragging[0] or start[0] is None:
                return
            is_dragging[0] = False
            cur_x = max(0, min(canvas_w, event.x))
            cur_y = max(0, min(canvas_h, event.y))
            x0, y0 = min(start[0][0], cur_x), min(start[0][1], cur_y)
            x1, y1 = max(start[0][0], cur_x), max(start[0][1], cur_y)
            w, h = x1 - x0, y1 - y0

            if w < 3 or h < 3:
                clear_overlay()
                current_rect[0] = None
                selected_area[0] = None
                confirm_btn.configure(state="disabled")
                info_label.configure(text="กรอบเล็กเกินไป กรุณาคลิกค้างแล้วลากใหม่")
                return

            current_rect[0] = (x0, y0, x1, y1)
            draw_selection(x0, y0, x1, y1)

            orig_x = int(round(x0 * img_w / canvas_w))
            orig_y = int(round(y0 * img_h / canvas_h))
            orig_w = max(1, int(round(w * img_w / canvas_w)))
            orig_h = max(1, int(round(h * img_h / canvas_h)))
            orig_x = max(0, min(img_w - 1, orig_x))
            orig_y = max(0, min(img_h - 1, orig_y))
            orig_w = min(img_w - orig_x, orig_w)
            orig_h = min(img_h - orig_y, orig_h)

            area = [bounds[0] + orig_x, bounds[1] + orig_y, orig_w, orig_h]
            selected_area[0] = area
            info_label.configure(text=f"เลือก: กว้าง {orig_w} × สูง {orig_h} px")
            confirm_btn.configure(state="normal")
            # Auto-confirm on mouse release for seamless UX
            confirm()

        def confirm(event=None):
            area = selected_area[0]
            if not area:
                return
            if name == "template":
                try:
                    crop_x = area[0] - bounds[0]
                    crop_y = area[1] - bounds[1]
                    crop = image[crop_y : crop_y + area[3], crop_x : crop_x + area[2]]
                    if cv2 is not None:
                        TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(TEMPLATE_FILE), crop)
                except Exception:
                    pass
                self._set_status("บันทึกภาพตัวอย่างสัญญาณปลากินเบ็ดแล้ว")
            else:
                self.settings.setdefault("rois", {})[name] = area
                save_settings(self.settings)
                self._set_status("เลือกพื้นที่แล้ว")
                if name == "bar":
                    try:
                        crop_x = max(0, area[0] - bounds[0])
                        crop_y = max(0, area[1] - bounds[1])
                        crop = image[crop_y : crop_y + area[3], crop_x : crop_x + area[2]]
                        self._update_preview_display(crop)
                    except Exception:
                        pass
            self._update_readiness()
            picker.destroy()

        def cancel(event=None):
            if is_dragging[0]:
                is_dragging[0] = False
                start[0] = None
                clear_overlay()
                confirm_btn.configure(state="disabled")
                info_label.configure(text="ยกเลิกการลากแล้ว — ลากใหม่เพื่อเลือก")
            else:
                picker.destroy()

        canvas.bind("<ButtonPress-1>", down)
        canvas.bind("<B1-Motion>", motion)
        canvas.bind("<ButtonRelease-1>", up)
        picker.bind("<Escape>", cancel)
        picker.bind("<Return>", confirm)

        bottom_bar = self.tk.Frame(picker, bg="#1e1b29", padx=14, pady=8)
        bottom_bar.pack(fill="x")
        info_label = self.tk.Label(bottom_bar, text="คลิกค้างแล้วลากเพื่อเลือกพื้นที่",
                                   fg="#9d96b0", bg="#1e1b29", font=(self.ui_font, 10))
        info_label.pack(side="left")

        btn_box = self.tk.Frame(bottom_bar, bg="#1e1b29")
        btn_box.pack(side="right")
        self.ttk.Button(btn_box, text="ยกเลิก (Esc)", style="Secondary.TButton", command=cancel).pack(side="right", padx=(6, 0))
        confirm_btn = self.ttk.Button(btn_box, text="ใช้พื้นที่นี้ (Enter)", style="Primary.TButton", state="disabled", command=confirm)
        confirm_btn.pack(side="right")

    def _settings(self):
        return validate_settings(self._read_ui(), primary_screen_bounds())

    def preview(self):
        if self.running or self.start_pending or self.test_hold_pending or self.test_hold_active:
            self._set_status("หยุดการทำงานก่อนตรวจจับแถบ")
            return
        try:
            settings = self._settings()
            if not settings["rois"].get("bar"):
                self._set_status("ยังไม่ได้กำหนดพื้นที่แถบมินิเกม")
                return
            cap = ScreenCapture()
            crop = cap.grab(settings["rois"]["bar"])
            result = detect_bar(crop)
            cap.close()
            labels = {"valid": "พบแถบพร้อมติดตาม", "absent": "ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่", "ambiguous": "ภาพไม่ชัดเจน"}
            self._set_status(f"ผลตรวจจับ: {labels.get(result[0], result[0])} • ตัวเลื่อน {result[1]} • ช่องเป้าหมาย {result[2]}")
            self._update_preview_display(crop, result)
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
            if not settings["rois"].get("bar"):
                raise RuntimeError("จำเป็นต้องกำหนดพื้นที่แถบมินิเกม")
            self.start_pending = True
            self._set_mode_widgets_state("disabled")
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="■  ยกเลิกเริ่ม (3s...)", style="Danger.TButton")
            self._set_status("สลับไปที่เกมภายใน 3 วินาทีเพื่อเริ่มทำงาน")
            self.pending_start_id = self.root.after(3000, lambda: self._start_now(settings))
        except Exception as exc:
            self._set_mode_widgets_state("normal")
            release_mouse()
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
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
            if settings["rois"].get("bite") and template_path.exists():
                if cv2 is not None:
                    self.template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
                bite_roi = settings["rois"]["bite"]
                if (self.template is not None and (
                        self.template.ndim != 3 or self.template.shape[2] < 3
                        or self.template.shape[0] > bite_roi[3]
                        or self.template.shape[1] > bite_roi[2]
                        or not _has_spatial_variation(self.template))):
                    self.template = None
            save_settings(settings)
            self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self.controller = Controller(settings)
            self.controller.start(time.monotonic())
            self.capture = ScreenCapture()
            _set_fast_input_timing()
            self.running = True
            self.fps_count, self.fps_started = 0, time.monotonic()
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="■  หยุด Auto (F8)", style="Danger.TButton")
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
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
            self._set_status(f"เริ่มไม่ได้: {exc}")

    def _pump(self):
        if not self.running:
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return
        capture_time = time.monotonic()
        try:
            if window_snapshot() != self.target:
                observation = None
                focused = False
            else:
                bar_image = self.capture.grab(self.settings["rois"]["bar"])
                bar = detect_bar(bar_image)
                bite = False
                mode = self.settings.get("fishing_mode", "rod")
                if mode != "net" and self.template is not None and self.settings["rois"].get("bite"):
                    bite = detect_bite(self.capture.grab(self.settings["rois"]["bite"]), self.template,
                                       self.settings["bite_threshold"])
                observation = {"bar": bar, "bite": bite}
                self.fps_count += 1
                focused = True

                # Real-time preview update (~6-7 FPS throttled for smooth UI without lagging capture)
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
            action = self.controller.step(now, observation, focused, capture_time=capture_time)
            if not apply_action(action, self.target):
                self.controller.stop("target window moved or pointer left it")
            elapsed = now - self.fps_started
            fps = self.fps_count / elapsed if elapsed > 0 else 0.0
            if self.settings.get("debug") and getattr(self.controller, "telemetry", None) and self.controller.state == "Track":
                t = self.controller.telemetry
                self._set_status(
                    f"[ดีบัก] ช่อง:[{t['target_left']:.0f},{t['target_right']:.0f}] กลาง:{t['target_center']:.0f} "
                    f"• ตัวชี้:{t['marker_x']:.0f} คาดการณ์:{t['predicted_x']:.0f} v:{t['velocity']:+.0f}px/s "
                    f"• หน่วง:{t['total_latency_ms']:.0f}ms (ภาพ:{t['frame_age_ms']:.0f}ms) • สั่ง:{t['action']}"
                )
            elif observation and observation.get("bar") and observation["bar"][0] == "absent" and self.controller.state in ("Track", "End"):
                self._set_status("ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่")
            else:
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
        self.root.after(12, self._pump)

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
        if hasattr(self, "main_action_btn"):
            self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
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


def _bootstrap() -> None:
    """Ensure .venv and .runtime/usr/lib (bundled Tk) are active when executed directly."""
    import sys
    if os.environ.get("_AUTO_FISHING_REEXEC") == "1":
        return
    runtime_lib = ROOT / ".runtime" / "usr" / "lib"
    venv_py = ROOT / ".venv" / "bin" / "python"
    need_reexec = False
    new_env = dict(os.environ)

    if runtime_lib.is_dir():
        ld_path = new_env.get("LD_LIBRARY_PATH", "")
        if str(runtime_lib) not in ld_path.split(":"):
            new_env["LD_LIBRARY_PATH"] = f"{runtime_lib}:{ld_path}" if ld_path else str(runtime_lib)
            new_env["TK_LIBRARY"] = str(runtime_lib / "tk8.6")
            need_reexec = True

    target_py = sys.executable
    if venv_py.is_file():
        try:
            if Path(sys.executable).resolve() != venv_py.resolve():
                target_py = str(venv_py)
                need_reexec = True
        except Exception:
            pass

    if need_reexec:
        new_env["_AUTO_FISHING_REEXEC"] = "1"
        try:
            os.execve(target_py, [target_py] + sys.argv, new_env)
        except Exception:
            pass


if __name__ == "__main__":
    _bootstrap()
    main()
