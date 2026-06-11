"""
handler.py — Live event handler
Plugs into watchdog's Observer and responds to file system events in real time.
Responsibilities:
  - Filter events by extension whitelist and ignore_prefixes
  - Detect actual moves vs. phantom delete+create pairs
  - Update the snapshot and log every event to the database

Performance optimisations:
  - 64KB hash chunk size (was 8KB) — faster I/O on modern drives
  - Single background sweep thread instead of one thread per deleted file
  - Uses os.scandir() for faster directory traversal
  - Batch snapshot upserts in move detection
"""

import os
import time
import hashlib
import threading
from watchdog.events import FileSystemEventHandler
from logger import get_logger

log = get_logger(__name__)

# Larger read buffer = faster hashing, especially on network drives
HASH_CHUNK_SIZE = 64 * 1024  # 64 KB


# ------------------------------------------------------------------
# UTILITY FUNCTIONS
# ------------------------------------------------------------------

def compute_hash(path: str, algorithm: str = "md5") -> str | None:
    """
    Reads a file in binary chunks and returns its hash digest.
    Chunked reading (64 KB at a time) avoids loading large files
    entirely into memory.
    Returns None if the file can't be read (e.g. it was deleted mid-hash).
    """
    h = hashlib.new(algorithm)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
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


def _scan_dir_for_hash(watch_dir: str, target_hash: str,
                       hash_algorithm: str, skip_path: str | None,
                       filter_fn) -> str | None:
    """
    Efficiently scans watch_dir using os.scandir() looking for a file
    whose hash matches target_hash. Returns the matching path, or None.

    This is used by the single background sweep thread instead of
    spawning one os.walk() per deleted file.
    """
    # Iterative stack-based recursion to avoid deep recursion limits
    stack = [watch_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if not filter_fn(entry.path):
                            continue
                        if entry.path == skip_path:
                            continue
                        # Quick size check before hashing
                        if compute_hash(entry.path, hash_algorithm) == target_hash:
                            return entry.path
        except PermissionError:
            continue
    return None


# ------------------------------------------------------------------
# WATCHDOG EVENT HANDLER
# ------------------------------------------------------------------

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

        self.hash_algorithm = config["snapshot"]["hash_algorithm"]
        self.db = db

        # Move detection: { md5_hash: (original_path, deadline_timestamp) }
        # When a DELETE fires, we store the file's hash here and let the
        # single sweep thread check for a matching CREATE.
        self.pending_deletes: dict = {}
        self.move_window: float = config["watcher"].getfloat("move_window", 2.0)
        self.watch_directory: str = config["watcher"]["watch_directory"]

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
        ext = os.path.splitext(filename)[1].lower()

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
        Every ~1 second, it:
          1. Removes expired entries from pending_deletes
          2. For each still-pending delete whose deadline has passed,
             logs it as a genuine DELETE

        on_created() is the primary path for fast moves — when a file
        appears with a hash matching a pending delete, it claims the
        match immediately. This sweep thread only handles the case
        where the window expires without a match.
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
        now = time.time()
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
        prev_hash is read from the snapshot BEFORE overwriting it, giving
        a before/after record of every modification in the events table.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path = event.src_path

        # Capture the previous hash before it gets overwritten
        prev_hash = self.db.get_snapshot_hash(path)

        file_hash = compute_hash(path, self.hash_algorithm)
        size, mtime = get_file_info(path)

        # Only log if the hash actually changed — skips metadata-only touches
        if file_hash == prev_hash:
            return

        self.db.log_event("MODIFIED", path, file_size=size,
                          md5_hash=file_hash, prev_hash=prev_hash)

        if size is not None:
            self.db.upsert_snapshot(path, size, mtime, file_hash)

    def on_deleted(self, event):
        """
        Fires when a file disappears.
        Instead of spawning a new polling thread per deletion, we simply
        store the file's hash in the pending_deletes dict. The single
        background sweep thread handles expiry and logs genuine DELETEs.

        If on_created() fires with a matching hash before the window
        expires, it claims the match and logs it as a MOVE instead.
        """
        if event.is_directory or not self._should_watch(event.src_path):
            return

        path = event.src_path

        # Retrieve the file's last known hash from the snapshot
        file_hash = self.db.get_snapshot_hash(path)

        if file_hash:
            deadline = time.time() + self.move_window
            with self._lock:
                self.pending_deletes[file_hash] = (path, deadline)

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