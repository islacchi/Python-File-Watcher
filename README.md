# File Watcher

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-1-lightgrey)

Monitors a directory for changes to Excel, Word, PDF, and image files.
Logs all events (create, modify, delete, rename, move) to a SQLite database.
Detects changes that occurred while the script was not running on every restart.
Auto-recovers if the watched drive goes offline.

---

## Prerequisites

- Python 3.10 or higher
- `watchdog` — the only dependency, installed via `requirements.txt`
- No database server, no running services, no XAMPP — SQLite is built into Python

---

## Project Structure

```
filewatcher/
├── config.ini        ← your configuration (edit this)
├── main.py           ← entry point
├── db.py             ← SQLite database layer
├── diff.py           ← startup diff and directory scanner
├── handler.py        ← live watchdog event handler
├── logger.py         ← centralized logging setup
├── utils.py          ← shared hashing and path utilities
├── watcher.py        ← observer startup and reconnect loop
├── query.py          ← CLI tool for reading logs
├── .gitignore        ← excludes cache, db, and log files from git
└── requirements.txt  ← Python dependencies
```

---

## Setup

### 1. Install Python 3.10+
Download from https://python.org — check "Add Python to PATH" during install.

### 2. Install dependencies
Open a terminal in the filewatcher folder and run:
```
pip install -r requirements.txt
```

### 3. Edit config.ini
Change at minimum:
- `watch_directory` → the folder you want to monitor (can be a UNC path e.g. `\\server\share`)
- `log_directory`   → where the SQLite database and log file will be saved (keep OUTSIDE watch_directory)

> **Note on large drives:** If `watch_directory` points to the root of a large drive or
> network share, the first startup scan will take longer as it hashes all matching files.
> The terminal will show an estimated time to completion and progress updates every 50 files.
> Every subsequent startup is significantly faster due to the mtime pre-filter.

### 4. Run manually to test
```
python main.py
```

---

## Configuration Reference

All settings live in `config.ini`. No code changes needed.

| Setting | Section | Default | Description |
|---|---|---|---|
| `watch_directory` | `[watcher]` | — | Full path to the directory to monitor |
| `recursive` | `[watcher]` | `true` | Watch subdirectories recursively |
| `reconnect_delay` | `[watcher]` | `30` | Seconds to wait before retrying if drive goes offline |
| `move_window` | `[watcher]` | `10` | Seconds to hold unmatched creates/deletes before confirming them as genuine events |
| `heartbeat_interval` | `[watcher]` | `5` | Seconds between heartbeat writes to the config table (used by Laravel UI health check) |
| `watch_extensions` | `[filters]` | see file | Whitelist of file extensions to track |
| `ignore_prefixes` | `[filters]` | `~$, .~, ~` | Filename prefixes to ignore (Office lock files) |
| `exclude_directories` | `[filters]` | — | Directory names or paths to skip entirely |
| `log_directory` | `[storage]` | — | Where to save `filelog.db` and `filewatcher.log` |
| `db_name` | `[storage]` | `filelog.db` | SQLite database filename |
| `retention_days` | `[storage]` | `0` | Days to keep events before auto-purge (0 = keep forever) |
| `hash_algorithm` | `[snapshot]` | `md5` | Hashing algorithm for file fingerprinting |

---

## Log Files

Two log outputs are written to `log_directory` on every run:

- **`filelog.db`** — SQLite database containing all file change events and the current snapshot
- **`filewatcher.log`** — rotating text log of all script activity including startup, errors, and reconnects. Rotates at 5MB, keeps last 5 files.

---

## Reading the Logs

### Option 1 — CLI query tool (quickest)

```bash
python query.py                          # last 50 events
python query.py --limit 100             # show more results
python query.py --type DELETED          # filter by event type
python query.py --type RENAMED          # filter by event type
python query.py --file budget.xlsx      # search by filename
python query.py --today                 # events from today only
python query.py --date 2026-05-26       # events from a specific date
python query.py --summary               # count of each event type
```

Filters stack — `--type DELETED --today` shows only today's deletes.

### Option 2 — DB Browser for SQLite (visual)

Download free from https://sqlitebrowser.org. Open `filelog.db` from your
`log_directory`, click the **Browse Data** tab, and select the `events` or
`snapshots` table from the dropdown.

### Option 3 — Python directly

```python
import sqlite3
conn = sqlite3.connect(r"C:\path\to\LOGS\filelog.db")
for row in conn.execute("SELECT * FROM events ORDER BY timestamp DESC LIMIT 50"):
    print(row)
```

---

## Events Table Reference

### Columns
| Column     | Description                                                         |
|------------|---------------------------------------------------------------------|
| timestamp  | ISO 8601 datetime of the event                                      |
| event_type | See event types table below                                         |
| src_path   | File path where the event occurred (source path for renames/moves)  |
| dest_path  | Destination path — populated for RENAMED, MOVED, MOVED_AND_RENAMED  |
| file_size  | Size in bytes at time of event                                      |
| md5_hash   | MD5 fingerprint of file contents after the event                    |
| prev_hash  | MD5 fingerprint before the change — populated for MODIFIED events only |

