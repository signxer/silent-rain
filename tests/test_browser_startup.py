"""浏览器启动阶段的超时保护测试。

背景：更新重启后，第一个新版本实例卡在「使用系统 Chrome」不再往下走，
界面既没有报错也没有进展——因为没有超时，任何一步（启动浏览器 / 建上下文 /
开标签页）挂住都会无声无息地停在那里。

这里锁住三件事：
  1. 每一步都有单项超时，超时会给出明确文案并抛错（而不是永久挂起）；
  2. 正常返回时原样透传结果；
  3. 超时常量是正的、可配置。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from main import AutoLearner, BROWSER_STEP_TIMEOUT_SECONDS  # noqa: E402


class BrowserStepTimeoutTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)

    def _logs(self):
        self.messages = []
        return lambda msg, style="": self.messages.append(str(msg))

    def test_timeout_reports_and_raises(self):
        log = self._logs()

        async def hanging():
            await asyncio.sleep(30)

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(self.learner._browser_step(hanging(), "启动浏览器", log, seconds=0.05))
        self.assertIn("超时", str(ctx.exception))
        self.assertTrue(any("启动浏览器超时" in m for m in self.messages), self.messages)

    def test_result_is_passed_through(self):
        async def quick():
            return {"ok": True}

        self.assertEqual(
            asyncio.run(self.learner._browser_step(quick(), "创建浏览器上下文", self._logs())),
            {"ok": True})

    def test_exception_from_step_is_not_swallowed(self):
        async def boom():
            raise ValueError("chrome not found")

        with self.assertRaises(ValueError):
            asyncio.run(self.learner._browser_step(boom(), "启动浏览器", self._logs()))

    def test_default_budget_is_positive(self):
        self.assertGreater(BROWSER_STEP_TIMEOUT_SECONDS, 0)

    def test_init_uses_timeouts_for_every_step(self):
        """init() 的三步都要走 _browser_step，否则某一环又会无声挂起。"""
        import inspect
        source = inspect.getsource(AutoLearner.init)
        self.assertGreaterEqual(source.count("_browser_step("), 4,
                                "启动浏览器/内置 Chromium/建上下文/开标签页都要加超时")
        self.assertIn("new_context", source)
        self.assertIn("new_page", source)


class DebugLogLocationTests(unittest.TestCase):
    """打包版日志要写到固定用户目录：CWD 变化后相对路径会静默写不进去。"""

    def test_init_records_absolute_log_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.log")
            with patch("main.DEBUG_LOG", path):
                main.init_debug_log("2.4.4")
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        self.assertIn(os.path.abspath(path), content)
        self.assertIn("Moisten Debug Run v2.4.4", content)


if __name__ == "__main__":
    unittest.main()
