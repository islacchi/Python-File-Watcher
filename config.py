"""
config.py — Configuration loading and validation
Centralizes all config.ini reading so every other module receives
a fully validated ConfigParser object rather than reading the file
independently or crashing mid-run on a missing key.

Usage:
    from config import load_config
    config = load_config()          # reads config.ini next to this file
    config = load_config("/path")   # reads from an explicit path
"""

import os
import sys
import configparser
from logger import get_logger

log = get_logger(__name__)

# Keys that must exist for the script to run — checked on load
REQUIRED_KEYS = {
    "watcher":  ["watch_directory"],
    "filters":  ["watch_extensions", "ignore_prefixes"],
    "storage":  ["log_directory", "db_name"],
    "snapshot": ["hash_algorithm"],
}


def load_config(config_path: str = None) -> configparser.ConfigParser:
    """
    Reads and validates config.ini.

    If config_path is not provided, looks for config.ini in the same
    directory as this file (i.e. next to main.py).

    Exits with a clear error message if:
      - The file does not exist
      - Any required key is missing
    """
    if config_path is None:
        script_dir  = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "config.ini")

    if not os.path.exists(config_path):
        print(f"[ERROR] config.ini not found at: {config_path}")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(config_path)

    _validate(config, config_path)

    log.info("Config loaded from: %s", config_path)
    return config


def _validate(config: configparser.ConfigParser, config_path: str):
    """
    Checks that all required sections and keys are present.
    Exits with a descriptive error if anything is missing so the user
    knows exactly what to fix rather than getting a KeyError mid-run.
    """
    missing = []

    for section, keys in REQUIRED_KEYS.items():
        if not config.has_section(section):
            missing.append(f"  Missing section: [{section}]")
            continue
        for key in keys:
            if not config.has_option(section, key):
                missing.append(f"  Missing key: {key} under [{section}]")

    if missing:
        print(f"[ERROR] config.ini at {config_path} is incomplete:")
        for m in missing:
            print(m)
        sys.exit(1)