### Event types
| Event type                    | Meaning                                              |
|-------------------------------|------------------------------------------------------|
| `CREATED`                     | A new file appeared in the watched directory         |
| `MODIFIED`                    | An existing file's contents changed                  |
| `DELETED`                     | A file was permanently removed                       |
| `RENAMED`                     | Filename changed, file stayed in the same folder     |
| `MOVED`                       | File moved to a different folder, filename unchanged |
| `MOVED_AND_RENAMED`           | File moved to a different folder and renamed         |
| `CREATED (offline)`           | File was created while the script was not running    |
| `MODIFIED (offline)`          | File was modified while the script was not running   |
| `DELETED (offline)`           | File was deleted while the script was not running    |
| `RENAMED (offline)`           | File was renamed while the script was not running    |
| `MOVED (offline)`             | File was moved while the script was not running      |
| `MOVED_AND_RENAMED (offline)` | File was moved and renamed while script was off      |

### Useful SQL queries

**See only deleted files:**
```sql
SELECT * FROM events WHERE event_type LIKE '%DELETED%'
```

**See only renames:**
```sql
SELECT * FROM events WHERE event_type LIKE '%RENAMED%'
```

**See all offline changes:**
```sql
SELECT * FROM events WHERE event_type LIKE '%offline%'
```

**Track a specific file:**
```sql
SELECT * FROM events WHERE src_path LIKE '%filename.pdf%'
```

**Find all events sharing the same file version:**
```sql
SELECT * FROM events WHERE md5_hash = 'paste_hash_here'
```

**Events from a specific date:**
```sql
SELECT * FROM events WHERE timestamp LIKE '2026-05-26%'
```

---

## Path Normalization

