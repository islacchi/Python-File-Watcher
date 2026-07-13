"""
db.py — Database layer
Handles all SQLite operations for three purposes:
  1. snapshots table  → stores the last known state of every watched file
  2. events table     → stores a permanent log of every change detected
  3. config table     → stores script metadata readable by the Laravel UI

Performance optimisations:
  - WAL journal mode for concurrent read/write
  - synchronous=NORMAL (faster writes, WAL protects durability)
  - Automatic batched commits — default flush every 50 writes or 1 second
  - Index on events.timestamp for fast purge queries

Path normalization:
  - All paths are stored and looked up as lowercase strings
  - Prevents false-positive DELETED (offline) events caused by case
    differences between os.walk() output and stored snapshot paths
    (common on Windows network drives where path case is inconsistent)
"""

import sqlite3
from datetime import datetime, timedelta
import threading
from logger import get_logger

log = get_logger(__name__)


def _normalize(path: str) -> str:
    """
    Normalizes a file path to lowercase for case-insensitive comparison.
    Called on every path before any read or write operation so that
    os.walk() output and stored snapshot paths always compare equal
    regardless of drive or OS-level casing differences.
    """
    return path.lower() if path else path


def _extract_extension(path: str) -> str | None:
    """
    Extracts a lowercase file extension from a path, mirroring the
    PHP pathinfo(PATHINFO_EXTENSION) logic used in
    EventService::getAnalyticsTopExtensions() — same rule: text after
    the last dot in the last path segment only (so "archive.tar.gz"
    -> "gz", not "tar.gz").

    Returns None if there's no extension (dotfiles like ".gitignore"
    are treated as extensionless).

    Path should already be normalized (lowercased) before calling this.
    """
    if not path:
        return None
    basename = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in basename or basename.startswith("."):
        return None
    ext = basename.rsplit(".", 1)[-1].strip()
    return ext or None



