"""学习中心学时解析测试。

覆盖报告的问题：部分用户的「今年已训」单元格里会多出一行说明文字
（如「2023年以来已学习xxxx学时」），旧实现按「第 N 个数字+学时」取值，
于是把说明句里的数字当成了网络自学学时。
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    AutoLearner,
    _STUDY_HOURS_DOM_JS,
    _STUDY_HOURS_DOM_JS_TEMPLATE,
    _hours_region_text,
    _required_region_text,
    parse_required_hours_text,
    parse_study_hours_dom,
    parse_study_hours_text,
    resolve_study_hours,
)


# 真实页面文本（取自 moisten_debug.log 的学习中心 inner_text）
REAL_PAGE_TEXT = """学习中心
总行
曹书恒
研究
学习
我的
管理
总体培训情况
集中培训
网络自学
应训时长
每年应完成
90学时
每年应完成
50学时
今年已训
242学时
14.25学时
完成进度
今年
100%
今年
28.5%
其中：基本培训
2023-2027年应完成 240学时
31.98%
学时指标:
1. 全行七职等及以上人员毎5年参加集中培训累计不少于3个月或550学时；其他人员毎年参加集中培训累计不少于12天或90学时。
2. 全行干部员工毎年参加网络自学累计不少于50学时。
3. 全行七职等及以上人员每5年参加基本培训累计不少于360学时；其他人员每5年参加基本培训累计不少于240小时。
集中培训 主要包括以下4种情形：
1. 经组织选调参加的脱产培训；
2. 参加党委理论学习中心组学习；
网络自学 学时根据建行学习平台学习数据及培训管理信息系统数据进行统计。
我的必学
更多
今日没有学习任务
我的选学
更多
课程
微课
读书
案例
直播
专题班
训练营
   共计12.75学时
