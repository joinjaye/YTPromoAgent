import json
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

        # Channel-level view: one row per YouTube channel, merged/deduped across
        # every video seen for it (including videos with no matched promo
        # platform — kept here for BD to review manually, unlike `leads`).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id           TEXT    PRIMARY KEY,
                account_name         TEXT    DEFAULT '',
                profile_url          TEXT    DEFAULT '',
                followers            INTEGER DEFAULT 0,
                country              TEXT    DEFAULT '',
                language             TEXT    DEFAULT '',
                market               TEXT    DEFAULT '',
                channel_video_cnt    INTEGER DEFAULT 0,
                channel_view_cnt     INTEGER DEFAULT 0,
                keyword              TEXT    DEFAULT '',
                promo_platform       TEXT    DEFAULT '',
                promo_link           TEXT    DEFAULT '',
                videos               TEXT    DEFAULT '[]',
                total_views          INTEGER DEFAULT 0,
                contact              TEXT    DEFAULT '',
                feishu_record_id     TEXT    DEFAULT '',
                first_crawled_at     INTEGER,
                last_crawled_at      INTEGER
            )
        """)
        _migrate_channels(conn)

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


def _migrate_channels(conn: sqlite3.Connection):
    """
    Migrate an older `channels` table forward. Two independent migrations,
    each a no-op once applied — safe to call on every init_db().
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()}

    # ① single "latest video" columns -> merged multi-video model (videos JSON + total_views)
    if "videos" not in existing and "latest_video_url" in existing:
        conn.execute("ALTER TABLE channels ADD COLUMN videos TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE channels ADD COLUMN total_views INTEGER DEFAULT 0")
        rows = conn.execute(
            "SELECT channel_id, latest_video_url, latest_video_title, latest_published_at, latest_view_count FROM channels"
        ).fetchall()
        for r in rows:
            if not r["latest_video_url"]:
                continue
            videos = [{
                "video_url":     r["latest_video_url"],
                "video_title":   r["latest_video_title"],
                "published_at":  r["latest_published_at"],
                "view_count":    r["latest_view_count"] or 0,
            }]
            conn.execute(
                "UPDATE channels SET videos = ?, total_views = ? WHERE channel_id = ?",
                (json.dumps(videos, ensure_ascii=False), r["latest_view_count"] or 0, r["channel_id"]),
            )
        existing.add("videos")
        existing.add("total_views")
        print("[DB] channels 表迁移：单条最新视频 → 合并视频列表 (videos/total_views)")

    # ② feishu_record_id for channel-level Feishu sync (independent of migration ①)
    if "feishu_record_id" not in existing:
        conn.execute("ALTER TABLE channels ADD COLUMN feishu_record_id TEXT DEFAULT ''")
        print("[DB] channels 表迁移新增列: feishu_record_id")


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


def _union_csv(existing: str, new_items: list[str]) -> str:
    """Append-only dedup union of a comma-separated string with new values."""
    items = [x for x in existing.split(",") if x] if existing else []
    seen = set(items)
    for it in new_items:
        it = (it or "").strip()
        if it and it not in seen:
            items.append(it)
            seen.add(it)
    return ",".join(items)


def _merge_contact(existing: str, social: dict[str, str], emails: list[str]) -> str:
    """
    Fill-if-empty merge of the `contact` field, stored as `label:value|label:value`.
    Social platforms already recorded are left untouched; the `email` bucket is a
    comma-separated union instead, since a channel can list more than one address.
    """
    parts: dict[str, str] = {}
    if existing:
        for token in existing.split("|"):
            if ":" in token:
                k, v = token.split(":", 1)
                parts[k] = v
    for platform, url in social.items():
        parts.setdefault(platform, url)
    if emails:
        existing_emails = {e for e in parts.get("email", "").split(",") if e}
        existing_emails.update(e.strip() for e in emails if e.strip())
        parts["email"] = ",".join(sorted(existing_emails))
    return "|".join(f"{k}:{v}" for k, v in parts.items() if v)


def _merge_videos(existing_json: str, new_video: dict) -> tuple[str, int]:
    """
    Merge one newly-seen video into the channel's full video list, deduped by
    video_url (re-sighting a video refreshes its view_count/title), sorted by
    published_at descending. Returns (videos_json, total_views) where
    total_views is the sum of view_count across every video captured for this
    channel so far — this is a crawl-observed total, distinct from
    `channel_view_cnt` which is the channel's site-wide lifetime total from the
    YouTube API.
    """
    try:
        videos = json.loads(existing_json) if existing_json else []
    except (json.JSONDecodeError, TypeError):
        videos = []
    if new_video.get("video_url"):
        by_url = {v["video_url"]: v for v in videos}
        by_url[new_video["video_url"]] = {
            "video_url":     new_video["video_url"],
            "video_title":   new_video.get("video_title", ""),
            "description":   new_video.get("description", ""),
            "published_at":  new_video.get("published_at", ""),
            "view_count":    new_video.get("view_count", 0),
            "hashtags":      new_video.get("hashtags", []),
        }
        videos = sorted(by_url.values(), key=lambda v: v.get("published_at", ""), reverse=True)
    total_views = sum(v.get("view_count", 0) for v in videos)
    return json.dumps(videos, ensure_ascii=False), total_views


