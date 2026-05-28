"""
query.py — Command-line log viewer
Run this script to search and filter the events log without opening DB Browser.

Usage examples:
  python query.py                          # show last 50 events
  python query.py --limit 100             # show last 100 events
  python query.py --type DELETED          # filter by event type
  python query.py --type RENAMED          # filter by event type
  python query.py --file budget.xlsx      # search by filename
  python query.py --today                 # events from today only
  python query.py --date 2026-05-26       # events from a specific date
  python query.py --summary              # count of each event type
"""

import sqlite3
import argparse
import os
import sys
from datetime import datetime, date


# ------------------------------------------------------------------
# CONFIG — update db_path if your log directory differs
# ------------------------------------------------------------------

DEFAULT_DB_PATH = r"C:\Users\primelink\Desktop\LOGS\filelog.db"


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def get_connection(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f"[ERROR] Database not found at: {db_path}")
        print("Make sure the file watcher has been run at least once.")
        sys.exit(1)
    return sqlite3.connect(db_path)


def format_row(row: tuple) -> str:
    """Formats a single event row for readable terminal output."""
    id_, timestamp, event_type, src_path, dest_path, file_size, md5_hash, prev_hash = row
    size_str = f"{file_size:,} bytes" if file_size else "N/A"

    if dest_path:
        return (f"[{timestamp}] {event_type}\n"
                f"  FROM : {src_path}\n"
                f"  TO   : {dest_path}\n"
                f"  SIZE : {size_str}\n")
    elif event_type in ("MODIFIED", "MODIFIED (offline)") and prev_hash:
        return (f"[{timestamp}] {event_type}\n"
                f"  PATH : {src_path}\n"
                f"  SIZE : {size_str}\n"
                f"  BEFORE: {prev_hash}\n"
                f"  AFTER : {md5_hash}\n")
    else:
        return (f"[{timestamp}] {event_type}\n"
                f"  PATH : {src_path}\n"
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
        SELECT id, timestamp, event_type, src_path, dest_path, file_size, md5_hash, prev_hash
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

    max_len = max(len(r[0]) for r in rows)
    for event_type, count in rows:
        print(f"  {event_type:<{max_len}}  {count:>6} event(s)")

    total = sum(r[1] for r in rows)
    print(f"\n  {'TOTAL':<{max_len}}  {total:>6} event(s)\n")


# ------------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Query the file watcher event log."
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH,
        help="Path to filelog.db (default: configured path)"
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

    args = parser.parse_args()
    conn = get_connection(args.db)

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