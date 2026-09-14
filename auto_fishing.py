"""Screen based Roblox fishing helper for visible, focused game windows.

The module is import safe: it does not capture screens, register hotkeys, or
send input until the UI or an explicit API call starts it.
"""

from __future__ import annotations

import collections
import ctypes
import json
import math
import multiprocessing
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

if platform.system() == "Linux":
    try:
        import gamescope_manager as gm
    except Exception:  # pragma: no cover
        gm = None
else:
    gm = None


ROOT = Path(__file__).resolve().parent
APP_VERSION = "0.0.7"
BUILD_ID = time.strftime("%Y%m%d-%H%M", time.localtime())


def user_data_dir(system: str | None = None, environ: dict[str, str] | None = None) -> Path:
    """Return a writable persistent directory for user-created settings."""
    if (system or platform.system()) == "Windows":
        environment = os.environ if environ is None else environ
        return Path(environment.get("LOCALAPPDATA") or Path.home()) / "RobloxAutoFishing"
    return ROOT


DATA_DIR = user_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
TEMPLATE_FILE = DATA_DIR / "bite-template.png"
DEBUG_LOG_FILE = DATA_DIR / "debug.log"


def log_diagnostic_event(event_type: str, data: dict[str, Any]) -> None:
    """Write diagnostic transitions, failures, and telemetry to %LOCALAPPDATA%/RobloxAutoFishing/debug.log."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if DEBUG_LOG_FILE.exists() and DEBUG_LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            old_file = DATA_DIR / "debug.log.old"
            try:
                if old_file.exists():
                    old_file.unlink()
                DEBUG_LOG_FILE.rename(old_file)
            except Exception:
                pass
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        log_entry = {
            "timestamp": ts,
            "version": APP_VERSION,
            "build": BUILD_ID,
            "event": event_type,
            **data,
        }
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
    # Shared core settings
    "fishing_mode": "rod",
    "cast_seconds": 2.0,
    "right_on_hold": False,
    "margin": 3.0,
    "lead": 0.0,
    "bite_threshold": 0.85,
    "bite_ack": True,
    "debug": False,
    "auto_refocus": False,
    "auto_restart_30s": True,
    "auto_restart_interval": 30,
    # Platform-specific isolated namespaces
    "windows": {
        "rois": {"bar": None, "bite": None},
        "auto_restart_30s": True,
        "auto_restart_interval": 30,
    },
    "linux": {
        "env_mode": "desktop",
        "desktop_rois": {"bar": None, "bite": None},
        "gamescope_rois": {"bar": None, "bite": None},
        "gamescope_fps": 60,
        "gamescope_res": "1280x720",
        "gamescope_fullscreen": False,
        "auto_restart_30s": True,
        "auto_restart_interval": 30,
        "gamescope_vk_device_override": None,
    },
    # Backwards compatibility mirrors
    "env_mode": "desktop",
    "gamescope_fps": 60,
    "gamescope_res": "1280x720",
    "gamescope_fullscreen": False,
    "gamescope_vk_device_override": None,
    "rois": {"bar": None, "bite": None},
    "gamescope_rois": {"bar": None, "bite": None},
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
    result.update({k: v for k, v in data.items() if k not in ("rois", "gamescope_rois", "linux", "windows")})
    result["env_mode"] = str(data.get("env_mode", "desktop"))
    if "gamescope_rois" in data and isinstance(data["gamescope_rois"], dict):
        result["gamescope_rois"] = dict(data["gamescope_rois"])
    if "linux" in data and isinstance(data["linux"], dict):
        result["linux"] = dict(data["linux"])
    if "windows" in data and isinstance(data["windows"], dict):
        result["windows"] = dict(data["windows"])
    mode = result.get("fishing_mode", "rod")
    if not isinstance(mode, str) or mode not in MODE_CONFIGS:
        raise ValueError("ประเภทอุปกรณ์ตกปลาต้องเป็น 'rod' หรือ 'net'")
    result["fishing_mode"] = str(mode)
    try:
        gs_fps = int(result.get("gamescope_fps", 60))
        result["gamescope_fps"] = gs_fps if 30 <= gs_fps <= 360 else 60
    except (ValueError, TypeError):
        result["gamescope_fps"] = 60
    res = str(result.get("gamescope_res", "1280x720"))
    result["gamescope_res"] = res if "x" in res else "1280x720"
    result["gamescope_fullscreen"] = bool(result.get("gamescope_fullscreen", False))
    for key, low, high in (("cast_seconds", 0.05, 10.0), ("lead", 0.0, 0.5),
                           ("margin", 0.0, None), ("bite_threshold", 0.0, 1.0)):
        value = result.get(key)
        if not _finite(value) or float(value) < low or (high is not None and float(value) > high):
            raise ValueError(f"ค่า {key} อยู่นอกช่วงที่กำหนด")
        result[key] = float(value)
    for key in ("right_on_hold", "bite_ack", "debug", "auto_refocus", "auto_restart_30s"):
        if key in result and not isinstance(result.get(key), bool):
            raise ValueError(f"ค่า {key} ต้องเป็น true หรือ false")
    result["auto_restart_30s"] = bool(result.get("auto_restart_30s", True))
    try:
        interval = int(result.get("auto_restart_interval", 30))
        result["auto_restart_interval"] = interval if 5 <= interval <= 600 else 30
    except (ValueError, TypeError):
        result["auto_restart_interval"] = 30
    if "windows" in result and isinstance(result["windows"], dict):
        result["windows"]["auto_restart_30s"] = bool(result["windows"].get("auto_restart_30s", result["auto_restart_30s"]))
        try:
            win_interval = int(result["windows"].get("auto_restart_interval", result["auto_restart_interval"]))
            result["windows"]["auto_restart_interval"] = win_interval if 5 <= win_interval <= 600 else 30
        except (ValueError, TypeError):
            result["windows"]["auto_restart_interval"] = 30
    if "linux" in result and isinstance(result["linux"], dict):
        result["linux"]["auto_restart_30s"] = bool(result["linux"].get("auto_restart_30s", result["auto_restart_30s"]))
        try:
            lin_interval = int(result["linux"].get("auto_restart_interval", result["auto_restart_interval"]))
            result["linux"]["auto_restart_interval"] = lin_interval if 5 <= lin_interval <= 600 else 30
        except (ValueError, TypeError):
            result["linux"]["auto_restart_interval"] = 30
    vk_override = data.get("gamescope_vk_device_override")
    if not vk_override and isinstance(data.get("linux"), dict):
        vk_override = data["linux"].get("gamescope_vk_device_override")
    if vk_override:
        result["gamescope_vk_device_override"] = str(vk_override).strip()
        if "linux" in result and isinstance(result["linux"], dict):
            result["linux"]["gamescope_vk_device_override"] = str(vk_override).strip()
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
    purple = cv2.inRange(hsv, (108, 22, 35), (170, 255, 255))
    white = cv2.inRange(hsv, (0, 0, 160), (179, 60, 255))

    def components(mask):
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        return [tuple(map(int, row)) for row in stats[1:]]

    markers = [(x, y, w, h) for x, y, w, h, area in components(white)
               if 3 <= h <= 80 and 2 <= w <= max(14, int(h * 1.8))
               and area >= min(4, w * h * 0.30)]

    raw_targets = [(x, y, w, h) for x, y, w, h, area in components(purple)
                   if 3 <= h <= 64 and w >= 2
                   and area >= min(5, w * h * 0.30)]
    if not raw_targets:
        return "absent", None, None

    def aligned(a, b):
        _, y, _, h = a
        _, other_y, _, other_h = b
        clipped = (y == 0 or other_y == 0 or (y + h >= image.shape[0]) or (other_y + other_h >= image.shape[0]))
        h_ratio = h / max(1, other_h)
        if clipped:
            vert_overlap = min(y + h, other_y + other_h) - max(y, other_y)
            return (0.15 <= h_ratio <= 6.0 and (vert_overlap >= -2 or abs(y + h / 2.0 - other_y - other_h / 2.0) <= max(6.0, min(h, other_h) * 0.65)))
        return (0.35 <= h_ratio <= 2.8
                and abs(y + h / 2.0 - other_y - other_h / 2.0) <= max(3.5, min(h, other_h) * 0.45))

    raw_targets.sort()
    merged = []
    for target in raw_targets:
        x, y, w, h = target
        joined = False
        for index, previous in enumerate(merged):
            px, py, pw, ph = previous
            gap_left, gap_right = px + pw, x
            if aligned(previous, target):
                if 0 <= gap_right - gap_left <= 5:
                    top, bottom = min(py, y), max(py + ph, y + h)
                    merged[index] = (px, top, x + w - px, bottom - top)
                    joined = True
                    break
                elif 0 <= gap_right - gap_left <= max(h, ph) * 2.5:
                    covering = any(aligned(marker, target) and mx <= gap_left + 4
                                   and mx + mw >= gap_right - 4
                                   for marker in markers for mx, my, mw, mh in [marker])
                    if covering:
                        top, bottom = min(py, y), max(py + ph, y + h)
                        merged[index] = (px, top, x + w - px, bottom - top)
                        joined = True
                        break
        if not joined:
            merged.append(target)

    valid_targets = [t for t in merged if t[2] >= 5]
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
                marker_center = mx + (mw - 1) / 2.0
                horizontal_distance = max(float(x) - marker_center, marker_center - float(x + w), 0.0)
                score = horizontal_distance * 3.0 + y_center_diff * 2.0 + h_diff
                matches.append((score, marker_center, (float(x), float(x + w))))

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
        self.desired_held = False
        self.actual_held = False
        self.cast_started = self.wait_started = self.absent_started = None
        self.valid_streak = self.bite_streak = self.bite_clear_streak = 0
        self.bite_latched = False
        mode = self.settings.get("fishing_mode", "rod")
        default_wait_timeout = 60.0 if mode == "net" else 45.0
        self.wait_timeout = float(wait_timeout) if wait_timeout is not None else float(self.settings.get("wait_timeout", default_wait_timeout))
        self.track_timeout = float(track_timeout) if track_timeout is not None else None
        self.max_retries = int(self.settings.get("max_retries", 3))
        self.retry_count = 0
        self.continuous_recovery = bool(self.settings.get("continuous_recovery", False))
        self.absent_timeout = float(self.settings.get("absent_timeout", 0.25))
        self.ambiguous_streak = 0
        self.ambiguous_started: float | None = None
        self.ambiguous_resume_frames = 0
        self.ambiguous_timeout = float(self.settings.get("ambiguous_timeout", 3.0))
        self.previous_x = self.previous_time = None
        self.velocity = 0.0
        configured_lead = float(self.settings.get("lead", 0.0))
        self.calibrated_latency = configured_lead if configured_lead > 0.0 else 0.04
        self.last_switch_action = None
        self.last_switch_time = None
        self.last_switch_x = None
        self.telemetry: dict[str, Any] = {}
        self.pre_focus_state = None
        self.resume_stable_frames = 0
        self.focus_lost_since = None
        self.capture_fail_streak = 0

    def sync_held(self, actual_held: bool) -> None:
        """Synchronize Controller's held state with verified physical/executor state."""
        self.actual_held = bool(actual_held)
        self.held = bool(actual_held)

    def acknowledge_action(self, action: str, ok: bool) -> None:
        """Update confirmed held state based on whether the action succeeded."""
        if action == "hold":
            self.actual_held = bool(ok)
            self.held = bool(ok)
        elif action == "release":
            self.actual_held = not bool(ok)
            self.held = not bool(ok)

    def start(self, now: float) -> None:
        self.state, self.reason = "Cast", ""
        self.cast_started = float(now)
        self.wait_started = self.absent_started = None
        self.valid_streak = self.bite_streak = self.bite_clear_streak = 0
        self.ambiguous_streak = 0
        self.ambiguous_started = None
        self.ambiguous_resume_frames = 0
        self.retry_count = 0
        self.bite_latched = False
        self.held = False
        self.desired_held = True
        self.actual_held = False
        self.previous_x = self.previous_time = None
        self.velocity = 0.0
        self.last_switch_action = None
        self.last_switch_time = None
        self.last_switch_x = None
        self.telemetry = {}
        self.pre_focus_state = None
        self.resume_stable_frames = 0
        self.focus_lost_since = None
        self.capture_fail_streak = 0

    def stop(self, reason: str = "stopped by user") -> str:
        self.desired_held = False
        action = "release" if self.held else "none"
        self.held = False
        self.actual_held = False
        self.state, self.reason = "Paused", reason
        self.pre_focus_state = None
        self.resume_stable_frames = 0
        self.focus_lost_since = None
        self.ambiguous_streak = 0
        self.ambiguous_started = None
        self.ambiguous_resume_frames = 0
        return action

    def _pause(self, reason: str) -> str:
        self.desired_held = False
        action = "release" if self.held else "none"
        self.held = False
        self.actual_held = False
        self.state, self.reason = "Paused", reason
        self.ambiguous_streak = 0
        self.ambiguous_started = None
        self.ambiguous_resume_frames = 0
        return action

    def _enter_cast(self, now: float) -> str:
        self.state, self.cast_started = "Cast", float(now)
        self.wait_started = self.absent_started = None
        self.valid_streak = self.bite_streak = self.bite_clear_streak = 0
        self.ambiguous_streak = 0
        self.ambiguous_started = None
        self.ambiguous_resume_frames = 0
        self.bite_latched = False
        self.desired_held = True
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
        self.ambiguous_started = None
        self.ambiguous_resume_frames = 0
        self.retry_count = 0
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
        self.desired_held = desired

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
            "desired_held": self.desired_held,
            "actual_held": self.actual_held,
        }

        return action

    def step(self, now: float, observation: dict[str, Any] | None, focused: bool,
             capture_time: float | None = None) -> str:
        now = float(now)

        # 1. Graceful focus handling: never immediately permanently kill Auto
        if not focused:
            self.desired_held = False
            action = "release" if self.held else "none"
            self.held = False
            self.actual_held = False
            if self.state not in ("Idle", "Paused", "FocusWait"):
                self.pre_focus_state = self.state
                self.state = "FocusWait"
                self.focus_lost_since = now
            elif self.focus_lost_since is None and self.state == "FocusWait":
                self.focus_lost_since = now
            self.reason = "waiting for game focus"
            self.previous_x = self.previous_time = None
            self.velocity = 0.0
            self.resume_stable_frames = 0
            return action

        # 2. Resuming from FocusWait when game focus returns
        if self.state == "FocusWait":
            self.focus_lost_since = None
            if observation is None:
                self.desired_held = False
                self.capture_fail_streak += 1
                if self.capture_fail_streak >= 30:
                    return self._pause("screen capture failed consistently")
                return "none"
            self.capture_fail_streak = 0
            status, x, target = self._bar(observation)
            if status == "valid" and x is not None and target is not None:
                self.resume_stable_frames += 1
                if self.resume_stable_frames < 2:
                    # Stabilizing: reset kinematics, do not click or hold on frame 1
                    self.previous_x = x
                    self.previous_time = capture_time if capture_time is not None else now
                    self.velocity = 0.0
                    self.reason = "resuming track (stabilizing)"
                    return "none"
                else:
                    # At least 2 consecutive valid frames: safely resume tracking
                    target_state = self.pre_focus_state if self.pre_focus_state in ("Track", "Wait", "End", "Cast") else "Track"
                    self.state = target_state
                    self.pre_focus_state = None
                    self.resume_stable_frames = 0
                    self.reason = "tracking resumed"
                    if target_state == "Track":
                        return self._track_step(now, x, target, capture_time=capture_time)
                    return "none"
            else:
                self.resume_stable_frames = 0
                if self.pre_focus_state == "Wait":
                    self.state = "Wait"
                    self.pre_focus_state = None
                    self.bite_streak = 0
                    return "none"
                elif self.pre_focus_state == "Cast":
                    self.state = "Cast"
                    self.pre_focus_state = None
                    self.cast_started = now
                    self.held = False
                    self.desired_held = True
                    self.actual_held = False
                    return "none"
                elif self.pre_focus_state == "End":
                    self.state = "End"
                    self.pre_focus_state = None
                    self.absent_started = now
                    return "none"
                else:
                    if self.absent_started is None:
                        self.absent_started = now
                    self.reason = "searching for target"
                    if now - self.absent_started >= 1.0:
                        self.pre_focus_state = None
                        return self._enter_cast(now)
                    return "none"

        # 3. Screen capture transient failure tolerance
        if observation is None:
            self.desired_held = False
            action = "release" if self.held else "none"
            self.held = False
            self.actual_held = False
            self.capture_fail_streak += 1
            if self.capture_fail_streak >= 30:
                return self._pause("screen capture failed consistently")
            self.reason = "screen capture failed (retrying)"
            return action
        self.capture_fail_streak = 0

        status, x, target = self._bar(observation)
        if self.state in ("Idle", "Paused"):
            self.desired_held = False
            return "none"
        if self.state == "Cast":
            if not self.held:
                self.desired_held = True
                self.held = True
                return "hold"
            if now - float(self.cast_started) >= float(self.settings["cast_seconds"]):
                self.desired_held = False
                self.held = False
                self.actual_held = False
                self.state, self.wait_started = "Wait", now
                return "release"
            self.desired_held = True
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
            self.desired_held = False
            is_net = (self.settings.get("fishing_mode") == "net")
            if not is_net and self.settings.get("bite_ack", True) and self.bite_streak >= 3 and not self.bite_latched:
                self.bite_latched = True
                return "click"
            if status == "valid" and x is not None and target is not None:
                self.retry_count = 0
                return self._track_step(now, x, target, capture_time=capture_time)
            if self.wait_started is not None and (now - float(self.wait_started) >= self.wait_timeout):
                # Only recast if confirmed absent (never recast during minigame or ambiguous frames)
                if status == "absent":
                    if self.retry_count < self.max_retries:
                        self.retry_count += 1
                        self.reason = f"wait timed out, recasting (attempt {self.retry_count}/{self.max_retries})"
                        return self._enter_cast(now)
                    else:
                        if self.continuous_recovery:
                            wait_cooldown = float(self.settings.get("continuous_wait_cooldown", 5.0))
                            if now - float(self.wait_started) >= self.wait_timeout + wait_cooldown:
                                self.retry_count = 1
                                self.reason = "wait timed out, auto-recasting"
                                return self._enter_cast(now)
                            self.reason = "wait timed out, waiting for target"
                            return "none"
                        return self._pause(f"wait timed out after {self.retry_count} retries")
                elif status == "ambiguous":
                    self.reason = "waiting for target detection to resolve"
                    return "none"
            return "none"
        if self.state == "End":
            self.desired_held = False
            if status == "valid" and x is not None and target is not None:
                self.retry_count = 0
                return self._track_step(now, x, target, capture_time=capture_time)
            if status == "ambiguous":
                # Do not treat ambiguous detection as absent to trigger premature recast
                self.reason = "waiting for unambiguous target state"
                return "none"
            if self.absent_started is None:
                self.absent_started = now
            if now - self.absent_started >= 1.0:
                return self._enter_cast(now)
            return "none"
        if self.state == "Track":
            if status == "ambiguous":
                self.ambiguous_streak += 1
                self.ambiguous_resume_frames = 0
                self.absent_started = None
                if self.ambiguous_started is None:
                    self.ambiguous_started = now
                self.previous_x = self.previous_time = None
                self.velocity = 0.0
                self.desired_held = False
                action = "release" if self.held else "none"
                self.held = False
                self.actual_held = False
                if now - self.ambiguous_started >= self.ambiguous_timeout and not self.continuous_recovery:
                    return self._pause("bar detection is ambiguous")
                self.reason = "bar detection is ambiguous (stabilizing)"
                return action

            # If recovering from ambiguous frames, require 2 consecutive valid frames
            if self.ambiguous_started is not None or self.ambiguous_streak > 0:
                if status == "valid" and x is not None and target is not None:
                    self.ambiguous_resume_frames += 1
                    if self.ambiguous_resume_frames < 2:
                        self.previous_x = x
                        self.previous_time = capture_time if capture_time is not None else now
                        self.velocity = 0.0
                        self.absent_started = None
                        self.desired_held = False
                        self.reason = "resuming track (stabilizing)"
                        return "none"
                    else:
                        self.ambiguous_started = None
                        self.ambiguous_streak = 0
                        self.ambiguous_resume_frames = 0
                        return self._track_step(now, x, target, capture_time=capture_time)
                elif self.continuous_recovery and status == "absent":
                    self.previous_x = self.previous_time = None
                    self.velocity = 0.0
                    self.ambiguous_resume_frames = 0
                    self.desired_held = False
                    action = "release" if self.held else "none"
                    self.held = False
                    self.actual_held = False
                    self.absent_started = self.absent_started if self.absent_started is not None else now
                    if now - self.absent_started < 0.25:
                        self.reason = "waiting for target detection to resolve"
                        return action
                    self.ambiguous_started = None
                    self.ambiguous_streak = 0
                else:
                    self.ambiguous_started = None
                    self.ambiguous_streak = 0
                    self.ambiguous_resume_frames = 0

            if status != "valid" or x is None or target is None:
                self.previous_x = self.previous_time = None
                self.absent_started = self.absent_started if self.absent_started is not None else now
                self.desired_held = False
                action = "release" if self.held else "none"
                self.held = False
                self.actual_held = False
                if now - self.absent_started < self.absent_timeout:
                    self.reason = "target temporarily lost"
                    return action
                self.state = "End"
                return action
            return self._track_step(now, x, target, capture_time=capture_time)
        return "none"


