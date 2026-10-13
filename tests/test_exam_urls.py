"""自定义 URL 模式里的考试支持。

需求：自定义 URL 里粘贴
    https://u.ccb.com/ote/#/exampreview?examArrangeID=...&domainName=u.ccb.com
要能自动参加考试，逻辑与现有（训练营课程页里发现的）考试完全一致。

这里锁住：
  1. OTE 考试链接被单独识别成一类，不会掉进"未识别到可学习的链接"；
  2. 考试任务按链接构造（URL 原样保留、名字取自 examArrangeID）；
  3. 执行走的就是 `_solve_exam_tasks` —— 即训练营考试同一段逻辑；
  4. 未开启自动答题 / 没配 Key 时给出明确提示且不误报成功。
"""
import asyncio
import inspect
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import AutoLearner  # noqa: E402

EXAM_URL = ("https://u.ccb.com/ote/#/exampreview?"
            "examArrangeID=e67201b1-05eb-47af-9b67-f45b771e2b4f&domainName=u.ccb.com")


class ExamUrlTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.progress = []
        self.learner = AutoLearner.__new__(AutoLearner)
        self.learner.exam_enabled = True
        self.learner.deepseek_api_key = "sk-test"
        self.learner._stop_event = threading.Event()

    def _run(self, urls, stats=None):
        captured = {}

        async def fake_solve(_self, tasks, worker_id, _log):
            captured["tasks"] = tasks
            captured["worker_id"] = worker_id
            return stats or {"found": len(tasks), "passed": len(tasks), "failed": 0,
                             "skipped": 0, "errors": 0, "all_ok": True}

        with patch.object(AutoLearner, "_solve_exam_tasks", fake_solve):
            out = asyncio.run(self.learner._learn_exam_urls(
                urls,
                lambda m, s="": self.logs.append(str(m)),
                lambda d: self.progress.append(d)))
        return out, captured

    def test_exam_task_is_built_from_the_url(self):
        _out, captured = self._run([EXAM_URL])
        task = captured["tasks"][0]
        self.assertEqual(task["url"], EXAM_URL, "URL 必须原样使用")
        self.assertIn("e67201b1", task["name"], "名字取自 examArrangeID，日志可辨认")
        self.assertEqual(captured["worker_id"], 0)

    def test_enabled_exam_is_solved_and_reported(self):
        out, _captured = self._run([EXAM_URL])
        self.assertEqual(out["passed"], 1)
        self.assertTrue(any("开始考试" in m for m in self.logs), self.logs)
        # 逐场结果由共用的 _solve_exam_tasks 打印（这里被替换掉了），
        # 所以只校验进度载荷把通过/未通过如实报出来
        final = self.progress[-1]
        self.assertEqual(final["manual_total"], 1)
        self.assertEqual(final["manual_done"], 1)
        self.assertIn("通过1", final.get("manual_status", ""))

    def test_multiple_exam_urls_are_all_solved(self):
        second = EXAM_URL.replace("e67201b1", "aaaaaaaa")
        _out, captured = self._run([EXAM_URL, second])
        self.assertEqual(len(captured["tasks"]), 2)
        self.assertEqual(captured["tasks"][0]["url"], EXAM_URL)
        self.assertEqual(captured["tasks"][1]["url"], second)

    def test_disabled_auto_exam_reports_clearly(self):
        self.learner.exam_enabled = False
        _out, captured = self._run([EXAM_URL])
        self.assertNotIn("tasks", captured, "没开启就不该去动考试页")
        self.assertTrue(any("未开启考试自动答题" in m for m in self.logs), self.logs)
        self.assertIn("未开启", self.progress[-1].get("manual_status", ""))

    def test_missing_api_key_reports_clearly(self):
        self.learner.deepseek_api_key = ""
        _out, captured = self._run([EXAM_URL])
        self.assertNotIn("tasks", captured)
        self.assertTrue(any("DeepSeek API Key" in m for m in self.logs), self.logs)
        self.assertIn("API Key", self.progress[-1].get("manual_status", ""))

    def test_failed_exam_is_reported_as_not_passed(self):
        out, _captured = self._run([EXAM_URL], stats={
            "found": 1, "passed": 0, "failed": 1, "skipped": 0, "errors": 0,
            "all_ok": False})
        self.assertEqual(out["failed"], 1)
        self.assertIn("未通过1", self.progress[-1].get("manual_status", ""))


