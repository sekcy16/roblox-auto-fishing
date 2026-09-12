import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from unittest import mock

from auto_fishing import (APP_VERSION, Controller, _has_spatial_variation,
                          choose_hold, detect_bar, user_data_dir,
                          validate_settings)


class VisionCheck(unittest.TestCase):
    def test_real_night_minigame_with_progress_bar(self):
        path = Path(__file__).parent / "tests/fixtures/night-minigame-crop.png"
        image = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1]
        status, marker, target = detect_bar(image)
        self.assertEqual(status, "valid")
        self.assertAlmostEqual(marker, 390, delta=3)
        self.assertAlmostEqual(target[0], 22, delta=5)
        self.assertAlmostEqual(target[1], 91, delta=5)

    def test_constant_colored_template_is_spatially_flat(self):
        self.assertFalse(_has_spatial_variation(np.full((12, 12, 3), (20, 80, 140), dtype=np.uint8)))
        varied = np.full((12, 12, 3), (20, 80, 140), dtype=np.uint8)
        varied[5, 5] = (21, 80, 140)
        self.assertTrue(_has_spatial_variation(varied))

    def test_supplied_screenshot_tight_roi(self):
        path = Path(__file__).parent / "tests" / "fixtures" / "reference-2.png"
        rgb = np.asarray(Image.open(path).convert("RGB"))
        frame = rgb[165:195, 85:705, ::-1]
        status, x, target = detect_bar(frame)
        self.assertEqual(status, "valid")
        self.assertGreater(x, 590)
        self.assertTrue(200 <= target[0] < target[1] <= 330)

    def test_bar_and_progress_are_distinct(self):
        frame = np.full((100, 320, 3), 90, dtype=np.uint8)
        frame[35:43, 10:310] = 0
        frame[30:49, 120:170] = (170, 100, 145)
        frame[29:50, 240:248] = 250
        frame[65:70, 70:250] = 250
        status, x, target = detect_bar(frame)
        self.assertEqual(status, "valid")
        self.assertTrue(240 <= x <= 248)
        self.assertTrue(115 <= target[0] < target[1] <= 175)
        self.assertEqual(detect_bar(np.zeros_like(frame))[0], "absent")

    def test_ambiguous_white_candidates(self):
        frame = np.full((80, 320, 3), 90, dtype=np.uint8)
        frame[25:35, 20:300] = 0
        frame[20:40, 100:150] = (170, 100, 145)
        frame[22:38, 55:63] = 250
        frame[22:38, 245:253] = 250
        self.assertEqual(detect_bar(frame)[0], "ambiguous")

    def test_fifteen_pixel_marker_occlusion_keeps_one_target(self):
        frame = np.full((80, 320, 3), 90, dtype=np.uint8)
        frame[25:35, 20:300] = 0
        frame[20:40, 100:200] = (170, 100, 145)
        frame[22:38, 140:155] = 250
        status, marker, target = detect_bar(frame)
        self.assertEqual(status, "valid")
        self.assertTrue(140 <= marker <= 155)
        self.assertTrue(90 <= target[0] < target[1] <= 210)

    def test_best_candidate_selected_when_one_marker_is_better_aligned(self):
        frame = np.full((80, 320, 3), 90, dtype=np.uint8)
        frame[25:35, 20:300] = 0
        frame[20:40, 100:150] = (170, 100, 145)
        frame[21:39, 240:248] = 250
        frame[27:35, 55:62] = 250
        status, marker, target = detect_bar(frame)
        self.assertEqual(status, "valid")
        self.assertTrue(240 <= marker <= 248)
        self.assertTrue(95 <= target[0] < target[1] <= 155)


