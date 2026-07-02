"""
Cloudflare Quick Tunnel.

Spawns `cloudflared tunnel --url http://localhost:<port>` as a subprocess.
Parses stdout for the assigned *.trycloudflare.com URL.

Verification does NOT probe the public *.trycloudflare.com URL.  Two reasons:
  1. Cloudflare's own quick-tunnel banner admits the URL "may take some time
     to be reachable" — public DNS propagation lag is expected, not a fault.
  2. An automated client (Python's `requests`, default User-Agent and all)
     making repeated requests to a brand-new hostname is exactly the shape
     of traffic edge security heuristics single out — a false negative here
     is indistinguishable from a genuine failure, and there's no way to
     prove which one happened from the outside.

Instead, cloudflared starts its OWN local metrics/readiness HTTP server by
default (no flag needed) on 127.0.0.1:<port in 20241-20245>, logging e.g.
"Starting metrics server on 127.0.0.1:20241/metrics".  Its /ready endpoint
returns HTTP 200 only once the connector has an established, healthy
connection to Cloudflare's edge — straight from cloudflared itself, over
loopback, with no DNS and no public network involved at all.  Once that's
confirmed, a second loopback request to our own local server proves the app
behind the tunnel is actually up (a healthy edge connection doesn't imply
that — cloudflared could be perfectly connected while our own server is
down).  Together these are the two things actually within our control;
public DNS catching up afterward is Cloudflare's problem, not ours to poll.

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
import time
from typing import Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

from tunnel.base import TunnelBase  # noqa: E402

URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_LEVEL_PATTERN = re.compile(r"\b(ERR|WRN)\b")
_METRICS_PATTERN = re.compile(r"metrics server on (127\.0\.0\.1:\d+)")
# cloudflared's own quick_tunnel.go prints "rate limit exceeded; wait a while
# and try again" specifically for HTTP 429 from api.trycloudflare.com — that
# 429 is how Cloudflare's edge signals what it shows a browser as its
# branded "Error 1015: You are being rate limited" page.  "429" is matched
# too, in case wording differs across cloudflared versions.
_RATE_LIMIT_PATTERN = re.compile(r"rate limit exceeded|\b429\b", re.IGNORECASE)


class CloudflareTunnel(TunnelBase):
    def __init__(self, local_port: int) -> None:
        super().__init__(local_port)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._metrics_addr: Optional[str] = None  # set once cloudflared logs it

    def _do_start(self) -> None:
        self._metrics_addr = None
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

        # Read stdout on its own thread for the whole life of the process —
        # NOT just until a URL is found.  The metrics-server address line
        # ("Starting metrics server on 127.0.0.1:20241/metrics") isn't
        # guaranteed to print before the URL banner; if we stopped reading
        # the moment we saw the URL, a metrics line arriving afterward would
        # never be seen, and _verify_via_readiness would wait forever for an
        # address that already went by on a pipe nobody was draining.
        found_url: list[Optional[str]] = [None]
        rate_limited: list[Optional[str]] = [None]
        url_found = threading.Event()

        def _read_output() -> None:
            try:
                for line in self._proc.stdout:
                    if self._metrics_addr is None:
                        mm = _METRICS_PATTERN.search(line)
                        if mm:
                            self._metrics_addr = mm.group(1)
                    if not found_url[0]:
                        m = URL_PATTERN.search(line)
                        if m:
                            found_url[0] = m.group(0)
                            url_found.set()
                    if _LEVEL_PATTERN.search(line):
                        # Surface cloudflared's own reported reason (DNS
                        # failure, QUIC handshake timeout, certificate error,
                        # etc.) immediately rather than only inferring
                        # failure later from silence via the URL watchdog.
                        self._emit_progress(f"cloudflared: {line.strip()}")
                    if not rate_limited[0] and _RATE_LIMIT_PATTERN.search(line):
                        rate_limited[0] = line.strip()
                        url_found.set()  # wake the waiter immediately, don't wait out the watchdog
            except Exception:
                pass
            finally:
                url_found.set()  # wake the waiter even if no URL ever appeared

        threading.Thread(target=_read_output, daemon=True, name="CloudflaredOutput").start()
        url_found.wait(timeout=self._URL_WAIT_TIMEOUT + 5)
        watchdog.cancel()

        if rate_limited[0]:
            # No point restarting into the exact same rejection — stop the
            # process outright and let the caller persist a cooldown instead
            # of burning through the normal restart budget for nothing.
            self._do_stop()
            self._emit_rate_limited(rate_limited[0])
            return

        if found_url[0] and not self.public_url:
            self._verify_via_readiness(found_url[0])
            return

        # No URL ever appeared — the watchdog likely already killed a stuck
        # process, or cloudflared exited on its own.  Retry rather than
        # leaving the tunnel silently dead.
        if not self._stop_requested:
            self._restart_or_give_up()

    def _verify_via_readiness(self, url: str) -> None:
        """
        Confirm the tunnel is live using cloudflared's own local /ready
        endpoint, plus a loopback check of our own server — see module
        docstring for why this replaces probing the public URL directly.
        """
        import requests

        for attempt in range(1, self._HEALTH_RETRIES + 1):
            if self._stop_requested:
                return
            self._emit_progress(
                f"verifying tunnel is connected to Cloudflare's edge... "
                f"(attempt {attempt}/{self._HEALTH_RETRIES})"
            )

            if not self._metrics_addr:
                print("[tunnel] cloudflared hasn't reported its metrics server address yet")
                time.sleep(self._HEALTH_RETRY_DELAY)
                continue

            try:
                ready = requests.get(
                    f"http://{self._metrics_addr}/ready", timeout=self._HEALTH_TIMEOUT
                )
            except Exception as e:
                print(f"[tunnel] couldn't reach cloudflared's own readiness endpoint: {e!r}")
                time.sleep(self._HEALTH_RETRY_DELAY)
                continue

            if ready.status_code != 200:
                print(f"[tunnel] cloudflared reports not ready yet (HTTP {ready.status_code})")
                time.sleep(self._HEALTH_RETRY_DELAY)
                continue

            # Edge connection confirmed by cloudflared itself.  Now confirm
            # our own local server actually answers — a healthy edge link
            # doesn't guarantee that; it just means cloudflared has somewhere
            # to forward requests, not that anything useful is listening.
            try:
                local = requests.get(
                    f"http://127.0.0.1:{self.local_port}/config.js",
                    timeout=self._HEALTH_TIMEOUT,
                )
                if local.status_code < 500:
                    self._announce_live(url)
                    return
                print(f"[tunnel] cloudflared is connected, but the local server returned {local.status_code}")
            except Exception as e:
                print(f"[tunnel] cloudflared is connected, but the local server isn't responding yet: {e!r}")

            time.sleep(self._HEALTH_RETRY_DELAY)

        self._restart_or_give_up()