# Alias for backward compatibility
FishingController = Controller


def _load_pyautogui():
    global _pyautogui
    if _pyautogui is None:
        import pyautogui
        _pyautogui = pyautogui
    return _pyautogui


def raw_release_mouse() -> ActionResult:
    """Release the left button via PyAutoGUI, returning a structured ActionResult."""
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
            _mouse_held = False
            return ActionResult(True, "ok", "ปล่อยเมาส์สำเร็จ")
        except Exception as exc:
            _mouse_held = False
            return ActionResult(False, "mouse_up_failed", f"ปล่อยเมาส์ล้มเหลว: {exc}", exc)


def emergency_release_mouse() -> None:
    """Safely release the mouse during exit/hotkey/error without raising exceptions."""
    try:
        raw_release_mouse()
    except Exception:
        pass


def release_mouse() -> ActionResult:
    """Backward-compatible release_mouse returning ActionResult while not raising."""
    return raw_release_mouse()


def _inside(point: tuple[int, int], bounds: tuple[int, int, int, int]) -> bool:
    x, y, w, h = bounds
    return x <= point[0] < x + w and y <= point[1] < y + h


class ActionResult:
    """Structured result of an input action execution."""

    def __init__(
        self,
        ok: bool,
        reason: str = "ok",
        detail: str = "",
        exception: Exception | None = None,
        winerror: int | None = None,
    ):
        self.ok = ok
        self.reason = reason
        self.detail = detail
        self.exception = exception
        self.winerror = winerror

    def __bool__(self) -> bool:
        return bool(self.ok)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "exception": str(self.exception) if self.exception else None,
            "winerror": self.winerror,
        }

    def __repr__(self) -> str:
        return f"ActionResult(ok={self.ok}, reason={self.reason!r}, detail={self.detail!r})"


