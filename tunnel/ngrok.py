"""
ngrok tunnel.

The user pastes their ngrok authtoken into settings.
The token is passed to the ngrok binary at runtime — never stored in the repo.
Port 4040 (ngrok inspect interface) is never opened or forwarded.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

import requests  # noqa: E402

from tunnel.base import TunnelBase  # noqa: E402


class NgrokTunnel(TunnelBase):
    def __init__(
        self,
        local_port: int,
        authtoken: str,
        custom_domain: str = "",
    ) -> None:
        super().__init__(local_port)
        self.authtoken = authtoken
        self.custom_domain = custom_domain
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
            # Set authtoken first (idempotent)
            subprocess.run(
                ["ngrok", "config", "add-authtoken", self.authtoken],
                capture_output=True, timeout=10,
                creationflags=_NO_WINDOW,
            )

            cmd = ["ngrok", "http", str(self.local_port)]
            if self.custom_domain:
                cmd += ["--hostname", self.custom_domain]

            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )

            # Poll the local ngrok API (127.0.0.1:4040) for the assigned URL
            for _ in range(30):
                time.sleep(1)
                try:
                    resp = requests.get(
                        "http://127.0.0.1:4040/api/tunnels", timeout=2
                    )
                    tunnels = resp.json().get("tunnels", [])
                    for t in tunnels:
                        if t.get("proto") == "https":
                            self.public_url = t["public_url"]
                            if self.on_url_assigned:
                                self.on_url_assigned(self.public_url)
                            return
                except Exception:
                    pass

            if self.on_error:
                self.on_error("ngrok started but no URL was assigned within 30 seconds")

        except FileNotFoundError:
            if self.on_error:
                self.on_error("ngrok not found. Install from https://ngrok.com/download")
        except Exception as e:
            if self.on_error:
                self.on_error(str(e))
