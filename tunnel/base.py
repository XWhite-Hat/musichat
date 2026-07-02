"""Abstract tunnel interface — all tunnel implementations must subclass this."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional


class TunnelBase(ABC):
    """
    A tunnel exposes a local port to an external URL.
    The spectrogram port is never passed to any tunnel — enforced by the caller.
    """

    # Health-check retry budget before giving up on a just-assigned URL.
    # A freshly-minted *.trycloudflare.com hostname can take real time to
    # become resolvable everywhere (DNS propagation, negative-cache TTLs on
    # the local resolver) — observed in practice as NameResolutionError /
    # getaddrinfo failures for the first 10-20s.  This matters a lot here:
    # giving up too early means *restarting*, which mints a brand-new random
    # hostname with the exact same propagation lag — a loop that never
    # actually gets ahead of the problem.  Patient retries against the SAME
    # hostname are far more likely to succeed than a restart.
    _HEALTH_RETRIES = 15
    _HEALTH_TIMEOUT = 3.0       # seconds, per request
    _HEALTH_RETRY_DELAY = 3.0   # seconds, between attempts
    # Cloudflare's own "couldn't reach your origin" edge error codes — the
    # only responses that actually mean the tunnel isn't routing traffic.
    # Anything else (200, 404, 401, 500, ...) proves our own local server
    # answered the request, which is all this check needs to confirm.  We
    # deliberately do NOT require a 200 from the specific probed route —
    # that would also fail if that route's behavior/auth ever changes,
    # which has nothing to do with whether the tunnel itself is working.
    _CLOUDFLARE_ORIGIN_ERROR_CODES = {521, 522, 523, 524, 525, 526, 530}
    # How long to wait for the tunnel binary to print a URL at all before
    # treating it as stuck.  Without this, a binary that hangs or never
    # prints the expected line blocks forever with zero feedback.
    _URL_WAIT_TIMEOUT = 45.0
    # Self-heal restart budget — bounded so a genuinely dead local server
    # (not a tunnel problem) doesn't crash-loop the tunnel process forever.
    _MAX_RESTARTS = 5
    # Increasing delay before each restart attempt — repeatedly re-opening a
    # tunnel in a tight loop is exactly the kind of pattern that gets an IP
    # rate-limited (or worse, flagged) by the tunnel provider, particularly
    # Cloudflare.  Doubles each attempt, capped at _RESTART_MAX_DELAY.
    _RESTART_BASE_DELAY = 5.0   # seconds, before the 1st restart
    _RESTART_MAX_DELAY = 60.0
    # Provider-side rate-limiting (e.g. Cloudflare's quick-tunnel creation
    # API) isn't documented with an official cooldown duration — this is an
    # observed value, not a published guarantee.  Retrying sooner than this
    # is pointless (the request will just be rejected again) and repeatedly
    # hammering a rate limit risks extending it, so callers should stop
    # attempting entirely rather than folding this into the normal
    # restart/backoff cycle.
    _RATE_LIMIT_COOLDOWN_SECONDS = 31 * 60

    def __init__(self, local_port: int) -> None:
        self.local_port = local_port
        self.public_url: Optional[str] = None
        self.on_url_assigned: Optional[Callable[[str], None]] = None
        # on_error(message, fatal) — fatal=False means a retry is already
        # scheduled; fatal=True means the tunnel gave up and needs a manual
        # restart (or a config fix, e.g. a missing binary).
        self.on_error: Optional[Callable[[str, bool], None]] = None
        # on_progress(message) — lightweight, non-error status updates (e.g.
        # "verifying... 2/6") so a long verification/wait window is never
        # silent, even when nothing has actually gone wrong yet.
        self.on_progress: Optional[Callable[[str], None]] = None
        # on_rate_limited(until_unix_ts, reason) — the provider itself
        # rejected tunnel creation as rate-limited.  Distinct from on_error
        # because the correct response isn't "retry with backoff", it's
        # "stop entirely until this specific time" — a persistent, provider-
        # scoped lockout the caller is expected to remember across restarts.
        self.on_rate_limited: Optional[Callable[[float, str], None]] = None
        self._restart_count = 0
        # Set by stop() — checked by the self-heal loop so a stop requested
        # while it's sleeping in a backoff window actually cancels the
        # pending restart instead of being silently overridden by it.
        self._stop_requested = False

    def _emit_error(self, msg: str, fatal: bool) -> None:
        # Always print, regardless of whether a UI callback is wired up —
        # the "no URL ever appeared" watchdog path has nothing else to log
        # from, so without this a fatal give-up can print nothing at all.
        print(f"[tunnel] {'FATAL' if fatal else 'WARN'}: {msg}")
        if self.on_error:
            self.on_error(msg, fatal)

    def _emit_progress(self, msg: str) -> None:
        print(f"[tunnel] {msg}")
        if self.on_progress:
            self.on_progress(msg)

    def _emit_rate_limited(
        self,
        reason: str,
        cooldown_seconds: Optional[float] = None,
        until: Optional[float] = None,
    ) -> None:
        """
        Provider rejected tunnel creation as rate-limited.  Marks a lockout
        and stops entirely — no restart, no further attempts — since
        retrying immediately would just repeat the exact same rejection.

        `until` lets a caller pass a precise unlock time parsed straight out
        of the provider's own response (e.g. Let's Encrypt sometimes states
        an exact "retry after" timestamp).  Without one, `cooldown_seconds`
        (defaulting to _RATE_LIMIT_COOLDOWN_SECONDS) sets a duration from
        now — different providers can have wildly different real cooldowns,
        so this is per-call rather than a single shared constant.
        """
        import datetime
        if until is None:
            if cooldown_seconds is None:
                cooldown_seconds = self._RATE_LIMIT_COOLDOWN_SECONDS
            until = time.time() + cooldown_seconds
        when = datetime.datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[tunnel] RATE LIMITED: {reason} — not retrying until {when}")
        if self.on_rate_limited:
            self.on_rate_limited(until, reason)

    def _announce_live(self, url: str) -> None:
        """Confirmed reachable — record it, reset the restart counter, and
        make sure there's an unambiguous success line in the console."""
        print(f"[tunnel] confirmed live: {url}")
        self.public_url = url
        self._restart_count = 0
        if self.on_url_assigned:
            self.on_url_assigned(url)

    def start(self) -> None:
        """Start the tunnel. Concrete — clears the stop flag, then delegates."""
        self._stop_requested = False
        self._do_start()

    def stop(self) -> None:
        """Tear down the tunnel. Concrete — sets the stop flag, then delegates."""
        self._stop_requested = True
        self._do_stop()

    @abstractmethod
    def _do_start(self) -> None:
        """Actually launch the tunnel. Must call on_url_assigned when the URL is known."""

    @abstractmethod
    def _do_stop(self) -> None:
        """Actually tear down the tunnel process/subprocess."""

    @property
    def is_running(self) -> bool:
        return self.public_url is not None

    def _start_url_watchdog(self) -> "threading.Timer":
        """
        Start a timer that force-kills the tunnel process if no URL has
        appeared within _URL_WAIT_TIMEOUT seconds.  Subclasses block reading
        the tunnel binary's stdout line-by-line waiting for the assigned URL;
        without this, a binary that hangs, stalls, or changes its output
        format blocks that thread forever with no error and no retry —
        exactly the "stuck on connecting" failure mode this exists to catch.

        Caller must call .cancel() on the returned timer once a URL is found
        or the process ends for any other reason.
        """
        timer = threading.Timer(self._URL_WAIT_TIMEOUT, self._on_url_wait_timeout)
        timer.daemon = True
        timer.start()
        return timer

    def _on_url_wait_timeout(self) -> None:
        if self.public_url or self._stop_requested:
            return
        self._emit_progress(
            f"no URL after {self._URL_WAIT_TIMEOUT:.0f}s — restarting the tunnel process"
        )
        # Kill the process directly (not self.stop()) — this isn't a user
        # stop, it's a kick to unblock the stdout-reading loop so _run()'s
        # post-loop handler can route it through the normal restart/give-up
        # decision in _restart_or_give_up().
        self._do_stop()

    def _restart_or_give_up(self) -> None:
        """
        Shared self-heal decision: retry with increasing backoff, or give up
        once _MAX_RESTARTS is exhausted.  Used both when a freshly-found URL
        never passes its reachability check and when the tunnel process never
        produces a URL at all.
        """
        if self._stop_requested:
            return

        self._restart_count += 1
        if self._restart_count > self._MAX_RESTARTS:
            self._emit_error(
                f"tunnel failed to come up after {self._MAX_RESTARTS} restart "
                f"attempts — restart manually",
                fatal=True,
            )
            return

        delay = min(
            self._RESTART_BASE_DELAY * (2 ** (self._restart_count - 1)),
            self._RESTART_MAX_DELAY,
        )
        self._emit_error(
            f"tunnel not routing traffic — restarting in {delay:.0f}s "
            f"(attempt {self._restart_count}/{self._MAX_RESTARTS})",
            fatal=False,
        )
        time.sleep(delay)
        if self._stop_requested:
            return
        self.stop()
        self.start()

    def _dns_resolves_via_doh(self, hostname: str) -> bool:
        """
        Check whether `hostname` resolves using Cloudflare's DNS-over-HTTPS
        endpoint, hit by IP (1.1.1.1) so this never depends on the local
        machine's own resolver or its cache.

        Exists because a browser can resolve a tunnel URL just fine (many
        browsers default to their own DoH) while this process's getaddrinfo-
        based lookup keeps failing on the exact same hostname — the local
        OS resolver cached an early failed lookup (made before the record
        existed) and won't re-query until its own negative-cache TTL expires,
        no matter how many times *we* retry.  This lets us tell "genuinely
        not resolvable anywhere yet" apart from "resolvable, just not to
        this machine's stale local cache" — restarting the tunnel fixes
        neither case, but only the first one actually needs it.
        """
        import requests
        try:
            resp = requests.get(
                "https://1.1.1.1/dns-query",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=3,
            )
            return resp.ok and bool(resp.json().get("Answer"))
        except Exception:
            return False

    def _verify_and_announce(self, url: str) -> None:
        """
        Confirm the tunnel actually proxies to the local server before treating
        it as live — cloudflared/ngrok/tailscale can each report a URL before
        the edge-to-local path is fully established, or a transient hiccup can
        leave a registered hostname pointing nowhere.  Either way, the URL
        would otherwise be shown as ready when it's actually dead until
        someone notices and restarts the app manually.

        Retries an unauthenticated request against /config.js, but success
        only requires getting *any* response that isn't one of Cloudflare's
        own "couldn't reach your origin" edge errors — a 200, 404, or even a
        401 all prove traffic is reaching our local server end-to-end, which
        is the only thing this needs to confirm.  Requiring that specific
        route to return 200 would also fail this check for reasons that have
        nothing to do with the tunnel (route behavior/auth changes).

        Reports progress on every attempt so this never goes silent.  If it
        never comes up, self-heals by restarting the tunnel process (bounded
        — see _MAX_RESTARTS) — unless DNS keeps resolving externally via DoH
        while our own lookup fails, in which case the deadline is extended
        instead, since restarting would just mint an equally-fresh,
        equally-stuck-in-the-same-local-cache hostname.

        Call this from the tunnel's worker thread — it blocks for up to
        _HEALTH_RETRIES * (_HEALTH_TIMEOUT + _HEALTH_RETRY_DELAY) seconds,
        plus up to _MAX_DNS_EXTENSIONS extensions while DoH keeps confirming.
        """
        import requests
        from urllib.parse import urlparse

        hostname = urlparse(url).hostname

        # Substrings from the actual exceptions urllib3/requests raise for a
        # DNS lookup failure — seen in practice as the dominant cause of
        # early attempts failing for a hostname that was just minted.
        _DNS_MARKERS = ("NameResolutionError", "getaddrinfo failed", "Name or service not known")
        _MAX_DNS_EXTENSIONS = 10  # hard ceiling — don't wait forever even if DoH keeps saying yes

        deadline = time.monotonic() + self._HEALTH_RETRIES * (
            self._HEALTH_TIMEOUT + self._HEALTH_RETRY_DELAY
        )
        extensions = 0
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            if self._stop_requested:
                return
            self._emit_progress(f"verifying tunnel is routing traffic... (attempt {attempt})")
            try:
                resp = requests.get(f"{url}/config.js", timeout=self._HEALTH_TIMEOUT)
                if resp.status_code not in self._CLOUDFLARE_ORIGIN_ERROR_CODES:
                    self._announce_live(url)
                    return
                print(
                    f"[tunnel] health check attempt {attempt}: Cloudflare edge "
                    f"reports origin unreachable (HTTP {resp.status_code})"
                )
            except Exception as e:
                reason = repr(e)
                if any(marker in reason for marker in _DNS_MARKERS):
                    if hostname and extensions < _MAX_DNS_EXTENSIONS and self._dns_resolves_via_doh(hostname):
                        extensions += 1
                        deadline = max(deadline, time.monotonic() + self._HEALTH_RETRY_DELAY * 3)
                        print(
                            f"[tunnel] health check attempt {attempt}: DNS resolves "
                            f"externally (confirmed via DoH) but not yet on this "
                            f"machine's own resolver — waiting rather than restarting "
                            f"({reason})"
                        )
                    else:
                        print(
                            f"[tunnel] health check attempt {attempt}: DNS for the new "
                            f"tunnel hostname hasn't propagated yet ({reason})"
                        )
                else:
                    print(f"[tunnel] health check attempt {attempt} failed: {reason}")
            time.sleep(self._HEALTH_RETRY_DELAY)

        self._restart_or_give_up()
