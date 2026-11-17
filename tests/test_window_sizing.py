"""窗口尺寸决策（低分辨率屏幕不裁掉底部按钮）。

背景：Windows 上有些屏幕分辨率/缩放不够，默认 1440×900 直接开出来会把底部
操作按钮顶出屏幕（标题栏在屏内、窗口底边被裁），最大化才看得见。
修复：按当前屏幕可用区域决定窗口该多大、是否启动即最大化（_resolve_* 纯函数）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402
from gui import _resolve_start_window_geometry  # noqa: E402


class WindowSizingTests(unittest.TestCase):
    def test_fullhd_stays_windowed(self):
        """1920×1080：正常窗口模式，不最大化。"""
        start_max, w, h, minw, minh = _resolve_start_window_geometry(1920, 1040)
        self.assertFalse(start_max)
        self.assertEqual((w, h), (1440, 900))

    def test_high_dpi_scale_push_fullhd_into_shortus_area(self):
        """任务栏吃掉后只剩 ~728 的可用高度：应缩小到屏幕内，不裁按钮。"""
        start_max, w, h, minw, minh = _resolve_start_window_geometry(1366, 728)
        self.assertFalse(start_max)
        self.assertLessEqual(h, 728, "窗口高度不能超过可用高度")
        self.assertEqual(minh, 648)

    def test_very_short_screen_maximizes(self):
        """可用高度低于内容所需（<680）：缩了也挤不下，直接最大化。"""
        start_max, w, h, _, _ = _resolve_start_window_geometry(1024, 600)
        self.assertTrue(start_max)

    def test_zero_geometry_falls_back(self):
        start_max, w, h, minw, minh = _resolve_start_window_geometry(0, 0)
        self.assertFalse(start_max)
        self.assertEqual((w, h), (1440, 900))
        self.assertEqual((minw, minh), (1120, 720))

    def test_narrow_screen_clamps_width(self):
        start_max, w, h, minw, minh = _resolve_start_window_geometry(1280, 900)
        self.assertFalse(start_max)
        self.assertEqual(w, 1280, "宽度超屏时减到屏宽")
        # 最小宽度不会超过原定的 1120（保留紧凑屏的最小可用）
        self.assertEqual(minw, 1120)
        self.assertEqual(minw, min(1120, 1280 - 80))

    def test_window_uses_helper_at_init(self):
        """MainWindow.__init__ 必须走同一决策函数。"""
        import inspect
        src = inspect.getsource(gui.MainWindow.__init__)
        self.assertIn("_resolve_start_window_geometry", src)
        self.assertIn("_start_maximized", src)

    def test_show_event_applies_maximize(self):
        """showEvent 里才落地最大化，避免被 show() 重置。"""
        import inspect
        src = inspect.getsource(gui.MainWindow.showEvent)
        self.assertIn("_start_maximized", src)
        self.assertIn("showMaximized", src)

    def test_no_stale_large_default_after_fix(self):
        """不再硬编码 resize(1440,900) 或 setMinimumSize(1120,720)。"""
        import inspect
        src = inspect.getsource(gui.MainWindow.__init__)
        self.assertNotIn("self.resize(1440, 900)", src)
        self.assertNotIn("self.setMinimumSize(1120, 720)", src)


if __name__ == "__main__":
    unittest.main()
