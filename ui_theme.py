"""Moisten GUI 设计系统与轻量共享组件。

只依赖项目现有的 Qt/QFluentWidgets，不参与学习业务逻辑。
"""

from dataclasses import dataclass
from PySide6.QtCore import Qt, QSize, Signal, QRectF
from PySide6.QtGui import QColor, QPalette, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QSizePolicy, QVBoxLayout,
    QDialog, QWidget,
)

from qfluentwidgets import Theme, setTheme
from qfluentwidgets import NavigationWidget
from qfluentwidgets.components.navigation.navigation_widget import NavigationTreeItem, drawIcon


_NAVIGATION_PATCHED = False


def _patch_navigation_item_paint():
    """QFluent 导航项使用 QPainter 自绘，QSS 无法覆盖选中背景；统一改为参考稿的珊瑚色选中态。"""
    global _NAVIGATION_PATCHED
    if _NAVIGATION_PATCHED:
        return

    def paint(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing |
                               QPainter.SmoothPixmapTransform)
        if self.isPressed:
            painter.setOpacity(0.72)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        is_dark = QApplication.instance().palette().color(QPalette.Window).lightness() < 128
        active_bg = "#352A3B" if is_dark else "#FFF0F1"
        active_accent = "#FF9BA4" if is_dark else "#FF6E6A"
        normal_icon = "#E2ECFA" if is_dark else "#0B2347"
        if self.isSelected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(active_bg))
            painter.drawRoundedRect(self.rect(), 12, 12)
            painter.setBrush(QColor(active_accent))
            painter.drawRoundedRect(0, 0, 4, self.height(), 2, 2)
        elif self.isAboutSelected and self.isEnabled():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(78, 53, 67, 150) if is_dark else QColor(255, 240, 241, 150))
            painter.drawRoundedRect(self.rect(), 12, 12)
        drawIcon(self._icon, painter, QRectF(19.5, 9.5, 17, 17),
                 fill=active_accent if self.isSelected else normal_icon)
        if self.isCompacted:
            return
        font = self.font()
        font.setPointSize(14)
        painter.setFont(font)
        painter.setPen(self.textColor())
        painter.drawText(QRectF(54, 0, self.width() - 66, self.height()),
                         Qt.AlignVCenter, self._text)
        painter.end()

    NavigationTreeItem.paintEvent = paint
    _NAVIGATION_PATCHED = True


@dataclass(frozen=True)
class ThemeTokens:
    page: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    accent: str
    accent_soft: str
    accent_strong: str
    success: str
    warning: str
    danger: str
    shadow: str


LIGHT = ThemeTokens(
    page="#EDF7FC", surface="#FFFFFF", surface_alt="#F2F7FB", border="#DCE8F2",
    text="#0B2347", text_muted="#7690AF", accent="#2B83F6", accent_soft="#E4F0FF",
    accent_strong="#0D5CC4", success="#18B8B4", warning="#E5A04F", danger="#FF6E6A",
    shadow="rgba(25, 72, 120, 0.14)",
)

DARK = ThemeTokens(
    page="#111827", surface="#182236", surface_alt="#202D43", border="#33435E",
    text="#F4F7FF", text_muted="#AEB9CE", accent="#61A7FF", accent_soft="#203D67",
    accent_strong="#8AC2FF", success="#5DDBC5", warning="#F3BF6F", danger="#FF8E9E",
    shadow="rgba(0, 0, 0, 0.32)",
)


def normalize_theme_mode(mode: str) -> str:
    return mode if mode in {"auto", "light", "dark"} else "auto"


def _palette(tokens: ThemeTokens) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(tokens.page))
    palette.setColor(QPalette.Base, QColor(tokens.surface))
    palette.setColor(QPalette.AlternateBase, QColor(tokens.surface_alt))
    palette.setColor(QPalette.Button, QColor(tokens.surface))
    palette.setColor(QPalette.Text, QColor(tokens.text))
    palette.setColor(QPalette.WindowText, QColor(tokens.text))
    palette.setColor(QPalette.ButtonText, QColor(tokens.text))
    palette.setColor(QPalette.PlaceholderText, QColor(tokens.text_muted))
    palette.setColor(QPalette.Highlight, QColor(tokens.accent))
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    return palette