All file paths are stored and compared as lowercase strings. This prevents
false-positive `DELETED (offline)` events on Windows network drives where
`os.walk()` and the stored snapshot may return the same path in different
cases (e.g. `\\Kyle\bid docs\` vs `\\kyle\bid docs\`).

> **Note:** The original casing of filenames and directories is not preserved
> in the database. This is intentional — see Known Limitations below.

> **If you reset the snapshots table** (e.g. `DELETE FROM snapshots`), the next
> startup will log every existing file as `CREATED (offline)`. This is expected
> and only happens once — the snapshot rebuilds itself on that restart and all
> subsequent startups will diff correctly.

---

## Companion UI

A Laravel web interface for this script is available at:

```bash
git clone https://github.com/islacchi/File-Watcher.git
```

It reads directly from `filelog.db` and provides a dashboard, analytics charts,
events log with hash-based file lineage search, snapshot browser, and live
health monitoring via the heartbeat and status keys.

---

## Config Table

The `config` table in `filelog.db` stores script metadata readable by external
tools such as the Laravel UI:

| Key | Description |
|-----|-------------|
| `watch_directory` | The directory currently being monitored |
| `log_directory` | Where logs and the database are stored |
| `retention_days` | Current retention setting |
| `script_version` | Version string from `main.py` |
| `started_at` | ISO 8601 timestamp of last startup |
| `status` | `scanning` during startup diff, `live` when active, `offline` on clean shutdown |
| `heartbeat` | ISO 8601 timestamp updated every `heartbeat_interval` seconds — used to determine if the script is currently alive |

---

## Architecture

```
python main.py
      │
      ▼
Load config.ini          [config.py]
      │
      ▼
Setup logging            [logger.py]  → filewatcher.log + console
      │
      ▼
Watch dir available?     [main.py]    → waits if drive not mounted yet
      │
      ▼
Open / create database   [db.py]      → filelog.db, creates tables + indexes
      │
      ▼
Purge old events         [db.py]      → deletes rows older than retention_days
      │                               → 0 = keep forever
      ▼
── STARTUP DIFF ──────────────────────────────────────────────────
      │
      ▼
Scan watch directory     [diff.py]    → normalize paths to lowercase
      │                               → mtime pre-filter → reuse stored hash
      │                               → parallel hash remaining files + ETA
      ▼
Diff snapshot vs disk    [diff.py]    → hash match: RENAMED / MOVED /
      │                                 MOVED_AND_RENAMED / CREATED / DELETED
      ▼
Log offline events       [db.py]      → log_events_batch() + upsert_snapshots_batch()
      │                               → db.flush() ensures single commit
      ▼
── HEARTBEAT ─────────────────────────────────────────────────────
      │
      ▼
Heartbeat thread starts  [main.py]    → daemon thread, upserts config.heartbeat
      │                                 every heartbeat_interval seconds
      ▼
── LIVE WATCHER ──────────────────────────────────────────────────
      │
      ▼
Watchdog observer starts [watcher.py] → attached to watch_dir, auto-reconnects
      │
      ▼ (loops on every file system event)
File system event fires  [handler.py] → on_created / on_modified
      │                                  on_deleted / on_moved
      ▼
Extension + prefix filter             → skip ignore_prefixes, check whitelist
      │
      ▼
Classify event           [handler.py]
      │
      │  on_deleted  → hash stored in pending_deletes (path, hash) composite key
      │               → sweep thread confirms DELETE after move_window expires
      │
      │  on_created  → retries compute_hash up to 5x with 0.5s delay
      │               → (UNC shares fire CREATE before file is fully written)
      │               → checks pending_deletes for hash match → MOVED/RENAMED
      │               → no match yet → parks in pending_creates and waits
      │
      │  on_deleted  → also checks pending_creates for CREATE-before-DELETE
      │  (cont.)       moves (SMB can fire CREATE before DELETE on UNC paths)
      │               → hash match found → MOVED/RENAMED logged immediately
      │
      │  on_moved    → watchdog sees both sides → clean MOVED/RENAMED, no
      │               hash matching needed
      ▼
Log live event           [db.py]      → log_event() + upsert_snapshot()
      │                               → all paths normalized to lowercase
      │                               → writes batched, committed every 50
      │                                 writes or 1 second, whichever first
      │
      └──────────────────────────────── loops back to next event

── QUERY (separate tool) ─────────────────────────────────────────

python query.py          [query.py]   → reads filelog.db directly
                                      → filter by type, file, date
```

---

## Known Limitations

- **First run on large drives is slow** — every matching file must be MD5 hashed to build the initial snapshot. On a network drive with thousands of files this can take several minutes. Every run after the first is fast due to the mtime pre-filter.

- **Move detection on UNC network shares** — Windows SMB can fire the `CREATE` event before the `DELETE` event for the same move operation, sometimes seconds apart. The script handles both orderings: DELETE-first moves are resolved via `pending_deletes`, CREATE-first moves are resolved via `pending_creates`. If no matching event arrives within `move_window` seconds, the events are confirmed as genuine CREATED and DELETED. Increase `move_window` in `config.ini` if moves on slow network drives are still not being detected correctly.

- **SMB file write lag** — on UNC network shares, the `CREATE` event can fire before the file's contents are fully written over the network. The script retries hashing the new file up to 5 times with a 0.5 second delay to give the transfer time to complete before attempting hash-based move resolution.

- **Bulk operations may cause missed live events (Windows)** — watchdog uses the `ReadDirectoryChangesW` API which has a fixed-size event buffer. If a large number of files change simultaneously (e.g. a bulk copy or mass rename), the buffer can overflow and watchdog will silently miss some live events. Any missed events will be detected and logged as `(offline)` variants on the next restart when the startup diff runs. This is an OS-level constraint with no workaround within the script.

- **Network drive hashing is slower than local** — MD5 hashing over a network connection is limited by network bandwidth, not disk speed. Pointing `watch_directory` to a specific subfolder rather than the drive root significantly reduces startup time.

- **No content logging** — the script records that a file changed and its MD5 hash, but does not store the file's contents or a diff of what changed inside it.

- **Paths stored as lowercase** — all paths in the database are normalized to lowercase. This is intentional (see Path Normalization above) but means the original casing of filenames and directories is not preserved in the log.

- **MODIFIED events on UNC shares** — many applications (Word, Excel) do not write to files in place. They write to a temp file and swap it in, which watchdog sees as DELETE + CREATE rather than MODIFIED. On UNC network shares this is common. The startup diff correctly identifies these as MODIFIED (offline) on the next restart by comparing hashes.

---

## Task Scheduler Setup (Windows)

To run automatically on startup:

1. Open Task Scheduler → Create Task
2. **General tab**
   - Name: File Watcher
   - Check: "Run whether user is logged on or not"
   - Check: "Run with highest privileges"

3. **Triggers tab**
   - New trigger → At startup

4. **Actions tab**
   - Action: Start a program
   - Program: `C:\Python312\python.exe` (run `where python` to find your actual path)
   - Arguments: `main.py`
   - Start in: `C:\path\to\filewatcher` (full path to this folder)

5. **Settings tab**
   - UNCHECK: "Stop the task if it runs longer than 3 days"
   - Select: "Do not start a new instance" if already running

> **Network drives:** If the watched share is not mounted yet when the script
> starts at boot, the script will wait patiently until the path becomes
> available rather than crashing.

---

## Linux / macOS (systemd)

For a persistent background service on Linux, create `/etc/systemd/system/filewatcher.service`:

```ini
[Unit]
Description=File Watcher

[Service]
ExecStart=/usr/bin/python3 /path/to/filewatcher/main.py
Restart=on-failure
WorkingDirectory=/path/to/filewatcher

[Install]
WantedBy=multi-user.target
```

Then enable it:
```
sudo systemctl enable filewatcher
sudo systemctl start filewatcher
```