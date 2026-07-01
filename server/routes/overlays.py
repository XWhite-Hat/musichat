"""
OBS browser-source overlay routes.

All endpoints live on the main server port (cfg.server.port) so OBS can
point a single Browser Source at localhost without needing to know about
the separate settings port.

Endpoints
---------
  GET  /overlay/nowplaying               → transparent HTML overlay (OBS source)
  WS   /overlay/nowplaying/ws            → real-time now-playing text stream
  GET  /albumart/square                  → 512×512 JPEG of current track thumbnail
  GET  /albumart/circle                  → 512×512 PNG, circle-cropped (transparent bg)

Template variables for nowplaying_template
------------------------------------------
  {title}        track title
  {artist}       artist name (empty if unknown)
  {display}      "{artist} — {title}" or just title when no artist
  {requested_by} Twitch username of requester, or empty string
  {source}       youtube | soundcloud | local
  {duration}     mm:ss formatted duration
"""

from __future__ import annotations

import asyncio
import io
import queue
import re
import threading
from typing import Optional

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

router = APIRouter(tags=["overlays"])

# ── Module-level state ─────────────────────────────────────────────────────────

_cfg           = None   # AppConfig
_queue_manager = None   # QueueManager

# Current track and its pre-rendered text — updated on every track change.
_current_track = None   # Track object (or None when idle)
_current_text: str = ""

# All active nowplaying WS clients
_np_clients: list[queue.Queue] = []
_np_lock = threading.Lock()

# Event loop reference — needed to schedule async fan-out from Qt/engine thread
_loop: Optional[asyncio.AbstractEventLoop] = None

# Simple in-process thumbnail cache: thumbnail_url → raw image bytes
_thumb_cache: dict[str, bytes] = {}
_CACHE_MAX = 20   # keep at most the last N thumbnails


# ── Init ───────────────────────────────────────────────────────────────────────

def init(cfg, queue_manager: object) -> None:
    """Called by server/app.py at startup."""
    global _cfg, _queue_manager
    _cfg           = cfg
    _queue_manager = queue_manager


# ── Public API (called from main.py) ──────────────────────────────────────────

def notify_track_changed(track) -> None:
    """
    Called (from any thread) when a new track starts playing.
    Updates the now-playing text and fans it out to all connected WS clients.
    """
    global _current_track, _current_text
    _current_track = track
    _current_text  = _format_track(track)

    payload = _current_text

    # Schedule fan-out onto the server event loop (thread-safe)
    global _loop
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast_nowplaying(payload), _loop)


def clear_now_playing() -> None:
    """Called when playback stops — clears the overlay."""
    global _current_track, _current_text
    _current_track = None
    _current_text  = ""
    global _loop
    if _loop and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast_nowplaying(""), _loop)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_track(track) -> str:
    if track is None:
        return ""

    # Pick the right template: viewer-requested songs get their own layout so
    # "Requested by: " never appears on streamer-played tracks (and vice versa).
    has_request = bool(getattr(track, "requested_by", ""))
    template = ""
    if _cfg is not None:
        try:
            if has_request:
                template = (
                    getattr(_cfg.overlay, "nowplaying_template_requested", "")
                    or _cfg.overlay.nowplaying_template
                )
            else:
                template = _cfg.overlay.nowplaying_template
        except AttributeError:
            pass
    if not template:
        template = "{display}"

    d = track.duration_seconds
    mins, secs = divmod(max(d, 0), 60)

    # Merge artist credits embedded in the title with the track's metadata
    # artist.  "KoruSe, mzmff - Two Different Worlds" with artist "KoruSe"
    # → display_artist="KoruSe, mzmff", clean_title="Two Different Worlds".
    try:
        from player.queue_manager import _merge_credited_artists
        display_artist, clean_title = _merge_credited_artists(
            track.title or "", track.artist or ""
        )
    except Exception:
        display_artist = track.artist or ""
        clean_title    = track.title  or ""

    display = f"{display_artist} — {clean_title}" if display_artist else clean_title

    kwargs = dict(
        title        = clean_title,
        artist       = display_artist,   # full merged credit (e.g. "KoruSe, mzmff")
        display      = display,
        requested_by = track.requested_by or "",
        source       = track.source.name.lower() if hasattr(track.source, "name") else "",
        duration     = f"{mins}:{secs:02d}",
    )
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError):
        # Malformed or unknown placeholder in the template — fall back gracefully
        # rather than crashing the on_track_started chain.
        return kwargs["display"]