def _stylesheet(t: ThemeTokens) -> str:
    is_dark = t is DARK
    light_surface = "rgba(255, 255, 255, 0.88)" if t is LIGHT else t.surface
    light_border = "rgba(255, 255, 255, 0.96)" if t is LIGHT else t.border
    nav_surface = "rgba(255, 255, 255, 0.72)" if t is LIGHT else t.surface
    slogan_color = t.text_muted
    active_nav_bg = "#352A3B" if is_dark else "#FFF0F1"
    active_nav_fg = "#FF9BA4" if is_dark else "#FF6E6A"
    icon_surface = "#223653" if is_dark else "#E5F0FF"
    table_alt = "rgba(31, 45, 66, 0.72)" if is_dark else "rgba(246, 251, 254, 0.72)"
    table_header = "rgba(31, 45, 66, 0.92)" if is_dark else "rgba(240, 247, 252, 0.86)"
    status_active = ("#8AC2FF", "#203D67") if is_dark else ("#1774DB", "#DDEEFF")
    status_waiting = ("#B1BDD0", "#26354A") if is_dark else ("#71829A", "#EEF3F7")
    status_success = ("#69D8C9", "#174943") if is_dark else ("#078E86", "#DDF8F3")
    status_danger = ("#FF9BA4", "#4A2834") if is_dark else ("#D85E6B", "#FFF0F2")
    status_warning = ("#F3BF6F", "#4A3923") if is_dark else ("#AD7621", "#FFF6E6")
    status_pill_success = ("#69D8C9", "#174943", "#285B55") if is_dark else ("#087E83", "#D9F8F3", "#C6F1EB")
    progress_track = "#2B3B52" if is_dark else "#E1EBF3"
    progress_start = "#4CCDE0" if is_dark else "#49D3E3"
    progress_mid = "#2DBBCB" if is_dark else "#31C9D0"
    progress_end = "#25B8B0" if is_dark else "#13AAA6"
    return f"""
    QWidget {{ color: {t.text}; }}
    QMainWindow, QDialog {{ background: {t.page}; }}
    QScrollArea, QAbstractScrollArea {{ border: none; background: transparent; }}
    QFrame#surfaceCard, QFrame#heroCard, QFrame#navRail {{
        background: {light_surface};
        border: 1px solid {light_border};
        border-radius: 20px;
    }}
    HeaderCardWidget, CardWidget, SimpleCardWidget {{
        background-color: {light_surface}; color: {t.text};
        border: 1px solid {light_border}; border-radius: 20px;
    }}
    HeaderCardWidget QLabel, CardWidget QLabel, SimpleCardWidget QLabel {{ color: {t.text}; }}
    QLabel#eyebrow {{ color: {t.accent}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#pageTitle {{ color: {t.text}; font-size: 26px; font-weight: 700; }}
    QLabel#pageSubtitle, QLabel#muted {{ color: {t.text_muted}; }}
    QLabel#dashboardGreeting {{ color: {t.text_muted}; font-size: 13px; }}
    QLabel#dashboardTitle {{ color: {t.text}; font-size: 32px; font-weight: 800; }}
    QLabel#dashboardSubtitle {{ color: {t.text_muted}; font-size: 14px; }}
    QLabel#slogan {{ color: {slogan_color}; font-size: 13px; letter-spacing: 0.3px; }}
    QLabel#runtime {{ color: {t.text}; font-size: 17px; font-weight: 600; letter-spacing: 0.2px; }}
    QLabel#heroKicker {{ color: {t.accent_strong}; font-size: 12px; font-weight: 700; }}
    QLabel#heroTitle {{ color: {t.text}; font-size: 22px; font-weight: 800; }}
    QLabel#heroHint {{ color: {t.text_muted}; font-size: 13px; }}
    QLabel#cardTitle {{ color: {t.text}; font-size: 18px; font-weight: 800; }}
    QLabel#cardMetric {{ color: {t.text}; font-size: 20px; font-weight: 800; }}
    QLabel#metricLabel {{ color: {t.text}; font-size: 13px; font-weight: 700; }}
    QLabel#metricText {{ color: {t.text}; font-size: 15px; font-weight: 800; }}
    QLabel#tableStatus {{ border: none; border-radius: 14px; padding: 5px 14px; min-width: 70px; font-size: 13px; font-weight: 700; }}
    QLabel#tableStatus[kind="active"] {{ color: {status_active[0]}; background: {status_active[1]}; }}
    QLabel#tableStatus[kind="waiting"] {{ color: {status_waiting[0]}; background: {status_waiting[1]}; }}
    QLabel#tableStatus[kind="success"] {{ color: {status_success[0]}; background: {status_success[1]}; }}
    QLabel#tableStatus[kind="danger"] {{ color: {status_danger[0]}; background: {status_danger[1]}; }}
    QLabel#tableStatus[kind="warning"] {{ color: {status_warning[0]}; background: {status_warning[1]}; }}
    QLabel#cardMeta {{ color: {t.text_muted}; font-size: 12px; }}
    QLabel#brandTitle {{ color: {t.text}; font-size: 20px; font-weight: 800; }}
    QLabel#brandSub {{ color: {t.text_muted}; font-size: 11px; }}
    QLabel#statusPill {{
        color: {t.accent_strong}; background: {t.accent_soft};
        border: 1px solid rgba(43, 131, 246, 0.10); border-radius: 10px; padding: 7px 14px;
        font-weight: 700;
    }}
    QLabel#statusPill[success="true"] {{ color: {status_pill_success[0]}; background: {status_pill_success[1]}; border-color: {status_pill_success[2]}; }}
    QLabel#metricValue {{ color: {t.text}; font-size: 21px; font-weight: 700; }}
    QLabel#sessionMetric {{ color: {t.accent}; font-weight: 800; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
        background: {t.surface}; color: {t.text}; border: 1px solid {t.border};
        border-radius: 9px; padding: 7px 10px; selection-background-color: {t.accent};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border: 2px solid {t.accent}; padding: 6px 9px;
    }}
    QComboBox::drop-down {{
        width: 28px; border: none; background: transparent;
    }}
    QComboBox QAbstractItemView {{
        background: {t.surface}; color: {t.text};
        border: 1px solid {t.border}; outline: none;
        selection-background-color: {t.accent_soft};
        selection-color: {t.text}; padding: 4px;
    }}
    QComboBox QAbstractItemView::item {{
        min-height: 28px; padding: 5px 8px; border-radius: 6px;
    }}
    QComboBox QAbstractItemView::item:hover {{ background: {t.accent_soft}; }}
    QPushButton {{ border-radius: 11px; padding: 8px 14px; }}
    QPushButton:hover {{ background: {t.accent_soft}; }}
    QPushButton:pressed {{ padding-top: 8px; padding-bottom: 6px; }}
    QPushButton:disabled {{ color: {t.text_muted}; background: {t.surface_alt}; }}
    QToolButton {{ color: {t.text_muted}; border: none; border-radius: 11px; padding: 7px 9px; text-align: left; }}
    QToolButton:hover, QToolButton[active="true"], QPushButton[active="true"] {{ color: {t.accent_strong}; background: {t.accent_soft}; }}
    QToolButton#topIconButton {{ color: {t.text}; background: transparent; border: none; border-radius: 0; padding: 0; }}
    QToolButton#topIconButton:hover {{ color: {t.accent_strong}; background: transparent; }}
    QToolButton#topIconButton:disabled {{ color: {t.text_muted}; background: transparent; border: none; }}
    QTableWidget {{ background: transparent; color: {t.text}; border: none; gridline-color: {t.border}; alternate-background-color: {table_alt}; }}
    QTableWidget::item {{ padding: 10px 8px; border-bottom: 1px solid {t.border}; }}
    QTableWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
    QHeaderView::section {{ background: {table_header}; color: {t.text_muted}; border: none; padding: 5px 8px; font-weight: 700; }}
    QProgressBar {{ background: {progress_track}; color: {t.text}; border: none; border-radius: 7px; text-align: center; min-height: 14px; max-height: 14px; padding: 0; }}
    QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {progress_start}, stop:0.38 {progress_mid}, stop:0.72 #25BDBD, stop:1 {progress_end}); border-radius: 7px; margin: 0; }}
    QFrame#heroCard {{ background: transparent; border: none; border-radius: 20px; }}
    QFrame#heroArt {{ background: transparent; border: none; border-radius: 18px; }}
    QFrame#topToolbar {{ background: transparent; border: none; }}
    QFrame#cardIcon {{ background: {icon_surface}; border: none; border-radius: 11px; }}
    QFrame#navRail {{ background: {nav_surface}; border-radius: 22px; }}
    #navRail NavigationPushButton, #navRail QToolButton {{ border: none; border-radius: 12px; padding: 10px 12px; margin: 2px 10px; color: {t.text_muted}; }}
    #navRail NavigationPushButton:hover, #navRail QToolButton:hover {{ background: {t.surface_alt}; color: {t.text}; }}
    #navRail NavigationPushButton:checked, #navRail QToolButton[active="true"] {{ background: {active_nav_bg}; color: {active_nav_fg}; font-weight: 700; border-left: 4px solid {active_nav_fg}; }}
    #navRail QScrollArea, #navRail QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QMessageBox {{
        background: {t.surface}; color: {t.text};
        border: 1px solid {t.border}; border-radius: 18px;
    }}
    QMessageBox QLabel {{ color: {t.text}; font-size: 14px; }}
    QMessageBox QPushButton {{
        min-width: 76px; min-height: 32px; padding: 6px 14px;
        color: {t.text}; background: {t.surface_alt};
        border: 1px solid {t.border}; border-radius: 10px;
    }}
    QMessageBox QPushButton:hover {{ color: {t.accent_strong}; background: {t.accent_soft}; }}
    QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; padding: 6px; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px; }}
    QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """


