"""
SoundCloud API client.

Each user supplies their own client_id via settings.
Stream URLs are fetched from the SoundCloud API and piped through
PyAV → sounddevice so PCM fan-out to the FFT pipeline works.
"""

from __future__ import annotations

from typing import Optional

import requests

from config import SoundCloudConfig
from player.queue_manager import Track, TrackSource

SC_API = "https://api.soundcloud.com"
SC_RESOLVE = f"{SC_API}/resolve"


class SoundCloudClient:
    def __init__(self, cfg: SoundCloudConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "StreamDeckMusic/1.0"

    # ── Public API ─────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> list[Track]:
        try:
            params = {
                "q": query,
                "client_id": self.cfg.client_id,
                "limit": limit,
                "offset": 0,
                "linked_partitioning": 1,
            }
            resp = self._session.get(
                f"{SC_API}/tracks", params=params, timeout=10
            )
            resp.raise_for_status()
            return [self._track_from_dict(t) for t in resp.json().get("collection", [])]
        except Exception as e:
            print(f"[soundcloud] search error: {e}")
            return []

    def resolve_url(self, url: str) -> Optional[Track]:
        """Resolve a soundcloud.com track URL to a Track."""
        try:
            resp = self._session.get(
                SC_RESOLVE,
                params={"url": url, "client_id": self.cfg.client_id},
                timeout=10,
                allow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("kind") == "track":
                return self._track_from_dict(data)
        except Exception as e:
            print(f"[soundcloud] resolve error: {e}")
        return None

    def get_stream_url(self, track_id: int) -> Optional[str]:
        """
        Returns the progressive stream URL for a track.
        This URL can be passed directly to PyAV for decoding.
        """
        try:
            resp = self._session.get(
                f"{SC_API}/tracks/{track_id}/stream",
                params={"client_id": self.cfg.client_id},
                timeout=10,
                allow_redirects=False,
            )
            if resp.status_code in (301, 302):
                return resp.headers.get("Location")
            resp.raise_for_status()
            data = resp.json()
            return data.get("url")
        except Exception as e:
            print(f"[soundcloud] stream URL error: {e}")
            return None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _track_from_dict(self, d: dict) -> Track:
        track_id = d.get("id", 0)
        stream_url = self.get_stream_url(track_id) or ""
        return Track(
            title=d.get("title", ""),
            artist=d.get("user", {}).get("username", ""),
            url=d.get("permalink_url", ""),
            stream_url=stream_url,
            thumbnail_url=d.get("artwork_url") or "",
            duration_seconds=d.get("duration", 0) // 1000,
            source=TrackSource.SOUNDCLOUD,
        )