async def _broadcast_nowplaying(text: str) -> None:
    with _np_lock:
        clients = list(_np_clients)
    import json
    payload = json.dumps({"text": text})
    for q in clients:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def _overlay_cfg_dict() -> dict:
    """Return the text-overlay display config as a plain dict for the overlay page."""
    if _cfg is None:
        return {}
    ov = _cfg.overlay
    return {
        "text_font_size":   getattr(ov, "text_font_size",   22),
        "text_width":       getattr(ov, "text_width",       600),
        "text_color":       getattr(ov, "text_color",       "#ffffff"),
        "text_scroll":      getattr(ov, "text_scroll",      False),
        "text_font":        getattr(ov, "text_font",        "Share Tech Mono"),
        "text_font_import": getattr(ov, "text_font_import", ""),
    }


def push_config() -> None:
    """Push updated overlay display config to all connected overlay WS clients.

    Call this (from any thread) whenever the overlay.* config fields change so
    the browser source updates live without a page reload.
    """
    global _loop
    if _loop and not _loop.is_closed():
        import json
        payload = json.dumps({"overlay_cfg": _overlay_cfg_dict()})
        asyncio.run_coroutine_threadsafe(_broadcast_raw(payload), _loop)


async def _broadcast_raw(payload: str) -> None:
    with _np_lock:
        clients = list(_np_clients)
    for q in clients:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

_NP_OVERLAY_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { background: transparent; overflow: hidden; }

    /* Outer clip: fixed width, overflow hidden so text never leaks */
    #outer {
      width: 600px;           /* overwritten by applyConfig() */
      overflow: hidden;
      padding: 6px 12px;
      text-align: center;
      white-space: nowrap;
      user-select: none;
    }

    /* Inner text: inline-block so we can measure its true rendered width */
    #text {
      display: inline-block;
      white-space: nowrap;
      font-family: 'Share Tech Mono', 'Courier New', monospace;
      font-size: 22px;        /* overwritten by applyConfig() */
      color: #ffffff;         /* overwritten by applyConfig() */
      text-shadow: 0 1px 6px rgba(0,0,0,.9), 0 0 2px rgba(0,0,0,.7);
      opacity: 1;
      transition: opacity .3s;
    }
    #text.fade { opacity: 0; }
  </style>
