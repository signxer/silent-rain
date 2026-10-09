"""学习频道页（/sys/#/channel/show/<id>）解析测试。

频道页里的入口本身就是专题班，所以解析应该"能静态读出来就别点"：
  1. 直接读 href / data-* / onclick 里的专题班 ID（不点击、不看弹窗）；
  2. 读不到才回退到点击卡片，并且必须等同标签页跳转真的落到 /detail 再取 ID
     （点击后立刻读地址会读到跳转前的旧路由，这正是原来"解析出问题"的根因之一）；
  3. 频道页改写自身路由时也不能因此判定失败。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    AutoLearner,
    CHANNEL_WORKSHOP_HARVEST_JS,
)

CHANNEL_URL = "https://u.ccb.com/sys/#/channel/show/268bc9bb-3ed5-466c-adca-39191ed61666"
WS_A = "11111111-2222-3333-4444-555555555555"
WS_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WS_C = "99999999-8888-7777-6666-555555555555"
WS_D = "dddddddd-1111-2222-3333-444444444444"
CHANNEL_ID = "268bc9bb-3ed5-466c-adca-39191ed61666"
DETAIL_A = f"https://u.ccb.com/workshop/#/detail?id={WS_A}&logChannelId=abc"
DETAIL_B = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={WS_B}"


class EmptyLocator:
    def filter(self, **kwargs):
        return self

    async def count(self):
        return 0

    def nth(self, index):
        return self


class FakeAnchor:
    """spec: label / href / attrs / popup(弹窗地址) / same_tab(同标签页地址)"""

    def __init__(self, page, spec):
        self.page = page
        self.spec = spec

    async def get_attribute(self, name):
        if name == "href":
            return self.spec.get("href", "")
        return (self.spec.get("attrs") or {}).get(name)

    async def inner_text(self):
        return self.spec.get("label", "")

    async def click(self, timeout=None):
        self.page.clicks += 1
        if self.spec.get("popup"):
            self.page.pending_popup = PopupPage(self.spec["popup"])
        elif self.spec.get("same_tab"):
            self.page.schedule_route(self.spec["same_tab"])


class AnchorLocator:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return len(self.page.anchors)

    def nth(self, index):
        return FakeAnchor(self.page, self.page.anchors[index])


class PopupPage:
    def __init__(self, url):
        self.url = url
        self.closed = False

    async def wait_for_load_state(self, state=None, timeout=None):
        return None

    async def close(self):
        self.closed = True


class _EventContext:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def value(self):
        page = self.page

        async def _resolve():
            if page.pending_popup is None:
                raise asyncio.TimeoutError("popup did not open")
            popup, page.pending_popup = page.pending_popup, None
            return popup

        return _resolve()


class FakeChannelPage:
    """模型化频道页：锚点、弹窗、同标签页路由延迟。"""

    def __init__(self, url=CHANNEL_URL, anchors=(), route_delay=0,
                 goto_fails=False, evaluate_fails=False):
        self.url = url
        self.anchors = list(anchors)
        self.route_delay = route_delay      # 同标签页跳转要等几次 wait_for_timeout 才落到 /detail
        self.goto_fails = goto_fails
        self.evaluate_fails = evaluate_fails
        self.clicks = 0
        self.gotos = []
        self.timeouts = 0
        self.pending_popup = None
        self._pending_route = None
        self.route_ticks_left = 0

    # ── 路由 ──
    def schedule_route(self, target):
        if not target:
            return
        self.url = target if self.route_delay <= 0 else self.url
        self._pending_route = None if self.route_delay <= 0 else target
        self.route_ticks_left = max(0, self.route_delay)

    def _tick(self):
        if self._pending_route is None:
            return
        if self.route_ticks_left <= 1:
            self.url = self._pending_route
            self._pending_route = None
            self.route_ticks_left = 0
        else:
            self.route_ticks_left -= 1

    async def wait_for_timeout(self, ms):
        self.timeouts += 1
        self._tick()

    # ── Playwright 接口 ──
    async def goto(self, url, wait_until=None, timeout=None):
        if self.goto_fails:
            raise TimeoutError("net::ERR_CONNECTION_TIMED_OUT")
        self.gotos.append(url)
        self.url = url

    async def wait_for_function(self, script, timeout=None):
        return True

    async def evaluate(self, script):
        if self.evaluate_fails:
            raise RuntimeError("evaluate failed")
        out = []
        seen = set()
        for spec in self.anchors:
            attrs = dict(spec.get("attrs") or {})
            href = spec.get("href", "")
            is_anchor = bool(href) or "href" in spec
            for name in ("href", "data-href", "data-url", "data-workshop-id",
                         "data-workshopid", "data-id", "onclick"):
                value = href if name == "href" else attrs.get(name)
                candidate = _candidate_id(name, value, is_anchor)
                if candidate:
                    if candidate not in seen:
                        seen.add(candidate)
                        out.append({"id": candidate, "title": spec.get("label", ""),
                                    "source": name, "raw": str(value or "")})
                    break
        return out

    def locator(self, selector):
        if selector == "a":
            return AnchorLocator(self)
        return EmptyLocator()

    def expect_event(self, name, timeout=None):
        return _EventContext(self)

    def is_closed(self):
        return False


_ID_PATTERN = (r"(?:[?&](?:id|workshopId|workshop_id)=|/detail/|/myworkshop/detail/)"
               r"([0-9a-zA-Z_-]{8,})")
_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_IDLIKE_PATTERN = r"^[0-9a-zA-Z_-]{8,}$"


def _candidate_id(name, value, is_anchor):
    """与 CHANNEL_WORKSHOP_HARVEST_JS 等价的镜像实现（靠浏览器用例防漂移）。"""
    import re
    if not value:
        return ""
    text = str(value)
    m = re.search(_ID_PATTERN, text)
    if m:
        return m.group(1)
    if name in ("data-workshop-id", "data-workshopid"):
        return text.strip() if re.match(_IDLIKE_PATTERN, text.strip()) else ""
    if name == "data-id" and is_anchor:
        return text.strip() if re.match(_IDLIKE_PATTERN, text.strip()) else ""
    if name == "onclick":
        u = re.search(_UUID_PATTERN, text)
        return u.group(0) if u else ""
    return ""


class WorkshopIdParsingTests(unittest.TestCase):
    def test_parses_detail_routes(self):
        parse = AutoLearner._channel_workshop_id_from_url
        self.assertEqual(parse(DETAIL_A), WS_A)
        # logChannelId 是频道 ID，不能顶替 ?id=
        self.assertEqual(
            parse(f"https://u.ccb.com/workshop/#/detail?logChannelId={CHANNEL_ID}&id={WS_D}"),
            WS_D)
        self.assertEqual(parse(DETAIL_B), WS_B)
        self.assertEqual(parse(f"https://u.ccb.com/workshop/#/detail?id={WS_C}"), WS_C)

    def test_rejects_non_detail_routes(self):
        parse = AutoLearner._channel_workshop_id_from_url
        self.assertEqual(parse(CHANNEL_URL), "")
        self.assertEqual(parse("https://u.ccb.com/workshop/#/index?collegeId="), "")
        self.assertEqual(parse(""), "")
        self.assertEqual(parse("not a url"), "")

    def test_looks_like_channel_card(self):
        check = AutoLearner._looks_like_channel_card
        self.assertTrue(check(""))
        self.assertTrue(check("javascript:void(0)"))
        self.assertTrue(check("https://u.ccb.com/workshop/#/detail?id=x"))
        self.assertFalse(check("https://u.ccb.com/portal/#/study"))


class ChannelHarvestTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)
        import threading
        self.learner._stop_event = threading.Event()

    def collect(self, page):
        return asyncio.run(self.learner._collect_channel_workshops(page, CHANNEL_URL))

    def test_reads_workshop_ids_from_hrefs_without_clicking(self):
        page = FakeChannelPage(anchors=[
            {"label": "2026年信贷业务专题班（第一期）", "href": DETAIL_A},
            {"label": "合规管理专题班", "href": DETAIL_B},
            {"label": "学习中心", "href": "https://u.ccb.com/portal/#/study"},
        ])
        self.assertEqual(self.collect(page), [WS_A, WS_B])
        self.assertEqual(page.clicks, 0)      # 关键：完全不需要点击

    def test_reads_ids_from_data_attributes_and_onclick(self):
        page = FakeChannelPage(anchors=[
            {"label": "内控合规专题班", "href": "javascript:void(0)",
             "attrs": {"onclick": f"openWorkshop('{WS_A}')", "_same_tab": False,
                       "data-id": WS_A}},
            {"label": "数据安全专题班", "href": "javascript:void(0)",
             "attrs": {"data-workshop-id": WS_B}},
        ])
        self.assertEqual(self.collect(page), [WS_A, WS_B])
        self.assertEqual(page.clicks, 0)

    def test_skips_course_and_trainingcamp_links(self):
        page = FakeChannelPage(anchors=[
            {"label": "某门课程", "href": f"https://u.ccb.com/course/#/detail?id={WS_A}"},
            {"label": "某个训练营", "href": f"https://u.ccb.com/trainingcamp/#/traincamp/study/{WS_B}/{WS_C}"},
            {"label": "真正的专题班", "href": DETAIL_B},
        ])
        self.assertEqual(self.collect(page), [WS_B])

    def test_deduplicates_repeated_ids(self):
        page = FakeChannelPage(anchors=[
            {"label": "专题班（卡片）", "href": DETAIL_A},
            {"label": "专题班（标题）", "href": DETAIL_A},
            {"label": "另一个专题班", "href": DETAIL_B},
        ])
        self.assertEqual(self.collect(page), [WS_A, WS_B])

    def test_channel_route_rewrite_still_works(self):
        """频道页改写自身路由时（不再是 /channel/show/），收割不能因此失败。"""
        page = FakeChannelPage(
            url="https://u.ccb.com/sys/#/channel/detail/268bc9bb",
            anchors=[{"label": "信贷业务专题班", "href": DETAIL_A}])
        self.assertEqual(self.collect(page), [WS_A])


class ChannelClickFallbackTests(unittest.TestCase):
    def setUp(self):
        # 把等待预算压到毫秒级：非卡片链接点了不动时不该拖慢测试
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)
        import threading
        self.learner._stop_event = threading.Event()

    def collect(self, page):
        return asyncio.run(self.learner._collect_channel_workshops(page, CHANNEL_URL))

    def test_popup_click_is_used_when_links_have_no_id(self):
        page = FakeChannelPage(anchors=[
            {"label": "2026年信贷业务专题班（第一期）", "href": "javascript:void(0)",
             "popup": DETAIL_A},
            {"label": "没有 ID 的导航链接", "href": "javascript:void(0)"},
        ])
        self.assertEqual(self.collect(page), [WS_A])
        self.assertGreaterEqual(page.clicks, 1)

    def test_same_tab_navigation_waits_for_detail_route(self):
        """同标签页跳转有渲染延迟：必须等路由真的落到 /detail 再取 ID。"""
        page = FakeChannelPage(route_delay=3, anchors=[
            {"label": "合规管理专题班（第二期）", "href": "javascript:void(0)",
             "same_tab": DETAIL_B},
        ])
        self.assertEqual(self.collect(page), [WS_B])
        self.assertEqual(page.clicks, 1)
        self.assertGreater(page.timeouts, 0, "应轮询等待路由变化，而不是点完立刻读地址")

    def test_same_tab_race_is_really_the_bug(self):
        """反证：点击后不等待（route_delay 大于零且不轮询）就只能拿到旧地址。"""
        page = FakeChannelPage(route_delay=3, anchors=[
            {"label": "合规管理专题班（第二期）", "href": "javascript:void(0)",
             "same_tab": DETAIL_B},
        ])
        page.anchors[0]["same_tab"] = DETAIL_B
        # 直接点击后立刻读地址（旧实现的行为）→ 还是频道页，取不到 ID
        asyncio.run(FakeAnchor(page, page.anchors[0]).click())
        self.assertEqual(
            AutoLearner._channel_workshop_id_from_url(page.url), "")
        # 走真实实现则能等到 /detail
        self.assertEqual(self.collect(page), [WS_B])

    def test_gives_up_gracefully_when_nothing_found(self):
        page = FakeChannelPage(anchors=[
            {"label": "只有导航的频道页", "href": "https://u.ccb.com/portal/#/study"},
        ])
        self.assertEqual(self.collect(page), [])

    def test_click_fallback_is_skipped_when_harvest_succeeds(self):
        page = FakeChannelPage(anchors=[
            {"label": "信贷业务专题班", "href": DETAIL_A},
        ])
        self.assertEqual(self.collect(page), [WS_A])
        self.assertEqual(page.clicks, 0)

    def test_goto_failure_returns_empty(self):
        page = FakeChannelPage(goto_fails=True, anchors=[
            {"label": "信贷业务专题班", "href": DETAIL_A}])
        self.assertEqual(self.collect(page), [])

    def test_evaluate_failure_falls_back_to_clicking(self):
        page = FakeChannelPage(evaluate_fails=True, anchors=[
            {"label": "信贷业务专题班（第一期）", "href": "javascript:void(0)",
             "popup": DETAIL_A},
        ])
        self.assertEqual(self.collect(page), [WS_A])
        self.assertEqual(page.clicks, 1)


# 频道页样例：同时用同一份数据生成「HTML fixture」和「假页面 spec」，
# 这样真实脚本与假页面镜像的结果可以直接比对，谁改漏了都会被发现。
CHANNEL_CASES = (
    ("2026年信贷业务专题班（第一期）", DETAIL_A, {}),
    ("内控合规专题班", "javascript:void(0)",
     {"data-id": WS_A, "onclick": f"openWorkshop('{WS_A}')"}),
    ("数据安全专题班", "javascript:void(0)", {"data-workshop-id": WS_B}),
    ("某门课程", f"https://u.ccb.com/course/#/detail?id={WS_A}", {}),
    ("某个训练营", f"https://u.ccb.com/trainingcamp/#/traincampdetail/{WS_B}/away", {}),
    ("学习中心", "https://u.ccb.com/portal/#/study", {}),
    ("另一个专题班",
     f"https://u.ccb.com/workshop/#/detail?id={WS_C}&logChannelId=xyz", {}),
    # 频道 ID 排在前面时，必须取 ?id=（专题班），不能取 logChannelId（频道）
    ("频道参数在前的专题班",
     f"https://u.ccb.com/workshop/#/detail?logChannelId={CHANNEL_ID}&id={WS_D}", {}),
)


def _fixture_html(cases):
    rows = []
    for label, href, attrs in cases:
        attr_text = "".join(f' {name}="{value}"' for name, value in attrs.items())
        rows.append(f'<a href="{href}"{attr_text}>{label}</a>')
    return ("<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
            + "".join(rows) + "</body></html>")


def _fake_specs(cases):
    return [{"label": label, "href": href, "attrs": dict(attrs)}
            for label, href, attrs in cases]


class _RealEvaluatePage:
    """把真实 Playwright 页面的 evaluate 暴露给收割逻辑。"""

    def __init__(self, page, url=CHANNEL_URL):
        self._page = page
        self.url = url

    async def evaluate(self, script):
        return await self._page.evaluate(script)


def _new_learner():
    import threading
    learner = AutoLearner.__new__(AutoLearner)
    learner._stop_event = threading.Event()
    return learner


class ChannelHarvestBrowserTests(unittest.TestCase):
    """在真实 Chromium 里跑收割脚本：既验证脚本本身，也防假页面镜像漂移。"""

    def _with_browser(self, callback):
        try:
            from playwright.async_api import async_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright 未安装")

        async def run():
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as exc:  # pragma: no cover
                    return f"skip:{exc}"
                try:
                    page = await browser.new_page()
                    await page.set_content(_fixture_html(CHANNEL_CASES))
                    return await callback(page)
                finally:
                    await browser.close()

        outcome = asyncio.run(run())
        if isinstance(outcome, str) and outcome.startswith("skip:"):  # pragma: no cover
            self.skipTest(outcome)
        return outcome

    def test_real_script_extracts_ids_and_python_filters_them(self):
        async def callback(page):
            raw = await page.evaluate(CHANNEL_WORKSHOP_HARVEST_JS)
            harvested = await _new_learner()._channel_harvest_workshops(
                _RealEvaluatePage(page))
            return raw, harvested

        raw, harvested = self._with_browser(callback)
        raw_ids = [entry["id"] for entry in raw]
        # 脚本抓到了带 ?id= 的链接、data-id 裸值、data-workshop-id 裸值
        self.assertEqual(raw_ids, [WS_A, WS_B, WS_C, WS_D])
        self.assertNotIn(CHANNEL_ID, raw_ids)   # 频道 ID 不能被当成专题班 ID
        # Python 侧剔除课程/训练营链接并去重（内控合规与首条重复，只保留首次出现）
        self.assertEqual(harvested, [WS_A, WS_B, WS_C, WS_D])

    def test_fake_page_mirror_matches_real_script(self):
        async def callback(page):
            return await page.evaluate(CHANNEL_WORKSHOP_HARVEST_JS)

        raw = self._with_browser(callback)
        script_ids = [entry["id"] for entry in raw]
        mirror_ids = asyncio.run(
            FakeChannelPage(anchors=_fake_specs(CHANNEL_CASES)).evaluate(""))
        self.assertEqual(
            script_ids, [entry["id"] for entry in mirror_ids],
            "假页面镜像与真实收割脚本结果不一致，请同步更新 _candidate_id")


if __name__ == "__main__":
    unittest.main()
