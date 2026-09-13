"""Unit and integration tests for Windows Normal Mode (Single-screen shared mouse).

Verifies:
1. ActionResult data structure backwards compatibility and boolean semantics.
2. apply_action() execution paths: hold, release, click, safe repositioning,
   failsafe handling, and detailed failure reasons.
3. focus_window() foreground verification on Windows.
4. Multi-monitor and negative coordinate support for window bounds and ROI validation.
5. Mode switching state isolation (clearing target and target_hwnd).
6. 3-second countdown safety before test_hold and start.
7. Full normal mode fishing loop workflow mocking OS APIs without real mouse input.
"""

from __future__ import annotations

import ctypes
import time
import unittest
from unittest import mock
import tkinter as tk

import auto_fishing
from auto_fishing import (
    ActionResult,
    Controller,
    apply_action,
    focus_window,
    get_virtual_screen_bounds,
    release_mouse,
    validate_settings,
)
import ui_windows


class TestActionResult(unittest.TestCase):
    """Test ActionResult structure and behavior."""

    def test_action_result_success_and_failure_truthiness(self):
        ok_res = ActionResult(True, "ok", "Mouse held down")
        fail_res = ActionResult(False, "foreground_hwnd_mismatch", "Foreground window changed")

        self.assertTrue(bool(ok_res))
        self.assertFalse(bool(fail_res))

        # Dict subscripting backwards compatibility
        self.assertTrue(ok_res["ok"])
        self.assertEqual(ok_res["reason"], "ok")
        self.assertEqual(ok_res["detail"], "Mouse held down")

        self.assertFalse(fail_res["ok"])
        self.assertEqual(fail_res["reason"], "foreground_hwnd_mismatch")


class TestApplyActionWindows(unittest.TestCase):
    """Test apply_action behavior on Windows with mocked OS and PyAutoGUI."""

    def setUp(self):
        self.target_wid = 12345
        self.target_bounds = (100, 100, 800, 600)
        self.target_snapshot = (self.target_wid, self.target_bounds)

    def tearDown(self):
        auto_fishing._mouse_held = False

    def test_hold_calls_mouse_down_when_foreground_matches(self):
        mock_pag = mock.MagicMock()
        mock_pag.position.return_value = (200, 200)

        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=self.target_snapshot), \
             mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):

            res = apply_action("hold", self.target_snapshot, safe_move=True)

            self.assertTrue(res.ok)
            self.assertEqual(res.reason, "ok")
            self.assertTrue(mock_pag.mouseDown.called)
            self.assertTrue(auto_fishing._mouse_held)

    def test_release_calls_mouse_up_and_clears_held_state(self):
        auto_fishing._mouse_held = True
        mock_pag = mock.MagicMock()
        with mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):
            res = apply_action("release", self.target_snapshot)

            self.assertTrue(res.ok)
            self.assertEqual(res.reason, "ok")
            self.assertTrue(mock_pag.mouseUp.called)
            self.assertFalse(auto_fishing._mouse_held)

    def test_click_calls_pyautogui_click(self):
        mock_pag = mock.MagicMock()
        mock_pag.position.return_value = (200, 200)

        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=self.target_snapshot), \
             mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):

            res = apply_action("click", self.target_snapshot, safe_move=True)

            self.assertTrue(res.ok)
            self.assertEqual(res.reason, "ok")
            self.assertTrue(mock_pag.click.called)

    def test_cursor_outside_target_auto_repositions_to_center(self):
        mock_pag = mock.MagicMock()
        # First call is outside: (50, 50). After moveTo(500, 400), second call is (500, 400).
        mock_pag.position.side_effect = [(50, 50), (500, 400)]

        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=self.target_snapshot), \
             mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):

            res = apply_action("hold", self.target_snapshot, safe_move=True)

            self.assertTrue(res.ok)
            # Center of (100, 100, 800, 600) is (500, 400)
            mock_pag.moveTo.assert_called_with(500, 400)
            self.assertTrue(mock_pag.mouseDown.called)

    def test_cursor_outside_target_without_safe_move_fails(self):
        mock_pag = mock.MagicMock()
        mock_pag.position.return_value = (50, 50)

        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=self.target_snapshot), \
             mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):

            res = apply_action("hold", self.target_snapshot, safe_move=False)

            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "cursor_outside_target")

    def test_foreground_hwnd_mismatch_blocks_input(self):
        other_wid = 99999
        other_snapshot = (other_wid, (0, 0, 1920, 1080))
        mock_pag = mock.MagicMock()

        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=other_snapshot), \
             mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):

            res = apply_action("hold", self.target_snapshot, safe_move=True)

            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "foreground_hwnd_mismatch")
            self.assertFalse(mock_pag.mouseDown.called)

    def test_target_missing_or_closed(self):
        res_missing = apply_action("hold", None)
        self.assertFalse(res_missing.ok)
        self.assertEqual(res_missing.reason, "target_missing")

        with mock.patch("auto_fishing.is_window_alive", return_value=False):
            res_closed = apply_action("hold", self.target_snapshot)
            self.assertFalse(res_closed.ok)
            self.assertEqual(res_closed.reason, "target_closed")

    def test_pyautogui_exception_reported_and_releases_mouse(self):
        auto_fishing._mouse_held = False
        mock_pag = mock.MagicMock()
        mock_pag.position.return_value = (200, 200)
        mock_pag.mouseDown.side_effect = RuntimeError("PyAutoGUI device error")

        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=self.target_snapshot), \
             mock.patch("auto_fishing._load_pyautogui", return_value=mock_pag):

            res = apply_action("hold", self.target_snapshot, safe_move=True)

            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "mouse_down_failed")
            self.assertIn("PyAutoGUI device error", res.detail)
            # Must have called mouseUp to release held state
            self.assertTrue(mock_pag.mouseUp.called)


