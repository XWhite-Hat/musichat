"""
Server-side request forgery via viewer song requests.

Song requests arrive from untrusted viewers.  A URL-shaped request was handed
straight to yt-dlp (which fetches the page) and later to FFmpeg (which opens it
as a stream), with no scheme or host validation on the chat and channel-point
paths — while the mod-panel path had validated all along.  That let a viewer
make the streamer's machine issue requests to cloud metadata endpoints, router
admin pages, and anything bound to localhost or the LAN.

The decisive test stands up a real HTTP listener and asserts a chat request
cannot reach it.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from player.net_guard import (
    is_allowed_media_host,
    is_fetchable,
    is_internal_host,
)
from player.queue_manager import RequestOrigin
from player.resolver import resolve


# ── Address classification ────────────────────────────────────────────────────

@pytest.mark.parametrize("target", [
    "http://127.0.0.1/x",
    "http://localhost/x",
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "http://192.168.1.1/router",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://[::1]/",
    "http://0.0.0.0/",
])
def test_internal_targets_are_recognised(target):
    assert is_internal_host(target)


def test_public_hosts_are_not_internal():
    assert not is_internal_host("https://www.youtube.com/watch?v=abc")


def test_unresolvable_hosts_fail_closed():
    """
    The previous implementation returned False on any exception, so a host that
    could not be resolved was treated as safe.
    """
    assert is_internal_host("http://this-host-does-not-exist.invalid/x")


def test_ipv4_mapped_ipv6_loopback_is_caught():
    """::ffff:127.0.0.1 hides a loopback address inside a v6 literal."""
    assert is_internal_host("http://[::ffff:127.0.0.1]/x")


# ── Allowlist ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc",
    "https://youtu.be/abc",
    "https://music.youtube.com/watch?v=abc",
    "https://soundcloud.com/artist/track",
])
def test_media_hosts_are_allowed(url):
    assert is_allowed_media_host(url)


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.test/watch?v=abc",   # suffix confusion
    "https://notyoutube.com/watch?v=abc",
    "https://evil.test/?x=youtube.com",
    "http://127.0.0.1/watch?v=abc",
])
def test_lookalike_hosts_are_not_allowed(url):
    assert not is_allowed_media_host(url)


@pytest.mark.parametrize("scheme_url", [
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
    "gopher://example.com/x",
    "data:audio/mpeg;base64,AAAA",
])
def test_non_http_schemes_are_refused(scheme_url):
    allowed, _ = is_fetchable(scheme_url, viewer_supplied=True)
    assert allowed is False


# ── Posture differs by trust ──────────────────────────────────────────────────

def test_viewer_requests_use_an_allowlist():
    ok, _ = is_fetchable("https://example.com/song.mp3", viewer_supplied=True)
    assert ok is False


def test_streamer_requests_use_the_internal_denylist():
    """The streamer may paste a wider range of links, but not internal ones."""
    ok, _ = is_fetchable("https://example.com/song.mp3", viewer_supplied=False)
    assert ok is True
    ok2, _ = is_fetchable("http://127.0.0.1:9/x", viewer_supplied=False)
    assert ok2 is False


# ── The real thing ────────────────────────────────────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self):                    # stdlib naming, not ours
        _Recorder.hits.append(self.path)
        body = b"\xff\xfb\x90\x00" * 64          # mp3-ish bytes
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):           # silence the test output
        pass


@pytest.fixture
def internal_service():
    """A real HTTP server on loopback, standing in for something internal."""
    _Recorder.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("origin", [
    RequestOrigin.CHAT,
    RequestOrigin.CHANNEL_POINTS,
])
def test_viewer_request_cannot_reach_an_internal_service(internal_service, origin):
    """
    The proof: a viewer asks for a URL on loopback, and the service records
    nothing because the request never left the resolver.
    """
    track = resolve(f"{internal_service}/internal-admin?x=1",
                    requested_by="viewer", origin=origin)
    assert track is None
    assert _Recorder.hits == [], f"the internal service was contacted: {_Recorder.hits}"


def test_viewer_request_cannot_reach_cloud_metadata():
    track = resolve(
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        requested_by="viewer", origin=RequestOrigin.CHAT,
    )
    assert track is None


def test_streamer_is_also_blocked_from_internal_hosts(internal_service):
    """
    MANUAL is more trusted, but internal address space is still off limits —
    otherwise a malicious link pasted by the streamer reaches the LAN.
    """
    track = resolve(f"{internal_service}/x.mp3", origin=RequestOrigin.MANUAL)
    assert track is None
    assert _Recorder.hits == []


def test_the_mod_panel_path_shares_one_guard():
    """
    Both paths must use the same implementation — two copies drift, and it was
    the un-guarded copy that viewers could reach.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "integrations" / "yt_dlp_client.py").read_text(encoding="utf-8-sig")
    assert "from player.net_guard import is_internal_host" in src