def apply_action(
    action: str,
    target_snapshot: tuple[int, tuple[int, int, int, int]] | None,
    safe_move: bool = False,
) -> ActionResult:
    """Apply one state transition only when the target window still matches."""
    global _mouse_held
    with _input_lock:
        if action in ("none", ""):
            return ActionResult(True, "ok", "ไม่มีคำสั่ง")
        if action == "release":
            return release_mouse()
        if target_snapshot is None:
            release_mouse()
            return ActionResult(False, "target_missing", "ยังไม่ได้เลือกหน้าต่างเกม")
        target_wid, target_bounds = target_snapshot
        snap = window_snapshot()
        if snap is None:
            release_mouse()
            if not is_window_alive(target_wid):
                return ActionResult(False, "target_closed", "หน้าต่างเกมถูกปิดแล้ว")
            return ActionResult(False, "target_not_foreground", "ไม่พบหน้าต่างที่อยู่ด้านหน้า")
        if snap[0] != target_wid:
            release_mouse()
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
            release_mouse()
            return ActionResult(False, "pyautogui_import_failed", f"โหลด PyAutoGUI ไม่สำเร็จ: {exc}", exc)

        try:
            mouse_pos = tuple(pyautogui.position())
        except Exception as exc:
            release_mouse()
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
                    release_mouse()
                    return ActionResult(
                        False,
                        "cursor_outside_target",
                        f"เลื่อนเมาส์เข้าเกมไม่สำเร็จ: {exc}",
                        exc,
                    )

            if not _inside(mouse_pos, current_bounds):
                release_mouse()
                return ActionResult(
                    False,
                    "cursor_outside_target",
                    "เมาส์อยู่นอกหน้าต่าง Roblox — กรุณาวางเมาส์ในเกม",
                )

        try:
            if action == "hold":
                if not _mouse_held:
                    old_failsafe = getattr(pyautogui, "FAILSAFE", True)
                    try:
                        pyautogui.FAILSAFE = False
                        pyautogui.mouseDown(button="left")
                    finally:
                        pyautogui.FAILSAFE = old_failsafe
                    _mouse_held = True
                return ActionResult(True, "ok", "กดค้างสำเร็จ")
            if action == "click":
                old_failsafe = getattr(pyautogui, "FAILSAFE", True)
                try:
                    pyautogui.FAILSAFE = False
                    pyautogui.click(button="left")
                finally:
                    pyautogui.FAILSAFE = old_failsafe
                return ActionResult(True, "ok", "คลิกสำเร็จ")
        except Exception as exc:
            release_mouse()
            exc_str = str(exc).lower()
            if "access denied" in exc_str or "permission" in exc_str or getattr(exc, "winerror", None) == 5:
                return ActionResult(
                    False,
                    "input_blocked_by_privilege_level",
                    "การส่งอินพุตถูกบล็อกโดยระบบสิทธิ์ Windows (UIPI) — กรุณารัน Auto Fishing ด้วยสิทธิ์ Administrator",
                    exc,
                )
            reason = "mouse_down_failed" if action == "hold" else "click_failed"
            return ActionResult(False, reason, f"ส่งคำสั่งเมาส์ล้มเหลว: {exc}", exc)
        return ActionResult(False, "unknown_action", f"คำสั่งไม่รู้จัก: {action}")


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


