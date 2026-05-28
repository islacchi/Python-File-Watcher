"""
handler.py — Live event handler
Plugs into watchdog's Observer and responds to file system events in real time.
Responsibilities:
  - Filter events by extension whitelist and ignore_prefixes
  - Detect actual moves vs. phantom delete+create pairs
  - Update the snapshot and log every event to the database
"""

import os
import time
import hashlib
import threading
from watchdog.events import FileSystemEventHandler
from logger import get_logger

log = get_logger(__name__)


# ------------------------------------------------------------------
# UTILITY FUNCTIONS
# ------------------------------------------------------------------

def compute_hash(path: str, algorithm: str = "md5") -> str | None:
    """
    Reads a file in binary chunks and returns its hash digest.
    Chunked reading (8192 bytes at a time) avoids loading large files
    entirely into memory.
    Returns None if the file can't be read (e.g. it was deleted mid-hash).
    """
    h = hashlib.new(algorithm)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (IOError, OSError):
        return None


def get_file_info(path: str) -> tuple:
    """
    Returns (size_in_bytes, modification_timestamp) for a file.
    Returns (None, None) if the file is inaccessible.
    """
    try:
        stat = os.stat(path)
        return stat.st_size, stat.st_mtime
    except OSError:
        return None, None


def classify_path_change(src: str, dest: str) -> str:
    """
    Determines whether a path change is a MOVED, RENAMED, or MOVED_AND_RENAMED
    by comparing the parent directory and filename of both paths.

    Rules:
      - Same folder + different filename   → RENAMED
      - Different folder + same filename   → MOVED
      - Different folder + different name  → MOVED_AND_RENAMED
    """
    src_dir   = os.path.dirname(os.path.abspath(src))
    dest_dir  = os.path.dirname(os.path.abspath(dest))
    src_name  = os.path.basename(src)
    dest_name = os.path.basename(dest)

    same_dir  = src_dir  == dest_dir
    same_name = src_name == dest_name

    if same_dir and not same_name:
        return "RENAMED"
    elif not same_dir and same_name:
        return "MOVED"
    else:
        return "MOVED_AND_RENAMED"


# ------------------------------------------------------------------
# WATCHDOG EVENT HANDLER
# ------------------------------------------------------------------

class FileWatchHandler(FileSystemEventHandler):
    """
    Subclass of watchdog's FileSystemEventHandler.
    watchdog calls on_created / on_modified / on_deleted / on_moved
    automatically whenever the OS fires a matching file system event.
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

        self.hash_algorithm = config["snapshot"]["hash_algorithm"]
        self.db = db

        # Move detection buffer: { md5_hash: (original_path, timestamp) }
        # When a DELETE fires, we store the file's hash here and wait.
        # If a CREATE arrives within move_window seconds with the same hash,
        # we log it as a MOVE instead of a DELETE + CREATE.
        self.pending_deletes: dict = {}
        self.move_window: float = 2.0  # seconds to wait before confirming a delete

        # Lock prevents race conditions when multiple events fire simultaneously
        self._lock = threading.Lock()

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
        ext = os.path.splitext(filename)[1].lower()

        for prefix in self.ignore_prefixes:
            if prefix and filename.startswith(prefix):
                return False

        return ext in self.watch_extensions

    # ------------------------------------------------------------------
    # MOVE DETECTION HELPERS
    # ------------------------------------------------------------------

    def _clean_pending_deletes(self):
        """Removes expired entries from the pending_deletes buffer."""
        now = time.time()
        expired = [
            h for h, (_, t) in self.pending_deletes.items()
            if now - t > self.move_window
        ]
        for h in expired:
            del self.pending_deletes[h]

    # ------------------------------------------------------------------
    # WATCHDOG CALLBACKS
    # ------------------------------------------------------------------

    def on_created(self, event):
        """
        Fires when a new file appears.
        Before logging as CREATED, check if it matches a pending delete —
        if so, it's actually a MOVE (file moved from outside the watched dir
        into it, or cross-drive move that the OS reports as delete+create).
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path = event.src_path
        file_hash = compute_hash(path, self.hash_algorithm)
        size, mtime = get_file_info(path)

        with self._lock:
            self._clean_pending_deletes()

            if file_hash and file_hash in self.pending_deletes:
                # Hash match → classify as MOVED, RENAMED, or MOVED_AND_RENAMED
                old_path, _ = self.pending_deletes.pop(file_hash)
                event_type = classify_path_change(old_path, path)
                self.db.log_event(event_type, old_path, dest_path=path,
                                  file_size=size, md5_hash=file_hash)
                self.db.delete_snapshot(old_path)
            else:
                self.db.log_event("CREATED", path, file_size=size, md5_hash=file_hash)

        if size is not None:
            self.db.upsert_snapshot(path, size, mtime, file_hash)

    def on_modified(self, event):
        """
        Fires when an existing file's contents or metadata change.
        We re-hash and re-snapshot the file on every modification.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path = event.src_path
        file_hash = compute_hash(path, self.hash_algorithm)
        size, mtime = get_file_info(path)

        self.db.log_event("MODIFIED", path, file_size=size, md5_hash=file_hash)

        if size is not None:
            self.db.upsert_snapshot(path, size, mtime, file_hash)

    def on_deleted(self, event):
        """
        Fires when a file disappears.
        We DON'T log it immediately — we store the hash in pending_deletes
        and wait move_window seconds. If a matching CREATE arrives in time,
        on_created() will claim it as a MOVE and clear the pending entry.
        If nothing claims it after move_window, a background thread logs
        it as a real DELETE.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path = event.src_path

        # Retrieve the file's last known hash from the snapshot
        snapshots = self.db.get_all_snapshots()
        file_hash = snapshots.get(path, {}).get("md5_hash")

        with self._lock:
            if file_hash:
                self.pending_deletes[file_hash] = (path, time.time())

        # Background thread waits, then confirms delete if unclaimed
        def delayed_delete_log():
            time.sleep(self.move_window)
            with self._lock:
                if file_hash and file_hash in self.pending_deletes:
                    self.pending_deletes.pop(file_hash, None)
                    self.db.log_event("DELETED", path)
                    self.db.delete_snapshot(path)

        threading.Thread(target=delayed_delete_log, daemon=True).start()

    def on_moved(self, event):
        """
        watchdog fires this when BOTH source and destination are inside
        the watched directory — watchdog can see both sides of the move.
        This is the clean case; no hash matching needed.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        src = event.src_path
        dest = event.dest_path
        file_hash = compute_hash(dest, self.hash_algorithm)
        size, mtime = get_file_info(dest)

        event_type = classify_path_change(src, dest)
        self.db.log_event(event_type, src, dest_path=dest,
                          file_size=size, md5_hash=file_hash)
        self.db.delete_snapshot(src)

        if size is not None:
            self.db.upsert_snapshot(dest, size, mtime, file_hash)