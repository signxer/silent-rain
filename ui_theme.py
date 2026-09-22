"""Moisten GUI 设计系统与轻量共享组件。

只依赖项目现有的 Qt/QFluentWidgets，不参与学习业务逻辑。
"""

from dataclasses import dataclass
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from qfluentwidgets import Theme, setTheme


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
        setTheme(Theme.AUTO)
        # Qt 的 palette 已经完成系统主题判断；浅色 token 是更安全的初始化值。
        tokens = DARK if app.palette().color(QPalette.Window).lightness() < 128 else LIGHT
    app.setPalette(_palette(tokens))
    family = app.font().family().replace("'", "")
    app.setStyleSheet(f"* {{ font-family: '{family}'; }}\n" + _stylesheet(tokens))
    app.setProperty("moisten_theme_mode", mode)
    app.setProperty("moisten_tokens", tokens)
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


class NavigationRail(QFrame):
    pageSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setFixedWidth(190)
        self._buttons = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(6)
        self.brand = QLabel("润物\nMOISTEN")
        self.brand.setObjectName("heroTitle")
        self.brand.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.brand)
        self._items_layout = QVBoxLayout()
        self._items_layout.setSpacing(5)
        layout.addLayout(self._items_layout)
        layout.addStretch()
        self.caption = QLabel("学习工作台")
        self.caption.setObjectName("muted")
        self.caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.caption)

    def add_item(self, key: str, icon, text: str):
        button = QPushButton(self)
        button.setToolTip(text)
        button.setIcon(icon.icon() if hasattr(icon, "icon") else icon)
        button.setText(text)
        button.setIconSize(QSize(18, 18))
        button.setFixedHeight(42)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button.clicked.connect(lambda: self.pageSelected.emit(key))
        self._items_layout.addWidget(button)
        self._buttons[key] = button

    def set_active(self, key: str):
        for name, button in self._buttons.items():
            button.setProperty("active", name == key)
            button.style().unpolish(button)
            button.style().polish(button)
