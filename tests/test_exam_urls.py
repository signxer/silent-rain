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


if __name__ == "__main__":
    unittest.main()
