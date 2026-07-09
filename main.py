from datetime import datetime, timedelta, timezone
from googleapiclient.errors import HttpError

from config import SEARCH_KEYWORDS
from youtube_fetcher import fetch_videos_for_query
from link_extractor import extract_promo_links
from db import (
    init_db, save_leads,
    get_unsynced_leads, mark_synced, reconcile_feishu_sync,
)
from feishu_client import (
    setup_table, batch_create_records, notify_new_records,
    fetch_all_records, FeishuWriteError,
)

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
    out_of_window = 0

    # main.py 现在只在 GitHub Actions 上跑：这个循环必须保证走到底 —— 任何一个
    # 关键词出问题（配额耗尽、单次请求异常等）都只影响这一个关键词，不能让整个
    # run() 崩溃退出，否则 crawl.yml 的 "Persist crawl log" 步骤不会执行，
    # 前面已经抓到的视频会连本地 / GitHub 都存不进去，直接丢失。
    quota_hit = False
    for query in SEARCH_KEYWORDS:
        if quota_hit:
            break
        try:
            videos = fetch_videos_for_query(query, published_after, published_before)
        except HttpError as e:
            # 配额错误在实测中出现过 429，YouTube 官方文档里也可能是 403 quotaExceeded —
            # 两种都当"配额耗尽"处理（youtube_fetcher 内部已经先按多 key 轮换重试过，
            # 走到这里说明所有 key 都已经用完）。已收集的视频照常往下走完整个流程。
            if e.resp.status in (429, 403):
                print(f"[限流] YouTube API 配额耗尽，停止搜索，已收集 {len(all_videos)} 条视频继续处理")
                quota_hit = True
            else:
                print(f"[YouTube] {query!r} 请求失败，跳过该关键词，继续下一个: {e}")
            continue
        except Exception as e:
            print(f"[YouTube] {query!r} 处理异常，跳过该关键词，继续下一个: {e}")
            continue

        for v in videos:
            if v["video_id"] in seen_video_ids:
                continue
            # YouTube's search publishedAfter/publishedBefore filters by an
            # internal indexing timestamp, not strictly snippet.publishedAt —
            # a small number of videos can slip through outside the window
            # (sometimes by hours). Re-check locally since our whole crawl
            # design depends on the window being exact.
            pub = v.get("published_at", "")
            if not (published_after <= pub < published_before):
                out_of_window += 1
                continue
            seen_video_ids.add(v["video_id"])
            all_videos.append(v)

    if out_of_window:
        print(f"[窗口] 过滤掉 {out_of_window} 条 YouTube 返回但发布时间不在窗口内的视频")

    print(f"\n[汇总] 共 {len(all_videos)} 条新视频（已跨关键词去重）")

    if not all_videos:
        print("[完成] 本次无新视频，跳过写入")
        return

    # ── Step 2: 提取 promo 链接 ──────────────────────────────────────────
    records: list[dict] = []
    for video in all_videos:
        try:
            promos = extract_promo_links(video.get("description", ""))
            for promo in promos:
                records.append({
                    "youtuber":      video["channel_title"],
                    "promo_platform": promo["promo_platform"],
                    "promo_link":    promo["promo_link"],
                    "video_url":     video["video_url"],
                    "published_at":  video.get("published_at", ""),
                })
        except Exception as e:
            print(f"[提取] 单条视频解析失败，跳过: {video.get('video_url', '')} — {e}")

    if not records:
        print("[完成] 本次视频中未提取到推广链接")
        return

    print(f"[推广] 提取到 {len(records)} 条推广记录")

    # ── Step 2.5: 本地持久化（供看板读取，即使飞书写入失败也不丢数据）───
    # 只保留本地真正新增的记录，避免同一天内被重复触发时，对已经写过的
    # 记录重复写入飞书 / 重复群推送。
    new_records = save_leads(records)
    skipped = len(records) - len(new_records)
    print(f"[本地] 新增 {len(new_records)} 条（{skipped} 条已存在，跳过重复处理）")

    # ── Step 2.6: 以飞书线上表格数据为准，校准本地同步状态 ─────────────
    # 自愈两种情况：① 之前"其实写成功了，但本地标记没跟上"——避免重复建行；
    # ② 表格被手动清理/记录被删——清空本地缓存，交给下面重新建行。
    # 这一步失败不影响后续流程，只是校准跳过，直接按本地已有状态同步。
    try:
        live = fetch_all_records()
        stale = reconcile_feishu_sync(live)
        if stale:
            print(f"[Lark] {stale} 条本地记录在线上表格中已找不到，将重新同步")
    except Exception as e:
        print(f"[Lark] 校准失败，跳过本次校准，直接按本地已有状态同步: {e}")

    # 待同步 = 本次新增 + 之前运行遗留下来、还没同步成功的历史记录
    pending = get_unsynced_leads()
    if not pending:
        print("[完成] 没有需要同步到飞书的记录")
        return

    # ── Step 3: 写入飞书多维表格 ─────────────────────────────────────────
    # 失败不应该拖垮已经抓到并落库的数据、也不应该拖垮后续的群推送 / 看板发布——
    # 跳过本次同步，留给下次运行的 Step 2.6 + get_unsynced_leads 自动重试。
    try:
        record_ids = batch_create_records(pending)
        mark_synced([(row["id"], rid) for row, rid in zip(pending, record_ids)])
    except FeishuWriteError as e:
        ok = len(e.partial_record_ids)
        if ok:
            mark_synced([(row["id"], rid) for row, rid in zip(pending[:ok], e.partial_record_ids)])
        print(f"[Lark] 批量新增失败，{ok}/{len(pending)} 条已写入，其余留待下次运行重试: {e}")
    except Exception as e:
        print(f"[Lark] 批量新增失败，本次跳过飞书同步，留待下次运行重试: {e}")

    # ── Step 4: 群推送 ────────────────────────────────────────────────────
    # 只推本次真正新抓到的记录（new_records），不推历史遗留的 pending 补同步部分，
    # 避免飞书连续故障期间每次运行都把旧记录重新通知一遍。
    # 即使上面飞书写入失败，群里依然可以先看到这批发现（不依赖飞书写入成功）。
    try:
        notify_new_records(new_records)
    except Exception as e:
        print(f"[Lark] 群通知失败，跳过（不影响已保存的数据 / 看板）: {e}")

    print(f"[完成] 本轮结束，共发现 {len(new_records)} 条新记录，待同步 {len(pending)} 条")


if __name__ == "__main__":
    init_db()
    try:
        setup_table()
    except Exception as e:
        print(f"[Lark] 表结构初始化失败，跳过（不影响本轮抓取 / 本地数据）: {e}")

    # run() 内部已经把已知的失败模式（配额耗尽、单个关键词异常、飞书读写失败）都
    # 处理成"跳过继续"，这里是最后一道防线：只有在完全没预料到的 bug 时才会走到这，
    # 目的仍然是同一个 —— 保证 crawl.yml 的 "Persist crawl log" 步骤能跑到、
    # 已经抓到 / 已经存到本地的数据不会因为一次异常整体丢失。
    try:
        run()
    except Exception as e:
        import traceback
        print(f"[致命错误] run() 异常退出，已保存的本地/飞书数据不受影响: {e}")
        traceback.print_exc()
