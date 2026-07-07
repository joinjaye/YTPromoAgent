import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent / "data" / "leads.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_log (
                search_query    TEXT PRIMARY KEY,
                last_crawled_at TEXT NOT NULL
            )
        """)
        # Auto-increment counter for Feishu primary field IDs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS record_counter (
                id    INTEGER PRIMARY KEY CHECK (id = 1),
                value INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO record_counter VALUES (1, 0)")
        conn.commit()
    print("[DB] 初始化完成")


def get_last_crawl_time(search_query: str) -> str | None:
    """Return the last crawled timestamp for this query (RFC3339), or None if never crawled."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_crawled_at FROM crawl_log WHERE search_query = ?",
            (search_query,),
        ).fetchone()
        return row["last_crawled_at"] if row else None


def update_crawl_log(search_query: str):
    now_rfc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO crawl_log (search_query, last_crawled_at)
            VALUES (?, ?)
            ON CONFLICT(search_query) DO UPDATE SET last_crawled_at = excluded.last_crawled_at
            """,
            (search_query, now_rfc),
        )
        conn.commit()


def allocate_record_ids(count: int) -> range:
    """
    Atomically reserve `count` sequential IDs for Feishu primary field.
    Returns a range object, e.g. range(1, 4) for 3 IDs → [1, 2, 3].
    """
    if count <= 0:
        return range(0, 0)
    with _connect() as conn:
        conn.execute(
            "UPDATE record_counter SET value = value + ? WHERE id = 1", (count,)
        )
        end = conn.execute("SELECT value FROM record_counter").fetchone()[0]
        conn.commit()
    return range(end - count + 1, end + 1)
