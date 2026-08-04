"""Shared, deterministic competitor-video metrics for reports and Winsight.

The source is a single first-capture snapshot kept in ``channels.videos``.
Nothing in this module refreshes historic YouTube data or infers missing values.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Iterable

from config import (
    CONTENT_TOPIC_KEYWORDS,
    EARLY_PERFORMANCE_MIN_SAMPLE,
    OFFICIAL_COMPETITOR_CHANNEL_IDS,
    WOW_HIGHLIGHT_ABS_CHANGE,
    WOW_HIGHLIGHT_PERCENT,
)

OBSERVATION_BUCKETS = (
    "0–12小时", "12–24小时", "24–36小时", "36–48小时", "48小时以上", "Unknown",
)
PROMOTION_METHODS = ("brand_led", "description_only", "multi_platform", "official", "unclassified")
CONTENT_TOPICS = tuple(CONTENT_TOPIC_KEYWORDS) + ("Other",)


def _safe_number(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def initial_engagement_rate(view_count, like_count, comment_count):
    views = _safe_number(view_count)
    likes = _safe_number(like_count)
    comments = _safe_number(comment_count)
    if views is None or views <= 0 or likes is None or comments is None:
        return None
    return (likes + comments) / views


def observation_age_bucket(hours) -> str:
    if hours is None or hours == "":
        return "Unknown"
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return "Unknown"
    if value < 0:
        return "Unknown"
    if value < 12:
        return "0–12小时"
    if value < 24:
        return "12–24小时"
    if value < 36:
        return "24–36小时"
    if value < 48:
        return "36–48小时"
    return "48小时以上"


def _brand_pattern(platform: str) -> re.Pattern:
    return re.compile(rf"(?<![\w]){re.escape(platform)}(?![\w])", re.I)


def classify_promotion_methods(
    platform: str,
    title: str,
    promoted_platforms: Iterable[str],
    channel_id: str = "",
) -> tuple[list[str], dict[str, str]]:
    """Return reproducible, multi-label promotion methods and evidence."""
    methods: list[str] = []
    evidence: dict[str, str] = {}
    if title and _brand_pattern(platform).search(title):
        methods.append("brand_led")
        evidence["brand_led"] = "competitor name matched in title"
    else:
        methods.append("description_only")
        evidence["description_only"] = "promotion link matched; competitor absent from title"

    unique_platforms = {p for p in promoted_platforms if p}
    if len(unique_platforms) > 1:
        methods.append("multi_platform")
        evidence["multi_platform"] = f"{len(unique_platforms)} promoted platforms on the same video"

    official_ids = OFFICIAL_COMPETITOR_CHANNEL_IDS.get(platform, set())
    if channel_id and channel_id in official_ids:
        methods.append("official")
        evidence["official"] = "channel_id matched explicit official-channel configuration"

    if not methods:
        methods.append("unclassified")
        evidence["unclassified"] = "insufficient title/link/channel evidence"
    return methods, evidence


def classify_content_topics(title: str, description: str, hashtags: Iterable[str] = ()) -> tuple[list[str], dict[str, list[str]]]:
    """Classify text into configured multi-label topics and expose matched terms."""
    text = " ".join([title or "", description or "", " ".join(hashtags or [])]).lower()
    topics: list[str] = []
    evidence: dict[str, list[str]] = {}
    for topic, keywords in CONTENT_TOPIC_KEYWORDS.items():
        matched = [keyword for keyword in keywords if keyword.lower() in text]
        if matched:
            topics.append(topic)
            evidence[topic] = matched[:5]
    if not topics:
        return ["Other"], {"Other": ["no configured keyword matched"]}
    return topics, evidence


def normalize_title(value: str) -> str:
    value = re.sub(r"#[^\s#]+", "", value or "")
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip().lower()


def dedupe_platform_videos(leads: list[dict]) -> list[dict]:
    """Collapse promotion records by competitor × video_url, preserving count."""
    grouped: dict[tuple[str, str], dict] = {}
    for row in leads:
        platform, url = row.get("promo_platform") or row.get("platform"), row.get("video_url")
        if not platform or not url:
            continue
        key = (platform, url)
        if key not in grouped:
            grouped[key] = dict(row, promo_platform=platform, promotion_record_count=0)
        grouped[key]["promotion_record_count"] += 1
    return list(grouped.values())


def build_video_facts(leads: list[dict], channels: list[dict], core_platforms: Iterable[str]) -> list[dict]:
    core = set(core_platforms)
    canonical = {name.lower(): name for name in core}
    promoted_by_url: dict[str, set[str]] = defaultdict(set)
    for row in dedupe_platform_videos(leads):
        promoted_by_url[row["video_url"]].add(canonical.get(row["promo_platform"].lower(), row["promo_platform"]))

    video_index, channel_index = {}, {}
    for channel in channels:
        for video in channel.get("videos") or []:
            url = video.get("video_url")
            if url:
                video_index[url] = video
                channel_index[url] = channel

    facts = []
    for row in dedupe_platform_videos(leads):
        platform = canonical.get(row["promo_platform"].lower(), row["promo_platform"])
        if platform not in core:
            continue
        url = row["video_url"]
        video = video_index.get(url, {})
        channel = channel_index.get(url, {})
        title = video.get("video_title") or ""
        description = video.get("description") or ""
        methods, method_evidence = classify_promotion_methods(
            platform, title, promoted_by_url[url], channel.get("channel_id", "")
        )
        topics, topic_evidence = classify_content_topics(title, description, video.get("hashtags") or [])
        view_count = _safe_number(video.get("view_count"))
        like_count = _safe_number(video.get("like_count"))
        comment_count = _safe_number(video.get("comment_count"))
        age = video.get("observation_age_hours")
        account = row.get("youtuber") or channel.get("account_name") or ""
        channel_id = channel.get("channel_id") or ""
        facts.append({
            "platform": platform,
            "video_url": url,
            "date": row.get("date") or (row.get("published_at") or "")[:10],
            "published_at": video.get("published_at") or row.get("published_at") or "",
            "account": account,
            "account_key": channel_id or account,
            "channel_id": channel_id,
            "market": channel.get("market") or "",
            "language": channel.get("language") or "",
            "title": title,
            "hashtags": video.get("hashtags") or [],
            "duration": video.get("duration"),
            "first_captured_at": video.get("first_captured_at"),
            "observation_age_hours": age,
            "observation_bucket": observation_age_bucket(age),
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "backfill_view_count": _safe_number(video.get("backfill_view_count")),
            "backfill_like_count": _safe_number(video.get("backfill_like_count")),
            "backfill_comment_count": _safe_number(video.get("backfill_comment_count")),
            "backfill_captured_at": video.get("backfill_captured_at"),
            "backfill_observation_age_hours": video.get("backfill_observation_age_hours"),
            "initial_engagement_rate": initial_engagement_rate(view_count, like_count, comment_count),
            "promotion_record_count": row["promotion_record_count"],
            "promoted_platform_count": len(promoted_by_url[url]),
            "promotion_methods": methods,
            "promotion_method_evidence": method_evidence,
            "content_topics": topics,
            "content_topic_evidence": topic_evidence,
            "detail_available": bool(video),
            "description": description,
        })
    return facts


def _distribution(rows: list[dict], key: str) -> dict[str, int]:
    counts = Counter()
    for row in rows:
        values = row.get(key)
        if isinstance(values, list):
            counts.update(values)
        elif values:
            counts[values] += 1
    return dict(counts.most_common())


def _coverage(rows: list[dict], predicate) -> dict:
    total = len(rows)
    covered = sum(bool(predicate(row)) for row in rows)
    return {"covered": covered, "total": total, "rate": covered / total if total else None}


def mark_early_high_performers(rows: list[dict], min_sample: int = EARLY_PERFORMANCE_MIN_SAMPLE) -> set[tuple[str, str]]:
    """Top 20% within the same observation-age bucket; no small-sample labels."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("observation_bucket") != "Unknown" and row.get("view_count") is not None:
            grouped[row["observation_bucket"]].append(row)
    marked: set[tuple[str, str]] = set()
    for bucket_rows in grouped.values():
        if len(bucket_rows) < min_sample:
            continue
        take = max(1, math.ceil(len(bucket_rows) * 0.2))
        ranked = sorted(bucket_rows, key=lambda row: row["view_count"], reverse=True)
        for row in ranked[:take]:
            marked.add((row["platform"], row["video_url"]))
    return marked


