from datetime import datetime, timezone

import requests
from config import (
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_BITABLE_APP_TOKEN,
    FEISHU_BITABLE_TABLE_ID,
    FEISHU_WEBHOOK_URL,
    DASHBOARD_URL,
)

FEISHU_API_BASE = "https://open.larksuite.com/open-apis"

_token_cache: str = ""
_primary_field_name: str = ""  # cached from setup_table; used as auto-increment ID column


class FeishuWriteError(RuntimeError):
    """批量新增部分失败时，携带已经成功写入的 record_id，避免调用方把它们当成完全没写入。"""

    def __init__(self, message: str, partial_record_ids: list[str] | None = None):
        super().__init__(message)
        self.partial_record_ids = partial_record_ids or []


def _get_token() -> str:
    global _token_cache
    if _token_cache:
        return _token_cache
    resp = requests.post(
        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark Token 获取失败: {data}")
    _token_cache = data["tenant_access_token"]
    return _token_cache


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _table_url(suffix: str = "") -> str:
    return (
        f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_BITABLE_APP_TOKEN}"
        f"/tables/{FEISHU_BITABLE_TABLE_ID}/records{suffix}"
    )


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── 表结构初始化 ──────────────────────────────────────────────────────────────

_FIELD_SCHEMA = [
    # (field_name, type)  type 1=Text, 5=DateTime
    ("Youtuber",  1),
    ("推广平台",  1),
    ("推广链接",  1),
    ("Video 链接", 1),
    ("发布时间",  5),
]


def _to_ms(published_at: str) -> int | None:
    """YouTube publishedAt (RFC3339 UTC) -> ms epoch for a Feishu DateTime field."""
    if not published_at:
        return None
    try:
        dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def setup_table():
    """
    Ensure required columns exist. Safe to call on every run — skips existing fields.
    Caches the primary (first) field name so batch_create_records can write sequential IDs.
    """
    global _primary_field_name
    token = _get_token()
    headers = _headers(token)
    fields_url = (
        f"{FEISHU_API_BASE}/bitable/v1/apps/{FEISHU_BITABLE_APP_TOKEN}"
        f"/tables/{FEISHU_BITABLE_TABLE_ID}/fields"
    )

    resp = requests.get(fields_url, headers=headers, timeout=10)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"[Lark] 获取表字段失败: {body}")
    items = body.get("data", {}).get("items", [])
    _primary_field_name = items[0]["field_name"] if items else ""
    existing_names = {f["field_name"] for f in items}

    created = []
    for name, ftype in _FIELD_SCHEMA:
        if name in existing_names:
            continue
        r = requests.post(
            fields_url,
            headers=headers,
            json={"field_name": name, "type": ftype},
            timeout=10,
        )
        r.raise_for_status()
        r_body = r.json()
        if r_body.get("code") != 0:
            raise RuntimeError(f"[Lark] 新建字段 {name} 失败: {r_body}")
        created.append(name)

    if created:
        print(f"[Lark] 新建字段: {', '.join(created)}")
    else:
        print("[Lark] 表结构已就绪")


# ── 写入 ──────────────────────────────────────────────────────────────────────

