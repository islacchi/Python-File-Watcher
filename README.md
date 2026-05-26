# File Watcher

Monitors a directory for changes to Excel, Word, PDF, and image files.
Logs all events (create, modify, delete, move/rename) to a SQLite database.
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
- `watch_directory` → the folder you want to monitor
- `log_directory`   → where the SQLite database will be saved (keep OUTSIDE watch_directory)

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

The SQLite database at your log_directory can be opened with:
- DB Browser for SQLite (free GUI): https://sqlitebrowser.org
- Or query directly in Python:

```python
import sqlite3
conn = sqlite3.connect(r"C:\Logs\filewatcher\filelog.db")
for row in conn.execute("SELECT * FROM events ORDER BY timestamp DESC LIMIT 50"):
    print(row)
```

### Events table columns
| Column     | Description                              |
|------------|------------------------------------------|
| timestamp  | ISO 8601 datetime of the event           |
| event_type | CREATED / MODIFIED / DELETED / MOVED     |
| src_path   | File path (source for moves)             |
| dest_path  | Destination path (MOVED events only)     |
| file_size  | Size in bytes at time of event           |
| md5_hash   | MD5 fingerprint of file contents         |

---

## Linux / macOS (systemd)

For persistent background service on Linux, create `/etc/systemd/system/filewatcher.service`:

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
