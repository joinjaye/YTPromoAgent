import requests
from config import (
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_BITABLE_APP_TOKEN,
    FEISHU_BITABLE_TABLE_ID,
    FEISHU_WEBHOOK_URL,
)

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

_token_cache: str = ""
_primary_field_name: str = ""  # cached from setup_table; used as auto-increment ID column


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
        raise RuntimeError(f"飞书 Token 获取失败: {data}")
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
    # (field_name, type)  type 1=Text
    ("Youtuber",  1),
    ("推广平台",  1),
    ("推广链接",  1),
    ("Video 链接", 1),
]


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
    items = resp.json().get("data", {}).get("items", [])
    _primary_field_name = items[0]["field_name"] if items else ""
    existing_names = {f["field_name"] for f in items}

    created = []
    for name, ftype in _FIELD_SCHEMA:
        if name in existing_names:
            continue
        requests.post(
            fields_url,
            headers=headers,
            json={"field_name": name, "type": ftype},
            timeout=10,
        ).raise_for_status()
        created.append(name)

    if created:
        print(f"[飞书] 新建字段: {', '.join(created)}")
    else:
        print("[飞书] 表结构已就绪")


# ── 写入 ──────────────────────────────────────────────────────────────────────

def batch_create_records(records: list[dict]):
    """
    Write promo records to Feishu bitable.
    Each record: {youtuber, promo_platform, promo_link, video_url}
    Primary field gets a sequential auto-increment ID sourced from local SQLite counter.
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
        if _primary_field_name:
            f[_primary_field_name] = str(ids[i])
        fields_list.append(f)

    token = _get_token()
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
            raise RuntimeError(f"批量新增失败: {data}")

    print(f"[飞书] 已写入 {len(records)} 条记录")


# ── 群通知（卡片格式，逐条展示）─────────────────────────────────────────────

def notify_new_records(records: list[dict]):
    if not FEISHU_WEBHOOK_URL:
        print("[飞书] 未配置 FEISHU_WEBHOOK_URL，跳过群通知")
        return

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

    card = {
        "header": {
            "title": {"tag": "plain_text", "content": f"🔍 发现 {len(records)} 条新推广记录"},
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
    print(f"[飞书] 卡片通知已发送（{len(records)} 条）")
