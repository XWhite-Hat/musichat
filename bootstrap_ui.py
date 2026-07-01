"""
bootstrap_ui.py — customtkinter first-run wizard and PySide6 recovery dialogs.

Runs before PySide6 is available.  Depends only on customtkinter (MIT) which
is bundled into the binary separately.  tkinter is part of Python's stdlib
so it's always available in the PyInstaller bundle.

Public surface:
  run_setup_wizard(default_dir) -> str | None
      Full 3-step wizard: welcome → folder picker → download.
      Returns the chosen data_dir path on success, None on cancel/failure.

  run_recovery_dialog(data_dir, reason) -> str | None
      Recovery for "data_dir found but PySide6 missing or incomplete".
      reason is "missing" or "incomplete".  Returns data_dir to proceed
      (repaired/re-downloaded), a new path if the user chose to change
      folder, or None to abort.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from pyside_downloader import download_pyside6

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

_W, _H       = 540, 420
_W_RECOVERY          = 480, 260
_W_RECOVERY_REPAIR   = 480, 310
_FONT_TITLE  = ("Segoe UI", 18, "bold")
_FONT_BODY   = ("Segoe UI", 12)
_FONT_HINT   = ("Segoe UI", 10)
_FONT_MONO   = ("Consolas", 10)
_GREEN       = "#22c55e"
_MUTED       = "#6b7280"
_DANGER      = "#ef4444"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _center(win: ctk.CTk | ctk.CTkToplevel, w: int, h: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")


# ── Folder existence check ─────────────────────────────────────────────────────

def _check_existing_folder(path: str) -> dict:
    """Return which MusicHat artefacts already exist in *path*."""
    p = Path(path)
    return {
        "has_pyside6":   (p / "pyside6" / "PySide6").is_dir(),
        "has_config":    (p / "config.json").exists(),
        "has_playlists": (p / "playlists.json").exists(),
    }


class _ExistingFolderDialog(ctk.CTkToplevel):
    """
    Shown when the folder the user picked already contains MusicHat data.

    .proceed  — True if user wants to continue, False/None if cancelled.
    .use_existing_pyside6 — True if user wants to skip re-download.
    """

    def __init__(self, parent: ctk.CTk, existing: dict) -> None:
        super().__init__(parent)
        self.title("MusicHat — Existing Data Found")
        self.resizable(False, False)
        _center(self, 500, 300)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.transient(parent)
        self.grab_set()

        self.proceed = False
        self.use_existing_pyside6 = False

        items = []
        if existing["has_pyside6"]:
            items.append("PySide6 UI library")
        if existing["has_config"]:
            items.append("config.json (settings)")
        if existing["has_playlists"]:
            items.append("playlists.json")
        bullet_list = "\n".join(f"  •  {i}" for i in items)

        ctk.CTkLabel(self, text="Existing data found", font=_FONT_TITLE).pack(
            anchor="w", padx=28, pady=(24, 10))
        ctk.CTkLabel(
            self,
            text=f"This folder already contains:\n{bullet_list}\n\n"
                 "Your existing config and playlists will be kept.",
            font=_FONT_BODY, justify="left", wraplength=448,
        ).pack(anchor="w", padx=28, pady=(0, 10))

        self._pyside_var = ctk.BooleanVar(value=existing["has_pyside6"])
        if existing["has_pyside6"]:
            ctk.CTkCheckBox(
                self,
                text="Use existing PySide6 (skip re-download)",
                variable=self._pyside_var,
                font=_FONT_BODY,
            ).pack(anchor="w", padx=28, pady=(0, 18))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=28, pady=(0, 20))
        ctk.CTkButton(btn_frame, text="Cancel", width=100,
                      fg_color="transparent", border_width=1,
                      command=self._cancel).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_frame, text="Continue", width=120,
                      command=self._confirm).pack(side="right")

    def _confirm(self) -> None:
        self.proceed = True
        self.use_existing_pyside6 = self._pyside_var.get()
        self.destroy()

    def _cancel(self) -> None:
        self.proceed = False
        self.destroy()


# ── Setup wizard ───────────────────────────────────────────────────────────────

class _SetupWizard(ctk.CTk):
    """3-step first-run wizard.  .result is the data_dir str on success."""

    def __init__(self, default_dir: str) -> None:
        super().__init__()
        self.title("MusicHat — First Run Setup")
        self.resizable(False, False)
        _center(self, _W, _H)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.result: Optional[str] = None
        self._chosen_dir = default_dir
        self._cancel_download = threading.Event()

        # Container holds one frame at a time
        self._container = ctk.CTkFrame(self, fg_color="transparent")
        self._container.pack(fill="both", expand=True, padx=32, pady=24)

        self._pages: list[ctk.CTkFrame] = []
        self._build_welcome()
        self._build_folder()
        self._build_download()
        self._build_done()
        self._show_page(0)

    # ── Page construction ──────────────────────────────────────────────────────

    def _build_welcome(self) -> None:
        f = ctk.CTkFrame(self._container, fg_color="transparent")
        self._pages.append(f)

        ctk.CTkLabel(f, text="Welcome to MusicHat", font=_FONT_TITLE).pack(anchor="w", pady=(0, 14))

        body = (
            "Before MusicHat can start, it needs a folder to store your data:\n\n"
            "  •  Playlists and settings\n"
            "  •  Authentication tokens\n"
            "  •  The PySide6 UI library  (~150 MB download)\n\n"
            "Keeping PySide6 in your data folder means you can update or replace\n"
            "it independently — a requirement of its open-source licence.\n\n"
            "You can move this folder later from  Settings → Local Data."
        )
        ctk.CTkLabel(f, text=body, font=_FONT_BODY, justify="left",
                     wraplength=_W - 80).pack(anchor="w", pady=(0, 24))

        btn_frame = ctk.CTkFrame(f, fg_color="transparent")
        btn_frame.pack(fill="x")
        ctk.CTkButton(
            btn_frame, text="Choose a folder  →", width=180,
            command=lambda: self._show_page(1),
        ).pack(side="right")

    def _build_folder(self) -> None:
        f = ctk.CTkFrame(self._container, fg_color="transparent")
        self._pages.append(f)

        ctk.CTkLabel(f, text="Choose a data folder", font=_FONT_TITLE).pack(anchor="w", pady=(0, 14))

        ctk.CTkLabel(f, text="MusicHat will store all its data here:", font=_FONT_BODY,
                     justify="left").pack(anchor="w", pady=(0, 6))

        path_row = ctk.CTkFrame(f, fg_color="transparent")
        path_row.pack(fill="x", pady=(0, 8))
        self._path_var = ctk.StringVar(value=self._chosen_dir)
        self._path_entry = ctk.CTkEntry(
            path_row, textvariable=self._path_var,
            font=_FONT_MONO, width=340,
        )
        self._path_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            path_row, text="Browse…", width=90,
            command=self._browse,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            f,
            text="About 200 MB of free space is needed (mostly the PySide6 library).",
            font=_FONT_HINT, text_color=_MUTED,
        ).pack(anchor="w", pady=(0, 20))

        btn_frame = ctk.CTkFrame(f, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom")
        ctk.CTkButton(btn_frame, text="← Back", width=100, fg_color="transparent",
                      border_width=1, command=lambda: self._show_page(0)).pack(side="left")
        ctk.CTkButton(btn_frame, text="Download & set up  →", width=180,
                      command=self._start_download).pack(side="right")

    def _build_download(self) -> None:
        f = ctk.CTkFrame(self._container, fg_color="transparent")
        self._pages.append(f)

        ctk.CTkLabel(f, text="Setting up MusicHat…", font=_FONT_TITLE).pack(anchor="w", pady=(0, 20))

        self._dl_status = ctk.StringVar(value="Starting…")
        ctk.CTkLabel(f, textvariable=self._dl_status, font=_FONT_BODY,
                     wraplength=_W - 80, justify="left").pack(anchor="w", pady=(0, 12))

        self._progress_bar = ctk.CTkProgressBar(f, width=_W - 80)
        self._progress_bar.pack(anchor="w", pady=(0, 6))
        self._progress_bar.set(0)

        self._progress_label = ctk.StringVar(value="")
        ctk.CTkLabel(f, textvariable=self._progress_label,
                     font=_FONT_HINT, text_color=_MUTED).pack(anchor="w", pady=(0, 20))

        self._error_label = ctk.CTkLabel(f, text="", font=_FONT_HINT,
                                          text_color=_DANGER, wraplength=_W - 80, justify="left")
        self._error_label.pack(anchor="w")

        btn_frame = ctk.CTkFrame(f, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom")
        self._cancel_btn = ctk.CTkButton(
            btn_frame, text="Cancel", width=100, fg_color="transparent",
            border_width=1, command=self._cancel,
        )
        self._cancel_btn.pack(side="right")
        self._retry_btn = ctk.CTkButton(
            btn_frame, text="← Try again", width=140,
            command=lambda: self._show_page(1),
        )
        self._retry_btn.pack(side="left")
        self._retry_btn.pack_forget()

    def _build_done(self) -> None:
        f = ctk.CTkFrame(self._container, fg_color="transparent")
        self._pages.append(f)

        ctk.CTkLabel(f, text="All set!", font=_FONT_TITLE,
                     text_color=_GREEN).pack(anchor="w", pady=(0, 14))
        ctk.CTkLabel(
            f,
            text="MusicHat is ready to use.\n\n"
                 "Your data folder has been set up with the PySide6 library.\n"
                 "You can move this folder later from  Settings → Local Data.",
            font=_FONT_BODY, justify="left", wraplength=_W - 80,
        ).pack(anchor="w", pady=(0, 32))

        btn_frame = ctk.CTkFrame(f, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom")
        ctk.CTkButton(
            btn_frame, text="Launch MusicHat  →", width=200,
            command=self._finish,
        ).pack(side="right")

    # ── Actions ────────────────────────────────────────────────────────────────

    def _show_page(self, idx: int) -> None:
        for p in self._pages:
            p.pack_forget()
        self._pages[idx].pack(fill="both", expand=True)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose MusicHat data folder",
            initialdir=self._path_var.get() or os.path.expanduser("~"),
        )
        if chosen:
            self._path_var.set(chosen)
            self._chosen_dir = chosen

    def _start_download(self) -> None:
        self._chosen_dir = self._path_var.get().strip()
        if not self._chosen_dir:
            return

        existing = _check_existing_folder(self._chosen_dir)
        if any(existing.values()):
            dlg = _ExistingFolderDialog(self, existing)
            self.wait_window(dlg)
            if not dlg.proceed:
                return
            if dlg.use_existing_pyside6:
                # PySide6 already in place — skip download, go straight to done.
                self._show_page(3)
                return

        self._cancel_download.clear()
        self._error_label.configure(text="")
        self._retry_btn.pack_forget()
        self._cancel_btn.configure(state="normal")
        self._progress_bar.set(0)
        self._progress_label.set("")
        self._show_page(2)
        threading.Thread(target=self._download_thread, daemon=True).start()

    def _download_thread(self) -> None:
        target = Path(self._chosen_dir) / "pyside6"

        def on_progress(done: int, total: int, msg: str) -> None:
            if self._cancel_download.is_set():
                return
            self.after(0, self._dl_status.set, msg)
            if total > 0:
                self.after(0, self._progress_bar.set, done / total)
                mb_done  = done  / 1_048_576
                mb_total = total / 1_048_576
                self.after(0, self._progress_label.set,
                           f"{mb_done:.1f} MB / {mb_total:.1f} MB")
            else:
                self.after(0, self._progress_bar.set, 0)
                self.after(0, self._progress_label.set, "")

        ok, msg = download_pyside6(target, on_progress)

        if self._cancel_download.is_set():
            return

        if ok:
            self.after(0, self._on_download_success)
        else:
            self.after(0, self._on_download_error, msg)

    def _on_download_success(self) -> None:
        self._show_page(3)

    def _on_download_error(self, msg: str) -> None:
        self._dl_status.set("Download failed.")
        self._error_label.configure(text=msg)
        self._cancel_btn.configure(state="disabled")
        self._retry_btn.pack(side="left")

    def _cancel(self) -> None:
        self._cancel_download.set()
        self.result = None
        self.destroy()

    def _finish(self) -> None:
        self.result = self._chosen_dir
        self.destroy()

    def _on_close(self) -> None:
        self._cancel_download.set()
        self.result = None
        self.destroy()


# ── Recovery dialog ────────────────────────────────────────────────────────────

class _RecoveryDialog(ctk.CTk):
    """
    Shown when bootstrap_check finds a valid data_dir but PySide6 is missing
    or the install is incomplete.

    reason:
      "missing"    — the PySide6 directories do not exist at all
      "incomplete" — directories exist but the install is broken/partial

    .result:
      "download"       — user chose to download / repair
      "manual"         — user will install manually; show instructions then exit
      "change_folder"  — user wants to pick a different folder (caller runs wizard)
      None             — window closed
    """

    def __init__(self, data_dir: str, reason: str = "missing") -> None:
        super().__init__()
        self.resizable(False, False)
        win_size = _W_RECOVERY_REPAIR if reason == "incomplete" else _W_RECOVERY
        _center(self, *win_size)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.result: Optional[str] = None
        self._data_dir = data_dir
        self._reason   = reason

        pad = dict(padx=28, pady=14)

        if reason == "incomplete":
            self.title("MusicHat — PySide6 Incomplete Installation")
            heading    = "PySide6 installation incomplete"
            body       = (
                f"MusicHat found your data folder:\n  {data_dir}\n\n"
                "A previous PySide6 installation is present but appears\n"
                "incomplete or damaged — it may have been interrupted or\n"
                "partially removed.\n\n"
                "MusicHat cannot start until PySide6 is repaired."
            )
            action_btn = "Repair now"
        else:
            self.title("MusicHat — PySide6 Missing")
            heading    = "PySide6 library not found"
            body       = (
                f"MusicHat found your data folder:\n  {data_dir}\n\n"
                "But the PySide6 UI library is missing from it.\n"
                "MusicHat cannot run without it."
            )
            action_btn = "Download now"

        ctk.CTkLabel(self, text=heading, font=_FONT_TITLE,
                     text_color=_DANGER).pack(anchor="w", **pad)
        ctk.CTkLabel(self, text=body, font=_FONT_BODY,
                     justify="left").pack(anchor="w", padx=28, pady=(0, 14))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=28, pady=(0, 20))
        ctk.CTkButton(btn_frame, text=action_btn,
                      command=self._download).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_frame, text="I'll install it myself", fg_color="transparent",
                      border_width=1,
                      command=self._manual).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_frame, text="Change folder", fg_color="transparent",
                      border_width=1,
                      command=self._change).pack(side="left")

    def _download(self) -> None:
        self.result = "download"
        self.destroy()

    def _manual(self) -> None:
        self.result = "manual"
        self.destroy()

    def _change(self) -> None:
        self.result = "change_folder"
        self.destroy()

    def _close(self) -> None:
        self.result = None
        self.destroy()


class _ManualInstallNotice(ctk.CTk):
    """
    Shown when the user chooses "I'll install it myself" so they know
    where to put PySide6 and that the app won't start without it.
    """

    def __init__(self, pyside6_path: str) -> None:
        super().__init__()
        self.title("MusicHat — Manual Install Instructions")
        self.resizable(False, False)
        _center(self, 500, 260)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        pad = dict(padx=28, pady=12)
        ctk.CTkLabel(self, text="Manual PySide6 installation",
                     font=_FONT_TITLE).pack(anchor="w", **pad)
        ctk.CTkLabel(
            self,
            text=(
                "Run the following command, then restart MusicHat:\n\n"
                f"  pip install PySide6 --target \"{pyside6_path}\"\n\n"
                "MusicHat will not start until PySide6 is present in that folder."
            ),
            font=_FONT_BODY, justify="left", wraplength=460,
        ).pack(anchor="w", padx=28, pady=(0, 16))
        ctk.CTkButton(self, text="OK", width=100, command=self.destroy).pack(
            anchor="e", padx=28, pady=(0, 20)
        )
        self.mainloop()


# ── Download-only dialog (for recovery after wizard selects existing dir) ─────

class _DownloadOnlyDialog(ctk.CTk):
    """
    Download / repair progress dialog.

    mode:
      "download" — fresh install ("Downloading PySide6…")
      "repair"   — fixing a damaged install ("Repairing PySide6…")
    """

    def __init__(self, data_dir: str, mode: str = "download") -> None:
        super().__init__()
        verb = "Repairing" if mode == "repair" else "Downloading"
        self.title(f"MusicHat — {verb} PySide6")
        self.resizable(False, False)
        _center(self, _W, 280)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no close during DL

        self.result: Optional[str] = None
        self._data_dir = data_dir
        self._cancel_ev = threading.Event()

        pad = dict(padx=32, pady=10)

        ctk.CTkLabel(self, text=f"{verb} PySide6…",
                     font=_FONT_TITLE).pack(anchor="w", padx=32, pady=(24, 10))

        self._status_var = ctk.StringVar(value="Starting…")
        ctk.CTkLabel(self, textvariable=self._status_var,
                     font=_FONT_BODY, wraplength=_W - 80,
                     justify="left").pack(anchor="w", **pad)

        self._bar = ctk.CTkProgressBar(self, width=_W - 80)
        self._bar.pack(anchor="w", padx=32, pady=(0, 4))
        self._bar.set(0)

        self._bytes_var = ctk.StringVar(value="")
        ctk.CTkLabel(self, textvariable=self._bytes_var,
                     font=_FONT_HINT, text_color=_MUTED).pack(anchor="w", **pad)

        self._error_lbl = ctk.CTkLabel(self, text="", font=_FONT_HINT,
                                        text_color=_DANGER, wraplength=_W - 80)
        self._error_lbl.pack(anchor="w", padx=32)

        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        target = Path(self._data_dir) / "pyside6"

        def on_progress(done: int, total: int, msg: str) -> None:
            self.after(0, self._status_var.set, msg)
            if total > 0:
                self.after(0, self._bar.set, done / total)
                self.after(0, self._bytes_var.set,
                           f"{done/1_048_576:.1f} MB / {total/1_048_576:.1f} MB")

        ok, msg = download_pyside6(target, on_progress)
        if ok:
            self.result = self._data_dir
            self.after(0, self.destroy)
        else:
            self.after(0, self._error_lbl.configure, {"text": f"Failed: {msg}"})
            self.after(0, self._status_var.set, "Download failed.")

    def run(self) -> Optional[str]:
        self.mainloop()
        return self.result


# ── Public API ─────────────────────────────────────────────────────────────────

def run_setup_wizard(default_dir: str) -> Optional[str]:
    """
    Show the full first-run wizard.  Returns the chosen data_dir on success
    (PySide6 downloaded and verified), None if the user cancelled.
    """
    wizard = _SetupWizard(default_dir)
    wizard.mainloop()
    return wizard.result


def run_recovery_dialog(data_dir: str, reason: str = "missing") -> Optional[str]:
    """
    Handle "data_dir found but PySide6 missing or incomplete".

    reason:
      "missing"    — PySide6 directories are absent
      "incomplete" — directories exist but install is damaged/partial

    Returns:
      str  — the resolved data_dir if PySide6 is now in place
      None — user chose to exit / closed the window
    """
    while True:
        dlg = _RecoveryDialog(data_dir, reason=reason)
        dlg.mainloop()
        action = dlg.result

        if action is None:
            return None

        if action == "manual":
            pyside6_path = str(Path(data_dir) / "pyside6")
            _ManualInstallNotice(pyside6_path)
            return None

        if action == "change_folder":
            # Run the full wizard so they can pick a new folder + download
            result = run_setup_wizard(data_dir)
            return result  # None if they cancelled again

        if action == "download":
            mode = "repair" if reason == "incomplete" else "download"
            dl = _DownloadOnlyDialog(data_dir, mode=mode)
            result = dl.run()
            if result:
                return result
            # Download/repair failed — loop back so they can try again
            continue
