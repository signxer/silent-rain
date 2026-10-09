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
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    CHANNEL_PAGE_DATA_JS,  # noqa: E402
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
            self.page.pending_popup = PopupPage(
                self.spec["popup"],
                nav_ticks=int(self.spec.get("popup_nav_ticks") or 0),
                requests=self.spec.get("popup_requests") or ())
        elif self.spec.get("same_tab"):
            self.page.schedule_route(self.spec["same_tab"])


class AnchorLocator:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return len(self.page.anchors)

    def nth(self, index):
        return FakeAnchor(self.page, self.page.anchors[index])


class _BodyLocator:
    def __init__(self, page):
        self.page = page

    async def inner_text(self, timeout=None):
        return self.page.body_text


class _GateLocator:
    def __init__(self, page, hit):
        self.page = page
        self.hit = hit

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.hit else 0

    async def is_visible(self):
        return self.hit

    async def click(self, timeout=None):
        self.page.gate_clicks.append(1)
        if self.page.after_gate is not None:
            self.page.anchors = list(self.page.after_gate)
            self.page.body_text = ""


class PopupPage:
    """频道卡片 window.open 出来的弹窗：先 about:blank，随后才落到真地址。"""

    def __init__(self, url, nav_ticks=0, requests=()):
        self.target = url
        self.url = "about:blank" if nav_ticks > 0 else url
        self.nav_ticks = nav_ticks
        self.pending_requests = list(requests)
        self.listeners = []
        self.closed = False

    async def wait_for_load_state(self, state=None, timeout=None):
        return None

    async def wait_for_timeout(self, ms):
        if self.pending_requests and self.listeners:
            url = self.pending_requests.pop(0)
            for handler in list(self.listeners):
                handler(SimpleNamespace(url=url))
        if self.nav_ticks > 0:
            self.nav_ticks -= 1
            if self.nav_ticks == 0:
                self.url = self.target

    def on(self, event, handler):
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        if handler in self.listeners:
            self.listeners.remove(handler)

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
        self.anchor_dump = []
        self.body_text = ""
        self.login_form = False
        self.user_box = False
        self.gate_text = ""            # 页面上存在这个「同意」按钮
        self.gate_clicks = []
        self.after_gate = None         # 点完提示后替换成的新锚点
        self._pending_route = None
        self.route_ticks_left = 0
        self.request_listeners = []
        self.response_listeners = []
        self.page_data = []            # 组件状态里能直接读到的 {id,title}
        self.responses = []            # 导航期间的内容接口响应

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
        self.emit_responses()

    async def wait_for_function(self, script, timeout=None):
        return True

    async def evaluate(self, script):
        if self.evaluate_fails:
            raise RuntimeError("evaluate failed")
        if "querySelectorAll('a').length" in str(script):   # 就绪等待脚本
            return len(self.anchors)
        if "inputPwd" in str(script):                       # 登录状态脚本
            return {"hasPwd": bool(self.login_form),
                    "userBox": bool(self.user_box),
                    "hasSms": bool(self.login_form)}
        if "slice(0, 30)" in str(script):        # 链接清单 dump 脚本
            return list(getattr(self, "anchor_dump", []))
        if "__vueParentComponent" in str(script):    # 页面组件状态收割脚本
            return list(getattr(self, "page_data", []))
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
        if selector == "body":
            return _BodyLocator(self)
        return EmptyLocator()

    def get_by_text(self, text, exact=False):
        hit = bool(self.gate_text) and text == self.gate_text and not self.gate_clicks
        return _GateLocator(self, hit)

    def expect_event(self, name, timeout=None):
        return _EventContext(self)

    def on(self, event, handler):
        if event == "response":
            self.response_listeners.append(handler)
        else:
            self.request_listeners.append(handler)

    def remove_listener(self, event, handler):
        bucket = (self.response_listeners if event == "response"
                  else self.request_listeners)
        if handler in bucket:
            bucket.remove(handler)

    def emit_request(self, url):
        for handler in list(self.request_listeners):
            handler(SimpleNamespace(url=url))

    def emit_responses(self):
        for response in list(self.responses):
            for handler in list(self.response_listeners):
                handler(response)

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
        patcher = patch("main.console", SimpleNamespace(print=lambda *a, **k: None))
        patcher.start()
        self.addCleanup(patcher.stop)
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
        patcher = patch("main.console", SimpleNamespace(print=lambda *a, **k: None))
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def setUp(self):
        patcher = patch("main.console", SimpleNamespace(print=lambda *a, **k: None))
        patcher.start()
        self.addCleanup(patcher.stop)

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


