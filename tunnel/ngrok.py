"""
ngrok tunnel.

The user pastes their ngrok authtoken into settings.
The token is passed to the ngrok binary at runtime — never stored in the repo.
Port 4040 (ngrok inspect interface) is never opened or forwarded.

ngrok's own agent prints its documented ERR_NGROK_NNN error codes to stdout
when it can't establish a session — most relevantly ERR_NGROK_108 ("your
account is limited to N simultaneous ngrok agent sessions"), which fires
when another ngrok agent is already running under the same account.  Unlike
Cloudflare's quick-tunnel rate limit or Let's Encrypt's cert-issuance limit,
this is a structural concurrent-session cap, not a time-based window — there
is no "wait N minutes and it clears itself".  It resolves only when the
other session ends or the plan is upgraded, so it's surfaced as a clear
fatal error rather than forced into the timed-lockout model used for the
other two.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from typing import Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

import requests  # noqa: E402

from tunnel.base import TunnelBase  # noqa: E402

_SESSION_LIMIT_PATTERN = re.compile(r"ERR_NGROK_108|simultaneous ngrok\D*session", re.IGNORECASE)
_ERR_CODE_PATTERN = re.compile(r"ERR_NGROK_\d+")


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
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_NO_WINDOW,
            )

            # ngrok's own error codes (ERR_NGROK_NNN) print to stdout, which
            # used to be discarded entirely (DEVNULL) — meaning a session
            # limit or any other agent-reported error was invisible and we'd
            # just sit polling the local API for a URL that would never
            # appear.  Drain it on its own thread so the polling loop below
            # can react without blocking on stdout itself.
            session_limited: list[Optional[str]] = [None]

            def _read_output() -> None:
                try:
                    for line in self._proc.stdout:
                        if _SESSION_LIMIT_PATTERN.search(line):
                            session_limited[0] = line.strip()
                            return
                        if _ERR_CODE_PATTERN.search(line):
                            self._emit_progress(f"ngrok: {line.strip()}")
                except Exception:
                    pass

            threading.Thread(target=_read_output, daemon=True, name="NgrokOutput").start()

            # Poll the local ngrok API (127.0.0.1:4040) for the assigned URL
            for attempt in range(1, 31):
                if self._stop_requested:
                    return
                if session_limited[0]:
                    # Structural concurrent-session cap, not a time-based
                    # rate limit — there's no cooldown that resolves this on
                    # its own, so it's a clear fatal error, not a timed
                    # lockout.  Close the other session (or upgrade) and
                    # restart manually.
                    self._emit_error(
                        f"ngrok rejected this session: {session_limited[0]} "
                        f"— close another running ngrok session (or upgrade your "
                        f"plan), then start the tunnel again manually.",
                        fatal=True,
                    )
                    self._do_stop()
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

            if session_limited[0]:
                self._emit_error(
                    f"ngrok rejected this session: {session_limited[0]} "
                    f"— close another running ngrok session (or upgrade your "
                    f"plan), then start the tunnel again manually.",
                    fatal=True,
                )
                self._do_stop()
                return

            if not self._stop_requested:
                self._restart_or_give_up()
            return

        except FileNotFoundError:
            self._emit_error("ngrok not found. Install from https://ngrok.com/download", fatal=True)
        except Exception as e:
            self._emit_error(str(e), fatal=True)
