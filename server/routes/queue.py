"""Queue management API routes."""

from __future__ import annotations

import asyncio
import time as _time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

router = APIRouter(prefix="/queue", tags=["queue"])
security = HTTPBearer()

# ── Per-user rate limiting (in-memory, resets on server restart) ──────────────
# Tracks (username → deque of request timestamps) for endpoints that could
# be abused by a valid but shared or compromised JWT.

_RATE_WINDOWS: dict[str, deque] = {}

# One-time WebSocket tickets: ticket → (username, expiry_monotonic)
# Issued by GET /queue/ws-ticket, consumed on WS upgrade. Browsers cannot send
# Authorization headers during a WebSocket handshake, so we use a short-lived
# ticket instead of passing the JWT in the URL (where it would appear in logs).
_WS_TICKETS: dict[str, tuple[str, float]] = {}
_WS_TICKET_TTL = 30  # seconds

def check_rate(username: str, limit: int, window_sec: int) -> bool:
    """Return True if the user is within their rate limit, False if over."""
    now = _time.monotonic()
    q = _RATE_WINDOWS.setdefault(username, deque())
    # Evict timestamps outside the window
    while q and now - q[0] > window_sec:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True

# Also wire into queue action endpoints (skip, prev, etc.) that already go
# through JWT verification via the _require_auth dependency — those are low
# volume so no additional rate limit is needed there.

# Injected at app startup
_queue_manager = None
_jwt_secret = ""
_ws_clients: list[WebSocket] = []
_event_loop: asyncio.AbstractEventLoop | None = None
_skip_callback = None        # set by main.py after engine is ready
_playpause_callback = None   # toggles engine play/pause
_seek_callback = None        # seek_relative(delta_seconds)
_prev_callback = None        # go to previous track in history
_player_state_getter = None  # returns engine PlayState name string
_position_getter = None      # returns (elapsed_seconds, total_seconds)
_pause_requests_callback = None  # set_requests_paused(bool) — from main.py
_pause_requests_getter = None    # returns cfg.twitch.requests_paused

# Append-only ring buffer — last 50 mod/engine actions, pushed with every WS frame
_action_log: deque = deque(maxlen=50)
_log_updated_callbacks: list = []


def on_log_updated(cb) -> None:
    """Register a callback fired on the calling thread whenever a new action is logged."""
    _log_updated_callbacks.append(cb)


def get_action_log() -> list:
    """Return a snapshot of the current action log (newest last)."""
    return list(_action_log)


def init(queue_manager, jwt_secret: str) -> None:
    global _queue_manager, _jwt_secret
    _queue_manager = queue_manager
    _jwt_secret = jwt_secret
    _queue_manager.on_queue_changed.append(_schedule_broadcast)


def set_skip_callback(cb) -> None:
    """Wire the engine's skip() so /queue/skip actually advances playback."""
    global _skip_callback
    _skip_callback = cb


def set_playpause_callback(cb) -> None:
    global _playpause_callback
    _playpause_callback = cb


def set_seek_callback(cb) -> None:
    global _seek_callback
    _seek_callback = cb


def set_player_state_getter(fn) -> None:
    global _player_state_getter
    _player_state_getter = fn


def set_prev_callback(cb) -> None:
    global _prev_callback
    _prev_callback = cb


def set_position_getter(fn) -> None:
    global _position_getter
    _position_getter = fn


def set_pause_requests_callback(cb) -> None:
    global _pause_requests_callback
    _pause_requests_callback = cb


def set_pause_requests_getter(fn) -> None:
    global _pause_requests_getter
    _pause_requests_getter = fn


def schedule_broadcast() -> None:
    """Public entry-point so main.py can trigger a broadcast on engine state changes."""
    _schedule_broadcast()


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _event_loop
    _event_loop = loop


def _state_payload(error: str | None = None) -> dict:
    """Build the canonical response payload used for both 200s and 409s.

    Includes current queue state, last 20 log entries, and the live player
    state so every panel can fully resync from a single response body.
    """
    data = _queue_manager.to_dict()
    data["log"] = list(_action_log)[-20:]
    data["state"] = _player_state()
    data["requests_paused"] = _pause_requests_getter() if _pause_requests_getter else False
    if error:
        data["error"] = error
    return data


def log_action(action: str, actor: str, detail: dict) -> None:
    """Append an entry to the action log.  Called from route handlers and app.py."""
    entry = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "actor": actor,
        **detail,
    }
    _action_log.append(entry)
    for _cb in _log_updated_callbacks:
        try:
            _cb()
        except Exception:
            pass
    # Push the updated state (including the new log entry) to all panels
    _schedule_broadcast()