class ExamUrlClassificationTests(unittest.TestCase):
    """考试链接要在自定义 URL 分类里被单独认出来。"""

    def setUp(self):
        self.source = inspect.getsource(AutoLearner.learn_from_urls)

    def test_ote_links_are_classified_as_exams(self):
        self.assertIn("exam_urls", self.source)
        self.assertIn(r"/ote/|#/exampreview|#/userexam|#/shamexam", self.source)

    def test_exam_only_url_is_not_reported_as_unrecognised(self):
        # 只有考试链接时不能走"未从URL中提取到有效ID"那条分支
        self.assertIn("or exam_urls)", self.source)

    def test_exams_run_through_the_shared_runner(self):
        self.assertIn("_learn_exam_urls", self.source)
        runner = inspect.getsource(AutoLearner._learn_exam_urls)
        self.assertIn("_solve_exam_tasks", runner)

    def test_trainingcamp_exams_use_the_same_runner(self):
        camp = inspect.getsource(AutoLearner.solve_trainingcamp_exams)
        self.assertIn("_solve_exam_tasks", camp,
                      "两条路径必须共用同一段考试逻辑")


class PassedExamRetakePromptTests(unittest.TestCase):
    """任务里发现"已通过"的考试时：问用户跳过还是再考一次，无响应则跳过。"""

    def setUp(self):
        self.debugs = []
        patcher = patch("main.debug", side_effect=lambda m: self.debugs.append(str(m)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.learner = AutoLearner.__new__(AutoLearner)
        self.learner.exam_passed_retry_hook = None
        self.task = {"name": "某场考试", "url": EXAM_URL}
        self.log = lambda m, s="": self.logs.append(str(m))

    def _ask(self):
        return asyncio.run(self.learner._confirm_retake_passed_exam(
            self.task, "已通过 90 分", self.log))

    def test_no_hook_means_skip(self):
        """命令行/无界面时不能卡住：默认跳过。"""
        self.assertFalse(self._ask())

    def test_user_choosing_retake_returns_true(self):
        async def hook(name, detail):
            return True
        self.learner.exam_passed_retry_hook = hook
        self.assertTrue(self._ask())
        self.assertTrue(any("询问是否再考一次" in m for m in self.logs), self.logs)

    def test_user_choosing_skip_returns_false(self):
        async def hook(name, detail):
            return False
        self.learner.exam_passed_retry_hook = hook
        self.assertFalse(self._ask())

    def test_hook_exception_falls_back_to_skip(self):
        async def hook(name, detail):
            raise RuntimeError("dialog boom")
        self.learner.exam_passed_retry_hook = hook
        self.assertFalse(self._ask(), "询问失败要当作跳过，不能把考试卡死")
        self.assertTrue(any("再考确认失败" in m for m in self.debugs), self.debugs)

    def test_hook_receives_name_and_detail(self):
        seen = {}

        async def hook(name, detail):
            seen["name"] = name
            seen["detail"] = detail
            return False
        self.learner.exam_passed_retry_hook = hook
        self._ask()
        self.assertEqual(seen["name"], "某场考试")
        self.assertEqual(seen["detail"], "已通过 90 分")

    def test_cancellation_propagates(self):
        async def hook(name, detail):
            raise asyncio.CancelledError()
        self.learner.exam_passed_retry_hook = hook
        with self.assertRaises(asyncio.CancelledError):
            self._ask()

    def test_already_passed_exam_consults_the_prompt(self):
        """已通过分支必须先问，用户同意再考就继续往下走（不再直接跳过）。"""
        src = inspect.getsource(AutoLearner._solve_one_exam)
        self.assertIn("_confirm_retake_passed_exam", src)
        self.assertIn("按用户选择再考一次", src)
        # 「待批阅」仍然是直接跳过，不该多问一次
        self.assertIn("不再重考", src)


class ExamPassedDialogWiringTests(unittest.TestCase):
    """GUI 侧：10 秒倒计时、无响应按跳过处理。"""

    def setUp(self):
        try:
            import gui  # noqa: F401
            self.gui = gui
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"GUI 不可导入: {exc}")

    def test_timeout_is_ten_seconds(self):
        self.assertEqual(self.gui.EXAM_PASSED_RETRY_TIMEOUT, 10)

    def test_signal_exists_on_thread(self):
        src = inspect.getsource(self.gui.AsyncThread)
        self.assertIn("exam_passed_signal", src)

    def test_handler_is_connected(self):
        src = inspect.getsource(self.gui.DashboardScreen)
        self.assertIn("exam_passed_signal.connect(self._on_exam_passed_retry)", src)

    def test_hook_is_injected_with_timeout(self):
        src = inspect.getsource(self.gui.DashboardScreen._run_learning)
        self.assertIn("exam_passed_retry_hook", src)
        self.assertIn("EXAM_PASSED_RETRY_TIMEOUT", src)
        self.assertIn("跳过不考", src)

    def test_countdown_defaults_to_skip(self):
        src = inspect.getsource(self.gui.DashboardScreen._run_exam_choice_dialog)
        self.assertIn("dlg.done(0)", src, "倒计时结束必须按跳过")
        self.assertIn("timer.stop()", src)

    def test_passed_dialog_asks_skip_or_retake(self):
        src = inspect.getsource(self.gui.DashboardScreen._on_exam_passed_retry)
        self.assertIn("再考一次", src)
        self.assertIn("跳过", src)


if __name__ == "__main__":
    unittest.main()
