"""Unit and integration tests for Gamescope display management and isolated worker."""

import os
import sys
import time
import unittest
from multiprocessing import Pipe
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not sys.platform.startswith("linux"):
    raise unittest.SkipTest("Gamescope worker tests require Linux")

import gamescope_manager as gm
from gamescope_worker import GamescopeWorker


class GamescopeManagerUnitTests(unittest.TestCase):
    def test_get_available_displays(self):
        displays = gm.get_available_x_displays()
        self.assertIsInstance(displays, list)
        for d in displays:
            self.assertTrue(d.startswith(":"))

    def test_nested_displays_filters_host(self):
        nested = gm.get_nested_displays(host_display=":0")
        self.assertNotIn(":0", nested)

    def test_inspect_invalid_display(self):
        info = gm.inspect_display(":99999")
        self.assertIsNone(info)


class MockTargetWindow:
    def __init__(self, width=640, height=480):
        self.width = width
        self.height = height
        self.id = 0x12345

    def get_attributes(self):
        class Attrs:
            map_state = 2  # X.IsViewable
        return Attrs()

    def get_geometry(self):
        class Geom:
            x = 0
            y = 0
            width = 640
            height = 480
        return Geom()

    def get_image(self, x, y, width, height, format, plane_mask):
        import numpy as np
        # Create a mock BGRX image with a purple bar and white pointer
        arr = np.zeros((height, width, 4), dtype=np.uint8)
        # Purple bar at x=100..200, y=50..70
        arr[50:70, 100:200] = [180, 50, 150, 255]
        # White marker at x=145..155, y=40..80
        arr[40:80, 145:155] = [255, 255, 255, 255]

        class RawImage:
            data = arr.tobytes()
        return RawImage()


