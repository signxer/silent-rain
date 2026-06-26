#!/usr/bin/env python3
"""CCBU-Auto Fluent Design GUI (QFluentWidgets)"""
import asyncio
import json
import os
import sys
import threading
from datetime import datetime

from PyQt5.QtCore import Qt, QThread, pyqtSignal as Signal, QSize
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QStackedWidget, QTableWidgetItem,
    QHeaderView, QSizePolicy, QSpacerItem,
    QDialog, QDialogButtonBox, QListWidget, QListWidgetItem,
)

from qfluentwidgets import (
    FluentWindow, MSFluentWindow,
    NavigationItemPosition, FluentIcon as FIF,
    CardWidget, HeaderCardWidget, SimpleCardWidget,
    PrimaryPushButton, PushButton, ToolButton,
    LineEdit, SpinBox, SwitchButton,
    RadioButton, CheckBox,
    TableWidget, ProgressBar, ProgressRing,
    PlainTextEdit, TextEdit,
    SubtitleLabel, BodyLabel, CaptionLabel, StrongBodyLabel,
    TitleLabel,
    InfoBar, InfoBarPosition,
    MessageBox, Dialog,
    HyperlinkButton,
    isDarkTheme, setTheme, Theme,
)

from main import CCBULearner, CONFIG_PATH, PROGRESS_PATH, STORAGE_STATE_PATH


# ─── Async Thread ──────────────────────────────────────────────────


class AsyncThread(QThread):
    log_signal = Signal(str, str)
    progress_signal = Signal(dict)
    hours_signal = Signal(dict)
    done_signal = Signal(int, int)
    tag_request_signal = Signal(dict)

    def __init__(self, coro_func, parent=None):
        super().__init__(parent)
        self._coro_func = coro_func

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._coro_func(self))
        except Exception as e:
            self.log_signal.emit(f"错误: {e}", "red")
        finally:
            loop.close()


# ─── Config Screen ─────────────────────────────────────────────────


class ConfigScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_config()
        self._build_ui()

    def _load_config(self):
        self._saved = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self._saved = json.load(f)
            except:
                pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        # Title
        title = TitleLabel("运行配置")
        layout.addWidget(title)

        subtitle = BodyLabel("设置工作线程数和浏览器模式")
        subtitle.setForegroundRole(self.palette().PlaceholderText)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        # Workers card
        workers_card = HeaderCardWidget(self)
        workers_card.setTitle("⚡ 工作线程数")
        workers_card.setBorderRadius(8)
        w_layout = QVBoxLayout()
        w_layout.setSpacing(12)

        self.spin_workers = SpinBox()
        self.spin_workers.setRange(1, 20)
        self.spin_workers.setValue(self._saved.get("workers", 1))
        self.spin_workers.setFixedWidth(200)
        w_label = BodyLabel("同时学习的课程数量，建议 3-10")
        w_label.setForegroundRole(self.palette().PlaceholderText)
        w_layout.addWidget(self.spin_workers)
        w_layout.addWidget(w_label)
        workers_card.viewLayout.addLayout(w_layout)
        layout.addWidget(workers_card)

        # Headless card
        headless_card = HeaderCardWidget(self)
        headless_card.setTitle("🌐 浏览器模式")
        headless_card.setBorderRadius(8)
        h_layout = QHBoxLayout()
        h_layout.setSpacing(12)

        self.switch_headless = SwitchButton()
        self.switch_headless.setChecked(self._saved.get("headless", False))
        self.switch_headless.setOnText("后台运行")
        self.switch_headless.setOffText("显示浏览器")
        h_label = BodyLabel("无头模式下浏览器不显示界面，适合后台挂机")
        h_label.setForegroundRole(self.palette().PlaceholderText)
        h_layout.addWidget(self.switch_headless)
        h_layout.addWidget(h_label)
        h_layout.addStretch()
        headless_card.viewLayout.addLayout(h_layout)
        layout.addWidget(headless_card)

        layout.addStretch()

        # Start button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_start = PrimaryPushButton("  开始")
        self.btn_start.setIcon(FIF.PLAY)
        self.btn_start.setFixedSize(160, 40)
        self.btn_start.clicked.connect(self._on_start)
        btn_layout.addWidget(self.btn_start)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _on_start(self):
        workers = self.spin_workers.value()
        headless = self.switch_headless.isChecked()
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["workers"] = workers
            cfg["headless"] = headless
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        win = self.window()
        win.cfg_workers = workers
        win.cfg_headless = headless
        win.next_screen()


