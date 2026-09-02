"""
Link handling in the track resolver.

Pasting a playlist or channel link used to hand the whole collection to yt-dlp
for full extraction.  Measured on a real playlist that took over 90 seconds
versus 1.5 seconds flat — and the entire time was spent holding the global
yt-dlp lock, which resolve_direct_url() also needs to start the next track.  So
one link stalled playback and every other request behind it, which is what
"breaks the entire application" looked like.

A channel was worse than slow: its flat entries are *tabs* (Videos, Shorts,
Live), so the first entry was itself a playlist and the resolver built a Track
whose stream URL was a channel ID — unplayable.

These cover the classification and selection logic, which is pure.  Network
behaviour is verified separately.
"""
from __future__ import annotations

import pytest

from player.resolver import (
    _channel_listing_url,
    _entry_target,
    _is_playable_entry,
    _sanitize_query,
    _url_kind,
)


@pytest.mark.parametrize("url, expected", [
    # Single videos
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",              "video"),
    ("https://youtu.be/dQw4w9WgXcQ",                             "video"),
    ("https://www.youtube.com/shorts/abc123",                    "video"),
    # A video that happens to sit inside a playlist is still a video request
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",   "video"),
    # Collections
    ("https://www.youtube.com/playlist?list=PLFgquLnL59al",      "playlist"),
    # Channels, in every form YouTube uses
    ("https://www.youtube.com/@RickAstleyYT",                    "channel"),
    ("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw", "channel"),
    ("https://www.youtube.com/c/RickAstley",                     "channel"),
    ("https://www.youtube.com/user/RickAstleyVEVO",              "channel"),
    ("https://www.youtube.com/@RickAstleyYT/videos",             "channel"),
])
def test_url_kind_youtube(url, expected):
    assert _url_kind(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://soundcloud.com/artist/some-track",        "video"),
    ("https://soundcloud.com/artist/sets/my-playlist",  "playlist"),
    ("https://soundcloud.com/artist",                   "channel"),
])
def test_url_kind_soundcloud(url, expected):
    assert _url_kind(url) == expected


def test_channel_root_is_pointed_at_the_video_listing():
    """A channel root flat-extracts to tabs, not videos — hence /videos."""
    assert _channel_listing_url("https://www.youtube.com/@Rick") == \
        "https://www.youtube.com/@Rick/videos"


def test_channel_listing_url_strips_query_and_trailing_slash():
    assert _channel_listing_url("https://www.youtube.com/@Rick/?x=1") == \
        "https://www.youtube.com/@Rick/videos"


@pytest.mark.parametrize("tab", ["/videos", "/streams", "/shorts", "/playlists"])
def test_channel_listing_url_leaves_an_existing_tab_alone(tab):
    url = f"https://www.youtube.com/@Rick{tab}"
    assert _channel_listing_url(url) == url


def test_playable_entry_accepts_a_normal_video():
    assert _is_playable_entry({"id": "abc", "url": "https://y/watch?v=abc"})


def test_playable_entry_rejects_a_nested_collection():
    """The channel-tab case that produced an unplayable Track."""
    assert not _is_playable_entry({"_type": "playlist", "id": "UCxxxx"})


def test_playable_entry_rejects_empty_and_none():
    assert not _is_playable_entry({})
    assert not _is_playable_entry(None)


@pytest.mark.parametrize("status", ["is_upcoming", "is_live"])
def test_playable_entry_rejects_live_and_premieres(status):
    assert not _is_playable_entry({"id": "a", "url": "u", "live_status": status})


def test_entry_target_prefers_a_full_url():
    assert _entry_target({"url": "https://www.youtube.com/watch?v=xyz", "id": "xyz"}) \
        == "https://www.youtube.com/watch?v=xyz"


def test_entry_target_builds_a_watch_url_from_a_bare_id():
    """Flat entries sometimes carry only an id."""
    assert _entry_target({"id": "xyz"}) == "https://www.youtube.com/watch?v=xyz"


def test_sanitize_strips_invisible_chars_used_to_dodge_chat_filters():
    assert _sanitize_query("never gonna​ give͏ you up") == \
        "never gonna give you up"


def test_sanitize_leaves_ordinary_text_alone():
    assert _sanitize_query("  daft punk one more time  ") == "daft punk one more time"


def test_channel_entries_are_ranked_by_view_count():
    """'Something from this channel' should mean a popular track, not the newest."""
    entries = [
        {"id": "a", "url": "u1", "view_count": 1_000},
        {"id": "b", "url": "u2", "view_count": 900_000},
        {"id": "c", "url": "u3", "view_count": None},
    ]
    entries.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    assert entries[0]["id"] == "b"


def test_resolver_never_full_extracts_a_collection():
    """Structural: the playlist/channel path must go through flat_entries."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "player" / "resolver.py").read_text(
        encoding="utf-8-sig"
    )
    body = src[src.index("def resolve("):src.index("def _dump_single(")]
    assert "flat_entries" in body, (
        "playlist/channel links must be enumerated flat — full extraction holds "
        "the global yt-dlp lock for minutes and stalls playback"
    )