"""

_ROW = "今年已训\n242学时\n14.25学时"


def _page_text_with_row(row: str) -> str:
    return REAL_PAGE_TEXT.replace(_ROW, row)


class StudyHoursTextParseTests(unittest.TestCase):
    """文本兜底解析（不依赖浏览器）。"""

    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_page_keeps_known_values(self):
        self.assertEqual(parse_study_hours_text(REAL_PAGE_TEXT),
                         {"central": 242.0, "online": 14.25})

    def test_annotation_line_does_not_shift_online_hours(self):
        """回归：说明行「2023年以来已学习848.05学时」不能再顶替网络自学。"""
        text = _page_text_with_row(
            "今年已训\n848.05 学时\n2023年以来已学习848.05学时\n51 学时 ›")
        self.assertEqual(parse_study_hours_text(text),
                         {"central": 848.05, "online": 51.0})

    def test_annotation_with_other_number_and_spaces(self):
        text = _page_text_with_row(
            "今年已训\n848.05学时\n2023年以来已学习 300 学时\n51学时")
        self.assertEqual(parse_study_hours_text(text),
                         {"central": 848.05, "online": 51.0})

    def test_annotation_merged_into_value_line(self):
        """说明文字与数值挤在同一行（无换行）时也不能误取。"""
        text = _page_text_with_row(
            "今年已训\n848.05学时2023年以来已学习300学时\n51 学时 ›")
        self.assertEqual(parse_study_hours_text(text),
                         {"central": 848.05, "online": 51.0})

    def test_annotation_on_online_column(self):
        text = _page_text_with_row(
            "今年已训\n242学时\n51学时\n2023年以来已学习51学时")
        self.assertEqual(parse_study_hours_text(text),
                         {"central": 242.0, "online": 51.0})

    def test_decimal_and_arrow_variants(self):
        text = _page_text_with_row("今年已训\n1234.5 学时 ›\n0.75学时")
        self.assertEqual(parse_study_hours_text(text),
                         {"central": 1234.5, "online": 0.75})

    def test_missing_row_label_falls_back_to_field_order(self):
        """没有行标签时保持旧行为：第 3、4 个学时字段为 已训A/已训B。"""
        text = REAL_PAGE_TEXT.replace("今年已训\n", "")
        self.assertEqual(parse_study_hours_text(text),
                         {"central": 242.0, "online": 14.25})

    def test_indicator_section_numbers_never_leak(self):
        text = _page_text_with_row("今年已训\n242学时\n14.25学时")
        result = parse_study_hours_text(text)
        self.assertNotIn(550.0, result.values())
        self.assertNotIn(240.0, result.values())
        self.assertNotIn(360.0, result.values())

    def test_empty_and_noise_only_text(self):
        self.assertEqual(parse_study_hours_text(""),
                         {"central": None, "online": None})
        self.assertEqual(parse_study_hours_text("学时指标：不少于50学时"),
                         {"central": None, "online": None})

    def test_required_hours_from_real_page(self):
        """应训时长行：集中培训每年 90、网络自学每年 50。"""
        self.assertEqual(parse_required_hours_text(REAL_PAGE_TEXT),
                         {"required_central": 90.0, "required_online": 50.0})

    def test_required_hours_single_line_layout(self):
        """「每年应完成 90 学时」挤在一行时也要能取到。"""
        text = REAL_PAGE_TEXT.replace(
            "应训时长\n每年应完成\n90学时\n每年应完成\n50学时",
            "应训时长\n每年应完成 90 学时\n每年应完成 50 学时")
        self.assertEqual(parse_required_hours_text(text),
                         {"required_central": 90.0, "required_online": 50.0})

    def test_required_hours_decimal(self):
        """「5年550学时」这类折算值也要支持小数。"""
        text = REAL_PAGE_TEXT.replace(
            "应训时长\n每年应完成\n90学时\n每年应完成\n50学时",
            "应训时长\n每年应完成\n137.5学时\n每年应完成\n62.5学时")
        self.assertEqual(parse_required_hours_text(text),
                         {"required_central": 137.5, "required_online": 62.5})

    def test_required_hours_absent(self):
        self.assertEqual(parse_required_hours_text(""),
                         {"required_central": None, "required_online": None})
        self.assertEqual(parse_required_hours_text(REAL_PAGE_TEXT.replace("应训时长", "")),
                         {"required_central": None, "required_online": None})

    def test_required_region_stops_at_actual_row(self):
        region = _required_region_text(REAL_PAGE_TEXT)
        self.assertIn("90学时", region)
        self.assertIn("50学时", region)
        self.assertNotIn("今年已训", region)
        self.assertNotIn("242学时", region)

    def test_resolve_carries_required_hours(self):
        result = resolve_study_hours(None, REAL_PAGE_TEXT)
        self.assertEqual((result["required_central"], result["required_online"]),
                         (90.0, 50.0))
        # 今年已训不受影响
        self.assertEqual((result["central"], result["online"]), (242.0, 14.25))

    def test_dom_required_wins_over_text(self):
        result = resolve_study_hours(
            {"central": 1.0, "online": 2.0, "required_central": 120.0,
             "required_online": 60.0}, REAL_PAGE_TEXT)
        self.assertEqual((result["required_central"], result["required_online"]),
                         (120.0, 60.0))

    def test_region_stops_at_next_section_label(self):
        region = _hours_region_text(REAL_PAGE_TEXT)
        self.assertIn("242学时", region)
        self.assertNotIn("完成进度", region)
        self.assertNotIn("学时指标", region)
        self.assertNotIn("31.98%", region)
        self.assertEqual(_hours_region_text("没有任何标签"), "")


class StudyHoursResolveTests(unittest.TestCase):
    """DOM 结果与文本结果的合并策略。"""

    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_complete_dom_wins(self):
        result = resolve_study_hours({"central": 11.0, "online": 22.0},
                                     "今年已训\n999学时\n888学时")
        self.assertEqual((result["central"], result["online"]), (11.0, 22.0))
        self.assertEqual(result["source"], "dom")

    def test_text_fills_missing_column(self):
        result = resolve_study_hours({"central": 848.05, "online": None},
                                     REAL_PAGE_TEXT.replace(
                                         "242学时\n14.25学时", "848.05学时\n14.25学时"))
        self.assertEqual((result["central"], result["online"]), (848.05, 14.25))
        self.assertEqual(result["source"], "dom+text")

    def test_text_used_when_dom_unavailable(self):
        result = resolve_study_hours(None, REAL_PAGE_TEXT)
        self.assertEqual((result["central"], result["online"]), (242.0, 14.25))
        self.assertEqual(result["source"], "text")

    def test_invalid_dom_values_are_ignored(self):
        dom = {"central": "not-a-number", "online": -1}
        parsed = parse_study_hours_dom(dom)
        self.assertIsNone(parsed["central"])
        self.assertIsNone(parsed["online"])
        result = resolve_study_hours(dom, REAL_PAGE_TEXT)
        self.assertEqual((result["central"], result["online"]), (242.0, 14.25))

    def test_zero_hours_from_dom_are_kept(self):
        result = resolve_study_hours({"central": 0, "online": 0},
                                     REAL_PAGE_TEXT)
        self.assertEqual((result["central"], result["online"]), (0.0, 0.0))
        self.assertEqual(result["total"], 0.0)
        self.assertEqual(result["source"], "dom")

    def test_missing_everything_returns_zeros(self):
        result = resolve_study_hours(None, "")
        self.assertEqual((result["central"], result["online"], result["total"]),
                         (0.0, 0.0, 0.0))


class FakeLocator:
    def __init__(self, text):
        self._text = text

    async def inner_text(self, timeout=None):
        return self._text


class FakePage:
    """最小化的 Playwright Page 替身，用于校验 _fetch_study_hours 的取数顺序。"""

    def __init__(self, text, dom_result):
        self.url = ""
        self.text = text
        self.dom_result = dom_result
        self._closed = False
        self.closed_before_evaluate = False
        self.evaluated = 0
        self.goto_url = None

    def is_closed(self):
        return self._closed

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_url = url
        self.url = url

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def wait_for_timeout(self, ms):
        return None

    def locator(self, selector):
        return FakeLocator(self.text)

    async def evaluate(self, script):
        self.evaluated += 1
        if self._closed:
            self.closed_before_evaluate = True
            raise RuntimeError("page closed")
        return self.dom_result

    async def close(self):
        self._closed = True


class FakeContext:
    def __init__(self, text, dom_result):
        self.text = text
        self.dom_result = dom_result
        self.created = 0

    async def new_page(self):
        self.created += 1
        return FakePage(self.text, self.dom_result)


class FetchStudyHoursWiringTests(unittest.TestCase):
    """校验 _fetch_study_hours 的接线：先取 DOM 再关页面，并合并文本兜底。"""

    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _learner(self):
        learner = AutoLearner.__new__(AutoLearner)
        learner._hours_cache = {"value": None, "ts": 0.0}
        learner._hours_ttl = 60.0
        learner._hours_lock = None
        return learner

    def test_annotation_page_resolves_from_dom_result(self):
        learner = self._learner()
        text = _page_text_with_row(
            "今年已训\n848.05 学时\n2023年以来已学习848.05学时\n51 学时 ›")
        page = FakePage(text, {"central": 848.05, "online": 51})
        result = asyncio.run(learner._fetch_study_hours(page))
        self.assertEqual((result["central"], result["online"]), (848.05, 51.0))
        self.assertEqual(result["total"], 899.05)
        self.assertIn("/portal/#/studyCenter", page.goto_url)

    def test_temporary_page_is_evaluated_before_close(self):
        learner = self._learner()
        context = FakeContext(REAL_PAGE_TEXT, {"central": None, "online": None})
        learner.context = context
        result = asyncio.run(learner._fetch_study_hours(None))
        self.assertEqual(context.created, 1)
        self.assertEqual((result["central"], result["online"]), (242.0, 14.25))

    def test_empty_dom_result_falls_back_to_text(self):
        learner = self._learner()
        context = FakeContext(REAL_PAGE_TEXT, None)
        learner.context = context
        result = asyncio.run(learner._fetch_study_hours(None))
        self.assertEqual((result["central"], result["online"]), (242.0, 14.25))


class StudyHoursEmbeddedJsTests(unittest.TestCase):
    """JS 模板注入：Python 标签常量必须真正进入渲染后的脚本。"""

    PLACEHOLDERS = ("__ROW_LABELS__", "__PREV_ROW_LABELS__", "__ROW_END_LABELS__",
                    "__CENTRAL_LABEL__", "__ONLINE_LABEL__")

    def test_template_placeholders_are_substituted(self):
        for placeholder in self.PLACEHOLDERS:
            self.assertIn(placeholder, _STUDY_HOURS_DOM_JS_TEMPLATE)
            self.assertNotIn(placeholder, _STUDY_HOURS_DOM_JS)

    def test_rendered_js_carries_label_constants(self):
        for label in ("今年已训", "应训时长", "完成进度", "集中培训", "网络自学"):
            self.assertIn(f'"{label}"', _STUDY_HOURS_DOM_JS)


def _study_center_html(central_value, central_note, online_value,
                       online_note="", bare_note_number=None,
                       value_unit_in_same_element=False,
                       note_own_line=False, include_prev_row=True,
                       row_label_suffix=""):
    """构造与截图同构的「总体培训情况」卡片（grid 布局）。"""
    def cell(value, note, bare_number):
        if value_unit_in_same_element:
            body = f'<div class="val">{value}<span class="unit">学时</span></div>'
        else:
            body = (f'<div class="val"><span class="num">{value}</span>'
                    f'<span class="unit">学时</span></div>')
        if bare_number is not None:
            body += (f'<div class="note">2023年以来已学习'
                     f'<b><span class="num">{bare_number}</span></b>学时</div>')
        elif note:
            body += f'<div class="note">{note}</div>'
        return f'<div class="cell">{body}</div>'

    prev_row = "" if not include_prev_row else (
        '<div class="row"><div class="label">应训时长</div>\n'
        '  <div class="cell"><span class="sub">每年应完成</span>\n'
        '    <div class="val">90<span class="unit">学时</span></div></div>\n'
        '  <div class="cell"><span class="sub">每年应完成</span>\n'
        '    <div class="val">50<span class="unit">学时</span></div></div></div>')

    if note_own_line:
        notes = (f'<div class="note">2023年以来已学习</div>'
                 f'<div class="note">{online_note}</div>')
        central_cell = (f'<div class="cell"><div class="val">'
                        f'<span class="num">{central_value}</span>'
                        f'<span class="unit">学时</span></div>{notes}</div>')
    else:
        central_cell = cell(central_value, central_note, bare_note_number)

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;padding:20px;font-family:sans-serif}}
.card{{width:900px;border:1px solid #eee;border-radius:8px;padding:16px}}
.row,.head{{display:grid;grid-template-columns:180px 340px 340px;}}
.label{{padding:12px 8px;font-weight:600}}
.cell{{padding:12px 8px;text-align:center}}
.head .cell{{font-weight:700;font-size:18px}}
.val{{font-size:20px;font-weight:700}} .unit{{font-size:12px;margin-left:4px}}
.sub,.note{{font-size:12px;color:#888;display:block}}
.bar{{height:14px;border-radius:7px;background:#2b83f6;color:#fff;font-size:11px}}
</style></head><body><div class="card"><h3>总体培训情况</h3>
<div class="head"><div class="label"></div>
  <div class="cell">集中培训</div><div class="cell">网络自学</div></div>
{prev_row}
<div class="row"><div class="label">今年已训{row_label_suffix}</div>
  {central_cell}
  {cell(online_value, online_note, None)}</div>
<div class="row"><div class="label">完成进度</div>
  <div class="cell"><span class="sub">今年</span><div class="bar">100%</div></div>
  <div class="cell"><span class="sub">今年</span><div class="bar">100%</div></div></div>
<div class="row"><div class="label">其中：基本培训</div>
  <div class="cell">2023-2027年应完成 <b>240</b> 学时
    <div class="bar">32.6%</div></div></div>
</div></body></html>"""