def style_moisten_dialog(dialog):
    """把业务弹窗收敛到 Moisten 的卡片、按钮和深浅主题视觉语言。"""
    app = QApplication.instance()
    tokens = app.property("moisten_tokens") if app is not None else None
    if not isinstance(tokens, ThemeTokens):
        tokens = LIGHT

    dialog.setObjectName("moistenDialog")
    dialog.setAttribute(Qt.WA_StyledBackground, True)
    for attribute, object_name in (
        ("yesButton", "dialogPrimaryButton"),
        ("cancelButton", "dialogSecondaryButton"),
    ):
        button = getattr(dialog, attribute, None)
        if button is not None:
            button.setObjectName(object_name)

    # QFluentWidgets.Dialog 自带一个窗口标题标签，同时内容区还有一个标题标签。
    # 隐藏前者，只保留内容标题，避免出现截图中的重复标题和过大的顶部留白。
    if hasattr(dialog, "setTitleBarVisible"):
        dialog.setTitleBarVisible(False)
        window_title = getattr(dialog, "windowTitleLabel", None)
        if window_title is not None:
            window_title.hide()
            window_title.setFixedHeight(0)
        text_layout = getattr(dialog, "textLayout", None)
        if text_layout is not None:
            text_layout.setContentsMargins(24, 18, 24, 8)
            text_layout.setSpacing(8)
        button_group = getattr(dialog, "buttonGroup", None)
        button_layout = getattr(dialog, "buttonLayout", None)
        if button_group is not None:
            button_group.setFixedHeight(64)
        if button_layout is not None:
            button_layout.setContentsMargins(24, 8, 24, 16)
            button_layout.setSpacing(10)
        # Dialog 基类初始化时锁定了默认尺寸；释放后按紧凑布局重新计算，
        # 只固定高度，保留长更新说明所需的自适应宽度。
        dialog.setMinimumSize(0, 0)
        dialog.setMaximumSize(16777215, 16777215)
        dialog.setMinimumWidth(380)
        dialog.adjustSize()
        dialog.setFixedHeight(max(180, dialog.sizeHint().height()))

    dialog.setStyleSheet(f"""
        QDialog#moistenDialog {{
            background: {tokens.surface};
            color: {tokens.text};
            border: 1px solid {tokens.border};
            border-radius: 20px;
        }}
        QDialog#moistenDialog QLabel {{ color: {tokens.text}; }}
        QDialog#moistenDialog QLabel#windowTitleLabel,
        QDialog#moistenDialog QLabel#titleLabel,
        QDialog#moistenDialog SubtitleLabel {{
            color: {tokens.text};
            font-size: 18px;
            font-weight: 800;
        }}
        QDialog#moistenDialog QLabel#contentLabel,
        QDialog#moistenDialog BodyLabel {{
            color: {tokens.text_muted};
            font-size: 14px;
        }}
        QDialog#moistenDialog QLabel#muted,
        QDialog#moistenDialog CaptionLabel {{ color: {tokens.text_muted}; }}
        QDialog#moistenDialog QFrame#buttonGroup {{
            background: transparent;
            border: none;
        }}
        QDialog#moistenDialog QPushButton {{
            min-height: 36px;
            padding: 8px 16px;
            border: 1px solid {tokens.border};
            border-radius: 11px;
            color: {tokens.text};
            background: {tokens.surface_alt};
            font-size: 13px;
            font-weight: 700;
        }}
        QDialog#moistenDialog QPushButton:hover {{
            color: {tokens.accent_strong};
            background: {tokens.accent_soft};
            border-color: {tokens.accent};
        }}
        QDialog#moistenDialog QPushButton:pressed {{
            padding-top: 9px;
            padding-bottom: 7px;
        }}
        QDialog#moistenDialog QPushButton#dialogPrimaryButton {{
            color: #FFFFFF;
            border: none;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 {tokens.accent}, stop:1 {tokens.success});
        }}
        QDialog#moistenDialog QPushButton#dialogPrimaryButton:hover {{
            color: #FFFFFF;
            background: {tokens.accent_strong};
        }}
        QDialog#moistenDialog QLineEdit,
        QDialog#moistenDialog QPlainTextEdit,
        QDialog#moistenDialog QTextEdit,
        QDialog#moistenDialog QComboBox {{
            background: {tokens.surface_alt};
            color: {tokens.text};
            border: 1px solid {tokens.border};
            border-radius: 9px;
            padding: 7px 10px;
        }}
        QDialog#moistenDialog QProgressBar {{
            min-height: 12px;
            max-height: 12px;
            border: none;
            border-radius: 6px;
            background: {tokens.surface_alt};
        }}
        QDialog#moistenDialog QProgressBar::chunk {{
            border-radius: 6px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 {tokens.accent}, stop:1 {tokens.success});
        }}
    """)
    if hasattr(dialog, "setTitleBarVisible"):
        # 样式表会改变标题和正文的 sizeHint，再计算一次高度，避免按钮被推到过低位置。
        dialog.setMinimumHeight(0)
        dialog.setMaximumHeight(16777215)
        dialog.adjustSize()
        dialog.setFixedHeight(max(180, dialog.sizeHint().height()))


