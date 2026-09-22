"""Moisten GUI 设计系统与轻量共享组件。

只依赖项目现有的 Qt/QFluentWidgets，不参与学习业务逻辑。
"""

from dataclasses import dataclass
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from qfluentwidgets import Theme, setTheme
from qfluentwidgets import NavigationWidget


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
    page="#F3F8F7", surface="#FFFFFF", surface_alt="#EAF4F3", border="#D7E6E4",
    text="#17363A", text_muted="#688084", accent="#087F88", accent_soft="#DDF3F2",
    accent_strong="#05636B", success="#23814A", warning="#A66C00", danger="#C23B4A",
    shadow="rgba(19, 67, 72, 0.10)",
)

DARK = ThemeTokens(
    page="#0D191B", surface="#152729", surface_alt="#1B3436", border="#2B4A4C",
    text="#E8F5F4", text_muted="#A4BCBC", accent="#63D1D4", accent_soft="#21484B",
    accent_strong="#9AE6E7", success="#6FD39A", warning="#F0C36B", danger="#F08D98",
    shadow="rgba(0, 0, 0, 0.30)",
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
    return f"""
    QWidget {{ color: {t.text}; }}
    QMainWindow, QDialog {{ background: {t.page}; }}
    QScrollArea, QAbstractScrollArea {{ border: none; background: transparent; }}
    QFrame#surfaceCard, QFrame#heroCard, QFrame#navRail {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 14px;
    }}
    HeaderCardWidget, CardWidget, SimpleCardWidget {{
        background-color: {t.surface}; color: {t.text};
        border: 1px solid {t.border}; border-radius: 14px;
    }}
    HeaderCardWidget QLabel, CardWidget QLabel, SimpleCardWidget QLabel {{ color: {t.text}; }}
    QLabel#eyebrow {{ color: {t.accent}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#pageTitle {{ color: {t.text}; font-size: 26px; font-weight: 700; }}
    QLabel#pageSubtitle, QLabel#muted {{ color: {t.text_muted}; }}
    QLabel#statusPill {{
        color: {t.accent_strong}; background: {t.accent_soft};
        border: 1px solid {t.border}; border-radius: 9px; padding: 5px 10px;
        font-weight: 700;
    }}
    QLabel#heroTitle {{ color: {t.text}; font-size: 18px; font-weight: 700; }}
    QLabel#metricValue {{ color: {t.text}; font-size: 21px; font-weight: 700; }}
    QLabel#sessionMetric {{ color: {t.accent}; font-weight: 700; }}
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
    QPushButton {{ border-radius: 9px; padding: 7px 14px; }}
    QPushButton:hover {{ background: {t.accent_soft}; }}
    QPushButton:pressed {{ padding-top: 8px; padding-bottom: 6px; }}
    QPushButton:disabled {{ color: {t.text_muted}; background: {t.surface_alt}; }}
    QToolButton {{ color: {t.text_muted}; border: none; border-radius: 9px; padding: 6px 8px; text-align: left; }}
    QToolButton:hover, QToolButton[active="true"], QPushButton[active="true"] {{ color: {t.accent_strong}; background: {t.accent_soft}; }}
    QTableWidget {{ background: {t.surface}; border: none; gridline-color: {t.border}; }}
    QHeaderView::section {{ background: {t.surface_alt}; color: {t.text_muted}; border: none; padding: 8px; }}
    QProgressBar {{ background: {t.surface_alt}; color: {t.text}; border: none; border-radius: 5px; text-align: center; min-height: 10px; }}
    QProgressBar::chunk {{ background: {t.accent}; border-radius: 5px; }}
    QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; padding: 6px; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px; }}
    QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """


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
    navigation = app.property("moisten_navigation")
    if navigation is not None and hasattr(navigation, "refresh_icons"):
        navigation.refresh_icons(mode, tokens.text_muted, tokens.accent_strong)
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


class BrandNavigationWidget(NavigationWidget):
    """官方 NavigationInterface 顶部品牌图标，不参与页面选择。"""

    def __init__(self, image_path: str, parent=None):
        super().__init__(False, parent)
        self.setFixedHeight(72)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setAlignment(Qt.AlignCenter)
        label = QLabel(self)
        label.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            label.setPixmap(pixmap.scaled(QSize(40, 40), Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        layout.addWidget(label)

    def setCompacted(self, isCompacted: bool):
        """官方基类会把自定义项强制设为 36px 高，这里保留品牌图标高度。"""
        super().setCompacted(isCompacted)
        self.setFixedHeight(72)
