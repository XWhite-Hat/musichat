"""
Settings server manager — wraps the settings FastAPI app in a uvicorn server
on a dedicated localhost-only port (default 8766).

Generates a one-time launch token for URL access control and exposes
`open_settings()` which opens the browser to the correct URL.
"""

from __future__ import annotations

import secrets
import threading
import webbrowser
from typing import Callable, Optional

import uvicorn

from config import AppConfig

SETTINGS_PORT = 8766
SETTINGS_HOST = "127.0.0.1"   # never bind 0.0.0.0 — settings must be local only


class SettingsServerManager:
    def __init__(
        self,
        cfg: AppConfig,
        bot_restart_cb:    Optional[Callable[[], None]] = None,
        spec_changed_cb:   Optional[Callable[[], None]] = None,
        tunnel_start_cb:   Optional[Callable[[], None]] = None,
        tunnel_stop_cb:    Optional[Callable[[], None]] = None,
        device_changed_cb: Optional[Callable] = None,
        data_reset_cb:     Optional[Callable[[], None]] = None,
        data_wipe_cb:      Optional[Callable[[], None]] = None,
        port: int = SETTINGS_PORT,
    ) -> None:
        self.cfg    = cfg
        self.port   = port
        self._token = secrets.token_urlsafe(16)
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server]   = None

        from server.settings_app import create_settings_app
        self.app = create_settings_app(
            cfg, self._token,
            bot_restart_cb=bot_restart_cb,
            spec_changed_cb=spec_changed_cb,
            tunnel_start_cb=tunnel_start_cb,
            tunnel_stop_cb=tunnel_stop_cb,
            device_changed_cb=device_changed_cb,
            data_reset_cb=data_reset_cb,
            data_wipe_cb=data_wipe_cb,
        )

    def start(self) -> None:
        uc = uvicorn.Config(
            self.app,
            host=SETTINGS_HOST,
            port=self.port,
            log_level="warning",
            log_config=None,
            loop="asyncio",
        )
        self._server = uvicorn.Server(uc)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5.0)

    def open_settings(self) -> None:
        """Open the settings page in the default browser."""
        url = f"http://{SETTINGS_HOST}:{self.port}/settings?token={self._token}"
        webbrowser.open(url)

    def broadcast(self, data: dict) -> None:
        """Push an event to all connected settings WebSocket clients."""
        try:
            ws_mgr = self.app.state.ws_manager
            ws_mgr.broadcast_sync(data)
        except AttributeError:
            pass
