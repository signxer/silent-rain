#!/usr/bin/env python3
"""CCBU-Auto Fluent Design GUI (QFluentWidgets)"""
import asyncio
import json
import os
import platform
import sys
import threading
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer, QEventLoop
from PySide6.QtGui import QColor, QFont, QIcon
from PySide6.QtWidgets import (
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
    TitleLabel, IconWidget,
    InfoBar, InfoBarPosition,
    MessageBox, Dialog,
    HyperlinkButton,
    isDarkTheme, setTheme, Theme,
)

from main import CCBULearner, CONFIG_PATH, PROGRESS_PATH, STORAGE_STATE_PATH, USER_CREDENTIALS_PATH


# ─── Async Thread ──────────────────────────────────────────────────


class AsyncThread(QThread):
    log_signal = Signal(str, str)
    progress_signal = Signal(dict)
    hours_signal = Signal(dict)
    done_signal = Signal(int, int)
    tag_request_signal = Signal(dict)
    tag_confirm_signal = Signal(list, dict)  # (saved_tags, tags_by_category)
    page_confirm_signal = Signal(int)  # last_page

    def __init__(self, coro_func, parent=None):
        super().__init__(parent)
        self._coro_func = coro_func

    def run(self):
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
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
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        # Workers card
        workers_card = HeaderCardWidget(self)
        workers_card.setTitle("工作线程数")
        workers_card.setBorderRadius(8)
        w_layout = QHBoxLayout()
        w_layout.setSpacing(12)
        w_layout.setContentsMargins(0, 8, 0, 8)

        self.spin_workers = SpinBox()
        self.spin_workers.setRange(1, 20)
        self.spin_workers.setValue(self._saved.get("workers", 1))
        self.spin_workers.setFixedWidth(120)
        w_layout.addWidget(self.spin_workers)
        w_layout.addWidget(BodyLabel("个线程"))
        w_hint = CaptionLabel("建议 3-10")
        w_hint.setStyleSheet("color: #888;")
        w_layout.addWidget(w_hint)
        w_layout.addStretch()
        workers_card.viewLayout.addLayout(w_layout)
        layout.addWidget(workers_card)

        # Browser settings card (headless + engine merged)
        browser_card = HeaderCardWidget(self)
        browser_card.setTitle("浏览器设置")
        browser_card.setBorderRadius(8)
        card_layout = QVBoxLayout()
        card_layout.setSpacing(10)
        card_layout.setContentsMargins(0, 8, 0, 8)

        # Row 1: Headless
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        self.switch_headless = SwitchButton()
        self.switch_headless.setChecked(self._saved.get("headless", True))
        self.switch_headless.setOnText("后台运行")
        self.switch_headless.setOffText("显示浏览器")
        row1.addWidget(BodyLabel("无头模式:"))
        row1.addWidget(self.switch_headless)
        row1.addStretch()
        card_layout.addLayout(row1)

        # Row 2: Browser engine
        default_browser = "chrome" if platform.system() == "Windows" else "chromium"
        saved_browser = self._saved.get("browser", default_browser)

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        self.switch_browser = SwitchButton()
        self.switch_browser.setChecked(saved_browser == "chrome")
        self.switch_browser.setOnText("本地 Chrome")
        self.switch_browser.setOffText("内置 Chromium")
        row2.addWidget(BodyLabel("浏览器引擎:"))
        row2.addWidget(self.switch_browser)
        row2.addStretch()
        card_layout.addLayout(row2)

        # Row 3: Chrome path (only when using local Chrome)
        self.chrome_path_widget = QWidget()
        path_row = QHBoxLayout(self.chrome_path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(8)
        path_row.addWidget(BodyLabel("Chrome路径:"))
        self.input_chrome_path = LineEdit()
        self.input_chrome_path.setPlaceholderText("留空自动检测")
        self.input_chrome_path.setText(self._saved.get("chrome_path", ""))
        self.input_chrome_path.setFixedWidth(250)
        path_row.addWidget(self.input_chrome_path)

        btn_browse = PushButton("浏览")
        btn_browse.setFixedWidth(60)
        btn_browse.clicked.connect(self._browse_chrome)
        path_row.addWidget(btn_browse)
        path_row.addStretch()
        card_layout.addWidget(self.chrome_path_widget)
        self.chrome_path_widget.setVisible(saved_browser == "chrome")

        self.switch_browser.checkedChanged.connect(lambda checked: self.chrome_path_widget.setVisible(checked))

        browser_card.viewLayout.addLayout(card_layout)
        layout.addWidget(browser_card)

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

    def _on_start(self):
        workers = self.spin_workers.value()
        headless = self.switch_headless.isChecked()
        browser = "chrome" if self.switch_browser.isChecked() else "chromium"
        chrome_path = self.input_chrome_path.text().strip() if self.switch_browser.isChecked() else ""
        try:
            cfg = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            cfg["workers"] = workers
            cfg["headless"] = headless
            cfg["browser"] = browser
            cfg["chrome_path"] = chrome_path
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except:
            pass
        win = self.window()
        win.cfg_workers = workers
        win.cfg_headless = headless
        win.cfg_browser = browser
        win.cfg_chrome_path = chrome_path
        win.next_screen()


# ─── Login Screen ──────────────────────────────────────────────────


class LoginScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_creds()
        self._build_ui()

    def _load_creds(self):
        path = USER_CREDENTIALS_PATH
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
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)

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

        path = USER_CREDENTIALS_PATH
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

        title = TitleLabel("学习目标")
        layout.addWidget(title)

        subtitle = BodyLabel("分别设置集中培训和网络自学的学习目标")
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        # 集中培训卡片
        central_card = HeaderCardWidget(self)
        central_card.setTitle("集中培训")
        central_card.setBorderRadius(8)
        c_layout = QVBoxLayout()
        c_layout.setSpacing(10)
        c_layout.setContentsMargins(0, 8, 0, 8)

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
        o_layout = QVBoxLayout()
        o_layout.setSpacing(10)
        o_layout.setContentsMargins(0, 8, 0, 8)

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

        layout.addStretch()

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_skip = PushButton("  跳过")
        btn_skip.setIcon(FIF.CLOSE)
        btn_skip.setFixedSize(120, 40)
        btn_skip.clicked.connect(lambda: self._on_done(False, 0, False, 0))
        btn_layout.addWidget(btn_skip)

        btn_next = PrimaryPushButton("  继续")
        btn_next.setIcon(FIF.RIGHT_ARROW)
        btn_next.setFixedSize(120, 40)
        btn_next.clicked.connect(self._on_next)
        btn_layout.addWidget(btn_next)

        layout.addLayout(btn_layout)

    def _on_next(self):
        central_on = self.switch_central.isChecked()
        online_on = self.switch_online.isChecked()
        central = self.spin_central.value() if central_on else 0
        online = self.spin_online.value() if online_on else 0
        central_mode = "remain" if self.radio_central_remain.isChecked() else "target"
        online_mode = "remain" if self.radio_online_remain.isChecked() else "target"
        self._on_done(central_on, central, central_mode, online_on, online, online_mode)

    def _on_done(self, central_on, central_goal, central_mode, online_on, online_goal, online_mode):
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
        win.next_screen()


