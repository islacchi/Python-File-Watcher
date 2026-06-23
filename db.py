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
                prev_hash   TEXT
            );

            -- Fast lookup for purge_old_events()
            CREATE INDEX IF NOT EXISTS idx_events_timestamp
                ON events(timestamp);

            -- config table: stores script metadata readable by the Laravel UI
            -- One row per key, upserted on every startup
            with self._db_lock:                    
            CREATE TABLE IF NOT EXISTS config (
                key         TEXT UNIQUE NOT NULL,
                value       TEXT,
                updated     TEXT
            );

            -- Add prev_hash to existing databases that predate this column
            -- This is a no-op if the column already exists
            PRAGMA legacy_alter_table = ON;
        """)
        self.conn.commit()

    def _migrate(self):
        """
        Adds prev_hash column to the events table if it does not exist yet.
        Handles databases created before this column was introduced — safe
        to call on every startup, does nothing if column already exists.
        """
        with self._db_lock:
            existing = {
                row[1] for row in
                self.conn.execute("PRAGMA table_info(events)").fetchall()
            }
            if "prev_hash" not in existing:
                self.conn.execute(
                    "ALTER TABLE events ADD COLUMN prev_hash TEXT"
                )
                self.conn.commit()
        if "prev_hash" not in existing:       
            log.info("Migrated events table: added prev_hash column.")

    # ------------------------------------------------------------------
    # BATCHED COMMIT
    # ------------------------------------------------------------------

    def _maybe_flush(self):
        """Commit if we've accumulated enough writes since last flush."""
        with self._flush_lock:
            if self._write_count > 0:
                self._write_count = 0
                with self._db_lock:
                    self.conn.commit()
        self._start_timer()

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
        """Called by the timer thread — commits pending writes."""
        with self._flush_lock:
            if self._write_count > 0:
                self._write_count = 0
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
        rows = [
            (
                now,
                e.get("event_type"),
                _normalize(e.get("src_path")),
                _normalize(e.get("dest_path")),
                e.get("file_size"),
                e.get("md5_hash"),
                e.get("prev_hash"),
            )
            for e in events
        ]

        with self._db_lock:
            self.conn.executemany("""
                INSERT INTO events (timestamp, event_type, src_path, dest_path,
                                    file_size, md5_hash, prev_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
        with self._db_lock:
            self.conn.execute("""
                INSERT INTO events (timestamp, event_type, src_path, dest_path,
                                    file_size, md5_hash, prev_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, event_type, _normalize(src_path), _normalize(dest_path),
                  file_size, md5_hash, prev_hash))
        self._mark_dirty()

        if dest_path:
            log.info("%s: %s  →  %s", event_type, src_path, dest_path)
        else:
            log.info("%s: %s", event_type, src_path)

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