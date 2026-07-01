"""
Spectrogram widget — renders real-time FFT data with full user control.

Physics model
─────────────
  Bars are governed by a gravity simulation on an independent QTimer.
  push_frame() sets target heights from FFT data.
  Each tick: if target >= bar, snap bar up and reset downward velocity.
             if target < bar (or no recent FFT), apply gravity.
  _frames_since_push tracks recency — targets effectively become 0 after
  a short silence window, so bars fall naturally when music pauses/stops.

Camber / arc rendering
──────────────────────
  camber_degrees = 0      → flat bars (normal upright chart)
  0 < camber_degrees < 360 → bars arranged along an arc; arc center is
                             below the widget so bars curve upward like a rainbow
  camber_degrees ≈ 360   → full radial circle; bars extend outward from
                             a fixed inner radius
  camber_asymmetric      → ouroboros: first and last bars blend smoothly
                             (only meaningful at 360°)
  double_sided            → bars extend both directions from the arc baseline;
                             overall bar scale is halved so they fit
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from config import SpectrogramConfig

_GRAVITY   = 0.008   # downward accel per frame at 60 fps
_SILENCE_FRAMES = 4  # frames of no FFT before targets treated as 0


class SpectrogramWidget(QWidget):
    """Self-contained spectrogram renderer.  Feed FFT data via `push_frame(bars)`."""

    config_changed = Signal(object)

    def __init__(self, cfg: SpectrogramConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._init_arrays(cfg.bar_count)

        self._waterfall: deque[np.ndarray] = deque(maxlen=200)
        self._visualiser_available = True
        self._frames_since_push: int = _SILENCE_FRAMES + 1  # start quiet
        self._alive = True  # set False in stop() so _tick is a no-op after teardown
        self._bg_skip: int = 0  # repaint throttle counter when app is not active

        interval = max(8, 1000 // max(cfg.fps_target, 1))
        self._gravity = _GRAVITY * (16.0 / interval)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(interval)

        self.setMinimumSize(200, 80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, cfg.background_alpha == 255)

    def _init_arrays(self, n: int) -> None:
        self._bars      = np.zeros(n, dtype=np.float64)
        self._velocities = np.zeros(n, dtype=np.float64)
        self._peaks     = np.zeros(n, dtype=np.float64)
        self._peak_hold_countdown = np.zeros(n, dtype=np.int32)
        self._targets   = np.zeros(n, dtype=np.float64)

    # ── Public API ─────────────────────────────────────────────────────────────

    def push_frame(self, bars: np.ndarray) -> None:
        """Called from FFT worker thread — only writes to _targets, safe.

        Uses a local reference to _targets so that if the main thread calls
        apply_config() and replaces _targets mid-frame we write into the old
        (still-valid) array rather than crashing with a shape mismatch.
        """
        tgt = self._targets          # one atomic load under the GIL
        n   = len(tgt)               # derive size from the array, not from cfg
        if len(bars) != n:
            bars = np.interp(np.linspace(0, len(bars) - 1, n), np.arange(len(bars)), bars)
        tgt[:] = np.clip(bars, 0.0, 1.0)
        self._frames_since_push = 0
        self._waterfall.append(tgt.astype(np.float32).copy())

    def stop(self) -> None:
        """Stop the render loop.  Call before the owning window closes."""
        self._alive = False
        self._timer.stop()

    def set_visualiser_available(self, available: bool) -> None:
        self._visualiser_available = available

    def apply_config(self, cfg: SpectrogramConfig) -> None:
        from dataclasses import replace as _replace
        cfg = _replace(cfg)   # snapshot — prevents external mutation (e.g. settings
                              # dialog's _save_form_to_preset) from changing bar_count
                              # on self.cfg without calling _init_arrays(), which would
                              # cause IndexError when the arrays are still the old size.
        if cfg.bar_count != self.cfg.bar_count:
            # Replace arrays BEFORE updating cfg so push_frame's local ref
            # strategy (see push_frame docstring) keeps things consistent.
            self._init_arrays(cfg.bar_count)
            self._waterfall.clear()   # old rows are the wrong width — discard
        self.cfg = cfg
        interval = max(8, 1000 // max(cfg.fps_target, 1))
        self._gravity = _GRAVITY * (16.0 / interval)
        self._timer.setInterval(interval)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, cfg.background_alpha == 255)

    # ── Physics ────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            self._tick_body()
        except RuntimeError:
            # Qt C++ object deleted while a final timer event was still queued.
            self._alive = False

    def _tick_body(self) -> None:
        self._frames_since_push += 1
        # After silence window, treat targets as zero so bars fall freely
        targets = self._targets if self._frames_since_push <= _SILENCE_FRAMES else np.zeros_like(self._targets)

        # Snap up where target exceeds current bar; reset downward velocity
        snap = targets >= self._bars
        self._bars       = np.where(snap, targets, self._bars)
        self._velocities = np.where(snap, 0.0, self._velocities)

        # Gravity on bars above their target
        fall = ~snap
        self._velocities = np.where(fall, self._velocities - self._gravity, self._velocities)
        self._bars       = np.where(fall, self._bars + self._velocities, self._bars)

        # Floor clamp
        at_floor = self._bars <= 0.0
        self._bars       = np.clip(self._bars, 0.0, 1.0)
        self._velocities = np.where(at_floor, 0.0, self._velocities)

        # Peak hold
        if self.cfg.peak_hold:
            new_peak = self._bars >= self._peaks
            self._peaks = np.where(new_peak, self._bars, self._peaks)
            self._peak_hold_countdown = np.where(
                new_peak, self.cfg.peak_hold_frames,
                np.maximum(0, self._peak_hold_countdown - 1),
            ).astype(np.int32)
            self._peaks = np.where(
                self._peak_hold_countdown == 0,
                np.maximum(0.0, self._peaks - self.cfg.peak_decay_rate),
                self._peaks,
            )

        app = QApplication.instance()
        if app is not None and app.applicationState() != Qt.ApplicationState.ApplicationActive:
            # App is in the background (game has focus): throttle to ~15 fps by
            # only repainting every 4th physics tick instead of skipping entirely.
            self._bg_skip = (self._bg_skip + 1) % 4
            if self._bg_skip != 0:
                return
        self.update()

    # ── Painting ───────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()

        bg = QColor(self.cfg.background_color)
        bg.setAlpha(self.cfg.background_alpha)
        painter.fillRect(0, 0, w, h, bg)

        if not self._visualiser_available:
            painter.setPen(QColor("#4d8a5f"))
            painter.setFont(QFont("Share Tech Mono", 11))
            painter.drawText(QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "▶ visualiser paused")
            painter.end()
            return

        mode   = self.cfg.vis_mode
        camber = self.cfg.camber_degrees

        if mode == "waterfall":
            self._draw_waterfall(painter, w, h)
        elif mode == "line":
            if camber < 1.0:
                self._draw_line(painter, w, h)
            elif camber >= 359.5:
                self._draw_line_arc_circle(painter, w, h)
            else:
                self._draw_line_arc_partial(painter, w, h, camber)
        else:  # bar
            if camber < 1.0:
                self._draw_bars_flat(painter, w, h)
            else:
                self._draw_bars_arc(painter, w, h, camber)

        painter.end()

    # ── Flat bar / line / waterfall rendering ──────────────────────────────────

    def _draw_bars_flat(self, painter: QPainter, w: int, h: int) -> None:
        n = self.cfg.bar_count
        gap = self.cfg.bar_gap
        bar_w = max(1.0, (w - gap * (n - 1)) / n)
        double   = self.cfg.double_sided
        inverted = getattr(self.cfg, "inverted", False) and not double
        if inverted:
            # Flip the coordinate system so bars hang from the top.
            # All drawing logic below is unchanged — the transform handles it.
            painter.save()
            painter.translate(0.0, float(h))
            painter.scale(1.0, -1.0)
        centre_y = h / 2 if double else float(h)
        max_bar_h = (h / 2 - 2) if double else (h - 2)

        for i in range(n):
            x = i * (bar_w + gap)
            bar_h = max(float(self.cfg.bar_min_height), float(self._bars[i]) * max_bar_h)
            grad = self._v_gradient(x, x + bar_w, centre_y)
            painter.fillRect(QRectF(x, centre_y - bar_h, bar_w, bar_h), grad)
            if double:
                painter.fillRect(QRectF(x, centre_y, bar_w, bar_h), grad)
            if self.cfg.peak_hold and self._peaks[i] > 0.01:
                ph = max(float(self.cfg.bar_min_height), float(self._peaks[i]) * max_bar_h)
                pk_col = QColor(self.cfg.color_end)
                pk_col.setAlpha(200)
                painter.fillRect(QRectF(x, centre_y - ph - 2, bar_w, 2), pk_col)
                if double:
                    painter.fillRect(QRectF(x, centre_y + ph, bar_w, 2), pk_col)

        if inverted:
            painter.restore()

    def _draw_line(self, painter: QPainter, w: int, h: int) -> None:
        n = self.cfg.bar_count
        if n < 2:
            return
        double   = self.cfg.double_sided
        inverted = getattr(self.cfg, "inverted", False) and not double
        if inverted:
            painter.save()
            painter.translate(0.0, float(h))
            painter.scale(1.0, -1.0)
        centre_y = h / 2 if double else float(h)
        scale = (h / 2 - 4) if double else (h - 4)
        pen = QPen(QColor(self.cfg.color_end))
        pen.setWidth(2)
        painter.setPen(pen)

        def _path(sign: float) -> QPainterPath:
            p = QPainterPath()
            for i, mag in enumerate(self._bars):
                x = (i / (n - 1)) * w
                y = centre_y - sign * float(mag) * scale
                p.moveTo(x, y) if i == 0 else p.lineTo(x, y)
            return p

        painter.drawPath(_path(1.0))
        if double:
            painter.drawPath(_path(-1.0))
        if inverted:
            painter.restore()

    def _draw_line_arc_partial(self, painter: QPainter, w: int, h: int, arc_deg: float) -> None:
        """Line mode with partial-arc camber — waveform bent along a rainbow arc."""
        n = len(self._bars)
        if n < 2:
            return

        # Geometry mirrors _draw_arc_partial
        half_rad = math.radians(arc_deg / 2.0)
        sin_h    = math.sin(half_rad)
        R        = min((w * 0.48) / sin_h if sin_h > 1e-6 else 1e9, 5000.0)
        cos_h    = math.cos(half_rad)
        cx       = w / 2.0
        cy       = h + R * cos_h
        max_bar_h = max(10.0, (cy - R) - 4)
        double   = self.cfg.double_sided
        if double:
            max_bar_h /= 2.0

        gap_deg   = arc_deg / n
        start_deg = -arc_deg / 2.0

        pen = QPen(QColor(self.cfg.color_end))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        def _arc_path(outward: bool) -> QPainterPath:
            path = QPainterPath()
            for i in range(n):
                theta_rad = math.radians(start_deg + (i + 0.5) * gap_deg)
                bar_h     = max(float(self.cfg.bar_min_height),
                                float(self._bars[i]) * max_bar_h)
                tip_r     = R + bar_h if outward else R - bar_h
                x = cx + tip_r * math.sin(theta_rad)
                y = cy - tip_r * math.cos(theta_rad)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            return path

        painter.drawPath(_arc_path(True))
        if double:
            painter.drawPath(_arc_path(False))

    def _draw_line_arc_circle(self, painter: QPainter, w: int, h: int) -> None:
        """Line mode with full 360° camber — waveform radiating around a circle."""
        n = len(self._bars)
        if n < 2:
            return

        margin  = 6
        R_outer = min(w, h) / 2.0 - margin
        double  = self.cfg.double_sided
        if double:
            R_inner = R_outer * 0.35
            R_base  = (R_inner + R_outer) / 2.0
            max_bar_h = (R_outer - R_inner) / 2.0
        else:
            R_inner = R_outer * 0.40
            R_base  = R_inner
            max_bar_h = R_outer - R_inner

        cx, cy      = w / 2.0, h / 2.0
        deg_per_bar = 360.0 / n

        pen = QPen(QColor(self.cfg.color_end))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        def _circle_path(outward: bool) -> QPainterPath:
            path = QPainterPath()
            for i in range(n):
                theta_rad = math.radians(i * deg_per_bar - 90.0)
                bar_h     = max(float(self.cfg.bar_min_height),
                                float(self._bars[i]) * max_bar_h)
                tip_r     = R_base + bar_h if outward else R_base - bar_h
                x = cx + tip_r * math.sin(theta_rad)
                y = cy - tip_r * math.cos(theta_rad)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            path.closeSubpath()
            return path

        painter.drawPath(_circle_path(True))
        if double:
            painter.drawPath(_circle_path(False))

    def _draw_waterfall(self, painter: QPainter, w: int, h: int) -> None:
        rows = list(self._waterfall)
        if not rows:
            return
        n_rows = len(rows)
        n_bars = self.cfg.bar_count

        # Build RGBA numpy image (height=n_rows, width=n_bars)
        img_arr = np.zeros((n_rows, n_bars, 4), dtype=np.uint8)
        c0 = QColor(self.cfg.color_start)
        c1 = QColor(self.cfg.color_mid)
        c2 = QColor(self.cfg.color_end)
        r0, g0, b0 = c0.red(), c0.green(), c0.blue()
        r1, g1, b1 = c1.red(), c1.green(), c1.blue()
        r2, g2, b2 = c2.red(), c2.green(), c2.blue()

        for row_i, row in enumerate(reversed(rows)):
            t = np.clip(row, 0.0, 1.0)
            lo = t < 0.5
            s = np.where(lo, t * 2.0, (t - 0.5) * 2.0)
            img_arr[row_i, :, 0] = np.where(lo, r0 + s * (r1 - r0), r1 + s * (r2 - r1)).astype(np.uint8)
            img_arr[row_i, :, 1] = np.where(lo, g0 + s * (g1 - g0), g1 + s * (g2 - g1)).astype(np.uint8)
            img_arr[row_i, :, 2] = np.where(lo, b0 + s * (b1 - b0), b1 + s * (b2 - b1)).astype(np.uint8)
            img_arr[row_i, :, 3] = self.cfg.background_alpha

        # RGBA → QImage (Format_RGBA8888) then scale to widget size
        raw = img_arr.tobytes()
        qimg = QImage(raw, n_bars, n_rows, n_bars * 4, QImage.Format.Format_RGBA8888)
        painter.drawImage(QRectF(0, 0, w, h), qimg, QRectF(0, 0, n_bars, n_rows))

    # ── Arc / camber rendering ─────────────────────────────────────────────────

    def _draw_bars_arc(self, painter: QPainter, w: int, h: int, arc_deg: float) -> None:
        n = self.cfg.bar_count
        double = self.cfg.double_sided
        full_circle = arc_deg >= 359.5

        bars  = self._bars.copy()
        peaks = self._peaks.copy()

        # Ouroboros: blend first/last bars AND peaks for seamless circle join
        if full_circle and self.cfg.camber_asymmetric and n >= 4:
            blend = max(1, n // 16)
            avg_b = (bars[0] + bars[-1]) / 2.0
            avg_p = (peaks[0] + peaks[-1]) / 2.0
            for k in range(blend):
                t = k / blend
                bars[k]       = avg_b + t * (bars[k]       - avg_b)
                bars[-(k+1)]  = avg_b + t * (bars[-(k+1)]  - avg_b)
                peaks[k]      = avg_p + t * (peaks[k]      - avg_p)
                peaks[-(k+1)] = avg_p + t * (peaks[-(k+1)] - avg_p)

        if full_circle:
            self._draw_arc_circle(painter, w, h, bars, peaks, double)
        else:
            self._draw_arc_partial(painter, w, h, bars, peaks, double, arc_deg)

    def _draw_arc_partial(self, painter, w, h, bars, peaks, double, arc_deg):
        """Rainbow arc — center below widget, bars point radially (outward or inward)."""
        inverted = getattr(self.cfg, "inverted", False) and not double
        n = len(bars)
        half_rad = math.radians(arc_deg / 2.0)
        sin_h = math.sin(half_rad)
        R = (w * 0.48) / sin_h if sin_h > 1e-6 else 1e9
        R = min(R, 5000.0)

        cos_h = math.cos(half_rad)
        cx = w / 2.0
        cy = h + R * cos_h

        arc_top_y = cy - R
        max_bar_h = max(10.0, arc_top_y - 4)
        if double:
            max_bar_h /= 2.0

        gap_deg = arc_deg / n
        start_deg = -arc_deg / 2.0
        bar_w_deg = max(0.1, gap_deg - 0.5)
        bar_w_px  = max(1.0, R * math.radians(bar_w_deg))

        pk_col = QColor(self.cfg.color_end)
        pk_col.setAlpha(200)

        # Gradient: outward = color_start at base, color_end at tip (further from center).
        # Inverted: same profile but bars grow inward, so gradient runs the other way.
        grad = (self._radial_gradient(R, R - max_bar_h)
                if inverted else
                self._radial_gradient(R, R + max_bar_h))
        bar_w_deg_rad = math.radians(bar_w_deg)

        for i in range(n):
            theta_deg = start_deg + (i + 0.5) * gap_deg
            bar_h = max(float(self.cfg.bar_min_height), float(bars[i]) * max_bar_h)

            painter.save()
            painter.translate(cx, cy)
            painter.rotate(theta_deg)
            if inverted:
                # Inward: baseline at -R, tip toward center at (-R + bar_h)
                painter.fillRect(QRectF(-bar_w_px / 2, -R, bar_w_px, bar_h), grad)
            else:
                # Outward: baseline at -R, tip away from center at -(R + bar_h)
                painter.fillRect(QRectF(-bar_w_px / 2, -(R + bar_h), bar_w_px, bar_h), grad)
                if double:
                    painter.fillRect(QRectF(-bar_w_px / 2, -R, bar_w_px, bar_h), grad)
            if self.cfg.peak_hold and peaks[i] > 0.01:
                ph = max(float(self.cfg.bar_min_height), float(peaks[i]) * max_bar_h)
                if inverted:
                    pk_w_in = max(1.0, (R - ph) * bar_w_deg_rad)
                    painter.fillRect(QRectF(-pk_w_in / 2, -(R - ph - 2), pk_w_in, 2), pk_col)
                else:
                    pk_w = max(1.0, (R + ph) * bar_w_deg_rad)
                    painter.fillRect(QRectF(-pk_w / 2, -(R + ph + 2), pk_w, 2), pk_col)
                    if double:
                        pk_w_in = max(1.0, (R - ph) * bar_w_deg_rad)
                        painter.fillRect(QRectF(-pk_w_in / 2, -(R - ph), pk_w_in, 2), pk_col)
            painter.restore()

    def _draw_arc_circle(self, painter, w, h, bars, peaks, double):
        """Full 360° radial circle — bars point outward, inward (inverted), or both (double)."""
        inverted = getattr(self.cfg, "inverted", False) and not double
        n = len(bars)
        margin = 6
        R_outer = min(w, h) / 2.0 - margin
        if double:
            R_inner   = R_outer * 0.35
            R_base    = (R_inner + R_outer) / 2.0
            max_bar_h = (R_outer - R_inner) / 2.0
        elif inverted:
            # Start at the outer edge and grow inward
            R_base    = R_outer
            max_bar_h = R_outer * 0.55
        else:
            R_inner   = R_outer * 0.40
            R_base    = R_inner
            max_bar_h = R_outer - R_inner

        cx, cy = w / 2.0, h / 2.0
        deg_per_bar = 360.0 / n
        bar_w_deg = max(0.1, deg_per_bar * 0.85)
        bar_w_px  = max(1.0, R_base * math.radians(bar_w_deg))

        pk_col = QColor(self.cfg.color_end)
        pk_col.setAlpha(200)

        # Inverted: gradient from outer baseline inward toward center.
        grad = (self._radial_gradient(R_base, R_base - max_bar_h)
                if inverted else
                self._radial_gradient(R_base, R_base + max_bar_h))
        bar_w_deg_rad = math.radians(deg_per_bar * 0.85)

        for i in range(n):
            theta_deg = i * deg_per_bar - 90.0
            bar_h = max(float(self.cfg.bar_min_height), float(bars[i]) * max_bar_h)

            painter.save()
            painter.translate(cx, cy)
            painter.rotate(theta_deg)
            if inverted:
                # Inward: baseline at -R_base (outer edge), tip toward center
                painter.fillRect(QRectF(-bar_w_px / 2, -R_base, bar_w_px, bar_h), grad)
            else:
                # Outward: baseline at -R_base (inner edge), tip away from center
                painter.fillRect(QRectF(-bar_w_px / 2, -(R_base + bar_h), bar_w_px, bar_h), grad)
                if double:
                    painter.fillRect(QRectF(-bar_w_px / 2, -R_base, bar_w_px, bar_h), grad)
            if self.cfg.peak_hold and peaks[i] > 0.01:
                ph = max(float(self.cfg.bar_min_height), float(peaks[i]) * max_bar_h)
                if inverted:
                    pk_w_in = max(1.0, (R_base - ph) * bar_w_deg_rad)
                    painter.fillRect(QRectF(-pk_w_in / 2, -(R_base - ph - 2), pk_w_in, 2), pk_col)
                else:
                    pk_w = max(1.0, (R_base + ph) * bar_w_deg_rad)
                    painter.fillRect(QRectF(-pk_w / 2, -(R_base + ph + 2), pk_w, 2), pk_col)
                    if double:
                        pk_w_in = max(1.0, (R_base - ph) * bar_w_deg_rad)
                        painter.fillRect(QRectF(-pk_w_in / 2, -(R_base - ph), pk_w_in, 2), pk_col)
            painter.restore()

    # ── Gradient helpers ───────────────────────────────────────────────────────

    def _v_gradient(self, x1: float, x2: float, base_y: float) -> QLinearGradient:
        """Vertical gradient from base_y (low intensity) to 0 (high intensity)."""
        g = QLinearGradient(x1, base_y, x1, 0)
        g.setColorAt(0.0, QColor(self.cfg.color_start))
        g.setColorAt(0.5, QColor(self.cfg.color_mid))
        g.setColorAt(1.0, QColor(self.cfg.color_end))
        return g

    def _radial_gradient(self, r_base: float, r_tip: float) -> QLinearGradient:
        """Gradient from bar baseline (-r_base) to max tip (-r_tip) in local coords."""
        g = QLinearGradient(0, -r_base, 0, -r_tip)
        g.setColorAt(0.0, QColor(self.cfg.color_start))
        g.setColorAt(0.5, QColor(self.cfg.color_mid))
        g.setColorAt(1.0, QColor(self.cfg.color_end))
        return g

    def _gradient_color(self, t: float) -> QColor:
        t = max(0.0, min(1.0, t))
        c0, c1, c2 = QColor(self.cfg.color_start), QColor(self.cfg.color_mid), QColor(self.cfg.color_end)
        if t < 0.5:
            s = t * 2.0
            return QColor(int(c0.red() + s*(c1.red()-c0.red())),
                          int(c0.green() + s*(c1.green()-c0.green())),
                          int(c0.blue() + s*(c1.blue()-c0.blue())))
        s = (t - 0.5) * 2.0
        return QColor(int(c1.red() + s*(c2.red()-c1.red())),
                      int(c1.green() + s*(c2.green()-c1.green())),
                      int(c1.blue() + s*(c2.blue()-c1.blue())))
