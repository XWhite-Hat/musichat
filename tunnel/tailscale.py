"""
Tailscale Funnel tunnel.

Requires Tailscale to be installed and authenticated independently.
URL is stable across restarts; no PII in the hostname.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from typing import Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

from tunnel.base import TunnelBase  # noqa: E402

URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.ts\.net(?:/\S*)?")


class TailscaleTunnel(TunnelBase):
    def __init__(self, local_port: int) -> None:
        super().__init__(local_port)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Turn off funnel on this port
        try:
            subprocess.run(
                ["tailscale", "funnel", "--bg=false", str(self.local_port)],
                capture_output=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
        except Exception:
            pass
        self.public_url = None

    def _run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["tailscale", "funnel", "--bg", str(self.local_port)],
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
                    "tailscale not found or not running. "
                    "Install and authenticate at https://tailscale.com/download"
                )
        except Exception as e:
            if self.on_error:
                self.on_error(str(e))
