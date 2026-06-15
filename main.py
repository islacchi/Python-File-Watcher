"""
main.py — Entry point
Wires all modules together and drives the startup sequence.
Business logic lives in the modules it imports — this file
is intentionally thin.

Execution order on every run:
  1. Load and validate config.ini         [config.py]
  2. Set up logging                       [logger.py]
  3. Wait for watch directory             [main.py]
  4. Open database                        [db.py]
  5. Write startup metadata to config     [db.py]
  6. Purge old events                     [db.py]
  7. Run startup diff                     [diff.py]
  8. Start heartbeat thread               [main.py]
  9. Start live watcher with reconnect    [watcher.py]

Usage:
    python main.py          # normal mode (daemon)
    python main.py --once   # scan once, print results, exit
"""

import os
import sys
import time
import signal
import threading
import datetime

from logger import setup_logging, get_logger
from config import load_config
from db import Database
from diff import run_startup_diff
from watcher import run_with_reconnect
from handler import FileWatchHandler

log = get_logger(__name__)

SCRIPT_VERSION = "1.0.0"

_shutdown_requested = False


def handle_signal(signum, frame):
    """Handles SIGTERM and SIGINT for clean shutdown."""
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    log.info("Received signal %d. Shutting down...", signum)


def is_shutdown_requested() -> bool:
    return _shutdown_requested


def start_heartbeat(db: Database, interval: int = 30):
    """
    Spawns a daemon thread that upserts a heartbeat timestamp to the
    config table every interval seconds while the script is alive.
    Uses immediate=False so heartbeat writes are batched rather than
    forcing a commit on every write.
    """
    def _beat():
        while not _shutdown_requested:
            try:
                db.upsert_config(
                    "heartbeat",
                    datetime.datetime.now().isoformat(),
                    immediate=False,
                )
            except Exception as e:
                log.warning("Heartbeat write failed: %s", e)
            time.sleep(interval)

    thread = threading.Thread(target=_beat, daemon=True)
    thread.start()
    log.info("Heartbeat started (every %ds).", interval)


def main():
    global _shutdown_requested
    _shutdown_requested = False

    once_mode = "--once" in sys.argv

    signal.signal(signal.SIGTERM, handle_signal)
    try:
        signal.signal(signal.SIGINT, handle_signal)
    except ValueError:
        pass

    # Step 1: load and validate config
    config     = load_config()
    watch_dir  = config["watcher"]["watch_directory"]
    log_dir    = config["storage"]["log_directory"]
    db_name    = config["storage"]["db_name"]
    recursive  = config["watcher"].getboolean("recursive", True)
    retention  = config["storage"].getint("retention_days", 90)
    reconnect_delay     = config["watcher"].getint("reconnect_delay", 30)
    heartbeat_interval  = config["watcher"].getint("heartbeat_interval", 30)

    # Step 2: set up logging
    os.makedirs(log_dir, exist_ok=True)
    setup_logging(log_dir)

    log.info("=" * 60)
    log.info("File Watcher v%s starting up.", SCRIPT_VERSION)
    log.info("Watch directory : %s", watch_dir)
    log.info("Log directory   : %s", log_dir)
    log.info("Retention       : %d day(s)", retention)

    # Step 3: wait for watch directory
    if not os.path.isdir(watch_dir):
        log.warning("Watch directory not available yet: %s", watch_dir)
        log.warning("Waiting for it to become available...")
        while not os.path.isdir(watch_dir) and not _shutdown_requested:
            time.sleep(reconnect_delay)
        if _shutdown_requested:
            return
        log.info("Watch directory is now available.")

    # Step 4: open database
    db_path = os.path.join(log_dir, db_name)
    db      = Database(db_path)

    # Step 5: write startup metadata
    db.upsert_config("watch_directory", watch_dir)
    db.upsert_config("log_directory",   log_dir)
    db.upsert_config("retention_days",  str(retention))
    db.upsert_config("script_version",  SCRIPT_VERSION)
    db.upsert_config("started_at",      datetime.datetime.now().isoformat())
    db.upsert_config("status",          "scanning")
    log.info("Config table updated.")

    # Step 6: purge old events
    db.purge_old_events(retention)

    # Step 7: startup diff
    total_changes = run_startup_diff(db, config)
    db.upsert_config("status", "live")

    if once_mode:
        print(f"\nStartup scan complete. {total_changes} change(s) detected.\n")
        db.upsert_config("status", "offline")
        db.close()
        return

    # Step 8: heartbeat
    start_heartbeat(db, heartbeat_interval)

    # Step 9: live watcher
    handler = FileWatchHandler(db, config)
    log.info("Live watcher active. Press Ctrl+C to stop.")

    run_with_reconnect(
        watch_dir, recursive, handler,
        reconnect_delay=reconnect_delay,
        shutdown_flag=is_shutdown_requested,
    )

    db.upsert_config("status", "offline")
    db.close()
    log.info("File Watcher stopped.")


if __name__ == "__main__":
    main()