def batch_create_records(records: list[dict]) -> list[str]:
    """
    Write promo records to Feishu bitable.
    Each record: {youtuber, promo_platform, promo_link, video_url, published_at?}
    Primary field gets a sequential auto-increment ID sourced from local SQLite counter.

    返回与 records 等长、顺序一致的 record_id 列表。若某个 500 条的分片失败，
    之前已经成功的分片的 record_id 仍然通过 FeishuWriteError.partial_record_ids
    带给调用方，避免下次重试时把已经写入的记录重复建行。
    """
    from db import allocate_record_ids
    ids = list(allocate_record_ids(len(records)))

    fields_list = []
    for i, r in enumerate(records):
        f: dict = {
            "Youtuber":   r["youtuber"],
            "推广平台":   r["promo_platform"],
            "推广链接":   r["promo_link"],
            "Video 链接": r["video_url"],
        }
        pub_ms = _to_ms(r.get("published_at", ""))
        if pub_ms is not None:
            f["发布时间"] = pub_ms
        if _primary_field_name:
            f[_primary_field_name] = str(ids[i])
        fields_list.append(f)

    token = _get_token()
    record_ids: list[str] = []
    for chunk in _chunks(fields_list, 500):
        resp = requests.post(
            _table_url("/batch_create"),
            headers=_headers(token),
            json={"records": [{"fields": f} for f in chunk]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuWriteError(f"[Lark] 批量新增失败（已成功写入 {len(record_ids)} 条）: {data}", record_ids)
        record_ids.extend(item["record_id"] for item in data["data"]["records"])

    print(f"[Lark] 已写入 {len(records)} 条记录")
    return record_ids


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
    """
    拉取飞书多维表格当前的全部记录（只读）。

    用作"以线上表格数据为准"的校准基准 —— 本地 leads.feishu_record_id 可能因为
    之前某次写入失败/超时而没记上，或者表格被手动清理过导致本地缓存过期；
    每次运行前用这份实时数据校正，而不是死信本地那份可能已经不准的记录。
    """
    token = _get_token()
    headers = _headers(token)
    url = _table_url()

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


# ── 群通知（卡片格式，逐条展示）─────────────────────────────────────────────

# Lark webhook silently drops overly large card payloads (HTTP 200, but the
# message never appears in the group) — cap records per card and send
# multiple messages instead of one giant one. Feishu's documented hard limit
# is 30KB per card; measured against real (longest-URL) records, 90 records
# ~= 27.4KB and 100 ~= 30.1KB (over the limit). 60 gives a comfortable margin
# (~19KB even in the worst case) while still batching efficiently.
MAX_RECORDS_PER_CARD = 60


def _content_date_label(records: list[dict]) -> str:
    """视频内容本身的发布日期范围（区别于推送发生的时间）。"""
    dates = sorted({r["published_at"][:10] for r in records if r.get("published_at")})
    if not dates:
        return ""
    return dates[0] if dates[0] == dates[-1] else f"{dates[0]} ~ {dates[-1]}"


def notify_new_records(records: list[dict]):
    if not FEISHU_WEBHOOK_URL:
        print("[Lark] 未配置 FEISHU_WEBHOOK_URL，跳过群通知")
        return
    if not records:
        return

    date_label = _content_date_label(records)
    batches = list(_chunks(records, MAX_RECORDS_PER_CARD))
    for i, batch in enumerate(batches, start=1):
        _send_card(batch, i, len(batches), date_label)


def _send_card(records: list[dict], batch_index: int, batch_total: int, date_label: str = ""):
    elements: list[dict] = []
    for i, r in enumerate(records):
        if i > 0:
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**Youtuber**：{r['youtuber']}\n"
                    f"**推广平台**：{r['promo_platform']}\n"
                    f"**推广链接**：{r['promo_link']}\n"
                    f"**视频链接**：{r['video_url']}"
                ),
            },
        })

    actions = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "查看数据详情"},
        "type": "primary",
        "url": "https://skyrocket.sg.larksuite.com/base/ZALBbXqoaa9NMes3X9nlmogUgob?table=tblIDZup3Y6nAMon&view=vew4AfH9L7",
    }]
    if DASHBOARD_URL:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看可视化看板"},
            "type": "default",
            "url": DASHBOARD_URL,
        })
    elements.append({"tag": "action", "actions": actions})

    title = f"🔍 发现 {len(records)} 条新推广记录"
    if date_label:
        title += f" · 视频日期 {date_label}"
    if batch_total > 1:
        title += f"（第 {batch_index}/{batch_total} 批）"

    card = {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }

    resp = requests.post(
        FEISHU_WEBHOOK_URL,
        json={"msg_type": "interactive", "card": card},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    # Lark webhook responses use either {"code":...} (new) or {"StatusCode":...}
    # (legacy custom bot) — a 200 status does NOT guarantee the message was
    # actually delivered, so check the body explicitly instead of trusting
    # raise_for_status() alone.
    status = body.get("code", body.get("StatusCode", 0))
    if status != 0:
        raise RuntimeError(f"[Lark] 群通知发送失败（第 {batch_index}/{batch_total} 批）: {body}")
    print(f"[Lark] 卡片通知已发送（第 {batch_index}/{batch_total} 批，{len(records)} 条）")
