"""
Headless Twitch OAuth callback server — no Qt dependency.

Manages the port 7329 server that receives the Twitch redirect after the user
authorises in the browser.  Used by the web settings UI to complete sign-in
without opening a PyQt dialog.

Only one session can hold port 7329 at a time; concurrent calls to start()
block until the port is free.
"""

from __future__ import annotations

import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from constants import (
    TWITCH_APP_CLIENT_ID,
    TWITCH_APP_CLIENT_SECRET,
    TWITCH_BOT_SCOPES,
    TWITCH_REDIRECT_PORT,
    TWITCH_REDIRECT_URI,
    TWITCH_STREAMER_SCOPES,
    TWITCH_WORKER_URL,
    is_byoi_mode,
)

# ── Global serialisation — only one auth session at a time ────────────────────
_SESSION_LOCK = threading.Lock()
_active_session: Optional["OAuthCallbackServer"] = None


_DONE_HTML = """\
<!DOCTYPE html><html>
<head><style>
  body{background:#0a0f0a;color:#b8ffca;font-family:monospace;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .ok{color:#00ff41;font-size:1.2em}.dim{color:#4d8a5f;font-size:.85em;margin-top:.5em}
</style></head>
<body><div style="text-align:center">
  <p class="ok">Signed in — you can close this tab.</p>
  <p class="dim">Return to the MusicHat settings page.</p>
</div></body></html>"""


def _make_callback_server(port: int, handler_cls) -> HTTPServer:
    """Dual-stack localhost server — accepts both ::1 (IPv6) and 127.0.0.1."""
    class _DualStack(HTTPServer):
        address_family = socket.AF_INET6
        def server_bind(self):
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                pass
            super().server_bind()
    try:
        return _DualStack(("::", port), handler_cls)
    except OSError:
        return HTTPServer(("127.0.0.1", port), handler_cls)


