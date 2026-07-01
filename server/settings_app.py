"""
FastAPI application for the web settings UI.

Served on 127.0.0.1:8766 — never tunnelled, localhost only.
A one-time launch token in the URL prevents stale-link access.

Security design
───────────────
• Token fields are NEVER included in API responses (only presence + masked preview).
• A BLOCKED_PATCH set prevents write access to tokens, IDs, and the JWT secret.
• The server binds 127.0.0.1 only — unreachable from outside the machine.
• Launch token (16-byte URL-safe random) expires after first use.
"""

from __future__ import annotations

import asyncio
import dataclasses
import secrets as _secrets
from pathlib import Path
from typing import Any, Callable, Optional, Set

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from config import AppConfig, save_config

# ── Paths ──────────────────────────────────────────────────────────────────────
# In a PyInstaller onefile binary sys._MEIPASS is the extraction root.
# In normal Python __file__ resolves relative to the package just as well.
import sys as _sys
_BUNDLE_ROOT = Path(getattr(_sys, "_MEIPASS", Path(__file__).parent.parent))
_STATIC = _BUNDLE_ROOT / "server" / "static" / "settings"

# ── Fields that must never be written via PATCH ───────────────────────────────
BLOCKED_PATCH: Set[str] = {
    "twitch.streamer_token",
    "twitch.streamer_refresh_token",
    "twitch.streamer_token_issued_at",
    "twitch.streamer_token_expires_in",
    "twitch.bot_token",
    "twitch.bot_refresh_token",
    "twitch.bot_token_issued_at",
    "twitch.bot_token_expires_in",
    "twitch.streamer_id",
    "twitch.streamer_username",
    "twitch.bot_username",
    "twitch.channel",                   # always auto-synced from streamer_username on sign-in
    "twitch.client_id",                 # changing redirects the OAuth flow to an attacker app
    "twitch.channel_points_reward_id",  # managed by the reward setup flow, not direct edit
    "server.host",                      # changing to 0.0.0.0 exposes the mod panel externally
    "server.port",                      # deployment-time decision, not runtime config
    "server.jwt_secret",
    "vibe_ack",                         # consent flag — must be set locally via the dialog, not remotely
}

# ── Fields that require a bot restart to take effect ─────────────────────────
RESTART_REQUIRED: Set[str] = {
    "twitch.prefix",
    "twitch.use_separate_bot",
    "twitch.command_aliases",   # aliases are baked into the bot at startup
}


# ── Module-level WS broadcaster (set by create_settings_app at startup) ───────
# Allows other parts of the app (e.g. _apply_art_colours in main_window.py) to
# push messages to all connected settings-page tabs without importing the whole
# FastAPI app.

_settings_ws_manager: "Optional[_WSManager]" = None

# Last-known tunnel status — cached so GET /api/tunnel/status works on page
# load even before a WS connection is established, and so a state that
# happened before the settings page was ever opened (an error, a restart in
# progress) is still visible instead of silently showing a "start tunnel"
# button as if nothing were wrong.
#
# status is the authoritative field for the settings page's UI logic:
#   "offline"     — not running, safe to start
#   "connecting"  — start requested, no URL confirmed yet
#   "verifying"   — URL found, confirming it actually routes traffic
#   "live"        — confirmed reachable
#   "restarting"  — self-healing after a failed reachability check
#   "failed"      — self-heal exhausted its retry budget; needs manual restart
_tunnel_status: dict = {
    "status": "offline", "url": None, "online": False, "error": None, "fatal": False,
}


def broadcast_to_settings(msg: dict) -> None:
    """Push *msg* to every connected settings-page WebSocket client (thread-safe)."""
    global _tunnel_status
    if msg.get("type") == "tunnel_status":
        online = bool(msg.get("online"))
        fatal = bool(msg.get("fatal"))
        has_error = bool(msg.get("error"))
        # Derive a status if the caller didn't set one explicitly, so any
        # broadcast that predates this field still degrades sensibly.
        status = msg.get("status") or (
            "live" if online else
            "failed" if fatal else
            "restarting" if has_error else
            "offline"
        )
        _tunnel_status = {
            "status": status,
            "url": msg.get("url"),
            "online": online,
            "error": msg.get("error"),
            "fatal": fatal,
        }
    if _settings_ws_manager is not None:
        _settings_ws_manager.broadcast_sync(msg)


# ── WebSocket manager ─────────────────────────────────────────────────────────

class _WSManager:
    def __init__(self) -> None:
        self._clients: list[WebSocket] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients = [c for c in self._clients if c is not ws]

    async def broadcast(self, data: dict) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def broadcast_sync(self, data: dict) -> None:
        """Thread-safe broadcast from non-async code (e.g. OAuth callback thread)."""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.broadcast(data), self._loop)


# ── Config masking ────────────────────────────────────────────────────────────

