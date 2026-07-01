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
    # Kept short — this is a plain unauthenticated GET, not a real workload —
    # so a genuinely dead tunnel is reported quickly instead of sitting silent.
    _HEALTH_RETRIES = 6
    _HEALTH_TIMEOUT = 3.0       # seconds, per request
    _HEALTH_RETRY_DELAY = 2.0   # seconds, between attempts
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

    def _verify_and_announce(self, url: str) -> None:
        """
        Confirm the tunnel actually proxies to the local server before treating
        it as live — cloudflared/ngrok/tailscale can each report a URL before
        the edge-to-local path is fully established, or a transient hiccup can
        leave a registered hostname pointing nowhere.  Either way, the URL
        would otherwise be shown as ready when it's actually dead until
        someone notices and restarts the app manually.

        Retries briefly against the mod panel's own unauthenticated
        /config.js endpoint, reporting progress on every attempt so this
        never goes silent.  If it never comes up, self-heals by restarting
        the tunnel process (bounded — see _MAX_RESTARTS).

        Call this from the tunnel's worker thread — it blocks for up to
        _HEALTH_RETRIES * (_HEALTH_TIMEOUT + _HEALTH_RETRY_DELAY) seconds.
        """
        import requests

        for attempt in range(1, self._HEALTH_RETRIES + 1):
            if self._stop_requested:
                return
            self._emit_progress(
                f"verifying tunnel is routing traffic... ({attempt}/{self._HEALTH_RETRIES})"
            )
            try:
                resp = requests.get(f"{url}/config.js", timeout=self._HEALTH_TIMEOUT)
                if resp.ok:
                    self.public_url = url
                    self._restart_count = 0
                    if self.on_url_assigned:
                        self.on_url_assigned(url)
                    return
            except Exception as e:
                print(f"[tunnel] health check attempt {attempt} failed: {e!r}")
            time.sleep(self._HEALTH_RETRY_DELAY)

        self._restart_or_give_up()
