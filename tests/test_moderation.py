"""
Banned terms and banned users.

`twitch.blacklist_terms` and `twitch.blacklist_channels` were presented in the
settings page as working moderation controls — "songs containing these words
will be rejected" — but nothing in the app ever read them.  They were saved to
disk and consulted by nobody, so a streamer who listed slurs, banned artists or
abusive viewers had protection that did not exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from player.moderation import (
    check_request,
    check_resolved,
    find_blocked_term,
    is_blocked_user,
)
from player.queue_manager import Track, TrackSource


@dataclass
class _TwitchCfg:
    blacklist_terms: list = field(default_factory=list)
    blacklist_channels: list = field(default_factory=list)


# ── Banned users ──────────────────────────────────────────────────────────────

def test_banned_user_is_recognised():
    assert is_blocked_user("BadActor", ["badactor"])


def test_user_match_is_case_insensitive():
    assert is_blocked_user("BADACTOR", ["BadActor"])


def test_leading_at_is_ignored_on_both_sides():
    """Streamers type '@name' as often as 'name'."""
    assert is_blocked_user("@badactor", ["badactor"])
    assert is_blocked_user("badactor", ["@badactor"])


def test_unlisted_user_is_allowed():
    assert not is_blocked_user("someone", ["badactor"])


def test_empty_list_blocks_nobody():
    assert not is_blocked_user("anyone", [])
    assert not is_blocked_user("anyone", None)


def test_partial_username_does_not_match():
    """A user list is exact — 'bad' must not ban 'badger'."""
    assert not is_blocked_user("badger", ["bad"])


# ── Banned terms ──────────────────────────────────────────────────────────────

def test_banned_term_is_found():
    assert find_blocked_term("a song about bannedword here", ["bannedword"]) == "bannedword"


def test_term_match_is_case_insensitive():
    assert find_blocked_term("BANNEDWORD", ["bannedword"])


def test_term_matches_as_a_substring():
    """A streamer listing 'slur' expects it to catch 'slurs'."""
    assert find_blocked_term("some slurs here", ["slur"])


def test_accents_do_not_evade_a_term():
    assert find_blocked_term("bánnedwörd", ["bannedword"])


def test_clean_text_passes():
    assert find_blocked_term("a perfectly ordinary song", ["bannedword"]) is None


def test_no_terms_configured_allows_everything():
    assert find_blocked_term("anything at all", []) is None
    assert find_blocked_term("anything at all", None) is None


# ── Pre-resolution screening ──────────────────────────────────────────────────

def test_request_from_a_banned_user_is_rejected():
    cfg = _TwitchCfg(blacklist_channels=["badactor"])
    reason = check_request("some song", "badactor", cfg)
    assert reason is not None
    assert "banned-users" in reason


def test_request_containing_a_banned_term_is_rejected():
    cfg = _TwitchCfg(blacklist_terms=["bannedword"])
    assert check_request("play bannedword now", "viewer", cfg) is not None


def test_ordinary_request_is_allowed():
    cfg = _TwitchCfg(blacklist_terms=["bannedword"], blacklist_channels=["badactor"])
    assert check_request("daft punk one more time", "viewer", cfg) is None


def test_screening_happens_before_any_lookup():
    """
    A banned user should cost nothing — the check runs on the request text and
    username alone, with no network access required.
    """
    cfg = _TwitchCfg(blacklist_channels=["badactor"])
    assert check_request("", "badactor", cfg) is not None


# ── Post-resolution screening ─────────────────────────────────────────────────

def test_banned_artist_is_caught_after_resolution():
    """
    '!sr that one song' reveals nothing up front — the banned artist only
    becomes visible once the track has been resolved.
    """
    cfg = _TwitchCfg(blacklist_terms=["bannedartist"])
    track = Track(title="A Song", artist="BannedArtist",
                  source=TrackSource.YOUTUBE)
    assert check_resolved(track, cfg) is not None


def test_banned_word_in_a_resolved_title_is_caught():
    cfg = _TwitchCfg(blacklist_terms=["bannedword"])
    track = Track(title="Song About Bannedword", artist="Someone",
                  source=TrackSource.YOUTUBE)
    assert check_resolved(track, cfg) is not None


def test_clean_resolved_track_passes():
    cfg = _TwitchCfg(blacklist_terms=["bannedword"])
    track = Track(title="One More Time", artist="Daft Punk",
                  source=TrackSource.YOUTUBE)
    assert check_resolved(track, cfg) is None


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_both_request_paths_screen_before_and_after_resolution():
    """
    The lists were previously read by nothing.  Both the chat and the
    channel-point path must consult them, on both sides of resolution.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8-sig"
    )
    chat = src[src.index("def on_song_request("):src.index("def on_token_refreshed(")]
    assert "check_request(" in chat
    assert "check_resolved(" in chat

    cp_start = src.index("def on_channel_points_request(")
    cp = src[cp_start:cp_start + 4000]
    assert "check_request(" in cp
    assert "check_resolved(" in cp


def test_a_blocked_channel_point_request_refunds_the_viewer():
    """Points must not be spent on something the streamer refuses to play."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8-sig"
    )
    cp_start = src.index("def on_channel_points_request(")
    cp = src[cp_start:cp_start + 4000]
    blocked_at = cp.index("check_request(")
    assert "_cancel_cp(" in cp[blocked_at:blocked_at + 600]
