"""
Playlists tab — browse, create, rename, delete playlists and their tracks.

Layout
──────
  ┌─ sidebar (playlists) ─┬─ track list ──────────────────────────────────┐
  │  Chill Beats          │  1.  Artist — Title          [3:45]           │
  │  Hype Train       ◀   │  2.  Artist — Title          [4:12]           │
  │  Late Night           │  ...                                           │
  │  ...                  │                                                │
  │  [+ New Playlist]     │  [▶ Play all]  [+ Add all to queue]  [⇀ Shuffle] │
  └───────────────────────┴────────────────────────────────────────────────┘
"""

from __future__ import annotations

import re as _re

_TOPIC_SUFFIX = _re.compile(r'\s*[-–]\s*Topic\s*$', _re.IGNORECASE)

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from player.playlist_manager import Playlist, PlaylistManager, PlaylistTrack
from player.queue_manager import QueueManager


class PlaylistPanel(QWidget):
    """Full playlist management widget — intended as a tab in MainWindow."""

    # Emitted when user wants to play a single track directly (bypasses queue)
    play_track_requested = Signal(object)   # Track

    # Emitted when the user adds all tracks to the queue (no shuffle state change).
    # Carries (Playlist, shuffle: bool).  Used by the vibe engine to set its
    # playlist context so it can pick vibe seeds from playlist tracks.
    playlist_started = Signal(object, bool)

    # Emitted when "Shuffle + play" or "Shuffle from selected" is clicked.
    # Carries (Playlist, start_track_id: str | None).
    # None means "pick a random starting track"; a str is a PlaylistTrack.id.
    shuffle_playlist_requested = Signal(object, object)

    def __init__(
        self,
        playlist_manager: PlaylistManager,
        queue_manager: QueueManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pm = playlist_manager
        self._qm = queue_manager
        self._selected_playlist: Playlist | None = None

        self._build_ui()
        self._pm.on_changed.append(self._refresh_playlists)
        self._refresh_playlists()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ── Left: playlist list ──
        left = QWidget()
        left.setMinimumWidth(160)
        left.setMaximumWidth(220)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)

        pl_label = QLabel("Playlists")
        pl_label.setObjectName("heading")
        left_layout.addWidget(pl_label)

        self._pl_list = QListWidget()
        self._pl_list.setAlternatingRowColors(True)
        self._pl_list.currentRowChanged.connect(self._on_playlist_selected)
        self._pl_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._pl_list.customContextMenuRequested.connect(self._playlist_context_menu)
        left_layout.addWidget(self._pl_list, 1)

        new_btn = QPushButton("+ New Playlist")
        new_btn.setObjectName("accent")
        new_btn.clicked.connect(self._create_playlist)
        left_layout.addWidget(new_btn)

        self._import_btn = QPushButton("↓ Import URL")
        self._import_btn.clicked.connect(self._import_playlist)
        left_layout.addWidget(self._import_btn)

        splitter.addWidget(left)

        # ── Right: track list ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)

        self._pl_name_lbl = QLabel("Select a playlist")
        self._pl_name_lbl.setObjectName("heading")
        right_layout.addWidget(self._pl_name_lbl)

        self._track_list = QListWidget()
        self._track_list.setAlternatingRowColors(True)
        self._track_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._track_list.model().rowsMoved.connect(self._on_tracks_reordered)
        self._track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._track_list.customContextMenuRequested.connect(self._track_context_menu)
        self._track_list.itemDoubleClicked.connect(self._play_track_double_click)
        self._track_list.currentItemChanged.connect(lambda *_: self._update_shuffle_from_btn())
        right_layout.addWidget(self._track_list, 1)

        btn_row = QHBoxLayout()

        # Shuffle + play takes the accent (primary) position.
        # Width is fixed to the widest label so the button never resizes
        # when the vibe-match state changes.
        self._shuffle_btn = QPushButton("⇀ Shuffle + play")
        self._shuffle_btn.setObjectName("accent")
        self._shuffle_btn.ensurePolished()
        self._shuffle_btn.setText("⇀ Shuffle + play (vibe-matched)")
        self._shuffle_btn.setFixedWidth(self._shuffle_btn.sizeHint().width())
        self._shuffle_btn.setText("⇀ Shuffle + play")
        self._shuffle_btn.clicked.connect(self._shuffle_play)

        self._queue_all_btn = QPushButton("+ Add all to queue")
        self._queue_all_btn.clicked.connect(self._queue_all)

        self._shuffle_from_selected_btn = QPushButton("⇀ Shuffle from selected")
        self._shuffle_from_selected_btn.setEnabled(False)
        self._shuffle_from_selected_btn.clicked.connect(self._shuffle_from_selected)

        btn_row.addWidget(self._shuffle_btn)
        btn_row.addWidget(self._queue_all_btn)
        btn_row.addWidget(self._shuffle_from_selected_btn)
        btn_row.addStretch()
        right_layout.addLayout(btn_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    # ── Refresh ────────────────────────────────────────────────────────────────

    def _refresh_playlists(self) -> None:
        prev_id = self._selected_playlist.id if self._selected_playlist else None
        self._pl_list.blockSignals(True)
        self._pl_list.clear()
        playlists = self._pm.playlists()
        select_row = 0
        for i, pl in enumerate(playlists):
            dur = pl.total_duration()
            m, s = divmod(dur, 60)
            h, m = divmod(m, 60)
            dur_str = f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'
            marker = "♪ " if pl.is_local else ""
            item = QListWidgetItem(f"{marker}{pl.name}  ({pl.track_count()} tracks)")
            item.setData(Qt.ItemDataRole.UserRole, pl.id)
            kind = "local files" if pl.is_local else "streaming"
            item.setToolTip(f"{pl.track_count()} tracks · {dur_str} · {kind}")
            self._pl_list.addItem(item)
            if pl.id == prev_id:
                select_row = i
        self._pl_list.blockSignals(False)
        if playlists:
            self._pl_list.setCurrentRow(select_row)
        else:
            self._selected_playlist = None
            self._refresh_tracks()

    def _sync_import_button(self) -> None:
        """Label the import action for whatever the selected playlist accepts."""
        pl = self._selected_playlist
        if pl is not None and pl.is_local:
            self._import_btn.setText("↓ Add local files")
            self._import_btn.setToolTip("Add audio files from this computer")
        else:
            self._import_btn.setText("↓ Import URL")
            self._import_btn.setToolTip("Import a YouTube or SoundCloud playlist")

    def _refresh_tracks(self) -> None:
        self._track_list.clear()
        self._sync_import_button()
        if self._selected_playlist is None:
            self._pl_name_lbl.setText("Select a playlist")
            return
        pl = self._pm.get(self._selected_playlist.id)
        if pl is None:
            self._selected_playlist = None
            self._pl_name_lbl.setText("Select a playlist")
            return
        self._selected_playlist = pl
        self._pl_name_lbl.setText(pl.name)
        for i, t in enumerate(pl.tracks, 1):
            dur = t.duration_seconds
            m, s = divmod(dur, 60)
            title_line  = f"  {i:>3}.  {t.title or '(unknown)'}  [{m}:{s:02d}]"
            artist_line = f"       {t.artist}" if t.artist else ""
            label = title_line + ("\n" + artist_line if artist_line else "")
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(0, 44))
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            item.setToolTip(t.stream_url)
            self._track_list.addItem(item)
        self._update_shuffle_from_btn()

    # ── Playlist selection ─────────────────────────────────────────────────────

    def _on_playlist_selected(self, row: int) -> None:
        item = self._pl_list.item(row)
        if item is None:
            self._selected_playlist = None
        else:
            pl_id = item.data(Qt.ItemDataRole.UserRole)
            self._selected_playlist = self._pm.get(pl_id)
        self._refresh_tracks()

    # ── Playlist context menu ──────────────────────────────────────────────────

    def _playlist_context_menu(self, pos) -> None:
        item = self._pl_list.itemAt(pos)
        if item is None:
            return
        pl_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Rename", lambda: self._rename_playlist(pl_id))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self._delete_playlist(pl_id))
        menu.exec(self._pl_list.mapToGlobal(pos))

    def _create_playlist(self) -> None:
        dlg = NewPlaylistDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, is_local = dlg.result_values()
        if name:
            self._pm.create(name, is_local=is_local)

    def _import_playlist(self) -> None:
        """Import into the selected playlist, using whichever source it accepts.

        A local-only playlist takes files from disk; a streaming playlist takes
        a URL.  The two are never offered together — that separation is the
        whole point of the local-only flag.
        """
        pl = self._selected_playlist
        if pl is not None and pl.is_local:
            ImportLocalFilesDialog(self._pm, pl, self).exec()
            return
        ImportPlaylistDialog(self._pm, self).exec()

    def _rename_playlist(self, pl_id: str) -> None:
        pl = self._pm.get(pl_id)
        if pl is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename Playlist", "New name:", text=pl.name
        )
        if ok and name.strip():
            self._pm.rename(pl_id, name.strip())

    def _delete_playlist(self, pl_id: str) -> None:
        pl = self._pm.get(pl_id)
        if pl is None:
            return
        reply = QMessageBox.question(
            self,
            "Delete playlist",
            f"Delete '{pl.name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._pm.delete(pl_id)

    # ── Track context menu ─────────────────────────────────────────────────────

    def _track_context_menu(self, pos) -> None:
        if self._selected_playlist is None:
            return
        item = self._track_list.itemAt(pos)
        if item is None:
            return
        track_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Play now", lambda: self._play_track_by_id(track_id))
        menu.addAction("Add to queue", lambda: self._queue_track_by_id(track_id))
        menu.addSeparator()
        menu.addAction("Remove from playlist", lambda: self._remove_track(track_id))
        menu.exec(self._track_list.mapToGlobal(pos))

    def _play_track_by_id(self, track_id: str) -> None:
        if self._selected_playlist is None:
            return
        for pt in self._selected_playlist.tracks:
            if pt.id == track_id:
                self.play_track_requested.emit(pt.to_track())
                return

    def _queue_track_by_id(self, track_id: str) -> None:
        if self._selected_playlist is None:
            return
        for pt in self._selected_playlist.tracks:
            if pt.id == track_id:
                self._qm.enqueue(pt.to_track())
                return

    def _remove_track(self, track_id: str) -> None:
        if self._selected_playlist is None:
            return
        self._pm.remove_track(self._selected_playlist.id, track_id)

    def _play_track_double_click(self, item: QListWidgetItem) -> None:
        track_id = item.data(Qt.ItemDataRole.UserRole)
        self._play_track_by_id(track_id)

    # ── Drag-drop reorder ──────────────────────────────────────────────────────

    def _on_tracks_reordered(self, parent, from_start, from_end, dest, dest_row) -> None:
        if self._selected_playlist is None:
            return
        self._pm.move_track(self._selected_playlist.id, from_start, dest_row)

    # ── Bulk actions ───────────────────────────────────────────────────────────

    def _queue_all(self) -> None:
        if self._selected_playlist is None:
            return
        count = self._pm.enqueue_all(self._selected_playlist.id, self._qm)
        print(f"[playlist] queued {count} tracks from '{self._selected_playlist.name}'")
        self.playlist_started.emit(self._selected_playlist, False)

    def _shuffle_play(self) -> None:
        if self._selected_playlist is None:
            return
        self.shuffle_playlist_requested.emit(self._selected_playlist, None)

    def _shuffle_from_selected(self) -> None:
        if self._selected_playlist is None:
            return
        item = self._track_list.currentItem()
        if item is None:
            return
        track_id = item.data(Qt.ItemDataRole.UserRole)
        self.shuffle_playlist_requested.emit(self._selected_playlist, track_id)

    def _update_shuffle_from_btn(self) -> None:
        """Enable the 'Shuffle from selected' button iff a track in the current playlist is selected."""
        item = self._track_list.currentItem()
        self._shuffle_from_selected_btn.setEnabled(item is not None)

    def update_shuffle_btn_label(self, vibe_on: bool) -> None:
        """Update the shuffle button label to reflect current vibe-match state."""
        self._shuffle_btn.setText(
            "⇀ Shuffle + play (vibe-matched)" if vibe_on else "⇀ Shuffle + play"
        )


# ── Playlist URL importer ──────────────────────────────────────────────────────

_UNAVAIL_TITLES = frozenset({"[deleted video]", "[private video]", "[unavailable]"})


def _is_unavailable(entry: dict | None) -> bool:
    if entry is None:
        return True
    title = (entry.get("title") or "").strip().lower()
    if title in _UNAVAIL_TITLES:
        return True
    avail = (entry.get("availability") or "").lower()
    return avail in ("needs_auth", "subscriber_only", "premium_only", "unavailable")


# YouTube video IDs are exactly 11 URL-safe base64 characters.
# Channel IDs (UC…, 24 chars) and playlist IDs (PL…, RD…) are longer/different,
# so they cleanly fail this pattern.  SoundCloud IDs are pure-numeric.
_YT_VIDEO_ID_RE = _re.compile(r'^[A-Za-z0-9_-]{11}$')


def _is_playable(entry: dict | None) -> bool:
    """Return False for non-track entries (channel pages, mixes, etc.)."""
    if entry is None:
        return False
    vid_id = entry.get("id") or ""
    if not _YT_VIDEO_ID_RE.match(vid_id) and not vid_id.isdigit():
        return False  # channel / playlist ID — not a playable video
    # Entries with an explicit zero duration are non-video metadata rows.
    # Allow None/missing — yt-dlp sometimes omits duration for valid tracks.
    duration = entry.get("duration")
    return not (duration is not None and duration == 0)


def _entry_to_playlist_track(entry: dict) -> PlaylistTrack:
    url = (
        entry.get("url")
        or entry.get("webpage_url")
        or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else "")
    )
    url = url.replace("music.youtube.com", "www.youtube.com")
    # extract_flat omits thumbnails for individual entries.  Construct from the
    # video ID when absent — maxresdefault is tried first; overlays.py's
    # _youtube_fallback_urls degrades to hqdefault/sddefault automatically on 404.
    vid_id = entry.get("id", "")
    is_yt  = "youtube" in url or "youtu.be" in url
    thumb  = (
        entry.get("thumbnail")
        or (entry.get("thumbnails") or [{}])[-1].get("url", "")
        or (f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg" if vid_id and is_yt else "")
    )
    raw_artist = (
        (entry.get("artist") or "").strip()
        or (entry.get("uploader") or "").strip()
        or (entry.get("channel") or "").strip()
    )
    artist = _TOPIC_SUFFIX.sub("", raw_artist).strip()
    return PlaylistTrack(
        title=entry.get("title") or "Unknown",
        artist=artist,
        stream_url=url,
        thumbnail_url=thumb,
        duration_seconds=int(entry.get("duration") or 0),
        source="YOUTUBE" if is_yt else "SOUNDCLOUD",
    )


class _PlaylistFetchThread(QThread):
    """Background thread: resolves a playlist URL via yt-dlp."""

    finished = Signal(object)   # dict | None

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    def run(self) -> None:
        from player.ytdlp_util import resolve_playlist
        self.finished.emit(resolve_playlist(self._url))


class NewPlaylistDialog(QDialog):
    """Name a new playlist and choose whether it holds local files or streams.

    The choice is made up front and not changed afterwards: converting a
    playlist between kinds would mean either discarding its contents or
    carrying both, and carrying both is exactly what the split avoids.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Playlist")
        self.setMinimumWidth(420)
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        lay.addWidget(QLabel("Playlist name:"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("New Playlist")
        self._name.returnPressed.connect(self._accept_if_named)
        lay.addWidget(self._name)

        self._local = QCheckBox("Local files only (music from this computer)")
        lay.addWidget(self._local)

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        self._hint.setObjectName("hint")
        lay.addWidget(self._hint)
        self._local.toggled.connect(self._update_hint)
        self._update_hint(False)

        foot = QHBoxLayout()
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Create")
        create.setObjectName("accent")
        create.clicked.connect(self._accept_if_named)
        foot.addWidget(cancel)
        foot.addWidget(create)
        lay.addLayout(foot)

    def _update_hint(self, checked: bool) -> None:
        self._hint.setText(
            "Holds audio files from your computer. Chat cannot request from it, "
            "and file paths are never shown in the mod panel."
            if checked else
            "Holds YouTube and SoundCloud tracks. This is the usual choice."
        )

    def _accept_if_named(self) -> None:
        if self._name.text().strip():
            self.accept()

    def result_values(self) -> tuple[str, bool]:
        return self._name.text().strip(), self._local.isChecked()


class _LocalProbeThread(QThread):
    """Background thread: reads metadata from a list of audio files.

    Probing opens and closes a container per file — roughly 10ms each, which is
    nothing for twenty files and several seconds for a few thousand.  Doing it
    on the Qt thread would look exactly like a freeze, so it runs here and
    reports progress as it goes.
    """

    progress = Signal(int, int)          # done, total
    finished = Signal(object)            # list[PlaylistTrack]

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self._paths = paths
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from player.local_media import probe
        from player.queue_manager import TrackSource

        out: list[PlaylistTrack] = []
        total = len(self._paths)
        for i, path in enumerate(self._paths, 1):
            if self._cancelled:
                break
            data = probe(path)
            if data:
                out.append(PlaylistTrack(
                    title=data["title"],
                    artist=data["artist"],
                    stream_url=data["filepath"],
                    thumbnail_url="",
                    duration_seconds=data["duration"],
                    source=TrackSource.LOCAL.name,
                ))
            self.progress.emit(i, total)
        self.finished.emit(out)


class ImportLocalFilesDialog(QDialog):
    """
    Add audio files from disk to a local-only playlist.

    Two entry points — pick files, or pick a folder and scan it — then probe
    everything on a worker thread and append what came back.  Files that cannot
    be read are counted and skipped rather than aborting the import, because a
    real music library always contains a few.
    """

    def __init__(self, pm: PlaylistManager, playlist: Playlist, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Add Local Files — {playlist.name}")
        self.setMinimumWidth(520)
        self._pm = pm
        self._playlist = playlist
        self._thread: _LocalProbeThread | None = None
        self._tracks: list[PlaylistTrack] = []
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        lay.addWidget(QLabel(
            f"Add music from this computer to <b>{self._playlist.name}</b>."
        ))

        btn_row = QHBoxLayout()
        files_btn = QPushButton("Choose files…")
        files_btn.clicked.connect(self._pick_files)
        folder_btn = QPushButton("Choose folder…")
        folder_btn.clicked.connect(self._pick_folder)
        btn_row.addWidget(files_btn)
        btn_row.addWidget(folder_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self._status = QLabel("No files selected yet.")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        lay.addWidget(self._progress)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self._cancel_btn = QPushButton("Close")
        self._cancel_btn.clicked.connect(self._on_close)
        self._add_btn = QPushButton("Add to playlist")
        self._add_btn.setObjectName("accent")
        self._add_btn.setEnabled(False)
        self._add_btn.clicked.connect(self._commit)
        foot.addWidget(self._cancel_btn)
        foot.addWidget(self._add_btn)
        lay.addLayout(foot)

    # ── Selection ──────────────────────────────────────────────────────────

    def _extension_filter(self) -> str:
        from player.local_media import SUPPORTED_EXTENSIONS
        pattern = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTENSIONS))
        return f"Audio files ({pattern});;All files (*)"

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select audio files", "", self._extension_filter()
        )
        if paths:
            self._start_probe(paths)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select a music folder")
        if not folder:
            return
        from player.local_media import scan_directory
        paths = scan_directory(folder, recursive=True)
        if not paths:
            self._status.setText("No supported audio files found in that folder.")
            return
        self._start_probe(paths)

    # ── Probing ────────────────────────────────────────────────────────────

    def _start_probe(self, paths: list[str]) -> None:
        self._add_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, len(paths))
        self._progress.setValue(0)
        self._status.setText(f"Reading {len(paths)} file(s)…")

        self._thread = _LocalProbeThread(paths)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(lambda t: self._on_probed(t, len(paths)))
        self._thread.start()

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setValue(done)

    def _on_probed(self, tracks: list, attempted: int) -> None:
        self._thread = None
        self._progress.setVisible(False)
        self._tracks = tracks
        skipped = attempted - len(tracks)
        msg = f"Ready to add {len(tracks)} track(s)."
        if skipped:
            msg += f"  {skipped} file(s) could not be read and will be skipped."
        self._status.setText(msg)
        self._add_btn.setEnabled(bool(tracks))

    # ── Commit ─────────────────────────────────────────────────────────────

    def _commit(self) -> None:
        added = self._pm.add_tracks(self._playlist.id, self._tracks)
        if added == 0:
            self._status.setText("Nothing was added — the playlist may have been deleted.")
            return
        self.accept()

    def _on_close(self) -> None:
        if self._thread is not None:
            self._thread.cancel()
            self._thread.wait(2000)
        self.reject()

    def closeEvent(self, event) -> None:   # Qt naming, not ours
        if self._thread is not None:
            self._thread.cancel()
            self._thread.wait(2000)
        super().closeEvent(event)


class ImportPlaylistDialog(QDialog):
    """
    Two-step import dialog:
      1. User enters a playlist URL → background fetch enumerates entries.
      2. Valid result shows found/unavailable counts + name field → Save.
    """

    def __init__(self, pm: PlaylistManager, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Playlist")
        self.setMinimumWidth(500)
        self._pm = pm
        self._valid_tracks: list[PlaylistTrack] = []
        self._thread: _PlaylistFetchThread | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)

        # ── URL row ──
        url_row = QHBoxLayout()
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("YouTube / SoundCloud playlist URL")
        self._url_input.returnPressed.connect(self._start_check)
        url_row.addWidget(self._url_input, 1)
        self._check_btn = QPushButton("Import")
        self._check_btn.setObjectName("accent")
        self._check_btn.setFixedWidth(80)
        self._check_btn.clicked.connect(self._start_check)
        url_row.addWidget(self._check_btn)
        lay.addLayout(url_row)

        # ── Validation / status feedback ──
        self._status_lbl = QLabel()
        self._status_lbl.setObjectName("importStatus")
        self._status_lbl.setVisible(False)
        lay.addWidget(self._status_lbl)

        # ── Name row (shown only after successful fetch) ──
        self._name_row = QWidget()
        name_lay = QHBoxLayout(self._name_row)
        name_lay.setContentsMargins(0, 4, 0, 0)
        name_lay.setSpacing(8)
        name_lay.addWidget(QLabel("playlist name"))
        self._name_input = QLineEdit()
        name_lay.addWidget(self._name_input, 1)
        self._name_row.setVisible(False)
        lay.addWidget(self._name_row)

        # ── Button row ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self._save_btn = QPushButton("Save playlist")
        self._save_btn.setObjectName("accent")
        self._save_btn.setVisible(False)
        self._save_btn.clicked.connect(self._save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._save_btn)
        lay.addLayout(btn_row)

    def _start_check(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            return
        self._check_btn.setEnabled(False)
        self._check_btn.setText("checking…")
        self._status_lbl.setVisible(False)
        self._name_row.setVisible(False)
        self._save_btn.setVisible(False)
        self._valid_tracks = []

        self._thread = _PlaylistFetchThread(url)
        self._thread.finished.connect(self._on_fetch_done)
        self._thread.start()

    def _on_fetch_done(self, info: dict | None) -> None:
        self._check_btn.setEnabled(True)
        self._check_btn.setText("Import")

        if not info:
            self._show_status("url not valid", error=True)
            return

        entries = info.get("entries") or []
        if not entries:
            self._show_status("url not valid", error=True)
            return

        found = []
        unavail_count = 0
        for entry in entries:
            if _is_unavailable(entry):
                unavail_count += 1
            elif not _is_playable(entry):
                pass  # channel overview / mix entry — silently skip
            else:
                found.append(_entry_to_playlist_track(entry))

        if not found:
            self._show_status("url not valid", error=True)
            return

        self._valid_tracks = found
        parts = [f"found: {len(found)}"]
        if unavail_count:
            parts.append(f"unavailable: {unavail_count}")
        self._show_status("playlist loaded · " + " · ".join(parts), error=False)

        self._name_input.setText(info.get("title") or "Imported Playlist")
        self._name_row.setVisible(True)
        self._save_btn.setVisible(True)
        self.adjustSize()

    def _show_status(self, text: str, *, error: bool) -> None:
        self._status_lbl.setText(text)
        colour = "#ff4d4d" if error else "#3a6b4a"
        self._status_lbl.setStyleSheet(f"color: {colour}; font-size: 11px;")
        self._status_lbl.setVisible(True)

    def _save(self) -> None:
        name = self._name_input.text().strip() or "Imported Playlist"
        self._pm.create_from_playlist_tracks(name, self._valid_tracks)
        self.accept()