def upsert_channel(row: dict) -> None:
    """
    Merge one video's channel-level signals into `channels` (unique by channel_id).
    Called once per processed video, whether or not a promo platform was matched —
    that's what lets unmatched channels surface for manual BD review instead of
    being silently dropped like in the `leads` pipeline.

    Expected keys on `row`: channel_id, account_name, profile_url, followers,
    country, language, market, channel_video_cnt, channel_view_cnt, keyword,
    promo_platforms (list), promo_links (list), video_url, video_title,
    description, published_at, view_count, hashtags (list), social (dict),
    emails (list).
    """
    now = _now_ms()
    keyword = row.get("keyword", "")
    promo_platforms = row.get("promo_platforms") or []
    promo_links = row.get("promo_links") or []
    social = row.get("social") or {}
    emails = row.get("emails") or []
    new_video = {
        "video_url":    row.get("video_url", ""),
        "video_title":  row.get("video_title", ""),
        "description":  row.get("description", ""),
        "published_at": row.get("published_at", ""),
        "view_count":   row.get("view_count", 0),
        "hashtags":     row.get("hashtags") or [],
    }

    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM channels WHERE channel_id = ?", (row["channel_id"],)
        ).fetchone()

        if existing is None:
            videos_json, total_views = _merge_videos("[]", new_video)
            conn.execute(
                """
                INSERT INTO channels (
                    channel_id, account_name, profile_url, followers, country, language, market,
                    channel_video_cnt, channel_view_cnt, keyword, promo_platform, promo_link,
                    videos, total_views, contact, first_crawled_at, last_crawled_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["channel_id"],
                    row.get("account_name", ""),
                    row.get("profile_url", ""),
                    row.get("followers", 0),
                    row.get("country", ""),
                    row.get("language", ""),
                    row.get("market", ""),
                    row.get("channel_video_cnt", 0),
                    row.get("channel_view_cnt", 0),
                    keyword,
                    _union_csv("", promo_platforms),
                    _union_csv("", promo_links),
                    videos_json,
                    total_views,
                    _merge_contact("", social, emails),
                    now,
                    now,
                ),
            )
        else:
            videos_json, total_views = _merge_videos(existing["videos"], new_video)
            conn.execute(
                """
                UPDATE channels SET
                    account_name = ?, profile_url = ?, followers = ?, country = ?, language = ?, market = ?,
                    channel_video_cnt = ?, channel_view_cnt = ?, keyword = ?, promo_platform = ?, promo_link = ?,
                    videos = ?, total_views = ?, contact = ?, last_crawled_at = ?
                WHERE channel_id = ?
                """,
                (
                    row.get("account_name", ""),
                    row.get("profile_url", ""),
                    row.get("followers", 0),
                    row.get("country", ""),
                    row.get("language", ""),
                    row.get("market", ""),
                    row.get("channel_video_cnt", 0),
                    row.get("channel_view_cnt", 0),
                    _union_csv(existing["keyword"], [keyword] if keyword else []),
                    _union_csv(existing["promo_platform"], promo_platforms),
                    _union_csv(existing["promo_link"], promo_links),
                    videos_json,
                    total_views,
                    _merge_contact(existing["contact"], social, emails),
                    now,
                    row["channel_id"],
                ),
            )
        conn.commit()


def get_channels(channel_ids: list[str]) -> list[dict]:
    """
    Fetch full rows for the given channel_ids — used to sync only the channels
    actually touched in the current run to Feishu, not the whole table every
    time. Unlike `leads` (append-only, synced once), a channel's fields can
    keep changing across runs, so callers need the full current row here to
    decide create-vs-update, not just a "still unsynced" flag.
    """
    if not channel_ids:
        return []
    with _connect() as conn:
        placeholders = ",".join("?" * len(channel_ids))
        rows = conn.execute(
            f"SELECT * FROM channels WHERE channel_id IN ({placeholders})", channel_ids
        ).fetchall()
        return [dict(r) for r in rows]


def mark_channels_synced(id_record_pairs: list[tuple[str, str]]):
    """把本地 channels.channel_id 对应的记录标记为已同步到飞书（写入 record_id）。"""
    if not id_record_pairs:
        return
    with _connect() as conn:
        conn.executemany(
            "UPDATE channels SET feishu_record_id = ? WHERE channel_id = ?",
            [(record_id, channel_id) for channel_id, record_id in id_record_pairs],
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


def reconcile_channels_feishu_sync(live_records: list[dict]) -> int:
    """
    channels 版的 reconcile_feishu_sync，用 channel_id 做匹配基准（而不是 leads
    那边的 video_url/promo_platform/promo_link 组合）——这也是频道表主字段
    要写 channel_id 的原因，保证这里能跟线上数据对上号。校正本地
    channels.feishu_record_id：
      - 本地按 channel_id 能在线上匹配到 —— 写回 record_id（覆盖"其实写成功了
        但本地标记没跟上"，避免把已有频道误判成新的、重复建行）
      - 本地曾标记已同步，但线上已经找不到对应记录 —— 清空，交给下一步
        batch_create_channel_records 重新建行
    返回被判定为"需要重新同步"的行数。
    """
    live_map = {r["channel_id"]: r.get("feishu_record_id", "") for r in live_records if r.get("channel_id")}
    with _connect() as conn:
        rows = conn.execute("SELECT channel_id, feishu_record_id FROM channels").fetchall()
        stale = 0
        for row in rows:
            live_id = live_map.get(row["channel_id"], "")
            if live_id and live_id != row["feishu_record_id"]:
                conn.execute("UPDATE channels SET feishu_record_id = ? WHERE channel_id = ?", (live_id, row["channel_id"]))
            elif not live_id and row["feishu_record_id"]:
                conn.execute("UPDATE channels SET feishu_record_id = '' WHERE channel_id = ?", (row["channel_id"],))
                stale += 1
        conn.commit()
        return stale
