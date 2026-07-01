"""
Playback engine.

Architecture
------------
All sources (YouTube, SoundCloud, local) go through the same PCM path:
  yt-dlp resolves webpage URLs → PyAV decodes → PCM queue → sounddevice output.
The same PCM buffer fans out to the FFT pipeline simultaneously.

URL resolution (yt-dlp) runs inside the decode thread so the Qt UI never blocks.
Queue auto-advance is scheduled back onto the Qt event loop via QTimer.singleShot.
"""

from __future__ import annotations

import queue
import threading
import time
from enum import Enum, auto
from typing import Callable, Optional

import numpy as np

# ── Debug helpers ──────────────────────────────────────────────────────────────
_t0 = time.perf_counter()

def _dbg(tag: str, msg: str = "") -> None:
    """Print a timestamped debug line with the current thread name."""
    elapsed = time.perf_counter() - _t0
    thread = threading.current_thread().name
    print(f"[{elapsed:8.3f}] [{thread:>24}] [{tag}] {msg}")

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore

from player.fft import FFTPipeline  # noqa: E402
from player.queue_manager import QueueManager, Track  # noqa: E402
from player.thread_gate import DecodeGate  # noqa: E402


class PlayState(Enum):
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()
    BUFFERING = auto()


class PlaybackEngine:
    """
    Owns the sounddevice OutputStream and the decode → PCM → FFT pipeline.
    Consumers register callbacks rather than subclassing.
    """

    BUFFER_FRAMES = 8192   # pre-buffer frames ahead of playback position
    PCM_QUEUE_SIZE = 128   # audio frames queued between decoder and sd callback
                           # 128 × 1024 = 131072 frames ≈ 2.73 s at 48 kHz —
                           # enough runway to survive a DASH segment boundary stall.

    def __init__(
        self,
        queue_manager: QueueManager,
        fft_pipeline: FFTPipeline,
        sample_rate: int = 48000,
        channels: int = 2,
        blocksize: int = 1024,
        output_device: Optional[int] = None,
    ) -> None:
        self.queue_manager = queue_manager
        self.fft = fft_pipeline
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.output_device = output_device

        self._state = PlayState.STOPPED
        self._volume = 1.0           # linear 0–1
        self._pcm_queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(
            maxsize=self.PCM_QUEUE_SIZE
        )
        self._stream: Optional[object] = None  # sd.OutputStream
        self._decode_thread: Optional[threading.Thread] = None
        self._current_track: Optional[Track] = None
        # Per-generation stop events — each decode thread receives its own
        # Event at spawn time.  _stop_current() sets the current generation's
        # event; _play_pcm() issues a fresh one for the next generation.
        # This prevents the stale-thread-writes-to-new-queue race that caused
        # two tracks to play simultaneously or produced rapid-fire fragments.
        self._gate = DecodeGate()
        self._closed = False         # set by close() to block new decode threads
        self._remainder: Optional[np.ndarray] = None
        self._yt_proc = None  # yt-dlp subprocess for web streams

        # Position tracking
        self._frames_played: int = 0
        self._track_total_frames: int = 0

        # Monotonic counter incremented every time a new stream is started.
        # Both the audio callback and the finished callback close over the value
        # at stream-creation time; if it no longer matches self._stream_generation
        # the callback knows it belongs to a superseded stream and is a no-op.
        self._stream_generation: int = 0

        # Set to True while pause() is in progress so _sd_finished knows the
        # stream stop was intentional and does not emit PlayState.STOPPED or
        # schedule a queue advance.
        self._pausing: bool = False

        # Seek support — set by seek() before restarting the decode thread.
        self._initial_seek: float = 0.0          # seconds to seek to in new stream
        self._use_cached_source: bool = False     # skip yt-dlp re-resolution
        self._last_audio_source: str = ""         # cached direct stream URL

        # Callbacks
        self.on_state_changed: Optional[Callable[[PlayState], None]] = None
        self.on_track_started: Optional[Callable[[Track], None]] = None
        self.on_visualiser_mode: Optional[Callable[[bool], None]] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def state(self) -> PlayState:
        return self._state

    @property
    def position(self) -> tuple[float, float]:
        """Current (elapsed_seconds, total_seconds). Safe to read from any thread."""
        elapsed = self._frames_played / self.sample_rate
        total = (self._track_total_frames / self.sample_rate
                 if self._track_total_frames else 0.0)
        return elapsed, total

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._volume = float(np.clip(value, 0.0, 1.0))

    def play_track(self, track: Track) -> None:
        _dbg("play_track", f"'{track.display_title()}' dur={track.duration_seconds}s")
        self._stop_current()
        self._current_track = track
        # Seed total duration from track metadata so UI shows it immediately
        self._track_total_frames = int(track.duration_seconds * self.sample_rate)
        self._frames_played = 0
        _dbg("play_track", f"_frames_played reset, _track_total_frames={self._track_total_frames} ({track.duration_seconds}s from metadata)")
        # Keep queue_manager in sync so the mod panel always reflects the
        # playing track — no-op when pop_next() already set _current to this track.
        self.queue_manager.set_current(track)
        if self.on_track_started:
            self.on_track_started(track)
        self._play_pcm(track)

    def play(self) -> None:
        if self._state == PlayState.PAUSED and self._stream is not None:
            self._stream.start()
            self._set_state(PlayState.PLAYING)

    def pause(self) -> None:
        if self._state == PlayState.PLAYING and self._stream is not None:
            # Raise _pausing BEFORE stream.stop() so _sd_finished (which fires
            # as part of the stop — possibly on the PortAudio callback thread)
            # sees the flag and does not emit STOPPED or schedule a queue advance.
            # Keep it raised until _state is PAUSED so any late-arriving async
            # callback is also caught by the _state == PAUSED guard.
            self._pausing = True
            try:
                self._stream.stop()
                self._set_state(PlayState.PAUSED)
            finally:
                self._pausing = False

    def stop(self) -> None:
        self._stop_current()
        self._set_state(PlayState.STOPPED)
        self.queue_manager.set_current(None)

    def skip(self) -> None:
        next_track = self.queue_manager.skip()
        if next_track:
            self.play_track(next_track)
        else:
            self.stop()

    def set_output_device(self, device: Optional[int]) -> None:
        """Hot-swap the output device. Restarts the stream at the current position
        if a track is playing so the change is immediate."""
        self.output_device = device
        if self._state == PlayState.PLAYING and self._current_track is not None:
            elapsed, _ = self.position
            self._initial_seek = elapsed
            self._use_cached_source = bool(self._last_audio_source)
            self._stop_current()
            self._frames_played = int(elapsed * self.sample_rate)
            self._play_pcm(self._current_track)

    def seek(self, seconds: float) -> None:
        """Seek to `seconds` within the currently playing track.

        Uses the cached direct stream URL (set during the first decode) so
        yt-dlp resolution is skipped — the restart is nearly instant.
        After _stop_current() the frames_played counter is set to the target
        position so the UI shows the correct time immediately.
        """
        if self._current_track is None:
            return
        target = max(0.0, float(seconds))
        _dbg("seek", f"to {target:.2f}s  (cached_source={bool(self._last_audio_source)})")
        self._initial_seek = target
        self._use_cached_source = bool(self._last_audio_source)
        self._stop_current()
        # Set frames_played to target so UI reflects the seek immediately
        # (the decode thread will start producing audio from that position).
        self._frames_played = int(target * self.sample_rate)
        self._play_pcm(self._current_track)

    # ── PCM path ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Permanently shut down the engine.  No new decode threads will start
        after this call.  Call engine.stop() first to drain the current stream."""
        self._closed = True
        self._gate.shutdown()

    def _play_pcm(self, track: Track) -> None:
        if sd is None:
            print("[engine] sounddevice not installed — cannot play")
            return

        # Refuse to start a new decode thread after the engine has been closed.
        # Prevents the "briefly decodes after app shutdown" bug where a pending
        # QTimer.singleShot fires _maybe_autostart after closeEvent runs.
        if self._closed:
            _dbg("_play_pcm", "engine is closed — ignoring play request")
            return

        if self.on_visualiser_mode:
            self.on_visualiser_mode(True)

        # Issue a fresh stop event for this decode generation.  Old decode
        # threads already hold a reference to their own (now-set) event from
        # a previous _stop_current() call and will exit without touching this
        # track's PCM queue.
        try:
            stop_event = self._gate.next_generation()
        except RuntimeError:
            _dbg("_play_pcm", "gate is shut down — ignoring play request")
            return

        # Capture generation so the audio callback knows if it belongs to this stream
        stream_gen = self._stream_generation
        _dbg("_play_pcm", f"starting stream gen={stream_gen}")

        # Sanitise output_device: only pass an integer index to sounddevice.
        # None → system default (sounddevice handles this).
        # Anything else (e.g. stale "" from config) → fall back to default.
        _device = self.output_device if isinstance(self.output_device, int) else None

        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            blocksize=self.blocksize,
            device=_device,
            callback=lambda out, fr, t, st: self._sd_callback(out, fr, t, st, stream_gen),
            finished_callback=lambda: self._sd_finished(stream_gen),
        )

        self._decode_thread = threading.Thread(
            target=self._decode_worker,
            args=(track, stop_event),
            daemon=True,
        )
        self._decode_thread.start()
        self._stream.start()
        self._set_state(PlayState.PLAYING)

    def _sd_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info,
        status,
        stream_gen: int,
    ) -> None:
        """sounddevice output callback — runs on a high-priority audio thread."""
        # Bail out if this callback belongs to a stream that has been superseded
        if stream_gen != self._stream_generation:
            _dbg("sd_callback STALE", f"gen={stream_gen} cur={self._stream_generation} — bailing")
            outdata[:] = 0
            raise sd.CallbackStop()

        filled = 0
        stop = False
        underrun = False
        _prev_frames = self._frames_played

        # Drain any leftover samples from the previous callback first.
        # Use in-place multiply (np.multiply with out=) to avoid creating a
        # temporary numpy array inside the audio thread, which can trigger GC.
        #
        # Snapshot self._remainder once — _stop_current() on the Qt main thread
        # can set it to None concurrently; reading the attribute multiple times
        # would race between the None-check and the len() call (TOCTOU crash).
        _rem = self._remainder
        if _rem is not None:
            n = min(len(_rem), frames)
            np.multiply(_rem[:n], self._volume, out=outdata[:n])
            self.fft.push(outdata[:n])
            filled = n
            self._remainder = _rem[n:] if n < len(_rem) else None

        while filled < frames:
            try:
                chunk = self._pcm_queue.get_nowait()
            except queue.Empty:
                # Underrun — PCM queue ran dry.  Fill remainder with silence so
                # sounddevice keeps running; the gap is logged below.
                # Do NOT advance _frames_played: silence frames are not content.
                outdata[filled:] = 0
                underrun = True
                break
            if chunk is None:
                outdata[filled:] = 0
                stop = True
                break

            n = min(len(chunk), frames - filled)
            np.multiply(chunk[:n], self._volume, out=outdata[filled : filled + n])
            self.fft.push(outdata[filled : filled + n])
            filled += n

            # Keep leftover samples for the next callback instead of discarding.
            if n < len(chunk):
                self._remainder = chunk[n:]

        # Advance position by real audio frames only (not silence filler).
        if filled > 0:
            self._frames_played += filled
            if _prev_frames == 0 and self._frames_played > 0:
                _dbg("sd_callback AUDIO",
                     f"first real audio — frames_played now {self._frames_played} "
                     f"({self._frames_played/self.sample_rate:.3f}s)")

        # Log underruns — the position at which they occur tells us whether a
        # DASH segment boundary is the culprit (repeatable timestamp each play).
        if underrun:
            fp = self._frames_played
            _dbg("sd_callback UNDERRUN",
                 f"queue empty at frames_played={fp} "
                 f"({fp/self.sample_rate:.3f}s) — silence inserted, "
                 f"queue_size={self._pcm_queue.qsize()}")

        if stop:
            _dbg("sd_callback END",
                 f"sentinel received, frames_played={self._frames_played} "
                 f"({self._frames_played/self.sample_rate:.3f}s)")
            raise sd.CallbackStop()

    def _sd_finished(self, stream_gen: int) -> None:
        if stream_gen != self._stream_generation:
            _dbg("sd_finished STALE", f"gen={stream_gen} cur={self._stream_generation} — ignoring")
            return
        # If pause() is in progress (or has already completed) this callback
        # was triggered by pause()'s stream.stop() call — not by a natural
        # track end.  Suppressing here prevents a spurious STOPPED event.
        if self._pausing or self._state == PlayState.PAUSED:
            _dbg("sd_finished PAUSED", f"gen={stream_gen} — suppressing (pause in progress)")
            return
        _dbg("sd_finished", f"gen={stream_gen} matched — setting STOPPED")
        # on_state_changed fires here.  MainWindow._on_state_changed emits
        # _track_ended (a Signal) which auto-queues _maybe_autostart to the
        # Qt main thread — the correct and reliable way to advance the queue from
        # a PortAudio callback thread (QTimer + lambda from a non-Qt thread is
        # not reliably delivered to the main event loop).
        self._set_state(PlayState.STOPPED)

    def _decode_worker(self, track: Track, stop_event: threading.Event) -> None:
        """
        Decode audio from track.stream_url into PCM frames and push into
        _pcm_queue.  Uses PyAV for format-agnostic decoding.

        ``stop_event`` is a per-generation Event issued by DecodeGate at spawn
        time.  This thread owns it exclusively — no other code can clear it,
        so checking ``stop_event.is_set()`` is race-free.

        For YouTube / SoundCloud page URLs, yt-dlp is used once to resolve the
        direct audio stream URL (--get-url).  PyAV then opens that URL over
        HTTP directly.  This avoids piping yt-dlp's stdout, which caused audible
        glitches whenever yt-dlp crossed an internal DASH segment boundary and
        the stdout pipe stalled for a few milliseconds.
        """
        try:
            import av  # type: ignore
        except ImportError:
            print("[engine] PyAV not installed — cannot decode")
            self._pcm_queue.put(None)
            return

        stream_url = track.stream_url
        _NEEDS_RESOLVE = ("youtube.com", "youtu.be", "soundcloud.com")

        # Consume seek flags atomically at thread start.
        use_cached = self._use_cached_source
        self._use_cached_source = False
        _seek_to = self._initial_seek
        self._initial_seek = 0.0

        if use_cached and self._last_audio_source:
            # Seek restart — reuse the cached direct URL, skip yt-dlp entirely.
            audio_source = self._last_audio_source
            _dbg("decode SEEK", f"reusing cached URL, seek to {_seek_to:.2f}s")
        elif any(d in stream_url for d in _NEEDS_RESOLVE):
            from player.ytdlp_util import resolve_direct_url as _resolve
            _dbg("decode YTDLP", f"resolving audio URL for {stream_url[:60]}")
            resolved = _resolve(stream_url)
            if not resolved:
                print("[engine] yt-dlp returned no URL — cannot play")
                self._pcm_queue.put(None)
                return
            _dbg("decode YTDLP", f"resolved → {resolved[:80]}")
            audio_source = resolved
            self._last_audio_source = audio_source   # cache for future seeks
        else:
            audio_source = stream_url
            self._last_audio_source = audio_source

        # Bail out if stopped while URL resolution was in progress.
        if stop_event.is_set():
            self._pcm_queue.put(None)
            return

        # ── libavformat HTTP options ───────────────────────────────────────────
        # reconnect / reconnect_streamed: if the TCP socket dies while the
        # decode thread is blocked behind a full PCM queue (i.e. during a long
        # pause), libavformat will transparently re-establish the connection on
        # the next read using an HTTP Range header to resume from the same
        # byte offset — no code change needed on our side.
        _AV_OPTIONS = {
            "reconnect": "1",
            "reconnect_streamed": "1",
            "reconnect_delay_max": "5",   # max seconds between retry attempts
        }

        # ── helpers ───────────────────────────────────────────────────────────

        def _open_container(url: str):
            _dbg("decode AV", f"av.open() → {url[:72]}")
            opts = _AV_OPTIONS if url.startswith("http") else {}
            c = av.open(url, options=opts)
            s = next(x for x in c.streams if x.type == "audio")
            _dbg("decode AV",
                 f"container opened: codec={s.codec_context.name} "
                 f"rate={s.codec_context.sample_rate}Hz "
                 f"dur={float(s.duration or 0)*float(s.time_base or 0):.1f}s")
            return c, s

        def _enqueue_frame(out_frame, first_chunk: list) -> bool:
            """Normalise PCM shape and push blocksize chunks; False → stop."""
            raw = out_frame.to_ndarray()
            if raw.ndim == 2 and raw.shape[0] == self.channels:
                pcm = np.ascontiguousarray(raw.T, dtype=np.float32)
            elif raw.ndim == 2 and raw.shape[1] == self.channels:
                pcm = raw.astype(np.float32)
            else:
                pcm = raw.reshape(-1, self.channels).astype(np.float32)
            for i in range(0, len(pcm), self.blocksize):
                chunk = pcm[i : i + self.blocksize]
                while not stop_event.is_set():
                    try:
                        self._pcm_queue.put(chunk, timeout=0.1)
                        if first_chunk[0]:
                            first_chunk[0] = False
                            _dbg("decode ENQUEUE",
                                 f"first chunk queued — frames_played={self._frames_played} "
                                 f"({self._frames_played/self.sample_rate:.3f}s)")
                        break
                    except queue.Full:
                        continue
                if stop_event.is_set():
                    return False
            return True

        def _run_decode(container, audio_stream) -> None:
            """Demux → decode → resample → enqueue loop."""
            resampler = av.AudioResampler(
                format="fltp", layout="stereo", rate=self.sample_rate,
            )
            # Refine total-duration estimate from container header.
            if audio_stream.duration and audio_stream.time_base:
                old_ttf = self._track_total_frames
                new_ttf = int(
                    float(audio_stream.duration)
                    * float(audio_stream.time_base)
                    * self.sample_rate
                )
                self._track_total_frames = new_ttf
                _dbg("decode TTF",
                     f"{old_ttf/self.sample_rate:.1f}s → {new_ttf/self.sample_rate:.1f}s")

            first_chunk = [True]
            for packet in container.demux(audio_stream):
                if stop_event.is_set():
                    return
                for frame in packet.decode():
                    for out_frame in resampler.resample(frame):
                        if not _enqueue_frame(out_frame, first_chunk):
                            return
            for out_frame in resampler.resample(None):   # flush tail
                if not _enqueue_frame(out_frame, first_chunk):
                    return

        # ── decode with expired-URL fallback ──────────────────────────────────
        # Primary attempt: open the already-resolved URL (fast, no yt-dlp).
        # If the stream dies while stopped AND libavformat's reconnect can't
        # recover (e.g. the signed URL has expired after ~6 h), we re-resolve
        # via yt-dlp and seek to the last played position before retrying once.
        try:
            container, audio_stream = _open_container(audio_source)
            # Apply initial seek if this was triggered by engine.seek().
            if _seek_to > 0.5:
                try:
                    container.seek(int(_seek_to * 1_000_000), backward=True)
                    _dbg("decode SEEK", f"sought to {_seek_to:.2f}s")
                except Exception as se:
                    _dbg("decode SEEK", f"seek failed ({se!r}), playing from start")
            try:
                _run_decode(container, audio_stream)
            except Exception as exc:
                if stop_event.is_set():
                    return   # normal stop, not an error
                # Stream died unexpectedly (likely expired URL after long pause).
                seek_secs = self._frames_played / self.sample_rate
                _dbg("decode RECONNECT",
                     f"stream error after {seek_secs:.1f}s: {exc!r} — "
                     f"re-resolving URL and seeking")
                container.close()
                container = None

                # Re-resolve the URL (only meaningful for web sources).
                new_url = audio_source
                if any(d in stream_url for d in _NEEDS_RESOLVE):
                    from player.ytdlp_util import resolve_direct_url as _resolve2
                    fresh = _resolve2(stream_url)
                    if fresh:
                        new_url = fresh
                        _dbg("decode RECONNECT", f"fresh URL → {new_url[:72]}")

                if stop_event.is_set():
                    return

                container, audio_stream = _open_container(new_url)
                # Seek to where we left off so the listener doesn't hear
                # the track start over from the beginning.
                if seek_secs > 1.0:
                    try:
                        # AV_TIME_BASE = 1 000 000 µs
                        container.seek(int(seek_secs * 1_000_000), backward=True)
                        _dbg("decode RECONNECT", f"seeked to {seek_secs:.1f}s")
                    except Exception as seek_exc:
                        _dbg("decode RECONNECT", f"seek failed ({seek_exc!r}), playing from start")
                _run_decode(container, audio_stream)

        except Exception as exc:
            if stop_event.is_set():
                return
            err_str = str(exc)
            # HTTP 4xx (most commonly 403 Forbidden on expired / rate-limited
            # CDN URLs) from the initial _open_container call.  Wait briefly and
            # re-resolve via yt-dlp so we get a fresh signed URL, then retry
            # once.  The mid-stream reconnect path (inner except above) handles
            # the same condition when it happens during an active _run_decode.
            is_http_error = any(
                token in err_str
                for token in ("403", "Forbidden", "429", "Too Many", "401", "Unauthorized")
            )
            if is_http_error and any(d in stream_url for d in _NEEDS_RESOLVE):
                import time as _time
                _dbg("decode HTTP-ERR",
                     f"{err_str[:80]} — waiting 1.5 s then re-resolving URL")
                _time.sleep(1.5)
                if stop_event.is_set():
                    return
                from player.ytdlp_util import resolve_direct_url as _resolve3
                _fresh2 = _resolve3(stream_url)
                if _fresh2 and not stop_event.is_set():
                    try:
                        _c2, _s2 = _open_container(_fresh2)
                        _dbg("decode HTTP-ERR", "retry succeeded — decoding fresh URL")
                        _run_decode(_c2, _s2)
                        return   # success; finally will still put(None)
                    except Exception as _retry_exc:
                        print(f"[engine] decode error (after retry): {_retry_exc}")
                        return
            print(f"[engine] decode error: {exc}")
        finally:
            self._pcm_queue.put(None)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _stop_current(self) -> None:
        _dbg("_stop_current", f"stream_gen {self._stream_generation} → {self._stream_generation + 1}")
        # Invalidate any in-flight callbacks that closed over the old generation
        self._stream_generation += 1
        # Signal the current decode thread via its own per-generation event.
        # The old thread holds a direct reference to this event; _play_pcm()
        # will issue a fresh event for the next generation via the gate.
        self._gate.stop_current()
        self._remainder = None
        if self._yt_proc is not None:
            # yt-dlp is used only for URL resolution now (not as an ongoing pipe),
            # so it should already have exited.  Kill it just in case the user
            # stopped playback while resolution was still in flight.
            try:
                self._yt_proc.terminate()
            except Exception:
                pass
            self._yt_proc = None
        # Drain queue so callback sees sentinel quickly
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._decode_thread is not None:
            # Reap the old decode thread in the background so we never block the
            # Qt main thread (a synchronous join can stall the event loop for up
            # to the full timeout while the thread drains its last yt-dlp read).
            old_thread = self._decode_thread
            self._decode_thread = None
            def _reap(t: threading.Thread) -> None:
                t.join(timeout=5.0)
                _dbg("_reap", f"old decode thread joined (alive={t.is_alive()})")
            threading.Thread(target=_reap, args=(old_thread,), daemon=True, name="DecodeReaper").start()
        # Fresh queue so any still-running stale decode thread can't contaminate
        # the next track.  The old thread will eventually put its sentinel into
        # the abandoned queue and exit gracefully.
        self._pcm_queue = queue.Queue(maxsize=self.PCM_QUEUE_SIZE)

    def _set_state(self, state: PlayState) -> None:
        if self._state != state:
            self._state = state
            if self.on_state_changed:
                self.on_state_changed(state)
