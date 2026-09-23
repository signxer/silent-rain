#!/usr/bin/env python3
"""润物 Moisten GUI"""
import asyncio
import json
import os
import re
import urllib.request
import urllib.error
import platform
import sys
import threading
import math
from datetime import datetime

from PySide6.QtCore import (
    Qt, QThread, Signal, QSize, QTimer, QEventLoop,
    QPropertyAnimation, QPauseAnimation, QSequentialAnimationGroup, QEasingCurve, Property,
)
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QBrush,
    QLinearGradient, QRadialGradient, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QStackedWidget, QTableWidgetItem,
    QHeaderView, QScrollArea, QFrame,
    QDialog, QLabel, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
    QComboBox, QProgressBar,
)

from qfluentwidgets import (
    FluentIcon as FIF,
    CardWidget, HeaderCardWidget, SimpleCardWidget,
    PrimaryPushButton, PushButton, ToolButton, TransparentToolButton,
    LineEdit, PasswordLineEdit, SpinBox, SwitchButton,
    RadioButton, CheckBox,
    TableWidget, ProgressBar, ProgressRing,
    PlainTextEdit,
    SubtitleLabel, BodyLabel, CaptionLabel, StrongBodyLabel,
    TitleLabel, IconWidget,
    InfoBar, InfoBarPosition,
    Dialog,
    NavigationInterface, NavigationItemPosition, NavigationWidget,
)

from ui_theme import (
    BrandNavigationWidget, PageHeader, StepBar, SurfaceCard,
    apply_theme, normalize_theme_mode, style_moisten_dialog,
)

from main import (
    AutoLearner, CONFIG_PATH, STORAGE_STATE_PATH, USER_CREDENTIALS_PATH,
    DEEPSEEK_DEFAULT_BASE_URL, DEEPSEEK_DEFAULT_MODEL, DeepSeekClient,
    obfuscate_secret, deobfuscate_secret,
)


def _is_dark_theme():
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.Window).lightness() < 128


# ─── Async Thread ──────────────────────────────────────────────────


class AsyncThread(QThread):
    log_signal = Signal(str, str)
    progress_signal = Signal(dict)
    hours_signal = Signal(dict)
    done_signal = Signal(int, int)
    tag_request_signal = Signal(dict)
    tag_confirm_signal = Signal(list, dict)  # (saved_tags, tags_by_category)
    page_confirm_signal = Signal(int)  # last_page
    eta_reset_signal = Signal()  # worker 请求在 GUI 线程重置 ETA 状态
    browser_download_signal = Signal(object)  # True=开始, str=进度文本, False=下载结束
    exam_retry_signal = Signal(str, str)  # (考试名称, 失败原因) → 询问是否重考

    def __init__(self, coro_func, parent=None):
        super().__init__(parent)
        self._coro_func = coro_func
        self._stop_event = threading.Event()
        self._loop = None
        self._main_task = None

    def request_stop(self):
        """请求协作式停止：学习协程在检查点主动退出"""
        self._stop_event.set()

    def cancel_pending(self):
        """在窗口退出等硬截止场景取消主协程。

        Playwright 的单次导航/等待可能还没走到业务层的停止检查点，
        仅设置 threading.Event 会让 GUI 长时间等不到线程退出。取消发生在
        worker 自己的 asyncio loop 中，仍会经过 _run_learning 的 finally 清理。
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._cancel_main_task)
            except RuntimeError:
                pass

    def _cancel_main_task(self):
        task = self._main_task
        if task is not None and not task.done():
            task.cancel()

    def run(self):
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._main_task = loop.create_task(self._coro_func(self))
        if self._stop_event.is_set():
            self._main_task.cancel()
        try:
            loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            # 关闭窗口/强制停止时的正常退出路径，不向日志报告为错误。
            pass
        except Exception as e:
            self.log_signal.emit(f"错误: {e}", "red")
        finally:
            # 协程异常退出时仍可能留下 refresh/预取等后台任务；先取消并等待，
            # 再关闭事件循环，避免下次启动出现 "Task was destroyed" 和页面泄漏。
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                self._main_task = None
                self._loop = None
                loop.close()


class _Sparkline(QWidget):
    """迷你趋势图：绘制学习学时随时间的变化曲线"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points = []

    def set_data(self, points):
        self._points = list(points)
        self.update()

    def paintEvent(self, e):
        if len(self._points) < 1:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dark = _is_dark_theme()
        if w < 10 or h < 10:
            return
        lo, hi = min(self._points), max(self._points)
        span = (hi - lo) or 1.0
        n = len(self._points)
        line_color = QColor("#72B9FF") if _is_dark_theme() else QColor("#3b82c4")
        p.setPen(QPen(line_color, 1.5))
        path = QPainterPath()
        for i, v in enumerate(self._points):
            x = i / max(1, n - 1) * (w - 6) + 3
            y = h - 3 - (v - lo) / span * (h - 6)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        p.drawPath(path)
        # 最后一个点高亮
        lx = (n - 1) / max(1, n - 1) * (w - 6) + 3
        ly = h - 3 - (self._points[-1] - lo) / span * (h - 6)
        p.setPen(Qt.NoPen)
        p.setBrush(line_color)
        p.drawEllipse(int(lx) - 2, int(ly) - 2, 5, 5)
        p.end()


class _GoalRing(QWidget):
    """仪表盘目标环：用轻量 QPainter 绘制，保证浅色/离屏渲染一致。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._track_color = QColor("#E2E7EF")
        self._bar_color = QColor("#1976F3")
        self.setMinimumSize(80, 80)

    def getValue(self):
        return self._value

    def setValue(self, value):
        self._value = max(0, min(100, int(value)))
        self.update()

    value = Property(int, getValue, setValue)

    def setCustomBarColor(self, primary, secondary=None):
        self._bar_color = QColor(primary)
        self.update()

    def setTextVisible(self, visible):
        # 与 QFluentWidgets.ProgressRing 保持兼容；本实现始终显示百分比。
        del visible

    def paintEvent(self, event):
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dark = _is_dark_theme()
        margin = 11
        diameter = min(self.width(), self.height()) - margin * 2
        rect = (self.width() - diameter) / 2, (self.height() - diameter) / 2, diameter, diameter
        track_color = QColor("#3D4D65") if dark else self._track_color
        bar_color = QColor("#61C6FF") if dark and self._bar_color.name().lower() == "#1976f3" else self._bar_color
        text_color = QColor("#F4F7FF") if dark else QColor("#17213D")
        p.setPen(QPen(track_color, 8, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(*[int(v) for v in rect], 0, 360 * 16)
        p.setPen(QPen(bar_color, 8, Qt.SolidLine, Qt.RoundCap))
        if self._value:
            p.drawArc(*[int(v) for v in rect], 90 * 16, -int(self._value * 360 * 16 / 100))
        p.setPen(text_color)
        font = p.font()
        font.setPointSize(16)
        font.setBold(True)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignCenter, f"{self._value}%")
        p.end()


class _MetricIcon(QWidget):
    """参考稿里的彩色线性图标，避免 QFluent 默认图标抢走视觉重点。"""

    def __init__(self, kind="bars", color="#2B83F6", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.color = QColor(color)
        self.setFixedSize(28, 28)

    def paintEvent(self, event):
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self.color, 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        w, h = self.width(), self.height()
        if self.kind == "clock":
            p.drawEllipse(4, 4, 20, 20)
            p.drawLine(14, 14, 14, 8)
            p.drawLine(14, 14, 19, 17)
        elif self.kind == "cap":
            p.setBrush(QBrush(self.color))
            cap = QPainterPath()
            cap.moveTo(3, 11)
            cap.lineTo(14, 5)
            cap.lineTo(25, 11)
            cap.lineTo(14, 17)
            cap.closeSubpath()
            p.drawPath(cap)
            p.drawLine(7, 14, 7, 20)
            p.drawArc(7, 14, 14, 10, 180 * 16, 180 * 16)
        elif self.kind == "laptop":
            p.setBrush(QBrush(self.color))
            p.drawRoundedRect(5, 5, 18, 14, 2, 2)
            p.drawLine(3, 22, 25, 22)
            p.drawLine(9, 22, 10, 19)
            p.drawLine(19, 22, 18, 19)
        elif self.kind == "ribbon":
            p.setBrush(QBrush(self.color))
            p.drawRoundedRect(7, 4, 14, 19, 3, 3)
            p.setPen(QPen(QColor("#FFFFFF"), 1.5, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(14, 8, 14, 15)
            p.drawLine(10.5, 11.5, 17.5, 11.5)
        elif self.kind == "target":
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(3, 3, 22, 22)
            p.drawEllipse(8, 8, 12, 12)
            p.setBrush(QBrush(self.color))
            p.drawEllipse(12, 12, 4, 4)
        elif self.kind == "book":
            p.setBrush(QBrush(self.color))
            p.drawRoundedRect(4, 5, 9, 18, 2, 2)
            p.drawRoundedRect(15, 5, 9, 18, 2, 2)
            p.setPen(QPen(QColor("#FFFFFF"), 1.2))
            p.drawLine(14, 6, 14, 23)
        else:  # bars
            p.setBrush(QBrush(self.color))
            p.drawRoundedRect(4, 16, 4, 8, 2, 2)
            p.drawRoundedRect(11, 10, 4, 14, 2, 2)
            p.drawRoundedRect(18, 4, 4, 20, 2, 2)
        p.end()


class _HeroCard(QFrame):
    """覆盖整张 Hero 卡的冰蓝流体背景。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wave_phase = 0.0

    def set_wave_phase(self, phase):
        self._wave_phase = float(phase)
        self.update()

    def paintEvent(self, event):
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if w < 2 or h < 2:
            p.end()
            return

        card = QPainterPath()
        card.addRoundedRect(0.5, 0.5, w - 1, h - 1, 20, 20)
        p.setClipPath(card)

        dark = _is_dark_theme()
        base = QLinearGradient(0, 0, w, h)
        if dark:
            base.setColorAt(0.0, QColor(25, 39, 60, 250))
            base.setColorAt(0.52, QColor(27, 48, 72, 245))
            base.setColorAt(1.0, QColor(28, 61, 87, 245))
        else:
            base.setColorAt(0.0, QColor(255, 255, 255, 245))
            base.setColorAt(0.52, QColor(246, 252, 255, 238))
            base.setColorAt(1.0, QColor(226, 244, 255, 238))
        p.fillPath(card, QBrush(base))

        # 波纹覆盖整张卡，但把左侧文字区留在更干净的白色层次中。
        phase = self._wave_phase

        def wy(base, amplitude=0.012, shift=0.0):
            return h * (base + amplitude * math.sin(phase * 0.78 + shift))

        def wave(start_y, points, color, phase_offset=0.0, amplitude=0.045):
            def traveling_y(x, y):
                return y + h * amplitude * math.sin(
                    (x / max(1, w)) * math.tau - phase + phase_offset
                )

            path = QPainterPath()
            path.moveTo(-24, traveling_y(-24, wy(start_y, 0.010)))
            for x, y, cx1, cy1, cx2, cy2 in points:
                path.cubicTo(
                    cx1, traveling_y(cx1, cy1),
                    cx2, traveling_y(cx2, cy2),
                    x, traveling_y(x, y),
                )
            path.lineTo(w + 24, h + 24)
            path.lineTo(-24, h + 24)
            path.closeSubpath()
            p.fillPath(path, QColor(*color))

        wave(0.73, [
            (w * 0.22, wy(0.55, 0.016), w * 0.06, wy(0.70, 0.014), w * 0.11, wy(0.82, 0.014, 0.4)),
            (w * 0.52, wy(0.74, 0.016, 0.8), w * 0.36, wy(0.38, 0.018, 0.3), w * 0.42, wy(0.90, 0.014, 0.5)),
            (w * 0.78, wy(0.27, 0.014, 1.2), w * 0.62, wy(0.62, 0.016, 0.6), w * 0.70, wy(0.28, 0.014, 0.9)),
            (w + 24, wy(0.10, 0.010, 1.5), w * 0.92, wy(0.04, 0.010, 1.0), w * 1.02, wy(0.15, 0.012, 1.3)),
        ], (55, 143, 207, 54) if dark else (164, 215, 248, 58), 0.0, 0.052)
        wave(0.82, [
            (w * 0.30, wy(0.70, 0.018, 1.0), w * 0.10, wy(0.80, 0.014, 0.6), w * 0.19, wy(0.95, 0.016, 0.8)),
            (w * 0.60, wy(0.84, 0.016, 1.6), w * 0.42, wy(0.56, 0.018, 1.0), w * 0.50, wy(0.98, 0.014, 1.3)),
            (w * 0.84, wy(0.43, 0.014, 2.1), w * 0.70, wy(0.75, 0.016, 1.7), w * 0.77, wy(0.42, 0.014, 1.9)),
            (w + 24, wy(0.22, 0.012, 2.4), w * 0.95, wy(0.13, 0.010, 2.0), w * 1.04, wy(0.26, 0.012, 2.2)),
        ], (31, 124, 170, 42) if dark else (83, 199, 229, 28), 1.4, 0.038)
        wave(0.64, [
            (w * 0.44, wy(0.62, 0.014, 2.4), w * 0.22, wy(0.32, 0.014, 2.0), w * 0.32, wy(0.80, 0.016, 2.2)),
            (w * 0.70, wy(0.34, 0.014, 2.9), w * 0.55, wy(0.55, 0.016, 2.5), w * 0.61, wy(0.32, 0.014, 2.7)),
            (w + 24, wy(0.18, 0.010, 3.2), w * 0.84, wy(0.05, 0.010, 2.8), w * 0.95, wy(0.20, 0.012, 3.0)),
        ], (145, 207, 242, 58) if dark else (255, 255, 255, 132), 2.5, 0.030)

        # 参考稿中的细白边让波纹显得轻，而不是一块突兀的色块。
        p.setPen(QPen(QColor(142, 207, 245, 135) if dark else QColor(255, 255, 255, 178), 1.4))
        line = QPainterPath()
        line.moveTo(-16, wy(0.73, 0.010))
        line.cubicTo(w * 0.06, wy(0.68, 0.012), w * 0.15, wy(0.82, 0.014), w * 0.23, wy(0.55, 0.012))
        line.cubicTo(w * 0.38, wy(0.34, 0.014), w * 0.46, wy(0.83, 0.014), w * 0.62, wy(0.34, 0.012))
        line.cubicTo(w * 0.76, wy(0.08, 0.010), w * 0.89, wy(0.23, 0.012), w + 16, wy(0.09, 0.010))
        p.drawPath(line)
        p.setClipping(False)
        p.setPen(QPen(QColor(166, 220, 248, 150) if dark else QColor(255, 255, 255, 220), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(0.5, 0.5, w - 1, h - 1, 20, 20)
        p.end()


# ─── Version & Update Check ────────────────────────────────────────


def _get_version():
    """获取版本号：环境变量 > VERSION文件 > 默认"""
    # 构建时通过环境变量注入
    v = os.environ.get("MOISTEN_VERSION", "")
    if v:
        return v.lstrip("v")
    # 从VERSION文件读取
    vf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    if os.path.exists(vf):
        with open(vf, encoding="utf-8") as f:
            return f.read().strip().lstrip("v")
    return "dev"

CURRENT_VERSION = _get_version()
DOWNLOAD_URL = "https://signxer.github.io/Moisten/"
# 考试未通过时询问是否重考的倒计时（秒）；倒计时内不操作 = 不重考
EXAM_RETRY_TIMEOUT = 30


RELEASES_JSON_URL = "https://raw.githubusercontent.com/signxer/Moisten/main/releases.json"
REPO_NAME = "signxer/silent-rain"

# GitHub 加速代理（前缀拼接即可加速公开资源，见 https://gh-proxy.com/docs/github-accelerator）。
# 国内直连 github.com / raw.githubusercontent.com 经常超时，因此默认走加速节点，不通再退回直连。
GH_PROXY_PREFIXES = ("https://gh-proxy.com/", "https://gh-proxy.org/")
_GITHUB_HOSTS = ("https://github.com/", "https://raw.githubusercontent.com/",
                 "https://api.github.com/", "https://objects.githubusercontent.com/")


def with_gh_proxies(url: str) -> list:
    """返回该 URL 的候选地址：加速节点优先，GitHub 直连兜底。

    非 GitHub 资源（如 GitHub Pages）不加代理，原样返回。
    """
    if not url or not url.startswith(_GITHUB_HOSTS):
        return [url]
    return [p + url for p in GH_PROXY_PREFIXES] + [url]


def _probe_download_url(url: str, timeout: float = 8.0) -> bool:
    """轻量探测下载源是否可达：拿到任何 HTTP 响应都算通，只有网络错误算不通。

    死节点上直接丢一个大文件下载会卡满超时，所以先用 HEAD 探一下。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Moisten"}, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True  # 有响应（例如 405 不支持 HEAD）说明链路是通的
    except Exception:
        return False


def _download_source_label(url: str) -> str:
    """给下载源起个用户能看懂的名字（进度框里显示）"""
    for prefix in GH_PROXY_PREFIXES:
        if url.startswith(prefix):
            return f"加速节点 {prefix.split('//', 1)[-1].strip('/')}"
    return "GitHub 直连"


def update_download_candidates(url: str, probe=None) -> list:
    """下载候选顺序：可达的加速节点 → 可达的直连 → 其余（保证顺序里仍有兜底）。"""
    probe = probe or _probe_download_url
    ordered = with_gh_proxies(url)
    reachable = [u for u in ordered if probe(u)]
    return reachable + [u for u in ordered if u not in reachable]


def _ver_tuple(v):
    """版本号转数字元组用于比较（1.10.0 > 1.7.0）"""
    parts = []
    for p in str(v).replace("-", ".").split("."):
        parts.append(int(p) if p.isdigit() else 0)
    return tuple(parts)


def _looks_like_executable(path) -> bool:
    """粗略校验下载文件是否为可执行文件（PE/Mach-O 魔数）。

    用于拦截 gh-proxy 等代理返回的错误页/截断文件被当作安装包
    替换掉正在运行的程序（否则重启时报 PyInstaller
    "Failed to start python interpreter"）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if sys.platform == "win32":
            return head[:2] == b"MZ"  # PE 可执行文件
        # macOS：Mach-O / universal binary 魔数
        return head[:2] == b"MZ" or head in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe")
    except Exception:
        return False


def check_for_update():
    """检查是否有新版本，返回 (最新版本号, 是否需要更新, 更新日志, 下载URL)"""
    data = None
    # 加速节点优先，不通再退回 GitHub 直连
    for url in with_gh_proxies(RELEASES_JSON_URL):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Moisten"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                break
        except:
            continue
    if data:
        latest = data.get("tag", "").lstrip("v")
        notes = data.get("notes", "")
        download_urls = {}
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            fname = asset.get("file", "")
            if fname:
                download_urls[name] = f"https://github.com/{REPO_NAME}/releases/download/{data.get('tag', '')}/{fname}"
        # 数字比较：仅当线上版本严格更新才提示（避免 dev 版本被提示降级）
        if latest and _ver_tuple(latest) > _ver_tuple(CURRENT_VERSION):
            return latest, True, notes, download_urls
    return CURRENT_VERSION, False, "", {}


# ─── Welcome Screen（首启体验）────────────────────────────────────


class WelcomeScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._animate_entrance()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(60, 50, 60, 50)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignCenter)

        self.lbl_icon = IconWidget(FIF.EDUCATION, self)
        self.lbl_icon.setFixedSize(72, 72)
        layout.addWidget(self.lbl_icon, 0, Qt.AlignHCenter)

        self.lbl_title = TitleLabel("润物 Moisten")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_title)

        version = CaptionLabel(f"v{CURRENT_VERSION} · 青黛润物工作台")
        version.setObjectName("statusPill")
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version, 0, Qt.AlignHCenter)

        self.lbl_sub = BodyLabel("网络课程自动学习工具 · 随风潜入夜，润物细无声")
        self.lbl_sub.setObjectName("pageSubtitle")
        self.lbl_sub.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_sub)

        layout.addSpacing(16)

        # 特性卡片
        self._feat_cards = []
        feats = [
            ("自动模式", "按学时目标自动寻找课程学习，支持标签筛选与断点续学"),
            ("手动模式", "粘贴专题班、训练营或课程 URL，精确学习"),
            ("智能稳定", "视频卡住自动刷新重试 · 变更配置即停旧任务 · 学时查询节流"),
        ]
        for title, desc in feats:
            card = HeaderCardWidget(self)
            card.setBorderRadius(10)
            card.setFixedWidth(440)
            card.setTitle(title)
            card.viewLayout.setContentsMargins(20, 8, 20, 12)
            lb = BodyLabel(desc)
            lb.setFixedHeight(28)
            card.viewLayout.addWidget(lb)
            layout.addWidget(card, 0, Qt.AlignHCenter)
            self._feat_cards.append(card)

        layout.addSpacing(16)

        self.btn_start = PrimaryPushButton("  开始使用")
        self.btn_start.setIcon(FIF.RIGHT_ARROW)
        self.btn_start.setFixedSize(200, 40)
        self.btn_start.clicked.connect(lambda: self.window().next_screen())
        layout.addWidget(self.btn_start, 0, Qt.AlignHCenter)

    def _animate_entrance(self):
        """交错入场：图标→标题→副标题→特性卡片→按钮，逐项淡入"""
        widgets = [self.lbl_icon, self.lbl_title, self.lbl_sub,
                   *self._feat_cards, self.btn_start]
        group = QSequentialAnimationGroup(self)
        for i, w in enumerate(widgets):
            effect = QGraphicsOpacityEffect(w)
            w.setGraphicsEffect(effect)
            effect.setOpacity(0.0)
            anim = QPropertyAnimation(effect, b"opacity", w)
            anim.setDuration(260)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(anim)
            if i < len(widgets) - 1:
                group.addAnimation(QPauseAnimation(70, group))
        self._entrance_group = group
        group.start()


# ─── Config Screen ─────────────────────────────────────────────────