def apply_theme(app, mode: str = "auto") -> ThemeTokens:
    """应用 QFluentWidgets 主题和 Moisten 调色板，返回当前 token。"""
    mode = normalize_theme_mode(mode)
    if mode == "dark":
        setTheme(Theme.DARK)
        tokens = DARK
    elif mode == "light":
        setTheme(Theme.LIGHT)
        tokens = LIGHT
    else:
        # 不从 app.palette() 判断系统主题：该 palette 是我们上一次应用的
        # 自定义 palette，切回“跟随系统”时会把旧的深色/浅色误认为系统主题，
        # 造成 QFluentWidgets 与自定义控件混用两套颜色。
        scheme = None
        try:
            scheme = app.styleHints().colorScheme()
        except Exception:
            pass
        if scheme == Qt.ColorScheme.Dark:
            tokens = DARK
        elif scheme == Qt.ColorScheme.Light:
            tokens = LIGHT
        else:
            # 低版本 Qt 或离屏环境可能没有可用的 colorScheme，读取 style
            # 的标准 palette；它不受 app.setPalette() 的自定义值污染。
            try:
                standard = app.style().standardPalette()
                is_dark = standard.color(QPalette.Window).lightness() < 128
            except Exception:
                is_dark = False
            tokens = DARK if is_dark else LIGHT
        # 显式选择当前系统主题，避免 Theme.AUTO 在已设置自定义 palette 后
        # 仍保留旧主题组件，产生深浅色混杂。
        setTheme(Theme.DARK if tokens is DARK else Theme.LIGHT)
    app.setPalette(_palette(tokens))
    family = app.font().family().replace("'", "")
    app.setStyleSheet(f"* {{ font-family: '{family}'; }}\n" + _stylesheet(tokens))
    combos = []
    for window in app.topLevelWidgets():
        combos.extend(window.findChildren(QComboBox))
    for combo in combos:
        view = combo.view()
        popup_palette = view.palette()
        popup_palette.setColor(QPalette.Base, QColor(tokens.surface))
        popup_palette.setColor(QPalette.Window, QColor(tokens.surface))
        popup_palette.setColor(QPalette.AlternateBase, QColor(tokens.surface_alt))
        popup_palette.setColor(QPalette.Text, QColor(tokens.text))
        popup_palette.setColor(QPalette.Highlight, QColor(tokens.accent_soft))
        popup_palette.setColor(QPalette.HighlightedText, QColor(tokens.text))
        view.setPalette(popup_palette)
        view.setAutoFillBackground(True)
        view.setStyleSheet(
            f"QAbstractItemView {{ background: {tokens.surface}; color: {tokens.text}; "
            f"border: 1px solid {tokens.border}; selection-background-color: {tokens.accent_soft}; "
            f"selection-color: {tokens.text}; }}"
        )
    app.setProperty("moisten_theme_mode", mode)
    app.setProperty("moisten_tokens", tokens)
    _patch_navigation_item_paint()
    navigation = app.property("moisten_navigation")
    if navigation is not None and hasattr(navigation, "refresh_icons"):
        navigation.refresh_icons(mode, tokens.text_muted, tokens.accent_strong)
    if navigation is not None and hasattr(navigation, "panel"):
        for item in getattr(navigation.panel, "items", {}).values():
            tree = getattr(item, "widget", None)
            nav_item = getattr(tree, "itemWidget", None)
            if nav_item is not None:
                nav_item.lightTextColor = QColor(tokens.text)
                nav_item.darkTextColor = QColor(tokens.text)
                nav_item.update()
    # 自绘的 Hero、目标环和导航波纹不完全依赖 QSS，主题切换后显式刷新一次，
    # 避免它们停留在切换前的颜色层级。
    for window in app.topLevelWidgets():
        if isinstance(window, QDialog) and window.objectName() == "moistenDialog":
            style_moisten_dialog(window)
        window.update()
        for child in window.findChildren(QWidget):
            if isinstance(child, QDialog) and child.objectName() == "moistenDialog":
                style_moisten_dialog(child)
            child.update()
    return tokens


