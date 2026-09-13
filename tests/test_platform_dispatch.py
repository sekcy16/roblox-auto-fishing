"""Tests for operating system UI separation and platform dispatch."""

from __future__ import annotations

import json
import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

# Set runtime libraries if available
runtime_lib = ROOT / ".runtime" / "usr" / "lib"
if runtime_lib.is_dir():
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)
    os.environ["TK_LIBRARY"] = str(runtime_lib / "tk8.6")

import auto_fishing
import ui_linux
import ui_windows


class PlatformDispatchTests(unittest.TestCase):
    """Test OS-specific entry point and UI class resolution."""

    def test_linux_dispatches_to_linux_app(self):
        with mock.patch("platform.system", return_value="Linux"):
            cls = auto_fishing.get_app_class()
            self.assertEqual(cls, ui_linux.LinuxFishingApp)

    def test_windows_dispatches_to_windows_app(self):
        with mock.patch("platform.system", return_value="Windows"):
            cls = auto_fishing.get_app_class()
            self.assertEqual(cls, ui_windows.WindowsFishingApp)


class LinuxUiCapabilityTests(unittest.TestCase):
    """Verify Linux UI shows Gamescope dual-screen controls and safeguards."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = ui_linux.LinuxFishingApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.close()
        except Exception:
            pass

    def test_linux_ui_has_dual_mode_and_gamescope_controls(self):
        app = self.app
        self.assertTrue(hasattr(app, "mode_desktop_rb"), "Linux UI must include Desktop mode radio button")
        self.assertTrue(hasattr(app, "mode_gamescope_rb"), "Linux UI must include Gamescope mode radio button")
        self.assertTrue(hasattr(app, "btn_launch_gs"), "Linux UI must include launch Sober button")
        self.assertTrue(hasattr(app, "btn_refresh_gs"), "Linux UI must include Gamescope refresh button")
        self.assertTrue(hasattr(app, "gamescope_status_badge"), "Linux UI must include Gamescope status badge")
        self.assertIn("Desktop X11", app.mode_desktop_rb.cget("text"))
        self.assertIn("Gamescope", app.mode_gamescope_rb.cget("text"))

    def test_sober_precheck_blocks_duplicate_launch(self):
        app = self.app
        with mock.patch("ui_linux.gm.is_sober_running", return_value=True):
            status_msgs = []
            app._set_status = lambda msg: status_msgs.append(msg)
            app._launch_gamescope()
            self.assertTrue(any("กำลังทำงานอยู่แล้ว" in m for m in status_msgs))
            self.assertFalse(app.gamescope_launch_pending)

    def test_linux_ui_has_os_tab_switcher(self):
        app = self.app
        self.assertTrue(hasattr(app, "tab_linux_btn"), "Linux UI must have Linux tab button")
        self.assertTrue(hasattr(app, "tab_win_btn"), "Linux UI must have Windows preview tab button")
        self.assertEqual(app.active_tab, "linux")

    def test_switch_to_windows_tab_and_back(self):
        app = self.app
        # Switch to Windows preview
        app._switch_os_tab("windows")
        self.assertEqual(app.active_tab, "windows")
        self.assertIn("Windows Preview", app.root.title())
        self.assertEqual(app.windows_panel_container.winfo_manager(), "pack")
        self.assertEqual(app.linux_panel_container.winfo_manager(), "")
        self.assertEqual(app.win_exp_frame.winfo_manager(), "pack")
        self.assertEqual(app.target_summary.cget("text"), "จอปกติ (แชร์เมาส์)")

        # Verify switching tab is blocked while running
        app.running = True
        app._switch_os_tab("linux")
        self.assertEqual(app.active_tab, "windows", "Should not switch tabs while bot is running")
        app.running = False

        # Switch back to Linux
        app._switch_os_tab("linux")
        self.assertEqual(app.active_tab, "linux")
        self.assertIn("(Linux)", app.root.title())
        self.assertEqual(app.linux_panel_container.winfo_manager(), "pack")
        self.assertEqual(app.windows_panel_container.winfo_manager(), "")
        self.assertEqual(app.win_exp_frame.winfo_manager(), "")

    def test_mode_widgets_state_disables_tab_buttons(self):
        app = self.app
        app._set_mode_widgets_state("disabled")
        self.assertEqual(str(app.tab_linux_btn.cget("state")), "disabled")
        self.assertEqual(str(app.tab_win_btn.cget("state")), "disabled")
        app._set_mode_widgets_state("normal")
        self.assertEqual(str(app.tab_linux_btn.cget("state")), "normal")
        self.assertEqual(str(app.tab_win_btn.cget("state")), "normal")


class WindowsUiCapabilityTests(unittest.TestCase):
    """Verify Windows UI only shows single-monitor normal mode and no Gamescope/X11."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = ui_windows.WindowsFishingApp(cls.root)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.close()
        except Exception:
            pass

    def test_windows_ui_excludes_linux_gamescope_and_x11(self):
        app = self.app
        self.assertFalse(hasattr(app, "mode_gamescope_rb"), "Windows UI must not show Gamescope radio button")
        self.assertFalse(hasattr(app, "btn_launch_gs"), "Windows UI must not show launch Sober button")
        self.assertFalse(hasattr(app, "btn_refresh_gs"), "Windows UI must not show Gamescope refresh button")
        self.assertFalse(hasattr(app, "gamescope_status_badge"), "Windows UI must not show Gamescope status badge")

    def test_windows_ui_clearly_states_foreground_requirement(self):
        # Walk widget tree to find requirement text
        found_req = False
        found_exp = False
        def walk(widget):
            nonlocal found_req, found_exp
            try:
                text = widget.cget("text")
                if "ต้องเปิดหน้าต่าง Roblox ค้างไว้ด้านหน้า" in text:
                    found_req = True
                if "กำลังวิจัยโหมดสองจอ" in text:
                    found_exp = True
            except Exception:
                pass
            for child in widget.winfo_children():
                walk(child)
        walk(self.app.root)
        self.assertTrue(found_req, "Windows UI must explicitly explain Roblox foreground & mouse sharing requirement")
        self.assertTrue(found_exp, "Windows UI must include the research feasibility notice")


