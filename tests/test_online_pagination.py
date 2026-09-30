"""网络自学课程列表翻页测试。

覆盖「翻页不稳定」的两个根因：
  1. 点击「下一页」后只固定 sleep，不看页面是否真的换页 —— SPA 还没渲染完就
     当成翻页成功，随后采集到上一页的旧 DOM，并把逻辑页码带偏；
  2. hash 路由先变、卡片后渲染，若拿路由当成功依据，同样会采到旧 DOM。
所以翻页必须按「卡片内容/分页器高亮」校验，并且只有确认翻页了才前进页码。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    AutoLearner,
    ONLINE_COURSE_LIST_CARD_SELECTOR,
    ONLINE_PAGE_CHANGE_TIMEOUT_MS,
    ONLINE_PAGE_DEEP_LINK_TIMEOUT_MS,
    ONLINE_PAGE_ROUTE_TIMEOUT_MS,
    ONLINE_PAGE_SETTLE_DELAY_MS,
)

LIST_URL = "https://example.test/course/#/list/1"


class FakeNextControl:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return 1 if self.page.has_next_control else 0

    def filter(self, **kwargs):
        return self

    def nth(self, index):
        return self

    async def is_visible(self):
        return True

    async def inner_text(self):
        return "下一页"

    async def get_attribute(self, name):
        if name == "class":
            return "page-next page_disabled" if self.page.next_disabled else "page-next"
        return None

    async def click(self, timeout=None):
        self.page.handle_next_click()


class EmptyLocator:
    def filter(self, **kwargs):
        return self

    async def count(self):
        return 0


class FakePagerPage:
    """模型化 /course/#/list/N：hash 立即变、卡片延迟渲染。"""

    def __init__(self, total_pages=5, start_page=1, render_ticks=3,
                 goto_ticks=1, has_next_control=True, disabled_on_last=True,
                 swallow_clicks=0, clamp_deep_link=True):
        self.total_pages = total_pages
        self.dom_page = start_page            # DOM 里真正渲染的页码
        self.requested_page = start_page
        self.url = self._list_url(start_page)
        self.render_ticks = render_ticks
        self.goto_ticks = goto_ticks      # goto 后路由在同一次渲染里就解析完
        self.has_next_control = has_next_control
        self.disabled_on_last = disabled_on_last
        self.swallow_clicks = swallow_clicks
        self.clamp_deep_link = clamp_deep_link
        self.clicks = 0
        self.gotos = 0
        self.timeouts = 0
        self._queue = []

    @staticmethod
    def _list_url(page):
        return f"https://example.test/course/#/list/{page}"

    @property
    def next_disabled(self):
        return self.disabled_on_last and self.dom_page >= self.total_pages

    def cards(self, page):
        return [(f"课程{page}-{i}", f"https://example.test/course/#/detail/{page}-{i}")
                for i in range(3)]

    # ── 页面行为 ──
    def _schedule(self, ticks, action):
        self._queue.append((ticks, action))

    def _tick(self):
        due = [item for item in self._queue if item[0] <= 1]
        self._queue = [(ticks - 1, action) for ticks, action in self._queue if ticks > 1]
        for _ticks, action in due:
            action()

    def _clamp(self, page):
        if self.clamp_deep_link and page > self.total_pages:
            return self.total_pages
        return page

    def _apply_page(self, page):
        self.dom_page = self._clamp(page)
        # SPA 越界时会把路由 replace 回最后一页
        self.url = self._list_url(self.dom_page)

    def handle_next_click(self):
        self.clicks += 1
        if self.clicks <= self.swallow_clicks:
            return                      # 点击被吞掉：路由和 DOM 都不动
        target = self._clamp(self.dom_page + 1)
        # 路由先变（真实 SPA 的 router.push），卡片稍后才渲染
        self.url = self._list_url(target)
        self._schedule(self.render_ticks, lambda: self._apply_page(self.dom_page + 1))

    async def goto(self, url, **kwargs):
        self.gotos += 1
        requested = int(url.rsplit("/", 1)[-1])
        self.requested_page = requested
        if url == self.url:
            return                      # 同一 hash 不派发 hashchange，SPA 不重渲染
        self.url = url
        self._schedule(self.goto_ticks, lambda: self._apply_page(requested))

    async def wait_for_timeout(self, milliseconds):
        self.timeouts += 1
        self._tick()
        await asyncio.sleep(0.002)

    async def wait_for_selector(self, selector, **kwargs):
        if selector == ONLINE_COURSE_LIST_CARD_SELECTOR:
            return object()
        raise TimeoutError(selector)

    def is_closed(self):
        return False

    def locator(self, selector):
        if "[class*=page-next]" in selector:
            return FakeNextControl(self)
        return EmptyLocator()

    async def evaluate(self, script, selector=None):
        parts = [f"{title}\u0001{href}" for title, href in self.cards(self.dom_page)]
        route = int(self.url.rsplit("/", 1)[-1])
        return {
            "raw": "\u0002".join(parts),
            "set": "\u0002".join(sorted(parts)),
            "pager": self.dom_page,
            "route": route,
        }


