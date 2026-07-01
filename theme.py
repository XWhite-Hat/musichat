"""
Design system ported from xwhitehat.dev.

Palette
-------
BG           #000000  —  pure black page background
CARD_BG      #040e06  —  dark green-tinted card surface (88% opaque on site)
GREEN        #00ff41  —  matrix green primary accent
GREEN_DIM    #00cc33  —  dimmer green for hover states
TEXT         #b8ffca  —  soft mint — primary readable text
TEXT_DIM     #4d8a5f  —  muted green — secondary / label text
RED          #ff3333  —  error / offline LED
YELLOW       #ffe033  —  warning LED
AMBER        #e07b00  —  degraded LED

Font: Share Tech Mono (Google Fonts) → Fira Code → Courier New → monospace
"""

from __future__ import annotations

# ── Colour tokens ──────────────────────────────────────────────────────────────
BG = "#000000"
CARD_BG = "#040e06"
GREEN = "#00ff41"
GREEN_DIM = "#00cc33"
GREEN_GLOW = "rgba(0,255,65,0.35)"
GREEN_SUBTLE = "rgba(0,255,65,0.08)"
GREEN_BORDER = "rgba(0,255,65,0.2)"
TEXT = "#b8ffca"
TEXT_DIM = "#4d8a5f"
RED = "#ff3333"
YELLOW = "#ffe033"
AMBER = "#e07b00"

FONT_FAMILY = '"Share Tech Mono", "Fira Code", "Courier New", monospace'

# ── Spectrogram gradient presets ───────────────────────────────────────────────
GRADIENT_PRESETS: dict[str, tuple[str, str, str]] = {
    # name: (start, mid, end)  — low intensity → high intensity
    "matrix":  ("#001a00", "#00cc33", "#00ff41"),
    "fire":    ("#1a0000", "#ff4400", "#ffff00"),
    "cyan":    ("#001a1a", "#00aaaa", "#00ffff"),
    "purple":  ("#0a0010", "#5500cc", "#9146ff"),
    "sunset":  ("#1a0010", "#cc0044", "#ff8800"),
    "ice":     ("#001020", "#0066cc", "#88ddff"),
    "mono":    ("#222222", "#888888", "#ffffff"),
}