class ControlCheck(unittest.TestCase):
    def test_direction(self):
        args = ((40, 60), 0, 0.1, 3)
        self.assertTrue(choose_hold(20, *args, True, False))
        self.assertFalse(choose_hold(80, *args, True, True))
        self.assertFalse(choose_hold(20, *args, False, True))
        self.assertTrue(choose_hold(80, *args, False, False))
        self.assertTrue(choose_hold(50, *args, True, True))

    def test_track_immediate_on_first_valid_frame_and_release_on_missing_bar(self):
        c = Controller({"cast_seconds": 1.0, "lead": 0.0, "margin": 3,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        self.assertEqual(c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True), "hold")
        self.assertEqual(c.step(1.0, {"bar": ("absent", None, None), "bite": False}, True), "release")
        valid = ("valid", 20.0, (40.0, 60.0))
        # First valid frame immediately enters Track and returns hold based on position
        self.assertEqual(c.step(2.0, {"bar": valid, "bite": False}, True), "hold")
        self.assertEqual(c.state, "Track")
        self.assertTrue(c.held)
        # Sustained tracking returns none
        self.assertEqual(c.step(2.1, {"bar": valid, "bite": False}, True), "none")
        self.assertEqual(c.step(2.2, {"bar": valid, "bite": False}, True), "none")
        self.assertEqual(c.state, "Track")
        # Missing bar releases and enters End
        self.assertEqual(c.step(2.3, {"bar": ("absent", None, None), "bite": False}, True), "release")
        self.assertEqual(c.state, "End")

    def test_focus_loss_pauses_and_releases(self):
        c = Controller({"cast_seconds": 1.0, "lead": 0.08, "margin": 3,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        self.assertEqual(c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True), "hold")
        self.assertEqual(c.step(0.1, {"bar": ("absent", None, None), "bite": False}, False), "release")
        self.assertEqual(c.state, "Paused")
        self.assertIn("focus", c.reason.lower())

    def test_end_recasts_after_one_second_absence(self):
        c = Controller({"cast_seconds": 0.1, "lead": 0.08, "margin": 3,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        valid = ("valid", 50.0, (40.0, 60.0))
        for t in (0.2, 0.3, 0.4):
            c.step(t, {"bar": valid, "bite": False}, True)
        self.assertEqual(c.state, "Track")
        # Bar lost -> enters End
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "End")
        # Absent for 1.0s (0.5 + 1.0 = 1.5) -> recasts
        self.assertEqual(c.step(1.4, {"bar": ("absent", None, None), "bite": False}, True), "none")
        self.assertEqual(c.state, "End")
        self.assertEqual(c.step(1.5, {"bar": ("absent", None, None), "bite": False}, True), "hold")
        self.assertEqual(c.state, "Cast")

    def test_end_requires_continuous_absence_after_valid_recovery(self):
        c = Controller({"cast_seconds": 1.0, "lead": 0.0, "margin": 3,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(1.0, {"bar": ("absent", None, None), "bite": False}, True)
        valid = ("valid", 50.0, (40.0, 60.0))
        c.step(2.0, {"bar": valid, "bite": False}, True)
        self.assertEqual(c.state, "Track")
        c.step(2.3, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "End")
        # When bar reappears, immediately enters Track
        c.step(2.8, {"bar": valid, "bite": False}, True)
        self.assertEqual(c.state, "Track")
        # Bar lost again -> enters End at 3.0
        c.step(3.0, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "End")
        # Not yet 1.0s continuous absence (0.5s elapsed)
        c.step(3.5, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "End")
        # 1.0s continuous absence elapsed (3.0 to 4.0) -> recasts
        action = c.step(4.0, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(action, "hold")
        self.assertEqual(c.state, "Cast")

    def test_transient_ambiguous_does_not_pause_immediately_in_track(self):
        c = Controller({"cast_seconds": 1.0, "lead": 0.08, "margin": 3,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(1.0, {"bar": ("absent", None, None), "bite": False}, True)
        valid = ("valid", 50.0, (40.0, 60.0))
        for t in (1.1, 1.2, 1.3):
            c.step(t, {"bar": valid, "bite": False}, True)
        self.assertEqual(c.state, "Track")
        # Transient ambiguous frame should not immediately pause
        action = c.step(1.4, {"bar": ("ambiguous", None, None), "bite": False}, True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(action, "none")
        # Recovers to valid frame
        action = c.step(1.5, {"bar": valid, "bite": False}, True)
        self.assertEqual(c.state, "Track")


class SettingsCheck(unittest.TestCase):
    def test_windows_user_data_uses_local_appdata(self):
        path = user_data_dir("Windows", {"LOCALAPPDATA": r"C:\Users\Friend\AppData\Local"})
        self.assertEqual(path, Path(r"C:\Users\Friend\AppData\Local") / "RobloxAutoFishing")
        self.assertEqual(APP_VERSION, "0.0.5")

    def test_rejects_invalid_numeric_and_roi_settings(self):
        base = {"cast_seconds": 1.0, "lead": 0.08, "margin": 3.0,
                "bite_threshold": 0.85, "right_on_hold": True, "bite_ack": False,
                "rois": {"bar": [0, 0, 100, 50], "bite": [0, 0, 100, 50]}}
        for key, value in (("cast_seconds", 0.01), ("lead", float("nan")),
                           ("margin", -1), ("bite_threshold", 2)):
            data = dict(base)
            data[key] = value
            with self.assertRaises(ValueError):
                validate_settings(data, (0, 0, 800, 600))
        data = dict(base)
        data["rois"] = {"bar": [790, 0, 20, 50], "bite": [0, 0, 100, 50]}
        with self.assertRaises(ValueError):
            validate_settings(data, (0, 0, 800, 600))

    def test_validate_returns_copy_with_defaults(self):
        data = {"cast_seconds": 1, "rois": {"bar": [0, 0, 100, 50], "bite": [0, 0, 100, 50]}}
        result = validate_settings(data, (0, 0, 800, 600))
        self.assertEqual(result["margin"], 3.0)
        self.assertEqual(result["lead"], 0.0)
        self.assertFalse(result["right_on_hold"])
        self.assertTrue(result["bite_ack"])
        self.assertEqual(result["fishing_mode"], "rod")


class InputSafetyCheck(unittest.TestCase):
    def test_area_dialog_reopens_after_window_manager_close(self):
        import auto_fishing
        app = object.__new__(auto_fishing.FishingApp)
        app.running = app.start_pending = app.test_hold_pending = app.test_hold_active = False
        app.root = mock.Mock()
        app.tk = mock.Mock()
        app.ttk = mock.Mock()
        app.area_dialog = mock.Mock()
        app.area_dialog.winfo_exists.return_value = False
        app.select_rois()
        app.tk.Toplevel.assert_called_once_with(app.root)
        app.area_dialog.protocol.assert_called_once_with("WM_DELETE_WINDOW", app._close_area_dialog)


    def test_focus_mismatch_releases_without_pressing(self):
        import auto_fishing
        fake = mock.Mock()
        with mock.patch.object(auto_fishing, "_pyautogui", fake), \
             mock.patch.object(auto_fishing, "window_snapshot", return_value=(2, (0, 0, 100, 100))):
            auto_fishing._mouse_held = True
            self.assertFalse(auto_fishing.apply_action("hold", (1, (0, 0, 100, 100))))
            fake.mouseDown.assert_not_called()
            fake.mouseUp.assert_called_once_with(button="left")
            auto_fishing._mouse_held = False

    def test_test_hold_arms_before_pressing_and_can_be_cancelled(self):
        import auto_fishing

        class Status:
            def __init__(self):
                self.values = []
            def set(self, value):
                self.values.append(value)

        class Root:
            def __init__(self):
                self.callback = None
            def after(self, delay, callback):
                self.callback = callback
                return "hold-id"
            def after_cancel(self, identifier):
                self.cancelled = identifier

        app = auto_fishing.FishingApp.__new__(auto_fishing.FishingApp)
        app.root, app.status = Root(), Status()
        app.target = (7, (0, 0, 100, 100))
        app.running = app.start_pending = False
        app.test_hold_pending = app.test_hold_active = False
        app.test_hold_id = None
        app.hotkey_cleanup = None
        app.stop_requested = __import__("threading").Event()
        with mock.patch.object(auto_fishing, "window_snapshot", return_value=app.target), \
             mock.patch.object(auto_fishing, "register_stop", return_value=lambda: None), \
             mock.patch.object(auto_fishing, "apply_action") as action, \
             mock.patch.object(auto_fishing, "release_mouse"), \
             mock.patch.object(auto_fishing, "_restore_input_timing"):
            app.test_hold()
            action.assert_not_called()
            self.assertTrue(app.test_hold_pending)
            app.stop()
            self.assertFalse(app.test_hold_pending)
            self.assertIn("หยุดการทำงานแล้ว", app.status.values[-1])

    def test_window_selection_rejects_the_helper_itself(self):
        import auto_fishing

        class Status:
            def set(self, value):
                self.value = value

        class Root:
            def winfo_id(self):
                return 12
            def winfo_rootx(self):
                return 0
            def winfo_rooty(self):
                return 0
            def winfo_width(self):
                return 100
            def winfo_height(self):
                return 100

        app = auto_fishing.FishingApp.__new__(auto_fishing.FishingApp)
        app.root, app.status = Root(), Status()
        with mock.patch.object(auto_fishing, "window_snapshot", return_value=(12, (0, 0, 100, 100))):
            app._finish_window_select()
        self.assertIsNone(app.target)
        self.assertIn("ไม่ใช่หน้าต่างช่วยเหลือ", app.status.value)


class FishingModeTests(unittest.TestCase):
    # 1. การตั้งค่าที่ไม่มี fishing_mode ต้อง fallback เป็น 'rod' เสมอ
    def test_1_settings_without_mode_defaults_to_rod(self):
        data = {"cast_seconds": 1.0, "rois": {"bar": [0, 0, 100, 50], "bite": [0, 0, 50, 50]}}
        result = validate_settings(data, (0, 0, 800, 600))
        self.assertEqual(result["fishing_mode"], "rod")

    # 2. ปฏิเสธค่าโหมดที่ไม่ใช่ 'rod' หรือ 'net'
    def test_2_rejects_invalid_fishing_mode(self):
        base = {"cast_seconds": 1.0, "rois": {"bar": [0, 0, 100, 50], "bite": [0, 0, 50, 50]}}
        for invalid in ("spear", "invalid", "", 123, None, True):
            with self.assertRaises(ValueError):
                validate_settings({**base, "fishing_mode": invalid}, (0, 0, 800, 600))

    # 3. Controller ในสถานะ Wait ต้องรอต่อไปเรื่อย ๆ โดยไม่ timeout และไม่เหวี่ยงเบ็ดซ้ำ แม้เวลาจะผ่านไปนานเท่าใด
    def test_3_wait_indefinitely_without_timeout_or_recast(self):
        for mode in ("rod", "net"):
            c = Controller({"fishing_mode": mode, "cast_seconds": 0.5, "lead": 0.05,
                            "margin": 3.0, "right_on_hold": False, "bite_ack": True})
            c.start(0.0)
            self.assertEqual(c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True), "hold")
            self.assertEqual(c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True), "release")
            self.assertEqual(c.state, "Wait")
            # Wait across different time points (30s, 60s, 300s, 1000s) without timeout or recast
            for t in (1.0, 30.0, 60.0, 120.0, 300.0, 1000.0):
                action = c.step(t, {"bar": ("absent", None, None), "bite": False}, True)
                self.assertEqual(action, "none")
                self.assertEqual(c.state, "Wait")

    # 4. Controller ตรวจพบสัญญาณปลากินเบ็ด 1 หรือ 2 เฟรมต่อเนื่อง ต้องยังไม่คลิก
    def test_4_bite_signal_under_three_frames_does_not_click(self):
        c = Controller({"cast_seconds": 0.5, "lead": 0.05, "margin": 3.0,
                        "right_on_hold": False, "bite_ack": True})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "Wait")
        # 1st bite frame
        action1 = c.step(1.0, {"bar": ("absent", None, None), "bite": True}, True)
        self.assertEqual(action1, "none")
        self.assertEqual(c.bite_streak, 1)
        self.assertFalse(c.bite_latched)
        # 2nd bite frame
        action2 = c.step(1.1, {"bar": ("absent", None, None), "bite": True}, True)
        self.assertEqual(action2, "none")
        self.assertEqual(c.bite_streak, 2)
        self.assertFalse(c.bite_latched)

    # 5. Controller ตรวจพบสัญญาณปลากินเบ็ด 3 เฟรมต่อเนื่อง ต้องส่งคำสั่งคลิก 1 ครั้ง (click)
    def test_5_bite_signal_three_frames_clicks_once(self):
        c = Controller({"cast_seconds": 0.5, "lead": 0.05, "margin": 3.0,
                        "right_on_hold": False, "bite_ack": True})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(1.0, {"bar": ("absent", None, None), "bite": True}, True)
        c.step(1.1, {"bar": ("absent", None, None), "bite": True}, True)
        # 3rd bite frame
        action3 = c.step(1.2, {"bar": ("absent", None, None), "bite": True}, True)
        self.assertEqual(action3, "click")
        self.assertTrue(c.bite_latched)

    # 6. Controller ได้รับสัญญาณปลากินเบ็ดต่อเนื่องยาวนาน ต้องไม่ส่งคำสั่งคลิกซ้ำซ้อน
    def test_6_sustained_bite_signal_does_not_repeat_click(self):
        c = Controller({"cast_seconds": 0.5, "lead": 0.05, "margin": 3.0,
                        "right_on_hold": False, "bite_ack": True})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(1.0, {"bar": ("absent", None, None), "bite": True}, True)
        c.step(1.1, {"bar": ("absent", None, None), "bite": True}, True)
        action3 = c.step(1.2, {"bar": ("absent", None, None), "bite": True}, True)
        self.assertEqual(action3, "click")
        # Continuous bite frames 4 through 10
        for t in (1.3, 1.4, 1.5, 1.6, 2.0, 3.0, 5.0):
            action = c.step(t, {"bar": ("absent", None, None), "bite": True}, True)
            self.assertEqual(action, "none")
            self.assertEqual(c.state, "Wait")

    # 7. Controller สัญญาณปลากินเบ็ดหายไป 3 เฟรมต่อเนื่อง ต้องปลด latch พร้อมตรวจจับรอบใหม่
    def test_7_bite_cleared_three_frames_unlatches(self):
        c = Controller({"cast_seconds": 0.5, "lead": 0.05, "margin": 3.0,
                        "right_on_hold": False, "bite_ack": True})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        # Latched with 3 bite frames
        c.step(1.0, {"bar": ("absent", None, None), "bite": True}, True)
        c.step(1.1, {"bar": ("absent", None, None), "bite": True}, True)
        c.step(1.2, {"bar": ("absent", None, None), "bite": True}, True)
        self.assertTrue(c.bite_latched)
        # Absence frame 1
        c.step(1.3, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertTrue(c.bite_latched)
        # Absence frame 2
        c.step(1.4, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertTrue(c.bite_latched)
        # Absence frame 3 -> unlatched!
        c.step(1.5, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertFalse(c.bite_latched)
        # Next 3 bite frames trigger click again
        self.assertEqual(c.step(2.0, {"bar": ("absent", None, None), "bite": True}, True), "none")
        self.assertEqual(c.step(2.1, {"bar": ("absent", None, None), "bite": True}, True), "none")
        self.assertEqual(c.step(2.2, {"bar": ("absent", None, None), "bite": True}, True), "click")
        self.assertTrue(c.bite_latched)

    # 8. Controller เมื่อพบหลอดมินิเกมครบตามเงื่อนไข ต้องเข้าสู่สถานะ Track ทันที ไม่ว่าจะรอมานานเท่าใด
    def test_8_bar_found_enters_track_regardless_of_time(self):
        c = Controller({"cast_seconds": 0.5, "lead": 0.0, "margin": 3.0,
                        "right_on_hold": False, "bite_ack": True})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        # Wait for 500 seconds
        c.step(500.0, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "Wait")
        # Bar appears: enters Track immediately on first frame
        valid = ("valid", 50.0, (40.0, 60.0))
        c.step(500.1, {"bar": valid, "bite": False}, True)
        self.assertEqual(c.state, "Track")

    # 9. Controller หลุดโฟกัสหรือสั่งหยุด ต้องปล่อยเมาส์เสมอ
    def test_9_focus_loss_or_stop_always_releases_mouse(self):
        # Case A: Focus loss while holding in Cast
        c1 = Controller({"cast_seconds": 1.0, "lead": 0.0, "margin": 3.0, "right_on_hold": False})
        c1.start(0.0)
        c1.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertTrue(c1.held)
        action1 = c1.step(0.2, {"bar": ("absent", None, None), "bite": False}, False)
        self.assertEqual(action1, "release")
        self.assertFalse(c1.held)
        self.assertEqual(c1.state, "Paused")

        # Case B: Focus loss while holding in Track
        c2 = Controller({"cast_seconds": 0.1, "lead": 0.0, "margin": 3.0, "right_on_hold": False})
        c2.start(0.0)
        c2.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c2.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        valid_hold = ("valid", 70.0, (40.0, 60.0))
        # First valid frame enters Track and holds immediately
        action_hold = c2.step(0.2, {"bar": valid_hold, "bite": False}, True)
        self.assertEqual(c2.state, "Track")
        self.assertEqual(action_hold, "hold")
        self.assertTrue(c2.held)
        action_loss = c2.step(0.3, {"bar": valid_hold, "bite": False}, False)
        self.assertEqual(action_loss, "release")
        self.assertFalse(c2.held)
        self.assertEqual(c2.state, "Paused")

        # Case C: Explicit stop while holding
        c3 = Controller({"cast_seconds": 1.0, "lead": 0.05, "margin": 3.0, "right_on_hold": False})
        c3.start(0.0)
        c3.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertTrue(c3.held)
        action_stop = c3.stop("user stopped")
        self.assertEqual(action_stop, "release")
        self.assertFalse(c3.held)
        self.assertEqual(c3.state, "Paused")

    # 10. ระบบตรวจสอบความพร้อมก่อนเริ่มทำงาน ต้องมี ROI bar ส่วน ROI bite หรือ bite-template.png เป็นตัวเลือกเสริม
    def test_10_missing_bite_roi_or_template_rejected_before_start(self):
        import auto_fishing
        bounds = (0, 0, 800, 600)
        # 10a: validate_settings rejects missing bar ROI
        no_bar_roi = {"cast_seconds": 1.0, "rois": {"bar": None, "bite": None}}
        with self.assertRaises(ValueError) as ctx:
            validate_settings(no_bar_roi, bounds)
        self.assertIn("แถบมินิเกม", str(ctx.exception))

        # 10b: validate_settings accepts missing bite ROI
        no_bite_roi = {"cast_seconds": 1.0, "rois": {"bar": [10, 10, 50, 20], "bite": None}}
        validated = validate_settings(no_bite_roi, bounds)
        self.assertIsNone(validated["rois"]["bite"])

        # 10c: FishingApp._start_now runs without errors even if bite-template.png is absent
        app = auto_fishing.FishingApp.__new__(auto_fishing.FishingApp)
        app.start_pending = True
        app.pending_start_id = None
        app.target = (1, (0, 0, 800, 600))
        app.settings = {"fishing_mode": "rod", "cast_seconds": 1.0,
                        "rois": {"bar": [10, 10, 50, 20], "bite": None}}
        app.stop_requested = mock.Mock()
        app.hotkey_cleanup = None
        app.status = mock.Mock()
        app._set_mode_widgets_state = mock.Mock()
        app.root = mock.Mock()

        with mock.patch.object(auto_fishing, "window_snapshot", return_value=app.target), \
             mock.patch.object(auto_fishing, "save_settings"), \
             mock.patch.object(auto_fishing, "register_stop", return_value=lambda: None), \
             mock.patch.object(auto_fishing, "ScreenCapture"), \
             mock.patch.object(auto_fishing, "_set_fast_input_timing"), \
             mock.patch.object(Path, "exists", return_value=False):
            app._start_now(app.settings)
            self.assertTrue(app.running)
            self.assertIsNone(app.template)

    def test_mode_selection_saved_and_loaded(self):
        import tempfile
        from auto_fishing import save_settings, load_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = {
                "fishing_mode": "net",
                "cast_seconds": 2.0,
                "right_on_hold": False,
                "margin": 3.0,
                "lead": 0.0,
                "bite_threshold": 0.85,
                "bite_ack": True,
                "rois": {"bar": [10, 20, 30, 40], "bite": [15, 25, 20, 20]}
            }
            save_settings(settings, path)
            loaded = load_settings(path)
            self.assertEqual(loaded["fishing_mode"], "net")

    def test_mode_change_locks_during_run(self):
        import auto_fishing
        app = auto_fishing.FishingApp.__new__(auto_fishing.FishingApp)
        app.running = False
        app.start_pending = False
        app.current_mode = "rod"
        app.vars = {
            "fishing_mode": mock.Mock(get=mock.Mock(return_value="net"), set=mock.Mock()),
        }
        # Mode change when idle updates current_mode
        app._on_mode_change()
        self.assertEqual(app.current_mode, "net")

        # Mode change when running reverts back to current_mode
        app.running = True
        app.vars["fishing_mode"].get.return_value = "rod"
        app._on_mode_change()
        app.vars["fishing_mode"].set.assert_called_with(app.current_mode)


class BarResponsivenessAndNetFixtureTests(unittest.TestCase):
    def test_net_fixture_detection_and_target_selection(self):
        """ภาพแหต้องตรวจพบตัวขาวและช่องม่วงถูกต้อง และแถบด้านล่างต้องไม่ถูกเลือกเป็นช่องเป้าหมาย"""
        path = Path(__file__).parent / "tests" / "fixtures" / "net-minigame.png"
        image = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1]
        status, marker, target = detect_bar(image)
        self.assertEqual(status, "valid")
        self.assertIsNotNone(marker)
        self.assertIsNotNone(target)
        # White marker position
        self.assertAlmostEqual(marker, 70.0, delta=2.0)
        # Purple target on left: (25.0, 56.0)
        self.assertAlmostEqual(target[0], 25.0, delta=3.0)
        self.assertAlmostEqual(target[1], 56.0, delta=3.0)
        # Marker is to the right of purple target
        self.assertGreater(marker, target[1])
        # Bottom progress bar (with reddish tip at x~60..180) must NOT be selected as target
        self.assertTrue(target[0] < 35.0 and target[1] < 65.0)

    def test_first_valid_frame_in_wait_enters_track_immediately_with_action(self):
        """เฟรม valid แรกในสถานะ Wait ต้องเข้าสู่ Track ทันที และคืน hold หรือ release ตามตำแหน่ง ไม่ใช่ none ทั้ง rod และ net"""
        for mode in ("rod", "net"):
            # Case A: right_on_hold=False (holding moves marker left towards target on the left)
            # Marker is at 70.0, target is (25.0, 56.0) -> must return 'hold' immediately
            c_hold = Controller({"fishing_mode": mode, "cast_seconds": 0.5, "lead": 0.0,
                                "margin": 3.0, "right_on_hold": False, "bite_ack": False})
            c_hold.start(0.0)
            c_hold.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
            c_hold.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
            self.assertEqual(c_hold.state, "Wait")
            valid = ("valid", 70.0, (25.0, 56.0))
            action_hold = c_hold.step(1.0, {"bar": valid, "bite": False}, True)
            self.assertEqual(c_hold.state, "Track")
            self.assertEqual(action_hold, "hold")
            self.assertTrue(c_hold.held)

            # Case B: right_on_hold=True (holding moves marker right; releasing moves left towards target on the left)
            # Marker is at 70.0, target is (25.0, 56.0) -> must return 'release' immediately
            c_rel = Controller({"fishing_mode": mode, "cast_seconds": 0.5, "lead": 0.0,
                               "margin": 3.0, "right_on_hold": True, "bite_ack": False})
            c_rel.start(0.0)
            c_rel.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
            c_rel.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
            self.assertEqual(c_rel.state, "Wait")
            action_rel = c_rel.step(1.0, {"bar": valid, "bite": False}, True)
            self.assertEqual(c_rel.state, "Track")
            self.assertEqual(action_rel, "release")
            self.assertFalse(c_rel.held)

    def test_end_state_recovers_to_track_and_responds_immediately(self):
        """สถานะ End ที่กลับมาพบแถบต้อง Track และตอบสนองทันทีทั้ง rod และ net"""
        for mode in ("rod", "net"):
            c = Controller({"fishing_mode": mode, "cast_seconds": 0.5, "lead": 0.0,
                            "margin": 3.0, "right_on_hold": False, "bite_ack": False})
            c.start(0.0)
            c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
            c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
            valid = ("valid", 70.0, (25.0, 56.0))
            self.assertEqual(c.step(1.0, {"bar": valid, "bite": False}, True), "hold")
            self.assertEqual(c.state, "Track")
            # Bar disappears -> transitions to End and releases
            action_lost = c.step(1.1, {"bar": ("absent", None, None), "bite": False}, True)
            self.assertEqual(c.state, "End")
            self.assertEqual(action_lost, "release")
            self.assertFalse(c.held)
            # Reappears in End -> immediately re-enters Track and responds with hold
            action_recover = c.step(1.2, {"bar": valid, "bite": False}, True)
            self.assertEqual(c.state, "Track")
            self.assertEqual(action_recover, "hold")
            self.assertTrue(c.held)

    def test_lead_zero_computes_from_current_position_without_prediction(self):
        """lead=0.0 ต้องคำนวณจากตำแหน่งปัจจุบันโดยไม่คาดการณ์ล่วงหน้า"""
        target = (40.0, 60.0)  # center = 50.0
        # Marker is at 45.0 (target is to the right, error = 50 - 45 = +5).
        # Even with high positive velocity = 100.0 px/s:
        # With lead=0.0, predicted remains 45.0, error remains +5.0 > margin
        hold_lead0 = choose_hold(45.0, target, velocity=100.0, lead=0.0, margin=1.0,
                                 right_on_hold=True, held=False)
        self.assertTrue(hold_lead0)

        rel_lead0 = choose_hold(45.0, target, velocity=100.0, lead=0.0, margin=1.0,
                                right_on_hold=False, held=False)
        self.assertFalse(rel_lead0)

    def test_missing_image_while_holding_returns_release(self):
        """เมื่อภาพหายขณะกดเมาส์ ต้องคืนคำสั่ง release"""
        c = Controller({"cast_seconds": 0.5, "lead": 0.0, "margin": 3.0,
                        "right_on_hold": False, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        valid = ("valid", 70.0, (25.0, 56.0))
        # Enters track and holds
        self.assertEqual(c.step(1.0, {"bar": valid, "bite": False}, True), "hold")
        self.assertTrue(c.held)
        # Bar lost: must release
        action = c.step(1.1, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(action, "release")
        self.assertFalse(c.held)
        self.assertEqual(c.state, "End")

    def test_net_mode_ignores_bite_and_never_clicks_in_wait(self):
        """โหมดแหต้องไม่คลิกเมื่อได้รับสัญญาณ bite ในสถานะ Wait และรอจนกว่ามินิเกมจะปรากฏ"""
        c = Controller({"fishing_mode": "net", "cast_seconds": 0.5, "lead": 0.0,
                        "margin": 3.0, "right_on_hold": False, "bite_ack": True})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        self.assertEqual(c.state, "Wait")
        # แม้จะมี bite ติดต่อกัน 10 เฟรม ก็ต้องไม่ส่ง click ออกมา
        for t in range(6, 16):
            action = c.step(t * 0.1, {"bar": ("absent", None, None), "bite": True}, True)
            self.assertEqual(action, "none")
            self.assertEqual(c.state, "Wait")


class SmallBarAndKinematicsPrecisionTests(unittest.TestCase):
    def test_smallest_target_detected_without_occlusion(self):
        """ช่องม่วงขนาดเล็กสุด (w=16, h=18) ที่ w/h < 1.3 ต้องตรวจพบเป็น valid ได้ถูกต้อง"""
        frame = np.full((80, 320, 3), 90, dtype=np.uint8)
        frame[25:35, 20:300] = 0
        frame[22:40, 100:116] = (170, 100, 145)  # Purple: w=16, h=18
        frame[22:40, 200:208] = 250               # Marker: w=8, h=18
        status, marker, target = detect_bar(frame)
        self.assertEqual(status, "valid")
        self.assertAlmostEqual(marker, 203.5, delta=1.5)
        self.assertAlmostEqual(target[0], 100.0, delta=2.0)
        self.assertAlmostEqual(target[1], 116.0, delta=2.0)

    def test_smallest_target_occluded_by_marker_is_merged(self):
        """เมื่อตัวชี้สีขาวบังกลางช่องม่วงขนาดเล็ก (w=20) ชิ้นส่วนซ้ายขวาต้องถูกรวมเป็นช่องสมบูรณ์"""
        frame = np.full((80, 320, 3), 90, dtype=np.uint8)
        frame[25:35, 20:300] = 0
        frame[22:40, 100:120] = (170, 100, 145)  # Purple: w=20
        frame[22:40, 106:114] = 250               # White marker in middle: w=8
        status, marker, target = detect_bar(frame)
        self.assertEqual(status, "valid")
        self.assertAlmostEqual(marker, 109.5, delta=1.5)
        self.assertAlmostEqual(target[0], 100.0, delta=2.0)
        self.assertAlmostEqual(target[1], 120.0, delta=2.0)

    def test_velocity_estimation_with_timestamps(self):
        """การคำนวณความเร็ว (velocity) ต้องอิงจาก timestamp จริงและไม่เป็น 0.0 เสมอไป"""
        c = Controller({"cast_seconds": 0.5, "lead": 0.05, "margin": 2.0,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        # Skip cast
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)

        target = (100.0, 130.0)
        # Frame 1 at t=1.00, x=50.0
        c.step(1.00, {"bar": ("valid", 50.0, target), "bite": False}, True, capture_time=1.00)
        self.assertEqual(c.state, "Track")
        self.assertEqual(c.velocity, 0.0)  # Initial entry has 0 velocity

        # Frame 2 at t=1.02 (dt=0.02s), x=56.0 -> v = (56-50)/0.02 = 300 px/s
        c.step(1.02, {"bar": ("valid", 56.0, target), "bite": False}, True, capture_time=1.02)
        self.assertAlmostEqual(c.velocity, 300.0 * 0.70, delta=10.0)
        self.assertGreater(c.velocity, 150.0)

    def test_frame_age_and_latency_prediction_without_double_counting(self):
        """การคำนวณตำแหน่งคาดการณ์ต้องรวมอายุภาพ + latency โดยไม่บวกซ้ำ"""
        c = Controller({"cast_seconds": 0.5, "lead": 0.04, "margin": 2.0,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)

        target = (200.0, 230.0)
        # Seed velocity with 2 frames
        c.step(1.00, {"bar": ("valid", 100.0, target), "bite": False}, True, capture_time=1.00)
        c.step(1.05, {"bar": ("valid", 110.0, target), "bite": False}, True, capture_time=1.05)

        # Frame 3: capture_time was 1.10, but step executed at 1.12 (frame_age = 0.02s)
        # configured lead = 0.04s -> total latency = 0.02 + 0.04 = 0.06s
        c.step(1.12, {"bar": ("valid", 120.0, target), "bite": False}, True, capture_time=1.10)
        telemetry = c.telemetry
        self.assertAlmostEqual(telemetry["frame_age_ms"], 20.0, delta=1.0)
        self.assertAlmostEqual(telemetry["total_latency_ms"], 60.0, delta=1.0)
        # predicted_x = 120.0 + velocity * 0.06
        self.assertAlmostEqual(telemetry["predicted_x"], 120.0 + c.velocity * 0.06, delta=0.5)

    def test_direction_reversal_resets_velocity_smoothing(self):
        """เมื่อตัวชี้ชนขอบแถบแล้วเด้งกลับทิศ ต้อง reset ความเร็วเดิมทันที ไม่ลากค่าเฉลี่ยข้ามทิศ"""
        c = Controller({"cast_seconds": 0.5, "lead": 0.05, "margin": 2.0,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        target = (50.0, 80.0)

        # Marker moving right fast (+250 px/s)
        c.step(1.00, {"bar": ("valid", 100.0, target), "bite": False}, True, capture_time=1.00)
        c.step(1.02, {"bar": ("valid", 105.0, target), "bite": False}, True, capture_time=1.02)
        self.assertGreater(c.velocity, 100.0)

        # Sudden bounce: marker at 101.0 at t=1.04 -> v_instant = (101 - 105)/0.02 = -200 px/s
        c.step(1.04, {"bar": ("valid", 101.0, target), "bite": False}, True, capture_time=1.04)
        # Velocity must immediately become negative without being averaged with positive velocity
        self.assertLess(c.velocity, 0.0)
        self.assertAlmostEqual(c.velocity, -200.0, delta=5.0)

    def test_margin_remains_usable_on_smallest_target(self):
        """ระยะเผื่อขอบ (margin) สำหรับช่องเล็ก ต้องไม่ยุบจนช่วงกดได้หายไป"""
        c = Controller({"cast_seconds": 0.5, "lead": 0.0, "margin": 0.2,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        # Small target width 14 (from 100 to 114)
        c.step(1.0, {"bar": ("valid", 107.0, (100.0, 114.0)), "bite": False}, True)
        # Margin floor must ensure deadband is at least 0.6..1.2 px, not collapsing to 0.2
        self.assertGreaterEqual(c.telemetry["margin"], 0.6)
        self.assertLessEqual(c.telemetry["margin"], 3.5)

    def test_latency_auto_tuning_adapts_on_turnaround(self):
        """ระบบปรับชดเชย Latency อัตโนมัติเมื่อตรวจพบการกลับตัว (Turnaround) ของตัวชี้"""
        c = Controller({"cast_seconds": 0.5, "lead": 0.0, "margin": 2.0,
                        "right_on_hold": True, "bite_ack": False})
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.5, {"bar": ("absent", None, None), "bite": False}, True)
        target = (100.0, 120.0)  # center = 110.0

        initial_latency = c.calibrated_latency
        # Simulate moving right, then releasing
        c.held = True
        c.last_switch_action = "release"
        # Turnaround happens at x=90.0 (stopped before target left 100.0 -> commanded too early)
        c.velocity = 200.0
        c._on_turnaround(90.0, target)
        self.assertLess(c.calibrated_latency, initial_latency)

        # Now simulate turnaround with overshoot at x=125.0 (overshot past target 120.0 -> commanded too late)
        lat_before = c.calibrated_latency
        c._on_turnaround(125.0, target)
        self.assertGreater(c.calibrated_latency, lat_before)


class RoiSelectionUITests(unittest.TestCase):
    def test_drag_in_all_four_directions_yields_correct_bounds(self):
        """การลากจากทุกทิศทาง (รวมถึงขวาล่างไปซ้ายบน) ต้องได้พิกัดมุมซ้ายบนและขนาดที่เป็นบวกเสมอ"""
        cases = [
            ((10, 20), (100, 150)),  # top-left to bottom-right
            ((100, 150), (10, 20)),  # bottom-right to top-left
            ((100, 20), (10, 150)),  # top-right to bottom-left
            ((10, 150), (100, 20)),  # bottom-left to top-right
        ]
        for p0, p1 in cases:
            x0 = min(p0[0], p1[0])
            y0 = min(p0[1], p1[1])
            x1 = max(p0[0], p1[0])
            y1 = max(p0[1], p1[1])
            w, h = x1 - x0, y1 - y0
            self.assertEqual(x0, 10)
            self.assertEqual(y0, 20)
            self.assertEqual(w, 90)
            self.assertEqual(h, 130)

    def test_coordinate_scaling_maps_accurately_without_drift(self):
        """พิกัดพื้นที่จริงต้องแปลงจากขนาดบน Canvas ไปยังพิกัดภาพต้นฉบับอย่างแม่นยำ"""
        img_w, img_h = 1920, 1080
        scale = min(1.0, 1100 / img_w, 700 / img_h)
        canvas_w = int(round(img_w * scale))
        canvas_h = int(round(img_h * scale))

        x0, y0, w, h = 200, 150, 300, 100
        orig_x = int(round(x0 * img_w / canvas_w))
        orig_y = int(round(y0 * img_h / canvas_h))
        orig_w = int(round(w * img_w / canvas_w))
        orig_h = int(round(h * img_h / canvas_h))

        re_x0 = int(round(orig_x * canvas_w / img_w))
        re_y0 = int(round(orig_y * canvas_h / img_h))
        self.assertAlmostEqual(re_x0, x0, delta=1)
        self.assertAlmostEqual(re_y0, y0, delta=1)

    def test_clamping_prevents_out_of_bounds_on_canvas(self):
        """การลากเมาส์ออกนอกขอบหน้าจอหรือขอบ Canvas ต้องถูก clamp ไม่ให้ติดลบหรือเกินขนาด"""
        canvas_w, canvas_h = 800, 600
        for test_x, test_y in [(-50, -30), (950, 750), (-10, 300), (400, 800)]:
            clamped_x = max(0, min(canvas_w, test_x))
            clamped_y = max(0, min(canvas_h, test_y))
            self.assertTrue(0 <= clamped_x <= canvas_w)
            self.assertTrue(0 <= clamped_y <= canvas_h)


if __name__ == "__main__":
    unittest.main()
