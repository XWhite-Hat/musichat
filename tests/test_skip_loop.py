"""
Skip must consume exactly one track.

Spamming !skip drained the whole queue and looked like the player was stuck
skipping by itself.  The cause is a feedback loop rather than the raw command
rate:

    engine.skip()
      -> queue.pop_next()          # engine is still STOPPED at this point
           -> _notify_changed()    # fires on_queue_changed SYNCHRONOUSLY
                -> _on_queue_changed sees "engine STOPPED + queue changed"
                     -> _maybe_autostart -> pop_next()   # pops ANOTHER track
                          -> _notify_changed() -> ...

`engine.skip()` pops and *then* plays, so between those two steps the engine
looks idle to anything watching queue changes.  Each pop announces itself and
invites another pop.

These tests model that listener directly rather than driving Qt, because the
loop lives in the QueueManager notification path — Qt only decides how quickly
the re-entry happens.
"""
from __future__ import annotations

from player.queue_manager import QueueManager


class _FakeEngine:
    """Stands in for PlaybackEngine: idle until a track is actually playing."""

    def __init__(self, queue: QueueManager) -> None:
        self.queue = queue
        self.playing = False
        self.started: list[str] = []
        self._advancing = False

    @property
    def stopped(self) -> bool:
        return not self.playing

    @property
    def is_advancing(self) -> bool:
        return self._advancing

    def play_track(self, track) -> None:
        self.playing = True
        self.started.append(track.id)

    def skip(self) -> None:
        """Mirrors PlaybackEngine.skip(): pop, then play, guarded throughout."""
        self._advancing = True
        try:
            self.playing = False      # the stream stops before the next starts
            nxt = self.queue.skip()
            if nxt:
                self.play_track(nxt)
        finally:
            self._advancing = False


def _wire_autostart(queue: QueueManager, engine: _FakeEngine) -> None:
    """The _on_queue_changed -> _maybe_autostart path from MainWindow."""
    def on_changed() -> None:
        # Mirrors MainWindow._on_queue_changed / _maybe_autostart, including the
        # is_advancing guard those now carry.
        if engine.is_advancing or not engine.stopped:
            return
        track = queue.pop_next()
        if track:
            engine.play_track(track)
    queue.on_queue_changed.append(on_changed)


def _fill(queue: QueueManager, track_factory, n: int) -> None:
    for i in range(n):
        queue.enqueue(track_factory(i, requested_by="viewer"))


def test_single_skip_consumes_exactly_one_track(track_factory):
    queue = QueueManager()
    engine = _FakeEngine(queue)
    _fill(queue, track_factory, 5)
    _wire_autostart(queue, engine)

    engine.skip()

    assert queue.length() == 4, (
        f"one skip drained {5 - queue.length()} tracks — the queue-changed "
        f"listener re-entered and popped again"
    )
    assert len(engine.started) == 1


def test_repeated_skips_consume_one_track_each(track_factory):
    queue = QueueManager()
    engine = _FakeEngine(queue)
    _fill(queue, track_factory, 10)
    _wire_autostart(queue, engine)

    for _ in range(3):
        engine.skip()

    assert queue.length() == 7
    assert len(engine.started) == 3


def test_skip_spam_cannot_empty_a_long_queue(track_factory):
    """The reported symptom: a burst of skips runs away through the queue."""
    queue = QueueManager()
    engine = _FakeEngine(queue)
    _fill(queue, track_factory, 20)
    _wire_autostart(queue, engine)

    for _ in range(5):
        engine.skip()

    assert queue.length() == 15, (
        f"5 skips consumed {20 - queue.length()} tracks"
    )


def test_autostart_still_works_when_engine_is_genuinely_idle(track_factory):
    """The loop fix must not break the case the listener exists for."""
    queue = QueueManager()
    engine = _FakeEngine(queue)
    _wire_autostart(queue, engine)

    # Engine idle, nothing playing, a track arrives — it should start.
    queue.enqueue(track_factory(1, requested_by="viewer"))

    assert engine.started == ["track-1"]
    assert queue.length() == 0


# ── The fixes themselves ──────────────────────────────────────────────────────
# The tests above model the loop with a stand-in engine.  These assert the real
# guards exist, so the model cannot drift away from the code it represents.

def _src(rel: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8-sig")


def test_engine_guards_the_pop_then_play_window():
    body = _src("player/engine.py")
    assert "def is_advancing" in body
    skip = body[body.index("    def skip(self) -> None:"):]
    skip = skip[:skip.index("\n    def ", 10)]
    assert "_advancing = True" in skip, "skip() must raise the guard before popping"
    assert "finally:" in skip, "the guard must be released even if play_track raises"


def test_autostart_paths_respect_the_guard():
    body = _src("ui/main_window.py")
    for fn in ("_on_queue_changed", "_maybe_autostart"):
        start = body.index(f"def {fn}(")
        chunk = body[start:body.index("\n    def ", start + 10)]
        assert "is_advancing" in chunk, (
            f"{fn} can auto-start a track while a skip is mid-flight, "
            f"which is what caused the runaway drain"
        )


def test_skip_command_is_debounced():
    body = _src("integrations/twitch_bot.py")
    assert "SKIP_DEBOUNCE_SECONDS" in body
    start = body.index("async def cmd_skip(")
    chunk = body[start:body.index("\n        @tw_commands.command", start)]
    assert "_check_skip_debounce" in chunk, "!skip must be debounced"