class OAuthCallbackServer:
    """
    Manages the port 7329 callback server for one Twitch OAuth round-trip.

    Parameters
    ----------
    scope_mode   "streamer" or "bot"
    on_success   called with (access_token, refresh_token, user_info_dict)
    on_failure   called with (error_message)
    """

    def __init__(
        self,
        scope_mode: str,
        on_success: Callable[[str, str, dict], None],
        on_failure: Callable[[str], None],
    ) -> None:
        self.scope_mode = scope_mode
        self.on_success = on_success
        self.on_failure = on_failure
        self._byoi  = is_byoi_mode()
        self._state = secrets.token_urlsafe(20)
        self._server: Optional[HTTPServer] = None
        self._done   = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_auth_url(self) -> str:
        """Return the URL the browser should open to begin sign-in."""
        if self._byoi:
            scopes = (
                TWITCH_STREAMER_SCOPES if self.scope_mode == "streamer"
                else TWITCH_BOT_SCOPES
            )
            scope_str = scopes.strip().replace(" ", "+")
            return (
                f"https://id.twitch.tv/oauth2/authorize"
                f"?client_id={TWITCH_APP_CLIENT_ID}"
                f"&redirect_uri={TWITCH_REDIRECT_URI}"
                f"&response_type=code"
                f"&scope={scope_str}"
                f"&state={self._state}"
            )
        else:
            return (
                f"{TWITCH_WORKER_URL}/login"
                f"?mode={self.scope_mode}"
                f"&state={self._state}"
            )

    def start(self) -> bool:
        """
        Acquire the session lock and start the port 7329 server.
        Returns False if the port is already in use.
        """
        global _active_session
        if not _SESSION_LOCK.acquire(blocking=False):
            return False  # another session is running
        _active_session = self
        session = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                print(f"[oauth_handler] GET {parsed.path} qs={dict(qs)} byoi={session._byoi}",
                      file=sys.stderr)

                # Both BYOI (Auth Code) and proxied flows redirect to /callback.
                # /token is kept for legacy compat with older Worker redirects.
                is_token_path = parsed.path.startswith("/token") or parsed.path == "/callback"

                if is_token_path:
                    body = _DONE_HTML.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                    if session._done:
                        return

                    error = qs.get("error", [""])[0]
                    code  = qs.get("code",  [""])[0]
                    state = qs.get("state", [""])[0]
                    if error:
                        session._fail(error)
                    elif code:
                        target = (session._exchange_byoi_code if session._byoi
                                  else session._exchange_code)
                        threading.Thread(
                            target=target,
                            args=(code, state),
                            daemon=True,
                        ).start()
                    else:
                        print(f"[oauth_handler] /callback hit with no code or error — qs={dict(qs)}",
                              file=sys.stderr)
                    return

                print(f"[oauth_handler] unhandled path {parsed.path!r} — returning 404",
                      file=sys.stderr)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_):
                pass

        try:
            self._server = _make_callback_server(TWITCH_REDIRECT_PORT, _Handler)
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
            return True
        except OSError as e:
            print(f"[oauth_handler] port {TWITCH_REDIRECT_PORT} busy: {e}", file=sys.stderr)
            _SESSION_LOCK.release()
            _active_session = None
            return False

    def stop(self) -> None:
        global _active_session
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
            self._server = None
        try:
            _SESSION_LOCK.release()
        except RuntimeError:
            pass
        _active_session = None

    # ── Completion paths ───────────────────────────────────────────────────────

    def _exchange_byoi_code(self, code: str, state: str) -> None:
        """BYOI: exchange auth code for tokens directly with Twitch."""
        if state != self._state:
            print(
                f"[oauth_handler] state mismatch: expected {self._state!r}, got {state!r}",
                file=sys.stderr,
            )
            self._fail("state_mismatch")
            return
        try:
            import requests
            resp = requests.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id":     TWITCH_APP_CLIENT_ID,
                    "client_secret": TWITCH_APP_CLIENT_SECRET,
                    "code":          code,
                    "grant_type":    "authorization_code",
                    "redirect_uri":  TWITCH_REDIRECT_URI,
                },
                timeout=10,
            )
            if not resp.ok:
                self._fail(f"token_exchange_failed_{resp.status_code}")
                return
            data = resp.json()
            access_token  = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            if not access_token:
                self._fail("no_access_token")
                return
            val_resp = requests.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {access_token}"},
                timeout=6,
            )
            val = val_resp.json() if val_resp.ok else {}
            user = {
                "login":        val.get("login", ""),
                "display_name": val.get("login", ""),
                "id":           val.get("user_id", ""),
            }
            print(f"[oauth_handler] BYOI resolved user login={user['login']!r} id={user['id']!r}",
                  file=sys.stderr)
            self._succeed(access_token, refresh_token, user)
        except Exception as e:
            print(f"[oauth_handler] BYOI exchange exception: {e}", file=sys.stderr)
            self._fail(str(e))

    def _exchange_code(self, code: str, state: str) -> None:
        """Proxied: exchange one-time UUID with Worker, then fetch user info."""
        if state != self._state:
            print(
                f"[oauth_handler] state mismatch: expected {self._state!r}, got {state!r}",
                file=sys.stderr,
            )
            self._fail("state_mismatch")
            return
        try:
            import requests
            import dpop_utils as _dpop
            _exchange_body: dict = {"code": code}
            _jwk = _dpop.get_public_jwk()
            if _jwk:
                _exchange_body["dpop_jwk"] = _jwk
            # If a tunnel is already running, include its origin so the Worker
            # can register/update it atomically with the streamer credential check.
            from server.settings_app import _tunnel_status as _ts
            _tu = (_ts or {}).get("url") or ""
            if _tu and _tu.startswith("https://"):
                _exchange_body["panel_origin"] = _tu
            resp = requests.post(
                f"{TWITCH_WORKER_URL}/exchange",
                json=_exchange_body,
                timeout=10,
            )
            print(f"[oauth_handler] /exchange HTTP {resp.status_code} keys={list(resp.json().keys()) if resp.ok else resp.text[:200]!r}",
                  file=sys.stderr)
            if not resp.ok:
                self._fail(f"exchange_failed_{resp.status_code}")
                return
            data = resp.json()
            access_token  = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            if not access_token:
                self._fail("no_access_token_in_response")
                return

            # Fetch user identity via Twitch's validation endpoint — no client_id needed,
            # works in proxied mode without any worker involvement.
            val_resp = requests.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {access_token}"},
                timeout=6,
            )
            print(f"[oauth_handler] /validate HTTP {val_resp.status_code} body={val_resp.text[:300]!r}",
                  file=sys.stderr)
            val = val_resp.json() if val_resp.ok else {}
            # validate returns: {"login": "name", "user_id": "123", "client_id": "...", ...}
            user = {
                "login":        val.get("login", ""),
                "display_name": val.get("login", ""),   # validate doesn't return display_name
                "id":           val.get("user_id", ""),
            }
            print(f"[oauth_handler] resolved user login={user['login']!r} id={user['id']!r}",
                  file=sys.stderr)
            self._succeed(access_token, refresh_token, user)
        except Exception as e:
            print(f"[oauth_handler] exchange exception: {e}", file=sys.stderr)
            self._fail(str(e))

    def _succeed(self, access_token: str, refresh_token: str, user: dict) -> None:
        if self._done:
            return
        self._done = True
        threading.Thread(target=self.stop, daemon=True).start()
        try:
            self.on_success(access_token, refresh_token, user)
        except Exception as e:
            print(f"[oauth_handler] on_success callback error: {e}", file=sys.stderr)

    def _fail(self, error: str) -> None:
        if self._done:
            return
        self._done = True
        threading.Thread(target=self.stop, daemon=True).start()
        try:
            self.on_failure(error)
        except Exception as e:
            print(f"[oauth_handler] on_failure callback error: {e}", file=sys.stderr)
