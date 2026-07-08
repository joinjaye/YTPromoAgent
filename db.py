import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent / "data" / "leads.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def init_db():
    with _connect() as conn:
        # Auto-increment counter for Feishu primary field IDs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS record_counter (
                id    INTEGER PRIMARY KEY CHECK (id = 1),
                value INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO record_counter VALUES (1, 0)")

        # Local copy of extracted leads (source of truth for the dashboard).
        # UNIQUE guards against re-processing the same video/platform/link pair.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                youtuber          TEXT    NOT NULL,
                promo_platform    TEXT    NOT NULL DEFAULT '',
                promo_link        TEXT    NOT NULL DEFAULT '',
                video_url         TEXT    NOT NULL DEFAULT '',
                feishu_record_id  TEXT    DEFAULT '',
                published_at      TEXT    DEFAULT '',
                created_at        INTEGER,
                UNIQUE(video_url, promo_platform, promo_link)
            )
        """)
        _migrate_leads(conn)

        # crawl_log tracked per-query incremental crawl timestamps; the crawler
        # now uses a fixed "previous calendar day" window instead, so it's gone.
        conn.execute("DROP TABLE IF EXISTS crawl_log")

        conn.commit()
    print("[DB] 初始化完成")


def _migrate_leads(conn: sqlite3.Connection):
    """Add new columns to an existing leads table (safe to run on fresh DBs too)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "published_at" not in existing:
        conn.execute("ALTER TABLE leads ADD COLUMN published_at TEXT DEFAULT ''")
        print("[DB] leads 表迁移新增列: published_at")


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


def save_leads(records: list[dict], created_at: int | None = None):
    """
    Persist extracted leads locally (source of truth for the dashboard).
    Each record: {youtuber, promo_platform, promo_link, video_url, published_at?}.
    Duplicates (same video_url/promo_platform/promo_link) are silently skipped.
    """
    if not records:
        return
    ts = created_at if created_at is not None else _now_ms()
    with _connect() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO leads
              (youtuber, promo_platform, promo_link, video_url, feishu_record_id, published_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["youtuber"],
                    r.get("promo_platform", ""),
                    r.get("promo_link", ""),
                    r.get("video_url", ""),
                    r.get("feishu_record_id", ""),
                    r.get("published_at", ""),
                    ts,
                )
                for r in records
            ],
        )
        conn.commit()
