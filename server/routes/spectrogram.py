"""
Spectrogram server routes — Browser Source edition.

OBS (or any browser) adds a Browser Source pointing at:
    http://localhost:<port>/spectrogram/overlay?preset=<Name>

The page connects back via WebSocket to receive real-time bar data,
then renders everything in a <canvas> element.

Endpoints
---------
  GET  /spectrogram/overlay                → HTML5 Canvas overlay page
  GET  /spectrogram/config/{preset_name}   → preset JSON (initial config)
  WS   /spectrogram/ws/{preset_name}       → real-time bar stream
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time as _time
from dataclasses import asdict

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/spectrogram", tags=["spectrogram"])

# Module-level state — initialised by init() at server startup
_cfg = None                                           # AppConfig
_broadcasters: dict = {}                              # str → PresetBroadcaster
_broadcasters_lock = threading.Lock()
_last_push_time: float = 0.0


# ── PresetBroadcaster ──────────────────────────────────────────────────────────

class PresetBroadcaster:
    """Manages all WebSocket clients subscribed to a single named preset.

    push_frame() is called from the audio thread (via FFTPipeline callback)
    and must never block.  Each client has its own bounded Queue; if the
    queue is full the oldest frame is dropped to keep latency low.
    """

    def __init__(self, preset_cfg) -> None:
        self.preset_cfg = preset_cfg
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    # ── Client lifecycle ──────────────────────────────────────────────────────

    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._clients.append(q)
        return q

    def remove_client(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    @property
    def has_clients(self) -> bool:
        with self._lock:
            return bool(self._clients)

    # ── Frame push ────────────────────────────────────────────────────────────

    def push_frame(self, bars: np.ndarray) -> None:
        """Resample bars to this preset's bar_count; fan out to all clients."""
        n = self.preset_cfg.bar_count
        if len(bars) != n:
            bars = np.interp(
                np.linspace(0, len(bars) - 1, n),
                np.arange(len(bars)),
                bars,
            )
        payload = json.dumps({"bars": bars.tolist()})

        with self._lock:
            clients = list(self._clients)

        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Drop oldest frame to keep latency low
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def push_config(self) -> None:
        """Push updated preset config to all connected clients.

        Called when settings change so the browser overlay updates live
        without a page reload.
        """
        payload = json.dumps({"config": asdict(self.preset_cfg)})
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            # Best-effort; don't drop bar frames just to fit a config update
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def initial_config_payload(self) -> str:
        """JSON string for the first message sent to a new WS client."""
        return json.dumps({"config": asdict(self.preset_cfg)})


# ── Module-level API (called from server startup + main.py) ───────────────────

def init(cfg) -> None:
    """Initialise with the full AppConfig.  Called by server startup."""
    global _cfg
    _cfg = cfg
    _sync_broadcasters()


def sync_presets(cfg) -> None:
    """Apply updated AppConfig (e.g. after settings dialog Apply).

    Creates broadcasters for new presets, removes stale ones, and updates
    the preset_cfg reference on existing broadcasters so push_frame uses
    the correct bar_count.
    """
    global _cfg
    _cfg = cfg
    _sync_broadcasters()


def push_bars_all(bars: np.ndarray) -> None:
    """Fan out FFT bars to all preset broadcasters that have active clients.

    Called at FFT frame rate from the audio thread.  No-op when no browser
    sources are connected (zero-cost when OBS is not capturing).
    Rate-limited to _PUSH_MIN_INTERVAL so CEF doesn't receive more GPU
    compositor frames than it can handle while a game is running.
    """
    global _last_push_time
    with _broadcasters_lock:
        active = [b for b in _broadcasters.values() if b.has_clients]
    if not active:
        return
    fps = max(1, _cfg.overlay_fps) if _cfg is not None else 30
    now = _time.monotonic()
    if now - _last_push_time < 1.0 / fps:
        return
    _last_push_time = now
    for broadcaster in active:
        broadcaster.push_frame(bars)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _sync_broadcasters() -> None:
    """Create/remove/update broadcasters to match cfg.spectrogram_presets.

    Broadcasters whose preset config changed get a push_config() call so all
    currently-connected browser sources update their rendering settings live.
    """
    if _cfg is None:
        return
    to_notify: list[PresetBroadcaster] = []
    with _broadcasters_lock:
        current_names = {p.name for p in _cfg.spectrogram_presets}
        for p in _cfg.spectrogram_presets:
            if p.name not in _broadcasters:
                _broadcasters[p.name] = PresetBroadcaster(p)
            else:
                # Update the preset_cfg reference so bar_count / settings are live
                _broadcasters[p.name].preset_cfg = p
                to_notify.append(_broadcasters[p.name])
        # Remove stale broadcasters (deleted presets)
        for name in list(_broadcasters.keys()):
            if name not in current_names:
                del _broadcasters[name]
    # Push updated config outside the lock (put_nowait is fast/non-blocking)
    for bc in to_notify:
        if bc.has_clients:
            bc.push_config()


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

@router.get("/overlay", response_class=HTMLResponse)
async def overlay_page():
    """Serves the HTML5 Canvas spectrogram overlay page."""
    html_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "static", "spectrogram_overlay.html")
    )
    with open(html_path, "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@router.get("/config/{preset_name}")
async def get_preset_config(preset_name: str):
    """Returns the SpectrogramConfig for the requested preset as JSON."""
    if _cfg is None:
        return JSONResponse({"error": "server not ready"}, status_code=503)
    preset = _cfg.get_preset(preset_name)
    if preset is None:
        # Fall back to first available preset rather than hard 404
        if _cfg.spectrogram_presets:
            preset = _cfg.spectrogram_presets[0]
        else:
            return JSONResponse({"error": "no presets configured"}, status_code=404)
    return JSONResponse(asdict(preset))


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@router.websocket("/ws/{preset_name}")
async def spectrogram_ws(websocket: WebSocket, preset_name: str):
    """Stream bar data to a browser source for the named preset.

    Sends the preset config as the first message so the browser overlay can
    initialise without a separate HTTP request.  Returns 1008 (Policy
    Violation) when the preset does not exist — the overlay shows an error
    rather than silently rendering the wrong preset.
    """
    await websocket.accept()

    with _broadcasters_lock:
        broadcaster = _broadcasters.get(preset_name)

    if broadcaster is None:
        # Preset was deleted or never existed
        await websocket.close(code=1008)
        return

    # Push initial config so the overlay configures itself immediately
    await websocket.send_text(broadcaster.initial_config_payload())

    client_q = broadcaster.add_client()
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        # Keep-alive ticker so OBS doesn't time out when music is paused
        _KEEPALIVE_INTERVAL = 5.0   # seconds
        _last_send = asyncio.get_event_loop().time()

        while True:
            try:
                payload = await loop.run_in_executor(
                    None, lambda: client_q.get(timeout=0.5)
                )
                await websocket.send_text(payload)
                _last_send = asyncio.get_event_loop().time()
            except queue.Empty:
                now = asyncio.get_event_loop().time()
                if now - _last_send >= _KEEPALIVE_INTERVAL:
                    try:
                        await websocket.send_text('{"ping":1}')
                        _last_send = now
                    except Exception:
                        break
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.remove_client(client_q)