class SurfaceCard(QFrame):
    def __init__(self, parent=None, object_name: str = "surfaceCard"):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", eyebrow: str = "润物 MOISTEN", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        self.eyebrow = QLabel(eyebrow)
        self.eyebrow.setObjectName("eyebrow")
        self.title = QLabel(title)
        self.title.setObjectName("pageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.eyebrow)
        layout.addWidget(self.title)
        if subtitle:
            layout.addWidget(self.subtitle)


class StepBar(QWidget):
    def __init__(self, steps, active: int = 0, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        for index, text in enumerate(steps):
            label = QLabel(f"{index + 1:02d}  {text}")
            label.setObjectName("statusPill" if index == active else "muted")
            layout.addWidget(label)
            if index < len(steps) - 1:
                separator = QLabel("›")
                separator.setObjectName("muted")
                layout.addWidget(separator)
        layout.addStretch()


class BrandMark(QWidget):
    """品牌区使用的蓝青双色书页标志。"""

    def __init__(self, image_path: str = "", parent=None):
        super().__init__(parent)
        self.setFixedSize(46, 46)
        self._pixmap = QPixmap(image_path) if image_path else QPixmap()

    def paintEvent(self, event):
        del event
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        if not self._pixmap.isNull():
            p.drawPixmap(QRectF(1, 1, 44, 44), self._pixmap, self._pixmap.rect())
        p.end()


class BrandNavigationWidget(NavigationWidget):
    """官方 NavigationInterface 顶部品牌区，不参与页面选择。"""

    def __init__(self, image_path: str, parent=None):
        super().__init__(False, parent)
        self.setFixedHeight(154)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 24, 16, 12)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignLeft)
        top = QHBoxLayout()
        top.setContentsMargins(12, 0, 0, 0)
        top.setSpacing(10)
        top.addWidget(BrandMark(image_path, self))
        wordmark = QLabel("润物\nMoisten", self)
        wordmark.setObjectName("brandTitle")
        top.addWidget(wordmark)
        layout.addLayout(top)
        sub = QLabel("让学习 · 如水润物", self)
        sub.setObjectName("brandSub")
        layout.addWidget(sub)

    def setCompacted(self, isCompacted: bool):
        """官方基类会把自定义项强制设为 36px 高，这里保留品牌图标高度。"""
        super().setCompacted(isCompacted)
        self.setFixedHeight(154)