# ─── Login Screen ──────────────────────────────────────────────────


class LoginScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_creds()
        self._build_ui()

    def _load_creds(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccbu_credentials.json")
        self._creds = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._creds = json.load(f)
            except:
                pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        title = TitleLabel("用户登录")
        layout.addWidget(title)

        subtitle = BodyLabel("输入建行统一认证账号密码")
        subtitle.setForegroundRole(self.palette().PlaceholderText)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        # Account card
        account_card = HeaderCardWidget(self)
        account_card.setTitle("👤 账号信息")
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
        self.btn_login = PrimaryPushButton("  登录")
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

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccbu_credentials.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"username": username, "password": password}, f, ensure_ascii=False, indent=2)
        except:
            pass

        win = self.window()
        win.cfg_username = username
        win.cfg_password = password
        win.cfg_auto_login = auto
        win.next_screen()


# ─── Goal Screen ───────────────────────────────────────────────────


class GoalScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_goal()
        self._build_ui()

    def _load_goal(self):
        self._saved_goal = 0
        self._saved_type = "central"
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self._saved_goal = cfg.get("study_goal", 0)
                self._saved_type = cfg.get("goal_type", "central")
            except:
                pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        title = TitleLabel("学习目标")
        layout.addWidget(title)

        subtitle = BodyLabel("设置本次学习的目标学时（可跳过）")
        subtitle.setForegroundRole(self.palette().PlaceholderText)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        # Goal type card
        type_card = HeaderCardWidget(self)
        type_card.setTitle("🎯 目标类型")
        type_card.setBorderRadius(8)
        t_layout = QHBoxLayout()
        t_layout.setSpacing(20)
        t_layout.setContentsMargins(0, 8, 0, 8)

        self.radio_central = RadioButton("集中培训")
        self.radio_online = RadioButton("网络自学")
        if self._saved_type == "central":
            self.radio_central.setChecked(True)
        else:
            self.radio_online.setChecked(True)
        t_layout.addWidget(self.radio_central)
        t_layout.addWidget(self.radio_online)
        t_layout.addStretch()
        type_card.viewLayout.addLayout(t_layout)
        layout.addWidget(type_card)

        # Hours card
        hours_card = HeaderCardWidget(self)
        hours_card.setTitle("目标学时")
        hours_card.setBorderRadius(8)
        h_layout = QHBoxLayout()
        h_layout.setSpacing(12)
        h_layout.setContentsMargins(0, 8, 0, 8)

        self.spin_hours = SpinBox()
        self.spin_hours.setRange(0, 9999)
        self.spin_hours.setValue(int(self._saved_goal))
        self.spin_hours.setSpecialValueText("不限制")
        self.spin_hours.setFixedWidth(200)
        h_label = BodyLabel("设为 0 表示不限制学时")
        h_label.setForegroundRole(self.palette().PlaceholderText)
        h_layout.addWidget(self.spin_hours)
        h_layout.addWidget(h_label)
        h_layout.addStretch()
        hours_card.viewLayout.addLayout(h_layout)
        layout.addWidget(hours_card)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_skip = PushButton("  跳过")
        btn_skip.setIcon(FIF.CLOSE)
        btn_skip.setFixedSize(120, 40)
        btn_skip.clicked.connect(lambda: self._on_done(0, "central"))
        btn_layout.addWidget(btn_skip)

        btn_next = PrimaryPushButton("  继续")
        btn_next.setIcon(FIF.RIGHT_ARROW)
        btn_next.setFixedSize(120, 40)
        btn_next.clicked.connect(self._on_next)
        btn_layout.addWidget(btn_next)

        layout.addLayout(btn_layout)

    def _on_next(self):
        goal_type = "central" if self.radio_central.isChecked() else "online"
        hours = self.spin_hours.value()
        self._on_done(hours, goal_type)

    def _on_done(self, hours, goal_type):
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["study_goal"] = hours
            cfg["goal_type"] = goal_type
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        win = self.window()
        win.cfg_goal_type = goal_type
        win.cfg_goal_hours = hours
        win.next_screen()


