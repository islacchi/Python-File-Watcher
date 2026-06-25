"""
logger.py — Centralized logging setup
Sets up a rotating file logger and a console handler so all modules
write to both the terminal and a persistent .log file simultaneously.

Usage in any module:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("Something happened")
    log.warning("Something looks wrong")
    log.error("Something broke")
"""

import logging
import os
from logging.handlers import RotatingFileHandler

# Silences 'No handlers could be found' warnings for log calls issued
# before setup_logging() is called (e.g. config.py at import time).
# setup_logging() strips this before installing the real handlers.
logging.getLogger("filewatcher").addHandler(logging.NullHandler())

def setup_logging(log_directory: str):
    """
    Call once from main.py on startup.
    Creates a rotating file handler that writes to filewatcher.log inside
    log_directory. Rotates when the file hits 5MB, keeps the last 5 files.
    Also attaches a console handler so output still appears in the terminal.
    """
    os.makedirs(log_directory, exist_ok=True)
    log_path = os.path.join(log_directory, "filewatcher.log")

    root_logger = logging.getLogger("filewatcher")
    root_logger.setLevel(logging.DEBUG)

    # Remove bootstrap NullHandler before adding real handlers
    root_logger.handlers = [
        h for h in root_logger.handlers
        if not isinstance(h, logging.NullHandler)
    ]

    # Avoid adding duplicate handlers if setup_logging is called more than once
    if root_logger.handlers:
        return

    formatter = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )

    # Rotating file handler — 5MB per file, keep last 5 files
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Console handler — INFO and above only to keep terminal readable
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Returns a child logger under the 'filewatcher' namespace.
    Example: get_logger(__name__) in db.py returns 'filewatcher.db'
    All child loggers inherit the handlers set up in setup_logging().
    """
    return logging.getLogger(f"filewatcher.{name}")