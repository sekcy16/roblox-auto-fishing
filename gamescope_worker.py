"""Isolated worker process for automated fishing inside Gamescope.

This process connects strictly to the Gamescope nested X11 display (e.g. :1).
It captures frames directly from the client window via XGetImage, performs color
detection and state machine tracking using the core auto_fishing logic, and injects
mouse actions via XTest into the nested display only. The host desktop cursor and
focus are completely untouched.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from Xlib import X, display
from Xlib.error import XError
from Xlib.ext import xtest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from auto_fishing import Controller, detect_bar, detect_bite


class GamescopeWorker:
    """Worker that runs inside Gamescope display and communicates via IPC Pipe."""

    def __init__(self, pipe: Any, display_str: str, window_id: int):
        self.pipe = pipe
        self.display_str = display_str
        self.window_id = window_id
        self.d = None
        self.target_win = None
        self.mouse_held = False
        self.controller = None
        self.settings: dict[str, Any] = {}
        self.running = False
        self.last_heartbeat = time.monotonic()
        self.last_preview_time = 0.0
        self.fps_count = 0
        self.fps_start = time.monotonic()
        self.template = None

    def connect(self) -> bool:
        """Connect to nested X11 display and resolve target client window."""
        try:
            self.d = display.Display(self.display_str)
            self.target_win = self.d.create_resource_object("window", self.window_id)
            # Verify viewable
            attrs = self.target_win.get_attributes()
            if attrs.map_state != X.IsViewable:
                return False
            return True
        except Exception:
            return False

    def release_mouse(self) -> None:
        """Safely release the virtual mouse on the nested display."""
        if self.d:
            try:
                xtest.fake_input(self.d, X.ButtonRelease, 1)
                self.d.sync()
            except Exception:
                pass
        self.mouse_held = False

    def check_target_valid(self) -> bool:
        """Verify that the target window still exists and is viewable inside Gamescope."""
        if not self.target_win or not self.d:
            return False
        try:
            attrs = self.target_win.get_attributes()
            return attrs.map_state == X.IsViewable
        except (XError, Exception):
            return False

    def grab_window_rect(self, x: int, y: int, width: int, height: int) -> np.ndarray | None:
        """Fast XGetImage directly on the target client window."""
        if not self.target_win or not self.d:
            return None
        try:
            raw = self.target_win.get_image(x, y, width, height, X.ZPixmap, 0xFFFFFFFF)
            if not raw or not raw.data:
                return None
            arr = np.frombuffer(raw.data, dtype=np.uint8).reshape((height, width, 4))
            # BGR slice
            return arr[:, :, :3].copy()
        except (XError, Exception):
            return None

    def capture_full_window(self) -> np.ndarray | None:
        """Capture the full game window for ROI selection on desktop."""
        if not self.target_win:
            return None
        try:
            geom = self.target_win.get_geometry()
            return self.grab_window_rect(0, 0, geom.width, geom.height)
        except Exception:
            return None

    def apply_action(self, action: str) -> bool:
        """Inject virtual mouse action into nested display."""
        if action in ("none", ""):
            return True
        if action == "release":
            self.release_mouse()
            return True

        if not self.check_target_valid():
            self.release_mouse()
            return False

        try:
            if action == "hold":
                if not self.mouse_held:
                    xtest.fake_input(self.d, X.ButtonPress, 1)
                    self.d.sync()
                    self.mouse_held = True
                return True
            if action == "click":
                xtest.fake_input(self.d, X.ButtonPress, 1)
                self.d.sync()
                time.sleep(0.04)
                xtest.fake_input(self.d, X.ButtonRelease, 1)
                self.d.sync()
                self.mouse_held = False
                return True
        except Exception:
            self.release_mouse()
            return False
        return False

    def _safe_send(self, data: Any) -> bool:
        """Safely send data over the IPC pipe, suppressing broken pipe on UI exit."""
        try:
            self.pipe.send(data)
            return True
        except (BrokenPipeError, EOFError, OSError):
            self.running = False
            return False

    def run(self) -> None:
        """Main worker loop handling IPC messages and the fishing cycle."""
        if not self.connect():
            self._safe_send(("ERROR", f"Could not connect to window {hex(self.window_id)} on {self.display_str}"))
            return

        self._safe_send(("CONNECTED", {"display": self.display_str, "window_id": self.window_id}))

        try:
            while True:
                # 1. Process all pending commands from UI
                try:
                    has_data = self.pipe.poll()
                except (BrokenPipeError, EOFError, OSError):
                    self.release_mouse()
                    return

                while has_data:
                    try:
                        cmd, *args = self.pipe.recv()
                    except (BrokenPipeError, EOFError, OSError):
                        self.release_mouse()
                        return
                    self.last_heartbeat = time.monotonic()

                    if cmd == "TERMINATE":
                        self.release_mouse()
                        return

                    elif cmd == "STOP":
                        reason = args[0] if args else "stopped by user"
                        self.stop_fishing(reason)

                    elif cmd == "START":
                        self.settings = args[0] if args else {}
                        self.start_fishing()

                    elif cmd == "CAPTURE_FULL":
                        full_img = self.capture_full_window()
                        self._safe_send(("FULL_IMAGE", full_img))

                    elif cmd == "PING":
                        self._safe_send(("PONG", time.monotonic()))

                    try:
                        has_data = self.pipe.poll()
                    except (BrokenPipeError, EOFError, OSError):
                        self.release_mouse()
                        return

                # 2. Safety Timeout: if UI died or stopped communicating for >3s while running
                now = time.monotonic()
                if self.running and (now - self.last_heartbeat > 3.0):
                    self.stop_fishing("heartbeat timeout from UI")

                # 3. If running, execute one tick of the fishing loop
                if self.running:
                    self.tick()
                else:
                    time.sleep(0.05)

        except KeyboardInterrupt:
            self.release_mouse()
        finally:
            self.release_mouse()
            if getattr(self, "d", None):
                try:
                    self.d.close()
                except Exception:
                    pass

    def start_fishing(self) -> None:
        """Initialize detection templates and controller state, then begin tick loop."""
        if not self.settings or "rois" not in self.settings or not self.settings["rois"].get("bar"):
            self._safe_send(("ERROR", "ต้องกำหนดพื้นที่แถบมินิเกมก่อนเริ่ม"))
            return

        self.controller = Controller(self.settings)
        self.controller.start(time.monotonic())
        self.running = True
        self.fps_count = 0
        self.fps_start = time.monotonic()
        self.last_preview_time = 0.0

        # Load bite template if available
        template_path = self.settings.get("template_file")
        if template_path and Path(template_path).is_file():
            import cv2
            self.template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        else:
            self.template = None

        self._safe_send(("STATUS", {
            "state": self.controller.state,
            "reason": self.controller.reason,
            "fps": 0.0,
            "telemetry": {},
        }))

    def stop_fishing(self, reason: str = "stopped") -> None:
        """Stop fishing and release mouse."""
        self.running = False
        if self.controller:
            self.controller.stop(reason)
        self.release_mouse()
        self._safe_send(("STOPPED", reason))

    def tick(self) -> None:
        """Execute one frame of capture, detection, and input."""
        now = time.monotonic()
        capture_time = now

        # Focus / Target Guard inside Gamescope
        focused = self.check_target_valid()
        if not focused:
            self.stop_fishing("target window lost or unmapped inside Gamescope")
            return

        bar_roi = self.settings["rois"]["bar"]
        x, y, w, h = bar_roi
        bar_image = self.grab_window_rect(x, y, w, h)

        if bar_image is None:
            self.controller.stop("window capture failed")
            self.release_mouse()
            self.stop_fishing("window capture failed")
            return

        bar = detect_bar(bar_image)
        bite = False
        mode = self.settings.get("fishing_mode", "rod")
        bite_roi = self.settings["rois"].get("bite")
        if mode != "net" and self.template is not None and bite_roi:
            bite_img = self.grab_window_rect(bite_roi[0], bite_roi[1], bite_roi[2], bite_roi[3])
            if bite_img is not None:
                bite = detect_bite(bite_img, self.template, self.settings.get("bite_threshold", 0.85))

        observation = {"bar": bar, "bite": bite}
        self.fps_count += 1

        action = self.controller.step(now, observation, focused, capture_time=capture_time)
        if not self.apply_action(action):
            self.stop_fishing("input injection failed or window closed")
            return

        elapsed = now - self.fps_start
        fps = self.fps_count / elapsed if elapsed > 0 else 0.0

        # Throttled status and preview dispatch to UI (~8 FPS preview for smooth responsiveness)
        if now - self.last_preview_time > 0.12:
            self.last_preview_time = now
            self._safe_send(("PREVIEW", bar_image, bar))
            self._safe_send(("STATUS", {
                "state": self.controller.state,
                "reason": self.controller.reason,
                "fps": fps,
                "telemetry": getattr(self.controller, "telemetry", {}),
                "observation_bar": bar[0],
            }))

        if self.controller.state == "Paused":
            self.stop_fishing(self.controller.reason or "paused")
            return

        # Maintain ~90-120 FPS tracking loop without burning 100% single-core spin
        time.sleep(0.008)


def worker_process_main(pipe: Any, display_str: str, window_id: int) -> None:
    """Entry point for the isolated background process."""
    # Set DISPLAY strictly to the nested display in this process
    os.environ["DISPLAY"] = display_str

    # Ensure runtime lib path is loaded for this subprocess
    runtime_lib = ROOT / ".runtime" / "usr" / "lib"
    if runtime_lib.is_dir():
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)

    worker = GamescopeWorker(pipe, display_str, window_id)
    worker.run()
