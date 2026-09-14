"""Unit tests for periodic auto-restart watchdog feature (30s refresh)."""
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent

# Set runtime libraries if available
runtime_lib = ROOT / ".runtime" / "usr" / "lib"
if runtime_lib.is_dir():
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)
    os.environ["TK_LIBRARY"] = str(runtime_lib / "tk8.6")

import auto_fishing
from auto_fishing import DEFAULT_SETTINGS, validate_settings


class AutoRestartWatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.root = tk.Tk()
        cls.app = auto_fishing.FishingApp(cls.root)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.close()
        except Exception:
            pass

    def setUp(self):
        self.app.stop()
        self.app._cancel_auto_restart()
        self.app.vars["auto_restart_30s"].set(True)
        self.app.vars["auto_restart_interval"].set("30")

    def tearDown(self):
        self.app.stop()
        self.app._cancel_auto_restart()

    def test_settings_validation_includes_auto_restart(self):
        raw = {"rois": {"bar": [10, 10, 100, 20]}, "auto_restart_30s": False, "auto_restart_interval": 45}
        validated = validate_settings(raw, (0, 0, 800, 600))
        self.assertFalse(validated["auto_restart_30s"])
        self.assertEqual(validated["auto_restart_interval"], 45)

        # Default fallback
        raw_default = {"rois": {"bar": [10, 10, 100, 20]}}
        validated_default = validate_settings(raw_default, (0, 0, 800, 600))
        self.assertTrue(validated_default["auto_restart_30s"])
        self.assertEqual(validated_default["auto_restart_interval"], 30)

    def test_schedule_when_running_and_enabled(self):
        self.app.running = True
        self.app._schedule_auto_restart()
        self.assertIsNotNone(self.app._auto_restart_job)
        self.app.running = False
        self.app._cancel_auto_restart()

    def test_schedule_skipped_when_disabled(self):
        self.app.running = True
        self.app.vars["auto_restart_30s"].set(False)
        self.app._schedule_auto_restart()
        self.assertIsNone(self.app._auto_restart_job)
        self.app.running = False

    def test_schedule_skipped_when_not_running(self):
        self.app.running = False
        self.app.vars["auto_restart_30s"].set(True)
        self.app._schedule_auto_restart()
        self.assertIsNone(self.app._auto_restart_job)

    def test_perform_auto_restart_stops_and_queues_restart(self):
        self.app.running = True
        with patch.object(self.app, "stop") as mock_stop:
            self.app._perform_auto_restart()
            mock_stop.assert_called_once_with(reason="auto_restart_cycle")
            self.assertIsNotNone(self.app._pending_restart_job)

    def test_perform_auto_restart_grants_grace_when_mid_track(self):
        self.app.running = True
        self.app._in_restart_grace = False

        # Simulate controller in Track state
        fake_controller = MagicMock()
        fake_controller.state = "Track"
        fake_controller.track_entered_time = 1000.0
        self.app.controller = fake_controller

        with patch("time.monotonic", return_value=1005.0):  # 5s in track (< 18s)
            with patch.object(self.app, "stop") as mock_stop:
                self.app._perform_auto_restart()
                # Should not stop immediately; should grant 4s grace
                mock_stop.assert_not_called()
                self.assertTrue(self.app._in_restart_grace)
                self.assertIsNotNone(self.app._auto_restart_job)

        self.app.controller = None
        self.app.running = False

    def test_perform_auto_restart_grants_grace_gamescope_worker(self):
        self.app.running = True
        self.app._in_restart_grace = False
        self.app.controller = None
        self.app.last_worker_state = "Track"
        self.app.last_worker_track_start = 1000.0

        with patch("time.monotonic", return_value=1005.0):
            with patch.object(self.app, "stop") as mock_stop:
                self.app._perform_auto_restart()
                mock_stop.assert_not_called()
                self.assertTrue(self.app._in_restart_grace)
                self.assertIsNotNone(self.app._auto_restart_job)

        self.app.last_worker_state = None
        self.app.running = False

    def test_manual_stop_cancels_pending_restart(self):
        self.app.running = True
        self.app._pending_restart_job = 12345
        self.app._auto_restart_job = 67890

        # When stopped by user
        self.app.stop("ผู้ใช้สั่งหยุด")
        self.assertIsNone(self.app._pending_restart_job)
        self.assertIsNone(self.app._auto_restart_job)
        self.assertFalse(self.app.running)

    def test_restart_after_cycle_aborts_if_stop_requested(self):
        self.app.running = False
        self.app.stop_requested.set()
        with patch.object(self.app, "start") as mock_start:
            self.app._restart_after_cycle()
            mock_start.assert_not_called()
        self.app.stop_requested.clear()

    def test_restart_after_cycle_gamescope_calls_start_gamescope(self):
        self.app.running = False
        self.app.stop_requested.clear()
        self.app.env_mode = "gamescope"
        with patch.object(self.app, "_start_gamescope") as mock_gamescope_start:
            self.app._restart_after_cycle()
            mock_gamescope_start.assert_called_once()

    def test_restart_after_cycle_desktop_calls_start(self):
        self.app.running = False
        self.app.stop_requested.clear()
        self.app.env_mode = "desktop"
        self.app.target = None
        with patch.object(self.app, "start") as mock_start:
            self.app._restart_after_cycle()
            mock_start.assert_called_once()

    def test_toggle_checkbox_cancels_active_job(self):
        self.app.running = True
        self.app._schedule_auto_restart()
        self.assertIsNotNone(self.app._auto_restart_job)

        # Toggle OFF
        self.app.vars["auto_restart_30s"].set(False)
        self.app._on_auto_restart_toggle()
        self.assertIsNone(self.app._auto_restart_job)
        self.assertFalse(self.app.settings["auto_restart_30s"])

        # Toggle ON while running
        self.app.vars["auto_restart_30s"].set(True)
        self.app._on_auto_restart_toggle()
        self.assertIsNotNone(self.app._auto_restart_job)
        self.assertTrue(self.app.settings["auto_restart_30s"])
    def test_change_interval_updates_settings_and_reschedules(self):
        self.app.running = True
        self.app.vars["auto_restart_interval"].set("45")
        with patch.object(self.app, "_schedule_auto_restart") as mock_sched:
            self.app._on_auto_restart_interval_change()
            self.assertEqual(self.app.settings["auto_restart_interval"], 45)
            self.assertEqual(self.app.vars["auto_restart_interval"].get(), "45")
            mock_sched.assert_called_once()
        self.app.running = False

    def test_change_interval_clamps_out_of_bounds(self):
        # Under min (5)
        self.app.vars["auto_restart_interval"].set("2")
        self.app._on_auto_restart_interval_change()
        self.assertEqual(self.app.settings["auto_restart_interval"], 5)
        self.assertEqual(self.app.vars["auto_restart_interval"].get(), "5")

        # Over max (600)
        self.app.vars["auto_restart_interval"].set("999")
        self.app._on_auto_restart_interval_change()
        self.assertEqual(self.app.settings["auto_restart_interval"], 600)
        self.assertEqual(self.app.vars["auto_restart_interval"].get(), "600")

        # Non-numeric fallback
        self.app.vars["auto_restart_interval"].set("invalid")
        self.app._on_auto_restart_interval_change()
        self.assertEqual(self.app.settings["auto_restart_interval"], 30)
        self.assertEqual(self.app.vars["auto_restart_interval"].get(), "30")

    def test_restart_after_cycle_windows_dual_screen_calls_start_dual_screen(self):
        self.app.running = False
        self.app.stop_requested.clear()
        self.app.env_mode = "dual_screen"
        self.app._start_dual_screen = MagicMock()
        self.app._restart_after_cycle()
        self.app._start_dual_screen.assert_called_once()
        delattr(self.app, "_start_dual_screen")

    def test_windows_app_settings_persists_auto_restart(self):
        import ui_windows
        win_app = object.__new__(ui_windows.WindowsFishingApp)
        win_app.settings = {
            "windows": {},
            "rois": {"bar": [10, 10, 100, 20]},
            "auto_restart_30s": True,
            "auto_restart_interval": 40,
        }
        win_app.env_mode = "desktop"
        win_app.target = None
        win_app.vars = {
            "fishing_mode": MagicMock(get=lambda: "rod"),
            "cast_seconds": MagicMock(get=lambda: "2.0"),
            "margin": MagicMock(get=lambda: "3.0"),
            "lead": MagicMock(get=lambda: "0.0"),
            "threshold": MagicMock(get=lambda: "0.85"),
            "right": MagicMock(get=lambda: False),
            "ack": MagicMock(get=lambda: True),
            "debug": MagicMock(get=lambda: False),
            "auto_restart_30s": MagicMock(get=lambda: True),
            "auto_restart_interval": MagicMock(get=lambda: "40"),
        }
        with patch("ui_windows.save_settings"):
            saved = win_app._settings()
        self.assertTrue(saved["auto_restart_30s"])
        self.assertEqual(saved["auto_restart_interval"], 40)
        self.assertTrue(win_app.settings["windows"]["auto_restart_30s"])
        self.assertEqual(win_app.settings["windows"]["auto_restart_interval"], 40)


if __name__ == "__main__":
    unittest.main()
