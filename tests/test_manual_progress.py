"""手动模式（自定义 URL 学习）总进度测试。

问题：手动模式没有学时目标，仪表盘的总进度环/进度条被硬编码清零，学完整轮
都显示 0。现在按「已处理工作项 / 总工作项」算：课程 URL 路径按 URL 数，
专题班路径按课程数，成功/失败/需人工都算处理过。

覆盖三层：
  1. _manual_progress_payload：载荷形状与钳制；
  2. _learn_course_urls：真的按 URL 逐个推进（0/N → 1/N → … → N/N）；
  3. DashboardScreen 的渲染与 _on_hours 手动分支不再把进度打回 0。
"""
import asyncio
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402
from main import AutoLearner, _manual_progress_payload  # noqa: E402


class ManualProgressPayloadTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(_manual_progress_payload(3, 10),
                         {"manual_done": 3, "manual_total": 10})

    def test_status_included_when_given(self):
        payload = _manual_progress_payload(0, 0, "准备中")
        self.assertEqual(payload["manual_status"], "准备中")

    def test_negative_values_are_clamped(self):
        payload = _manual_progress_payload(-5, -1)
        self.assertEqual((payload["manual_done"], payload["manual_total"]), (0, 0))

    def test_no_wid_field(self):
        """不能带 wid：否则 GUI 会把它当成某个 worker 的行进度。"""
        self.assertNotIn("wid", _manual_progress_payload(1, 2))


class FakeUrlPage:
    def __init__(self):
        self.url = "about:blank"
        self.visited = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.visited.append(url)

    async def wait_for_timeout(self, ms):
        return None

    def locator(self, selector):
        return FakeLocator()


class FakeLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def click(self, **kwargs):
        return None


class CourseUrlProgressTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        # worker 依次启动的间隔对进度逻辑没意义，归零避免拖慢测试
        patcher = patch("main.WORKER_STAGGER_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _learner(self, workers, results):
        learner = AutoLearner.__new__(AutoLearner)
        learner.workers = workers
        learner.study_goal = 0
        learner._stop_event = threading.Event()
        learner.pages = [FakeUrlPage() for _ in range(workers)]
        calls = {"n": 0}

        async def fake_play(wp, wid, on_progress):
            on_progress(50)
            results.append(wid)
            calls["n"] += 1
            return True

        learner.find_and_play_video = fake_play
        return learner

    def _run(self, urls, workers=2):
        learner = self._learner(workers, [])
        events = []
        asyncio.run(learner._learn_course_urls(
            urls, workers, lambda m, s="": None, events.append, lambda d: None))
        manual = [e for e in events if "manual_total" in e]
        return manual

    def test_reports_zero_then_each_item(self):
        manual = self._run(["https://a/1", "https://a/2", "https://a/3"], workers=2)
        totals = {e["manual_total"] for e in manual}
        self.assertEqual(totals, {3})                     # 总数一开始就确定
        self.assertEqual(manual[0]["manual_done"], 0)     # 先给 0/N，界面立刻有反馈
        self.assertEqual(manual[0].get("manual_status"), "准备中")
        done = [e["manual_done"] for e in manual]
        self.assertEqual(max(done), 3)                    # 全部处理完推进到 N/N
        self.assertEqual(done, sorted(done))              # 单调不回退

    def test_single_url_progress_reaches_full(self):
        manual = self._run(["https://a/only"], workers=1)
        self.assertEqual(manual[-1]["manual_done"], 1)
        self.assertEqual(manual[-1]["manual_total"], 1)

    def test_stopped_run_reports_partial_with_status(self):
        learner = self._learner(1, [])
        order = []

        async def fake_play(wp, wid, on_progress):
            order.append(1)
            on_progress(10)
            learner._stop_event.set()      # 第一项做完就停
            return False

        learner.find_and_play_video = fake_play
        events = []
        asyncio.run(learner._learn_course_urls(
            ["https://a/1", "https://a/2"], 1, lambda m, s="": None,
            events.append, lambda d: None))
        manual = [e for e in events if "manual_total" in e]
        self.assertEqual(manual[-1]["manual_done"], 1)
        self.assertEqual(manual[-1]["manual_total"], 2)
        self.assertEqual(manual[-1].get("manual_status"), "已停止")

    def test_zero_tasks_reports_preparing(self):
        manual = self._run([], workers=1)
        self.assertEqual(manual[0]["manual_total"], 0)
        self.assertEqual(manual[0]["manual_done"], 0)


class _FakeLabel:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


class _FakeBar:
    def __init__(self):
        self.value = 0

    def setValue(self, value):
        self.value = int(value)

    def value_(self):
        return self.value


class _FakeDash:
    """只带手动进度渲染所需状态；用真实方法实现。"""

    _apply_manual_progress = gui.DashboardScreen._apply_manual_progress
    _render_manual_progress = gui.DashboardScreen._render_manual_progress
    _on_progress = gui.DashboardScreen._on_progress
    _on_hours = gui.DashboardScreen._on_hours

    def __init__(self, mode="manual"):
        self._manual_done = 0
        self._manual_total = 0
        self._manual_status = ""
        self.progress_ring = _FakeBar()
        self.goal_progress = _FakeBar()
        self.current_progress = _FakeBar()
        self.lbl_goal_info = _FakeLabel()
        self.lbl_eta = _FakeLabel()
        self.lbl_central = _FakeLabel()
        self.lbl_online = _FakeLabel()
        self.lbl_updated = _FakeLabel()
        self.lbl_session = _FakeLabel()
        self._session_start_total = None
        self._hours_history = []
        self.sparkline = SimpleNamespace(set_data=lambda pts: None)
        self._cfg_mode = mode
        self.ring_anims = []
        self.bar_anims = []
        self.table = SimpleNamespace(rowCount=lambda: 2)
        self._progress_bars = []
        self._progress_labels = []
        self._worker_display_states = {}
        self.lbl_session_state = _FakeLabel()
        self.table_set = []
        self.rows_touched = 0

    # 桩：把动画记下来，直接看目标值
    def _animate_ring(self, target):
        self.ring_anims.append(int(target))
        self.progress_ring.setValue(target)

    def _animate_progress_bar(self, bar, target, duration=280):
        self.bar_anims.append(int(target))
        bar.setValue(target)

    def _manual_goal_text(self):
        return "手动模式 · 2 个URL"

    def window(self):
        return SimpleNamespace(cfg_mode=self._cfg_mode)

    def table_row_count(self):
        return 2


class ManualProgressRenderTests(unittest.TestCase):
    def test_ring_follows_done_over_total(self):
        dash = _FakeDash()
        dash._apply_manual_progress({"manual_done": 1, "manual_total": 4})
        self.assertEqual(dash.progress_ring.value, 25)
        self.assertEqual(dash.goal_progress.value, 25)
        self.assertIn("已完成 1/4 项", dash.lbl_goal_info.text)
        self.assertIn("25%", dash.lbl_goal_info.text)

    def test_completion_shows_full(self):
        dash = _FakeDash()
        dash._apply_manual_progress({"manual_done": 3, "manual_total": 3})
        self.assertEqual(dash.progress_ring.value, 100)
        self.assertIn("3/3", dash.lbl_goal_info.text)

    def test_done_is_clamped_to_total(self):
        dash = _FakeDash()
        dash._apply_manual_progress({"manual_done": 9, "manual_total": 4})
        self.assertEqual(dash._manual_done, 4)
        self.assertEqual(dash.progress_ring.value, 100)

    def test_unknown_total_keeps_zero_but_shows_text(self):
        dash = _FakeDash()
        dash._apply_manual_progress({"manual_done": 0, "manual_total": 0,
                                     "manual_status": "准备中"})
        self.assertEqual(dash.progress_ring.value, 0)
        self.assertIn("手动模式", dash.lbl_goal_info.text)
        self.assertIn("准备中", dash.lbl_goal_info.text)

    def test_progress_event_is_not_treated_as_worker_row(self):
        """带 manual_* 的载荷不能去写 worker 表格行。"""
        dash = _FakeDash()
        dash._apply_manual_progress({"manual_done": 2, "manual_total": 4})
        self.assertEqual(dash.progress_ring.value, 50)

    def test_hours_callback_does_not_reset_manual_progress(self):
        """回归：原来 _on_hours 的手动分支把进度硬清零，学完整轮都是 0。"""
        dash = _FakeDash()
        dash._apply_manual_progress({"manual_done": 3, "manual_total": 4})
        self.assertEqual(dash.progress_ring.value, 75)

        tokens = SimpleNamespace(accent="#111111", success="#222222")
        fake_app = SimpleNamespace(property=lambda key: tokens)
        with patch("gui.QApplication.instance", return_value=fake_app):
            dash._on_hours({"central": 1.0, "online": 2.0, "updated": "10:00"})
        self.assertEqual(dash.progress_ring.value, 75, "学时回调不该把手动进度打回 0")
        self.assertEqual(dash.goal_progress.value, 75)
        self.assertIn("3/4", dash.lbl_goal_info.text)

    def test_hours_callback_before_any_progress_event(self):
        dash = _FakeDash()
        tokens = SimpleNamespace(accent="#111111", success="#222222")
        fake_app = SimpleNamespace(property=lambda key: tokens)
        with patch("gui.QApplication.instance", return_value=fake_app):
            dash._on_hours({"central": 0, "online": 0, "updated": "10:00"})
        self.assertEqual(dash.progress_ring.value, 0)
        self.assertIn("手动模式", dash.lbl_goal_info.text)


class ManualProgressWiringTests(unittest.TestCase):
    def test_manual_mode_reports_item_progress_flag(self):
        """手动专题班路径要开 report_item_progress，否则总进度永远不动。"""
        import inspect
        source = inspect.getsource(AutoLearner.learn_from_urls)
        self.assertIn("report_item_progress=True", source)

    def test_parallel_learn_courses_accepts_flag(self):
        import inspect
        sig = inspect.signature(AutoLearner.parallel_learn_courses)
        self.assertIn("report_item_progress", sig.parameters)
        self.assertFalse(sig.parameters["report_item_progress"].default)

    def test_parallel_learn_courses_reports_items(self):
        import inspect
        source = inspect.getsource(AutoLearner.parallel_learn_courses)
        self.assertIn("_manual_progress_payload", source)
        self.assertGreaterEqual(source.count("report_items()"), 5,
                                "成功与各失败分支都要推进总进度")
        self.assertIn('report_items("准备中")', source,
                      "开工前要先报一次总数，界面才能显示 0/N")


if __name__ == "__main__":
    unittest.main()
