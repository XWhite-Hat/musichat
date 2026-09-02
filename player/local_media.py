"""
local_media.py — read metadata from audio files on disk.

The playback engine already handles local files without modification: its
yt-dlp re-resolution is gated on a domain match, so a filesystem path falls
straight through to PyAV, and _open_container already skips the HTTP reconnect
options for non-http sources.  What was missing is the step before that —
turning a path into a populated Track.

Metadata comes from the container itself rather than a tag library.  PyAV is
already a dependency and libavformat parses ID3, Vorbis comments, MP4 atoms and
the rest, so `container.metadata` covers every format the app can decode without
adding mutagen.
"""
from __future__ import annotations

import os

# Extensions offered in file dialogs and accepted on import.
#
# Chosen to match the decoders actually present in the vendored LGPL FFmpeg
# build (mp3, aac, flac, vorbis, opus, alac, pcm, wma, ac3, ape, musepack) —
# not a guess, and not the wider set a GPL build would carry.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".mp3", ".wav", ".flac", ".m4a", ".mp4", ".aac",
    ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif",
    ".alac", ".ape", ".wv", ".mpc",
})

# Container metadata keys vary by format: ID3 uses TPE1 -> "artist", Vorbis
# comments use ARTIST, MP4 uses \xa9ART.  libavformat normalises most of these,
# but not consistently across formats, so check several spellings.
_TITLE_KEYS  = ("title", "TITLE", "\xa9nam")
_ARTIST_KEYS = ("artist", "ARTIST", "album_artist", "albumartist", "\xa9ART")
_ALBUM_KEYS  = ("album", "ALBUM", "\xa9alb")


def is_supported_file(path: str) -> bool:
    """True if *path* has an extension the app can decode."""
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def _first(meta: dict, keys: tuple[str, ...]) -> str:
    """First non-empty value among *keys*, matched case-insensitively."""
    lowered = {k.lower(): v for k, v in meta.items() if isinstance(k, str)}
    for key in keys:
        val = meta.get(key) or lowered.get(key.lower())
        if val and str(val).strip():
            return str(val).strip()
    return ""


def probe(path: str) -> dict | None:
    """
    Read title/artist/album/duration from an audio file.

    Returns a dict shaped like the yt-dlp info dicts the resolver already
    consumes, so downstream conversion needs no special case:

        {"title", "artist", "album", "duration", "webpage_url", "filepath"}

    Returns None if the file is missing, unreadable, or has no audio stream —
    callers should treat that as "skip this file", not as an error worth
    raising, because bulk import will always meet a few bad files.
    """
    if not path or not os.path.isfile(path):
        return None

    try:
        # Deferred: importing PyAV is not free and this module is imported
        # during app start.
        import av
    except ImportError:
        print("[local_media] PyAV not available — cannot read local files")
        return None

    try:
        with av.open(path) as container:
            stream = next(
                (s for s in container.streams if s.type == "audio"), None
            )
            if stream is None:
                return None

            meta = dict(container.metadata or {})
            duration = 0
            if stream.duration and stream.time_base:
                duration = int(float(stream.duration) * float(stream.time_base))
            elif container.duration:
                duration = int(container.duration / 1_000_000)   # AV_TIME_BASE

            title = _first(meta, _TITLE_KEYS)
            artist = _first(meta, _ARTIST_KEYS)
    except Exception as exc:
        print(f"[local_media] could not read {os.path.basename(path)}: {exc}")
        return None

    # Untagged files are the common case, not the exception — a filename is a
    # far better label than "Unknown title".
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]

    return {
        "title":       title,
        "artist":      artist,
        "album":       _first(meta, _ALBUM_KEYS),
        "duration":    max(0, duration),
        "filepath":    os.path.abspath(path),
        # The resolver reads webpage_url when building a Track; for local media
        # the absolute path IS the playable source.
        "webpage_url": os.path.abspath(path),
    }


def scan_directory(root: str, recursive: bool = True) -> list[str]:
    """
    List supported audio files under *root*, sorted for a stable import order.

    Does not probe them — enumeration should stay fast so the caller can show a
    count before committing to reading several thousand containers.
    """
    found: list[str] = []
    if not os.path.isdir(root):
        return found

    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if is_supported_file(name):
                    found.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full) and is_supported_file(name):
                found.append(full)

    found.sort(key=str.lower)
    return found


def extract_cover_art(path: str) -> bytes | None:
    """
    Return the embedded cover image from an audio file, or None.

    In a container, artwork is carried as a video stream flagged
    AV_DISPOSITION_ATTACHED_PIC — a single still frame rather than a moving
    picture.  Reading it here means local tracks feed the existing cover-art
    colour matching exactly like a remote thumbnail does, without the artwork
    ever needing to exist as a file or a URL.
    """
    if not path or not os.path.isfile(path):
        return None

    try:
        import av
    except ImportError:
        return None

    # libavformat exposes the flag as a bit on the stream disposition.
    _ATTACHED_PIC = 0x0400

    try:
        with av.open(path) as container:
            for stream in container.streams:
                if stream.type != "video":
                    continue
                disposition = int(getattr(stream, "disposition", 0) or 0)
                # Some builds report attached pictures without the flag set;
                # a video stream inside an audio file is cover art regardless.
                if disposition and not (disposition & _ATTACHED_PIC):
                    continue
                for packet in container.demux(stream):
                    if packet.size:
                        return bytes(packet)
    except Exception as exc:
        print(f"[local_media] no cover art in {os.path.basename(path)}: {exc}")
    return None