class WorkshopIdFromLandingTests(unittest.TestCase):
    """点击后落地地址形状不确定：要尽力取到 ID，又不能把频道 ID 当成专题班。"""

    def test_prefers_detail_route(self):
        extract = AutoLearner._channel_workshop_id_from_landing
        self.assertEqual(extract(DETAIL_A), WS_A)
        self.assertEqual(extract(DETAIL_B), WS_B)

    def test_accepts_path_style_and_other_params(self):
        extract = AutoLearner._channel_workshop_id_from_landing
        self.assertEqual(
            extract(f"https://u.ccb.com/workshop/#/detail/{WS_C}"), WS_C)
        self.assertEqual(
            extract(f"https://u.ccb.com/portal/#/workshopDetail?workshopId={WS_C}"), WS_C)
        self.assertEqual(
            extract(f"https://u.ccb.com/sys/#/channel/course?workshop_id={WS_C}"), WS_C)

    def test_single_uuid_is_accepted_anywhere(self):
        extract = AutoLearner._channel_workshop_id_from_landing
        self.assertEqual(extract(f"https://u.ccb.com/x/#/y/{WS_C}"), WS_C)

    def test_channel_page_id_is_never_treated_as_workshop(self):
        """点击没跳走时页面还是频道地址，里面的 UUID 是频道 ID。"""
        extract = AutoLearner._channel_workshop_id_from_landing
        self.assertEqual(extract(CHANNEL_URL), "")
        self.assertEqual(extract(f"https://u.ccb.com/sys/#/channel/show/{CHANNEL_ID}"), "")

    def test_ambiguous_uuids_are_rejected(self):
        extract = AutoLearner._channel_workshop_id_from_landing
        self.assertEqual(
            extract(f"https://u.ccb.com/x/#/y/{WS_C}/{WS_D}"), "")

    def test_requests_can_supply_the_id(self):
        find = AutoLearner._workshop_id_from_requests
        urls = ["https://api.u.ccb.com/v1/user/me",
                f"https://api.u.ccb.com/v1/workshop/detail?id={WS_C}",
                "https://u.ccb.com/sys/#/channel/show/x"]
        self.assertEqual(find(urls), WS_C)

    def test_requests_ignore_channel_urls(self):
        self.assertEqual(
            AutoLearner._workshop_id_from_requests([CHANNEL_URL]), "")


class ChannelDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        patcher = patch("main.debug", side_effect=lambda m: self.logs.append(str(m)))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.console", SimpleNamespace(print=lambda *a, **k: None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.silent = lambda msg, style="": None

    def _learner(self):
        import threading
        learner = AutoLearner.__new__(AutoLearner)
        learner._stop_event = threading.Event()
        return learner

    def test_dump_lists_links_when_harvest_is_empty(self):
        page = FakeChannelPage(anchors=[{"label": "导航", "href": "javascript:void(0)"}])
        page.anchor_dump = [{"kind": "a", "label": "2026年信贷业务专题班",
                             "href": "javascript:void(0)", "attrs": "data-id"},
                            {"kind": "card", "label": "不是链接的卡片",
                             "href": "", "attrs": "channel-card"}]
        asyncio.run(self._learner()._dump_channel_anchors(page, self.silent))
        joined = "\n".join(self.logs)
        self.assertIn("频道页链接清单", joined)
        self.assertIn("2026年信贷业务专题班", joined)
        self.assertIn("javascript:void(0)", joined)
        self.assertIn("node[card]", joined)      # 非链接卡片也要列出来
        self.assertIn("channel-card", joined)

    def test_click_attempts_are_logged(self):
        """点了没反应也要留痕，否则日志里完全看不到。"""
        page = FakeChannelPage(anchors=[
            {"label": "信贷业务专题班（第一期）", "href": "javascript:void(0)"},
        ])
        asyncio.run(self._learner()._collect_channel_workshops(
            page, CHANNEL_URL, self.silent))
        joined = "\n".join(self.logs)
        self.assertIn("频道页点击[0]", joined)
        self.assertIn("id=-", joined)

    def test_requests_during_click_supply_the_id(self):
        class ClickRequestsThenNavPage(FakeChannelPage):
            async def _noop(self):
                return None

        page = ClickRequestsThenNavPage(route_delay=3, anchors=[
            {"label": "信贷业务专题班（第一期）", "href": "javascript:void(0)"},
        ])
        original_click = FakeAnchor.click

        async def click_with_request(self, timeout=None):
            self.page.clicks += 1
            self.page.emit_request(f"https://api.u.ccb.com/v1/workshop/detail?id={WS_D}")

        with patch.object(FakeAnchor, "click", click_with_request):
            found = asyncio.run(self._learner()._collect_channel_workshops(
                page, CHANNEL_URL, self.silent))
        self.assertEqual(found, [WS_D])


