"""
Cloudflare Quick Tunnel.

Spawns `cloudflared tunnel --url http://localhost:<port>` as a subprocess.
Parses stdout for the assigned *.trycloudflare.com URL.

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


class CloudflareTunnel(TunnelBase):
    def __init__(self, local_port: int) -> None:
        super().__init__(local_port)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self.public_url = None

    def _run(self) -> None:
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
                    self.public_url = m.group(0)
                    if self.on_url_assigned:
                        self.on_url_assigned(self.public_url)
        except FileNotFoundError:
            if self.on_error:
                self.on_error(
                    "cloudflared not found. Install from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
                )
        except Exception as e:
            if self.on_error:
                self.on_error(str(e))
