"""
DecodeGate — per-generation stop event management for decode threads.

The PlaybackEngine previously used a single shared threading.Event for all
decode generations.  When _stop_current() set the event and then _play_pcm()
cleared it for the next track, old decode threads that were blocked in a 0.1s
queue-put timeout could wake up, see the event was clear again, and keep
writing into the new track's PCM queue — producing two simultaneous audio
streams or rapid-fire fragments.

DecodeGate fixes this by issuing a fresh, independent Event per generation.
Old threads hold a reference to their own (permanently-set) event and exit
cleanly regardless of what happens to the engine state afterwards.
"""

from __future__ import annotations

import threading
from typing import Optional


class DecodeGate:
    """
    One-shot stop event per decode generation.

    Typical usage in PlaybackEngine
    --------------------------------
    # __init__
    self._gate = DecodeGate()

    # _stop_current()
    self._gate.stop_current()          # sets current generation's event

    # _play_pcm()
    stop_evt = self._gate.next_generation()    # fresh, unset event
    thread = Thread(target=_decode_worker, args=(track, stop_evt))

    # engine.close() / app shutdown
    self._gate.shutdown()              # sets current event, blocks new ones
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[threading.Event] = None
        self._closed = False

    # ── public API ─────────────────────────────────────────────────────────────

    def next_generation(self) -> threading.Event:
        """Return a fresh, unset stop event for an upcoming decode thread.

        The *previous* generation's event is left as-is (it was already set
        by stop_current()).  Raises RuntimeError if shutdown() has been called.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("DecodeGate is shut down; cannot start new generation")
            evt = threading.Event()
            self._current = evt
            return evt

    def stop_current(self) -> None:
        """Signal the current generation's decode thread to stop."""
        with self._lock:
            evt = self._current
        if evt is not None:
            evt.set()

    def shutdown(self) -> None:
        """Permanently stop the current generation and prevent new ones.

        Idempotent — safe to call multiple times.
        """
        with self._lock:
            self._closed = True
            evt = self._current
            self._current = None
        if evt is not None:
            evt.set()

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed
