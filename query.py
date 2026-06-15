"""
query.py — Command-line log viewer
Run this script to search and filter the events log without opening DB Browser.

Reads the database path from config.ini by default.
Override with --db if needed.

Usage examples:
  python query.py                          # show last 50 events
  python query.py --limit 100             # show last 100 events
  python query.py --type DELETED          # filter by event type
  python query.py --type RENAMED          # filter by event type
  python query.py --file budget.xlsx      # search by filename
  python query.py --today                 # events from today only
  python query.py --date 2026-05-26       # events from a specific date
  python query.py --summary              # count of each event type
  python query.py --db C:\path\to\filelog.db  # override database path
"""

import sqlite3
import argparse
import configparser
import os
import sys
from datetime import date


# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------

def resolve_db_path(override: str | None) -> str:
    """
    Returns the database path to use, in order of priority:
      1. --db argument if provided
      2. log_directory + db_name from config.ini in the same directory
      3. Exits with a clear error if neither is available
    """
    if override:
        return override

    script_dir  = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.ini")

    if not os.path.exists(config_path):
        print("[ERROR] config.ini not found and no --db path provided.")
        print(f"        Expected config at: {config_path}")
        print("        Run: python query.py --db C:\\path\\to\\filelog.db")
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(config_path)

    try:
        log_dir = config["storage"]["log_directory"]
        db_name = config["storage"].get("db_name", "filelog.db")
    except KeyError:
        print("[ERROR] config.ini is missing [storage] section or log_directory key.")
        sys.exit(1)

    return os.path.join(log_dir, db_name)


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f"[ERROR] Database not found at: {db_path}")
        print("Make sure the file watcher has been run at least once.")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # named column access instead of positional
    return conn


def format_row(row: sqlite3.Row) -> str:
    """Formats a single event row for readable terminal output."""
    size_str = f"{row['file_size']:,} bytes" if row['file_size'] else "N/A"

    if row['dest_path']:
        return (f"[{row['timestamp']}] {row['event_type']}\n"
                f"  FROM : {row['src_path']}\n"
                f"  TO   : {row['dest_path']}\n"
                f"  SIZE : {size_str}\n")
    elif row['event_type'] in ("MODIFIED", "MODIFIED (offline)") and row['prev_hash']:
        return (f"[{row['timestamp']}] {row['event_type']}\n"
                f"  PATH  : {row['src_path']}\n"
                f"  SIZE  : {size_str}\n"
                f"  BEFORE: {row['prev_hash']}\n"
                f"  AFTER : {row['md5_hash']}\n")
    else:
        return (f"[{row['timestamp']}] {row['event_type']}\n"
                f"  PATH : {row['src_path']}\n"
                f"  SIZE : {size_str}\n")


# ------------------------------------------------------------------
# QUERY FUNCTIONS
# ------------------------------------------------------------------

def query_events(conn: sqlite3.Connection, event_type: str = None,
                 filename: str = None, date_filter: str = None,
                 limit: int = 50) -> list:
    """
    Builds and runs a filtered SELECT query against the events table.
    All filters are optional and stack — you can combine --type and --file.
    """
    conditions = []
    params     = []

    if event_type:
        conditions.append("event_type LIKE ?")
        params.append(f"%{event_type.upper()}%")

    if filename:
        conditions.append("(src_path LIKE ? OR dest_path LIKE ?)")
        params.append(f"%{filename}%")
        params.append(f"%{filename}%")

    if date_filter:
        conditions.append("timestamp LIKE ?")
        params.append(f"{date_filter}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    query = f"""
        SELECT id, timestamp, event_type, src_path, dest_path,
               file_size, md5_hash, prev_hash
        FROM events
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
    """

    return conn.execute(query, params).fetchall()


def query_summary(conn: sqlite3.Connection):
    """Prints a count of each event type — useful for a quick overview."""
    rows = conn.execute("""
        SELECT event_type, COUNT(*) as count
        FROM events
        GROUP BY event_type
        ORDER BY count DESC
    """).fetchall()

    print("\n=== EVENT SUMMARY ===\n")
    if not rows:
        print("No events logged yet.")
        return

    max_len = max(len(r['event_type']) for r in rows)
    for row in rows:
        print(f"  {row['event_type']:<{max_len}}  {row['count']:>6} event(s)")

    total = sum(r['count'] for r in rows)
    print(f"\n  {'TOTAL':<{max_len}}  {total:>6} event(s)\n")


# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Query the file watcher event log."
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to filelog.db (default: read from config.ini)"
    )
    parser.add_argument(
        "--type", dest="event_type",
        help="Filter by event type (e.g. DELETED, RENAMED, MOVED, CREATED, MODIFIED)"
    )
    parser.add_argument(
        "--file", dest="filename",
        help="Filter by filename or partial path"
    )
    parser.add_argument(
        "--today", action="store_true",
        help="Show only events from today"
    )
    parser.add_argument(
        "--date",
        help="Show events from a specific date (format: YYYY-MM-DD)"
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum number of results to show (default: 50)"
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Show a count of each event type instead of individual events"
    )

    args    = parser.parse_args()
    db_path = resolve_db_path(args.db)
    conn    = get_connection(db_path)

    if args.summary:
        query_summary(conn)
        conn.close()
        return

    date_filter = None
    if args.today:
        date_filter = date.today().isoformat()
    elif args.date:
        date_filter = args.date

    rows = query_events(
        conn,
        event_type=args.event_type,
        filename=args.filename,
        date_filter=date_filter,
        limit=args.limit
    )

    print(f"\n=== {len(rows)} EVENT(S) ===\n")

    if not rows:
        print("No events match your filter.")
    else:
        for row in rows:
            print(format_row(row))

    conn.close()


if __name__ == "__main__":
    main()