"""
MusicHat — entry point.

Boot sequence
─────────────
1. Load config
2. Init Qt application + apply stylesheet
3. Build player stack (QueueManager → FFTPipeline → PlaybackEngine)
4. Build main window (wires FFT → spectrogram widget)
5. Start FastAPI server in background thread
6. Start Twitch bot in background thread
7. Start tunnel (mode from config)
8. Enter Qt event loop
"""

from __future__ import annotations

import os
import secrets
import sys
import threading


def _load_dotenv() -> None:
    """
    Load DATA_DIR/.env into os.environ before constants.py is imported.

    In the frozen binary, bootstrap_check sets MUSICHAT_DATA_DIR before
    main.py is imported, so DATA_DIR is already known here.  In dev mode
    (python main.py) the env var is unset and we fall back to a .env in
    the project root — keeping the old dev workflow intact.

    Values already set in the environment take precedence (setdefault), so
    shell env vars always win over the file.
    """
    data_dir = os.environ.get("MUSICHAT_DATA_DIR")
    if data_dir:
        env_path = os.path.join(data_dir, ".env")
    else:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip("\"'")
            if key:
                os.environ.setdefault(key, value)


_load_dotenv()

from PySide6.QtCore import QObject, Signal as _Signal  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from config import load_config, save_config  # noqa: E402
from player.engine import PlaybackEngine  # noqa: E402
from player.fft import FFTPipeline  # noqa: E402
from player.playlist_manager import PlaylistManager  # noqa: E402
from player.queue_manager import QueueManager  # noqa: E402
from server.routes import spectrogram as spec_routes  # noqa: E402
from theme import APP_QSS  # noqa: E402
from ui.main_window import MainWindow, show_vibe_ack_dialog  # noqa: E402


class _MainThreadInvoker(QObject):
    """Posts any callable to the Qt main-thread event queue from any thread.

    Signals are marshalled through Qt's queued-connection mechanism, which is
    the only officially thread-safe way to call Qt APIs from non-Qt threads.
    This eliminates the QObject::setParent warning that QTimer.singleShot
    (context form) produces when called from daemon threads.
    """
    _invoke = _Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._invoke.connect(self._run)

    def _run(self, fn) -> None:
        fn()

    def __call__(self, fn) -> None:
        self._invoke.emit(fn)


# Populated in main() after QApplication is created; safe to use from any
# thread thereafter since all daemon threads start after this point.
_gui: _MainThreadInvoker | None = None


def _load_share_tech_mono() -> None:
    """Try to register Share Tech Mono from the resources folder."""
    import os
    font_path = os.path.join(
        os.path.dirname(__file__), "resources", "ShareTechMono-Regular.ttf"
    )
    if os.path.exists(font_path):
        QFontDatabase.addApplicationFont(font_path)


def _stop_tunnel(tunnel_ref: list) -> None:
    """Stop any currently running tunnel and clear the ref."""
    if tunnel_ref[0] is not None:
        try:
            tunnel_ref[0].stop()
        except Exception:
            pass
        tunnel_ref[0] = None


def _start_tunnel(cfg, window: MainWindow, tunnel_ref: list) -> None:
    """
    Build and start the tunnel for the current config.

    `tunnel_ref` is a one-element list so callers can hold a mutable reference
    to the active tunnel (for later stop/restart via the settings page).
    """
    mode = cfg.server.tunnel_mode
    port = cfg.server.port

    if not mode or mode == "none":
        return  # Tunnel disabled — stays grey, mod panel only reachable on LAN/localhost

    if mode == "cloudflare":
        if not cfg.server.cloudflare_accepted_tos:
            window.set_tunnel_status("ToS not accepted", "grey")
            return
        from tunnel.cloudflared import CloudflareTunnel
        t = CloudflareTunnel(port)
    elif mode == "ngrok":
        if not cfg.server.ngrok_authtoken:
            window.set_tunnel_status("ngrok token missing", "grey")
            return
        from tunnel.ngrok import NgrokTunnel
        t = NgrokTunnel(port, cfg.server.ngrok_authtoken, cfg.server.ngrok_domain)
    elif mode == "tailscale":
        from tunnel.tailscale import TailscaleTunnel
        t = TailscaleTunnel(port)
    else:
        window.set_tunnel_status("self-hosted", "green")
        return

    _service_labels = {
        "cloudflare": "cloudflare",
        "ngrok":      "ngrok",
        "tailscale":  "tailscale",
    }

    def on_url(url: str) -> None:
        service = _service_labels.get(mode, "tunnel")
        _gui(lambda: window.set_tunnel_status(service, "green", copy_url=url))
        from server.settings_app import broadcast_to_settings
        broadcast_to_settings({"type": "tunnel_status", "url": url, "online": True})
        # Push the new tunnel origin to the Worker so mod logins from this URL
        # are accepted.  Runs in a daemon thread so it never blocks the UI.
        from constants import TWITCH_WORKER_URL, is_byoi_mode
        if not is_byoi_mode() and cfg.twitch.streamer_token and cfg.twitch.streamer_id:
            from server.auth import update_panel_origin as _upo
            threading.Thread(
                target=_upo,
                args=(url, cfg.twitch.streamer_token, cfg.twitch.streamer_id, TWITCH_WORKER_URL),
                daemon=True,
            ).start()

    def on_err(msg: str) -> None:
        _gui(lambda: window.set_tunnel_status(f"error: {msg[:60]}", "red"))
        from server.settings_app import broadcast_to_settings
        broadcast_to_settings({"type": "tunnel_status", "url": None, "online": False,
                                "error": msg})  # no truncation — settings page wraps

    t.on_url_assigned = on_url
    t.on_error = on_err
    tunnel_ref[0] = t
    service = _service_labels.get(mode, "tunnel")
    window.set_tunnel_status(f"{service}: connecting", "yellow")
    t.start()


