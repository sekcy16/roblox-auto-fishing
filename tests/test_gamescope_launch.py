import unittest
from unittest import mock

import gamescope_manager as gm
from auto_fishing import FishingApp


class LaunchManagerTests(unittest.TestCase):
    def test_command_aligns_nested_and_unfocused_refresh(self):
        command = gm.build_gamescope_command(refresh_rate=59, fullscreen=False)
        self.assertEqual(command[command.index("-r") + 1], "59")
        self.assertEqual(command[command.index("-o") + 1], "59")
        self.assertNotIn("-f", command)

    def test_missing_gamescope_is_actionable(self):
        with mock.patch.object(gm.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaises(gm.GamescopeLaunchError) as caught:
                gm.launch_sober_in_gamescope()
        self.assertIn("gamescope", str(caught.exception).lower())
        self.assertIn("ติดตั้ง", str(caught.exception))

    def test_failed_process_includes_captured_output(self):
        process = mock.Mock()
        process.poll.return_value = 127
        process.stdout = None
        process.communicate.return_value = (b"gamescope: failed to start", b"")
        with mock.patch.object(gm.subprocess, "Popen", return_value=process), \
                mock.patch.object(gm, "get_available_x_displays", return_value=[]):
            with self.assertRaises(gm.GamescopeLaunchError) as caught:
                gm.launch_sober_in_gamescope()
        self.assertIn("failed to start", str(caught.exception))

    def test_output_is_redirected_away_from_pipes(self):
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(gm.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(gm, "get_available_x_displays", return_value=[":0"]):
            proc, _ = gm.launch_sober_in_gamescope(timeout=0, poll_interval=0)
        self.assertIsNot(popen.call_args.kwargs["stdout"], gm.subprocess.PIPE)
        gm.stop_process_safely(proc)


class LaunchUiTests(unittest.TestCase):
    def test_duplicate_click_while_launching_is_ignored(self):
        app = object.__new__(FishingApp)
        app.gamescope_launch_pending = True
        app.spawned_gamescope_proc = None
        app._set_status = mock.Mock()
        app._set_gamescope_launch_button = mock.Mock()
        app._launch_gamescope_worker = mock.Mock()
        app._launch_gamescope()
        app._launch_gamescope_worker.assert_not_called()
        app._set_status.assert_called_once()

    def test_duplicate_click_for_owned_process_is_ignored(self):
        app = object.__new__(FishingApp)
        app.gamescope_launch_pending = False
        app.spawned_gamescope_proc = mock.Mock()
        app.spawned_gamescope_proc.poll.return_value = None
        app._set_status = mock.Mock()
        app.gamescope_status_badge = mock.Mock()
        app._refresh_gamescope_status = mock.Mock()
        app._set_gamescope_launch_button = mock.Mock()
        app._launch_gamescope_worker = mock.Mock()
        app._launch_gamescope()
        app._launch_gamescope_worker.assert_not_called()
        app._set_status.assert_called_once()

    def test_timeout_stops_owned_process_and_keeps_error_visible(self):
        app = object.__new__(FishingApp)
        app._closing = False
        app.gamescope_launch_pending = True
        app._set_gamescope_launch_button = mock.Mock()
        app.gamescope_status_badge = mock.Mock()
        app._set_status = mock.Mock()
        app._update_readiness = mock.Mock()
        process = mock.Mock()
        with mock.patch.object(gm, "stop_process_safely") as stop:
            app._finish_gamescope_launch(process, None, "ไม่พบจอ Gamescopeภายใน timeout")
        stop.assert_called_once_with(process)
        self.assertIsNone(app.spawned_gamescope_proc)
        self.assertIn("timeout", app._set_status.call_args.args[0])

    def test_status_poll_does_not_replace_launch_error(self):
        app = object.__new__(FishingApp)
        app._closing = False
        app.gamescope_launch_error = "เปิดไม่ได้"
        app.spawned_gamescope_proc = None
        app.gamescope_status_job = None
        app._update_readiness = mock.Mock()
        app._refresh_gamescope_status = mock.Mock()
        app.root = mock.Mock()
        app.root.after.return_value = "status-id"
        app._poll_gamescope_status()
        app._refresh_gamescope_status.assert_not_called()
        app._update_readiness.assert_called_once()


if __name__ == "__main__":
    unittest.main()
