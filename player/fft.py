"""
FFT pipeline — converts raw PCM frames into per-bar magnitudes.

The engine drops PCM blocks into `pcm_queue`; the FFT worker thread
consumes them, accumulates a full window, runs the transform, maps
frequency bins to bar indices, applies temporal smoothing, and calls
the registered `on_frame` callback with a float32 array of shape (bar_count,).
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np


def _get_window(window: str, n: int) -> np.ndarray:
    """Numpy-native window factory — covers every window the app exposes."""
    w = window.lower()
    if w in ("hann", "hanning"):
        return np.hanning(n)
    if w == "hamming":
        return np.hamming(n)
    if w == "blackman":
        return np.blackman(n)
    if w == "bartlett":
        return np.bartlett(n)
    if w in ("boxcar", "flat", "rect"):
        return np.ones(n)
    if w.startswith("kaiser"):
        try:
            beta = float(w.split("(")[1].rstrip(")"))
        except (IndexError, ValueError):
            beta = 14.0
        return np.kaiser(n, beta)
    return np.hanning(n)  # safe fallback


class FFTPipeline:
    def __init__(
        self,
        sample_rate: int = 48000,
        fft_size: int = 2048,
        bar_count: int = 64,
        freq_min: int = 20,
        freq_max: int = 20000,
        window_function: str = "hann",
        smoothing: float = 0.75,
    ) -> None:
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.bar_count = bar_count
        self.freq_min = freq_min
        self.freq_max = freq_max
        self.window_function = window_function
        self.smoothing = smoothing

        self._frame_listeners: list[Callable[[np.ndarray], None]] = []

        self._pcm_queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(maxsize=32)
        self._accum = np.zeros(fft_size, dtype=np.float32)
        self._accum_pos = 0
        self._smoothed = np.zeros(bar_count, dtype=np.float32)
        self._window = _get_window(window_function, fft_size).astype(np.float32)
        self._bin_edges = self._compute_bin_edges()

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────────

    def push(self, pcm: np.ndarray) -> None:
        """Called from the sounddevice callback thread. Non-blocking."""
        try:
            self._pcm_queue.put_nowait(pcm.copy())
        except queue.Full:
            pass  # Drop frame rather than block the audio callback

    def reconfigure(
        self,
        *,
        bar_count: Optional[int] = None,
        freq_min: Optional[int] = None,
        freq_max: Optional[int] = None,
        window_function: Optional[str] = None,
        smoothing: Optional[float] = None,
        fft_size: Optional[int] = None,
    ) -> None:
        if bar_count is not None:
            self.bar_count = bar_count
            self._smoothed = np.zeros(bar_count, dtype=np.float32)
        if freq_min is not None:
            self.freq_min = freq_min
        if freq_max is not None:
            self.freq_max = freq_max
        if smoothing is not None:
            self.smoothing = smoothing
        if window_function is not None:
            self.window_function = window_function
        if fft_size is not None:
            self.fft_size = fft_size
            self._accum = np.zeros(fft_size, dtype=np.float32)
            self._accum_pos = 0
        self._window = _get_window(self.window_function, self.fft_size).astype(np.float32)
        self._bin_edges = self._compute_bin_edges()

    def add_frame_listener(self, cb: Callable[[np.ndarray], None]) -> None:
        if cb not in self._frame_listeners:
            self._frame_listeners.append(cb)

    def remove_frame_listener(self, cb: Callable[[np.ndarray], None]) -> None:
        try:
            self._frame_listeners.remove(cb)
        except ValueError:
            pass

    def stop(self) -> None:
        self._pcm_queue.put(None)
        self._thread.join(timeout=2.0)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _compute_bin_edges(self) -> np.ndarray:
        """Map bar indices to FFT bin ranges using logarithmic spacing."""
        freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / self.sample_rate)
        log_min = np.log10(max(self.freq_min, 1))
        log_max = np.log10(min(self.freq_max, self.sample_rate / 2))
        bar_freqs = np.logspace(log_min, log_max, self.bar_count + 1)
        edges = np.searchsorted(freqs, bar_freqs)
        edges = np.clip(edges, 0, len(freqs) - 1)
        return edges

    def _worker(self) -> None:
        while True:
            pcm = self._pcm_queue.get()
            if pcm is None:
                return

            # Convert stereo → mono
            if pcm.ndim == 2:
                mono = pcm.mean(axis=1).astype(np.float32)
            else:
                mono = pcm.astype(np.float32)

            # Fill accumulator
            pos = 0
            while pos < len(mono):
                space = self.fft_size - self._accum_pos
                chunk = mono[pos : pos + space]
                self._accum[self._accum_pos : self._accum_pos + len(chunk)] = chunk
                self._accum_pos += len(chunk)
                pos += len(chunk)

                if self._accum_pos >= self.fft_size:
                    self._process_window()
                    self._accum_pos = 0

    def _process_window(self) -> None:
        windowed = self._accum * self._window
        spectrum = np.abs(np.fft.rfft(windowed)) / self.fft_size
        # dB scale, normalised to [0, 1]
        with np.errstate(divide="ignore"):
            db = 20.0 * np.log10(np.maximum(spectrum, 1e-10))
        db_min, db_max = -80.0, 0.0
        normalised = np.clip((db - db_min) / (db_max - db_min), 0.0, 1.0).astype(
            np.float32
        )

        bars = np.zeros(self.bar_count, dtype=np.float32)
        edges = self._bin_edges
        for i in range(self.bar_count):
            lo, hi = edges[i], edges[i + 1]
            if hi > lo:
                bars[i] = float(normalised[lo:hi].max())
            elif lo < len(normalised):
                bars[i] = float(normalised[lo])

        # Temporal smoothing
        self._smoothed = (
            self.smoothing * self._smoothed + (1.0 - self.smoothing) * bars
        )

        if self._frame_listeners:
            frame = self._smoothed.copy()
            for cb in self._frame_listeners:
                cb(frame)