def _start_bot(cfg, queue: QueueManager, window: MainWindow, vibe=None,
               tunnel_ref: list = None, vibe_needs_disarm: list = None,
               playlist_shuffle_active: list = None) -> None:
    if vibe_needs_disarm is None:
        vibe_needs_disarm = [False]
    if playlist_shuffle_active is None:
        playlist_shuffle_active = [False]
    from integrations.twitch_bot import TwitchBot
    from constants import TWITCH_WORKER_URL, is_byoi_mode
    bot = TwitchBot(cfg.twitch, queue)

    def on_connecting(account: str) -> None:
        _gui(lambda: window.set_bot_status("yellow", account))

    def on_tokens_ready() -> None:
        # Runs on the bot thread after tokens are confirmed fresh, before the
        # bot connects.  Syncing DPoP here guarantees the Worker has the current
        # JWK before any subsequent Worker call (reward check, EventSub, etc.).
        if not is_byoi_mode() and cfg.twitch.streamer_token and cfg.twitch.streamer_id:
            from server.auth import sync_dpop_key as _sdk
            _sdk(cfg.twitch.streamer_token, cfg.twitch.streamer_id, TWITCH_WORKER_URL)

    def on_connected() -> None:
        sending_account = (
            cfg.twitch.bot_username
            if (cfg.twitch.use_separate_bot and cfg.twitch.bot_username)
            else cfg.twitch.streamer_username
        ) or ""
        _gui(lambda: (
            window.set_bot_status("green", sending_account),
            window._mode_bar.set_twitch_connected(True),
            window._mode_bar.set_requests_paused(cfg.twitch.requests_paused),
        ))
        # Start the tunnel now that the bot is confirmed connected — mod panel
        # only becomes reachable from a working authenticated session.
        # Guard against double-start on reconnect: if the tunnel is already
        # running (tunnel_ref[0] is set) leave it alone.
        if tunnel_ref is not None and cfg.twitch.streamer_id and tunnel_ref[0] is None:
            print("[main] bot connected — starting tunnel")
            _start_tunnel(cfg, window, tunnel_ref)

    def on_disconnected() -> None:
        _gui(lambda: (
            window.set_bot_status("grey"),
            window._mode_bar.set_twitch_connected(False),
        ))

    def on_chat(username: str, message: str) -> None:
        _gui(lambda: window.append_chat(f"[{username}] {message}"))

    def on_song_request(query: str, username: str) -> None:
        """Resolve query to a track in a background thread, then enqueue it."""
        _gui(lambda: window.append_chat(f"[req] @{username}: {query!r} — looking up..."))

        def _resolve() -> None:
            from player.resolver import resolve
            from player.queue_manager import RequestOrigin

            track = resolve(query, requested_by=username, origin=RequestOrigin.CHAT)

            if track is None:
                _gui(lambda: window.append_chat(f"[req] @{username}: couldn't resolve {query!r}"))
                bot.announce_failure(username, query)
                return

            pos = queue.enqueue_request(track)
            if vibe:
                vibe.on_user_request(track)
            pos_str = "will play next" if pos == 1 else f"#{pos} in queue"
            title = track.display_title()
            _gui(lambda: window.append_chat(f"[req] {pos_str} for @{username} — {title}"))
            bot.announce_queued(username, title, pos)

        threading.Thread(target=_resolve, daemon=True).start()

    def on_token_refreshed(account: str, access_token: str, refresh_token: str) -> None:
        # Called from the bot thread after a runtime token refresh.
        # The in-memory cfg.twitch has already been updated; just persist.
        save_config(cfg)
        print(f"[main] {account} token refreshed and saved")
        # Re-sync DPoP and re-register panel origin whenever the streamer token
        # rotates at runtime, so the Worker KV stays current.
        if account == "streamer" and not is_byoi_mode() and cfg.twitch.streamer_id:
            from server.auth import sync_dpop_key as _sdk
            _sdk(access_token, cfg.twitch.streamer_id, TWITCH_WORKER_URL)
            from server.settings_app import _tunnel_status as _ts
            _turl = (_ts or {}).get("url") or ""
            if _turl:
                from server.auth import update_panel_origin as _upo
                threading.Thread(
                    target=_upo,
                    args=(_turl, access_token, cfg.twitch.streamer_id, TWITCH_WORKER_URL),
                    daemon=True,
                ).start()

    def _cancel_cp(rid: str, rwid: str) -> None:
        """Refund channel-points if redemption IDs are present."""
        if rid and rwid:
            bot.cancel_redemption(rid, rwid)

    def on_channel_points_request(
        query:         str,
        username:      str,
        redemption_id: str,
        reward_id:     str,
    ) -> None:
        """
        Channel-points song request.

        Resolves the query in a background thread.  On success the redemption
        IDs are stored on the Track so bot.mark_track_started() can FULFILL
        when the song actually plays.  On failure the redemption is CANCELED
        immediately (refunds the viewer's points).
        """
        _gui(lambda: window.append_chat(f"[cp] @{username}: {query!r} — looking up..."))

        def _resolve() -> None:
            from player.resolver import resolve
            from player.queue_manager import RequestOrigin

            track = resolve(query, requested_by=username, origin=RequestOrigin.CHANNEL_POINTS)

            if track is None:
                _gui(lambda: window.append_chat(
                    f"[cp] @{username}: couldn't resolve {query!r} — refunding points"
                ))
                bot.announce_failure(username, query)
                _cancel_cp(redemption_id, reward_id)
                return

            # Attach redemption metadata so fulfill/cancel can fire later
            track.redemption_id        = redemption_id
            track.redemption_reward_id = reward_id

            pos = queue.enqueue_request(track)
            if vibe:
                vibe.on_user_request(track)
            pos_str = "will play next" if pos == 1 else f"#{pos} in queue"
            title = track.display_title()
            _gui(lambda: window.append_chat(f"[cp] {pos_str} for @{username} — {title}"))
            bot.announce_queued(username, title, pos)
            # Points are NOT spent yet — FULFILLED fires after 5-second window.

        threading.Thread(target=_resolve, daemon=True).start()

    def on_reauth_required(account: str, message: str) -> None:
        """Token was revoked or replaced — signal the UI to prompt re-authentication."""
        from server.settings_app import broadcast_to_settings
        broadcast_to_settings({
            "type":    "reauth_required",
            "account": account,
            "message": message,
        })
        _gui(lambda: window.append_chat(
            f"[twitch] ⚠ {account} account needs re-authentication — "
            "open settings → Accounts to sign in again"
        ))

    def on_auth_failed(account: str) -> None:
        """Bot token is permanently invalid — show red LED and banner in settings."""
        from server.settings_app import broadcast_to_settings
        _gui(lambda: window.set_bot_status("red"))
        broadcast_to_settings({"type": "bot_auth_failed", "account": account})
        _gui(lambda: window.append_chat(
            f"[twitch] ✗ {account} login failed — token invalid. "
            "Open settings → Accounts to sign in again."
        ))

    def on_config_changed() -> None:
        """Called by the bot when it auto-modifies config (e.g. reward deleted)."""
        save_config(cfg)

    bot.on_connecting   = on_connecting
    bot.on_tokens_ready = on_tokens_ready
    bot.on_connected    = on_connected
    bot.on_disconnected = on_disconnected
    bot.on_chat_message = on_chat
    bot.on_song_request = on_song_request
    bot.on_channel_points_request = on_channel_points_request
    bot.on_config_changed  = on_config_changed
    bot.on_token_refreshed = on_token_refreshed
    bot.on_reauth_required = on_reauth_required
    bot.on_auth_failed     = on_auth_failed

    # ── Channel-points redemption lifecycle ────────────────────────────────────
    # Redemptions are FULFILLED only when their song plays to natural completion.
    # Any skip (mod panel, bot !skip, desktop button, !wrongsong) or stream-end
    # CANCELS (refunds) the redemption immediately.
    #
    # _active_cp_ref[0]: the Track currently playing that has a CP redemption.
    # Wrapped in a list so nested-function closures can reassign it.

    _active_cp_ref: list = [None]
    _active_cp_lock = threading.Lock()

    def _cancel_current_cp() -> None:
        """Refund the CP redemption for the currently-playing track (if any)."""
        with _active_cp_lock:
            track = _active_cp_ref[0]
            _active_cp_ref[0] = None
        if track is not None:
            _cancel_cp(
                getattr(track, "redemption_id",        ""),
                getattr(track, "redemption_reward_id", ""),
            )

    def _fulfill_current_cp() -> None:
        """Fulfill the CP redemption for the currently-playing track (natural end)."""
        with _active_cp_lock:
            track = _active_cp_ref[0]
            _active_cp_ref[0] = None
        if track is not None:
            bot.mark_track_started(track)

    _orig_on_track_started = window.engine.on_track_started

    def _on_track_started(track) -> None:
        # Fire existing listeners (now-playing card, history, etc.)
        if _orig_on_track_started:
            _orig_on_track_started(track)

        # Fulfill the previously-playing CP track — it reached natural completion
        # because a new track is now starting (auto-advance replaced it).
        with _active_cp_lock:
            prev = _active_cp_ref[0]
            rid  = getattr(track, "redemption_id",        "")
            rwid = getattr(track, "redemption_reward_id", "")
            _active_cp_ref[0] = track if (rid and rwid) else None
        if prev is not None:
            bot.mark_track_started(prev)

        # Push to OBS now-playing overlay clients
        from server.routes.overlays import notify_track_changed
        notify_track_changed(track)

        # Vibe engine
        if vibe:
            vibe.on_track_started(track)

        # Vibe was armed with no song playing — the user never actually
        # pinned anything, so don't let it silently ride along into whatever
        # just started.  Switch it back off now.  Playlist-shuffle mode never
        # sets this flag, so it's unaffected.
        if vibe_needs_disarm[0] and not playlist_shuffle_active[0]:
            vibe_needs_disarm[0] = False
            window._mode_bar._vibe_btn.setChecked(False)

    window.engine.on_track_started = _on_track_started

    def _cancel_current_song(username: str):
        """Called by !wrongsong — cancel the currently-playing track if it belongs to this user."""
        current = window.engine._current_track
        if current is None:
            return None
        if (current.requested_by or "").lower() != username.lower():
            return None
        _cancel_current_cp()
        _gui(window.engine.skip)
        return current

    bot.on_cancel_current_song = _cancel_current_song

    def _refund_all_cp() -> None:
        """Refund all pending CP redemptions — current track + entire queue."""
        _cancel_current_cp()
        for t in queue.snapshot():
            rid  = getattr(t, "redemption_id",        "")
            rwid = getattr(t, "redemption_reward_id", "")
            if rid and rwid:
                bot.cancel_redemption(rid, rwid)

    bot.on_stream_offline = _refund_all_cp

    # Clear the OBS overlay when playback stops so album-art pages go blank
    # rather than sticking on the last track.
    _orig_on_state_changed = window.engine.on_state_changed

    def _on_state_changed_with_overlay(state) -> None:
        if _orig_on_state_changed:
            _orig_on_state_changed(state)
        from player.engine import PlayState
        if state == PlayState.STOPPED:
            # Fulfill the last track's CP redemption if it ended naturally.
            # Skips clear _active_cp_ref first, so this is a no-op after a skip.
            # Manual stop (■) parks the track — do not fulfill.
            if not window._user_stopped:
                _fulfill_current_cp()
            from server.routes.overlays import clear_now_playing
            clear_now_playing()
        elif state == PlayState.PLAYING:
            # When resuming from pause the overlay was never cleared (the engine
            # fix suppresses the spurious STOPPED during pause), but re-pushing
            # here is a cheap belt-and-suspenders guard in case something else
            # (e.g. OBS reloading the source) left the overlay empty.
            current = window.engine._current_track
            if current is not None:
                from server.routes.overlays import notify_track_changed
                notify_track_changed(current)

    window.engine.on_state_changed = _on_state_changed_with_overlay

    # ── Streamer-initiated queue removal → refund channel-point redemption ────
    # The queue panel's remove button calls queue.remove() directly.  Intercept
    # here so any CP redemption on the removed track gets cancelled (refunded).
    def _on_queue_remove_requested(track_id: str) -> None:
        track = next((t for t in queue.snapshot() if t.id == track_id), None)
        if track is not None:
            rid  = getattr(track, "redemption_id",        "")
            rwid = getattr(track, "redemption_reward_id", "")
            if rid and rwid:
                bot.cancel_redemption(rid, rwid)
        queue.remove(track_id)

    window._queue_panel.remove_requested.disconnect(queue.remove)
    window._queue_panel.remove_requested.connect(_on_queue_remove_requested)

    # ── All skip paths → refund CP redemption ──────────────────────────────────
    # Defined here so _cancel_current_cp and bot are both in scope.
    from server.routes import queue as _queue_routes  # noqa: E402

    def _skip_with_refund() -> None:
        _cancel_current_cp()
        if window._user_stopped:
            window._clear_stop_state()
        window.engine.skip()

    bot.on_skip_requested = _skip_with_refund
    _queue_routes.set_skip_callback(_skip_with_refund)
    window._transport.skip_clicked.disconnect(window._do_skip)
    window._transport.skip_clicked.connect(_skip_with_refund)

    bot.start()
    return bot