class ConfigScreen(QWidget):
    # 后台线程测试 DeepSeek 连接的结果（跨线程 → 主线程，QueuedConnection）
    api_test_signal = Signal(bool, str)

    def __init__(self, parent=None, section: str = "all", sidebar_mode: bool = False):
        super().__init__(parent)
        self.section = section
        self.sidebar_mode = sidebar_mode
        self._load_config()
        self._build_ui()
        self.api_test_signal.connect(self._finish_test_api)

    def _load_config(self):
        self._saved = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self._saved = json.load(f)
            except:
                pass

    def _build_ui(self):
        # 设置项会随功能增加而变多：整页放进滚动区，小窗口下也不会被裁切
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 只让滚动区/容器透明，避免影响卡片自身背景
        scroll.setObjectName("configScroll")
        scroll.setStyleSheet("#configScroll { border: none; background: transparent; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        container = QWidget()
        container.setObjectName("configContent")
        container.setStyleSheet("#configContent { background: transparent; }")
        scroll.setWidget(container)
        root.addWidget(scroll)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        title_map = {
            "all": ("运行配置", "设置工作线程数、浏览器模式和考试自动答题"),
            "runtime": ("运行与浏览器", "调整 worker、浏览器和启动方式"),
            "exam": ("考试设置", "配置训练营考试的自动答题能力"),
            "appearance": ("外观设置", "选择主题和动效偏好"),
        }
        title, subtitle = title_map.get(self.section, title_map["all"])
        layout.addWidget(PageHeader(title, subtitle))
        self.step_bar = StepBar(["配置", "登录", "学习方式", "目标"], active=0)
        self.step_bar.setVisible(not self.sidebar_mode)
        layout.addWidget(self.step_bar)

        layout.addSpacing(10)

        # Workers card
        workers_card = HeaderCardWidget(self)
        workers_card.setTitle("工作线程数")
        workers_card.setBorderRadius(8)
        workers_card.viewLayout.setContentsMargins(24, 8, 24, 16)
        w_layout = QHBoxLayout()
        w_layout.setSpacing(12)
        w_layout.setContentsMargins(0, 0, 0, 0)

        self.spin_workers = SpinBox()
        self.spin_workers.setRange(1, 20)
        self.spin_workers.setValue(self._saved.get("workers", 5))
        self.spin_workers.setFixedWidth(120)
        w_layout.addWidget(self.spin_workers)
        w_layout.addSpacing(4)
        w_layout.addWidget(BodyLabel("个线程"))
        w_layout.addSpacing(16)
        w_hint = CaptionLabel("建议 3-10")
        w_hint.setObjectName("muted")
        w_layout.addWidget(w_hint)
        w_layout.addStretch()
        workers_card.viewLayout.addLayout(w_layout)
        layout.addWidget(workers_card)

        # Browser settings card (headless + engine merged)
        browser_card = HeaderCardWidget(self)
        browser_card.setTitle("浏览器设置")
        browser_card.setBorderRadius(8)
        # 卡片 viewLayout 是 QHBoxLayout：边距只设一次，内层归零（避免叠加）
        browser_card.viewLayout.setContentsMargins(24, 8, 24, 16)
        card_layout = QVBoxLayout()
        card_layout.setSpacing(14)
        card_layout.setContentsMargins(0, 0, 0, 0)

        # 行标签统一宽度，让控件纵向对齐（避免文字挤在一起）
        def _row_label(text):
            lbl = BodyLabel(text)
            lbl.setFixedWidth(92)
            return lbl

        # Row 1: Headless
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        self.switch_headless = SwitchButton()
        self.switch_headless.setChecked(self._saved.get("headless", True))
        self.switch_headless.setOnText("后台运行")
        self.switch_headless.setOffText("显示浏览器")
        row1.addWidget(_row_label("无头模式"))
        row1.addWidget(self.switch_headless)
        row1.addStretch()
        card_layout.addLayout(row1)

        # Row 2: Browser engine
        default_browser = "chrome"  # 默认使用系统 Chrome
        saved_browser = self._saved.get("browser", default_browser)

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        self.switch_browser = SwitchButton()
        self.switch_browser.setChecked(saved_browser == "chrome")
        self.switch_browser.setOnText("本地 Chrome")
        self.switch_browser.setOffText("内置 Chromium")
        row2.addWidget(_row_label("浏览器引擎"))
        row2.addWidget(self.switch_browser)
        row2.addStretch()
        card_layout.addLayout(row2)

        # 内置 Chromium 提示：首次使用自动下载（缩进与行对齐，避免拥挤）
        browser_hint = CaptionLabel("  内置 Chromium 首次使用会自动下载（约200MB）；本地 Chrome 无需下载")
        browser_hint.setObjectName("muted")
        browser_hint.setWordWrap(True)
        card_layout.addWidget(browser_hint)
        card_layout.addSpacing(2)

        # Row 3: Chrome path (only when using local Chrome)
        self.chrome_path_widget = QWidget()
        path_row = QHBoxLayout(self.chrome_path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(8)
        path_row.addWidget(_row_label("Chrome路径"))
        self.input_chrome_path = LineEdit()
        self.input_chrome_path.setPlaceholderText("留空自动检测")
        self.input_chrome_path.setText(self._saved.get("chrome_path", ""))
        self.input_chrome_path.setFixedWidth(250)
        self.input_chrome_path.setFixedHeight(32)  # 显式高度，避免输入框被裁切
        path_row.addWidget(self.input_chrome_path)

        btn_browse = PushButton("浏览")
        btn_browse.setFixedWidth(60)
        btn_browse.setFixedHeight(32)
        btn_browse.clicked.connect(self._browse_chrome)
        path_row.addWidget(btn_browse)
        path_row.addStretch()
        card_layout.addWidget(self.chrome_path_widget)
        self.chrome_path_widget.setVisible(saved_browser == "chrome")

        self.switch_browser.checkedChanged.connect(lambda checked: self.chrome_path_widget.setVisible(checked))

        browser_card.viewLayout.addLayout(card_layout)
        layout.addWidget(browser_card)

        # Exam settings card (DeepSeek 自动答题)
        exam_card = HeaderCardWidget(self)
        exam_card.setTitle("考试自动答题（DeepSeek）")
        exam_card.setBorderRadius(8)
        exam_card.viewLayout.setContentsMargins(24, 8, 24, 16)
        exam_layout = QVBoxLayout()
        exam_layout.setSpacing(14)
        exam_layout.setContentsMargins(0, 0, 0, 0)

        row_exam_on = QHBoxLayout()
        row_exam_on.setSpacing(12)
        self.switch_exam = SwitchButton()
        self.switch_exam.setChecked(bool(self._saved.get("exam_enabled", False)))
        self.switch_exam.setOnText("自动答题")
        self.switch_exam.setOffText("关闭")
        row_exam_on.addWidget(_row_label("训练营考试"))
        row_exam_on.addWidget(self.switch_exam)
        row_exam_on.addStretch()
        exam_layout.addLayout(row_exam_on)

        row_key = QHBoxLayout()
        row_key.setSpacing(12)
        row_key.addWidget(_row_label("API Key"))
        self.input_api_key = PasswordLineEdit()
        self.input_api_key.setPlaceholderText("sk-...  （DeepSeek 开放平台申请）")
        self.input_api_key.setText(deobfuscate_secret(self._saved.get("deepseek_api_key", "")))
        self.input_api_key.setFixedWidth(320)
        self.input_api_key.setFixedHeight(32)
        row_key.addWidget(self.input_api_key)
        row_key.addStretch()
        exam_layout.addLayout(row_key)

        row_model = QHBoxLayout()
        row_model.setSpacing(12)
        row_model.addWidget(_row_label("模型"))
        self.input_model = LineEdit()
        self.input_model.setPlaceholderText(DEEPSEEK_DEFAULT_MODEL)
        self.input_model.setText(self._saved.get("deepseek_model", "") or DEEPSEEK_DEFAULT_MODEL)
        self.input_model.setFixedWidth(180)
        self.input_model.setFixedHeight(32)
        row_model.addWidget(self.input_model)
        row_model.addSpacing(6)
        self.switch_thinking = SwitchButton()
        self.switch_thinking.setChecked(bool(self._saved.get("deepseek_thinking", False)))
        self.switch_thinking.setOnText("深度思考")
        self.switch_thinking.setOffText("快速作答")
        row_model.addWidget(self.switch_thinking)
        row_model.addSpacing(10)
        self.btn_test_api = PushButton("测试连接")
        self.btn_test_api.setFixedWidth(84)
        self.btn_test_api.setFixedHeight(32)
        self.btn_test_api.clicked.connect(self._on_test_api)
        row_model.addWidget(self.btn_test_api)
        row_model.addStretch()
        exam_layout.addLayout(row_model)

        exam_hint = CaptionLabel(
            "  开启后，训练营课程页里的「随堂测试」会用 DeepSeek 自动答题并提交；"
            "考试记录只取一次，未通过会记录日志后继续下一门")
        exam_hint.setObjectName("muted")
        exam_hint.setWordWrap(True)
        exam_layout.addWidget(exam_hint)
        self.lbl_exam_validation = CaptionLabel("")
        self.lbl_exam_validation.setObjectName("muted")
        exam_layout.addWidget(self.lbl_exam_validation)
        self.switch_exam.checkedChanged.connect(self._validate_exam_settings)
        self.input_api_key.textChanged.connect(lambda: self._validate_exam_settings())

        exam_card.viewLayout.addLayout(exam_layout)
        layout.addWidget(exam_card)

        appearance_card = HeaderCardWidget(self)
        appearance_card.setTitle("外观与动效")
        appearance_card.setBorderRadius(10)
        appearance_card.viewLayout.setContentsMargins(24, 8, 24, 16)
        appearance_layout = QVBoxLayout()
        appearance_layout.setContentsMargins(0, 0, 0, 0)
        appearance_layout.setSpacing(12)
        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)
        theme_row.addWidget(BodyLabel("主题"))
        self.combo_theme = QComboBox()
        self.combo_theme.addItem("跟随系统", "auto")
        self.combo_theme.addItem("浅色", "light")
        self.combo_theme.addItem("深色", "dark")
        saved_theme = normalize_theme_mode(self._saved.get("theme_mode", "auto"))
        self.combo_theme.setCurrentIndex(self.combo_theme.findData(saved_theme))
        self.combo_theme.setFixedWidth(150)
        self.combo_theme.currentIndexChanged.connect(self._on_appearance_changed)
        theme_row.addWidget(self.combo_theme)
        theme_row.addStretch()
        appearance_layout.addLayout(theme_row)
        motion_row = QHBoxLayout()
        motion_row.setSpacing(12)
        motion_row.addWidget(BodyLabel("动效"))
        self.switch_reduced_motion = SwitchButton()
        self.switch_reduced_motion.setChecked(bool(self._saved.get("reduced_motion", False)))
        self.switch_reduced_motion.setOnText("减少")
        self.switch_reduced_motion.setOffText("标准")
        self.switch_reduced_motion.checkedChanged.connect(self._on_appearance_changed)
        motion_row.addWidget(self.switch_reduced_motion)
        motion_hint = CaptionLabel("减少页面切换和进度动画")
        motion_hint.setObjectName("muted")
        motion_row.addWidget(motion_hint)
        motion_row.addStretch()
        appearance_layout.addLayout(motion_row)
        appearance_card.viewLayout.addLayout(appearance_layout)
        layout.addWidget(appearance_card)

        self._section_widgets = {
            "runtime": (workers_card, browser_card),
            "exam": (exam_card,),
            "appearance": (appearance_card,),
        }
        if self.sidebar_mode and self.section in self._section_widgets:
            visible = set(self._section_widgets[self.section])
            for card in (workers_card, browser_card, exam_card, appearance_card):
                card.setVisible(card in visible)

        layout.addStretch()

        # 首次向导仍保留“开始/继续”；侧栏外观设置采用控件变更即保存，
        # 不再放置一个会让用户误以为需要提交的底部按钮。
        self.btn_start = None
        if not (self.sidebar_mode and self.section == "appearance"):
            self.btn_start = PrimaryPushButton("  保存并返回" if self.sidebar_mode else "  开始")
            self.btn_start.setIcon(FIF.PLAY)
            self.btn_start.setFixedSize(160, 40)
            self.btn_start.clicked.connect(self._on_start)
            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            btn_layout.addWidget(self.btn_start)
            btn_layout.addStretch()
            layout.addLayout(btn_layout)

    def _validate_exam_settings(self):
        if self.switch_exam.isChecked() and not self.input_api_key.text().strip():
            self.lbl_exam_validation.setText("开启自动答题后需要填写 DeepSeek API Key；留空则学习时跳过考试。")
        else:
            self.lbl_exam_validation.setText("")

    def _on_appearance_changed(self, *_args):
        """外观侧栏即时保存并立即预览，不触发学习任务重启。"""
        if not hasattr(self, "combo_theme") or not hasattr(self, "switch_reduced_motion"):
            return
        mode = normalize_theme_mode(self.combo_theme.currentData() or "auto")
        reduced_motion = self.switch_reduced_motion.isChecked()
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["theme_mode"] = mode
            cfg["reduced_motion"] = reduced_motion
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            # 主题预览仍然生效；下次进入页面会从现有配置重新读取。
            pass
        win = self.window()
        win.cfg_theme_mode = mode
        win.cfg_reduced_motion = reduced_motion
        dashboard = getattr(win, "screen_dashboard", None)
        if dashboard is not None and hasattr(dashboard, "set_motion_enabled"):
            dashboard.set_motion_enabled(not reduced_motion)
        apply_theme(QApplication.instance(), mode)

    def _browse_chrome(self):
        from PySide6.QtWidgets import QFileDialog
        import platform
        if platform.system() == "Windows":
            default_dir = r"C:\Program Files\Google\Chrome\Application"
        elif platform.system() == "Darwin":
            default_dir = "/Applications/Google Chrome.app/Contents/MacOS"
        else:
            default_dir = "/usr/bin"
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Chrome 可执行文件", default_dir,
            "Chrome (chrome*);;所有文件 (*)"
        )
        if path:
            self.input_chrome_path.setText(path)

    def _on_test_api(self):
        """后台线程测试 DeepSeek API Key 是否可用"""
        api_key = self.input_api_key.text().strip()
        model = self.input_model.text().strip() or DEEPSEEK_DEFAULT_MODEL
        thinking = self.switch_thinking.isChecked()
        if not api_key:
            InfoBar.warning("提示", "请先填写 DeepSeek API Key", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            return

        self.btn_test_api.setEnabled(False)
        self.btn_test_api.setText("测试中")

        def _work():
            message = ""
            ok = False
            try:
                client = DeepSeekClient(api_key=api_key, model=model,
                                        base_url=DEEPSEEK_DEFAULT_BASE_URL,
                                        thinking=thinking, timeout=60)
                loop = asyncio.new_event_loop()
                try:
                    # 预算不能太小：思考模式下思维链也计入 max_tokens，
                    # 太小会出现「只推理、没答案」而被误判为 Key 不可用
                    reply = loop.run_until_complete(client.chat(
                        [{"role": "user", "content": "只回复两个字：正常"}],
                        json_mode=False, max_tokens=512, retries=1))
                    ok = True
                    mode = "深度思考" if thinking else "快速作答"
                    message = f"模型 {client.model}（{mode}）响应正常：{reply.strip()[:20]}"
                finally:
                    loop.close()
            except Exception as e:
                message = str(e)[:180]
            self.api_test_signal.emit(ok, message)

        threading.Thread(target=_work, daemon=True).start()

    def _finish_test_api(self, ok, message):
        self.btn_test_api.setEnabled(True)
        self.btn_test_api.setText("测试连接")
        if ok:
            InfoBar.success("连接成功", message, parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
        else:
            InfoBar.error("连接失败", message, parent=self,
                          position=InfoBarPosition.TOP, duration=6000)

    def _on_start(self):
        workers = self.spin_workers.value()
        headless = self.switch_headless.isChecked()
        browser = "chrome" if self.switch_browser.isChecked() else "chromium"
        chrome_path = self.input_chrome_path.text().strip() if self.switch_browser.isChecked() else ""
        exam_enabled = self.switch_exam.isChecked()
        api_key = self.input_api_key.text().strip()
        model = self.input_model.text().strip() or DEEPSEEK_DEFAULT_MODEL
        thinking = self.switch_thinking.isChecked()
        if exam_enabled and not api_key:
            InfoBar.warning("提示", "已开启考试自动答题，但未填写 DeepSeek API Key", parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["workers"] = workers
            cfg["headless"] = headless
            cfg["browser"] = browser
            cfg["chrome_path"] = chrome_path
            cfg["exam_enabled"] = exam_enabled
            cfg["deepseek_model"] = model
            cfg["deepseek_thinking"] = thinking
            cfg["deepseek_base_url"] = DEEPSEEK_DEFAULT_BASE_URL
            cfg["theme_mode"] = self.combo_theme.currentData() or "auto"
            cfg["reduced_motion"] = self.switch_reduced_motion.isChecked()
            if api_key:
                cfg["deepseek_api_key"] = obfuscate_secret(api_key)
            elif not exam_enabled:
                cfg.pop("deepseek_api_key", None)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        win = self.window()
        win.cfg_workers = workers
        win.cfg_headless = headless
        win.cfg_browser = browser
        win.cfg_chrome_path = chrome_path
        win.cfg_exam_enabled = exam_enabled
        win.cfg_deepseek_api_key = api_key
        win.cfg_deepseek_model = model
        win.cfg_deepseek_thinking = thinking
        win.cfg_theme_mode = self.combo_theme.currentData() or "auto"
        win.cfg_reduced_motion = self.switch_reduced_motion.isChecked()
        apply_theme(QApplication.instance(), win.cfg_theme_mode)
        if self.sidebar_mode or getattr(win, "_settings_mode", False):
            win.return_to_dashboard(restart=True)
            return
        win.next_screen()


# ─── Login Screen ──────────────────────────────────────────────────


class LoginScreen(QWidget):
    def __init__(self, parent=None, sidebar_mode: bool = False):
        super().__init__(parent)
        self.sidebar_mode = sidebar_mode
        self._load_creds()
        self._build_ui()

    def _load_creds(self):
        path = USER_CREDENTIALS_PATH
        self._creds = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._creds = json.load(f)
                # 密码：优先系统钥匙串，回退旧 XOR 字段
                username = self._creds.get("username", "")
                if username:
                    try:
                        from main import AutoLearner
                        kp = AutoLearner._load_password(username)
                        if kp:
                            self._creds["password"] = kp
                            return
                    except:
                        pass
                if self._creds.get("password"):
                    try:
                        from main import AutoLearner
                        self._creds["password"] = AutoLearner._xor_decrypt(self._creds["password"])
                    except:
                        pass  # 兼容旧的明文密码
            except:
                pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        title = "账号登录" if self.sidebar_mode else "用户登录"
        subtitle = "管理账号、登录方式和当前会话" if self.sidebar_mode else "输入统一认证账号密码"
        layout.addWidget(PageHeader(title, subtitle))
        self.step_bar = StepBar(["配置", "登录", "学习方式", "目标"], active=1)
        self.step_bar.setVisible(not self.sidebar_mode)
        layout.addWidget(self.step_bar)

        layout.addSpacing(10)

        # Account card
        account_card = HeaderCardWidget(self)
        account_card.setTitle("账号信息")
        account_card.setBorderRadius(8)
        a_layout = QFormLayout()
        a_layout.setSpacing(16)
        a_layout.setContentsMargins(0, 8, 0, 8)

        self.input_user = LineEdit()
        self.input_user.setText(self._creds.get("username", ""))
        self.input_user.setPlaceholderText("请输入账号")
        a_layout.addRow("账号", self.input_user)

        self.input_pass = LineEdit()
        self.input_pass.setText(self._creds.get("password", ""))
        self.input_pass.setPlaceholderText("请输入密码")
        self.input_pass.setEchoMode(LineEdit.Password)
        a_layout.addRow("密码", self.input_pass)

        account_card.viewLayout.addLayout(a_layout)
        layout.addWidget(account_card)

        # Mode card
        mode_card = HeaderCardWidget(self)
        mode_card.setTitle("登录方式")
        mode_card.setBorderRadius(8)
        m_layout = QHBoxLayout()
        m_layout.setSpacing(20)
        m_layout.setContentsMargins(0, 8, 0, 8)

        self.radio_auto = RadioButton("自动登录")
        self.radio_manual = RadioButton("手动登录")
        self.radio_auto.setChecked(True)
        m_layout.addWidget(self.radio_auto)
        m_layout.addWidget(self.radio_manual)
        m_layout.addStretch()
        mode_card.viewLayout.addLayout(m_layout)
        layout.addWidget(mode_card)

        # Status
        self.lbl_status = BodyLabel("")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_status)

        layout.addStretch()

        # Button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_login = PrimaryPushButton("  保存并重新登录" if self.sidebar_mode else "  登录")
        self.btn_login.setIcon(FIF.PEOPLE)
        self.btn_login.setFixedSize(160, 40)
        self.btn_login.clicked.connect(self._on_login)
        btn_layout.addWidget(self.btn_login)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.input_pass.returnPressed.connect(self._on_login)

    def _on_login(self):
        username = self.input_user.text().strip()
        password = self.input_pass.text()
        auto = self.radio_auto.isChecked()

        if not username:
            InfoBar.warning("提示", "请输入账号", parent=self, position=InfoBarPosition.TOP)
            return
        if auto and not password:
            InfoBar.warning("提示", "自动登录需要输入密码", parent=self, position=InfoBarPosition.TOP)
            self.input_pass.setFocus()
            return

        try:
            from main import AutoLearner
            AutoLearner().save_user_credentials(username, password)
        except:
            pass

        win = self.window()
        win.cfg_username = username
        win.cfg_password = password
        win.cfg_auto_login = auto
        self.lbl_status.setText("账号已保存，正在重新启动学习…" if self.sidebar_mode else "账号已保存，正在进入学习设置…")
        if self.sidebar_mode:
            win.return_to_dashboard(restart=True)
        else:
            win.next_screen()


# ─── Goal Screen ───────────────────────────────────────────────────


class GoalScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_goal()
        self._build_ui()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "step_bar"):
            self.step_bar.setVisible(not getattr(self.window(), "_settings_mode", False))
        # 模式互斥切换会清空另一模式的目标配置，进入时重新读取并刷新控件，
        # 避免显示切换前的旧值（A14）
        self._load_goal()
        try:
            self.switch_central.setChecked(self._saved_central_on)
            self.spin_central.setValue(int(self._saved_central))
            self.switch_online.setChecked(self._saved_online_on)
            self.spin_online.setValue(int(self._saved_online))
            self.radio_central_remain.setChecked(self._saved_central_mode == "remain")
            self.radio_central_target.setChecked(self._saved_central_mode != "remain")
            self.radio_online_remain.setChecked(self._saved_online_mode == "remain")
            self.radio_online_target.setChecked(self._saved_online_mode != "remain")
            self.central_goal_widget.setVisible(self._saved_central_on)
            self.online_goal_widget.setVisible(self._saved_online_on)
        except Exception:
            pass
        if hasattr(self, "btn_next"):
            self.btn_next.setText("  保存并返回" if getattr(self.window(), "_settings_mode", False) else "  继续")

    def _load_goal(self):
        self._saved_central = 0
        self._saved_online = 0
        self._saved_central_on = False
        self._saved_online_on = False
        self._saved_central_mode = "target"  # target/remain
        self._saved_online_mode = "target"
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self._saved_central = cfg.get("central_goal", 0)
                self._saved_online = cfg.get("online_goal", 0)
                self._saved_central_mode = cfg.get("central_mode", "target")
                self._saved_online_mode = cfg.get("online_mode", "target")
                self._saved_central_on = self._saved_central > 0
                self._saved_online_on = self._saved_online > 0
                # 向后兼容旧格式
                if cfg.get("study_goal", 0) > 0:
                    old_goal = cfg["study_goal"]
                    if cfg.get("goal_type") == "central":
                        self._saved_central = old_goal
                        self._saved_central_on = True
                    else:
                        self._saved_online = old_goal
                        self._saved_online_on = True
            except:
                pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        layout.addWidget(PageHeader("学习目标", "分别设置集中培训和网络自学的学习目标"))
        self.step_bar = StepBar(["配置", "登录", "学习方式", "目标"], active=3)
        layout.addWidget(self.step_bar)

        layout.addSpacing(10)

        # 集中培训卡片
        central_card = HeaderCardWidget(self)
        central_card.setTitle("集中培训")
        central_card.setBorderRadius(8)
        central_card.viewLayout.setContentsMargins(24, 8, 24, 16)
        c_layout = QVBoxLayout()
        c_layout.setSpacing(10)
        c_layout.setContentsMargins(0, 0, 0, 0)

        c_switch_row = QHBoxLayout()
        c_switch_row.addWidget(BodyLabel("是否学习:"))
        self.switch_central = SwitchButton()
        self.switch_central.setChecked(self._saved_central_on)
        self.switch_central.setOnText("学习")
        self.switch_central.setOffText("不学习")
        c_switch_row.addWidget(self.switch_central)
        c_switch_row.addStretch()
        c_layout.addLayout(c_switch_row)

        self.central_goal_widget = QWidget()
        central_goal_layout = QVBoxLayout(self.central_goal_widget)
        central_goal_layout.setContentsMargins(0, 0, 0, 0)
        central_goal_layout.setSpacing(8)

        # 模式选择
        c_mode_row = QHBoxLayout()
        self.radio_central_target = RadioButton("总学时")
        self.radio_central_remain = RadioButton("差额补修")
        if self._saved_central_mode == "remain":
            self.radio_central_remain.setChecked(True)
        else:
            self.radio_central_target.setChecked(True)
        c_mode_row.addWidget(self.radio_central_target)
        c_mode_row.addWidget(self.radio_central_remain)
        c_mode_row.addStretch()
        central_goal_layout.addLayout(c_mode_row)

        # 学时输入
        c_hours_row = QHBoxLayout()
        self.lbl_central_prefix = BodyLabel("目标总学时:" if self._saved_central_mode != "remain" else "差额学时:")
        c_hours_row.addWidget(self.lbl_central_prefix)
        self.spin_central = SpinBox()
        self.spin_central.setRange(0, 9999)
        self.spin_central.setValue(int(self._saved_central))
        self.spin_central.setFixedWidth(150)
        c_hours_row.addWidget(self.spin_central)
        c_hours_row.addWidget(BodyLabel("学时"))
        c_hours_row.addStretch()
        central_goal_layout.addLayout(c_hours_row)

        # 切换模式时更新标签
        self.radio_central_target.toggled.connect(
            lambda checked: self.lbl_central_prefix.setText("目标总学时:" if checked else "差额学时:"))
        self.radio_central_remain.toggled.connect(
            lambda checked: self.lbl_central_prefix.setText("差额学时:" if checked else "目标总学时:"))

        c_layout.addWidget(self.central_goal_widget)

        central_card.viewLayout.addLayout(c_layout)
        layout.addWidget(central_card)

        # 网络自学卡片
        online_card = HeaderCardWidget(self)
        online_card.setTitle("网络自学")
        online_card.setBorderRadius(8)
        online_card.viewLayout.setContentsMargins(24, 8, 24, 16)
        o_layout = QVBoxLayout()
        o_layout.setSpacing(10)
        o_layout.setContentsMargins(0, 0, 0, 0)

        o_switch_row = QHBoxLayout()
        o_switch_row.addWidget(BodyLabel("是否学习:"))
        self.switch_online = SwitchButton()
        self.switch_online.setChecked(self._saved_online_on)
        self.switch_online.setOnText("学习")
        self.switch_online.setOffText("不学习")
        o_switch_row.addWidget(self.switch_online)
        o_switch_row.addStretch()
        o_layout.addLayout(o_switch_row)

        self.online_goal_widget = QWidget()
        online_goal_layout = QVBoxLayout(self.online_goal_widget)
        online_goal_layout.setContentsMargins(0, 0, 0, 0)
        online_goal_layout.setSpacing(8)

        # 模式选择
        o_mode_row = QHBoxLayout()
        self.radio_online_target = RadioButton("总学时")
        self.radio_online_remain = RadioButton("差额补修")
        if self._saved_online_mode == "remain":
            self.radio_online_remain.setChecked(True)
        else:
            self.radio_online_target.setChecked(True)
        o_mode_row.addWidget(self.radio_online_target)
        o_mode_row.addWidget(self.radio_online_remain)
        o_mode_row.addStretch()
        online_goal_layout.addLayout(o_mode_row)

        # 学时输入
        o_hours_row = QHBoxLayout()
        self.lbl_online_prefix = BodyLabel("目标总学时:" if self._saved_online_mode != "remain" else "差额学时:")
        o_hours_row.addWidget(self.lbl_online_prefix)
        self.spin_online = SpinBox()
        self.spin_online.setRange(0, 9999)
        self.spin_online.setValue(int(self._saved_online))
        self.spin_online.setFixedWidth(150)
        o_hours_row.addWidget(self.spin_online)
        o_hours_row.addWidget(BodyLabel("学时"))
        o_hours_row.addStretch()
        online_goal_layout.addLayout(o_hours_row)

        # 切换模式时更新标签
        self.radio_online_target.toggled.connect(
            lambda checked: self.lbl_online_prefix.setText("目标总学时:" if checked else "差额学时:"))
        self.radio_online_remain.toggled.connect(
            lambda checked: self.lbl_online_prefix.setText("差额学时:" if checked else "目标总学时:"))

        o_layout.addWidget(self.online_goal_widget)

        online_card.viewLayout.addLayout(o_layout)
        layout.addWidget(online_card)

        # 切换时显隐学时输入
        self.switch_central.checkedChanged.connect(lambda checked: self.central_goal_widget.setVisible(checked))
        self.switch_online.checkedChanged.connect(lambda checked: self.online_goal_widget.setVisible(checked))
        self.central_goal_widget.setVisible(self._saved_central_on)
        self.online_goal_widget.setVisible(self._saved_online_on)

        self.lbl_goal_summary = CaptionLabel("")
        self.lbl_goal_summary.setObjectName("muted")
        layout.addWidget(self.lbl_goal_summary)
        for control in (self.switch_central, self.switch_online,
                        self.spin_central, self.spin_online,
                        self.radio_central_target, self.radio_central_remain,
                        self.radio_online_target, self.radio_online_remain):
            if hasattr(control, "checkedChanged"):
                control.checkedChanged.connect(lambda *_: self._update_goal_summary())
            elif hasattr(control, "valueChanged"):
                control.valueChanged.connect(lambda *_: self._update_goal_summary())
        self._update_goal_summary()

        layout.addStretch()

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_skip = PushButton("  跳过")
        btn_skip.setIcon(FIF.CLOSE)
        btn_skip.setFixedSize(120, 40)
        btn_skip.clicked.connect(lambda: self._on_done(False, 0, "target", False, 0, "target"))
        btn_layout.addWidget(btn_skip)

        btn_next = PrimaryPushButton("  继续")
        btn_next.setIcon(FIF.RIGHT_ARROW)
        btn_next.setFixedSize(120, 40)
        btn_next.clicked.connect(self._on_next)
        self.btn_next = btn_next
        btn_layout.addWidget(btn_next)

        layout.addLayout(btn_layout)

    def _update_goal_summary(self):
        parts = []
        if self.switch_central.isChecked() and self.spin_central.value() > 0:
            mode = "总学时" if self.radio_central_target.isChecked() else "差额补修"
            parts.append(f"集中培训 · {mode} {self.spin_central.value()} 学时")
        if self.switch_online.isChecked() and self.spin_online.value() > 0:
            mode = "总学时" if self.radio_online_target.isChecked() else "差额补修"
            parts.append(f"网络自学 · {mode} {self.spin_online.value()} 学时")
        self.lbl_goal_summary.setText("当前选择：" + ("；".join(parts) if parts else "未设置学习目标"))

    def _on_next(self):
        central_on = self.switch_central.isChecked()
        online_on = self.switch_online.isChecked()
        central = self.spin_central.value() if central_on else 0
        online = self.spin_online.value() if online_on else 0
        central_mode = "remain" if self.radio_central_remain.isChecked() else "target"
        online_mode = "remain" if self.radio_online_remain.isChecked() else "target"
        self._on_done(central_on, central, central_mode, online_on, online, online_mode)

    def _on_done(self, central_on, central_goal, central_mode, online_on, online_goal, online_mode):
        # 直接保存原始值，差额模式在仪表盘登录后获取学时时再算绝对目标
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["central_goal"] = central_goal if central_on else 0
            cfg["online_goal"] = online_goal if online_on else 0
            cfg["central_mode"] = central_mode if central_on else "target"
            cfg["online_mode"] = online_mode if online_on else "target"
            # 清理旧字段
            cfg.pop("study_goal", None)
            cfg.pop("goal_type", None)
            cfg.pop("goal_mode", None)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        win = self.window()
        win.cfg_central_goal = central_goal if central_on else 0
        win.cfg_online_goal = online_goal if online_on else 0
        win.cfg_central_mode = central_mode if central_on else "target"
        win.cfg_online_mode = online_mode if online_on else "target"
        if getattr(win, "_settings_mode", False):
            win.return_to_dashboard(restart=True)
        else:
            win.next_screen()


# ─── Dashboard Screen ──────────────────────────────────────────────


# ─── Mode Selection Screen ─────────────────────────────────────────


class ModeScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "step_bar"):
            self.step_bar.setVisible(not getattr(self.window(), "_settings_mode", False))

    def _build_ui(self):
        layout = QVBoxLayout(self)
        # 与账号、运行、考试等设置页使用同一套内容边距，避免学习方式页
        # 单独收窄成居中的窄栏。
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        layout.addWidget(PageHeader("选择模式", "选择学习方式，后续可在设置中切换"))
        self.step_bar = StepBar(["配置", "登录", "学习方式", "目标"], active=2)
        layout.addWidget(self.step_bar)

        # 当前模式提示（互斥）
        self.lbl_current = CaptionLabel("")
        self.lbl_current.setObjectName("statusPill")
        layout.addWidget(self.lbl_current)

        layout.addSpacing(24)

        # Auto mode card（viewLayout是QHBoxLayout：边距只设一次，内层归零，
        # 显式行高杜绝裁切；内容贴分割线、按钮留出底边距）
        auto_card = HeaderCardWidget(self)
        auto_card.setTitle("自动模式")
        auto_card.setBorderRadius(10)
        auto_card.setMinimumHeight(150)
        auto_card.viewLayout.setContentsMargins(24, 6, 24, 14)
        a_layout = QVBoxLayout()
        a_layout.setSpacing(6)
        a_layout.setContentsMargins(0, 0, 0, 0)
        a_desc = BodyLabel("自动寻找专题班，按学时目标学习")
        a_desc.setFixedHeight(30)
        a_layout.addWidget(a_desc)
        a_hint = CaptionLabel("适合：需要完成学时目标的日常挂机学习")
        a_hint.setObjectName("muted")
        a_hint.setFixedHeight(24)
        a_layout.addWidget(a_hint)
        a_layout.addSpacing(12)
        btn_auto = PrimaryPushButton("  选择自动模式")
        btn_auto.setIcon(FIF.PLAY)
        btn_auto.setFixedWidth(200)
        btn_auto.setFixedHeight(36)
        btn_auto.clicked.connect(lambda: self._select_mode("auto"))
        a_button_row = QHBoxLayout()
        a_button_row.addStretch()
        a_button_row.addWidget(btn_auto)
        a_button_row.addStretch()
        a_layout.addLayout(a_button_row)
        auto_card.viewLayout.addLayout(a_layout)
        layout.addWidget(auto_card)

        # Manual mode card
        manual_card = HeaderCardWidget(self)
        manual_card.setTitle("手动模式")
        manual_card.setBorderRadius(10)
        manual_card.setMinimumHeight(150)
        manual_card.viewLayout.setContentsMargins(24, 6, 24, 14)
        m_layout = QVBoxLayout()
        m_layout.setSpacing(6)
        m_layout.setContentsMargins(0, 0, 0, 0)
        m_desc = BodyLabel("指定专题班、训练营或课程 URL，精确学习")
        m_desc.setFixedHeight(30)
        m_layout.addWidget(m_desc)
        m_hint = CaptionLabel("适合：学习特定课程、补学指定内容")
        m_hint.setObjectName("muted")
        m_hint.setFixedHeight(24)
        m_layout.addWidget(m_hint)
        m_layout.addSpacing(12)
        btn_manual = PrimaryPushButton("  选择手动模式")
        btn_manual.setIcon(FIF.LINK)
        btn_manual.setFixedWidth(200)
        btn_manual.setFixedHeight(36)
        btn_manual.clicked.connect(lambda: self._select_mode("manual"))
        m_button_row = QHBoxLayout()
        m_button_row.addStretch()
        m_button_row.addWidget(btn_manual)
        m_button_row.addStretch()
        m_layout.addLayout(m_button_row)
        manual_card.viewLayout.addLayout(m_layout)
        layout.addWidget(manual_card)

        layout.addStretch()

    def showEvent(self, event):
        super().showEvent(event)
        win = self.window()
        mode = getattr(win, "cfg_mode", "auto")
        self.lbl_current.setText(
            f"当前模式：{'自动模式' if mode == 'auto' else '手动模式'}（两模式互斥，切换会清空另一种模式的设置）")

    def _select_mode(self, mode):
        win = self.window()
        # 切换会清空另一模式配置（目标学时/URL），先确认，避免误触丢失数据
        if mode == "auto":
            other = "手动模式"
            loss = "手动URL列表"
        else:
            other = "自动模式"
            loss = "集中培训/网络自学的目标学时"
        dlg = Dialog("切换模式",
                     f"切换到{'自动模式' if mode == 'auto' else '手动模式'}将清空{other}的{loss}，确定继续？",
                     win)
        style_moisten_dialog(dlg)
        dlg.cancelButton.setText("取消")
        dlg.yesButton.setText("确定")
        if not dlg.exec():
            return
        win.cfg_mode = mode
        if hasattr(win, "update_learning_navigation"):
            win.update_learning_navigation()
        # 互斥：选择一种模式即清空另一种模式的配置，避免两套设置混在一起
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["mode"] = mode
            if mode == "auto":
                win.cfg_manual_urls = []
                cfg.pop("manual_urls", None)
            else:
                win.cfg_central_goal = 0
                win.cfg_online_goal = 0
                cfg.pop("central_goal", None)
                cfg.pop("online_goal", None)
                cfg.pop("central_mode", None)
                cfg.pop("online_mode", None)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        if mode == "auto":
            win.next_screen()  # → goal → tags → dashboard
        else:
            win.go_to_manual()  # → manual URL input → dashboard


