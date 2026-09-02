"""
moderation.py — enforce the streamer's banned-terms and banned-users lists.

`twitch.blacklist_terms` and `twitch.blacklist_channels` have existed in the
config and in the settings page for some time, described there as "songs
containing these words will be rejected".  Nothing read them: they were written
to disk and never consulted, so a streamer who listed slurs, banned artists or
abusive viewers got protection that did not exist.

Two checks, applied at different points because they need different information:

  * A banned **user** is known from the request itself, so that check runs
    before any network work.
  * A banned **term** may appear in the request text or only in the resolved
    title/artist, so the text is checked up front and the resolved metadata is
    checked again afterwards.
"""
from __future__ import annotations

import unicodedata


def _normalise(text: str) -> str:
    """Casefold and strip accents so simple evasions do not slip through.

    Deliberately modest: this is a streamer's own word list, not a spam filter,
    and over-normalising would make short entries match far more than intended.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().strip()


def is_blocked_user(username: str, blocked_users: list | None) -> bool:
    """True if *username* is on the banned-users list (case-insensitive)."""
    if not username or not blocked_users:
        return False
    target = _normalise(username).lstrip("@")
    return any(target == _normalise(str(u)).lstrip("@") for u in blocked_users if u)


def find_blocked_term(text: str, blocked_terms: list | None) -> str | None:
    """
    Return the first banned term appearing in *text*, or None.

    Substring matching, because the list is written by a streamer who expects
    "slur" to catch "slurs" — requiring whole words would quietly fail on the
    plurals and compounds people actually type.
    """
    if not text or not blocked_terms:
        return None
    haystack = _normalise(text)
    for raw in blocked_terms:
        term = _normalise(str(raw))
        if term and term in haystack:
            return str(raw)
    return None


def check_request(
    query: str,
    username: str,
    cfg_twitch,
) -> str | None:
    """
    Screen a request before it is resolved.

    Returns None to allow, or a short reason to reject.  The reason is for the
    streamer's log — callers decide what, if anything, to tell chat.
    """
    if is_blocked_user(username, getattr(cfg_twitch, "blacklist_channels", None)):
        return f"user {username!r} is on the banned-users list"

    hit = find_blocked_term(query, getattr(cfg_twitch, "blacklist_terms", None))
    if hit:
        return f"request text contains banned term {hit!r}"
    return None


def check_resolved(track, cfg_twitch) -> str | None:
    """
    Screen a track after resolution.

    The request text alone is not enough: "!sr that one song" reveals nothing,
    and the banned artist or title only becomes visible once yt-dlp has
    resolved it.
    """
    terms = getattr(cfg_twitch, "blacklist_terms", None)
    if not terms:
        return None

    for field in (getattr(track, "title", ""), getattr(track, "artist", "")):
        hit = find_blocked_term(field, terms)
        if hit:
            return f"resolved track matches banned term {hit!r}"
    return None
