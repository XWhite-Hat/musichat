"""
Twitch chatbot — built on twitchio 3.x.

Runs in a dedicated thread with its own asyncio event loop.
Communicates back to the Qt UI via registered callbacks (no direct Qt calls).

Commands
--------
  !songrequest <url|query>   add to queue
  !queue                     show current queue (up to 5 tracks)
  !skip                      skip current track (mod/broadcaster only by default)
  !currentsong               announce current track
  !wrongsong                 remove requester's last queued track

Permission tiers (skip_permission setting)
------------------------------------------
  broadcaster > mod > vip > subscriber > all
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from config import TwitchConfig
from player.queue_manager import QueueManager

try:
    import twitchio
    from twitchio.ext import commands as tw_commands
    from twitchio import eventsub as tw_eventsub
    HAS_TWITCHIO = True
except ImportError:
    HAS_TWITCHIO = False


class TwitchBot:
    def __init__(
        self,
        cfg: TwitchConfig,
        queue: QueueManager,
    ) -> None:
        self.cfg = cfg
        self.queue = queue

        # Callbacks — set by whoever needs to react (UI, resolver)
        self.on_song_request: Optional[Callable[[str, str], None]] = None
        self.on_chat_message: Optional[Callable[[str, str], None]] = None
        self.on_connecting: Optional[Callable[[str], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        # Called when a token refresh succeeds so the caller can persist the
        # updated tokens.  Signature: (account: str, access_token: str, refresh_token: str)
        self.on_token_refreshed: Optional[Callable[[str, str, str], None]] = None

        # Channel-points callback — (query, username, redemption_id, reward_id).
        self.on_channel_points_request: Optional[Callable[[str, str, str, str], None]] = None

        # Called after the bot modifies cfg (e.g. reward deleted → cleared).
        self.on_config_changed: Optional[Callable[[], None]] = None

        # Called by !wrongsong when no queued track is found for the user.
        # Signature: (username: str) -> Optional[Track]
        self.on_cancel_current_song: Optional[Callable[[str], Optional[object]]] = None

        # Called when the stored CP reward was not found on startup.
        self.on_cp_reward_deleted: Optional[Callable[[], None]] = None

        # Called when a token refresh returns reauth_required from the Worker,
        # meaning the token was revoked or replaced on another device.
        # Signature: (account: str, message: str)
        self.on_reauth_required: Optional[Callable[[str, str], None]] = None

        # Called when startup auth fails definitively (token invalid after retry).
        # Signature: (account: str)  — "bot" | "streamer"
        self.on_auth_failed: Optional[Callable[[str], None]] = None

        # Called when stream.offline fires — refund all queued CP redemptions.
        self.on_stream_offline: Optional[Callable[[], None]] = None

        # Called after tokens are confirmed fresh (refreshed or still valid),
        # before the bot connects.  Use this to sync DPoP keys or do any
        # pre-connect setup that requires a valid access token.
        self.on_tokens_ready: Optional[Callable[[], None]] = None

        # Cooldown tracking: username → last request timestamp
        self._cooldowns: dict[str, float] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._bot: Optional[object] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not HAS_TWITCHIO:
            print("[twitch] twitchio not installed — bot disabled")
            return
        effective_token = self.cfg.bot_token if self.cfg.use_separate_bot else self.cfg.streamer_token
        if not effective_token or not self.cfg.channel:
            print("[twitch] missing credentials — bot disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop and not self._loop.is_closed() and self._bot:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._bot.close(), self._loop
                )
                future.result(timeout=4.0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5.0)

    # ── Internal ───────────────────────────────────────────────────────────────

    _AUTH_ERROR_KEYWORDS = (
        "login unsuccessful", "unauthorized", "invalid token",
        "authentication failed", "token was invalid", "cannot be refreshed",
    )

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            # 1+2: Refresh both tokens upfront so every subsequent step
            #      (DPoP sync, reward check, EventSub) sees a valid token.
            self._maybe_refresh_token()

            # 3: Notify caller — good moment to sync DPoP key while tokens
            #    are freshly confirmed, before any Worker calls fire.
            if self.on_tokens_ready:
                self.on_tokens_ready()

            # 4: Verify the channel-points reward exists on Twitch.
            self._verify_reward()

            # 5: Connect to Twitch chat.  Attempt at most twice: on an auth
            #    rejection force a token re-exchange and retry once.
            _auth_failed_account: Optional[str] = None
            for _attempt in range(2):
                if _attempt > 0:
                    # Auth was rejected — zero the timestamps so
                    # _maybe_refresh_token treats them as stale and forces a
                    # fresh exchange before the second connect attempt.
                    self.cfg.streamer_token_issued_at = 0.0
                    self.cfg.bot_token_issued_at = 0.0
                    print("[twitch] auth rejection — fetching fresh tokens and retrying once")
                    time.sleep(3)
                    self._maybe_refresh_token()

                # Resolve client_id with the (now guaranteed fresh) token.
                resolved_client_id = self._resolve_client_id()
                self._bot = _Bot(self.cfg, self.queue, self,
                                 client_id=resolved_client_id)
                # Pass the user access token as the "app token" to bypass the
                # client_secret requirement in twitchio's login().  We don't use
                # twitchio's built-in token store or OAuth adapter.
                effective_token = (
                    self.cfg.bot_token if self.cfg.use_separate_bot
                    else self.cfg.streamer_token
                )
                if self.on_connecting:
                    sending_account = (
                        self.cfg.bot_username
                        if (self.cfg.use_separate_bot and self.cfg.bot_username)
                        else self.cfg.streamer_username
                    ) or ""
                    self.on_connecting(sending_account)
                try:
                    self._loop.run_until_complete(
                        self._bot.start(
                            token=effective_token,
                            with_adapter=False,
                            load_tokens=False,
                            save_tokens=False,
                        )
                    )
                    break
                except Exception as e:
                    err = str(e).lower()
                    is_auth = any(kw in err for kw in self._AUTH_ERROR_KEYWORDS)
                    if is_auth and _attempt == 0:
                        continue
                    if is_auth:
                        _auth_failed_account = "bot" if self.cfg.use_separate_bot else "streamer"
                        print(f"[twitch] {_auth_failed_account} token is invalid — re-authentication required")
                    else:
                        print(f"[twitch] bot error: {e}")
                    break

        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                self._loop.close()

            if _auth_failed_account:
                if self.on_auth_failed:
                    self.on_auth_failed(_auth_failed_account)
            elif self.on_disconnected:
                self.on_disconnected()

    def _handle_redemption(
        self,
        user_input:    str,
        user_login:    str,
        redemption_id: str,
        reward_id:     str,
    ) -> None:
        if not user_input:
            return
        if self.cfg.requests_paused:
            self._schedule_redemption_update(redemption_id, reward_id, "CANCELED")
            return
        if self.on_channel_points_request:
            self.on_channel_points_request(user_input, user_login, redemption_id, reward_id)
        elif self.on_song_request:
            self.on_song_request(user_input, user_login)

    def mark_track_started(self, track) -> None:
        """FULFILL the CP redemption after the song actually starts playing."""
        rid  = getattr(track, "redemption_id",        "")
        rwid = getattr(track, "redemption_reward_id", "")
        if rid and rwid:
            self._schedule_redemption_update(rid, rwid, "FULFILLED")

    def cancel_redemption(self, redemption_id: str, reward_id: str) -> None:
        """CANCEL (refund) a redemption.  Thread-safe."""
        if redemption_id and reward_id:
            self._schedule_redemption_update(redemption_id, reward_id, "CANCELED")

    def _schedule_redemption_update(
        self,
        redemption_id: str,
        reward_id:     str,
        status:        str,
    ) -> None:
        if not (self._loop and not self._loop.is_closed()):
            print(f"[twitch] can't {status} redemption — event loop not running")
            return
        asyncio.run_coroutine_threadsafe(
            self._do_update_redemption(redemption_id, reward_id, status),
            self._loop,
        )

    async def _do_update_redemption(
        self,
        redemption_id: str,
        reward_id:     str,
        status:        str,
    ) -> None:
        import aiohttp
        from constants import TWITCH_WORKER_URL, is_byoi_mode

        broadcaster_id = self.cfg.streamer_id
        access_token   = self.cfg.streamer_token

        try:
            async with aiohttp.ClientSession() as http:
                if is_byoi_mode():
                    from constants import TWITCH_APP_CLIENT_ID
                    client_id = self.cfg.client_id or TWITCH_APP_CLIENT_ID
                    url = (
                        f"https://api.twitch.tv/helix"
                        f"/channel_points/custom_rewards/redemptions"
                        f"?broadcaster_id={broadcaster_id}"
                        f"&id={redemption_id}"
                        f"&reward_id={reward_id}"
                    )
                    resp = await http.patch(
                        url,
                        json={"status": status},
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Client-Id":     client_id,
                        },
                    )
                else:
                    import dpop_utils as _dpop
                    _url = f"{TWITCH_WORKER_URL}/fulfill-redemption"
                    resp = await http.post(
                        _url,
                        json={
                            "access_token":   access_token,
                            "broadcaster_id": broadcaster_id,
                            "redemption_id":  redemption_id,
                            "reward_id":      reward_id,
                            "status":         status,
                        },
                        headers=_dpop.dpop_header("POST", _url, access_token),
                    )

                verb = "fulfilled" if status == "FULFILLED" else "canceled (refunded)"
                if resp.status in (200, 204):
                    print(f"[twitch] redemption {verb}: {redemption_id[:8]}…")
                else:
                    text = await resp.text()
                    print(f"[twitch] redemption update failed {resp.status}: {text[:200]}")

        except Exception as exc:
            print(f"[twitch] _do_update_redemption error: {exc!r}")

    def _verify_reward(self) -> None:
        if not (self.cfg.channel_points_enabled and self.cfg.channel_points_reward_id):
            return
        if not (self.cfg.streamer_id and self.cfg.streamer_token):
            return

        from server.auth import check_reward_exists
        from constants import is_byoi_mode

        client_id = self.cfg.client_id if is_byoi_mode() else ""

        print("[twitch] verifying channel-points reward still exists…")
        exists = check_reward_exists(
            self.cfg.streamer_id,
            self.cfg.channel_points_reward_id,
            self.cfg.streamer_token,
            client_id,
        )

        if not exists:
            print(
                "[twitch] ⚠ channel-points reward not found on Twitch — "
                "it may have been deleted from the dashboard"
            )
            self.cfg.channel_points_enabled   = False
            self.cfg.channel_points_reward_id = ""
            if self.on_config_changed:
                self.on_config_changed()
            if self.on_cp_reward_deleted:
                self.on_cp_reward_deleted()
        else:
            print("[twitch] channel-points reward verified ✓")

    def _resolve_client_id(self) -> str:
        """Return the Twitch app client_id for the current token.

        In BYOI mode returns cfg.client_id directly.  In proxied mode the
        client_id is not bundled in the binary; we fetch it from Twitch's
        /validate endpoint, which always returns the client_id a token was
        issued to — this is a public value, not a secret.
        """
        from constants import TWITCH_APP_CLIENT_ID, is_byoi_mode
        if is_byoi_mode():
            return TWITCH_APP_CLIENT_ID

        # cfg.client_id may hold a previously-resolved value (e.g. if the
        # caller cached it).  Fall back to a live validate call if empty.
        if self.cfg.client_id:
            return self.cfg.client_id

        import requests as _req

        token = self.cfg.streamer_token
        if not token:
            print("[twitch] _resolve_client_id: no streamer token available")
            return ""

        try:
            resp = _req.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {token}"},
                timeout=8,
            )
            if not resp.ok:
                print(f"[twitch] _resolve_client_id: validate returned {resp.status_code}")
                return ""
            client_id = resp.json().get("client_id", "")
            if client_id:
                print("[twitch] resolved client_id from token validation")
            else:
                print("[twitch] _resolve_client_id: validate response missing client_id")
            return client_id
        except Exception as exc:
            print(f"[twitch] could not resolve client_id: {exc!r}")
            return ""

    def _maybe_refresh_token(self) -> None:
        from constants import is_byoi_mode, TWITCH_WORKER_URL
        if is_byoi_mode():
            self._refresh_one_byoi("streamer", self.cfg.streamer_refresh_token)
            if self.cfg.use_separate_bot and self.cfg.bot_refresh_token:
                self._refresh_one_byoi("bot", self.cfg.bot_refresh_token)
            return

        self._refresh_one(
            account="streamer",
            refresh_token=self.cfg.streamer_refresh_token,
            worker_url=TWITCH_WORKER_URL,
        )

        if self.cfg.use_separate_bot and self.cfg.bot_refresh_token:
            self._refresh_one(
                account="bot",
                refresh_token=self.cfg.bot_refresh_token,
                worker_url=TWITCH_WORKER_URL,
            )

    _REFRESH_BUFFER_SECONDS = 1800

    @staticmethod
    def _is_token_stale(issued_at: float, expires_in: int) -> bool:
        if issued_at == 0.0:
            return True
        return time.time() >= issued_at + expires_in - TwitchBot._REFRESH_BUFFER_SECONDS

    def _refresh_one_byoi(self, account: str, refresh_token: str) -> None:
        """BYOI mode: refresh directly against Twitch token endpoint."""
        if not refresh_token:
            print(f"[twitch] no refresh token for {account} — using stored token as-is")
            return

        issued_at  = self.cfg.bot_token_issued_at  if account == "bot" else self.cfg.streamer_token_issued_at
        expires_in = self.cfg.bot_token_expires_in if account == "bot" else self.cfg.streamer_token_expires_in

        if not self._is_token_stale(issued_at, expires_in):
            remaining = int(issued_at + expires_in - time.time())
            print(f"[twitch] {account} token still valid ({remaining // 60}m remaining) — skipping refresh")
            return

        print(f"[twitch] {account} token is stale — refreshing directly with Twitch...")
        from constants import TWITCH_APP_CLIENT_ID, TWITCH_APP_CLIENT_SECRET
        try:
            import requests as _req
            resp = _req.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id":     TWITCH_APP_CLIENT_ID,
                    "client_secret": TWITCH_APP_CLIENT_SECRET,
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=10,
            )
            if not resp.ok:
                print(f"[twitch] {account} BYOI refresh failed: HTTP {resp.status_code}")
                if resp.status_code in (400, 401) and self.on_reauth_required:
                    self.on_reauth_required(account, "Refresh token expired — please re-authenticate")
                return
            data = resp.json()
        except Exception as exc:
            print(f"[twitch] {account} BYOI refresh exception: {exc}")
            return

        new_access  = data.get("access_token", "")
        new_refresh = data.get("refresh_token", "")
        new_expires = int(data.get("expires_in", 14400))

        if not new_access:
            print(f"[twitch] {account} BYOI refresh response missing access_token")
            return

        print(f"[twitch] {account} BYOI token refreshed OK (expires in {new_expires // 60}m)")
        now = time.time()
        if account == "bot":
            self.cfg.bot_token            = new_access
            self.cfg.bot_token_issued_at  = now
            self.cfg.bot_token_expires_in = new_expires
            if new_refresh:
                self.cfg.bot_refresh_token = new_refresh
        else:
            self.cfg.streamer_token            = new_access
            self.cfg.streamer_token_issued_at  = now
            self.cfg.streamer_token_expires_in = new_expires
            if new_refresh:
                self.cfg.streamer_refresh_token = new_refresh

        if self.on_token_refreshed:
            self.on_token_refreshed(account, new_access, new_refresh or refresh_token)

    def _refresh_one(self, account: str, refresh_token: str, worker_url: str) -> None:
        if not refresh_token:
            print(f"[twitch] no refresh token for {account} — using stored token as-is")
            return

        issued_at  = self.cfg.bot_token_issued_at  if account == "bot" else self.cfg.streamer_token_issued_at
        expires_in = self.cfg.bot_token_expires_in if account == "bot" else self.cfg.streamer_token_expires_in

        if not self._is_token_stale(issued_at, expires_in):
            remaining = int(issued_at + expires_in - time.time())
            print(f"[twitch] {account} token still valid ({remaining // 60}m remaining) — skipping refresh")
            return

        print(f"[twitch] {account} token is stale — refreshing...")
        from server.auth import refresh_access_token
        bid = (self.cfg.bot_id if (account == "bot" and self.cfg.bot_id)
               else self.cfg.streamer_id)
        result = refresh_access_token(refresh_token, worker_url, broadcaster_id=bid)

        if result and result.get("reauth_required"):
            msg = result.get("error", "Re-authentication required")
            print(f"[twitch] ⚠ {account} requires re-authentication: {msg}")
            if self.on_reauth_required:
                self.on_reauth_required(account, msg)
            return

        if not result:
            print(f"[twitch] {account} token refresh failed — will try stored token")
            return

        new_access  = result.get("access_token", "")
        new_refresh = result.get("refresh_token", "")
        new_expires = int(result.get("expires_in", 14400))

        if not new_access:
            print(f"[twitch] {account} refresh response missing access_token")
            return

        print(f"[twitch] {account} token refreshed OK (expires in {new_expires // 60}m)")

        now = time.time()
        if account == "bot":
            self.cfg.bot_token            = new_access
            self.cfg.bot_token_issued_at  = now
            self.cfg.bot_token_expires_in = new_expires
            if new_refresh:
                self.cfg.bot_refresh_token = new_refresh
            refreshed_uid = result.get("user_id", "")
            if refreshed_uid and not self.cfg.bot_id:
                self.cfg.bot_id = refreshed_uid
                print(f"[twitch] bot_id populated from refresh response: {refreshed_uid}")
        else:
            self.cfg.streamer_token            = new_access
            self.cfg.streamer_token_issued_at  = now
            self.cfg.streamer_token_expires_in = new_expires
            if new_refresh:
                self.cfg.streamer_refresh_token = new_refresh

        if self.on_token_refreshed:
            self.on_token_refreshed(account, new_access, new_refresh or refresh_token)

    def _get_tier_cfg(self, chatter):
        """Return the TierConfig matching the chatter's highest applicable tier."""
        if chatter.broadcaster:
            return self.cfg.tier_head_mod
        if chatter.moderator:
            return self.cfg.tier_mod
        if getattr(chatter, "vip", False):
            return self.cfg.tier_vip
        if chatter.subscriber:
            return self.cfg.tier_subscriber
        return self.cfg.tier_viewer

    # ── Chat announcements ─────────────────────────────────────────────────────

    def announce_queued(self, username: str, title: str, position: int) -> None:
        """Post a queue confirmation to the channel.  Thread-safe."""
        pos_str = "will play next" if position == 1 else f"#{position} in queue"
        self._post(f"@{username} ✓ added: {title} ({pos_str})")

    def announce_failure(self, username: str, query: str) -> None:
        """Tell chat that a request couldn't be resolved.  Thread-safe."""
        self._post(f"@{username} couldn't find anything for: {query!r}")

    def _post(self, message: str) -> None:
        """Send a chat message from any thread."""
        if not (self._bot and self._loop and not self._loop.is_closed()):
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._bot._send_to_channel(message),
                self._loop,
            )
        except Exception as e:
            print(f"[twitch] announce failed: {e}")

    def _check_cooldown(self, username: str, cooldown_seconds: int) -> bool:
        now = time.time()
        last = self._cooldowns.get(username, 0.0)
        if now - last < cooldown_seconds:
            return False
        self._cooldowns[username] = now
        return True

    def _check_request_cap(self, username: str, queue_limit: int) -> bool:
        if queue_limit == 0:
            return True
        return self.queue.user_request_count(username) < queue_limit

    def _check_stream_cap(self, username: str, max_per_stream: int) -> bool:
        if max_per_stream == 0:
            return True
        return self.queue.get_session_request_count(username) < max_per_stream

    def _has_skip_permission(self, chatter) -> bool:
        perm = self.cfg.skip_permission
        if perm == "all":
            return True
        if chatter.broadcaster:
            return True
        if perm == "mod" and chatter.moderator:
            return True
        if perm in ("mod", "vip") and getattr(chatter, "vip", False):
            return True
        if perm in ("mod", "vip", "subscriber") and chatter.subscriber:
            return True
        return False