def _start_server(cfg, queue: QueueManager, window: MainWindow):
    from server.app import ServerManager
    mgr = ServerManager(cfg, queue)

    def on_ready() -> None:
        _gui(lambda: window.set_server_status("green"))

    def on_error(msg: str) -> None:
        _gui(lambda: window.set_server_status("red"))
        print(f"[server] startup failed: {msg}")

    mgr.on_ready = on_ready
    mgr.on_error = on_error
    window.set_server_status("yellow")
    mgr.start()
    return mgr


def _start_settings_server(
    cfg,
    bot_ref: list,
    window_ref: list,
    tunnel_ref: list,
):
    """
    Start the localhost-only settings web server on port 8766.

    `bot_ref`, `window_ref`, and `tunnel_ref` are one-element lists so we can
    capture the live objects after they're created without circular dependencies.
    """
    from server.settings_server import SettingsServerManager

    def bot_restart():
        bot = bot_ref[0]
        if bot:
            print("[settings] restarting bot with updated config...")
            bot.stop()
            threading.Thread(target=bot.start, daemon=True).start()

    def spec_changed():
        """
        Called from a FastAPI worker thread when a spectrogram field changes.
        Emits a Qt signal so the update runs on the main thread — direct Qt
        calls from a non-Qt thread are not safe.
        """
        win = window_ref[0]
        if win:
            win.spec_config_changed.emit()

    def tunnel_start():
        win = window_ref[0]
        if win:
            _stop_tunnel(tunnel_ref)
            _start_tunnel(cfg, win, tunnel_ref)

    def tunnel_stop():
        win = window_ref[0]
        _stop_tunnel(tunnel_ref)
        if win:
            _gui(lambda: win.set_tunnel_status("stopped", "grey"))
        from server.settings_app import broadcast_to_settings
        broadcast_to_settings({"type": "tunnel_status", "url": None, "online": False})

    def device_changed(device):
        win = window_ref[0]
        if win is None:
            return
        win._output_device_changed.emit(device)

    def data_reset():
        """Clear all persisted credentials and config; keep app alive."""
        import dataclasses as _dc
        import secure_store as _ss
        from config import AppConfig as _AppConfig

        for key in [
            _ss.STREAMER_TOKEN, _ss.STREAMER_REFRESH,
            _ss.BOT_TOKEN, _ss.BOT_REFRESH,
            _ss.JWT_SECRET, _ss.DPOP_PRIVATE_KEY,
        ]:
            try:
                _ss.put(key, "")
            except Exception:
                pass

        # Reset cfg in-place so all live references see fresh defaults.
        _defaults = _AppConfig()
        for _field in _dc.fields(_defaults):
            try:
                setattr(cfg, _field.name, getattr(_defaults, _field.name))
            except Exception:
                pass

        save_config(cfg)

        # Clear the in-process DPoP keypair — will regenerate on next call.
        try:
            import dpop_utils as _dpop
            _dpop._private_key = None
            _dpop._public_jwk = None
        except Exception:
            pass

        from server.settings_app import broadcast_to_settings
        broadcast_to_settings({"type": "data_reset"})

    def _wipe_cleanup(remove_pyside6: bool = True) -> None:
        """Background thread: clears credentials + config dir, then quits.

        Runs after begin_wipe_shutdown() has already hidden the main window and
        shown the RemovalSplash, so slow I/O here doesn't block the UI.
        """
        import pathlib
        import shutil
        import secure_store as _ss
        from config import CONFIG_PATH
        from PySide6.QtCore import QMetaObject, Qt
        from PySide6.QtWidgets import QApplication

        for _key in [
            _ss.STREAMER_TOKEN, _ss.STREAMER_REFRESH,
            _ss.BOT_TOKEN, _ss.BOT_REFRESH,
            _ss.JWT_SECRET, _ss.DPOP_PRIVATE_KEY,
        ]:
            try:
                _ss.put(_key, "")
            except Exception:
                pass

        _cfg_dir = pathlib.Path(CONFIG_PATH).parent
        if _cfg_dir.exists():
            if remove_pyside6:
                shutil.rmtree(_cfg_dir, ignore_errors=True)
            else:
                # Keep pyside6/ — only delete user data files/folders.
                for _item in list(_cfg_dir.iterdir()):
                    if _item.name == "pyside6":
                        continue
                    try:
                        if _item.is_dir():
                            shutil.rmtree(_item, ignore_errors=True)
                        else:
                            _item.unlink(missing_ok=True)
                    except Exception:
                        pass

        # When PySide6 is being removed, also clear the bootstrap config so
        # the next launch re-runs the setup wizard rather than trying to load
        # the now-deleted (or partially deleted) PySide6 installation.
        if remove_pyside6:
            _local_app = os.environ.get(
                "LOCALAPPDATA",
                str(pathlib.Path.home() / "AppData" / "Local"),
            )
            _bootstrap = pathlib.Path(_local_app) / "musichat" / "bootstrap.json"
            try:
                _bootstrap.unlink(missing_ok=True)
            except Exception:
                pass

        QMetaObject.invokeMethod(
            QApplication.instance(),
            "quit",
            Qt.ConnectionType.QueuedConnection,
        )

    def data_wipe(remove_pyside6: bool = True):
        """Entry point called from the DataWipe daemon thread spawned by the
        settings server.  Stops non-Qt services here (safe from any thread),
        then dispatches begin_wipe_shutdown() to the Qt main thread via
        QMetaObject.invokeMethod — the only reliable cross-thread Qt dispatch
        from a plain Python thread.
        """
        from PySide6.QtCore import QMetaObject, Qt

        _bot = bot_ref[0]
        if _bot:
            try:
                _bot.stop()
            except Exception:
                pass
        _tnl = tunnel_ref[0]
        if _tnl:
            try:
                _tnl.stop()
            except Exception:
                pass

        _win = window_ref[0]
        if _win:
            _win.on_wipe_shutdown = lambda: _wipe_cleanup(remove_pyside6)
            QMetaObject.invokeMethod(
                _win,
                "begin_wipe_shutdown",
                Qt.ConnectionType.QueuedConnection,
            )
        else:
            # No window (shouldn't happen) — run cleanup directly and quit.
            threading.Thread(
                target=_wipe_cleanup, args=(remove_pyside6,),
                daemon=True, name="WipeCleanup",
            ).start()

    mgr = SettingsServerManager(
        cfg,
        bot_restart_cb=bot_restart,
        spec_changed_cb=spec_changed,
        tunnel_start_cb=tunnel_start,
        tunnel_stop_cb=tunnel_stop,
        device_changed_cb=device_changed,
        data_reset_cb=data_reset,
        data_wipe_cb=data_wipe,
    )
    mgr.start()
    return mgr


