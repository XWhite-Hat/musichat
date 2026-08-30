"""
Queue behaviour, with emphasis on the per-user request cap.

The cap shipped broken in two separate ways, so both are covered here:

  * user_request_count() read a counter that nothing ever incremented, so the
    cap never triggered and every tier's queue_limit setting did nothing.
  * The bot checked the cap, then spent a network round-trip resolving the
    track before enqueueing, so two quick requests could both pass the check.
"""
from __future__ import annotations

import threading

from player.queue_manager import QueueManager


def test_count_is_zero_for_empty_queue(track_factory):
    q = QueueManager()
    assert q.user_request_count("bob") == 0


def test_count_reflects_queued_tracks(track_factory):
    q = QueueManager()
    for i in range(3):
        q.enqueue_request(track_factory(i, requested_by="bob"))
    assert q.user_request_count("bob") == 3


def test_count_is_case_insensitive(track_factory):
    q = QueueManager()
    q.enqueue_request(track_factory(0, requested_by="Bob"))
    assert q.user_request_count("bOB") == 1


def test_count_ignores_other_users(track_factory):
    q = QueueManager()
    q.enqueue_request(track_factory(0, requested_by="bob"))
    q.enqueue_request(track_factory(1, requested_by="eve"))
    assert q.user_request_count("bob") == 1
    assert q.user_request_count("eve") == 1


def test_count_ignores_unattributed_tracks(track_factory):
    q = QueueManager()
    q.enqueue_request(track_factory(0, requested_by=""))
    assert q.user_request_count("") == 0


def test_cap_rejects_over_limit(track_factory):
    q = QueueManager()
    for i in range(3):
        assert q.enqueue_request(track_factory(i, requested_by="bob"), max_for_user=3) != -1
    assert q.enqueue_request(track_factory(99, requested_by="bob"), max_for_user=3) == -1


def test_rejected_track_is_not_queued(track_factory):
    q = QueueManager()
    for i in range(2):
        q.enqueue_request(track_factory(i, requested_by="bob"), max_for_user=2)
    q.enqueue_request(track_factory(99, requested_by="bob"), max_for_user=2)
    assert q.length() == 2


def test_cap_of_zero_means_unlimited(track_factory):
    q = QueueManager()
    for i in range(25):
        assert q.enqueue_request(track_factory(i, requested_by="bob"), max_for_user=0) != -1
    assert q.length() == 25


def test_playing_a_track_frees_a_slot(track_factory):
    """The cap means 'queued at once', so it must fall as tracks leave."""
    q = QueueManager()
    for i in range(3):
        q.enqueue_request(track_factory(i, requested_by="bob"), max_for_user=3)
    assert q.enqueue_request(track_factory(98, requested_by="bob"), max_for_user=3) == -1

    q.pop_next()   # bob's first track starts playing and leaves the queue
    assert q.user_request_count("bob") == 2
    assert q.enqueue_request(track_factory(99, requested_by="bob"), max_for_user=3) != -1


def test_removing_a_track_frees_a_slot(track_factory):
    q = QueueManager()
    for i in range(2):
        q.enqueue_request(track_factory(i, requested_by="bob"), max_for_user=2)
    q.remove("track-0")
    assert q.user_request_count("bob") == 1


def test_cap_is_enforced_atomically_under_concurrency(track_factory):
    """
    Eight threads request simultaneously against a cap of two.

    This is the shape of the real race: the bot's pre-resolve check happens
    seconds before the enqueue, so the cap has to hold at insert time.
    """
    q = QueueManager()
    barrier = threading.Barrier(8)
    results: list[int] = []
    lock = threading.Lock()

    def racer(i: int) -> None:
        barrier.wait()
        pos = q.enqueue_request(track_factory(i, requested_by="spammer"), max_for_user=2)
        with lock:
            results.append(pos)

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len([r for r in results if r != -1]) == 2
    assert q.length() == 2


def test_requests_jump_ahead_of_auto_suggestions(track_factory):
    q = QueueManager()
    auto = track_factory(1, requested_by="")
    auto.is_auto_suggestion = True
    q.enqueue(auto)
    q.enqueue_request(track_factory(2, requested_by="bob"))
    assert q.snapshot()[0].requested_by == "bob"
