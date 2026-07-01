"""
Cloudflare Quick Tunnel.

Spawns `cloudflared tunnel --url http://localhost:<port>` as a subprocess.
Parses stdout for the assigned *.trycloudflare.com URL.

cloudflared's own quick-tunnel banner reads:
    Your quick Tunnel has been created! Visit it at (it may take some time
    to be reachable): https://<random>.trycloudflare.com
— i.e. Cloudflare itself documents that the URL can be printed before it's
actually routable, which is exactly what TunnelBase's post-announce health
check (_verify_and_announce) exists to cover.

Every line is tagged with a level (INF/WRN/ERR), sometimes with a leading
RFC3339 timestamp: "2021-06-04T06:21:16Z INF Starting tunnel ...". ERR/WRN
lines are surfaced as progress so an actual reported reason (DNS failure,
QUIC handshake timeout, certificate error, etc.) is visible immediately
instead of only being inferred later from silence via the URL watchdog.

ToS note: The main branch ships this mode.  The `no-cloudflare` branch
omits it entirely; the tunnel layer is swappable by design.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from typing import Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

from tunnel.base import TunnelBase  # noqa: E402

URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_LEVEL_PATTERN = re.compile(r"\b(ERR|WRN)\b")


class CloudflareTunnel(TunnelBase):
    def __init__(self, local_port: int) -> None:
        super().__init__(local_port)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def _do_start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _do_stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self.public_url = None

    def _check_config_conflict(self) -> None:
        """
        Quick Tunnels refuse to start at all if ~/.cloudflared/config.yml (or
        .yaml) exists — a documented, easy-to-hit footgun that otherwise
        looks identical to any other startup failure.  Surface it by name so
        it doesn't have to be re-diagnosed from a generic timeout.
        """
        from pathlib import Path
        cfg_dir = Path.home() / ".cloudflared"
        for name in ("config.yml", "config.yaml"):
            if (cfg_dir / name).is_file():
                self._emit_progress(
                    f"note: {cfg_dir / name} exists — cloudflared quick tunnels "
                    f"refuse to start with a config file present; if this fails "
                    f"repeatedly, try renaming that file"
                )
                return

    def _run(self) -> None:
        self._check_config_conflict()
        watchdog = self._start_url_watchdog()
        try:
            self._proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{self.local_port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
            for line in self._proc.stdout:
                m = URL_PATTERN.search(line)
                if m and not self.public_url:
                    watchdog.cancel()
                    self._verify_and_announce(m.group(0))
                    return
                if _LEVEL_PATTERN.search(line):
                    # Surface cloudflared's own reported reason (DNS failure,
                    # QUIC handshake timeout, certificate error, etc.)
                    # immediately rather than only inferring failure later
                    # from silence via the URL watchdog.
                    self._emit_progress(f"cloudflared: {line.strip()}")
        except FileNotFoundError:
            watchdog.cancel()
            self._emit_error(
                "cloudflared not found. Install from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
                fatal=True,
            )
            return
        except Exception as e:
            watchdog.cancel()
            self._emit_error(str(e), fatal=True)
            return

        # stdout hit EOF without ever printing a URL — either the watchdog
        # killed a stuck process, or cloudflared exited on its own.  Retry
        # rather than leaving the tunnel silently dead.
        watchdog.cancel()
        if not self._stop_requested:
            self._restart_or_give_up()
