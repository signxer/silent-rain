"""专题班课程采集：报名环节 + 表格解析 + 可学判断。

背景（用户实测）：频道里的专题班
  1. 必须先报名，否则拿不到完整课程；报名后详情地址会变，要重新进详情页；
  2. 报名前也已经有课程列表，但那张表只有「类型 / 标题 / 必·选修」三列 ——
     原解析器要求 ≥4 个单元格并按固定下标取列，于是整张表被丢掉，报「未获取到课程」；
  3. 三列表没有「操作」列，原来的可学判断（action 为空即不可学）会把所有行过滤光。
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    AutoLearner,
    WORKSHOP_COURSE_TABLE_JS,
    _progress_completed,
)


class _Btn:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def _hit(self):
        return bool(self.page.keyword) and self.page.keyword in self.selector

    async def count(self):
        return 1 if self._hit() else 0

    async def is_visible(self, timeout=None):
        return self._hit() and not self.page.enrolled

    async def click(self, timeout=None):
        self.page.clicks += 1
        # 报名成功后地址会变：下一个 tick 生效
        self.page.pending_url_change = True


class _Body:
    def __init__(self, page):
        self.page = page

    async def inner_text(self, timeout=None):
        return self.page.body_text


class FakeEnrollPage:
    def __init__(self, keyword="", enrolled=False, url="https://u.ccb.com/workshop/#/detail?id=x"):
        self.keyword = keyword
        self.enrolled = enrolled
        self.url = url
        self.body_text = ""
        self.clicks = 0
        self.timeouts = 0
        self.pending_url_change = False

    def locator(self, selector):
        if selector == "body":
            return _Body(self)
        return _Btn(self, selector)

    async def wait_for_timeout(self, ms):
        self.timeouts += 1
        if self.pending_url_change:
            self.pending_url_change = False
            self.enrolled = True
            self.url = "https://u.ccb.com/workshop/#/myworkshop/detail?id=x"

    async def wait_for_load_state(self, state=None, timeout=None):
        return None


class WorkshopEnrollTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)
        self.logs = []
        self.log = lambda msg, style="": self.logs.append(str(msg))

    def test_clicks_enroll_button_and_reports_url_change(self):
        page = FakeEnrollPage(keyword="立即报名")
        before = page.url
        moved = asyncio.run(
            self.learner._enroll_workshop_if_needed(page, before, self.log))
        self.assertTrue(moved)
        self.assertEqual(page.clicks, 1)
        self.assertNotEqual(page.url, before, "报名后地址应当变化")
        self.assertTrue(any("需要报名" in m for m in self.logs), self.logs)

    def test_no_enroll_button_is_a_noop(self):
        page = FakeEnrollPage(keyword="")
        self.assertFalse(asyncio.run(
            self.learner._enroll_workshop_if_needed(page, page.url, self.log)))
        self.assertEqual(page.clicks, 0)
        self.assertEqual(page.timeouts, 0, "没按钮就不该空等")

    def test_already_enrolled_is_skipped(self):
        page = FakeEnrollPage(keyword="立即报名", enrolled=True)
        self.assertFalse(asyncio.run(
            self.learner._enroll_workshop_if_needed(page, page.url, self.log)))
        self.assertEqual(page.clicks, 0)

    def test_manual_flow_enrolls_before_reading_courses(self):
        """手动模式的专题班流程必须包含报名环节，并在报名后重新进详情页。"""
        import inspect
        source = inspect.getsource(AutoLearner.learn_from_urls)
        self.assertIn("_enroll_workshop_if_needed", source)
        self.assertLess(source.index("_enroll_workshop_if_needed"),
                        source.index("get_courses_from_workshop"),
                        "报名要在读课程列表之前")


class LearnableTests(unittest.TestCase):
    def test_no_action_column_falls_back_to_progress(self):
        """三列表没有操作列：没有进度就算待学，100% 才算学完。"""
        self.assertTrue(AutoLearner._is_learnable("", "", ""))
        self.assertTrue(AutoLearner._is_learnable("", "", "-"))
        self.assertFalse(AutoLearner._is_learnable("", "", "100%"))
        self.assertFalse(AutoLearner._is_learnable("", "", "100"))

    def test_explicit_actions_still_win(self):
        self.assertFalse(AutoLearner._is_learnable("立即回看", "2"))
        self.assertFalse(AutoLearner._is_learnable("已完成", "2"))
        self.assertTrue(AutoLearner._is_learnable("立即学习", "2"))
        self.assertTrue(AutoLearner._is_learnable("继续学习", "2"))

    def test_zero_hours_still_skipped(self):
        self.assertFalse(AutoLearner._is_learnable("立即学习", "0"))

    def test_progress_completed_helper(self):
        self.assertTrue(_progress_completed("100%"))
        self.assertTrue(_progress_completed("100"))
        self.assertFalse(_progress_completed("99.9%"))
        self.assertFalse(_progress_completed(""))
        self.assertFalse(_progress_completed(None))


THREE_COLUMN_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<table class="table courseList-table">
  <tr class="header">
    <th width="300" class="text-left">类型</th>
    <th class="text-left">标题</th>
    <th width="300">必/选修</th>
  </tr>
  <tbody class="content">
    <tr><td><span class="course-type">图文</span></td>
        <td><a href="#/course/detail?id=c1">《习近平新时代中国特色社会主义思想的世界观和方法论》</a></td>
        <td>必修</td></tr>
    <tr><td><span class="course-type">视频</span></td>
        <td><a href="#/course/detail?id=c2">《习近平关于中国式现代化论述》</a></td>
        <td>选修</td></tr>
  </tbody>
</table></body></html>"""