class TestFocusWindowAndMultiMonitor(unittest.TestCase):
    """Test focus_window and multi-monitor metrics on Windows."""

    def test_focus_window_success_verifies_foreground(self):
        mock_user32 = mock.MagicMock()
        mock_user32.IsWindow.return_value = 1
        mock_user32.IsIconic.return_value = 0
        mock_user32.SetForegroundWindow.return_value = 1
        mock_user32.GetForegroundWindow.return_value = 12345

        with mock.patch("auto_fishing.platform.system", return_value="Windows"), \
             mock.patch("ctypes.windll.user32", mock_user32), \
             mock.patch("auto_fishing.time.sleep"):

            ret = focus_window(12345)
            self.assertTrue(ret)
            mock_user32.SetForegroundWindow.assert_called_with(mock.ANY)

    def test_focus_window_failure_when_foreground_does_not_match(self):
        mock_user32 = mock.MagicMock()
        mock_user32.IsWindow.return_value = 1
        mock_user32.IsIconic.return_value = 0
        mock_user32.SetForegroundWindow.return_value = 1
        mock_user32.GetForegroundWindow.return_value = 99999  # Different window has foreground

        with mock.patch("auto_fishing.platform.system", return_value="Windows"), \
             mock.patch("ctypes.windll.user32", mock_user32), \
             mock.patch("auto_fishing.time.sleep"):

            ret = focus_window(12345)
            self.assertFalse(ret)

    def test_multi_monitor_negative_coordinates_roi_validation(self):
        # Target window on secondary monitor to the left: x = -1920, y = 0, w = 1920, h = 1080
        target_bounds = (-1920, 0, 1920, 1080)
        settings = {
            "rois": {
                "bar": [-1500, 400, 300, 40],
                "cast": None,
                "bite": None,
            },
            "cast_seconds": 1.5,
            "margin": 3.0,
            "lead": 0.05,
            "right_on_hold": False,
        }

        # Validate settings against target window bounds
        validated = validate_settings(settings, target_bounds)
        self.assertEqual(validated["rois"]["bar"], [-1500, 400, 300, 40])

    def test_virtual_screen_bounds_windows_metrics(self):
        mock_user32 = mock.MagicMock()
        # SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77, SM_CXVIRTUALSCREEN = 78, SM_CYVIRTUALSCREEN = 79
        mock_user32.GetSystemMetrics.side_effect = lambda code: {
            76: -1920,
            77: 0,
            78: 3840,
            79: 1080,
        }.get(code, 0)

        with mock.patch("auto_fishing.platform.system", return_value="Windows"), \
             mock.patch("ctypes.windll.user32", mock_user32):

            bounds = get_virtual_screen_bounds()
            self.assertEqual(bounds, (-1920, 0, 3840, 1080))