class OnlinePaginationTests(unittest.TestCase):
    def setUp(self):
        # 把等待预算压到毫秒级，保持测试快
        for name, value in (("ONLINE_PAGE_CHANGE_TIMEOUT_MS", 400),
                            ("ONLINE_PAGE_ROUTE_TIMEOUT_MS", 400),
                            ("ONLINE_PAGE_SETTLE_DELAY_MS", 5),
                            ("ONLINE_PAGE_DEEP_LINK_TIMEOUT_MS", 400)):
            patcher = patch(f"main.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)

    def advance(self, page, current_page):
        return asyncio.run(self.learner._advance_online_course_page_state(
            page, LIST_URL, current_page))

    # ── 点击路径 ──
    def test_click_waits_for_render_and_returns_observed_page(self):
        page = FakePagerPage(total_pages=5, start_page=1, render_ticks=4)
        result = self.advance(page, 1)
        self.assertIs(result["moved"], True)
        self.assertEqual(result["page"], 2)
        # 关键：返回时 DOM 必须已经渲染到第 2 页，而不是「路由到了但卡片还是旧的」
        self.assertEqual(page.dom_page, 2)
        self.assertEqual(page.clicks, 1)
        self.assertGreaterEqual(page.timeouts, 4)

    def test_route_change_alone_is_not_treated_as_paging(self):
        """hash 先变、卡片没变时不能判定翻页成功。"""
        page = FakePagerPage(total_pages=5, start_page=1, render_ticks=10 ** 6)
        result = self.advance(page, 1)
        self.assertIsNot(result["moved"], True)
        self.assertEqual(page.dom_page, 1)

    def test_swallowed_click_is_recovered(self):
        page = FakePagerPage(total_pages=5, start_page=1, swallow_clicks=1,
                             render_ticks=2)
        result = self.advance(page, 1)
        self.assertIs(result["moved"], True)
        self.assertEqual(page.dom_page, 2)
        self.assertEqual(page.clicks, 1)         # 只点了一次，靠路由兜底恢复
        self.assertGreaterEqual(page.gotos, 1)

    def test_disabled_next_control_means_last_page(self):
        page = FakePagerPage(total_pages=3, start_page=3, disabled_on_last=True)
        result = self.advance(page, 3)
        self.assertIs(result["moved"], False)
        self.assertEqual(page.dom_page, 3)
        self.assertEqual(page.clicks, 0)
        self.assertEqual(page.gotos, 0)      # 明确禁用时不再白发路由跳转

    def test_enabled_but_dead_next_control_means_last_page(self):
        """末页的「下一页」没被禁用但点了不动：路由夹回当前页即判定末页。"""
        page = FakePagerPage(total_pages=3, start_page=3, disabled_on_last=False)
        result = self.advance(page, 3)
        self.assertIs(result["moved"], False)
        self.assertEqual(page.clicks, 1)
        self.assertEqual(page.dom_page, 3)

    def test_unrecognized_pager_is_retried_not_closed(self):
        """没有分页控件、路由也认不出来时返回 None（稍后重试），不能当成末页。"""
        page = FakePagerPage(total_pages=5, start_page=1, has_next_control=False)
        page.url = "https://example.test/course/#/foo"
        result = self.advance(page, 1)
        self.assertIsNone(result["moved"])

    # ── 路由兜底 ──
    def test_route_fallback_pages_when_no_control(self):
        page = FakePagerPage(total_pages=5, start_page=1, has_next_control=False,
                             render_ticks=2)
        result = self.advance(page, 1)
        self.assertIs(result["moved"], True)
        self.assertEqual(result["page"], 2)
        self.assertEqual(page.dom_page, 2)

    def test_route_clamped_back_to_current_page_is_last_page(self):
        page = FakePagerPage(total_pages=2, start_page=2, has_next_control=False)
        result = self.advance(page, 2)
        self.assertIs(result["moved"], False)
        self.assertEqual(page.dom_page, 2)

    # ── 兼容签名 ──
    def test_legacy_wrapper_returns_bool(self):
        page = FakePagerPage(total_pages=5, start_page=1, render_ticks=2)
        moved = asyncio.run(self.learner._advance_online_course_page(page, LIST_URL, 1))
        self.assertIs(moved, True)

    # ── 深链 ──
    def test_deep_link_jumps_straight_to_target_page(self):
        page = FakePagerPage(total_pages=8, start_page=1, render_ticks=2)
        self.assertTrue(asyncio.run(
            self.learner._goto_online_course_page(page, LIST_URL, 6)))
        self.assertEqual(page.dom_page, 6)
        self.assertEqual(page.gotos, 1)

    def test_deep_link_beyond_last_page_is_rejected(self):
        page = FakePagerPage(total_pages=3, start_page=1, render_ticks=2)
        self.assertFalse(asyncio.run(
            self.learner._goto_online_course_page(page, LIST_URL, 9)))
        self.assertEqual(page.dom_page, 3)

    def test_deep_link_needs_cards_to_render(self):
        class NoCardsPage(FakePagerPage):
            async def wait_for_selector(self, selector, **kwargs):
                raise TimeoutError(selector)

        page = NoCardsPage(total_pages=5, start_page=1, render_ticks=1)
        self.assertFalse(asyncio.run(
            self.learner._goto_online_course_page(page, LIST_URL, 3)))

    # ── 签名读取 ──
    def test_signature_falls_back_to_route_without_evaluate(self):
        class NoEvaluatePage:
            url = LIST_URL

            def is_closed(self):
                return False

        signature = asyncio.run(
            self.learner._online_course_page_signature(NoEvaluatePage()))
        self.assertEqual(signature["route"], 1)
        self.assertIsNone(signature["cards_set"])

    def test_signature_reads_cards_pager_and_route(self):
        page = FakePagerPage(total_pages=5, start_page=3)
        signature = asyncio.run(self.learner._online_course_page_signature(page))
        self.assertEqual(signature["route"], 3)
        self.assertEqual(signature["indicator"], 3)
        self.assertIn("课程3-0", signature["cards"])

    def test_observed_page_rejects_implausible_indices(self):
        learner = self.learner
        self.assertEqual(learner._observed_advanced_page({"indicator": 2}, 1), 2)
        self.assertIsNone(learner._observed_advanced_page({"indicator": 7}, 1))
        self.assertIsNone(learner._observed_advanced_page({"indicator": 1}, 1))
        self.assertIsNone(learner._observed_advanced_page({}, 1))


class TimeoutConstantsTests(unittest.TestCase):
    def test_defaults_are_configured(self):
        self.assertGreater(ONLINE_PAGE_CHANGE_TIMEOUT_MS, 0)
        self.assertGreater(ONLINE_PAGE_ROUTE_TIMEOUT_MS, 0)
        self.assertGreater(ONLINE_PAGE_SETTLE_DELAY_MS, 0)
        self.assertGreater(ONLINE_PAGE_DEEP_LINK_TIMEOUT_MS, 0)


if __name__ == "__main__":
    unittest.main()
