import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if not sys.platform.startswith("linux"):
    raise unittest.SkipTest("Gamescope launch tests require Linux")

import gamescope_manager as gm
from auto_fishing import FishingApp


class LaunchManagerTests(unittest.TestCase):
    def test_command_keeps_gamescope_gpu_auto_selected(self):
        with mock.patch.object(gm.Path, "exists", return_value=True):
            command = gm.build_gamescope_command(refresh_rate=60, fullscreen=False)
        self.assertNotIn("--prefer-vk-device", command)
        self.assertFalse(any(arg.startswith("--env=__NV_") for arg in command))
        self.assertNotIn("--env=__GLX_VENDOR_LIBRARY_NAME=nvidia", command)

    def test_launch_does_not_force_nvidia_environment(self):
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(shutil, "which", return_value="/usr/bin/nvidia-smi"), \
                mock.patch.dict(gm.os.environ, {}, clear=True), \
                mock.patch.object(gm.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(gm, "get_available_x_displays", return_value=[":0"]):
            proc, _ = gm.launch_sober_in_gamescope(timeout=0, poll_interval=0)
            child_env = popen.call_args.kwargs.get("env", gm.os.environ)
            self.assertNotIn("__NV_PRIME_RENDER_OFFLOAD", child_env)
            self.assertNotIn("__VK_LAYER_NV_optimus", child_env)
            self.assertNotIn("__GLX_VENDOR_LIBRARY_NAME", child_env)
        gm.stop_process_safely(proc)

    def test_command_aligns_nested_and_unfocused_refresh(self):
        command = gm.build_gamescope_command(refresh_rate=59, fullscreen=False)
        self.assertEqual(command[command.index("-r") + 1], "59")
        self.assertEqual(command[command.index("-o") + 1], "59")
        self.assertEqual(command[command.index("--framerate-limit") + 1], "59")
        self.assertNotIn("-f", command)

    def test_command_defaults_to_stable_60fps_and_respects_env(self):
        command = gm.build_gamescope_command(fullscreen=False)
        self.assertEqual(command[command.index("-r") + 1], "60")
        self.assertEqual(command[command.index("-o") + 1], "60")
        self.assertEqual(command[command.index("--framerate-limit") + 1], "60")

        with mock.patch.dict(gm.os.environ, {"GAMESCOPE_REFRESH": "240"}):
            command_240 = gm.build_gamescope_command(fullscreen=False)
            self.assertEqual(command_240[command_240.index("-r") + 1], "240")
            self.assertEqual(command_240[command_240.index("-o") + 1], "240")
            self.assertEqual(command_240[command_240.index("--framerate-limit") + 1], "240")

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


class ShellLauncherTests(unittest.TestCase):
    def test_launcher_keeps_gamescope_gpu_auto_selected(self):
        script = Path(__file__).resolve().parents[1] / "run_gamescope.sh"
        with tempfile.TemporaryDirectory() as tmp:
            fake_gamescope = Path(tmp) / "gamescope"
            fake_gamescope.write_text(
                '#!/bin/sh\nprintf "PRIME=%s\\n" "${__NV_PRIME_RENDER_OFFLOAD-}"\nprintf "%s\\n" "$@"\n'
            )
            fake_gamescope.chmod(0o755)
            fake_nvidia = Path(tmp) / "nvidia-smi"
            fake_nvidia.write_text("#!/bin/sh\nexit 0\n")
            fake_nvidia.chmod(0o755)
            env = os.environ.copy()
            env.update({"HOME": tmp, "PATH": f"{tmp}:/usr/bin:/bin"})
            result = subprocess.run(
                [str(script)], env=env, text=True, capture_output=True, check=True
            )
        self.assertIn("PRIME=", result.stdout)
        self.assertNotIn("PRIME=1", result.stdout)
        self.assertNotIn("--prefer-vk-device", result.stdout)
        self.assertNotIn("--env=__NV_", result.stdout)
        self.assertNotIn("--env=__GLX_VENDOR_LIBRARY_NAME=nvidia", result.stdout)


if __name__ == "__main__":
    unittest.main()
