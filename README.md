# File Watcher

Monitors a directory for changes to Excel, Word, PDF, and image files.
Logs all events (create, modify, delete, rename, move) to a SQLite database.
Detects changes that occurred while the script was not running on every restart.

---

## Project Structure

```
filewatcher/
├── config.ini        ← your configuration (edit this)
├── main.py           ← entry point
├── db.py             ← SQLite database layer
├── handler.py        ← live watchdog event handler
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
- `watch_directory` → the folder you want to monitor (can be a network drive e.g. `K:\`)
- `log_directory`   → where the SQLite database will be saved (keep OUTSIDE watch_directory)

> **Note:** If `watch_directory` points to a large drive or network share, the first startup
> scan will take longer as it hashes all matching files. Consider pointing to a specific
> subfolder instead of the root of a drive.

### 4. Run manually to test
```
python main.py
```

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
   - Program: `C:\Python312\python.exe`   (use your actual Python path — run `where python` to find it)
   - Arguments: `main.py`
   - Start in: `C:\path\to\filewatcher`   (full path to this folder)

5. **Settings tab**
   - UNCHECK: "Stop the task if it runs longer than 3 days"
   - Select: "Do not start a new instance" if already running

---

## Reading the Logs

The SQLite database at your `log_directory` can be opened with:
- DB Browser for SQLite (free GUI): https://sqlitebrowser.org
- Or query directly in Python:

```python
import sqlite3
conn = sqlite3.connect(r"C:\Users\primelink\Desktop\LOGS\filelog.db")
for row in conn.execute("SELECT * FROM events ORDER BY timestamp DESC LIMIT 50"):
    print(row)
```

### Events table columns
| Column     | Description                                                        |
|------------|--------------------------------------------------------------------|
| timestamp  | ISO 8601 datetime of the event                                     |
| event_type | See event types below                                              |
| src_path   | File path where the event occurred (source path for renames/moves) |
| dest_path  | Destination path — populated for RENAMED, MOVED, MOVED_AND_RENAMED |
| file_size  | Size in bytes at time of event                                     |
| md5_hash   | MD5 fingerprint of file contents                                   |

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

### Useful queries

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