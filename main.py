from datetime import datetime, timezone

from config import SEARCH_KEYWORDS
from youtube_fetcher import fetch_videos_for_query
from link_extractor import extract_promo_links
from db import init_db, get_last_crawl_time, update_crawl_log
from feishu_client import setup_table, batch_create_records, notify_new_records

def run():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*50}")
    print(f"[{ts}] 开始执行")
    print("="*50)

    # ── Step 1: 爬取 YouTube（增量，仅拉取上次爬取之后的新视频）──────────
    seen_video_ids: set[str] = set()
    all_videos: list[dict] = []

    for query in SEARCH_KEYWORDS:
        published_after = get_last_crawl_time(query)
        videos = fetch_videos_for_query(query, published_after)
        update_crawl_log(query)
        for v in videos:
            if v["video_id"] not in seen_video_ids:
                seen_video_ids.add(v["video_id"])
                all_videos.append(v)

    print(f"\n[汇总] 共 {len(all_videos)} 条新视频（已跨关键词去重）")

    if not all_videos:
        print("[完成] 本次无新视频，跳过写入")
        return

    # ── Step 2: 提取 promo 链接 ──────────────────────────────────────────
    records: list[dict] = []
    for video in all_videos:
        promos = extract_promo_links(video.get("description", ""))
        for promo in promos:
            records.append({
                "youtuber":      video["channel_title"],
                "promo_platform": promo["promo_platform"],
                "promo_link":    promo["promo_link"],
                "video_url":     video["video_url"],
            })

    if not records:
        print("[完成] 本次视频中未提取到推广链接")
        return

    print(f"[推广] 提取到 {len(records)} 条推广记录")

    # ── Step 3: 写入飞书多维表格 ─────────────────────────────────────────
    batch_create_records(records)

    # ── Step 4: 群推送 ────────────────────────────────────────────────────
    notify_new_records(records)

    print(f"[完成] 本轮结束，共写入 {len(records)} 条记录")


if __name__ == "__main__":
    init_db()
    setup_table()
    run()
