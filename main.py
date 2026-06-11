"""
main.py — Entry point
Execution order on every run:
  1. Load config.ini
  2. Set up logging (file + console)
  3. Open (or create) the SQLite database
  4. Purge events older than retention_days
  5. Run startup diff  → detect changes that happened while the script was off
  6. Start watchdog observer → catch live changes going forward
  7. Stay alive, auto-reconnect if the watched drive goes offline

Usage:
    python main.py              # normal mode (daemon)
    python main.py --once       # scan once, print results, exit (no observer)

Performance optimisations:
  - Parallel hashing with progress, no wasteful sampling phase
  - mtime pre-filter avoids re-hashing unchanged files
  - SIGTERM handler for clean shutdown when run as a service

Path normalization:
  - All paths passed to db.py are lowercased before any comparison or storage
  - scan_directory() normalizes os.walk() output to lowercase so set
    operations against the stored snapshot never produce false positives
    due to case differences on Windows network drives
"""

import os
import sys
import time
import signal
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from watchdog.observers import Observer

from logger import setup_logging, get_logger
from db import Database
from handler import FileWatchHandler, compute_hash, get_file_info, classify_path_change

log = get_logger(__name__)

SCRIPT_VERSION = "1.0.0"

# Global flag for graceful shutdown
_shutdown_requested = False


def handle_signal(signum, frame):
    """Handles SIGTERM (and others) for clean shutdown."""
    global _shutdown_requested
    if _shutdown_requested:
        return  # already shutting down, ignore duplicate signals
    _shutdown_requested = True
    log.info("Received signal %d. Shutting down...", signum)


# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------

def load_config(config_path: str = "config.ini") -> configparser.ConfigParser:
    """
    Reads config.ini from the same directory as main.py.
    Raises a clear error if the file is missing so the user knows what's wrong.
    """
    if not os.path.exists(config_path):
        print(f"[ERROR] config.ini not found at: {config_path}")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(config_path)
    return config


# ------------------------------------------------------------------
# STARTUP DIFF — detects offline changes
# ------------------------------------------------------------------

