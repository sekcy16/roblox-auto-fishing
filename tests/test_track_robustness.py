"""Tests covering Track resilience, focus grace period, auto-resumption,
window identity decoupling, and mode-specific input isolation.
"""

import collections
import sys
import unittest
from unittest import mock

import auto_fishing
import numpy as np
from auto_fishing import (
    Controller,
    apply_action,
    _inside_rect,
    is_window_alive,
    get_window_bounds,
)


class TrackRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "fishing_mode": "rod",
            "cast_seconds": 0.1,
            "right_on_hold": True,
            "margin": 3.0,
            "lead": 0.05,
            "bite_threshold": 0.85,
            "bite_ack": False,
            "debug": False,
            "auto_refocus": False,
            "rois": {"bar": [200, 200, 150, 30], "bite": None},
        }

    @unittest.skipIf(auto_fishing.cv2 is None, "OpenCV unavailable")
    def test_detector_prefers_white_marker_inside_purple_target(self):
        image = np.zeros((80, 300, 3), dtype=np.uint8)
        cv2 = auto_fishing.cv2
        cv2.rectangle(image, (100, 30), (180, 50), (180, 70, 180), -1)
        cv2.rectangle(image, (137, 27), (143, 53), (255, 255, 255), -1)
        cv2.rectangle(image, (240, 27), (246, 53), (255, 255, 255), -1)

        status, marker, target = auto_fishing.detect_bar(image)

        self.assertEqual(status, "valid")
        self.assertAlmostEqual(marker, 140.0, delta=1.0)
        self.assertEqual(target, (100.0, 181.0))

    # 1. เสียโฟกัสหนึ่งเฟรม: เมาส์ถูกปล่อย, ไม่หยุดถาวร (FocusWait), กลับมาทำงานต่อเอง
    def test_1_momentary_focus_loss_releases_mouse_and_auto_resumes(self):
        c = Controller(self.cfg)
        c.start(0.0)

        # Enter Track and verify hold
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        act = c.step(0.2, obs_valid, True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act, "hold")
        self.assertTrue(c.held)

        # Frame 1 of focus loss: mouse MUST be released, state becomes FocusWait (not Paused)
        act_loss = c.step(0.3, obs_valid, focused=False)
        self.assertEqual(act_loss, "release")
        self.assertFalse(c.held)
        self.assertEqual(c.state, "FocusWait")
        self.assertIn("focus", c.reason.lower())

        # Focus returns - Frame 1: must stabilize without issuing click/hold
        act_return1 = c.step(0.4, obs_valid, focused=True)
        self.assertEqual(act_return1, "none")
        self.assertFalse(c.held)
        self.assertEqual(c.state, "FocusWait")

        # Focus returns - Frame 2: 2nd consecutive stable frame resumes Track
        act_return2 = c.step(0.45, obs_valid, focused=True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act_return2, "hold")
        self.assertTrue(c.held)

    # 2. เสียโฟกัสเป็นเวลานาน: ไม่มีการคลิก, อยู่ในสถานะรอ, ยังสามารถกด Stop ได้
    def test_2_extended_focus_loss_stays_waiting_and_allows_stop(self):
        c = Controller(self.cfg)
        c.start(0.0)
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.2, obs_valid, True)
        self.assertEqual(c.state, "Track")

        # Lose focus for 20 frames
        for i in range(20):
            t = 1.0 + i * 0.05
            act = c.step(t, obs_valid, focused=False)
            self.assertIn(act, ("none", "release"))
            self.assertFalse(c.held)
            self.assertEqual(c.state, "FocusWait")
            self.assertIn("focus", c.reason.lower())

        # User presses Stop
        stop_act = c.stop("stopped by user")
        self.assertEqual(stop_act, "none")
        self.assertEqual(c.state, "Paused")
        self.assertEqual(c.reason, "stopped by user")

    # 3. กลับมาโฟกัส: velocity และประวัติถูก reset, ไม่คลิกในเฟรมแรก, รอตรวจจับ 2 เฟรม
    def test_3_resumption_kinematics_reset_and_two_frame_stabilization(self):
        c = Controller(self.cfg)
        c.start(0.0)
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)

        # Tracking with motion at t=0.2 and t=0.25
        c.step(0.2, {"bar": ("valid", 30.0, (40.0, 60.0)), "bite": False}, True)
        c.step(0.25, {"bar": ("valid", 40.0, (40.0, 60.0)), "bite": False}, True)
        self.assertNotEqual(c.velocity, 0.0)

        # Lose focus at t=0.3
        c.step(0.3, {"bar": ("valid", 40.0, (40.0, 60.0)), "bite": False}, False)
        self.assertEqual(c.state, "FocusWait")
        self.assertEqual(c.velocity, 0.0)
        self.assertIsNone(c.previous_x)
        self.assertIsNone(c.previous_time)

        # Regain focus at t=2.0 (1.7s gap) with marker moved far away (e.g. at 80.0)
        obs_jump = {"bar": ("valid", 80.0, (40.0, 60.0)), "bite": False}
        act1 = c.step(2.0, obs_jump, focused=True)
        # Frame 1: must NOT click, must NOT calculate spurious velocity across 1.7s gap
        self.assertEqual(act1, "none")
        self.assertEqual(c.state, "FocusWait")
        self.assertEqual(c.velocity, 0.0)
        self.assertEqual(c.previous_x, 80.0)

        # Frame 2 at t=2.05 (normal dt=0.05s) with marker at 81.0
        obs_frame2 = {"bar": ("valid", 81.0, (40.0, 60.0)), "bite": False}
        act2 = c.step(2.05, obs_frame2, focused=True)
        self.assertEqual(c.state, "Track")
        # Velocity is filtered (0.70 * v_instant + 0.30 * 0.0 = 14.0 px/s)
        # computed only between t=2.0 and 2.05, proving it did NOT compute across the 1.7s pause
        expected_v = 0.70 * ((81.0 - 80.0) / 0.05)
        self.assertAlmostEqual(c.velocity, expected_v, delta=1.0)

    # 4. หน้าต่างเดิมถูกย้ายหรือปรับขนาด: ไม่มองว่าเป็นคนละหน้าต่าง, bounds อัปเดต, ROI ตรวจใหม่
    def test_4_window_move_and_resize_identity_and_relative_rois(self):
        target_wid = 99999
        initial_bounds = (100, 100, 800, 600)
        target = (target_wid, initial_bounds)

        # Setup mock app with relative ROI
        app = mock.Mock()
        app.target = target
        app.settings = {
            "fishing_mode": "rod",
            "rois": {"bar": [200, 200, 150, 30]},
            "auto_refocus": False,
        }
        app._relative_rois = {"bar": (100, 100, 150, 30)}
        app.controller = mock.Mock(focus_lost_since=None)
        app.recent_events = collections.deque(maxlen=20)
        app._log_debug_event = lambda event, details=None: None

        # Window moved from (100, 100) to (300, 400)
        new_bounds = (300, 400, 800, 600)
        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.get_window_bounds", return_value=new_bounds), \
             mock.patch("auto_fishing.window_snapshot", return_value=(target_wid, new_bounds)), \
             mock.patch("auto_fishing._load_pyautogui") as mock_pag:
            mock_pag.return_value.position.return_value = (400, 500)

            status, cur_bounds = auto_fishing.BaseFishingApp._check_target_status(app)
            self.assertEqual(status, "ready")
            self.assertEqual(app.target[1], new_bounds)
            # Relative ROI shifted accordingly: 300 + 100 = 400, 400 + 100 = 500
            self.assertEqual(app.settings["rois"]["bar"], [400, 500, 150, 30])

        # If window is resized smaller so ROI is outside
        shrunk_bounds = (300, 400, 200, 150)
        with mock.patch("auto_fishing.is_window_alive", return_value=True), \
             mock.patch("auto_fishing.get_window_bounds", return_value=shrunk_bounds), \
             mock.patch("auto_fishing.window_snapshot", return_value=(target_wid, shrunk_bounds)):
            status, _ = auto_fishing.BaseFishingApp._check_target_status(app)
            self.assertEqual(status, "roi_outside")

        # If window is closed
        with mock.patch("auto_fishing.is_window_alive", return_value=False):
            status, _ = auto_fishing.BaseFishingApp._check_target_status(app)
            self.assertEqual(status, "closed")

    # 5. เมาส์ออกนอกหน้าต่างใน Desktop mode: ปล่อยเมาส์และรอ, กลับมาเมื่อเมาส์เข้า
    def test_5_mouse_outside_in_desktop_mode(self):
        target_wid = 99999
        bounds = (100, 100, 800, 600)
        target = (target_wid, bounds)

        # Mouse pointer is at (50, 50) outside (100, 100, 800, 600)
        with mock.patch("auto_fishing.window_snapshot", return_value=(target_wid, bounds)), \
             mock.patch("auto_fishing._load_pyautogui") as mock_pag, \
             mock.patch("auto_fishing.release_mouse") as mock_rel:
            mock_pag.return_value.position.return_value = (50, 50)
            res = apply_action("hold", target)
            self.assertFalse(res)
            mock_rel.assert_called()

        # Mouse pointer is at (200, 200) inside
        with mock.patch("auto_fishing.window_snapshot", return_value=(target_wid, bounds)), \
             mock.patch("auto_fishing._load_pyautogui") as mock_pag:
            mock_pag.return_value.position.return_value = (200, 200)
            res = apply_action("hold", target)
            self.assertTrue(res)

    # 6. Gamescope และ Windows Dual-screen: desktop focus ไม่ทำให้ Track หยุด
    @unittest.skipUnless(sys.platform.startswith("linux"), "Gamescope worker requires Linux")
    def test_6_gamescope_and_dual_screen_independent_of_desktop_focus(self):
        # Gamescope worker: doesn't check desktop window snapshot
        from gamescope_worker import GamescopeWorker
        worker = GamescopeWorker(None, ":99", 12345)
        worker.d = mock.Mock()
        worker.target_win = mock.Mock()
        # Viewable state inside Gamescope
        worker.target_win.get_attributes.return_value.map_state = 2  # X.IsViewable
        self.assertTrue(worker.check_target_valid())

        # Test retry streak on transient unmap (up to 4 frames doesn't kill worker)
        worker.target_win.get_attributes.side_effect = [Exception("glitch"), Exception("glitch")]
        worker.release_mouse = mock.Mock()
        worker.stop_fishing = mock.Mock()
        with mock.patch("time.sleep"):
            worker.tick()
            worker.tick()
        self.assertEqual(worker.target_lost_streak, 2)
        worker.stop_fishing.assert_not_called()

    # 7. การตรวจไม่พบแถบสีม่วงชั่วคราว: ระบบค้นหาใหม่, ไม่แจ้งผิดว่าเสียโฟกัส
    def test_7_purple_bar_temporary_absence_does_not_claim_focus_loss(self):
        c = Controller(self.cfg)
        c.start(0.0)
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        act = c.step(0.2, obs_valid, True)
        self.assertEqual(c.state, "Track")

        # A short dropout releases safely but remains in Track.
        obs_absent = {"bar": ("absent", None, None), "bite": False}
        act = c.step(0.3, obs_absent, focused=True)
        self.assertEqual(act, "release")
        self.assertEqual(c.state, "Track")
        self.assertNotIn("focus", c.reason.lower())

        # Purple bar reappears within the grace period -> resumes Track.
        act_resume = c.step(0.5, obs_valid, focused=True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act_resume, "hold")


if __name__ == "__main__":
    unittest.main()