# ─── Dashboard Screen ──────────────────────────────────────────────


# ─── Mode Selection Screen ─────────────────────────────────────────


class ModeScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(120, 60, 120, 60)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignTop)

        title = TitleLabel("选择模式")
        layout.addWidget(title)

        subtitle = BodyLabel("选择学习方式")
        subtitle.setForegroundRole(self.palette().PlaceholderText)
        layout.addWidget(subtitle)

        layout.addSpacing(20)

        # Auto mode card
        auto_card = HeaderCardWidget(self)
        auto_card.setTitle("🔄 自动模式")
        auto_card.setBorderRadius(8)
        a_layout = QVBoxLayout()
        a_layout.setSpacing(8)
        a_layout.setContentsMargins(0, 8, 0, 8)
        a_layout.addWidget(BodyLabel("自动寻找专题班，按学时目标学习"))
        a_layout.addWidget(CaptionLabel("适合：需要完成学时目标的日常挂机学习"))
        btn_auto = PrimaryPushButton("  选择自动模式")
        btn_auto.setIcon(FIF.PLAY)
        btn_auto.setFixedWidth(200)
        btn_auto.clicked.connect(lambda: self._select_mode("auto"))
        a_layout.addWidget(btn_auto)
        a_layout.addStretch()
        auto_card.viewLayout.addLayout(a_layout)
        layout.addWidget(auto_card)

        # Manual mode card
        manual_card = HeaderCardWidget(self)
        manual_card.setTitle("🎯 手动模式")
        manual_card.setBorderRadius(8)
        m_layout = QVBoxLayout()
        m_layout.setSpacing(8)
        m_layout.setContentsMargins(0, 8, 0, 8)
        m_layout.addWidget(BodyLabel("指定专题班或课程URL，精确学习"))
        m_layout.addWidget(CaptionLabel("适合：学习特定课程、补学指定内容"))
        btn_manual = PrimaryPushButton("  选择手动模式")
        btn_manual.setIcon(FIF.LINK)
        btn_manual.setFixedWidth(200)
        btn_manual.clicked.connect(lambda: self._select_mode("manual"))
        m_layout.addWidget(btn_manual)
        m_layout.addStretch()
        manual_card.viewLayout.addLayout(m_layout)
        layout.addWidget(manual_card)

        layout.addStretch()

    def _select_mode(self, mode):
        win = self.window()
        win.cfg_mode = mode
        if mode == "auto":
            win.next_screen()  # → goal → tags → dashboard
        else:
            win.go_to_manual()  # → manual URL input → dashboard


# ─── Manual URL Input Screen ───────────────────────────────────────


class ManualScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignTop)

        title = TitleLabel("手动指定课程")
        layout.addWidget(title)

        subtitle = BodyLabel("输入专题班或课程的URL，每行一个")
        subtitle.setForegroundRole(self.palette().PlaceholderText)
        layout.addWidget(subtitle)

        # URL input card
        input_card = HeaderCardWidget(self)
        input_card.setTitle("🔗 课程链接")
        input_card.setBorderRadius(8)
        i_layout = QVBoxLayout()
        i_layout.setSpacing(8)
        i_layout.setContentsMargins(0, 8, 0, 8)

        self.text_urls = PlainTextEdit()
        self.text_urls.setPlaceholderText(
            "粘贴URL，每行一个，例如：\n"
            "https://u.ccb.com/workshop/#/myworkshop/detail?id=xxx\n"
            "https://u.ccb.com/workshop/#/detail?id=xxx"
        )
        self.text_urls.setMinimumHeight(200)
        i_layout.addWidget(self.text_urls)

        hint = CaptionLabel("支持专题班详情页URL，会自动提取其中的课程")
        hint.setForegroundRole(self.palette().PlaceholderText)
        i_layout.addWidget(hint)

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
        btn_layout.addWidget(btn_start)

        layout.addLayout(btn_layout)

    def _on_start(self):
        text = self.text_urls.toPlainText().strip()
        if not text:
            InfoBar.warning("提示", "请输入至少一个URL", parent=self, position=InfoBarPosition.TOP)
            return

        urls = [line.strip() for line in text.split("\n") if line.strip() and "ccb.com" in line]
        if not urls:
            InfoBar.warning("提示", "未识别到有效的CCB URL", parent=self, position=InfoBarPosition.TOP)
            return

        win = self.window()
        win.cfg_manual_urls = urls
        win.next_screen()  # → dashboard