def is_window_alive(wid: int) -> bool:
    """Verify whether a window handle or X11 ID is still valid in the OS."""
    if not wid:
        return False
    if platform.system() == "Windows":
        try:
            return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(wid)))
        except Exception:
            return False
    if not os.environ.get("DISPLAY") or (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        return False
    try:
        from Xlib import display
        dpy = display.Display()
        client = dpy.create_resource_object("window", wid)
        _ = client.get_geometry()
        dpy.close()
        return True
    except Exception:
        return False


def get_window_bounds(wid: int) -> tuple[int, int, int, int] | None:
    """Fetch current bounds (x, y, w, h) of target window from OS."""
    if not wid:
        return None
    if platform.system() == "Windows":
        try:
            user32 = ctypes.windll.user32
            hwnd = ctypes.c_void_p(wid)
            if not user32.IsWindow(hwnd):
                return None
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rect = RECT()
            point = POINT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)) or not user32.ClientToScreen(hwnd, ctypes.byref(point)):
                return None
            return (int(point.x), int(point.y), int(rect.right - rect.left), int(rect.bottom - rect.top))
        except Exception:
            return None
    if not os.environ.get("DISPLAY") or (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        return None
    try:
        from Xlib import display
        dpy = display.Display()
        root = dpy.screen().root
        client = dpy.create_resource_object("window", wid)
        geometry = client.get_geometry()
        translated = root.translate_coords(client, 0, 0)
        result = (int(translated.x), int(translated.y), int(geometry.width), int(geometry.height))
        dpy.close()
        return result
    except Exception:
        return None


def focus_window(wid: int) -> bool:
    """Request the operating system to bring the target window to foreground."""
    if not wid:
        return False
    if platform.system() == "Windows":
        try:
            user32 = ctypes.windll.user32
            hwnd = ctypes.c_void_p(wid)
            if not user32.IsWindow(hwnd):
                return False
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            fg = user32.GetForegroundWindow()
            fg_val = fg.value if isinstance(fg, ctypes.c_void_p) else fg
            if int(fg_val or 0) == wid:
                return True
            time.sleep(0.05)
            fg = user32.GetForegroundWindow()
            fg_val = fg.value if isinstance(fg, ctypes.c_void_p) else fg
            return int(fg_val or 0) == wid
        except Exception:
            return False
    if not os.environ.get("DISPLAY") or (os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        return False
    try:
        from Xlib import X, display, protocol
        dpy = display.Display()
        root = dpy.screen().root
        atom = dpy.intern_atom("_NET_ACTIVE_WINDOW")
        data = [1, X.CurrentTime, 0, 0, 0]
        event = protocol.event.ClientMessage(
            window=wid,
            client_type=atom,
            data=(32, data)
        )
        root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        dpy.flush()
        dpy.close()
        return True
    except Exception:
        try:
            subprocess.run(["xdotool", "windowactivate", str(wid)], check=False, timeout=0.5)
            return True
        except Exception:
            return False


def _inside_rect(sub_rect: tuple[int, int, int, int] | list[int], parent_rect: tuple[int, int, int, int] | list[int]) -> bool:
    """Return True if sub_rect is completely enclosed inside parent_rect."""
    sx, sy, sw, sh = sub_rect
    px, py, pw, ph = parent_rect
    return sx >= px and sy >= py and (sx + sw) <= (px + pw) and (sy + sh) <= (py + ph)


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


def get_virtual_screen_bounds() -> tuple[int, int, int, int]:
    """Return the total virtual screen bounds covering all connected monitors."""
    if platform.system() == "Windows":
        try:
            user32 = ctypes.windll.user32
            # SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77, SM_CXVIRTUALSCREEN = 78, SM_CYVIRTUALSCREEN = 79
            vx = int(user32.GetSystemMetrics(76))
            vy = int(user32.GetSystemMetrics(77))
            vw = int(user32.GetSystemMetrics(78))
            vh = int(user32.GetSystemMetrics(79))
            if vw > 0 and vh > 0:
                return (vx, vy, vw, vh)
        except Exception:
            pass
    try:
        import mss
        mss_cls = getattr(mss, "MSS", getattr(mss, "mss", None))
        with mss_cls() as capture:
            monitors = capture.monitors
            if monitors:
                m0 = monitors[0]
                return (int(m0["left"]), int(m0["top"]), int(m0["width"]), int(m0["height"]))
    except Exception:
        pass
    try:
        return primary_screen_bounds()
    except Exception:
        return (0, 0, 1920, 1080)


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
    to_save = dict(settings)
    # Sync platform namespaces before writing to disk
    if platform.system() == "Windows":
        if "windows" not in to_save or not isinstance(to_save["windows"], dict):
            to_save["windows"] = {}
        if "rois" in to_save:
            to_save["windows"]["rois"] = dict(to_save["rois"])
    else:
        if "linux" not in to_save or not isinstance(to_save["linux"], dict):
            to_save["linux"] = {}
        to_save["linux"]["env_mode"] = to_save.get("env_mode", "desktop")
        to_save["linux"]["gamescope_fps"] = to_save.get("gamescope_fps", 60)
        to_save["linux"]["gamescope_res"] = to_save.get("gamescope_res", "1280x720")
        to_save["linux"]["gamescope_fullscreen"] = to_save.get("gamescope_fullscreen", False)
        if to_save.get("gamescope_rois"):
            to_save["linux"]["gamescope_rois"] = dict(to_save["gamescope_rois"])
        if to_save.get("env_mode") == "gamescope":
            if "rois" in to_save:
                to_save["linux"]["gamescope_rois"] = dict(to_save["rois"])
        else:
            if "rois" in to_save:
                to_save["linux"]["desktop_rois"] = dict(to_save["rois"])

    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(to_save, handle, indent=2)
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
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    result = dict(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        result.update({k: v for k, v in data.items() if k not in ("linux", "windows")})
        # Windows namespace migration
        win_data = data.get("windows") if isinstance(data.get("windows"), dict) else {}
        win_rois = win_data.get("rois") or data.get("windows_rois") or data.get("rois") or {"bar": None, "bite": None}
        result["windows"] = {
            "rois": dict(win_rois) if isinstance(win_rois, dict) else {"bar": None, "bite": None}
        }
        # Linux namespace migration
        lin_data = data.get("linux") if isinstance(data.get("linux"), dict) else {}
        lin_env = lin_data.get("env_mode") or data.get("env_mode", "desktop")
        lin_desktop_rois = lin_data.get("desktop_rois") or data.get("rois") or {"bar": None, "bite": None}
        lin_gs_rois = lin_data.get("gamescope_rois") or data.get("gamescope_rois") or {"bar": None, "bite": None}
        vk_override = lin_data.get("gamescope_vk_device_override") or data.get("gamescope_vk_device_override")
        result["linux"] = {
            "env_mode": str(lin_env),
            "desktop_rois": dict(lin_desktop_rois) if isinstance(lin_desktop_rois, dict) else {"bar": None, "bite": None},
            "gamescope_rois": dict(lin_gs_rois) if isinstance(lin_gs_rois, dict) else {"bar": None, "bite": None},
            "gamescope_fps": int(lin_data.get("gamescope_fps", data.get("gamescope_fps", 60))),
            "gamescope_res": str(lin_data.get("gamescope_res", data.get("gamescope_res", "1280x720"))),
            "gamescope_fullscreen": bool(lin_data.get("gamescope_fullscreen", data.get("gamescope_fullscreen", False))),
            "gamescope_vk_device_override": str(vk_override).strip() if vk_override else None,
            "auto_restart_30s": bool(lin_data.get("auto_restart_30s", data.get("auto_restart_30s", True))),
            "auto_restart_interval": int(lin_data.get("auto_restart_interval", data.get("auto_restart_interval", 30))),
        }

    # Mirror active platform settings to top-level keys
    if platform.system() == "Windows":
        result["rois"] = dict(result["windows"]["rois"])
    else:
        result["env_mode"] = result["linux"]["env_mode"]
        result["gamescope_fps"] = result["linux"]["gamescope_fps"]
        result["gamescope_res"] = result["linux"]["gamescope_res"]
        result["gamescope_fullscreen"] = result["linux"]["gamescope_fullscreen"]
        result["gamescope_vk_device_override"] = result["linux"]["gamescope_vk_device_override"]
        result["gamescope_rois"] = dict(result["linux"]["gamescope_rois"])
        if result["linux"]["env_mode"] == "gamescope":
            result["rois"] = dict(result["linux"]["gamescope_rois"])
        else:
            result["rois"] = dict(result["linux"]["desktop_rois"])

    return result


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


class BaseFishingApp:
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
        self._global_hotkey_cleanup = None
        self._last_f8_time = 0.0
        self._closing = False
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
        self.recent_events = collections.deque(maxlen=20)
        self._last_auto_refocus_time = 0.0
        self._relative_rois = {}
        mode = self.settings.get("fishing_mode", "rod")
        if mode not in MODE_CONFIGS:
            mode = "rod"
        self.current_mode = mode
        self._init_vars()
        self.status = tk.StringVar(value="พร้อม — เลือกพื้นที่บนหน้าจอเพื่อเริ่ม")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._init_global_hotkey()

    def _init_vars(self):
        self.vars = {
            "fishing_mode": self.tk.StringVar(value=self.current_mode),
            "cast_seconds": self.tk.StringVar(value=str(self.settings.get("cast_seconds", DEFAULT_SETTINGS["cast_seconds"]))),
            "margin": self.tk.StringVar(value=str(self.settings.get("margin", DEFAULT_SETTINGS["margin"]))),
            "lead": self.tk.StringVar(value=str(self.settings.get("lead", DEFAULT_SETTINGS["lead"]))),
            "threshold": self.tk.StringVar(value=str(self.settings.get("bite_threshold", DEFAULT_SETTINGS["bite_threshold"]))),
            "right": self.tk.BooleanVar(value=bool(self.settings.get("right_on_hold", DEFAULT_SETTINGS["right_on_hold"]))),
            "ack": self.tk.BooleanVar(value=bool(self.settings.get("bite_ack", DEFAULT_SETTINGS["bite_ack"]))),
            "debug": self.tk.BooleanVar(value=bool(self.settings.get("debug", DEFAULT_SETTINGS.get("debug", False)))),
            "auto_refocus": self.tk.BooleanVar(value=bool(self.settings.get("auto_refocus", False))),
            "auto_restart_30s": self.tk.BooleanVar(value=bool(self.settings.get("auto_restart_30s", True))),
            "auto_restart_interval": self.tk.StringVar(value=str(self.settings.get("auto_restart_interval", 30))),
            "test_hold_duration": self.tk.StringVar(value="1.0"),
        }

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

    def _restore_mode_preview(self):
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

    def _build_mode_panel(self, outer):
        """Platform-specific hook to insert mode selection or requirement card."""
        pass

    def _build_advanced_tuning(self, tuning):
        """Platform-specific hook to add tuning controls."""
        pass

    def _build_advanced_extra(self, advanced_frame):
        """Platform-specific hook to add extra settings/notices."""
        pass

    def _on_built(self):
        """Platform-specific hook executed at the end of _build()."""
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
        self.root.bind("<F8>", lambda event: self._on_f8_pressed())

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

        # 3. Control Panel
        self._build_mode_panel(outer)

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

        s1_focus_row = self.tk.Frame(step1_card, bg=BG_CARD)
        s1_focus_row.pack(fill="x", pady=(6, 0))
        self.cb_auto_refocus = self.ttk.Checkbutton(
            s1_focus_row,
            text="ดึงเกมกลับมาโฟกัสอัตโนมัติ",
            variable=self.vars["auto_refocus"],
        )
        self.cb_auto_refocus.pack(side="left")
        self.btn_focus_now = self.ttk.Button(
            s1_focus_row,
            text="🎯  โฟกัสเกมตอนนี้",
            style="Secondary.TButton",
            command=self._focus_target_window,
        )
        self.btn_focus_now.pack(side="right")

        self.lbl_focus_hint = self.tk.Label(
            step1_card,
            text="* หากเปิดดึงโฟกัสอัตโนมัติ ระบบจะดึงเกมขึ้นมาด้านหน้าเมื่อเสียโฟกัส >0.5s (อาจสลับออกจากโปรแกรมอื่นที่กำลังใช้งาน)",
            fg=TEXT_HINT,
            bg=BG_CARD,
            font=(self.ui_font, 8),
            wraplength=550,
            justify="left",
        )
        self.lbl_focus_hint.pack(anchor="w", pady=(2, 0))

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

        restart_row = self.tk.Frame(step3_card, bg=BG_CARD)
        restart_row.pack(fill="x", pady=(2, 4))
        self.auto_restart_cb = self.ttk.Checkbutton(
            restart_row,
            text="🔄 หยุดและเริ่มใหม่อัตโนมัติทุกๆ",
            variable=self.vars["auto_restart_30s"],
            command=self._on_auto_restart_toggle,
        )
        self.auto_restart_cb.pack(side="left")

        self.auto_restart_entry = self.ttk.Combobox(
            restart_row,
            textvariable=self.vars["auto_restart_interval"],
            values=("10", "15", "20", "25", "30", "45", "60", "90", "120"),
            width=4,
            justify="center",
        )
        self.auto_restart_entry.pack(side="left", padx=(4, 4))
        self.auto_restart_entry.bind("<<ComboboxSelected>>", lambda _e: self._on_auto_restart_interval_change())
        self.auto_restart_entry.bind("<FocusOut>", lambda _e: self._on_auto_restart_interval_change())
        self.auto_restart_entry.bind("<Return>", lambda _e: self._on_auto_restart_interval_change())

        self.auto_restart_unit_lbl = self.tk.Label(
            restart_row,
            text="วินาที (รีเฟรชการ Track อัตโนมัติ)",
            fg=TEXT_MUTED,
            bg=BG_CARD,
            font=(self.ui_font, 9),
        )
        self.auto_restart_unit_lbl.pack(side="left")

        if not bool(self.vars["auto_restart_30s"].get()):
            self.auto_restart_entry.configure(state="disabled")

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

        self.tk.Label(tuning, text="รอบรีเซ็ตอัตโนมัติ (วินาที)", fg=TEXT_MAIN, bg=BG_CARD,
                      font=(self.ui_font, 10)).grid(row=5, column=0, sticky="w", pady=3)
        restart_box = self.ttk.Combobox(tuning, textvariable=self.vars["auto_restart_interval"],
                                        values=("10", "15", "20", "25", "30", "45", "60", "90", "120"), width=7)
        restart_box.grid(row=5, column=1, sticky="w", padx=(8, 16), pady=3)
        restart_box.bind("<<ComboboxSelected>>", lambda _e: self._on_auto_restart_interval_change())
        restart_box.bind("<FocusOut>", lambda _e: self._on_auto_restart_interval_change())
        restart_box.bind("<Return>", lambda _e: self._on_auto_restart_interval_change())

        self._build_advanced_tuning(tuning)

        cb_frame = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        cb_frame.pack(fill="x", pady=(4, 4))
        self.ttk.Checkbutton(cb_frame, text="คลิกยืนยันเมื่อปลากินเบ็ด", variable=self.vars["ack"]).pack(anchor="w", pady=1)
        self.ttk.Checkbutton(cb_frame, text="แสดงข้อมูลติดตามละเอียดบนภาพ Preview (ดีบัก)", variable=self.vars["debug"]).pack(anchor="w", pady=1)

        extra_row = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        extra_row.pack(fill="x", pady=(6, 2))
        self.btn_test_hold = self.ttk.Button(extra_row, text="⚡ ทดลองกดค้าง", style="Secondary.TButton", command=self.test_hold)
        self.btn_test_hold.pack(side="left")
        self.test_hold_combo = self.ttk.Combobox(
            extra_row,
            textvariable=self.vars["test_hold_duration"],
            values=["0.5", "1.0", "2.0"],
            width=5,
            state="readonly",
        )
        self.test_hold_combo.pack(side="left", padx=(4, 4))
        self.tk.Label(extra_row, text="วินาที", fg=TEXT_MUTED, bg=BG_CARD, font=(self.ui_font, 9)).pack(side="left")
        self.ttk.Button(extra_row, text="เลือกพื้นที่เพิ่มเติม...", style="Secondary.TButton", command=self.select_rois).pack(side="right")

        diag_row = self.tk.Frame(self.advanced_frame, bg=BG_CARD)
        diag_row.pack(fill="x", pady=(4, 2))
        self.ttk.Button(diag_row, text="📋 คัดลอกผลวินิจฉัย (Diagnostics)", style="Secondary.TButton", command=self.copy_diagnostics).pack(side="left")
        self.ttk.Button(diag_row, text="📂 เปิดโฟลเดอร์ Log", style="Secondary.TButton", command=self.open_log_folder).pack(side="right")
        self._build_advanced_extra(self.advanced_frame)

        self._draw_preview_placeholder()
        self._update_readiness()
        self._on_built()

    def toggle_advanced(self):
        if self.advanced_visible:
            self.advanced_frame.pack_forget()
            self.advanced_btn.configure(text="⚙️  ตั้งค่าขั้นสูง  ▸")
        else:
            self.advanced_frame.pack(fill="x", pady=(8, 0))
            self.advanced_btn.configure(text="⚙️  ตั้งค่าขั้นสูง  ▾")
        self.advanced_visible = not self.advanced_visible

    def _init_global_hotkey(self):
        if getattr(self, "_global_hotkey_cleanup", None):
            return
        try:
            self._global_hotkey_cleanup = register_stop(self._on_global_hotkey_signal)
        except Exception as exc:
            self._global_hotkey_cleanup = None
            self._log_debug_event("global_hotkey_init_failed", {"error": str(exc)})

    def _on_global_hotkey_signal(self):
        try:
            if hasattr(self, "root") and self.root.winfo_exists():
                self.root.after(0, self._on_f8_pressed)
        except Exception:
            pass

    def _on_f8_pressed(self):
        if getattr(self, "_closing", False):
            return
        now = time.monotonic()
        if now - getattr(self, "_last_f8_time", 0.0) < 0.35:
            return
        self._last_f8_time = now
        self._toggle_start_stop()

    def _toggle_start_stop(self):
        if getattr(self, "_closing", False):
            return
        if self.running or getattr(self, "start_pending", False) or getattr(self, "_pending_restart_job", None) is not None:
            self.stop_requested.set()
            self.stop("หยุดด้วย F8")
        elif getattr(self, "test_hold_pending", False) or getattr(self, "test_hold_active", False):
            self.stop_requested.set()
            self.stop("หยุดด้วย F8")
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
        state_names = {
            "Idle": "พร้อม",
            "Cast": "เหวี่ยงเบ็ด",
            "Wait": "รอปลา",
            "Track": "คุมแถบ",
            "End": "รอยืนยันจบรอบ",
            "Paused": "พักการทำงาน",
            "FocusWait": "รอโฟกัสเกม",
        }
        reason_names = {
            "tracking": "กำลังอ่านภาพเกม",
            "stopped by user": "หยุดโดยผู้ใช้",
            "focus lost; press Start to resume": "ไม่พบโฟกัสเกม",
            "waiting for game focus": "รอโฟกัสเกม",
            "resuming track (stabilizing)": "รอความนิ่งของเป้าหมาย",
            "tracking resumed": "กลับมา Track แล้ว",
            "screen capture failed": "อ่านภาพหน้าจอไม่สำเร็จ",
            "screen capture failed (retrying)": "จับภาพไม่ได้ — กำลังลองใหม่",
            "bar detection is ambiguous": "พบแถบหลายตำแหน่ง จึงหยุดเพื่อความปลอดภัย",
            "bar detection is ambiguous (stabilizing)": "ภาพแถบไม่ชัดเจน — ปล่อยเมาส์รอความนิ่ง",
            "target window moved or pointer left it": "หน้าต่างเกมย้ายตำแหน่งหรือตัวชี้ออกนอกพื้นที่",
            "target window lost or unmapped inside Gamescope": "ไม่พบหน้าต่างเกมในจอ Gamescope",
            "target window closed": "หน้าต่างเกมถูกปิด",
            "target window closed or destroyed": "หน้าต่างเกมถูกปิดหรือถูกทำลาย",
            "window capture failed": "จับภาพหน้าต่างเกมไม่สำเร็จ",
            "heartbeat timeout from UI": "ขาดการติดต่อกับหน้าต่างหลัก",
            "waiting for unambiguous target state": "รอความชัดเจนของเป้าหมาย",
            "input injection failed repeatedly": "ส่งคำสั่งเมาส์ซ้ำๆ ไม่สำเร็จ",
            "waiting for Gamescope target window": "รอภาพหน้าต่างเกมใน Gamescope",
            "waiting for Gamescope frame capture": "รอภาพจาก Gamescope",
            "target temporarily lost": "เป้าหมายคลาดเคลื่อนชั่วคราว — กำลังตรวจจับใหม่",
            "wait timed out, auto-recasting": "หมดเวลารอ — กำลังเหวี่ยงเบ็ดใหม่",
        }
        display = str(text)
        if display in reason_names:
            display = reason_names[display]
        elif display.startswith("wait timed out"):
            display = "รอปลานานเกินกำหนด: " + display
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

        # Update visual status pill and guidance text
        if hasattr(self, "status_pill") and hasattr(self, "status_text_lbl") and hasattr(self, "status_dot"):
            if any(k in display for k in ("เริ่มไม่ได้", "เกิดข้อผิดพลาด", "ไม่ได้", "error")):
                self.status_pill.configure(bg="#450a0a")
                self.status_dot.configure(bg="#450a0a")
                self.status_dot.itemconfig(self.status_dot_id, fill="#ef4444")
                self.status_text_lbl.configure(text="ข้อผิดพลาด", fg="#f87171")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text=f"🔴  {display}", fg="#ef4444")
            elif any(k in display for k in ("รอโฟกัส", "เมาส์อยู่นอก", "พื้นที่ Track อยู่นอก", "ลองใหม่")):
                self.status_pill.configure(bg="#451a03")
                self.status_dot.configure(bg="#451a03")
                self.status_dot.itemconfig(self.status_dot_id, fill="#f59e0b")
                self.status_text_lbl.configure(text="รอความพร้อม", fg="#fbbf24")
                if hasattr(self, "action_guidance_lbl"):
                    self.action_guidance_lbl.configure(text=f"🟡  {display}", fg="#f59e0b")
            elif any(k in display for k in ("กำลังทำงาน", "คุมแถบ", "เหวี่ยงเบ็ด", "รอปลา", "กลับมา Track")):
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
    def _update_readiness(self):
        if not hasattr(self, "target_summary"):
            return

        if hasattr(self, "btn_select_window"):
            self.btn_select_window.pack(side="right")
        if hasattr(self, "btn_select_roi"):
            self.btn_select_roi.configure(text="🎯  เลือกพื้นที่บนหน้าจอ")
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
                if hasattr(self, "action_guidance_lbl") and self.status_text_lbl.cget("text") != "ข้อผิดพลาด":
                    self.action_guidance_lbl.configure(text="⚠️  กรุณาเลือกพื้นที่บนหน้าจอในขั้นตอนที่ 1 ก่อนเริ่ม", fg="#f59e0b")

    def _read_ui(self):
        try:
            interval = int(str(self.vars["auto_restart_interval"].get()).strip()) if "auto_restart_interval" in self.vars else 30
            interval = max(5, min(600, interval))
        except Exception:
            interval = 30
        return {
            "fishing_mode": str(self.vars["fishing_mode"].get()),
            "cast_seconds": float(self.vars["cast_seconds"].get()),
            "margin": float(self.vars["margin"].get()),
            "lead": float(self.vars["lead"].get()),
            "bite_threshold": float(self.vars["threshold"].get()),
            "right_on_hold": bool(self.vars["right"].get()),
            "bite_ack": bool(self.vars["ack"].get()),
            "debug": bool(self.vars["debug"].get()),
            "auto_restart_30s": bool(self.vars["auto_restart_30s"].get()) if "auto_restart_30s" in self.vars else True,
            "auto_restart_interval": interval,
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

    def _save_roi_selection(self, name, area, bounds, image):
        if name == "template":
            try:
                crop_x = max(0, area[0] - bounds[0])
                crop_y = max(0, area[1] - bounds[1])
                crop = image[crop_y : crop_y + area[3], crop_x : crop_x + area[2]]
                if cv2 is not None:
                    TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(TEMPLATE_FILE), crop)
            except Exception:
                pass
            self._set_status("บันทึกภาพตัวอย่างสัญญาณปลากินเบ็ดแล้ว")
        else:
            self.settings.setdefault("rois", {})[name] = area
            if self.target and len(bounds) == 4:
                wx, wy = bounds[0], bounds[1]
                if not hasattr(self, "_relative_rois"):
                    self._relative_rois = {}
                self._relative_rois[name] = (area[0] - wx, area[1] - wy, area[2], area[3])
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

    def _capture_area(self, name):
        try:
            if self.target and len(self.target) >= 2 and is_window_alive(self.target[0]):
                cur_bounds = get_window_bounds(self.target[0]) or self.target[1]
                bounds = cur_bounds
            else:
                bounds = get_virtual_screen_bounds()
            capture = ScreenCapture()
            image = capture.grab(bounds)
            capture.close()
        except Exception as exc:
            self.root.deiconify()
            self._set_status(f"จับภาพพื้นที่ไม่ได้: {exc}")
            return
        self._open_roi_canvas(image, bounds, name)

    def _open_roi_canvas(self, image, bounds, name):
        area_names = {"bar": "แถบมินิเกม", "cast": "แถบเหวี่ยงเบ็ด", "bite": "สัญญาณปลากินเบ็ด", "template": "ภาพตัวอย่างสัญญาณ"}
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
            if y0 > 0:
                canvas.create_rectangle(0, 0, canvas_w, y0, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
            if y1 < canvas_h:
                canvas.create_rectangle(0, y1, canvas_w, canvas_h, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
            if x0 > 0:
                canvas.create_rectangle(0, y0, x0, y1, fill="#000000", stipple="gray50", outline="", tags="roi_dim")
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

            actual_w = max(1, int(round(w * img_w / canvas_w)))
            actual_h = max(1, int(round(h * img_h / canvas_h)))
            dim_text = f"{actual_w} × {actual_h} px"

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
            confirm()

        def confirm(event=None):
            area = selected_area[0]
            if not area:
                return
            self._save_roi_selection(name, area, bounds, image)
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
        target_bounds = None
        if self.target and len(self.target) >= 2:
            target_bounds = self.target[1]
        if not target_bounds:
            target_bounds = get_virtual_screen_bounds()
        return validate_settings(self._read_ui(), target_bounds)

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
            self._set_status("ทดลองกดค้างไม่ได้: ต้องเลือกหน้าต่างเกมก่อน")
            return
        try:
            dur_str = self.vars["test_hold_duration"].get() if "test_hold_duration" in self.vars else "1.0"
            self.test_hold_duration = float(dur_str)
        except Exception:
            self.test_hold_duration = 1.0
        try:
            self.stop_requested.clear()
            if not getattr(self, "_global_hotkey_cleanup", None):
                self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self.test_hold_pending = True
            self.test_hold_countdown = 3
            self._set_status("สลับไปที่เกมภายใน 3 วินาทีเพื่อทดลองกดค้าง (F8 เพื่อยกเลิก)")
            self.test_hold_id = self.root.after(1000, self._update_test_hold_countdown)
        except Exception as exc:
            self.test_hold_pending = False
            if hasattr(self, "executor"):
                self.executor.emergency_release()
            else:
                release_mouse()
            self._set_status(f"ทดลองกดค้างไม่ได้: {exc}")

    def _update_test_hold_countdown(self):
        if not getattr(self, "test_hold_pending", False):
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return
        if self.test_hold_countdown > 0:
            self._set_status(f"สลับไปที่เกม Roblox ภายใน {self.test_hold_countdown} วินาทีเพื่อทดลองกดค้าง (F8 เพื่อยกเลิก)")
            self.test_hold_countdown -= 1
            self.test_hold_id = self.root.after(1000, self._update_test_hold_countdown)
        else:
            self._begin_test_hold()

    def _begin_test_hold(self):
        if not getattr(self, "test_hold_pending", False):
            return
        self.test_hold_pending = False
        self.test_hold_id = None
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return
        if not self.target:
            self.stop("ยังไม่ได้เลือกหน้าต่างเกม")
            return

        target_wid = self.target[0]
        if not is_window_alive(target_wid):
            self.stop("หน้าต่างเกมถูกปิดแล้ว")
            return

        snap = window_snapshot()
        if snap is None or snap[0] != target_wid:
            self.stop("ทดลองกดค้างไม่ได้: หน้าต่าง Roblox ไม่ได้อยู่ด้านหน้า")
            return

        # Update bounds if window moved
        self.target = snap

        if hasattr(self, "executor"):
            res = self.executor.apply("hold", self.target, safe_move=True)
        else:
            res = apply_action("hold", self.target, safe_move=True)

        if not res:
            self.stop(f"ทดลองกดค้างล้มเหลว: {res.detail or res.reason}")
            return

        self.test_hold_active = True
        dur = getattr(self, "test_hold_duration", 1.0)
        self._set_status(f"กำลังกดค้าง {dur:.1f} วินาที...")
        ms = max(50, int(dur * 1000))
        self.test_hold_id = self.root.after(ms, self._finish_test_hold)

    def _finish_test_hold(self):
        try:
            self.test_hold_active = False
            self.test_hold_id = None
        finally:
            if hasattr(self, "executor"):
                self.executor.emergency_release()
            else:
                release_mouse()
            if self.hotkey_cleanup:
                try:
                    self.hotkey_cleanup()
                except Exception:
                    pass
                self.hotkey_cleanup = None
        dur = getattr(self, "test_hold_duration", 1.0)
        backend = getattr(getattr(self, "executor", None), "backend", "pyautogui").upper()
        target_wid = self.target[0] if self.target else None
        snap = window_snapshot()
        fg_wid = snap[0] if snap else None
        if self.stop_requested.is_set():
            self._set_status("หยุดฉุกเฉินด้วย F8")
        else:
            self._set_status(
                f"✅ [{backend}] ทดลองกดค้างและปล่อยแล้ว (เป้าหมาย HWND {target_wid} | หน้าต่างหน้า HWND {fg_wid} | กดค้าง {dur:.1f}s)"
            )

    def copy_diagnostics(self) -> None:
        try:
            summary = self._collect_diagnostic_summary()
            self.root.clipboard_clear()
            self.root.clipboard_append(summary)
            self._set_status("📋 คัดลอกผลวินิจฉัยลง Clipboard แล้ว")
        except Exception as exc:
            self._set_status(f"คัดลอกผลวินิจฉัยไม่ได้: {exc}")

    def open_log_folder(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows":
                os.startfile(str(DATA_DIR))
            else:
                subprocess.run(["xdg-open", str(DATA_DIR)], check=False)
            self._set_status("📂 เปิดโฟลเดอร์ Log แล้ว")
        except Exception as exc:
            self._set_status(f"เปิดโฟลเดอร์ Log ไม่ได้: {exc}")

    def _collect_diagnostic_summary(self) -> str:
        snap = window_snapshot()
        lines = [
            f"=== Roblox Auto Fishing Diagnostic Report ===",
            f"Version: {APP_VERSION} (Build {BUILD_ID})",
            f"OS: {platform.system()} {platform.release()} ({platform.architecture()[0]})",
            f"Mode: {getattr(self, 'env_mode', 'desktop')}",
            f"Target HWND: {self.target[0] if self.target else None}",
            f"Target Bounds: {self.target[1] if self.target else None}",
            f"Foreground HWND: {snap[0] if snap else None}",
            f"Controller State: {getattr(self.controller, 'state', None)}",
            f"Desired Held: {getattr(self.controller, 'desired_held', None)}",
            f"Actual Held: {getattr(getattr(self, 'executor', None), 'actual_held', _mouse_held)}",
            f"Executor Backend: {getattr(getattr(self, 'executor', None), 'backend', 'N/A')}",
            f"Failure Streak: {getattr(self, 'input_failure_streak', 0)}",
            f"Log File: {DEBUG_LOG_FILE}",
        ]
        if hasattr(self, "recent_events") and self.recent_events:
            lines.append("--- Recent Events ---")
            for ev in list(self.recent_events)[-10:]:
                lines.append(f"  {ev}")
        return "\n".join(lines)

    def _cancel_auto_restart(self) -> None:
        if getattr(self, "_auto_restart_job", None) is not None:
            try:
                self.root.after_cancel(self._auto_restart_job)
            except Exception:
                pass
            self._auto_restart_job = None
        if getattr(self, "_pending_restart_job", None) is not None:
            try:
                self.root.after_cancel(self._pending_restart_job)
            except Exception:
                pass
            self._pending_restart_job = None

    def _schedule_auto_restart(self) -> None:
        self._cancel_auto_restart()
        if not self.running or getattr(self, "_closing", False) or self.stop_requested.is_set():
            return
        enabled = True
        if hasattr(self, "vars") and "auto_restart_30s" in self.vars:
            enabled = bool(self.vars["auto_restart_30s"].get())
        elif isinstance(self.settings, dict):
            enabled = bool(self.settings.get("auto_restart_30s", True))
        if not enabled:
            return

        interval = 30
        if hasattr(self, "vars") and "auto_restart_interval" in self.vars:
            try:
                interval = max(5, int(self.vars["auto_restart_interval"].get()))
            except Exception:
                interval = 30
        elif isinstance(self.settings, dict):
            try:
                interval = max(5, int(self.settings.get("auto_restart_interval", 30)))
            except Exception:
                interval = 30

        self._auto_restart_job = self.root.after(interval * 1000, self._perform_auto_restart)

    def _perform_auto_restart(self) -> None:
        self._auto_restart_job = None
        if not self.running or getattr(self, "_closing", False) or self.stop_requested.is_set():
            return

        # If currently in the middle of active minigame tracking, grant brief grace to let the catch finish
        controller = getattr(self, "controller", None)
        worker_state = getattr(self, "last_worker_state", None)
        is_tracking = False
        track_age = 0.0
        if controller and getattr(controller, "state", None) == "Track":
            is_tracking = True
            track_age = time.monotonic() - getattr(controller, "track_entered_time", 0.0)
        elif worker_state == "Track":
            is_tracking = True
            track_age = time.monotonic() - getattr(self, "last_worker_track_start", time.monotonic())

        if is_tracking and track_age < 18.0 and not getattr(self, "_in_restart_grace", False):
            self._in_restart_grace = True
            self._auto_restart_job = self.root.after(4000, self._perform_auto_restart)
            return

        self._in_restart_grace = False
        interval = 30
        if hasattr(self, "vars") and "auto_restart_interval" in self.vars:
            try:
                interval = int(self.vars["auto_restart_interval"].get())
            except Exception:
                interval = 30
        elif isinstance(self.settings, dict):
            try:
                interval = int(self.settings.get("auto_restart_interval", 30))
            except Exception:
                interval = 30

        self._set_status(f"🔄 รีเซ็ตหยุดและเริ่มใหม่อัตโนมัติ (รอบ {interval} วินาที)...")
        if hasattr(self, "_log_debug_event"):
            self._log_debug_event("auto_restart_cycle", {"interval": interval})

        # Stop current session cleanly
        self.stop(reason="auto_restart_cycle")

        # Allow 400ms for worker process and mouse state to settle, then restart fresh
        self._pending_restart_job = self.root.after(400, self._restart_after_cycle)

    def _restart_after_cycle(self) -> None:
        self._pending_restart_job = None
        if getattr(self, "_closing", False) or self.stop_requested.is_set():
            return
        if self.running:
            return

        # Start anew
        if getattr(self, "env_mode", "desktop") == "gamescope" and hasattr(self, "_start_gamescope"):
            self._start_gamescope()
        elif getattr(self, "env_mode", "desktop") == "dual_screen" and hasattr(self, "_start_dual_screen"):
            self._start_dual_screen()
        elif getattr(self, "target", None) and is_window_alive(self.target[0]):
            try:
                settings = self._settings()
                self.start_pending = True
                self._start_now(settings)
            except Exception:
                self.start()
        else:
            self.start()

    def _on_auto_restart_toggle(self) -> None:
        enabled = bool(self.vars["auto_restart_30s"].get())
        if hasattr(self, "auto_restart_entry"):
            try:
                self.auto_restart_entry.configure(state="normal" if enabled else "disabled")
            except Exception:
                pass
        if not enabled:
            self._cancel_auto_restart()
        elif self.running:
            self._schedule_auto_restart()
        self.settings["auto_restart_30s"] = enabled
        if "linux" in self.settings and isinstance(self.settings["linux"], dict):
            self.settings["linux"]["auto_restart_30s"] = enabled
        if "windows" in self.settings and isinstance(self.settings["windows"], dict):
            self.settings["windows"]["auto_restart_30s"] = enabled
        save_settings(self.settings)

    def _on_auto_restart_interval_change(self) -> None:
        try:
            val = int(str(self.vars["auto_restart_interval"].get()).strip())
            val = max(5, min(600, val))
        except Exception:
            val = 30
        self.vars["auto_restart_interval"].set(str(val))
        self.settings["auto_restart_interval"] = val
        if "linux" in self.settings and isinstance(self.settings["linux"], dict):
            self.settings["linux"]["auto_restart_interval"] = val
        if "windows" in self.settings and isinstance(self.settings["windows"], dict):
            self.settings["windows"]["auto_restart_interval"] = val
        save_settings(self.settings)
        if self.running and bool(self.vars["auto_restart_30s"].get()):
            self._schedule_auto_restart()

    def start(self):
        if (self.running or getattr(self, "start_pending", False)
                or getattr(self, "test_hold_pending", False)
                or getattr(self, "test_hold_active", False)):
            return

        try:
            if not self.target:
                snap = window_snapshot()
                helper_id = int(self.root.winfo_id()) if hasattr(self, "root") and self.root.winfo_exists() else None
                helper_bounds = None
                try:
                    helper_bounds = (int(self.root.winfo_rootx()), int(self.root.winfo_rooty()),
                                     int(self.root.winfo_width()), int(self.root.winfo_height()))
                except Exception:
                    pass
                same_bounds = helper_bounds and snap and snap[1] == helper_bounds
                if snap and snap[0] != helper_id and not same_bounds:
                    self.target = snap
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

    def _focus_target_window(self) -> None:
        """Focus the selected game window immediately (manual button trigger)."""
        if not self.target:
            self._set_status("ยังไม่ได้เลือกหน้าต่างเกม")
            return
        wid = self.target[0]
        if not is_window_alive(wid):
            self._set_status("หน้าต่างเกมถูกปิดแล้ว")
            return
        self._log_debug_event("manual_focus_requested", {"window_id": wid})
        if focus_window(wid):
            self._set_status("ส่งคำสั่งโฟกัสหน้าต่างเกมแล้ว")
        else:
            self._set_status("ไม่สามารถดึงโฟกัสหน้าต่างเกมได้")

    def _log_debug_event(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        """Record important transitions and diagnostic events in a fixed 20-item ring buffer."""
        if not hasattr(self, "recent_events"):
            self.recent_events = collections.deque(maxlen=20)
        entry = {
            "time": time.strftime("%H:%M:%S", time.localtime()),
            "timestamp": time.monotonic(),
            "mode": self.settings.get("fishing_mode", "rod") if hasattr(self, "settings") else "rod",
            "event": event_type,
            "state": getattr(self.controller, "state", "Unknown") if hasattr(self, "controller") else "Unknown",
        }
        if details:
            entry.update(details)
        self.recent_events.append(entry)
        if hasattr(self, "settings") and self.settings.get("debug"):
            print(f"[DEBUG_EVENT] {entry}")

    def _check_target_status(self) -> tuple[str, tuple[int, int, int, int] | None]:
        """Verify window identity, bounds, ROI containment, foreground focus, and mouse position."""
        if not self.target:
            return "no_target", None
        target_wid, target_bounds = self.target
        if not is_window_alive(target_wid):
            return "closed", None

        cur_bounds = get_window_bounds(target_wid)
        if not cur_bounds:
            return "closed", None

        # If window moved or resized, update target bounds and relative ROIs
        if cur_bounds != target_bounds:
            old_bounds = target_bounds
            self.target = (target_wid, cur_bounds)
            self._log_debug_event("window_moved", {
                "window_id": target_wid,
                "old_bounds": old_bounds,
                "new_bounds": cur_bounds,
            })
            if hasattr(self, "_relative_rois") and self._relative_rois:
                for name, rel_roi in self._relative_rois.items():
                    rel_x, rel_y, rw, rh = rel_roi
                    new_roi = [cur_bounds[0] + rel_x, cur_bounds[1] + rel_y, rw, rh]
                    if _inside_rect(new_roi, cur_bounds):
                        self.settings.setdefault("rois", {})[name] = new_roi

        # Verify bar ROI is strictly within current window bounds
        bar_roi = self.settings.get("rois", {}).get("bar")
        if bar_roi and not _inside_rect(bar_roi, cur_bounds):
            return "roi_outside", cur_bounds

        # Check desktop foreground window
        snap = window_snapshot()
        if snap is None or snap[0] != target_wid:
            return "not_foreground", cur_bounds

        # Check if cursor is inside game window bounds in Desktop mode
        try:
            pyautogui = _load_pyautogui()
            mouse_pos = tuple(pyautogui.position())
            if not _inside(mouse_pos, cur_bounds):
                safe_x = cur_bounds[0] + max(10, cur_bounds[2] // 2)
                safe_y = cur_bounds[1] + max(10, cur_bounds[3] // 2)
                old_failsafe = getattr(pyautogui, "FAILSAFE", True)
                try:
                    pyautogui.FAILSAFE = False
                    pyautogui.moveTo(safe_x, safe_y)
                finally:
                    pyautogui.FAILSAFE = old_failsafe
                mouse_pos = tuple(pyautogui.position())
                if not _inside(mouse_pos, cur_bounds):
                    return "mouse_outside", cur_bounds
        except Exception:
            pass

        return "ready", cur_bounds

    def _start_now(self, settings):
        if not getattr(self, "start_pending", False):
            return
        self.start_pending = False
        self.pending_start_id = None
        try:
            target = window_snapshot()
            if not target or target[0] != self.target[0]:
                raise RuntimeError("กรุณาโฟกัสหน้าต่างเกมเดิมที่เลือกไว้")
            settings = validate_settings(settings, target[1])
            self.target = target
            self.stop_requested.clear()
            self.settings = settings
            # Initialize relative ROIs for dynamic window movement
            wx, wy, ww, wh = target[1]
            self._relative_rois = {}
            for name, roi in self.settings.get("rois", {}).items():
                if roi and len(roi) == 4:
                    self._relative_rois[name] = (roi[0] - wx, roi[1] - wy, roi[2], roi[3])
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
            if not getattr(self, "_global_hotkey_cleanup", None):
                self.hotkey_cleanup = register_stop(self.stop_requested.set)
            self.controller = Controller(settings)
            self.controller.start(time.monotonic())
            self.capture = ScreenCapture()
            _set_fast_input_timing()
            self.running = True
            self.input_failure_streak = 0
            executor = getattr(self, "executor", None)
            if executor is not None and hasattr(executor, "emergency_release"):
                executor.emergency_release()
            self.fps_count, self.fps_started = 0, time.monotonic()
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="■  หยุด Auto (F8)", style="Danger.TButton")
            mode = settings.get("fishing_mode", "rod")
            mode_name = MODE_CONFIGS.get(mode, MODE_CONFIGS["rod"])["name"]
            self._set_status(f"กำลังทำงาน — โหมด{mode_name} — กด F8 เพื่อหยุดทันที")
            self._schedule_auto_restart()
            self.root.after(0, self._pump)
        except Exception as exc:
            self._set_mode_widgets_state("normal")
            emergency_release_mouse()
            executor = getattr(self, "executor", None)
            if executor is not None and hasattr(executor, "emergency_release"):
                executor.emergency_release()
            if self.hotkey_cleanup:
                self.hotkey_cleanup()
                self.hotkey_cleanup = None
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
            self._set_status(f"เริ่มไม่ได้: {exc}")

    def _log_action_telemetry(self, action: str, result: Any) -> None:
        """Record telemetry for an action transition without spamming idle frames."""
        target_wid = self.target[0] if self.target else None
        target_bounds = self.target[1] if self.target else None
        snap = window_snapshot()
        fg_wid = snap[0] if snap else None
        cursor_pos = None
        try:
            pyautogui = _load_pyautogui()
            cursor_pos = tuple(pyautogui.position())
        except Exception:
            pass
        ok = bool(result)
        reason = getattr(result, "reason", "ok" if ok else "unknown")
        detail = getattr(result, "detail", "")
        exc = getattr(result, "exception", None)
        self._log_debug_event("action_telemetry", {
            "controller_action": action,
            "target_hwnd": target_wid,
            "foreground_hwnd": fg_wid,
            "cursor_pos": cursor_pos,
            "target_bounds": target_bounds,
            "mouse_held": getattr(self, "mouse_held", _mouse_held),
            "ok": ok,
            "reason": reason,
            "detail": detail,
            "exception": str(exc) if exc else None,
            "winerror": getattr(result, "winerror", None),
            "desired_held": getattr(getattr(self, "controller", None), "desired_held", None),
            "actual_held": getattr(getattr(self, "executor", None), "actual_held", getattr(getattr(self, "controller", None), "actual_held", None)),
        })

    def _pump(self):
        if not self.running:
            return
        if self.stop_requested.is_set():
            self.stop("หยุดฉุกเฉินด้วย F8")
            return

        now = time.monotonic()
        capture_time = now

        target_status, current_bounds = self._check_target_status()

        if target_status == "closed":
            self._log_debug_event("window_closed", {"target": self.target})
            self.stop("หน้าต่างเกมถูกปิด")
            return

        if target_status == "not_foreground":
            observation = None
            focused = False
            # Check auto-refocus if enabled: trigger when lost > 500ms, throttled to 2s
            if self.settings.get("auto_refocus", False):
                lost_since = getattr(self.controller, "focus_lost_since", None)
                if lost_since is not None and (now - lost_since >= 0.5):
                    if now - getattr(self, "_last_auto_refocus_time", 0.0) >= 2.0:
                        self._last_auto_refocus_time = now
                        self._log_debug_event("auto_refocus_attempt", {"window_id": self.target[0]})
                        focus_window(self.target[0])
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
            bar_image = None
            try:
                bar_image = self.capture.grab(self.settings["rois"]["bar"])
            except Exception:
                bar_image = None

            if bar_image is None or bar_image.size == 0:
                observation = None
                focused = True
                self._set_status("จับภาพไม่ได้ — กำลังลองใหม่")
            else:
                bar = detect_bar(bar_image)
                bite = False
                mode = self.settings.get("fishing_mode", "rod")
                if mode != "net" and self.template is not None and self.settings["rois"].get("bite"):
                    try:
                        bite_crop = self.capture.grab(self.settings["rois"]["bite"])
                        bite = detect_bite(bite_crop, self.template, self.settings["bite_threshold"])
                    except Exception:
                        bite = False
                observation = {"bar": bar, "bite": bite}
                self.fps_count += 1
                focused = True

                # Real-time preview update (~6-7 FPS throttled)
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

        # State reconciliation if out of sync between Controller and Executor
        executor = getattr(self, "executor", None)
        actual_held = getattr(executor, "actual_held", None) if executor is not None else None
        if actual_held is None:
            actual_held = getattr(self.controller, "actual_held", False)
        else:
            self.controller.sync_held(actual_held)

        if action == "none" and self.controller.desired_held != actual_held:
            action = "hold" if self.controller.desired_held else "release"

        if executor is not None and hasattr(executor, "apply"):
            res = executor.apply(action, self.target, safe_move=True)
        else:
            res = apply_action(action, self.target, safe_move=True)

        if action in ("hold", "release"):
            self.controller.acknowledge_action(action, bool(res))
            if executor is not None and hasattr(executor, "actual_held"):
                self.controller.sync_held(executor.actual_held)

        if not res:
            self.input_failure_streak = getattr(self, "input_failure_streak", 0) + 1
            # Transient input failure (e.g. mouse moved out during frame) - release mouse safely
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

        if action not in ("none", "") or not res:
            self._log_action_telemetry(action, res)

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
                self._set_status("ตรวจไม่พบแถบสีม่วง — กำลังค้นหาใหม่")
            elif self.controller.state == "Track":
                if getattr(self.controller, "reason", "") == "tracking resumed":
                    self._set_status(f"กลับมา Track แล้ว | {fps:.1f} FPS")
                else:
                    self._set_status(f"กำลัง Track | {fps:.1f} FPS")
            else:
                self._set_status(f"{self.controller.state}: {self.controller.reason or 'tracking'} | {fps:.1f} FPS")

        if self.controller.state == "Paused":
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

        if reason == "auto_restart_cycle":
            if getattr(self, "_auto_restart_job", None) is not None:
                try:
                    self.root.after_cancel(self._auto_restart_job)
                except Exception:
                    pass
                self._auto_restart_job = None
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="🔄  กำลังรีเซ็ต...", style="Danger.TButton")
        else:
            self._cancel_auto_restart()
            self._set_mode_widgets_state("normal")
            if hasattr(self, "main_action_btn"):
                self.main_action_btn.configure(text="▶  เริ่ม Auto (F8)", style="Primary.TButton")
        controller = getattr(self, "controller", None)
        previous_reason = controller.reason if controller else ""
        if controller:
            controller.stop(reason or previous_reason or "stopped by user")
        emergency_release_mouse()
        executor = getattr(self, "executor", None)
        if executor is not None and hasattr(executor, "emergency_release"):
            executor.emergency_release()
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
        self._closing = True
        self._cancel_auto_restart()
        self.stop()
        if getattr(self, "_global_hotkey_cleanup", None):
            try:
                self._global_hotkey_cleanup()
            except Exception:
                pass
            self._global_hotkey_cleanup = None
        self.root.destroy()


def get_app_class():
    """Dynamically return the platform-specific UI class (Linux vs Windows)."""
    system = platform.system()
    if system == "Windows":
        try:
            from ui_windows import WindowsFishingApp
            return WindowsFishingApp
        except Exception:
            return BaseFishingApp
    elif system == "Linux":
        try:
            from ui_linux import LinuxFishingApp
            return LinuxFishingApp
        except Exception:
            return BaseFishingApp
    return BaseFishingApp


FishingApp = get_app_class()


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
    app_cls = get_app_class()
    app_cls(root)
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