class Database:
    def __init__(self, db_path: str):
        """
        Opens (or creates) the SQLite database at db_path.
        check_same_thread=False is required because watchdog fires events
        on background threads, not the main thread.
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)

        # Serializes all conn.execute* calls across threads.
        # check_same_thread=False disables sqlite3's guard but adds no
        # synchronization — without this lock, concurrent calls from the
        # watchdog, sweep, and heartbeat threads can corrupt connection state.
        self._db_lock = threading.Lock()

        # Performance: WAL mode + relaxed sync for ~2-5x faster writes
        with self._db_lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")

        self._create_tables()
        self._migrate()

        # Batched commit state
        self._write_count = 0
        self._flush_lock = threading.Lock()
        self._flush_interval = 50       # flush every N writes
        self._flush_timer: threading.Timer | None = None
        self._flush_timer_interval = 1.0  # also flush every 1 second

    # ------------------------------------------------------------------
    # SETUP
    # ------------------------------------------------------------------

    def _create_tables(self):
        """
        Creates both tables if they don't exist yet.
        Safe to call on every startup — IF NOT EXISTS prevents duplicates.
        """
        with self._db_lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    path        TEXT    UNIQUE NOT NULL,
                    size        INTEGER,
                    mtime       REAL,
                    md5_hash    TEXT,
                    last_seen   TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    event_type  TEXT    NOT NULL,
                    src_path    TEXT    NOT NULL,
                    dest_path   TEXT,
                    file_size   INTEGER,
                    md5_hash    TEXT,
                    prev_hash   TEXT,
                    extension   TEXT
                );

                -- Fast range queries in purge_old_events() and date filters
                CREATE INDEX IF NOT EXISTS idx_events_timestamp
                    ON events(timestamp);

                -- Fast filtering by event type in getFilteredEvents()
                CREATE INDEX IF NOT EXISTS idx_events_event_type
                    ON events(event_type);

                -- Fast filtering by source path in getFilteredEvents()
                CREATE INDEX IF NOT EXISTS idx_events_src_path
                    ON events(src_path);

                -- config table: stores script metadata readable by the Laravel UI
                -- One row per key, upserted on every startup                    
                CREATE TABLE IF NOT EXISTS config (
                    key         TEXT UNIQUE NOT NULL,
                    value       TEXT,
                    updated     TEXT
                );
            """)
        self.conn.commit()

    def _migrate(self):
        """
        Runs all incremental migrations on every startup.
        Every migration uses IF NOT EXISTS or checks before altering so it
        is safe to call repeatedly — already-applied migrations are no-ops.

        Migrations:
          1. prev_hash column — added for before/after hash comparison on MODIFIED
          2. idx_events_event_type — added for fast tab and type filter queries
          3. idx_events_src_path   — added for fast path search queries
          4. extension column + idx_events_extension — added so
             getAnalyticsTopExtensions() can GROUP BY extension directly in
             SQL instead of pulling every distinct src_path into PHP and
             parsing extensions there. Backfilled once for existing rows;
             completion is tracked in the config table so restarts after
             the initial backfill don't rescan the whole table.
          5. idx_events_timestamp_event_type — composite index so analytics
             queries that filter by timestamp range AND group by event_type
             in the same query don't fall back to a full scan on the second
             dimension (SQLite can generally only use one single-column
             index per table per query).
        """
        with self._db_lock:
            existing_cols = {
                row[1] for row in
                self.conn.execute("PRAGMA table_info(events)").fetchall()
            }

            # Migration 1: prev_hash column
            if "prev_hash" not in existing_cols:
                self.conn.execute(
                    "ALTER TABLE events ADD COLUMN prev_hash TEXT"
                )
                self.conn.commit()
                log.info("Migration: added prev_hash column to events table.")

            # Migration 4a: extension column (backfill happens after the
            # lock is released — see below)
            extension_col_added = False
            if "extension" not in existing_cols:
                self.conn.execute(
                    "ALTER TABLE events ADD COLUMN extension TEXT"
                )
                self.conn.commit()
                extension_col_added = True
                log.info("Migration: added extension column to events table.")

            # Migration 2, 3, 4b, 5: performance indexes
            # CREATE INDEX IF NOT EXISTS is a no-op if the index already exists.
            # Runs on every startup so existing databases created before these
            # indexes were introduced are upgraded automatically.
            self.conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_events_event_type
                    ON events(event_type);
                CREATE INDEX IF NOT EXISTS idx_events_src_path
                    ON events(src_path);
                CREATE INDEX IF NOT EXISTS idx_events_extension
                    ON events(extension);
                CREATE INDEX IF NOT EXISTS idx_events_timestamp_event_type
                    ON events(timestamp, event_type);
            """)
            log.info("Migration: verified performance indexes on events table.")

        # Backfill runs outside the lock above (get_config/upsert_config/
        # the backfill loop each take the lock themselves per-call) —
        # this method's lock is a plain threading.Lock, not reentrant,
        # so nesting a locked call inside the block above would deadlock.
        if extension_col_added or self.get_config("extension_backfill_done") != "1":
            self._backfill_extension_column()
            self.upsert_config("extension_backfill_done", "1")

    def _backfill_extension_column(self):
        """
        One-time backfill of the extension column for rows written before
        this migration existed. Walks the table in id order (using the
        implicit rowid index, not a full scan) so it stays fast even on
        a large events table, and commits per batch rather than holding
        one giant transaction.
        """
        BATCH = 2000
        last_id = 0
        total = 0
        while True:
            with self._db_lock:
                rows = self.conn.execute(
                    "SELECT id, src_path FROM events WHERE id > ? ORDER BY id LIMIT ?",
                    (last_id, BATCH),
                ).fetchall()
            if not rows:
                break

            updates = [
                (_extract_extension(src_path), row_id)
                for row_id, src_path in rows
            ]
            with self._db_lock:
                self.conn.executemany(
                    "UPDATE events SET extension = ? WHERE id = ?",
                    updates,
                )
                self.conn.commit()

            last_id = rows[-1][0]
            total += len(rows)
            if len(rows) < BATCH:
                break

        if total:
            log.info("Migration: backfilled extension column for %d events.", total)    

    # ------------------------------------------------------------------
    # BATCHED COMMIT
    # ------------------------------------------------------------------

    def _maybe_flush(self):
            """
            Lock ordering: always acquire _flush_lock before _db_lock.
            """
            with self._flush_lock:
                self._write_count += 1
                if self._write_count >= self._flush_interval:
                    self._write_count = 0
                    with self._db_lock:
                        self.conn.commit()

    def _start_timer(self):
        """Start a background timer that flushes every 1 second."""
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(
            self._flush_timer_interval, self._timed_flush
        )
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _timed_flush(self):
        with self._flush_lock:
            if self._write_count > 0:
                self._write_count = 0
                with self._db_lock:
                    self.conn.commit()
        self._start_timer()

    def _mark_dirty(self):
        """Call after every INSERT/UPDATE/DELETE to trigger batched commits."""
        self._maybe_flush()
        # Lazily start the timer on first write
        if self._flush_timer is None:
            self._start_timer()

    def flush(self):
        """Force an immediate commit. Call before shutdown or during idle."""
        with self._flush_lock:
            if self._write_count > 0:
                self._write_count = 0
                with self._db_lock:
                    self.conn.commit()
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

    # ------------------------------------------------------------------
    # BATCH EVENT INSERT
    # ------------------------------------------------------------------

    def log_events_batch(self, events: list):
        """
        Inserts multiple events in a single executemany() call.
        Much faster than calling log_event() in a loop for bulk operations
        (startup diff, initial scan, etc.).

        Each event is a dict with keys:
          event_type, src_path, dest_path, file_size, md5_hash, prev_hash
        """
        if not events:
            return

        now = datetime.now().isoformat()
        rows = []
        for e in events:
            src_path = _normalize(e.get("src_path"))
            rows.append((
                now,
                e.get("event_type"),
                src_path,
                _normalize(e.get("dest_path")),
                e.get("file_size"),
                e.get("md5_hash"),
                e.get("prev_hash"),
                _extract_extension(src_path),
            ))

        with self._db_lock:
            self.conn.executemany("""
                INSERT INTO events (timestamp, event_type, src_path, dest_path,
                                    file_size, md5_hash, prev_hash, extension)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
        self._mark_dirty()

    # ------------------------------------------------------------------
    # SNAPSHOT OPERATIONS
    # ------------------------------------------------------------------

    def get_snapshot_hash(self, path: str) -> str | None:
        """
        Returns the stored md5_hash for a single file path from the snapshot.
        Used by on_modified to capture the previous hash before overwriting it.
        Returns None if the file is not in the snapshot yet.
        """
        with self._db_lock:
            cursor = self.conn.execute(
                "SELECT md5_hash FROM snapshots WHERE path = ?", (_normalize(path),)
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def upsert_snapshot(self, path: str, size: int, mtime: float, md5_hash: str):
        """
        INSERT or UPDATE a file's record in the snapshot table.
        ON CONFLICT(path) means: if this path already exists, update it.
        This keeps only the LATEST known state per file path.
        Path is normalized to lowercase before storage.
        """
        now = datetime.now().isoformat()
        with self._db_lock:
            self.conn.execute("""
                INSERT INTO snapshots (path, size, mtime, md5_hash, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size      = excluded.size,
                    mtime     = excluded.mtime,
                    md5_hash  = excluded.md5_hash,
                    last_seen = excluded.last_seen
            """, (_normalize(path), size, mtime, md5_hash, now))
        self._mark_dirty()

    def upsert_snapshots_batch(self, snapshots: list):
        """
        Insert or update multiple snapshots in a single batch.
        Each item is a tuple (path, size, mtime, md5_hash).
        Paths are normalized to lowercase before storage.
        """
        if not snapshots:
            return

        now = datetime.now().isoformat()
        with self._db_lock:
            self.conn.executemany("""
                INSERT INTO snapshots (path, size, mtime, md5_hash, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size      = excluded.size,
                    mtime     = excluded.mtime,
                    md5_hash  = excluded.md5_hash,
                    last_seen = excluded.last_seen
            """, [(_normalize(p), s, m, h, now) for p, s, m, h in snapshots])
            self.conn.commit()

    def delete_snapshot(self, path: str):
        """
        Removes a file from the snapshot table.
        Called when a file is confirmed deleted or moved away from its old path.
        """
        with self._db_lock:
            self.conn.execute(
                "DELETE FROM snapshots WHERE path = ?", (_normalize(path),)
            )
        self._mark_dirty()

    def delete_snapshots_batch(self, paths: list):
        """
        Delete multiple snapshots in a single batch.
        """
        if not paths:
            return
        with self._db_lock:
            self.conn.executemany(
                "DELETE FROM snapshots WHERE path = ?",
                [(_normalize(p),) for p in paths]
            )
            self.conn.commit()

    def get_all_snapshots(self) -> dict:
        """
        Returns the entire snapshot table as a dictionary:
        { filepath: { size, mtime, md5_hash } }
        Used during startup diff to compare against the current directory state.
        Paths are returned as-stored (already normalized to lowercase).
        """
        with self._db_lock:
            cursor = self.conn.execute(
                "SELECT path, size, mtime, md5_hash FROM snapshots"
            )
            rows = cursor.fetchall()
        return {
            row[0]: {"size": row[1], "mtime": row[2], "md5_hash": row[3]}
            for row in rows
        }

    # ------------------------------------------------------------------
    # EVENT LOG OPERATIONS
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        src_path: str,
        dest_path: str = None,
        file_size: int = None,
        md5_hash: str = None,
        prev_hash: str = None,
    ):
        """
        Appends one row to the events table and writes to the logger.
        dest_path is only used for MOVED/RENAMED events.
        prev_hash is the hash of the file before a MODIFIED event — allows
        before/after comparison without storing file contents.
        Paths are normalized to lowercase before storage.

        Writes are batched — commit happens after N writes or 1 second,
        whichever comes first.
        """
        timestamp = datetime.now().isoformat()
        normalized_src = _normalize(src_path)
        with self._db_lock:
            self.conn.execute("""
                INSERT INTO events (timestamp, event_type, src_path, dest_path,
                                    file_size, md5_hash, prev_hash, extension)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, event_type, normalized_src, _normalize(dest_path),
                  file_size, md5_hash, prev_hash, _extract_extension(normalized_src)))
        self._mark_dirty()

    # ------------------------------------------------------------------
    # RETENTION / CLEANUP
    # ------------------------------------------------------------------

    def purge_old_events(self, retention_days: int):
        """
        Deletes events older than retention_days from the events table.
        Snapshots are never purged — they represent current file state.
        Called once on every startup before the diff runs.
        Uses the idx_events_timestamp index for fast deletion.

        retention_days semantics:
          > 0  purge events older than N days.
          = 0  keep all events indefinitely; no purge performed.
          < 0  invalid — almost certainly a config typo; logs a warning.
        """
        if retention_days < 0:
            log.warning(
                "retention_days is %d (negative) — skipping purge. "
                "Set to 0 to keep all events indefinitely, "
                "or a positive integer to enable cleanup.",
                retention_days,
            )
            return

        if retention_days == 0:
            log.info("retention_days = 0: keeping all events indefinitely.")
            return

        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with self._db_lock:
            cursor = self.conn.execute(
                "DELETE FROM events WHERE timestamp < ?", (cutoff,)
            )
            self.conn.commit()

        if cursor.rowcount:
            log.info("Purged %d event(s) older than %d day(s).",
                     cursor.rowcount, retention_days)
        else:
            log.info("No events older than %d day(s) to purge.", retention_days)

    # ------------------------------------------------------------------
    # CONFIG TABLE OPERATIONS
    # ------------------------------------------------------------------

    def upsert_config(self, key: str, value: str, immediate: bool = True):
        """
        Inserts or updates a single row in the config table.
        Called on every startup to keep the UI informed of the current
        script state — watch directory, start time, retention setting, etc.

        immediate=True  (default) commits right away — used for startup
                        writes where durability matters.
        immediate=False batches the write — used for high-frequency writes
                        like the heartbeat where immediate durability is
                        not required.
        """
        now = datetime.now().isoformat()
        with self._db_lock:
            self.conn.execute("""
                INSERT INTO config (key, value, updated)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value   = excluded.value,
                    updated = excluded.updated
            """, (key, value, now))

            if immediate:
                self.conn.commit()

        if not immediate:
            self._mark_dirty()

    def get_config(self, key: str) -> str | None:
        """
        Returns the value for a given config key.
        Returns None if the key does not exist.
        """
        with self._db_lock:
            cursor = self.conn.execute(
                "SELECT value FROM config WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # CLEANUP
    # ------------------------------------------------------------------

    def close(self):
        self.flush()
        with self._db_lock:
            self.conn.close()