</head>
<body>
  <div id="outer"><span id="text"></span></div>
  <script>
    'use strict';
    const outerEl = document.getElementById('outer');
    const textEl  = document.getElementById('text');
    let current       = '';
    let scrollEnabled = false;
    let scrollTimer   = null;
    let retryDelay    = 1000;

    // ── Config ────────────────────────────────────────────────────────────────

    function applyConfig(cfg) {
      // Inject font — either a remote CSS stylesheet (Google Fonts) or a local
      // font embedded as a data URI (@font-face).  The two paths are mutually
      // exclusive so we clean up whichever element type is no longer needed.
      if (cfg.text_font_import) {
        const imp = cfg.text_font_import;
        if (imp.startsWith('data:')) {
          // Local font: inject a @font-face rule via a <style> element.
          document.getElementById('_font_import')?.remove();
          let st = document.getElementById('_font_face');
          if (!st) {
            st = document.createElement('style');
            st.id = '_font_face';
            document.head.appendChild(st);
          }
          const fam = (cfg.text_font || 'CustomFont').replace(/'/g, "\\'");
          const rule = `@font-face { font-family: '${fam}'; src: url('${imp}'); }`;
          if (st.textContent !== rule) st.textContent = rule;
        } else {
          // Remote CSS stylesheet (Google Fonts etc.): inject a <link>.
          document.getElementById('_font_face')?.remove();
          let link = document.getElementById('_font_import');
          if (!link) {
            link = document.createElement('link');
            link.id  = '_font_import';
            link.rel = 'stylesheet';
            document.head.appendChild(link);
          }
          if (link.href !== imp) link.href = imp;
        }
      }

      const fontSize = cfg.text_font_size || 22;
      const width    = cfg.text_width     || 600;

      textEl.style.fontFamily = (cfg.text_font || 'Share Tech Mono') +
                                ", 'Courier New', monospace";
      textEl.style.fontSize   = fontSize + 'px';
      textEl.style.color      = cfg.text_color || '#ffffff';
      outerEl.style.width     = width + 'px';

      scrollEnabled = !!cfg.text_scroll;
      if (!scrollEnabled) cancelScroll();
      else if (current) scheduleScroll();
    }

    // ── Scroll ────────────────────────────────────────────────────────────────

    function cancelScroll() {
      if (scrollTimer) { clearTimeout(scrollTimer); scrollTimer = null; }
      textEl.style.transition = 'none';
      textEl.style.transform  = 'translateX(0)';
    }

    function scheduleScroll() {
      if (!scrollEnabled) return;
      if (scrollTimer) clearTimeout(scrollTimer);
      scrollTimer = setTimeout(doScroll, 10000);
    }

    function doScroll() {
      scrollTimer = null;
      // Measure real overflow (subtract the 2×12 px side padding from outer)
      const available = outerEl.clientWidth - 24;
      const overflow  = textEl.scrollWidth - available;
      if (overflow <= 4) { scheduleScroll(); return; }   // fits — nothing to do

      const speed   = 60;  // px per second
      const scrollT = Math.round((overflow / speed) * 1000);

      // Scroll left to reveal the end of the string
      textEl.style.transition = 'transform ' + scrollT + 'ms linear';
      textEl.style.transform  = 'translateX(-' + overflow + 'px)';

      // After arrival: pause 1.5 s, then ease back to start
      scrollTimer = setTimeout(() => {
        textEl.style.transition = 'transform 600ms ease';
        textEl.style.transform  = 'translateX(0)';
        scrollTimer = setTimeout(scheduleScroll, 600);
      }, scrollT + 1500);
    }

    // ── Text updates ──────────────────────────────────────────────────────────

    function setText(newText) {
      if (newText === current) return;
      cancelScroll();
      textEl.classList.add('fade');
      setTimeout(() => {
        current = newText;
        textEl.textContent = current;
        textEl.classList.remove('fade');
        scheduleScroll();
      }, 300);
    }

    // ── WebSocket ─────────────────────────────────────────────────────────────

    function connect() {
      const ws = new WebSocket('ws://' + location.host + '/overlay/nowplaying/ws');

      ws.onopen = () => { retryDelay = 1000; };

      ws.onmessage = e => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.overlay_cfg) applyConfig(msg.overlay_cfg);
          if (msg.text !== undefined) setText(msg.text);
        } catch(_) {}
      };

      ws.onclose = () => {
        retryDelay = Math.min(retryDelay * 2, 30000);
        setTimeout(connect, retryDelay);
      };
    }

    connect();
  </script>