# ── Full application QSS stylesheet ────────────────────────────────────────────
APP_QSS = """
/* ── Global ── */
* {
    font-family: "Share Tech Mono", "Courier New", monospace;
    font-size: 13px;
    color: #b8ffca;
    outline: none;
}

QMainWindow, QDialog {
    background-color: #000000;
}

QWidget {
    background-color: #000000;
    color: #b8ffca;
}

/* ── Cards / Frames ── */
QFrame#card {
    background-color: #040e06;
    border: 1px solid rgba(0,255,65,0.18);
    border-radius: 12px;
}

/* ── Tabs ── */
QTabWidget::pane {
    background-color: #040e06;
    border: 1px solid rgba(0,255,65,0.18);
    border-radius: 0 8px 8px 8px;
}

QTabBar::tab {
    background-color: #000000;
    color: #4d8a5f;
    border: 1px solid rgba(0,255,65,0.12);
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    padding: 7px 18px;
    margin-right: 2px;
    letter-spacing: 0.08em;
    font-size: 11px;
}

QTabBar::tab:selected {
    background-color: #040e06;
    color: #00ff41;
    border-color: rgba(0,255,65,0.35);
}

QTabBar::tab:hover:!selected {
    color: #b8ffca;
    background-color: rgba(0,255,65,0.06);
}

/* ── Buttons ── */
QPushButton {
    background-color: rgba(0,255,65,0.06);
    color: #b8ffca;
    border: 1px solid rgba(0,255,65,0.2);
    border-radius: 6px;
    padding: 7px 18px;
    letter-spacing: 0.06em;
    font-size: 12px;
}

QPushButton:hover {
    background-color: rgba(0,255,65,0.12);
    border-color: #00ff41;
    color: #00ff41;
}

QPushButton:pressed {
    background-color: rgba(0,255,65,0.2);
    border-color: #00cc33;
}

QPushButton:disabled {
    color: #2a4a30;
    border-color: rgba(0,255,65,0.06);
}

QPushButton#accent {
    background-color: rgba(0,255,65,0.15);
    border-color: #00ff41;
    color: #00ff41;
}

QPushButton#accent:hover {
    background-color: rgba(0,255,65,0.25);
}

QPushButton#danger {
    border-color: rgba(255,51,51,0.4);
    color: #ff6666;
}

QPushButton#danger:hover {
    background-color: rgba(255,51,51,0.12);
    border-color: #ff3333;
    color: #ff3333;
}

/* ── Line edits / inputs ── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #000000;
    border: 1px solid rgba(0,255,65,0.2);
    border-radius: 5px;
    padding: 5px 10px;
    color: #b8ffca;
    selection-background-color: rgba(0,255,65,0.25);
}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #00ff41;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid #4d8a5f;
    margin-right: 6px;
}

QComboBox QAbstractItemView {
    background-color: #040e06;
    border: 1px solid rgba(0,255,65,0.25);
    selection-background-color: rgba(0,255,65,0.15);
    color: #b8ffca;
    border-radius: 5px;
    padding: 4px;
}

/* ── Sliders ── */
QSlider {
    border: none;
    background: transparent;
}

QSlider::groove:horizontal {
    height: 3px;
    background: rgba(0,255,65,0.15);
    border-radius: 2px;
    border: none;
}

QSlider::sub-page:horizontal {
    background: #00ff41;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: #00ff41;
    border: 2px solid #000000;
    width: 14px;
    height: 14px;
    border-radius: 7px;
    margin: -6px 0;
}

QSlider::handle:horizontal:hover {
    background: #00cc33;
    border-color: #00ff41;
}

/* ── Checkboxes ── */
QCheckBox {
    spacing: 8px;
    color: #b8ffca;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid rgba(0,255,65,0.35);
    border-radius: 3px;
    background-color: #000000;
}

QCheckBox::indicator:checked {
    background-color: rgba(0,255,65,0.2);
    border-color: #00ff41;
    image: none;
}

QCheckBox::indicator:hover {
    border-color: #00ff41;
}

/* ── Scrollbars ── */
QScrollBar:vertical {
    background: #000000;
    width: 8px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: rgba(0,255,65,0.25);
    border-radius: 4px;
    min-height: 20px;
}

QScrollBar::handle:vertical:hover {
    background: rgba(0,255,65,0.45);
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background: #000000;
    height: 8px;
    border-radius: 4px;
}

QScrollBar::handle:horizontal {
    background: rgba(0,255,65,0.25);
    border-radius: 4px;
    min-width: 20px;
}

QScrollBar::handle:horizontal:hover {
    background: rgba(0,255,65,0.45);
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ── List / Table widgets ── */
QListWidget, QTableWidget, QTreeWidget {
    background-color: #000000;
    border: 1px solid rgba(0,255,65,0.15);
    border-radius: 6px;
    alternate-background-color: rgba(0,255,65,0.03);
}

QListWidget::item, QTableWidget::item {
    padding: 6px 10px;
    border-bottom: 1px solid rgba(0,255,65,0.06);
    color: #b8ffca;
}

QListWidget::item:selected, QTableWidget::item:selected {
    background-color: rgba(0,255,65,0.12);
    color: #00ff41;
    border-color: rgba(0,255,65,0.3);
}

QListWidget::item:hover, QTableWidget::item:hover {
    background-color: rgba(0,255,65,0.06);
}

QHeaderView::section {
    background-color: #040e06;
    color: #4d8a5f;
    border: none;
    border-bottom: 1px solid rgba(0,255,65,0.18);
    padding: 6px 10px;
    letter-spacing: 0.08em;
    font-size: 11px;
}

/* ── Labels ── */
QLabel {
    color: #b8ffca;
    background: transparent;
}

QLabel#dim {
    color: #4d8a5f;
    font-size: 11px;
    letter-spacing: 0.06em;
}

QLabel#green {
    color: #00ff41;
}

QLabel#heading {
    color: #00ff41;
    font-size: 15px;
    letter-spacing: 0.06em;
    font-weight: bold;
}

/* ── Tooltips ── */
QToolTip {
    background-color: #040e06;
    color: #b8ffca;
    border: 1px solid rgba(0,255,65,0.35);
    border-radius: 5px;
    padding: 5px 9px;
    font-size: 12px;
}

/* ── Group boxes ── */
QGroupBox {
    border: 1px solid rgba(0,255,65,0.15);
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 8px;
    color: #4d8a5f;
    letter-spacing: 0.08em;
    font-size: 11px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 8px;
    color: #4d8a5f;
    font-size: 11px;
    left: 12px;
}

/* ── Splitter ── */
QSplitter::handle {
    background-color: rgba(0,255,65,0.12);
}

QSplitter::handle:hover {
    background-color: rgba(0,255,65,0.3);
}

/* ── Custom title bar ── */
#TitleBar {
    background-color: #040e06;
    border-bottom: 1px solid rgba(0,255,65,0.12);
}
#titleBarLabel {
    color: #00ff41;
    font-size: 12px;
    font-weight: bold;
    letter-spacing: 0.1em;
}
#titleBtnMin, #titleBtnMax, #titleBtnClose {
    background: transparent;
    border: none;
    color: #4d8a5f;
    font-size: 13px;
    padding: 0;
}
#titleBtnMin:hover, #titleBtnMax:hover {
    background: rgba(0,255,65,0.08);
    color: #00ff41;
}
#titleBtnClose:hover {
    background: rgba(255,51,51,0.15);
    color: #ff3333;
}

/* ── Status bar ── */
QStatusBar {
    background-color: #040e06;
    color: #4d8a5f;
    border-top: 1px solid rgba(0,255,65,0.12);
    font-size: 11px;
    letter-spacing: 0.04em;
}
QStatusBar::item {
    border: none;
}
QSizeGrip {
    width: 0px;
    height: 0px;
    background: transparent;
}

/* ── Progress bar ── */
QProgressBar {
    background-color: rgba(0,255,65,0.08);
    border: 1px solid rgba(0,255,65,0.2);
    border-radius: 4px;
    height: 6px;
    text-align: center;
    color: transparent;
}

QProgressBar::chunk {
    background-color: #00ff41;
    border-radius: 3px;
}

/* ── Text edit (chat log) ── */
QTextEdit, QPlainTextEdit {
    background-color: #000000;
    border: 1px solid rgba(0,255,65,0.15);
    border-radius: 6px;
    color: #b8ffca;
    selection-background-color: rgba(0,255,65,0.2);
    padding: 6px;
}

/* ── Spin box arrows ── */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background: transparent;
    border: none;
    width: 16px;
}

QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #4d8a5f;
}

QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #4d8a5f;
}
"""