def wow_metrics(current: int, previous: int) -> dict:
    change = current - previous
    percent = None if previous == 0 else change / previous * 100
    significant = (
        percent is not None
        and abs(percent) >= WOW_HIGHLIGHT_PERCENT
        and abs(change) >= WOW_HIGHLIGHT_ABS_CHANGE
    )
    return {"current": current, "previous": previous, "change": change, "percent": percent, "significant": significant}


def _concentration_label(video_count: int, top1_share: float | None) -> str:
    if video_count < 3 or top1_share is None:
        return "样本不足"
    if top1_share >= 0.6:
        return "高度集中"
    if top1_share >= 0.4:
        return "相对集中"
    return "相对分散"


def _signals(metric: dict, previous_metric: dict | None) -> list[str]:
    if metric["videos"] < 3:
        return ["数据不足"]
    signals = []
    if metric["concentration_signal"] in {"高度集中", "相对集中"}:
        signals.append("集中铺量")
    elif metric["accounts"] >= 4 and metric["top1_share"] < 0.4:
        signals.append("长尾扩张")
    if metric["method_shares"].get("description_only", 0) >= 0.6:
        signals.append("Description挂链为主")
    if metric["method_shares"].get("multi_platform", 0) >= 0.5:
        signals.append("Multi-platform为主")
    if metric["method_shares"].get("official", 0) >= 0.5:
        signals.append("官方内容驱动")
    if metric["early_high_performers"] and metric["videos"] <= 5:
        signals.append("高播放少量")
    if previous_metric:
        for topic, label in (("Activity", "活动内容增长"), ("Product", "产品内容增长")):
            if metric["topic_counts"].get(topic, 0) - previous_metric["topic_counts"].get(topic, 0) >= 3:
                signals.append(label)
    return signals[:3] or ["暂无显著结构信号"]