</body>
</html>
"""


@router.get("/overlay/nowplaying", response_class=HTMLResponse)
async def nowplaying_overlay():
    """Transparent HTML overlay — add as OBS Browser Source."""
    global _loop
    _loop = asyncio.get_event_loop()
    return HTMLResponse(_NP_OVERLAY_HTML)


@router.get("/overlay/nowplaying/config")
async def nowplaying_config():
    """
    Returns the current nowplaying template, track text, and display config.

    The text is re-rendered on every call so that saving a new template in the
    settings UI immediately reflects in the preview — no need to wait for the
    next track-change event.  overlay_cfg is included so the settings page can
    show the live-preview and OBS dimensions without a separate request.
    """
    template = _cfg.overlay.nowplaying_template if _cfg else "{display}"
    template_requested = (
        getattr(_cfg.overlay, "nowplaying_template_requested", "")
        if _cfg else ""
    )
    current = _format_track(_current_track) if _current_track else _current_text
    return JSONResponse({
        "template":           template,
        "template_requested": template_requested,
        "current":            current,
        "overlay_cfg":        _overlay_cfg_dict(),
    })


# ── WebSocket ──────────────────────────────────────────────────────────────────

@router.websocket("/overlay/nowplaying/ws")
async def nowplaying_ws(ws: WebSocket):
    """Real-time now-playing text stream for the overlay page."""
    global _loop
    _loop = asyncio.get_event_loop()

    await ws.accept()

    import json
    client_q: queue.Queue = queue.Queue(maxsize=4)
    with _np_lock:
        _np_clients.append(client_q)

    # Send current state + display config immediately so the overlay
    # configures itself and isn't blank on connect
    await ws.send_text(json.dumps({
        "text":        _current_text,
        "overlay_cfg": _overlay_cfg_dict(),
    }))

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                payload = await loop.run_in_executor(
                    None, lambda: client_q.get(timeout=10.0)
                )
                await ws.send_text(payload)
            except queue.Empty:
                # Keepalive ping so OBS doesn't drop the connection
                try:
                    await ws.send_text('{"ping":1}')
                except Exception:
                    break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        with _np_lock:
            try:
                _np_clients.remove(client_q)
            except ValueError:
                pass


# ── Album art ──────────────────────────────────────────────────────────────────

# HTML wrapper served to OBS browser sources.
# A raw PNG URL loaded directly in Chromium renders against a white page
# background, making alpha channels invisible.  Wrapping it in an HTML page
# with `background: transparent` fixes that, and the WS listener ensures OBS
# updates its cache immediately when the track changes.
def _albumart_html(shape: str) -> str:
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html, body {{
      background: transparent;
      overflow: hidden;
      width: 100%; height: 100%;
    }}
    img {{
      display: block;
      width: 100%; height: 100%;
      object-fit: contain;
    }}
  </style>
</head>
<body>
  <img id="art" src="" style="display:none">
  <script>
    const shape = {shape!r};
    const img   = document.getElementById('art');
    let retryDelay = 1000;

    function refresh() {{
      img.src = '/albumart/' + shape + '/raw?t=' + Date.now();
      img.style.display = 'block';
    }}

    function hide() {{
      img.style.display = 'none';
      img.src = '';
    }}

    function connect() {{
      const ws = new WebSocket('ws://' + location.host + '/overlay/nowplaying/ws');
      ws.onopen    = () => {{ retryDelay = 1000; }};
      ws.onmessage = e => {{
        try {{
          const msg = JSON.parse(e.data);
          if ('text' in msg) {{
            // Empty text = track stopped/nothing playing — hide the element
            // completely so a transparent image doesn't appear as a black dot.
            // Non-empty text = track started — fetch fresh art and show it.
            if (msg.text === '') hide();
            else refresh();
          }}
        }} catch (_) {{}}
      }};
      ws.onclose = () => {{
        retryDelay = Math.min(retryDelay * 2, 30_000);
        setTimeout(connect, retryDelay);
      }};
    }}

    // The WS server immediately sends the current state when a client connects,
    // so we don't need an eager refresh() here — the first WS message handles it.
    connect();
  </script>
</body>
</html>
"""

def _youtube_fallback_urls(url: str) -> list[str]:
    """
    If *url* is a YouTube thumbnail URL (ytimg.com), return lower-quality but
    guaranteed-present fallback candidates in preference order.

    yt-dlp often picks the "best" thumbnail (maxresdefault.jpg) which can 404
    for videos that have no high-res still.  hqdefault.jpg (480×360) is always
    available for any public video.
    """
    if "ytimg.com" not in url:
        return []
    m = re.search(r"/vi(?:_webp)?/([a-zA-Z0-9_-]+)/", url)
    if not m:
        return []
    vid = m.group(1)
    return [
        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
    ]


async def _fetch_thumbnail(url: str) -> Optional[bytes]:
    """
    Fetch thumbnail bytes with an in-process LRU cache.

    For YouTube thumbnails the yt-dlp "best" pick (often maxresdefault.jpg)
    can 404.  We automatically fall through to lower-quality but
    guaranteed-present alternatives (hqdefault → sddefault → mqdefault).
    """
    if not url:
        return None
    if url in _thumb_cache:
        return _thumb_cache[url]

    # Build ordered candidate list: primary + YouTube fallbacks (deduped)
    candidates: list[str] = [url]
    for fb in _youtube_fallback_urls(url):
        if fb not in candidates:
            candidates.append(fb)

    async with httpx.AsyncClient(timeout=8.0) as client:
        for candidate in candidates:
            if candidate in _thumb_cache:
                data = _thumb_cache[candidate]
                _thumb_cache[url] = data   # promote alias
                return data
            try:
                resp = await client.get(
                    candidate, headers={"User-Agent": "StreamDeck-Music/1.0"}
                )
                if resp.status_code == 200:
                    data = resp.content
                    # Trim LRU cache
                    while len(_thumb_cache) >= _CACHE_MAX:
                        _thumb_cache.pop(next(iter(_thumb_cache)))
                    _thumb_cache[candidate] = data
                    _thumb_cache[url]       = data   # primary alias
                    return data
                # 404 or other non-200 → try next candidate
            except Exception as exc:
                print(f"[overlays] thumbnail fetch failed ({candidate!r}): {exc!r}")

    return None