def _show_dependency_warning(missing: list, docs_url: str) -> None:
    """
    Show a non-blocking warning dialog listing missing dependencies and the
    features they affect.  The user can continue with reduced functionality
    or exit to install the missing items first.
    """
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QDialog, QDialogButtonBox, QLabel,
        QVBoxLayout, QHBoxLayout, QFrame,
    )

    dlg = QDialog()
    dlg.setWindowTitle("MusicHat — Missing Dependencies")
    dlg.setMinimumWidth(500)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)
    layout.setContentsMargins(20, 20, 20, 20)

    header = QLabel("Some features will be unavailable")
    header.setStyleSheet("font-size: 14px; font-weight: bold;")
    layout.addWidget(header)

    sub = QLabel(
        "The following system dependencies are missing. "
        "MusicHat will still launch, but the affected features won't work."
    )
    sub.setWordWrap(True)
    layout.addWidget(sub)

    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFrameShadow(QFrame.Shadow.Sunken)
    layout.addWidget(sep)

    for item in missing:
        row = QHBoxLayout()
        bullet = QLabel("✗")
        bullet.setFixedWidth(16)
        bullet.setStyleSheet("color: #ff4444; font-weight: bold;")
        text = QLabel(f"<b>{item.label}</b> — {item.feature}")
        text.setWordWrap(True)
        row.addWidget(bullet, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text, 1)
        layout.addLayout(row)

    sep2 = QFrame()
    sep2.setFrameShape(QFrame.Shape.HLine)
    sep2.setFrameShadow(QFrame.Shadow.Sunken)
    layout.addWidget(sep2)

    docs_label = QLabel(f'<a href="{docs_url}">Setup guide — installation instructions and explanations</a>')
    docs_label.setOpenExternalLinks(False)
    docs_label.linkActivated.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
    layout.addWidget(docs_label)

    buttons = QDialogButtonBox()
    continue_btn = buttons.addButton("Continue anyway", QDialogButtonBox.ButtonRole.AcceptRole)
    buttons.addButton("Exit", QDialogButtonBox.ButtonRole.RejectRole)
    continue_btn.setDefault(True)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Rejected:
        sys.exit(0)


