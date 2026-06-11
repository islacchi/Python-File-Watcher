"""
utils.py — Shared utility functions
Low-level helpers used by both the live event handler and the startup diff.
No business logic lives here — only pure functions that transform data.

Keeping these separate from handler.py and diff.py prevents circular imports
and gives every module a single clear responsibility.
"""

import os
import hashlib
from logger import get_logger

log = get_logger(__name__)

# Larger read buffer = faster hashing, especially on network drives
HASH_CHUNK_SIZE = 64 * 1024  # 64 KB


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
    src_dir  = os.path.dirname(os.path.abspath(src))
    dest_dir = os.path.dirname(os.path.abspath(dest))
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