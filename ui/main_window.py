"""
Main application window.

Layout (top → bottom)
──────────────────────
  ┌─ status bar (tunnel URL · bot status · server status) ──────────────────┐
  │ ┌─ now playing card ──────────────────────────────────────────────────┐ │
  │ │  thumbnail | title | artist | source badge                            │ │
  │ └─────────────────────────────────────────────────────────────────────┘ │
  │ ┌─ transport bar ──────────────────────────────────────────────────────┐ │
  │ │  ◀◀  ▶/❚❚  ▶▶  ──────────────●──────────── 02:34 / 04:12  🔊 ───  │ │
  │ └─────────────────────────────────────────────────────────────────────┘ │
  │ ┌─ tabs ──────────────────────────────────────────────────────────────┐ │
  │ │  VISUALISER | QUEUE | CHAT LOG | SEARCH                             │ │
  │ │  (content area)                                                      │ │
  │ └─────────────────────────────────────────────────────────────────────┘ │
  └─────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import sys

from PySide6.QtCore import QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor, QIcon, QKeySequence,
    QPainter, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
import pathlib
from config import AppConfig
from player.engine import PlayState, PlaybackEngine
from player.fft import FFTPipeline
from player.history_manager import PlayHistory
from player.playlist_manager import PlaylistManager
from player.queue_manager import QueueManager, Track, _merge_credited_artists
from server.routes import spectrogram as spec_routes
from theme import APP_QSS, GREEN
from ui.playlist_panel import PlaylistPanel
from ui.spectrogram_widget import SpectrogramWidget


class LedIndicator(QLabel):
    """Small status LED."""

    COLOURS = {
        "green":  "background:#00ff41; border-radius:5px; border:1px solid #00cc33;",
        "red":    "background:#ff3333; border-radius:5px; border:1px solid #cc0000;",
        "yellow": "background:#ff9500; border-radius:5px; border:1px solid #cc7700;",
        "grey":   "background:#333333; border-radius:5px; border:1px solid #222222;",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.set_state("grey")

    def set_state(self, colour: str) -> None:
        style = self.COLOURS.get(colour, self.COLOURS["grey"])
        self.setStyleSheet(f"QLabel {{ {style} }}")


class TunnelStatusWidget(QWidget):
    """Status-bar widget for the tunnel: offline label or service + copy button."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._led = LedIndicator()
        self._label = QLabel("tunnel: offline")
        self._label.setObjectName("dim")
        self._copy_btn = QPushButton("copy")
        self._copy_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid rgba(0,255,65,0.25);
                border-radius: 3px;
                color: #4d8a5f;
                padding: 1px 6px;
                font-size: 10px;
                letter-spacing: 0.06em;
            }
            QPushButton:hover {
                border-color: #00ff41;
                color: #00ff41;
                background: rgba(0,255,65,0.08);
            }
        """)
        self._copy_btn.setVisible(False)
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.clicked.connect(self._copy_url)
        self._url: str = ""

        layout.addWidget(self._led)
        layout.addWidget(self._label)
        layout.addWidget(self._copy_btn)

    def set_status(self, display: str, state: str, copy_url: str = "") -> None:
        self._led.set_state(state)
        self._label.setText(f"tunnel: {display}")
        self._url = copy_url
        self._copy_btn.setVisible(bool(state == "green" and copy_url))
        self._copy_btn.setText("copy")

    def _copy_url(self) -> None:
        if self._url:
            QApplication.clipboard().setText(self._url)
            self._copy_btn.setText("copied!")
            QTimer.singleShot(2000, lambda: self._copy_btn.setText("copy"))


def _rel_time(iso_ts: str) -> str:
    """Return a human-readable relative time string from a UTC ISO 8601 timestamp."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return iso_ts[11:19] if len(iso_ts) >= 19 else iso_ts
    if diff < 10:
        return "just now"
    if diff < 60:
        return f"{diff}s ago"
    m = diff // 60
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


class ActionsPanel(QWidget):
    """Mirror of the mod panel action log — tabular view of recent actions."""

    _COLS = ["time", "actor", "action", "track"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # time
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # actor
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # action
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)           # track

        layout.addWidget(self._table)

        self._last_entries: list = []

        # Tick every 30 s so relative labels ("2m ago") stay current without
        # needing a server push.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(30_000)
        self._tick_timer.timeout.connect(self._redraw)
        self._tick_timer.start()

    def refresh(self, entries: list) -> None:
        """Rebuild from a list of action-log dicts (newest last → display newest first)."""
        self._last_entries = list(entries)
        self._redraw()

    def _redraw(self) -> None:
        self._table.setRowCount(0)
        for entry in reversed(self._last_entries):
            time_str = _rel_time(entry.get("ts", ""))
            actor    = entry.get("actor", "")
            action   = entry.get("action", "")
            title    = entry.get("track_title") or ""
            ok       = entry.get("outcome", "ok") == "ok"

            row = self._table.rowCount()
            self._table.insertRow(row)
            for col, text in enumerate([time_str, actor, action, title]):
                item = QTableWidgetItem(text)
                if not ok:
                    item.setForeground(QColor("#ff8844"))
                self._table.setItem(row, col, item)


class NowPlayingCard(QFrame):
    save_to_playlist_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(16)

        self._title_lbl = QLabel("Nothing playing")
        self._title_lbl.setObjectName("heading")
        self._artist_lbl = QLabel("")
        self._artist_lbl.setObjectName("dim")
        self._source_lbl = QLabel("")
        self._source_lbl.setObjectName("dim")

        # Shown only when vibe-match is active
        self._vibe_lbl = QLabel("")
        self._vibe_lbl.setObjectName("dim")
        self._vibe_lbl.setVisible(False)

        info = QVBoxLayout()
        info.setSpacing(2)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._title_lbl)
        row.addWidget(self._source_lbl)
        row.addStretch()
        info.addLayout(row)
        info.addWidget(self._artist_lbl)
        info.addWidget(self._vibe_lbl)
        layout.addLayout(info, 1)

        self._save_btn = QPushButton("+ playlist")
        self._save_btn.setToolTip("Add currently playing track to a playlist")
        self._save_btn.clicked.connect(self.save_to_playlist_clicked)
        self._save_btn.setEnabled(False)
        layout.addWidget(self._save_btn)

    def update_track(self, track: Optional[Track]) -> None:
        has_track = track is not None
        self._save_btn.setEnabled(has_track)
        if not has_track:
            self._title_lbl.setText("Nothing playing")
            self._artist_lbl.setText("")
            self._source_lbl.setText("")
            return
        _disp_artist, _clean_title = _merge_credited_artists(
            track.title or "", track.artist or ""
        )
        self._title_lbl.setText(_clean_title or "Unknown title")
        self._artist_lbl.setText(_disp_artist or "")
        self._source_lbl.setText(f"[{track.source.name}]")

    def update_vibe_seed(self, seed_name: Optional[str]) -> None:
        """Show or hide the vibe-match attribution line. Text is used as-is."""
        if seed_name:
            self._vibe_lbl.setText(seed_name)
            self._vibe_lbl.setVisible(True)
        else:
            self._vibe_lbl.setVisible(False)
            self._vibe_lbl.setText("")