class TestWindowsFishingAppNormalMode(unittest.TestCase):
    """Test WindowsFishingApp in Normal Desktop Mode."""

    @classmethod
    def setUpClass(cls):
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
        self.app.env_mode = "desktop"
        self.app.vars["env_mode"].set("desktop")

    def tearDown(self):
        try:
            self.app.stop()
        except Exception:
            pass

    def test_mode_switch_clears_target_state(self):
        self.app.target_hwnd = 12345
        self.app.target = (12345, (100, 100, 800, 600))
        self.app.target_client_size = (800, 600)
        self.app.target_title = "Roblox"

        # Switch to dual_screen
        self.app.vars["env_mode"].set("dual_screen")
        self.app._on_env_mode_change()

        self.assertIsNone(self.app.target_hwnd)
        self.assertIsNone(self.app.target)
        self.assertIsNone(self.app.target_client_size)
        self.assertEqual(self.app.target_title, "")

    def test_auto_resolve_single_roblox_window_on_start(self):
        roblox_window = {
            "hwnd": 54321,
            "title": "Roblox",
            "class_name": "WINDOWSCLIENT",
            "client_x": 200,
            "client_y": 150,
            "width": 1024,
            "height": 768,
            "is_iconic": False,
            "process_id": 9999,
            "monitor": "DISPLAY1",
        }

        with mock.patch("ui_windows.find_roblox_windows", return_value=[roblox_window]), \
             mock.patch.object(self.app, "_settings") as mock_settings:
            mock_settings.return_value = {"rois": {"bar": [250, 200, 200, 30]}}

            self.app.start()

            # Target should be auto-set
            self.assertEqual(self.app.target_hwnd, 54321)
            self.assertEqual(self.app.target, (54321, (200, 150, 1024, 768)))
            self.assertTrue(self.app.start_pending)

    def test_test_hold_countdown_and_execution(self):
        target_wid = 77777
        self.app.target = (target_wid, (100, 100, 800, 600))

        with mock.patch("auto_fishing.register_stop", return_value=lambda: None), \
             mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.window_snapshot", return_value=(target_wid, (100, 100, 800, 600))), \
             mock.patch.object(self.app.executor, "apply", return_value=ActionResult(True, "ok", "กดค้างสำเร็จ")) as mock_apply, \
             mock.patch.object(self.app.executor, "emergency_release") as mock_release:

            self.app.test_hold()

            self.assertTrue(self.app.test_hold_pending)
            self.assertEqual(self.app.test_hold_countdown, 3)

            # Advance countdown to zero
            self.app.test_hold_countdown = 0
            self.app._update_test_hold_countdown()

            # _begin_test_hold should have been called and applied hold
            self.assertTrue(mock_apply.called)
            self.assertTrue(self.app.test_hold_active)

            # Complete hold
            self.app._finish_test_hold()
            self.assertFalse(self.app.test_hold_active)
            self.assertTrue(mock_release.called)
            self.assertIn("ทดลองกดค้างและปล่อยแล้ว", self.app.status.get())

    def test_windows_app_uses_legacy_pyautogui_backend_for_roblox_compatibility(self):
        self.assertEqual(self.app.executor.backend, "pyautogui")

    def test_desktop_pump_is_separate_from_dual_screen_pump(self):
        self.app.env_mode = "dual_screen"
        with mock.patch.object(self.app, "_pump_desktop_legacy") as desktop_pump:
            self.app._pump()
        desktop_pump.assert_not_called()

    def test_privilege_hint_warns_on_access_denied(self):
        mock_kernel32 = mock.MagicMock()
        mock_kernel32.OpenProcess.return_value = 0  # Failed
        mock_kernel32.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED

        with mock.patch("ctypes.windll.kernel32", mock_kernel32), \
             mock.patch("ui_windows.get_user32") as mock_get_user32:
            mock_user32 = mock.MagicMock()
            mock_get_user32.return_value = mock_user32
            # Simulate valid PID
            def fake_get_pid(hwnd, byref_pid):
                ctypes.cast(byref_pid, ctypes.POINTER(ctypes.c_ulong)).contents.value = 8888
                return 1
            mock_user32.GetWindowThreadProcessId.side_effect = fake_get_pid

            self.app._check_privilege_hint(12345)

            self.assertIn("Administrator", self.app.status.get())


