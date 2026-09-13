"""Unit tests for Windows dual-screen mode auto fishing.

Verifies:
1. ui_windows module imports cleanly.
2. FishingController alias and Controller class compatibility.
3. Dual-screen start initializes Controller without ImportError and with correct type and state.
4. _pump_dual_screen() interacts with Controller API without real mouse input.
5. Start failure cleanly rolls back running state, UI button text, and widget states.
6. Stop cleanly releases background inputs and restores UI.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

import numpy as np

import auto_fishing
from auto_fishing import Controller, FishingController
import ui_windows


class WindowsDualScreenTests(unittest.TestCase):
    """Test Windows dual-screen controller lifecycle and error handling."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.app = ui_windows.WindowsFishingApp(self.root)
        self.app.env_mode = "dual_screen"
        self.app.vars["env_mode"].set("dual_screen")

    def tearDown(self):
        try:
            self.app.stop()
        except Exception:
            pass

    def test_ui_windows_module_loads(self):
        """Verify ui_windows imports and defines WindowsFishingApp."""
        self.assertTrue(hasattr(ui_windows, "WindowsFishingApp"))
        self.assertIs(auto_fishing.FishingController, auto_fishing.Controller)

    def test_controller_import_and_alias_consistency(self):
        """Verify Controller can be imported and initialized directly and via FishingController alias."""
        settings = {"cast_seconds": 1.5, "margin": 3.0, "lead": 0.05, "right_on_hold": False}
        ctrl1 = Controller(settings)
        ctrl2 = FishingController(settings)
        self.assertIsInstance(ctrl1, Controller)
        self.assertIsInstance(ctrl2, Controller)
        self.assertEqual(ctrl1.state, "Idle")

        # Calling start should transition to Cast
        now = time.monotonic()
        ctrl1.start(now)
        self.assertEqual(ctrl1.state, "Cast")
        self.assertEqual(ctrl1.cast_started, now)

    def test_dual_screen_start_creates_controller_without_importerror(self):
        """Verify _start_dual_screen initializes Controller without ImportError and sets state to Cast."""
        mock_user32 = mock.MagicMock()
        mock_user32.IsWindow.return_value = 1
        mock_user32.IsWindowVisible.return_value = 1
        mock_user32.IsIconic.return_value = 0

        # Setup valid ROIs and valid window
        self.app.target_hwnd = 99999
        self.app.target_client_size = (1280, 720)
        self.app.settings["windows"] = {
            "rois": {"bar": [100, 200, 300, 40], "bite": [50, 50, 60, 60]}
        }

        with mock.patch("ui_windows.get_user32", return_value=mock_user32), \
             mock.patch.object(self.app, "_check_target_valid_windows", return_value=(True, "OK")), \
             mock.patch("ui_windows.register_stop", return_value=lambda: None), \
             mock.patch("ui_windows.ScreenCapture") as mock_cap_cls:
            mock_cap = mock.MagicMock()
            mock_cap_cls.return_value = mock_cap

            self.app._start_dual_screen()

            # Controller verification
            self.assertIsNotNone(self.app.controller)
            self.assertIsInstance(self.app.controller, Controller)
            # Must be in Cast state (because start() was called), not stuck in Idle
            self.assertEqual(self.app.controller.state, "Cast")
            self.assertTrue(self.app.running)
            self.assertEqual(self.app.main_action_btn.cget("text"), "■  หยุด Auto (F8)")
            self.assertIn("กำลังทำงานในโหมดสองจอ", self.app.status.get())

    def test_dual_screen_pump_step_and_controller_api(self):
        """Verify _pump_dual_screen steps the Controller and executes actions safely."""
        self.app.target_hwnd = 88888
        self.app.target_client_size = (1280, 720)
        self.app.settings["windows"] = {
            "rois": {"bar": [100, 200, 300, 40], "bite": None}
        }
        self.app.controller = Controller(self.app._read_ui())
        self.app.controller.start(time.monotonic())
        self.app.running = True
        self.app.capture = mock.MagicMock()

        dummy_frame = np.zeros((40, 300, 3), dtype=np.uint8)
        self.app.capture.grab.return_value = dummy_frame

        with mock.patch.object(self.app, "_check_target_valid_windows", return_value=(True, "OK")), \
             mock.patch("ui_windows.detect_bar", return_value=("valid", 150.0, (140.0, 160.0))), \
             mock.patch.object(self.app, "_apply_background_action", return_value=True) as mock_action, \
             mock.patch.object(self.app.root, "after") as mock_after:

            self.app._pump_dual_screen()

            # Verify action was applied via mock (no real OS input)
            self.assertTrue(mock_action.called)
            # Verify loop scheduled next pump
            mock_after.assert_called_with(12, self.app._pump_dual_screen)

    def test_start_failure_missing_bar_roi_rolls_back_safely(self):
        """Verify missing bar ROI halts start cleanly without leaving running=True or broken button."""
        self.app.target_hwnd = 11111
        self.app.settings["windows"] = {"rois": {"bar": None, "bite": None}}
        self.app.settings["rois"] = {"bar": None, "bite": None}

        with mock.patch.object(self.app, "_check_target_valid_windows", return_value=(True, "OK")):
            self.app._start_dual_screen()

            self.assertFalse(self.app.running)
            self.assertEqual(self.app.main_action_btn.cget("text"), "▶  เริ่ม Auto สองจอ (F8)")
            self.assertIn("กรุณาเลือกพื้นที่แถบมินิเกมบนจอ 2 ก่อนเริ่ม", self.app.status.get())

    def test_start_failure_invalid_window_rolls_back_safely(self):
        """Verify invalid window check halts start cleanly and sets status."""
        self.app.target_hwnd = 22222
        with mock.patch.object(self.app, "_check_target_valid_windows", return_value=(False, "หน้าต่างถูกย่อ")):
            self.app._start_dual_screen()

            self.assertFalse(self.app.running)
            self.assertEqual(self.app.main_action_btn.cget("text"), "▶  เริ่ม Auto สองจอ (F8)")
            self.assertIn("หน้าต่างเกมไม่พร้อม: หน้าต่างถูกย่อ", self.app.status.get())

    def test_stop_releases_background_input_and_resets_ui(self):
        """Verify stop resets running, releases mouse, and sets button text."""
        self.app.target_hwnd = 33333
        self.app.running = True
        self.app.mouse_held = True
        self.app.controller = Controller(self.app._read_ui())
        self.app.controller.start(time.monotonic())

        with mock.patch("ui_windows.emergency_release_hwnd") as mock_rel:
            self.app.stop("หยุดฉุกเฉินด้วย F8")

            self.assertFalse(self.app.running)
            self.assertFalse(self.app.mouse_held)
            mock_rel.assert_called_once_with(33333)
            self.assertEqual(self.app.main_action_btn.cget("text"), "▶  เริ่ม Auto สองจอ (F8)")
            self.assertEqual(self.app.controller.state, "Paused")
            self.assertIn("หยุดฉุกเฉินด้วย F8", self.app.status.get())

    def test_failed_background_release_is_reported_and_keeps_held_state(self):
        self.app.target_hwnd = 44444
        self.app.target_client_size = (1280, 720)
        self.app.mouse_held = True

        with mock.patch("ui_windows.send_background_release", return_value=False):
            self.assertFalse(self.app._apply_background_action("release"))

        self.assertTrue(self.app.mouse_held)

    def test_dual_screen_hold_test_uses_background_path(self):
        self.app.test_hold_pending = True
        self.app.test_hold_duration = 0.5

        with mock.patch.object(self.app, "_check_target_valid_windows", return_value=(True, "OK")), \
             mock.patch.object(self.app, "_apply_background_action", return_value=True) as apply_action, \
             mock.patch.object(self.app.root, "after") as after:
            self.app._begin_test_hold()

        apply_action.assert_called_once_with("hold")
        after.assert_called_once_with(500, self.app._finish_test_hold)
        self.assertTrue(self.app.test_hold_active)

    def test_dual_screen_hold_test_releases_through_background_path(self):
        self.app.test_hold_active = True

        with mock.patch.object(self.app, "_apply_background_action", return_value=True) as apply_action:
            self.app._finish_test_hold()

        apply_action.assert_called_once_with("release")
        self.assertFalse(self.app.test_hold_active)


if __name__ == "__main__":
    unittest.main()
