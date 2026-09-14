"""Tests covering Track resilience, focus grace period, auto-resumption,
window identity decoupling, and mode-specific input isolation.
"""

import collections
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

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

    # 8. ภาพกำกวมชั่วคราว: ปล่อยเมาส์ทันที, รีเซ็ตความเร็ว, ฟื้นตัวเมื่อ valid 2 เฟรม
    def test_8_ambiguous_detection_releases_mouse_and_auto_resumes(self):
        c = Controller(self.cfg)
        c.start(0.0)
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        act = c.step(0.2, obs_valid, True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act, "hold")
        self.assertTrue(c.held)

        obs_ambiguous = {"bar": ("ambiguous", None, None), "bite": False}
        # Frame 1 of ambiguous: MUST safely release mouse, not keep holding
        act_amb1 = c.step(0.3, obs_ambiguous, focused=True)
        self.assertEqual(act_amb1, "release")
        self.assertFalse(c.held)
        self.assertEqual(c.velocity, 0.0)
        self.assertIsNone(c.previous_x)

        # Ambiguous persists for several frames (< timeout)
        for i in range(1, 10):
            t = 0.3 + i * 0.02
            act_amb = c.step(t, obs_ambiguous, focused=True)
            self.assertEqual(act_amb, "none")
            self.assertFalse(c.held)
            self.assertNotEqual(c.state, "Paused")

        # Valid returns - Frame 1: stabilizes, no premature hold
        act_ret1 = c.step(0.6, obs_valid, focused=True)
        self.assertEqual(act_ret1, "none")
        self.assertFalse(c.held)
        self.assertEqual(c.velocity, 0.0)

        # Valid returns - Frame 2: fully resumes tracking
        act_ret2 = c.step(0.65, obs_valid, focused=True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act_ret2, "hold")
        self.assertTrue(c.held)

    # 9. ภาพกำกวมต่อเนื่องเกิน timeout: พักการทำงานอย่างปลอดภัยอิงเวลาจริง ไม่ใช่นับเฟรม
    def test_9_ambiguous_monotonic_timeout_not_fps_dependent(self):
        c = Controller(self.cfg)
        c.start(0.0)
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.2, obs_valid, True)

        obs_ambiguous = {"bar": ("ambiguous", None, None), "bite": False}
        # At 120 FPS (dt = 0.008s), 35 frames is only 0.28 seconds -> MUST NOT pause yet!
        for i in range(35):
            t = 0.3 + i * 0.008
            c.step(t, obs_ambiguous, focused=True)
            self.assertNotEqual(c.state, "Paused", f"Incorrectly paused after only {t - 0.3:.2f}s")

        # After monotonic timeout (default 3.0s, e.g. at t = 3.5s)
        act_timeout = c.step(3.5, obs_ambiguous, focused=True)
        self.assertEqual(c.state, "Paused")
        self.assertIn("ambiguous", c.reason.lower())

    # 10. คำสั่ง Stop/F8 ระหว่างรอฟื้น: ภาพ valid ในภายหลังต้องไม่ทำงานต่อเอง
    def test_10_user_stop_during_ambiguous_recovery_prevents_auto_resume(self):
        c = Controller(self.cfg)
        c.start(0.0)
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.2, obs_valid, True)

        obs_ambiguous = {"bar": ("ambiguous", None, None), "bite": False}
        c.step(0.3, obs_ambiguous, focused=True)

        # User presses Stop
        c.stop("stopped by user")
        self.assertEqual(c.state, "Paused")

        # Future valid frames must do nothing
        act_future1 = c.step(0.4, obs_valid, focused=True)
        act_future2 = c.step(0.5, obs_valid, focused=True)
        self.assertEqual(act_future1, "none")
        self.assertEqual(act_future2, "none")
        self.assertEqual(c.state, "Paused")

    # 11. Wait timeout: รอต่อเมื่อยังไม่ครบ, เริ่มรอบใหม่เมื่อครบและ absent, ไม่เหวี่ยงซ้ำเมื่อ valid, หยุดเมื่อครบ retry, รีเซ็ต retry เมื่อสำเร็จ
    def test_11_wait_timeout_and_bounded_recast_recovery(self):
        cfg = dict(self.cfg)
        cfg["cast_seconds"] = 1.0
        cfg["wait_timeout"] = 5.0
        cfg["max_retries"] = 2
        c = Controller(cfg)
        c.start(0.0)

        obs_absent = {"bar": ("absent", None, None), "bite": False}
        obs_ambiguous = {"bar": ("ambiguous", None, None), "bite": False}
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}

        # 1. Cast phase (0.0 to 1.0)
        c.step(0.0, obs_absent, True)
        self.assertEqual(c.state, "Cast")
        c.step(1.0, obs_absent, True)
        self.assertEqual(c.state, "Wait")

        # 2. Before timeout (2.0s elapsed in Wait, timeout is 5.0s) -> ยังไม่ครบ timeout รอต่อ
        act_wait = c.step(3.0, obs_absent, True)
        self.assertEqual(c.state, "Wait")
        self.assertEqual(act_wait, "none")

        # 3. Timeout reached but detection is ambiguous -> ห้ามเหวี่ยงใหม่ขณะข้อมูลกำกวม
        act_amb = c.step(6.1, obs_ambiguous, True)
        self.assertEqual(c.state, "Wait")
        self.assertEqual(act_amb, "none")

        # 4. Timeout and confirmed absent -> เริ่มรอบใหม่ครั้งเดียว (retry 1/2)
        act_recast = c.step(6.2, obs_absent, True)
        self.assertEqual(c.state, "Cast")
        self.assertEqual(act_recast, "hold")
        self.assertEqual(c.retry_count, 1)

        # Cast completes (6.2 to 7.2) -> enters Wait again
        c.step(7.2, obs_absent, True)
        self.assertEqual(c.state, "Wait")

        # 5. Valid comes before timeout edge -> ติดตามทันที ไม่เหวี่ยงซ้ำ และรีเซ็ต retry
        act_track = c.step(10.0, obs_valid, True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act_track, "hold")
        self.assertEqual(c.retry_count, 0)

        # 6. Test max retry limit: Controller hits max retries -> pauses with reason
        c2 = Controller(cfg)
        c2.start(0.0)
        c2.step(0.0, obs_absent, True)
        c2.step(1.0, obs_absent, True)  # Wait started at t=1.0

        # Wait timeout 1 -> Cast
        c2.step(6.0, obs_absent, True)
        self.assertEqual(c2.state, "Cast")
        self.assertEqual(c2.retry_count, 1)

        # Cast ends -> Wait started at t=7.0
        c2.step(7.0, obs_absent, True)
        self.assertEqual(c2.state, "Wait")

        # Wait timeout 2 -> Cast
        c2.step(12.0, obs_absent, True)
        self.assertEqual(c2.state, "Cast")
        self.assertEqual(c2.retry_count, 2)

        # Cast ends -> Wait started at t=13.0
        c2.step(13.0, obs_absent, True)
        self.assertEqual(c2.state, "Wait")

        # Wait timeout 3 -> Exceeds max_retries (2) -> Stops safely
        act_stop = c2.step(18.0, obs_absent, True)
        self.assertEqual(c2.state, "Paused")
        self.assertEqual(act_stop, "none")
        self.assertIn("retries", c2.reason.lower())

    # 12. การจำลองระยะยาวด้วยเวลาเสมือน (Virtual-Time Long-Running Simulation)
    def test_12_long_running_virtual_time_simulation(self):
        """Simulate multi-cycle fishing session with injected glitches, ambiguous frames, and recovery."""
        cfg = dict(self.cfg)
        cfg["cast_seconds"] = 1.0
        cfg["wait_timeout"] = 15.0
        cfg["max_retries"] = 3
        c = Controller(cfg)
        c.start(0.0)

        # Worker physical state tracking
        worker_mouse_held = False

        def apply_mock_worker_action(action: str) -> None:
            nonlocal worker_mouse_held
            if action == "hold":
                worker_mouse_held = True
            elif action in ("release", "click"):
                worker_mouse_held = False

        now = 0.0
        dt = 0.01  # 100 FPS tick rate
        completed_cycles = 0
        recast_count = 0

        # Simulate 6 full fishing cycles
        while completed_cycles < 6 and now < 600.0:
            # 1. Cast phase
            while c.state == "Cast" and now < 600.0:
                obs = {"bar": ("absent", None, None), "bite": False}
                c.sync_held(worker_mouse_held)
                action = c.step(now, obs, focused=True)
                if action == "none" and c.desired_held != worker_mouse_held:
                    action = "hold" if c.desired_held else "release"
                apply_mock_worker_action(action)
                c.acknowledge_action(action, True)
                self.assertEqual(c.held, worker_mouse_held)
                now += dt

            self.assertEqual(c.state, "Wait")
            self.assertFalse(worker_mouse_held)

            # 2. Wait phase: in cycle 2, simulate a missed bite that times out and recasts
            simulate_miss = (completed_cycles == 2 and recast_count == 0)
            wait_duration = 16.0 if simulate_miss else 4.0
            wait_end = now + wait_duration

            while c.state == "Wait" and now < wait_end:
                obs = {"bar": ("absent", None, None), "bite": False}
                c.sync_held(worker_mouse_held)
                action = c.step(now, obs, focused=True)
                apply_mock_worker_action(action)
                c.acknowledge_action(action, True)
                now += dt

            if simulate_miss:
                self.assertEqual(c.state, "Cast", "Controller should recast on wait timeout when absent")
                self.assertTrue(worker_mouse_held, "Entering cast should hold mouse")
                recast_count += 1
                continue

            self.assertFalse(worker_mouse_held)

            # 3. Minigame Tracking phase (lasts ~8 seconds)
            minigame_end = now + 8.0
            marker_x = 50.0
            target_range = (40.0, 60.0)
            tick_in_minigame = 0

            while now < minigame_end:
                tick_in_minigame += 1

                # Inject a momentary capture glitch at tick 50 (3 frames missing)
                if 50 <= tick_in_minigame <= 52:
                    # Worker sees capture glitch -> releases mouse & resets kinematics
                    worker_mouse_held = False
                    c.sync_held(False)
                    c.previous_x = None
                    c.previous_time = None
                    c.velocity = 0.0
                    now += dt
                    continue

                # Inject an ambiguous detection at tick 120 (8 frames ambiguous)
                if 120 <= tick_in_minigame <= 127:
                    obs = {"bar": ("ambiguous", None, None), "bite": False}
                else:
                    # Target moving sinusoidally, marker responding to mouse hold
                    target_center = 50.0 + 20.0 * np.sin(now * 1.5)
                    target_range = (target_center - 10.0, target_center + 10.0)
                    if worker_mouse_held:
                        marker_x += 120.0 * dt
                    else:
                        marker_x -= 120.0 * dt
                    marker_x = max(10.0, min(140.0, marker_x))
                    obs = {"bar": ("valid", marker_x, target_range), "bite": False}

                c.sync_held(worker_mouse_held)
                action = c.step(now, obs, focused=True)
                if action == "none" and c.desired_held != worker_mouse_held:
                    action = "hold" if c.desired_held else "release"
                apply_mock_worker_action(action)
                c.acknowledge_action(action, True)

                # Verification: mouse_held must match
                self.assertEqual(c.held, worker_mouse_held)
                self.assertNotEqual(c.state, "Paused", f"Unexpected pause at t={now:.2f}")

                # During ambiguous, mouse must not be held
                if 120 <= tick_in_minigame <= 127:
                    self.assertFalse(worker_mouse_held)

                now += dt

            # 4. Minigame ends: bar disappears until recast into Cast
            end_start = now
            while c.state in ("Track", "End") and now < end_start + 3.0:
                obs = {"bar": ("absent", None, None), "bite": False}
                c.sync_held(worker_mouse_held)
                action = c.step(now, obs, focused=True)
                apply_mock_worker_action(action)
                c.acknowledge_action(action, True)
                now += dt

            self.assertEqual(c.state, "Cast", "Controller should auto-recast after minigame end")
            completed_cycles += 1

        self.assertEqual(completed_cycles, 6)
        self.assertEqual(recast_count, 1)

        # 5. User issues Stop / F8 -> state Paused, mouse released
        stop_action = c.stop("F8 emergency stop")
        self.assertEqual(c.state, "Paused")
        self.assertFalse(c.held)

    def test_13_gamescope_continuous_recovery_survives_ten_minutes(self):
        cfg = dict(self.cfg)
        cfg["continuous_recovery"] = True
        cfg["ambiguous_timeout"] = 3.0
        c = Controller(cfg)
        c.start(0.0)
        obs_valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        obs_ambiguous = {"bar": ("ambiguous", None, None), "bite": False}

        c.step(0.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.1, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(0.2, obs_valid, True)

        # Recovery must remain armed through a long intermittent outage.
        c.step(4.0, obs_ambiguous, True)
        c.step(10.0, obs_ambiguous, True)
        c.step(300.0, {"bar": ("absent", None, None), "bite": False}, True)
        c.step(300.1, obs_ambiguous, True)
        self.assertEqual(c.state, "Track")
        c.step(600.0, obs_ambiguous, True)
        self.assertEqual(c.state, "Track")
        self.assertFalse(c.held)

        # Two stable valid frames safely resume tracking after more than 10 minutes.
        self.assertEqual(c.step(600.1, obs_valid, True), "none")
        self.assertEqual(c.step(600.2, obs_valid, True), "hold")
        self.assertEqual(c.state, "Track")
        self.assertTrue(c.held)

        # Explicit stop remains terminal; later valid frames cannot auto-resume.
        c.stop("F8 emergency stop")
        self.assertEqual(c.step(601.0, obs_valid, True), "none")
        self.assertEqual(c.state, "Paused")

    def test_14_gamescope_exhausted_wait_retries_stays_passive_until_target_returns(self):
        cfg = dict(self.cfg)
        cfg.update({"continuous_recovery": True, "cast_seconds": 0.1,
                    "wait_timeout": 0.2, "max_retries": 0})
        c = Controller(cfg)
        c.start(0.0)
        absent = {"bar": ("absent", None, None), "bite": False}
        valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}

        c.step(0.0, absent, True)
        c.step(0.1, absent, True)
        self.assertEqual(c.state, "Wait")
        self.assertEqual(c.step(0.4, absent, True), "none")
        self.assertEqual(c.state, "Wait")
        self.assertEqual(c.step(0.5, valid, True), "hold")
        self.assertEqual(c.state, "Track")

    def test_15_gamescope_recovery_requires_two_valid_frames_after_absent(self):
        cfg = dict(self.cfg)
        cfg["continuous_recovery"] = True
        c = Controller(cfg)
        c.start(0.0)
        valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        ambiguous = {"bar": ("ambiguous", None, None), "bite": False}
        absent = {"bar": ("absent", None, None), "bite": False}

        c.step(0.0, absent, True)
        c.step(0.1, absent, True)
        c.step(0.2, valid, True)
        c.step(0.3, ambiguous, True)
        self.assertEqual(c.step(0.4, valid, True), "none")
        self.assertEqual(c.step(0.5, absent, True), "none")
        self.assertEqual(c.state, "Track")
        self.assertEqual(c.step(0.6, valid, True), "none")
        self.assertEqual(c.step(0.8, absent, True), "none")
        self.assertEqual(c.state, "Track")
        self.assertEqual(c.step(0.9, valid, True), "none")
        self.assertEqual(c.step(1.0, valid, True), "hold")

    def test_16_gamescope_sustained_absent_after_ambiguity_completes_cycle(self):
        cfg = dict(self.cfg)
        cfg.update({"continuous_recovery": True, "cast_seconds": 0.1})
        c = Controller(cfg)
        c.start(0.0)
        valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        ambiguous = {"bar": ("ambiguous", None, None), "bite": False}
        absent = {"bar": ("absent", None, None), "bite": False}

        c.step(0.0, absent, True)
        c.step(0.1, absent, True)
        c.step(0.2, valid, True)
        c.step(0.3, ambiguous, True)
        c.step(0.4, absent, True)
        c.step(0.7, absent, True)
        self.assertEqual(c.state, "End")
        c.step(1.5, absent, True)
        self.assertEqual(c.state, "Cast")

    def test_17_narrow_roi_clipped_marker_detected_as_valid(self):
        import cv2
        fixture_path = ROOT / "tests" / "fixtures" / "night-minigame-crop.png"
        if not fixture_path.is_file():
            self.skipTest("night-minigame-crop.png fixture not available")
        img = cv2.imread(str(fixture_path))
        # Test narrow 16px high crops at y offsets where marker touches the boundary
        for y in (12, 16, 20, 24, 28, 32):
            crop = img[y:y + 16, :]
            status, marker, target = auto_fishing.detect_bar(crop)
            self.assertEqual(status, "valid", f"Crop at y={y} with h=16 should detect valid target and marker")
            self.assertIsNotNone(marker)
            self.assertIsNotNone(target)

    def test_18_track_tolerates_short_absent_glitch_without_ending(self):
        cfg = dict(self.cfg)
        cfg["absent_timeout"] = 0.5
        c = Controller(cfg)
        c.start(0.0)
        valid = {"bar": ("valid", 20.0, (40.0, 60.0)), "bite": False}
        absent = {"bar": ("absent", None, None), "bite": False}

        c.step(0.0, absent, True)
        c.step(0.1, absent, True)
        c.step(0.2, valid, True)
        self.assertEqual(c.state, "Track")

        # 0.2s absent glitch (< 0.5s absent_timeout)
        act = c.step(0.3, absent, True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act, "release")
        self.assertFalse(c.held)

        act2 = c.step(0.45, absent, True)
        self.assertEqual(c.state, "Track")
        self.assertEqual(act2, "none")

        # Returns to valid before absent_timeout expires
        act3 = c.step(0.55, valid, True)
        self.assertEqual(c.state, "Track")

    def test_19_continuous_recovery_auto_recasts_after_wait_timeout_cooldown(self):
        cfg = dict(self.cfg)
        cfg.update({
            "continuous_recovery": True,
            "cast_seconds": 0.1,
            "wait_timeout": 0.2,
            "max_retries": 0,
            "continuous_wait_cooldown": 0.3,
        })
        c = Controller(cfg)
        c.start(0.0)
        absent = {"bar": ("absent", None, None), "bite": False}

        c.step(0.0, absent, True)
        c.step(0.1, absent, True)
        self.assertEqual(c.state, "Wait")

        # Within cooldown (< wait_timeout + cooldown): passive waiting
        self.assertEqual(c.step(0.35, absent, True), "none")
        self.assertEqual(c.state, "Wait")

        # Past cooldown (t >= 0.1 + 0.2 + 0.3 = 0.6): auto-recast into Cast!
        act = c.step(0.65, absent, True)
        self.assertEqual(c.state, "Cast")
        self.assertIn("auto-recasting", c.reason)



if __name__ == "__main__":
    unittest.main()