TABLE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:20px;font-family:sans-serif}
table{border-collapse:collapse;width:900px}
th,td{padding:12px 8px;text-align:center}
.note{font-size:12px;color:#888;display:block}
</style></head><body><table>
<tr><th></th><th>集中培训</th><th>网络自学</th></tr>
<tr><td>应训时长</td><td>每年应完成<br>90 学时</td><td>每年应完成<br>50 学时</td></tr>
<tr><td>今年已训</td>
  <td><b>848.05 学时</b><br><span class="note">2023年以来已学习 300 学时</span></td>
  <td><b>51 学时 ›</b></td></tr>
<tr><td>完成进度</td><td>100%</td><td>100%</td></tr>
<tr><td>其中：基本培训</td><td>2023-2027年应完成 <b>240</b> 学时</td><td></td></tr>
</table></body></html>"""


# (用例名, HTML, 期望 (集中培训, 网络自学), 文本兜底层是否也应给出同样结果,
#  期望应训时长 (集中培训, 网络自学)，None 表示该 fixture 没有应训时长行)
DOM_CASES = (
    ("说明行与数值同号",
     _study_center_html("848.05", "2023年以来已学习848.05学时", "51"),
     (848.05, 51.0), True, (90.0, 50.0)),
    ("说明行数字不同且带空格",
     _study_center_html("848.05", "2023年以来已学习 300 学时", "51"),
     (848.05, 51.0), True, (90.0, 50.0)),
    ("说明行数字是独立节点",
     _study_center_html("848.05", None, "51", bare_note_number="300"),
     (848.05, 51.0), True, (90.0, 50.0)),
    ("说明行数字独占一行",
     _study_center_html("848.05", None, "51", note_own_line=True),
     (848.05, 51.0), False, (90.0, 50.0)),  # 只有 DOM 层能靠位置区分
    ("无独立单位节点",
     _study_center_html("848.05", None, "51",
                        value_unit_in_same_element=True),
     (848.05, 51.0), True, (90.0, 50.0)),
    ("缺少上一行标签",
     _study_center_html("848.05", "2023年以来已学习848.05学时", "51",
                        include_prev_row=False),
     (848.05, 51.0), True, None),  # 该 fixture 没有应训时长行
    ("无说明文字（回归）",
     _study_center_html("242", None, "14.25"), (242.0, 14.25), True, (90.0, 50.0)),
    ("说明行在网络自学列",
     _study_center_html("242", None, "51",
                        online_note="2023年以来已学习51学时"),
     (242.0, 51.0), True, (90.0, 50.0)),
    ("小数",
     _study_center_html("1234.5", "2023年以来已学习1234.5学时", "0.75"),
     (1234.5, 0.75), True, (90.0, 50.0)),
    ("行标签带小尾巴",
     _study_center_html("848.05", "2023年以来已学习848.05学时", "51",
                        row_label_suffix=" ›"),
     (848.05, 51.0), True, (90.0, 50.0)),
    ("table 布局", TABLE_HTML, (848.05, 51.0), True, (90.0, 50.0)),
)


class StudyHoursDomParseTests(unittest.TestCase):
    """在真实 Chromium 里渲染同构卡片，验证 DOM 层按行列取值。"""

    def setUp(self):
        patcher = patch("main.debug")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dom_layer_ignores_annotation_text(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright 未安装")

        results = []

        async def run():
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch()
                except Exception as exc:  # pragma: no cover
                    return f"skip:{exc}"
                try:
                    page = await browser.new_page(
                        viewport={"width": 1200, "height": 900})
                    for case in DOM_CASES:
                        name, html, want, _text_agrees, _want_required = case
                        await page.set_content(html)
                        dom = await page.evaluate(_STUDY_HOURS_DOM_JS)
                        text = await page.locator("body").inner_text()
                        resolved = resolve_study_hours(dom, text)
                        results.append((case, dom, resolved, text))
                finally:
                    await browser.close()
            return None

        skip_reason = asyncio.run(run())
        if skip_reason:
            self.skipTest(skip_reason)
        self.assertEqual(len(results), len(DOM_CASES), "浏览器用例未全部执行")

        for (name, _html, want, text_agrees, want_required), dom, resolved, text \
                in results:
            with self.subTest(case=name):
                self.assertEqual((resolved["central"], resolved["online"]), want)
                self.assertEqual((dom["central"], dom["online"]), want)
                if want_required is not None:
                    self.assertEqual(
                        (resolved["required_central"], resolved["required_online"]),
                        want_required)
                # 文本兜底层在多数场景也应一致；个别场景（说明行独占一行）
                # 只有 DOM 层能靠位置区分，此时不要求文本层一致。
                text_only = parse_study_hours_text(text)
                if text_agrees:
                    self.assertEqual(
                        (text_only["central"], text_only["online"]), want)
                else:
                    self.assertIsNotNone(text_only["central"])

    def test_embedded_js_is_syntactically_valid(self):
        node = shutil.which("node")
        if not node:  # pragma: no cover
            self.skipTest("node 未安装")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("const studyHours = " + _STUDY_HOURS_DOM_JS + ";\n")
            proc = subprocess.run([node, "--check", path],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)



if __name__ == "__main__":
    unittest.main()
