"""Unit tests for F8 global start/stop toggle functionality."""

import time
import unittest
from unittest import mock

import auto_fishing


class TestF8Hotkey(unittest.TestCase):
    def setUp(self):
        self.root = mock.MagicMock()
        self.root.winfo_exists.return_value = True
        self.app = auto_fishing.BaseFishingApp.__new__(auto_fishing.BaseFishingApp)
        self.app.root = self.root
        self.app.running = False
        self.app.start_pending = False
        self.app.test_hold_pending = False
        self.app.test_hold_active = False
        self.app.stop_requested = mock.MagicMock()
        self.app._closing = False
        self.app._last_f8_time = 0.0
        self.app.hotkey_cleanup = None
        self.app._global_hotkey_cleanup = None

    def test_f8_pressed_starts_when_stopped(self):
        with mock.patch.object(self.app, "start") as mock_start, \
             mock.patch.object(self.app, "stop") as mock_stop:
            self.app._on_f8_pressed()
            mock_start.assert_called_once()
            mock_stop.assert_not_called()

    def test_f8_pressed_stops_when_running(self):
        self.app.running = True
        with mock.patch.object(self.app, "start") as mock_start, \
             mock.patch.object(self.app, "stop") as mock_stop:
            self.app._on_f8_pressed()
            self.app.stop_requested.set.assert_called_once()
            mock_stop.assert_called_once_with("หยุดด้วย F8")
            mock_start.assert_not_called()

    def test_f8_pressed_stops_when_start_pending(self):
        self.app.start_pending = True
        with mock.patch.object(self.app, "start") as mock_start, \
             mock.patch.object(self.app, "stop") as mock_stop:
            self.app._on_f8_pressed()
            self.app.stop_requested.set.assert_called_once()
            mock_stop.assert_called_once_with("หยุดด้วย F8")
            mock_start.assert_not_called()

    def test_f8_pressed_stops_when_test_hold_active(self):
        self.app.test_hold_active = True
        with mock.patch.object(self.app, "start") as mock_start, \
             mock.patch.object(self.app, "stop") as mock_stop:
            self.app._on_f8_pressed()
            self.app.stop_requested.set.assert_called_once()
            mock_stop.assert_called_once_with("หยุดด้วย F8")
            mock_start.assert_not_called()

    def test_f8_debounce_prevents_duplicate_calls(self):
        with mock.patch.object(self.app, "start") as mock_start:
            self.app._on_f8_pressed()
            self.assertEqual(mock_start.call_count, 1)
            # Immediate second call within debounce window
            self.app._on_f8_pressed()
            self.assertEqual(mock_start.call_count, 1)

    def test_global_hotkey_signal_dispatches_to_root_after(self):
        self.app._on_global_hotkey_signal()
        self.root.after.assert_called_once_with(0, self.app._on_f8_pressed)

    def test_close_cleans_up_global_hotkey(self):
        mock_cleanup = mock.MagicMock()
        self.app._global_hotkey_cleanup = mock_cleanup
        with mock.patch.object(self.app, "stop"):
            self.app.close()
            mock_cleanup.assert_called_once()
            self.assertIsNone(self.app._global_hotkey_cleanup)
            self.root.destroy.assert_called_once()

    def test_start_auto_detects_target_window_if_in_game(self):
        self.app.target = None
        self.app._settings = mock.MagicMock(return_value={"rois": {"bar": [10, 10, 50, 20]}})
        self.app._set_mode_widgets_state = mock.MagicMock()
        self.app._set_status = mock.MagicMock()
        self.root.winfo_id.return_value = 99999
        self.root.winfo_rootx.return_value = 0
        self.root.winfo_rooty.return_value = 0
        self.root.winfo_width.return_value = 400
        self.root.winfo_height.return_value = 500

        game_snap = (12345, (100, 100, 800, 600))
        with mock.patch.object(auto_fishing, "window_snapshot", return_value=game_snap):
            self.app.start()
            self.assertEqual(self.app.target, game_snap)
            self.assertTrue(self.app.start_pending)


if __name__ == "__main__":
    unittest.main()
