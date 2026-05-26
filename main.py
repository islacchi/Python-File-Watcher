"""
main.py — Entry point
Execution order on every run:
  1. Load config.ini
  2. Open (or create) the SQLite database
  3. Run startup diff  → detect changes that happened while the script was off
  4. Start watchdog observer → catch live changes going forward
  5. Stay alive until Ctrl+C or Task Scheduler kills the process
"""

import os
import sys
import time
import configparser
from watchdog.observers import Observer

from db import Database
from handler import FileWatchHandler, compute_hash, get_file_info


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
                   hash_algorithm: str, recursive: bool) -> dict:
    """
    Walks the watch_directory right now and returns its current state as:
    { filepath: { size, mtime, md5_hash } }

    This is the "ground truth" snapshot of what actually exists on disk.
    We compare it against the last saved snapshot in SQLite to find changes.
    """
    current = {}

    walker = os.walk(watch_dir) if recursive else [
        (watch_dir, [], os.listdir(watch_dir))
    ]

    for root, _dirs, files in walker:
        for filename in files:
            # Skip ignored prefixes
            if any(prefix and filename.startswith(prefix) for prefix in ignore_prefixes):
                continue

            ext = os.path.splitext(filename)[1].lower()
            if ext not in watch_extensions:
                continue

            path = os.path.join(root, filename)
            size, mtime = get_file_info(path)
            file_hash = compute_hash(path, hash_algorithm)

            if size is not None:
                current[path] = {"size": size, "mtime": mtime, "md5_hash": file_hash}

    return current


def run_startup_diff(db: Database, config: configparser.ConfigParser):
    """
    Compares the saved SQLite snapshot against the actual current directory state.

    Logic:
      - Paths in snapshot but NOT on disk       → deleted (or moved) while offline
      - Paths on disk but NOT in snapshot        → created (or moved) while offline
      - Paths in both but hash changed           → modified while offline
      - Deleted path hash matches a created path → move/rename while offline
    """
    watch_dir      = config["watcher"]["watch_directory"]
    recursive      = config["watcher"].getboolean("recursive", True)
    watch_ext      = set(config["filters"]["watch_extensions"].replace(" ", "").split(","))
    ignore_pfx     = config["filters"]["ignore_prefixes"].replace(" ", "").split(",")
    hash_algorithm = config["snapshot"]["hash_algorithm"]

    print("[STARTUP] Scanning for offline changes...")

    old_snapshot     = db.get_all_snapshots()           # what SQLite remembers
    current_snapshot = scan_directory(                  # what's on disk right now
        watch_dir, watch_ext, ignore_pfx, hash_algorithm, recursive
    )

    old_paths     = set(old_snapshot.keys())
    current_paths = set(current_snapshot.keys())

    missing_paths = old_paths - current_paths           # gone from disk
    new_paths     = current_paths - old_paths           # new on disk
    common_paths  = old_paths & current_paths           # existed before and still exist

    # Build a reverse-lookup: hash → old_path for all missing files
    # This lets us detect moves/renames: a "new" file whose hash matches
    # a "missing" file is really the same file moved.
    deleted_by_hash = {
        old_snapshot[p]["md5_hash"]: p
        for p in missing_paths
        if old_snapshot[p]["md5_hash"]
    }

    resolved_as_move_old  = set()   # old paths explained by a move
    resolved_as_move_new  = set()   # new paths explained by a move

    # ---- OFFLINE MOVE DETECTION ----
    for path in new_paths:
        new_hash = current_snapshot[path]["md5_hash"]
        if new_hash and new_hash in deleted_by_hash:
            old_path = deleted_by_hash[new_hash]
            info = current_snapshot[path]
            db.log_event("MOVED (offline)", old_path, dest_path=path,
                         file_size=info["size"], md5_hash=new_hash)
            db.delete_snapshot(old_path)
            db.upsert_snapshot(path, info["size"], info["mtime"], new_hash)
            resolved_as_move_old.add(old_path)
            resolved_as_move_new.add(path)

    # ---- OFFLINE DELETES (genuinely gone, not a move) ----
    for path in missing_paths - resolved_as_move_old:
        db.log_event("DELETED (offline)", path)
        db.delete_snapshot(path)

    # ---- OFFLINE CREATES (genuinely new, not a move destination) ----
    for path in new_paths - resolved_as_move_new:
        info = current_snapshot[path]
        db.log_event("CREATED (offline)", path,
                     file_size=info["size"], md5_hash=info["md5_hash"])
        db.upsert_snapshot(path, info["size"], info["mtime"], info["md5_hash"])

    # ---- OFFLINE MODIFICATIONS ----
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
    print(f"[STARTUP] Done. {total_changes} offline change(s) detected.\n")


# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main():
    # Resolve config path relative to this script's location
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.ini")

    config    = load_config(config_path)
    watch_dir = config["watcher"]["watch_directory"]
    log_dir   = config["storage"]["log_directory"]
    db_name   = config["storage"]["db_name"]
    recursive = config["watcher"].getboolean("recursive", True)

    # Validate watch directory
    if not os.path.isdir(watch_dir):
        print(f"[ERROR] watch_directory does not exist: {watch_dir}")
        sys.exit(1)

    # Create log directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)

    # Open database (creates file if new)
    db_path = os.path.join(log_dir, db_name)
    db      = Database(db_path)

    # Phase 1: detect offline changes before starting live watcher
    run_startup_diff(db, config)

    # Phase 2: start live watchdog observer
    handler  = FileWatchHandler(db, config)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=recursive)
    observer.start()

    print(f"[LIVE] Watching : {watch_dir}")
    print(f"[LIVE] Log DB   : {db_path}")
    print("[LIVE] Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[STOPPING] Shutting down file watcher...")
        observer.stop()

    observer.join()
    db.close()
    print("[STOPPED]")


if __name__ == "__main__":
    main()