def _placeholder_bytes() -> bytes:
    """
    Return a 1×1 fully-transparent PNG for use when Pillow is not installed.
    All real image rendering uses Pillow directly; this is purely a last resort.
    """
    # Minimal valid 1×1 RGBA PNG with alpha=0 (fully transparent).
    # CRCs were pre-computed offline and are embedded literally.
    # fmt: off
    return bytes([
        # PNG signature
        0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a,
        # IHDR: 1×1, 8-bit RGBA (colour type 6)
        0x00,0x00,0x00,0x0d, 0x49,0x48,0x44,0x52,
        0x00,0x00,0x00,0x01, 0x00,0x00,0x00,0x01,
        0x08,0x06,0x00,0x00,0x00, 0x1f,0x15,0xc4,0x89,
        # IDAT: zlib-deflate of filter-byte(0) + R=0 G=0 B=0 A=0
        0x00,0x00,0x00,0x0b, 0x49,0x44,0x41,0x54,
        0x08,0xd7,0x63,0x60,0x60,0x60,0x60,0x00,0x00,0x00,0x05,0x00,0x01,
        0xa5,0xf6,0x45,0x40,
        # IEND
        0x00,0x00,0x00,0x00, 0x49,0x45,0x4e,0x44, 0xae,0x42,0x60,0x82,
    ])
    # fmt: on


@router.get("/albumart/{shape}", response_class=HTMLResponse)
async def albumart_page(shape: str):
    """
    OBS browser-source page for album art.

    Serves an HTML page with a transparent background and a WebSocket listener
    that refreshes the image whenever the track changes.  The actual PNG is at
    /albumart/{shape}/raw.
    """
    global _loop
    _loop = asyncio.get_event_loop()
    if shape not in ("square", "circle"):
        return HTMLResponse("<h1>shape must be square or circle</h1>", status_code=400)
    return HTMLResponse(_albumart_html(shape))


@router.get("/albumart/{shape}/raw")
async def albumart_raw(shape: str):
    """
    Raw RGBA PNG of the current track's thumbnail.

    Used by the settings-page <img> previews and any direct image consumer.
    OBS browser sources should use /albumart/{shape} (HTML wrapper) instead so
    that the transparent background and auto-refresh work correctly.
    """
    if shape not in ("square", "circle"):
        return JSONResponse({"error": "shape must be 'square' or 'circle'"}, status_code=400)

    size = 512
    if _cfg is not None:
        try:
            size = int(_cfg.overlay.albumart_size)
        except (AttributeError, TypeError, ValueError):
            pass

    # Use the track stored by notify_track_changed (works for queue, play-now,
    # prev-track, history double-click — any play path that fires on_track_started)
    thumb_url = getattr(_current_track, "thumbnail_url", "") if _current_track else ""

    raw: Optional[bytes] = None
    if thumb_url:
        raw = await _fetch_thumbnail(thumb_url)

    try:
        from PIL import Image, ImageDraw
        HAS_PIL = True
    except ImportError:
        HAS_PIL = False
        # One-time warning so the user knows why art isn't showing
        if not getattr(albumart_raw, "_pil_warned", False):
            albumart_raw._pil_warned = True  # type: ignore[attr-defined]
            print(
                "[overlays] Pillow is not installed — album art unavailable. "
                "Run:  pip install Pillow"
            )

    if not HAS_PIL:
        return Response(
            content=_placeholder_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    if raw:
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        # Centre-crop to square before resizing (avoids letterbox distortion)
        w, h = img.size
        if w != h:
            side = min(w, h)
            left = (w - side) // 2
            top  = (h - side) // 2
            img  = img.crop((left, top, left + side, top + side))
        img = img.resize((size, size), Image.LANCZOS)
    else:
        # Transparent placeholder — no art available yet
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    if shape == "circle":
        # putalpha() replaces the entire alpha channel in one operation —
        # more reliable than paste+mask which can leave opaque corners when
        # the source image was originally RGB (no alpha).
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        img.putalpha(mask)

    # Both shapes are now RGBA PNG so OBS browser sources see full transparency.
    # JPEG was dropped because it has no alpha channel.
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
