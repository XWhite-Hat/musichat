"""
net_guard.py — decide whether a request URL may be fetched.

Song requests arrive from untrusted viewers over Twitch chat and channel-point
redemptions, and a URL-shaped request is handed to yt-dlp (which fetches the
page) and later to FFmpeg (which opens it as a stream, speaking many protocols).
Without a check, a viewer can make the streamer's machine issue requests to
hosts the viewer cannot reach themselves: cloud metadata endpoints, router admin
pages, anything bound to localhost or the LAN.

Two postures, because the callers differ in trust:

  * Viewer-supplied (chat, channel points) — an **allowlist** of the media hosts
    the feature exists to serve.  A denylist has to be right every time; an
    allowlist only has to be right once, and it sidesteps redirects, DNS
    rebinding and alternate encodings entirely.
  * Streamer-supplied (the app's own UI) — a **denylist** of internal address
    space, since the streamer may legitimately paste a wider variety of links.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Hosts a chat/channel-point request may reach.  Subdomains are permitted; the
# match is on the registrable suffix so "music.youtube.com" passes and
# "youtube.com.evil.test" does not.
ALLOWED_MEDIA_HOSTS: frozenset[str] = frozenset({
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    "snd.sc",
})

_ALLOWED_SCHEMES = ("http", "https")


def _host_of(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    return host or None


def is_allowed_media_host(url: str) -> bool:
    """True if *url* points at one of the services song requests are meant for."""
    host = _host_of(url)
    if not host:
        return False
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in ALLOWED_MEDIA_HOSTS
    )


def is_internal_host(url_or_host: str) -> bool:
    """
    True if the target resolves to address space that should never be fetched.

    Fails **closed**: a name that cannot be resolved is treated as internal.
    The previous implementation returned False on any exception, so an
    unresolvable or deliberately malformed host was waved through.

    Every address the name resolves to is checked, not just the first — a host
    with both a public and a private record would otherwise pass on the public
    one and connect to the private one.
    """
    host = _host_of(url_or_host) or url_or_host.split("/")[0].split(":")[0].strip()
    if not host:
        return True

    # A bare IP literal needs no lookup.
    try:
        return _is_internal_address(ipaddress.ip_address(host))
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True     # cannot resolve -> cannot vouch for it

    if not infos:
        return True

    for info in infos:
        sockaddr = info[4]
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return True
        if _is_internal_address(addr):
            return True
    return False


def _is_internal_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address outside the ordinary public internet."""
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return True
    # The cloud instance-metadata address is link-local and already caught
    # above; named here so the intent survives future edits.
    if str(addr) in ("169.254.169.254", "fd00:ec2::254"):
        return True
    # An IPv4 address wrapped in IPv6 (::ffff:127.0.0.1) hides behind the v6
    # checks, so unwrap and re-test it.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _is_internal_address(mapped)
    return False


def is_fetchable(url: str, viewer_supplied: bool) -> tuple[bool, str]:
    """
    Whether *url* may be fetched, and why not when it may not.

    Returns (allowed, reason).  *reason* is for logs — it is deliberately not
    echoed to chat, since telling a prober which hosts are refused maps the
    internal network for them.
    """
    if not url:
        return False, "empty URL"

    parsed_host = _host_of(url)
    if parsed_host is None:
        return False, "only http and https URLs are accepted"

    if viewer_supplied:
        if not is_allowed_media_host(url):
            return False, f"host {parsed_host!r} is not an allowed media source"
        return True, ""

    if is_internal_host(url):
        return False, f"host {parsed_host!r} resolves to internal address space"
    return True, ""