class TestWindowsInputExecutor(unittest.TestCase):
    """Unit tests for WindowsInputExecutor SendInput and fallback behavior."""

    def setUp(self):
        self.executor = ui_windows.WindowsInputExecutor(backend="sendinput")

    def test_sendinput_success_sets_actual_held(self):
        mock_user32 = mock.MagicMock()
        mock_user32.SendInput.return_value = 1

        with mock.patch("ui_windows.get_user32", return_value=mock_user32):
            res_hold = self.executor.hold()
            self.assertTrue(res_hold.ok)
            self.assertTrue(self.executor.actual_held)
            self.assertIn("SendInput", res_hold.detail)

            res_rel = self.executor.release()
            self.assertTrue(res_rel.ok)
            self.assertFalse(self.executor.actual_held)

    def test_sendinput_error_access_denied(self):
        mock_user32 = mock.MagicMock()
        mock_user32.SendInput.return_value = 0
        mock_kernel32 = mock.MagicMock()
        mock_kernel32.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED

        with mock.patch("ui_windows.get_user32", return_value=mock_user32), \
             mock.patch("ctypes.windll.kernel32", mock_kernel32):
            res = self.executor.hold()
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "input_blocked_by_privilege_level")
            self.assertEqual(res.winerror, 5)
            self.assertFalse(self.executor.actual_held)

    def test_sendinput_generic_error(self):
        mock_user32 = mock.MagicMock()
        mock_user32.SendInput.return_value = 0
        mock_kernel32 = mock.MagicMock()
        mock_kernel32.GetLastError.return_value = 87  # ERROR_INVALID_PARAMETER

        with mock.patch("ui_windows.get_user32", return_value=mock_user32), \
             mock.patch("ctypes.windll.kernel32", mock_kernel32):
            res = self.executor.hold()
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "send_input_failed")
            self.assertEqual(res.winerror, 87)
            self.assertFalse(self.executor.actual_held)

    def test_emergency_release_clears_actual_held_without_raising(self):
        self.executor.actual_held = True
        with mock.patch("ui_windows._send_input_mouse", side_effect=RuntimeError("Simulated OS failure")), \
             mock.patch("ui_windows.emergency_release_mouse"):
            self.executor.emergency_release()
            self.assertFalse(self.executor.actual_held)

    def test_apply_routes_hold_when_foreground_matches(self):
        target = (12345, (100, 100, 800, 600))
        mock_pag = mock.MagicMock()
        mock_pag.position.return_value = (200, 200)

        with mock.patch("ui_windows.is_window_alive", return_value=True), \
             mock.patch("ui_windows.window_snapshot", return_value=target), \
             mock.patch("ui_windows._load_pyautogui", return_value=mock_pag), \
             mock.patch.object(self.executor, "hold", return_value=ActionResult(True, "ok", "hold ok")) as mock_hold:

            res = self.executor.apply("hold", target, safe_move=True)
            self.assertTrue(res.ok)
            self.assertTrue(mock_hold.called)

    def test_apply_blocks_input_on_foreground_mismatch(self):
        target = (12345, (100, 100, 800, 600))
        fg_snap = (99999, (0, 0, 1920, 1080))

        with mock.patch("ui_windows.is_window_alive", return_value=True), \
             mock.patch("ui_windows.window_snapshot", return_value=fg_snap):

            res = self.executor.apply("hold", target, safe_move=True)
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "foreground_hwnd_mismatch")


