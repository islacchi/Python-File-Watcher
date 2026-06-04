"""
db.py — Database layer
Handles all SQLite operations for three purposes:
  1. snapshots table  → stores the last known state of every watched file
  2. events table     → stores a permanent log of every change detected
  3. config table     → stores script metadata readable by the Laravel UI
"""

import sqlite3
from datetime import datetime, timedelta
from logger import get_logger

log = get_logger(__name__)


class Database:
    def __init__(self, db_path: str):
        """
        Opens (or creates) the SQLite database at db_path.
        check_same_thread=False is required because watchdog fires events
        on background threads, not the main thread.
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()
        self._migrate()

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

            -- config table: stores script metadata readable by the Laravel UI
            -- One row per key, upserted on every startup
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
        existing = {
            row[1] for row in
            self.conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "prev_hash" not in existing:
            self.conn.execute(
                "ALTER TABLE events ADD COLUMN prev_hash TEXT"
            )
            self.conn.commit()
            log.info("Migrated events table: added prev_hash column.")

    # ------------------------------------------------------------------
    # SNAPSHOT OPERATIONS
    # ------------------------------------------------------------------

    def get_snapshot_hash(self, path: str) -> str | None:
        """
        Returns the stored md5_hash for a single file path from the snapshot.
        Used by on_modified to capture the previous hash before overwriting it.
        Returns None if the file is not in the snapshot yet.
        """
        cursor = self.conn.execute(
            "SELECT md5_hash FROM snapshots WHERE path = ?", (path,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def upsert_snapshot(self, path: str, size: int, mtime: float, md5_hash: str):
        """
        INSERT or UPDATE a file's record in the snapshot table.
        ON CONFLICT(path) means: if this path already exists, update it.
        This keeps only the LATEST known state per file path.
        """
        now = datetime.now().isoformat()
        self.conn.execute("""
            INSERT INTO snapshots (path, size, mtime, md5_hash, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size      = excluded.size,
                mtime     = excluded.mtime,
                md5_hash  = excluded.md5_hash,
                last_seen = excluded.last_seen
        """, (path, size, mtime, md5_hash, now))
        self.conn.commit()

    def delete_snapshot(self, path: str):
        """
        Removes a file from the snapshot table.
        Called when a file is confirmed deleted or moved away from its old path.
        """
        self.conn.execute("DELETE FROM snapshots WHERE path = ?", (path,))
        self.conn.commit()

    def get_all_snapshots(self) -> dict:
        """
        Returns the entire snapshot table as a dictionary:
        { filepath: { size, mtime, md5_hash } }
        Used during startup diff to compare against the current directory state.
        """
        cursor = self.conn.execute(
            "SELECT path, size, mtime, md5_hash FROM snapshots"
        )
        return {
            row[0]: {"size": row[1], "mtime": row[2], "md5_hash": row[3]}
            for row in cursor.fetchall()
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
        """
        timestamp = datetime.now().isoformat()
        self.conn.execute("""
            INSERT INTO events (timestamp, event_type, src_path, dest_path, file_size, md5_hash, prev_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, event_type, src_path, dest_path, file_size, md5_hash, prev_hash))
        self.conn.commit()

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
        """
        if retention_days <= 0:
            return

        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM events WHERE timestamp < ?", (cutoff,)
        )
        self.conn.commit()

        if cursor.rowcount:
            log.info("Purged %d event(s) older than %d day(s).",
                     cursor.rowcount, retention_days)

    # ------------------------------------------------------------------
    # CONFIG TABLE OPERATIONS
    # ------------------------------------------------------------------

    def upsert_config(self, key: str, value: str):
        """
        Inserts or updates a single row in the config table.
        Called on every startup to keep the UI informed of the current
        script state — watch directory, start time, retention setting, etc.
        """
        now = datetime.now().isoformat()
        self.conn.execute("""
            INSERT INTO config (key, value, updated)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value   = excluded.value,
                updated = excluded.updated
        """, (key, value, now))
        self.conn.commit()

    def get_config(self, key: str) -> str | None:
        """
        Returns the value for a given config key.
        Returns None if the key does not exist.
        """
        cursor = self.conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # CLEANUP
    # ------------------------------------------------------------------

    def close(self):
        self.conn.close()