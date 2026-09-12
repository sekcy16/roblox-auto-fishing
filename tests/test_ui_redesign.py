"""Comprehensive unit & visual tests for the redesigned Auto Fishing UI."""
import os
import unittest
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

# Set runtime libraries if available
runtime_lib = ROOT / ".runtime" / "usr" / "lib"
if runtime_lib.is_dir():
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{runtime_lib}:{current_ld}" if current_ld else str(runtime_lib)
    os.environ["TK_LIBRARY"] = str(runtime_lib / "tk8.6")

import auto_fishing


class UiRedesignVerificationTests(unittest.TestCase):
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

    def test_theme_and_structure_hierarchy_present(self):
        """ตรวจสอบว่า UI มีส่วนประกอบ Header, Status Pill, Preview, 3 ขั้นตอน และ Advanced ครบถ้วน"""
        app = self.app
        self.assertTrue(hasattr(app, "status_pill"))
        self.assertTrue(hasattr(app, "status_text_lbl"))
        self.assertTrue(hasattr(app, "preview_canvas"))
        self.assertTrue(hasattr(app, "preview_badge"))
        self.assertTrue(hasattr(app, "roi_size_label"))
        self.assertTrue(hasattr(app, "preview_detection_label"))
        self.assertTrue(hasattr(app, "btn_select_roi"))
        self.assertTrue(hasattr(app, "chip_target"))
        self.assertTrue(hasattr(app, "chip_marker"))
        self.assertTrue(hasattr(app, "main_action_btn"))
        self.assertTrue(hasattr(app, "action_guidance_lbl"))
        self.assertTrue(hasattr(app, "advanced_btn"))
        self.assertTrue(hasattr(app, "advanced_frame"))

    def test_placeholder_rendered_when_no_roi(self):
        """เมื่อไม่มีภาพ ให้แสดง placeholder บน canvas พร้อมคำแนะนำ"""
        self.app._draw_preview_placeholder()
        self.root.update()
        items = self.app.preview_canvas.find_all()
        self.assertGreater(len(items), 0)
        self.assertIn("ยังไม่ได้เลือกพื้นที่", self.app.preview_canvas.itemcget(items[-2], "text"))

    def test_preview_rendering_with_valid_fixture(self):
        """เมื่อส่งภาพแถบมินิเกมเข้ามา ต้องตรวจจับช่องม่วงและตัวชี้ พร้อมอัปเดตชิปสถานะและวาด overlay"""
        fixture_path = ROOT / "tests" / "fixtures" / "night-minigame-crop.png"
        img = np.asarray(Image.open(fixture_path).convert("RGB"))[:, :, ::-1]  # BGR
        self.app._update_preview_display(img)
        self.root.update()

        # Check Step 2 Chips
        target_chip_text = self.app.chip_target.cget("text")
        marker_chip_text = self.app.chip_marker.cget("text")
        self.assertIn("ช่องม่วง: พบ", target_chip_text)
        self.assertIn("ตัวชี้: พบ", marker_chip_text)

        # Check preview footer
        self.assertIn("415 × 65 px", self.app.roi_size_label.cget("text"))
        self.assertIn("สมบูรณ์", self.app.preview_detection_label.cget("text"))
        self.assertIn("ภาพสด", self.app.preview_badge.cget("text"))

        # Check canvas has photo and overlays
        items = self.app.preview_canvas.find_all()
        self.assertGreater(len(items), 3)

    def test_toggle_advanced_settings_accordion(self):
        """กดปุ่มตั้งค่าขั้นสูง ต้องสลับการแสดงผลแบบ Accordion"""
        self.assertFalse(self.app.advanced_visible)
        self.app.toggle_advanced()
        self.root.update()
        self.assertTrue(self.app.advanced_visible)
        self.assertIn("▾", self.app.advanced_btn.cget("text"))

        self.app.toggle_advanced()
        self.root.update()
        self.assertFalse(self.app.advanced_visible)
        self.assertIn("▸", self.app.advanced_btn.cget("text"))

    def test_status_pill_and_guidance_state_transitions(self):
        """การเปลี่ยนสถานะต้องอัปเดต Status Pill และ Action Guidance อย่างสอดคล้อง"""
        app = self.app

        # Error / Blocked state
        app._set_status("เริ่มไม่ได้: กรุณาเลือกหน้าต่างเกม")
        self.root.update()
        self.assertIn("ข้อผิดพลาด", app.status_text_lbl.cget("text"))
        self.assertIn("🔴", app.action_guidance_lbl.cget("text"))

        # Working / Tracking state
        app._set_status("Track: tracking | 60.0 FPS")
        self.root.update()
        self.assertIn("กำลังทำงาน", app.status_text_lbl.cget("text"))
        self.assertIn("🟢", app.action_guidance_lbl.cget("text"))

        # Target lost / searching state
        app._set_status("ตรวจไม่พบเป้าหมาย กำลังค้นหาใหม่")
        self.root.update()
        self.assertIn("ค้นหา", app.status_text_lbl.cget("text"))
        self.assertIn("🟡", app.action_guidance_lbl.cget("text"))

        # Stopped state
        app.stop("หยุดการทำงานแล้ว")
        self.root.update()
        self.assertIn("หยุด", app.status_text_lbl.cget("text"))


if __name__ == "__main__":
    unittest.main()