CHILD_ATTR_FIXTURE = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="channel">
  <a href="javascript:void(0)" class="card">
    <div class="card-body" data-workshop-id="{WS_B}">数据安全专题班</div>
  </a>
  <a href="javascript:void(0)" class="card">
    <span data-id="{WS_A}" onclick="openWorkshop('{WS_A}')">内控合规专题班</span>
  </a>
  <a href="javascript:void(0)" class="card">
    <div>纯 @click 卡片，属性里没有 ID</div>
  </a>
  <a href="https://u.ccb.com/portal/#/study">学习中心</a>
</div></body></html>"""


class ChannelHarvestChildAttrTests(unittest.TestCase):
    """ID 挂在链接的子元素上（Vue 常见：外层 a 只有 @click）时也要能读到。"""

    def setUp(self):
        patcher = patch("main.console", SimpleNamespace(print=lambda *a, **k: None))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_child_element_ids_are_harvested(self):
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
                    await page.set_content(CHILD_ATTR_FIXTURE)
                    raw = await page.evaluate(CHANNEL_WORKSHOP_HARVEST_JS)
                    learner = AutoLearner.__new__(AutoLearner)
                    import threading
                    learner._stop_event = threading.Event()
                    harvested = await learner._channel_harvest_workshops(
                        _RealEvaluatePage(page))
                    return raw, harvested
                finally:
                    await browser.close()

        outcome = asyncio.run(run())
        if isinstance(outcome, str):  # pragma: no cover
            self.skipTest(outcome)
        raw, harvested = outcome
        self.assertEqual([e["id"] for e in raw], [WS_B, WS_A])
        self.assertEqual(harvested, [WS_B, WS_A])
        # 标题取自外层链接（子元素没有完整文案时也要有可读名字）
        self.assertIn("数据安全专题班", [e["title"] for e in raw])


class ChannelGateTests(unittest.TestCase):
    """频道页的文明公约/登录遮挡：必须先过掉，且不要在遮挡页上乱点链接。"""

    def setUp(self):
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.CHANNEL_POPUP_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.learner = AutoLearner.__new__(AutoLearner)
        import threading
        self.learner._stop_event = threading.Event()

    def _collect(self, page):
        asyncio.run(self.learner._collect_channel_workshops(
            page, CHANNEL_URL, lambda m, s="": self.logs.append(str(m))))

    def test_gate_is_dismissed_then_content_is_harvested(self):
        page = FakeChannelPage(anchors=[{"label": "导航入口", "href": "javascript:void(0)"}])
        page.gate_text = "同意"
        page.body_text = "文明公约 取消 同意"
        page.after_gate = [{"label": "2026年信贷业务专题班", "href": DETAIL_A}]
        self._collect(page)
        self.assertEqual(page.gate_clicks, [1])
        self.assertTrue(any("通过提示" in m for m in self.logs), self.logs)

    def test_gated_page_stops_without_clicking_links(self):
        """停在登录/公约页时逐个点链接毫无意义——实测会点到《用户服务协议》上。"""
        page = FakeChannelPage(anchors=[
            {"label": "《用户服务协议》", "href": ""},
            {"label": "《隐私政策》", "href": ""},
            {"label": "《免责声明》", "href": ""},
        ])
        page.body_text = "密码登录 短信登录 获取验证码 文明公约取消 同意"
        self._collect(page)
        self.assertEqual(page.clicks, 0, "遮挡页上不该去点协议/导航链接")
        self.assertTrue(any("仍停在登录/提示页" in m for m in self.logs), self.logs)

    def test_non_content_labels_are_skipped_in_click_fallback(self):
        page = FakeChannelPage(anchors=[
            {"label": "《用户服务协议》", "href": "javascript:void(0)"},
            {"label": "首页返回入口", "href": "javascript:void(0)"},
            {"label": "获取验证码按钮", "href": "javascript:void(0)"},
        ])
        page.body_text = "正常页面，没有登录也没有公约"   # 不触发 gated 判定
        self._collect(page)
        self.assertEqual(page.clicks, 0)


class ChannelReadinessTests(unittest.TestCase):
    """采集前要等 SPA 渲染完，并把登录状态写进日志。"""

    def setUp(self):
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.CHANNEL_POPUP_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.debugs = []
        patcher = patch("main.debug", side_effect=lambda m: self.debugs.append(str(m)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)
        import threading
        self.learner._stop_event = threading.Event()

    def _collect(self, page):
        asyncio.run(self.learner._collect_channel_workshops(
            page, CHANNEL_URL, lambda m, s="": self.logs.append(str(m))))

    def test_login_state_is_logged(self):
        page = FakeChannelPage(anchors=[{"label": "信贷业务专题班（第一期）",
                                         "href": DETAIL_A}])
        page.user_box = True
        self._collect(page)
        self.assertTrue(any("频道页登录状态: logged" in m for m in self.debugs), self.debugs)

    def test_missing_login_is_reported_not_hidden(self):
        page = FakeChannelPage(anchors=[{"label": "首页入口", "href": "javascript:void(0)"}])
        page.login_form = True
        page.body_text = "密码登录 短信登录 获取验证码"
        self._collect(page)
        self.assertTrue(any("频道页登录状态: login-form" in m for m in self.debugs), self.debugs)
        self.assertTrue(any("可能没有登录" in m for m in self.logs), self.logs)

    def test_harvest_is_retried_for_async_rendered_content(self):
        """内容异步渲染：第一轮读不到，等一会再读要能读到。"""
        page = FakeChannelPage(anchors=[{"label": "页面骨架", "href": "javascript:void(0)"}])

        state = {"round": 0}
        original = FakeChannelPage.evaluate

        async def evaluate_with_delay(self, script):
            if "slice(0, 30)" not in str(script) and "querySelectorAll('a').length" not in str(script) \
                    and "inputPwd" not in str(script):
                state["round"] += 1
                if state["round"] >= 2:            # 第二轮才出现内容
                    self.anchors = [{"label": "信贷业务专题班（第一期）", "href": DETAIL_A}]
            return await original(self, script)

        with patch.object(FakeChannelPage, "evaluate", evaluate_with_delay):
            self._collect(page)
        from main import AutoLearner as _AL
        self.assertTrue(any("频道页采集到 1 个专题班" in m for m in self.logs), self.logs)


class PopupNavigationTests(unittest.TestCase):
    """频道卡片的弹窗先开成 about:blank：必须等它落到真地址，否则一律判空。"""

    def setUp(self):
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.CHANNEL_POPUP_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.CHANNEL_POPUP_NAV_MS", 600)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.learner = AutoLearner.__new__(AutoLearner)
        import threading
        self.learner._stop_event = threading.Event()

    def _collect(self, page):
        return asyncio.run(self.learner._collect_channel_workshops(
            page, CHANNEL_URL, lambda m, s="": None))

    def test_popup_url_is_waited_until_navigated(self):
        page = FakeChannelPage(anchors=[
            {"label": "《习近平关于中国式现代化论述》",
             "href": "javascript:void(0)", "popup": DETAIL_A, "popup_nav_ticks": 2},
        ])
        self.assertEqual(self._collect(page), [WS_A])

    def test_blank_popup_without_navigation_is_a_miss(self):
        page = FakeChannelPage(anchors=[
            {"label": "《习近平关于中国式现代化论述》",
             "href": "javascript:void(0)", "popup": "about:blank", "popup_nav_ticks": 999},
        ])
        self.assertEqual(self._collect(page), [])

    def test_popup_requests_can_supply_the_id(self):
        """弹窗地址始终是空的，但它请求了详情接口——从请求里取 ID。"""
        page = FakeChannelPage(anchors=[
            {"label": "《习近平关于中国式现代化论述》",
             "href": "javascript:void(0)", "popup": "about:blank", "popup_nav_ticks": 999,
             "popup_requests": [f"https://api.u.ccb.com/v1/workshop/detail?id={WS_D}"]},
        ])
        self.assertEqual(self._collect(page), [WS_D])


class _FakeResponse:
    def __init__(self, url, body, ctype="application/json"):
        self.url = url
        self.headers = {"content-type": ctype}
        self._body = body

    async def text(self):
        return self._body


class ChannelFastHarvestTests(unittest.TestCase):
    """别逐张点击：优先从页面组件状态/内容接口一次拿全。"""

    def setUp(self):
        patcher = patch("main.CHANNEL_DETAIL_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.CHANNEL_POPUP_WAIT_MS", 120)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []
        self.learner = AutoLearner.__new__(AutoLearner)
        import threading
        self.learner._stop_event = threading.Event()

    def _collect(self, page):
        return asyncio.run(self.learner._collect_channel_workshops(
            page, CHANNEL_URL, lambda m, s="": self.logs.append(str(m))))

    def test_page_data_harvest_needs_no_clicking(self):
        """组件状态里有 ID 时，一次 evaluate 就够，绝不去点卡片。"""
        page = FakeChannelPage(anchors=[
            {"label": "页面骨架", "href": "javascript:void(0);"}])
        page.page_data = [{"id": WS_A, "title": "《习近平关于中国式现代化论述》"},
                          {"id": WS_B, "title": "《伟大建党精神与国防和军队现代化》"}]
        self.assertEqual(self._collect(page), [WS_A, WS_B])
        self.assertEqual(page.clicks, 0, "能直接读数据就不该逐张点击")
        self.assertTrue(any("页面数据" in m for m in self.logs), self.logs)

    def test_channel_id_is_excluded_from_page_data(self):
        page = FakeChannelPage(anchors=[
            {"label": "页面骨架", "href": "javascript:void(0);"}])
        page.page_data = [{"id": CHANNEL_ID, "title": "频道本体"},
                          {"id": WS_A, "title": "《某专题》"}]
        self.assertEqual(self._collect(page), [WS_A])

    def test_response_harvest_is_used_when_page_data_is_empty(self):
        page = FakeChannelPage(anchors=[
            {"label": "页面骨架", "href": "javascript:void(0);"}])
        page.responses = [_FakeResponse(
            "https://api.u.ccb.com/v1/cu/getChannelContent",
            '{"data":[{"id":"%s"},{"id":"%s"}]}' % (WS_A, WS_B))]
        self.assertEqual(self._collect(page), [WS_A, WS_B])
        self.assertEqual(page.clicks, 0)

    def test_page_data_wins_over_clicking_and_dedups(self):
        page = FakeChannelPage(anchors=[
            {"label": "《某专题》", "href": DETAIL_A}])
        page.page_data = [{"id": WS_A, "title": "《某专题》"},
                          {"id": WS_A, "title": "《某专题》重复"}]
        self.assertEqual(self._collect(page), [WS_A])

    def test_channel_id_from_url(self):
        from main import _channel_id_from_url
        self.assertEqual(_channel_id_from_url(CHANNEL_URL), CHANNEL_ID)
        self.assertEqual(_channel_id_from_url("https://u.ccb.com/portal/#/study"), "")


class PageDataScriptBrowserTests(unittest.TestCase):
    """在真实 Chromium 里验证组件状态收割脚本确实能取到 ID。"""

    HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
    <div class="channel-show">
      <div class="card-item" id="c1">《习近平关于中国式现代化论述》</div>
      <div class="card-item" id="c2">《伟大建党精神与国防和军队现代化》</div>
      <div class="card-item" id="c3">频道本体</div>
    </div>
    <script>
      const ids = ['11111111-2222-3333-4444-555555555555',
                   'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                   '%s'];
      document.querySelectorAll('.card-item').forEach((el, i) => {
        el.__vueParentComponent = {
          props: {item: {dataStoreId: ids[i], title: el.innerText}},
          setupState: {unrelated: i},
        };
      });
      document.body.dataset.done = '1';
    </script></body></html>""" % CHANNEL_ID

    def test_script_reads_ids_from_component_state(self):
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
                    await page.set_content(self.HTML)
                    return await page.evaluate(CHANNEL_PAGE_DATA_JS)
                finally:
                    await browser.close()

        out = asyncio.run(go())
        if isinstance(out, str):  # pragma: no cover
            self.skipTest(out)
        ids = sorted(entry["id"] for entry in out)
        self.assertEqual(ids, sorted([WS_A, WS_B, CHANNEL_ID]))
        titles = {entry["id"]: entry["title"] for entry in out}
        self.assertIn("中国式现代化", titles[WS_A])


if __name__ == "__main__":
    unittest.main()
