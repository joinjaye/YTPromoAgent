from datetime import datetime, timedelta, timezone
from googleapiclient.errors import HttpError

from config import SEARCH_KEYWORDS
from youtube_fetcher import fetch_videos_for_query
from link_extractor import extract_promo_links
from db import init_db, save_leads
from feishu_client import setup_table, batch_create_records, notify_new_records

CST = timezone(timedelta(hours=8))

def run():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*50}")
    print(f"[{ts}] 开始执行")
    print("="*50)

    # ── Step 1: 爬取 YouTube（固定窗口：只抓取"前一天"（北京时间）发布的视频）
    # 例如今天(t+1)运行，抓取 t 这一天北京时间 00:00~24:00 发布的视频，
    # 与上次实际爬取时间无关，避免窗口随运行间隔漂移。
    today_start_cst     = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start_cst = today_start_cst - timedelta(days=1)
    published_after  = yesterday_start_cst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    published_before = today_start_cst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[窗口] 抓取北京时间 {yesterday_start_cst.date()} 发布的视频（UTC: {published_after} ~ {published_before}）")

    seen_video_ids: set[str] = set()
    all_videos: list[dict] = []

    quota_hit = False
    for query in SEARCH_KEYWORDS:
        if quota_hit:
            break
        try:
            videos = fetch_videos_for_query(query, published_after, published_before)
            for v in videos:
                if v["video_id"] not in seen_video_ids:
                    seen_video_ids.add(v["video_id"])
                    all_videos.append(v)
        except HttpError as e:
            if e.resp.status == 429:
                print(f"[限流] YouTube API 配额耗尽，停止搜索，已收集 {len(all_videos)} 条视频继续处理")
                quota_hit = True
            else:
                raise

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
                "published_at":  video.get("published_at", ""),
            })

    if not records:
        print("[完成] 本次视频中未提取到推广链接")
        return

    print(f"[推广] 提取到 {len(records)} 条推广记录")

    # ── Step 2.5: 本地持久化（供看板读取，即使飞书写入失败也不丢数据）───
    save_leads(records)

    # ── Step 3: 写入飞书多维表格 ─────────────────────────────────────────
    batch_create_records(records)

    # ── Step 4: 群推送 ────────────────────────────────────────────────────
    notify_new_records(records)

    print(f"[完成] 本轮结束，共写入 {len(records)} 条记录")


if __name__ == "__main__":
    init_db()
    setup_table()
    run()