SIX_COLUMN_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<table class="table courseList-table">
  <tr class="header"><th width="300" class="text-left">类型</th><th class="text-left">标题</th>
    <th width="300">必/选修</th><th>学时</th><th>进度</th><th>操作</th></tr>
  <tbody class="content">
    <tr class="text-center"><td><span class="course-type">视频</span></td>
        <td><a href="#/course/play/1">某门课程</a></td><td>必修</td><td>2</td>
        <td><span class="percent-text">40%</span></td>
        <td><span class="edit-block">继续学习</span></td></tr>
    <tr class="text-center"><td><span class="course-type">视频</span></td>
        <td><a href="#/course/play/2">已学完课程</a></td><td>选修</td><td>1</td>
        <td><span class="percent-text">100%</span></td>
        <td><span class="edit-block">立即回看</span></td></tr>
  </tbody>
</table></body></html>"""


class CourseTableParseBrowserTests(unittest.TestCase):
    """在真实 Chromium 里跑解析脚本：三列表（无操作列）必须能读出来。"""

    def _run(self, html):
        try:
            from playwright.async_api import async_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright 未安装")

        async def go():
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as exc:  # pragma: no cover
                    return f"skip:{exc}"
                try:
                    page = await browser.new_page()
                    await page.set_content(html)
                    return await page.evaluate(WORKSHOP_COURSE_TABLE_JS)
                finally:
                    await browser.close()

        out = asyncio.run(go())
        if isinstance(out, str):  # pragma: no cover
            self.skipTest(out)
        return out

    def test_three_column_table_is_parsed(self):
        """回归：三列表原来因为 cells<4 被整表丢掉，报「未获取到课程」。"""
        data = self._run(THREE_COLUMN_HTML)
        self.assertEqual(data["headers"], ["类型", "标题", "必/选修"])
        rows = data["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["type"], "图文")
        self.assertIn("世界观和方法论", rows[0]["title"])
        self.assertEqual(rows[0]["required"], "必修")
        self.assertEqual(rows[0]["action"], "")      # 没有操作列
        self.assertEqual(rows[0]["hours"], "")
        self.assertIn("id=c1", rows[0]["url"])
        # 这些行在旧逻辑下会被 _is_learnable 全部过滤掉
        self.assertTrue(AutoLearner._is_learnable(
            rows[0]["action"], rows[0]["hours"], rows[0]["progress"]))

    def test_six_column_table_still_parsed(self):
        data = self._run(SIX_COLUMN_HTML)
        rows = data["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["action"], "继续学习")
        self.assertEqual(rows[0]["progress"], "40%")
        self.assertEqual(rows[0]["hours"], "2")
        self.assertEqual(rows[1]["action"], "立即回看")
        self.assertFalse(AutoLearner._is_learnable(
            rows[1]["action"], rows[1]["hours"], rows[1]["progress"]))


class _FakePage:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class ManualPipelineTests(unittest.TestCase):
    """拿到课程就开始学，其余专题班边学边报名（不要再干等所有报名）。"""

    def setUp(self):
        import inspect
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = inspect.getsource(AutoLearner.learn_from_urls)

    def test_learning_starts_before_every_workshop_is_collected(self):
        self.assertIn("fetch_more_courses", self.source)
        self.assertIn("total_ref=total_counter", self.source)
        # 先采一批（seed）再调用学习，而不是采完 24 个才调用
        self.assertLess(self.source.index("while pending and not all_tasks"),
                        self.source.index("parallel_learn_courses"))

    def test_remaining_workshops_are_handled_by_the_fetch_callback(self):
        body = self.source[self.source.index("async def fetch_more_courses"):]
        body = body[:body.index("\n            _log(")]
        self.assertIn("pending.pop(0)", body)
        self.assertIn("collect_one(", body)
        self.assertIn("queue.put_nowait", body)

    def test_enrollment_lives_inside_collect_one(self):
        body = self.source[self.source.index("async def collect_one"):]
        self.assertIn("_enroll_workshop_if_needed", body)

    def test_total_grows_as_more_workshops_are_collected(self):
        self.assertIn("total_counter[0] += len(tasks)", self.source)

    def test_concurrent_workers_do_not_collect_at_the_same_time(self):
        self.assertIn("collect_lock = asyncio.Lock()", self.source)
        self.assertIn("async with collect_lock", self.source)

    def test_idle_worker_reuses_work_added_by_others(self):
        """等锁期间别人补了货，要返回"有活了"，别让 worker 提前退出。"""
        self.assertIn("return queue.qsize()", self.source)

    def test_collection_page_is_closed_afterwards(self):
        self.assertIn("_close_collection_page", self.source)
        self.assertIn("finally", self.source)


class CollectionPageTests(unittest.TestCase):
    """采集专用页：worker 占用 pages[0..N-1]，采集不能共用同一页。"""

    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)

    def test_creates_a_dedicated_page(self):
        created = []

        class Ctx:
            async def new_page(self):
                page = _FakePage()
                created.append(page)
                return page

        self.learner.context = Ctx()
        page = asyncio.run(self.learner._new_collection_page("fallback"))
        self.assertIs(page, created[0])

    def test_falls_back_when_context_is_missing(self):
        self.learner.context = None
        self.assertEqual(
            asyncio.run(self.learner._new_collection_page("fallback")), "fallback")

    def test_falls_back_when_new_page_fails(self):
        class Ctx:
            async def new_page(self):
                raise RuntimeError("target closed")

        self.learner.context = Ctx()
        self.assertEqual(
            asyncio.run(self.learner._new_collection_page("fallback")), "fallback")

    def test_dedicated_page_is_closed_but_fallback_is_not(self):
        page = _FakePage()
        asyncio.run(self.learner._close_collection_page("fallback", page))
        self.assertTrue(page.closed)

        fallback = _FakePage()
        asyncio.run(self.learner._close_collection_page(fallback, fallback))
        self.assertFalse(fallback.closed, "降级共用时不能把主页面关掉")


if __name__ == "__main__":
    unittest.main()