if HAS_TWITCHIO:
    class _MusicCommands(tw_commands.Component):
        """All chat commands in a Component so twitchio v3 can discover them."""

        def __init__(
            self,
            cfg: TwitchConfig,
            queue: QueueManager,
            controller: TwitchBot,
        ) -> None:
            self.cfg = cfg
            self.queue = queue
            self.ctrl = controller

        @tw_commands.command(name="songrequest", aliases=["sr", "request"])
        async def cmd_songrequest(self, ctx: tw_commands.Context, *, query: str = "") -> None:
            if not query:
                await ctx.send(f"@{ctx.chatter.name} Usage: !sr <URL or search query>")
                return
            if self.ctrl.cfg.requests_paused:
                await ctx.send(f"@{ctx.chatter.name} song requests are currently paused")
                return
            username = ctx.chatter.name
            tier = self.ctrl._get_tier_cfg(ctx.chatter)
            if not tier.can_request:
                await ctx.send(f"@{username} song requests are not available for your viewer tier")
                return
            if not self.ctrl._check_cooldown(username, tier.cooldown_seconds):
                remaining = int(
                    tier.cooldown_seconds
                    - (time.time() - self.ctrl._cooldowns.get(username, 0))
                )
                await ctx.send(f"@{username} cooldown active — {remaining}s remaining")
                return
            if not self.ctrl._check_request_cap(username, tier.queue_limit):
                await ctx.send(
                    f"@{username} you've reached your queue limit — wait for one of your songs to play"
                )
                return
            if not self.ctrl._check_stream_cap(username, getattr(tier, "max_per_stream", 0)):
                await ctx.send(f"@{username} you've reached your per-stream request limit")
                return
            if self.ctrl.on_song_request:
                self.ctrl.on_song_request(query, username)
            else:
                await ctx.send(f"@{username} song resolver not ready — try again shortly")

        @tw_commands.command(name="queue", aliases=["q"])
        async def cmd_queue(self, ctx: tw_commands.Context) -> None:
            tracks  = self.queue.snapshot()[:5]
            current = self.queue.current
            if not tracks and not current:
                await ctx.send("Queue is empty")
                return
            parts = []
            if current:
                parts.append(f"NOW: {current.display_title()}")
            for i, t in enumerate(tracks, 1):
                parts.append(f"{i}. {t.display_title()}")
            await ctx.send(" | ".join(parts)[:450])

        @tw_commands.command(name="skip")
        async def cmd_skip(self, ctx: tw_commands.Context) -> None:
            if not self.ctrl._has_skip_permission(ctx.chatter):
                await ctx.send(f"@{ctx.chatter.name} you don't have permission to skip")
                return
            current = self.queue.current
            if self.ctrl.on_skip_requested:
                self.ctrl.on_skip_requested()
            name = current.display_title() if current else "track"
            await ctx.send(f"Skipped: {name}")

        @tw_commands.command(name="currentsong", aliases=["song", "np"])
        async def cmd_currentsong(self, ctx: tw_commands.Context) -> None:
            current = self.queue.current
            if not current:
                await ctx.send("Nothing is playing right now")
                return
            msg = f"Now playing: {current.display_title()}"
            if current.requested_by:
                msg += f" (requested by @{current.requested_by})"
            await ctx.send(msg)

        @tw_commands.command(name="wrongsong", aliases=["remove", "cancelsong", "whoops"])
        async def cmd_wrongsong(self, ctx: tw_commands.Context) -> None:
            username = ctx.chatter.name
            removed = self.queue.remove_last_by_user(username)
            if removed:
                rid  = getattr(removed, "redemption_id",        "")
                rwid = getattr(removed, "redemption_reward_id", "")
                if rid and rwid:
                    self.ctrl.cancel_redemption(rid, rwid)
                await ctx.send(f"@{username} removed your request: {removed.display_title()}")
                return
            if self.ctrl.on_cancel_current_song:
                cancelled = self.ctrl.on_cancel_current_song(username)
                if cancelled:
                    await ctx.send(
                        f"@{username} cancelled: {cancelled.display_title()} "
                        f"— points refunded if applicable"
                    )
                    return
            await ctx.send(f"@{username} no queued song found from you")

    class _Bot(tw_commands.Bot):
        def __init__(
            self,
            cfg: TwitchConfig,
            queue: QueueManager,
            controller: TwitchBot,
            client_id: str = "",
        ) -> None:
            bot_user_id = cfg.bot_id if (cfg.use_separate_bot and cfg.bot_id) else cfg.streamer_id

            super().__init__(
                client_id=client_id,
                client_secret="",     # intentionally not passed — refresh handled by _maybe_refresh_token
                bot_id=bot_user_id,
                prefix=cfg.prefix,
            )
            self.cfg = cfg
            self.queue = queue
            self.ctrl = controller

        async def setup_hook(self) -> None:
            # Add user tokens — twitchio needs them registered before subscribing.
            # Refresh tokens are intentionally omitted: TwitchIO's background
            # revalidation would try to refresh directly against Twitch using
            # client_secret so twitchio can't attempt its own refresh loop —
            # all proactive refreshing is handled by _maybe_refresh_token()
            # before connect (proxied: via Worker; BYOI: direct to Twitch).
            # Mid-session expiry is caught by the reconnect loop in _run().
            if self.cfg.streamer_token:
                await self.add_token(self.cfg.streamer_token, "")

            if self.cfg.use_separate_bot and self.cfg.bot_token:
                await self.add_token(self.cfg.bot_token, "")

            broadcaster_id = self.cfg.streamer_id
            bot_user_id    = self.cfg.bot_id if (self.cfg.use_separate_bot and self.cfg.bot_id) else self.cfg.streamer_id

            # Subscribe to chat messages on the streamer's channel.
            # Requires user:read:chat scope on the bot/streamer token.
            # If subscription fails with a scope error, the user needs to
            # sign out and sign back in to obtain a token with the new scope.
            if broadcaster_id and bot_user_id:
                try:
                    await self.subscribe_websocket(
                        tw_eventsub.ChatMessageSubscription(
                            broadcaster_user_id=broadcaster_id,
                            user_id=bot_user_id,
                        )
                    )
                    print(f"[twitch] subscribed to chat for #{self.cfg.channel}")
                except Exception as exc:
                    err_str = str(exc)
                    if "scope" in err_str.lower() or "403" in err_str or "401" in err_str:
                        print(
                            f"[twitch] ⚠ chat subscription failed — token missing "
                            f"user:read:chat scope. Sign out and sign back in: {exc!r}"
                        )
                    else:
                        print(f"[twitch] chat subscription error: {exc!r}")

            # Subscribe to channel-point redemptions when the feature is active.
            if (
                self.cfg.channel_points_enabled
                and self.cfg.channel_points_reward_id
                and broadcaster_id
            ):
                try:
                    await self.subscribe_websocket(
                        tw_eventsub.ChannelPointsRedeemAddSubscription(
                            broadcaster_user_id=broadcaster_id,
                            reward_id=self.cfg.channel_points_reward_id,
                        ),
                        token_for=broadcaster_id,
                    )
                    print("[twitch] subscribed to channel-points redemptions")
                except Exception as exc:
                    print(f"[twitch] channel-points subscription error: {exc!r}")

            # Subscribe to stream.offline so queued CP redemptions can be refunded
            # when the stream ends.  No special scope required.
            if broadcaster_id:
                try:
                    await self.subscribe_websocket(
                        tw_eventsub.StreamOfflineSubscription(
                            broadcaster_user_id=broadcaster_id,
                        ),
                        token_for=broadcaster_id,
                    )
                    print("[twitch] subscribed to stream.offline")
                except Exception as exc:
                    print(f"[twitch] stream.offline subscription error: {exc!r}")

            # Load chat commands via Component.
            await self.add_component(_MusicCommands(self.cfg, self.queue, self.ctrl))

            # Register user-configured extra command aliases.
            user_aliases: dict = getattr(self.cfg, "command_aliases", {}) or {}
            for cmd_name, extras in user_aliases.items():
                base_cmd = self.get_command(cmd_name)
                if base_cmd is None:
                    continue
                for alias in (extras or []):
                    alias_clean = alias.strip().lstrip(self.cfg.prefix).lower()
                    if alias_clean and alias_clean not in self._commands:
                        self._commands[alias_clean] = base_cmd
                        print(f"[twitch] registered alias !{alias_clean} → !{cmd_name}")

            print(f"[twitch] bot ready on #{self.cfg.channel}")
            if self.ctrl.on_connected:
                self.ctrl.on_connected()

        async def event_message(self, payload: twitchio.ChatMessage) -> None:
            if payload.chatter.id == self.bot_id:
                return
            if payload.source_broadcaster is not None:
                return
            if self.ctrl.on_chat_message:
                self.ctrl.on_chat_message(payload.chatter.name, payload.text)
            await self.process_commands(payload)

        async def event_custom_redemption_add(
            self, payload: twitchio.ChannelPointsRedemptionAdd
        ) -> None:
            user_input    = (payload.user_input or "").strip()
            user_login    = payload.user.name
            redemption_id = payload.id
            reward_id     = payload.reward.id
            reward_title  = payload.reward.title

            print(f"[cp] redemption from @{user_login}: [{reward_title}] {user_input!r}")

            if not user_input:
                return

            self.ctrl._handle_redemption(user_input, user_login, redemption_id, reward_id)

        async def event_stream_offline(self, payload) -> None:
            print("[twitch] stream offline — refunding queued CP redemptions")
            if self.ctrl.on_stream_offline:
                self.ctrl.on_stream_offline()

        async def _send_to_channel(self, message: str) -> None:
            """Send a message to the streamer's channel using the bot's user token."""
            broadcaster = self.create_partialuser(self.cfg.streamer_id, self.cfg.channel)
            bot_user_id = (
                self.cfg.bot_id if (self.cfg.use_separate_bot and self.cfg.bot_id)
                else self.cfg.streamer_id
            )
            try:
                await broadcaster.send_message(message, sender=bot_user_id, token_for=bot_user_id)
            except Exception as exc:
                print(f"[twitch] _send_to_channel error: {exc!r}")