def main() -> int:
    global _gui
    # ── Qt app ─────────────────────────────────────────────────────────────────
    app = QApplication(sys.argv)
    _gui = _MainThreadInvoker()
    app.setApplicationName("MusicHat")
    app.setOrganizationName("xwhitehat")

    from ui.main_window import _app_icon_path
    from PySide6.QtGui import QIcon as _QIcon
    _ico = _QIcon(str(_app_icon_path()))
    if not _ico.isNull():
        app.setWindowIcon(_ico)

    _load_share_tech_mono()

    # Apply global stylesheet
    app.setStyleSheet(APP_QSS)

    # ── Splash screen ──────────────────────────────────────────────────────────
    # Show immediately so the user sees something while the heavy init runs.
    from ui.splash import SplashScreen as _Splash
    splash = _Splash()
    splash.show()
    app.processEvents()

    # ── Config ─────────────────────────────────────────────────────────────────
    splash.step(5, "Loading configuration…")
    cfg = load_config()

    # Self-heal: configs saved before bot_id was added won't have the field.
    # Validate the stored bot token once so _refresh_one can use the correct
    # broadcaster_id instead of falling back to streamer_id (which 403s).
    if cfg.twitch.use_separate_bot and cfg.twitch.bot_token and not cfg.twitch.bot_id:
        try:
            import requests as _r
            _bv = _r.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {cfg.twitch.bot_token}"},
                timeout=6,
            )
            if _bv.ok:
                _buid = _bv.json().get("user_id", "")
                if _buid:
                    cfg.twitch.bot_id = _buid
                    save_config(cfg)
                    print(f"[main] bot_id migrated from token validation: {_buid}")
        except Exception as _be:
            print(f"[main] bot_id migration failed (re-auth bot in Settings): {_be}")

    # First launch = no Twitch account linked yet; show more verbose messages.
    _first = not cfg.twitch.streamer_id

    # ── Pre-launch system checks ────────────────────────────────────────────
    splash.step(10, "Checking system dependencies…" if _first else "Verifying dependencies…")
    from precheck import check as _precheck, SETUP_DOCS_URL as _DOCS
    _missing = _precheck(cfg)
    if _missing:
        _show_dependency_warning(_missing, _DOCS)

    # Auto-generate JWT secret on first run
    splash.step(15, "Generating security credentials…" if not cfg.server.jwt_secret else "Loading security credentials…")
    if not cfg.server.jwt_secret:
        cfg.server.jwt_secret = secrets.token_hex(32)
        save_config(cfg)

    # Load or generate the streamer's DPoP keypair (Channel A: PC → Worker)
    splash.step(20, "Generating cryptographic keypair…" if _first else "Loading DPoP keypair…")
    import dpop_utils as _dpop
    _dpop.load_or_generate()

    # ── Player stack ───────────────────────────────────────────────────────────
    splash.step(30, "Initialising queue manager…")
    queue = QueueManager()
    playlists = PlaylistManager()

    splash.step(38, "Configuring audio pipeline…")
    fft = FFTPipeline(
        sample_rate=cfg.audio.sample_rate,
        fft_size=cfg.spectrogram.fft_size,
        bar_count=cfg.spectrogram.bar_count,
        freq_min=cfg.spectrogram.freq_min,
        freq_max=cfg.spectrogram.freq_max,
        window_function=cfg.spectrogram.window_function,
        smoothing=cfg.spectrogram.smoothing,
    )

    splash.step(48, "Starting playback engine…")
    engine = PlaybackEngine(
        queue_manager=queue,
        fft_pipeline=fft,
        sample_rate=cfg.audio.sample_rate,
        channels=cfg.audio.channels,
        blocksize=1024,
        output_device=cfg.audio.output_device,
    )

    # ── Integration clients ────────────────────────────────────────────────────
    splash.step(56, "Loading integration clients…")
    from integrations.soundcloud import SoundCloudClient
    sc_client = SoundCloudClient(cfg.soundcloud) if cfg.soundcloud.client_id else None

    # ── Main window ────────────────────────────────────────────────────────────
    splash.step(62, "Building interface…")
    window = MainWindow(cfg, queue, engine, fft, playlists, soundcloud_client=sc_client)

    # Wire FFT frames → spectrogram widget
    fft.add_frame_listener(window._spectrogram.push_frame)

    # ── Vibe engine ────────────────────────────────────────────────────────────
    splash.step(68, "Wiring vibe engine…")
    from player.vibe_engine import VibeEngine
    vibe = VibeEngine(queue, cfg.youtube)

    # Restore saved volume (no signal — engine is set directly too)
    window._transport.init_volume(cfg.audio.volume)
    window.engine.volume = cfg.audio.volume

    def _on_volume_changed(linear: float) -> None:
        cfg.audio.volume = round(linear, 4)
        save_config(cfg)
    window._transport.volume_changed.connect(_on_volume_changed)

    # Initialise slider + checkbox from saved config (no signals fired)
    window._mode_bar.init_vibe_controls(
        cfg.youtube.vibe_rigidness,
        cfg.youtube.vibe_artist_guard,
    )

    # Authoritative vibe-enabled state — updated by the toggle signal so
    # other callbacks don't have to reach back into the widget to check it.
    _vibe_enabled: list[bool] = [False]

    # True when vibe was switched on with nothing playing, so there was no
    # song for the user to knowingly pin.  Cleared the moment a track starts —
    # at that point the toggle is switched back off rather than silently
    # pinning whatever happened to start.  This is what lets us say the user
    # always affirmatively chose to pin a specific song: the only way vibe
    # mode survives into a new track is by pinning it live or via "shuffle +
    # play" on a playlist (which never sets this flag).
    _vibe_needs_disarm: list[bool] = [False]

    # Vibe-match toggle → vibe engine + now-playing card label + playlist panel
    def _on_vibe_toggled(enabled: bool) -> None:
        if enabled and not cfg.vibe_ack:
            if not show_vibe_ack_dialog(window):
                window._mode_bar._vibe_btn.setChecked(False)
                return
            cfg.vibe_ack = True
            save_config(cfg)
        _vibe_enabled[0] = enabled
        current = window.engine._current_track
        _vibe_needs_disarm[0] = enabled and current is None
        vibe.on_vibe_toggled(enabled, current)
        if enabled and current is not None:
            window.set_vibe_seed(f"vibe: {current.display_title()}")
        else:
            window.set_vibe_seed(None)
        window._playlist_panel.update_shuffle_btn_label(enabled)
    window._mode_bar.vibe_match_toggled.connect(_on_vibe_toggled)

    # Vibe rigidness slider → config
    def _on_vibe_rigidness(value: float) -> None:
        cfg.youtube.vibe_rigidness = round(value, 2)
        save_config(cfg)
    window._mode_bar.vibe_rigidness_changed.connect(_on_vibe_rigidness)

    # Artist guard checkbox → config
    def _on_artist_guard(enabled: bool) -> None:
        cfg.youtube.vibe_artist_guard = enabled
        save_config(cfg)
    window._mode_bar.vibe_artist_guard_changed.connect(_on_artist_guard)

    # Pause/resume requests toggle → config + Twitch reward
    def _on_requests_paused(paused: bool) -> None:
        cfg.twitch.requests_paused = paused
        save_config(cfg)
        # Sync desktop button — idempotent when triggered from the button itself,
        # required when triggered from the mod panel (different thread).
        _gui(lambda p=paused: window._mode_bar.set_requests_paused(p))
        if (
            cfg.twitch.channel_points_enabled
            and cfg.twitch.channel_points_reward_id
            and cfg.twitch.streamer_id
            and cfg.twitch.streamer_token
        ):
            from constants import is_byoi_mode as _ibm
            from server.auth import set_reward_enabled as _sre
            threading.Thread(
                target=_sre,
                args=(
                    cfg.twitch.streamer_id,
                    cfg.twitch.channel_points_reward_id,
                    not paused,
                    cfg.twitch.streamer_token,
                    cfg.twitch.client_id if _ibm() else "",
                ),
                daemon=True,
            ).start()
    window._mode_bar.requests_paused_toggled.connect(_on_requests_paused)

    # Queue cleared → vibe engine
    def _on_queue_changed() -> None:
        if queue.length() == 0:
            vibe.on_queue_cleared()
    queue.on_queue_changed.append(_on_queue_changed)

    # Playlist loaded via "Add all to queue" → vibe engine context (no shuffle state)
    def _on_playlist_started(playlist, shuffled: bool) -> None:  # noqa: ARG001
        vibe_on = _vibe_enabled[0]
        vibe.on_playlist_started(playlist.tracks, vibe_enabled=vibe_on)
        if vibe_on:
            window.set_vibe_seed(f"vibe: playlist · {playlist.name}")
    window._playlist_panel.playlist_started.connect(_on_playlist_started)

    # ── Playlist-shuffle mode ──────────────────────────────────────────────────

    _playlist_shuffle_active: list[bool] = [False]

    def _on_playlist_shuffle_ended() -> None:
        """Cleanup when playlist-shuffle mode is killed (button, stop, or skip)."""
        _playlist_shuffle_active[0] = False
        current = window.engine._current_track
        # Clear the playlist context BEFORE toggling vibe off.  on_vibe_toggled
        # checks _playlist_tracks and re-fires a fill thread when they're present
        # ("vibe off but playlist active" keeps the playlist-random fill running).
        # Clearing first ensures no new auto-suggestions are enqueued.
        vibe.on_playlist_ended()
        vibe.on_vibe_toggled(False, current)
        _vibe_enabled[0] = False
        # Remove ALL auto-suggestions — not just the first — so none of them
        # silently keep playing after the user ends the shuffle.
        for t in list(queue.snapshot()):
            if t.is_auto_suggestion:
                queue.remove(t.id)
        window.set_vibe_seed(None)
        window._playlist_panel.update_shuffle_btn_label(False)
    window._mode_bar.playlist_shuffle_ended.connect(_on_playlist_shuffle_ended)

    def _on_shuffle_playlist(playlist, start_track_id) -> None:
        """Handle 'Shuffle + play' or 'Shuffle from selected' from the playlist panel."""
        import random as _random

        tracks = [pt.to_track() for pt in playlist.tracks]
        if not tracks:
            return

        already_shuffling = _playlist_shuffle_active[0]

        if already_shuffling and start_track_id is not None:
            # "Shuffle from selected" while playlist-shuffle is already running.
            # Don't interrupt the current song — just swap the next queued track.
            for t in list(queue.snapshot()):
                if t.is_auto_suggestion:
                    queue.remove(t.id)
                    break
            pt = next((p for p in playlist.tracks if p.id == start_track_id), None)
            if pt:
                queue.enqueue(pt.to_track(), position=1)
            # Vibe engine keeps its playlist context — no state change needed.
            return

        # Fresh start: clear the queue entirely and enqueue one track.
        queue.clear()
        for t in list(queue.snapshot()):  # remove any lingering auto-suggestions
            queue.remove(t.id)

        if start_track_id is not None:
            pt = next((p for p in playlist.tracks if p.id == start_track_id), None)
            first = pt.to_track() if pt else _random.choice(tracks)
        else:
            first = _random.choice(tracks)

        queue.enqueue(first)

        # Tell the vibe engine to keep picking from this playlist (always enabled
        # in playlist-shuffle mode regardless of the normal vibe-match toggle).
        vibe.on_playlist_started(playlist.tracks, vibe_enabled=True)

        _playlist_shuffle_active[0] = True
        _vibe_needs_disarm[0] = False  # this path arms vibe on purpose — never auto-disarm it
        window._mode_bar.enter_playlist_shuffle_mode(playlist.name)
        window.set_vibe_seed(f"Shuffling playlist: {playlist.name}")
    window._playlist_panel.shuffle_playlist_requested.connect(_on_shuffle_playlist)

    # ── Background services ────────────────────────────────────────────────────
    server = _start_server(cfg, queue, window)

    # Wire mod panel transport controls → engine.
    # Must happen after both the server (which calls queue_routes.init) and
    # window.engine are ready.
    from server.routes import queue as queue_routes
    from player.engine import PlayState as _PlayState

    queue_routes.set_player_state_getter(lambda: window.engine.state.name)

    def _playpause():
        if window.engine.state == _PlayState.PLAYING:
            window.engine.pause()
        elif window.engine.state == _PlayState.PAUSED:
            window.engine.play()

    def _seek(delta: float):
        if window.engine.state in (_PlayState.PLAYING, _PlayState.PAUSED):
            elapsed, _ = window.engine.position
            window.engine.seek(elapsed + delta)

    queue_routes.set_playpause_callback(_playpause)
    queue_routes.set_seek_callback(_seek)
    queue_routes.set_position_getter(lambda: window.engine.position)
    queue_routes.set_pause_requests_callback(_on_requests_paused)
    queue_routes.set_pause_requests_getter(lambda: cfg.twitch.requests_paused)

    def _prev_track():
        if window.engine.state == _PlayState.STOPPED:
            recent = window._history.all_recent()
            if recent:
                window.engine.play_track(recent[0])
        else:
            prev = window._history.previous()
            if prev:
                window.engine.play_track(prev)
            else:
                window.engine.seek(0.0)

    queue_routes.set_prev_callback(_prev_track)

    # Engine state changes (play/pause/stop) must also push a WS update so
    # mod panels can enable/disable transport controls in real time.
    _prev_on_sc = window.engine.on_state_changed

    def _on_sc_with_ws(state) -> None:
        if _prev_on_sc:
            _prev_on_sc(state)
        queue_routes.schedule_broadcast()
        # When the engine stops naturally (queue ran dry) while playlist-shuffle
        # is active, the vibe engine is still running and will add the next track
        # shortly.  Keep the shuffle visual alive so the streamer doesn't see the
        # UI flicker to "nothing playing / vibe off" between tracks.
        # User-initiated stop goes through _on_stop → exit_playlist_shuffle_mode
        # and is handled separately; _user_stopped guards that path.
        from player.engine import PlayState as _PS
        if (state == _PS.STOPPED
                and _playlist_shuffle_active[0]
                and not window._user_stopped):
            # Restore the "Shuffling playlist…" seed label in case
            # _maybe_clear_now_playing ran and wiped it.
            if window._mode_bar._playlist_shuffle_mode:
                # Re-arm the label — use whatever name is in the vibe label text,
                # or fall back to a generic marker so the label stays visible.
                current_seed = window._now_playing._vibe_lbl.text()
                if not current_seed:
                    window.set_vibe_seed("Shuffling playlist…")

    window.engine.on_state_changed = _on_sc_with_ws

    # Push action-log updates to the desktop Actions tab.
    # log_action() fires from the FastAPI asyncio thread — marshal to Qt main thread.
    def _on_log_updated() -> None:
        # Emit a Signal — the only reliable way to marshal a call from the
        # FastAPI asyncio thread to the Qt main thread.  Qt auto-queues delivery.
        window._actions_log_ready.emit(queue_routes.get_action_log())

    queue_routes.on_log_updated(_on_log_updated)

    # Wire FFT frames → browser source broadcaster (no-op when no OBS clients)
    fft.add_frame_listener(spec_routes.push_bars_all)

    # Mutable containers — filled after the objects are created so the settings
    # server callbacks can reference them without circular dependencies.
    bot_ref:    list = [None]
    window_ref: list = [window]
    tunnel_ref: list = [None]   # holds the active TunnelBase instance, if any

    # Wire the settings server's spec_changed signal to repaint the spectrogram.
    # The signal is emitted from a FastAPI thread; Qt auto-queues the call to
    # the main thread via the signal/slot mechanism.
    window.spec_config_changed.connect(lambda: window._on_settings_applied(cfg))

    splash.step(75, "Starting mod panel server…")
    settings_server = _start_settings_server(cfg, bot_ref, window_ref, tunnel_ref)

    splash.step(82, "Connecting to Twitch…" if not _first else "Setting up Twitch integration…")
    bot = _start_bot(
        cfg, queue, window, vibe=vibe, tunnel_ref=tunnel_ref,
        vibe_needs_disarm=_vibe_needs_disarm,
        playlist_shuffle_active=_playlist_shuffle_active,
    )
    bot_ref[0] = bot

    # Wire bot status changes → settings WebSocket so the page updates live
    _orig_connected    = bot.on_connected
    _orig_disconnected = bot.on_disconnected
    _orig_auth_failed  = bot.on_auth_failed
    def _on_connected():
        if _orig_connected:
            _orig_connected()
        settings_server.broadcast({"type": "bot_status", "connected": True})
    def _on_disconnected():
        if _orig_disconnected:
            _orig_disconnected()
        settings_server.broadcast({"type": "bot_status", "connected": False})
    def _on_auth_failed(account: str):
        if _orig_auth_failed:
            _orig_auth_failed(account)
        settings_server.broadcast({"type": "bot_status", "connected": False})
    bot.on_connected    = _on_connected
    bot.on_disconnected = _on_disconnected
    bot.on_auth_failed  = _on_auth_failed

    # Wire channel-points reward-deleted warning → settings WebSocket.
    # _verify_reward() runs in the bot thread (after bot.start() has been
    # called) so settings_server is guaranteed to exist by the time it fires.
    def _on_cp_reward_deleted():
        settings_server.broadcast({
            "type":    "cp_reward_deleted",
            "message": (
                'The "Song Request" channel points reward was deleted from Twitch. '
                "Re-check the box in settings to create a new one."
            ),
        })
    bot.on_cp_reward_deleted = _on_cp_reward_deleted

    # Settings button in main window → open browser
    window.on_open_settings = settings_server.open_settings

    # Tunnel starts from on_connected (inside _start_bot) once the bot is
    # confirmed up — so the mod panel is only reachable from a live session.
    splash.step(90, "Waiting for Twitch…")

    # ── Global media hotkeys ───────────────────────────────────────────────────
    # The keyboard library fires callbacks on its own background thread, so we
    # emit a Qt signal instead of calling slots directly — the signal bridges
    # safely to the main thread via Qt's auto-connection queuing.
    splash.step(96, "Registering media hotkeys…")
    from player.hotkeys import HotkeyManager
    hotkeys = HotkeyManager()
    hotkeys.on_play_pause = lambda: window._hotkey_trigger.emit("play_pause")
    hotkeys.on_next_track = lambda: window._hotkey_trigger.emit("next")
    hotkeys.on_prev_track = lambda: window._hotkey_trigger.emit("prev")
    hotkeys.start()   # no-op + informational message when keyboard pkg missing

    # ── Update check (daemon — fires 15 s after boot, never blocks UI) ───────────
    import updater as _upd
    def _on_update(tag: str) -> None:
        settings_server.broadcast({"type": "update_available", "version": tag})
    _upd.start_background_check(on_update_found=_on_update)

    # ── Show + run ─────────────────────────────────────────────────────────────
    splash.step(100, "Ready.")
    splash.finish()
    window.show()
    result = app.exec()

    # ── Cleanup ────────────────────────────────────────────────────────────────
    hotkeys.stop()
    engine.stop()
    fft.stop()
    _stop_tunnel(tunnel_ref)
    server.stop()
    settings_server.stop()
    bot.stop()

    return result


if __name__ == "__main__":
    import traceback
    import pathlib
    import datetime
    from data_dir import DATA_DIR as _DATA_DIR
    _log_dir = pathlib.Path(_DATA_DIR)
    _log_dir.mkdir(parents=True, exist_ok=True)
    _crash_log = _log_dir / "crash.log"
    try:
        sys.exit(main())
    except BaseException as _exc:
        # SystemExit(0) is a clean quit — don't write a crash log for it.
        # SystemExit with a non-zero code or any other exception is a real crash.
        if isinstance(_exc, SystemExit) and (_exc.code == 0 or _exc.code is None):
            raise
        _ts = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            # Directory may have been deleted by data_wipe(); recreate it.
            _log_dir.mkdir(parents=True, exist_ok=True)
            _crash_log.write_text(f"[{_ts}]\n{traceback.format_exc()}\n")
        except OSError:
            pass
        raise