# ─── Dashboard Screen ──────────────────────────────────────────────


# ─── Mode Selection Screen ─────────────────────────────────────────


class ModeScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(160, 50, 160, 50)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignTop)

        title = TitleLabel("选择模式")
        layout.addWidget(title)

        subtitle = BodyLabel("选择学习方式，后续可在设置中切换")
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)

        layout.addSpacing(24)

        # Auto mode card
        auto_card = HeaderCardWidget(self)
        auto_card.setTitle("自动模式")
        auto_card.setBorderRadius(10)
        a_layout = QVBoxLayout()
        a_layout.setSpacing(10)
        a_layout.setContentsMargins(0, 10, 0, 10)
        a_desc = BodyLabel("自动寻找专题班，按学时目标学习")
        a_desc.setStyleSheet("line-height: 1.4;")
        a_layout.addWidget(a_desc)
        a_hint = CaptionLabel("适合：需要完成学时目标的日常挂机学习")
        a_hint.setStyleSheet("color: #888;")
        a_layout.addWidget(a_hint)
        a_layout.addSpacing(8)
        btn_auto = PrimaryPushButton("  选择自动模式")
        btn_auto.setIcon(FIF.PLAY)
        btn_auto.setFixedWidth(200)
        btn_auto.setFixedHeight(36)
        btn_auto.clicked.connect(lambda: self._select_mode("auto"))
        a_layout.addWidget(btn_auto)
        a_layout.addStretch()
        auto_card.viewLayout.addLayout(a_layout)
        layout.addWidget(auto_card)

        # Manual mode card
        manual_card = HeaderCardWidget(self)
        manual_card.setTitle("手动模式")
        manual_card.setBorderRadius(10)
        m_layout = QVBoxLayout()
        m_layout.setSpacing(10)
        m_layout.setContentsMargins(0, 10, 0, 10)
        m_desc = BodyLabel("指定专题班或课程 URL，精确学习")
        m_desc.setStyleSheet("line-height: 1.4;")
        m_layout.addWidget(m_desc)
        m_hint = CaptionLabel("适合：学习特定课程、补学指定内容")
        m_hint.setStyleSheet("color: #888;")
        m_layout.addWidget(m_hint)
        m_layout.addSpacing(8)
        btn_manual = PrimaryPushButton("  选择手动模式")
        btn_manual.setIcon(FIF.LINK)
        btn_manual.setFixedWidth(200)
        btn_manual.setFixedHeight(36)
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
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)

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
            "https://u.ccb.com/workshop/#/detail?id=xxx"
        )
        self.text_urls.setMinimumHeight(200)
        i_layout.addWidget(self.text_urls)

        hint = CaptionLabel("支持专题班详情页URL，会自动提取其中的课程")
        hint.setStyleSheet("color: #888;")
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
        self._learn_start_time = None      # 学习开始时间
        self._progress_history = []         # [(timestamp, pct), ...]
        self._eta_seconds = None            # 最新预估剩余秒数
        self._eta_calc_time = None          # 预估计算时的时间戳
        # 实时倒计时定时器
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._tick_eta)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        header.setSpacing(8)
        title = StrongBodyLabel("润物细无声 CCBU-Auto")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()

        # Mode indicator
        self.lbl_mode = CaptionLabel("")
        self.lbl_mode.setStyleSheet("padding: 2px 8px; border-radius: 4px; background: rgba(128,128,128,0.1);")
        header.addWidget(self.lbl_mode)

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
        hours_card.setBorderRadius(10)
        hl = QVBoxLayout(hours_card)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(8)
        hl_title = QHBoxLayout()
        hl_title.setSpacing(6)
        hl_icon = IconWidget(FIF.PEOPLE, self)
        hl_icon.setFixedSize(16, 16)
        hl_title.addWidget(hl_icon)
        hl_title.addWidget(SubtitleLabel("培训学时"))
        hl_title.addStretch()
        hl.addLayout(hl_title)
        self.lbl_central = BodyLabel("集中培训: -- 学时")
        self.lbl_online = BodyLabel("网络自学: -- 学时")
        self.lbl_updated = CaptionLabel("更新时间: --")
        hl.addWidget(self.lbl_central)
        hl.addWidget(self.lbl_online)
        hl.addWidget(self.lbl_updated)
        info_row.addWidget(hours_card, 1)

        goal_card = SimpleCardWidget(self)
        goal_card.setBorderRadius(10)
        gl = QHBoxLayout(goal_card)
        gl.setContentsMargins(16, 14, 16, 14)
        gl.setSpacing(16)
        gl_left = QVBoxLayout()
        gl_left.setSpacing(6)
        gl_title = QHBoxLayout()
        gl_title.setSpacing(6)
        gl_icon = IconWidget(FIF.FLAG, self)
        gl_icon.setFixedSize(16, 16)
        gl_title.addWidget(gl_icon)
        gl_title.addWidget(SubtitleLabel("学习目标"))
        gl_title.addStretch()
        gl_left.addLayout(gl_title)
        self.lbl_goal_info = BodyLabel("--")
        gl_left.addWidget(self.lbl_goal_info)
        self.lbl_eta = CaptionLabel("")
        self.lbl_eta.setStyleSheet("color: #888;")
        gl_left.addWidget(self.lbl_eta)
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
        table_card.setBorderRadius(10)
        tl = QVBoxLayout(table_card)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)

        table_header = QHBoxLayout()
        table_header.setContentsMargins(16, 12, 16, 8)
        table_header.setSpacing(6)
        tbl_icon = IconWidget(FIF.GAME, self)
        tbl_icon.setFixedSize(16, 16)
        table_header.addWidget(tbl_icon)
        table_header.addWidget(SubtitleLabel("学习进度"))
        table_header.addStretch()
        self.lbl_progress_summary = CaptionLabel("")
        table_header.addWidget(self.lbl_progress_summary)
        tl.addLayout(table_header)

        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["课程", "进度", "预计", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 100)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setSelectionMode(TableWidget.NoSelection)
        self.table.setBorderRadius(8)
        tl.addWidget(self.table)

        left.addWidget(table_card, 1)
        main_area.addLayout(left, 1)

        # ── Right panel: log ──
        log_card = SimpleCardWidget(self)
        log_card.setBorderRadius(10)
        log_card.setFixedWidth(320)
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)

        log_header = QHBoxLayout()
        log_header.setContentsMargins(16, 12, 16, 8)
        log_header.setSpacing(6)
        log_icon = IconWidget(FIF.CHECKBOX, self)
        log_icon.setFixedSize(16, 16)
        log_header.addWidget(log_icon)
        log_header.addWidget(SubtitleLabel("日志"))
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

        # Mode indicator
        mode = getattr(win, "cfg_mode", "auto")
        if mode == "manual":
            self.lbl_mode.setText("手动模式")
            self.lbl_mode.setStyleSheet("padding: 2px 8px; border-radius: 4px; background: rgba(0,120,215,0.15); color: #0078d7;")
        else:
            self.lbl_mode.setText("自动模式")
            self.lbl_mode.setStyleSheet("padding: 2px 8px; border-radius: 4px; background: rgba(16,124,16,0.15); color: #107c10;")

        self._worker = AsyncThread(self._run_learning, self)
        self._worker.log_signal.connect(self._on_log)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.hours_signal.connect(self._on_hours)
        self._worker.done_signal.connect(self._on_done)
        self._worker.tag_request_signal.connect(self._on_tag_request)
        self._worker.tag_confirm_signal.connect(self._on_tag_confirm)
        self._worker.page_confirm_signal.connect(self._on_page_confirm)
        self._worker.start()

    def _init_table(self, workers):
        self.table.setRowCount(workers)
        for i in range(workers):
            self.table.setItem(i, 0, QTableWidgetItem("-"))
            self.table.setItem(i, 1, QTableWidgetItem("-"))
            self.table.setItem(i, 2, QTableWidgetItem("-"))
            self.table.setItem(i, 3, QTableWidgetItem("等待中"))

    def _set_goal_info(self, win):
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
        # 重置 ETA 追踪
        self._learn_start_time = None
        self._progress_history = []
        self._eta_seconds = None
        self._eta_calc_time = None
        self._eta_timer.stop()
        self.lbl_eta.setText("")

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
            cfg_browser = getattr(win, "cfg_browser", "chromium")
            cfg_chrome_path = getattr(win, "cfg_chrome_path", "")
            log("正在初始化浏览器...")
            learner = CCBULearner(headless=cfg_headless, workers=cfg_workers, browser=cfg_browser)
            await learner.init(log_callback=log, chrome_path=cfg_chrome_path)
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

            # 获取配置（新格式：central_goal/online_goal，0表示不学习）
            cfg_central_goal = getattr(win, "cfg_central_goal", 0)
            cfg_online_goal = getattr(win, "cfg_online_goal", 0)
            cfg_central_mode = getattr(win, "cfg_central_mode", "target")
            cfg_online_mode = getattr(win, "cfg_online_mode", "target")

            if cfg_central_goal <= 0 and cfg_online_goal <= 0:
                log("未设定学习目标，退出", "yellow")
                thread.done_signal.emit(0, 0)
                return

            # 创建独立页面用于学时查询（不能用主页面，否则会污染导航状态）
            try:
                hours_page = await learner.context.new_page()
            except:
                hours_page = None

            # 登录后立即检查学时
            cur_hours = {"central": 0, "online": 0}
            if (cfg_central_goal > 0 or cfg_online_goal > 0) and hours_page:
                log("正在检查当前学时...", "blue")
                try:
                    _h = await learner._get_study_hours(hours_page)
                    cur_hours = {"central": _h.get("central", 0), "online": _h.get("online", 0)}
                    log(f"当前: 集中{cur_hours['central']:.1f} 网络{cur_hours['online']:.1f} 学时", "blue")
                    hours_cb(_h)

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

            # 手动模式：直接从指定URL学习
            if cfg_mode == "manual":
                log(f"手动模式：{len(cfg_manual_urls)} 个URL", "blue")
                await learner.learn_from_urls(
                    cfg_manual_urls, cfg_workers,
                    progress_cb, hours_cb, log
                )
                thread.done_signal.emit(0, 0)
                return

            # ── 自动模式：按阶段顺序学习 ──
            page = learner.pages[0]
            list_url = "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise"

            # 加载专题班列表页（刷新直到标签树出来）
            tags_by_category = {}
            load_attempt = 0
            while not tags_by_category:
                load_attempt += 1
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
                    # 等待用户手动登录
                    for _ in range(120):
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

            if cfg_tags:
                # 有已保存标签，询问用户
                self._tag_event.clear()
                thread.tag_confirm_signal.emit(cfg_tags, tags_by_category)
                await asyncio.get_event_loop().run_in_executor(None, self._tag_event.wait)
                cfg_tags = list(getattr(win, "cfg_tags", []))
            elif tags_by_category:
                # 无已保存标签，直接弹选择框
                self._tag_event.clear()
                thread.tag_request_signal.emit(tags_by_category)
                await asyncio.get_event_loop().run_in_executor(None, self._tag_event.wait)
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
                await asyncio.get_event_loop().run_in_executor(None, self._page_event.wait)

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
                type_name = "集中培训" if phase_goal_type == "central" else "网络自学"
                if phase_goal_hours > 0:
                    log(f"━━ 阶段: {type_name} 目标{phase_goal_hours:.0f}学时 ━━", "bold blue")
                else:
                    log(f"━━ 阶段: {type_name} 无限制 ━━", "bold blue")

                # 重置ETA追踪
                self._learn_start_time = None
                self._progress_history = []
                self._eta_seconds = None
                self._eta_calc_time = None

                # study_goal 是绝对目标值（当前学时 + 还需学时）
                # phase_goal_hours 是"还需"的值，需要加上当前学时
                try:
                    _cur = (await learner._get_study_hours(hours_page)).get(phase_goal_type, 0) if hours_page else 0
                    learner.study_goal = _cur + phase_goal_hours
                except:
                    learner.study_goal = phase_goal_hours
                learner.goal_type = phase_goal_type

                no_more_pages = False
                tasks = []
                ws_locks = {}

                # 采集课程，至少凑够 worker 数量再开始学（除非已无更多页）
                while len(tasks) < cfg_workers and not no_more_pages:
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
                        await page.wait_for_timeout(3000)

                if tasks:
                    log(f"开始学习（{len(tasks)} 门课程, {cfg_workers} 个线程）", "bold blue")
                    _fetch_lock = asyncio.Lock()
                    _collect_page = await learner.context.new_page()

                    _fetched_page = 0

                    async def fetch_more_courses(queue):
                        nonlocal _fetched_page, no_more_pages
                        if no_more_pages:
                            log("无更多页，停止采集", "yellow")
                            return 0
                        async with _fetch_lock:
                            if no_more_pages:
                                return 0
                            if queue.qsize() > 0:
                                return 0
                            log("课程池空了，自动翻页采集...", "blue")
                            # 检查当前阶段目标是否已达成
                            if phase_goal_hours > 0 and hours_page:
                                try:
                                    _h = await learner._get_study_hours(hours_page)
                                    if _h.get(phase_goal_type, 0) >= phase_goal_hours:
                                        log(f"✓ {type_name}目标已达成!", "bold green")
                                        return 0
                                except:
                                    pass
                            try:
                                await _collect_page.goto(
                                    "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                                    wait_until="domcontentloaded", timeout=20000)
                                await _collect_page.wait_for_timeout(8000)
                            except:
                                pass
                            moved = await learner.go_to_next_page(_collect_page)
                            if not moved:
                                log("已到最后一页", "yellow")
                                no_more_pages = True
                                return 0
                            nonlocal page_num
                            page_num += 1
                            _fetched_page = page_num
                            await _collect_page.wait_for_timeout(5000)
                            new_ws = await learner.get_workshops(_collect_page)
                            if not new_ws:
                                no_more_pages = True
                                return 0
                            log(f"自动翻到第 {page_num} 页: {len(new_ws)} 个专题班", "blue")
                            learner.save_progress(completed_ids, page_num, 0)
                            new_t, new_l = await learner._collect_workshops_courses(
                                _collect_page, new_ws, completed_ids, log_callback=log
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
                    log(f"✓ {type_name}阶段完成", "bold green")
                else:
                    log(f"{type_name}: 没有需要学习的课程", "yellow")

            log("全部学习目标完成!", "bold green")
            thread.done_signal.emit(0, 0)

        except Exception as e:
            # 页面/浏览器被关闭时静默处理（用户打开设置页面等场景）
            if "Target page, context or browser has been closed" in str(e):
                thread.done_signal.emit(0, 0)
                return
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
        import time as _time
        self.lbl_central.setText(f"集中培训: {data.get('central', 0):.1f} 学时")
        self.lbl_online.setText(f"网络自学: {data.get('online', 0):.1f} 学时")
        self.lbl_updated.setText(f"更新时间: {data.get('updated', '--')}")
        win = self.window()
        c_goal = getattr(win, "cfg_central_goal", 0)
        o_goal = getattr(win, "cfg_online_goal", 0)
        c_mode = getattr(win, "cfg_central_mode", "target")
        o_mode = getattr(win, "cfg_online_mode", "target")

        c_cur = data.get("central", 0)
        o_cur = data.get("online", 0)

        # 差额模式：目标 = 已有 + 差额
        c_target = c_cur + c_goal if c_mode == "remain" else c_goal
        o_target = o_cur + o_goal if o_mode == "remain" else o_goal

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
            self.progress_ring.setValue(100)
            self.lbl_goal_info.setText(f"✓ 全部完成 集中{c_cur:.1f} 网络{o_cur:.1f}")
            self._eta_seconds = None
            self._eta_timer.stop()
            self.lbl_eta.setText("✓ 已完成")
            return

        # 计算进度
        pct_f = min(100.0, cur / goal * 100) if goal > 0 else 0
        pct = int(pct_f)
        self.progress_ring.setValue(pct)
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

    def _on_done(self, success, failed):
        self._eta_timer.stop()
        InfoBar.success("完成", f"学习流程结束，成功 {success} 门", parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _on_tag_request(self, tags_by_category):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget

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
        hint.setStyleSheet("color: #888;")
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

    def _on_tag_confirm(self, saved_tags, tags_by_category):
        """有已保存标签时，询问用户：使用已保存 / 重新选择 / 跳过"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout
        from PySide6.QtCore import QTimer

        TIMEOUT = 10  # 秒

        dlg = QDialog(self)
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
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout
        from PySide6.QtCore import QTimer

        TIMEOUT = 10

        dlg = QDialog(self)
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

if sys.platform == "darwin":
    from PySide6.QtWidgets import QMainWindow, QStackedWidget

    class _BaseWindow(QMainWindow):
        """macOS原生窗口：交通灯在左侧，不使用无边框方案"""
        def __init__(self):
            super().__init__()
            self._stack = QStackedWidget()
            self.setCentralWidget(self._stack)
        def addSubInterface(self, widget, icon, text, **kw):
            self._stack.addWidget(widget)
        def switchTo(self, widget):
            self._stack.setCurrentWidget(widget)
        class _NavStub:
            def hide(self): pass
            def show(self): pass
        @property
        def navigationInterface(self):
            if not hasattr(self, '_nav'):
                self._nav = self._NavStub()
            return self._nav
else:
    _BaseWindow = MSFluentWindow


class MainWindow(_BaseWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("润物细无声 CCBU-Auto")
        self.resize(1000, 650)
        self.setMinimumSize(800, 500)
        self._drag_pos = None

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
        self.cfg_workers = 1
        self.cfg_headless = True
        self.cfg_browser = "chrome" if platform.system() == "Windows" else "chromium"
        self.cfg_chrome_path = ""
        self.cfg_username = ""
        self.cfg_password = ""
        self.cfg_auto_login = True
        self.cfg_goal_mode = "none"      # none/unlimited/target/remain
        self.cfg_central_goal = 0.0
        self.cfg_online_goal = 0.0
        self.cfg_tags = []
        self.cfg_mode = "auto"
        self.cfg_manual_urls = []

        # 创建子界面
        self._createSubInterfaces()

        # 隐藏启动画面
        self.splashScreen.finish()

        # 检查是否有保存的配置，有则自动开始
        has_config = self._load_saved_config()
        if has_config:
            self._screen_index = 5
            self.switchTo(self.screen_dashboard)
            self.screen_dashboard.start_learning()
        else:
            self.switchTo(self.screen_config)

    def _createSubInterfaces(self):
        """创建所有子界面"""
        # 让启动画面显示一下（用事件循环避免阻塞UI）
        loop = QEventLoop(self)
        QTimer.singleShot(1500, loop.quit)
        loop.exec()
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
        self.addSubInterface(self.screen_mode, FIF.TILES, "模式")
        self.addSubInterface(self.screen_goal, FIF.FLAG, "目标")
        self.addSubInterface(self.screen_manual, FIF.LINK, "手动")
        self.addSubInterface(self.screen_dashboard, FIF.HOME, "仪表盘")

        self._screen_index = 0
        self.navigationInterface.hide()

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
            self.cfg_headless = cfg.get("headless", True)
            default_browser = "chrome" if platform.system() == "Windows" else "chromium"
            self.cfg_browser = cfg.get("browser", default_browser)
            self.cfg_chrome_path = cfg.get("chrome_path", "")
            self.cfg_central_goal = cfg.get("central_goal", 0)
            self.cfg_online_goal = cfg.get("online_goal", 0)
            self.cfg_central_mode = cfg.get("central_mode", "target")
            self.cfg_online_mode = cfg.get("online_mode", "target")
            # 向后兼容旧格式
            if cfg.get("study_goal", 0) > 0:
                if cfg.get("goal_type") == "central":
                    self.cfg_central_goal = cfg["study_goal"]
                else:
                    self.cfg_online_goal = cfg["study_goal"]
            self.cfg_tags = cfg.get("selected_tags", [])
            # 加载账号
            creds_path = USER_CREDENTIALS_PATH
            if os.path.exists(creds_path):
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds = json.load(f)
                self.cfg_username = creds.get("username", "")
                self.cfg_password = creds.get("password", "")
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


def _get_resource_path(filename):
    """获取资源文件路径（兼容 PyInstaller 打包）"""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def main():
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

    # 全局样式覆盖字体族
    app.setStyleSheet(f"* {{ font-family: '{font_family}'; }}")

    app.setStyle("Windows")
    setTheme(Theme.AUTO)

    # 设置应用图标（全局生效）
    icon_path = _get_resource_path("icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
