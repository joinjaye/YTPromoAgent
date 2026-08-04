import os
from datetime import datetime, timedelta, timezone
from googleapiclient.errors import HttpError

from config import SEARCH_KEYWORDS, CORE_COMPETITOR_KEYWORDS, CORE_SEARCH_MAX_RESULTS
from youtube_fetcher import fetch_videos_for_query
from link_extractor import (
    extract_promo_links, extract_social_links, extract_emails,
    detect_language, classify_market, match_platform_in_text, extract_hashtags,
)
from db import (
    init_db, save_leads, upsert_channel,
    get_unsynced_leads, mark_synced, reconcile_feishu_sync,
    get_channels, mark_channels_synced, reconcile_channels_feishu_sync,
)
from feishu_client import (
    setup_table, batch_create_records, notify_new_records,
    fetch_all_records, FeishuWriteError,
    setup_channel_table, batch_create_channel_records, batch_update_channel_records,
    fetch_all_channel_records,
)

CST = timezone(timedelta(hours=8))
RUN_MODE = os.getenv("PROMOLEADS_RUN_MODE", "normal").strip().lower()
if RUN_MODE not in {"normal", "channels_only"}:
    raise ValueError("PROMOLEADS_RUN_MODE must be 'normal' or 'channels_only'")

def run():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*50}")
    print(f"[{ts}] 开始执行")
    print(f"[模式] {RUN_MODE}")
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
        # 分层翻页上限：核心竞品（config.CORE_COMPETITOR_KEYWORDS）拉宽到
        # CORE_SEARCH_MAX_RESULTS 页，确保「竞品声量」窗口分析用得上的这几个
        # 平台数据基本完整；其余关键词维持 youtube_fetcher 默认的 1 页上限。
        max_results = CORE_SEARCH_MAX_RESULTS if query.lower() in CORE_COMPETITOR_KEYWORDS else None
        try:
            videos = fetch_videos_for_query(query, published_after, published_before, max_results)
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
            v["search_query"] = query
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
    channel_ids: set[str] = set()
    unmatched_videos = 0
    for video in all_videos:
        promos: list[dict] = []
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

        # ── Step 2.1: 频道维度落库 —— 无论是否命中已知推广平台都保留一条频道记录，
        # 供 BD 人工审查未识别的频道（这是与上面 records/leads 流程的核心区别：
        # 那条流程只保留匹配上的记录，这里按 channel_id 去重/合并所有视频）。
        # 只落本地 channels 表；对应的飞书新 Base/Table 由你后续提供后再接入同步。
        channel_id = video.get("channel_id", "")
        if not channel_id:
            continue
        channel_ids.add(channel_id)
        # 标题兜底：description 里没解析到已知平台链接时，再看 title 是否直接
        # 点名了某个已知交易所 —— 只用于频道维度的 promo_platform 标记，不生成
        # promo_link（没有实际链接），也不影响上面 leads/飞书那条更严格的流程。
        promo_platforms = [p["promo_platform"] for p in promos]
        if not promo_platforms:
            title_platform = match_platform_in_text(video.get("title", ""))
            if title_platform:
                promo_platforms = [title_platform]
        if not promo_platforms:
            unmatched_videos += 1
        try:
            text = f"{video.get('title', '')} {video.get('description', '')}"
            language = detect_language(text)
            upsert_channel({
                "channel_id":        channel_id,
                "account_name":      video.get("channel_title", ""),
                "profile_url":       video.get("profile_url", ""),
                "followers":         video.get("subscriber_count", 0),
                "country":           video.get("country", ""),
                "language":          language,
                "market":            classify_market(video.get("country", ""), language),
                "channel_video_cnt": video.get("channel_video_cnt", 0),
                "channel_view_cnt":  video.get("channel_view_cnt", 0),
                "keyword":           video.get("search_query", ""),
                "promo_platforms":   promo_platforms,
                "promo_links":       [p["promo_link"] for p in promos],
                "video_url":         video.get("video_url", ""),
                "video_title":       video.get("title", ""),
                "description":       video.get("description", ""),
                "published_at":      video.get("published_at", ""),
                "view_count":        video.get("view_count"),
                "like_count":        video.get("like_count"),
                "comment_count":     video.get("comment_count"),
                "duration":          video.get("duration"),
                "first_captured_at": video.get("first_captured_at"),
                "observation_age_hours": video.get("observation_age_hours"),
                "hashtags":          extract_hashtags(text),
                "social":            extract_social_links(text),
                "emails":            extract_emails(video.get("description", "")),
            })
        except Exception as e:
            print(f"[频道] 频道信息处理失败，跳过: {video.get('video_url', '')} — {e}")

    if channel_ids:
        print(f"[频道] 本轮涉及 {len(channel_ids)} 个频道，其中 {unmatched_videos} 条视频未识别到推广平台")

    # ── Step 2.2: 频道级同步到飞书（独立的 Base/Table）──────────────────────
    # 只同步本轮实际碰到的频道（channel_ids），不是全表扫描。跟下面 leads 的
    # append-only 同步不同：已经同步过的频道（有 feishu_record_id）只要这轮
    # 又被碰到，就要重新推一次最新状态（market/推广平台/contact 都可能变化），
    # 不是"同步过就不用管了"。失败不影响 leads 流程，跳过留给下次运行重试。
    if channel_ids:
        # 以飞书线上频道表数据为准，校正本地 feishu_record_id —— 自愈两种情况：
        # ①之前"其实写成功了，但本地标记没跟上"（避免误判成新频道、重复建行）；
        # ②表格被手动清理/记录被删（清空本地缓存，交给下面重新建行）。这一步
        # 失败不影响后续流程，只是校准跳过，直接按本地已有状态同步。
        try:
            live_channels = fetch_all_channel_records()
            stale = reconcile_channels_feishu_sync(live_channels)
            if stale:
                print(f"[Lark] {stale} 个本地频道在线上频道表中已找不到，将重新同步")
        except Exception as e:
            print(f"[Lark] 频道表校准失败，跳过本次校准，直接按本地已有状态同步: {e}")

        try:
            touched_channels = get_channels(list(channel_ids))
        except Exception as e:
            touched_channels = []
            print(f"[Lark] 读取本轮频道数据失败，跳过频道同步: {e}")

        if touched_channels:
            new_channels = [c for c in touched_channels if not c.get("feishu_record_id")]
            existing_channels = [c for c in touched_channels if c.get("feishu_record_id")]

            if new_channels:
                try:
                    record_ids = batch_create_channel_records(new_channels)
                    mark_channels_synced([(c["channel_id"], rid) for c, rid in zip(new_channels, record_ids)])
                except FeishuWriteError as e:
                    ok = len(e.partial_record_ids)
                    if ok:
                        mark_channels_synced([(c["channel_id"], rid) for c, rid in zip(new_channels[:ok], e.partial_record_ids)])
                    print(f"[Lark] 频道批量新增失败，{ok}/{len(new_channels)} 条已写入，其余留待下次运行重试: {e}")
                except Exception as e:
                    print(f"[Lark] 频道批量新增失败，本次跳过，留待下次运行重试: {e}")

            if existing_channels:
                try:
                    batch_update_channel_records(existing_channels)
                except Exception as e:
                    print(f"[Lark] 频道批量更新失败，本次跳过，留待下次运行重试: {e}")

    # Channel-only is a safe end-to-end validation mode: all YouTube fetching,
    # channel enrichment, local channel persistence, reconciliation and Channel
    # Bitable writes above are real. Stop here before touching the leads table,
    # Leads Bitable or group webhook.
    if RUN_MODE == "channels_only":
        print(
            "[完成] channels_only：频道本地数据与 Channel 飞书同步已处理；"
            "已跳过 leads 落库、Leads 飞书主表和群推送"
        )
        return

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
    if RUN_MODE == "normal":
        try:
            setup_table()
        except Exception as e:
            print(f"[Lark] 表结构初始化失败，跳过（不影响本轮抓取 / 本地数据）: {e}")
    else:
        print("[模式] channels_only：跳过 Leads 飞书主表结构初始化")
    try:
        setup_channel_table()
    except Exception as e:
        print(f"[Lark] 频道表结构初始化失败，跳过（不影响本轮抓取 / 本地数据）: {e}")

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