# ─── Dashboard Screen ──────────────────────────────────────────────


class DashboardScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tag_event = threading.Event()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        title = StrongBodyLabel("学习仪表盘")
        title.setFont(QFont("", 16, QFont.Bold))
        header.addWidget(title)
        header.addStretch()
        btn_settings = ToolButton(FIF.SETTING)
        btn_settings.setToolTip("设置")
        btn_settings.clicked.connect(lambda: self.window().show_settings())
        header.addWidget(btn_settings)
        layout.addLayout(header)

        # Main area: left (info+table) | right (log)
        main_area = QHBoxLayout()
        main_area.setSpacing(10)

        # ── Left panel ──
        left = QVBoxLayout()
        left.setSpacing(10)

        # Row 1: hours + goal side by side
        info_row = QHBoxLayout()
        info_row.setSpacing(10)

        hours_card = SimpleCardWidget(self)
        hours_card.setBorderRadius(8)
        hl = QVBoxLayout(hours_card)
        hl.setContentsMargins(14, 10, 14, 10)
        hl.setSpacing(6)
        hl.addWidget(SubtitleLabel("📊 培训学时"))
        self.lbl_central = BodyLabel("🏢 集中: --")
        self.lbl_online = BodyLabel("🌐 网络: --")
        self.lbl_updated = CaptionLabel("🕐 更新: --")
        hl.addWidget(self.lbl_central)
        hl.addWidget(self.lbl_online)
        hl.addWidget(self.lbl_updated)
        info_row.addWidget(hours_card, 1)

        goal_card = SimpleCardWidget(self)
        goal_card.setBorderRadius(8)
        gl = QHBoxLayout(goal_card)
        gl.setContentsMargins(14, 10, 14, 10)
        gl.setSpacing(12)
        gl_left = QVBoxLayout()
        gl_left.setSpacing(4)
        gl_left.addWidget(SubtitleLabel("🎯 学习目标"))
        self.lbl_goal_info = BodyLabel("--")
        gl_left.addWidget(self.lbl_goal_info)
        gl_left.addStretch()
        gl.addLayout(gl_left)
        self.progress_ring = ProgressRing()
        self.progress_ring.setFixedSize(72, 72)
        self.progress_ring.setValue(0)
        self.progress_ring.setTextVisible(True)
        gl.addWidget(self.progress_ring)
        info_row.addWidget(goal_card, 1)

        left.addLayout(info_row)

        # Row 2: worker table
        table_card = SimpleCardWidget(self)
        table_card.setBorderRadius(8)
        tl = QVBoxLayout(table_card)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)

        table_header = QHBoxLayout()
        table_header.setContentsMargins(14, 10, 14, 6)
        table_header.addWidget(SubtitleLabel("📈 学习进度"))
        table_header.addStretch()
        self.lbl_progress_summary = CaptionLabel("")
        table_header.addWidget(self.lbl_progress_summary)
        tl.addLayout(table_header)

        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["课程", "进度", "预计", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 100)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setSelectionMode(TableWidget.NoSelection)
        self.table.setBorderRadius(8)
        tl.addWidget(self.table)

        left.addWidget(table_card, 1)
        main_area.addLayout(left, 1)

        # ── Right panel: log ──
        log_card = SimpleCardWidget(self)
        log_card.setBorderRadius(8)
        log_card.setFixedWidth(320)
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        log_header = QHBoxLayout()
        log_header.setContentsMargins(14, 10, 14, 6)
        log_header.addWidget(SubtitleLabel("📋 日志"))
        log_header.addStretch()
        ll.addLayout(log_header)

        self.log_view = PlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        ll.addWidget(self.log_view)

        main_area.addWidget(log_card)

        layout.addLayout(main_area, 1)

    def start_learning(self):
        win = self.window()
        self._init_table(win.cfg_workers)
        self._set_goal_info(win)

        self._worker = AsyncThread(self._run_learning, self)
        self._worker.log_signal.connect(self._on_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.hours_signal.connect(self._on_hours)
        self._worker.done_signal.connect(self._on_done)
        self._worker.tag_request_signal.connect(self._on_tag_request)
        self._worker.start()

    def _init_table(self, workers):
        self.table.setRowCount(workers)
        for i in range(workers):
            self.table.setItem(i, 0, QTableWidgetItem("-"))
            self.table.setItem(i, 1, QTableWidgetItem("-"))
            self.table.setItem(i, 2, QTableWidgetItem("-"))
            self.table.setItem(i, 3, QTableWidgetItem("等待中"))

    def _set_goal_info(self, win):
        goal_type = getattr(win, "cfg_goal_type", "central")
        goal_hours = getattr(win, "cfg_goal_hours", 0)
        type_name = "集中培训" if goal_type == "central" else "网络自学"
        if goal_hours > 0:
            self.lbl_goal_info.setText(f"{type_name} {goal_hours:.0f} 学时")
        else:
            self.lbl_goal_info.setText("不限制")

    async def _run_learning(self, thread: AsyncThread):
        win = self.window()
        cfg_workers = getattr(win, "cfg_workers", 1)
        cfg_headless = getattr(win, "cfg_headless", False)
        cfg_username = getattr(win, "cfg_username", "")
        cfg_password = getattr(win, "cfg_password", "")
        cfg_auto_login = getattr(win, "cfg_auto_login", True)
        cfg_goal_type = getattr(win, "cfg_goal_type", "central")
        cfg_goal_hours = getattr(win, "cfg_goal_hours", 0)
        cfg_tags = getattr(win, "cfg_tags", [])
        cfg_mode = getattr(win, "cfg_mode", "auto")
        cfg_manual_urls = getattr(win, "cfg_manual_urls", [])

        log = lambda msg, style="": thread.log_signal.emit(msg, style)
        progress_cb = lambda data: thread.progress_signal.emit(data)
        hours_cb = lambda data: thread.hours_signal.emit(data)

        try:
            log("正在初始化浏览器...")
            learner = CCBULearner(headless=cfg_headless, workers=cfg_workers)
            await learner.init()
            log("浏览器初始化完成", "green")

            log("正在登录...")
            await learner.login(
                page=learner.pages[0],
                username=cfg_username,
                password=cfg_password,
                auto_login=cfg_auto_login,
                log_callback=log,
            )
            log("登录成功", "green")

            # 手动模式：直接从指定URL学习
            if cfg_mode == "manual":
                log(f"手动模式：{len(cfg_manual_urls)} 个URL", "blue")
                await learner.learn_from_urls(
                    cfg_manual_urls, cfg_workers,
                    progress_cb, hours_cb, log
                )
                thread.done_signal.emit(0, 0)
                return

            # 自动模式
            learner.study_goal = cfg_goal_hours
            learner.goal_type = cfg_goal_type
            if cfg_tags:
                learner.tags_to_learn = cfg_tags

            page = learner.pages[0]
            await page.goto(
                "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                wait_until="networkidle", timeout=30000,
            )
            await page.wait_for_timeout(5000)

            if not cfg_tags:
                try:
                    tags_by_category = await learner.get_available_tags(page)
                    if tags_by_category:
                        tag_count = sum(len(v) for v in tags_by_category.values())
                        log(f"发现 {tag_count} 个标签，等待选择...", "blue")
                        self._tag_event.clear()
                        thread.tag_request_signal.emit(tags_by_category)
                        await asyncio.get_event_loop().run_in_executor(None, self._tag_event.wait)
                        # 重新读取用户选择的标签
                        cfg_tags = list(getattr(win, "cfg_tags", []))
                        log(f"已选择标签: {cfg_tags}" if cfg_tags else "未选择标签", "green" if cfg_tags else "yellow")
                except Exception as e:
                    log(f"标签加载失败: {e}", "yellow")

            if cfg_tags:
                learner.tags_to_learn = cfg_tags
                log(f"应用标签筛选: {', '.join(cfg_tags)}", "blue")
                await learner.filter_by_tags(page)

            progress = learner.load_progress()
            completed_ids = set(progress.get("completed_ws_ids", []))

            page_num = 1
            no_more_pages = False
            tasks = []
            ws_locks = {}

            while len(tasks) < cfg_workers and not no_more_pages:
                workshops = await learner.get_workshops(page)
                if not workshops:
                    no_more_pages = True
                    break
                log(f"第 {page_num} 页: {len(workshops)} 个专题班", "blue")
                new_tasks, new_locks = await learner._collect_workshops_courses(
                    page, workshops, completed_ids
                )
                tasks.extend(new_tasks)
                ws_locks.update(new_locks)
                if len(tasks) >= cfg_workers:
                    break
                moved = await learner.go_to_next_page(page)
                if not moved:
                    no_more_pages = True
                else:
                    page_num += 1
                    await page.wait_for_timeout(3000)

            if tasks:
                log(f"开始学习（{len(tasks)} 门课程, {cfg_workers} 个线程）", "bold blue")
                _fetch_lock = asyncio.Lock()

                async def fetch_more_courses(queue):
                    if no_more_pages:
                        return 0
                    async with _fetch_lock:
                        if no_more_pages:
                            return 0
                        if cfg_goal_hours > 0:
                            try:
                                _h = await learner._get_study_hours(page)
                                if _h.get(cfg_goal_type, 0) >= cfg_goal_hours:
                                    log("已达到学习目标!", "bold green")
                                    return 0
                            except:
                                pass
                        moved = await learner.go_to_next_page(page)
                        if not moved:
                            return 0
                        nonlocal page_num
                        page_num += 1
                        await page.wait_for_timeout(3000)
                        new_ws = await learner.get_workshops(page)
                        if not new_ws:
                            return 0
                        log(f"自动翻到第 {page_num} 页: {len(new_ws)} 个专题班", "blue")
                        new_t, new_l = await learner._collect_workshops_courses(
                            page, new_ws, completed_ids
                        )
                        ws_locks.update(new_l)
                        for t in new_t:
                            queue.put_nowait((*t, 0))
                        if new_t:
                            log(f"新增 {len(new_t)} 门课程", "green")
                        return len(new_t)

                await learner.parallel_learn_courses(
                    tasks, ws_locks, fetch_more_courses, progress_cb, hours_cb, log
                )
            else:
                log("没有需要学习的课程", "yellow")

            thread.done_signal.emit(0, 0)

        except Exception as e:
            log(f"错误: {e}", "red")
            import traceback
            log(traceback.format_exc(), "red")
            thread.done_signal.emit(0, 0)

    # ── Slots ──

    def _on_log(self, msg, style):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def _on_progress(self, data):
        wid = data.get("wid", 0)
        if wid >= self.table.rowCount():
            return
        self.table.setItem(wid, 0, QTableWidgetItem(str(data.get("course", "-"))[:40]))
        self.table.setItem(wid, 1, QTableWidgetItem(str(data.get("progress", "-"))))
        self.table.setItem(wid, 2, QTableWidgetItem(str(data.get("eta", "-"))))
        self.table.setItem(wid, 3, QTableWidgetItem(str(data.get("status", "-"))))

    def _on_hours(self, data):
        self.lbl_central.setText(f"集中培训: {data.get('central', 0):.1f} 学时")
        self.lbl_online.setText(f"网络自学: {data.get('online', 0):.1f} 学时")
        self.lbl_updated.setText(f"更新时间: {data.get('updated', '--')}")
        win = self.window()
        goal_type = getattr(win, "cfg_goal_type", "central")
        goal_hours = getattr(win, "cfg_goal_hours", 0)
        if goal_hours > 0:
            cur = data.get(goal_type, 0)
            pct = min(100, int(cur / goal_hours * 100))
            self.progress_ring.setValue(pct)
            type_name = "集中培训" if goal_type == "central" else "网络自学"
            self.lbl_goal_info.setText(f"{type_name} {cur:.1f}/{goal_hours:.0f} 学时 ({pct}%)")

    def _on_done(self, success, failed):
        InfoBar.success("完成", f"学习流程结束，成功 {success} 门", parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _on_tag_request(self, tags_by_category):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget

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
        hint.setForegroundRole(self.palette().PlaceholderText)
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


# ─── Main Window ───────────────────────────────────────────────────


class MainWindow(MSFluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CCBU-Auto 自动学习")
        self.resize(1000, 650)
        self.setMinimumSize(800, 500)
        self._drag_pos = None

        # Config state
        self.cfg_workers = 1
        self.cfg_headless = False
        self.cfg_username = ""
        self.cfg_password = ""
        self.cfg_auto_login = True
        self.cfg_goal_type = "central"
        self.cfg_goal_hours = 0
        self.cfg_tags = []
        self.cfg_mode = "auto"
        self.cfg_manual_urls = []

        # Screens
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
        self.screen_dashboard = DashboardScreen(self)
        self.screen_dashboard.setObjectName("dashboard")

        # Add sub interfaces with icons
        self.addSubInterface(self.screen_config, FIF.SETTING, "配置")
        self.addSubInterface(self.screen_login, FIF.PEOPLE, "登录")
        self.addSubInterface(self.screen_mode, FIF.SHOPPING_MODE, "模式")
        self.addSubInterface(self.screen_goal, FIF.FLAG, "目标")
        self.addSubInterface(self.screen_manual, FIF.LINK, "手动")
        self.addSubInterface(self.screen_dashboard, FIF.HOME, "仪表盘")

        self._screen_index = 0
        self.navigationInterface.hide()

        # 检查是否有保存的配置，有则自动开始
        has_config = self._load_saved_config()
        if has_config:
            self._screen_index = 5
            self.switchTo(self.screen_dashboard)
            self.screen_dashboard.start_learning()
        else:
            self.switchTo(self.screen_config)

    def _load_saved_config(self):
        """加载保存的配置，返回是否有完整配置"""
        try:
            if not os.path.exists(CONFIG_PATH):
                return False
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "workers" not in cfg:
                return False
            self.cfg_workers = cfg.get("workers", 1)
            self.cfg_headless = cfg.get("headless", False)
            self.cfg_goal_type = cfg.get("goal_type", "central")
            self.cfg_goal_hours = cfg.get("study_goal", 0)
            self.cfg_tags = cfg.get("selected_tags", [])
            # 加载账号
            creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccbu_credentials.json")
            if os.path.exists(creds_path):
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds = json.load(f)
                self.cfg_username = creds.get("username", "")
                self.cfg_password = creds.get("password", "")
            return bool(self.cfg_username)
        except:
            return False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def next_screen(self):
        """根据当前界面和模式决定下一个界面"""
        self._screen_index += 1

        if self._screen_index == 1:
            # config → login
            self.switchTo(self.screen_login)
        elif self._screen_index == 2:
            # login → mode selection
            self.switchTo(self.screen_mode)
        elif self._screen_index == 3:
            # mode → goal (auto) or manual (manual)
            if self.cfg_mode == "auto":
                self.switchTo(self.screen_goal)
            else:
                self.switchTo(self.screen_manual)
        elif self._screen_index == 4:
            # goal → tags → dashboard (auto), or manual → dashboard
            if self.cfg_mode == "auto":
                # tags handled in goal screen, go to dashboard
                self.switchTo(self.screen_dashboard)
                self.screen_dashboard.start_learning()
            else:
                self.switchTo(self.screen_dashboard)
                self.screen_dashboard.start_learning()
        elif self._screen_index == 5:
            # auto: tags → dashboard
            self.switchTo(self.screen_dashboard)
            self.screen_dashboard.start_learning()

    def go_to_manual(self):
        """从模式选择跳到手动URL输入"""
        self._screen_index = 3
        self.switchTo(self.screen_manual)

    def show_mode_screen(self):
        """从手动URL输入返回模式选择"""
        self._screen_index = 2
        self.switchTo(self.screen_mode)

    def show_settings(self):
        """从仪表盘返回设置界面"""
        self._screen_index = 0
        self.switchTo(self.screen_config)


# ─── Entry ─────────────────────────────────────────────────────────


def main():
    app = QApplication(sys.argv)
    app.setStyle("Windows")
    setTheme(Theme.AUTO)
    # Fix font for macOS
    from PyQt5.QtGui import QFont
    app.setFont(QFont("PingFang SC", 13))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