# ─── Manual URL Input Screen ───────────────────────────────────────


class ManualScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "step_bar"):
            self.step_bar.setVisible(not getattr(self.window(), "_settings_mode", False))
        # 每次进入手动页时回填已保存的 URL（重启/重新进入后依然可见可用）
        try:
            win = self.window()
            saved = getattr(win, "cfg_manual_urls", [])
            if saved and not self.text_urls.toPlainText().strip():
                self.text_urls.setPlainText("\n".join(saved))
        except Exception:
            pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignTop)

        layout.addWidget(PageHeader("手动指定课程", "输入专题班、训练营或课程 URL，每行一个"))
        self.step_bar = StepBar(["配置", "登录", "学习方式", "课程"], active=3)
        layout.addWidget(self.step_bar)

        # URL input card
        input_card = HeaderCardWidget(self)
        input_card.setTitle("课程链接")
        input_card.setBorderRadius(8)
        i_layout = QVBoxLayout()
        i_layout.setSpacing(8)
        i_layout.setContentsMargins(0, 8, 0, 8)

        self.text_urls = PlainTextEdit()
        self.text_urls.setPlaceholderText(
            "粘贴URL，每行一个，例如：\n"
            "https://u.ccb.com/workshop/#/myworkshop/detail?id=xxx\n"
            "https://u.ccb.com/workshop/#/detail?id=xxx\n"
            "https://u.ccb.com/trainingcamp/#/traincampdetail/训练营ID/away"
        )
        self.text_urls.setMinimumHeight(200)
        i_layout.addWidget(self.text_urls)

        hint = CaptionLabel("支持专题班、训练营详情页和课程页URL；详情页会自动提取课程")
        hint.setObjectName("muted")
        i_layout.addWidget(hint)

        self.lbl_url_summary = CaptionLabel("等待输入链接")
        self.lbl_url_summary.setObjectName("muted")
        i_layout.addWidget(self.lbl_url_summary)

        input_card.viewLayout.addLayout(i_layout)
        layout.addWidget(input_card)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_back = PushButton("  返回")
        btn_back.setIcon(FIF.RETURN)
        btn_back.setFixedSize(120, 40)
        btn_back.clicked.connect(lambda: self.window().show_mode_screen())
        btn_layout.addWidget(btn_back)

        btn_start = PrimaryPushButton("  开始学习")
        btn_start.setIcon(FIF.PLAY)
        btn_start.setFixedSize(140, 40)
        btn_start.clicked.connect(self._on_start)
        self.btn_start = btn_start
        btn_layout.addWidget(btn_start)

        layout.addLayout(btn_layout)
        self.text_urls.textChanged.connect(self._update_url_summary)
        self._update_url_summary()

    def _parse_urls(self):
        lines = [line.strip() for line in self.text_urls.toPlainText().splitlines() if line.strip()]
        valid = [line for line in lines if "ccb.com" in line and line.startswith(("http://", "https://"))]
        types = {"专题班": 0, "训练营": 0, "课程页": 0, "未知": 0}
        for url in valid:
            if "/trainingcamp/" in url:
                types["训练营"] += 1
            elif "/workshop/" in url:
                types["专题班"] += 1
            elif "/course/" in url:
                types["课程页"] += 1
            else:
                types["未知"] += 1
        return lines, valid, types

    def _update_url_summary(self):
        lines, valid, types = self._parse_urls()
        invalid = len(lines) - len(valid)
        if not lines:
            self.lbl_url_summary.setText("等待输入链接")
            self.btn_start.setEnabled(False)
            return
        parts = [f"已识别 {len(valid)} 个"]
        for name, count in types.items():
            if count:
                parts.append(f"{name} {count}")
        if invalid:
            parts.append(f"无效 {invalid}")
        self.lbl_url_summary.setText(" · ".join(parts))
        self.btn_start.setEnabled(bool(valid))

    def _on_start(self):
        text = self.text_urls.toPlainText().strip()
        if not text:
            InfoBar.warning("提示", "请输入至少一个URL", parent=self, position=InfoBarPosition.TOP)
            return

        urls = self._parse_urls()[1]
        if not urls:
            InfoBar.warning("提示", "未识别到有效的课程URL", parent=self, position=InfoBarPosition.TOP)
            return

        win = self.window()
        win.cfg_manual_urls = urls
        # 持久化手动模式与URL（重启后仍按手动模式继续）
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["mode"] = "manual"
            cfg["manual_urls"] = urls
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        if getattr(win, "_settings_mode", False):
            win.return_to_dashboard(restart=True)
        else:
            win.next_screen()  # → dashboard


# ─── Dashboard Screen ──────────────────────────────────────────────