class GamescopeWorkerUnitTests(unittest.TestCase):
    def setUp(self):
        self.parent_pipe, self.child_pipe = Pipe()
        self.worker = GamescopeWorker(self.child_pipe, ":1", 0x12345)
        # Inject mock window for isolated unit testing
        self.mock_win = MockTargetWindow()
        self.worker.target_win = self.mock_win
        self.worker.d = None  # mock display

    def tearDown(self):
        self.parent_pipe.close()
        self.child_pipe.close()

    def test_grab_window_rect_shape(self):
        self.worker.d = object()  # dummy truthy
        img = self.worker.grab_window_rect(10, 10, 120, 40)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (40, 120, 3))

    def test_full_window_capture(self):
        self.worker.d = object()
        full = self.worker.capture_full_window()
        self.assertIsNotNone(full)
        self.assertEqual(full.shape, (480, 640, 3))

    def test_start_and_stop_fishing(self):
        settings = {
            "rois": {"bar": [100, 50, 100, 20]},
            "cast_seconds": 0.5,
            "right_on_hold": False,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        self.worker.settings = settings
        self.worker.start_fishing()
        self.assertTrue(self.worker.running)
        self.assertIsNotNone(self.worker.controller)
        self.assertEqual(self.worker.controller.state, "Cast")

        # Check status message sent over pipe
        msg = self.parent_pipe.recv()
        self.assertEqual(msg[0], "STATUS")

        self.worker.stop_fishing("unit test stop")
        self.assertFalse(self.worker.running)
        stop_msg = self.parent_pipe.recv()
        self.assertEqual(stop_msg[0], "STOPPED")
        self.assertEqual(stop_msg[1], "unit test stop")

    def test_mouse_sync_recovers_after_capture_failure(self):
        from unittest import mock
        import numpy as np

        settings = {
            "rois": {"bar": [100, 50, 100, 20]},
            "cast_seconds": 0.1,
            "right_on_hold": True,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        self.worker.settings = settings
        self.worker.start_fishing()
        self.parent_pipe.recv()

        obs_frame = np.zeros((20, 100, 3), dtype=np.uint8)
        self.worker.grab_window_rect = mock.Mock(return_value=obs_frame)
        self.worker.d = mock.Mock()
        self.worker.controller.state = "Track"

        # Valid frame requiring hold: marker 20.0, target (40.0, 60.0), right_on_hold=True
        with mock.patch("gamescope_worker.detect_bar", return_value=("valid", 20.0, (40.0, 60.0))), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        self.assertEqual(self.worker.controller.state, "Track")
        self.assertTrue(self.worker.controller.held)
        self.assertTrue(self.worker.mouse_held)

        # 1 frame capture failure
        self.worker.grab_window_rect = mock.Mock(return_value=None)
        with mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        # Worker released mouse
        self.assertFalse(self.worker.mouse_held)
        # Controller held MUST also be synced to False
        self.assertFalse(self.worker.controller.held)

        # Valid frame returns requiring hold
        self.worker.grab_window_rect = mock.Mock(return_value=obs_frame)
        with mock.patch("gamescope_worker.detect_bar", return_value=("valid", 20.0, (40.0, 60.0))), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        # Both must be holding
        self.assertTrue(self.worker.controller.held)
        self.assertTrue(self.worker.mouse_held)

    def test_mouse_sync_does_not_hold_if_recovered_frame_requires_release(self):
        from unittest import mock
        import numpy as np

        settings = {
            "rois": {"bar": [100, 50, 100, 20]},
            "cast_seconds": 0.1,
            "right_on_hold": True,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        self.worker.settings = settings
        self.worker.start_fishing()
        self.parent_pipe.recv()

        obs_frame = np.zeros((20, 100, 3), dtype=np.uint8)
        self.worker.grab_window_rect = mock.Mock(return_value=obs_frame)
        self.worker.d = mock.Mock()
        self.worker.controller.state = "Track"

        with mock.patch("gamescope_worker.detect_bar", return_value=("valid", 20.0, (40.0, 60.0))), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        self.assertTrue(self.worker.mouse_held)

        # Capture fails
        self.worker.grab_window_rect = mock.Mock(return_value=None)
        with mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()
        self.assertFalse(self.worker.mouse_held)
        self.assertFalse(self.worker.controller.held)

        # Recovered frame requires release: marker at 80.0 > target (40.0, 60.0) with right_on_hold=True
        self.worker.grab_window_rect = mock.Mock(return_value=obs_frame)
        with mock.patch("gamescope_worker.detect_bar", return_value=("valid", 80.0, (40.0, 60.0))), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        self.assertFalse(self.worker.controller.held)
        self.assertFalse(self.worker.mouse_held)

    def test_gamescope_outage_keeps_worker_listening_and_reports_recovery(self):
        from unittest import mock

        settings = {
            "rois": {"bar": [100, 50, 100, 20]},
            "cast_seconds": 0.1,
            "right_on_hold": True,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        self.worker.settings = settings
        self.worker.start_fishing()
        self.parent_pipe.recv()
        self.worker.controller.state = "Track"
        self.worker.controller.previous_x = 20.0
        self.worker.controller.previous_time = 1.0
        self.worker.controller.velocity = 42.0
        self.worker.mouse_held = True
        self.worker.target_lost_since = time.monotonic() - 10.0

        with mock.patch.object(self.worker, "check_target_valid", return_value=False), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        self.assertTrue(self.worker.running)
        self.assertFalse(self.worker.mouse_held)
        self.assertFalse(self.worker.controller.held)
        self.assertIsNone(self.worker.controller.previous_x)
        self.assertEqual(self.worker.controller.velocity, 0.0)

        # A valid frame resumes the controller only after the outage is over.
        import numpy as np
        self.worker.d = mock.Mock()
        self.worker.capture_fail_since = None
        self.worker.grab_window_rect = mock.Mock(return_value=np.zeros((20, 100, 3), dtype=np.uint8))
        with mock.patch.object(self.worker, "check_target_valid", return_value=True), \
             mock.patch("gamescope_worker.detect_bar", return_value=("valid", 20.0, (40.0, 60.0))), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()
        self.assertTrue(self.worker.running)
        self.assertTrue(self.worker.mouse_held)
        self.assertTrue(self.worker.controller.held)
        messages = []
        while self.parent_pipe.poll():
            messages.append(self.parent_pipe.recv())
        self.assertTrue(any(message[0] == "STATUS" for message in messages))
        self.assertFalse(any(message[0] == "STOPPED" for message in messages))

        # A prolonged capture outage follows the same recoverable path.
        self.worker.target_lost_since = None
        self.worker.capture_fail_since = time.monotonic() - 10.0
        self.worker.grab_window_rect = mock.Mock(return_value=None)
        with mock.patch.object(self.worker, "check_target_valid", return_value=True), \
             mock.patch("gamescope_worker.xtest.fake_input"), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()
        self.assertTrue(self.worker.running)
        self.assertFalse(self.worker.mouse_held)
        self.assertFalse(self.worker.controller.held)

    def test_target_window_destroyed_stops_worker(self):
        from unittest import mock

        settings = {
            "rois": {"bar": [100, 50, 100, 20]},
            "cast_seconds": 0.1,
            "right_on_hold": True,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        self.worker.settings = settings
        self.worker.start_fishing()
        self.parent_pipe.recv()

        self.worker.d = mock.Mock()
        bad_window_exc = type("BadWindow", (Exception,), {"_error": 3})("BadWindow error")
        self.mock_win.get_attributes = mock.Mock(side_effect=bad_window_exc)
        with mock.patch("gamescope_worker.xtest.fake_input"):
            self.worker.tick()

        self.assertTrue(self.worker.target_window_dead)
        self.assertFalse(self.worker.running)
        stop_msg = self.parent_pipe.recv()
        self.assertEqual(stop_msg[0], "STOPPED")
        self.assertEqual(stop_msg[1], "target window closed")

    def test_transient_input_failure_does_not_terminate(self):
        from unittest import mock
        import numpy as np

        settings = {
            "rois": {"bar": [100, 50, 100, 20]},
            "cast_seconds": 0.1,
            "right_on_hold": True,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        self.worker.settings = settings
        self.worker.start_fishing()
        self.parent_pipe.recv()

        obs_frame = np.zeros((20, 100, 3), dtype=np.uint8)
        self.worker.grab_window_rect = mock.Mock(return_value=obs_frame)
        self.worker.d = mock.Mock()
        self.worker.controller.state = "Track"

        # Frame 1: apply_action returns False (transient failure)
        with mock.patch("gamescope_worker.detect_bar", return_value=("valid", 20.0, (40.0, 60.0))), \
             mock.patch.object(self.worker, "apply_action", return_value=False), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        # Worker must stay running through a transient failure
        self.assertTrue(self.worker.running)
        self.assertEqual(self.worker.input_fail_streak, 1)
        self.assertFalse(self.worker.mouse_held)
        self.assertFalse(self.worker.controller.held)

        # Frame 2: apply_action returns True (recovery)
        with mock.patch("gamescope_worker.detect_bar", return_value=("valid", 20.0, (40.0, 60.0))), \
             mock.patch.object(self.worker, "apply_action", return_value=True), \
             mock.patch("gamescope_worker.time.sleep"):
            self.worker.tick()

        self.assertTrue(self.worker.running)
        self.assertEqual(self.worker.input_fail_streak, 0)

    def test_worker_survives_extended_ui_silence_while_parent_alive(self):
        from unittest import mock

        self.worker.running = True
        self.worker.last_heartbeat = time.monotonic() - 6.0
        self.worker.check_parent_alive = mock.Mock(return_value=True)

        # Should survive 6 seconds of UI silence because timeout is now 10s and parent is alive
        now = time.monotonic()
        if not self.worker.check_parent_alive():
            self.worker.running = False
        if self.worker.running and (now - self.worker.last_heartbeat > 10.0):
            self.worker.running = False

        self.assertTrue(self.worker.running)

    def test_persistent_runtime_events_log_writes_transitions(self):
        log_file = ROOT / "runtime-events.log"
        test_marker = f"unit_test_event_{int(time.time() * 1000)}"
        self.worker._log_event(test_marker, {"detail": "verify_file_write"})

        self.assertTrue(log_file.is_file())
        content = log_file.read_text(encoding="utf-8")
        self.assertIn(test_marker, content)



class GamescopeWorkerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import subprocess
        # Start a temporary gamescope session with a simulated game window
        cls.handshake_file = ROOT / "scripts" / "gs_worker_test_state.txt"
        if cls.handshake_file.exists():
            cls.handshake_file.unlink()

        wrapper = f"""
echo "DISP=$DISPLAY" > {cls.handshake_file}
python3 -c '
import tkinter as tk
r = tk.Tk()
r.geometry("500x350+0+0")
r.title("Sober Simulation Window")
r.configure(bg="#222233")
# Draw a simulated purple bar on canvas
c = tk.Canvas(r, width=400, height=80, bg="#111122", highlightthickness=0)
c.pack(pady=40)
c.create_rectangle(120, 20, 260, 50, fill="#a855f7", outline="")
c.create_rectangle(188, 10, 194, 60, fill="#ffffff", outline="")
r.mainloop()
'
"""
        cls.gs_proc = subprocess.Popen(
            ["gamescope", "-W", "800", "-H", "600", "--", "bash", "-c", wrapper],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        cls.gs_disp = None
        for _ in range(30):
            if cls.handshake_file.exists():
                txt = cls.handshake_file.read_text().strip()
                if txt.startswith("DISP="):
                    cls.gs_disp = txt.split("=")[1].strip()
                    break
            time.sleep(0.2)

        if not cls.gs_disp:
            cls.gs_proc.terminate()
            raise RuntimeError("Gamescope failed to start for integration test")

        time.sleep(1.5)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "gs_proc") and cls.gs_proc:
            cls.gs_proc.terminate()
            try:
                cls.gs_proc.wait(timeout=2)
            except Exception:
                cls.gs_proc.kill()
        if hasattr(cls, "handshake_file") and cls.handshake_file.exists():
            cls.handshake_file.unlink()

    def test_full_worker_process_ipc_and_capture(self):
        import multiprocessing
        from gamescope_worker import worker_process_main

        # Find target window in gamescope session
        info = gm.inspect_display(self.gs_disp)
        self.assertIsNotNone(info)
        win_id = info.get("target_window_id")
        self.assertIsNotNone(win_id)

        parent_pipe, child_pipe = multiprocessing.Pipe()
        p = multiprocessing.Process(
            target=worker_process_main,
            args=(child_pipe, self.gs_disp, win_id)
        )
        p.start()

        # 1. Expect CONNECTED
        self.assertTrue(parent_pipe.poll(timeout=3.0))
        msg = parent_pipe.recv()
        self.assertEqual(msg[0], "CONNECTED")

        # 2. Test CAPTURE_FULL
        parent_pipe.send(("CAPTURE_FULL",))
        self.assertTrue(parent_pipe.poll(timeout=3.0))
        full_msg = parent_pipe.recv()
        self.assertEqual(full_msg[0], "FULL_IMAGE")
        full_img = full_msg[1]
        self.assertIsNotNone(full_img)
        self.assertGreater(full_img.shape[0], 100)
        self.assertGreater(full_img.shape[1], 100)

        # 3. Test START auto fishing
        settings = {
            "rois": {"bar": [50, 40, 300, 80]},
            "fishing_mode": "rod",
            "cast_seconds": 0.5,
            "right_on_hold": False,
            "margin": 3.0,
            "lead": 0.0,
            "bite_threshold": 0.85,
        }
        parent_pipe.send(("START", settings))

        # Expect initial STATUS
        self.assertTrue(parent_pipe.poll(timeout=3.0))
        start_status = parent_pipe.recv()
        self.assertEqual(start_status[0], "STATUS")
        self.assertEqual(start_status[1]["state"], "Cast")

        # Wait a moment for worker ticks and check for PREVIEW/STATUS messages
        previews_received = 0
        for _ in range(20):
            if parent_pipe.poll(timeout=0.3):
                packet = parent_pipe.recv()
                if packet[0] == "PREVIEW":
                    previews_received += 1
                    bar_img, bar_res = packet[1], packet[2]
                    self.assertIsNotNone(bar_img)
            time.sleep(0.05)

        self.assertGreater(previews_received, 0, "Worker should stream preview frames via IPC")

        # 4. Test STOP
        parent_pipe.send(("STOP", "test stop completed"))
        self.assertTrue(parent_pipe.poll(timeout=2.0))
        stop_msg = None
        while parent_pipe.poll():
            m = parent_pipe.recv()
            if m[0] == "STOPPED":
                stop_msg = m
                break
        self.assertIsNotNone(stop_msg)
        self.assertEqual(stop_msg[1], "test stop completed")

        # 5. Clean termination
        parent_pipe.send(("TERMINATE",))
        p.join(timeout=3.0)
        self.assertFalse(p.is_alive())


if __name__ == "__main__":
    unittest.main()
