"""
Global media hotkey manager.

Platform strategy
─────────────────
Windows   — WH_KEYBOARD_LL low-level keyboard hook via ctypes.
            Intercepts media key events system-wide BEFORE they reach any
            other application (including browsers via SMTC / WM_APPCOMMAND).
            The hook procedure returns 1 to suppress the key so only our app
            acts on it.  No extra packages required; no admin rights needed.

Other OS  — Optional ``keyboard`` package via ``keyboard.hook()``.
            Falls back gracefully (informational print) if unavailable.

Callbacks fire on a background thread; wire them to a thread-safe bridge
(e.g. a PyQt signal) before calling start().
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

# Windows Virtual-Key codes for the dedicated media cluster
_VK_MEDIA_NEXT_TRACK  = 0xB0
_VK_MEDIA_PREV_TRACK  = 0xB1
_VK_MEDIA_STOP        = 0xB2
_VK_MEDIA_PLAY_PAUSE  = 0xB3


class HotkeyManager:
    """Register global media-key hotkeys using the most reliable method available."""

    def __init__(self) -> None:
        self.on_play_pause: Optional[Callable[[], None]] = None
        self.on_next_track: Optional[Callable[[], None]] = None
        self.on_prev_track: Optional[Callable[[], None]] = None

        self._active          = False
        self._thread: Optional[threading.Thread] = None
        self._win32_tid: int  = 0
        self._win32_hook      = None   # keep HOOKPROC alive (prevents GC)
        self._kb_hooked       = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the hotkey listener.  Returns True on success."""
        if sys.platform == "win32":
            return self._start_win32()
        return self._start_keyboard_lib()

    def stop(self) -> None:
        """Unregister all hotkeys and stop background threads."""
        if not self._active:
            return
        self._active = False

        if sys.platform == "win32" and self._win32_tid:
            try:
                import ctypes
                # WM_QUIT (0x0012) breaks out of GetMessageW on the pump thread
                ctypes.windll.user32.PostThreadMessageW(
                    self._win32_tid, 0x0012, 0, 0
                )
            except Exception:
                pass

        if self._kb_hooked:
            try:
                import keyboard  # type: ignore[import]
                keyboard.unhook_all()
            except Exception:
                pass
            self._kb_hooked = False

    # ── Windows: WH_KEYBOARD_LL low-level hook ─────────────────────────────────

    def _start_win32(self) -> bool:
        """
        Install a system-wide WH_KEYBOARD_LL keyboard hook.

        Unlike RegisterHotKey, a low-level hook intercepts the key event
        *before* Windows dispatches WM_APPCOMMAND to the foreground window,
        so returning 1 from the hook procedure fully suppresses the key —
        Chrome/browsers will not see it.

        The hook proc must return quickly (< 300 ms) and the installing
        thread must pump messages, which we do via GetMessageW.
        """
        import ctypes
        import ctypes.wintypes

        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32  # used for GetCurrentThreadId

        WH_KEYBOARD_LL = 13
        WM_KEYDOWN     = 0x0100
        WM_SYSKEYDOWN  = 0x0104
        HC_ACTION      = 0

        # KBDLLHOOKSTRUCT (winuser.h)
        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode",      ctypes.wintypes.DWORD),
                ("scanCode",    ctypes.wintypes.DWORD),
                ("flags",       ctypes.wintypes.DWORD),
                ("time",        ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_int,
            ctypes.wintypes.WPARAM,
            ctypes.POINTER(KBDLLHOOKSTRUCT),
        )

        _vk_callbacks: dict[int, Callable[[], None]] = {
            _VK_MEDIA_PLAY_PAUSE: lambda: self._fire(self.on_play_pause),
            _VK_MEDIA_NEXT_TRACK: lambda: self._fire(self.on_next_track),
            _VK_MEDIA_PREV_TRACK: lambda: self._fire(self.on_prev_track),
        }

        hook_id = ctypes.c_void_p(0)
        ready   = threading.Event()

        def _proc(nCode: int, wParam: int, lParam) -> int:
            if nCode == HC_ACTION and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                vk = lParam.contents.vkCode
                cb = _vk_callbacks.get(vk)
                if cb is not None:
                    cb()
                    return 1   # suppress — key won't reach SMTC / WM_APPCOMMAND
            # Pass through all other keys unchanged
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        proc_c = HOOKPROC(_proc)

        def _pump() -> None:
            nonlocal hook_id
            self._win32_tid = kernel32.GetCurrentThreadId()
            self._win32_hook = proc_c   # keep alive on instance

            # hMod MUST be NULL for WH_KEYBOARD_LL — MSDN explicitly requires
            # this when the hook proc is inside the calling process rather than
            # a separate DLL.  Passing GetModuleHandleW(None) yields err=126
            # (ERROR_MOD_NOT_FOUND) from the Python interpreter host.
            hook_id = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                proc_c,
                None,  # NULL
                0,     # dwThreadId=0 → global hook
            )

            if not hook_id:
                err = kernel32.GetLastError()
                print(f"[hotkeys] SetWindowsHookExW failed (err={err}).")
                ready.set()
                return

            ready.set()

            msg = ctypes.wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break

            user32.UnhookWindowsHookEx(hook_id)
            self._win32_hook = None

        self._active = True
        self._thread = threading.Thread(
            target=_pump, name="hotkey-pump", daemon=True
        )
        self._thread.start()
        ready.wait(timeout=2.0)

        if not hook_id:
            self._active = False
            return False

        print("[hotkeys] global media hotkeys active (WH_KEYBOARD_LL).")
        return True

    # ── Non-Windows: keyboard library ─────────────────────────────────────────

    def _start_keyboard_lib(self) -> bool:
        """Use keyboard.hook() to catch media key events (Linux / macOS)."""
        try:
            import keyboard  # type: ignore[import]
        except (ImportError, Exception) as exc:
            if isinstance(exc, ImportError):
                print(
                    "[hotkeys] 'keyboard' package not installed — "
                    "global media keys disabled.  Run:  pip install keyboard"
                )
            else:
                print(f"[hotkeys] could not load keyboard package: {exc}")
            return False

        _PLAY = {"play/pause media", "media play/pause", "play pause", "playpause"}
        _NEXT = {"next track", "media next track", "nexttrack"}
        _PREV = {"previous track", "media previous track", "previoustrack",
                 "prev track"}

        def _on_key(event) -> None:
            if event.event_type != keyboard.KEY_DOWN:
                return
            raw  = (event.name or "").lower()
            norm = raw.replace(" ", "")
            if raw in _PLAY or norm in _PLAY:
                self._fire(self.on_play_pause)
            elif raw in _NEXT or norm in _NEXT:
                self._fire(self.on_next_track)
            elif raw in _PREV or norm in _PREV:
                self._fire(self.on_prev_track)

        try:
            keyboard.hook(_on_key, suppress=False)
            self._kb_hooked = True
            print("[hotkeys] global media hotkeys active (keyboard library).")
            return True
        except Exception as exc:
            print(f"[hotkeys] keyboard hook failed: {exc}")
            return False

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fire(cb: Optional[Callable[[], None]]) -> None:
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # noqa: BLE001
            print(f"[hotkeys] callback error: {exc}")
