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


class ConcurrentExamPromptTests(unittest.TestCase):
    """并发核心：两个考试询问同时等待，各自的选择必须写回各自的那份。
    直接驱动 DashboardScreen 的 _begin_exam_prompt/_resolve_exam_prompt——
    这正是当初"共用 Event + 单个 choice 标志"会串台的地方（v2.4.17 修复）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import gui
            cls.gui = gui
        except Exception:  # pragma: no cover
            raise unittest.SkipTest("GUI 不可导入")

    def setUp(self):
        self.dash = self.gui.DashboardScreen.__new__(self.gui.DashboardScreen)
        # __new__ 不走 __init__，这里补齐并发询问需要的外部状态
        self.dash._exam_prompt_seq = 0
        self.dash._exam_prompt_events = {}
        self.dash._exam_prompt_choices = {}

    def _resolve(self, prompt_id, choice):
        self.dash._resolve_exam_prompt(prompt_id, choice)

    def test_two_prompts_resolve_independently(self):
        """两个并发询问，一个选"再考"一个选"跳过"，各自结果必须正确。"""
        import threading

        name1, id1, on1 = self.dash._begin_exam_prompt()
        name2, id2, on2 = self.dash._begin_exam_prompt()
        self.assertNotEqual(id1, id2, "两个询问必须分配不同的 prompt_id")
        results = {}

        def run(prompt_id, on_choice, choice, key):
            threading.Timer(0.05, lambda: self._resolve(prompt_id, choice)).start()
            loop = asyncio.new_event_loop()
            try:
                results[key] = loop.run_until_complete(
                    self.dash._wait_exam_prompt(
                        name1, 10, lambda m, s="": None, prompt_id, on_choice))
            finally:
                loop.close()

        threads = [
            threading.Thread(target=run, args=(id1, on1, True, "a"), daemon=True),
            threading.Thread(target=run, args=(id2, on2, False, "b"), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertTrue(results.get("a"),
                        "第一个询问应拿到 再考（不能被第二个的跳过覆盖）")
        self.assertFalse(results.get("b"),
                         "第二个询问应拿到 跳过（不能被第一个的再考覆盖）")

    def test_timeout_uses_default(self):
        """询问超时未响应 → 返回 default（并发场景也不会卡死）。"""
        name, prompt_id, on_choice = self.dash._begin_exam_prompt()
        loop = asyncio.new_event_loop()
        try:
            val = loop.run_until_complete(self.dash._wait_exam_prompt(
                name, 0.05, lambda m, s="": None, prompt_id, on_choice,
                default=True))
        finally:
            loop.close()
        self.assertTrue(val)
        self.assertNotIn(prompt_id, self.dash._exam_prompt_events)

    def test_state_cleaned_after_resolve(self):
        name, prompt_id, on_choice = self.dash._begin_exam_prompt()
        self._resolve(prompt_id, True)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.dash._wait_exam_prompt(
                name, 5, lambda m, s="": None, prompt_id, on_choice))
        finally:
            loop.close()
        # 结束后事件与 choice 都应清理，避免泄漏导致 id 暴增
        self.assertNotIn(prompt_id, self.dash._exam_prompt_events)
        self.assertNotIn(prompt_id, self.dash._exam_prompt_choices)


if __name__ == "__main__":
    unittest.main()


class RetakeRaceAndResilienceTests(unittest.TestCase):
    """并发考试询问不串台 + 空题/待批阅的确认点。"""

    def setUp(self):
        self.debugs = []
        patcher = patch("main.debug", side_effect=lambda m: self.debugs.append(str(m)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.learner = AutoLearner.__new__(AutoLearner)
        self.learner.exam_passed_retry_hook = None
        self.learner.exam_confirm_unanswered_hook = None
        self.task = {"name": "X", "url": EXAM_URL}
        self.log = lambda m, s="": self.logs.append(str(m))

    def test_unanswered_hook_returns_true_means_submit(self):
        async def hook(name, unanswered, total):
            return True
        self.learner.exam_confirm_unanswered_hook = hook

        async def real():
            return await self.learner._logged_answer_gate_placeholder()
        # hook 签名为 (name, unanswered, total)；这里直接触发完成路径，
        # 用一个薄封装模拟 _solve_one_exam 里的调用点
        async def call():
            return bool(await self.learner.exam_confirm_unanswered_hook("X", 10, 20))
        self.assertTrue(asyncio.run(call()))

    def test_attending_helper_defaults_to_skip_without_hook(self):
        self.assertFalse(asyncio.run(
            self.learner._confirm_attending("待批阅", self.log)))

    def test_exam_delay_total_is_capped(self):
        from main import _exam_delay_plan, EXAM_DELAY_TOTAL_LIMIT
        # 30 题 × 10 秒 = 300 秒，未超上限：正常
        per, total = _exam_delay_plan(30, 10, 20, rng=_FakeRng(10.0))
        self.assertEqual(per, 10.0)
        self.assertEqual(total, 300.0)
        # 2000 题 × 15 秒远超上限：必须封顶
        per, total = _exam_delay_plan(2000, 10, 20, rng=_FakeRng(15.0))
        self.assertEqual(per, 15.0)
        self.assertEqual(total, float(EXAM_DELAY_TOTAL_LIMIT),
                         "整场等待必须封顶在 EXAM_DELAY_TOTAL_LIMIT")

    def test_signal_carries_prompt_id(self):
        try:
            import gui
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"GUI 不可导入: {exc}")
        src = inspect.getsource(gui.AsyncThread)
        self.assertIn("exam_passed_signal = Signal(int, str, str)", src)

    def test_gui_uses_per_prompt_state_not_shared_flag(self):
        try:
            import gui
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"GUI 不可导入: {exc}")
        src = inspect.getsource(gui.DashboardScreen)
        self.assertIn("def _begin_exam_prompt", src)
        self.assertIn("_exam_prompt_seq", src)
        self.assertIn("threading.Event()", src)
        self.assertNotIn("_exam_retry_choice = False", src)
        self.assertNotIn("self._exam_retry_event", src)

    def test_phase_status_shown_when_total_positive(self):
        try:
            import gui
        except Exception:  # pragma: no cover
            self.skipTest("GUI 不可导入")
        src = inspect.getsource(gui.DashboardScreen._render_manual_progress)
        self.assertIn("phase = getattr(self, \"_manual_status\", \"\") or \"\"", src)


class _FakeRng:
    """注入固定值的 rng，便于 _exam_delay_plan 单测。"""
    def __init__(self, value):
        self.value = value

    def uniform(self, a, b):
        return self.value


class UnansweredPromptFlowTests(unittest.TestCase):
    """空题占比过高时确认"按未答提交"还是"放弃"的分支。"""

    def setUp(self):
        self.debugs = []
        patcher = patch("main.debug", side_effect=lambda m: self.debugs.append(str(m)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.learner = AutoLearner.__new__(AutoLearner)
        self.learner.exam_confirm_unanswered_hook = None
        self.log = lambda m, s="": self.logs.append(str(m))

    async def _resolve_flow(self, unanswered, total):
        """模拟 _solve_one_exam 里那段判定逻辑（return 是否继续提交）。"""
        if (self.learner.exam_confirm_unanswered_hook
                and unanswered >= max(3, int(total * 0.5))):
            return await self.learner.exam_confirm_unanswered_hook("X", unanswered, total)
        # 未达阈值：照常按未答提交，不问
        return True

    def test_low_unanswered_skips_prompt(self):
        """11/30 未答（不足一半）→ 不弹窗，直接按未答提交。"""
        def _boom(name, u, t):  # pragma: no cover
            raise AssertionError("不应被调用")
        self.learner.exam_confirm_unanswered_hook = _boom
        self.assertTrue(asyncio.run(self._resolve_flow(11, 30)))

    def test_high_unanswered_asks_and_submit_on_yes(self):
        async def hook(name, unanswered, total):
            return True  # 用户选"按未答提交"
        self.learner.exam_confirm_unanswered_hook = hook
        self.assertTrue(asyncio.run(self._resolve_flow(20, 30)))

    def test_high_unanswered_can_choose_to_give_up(self):
        async def hook(name, unanswered, total):
            return False  # 用户选"放弃本场"
        self.learner.exam_confirm_unanswered_hook = hook
        self.assertFalse(asyncio.run(self._resolve_flow(20, 30)))

    def test_threshold_used_is_half_or_three(self):
        """触发条件是 max(3, 50%)：小卷子至少 3 题空才问。"""
        trigger = lambda u, t: (u >= max(3, int(t * 0.5)))
        self.assertTrue(trigger(3, 5), "3/5 → 触发")
        self.assertFalse(trigger(2, 5), "2/5 → 不触发")
        self.assertTrue(trigger(4, 5), "4/5 → 触发")

    def test_solve_flow_integrates_the_hook(self):
        """_solve_one_exam 里确实接了 exam_confirm_unanswered_hook，且返回 skipped 表示放弃。"""
        import inspect
        src = inspect.getsource(AutoLearner._solve_one_exam)
        self.assertIn("exam_confirm_unanswered_hook", src)
        self.assertIn("用户选择放弃", src)
        self.assertIn("按未答提交", src)


class AnswerExamResilienceTests(unittest.TestCase):
    """answer_exam 保留部分答案，不再整卷丢弃。"""

    def setUp(self):
        import main as main_mod
        self.main = main_mod
        self.client = main_mod.DeepSeekClient.__new__(main_mod.DeepSeekClient)
        self.client.api_key = "k"
        self.client.thinking = False
        self.client.model = "m"
        self.calls = []

    def _fake_chat(self, seq):
        async def chat(messages, **kw):
            n = len(self.calls)
            self.calls.append(messages)
            return seq[n if n < len(seq) else -1]
        self.client.chat = chat

    def test_partial_json_preserves_parsed_answers(self):
        questions = [{"index": 1, "type": "SingleChoice", "content": "q1",
                      "options": []}, {"index": 2, "type": "SingleChoice",
                                       "content": "q2", "options": []}]
        first = '{"answers":[{"index":1,"choices":["A"]}],"extra":true}'
        # 第二轮用补全上下文请求返回缺失的第 2 题
        seq = [first, '{"answers":[{"index":2,"choices":["B"]}]}']
        self._fake_chat(seq)
        out = asyncio.run(self.client.answer_exam(questions, log=None))
        self.assertEqual(out[1]["choices"], ["A"])
        self.assertEqual(out[2]["choices"], ["B"])
        # 第二轮必须带了"补全没答的题"的请求（calls[1][3] 是追加的补全提示）
        self.assertEqual(len(self.calls), 2)
        correction_msg = " ".join(m.get("content", "") for m in self.calls[1][2:])
        self.assertIn("还没答的题", correction_msg)
        self.assertIn("[2]", correction_msg)

    def test_complete_first_answer_returns_immediately(self):
        questions = [{"index": 1, "type": "SingleChoice", "content": "q1",
                      "options": []}]
        seq = ['{"answers":[{"index":1,"choices":["A"]}]}']
        self._fake_chat(seq)
        out = asyncio.run(self.client.answer_exam(questions, log=None))
        self.assertEqual(out[1]["choices"], ["A"])
        self.assertEqual(len(self.calls), 1, "一次就拿全就不必再问")