class TestControllerStateSync(unittest.TestCase):
    """Test Controller held/desired_held/actual_held separation and acknowledgment."""

    def test_sync_held(self):
        ctrl = Controller({"cast_seconds": 1.0, "lead": 0.0, "margin": 3.0})
        self.assertFalse(ctrl.actual_held)
        ctrl.sync_held(True)
        self.assertTrue(ctrl.actual_held)
        self.assertTrue(ctrl.held)
        ctrl.sync_held(False)
        self.assertFalse(ctrl.actual_held)
        self.assertFalse(ctrl.held)

    def test_acknowledge_action_hold(self):
        ctrl = Controller({"cast_seconds": 1.0, "lead": 0.0, "margin": 3.0})
        ctrl.desired_held = True

        # Successful hold
        ctrl.acknowledge_action("hold", True)
        self.assertTrue(ctrl.actual_held)
        self.assertTrue(ctrl.held)

        # Failed hold -> rolls back actual_held
        ctrl.acknowledge_action("hold", False)
        self.assertFalse(ctrl.actual_held)
        self.assertFalse(ctrl.held)
        # desired_held stays True so system knows it still wants to hold
        self.assertTrue(ctrl.desired_held)

    def test_acknowledge_action_release(self):
        ctrl = Controller({"cast_seconds": 1.0, "lead": 0.0, "margin": 3.0})
        ctrl.actual_held = True
        ctrl.held = True
        ctrl.desired_held = False

        # Successful release
        ctrl.acknowledge_action("release", True)
        self.assertFalse(ctrl.actual_held)
        self.assertFalse(ctrl.held)

        # Failed release -> actual_held retained as True
        ctrl.acknowledge_action("release", False)
        self.assertTrue(ctrl.actual_held)
        self.assertTrue(ctrl.held)


