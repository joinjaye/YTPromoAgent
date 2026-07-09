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


def save_leads(records: list[dict], created_at: int | None = None) -> list[dict]:
    """
    Persist extracted leads locally (source of truth for the dashboard).
    Each record: {youtuber, promo_platform, promo_link, video_url, published_at?}.
    Duplicates (same video_url/promo_platform/promo_link) are silently skipped.

    Returns the subset of records that were newly inserted (not already present).
    Callers should only forward this subset to Feishu/Lark, otherwise a repeat
    run within the same crawl window (e.g. a duplicate trigger) will re-write
    and re-notify for data that was already sent.
    """
    if not records:
        return []
    ts = created_at if created_at is not None else _now_ms()
    new_records = []
    with _connect() as conn:
        for r in records:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO leads
                  (youtuber, promo_platform, promo_link, video_url, feishu_record_id, published_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["youtuber"],
                    r.get("promo_platform", ""),
                    r.get("promo_link", ""),
                    r.get("video_url", ""),
                    r.get("feishu_record_id", ""),
                    r.get("published_at", ""),
                    ts,
                ),
            )
            if cur.rowcount > 0:
                new_records.append(r)
        conn.commit()
    return new_records


def get_unsynced_leads() -> list[dict]:
    """
    本地已保存、但还没成功写入飞书的记录（feishu_record_id 为空）。
    覆盖两种情况：本次运行刚新增的，以及之前运行飞书写入失败、遗留下来的历史记录 ——
    每次运行都会重新尝试，直到真正同步成功为止，不会因为一次失败就永久丢失。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE feishu_record_id = '' OR feishu_record_id IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_synced(id_record_pairs: list[tuple[int, str]]):
    """把本地 leads.id 对应的记录标记为已同步到飞书（写入 record_id）。"""
    if not id_record_pairs:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE leads SET feishu_record_id = ? WHERE id = ?",
            [(record_id, lead_id) for lead_id, record_id in id_record_pairs],
        )
        conn.commit()


def reconcile_feishu_sync(live_records: list[dict]) -> int:
    """
    以飞书表格当前的实际数据（live_records，来自 feishu_client.fetch_all_records）为准，
    校正本地 leads.feishu_record_id：
      - 本地按 (video_url, promo_platform, promo_link) 能在线上匹配到 —— 写回 record_id
        （覆盖"飞书其实写成功了，但本地标记步骤没跑到"这种半途失败场景，避免重复建行）
      - 本地曾经标记为已同步，但线上已经找不到对应记录（记录被删 / 表被重建）—— 清空，
        交给下一步 batch_create_records 重新写回飞书
    返回被判定为"需要重新同步"的行数。
    """
    live_map = {
        (r.get("video_url", ""), r.get("promo_platform", ""), r.get("promo_link", "")): r.get("feishu_record_id", "")
        for r in live_records
    }
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, video_url, promo_platform, promo_link, feishu_record_id FROM leads"
        ).fetchall()
        stale = 0
        for row in rows:
            key = (row["video_url"], row["promo_platform"], row["promo_link"])
            live_id = live_map.get(key, "")
            if live_id and live_id != row["feishu_record_id"]:
                conn.execute("UPDATE leads SET feishu_record_id = ? WHERE id = ?", (live_id, row["id"]))
            elif not live_id and row["feishu_record_id"]:
                conn.execute("UPDATE leads SET feishu_record_id = '' WHERE id = ?", (row["id"],))
                stale += 1
        conn.commit()
        return stale
