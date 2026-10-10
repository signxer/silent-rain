#!/usr/bin/env python3
import asyncio
import json
import inspect
import os
import platform
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import parse_qs, urljoin, urlsplit
from datetime import datetime
from typing import List, Dict, Optional, Union

# Windows需要ProactorEventLoop才能支持subprocess等
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 跨平台快捷键
_SELECT_ALL = "Meta+a" if platform.system() != "Windows" else "Control+a"

# Windows ANSI转义码支持
if platform.system() == "Windows":
    try:
        import colorama
        colorama.init()
    except ImportError:
        pass

import click
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

load_dotenv()
console = Console()


def _hidden_subprocess_kwargs() -> dict:
    """在 Windows GUI 程序中启动控制台子进程时不显示控制台窗口。"""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _atomic_json_dump(path: str, data: dict) -> None:
    """原子写入 JSON，避免进程中断留下半截状态文件。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".moisten-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _default_browsers_path() -> str:
    """Playwright 默认浏览器缓存目录（与 node 端默认一致）"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "ms-playwright")
    elif sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), "ms-playwright")
    else:
        return os.path.join(os.path.expanduser("~/.cache"), "ms-playwright")


def _dir_file_sizes(root: str) -> dict:
    """目录内所有文件 {绝对路径: 字节数}（下载进度监控基线）"""
    sizes = {}
    try:
        for _r, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(_r, f)
                try:
                    sizes[p] = os.path.getsize(p)
                except Exception:
                    pass
    except Exception:
        pass
    return sizes


def _dir_growth_bytes(root: str, baseline: dict) -> int:
    """相对基线的增长字节数 = 新增文件 + 已变大文件（用于统计下载量）"""
    total = 0
    try:
        for _r, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(_r, f)
                try:
                    sz = os.path.getsize(p)
                except Exception:
                    continue
                base = baseline.get(p)
                if base is None:
                    total += sz          # 新增文件（下载中的 zip/解压产物）
                elif sz > base:
                    total += sz - base   # 增长部分
    except Exception:
        pass
    return total


class GoalReached(Exception):
    """学习目标已达成，通知上层清理退出"""
    pass


class StopLearning(Exception):
    """用户变更配置请求停止当前学习任务"""
    pass


class OnlineCourseListUnavailable(RuntimeError):
    """课程列表暂时未加载，不应将单门课程直接判为学习失败。"""


ONLINE_COURSE_LIST_CARD_SELECTOR = "a.p-cursor[title]"

# 多 worker 依次启动的间隔（秒）：避免同时打开平台页面；抽成常量便于测试归零
WORKER_STAGGER_SECONDS = 3.0

# 浏览器启动/建上下文/开页的单项超时（秒）。没有超时的话，一旦被安全软件
# 拦截或驱动半死，就会永远卡在「使用系统 Chrome」那一步且毫无提示。
BROWSER_STEP_TIMEOUT_SECONDS = 90

# 学习频道页的专题班入口收割：能从 href / data-* / onclick 静态读出 ID 就不要点击。
# 覆盖站点不同版本的写法：?id= / workshopId= / workshop_id= / /detail/<id>。
# 故意不认 logChannelId：那是「频道」ID，当成专题班 ID 会跳错详情页。
CHANNEL_WORKSHOP_HARVEST_JS = r"""
() => {
  const PATTERN = /(?:[?&](?:id|workshopId|workshop_id)=|\/detail\/|\/myworkshop\/detail\/)([0-9a-zA-Z_-]{8,})/;
  const UUID = /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;
  const IDLIKE = /^[0-9a-zA-Z_-]{8,}$/;
  const ATTRS = ['href', 'data-href', 'data-url', 'data-workshop-id',
                 'data-workshopid', 'data-id', 'onclick'];
  const idOf = (el) => {
    if (!el || !el.getAttribute) return null;
    const isAnchor = el.tagName === 'A' || el.hasAttribute('href');
    for (const name of ATTRS) {
      const value = el.getAttribute(name);
      if (!value) continue;
      const text = String(value);
      let candidate = '';
      const m = text.match(PATTERN);
      if (m) {
        candidate = m[1];
      } else if (name === 'data-workshop-id' || name === 'data-workshopid') {
        if (IDLIKE.test(text.trim())) candidate = text.trim();
      } else if (name === 'data-id' && isAnchor) {
        if (IDLIKE.test(text.trim())) candidate = text.trim();
      } else if (name === 'onclick') {
        const u = text.match(UUID);
        if (u) candidate = u[0];
      }
      if (candidate) return {id: candidate, source: name, raw: text};
    }
    return null;
  };
  const out = [];
  const seen = new Set();
  const push = (hit, title) => {
    if (!hit || seen.has(hit.id)) return false;
    seen.add(hit.id);
    out.push({id: hit.id, title: title, source: hit.source, raw: hit.raw});
    return true;
  };
  // 1) 链接本身带 ID
  for (const a of Array.from(document.querySelectorAll('a'))) {
    const title = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
    if (push(idOf(a), title)) continue;
    // 2) 卡片把 ID 挂在链接里的子元素上（Vue 常见：外层 a 只有 @click）
    const kids = a.querySelectorAll(
        '[href],[data-id],[data-href],[data-url],[data-workshop-id],[data-workshopid],[onclick]');
    for (const child of Array.from(kids)) {
      if (push(idOf(child), title)) break;
    }
  }
  // 3) 非 <a> 的卡片容器
  for (const el of Array.from(document.querySelectorAll(
      '[data-id],[data-href],[data-url],[data-workshop-id],[data-workshopid],[onclick]'))) {
    if (el.closest && el.closest('a')) continue;   // 上面已扫过
    const title = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    push(idOf(el), title);
  }
  return out;
}
"""

# 更聪明的收割：频道页已经把这 24 个内容渲染出来了，说明数据就在页面的
# 组件状态里（卡片是 Vue 组件，点击时才用里面的 id 拼出详情地址）。
# 直接从组件状态里取，一次 evaluate 拿全，省掉"逐张点击 + 等弹窗"的几十秒。
CHANNEL_PAGE_DATA_JS = r"""
() => {
  const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
  const found = new Map();
  let budget = 6000;
  const walk = (value, depth, sink) => {
    if (value == null || depth > 4 || budget <= 0) return;
    if (typeof value === 'string') {
      if (UUID_RE.test(value)) sink.add(value);
      return;
    }
    if (typeof value !== 'object') return;
    budget -= 1;
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 60)) walk(item, depth + 1, sink);
      return;
    }
    for (const key of Object.keys(value)) {
      if (key.charCodeAt(0) === 95 /* _ */) continue;      // __vue__ / _self 这类内部字段
      if (key === '$el' || key === '$parent' || key === 'parent' || key === 'children') continue;
      try { walk(value[key], depth + 1, sink); } catch (err) { /* 忽略 getter 报错 */ }
    }
  };
  const nodes = Array.from(document.querySelectorAll(
      '[class*=card],[class*=item],[class*=list] a,a')).slice(0, 300);
  for (const el of nodes) {
    const inst = el.__vueParentComponent || el.__vue__;
    if (!inst) continue;
    const ids = new Set();
    walk(inst.props, 0, ids);
    walk(inst.setupState, 0, ids);
    walk(inst.data, 0, ids);
    walk(inst.ctx, 0, ids);
    if (!ids.size) continue;
    const title = ((el.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 60);
    for (const id of ids) {
      if (!found.has(id)) found.set(id, title);
    }
  }
  return Array.from(found, ([id, title]) => ({id: id, title: title}));
}
"""

# 频道页里"像内容条目"的链接数量，用来校验快速通道是否取全
CHANNEL_CONTENT_COUNT_JS = r"""
() => {
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const skip = /协议|隐私|免责|声明|公约|取消|同意/;
  return Array.from(document.querySelectorAll('a')).filter((a) => {
    const label = norm(a.innerText || a.textContent);
    return label.length > 5 && !skip.test(label);
  }).length;
}
"""

# 收割一无所获时，把页面链接原样记下来：否则只能靠猜这个页面的链接长什么样。
CHANNEL_ANCHOR_DUMP_JS = r"""
() => {
  const out = [];
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  for (const a of Array.from(document.querySelectorAll('a')).slice(0, 30)) {
    out.push({
      kind: 'a',
      label: clean(a.innerText || a.textContent).slice(0, 40),
      href: (a.getAttribute('href') || '').slice(0, 160),
      attrs: Array.from(a.attributes || []).map((x) => x.name)
          .filter((n) => n.startsWith('data-') || n === 'onclick').join(','),
    });
  }
  // 卡片可能根本不是链接（div/li + @click）：一并列出来，才知道点击该找谁
  for (const el of Array.from(document.querySelectorAll(
      '[class*=card],[class*=item],[class*=course],[class*=channel],[class*=list]')).slice(0, 30)) {
    if (el.closest('a') || el.querySelector('a')) continue;
    const label = clean(el.innerText || el.textContent);
    if (label.length < 4) continue;
    out.push({
      kind: 'card',
      label: label.slice(0, 40),
      href: '',
      attrs: String(el.className || '').slice(0, 80),
    });
  }
  return out;
}
"""

# 翻页校验的等待预算（毫秒）。抽成常量便于测试压到毫秒级。
ONLINE_PAGE_CHANGE_TIMEOUT_MS = 4500      # 点击下一页后等渲染真正生效
ONLINE_PAGE_ROUTE_TIMEOUT_MS = 5000       # 路由跳转后等卡片真正换掉
ONLINE_PAGE_SETTLE_DELAY_MS = 400         # 落地路由确认间隔
ONLINE_PAGE_DEEP_LINK_TIMEOUT_MS = 8000   # 深链跳页后等卡片渲染
# 频道页点击后的等待预算（毫秒）。弹窗要短：卡片多数是同标签页跳转，
# 等满超时会让每次落空都很贵（实测 6 次落空耗了 87 秒）。
CHANNEL_POPUP_WAIT_MS = 2500
# 频道卡片点开后，弹窗先落在 about:blank，真正地址是随后跳转过去的；
# 立刻读只会拿到空地址（实测 id=- → "#"），所以还要等它落到真地址。
CHANNEL_POPUP_NAV_MS = 8000
# 只等"弹窗事件"出现的时间（不等它导航到真地址）——导航统一放到最后并行等。
# 给得宽一些：连点时 SPA 要先请求再 window.open，实测卡得紧会漏掉一部分卡片
CHANNEL_POPUP_EVENT_MS = 5000
# 两次点击之间留一点处理时间，别把 SPA 点懵
CHANNEL_CLICK_GAP_MS = 200
# 点击兜底最多处理多少个候选入口
CHANNEL_CLICK_LIMIT = 60

# 频道页常见的"遮挡层"：文明公约/须知需要先点同意才会显示内容
CHANNEL_GATE_TEXTS = ("同意", "我同意", "接受", "我知道了", "继续访问")
# 判定"还停在登录/公约页"的特征词
CHANNEL_LOGIN_HINTS = ("密码登录", "短信登录", "获取验证码", "验证码登录",
                       "账号登录", "立即登录", "文明公约")
# 点击兜底时跳过明显不是内容入口的链接。
# 注意：工具类链接都是短文案，而内容标题可能很长且天然带「关于」这类词
# （实测「《习近平关于中国式现代化论述》」差点被当成"关于我们"跳过），
# 所以这里只对「短文案」做工具词匹配，长标题一律不当成工具链接。
CHANNEL_SHORT_UTILITY_LABEL = re.compile(
    r"首页|返回|退出|更多|全部|帮助|客服|设置|收藏|分享|刷新|登录|注册|"
    r"验证码|下载|浏览器|导航|取消|同意")
CHANNEL_DOC_LABEL = re.compile(r"《(?:用户服务协议|隐私政策|免责声明|版权声明)》")
CHANNEL_DETAIL_WAIT_MS = 4000

# 列表页可观测状态：卡片指纹 + 分页器高亮页码 + 路由页码。
# 翻页点击后用它校验「页面真的换了」，避免 SPA 没渲染完就采集到上一页。
# 页码只作为旁证：取不到或取值异常时一律忽略，不影响主流程。
ONLINE_COURSE_PAGE_STATE_JS = r"""
(selector) => {
  const ACTIVE = ['active', 'on', 'current', 'selected', 'checked', 'now'];
  const tokens = (el) => ((el.getAttribute('class') || '').toLowerCase()).split(/[\s_\-]+/);
  const readIndex = (el) => {
    if (!el) return null;
    const n = parseInt((el.textContent || '').trim(), 10);
    return (Number.isFinite(n) && n > 0 && n < 10000) ? n : null;
  };
  const nodes = Array.from(document.querySelectorAll(selector));
  const parts = nodes.map((el) => (el.getAttribute('title') || '')
      + '\u0001' + (el.getAttribute('href') || ''));
  let pager = null;
  for (const el of Array.from(document.querySelectorAll('[class*=page_num], [class*=page-num]'))) {
    if (!ACTIVE.some((name) => tokens(el).includes(name))) continue;
    pager = readIndex(el);
    if (pager !== null) break;
  }
  if (pager === null) {
    for (const sel of ['.el-pager li.is-active', '.ant-pagination-item-active',
                       '[class*=pager] [class*=active]']) {
      pager = readIndex(document.querySelector(sel));
      if (pager !== null) break;
    }
  }
  const match = (location.hash || '').match(/\/list\/(\d+)/);
  return {
    raw: parts.join('\u0002'),
    set: parts.slice().sort().join('\u0002'),
    pager: pager,
    route: match ? parseInt(match[1], 10) : null,
  };
}
"""


def _same_document_url(current_url: str, target_url: str) -> bool:
    """判断 goto(target_url) 是否只会做同文档导航（scheme/host/path 相同）。

    网络自学列表 /course/#/list/N 是 hash 路由：同文档内的跳转不会重新加载页面。
    """
    try:
        current, target = urlsplit(current_url or ""), urlsplit(target_url or "")
    except Exception:
        return False
    return ((current.scheme, current.netloc, current.path)
            == (target.scheme, target.netloc, target.path))


def _same_hash_url(current_url: str, target_url: str) -> bool:
    """同文档且 hash 完全一致：浏览器不会派发 hashchange，SPA 不会重新渲染。"""
    try:
        return (_same_document_url(current_url, target_url)
                and urlsplit(current_url or "").fragment == urlsplit(target_url or "").fragment)
    except Exception:
        return False


def _page_url(page) -> str:
    """安全读取页面地址；page 可能是不带 url 的替身对象。"""
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _online_course_target_url(list_url: str, href: str) -> str:
    """仅将同站课程卡片的可导航 href 解析为直达地址。"""
    href = (href or "").strip()
    if not href or href == "#":
        return ""
    target = urljoin(list_url, href)
    base, parsed = urlsplit(list_url), urlsplit(target)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
        return ""
    if (parsed.path, parsed.query, parsed.fragment) == (base.path, base.query, base.fragment):
        return ""
    if parsed.path == base.path and not parsed.fragment:
        return ""
    return target


def _defer_online_course(course_queue: asyncio.Queue, task: dict, max_retries: int = 2) -> bool:
    """列表暂不可用时把课程放回队尾，让 worker 先尝试其他课程。"""
    failures = task.get("list_failures", 0) + 1
    if failures > max_retries:
        return False
    task["list_failures"] = failures
    course_queue.put_nowait(task)
    return True


def _build_online_playlist_tasks(parent: dict, entries: list, done_keys=None) -> list:
    """把同一播放页中的视频目录项转换为可独立排队的任务。"""
    done_keys = set(done_keys or ())
    tasks = []
    seen = set()
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "").strip()
        href = str(entry.get("href") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not video_id or not href or not title:
            continue
        try:
            parent_url = urlsplit(parent.get("href") or href)
            video_url = urlsplit(urljoin(parent.get("href") or href, href))
            route, _, query = video_url.fragment.partition("?")
            params = parse_qs(query)
            if (video_url.scheme not in {"http", "https"}
                    or video_url.netloc.lower() != parent_url.netloc.lower()
                    or not route.startswith("/play/")
                    or params.get("pKnowledgeId", [""])[0] != video_id
                    or not params.get("cid")):
                continue
        except (TypeError, ValueError):
            continue
        key = f"{parent.get('key') or parent.get('href') or parent.get('title', '')}::video:{video_id}"
        if key in seen:
            continue
        seen.add(key)
        if key in done_keys:
            continue
        tasks.append({
            "page": parent.get("page", 1),
            "title": f"{parent.get('title', '')} · {title}"[:120],
            "href": href,
            "key": key,
            "playlist_child": True,
            "parent_title": parent.get("title", ""),
            "video_title": title,
            "video_id": video_id,
        })
    return tasks


def _kill_playwright_chrome():
    """清理Playwright残留的Chrome进程（不影响用户自己的浏览器）"""
    try:
        if sys.platform == "win32":
            # 用PowerShell只杀带--remote-debugging的Chrome（Playwright启动的）
            subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
                 "| Where-Object {$_.CommandLine -match '--remote-debugging'} "
                 "| Stop-Process -Force"],
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5,
                **_hidden_subprocess_kwargs())
        else:
            subprocess.run(["pkill", "-f", "chrome-headless-shell"],
                          stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
            subprocess.run(["pkill", "-f", "chromium.*--remote-debugging"],
                          stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
    except:
        pass


# 存储文件路径
# 源码运行：脚本所在目录；PyInstaller 冻结：每用户数据目录
# （避免写入 Program Files / .app 包内导致写入失败、升级丢数据）
if getattr(sys, 'frozen', False):
    if sys.platform == "win32":
        _BASE_DIR = os.path.join(os.environ.get("APPDATA", os.path.dirname(sys.executable)), "Moisten")
    elif sys.platform == "darwin":
        _BASE_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "Moisten")
    else:
        _BASE_DIR = os.path.join(os.path.expanduser("~/.config"), "moisten")
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    os.makedirs(_BASE_DIR, exist_ok=True)
except Exception:
    pass

# 打包版把调试日志放到固定的用户目录：DEBUG_LOG 原本是相对路径，写的是「当前工作
# 目录」。用户从 Finder 启动 .app 时 CWD 是 /，写不进去，异常又被吞掉——于是自动
# 更新重启之后日志会整个消失，排查时什么都拿不到。
if getattr(sys, 'frozen', False):
    DEBUG_LOG = os.path.join(_BASE_DIR, "moisten_debug.log")

STORAGE_STATE_PATH = os.path.join(_BASE_DIR, "moisten_session.json")
USER_CREDENTIALS_PATH = os.path.join(_BASE_DIR, "moisten_credentials.json")
TAGS_STATE_PATH = os.path.join(_BASE_DIR, "moisten_tags.json")
CONFIG_PATH = os.path.join(_BASE_DIR, "moisten_config.json")
PROGRESS_PATH = os.path.join(_BASE_DIR, "moisten_progress.json")


def safe_print(text, style=None):
    """安全的打印，避免Rich Markup错误"""
    try:
        if style:
            console.print(text, style=style)
        else:
            console.print(text)
    except Exception:
        # 如果Rich解析失败时，直接打印
        print(text)


DEBUG_LOG = "moisten_debug.log"
_DEBUG_LOG_LOCK = threading.Lock()

def init_debug_log(version: str = ""):
    """写一条运行标记。

    带上版本号与运行形态：没有这些信息时，拿到日志也无法判断用户跑的是哪一版、
    是源码还是打包版，排查只能靠猜（每次都要多问一轮）。
    """
    try:
        tag = f" v{version}" if version else " v?"
        frozen = "frozen" if getattr(sys, "frozen", False) else "source"
        with _DEBUG_LOG_LOCK, open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n# 日志文件: {os.path.abspath(DEBUG_LOG)}\n")
        with _DEBUG_LOG_LOCK, open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== Moisten Debug Run{tag} | started "
                    f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                    f"| {platform.system()} {platform.release()} "
                    f"| python {platform.python_version()} | {frozen} ===\n")
    except:
        pass

def debug(msg: str):
    # Timestamp every event; keep this file free of page bodies and credentials.
    try:
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with _DEBUG_LOG_LOCK, open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {msg}\n")
    except:
        pass

def _safe_debug_url(url: str) -> str:
    """Keep only origin/path and a generic SPA route; never log query/hash secrets."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        route = ""
        segments = [part for part in parts.fragment.split("?")[0].split("/") if part]
        if segments:
            route = f"#/{segments[0]}"
        return f"{parts.scheme}://{host}{parts.path}{route}"
    except Exception:
        return "<url-unavailable>"

def _safe_debug_error(error) -> str:
    """Strip URL query/hash material from Playwright exceptions before logging."""
    try:
        return re.sub(
            r"https?://[^\s\]\[()'\"]+",
            lambda match: _safe_debug_url(match.group(0).rstrip(".,;:")),
            str(error),
        )
    except Exception:
        return type(error).__name__

def _page_debug_state(page) -> str:
    """Return a small, credential-free snapshot for browser lifecycle diagnostics."""
    if page is None:
        return "page=none"
    try:
        return f"closed={page.is_closed()} url={_safe_debug_url(page.url)}"
    except Exception as exc:
        return f"state-unavailable={type(exc).__name__}"

# === 常驻 stdin 读取线程 ===
# 单一线程读 stdin，通过 queue 分发给各 async_input 调用，
# 彻底避免多线程竞争 stdin 导致输入丢失。
_stdin_line_q = queue.Queue()


def _stdin_reader_thread():
    """常驻后台线程：持续读取 stdin 每一行，放入队列（保留原始输入）"""
    while True:
        try:
            line = input()
            _stdin_line_q.put(line.strip())
        except EOFError:
            break
        except Exception:
            break


# 仅CLI模式启动stdin读取线程，GUI导入时不应启动（避免与PyQt5竞争stdin）
if __name__ == "__main__" or "main" in sys.argv[0]:
    _stdin_thread = threading.Thread(target=_stdin_reader_thread, daemon=True)
    _stdin_thread.start()


async def async_input(prompt: str, default: str = "y", timeout: int = 5,
                       block: bool = False, raw: bool = False, password: bool = False) -> str:
    """带超时的输入，从常驻 stdin 队列取数据。

    Args:
        prompt: 提示文字
        default: 超时后的默认值
        timeout: 超时秒数
        block: True=无限等待（用于"按回车键继续"类场景）
        raw: True=不 strip/lowercase（用于用户名等需要保留原始输入的场景）
        password: True=输入后清除行（密码输入用）
    """
    if block:
        console.print(prompt, style="yellow", end="")
    else:
        console.print(f"{prompt}（{timeout}秒后自动: {default}）", style="yellow", end="")

    try:
        if block:
            line = await asyncio.get_event_loop().run_in_executor(
                None, _stdin_line_q.get)
        else:
            line = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _stdin_line_q.get(timeout=timeout))
        # 密码模式：清除刚输入的那一行
        if password:
            sys.stdout.write("\033[A\033[K")  # 上移一行并清除
            sys.stdout.flush()
        return line if raw else (line.strip().lower() if line else default)
    except queue.Empty:
        console.print(f"\n[超时，自动: {default}]", style="yellow")
        return default


# ─── DeepSeek 自动答题 ──────────────────────────────────────────────
#
# 训练营课程页里的「随堂测试」是一个 cuWebExam 组件，点击「开始考试」会打开
# OTE 考试中心（/ote/#/exampreview?examArrangeID=...），再由页面自身跳到
# 答题页（/ote/#/userexam?arrangeId=...&userExamMapId=...）。
# 答题页把题目挂在 Vue 实例上（questionsList），提交走两个接口：
#   POST {api}/ote/user/logAnswers/{userExamId}
#   POST {api}/ote/web/userexam/{userExamId}/submit?arrangeId=&userExamMapId=
# 因此本模块只负责：读题 → 问 DeepSeek → 回填并提交，全部复用页面自身的登录态。

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
# OTE 接口域名（页面用 axios 实例固定指向 api.u.ccb.com/v1/）
OTE_API_BASE = "https://api.u.ccb.com/v1"

# 训练营视频学习节奏：轮询间隔 / 本地播完后等待平台结算的时间
TRAINCAMP_POLL_SECONDS = 10
TRAINCAMP_LOCAL_SETTLE_SECONDS = 180

# 考试没考成/没通过时，最多允许用户手动选择重考的次数
EXAM_RETRY_LIMIT = 3

# 交卷节奏：AI 答题几乎是瞬间完成的，直接交卷会显得异常。
# 每题模拟耗时取 [min, max] 区间内的一个随机值，交卷前等待「题量 × 每题耗时」秒。
EXAM_DELAY_MIN_DEFAULT = 10.0
EXAM_DELAY_MAX_DEFAULT = 20.0
# 单题耗时上限：防止误填（例如 1000）导致一次考试等待数小时
EXAM_DELAY_PER_QUESTION_LIMIT = 600.0


def _exam_delay_bounds(delay_min, delay_max) -> tuple:
    """规范化每题的模拟耗时区间（秒）。

    非法值/负数回退到默认，超出上限则夹紧；min > max 时交换，保证区间可用。
    """
    def _coerce(value, fallback):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        if number != number or number < 0:      # NaN / 负数
            return fallback
        return min(number, EXAM_DELAY_PER_QUESTION_LIMIT)

    low = _coerce(delay_min, EXAM_DELAY_MIN_DEFAULT)
    high = _coerce(delay_max, EXAM_DELAY_MAX_DEFAULT)
    if low > high:
        low, high = high, low
    return low, high


def _exam_delay_plan(question_count: int, delay_min, delay_max, rng=None) -> tuple:
    """按题量算出本题交卷前的模拟答题时长，返回 (每题秒数, 总秒数)。

    每题耗时在区间内随机取一次，整场考试共用该值；题量为 0 或区间为 0 时不延时。
    """
    try:
        questions = max(0, int(question_count or 0))
    except (TypeError, ValueError):
        questions = 0
    low, high = _exam_delay_bounds(delay_min, delay_max)
    if questions <= 0 or high <= 0:
        return 0.0, 0.0
    per_question = (rng or random).uniform(low, high)
    return per_question, per_question * questions


def _exam_needs_retake_choice(result: Optional[Dict]) -> bool:
    """这次考试结果是否需要问用户「要不要重考」。

    已通过、已交卷待批阅都算正常结果，不再打扰用户；
    未通过、答题/提交异常、以及不可考试（过期/仅手机扫码等）都问一次。
    """
    result = result or {}
    status = result.get("status")
    detail = result.get("detail") or ""
    if status in ("failed", "error"):
        return True
    if status == "skipped":
        return not any(key in detail for key in ("已通过", "待批阅"))
    return False

EXAM_SYSTEM_PROMPT = """你是中国建设银行在线学习平台的考试答题助手，需要判断题目的正确答案。
只输出一个 JSON 对象（json），不要输出解释、不要包裹代码块。

输出格式示例：
{"answers":[{"index":1,"choices":["A"]},{"index":2,"choices":["A","C"]},{"index":3,"blanks":["答案文本","第二个空"]},{"index":4,"text":"问答题的作答内容"}]}

规则：
1. 单选题用 choices，只包含 1 个选项字母。
2. 多选题用 choices，包含所有正确选项字母。
3. 判断题用 choices，只包含 1 个选项字母（按题目给出的选项字母作答，A/B 即为该题的两个选项）。
4. 填空题用 blanks，按空的顺序给出每个空的答案文本。
5. 问答题用 text，给出条理清晰的作答内容（可含要点，200 字以内）。
6. 每道题都必须给出答案，index 必须与题目编号一致，不能遗漏或新增题目。
7. 若某题信息不足，也要按最可能的正确答案作答。"""


class DeepSeekError(Exception):
    """DeepSeek 接口调用失败"""


# 配置里的密钥做一层混淆（与账号密码同一套 XOR+base64），避免明文落盘
_SECRET_PREFIX = "enc:"


def obfuscate_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _SECRET_PREFIX + AutoLearner._xor_crypt(value)
    except Exception:
        return value


def deobfuscate_secret(value: str) -> str:
    if not value:
        return ""
    if isinstance(value, str) and value.startswith(_SECRET_PREFIX):
        try:
            return AutoLearner._xor_decrypt(value[len(_SECRET_PREFIX):])
        except Exception:
            return ""
    return value


def exam_settings_from_config(cfg: Dict) -> Dict:
    """从 moisten_config.json 里取出考试答题相关设置（密钥自动解密）。"""
    cfg = cfg or {}
    delay_min, delay_max = _exam_delay_bounds(cfg.get("exam_delay_min"),
                                              cfg.get("exam_delay_max"))
    return {
        "exam_enabled": bool(cfg.get("exam_enabled", False)),
        "deepseek_api_key": deobfuscate_secret(cfg.get("deepseek_api_key", "")),
        "deepseek_model": cfg.get("deepseek_model", "") or DEEPSEEK_DEFAULT_MODEL,
        "deepseek_base_url": cfg.get("deepseek_base_url", "") or DEEPSEEK_DEFAULT_BASE_URL,
        "deepseek_thinking": bool(cfg.get("deepseek_thinking", False)),
        "exam_delay_min": delay_min,
        "exam_delay_max": delay_max,
    }
def _component_finished(component: Optional[Dict]) -> bool:
    """训练营组件是否已被平台记录完成（进度到达阈值，或带完成标记）"""
    if not component:
        return False
    try:
        pct = float(component.get("platformPct") or 0)
        threshold = float(component.get("threshold") or 95)
    except (TypeError, ValueError):
        return bool(component.get("finishedFlag"))
    return pct >= threshold or bool(component.get("finishedFlag"))


def _exam_max_tokens(question_count: int, thinking: bool) -> int:
    """按题量估算答题输出预算。

    思考模式下思维链也算进 max_tokens，所以必须额外留出推理预算，
    否则容易出现「推理写完、JSON 被截断/为空」。
    """
    questions = max(1, int(question_count or 0))
    budget = 1024 + 320 * questions          # JSON 答案本身
    if thinking:
        budget += 2048 + 1200 * min(questions, 30)   # 思维链预算
    return max(2048, min(65536, budget))


class DeepSeekClient:
    """DeepSeek Chat Completions 客户端（OpenAI 兼容，仅用标准库实现）。"""

    def __init__(self, api_key: str, model: str = DEEPSEEK_DEFAULT_MODEL,
                 base_url: str = DEEPSEEK_DEFAULT_BASE_URL,
                 thinking: bool = False, timeout: float = 180.0,
                 reasoning_effort: str = "high"):
        self.api_key = (api_key or "").strip()
        self.model = (model or DEEPSEEK_DEFAULT_MODEL).strip() or DEEPSEEK_DEFAULT_MODEL
        self.base_url = (base_url or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")
        self.thinking = bool(thinking)
        # deepseek-flash「思考模式默认打开且 effort 默认 high」，所以开关必须显式传：
        # 关闭时传 disabled（否则省不下时间与 token），打开时再给推理强度。
        self.reasoning_effort = reasoning_effort if reasoning_effort in ("low", "high", "max") else "high"
        self.timeout = timeout

    def _build_payload(self, messages: List[Dict], json_mode: bool, max_tokens: int) -> Dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
        }
        if self.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, path: str, payload: dict) -> dict:
        """同步 POST（在线程池里跑，避免阻塞事件循环）"""
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise DeepSeekError(f"HTTP {e.code}: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise DeepSeekError(f"网络错误: {e.reason}") from e

    async def chat(self, messages: List[Dict], json_mode: bool = True,
                   max_tokens: int = 8192, retries: int = 3) -> str:
        """调用 /chat/completions，返回首条回复文本；失败抛出 DeepSeekError。

        注意：思考模式下思维链（reasoning_content）同样计入 max_tokens，
        预算不足会出现「有思维链但 content 为空、finish_reason=length」的情况，
        这里会自动放宽预算重试一次。
        """
        if not self.api_key:
            raise DeepSeekError("未配置 DeepSeek API Key")

        budget = max(64, int(max_tokens))
        loop = asyncio.get_event_loop()
        last_err = None
        attempt = 0
        while attempt < max(1, retries):
            attempt += 1
            payload = self._build_payload(messages, json_mode, budget)
            try:
                data = await loop.run_in_executor(None, self._post, "/chat/completions", payload)
            except DeepSeekError as e:
                last_err = e
                # 400/401/403 属于配置错误，重试无意义
                msg = str(e)
                if msg.startswith("HTTP 4") and "429" not in msg:
                    raise
                await asyncio.sleep(1.5 * attempt)
                continue
            except Exception as e:
                last_err = DeepSeekError(str(e))
                await asyncio.sleep(1.5 * attempt)
                continue

            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            reasoning = message.get("reasoning_content") or ""
            finish = choice.get("finish_reason") or ""
            usage = data.get("usage") or {}
            reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

            if content.strip():
                return content

            if finish == "length":
                last_err = DeepSeekError(
                    f"输出被 max_tokens={budget} 截断（推理占用 {reasoning_tokens or 0} tokens）")
                # 思维链吃满预算：放宽后再试一次
                budget = min(65536, budget * 4)
            elif reasoning:
                last_err = DeepSeekError("只返回了思维链，没有最终答案")
            else:
                last_err = DeepSeekError("接口返回空内容")
            await asyncio.sleep(1.5 * attempt)
        raise last_err or DeepSeekError("调用失败")

    async def answer_exam(self, questions: List[Dict], log=None) -> Dict[int, Dict]:
        """把题目交给 DeepSeek，返回 {题目编号: {"choices": [...], "blanks": [...], "text": "..."}}"""
        _log = log or (lambda msg, style="": None)
        if not questions:
            return {}

        user_payload = {"questions": questions}
        user_prompt = (
            "请作答以下试题，并按要求输出 JSON（json）：\n"
            + json.dumps(user_payload, ensure_ascii=False, indent=1)
        )
        messages = [
            {"role": "system", "content": EXAM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        last_err = None
        for attempt in range(2):
            budget = _exam_max_tokens(len(questions), self.thinking)
            raw = await self.chat(messages, json_mode=True, max_tokens=budget)
            parsed = _parse_exam_json(raw)
            if parsed:
                return parsed
            last_err = DeepSeekError("返回内容无法解析为 JSON")
            messages = messages + [
                {"role": "assistant", "content": raw[:2000]},
                {"role": "user", "content": "上一次输出不是合法 JSON。请只输出 JSON 对象，"
                                           "格式：{\"answers\":[{\"index\":1,\"choices\":[\"A\"]}]}"},
            ]
        raise last_err or DeepSeekError("答题结果解析失败")


def _parse_exam_json(raw: str) -> Dict[int, Dict]:
    """从模型输出里提取 {题目编号: 答案}，兼容代码块/多余文字。"""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return {}
    items = data.get("answers") if isinstance(data, dict) else None
    if items is None and isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return {}

    result: Dict[int, Dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        choices = item.get("choices") or item.get("choice") or []
        if isinstance(choices, str):
            choices = [c for c in re.split(r"[,，、\s]+", choices) if c]
        choices = [str(c).strip().upper() for c in choices if str(c).strip()]
        blanks = item.get("blanks") or []
        if isinstance(blanks, str):
            blanks = [blanks]
        blanks = [str(b).strip() for b in blanks]
        text = item.get("text") or item.get("answer") or ""
        if isinstance(text, list):
            text = " ".join(str(t) for t in text)
        result[index] = {"choices": choices, "blanks": blanks, "text": str(text).strip()}
    return result


# ───────────────────────── 学习中心学时解析 ─────────────────────────
# 学习中心「总体培训情况」是一张两列表格：列为 集中培训 / 网络自学，
# 行为 应训时长 / 今年已训 / 完成进度。部分用户的「今年已训」单元格里
# 会多出一行说明文字（如「2023年以来已学习xxxx学时」），旧实现直接取
# 第 1、2 个「数字+学时」，于是把说明里的数字当成了网络自学学时。
#
# 现在的解析分两层：
#   1) DOM 结构化解析（_STUDY_HOURS_DOM_JS）：按 今年已训 行的 y 范围取
#      两列的真实数值节点，再用列头 x 坐标归属到 集中培训 / 网络自学。
#   2) 文本兜底解析（parse_study_hours_text）：按行打分，只有「整行就是
#      数值」「数值在中文之前」才算候选，从而剔除说明句里的数字。
# 任一层拿到两个值即可；另一层用于补齐缺失的一列。

STUDY_HOURS_CENTRAL_LABEL = "集中培训"
STUDY_HOURS_ONLINE_LABEL = "网络自学"
STUDY_HOURS_ROW_LABELS = ("今年已训", "今年已培训", "本年已训")
# 「应训时长」行：每年应完成的学时要求（集中培训 / 网络自学各一列）
STUDY_HOURS_REQUIRED_ROW_LABELS = ("应训时长", "应训学时", "应训标准")
STUDY_HOURS_PREV_ROW_LABELS = STUDY_HOURS_REQUIRED_ROW_LABELS + ("集中培训学时",)
STUDY_HOURS_ROW_END_LABELS = ("完成进度", "其中：基本培训", "其中:基本培训")
# 文本兜底扫描时，区域再往后的结束标记（防止把指标说明里的数字算进来）
STUDY_HOURS_TEXT_END_LABELS = STUDY_HOURS_ROW_END_LABELS + (
    "学时指标", "培训项目", "我的必学")

_HOURS_STRICT_LINE_RE = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:学时|小时)?\s*[>›»→]?\s*$')
_HOURS_TOKEN_RE = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*(?:学时|小时)')
_HOURS_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
# Playwright 选择器：页面上出现「数字+学时/小时」即认为学时数据已绑定
_HOURS_VALUE_SELECTOR = r"text=/[0-9]+(?:\.[0-9]+)?\s*(?:学时|小时)/"

# 页面内取数：返回 {"central": float|None, "online": float|None, "debug": {...}}
# __XXX__ 占位符由下方 json.dumps 注入，保证 JS 与 Python 用同一份标签常量。
_STUDY_HOURS_DOM_JS_TEMPLATE = r"""
() => {
  const ROW_LABELS = __ROW_LABELS__;
  const REQUIRED_ROW_LABELS = __REQUIRED_ROW_LABELS__;
  const PREV_ROW_LABELS = __PREV_ROW_LABELS__;
  const ROW_END_LABELS = __ROW_END_LABELS__;
  const CENTRAL_LABEL = __CENTRAL_LABEL__;
  const ONLINE_LABEL = __ONLINE_LABEL__;
  const norm = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const VALUE_RE = /^([0-9]{1,7}(?:\.[0-9]{1,3})?)\s*(学时|小时)?\s*[>›»→]?$/;
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2, top: r.top, bottom: r.bottom,
            left: r.left, right: r.right, w: r.width, h: r.height};
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity || '1') > 0.05;
  };
  const nodes = Array.from(document.querySelectorAll('body *'));
  const texts = nodes.map((el) => norm(el.textContent));
  // 标签允许带极短后缀（如「今年已训 ›」），但不能是整行拼接文本
  const isLabel = (text, label) => text === label
      || (text.length > label.length && text.length <= label.length + 2
          && text.indexOf(label) === 0);
  const pickLabel = (label, aboveY) => {
    let best = null;
    for (let i = 0; i < nodes.length; i++) {
      if (!isLabel(texts[i], label)) continue;
      if (!visible(nodes[i])) continue;
      const c = rect(nodes[i]);
      if (aboveY !== undefined && aboveY !== null && c.y >= aboveY) continue;
      // 同名嵌套节点取最靠下的那个（离目标行最近）
      if (!best || c.y > best.y) best = c;
    }
    return best;
  };
  // 读取「某一行」在两个列上的数值。应训时长行与今年已训行共用同一套
  // 行/列定位逻辑，避免两处各写一份、各自漂移。
  const readRow = (labels, prevLabels, endLabels) => {
    const info = {central: null, online: null, debug: {}};
    // 1) 行标签
    let rowLabel = null, rowName = null;
    for (const name of labels) {
      rowLabel = pickLabel(name, null);
      if (rowLabel) { rowName = name; break; }
    }
    if (!rowLabel) { info.debug.error = 'no-row-label'; return info; }
    info.debug.row = rowName;
    // 2) 列头：位于行标签上方、离得最近的那组
    const centralHeader = pickLabel(CENTRAL_LABEL, rowLabel.y);
    const onlineHeader = pickLabel(ONLINE_LABEL, rowLabel.y);
    if (!centralHeader || !onlineHeader) { info.debug.error = 'no-column-header'; return info; }
    // 3) 行范围：上边界取「上一行标签」与本行的中点（避免吃进上一行的数值）；
    //    本行就是第一行数据（没有上一行标签）时，用列头底边当上边界。
    let prevLabelY = null;
    for (const name of prevLabels) {
      for (let i = 0; i < nodes.length; i++) {
        if (!isLabel(texts[i], name) || !visible(nodes[i])) continue;
        const c = rect(nodes[i]);
        if (c.y >= rowLabel.y - 4) continue;
        if (prevLabelY === null || c.y > prevLabelY) prevLabelY = c.y;
      }
    }
    const halfRow = rowLabel.h / 2;
    let rowTop;
    if (prevLabels.length === 0) {
      rowTop = Math.min(centralHeader.bottom, onlineHeader.bottom) + 2;
    } else if (prevLabelY === null) {
      rowTop = rowLabel.y - Math.max(halfRow, 8);
    } else {
      rowTop = Math.max((prevLabelY + rowLabel.y) / 2, rowLabel.y - halfRow - 4);
    }
    let rowBottom = rowLabel.bottom + 140;
    for (const name of endLabels) {
      let below = null;
      for (let i = 0; i < nodes.length; i++) {
        if (!isLabel(texts[i], name) || !visible(nodes[i])) continue;
        const c = rect(nodes[i]);
        if (c.y <= rowLabel.y + 4) continue;
        if (!below || c.y < below.y) below = c;
      }
      if (below) { rowBottom = Math.min(rowBottom, below.top); break; }
    }
    // 4) 行内数值节点（整段文本就是一个数字[+学时]）
    const values = [];
    for (let i = 0; i < nodes.length; i++) {
      const t = texts[i];
      if (!t) continue;
      const m = VALUE_RE.exec(t);
      if (!m) continue;
      if (!visible(nodes[i])) continue;
      const c = rect(nodes[i]);
      if (c.y < rowTop || c.y > rowBottom) continue;
      const num = parseFloat(m[1]);
      if (!isFinite(num)) continue;
      values.push({num: num, unit: !!m[2], x: c.x, y: c.y, text: t, area: c.w * c.h});
    }
    info.debug.rowTop = Math.round(rowTop);
    info.debug.rowBottom = Math.round(rowBottom);
    info.debug.candidates = values.map((v) => ({t: v.text, x: Math.round(v.x), y: Math.round(v.y)}));
    info.debug.headers = {central: Math.round(centralHeader.x), online: Math.round(onlineHeader.x)};
    // 5) 按列归属；同列优先「带学时单位」、其次离本行中线最近、再其次面积最小
    const pickColumn = (header, other) => {
      const pool = values.filter((v) => Math.abs(v.x - header.x) <= Math.abs(v.x - other.x));
      if (!pool.length) return null;
      pool.sort((a, b) => (b.unit - a.unit)
          || (Math.abs(a.y - rowLabel.y) - Math.abs(b.y - rowLabel.y))
          || (a.area - b.area));
      return pool[0];
    };
    const c = pickColumn(centralHeader, onlineHeader);
    const o = pickColumn(onlineHeader, centralHeader);
    if (c) info.central = c.num;
    if (o) info.online = o.num;
    info.debug.picked = {central: c ? c.text : null, online: o ? o.text : null};
    return info;
  };

  // 今年已训（上一行是应训时长）/ 应训时长（位于列头下方第一行）
  const actual = readRow(ROW_LABELS, PREV_ROW_LABELS, ROW_END_LABELS);
  const required = readRow(REQUIRED_ROW_LABELS, [], ROW_LABELS);
  return {
    central: actual.central,
    online: actual.online,
    required_central: required.central,
    required_online: required.online,
    debug: {actual: actual.debug, required: required.debug},
  };
}
"""


def _uuids_from_json_text(text: str, exclude_id: str = "") -> List[str]:
    """从接口返回的 JSON 文本里取内容 ID。

    只看 id 类字段（id / dataStoreId / workshopId ...），不扫载荷里的任意 UUID——
    否则频道名、图片名里的 UUID 都会被当成内容。
    """
    ids = []
    seen = set()
    for match in re.findall(r'"[A-Za-z]{0,20}[Ii]d"\s*:\s*"([0-9a-fA-F-]{36})"',
                            text or ""):
        if match == exclude_id or match in seen:
            continue
        seen.add(match)
        ids.append(match)
    return ids


def _channel_id_from_url(channel_url: str) -> str:
    """频道页地址末段的频道 ID（用来把它从收割结果里排掉）。"""
    try:
        parts = urlsplit(str(channel_url))
    except Exception:
        return ""
    fragment = parts.fragment or parts.path
    tail = fragment.rstrip("/").rsplit("/", 1)[-1]
    return tail if re.fullmatch(r"[0-9a-fA-F-]{8,}", tail or "") else ""


def _progress_completed(progress) -> bool:
    """进度文本是否表示已学完（"100%" / "100" 都算）。"""
    try:
        return float(str(progress).replace("%", "").strip()) >= 100
    except Exception:
        return False


def _debug_url_shape(url: str) -> str:
    """日志里记录 URL 形状：保留路由与参数名，丢掉参数值（避免把 token 写进日志）。"""
    try:
        parts = urlsplit(url or "")
        route, _sep, query = parts.fragment.partition("?")
        names = ",".join(sorted(parse_qs(query).keys())) if query else ""
        return f"{parts.netloc}{parts.path}#{route}" + (f"?[{names}]" if names else "")
    except Exception:
        return ""


WORKSHOP_COURSE_TABLE_JS = r"""() => {
                const norm = (el) => (el && el.innerText ? el.innerText : '')
                    .replace(/\s+/g, ' ').trim();
                const table = document.querySelector('table.courseList-table')
                    || document.querySelector('table');
                if (!table) return {rows: [], headers: []};
                const headers = Array.from(
                    table.querySelectorAll('tr.header th, thead th')).map(norm);
                const findIdx = (keywords, fallback) => {
                    for (let i = 0; i < headers.length; i++) {
                        if (keywords.some((k) => headers[i].indexOf(k) >= 0)) return i;
                    }
                    return fallback;
                };
                const idx = {
                    type: findIdx(['类型'], 0),
                    title: findIdx(['标题', '课程'], 1),
                    required: findIdx(['必'], 2),
                    hours: findIdx(['学时'], 3),
                    progress: findIdx(['进度'], 4),
                    action: findIdx(['操作'], 5),
                };
                const rows = [];
                const seen = new Set();
                const push = (tr) => {
                    if (!tr || seen.has(tr)) return;
                    const cells = Array.from(tr.querySelectorAll('td'));
                    if (!cells.length) return;
                    const cellText = (i) => (i >= 0 && i < cells.length) ? norm(cells[i]) : '';
                    const title = cellText(idx.title) || cellText(1);
                    if (!title) return;
                    seen.add(tr);
                    const typeCell = cells[idx.type] ? cells[idx.type].querySelector('.course-type') : null;
                    const pct = cells[idx.progress] ? cells[idx.progress].querySelector('.percent-text') : null;
                    const actionSpan = cells[idx.action] ? cells[idx.action].querySelector('.edit-block') : null;
                    const titleCell = cells[idx.title] || tr;
                    const link = titleCell.querySelector('a[href]') || tr.querySelector('a[href*="course"]');
                    const href = link ? link.getAttribute('href') : '';
                    const dataId = tr.getAttribute('data-id') || tr.getAttribute('data-course-id') || '';
                    rows.push({
                        type: typeCell ? norm(typeCell) : cellText(idx.type),
                        title: title,
                        required: cellText(idx.required),
                        hours: cellText(idx.hours),
                        progress: pct ? norm(pct) : cellText(idx.progress),
                        action: actionSpan ? norm(actionSpan) : cellText(idx.action),
                        url: href || dataId || ''
                    });
                };
                table.querySelectorAll('tbody tr').forEach(push);
                document.querySelectorAll('tr.text-center').forEach(push);
                return {rows: rows, headers: headers};
            }"""


def _manual_progress_payload(done: int, total: int, status: str = "") -> dict:
    """手动模式的「总体进度」载荷：已完成工作项 / 总工作项。

    手动模式没有学时目标，进度只能按"要学的东西学完了多少"来算。载荷只带
    manual_* 字段、不带 wid，GUI 据此与单个 worker 的行进度区分开，不会互相覆盖。
    """
    payload = {"manual_done": max(0, int(done)), "manual_total": max(0, int(total))}
    if status:
        payload["manual_status"] = status
    return payload


def _render_study_hours_dom_js() -> str:
    """把 Python 侧的标签常量注入 JS 模板（json.dumps 保证转义安全）。"""
    js = _STUDY_HOURS_DOM_JS_TEMPLATE
    replacements = {
        "__ROW_LABELS__": list(STUDY_HOURS_ROW_LABELS),
        "__REQUIRED_ROW_LABELS__": list(STUDY_HOURS_REQUIRED_ROW_LABELS),
        "__PREV_ROW_LABELS__": list(STUDY_HOURS_PREV_ROW_LABELS),
        "__ROW_END_LABELS__": list(STUDY_HOURS_ROW_END_LABELS),
        "__CENTRAL_LABEL__": STUDY_HOURS_CENTRAL_LABEL,
        "__ONLINE_LABEL__": STUDY_HOURS_ONLINE_LABEL,
    }
    for placeholder, value in replacements.items():
        js = js.replace(placeholder, json.dumps(value, ensure_ascii=False))
    return js


_STUDY_HOURS_DOM_JS = _render_study_hours_dom_js()


def _hours_region_text(text: str) -> str:
    """截取「今年已训」所在行的文本区域（到下一行标签为止）。"""
    if not text:
        return ""
    start, start_len = -1, 0
    for label in STUDY_HOURS_ROW_LABELS:
        idx = text.find(label)
        if idx >= 0 and (start < 0 or idx < start):
            start, start_len = idx, len(label)
    if start < 0:
        return ""
    region = text[start + start_len:]
    end = len(region)
    for label in STUDY_HOURS_TEXT_END_LABELS:
        idx = region.find(label)
        if idx >= 0:
            end = min(end, idx)
    return region[:end]


def _hours_candidates(region: str) -> list:
    """把区域文本拆成候选学时，按出现顺序返回 [(score, value)]。

    评分（越高越可信）：
      3 = 整格只有「数字(+学时)(+箭头)」
      2 = 整格不含中文（纯数值格）
      1 = 数字出现在本格中文之前（如「848.05学时 2023年以来…」）
      0 = 数字前面已经有中文（说明文字，如「2023年以来已学习848.05学时」）→ 丢弃

    表格布局下 inner_text 会用 \\t 分隔同一行的单元格（「今年已训\\t848.05 学时」），
    所以按「单元格」而不是「整行」评分，否则标签会把同一格的数值一起拖下水。
    """
    out = []
    for raw_line in region.splitlines():
        line = raw_line.replace('\u00a0', ' ')
        for raw_cell in line.split('\t'):
            cell = raw_cell.strip()
            if not cell:
                continue
            m = _HOURS_STRICT_LINE_RE.match(cell)
            if m:
                try:
                    out.append((3, float(m.group(1))))
                except ValueError:
                    pass
                continue
            has_cjk = bool(_HOURS_CJK_RE.search(cell))
            if not has_cjk:
                for m in _HOURS_TOKEN_RE.finditer(cell):
                    try:
                        out.append((2, float(m.group(1))))
                    except ValueError:
                        pass
                continue
            for m in _HOURS_TOKEN_RE.finditer(cell):
                prefix = cell[:m.start()]
                # 数字紧跟在中文/说明文字后面 → 说明句，不算学时
                if _HOURS_CJK_RE.search(prefix):
                    continue
                try:
                    out.append((1, float(m.group(1))))
                except ValueError:
                    pass
    return out


def _required_region_text(text: str) -> str:
    """截取「应训时长」所在行的文本区域（到今年已训/下一段为止）。"""
    if not text:
        return ""
    start, start_len = -1, 0
    for label in STUDY_HOURS_REQUIRED_ROW_LABELS:
        idx = text.find(label)
        if idx >= 0 and (start < 0 or idx < start):
            start, start_len = idx, len(label)
    if start < 0:
        return ""
    region = text[start + start_len:]
    end = len(region)
    for label in STUDY_HOURS_ROW_LABELS + STUDY_HOURS_TEXT_END_LABELS:
        idx = region.find(label)
        if idx >= 0:
            end = min(end, idx)
    return region[:end]


def parse_required_hours_text(text: str) -> Dict[str, Optional[float]]:
    """解析「应训时长」行的集中培训 / 网络自学应完成学时（文本兜底层）。

    这一行是要求值，格式比「今年已训」规整：常见是
    「每年应完成 / 90学时 / 每年应完成 / 50学时」，也可能挤成一行
    「每年应完成 90 学时」或表格里的「应训时长\\t每年应完成\\n90 学时\\t…」。
    所以先取「整格就是数值」的候选，凑不齐两个再按出现顺序放宽。
    """
    result: Dict[str, Optional[float]] = {"required_central": None, "required_online": None}
    region = _required_region_text(text or "")
    if not region:
        return result
    candidates = []          # 按出现顺序的 (是否整格纯数值, 数值)
    for raw_line in region.splitlines():
        line = raw_line.replace('\u00a0', ' ')
        for raw_cell in line.split('\t'):
            cell = raw_cell.strip()
            if not cell:
                continue
            m = _HOURS_STRICT_LINE_RE.match(cell)
            if m:
                try:
                    candidates.append((True, float(m.group(1))))
                except ValueError:
                    pass
                continue
            # 「每年应完成 90 学时」这类标签+数值同格也接受
            for token in _HOURS_TOKEN_RE.finditer(cell):
                try:
                    candidates.append((False, float(token.group(1))))
                except ValueError:
                    pass
    strict = [value for is_strict, value in candidates if is_strict]
    values = strict if len(strict) >= 2 else [value for _is_strict, value in candidates]
    debug(f"应训时长候选: {candidates} -> {values}")
    if values:
        result["required_central"] = values[0]
    if len(values) >= 2:
        result["required_online"] = values[1]
    return result


def parse_study_hours_text(text: str) -> Dict[str, Optional[float]]:
    """从学习中心页面文本解析 集中培训 / 网络自学 学时（文本兜底层）。"""
    result: Dict[str, Optional[float]] = {"central": None, "online": None}
    if not text:
        return result
    region = _hours_region_text(text)
    if region:
        candidates = _hours_candidates(region)
        debug(f"[今年已训]行候选: {candidates}")
        values = [value for _score, value in candidates]
        if values:
            result["central"] = values[0]
            if len(values) >= 2:
                result["online"] = values[1]
            return result
        debug("学习中心[今年已训]行未解析出学时，退化为全文扫描")
    # 兜底：没有行标签时按「应完成X, 应完成Y, 已训A, 已训B」的字段顺序取
    full = [value for _score, value in _hours_candidates(text)]
    debug(f"学时全文字段: {full}")
    if len(full) >= 4:
        result["central"], result["online"] = full[2], full[3]
    elif len(full) >= 2:
        result["central"], result["online"] = full[0], full[1]
    elif len(full) == 1:
        result["central"] = full[0]
    return result


def parse_study_hours_dom(dom: Optional[dict]) -> Dict[str, Optional[float]]:
    """把页面 JS 的结构化解析结果规整成 {central, online, required_central, required_online}。"""
    result: Dict[str, Optional[float]] = {"central": None, "online": None,
                                          "required_central": None, "required_online": None,
                                          "debug": None}
    if not isinstance(dom, dict):
        return result
    result["debug"] = dom.get("debug")
    for key in ("central", "online", "required_central", "required_online"):
        raw = dom.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            result[key] = value
    return result


def resolve_study_hours(dom: Optional[dict], text: str) -> Dict[str, object]:
    """合并 DOM 结构化解析与文本兜底解析，返回最终学时。

    central/online 是今年已训，required_central/required_online 是应训时长
    （自动识别目标模式用它当学习目标）。source 只描述今年已训的来源。
    """
    structured = parse_study_hours_dom(dom)
    central, online = structured["central"], structured["online"]
    required_central = structured["required_central"]
    required_online = structured["required_online"]
    had_dom = central is not None or online is not None
    if central is not None and online is not None:
        source = "dom"
    else:
        fallback = parse_study_hours_text(text)
        if central is None:
            central = fallback["central"]
        if online is None:
            online = fallback["online"]
        source = "dom+text" if (had_dom and (central is not None or online is not None)) else "text"
    if required_central is None or required_online is None:
        required = parse_required_hours_text(text)
        if required_central is None:
            required_central = required["required_central"]
        if required_online is None:
            required_online = required["required_online"]
    central = 0.0 if central is None else float(central)
    online = 0.0 if online is None else float(online)
    required_central = 0.0 if required_central is None else float(required_central)
    required_online = 0.0 if required_online is None else float(required_online)
    return {"central": central, "online": online, "total": central + online,
            "required_central": required_central, "required_online": required_online,
            "source": source, "debug": structured["debug"]}


class AutoLearner:
    def __init__(self, headless: bool = False, workers: int = 1, browser: str = "chromium"):
        self.headless = headless
        self.workers = workers
        self.browser_type = browser  # "chromium" or "chrome"
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages: List[Page] = []
        self.study_hours = 0.0
        self.target_hours = 0.0
        self.tags_to_learn = []
        self.study_goal = 0.0  # 学习目标学时
        self.goal_type = 'central'  # 目标类型: central=集中培训 online=网络自学
        self.last_stats = (0, 0)  # 最近一次学习任务的 (成功数, 失败数)，供 GUI 显示
        self._stop_event = threading.Event()  # GUI 变更配置时置位，学习引擎协作式停止
        self._progress_lock = threading.RLock()  # 进度文件并发读写锁（可重入）
        self._hours_cache = {"value": None, "ts": 0.0}  # 学时查询 TTL 缓存
        self._hours_ttl = 60.0  # 学时缓存有效期（秒）
        self._hours_lock = None  # 懒创建 asyncio.Lock（避免在 __init__ 绑定事件循环）
        self.user_data = {}
        # 考试自动答题（训练营随堂测试）：默认关闭，需在设置里配置 DeepSeek API Key
        self.exam_enabled = False
        self.deepseek_api_key = ""
        self.deepseek_model = DEEPSEEK_DEFAULT_MODEL
        self.deepseek_base_url = DEEPSEEK_DEFAULT_BASE_URL
        self.deepseek_thinking = False
        # 交卷延时：每题模拟耗时（秒）的随机区间，交卷前等待「题量 × 区间内随机值」秒
        self.exam_delay_min = EXAM_DELAY_MIN_DEFAULT
        self.exam_delay_max = EXAM_DELAY_MAX_DEFAULT
        # 考试没考成/没通过时询问是否重考的回调（GUI 注入；命令行/无界面时保持 None → 不重考）
        self.exam_retry_hook = None

    async def _browser_step(self, coro, what: str, _log, seconds=None):
        """给浏览器启动的每一步加超时：卡住要能看见、能报错，而不是无声挂起。"""
        budget = BROWSER_STEP_TIMEOUT_SECONDS if seconds is None else seconds
        try:
            return await asyncio.wait_for(coro, timeout=budget)
        except asyncio.TimeoutError:
            debug(f"{what} 超时（{budget}s）")
            _log(f"{what}超时（{budget:.0f} 秒无响应），已中止本次启动；"
                 f"常见原因是安全软件拦截或浏览器未完全退出，稍后重试即可", "red")
            raise RuntimeError(f"{what}超时") from None

    async def init(self, log_callback=None, chrome_path="", download_callback=None):
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        # download_callback(start: bool)：下载 Chromium 前回调 True、完成后回调 False，
        # 供 GUI 显示等待进度（GUI 侧通过信号转回主线程）

        # 冻结版关键修复：Playwright 的 transport 在冻结时会用
        # env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0") 把浏览器路径指向临时 _MEI 解包目录
        # （每次启动都会被清空，且与下载位置不一致）。
        # 这里预先设到标准用户缓存目录，setdefault 便不会覆盖，下载与启动用同一持久路径。
        if getattr(sys, 'frozen', False):
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _default_browsers_path())

        # 不打包浏览器：Chromium 使用 Playwright 默认缓存目录
        # （macOS: ~/Library/Caches/ms-playwright，Windows: %LOCALAPPDATA%\ms-playwright），
        # 首次使用"内置 Chromium"模式时自动下载

        # 启动 Playwright
        try:
            self.playwright = await async_playwright().start()
        except Exception as e:
            err_msg = str(e)
            if "Connection closed" in err_msg or "driver" in err_msg.lower():
                _log("Playwright 驱动异常，请在终端运行：", "red")
                if sys.platform == "win32":
                    _log("  pip install playwright && python -m playwright install chromium", "yellow")
                else:
                    _log("  pip3 install playwright && python3 -m playwright install chromium", "yellow")
            raise

        # 检测内置 Chromium 是否可用（仅在使用内置 Chromium 时检测）
        if self.browser_type != "chrome":
            try:
                test_browser = await self.playwright.chromium.launch(headless=True)
                await test_browser.close()
            except Exception as e:
                err_msg = str(e)
                if "Executable doesn't exist" in err_msg or "Browser" in err_msg:
                    _log("未找到内置 Chromium 浏览器，正在下载（首次使用约需几分钟）...", "yellow")
                    if download_callback:
                        download_callback(True)
                    ok = await self._download_chromium(_log, download_callback)
                    if download_callback:
                        download_callback(False)
                    if not ok:
                        _log("Chromium 下载失败，请检查网络后重试，或改用系统 Chrome 浏览器", "red")
                        raise RuntimeError("Chromium download failed")
                    _log("Chromium 下载完成", "green")
                else:
                    raise

        # 清理Playwright残留的chromium进程（不影响用户自己的浏览器）
        _kill_playwright_chrome()

        # 复用上方已启动的 playwright 实例（不再重复 start，避免驱动进程泄漏）
        # --autoplay-policy：训练营视频靠脚本拉起播放，默认策略会拦截无用户手势的自动播放
        launch_opts = {
            "headless": self.headless,
            "args": ["--autoplay-policy=no-user-gesture-required"],
        }
        use_system_chrome = False

        if self.browser_type == "chrome":
            # 用户选择使用系统 Chrome
            if chrome_path and os.path.exists(chrome_path):
                # 用户手动指定了路径
                launch_opts["executablePath"] = chrome_path
                use_system_chrome = True
                _log(f"使用指定 Chrome: {chrome_path}", "green")
            elif sys.platform == "win32":
                # Windows 自动检测
                chrome_paths = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                ]
                for path in chrome_paths:
                    if os.path.exists(path):
                        launch_opts["channel"] = "chrome"
                        use_system_chrome = True
                        _log("使用系统 Chrome", "green")
                        break
                if not use_system_chrome:
                    try:
                        subprocess.run(["where", "chrome"], check=True, capture_output=True,
                                       timeout=3, **_hidden_subprocess_kwargs())
                        launch_opts["channel"] = "chrome"
                        use_system_chrome = True
                        _log("使用系统 Chrome (PATH)", "green")
                    except:
                        pass
            else:
                # macOS/Linux
                launch_opts["channel"] = "chrome"
                use_system_chrome = True
                _log("使用系统 Chrome", "green")

            if not use_system_chrome:
                _log("未找到系统 Chrome，改用内置 Chromium", "yellow")

        if not use_system_chrome:
            _log("使用内置 Chromium", "yellow")

        try:
            self.browser = await self._browser_step(
                self.playwright.chromium.launch(**launch_opts), "启动浏览器", _log)
        except Exception as e:
            if use_system_chrome:
                console.print("系统Chrome启动失败，改用内置Chromium", style="yellow")
                launch_opts.pop("channel", None)
                try:
                    self.browser = await self._browser_step(
                        self.playwright.chromium.launch(**launch_opts), "启动内置 Chromium", _log)
                except Exception as e2:
                    err2 = str(e2)
                    if "Executable doesn't exist" in err2 or "Browser" in err2:
                        # 内置 Chromium 未安装：下载后重试（与浏览器探测分支同一流程）
                        _log("未找到内置 Chromium 浏览器，正在下载（首次使用约需几分钟）...", "yellow")
                        if download_callback:
                            download_callback(True)
                        ok = await self._download_chromium(_log, download_callback)
                        if download_callback:
                            download_callback(False)
                        if not ok:
                            _log("Chromium 下载失败，请检查网络后重试，或改用系统 Chrome 浏览器", "red")
                            raise RuntimeError("Chromium download failed")
                        _log("Chromium 下载完成", "green")
                        self.browser = await self._browser_step(
                            self.playwright.chromium.launch(**launch_opts),
                            "启动内置 Chromium", _log)
                    else:
                        raise
            else:
                raise

        # 创建浏览器上下文（不硬编码user_agent，让Playwright自动匹配当前OS）
        context_opts = {
            "viewport": {"width": 1920, "height": 1080},
        }
        if os.path.exists(STORAGE_STATE_PATH):
            try:
                self.context = await self._browser_step(
                    self.browser.new_context(storage_state=STORAGE_STATE_PATH, **context_opts),
                    "创建浏览器上下文", _log)
                console.print("已加载保存的会话", style="green")
            except Exception as e:
                console.print("加载会话失败，创建新会话", style="yellow")
                self.context = await self._browser_step(
                    self.browser.new_context(**context_opts), "创建浏览器上下文", _log)
        else:
            self.context = await self._browser_step(
                self.browser.new_context(**context_opts), "创建浏览器上下文", _log)

        for i in range(self.workers):
            page = await self._browser_step(
                self.context.new_page(), f"打开第 {i + 1} 个标签页", _log)
            self.pages.append(page)

    async def _download_chromium(self, _log=None, download_callback=None) -> bool:
        """下载内置 Chromium 浏览器到系统缓存目录（B1：真实进度反馈）。

        源码版用 python -m playwright；冻结版用打包自带的 Playwright 驱动（node + cli.js）。
        Playwright 安装 CLI 在非终端下不输出百分比，这里改为监控缓存目录的
        "新文件+增长文件"字节数，给出真实的"已下载 MB · 速度"反馈。
        download_callback: True=开始, str=进度文本, False=结束。
        """
        _log = _log or (lambda msg, style="": console.print(msg, style=style))

        def report(status):
            if download_callback:
                try:
                    download_callback(status)
                except Exception:
                    pass

        try:
            if getattr(sys, 'frozen', False):
                from playwright._impl._driver import compute_driver_executable, get_driver_env
                node, cli = compute_driver_executable()
                env = get_driver_env()
                # 关键：冻结版必须与运行时查找路径一致（标准用户缓存），
                # 否则会装进临时 _MEI 目录（每次启动丢失）
                env.setdefault("PLAYWRIGHT_BROWSERS_PATH", _default_browsers_path())
                proc = subprocess.Popen([node, cli, "install", "chromium"], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        **_hidden_subprocess_kwargs())
            else:
                proc = subprocess.Popen([sys.executable, "-m", "playwright", "install", "chromium"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        **_hidden_subprocess_kwargs())

            # 监控下载进度：缓存目录内新增/增长文件字节数
            reg = _default_browsers_path()
            baseline = _dir_file_sizes(reg)
            start_ts = time.time()
            last_report = ""
            timeout_deadline = time.time() + 1800  # 30 分钟上限
            while proc.poll() is None:
                if time.time() > timeout_deadline:
                    proc.kill()
                    debug("下载 Chromium 超时")
                    return False
                growth = _dir_growth_bytes(reg, baseline)
                if growth > 0:
                    mb = growth / 1024 / 1024
                    speed = mb / max(1, time.time() - start_ts)
                    txt = f"已下载 {mb:.0f} MB · {speed:.1f} MB/s"
                    if txt != last_report:
                        last_report = txt
                        report(txt)
                await asyncio.sleep(1)
            return proc.returncode == 0
        except asyncio.CancelledError:
            # 停止学习时终止下载进程
            if 'proc' in locals() and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise
        except Exception as e:
            debug(f"下载 Chromium 失败: {e}")
            return False

    async def close(self):
        # 先保存会话状态（必须在 context 关闭之前，否则 storage_state 必然失败，
        # 导致登录态每次退出都丢失、用户反复重新登录）
        try:
            if self.context:
                await self.context.storage_state(path=STORAGE_STATE_PATH)
                console.print("会话已保存", style="green")
        except:
            console.print("保存会话失败", style="yellow")

        # 再关闭所有页面和弹窗
        if self.context:
            try:
                for p in self.context.pages:
                    try:
                        await p.close()
                    except:
                        pass
                await self.context.close()
            except:
                pass
            self.context = None
            self.pages = []
        
        # 关闭浏览器
        if self.browser:
            try:
                await self.browser.close()
            except:
                pass
            self.browser = None
        
        # 停止Playwright
        if self.playwright:
            try:
                await self.playwright.stop()
            except:
                pass
            self.playwright = None
        
        # 强制结束Playwright残留进程
        _kill_playwright_chrome()

    async def check_login_status(self, page: Page) -> bool:
        """检查是否已登录 - 通过页面真实DOM状态检测"""
        try:
            console.print("正在检查登录状态...", style="blue")
            try:
                await page.goto("https://u.ccb.com/portal/#/study",
                                wait_until="networkidle", timeout=20000)
            except:
                await page.goto("https://u.ccb.com/portal/#/study",
                                wait_until="domcontentloaded", timeout=15000)
            # SPA 可能需要额外时间渲染，等待关键元素出现
            await page.wait_for_timeout(5000)
            
            current_url = page.url
            debug(f"当前URL: {current_url}")
            
            # 1) 检查是否被重定向到统一登录页
            if "/sys/#/login" in current_url:
                console.print("被重定向到登录页面，判定未登录", style="yellow")
                return False
            
            # 2) 检查未登录标志元素（访客模式下页面特有）
            try:
                notlogin_tips = await page.locator(".cuWeb-swipe-web-info-notlogin-tips").count()
                notlogin_btn = await page.locator(".cuWeb-swipe-web-info-notlogin-btn").count()
                if notlogin_tips > 0 or notlogin_btn > 0:
                    console.print('发现未登录提示“登录跟进你的学习进度”，判定未登录', style='yellow')
                    return False
            except:
                pass
            
            # 3) 检查用户盒子：显示"登录"=未登录，显示用户名=已登录
            try:
                user_box_text = await page.locator(".ccb-user-box").inner_text(timeout=3000)
                if "登录" in user_box_text and len(user_box_text.strip()) < 10:
                    console.print("用户盒子显示「登录」，判定未登录", style="yellow")
                    return False
            except:
                pass
            
            # 4) 页面文本兜底判断
            page_text = await page.locator("body").inner_text(timeout=5000)
            if "立即登录" in page_text and "0学时" in page_text:
                console.print("页面显示「立即登录」且学时为0，判定未登录", style="yellow")
                return False
            
            # 5) 通过以上所有检查，再看是否有真实用户学习数据
            if "学时" in page_text and "学员" in page_text and "立即登录" not in page_text:
                console.print("检测到真实用户数据，判定已登录", style="green")
                return True
            
            console.print("未能确认登录状态，默认判定未登录", style="yellow")
            return False
        except Exception as e:
            console.print(f"检查登录状态失败: {e}", style="yellow")
            return False

    @staticmethod
    def _xor_crypt(data: str, key: int = 5277) -> str:
        """XOR + base64 加密/解密"""
        import base64
        key_bytes = str(key).encode()
        encrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data.encode()))
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _xor_decrypt(token: str, key: int = 5277) -> str:
        """XOR + base64 解密"""
        import base64
        key_bytes = str(key).encode()
        decoded = base64.b64decode(token)
        decrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(decoded))
        return decrypted.decode()

    @staticmethod
    def _store_password(username: str, password: str) -> bool:
        """优先存入系统钥匙串（keyring），失败时退回 XOR 混淆文件字段。
        返回是否成功写入 keyring。"""
        try:
            import keyring
            keyring.set_password("Moisten", username, password)
            return True
        except Exception:
            return False

    @staticmethod
    def _load_password(username: str) -> str:
        """先从系统钥匙串取密码；没有则返回空（由调用方回退旧 XOR 字段）。"""
        try:
            import keyring
            return keyring.get_password("Moisten", username) or ""
        except Exception:
            return ""

    def load_user_credentials(self) -> Optional[Dict]:
        """加载保存的用户凭证（keyring 优先，兼容旧的 XOR 存储）"""
        if os.path.exists(USER_CREDENTIALS_PATH):
            try:
                with open(USER_CREDENTIALS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                username = data.get('username', '')
                # 优先从 keyring 取密码
                if username:
                    kp = self._load_password(username)
                    if kp:
                        data['password'] = kp
                        return data
                # 回退：旧的 XOR 字段
                if 'password' in data and data['password']:
                    try:
                        data['password'] = self._xor_decrypt(data['password'])
                    except:
                        pass  # 兼容旧的明文密码
                return data
            except Exception as e:
                console.print("加载凭证失败", style="yellow")
        return None

    def save_user_credentials(self, username: str, password: str):
        """保存用户凭证（优先系统钥匙串，文件仅存账号与混淆兜底）"""
        try:
            ok = self._store_password(username, password) if password else True
            # 文件仍写一份 XOR 兜底，供无 keyring 环境回退
            encrypted_pw = self._xor_crypt(password) if password else ""
            with open(USER_CREDENTIALS_PATH, 'w', encoding='utf-8') as f:
                json.dump({"username": username, "password": encrypted_pw}, f, ensure_ascii=False, indent=2)
            console.print("凭证已保存" + ("（系统钥匙串）" if ok else "（本地混淆存储）"), style="green")
        except Exception as e:
            console.print("保存凭证失败", style="yellow")

    def load_progress(self) -> dict:
        """加载学习进度（已完成的专题班ID集合）"""
        with self._progress_lock:
            try:
                if os.path.exists(PROGRESS_PATH):
                    with open(PROGRESS_PATH, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    return data
            except:
                pass
            return {"completed_ws_ids": [], "last_page": 1, "last_idx": 0}

    def save_progress(self, completed_ws_ids: set, last_page: int = 1, last_idx: int = 0):
        """保存学习进度（带锁，避免多 worker 并发写互相覆盖）"""
        with self._progress_lock:
            try:
                _atomic_json_dump(PROGRESS_PATH, {
                    "completed_ws_ids": list(completed_ws_ids),
                    "last_page": last_page,
                    "last_idx": last_idx,
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            except Exception as e:
                debug(f"保存进度失败: {e}")

    def mark_workshop_completed(self, ws_id: str):
        """标记单个专题班完成，立即落盘（带锁）"""
        with self._progress_lock:
            try:
                progress = self.load_progress()
                completed = set(progress.get("completed_ws_ids", []))
                completed.add(ws_id)
                self.save_progress(completed,
                                   progress.get("last_page", 1),
                                   progress.get("last_idx", 0))
            except Exception as e:
                debug(f"标记完成失败: {e}")

    def mark_course_completed(self, title: str, course_key: str = ""):
        """网络自学断点续学：记录课程标题和稳定键，立即落盘（带锁）。"""
        with self._progress_lock:
            try:
                progress = self.load_progress()
                done = set(progress.get("completed_course_titles", []))
                done.add(title)
                progress["completed_course_titles"] = sorted(done)
                if course_key:
                    keys = set(progress.get("completed_course_keys", []))
                    keys.add(course_key)
                    progress["completed_course_keys"] = sorted(keys)
                progress["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _atomic_json_dump(PROGRESS_PATH, progress)
            except Exception as e:
                debug(f"标记课程完成失败: {e}")

    def load_completed_course_titles(self) -> set:
        """读取已学课程标题集合（网络自学断点续学用）"""
        try:
            return set(self.load_progress().get("completed_course_titles", []))
        except Exception:
            return set()

    async def login(self, page=None, username="", password="", auto_login=True, log_callback=None):
        """登录。GUI模式传入username/password/auto_login/log_callback。"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        if page is None:
            page = self.pages[0]

        # GUI模式：直接用传入的凭证登录
        if username:
            await self._do_login(page, username, password, auto_login, _log)
            return

        console.print("自动登录", style="bold blue")
        if await self.check_login_status(page):
            # 显示当前用户并询问是否切换
            try:
                _uname = ""
                try:
                    _uname = await page.locator(".ccb-user-box").inner_text(timeout=3000)
                except:
                    pass
                if not _uname:
                    try:
                        _uname = await page.evaluate("() => localStorage.getItem('userName') || ''")
                    except:
                        pass
                _uname = (_uname or "").strip()
                if _uname:
                    console.print(f"当前用户: {_uname}", style="green")
                    _switch = await async_input("是否切换用户？(y/n)", default="n", timeout=5)
                    if _switch in ('y', 'yes'):
                        try:
                            if os.path.exists(STORAGE_STATE_PATH):
                                os.remove(STORAGE_STATE_PATH)
                            await page.context.clear_cookies()
                            console.print("已清除会话，准备重新登录", style="yellow")
                        except:
                            pass
                    else:
                        console.print("✓ 继续使用当前会话", style="bold green")
                        return
                else:
                    console.print("✓ 检测到已登录状态，无需重新登录!", style="bold green")
                    return
            except:
                console.print("✓ 检测到已登录状态，无需重新登录!", style="bold green")
                return
        
        console.print("未检测到登录状态，需要登录", style="yellow")
        
        # 尝试加载已保存的凭证
        saved_credentials = self.load_user_credentials()
        use_saved = False
        
        if saved_credentials and 'username' in saved_credentials:
            choice = await async_input(f"发现已保存账号: {saved_credentials['username']}，是否使用？(y/n，默认y)", default="y", timeout=5)
            if choice != 'n' and choice != 'no':
                use_saved = True
        
        if use_saved and saved_credentials:
            username = saved_credentials['username']
            password = saved_credentials.get('password', '')
            if not password:
                password = await async_input("请输入密码", default="", timeout=300, block=True, raw=True, password=True)
        else:
            # 询问用户是自动登录还是手动登录
            choice = await async_input("是否使用自动登录？(y/n，默认y)", default="y", timeout=5)
            
            if choice == 'n' or choice == 'no':
                # 手动登录模式
                console.print("请在打开的浏览器中完成登录...", style="bold blue")
                await page.goto("https://u.ccb.com/portal/#/study")

                console.print("等待登录完成...", style="yellow")
                console.print("提示：登录成功后按回车键继续", style="green")

                await async_input("登录成功后按回车键继续", default="", timeout=600, block=True)

                console.print("✓ 登录成功!", style="bold green")
                return
            else:
                # 自动登录模式
                console.print()
                username = await async_input("请输入统一认证账号", default="", timeout=300, block=True, raw=True)
                # 密码也用async_input，避免和stdin线程冲突
                password = await async_input("请输入密码", default="", timeout=300, block=True, raw=True, password=True)
                
                if not username or not password:
                    console.print("用户名或密码不能为空，将使用手动登录模式", style="red")
                    await self.login()
                    return
        
        # 使用自动登录流程
        console.print("正在导航到登录页面...", style="blue")
        await page.goto("https://u.ccb.com/sys/#/login")
        await asyncio.sleep(3)
        
        try:
            # 输入用户名（先移除maxlength限制，再通过原生DOM API赋值）
            console.print("正在输入用户名...", style="blue")
            await page.evaluate(f"""() => {{
                const el = document.querySelector('input[placeholder*="账号"]');
                if (el) {{
                    el.removeAttribute('maxlength');
                    el.removeAttribute('maxLength');
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '{username}');
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}""")
            # 验证输入是否正确
            actual_uname = await page.evaluate("""() => {
                const el = document.querySelector('input[placeholder*="账号"]');
                return el ? el.value : '';
            }""")
            console.print(f"  实际填入: [{actual_uname}]", style="blue")
            if len(actual_uname) < len(username):
                console.print("输入不完整，尝试逐字符键盘输入...", style="yellow")
                await page.keyboard.press(_SELECT_ALL)
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(300)
                await page.keyboard.type(username, delay=150)
                actual_uname = await page.evaluate("""() => {
                    const el = document.querySelector('input[placeholder*="账号"]');
                    return el ? el.value : '';
                }""")
                console.print(f"  键盘输入后: [{actual_uname}]", style="blue")
            await asyncio.sleep(0.5)
            
            # 输入密码
            console.print("正在输入密码...", style="blue")
            await page.evaluate(f"""() => {{
                const el = document.getElementById('inputPwd');
                if (el) {{
                    el.removeAttribute('maxlength');
                    el.removeAttribute('maxLength');
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '{password}');
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}""")
            await asyncio.sleep(0.5)
            
            # 点击登录按钮（用JS点击，headless更可靠）
            console.print("正在点击登录按钮...", style="blue")
            await asyncio.sleep(1)
            await page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.innerText && btn.innerText.includes('登录')) {
                        btn.click();
                        return;
                    }
                }
                // 兜底：找type=submit的按钮
                const submit = document.querySelector('button[type="submit"]');
                if (submit) submit.click();
            }""")
            
            # 等待登录成功后保存凭证
            if not use_saved:
                save_choice = await async_input("是否保存账号密码以便下次使用？(y/n，默认y)", default="y", timeout=5)
                if save_choice != 'n' and save_choice != 'no':
                    self.save_user_credentials(username, password)
            
            # 自动检测登录是否完成
            console.print("正在等待登录完成...", style="yellow")
            
            logged_in = False
            login_failed = False
            for i in range(60):  # 最多等待60秒
                await asyncio.sleep(1)
                try:
                    current_url = page.url
                    
                    # 如果还停留在登录页
                    if "/sys/#/login" in current_url:
                        # 检查是否有错误提示
                        try:
                            # 检查页面上的错误文字（密码错误、认证失败等）
                            body_text = await page.locator("body").inner_text(timeout=2000)
                            for err_msg in ["用户认证失败", "认证失败", "密码错误", "账号或密码", "请检查"]:
                                if err_msg in body_text:
                                    console.print(f"[red]登录失败: {err_msg}[/red]")
                                    login_failed = True
                                    break
                            if login_failed:
                                break
                        except:
                            pass
                        try:
                            err = page.locator(".el-message--error, .el-form-item__error, [class*=error]")
                            err_text = await err.first.inner_text(timeout=1500)
                            if err_text:
                                console.print(f"[red]登录失败: {err_text.strip()}[/red]")
                                login_failed = True
                                break
                        except:
                            pass
                        if i >= 30:
                            console.print("[yellow]登录请求似乎未成功，尝试检查页面状态...[/yellow]")
                            login_failed = True
                            break
                        continue
                    
                    # URL不再是登录页 → 登录成功！导航到study页确认
                    console.print(f"检测到页面跳转: {current_url}", style="green")
                    console.print("正在导航到学习页面确认登录状态...", style="blue")
                    
                    # 导航到study页面做最终确认
                    await page.goto("https://u.ccb.com/portal/#/study",
                                    wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(5000)
                    
                    # 用和check_login_status相同的逻辑做最终验证
                    final_url = page.url
                    if "/sys/#/login" in final_url:
                        console.print("被重定向回登录页，登录未成功", style="yellow")
                        await asyncio.sleep(3)
                        continue
                    
                    # 检查是否还有未登录标志
                    nt = await page.locator(".cuWeb-swipe-web-info-notlogin-tips").count()
                    nb = await page.locator(".cuWeb-swipe-web-info-notlogin-btn").count()
                    if nt == 0 and nb == 0:
                        try:
                            ubt = await page.locator(".ccb-user-box").inner_text(timeout=2000)
                            if "登录" not in ubt or len(ubt.strip()) >= 10:
                                logged_in = True
                                console.print("✓ 检测到登录成功!", style="bold green")
                                break
                        except:
                            pass
                        # 兜底：页面文字
                        pt = await page.locator("body").inner_text(timeout=3000)
                        if "立即登录" not in pt and ("学时" in pt or "课程" in pt):
                            logged_in = True
                            console.print("✓ 检测到登录成功!", style="bold green")
                            break
                    
                    # 如果到了这里还没确认，继续等待
                    console.print("尚未确认登录状态，继续等待...", style="yellow")
                except:
                    pass
            
            # 处理登录失败：重试
            if login_failed and not logged_in:
                console.print("[yellow]登录失败！[/yellow]")
                console.print("可能原因：账号/密码错误、网络问题或验证码", style="yellow")
                retry = await async_input("是否重新输入账号密码重试？(y/n，默认y)", default="y", timeout=5)
                if retry != 'n':
                    for attempt in range(3):
                        console.print(f"[bold blue]第 {attempt+1} 次重试[/bold blue]")
                        await page.goto("https://u.ccb.com/sys/#/login")
                        await asyncio.sleep(2)
                        
                        console.print("正在输入用户名...", style="blue")
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('input[placeholder*="账号"]');
                            if (el) {{
                                el.removeAttribute('maxlength');
                                el.removeAttribute('maxLength');
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, '{username}');
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            }}
                        }}""")
                        actual_uname = await page.evaluate("""() => {
                            const el = document.querySelector('input[placeholder*="账号"]');
                            return el ? el.value : '';
                        }""")
                        if len(actual_uname) < len(username):
                            console.print("输入不完整，用键盘补充...", style="yellow")
                            await page.keyboard.press(_SELECT_ALL)
                            await page.keyboard.press("Backspace")
                            await page.wait_for_timeout(300)
                            await page.keyboard.type(username, delay=150)
                        
                        await asyncio.sleep(0.5)
                        console.print("正在输入密码...", style="blue")
                        await page.evaluate(f"""() => {{
                            const el = document.getElementById('inputPwd');
                            if (el) {{
                                el.removeAttribute('maxlength');
                                el.removeAttribute('maxLength');
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, '{password}');
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            }}
                        }}""")
                        
                        console.print("正在点击登录按钮...", style="blue")
                        lb = page.get_by_role("button", name="登录")
                        await lb.click()
                        
                        for j in range(30):
                            await asyncio.sleep(1)
                            cu = page.url
                            if "/sys/#/login" not in cu:
                                await page.wait_for_timeout(3000)
                                nt = await page.locator(".cuWeb-swipe-web-info-notlogin-tips").count()
                                if nt == 0:
                                    logged_in = True
                                    console.print("✓ 重试登录成功!", style="bold green")
                                    break
                                break
                            # 检查错误
                            try:
                                body_text = await page.locator("body").inner_text(timeout=2000)
                                for err_msg in ["用户认证失败", "认证失败", "密码错误", "账号或密码", "请检查"]:
                                    if err_msg in body_text:
                                        console.print(f"[red]重试失败: {err_msg}[/red]")
                                        break
                            except:
                                pass
                            try:
                                e = page.locator(".el-message--error, .el-form-item__error")
                                t = await e.first.inner_text(timeout=1500)
                                if t:
                                    console.print(f"[red]重试失败: {t.strip()}[/red]")
                                    break
                            except:
                                pass
                        if logged_in:
                            break
                    else:
                        console.print("[yellow]多次重试失败，将使用手动登录模式[/yellow]")
            
            if not logged_in:
                await async_input("请手动在浏览器中完成登录，然后按回车键继续", default="", timeout=600, block=True)
            
            console.print("✓ 登录流程完成!", style="bold green")

        except Exception as e:
            console.print("自动登录失败", style="red")
            console.print("将使用手动登录模式", style="yellow")
            await async_input("请在浏览器中完成登录后按回车键继续", default="", timeout=600, block=True)

    async def _do_login(self, page, username, password, auto_login, _log):
        """GUI模式登录：直接用传入的凭证提交表单。失败自动重试，页面未加载会刷新。"""
        if not auto_login:
            _log("请在浏览器中完成登录...", "blue")
            await page.goto("https://u.ccb.com/portal/#/study")
            _log("等待登录完成...", "yellow")
            # 等待URL不再是登录页
            for _ in range(120):
                await asyncio.sleep(1)
                if "/sys/#/login" not in page.url:
                    break
            _log("登录成功", "green")
            return

        login_url = "https://u.ccb.com/sys/#/login"
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    _log(f"正在重试登录({attempt}/{max_retries})...", "yellow")
                    await asyncio.sleep(3)

                # ── 1. 导航到登录页，确保页面加载 ──
                _log("正在导航到登录页面...", "blue")
                page_loaded = False
                for refresh in range(1, 4):  # 最多刷新3次
                    try:
                        await page.goto(login_url, timeout=15000)
                    except:
                        pass
                    await asyncio.sleep(3)

                    # 检查登录表单是否出现
                    has_form = await page.evaluate("""() => {
                        return !!(
                            document.querySelector('input[placeholder*="账号"]') ||
                            document.querySelector('input[placeholder*="用户"]') ||
                            document.querySelector('#inputPwd') ||
                            document.querySelector('input[type="password"]')
                        );
                    }""")
                    if has_form:
                        page_loaded = True
                        break
                    _log(f"登录页未加载完，刷新({refresh}/3)...", "yellow")

                if not page_loaded:
                    _log(f"登录页加载失败(尝试 {attempt}/{max_retries})", "red")
                    continue  # 下一次重试

                # ── 2. 输入用户名 ──
                _log("正在输入用户名...", "blue")
                username_filled = await page.evaluate(f"""() => {{
                    const el = document.querySelector('input[placeholder*="账号"]')
                            || document.querySelector('input[placeholder*="用户"]');
                    if (el) {{
                        el.removeAttribute('maxlength');
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, '{username}');
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }}""")
                if not username_filled:
                    _log(f"用户名输入框未找到(尝试 {attempt}/{max_retries})", "red")
                    continue
                await asyncio.sleep(0.5)

                # ── 3. 输入密码 ──
                _log("正在输入密码...", "blue")
                password_filled = await page.evaluate(f"""() => {{
                    const el = document.getElementById('inputPwd')
                            || document.querySelector('input[type="password"]');
                    if (el) {{
                        el.removeAttribute('maxlength');
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, '{password}');
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }}""")
                if not password_filled:
                    _log(f"密码输入框未找到(尝试 {attempt}/{max_retries})", "red")
                    continue
                await asyncio.sleep(0.5)

                # ── 4. 点击登录 ──
                _log("正在点击登录按钮...", "blue")
                login_button = page.get_by_role("button", name="登录")
                try:
                    await login_button.wait_for(state="visible", timeout=10000)
                except:
                    pass
                try:
                    await login_button.click(timeout=10000)
                except Exception:
                    try:
                        await login_button.click(force=True, timeout=10000)
                    except Exception:
                        _log(f"登录按钮点击失败(尝试 {attempt}/{max_retries})", "red")
                        continue

                # ── 5. 等待登录完成 ──
                _log("正在等待登录完成...", "yellow")
                logged_in = False
                for i in range(60):
                    await asyncio.sleep(1)
                    # 检查密码错误
                    try:
                        body = await page.locator("body").inner_text(timeout=2000)
                        for err_msg in ["用户认证失败", "认证失败", "密码错误", "账号或密码", "请检查"]:
                            if err_msg in body:
                                _log(f"登录失败: {err_msg}", "red")
                                return
                    except:
                        pass
                    if "/sys/#/login" not in page.url:
                        try:
                            await page.goto("https://u.ccb.com/portal/#/study",
                                            wait_until="networkidle", timeout=15000)
                        except:
                            pass
                        await page.wait_for_timeout(3000)
                        if "/sys/#/login" not in page.url:
                            logged_in = True
                            break

                if logged_in:
                    _log("登录成功", "green")
                    return

                _log(f"登录超时(尝试 {attempt}/{max_retries})", "red")

            except Exception as e:
                _log(f"自动登录失败(尝试 {attempt}/{max_retries}): {e}", "red")

        raise Exception(f"登录失败，已重试 {max_retries} 次")

    async def get_workshops(self, page: Page) -> List[Dict]:
        """获取专题班列表 - 从.card结构提取所有专题班"""
        workshops = []
        try:
            console.print("正在获取专题班列表...", style="blue")
            
            # 等待专题班列表容器加载
            # await page.wait_for_selector(".workshop-content-list", timeout=10000)
            try:
                await page.wait_for_selector(".workshop-content-list", timeout=10000)
            except:
                pass
            
            # 方法1：从 workshop-content-list 中提取卡片
            # 注意：DOM 结构为 .workshop-content-list > ul > li.clearfix（中间有ul层）
            cards = await page.locator(".workshop-content-list li.clearfix").all()
            console.print(f"找到 {len(cards)} 个专题班卡片元素", style="green")
            
            for card in cards:
                try:
                    # 提取标题
                    title_el = card.locator(".workshop-list-content-title")
                    title_text = await title_el.inner_text(timeout=3000)
                    
                    # 提取课程数和学时
                    info_spans = await card.locator(".workshop-list-content span").all()
                    course_count = ""
                    study_hours = ""
                    for span in info_spans:
                        text = (await span.inner_text()).strip()
                        if "总课程" in text:
                            course_count = text
                        elif "学时" in text:
                            study_hours = text
                    
                    # 提取报名状态
                    enroll_status = ""
                    try:
                        status_el = card.locator(".border-ing, .border-end")
                        enroll_status = await status_el.inner_text(timeout=2000)
                    except:
                        pass

                    # 报名已结束，直接跳过
                    if "已结束" in enroll_status or "报名截止" in enroll_status:
                        continue

                    # 提取详情页链接
                    detail_link = ""
                    try:
                        link_el = card.locator("a").first
                        href = await link_el.get_attribute("href")
                        if href:
                            detail_link = href
                    except:
                        pass
                    
                    workshops.append({
                        "title": title_text.strip(),
                        "course_count": course_count,
                        "study_hours": study_hours,
                        "enroll_status": enroll_status.strip(),
                        "detail_link": detail_link,
                        "element": card
                    })
                except Exception as e2:
                    pass
            
            # 方法2：如果没有找到卡片，尝试从<a>标签提取（兜底）
            if not workshops:
                console.print("卡片提取未找到结果，改用链接匹配...", style="yellow")
                link_elements = await page.get_by_role("link").all()
                for link in link_elements:
                    try:
                        text = await link.inner_text()
                        if text and len(text.strip()) > 3:
                            text_clean = text.strip()[:100]
                            href = await link.get_attribute("href") or ""
                            workshops.append({
                                "title": text_clean,
                                "course_count": "",
                                "study_hours": "",
                                "enroll_status": "",
                                "detail_link": href,
                                "element": link
                            })
                    except:
                        pass
            
            console.print(f"共获取 {len(workshops)} 个专题班", style="green")
            
        except Exception as e:
            console.print("获取专题班列表失败", style="red")
            import traceback
            traceback.print_exc()
        
        return workshops

    async def go_to_next_page(self, page: Page) -> bool:
        """翻到下一页 - 检查按钮是否可用（含frame检测）"""
        try:
            console.print("正在查找下一页按钮...", style="blue")

            # 先等待分页元素出现
            try:
                await page.wait_for_selector("span.pagetext, .pager_manu, .pageinfo, .pagination", timeout=8000)
            except:
                debug("等待分页元素超时")

            # 收集所有可搜索的上下文（主页面 + 所有frame）
            search_contexts = [("main", page)]
            for frame in page.frames:
                if frame != page.main_frame:
                    search_contexts.append((f"frame:{frame.url[:60]}", frame))
            debug(f"搜索上下文: {len(search_contexts)} 个 ({', '.join(c[0] for c in search_contexts)})")

            # 分页按钮可能是中文"下一页"或英文"Next"
            NEXT_TEXTS = ["下一页", "Next"]

            # 方式1: 在分页区域找"下一页/Next"
            try:
                page_container = page.locator("div.pager_manu, .pageinfo, .pagination, [class*=page]:not(.pageheader):not(.homepage_layout)")
                container_count = await page_container.count()
                debug(f"分页容器: {container_count} 个")

                if container_count > 0:
                    for nxt in NEXT_TEXTS:
                        next_btn = page_container.first.locator(f"text={nxt}")
                        btn_count = await next_btn.count()
                        debug(f"方式1: 找到 {btn_count} 个'{nxt}'元素")
                        if btn_count > 0:
                            btn_class = await next_btn.first.get_attribute("class") or ""
                            debug(f"方式1: class=[{btn_class}]")
                            if "disable" not in btn_class:
                                await next_btn.first.click()
                                await page.wait_for_timeout(5000)
                                console.print("已翻到下一页", style="green")
                                return True
                            else:
                                console.print("下一页按钮不可用（disable），已到最后一页", style="yellow")
                                return False
            except Exception as e1:
                debug(f"方式1异常: {e1}")

            # 方式2: 查找span.pagetext的"下一页/Next"元素（在所有上下文中搜索）
            for ctx_name, ctx in search_contexts:
                for nxt in NEXT_TEXTS:
                    try:
                        next_spans = ctx.locator("span.pagetext").filter(has_text=nxt)
                        sc = await next_spans.count()
                        debug(f"方式2[{ctx_name}]: span.pagetext '{nxt}' 找到 {sc} 个")
                        if sc > 0:
                            cls = await next_spans.first.get_attribute("class") or ""
                            visible = await next_spans.first.is_visible()
                            debug(f"方式2[{ctx_name}]: class=[{cls}] visible={visible}")
                            if "disable" not in cls and visible:
                                await next_spans.first.click()
                                await page.wait_for_timeout(5000)
                                console.print("已翻到下一页", style="green")
                                return True
                    except Exception as e2:
                        debug(f"方式2[{ctx_name}]异常: {e2}")

            # 方式3: 直接查找可点击的"下一页/Next"元素（在所有上下文中搜索）
            for ctx_name, ctx in search_contexts:
                for nxt in NEXT_TEXTS:
                    try:
                        next_els = ctx.locator("a, button, span, li").filter(has_text=nxt)
                        count = await next_els.count()
                        debug(f"方式3[{ctx_name}]: 找到 {count} 个含'{nxt}'的元素")
                        for i in range(count):
                            el = next_els.nth(i)
                            cls_str = (await el.get_attribute("class")) or ""
                            is_disabled = await el.get_attribute("disabled")
                            has_disable = "disable" in cls_str
                            is_visible = await el.is_visible()
                            tag = await el.evaluate("el => el.tagName")
                            debug(f"  [{i}] tag={tag} class=[{cls_str}] visible={is_visible} disabled={is_disabled}")
                            if is_visible and not is_disabled and not has_disable:
                                await el.click()
                                await page.wait_for_timeout(5000)
                                console.print("已翻到下一页", style="green")
                                return True
                    except Exception as e3:
                        debug(f"方式3[{ctx_name}]异常: {e3}")

            # 方式4: 兜底 - dump页面中所有含"下一页/Next"的元素
            for nxt in NEXT_TEXTS:
                try:
                    page_els = page.locator("*").filter(has_text=nxt)
                    pc = await page_els.count()
                    debug(f"方式4(兜底): 页面中共 {pc} 个含'{nxt}'的元素")
                    for i in range(min(pc, 10)):
                        el = page_els.nth(i)
                        tag = await el.evaluate("el => el.tagName")
                        cls_str = (await el.get_attribute("class")) or ""
                        txt = (await el.inner_text())[:50]
                        debug(f"  [{i}] <{tag}> class=[{cls_str}] text=[{txt}]")
                except Exception as e4:
                    debug(f"方式4异常: {e4}")

            # 方式5: dump所有frame中的分页区域innerHTML
            for ctx_name, ctx in search_contexts:
                try:
                    pager_html = await ctx.evaluate("""() => {
                        const selectors = ['.pager_manu', '.pageinfo', '.pagination',
                            '[class*=pager]', '[class*=paging]', '[class*=page_num]'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el) return sel + ': ' + el.innerHTML.substring(0, 500);
                        }
                        // 兜底: 找含"下一页"或"Next"的元素
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (el.innerText && (el.innerText.includes('下一页') || el.innerText.includes('Next')) && el.children.length < 5)
                                return 'found: <' + el.tagName + ' class="' + el.className + '">' + el.outerHTML.substring(0, 300);
                        }
                        return 'none';
                    }""")
                    debug(f"方式5[{ctx_name}]: {pager_html}")
                except Exception as e5:
                    debug(f"方式5[{ctx_name}]异常: {e5}")

            console.print("未找到可用的下一页按钮，已到最后一页", style="yellow")
            return False
        except Exception as e:
            console.print(f"翻页失败: {e}", style="yellow")
            return False

    async def display_workshops(self, workshops: List[Dict]):
        table = Table(title="专题班列表")
        table.add_column("序号", style="cyan")
        table.add_column("专题班名称", style="magenta")

        for i, workshop in enumerate(workshops, 1):
            table.add_row(str(i), workshop["title"][:60])

        console.print(table)

    async def filter_by_tags(self, page: Page) -> bool:
        """根据标签筛选专题班，返回是否成功"""
        if not self.tags_to_learn:
            return True

        console.print(f"正在筛选标签: {', '.join(self.tags_to_learn)}", style="blue")
        all_found = True

        try:
            # 等待标签树加载
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_selector("ul.tag-tree-list", timeout=15000)
            except:
                debug("标签树未加载，继续尝试...")

            for tag in self.tags_to_learn:
                console.print(f"查找标签: {tag}", style="blue")

                found = False

                # 方法1：在 tag-tree-list 中查找 span.single-tag 匹配文本
                for attempt in range(3):
                    try:
                        all_tags = page.locator("ul.tag-tree-list span.single-tag")
                        cnt = await all_tags.count()
                        debug(f"tag-tree-list: {cnt} spans, URL: {page.url}")
                        if cnt == 0:
                            # 打印页面结构帮助排查
                            try:
                                body = await page.locator("body").inner_text(timeout=3000)
                                debug(f"页面内容前200字: {body[:200]}")
                            except:
                                pass
                        for i in range(cnt):
                            text = (await all_tags.nth(i).inner_text()).strip()
                            if text == tag:
                                console.print(f"  找到匹配标签: {text}", style="green")
                                await all_tags.nth(i).click()
                                await page.wait_for_timeout(3000)
                                console.print(f"  ✓ 已点击标签: {tag}", style="green")
                                found = True
                                break
                        if found:
                            break
                    except Exception as e1:
                        console.print(f"  方法1尝试 {attempt+1} 失败: {e1}", style="yellow")
                    await page.wait_for_timeout(2000)

                if found:
                    continue

                # 方法2：在全页面范围找匹配文本的可见clickable元素
                for attempt in range(3):
                    try:
                        console.print(f"  方法2: 页面搜索标签...", style="blue")
                        candidates = page.locator("span, div, li, a").filter(has_text=tag)
                        cc = await candidates.count()
                        console.print(f"  找到 {cc} 个候选元素", style="blue")
                        for j in range(min(cc, 20)):
                            try:
                                t = (await candidates.nth(j).inner_text()).strip()
                                if t == tag and await candidates.nth(j).is_visible():
                                    console.print(f"  找到可见标签元素: {tag}", style="green")
                                    await candidates.nth(j).click()
                                    await page.wait_for_timeout(3000)
                                    console.print(f"  ✓ 已点击标签: {tag}", style="green")
                                    found = True
                                    break
                            except:
                                pass
                        if found:
                            break
                    except:
                        pass
                    await page.wait_for_timeout(2000)

                if not found:
                    console.print(f"  ✗ 未找到标签: {tag}", style="red")
                    all_found = False

            if all_found:
                console.print("标签筛选完成", style="green")
            else:
                console.print("部分标签未找到，筛选可能不完整", style="yellow")
        except Exception as e:
            console.print(f"标签筛选失败: {e}", style="red")
            all_found = False

        return all_found




    async def _set_lowest_quality(self, page: Page):
        """静音 + 最低画质 + 2倍速度（JS直接操作 + UI点击双重保障）"""

        # 1) 静音（JS直接设置，最可靠）
        try:
            await page.evaluate("() => { const v = document.querySelector('video'); if (v) v.muted = true; }")
        except:
            pass

        # 2) 画质：先尝试JS，失败再UI点击
        for attempt in range(2):
            try:
                if attempt == 0:
                    # JS方式：遍历画质选项找最低的并点击
                    result = await page.evaluate("""() => {
                        const btn = document.querySelector('.current-quality');
                        if (!btn) return 'no-btn';
                        btn.click();
                        return 'clicked';
                    }""")
                    if result == 'no-btn':
                        break
                    await page.wait_for_timeout(1000)
                    # 点击最后一个选项（最低画质）
                    clicked = await page.evaluate("""() => {
                        const items = document.querySelectorAll('.quality-list li');
                        if (items.length > 1) {
                            items[items.length - 1].click();
                            return items[items.length - 1].innerText.trim();
                        }
                        return null;
                    }""")
                    if clicked:
                        debug(f"画质(JS): 选 {clicked}")
                        await page.wait_for_timeout(1500)
                        break
                else:
                    # UI方式：hover展开控制栏再点击
                    try:
                        await page.locator(".prism-player, video, #player_area").first.hover()
                        await page.wait_for_timeout(500)
                    except:
                        pass
                    qbtn = page.locator('.current-quality').first
                    if await qbtn.count() > 0:
                        await qbtn.click(force=True)
                        await page.wait_for_timeout(1000)
                        # 等待下拉菜单可见
                        try:
                            await page.locator('.quality-list li').last.wait_for(state="visible", timeout=3000)
                        except:
                            pass
                        items = page.locator('.quality-list li')
                        cnt = await items.count()
                        if cnt > 1:
                            lowest = items.nth(cnt - 1)
                            text = await lowest.inner_text()
                            debug(f"画质(UI): {cnt}个, 选 {text.strip()}")
                            await lowest.click(force=True)
                            await page.wait_for_timeout(1500)
            except Exception as _qe:
                debug(f"画质异常(attempt={attempt}): {_qe}")

        # 3) 倍速：必须用UI点击（服务器需要收到事件才能正确计算进度）
        for attempt in range(3):
            try:
                # hover展开控制栏
                try:
                    await page.locator(".prism-player, video, #player_area").first.hover()
                    await page.wait_for_timeout(500)
                except:
                    pass
                rate_btn = page.locator('.current-rate').first
                if await rate_btn.count() == 0:
                    debug("倍速: 未找到速率按钮")
                    break
                cur = (await rate_btn.inner_text()).strip()
                if cur.startswith('2'):
                    debug("倍速: 已经是2x")
                    break
                # 点击展开下拉
                await rate_btn.click(force=True)
                await page.wait_for_timeout(800)
                # 等待选项可见
                opt = page.locator('li[data-rate="2.0"]').first
                try:
                    await opt.wait_for(state="visible", timeout=3000)
                except:
                    pass
                if await opt.count() > 0:
                    await opt.click(force=True)
                    debug(f"倍速: 已设为2x (attempt={attempt})")
                    await page.wait_for_timeout(500)
                    break
            except Exception as _se:
                debug(f"倍速异常(attempt={attempt}): {_se}")
                await page.wait_for_timeout(1000)


    async def _check_video_progress(self, page: Page) -> float:
        # 检查当前课程的平台播放进度。
        # 本地 currentTime 只用于停滞检测，不能代替平台上报结果。
        try:
            pct = await page.evaluate('''() => {
                const el = document.querySelector('.el-progress__text');
                if (el) {
                    const t = el.innerText.trim().replace('%', '');
                    const n = parseFloat(t);
                    if (!isNaN(n)) return n;
                }
                return -1;
            }''')
            if isinstance(pct, (int, float)) and pct >= 0:
                return float(pct)
        except:
            pass
        return -1

    async def find_and_play_video(self, page: Page, worker_id: int, progress_callback=None,
                                  course_type="", cancel_event=None):
        # 查找并播放视频，监控进度到100%
        # O4：3 分钟进度无变化判定卡住，提前放弃（替代最长 20 分钟空转）
        try:
            debug(f"[工作线程 {worker_id+1}] phase=video_probe start {_page_debug_state(page)}")
            if ("/course/#/detail/" in page.url
                    and not await page.query_selector("video, audio, .prism-player")):
                debug(f"[工作线程 {worker_id+1}] phase=video_probe still_on_detail {_page_debug_state(page)}")
                return False

            # 等待视频元素出现，找不到就刷新重试
            video_found = False
            video_selectors = ["video", "audio", "[class*='video']", "[class*='audio']", ".prism-player"]
            # Course pages may take a while to hydrate their embedded player.
            # Keep the longer retry window used by the previously reliable flow.
            max_load_refreshes = 9
            for refresh_attempt in range(max_load_refreshes + 1):
                if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                    # 用户停止或心跳超时触发重试，立即结束当前播放。
                    return False
                for sel in video_selectors:
                    try:
                        v = await page.query_selector(sel)
                        if v:
                            debug(f"[工作线程 {worker_id+1}] 找到视频元素: {sel}")
                            video_found = True
                            break
                    except:
                        pass
                if video_found:
                    break
                if refresh_attempt < max_load_refreshes:
                    debug(f"[工作线程 {worker_id+1}] phase=video_probe player_not_found; refresh={refresh_attempt+1}/{max_load_refreshes}; {_page_debug_state(page)}")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(5000)
                        debug(f"[工作线程 {worker_id+1}] phase=video_probe refresh_complete={refresh_attempt+1}; {_page_debug_state(page)}")
                    except Exception as exc:
                        debug(f"[工作线程 {worker_id+1}] phase=video_probe refresh_error={type(exc).__name__}: {_safe_debug_error(exc)}; {_page_debug_state(page)}")

            if not video_found:
                ctype = (course_type or "").lower()
                if any(k in ctype for k in ["图书", "book", "document", "doc", "pdf", "图文"]):
                    debug(f"[工作线程 {worker_id+1}] 图书类课程，视为完成")
                    return True
                # 无播放器但页面显示已完成（如已学过、图文类）→ 视为完成
                try:
                    body_text = await page.locator("body").inner_text(timeout=3000)
                    if any(k in body_text for k in ["已学习", "已完成", "学习完成", "已看完"]):
                        debug(f"[工作线程 {worker_id+1}] 页面显示已学习，视为完成")
                        return True
                except:
                    pass
                try:
                    media_snapshot = await page.evaluate("""() => ({
                        readyState: document.readyState,
                        video: document.querySelectorAll('video').length,
                        audio: document.querySelectorAll('audio').length,
                        iframes: document.querySelectorAll('iframe').length,
                        mediaLike: document.querySelectorAll('[class*=video], [class*=audio], .prism-player').length,
                        loginForm: !!document.querySelector('input[type=password]'),
                        bodyChars: document.body ? document.body.innerText.length : 0
                    })""")
                except Exception as exc:
                    media_snapshot = {"snapshotError": type(exc).__name__}
                debug(f"[工作线程 {worker_id+1}] phase=video_probe exhausted; attempts={max_load_refreshes+1}; selectors={video_selectors}; dom={media_snapshot}; {_page_debug_state(page)}")
                return False

            # 确保视频开始播放（JS控制）
            await self._ensure_video_playing(page)

            await self._set_lowest_quality(page)

            # O4：进度停滞检测 —— 约3分钟无进展 → 刷新页面重试播放；
            # 多次刷新仍卡住才放弃（替代原来最长20分钟空转）。
            # "无进展" = 平台百分比 与 本地播放时间 都未变化（避免误判平台显示滞后）
            STALL_CHECKS = 18  # 18 × 10s ≈ 3 分钟
            MAX_REFRESHES = 3  # 卡住后最多刷新重试次数
            refresh_count = 0
            last_pct = -1.0
            last_local_time = -1.0
            stall_count = 0
            for check in range(120 * (MAX_REFRESHES + 1)):
                if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                    return False
                await asyncio.sleep(10)
                # 每次检查进度时，确保视频还在播放
                await self._ensure_video_playing(page)
                progress = await self._check_video_progress(page)
                local_time = await self._check_video_time(page)
                advanced = False
                if isinstance(progress, (int, float)) and progress >= 0:
                    if progress_callback:
                        progress_callback(progress)
                    if progress >= 100:
                        return True
                    if progress != last_pct:
                        last_pct = progress
                        advanced = True
                if local_time >= 0 and local_time != last_local_time:
                    last_local_time = local_time
                    advanced = True
                # 没有平台进度条时，只有页面明确报告完成才能算成功；
                # 本地播放到末尾本身不能证明服务器已收到学习进度。
                if progress < 0 and local_time >= 0:
                    try:
                        done = await page.evaluate("""() => {
                            const text = (document.body && (document.body.innerText || '')) || '';
                            return /学习完成|已完成|已学习|已看完|恭喜您/.test(text);
                        }""")
                        if done:
                            return True
                    except Exception:
                        pass
                if advanced:
                    stall_count = 0
                    continue
                # 平台%与本地时间都没动 → 停滞
                stall_count += 1
                if stall_count >= STALL_CHECKS:
                    if refresh_count < MAX_REFRESHES:
                        refresh_count += 1
                        debug(f"[工作线程 {worker_id+1}] 视频停滞约3分钟，第{refresh_count}次刷新重试")
                        if not await self._refresh_video_page(page, worker_id):
                            return False
                        last_pct = -1.0
                        last_local_time = -1.0
                        stall_count = 0
                    else:
                        debug(f"[工作线程 {worker_id+1}] 视频停滞，{MAX_REFRESHES}次刷新后仍卡住，放弃")
                        return False

            return True
        except Exception as e:
            debug(f"[工作线程 {worker_id+1}] phase=video_play exception={type(e).__name__}: {_safe_debug_error(e)}; {_page_debug_state(page)}")
            return False

    # 训练营课程页的媒体状态快照。
    # 注意：学习进度条（.traincamp-progress-data）只在 componentCode 为 cuVideo/cuAudio
    # 时渲染；cuCase（课程包/案例）会渲染同一个阿里播放器却没有任何进度条，
    # 所以这里以 DOM 为准发现播放器，进度同时读「页面 / 组件 / 服务器」三个来源。
    _TRAINCAMP_MEDIA_JS = r"""() => {
        const doneEl = document.querySelector('.traincamp-journey-study-done span');
        const doneText = doneEl ? (doneEl.innerText || '').trim() : '';
        const pageDone = /恭喜您，已完成/.test(doneText);

        // 页面级组件列表（平台返回的 videoProgress / progress / finishFlag）
        // 目录项元素不一定存在（分组页/空页），所以多试几个锚点再向上找组件实例
        let pageVm = null;
        const anchors = [
            ...document.querySelectorAll('[id^="traincamp-journey-module-item-"]'),
            ...document.querySelectorAll('.traincamp-journey-module-list, .traincamp-journey, #app')
        ];
        for (const node of anchors) {
            let cur = node;
            while (cur) {
                if (cur.__vue__ && Array.isArray(cur.__vue__.compMapList)) { pageVm = cur.__vue__; break; }
                cur = cur.parentElement;
            }
            if (pageVm) break;
        }
        const itemById = {};
        const itemByClass = {};
        if (pageVm) {
            (pageVm.compMapList || []).forEach((item, i) => {
                if (item && item.id !== undefined) itemById[String(item.id)] = item;
                itemByClass['comp-item-' + i] = item;
            });
        }

        const wrappers = [...document.querySelectorAll('[class*="comp-item-"]')].filter(el => {
            const cn = el.className;
            return typeof cn === 'string' && /(^|\s)comp-item-\d+(\s|$)/.test(cn);
        });

        const seen = new Set();
        const components = [];
        const allCodes = [];
        for (const wrapper of wrappers) {
            const cls = (String(wrapper.className).match(/comp-item-\d+/) || [''])[0];
            if (!cls || seen.has(cls)) continue;
            seen.add(cls);

            const playerEl = wrapper.querySelector('[id^="player-con"]');
            let mediaEl = wrapper.querySelector('video, audio');
            if (!mediaEl && playerEl) mediaEl = playerEl.querySelector('video, audio');

            // 组件 Vue 实例：视频/音频组件都带 player 或 resourceDetail
            let vm = null;
            const nodes = [wrapper, ...wrapper.querySelectorAll('*')];
            for (const node of nodes) {
                const v = node.__vue__;
                if (!v) continue;
                if ((v.player && typeof v.player.play === 'function') || v.resourceDetail) { vm = v; break; }
            }

            const item = (vm && vm.id !== undefined && itemById[String(vm.id)]) || itemByClass[cls] || null;
            if (item && item.componentCode) allCodes.push(item.componentCode);

            // 组件类型决定用哪种方式完成它（与页面自身的上报逻辑对齐）：
            //   media   视频/音频/直播     → 播到平台阈值
            //   book    图书「开始阅读」    → 点一下即上报 componentDone
            //   outlink 外链「由此进入」    → 点一下即上报 componentDone
            //   read    图文/图片/外链课程  → 滚进视口即上报（组件自带 scrollIntoView 判定）
            //   exam    随堂测试           → 走自动答题流程
            //   manual  作业/投票/讨论      → 需要人工，跳过
            // 组件根元素本身就是 .cuWeb-xxx（querySelector 不匹配自身），所以要先 matches 再看后代
            const has = (sel) => {
                try {
                    if (wrapper.matches && wrapper.matches(sel)) return true;
                } catch (e) {}
                return !!wrapper.querySelector(sel);
            };
            let kind = 'other';
            if (mediaEl || playerEl) kind = 'media';
            else if (has('.cuWeb-book-btn')) kind = 'book';
            else if (has('.cuWeb-outLink-img-box')) kind = 'outlink';
            else if (has('.cuWeb-exam')) kind = 'exam';
            else if (has('.cuWeb-text, .cuWeb-picture, iframe')) kind = 'read';
            else if (has('.cuWeb-workPlan, .cuWeb-assigntask, .cuWeb-vote, .cuWeb-compr')) kind = 'manual';
            else if (item && /Assigntask|WorkPlan|Vote|Compr/i.test(item.componentCode || '')) kind = 'manual';
            else if (has('.cuWeb-comment, .cuWeb-discuss')) kind = 'other';

            const domPctEl = wrapper.querySelector('.traincamp-progress-data');
            const domPct = domPctEl ? parseFloat((domPctEl.innerText || '').replace('%', '').trim()) : NaN;

            let hasPlayerApi = false, playerTime = -1, playerDuration = 0;
            if (vm && vm.player && typeof vm.player.getCurrentTime === 'function') {
                hasPlayerApi = true;
                try { playerTime = Number(vm.player.getCurrentTime()); } catch (e) { playerTime = -1; }
                try { playerDuration = Number(vm.player.getDuration()); } catch (e) { playerDuration = 0; }
            }
            const mediaTime = (mediaEl && isFinite(mediaEl.currentTime)) ? mediaEl.currentTime : -1;
            const mediaDuration = (mediaEl && isFinite(mediaEl.duration)) ? mediaEl.duration : 0;

            const candidates = [
                Number.isFinite(domPct) ? domPct : null,
                (item && Number.isFinite(Number(item.videoProgress))) ? Number(item.videoProgress) : null,
                (item && Number.isFinite(Number(item.progress))) ? Number(item.progress) : null,
                (vm && Number.isFinite(Number(vm.studySchedule))) ? Number(vm.studySchedule) : null
            ].filter(v => v !== null);
            const platformPct = candidates.length ? Math.max.apply(null, candidates) : 0;

            components.push({
                componentClass: cls,
                kind: kind,
                componentCode: (item && item.componentCode) || '',
                componentId: (vm && vm.id !== undefined) ? String(vm.id) : '',
                resourceName: (vm && vm.resourceDetail && vm.resourceDetail.resourceName) || '',
                status: (vm && vm.status) || '',
                threshold: (vm && Number.isFinite(Number(vm.videoProcess)) && Number(vm.videoProcess) > 0)
                    ? Number(vm.videoProcess) : 95,
                platformPct: platformPct,
                finishedFlag: !!wrapper.querySelector('.finish-flag-img') ||
                    !!(item && (Number(item.progress) >= 100 || item.finishFlag === 1 || item.finishFlag === '1')),
                hasPlayerEl: !!playerEl,
                playerId: playerEl ? playerEl.id : '',
                hasPlayerApi: hasPlayerApi,
                hasMedia: !!mediaEl,
                mediaTime: mediaTime,
                mediaDuration: mediaDuration,
                playerTime: playerTime,
                playerDuration: playerDuration,
                paused: mediaEl ? !!mediaEl.paused : true,
                ended: mediaEl ? !!mediaEl.ended : false,
                readyState: mediaEl ? mediaEl.readyState : -1,
                networkState: mediaEl ? mediaEl.networkState : -1,
                errorCode: (mediaEl && mediaEl.error) ? mediaEl.error.code : 0,
                mediaSrc: (mediaEl && (mediaEl.currentSrc || mediaEl.src)) || '',
                hasPlayUrl: !!(vm && vm.videoInfo && vm.videoInfo.url),
                coverVisible: !!(wrapper.querySelector('.prism-cover, .prism-big-play-btn')
                    && !wrapper.querySelector('.prism-cover[style*="display: none"]'))
            });
        }
        return {
            pageDone: pageDone,
            doneText: doneText,
            isPreview: !!(pageVm && pageVm.isPreview),
            courseId: pageVm ? pageVm.courseId : '',
            // 页面组件清单（判断「真的没有组件」还是「组件没渲染出来」）
            componentCount: pageVm ? (pageVm.compMapList || []).length : -1,
            mapList: pageVm ? (pageVm.compMapList || []).map((item, i) => ({
                i: i,
                id: String(item.id),
                code: item.componentCode || '',
                name: item.componentName || '',
                progress: Number(item.progress) || 0,
                videoProgress: Number(item.videoProgress) || 0,
                hasComponents: Array.isArray(item.componentList) && item.componentList.length > 0
            })) : [],
            wrapperCount: wrappers.length,
            playerCount: document.querySelectorAll('[id^="player-con"]').length,
            mediaCount: document.querySelectorAll('video, audio').length,
            iframes: [...document.querySelectorAll('iframe')].map(f => f.src).filter(Boolean).slice(0, 5),
            codes: allCodes,
            components: components
        };
    }"""

    # 播放：优先用组件自己的播放器 API（阿里播放器/cyberplayer），再兜底 HTML5 元素
    _TRAINCAMP_PLAY_JS = r"""(componentClass) => {
        const wrapper = document.querySelector('.' + componentClass);
        if (!wrapper) return {ok: false, reason: 'wrapper-missing'};
        const out = {ok: false, via: '', clicked: false, muted: false, reason: ''};

        const box = wrapper.querySelector('[id^="player-con"]');
        const media = (box && box.querySelector('video, audio')) || wrapper.querySelector('video, audio');
        const isPaused = !media || media.paused || media.ended;

        // 阿里播放器的封面/大播放按钮会挡住 video，先点掉（仅在暂停时点，避免切到暂停）
        if (isPaused) {
            const bigBtn = wrapper.querySelector('.prism-big-play-btn, .prism-play-btn');
            if (bigBtn) { try { bigBtn.click(); out.clicked = true; } catch (e) {} }
        }

        let vm = null;
        for (const node of [wrapper, ...wrapper.querySelectorAll('*')]) {
            const v = node.__vue__;
            if (v && v.player && typeof v.player.play === 'function') { vm = v; break; }
        }
        if (vm) {
            try { vm.player.play(); out.via = 'player-api'; out.ok = true; } catch (e) { out.reason = String(e); }
        }

        if (media) {
            if (media.paused || media.ended) {
                let promise = null;
                try { media.muted = false; } catch (e) {}
                try { promise = media.play(); } catch (e) { out.reason = String(e); }
                if (promise && promise.catch) {
                    promise.catch(() => {
                        // 自动播放被拦截时退回静音播放（平台按播放时长计进度，静音同样有效）
                        try { media.muted = true; out.muted = true; media.play(); } catch (e) {}
                    });
                }
                out.ok = true;
                out.via = out.via ? out.via + '+element' : 'element';
            } else {
                out.ok = true;
                out.via = out.via ? out.via + '+playing' : 'playing';
            }
        }
        const cover = wrapper.querySelector('.prism-cover');
        if (cover) { try { cover.style.display = 'none'; } catch (e) {} }
        if (!out.ok) out.reason = out.reason || '未找到播放器元素';
        return out;
    }"""

    async def find_and_play_trainingcamp_video(self, page: Page, worker_id: int,
                                               progress_callback=None, log_callback=None,
                                               cancel_event=None):
        """学习训练营课程页里的视频/音频组件，由平台确认课程完成。

        组件类型不止 cuVideo/cuAudio（cuCase 课程包同样渲染阿里播放器且没有进度条），
        因此按 DOM 发现播放器；进度取「页面 compMapList 的 videoProgress / 组件
        studySchedule / 进度条文本」的最大值作为平台进度，本地播放位置只用于卡顿判断。
        """
        prefix = f"[工作线程 {worker_id+1}] 训练营"
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))

        async def snapshot():
            try:
                return await page.evaluate(self._TRAINCAMP_MEDIA_JS)
            except Exception as e:
                debug(f"{prefix} 读取页面状态失败: {e}")
                return {"pageDone": False, "doneText": "", "components": [], "codes": []}

        def media_components(state):
            return [c for c in (state.get("components") or [])
                    if c.get("hasMedia") or c.get("hasPlayerEl") or c.get("hasPlayerApi")]

        def component_done(component):
            """该组件是否已被平台记录完成"""
            return _component_finished(component)

        def components_of_kind(state, *kinds):
            return [c for c in (state.get("components") or [])
                    if c.get("kind") in kinds and not component_done(c)]

        def report_progress(state):
            components = media_components(state)
            if not progress_callback or not components:
                return
            total = sum(
                min(1.0, float(item.get("platformPct") or 0) / float(item.get("threshold") or 95))
                for item in components
            )
            # 只有训练营页面确认完成后才报告 100%，避免课程行显示成功但平台未完成。
            progress_callback(min(99.0, total * 100 / len(components)))

        def local_position(component):
            values = []
            for key in ("mediaTime", "playerTime"):
                try:
                    value = float(component.get(key))
                except (TypeError, ValueError):
                    continue
                if value == value:  # 排除 NaN
                    values.append(value)
            return max(values) if values else -1.0

        def local_duration(component):
            values = []
            for key in ("mediaDuration", "playerDuration"):
                try:
                    value = float(component.get(key))
                except (TypeError, ValueError):
                    continue
                if value == value and value > 0:
                    values.append(value)
            return max(values) if values else 0.0

        def diagnose(components, state):
            if not components:
                codes = "、".join(state.get("codes") or []) or "无"
                iframes = "、".join(state.get("iframes") or [])
                kinds = {c.get("kind") for c in (state.get("components") or [])}
                if kinds & {"book", "outlink", "read"}:
                    # 这页是图书/图文/外链，本来就没有播放器，不算异常
                    debug(f"{prefix} 本页没有视频组件，改走点击/滚动完成（{codes}）")
                else:
                    _log(f"{prefix} 未发现可播放的视频/音频组件（组件类型：{codes}）"
                         f"{'，页面含 iframe' if iframes else ''}"
                         f"{'，当前为预览模式（preview=1）不记录进度' if state.get('isPreview') else ''}",
                         "yellow")
                debug(f"{prefix} 组件类型: {codes}；iframe: {iframes}；URL: {page.url}")
            for c in components:
                debug(f"{prefix} 组件 {c.get('componentClass')} [{c.get('componentCode')}] "
                      f"{c.get('resourceName')} status={c.get('status')} "
                      f"playerEl={c.get('hasPlayerEl')} media={c.get('hasMedia')} "
                      f"readyState={c.get('readyState')} networkState={c.get('networkState')} "
                      f"err={c.get('errorCode')} 有播放地址={c.get('hasPlayUrl')} "
                      f"src={str(c.get('mediaSrc') or '')[:80]} "
                      f"平台进度={c.get('platformPct')}")
            if not components:
                debug(f"{prefix} 页面组件清单: {state.get('mapList')}；"
                      f"comp-item 容器数={state.get('wrapperCount')} "
                      f"播放器容器数={state.get('playerCount')} media={state.get('mediaCount')}")

        async def handle_empty_page(state):
            """列表页/分组标题页：自身没有组件，交给页面的「完成学习」由平台判定。"""
            map_list = state.get("mapList") or []
            if state.get("componentCount") == 0 or (state.get("componentCount") == -1 and not map_list):
                _log(f"{prefix} 该课程页没有学习组件（可能是分组标题页），"
                     f"直接提交「完成学习」由平台判定", "yellow")
                if await self._confirm_trainingcamp_finish(page, worker_id):
                    if progress_callback:
                        progress_callback(100)
                    return True
                _log(f"{prefix} 平台未确认该页完成", "yellow")
                return False
            # 组件清单非空却没有渲染出来：属于页面/组件库加载失败，不能假装学完
            _log(f"{prefix} 页面有 {state.get('componentCount')} 个组件但没有渲染出播放器"
                 f"（comp-item 容器 {state.get('wrapperCount')} 个），本次跳过", "red")
            debug(f"{prefix} 组件清单: {map_list}")
            return False

        def at_threshold(component):
            return _component_finished(component)

        try:
            if not re.search(r"#/traincamp/study/", page.url):
                debug(f"{prefix} 当前页面不是训练营课程页: {page.url}")
                return False

            # 等 Vue 路由及训练营组件完成渲染。
            # 注意：页面被平台标记「已完成」不代表视频真的看过（例如考试先通过、
            # 或上一次只考试没学视频），所以只对「已经达标的组件」放行，
            # 没达标的仍然要播放，否则会出现「只考试、没看视频」。
            state = await snapshot()
            for poll in range(30):
                components = media_components(state)
                if components and all(at_threshold(c) for c in components):
                    if progress_callback:
                        progress_callback(100)
                    debug(f"{prefix} 页面组件均已达标，无需重复学习")
                    return True
                if state.get("pageDone") and not components:
                    if progress_callback:
                        progress_callback(100)
                    debug(f"{prefix} 页面已由平台标记完成（没有媒体组件）")
                    return True
                if components:
                    break
                # 组件已经渲染出来了，但一个能自动完成的都没有（例如只有交作业的页面）：
                # 再等下去也没有播放器会出现，直接进分类逻辑，别白等 30 秒占着 worker
                if poll >= 1 and (state.get("wrapperCount") or 0) > 0 and not any(
                        c.get("kind") in ("media", "book", "outlink", "read", "exam")
                        for c in (state.get("components") or [])):
                    debug(f"{prefix} 页面组件已渲染但没有可自动完成的组件，提前结束等待")
                    break
                if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                    return False
                await page.wait_for_timeout(1000)
                state = await snapshot()

            components = media_components(state)
            report_progress(state)
            all_components = state.get("components") or []
            if not components:
                diagnose(components, state)
                if state.get("pageDone"):
                    if progress_callback:
                        progress_callback(100)
                    return True
                if not any(c.get("kind") in ("book", "outlink", "read", "exam")
                           for c in all_components):
                    # 没有可自动完成的组件：要么是空页/分组页（交给平台的完成学习判定），
                    # 要么只剩交作业/投票/讨论这类必须人工的组件（跳过，不报未完成）
                    if all_components:
                        codes = "、".join(sorted({(c.get("componentCode") or c.get("kind") or "")
                                                  for c in all_components}))
                        _log(f"{prefix} 本页组件都需要人工完成（{codes}），跳过", "yellow")
                        # 同步写进调试日志：这类页面本来就该人工处理，避免看起来像失败
                        debug(f"{prefix} 本页组件都需要人工完成（{codes}），跳过；URL: {page.url}")
                        return False
                    return await handle_empty_page(state)
                # 有考试组件时不在这里下结论：考试流程跑完后再由平台的「完成学习」判定
                if not any(c.get("kind") in ("book", "outlink", "read") for c in all_components):
                    _log(f"{prefix} 本页只有考试组件，交给考试流程处理", "blue")
                    return False

            if state.get("pageDone"):
                pending = [c.get("componentClass") for c in components if not at_threshold(c)]
                _log(f"{prefix} 页面已被平台标记完成，但仍有 {len(pending)} 个组件未达标，"
                     f"继续补学视频：{'、'.join(pending)}", "yellow")
            if state.get("isPreview"):
                debug(f"{prefix} 当前是预览模式，平台不会记录进度")
            diagnose(components, state)

            refresh_count = 0
            pending_components = []   # 本地已播完/停滞但平台尚未结算的组件
            for component in components:
                component_class = component.get("componentClass")
                if not component_class:
                    continue

                threshold = float(component.get("threshold") or 95)
                if float(component.get("platformPct") or 0) >= threshold or component.get("finishedFlag"):
                    continue  # 平台已记录达标，无需重复播放

                wrapper = page.locator(f".{component_class}").first
                try:
                    await wrapper.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass

                # 等待播放器与 video 元素挂载（cuCase 等组件要等接口返回播放地址）
                media_ready = False
                for poll in range(30):
                    current_state = await snapshot()
                    current_match = next((x for x in media_components(current_state)
                                          if x.get("componentClass") == component_class), None)
                    # 平台标记完成且该组件已达标才算学完；未达标要继续播
                    if current_state.get("pageDone") and at_threshold(current_match):
                        if progress_callback:
                            progress_callback(100)
                        return True
                    match = current_match
                    if match and match.get("hasMedia"):
                        component = match
                        media_ready = True
                        break
                    if match and (match.get("hasPlayerEl") or match.get("hasPlayerApi")):
                        component = match
                    # 连播放地址都没有（组件拿不到播放源，播放器永远不会挂载）：
                    # 别在这里等满 30 秒，更别进后面的停滞重试循环
                    if poll >= 2 and match and not match.get("hasMedia") \
                            and not match.get("hasPlayUrl") \
                            and (current_state.get("mediaCount") or 0) == 0:
                        _log(f"{prefix} 组件 {component_class} 没有播放地址"
                             f"（平台进度 {float(match.get('platformPct') or 0):.0f}%），跳过不空等", "yellow")
                        debug(f"{prefix} 组件 {component_class} 无播放地址: {match}")
                        component = match
                        media_ready = False
                        break
                    if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                        return False
                    await page.wait_for_timeout(1000)

                # 没有播放器可播：记为待平台结算，不再进播放/停滞循环（否则要空转十几分钟）
                if not media_ready:
                    try:
                        play_result = await page.evaluate(self._TRAINCAMP_PLAY_JS, component_class)
                    except Exception as e:
                        play_result = {"ok": False, "reason": str(e)}
                    _log(f"{prefix} 组件 {component_class} 没有挂载出播放器"
                         f"（{play_result.get('reason') or '无媒体元素'}），跳过该组件", "yellow")
                    debug(f"{prefix} 组件 {component_class} 播放结果 {play_result}")
                    if component_class not in pending_components:
                        pending_components.append(component_class)
                    continue

                try:
                    play_result = await page.evaluate(self._TRAINCAMP_PLAY_JS, component_class)
                except Exception as e:
                    play_result = {"ok": False, "reason": str(e)}
                debug(f"{prefix} 组件 {component_class} 开始播放 via={play_result.get('via')} "
                      f"{'（静音兜底）' if play_result.get('muted') else ''}")
                await page.wait_for_timeout(1500)

                last_progress = float(component.get("platformPct") or 0)
                last_local = local_position(component)
                stall_count = 0
                # 本地是否已播到阈值：一旦达成就不复位，避免播完后被重新拉起时反复重置
                local_reached = False
                local_reached_since = None
                component_complete = False
                for _ in range(120 * 4):
                    if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                        return False
                    await page.wait_for_timeout(TRAINCAMP_POLL_SECONDS * 1000)
                    current_state = await snapshot()
                    match = next((x for x in media_components(current_state)
                                  if x.get("componentClass") == component_class), None)
                    # 平台标记完成且当前组件已达标才提前收工（否则继续把视频学完）
                    if current_state.get("pageDone") and at_threshold(match):
                        if progress_callback:
                            progress_callback(100)
                        return True
                    if not match:
                        debug(f"{prefix} 页面组件在播放期间消失")
                        return False

                    pct = float(match.get("platformPct") or 0)
                    report_progress(current_state)
                    if pct >= float(match.get("threshold") or threshold) or match.get("finishedFlag"):
                        component_complete = True
                        break

                    position = local_position(match)
                    duration = local_duration(match)
                    advanced = pct != last_progress or (position >= 0 and position != last_local)
                    last_progress = pct
                    last_local = position

                    if not local_reached and duration > 0 and position > 0 \
                            and position / duration * 100 >= threshold:
                        local_reached = True
                    if match.get("ended") and position > 0:
                        local_reached = True

                    # 本地已播到阈值但平台还没结算：给平台结算时间（postProgress 有间隔），
                    # 超时后交由页面「完成学习」由平台判定，绝不再从头重播。
                    if local_reached:
                        if local_reached_since is None:
                            local_reached_since = time.time()
                        elif time.time() - local_reached_since > TRAINCAMP_LOCAL_SETTLE_SECONDS:
                            _log(f"{prefix} 组件 {component_class} 本地已播完"
                                 f"（{position:.0f}/{duration:.0f}秒），平台进度仍为 {pct:.0f}%，"
                                 f"交由「完成学习」判定", "yellow")
                            pending_components.append(component_class)
                            break

                    if advanced:
                        stall_count = 0
                        continue

                    stall_count += 1
                    # 只在「播放中途暂停」时重新拉起；已播完的媒体重播会把进度打回 0
                    if not match.get("ended") and (match.get("paused") or not match.get("hasMedia")) \
                            and not local_reached:
                        # 弹窗遮罩（视频互动问答等）会挡住播放器并暂停视频，先清掉再拉起
                        await self._clear_blocking_overlays(page, _log)
                        try:
                            await page.evaluate(self._TRAINCAMP_PLAY_JS, component_class)
                        except Exception:
                            pass
                    if stall_count >= 18:
                        if refresh_count >= 3:
                            _log(f"{prefix} 组件 {component_class} 播放进度停滞"
                                 f"（平台进度 {pct:.0f}%），交由「完成学习」判定", "yellow")
                            debug(f"{prefix} 组件 {component_class} 连续停滞，重试后仍无进度")
                            pending_components.append(component_class)
                            break
                        refresh_count += 1
                        debug(f"{prefix} 播放进度停滞，刷新课程页重试 ({refresh_count}/3)")
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=20000)
                            await page.wait_for_timeout(5000)
                            if not re.search(r"#/traincamp/study/", page.url):
                                return False
                            refreshed = await snapshot()
                            refreshed_match = next(
                                (x for x in media_components(refreshed)
                                 if x.get("componentClass") == component_class), None)
                            if refreshed_match:
                                component = refreshed_match
                            try:
                                await page.locator(f".{component_class}").first.scroll_into_view_if_needed(timeout=5000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(2000)
                            await page.evaluate(self._TRAINCAMP_PLAY_JS, component_class)
                        except Exception as e:
                            debug(f"{prefix} 刷新课程页失败: {e}")
                            return False
                        stall_count = 0

                if not component_complete and component_class not in pending_components:
                    _log(f"{prefix} 组件 {component_class} 未达到平台学习阈值", "yellow")
                    debug(f"{prefix} 组件 {component_class} 未达到平台学习阈值")
                    return False

            if pending_components:
                _log(f"{prefix} {len(pending_components)} 个组件本地已播完但平台未结算，"
                     f"交由「完成学习」由平台判定", "yellow")

            # ── 图书 / 外链：点一下即完成 ────────────────────────────────
            # 组件自身在点击时 emit componentDone → 页面调 studyHistoryCreate 记录完成，
            # 所以「逐个点开始阅读」就等于学完；点开的新标签页看完就关掉。
            state = await snapshot()
            for component in components_of_kind(state, "book", "outlink"):
                if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                    return False
                component_class = component.get("componentClass")
                is_book = component.get("kind") == "book"
                label = "开始阅读" if is_book else "由此进入"
                target = (f".{component_class} .cuWeb-book-btn" if is_book
                          else f".{component_class} .cuWeb-outLink-img-box")
                try:
                    await page.locator(f".{component_class}").first.scroll_into_view_if_needed(timeout=5000)
                    button = page.locator(target).first
                    if await button.count() == 0:
                        debug(f"{prefix} 组件 {component_class} 未找到「{label}」入口")
                        continue
                    # 页面上的遮罩（.v-modal）会拦住 pointer events，点击会一直超时：先清掉
                    await self._clear_blocking_overlays(page, _log)
                    # 点击必须恰好发生一次；expect_event 只是顺手接住 window.open 出来的标签页
                    clicked = False
                    popup = None
                    try:
                        async with page.expect_event("popup", timeout=5000) as popup_info:
                            try:
                                await button.click(timeout=3500)
                                clicked = True
                            except Exception as e:
                                # 遮罩清不掉时，用 JS 触发组件自身的 click（平台的处理逻辑一致）
                                debug(f"{prefix} 组件 {component_class} 常规点击失败"
                                      f"({type(e).__name__})，改用 JS 点击")
                                if await self._clear_blocking_overlays(page, _log):
                                    try:
                                        await button.click(timeout=3000)
                                        clicked = True
                                    except Exception:
                                        pass
                                if not clicked:
                                    clicked = await page.evaluate(self._JS_CLICK_JS, target)
                        popup = await popup_info.value
                    except Exception as e:
                        debug(f"{prefix} 组件 {component_class} 未捕获到新标签页({type(e).__name__})")
                        if not clicked:
                            clicked = await page.evaluate(self._JS_CLICK_JS, target)
                    if not clicked:
                        _log(f"{prefix} 组件 {component_class} 点不到「{label}」，跳过该组件", "yellow")
                        continue
                    await page.wait_for_timeout(1200)
                    await self._dismiss_page_dialog(page)
                    await page.wait_for_timeout(2500)
                    if popup is not None:
                        try:
                            await popup.close()
                        except Exception:
                            pass
                    name = component.get("resourceName") or component.get("componentCode") or component_class
                    # 点击后回读一次，确认平台真的记了完成，别让日志说谎
                    latest_state = await snapshot()
                    latest = next((c for c in (latest_state.get("components") or [])
                                   if c.get("componentClass") == component_class), None)
                    if _component_finished(latest):
                        _log(f"{prefix} 已点「{label}」并记录完成：{name}", "green")
                    else:
                        _log(f"{prefix} 已点「{label}」，但平台尚未标记该组件完成：{name}", "yellow")
                except Exception as e:
                    debug(f"{prefix} 组件 {component_class} 点击「{label}」失败: {e}")
                    _log(f"{prefix} 组件 {component_class} 点击「{label}」失败: {e}", "yellow")

            # 图文/图片/外链课程组件：滚进视口就会上报完成（组件自带滚动判定）
            state = await snapshot()
            read_components = components_of_kind(state, "read")
            if read_components:
                _log(f"{prefix} 有 {len(read_components)} 个图文/图片组件未完成，逐个滚动到视口", "blue")
            for component in read_components:
                if self._stop_event.is_set() or (cancel_event and cancel_event.is_set()):
                    return False
                component_class = component.get("componentClass")
                try:
                    await page.evaluate("""(cls) => {
                        const el = document.querySelector('.' + cls);
                        if (el) el.scrollIntoView({ block: 'center' });
                    }""", component_class)
                    await page.wait_for_timeout(300)
                    await page.evaluate("() => window.scrollBy(0, 1)")  # 保证触发滚动事件
                    await page.wait_for_timeout(1500)
                except Exception as e:
                    debug(f"{prefix} 组件 {component_class} 滚动到视口失败: {e}")

            # 剩下的未完成组件里，哪些是必须人工的（如交作业）
            state = await snapshot()
            manual_pending = components_of_kind(state, "manual")
            if manual_pending:
                names = "、".join(sorted({c.get("componentCode") or c.get("componentClass")
                                          for c in manual_pending}))
                _log(f"{prefix} {len(manual_pending)} 个组件需要人工完成（{names}），已跳过", "yellow")
            report_progress(state)

            # 视频组件会先记录自身完成；训练营还需要点击页面的“完成学习”，再由平台确认课程完成。
            if state.get("pageDone"):
                if progress_callback:
                    progress_callback(100)
                return True
            # 复用同一套健壮点击：先清遮挡弹窗、常规点击失败则 JS 触发、最多重试 3 次
            if await self._confirm_trainingcamp_finish(page, worker_id, _log):
                if progress_callback:
                    progress_callback(100)
                return True
            debug(f"{prefix} 视频进度已上报，但平台尚未确认课程完成 ({state.get('doneText', '')})")
            _log(f"{prefix} 视频已播放，但平台未确认课程完成（{state.get('doneText', '')}）", "yellow")
            return False
        except Exception as e:
            debug(f"{prefix} 播放异常: {e}\n{traceback.format_exc()}")
            _log(f"{prefix} 播放异常: {e}", "red")
            return False

    # ─── 训练营随堂测试（考试）自动答题 ────────────────────────────────

    _TRAINCAMP_COMPONENTS_JS = r"""() => {
        const items = document.querySelectorAll('[id^="traincamp-journey-module-item-"]');
        let vm = null;
        for (const node of items) {
            let cur = node;
            while (cur) {
                if (cur.__vue__ && Array.isArray(cur.__vue__.compMapList)) { vm = cur.__vue__; break; }
                cur = cur.parentElement;
            }
            if (vm) break;
        }
        if (!vm) return null;
        const components = (vm.compMapList || []).map((item, i) => {
            const cfg = (item.componentConfig && item.componentConfig.data_config) || [];
            let resources = [];
            try {
                const dataNode = cfg.find(c => c.code === 'data');
                const listNode = dataNode && (dataNode.list || []).find(l => l.prop_key === 'dataList');
                const value = listNode && listNode.value;
                resources = (value && value[0] && value[0].list) || [];
            } catch (e) { resources = []; }
            return {
                index: i,
                componentCode: item.componentCode || '',
                componentName: item.componentName || '',
                resources: resources.map(r => ({
                    pcUrl: (r && r.pcUrl) || '',
                    name: (r && (r.resourceName || r.name)) || '',
                    requiredFlag: (r && r.resourceConfig) ? r.resourceConfig.requiredFlag : null,
                    examDate: (r && r.resourceConfig) ? (r.resourceConfig.examDate || '') : ''
                }))
            };
        });
        return { components };
    }"""

    _EXAM_PREVIEW_JS = r"""() => {
        const el = document.querySelector('.exam_exampreview');
        const vm = el && el.__vue__;
        if (!vm) return null;
        const m = vm.userExamMap || {};
        return {
            arrangeName: m.arrangeName || '',
            btnText: vm.btnTextT || '',
            btnEnabled: !!vm.btnStatus && !!vm.isShowBtn,
            lblMsg: vm.lblMsg || '',
            isAppExam: !!m.isAppExam,
            isShowBtn: !!vm.isShowBtn,
            userExamMapID: vm.userExamMapID || '',
            examArrangeID: vm.examArrangeID || ''
        };
    }"""

    _EXAM_QUESTIONS_JS = r"""() => {
        const findVm = () => {
            const root = document.querySelector('.practiceing');
            if (root && root.__vue__ && Array.isArray(root.__vue__.questionsList)) return root.__vue__;
            const all = document.querySelectorAll('div');
            for (const el of all) {
                const vm = el.__vue__;
                if (vm && Array.isArray(vm.questionsList) && vm.userExamId) return vm;
            }
            return null;
        };
        const vm = findVm();
        if (!vm) return null;
        const strip = (html) => {
            const d = document.createElement('div');
            d.innerHTML = html == null ? '' : String(html);
            return (d.innerText || d.textContent || '').replace(/\s+/g, ' ').trim();
        };
        return {
            userExamId: vm.userExamId,
            arrangeId: vm.arrangeId,
            userExamMapId: vm.userExamMapId,
            uniqueId: vm.uniqueId,
            arrangeName: vm.arrangeName || '',
            maxScore: vm.maxScore,
            passScore: vm.passScore,
            totalQuestionQty: vm.totalQuestionQty,
            questions: (vm.questionsList || []).map(q => {
                const raw = q.QuestionType === 'Judge' ? q.JudgeItems : q.ChoiceItems;
                const options = (raw || []).map(o => ({
                    id: o.ID, code: o.ItemCode, text: strip(o.ItemContent)
                }));
                return {
                    id: q.ID,
                    index: q.OrderIndex,
                    type: q.QuestionType,
                    content: strip(q.QuestionContent),
                    hasImage: /<img/i.test(String(q.QuestionContent || '')),
                    options: options,
                    blankCount: (q.FillInItems || []).length
                };
            })
        };
    }"""

    _EXAM_SUBMIT_JS = r"""async ({ apiBase, userExamId, arrangeId, userExamMapId, payload }) => {
        const clean = (location.hash.split('#/')[1] || '').split('?')[0];
        const cp = btoa(location.pathname + '#/' + clean);
        const headers = {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json;charset=UTF-8',
            'token': window.localStorage.getItem('token') || '',
            'CParam1': cp,
            'CParam2': cp,
            'source': '501'
        };
        const body = JSON.stringify(payload);
        const out = {};
        const q = 'arrangeId=' + encodeURIComponent(arrangeId) +
                  '&userExamMapId=' + encodeURIComponent(userExamMapId);
        try {
            const r = await fetch(apiBase + '/ote/user/logAnswers/' + userExamId,
                { method: 'POST', headers, body, mode: 'cors', credentials: 'omit' });
            out.logStatus = r.status;
        } catch (e) { out.logError = String(e); }
        try {
            const r = await fetch(apiBase + '/ote/web/userexam/' + userExamId + '/submit?' + q,
                { method: 'POST', headers, body, mode: 'cors', credentials: 'omit' });
            out.submitStatus = r.status;
            try { out.body = await r.json(); } catch (e) { out.body = null; }
        } catch (e) { out.submitError = String(e); }
        return out;
    }"""

    _EXAM_RESULT_JS = r"""() => {
        const el = document.querySelector('.finishContainer');
        const vm = el && el.__vue__;
        if (!vm) return null;
        const m = vm.userExamMap || {};
        return {
            examName: m.examName || '',
            userStatus: m.userStatus || '',
            submitTime: m.submitTime || '',
            isShowScore: m.isShowScore,
            score: m.score,
            isPass: m.isPass,
            isAllowRepeat: m.isAllowRepeat,
            examTimes: m.examTimes,
            usedExamTimes: m.usedExamTimes
        };
    }"""

    async def _dismiss_page_dialog(self, page: Page) -> bool:
        """点掉 Element UI 的提示/确认弹窗（例如外链只能在内网访问的提示）"""
        for selector in (".el-message-box__btns button.el-button--primary",
                         ".el-message-box__btns button.el-button--default"):
            try:
                button = page.locator(selector).first
                if await button.count() > 0 and await button.is_visible():
                    await button.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    async def _trainingcamp_media_progress(self, page: Page) -> Optional[Dict]:
        """页面上媒体组件的数量与达标情况（用于判断「视频是否真的学完」）"""
        try:
            state = await page.evaluate(self._TRAINCAMP_MEDIA_JS)
        except Exception as e:
            debug(f"读取训练营媒体状态失败: {e}")
            return None
        components = [c for c in (state.get("components") or [])
                      if c.get("hasMedia") or c.get("hasPlayerEl") or c.get("hasPlayerApi")]
        ready = bool(components) and all(
            float(c.get("platformPct") or 0) >= float(c.get("threshold") or 95)
            or c.get("finishedFlag") for c in components)
        return {
            "count": len(components),
            "ready": ready,
            "page_done": bool(state.get("pageDone")),
            "progress": [round(float(c.get("platformPct") or 0)) for c in components],
            "books": [c.get("componentCode") for c in (state.get("components") or [])
                      if c.get("kind") == "book"],
            "reads": [c.get("componentClass") for c in (state.get("components") or [])
                      if c.get("kind") == "read"],
            "manual_pending": [c.get("componentCode") or c.get("componentClass")
                               for c in (state.get("components") or [])
                               if c.get("kind") == "manual" and not _component_finished(c)],
            "automatable_pending": [c.get("componentClass") for c in (state.get("components") or [])
                                    if c.get("kind") in ("media", "book", "outlink", "read")
                                    and not _component_finished(c)],
        }

    # 视频里的「互动问答」弹窗：close-on-press-escape / close-on-click-modal 都是 false，
    # 必答题连关闭按钮都没有，不处理会一直挡住页面（.v-modal 拦截点击）并让视频卡住。
    _VIDEO_QUIZ_JS = r"""() => {
        const root = document.querySelector('.answer');
        const vm = root && root.__vue__;
        if (!vm || typeof vm.submit !== 'function' || !vm.dialogvisible) return null;
        const strip = (html) => {
            const d = document.createElement('div');
            d.innerHTML = html == null ? '' : String(html);
            return (d.innerText || d.textContent || '').replace(/\s+/g, ' ').trim();
        };
        return {
            type: vm.questionType,
            typeName: vm.questionTypeName || '',
            title: strip(vm.questionTitle),
            isRequired: !!vm.isRequired,
            answered: !!vm.answered,
            options: (vm.optionList || []).map((o) => ({
                id: o.id, text: strip(o.title || o.content || '')
            }))
        };
    }"""

    _VIDEO_QUIZ_APPLY_JS = r"""(payload) => {
        const root = document.querySelector('.answer');
        const vm = root && root.__vue__;
        if (!vm) return false;
        try {
            if (payload.skip) { vm.close(); return true; }   // 平台自己的跳过流程（提交空答案后关闭）
            if (payload.ids && payload.ids.length) vm.checkedQuiz = payload.ids;
            if (payload.text) vm.text = payload.text;
            vm.submit();
            return true;
        } catch (e) { return false; }
    }"""

    # 被遮罩挡住时的兜底：直接用 JS 触发元素自身的 click（走平台自己的处理逻辑）
    _JS_CLICK_JS = r"""(selector) => {
        const el = document.querySelector(selector);
        if (!el) return false;
        el.click();
        return true;
    }"""

    async def _handle_video_quiz(self, page: Page, log=None) -> bool:
        """处理视频互动问答弹窗：配了 DeepSeek 就作答，否则按平台跳过流程关闭。"""
        try:
            quiz = await page.evaluate(self._VIDEO_QUIZ_JS)
        except Exception:
            return False
        if not quiz:
            return False
        kinds = {0: "Judge", 1: "SingleChoice", 2: "MultiChoice", 3: "QuestionAndAnswer"}
        try:
            qtype = kinds.get(int(quiz.get("type")), "QuestionAndAnswer")
        except (TypeError, ValueError):
            qtype = "QuestionAndAnswer"

        payload = {"skip": True}
        if self.deepseek_api_key and quiz.get("options") and qtype != "QuestionAndAnswer":
            letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            question = {
                "index": 1,
                "type": qtype,
                "content": quiz.get("title") or "",
                "options": [{"code": letters[i] if i < len(letters) else str(i),
                             "text": option.get("text") or ""}
                            for i, option in enumerate(quiz.get("options") or [])],
                "blankCount": 0,
            }
            try:
                client = DeepSeekClient(api_key=self.deepseek_api_key, model=self.deepseek_model,
                                        base_url=self.deepseek_base_url,
                                        thinking=self.deepseek_thinking)
                picked = ((await client.answer_exam([question], log=log)).get(1) or {}).get("choices") or []
                ids = [quiz["options"][letters.index(c)]["id"] for c in picked
                       if c in letters and letters.index(c) < len(quiz["options"])]
                if ids:
                    payload = {"ids": ids}
            except Exception as e:
                debug(f"视频互动问答作答失败: {e}")

        try:
            await page.evaluate(self._VIDEO_QUIZ_APPLY_JS, payload)
        except Exception as e:
            debug(f"视频互动问答处理失败: {e}")
            return False
        await page.wait_for_timeout(1200)
        if log:
            if payload.get("ids"):
                log(f"训练营 视频互动问答已作答（{quiz.get('typeName') or qtype}）", "green")
            else:
                log("训练营 视频互动问答未作答，已按平台跳过流程关闭", "yellow")
        return True

    async def _clear_blocking_overlays(self, page: Page, log=None) -> bool:
        """关掉挡住页面的 Element UI 弹窗/遮罩（视频互动问答、提示框等）。

        这些遮罩（.v-modal）会拦截 pointer events，导致「完成学习」等按钮点不到。
        """
        try:
            if await page.locator(".v-modal").count() == 0:
                return False
        except Exception:
            return False
        if await self._handle_video_quiz(page, log):
            return True
        for selector in (".el-dialog__headerbtn",
                         ".el-dialog .jumpover",
                         ".el-dialog__footer button.el-button--primary",
                         ".el-message-box__btns button.el-button--primary",
                         ".el-message-box__btns button.el-button--default"):
            try:
                button = page.locator(selector).first
                if await button.count() > 0 and await button.is_visible():
                    await button.click(timeout=2500)
                    await page.wait_for_timeout(600)
                    return True
            except Exception:
                continue
        try:
            await page.keyboard.press("Escape")  # Element UI 弹窗默认支持 ESC
            await page.wait_for_timeout(600)
            return True
        except Exception:
            return False

    async def _confirm_trainingcamp_finish(self, page: Page, worker_id: int, log_callback=None) -> bool:
        """点训练营课程页的「完成学习」并等待平台确认。

        遮罩弹窗（.v-modal）会拦截 pointer events 让点击超时，所以每次点击前先清弹窗；
        常规点击仍失败时，用 JS 直接触发页面自身的 click 处理（平台逻辑一致，只是绕过遮挡）。
        """
        prefix = f"[工作线程 {worker_id+1}] 训练营"
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        done_js = r"""() => {
            const d = document.querySelector('.traincamp-journey-study-done span');
            return !!(d && /恭喜您，已完成/.test(d.innerText || ''));
        }"""
        try:
            if await page.evaluate(done_js):
                return True
            button = page.locator(".traincamp-journey-study-done span").first
            if await button.count() == 0:
                return False
            await button.scroll_into_view_if_needed(timeout=3000)
            label = (await button.inner_text(timeout=2000)).strip()
            if "完成学习" not in label:
                return await page.evaluate(done_js)

            for attempt in range(1, 4):
                if self._stop_event.is_set():
                    return False
                # 弹窗遮罩会拦点击：先清掉（视频互动问答 / 提示框）
                await self._clear_blocking_overlays(page, _log)
                try:
                    await button.click(timeout=5000)
                except Exception as e:
                    debug(f"{prefix} 完成学习点击失败({attempt}/3): {e}")
                    try:
                        await page.evaluate("""() => {
                            const el = document.querySelector('.traincamp-journey-study-done span');
                            if (el) el.click();
                        }""")
                    except Exception as e2:
                        debug(f"{prefix} 完成学习 JS 点击也失败: {e2}")
                for _ in range(8):
                    await page.wait_for_timeout(1000)
                    if await page.evaluate(done_js):
                        return True
                # 平台可能弹了错误提示（例如仍有组件未完成），关掉再重试
                await self._dismiss_page_dialog(page)
            debug(f"{prefix} 完成学习重试 {3} 次，平台仍未确认")
            return False
        except Exception as e:
            debug(f"{prefix} 点击完成学习失败: {e}")
            return False

    def apply_exam_settings(self, settings: Dict) -> None:
        """把考试答题设置写入 learner（GUI 与命令行共用）"""
        settings = settings or {}
        self.exam_enabled = bool(settings.get("exam_enabled", False))
        self.deepseek_api_key = settings.get("deepseek_api_key", "") or ""
        self.deepseek_model = settings.get("deepseek_model", "") or DEEPSEEK_DEFAULT_MODEL
        self.deepseek_base_url = settings.get("deepseek_base_url", "") or DEEPSEEK_DEFAULT_BASE_URL
        self.deepseek_thinking = bool(settings.get("deepseek_thinking", False))
        self.exam_delay_min, self.exam_delay_max = _exam_delay_bounds(
            settings.get("exam_delay_min"), settings.get("exam_delay_max"))

    async def _trainingcamp_components(self, page: Page) -> Optional[List[Dict]]:
        """读取训练营课程页的组件列表（含 cuExam 考试组件）"""
        try:
            data = await page.evaluate(self._TRAINCAMP_COMPONENTS_JS)
        except Exception as e:
            debug(f"读取训练营组件失败: {e}")
            return None
        if not isinstance(data, dict):
            return None
        return data.get("components") or []

    async def solve_trainingcamp_exams(self, page: Page, worker_id: int, log_callback=None) -> Dict:
        """处理训练营课程页中的考试组件，返回统计信息。

        返回 {found, passed, failed, skipped, errors, all_ok, page_has_media}
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        prefix = f"[工作线程 {worker_id+1}] 考试"
        state = {"found": 0, "passed": 0, "failed": 0, "skipped": 0,
                 "errors": 0, "all_ok": True, "page_has_media": False}

        if not self.exam_enabled:
            return state
        if not self.deepseek_api_key:
            _log(f"{prefix} 已开启自动答题但未配置 DeepSeek API Key，跳过考试", "yellow")
            state["all_ok"] = False
            state["errors"] += 1
            return state

        components = await self._trainingcamp_components(page)
        if components is None:
            return state

        media_codes = {"cuVideo", "cuAudio", "cuCase", "cuCampLive"}
        state["page_has_media"] = any(
            (c.get("componentCode") or "") in media_codes for c in components
        )

        exam_tasks: List[Dict] = []
        seen_urls = set()
        for comp in components:
            code = comp.get("componentCode") or ""
            is_exam_component = code in ("cuExam", "cuWebExam")
            for res in (comp.get("resources") or []):
                url = (res.get("pcUrl") or "").strip()
                if not url:
                    continue
                # 认证考试之类的资源不一定叫 cuExam：只要指向考试中心（/ote/）就当考试处理
                if not is_exam_component and "/ote/" not in url and "exampreview" not in url:
                    continue
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://u.ccb.com" + url
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                name = (res.get("name") or comp.get("componentName") or "随堂测试").strip()
                exam_tasks.append({
                    "url": url,
                    "name": name,
                    "examDate": res.get("examDate") or "",
                    "required": res.get("requiredFlag"),
                })

        state["found"] = len(exam_tasks)
        if not exam_tasks:
            return state

        _log(f"{prefix} 本课程页发现 {len(exam_tasks)} 场考试，开始自动答题", "blue")
        for task in exam_tasks:
            if self._stop_event.is_set():
                state["all_ok"] = False
                return state
            try:
                result = await self._solve_one_exam(task, worker_id, _log)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug(f"{prefix} 异常: {e}\n{traceback.format_exc()}")
                result = {"status": "error", "detail": str(e)}

            # 只要这次没考成/没通过，就问用户要不要重考；
            # 不回答（倒计时结束）按「不重考」处理，避免无人值守时卡住。
            retakes = 0
            while (_exam_needs_retake_choice(result) and self.exam_retry_hook
                   and retakes < EXAM_RETRY_LIMIT and not self._stop_event.is_set()):
                _log(f"{prefix} 「{task['name']}」未通过/未完成（{result.get('detail', '')}），"
                     f"询问是否重考", "yellow")
                try:
                    again = await self.exam_retry_hook(task["name"], result.get("detail") or "")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug(f"{prefix} 重考确认失败: {e}")
                    again = False
                if not again:
                    _log(f"{prefix} 已选择不重考「{task['name']}」", "yellow")
                    break
                retakes += 1
                _log(f"{prefix} 按用户选择重考「{task['name']}」（第 {retakes}/{EXAM_RETRY_LIMIT} 次）",
                     "blue")
                try:
                    result = await self._solve_one_exam(task, worker_id, _log)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug(f"{prefix} 重考异常: {e}\n{traceback.format_exc()}")
                    result = {"status": "error", "detail": str(e)}

            status = result.get("status")
            if status == "passed":
                state["passed"] += 1
                _log(f"{prefix} 「{task['name']}」已完成：{result.get('detail', '')}", "green")
            elif status == "failed":
                state["failed"] += 1
                state["all_ok"] = False
                _log(f"{prefix} 「{task['name']}」未通过：{result.get('detail', '')}", "red")
            elif status == "skipped":
                state["skipped"] += 1
                _log(f"{prefix} 「{task['name']}」跳过：{result.get('detail', '')}", "yellow")
            else:
                state["errors"] += 1
                state["all_ok"] = False
                _log(f"{prefix} 「{task['name']}」答题失败：{result.get('detail', '')}", "red")

        summary = (f"考试处理完成：通过 {state['passed']}，未通过 {state['failed']}，"
                   f"跳过 {state['skipped']}，异常 {state['errors']}")
        _log(f"{prefix} {summary}", "green" if state["all_ok"] else "yellow")
        return state

    _EXAM_PREVIEW_RESULT_JS = r"""() => {
        const el = document.querySelector('.exam_exampreview');
        const vm = el && el.__vue__;
        if (!vm) return null;
        const m = vm.userExamMap || {};
        const records = (Array.isArray(vm.examSubList) ? vm.examSubList : []).map(r => ({
            submitTime: r.submitTime || '',
            status: r.status || '',
            score: (typeof r.score === 'number') ? r.score : null,
            isPass: (r.isPass === true || r.isPass === 'true' || r.isPass === 1) ? true
                   : (r.isPass === false || r.isPass === 'false' || r.isPass === 0) ? false : null
        }));
        return {
            arrangeName: m.arrangeName || '',
            btnText: vm.btnTextT || '',
            lastStatus: m.lastStatus || '',
            isShowScore: m.isShowScore,
            examTimes: m.examTimes,
            usedExamTimes: m.usedExamTimes,
            isAllowRepeat: !!m.isAllowRepeat,
            records: records
        };
    }"""

    async def _read_exam_result_from_preview(self, exam_page, task: Dict, meta: Dict,
                                             _log) -> Optional[Dict]:
        """刷新考试说明页读取是否通过（平台在这里显示考试记录与成绩）。

        提交后答题页不会自动刷新成绩，说明页重新加载才会拿到最新记录，
        这也是页面自身的“再考一次 / 已完成”按钮状态来源。
        """
        arrange_id = meta.get("arrangeId") or ""
        map_id = meta.get("userExamMapId") or ""
        base = task["url"].split("?")[0]
        url = f"{base}?examArrangeID={arrange_id}&userExamMapID={map_id}&hideFooter=true"
        try:
            await exam_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await exam_page.wait_for_selector(".exam_exampreview", state="attached", timeout=30000)
        except Exception as e:
            debug(f"打开考试说明页失败: {e}")
            return None

        for _ in range(20):
            try:
                data = await exam_page.evaluate(self._EXAM_PREVIEW_RESULT_JS)
            except Exception:
                data = None
            if data and (data.get("records") or data.get("lastStatus")):
                return data
            await exam_page.wait_for_timeout(1000)
        return None

    @staticmethod
    def _exam_result_from_records(data: Dict) -> Optional[Dict]:
        """把说明页的考试记录翻译成 {status, detail}；无有效记录返回 None。"""
        records = [r for r in (data.get("records") or []) if r.get("status") == "Done"]
        last_status = (data.get("lastStatus") or "").strip()
        if not records:
            if last_status in ("Submited", "Marking"):
                return {"status": "passed", "detail": f"已交卷待批阅（{last_status}）"}
            # NotStarted / Evaluating（考试进行中，按钮是「继续考试」）→ 没有结论，继续考
            return None

        latest = records[0]
        bits = []
        if latest.get("submitTime"):
            bits.append(f"交卷 {latest['submitTime']}")
        if data.get("isShowScore") and latest.get("score") is not None:
            bits.append(f"得分 {latest['score']}")
        if latest.get("isPass") is True:
            bits.append("已通过")
            return {"status": "passed", "detail": "，".join(bits)}
        if latest.get("isPass") is False:
            bits.append("未通过")
            return {"status": "failed", "detail": "，".join(bits)}
        bits.append(f"状态 {last_status or '未知'}")
        return {"status": "passed", "detail": "，".join(bits)}

    async def _pace_exam_submission(self, delay_seconds: float, prefix: str, _log) -> float:
        """交卷前按模拟答题时长等待，返回实际等待的秒数。

        分片 sleep 以便「停止学习」能及时中断。等待期间不动页面（不刷新、不点击），
        避免打断答题页自身的状态；等待结束后由调用方正常提交。
        """
        total = max(0.0, float(delay_seconds or 0))
        waited = 0.0
        while waited < total:
            if self._stop_event.is_set():
                _log(f"{prefix} 已请求停止，跳过剩余交卷延时 {total - waited:.0f} 秒", "yellow")
                break
            chunk = min(1.0, total - waited)
            await asyncio.sleep(chunk)
            waited += chunk
        return waited

    async def _solve_one_exam(self, task: Dict, worker_id: int, _log) -> Dict:
        """在独立标签页里完成一场考试：说明页 → 开始考试 → AI 答题 → 提交 → 读成绩"""
        prefix = f"[工作线程 {worker_id+1}] 考试"
        if not self.context:
            return {"status": "error", "detail": "浏览器上下文不可用"}

        exam_page = await self.context.new_page()
        try:
            await exam_page.goto(task["url"], wait_until="domcontentloaded", timeout=30000)
            try:
                await exam_page.wait_for_selector(".exam_exampreview", timeout=30000)
            except Exception:
                return {"status": "error", "detail": f"考试说明页未正常加载: {task['url']}"}
            await exam_page.wait_for_timeout(1500)

            preview = None
            for _ in range(20):
                try:
                    preview = await exam_page.evaluate(self._EXAM_PREVIEW_JS)
                except Exception:
                    preview = None
                if preview:
                    break
                if self._stop_event.is_set():
                    return {"status": "skipped", "detail": "已请求停止"}
                await exam_page.wait_for_timeout(1000)
            if not preview:
                return {"status": "error", "detail": "未能读取考试说明数据"}

            # 说明页会列出考试记录：已经通过就不用再考一次
            # （允许重考的考试按钮会显示「再考一次」且可点，只靠按钮状态判断不出来）
            try:
                records = await exam_page.evaluate(self._EXAM_PREVIEW_RESULT_JS)
            except Exception:
                records = None
            if records:
                prior = self._exam_result_from_records(records)
                if prior and "已通过" in (prior.get("detail") or ""):
                    return {"status": "skipped",
                            "detail": f"上次已通过（{prior['detail']}），不再重考"}
                if prior and "待批阅" in (prior.get("detail") or ""):
                    return {"status": "skipped",
                            "detail": f"上次{prior['detail']}，不再重考"}

            if not preview.get("isShowBtn"):
                return {"status": "skipped",
                        "detail": preview.get("lblMsg") or "当前不可考试（可能已过期或已完成）"}
            if not preview.get("btnEnabled"):
                return {"status": "skipped",
                        "detail": f"按钮不可用（{preview.get('btnText') or '未知状态'}）"}
            if preview.get("isAppExam"):
                return {"status": "skipped", "detail": "该考试仅支持手机扫码"}

            # 点「开始考试」。checkmanage 返回非 200 时平台会弹确认框。
            start_btn = exam_page.locator(".exam-start-btn").first
            if await start_btn.count() == 0:
                return {"status": "error", "detail": "未找到「开始考试」按钮"}
            await start_btn.click(timeout=15000)
            await exam_page.wait_for_timeout(1200)
            for _ in range(3):
                try:
                    confirm = exam_page.locator(
                        ".el-message-box__btns button.el-button--primary").first
                    if await confirm.count() > 0 and await confirm.is_visible():
                        await confirm.click(timeout=3000)
                        break
                except Exception:
                    pass
                await exam_page.wait_for_timeout(800)

            try:
                # 正式答题是 /userexam，模拟自测（模拟考试）是 /shamexam：
                # 两者数据结构与提交接口完全一致（getQuestionList/logAnswers/examSubmit），
                # 只是路由名不同，所以这里都接受。
                await exam_page.wait_for_function(
                    "() => location.hash.indexOf('/userexam') >= 0"
                    " || location.hash.indexOf('/shamexam') >= 0", timeout=30000)
            except Exception:
                return {"status": "error", "detail": "未能进入答题页"}
            debug(f"{prefix} 答题页路由: {exam_page.url}")

            # 等答题页把题目拉回来（init → getQuestionList）
            try:
                await exam_page.wait_for_function(
                    "() => { const r = document.querySelector('.practiceing');"
                    " return !!(r && r.__vue__ && Array.isArray(r.__vue__.questionsList)"
                    " && r.__vue__.questionsList.length && r.__vue__.userExamId); }",
                    timeout=60000)
            except Exception:
                return {"status": "error", "detail": "答题页题目加载超时"}

            paper = await exam_page.evaluate(self._EXAM_QUESTIONS_JS)
            if not paper or not paper.get("questions"):
                return {"status": "error", "detail": "未能读取试卷题目"}

            questions = paper["questions"]
            _log(f"{prefix} 试卷「{paper.get('arrangeName') or task['name']}」"
                 f"{len(questions)} 题，正在请求 DeepSeek 作答...", "blue")

            ai_questions = [{
                "index": q["index"],
                "type": q["type"],
                "content": q["content"],
                "options": [{"code": o["code"], "text": o["text"]} for o in q["options"]],
                "blankCount": q.get("blankCount") or 0,
            } for q in questions]

            model = DeepSeekClient(
                api_key=self.deepseek_api_key,
                model=self.deepseek_model,
                base_url=self.deepseek_base_url,
                thinking=self.deepseek_thinking,
            )
            ai_answers = await model.answer_exam(ai_questions, log=_log)
            if not ai_answers:
                return {"status": "error", "detail": "DeepSeek 未返回可用答案"}

            payload_answers = []
            unanswered = 0
            for q in questions:
                ai = ai_answers.get(q["index"]) or {}
                qtype = q["type"]
                if qtype == "FillIn":
                    blanks = [b for b in (ai.get("blanks") or []) if b.strip()]
                    answer = blanks if blanks else [""]
                elif qtype == "QuestionAndAnswer":
                    answer = [ai.get("text") or ""]
                else:
                    code_map = {}
                    for o in q["options"]:
                        code_map[str(o["code"]).strip().upper()] = o["id"]
                    ids = [code_map[c] for c in (ai.get("choices") or []) if c in code_map]
                    if qtype in ("SingleChoice", "Judge"):
                        ids = ids[:1]
                    if not ids and q["options"]:
                        # 兜底：至少选第一项，避免整题留空
                        ids = [q["options"][0]["id"]]
                    answer = ids
                if not answer or all((not str(a).strip()) for a in answer):
                    unanswered += 1
                    continue
                payload_answers.append({
                    "answer": answer,
                    "questionId": q["id"],
                    "index": q["index"],
                    "questionType": "QuestionAnswer" if qtype == "QuestionAndAnswer" else qtype,
                })

            submit_payload = {
                "submitType": 0,
                "uniqueId": paper.get("uniqueId") or "",
                "usedTime": 0,
                "answers": payload_answers,
            }
            if unanswered:
                _log(f"{prefix} 有 {unanswered} 题未能作答，将按未答提交", "yellow")

            # 交卷节奏：AI 答题瞬间完成，直接交卷会显得异常。
            # 按题量等待「题量 × 每题随机耗时」秒，模拟正常作答时间后再提交。
            per_question, delay_seconds = _exam_delay_plan(
                len(questions), self.exam_delay_min, self.exam_delay_max)
            if delay_seconds > 0:
                _log(f"{prefix} 模拟作答节奏：{len(questions)} 题 × "
                     f"{per_question:.1f} 秒 ≈ {delay_seconds:.0f} 秒后交卷", "blue")
                waited = await self._pace_exam_submission(delay_seconds, prefix, _log)
                debug(f"{prefix} 交卷延时结束: 计划 {delay_seconds:.1f} 秒，"
                      f"实际等待 {waited:.1f} 秒")

            result = await exam_page.evaluate(self._EXAM_SUBMIT_JS, {
                "apiBase": OTE_API_BASE,
                "userExamId": paper.get("userExamId"),
                "arrangeId": paper.get("arrangeId"),
                "userExamMapId": paper.get("userExamMapId"),
                "payload": submit_payload,
            })
            status_code = (result or {}).get("submitStatus")
            if not status_code or int(status_code) >= 400:
                return {"status": "error",
                        "detail": f"提交失败：{result}"}
            _log(f"{prefix} 已提交 {len(payload_answers)} 题答案（HTTP {status_code}）", "green")

            # 平台交卷后需要在考试说明页刷新才能看到成绩与是否通过，优先读说明页；
            # 取不到时再退回成绩页（examfinishedview）。
            preview_result = await self._read_exam_result_from_preview(
                exam_page, task, {"arrangeId": paper.get("arrangeId"),
                                  "userExamMapId": paper.get("userExamMapId")}, _log)
            if preview_result:
                outcome = self._exam_result_from_records(preview_result)
                if outcome:
                    debug(f"{prefix} 说明页记录: {preview_result}")
                    if outcome["status"] == "failed" and preview_result.get("isAllowRepeat"):
                        debug(f"{prefix} 该考试允许重考（{preview_result.get('usedExamTimes')}/"
                              f"{preview_result.get('examTimes')}），按设置不自动重考")
                    return outcome

            finished_url = (
                "https://u.ccb.com/ote/#/examfinishedview"
                f"?userExamId={paper.get('userExamId')}"
                f"&examArrangeID={paper.get('arrangeId')}"
                f"&userExamMapID={paper.get('userExamMapId')}&hideFooter=true"
            )
            try:
                await exam_page.goto(finished_url, wait_until="domcontentloaded", timeout=30000)
                await exam_page.wait_for_selector(".finishContainer", state="attached", timeout=20000)
                score = None
                for _ in range(20):
                    try:
                        score = await exam_page.evaluate(self._EXAM_RESULT_JS)
                    except Exception:
                        score = None
                    if score:
                        break
                    await exam_page.wait_for_timeout(1000)
            except Exception as e:
                debug(f"{prefix} 读取成绩失败: {e}")
                score = None

            if not score:
                return {"status": "passed", "detail": "答案已提交（未取到成绩，请到考试记录中确认）"}

            user_status = score.get("userStatus") or ""
            detail_bits = [f"状态 {user_status or '未知'}"]
            if score.get("submitTime"):
                detail_bits.append(f"交卷 {score['submitTime']}")
            if score.get("isShowScore") and score.get("score") is not None:
                detail_bits.append(f"得分 {score['score']}")
            detail = "，".join(detail_bits)

            if user_status == "Done":
                if score.get("isPass"):
                    return {"status": "passed", "detail": detail}
                return {"status": "failed", "detail": detail + "（未通过，请查看答卷）"}
            return {"status": "passed", "detail": detail + "（待批阅）"}
        finally:
            try:
                await exam_page.close()
            except Exception:
                pass

    async def _check_video_time(self, page: Page) -> float:
        """读取本地视频 currentTime（停滞检测用：平台%滞后时仍能判断在播）"""
        try:
            t = await page.evaluate("""() => {
                const v = document.querySelector('video');
                if (v && isFinite(v.currentTime)) return v.currentTime;
                return -1;
            }""")
            if isinstance(t, (int, float)) and t >= 0:
                return float(t)
        except:
            pass
        return -1

    async def _refresh_video_page(self, page: Page, worker_id: int) -> bool:
        """刷新课程页并重新进入播放（卡住时重试），返回是否恢复成功"""
        try:
            if ("/course/#/detail/" in page.url
                    and not await page.query_selector("video, audio, .prism-player")):
                debug(f"[工作线程 {worker_id+1}] 当前是课程详情页，不能刷新重试播放器")
                return False
            await page.reload(wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(5000)
            # 刷新后播放器状态丢失，重新点击学习按钮
            for kw in ["我要学习", "开始学习", "进入课程", "继续学习", "学习课程", "进入课程学习"]:
                try:
                    sb = page.locator(f"text={kw}").first
                    if await sb.count() > 0:
                        await sb.click()
                        await page.wait_for_timeout(5000)
                        break
                except:
                    pass
            # 等待视频元素重新出现
            video_selectors = ["video", "audio", "[class*='video']", "[class*='audio']", ".prism-player"]
            video_found = False
            for _wait in range(2):
                for sel in video_selectors:
                    try:
                        if await page.query_selector(sel):
                            video_found = True
                            break
                    except:
                        pass
                if video_found:
                    break
                await page.wait_for_timeout(3000)
            if not video_found:
                debug(f"[工作线程 {worker_id+1}] 刷新后未找到视频元素")
                return False
            await self._ensure_video_playing(page)
            await self._set_lowest_quality(page)
            debug(f"[工作线程 {worker_id+1}] 刷新后视频已恢复")
            return True
        except Exception as e:
            debug(f"[工作线程 {worker_id+1}] 刷新播放页失败: {e}")
            return False

    async def _ensure_video_playing(self, page: Page):
        """用JS检查视频播放状态，暂停则恢复播放"""
        try:
            result = await page.evaluate("""() => {
                const videos = document.querySelectorAll('video');
                if (videos.length === 0) return {status: 'no_video'};
                const v = videos[0];
                if (v.paused || v.ended) {
                    try { v.play(); } catch(e) {}
                    // 取消静音（有些浏览器autoplay需要unmute）
                    try { v.muted = false; } catch(e) {}
                    return {status: 'resumed', paused: v.paused, currentTime: v.currentTime, duration: v.duration};
                }
                return {status: 'playing', currentTime: v.currentTime, duration: v.duration};
            }""")
            if result.get('status') == 'resumed':
                debug(f"  视频已恢复播放, currentTime={result.get('currentTime', 0):.1f}")
        except:
            pass


    _api_lock = None

    async def _get_courses_by_api(self, page: Page, ws_id: str, log_callback=None) -> list:
        """通过专题班详情API直接获取课程列表（429自动重试）"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        if not self._api_lock:
            self._api_lock = asyncio.Lock()

        api_url = f"https://api.u.ccb.com/v1/workshop/users/workshops/v2/{ws_id}"

        # 先从主页面读取token（保证有有效的认证信息）
        _token = ""
        try:
            _token = await self.pages[0].evaluate(
                "() => { try { return localStorage.getItem('token') || ''; } catch(e) { return ''; } }"
            )
        except:
            pass

        # fetch代码（优先用预读的token，否则从当前页面取）
        fetch_code = """(url) => {
            let token = '""" + _token + """';
            if (!token) try { token = localStorage.getItem('token') || ''; } catch(e) {}
            if (!token) try { token = sessionStorage.getItem('token') || ''; } catch(e) {}
            if (!token) try { token = window.__token__ || ''; } catch(e) {}
            if (!token) try {
                const ax = window.axios;
                if (ax && ax.defaults && ax.defaults.headers)
                    token = ax.defaults.headers.common.token || ax.defaults.headers.token || '';
            } catch(e) {}
            if (!token) {
                const m = document.cookie.match(/token=([^;]+)/);
                if (m) token = m[1];
            }
            return fetch(url, {
                headers: {
                    'accept': 'application/json, text/plain, */*',
                    'source': '501',
                    'token': token,
                    'CParam1': 'L3dvcmtzaG9wLyMvbXl3b3Jrc2hvcC9kZXRhaWw=',
                    'CParam2': 'L3dvcmtzaG9wLyMvbXl3b3Jrc2hvcA=='
                },
                method: 'GET',
                credentials: 'include'
            }).then(async r => {
                if (!r.ok) {
                    let body = '';
                    try { body = await r.text(); } catch(e) {}
                    return {error: 'HTTP ' + r.status, body: body.slice(0, 200)};
                }
                return r.json();
            }).catch(e => ({error: e.message}));
        }"""

        try:
            async with self._api_lock:
                result = await page.evaluate(fetch_code, api_url)

                # 429 → 递增等待重试（5s, 10s, 15s）
                for retry_wait in [5, 10, 15]:
                    if not (isinstance(result, dict) and result.get("error") == "HTTP 429"):
                        break
                    _log(f"  429限流，等{retry_wait}秒重试...", "yellow")
                    await asyncio.sleep(retry_wait)
                    result = await page.evaluate(fetch_code, api_url)

                await asyncio.sleep(0.5)

            if isinstance(result, dict) and result.get("error"):
                err_msg = result['error']
                body = result.get('body', '')
                if 'HTTP 400' in err_msg and body:
                    _log(f"  API失败: {err_msg} ({body[:80]})", "red")
                else:
                    _log(f"  API失败: {err_msg}", "red")
                # 400是客户端错误，重试无意义，直接返回
                if 'HTTP 4' in err_msg:
                    return None
            if not isinstance(result, dict):
                _log(f"  API返回异常: {type(result).__name__}", "red")
                return None
            return result
        except Exception as e:
            _log(f"  API异常: {e}", "red")
            return None

    async def _enroll_workshop_if_needed(self, page: Page, ws_url: str, _log) -> bool:
        """专题班要先报名，课程列表才会出现（未报名时课程接口拿不到数据）。

        返回 True 表示点了报名；调用方需要重新进详情页等服务器处理。
        """
        for keyword in ("立即报名", "加入学习", "免费报名"):
            try:
                btn = page.locator(f"text={keyword}").first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                _log(f"  需要报名，点击「{keyword}」", "blue")
                old_url = _page_url(page)
                await btn.click(timeout=8000)
                # 报名后地址会变（详情路由切到"已报名"形态），等它切过去再继续
                for _ in range(10):
                    await page.wait_for_timeout(2000)
                    if _page_url(page) != old_url:
                        break
                    try:
                        if not await btn.is_visible(timeout=1000):
                            break
                    except Exception:
                        break
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
                debug(f"报名前后 URL: {_safe_debug_url(old_url)} → "
                      f"{_safe_debug_url(_page_url(page))}")
                return True
            except Exception as exc:
                debug(f"报名按钮「{keyword}」点击失败: {type(exc).__name__}: {_safe_debug_error(exc)}")
        return False

    async def get_courses_from_workshop(self, page: Page, ws_title: str = "") -> List[Dict]:
        # 从表格提取全部课程信息（不含URL，URL由collector动态采集）
        courses = []
        try:
            # 检查页面是否在正确的URL上
            current_url = page.url
            if "workshop" not in current_url and "detail" not in current_url:
                debug(f"  页面不在专题班详情页: {current_url}")
                return []

            debug("正在获取课程列表...")
            # 等待表格tbody有数据行（API异步加载）
            for _wait in range(6):
                row_count = await page.locator("tr.text-center").count()
                tbody_has_children = await page.evaluate(
                    "() => { const tb = document.querySelector('tbody.content'); return tb ? tb.children.length : 0; }")
                if row_count > 0 or tbody_has_children > 0:
                    break
                debug(f"  等待课程数据加载({_wait+1}/6)...")
                await page.wait_for_timeout(5000)
            await page.wait_for_timeout(2000)

            # 检查页面是否加载了课程表格
            row_count = await page.locator("tr.text-center").count()
            if row_count == 0:
                # 表格没加载出来，打印页面关键信息帮助排查
                try:
                    url_now = page.url
                    body_text = await page.locator("body").inner_text(timeout=3000)
                    # 只取前500字符避免刷屏
                    debug(f"  页面无课程表格, URL: {url_now}")
                    debug(f"  页面内容: {body_text[:500]}")
                except:
                    pass

            table_data = await page.evaluate(WORKSHOP_COURSE_TABLE_JS)
            rows_data = table_data.get("rows") or []
            debug(f"  表格列头: {table_data.get('headers')}")
            debug(f"  原始表格行数: {len(rows_data)}, URL: {page.url}")
            if not rows_data:
                # 没有任何行，dump页面关键区域
                try:
                    html_snippet = await page.evaluate("""() => {
                        const t = document.querySelector('table');
                        if (t) return t.outerHTML.substring(0, 1000);
                        const main = document.querySelector('.workshop-detail, .detail-content, #app');
                        if (main) return main.innerHTML.substring(0, 1000);
                        return document.body.innerHTML.substring(0, 1000);
                    }""")
                    debug(f"  页面HTML: {html_snippet[:500]}")
                except:
                    pass

            skipped = []  # 被过滤的课程
            for row in rows_data:
                title = row.get('title', '').strip()
                ctype = row.get('type', '').strip()
                # 排除考试/scorm（不能自动完成）
                if ctype in ('考试', 'scorm'):
                    skipped.append(f"{ctype}: {title[:30]}")
                    continue
                if title and len(title) > 3:
                    courses.append(row)

            # debug: 打印所有 action 值帮助排查
            action_vals = set(c.get('action', '') for c in courses)
            debug(f"课程 action 值: {action_vals}")
            if courses:
                debug(f"前3门课程 action: {[(c['title'][:30], c['action']) for c in courses[:3]]}")

            # 检查是否有NaN（页面未完全加载）
            has_nan = False
            for c in courses:
                for v in c.values():
                    if isinstance(v, str) and 'NaN' in v:
                        has_nan = True
                        break
            if has_nan:
                debug("  课程数据包含NaN，页面未完全加载")
                return None  # 返回None表示需要重试

            # 区分：表格有数据但全被过滤 vs 表格根本没数据
            raw_count = len(rows_data) or await page.locator("tr.text-center").count()
            if raw_count > 0 and len(courses) == 0:
                # 表格有行但全被过滤（图书/考试等），不需要重试
                skipped_str = ", ".join(skipped[:5])
                if len(skipped) > 5:
                    skipped_str += f" 等{len(skipped)}项"
                debug(f"  表格有{raw_count}行但全被过滤: {skipped_str}")
                prefix = f"[{ws_title[:20]}] " if ws_title else ""
                console.print(f"{prefix}课程列表: 0 门（过滤: {skipped_str}）", style="yellow")
                return []  # 返回空列表表示确实没有可学课程

            if raw_count == 0 and len(courses) == 0:
                # 表格没数据，需要重试
                debug("  表格无数据行，需要重试")
                return None  # 返回None表示需要重试

            prefix = f"[{ws_title[:20]}] " if ws_title else ""
            console.print(f"{prefix}课程列表: {len(courses)} 门", style="green")
        except Exception as e:
            console.print(f"获取课程列表失败: {e}", style="yellow")
            import traceback
            traceback.print_exc()
            return None  # 异常也返回None表示需要重试

        return courses

    async def _get_study_hours(self, page=None, force=False) -> dict:
        """获取学时（O1 节流）：60 秒 TTL 缓存 + 并发单飞。

        - 未过期直接返回缓存值（每门课后不再各自打学习中心）；
        - force=True 强制刷新（供定时刷新线程用）；
        - 并发调用合并为一次真实查询。
        """
        now = time.time()
        cache = self._hours_cache
        if not force and cache["value"] is not None and now - cache["ts"] < self._hours_ttl:
            return cache["value"]
        if self._hours_lock is None:
            self._hours_lock = asyncio.Lock()
        async with self._hours_lock:
            # 二次检查：等待锁期间可能已被其他调用刷新
            if not force and cache["value"] is not None and now - cache["ts"] < self._hours_ttl:
                return cache["value"]
            value = await self._fetch_study_hours(page)
            cache["value"] = value
            cache["ts"] = time.time()
            return value

    async def _fetch_study_hours(self, page=None) -> dict:
        # 从学习中心获取今年的培训学时。
        # 优先复用调用方传入的页面（避免每门课后新建页面造成的并发/429压力），
        # 页面为空或已关闭时才临时新建。
        _page = page
        _close_after = False
        if _page is None or (hasattr(_page, "is_closed") and _page.is_closed()):
            try:
                _page = await self.context.new_page()
                _close_after = True
            except Exception:
                return {"central": 0, "online": 0, "total": 0}
        dom_result = None
        try:
            await _page.goto("https://u.ccb.com/portal/#/studyCenter",
                           wait_until="domcontentloaded", timeout=20000)
            # 先等「今年已训」行标签出现，再等学时数值绑定完成，最后留一点稳定时间；
            # 比固定 sleep 8s 更快，也避免数据未到位时把学时解析成 0。
            try:
                await _page.wait_for_selector(
                    f"text={STUDY_HOURS_ROW_LABELS[0]}", timeout=6000)
            except Exception:
                pass
            try:
                await _page.wait_for_selector(_HOURS_VALUE_SELECTOR, timeout=6000)
            except Exception:
                pass
            await _page.wait_for_timeout(700)
            text = await _page.locator("body").inner_text(timeout=5000)
            # DOM 结构化解析必须在页面关闭前完成
            try:
                dom_result = await _page.evaluate(_STUDY_HOURS_DOM_JS)
            except Exception as _dex:
                debug(f"学习中心结构化解析失败: {_dex}")
        except Exception as _ex:
            debug(f"学习中心加载失败: {_ex}")
            if _close_after:
                try:
                    await _page.close()
                except:
                    pass
            return {"central": 0, "online": 0, "total": 0}
        finally:
            if _close_after:
                try:
                    await _page.close()
                except:
                    pass

        debug(f"学习中心页面内容:\n{text[:600]}")

        # 优先 DOM 结构化解析（按行/列归属，能区分单元格里的说明文字），
        # 缺失的列再用文本兜底解析补齐。
        result = resolve_study_hours(dom_result, text)
        if isinstance(dom_result, dict):
            _dbg = dom_result.get("debug")
            if _dbg:
                debug(f"学时DOM解析: {_dbg}")

        central = float(result["central"])
        online = float(result["online"])
        required_central = float(result["required_central"])
        required_online = float(result["required_online"])
        debug(f"学时解析({result['source']}): 集中培训={central}, 网络自学={online}; "
              f"应训时长: 集中培训={required_central}, 网络自学={required_online}")
        return {"central": central, "online": online, "total": central + online,
                "required_central": required_central, "required_online": required_online}

    async def _course_mode(self, page: Page):
        # 从 /course/#/list/1 选择课程学习
        console.print("课程列表模式", style="bold")
        list_url = "https://u.ccb.com/course/#/list/1"
        await page.goto(list_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

        # 课程列表交互式筛选（与专题班标签同结构）
        _fk = await async_input("是否筛选课程？(y/n，默认n)", default="n", timeout=5)
        if _fk in ('y', 'yes'):
            await page.wait_for_timeout(2000)
            # 提取tag-second过滤项
            _fitems = page.locator("li.tag-second:not(.active)")
            _fc = await _fitems.count()
            if _fc > 0:
                console.print("\n可选筛选条件:", style="bold")
                _all_flt = []
                for _fi in range(_fc):
                    _txt = (await _fitems.nth(_fi).inner_text()).strip()
                    console.print(f"  [{_fi+1:3d}] {_txt[:25]}", style="white")
                    _all_flt.append(_fitems.nth(_fi))
                _sel = await async_input("输入编号（逗号分隔，回车跳过）", default="", timeout=30)
                for _part in _sel.split(","):
                    _part = _part.strip()
                    if _part.isdigit() and 1 <= int(_part) <= len(_all_flt):
                        await _all_flt[int(_part)-1].click()
                        await page.wait_for_timeout(1500)
                await page.wait_for_timeout(3000)
            else:
                console.print("未找到筛选选项", style="yellow")

        console.print("正在提取课程列表...", style="blue")
        courses_data = []
        
        # 分页收集
        _max_page = 1
        try:
            # 等待分页栏出现
            await page.wait_for_selector("[class*=page-next], [class*=page_num]", timeout=10000)
            if await page.locator("[class*=page-next]").count() > 0:
                _pn = page.locator("[class*=page_num]")
                _tp = await _pn.count()
                if _tp > 0:
                    _lt = (await _pn.nth(_tp - 1).inner_text()).strip()
                    if _lt.isdigit():
                        console.print(f"当前显示约 {_lt} 页", style="blue")
                        _ip = await async_input("获取前几页？(回车=1，0=全部)", default="1", timeout=10)
                        _max_page = int(_lt) if _ip == "0" else (int(_ip) if _ip.isdigit() and int(_ip) > 0 else 1)
        except:
            debug("未检测到分页控件，尝试文本检测")
            _body = await page.locator("body").inner_text()
            if "下一页" in _body or "Next" in _body:
                _ip = await async_input("检测到分页，获取前几页？(回车=1，0=全部)", default="1", timeout=10)
                _max_page = 999 if _ip == "0" else (int(_ip) if _ip.isdigit() and int(_ip) > 0 else 1)
            else:
                debug("页面无分页")
        
        for _pg in range(_max_page):
            if _pg > 0:
                try:
                    _nb = page.locator("[class*=page-next]:not([class*=page_disabled])")
                    if await _nb.count() > 0:
                        await _nb.first.click()
                        await page.wait_for_timeout(5000)
                    else:
                        break
                except:
                    break
            
            _cards = page.locator("a.p-cursor[title]")
            _cc = await _cards.count()
            for _ci in range(_cc):
                _t = await _cards.nth(_ci).get_attribute("title")
                if _t:
                    courses_data.append({"title": _t.strip()[:60], "hours": "", "page": _pg + 1})

        if not courses_data:
            console.print("未获取到课程", style="yellow")
            return

        console.print(f"找到 {len(courses_data)} 门课程:", style="green")
        for i, c in enumerate(courses_data, 1):
            console.print(f"  [{i:3d}] {c['title']}", style="white")

        console.print()
        sel = await async_input("输入课程编号（逗号/范围分隔，回车全学）", default="", timeout=30)
        indices = list(range(len(courses_data)))
        if sel:
            indices = []
            for p in sel.split(","):
                p = p.strip()
                if "-" in p:
                    a, b = p.split("-", 1)
                    indices.extend(range(int(a)-1, int(b)))
                elif p.isdigit():
                    indices.append(int(p)-1)
            indices = [i for i in indices if 0 <= i < len(courses_data)]

        nw = min(self.workers, len(indices))
        console.print(f"使用 {nw} 个工作线程学习 {len(indices)} 门课程", style="bold blue")

        async def cworker(wid, wp, aidx):
            for gi in aidx:
                c = courses_data[gi]
                cpage = c.get("page", 1)
                console.print(f"[工作线程 {wid+1}] {c['title'][:35]}", style="bold blue")
                cp = None
                try:
                    # 回到列表页并翻到课程所在页码（避免跨页全局索引点错/跳过）
                    await wp.goto(list_url, wait_until="networkidle", timeout=20000)
                    await wp.wait_for_timeout(5000)
                    for _ in range(cpage - 1):
                        try:
                            nxt = wp.locator("[class*=page-next]:not([class*=page_disabled])")
                            if await nxt.count() == 0:
                                break
                            await nxt.first.click()
                            await wp.wait_for_timeout(3000)
                        except:
                            break
                    # 按标题精确定位（翻页后索引不再可靠）
                    links = wp.locator("a.p-cursor[title]")
                    ln = await links.count()
                    link = None
                    for i in range(ln):
                        try:
                            t = (await links.nth(i).get_attribute("title") or "").strip()
                        except:
                            t = ""
                        if t and c['title'] and c['title'] in t:
                            link = links.nth(i)
                            break
                    # 第1页兜底：全局索引即页内相对索引
                    if link is None and cpage == 1 and gi < ln:
                        link = links.nth(gi)
                    if link is None:
                        console.print(f"未找到课程: {c['title'][:30]}", style="yellow")
                        continue
                    async with wp.expect_event("popup", timeout=20000) as pi:
                        await link.click()
                    cp = await pi.value
                    await cp.wait_for_load_state()
                    await cp.wait_for_timeout(5000)
                    for kw in ["我要学习", "开始学习", "进入课程", "继续学习", "学习课程"]:
                        try:
                            sb = cp.locator(f"text={kw}").first
                            if await sb.count() > 0:
                                debug(f"找到 {kw}")
                                await sb.click()
                                await cp.wait_for_timeout(5000)
                                break
                        except:
                            pass
                    play_ok = await self.find_and_play_video(cp, wid)
                    if not play_ok:
                        console.print(f"视频未完成: {c['title'][:30]}", style="yellow")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug(f"课程异常: {e}")
                finally:
                    if cp:
                        try:
                            await cp.close()
                        except:
                            pass

                # 检查学习目标
                if self.study_goal > 0:
                    h = await self._get_study_hours(wp)
                    cur = h.get("online", 0)
                    console.print(f"网络自学: {cur:.1f}/{self.study_goal} 学时", style="blue")
                    if cur >= self.study_goal:
                        console.print("已达到学习目标! 程序退出", style="bold green")
                        raise GoalReached()

        tasks = []
        for wid in range(nw):
            aidx = [indices[j] for j in range(wid, len(indices), nw)]
            tasks.append(asyncio.create_task(cworker(wid, self.pages[wid], aidx)))
            await asyncio.sleep(3)
        try:
            await asyncio.gather(*tasks)
        except GoalReached:
            # 目标达成：停止其余线程，正常收尾
            for t in tasks:
                if not t.done():
                    t.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except:
                pass
        console.print("课程模式学习完成", style="bold green")

    async def _wait_online_course_cards(self, page: Page, timeout: int) -> bool:
        """等待课程卡片可见；只做判定，不抛异常。"""
        try:
            await page.wait_for_selector(ONLINE_COURSE_LIST_CARD_SELECTOR,
                                         state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    async def _load_online_course_list(self, page: Page, list_url: str, *,
                                       worker_id: int = -1, attempts: int = 2) -> bool:
        """打开网络自学课程列表页，确保课程卡片真正渲染出来。

        列表地址 /course/#/list/N 是 hash 路由，而 goto() 到同文档地址只是同文档
        导航：hash 没变化时浏览器连 hashchange 都不会派发，SPA 也就不会重新渲染。
        于是「卡片为空」这种一次性的渲染失败会变成该标签页的永久故障——地址始终正确、
        卡片始终为空，之后每一轮都白等一个超时，最终报「课程列表未加载」。
        所以标签页已经停在列表地址上时必须 reload()（或先重置页面）拿到真正的新文档。
        """
        tag = f"[工作线程 {worker_id + 1}] " if worker_id >= 0 else ""
        reload_page = getattr(page, "reload", None)

        for attempt in range(1, max(1, attempts) + 1):
            current_url = _page_url(page)
            # 已经停在列表页且卡片可见：直接复用，不做无谓的重新加载。
            if _same_hash_url(current_url, list_url) and await self._wait_online_course_cards(page, 1500):
                debug(f"{tag}phase=list_reuse attempt={attempt}; {_page_debug_state(page)}")
                return True

            navigated = False
            try:
                if _same_hash_url(current_url, list_url) and callable(reload_page):
                    # 地址与目标完全一致，goto 是空操作（不派发 hashchange），
                    # 只有 reload 才能按同一地址真正重新加载。
                    debug(f"{tag}phase=list_reload attempt={attempt}; "
                          f"同址 goto 不会重渲染，改为 reload; {_page_debug_state(page)}")
                    await reload_page(wait_until="domcontentloaded", timeout=20000)
                else:
                    # 文档不同或 hash 不同：hash 变化会触发路由，goto 即可切到目标页。
                    # 这里不能改成 reload：reload 会停留在标签页当前所在的页（例如第 2 页）。
                    await page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                navigated = True
            except Exception as exc:
                debug(f"{tag}phase=list_nav_error attempt={attempt}; "
                      f"error={type(exc).__name__}: {_safe_debug_error(exc)}; {_page_debug_state(page)}")

            if await self._wait_online_course_cards(page, 12000):
                debug(f"{tag}phase=list_ready attempt={attempt}; {_page_debug_state(page)}")
                return True

            if not navigated:
                # 导航本身就没成功，属于网络问题：交给上层稍后整体重试，别再折腾标签页。
                break

            if attempt < attempts:
                # 页面地址变了但卡片没渲染：SPA 残留状态，重置标签页后再来一次。
                debug(f"{tag}phase=list_empty attempt={attempt}; 重置标签页后重试; "
                      f"{_page_debug_state(page)}")
                try:
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    break

        debug(f"{tag}phase=list_unavailable; {_page_debug_state(page)}")
        return False

    @staticmethod
    def _route_page_index(url: str) -> Optional[int]:
        """从 /course/#/list/N 这类 hash 路由里取当前页码。"""
        try:
            match = re.search(r"/list/(\d+)", urlsplit(url or "").fragment)
        except Exception:
            return None
        return int(match.group(1)) if match else None

    async def _online_course_page_signature(self, page: Page) -> dict:
        """读取列表页签名：{route, indicator, cards, cards_set}。

        卡片指纹优先用页面内一次性 evaluate 取，省掉逐卡片往返；页面对象不支持
        evaluate（或读取失败）时退化为只看路由页码。
        """
        signature = {"route": None, "indicator": None, "cards": None, "cards_set": None}
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                state = await evaluate(ONLINE_COURSE_PAGE_STATE_JS,
                                       ONLINE_COURSE_LIST_CARD_SELECTOR)
            except Exception:
                state = None
            if isinstance(state, dict):
                signature["indicator"] = state.get("pager")
                signature["cards"] = state.get("raw")
                signature["cards_set"] = state.get("set")
                signature["route"] = state.get("route")
        if not isinstance(signature["route"], int):
            signature["route"] = self._route_page_index(_page_url(page))
        return signature

    async def _wait_online_course_page_change(self, page: Page, before: dict, *,
                                              timeout_ms: Optional[int] = None,
                                              keys: tuple = ("cards_set", "indicator")) -> Optional[dict]:
        """轮询等待换页生效；超时未变化返回 None。

        默认不看路由：点击「下一页」时 hash 往往立刻变，但卡片是之后才渲染的，
        拿路由当成功依据会采到上一页的旧 DOM（这正是翻页不稳定的来源）。
        """
        budget_ms = ONLINE_PAGE_CHANGE_TIMEOUT_MS if timeout_ms is None else timeout_ms
        deadline = time.monotonic() + max(0.05, budget_ms / 1000.0)
        delay_ms = 350
        while True:
            after = await self._online_course_page_signature(page)
            for key in keys:
                old, new = before.get(key), after.get(key)
                if old is not None and new is not None and old != new:
                    return after
            if time.monotonic() >= deadline:
                return None
            try:
                await page.wait_for_timeout(delay_ms)
            except Exception:
                return None
            delay_ms = min(int(delay_ms * 1.6), 1500)

    async def _settled_route_page(self, page: Page, expected: Optional[int],
                                  attempts: int = 3,
                                  delay_ms: int = ONLINE_PAGE_SETTLE_DELAY_MS) -> Optional[int]:
        """读取落地路由页码；命中 expected 时再多确认几次，防止越界被夹回。"""
        landed = None
        for index in range(max(1, attempts)):
            if index:
                try:
                    await page.wait_for_timeout(delay_ms)
                except Exception:
                    break
            landed = self._route_page_index(_page_url(page))
            if expected is not None and landed == expected:
                continue
            break
        return landed

    @staticmethod
    def _observed_advanced_page(signature: dict, current_page: int) -> Optional[int]:
        """只接受 current+1 这个观测值，避免异常页码把逻辑页码带偏。"""
        for key in ("indicator", "route"):
            value = signature.get(key)
            if isinstance(value, int) and value == current_page + 1:
                return value
        return None

    async def _online_course_next_controls(self, page: Page) -> tuple:
        """收集可点击的「下一页」控件（按优先级），并标记是否见过明确禁用态。"""
        found = []
        disabled_seen = False
        candidates = [
            (page.locator("[class*=page-next]"), False),
            (page.locator("span.pagetext").filter(has_text=re.compile(r"下一页|Next", re.I)), True),
            (page.locator("a,button,li").filter(has_text=re.compile(r"^\s*(?:下一页|Next)\s*$", re.I)), True),
        ]
        for locator, require_next_text in candidates:
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    label = (await item.inner_text()).strip()
                    if require_next_text and not re.search(r"下一页|Next", label, re.I):
                        continue
                    classes = (await item.get_attribute("class") or "").lower()
                    aria_disabled = (await item.get_attribute("aria-disabled") or "").lower()
                    disabled = await item.get_attribute("disabled")
                    if (disabled is not None or aria_disabled == "true"
                            or any(token in classes for token in ("page_disabled", "is-disabled", "disabled"))):
                        disabled_seen = True
                        continue
                    found.append((item, label, classes))
                except Exception as exc:
                    debug(f"网络课程分页控件不可用; error={type(exc).__name__}: {_safe_debug_error(exc)}")
        return found, disabled_seen

    async def _advance_online_course_page_by_route(self, page: Page, list_url: str,
                                                   current_page: int, before: dict) -> Optional[dict]:
        """路由兜底：直接 goto /list/<N+1>，并用落地路由 + 卡片指纹双重校验。

        先看落地路由（快，且能立刻识别「越界被夹回当前页」= 末页），
        再在卡片可读时确认卡片真的换了 —— 路由前进不等于 SPA 渲染完了。
        """
        try:
            parts = urlsplit(_page_url(page) or list_url)
            match = re.match(r"^(.*?/list/)\d+$", parts.fragment.rstrip("/"))
            if not match:
                return None
            next_page = current_page + 1
            target = parts._replace(fragment=f"{match.group(1)}{next_page}").geturl()
            await page.goto(target, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            debug(f"网络课程分页路由兜底失败 current={current_page}; "
                  f"error={type(exc).__name__}: {_safe_debug_error(exc)}")
            return None

        landed = await self._settled_route_page(page, expected=next_page)
        if landed == current_page:
            debug(f"网络课程分页: SPA 路由未前进，判定末页 current={current_page}")
            return {"moved": False, "page": None, "reason": "route-last"}
        if landed != next_page:
            return None
        if before.get("cards_set") is not None:
            after = await self._wait_online_course_page_change(
                page, before, timeout_ms=ONLINE_PAGE_ROUTE_TIMEOUT_MS,
                keys=("cards_set",))
            if after is None:
                debug(f"网络课程分页: 路由已前进但卡片未变化，判定深链未生效 current={current_page}")
                return None
        debug(f"网络课程分页: 通过 SPA 路由翻页 current={current_page}; "
              f"target={_safe_debug_url(target)}")
        return {"moved": True, "page": next_page, "reason": "route"}

    async def _advance_online_course_page_state(self, page: Page, list_url: str,
                                                current_page: int) -> dict:
        """翻到下一页并校验翻页真的生效。

        moved=True  已确认换页（page 为观测到的页码，未知时为 None）
        moved=False 确认没有下一页（控件禁用，或路由跳转被夹回当前页）
        moved=None  暂时无法翻页（未识别入口/网络问题），稍后重试
        """
        before = await self._online_course_page_signature(page)
        controls, disabled_seen = await self._online_course_next_controls(page)
        clicked = 0
        # 最多试 2 个候选控件：每个失败都要等一个校验窗口，避免卡太久
        for item, label, classes in controls[:2]:
            try:
                await item.click(timeout=5000)
            except Exception as exc:
                debug(f"网络课程分页控件点击失败 current={current_page}; "
                      f"error={type(exc).__name__}: {_safe_debug_error(exc)}")
                continue
            clicked += 1
            after = await self._wait_online_course_page_change(page, before)
            if after is not None:
                debug(f"网络课程分页: 控件翻页已确认 current={current_page}; "
                      f"label={label!r}; class={classes!r}")
                return {"moved": True,
                        "page": self._observed_advanced_page(after, current_page),
                        "reason": "control"}
            debug(f"网络课程分页: 控件点击后页面未变化 current={current_page}; label={label!r}")

        if disabled_seen and not clicked:
            # 「下一页」明确禁用就是末页，不必再白发一次路由跳转。
            debug(f"网络课程分页: 下一页控件明确禁用 current={current_page}")
            return {"moved": False, "page": None, "reason": "disabled"}

        route_result = await self._advance_online_course_page_by_route(
            page, list_url, current_page, before)
        if route_result is not None:
            return route_result

        if disabled_seen:
            debug(f"网络课程分页: 下一页控件明确禁用 current={current_page}")
            return {"moved": False, "page": None, "reason": "disabled"}
        if clicked:
            # 控件点了没反应、路由也没能给出结论：不轻易判定末页，留给下一轮重试。
            debug(f"网络课程分页: 控件点击未生效且路由无法判定 current={current_page}")
            return {"moved": None, "page": None, "reason": "no-effect"}
        debug(f"网络课程分页: 未识别下一页控件 current={current_page}; url={_page_debug_state(page)}")
        return {"moved": None, "page": None, "reason": "no-control"}

    async def _advance_online_course_page(self, page: Page, list_url: str,
                                          current_page: int) -> Optional[bool]:
        """兼容旧签名：只返回是否翻页成功（True/False/None）。"""
        result = await self._advance_online_course_page_state(page, list_url, current_page)
        return result.get("moved")

    async def _goto_online_course_page(self, page: Page, list_url: str,
                                       target_page: int, *,
                                       timeout_ms: int = ONLINE_PAGE_DEEP_LINK_TIMEOUT_MS) -> bool:
        """直接用 /list/N 深链跳到目标页；落地路由与卡片都校验通过才返回 True。"""
        if target_page <= 1:
            return False
        try:
            parts = urlsplit(_page_url(page) or list_url)
            match = re.match(r"^(.*?/list/)\d+$", parts.fragment.rstrip("/"))
            if not match:
                return False
            if self._route_page_index(_page_url(page)) == target_page:
                return await self._wait_online_course_cards(page, 3000)
            target = parts._replace(fragment=f"{match.group(1)}{target_page}").geturl()
            await page.goto(target, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            debug(f"网络课程列表深链跳转失败 page={target_page}; "
                  f"error={type(exc).__name__}: {_safe_debug_error(exc)}")
            return False
        landed = await self._settled_route_page(page, expected=target_page)
        if landed != target_page:
            debug(f"网络课程列表深链未落在目标页 page={target_page}; landed={landed}")
            return False
        if not await self._wait_online_course_cards(page, timeout_ms):
            debug(f"网络课程列表深链后卡片未渲染 page={target_page}")
            return False
        debug(f"网络课程列表深链直达第 {target_page} 页")
        return True

    async def _open_online_course_from_list(self, worker_page: Page, list_url: str,
                                            task: dict, worker_id: int = -1) -> Page:
        """仅在卡片没有可用直达地址时，回列表页定位并打开课程。"""
        if not await self._load_online_course_list(worker_page, list_url, worker_id=worker_id):
            debug(f"[工作线程 {worker_id+1}] 网络自学课程列表加载失败: 课程卡片未渲染; "
                  f"{_page_debug_state(worker_page)}")
            raise OnlineCourseListUnavailable("课程列表未加载")

        target_page = int(task.get("page", 1) or 1)
        if target_page > 1:
            # 优先一次深链直达：顺序点击要翻 N-1 次，任何一次被 SPA 吞掉都会失败。
            if not await self._goto_online_course_page(worker_page, list_url, target_page):
                # 深链不可用：回到第 1 页再顺序翻页（列表地址可能与当前路由不同）。
                if not await self._load_online_course_list(worker_page, list_url,
                                                           worker_id=worker_id):
                    raise OnlineCourseListUnavailable("课程列表未加载")
                for _ in range(target_page - 1):
                    try:
                        result = await self._advance_online_course_page_state(
                            worker_page, list_url, _ + 1)
                        if result.get("moved") is not True:
                            raise OnlineCourseListUnavailable("课程列表翻页入口不可用")
                        if not await self._wait_online_course_cards(worker_page, 12000):
                            raise OnlineCourseListUnavailable("课程列表翻页后未加载")
                    except OnlineCourseListUnavailable:
                        raise
                    except Exception as exc:
                        raise OnlineCourseListUnavailable("课程列表翻页失败") from exc

        links = worker_page.locator(ONLINE_COURSE_LIST_CARD_SELECTOR)
        count = await links.count()
        href = task.get("href", "")
        title = task["title"]
        match = None
        title_match = None
        for index in range(count):
            candidate = links.nth(index)
            try:
                candidate_title = (await candidate.get_attribute("title") or "").strip()
                candidate_href = (await candidate.get_attribute("href") or "").strip()
            except Exception:
                continue
            if href and candidate_href and href.rstrip("/") == candidate_href.rstrip("/"):
                match = candidate
                break
            if candidate_title and title in candidate_title and title_match is None:
                title_match = candidate
        if match is None:
            match = title_match
        if match is None:
            raise OnlineCourseListUnavailable("课程列表已变化，暂未找到目标课程")

        # 监听该 worker 页自己的 popup，避免并发 worker 的弹窗被误认。
        popup = asyncio.create_task(worker_page.wait_for_event("popup", timeout=10000))
        await asyncio.sleep(0)
        old_url = worker_page.url
        debug(f"[工作线程 {worker_id+1}] phase=open_course_from_list title={task.get('title', '')!r}; worker={_page_debug_state(worker_page)}")
        try:
            await match.click()
            for _ in range(20):
                if popup.done() and not popup.cancelled() and popup.exception() is None:
                    course_page = popup.result()
                    debug(f"[工作线程 {worker_id+1}] phase=course_popup_opened; {_page_debug_state(course_page)}")
                    try:
                        await course_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        # The learning site is an SPA: DOMContentLoaded often precedes
                        # rendering its course detail content and action button.
                        settle = getattr(course_page, "wait_for_timeout", None)
                        if callable(settle):
                            settled = settle(5000)
                            if inspect.isawaitable(settled):
                                await settled
                        debug(f"[工作线程 {worker_id+1}] phase=course_popup_ready; {_page_debug_state(course_page)}")
                    except Exception:
                        debug(f"[工作线程 {worker_id+1}] phase=course_popup_load_error; {_page_debug_state(course_page)}")
                        if not course_page.is_closed():
                            await course_page.close()
                        raise
                    return course_page
                if not worker_page.is_closed() and worker_page.url != old_url:
                    debug(f"[工作线程 {worker_id+1}] phase=course_same_tab_navigation; before={_safe_debug_url(old_url)}; after={_page_debug_state(worker_page)}")
                    return worker_page
                await worker_page.wait_for_timeout(500)
            raise OnlineCourseListUnavailable("点击课程后未打开详情页或播放页")
        finally:
            if not popup.done():
                popup.cancel()
            await asyncio.gather(popup, return_exceptions=True)

    async def _collect_online_playlist_tasks(self, page: Page, parent: dict,
                                             done_keys=None) -> Optional[list]:
        """读取网络自学播放页的目录，目录视频分别成为共享队列任务。

        只接受路由中明确带有 pKnowledgeId 的目录项；单视频页面或无法确认
        目录身份的页面返回空列表，由原有单视频流程继续处理。
        """
        entries = await page.evaluate(r"""() => {
            const current = new URL(location.href);
            const hashParts = current.hash.split('?');
            const route = (hashParts[0] || '').replace(/^#/, '');
            if (!/\/play\//.test(route)) return [];
            const paramsFrom = (value) => {
                const hash = String(value || '').split('#').pop() || '';
                const query = hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : '';
                return new URLSearchParams(query);
            };
            const makeHref = (id, rawHref) => {
                if (rawHref) {
                    try {
                        const resolved = new URL(rawHref, location.href);
                        const hash = resolved.hash || '';
                        if (/\/play\//.test(hash) && paramsFrom(hash).get('pKnowledgeId')) return resolved.href;
                    } catch (e) {}
                }
                const params = new URLSearchParams(hashParts[1] || '');
                if (!params.has('cid') || !id) return '';
                params.set('pKnowledgeId', String(id));
                return current.origin + current.pathname + '#' + route + '?' + params.toString();
            };
            const result = new Map();
            const add = (id, title, rawHref) => {
                id = String(id || '').trim();
                title = String(title || '').replace(/\s+/g, ' ').trim();
                const href = makeHref(id, rawHref);
                if (!id || !title || !href) return;
                const params = paramsFrom(href);
                if (params.get('pKnowledgeId') !== id) return;
                if (id === paramsFrom(location.href).get('pKnowledgeId')) {
                    // 当前选中项也要参与统一去重/进度判断，故仍加入目录。
                }
                if (!result.has(id)) result.set(id, {id, title: title.slice(0, 100), href});
            };

            // 先取带完整播放路由的链接，以及平台常见的知识点 data-* 属性。
            const selectors = 'a[href], [data-pknowledgeid], [data-p-knowledge-id], [data-knowledgeid], [data-knowledge-id], [pknowledgeid]';
            for (const el of document.querySelectorAll(selectors)) {
                const href = el.getAttribute('href') || el.getAttribute('data-href') || '';
                const attrs = ['data-pknowledgeid', 'data-p-knowledge-id', 'data-knowledgeid', 'data-knowledge-id', 'pknowledgeid'];
                let id = '';
                for (const name of attrs) { id = el.getAttribute(name) || ''; if (id) break; }
                if (!id && href) id = paramsFrom(href).get('pKnowledgeId') || '';
                if (id) add(id, el.getAttribute('title') || el.getAttribute('aria-label') || el.innerText, href);
            }

            // SPA 目录常把目录对象留在 Vue props/data，而不是暴露 href。
            const visited = new Set();
            const walk = (value, depth) => {
                if (!value || typeof value !== 'object' || depth > 5 || visited.has(value)) return;
                visited.add(value);
                if (Array.isArray(value)) {
                    for (const item of value.slice(0, 300)) walk(item, depth + 1);
                    return;
                }
                const keys = Object.keys(value);
                const idKey = keys.find(k => /^(pKnowledgeId|knowledgeId|p_knowledge_id)$/i.test(k));
                const nameKey = keys.find(k => /^(resourceName|knowledgeName|videoName|chapterName|title|name)$/i.test(k));
                if (idKey && nameKey) {
                    const id = value[idKey];
                    const title = value[nameKey];
                    const rawHref = value.href || value.url || value.route || '';
                    if (typeof id === 'string' || typeof id === 'number') add(id, title, rawHref);
                }
                for (const key of keys) {
                    if (/list|knowledge|video|chapter|lesson|resource|node|item|data|children/i.test(key)) {
                        walk(value[key], depth + 1);
                    }
                }
            };
            for (const el of document.querySelectorAll('*')) {
                if (el.__vue__) {
                    const vm = el.__vue__;
                    walk(vm.$props, 0);
                    walk(vm.$data, 0);
                    walk(vm.item, 0);
                    walk(vm.data, 0);
                }
            }
            return [...result.values()];
        }""")
        tasks = _build_online_playlist_tasks(parent, entries, done_keys)
        if len(entries or []) > 1:
            debug(f"网络课程目录识别: course={parent.get('title', '')!r}; items={len(entries)}; pending={len(tasks)}")
            return tasks
        return None

    async def _enter_online_course_player(self, detail_page: Page, worker_id: int) -> Optional[Page]:
        """直接播放页原样返回；详情页 100% 返回 None，否则点击入口。"""
        entry_labels = ("立即学习", "我要学习", "开始学习", "进入课程",
                        "继续学习", "学习课程", "进入课程学习", "重新学习")
        is_detail_route = "/detail/" in detail_page.url.lower()
        initial_url = detail_page.url
        debug(f"[工作线程 {worker_id+1}] phase=classify_page detail_route={is_detail_route}; {_page_debug_state(detail_page)}")
        if not is_detail_route:
            return detail_page

        async def find_entry():
            button = detail_page.locator("button.to-learn").first
            if await button.count():
                return button
            for label in entry_labels:
                candidate = detail_page.get_by_role("button", name=label, exact=True).first
                if await candidate.count():
                    return candidate
                # Some versions render the call-to-action as an anchor or a
                # styled div/span rather than a semantic button.
                candidate = detail_page.get_by_text(label, exact=True).first
                if await candidate.count():
                    try:
                        if await candidate.is_visible():
                            return candidate
                    except Exception:
                        pass
            return None

        percent = -1.0
        button = None
        # Wait for the SPA to render progress and its action. Direct-play pages
        # have neither, so they still pass through unchanged after a short probe.
        for _ in range(10):
            progress = detail_page.locator(".progress-contain [role='progressbar']").first
            if await progress.count():
                try:
                    percent = float(await progress.get_attribute("aria-valuenow") or -1)
                except (TypeError, ValueError):
                    percent = -1
                if percent >= 100:
                    debug(f"[工作线程 {worker_id+1}] phase=classify_page progress={percent:g}; skip_playback; {_page_debug_state(detail_page)}")
                    return None
            button = await find_entry()
            if button is not None:
                break
            if not is_detail_route:
                return detail_page
            await detail_page.wait_for_timeout(500)

        if button is None:
            if not is_detail_route:
                debug(f"[工作线程 {worker_id+1}] phase=classify_page no_entry_but_not_detail; {_page_debug_state(detail_page)}")
                return detail_page
            debug(f"[工作线程 {worker_id+1}] phase=classify_page entry_not_found; initial_url={_safe_debug_url(initial_url)}; {_page_debug_state(detail_page)}")
            raise RuntimeError("课程详情页没有学习入口按钮（页面内容未渲染或入口类型未知）")

        existing_pages = set(detail_page.context.pages)
        label = (await button.inner_text()).strip()
        if label == "重新学习" and percent < 0:
            raise RuntimeError("课程详情页进度未加载，暂不点击“重新学习”以免重学已完成课程")
        debug(f"[工作线程 {worker_id+1}] phase=click_entry label={label!r}; progress={percent:g}; before={_page_debug_state(detail_page)}; open_pages={len(existing_pages)}")
        await button.click()
        for poll_index in range(16):
            if self._stop_event.is_set():
                raise RuntimeError("学习任务已停止")
            new_pages = [p for p in detail_page.context.pages if p not in existing_pages]
            if new_pages:
                player_page = new_pages[-1]
                try:
                    await player_page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    if not player_page.is_closed():
                        await player_page.close()
                    raise
                debug(f"[工作线程 {worker_id+1}] phase=player_opened via=popup after={poll_index+1} polls; {_page_debug_state(player_page)}")
                return player_page
            if not detail_page.is_closed() and "/course/#/detail/" not in detail_page.url:
                debug(f"[工作线程 {worker_id+1}] phase=player_opened via=same_tab after={poll_index+1} polls; {_page_debug_state(detail_page)}")
                return detail_page
            if not detail_page.is_closed() and await detail_page.query_selector("video, audio, .prism-player"):
                debug(f"[工作线程 {worker_id+1}] phase=player_opened via=media_on_detail after={poll_index+1} polls; {_page_debug_state(detail_page)}")
                return detail_page
            await detail_page.wait_for_timeout(500)
        debug(f"[工作线程 {worker_id+1}] phase=player_open_timeout label={label!r}; initial_url={_safe_debug_url(initial_url)}; {_page_debug_state(detail_page)}; open_pages={len(detail_page.context.pages)}")
        raise RuntimeError(f"点击“{label}”后仍停留在课程详情页，未进入播放页")

    async def learn_course_list(self, list_url: str = "https://u.ccb.com/course/#/list/1",
                                log_callback=None, progress_callback=None, hours_callback=None):
        """网络自学：从课程列表页 /course/#/list/1 采集课程并学习（GUI自动模式使用）。

        与专题班流程不同：网络自学直接从课程列表页点开课程播放，
        而不是进入专题班再找课程。学习目标由 self.goal_type / self.study_goal 控制。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        _progress = progress_callback or (lambda d: None)
        _hours = hours_callback or (lambda d: None)

        # 使用独立列表页采集课程，避免与学习中的 worker 页面争用。
        page = await self.context.new_page()
        _log(f"网络自学: 从课程列表加载 {list_url}", "blue")
        if not await self._load_online_course_list(page, list_url):
            _log("课程列表加载失败: 课程卡片未渲染", "red")
            self.last_stats = (0, 0)
            try:
                await page.close()
            except Exception:
                pass
            return False

        # 检查登录态，Session过期则等待用户重新登录
        try:
            body = await page.locator("body").inner_text(timeout=3000)
            if "立即登录" in body or "密码登录" in body or "统一认证" in body:
                _log("Session过期，请在浏览器中重新登录...", "red")
                try:
                    await page.goto("https://u.ccb.com/portal/#/study",
                                    wait_until="domcontentloaded", timeout=15000)
                except:
                    pass
                for _ in range(120):
                    if self._stop_event.is_set():  # 停止学习时立即退出等待
                        break
                    await asyncio.sleep(2)
                    try:
                        check_body = await page.locator("body").inner_text(timeout=2000)
                        if "立即登录" not in check_body and "密码登录" not in check_body:
                            break
                    except:
                        pass
                _log("登录成功，继续加载...", "green")
                if not await self._load_online_course_list(page, list_url):
                    _log("课程列表加载失败: 课程卡片未渲染", "red")
                    self.last_stats = (0, 0)
                    try:
                        await page.close()
                    except Exception:
                        pass
                    return False
        except:
            pass

        # 网络自学使用共享队列；worker 消费完当前课程后会继续取课。
        # 队列将空时，单独的列表页串行翻页并补充新课程。
        done_titles = self.load_completed_course_titles()
        done_keys = set(self.load_progress().get("completed_course_keys", []))
        if done_titles:
            _log(f"已有 {len(done_titles)} 门课程学过，将跳过", "blue")
        seen_titles = set()
        seen_pages = {}  # 逻辑页码 -> 该页已采集的卡片指纹
        page_num = 1
        no_more_pages = False
        retry_current_page = False
        saw_courses = False
        course_queue = asyncio.Queue()
        fetch_lock = asyncio.Lock()
        goal_reached = asyncio.Event()
        pool_changed = asyncio.Event()
        active_workers = [0]
        prefetch_tasks = set()

        async def collect_current_page():
            nonlocal no_more_pages, retry_current_page, saw_courses
            loaded = False
            for attempt in range(2):
                try:
                    await page.wait_for_selector(ONLINE_COURSE_LIST_CARD_SELECTOR, timeout=10000)
                    loaded = True
                    break
                except Exception:
                    if attempt < 1:
                        await page.wait_for_timeout(2000)
            if not loaded:
                # 网络抖动不能等价于“没有下一页”；保留当前页，后续补课时重试。
                retry_current_page = True
                _log(f"课程列表第 {page_num} 页暂时未加载，稍后重试", "yellow")
                return 0
            cards = page.locator(ONLINE_COURSE_LIST_CARD_SELECTOR)
            cnt = await cards.count()
            if cnt == 0:
                _log(f"课程列表第 {page_num} 页暂时为空，稍后重试", "yellow")
                retry_current_page = True
                return 0
            saw_courses = True

            cards_data = []
            for i in range(cnt):
                try:
                    title = (await cards.nth(i).get_attribute("title") or "").strip()
                    href = (await cards.nth(i).get_attribute("href") or "").strip()
                except Exception:
                    title = ""
                    href = ""
                if title:
                    cards_data.append({"title": title, "href": href, "index": i})

            # 同一逻辑页码的同一批卡片只算一次：先记下该页码采到的指纹。
            # 翻页本身已由 _advance_online_course_page_state 校验，这里不再
            # 用「内容没变」去判定翻页失败（那会把页码和实际页面搞散）。
            fingerprint = tuple((item["title"], item["href"]) for item in cards_data)
            if seen_pages.get(page_num) == fingerprint:
                _log(f"课程列表第 {page_num} 页已采集过，跳过重复扫描", "blue")
                return 0
            seen_pages[page_num] = fingerprint

            added = 0
            for item in cards_data:
                title = item["title"]
                href = item["href"]
                course_key = href or f"page:{page_num}:index:{item['index']}"
                if course_key in seen_titles:
                    continue
                seen_titles.add(course_key)
                # 新记录优先按 href 去重；旧版本只有标题记录时才回退到标题。
                if (href and href in done_keys) or (not href and title[:60] in done_titles):
                    continue
                course_queue.put_nowait({"page": page_num, "title": title[:60],
                                         "href": href, "key": course_key})
                added += 1
            if added:
                pool_changed.set()
            _log(f"课程列表第 {page_num} 页: {cnt} 门（新增 {added}）", "blue")
            return added

        async def fetch_more_courses(force=False):
            nonlocal page_num, no_more_pages, retry_current_page
            async with fetch_lock:
                if self._stop_event.is_set() or goal_reached.is_set() or no_more_pages:
                    return 0
                if course_queue.qsize() > 0 and not force:
                    return course_queue.qsize()

                if retry_current_page:
                    retry_current_page = False
                    # 翻页状态通常只保存在前端内存；非首页刷新会跳回第 1 页。
                    if page_num == 1:
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=20000)
                        except Exception as exc:
                            retry_current_page = True
                            _log(f"课程列表刷新失败，稍后重试: {exc}", "yellow")
                            return 0
                    added = await collect_current_page()
                    if added > 0:
                        _log(f"重试当前页后新增 {added} 门课程", "green")
                    return added

                while not no_more_pages and not self._stop_event.is_set():
                    try:
                        result = await self._advance_online_course_page_state(
                            page, list_url, page_num)
                    except Exception as e:
                        _log(f"翻到下一页失败，稍后重试: {e}", "yellow")
                        retry_current_page = True
                        return 0
                    moved = result.get("moved")
                    if moved is False:
                        no_more_pages = True
                        _log(f"课程列表已到最后一页（第 {page_num} 页）", "blue")
                        return 0
                    if moved is None:
                        _log(f"暂未识别第 {page_num} 页的下一页入口，稍后重试补课", "yellow")
                        return 0
                    # 逻辑页码跟着观测结果走：只有确认翻页了才前进，
                    # 观测到具体页码时以观测值为准，避免页码与实际页面错位。
                    observed = result.get("page")
                    if isinstance(observed, int) and observed > page_num:
                        page_num = observed
                    else:
                        page_num += 1

                    added = await collect_current_page()
                    if added > 0:
                        _log(f"课程池新增 {added} 门课程", "green")
                        return added
                    if retry_current_page:
                        return 0

                return 0

        async def prefetch_courses():
            try:
                await fetch_more_courses()
            except Exception as e:
                _log(f"补充课程失败: {e}", "yellow")

        def schedule_prefetch():
            task = asyncio.create_task(prefetch_courses())
            prefetch_tasks.add(task)
            task.add_done_callback(prefetch_tasks.discard)

        await collect_current_page()
        initial_retries = 0
        while retry_current_page and course_queue.empty() and initial_retries < 2:
            initial_retries += 1
            await asyncio.sleep(2 * initial_retries)
            await fetch_more_courses(force=True)
        # 启动时尽量给每个 worker 一门课；之后再按需补充，避免提前扫完所有页面。
        bootstrap_attempts = 0
        while course_queue.qsize() < self.workers and not no_more_pages:
            before = course_queue.qsize()
            before_page = page_num
            await fetch_more_courses(force=True)
            if course_queue.qsize() > before:
                bootstrap_attempts = 0
                continue
            if no_more_pages:
                break
            bootstrap_attempts += 1
            if bootstrap_attempts >= 3:
                _log(f"初始补充课程池暂不可用，先以 {course_queue.qsize()} 门课程启动；worker 空闲时会继续补充", "yellow")
                break
            if page_num == before_page:
                await asyncio.sleep(2 * bootstrap_attempts)

        if course_queue.qsize() == 0:
            _log("课程列表没有待学习课程" if saw_courses else "课程列表未获取到课程", "yellow")
            self.last_stats = (0, 0)
            try:
                await page.close()
            except Exception:
                pass
            return bool(saw_courses and not self._stop_event.is_set())

        # 即使初始课程数少于 worker，也全部启动：某个列表型课程展开后会
        # 把多个视频子任务补入同一个池，空闲 worker 不能提前退出。
        nw = min(self.workers, len(self.pages))
        _log(f"课程池初始采集 {course_queue.qsize()} 门课程，启动 {nw} 个线程", "bold blue")

        ok_count = [0]
        skipped_count = [0]
        fail_count = [0]
        list_open_semaphore = asyncio.Semaphore(2)

        async def course_task_stream(wid: int):
            list_retries = 0
            while not self._stop_event.is_set() and not goal_reached.is_set():
                try:
                    task = course_queue.get_nowait()
                    list_retries = 0
                    debug(f"[线程{wid+1}] phase=dequeue course={task.get('title', '')!r}; queued={course_queue.qsize()}; no_more_pages={no_more_pages}")
                    active_workers[0] += 1
                    if course_queue.empty() and not no_more_pages:
                        # 趁 worker 正在学习当前课程时预取下一页，减少池空后的等待。
                        schedule_prefetch()
                    try:
                        yield task
                    finally:
                        active_workers[0] = max(0, active_workers[0] - 1)
                        pool_changed.set()
                    if not self._stop_event.is_set() and not goal_reached.is_set():
                        _progress({"wid": wid, "course": "-", "progress": "-", "eta": "-",
                                   "status": "等待下一门"})
                    continue
                except asyncio.QueueEmpty:
                    if active_workers[0] > 0:
                        # 仍有 worker 正在打开/识别课程目录或播放视频；
                        # 等它补入列表子任务，而不是把本 worker 永久置为空闲。
                        pool_changed.clear()
                        if not course_queue.empty():
                            continue
                        try:
                            await asyncio.wait_for(pool_changed.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                        continue
                    _progress({"wid": wid, "course": "-", "progress": "-", "eta": "-",
                               "status": "检查课程队列"})
                    debug(f"[线程{wid+1}] phase=queue_empty queued=0; no_more_pages={no_more_pages}; retry_current_page={retry_current_page}")
                    added = await fetch_more_courses()
                    debug(f"[线程{wid+1}] phase=queue_fetch_done added={added}; queued={course_queue.qsize()}; no_more_pages={no_more_pages}; retry_current_page={retry_current_page}")
                    if added <= 0:
                        if no_more_pages:
                            _progress({"wid": wid, "course": "-", "progress": "-", "eta": "-",
                                       "status": "空闲"})
                            debug(f"[线程{wid+1}] phase=worker_idle reason=course_list_exhausted")
                            return
                        list_retries += 1
                        if list_retries >= 3:
                            _progress({"wid": wid, "course": "-", "progress": "-", "eta": "-",
                                       "status": "列表异常"})
                            raise OnlineCourseListUnavailable("课程列表连续加载失败，已暂停当前阶段；未完成课程下次仍可继续")
                        _log(f"课程列表暂不可用，{min(5 * list_retries, 10)} 秒后重试补课", "yellow")
                        await asyncio.sleep(min(5 * list_retries, 10))
            if goal_reached.is_set():
                _progress({"wid": wid, "course": "-", "progress": "-", "eta": "-", "status": "目标达成"})
            elif self._stop_event.is_set():
                _progress({"wid": wid, "course": "-", "progress": "-", "eta": "-", "status": "已停止"})

        async def cworker(wid: int, wp: Page):
            async for task in course_task_stream(wid):
                # 用户变更配置：停止取新课程
                if self._stop_event.is_set():
                    break
                if wp.is_closed():
                    wp = await self.context.new_page()
                    self.pages[wid] = wp
                ctitle = task["title"]
                chref, ckey = task.get("href", ""), task.get("key", "")
                _log(f"[线程{wid+1}] {ctitle}", "blue")
                _progress({"wid": wid, "course": ctitle[:40], "progress": "-", "eta": "-", "status": "加载中"})
                done_ok = False
                already_complete = False
                deferred = False
                expanded_count = 0
                for attempt in range(1, 3):  # 每门课程最多重试1次
                    cp = None
                    play_page = None
                    phase = "prepare"
                    try:
                        if wp.is_closed():
                            debug(f"[线程{wid+1}] phase=recover_worker_page attempt={attempt}; 原页面已关闭，重建标签页")
                            wp = await self.context.new_page()
                            self.pages[wid] = wp
                        # 采集时已有可导航地址，就让 worker 直达课程，避免十几个
                        # worker 每门课都刷新列表页导致页面限流或超时。
                        target_url = _online_course_target_url(list_url, chref)
                        if target_url:
                            try:
                                phase = "navigate_direct"
                                debug(f"[线程{wid+1}] phase={phase} attempt={attempt}; course={ctitle!r}; target={_safe_debug_url(target_url)}")
                                await wp.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                                await wp.wait_for_timeout(3000)
                                if wp.url == list_url:
                                    raise OnlineCourseListUnavailable("课程地址返回列表页")
                                cp = wp
                                debug(f"[线程{wid+1}] phase={phase}_ready; {_page_debug_state(cp)}")
                            except Exception as exc:
                                _log(f"[线程{wid+1}] 课程直达失败，回列表重试: {str(exc)[:120]}", "yellow")
                        if cp is None:
                            phase = "open_from_list"
                            debug(f"[线程{wid+1}] phase={phase} attempt={attempt}; course={ctitle!r}; {_page_debug_state(wp)}")
                            async with list_open_semaphore:
                                cp = await self._open_online_course_from_list(wp, list_url, task, wid)
                            debug(f"[线程{wid+1}] phase=course_page_ready; {_page_debug_state(cp)}")
                        # 3) 详情页先进入播放器；不能在信息页反复刷新找视频。
                        phase = "enter_player"
                        play_page = await self._enter_online_course_player(cp, wid)
                        if play_page is None:
                            already_complete = True
                            done_ok = True
                            break
                        # 播放页若包含多个带独立 pKnowledgeId 的目录项，就拆成
                        # 子任务共享给所有 worker；普通单视频页保持旧流程。
                        if not task.get("playlist_child"):
                            child_tasks = await self._collect_online_playlist_tasks(play_page, task, done_keys)
                            if child_tasks is not None:
                                for child in child_tasks:
                                    course_queue.put_nowait(child)
                                expanded_count = len(child_tasks)
                                if expanded_count:
                                    pool_changed.set()
                                    debug(f"[线程{wid+1}] phase=playlist_expanded parent={ctitle!r}; children={expanded_count}; queued={course_queue.qsize()}")
                                else:
                                    # 目录存在但每个视频的独立断点键都已完成。
                                    already_complete = True
                                    done_ok = True
                                break
                        # 4) 播放视频（检查返回值：失败走重试）
                        phase = "find_play_video"
                        debug(f"[线程{wid+1}] phase={phase} course={ctitle!r}; course_page={_page_debug_state(cp)}; player_page={_page_debug_state(play_page)}")
                        _progress({"wid": wid, "course": ctitle[:40], "progress": "0%", "eta": "-", "status": "学习中"})
                        def on_progress(pct):
                            _progress({"wid": wid, "course": ctitle[:40],
                                       "progress": f"{pct:.0f}%", "eta": "-", "status": "学习中"})
                        ok = await self.find_and_play_video(play_page, wid, on_progress)
                        if not ok:
                            raise RuntimeError("视频未完成（未找到播放器或进度停滞）")
                        done_ok = True
                        break
                    except asyncio.CancelledError:
                        raise
                    except OnlineCourseListUnavailable as e:
                        if not self._stop_event.is_set() and _defer_online_course(course_queue, task):
                            deferred = True
                            _progress({"wid": wid, "course": ctitle[:40], "progress": "-", "eta": "-",
                                       "status": "等待列表恢复"})
                            _log(f"[线程{wid+1}] {e}，延后重试: {ctitle}", "yellow")
                            await asyncio.sleep(3 * task["list_failures"])
                        else:
                            _log(f"[线程{wid+1}] 课程列表持续不可用，保留待下次运行: {ctitle} - {e}", "red")
                            _progress({"wid": wid, "course": ctitle[:40], "progress": "-", "eta": "-", "status": "异常"})
                        break
                    except Exception as e:
                        debug(f"[线程{wid+1}] phase={phase} attempt={attempt} course={ctitle!r} exception={type(e).__name__}: {_safe_debug_error(e)}; worker={_page_debug_state(wp)}; course_page={_page_debug_state(cp)}; player_page={_page_debug_state(play_page)}")
                        if attempt < 2:
                            _log(f"[线程{wid+1}] 第{attempt}次失败，重试: {ctitle} - {str(e)[:180]}", "yellow")
                            await asyncio.sleep(2)
                            continue
                        _log(f"[线程{wid+1}] 课程失败: {ctitle} - {e}", "red")
                        _progress({"wid": wid, "course": ctitle[:40], "progress": "-", "eta": "-", "status": "异常"})
                        break
                    finally:
                        for opened in (play_page, cp):
                            if opened and opened is not wp and not opened.is_closed():
                                try:
                                    await opened.close()
                                except Exception:
                                    pass

                if deferred:
                    continue
                if expanded_count:
                    _progress({"wid": wid, "course": ctitle[:40], "progress": "-", "eta": "-",
                               "status": f"已拆分 {expanded_count} 个视频"})
                    _log(f"[线程{wid+1}] 列表课程已拆分为 {expanded_count} 个独立视频任务: {ctitle}", "green")
                    continue
                if done_ok:
                    if already_complete:
                        skipped_count[0] += 1
                    else:
                        ok_count[0] += 1
                    # O3 断点续学：记录已学课程，中断后重跑自动跳过
                    try:
                        self.mark_course_completed(ctitle, ckey)
                    except Exception:
                        pass
                    _progress({"wid": wid, "course": ctitle[:40], "progress": "100%", "eta": "-",
                               "status": "已完成" if already_complete else "✓ 完成"})
                    _log(f"[线程{wid+1}] {'已学完，跳过' if already_complete else '完成'}: {ctitle}", "green")
                    debug(f"[线程{wid+1}] phase=course_done course={ctitle!r}; queued={course_queue.qsize()}; no_more_pages={no_more_pages}")
                else:
                    fail_count[0] += 1

                # 5) 完成一门课后检查目标（O1 节流：60s TTL 缓存合并并发查询）
                if self.study_goal > 0:
                    try:
                        _progress({"wid": wid, "course": ctitle[:40], "progress": "100%", "eta": "-",
                                   "status": "更新学时"})
                        debug(f"[线程{wid+1}] phase=study_hours_check_start course={ctitle!r}")
                        h = await self._get_study_hours(wp)
                        _hours({"central": h.get("central", 0), "online": h.get("online", 0),
                                "updated": datetime.now().strftime("%H:%M:%S")})
                        cur = h.get(self.goal_type, 0)
                        _log(f"网络自学进度: {cur:.1f}/{self.study_goal} 学时", "blue")
                        debug(f"[线程{wid+1}] phase=study_hours_check_done current={cur:.1f}; target={self.study_goal:.1f}")
                        if cur >= self.study_goal:
                            _log(f"✓ 网络自学目标已达成!", "bold green")
                            goal_reached.set()
                            _progress({"wid": wid, "course": ctitle[:40], "progress": "100%", "eta": "-",
                                       "status": "目标达成"})
                            raise GoalReached()
                    except GoalReached:
                        raise
                    except Exception as exc:
                        debug(f"[线程{wid+1}] phase=study_hours_check_failed exception={type(exc).__name__}: {_safe_debug_error(exc)}")
                        pass

        tasks = [asyncio.create_task(cworker(wid, self.pages[wid])) for wid in range(nw)]
        try:
            await asyncio.gather(*tasks)
        except GoalReached:
            for t in tasks:
                if not t.done():
                    t.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except:
                pass
        except OnlineCourseListUnavailable as exc:
            _log(str(exc), "red")
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.last_stats = (ok_count[0], fail_count[0])
            return False
        except Exception:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if prefetch_tasks:
                await asyncio.gather(*list(prefetch_tasks), return_exceptions=True)
            try:
                await page.close()
            except Exception:
                pass
        self.last_stats = (ok_count[0], fail_count[0])
        _log(f"网络自学阶段完成: 新学 {ok_count[0]} 门, 已学跳过 {skipped_count[0]} 门, 失败 {fail_count[0]} 门", "bold green")
        # 只有全部课程成功且没有被用户停止，阶段才算完成。
        return bool(ok_count[0] + skipped_count[0] > 0 and fail_count[0] == 0
                    and not self._stop_event.is_set())

    @staticmethod
    def _is_learnable(action: str, hours: str = "", progress: str = "") -> bool:
        """判断课程是否可以学习（未完成或进行中）。

        频道里的专题班课程表只有「类型/标题/必选修」三列、没有「操作」列，
        这时不能一律判成不可学（原来 action 为空直接 False，整张表会被过滤光），
        改看进度：读到 100% 才算学完。
        """
        if not action:
            return not _progress_completed(progress)
        # 跳过0学时课程
        try:
            h = float(hours) if hours else -1
            if h == 0:
                return False
        except:
            pass
        # 100%完成 → 不需要学
        if action in ('立即回看', '学习完成', '已完成', '已学习'):
            return False
        # 明确可学的状态
        if action in ('立即学习', '继续学习', '继续回看',
                       '开始学习', '进入课程', '学习课程'):
            return True
        # 含"学习"但不含"完成"
        if '学习' in action and '完成' not in action:
            return True
        return False


    async def _collect_workshops_courses(self, page: Page, workshops: List[Dict],
                                          completed_ids: set = None, log_callback=None) -> tuple:
        """预收集：并行 enroll + 获取课程列表，跳过已完成的专题班"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        if completed_ids is None:
            completed_ids = set()

        to_process = list(workshops)

        if completed_ids:
            _log(f"已完成 {len(completed_ids)} 个专题班，将跳过", "green")

        all_tasks = []       # [(ws_id, course_idx, course_info, ws_title), ...]
        ws_locks = {}        # ws_id -> asyncio.Lock

        # 每个collector用独立页面，避免并发干扰
        COLLECT_CONCURRENCY = 10
        collect_pages = []
        for _ in range(COLLECT_CONCURRENCY):
            try:
                collect_pages.append(await self.context.new_page())
            except:
                pass

        _log(f"使用 {len(collect_pages)} 个页面并行采集", "blue")
        if not collect_pages:
            _log("无法创建课程采集页面，本轮不标记为已完成", "red")
            return [], {}

        # 每个采集页都先导航到专题班列表
        async def init_collect_page(cp):
            try:
                await cp.goto("https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                              wait_until="networkidle", timeout=20000)
                await cp.wait_for_timeout(3000)
                if self.tags_to_learn:
                    for tag in self.tags_to_learn:
                        try:
                            all_tags = cp.locator("ul.tag-tree-list span.single-tag")
                            cnt = await all_tags.count()
                            for i in range(cnt):
                                text = (await all_tags.nth(i).inner_text()).strip()
                                if text == tag:
                                    await all_tags.nth(i).click()
                                    await cp.wait_for_timeout(3000)
                                    break
                        except:
                            pass
            except:
                pass

        await asyncio.gather(*(init_collect_page(cp) for cp in collect_pages))

        # 页面创建可能部分失败；并发上限必须跟实际页面数一致，
        # 否则多个 collector 会同时操作同一个 SPA 页面。
        sem = asyncio.Semaphore(len(collect_pages))

        async def collect_one(idx: int, ws: dict, cp: Page):
            """单个专题班：每个专题班用新页面，避免SPA状态累积"""
            ws_title = ws['title'][:50]
            async with sem:
                _log(f"[采集 {idx+1}/{len(to_process)}] {ws_title}", "blue")

                # 从 detail_link 提取 workshop ID
                detail_link = ws.get('detail_link', '')
                ws_id = ""
                m = re.search(r'id=([a-f0-9\-]+)', detail_link)
                if m:
                    ws_id = m.group(1)
                if not ws_id:
                    _log(f"  ✗ 无法从链接提取ID: {detail_link[:60]}", "red")
                    return None

                # 检查是否已完成
                if ws_id in completed_ids:
                    _log(f"  ⊘ 已完成，跳过: {ws_title[:30]}", "green")
                    return None

                # 直接用传入的页面（已认证），导航到专题班详情页
                ws_url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
                try:
                    await cp.goto(ws_url, wait_until="domcontentloaded", timeout=20000)
                    await cp.wait_for_timeout(6000)
                except:
                    pass

                body_text = ""
                try:
                    body_text = await cp.locator("body").inner_text(timeout=3000)
                except:
                    pass

                # 检查是否报名截止（列表页已过滤，这里兜底）
                if "报名截止" in body_text or "报名已结束" in body_text:
                    _log(f"  ⊘ 报名已截止，跳过: {ws_title[:30]}", "yellow")
                    return None

                # 先检查是否需要报名（必须先报名才能看到课程，否则API会400）
                need_enroll = False
                for kw in ["立即报名", "加入学习", "免费报名"]:
                    try:
                        btn = cp.locator(f"text={kw}").first
                        if await btn.count() > 0 and await btn.is_visible():
                            _log(f"  需要报名，点击「{kw}」", "blue")
                            old_url = cp.url
                            await btn.click()
                            for _ in range(10):
                                await cp.wait_for_timeout(2000)
                                if cp.url != old_url:
                                    break
                                try:
                                    if not await cp.locator(f"text={kw}").first.is_visible(timeout=1000):
                                        break
                                except:
                                    break
                            await cp.wait_for_load_state("networkidle", timeout=15000)
                            await cp.wait_for_timeout(3000)
                            need_enroll = True
                            break
                    except:
                        pass

                # 报名后重新导航到详情页，等服务器处理
                if need_enroll:
                    try:
                        await cp.goto(ws_url, wait_until="domcontentloaded", timeout=15000)
                        await cp.wait_for_timeout(5000)
                    except:
                        pass
                else:
                    # 未报名的也点课程标签（已报名的跳过，直接API采集）
                    for tab_text in ["课程", "课程列表", "课程目录"]:
                        try:
                            tab = cp.locator(f"text={tab_text}").first
                            if await tab.count() > 0 and await tab.is_visible():
                                await tab.click()
                                await cp.wait_for_timeout(3000)
                                break
                        except:
                            pass

                    # 等待课程表格加载
                    for _wait in range(3):
                        row_count = await cp.locator("tr.text-center").count()
                        if row_count > 0:
                            break
                        page_text = ""
                        try:
                            page_text = await cp.locator("body").inner_text(timeout=2000)
                        except:
                            pass
                        if "NaN" in page_text or "总课程门" in page_text:
                            debug(f"  表格数据未加载，等待刷新({_wait+1}/3)")
                            await cp.wait_for_timeout(5000)
                        else:
                            break

                # 获取课程列表：API重试（最多5次，递增等待，400不重试）
                courses = []
                API_MAX_RETRIES = 5
                api_gave_up = False
                for api_attempt in range(API_MAX_RETRIES):
                    if api_attempt > 0:
                        wait_sec = api_attempt * 3
                        _log(f"  API重试({api_attempt}/{API_MAX_RETRIES})，等{wait_sec}秒...", "yellow")
                        await asyncio.sleep(wait_sec)
                    try:
                        api_result = await self._get_courses_by_api(cp, ws_id, log_callback=_log)
                        # api_result为None表示400等不可恢复错误，不重试
                        if api_result is None:
                            api_gave_up = True
                            break
                        if api_result and isinstance(api_result, dict):
                            data = api_result.get("contentList", [])
                            if not data:
                                for key in ["courses", "knowledgeList", "courseList", "lessons"]:
                                    val = api_result.get(key)
                                    if isinstance(val, list) and len(val) > 0:
                                        data = val
                                        break
                            if data and isinstance(data, list):
                                for item in data:
                                    if not isinstance(item, dict):
                                        continue
                                    title = str(item.get("knowledgeName", item.get("title",
                                        item.get("courseName", item.get("name", "")))))
                                    ctype = str(item.get("kngType", item.get("type", "")))
                                    if ctype in ("考试", "scorm", "ExamKnowledge", "ScormKnowledge"):
                                        continue
                                    if not title or len(title) <= 3:
                                        continue
                                    progress_val = item.get("progress", 0)
                                    hours_val = item.get("hours", 0)
                                    detail_url = item.get("kngDetailUrl", "")
                                    course_id = item.get("knowledgeId", item.get("id", ""))
                                    courses.append({
                                        "title": title.strip(),
                                        "type": ctype.strip(),
                                        "required": "必修" if item.get("type") == 1 else "选修",
                                        "hours": str(hours_val),
                                        "progress": f"{float(progress_val)*100:.0f}%" if progress_val else "0%",
                                        "action": "已学习" if progress_val and float(progress_val) >= 1 else "未学习",
                                        "url": detail_url or course_id,
                                    })
                                if courses:
                                    _log(f"  ✓ API获取 {len(courses)} 门课程", "green")
                                    break
                                else:
                                    _log(f"  API返回0门课程", "yellow")
                            else:
                                _log(f"  API无课程数据", "yellow")
                        else:
                            _log(f"  API返回异常", "yellow")
                    except Exception as e:
                        _log(f"  API异常: {e}", "red")

                if courses is None:
                    courses = []

                if courses:
                    to_learn = [(i, c) for i, c in enumerate(courses)
                                if self._is_learnable(c.get('action', ''), c.get('hours', ''),
                                                          c.get('progress', ''))]
                    action_vals = set(c.get('action', '') for c in courses)
                    debug(f"  课程action值: {action_vals}, 待学: {len(to_learn)}")
                    if not to_learn:
                        if ws_id not in completed_ids:
                            completed_ids.add(ws_id)
                            self.mark_workshop_completed(ws_id)
                            debug(f"  标记已完成: {ws_id}")
                        _log(f"  ✓ 全部已完成（共{len(courses)}门）", "green")
                    else:
                        _log(f"  ✓ {len(to_learn)} 门待学（共{len(courses)}门）", "green")
                    return {
                        "ws_id": ws_id,
                        "ws_title": ws_title,
                        "tasks": [(ws_id, ci, c, ws_title) for ci, c in to_learn],
                        "courses": courses
                    }
                else:
                    _log(f"  ✗ 未获取到课程", "yellow")
                    return None

        # 并行执行所有采集任务
        console.print(f"\n开始并行采集 {len(to_process)} 个专题班...", style="bold blue")
        results = await asyncio.gather(
            *(collect_one(i, ws, collect_pages[i % len(collect_pages)])
              for i, ws in enumerate(to_process)),
            return_exceptions=True
        )

        # 汇总结果
        for r in results:
            if isinstance(r, Exception):
                debug(f"采集异常: {r}")
                continue
            if r is None:
                continue
            ws_id = r["ws_id"]
            for t in r["tasks"]:
                all_tasks.append(t)
            if ws_id not in ws_locks:
                ws_locks[ws_id] = asyncio.Lock()

        # 关闭所有采集页面
        for cp in collect_pages:
            try:
                await cp.close()
            except:
                pass

        # 确保主页面回到列表页
        try:
            await page.goto("https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                            wait_until="networkidle", timeout=15000)
            await page.wait_for_timeout(3000)
        except:
            pass

        return all_tasks, ws_locks

    async def parallel_learn_courses(self, all_tasks: List, ws_locks: Dict, fetch_more_callback=None,
                                      progress_callback=None, hours_callback=None, log_callback=None,
                                      report_item_progress: bool = False,
                                      total_ref: Optional[List[int]] = None,
                                      producer=None):
        """全局课程队列：所有 worker 跨专题班并发消费，自动标记已完成专题班
        fetch_more_callback: async callable(queue) -> int，队列空时调用，往queue里加新任务，返回新增数
        progress_callback: callable(data_dict) - Textual进度更新回调
        hours_callback: callable(data_dict) - Textual学时更新回调
        log_callback: callable(msg, style) - Textual日志回调
        report_item_progress: 手动模式用——把「已处理课程数 / 总课程数」当总体进度上报
        total_ref: 外部传入的单元素列表 [总数]；边学边采集时总数还会增长，
                   用外部列表才能让进度分母跟着涨
        producer: async callable(queue) —— 后台采集协程，与学习**并行**跑，
                  持续把新任务投进队列。fetch_more_callback 只在 worker 空闲时
                  才被调用，学得慢就会出现"学了 2 门就再也不报名了"""
        if not all_tasks:
            console.print("没有需要学习的课程", style="green")
            return set()

        # 开局就把配置的线程全部拉起来待命，而不是按"初始任务数"截断：
        # 课程池是边学边补的，截断会让后补进来的课没人学（线程数不会再涨）。
        # 池子涨到几门，就立刻有几门被空闲线程接走。
        num_workers = max(1, min(int(self.workers or 1), len(self.pages) or 1))
        console.print(f"\n[bold]启动 {num_workers} 个工作线程，"
                      f"当前 {len(all_tasks)} 门课程（后续边学边补）[/bold]")

        # 共享的线程状态（用于Live表格显示）
        worker_status = {}
        status_lock = asyncio.Lock()
        study_hours_info = {"central": 0, "online": 0, "updated": "未查询"}
        # 心跳：记录每个worker最后活动时间
        worker_heartbeat = {}  # {w_id: timestamp}
        HEARTBEAT_TIMEOUT = 600  # 10分钟无进展判定卡死
        # 当前任务信息（用于超时重试）
        worker_current_task = {}  # {w_id: (ws_id, cidx, course, ws_title, retry)}
        worker_cancel_event = {}  # {w_id: asyncio.Event}
        # 进度追踪：记录每个worker的(时间戳, 百分比)用于预估剩余时间
        worker_progress_history = {}  # {w_id: [(timestamp, pct), ...]}

        def update_status(w_id, **kwargs):
            """更新线程状态（线程安全）+ 心跳 + 进度追踪"""
            current = worker_status.setdefault(w_id, {})
            if "course" in kwargs and current.get("course") != kwargs.get("course"):
                # 一个 worker 切换课程时，不能把上一门课的速率带到新课程。
                worker_progress_history[w_id] = []
            current.update(kwargs)
            worker_heartbeat[w_id] = time.time()
            # 记录进度变化
            pct_str = kwargs.get("progress", "")
            if pct_str and pct_str != "-":
                try:
                    pct_val = float(pct_str.replace("%", ""))
                    history = worker_progress_history.setdefault(w_id, [])
                    history.append((time.time(), pct_val))
                    # 只保留最近10条记录
                    if len(history) > 10:
                        worker_progress_history[w_id] = history[-10:]
                except:
                    pass
            # Textual/GUI 回调放在记录进度之后，确保本次更新就能参与 ETA 计算。
            if progress_callback:
                try:
                    info = worker_status.get(w_id, {})
                    try:
                        eta = estimate_remaining(w_id)
                    except:
                        eta = "-"
                    progress_callback({
                        "wid": w_id,
                        "course": info.get("course", "-"),
                        "progress": info.get("progress", "-"),
                        "eta": eta,
                        "status": info.get("status", "-"),
                    })
                except:
                    pass

        def estimate_remaining(w_id):
            """根据进度变化率预估剩余时间（平滑计算）"""
            history = worker_progress_history.get(w_id, [])
            status = worker_status.get(w_id, {}).get("status", "")

            # 没有历史记录
            if not history:
                return "..." if status == "学习中" else "-"

            # 已完成
            if history[-1][1] >= 100:
                return "✓"

            # 只有1条记录，用任务开始时间估算
            if len(history) < 2:
                start_time = worker_heartbeat.get(w_id, 0)
                if start_time > 0 and history[-1][1] > 0:
                    elapsed = time.time() - start_time
                    rate = history[-1][1] / elapsed
                    if rate > 0:
                        remaining = (100 - history[-1][1]) / rate
                        return _format_time(remaining)
                return "..." if status == "学习中" else "-"

            # 多条记录：用线性回归计算平均速率
            t0, p0 = history[0]
            t_last, p_last = history[-1]
            dt = t_last - t0
            dp = p_last - p0

            if dt <= 0:
                return "..." if status == "学习中" else "-"

            # 进度没变化但还在学习中
            if dp <= 0:
                if status == "学习中":
                    return "计算中..."
                return "-"

            rate = dp / dt
            if rate <= 0:
                return "-"

            remaining = (100 - p_last) / rate
            return _format_time(remaining)

        def _format_time(seconds):
            """格式化为中文倒计时"""
            if seconds < 0:
                return "计算中"
            if seconds < 60:
                return f"剩{seconds:.0f}秒"
            elif seconds < 3600:
                m = int(seconds // 60)
                s = int(seconds % 60)
                return f"剩{m}分{s}秒" if s else f"剩{m}分"
            else:
                h = int(seconds // 3600)
                m = int((seconds % 3600) // 60)
                return f"剩{h}时{m}分" if m else f"剩{h}时"

        def make_progress_table():
            # 学时信息面板
            hours_table = Table(title="培训学时", show_header=False, box=None, padding=(0, 2))
            hours_table.add_column("项目", style="cyan")
            hours_table.add_column("数值", style="green")
            h = study_hours_info
            hours_table.add_row("集中培训", f"{h['central']:.1f} 学时")
            hours_table.add_row("网络自学", f"{h['online']:.1f} 学时")
            if self.study_goal > 0:
                goal_type_name = "集中培训" if self.goal_type == "central" else "网络自学"
                cur = h.get(self.goal_type, 0)
                pct = min(100, cur / self.study_goal * 100) if self.study_goal > 0 else 0
                bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
                hours_table.add_row("目标", f"{goal_type_name} {self.study_goal:.0f} 学时")
                hours_table.add_row("进度", f"[{'bold green' if pct >= 100 else 'yellow'}]{bar} {pct:.1f}%[/]")
            hours_table.add_row("更新时间", h['updated'])

            # 线程进度表
            table = Table(title=f"学习进度（完成 {completed_count[0]}/{total_ref[0]}，失败 {failed[0]}）")
            table.add_column("线程", style="cyan", width=4)
            table.add_column("课程", style="white", width=36)
            table.add_column("进度", style="green", width=6)
            table.add_column("预计", style="magenta", width=6)
            table.add_column("状态", style="yellow", width=10)
            for wid in range(num_workers):
                info = worker_status.get(wid, {})
                eta = estimate_remaining(wid) if info.get("status") == "学习中" else "-"
                table.add_row(
                    str(wid + 1),
                    info.get("course", "-"),
                    info.get("progress", "-"),
                    eta,
                    info.get("status", "等待中")
                )
            # 合并两个表
            from rich.console import Group
            return Group(hours_table, table)

        # 进度统计
        if total_ref is None:
            total_ref = [len(all_tasks)]
        completed_count = [0]
        failed = [0]
        lock_stat = asyncio.Lock()

        def report_items(status=""):
            """手动模式：把已处理课程数当总体进度上报（成功与失败都算处理过）。"""
            if not report_item_progress or not progress_callback:
                return
            try:
                progress_callback(_manual_progress_payload(
                    completed_count[0] + failed[0], total_ref[0], status))
            except Exception:
                pass

        report_items("准备中")

        # 按专题班统计完成情况：{ws_id: {"total": N, "done": N, "title": str}}
        ws_progress = {}
        for ws_id, cidx, course, ws_title in all_tasks:
            if ws_id not in ws_progress:
                ws_progress[ws_id] = {"total": 0, "done": 0, "title": ws_title}
            ws_progress[ws_id]["total"] += 1
        completed_ws_ids = set()

        # 动态补课也必须即时纳入总数和专题班统计。
        registered_task_keys = set()

        def task_key(ws_id, cidx, course):
            return (ws_id, cidx, course.get('url', '') or course.get('title', '').strip()[:80])

        for ws_id, cidx, course, _ws_title in all_tasks:
            registered_task_keys.add(task_key(ws_id, cidx, course))

        def register_queued_task(item):
            """队列入队钩子：初始任务已登记，动态任务在入队时登记。"""
            if not isinstance(item, (tuple, list)) or len(item) < 4:
                return
            ws_id, cidx, course, ws_title = item[:4]
            if not isinstance(course, dict):
                return
            key = task_key(ws_id, cidx, course)
            if key in registered_task_keys:
                return
            registered_task_keys.add(key)
            total_ref[0] += 1
            wp = ws_progress.setdefault(ws_id, {"total": 0, "done": 0, "title": ws_title})
            wp["total"] += 1

        class _TrackedQueue(asyncio.Queue):
            def put_nowait(self, item):
                register_queued_task(item)
                return super().put_nowait(item)

        # 构建任务队列（每个任务带重试计数）
        MAX_RETRY = 3
        course_queue = _TrackedQueue()
        seen_courses = set()  # 去重：已见过的课程URL
        dedup_count = 0
        for t in all_tasks:
            ws_id, cidx, course, ws_title = t
            # 用URL去重，没有URL则用标题
            dedup_key = course.get('url', '') or course['title'].strip()[:50]
            if dedup_key and dedup_key in seen_courses:
                dedup_count += 1
                # 重复课程直接标记完成（不入队）
                if ws_id not in ws_progress:
                    ws_progress[ws_id] = {"total": 0, "done": 0, "title": ws_title}
                ws_progress[ws_id]["total"] += 1
                ws_progress[ws_id]["done"] += 1
                continue
            if dedup_key:
                seen_courses.add(dedup_key)
            course_queue.put_nowait((*t, 0))  # (ws_id, cidx, course, ws_title, retry)
        if dedup_count > 0:
            console.print(f"  去重: 跳过 {dedup_count} 门重复课程", style="yellow")

        def retry_task(ws_id, cidx, course, ws_title, retry, wid):
            """失败任务放回队列重试（wid 显式传入，避免闭包引用到错误 worker）"""
            if retry < MAX_RETRY:
                course_queue.put_nowait((ws_id, cidx, course, ws_title, retry + 1))
                update_status(wid, status=f"重试({retry+1}/{MAX_RETRY})")
                return True
            return False

        async def worker(w_id: int, page: Page):
            """单个工作线程：从队列取任务，独立完成学习"""
            cancel_event = asyncio.Event()
            worker_cancel_event[w_id] = cancel_event

            while True:
                # 用户变更配置：停止取新任务
                if self._stop_event.is_set():
                    break
                try:
                    ws_id, cidx, course, ws_title, retry = await asyncio.wait_for(
                        course_queue.get(), timeout=30)
                except (asyncio.TimeoutError, asyncio.QueueEmpty):
                    # 队列获取超时——但不代表队列真的空了（可能是ws_lock排队）
                    if course_queue.qsize() > 0:
                        continue  # 队列还有任务，继续取
                    # 后台采集还在跑：等它投喂，不要就此退出
                    if producer_task is not None and not producer_task.done():
                        continue
                    # 队列确实空了，尝试采集更多课程
                    if fetch_more_callback:
                        try:
                            added = await fetch_more_callback(course_queue)
                            if added > 0:
                                continue  # 有新课程，继续取
                        except Exception as e:
                            debug(f"[工作线程 {w_id+1}] 采集回调异常: {e}")
                    break

                title = course['title'][:40]
                ws_url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
                # 记录当前任务（供心跳超时重试）
                worker_current_task[w_id] = (ws_id, cidx, course, ws_title, retry)
                cancel_event.clear()

                try:
                    update_status(w_id, course=title, workshop=ws_title[:20], progress="-", status="加载中")

                    # 1) 导航到专题班页（reload确保SPA刷新内容）
                    try:
                        await page.goto(ws_url, wait_until="networkidle", timeout=20000)
                        await page.reload(wait_until="networkidle", timeout=20000)
                        await page.wait_for_selector("tr.text-center", timeout=15000)
                        await page.wait_for_timeout(2000)
                    except Exception as e:
                        debug(f"[工作线程 {w_id+1}] 页面加载异常: {traceback.format_exc()}")
                        if retry_task(ws_id, cidx, course, ws_title, retry, w_id):
                            continue
                        update_status(w_id, status="加载失败")
                        async with lock_stat:
                            failed[0] += 1
                            report_items()
                        continue

                    # 2) 加锁：同一专题班的课程串行点击
                    course_page = None
                    async with ws_locks.get(ws_id, asyncio.Lock()):
                        rows = page.locator("tr.text-center")
                        row_count = await rows.count()

                        # 按标题查找课程行（不依赖索引，避免索引超限）
                        row = None
                        course_title = course.get('title', '').strip()[:30]
                        if course_title:
                            for i in range(row_count):
                                try:
                                    r = rows.nth(i)
                                    text = await r.inner_text(timeout=2000)
                                    if course_title in text:
                                        row = r
                                        break
                                except:
                                    pass
                        # 兜底：用索引
                        if row is None and cidx < row_count:
                            row = rows.nth(cidx)
                        if row is None:
                            update_status(w_id, status="未找到课程")
                            async with lock_stat:
                                failed[0] += 1
                                report_items()
                            continue

                        btn = row.locator("span.edit-block").first
                        if await btn.count() == 0:
                            update_status(w_id, status="无按钮")
                            async with lock_stat:
                                failed[0] += 1
                                report_items()
                            continue

                        try:
                            async with page.expect_event("popup", timeout=20000) as pi:
                                await btn.click()
                            course_page = await pi.value
                            await course_page.wait_for_load_state()
                        except Exception as e:
                            debug(f"[工作线程 {w_id+1}] popup异常: {traceback.format_exc()}")
                            if course_page:
                                try: await course_page.close()
                                except: pass
                            if retry_task(ws_id, cidx, course, ws_title, retry, w_id):
                                continue
                            update_status(w_id, status="打开失败")
                            async with lock_stat:
                                failed[0] += 1
                                report_items()
                            continue

                    # 3) 找学习按钮
                    update_status(w_id, status="查找按钮")
                    found_btn = False
                    for kw in ["我要学习", "开始学习", "进入课程", "继续学习", "学习课程", "进入课程学习"]:
                        try:
                            await course_page.wait_for_selector(f"text={kw}", timeout=8000)
                            sb = course_page.locator(f"text={kw}").first
                            if await sb.count() > 0:
                                await sb.click()
                                await course_page.wait_for_timeout(5000)
                                found_btn = True
                                break
                        except:
                            pass

                    # 4) 播放视频（传入进度回调和课程类型；检查返回值，失败走重试）
                    update_status(w_id, status="学习中", progress="0%")
                    def on_progress(pct):
                        update_status(w_id, progress=f"{pct:.0f}%", status="学习中")
                    play_ok = await self.find_and_play_video(
                        course_page, w_id, on_progress,
                        course_type=course.get('type', ''), cancel_event=cancel_event)
                    if not play_ok:
                        debug(f"[工作线程 {w_id+1}] 视频未完成: {title}")
                        try:
                            await course_page.close()
                        except:
                            pass
                        if cancel_event.is_set():
                            continue
                        if retry_task(ws_id, cidx, course, ws_title, retry, w_id):
                            continue
                        update_status(w_id, status="播放失败")
                        async with lock_stat:
                            failed[0] += 1
                            report_items()
                        continue

                    # 用户变更配置：放弃当前任务，不再计数
                    if self._stop_event.is_set():
                        try:
                            await course_page.close()
                        except:
                            pass
                        break

                    # 心跳超时判定：放弃当前任务（心跳已把任务重试入队，避免重复学习）
                    if cancel_event.is_set():
                        debug(f"[工作线程 {w_id+1}] 心跳取消当前任务: {title}")
                        try:
                            await course_page.close()
                        except:
                            pass
                        continue

                    # 5) 关闭课程标签页
                    try:
                        await course_page.close()
                    except:
                        pass

                    # 6) 更新进度 + 检查专题班是否全部完成
                    async with lock_stat:
                        completed_count[0] += 1
                        report_items()
                        wp = ws_progress.get(ws_id)
                        if wp:
                            wp["done"] += 1
                            if wp["done"] >= wp["total"] and ws_id not in completed_ws_ids:
                                completed_ws_ids.add(ws_id)
                                self.mark_workshop_completed(ws_id)
                        update_status(w_id, status="✓ 完成", progress="100%")
                        if log_callback:
                            log_callback(f"[线程{w_id+1}] 完成: {title}", "green")

                    # 7) 完成一门课后检查目标（O1 节流：仅在需要查目标时查询，
                    #    且 60s TTL 缓存合并并发，不再每门课都打学习中心）
                    if self.study_goal > 0:
                        try:
                            _h = await self._get_study_hours(page)
                            study_hours_info.update({
                                "central": _h.get("central", 0),
                                "online": _h.get("online", 0),
                                "updated": datetime.now().strftime("%H:%M:%S")
                            })
                            _cur = _h.get(self.goal_type, 0)
                            if _cur >= self.study_goal:
                                update_status(w_id, status="目标达成!")
                                raise GoalReached()
                        except GoalReached:
                            raise
                        except Exception:
                            pass

                except asyncio.CancelledError:
                    # 被取消（GUI停止/重启）：关闭弹窗页再退出，避免泄漏
                    if course_page and not course_page.is_closed():
                        try:
                            await course_page.close()
                        except:
                            pass
                    raise
                except Exception as e:
                    debug(f"[工作线程 {w_id+1}] 未捕获异常:\n{traceback.format_exc()}")
                    try:
                        if course_page and not course_page.is_closed():
                            await course_page.close()
                    except:
                        pass
                    if retry_task(ws_id, cidx, course, ws_title, retry, w_id):
                        continue
                    update_status(w_id, status="异常")
                    async with lock_stat:
                        failed[0] += 1
                        report_items()

            update_status(w_id, status="已退出", course="-", workshop="-")

        # 后台采集协程：与学习并行把后续专题班报名+采集进来。
        # 只靠 fetch_more_callback 的话，worker 忙着学就不会去采集，
        # 表现就是"开始学了 2 门课程就没有继续报名了"。
        producer_task = None
        if producer is not None:
            async def _run_producer():
                try:
                    await producer(course_queue)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    debug(f"后台采集协程异常: {type(exc).__name__}: {_safe_debug_error(exc)}")
                    if log_callback:
                        log_callback(f"后台采集中断: {type(exc).__name__}", "yellow")
            producer_task = asyncio.create_task(_run_producer())

        # 用 Live 表格实时刷新
        from rich.live import Live
        # 创建独立页面用于定时查询学时（不与worker冲突）
        try:
            hours_page = await self.context.new_page()
        except:
            hours_page = None

        # 启动前先查询一次学时（force：预热缓存）
        if hours_page:
            try:
                _h = await self._get_study_hours(hours_page, force=True)
                _info = {
                    "central": _h.get("central", 0),
                    "online": _h.get("online", 0),
                    "updated": datetime.now().strftime("%H:%M:%S"),
                }
                study_hours_info.update(_info)
                if hours_callback:
                    hours_callback(_info)
                if log_callback:
                    log_callback(f"学时更新: 集中{_info['central']:.1f} 网络{_info['online']:.1f}", "blue")
            except:
                pass

        # 启动所有 worker
        tasks = []
        for w_id in range(num_workers):
            tasks.append(asyncio.create_task(worker(w_id, self.pages[w_id])))
            await asyncio.sleep(2)

        # 定时采集学时间隔（秒）
        HOURS_CHECK_INTERVAL = 60

        # 心跳检测 + 刷新
        async def refresh_display():
            import time
            last_hours_check = time.time()
            while not all(t.done() for t in tasks):
                # 用户变更配置：立即停止（StopLearning 会触发上层取消所有 worker）
                if self._stop_event.is_set():
                    raise StopLearning()
                now = time.time()

                # 定时采集总体学习进度（两种模式统一处理；force 刷新共享缓存）
                if now - last_hours_check >= HOURS_CHECK_INTERVAL and hours_page:
                    last_hours_check = now
                    try:
                        _h = await asyncio.wait_for(
                            self._get_study_hours(hours_page, force=True), timeout=30)
                        _info = {
                            "central": _h.get("central", 0),
                            "online": _h.get("online", 0),
                            "updated": datetime.now().strftime("%H:%M:%S"),
                        }
                        study_hours_info.update(_info)
                        if hours_callback:
                            hours_callback(_info)
                        if log_callback:
                            log_callback(f"学时更新: 集中{_info['central']:.1f} 网络{_info['online']:.1f}", "blue")
                        # 定时检查目标学时（避免因平台统计延迟导致多学）
                        if self.study_goal > 0:
                            _cur = _h.get(self.goal_type, 0)
                            if _cur >= self.study_goal:
                                raise GoalReached()
                    except GoalReached:
                        raise
                    except Exception as _hex:
                        debug(f"学时刷新失败: {_hex}")
                        if log_callback:
                            log_callback(f"学时刷新失败: {_hex}", "yellow")

                # Rich模式：更新Live表格
                if live_ctx:
                    live_ctx.update(make_progress_table())

                await asyncio.sleep(2)
                # 心跳检测
                now = time.time()
                for wid in range(num_workers):
                    last = worker_heartbeat.get(wid, 0)
                    if last > 0 and now - last > HEARTBEAT_TIMEOUT:
                        info = worker_status.get(wid, {})
                        status = info.get("status", "")
                        if status in ("学习中", "查找按钮", "加载中"):
                            debug(f"[心跳] 工作线程 {wid+1} 超时({now-last:.0f}s)，触发重试")
                            task_info = worker_current_task.get(wid)
                            if task_info:
                                ws_id, cidx, course, ws_title, retry = task_info
                                if retry < MAX_RETRY:
                                    course_queue.put_nowait((ws_id, cidx, course, ws_title, retry + 1))
                                    # 通知 worker 放弃当前任务（避免重复学习）。
                                    # 不再关闭/替换页面：那会关闭 worker 正在使用的页面，
                                    # 而新页面 worker 永远拿不到，等于杀死 worker。
                                    ce = worker_cancel_event.get(wid)
                                    if ce:
                                        ce.set()
                                    update_status(wid, status=f"超时重试")
                                else:
                                    update_status(wid, status="超时放弃")
                                    async with lock_stat:
                                        failed[0] += 1
                                        report_items()
                            worker_heartbeat[wid] = now
            if live_ctx:
                live_ctx.update(make_progress_table())

        # 根据模式选择显示方式
        if progress_callback:
            # Textual模式：不使用Rich Live
            live_ctx = None
            refresh_task = asyncio.create_task(refresh_display())
            _done, _pending = await asyncio.wait(
                [refresh_task, *tasks], return_when=asyncio.FIRST_EXCEPTION)
            for p in _pending:
                p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
            for d in _done:
                if d is not refresh_task:
                    try:
                        d.result()
                    except (GoalReached, StopLearning):
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                    except:
                        pass
        else:
            # CLI模式：使用Rich Live
            with Live(make_progress_table(), console=console, refresh_per_second=1) as live:
                live_ctx = live
                refresh_task = asyncio.create_task(refresh_display())
                _done, _pending = await asyncio.wait(
                    [refresh_task, *tasks], return_when=asyncio.FIRST_EXCEPTION)
                for p in _pending:
                    p.cancel()
                    try:
                        await p
                    except (asyncio.CancelledError, Exception):
                        pass
                for d in _done:
                    if d is not refresh_task:
                        try:
                            d.result()
                        except (GoalReached, StopLearning):
                            for t in tasks:
                                if not t.done():
                                    t.cancel()
                        except:
                            pass

        # 后台采集协程：学习都结束了就收掉（正常应已自然结束）
        if producer_task is not None:
            if not producer_task.done():
                producer_task.cancel()
            try:
                await producer_task
            except (asyncio.CancelledError, Exception):
                pass

        # 记录完成统计（供 GUI 显示真实成功/失败数）
        self.last_stats = (completed_count[0], failed[0])
        # 关闭定时查询学时的专用页面，避免每次调用泄漏一个页面
        if hours_page:
            try:
                await hours_page.close()
            except:
                pass

        if self._stop_event.is_set():
            # 用户变更配置主动停止，不算"完成"
            if log_callback:
                log_callback(f"学习已停止: 已成功 {completed_count[0]} 门", "yellow")
            else:
                console.print(f"\n[bold yellow]学习已停止: 已成功 {completed_count[0]} 门[/bold yellow]")
        elif log_callback:
            log_callback(f"学习任务完成: 成功 {completed_count[0]} 门, 失败 {failed[0]} 门", "bold green")
            log_callback(f"已完成 {len(completed_ws_ids)}/{len(ws_progress)} 个专题班", "green")
        else:
            console.print(f"\n[bold green]学习任务完成: 成功 {completed_count[0]} 门, 失败 {failed[0]} 门[/bold green]")
            console.print(f"已完成 {len(completed_ws_ids)}/{len(ws_progress)} 个专题班", style="green")
        return completed_ws_ids

    async def _collect_trainingcamp_courses(self, page: Page, camp_id: str, log_callback=None) -> List[Dict]:
        """从训练营详情页读取模块中的课程页，并生成可直接学习的路由。

        目录项分两种（与 traincamp-journey 组件的点击逻辑一致）：
        - 叶子页：带 componentList，点它进入课程学习页；
        - 分组标题：没有 componentList，自身没有组件，真正要学的是它里面嵌套的子页
          （子项点击走 /traincamp/study/{campId}/{子项id}?courseIndex=分组下标）。
        只采集第一层会把分组标题当成课程页，打开后一个组件都没有 → 直接判「未完成」。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        camp_url = f"https://u.ccb.com/trainingcamp/#/traincampdetail/{camp_id}/away"
        try:
            await page.goto(camp_url, wait_until="domcontentloaded", timeout=20000)
            # 用 attached 而不是默认的 visible：目录为空时容器高度为 0，visible 会一直等
            await page.wait_for_selector(".traincamp-journey", state="attached", timeout=20000)
            await page.wait_for_function("""() => {
                const root = document.querySelector('.traincamp-journey');
                const vm = root && root.__vue__;
                return !!(vm && Array.isArray(vm.dataList) && vm.dataList.length);
            }""", timeout=20000)
            collected = await page.evaluate("""() => {
                const root = document.querySelector('.traincamp-journey');
                const vm = root && root.__vue__;
                if (!vm || !Array.isArray(vm.dataList)) return [];
                const result = [];
                const modules = [];
                const usable = (c) => !!(c && c.id && c.pageName) &&
                    !(c.hideFlag === 1 || c.hideFlag === '1') &&
                    !(c.finishFlag === 1 || c.finishFlag === '1' || c.finishFlag === true);
                const push = (c, moduleIndex, courseIndex, kind) => {
                    result.push({
                        id: String(c.id),
                        title: String(c.pageName).trim(),
                        moduleIndex,
                        courseIndex,
                        kind
                    });
                };
                vm.dataList.forEach((module, moduleIndex) => {
                    const summary = [];
                    const skipTag = (c) => (c.finishFlag === 1 || c.finishFlag === '1' || c.finishFlag === true)
                        ? '[已完成]'
                        : ((c.hideFlag === 1 || c.hideFlag === '1') ? '[已隐藏]' : '');
                    (module.beanList || []).forEach((item, courseIndex) => {
                        if (!item) return;
                        const children = (item.beanList || []).filter(Boolean);
                        // componentList 非空 = 叶子页：点标题就进课程页（课程包也是这种，
                        // 里面的小节由播放器顺序播放，不能当成独立页面去点）
                        const isLeaf = !!(item.componentList && item.componentList.length);
                        const tag = skipTag(item);
                        if (isLeaf) {
                            if (!tag && usable(item)) push(item, moduleIndex, courseIndex, 'leaf');
                            summary.push((item.pageName || '?') + (tag || '[标题页]'));
                            return;
                        }
                        if (children.length) {
                            // 没有 componentList 但挂了子项 = 分组标题：真正要学的是里面的子页
                            let added = 0;
                            children.forEach((child) => {
                                if (usable(child)) { push(child, moduleIndex, courseIndex, 'child'); added++; }
                            });
                            summary.push((item.pageName || '?') + (tag || ('[分组 ' + added + '/' + children.length + ' 个子页]')));
                            return;
                        }
                        if (!tag && usable(item)) push(item, moduleIndex, courseIndex, 'item');
                        summary.push((item.pageName || '?') + (tag || '[单独页]'));
                    });
                    modules.push('模块' + (moduleIndex + 1) + ': ' + summary.join('，'));
                });
                return {items: result, modules: modules};
            }""")
        except Exception as e:
            _log(f"训练营课程列表加载失败 ({camp_id}): {e}", "yellow")
            return []

        if isinstance(collected, dict):
            pages = collected.get("items") or []
            module_lines = collected.get("modules") or []
        else:
            pages = collected or []
            module_lines = []

        for line in module_lines:
            _log(f"训练营 {camp_id} 目录 · {line}", "blue")

        course_tasks = []
        seen_ids = set()
        kinds = {}
        for item in pages or []:
            course_id = str(item.get("id", "")).strip()
            title = str(item.get("title", "")).strip()
            if not course_id or not title or course_id in seen_ids:
                continue
            seen_ids.add(course_id)
            kinds[item.get("kind") or "item"] = kinds.get(item.get("kind") or "item", 0) + 1
            course_url = (
                f"https://u.ccb.com/trainingcamp/#/traincamp/study/{camp_id}/{course_id}"
                f"?moduleIndex={item['moduleIndex']}&courseIndex={item['courseIndex']}"
            )
            course_tasks.append({"url": course_url, "title": title})

        if kinds:
            detail = "、".join(f"{k} {v}" for k, v in kinds.items())
            _log(f"训练营 {camp_id}: 目录项 {detail}", "blue")
        _log(f"训练营 {camp_id}: 找到 {len(course_tasks)} 个课程页面", "green" if course_tasks else "yellow")
        return course_tasks

    @staticmethod
    def _channel_workshop_id_from_url(url: str) -> str:
        """从 /workshop/#/detail?id=xxx 这类地址里取专题班 ID。"""
        try:
            fragment = urlsplit(url or "").fragment
        except Exception:
            return ""
        route, _separator, query = fragment.partition("?")
        if not re.fullmatch(r"/(?:myworkshop/)?detail", route):
            return ""
        return (parse_qs(query).get("id") or [""])[0].strip()

    @staticmethod
    def _channel_workshop_id_from_landing(url: str, exclude_id: str = "") -> str:
        """从点击后的落地地址里尽力取专题班 ID。

        频道页的卡片不一定跳到 /workshop/#/detail：可能是别的路由、路径式
        /detail/<id>，或者只是地址里带了个 UUID。这里按"越明确的越优先"来取。
        """
        strict = AutoLearner._channel_workshop_id_from_url(url)
        if strict:
            return strict
        try:
            parts = urlsplit(url or "")
        except Exception:
            return ""
        fragment = parts.fragment
        route, _sep, fragment_query = fragment.partition("?")
        # 1) 明确的 ID 参数：hash 里的、以及 API 地址 query 里的（SPA 取详情时最常见）
        for query in (fragment_query, parts.query):
            params = parse_qs(query or "")
            for key in ("id", "workshopId", "workshop_id"):
                value = (params.get(key) or [""])[0].strip()
                if value:
                    return value
        # 2) 路径式 /detail/<id>、/myworkshop/detail/<id>
        haystack = f"{parts.path}#{fragment}"
        match = (re.search(r"/(?:my)?workshop/detail/([0-9a-zA-Z_-]{8,})", haystack)
                 or re.search(r"/detail/([0-9a-zA-Z_-]{8,})", haystack))
        if match:
            return match.group(1)
        # 3) 兜底：只有一个 UUID 才敢认。
        #    还在频道落地页上时地址里的 UUID 是频道 ID；已知频道 ID 也要排掉。
        if route.rstrip("/").startswith("/channel/show"):
            return ""
        uuids = set(re.findall(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            fragment))
        if exclude_id:
            uuids.discard(exclude_id)
        return next(iter(uuids)) if len(uuids) == 1 else ""

    @staticmethod
    def _workshop_id_from_requests(urls, exclude_id: str = "") -> str:
        """从点击期间的网络请求里找专题班 ID（SPA 一定会去取详情）。"""
        found = ""
        for url in urls or []:
            try:
                low = str(url).lower()
            except Exception:
                continue
            if "workshop" not in low and "/detail" not in low:
                continue
            if "/channel/show" in low:
                continue
            candidate = AutoLearner._channel_workshop_id_from_landing(
                str(url), exclude_id=exclude_id)
            if candidate:
                found = candidate
        return found

    @staticmethod
    def _is_non_content_label(label: str) -> bool:
        """协议/导航/工具类链接：点了也不是内容入口。"""
        label = (label or "").strip()
        if not label:
            return False
        if CHANNEL_DOC_LABEL.search(label):
            return True
        if label == "查看全部":
            return True
        # 长标题里出现「关于/全部」这类词是正常的，不按工具链接处理
        return len(label) <= 8 and bool(CHANNEL_SHORT_UTILITY_LABEL.search(label))

    @staticmethod
    def _looks_like_channel_card(href: str) -> bool:
        """频道页卡片入口的粗略特征：无 href、javascript:void(0) 或指向详情。"""
        href = (href or "").strip().lower()
        if not href or href.startswith("javascript:"):
            return True
        return "detail" in href

    async def _wait_channel_detail_url(self, page: Page,
                                       timeout_ms: Optional[int] = None) -> str:
        """等同标签页跳转落到 /detail 路由再读地址（点击后立刻读会读到旧地址）。"""
        budget_ms = CHANNEL_DETAIL_WAIT_MS if timeout_ms is None else timeout_ms
        deadline = time.monotonic() + max(0.05, budget_ms / 1000.0)
        delay_ms = 300
        while True:
            url = _page_url(page)
            if self._channel_workshop_id_from_url(url):
                return url
            if time.monotonic() >= deadline:
                return url
            try:
                await page.wait_for_timeout(delay_ms)
            except Exception:
                return url
            delay_ms = min(int(delay_ms * 1.5), 1000)

    async def _wait_popup_url(self, popup, timeout_ms: Optional[int] = None) -> str:
        """等弹窗落到真实地址（about:blank 不算）。

        频道卡片是 window.open 出来的，刚拿到时还是 about:blank，
        立刻读会得到空地址——这正是"点了却拿不到详情"的直接原因。
        """
        budget_ms = CHANNEL_POPUP_NAV_MS if timeout_ms is None else timeout_ms
        deadline = time.monotonic() + max(0.2, budget_ms / 1000.0)
        last = ""
        while True:
            url = _page_url(popup)
            if url and not url.startswith("about:"):
                return url
            if url:
                last = url
            if time.monotonic() >= deadline:
                return last
            try:
                await popup.wait_for_timeout(300)
            except Exception:
                return last

    async def _channel_open_detail(self, page: Page, clicker,
                                   captured: Optional[list] = None) -> str:
        """点击一个频道入口并返回落地地址；弹窗与同标签页跳转都支持。

        captured 传入列表时，顺带记录点击期间发出的请求 URL（主页面与弹窗都听）——
        页面把内容 ID 藏在组件状态里时，往往只有"点一下看它请求了什么"才能拿到。
        """
        listeners = []
        if captured is not None:
            def _on_request(request):
                try:
                    captured.append(getattr(request, "url", ""))
                except Exception:
                    pass

            for target in (page,):
                if hasattr(target, "on"):
                    try:
                        target.on("request", _on_request)
                        listeners.append((target, _on_request))
                    except Exception:
                        pass
        popup = None
        try:
            try:
                async with page.expect_event("popup",
                                             timeout=CHANNEL_POPUP_WAIT_MS) as popup_info:
                    await clicker()
                popup = await popup_info.value
                # 弹窗里的请求也要听：详情/文章内容多半是弹窗自己拉的
                if captured is not None and hasattr(popup, "on"):
                    try:
                        popup.on("request", _on_request)
                        listeners.append((popup, _on_request))
                    except Exception:
                        pass
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                return await self._wait_popup_url(popup)
            except Exception:
                # 同标签页打开：等路由真的变成 /detail 再读，避免拿到跳转前的地址
                return await self._wait_channel_detail_url(page)
        finally:
            if popup is not None:
                try:
                    await popup.close()
                except Exception:
                    pass
            for target, handler in listeners:
                if hasattr(target, "remove_listener"):
                    try:
                        target.remove_listener("request", handler)
                    except Exception:
                        pass

    async def _channel_harvest_workshops(self, page: Page, log_callback=None) -> List[str]:
        """不点击，直接从 DOM 里读专题班入口（href / data-* / onclick）。

        频道页卡片可能把 ID 写在 href 上（/workshop/#/detail?id=xxx&logChannelId=…），
        也可能只是 javascript:void(0) + onclick。能静态读出来的就不要靠"逐张点击
        再看弹窗地址"——后者会被弹窗拦截、标题重复、以及同标签页渲染时序坑到。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        try:
            entries = await page.evaluate(CHANNEL_WORKSHOP_HARVEST_JS)
        except Exception as exc:
            debug(f"频道页链接收割失败: {type(exc).__name__}: {_safe_debug_error(exc)}")
            return []
        workshop_ids = []
        seen = set()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            raw = str(entry.get("raw") or "").lower()
            # 课程/训练营链接交给各自流程，别当成专题班 ID
            if "/course/" in raw or "/trainingcamp/" in raw:
                continue
            workshop_id = str(entry.get("id") or "").strip()
            if not workshop_id or workshop_id in seen:
                continue
            seen.add(workshop_id)
            workshop_ids.append(workshop_id)
            title = str(entry.get("title") or "").strip()
            if title:
                _log(f"  频道课程: {title[:42]}", "green")
        return workshop_ids

    async def _wait_channel_ready(self, page: Page, attempts: int = 6,
                                  delay_ms: int = 1500) -> None:
        """等频道页的 SPA 稳定：链接数量连续两次不变。

        原来的条件只是"页面上有 2 个链接"——页头、登录壳、协议链接就满足了，
        于是内容还没渲染就开始采集，采到的全是登录面板与协议链接。
        """
        last = -1
        for _ in range(max(1, attempts)):
            try:
                count = await page.evaluate("() => document.querySelectorAll('a').length")
            except Exception:
                return
            if count == last:
                return
            last = count
            try:
                await page.wait_for_timeout(delay_ms)
            except Exception:
                return

    async def _channel_login_state(self, page: Page) -> str:
        """频道页当前登录状态：logged / login-form / unknown。

        直接写进日志，回答"是不是没登录"这种问题，不用靠猜。
        """
        try:
            state = await page.evaluate("""() => {
              const hasPwd = !!document.querySelector('#inputPwd, input[type="password"]');
              const userBox = !!document.querySelector('.ccb-user-box');
              const body = (document.body && document.body.innerText) || '';
              const hasSms = body.indexOf('\u83b7\u53d6\u9a8c\u8bc1\u7801') >= 0
                          || body.indexOf('\u77ed\u4fe1\u767b\u5f55') >= 0;
              return {hasPwd: hasPwd, userBox: userBox, hasSms: hasSms};
            }""")
        except Exception:
            return "unknown"
        if not isinstance(state, dict):
            return "unknown"
        if state.get("userBox"):
            return "logged"
        if state.get("hasPwd") or state.get("hasSms"):
            return "login-form"
        return "unknown"

    async def _dismiss_channel_gate(self, page: Page, log_callback=None) -> bool:
        """先过掉频道页的文明公约/须知提示，否则内容根本不会渲染。

        实测：未过公约时页面只有登录面板与协议链接，采集必然为 0；
        此时再去逐个点链接纯属白费（会点到《用户服务协议》这类链接上）。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        for text in CHANNEL_GATE_TEXTS:
            try:
                btn = page.get_by_text(text, exact=True).first
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.click(timeout=5000)
                await page.wait_for_timeout(1500)
                _log(f"频道页已点击「{text}」通过提示", "blue")
                debug(f"频道页点击提示按钮: {text}")
                return True
            except Exception as exc:
                debug(f"频道页提示按钮 {text} 点击失败: {type(exc).__name__}")
        return False

    async def _channel_looks_gated(self, page: Page) -> str:
        """页面是否还停在登录/公约状态；返回命中的特征词（空=看起来正常）。"""
        try:
            text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""
        for hint in CHANNEL_LOGIN_HINTS:
            if hint in text:
                return hint
        return ""

    async def _channel_harvest_from_page_data(self, page: Page, exclude_id: str = "",
                                             log_callback=None) -> List[str]:
        """从页面组件状态里直接取专题班 ID（不点击）。"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        try:
            entries = await page.evaluate(CHANNEL_PAGE_DATA_JS)
        except Exception as exc:
            debug(f"频道页组件状态读取失败: {type(exc).__name__}: {_safe_debug_error(exc)}")
            return []
        ids = []
        seen = set()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            workshop_id = str(entry.get("id") or "").strip()
            if not workshop_id or workshop_id in seen or workshop_id == exclude_id:
                continue
            seen.add(workshop_id)
            ids.append(workshop_id)
            title = str(entry.get("title") or "").strip()
            if title:
                _log(f"  频道课程: {title[:42]}", "green")
        return ids

    async def _channel_content_candidate_count(self, page: Page) -> int:
        """页面上"像内容条目"的链接数，用来判断快速通道是否取全。"""
        try:
            return int(await page.evaluate(CHANNEL_CONTENT_COUNT_JS))
        except Exception:
            return 0

    async def _channel_harvest_from_responses(self, responses, exclude_id: str = "",
                                              min_ids: int = 5,
                                              log_callback=None) -> List[str]:
        """从页面自己请求的内容接口响应里取 ID（一次拿全，也不用点击）。

        关键：不能"碰到第一个含 UUID 的响应就用"——实测那样命中
        /v1/channel/findById（频道自身元数据，只有 3 个 UUID），而真正的内容
        列表在另一个接口里。这里扫完所有候选，取最富的那一条。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        best_ids, best_url = [], ""
        for response in list(responses or [])[:20]:
            try:
                body = await response.text()
            except Exception:
                continue
            ids = _uuids_from_json_text(body, exclude_id)
            if len(ids) > len(best_ids):
                best_ids = ids
                best_url = str(getattr(response, "url", "") or "")
        if len(best_ids) < min_ids:
            debug(f"内容接口最多只取到 {len(best_ids)} 个 ID（<{min_ids}），"
                  f"判定不可靠，交给点击兜底")
            return []
        debug(f"频道页内容接口命中 {len(best_ids)} 个 ID: {_debug_url_shape(best_url)}")
        _log(f"频道页从内容接口取到 {len(best_ids)} 个专题班", "green")
        return best_ids

    async def _dump_channel_anchors(self, page: Page, log_callback=None) -> None:
        """收割不到 ID 时，把频道页链接原样记进日志。

        页面把 ID 藏在组件状态里（Vue @click）时，只能靠点击；但下一次若是别的
        写法，有这份链接清单就能直接改选择器，不用再让用户来回跑一轮。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        try:
            anchors = await page.evaluate(CHANNEL_ANCHOR_DUMP_JS)
        except Exception as exc:
            debug(f"频道页链接清单读取失败: {type(exc).__name__}")
            return
        if not anchors:
            debug("频道页链接清单: 页面上没有 <a> 元素")
            return
        debug(f"频道页链接清单({len(anchors)}条):")
        for item in anchors:
            debug(f"  node[{item.get('kind', 'a')}]: label={item.get('label')!r} "
                  f"href={item.get('href')!r} attrs={item.get('attrs')!r}")
        _log(f"频道页 {len(anchors)} 个链接的属性已写入调试日志", "yellow")

    def _attach_page_requests(self, page: Page, sink: List[str]):
        """挂上请求监听，返回解绑函数（拿不到监听能力时返回 None）。"""
        if not hasattr(page, "on"):
            return None

        def _on_request(request):
            try:
                sink.append(str(getattr(request, "url", "") or ""))
            except Exception:
                pass

        try:
            page.on("request", _on_request)
        except Exception:
            return None

        def _detach():
            if hasattr(page, "remove_listener"):
                try:
                    page.remove_listener("request", _on_request)
                except Exception:
                    pass
        return _detach

    async def _click_collect_popup(self, page: Page, link, captured=None,
                                   detachers=None):
        """点一下，只等"弹窗出现"（不等它导航），返回弹窗对象或 None。

        频道卡片是 window.open 出来的：真正贵的不是点击，而是等弹窗从
        about:blank 跳到详情地址（每张约 2 秒）。这一步不等待，
        所有弹窗的地址最后并行解析。

        captured/detachers：弹窗里的请求也要记（详情接口多半是弹窗自己拉的）。
        """
        try:
            async with page.expect_event("popup",
                                         timeout=CHANNEL_POPUP_EVENT_MS) as popup_info:
                await link.click(timeout=10000)
            popup = await popup_info.value
        except Exception:
            return None
        if captured is not None:
            detach = self._attach_page_requests(popup, captured)
            if detach is not None and detachers is not None:
                detachers.append(detach)
        return popup

    async def _channel_click_workshops(self, page: Page, channel_url: str,
                                       log_callback=None) -> List[str]:
        """兜底：逐个点击频道入口，从落地地址里取专题班 ID。

        点击"一下一张"但不等弹窗跳转（那才是原来 ~50 秒的来源）：先点完，
        再并行解析所有弹窗地址。第一轮没取到 ID 的入口会**串行补采一次**，
        避免连点太快漏掉一部分卡片（实测 24 个只出 15 个）。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        channel_id = _channel_id_from_url(channel_url)
        anchors = page.locator("a")
        try:
            total = await anchors.count()
        except Exception:
            return []

        # 点击前先筛出候选入口，别边点边判断
        candidates = []
        for index in range(min(total, CHANNEL_CLICK_LIMIT)):
            link = anchors.nth(index)
            try:
                href = await link.get_attribute("href")
                label = (await link.inner_text() or "").replace("\n", " ").strip()
            except Exception:
                continue
            if len(label) <= 5 or label == "查看全部":
                continue
            if self._is_non_content_label(label):
                # 协议/隐私/导航这类链接点了也不是内容入口，别浪费时间
                continue
            if not self._looks_like_channel_card(href):
                continue
            candidates.append((index, link, label, href))
        if not candidates:
            return []

        captured: List[str] = []
        detach = self._attach_page_requests(page, captured)
        popup_detachers: List = []
        opened = []      # [(index, label, href, popup)]
        same_tab = []    # [(index, label, href, url)]
        try:
            for index, link, label, href in candidates:
                if self._stop_event.is_set():
                    break
                popup = await self._click_collect_popup(
                    page, link, captured, popup_detachers)
                if popup is not None:
                    opened.append((index, label, href, popup))
                else:
                    # 没有弹窗：可能是同标签页打开，也可能原地不动
                    detail_url = await self._wait_channel_detail_url(page)
                    same_tab.append((index, label, href, detail_url))
                    if _page_url(page) != channel_url:
                        try:
                            await page.goto(channel_url, wait_until="domcontentloaded",
                                            timeout=15000)
                        except Exception:
                            pass
                try:
                    await page.wait_for_timeout(CHANNEL_CLICK_GAP_MS)
                except Exception:
                    pass

            # 并行等所有弹窗落到真地址（串行等就是原来 50 秒的来源）
            resolved = list(same_tab)
            if opened:
                urls = await asyncio.gather(
                    *[self._wait_popup_url(item[3]) for item in opened],
                    return_exceptions=True)
                for item, url in zip(opened, urls):
                    resolved.append((item[0], item[1], item[2],
                                     url if isinstance(url, str) else ""))
        finally:
            if detach is not None:
                detach()
            for popup_detach in popup_detachers:
                popup_detach()
            for _, _, _, popup in opened:
                try:
                    await popup.close()
                except Exception:
                    pass

        workshop_ids: List[str] = []
        seen = set()
        misses = []      # 第一轮没拿到 ID 的入口，第二轮补采

        def _take(workshop_id: str, label: str) -> bool:
            if not workshop_id or workshop_id in seen:
                return bool(workshop_id)
            seen.add(workshop_id)
            workshop_ids.append(workshop_id)
            _log(f"  频道课程: {label[:42]}", "green")
            return True

        for index, label, href, detail_url in sorted(resolved, key=lambda x: x[0]):
            # 落在频道落地页时提取器会返回空（那里只有频道 ID），
            # 已跳走则按各种形状取；都没取到就听点击期间请求了什么详情接口
            workshop_id = self._channel_workshop_id_from_landing(
                detail_url, exclude_id=channel_id)
            if not workshop_id:
                workshop_id = self._workshop_id_from_requests(
                    captured, exclude_id=channel_id)
            debug(f"频道页点击[{index}] label={label[:30]!r} "
                  f"href={_debug_url_shape(href)} → {_debug_url_shape(detail_url)} "
                  f"id={workshop_id or '-'} 请求{len(captured)}条")
            if not workshop_id:
                misses.append((index, label, href))
            _take(workshop_id, label)

        # 第二轮：漏掉的入口串行补采（等满弹窗地址），专治"连点太快漏卡片"
        if misses and not self._stop_event.is_set():
            _log(f"  第一轮有 {len(misses)} 个入口没取到 ID，串行补采一次", "yellow")
            debug(f"频道页补采: {[m[0] for m in misses]}")
            for index, label, href in misses:
                if self._stop_event.is_set():
                    break
                retry_captured: List[str] = []
                try:
                    link = anchors.nth(index)
                    detail_url = await self._channel_open_detail(
                        page, lambda item=link: item.click(timeout=10000),
                        captured=retry_captured)
                except Exception as exc:
                    debug(f"频道页补采[{index}] 失败: {type(exc).__name__}")
                    continue
                workshop_id = self._channel_workshop_id_from_landing(
                    detail_url, exclude_id=channel_id)
                if not workshop_id:
                    workshop_id = self._workshop_id_from_requests(
                        retry_captured, exclude_id=channel_id)
                debug(f"频道页补采[{index}] label={label[:30]!r} "
                      f"→ {_debug_url_shape(detail_url)} id={workshop_id or '-'}")
                if not workshop_id and _page_url(page) != channel_url:
                    try:
                        await page.goto(channel_url, wait_until="domcontentloaded",
                                        timeout=15000)
                    except Exception:
                        pass
                _take(workshop_id, label)

        if not workshop_ids:
            _log("频道页多次点击都未打开专题班详情，停止尝试", "yellow")
        return workshop_ids
    async def _collect_channel_workshops(self, page: Page, channel_url: str,
                                         log_callback=None) -> List[str]:
        """从学习频道页收集专题班 ID。

        三层，越靠前越稳：先读链接（href/data-*/onclick），再退回点击卡片。
        频道页可能改写路由（例如落到 /channel/detail/<id>），所以这里不再强求
        hash 一直停在 /channel/show/，只等页面把链接渲染出来。
        """
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        exclude_id = _channel_id_from_url(channel_url)

        # 内容接口是在导航时请求的，监听必须在 goto 之前挂上
        responses = []
        keep_responses = [True]
        response_listener = None
        if hasattr(page, "on"):
            def _on_response(response):
                if not keep_responses[0]:
                    return
                try:
                    url = str(getattr(response, "url", "") or "")
                    headers = getattr(response, "headers", None) or {}
                    ctype = str(headers.get("content-type", "")).lower()
                    if "json" not in ctype:
                        return
                    if not any(k in url for k in ("/cu/", "channel", "workshop", "content")):
                        return
                    responses.append(response)
                except Exception:
                    pass
            try:
                page.on("response", _on_response)
                response_listener = _on_response
            except Exception:
                response_listener = None

        def _drop_response_listener():
            keep_responses[0] = False
            if response_listener is not None and hasattr(page, "remove_listener"):
                try:
                    page.remove_listener("response", response_listener)
                except Exception:
                    pass

        try:
            await page.goto(channel_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            _drop_response_listener()
            _log(f"频道页打开失败: {e}", "yellow")
            return []
        await self._wait_channel_ready(page)
        login_state = await self._channel_login_state(page)
        debug(f"频道页登录状态: {login_state}")
        if login_state == "login-form":
            _log("频道页显示登录框：当前会话可能没有登录到该页面", "yellow")

        await self._dismiss_channel_gate(page, _log)

        # 内容可能是异步渲染的（提示弹完才拉数据）：多试几轮，别一轮定生死
        workshop_ids = []
        for attempt in range(3):
            workshop_ids = await self._channel_harvest_workshops(page, _log)
            if workshop_ids:
                break
            if attempt < 2:
                try:
                    await page.wait_for_timeout(2500)
                except Exception:
                    break
                await self._dismiss_channel_gate(page, _log)

        if workshop_ids:
            _drop_response_listener()
            _log(f"频道页发现 {len(workshop_ids)} 个专题班入口（直接读取链接）", "blue")
            _log(f"频道页采集到 {len(workshop_ids)} 个专题班", "green")
            return workshop_ids

        # 链接上没有 ID 时，先用快速通道试一次（页面组件状态 / 内容接口），
        # 但**不能拿它当权威**：实测它命中过频道元数据只回 3 个，也出现过
        # 与可见条目数正好相等却仍然不全的情况。所以点击照跑，最后取并集。
        candidates = await self._channel_content_candidate_count(page)
        fast_ids = await self._channel_harvest_from_page_data(page, exclude_id, _log)
        if not fast_ids:
            fast_ids = await self._channel_harvest_from_responses(
                responses, exclude_id, log_callback=_log)
        _drop_response_listener()
        debug(f"频道页快速通道: 取到 {len(fast_ids)} 个 ID，页面内容候选 {candidates} 个")

        await self._dump_channel_anchors(page, _log)
        gate = await self._channel_looks_gated(page)
        if gate:
            # 卡在登录/公约页时逐个点链接毫无意义，直接说清楚原因
            _log(f"频道页仍停在登录/提示页（命中「{gate}」，登录状态={login_state}），"
                 f"未进入频道内容", "red")
            debug(f"频道页被登录/公约遮挡: gate={gate} login_state={login_state}")
            return []
        _log("频道页链接里没有专题班 ID，回退为点击卡片读取", "blue")
        clicked_ids = await self._channel_click_workshops(page, channel_url, _log)
        # 取并集：点击是权威枚举，快速通道只做补充（两边都可能多出对方漏掉的）
        workshop_ids = list(clicked_ids)
        for workshop_id in fast_ids:
            if workshop_id not in workshop_ids:
                workshop_ids.append(workshop_id)
        if clicked_ids and fast_ids:
            debug(f"频道页取并集: 点击 {len(clicked_ids)} 个 + 快速通道 "
                  f"{len(fast_ids)} 个 → {len(workshop_ids)} 个")
        _log(f"频道页采集到 {len(workshop_ids)} 个专题班",
             "green" if workshop_ids else "yellow")
        return workshop_ids

    async def _learn_course_urls(self, urls: List[Union[str, Dict]], workers: int,
                                 _log, _progress, _hours):
        """手动模式：直接打开课程详情URL学习（无需专题班）"""
        nw = min(workers, len(urls))
        _log(f"共 {len(urls)} 个课程URL待学习，使用 {nw} 个线程", "blue")
        # 手动模式的总体进度：一个 URL 学完（成功/失败/需人工）算一项
        manual_total = len(urls)
        manual_done = [0]
        _progress(_manual_progress_payload(0, manual_total, "准备中"))

        def format_eta(seconds):
            """格式化课程剩余时间，供训练营进度回调显示。"""
            try:
                seconds = max(0.0, float(seconds))
            except (TypeError, ValueError):
                return "计算中..."
            if seconds < 60:
                return f"剩{seconds:.0f}秒"
            minutes = int(seconds // 60)
            if minutes < 60:
                return f"剩{minutes}分"
            hours = int(minutes // 60)
            remain_minutes = minutes % 60
            return f"剩{hours}时{remain_minutes}分" if remain_minutes else f"剩{hours}时"

        async def cworker(wid, wp, task_urls):
            for task in task_urls:
                # 用户变更配置：停止
                if self._stop_event.is_set():
                    break
                if isinstance(task, dict):
                    url = task.get("url", "")
                    title = task.get("title") or url[:40]
                else:
                    url = task
                    title = (url.split("id=")[-1][:40] if "id=" in url else url[:40])
                _log(f"[线程{wid+1}] 打开课程: {url}", "blue")
                _progress({"wid": wid, "course": title, "progress": "-", "eta": "-", "status": "加载中"})
                last_reported_progress = [None]
                try:
                    await wp.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await wp.wait_for_timeout(5000)
                    is_trainingcamp = bool(re.search(r"https?://[^/]+/trainingcamp/#/traincamp/study/", url))
                    if is_trainingcamp:
                        expected_route = url.split("#", 1)[-1].split("?", 1)[0].rstrip("/")
                        actual_route = wp.url.split("#", 1)[-1].split("?", 1)[0].rstrip("/")
                        _log(f"[线程{wid+1}] 训练营课程路由: {actual_route}", "blue")
                        if actual_route != expected_route:
                            _log(f"[线程{wid+1}] 训练营路由与目标不符，预期 {expected_route}", "yellow")
                    if not is_trainingcamp:
                        # 旧课程详情页需要先点击学习按钮；训练营路由已经直接进入组件学习页。
                        for kw in ["我要学习", "开始学习", "进入课程", "继续学习", "学习课程", "进入课程学习"]:
                            try:
                                sb = wp.locator(f"text={kw}").first
                                if await sb.count() > 0:
                                    await sb.click()
                                    await wp.wait_for_timeout(5000)
                                    break
                            except:
                                pass
                    def on_progress(pct):
                        now = time.monotonic()
                        last_reported_progress[0] = max(0.0, min(99.0, float(pct)))
                        if not hasattr(on_progress, "history"):
                            on_progress.history = []
                            on_progress.started = now
                        history = on_progress.history
                        if not history or last_reported_progress[0] != history[-1][1]:
                            history.append((now, last_reported_progress[0]))
                            if len(history) > 20:
                                del history[:-20]
                        eta = "计算中..."
                        if len(history) >= 2:
                            t0, p0 = history[0]
                            t1, p1 = history[-1]
                            dt = t1 - t0
                            dp = p1 - p0
                            if dt >= 1.0 and dp > 0:
                                eta = format_eta((100.0 - p1) * dt / dp)
                        elif last_reported_progress[0] > 0 and now - on_progress.started > 1:
                            eta = format_eta(
                                (100.0 - last_reported_progress[0])
                                * (now - on_progress.started)
                                / last_reported_progress[0]
                            )
                        _progress({"wid": wid, "course": title,
                                   "progress": f"{last_reported_progress[0]:.0f}%", "eta": eta, "status": "学习中"})
                    # 训练营课程页可能同时有多个视频和考试，顺序很关键：
                    # 必须先看视频再做考试——平台有可能在考试通过后就把整页标记完成，
                    # 先考试会让接下来的视频学习被「已完成」短路掉（一节视频都没看）。
                    if is_trainingcamp:
                        play_ok = await self.find_and_play_trainingcamp_video(
                            wp, wid, on_progress, log_callback=_log)
                    else:
                        play_ok = await self.find_and_play_video(wp, wid, on_progress)

                    exam_state = None
                    if is_trainingcamp and self.exam_enabled:
                        _progress({"wid": wid, "course": title, "progress": "-",
                                   "eta": "-", "status": "考试答题中"})
                        exam_state = await self.solve_trainingcamp_exams(wp, wid, _log)

                    exam_ok = bool(exam_state and exam_state.get("found")
                                   and exam_state.get("all_ok"))
                    status_text = "✓ 完成"
                    if not play_ok and is_trainingcamp and exam_ok and self.exam_enabled:
                        # 视频已学完但平台要求考试后才放行（或本身就是纯考试页）：
                        # 补点「完成学习」由平台判定；视频确实没学完时不代替平台放行。
                        media = await self._trainingcamp_media_progress(wp)
                        if media and (media.get("ready") or media.get("count") == 0
                                      or media.get("page_done")):
                            if await self._confirm_trainingcamp_finish(wp, wid, _log):
                                play_ok = True
                                status_text = ("✓ 考试完成" if media.get("count") == 0
                                               else "✓ 完成")
                        else:
                            debug(f"[线程{wid+1}] 考试已完成但视频未达标，不代替平台放行: {media}")
                    if play_ok:
                        _progress({"wid": wid, "course": title, "progress": "100%",
                                   "eta": "-", "status": status_text})
                        _log(f"[线程{wid+1}] 完成: {title}", "green")
                    else:
                        # 只剩「需要人工完成」的组件（如交作业）时不算失败，标记为需人工
                        info = await self._trainingcamp_media_progress(wp) if is_trainingcamp else None
                        manual_pending = (info or {}).get("manual_pending") or []
                        blocked = (info or {}).get("automatable_pending") or []
                        if manual_pending and not blocked:
                            _progress({"wid": wid, "course": title, "progress": "-",
                                       "eta": "-", "status": "⚠ 需人工"})
                            _log(f"[线程{wid+1}] 跳过（需人工完成 {'、'.join(manual_pending)}）: {title}",
                                 "yellow")
                        else:
                            progress_text = (
                                f"{last_reported_progress[0]:.0f}%"
                                if last_reported_progress[0] is not None else "-"
                            )
                            _progress({"wid": wid, "course": title, "progress": progress_text,
                                       "eta": "-", "status": "未完成"})
                            _log(f"[线程{wid+1}] 未完成: {title}", "yellow")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _log(f"[线程{wid+1}] 课程失败: {title} - {e}", "red")
                    progress_text = (
                        f"{last_reported_progress[0]:.0f}%"
                        if last_reported_progress[0] is not None else "-"
                    )
                    _progress({"wid": wid, "course": title, "progress": progress_text, "eta": "-", "status": "异常"})
                # 检查学习目标（O1 节流：TTL 缓存）
                if self.study_goal > 0:
                    try:
                        h = await self._get_study_hours(wp)
                        _hours({"central": h.get("central", 0), "online": h.get("online", 0),
                                "updated": datetime.now().strftime("%H:%M:%S")})
                        if h.get(self.goal_type, 0) >= self.study_goal:
                            _log("✓ 学习目标已达成!", "bold green")
                            raise GoalReached()
                    except GoalReached:
                        raise
                    except Exception:
                        pass

                # 这一项不论是学完、失败还是需人工，都算处理过，推进总进度。
                # （目标达成会 raise 提前跳出，不会走到这里。）
                manual_done[0] += 1
                _progress(_manual_progress_payload(manual_done[0], manual_total))

        tasks = []
        for wid in range(nw):
            tasks.append(asyncio.create_task(cworker(wid, self.pages[wid], urls[wid::nw])))
            await asyncio.sleep(WORKER_STAGGER_SECONDS)
        try:
            await asyncio.gather(*tasks)
        except GoalReached:
            for t in tasks:
                if not t.done():
                    t.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except:
                pass
        finally:
            if self._stop_event.is_set() and manual_done[0] < manual_total:
                _progress(_manual_progress_payload(manual_done[0], manual_total, "已停止"))

    async def learn_from_urls(self, urls: List[str], workers: int = 1,
                               progress_callback=None, hours_callback=None, log_callback=None):
        """手动模式：从指定URL列表学习课程"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        _progress = progress_callback or (lambda d: None)
        _hours = hours_callback or (lambda d: None)

        # 手动模式没有学时目标，总进度按「要学的东西学完了多少」来报。
        # 总数要等采集完才知道，先给一个"准备中"，让界面立刻有反馈。
        _progress(_manual_progress_payload(0, 0, "准备中"))

        page = self.pages[0]

        # 区分专题班、训练营详情与课程URL。
        workshop_ids = []
        trainingcamp_ids = []
        channel_urls = []
        course_urls = []
        for url in urls:
            # 频道页不是课程本身；其卡片会打开带课程ID的专题班详情。
            channel_match = re.search(r"#/channel/show/[^/?#]+", url)
            if channel_match:
                if url not in channel_urls:
                    channel_urls.append(url)
                continue

            study_match = re.search(r"#/traincamp/study/([^/?#]+)/([^/?#]+)", url)
            if study_match:
                if url not in course_urls:
                    course_urls.append(url)
                continue

            camp_match = re.search(r"#/traincampdetail/([^/?#]+)", url)
            if camp_match:
                camp_id = camp_match.group(1)
                if camp_id not in trainingcamp_ids:
                    trainingcamp_ids.append(camp_id)
                continue

            m = re.search(r'id=([a-f0-9\-]+)', url)
            if not m:
                continue
            uid = m.group(1)
            if "/course/" in url or "#/course" in url:
                if url not in course_urls:
                    course_urls.append(url)
            else:
                if uid not in workshop_ids:
                    workshop_ids.append(uid)

        debug(f"手动模式URL分类: 共{len(urls)}条 → 频道{len(channel_urls)} "
              f"训练营{len(trainingcamp_ids)} 课程{len(course_urls)} 专题班{len(workshop_ids)}")
        if not (channel_urls or trainingcamp_ids or course_urls or workshop_ids):
            # 一条都没认出来：明确告诉用户，别让进度环停在"准备中"
            debug(f"手动模式: {len(urls)} 条 URL 都没识别出类型")
            _progress(_manual_progress_payload(0, 0, "未识别到可学习的链接"))
            _log("未从URL中提取到有效的专题班/训练营/课程ID", "red")
            return

        for channel_url in channel_urls:
            _log(f"正在采集学习频道: {channel_url.rsplit('/', 1)[-1]}", "blue")
            found = await self._collect_channel_workshops(page, channel_url, _log)
            debug(f"学习频道采集结果: {len(found)} 个专题班")
            for workshop_id in found:
                if workshop_id not in workshop_ids:
                    workshop_ids.append(workshop_id)

        # 训练营详情页中的课程页使用 /traincamp/study/{campId}/{courseId} 路由。
        for camp_id in trainingcamp_ids:
            _log(f"正在采集训练营课程: {camp_id}", "blue")
            camp_courses = await self._collect_trainingcamp_courses(page, camp_id, _log)
            debug(f"训练营采集结果: {len(camp_courses)} 个课程链接")
            course_urls.extend(camp_courses)

        # 课程URL：直接打开课程页学习（不走专题班流程）
        if course_urls:
            deduplicated_urls = []
            course_url_indexes = {}
            for task in course_urls:
                url = task.get("url", "") if isinstance(task, dict) else task
                if not url:
                    continue
                if url not in course_url_indexes:
                    course_url_indexes[url] = len(deduplicated_urls)
                    deduplicated_urls.append(task)
                elif isinstance(task, dict) and not isinstance(deduplicated_urls[course_url_indexes[url]], dict):
                    # 详情页采集到的标题比单独粘贴课程路由更完整。
                    deduplicated_urls[course_url_indexes[url]] = task
            debug(f"手动模式课程URL开始学习: {len(deduplicated_urls)} 个")
            await self._learn_course_urls(deduplicated_urls, workers, _log, _progress, _hours)

        if not workshop_ids:
            if not course_urls:
                if trainingcamp_ids:
                    _log("训练营中未获取到可学习课程", "yellow")
                    debug("手动模式: 训练营没有采集到可学习课程")
                    _progress(_manual_progress_payload(0, 0, "没有可学习的内容（可能已全部完成）"))
                else:
                    _log("未从URL中提取到有效的专题班/训练营/课程ID", "red")
                    debug("手动模式: 专题班/课程都没采集到内容")
                    _progress(_manual_progress_payload(0, 0, "没有可学习的内容"))
            return

        _log(f"共 {len(workshop_ids)} 个专题班待学习", "blue")

        # 采集每个专题班的课程。24 个专题班一个个报名要等好几分钟，
        # 所以不再"全部采完才开跑"：先把第一批交给学习线程，剩下的专题班由
        # 后台 producer 协程**并行**报名+采集（不是等 worker 空闲才去采）。
        ws_locks = {}
        pending = list(workshop_ids)
        collect_page = await self._new_collection_page(page)
        total_counter = [0]

        async def collect_one(ws_id: str) -> List:
            """采集一个专题班，返回待学任务 [(ws_id, idx, course, title), ...]。"""
            ws_url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
            _log(f"正在采集: {ws_id[:16]}...", "blue")

            # 导航（先回列表页重置SPA，再导航到目标）
            list_url = "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise"
            body = ""
            for nav_url in [ws_url, ws_url.replace("/myworkshop/detail", "/detail")]:
                try:
                    await collect_page.goto(list_url, wait_until="domcontentloaded", timeout=15000)
                    await collect_page.wait_for_timeout(2000)
                    await collect_page.evaluate(
                        f"window.location.hash = '{nav_url.split('#')[1]}';")
                    await collect_page.wait_for_timeout(5000)
                except Exception:
                    pass
                try:
                    body = await collect_page.locator("body").inner_text(timeout=3000)
                except Exception:
                    body = ""
                if "创建日期" in body or "报名" in body:
                    break

            if "报名截止" in body or "报名已结束" in body:
                _log(f"  ⊘ 报名截止，跳过", "yellow")
                return []

            # 专题班必须先报名，课程列表才会出现；报名后详情地址会变，
            # 所以要重新进一次详情页等服务器处理（与自动模式同一套做法）。
            if await self._enroll_workshop_if_needed(collect_page, ws_url, _log):
                try:
                    await collect_page.goto(ws_url, wait_until="domcontentloaded", timeout=15000)
                    await collect_page.wait_for_timeout(5000)
                except Exception as exc:
                    debug(f"报名后重新进入详情页失败: {type(exc).__name__}")

            # 点击课程标签
            for tab_text in ["课程", "课程列表", "课程目录"]:
                try:
                    tab = collect_page.locator(f"text={tab_text}").first
                    if await tab.count() > 0 and await tab.is_visible():
                        await tab.click()
                        await collect_page.wait_for_timeout(3000)
                        break
                except Exception:
                    pass

            # 等待数据加载
            for _ in range(4):
                rows = await collect_page.locator("tr.text-center").count()
                if rows > 0:
                    break
                await collect_page.wait_for_timeout(3000)

            courses = await self.get_courses_from_workshop(collect_page)
            if not courses:
                _log(f"  ✗ 未获取到课程", "yellow")
                return []

            to_learn = [(i, c) for i, c in enumerate(courses)
                        if self._is_learnable(c.get('action', ''), c.get('hours', ''),
                                              c.get('progress', ''))]
            ws_title = body[:50].split("\n")[0].strip() if body else ws_id[:16]

            if not to_learn:
                _log(f"  ✓ 全部已完成（{len(courses)}门）", "green")
                return []

            _log(f"  ✓ {len(to_learn)} 门待学（共{len(courses)}门）", "green")
            ws_locks[ws_id] = asyncio.Lock()
            return [(ws_id, ci, c, ws_title) for ci, c in to_learn]

        try:
            # 拿到第一个专题班的课就开跑（线程已全部待命），后面的边学边补，
            # 池子涨到几门就立刻有几门被空闲线程接走 —— 不用等池子备满。
            all_tasks = []
            while pending and not all_tasks and not self._stop_event.is_set():
                all_tasks = await collect_one(pending.pop(0))
            if not all_tasks:
                _log("没有需要学习的课程", "yellow")
                return
            total_counter[0] = len(all_tasks)

            async def collect_rest(queue) -> None:
                """后台持续把专题班报名+采集进课程池——与学习**并行**。

                之前靠 fetch_more_callback（worker 空闲才调用），结果"开始学了
                2 门课程就没有继续报名了"：worker 忙着学，采集根本没机会跑。
                这里是一个独立协程，不断给池子补课，让 worker 不用等。
                """
                while pending and not self._stop_event.is_set():
                    tasks = await collect_one(pending.pop(0))
                    for task in tasks:
                        queue.put_nowait((*task, 0))
                    total_counter[0] += len(tasks)
                    if tasks:
                        debug(f"边学边采集: 新增 {len(tasks)} 门课程，"
                              f"池中 {queue.qsize()} 门，剩余 {len(pending)} 个专题班")
                    elif pending:
                        debug(f"边学边采集: 该专题班没有可学课程，"
                              f"继续下一个（剩余 {len(pending)} 个）")

            _log(f"\n开始学习 {len(all_tasks)} 门课程"
                 f"（{max(1, int(workers))} 个线程待命，剩余 {len(pending)} 个专题班后台采集）",
                 "bold blue")
            debug(f"手动模式专题班开始学习: 池中 {len(all_tasks)} 门，"
                  f"待采集 {len(pending)} 个专题班，线程 {workers}")
            await self.parallel_learn_courses(
                all_tasks, ws_locks, None, _progress, _hours, _log,
                report_item_progress=True, total_ref=total_counter,
                producer=collect_rest,
            )
        finally:
            await self._close_collection_page(page, collect_page)

    async def _new_collection_page(self, fallback: Page) -> Page:
        """采集用的独立标签页。

        学习 worker 会一直占用 pages[0..N-1]，如果"边学边采集"共用同一页，
        两边的导航会互相踩。单独开一页，学完再关掉。
        """
        try:
            if self.context is not None:
                return await self.context.new_page()
        except Exception as exc:
            debug(f"创建采集专用标签页失败: {type(exc).__name__}: {_safe_debug_error(exc)}")
        return fallback

    async def _close_collection_page(self, fallback: Page, page: Page) -> None:
        if page is fallback:
            return
        try:
            await page.close()
        except Exception:
            pass

    async def get_available_tags(self, page: Page) -> Dict[str, List[str]]:
        # 从页面提取所有可见标签，按分类分组
        try:
            tags_dict = await page.evaluate('''() => {
                const result = {};
                const cats = document.querySelectorAll('ul.tag-tree-list > li');
                cats.forEach(cat => {
                    const titleEl = cat.querySelector('.portal-title');
                    if (!titleEl) return;
                    const category = titleEl.innerText.trim();
                    if (!category) return;
                    const tags = [];
                    const items = cat.querySelectorAll('li.tag-second span.single-tag');
                    items.forEach(span => {
                        const text = span.innerText.trim();
                        if (text) tags.push(text);
                    });
                    if (tags.length > 0) result[category] = tags;
                });
                return result;
            }''')
            return tags_dict
        except Exception as e:
            console.print(f"获取标签列表失败: {e}", style="yellow")
            return {}

    async def interactive_tag_selection(self, page: Page) -> List[str]:
        # 等待标签树加载
        try:
            await page.wait_for_selector("ul.tag-tree-list", timeout=15000)
            await page.wait_for_timeout(3000)
        except:
            console.print("标签树未加载，尝试从页面文本提取标签...", style="yellow")
            # 尝试从文本提取（兜底）
            _txt = await page.locator("body").inner_text()
            _cats = {}
            _current_cat = ""
            for _ln in _txt.split("\n"):
                _ln = _ln.strip()
                if _ln in ("岗位标签", "党性教育", "研修院", "平台", "学科"):
                    _current_cat = _ln
                    _cats[_current_cat] = []
                elif _current_cat and _ln and len(_ln) < 30 and _ln != "不限":
                    _cats[_current_cat].append(_ln)
            if any(v for v in _cats.values()):
                console.print("从文本提取成功", style="green")
                tags_by_category = _cats
            else:
                console.print("无法获取标签", style="yellow")
                return []
        

        # 如果文本兜底已提取到标签，跳过DOM查询
        if 'tags_by_category' not in dir() or not tags_by_category:
            tags_by_category = await self.get_available_tags(page)
        if not tags_by_category:
            console.print("未获取到可用标签", style="yellow")
            return []
        
        all_tags = []
        idx = 1
        
        console.print()
        console.print("[bold]可用的标签分类:[/bold]", style="blue")
        
        for category, tags in tags_by_category.items():
            for tag in tags:
                console.print(f"  [{idx:3d}] {category} → {tag}", style="white")
                all_tags.append(tag)
                idx += 1
        
        console.print()
        console.print("请输入要筛选的标签编号（多个用逗号分隔，直接回车跳过）: ", style="yellow", end="")
        choice = await async_input("输入编号（逗号分隔，直接回车跳过）", default="", timeout=30)
        if not choice:
            console.print("跳过标签筛选", style="yellow")
            return []
        
        selected_indices = []
        for part in choice.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(all_tags):
                selected_indices.append(int(part) - 1)
        
        selected_tags = [all_tags[i] for i in selected_indices]
        if selected_tags:
            self.tags_to_learn = selected_tags
            console.print(f"已选择标签: {', '.join(selected_tags)}", style="green")
        return selected_tags

    async def get_study_hours(self) -> float:
        """查看当前学时（CLI hours 命令）"""
        try:
            h = await self._get_study_hours(self.pages[0])
            central = h.get("central", 0)
            online = h.get("online", 0)
            console.print(f"集中培训: {central:.1f} 学时", style="bold blue")
            console.print(f"网络自学: {online:.1f} 学时", style="bold blue")
            console.print(f"总计: {central + online:.1f} 学时", style="green")
            return central + online
        except Exception as e:
            console.print(f"获取学时失败: {e}", style="red")
            return 0.0


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """网络课程自动学习工具"""
    if ctx.invoked_subcommand is None:
        ctx.invoke(start)


@cli.command()
@click.option("--headless", is_flag=True, help="隐藏浏览器界面")
@click.option("--workers", default=1, type=click.IntRange(1, 20),
              help="同时学习的页面数量（1-20）")
@click.option("--target-hours", default=0.0, help="目标学习学时，0表示不限制")
@click.option("--tags", multiple=True, help="要学习的标签，例如：党的创新理论教育 党性教育")
@click.option("--exam/--no-exam", "exam", default=None,
              help="训练营考试是否用 DeepSeek 自动答题（默认读配置）")
@click.option("--deepseek-key", default="", help="临时指定 DeepSeek API Key（覆盖配置）")
def start(headless, workers, target_hours, tags, exam, deepseek_key):
    """开始自动学习"""
    async def run():
        # 运行时询问worker数量和headless配置
        _w, _h = workers, headless
        if workers == 1 and not headless:  # 用户没有用参数，就询问
            _saved = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                        _saved = json.load(_f)
                except:
                    pass
            if _saved.get("workers") is not None or _saved.get("headless") is not None:
                _sw = _saved.get("workers", 1)
                _sh = _saved.get("headless", False)
                console.print(f"发现上次配置: 工作线程={_sw}, 无头模式={'是' if _sh else '否'}", style="green")
                _use = await async_input("使用上次配置？(y/n)", default="y", timeout=5)
                if _use in ('y', 'yes', ''):
                    _w, _h = _sw, _sh
                else:
                    print()
                    _wi = await async_input("工作线程数量 (默认1): ", default="1", timeout=10)
                    if _wi.isdigit() and int(_wi) > 0:
                        _w = int(_wi)
                    _hi = await async_input("无头模式 (浏览器不显示界面)？(y/n，默认n)", default="n", timeout=5)
                    _h = _hi in ('y', 'yes')
                    # 保存配置
                    try:
                        with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
                            json.dump({"workers": _w, "headless": _h}, _f, ensure_ascii=False, indent=2)
                        console.print("配置已保存", style="green")
                    except:
                        pass
            else:
                print()
                _wi = await async_input("工作线程数量 (默认1): ", default="1", timeout=10)
                if _wi.isdigit() and int(_wi) > 0:
                    _w = int(_wi)
                _hi = await async_input("无头模式 (浏览器不显示界面)？(y/n，默认n)", default="n", timeout=5)
                _h = _hi in ('y', 'yes')
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
                        json.dump({"workers": _w, "headless": _h}, _f, ensure_ascii=False, indent=2)
                    console.print("配置已保存", style="green")
                except:
                    pass
        
        # 显示运行配置
        from rich.panel import Panel
        config_table = Table(show_header=False, box=None, padding=(0, 2))
        config_table.add_column("项", style="cyan")
        config_table.add_column("值", style="green")
        config_table.add_row("工作线程", str(_w))
        config_table.add_row("无头模式", "是" if _h else "否")
        console.print(Panel(config_table, title="[bold]运行配置[/bold]", border_style="blue"))

        learner = AutoLearner(headless=_h, workers=_w)
        learner.target_hours = target_hours
        learner.tags_to_learn = list(tags)

        # 考试自动答题：命令行参数优先，其次读配置文件
        try:
            _ec = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                    _ec = json.load(_f)
            _exam_settings = exam_settings_from_config(_ec)
            if exam is not None:
                _exam_settings["exam_enabled"] = bool(exam)
            if deepseek_key:
                _exam_settings["deepseek_api_key"] = deepseek_key
            learner.apply_exam_settings(_exam_settings)
            if learner.exam_enabled:
                if learner.deepseek_api_key:
                    console.print(f"考试自动答题已开启（模型 {learner.deepseek_model}）", style="blue")
                    console.print(f"交卷延时：每题 {learner.exam_delay_min:g}~"
                                  f"{learner.exam_delay_max:g} 秒（按题量随机等待后交卷）",
                                  style="blue")
                else:
                    console.print("考试自动答题已开启，但未配置 DeepSeek API Key，将跳过考试", style="yellow")
        except Exception:
            pass

        try:
            await learner.init()
            await learner.login()

            # 学习目标设置
            try:
                _gc = {}
                if os.path.exists(CONFIG_PATH):
                    with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                        _gc = json.load(_f)
                _saved_goal = _gc.get("study_goal", 0)
                _saved_type = _gc.get("goal_type", "central")
                if _saved_goal > 0:
                    _stn = "集中培训" if _saved_type == "central" else "网络自学"
                    console.print(f"发现保存的学习目标: {_stn} {_saved_goal} 学时", style="green")
                    _use = await async_input("使用？(y/n)", default="y", timeout=5)
                    if _use in ('y', 'yes', ''):
                        learner.study_goal = _saved_goal
                        learner.goal_type = _saved_type
                if learner.study_goal <= 0:
                    _gt = await async_input("目标类型: 集中培训(c) / 网络自学(w)？(默认c)", default="c", timeout=10)
                    learner.goal_type = "online" if _gt in ('w', '网络自学') else "central"
                    _gm = await async_input("目标模式: 总学时(t) / 还需学时(n)？(默认t)", default="t", timeout=10)
                    _gi = await async_input("输入学时数（0=不限制）", default="0", timeout=10)
                    if _gi.replace('.', '').isdigit() and float(_gi) > 0:
                        goal_val = float(_gi)
                        if _gm in ('n', '还需'):
                            _h_val = await learner._get_study_hours(learner.pages[0])
                            _cur = _h_val.get(learner.goal_type, 0)
                            goal_val = _cur + goal_val
                        learner.study_goal = goal_val
                        _gc["study_goal"] = learner.study_goal
                        _gc["goal_type"] = learner.goal_type
                        with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
                            json.dump(_gc, _f, ensure_ascii=False, indent=2)

                # 显示学习目标面板
                if learner.study_goal > 0:
                    _h_val = await learner._get_study_hours(learner.pages[0])
                    _tn = "集中培训" if learner.goal_type == "central" else "网络自学"
                    _cur = _h_val.get(learner.goal_type, 0)
                    _pct = min(100, _cur / learner.study_goal * 100) if learner.study_goal > 0 else 0
                    _bar = "█" * int(_pct // 5) + "░" * (20 - int(_pct // 5))

                    goal_table = Table(show_header=False, box=None, padding=(0, 2))
                    goal_table.add_column("项", style="cyan")
                    goal_table.add_column("值", style="green")
                    goal_table.add_row("目标类型", _tn)
                    goal_table.add_row("当前学时", f"{_cur:.1f}")
                    goal_table.add_row("目标学时", f"{learner.study_goal:.1f}")
                    goal_table.add_row("完成进度", f"{_bar} {_pct:.1f}%")
                    console.print(Panel(goal_table, title="[bold]学习目标[/bold]", border_style="green"))

                    if _cur >= learner.study_goal and _cur > 0:
                        console.print(f"[bold green]✓ 已达到目标，无需学习！[/bold green]")
                        await async_input("按回车键退出", default="", timeout=600, block=True)
                        return

            except Exception as _ge:
                debug(f"学习目标异常: {_ge}")
            

            # 选择学习模式（有目标时自动选择）
            if learner.study_goal > 0:
                _tn = "集中培训" if learner.goal_type == "central" else "网络自学"
                _mode = "1" if learner.goal_type == "central" else "2"
                console.print(f"目标类型「{_tn}」→ 自动选择{'专题班' if _mode == '1' else '课程'}模式", style="blue")
            else:
                _mode = await async_input("选择模式: 专题班(1) / 课程列表(2)？(默认1)", default="1", timeout=5)
            if _mode == "2":
                await learner._course_mode(learner.pages[0])
                await async_input("\n课程模式完成! 按回车键关闭浏览器", default="", timeout=600, block=True)
                return
            
            # 访问专题班页面
            page = learner.pages[0]
            await page.goto("https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise")
            await asyncio.sleep(5)
            
            # 根据标签筛选
            if tags:
                learner.tags_to_learn = list(tags)
                await learner.filter_by_tags(page)
                await asyncio.sleep(3)
                try:
                    with open(TAGS_STATE_PATH, "w", encoding="utf-8") as _f:
                        json.dump({"tags": list(tags), "source": "cli"}, _f)
                except:
                    pass
            else:
                saved_tags = None
                if os.path.exists(TAGS_STATE_PATH):
                    try:
                        with open(TAGS_STATE_PATH, "r", encoding="utf-8") as _f:
                            saved_tags = json.load(_f).get("tags", [])
                    except:
                        pass

                use_tags = []
                if saved_tags:
                    console.print(f"已保存标签: [cyan]{', '.join(saved_tags)}[/cyan]")
                    _c = await async_input("使用(u) / 重新选择(r) / 跳过(s)？(默认u)", default="u", timeout=5)
                    if _c in ('', 'u', 'use'):
                        use_tags = saved_tags
                    elif _c in ('r', 're', '重新'):
                        use_tags = await learner.interactive_tag_selection(page)
                elif not tags:
                    _c = await async_input("是否筛选标签？(y/n，默认n)", default="n", timeout=5)
                    if _c in ('y', 'yes'):
                        use_tags = await learner.interactive_tag_selection(page)

                if use_tags:
                    learner.tags_to_learn = use_tags
                    await learner.filter_by_tags(page)
                    await asyncio.sleep(3)
                    try:
                        with open(TAGS_STATE_PATH, "w", encoding="utf-8") as _f:
                            json.dump({"tags": use_tags}, _f, ensure_ascii=False, indent=2)
                    except:
                        pass

            # 加载学习进度
            progress = learner.load_progress()
            completed_ids = set(progress.get("completed_ws_ids", []))
            if completed_ids:
                console.print(f"已有进度: [green]{len(completed_ids)}[/green] 个专题班已完成")
                _rp = await async_input("继续(回车) / 重新开始(r)？", default="", timeout=5)
                if _rp in ('r', 're', '重新'):
                    completed_ids = set()
                    learner.save_progress(set())

            # ===== 按需翻页 + 逐页采集 + 学习 =====
            page_num = 1
            no_more_pages = False  # 标记是否已无更多页
            tasks = []
            ws_locks = {}

            # 采集课程，至少凑够 worker 数量再开始学（除非已无更多页）
            while len(tasks) < learner.workers and not no_more_pages:
                current_workshops = await learner.get_workshops(page)
                if not current_workshops:
                    no_more_pages = True
                    break

                console.print(f"\n{'='*50}", style="bold blue")
                console.print(f"第 {page_num} 页: {len(current_workshops)} 个专题班", style="bold blue")
                console.print(f"{'='*50}", style="bold blue")
                await learner.display_workshops(current_workshops)

                new_tasks, new_locks = await learner._collect_workshops_courses(
                    page, current_workshops, completed_ids)
                learner.save_progress(completed_ids, page_num, 0)
                tasks.extend(new_tasks)
                ws_locks.update(new_locks)

                if len(tasks) >= learner.workers:
                    break

                # 不够，翻页继续采
                moved = await learner.go_to_next_page(page)
                if not moved:
                    no_more_pages = True
                else:
                    page_num += 1
                    await page.wait_for_timeout(3000)

            if tasks:
                # 定义回调：worker队列空时自动翻页采集更多课程
                _fetch_lock = asyncio.Lock()
                _page_ref = [page]  # 用列表包装以便闭包修改

                async def fetch_more_courses(queue):
                    nonlocal no_more_pages, page_num
                    if no_more_pages:
                        return 0
                    async with _fetch_lock:
                        # 再次检查（可能其他worker已经采了）
                        if no_more_pages:
                            return 0
                        # 检查目标学时
                        if learner.study_goal > 0:
                            try:
                                _h = await learner._get_study_hours(_page_ref[0])
                                _cur = _h.get(learner.goal_type, 0)
                                if _cur >= learner.study_goal:
                                    console.print(f"\n已达到学习目标! 停止采集", style="bold green")
                                    no_more_pages = True
                                    return 0
                            except:
                                pass
                        # 翻页
                        moved = await learner.go_to_next_page(_page_ref[0])
                        if not moved:
                            console.print("\n已无更多页", style="yellow")
                            no_more_pages = True
                            return 0
                        page_num += 1
                        await _page_ref[0].wait_for_timeout(3000)
                        # 采集新页
                        new_ws = await learner.get_workshops(_page_ref[0])
                        if not new_ws:
                            no_more_pages = True
                            return 0
                        console.print(f"\n自动翻到第 {page_num} 页: {len(new_ws)} 个专题班", style="bold blue")
                        new_tasks, new_locks = await learner._collect_workshops_courses(
                            _page_ref[0], new_ws, completed_ids)
                        learner.save_progress(completed_ids, page_num, 0)
                        # 合并锁
                        ws_locks.update(new_locks)
                        # 加入队列
                        for t in new_tasks:
                            queue.put_nowait((*t, 0))
                        if new_tasks:
                            console.print(f"新增 {len(new_tasks)} 门课程", style="green")
                        return len(new_tasks)

                console.print(f"\n{'='*50}", style="bold blue")
                console.print(f"开始学习（{len(tasks)} 门课程, {learner.workers} 个线程）", style="bold blue")
                console.print(f"{'='*50}", style="bold blue")
                await learner.parallel_learn_courses(tasks, ws_locks, fetch_more_courses)
                # 学完后重新加载已完成列表
                progress = learner.load_progress()
                completed_ids = set(progress.get("completed_ws_ids", []))
            else:
                console.print("没有需要学习的课程", style="yellow")

            console.print("\n✓ 学习流程完成! 浏览器将保持打开", style="bold green")
            await async_input("按回车键关闭浏览器", default="", timeout=600, block=True)
            
        finally:
            await learner.close()
    
    asyncio.run(run())


@cli.command()
def hours():
    """查看当前学时"""
    async def run():
        learner = AutoLearner(headless=False)
        try:
            await learner.init()
            await learner.login()
            hours = await learner.get_study_hours()
            await async_input("\n按回车键关闭浏览器", default="", timeout=600, block=True)
        finally:
            await learner.close()
    
    asyncio.run(run())


@cli.command()
def clear():
    """清除所有保存的会话、凭证和标签筛选"""
    removed = []
    for _p in [STORAGE_STATE_PATH, USER_CREDENTIALS_PATH, TAGS_STATE_PATH, CONFIG_PATH, PROGRESS_PATH]:
        if os.path.exists(_p):
            try:
                os.remove(_p)
                removed.append(_p)
            except:
                pass
    if removed:
        for _r in removed:
            console.print(f"已删除: {_r}", style="green")
        console.print("✓ 清除完成", style="bold green")
    else:
        console.print("没有需要清除的文件", style="yellow")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    cli()