class DashboardScreen(QWidget):
    update_check_signal = Signal(object)  # (latest, needs_update, notes, download_urls)
    update_check_fail_signal = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tag_event = threading.Event()
        self._exam_retry_event = threading.Event()
        self._exam_retry_choice = False
        self._learn_start_time = None      # 学习开始时间
        self._progress_history = []         # [(timestamp, pct), ...]
        # 每个 worker 独立记录当前课程的进度，用于训练营课程回调未提供
        # eta 时在 GUI 侧计算预计剩余时间。
        self._worker_eta_state = {}
        self._eta_seconds = None            # 最新预估剩余秒数
        self._eta_calc_time = None          # 预估计算时的时间戳
        self._session_start_total = None    # 本次会话起始总学时（用于"本次已学"）
        self._hours_history = []            # 学时趋势点 [(timestamp, total), ...]
        self._runtime_start = None
        self._log_collapsed = False
        self._unread_logs = 0
        self._wave_phase = 0.0
        self._progress_animations = {}
        # 实时倒计时定时器
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._tick_eta)
        self._runtime_timer = QTimer(self)
        self._runtime_timer.setInterval(1000)
        self._runtime_timer.timeout.connect(self._update_runtime)
        self._wave_timer = QTimer(self)
        self._wave_timer.setInterval(50)
        self._wave_timer.timeout.connect(self._advance_wave)
        self.update_check_signal.connect(self._on_update_check_result)
        self.update_check_fail_signal.connect(self._on_update_check_fail)
        self._build_ui()

    def showEvent(self, event):
        super().showEvent(event)
        self.set_motion_enabled(not getattr(self.window(), "cfg_reduced_motion", False))

    def hideEvent(self, event):
        self._wave_timer.stop()
        super().hideEvent(event)

    def set_motion_enabled(self, enabled):
        """切换背景流体动效；减少动效时保留静态背景和进度反馈。"""
        enabled = bool(enabled)
        if enabled and self.isVisible():
            if not self._wave_timer.isActive():
                self._wave_timer.start()
        else:
            self._wave_timer.stop()
        self.update()

    def _advance_wave(self):
        if getattr(self.window(), "cfg_reduced_motion", False):
            self._wave_timer.stop()
            return
        # 50ms tick + this angular step gives a calm ~6s loop. The previous
        # smaller step changed the artwork too slowly to read as moving water.
        self._wave_phase = (self._wave_phase + 0.052) % (math.pi * 2)
        self.update()
        hero = getattr(self, "current_card", None)
        if hero is not None and hasattr(hero, "set_wave_phase"):
            hero.set_wave_phase(self._wave_phase * 0.72)

    def paintEvent(self, event):
        """绘制参考稿中的冰蓝底色、柔光和水墨波纹。"""
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dark = _is_dark_theme()
        base = QLinearGradient(0, 0, w, h)
        if dark:
            base.setColorAt(0.0, QColor("#111827"))
            base.setColorAt(0.48, QColor("#152439"))
            base.setColorAt(1.0, QColor("#162D43"))
        else:
            base.setColorAt(0.0, QColor("#F8FCFF"))
            base.setColorAt(0.48, QColor("#EEF8FD"))
            base.setColorAt(1.0, QColor("#E8F4FA"))
        p.fillRect(self.rect(), QBrush(base))

        # 顶部的蓝色冷光与右侧青色光晕
        glow = QRadialGradient(w * 0.43, h * 0.13, max(w, h) * 0.42)
        glow.setColorAt(0.0, QColor(53, 126, 205, 76) if dark else QColor(184, 225, 255, 118))
        glow.setColorAt(0.7, QColor(65, 145, 214, 20) if dark else QColor(220, 244, 255, 28))
        glow.setColorAt(1.0, QColor(65, 145, 214, 0) if dark else QColor(220, 244, 255, 0))
        p.fillRect(self.rect(), QBrush(glow))
        teal = QRadialGradient(w * 0.83, h * 0.36, max(w, h) * 0.27)
        teal.setColorAt(0.0, QColor(40, 170, 178, 34) if dark else QColor(144, 231, 222, 52))
        teal.setColorAt(1.0, QColor(40, 170, 178, 0) if dark else QColor(144, 231, 222, 0))
        p.fillRect(self.rect(), QBrush(teal))

        phase = self._wave_phase

        def wy(base, amplitude=0.014, shift=0.0):
            return h * (base + amplitude * math.sin(phase * 0.62 + shift))

        def wave(points, color, phase_offset=0.0, amplitude=0.025):
            def traveling_y(x, y):
                return y + h * amplitude * math.sin(
                    (x / max(1, w)) * math.tau - phase + phase_offset
                )

            path = QPainterPath()
            path.moveTo(0, traveling_y(0, points[0][1]))
            for x, y, cx1, cy1, cx2, cy2 in points[1:]:
                path.cubicTo(
                    cx1, traveling_y(cx1, cy1),
                    cx2, traveling_y(cx2, cy2),
                    x, traveling_y(x, y),
                )
            path.lineTo(w, h)
            path.lineTo(0, h)
            path.closeSubpath()
            p.fillPath(path, QColor(*color))

        wave([
            (0, wy(0.74, 0.012)),
            (w * 0.24, wy(0.67, 0.016), w * 0.07, wy(0.67, 0.014), w * 0.14, wy(0.82, 0.014, 0.4)),
            (w * 0.48, wy(0.78, 0.016, 0.8), w * 0.34, wy(0.61, 0.014, 0.3), w * 0.41, wy(0.84, 0.016, 0.5)),
            (w, wy(0.64, 0.014, 1.2), w * 0.74, wy(0.72, 0.014, 0.6), w * 0.86, wy(0.56, 0.016, 0.9)),
        ], (44, 106, 157, 48) if dark else (169, 219, 247, 54), 0.0, 0.026)
        wave([
            (0, wy(0.83, 0.014, 1.0)),
            (w * 0.25, wy(0.76, 0.016, 1.4), w * 0.10, wy(0.78, 0.014, 1.0), w * 0.16, wy(0.92, 0.016, 1.2)),
            (w * 0.58, wy(0.84, 0.016, 1.8), w * 0.36, wy(0.65, 0.014, 1.4), w * 0.47, wy(0.92, 0.016, 1.6)),
            (w, wy(0.73, 0.014, 2.2), w * 0.76, wy(0.72, 0.014, 1.8), w * 0.89, wy(0.64, 0.016, 2.0)),
        ], (36, 91, 140, 32) if dark else (117, 190, 238, 35), 1.2, 0.021)
        # 左下角的纸张颗粒与小点，模拟参考稿的手绘留白。
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(94, 157, 211, 35) if dark else QColor(92, 165, 222, 42))
        for x, y, r in ((26, h - 78, 2), (42, h - 56, 1), (56, h - 101, 2),
                        (72, h - 68, 1), (96, h - 42, 2), (118, h - 82, 1)):
            p.drawEllipse(x, y, r * 2, r * 2)
        p.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_slogan()

    def _place_slogan(self):
        """让 slogan 固定贴在工具栏上方，避免被垂直布局越推越远。"""
        slogan = getattr(self, "_slogan", None)
        toolbar = getattr(self, "_toolbar", None)
        button = getattr(self, "btn_stop", None)
        if slogan is None or toolbar is None or button is None:
            return
        toolbar_rect = toolbar.geometry()
        button_top = toolbar_rect.top() + button.geometry().top()
        # 以工作区右侧内容边界为锚点，和最右侧文档图标保持同一条右边界。
        x = self.width() - 28 - slogan.width()
        y = button_top - slogan.height() - 6
        slogan.move(max(0, x), max(0, y))
        slogan.raise_()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        # 背景仍由 DashboardScreen 铺满工作区，内容卡片保留参考稿中的呼吸边距。
        layout.setContentsMargins(28, 28, 28, 8)
        layout.setSpacing(16)

        def icon_box(kind, color="#2B83F6"):
            box = QFrame(self)
            box.setObjectName("cardIcon")
            box.setFixedSize(36, 36)
            box_layout = QHBoxLayout(box)
            box_layout.setContentsMargins(4, 4, 4, 4)
            box_layout.setAlignment(Qt.AlignCenter)
            icon_widget = _MetricIcon(kind, color, box)
            box_layout.addWidget(icon_widget, 0, Qt.AlignCenter)
            return box

        def add_shadow(widget, blur=28, offset_y=8, alpha=36):
            effect = QGraphicsDropShadowEffect(widget)
            effect.setBlurRadius(blur)
            effect.setOffset(0, offset_y)
            effect.setColor(QColor(0, 0, 0, 75 if _is_dark_theme() else alpha))
            widget.setGraphicsEffect(effect)

        def text_label(text, object_name):
            label = QLabel(text, self)
            label.setObjectName(object_name)
            return label

        def toolbar_divider():
            divider = QFrame(self)
            divider.setFixedSize(1, 26)
            divider.setStyleSheet(
                f"background: {'#34465D' if _is_dark_theme() else '#D9E5EF'}; border: none;"
            )
            return divider

        # 顶部品牌标题与会话工具栏。slogan 作为独立浮层贴在工具栏上方，
        # 这样可以精确控制它与按钮的留白。
        header_wrap = QVBoxLayout()
        header_wrap.setContentsMargins(0, 0, 0, 0)
        header_wrap.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(12)
        intro = QVBoxLayout()
        intro.setSpacing(2)
        greeting = QLabel("专注学习 · 持续成长")
        greeting.setObjectName("dashboardGreeting")
        intro.addWidget(greeting)
        title = QLabel(f"润物 Moisten <span style='font-size:14px; color:#91A9C2;'>v{CURRENT_VERSION}</span>")
        title.setTextFormat(Qt.RichText)
        title.setObjectName("dashboardTitle")
        intro.addWidget(title)
        subtitle = QLabel("")
        subtitle.setObjectName("dashboardSubtitle")
        subtitle.setFixedHeight(2)
        header.addLayout(intro)
        header.addStretch()

        # 工具栏整体略向下，并与左侧标题区域的底边对齐。
        toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 8, 0, 0)
        toolbar_layout.setSpacing(12)

        self.lbl_mode = CaptionLabel("")
        self.lbl_mode.setObjectName("statusPill")
        self.lbl_mode.setFixedHeight(38)
        toolbar_layout.addWidget(self.lbl_mode)
        self.lbl_session_state = CaptionLabel("准备中")
        self.lbl_session_state.setObjectName("statusPill")
        self.lbl_session_state.setProperty("success", True)
        self.lbl_session_state.setFixedHeight(38)
        toolbar_layout.addWidget(self.lbl_session_state)
        self.lbl_runtime = CaptionLabel("00:00:00")
        self.lbl_runtime.setObjectName("runtime")
        self.lbl_runtime.setAlignment(Qt.AlignCenter)
        self.lbl_runtime.setFixedWidth(76)
        toolbar_layout.addWidget(self.lbl_runtime)
        toolbar_layout.addWidget(toolbar_divider())

        self.btn_stop = TransparentToolButton(self)
        self.btn_stop.setObjectName("topIconButton")
        self.btn_stop.setIcon(FIF.PAUSE)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setIconSize(QSize(22, 22))
        self.btn_stop.setFixedSize(42, 40)
        self.btn_stop.setToolTip("停止学习")
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        toolbar_layout.addWidget(self.btn_stop)
        toolbar_layout.addWidget(toolbar_divider())

        btn_update = TransparentToolButton(self)
        btn_update.setObjectName("topIconButton")
        btn_update.setIcon(FIF.CLOUD)
        btn_update.setIconSize(QSize(22, 22))
        btn_update.setFixedSize(42, 40)
        btn_update.setToolTip("检查更新")
        btn_update.clicked.connect(self._check_update_manual)
        toolbar_layout.addWidget(btn_update)
        toolbar_layout.addWidget(toolbar_divider())
        self.btn_log_open = TransparentToolButton(self)
        self.btn_log_open.setObjectName("topIconButton")
        self.btn_log_open.setIcon(FIF.DOCUMENT)
        self.btn_log_open.setIconSize(QSize(22, 22))
        self.btn_log_open.setFixedSize(42, 40)
        self.btn_log_open.setToolTip("展开日志")
        self.btn_log_open.clicked.connect(self._toggle_log)
        toolbar_layout.addWidget(self.btn_log_open)
        header.addWidget(toolbar, 0, Qt.AlignBottom)
        header_wrap.addLayout(header)
        layout.addLayout(header_wrap)
        self._toolbar = toolbar
        self._slogan = QLabel("让知识，如水般滋养成长", self)
        self._slogan.setObjectName("slogan")
        self._slogan.adjustSize()
        QTimer.singleShot(0, self._place_slogan)

        # 当前任务 Hero 卡：进度条与可识别的课程意象
        self.current_card = _HeroCard(self)
        self.current_card.setObjectName("heroCard")
        hero_layout = QHBoxLayout(self.current_card)
        hero_layout.setContentsMargins(24, 20, 20, 20)
        hero_layout.setSpacing(18)
        hero_layout.addWidget(icon_box("book", "#256BD3"))
        hero_text = QVBoxLayout()
        hero_text.setSpacing(7)
        kicker = QLabel("当前任务")
        kicker.setObjectName("heroKicker")
        hero_text.addWidget(kicker)
        self.lbl_current_task = QLabel("尚未开始学习")
        self.lbl_current_task.setObjectName("heroTitle")
        self.lbl_current_task.setWordWrap(True)
        hero_text.addWidget(self.lbl_current_task)
        self.lbl_current_hint = QLabel("启动后，这里会显示当前 worker 正在处理的课程")
        self.lbl_current_hint.setObjectName("heroHint")
        hero_text.addWidget(self.lbl_current_hint)
        self.current_progress = QProgressBar(self)
        self.current_progress.setRange(0, 100)
        self.current_progress.setValue(0)
        self.current_progress.setFormat("%p%")
        self.current_progress.setFixedHeight(18)
        # 参考稿的 Hero 卡只保留任务信息，进度条放在“学习目标”卡内。
        self.current_progress.setVisible(False)
        hero_layout.addLayout(hero_text, 1)

        hero_art = QFrame(self.current_card)
        hero_art.setObjectName("heroArt")
        hero_art.setFixedWidth(360)
        art_layout = QVBoxLayout(hero_art)
        art_layout.setContentsMargins(22, 18, 22, 18)
        art_layout.setSpacing(3)
        art_label = QLabel("在真实的场景中")
        art_label.setObjectName("heroTitle")
        art_layout.addWidget(art_label)
        art_hint = QLabel("遇见更大的可能")
        art_hint.setObjectName("heroHint")
        art_layout.addWidget(art_hint)
        art_layout.addStretch()
        hero_layout.addWidget(hero_art)
        layout.addWidget(self.current_card)
        add_shadow(self.current_card, blur=30, offset_y=10, alpha=42)

        # Main area: 信息卡片 + 进度表，日志仍保留为可折叠侧栏
        main_area = QHBoxLayout()
        main_area.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(16)
        info_row = QHBoxLayout()
        info_row.setSpacing(16)

        hours_card = SurfaceCard(self)
        hl = QVBoxLayout(hours_card)
        hl.setContentsMargins(20, 18, 20, 16)
        hl.setSpacing(12)
        hl_title = QHBoxLayout()
        hl_title.addWidget(icon_box("clock", "#2B83F6"))
        hl_title.addWidget(text_label("训练学时", "cardTitle"))
        hl_title.addStretch()
        self.lbl_updated = CaptionLabel("更新: --")
        self.lbl_updated.setObjectName("cardMeta")
        hl_title.addWidget(self.lbl_updated)
        hl.addLayout(hl_title)

        metrics = QHBoxLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(0)

        def metric_column(kind, color, label, value_widget):
            col = QVBoxLayout()
            col.setAlignment(Qt.AlignCenter)
            col.setSpacing(5)
            col.addWidget(_MetricIcon(kind, color, self), 0, Qt.AlignHCenter)
            caption = QLabel(label)
            caption.setObjectName("metricLabel")
            caption.setAlignment(Qt.AlignCenter)
            col.addWidget(caption)
            value_widget.setAlignment(Qt.AlignCenter)
            value_widget.setObjectName("metricText")
            value_widget.setWordWrap(True)
            col.addWidget(value_widget)
            return col

        self.lbl_central = QLabel("-- 学时")
        self.lbl_online = QLabel("-- 学时")
        self.lbl_session = QLabel("-- 学时")
        metrics.addLayout(metric_column("cap", "#2B83F6", "集中培训", self.lbl_central), 1)
        separator1 = QFrame(self)
        separator1.setFrameShape(QFrame.VLine)
        separator1.setFrameShadow(QFrame.Plain)
        separator1.setStyleSheet(
            f"color: {'#34465D' if _is_dark_theme() else '#DDEAF5'};"
        )
        metrics.addWidget(separator1)
        metrics.addLayout(metric_column("laptop", "#1FB8C4", "网络自学", self.lbl_online), 1)
        separator2 = QFrame(self)
        separator2.setFrameShape(QFrame.VLine)
        separator2.setFrameShadow(QFrame.Plain)
        separator2.setStyleSheet(
            f"color: {'#34465D' if _is_dark_theme() else '#DDEAF5'};"
        )
        metrics.addWidget(separator2)
        metrics.addLayout(metric_column("ribbon", "#F2B35C", "本次已学", self.lbl_session), 1)
        hl.addLayout(metrics, 1)
        self.sparkline = _Sparkline()
        self.sparkline.setFixedHeight(1)
        info_row.addWidget(hours_card, 1)
        add_shadow(hours_card)

        goal_card = SurfaceCard(self)
        gl = QHBoxLayout(goal_card)
        gl.setContentsMargins(20, 18, 20, 18)
        gl.setSpacing(18)
        gl_left = QVBoxLayout()
        gl_left.setSpacing(8)
        gl_title = QHBoxLayout()
        gl_title.addWidget(icon_box("target", "#1FB8B7"))
        gl_title.addWidget(text_label("学习目标", "cardTitle"))
        gl_title.addStretch()
        gl_left.addLayout(gl_title)
        self.lbl_goal_info = BodyLabel("--")
        self.lbl_goal_info.setWordWrap(True)
        gl_left.addWidget(self.lbl_goal_info)
        self.goal_progress = QProgressBar(self)
        self.goal_progress.setRange(0, 100)
        self.goal_progress.setValue(0)
        self.goal_progress.setTextVisible(False)
        self.goal_progress.setFixedHeight(13)
        gl_left.addWidget(self.goal_progress)
        goal_note = QLabel("按计划完成学习任务，持续提升专业能力")
        goal_note.setObjectName("cardMeta")
        gl_left.addWidget(goal_note)
        self.lbl_eta = CaptionLabel("")
        self.lbl_eta.setObjectName("cardMeta")
        gl_left.addWidget(self.lbl_eta)
        gl_left.addStretch()
        gl.addLayout(gl_left, 1)
        self.progress_ring = _GoalRing()
        self.progress_ring.setFixedSize(116, 116)
        self.progress_ring.setValue(0)
        self.progress_ring.setTextVisible(True)
        gl.addWidget(self.progress_ring)
        info_row.addWidget(goal_card, 1)
        add_shadow(goal_card)
        left.addLayout(info_row)

        table_card = SurfaceCard(self)
        tl = QVBoxLayout(table_card)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        table_header = QHBoxLayout()
        table_header.setContentsMargins(20, 16, 20, 12)
        table_header.setSpacing(10)
        table_header.addWidget(icon_box("bars", "#2B83F6"))
        table_header.addWidget(text_label("学习进度", "cardTitle"))
        table_header.addStretch()
        self.lbl_progress_summary = CaptionLabel("")
        self.lbl_progress_summary.setObjectName("cardMeta")
        table_header.addWidget(self.lbl_progress_summary)
        table_header.addWidget(text_label("⋮", "heroKicker"))
        tl.addLayout(table_header)

        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["#", "课程", "进度", "预计", "状态"])
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Fixed)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.Fixed)
        header_view.setSectionResizeMode(3, QHeaderView.Fixed)
        header_view.setSectionResizeMode(4, QHeaderView.Fixed)
        header_view.setFixedHeight(31)
        self.table.setColumnWidth(0, 54)
        # 进度与预计保持同宽，课程列使用剩余空间，避免长课程名被压缩。
        self.table.setColumnWidth(2, 84)
        self.table.setColumnWidth(3, 84)
        self.table.setColumnWidth(4, 180)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setSelectionMode(TableWidget.NoSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setBorderRadius(8)
        tl.addWidget(self.table)
        left.addWidget(table_card, 1)
        add_shadow(table_card, blur=30, offset_y=9, alpha=34)
        main_area.addLayout(left, 1)

        # 日志侧栏：默认折叠，异常或用户主动打开时展示
        self.log_card = SurfaceCard(self)
        self.log_card.setFixedWidth(360)
        ll = QVBoxLayout(self.log_card)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        log_header = QHBoxLayout()
        log_header.setContentsMargins(18, 14, 18, 10)
        log_header.addWidget(icon_box("book", "#2B83F6"))
        log_header.addWidget(text_label("运行日志", "cardTitle"))
        log_header.addStretch()
        self.btn_log_toggle = ToolButton(FIF.CHEVRON_RIGHT)
        self.btn_log_toggle.setToolTip("折叠日志")
        self.btn_log_toggle.clicked.connect(self._toggle_log)
        log_header.addWidget(self.btn_log_toggle)
        self.lbl_log_badge = CaptionLabel("")
        self.lbl_log_badge.setObjectName("statusPill")
        self.lbl_log_badge.hide()
        log_header.addWidget(self.lbl_log_badge)
        ll.addLayout(log_header)
        self.log_view = PlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        ll.addWidget(self.log_view)
        main_area.addWidget(self.log_card)
        add_shadow(self.log_card)
        layout.addLayout(main_area, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 4, 0)
        footer_left = QLabel(f"润物 Moisten   v{CURRENT_VERSION}    |    学而不止 · 润物无声")
        footer_left.setObjectName("cardMeta")
        footer_right = QLabel("今天也在进步  ♡")
        footer_right.setObjectName("cardMeta")
        footer.addWidget(footer_left)
        footer.addStretch()
        footer.addWidget(footer_right)
        layout.addLayout(footer)
        # 当前任务优先；日志在有异常或用户主动展开时占用空间。
        self._toggle_log()

    def _toggle_log(self):
        self._log_collapsed = not self._log_collapsed
        self.log_card.setVisible(not self._log_collapsed)
        self.log_view.setVisible(not self._log_collapsed)
        self.btn_log_toggle.setIcon(FIF.LEFT_ARROW if self._log_collapsed else FIF.CHEVRON_RIGHT)
        self.btn_log_toggle.setToolTip("展开日志" if self._log_collapsed else "折叠日志")
        self.btn_log_open.setToolTip("展开日志" if self._log_collapsed else "折叠日志")
        if not self._log_collapsed:
            self._unread_logs = 0
            self.lbl_log_badge.setText("")
            self.lbl_log_badge.hide()

    def _on_stop_clicked(self):
        worker = getattr(self, "_worker", None)
        if not worker or not worker.isRunning():
            return
        dlg = Dialog("停止学习", "当前学习任务会在安全检查点停止，已完成进度会保留。", self)
        style_moisten_dialog(dlg)
        dlg.cancelButton.setText("继续学习")
        dlg.yesButton.setText("停止")
        if dlg.exec():
            self.lbl_session_state.setText("停止中")
            self.btn_stop.setEnabled(False)
            self._stop_current_learning()

    def _update_runtime(self):
        if self._runtime_start is None:
            return
        elapsed = max(0, int(__import__("time").time() - self._runtime_start))
        self.lbl_runtime.setText(f"{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}")

    def _stop_current_learning(self):
        """停止正在运行的学习任务（配置变更/重新开始时调用）"""
        # 1) 解除可能阻塞 worker 的对话框等待
        for ev in ("_tag_event", "_page_event", "_exam_retry_event"):
            ev_obj = getattr(self, ev, None)
            if ev_obj:
                ev_obj.set()
        # 2) 请求协作式停止：学习引擎在课程/阶段边界主动退出
        learner = getattr(self, "_learner", None)
        if learner:
            try:
                learner._stop_event.set()
            except Exception:
                pass
        worker = getattr(self, "_worker", None)
        if worker:
            worker.request_stop()

    def start_learning(self):
        # 已有学习线程在运行：先停止旧任务并关闭其浏览器，再用新配置重新开始
        self._runtime_start = __import__("time").time()
        self._runtime_timer.start()
        self.lbl_session_state.setText("初始化")
        self.lbl_current_task.setText("正在准备学习任务…")
        self.lbl_current_hint.setText("正在启动浏览器并读取课程列表")
        self.btn_stop.setEnabled(True)
        self.current_progress.setValue(0)
        self.goal_progress.setValue(0)
        # 重置进度环状态（颜色恢复主题色、数值清零）
        self.progress_ring.setValue(0)
        try:
            tokens = QApplication.instance().property("moisten_tokens")
            accent = getattr(tokens, "accent", "#087F88")
            accent_soft = getattr(tokens, "accent_strong", "#63D1D4")
            self.progress_ring.setCustomBarColor(accent, accent_soft)
        except Exception:
            pass
        old_worker = getattr(self, "_worker", None)
        if old_worker and old_worker.isRunning():
            self._on_log("检测到学习中，正在停止旧任务...", "yellow")
            old_learner = getattr(self, "_learner", None)
            self._stop_current_learning()
            if not old_worker.wait(15000):
                self._on_log("旧任务未能在限时内停止，请稍后重试", "red")
                return
            # 关闭旧 learner 的浏览器，避免新旧两个浏览器并存
            if old_learner:
                try:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(old_learner.close())
                    loop.close()
                except:
                    pass
            self._on_log("旧任务已停止，使用新配置重新开始", "green")
        win = self.window()
        self._init_table(win.cfg_workers)
        self._set_goal_info(win)

        # Mode indicator
        mode = getattr(win, "cfg_mode", "auto")
        if mode == "manual":
            self.lbl_mode.setText("手动模式")
        else:
            self.lbl_mode.setText("自动模式")

        self._learner = None  # 保存learner引用用于退出时清理
        self._worker = AsyncThread(self._run_learning, self)
        self._worker.win = win  # GUI线程捕获窗口引用，worker不再跨线程调用 window()
        self._worker.log_signal.connect(self._on_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.hours_signal.connect(self._on_hours)
        self._worker.done_signal.connect(self._on_done)
        self._worker.tag_request_signal.connect(self._on_tag_request)
        self._worker.tag_confirm_signal.connect(self._on_tag_confirm)
        self._worker.page_confirm_signal.connect(self._on_page_confirm)
        self._worker.eta_reset_signal.connect(self._on_eta_reset)
        self._worker.browser_download_signal.connect(self._on_browser_download)
        self._worker.exam_retry_signal.connect(self._on_exam_retry)
        self._worker.start()

    @staticmethod
    def _status_kind(status):
        status = str(status or "").strip()
        if status in {"", "-", "等待中"}:
            return "waiting"
        if any(k in status for k in ("完成", "目标达成")):
            return "success"
        if any(k in status for k in ("异常", "失败", "放弃", "超时")):
            return "danger"
        if any(k in status for k in ("学习", "加载", "查找", "考试")):
            return "active"
        return "warning"

    @classmethod
    def _status_capsule(cls, text, kind=None):
        container = QWidget()
        container.setAttribute(Qt.WA_TranslucentBackground)
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        label = QLabel(str(text))
        label.setObjectName("tableStatus")
        label.setProperty("kind", kind or cls._status_kind(text))
        label.setAlignment(Qt.AlignCenter)
        row.addStretch(1)
        row.addWidget(label, 0, Qt.AlignCenter)
        row.addStretch(1)
        return container

    def _set_status_cell(self, row, text, kind=None):
        """替换状态单元格时同时移除旧 widget/item，避免出现两层状态文本。"""
        old_widget = self.table.cellWidget(row, 4)
        if old_widget is not None:
            old_widget.hide()
            old_widget.setParent(None)
            old_widget.deleteLater()
        self.table.removeCellWidget(row, 4)
        self.table.takeItem(row, 4)
        self.table.setCellWidget(row, 4, self._status_capsule(text, kind))

    def _init_table(self, workers):
        self.table.setRowCount(workers)
        self._progress_bars = []
        self._progress_labels = []
        for i in range(workers):
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(13)
            progress_cell = QWidget()
            progress_layout = QHBoxLayout(progress_cell)
            progress_layout.setContentsMargins(6, 4, 6, 4)
            progress_layout.setSpacing(6)
            progress_layout.addWidget(bar, 1)
            percent_label = QLabel("0%")
            percent_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            percent_label.setMinimumWidth(28)
            progress_layout.addWidget(percent_label)
            self.table.setCellWidget(i, 2, progress_cell)
            self._progress_bars.append(bar)
            self._progress_labels.append(percent_label)
            self.table.setItem(i, 1, QTableWidgetItem("-"))
            self.table.setItem(i, 3, QTableWidgetItem("-"))
            self._set_status_cell(i, "等待中", "waiting")

    @staticmethod
    def _format_worker_eta(seconds):
        """把 worker 的预计剩余秒数压缩成适合窄列的中文文本。"""
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

    def _worker_eta(self, wid, course, progress_text, status, reported_eta):
        """优先使用后端 ETA；训练营未上报时按当前 worker 进度估算。"""
        import time as _time

        placeholder = {"", "-", "...", "计算中", "计算中..."}
        reported = str(reported_eta or "").strip()
        try:
            pct = float(str(progress_text).rstrip("%"))
            pct = max(0.0, min(100.0, pct))
            pct_valid = True
        except (TypeError, ValueError):
            pct = 0.0
            pct_valid = False

        state = self._worker_eta_state.get(wid)
        if state is None or state.get("course") != course:
            state = {"course": course, "started": _time.monotonic(), "history": []}
            self._worker_eta_state[wid] = state

        if pct_valid:
            history = state["history"]
            now = _time.monotonic()
            if not history or pct != history[-1][1]:
                history.append((now, pct))
                if len(history) > 20:
                    del history[:-20]

        # 后端有可靠结果时直接显示；训练营通常会传 "-"，再走本地估算。
        if reported not in placeholder:
            return reported
        if pct_valid and pct >= 100:
            return "-"
        if status == "考试答题中":
            return "考试中"
        active = status in {"学习中", "加载中", "查找按钮"}
        if not active:
            return "-"

        history = state["history"]
        if len(history) >= 2:
            t0, p0 = history[0]
            t1, p1 = history[-1]
            dt = t1 - t0
            dp = p1 - p0
            # 过短的采样间隔会把 ETA 放大成 0 秒（页面初始化时常见），
            # 至少积累一秒再展示数值。
            if dt >= 1.0 and dp > 0:
                return self._format_worker_eta((100.0 - p1) * dt / dp)
        if pct_valid and pct > 0:
            elapsed = _time.monotonic() - state["started"]
            if elapsed > 1:
                return self._format_worker_eta((100.0 - pct) * elapsed / pct)
        return "计算中..."

    def _manual_goal_text(self):
        """手动模式的目标区文案（供 _set_goal_info / _on_hours 共用）"""
        win = self.window()
        n = len(getattr(win, "cfg_manual_urls", []))
        return f"手动模式 · {n} 个URL" if n else "手动模式（未设置URL）"

    def _set_goal_info(self, win):
        # 手动模式：无学时目标，显示手动信息而不是"不学习"
        mode = getattr(win, "cfg_mode", "auto")
        if mode == "manual":
            self.lbl_goal_info.setText(self._manual_goal_text())
            return
        c_goal = getattr(win, "cfg_central_goal", 0)
        o_goal = getattr(win, "cfg_online_goal", 0)
        c_mode = getattr(win, "cfg_central_mode", "target")
        o_mode = getattr(win, "cfg_online_mode", "target")

        if c_goal <= 0 and o_goal <= 0:
            self.lbl_goal_info.setText("不学习")
            return

        parts = []
        if c_goal > 0:
            mode_str = "总" if c_mode == "target" else "差额"
            parts.append(f"集中{mode_str}{c_goal:.0f}")
        if o_goal > 0:
            mode_str = "总" if o_mode == "target" else "差额"
            parts.append(f"网络{mode_str}{o_goal:.0f}")
        self.lbl_goal_info.setText(" + ".join(parts) + "学时")

    async def _run_learning(self, thread: AsyncThread):
        # 重置 ETA 追踪（在 GUI 线程执行，worker 不跨线程操作 Qt 对象）
        thread.eta_reset_signal.emit()

        learner = None
        win = thread.win  # GUI线程捕获的窗口引用，避免跨线程 window()
        # 清除上一轮的固定绝对目标（差额模式下进度环使用）
        win.cfg_central_abs_goal = 0
        win.cfg_online_abs_goal = 0
        cfg_workers = getattr(win, "cfg_workers", 1)
        cfg_headless = getattr(win, "cfg_headless", False)
        cfg_username = getattr(win, "cfg_username", "")
        cfg_password = getattr(win, "cfg_password", "")
        cfg_auto_login = getattr(win, "cfg_auto_login", True)
        cfg_tags = getattr(win, "cfg_tags", [])
        cfg_mode = getattr(win, "cfg_mode", "auto")
        cfg_manual_urls = getattr(win, "cfg_manual_urls", [])

        log = lambda msg, style="": thread.log_signal.emit(msg, style)
        progress_cb = lambda data: thread.progress_signal.emit(data)
        hours_cb = lambda data: thread.hours_signal.emit(data)

        try:
            cfg_browser = getattr(win, "cfg_browser", "chrome")  # 默认系统 Chrome
            cfg_chrome_path = getattr(win, "cfg_chrome_path", "")
            log("正在初始化浏览器...")
            learner = AutoLearner(headless=cfg_headless, workers=cfg_workers, browser=cfg_browser)
            # 考试没考成/没通过时弹窗询问是否重考（worker 线程通过信号回主线程弹窗）
            async def _ask_exam_retry(exam_name, reason):
                self._exam_retry_event.clear()
                self._exam_retry_choice = False
                thread.exam_retry_signal.emit(exam_name, reason or "")
                finished = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._exam_retry_event.wait(EXAM_RETRY_TIMEOUT))
                if not finished:
                    log(f"「{exam_name}」重考确认超时（{EXAM_RETRY_TIMEOUT}秒未操作），不重考", "yellow")
                    return False
                return bool(self._exam_retry_choice)

            learner.exam_retry_hook = _ask_exam_retry
            # 考试自动答题：只有开启且配置了 Key 时才生效
            learner.apply_exam_settings({
                "exam_enabled": getattr(win, "cfg_exam_enabled", False),
                "deepseek_api_key": getattr(win, "cfg_deepseek_api_key", ""),
                "deepseek_model": getattr(win, "cfg_deepseek_model", ""),
                "deepseek_base_url": DEEPSEEK_DEFAULT_BASE_URL,
                "deepseek_thinking": getattr(win, "cfg_deepseek_thinking", False),
            })
            if learner.exam_enabled and not learner.deepseek_api_key:
                log("已开启考试自动答题，但未配置 DeepSeek API Key，将跳过考试", "yellow")
            elif learner.exam_enabled:
                log(f"考试自动答题已开启（模型 {learner.deepseek_model}）", "blue")
            self._learner = learner  # 保存引用用于退出时清理
            await learner.init(
                log_callback=log, chrome_path=cfg_chrome_path,
                download_callback=lambda s: thread.browser_download_signal.emit(s),
            )
            log("浏览器初始化完成", "green")

            log("正在登录...")
            try:
                await learner.login(
                    page=learner.pages[0],
                    username=cfg_username,
                    password=cfg_password,
                    auto_login=cfg_auto_login,
                    log_callback=log,
                )
            except Exception as e:
                log(f"登录失败: {e}", "red")
                thread.done_signal.emit(1, 0)
                return
            log("登录成功", "green")

            # 手动模式：直接从指定URL学习（不依赖学习目标，需在任何目标检查之前）
            if cfg_mode == "manual":
                if not cfg_manual_urls:
                    log("手动模式未指定URL，退出", "yellow")
                    thread.done_signal.emit(0, 0)
                    return
                log(f"手动模式：{len(cfg_manual_urls)} 个URL", "blue")
                await learner.learn_from_urls(
                    cfg_manual_urls, cfg_workers,
                    progress_cb, hours_cb, log
                )
                thread.done_signal.emit(*getattr(learner, "last_stats", (0, 0)))
                return

            # 获取配置（新格式：central_goal/online_goal，0表示不学习）
            cfg_central_goal = getattr(win, "cfg_central_goal", 0)
            cfg_online_goal = getattr(win, "cfg_online_goal", 0)
            cfg_central_mode = getattr(win, "cfg_central_mode", "target")
            cfg_online_mode = getattr(win, "cfg_online_mode", "target")

            if cfg_central_goal <= 0 and cfg_online_goal <= 0:
                log("未设定学习目标，退出", "yellow")
                thread.done_signal.emit(0, 0)
                return

            # 登录后立即检查学时
            cur_hours = {"central": 0, "online": 0}
            if cfg_central_goal > 0 or cfg_online_goal > 0:
                log("正在检查当前学时...", "blue")
                try:
                    _h = await learner._get_study_hours()
                    cur_hours = {"central": _h.get("central", 0), "online": _h.get("online", 0)}
                    # 如果获取失败（返回0），尝试用配置中保存的值
                    if cur_hours["central"] == 0 and cur_hours["online"] == 0:
                        cfg_old = {}
                        if os.path.exists(CONFIG_PATH):
                            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                                cfg_old = json.load(f)
                        cur_hours["central"] = cfg_old.get("_last_central_hours", 0)
                        cur_hours["online"] = cfg_old.get("_last_online_hours", 0)
                        if cur_hours["central"] > 0 or cur_hours["online"] > 0:
                            log(f"使用上次记录: 集中{cur_hours['central']:.1f} 网络{cur_hours['online']:.1f} 学时", "yellow")
                    else:
                        log(f"当前: 集中{cur_hours['central']:.1f} 网络{cur_hours['online']:.1f} 学时", "blue")
                    hours_cb({
                        "central": cur_hours["central"], "online": cur_hours["online"],
                        "updated": datetime.now().strftime("%H:%M:%S"),
                    })

                    # 保存当前学时到配置（供差额模式计算绝对目标用）
                    try:
                        cfg_save = {}
                        if os.path.exists(CONFIG_PATH):
                            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                                cfg_save = json.load(f)
                        cfg_save["_last_central_hours"] = cur_hours["central"]
                        cfg_save["_last_online_hours"] = cur_hours["online"]
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(cfg_save, f, ensure_ascii=False, indent=2)
                    except:
                        pass

                    # 统一转成"还需学习多少"：
                    # target模式：总目标 - 已有 = 还需
                    # remain模式：差额本身就是还需
                    if cfg_central_mode == "target":
                        cfg_central_goal = max(0, cfg_central_goal - cur_hours["central"])
                    if cfg_online_mode == "target":
                        cfg_online_goal = max(0, cfg_online_goal - cur_hours["online"])

                    # 存储"还需"值供phases判断，不改win上的原始目标（_on_hours要用原始值算进度）
                    # 检查是否都已完成
                    if cfg_central_goal <= 0 and cfg_online_goal <= 0:
                        log(f"已达到全部学习目标，无需学习", "bold green")
                        thread.done_signal.emit(0, 0)
                        return
                except Exception as e:
                    log(f"学时检查失败(继续学习): {e}", "yellow")

            # 构建阶段列表：先集中培训，再网络自学
            phases = []
            if cfg_central_goal > 0:
                phases.append(("central", cfg_central_goal))
            if cfg_online_goal > 0:
                phases.append(("online", cfg_online_goal))

            if not phases:
                log("未设定学习目标，退出", "yellow")
                thread.done_signal.emit(0, 0)
                return

            # ── 自动模式：按阶段顺序学习 ──
            page = learner.pages[0]
            central_phase = any(p[0] == "central" for p in phases)

            # 集中培训：走专题班流程（标签筛选、翻页、进度恢复）
            # 网络自学不走这里，直接去课程列表 /course/#/list/1
            if central_phase:
                list_url = "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise"

                # 加载专题班列表页（刷新直到标签树出来，最多重试 N 次防止无限卡死）
                tags_by_category = {}
                load_attempt = 0
                MAX_TAG_LOAD = 10
                while not tags_by_category:
                    load_attempt += 1
                    if load_attempt > MAX_TAG_LOAD:
                        log(f"标签加载失败（{MAX_TAG_LOAD}次），按不筛选继续学习", "yellow")
                        break
                    try:
                        await page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                        await page.wait_for_timeout(6000)
                    except:
                        pass

                    # 检查是否还在登录页（session过期）
                    body = ""
                    try:
                        body = await page.locator("body").inner_text(timeout=3000)
                    except:
                        pass
                    if "立即登录" in body or "密码登录" in body or "统一认证" in body:
                        log("Session过期，请在浏览器中重新登录...", "red")
                        await page.goto("https://u.ccb.com/portal/#/study", wait_until="domcontentloaded", timeout=15000)
                        # 等待用户手动登录（停止/退出时立即跳出）
                        for _ in range(120):
                            if thread._stop_event.is_set():
                                break
                            await asyncio.sleep(2)
                            try:
                                check_body = await page.locator("body").inner_text(timeout=2000)
                                if "立即登录" not in check_body and "密码登录" not in check_body:
                                    break
                            except:
                                pass
                        log("登录成功，继续加载...", "green")
                        continue

                    try:
                        tags_by_category = await learner.get_available_tags(page) or {}
                    except:
                        pass

                    if tags_by_category:
                        tag_count = sum(len(v) for v in tags_by_category.values())
                        log(f"发现 {tag_count} 个标签", "blue")
                        break

                    log(f"标签未加载，重试({load_attempt})...", "yellow")
                    await page.wait_for_timeout(3000)

                # 标签选择：等待对话框结果（带超时，防止 worker 永久阻塞）
                # 用 Event.wait(timeout) 而非 run_in_executor：超时不会泄漏线程
                async def _wait_tag(timeout=180):
                    return await asyncio.get_event_loop().run_in_executor(
                        None, lambda: self._tag_event.wait(timeout))

                if cfg_tags:
                    # 有已保存标签，询问用户
                    self._tag_event.clear()
                    thread.tag_confirm_signal.emit(cfg_tags, tags_by_category)
                    if not await _wait_tag():
                        log("标签选择超时，使用已保存标签", "yellow")
                    cfg_tags = list(getattr(win, "cfg_tags", []))
                elif tags_by_category:
                    # 无已保存标签，直接弹选择框
                    self._tag_event.clear()
                    thread.tag_request_signal.emit(tags_by_category)
                    if not await _wait_tag():
                        log("标签选择超时，按未选择继续", "yellow")
                    cfg_tags = list(getattr(win, "cfg_tags", []))

                log(f"标签: {', '.join(cfg_tags)}" if cfg_tags else "未选择标签，学习全部", "green" if cfg_tags else "yellow")

                if cfg_tags:
                    learner.tags_to_learn = cfg_tags
                    log(f"应用标签筛选: {', '.join(cfg_tags)}", "blue")
                    filter_ok = await learner.filter_by_tags(page)
                    if not filter_ok:
                        log("标签筛选失败，停止学习", "red")
                        thread.done_signal.emit(0, 0)
                        return

                progress = learner.load_progress()
                completed_ids = set(progress.get("completed_ws_ids", []))
                last_page = progress.get("last_page", 1)

                # 询问是否从上次页码继续
                page_num = 1
                if last_page > 1:
                    self._page_event = threading.Event()
                    self._page_resume = True
                    thread.page_confirm_signal.emit(last_page)
                    # 带超时的 Event.wait（不泄漏线程）
                    if not await asyncio.get_event_loop().run_in_executor(
                            None, lambda: self._page_event.wait(120)):
                        log("页码确认超时，从第 1 页开始", "yellow")
                        self._page_resume = False

                    if self._page_resume:
                        log(f"跳转到第 {last_page} 页", "blue")
                        for _ in range(last_page - 1):
                            moved = await learner.go_to_next_page(page)
                            if not moved:
                                break
                            page_num += 1
                            await page.wait_for_timeout(1000)
                    else:
                        log("从第 1 页开始", "blue")

            # ── 按阶段顺序学习（先集中培训，再网络自学）──
            for phase_goal_type, phase_goal_hours in phases:
                # 用户变更配置：停止整个学习流程
                if thread._stop_event.is_set():
                    log("学习已停止（配置已变更）", "yellow")
                    break
                type_name = "集中培训" if phase_goal_type == "central" else "网络自学"
                if phase_goal_hours > 0:
                    log(f"━━ 阶段: {type_name} 目标{phase_goal_hours:.0f}学时 ━━", "bold blue")
                else:
                    log(f"━━ 阶段: {type_name} 无限制 ━━", "bold blue")

                # 重置ETA追踪（GUI线程执行）
                thread.eta_reset_signal.emit()

                # study_goal 是绝对目标值（当前学时 + 还需学时）
                # phase_goal_hours 是"还需"的值，需要加上当前学时
                try:
                    _cur = (await learner._get_study_hours()).get(phase_goal_type, 0)
                    learner.study_goal = _cur + phase_goal_hours
                    # 记录固定绝对目标，供GUI进度环显示（差额模式不再随学时增长）
                    if phase_goal_type == "central":
                        win.cfg_central_abs_goal = learner.study_goal
                    else:
                        win.cfg_online_abs_goal = learner.study_goal
                except:
                    learner.study_goal = phase_goal_hours
                learner.goal_type = phase_goal_type

                if phase_goal_type == "online":
                    # 网络自学：从课程列表页 /course/#/list/1 采集课程学习，
                    # 不走专题班流程
                    ok = await learner.learn_course_list(
                        "https://u.ccb.com/course/#/list/1",
                        log_callback=log, progress_callback=progress_cb, hours_callback=hours_cb,
                    )
                    if ok:
                        log(f"✓ {type_name}阶段完成", "bold green")
                    else:
                        log(f"✗ {type_name}阶段未完成（未能获取课程，请检查登录/网络后重试）", "red")
                    continue

                no_more_pages = False
                tasks = []
                ws_locks = {}

                # 采集课程，至少凑够 worker 数量再开始学（除非已无更多页）
                while len(tasks) < cfg_workers and not no_more_pages:
                    if thread._stop_event.is_set():
                        break
                    workshops = await learner.get_workshops(page)
                    if not workshops:
                        no_more_pages = True
                        break
                    log(f"第 {page_num} 页: {len(workshops)} 个专题班", "blue")
                    learner.save_progress(completed_ids, page_num, 0)
                    new_tasks, new_locks = await learner._collect_workshops_courses(
                        page, workshops, completed_ids, log_callback=log
                    )
                    tasks.extend(new_tasks)
                    ws_locks.update(new_locks)
                    if len(tasks) >= cfg_workers:
                        break
                    log(f"已采集 {len(tasks)} 门，不足 {cfg_workers}，翻页继续...", "yellow")
                    moved = await learner.go_to_next_page(page)
                    if not moved:
                        no_more_pages = True
                    else:
                        page_num += 1
                        await page.wait_for_timeout(3000)

                if tasks:
                    log(f"开始学习（{len(tasks)} 门课程, {cfg_workers} 个线程）", "bold blue")
                    _fetch_lock = asyncio.Lock()

                    # 两个专用页面：一个保持在列表页翻页，一个用于采集课程
                    _list_page = await learner.context.new_page()
                    _detail_page = await learner.context.new_page()
                    # 列表页先导航到专题班列表
                    try:
                        await _list_page.goto(
                            "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                            wait_until="domcontentloaded", timeout=20000)
                        await _list_page.wait_for_timeout(5000)
                        # 应用标签筛选
                        if cfg_tags:
                            await learner.filter_by_tags(_list_page)
                            await _list_page.wait_for_timeout(3000)
                    except:
                        pass

                    async def _refresh_session():
                        """刷新session：重新访问登录页触发cookie刷新"""
                        log("检测到Session过期，尝试刷新...", "yellow")
                        try:
                            await _list_page.goto("https://u.ccb.com/portal/#/study",
                                                  wait_until="domcontentloaded", timeout=15000)
                            await _list_page.wait_for_timeout(5000)
                            # 检查是否需要重新登录
                            body = await _list_page.locator("body").inner_text(timeout=3000)
                            if "立即登录" in body or "密码登录" in body:
                                log("Session已失效，需要重新登录", "red")
                                return False
                            log("Session刷新成功", "green")
                            return True
                        except:
                            return False

                    async def fetch_more_courses(queue):
                        nonlocal no_more_pages, page_num
                        if no_more_pages or thread._stop_event.is_set():
                            return 0
                        async with _fetch_lock:
                            if no_more_pages or thread._stop_event.is_set():
                                return 0
                            # 队列有其他worker补充的课程，不算空
                            if queue.qsize() > 0:
                                return queue.qsize()
                            log("课程池空了，自动翻页采集...", "blue")
                            # 检查目标是否已达成（用绝对目标值比较）
                            if learner.study_goal > 0:
                                try:
                                    _h = await learner._get_study_hours()
                                    if _h.get(phase_goal_type, 0) >= learner.study_goal:
                                        log(f"✓ {type_name}目标已达成!", "bold green")
                                        return 0
                                except:
                                    pass
                            # 列表页翻到下一页
                            try:
                                moved = await learner.go_to_next_page(_list_page)
                            except Exception as e:
                                # 可能是session过期，尝试刷新
                                if "401" in str(e) or "403" in str(e):
                                    if await _refresh_session():
                                        moved = await learner.go_to_next_page(_list_page)
                                    else:
                                        no_more_pages = True
                                        return 0
                                else:
                                    log(f"翻页失败: {e}", "red")
                                    no_more_pages = True
                                    return 0
                            if not moved:
                                log("已到最后一页", "yellow")
                                no_more_pages = True
                                return 0
                            page_num += 1
                            await _list_page.wait_for_timeout(5000)
                            # 从列表页获取专题班（翻页已自动保留筛选状态）
                            try:
                                new_ws = await learner.get_workshops(_list_page)
                            except Exception as e:
                                log(f"获取专题班失败: {e}", "red")
                                return 0
                            if not new_ws:
                                log("下一页无专题班", "yellow")
                                no_more_pages = True
                                return 0
                            log(f"自动翻到第 {page_num} 页: {len(new_ws)} 个专题班", "blue")
                            learner.save_progress(completed_ids, page_num, 0)
                            # 用独立页面采集课程（401时刷新session重试）
                            new_t, new_l = [], {}
                            try:
                                new_t, new_l = await learner._collect_workshops_courses(
                                    _detail_page, new_ws, completed_ids, log_callback=log
                                )
                            except Exception as e:
                                if "401" in str(e) or "403" in str(e):
                                    if await _refresh_session():
                                        try:
                                            new_t, new_l = await learner._collect_workshops_courses(
                                                _detail_page, new_ws, completed_ids, log_callback=log
                                            )
                                        except:
                                            pass
                                    else:
                                        return 0
                                else:
                                    log(f"采集课程失败: {e}", "red")
                                    return 0
                            ws_locks.update(new_l)
                            for t in new_t:
                                queue.put_nowait((*t, 0))
                            log(f"新增 {len(new_t)} 门课程", "green")
                            return len(new_t)

                    await learner.parallel_learn_courses(
                        tasks, ws_locks, fetch_more_courses, progress_cb, hours_cb, log
                    )
                    # 关闭阶段专用页面，避免长会话页面累积
                    for _p in (_list_page, _detail_page):
                        try:
                            await _p.close()
                        except:
                            pass
                    if thread._stop_event.is_set():
                        break
                    log(f"✓ {type_name}阶段完成", "bold green")
                else:
                    log(f"{type_name}: 没有需要学习的课程", "yellow")

            if thread._stop_event.is_set():
                log("本次学习已停止（配置已变更），旧任务结束", "yellow")
            else:
                log("全部学习目标完成!", "bold green")
            thread.done_signal.emit(*getattr(learner, "last_stats", (0, 0)))

        except Exception as e:
            # 页面/浏览器被关闭时静默处理（用户打开设置页面等场景）
            if "Target page, context or browser has been closed" in str(e):
                thread.done_signal.emit(0, 0)
                return
            log(f"错误: {e}", "red")
            import traceback
            log(traceback.format_exc(), "red")
            thread.done_signal.emit(0, 0)
        finally:
            # 关键：必须在 worker 自己的事件循环内优雅关闭浏览器并保存会话。
            # GUI 线程跨循环调用 learner.close() 会因 "Event loop is closed" 全部失败，
            # 导致会话从不保存、浏览器只能靠 pkill 强杀（见体检 A1/A2）。
            if learner:
                try:
                    await learner.close()
                except Exception:
                    pass

    # ── Slots ──

    # 日志/状态颜色（浅色主题可读）
    _LOG_COLORS = {
        "red": "#d64545",
        "yellow": "#c99700",
        "green": "#2e9e5b",
        "bold green": "#23814a",
        "blue": "#3b82c4",
        "bold blue": "#2563a8",
    }

    def _on_log(self, msg, style):
        ts = datetime.now().strftime("%H:%M:%S")
        color = self._LOG_COLORS.get(str(style).lower(), "")
        safe = str(msg).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if color:
            self.log_view.appendHtml(f'<span style="color:{color};">[{ts}] {safe}</span>')
        else:
            self.log_view.appendHtml(f"[{ts}] {safe}")
        if self._log_collapsed:
            self._unread_logs += 1
            self.lbl_log_badge.setText(str(self._unread_logs))
            self.lbl_log_badge.show()
        if str(style).lower() in {"red", "yellow"} and self._log_collapsed:
            self._toggle_log()

    def _animate_progress_bar(self, bar, target, duration=280):
        """让实时进度变化有明确反馈，同时在减少动效时立即更新。"""
        target = max(0, min(100, int(target)))
        if getattr(self.window(), "cfg_reduced_motion", False):
            bar.setValue(target)
            return
        key = id(bar)
        previous = self._progress_animations.get(key)
        if previous is not None:
            try:
                previous.stop()
                previous.deleteLater()
            except RuntimeError:
                pass
        if bar.value() == target:
            return
        anim = QPropertyAnimation(bar, b"value", bar)
        anim.setDuration(duration)
        anim.setStartValue(bar.value())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._progress_animations[key] = anim
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _on_progress(self, data):
        wid = data.get("wid", 0)
        if wid >= self.table.rowCount():
            return

        def _item(text, color=None):
            it = QTableWidgetItem(str(text)[:40])
            if color:
                it.setForeground(QColor(color))
            return it

        self.table.setItem(wid, 1, _item(data.get("course", "-")))
        progress_text = str(data.get("progress", "-"))
        if wid < len(getattr(self, "_progress_bars", [])):
            try:
                pct = max(0, min(100, int(float(progress_text.rstrip("%")))))
                self._animate_progress_bar(self._progress_bars[wid], pct)
                if wid < len(getattr(self, "_progress_labels", [])):
                    self._progress_labels[wid].setText(f"{pct}%")
                if wid == 0:
                    self._animate_progress_bar(self.current_progress, pct)
                    self._animate_progress_bar(self.goal_progress, pct)
            except (TypeError, ValueError):
                pass
        status = str(data.get("status", "-"))
        course = str(data.get("course", "-")).strip()
        eta_text = self._worker_eta(
            wid,
            course,
            progress_text,
            status,
            data.get("eta", "-"),
        )
        self.table.setItem(wid, 3, _item(eta_text))
        if status in {"学习中", "加载中", "查找按钮", "考试答题中"}:
            self.lbl_session_state.setText("学习中")
        elif "异常" in status or "失败" in status:
            self.lbl_session_state.setText("需要处理")
        if course and course != "-":
            self.lbl_current_task.setText(course)
            self.lbl_current_hint.setText(f"线程 {wid + 1} · {status} · {progress_text}")
        self._set_status_cell(wid, status, self._status_kind(status))

    def _animate_ring(self, target):
        """进度环数值平滑动画（OutCubic，400ms）"""
        if getattr(self.window(), "cfg_reduced_motion", False):
            self.progress_ring.setValue(int(target))
            return
        try:
            anim = QPropertyAnimation(self.progress_ring, b"value", self)
            anim.setDuration(400)
            anim.setStartValue(self.progress_ring.value)
            anim.setEndValue(int(target))
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception:
            self.progress_ring.setValue(int(target))

    def _on_hours(self, data):
        import time as _time
        tokens = QApplication.instance().property("moisten_tokens")
        central_color = getattr(tokens, "accent", "#3b82c4")
        online_color = getattr(tokens, "success", "#2e9e5b")
        # 学时数值着色（集中主色 / 网络成功色），一眼可读
        self.lbl_central.setText(
            f'<span style="color:{central_color};"><b>{data.get("central", 0):.1f}</b> 学时</span>')
        self.lbl_online.setText(
            f'<span style="color:{online_color};"><b>{data.get("online", 0):.1f}</b> 学时</span>')
        self.lbl_updated.setText(f"更新: {data.get('updated', '--')}")

        # 本次已学 + 学时趋势
        total = data.get("central", 0) + data.get("online", 0)
        if self._session_start_total is None:
            self._session_start_total = total
        try:
            learned = max(0.0, total - self._session_start_total)
            self.lbl_session.setText(f"<b>{learned:.1f}</b> 学时")
            now = _time.time()
            self._hours_history.append((now, total))
            if len(self._hours_history) > 120:
                self._hours_history = self._hours_history[-120:]
            # 趋势图：最近若干点（至少2个点才画线）
            if len(self._hours_history) >= 2:
                pts = [p[1] for p in self._hours_history[-40:]]
                self.sparkline.set_data(pts)
        except Exception:
            pass

        win = self.window()

        # 手动模式：无学时目标，显示手动信息而不是"不学习"
        mode = getattr(win, "cfg_mode", "auto")
        if mode == "manual":
            self.progress_ring.setValue(0)
            self.lbl_goal_info.setText(self._manual_goal_text())
            self.lbl_eta.setText("")
            return

        c_goal = getattr(win, "cfg_central_goal", 0)
        o_goal = getattr(win, "cfg_online_goal", 0)
        c_mode = getattr(win, "cfg_central_mode", "target")
        o_mode = getattr(win, "cfg_online_mode", "target")

        c_cur = data.get("central", 0)
        o_cur = data.get("online", 0)

        # 差额模式：目标 = 阶段开始时固定的绝对目标（已有 + 差额），
        # 不再用"当前学时 + 差额"动态计算（否则进度环永远到不了100%）
        c_target = getattr(win, "cfg_central_abs_goal", 0) or (c_cur + c_goal if c_mode == "remain" else c_goal)
        o_target = getattr(win, "cfg_online_abs_goal", 0) or (o_cur + o_goal if o_mode == "remain" else o_goal)

        # 没有目标
        if c_target <= 0 and o_target <= 0:
            self.progress_ring.setValue(0)
            self.lbl_goal_info.setText("不学习")
            return

        # 确定当前阶段和目标
        if c_target > 0 and c_cur < c_target:
            goal = c_target
            cur = c_cur
            label = "集中培训"
        elif o_target > 0 and o_cur < o_target:
            goal = o_target
            cur = o_cur
            label = "网络自学"
        else:
            self._animate_ring(100)
            tokens = QApplication.instance().property("moisten_tokens")
            done_color = getattr(tokens, "success", "#2e9e5b")
            self.progress_ring.setCustomBarColor(done_color, done_color)  # 完成变绿
            self.lbl_goal_info.setText(f"✓ 全部完成 集中{c_cur:.1f} 网络{o_cur:.1f}")
            self._eta_seconds = None
            self._eta_timer.stop()
            self.lbl_eta.setText("✓ 已完成")
            return

        # 计算进度（环值平滑动画）
        pct_f = min(100.0, cur / goal * 100) if goal > 0 else 0
        pct = int(pct_f)
        self._animate_ring(pct)
        remaining = max(0, goal - cur)
        self.lbl_goal_info.setText(f"{label} {cur:.1f}/{goal:.0f}学时 剩{remaining:.1f}")

        # ── ETA 计算 ──
        now = _time.time()
        if self._learn_start_time is None:
            self._learn_start_time = now
        self._progress_history.append((now, pct_f))
        if len(self._progress_history) > 20:
            self._progress_history = self._progress_history[-20:]

        if pct_f >= 100:
            self._eta_seconds = None
            self._eta_timer.stop()
            self.lbl_eta.setText(f"✓ {label}完成")
        else:
            eta_sec = self._calc_eta(pct_f, now)
            if eta_sec is not None and eta_sec > 0:
                self._eta_seconds = eta_sec
                self._eta_calc_time = now
                self._update_eta_label()
                if not self._eta_timer.isActive():
                    self._eta_timer.start()
            else:
                self.lbl_eta.setText("计算中...")

    def _calc_eta(self, pct_f, now):
        """计算ETA秒数，用首尾点+平滑过滤"""
        history = self._progress_history
        if len(history) < 2:
            if pct_f > 0 and self._learn_start_time:
                elapsed = now - self._learn_start_time
                return elapsed * (100 - pct_f) / pct_f
            return None
        # 取首尾两点算平均速率（最稳定）
        t0, p0 = history[0]
        t_last, p_last = history[-1]
        dt = t_last - t0
        dp = p_last - p0
        if dp > 0 and dt > 0:
            rate = dp / dt  # pct/秒
            return (100 - p_last) / rate
        return None

    def _tick_eta(self):
        """每秒刷新倒计时"""
        if self._eta_seconds is None:
            self._eta_timer.stop()
            return
        self._update_eta_label()

    def _update_eta_label(self):
        """根据存储的eta_seconds和计算时间，显示实时倒计时"""
        import time as _time
        from datetime import datetime, timedelta
        if self._eta_seconds is None:
            return
        remaining = self._eta_seconds - (_time.time() - self._eta_calc_time)
        if remaining <= 0:
            self.lbl_eta.setText("即将完成...")
            return
        # 中文倒计时
        if remaining < 60:
            cn = f"{remaining:.0f}秒"
        elif remaining < 3600:
            m = int(remaining // 60)
            s = int(remaining % 60)
            cn = f"{m}分{s}秒" if s else f"{m}分"
        else:
            h = int(remaining // 3600)
            m = int((remaining % 3600) // 60)
            cn = f"{h}时{m}分" if m else f"{h}时"
        finish = datetime.now() + timedelta(seconds=remaining)
        self.lbl_eta.setText(f"剩余{cn}·预计{finish.strftime('%H:%M')}完成")

    def _on_eta_reset(self):
        """GUI线程内重置ETA状态（由worker通过信号触发，避免跨线程写Qt对象）"""
        self._learn_start_time = None
        self._progress_history = []
        self._worker_eta_state = {}
        self._eta_seconds = None
        self._eta_calc_time = None
        self._eta_timer.stop()
        self.lbl_eta.setText("")

    def _on_exam_retry(self, exam_name, reason):
        """考试没考成/没通过：弹窗问是否重考。倒计时结束未操作 = 不重考。"""
        TIMEOUT = EXAM_RETRY_TIMEOUT
        dlg = QDialog(self)
        style_moisten_dialog(dlg)
        dlg.setWindowTitle("考试未通过")
        dlg.setMinimumWidth(420)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 22, 24, 20)

        layout.addWidget(SubtitleLabel(f"「{exam_name}」未通过"))
        info = BodyLabel(reason or "本次考试没有完成")
        info.setWordWrap(True)
        layout.addWidget(info)

        hint = CaptionLabel(f"不操作将在 {TIMEOUT} 秒后按「不重考」继续，不阻塞学习")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        btn_retry = PushButton("  重考一次")
        btn_retry.setIcon(FIF.SYNC)
        btn_retry.clicked.connect(lambda: dlg.done(1))
        btn_layout.addWidget(btn_retry)

        btn_skip = PrimaryPushButton(f"  不重考 ({TIMEOUT}s)")
        btn_skip.setIcon(FIF.ACCEPT)
        btn_skip.clicked.connect(lambda: dlg.done(0))
        btn_layout.addWidget(btn_skip)

        layout.addLayout(btn_layout)

        countdown = [TIMEOUT]
        timer = QTimer(dlg)
        timer.setInterval(1000)

        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                timer.stop()
                dlg.done(0)  # 超时按「不重考」
            else:
                btn_skip.setText(f"  不重考 ({countdown[0]}s)")

        timer.timeout.connect(tick)
        timer.start()
        btn_retry.clicked.connect(timer.stop)
        btn_skip.clicked.connect(timer.stop)

        choice = dlg.exec()
        self._exam_retry_choice = bool(choice == 1)
        self._exam_retry_event.set()

    def _on_browser_download(self, status):
        """Chromium 下载进度提示：模态等待框，下载完成前阻止继续使用。
        status: True=开始, str=进度文本, False=下载结束"""
        if status is True:
            try:
                from PySide6.QtWidgets import QDialog, QVBoxLayout
                from qfluentwidgets import ProgressBar, SubtitleLabel, BodyLabel
                dlg = QDialog(self.window())
                style_moisten_dialog(dlg)
                dlg.setWindowTitle("正在下载内置 Chromium")
                dlg.setModal(True)
                dlg.setMinimumWidth(440)
                lay = QVBoxLayout(dlg)
                lay.setContentsMargins(24, 20, 24, 20)
                lay.setSpacing(12)
                lay.addWidget(SubtitleLabel("首次使用内置 Chromium，正在下载浏览器..."))
                bar = ProgressBar()
                bar.setRange(0, 0)  # 忙碌指示（无真实百分比，用字节反馈）
                lay.addWidget(bar)
                self._download_status_lbl = BodyLabel("准备下载...")
                lay.addWidget(self._download_status_lbl)
                self._download_dlg = dlg
                # exec() 进入嵌套事件循环：worker 线程继续下载，完成信号到达后自动关闭
                dlg.exec()
                self._download_dlg = None
            except Exception:
                self._download_dlg = None
        elif status is False:
            dlg = getattr(self, "_download_dlg", None)
            if dlg:
                dlg.accept()
                self._download_dlg = None
        else:
            # 进度文本（如 "已下载 42 MB · 12.3 MB/s"）
            lbl = getattr(self, "_download_status_lbl", None)
            if lbl:
                try:
                    lbl.setText(str(status))
                except Exception:
                    pass

    def _on_done(self, success, failed):
        self._eta_timer.stop()
        self._runtime_timer.stop()
        self.btn_stop.setEnabled(False)
        self.lbl_session_state.setText("已完成" if not failed else "已完成 · 有失败")
        self.lbl_current_task.setText("本次学习已结束")
        self.lbl_current_hint.setText(f"成功 {success} 门 · 失败 {failed} 门")
        # 表格标题栏显示汇总
        if success or failed:
            self.lbl_progress_summary.setText(f"成功 {success} · 失败 {failed}")
        else:
            self.lbl_progress_summary.setText("")
        if success or failed:
            InfoBar.success("完成", f"学习流程结束，成功 {success} 门，失败 {failed} 门", parent=self, position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.success("完成", "学习流程结束", parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _check_update_manual(self):
        """手动检查更新（后台线程，避免阻塞GUI）"""
        InfoBar.info("检查更新", "正在检查更新...", parent=self, position=InfoBarPosition.TOP_RIGHT)
        def _run():
            try:
                self.update_check_signal.emit(check_for_update())
            except Exception as e:
                self.update_check_signal.emit((CURRENT_VERSION, False, "", {}))
                self.update_check_fail_signal.emit(str(e))
        threading.Thread(target=_run, daemon=True).start()

    def _on_update_check_result(self, result):
        latest, needs_update, notes, download_urls = result
        try:
            if needs_update:
                msg = f"当前版本: v{CURRENT_VERSION}\n最新版本: v{latest}"
                if notes:
                    msg += f"\n\n更新内容:\n{notes}"
                dlg = Dialog("发现新版本", msg, self)
                style_moisten_dialog(dlg)
                dlg.cancelButton.setText("稍后")
                dlg.yesButton.setText("立即更新")
                if dlg.exec():
                    self.window()._do_update(download_urls)
            else:
                InfoBar.success("检查更新", f"已是最新版本 v{CURRENT_VERSION}", parent=self, position=InfoBarPosition.TOP_RIGHT)
        except Exception:
            pass

    def _on_update_check_fail(self, err):
        InfoBar.error("检查失败", str(err), parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _on_tag_request(self, tags_by_category):

        # 加载上次选择
        saved_tags = set()
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    saved_tags = set(cfg.get("selected_tags", []))
        except:
            pass

        dlg = QDialog(self)
        style_moisten_dialog(dlg)
        dlg.setWindowTitle("选择标签")
        dlg.setMinimumWidth(500)
        dlg.setMinimumHeight(500)

        outer = QVBoxLayout(dlg)
        outer.setSpacing(12)
        outer.setContentsMargins(20, 20, 20, 20)

        header = QHBoxLayout()
        title = SubtitleLabel("选择要学习的标签")
        header.addWidget(title)
        header.addStretch()
        btn_all = PushButton("全选")
        btn_none = PushButton("全不选")
        header.addWidget(btn_all)
        header.addWidget(btn_none)
        outer.addLayout(header)

        hint = CaptionLabel("不选则学习全部内容")
        hint.setObjectName("muted")
        outer.addWidget(hint)

        # Scroll area with checkboxes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(2)
        scroll_layout.setContentsMargins(8, 8, 8, 8)

        all_tags = []
        checkboxes = []
        for category, tags in tags_by_category.items():
            # Category header
            cat_label = StrongBodyLabel(category)
            cat_label.setStyleSheet("margin-top: 8px;")
            scroll_layout.addWidget(cat_label)

            # 防御：异常数据（如单个字符串）也按可迭代处理，避免槽抛异常导致事件永不放行
            if not isinstance(tags, (list, tuple, set)):
                tags = [tags]
            for tag in tags:
                all_tags.append(tag)
                cb = CheckBox(f"  {tag}")
                cb.setChecked(tag in saved_tags)
                checkboxes.append(cb)
                scroll_layout.addWidget(cb)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        outer.addWidget(scroll, 1)

        # All/None buttons
        def select_all():
            for cb in checkboxes:
                cb.setChecked(True)
        def select_none():
            for cb in checkboxes:
                cb.setChecked(False)
        btn_all.clicked.connect(select_all)
        btn_none.clicked.connect(select_none)

        # Action buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_skip = PushButton("跳过")
        btn_skip.clicked.connect(lambda: dlg.done(0))
        btn_layout.addWidget(btn_skip)
        btn_ok = PrimaryPushButton("确认选择")
        btn_ok.clicked.connect(lambda: dlg.done(1))
        btn_layout.addWidget(btn_ok)
        outer.addLayout(btn_layout)

        # 自动确认倒计时：超时按当前勾选继续，避免 worker 超时后用过期标签
        TIMEOUT = 30
        countdown = [TIMEOUT]
        timer = QTimer(dlg)
        timer.setInterval(1000)

        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                timer.stop()
                dlg.done(1)  # 按当前勾选自动确认
            else:
                btn_ok.setText(f"确认选择 ({countdown[0]}s)")

        timer.timeout.connect(tick)
        timer.start()
        btn_skip.clicked.connect(timer.stop)
        btn_ok.clicked.connect(timer.stop)

        result = dlg.exec()

        if result:
            selected = [all_tags[i] for i, cb in enumerate(checkboxes) if cb.isChecked()]
        else:
            selected = []

        # 保存选择
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["selected_tags"] = selected
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass

        win = self.window()
        win.cfg_tags = selected
        if selected:
            self._on_log(f"已选择 {len(selected)} 个标签", "green")
        else:
            self._on_log("未选择标签，将学习全部", "yellow")
        self._tag_event.set()

    def _on_tag_confirm(self, saved_tags, tags_by_category):
        """有已保存标签时，询问用户：使用已保存 / 重新选择 / 跳过"""

        TIMEOUT = 10  # 秒

        dlg = QDialog(self)
        style_moisten_dialog(dlg)
        dlg.setWindowTitle("标签筛选")
        dlg.setMinimumWidth(400)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = SubtitleLabel("标签筛选")
        layout.addWidget(title)

        tags_text = ", ".join(saved_tags[:5])
        if len(saved_tags) > 5:
            tags_text += f" 等{len(saved_tags)}个"
        info = BodyLabel(f"已保存标签: {tags_text}")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        btn_skip = PushButton("  跳过（学习全部）")
        btn_skip.setIcon(FIF.CLOSE)
        btn_skip.clicked.connect(lambda: dlg.done(0))
        btn_layout.addWidget(btn_skip)

        btn_resel = PushButton("  重新选择")
        btn_resel.setIcon(FIF.EDIT)
        btn_resel.clicked.connect(lambda: dlg.done(1))
        btn_layout.addWidget(btn_resel)

        btn_use = PrimaryPushButton(f"  使用已保存 ({TIMEOUT}s)")
        btn_use.setIcon(FIF.ACCEPT_MEDIUM)
        btn_use.clicked.connect(lambda: dlg.done(2))
        btn_layout.addWidget(btn_use)

        layout.addLayout(btn_layout)

        # 倒计时
        countdown = [TIMEOUT]
        timer = QTimer(dlg)
        timer.setInterval(1000)

        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                timer.stop()
                dlg.done(2)
            else:
                btn_use.setText(f"  使用已保存 ({countdown[0]}s)")

        timer.timeout.connect(tick)
        timer.start()

        # 用户点击任何按钮时停止倒计时
        btn_skip.clicked.connect(timer.stop)
        btn_resel.clicked.connect(timer.stop)
        btn_use.clicked.connect(timer.stop)

        result = dlg.exec()

        win = self.window()
        if result == 2:
            # 使用已保存标签
            win.cfg_tags = list(saved_tags)
        elif result == 1:
            # 重新选择：弹出完整标签选择框
            self._on_tag_request(tags_by_category)
            return  # _on_tag_request 会设置 _tag_event
        else:
            # 跳过
            win.cfg_tags = []

        self._on_log(f"标签: {', '.join(win.cfg_tags)}" if win.cfg_tags else "跳过标签筛选", "green" if win.cfg_tags else "yellow")
        self._tag_event.set()

    def _on_page_confirm(self, last_page):
        """询问是否从上次保存的页码继续"""

        TIMEOUT = 10

        dlg = QDialog(self)
        style_moisten_dialog(dlg)
        dlg.setWindowTitle("继续学习")
        dlg.setMinimumWidth(380)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = SubtitleLabel("继续学习")
        layout.addWidget(title)

        info = BodyLabel(f"上次学习到第 {last_page} 页，是否继续？")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        btn_restart = PushButton("  从第1页开始")
        btn_restart.setIcon(FIF.CLOSE)
        btn_restart.clicked.connect(lambda: dlg.done(0))
        btn_layout.addWidget(btn_restart)

        btn_continue = PrimaryPushButton(f"  继续第{last_page}页 ({TIMEOUT}s)")
        btn_continue.setIcon(FIF.PLAY)
        btn_continue.clicked.connect(lambda: dlg.done(1))
        btn_layout.addWidget(btn_continue)

        layout.addLayout(btn_layout)

        countdown = [TIMEOUT]
        timer = QTimer(dlg)
        timer.setInterval(1000)

        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                timer.stop()
                dlg.done(1)
            else:
                btn_continue.setText(f"  继续第{last_page}页 ({countdown[0]}s)")

        timer.timeout.connect(tick)
        timer.start()

        btn_restart.clicked.connect(timer.stop)
        btn_continue.clicked.connect(timer.stop)

        result = dlg.exec()

        self._page_resume = (result == 1)
        self._page_event.set()


# ─── Main Window ───────────────────────────────────────────────────

from PySide6.QtWidgets import QMainWindow


class _NavigationRail(NavigationInterface):
    """参考稿侧边栏：保留清爽导航，同时在底部补一层低对比度水波纹。"""

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if w < 2 or h < 120:
            p.end()
            return
        dark = _is_dark_theme()

        p.save()
        p.setClipRect(0, max(0, h - 245), w, 245)
        p.translate(10, 0)

        def rounded_wave(start_y, controls, color):
            path = QPainterPath()
            path.moveTo(-24, h * start_y)
            for cx, cy, x, y in controls:
                path.quadTo(cx, cy, x, y)
            path.lineTo(w + 24, h + 24)
            path.lineTo(-24, h + 24)
            path.closeSubpath()
            p.fillPath(path, QColor(*color))

        rounded_wave(0.90, [
            (w * 0.10, h * 0.78, w * 0.28, h * 0.88),
            (w * 0.46, h * 0.99, w * 0.64, h * 0.87),
            (w * 0.88, h * 0.72, w + 24, h * 0.82),
        ], (45, 107, 159, 48) if dark else (164, 215, 247, 50))
        rounded_wave(0.96, [
            (w * 0.16, h * 0.86, w * 0.36, h * 0.94),
            (w * 0.58, h * 1.00, w * 0.76, h * 0.91),
            (w * 0.98, h * 0.82, w + 24, h * 0.88),
        ], (33, 84, 133, 34) if dark else (91, 181, 235, 26))

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(113, 181, 224, 34) if dark else QColor(73, 151, 211, 42))
        for x, y, r in ((22, h - 94, 2), (38, h - 70, 1), (54, h - 112, 2),
                        (72, h - 82, 1), (91, h - 55, 2), (112, h - 96, 1)):
            p.drawEllipse(x, y, r * 2, r * 2)

        p.setPen(QColor(137, 171, 204, 155) if dark else QColor(112, 153, 190, 155))
        font = p.font()
        font.setPointSize(12)
        font.setItalic(True)
        p.setFont(font)
        p.drawText(20, h - 42, "如水润物")
        p.drawText(46, h - 18, "向知而行")
        p.restore()
        p.end()


class _BaseWindow(QMainWindow):
    """原生窗口壳层 + QFluentWidgets 官方 NavigationInterface。"""

    def __init__(self):
        super().__init__()
        self._stack = QStackedWidget()
        self.navigationInterface = _NavigationRail(
            self, showMenuButton=False, showReturnButton=False, collapsible=False)
        self.navigationInterface.setObjectName("navRail")
        self.navigationInterface.setExpandWidth(228)
        self.navigationInterface.expand()
        # 菜单项之间留出更明确的呼吸感，避免放大文字后侧栏显得拥挤。
        self.navigationInterface.panel.topLayout.setSpacing(10)
        QApplication.instance().setProperty("moisten_navigation", self.navigationInterface)
        shell = QWidget(self)
        shell_layout = QHBoxLayout(shell)
        # 壳层必须铺满窗口：参考稿的工作区背景直接贴到右侧/底部边缘，
        # 不能让 QMainWindow 的 page 色在外圈露出来。
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self.navigationInterface)
        shell_layout.addWidget(self._stack, 1)
        self.setCentralWidget(shell)

    def addSubInterface(self, widget, icon, text, **kw):
        self._stack.addWidget(widget)
        route_key = widget.objectName()
        on_click = (lambda key=route_key: self._on_navigation(key)) \
            if hasattr(self, "_on_navigation") else (lambda: self.switchTo(widget))
        self.navigationInterface.addItem(
            route_key, icon, text,
            onClick=on_click,
            position=kw.get("position", NavigationItemPosition.TOP),
        )

    def switchTo(self, widget):
        self._stack.setCurrentWidget(widget)

    def set_navigation_visible(self, visible: bool):
        self.navigationInterface.setVisible(bool(visible))


class MainWindow(_BaseWindow):
    update_check_signal = Signal(object)  # (latest, needs_update, notes, download_urls)

    def switchTo(self, widget):
        """切换到指定页面，附带轻微淡入（仪表盘除外，避免实时刷新闪烁）"""
        super().switchTo(widget)
        if getattr(self, "cfg_reduced_motion", False):
            return
        try:
            if isinstance(widget, DashboardScreen):
                return
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            effect.setOpacity(0.55)
            anim = QPropertyAnimation(effect, b"opacity", widget)
            anim.setDuration(160)
            anim.setStartValue(0.55)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception:
            pass

    def closeEvent(self, event):
        """关闭窗口时二次确认并清理资源"""
        dlg = Dialog("确认退出", "确定要退出吗？学习进度会自动保存。", self)
        style_moisten_dialog(dlg)
        dlg.cancelButton.setText("取消")
        dlg.yesButton.setText("退出")
        if dlg.exec():
            dash = getattr(self, "screen_dashboard", None)
            # 1) 请求协作式停止（置位停止标志、解除对话框等待，让 worker 尽快退出）
            if dash:
                dash._stop_current_learning()
            # 2) 等待 worker 线程结束（限时，避免 GUI 卡死）
            worker = getattr(dash, "_worker", None) if dash else None
            if worker and worker.isRunning():
                # 先给业务协程一个短暂的协作式退出窗口；如果正卡在
                # Playwright 导航/等待中，取消 worker 自己的 asyncio 主任务，
                # 让 _run_learning 的 finally 负责关闭浏览器。
                if not worker.wait(2500):
                    worker.cancel_pending()
                if not worker.wait(10000):
                    # 最后的兜底只处理 Playwright 进程并终止线程，避免窗口
                    # 永远停留在“仍在学习”状态。正常路径不会走到这里。
                    try:
                        from main import _kill_playwright_chrome
                        _kill_playwright_chrome()
                    except Exception:
                        pass
                    try:
                        worker.terminate()
                        worker.wait(3000)
                    except Exception:
                        pass
                if worker.isRunning():
                    InfoBar.warning(
                        "仍在学习",
                        "任务尚未安全停止，请稍后再退出，避免损坏学习进度。",
                        parent=self,
                        position=InfoBarPosition.TOP,
                        duration=5000,
                    )
                    event.ignore()
                    return
            # 3) 清理浏览器（worker 结束后再关，避免两个事件循环并发操作同一 context）
            try:
                learner = dash._learner if dash else None
                if learner:
                    import asyncio
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(learner.close())
                    loop.close()
            except:
                pass
            event.accept()
        else:
            event.ignore()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("润物 Moisten")
        # 默认/最小尺寸要足够容纳各设置页内容（过小会导致文字被裁切）
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        self._drag_pos = None
        self._in_main_shell = False
        self._settings_mode = False
        self._update_in_progress = False
        self._update_download_path = ""
        self._update_wait_started = 0.0
        self._update_wait_timer = None

        # 设置窗口图标
        icon_path = _get_resource_path("icon.png")
        app_icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.setWindowIcon(app_icon)

        # 创建启动画面
        from qfluentwidgets import SplashScreen
        self.splashScreen = SplashScreen(app_icon, self)
        self.splashScreen.setIconSize(QSize(128, 128))
        self.show()

        # Config state
        self.cfg_workers = 5
        self.cfg_headless = True
        self.cfg_browser = "chrome"  # 默认使用系统 Chrome
        self.cfg_chrome_path = ""
        self.cfg_username = ""
        self.cfg_password = ""
        self.cfg_auto_login = True
        self.cfg_central_goal = 0.0
        self.cfg_online_goal = 0.0
        self.cfg_tags = []
        self.cfg_mode = "auto"
        self.cfg_manual_urls = []
        self.cfg_theme_mode = "auto"
        self.cfg_reduced_motion = False
        # 考试自动答题（DeepSeek）
        self.cfg_exam_enabled = False
        self.cfg_deepseek_api_key = ""
        self.cfg_deepseek_model = DEEPSEEK_DEFAULT_MODEL
        self.cfg_deepseek_thinking = False

        # 创建子界面
        self._createSubInterfaces()

        # 隐藏启动画面
        self.splashScreen.finish()

        # 检查更新（非阻塞，后台线程 + 信号）
        self.update_check_signal.connect(self._on_update_result)
        QTimer.singleShot(2000, self._check_update)

        # 检查是否有保存的配置，有则自动开始
        has_config = self._load_saved_config()
        apply_theme(QApplication.instance(), getattr(self, "cfg_theme_mode", "auto"))
        self.update_learning_navigation()
        if has_config:
            self._screen_index = 5
            self._in_main_shell = True
            self._settings_mode = False
            self.set_navigation_visible(True)
            self.navigationInterface.setCurrentItem("dashboard")
            self.switchTo(self.screen_dashboard)
            self.screen_dashboard.start_learning()
        elif os.path.exists(CONFIG_PATH):
            # 返回用户但未保存账号 → 直接到配置页
            self._screen_index = 1
            self.switchTo(self.screen_config)
        else:
            # 首次使用 → 欢迎页
            self._screen_index = 0
            self.switchTo(self.screen_welcome)

    def _createSubInterfaces(self):
        """创建所有子界面"""
        # 让启动画面显示一下（用事件循环避免阻塞UI）
        loop = QEventLoop(self)
        QTimer.singleShot(1500, loop.quit)
        loop.exec()
        self.screen_welcome = WelcomeScreen(self)
        self.screen_welcome.setObjectName("welcome")
        self.screen_config = ConfigScreen(self)
        self.screen_config.setObjectName("config")
        self.screen_login = LoginScreen(self)
        self.screen_login.setObjectName("login")
        self.screen_mode = ModeScreen(self)
        self.screen_mode.setObjectName("mode")
        self.screen_goal = GoalScreen(self)
        self.screen_goal.setObjectName("goal")
        self.screen_manual = ManualScreen(self)
        self.screen_manual.setObjectName("manual")
        # 主界面设置：与首次使用向导共用表单逻辑，但不再串行跳转。
        self.screen_account = LoginScreen(self, sidebar_mode=True)
        self.screen_account.setObjectName("account")
        self.screen_runtime = ConfigScreen(self, section="runtime", sidebar_mode=True)
        self.screen_runtime.setObjectName("runtime")
        self.screen_exam = ConfigScreen(self, section="exam", sidebar_mode=True)
        self.screen_exam.setObjectName("exam")
        self.screen_appearance = ConfigScreen(self, section="appearance", sidebar_mode=True)
        self.screen_appearance.setObjectName("appearance")
        self.screen_dashboard = DashboardScreen(self)
        self.screen_dashboard.setObjectName("dashboard")

        # Official QFluentWidgets navigation owns the application shell.
        for wizard_screen in (self.screen_welcome, self.screen_config, self.screen_login,
                              self.screen_goal, self.screen_manual):
            self._stack.addWidget(wizard_screen)
        brand_widget = BrandNavigationWidget(
            _get_resource_path("icon.png"), self.navigationInterface)
        self.navigationInterface.addWidget(
            "brand", brand_widget, position=NavigationItemPosition.TOP)

        self.addSubInterface(self.screen_dashboard, FIF.HOME, "仪表盘")
        # 侧栏按设置使用顺序排列：账号 → 浏览器 → 学习方式 →
        # 学习目标/手动学习 → 考试 → 外观。
        self.addSubInterface(self.screen_account, FIF.PEOPLE, "账号登录")
        self.addSubInterface(self.screen_runtime, FIF.SETTING, "运行与浏览器")
        self.addSubInterface(self.screen_mode, FIF.TILES, "学习方式")
        self.navigationInterface.addItem(
            "learning", FIF.FLAG, "学习目标",
            onClick=lambda: self._on_navigation("learning"),
            position=NavigationItemPosition.TOP,
        )
        self.addSubInterface(self.screen_exam, FIF.CHECKBOX, "考试设置")
        self.addSubInterface(self.screen_appearance, FIF.PALETTE, "外观设置")

        # 官方导航项的点击信号负责更新当前页面状态；显式连接可避免不同
        # QFluentWidgets 版本对 onClick 参数签名的差异。
        for route_key in ("dashboard", "mode", "account", "runtime", "exam", "appearance"):
            item = self.navigationInterface.panel.items[route_key].widget
            item.clicked.connect(lambda _checked, key=route_key: self._on_navigation(key))
        self.navigationInterface.panel.items["learning"].widget.clicked.connect(
            lambda _checked: self._on_navigation("learning"))

        self._screen_index = 0
        self._main_pages = {
            "dashboard": self.screen_dashboard,
            "mode": self.screen_mode,
            "learning": self.screen_goal,
            "account": self.screen_account,
            "runtime": self.screen_runtime,
            "exam": self.screen_exam,
            "appearance": self.screen_appearance,
        }
        self.update_learning_navigation()
        self.set_navigation_visible(False)

    def _on_navigation(self, key):
        if key == "learning":
            self.update_learning_navigation()
        widget = self._main_pages.get(key)
        if not widget:
            return
        if key == "mode":
            self._screen_index = 3
        elif key == "learning":
            self._screen_index = 4
            widget = self.screen_goal if self.cfg_mode == "auto" else self.screen_manual
        elif key in ("account", "runtime", "exam", "appearance"):
            self._screen_index = 1
        elif key == "dashboard":
            self._screen_index = 5
        self._settings_mode = key != "dashboard"
        if hasattr(widget, "step_bar"):
            widget.step_bar.setVisible(not self._settings_mode)
        self.switchTo(widget)
        self.navigationInterface.setCurrentItem(key)

    def update_learning_navigation(self):
        """根据当前模式更新侧栏的动态学习入口。"""
        if not hasattr(self, "navigationInterface") or "learning" not in self.navigationInterface.panel.items:
            return
        item = self.navigationInterface.panel.items["learning"].widget
        if getattr(self, "cfg_mode", "auto") == "manual":
            item.setText("手动学习")
            item.setIcon(FIF.LINK)
        else:
            item.setText("学习目标")
            item.setIcon(FIF.FLAG)

    def _check_update(self):
        """检查是否有新版本（后台线程，避免阻塞GUI）"""
        def _run():
            try:
                self.update_check_signal.emit(check_for_update())
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _on_update_result(self, result):
        latest, needs_update, notes, download_urls = result
        try:
            if needs_update:
                msg = f"当前版本: v{CURRENT_VERSION}\n最新版本: v{latest}"
                if notes:
                    msg += f"\n\n更新内容:\n{notes}"
                dlg = Dialog(
                    "发现新版本",
                    msg,
                    self
                )
                style_moisten_dialog(dlg)
                dlg.cancelButton.setText("稍后")
                dlg.yesButton.setText("立即更新")
                if dlg.exec():
                    self._do_update(download_urls)
        except:
            pass

    def _do_update(self, download_urls):
        """下载新版本并替换自己"""
        import platform as _plat
        from PySide6.QtWidgets import QProgressDialog

        # 选择对应平台的下载链接
        if _plat.system() == "Windows":
            url = download_urls.get("Windows", "")
        else:
            url = download_urls.get("macOS", "")

        if not url:
            InfoBar.warning("提示", "未找到对应平台的下载链接", parent=self, position=InfoBarPosition.TOP)
            import webbrowser
            webbrowser.open(DOWNLOAD_URL)
            return

        # 下载进度对话框（Fluent 风格）
        dlg = QDialog(self)
        style_moisten_dialog(dlg)
        dlg.setWindowTitle("正在更新")
        dlg.setMinimumWidth(400)
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        lbl_title = SubtitleLabel("正在下载新版本...")
        layout.addWidget(lbl_title)

        progress_bar = ProgressBar()
        progress_bar.setValue(0)
        layout.addWidget(progress_bar)

        lbl_status = BodyLabel("准备下载...")
        layout.addWidget(lbl_status)

        btn_cancel = PushButton("取消")
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        cancel_flag = [False]
        download_done = [False]  # 下载是否已成功完成（区分"点X关闭"与"完成"）

        # 对话框被关闭（含标题栏 X）且未完成下载 → 取消下载
        dlg.finished.connect(lambda: cancel_flag.__setitem__(0, True) if not download_done[0] else None)

        # 下载线程完成后切回 GUI 线程执行安装/提示（Qt 控件只能在 GUI 线程操作）
        from PySide6.QtCore import QObject as _QObject
        class _UpdateBridge(_QObject):
            apply = Signal(str)
            fail = Signal(str)
        _bridge = _UpdateBridge()
        _bridge.apply.connect(self._apply_update)
        _bridge.fail.connect(lambda err: InfoBar.error("更新失败", err, parent=self, position=InfoBarPosition.TOP))

        def do_download():
            import tempfile as _tf
            import time as _tm
            from PySide6.QtCore import QMetaObject, Q_ARG
            try:
                filename = url.split("/")[-1]
                # Windows 同目录更新：优先下载到 exe 所在目录（可直接启动新版本，
                # 无需临时目录 + bat 覆盖正在运行的 exe）；目录不可写则退回临时目录
                download_path = os.path.join(_tf.gettempdir(), filename)
                current = sys.executable
                if sys.platform == "win32" and current.lower().endswith(".exe"):
                    install_dir = os.path.dirname(os.path.abspath(current))
                    try:
                        probe = os.path.join(install_dir, ".moisten_write_test")
                        with open(probe, "w") as f:
                            f.write("")
                        os.remove(probe)
                        download_path = os.path.join(install_dir, "Moisten.new.exe")
                    except Exception:
                        pass  # 目录不可写（如 Program Files），退回临时目录+bat方案

                # 候选下载源：gh-proxy 加速节点优先（国内直连 GitHub 经常超时），
                # 加速不通再退回直连；先 HEAD 探测一次，避免在死节点上空等超时
                candidates = update_download_candidates(url)
                if not candidates:
                    raise RuntimeError("下载地址无效")

                # 下载 + 完整性校验（大小一致 + 可执行文件头），失败自动重试/换源
                ok = False
                last_err = ""
                for dl_url in candidates:
                    source = _download_source_label(dl_url)
                    QMetaObject.invokeMethod(lbl_status, "setText", Qt.QueuedConnection,
                                             Q_ARG(str, f"{source}：正在连接..."))
                    for attempt in range(3):
                        if cancel_flag[0]:
                            return
                        try:
                            req = urllib.request.Request(dl_url, headers={"User-Agent": "Moisten"})
                            with urllib.request.urlopen(req, timeout=300) as resp:
                                total = int(resp.headers.get("Content-Length", 0))
                                downloaded = 0
                                with open(download_path, "wb") as f:
                                    while True:
                                        if cancel_flag[0]:
                                            os.remove(download_path)
                                            return
                                        chunk = resp.read(8192)
                                        if not chunk:
                                            break
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        if total > 0:
                                            pct = int(downloaded / total * 100)
                                            size_mb = downloaded / 1024 / 1024
                                            total_mb = total / 1024 / 1024
                                            QMetaObject.invokeMethod(progress_bar, "setValue", Qt.QueuedConnection, Q_ARG(int, pct))
                                            QMetaObject.invokeMethod(lbl_status, "setText", Qt.QueuedConnection, Q_ARG(str, f"已下载 {size_mb:.1f} / {total_mb:.1f} MB ({pct}%)"))
                            # 完整性校验：拦截代理错误页/截断文件
                            if total > 0 and downloaded != total:
                                raise RuntimeError(f"下载不完整（{downloaded}/{total} 字节），已中止")
                            if not _looks_like_executable(download_path):
                                raise RuntimeError("下载文件校验失败（非有效安装包，下载源可能返回了错误页）")
                            ok = True
                            break
                        except Exception as e:
                            last_err = str(e)
                            try:
                                os.remove(download_path)
                            except Exception:
                                pass
                            _tm.sleep(2)
                    if ok:
                        break
                if not ok:
                    tried = "、".join(_download_source_label(u) for u in candidates)
                    raise RuntimeError(f"下载失败（已尝试 {tried}）: {last_err}")

                QMetaObject.invokeMethod(lbl_status, "setText", Qt.QueuedConnection, Q_ARG(str, "下载完成，正在安装..."))
                QMetaObject.invokeMethod(progress_bar, "setValue", Qt.QueuedConnection, Q_ARG(int, 100))
                download_done[0] = True  # 完成后再关闭对话框不会触发取消
                _bridge.apply.emit(download_path)

            except Exception as e:
                QMetaObject.invokeMethod(dlg, "reject", Qt.QueuedConnection)
                # 经信号切回 GUI 线程弹提示（下载线程不允许创建 Qt 控件）
                _bridge.fail.emit(str(e))

        btn_cancel.clicked.connect(lambda: (cancel_flag.__setitem__(0, True), dlg.reject()))
        dlg.show()
        import threading
        threading.Thread(target=do_download, daemon=True).start()

    def _apply_update(self, download_path):
        """下载完成后先结束学习和浏览器，再启动更新进程。"""
        if self._update_in_progress:
            return
        self._update_in_progress = True
        self._update_download_path = download_path
        self._update_wait_started = __import__("time").monotonic()
        self._set_update_status("正在关闭学习任务和浏览器…")
        dash = getattr(self, "screen_dashboard", None)
        worker = getattr(dash, "_worker", None) if dash else None
        if worker and worker.isRunning():
            dash._stop_current_learning()
            self._update_wait_timer = QTimer(self)
            self._update_wait_timer.setInterval(200)
            self._update_wait_timer.timeout.connect(self._poll_update_shutdown)
            self._update_wait_timer.start()
        else:
            self._finish_update_shutdown(False)

    def _set_update_status(self, message):
        dash = getattr(self, "screen_dashboard", None)
        if dash is not None:
            dash.lbl_session_state.setText(message)

    def _poll_update_shutdown(self):
        dash = getattr(self, "screen_dashboard", None)
        worker = getattr(dash, "_worker", None) if dash else None
        if not worker or not worker.isRunning():
            self._finish_update_shutdown(False)
            return
        elapsed = __import__("time").monotonic() - self._update_wait_started
        if elapsed < 20:
            self._set_update_status(f"正在关闭学习任务… {int(elapsed)} / 20 秒")
            return
        try:
            from main import _kill_playwright_chrome
            _kill_playwright_chrome()
        except Exception:
            pass
        try:
            worker.terminate()
            worker.wait(3000)
        except Exception:
            pass
        self._finish_update_shutdown(True)

    def _finish_update_shutdown(self, forced):
        if self._update_wait_timer:
            self._update_wait_timer.stop()
            self._update_wait_timer.deleteLater()
            self._update_wait_timer = None
        self._set_update_status("浏览器关闭超时，正在强制重启…" if forced else "浏览器已关闭，正在重启…")
        QTimer.singleShot(250, self._launch_update_process)

    def _launch_update_process(self):
        """用下载的文件替换自己（通过外部脚本 / 同目录直接启动新版本）"""
        import platform as _plat
        import subprocess
        import tempfile
        import shutil

        current = sys.executable

        if _plat.system() == "Windows" and current.endswith(".exe"):
            same_dir = (os.path.dirname(os.path.abspath(download_path))
                        == os.path.dirname(os.path.abspath(current)))
            if same_dir:
                # 同目录更新（推荐）：直接启动新 exe，由新实例启动时删除旧版并改回规范名，
                # 完全绕开"覆盖正在运行的 exe"与临时目录替换问题
                try:
                    subprocess.Popen(
                        [download_path, "--post-update-old", current],
                        cwd=os.path.dirname(os.path.abspath(current)),
                        creationflags=0x08000000,
                    )
                except Exception as exc:
                    self._update_in_progress = False
                    InfoBar.error("更新失败", str(exc)[:180], parent=self, position=InfoBarPosition.TOP)
                    return
                InfoBar.success("更新中", "程序将自动重启", parent=self, position=InfoBarPosition.TOP)
                QTimer.singleShot(300, sys.exit)
                return

            # 兜底（安装目录不可写）：bat 等主程序退出 → 备份旧版 → 替换 → 重启
            bat_path = os.path.join(tempfile.gettempdir(), "moisten_update.bat")
            with open(bat_path, "w") as f:
                f.write(f"""@echo off
echo 正在更新...
:wait
tasklist /fi "PID eq {os.getpid()}" | find "{os.getpid()}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
copy /y "{current}" "{current}.bak" >nul 2>&1
copy /y "{download_path}" "{current}" >nul
del "{download_path}" >nul
start "" "{current}"
del "%~f0"
""")
            # 启动bat脚本，退出自己
            try:
                subprocess.Popen(["cmd", "/c", bat_path], creationflags=0x08000000)
            except Exception as exc:
                self._update_in_progress = False
                InfoBar.error("更新失败", str(exc)[:180], parent=self, position=InfoBarPosition.TOP)
                return
            InfoBar.success("更新中", "程序将自动重启", parent=self, position=InfoBarPosition.TOP)
            QTimer.singleShot(500, sys.exit)

        elif _plat.system() == "Darwin":
            # macOS 发布物是 .dmg，不能直接覆盖二进制（会导致应用损坏），
            # 打开下载页由用户手动替换 .app
            import webbrowser
            webbrowser.open(DOWNLOAD_URL)
            InfoBar.info("更新", "请下载新版本 DMG 并手动替换应用", parent=self, position=InfoBarPosition.TOP)
            self._update_in_progress = False
        else:
            # 源码运行，打开下载目录
            try:
                os.system(f'open "{os.path.dirname(download_path)}"')
            except Exception:
                pass
            self._update_in_progress = False

    def _load_saved_config(self):
        """加载保存的配置，返回是否有完整配置"""
        try:
            if not os.path.exists(CONFIG_PATH):
                return False
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # 先恢复模式与手动URL（即使配置不完整返回 False，也要恢复，
            # 供手动页回填与手动模式自动开始使用）
            self.cfg_mode = cfg.get("mode", "auto")
            self.cfg_manual_urls = cfg.get("manual_urls", [])
            if "workers" not in cfg:
                return False
            try:
                self.cfg_workers = max(1, min(20, int(cfg.get("workers", 5))))
            except (TypeError, ValueError):
                self.cfg_workers = 5
            self.cfg_headless = cfg.get("headless", True)
            default_browser = "chrome"  # 默认使用系统 Chrome
            self.cfg_browser = cfg.get("browser", default_browser)
            self.cfg_chrome_path = cfg.get("chrome_path", "")
            self.cfg_central_goal = cfg.get("central_goal", 0)
            self.cfg_online_goal = cfg.get("online_goal", 0)
            self.cfg_central_mode = cfg.get("central_mode", "target")
            self.cfg_online_mode = cfg.get("online_mode", "target")
            # 向后兼容旧格式：仅当新格式字段未设置时才回退旧 study_goal
            if cfg.get("study_goal", 0) > 0 and not (self.cfg_central_goal or self.cfg_online_goal):
                if cfg.get("goal_type") == "central":
                    self.cfg_central_goal = cfg["study_goal"]
                else:
                    self.cfg_online_goal = cfg["study_goal"]
            self.cfg_tags = cfg.get("selected_tags", [])
            self.cfg_theme_mode = normalize_theme_mode(cfg.get("theme_mode", "auto"))
            self.cfg_reduced_motion = bool(cfg.get("reduced_motion", False))
            # 考试自动答题（DeepSeek）
            self.cfg_exam_enabled = bool(cfg.get("exam_enabled", False))
            self.cfg_deepseek_api_key = deobfuscate_secret(cfg.get("deepseek_api_key", ""))
            self.cfg_deepseek_model = cfg.get("deepseek_model", "") or DEEPSEEK_DEFAULT_MODEL
            self.cfg_deepseek_thinking = bool(cfg.get("deepseek_thinking", False))
            # 加载账号
            creds_path = USER_CREDENTIALS_PATH
            if os.path.exists(creds_path):
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds = json.load(f)
                self.cfg_username = creds.get("username", "")
                self.cfg_password = creds.get("password", "")
                # 密码：优先系统钥匙串，回退旧 XOR 字段
                if self.cfg_username:
                    try:
                        from main import AutoLearner
                        kp = AutoLearner._load_password(self.cfg_username)
                        if kp:
                            self.cfg_password = kp
                    except:
                        pass
                if self.cfg_password:
                    try:
                        from main import AutoLearner
                        self.cfg_password = AutoLearner._xor_decrypt(self.cfg_password)
                    except:
                        pass
            return bool(self.cfg_username)
        except:
            return False

    def mousePressEvent(self, event):
        if sys.platform != "darwin":  # macOS原生窗口自带拖拽
            if event.button() == Qt.LeftButton:
                self._drag_pos = event.globalPos() - self.pos()
                event.accept()

    def mouseMoveEvent(self, event):
        if sys.platform != "darwin":
            if self._drag_pos and event.buttons() == Qt.LeftButton:
                self.move(event.globalPos() - self._drag_pos)
                event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def next_screen(self):
        """根据当前界面和模式决定下一个界面
        索引：0欢迎 1配置 2登录 3模式 4目标/手动 5仪表盘"""
        self._screen_index += 1

        if self._screen_index == 1:
            # 欢迎 → 配置
            self.switchTo(self.screen_config)
        elif self._screen_index == 2:
            # 配置 → 登录
            self.switchTo(self.screen_login)
        elif self._screen_index == 3:
            # 登录 → 模式
            self.switchTo(self.screen_mode)
        elif self._screen_index == 4:
            # 模式 → 目标(自动) 或 手动URL(手动)
            if self.cfg_mode == "auto":
                self.switchTo(self.screen_goal)
            else:
                self.switchTo(self.screen_manual)
        elif self._screen_index == 5:
            # 目标/手动 → 仪表盘，或启动时恢复配置自动进入
            self._in_main_shell = True
            self._settings_mode = False
            self.set_navigation_visible(True)
            self.navigationInterface.setCurrentItem("dashboard")
            self.switchTo(self.screen_dashboard)
            self.screen_dashboard.start_learning()

    def go_to_manual(self):
        """从模式选择跳到手动URL输入"""
        self._screen_index = 4
        if hasattr(self, "update_learning_navigation"):
            self.update_learning_navigation()
        self.switchTo(self.screen_manual)

    def show_mode_screen(self):
        """从手动URL输入返回模式选择"""
        self._screen_index = 3
        self._settings_mode = False
        self.set_navigation_visible(self._in_main_shell)
        self.switchTo(self.screen_mode)

    def show_settings(self):
        """兼容旧调用：从仪表盘打开运行与浏览器设置。"""
        self._screen_index = 1
        self._in_main_shell = True
        self._settings_mode = True
        self.set_navigation_visible(True)
        self.navigationInterface.setCurrentItem("runtime")
        self.switchTo(self.screen_runtime)

    def return_to_dashboard(self, restart: bool = False):
        """从侧栏设置页返回仪表盘，并按需重新启动当前学习会话。"""
        self._screen_index = 5
        self._in_main_shell = True
        self._settings_mode = False
        self.set_navigation_visible(True)
        self.navigationInterface.setCurrentItem("dashboard")
        self.switchTo(self.screen_dashboard)
        if restart:
            self.screen_dashboard.start_learning()


# ─── Entry ─────────────────────────────────────────────────────────


def _get_resource_path(filename):
    """获取资源文件路径（兼容 PyInstaller 打包）"""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def _updated_exe_name(old_path: str) -> str:
    """根据旧文件名与新版本号，计算更新后的文件名（保留用户命名风格）。

    - 旧文件带版本号（如 Moisten-1.7.5-Windows.exe）→ 版本号换成新版本
      （Moisten-1.7.6-Windows.exe），文件名与真实版本一致，不再出现"新版旧名"
    - 旧文件无版本号（如 Moisten.exe）→ 保持原名，快捷方式不失效
    """
    base = os.path.basename(old_path)
    m = re.match(r"^(.*?)-(\d+\.\d+\.\d+)(.*?)(\.\w+)$", base)
    if m:
        prefix, _old_ver, middle, ext = m.groups()
        return f"{prefix}-{CURRENT_VERSION}{middle}{ext}"
    return base


def _handle_self_update():
    """新版本启动时清理旧版（Windows 同目录更新方案）。

    旧版启动新 exe（Moisten.new.exe --post-update-old <旧exe路径>）后退出；
    新实例在后台线程等待旧进程释放文件锁 → 删除旧 exe → 把自己改回规范名，
    之后快捷方式/下次启动仍指向 Moisten.exe。
    """
    try:
        if "--post-update-old" not in sys.argv:
            return
        i = sys.argv.index("--post-update-old")
        if i + 1 >= len(sys.argv):
            return
        old_path = os.path.abspath(sys.argv[i + 1])
        del sys.argv[i:i + 2]  # 从 argv 移除，避免传给 Qt
        current = os.path.abspath(sys.executable)
        if old_path == current or not old_path.lower().endswith(".exe"):
            return

        def _cleanup():
            import time as _t
            # 目标文件名：旧名带版本号则换新版本号，否则保持原名
            target = os.path.join(os.path.dirname(old_path), _updated_exe_name(old_path))
            for _ in range(60):  # 最多等 60 秒（旧进程约 300ms 后退出）
                try:
                    if os.path.exists(old_path) and os.path.abspath(old_path) != os.path.abspath(target):
                        os.remove(old_path)  # 删除旧版（旧进程已退出，文件已解锁）
                    # 把自己改名为目标名（Windows 允许重命名运行中的 exe）
                    if os.path.abspath(target) != current:
                        os.rename(current, target)
                    return
                except OSError:
                    _t.sleep(1)
                except Exception:
                    return

        # 更新前旧进程已经完成浏览器清理，这里同步完成文件替换，
        # 避免新版本 GUI 和后台重命名线程同时运行。
        _cleanup()
    except Exception:
        pass


def main():
    # 打包为 windowed 模式（-w）时没有控制台，启动期致命错误必须弹窗可见
    try:
        _handle_self_update()
        _main()
    except Exception as e:
        import traceback
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "Moisten 启动失败",
                                 f"{e}\n\n{traceback.format_exc()}")
        except Exception:
            pass
        sys.exit(1)


def _main():
    import platform, multiprocessing
    multiprocessing.freeze_support()
    # 抑制 Qt 字体警告
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")
    # macOS 高DPI支持
    if platform.system() == "Darwin":
        os.environ.pop("QT_FONT_DPI", None)

    app = QApplication(sys.argv)
    app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # 平台适配字体
    from PySide6.QtGui import QFont
    if platform.system() == "Darwin":
        font_family = "PingFang SC"
    elif platform.system() == "Windows":
        font_family = "Microsoft YaHei"
    else:
        font_family = "Noto Sans CJK SC"
    font = QFont(font_family, 13)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # 全局字体和青黛主题；具体主题会在读取配置后再次应用。
    app.setStyleSheet(f"* {{ font-family: '{font_family}'; }}")

    app.setStyle("Windows")
    apply_theme(app, "auto")

    # 设置应用图标（全局生效）
    icon_path = _get_resource_path("icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
