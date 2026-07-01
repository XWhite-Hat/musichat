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
            for attempt in range(1, 31):
                if self._stop_requested:
                    return
                if attempt == 1 or attempt % 5 == 0:
                    self._emit_progress(f"waiting for ngrok to assign a URL... ({attempt}/30)")
                time.sleep(1)
                try:
                    resp = requests.get(
                        "http://127.0.0.1:4040/api/tunnels", timeout=2
                    )
                    tunnels = resp.json().get("tunnels", [])
                    for t in tunnels:
                        if t.get("proto") == "https":
                            self._verify_and_announce(t["public_url"])
                            return
                except Exception:
                    pass

            if not self._stop_requested:
                self._restart_or_give_up()
            return

        except FileNotFoundError:
            self._emit_error("ngrok not found. Install from https://ngrok.com/download", fatal=True)
        except Exception as e:
            self._emit_error(str(e), fatal=True)