def summarize_window(
    facts: list[dict], start: str, end: str, previous_start: str, previous_end: str,
    core_platforms: Iterable[str],
) -> dict:
    platforms = list(core_platforms)
    current_all = [row for row in facts if start <= row.get("date", "") <= end]
    previous_all = [row for row in facts if previous_start <= row.get("date", "") <= previous_end]
    early = mark_early_high_performers(current_all)
    denominator = len(current_all)

    first_seen: dict[tuple[str, str], str] = {}
    for row in facts:
        account_key = row.get("account_key")
        if account_key and row.get("date"):
            key = (row["platform"], account_key)
            first_seen[key] = min(first_seen.get(key, row["date"]), row["date"])

    def calculate(
        platform: str, rows: list[dict], previous_rows: list[dict] | None = None,
        share_denominator: int | None = None,
    ) -> dict:
        account_counts = Counter(row.get("account_key") or "未知" for row in rows)
        account_names = {row.get("account_key") or "未知": row.get("account") or "未知" for row in rows}
        ordered_accounts = account_counts.most_common()
        top1_count = ordered_accounts[0][1] if ordered_accounts else 0
        top3_count = sum(count for _, count in ordered_accounts[:3])
        count = len(rows)
        top1_share = top1_count / count if count else None
        method_counts = _distribution(rows, "promotion_methods")
        topic_counts = _distribution(rows, "content_topics")
        views = [row["view_count"] for row in rows if row.get("view_count") is not None]
        likes = [row["like_count"] for row in rows if row.get("like_count") is not None]
        comments = [row["comment_count"] for row in rows if row.get("comment_count") is not None]
        engagement_rows = [row for row in rows if row.get("initial_engagement_rate") is not None]
        previous_accounts = {row.get("account_key") for row in (previous_rows or []) if row.get("account_key")}
        current_accounts = {row.get("account_key") for row in rows if row.get("account_key")}
        titles = [normalize_title(row.get("title", "")) for row in rows if normalize_title(row.get("title", ""))]
        metric = {
            "platform": platform,
            "promotion_records": sum(row.get("promotion_record_count", 0) for row in rows),
            "videos": count,
            "promotion_share": count / share_denominator if share_denominator else None,
            "accounts": len(current_accounts),
            "new_observed_accounts": sum(first_seen.get((platform, account)) >= start for account in current_accounts),
            "continuous_accounts": len(current_accounts & previous_accounts),
            "top_account": account_names.get(ordered_accounts[0][0], "") if ordered_accounts else "",
            "top1_share": top1_share,
            "top3_share": top3_count / count if count else None,
            "concentration_signal": _concentration_label(count, top1_share),
            "videos_per_account": count / len(current_accounts) if current_accounts else None,
            "method_counts": method_counts,
            "method_shares": {key: value / count for key, value in method_counts.items()} if count else {},
            "topic_counts": topic_counts,
            "topic_shares": {key: value / count for key, value in topic_counts.items()} if count else {},
            "language_counts": _distribution(rows, "language"),
            "market_counts": _distribution(rows, "market"),
            "first_views_total": sum(views) if views else None,
            "first_views_median": median(views) if views else None,
            "first_likes_total": sum(likes) if likes else None,
            "first_comments_total": sum(comments) if comments else None,
            "initial_engagement_rate": (
                sum((row["like_count"] + row["comment_count"]) for row in engagement_rows)
                / sum(row["view_count"] for row in engagement_rows)
                if engagement_rows and sum(row["view_count"] for row in engagement_rows) > 0 else None
            ),
            "early_high_performers": sum((platform, row["video_url"]) in early for row in rows),
            "title_coverage": len(titles),
            "distinct_titles": len(set(titles)),
            "title_repeat_rate": 1 - len(set(titles)) / len(titles) if titles else None,
            "suspected_templated": len(titles) >= 3 and len(set(titles)) / len(titles) <= 0.5,
            "coverage": {
                "video_details": _coverage(rows, lambda row: row.get("detail_available")),
                "first_views": _coverage(rows, lambda row: row.get("view_count") is not None),
                "likes_comments": _coverage(rows, lambda row: row.get("like_count") is not None and row.get("comment_count") is not None),
                "promotion_method": _coverage(rows, lambda row: row.get("promotion_methods") and row.get("promotion_methods") != ["unclassified"]),
                "content_topic": _coverage(rows, lambda row: row.get("detail_available") and row.get("content_topics")),
                "market": _coverage(rows, lambda row: row.get("market")),
                "language": _coverage(rows, lambda row: row.get("language")),
                "observation_age": _coverage(rows, lambda row: row.get("observation_bucket") != "Unknown"),
                "late_snapshot": _coverage(rows, lambda row: row.get("backfill_captured_at")),
            },
        }
        return metric

    metrics = []
    previous_metrics = {}
    for platform in platforms:
        prev_rows = [row for row in previous_all if row["platform"] == platform]
        previous_metrics[platform] = calculate(platform, prev_rows, share_denominator=len(previous_all))
    for platform in platforms:
        rows = [row for row in current_all if row["platform"] == platform]
        prev_rows = [row for row in previous_all if row["platform"] == platform]
        metric = calculate(platform, rows, prev_rows, denominator)
        metric["wow"] = wow_metrics(metric["videos"], previous_metrics[platform]["videos"])
        previous_metric = previous_metrics[platform]
        metric["previous_promotion_share"] = previous_metric["promotion_share"]
        metric["promotion_share_change"] = (
            metric["promotion_share"] - previous_metric["promotion_share"]
            if metric["promotion_share"] is not None and previous_metric["promotion_share"] is not None else None
        )
        metric["previous_structure"] = {
            key: previous_metric[key] for key in (
                "accounts", "top1_share", "top3_share", "method_shares", "topic_shares",
                "language_counts", "market_counts", "first_views_median",
            )
        }
        metric["signals"] = _signals(metric, previous_metrics[platform])
        metrics.append(metric)

    return {
        "window": {"start": start, "end": end},
        "previous_window": {"start": previous_start, "end": previous_end},
        "total_videos": denominator,
        "total_promotion_records": sum(row.get("promotion_record_count", 0) for row in current_all),
        "early_high_keys": [list(key) for key in sorted(early)],
        "platforms": metrics,
    }


