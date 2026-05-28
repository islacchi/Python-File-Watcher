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
"""

import os
import sys
import time
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from watchdog.observers import Observer

from logger import setup_logging, get_logger
from db import Database
from handler import FileWatchHandler, compute_hash, get_file_info, classify_path_change

log = get_logger(__name__)


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
                   old_snapshot: dict = None) -> dict:
    """
    Walks the watch_directory right now and returns its current state as:
    { filepath: { size, mtime, md5_hash } }

    Performance optimisations:
      1. mtime pre-filter  — if a file's size and mtime match the last snapshot,
                             reuse the stored hash instead of re-hashing the file.
      2. Parallel hashing  — files that DO need hashing are processed concurrently
                             using a thread pool.
    """
    if old_snapshot is None:
        old_snapshot = {}

    candidates = []

    walker = os.walk(watch_dir) if recursive else [
        (watch_dir, [], os.listdir(watch_dir))
    ]

    for root, _dirs, files in walker:
        for filename in files:
            if any(prefix and filename.startswith(prefix) for prefix in ignore_prefixes):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in watch_extensions:
                continue
            path = os.path.join(root, filename)
            size, mtime = get_file_info(path)
            if size is not None:
                candidates.append((path, size, mtime))

    current = {}
    to_hash = []
    skipped = 0

    for path, size, mtime in candidates:
        snap = old_snapshot.get(path)
        if snap and snap["size"] == size and snap["mtime"] == mtime and snap["md5_hash"]:
            current[path] = {"size": size, "mtime": mtime, "md5_hash": snap["md5_hash"]}
            skipped += 1
        else:
            to_hash.append((path, size, mtime))

    log.info("mtime pre-filter: %d unchanged, %d need hashing.", skipped, len(to_hash))

    if to_hash:
        max_workers = min(8, len(to_hash))

        def hash_file(args):
            path, size, mtime = args
            file_hash = compute_hash(path, hash_algorithm)
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

        # --- ETA estimation ---
        # Hash a small sample of up to 5 files first and measure how long
        # it takes. Use that rate to estimate how long the rest will take.
        SAMPLE_SIZE    = min(5, len(to_hash))
        sample         = to_hash[:SAMPLE_SIZE]
        remaining      = to_hash[SAMPLE_SIZE:]
        sample_start   = time.time()
        sample_results = []

        with ThreadPoolExecutor(max_workers=min(max_workers, SAMPLE_SIZE)) as executor:
            futures = {executor.submit(hash_file, item): item for item in sample}
            for future in as_completed(futures):
                result = future.result()
                sample_results.append(result)
                current[result[0]] = {
                    "size": result[1], "mtime": result[2], "md5_hash": result[3]
                }

        sample_elapsed   = time.time() - sample_start
        seconds_per_file = sample_elapsed / SAMPLE_SIZE if SAMPLE_SIZE else 0
        estimated_seconds = (seconds_per_file * len(remaining)) / max(max_workers, 1)

        if remaining:
            log.info(
                "Sampled %d file(s) in %.1fs. "
                "Estimated time for remaining %d file(s): %s",
                SAMPLE_SIZE, sample_elapsed, len(remaining),
                format_eta(estimated_seconds)
            )
        else:
            log.info("All files covered in sample. Scan complete.")

        # --- Hash remaining files with progress updates every 50 files ---
        hashed_count = SAMPLE_SIZE
        total        = len(to_hash)
        batch_start  = time.time()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(hash_file, item): item for item in remaining}
            for future in as_completed(futures):
                path, size, mtime, file_hash = future.result()
                current[path] = {"size": size, "mtime": mtime, "md5_hash": file_hash}
                hashed_count += 1

                if hashed_count % 50 == 0 or hashed_count == total:
                    elapsed     = time.time() - batch_start
                    done_so_far = hashed_count - SAMPLE_SIZE
                    if done_so_far > 0 and elapsed > 0:
                        rate     = done_so_far / elapsed / max(max_workers, 1)
                        left     = total - hashed_count
                        eta_secs = (left / rate / max(max_workers, 1)) if rate > 0 else 0
                        log.info(
                            "Progress: %d / %d file(s) hashed. ETA: %s",
                            hashed_count, total, format_eta(eta_secs)
                        )

    return current


def run_startup_diff(db: Database, config: configparser.ConfigParser):
    """
    Compares the saved SQLite snapshot against the actual current directory state.
    """
    watch_dir      = config["watcher"]["watch_directory"]
    recursive      = config["watcher"].getboolean("recursive", True)
    watch_ext      = set(config["filters"]["watch_extensions"].replace(" ", "").split(","))
    ignore_pfx     = config["filters"]["ignore_prefixes"].replace(" ", "").split(",")
    hash_algorithm = config["snapshot"]["hash_algorithm"]

    log.info("Scanning for offline changes...")

    old_snapshot     = db.get_all_snapshots()
    current_snapshot = scan_directory(
        watch_dir, watch_ext, ignore_pfx, hash_algorithm, recursive,
        old_snapshot=old_snapshot
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


# ------------------------------------------------------------------
# OBSERVER MANAGEMENT — handles network drive disconnects
# ------------------------------------------------------------------

def start_observer(watch_dir: str, recursive: bool, handler: FileWatchHandler):
    """Creates and starts a fresh watchdog Observer."""
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=recursive)
    observer.start()
    return observer


def run_with_reconnect(watch_dir: str, recursive: bool,
                       handler: FileWatchHandler, reconnect_delay: int = 30):
    """
    Keeps the watchdog Observer alive indefinitely.
    If the watched path becomes unreachable (network drive goes offline),
    the observer is stopped and the script waits reconnect_delay seconds
    before trying again. Retries until the drive comes back online.
    This prevents the script from silently dying mid-session.
    """
    observer = None

    while True:
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
                time.sleep(reconnect_delay)
                continue

            if observer is None:
                observer = start_observer(watch_dir, recursive, handler)
                log.info("Observer started. Watching: %s", watch_dir)

            time.sleep(1)

            # If watchdog's internal thread died unexpectedly, restart it
            if not observer.is_alive():
                log.error("Observer thread died unexpectedly. Restarting...")
                observer = None

        except KeyboardInterrupt:
            log.info("Shutdown requested.")
            break
        except Exception as e:
            log.error("Unexpected error in observer loop: %s", e, exc_info=True)
            if observer and observer.is_alive():
                observer.stop()
                observer.join()
            observer = None
            time.sleep(reconnect_delay)

    if observer and observer.is_alive():
        observer.stop()
        observer.join()


# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main():
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.ini")

    config    = load_config(config_path)
    watch_dir = config["watcher"]["watch_directory"]
    log_dir   = config["storage"]["log_directory"]
    db_name   = config["storage"]["db_name"]
    recursive = config["watcher"].getboolean("recursive", True)
    retention = config["storage"].getint("retention_days", 90)
    reconnect_delay = config["watcher"].getint("reconnect_delay", 30)

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
        while not os.path.isdir(watch_dir):
            time.sleep(reconnect_delay)
        log.info("Watch directory is now available.")

    # Step 3: open database
    db_path = os.path.join(log_dir, db_name)
    db      = Database(db_path)

    # Step 4: purge old events
    db.purge_old_events(retention)

    # Step 5: detect offline changes
    run_startup_diff(db, config)

    # Step 6: start live watcher with reconnect support
    handler = FileWatchHandler(db, config)
    log.info("Live watcher active. Press Ctrl+C to stop.")

    run_with_reconnect(watch_dir, recursive, handler, reconnect_delay)

    db.close()
    log.info("File Watcher stopped.")


if __name__ == "__main__":
    main()