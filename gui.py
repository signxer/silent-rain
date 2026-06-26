#!/usr/bin/env python3
"""CCBU-Auto Qt GUI Application"""
import asyncio
import json
import os
import threading
import sys
from datetime import datetime

from PySide6.QtCore import (
    Qt, QThread, Signal, Slot, QTimer,
)
from PySide6.QtGui import QFont, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QCheckBox,
    QRadioButton, QButtonGroup, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QPlainTextEdit, QListWidget, QListWidgetItem,
    QProgressBar, QSplitter, QMessageBox, QSizePolicy,
)

from main import CCBULearner, CONFIG_PATH, PROGRESS_PATH


# ─── Async Thread ──────────────────────────────────────────────────


class AsyncThread(QThread):
    """Runs an async function in a dedicated thread with its own event loop."""

    log_signal = Signal(str, str)          # (message, style)
    progress_signal = Signal(dict)         # worker progress
    hours_signal = Signal(dict)            # study hours
    done_signal = Signal(int, int)         # (success, failed)
    login_done_signal = Signal(bool, str)  # (success, username)
    tag_request_signal = Signal(dict)      # 请求选择标签，传入tags_by_category
    tag_result_signal = Signal(list)       # 标签选择结果

    def __init__(self, coro_func, parent=None):
        super().__init__(parent)
        self._coro_func = coro_func
        self._loop = None

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._coro_func(self))
        except Exception as e:
            self.log_signal.emit(f"错误: {e}", "red")
        finally:
            self._loop.close()

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ─── Styles ────────────────────────────────────────────────────────

STYLE = """
QMainWindow {
    background-color: #1e1e2e;
}
QWidget {
    color: #cdd6f4;
    font-size: 14px;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: #89b4fa;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QPushButton {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 8px 20px;
    font-weight: bold;
    color: #cdd6f4;
}
QPushButton:hover {
    background-color: #45475a;
}
QPushButton:pressed {
    background-color: #585b70;
}
QPushButton#primary {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
}
QPushButton#primary:hover {
    background-color: #74c7ec;
}
QLineEdit {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #cdd6f4;
}
QLineEdit:focus {
    border-color: #89b4fa;
}
QSpinBox {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #cdd6f4;
}
QCheckBox {
    spacing: 8px;
    color: #cdd6f4;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #45475a;
    background-color: #313244;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QRadioButton {
    spacing: 8px;
    color: #cdd6f4;
}
QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border-radius: 10px;
    border: 1px solid #45475a;
    background-color: #313244;
}
QRadioButton::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QTableWidget {
    background-color: #181825;
    alternate-background-color: #1e1e2e;
    border: 1px solid #313244;
    border-radius: 6px;
    gridline-color: #313244;
    selection-background-color: #45475a;
}
QTableWidget::item {
    padding: 4px 8px;
}
QHeaderView::section {
    background-color: #313244;
    color: #89b4fa;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #45475a;
    border-bottom: 1px solid #45475a;
    font-weight: bold;
}
QPlainTextEdit {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 6px;
    color: #a6adc8;
    font-family: 'Menlo', 'Consolas', monospace;
    font-size: 12px;
    padding: 6px;
}
QProgressBar {
    border: 1px solid #45475a;
    border-radius: 6px;
    text-align: center;
    color: #1e1e2e;
    font-weight: bold;
    background-color: #313244;
    height: 22px;
}
QProgressBar::chunk {
    background-color: #a6e3a1;
    border-radius: 5px;
}
QLabel#title {
    font-size: 20px;
    font-weight: bold;
    color: #89b4fa;
}
QLabel#subtitle {
    color: #a6adc8;
    font-size: 12px;
}
QListWidget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 6px;
    padding: 4px;
}
QListWidget::item {
    padding: 6px 8px;
    border-radius: 4px;
}
QListWidget::item:hover {
    background-color: #313244;
}
QListWidget::item:selected {
    background-color: #45475a;
}
"""