class SettingsNamespaceIsolationTests(unittest.TestCase):
    """Verify settings namespaces never contaminate or overwrite ROIs across platforms."""

    def test_isolated_namespaces_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "settings.json"

            # 1. Legacy settings (no linux/windows sub-objects)
            legacy_content = {
                "fishing_mode": "rod",
                "cast_seconds": 2.5,
                "rois": {"bar": [100, 200, 300, 50], "bite": None},
                "gamescope_rois": {"bar": [50, 60, 400, 65], "bite": None},
                "gamescope_fps": 120,
            }
            cfg_path.write_text(json.dumps(legacy_content), encoding="utf-8")

            # Load on Linux
            with mock.patch("platform.system", return_value="Linux"):
                loaded_linux = auto_fishing.load_settings(cfg_path)
                self.assertIn("linux", loaded_linux)
                self.assertEqual(loaded_linux["linux"]["desktop_rois"]["bar"], [100, 200, 300, 50])
                self.assertEqual(loaded_linux["linux"]["gamescope_rois"]["bar"], [50, 60, 400, 65])
                self.assertEqual(loaded_linux["linux"]["gamescope_fps"], 120)

            # Load on Windows
            with mock.patch("platform.system", return_value="Windows"):
                loaded_win = auto_fishing.load_settings(cfg_path)
                self.assertIn("windows", loaded_win)
                self.assertEqual(loaded_win["windows"]["rois"]["bar"], [100, 200, 300, 50])

            # 2. Verify save on Linux in Gamescope mode doesn't touch Windows or Desktop ROIs
            with mock.patch("platform.system", return_value="Linux"):
                loaded_linux["env_mode"] = "gamescope"
                loaded_linux["rois"] = {"bar": [999, 999, 500, 70], "bite": None}
                auto_fishing.save_settings(loaded_linux, cfg_path)

            raw_saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            # Linux gamescope ROI updated:
            self.assertEqual(raw_saved["linux"]["gamescope_rois"]["bar"], [999, 999, 500, 70])
            # Linux desktop ROI preserved:
            self.assertEqual(raw_saved["linux"]["desktop_rois"]["bar"], [100, 200, 300, 50])
            # Windows ROI preserved:
            self.assertEqual(raw_saved["windows"]["rois"]["bar"], [100, 200, 300, 50])


if __name__ == "__main__":
    unittest.main()
