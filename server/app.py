"""
FastAPI mod panel application.

Startup
-------
  Runs in a background thread via uvicorn.
  The spectrogram endpoint is bound to localhost only (hardcoded).
  The mod panel endpoint binds to server.host (default 127.0.0.1).

Auth
----
  GET  /login             → redirect to Twitch OAuth
  GET  /auth/callback     → exchange code, verify mod, issue JWT
  GET  /me                → whoami (requires JWT)
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import AppConfig
from server.auth import (
    build_twitch_auth_url,
    generate_oauth_state,
    get_twitch_user,
    is_mod_or_broadcaster,
    issue_jwt,
    validate_oauth_state,
    verify_jwt,
)
from server.routes import queue as queue_routes
from server.routes import spectrogram as spec_routes
from server.routes import overlays as overlay_routes


# Per-IP rate limiter for /auth/token — 20 requests per 60-second window.
# Keyed by remote IP; entries prune automatically as the window slides.
_TOKEN_RL: dict[str, deque] = {}
_TOKEN_RL_MAX   = 20
_TOKEN_RL_WINDOW = 60  # seconds

def _token_rate_ok(ip: str) -> bool:
    now = time.monotonic()
    dq  = _TOKEN_RL.setdefault(ip, deque())
    while dq and now - dq[0] > _TOKEN_RL_WINDOW:
        dq.popleft()
    if len(dq) >= _TOKEN_RL_MAX:
        return False
    dq.append(now)
    return True


def create_app(cfg: AppConfig, queue_manager, on_ready=None) -> FastAPI:
    app = FastAPI(title="StreamDeck Mod Panel", docs_url=None, redoc_url=None, openapi_url=None)

    # ── Security headers ────────────────────────────────────────────────────────
    # Applied to every response from this server.
    @app.middleware("http")
    async def add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # CSP: allow same-origin scripts, styles, images; block everything else.
        # 'unsafe-inline' is required for the small inline <script> blocks in
        # the callback and stale-session pages.  ws: allows the /queue/live WS.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' ws: wss: https://id.twitch.tv; "
            "frame-ancestors 'none'"
        )
        return response

    # Auto-generate JWT secret if not set
    if not cfg.server.jwt_secret:
        cfg.server.jwt_secret = secrets.token_hex(32)

    # Init route modules
    queue_routes.init(queue_manager, cfg.server.jwt_secret)
    spec_routes.init(cfg)
    overlay_routes.init(cfg, queue_manager)

    app.include_router(queue_routes.router)
    app.include_router(spec_routes.router)
    app.include_router(overlay_routes.router)

    # ── Static assets (htmx, fonts, icons) ─────────────────────────────────────
    import os as _os
    _static_dir = _os.path.join(_os.path.dirname(__file__), "static")

    # Block the settings sub-directory on this port — it's only served from
    # the settings server (8766) and must not be reachable over the tunnel.
    # This route must be registered BEFORE the StaticFiles mount so it takes
    # priority in Starlette's route list.
    @app.get("/static/settings/{path:path}", include_in_schema=False)
    async def _block_settings_static(path: str):
        return JSONResponse({"error": "not_found"}, status_code=404)

    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    # ── Static mod panel ────────────────────────────────────────────────────────

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        import os
        from fastapi.responses import FileResponse
        ico = os.path.join(os.path.dirname(__file__), "static", "favicon.ico")
        return FileResponse(ico, media_type="image/x-icon")

    @app.get("/", response_class=HTMLResponse)
    async def serve_panel(request: Request):
        import os
        html_path = os.path.join(
            os.path.dirname(__file__), "static", "index.html"
        )
        with open(html_path, "r", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())

    # ── Auth flow ───────────────────────────────────────────────────────────────
    #
    # Mod panel uses Twitch Implicit Grant (response_type=token).  All OAuth URL
    # building happens server-side — the frontend JS just redirects to /login.
    #
    # Flow
    # ----
    # 1. Mod clicks "Sign in" → /login → Twitch OAuth (response_type=token)
    # 2. Twitch redirects to /auth/callback with token in URL *fragment*
    # 3. The callback page JS reads the fragment, POSTs bare token to /auth/token
    # 4. Server verifies mod status using the streamer's stored token, issues JWT
    #
    # BYOI mode
    # ---------
    # /login builds a Twitch Implicit Grant URL using the user's own client_id
    # (from TWITCH_CLIENT_ID env var).  redirect_uri comes from the request base,
    # so localhost and any stable tunnel domain work.  User must register whatever
    # URI they plan to use in their own Twitch developer console — that's the
    # point of BYOI; they own the app.
    #
    # Proxied mode
    # ------------
    # /login passes control to the Worker's /mod-login endpoint.  The Worker owns
    # the entire OAuth loop:
    #   • musicauth.xwhitehat.dev/mod-callback is a permanently-registered URI in
    #     the MusicHat Twitch app — no local URL ever needs to be registered.
    #   • Worker stores state → return_uri in KV, redirects to Twitch.
    #   • Twitch returns token in fragment to /mod-callback.
    #   • /mod-callback JS calls /mod-relay to retrieve return_uri, then relays
    #     the fragment to the local /auth/callback page.
    #   • /auth/token verifies mod status via Worker /userinfo + /is-mod.

    @app.get("/login")
    async def login(request: Request):
        from constants import TWITCH_APP_CLIENT_ID, TWITCH_WORKER_URL, is_byoi_mode
        from urllib.parse import quote as _quote
        state = generate_oauth_state()
        if state is None:
            return JSONResponse({"error": "server_busy"}, status_code=429)

        if is_byoi_mode():
            # BYOI: Implicit Grant direct to Twitch using the user's own client ID.
            # redirect_uri is derived from the current request base so it works
            # for localhost and any stable tunnel domain the user has registered
            # in their Twitch developer console.
            client_id = cfg.twitch.client_id or TWITCH_APP_CLIENT_ID
            if not client_id:
                return JSONResponse({"error": "app_not_configured"}, status_code=503)
            redirect_uri = str(request.url_for("auth_callback"))
            url = build_twitch_auth_url(client_id, redirect_uri, state)
        else:
            # Proxied: delegate the entire OAuth loop to the Worker.
            # musicauth.xwhitehat.dev/mod-callback is a permanently-registered
            # redirect URI in the MusicHat Twitch app.  The Worker stores
            # state → {return_uri, broadcaster_id} in KV, gates the mod auth
            # flow on the broadcaster being already registered, and redirects
            # to Twitch.  No local URL needs to be registered.
            if not cfg.twitch.streamer_id:
                # Broadcaster hasn't authenticated yet — mods can't sign in
                # until the streamer has logged in at least once.
                callback_base = str(request.url_for("auth_callback"))
                return RedirectResponse(
                    f"{callback_base}?error=streamer_not_authenticated_yet"
                )
            callback_uri = str(request.url_for("auth_callback"))
            url = (
                f"{TWITCH_WORKER_URL}/mod-login"
                f"?state={_quote(state)}"
                f"&return_uri={_quote(callback_uri)}"
                f"&broadcaster_id={_quote(cfg.twitch.streamer_id)}"
            )

        return RedirectResponse(url)

    # /auth/callback — handles two flows:
    #
    # BYOI:    Twitch sends #access_token=TOKEN&state=STATE in the URL fragment.
    #          JS reads the fragment and POSTs {token, state} to /auth/token.
    #
    # Proxied: Worker redirects here with ?mod_code=CODE&state=STATE as query
    #          params (no token in the URL — token stayed on musicauth worker).
    #          JS reads query params and POSTs {mod_code, state} to /auth/token.
    #          /auth/token then calls worker /mod-exchange to claim the token.
    #
    # Security: all user-visible messages use textContent not innerHTML to prevent
    # any Twitch-supplied error strings from being interpreted as HTML.
    #
    # DPoP: a fresh keypair is generated here and persisted to IndexedDB
    # (dpop.js) before /auth/token is called, so the server can bind its
    # thumbprint into the issued JWT (cnf.jkt).  The mod panel loads this same
    # keypair back out of IndexedDB after the redirect to '/' — see dpop.js.
    _MOD_CALLBACK_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body{background:#0a0f0a;color:#b8ffca;font-family:monospace;
         display:flex;align-items:center;justify-content:center;
         height:100vh;margin:0;}
    #msg{text-align:center;color:#4d8a5f;font-size:.85em;}
  </style>
</head>
<body>
<div id="msg">Completing sign-in…</div>
<script src="/static/dpop.js"></script>
<script>
  const msg = document.getElementById('msg');

  function ok(text) {
    msg.textContent = text;
    msg.style.color = '#00ff41';
    msg.style.fontSize = '1.2em';
  }

  function fail(text) {
    // textContent — never innerHTML — user-supplied strings must not become HTML
    msg.textContent = text;
    msg.style.color = '#ff4444';
  }

  async function postToken(payload) {
    try {
      const keyPair = await dpopGenerateKeyPair();
      await dpopSaveKeyPair(keyPair);
      payload.jwk = await dpopExportPublicJwk(keyPair);
    } catch (e) {
      fail('Could not create a signing key for this session: ' + e);
      return;
    }
    fetch('/auth/token', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(payload),
    })
    .then(r => r.json())
    .then(d => {
      if (d.jwt) {
        sessionStorage.setItem('mh_jwt', d.jwt);
        ok('Signed in — redirecting…');
        setTimeout(() => { location.href = '/'; }, 800);
      } else {
        fail('Access denied: ' + (d.error || 'unknown'));
      }
    })
    .catch(() => fail('Network error — close this tab and try again.'));
  }

  const qs      = new URLSearchParams(location.search);
  const frag    = new URLSearchParams(location.hash.slice(1));
  const modCode = qs.get('mod_code');
  const state   = qs.get('state') || frag.get('state') || '';
  const token   = frag.get('access_token');
  const err     = qs.get('error') || frag.get('error');

  const errorMessages = {
    streamer_not_authenticated_yet:
      "The streamer hasn't logged in yet — ask them to open the app and sign in first.",
  };

  if (err) {
    fail(errorMessages[err] || ('Sign-in cancelled: ' + err));
  } else if (modCode) {
    // Proxied flow: token lives on the worker, claim it with the opaque code
    postToken({mod_code: modCode, state});
  } else if (token) {
    // BYOI flow: token arrived in fragment directly from Twitch
    postToken({token, state});
  } else {
    fail('No token received. Try again.');
  }
</script>
</body>
</html>"""

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback():
        """Serve the JS fragment-reader page — token never hits this server directly."""
        return HTMLResponse(_MOD_CALLBACK_HTML)

    @app.post("/auth/token")
    async def auth_token(request: Request):
        """
        Verify mod status and issue a short-lived JWT.

        Handles two sub-flows:

        BYOI (body contains 'token'):
          access_token arrived directly from Twitch Implicit Grant fragment.
          Helix calls made directly using the user's own client_id.

        Proxied (body contains 'mod_code'):
          access_token was held by the Worker; claim it via /mod-exchange.
          Helix calls routed through Worker /userinfo + /is-mod.
        """
        client_ip = (request.client.host if request.client else "") or "unknown"
        if not _token_rate_ok(client_ip):
            return JSONResponse({"error": "rate_limited"}, status_code=429)

        ct = request.headers.get("content-type", "").split(";")[0].strip()
        if ct != "application/json":
            return JSONResponse({"error": "invalid_content_type"}, status_code=415)

        import asyncio as _asyncio
        import requests as _req
        from constants import TWITCH_APP_CLIENT_ID, TWITCH_WORKER_URL

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_body"}, status_code=400)

        state    = (body.get("state") or "").strip()
        mod_code = (body.get("mod_code") or "").strip()
        token    = (body.get("token") or "").strip()
        jwk      = body.get("jwk")

        if not state:
            return JSONResponse({"error": "missing_state"}, status_code=400)
        if not validate_oauth_state(state):
            return JSONResponse({"error": "invalid_state"}, status_code=400)

        # The mod's DPoP public key must be supplied at login time — its
        # thumbprint gets embedded in the JWT (cnf.jkt) so every subsequent
        # API call can be bound to this specific browser keypair.
        if (not isinstance(jwk, dict)
                or jwk.get("kty") != "EC"
                or jwk.get("crv") != "P-256"
                or not isinstance(jwk.get("x"), str)
                or not isinstance(jwk.get("y"), str)):
            return JSONResponse({"error": "missing_or_invalid_jwk"}, status_code=400)

        loop = _asyncio.get_running_loop()

        if mod_code:
            # Proxied path: claim the access token from the worker's KV store.
            # The worker generated the opaque code in /mod-verify; it's one-time
            # use and expires in 60 seconds.
            def _exchange() -> str:
                try:
                    r = _req.post(
                        f"{TWITCH_WORKER_URL}/mod-exchange",
                        json={"code": mod_code},
                        timeout=10,
                    )
                    r.raise_for_status()
                    return r.json().get("access_token", "")
                except Exception as exc:
                    print(f"[auth] mod-exchange failed: {exc}")
                    return ""

            access_token = await loop.run_in_executor(None, _exchange)
            if not access_token:
                return JSONResponse({"error": "exchange_failed"}, status_code=500)
            client_id = ""  # proxied — worker supplies Client-Id
        elif token:
            # BYOI path: token arrived directly from Twitch fragment.
            access_token = token
            client_id = cfg.twitch.client_id or TWITCH_APP_CLIENT_ID
        else:
            return JSONResponse({"error": "missing_token"}, status_code=400)

        # In proxied mode the worker gates /userinfo on broadcaster_id to prevent
        # third-party use.  Pass the streamer's ID so the gate can be checked.
        _broadcaster_id = cfg.twitch.streamer_id if not client_id else ""

        def _lookup_user():
            return get_twitch_user(access_token, client_id, broadcaster_id=_broadcaster_id)

        user = await loop.run_in_executor(None, _lookup_user)
        if not user:
            return JSONResponse({"error": "user_lookup_failed"}, status_code=500)

        username = user.get("login", "")
        user_id  = user.get("id", "")
        def _check_mod():
            return is_mod_or_broadcaster(
                username,
                user_id,
                cfg.twitch.streamer_id,
                access_token,   # mod's own fresh token — never stale
                client_id,
            )

        authorized = await loop.run_in_executor(None, _check_mod)
        if not authorized:
            return JSONResponse({"error": "not_a_moderator"}, status_code=403)

        from dpop_utils import jwk_thumbprint
        jkt = jwk_thumbprint(
            {"kty": jwk["kty"], "crv": jwk["crv"], "x": jwk["x"], "y": jwk["y"]}
        )
        jwt_token = issue_jwt(
            username, cfg.server.jwt_secret, cfg.server.jwt_expiry_minutes, jkt=jkt
        )
        return JSONResponse({"jwt": jwt_token, "username": username})

    @app.get("/config.js")
    async def client_config():
        """Exposes non-secret config to the frontend JS."""
        from constants import TWITCH_APP_CLIENT_ID
        client_id = cfg.twitch.client_id or TWITCH_APP_CLIENT_ID
        return JSONResponse({
            "twitch_client_id": client_id,
            "port": cfg.server.port,
            "channel": cfg.twitch.channel or "",
        })

    @app.get("/me")
    async def whoami(request: Request):
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        payload = verify_jwt(token, cfg.server.jwt_secret)
        if not payload:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _verify_dpop(request, payload):
            return JSONResponse({"error": "invalid_dpop"}, status_code=401)
        return {"username": payload["sub"]}

    # ── Search ──────────────────────────────────────────────────────────────────
    # Mod-facing search — runs yt-dlp in an executor so the event loop isn't
    # blocked.  Returns lightweight result cards; enqueue resolves the full URL.

    def _verify_dpop(request: Request, payload: dict) -> bool:
        """Verify the DPoP proof against the key thumbprint bound into this
        token's cnf.jkt claim (RFC 9449 §6.1) — no separate registry lookup."""
        jkt = (payload.get("cnf") or {}).get("jkt")
        if not jkt:
            return False  # token predates DPoP binding — force re-login
        proof = request.headers.get("DPoP", "")
        if not proof:
            return False
        from dpop_utils import verify_proof
        htu = str(request.url).split("?")[0]
        return verify_proof(proof, request.method, htu, jkt)

    @app.get("/search")
    async def search_tracks(q: str, request: Request):
        import asyncio as _asyncio
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        payload = verify_jwt(token, cfg.server.jwt_secret)
        if not payload:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        username = payload["sub"]
        if not _verify_dpop(request, payload):
            return JSONResponse({"error": "invalid_dpop"}, status_code=401)
        # 20 searches per minute per user
        if not queue_routes.check_rate(username, 20, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        if not q or not q.strip():
            return {"results": []}
        from integrations.yt_dlp_client import search_youtube
        loop = _asyncio.get_running_loop()
        tracks = await loop.run_in_executor(None, search_youtube, q.strip(), 8)
        return {
            "results": [
                {
                    "id": t.id,
                    "title": t.title,
                    "url": t.url,
                    "thumbnail": t.thumbnail_url,
                    "duration": t.duration_seconds,
                }
                for t in tracks
            ]
        }

    @app.post("/enqueue")
    async def enqueue_track(request: Request):
        import asyncio as _asyncio
        from player.queue_manager import RequestOrigin
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        payload = verify_jwt(token, cfg.server.jwt_secret)
        if not payload:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        username = payload["sub"]
        if not _verify_dpop(request, payload):
            return JSONResponse({"error": "invalid_dpop"}, status_code=401)
        # 10 enqueues per minute per user
        if not queue_routes.check_rate(username, 10, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_body"}, status_code=400)
        url = (body.get("url") or "").strip()
        if not url:
            return JSONResponse({"error": "missing_url"}, status_code=400)
        # Resolve in executor — yt-dlp is blocking
        from integrations.yt_dlp_client import resolve_url
        loop = _asyncio.get_running_loop()
        track = await loop.run_in_executor(None, resolve_url, url)
        if track is None:
            return JSONResponse({"error": "could_not_resolve"}, status_code=422)
        track.requested_by = username
        track.origin = RequestOrigin.CHAT
        pos = queue_manager.enqueue_request(track)
        queue_routes.log_action("enqueue", username, {
            "track_id": track.id,
            "track_title": track.display_title(),
            "outcome": "ok",
        })
        return {"position": pos, "track": queue_manager._track_dict(track)}

    @app.on_event("startup")
    async def _on_startup():
        import asyncio as _asyncio
        queue_routes.set_event_loop(_asyncio.get_running_loop())
        if on_ready:
            on_ready()

    return app


class ServerManager:
    def __init__(self, cfg: AppConfig, queue_manager) -> None:
        self.cfg = cfg
        self.app = create_app(cfg, queue_manager, on_ready=self._on_ready)
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None
        self.on_ready: Optional[callable] = None
        self.on_error: Optional[callable] = None

    def start(self) -> None:
        uc = uvicorn.Config(
            self.app,
            host=self.cfg.server.host,
            port=self.cfg.server.port,
            log_level="warning",
            log_config=None,   # DefaultFormatter calls isatty() which breaks when stderr is None (windowed binary)
            loop="asyncio",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",  # only trust the local tunnel process
        )
        self._server = uvicorn.Server(uc)
        self._thread = threading.Thread(
            target=self._run_server, daemon=True
        )
        self._thread.start()

    def _run_server(self) -> None:
        try:
            self._server.run()
        except Exception as exc:
            if self.on_error:
                self.on_error(str(exc))

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5.0)

    def _on_ready(self) -> None:
        if self.on_ready:
            self.on_ready()
