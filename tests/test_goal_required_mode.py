"""「自动识别应训时长」目标模式测试。

需求：学习中心「应训时长」行给出每年应完成的学时（集中培训 90 / 网络自学 50），
自动模式要能直接拿它当学习目标，同时保留原来的自定义数值模式。

覆盖三层：
  1. resolve_required_goal：页面值优先、读不到沿用上次记录、都没有则跳过该阶段；
  2. GoalScreen 的配置持久化：来源(custom/required)、显式开关、数值并存；
  3. 依赖 main 的学时解析（应训时长随 _get_study_hours 一起返回）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402


class ResolveRequiredGoalTests(unittest.TestCase):
    def test_page_value_wins(self):
        self.assertEqual(gui.resolve_required_goal(90, 50), (90.0, "page"))

    def test_falls_back_to_last_recorded(self):
        self.assertEqual(gui.resolve_required_goal(0, 50), (50.0, "cached"))
        self.assertEqual(gui.resolve_required_goal(None, 62.5), (62.5, "cached"))

    def test_missing_when_nothing_available(self):
        self.assertEqual(gui.resolve_required_goal(0, 0), (0.0, "missing"))
        self.assertEqual(gui.resolve_required_goal(None, None), (0.0, "missing"))

    def test_garbage_values_are_ignored(self):
        self.assertEqual(gui.resolve_required_goal("not-a-number", "50"),
                         (50.0, "cached"))
        self.assertEqual(gui.resolve_required_goal(-5, 0), (0.0, "missing"))

    def test_keeps_decimals(self):
        self.assertEqual(gui.resolve_required_goal(137.5, 0), (137.5, "page"))


class _FakeGoalScreen(gui.GoalScreen):
    """不构建 Qt 控件，只验证读写配置的逻辑。"""

    def __init__(self, config_path):
        self.config_path = config_path

    def _load_goal(self):
        super()._load_goal()

    def _persist(self, central_on, central_goal, central_mode,
                 online_on, online_goal, online_mode,
                 central_source, online_source):
        win = type("Win", (), {})()
        with patch("gui.CONFIG_PATH", self.config_path):
            gui.GoalScreen._on_done(
                self, central_on, central_goal, central_mode,
                online_on, online_goal, online_mode, central_source, online_source)
        return win


class GoalSourcePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, "moisten_config.json")

    def _write(self, data):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def _read(self):
        with open(self.config_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _screen(self):
        return _FakeGoalScreen(self.config_path)

    def test_auto_source_survives_zero_numeric_goal(self):
        """自动识别模式下数值是 0，也必须记住「已启用 + 自动识别」。"""
        screen = self._screen()
        window = type("Win", (), {"_settings_mode": False,
                                  "next_screen": lambda self: None})()
        screen.window = lambda: window
        with patch("gui.CONFIG_PATH", self.config_path):
            gui.GoalScreen._on_done(screen, True, 0, "target", False, 0, "target",
                                    "required", "custom")
        cfg = self._read()
        self.assertEqual(cfg["central_source"], "required")
        self.assertIs(cfg["central_enabled"], True)
        self.assertIs(cfg["online_enabled"], False)

        loaded = self._screen()
        with patch("gui.CONFIG_PATH", self.config_path):
            loaded._load_goal()
        self.assertTrue(loaded._saved_central_on)
        self.assertEqual(loaded._saved_central_source, "required")
        self.assertFalse(loaded._saved_online_on)

    def test_custom_goal_and_source_round_trip(self):
        window = type("Win", (), {"_settings_mode": False,
                                  "next_screen": lambda self: None})()
        screen = self._screen()
        screen.window = lambda: window
        with patch("gui.CONFIG_PATH", self.config_path):
            gui.GoalScreen._on_done(screen, True, 120, "remain", True, 50, "target",
                                    "custom", "required")
        cfg = self._read()
        self.assertEqual(cfg["central_goal"], 120)
        self.assertEqual(cfg["central_mode"], "remain")
        self.assertEqual(cfg["central_source"], "custom")
        self.assertEqual(cfg["online_source"], "required")

        loaded = self._screen()
        with patch("gui.CONFIG_PATH", self.config_path):
            loaded._load_goal()
        self.assertEqual(loaded._saved_central, 120)
        self.assertEqual(loaded._saved_central_mode, "remain")
        self.assertEqual(loaded._saved_online_source, "required")
        self.assertTrue(loaded._saved_online_on)

    def test_legacy_config_without_new_keys_still_loads(self):
        self._write({"central_goal": 90, "online_goal": 50,
                     "central_mode": "target", "online_mode": "target"})
        loaded = self._screen()
        with patch("gui.CONFIG_PATH", self.config_path):
            loaded._load_goal()
        self.assertTrue(loaded._saved_central_on)
        self.assertTrue(loaded._saved_online_on)
        self.assertEqual(loaded._saved_central_source, "custom")
        self.assertEqual(loaded._saved_online_source, "custom")

    def test_disabled_card_clears_source(self):
        window = type("Win", (), {"_settings_mode": False,
                                  "next_screen": lambda self: None})()
        screen = self._screen()
        screen.window = lambda: window
        with patch("gui.CONFIG_PATH", self.config_path):
            gui.GoalScreen._on_done(screen, False, 0, "target", True, 0, "target",
                                    "required", "required")
        cfg = self._read()
        self.assertFalse(cfg["central_enabled"])
        self.assertEqual(cfg["central_source"], "custom")
        self.assertTrue(cfg["online_enabled"])
        self.assertEqual(cfg["online_source"], "required")


class MainWindowConfigTests(unittest.TestCase):
    def test_loads_source_flags_into_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "z.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"workers": 3, "central_source": "required",
                           "online_source": "custom", "central_goal": 0,
                           "online_goal": 50}, fh)

            class _Win:
                pass

            win = _Win()
            with patch("gui.CONFIG_PATH", path):
                gui.MainWindow._load_saved_config(win)
            self.assertEqual(win.cfg_central_source, "required")
            self.assertEqual(win.cfg_online_source, "custom")
            self.assertEqual(win.cfg_online_goal, 50)


class DependencyContractTests(unittest.TestCase):
    def test_study_hours_exposes_required_hours(self):
        """worker 依赖 _get_study_hours 返回应训时长字段。"""
        from main import resolve_study_hours
        result = resolve_study_hours(None, "应训时长\n每年应完成\n90学时\n每年应完成\n50学时\n"
                                           "今年已训\n242学时\n14.25学时\n完成进度\n")
        self.assertEqual((result["required_central"], result["required_online"]),
                         (90.0, 50.0))

    def test_missing_required_keys_default_to_zero(self):
        """学时获取失败时返回的字典没有应训时长字段，worker 要能安全取 0。"""
        failure = {"central": 0, "online": 0, "total": 0}
        self.assertEqual(failure.get("required_central", 0) or 0, 0)
        self.assertEqual(failure.get("required_online", 0) or 0, 0)


if __name__ == "__main__":
    unittest.main()