def scan_directory(watch_dir: str, watch_extensions: set, ignore_prefixes: list,
                   hash_algorithm: str, recursive: bool,
                   exclude_dirs: set = None,
                   old_snapshot: dict = None) -> dict:
    """
    Walks the watch_directory right now and returns its current state as:
    { filepath: { size, mtime, md5_hash } }

    All returned paths are normalized to lowercase so they compare correctly
    against snapshot paths loaded from SQLite (which are also stored lowercase).
    This prevents false-positive DELETED (offline) events caused by case
    differences between os.walk() output and stored snapshot paths.

    Performance optimisations:
      1. mtime pre-filter  — if a file's size and mtime match the last snapshot,
                              reuse the stored hash instead of re-hashing the file.
      2. Parallel hashing  — files that DO need hashing are processed concurrently
                              using a thread pool (no wasteful sampling phase).
      3. Exclude dirs      — skips directories matching exclude_dirs patterns.
    """
    if old_snapshot is None:
        old_snapshot = {}

    if exclude_dirs is None:
        exclude_dirs = set()

    # Normalize exclude_dirs to lowercase for consistent comparison
    exclude_dirs = {d.lower() for d in exclude_dirs}

    candidates = []

    walker = os.walk(watch_dir) if recursive else [
        (watch_dir, [], os.listdir(watch_dir))
    ]

    for root, dirs, files in walker:
        # Filter out excluded directories in-place so os.walk skips them
        if exclude_dirs:
            dirs[:] = [
                d for d in dirs
                if d.lower() not in exclude_dirs
                and os.path.join(root, d).lower() not in exclude_dirs
            ]

        for filename in files:
            if any(prefix and filename.startswith(prefix) for prefix in ignore_prefixes):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in watch_extensions:
                continue
            # Normalize path to lowercase — this is the core fix that prevents
            # case-mismatch false positives during startup diff set operations
            path = os.path.join(root, filename).lower()
            size, mtime = get_file_info(os.path.join(root, filename))
            if size is not None:
                candidates.append((path, size, mtime, os.path.join(root, filename)))

    current = {}
    to_hash = []
    skipped = 0

    for path, size, mtime, real_path in candidates:
        snap = old_snapshot.get(path)
        if snap and snap["size"] == size and snap["mtime"] == mtime and snap["md5_hash"]:
            current[path] = {"size": size, "mtime": mtime, "md5_hash": snap["md5_hash"]}
            skipped += 1
        else:
            to_hash.append((path, size, mtime, real_path))

    log.info("mtime pre-filter: %d unchanged, %d need hashing.", skipped, len(to_hash))

    if to_hash:
        max_workers = min(8, len(to_hash))

        def hash_file(args):
            path, size, mtime, real_path = args
            file_hash = compute_hash(real_path, hash_algorithm)
            return path, size, mtime, file_hash

        def format_eta(seconds: float) -> str:
            """Converts a raw second count into a human-readable string."""
            if seconds < 60:
                return f"~{int(seconds)}s"
            elif seconds < 3600:
                mins = int(seconds // 60)
                secs = int(seconds % 60)
                return f"~{mins}m {secs}s"
            else:
                hours = int(seconds // 3600)
                mins  = int((seconds % 3600) // 60)
                return f"~{hours}h {mins}m"

        total        = len(to_hash)
        hashed_count = 0
        batch_start  = time.time()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(hash_file, item): item for item in to_hash}
            for future in as_completed(futures):
                path, size, mtime, file_hash = future.result()
                current[path] = {"size": size, "mtime": mtime, "md5_hash": file_hash}
                hashed_count += 1

                if hashed_count % 50 == 0 or hashed_count == total:
                    elapsed = time.time() - batch_start
                    if elapsed > 0:
                        rate    = hashed_count / elapsed / max(max_workers, 1)
                        left    = total - hashed_count
                        eta_secs = (left / rate / max(max_workers, 1)) if rate > 0 else 0
                        log.info(
                            "Progress: %d / %d file(s) hashed. ETA: %s",
                            hashed_count, total, format_eta(eta_secs)
                        )

    return current


def run_startup_diff(db: Database, config: configparser.ConfigParser):
    """
    Compares the saved SQLite snapshot against the actual current directory state.
    Uses batch database operations for better performance.

    Both old_snapshot (from SQLite) and current_snapshot (from scan_directory)
    use lowercase-normalized paths, so set operations never produce false
    positives from case differences.
    """
    watch_dir      = config["watcher"]["watch_directory"]
    recursive      = config["watcher"].getboolean("recursive", True)
    watch_ext      = set(config["filters"]["watch_extensions"].replace(" ", "").split(","))
    ignore_pfx     = config["filters"]["ignore_prefixes"].replace(" ", "").split(",")
    hash_algorithm = config["snapshot"]["hash_algorithm"]

    # Parse optional exclude_directories filter
    exclude_dirs_raw = config["filters"].get("exclude_directories", "").strip()
    exclude_dirs = set()
    if exclude_dirs_raw:
        exclude_dirs = set(
            d.strip() for d in exclude_dirs_raw.replace(" ", "").split(",") if d.strip()
        )

    log.info("Scanning for offline changes...")

    # get_all_snapshots() returns paths as stored — already lowercase
    old_snapshot     = db.get_all_snapshots()
    # scan_directory() normalizes all paths to lowercase before returning
    current_snapshot = scan_directory(
        watch_dir, watch_ext, ignore_pfx, hash_algorithm, recursive,
        exclude_dirs=exclude_dirs, old_snapshot=old_snapshot
    )

    old_paths     = set(old_snapshot.keys())
    current_paths = set(current_snapshot.keys())
    missing_paths = old_paths - current_paths
    new_paths     = current_paths - old_paths
    common_paths  = old_paths & current_paths

    deleted_by_hash = {
        old_snapshot[p]["md5_hash"]: p
        for p in missing_paths
        if old_snapshot[p]["md5_hash"]
    }

    resolved_as_move_old = set()
    resolved_as_move_new = set()

    for path in new_paths:
        new_hash = current_snapshot[path]["md5_hash"]
        if new_hash and new_hash in deleted_by_hash:
            old_path = deleted_by_hash[new_hash]
            info = current_snapshot[path]
            event_type = classify_path_change(old_path, path) + " (offline)"
            db.log_event(event_type, old_path, dest_path=path,
                         file_size=info["size"], md5_hash=new_hash)
            db.delete_snapshot(old_path)
            db.upsert_snapshot(path, info["size"], info["mtime"], new_hash)
            resolved_as_move_old.add(old_path)
            resolved_as_move_new.add(path)

    for path in missing_paths - resolved_as_move_old:
        db.log_event("DELETED (offline)", path)
        db.delete_snapshot(path)

    for path in new_paths - resolved_as_move_new:
        info = current_snapshot[path]
        db.log_event("CREATED (offline)", path,
                     file_size=info["size"], md5_hash=info["md5_hash"])
        db.upsert_snapshot(path, info["size"], info["mtime"], info["md5_hash"])

    for path in common_paths:
        old_hash = old_snapshot[path]["md5_hash"]
        new_hash = current_snapshot[path]["md5_hash"]
        if old_hash != new_hash:
            info = current_snapshot[path]
            db.log_event("MODIFIED (offline)", path,
                         file_size=info["size"], md5_hash=new_hash)
            db.upsert_snapshot(path, info["size"], info["mtime"], new_hash)

    total_changes = (
        len(resolved_as_move_old) +
        len(missing_paths - resolved_as_move_old) +
        len(new_paths - resolved_as_move_new) +
        sum(
            1 for p in common_paths
            if old_snapshot[p]["md5_hash"] != current_snapshot[p]["md5_hash"]
        )
    )
    log.info("Startup diff done. %d offline change(s) detected.", total_changes)
    db.flush()
    return total_changes


# ------------------------------------------------------------------
# OBSERVER MANAGEMENT — handles network drive disconnects
# ------------------------------------------------------------------

def start_observer(watch_dir: str, recursive: bool, handler: FileWatchHandler):
    """Creates and starts a fresh watchdog Observer."""
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=recursive)
    observer.start()
    return observer


def start_heartbeat(db: Database, interval: int = 30):
    """
    Spawns a daemon thread that upserts a heartbeat timestamp to the
    config table every `interval` seconds while the script is alive.

    The Laravel UI reads this value to determine whether the script is
    currently running. Without this, the UI would show the script as
    offline during periods of no file activity since no events are written.

    The thread is a daemon — it dies automatically when main.py exits,
    so no manual cleanup is needed.
    """
    import threading

    def _beat():
        while not _shutdown_requested:
            try:
                db.upsert_config(
                    "heartbeat",
                    __import__("datetime").datetime.now().isoformat()
                )
            except Exception as e:
                log.warning("Heartbeat write failed: %s", e)
            time.sleep(interval)

    thread = threading.Thread(target=_beat, daemon=True)
    thread.start()
    log.info("Heartbeat started (every %ds).", interval)


def run_with_reconnect(watch_dir: str, recursive: bool,
                       handler: FileWatchHandler, reconnect_delay: int = 30):
    """
    Keeps the watchdog Observer alive indefinitely.
    If the watched path becomes unreachable (network drive goes offline),
    the observer is stopped and the script waits reconnect_delay seconds
    before trying again. Retries until the drive comes back online.
    This prevents the script from silently dying mid-session.
    Respects the global _shutdown_requested flag.
    """
    global _shutdown_requested
    observer = None

    while not _shutdown_requested:
        try:
            if not os.path.exists(watch_dir):
                log.warning(
                    "Watch directory unreachable: %s. "
                    "Retrying in %d seconds...", watch_dir, reconnect_delay
                )
                if observer and observer.is_alive():
                    observer.stop()
                    observer.join()
                    observer = None
                # Sleep in short increments so signal can interrupt
                for _ in range(reconnect_delay):
                    if _shutdown_requested:
                        break
                    time.sleep(1)
                continue

            if observer is None:
                observer = start_observer(watch_dir, recursive, handler)
                log.info("Observer started. Watching: %s", watch_dir)

            # Sleep in short increments to allow signal interrupt
            for _ in range(60):
                if _shutdown_requested:
                    break
                time.sleep(1)

            # If watchdog's internal thread died unexpectedly, restart it
            if observer and not observer.is_alive():
                log.error("Observer thread died unexpectedly. Restarting...")
                observer = None

        except Exception as e:
            log.error("Unexpected error in observer loop: %s", e, exc_info=True)
            if observer and observer.is_alive():
                observer.stop()
                observer.join()
            observer = None
            for _ in range(reconnect_delay):
                if _shutdown_requested:
                    break
                time.sleep(1)

    if observer and observer.is_alive():
        observer.stop()
        observer.join()


# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main():
    global _shutdown_requested
    _shutdown_requested = False

    # Parse CLI arguments
    once_mode = "--once" in sys.argv

    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGTERM, handle_signal)
    # SIGINT (Ctrl+C) is already handled by KeyboardInterrupt,
    # but we register it too for consistency
    try:
        signal.signal(signal.SIGINT, handle_signal)
    except ValueError:
        pass  # SIGINT handler may not be changeable on all platforms

    script_dir  = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.ini")

    config    = load_config(config_path)
    watch_dir = config["watcher"]["watch_directory"]
    log_dir   = config["storage"]["log_directory"]
    db_name   = config["storage"]["db_name"]
    recursive = config["watcher"].getboolean("recursive", True)
    retention = config["storage"].getint("retention_days", 90)
    reconnect_delay = config["watcher"].getint("reconnect_delay", 30)
    heartbeat_interval = config["watcher"].getint("heartbeat_interval", 30)

    # Step 1: set up logging before anything else so all messages are captured
    os.makedirs(log_dir, exist_ok=True)
    setup_logging(log_dir)

    log.info("=" * 60)
    log.info("File Watcher starting up.")
    log.info("Watch directory : %s", watch_dir)
    log.info("Log directory   : %s", log_dir)
    log.info("Retention       : %d day(s)", retention)

    # Step 2: wait for the watch directory to be available before proceeding
    # This handles the case where the script starts before the network drive mounts
    if not os.path.isdir(watch_dir):
        log.warning("Watch directory not available yet: %s", watch_dir)
        log.warning("Waiting for it to become available...")
        while not os.path.isdir(watch_dir) and not _shutdown_requested:
            time.sleep(reconnect_delay)
        if _shutdown_requested:
            return
        log.info("Watch directory is now available.")

    # Step 3: open database
    db_path = os.path.join(log_dir, db_name)
    db      = Database(db_path)

    # Step 4: write script metadata to config table for the Laravel UI
    db.upsert_config("watch_directory", watch_dir)
    db.upsert_config("log_directory",   log_dir)
    db.upsert_config("retention_days",  str(retention))
    db.upsert_config("script_version",  SCRIPT_VERSION)
    db.upsert_config("started_at",      __import__("datetime").datetime.now().isoformat())
    log.info("Config table updated.")

    # Step 5: purge old events
    db.purge_old_events(retention)

    # Step 6: detect offline changes
    total_changes = run_startup_diff(db, config)

    # If --once mode, print summary and exit
    if once_mode:
        print(f"\nStartup scan complete. {total_changes} change(s) detected.\n")
        db.close()
        return

    # Step 7: start heartbeat so the Laravel UI knows the script is alive
    start_heartbeat(db, heartbeat_interval)

    # Step 8: start live watcher with reconnect support
    handler = FileWatchHandler(db, config)
    log.info("Live watcher active. Press Ctrl+C to stop.")

    run_with_reconnect(watch_dir, recursive, handler, reconnect_delay)

    db.close()
    log.info("File Watcher stopped.")


if __name__ == "__main__":
    main()