def _schedule_broadcast() -> None:
    """Schedule a WS push from any thread — safe to call from Qt or asyncio."""
    if _event_loop and not _event_loop.is_closed():
        _event_loop.call_soon_threadsafe(
            lambda: _event_loop.create_task(_broadcast_queue_state())
        )


def _require_auth(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials, Depends(security)],
):
    from server.auth import verify_jwt, get_mod_dpop_jwk
    username = verify_jwt(creds.credentials, _jwt_secret)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # DPoP verification (Channel B)
    from server.auth import dpop_grace_active
    jwk = get_mod_dpop_jwk(username)
    if jwk is None:
        # No JWK registered — only allow during the startup grace window.
        if not dpop_grace_active():
            raise HTTPException(status_code=401, detail="DPoP registration required")
    else:
        proof = request.headers.get("DPoP", "")
        if not proof:
            raise HTTPException(status_code=401, detail="DPoP proof required")
        from dpop_utils import verify_proof
        htu = str(request.url).split("?")[0]
        if not verify_proof(proof, request.method, htu, jwk):
            raise HTTPException(status_code=401, detail="Invalid DPoP proof")

    return username


# ── Models ─────────────────────────────────────────────────────────────────────

class SkipRequest(BaseModel):
    track_id: Optional[str] = None  # idempotency guard: reject if current doesn't match


class RemoveRequest(BaseModel):
    track_id: str  # always idempotent — remove only if the track is still queued


class MoveRequest(BaseModel):
    track_id: str
    position: int


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/ws-ticket")
async def get_ws_ticket(username: str = Depends(_require_auth)):
    import secrets as _sec
    ticket = _sec.token_urlsafe(24)
    now = _time.monotonic()
    _WS_TICKETS[ticket] = (username, now + _WS_TICKET_TTL)
    # Prune expired tickets while we're here
    expired = [t for t, (_, exp) in list(_WS_TICKETS.items()) if now > exp]
    for t in expired:
        _WS_TICKETS.pop(t, None)
    return {"ticket": ticket}


@router.get("")
async def get_queue(username: str = Depends(_require_auth)):
    return _queue_manager.to_dict()


@router.get("/history")
async def get_history(username: str = Depends(_require_auth)):
    return {"history": [_queue_manager._track_dict(t) for t in _queue_manager.history_snapshot()]}


@router.post("/skip")
async def skip_track(
    req: SkipRequest = Body(default=SkipRequest()),
    username: str = Depends(_require_auth),
):
    current = _queue_manager.current

    # Idempotency: if the mod told us what song it was skipping and it's already gone,
    # return the actual state so the panel can resync rather than skipping a ghost.
    if req.track_id and (current is None or current.id != req.track_id):
        log_action("skip", username, {
            "track_id": req.track_id,
            "outcome": "track_mismatch",
            "actual_id": current.id if current else None,
        })
        return JSONResponse(_state_payload("track_mismatch"), status_code=409)

    # Capture what was playing BEFORE the skip so we can return / log it.
    was_playing = current

    # Let the engine handle pop + play atomically.  The engine calls
    # queue_manager.pop_next() internally — do NOT call _queue_manager.skip()
    # here or the queue gets double-popped and the next track never plays.
    if _skip_callback:
        _skip_callback()

    log_action("skip", username, {
        "track_id": was_playing.id if was_playing else None,
        "track_title": was_playing.display_title() if was_playing else None,
        "outcome": "ok",
    })
    return {"skipped": _queue_manager._track_dict(was_playing)}


class PauseRequestsRequest(BaseModel):
    paused: bool


@router.post("/pause-requests")
async def pause_requests_toggle(
    req: PauseRequestsRequest,
    username: str = Depends(_require_auth),
):
    if _pause_requests_callback:
        _pause_requests_callback(req.paused)
    action = "pause_requests" if req.paused else "resume_requests"
    log_action(action, username, {"outcome": "ok"})
    return _state_payload()


class PlayPauseRequest(BaseModel):
    expected_state: str  # "PLAYING" or "PAUSED" — what the client saw when it clicked


@router.post("/playpause")
async def play_pause(req: PlayPauseRequest, username: str = Depends(_require_auth)):
    actual = _player_state()
    if actual != req.expected_state:
        # Another mod (or the streamer) already changed state — collapse the
        # duplicate into a no-op and let the 409 body resync this panel.
        return JSONResponse(_state_payload("state_mismatch"), status_code=409)

    if _playpause_callback:
        _playpause_callback()

    action = "pause" if req.expected_state == "PLAYING" else "play"
    log_action(action, username, {"outcome": "ok"})
    return {"ok": True}


class PrevRequest(BaseModel):
    track_id: Optional[str] = None  # what the client thought was playing; None = nothing


