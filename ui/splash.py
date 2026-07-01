"""
Startup splash screen — shown during boot, closed when the main window appears.

Usage in main.py:
    splash = SplashScreen(first_launch=is_first)
    splash.show()
    app.processEvents()

    splash.step(20, "Starting playback engine…")   # pct 0-100, any message

    splash.finish()  # hides the splash; call just before window.show()
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget


class SplashScreen(QWidget):
    def __init__(self, first_launch: bool = False) -> None:
        super().__init__()
        self._first_launch = first_launch

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFixedSize(480, 210)
        self.setStyleSheet(
            "QWidget { background-color: #000000; border: 1px solid rgba(0,255,65,0.25); }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(44, 32, 44, 28)
        root.setSpacing(0)

        # ── Logo ──────────────────────────────────────────────────────────────
        logo = QLabel("MusicHat")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "QLabel {"
            "  color: #00ff41;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 34px;"
            "  letter-spacing: 2px;"
            "  border: none;"
            "  background: transparent;"
            "}"
        )
        root.addWidget(logo)

        byline = QLabel("by xwhitehat")
        byline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        byline.setStyleSheet(
            "QLabel {"
            "  color: #4d8a5f;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 11px;"
            "  border: none;"
            "  background: transparent;"
            "  margin-bottom: 20px;"
            "}"
        )
        root.addWidget(byline)

        # ── Step label ────────────────────────────────────────────────────────
        self._step_lbl = QLabel("Initialising…")
        self._step_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._step_lbl.setStyleSheet(
            "QLabel {"
            "  color: #b8ffca;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 12px;"
            "  border: none;"
            "  background: transparent;"
            "  margin-bottom: 10px;"
            "}"
        )
        root.addWidget(self._step_lbl)

        # ── Progress bar ──────────────────────────────────────────────────────
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setStyleSheet(
            "QProgressBar {"
            "  background-color: rgba(0,255,65,0.08);"
            "  border: 1px solid rgba(0,255,65,0.18);"
            "  border-radius: 2px;"
            "}"
            "QProgressBar::chunk {"
            "  background-color: #00ff41;"
            "  border-radius: 2px;"
            "}"
        )
        root.addWidget(self._bar)

        # ── Percentage ────────────────────────────────────────────────────────
        self._pct_lbl = QLabel("0%")
        self._pct_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pct_lbl.setStyleSheet(
            "QLabel {"
            "  color: #4d8a5f;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 10px;"
            "  border: none;"
            "  background: transparent;"
            "  margin-top: 6px;"
            "}"
        )
        root.addWidget(self._pct_lbl)

        self._center()

    def _center(self) -> None:
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            self.move(
                (geo.width() - self.width()) // 2,
                (geo.height() - self.height()) // 2,
            )

    def step(self, pct: int, message: str) -> None:
        """Update progress. Call app.processEvents() is handled internally."""
        self._step_lbl.setText(message)
        self._bar.setValue(pct)
        self._pct_lbl.setText(f"{pct}%")
        QApplication.processEvents()

    def finish(self) -> None:
        self.close()


class RemovalSplash(QWidget):
    """Shown while local data is being wiped before exit."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFixedSize(480, 210)
        self.setStyleSheet(
            "QWidget { background-color: #000000; border: 1px solid rgba(255,68,68,0.25); }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(44, 32, 44, 28)
        root.setSpacing(0)

        logo = QLabel("MusicHat")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "QLabel {"
            "  color: #ff4444;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 34px;"
            "  letter-spacing: 2px;"
            "  border: none;"
            "  background: transparent;"
            "}"
        )
        root.addWidget(logo)

        byline = QLabel("by xwhitehat")
        byline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        byline.setStyleSheet(
            "QLabel {"
            "  color: #8a4d4d;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 11px;"
            "  border: none;"
            "  background: transparent;"
            "  margin-bottom: 28px;"
            "}"
        )
        root.addWidget(byline)

        self._msg = QLabel("Removing local data…")
        self._msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._msg.setStyleSheet(
            "QLabel {"
            "  color: #ffb8b8;"
            "  font-family: 'Share Tech Mono', 'Courier New', monospace;"
            "  font-size: 12px;"
            "  border: none;"
            "  background: transparent;"
            "}"
        )
        root.addWidget(self._msg)

        self._center()

    def _center(self) -> None:
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            self.move(
                (geo.width() - self.width()) // 2,
                (geo.height() - self.height()) // 2,
            )

    def set_message(self, msg: str) -> None:
        self._msg.setText(msg)
        QApplication.processEvents()
