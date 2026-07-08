#!/usr/bin/env python3
"""
backfill_feishu.py — One-time backfill of the local `leads` table from the
existing Feishu Bitable (read-only). Run this once so the dashboard has real
historical data instead of starting empty.

Usage:
    python3 backfill_feishu.py

Note: Feishu's Bitable API doesn't expose a creation-timestamp for this table
(it was never added to the schema), so backfilled rows all get created_at =
now. Time-based charts will show a single spike for the backfill; they become
meaningful as new leads accumulate with real timestamps going forward.
"""

import requests

from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BITABLE_APP_TOKEN, FEISHU_BITABLE_TABLE_ID
from db import init_db, save_leads

FEISHU_API_BASE = "https://open.larksuite.com/open-apis"


def _get_token() -> str:
    resp = requests.post(
        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark Token 获取失败: {data}")
    return data["tenant_access_token"]


def _field_text(value) -> str:
    """Normalize a Feishu text-field value: plain string or [{type,text},...] segments."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(seg.get("text", "") for seg in value if isinstance(seg, dict))
    return str(value)


def fetch_all_records() -> list[dict]:
    token = _get_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_BITABLE_APP_TOKEN}/tables/{FEISHU_BITABLE_TABLE_ID}/records"

    records: list[dict] = []
    page_token = ""
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"[Lark] 拉取记录失败: {body}")

        data = body.get("data", {})
        for item in data.get("items", []):
            fields = item.get("fields", {})
            records.append({
                "youtuber":         _field_text(fields.get("Youtuber")),
                "promo_platform":   _field_text(fields.get("推广平台")),
                "promo_link":       _field_text(fields.get("推广链接")),
                "video_url":        _field_text(fields.get("Video 链接")),
                "feishu_record_id": item.get("record_id", ""),
            })

        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")

    return records


def main():
    init_db()
    print("[Backfill] 拉取飞书多维表格记录...")
    records = fetch_all_records()
    print(f"[Backfill] 获取到 {len(records)} 条记录，写入本地 leads 表...")
    save_leads(records)
    print(f"[Backfill] 完成，共处理 {len(records)} 条记录（重复记录已跳过）")


if __name__ == "__main__":
    main()