# ─── Screens ───────────────────────────────────────────────────────


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
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Title
        title = QLabel("运行配置")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Form
        group = QGroupBox("参数设置")
        form = QFormLayout()
        form.setSpacing(12)

        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 20)
        self.spin_workers.setValue(self._saved.get("workers", 1))
        form.addRow("工作线程数:", self.spin_workers)

        self.chk_headless = QCheckBox("无头模式（后台运行）")
        self.chk_headless.setChecked(self._saved.get("headless", False))
        form.addRow("", self.chk_headless)

        group.setLayout(form)
        layout.addWidget(group)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_start = QPushButton("开始")
        self.btn_start.setObjectName("primary")
        self.btn_start.setFixedWidth(160)
        self.btn_start.clicked.connect(self._on_start)
        btn_layout.addWidget(self.btn_start)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

    def _on_start(self):
        workers = self.spin_workers.value()
        headless = self.chk_headless.isChecked()

        # Save config
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

        main_win = self.window()
        main_win.cfg_workers = workers
        main_win.cfg_headless = headless
        main_win.next_screen()


class LoginScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_creds()
        self._build_ui()

    def _load_creds(self):
        creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccbu_credentials.json")
        self._creds = {}
        if os.path.exists(creds_path):
            try:
                with open(creds_path, "r", encoding="utf-8") as f:
                    self._creds = json.load(f)
            except:
                pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("用户登录")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        group = QGroupBox("账号信息")
        form = QFormLayout()
        form.setSpacing(12)

        self.input_user = QLineEdit(self._creds.get("username", ""))
        form.addRow("账号:", self.input_user)

        self.input_pass = QLineEdit(self._creds.get("password", ""))
        self.input_pass.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("密码:", self.input_pass)

        group.setLayout(form)
        layout.addWidget(group)

        # Login mode
        mode_group = QGroupBox("登录方式")
        mode_layout = QHBoxLayout()
        self.radio_auto = QRadioButton("自动登录")
        self.radio_manual = QRadioButton("手动登录")
        self.radio_auto.setChecked(True)
        mode_layout.addWidget(self.radio_auto)
        mode_layout.addWidget(self.radio_manual)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        # Status
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("subtitle")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_login = QPushButton("登录")
        self.btn_login.setObjectName("primary")
        self.btn_login.setFixedWidth(160)
        self.btn_login.clicked.connect(self._on_login)
        btn_layout.addWidget(self.btn_login)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

        self.input_pass.returnPressed.connect(self._on_login)

    def _on_login(self):
        username = self.input_user.text().strip()
        password = self.input_pass.text()
        auto = self.radio_auto.isChecked()

        if not username:
            self.lbl_status.setText("[red]请输入账号[/red]")
            return

        # Save creds
        creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccbu_credentials.json")
        try:
            with open(creds_path, "w", encoding="utf-8") as f:
                json.dump({"username": username, "password": password}, f, ensure_ascii=False, indent=2)
        except:
            pass

        main_win = self.window()
        main_win.cfg_username = username
        main_win.cfg_password = password
        main_win.cfg_auto_login = auto
        main_win.next_screen()


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
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("学习目标")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        group = QGroupBox("目标设置")
        form = QFormLayout()
        form.setSpacing(12)

        # Goal type
        type_layout = QHBoxLayout()
        self.radio_central = QRadioButton("集中培训")
        self.radio_online = QRadioButton("网络自学")
        if self._saved_type == "central":
            self.radio_central.setChecked(True)
        else:
            self.radio_online.setChecked(True)
        type_layout.addWidget(self.radio_central)
        type_layout.addWidget(self.radio_online)
        form.addRow("目标类型:", type_layout)

        # Hours
        self.spin_hours = QSpinBox()
        self.spin_hours.setRange(0, 9999)
        self.spin_hours.setValue(int(self._saved_goal))
        self.spin_hours.setSpecialValueText("不限制")
        form.addRow("目标学时:", self.spin_hours)

        group.setLayout(form)
        layout.addWidget(group)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_skip = QPushButton("跳过")
        self.btn_skip.setFixedWidth(120)
        self.btn_skip.clicked.connect(lambda: self._on_done(0, "central"))
        btn_layout.addWidget(self.btn_skip)

        self.btn_next = QPushButton("继续")
        self.btn_next.setObjectName("primary")
        self.btn_next.setFixedWidth(120)
        self.btn_next.clicked.connect(self._on_next)
        btn_layout.addWidget(self.btn_next)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

    def _on_next(self):
        goal_type = "central" if self.radio_central.isChecked() else "online"
        hours = self.spin_hours.value()
        self._on_done(hours, goal_type)

    def _on_done(self, hours, goal_type):
        self._save_goal(hours, goal_type)
        main_win = self.window()
        main_win.cfg_goal_type = goal_type
        main_win.cfg_goal_hours = hours
        main_win.next_screen()

    def _save_goal(self, hours, goal_type):
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


class TagScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("选择标签（可多选）")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        layout.addWidget(self.tag_list)

        self.lbl_hint = QLabel("标签将在浏览器加载后自动填充")
        self.lbl_hint.setObjectName("subtitle")
        self.lbl_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_hint)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_skip = QPushButton("跳过")
        btn_skip.setFixedWidth(120)
        btn_skip.clicked.connect(self._on_skip)
        btn_layout.addWidget(btn_skip)

        self.btn_confirm = QPushButton("确认选择")
        self.btn_confirm.setObjectName("primary")
        self.btn_confirm.setFixedWidth(120)
        self.btn_confirm.clicked.connect(self._on_confirm)
        btn_layout.addWidget(self.btn_confirm)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def set_tags(self, tags_by_category: dict):
        """Populate tag list from browser data."""
        self.tag_list.clear()
        self._all_tags = []
        for category, tags in tags_by_category.items():
            for tag in tags:
                self._all_tags.append(tag)
                item = QListWidgetItem(f"  {category} → {tag}")
                self.tag_list.addItem(item)
        self.lbl_hint.setText(f"共 {len(self._all_tags)} 个标签，点击选择")

    def _on_skip(self):
        main_win = self.window()
        main_win.cfg_tags = []
        main_win.next_screen()

    def _on_confirm(self):
        selected = []
        for i in range(self.tag_list.count()):
            if self.tag_list.item(i).isSelected():
                tag = self._all_tags[i] if i < len(self._all_tags) else ""
                if tag:
                    selected.append(tag)
        main_win = self.window()
        main_win.cfg_tags = selected
        main_win.next_screen()


# ─── Dashboard ─────────────────────────────────────────────────────


class DashboardScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._worker = None

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Top bar
        top = QHBoxLayout()
        self.lbl_title = QLabel("CCBU-Auto 自动学习")
        self.lbl_title.setObjectName("title")
        top.addWidget(self.lbl_title)
        top.addStretch()
        self.lbl_status = QLabel("正在启动...")
        self.lbl_status.setObjectName("subtitle")
        top.addWidget(self.lbl_status)
        layout.addLayout(top)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Hours group
        hours_group = QGroupBox("培训学时")
        hours_form = QFormLayout()
        self.lbl_central = QLabel("-- 学时")
        self.lbl_online = QLabel("-- 学时")
        self.lbl_updated = QLabel("--")
        hours_form.addRow("集中培训:", self.lbl_central)
        hours_form.addRow("网络自学:", self.lbl_online)
        hours_form.addRow("更新时间:", self.lbl_updated)
        hours_group.setLayout(hours_form)
        left_layout.addWidget(hours_group)

        # Goal group
        goal_group = QGroupBox("学习目标")
        goal_layout = QVBoxLayout()
        self.lbl_goal_info = QLabel("--")
        self.progress_goal = QProgressBar()
        self.progress_goal.setValue(0)
        goal_layout.addWidget(self.lbl_goal_info)
        goal_layout.addWidget(self.progress_goal)
        goal_group.setLayout(goal_layout)
        left_layout.addWidget(goal_group)

        left_layout.addStretch()
        splitter.addWidget(left)

        # Right panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Worker table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["线程", "课程", "进度", "预计", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(2, 60)
        self.table.setColumnWidth(3, 60)
        self.table.setColumnWidth(4, 90)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)
        right_layout.addWidget(self.table)

        # Log
        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout()
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)
        log_group.setLayout(log_layout)
        log_group.setMaximumHeight(200)
        right_layout.addWidget(log_group)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)

        layout.addWidget(splitter)

    def start_learning(self):
        main_win = self.window()
        self._init_table(main_win.cfg_workers)
        self._set_goal_info(main_win)

        self._tag_event = threading.Event()
        self._tag_result = []

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
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QTableWidgetItem("-"))
            self.table.setItem(i, 2, QTableWidgetItem("-"))
            self.table.setItem(i, 3, QTableWidgetItem("-"))
            self.table.setItem(i, 4, QTableWidgetItem("等待中"))

    def _set_goal_info(self, main_win):
        goal_type = getattr(main_win, "cfg_goal_type", "central")
        goal_hours = getattr(main_win, "cfg_goal_hours", 0)
        type_name = "集中培训" if goal_type == "central" else "网络自学"
        if goal_hours > 0:
            self.lbl_goal_info.setText(f"{type_name} {goal_hours:.0f} 学时")
        else:
            self.lbl_goal_info.setText("不限制")

    async def _run_learning(self, thread: AsyncThread):
        main_win = self.window()
        cfg_workers = getattr(main_win, "cfg_workers", 1)
        cfg_headless = getattr(main_win, "cfg_headless", False)
        cfg_username = getattr(main_win, "cfg_username", "")
        cfg_password = getattr(main_win, "cfg_password", "")
        cfg_auto_login = getattr(main_win, "cfg_auto_login", True)
        cfg_goal_type = getattr(main_win, "cfg_goal_type", "central")
        cfg_goal_hours = getattr(main_win, "cfg_goal_hours", 0)
        cfg_tags = getattr(main_win, "cfg_tags", [])

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

            learner.study_goal = cfg_goal_hours
            learner.goal_type = cfg_goal_type
            if cfg_tags:
                learner.tags_to_learn = cfg_tags

            page = learner.pages[0]

            # 导航到专题班页面后加载标签
            await page.goto(
                "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                wait_until="networkidle", timeout=30000,
            )
            await page.wait_for_timeout(5000)

            # 如果没有CLI标签，尝试从浏览器加载并让用户选择
            if not cfg_tags:
                try:
                    tags_by_category = await learner.get_available_tags(page)
                    if tags_by_category:
                        tag_count = sum(len(v) for v in tags_by_category.values())
                        log(f"发现 {tag_count} 个标签，等待选择...", "blue")
                        # 请求UI弹出标签选择对话框
                        self._tag_event.clear()
                        thread.tag_request_signal.emit(tags_by_category)
                        # 等待用户选择（在子线程中阻塞等待，不阻塞UI）
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._tag_event.wait
                        )
                        cfg_tags = getattr(self.window(), "cfg_tags", [])
                except Exception as e:
                    log(f"标签加载失败: {e}", "yellow")

            if cfg_tags:
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

    @Slot(str, str)
    def _on_log(self, msg, style):
        ts = datetime.now().strftime("%H:%M:%S")
        if "red" in style:
            color = "#f38ba8"
        elif "green" in style:
            color = "#a6e3a1"
        elif "blue" in style:
            color = "#89b4fa"
        elif "yellow" in style:
            color = "#f9e2af"
        else:
            color = "#a6adc8"

        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)
        self.log_view.appendHtml(
            f'<span style="color:#585b70">[{ts}]</span> '
            f'<span style="color:{color}">{msg}</span>'
        )

    @Slot(dict)
    def _on_progress(self, data):
        wid = data.get("wid", 0)
        if wid >= self.table.rowCount():
            return

        course = data.get("course", "-")
        progress = data.get("progress", "-")
        eta = data.get("eta", "-")
        status = data.get("status", "-")

        self.table.setItem(wid, 1, QTableWidgetItem(str(course)[:40]))
        self.table.setItem(wid, 2, QTableWidgetItem(str(progress)))
        self.table.setItem(wid, 3, QTableWidgetItem(str(eta)))
        self.table.setItem(wid, 4, QTableWidgetItem(str(status)))

        # Color status
        status_item = self.table.item(wid, 4)
        if status in ("✓ 完成", "目标达成!"):
            status_item.setForeground(QColor("#a6e3a1"))
        elif "失败" in status or "异常" in status:
            status_item.setForeground(QColor("#f38ba8"))
        elif status == "学习中":
            status_item.setForeground(QColor("#89b4fa"))

    @Slot(dict)
    def _on_hours(self, data):
        self.lbl_central.setText(f"{data.get('central', 0):.1f} 学时")
        self.lbl_online.setText(f"{data.get('online', 0):.1f} 学时")
        self.lbl_updated.setText(data.get("updated", "--"))

        main_win = self.window()
        goal_type = getattr(main_win, "cfg_goal_type", "central")
        goal_hours = getattr(main_win, "cfg_goal_hours", 0)
        if goal_hours > 0:
            cur = data.get(goal_type, 0)
            pct = min(100, int(cur / goal_hours * 100))
            self.progress_goal.setValue(pct)
            type_name = "集中培训" if goal_type == "central" else "网络自学"
            self.lbl_goal_info.setText(f"{type_name} {cur:.1f}/{goal_hours:.0f} 学时 ({pct}%)")

    @Slot(int, int)
    def _on_done(self, success, failed):
        self.lbl_status.setText(f"[green]完成: 成功 {success}, 失败 {failed}[/green]")
        self._on_log("学习流程完成!", "green")

    @Slot(dict)
    def _on_tag_request(self, tags_by_category):
        """弹出标签选择对话框（在主线程中运行）。"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowTitle("选择标签")
        dlg.setMinimumWidth(400)
        dlg.setMinimumHeight(500)

        layout = QVBoxLayout(dlg)

        lbl = QLabel("选择要学习的标签（可多选，不选则全部学习）")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        all_tags = []
        for category, tags in tags_by_category.items():
            for tag in tags:
                all_tags.append(tag)
                list_widget.addItem(f"  {category} → {tag}")
        layout.addWidget(list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Skip)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认选择")
        buttons.button(QDialogButtonBox.StandardButton.Skip).setText("跳过（全部学习）")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.accept)  # Skip也用accept，通过result区分
        layout.addWidget(buttons)

        dlg.exec()

        # 获取选择结果
        selected = []
        if dlg.result() == QDialog.DialogCode.Accepted:
            for i in range(list_widget.count()):
                if list_widget.item(i).isSelected():
                    selected.append(all_tags[i])

        self._tag_result = selected
        main_win = self.window()
        main_win.cfg_tags = selected
        if selected:
            self._on_log(f"已选择 {len(selected)} 个标签", "green")
        else:
            self._on_log("未选择标签，将学习全部", "yellow")
        self._tag_event.set()


# ─── Main Window ───────────────────────────────────────────────────


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CCBU-Auto 自动学习")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)

        # Config state
        self.cfg_workers = 1
        self.cfg_headless = False
        self.cfg_username = ""
        self.cfg_password = ""
        self.cfg_auto_login = True
        self.cfg_goal_type = "central"
        self.cfg_goal_hours = 0
        self.cfg_tags = []

        # Stacked widget for screens
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.screen_config = ConfigScreen()
        self.screen_login = LoginScreen()
        self.screen_goal = GoalScreen()
        self.screen_dashboard = DashboardScreen()

        self.stack.addWidget(self.screen_config)   # 0
        self.stack.addWidget(self.screen_login)     # 1
        self.stack.addWidget(self.screen_goal)      # 2
        self.stack.addWidget(self.screen_dashboard) # 3

        self._screen_index = 0

    def next_screen(self):
        self._screen_index += 1
        if self._screen_index < self.stack.count():
            self.stack.setCurrentIndex(self._screen_index)
            if self._screen_index == 3:
                self.screen_dashboard.start_learning()


# ─── Entry Point ───────────────────────────────────────────────────


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