def latest_complete_week(facts: list[dict]) -> tuple[date, date]:
    dates = [date.fromisoformat(row["date"]) for row in facts if row.get("date")]
    anchor = max(dates) if dates else datetime.now(timezone.utc).date()
    end = anchor - timedelta(days=(anchor.weekday() - 3) % 7)  # most recent Thursday
    return end - timedelta(days=6), end


def build_report_payload(facts: list[dict], core_platforms: Iterable[str], max_windows: int = 8) -> dict:
    """Precompute every selectable report window so browser code only renders."""
    platforms = list(core_platforms)
    dated = [date.fromisoformat(row["date"]) for row in facts if row.get("date")]
    anchor = max(dated) if dated else datetime.now(timezone.utc).date()
    payload = {"facts": [{k: v for k, v in row.items() if k != "description"} for row in facts], "windows": {}}
    for size in (7, 14, 30):
        end = anchor - timedelta(days=(anchor.weekday() - 3) % 7) if size == 7 else anchor
        windows = []
        for offset in range(max_windows):
            window_end = end - timedelta(days=offset * size)
            window_start = window_end - timedelta(days=size - 1)
            prev_end = window_start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=size - 1)
            summary = summarize_window(
                facts, window_start.isoformat(), window_end.isoformat(),
                prev_start.isoformat(), prev_end.isoformat(), platforms,
            )
            windows.append(summary)
        payload["windows"][str(size)] = windows
    return payload
