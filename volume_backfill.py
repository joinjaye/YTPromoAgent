#!/usr/bin/env python3
"""Targeted, persistent enrichment for existing core-competitor volume videos.

This intentionally does not call search.list, write Leads, sync Feishu, notify a
group, or build unrelated pages.  Current YouTube statistics are stored as a
clearly separated late backfill snapshot; they never masquerade as first-capture
performance.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import db
from config import CORE_COMPETITOR_DISPLAY_NAMES
from link_extractor import (
    classify_market, detect_language, extract_emails, extract_hashtags,
    extract_social_links,
)
from reporter import load_leads
from youtube_fetcher import _get_channel_details, _get_video_details


def _video_id(url: str) -> str:
    return parse_qs(urlparse(url).query).get("v", [""])[0]


def _first_capture(row: dict) -> tuple[str | None, float | None]:
    created_at, published_at = row.get("created_at"), row.get("published_at")
    if not created_at or not published_at:
        return None, None
    try:
        captured = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return captured.isoformat(timespec="seconds"), round(max(0, (captured - published).total_seconds() / 3600), 2)
    except (TypeError, ValueError, OSError):
        return None, None


def run(start: str, end: str) -> dict:
    core = {name.lower(): name for name in CORE_COMPETITOR_DISPLAY_NAMES.values()}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in load_leads():
        platform = core.get((row.get("promo_platform") or "").lower())
        if platform and start <= row.get("date", "") <= end and row.get("video_url"):
            grouped[row["video_url"]].append(dict(row, canonical_platform=platform))

    ids = [_video_id(url) for url in grouped if _video_id(url)]
    completed = unavailable = failed_batches = 0
    db.init_db()
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        try:
            videos = _get_video_details(batch)
            channel_info = _get_channel_details([video["channel_id"] for video in videos if video.get("channel_id")])
        except Exception as exc:
            failed_batches += 1
            print(f"[Volume Backfill] batch {offset // 50 + 1} failed: {exc}")
            continue
        returned = {video["video_id"] for video in videos}
        unavailable += len(set(batch) - returned)
        for video in videos:
            url = video["video_url"]
            rows = grouped.get(url, [])
            if not rows:
                continue
            earliest = min(rows, key=lambda row: row.get("created_at") or 2**63)
            first_captured_at, observation_age = _first_capture(earliest)
            info = channel_info.get(video.get("channel_id"), {})
            text = f"{video.get('title', '')} {video.get('description', '')}"
            language = detect_language(text)
            platforms = sorted({row["canonical_platform"] for row in rows})
            links = sorted({row.get("promo_link", "") for row in rows if row.get("promo_link")})
            db.upsert_channel({
                "channel_id": video.get("channel_id", ""),
                "account_name": video.get("channel_title", ""),
                "profile_url": info.get("profile_url", ""),
                "followers": info.get("subscriber_count", 0),
                "country": info.get("country", ""),
                "language": language,
                "market": classify_market(info.get("country", ""), language),
                "channel_video_cnt": info.get("channel_video_cnt", 0),
                "channel_view_cnt": info.get("channel_view_cnt", 0),
                "keyword": "volume_backfill",
                "promo_platforms": platforms,
                "promo_links": links,
                "video_url": url,
                "video_title": video.get("title", ""),
                "description": video.get("description", ""),
                "published_at": video.get("published_at", earliest.get("published_at", "")),
                # Recoverable first-capture timing comes from Leads insertion.
                # Current API stats remain explicitly separate late snapshots.
                "view_count": None,
                "like_count": None,
                "comment_count": None,
                "duration": video.get("duration"),
                "first_captured_at": first_captured_at,
                "observation_age_hours": observation_age,
                "backfill_view_count": video.get("view_count"),
                "backfill_like_count": video.get("like_count"),
                "backfill_comment_count": video.get("comment_count"),
                "backfill_captured_at": video.get("first_captured_at"),
                "backfill_observation_age_hours": video.get("observation_age_hours"),
                "hashtags": extract_hashtags(text),
                "social": extract_social_links(text),
                "emails": extract_emails(video.get("description", "")),
            })
            completed += 1
        print(f"[Volume Backfill] {min(offset + 50, len(ids))}/{len(ids)} video IDs processed")
    result = {"target_urls": len(ids), "completed": completed, "unavailable": unavailable, "failed_batches": failed_batches}
    print(f"[Volume Backfill] done: {result}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    run(args.start, args.end)
