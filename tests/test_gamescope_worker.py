"""Unit and integration tests for Gamescope display management and isolated worker."""

import os
import sys
import time
import unittest
from multiprocessing import Pipe
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