@router.post("/prev")
async def prev_track(
    req: PrevRequest = Body(default=PrevRequest()),
    username: str = Depends(_require_auth),
):
    current = _queue_manager.current

    # Idempotency: if the client told us what was playing and it has already
    # changed (concurrent skip), resync rather than acting on stale state.
    if req.track_id and (current is None or current.id != req.track_id):
        log_action("prev", username, {
            "track_id": req.track_id,
            "outcome": "track_mismatch",
            "actual_id": current.id if current else None,
        })
        return JSONResponse(_state_payload("track_mismatch"), status_code=409)

    elapsed, _ = _position_getter() if _position_getter else (0.0, 0.0)

    if current is not None and elapsed > 3.0:
        # Seek to the beginning of the current track.
        # Pass -elapsed as a relative delta; the seek callback re-reads position
        # on the main thread so the landing point is ≈ 0 s.
        if _seek_callback:
            _seek_callback(-elapsed)
        mode = "restart"
    else:
        # Go back one track in history.
        if _prev_callback:
            _prev_callback()
        mode = "prev_track"

    log_action("prev", username, {
        "track_id": current.id if current else None,
        "track_title": current.display_title() if current else None,
        "outcome": "ok",
        "mode": mode,
    })
    return {"ok": True}


class SeekRequest(BaseModel):
    delta: float = Field(..., ge=-600.0, le=600.0)  # ±10 min max; clamps abuse
    track_id: str  # which track the client was showing; reject if track changed


@router.post("/seek")
async def seek_track(req: SeekRequest, username: str = Depends(_require_auth)):
    current = _queue_manager.current
    if current is None or current.id != req.track_id:
        # Track changed between the click and the request — stale seek, drop it.
        return JSONResponse(_state_payload("track_mismatch"), status_code=409)

    if _seek_callback:
        _seek_callback(req.delta)

    log_action(
        "seek", username,
        {
            "track_id": req.track_id,
            "track_title": current.display_title(),
            "delta": req.delta,
            "outcome": "ok",
        },
    )
    return {"ok": True}


@router.post("/remove")
async def remove_track(
    req: RemoveRequest,
    username: str = Depends(_require_auth),
):
    """Remove a queued track by its stable track_id (idempotent, position-safe)."""
    tracks = _queue_manager.snapshot()
    track = next((t for t in tracks if t.id == req.track_id), None)
    if track is None:
        return JSONResponse(_state_payload("track_not_found"), status_code=409)

    _queue_manager.remove(req.track_id)
    log_action("remove", username, {
        "track_id": req.track_id,
        "track_title": track.display_title(),
        "outcome": "ok",
    })
    return {"removed": _queue_manager._track_dict(track)}


@router.delete("/{position}")
async def remove_by_position(position: int, username: str = Depends(_require_auth)):
    """Legacy position-based remove — kept for backwards compat."""
    tracks = _queue_manager.snapshot()
    idx = position - 1
    if idx < 0 or idx >= len(tracks):
        raise HTTPException(status_code=404, detail="Position out of range")
    removed = tracks[idx]
    _queue_manager.remove(removed.id)
    return {"removed": _queue_manager._track_dict(removed)}


@router.post("/move")
async def move_track(req: MoveRequest, username: str = Depends(_require_auth)):
    ok = _queue_manager.move(req.track_id, req.position)
    if not ok:
        raise HTTPException(status_code=404, detail="Track not found")
    return {"ok": True}


# ── WebSocket live feed ─────────────────────────────────────────────────────────

@router.websocket("/live")
async def queue_live(websocket: WebSocket):
    # Authenticate via a one-time ticket fetched by the client from GET /queue/ws-ticket.
    # Browsers cannot send Authorization headers on a WebSocket upgrade, so a
    # short-lived ticket (30s TTL, single-use) is the correct mechanism.
    ticket = websocket.query_params.get("ticket", "")
    entry = _WS_TICKETS.pop(ticket, None)
    if not entry or _time.monotonic() > entry[1]:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        # Push current state immediately on connect
        data = _queue_manager.to_dict()
        data["log"] = list(_action_log)[-20:]
        data["state"] = _player_state()
        await websocket.send_json(data)
        # Hold connection open — server pushes all updates
        while True:
            await websocket.receive_text()  # ignore client pings, just keep alive
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        import traceback
        print(f"[WS queue_live] unhandled exception: {exc!r}")
        traceback.print_exc()
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


def _player_state() -> str:
    return _player_state_getter() if _player_state_getter else "STOPPED"


async def _broadcast_queue_state() -> None:
    if not _ws_clients:
        return
    data = _queue_manager.to_dict()
    data["log"] = list(_action_log)[-20:]
    data["state"] = _player_state()
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
