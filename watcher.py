"""
watcher.py — Observer lifecycle and reconnect loop
Responsible for starting and keeping the watchdog Observer alive.
If the watched network drive goes offline, this module waits and
retries automatically without crashing the script.

Separating this from main.py means adding a second watched directory
in the future only requires changes here, not in the entry point.
"""
from __future__ import annotations
from collections.abc import Callable
import os
import time
from typing import TYPE_CHECKING
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

if TYPE_CHECKING:
    from handler import FileWatchHandler

from logger import get_logger


log = get_logger(__name__)
_JOIN_TIMEOUT = 10
_HEARTBEAT_INTERVAL = 60


def start_observer(watch_dir: str, recursive: bool,
                   handler: FileWatchHandler) -> BaseObserver:
    """Creates, schedules, and starts a fresh watchdog Observer."""
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=recursive)
    observer.start()
    return observer


def run_with_reconnect(watch_dir: str, recursive: bool,
                       handler: FileWatchHandler,
                       reconnect_delay: int = 30,
                       shutdown_flag: Callable[[], bool] | None = None):
    """
    Keeps the watchdog Observer alive indefinitely.

    If the watched path becomes unreachable (network drive goes offline),
    the observer is stopped and the script waits reconnect_delay seconds
    before trying again. Retries until the drive comes back online.

    shutdown_flag is a callable that returns True when a clean shutdown
    has been requested (e.g. via SIGTERM). Defaults to always False if
    not provided — useful for testing.
    """
    if shutdown_flag is None:
        def _default_shutdown() -> bool:
            return False
        shutdown_flag = _default_shutdown

    observer = None

    while not shutdown_flag():
        try:
            if not os.path.exists(watch_dir):
                log.warning(
                    "Watch directory unreachable: %s. "
                    "Retrying in %d seconds...", watch_dir, reconnect_delay
                )
                if observer and observer.is_alive():
                    observer.stop()
                    observer.join(timeout=_JOIN_TIMEOUT)
                    observer = None

                for _ in range(reconnect_delay):
                    if shutdown_flag():
                        break
                    time.sleep(1)
                continue

            if observer is None:
                observer = start_observer(watch_dir, recursive, handler)
                log.info("Observer started. Watching: %s", watch_dir)

            for _ in range(_HEARTBEAT_INTERVAL):
                if shutdown_flag():
                    break
                if not observer.is_alive():
                    break
                time.sleep(1)

            if observer and not observer.is_alive():
                log.error("Observer thread died unexpectedly. Restarting...")
                observer = None

        except Exception as e:
            log.error("Unexpected error in observer loop: %s", e, exc_info=True)
            if observer and observer.is_alive():
                observer.stop()
                observer.join(timeout=_JOIN_TIMEOUT)
            observer = None
            for _ in range(reconnect_delay):
                if shutdown_flag():
                    break
                time.sleep(1)

    if observer and observer.is_alive():
        observer.stop()
        observer.join(timeout=_JOIN_TIMEOUT)