class SeekBar(QWidget):
    """Custom seek bar with ghost playhead during scrubbing.

    Paints a flat track (background), a filled portion (real playback
    position), a small ghost dot showing the real position *while* the
    user is dragging, and a white thumb that follows the scrub target.

    Signals
    -------
    seek_requested(float)   — 0-1 fraction, emitted on mouse release
    scrub_changed(float)    — 0-1 fraction, emitted on every mouse move
    """

    seek_requested = Signal(float)
    scrub_changed  = Signal(float)

    _TRACK_H = 4    # height of the filled bar, px
    _THUMB_R = 6    # radius of the draggable thumb, px

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._real_fraction: float = 0.0
        self._scrub_fraction: float = 0.0
        self._scrubbing: bool = False

    # ── Public API ─────────────────────────────────────────────────────────

    def set_fraction(self, frac: float) -> None:
        """Update the real playback position (called from smooth-tick)."""
        self._real_fraction = max(0.0, min(1.0, frac))
        if not self._scrubbing:
            self.update()

    @property
    def is_scrubbing(self) -> bool:
        return self._scrubbing

    # ── Mouse ──────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._scrubbing = True
            self._scrub_fraction = self._frac_from_x(int(event.position().x()))
            self.scrub_changed.emit(self._scrub_fraction)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._scrubbing:
            self._scrub_fraction = self._frac_from_x(int(event.position().x()))
            self.scrub_changed.emit(self._scrub_fraction)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._scrubbing:
            self._scrub_fraction = self._frac_from_x(int(event.position().x()))
            self._scrubbing = False
            self.seek_requested.emit(self._scrub_fraction)
            self.update()

    # ── Paint ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r   = self._THUMB_R
        w   = self.width()
        cy  = self.height() // 2
        tx0 = r
        txw = w - 2 * r               # usable track width

        # ── background track ──
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1e2e22"))
        p.drawRoundedRect(tx0, cy - self._TRACK_H // 2, txw, self._TRACK_H, 2, 2)

        # ── filled portion (real position) ──
        fill_w = max(0, int(self._real_fraction * txw))
        if fill_w:
            p.setBrush(QColor(GREEN))
            p.drawRoundedRect(tx0, cy - self._TRACK_H // 2, fill_w, self._TRACK_H, 2, 2)

        # ── ghost dot — real position shown while user is scrubbing ──
        if self._scrubbing:
            gx = tx0 + int(self._real_fraction * txw)
            p.setBrush(QColor(180, 180, 180, 160))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(gx - 4, cy - 4, 8, 8)

        # ── thumb (scrub target while dragging, else real position) ──
        frac = self._scrub_fraction if self._scrubbing else self._real_fraction
        tx   = tx0 + int(frac * txw)
        p.setBrush(QColor("#ffffff"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(tx - r, cy - r, r * 2, r * 2)

        p.end()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _frac_from_x(self, x: int) -> float:
        r   = self._THUMB_R
        txw = self.width() - 2 * r
        if txw <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - r) / txw))


class TransportBar(QFrame):
    play_pause_clicked = Signal()
    stop_clicked       = Signal()
    skip_clicked       = Signal()
    prev_clicked       = Signal()
    seek_changed       = Signal(float)   # 0-1 fraction, forwarded from SeekBar
    volume_changed     = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedHeight(56)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        self._prev_btn = QPushButton("|< prev")
        self._play_btn = QPushButton("> play")
        self._play_btn.setObjectName("accent")
        # Lock width to the wider "|| pause" label so the transport bar
        # never reflows when toggling between play and pause.
        self._play_btn.ensurePolished()
        self._play_btn.setText("|| pause")
        self._play_btn.setFixedWidth(self._play_btn.sizeHint().width())
        self._play_btn.setText("> play")
        self._stop_btn = QPushButton("■ stop")
        self._skip_btn = QPushButton("skip >|")

        self._seek = SeekBar()

        self._time_lbl = QLabel("--:-- / --:--")
        self._time_lbl.setObjectName("dim")
        self._time_lbl.setFixedWidth(130)

        vol_lbl = QLabel("vol")
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(80)

        layout.addWidget(self._prev_btn)
        layout.addWidget(self._play_btn)
        layout.addWidget(self._stop_btn)
        layout.addWidget(self._skip_btn)
        layout.addWidget(self._seek)
        layout.addWidget(self._time_lbl)
        layout.addWidget(vol_lbl)
        layout.addWidget(self._volume)

        self._prev_btn.clicked.connect(self.prev_clicked)
        self._play_btn.clicked.connect(self.play_pause_clicked)
        self._stop_btn.clicked.connect(self.stop_clicked)
        self._skip_btn.clicked.connect(self.skip_clicked)
        self._volume.valueChanged.connect(
            lambda v: self.volume_changed.emit(v / 100.0)
        )

        # Scrub signals from SeekBar
        self._seek.scrub_changed.connect(self._on_scrub_changed)
        self._seek.seek_requested.connect(lambda frac: self.seek_changed.emit(frac))

        # ── Smooth position interpolation ──────────────────────────────────────
        # The engine position is polled at 10 Hz; if the Qt thread is busy
        # (spectrogram repaints) that rate can drop to ~1 Hz, causing a visible
        # 1-second jump every poll.  We decouple visual smoothness from poll rate
        # by remembering the last anchor (elapsed, wall_time) and extrapolating
        # forward at 33 Hz.
        self._anchor_elapsed: float = 0.0
        self._anchor_wall: float = 0.0
        self._smooth_total: float = 0.0
        self._is_playing: bool = False

        self._smooth_timer = QTimer(self)
        self._smooth_timer.timeout.connect(self._smooth_tick)
        self._smooth_timer.start(30)  # 33 Hz — cheap label + seek-bar update

    def set_playing(self, playing: bool) -> None:
        self._is_playing = playing
        self._play_btn.setText("|| pause" if playing else "> play")
        if not playing:
            self._is_playing = False

    def set_held(self, held: bool) -> None:
        """Style the stop button to indicate a user-hold is active."""
        if held:
            # Dim green — visible but clearly inactive/suppressed
            self._stop_btn.setStyleSheet(
                "QPushButton { color: #33aa5a; border-color: #1a6b2e; "
                "background: transparent; }"
            )
            self._stop_btn.setEnabled(False)
        else:
            self._stop_btn.setStyleSheet("")
            self._stop_btn.setEnabled(True)

    def set_position(self, elapsed: float, total: float) -> None:
        """Called from the poll timer (10 Hz).  Anchors the interpolation."""
        self._anchor_elapsed = elapsed
        self._anchor_wall = time.perf_counter()
        self._smooth_total = total
        # Apply immediately so the anchor frame itself is never stale.
        self._apply_display(elapsed, total)

    def _smooth_tick(self) -> None:
        """33 Hz interpolation tick — extrapolates position from last anchor."""
        if not self._is_playing or self._smooth_total <= 0:
            return
        predicted = self._anchor_elapsed + (time.perf_counter() - self._anchor_wall)
        predicted = min(predicted, self._smooth_total)
        self._apply_display(predicted, self._smooth_total)

    def _apply_display(self, elapsed: float, total: float) -> None:
        """Update SeekBar fraction and time label (skips label while scrubbing)."""
        if total > 0:
            self._seek.set_fraction(elapsed / total)
        if not self._seek.is_scrubbing:
            self._time_lbl.setText(
                f"{self._fmt(elapsed)} / {self._fmt(total)}"
            )

    def _on_scrub_changed(self, frac: float) -> None:
        """While user drags, show the scrub target in the time label."""
        if self._smooth_total > 0:
            scrub_secs = frac * self._smooth_total
            self._time_lbl.setText(
                f"→ {self._fmt(scrub_secs)} / {self._fmt(self._smooth_total)}"
            )

    def init_volume(self, linear: float) -> None:
        """Set slider position from a saved linear 0–1 value without emitting."""
        self._volume.blockSignals(True)
        self._volume.setValue(round(linear * 100))
        self._volume.blockSignals(False)

    @staticmethod
    def _fmt(secs: float) -> str:
        s = int(secs)
        return f"{s // 60:02d}:{s % 60:02d}"


def show_vibe_ack_dialog(parent: QWidget) -> bool:
    """One-time consent dialog shown before vibe mode is enabled for the first time.

    Returns True if the user checked the box and clicked Enable, False if cancelled.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle("Vibe Mode")
    dlg.setModal(True)
    dlg.setMinimumWidth(420)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(14)

    msg = QLabel(
        "Vibe mode queues tracks chosen by YouTube's recommendation algorithm — "
        "you won't always know what's coming next.\n\n"
        "You're responsible for ensuring you have the right to publicly perform "
        "anything it selects, including on live streams."
    )
    msg.setWordWrap(True)
    layout.addWidget(msg)

    ack_cb = QCheckBox("I understand")
    layout.addWidget(ack_cb)

    buttons = QDialogButtonBox()
    enable_btn = buttons.addButton("Enable", QDialogButtonBox.ButtonRole.AcceptRole)
    buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
    enable_btn.setEnabled(False)
    layout.addWidget(buttons)

    ack_cb.toggled.connect(enable_btn.setEnabled)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)

    return dlg.exec() == QDialog.DialogCode.Accepted


class ModeBar(QFrame):
    """Thin bar with playback-mode toggles: Repeat · Vibe-match · Pause requests.

    Repeat cycles:  off → one → all → off
    Vibe-match:     master switch for YouTube auto-suggestion queue expansion
    Pause requests: shown only when signed into Twitch; pauses chat/CP requests
    """

    repeat_changed            = Signal(str)    # "off" | "one" | "all"
    vibe_match_toggled        = Signal(bool)
    vibe_rigidness_changed    = Signal(float)  # 0.0–1.0
    vibe_artist_guard_changed = Signal(bool)
    requests_paused_toggled   = Signal(bool)
    # Emitted when playlist-shuffle mode is ended by any means (button, stop, etc.)
    playlist_shuffle_ended    = Signal()

    _REPEAT_CYCLE  = ["off", "one", "all"]
    _REPEAT_LABELS = {
        "off": "↺  repeat: off",
        "one": "↺  repeat: one",
        "all": "↺  repeat: all",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumHeight(38)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Main controls row ────────────────────────────────────────────────
        _main = QWidget()
        layout = QHBoxLayout(_main)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(10)

        # ── Repeat ──────────────────────────────────────────────────────────
        self._repeat_state = "off"
        self._repeat_btn = QPushButton(self._REPEAT_LABELS["off"])
        self._repeat_btn.setToolTip(
            "Cycle repeat mode: off → repeat one → repeat all"
        )
        self._repeat_btn.clicked.connect(self._cycle_repeat)
        layout.addWidget(self._repeat_btn)

        # ── Vibe-match ────────────────────────────────────────────────────────
        self._vibe_btn = QPushButton("◈  vibe-match")
        self._vibe_btn.setCheckable(True)
        self._vibe_btn.setChecked(False)
        self._vibe_btn.setToolTip(
            "Vibe-match: hand queue control to YouTube suggestions\n"
            "so the music follows the current vibe automatically"
        )
        self._vibe_btn.toggled.connect(self._on_vibe_toggled)
        # Pin width to the widest label so the bar never reflows between states
        self._vibe_btn.ensurePolished()
        self._vibe_btn.setText("◈  shuffling playlist...")
        self._vibe_btn.setFixedWidth(self._vibe_btn.sizeHint().width())
        self._vibe_btn.setText("◈  vibe-match")
        layout.addWidget(self._vibe_btn)

        # ── Pause requests ────────────────────────────────────────────────────
        self._pause_btn = QPushButton("pause requests")
        self._pause_btn.setCheckable(True)
        self._pause_btn.setChecked(False)
        self._pause_btn.setToolTip(
            "Pause song requests: disables the channel-point reward and ignores\n"
            "chat commands until requests are resumed"
        )
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        self._pause_btn.setVisible(False)  # shown only when signed into Twitch
        layout.addWidget(self._pause_btn)

        self._playlist_shuffle_mode = False

        layout.addStretch()
        outer.addWidget(_main)

        # ── Vibe settings sub-row (visible only when vibe is on) ─────────────
        self._vibe_row = QWidget()
        vr = QHBoxLayout(self._vibe_row)
        vr.setContentsMargins(20, 0, 16, 6)
        vr.setSpacing(8)

        _loose = QLabel("loose")
        _loose.setObjectName("dim")
        vr.addWidget(_loose)

        self._rigidness_slider = QSlider(Qt.Orientation.Horizontal)
        self._rigidness_slider.setRange(0, 100)
        self._rigidness_slider.setValue(70)
        self._rigidness_slider.setFixedWidth(110)
        self._rigidness_slider.setToolTip(
            "Vibe rigidness\n"
            "Loose: diversifies across artists, penalises repeated bands\n"
            "Strict: follows YouTube's genre signal closely"
        )
        self._rigidness_slider.valueChanged.connect(
            lambda v: self.vibe_rigidness_changed.emit(v / 100.0)
        )
        vr.addWidget(self._rigidness_slider)

        _strict = QLabel("strict")
        _strict.setObjectName("dim")
        vr.addWidget(_strict)

        vr.addSpacing(20)

        self._artist_guard_cb = QCheckBox("artist guard")
        self._artist_guard_cb.setChecked(True)
        self._artist_guard_cb.setToolTip(
            "Artist guard: when one artist dominates the suggestion batch\n"
            "(heavy genre signal like metal), penalise them to force diversity"
        )
        self._artist_guard_cb.toggled.connect(self.vibe_artist_guard_changed)
        vr.addWidget(self._artist_guard_cb)

        vr.addStretch()
        self._vibe_row.setVisible(False)
        outer.addWidget(self._vibe_row)

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def repeat(self) -> str:
        return self._repeat_state

    @property
    def vibe_match(self) -> bool:
        return self._vibe_btn.isChecked()

    def set_twitch_connected(self, connected: bool) -> None:
        """Show or hide the pause-requests button based on Twitch sign-in state."""
        self._pause_btn.setVisible(connected)

    def set_requests_paused(self, paused: bool) -> None:
        """Silently sync button state from config (no signal emitted)."""
        self._pause_btn.blockSignals(True)
        self._pause_btn.setChecked(paused)
        self._pause_btn.setObjectName("accent" if paused else "")
        self._pause_btn.style().unpolish(self._pause_btn)
        self._pause_btn.style().polish(self._pause_btn)
        self._pause_btn.blockSignals(False)

    # ── Internal slots ─────────────────────────────────────────────────────────

    def _cycle_repeat(self) -> None:
        idx = self._REPEAT_CYCLE.index(self._repeat_state)
        self._repeat_state = self._REPEAT_CYCLE[(idx + 1) % len(self._REPEAT_CYCLE)]
        self._repeat_btn.setText(self._REPEAT_LABELS[self._repeat_state])
        # Visually distinguish active repeat via the accent style
        active = self._repeat_state != "off"
        self._repeat_btn.setObjectName("accent" if active else "")
        self._repeat_btn.style().unpolish(self._repeat_btn)
        self._repeat_btn.style().polish(self._repeat_btn)
        self.repeat_changed.emit(self._repeat_state)

    def init_vibe_controls(self, rigidness: float, artist_guard: bool) -> None:
        """Initialise slider/checkbox from saved config without emitting signals."""
        self._rigidness_slider.blockSignals(True)
        self._rigidness_slider.setValue(int(rigidness * 100))
        self._rigidness_slider.blockSignals(False)
        self._artist_guard_cb.blockSignals(True)
        self._artist_guard_cb.setChecked(artist_guard)
        self._artist_guard_cb.blockSignals(False)

    def enter_playlist_shuffle_mode(self, playlist_name: str) -> None:
        """Switch the vibe button to 'shuffling playlist…' appearance."""
        self._playlist_shuffle_mode = True
        self._vibe_btn.blockSignals(True)
        self._vibe_btn.setChecked(True)
        self._vibe_btn.blockSignals(False)
        self._vibe_btn.setObjectName("accent")
        self._vibe_btn.setText("◈  shuffling playlist...")
        self._vibe_btn.style().unpolish(self._vibe_btn)
        self._vibe_btn.style().polish(self._vibe_btn)
        self._vibe_row.setVisible(False)  # rigidity/artist-guard not relevant here

    def exit_playlist_shuffle_mode(self) -> None:
        """Restore the vibe button to normal and emit playlist_shuffle_ended."""
        if not self._playlist_shuffle_mode:
            return
        self._playlist_shuffle_mode = False
        self._vibe_btn.blockSignals(True)
        self._vibe_btn.setChecked(False)
        self._vibe_btn.blockSignals(False)
        self._vibe_btn.setObjectName("")
        self._vibe_btn.setText("◈  vibe-match")
        self._vibe_btn.style().unpolish(self._vibe_btn)
        self._vibe_btn.style().polish(self._vibe_btn)
        self._vibe_row.setVisible(False)
        self.playlist_shuffle_ended.emit()

    def _on_vibe_toggled(self, checked: bool) -> None:
        if self._playlist_shuffle_mode:
            # User clicked the button while in playlist-shuffle mode — exit it.
            # Cleanup (vibe engine, queue, label) happens via playlist_shuffle_ended.
            self.exit_playlist_shuffle_mode()
            return
        self._vibe_btn.setObjectName("accent" if checked else "")
        self._vibe_btn.style().unpolish(self._vibe_btn)
        self._vibe_btn.style().polish(self._vibe_btn)
        self._vibe_row.setVisible(checked)
        self.vibe_match_toggled.emit(checked)

    def _on_pause_toggled(self, checked: bool) -> None:
        self._pause_btn.setObjectName("accent" if checked else "")
        self._pause_btn.style().unpolish(self._pause_btn)
        self._pause_btn.style().polish(self._pause_btn)
        self.requests_paused_toggled.emit(checked)


class QueuePanel(QWidget):
    remove_requested = Signal(str)  # track id
    clear_requested  = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._remove_btn = QPushButton("Remove selected")
        self._remove_btn.setObjectName("danger")
        self._clear_btn = QPushButton("Clear all")
        self._clear_btn.setObjectName("danger")
        btn_row.addStretch()
        btn_row.addWidget(self._remove_btn)
        btn_row.addWidget(self._clear_btn)
        layout.addLayout(btn_row)

        self._remove_btn.clicked.connect(self._on_remove_clicked)
        self._clear_btn.clicked.connect(self.clear_requested)

    def _on_remove_clicked(self) -> None:
        item = self._list.currentItem()
        if item:
            track_id = item.data(Qt.ItemDataRole.UserRole)
            if track_id:
                self.remove_requested.emit(track_id)

    def refresh(self, tracks: list[Track]) -> None:
        self._list.clear()
        for i, t in enumerate(tracks, 1):
            item = QListWidgetItem(f"  {i:>3}.  {t.display_title()}")
            if t.requested_by:
                item.setToolTip(f"Requested by @{t.requested_by}")
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            self._list.addItem(item)


def _app_icon_path() -> pathlib.Path:
    """Return the path to icon.ico whether running frozen or from source."""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(sys._MEIPASS)
    else:
        base = pathlib.Path(__file__).parent.parent
    return base / "assets" / "icon.ico"


class TitleBar(QWidget):
    """Frameless-window title bar: drag to move, dbl-click to max, ─ □ ✕."""

    def __init__(self, parent: QMainWindow) -> None:
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setObjectName("TitleBar")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 4, 0)
        lay.setSpacing(0)

        self._title = QLabel("> musichat")
        self._title.setObjectName("titleBarLabel")
        lay.addWidget(self._title)
        lay.addStretch()

        for symbol, name, slot in (
            ("─", "titleBtnMin",   self._minimize),
            ("□", "titleBtnMax",   self._toggle_max),
            ("✕", "titleBtnClose", parent.close),
        ):
            btn = QPushButton(symbol)
            btn.setObjectName(name)
            btn.setFixedSize(40, 34)
            btn.clicked.connect(slot)
            lay.addWidget(btn)
            if name == "titleBtnMax":
                self._btn_max = btn

        self._drag_pos: QPoint | None = None

    # ── window control helpers ─────────────────────────────────────────────────

    def _minimize(self) -> None:
        self.window().showMinimized()

    def _toggle_max(self) -> None:
        w = self.window()
        if w.isMaximized():
            w.showNormal()
            self._btn_max.setText("□")
        else:
            w.showMaximized()
            self._btn_max.setText("❐")

    # ── drag-to-move ──────────────────────────────────────────────────────────

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.window().move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()


class MainWindow(QMainWindow):
    # Emitted (from any thread) when a spectrogram setting changes via the
    # web UI — auto-queued to the Qt main thread via the signal mechanism.
    spec_config_changed = Signal()

    # Emitted from the settings-server daemon thread when the audio output
    # device changes.  Qt auto-queues delivery to the main thread so
    # engine.set_output_device is always called where it's safe.
    _output_device_changed = Signal(object)

    # Thread-safe bridge for global hotkey callbacks (keyboard lib fires on a
    # background thread; signal delivery is auto-queued to the main thread).
    _hotkey_trigger = Signal(str)

    # Fired by the engine's finished_callback (PortAudio thread) when a track
    # ends naturally.  Connected to _maybe_autostart so the advance always
    # happens on the Qt main thread — a plain QTimer lambda from a non-Qt
    # thread is not reliably delivered.
    _track_ended = Signal()

    # Emitted from the art-colour worker thread once the gradient is extracted.
    # Carries (color_start, color_mid, color_end) as hex strings.  Delivery is
    # auto-queued to the main thread by Qt's signal mechanism.
    _art_colours_ready = Signal(str, str, str)

    # Emitted from the FastAPI asyncio thread whenever log_action() fires.
    # Qt auto-queues delivery to the main thread so the Actions panel widget
    # is always updated from the correct thread.
    _actions_log_ready = Signal(list)

    def __init__(
        self,
        cfg: AppConfig,
        queue_manager: QueueManager,
        engine: PlaybackEngine,
        fft: FFTPipeline,
        playlist_manager: PlaylistManager,
        youtube_client=None,
        soundcloud_client=None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.queue = queue_manager
        self.engine = engine
        self._fft = fft
        self._pm = playlist_manager
        self._sc = soundcloud_client
        self._search_track_cache: list = []  # Track objects matching current results
        self._current_track: Optional[Track] = None   # for "save to playlist"
        self._history = PlayHistory()
        # Colours extracted from the last track's cover art.  Cached so that
        # enabling cover_art_match mid-song can apply them instantly without a
        # re-fetch.  Cleared when playback stops entirely.
        self._cached_art_colours: Optional[tuple[str, str, str]] = None

        # ── User-stop state ────────────────────────────────────────────────────
        # True when the user clicked ■ stop. The stopped track is at position 0
        # in the queue. Cleared on play / skip / prev.
        self._user_stopped: bool = False

        # Set to True in closeEvent so that any in-flight PortAudio callbacks
        # that fire after Qt has started destroying C++ objects are ignored.
        self._closing: bool = False

        # Wired by main.py after settings server starts — opens browser to web UI.
        # If None at click time a warning dialog is shown instead.
        self.on_open_settings = None

        # Wired by main.py — called on a background thread after the wipe
        # sequence has shown the splash.  Should clear credentials + config dir
        # then invoke QApplication.quit() on the main thread.
        self.on_wipe_shutdown: Optional[Callable[[], None]] = None

        self.setWindowTitle("> musichat")
        self.setMinimumSize(900, 640)
        self.setStyleSheet(APP_QSS)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)

        _icon = QIcon(str(_app_icon_path()))
        if not _icon.isNull():
            self.setWindowIcon(_icon)

        self._build_ui()
        self._connect_signals()

        _sc_settings = QShortcut(QKeySequence("Ctrl+,"), self)
        _sc_settings.activated.connect(self._open_settings)
        _sc_quit = QShortcut(QKeySequence("Ctrl+Q"), self)
        _sc_quit.activated.connect(QApplication.quit)

        # Wire the hotkey bridge signal → main-thread dispatcher
        self._hotkey_trigger.connect(self._on_hotkey)

        # Wire the output-device bridge signal → engine (always on main thread)
        self._output_device_changed.connect(self.engine.set_output_device)

        # Position poll timer — reads engine.position on the Qt thread at 10 Hz.
        # Polling avoids a cross-thread signal queue that can backlog and replay
        # as a visible seek-bar jump when the Qt thread is briefly busy.
        self._pos_timer = QTimer(self)
        self._pos_timer.timeout.connect(self._poll_position)
        self._pos_timer.start(100)

        # Status update timer
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._tick_status)
        self._status_timer.start(2000)

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._title_bar = TitleBar(self)
        outer.addWidget(self._title_bar)

        content = QWidget()
        outer.addWidget(content, 1)

        root = QVBoxLayout(content)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)

        # Now playing
        self._now_playing = NowPlayingCard()
        root.addWidget(self._now_playing)

        # Transport
        self._transport = TransportBar()
        root.addWidget(self._transport)

        # Mode controls (Repeat · Shuffle · Vibe-match)
        self._mode_bar = ModeBar()
        root.addWidget(self._mode_bar)

        # Tabs
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)

        # ── Visualiser tab ──
        vis_container = QWidget()
        vis_layout    = QVBoxLayout(vis_container)
        vis_layout.setContentsMargins(0, 0, 0, 0)
        vis_layout.setSpacing(4)

        # Preset preview selector — only visible when > 1 preset exists
        self._preview_row = QWidget()
        preview_hl = QHBoxLayout(self._preview_row)
        preview_hl.setContentsMargins(4, 2, 4, 0)
        preview_hl.setSpacing(6)
        preview_hl.addWidget(QLabel("Preview:"))
        self._preview_combo = QComboBox()
        self._preview_combo.setFixedWidth(180)
        self._preview_combo.currentTextChanged.connect(self._on_preview_preset_changed)
        preview_hl.addWidget(self._preview_combo)
        preview_hl.addStretch()
        vis_layout.addWidget(self._preview_row)

        self._spectrogram = SpectrogramWidget(self.cfg.spectrogram)
        vis_layout.addWidget(self._spectrogram, 1)

        self._refresh_preview_combo()
        self._tabs.addTab(vis_container, "Visualiser")

        # ── Queue tab ──
        self._queue_panel = QueuePanel()
        self._tabs.addTab(self._queue_panel, "Queue")

        # ── Playlists tab ──
        self._playlist_panel = PlaylistPanel(self._pm, self.queue)
        self._tabs.addTab(self._playlist_panel, "Playlists")

        # ── Last Played tab ──
        self._history_list = QListWidget()
        self._history_list.setAlternatingRowColors(True)
        self._tabs.addTab(self._history_list, "Last Played")

        # ── Chat log tab ──
        self._chat_log = QPlainTextEdit()
        self._chat_log.setReadOnly(True)
        self._chat_log.setMaximumBlockCount(500)
        self._tabs.addTab(self._chat_log, "Chat Log")

        # ── Actions tab ──
        self._actions_panel = ActionsPanel()
        self._tabs.addTab(self._actions_panel, "Actions")

        # ── Search tab ──
        self._tabs.addTab(self._build_search_tab(), "Search")

        # Status bar
        self._statusbar = QStatusBar()
        self._statusbar.setSizeGripEnabled(False)
        self.setStatusBar(self._statusbar)

        def _gap() -> QLabel:
            lbl = QLabel()
            lbl.setFixedWidth(16)
            return lbl

        # Tunnel — composite widget with LED + label + optional copy button
        self._tunnel_widget = TunnelStatusWidget()
        self._statusbar.addPermanentWidget(self._tunnel_widget)
        self._statusbar.addPermanentWidget(_gap())

        # Bot + server — simple LED + label pairs
        self._led_bot = LedIndicator()
        self._status_bot = QLabel("bot: offline")
        self._status_bot.setObjectName("dim")
        self._statusbar.addPermanentWidget(self._led_bot)
        self._statusbar.addPermanentWidget(self._status_bot)
        self._statusbar.addPermanentWidget(_gap())

        self._led_server = LedIndicator()
        self._status_server = QLabel("server: offline")
        self._status_server.setObjectName("dim")
        self._statusbar.addPermanentWidget(self._led_server)
        self._statusbar.addPermanentWidget(self._status_server)

        settings_btn = QPushButton("> settings")
        settings_btn.clicked.connect(self._open_settings)
        self._statusbar.addWidget(settings_btn)

        self._statusbar.addWidget(QLabel(), 1)   # left spacer — pushes credit to centre
        credit_lbl = QLabel("Made by XWhiteHat.  https://xwhitehat.dev/")
        credit_lbl.setObjectName("dim")
        self._statusbar.addWidget(credit_lbl)
        self._statusbar.addWidget(QLabel(), 1)   # right spacer — equal to left

    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        hint = QLabel("Search YouTube or SoundCloud — paste a URL or type a query")
        hint.setObjectName("dim")
        layout.addWidget(hint)

        row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("URL or search query...")
        self._search_input.returnPressed.connect(self._do_search)
        self._search_btn = QPushButton("Search")
        self._search_btn.setObjectName("accent")
        self._search_btn.clicked.connect(self._do_search)
        row.addWidget(self._search_input, 1)
        row.addWidget(self._search_btn)
        layout.addLayout(row)

        self._search_results = QListWidget()
        layout.addWidget(self._search_results, 1)

        add_row = QHBoxLayout()
        self._add_queue_btn = QPushButton("Add to queue")
        self._add_queue_btn.setObjectName("accent")
        self._add_queue_btn.clicked.connect(self._add_selected_to_queue)
        self._play_now_btn = QPushButton("Play now")
        self._play_now_btn.clicked.connect(self._play_selected_now)
        self._add_playlist_btn = QPushButton("+ playlist ▾")
        self._add_playlist_btn.clicked.connect(self._show_add_to_playlist_menu_search)
        add_row.addStretch()
        add_row.addWidget(self._add_playlist_btn)
        add_row.addWidget(self._add_queue_btn)
        add_row.addWidget(self._play_now_btn)
        layout.addLayout(add_row)
        return w

    def _selected_search_track(self):
        row = self._search_results.currentRow()
        if row < 0 or row >= len(self._search_track_cache):
            return None
        return self._search_track_cache[row]

    @Slot()
    def _add_selected_to_queue(self) -> None:
        track = self._selected_search_track()
        if track:
            pos = self.queue.enqueue(track)
            self.append_chat(f"[queue] Added #{pos}: {track.display_title()}")

    @Slot()
    def _play_selected_now(self) -> None:
        track = self._selected_search_track()
        if track:
            self.engine.play_track(track)

    # ── Signal wiring ──────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        self._transport.play_pause_clicked.connect(self._toggle_play)
        self._transport.stop_clicked.connect(self._on_stop)
        self._transport.skip_clicked.connect(self._do_skip)
        self._transport.prev_clicked.connect(self._on_prev)
        self._transport.seek_changed.connect(self._on_seek_changed)
        self._transport.volume_changed.connect(
            lambda v: setattr(self.engine, "volume", v)
        )

        self.queue.on_queue_changed.append(self._on_queue_changed)
        self._queue_panel.remove_requested.connect(self.queue.remove)
        self._queue_panel.clear_requested.connect(self.queue.clear)
        # Note: queue.on_track_started is intentionally NOT wired here.
        # The engine fires on_track_started after play_track() is called,
        # which is the correct moment — after audio actually begins.
        # Wiring both would double-fire _on_track_started on every auto-advance.

        self.engine.on_state_changed = self._on_state_changed
        self.engine.on_track_started = self._on_track_started
        self.engine.on_visualiser_mode = self._spectrogram.set_visualiser_available

        # _track_ended is emitted from the PortAudio finished_callback (non-Qt
        # thread).  Qt auto-queues signal delivery to the main thread, so
        # _maybe_autostart always runs where it's safe to call play_track().
        self._track_ended.connect(self._maybe_autostart)

        # Art-colour worker emits this from a background thread; Qt queues
        # delivery to the main thread so _apply_art_colours can safely update
        # the spectrogram widget.
        self._art_colours_ready.connect(self._apply_art_colours)

        self._actions_log_ready.connect(self._actions_panel.refresh)

        # Now playing card → playlist
        self._now_playing.save_to_playlist_clicked.connect(
            self._show_add_to_playlist_menu_now_playing
        )
        # Playlist panel → direct play
        self._playlist_panel.play_track_requested.connect(self.engine.play_track)

        # History list — double-click plays, right-click context menu
        self._history_list.itemDoubleClicked.connect(self._on_history_double_clicked)
        self._history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._history_list.customContextMenuRequested.connect(
            self._on_history_context_menu
        )

    # ── Slot handlers ──────────────────────────────────────────────────────────

    @Slot()
    def _on_stop(self) -> None:
        """User clicked ■ stop — park current track at front of queue, stop audio."""
        # Kill playlist-shuffle mode before parking (cleanup removes auto-suggestion,
        # then we add the parked track so it survives as the next song to play).
        self._mode_bar.exit_playlist_shuffle_mode()

        state = self.engine.state
        if state not in (PlayState.PLAYING, PlayState.PAUSED):
            return
        current = self.engine._current_track
        if current is None:
            return
        # Re-insert at front so it's next to play.
        # Use a fresh copy so the queue ID is unique from the history entry.
        import copy
        parked = copy.copy(current)
        self.queue.enqueue(parked, position=1)
        # Set _user_stopped BEFORE engine.stop() so that _on_state_changed's
        # _track_ended emission (which can fire synchronously on the main thread)
        # sees the flag and doesn't auto-pop the parked track via _maybe_autostart.
        self._user_stopped = True
        self.engine.stop()
        self._transport.set_held(True)

    @Slot()
    def _toggle_play(self) -> None:
        if self._user_stopped:
            # Resume: pop the parked track (position 0) and play it.
            self._clear_stop_state()
            track = self.queue.pop_next()
            if track:
                self.engine.play_track(track)
            return
        if self.engine.state == PlayState.PLAYING:
            self.engine.pause()
        elif self.engine.state == PlayState.PAUSED:
            self.engine.play()
        else:
            track = self.queue.pop_next()
            if track:
                self.engine.play_track(track)

    @Slot()
    def _do_skip(self) -> None:
        if self._user_stopped:
            self._clear_stop_state()
        self.engine.skip()

    def _clear_stop_state(self) -> None:
        self._user_stopped = False
        self._transport.set_held(False)

    @Slot()
    def _on_queue_changed(self) -> None:
        if self._closing:
            return
        self._queue_panel.refresh(self.queue.snapshot())
        # If the engine is idling and the queue just got a track, auto-start it.
        # Covers: vibe suggestion arriving after a skip-to-empty, a Twitch
        # request landing after the queue runs dry, etc.
        # _on_queue_changed can be called from background threads (QueueManager
        # notifies directly), so defer the play to the main thread via a timer.
        if not self._user_stopped and self.engine.state == PlayState.STOPPED:
            QTimer.singleShot(0, self._maybe_autostart)

    @Slot()
    def _maybe_autostart(self) -> None:
        """Pop and play the next queued track if the engine is still idle.

        Called from two paths, both on the Qt main thread:
          • _track_ended signal   — emitted by _on_state_changed when a track
                                    finishes naturally (engine → STOPPED).
          • _on_queue_changed     — a new track arrived while engine is already
                                    stopped (e.g. vibe suggestion landed after
                                    the previous track ended with an empty queue).

        The guard prevents double-pops if both paths fire close together, and
        also respects user-stop (■ button) which parks the current track.
        """
        if self._closing or self._user_stopped or self.engine.state != PlayState.STOPPED:
            return
        track = self.queue.pop_next()
        if track:
            self.engine.play_track(track)

    def _on_track_started(self, track: Track) -> None:
        """Called by engine.on_track_started — always on the Qt main thread."""
        # If something starts playing, stop state is no longer relevant.
        if self._user_stopped:
            self._clear_stop_state()
        self._current_track = track
        self._now_playing.update_track(track)
        self._history.record(track)
        self._refresh_history()

        # Always fetch the cover art colours for every track that has a thumbnail,
        # regardless of whether cover_art_match is currently on.  The result is
        # cached in self._cached_art_colours so that:
        #   a) if cover_art_match IS on the colours are applied straight away, and
        #   b) if the user enables cover_art_match mid-song, _on_settings_applied
        #      can apply the cached result instantly without a new network request.
        self._cached_art_colours = None  # clear stale colours from previous track
        if getattr(track, "thumbnail_url", None):
            import threading
            threading.Thread(
                target=self._fetch_art_colours,
                args=(track.thumbnail_url,),
                daemon=True,
                name="ArtColours",
            ).start()

    def _fetch_art_colours(self, url: str) -> None:
        """Worker (background thread): extract gradient and emit signal."""
        from player.art_colours import extract_gradient
        low, mid, high = extract_gradient(url)
        self._art_colours_ready.emit(low, mid, high)

    @Slot(str, str, str)
    def _apply_art_colours(self, low: str, mid: str, high: str) -> None:
        """Main thread: cache colours and apply to every cover_art_match preset."""
        # Cache so _on_settings_applied can apply them instantly if cover_art_match
        # is enabled mid-song without spawning a new network request.
        self._cached_art_colours = (low, mid, high)

        # Apply to every preset that has cover_art_match enabled, not just the
        # active one — a non-active preset (e.g. "Test") should keep receiving
        # colour updates even when the settings dropdown is on a different preset.
        updated_active = False
        updated_presets: dict[str, dict] = {}
        for preset in self.cfg.spectrogram_presets:
            if getattr(preset, "cover_art_match", False):
                preset.color_start = low
                preset.color_mid   = mid
                preset.color_end   = high
                updated_presets[preset.name] = {
                    "color_start": low,
                    "color_mid":   mid,
                    "color_end":   high,
                }
                if preset.name == self.cfg.active_preset_name:
                    updated_active = True

        # Repaint the in-app spectrogram if the preset it is currently displaying
        # was one of the ones updated.  The widget may be showing a preview preset
        # that differs from active_preset_name (user changed the preview combo),
        # so we check the widget's own cfg.name rather than active_preset_name.
        displayed = getattr(self._spectrogram.cfg, "name", None)
        if displayed and displayed in updated_presets:
            preset = self.cfg.get_preset(displayed)
            if preset:
                self._spectrogram.apply_config(preset)
        elif updated_active:
            self._spectrogram.apply_config(self.cfg.spectrogram)

        # Push updated configs for ALL presets to connected OBS browser sources.
        # Call sync_presets directly rather than emitting spec_config_changed — emitting
        # the signal would re-enter _on_settings_applied which calls _fft.reconfigure
        # (unnecessary for a colour-only change) and would also re-trigger this very
        # method via the _art_colours_ready signal chain, creating an infinite loop.
        spec_routes.sync_presets(self.cfg)

        # Push colour changes to any open settings pages so their colour pickers
        # update live without requiring a manual page refresh.
        if updated_presets:
            from server.settings_app import broadcast_to_settings
            broadcast_to_settings({
                "type": "preset_colours_updated",
                "presets": updated_presets,
            })

        # Persist so the palette survives an app restart.
        from config import save_config
        save_config(self.cfg)

    def _on_state_changed(self, state: PlayState) -> None:
        # Belt-and-suspenders guard: if closeEvent already ran, the C++ widgets
        # have been (or are being) deleted — touching them raises RuntimeError.
        if self._closing:
            return
        # This callback fires from the PortAudio thread (via _sd_finished) as well
        # as from the Qt main thread (via stop(), play_track()).  QTimer and direct
        # Qt calls here are therefore NOT safe.  Use signals to marshal anything
        # that must run on the main thread.
        #
        # set_playing is a simple bool write — safe enough from any thread since
        # sounddevice callbacks can't race the main thread mid-paint.
        self._transport.set_playing(state == PlayState.PLAYING)
        if state == PlayState.STOPPED and not self._user_stopped:
            # _track_ended is a Signal — Qt auto-queues delivery to the main
            # thread regardless of which thread emits it.  _maybe_autostart pops
            # and plays the next queued track; _maybe_clear_now_playing clears the
            # card if nothing new has started after 500 ms.
            self._track_ended.emit()
            QTimer.singleShot(500, self._maybe_clear_now_playing)

    @Slot()
    def _poll_position(self) -> None:
        now = time.perf_counter()
        if self.engine.state == PlayState.PLAYING:
            elapsed, total = self.engine.position
            self._transport.set_position(elapsed, total)
        # Warn if the Qt event loop was blocked long enough to cause a
        # noticeable position jump (threshold: 1.5× the 100 ms interval).
        gap = now - getattr(self, "_last_poll_wall", now)
        self._last_poll_wall = now
        if gap > 0.150:
            print(f"[poll_pos] Qt blocked: gap={gap*1000:.0f}ms  "
                  f"elapsed={self.engine.position[0]:.3f}s")

    @Slot()
    def _on_prev(self) -> None:
        """First press → seek to start; subsequent presses → go back in history.

        When the engine is STOPPED (song finished naturally) we always go back
        to the previous track regardless of elapsed position — the "restart if
        >3 s" heuristic only makes sense while a track is actively playing.
        Note: seek() intentionally skips on_track_started (no card update), so
        we must never call seek() when the now-playing card may be blank.

        When user-stopped: go to the track before the parked one.  If nothing
        is in history, fall through to regular play behaviour (play the parked
        track at position 0).
        """
        if self._user_stopped:
            self._clear_stop_state()
            prev = self._history.previous()
            if prev:
                self.engine.play_track(prev)
            else:
                # No history — play the parked track (same as pressing play)
                track = self.queue.pop_next()
                if track:
                    self.engine.play_track(track)
            return

        state = self.engine.state
        elapsed, _ = self.engine.position

        if state == PlayState.STOPPED:
            # Nothing playing — go back to the most recently played track.
            # (history.previous() returns the track *before* the last, which is
            # wrong here; we want the last track itself since nothing is current.)
            recent = self._history.all_recent()
            if recent:
                self.engine.play_track(recent[0])
            return

        if state == PlayState.PLAYING and elapsed > 3.0:
            self.engine.seek(0.0)
        else:
            prev = self._history.previous()
            if prev:
                self.engine.play_track(prev)
            elif state in (PlayState.PLAYING, PlayState.PAUSED):
                self.engine.seek(0.0)

    @Slot()
    def _seek_forward(self) -> None:
        """Seek forward 5 seconds."""
        elapsed, total = self.engine.position
        if total > 0:
            self.engine.seek(min(elapsed + 5.0, total))

    @Slot()
    def _seek_back(self) -> None:
        """Seek backward 5 seconds."""
        elapsed, _ = self.engine.position
        self.engine.seek(max(elapsed - 5.0, 0.0))

    @Slot(str)
    def _on_hotkey(self, action: str) -> None:
        """Dispatch a hotkey action arriving from the HotkeyManager background thread."""
        if action == "play_pause":
            self._toggle_play()
        elif action == "next":
            self._do_skip()
        elif action == "prev":
            self._on_prev()
        elif action == "seek_forward":
            self._seek_forward()
        elif action == "seek_back":
            self._seek_back()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """
        Handle keyboard shortcuts for transport control.

        Space  → play / pause
        Right  → seek +5 s
        Left   → seek −5 s

        Keys are only intercepted when a writeable text input does NOT have
        focus, so typing in the search bar is never disrupted.
        """
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit) and not focused.isReadOnly():
            super().keyPressEvent(event)
            return

        key = event.key()
        if key == Qt.Key.Key_Space:
            self._toggle_play()
            event.accept()
            return
        if key == Qt.Key.Key_Right:
            self._seek_forward()
            event.accept()
            return
        if key == Qt.Key.Key_Left:
            self._seek_back()
            event.accept()
            return

        super().keyPressEvent(event)

    def _on_seek_changed(self, fraction: float) -> None:
        """Translate a 0-1 seek fraction from SeekBar into an absolute seek."""
        _, total = self.engine.position
        if total > 0:
            self.engine.seek(fraction * total)

    def set_vibe_seed(self, seed_name: Optional[str]) -> None:
        """Update the vibe-match attribution on the now-playing card.

        Safe to call from any thread — marshals to the Qt main thread.
        """
        QTimer.singleShot(0, lambda: self._now_playing.update_vibe_seed(seed_name))

    def _maybe_clear_now_playing(self) -> None:
        """Clear the now-playing card if the engine is still stopped.

        Handles repeat-one: if the mode bar is set to repeat one and a track
        was playing, replay it immediately instead of clearing.
        Skips clearing entirely when the user has explicitly stopped (the card
        should keep showing the parked track).
        """
        if self._user_stopped:
            return
        if self.engine.state == PlayState.STOPPED:
            if self._mode_bar.repeat == "one" and self._current_track is not None:
                self.engine.play_track(self._current_track)
                return
            self._current_track = None
            self._cached_art_colours = None  # no track playing — stale colours invalid
            self._now_playing.update_track(None)
            self._transport.set_playing(False)
            self._transport.set_position(0.0, 0.0)

    # ── History tab ────────────────────────────────────────────────────────────

    def _refresh_history(self) -> None:
        """Rebuild the Last Played list from the in-memory history."""
        self._history_list.clear()
        for t in self._history.all_recent():
            item = QListWidgetItem(t.display_title())
            item.setData(Qt.ItemDataRole.UserRole, t)
            if t.duration_seconds:
                m, s = divmod(t.duration_seconds, 60)
                item.setToolTip(f"{m}:{s:02d}  —  {t.artist or ''}")
            self._history_list.addItem(item)

    @Slot(QListWidgetItem)
    def _on_history_double_clicked(self, item: QListWidgetItem) -> None:
        track = item.data(Qt.ItemDataRole.UserRole)
        if track:
            self.engine.play_track(track)

    def _on_history_context_menu(self, pos) -> None:
        item = self._history_list.itemAt(pos)
        if item is None:
            return
        track = item.data(Qt.ItemDataRole.UserRole)
        if track is None:
            return
        menu = QMenu(self)
        menu.addAction("Play now", lambda: self.engine.play_track(track))
        menu.addAction("Add to queue", lambda: self.queue.enqueue(track))
        menu.exec(self._history_list.mapToGlobal(pos))

    @Slot()
    def _do_search(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            return

        self._search_btn.setEnabled(False)
        self._search_btn.setText("searching...")
        self._search_results.clear()
        self._search_track_cache = []

        import integrations.yt_dlp_client as ytdlp

        is_sc_url = "soundcloud.com" in query
        is_yt_url = "youtube.com" in query or "youtu.be" in query

        tracks = []
        try:
            if is_sc_url:
                t = ytdlp.resolve_url(query)
                tracks = [t] if t else []
            elif is_yt_url:
                t = ytdlp.resolve_url(query)
                tracks = [t] if t else []
            elif "soundcloud" in query.lower():
                tracks = ytdlp.search_soundcloud(query)
            else:
                # Default: YouTube search via yt-dlp (no API key needed)
                tracks = ytdlp.search_youtube(query)
        except Exception as e:
            self._search_results.addItem(f"Search error: {e}")

        self._search_track_cache = [t for t in tracks if t]
        for t in self._search_track_cache:
            label = f"  {t.title}"
            if t.duration_seconds:
                m, s = divmod(t.duration_seconds, 60)
                label += f"  [{m}:{s:02d}]"
            self._search_results.addItem(label)

        self._search_btn.setEnabled(True)
        self._search_btn.setText("search")
        if not self._search_track_cache:
            self._search_results.addItem("-- no results --")

    def append_chat(self, message: str) -> None:
        self._chat_log.appendPlainText(message)

    # ── Add to playlist menus ──────────────────────────────────────────────────

    def _build_playlist_menu(self, track: Optional[Track]) -> None:
        """Show a QMenu to pick a playlist; add track if one is chosen."""
        if track is None:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        playlists = self._pm.playlists()
        if playlists:
            for pl in playlists:
                menu.addAction(pl.name, lambda _pl=pl: self._pm.add_track(_pl.id, track))
            menu.addSeparator()
        menu.addAction("+ New playlist…", lambda: self._add_to_new_playlist(track))
        menu.exec(self.cursor().pos())

    def _add_to_new_playlist(self, track: Track) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")
        if ok and name.strip():
            pl = self._pm.create(name.strip())
            self._pm.add_track(pl.id, track)

    @Slot()
    def _show_add_to_playlist_menu_search(self) -> None:
        self._build_playlist_menu(self._selected_search_track())

    @Slot()
    def _show_add_to_playlist_menu_now_playing(self) -> None:
        self._build_playlist_menu(self._current_track)

    # ── Window lifecycle ───────────────────────────────────────────────────────

    @Slot()
    def begin_wipe_shutdown(self) -> None:
        """Wipe-and-exit sequence dispatched from the settings server thread.

        Called via QMetaObject.invokeMethod (QueuedConnection) so it always
        runs on the Qt main thread.  Stops the engine, hides this window, shows
        the RemovalSplash, then delegates the slow I/O cleanup (credential wipe,
        rmtree) to a background thread which invokes QApplication.quit() when
        done.
        """
        self._closing = True
        self.engine.stop()
        self.engine.close()
        self._spectrogram.stop()
        self.hide()

        from ui.splash import RemovalSplash
        splash = RemovalSplash()
        splash.show()
        QApplication.processEvents()

        cb = self.on_wipe_shutdown
        if cb:
            import threading as _th
            _th.Thread(target=cb, daemon=True, name="WipeCleanup").start()
        else:
            QApplication.instance().quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        """Tear down audio and timers before Qt starts destroying widgets.

        Stopping the engine here ensures sounddevice's finished_callback never
        fires after the QPushButton (and other C++ objects) have been deleted,
        which would otherwise produce a RuntimeError from the PortAudio thread.
        """
        self._closing = True
        self.engine.stop()          # drains current stream, fires STOPPED state
        self.engine.close()         # shuts DecodeGate — no new decode threads can start
        self._spectrogram.stop()
        super().closeEvent(event)

    # ── Settings ───────────────────────────────────────────────────────────────

    @Slot()
    def _open_settings(self) -> None:
        if callable(self.on_open_settings):
            self.on_open_settings()
        else:
            # Settings server not wired (shouldn't happen in normal operation)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Settings unavailable",
                "The settings server has not started yet. "
                "Try again in a moment, or restart the app.",
            )

    def _on_settings_applied(self, new_cfg: AppConfig) -> None:
        self.cfg = new_cfg
        sc = new_cfg.spectrogram
        # Push all FFT-pipeline parameters live — smoothing, bar count, freq
        # range, window function, and FFT size all reconfigure without restart.
        self._fft.reconfigure(
            bar_count=sc.bar_count,
            freq_min=sc.freq_min,
            freq_max=sc.freq_max,
            smoothing=sc.smoothing,
            window_function=sc.window_function,
            fft_size=sc.fft_size,
        )
        self._spectrogram.apply_config(sc)

        # Sync browser source broadcasters to the updated preset list
        spec_routes.sync_presets(new_cfg)

        # If cover_art_match was just enabled on any preset and we already have
        # cached colours from the current track, apply them immediately — no
        # re-fetch needed, and no feedback loop because _apply_art_colours calls
        # sync_presets directly rather than emitting spec_config_changed.
        _any_match = any(
            getattr(p, "cover_art_match", False)
            for p in new_cfg.spectrogram_presets
        )
        if _any_match and self._cached_art_colours is not None:
            self._apply_art_colours(*self._cached_art_colours)

        # Refresh the preview dropdown (preset names may have changed)
        self._refresh_preview_combo()

    # ── Visualiser preset preview ──────────────────────────────────────────────

    def _refresh_preview_combo(self) -> None:
        """Rebuild the preset preview combo; hide it when only one preset exists."""
        self._preview_combo.blockSignals(True)
        self._preview_combo.clear()
        for p in self.cfg.spectrogram_presets:
            self._preview_combo.addItem(p.name)
        # Select the active preset
        idx = self._preview_combo.findText(self.cfg.active_preset_name)
        self._preview_combo.setCurrentIndex(max(0, idx))
        self._preview_combo.blockSignals(False)
        # Only show the row when there are multiple presets to choose from
        self._preview_row.setVisible(len(self.cfg.spectrogram_presets) > 1)

    def _on_preview_preset_changed(self, preset_name: str) -> None:
        """Switch the in-app preview to the selected preset."""
        preset = self.cfg.get_preset(preset_name)
        if preset is None:
            return
        self._spectrogram.apply_config(preset)
        # Reconfigure FFT for this preset's frequency range + bar count
        self._fft.reconfigure(
            bar_count=preset.bar_count,
            freq_min=preset.freq_min,
            freq_max=preset.freq_max,
            smoothing=preset.smoothing,
            window_function=preset.window_function,
            fft_size=preset.fft_size,
        )

    # ── Status bar ticks ───────────────────────────────────────────────────────

    def _tick_status(self) -> None:
        # Updated by external components calling set_*_status()
        pass

    def set_tunnel_status(self, display: str, state: str, copy_url: str = "") -> None:
        self._tunnel_widget.set_status(display, state, copy_url)

    def refresh_actions_panel(self, entries: list) -> None:
        """Update the Actions tab from the current action-log snapshot."""
        self._actions_panel.refresh(entries)

    def set_bot_status(self, state: str, account: str = "") -> None:
        self._led_bot.set_state(state)
        if state == "green":
            self._status_bot.setText(f"bot: {account}" if account else "bot: online")
        elif state == "yellow":
            self._status_bot.setText(f"bot: connecting ({account})" if account else "bot: connecting")
        elif state == "red":
            self._status_bot.setText("bot: login failed")
        else:
            self._status_bot.setText("bot: offline")

    def set_server_status(self, state: str) -> None:
        self._led_server.set_state(state)
        labels = {
            "green":  "server: online",
            "yellow": "server: starting",
            "red":    "server: error",
            "grey":   "server: offline",
        }
        self._status_server.setText(labels.get(state, "server: offline"))
