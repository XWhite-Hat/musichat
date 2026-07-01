"""
Twitch EventSub WebSocket listener for channel-point redemptions.

No inbound port or HTTPS endpoint required — the connection is initiated
outbound from the app to wss://eventsub.wss.twitch.tv/ws.

Flow
----
1. Connect to Twitch EventSub WS endpoint
2. Receive session_welcome  → register subscription via Helix API
   (proxied through the CF Worker so Client-Id never leaves Cloudflare)
3. Receive notification     → forward redemption metadata to TwitchBot
4. Receive session_reconnect → reconnect to new URL (Twitch rotates ~30–90 min)
5. Auto-reconnect on drops with exponential backoff
6. Receive revocation       → log reason and stop listener

Fulfill / Cancel
----------------
Redemption IDs are forwarded to TwitchBot._handle_redemption() which passes
them into main.py's on_channel_points_request callback.  main.py sets the
IDs on the Track dataclass.  TwitchBot.mark_track_started() fires FULFILLED
when the song actually plays; cancel_redemption() fires CANCELED on
!wrongsong / !whoops or when the song can't be resolved.

Scope required
--------------
channel:manage:redemptions — covers reading and updating redemption status.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from config import TwitchConfig

EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX_API       = "https://api.twitch.tv/helix"
_SUB_TYPE       = "channel.channel_points_custom_reward_redemption.add"


class ChannelPointsListener:
    """
    Async EventSub WebSocket listener.

    Instantiate once per bot session.  Schedule ``run()`` as a task inside
    the bot's asyncio event loop; call ``stop()`` to tear it down cleanly.
    """

    def __init__(
        self,
        cfg: "TwitchConfig",
        controller,                           # TwitchBot — holds callbacks
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cfg  = cfg
        self._ctrl = controller
        self._loop = loop
        self._stop_event = asyncio.Event()

    # ── Public ─────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Long-running coroutine.  Connects, listens, and reconnects as needed.
        Returns only when stop() is called or a revocation is received.
        """
        try:
            import aiohttp
        except ImportError:
            print("[cp] aiohttp not available — channel points disabled")
            return

        backoff = 1.0
        url     = EVENTSUB_WS_URL

        while not self._stop_event.is_set():
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(
                        url,
                        heartbeat=20,
                        receive_timeout=None,
                    ) as ws:
                        print(f"[cp] EventSub WS connected ({url[:72]})")
                        backoff = 1.0  # reset on successful connect
                        reconnect_url = await self._pump(ws, http)
                        if reconnect_url:
                            url = reconnect_url
                            print(f"[cp] session_reconnect → {url[:72]}")
                            continue
                        if self._stop_event.is_set():
                            break   # clean exit — stop() called or revocation
                        # WS closed without a reconnect URL — fall through to
                        # the backoff/retry path by raising so the except block fires.
                        raise RuntimeError("EventSub WS closed unexpectedly")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                print(f"[cp] WS error: {exc!r} — reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 120.0)
                url = EVENTSUB_WS_URL  # reset to default on error

        print("[cp] listener stopped")

    def stop(self) -> None:
        """Signal the run() loop to exit cleanly."""
        self._stop_event.set()

    # ── Message pump ───────────────────────────────────────────────────────────

    async def _pump(
        self,
        ws,           # aiohttp ClientWebSocketResponse
        http,         # aiohttp ClientSession (for subscribe calls)
    ) -> Optional[str]:
        """
        Drive the WebSocket message loop until the connection closes.

        Returns a reconnect URL if Twitch requests one, None otherwise.
        """
        import aiohttp

        keepalive_timeout: float = 20.0   # updated from session_welcome
        last_msg: float = time.monotonic()

        while not self._stop_event.is_set():
            if time.monotonic() - last_msg > keepalive_timeout + 5:
                print("[cp] keepalive timeout — dropping and reconnecting")
                return None

            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            if msg.type in (
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                print(f"[cp] WS closed: {msg.type.name}")
                return None

            if msg.type != aiohttp.WSMsgType.TEXT:
                continue

            last_msg = time.monotonic()

            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            msg_type = data.get("metadata", {}).get("message_type", "")

            if msg_type == "session_welcome":
                sess              = data["payload"]["session"]
                session_id        = sess["id"]
                keepalive_timeout = float(sess.get("keepalive_timeout_seconds", 15))
                print(f"[cp] session_welcome id={session_id[:16]}… "
                      f"keepalive={keepalive_timeout}s")
                asyncio.ensure_future(self._subscribe(http, session_id))

            elif msg_type == "session_keepalive":
                pass  # timestamp already reset above

            elif msg_type == "session_reconnect":
                reconnect_url = (
                    data.get("payload", {})
                        .get("session", {})
                        .get("reconnect_url", "")
                )
                return reconnect_url or EVENTSUB_WS_URL

            elif msg_type == "notification":
                sub_type = (
                    data.get("payload", {})
                        .get("subscription", {})
                        .get("type", "")
                )
                if sub_type == _SUB_TYPE:
                    event = data["payload"]["event"]
                    self._handle_event(event)

            elif msg_type == "revocation":
                status = (
                    data.get("payload", {})
                        .get("subscription", {})
                        .get("status", "unknown")
                )
                print(f"[cp] subscription revoked — status={status!r}")
                self._stop_event.set()
                return None

        return None

    # ── Subscription ───────────────────────────────────────────────────────────

    async def _subscribe(self, http, session_id: str) -> None:
        """
        Register the EventSub subscription pointing at our WS session.

        Proxied mode : POST /eventsub-subscribe on the CF Worker (Client-Id
                       stays server-side; never in the binary).
        BYOI mode    : POST directly to Helix with the user's own client_id.

        Uses a dedicated ClientSession so the WS session closing (e.g. the
        pump returning before this POST completes) cannot interrupt the HTTP
        request and cause a spurious ServerDisconnectedError.
        """
        import aiohttp as _aiohttp
        from constants import TWITCH_WORKER_URL, is_byoi_mode

        broadcaster_id = self._cfg.streamer_id
        access_token   = self._cfg.streamer_token
        reward_id      = self._cfg.channel_points_reward_id or None

        condition: dict = {"broadcaster_user_id": broadcaster_id}
        if reward_id:
            condition["reward_id"] = reward_id

        helix_body = {
            "type":      _SUB_TYPE,
            "version":   "1",
            "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id},
        }

        try:
            async with _aiohttp.ClientSession() as sub_http:
                if is_byoi_mode():
                    from constants import TWITCH_APP_CLIENT_ID
                    resp = await sub_http.post(
                        f"{HELIX_API}/eventsub/subscriptions",
                        json=helix_body,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Client-Id":     TWITCH_APP_CLIENT_ID,
                        },
                    )
                    ok = resp.status in (200, 202, 409)  # 409 = already subscribed
                    if ok:
                        print("[cp] subscription registered (BYOI)")
                    else:
                        print(f"[cp] subscription failed {resp.status}: "
                              f"{(await resp.text())[:200]}")
                else:
                    import dpop_utils as _dpop
                    _url = f"{TWITCH_WORKER_URL}/eventsub-subscribe"
                    resp = await sub_http.post(
                        _url,
                        json={
                            "access_token":   access_token,
                            "broadcaster_id": broadcaster_id,
                            "session_id":     session_id,
                            "reward_id":      reward_id,
                        },
                        headers=_dpop.dpop_header("POST", _url, access_token),
                    )
                    ok = resp.status in (200, 202, 409)
                    if ok:
                        print("[cp] subscription registered (proxied)")
                    else:
                        print(f"[cp] proxied subscription failed {resp.status}: "
                              f"{(await resp.text())[:200]}")
        except Exception as exc:
            print(f"[cp] subscribe error: {exc!r}")

    # ── Redemption event ───────────────────────────────────────────────────────

    def _handle_event(self, event: dict) -> None:
        """
        Called for each channel.channel_points_custom_reward_redemption.add event.

        Passes redemption metadata to TwitchBot._handle_redemption() so the
        controller can attach the IDs to the Track and later FULFILL/CANCEL.
        """
        user_login    = event.get("user_login", "")
        user_input    = event.get("user_input", "").strip()
        redemption_id = event.get("id", "")
        reward        = event.get("reward", {})
        reward_id     = reward.get("id", "")
        reward_title  = reward.get("title", "?")

        print(f"[cp] redemption from @{user_login}: [{reward_title}] {user_input!r}")

        if not user_input:
            # Reward created without "Require Viewer to Enter Text" — nothing to do
            return

        # Route through the controller — no callbacks, just metadata forwarding.
        # TwitchBot owns fulfill/cancel scheduling.
        self._ctrl._handle_redemption(user_input, user_login, redemption_id, reward_id)
