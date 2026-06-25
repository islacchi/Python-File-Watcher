"""
diff.py — Startup diff and directory scanner
Responsible for two things only:
  1. Scanning the watch directory and returning its current state
  2. Comparing that state against the stored snapshot and logging
     any changes that occurred while the script was not running

Imports utils.py for hashing and path classification so that
handler.py and diff.py share the same implementations without
either depending on the other.
"""

import os
import time
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed

from logger import get_logger
from db import Database
from utils import compute_hash, get_file_info, classify_path_change

log = get_logger(__name__)


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
                             using a thread pool.
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
            # path      — lowercase-normalized; used as the DB/dict key only.
            #             NEVER pass to os.stat(), open(), or compute_hash() —
            #             on case-sensitive filesystems (Linux, most NFS/SMB
            #             mounts) a lowercased path may not exist and will
            #             raise FileNotFoundError.
            # real_path — original OS casing; use for all filesystem access.
            path      = os.path.join(root, filename).lower()
            real_path = os.path.join(root, filename)
            size, mtime = get_file_info(real_path)
            if size is not None:
                candidates.append((path, size, mtime, real_path))

    current  = {}
    to_hash  = []
    skipped  = 0

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
                        # Rate is total wall-clock throughput (files/sec).
                        # Do not divide by max_workers — elapsed already
                        # reflects parallelism. Dividing again produces
                        # ETAs 8x too short with 8 workers.
                        rate     = hashed_count / elapsed
                        left     = total - hashed_count
                        eta_secs = (left / rate) if rate > 0 else 0
                        log.info(
                            "Progress: %d / %d file(s) hashed. ETA: %s",
                            hashed_count, total, format_eta(eta_secs)
                        )

    return current


def run_startup_diff(db: Database, config: configparser.ConfigParser) -> int:
    """
    Compares the saved SQLite snapshot against the actual current directory state.
    Collects all changes into lists and commits them in three bulk operations.

    Both old_snapshot (from SQLite) and current_snapshot (from scan_directory)
    use lowercase-normalized paths, so set operations never produce false
    positives from case differences.

    Returns the total number of offline changes detected.
    """
    watch_dir      = config["watcher"]["watch_directory"]
    recursive      = config["watcher"].getboolean("recursive", True)
    watch_ext      = set(config["filters"]["watch_extensions"].replace(" ", "").split(","))
    ignore_pfx     = config["filters"]["ignore_prefixes"].replace(" ", "").split(",")
    hash_algorithm = config["snapshot"]["hash_algorithm"]

    exclude_dirs_raw = config["filters"].get("exclude_directories", "").strip()
    exclude_dirs = set()
    if exclude_dirs_raw:
        exclude_dirs = {
            d.strip() for d in exclude_dirs_raw.replace(" ", "").split(",") if d.strip()
        }

    log.info("Scanning for offline changes...")

    old_snapshot     = db.get_all_snapshots()
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

    events_to_log       = []
    snapshots_to_upsert = []
    snapshots_to_delete = []

    for path in new_paths:
        new_hash = current_snapshot[path]["md5_hash"]
        if new_hash and new_hash in deleted_by_hash:
            old_path   = deleted_by_hash[new_hash]
            info       = current_snapshot[path]
            event_type = classify_path_change(old_path, path) + " (offline)"
            events_to_log.append({
                "event_type": event_type,
                "src_path":   old_path,
                "dest_path":  path,
                "file_size":  info["size"],
                "md5_hash":   new_hash,
                "prev_hash":  None,
            })
            snapshots_to_delete.append(old_path)
            snapshots_to_upsert.append((path, info["size"], info["mtime"], new_hash))
            resolved_as_move_old.add(old_path)
            resolved_as_move_new.add(path)

    for path in missing_paths - resolved_as_move_old:
        events_to_log.append({
            "event_type": "DELETED (offline)",
            "src_path":   path,
            "dest_path":  None,
            "file_size":  None,
            "md5_hash":   None,
            "prev_hash":  None,
        })
        snapshots_to_delete.append(path)

    for path in new_paths - resolved_as_move_new:
        info = current_snapshot[path]
        events_to_log.append({
            "event_type": "CREATED (offline)",
            "src_path":   path,
            "dest_path":  None,
            "file_size":  info["size"],
            "md5_hash":   info["md5_hash"],
            "prev_hash":  None,
        })
        snapshots_to_upsert.append((path, info["size"], info["mtime"], info["md5_hash"]))

    for path in common_paths:
        old_hash = old_snapshot[path]["md5_hash"]
        new_hash = current_snapshot[path]["md5_hash"]
        if old_hash != new_hash:
            info = current_snapshot[path]
            events_to_log.append({
                "event_type": "MODIFIED (offline)",
                "src_path":   path,
                "dest_path":  None,
                "file_size":  info["size"],
                "md5_hash":   new_hash,
                "prev_hash":  old_hash,
            })
            snapshots_to_upsert.append((path, info["size"], info["mtime"], new_hash))

    # Commit all changes in three bulk operations
    db.delete_snapshots_batch(snapshots_to_delete)
    db.log_events_batch(events_to_log)
    db.upsert_snapshots_batch(snapshots_to_upsert)
    db.flush()

    total_changes = len(events_to_log)
    log.info("Startup diff done. %d offline change(s) detected.", total_changes)

    return total_changes