class TestIntegrationNormalModeWorkflow(unittest.TestCase):
    """End-to-end integration test of normal mode fishing pump loop."""

    @classmethod
    def setUpClass(cls):
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
        self.app.env_mode = "desktop"
        self.app.vars["env_mode"].set("desktop")

    def tearDown(self):
        try:
            self.app.stop()
        except Exception:
            pass

    def test_full_normal_mode_step_and_hold_execution(self):
        """Simulate Roblox target selected, ROI set, start initiated, and pump stepping Controller."""
        target_wid = 33333
        target_bounds = (100, 100, 800, 600)
        self.app.target = (target_wid, target_bounds)
        self.app.settings = {
            "rois": {"bar": [200, 200, 300, 40], "bite": None},
            "cast_seconds": 1.5,
            "margin": 3.0,
            "lead": 0.05,
            "right_on_hold": False,
        }

        # Initialize controller and capture mock
        self.app.controller = Controller(self.app.settings)
        self.app.controller.start(time.monotonic())
        # In Cast state, the controller's initial step should hold
        self.assertEqual(self.app.controller.state, "Cast")
        self.app.running = True
        self.app.capture = mock.MagicMock()

        import numpy as np
        fake_frame = np.zeros((40, 300, 3), dtype=np.uint8)
        self.app.capture.grab.return_value = fake_frame

        applied_actions = []

        def fake_apply(action, target):
            applied_actions.append(action)
            return ActionResult(True, "ok", f"{action}_success")

        with mock.patch("ui_windows.window_snapshot", return_value=self.app.target), \
             mock.patch("ui_windows.detect_bar", return_value=("valid", 40.0, (80.0, 120.0))), \
             mock.patch("ui_windows.apply_action", side_effect=fake_apply), \
             mock.patch.object(self.app.root, "after") as mock_after:

            # Execute one pump cycle
            self.app._pump()

            # The controller in Cast state should trigger "hold"
            self.assertIn("hold", applied_actions)
            # Scheduled next pump
            mock_after.assert_called_with(12, self.app._pump)

        # Now test stop releases mouse
        with mock.patch.object(self.app.executor, "emergency_release") as mock_release:
            self.app.stop("Test finished")
            self.assertFalse(self.app.running)
            self.assertTrue(mock_release.called)

    def test_state_reconciliation_heals_out_of_sync(self):
        """When controller wants to hold but executor is not holding, _pump actively reconciles."""
        target_wid = 33333
        target_bounds = (100, 100, 800, 600)
        self.app.target = (target_wid, target_bounds)
        self.app.settings = {
            "rois": {"bar": [200, 200, 300, 40], "bite": None},
            "cast_seconds": 1.5,
            "margin": 3.0,
            "lead": 0.05,
            "right_on_hold": False,
        }

        self.app.controller = Controller(self.app.settings)
        self.app.controller.state = "Track"
        self.app.controller.desired_held = True
        self.app.controller.held = True  # Controller thought it was held
        self.app.executor.actual_held = False  # But OS/executor wasn't holding!
        self.app.running = True
        self.app.capture = mock.MagicMock()

        import numpy as np
        fake_frame = np.zeros((40, 300, 3), dtype=np.uint8)
        self.app.capture.grab.return_value = fake_frame

        applied_actions = []

        def fake_apply(action, target, safe_move=True):
            applied_actions.append(action)
            self.app.executor.actual_held = (action == "hold")
            return ActionResult(True, "ok", f"{action}_success")

        # Controller.step will return "none" because it thinks it's already held,
        # but _pump must detect desired_held (True) != actual_held (False) and force "hold"!
        with mock.patch.object(self.app, "_check_target_status", return_value=("ready", target_bounds)), \
             mock.patch("auto_fishing.detect_bar", return_value=("valid", 100.0, (90.0, 110.0))), \
             mock.patch.object(self.app.executor, "apply", side_effect=fake_apply), \
             mock.patch.object(self.app.root, "after"):

            self.app._pump()

            self.assertIn("hold", applied_actions)
            self.assertTrue(self.app.executor.actual_held)
            self.assertTrue(self.app.controller.actual_held)

    def test_failure_streak_counter_stops_after_consecutive_errors(self):
        """Consecutive input failures increment streak and cleanly stop at 15 failures."""
        target_wid = 33333
        target_bounds = (100, 100, 800, 600)
        self.app.target = (target_wid, target_bounds)
        self.app.settings = {
            "rois": {"bar": [200, 200, 300, 40], "bite": None},
            "cast_seconds": 1.5,
            "margin": 3.0,
            "lead": 0.05,
            "right_on_hold": False,
        }

        self.app.controller = Controller(self.app.settings)
        self.app.controller.start(time.monotonic())
        self.app.running = True
        self.app.capture = mock.MagicMock()
        self.app.input_failure_streak = 14  # Next failure will make it 15

        import numpy as np
        fake_frame = np.zeros((40, 300, 3), dtype=np.uint8)
        self.app.capture.grab.return_value = fake_frame

        with mock.patch.object(self.app, "_check_target_status", return_value=("ready", target_bounds)), \
             mock.patch("auto_fishing.detect_bar", return_value=("valid", 40.0, (80.0, 120.0))), \
             mock.patch.object(self.app.executor, "apply", return_value=ActionResult(False, "send_input_failed", "fail")), \
             mock.patch.object(self.app, "stop", wraps=self.app.stop) as mock_stop:

            self.app._pump()

            self.assertTrue(mock_stop.called)
            self.assertFalse(self.app.running)
            self.assertEqual(self.app.input_failure_streak, 15)


if __name__ == "__main__":
    unittest.main()
