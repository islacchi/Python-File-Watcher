"""
handler.py — Live event handler
Plugs into watchdog's Observer and responds to file system events in real time.
Responsibilities:
  - Filter events by extension whitelist and ignore_prefixes
  - Detect actual moves vs. phantom delete+create pairs
  - Update the snapshot and log every event to the database

Utility functions (compute_hash, get_file_info, classify_path_change)
live in utils.py and are shared with diff.py to avoid duplication.

Performance optimisations:
  - 64KB hash chunk size — faster I/O on modern drives
  - Single background sweep thread instead of one thread per deleted file
  - Batch snapshot upserts in move detection
"""

import os
import time
import threading
from watchdog.events import FileSystemEventHandler

from logger import get_logger
from utils import compute_hash, get_file_info, classify_path_change

log = get_logger(__name__)


class FileWatchHandler(FileSystemEventHandler):
    """
    Subclass of watchdog's FileSystemEventHandler.
    watchdog calls on_created / on_modified / on_deleted / on_moved
    automatically whenever the OS fires a matching file system event.

    Move detection uses a single background sweep thread rather than
    spawning one thread per deletion. This prevents thread explosion
    when many files are deleted in a batch.
    """

    def __init__(self, db, config):
        # Build a set of lowercase extensions for fast O(1) lookup
        self.watch_extensions = set(
            config["filters"]["watch_extensions"].replace(" ", "").split(",")
        )

        # List of filename prefixes to ignore (e.g. ~$, .~)
        self.ignore_prefixes = (
            config["filters"]["ignore_prefixes"].replace(" ", "").split(",")
        )

        self.hash_algorithm  = config["snapshot"]["hash_algorithm"]
        self.db              = db

        # Move detection: { md5_hash: (original_path, deadline_timestamp) }
        self.pending_deletes: dict = {}
        self.move_window: float    = config["watcher"].getfloat("move_window", 2.0)
        self.watch_directory: str  = config["watcher"]["watch_directory"]

        self._lock = threading.Lock()

        # Start the single background sweep thread
        self._sweep_thread_running = True
        self._sweep_thread = threading.Thread(
            target=self._sweep_loop, daemon=True, name="move-sweep"
        )
        self._sweep_thread.start()
        log.info("Move-detection sweep thread started (window=%.1fs).",
                 self.move_window)

    # ------------------------------------------------------------------
    # FILTER
    # ------------------------------------------------------------------

    def _should_watch(self, path: str) -> bool:
        """
        Returns True only if the file:
          1. Does NOT start with any ignored prefix
          2. Has an extension in our whitelist
        """
        filename = os.path.basename(path)
        ext      = os.path.splitext(filename)[1].lower()

        for prefix in self.ignore_prefixes:
            if prefix and filename.startswith(prefix):
                return False

        return ext in self.watch_extensions

    # ------------------------------------------------------------------
    # SINGLE BACKGROUND SWEEP THREAD
    # ------------------------------------------------------------------

    def _sweep_loop(self):
        """
        Runs continuously in a single daemon thread.
        Every ~1 second, checks pending deletes whose window has expired
        and logs them as genuine DELETEs.
        """
        poll_interval = 1.0
        while self._sweep_thread_running:
            time.sleep(poll_interval)
            try:
                self._sweep_expired()
            except Exception as e:
                log.warning("Sweep thread error: %s", e, exc_info=True)

    def _sweep_expired(self):
        """Check each pending delete — if deadline passed, log as genuine DELETE."""
        now       = time.time()
        to_delete = []

        with self._lock:
            for file_hash, (orig_path, deadline) in list(self.pending_deletes.items()):
                if now >= deadline:
                    to_delete.append((file_hash, orig_path))

            for file_hash, orig_path in to_delete:
                self.pending_deletes.pop(file_hash, None)
                self.db.log_event("DELETED", orig_path)
                self.db.delete_snapshot(orig_path)

    # ------------------------------------------------------------------
    # WATCHDOG CALLBACKS
    # ------------------------------------------------------------------

    def on_created(self, event):
        """
        Fires when a new file appears.
        Checks pending_deletes for a hash match before logging as CREATED —
        a match means this is actually a MOVE from outside the watched dir.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path      = event.src_path
        file_hash = compute_hash(path, self.hash_algorithm)
        size, mtime = get_file_info(path)

        with self._lock:
            if file_hash and file_hash in self.pending_deletes:
                old_path, _ = self.pending_deletes.pop(file_hash)
                event_type  = classify_path_change(old_path, path)
                self.db.log_event(event_type, old_path, dest_path=path,
                                  file_size=size, md5_hash=file_hash)
                self.db.delete_snapshot(old_path)
            else:
                self.db.log_event("CREATED", path,
                                  file_size=size, md5_hash=file_hash)

        if size is not None:
            self.db.upsert_snapshot(path, size, mtime, file_hash)

    def on_modified(self, event):
        """
        Fires when an existing file's contents or metadata change.
        Only logs if the hash actually changed — skips metadata-only touches.
        prev_hash captures the before state for before/after comparison.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path      = event.src_path
        prev_hash = self.db.get_snapshot_hash(path)
        file_hash = compute_hash(path, self.hash_algorithm)
        size, mtime = get_file_info(path)

        if file_hash == prev_hash:
            return

        self.db.log_event("MODIFIED", path, file_size=size,
                          md5_hash=file_hash, prev_hash=prev_hash)

        if size is not None:
            self.db.upsert_snapshot(path, size, mtime, file_hash)

    def on_deleted(self, event):
        """
        Fires when a file disappears.
        Stores the file's hash in pending_deletes. The sweep thread handles
        expiry and logs genuine DELETEs. on_created() claims hash matches
        before expiry and logs them as MOVEs instead.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path      = event.src_path
        file_hash = self.db.get_snapshot_hash(path)

        if file_hash:
            deadline = time.time() + self.move_window
            with self._lock:
                self.pending_deletes[file_hash] = (path, deadline)

    def on_moved(self, event):
        """
        Fires when both source and destination are inside the watched directory.
        Clean case — watchdog sees both sides, no hash matching needed.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        src        = event.src_path
        dest       = event.dest_path
        file_hash  = compute_hash(dest, self.hash_algorithm)
        size, mtime = get_file_info(dest)

        event_type = classify_path_change(src, dest)
        self.db.log_event(event_type, src, dest_path=dest,
                          file_size=size, md5_hash=file_hash)
        self.db.delete_snapshot(src)

        if size is not None:
            self.db.upsert_snapshot(dest, size, mtime, file_hash)