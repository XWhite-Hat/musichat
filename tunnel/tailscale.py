"""
Tailscale Funnel tunnel.

Requires Tailscale to be installed and authenticated independently.
URL is stable across restarts; no PII in the hostname.

Funnel provisions its TLS cert via Let's Encrypt.  Let's Encrypt's own
"duplicate certificate" rate limit — 5 certs per exact set of domains per
168 hours — is well-documented and can be hit by repeated funnel restarts,
surfacing as an ACME error like:
    "429 urn:ietf:params:acme:error:rateLimited: ... too many certificates
    (5) already issued for this exact set of domains in the last 168 hours"
sometimes with an exact "retry after <RFC3339 timestamp>".  Unlike
Cloudflare's undocumented quick-tunnel rate limit, this window and count
are officially published, so a precise "retry after" timestamp (when
present) is preferred over the 168h fallback.
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
_LETSENCRYPT_LIMIT_PATTERN = re.compile(r"rateLimited|too many certificates", re.IGNORECASE)
_RETRY_AFTER_PATTERN = re.compile(r"retry after (\d{4}-\d{2}-\d{2}T[\d:.]+Z)")
# Let's Encrypt's officially documented duplicate-certificate window — a
# safe upper bound even though the actual reset may be sooner depending on
# when within that window the previous certs were issued.
_LETSENCRYPT_COOLDOWN_SECONDS = 168 * 3600


class TailscaleTunnel(TunnelBase):
    def __init__(self, local_port: int) -> None:
        super().__init__(local_port)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def _do_start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _do_stop(self) -> None:
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
        watchdog = self._start_url_watchdog()
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
                if _LETSENCRYPT_LIMIT_PATTERN.search(line):
                    watchdog.cancel()
                    until = None
                    retry_match = _RETRY_AFTER_PATTERN.search(line)
                    if retry_match:
                        import datetime
                        try:
                            until = datetime.datetime.strptime(
                                retry_match.group(1), "%Y-%m-%dT%H:%M:%S.%fZ"
                            ).replace(tzinfo=datetime.timezone.utc).timestamp()
                        except ValueError:
                            until = None
                    if until is not None:
                        self._emit_rate_limited(line.strip(), until=until)
                    else:
                        # No precise "retry after" in the message — fall back
                        # to Let's Encrypt's documented window, NOT the base
                        # class's generic default (that's sized for
                        # Cloudflare's undocumented ~31min quick-tunnel
                        # limit, wildly wrong for a 168h cert-issuance one).
                        self._emit_rate_limited(
                            line.strip(), cooldown_seconds=_LETSENCRYPT_COOLDOWN_SECONDS
                        )
                    self._do_stop()
                    return
                m = URL_PATTERN.search(line)
                if m and not self.public_url:
                    watchdog.cancel()
                    self._verify_and_announce(m.group(0))
                    return
        except FileNotFoundError:
            watchdog.cancel()
            self._emit_error(
                "tailscale not found or not running. "
                "Install and authenticate at https://tailscale.com/download",
                fatal=True,
            )
            return
        except Exception as e:
            watchdog.cancel()
            self._emit_error(str(e), fatal=True)
            return

        # stdout hit EOF without ever printing a URL — either the watchdog
        # killed a stuck process, or tailscale exited on its own.  Retry
        # rather than leaving the tunnel silently dead.
        watchdog.cancel()
        if not self._stop_requested:
            self._restart_or_give_up()