def _mask(value: str, show: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= show:
        return "••••••"
    return value[:show] + "••••"


def mask_config(cfg: AppConfig) -> dict:
    """Return a safe dict representation — tokens replaced with presence info."""
    d = dataclasses.asdict(cfg)
    tw = d["twitch"]

    # Replace every token field with a safe descriptor
    _TOKEN_FIELDS = (
        "streamer_token", "streamer_refresh_token",
        "bot_token", "bot_refresh_token",
    )
    for f in _TOKEN_FIELDS:
        raw = tw.pop(f, "")
        tw[f"__{f}_present"] = bool(raw)
        tw[f"__{f}_preview"] = _mask(raw) if raw else ""

    # Remove issued_at / expires_in from response (internal bookkeeping)
    for f in (
        "streamer_token_issued_at", "streamer_token_expires_in",
        "bot_token_issued_at", "bot_token_expires_in",
    ):
        tw.pop(f, None)

    # JWT secret — just presence
    srv = d["server"]
    raw_jwt = srv.pop("jwt_secret", "")
    srv["__jwt_secret_present"] = bool(raw_jwt)

    return d


# ── Pydantic models ───────────────────────────────────────────────────────────
# All Pydantic models must live at module level — defining them inside a
# function confuses Pydantic v2's schema resolution and FastAPI silently
# rejects request bodies without raising an obvious error.

class PatchRequest(BaseModel):
    path: str                          # dotted path, e.g. "twitch.channel"
    value: Any
    preset_name: Optional[str] = None  # if set, "spectrogram.*" patches target THIS preset
                                       # instead of the active one.  Lets the settings page
                                       # edit any preset without switching active_preset_name.


class PresetCreateRequest(BaseModel):
    name: str
    copy_from: Optional[str] = None   # name of an existing preset to copy from


class PresetRenameRequest(BaseModel):
    new_name: str


# ── Stale-session page ────────────────────────────────────────────────────────
# Returned (with 200) when the settings URL is opened with an invalid or
# already-consumed token, instead of a raw 403 JSON blob.  Styled to match the
# app's dark theme so it doesn't look broken to the user.
_STALE_SESSION_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings — Session Expired</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0e0e0e;color:#e0e0e0;font-family:system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{text-align:center;padding:40px 48px;background:#1a1a1a;
      border:1px solid #333;border-radius:8px;max-width:440px}
h1{font-size:17px;font-weight:600;margin-bottom:12px;color:#fff}
p{font-size:13px;color:#888;line-height:1.7}
strong{color:#ccc}
.btn{display:inline-block;margin-top:20px;padding:7px 18px;background:transparent;
     border:1px solid #555;border-radius:4px;color:#aaa;font-size:13px;
     cursor:pointer;font-family:inherit}
.btn:hover{border-color:#888;color:#fff}
</style>
</head>
<body>
<div class="card">
  <h1>Settings link expired</h1>
  <p>This tab is no longer connected to the app.<br>
     Close it and click <strong>&gt; settings</strong> in the player window
     to open a fresh session.</p>
  <button class="btn" onclick="window.close()">Close tab</button>
</div>
</body>
</html>"""

# ── Factory ───────────────────────────────────────────────────────────────────

def create_settings_app(
    cfg: AppConfig,
    launch_token: str,
    bot_restart_cb:    Optional[Callable[[], None]] = None,
    spec_changed_cb:   Optional[Callable[[], None]] = None,
    tunnel_start_cb:   Optional[Callable[[], None]] = None,
    tunnel_stop_cb:    Optional[Callable[[], None]] = None,
    device_changed_cb: Optional[Callable[[Optional[int]], None]] = None,
    data_reset_cb:     Optional[Callable[[], None]] = None,
    data_wipe_cb:      Optional[Callable[[bool], None]] = None,
) -> FastAPI:
    """
    Build and return the settings FastAPI app.

    Parameters
    ----------
    cfg               Shared AppConfig — mutations are visible to the whole process.
    launch_token      One-time token embedded in the settings URL for access control.
    spec_changed_cb   Called (thread-safe) whenever a spectrogram field or preset
                      changes so the Qt widget can re-read the config and repaint.
    bot_restart_cb    Called when a RESTART_REQUIRED field changes.
    device_changed_cb Called with the new device index (or None) when
                      audio.output_device is patched so the engine can hot-swap.
    """
    global _settings_ws_manager
    app = FastAPI(title="MusicHat Settings", docs_url=None, redoc_url=None)
    ws_manager = _WSManager()
    _settings_ws_manager = ws_manager   # expose for broadcast_to_settings()
    _token_store = {"token": launch_token, "used": False}
    _session_tokens: set[str] = set()

    # Every /api/* and /ws request must present a valid settings_session cookie.
    # The cookie is issued when the settings page is first loaded with the launch
    # token, so only the browser that opened the URL (or a tab descended from it)
    # can reach the API.  SameSite=Strict prevents cross-site form POST CSRF.
    @app.middleware("http")
    async def _api_auth_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/") or request.url.path == "/ws":
            tok = request.cookies.get("settings_session", "")
            if tok not in _session_tokens:
                return JSONResponse({"error": "Not authenticated"}, status_code=403)
        return await call_next(request)

    # Active OAuth sessions: account → OAuthCallbackServer
    _auth_sessions: dict[str, Any] = {}

    # ── Startup ───────────────────────────────────────────────────────────────

    @app.on_event("startup")
    async def _startup():
        ws_manager.set_loop(asyncio.get_event_loop())
        asyncio.get_event_loop().run_in_executor(None, _recover_usernames_if_needed)

    def _recover_usernames_if_needed() -> None:
        """
        If a token is present but username is empty (e.g. after a save that lost
        user-info), re-fetch and persist the username so the UI shows the account.
        Runs in a thread pool so it doesn't block the event loop.
        """
        import sys
        from constants import is_byoi_mode, TWITCH_APP_CLIENT_ID

        accounts = []
        if cfg.twitch.streamer_token and not cfg.twitch.streamer_username:
            accounts.append(("streamer", cfg.twitch.streamer_token))
        if cfg.twitch.bot_token and not cfg.twitch.bot_username:
            accounts.append(("bot", cfg.twitch.bot_token))

        if not accounts:
            return

        try:
            import requests as _req
        except ImportError:
            return

        for account, token in accounts:
            try:
                if is_byoi_mode():
                    resp = _req.get(
                        "https://api.twitch.tv/helix/users",
                        headers={"Authorization": f"Bearer {token}", "Client-Id": TWITCH_APP_CLIENT_ID},
                        timeout=6,
                    )
                    raw = resp.json().get("data", [{}])[0] if resp.ok else {}
                    username = raw.get("display_name") or raw.get("login", "")
                    user_id  = raw.get("id", "")
                else:
                    # Twitch validate endpoint — no client_id required, works in proxied mode
                    resp = _req.get(
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {token}"},
                        timeout=6,
                    )
                    val = resp.json() if resp.ok else {}
                    username = val.get("login", "")
                    user_id  = val.get("user_id", "")
                if not username:
                    print(f"[settings_app] startup recovery: could not get username for {account}", file=sys.stderr)
                    continue

                print(f"[settings_app] startup recovery: restored {account} → {username}", file=sys.stderr)
                if account == "streamer":
                    cfg.twitch.streamer_username = username
                    if user_id:
                        cfg.twitch.streamer_id = user_id
                    if not cfg.twitch.channel:
                        cfg.twitch.channel = username.lower()
                else:
                    cfg.twitch.bot_username = username

            except Exception as exc:
                print(f"[settings_app] startup recovery error ({account}): {exc}", file=sys.stderr)

        save_config(cfg)

    # ── Static / index ────────────────────────────────────────────────────────

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        ico = _STATIC.parent / "favicon.ico"
        if ico.exists():
            return FileResponse(str(ico), media_type="image/x-icon")
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/")
    async def root():
        return RedirectResponse("/settings")

    @app.get("/settings")
    async def settings_page(token: str = Query("")):
        if not _token_store["used"]:
            if token != _token_store["token"]:
                return HTMLResponse(_STALE_SESSION_HTML, status_code=200)
            _token_store["used"] = True
        html_path = _STATIC / "index.html"
        if not html_path.exists():
            return HTMLResponse("<h1>settings/index.html not found</h1>", status_code=500)
        session_tok = _secrets.token_hex(16)
        _session_tokens.add(session_tok)
        fr = FileResponse(str(html_path), media_type="text/html")
        fr.set_cookie("settings_session", session_tok, httponly=True, samesite="strict", path="/")
        return fr

    # ── Config API ────────────────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config():
        return JSONResponse(mask_config(cfg))

    @app.patch("/api/config")
    async def patch_config(req: PatchRequest):
        import re as _re
        path = req.path.strip()

        if path in BLOCKED_PATCH:
            raise HTTPException(403, f"Field '{path}' is read-only via API.")

        # Validate overlay.text_font_import to prevent CSS injection.
        # The value is injected verbatim into a CSS url() context in the overlay.
        if path == "overlay.text_font_import" and req.value:
            val = str(req.value)
            if val.startswith("data:"):
                if not _re.fullmatch(r'data:[a-zA-Z0-9/+\-.]+;base64,[A-Za-z0-9+/=]+', val):
                    raise HTTPException(400, "text_font_import: data URI must be base64-encoded")
            elif val and not (val.startswith("http://") or val.startswith("https://")):
                raise HTTPException(400, "text_font_import must be an http/https URL or base64 data URI")

        try:
            _apply_patch(cfg, path, req.value, preset_name=req.preset_name)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, f"Cannot set '{path}': {e}")

        save_config(cfg)

        needs_restart = path in RESTART_REQUIRED

        # Notify Qt app to repaint spectrogram when any preset field changes
        is_spec = path.startswith("spectrogram") or path == "active_preset_name"
        if is_spec and spec_changed_cb:
            import threading as _t
            _t.Thread(target=spec_changed_cb, daemon=True).start()

        # Notify engine to hot-swap audio device
        if path == "audio.output_device" and device_changed_cb:
            import threading as _t
            _dev = cfg.audio.output_device
            _t.Thread(target=lambda: device_changed_cb(_dev), daemon=True).start()

        # Push updated display config to connected overlay browser sources
        if path.startswith("overlay.text_"):
            from server.routes import overlays as _ov
            import threading as _t
            _t.Thread(target=_ov.push_config, daemon=True).start()

        # Include value + preset_name so other open settings tabs can update their
        # inputs without a full config reload.
        await ws_manager.broadcast({
            "type": "config_changed",
            "path": path,
            "value": req.value,
            "preset_name": req.preset_name,
            "restart_required": needs_restart,
        })
        return {"ok": True, "restart_required": needs_restart}

    # ── Status ────────────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def get_status():
        from constants import TWITCH_WORKER_URL, is_byoi_mode
        return {
            "streamer_username": cfg.twitch.streamer_username,
            "streamer_signed_in": bool(cfg.twitch.streamer_username),
            "bot_username": cfg.twitch.bot_username,
            "bot_signed_in": bool(cfg.twitch.bot_username),
            "use_separate_bot": cfg.twitch.use_separate_bot,
            "byoi_mode": is_byoi_mode(),
            "worker_url": TWITCH_WORKER_URL,
            "channel": cfg.twitch.channel,
        }

    # ── Audio devices ─────────────────────────────────────────────────────────

    @app.get("/api/devices")
    async def get_devices():
        try:
            import sounddevice as sd
            devices = []
            for i, d in enumerate(sd.query_devices()):
                if d["max_output_channels"] > 0:
                    devices.append({
                        "index": i,
                        "name": d["name"],
                        "channels": d["max_output_channels"],
                        "default": i == sd.default.device[1],
                    })
            return {"devices": devices, "current": cfg.audio.output_device}
        except Exception as e:
            return {"devices": [], "error": str(e), "current": cfg.audio.output_device}

    # ── Channel point rewards ─────────────────────────────────────────────────

    @app.post("/api/twitch/channel-points/setup")
    async def cp_setup():
        """
        Create (or verify existing) channel-points reward and enable the feature.

        Called when the user checks the "enable channel points" checkbox in the
        settings UI.  The app must own/create the reward so it can later PATCH
        redemption status (Twitch only allows the creating Client-Id to do this).

        Flow
        ----
        1. Streamer must be signed in — 400 otherwise.
        2. If a reward_id is already stored, verify it still exists:
           - Exists  → just enable the feature and return.
           - Missing → fall through to creation (create a fresh reward).
        3. Create the reward via Helix (proxied through Worker in managed mode).
        4. Store reward_id, set enabled=True, save config.
        5. Trigger a bot restart so the EventSub listener picks up the new ID.
        """
        if not cfg.twitch.streamer_id or not cfg.twitch.streamer_token:
            raise HTTPException(400, "Streamer not signed in — please authenticate first.")

        from server.auth import create_channel_points_reward, check_reward_exists, get_channel_rewards
        from constants import is_byoi_mode
        client_id = cfg.twitch.client_id if is_byoi_mode() else ""

        # If we have a stored reward_id, check it still exists
        if cfg.twitch.channel_points_reward_id:
            exists = check_reward_exists(
                cfg.twitch.streamer_id,
                cfg.twitch.channel_points_reward_id,
                cfg.twitch.streamer_token,
                client_id,
            )
            if exists:
                # Reward is still there — just re-enable
                cfg.twitch.channel_points_enabled = True
                save_config(cfg)
                if bot_restart_cb:
                    import threading as _t
                    _t.Thread(target=bot_restart_cb, daemon=True).start()
                return {
                    "ok": True,
                    "reward_id": cfg.twitch.channel_points_reward_id,
                    "created": False,
                }
            # Reward was deleted — clear it and create a new one below
            cfg.twitch.channel_points_reward_id = ""

        # Create a new reward (cost hardcoded to 200 — adjustable on Twitch dashboard)
        _reward_recovered = False
        try:
            reward = create_channel_points_reward(
                broadcaster_id=cfg.twitch.streamer_id,
                access_token=cfg.twitch.streamer_token,
                title="Song Request",
                client_id=client_id,
            )
        except RuntimeError as _e:
            _msg = str(_e)
            if "DUPLICATE_REWARD" in _msg:
                # Twitch says the title "Song Request" already exists.
                # Check if the duplicate was created by this app (manageable) or by
                # the streamer's Twitch dashboard / a different app (not manageable).
                _manageable = get_channel_rewards(
                    cfg.twitch.streamer_id,
                    cfg.twitch.streamer_token,
                    client_id,
                    only_manageable=True,
                )
                reward = next((r for r in _manageable if r.get("title") == "Song Request"), None)
                if not reward:
                    raise HTTPException(
                        409,
                        'There is a conflicting "Song Request" channel point reward on your '
                        "channel that was not created by this app. Please delete it from the "
                        "Twitch dashboard (Viewer Rewards → Channel Points) and try again.",
                    )
                _reward_recovered = True
            else:
                raise HTTPException(502, _msg)

        if not reward:
            raise HTTPException(502, "Twitch returned an empty reward list — try again.")

        cfg.twitch.channel_points_reward_id = reward["id"]
        cfg.twitch.channel_points_enabled   = True
        save_config(cfg)

        if bot_restart_cb:
            import threading as _t
            _t.Thread(target=bot_restart_cb, daemon=True).start()

        dashboard_url = (
            f"https://dashboard.twitch.tv/u/{cfg.twitch.streamer_username}"
            f"/viewer-rewards/channel-points/rewards"
        )
        return {
            "ok":            True,
            "reward_id":     reward["id"],
            "reward_title":  reward.get("title", "Song Request"),
            "dashboard_url": dashboard_url,
            "created":       not _reward_recovered,
        }

    # ── Twitch auth ───────────────────────────────────────────────────────────

    @app.post("/api/auth/{account}/begin")
    async def auth_begin(account: str):
        if account not in ("streamer", "bot"):
            raise HTTPException(400, "account must be 'streamer' or 'bot'")

        from constants import is_byoi_mode, TWITCH_APP_CLIENT_SECRET
        if is_byoi_mode() and not TWITCH_APP_CLIENT_SECRET:
            raise HTTPException(
                400,
                "TWITCH_CLIENT_SECRET is not set — add it to your .env file alongside TWITCH_CLIENT_ID",
            )

        if account in _auth_sessions:
            old = _auth_sessions.pop(account)
            old.stop()

        from server.oauth_handler import OAuthCallbackServer
        import time as _time

        def on_success(access_token: str, refresh_token: str, user: dict) -> None:
            username = user.get("display_name") or user.get("login", "")
            user_id  = user.get("id", "")

            if account == "streamer":
                cfg.twitch.streamer_token            = access_token
                cfg.twitch.streamer_refresh_token    = refresh_token
                cfg.twitch.streamer_username         = username
                cfg.twitch.streamer_id               = user_id
                cfg.twitch.streamer_token_issued_at  = _time.time()
                cfg.twitch.streamer_token_expires_in = 14400
                cfg.twitch.channel = username.lower()  # always locked to signed-in user
            else:
                cfg.twitch.bot_token            = access_token
                cfg.twitch.bot_refresh_token    = refresh_token
                cfg.twitch.bot_username         = username
                cfg.twitch.bot_id               = user_id
                cfg.twitch.bot_token_issued_at  = _time.time()
                cfg.twitch.bot_token_expires_in = 14400

            save_config(cfg)
            _auth_sessions.pop(account, None)

            ws_manager.broadcast_sync({
                "type": "auth_complete",
                "account": account,
                "username": username,
                "success": True,
            })

            # Always restart the bot after any auth change — the bot's own
            # start() guard handles missing-credentials gracefully (exits
            # cleanly with a log line).  Without this, signing in a bot
            # account while `use_separate_bot` is True would leave the bot
            # dead because the token was empty when the earlier PATCH restart
            # fired, and nothing wakes it back up once the token arrives.
            if bot_restart_cb:
                import threading as _t
                _t.Thread(target=bot_restart_cb, daemon=True).start()

        def on_failure(error: str) -> None:
            _auth_sessions.pop(account, None)
            ws_manager.broadcast_sync({
                "type": "auth_complete",
                "account": account,
                "success": False,
                "error": error,
            })

        session = OAuthCallbackServer(
            scope_mode=account,
            on_success=on_success,
            on_failure=on_failure,
        )

        if not session.start():
            raise HTTPException(503, "Port 7329 is in use — another auth session is running.")

        _auth_sessions[account] = session
        return {"url": session.get_auth_url()}

    @app.post("/api/auth/{account}/revoke")
    async def auth_revoke(account: str):
        if account not in ("streamer", "bot"):
            raise HTTPException(400)

        if account == "streamer":
            cfg.twitch.streamer_token         = ""
            cfg.twitch.streamer_refresh_token = ""
            cfg.twitch.streamer_username      = ""
            cfg.twitch.streamer_id            = ""
            cfg.twitch.streamer_token_issued_at = 0.0
        else:
            cfg.twitch.bot_token         = ""
            cfg.twitch.bot_refresh_token = ""
            cfg.twitch.bot_username      = ""
            cfg.twitch.bot_id            = ""
            cfg.twitch.bot_token_issued_at = 0.0

        save_config(cfg)
        await ws_manager.broadcast({"type": "auth_revoked", "account": account})

        # Restart bot so it picks up the credential change — if credentials
        # are now missing it will exit cleanly; if still present it stays up.
        if bot_restart_cb:
            import threading as _t
            _t.Thread(target=bot_restart_cb, daemon=True).start()

        return {"ok": True}

    # ── Bot restart ───────────────────────────────────────────────────────────

    # ── Proxied-auth consent ──────────────────────────────────────────────────

    @app.post("/api/auth/proxied-consent")
    async def record_consent():
        """Mark that the user has read and accepted the proxied-auth notice."""
        cfg.twitch.proxied_consent_given = True
        save_config(cfg)
        return {"ok": True}

    # ── Spectrogram preset management ─────────────────────────────────────────

    @app.post("/api/spectrogram/presets")
    async def create_preset(req: PresetCreateRequest):
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "name must not be empty")

        existing = [p for p in (cfg.spectrogram_presets or []) if p.name == name]
        if existing:
            raise HTTPException(409, f"Preset '{name}' already exists")

        if req.copy_from:
            base = next(
                (p for p in (cfg.spectrogram_presets or []) if p.name == req.copy_from),
                cfg.spectrogram,
            )
        else:
            base = cfg.spectrogram

        import copy
        new_preset = copy.copy(base)
        new_preset.name = name          # type: ignore[attr-defined]

        if cfg.spectrogram_presets is None:
            cfg.spectrogram_presets = []
        cfg.spectrogram_presets.append(new_preset)
        cfg.active_preset_name = name
        save_config(cfg)
        if spec_changed_cb:
            import threading as _t
            _t.Thread(target=spec_changed_cb, daemon=True).start()

        await ws_manager.broadcast({
            "type": "preset_created",
            "name": name,
        })
        return {"ok": True, "name": name}

    @app.delete("/api/spectrogram/presets/{name}")
    async def delete_preset(name: str):
        presets = cfg.spectrogram_presets or []
        if len(presets) <= 1:
            raise HTTPException(409, "Cannot delete the last preset")

        remaining = [p for p in presets if p.name != name]
        if len(remaining) == len(presets):
            raise HTTPException(404, f"Preset '{name}' not found")

        cfg.spectrogram_presets = remaining
        if cfg.active_preset_name == name:
            cfg.active_preset_name = remaining[0].name
        save_config(cfg)
        if spec_changed_cb:
            import threading as _t
            _t.Thread(target=spec_changed_cb, daemon=True).start()

        await ws_manager.broadcast({
            "type": "preset_deleted",
            "name": name,
            "active": cfg.active_preset_name,
        })
        return {"ok": True, "active": cfg.active_preset_name}

    @app.patch("/api/spectrogram/presets/{name}")
    async def rename_preset(name: str, req: PresetRenameRequest):
        new_name = req.new_name.strip()
        if not new_name:
            raise HTTPException(400, "new_name must not be empty")

        presets = cfg.spectrogram_presets or []
        target = next((p for p in presets if p.name == name), None)
        if target is None:
            raise HTTPException(404, f"Preset '{name}' not found")
        if any(p.name == new_name for p in presets if p is not target):
            raise HTTPException(409, f"Preset '{new_name}' already exists")

        target.name = new_name  # type: ignore[attr-defined]
        if cfg.active_preset_name == name:
            cfg.active_preset_name = new_name
        save_config(cfg)
        if spec_changed_cb:
            import threading as _t
            _t.Thread(target=spec_changed_cb, daemon=True).start()

        await ws_manager.broadcast({
            "type": "preset_renamed",
            "old_name": name,
            "new_name": new_name,
            "active": cfg.active_preset_name,
        })
        return {"ok": True, "name": new_name, "active": cfg.active_preset_name}

    # ── OBS overlay helpers ───────────────────────────────────────────────────

    @app.get("/api/overlay/nowplaying")
    async def overlay_nowplaying_info():
        """
        Returns the current now-playing text as formatted by the overlay
        template, plus the template string itself.

        Called by the settings UI to populate the nowplaying preview field
        without having to cross the port boundary to the main server.
        """
        from server.routes.overlays import _format_track, _current_track, _current_text
        # Re-render with the live template so saving a new template is reflected
        # immediately — _current_text is stale if template changed after track start
        current = _format_track(_current_track) if _current_track else _current_text
        return {
            "template":           cfg.overlay.nowplaying_template,
            "template_requested": getattr(cfg.overlay, "nowplaying_template_requested", ""),
            "current":            current,
        }

    # ── Commands ──────────────────────────────────────────────────────────────

    @app.get("/api/twitch/commands")
    async def get_commands():
        """Return static command definitions plus any configured custom aliases."""
        _COMMAND_DEFS = [
            {"name": "songrequest", "defaults": ["sr", "request"],
             "desc": "Request a song by URL or search query"},
            {"name": "queue",       "defaults": ["q"],
             "desc": "Show the current request queue (up to 5 tracks)"},
            {"name": "skip",        "defaults": [],
             "desc": "Skip the current song (mod / broadcaster only by default)"},
            {"name": "currentsong", "defaults": ["song", "np"],
             "desc": "Announce what’s currently playing"},
            {"name": "wrongsong",   "defaults": ["remove", "cancelsong", "whoops"],
             "desc": "Remove your last queued request (and refund channel points)"},
        ]
        aliases = cfg.twitch.command_aliases or {}
        for cmd in _COMMAND_DEFS:
            cmd["custom"] = list(aliases.get(cmd["name"], []))
        return {"commands": _COMMAND_DEFS, "prefix": cfg.twitch.prefix}

    # ── Bot restart ───────────────────────────────────────────────────────────

    @app.post("/api/bot/restart")
    async def restart_bot():
        if bot_restart_cb:
            bot_restart_cb()
            return {"ok": True}
        return {"ok": False, "error": "no restart callback registered"}

    # ── Tunnel control ────────────────────────────────────────────────────────

    @app.get("/api/tunnel/status")
    async def tunnel_status_get():
        """Return the last-known tunnel URL and online state for page load."""
        return JSONResponse(_tunnel_status)

    @app.post("/api/tunnel/start")
    async def tunnel_start():
        if not tunnel_start_cb:
            return JSONResponse({"error": "tunnel control not available"}, status_code=503)
        # Defense in depth — the settings page already disables the Start
        # button while connecting/live/restarting, but reject server-side too
        # (stale tab, double-click, direct API call) rather than tearing down
        # an in-progress attempt just to spawn an identical one.
        if _tunnel_status.get("status") in ("connecting", "live", "restarting"):
            return JSONResponse(
                {"error": f"tunnel is already {_tunnel_status['status']}"}, status_code=409
            )
        import threading as _t
        _t.Thread(target=tunnel_start_cb, daemon=True, name="TunnelStart").start()
        return {"ok": True, "message": "Starting tunnel — URL will arrive via WebSocket"}

    @app.post("/api/tunnel/stop")
    async def tunnel_stop():
        if not tunnel_stop_cb:
            return JSONResponse({"error": "tunnel control not available"}, status_code=503)
        import threading as _t
        _t.Thread(target=tunnel_stop_cb, daemon=True, name="TunnelStop").start()
        return {"ok": True}

    # ── Data removal ─────────────────────────────────────────────────────────

    @app.post("/api/data/reset")
    async def data_reset_endpoint():
        """Clear all persisted data (config + credentials) and keep app alive."""
        if not data_reset_cb:
            return JSONResponse({"error": "not available"}, status_code=503)
        import threading as _t
        _t.Thread(target=data_reset_cb, daemon=True, name="DataReset").start()
        return {"ok": True}

    @app.post("/api/data/wipe")
    async def data_wipe_endpoint(request: Request):
        """Wipe all local data and close the application."""
        if not data_wipe_cb:
            return JSONResponse({"error": "not available"}, status_code=503)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        remove_pyside6 = bool(body.get("remove_pyside6", True))
        import threading as _t
        _t.Thread(target=data_wipe_cb, args=(remove_pyside6,), daemon=True, name="DataWipe").start()
        return {"ok": True}

    @app.post("/api/data/check-folder")
    async def check_folder_endpoint(request: Request):
        """Return what MusicHat data already exists at the given path."""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        from pathlib import Path as _Path
        path = (body.get("path") or "").strip()
        if not path:
            return JSONResponse({"error": "no path"}, status_code=400)
        p = _Path(path)
        return JSONResponse({
            "has_pyside6":   (p / "pyside6" / "PySide6").is_dir(),
            "has_config":    (p / "config.json").exists(),
            "has_playlists": (p / "playlists.json").exists(),
        })

    # ── Data directory ───────────────────────────────────────────────────────

    @app.post("/api/data/open-folder")
    async def data_open_folder():
        """Open the data directory in the OS file explorer."""
        from data_dir import DATA_DIR
        import subprocess
        try:
            subprocess.Popen(["explorer", DATA_DIR])
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return {"ok": True}

    @app.get("/api/data/info")
    async def data_info():
        """Return the current data directory path, PySide6 version, and BYOI env path."""
        from data_dir import DATA_DIR
        from pathlib import Path as _Path
        ver_file = _Path(DATA_DIR) / "pyside6" / ".pyside6_version"
        pyside6_version = ""
        try:
            pyside6_version = ver_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            pass
        from constants import is_byoi_mode as _ibm
        return JSONResponse({
            "data_dir": DATA_DIR,
            "pyside6_version": pyside6_version,
            "env_file": str(_Path(DATA_DIR) / ".env"),
            "byoi_mode": _ibm(),
        })

    @app.post("/api/byoi/open-env")
    async def byoi_open_env():
        """Open the BYOI .env file in the system default editor (Notepad on Windows)."""
        import os as _os
        from data_dir import DATA_DIR
        from pathlib import Path as _Path
        env_path = _Path(DATA_DIR) / ".env"
        try:
            # Ensure the file exists — create the template if not
            if not env_path.exists():
                from bootstrap_check import _ensure_env_file
                _ensure_env_file(_Path(DATA_DIR))
            _os.startfile(str(env_path))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return {"ok": True}

    @app.get("/api/update/status")
    async def update_status():
        """Return current version and whether a newer GitHub release is available."""
        import updater as _upd
        return JSONResponse(_upd.get_status())

    @app.post("/api/update/check")
    async def update_check():
        """Trigger a synchronous update check and return the result."""
        import updater as _upd
        return JSONResponse(_upd.check_now())

    @app.post("/api/data/migrate")
    async def data_migrate(body: dict):
        """
        Copy all data to a new directory and update bootstrap.json.

        Body: {"new_path": "<absolute path>"}

        The caller must restart the app after this completes.
        """
        import shutil as _shutil
        import threading as _t
        from pathlib import Path as _Path
        from data_dir import DATA_DIR

        new_path = (body or {}).get("new_path", "").strip()
        if not new_path:
            return JSONResponse({"error": "new_path is required"}, status_code=400)

        new_path_obj = _Path(new_path)
        if new_path_obj == _Path(DATA_DIR):
            return JSONResponse({"error": "new path is the same as current"}, status_code=400)

        def _migrate() -> None:
            try:
                new_path_obj.mkdir(parents=True, exist_ok=True)
                # Copy all files from current data dir to new location
                for item in _Path(DATA_DIR).iterdir():
                    dest = new_path_obj / item.name
                    if item.is_dir():
                        _shutil.copytree(str(item), str(dest), dirs_exist_ok=True)
                    else:
                        _shutil.copy2(str(item), str(dest))
                # Update bootstrap.json to point to new location
                from bootstrap_check import write_bootstrap_config
                ver_file = new_path_obj / "pyside6" / ".pyside6_version"
                version = ""
                try:
                    version = ver_file.read_text(encoding="utf-8").strip()
                except (FileNotFoundError, OSError):
                    pass
                write_bootstrap_config(str(new_path_obj), version)
                print(f"[settings] data migrated to {new_path_obj}")
                broadcast_to_settings({
                    "type":    "data_migrated",
                    "new_path": str(new_path_obj),
                    "message": (
                        f"Data copied to {new_path_obj}. "
                        "Restart MusicHat to use the new location."
                    ),
                })
            except Exception as exc:
                print(f"[settings] migration error: {exc}")
                broadcast_to_settings({
                    "type":    "data_migrate_error",
                    "message": str(exc),
                })

        _t.Thread(target=_migrate, daemon=True, name="DataMigrate").start()
        return {"ok": True, "message": "Migration started — restart after it completes."}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws_manager.connect(ws)
        try:
            # Send initial state immediately
            await ws.send_json({
                "type": "init",
                "status": {
                    "streamer_username": cfg.twitch.streamer_username,
                    "bot_username": cfg.twitch.bot_username,
                    "channel": cfg.twitch.channel,
                },
                "tunnel": _tunnel_status,
            })
            while True:
                # Keep alive — client sends pings; we echo
                data = await ws.receive_text()
                if data == "ping":
                    await ws.send_text("pong")
        except WebSocketDisconnect:
            ws_manager.disconnect(ws)

    # ── External push helper (called from main.py) ────────────────────────────
    # Store the manager on the app object so main.py can push events
    app.state.ws_manager = ws_manager

    return app


# ── Patch helper ──────────────────────────────────────────────────────────────

def _apply_patch(cfg: AppConfig, path: str, value: Any, preset_name: Optional[str] = None) -> None:
    """
    Apply a dotted-path value to the config dataclass tree.

    Supports paths like:
      "twitch.channel"                        → cfg.twitch.channel = value
      "twitch.tier_viewer.queue_limit"        → cfg.twitch.tier_viewer.queue_limit = value
      "spectrogram.bar_count"                 → active preset bar_count = value
      "spectrogram_presets.0.bar_count"       → first preset bar_count = value

    When *preset_name* is provided, "spectrogram.*" patches target THAT preset
    instead of the active one, allowing the settings page to edit any preset
    without switching active_preset_name (and thus the in-app preview).
    """
    parts = path.split(".")

    # Special case: spectrogram.* → edit a specific or the active preset
    if parts[0] == "spectrogram" and len(parts) == 2:
        if preset_name:
            preset = cfg.get_preset(preset_name)
            if preset is None:
                raise AttributeError(f"Preset '{preset_name}' not found")
        else:
            preset = cfg.spectrogram
        attr = parts[1]
        if not hasattr(preset, attr):
            raise AttributeError(f"SpectrogramConfig has no field '{attr}'")
        _set_typed(preset, attr, value)
        _clamp_spectrogram(preset, attr)
        return

    # Walk the dotted path
    obj = cfg
    for part in parts[:-1]:
        # Handle list indexing (e.g. spectrogram_presets.0)
        if hasattr(obj, "__getitem__") and part.isdigit():
            obj = obj[int(part)]
        else:
            obj = getattr(obj, part)

    attr = parts[-1]
    if attr.isdigit():
        raise ValueError("Cannot set a list element by index directly")
    if not hasattr(obj, attr):
        raise AttributeError(f"No field '{attr}' on {type(obj).__name__}")
    _set_typed(obj, attr, value)


# Hard limits for spectrogram fields — (min, max).  Values outside these ranges
# are silently clamped before saving so bad UI input can't break the renderer.
_SPEC_CLAMPS: dict[str, tuple[float, float]] = {
    "bar_count":        (8,    256),
    "bar_gap":          (0,    30),
    "bar_min_height":   (0,    20),
    "obs_width":        (200,  3840),
    "obs_height":       (50,   1080),
    "freq_min":         (20,   2000),
    "freq_max":         (1000, 24000),
    "smoothing":        (0.0,  0.98),
    "peak_hold_frames": (1,    300),
    "peak_decay_rate":  (0.001, 0.2),
    "fft_size":         (512,  8192),
    "fps_target":       (10,   144),
    "camber_degrees":   (0,    360),
    "background_alpha": (0,    255),
    "text_font_size":   (6,    120),
    "text_width":       (100,  3840),
}


def _clamp_spectrogram(preset: object, attr: str) -> None:
    """Clamp a just-set numeric spectrogram field to its allowed range."""
    if attr not in _SPEC_CLAMPS:
        return
    lo, hi = _SPEC_CLAMPS[attr]
    val = getattr(preset, attr, None)
    if isinstance(val, (int, float)):
        clamped = max(lo, min(hi, val))
        if type(val) is int:
            clamped = int(round(clamped))
        setattr(preset, attr, clamped)


def _set_typed(obj: object, attr: str, value: Any) -> None:
    """Set attr on a dataclass instance, coercing value to the field's type."""
    import dataclasses as _dc
    import typing
    fields = {f.name: f for f in _dc.fields(obj)}
    if attr not in fields:
        raise AttributeError(f"'{attr}' is not a dataclass field")

    field_type = fields[attr].type
    if isinstance(field_type, str):
        import sys
        field_type = eval(field_type, sys.modules[type(obj).__module__].__dict__)

    # Unwrap Optional[X] (i.e. Union[X, None]).
    # If the incoming value is None or empty-string, store None and return.
    # Otherwise unwrap to X so the coercion below applies correctly.
    origin = getattr(field_type, "__origin__", None)
    if origin is typing.Union:
        inner = [t for t in field_type.__args__ if t is not type(None)]
        if value is None or value == "":
            setattr(obj, attr, None)
            return
        field_type = inner[0] if len(inner) == 1 else field_type

    # Coerce to the declared type (basic scalars only — list/dict stay as-is)
    if field_type is bool or field_type == "bool":
        value = bool(value)
    elif field_type is int or field_type == "int":
        value = int(value)
    elif field_type is float or field_type == "float":
        value = float(value)
    elif field_type is str or field_type == "str":
        value = str(value)

    setattr(obj, attr, value)
