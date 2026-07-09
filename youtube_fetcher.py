import json

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import YOUTUBE_API_KEYS, SEARCH_MAX_RESULTS

# 当前使用的 key 在 YOUTUBE_API_KEYS 中的下标
_key_index = 0

# 所有配置的 key 是否都已耗尽配额。一旦为 True，本次进程内不再重试
# （每次运行都是全新进程，天然重置，不需要手动清零）。
quota_exhausted = False


def _current_key() -> str:
    if not YOUTUBE_API_KEYS:
        raise RuntimeError("未配置 YOUTUBE_API_KEYS / YOUTUBE_API_KEY")
    return YOUTUBE_API_KEYS[_key_index]


def _build_client():
    return build("youtube", "v3", developerKey=_current_key())


def _rotate_key() -> bool:
    """切换到下一个 API Key。成功返回 True；已经是最后一个则返回 False。"""
    global _key_index
    if _key_index + 1 >= len(YOUTUBE_API_KEYS):
        return False
    _key_index += 1
    print(f"  [YouTube] Key #{_key_index} 配额耗尽，切换到 Key #{_key_index + 1}（共 {len(YOUTUBE_API_KEYS)} 个）")
    return True


def _is_quota_error(e: HttpError) -> bool:
    status = getattr(e.resp, "status", None)
    if status == 429:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(e.content.decode("utf-8"))
        reasons = {
            err.get("reason", "")
            for err in payload.get("error", {}).get("errors", [])
        }
    except Exception:
        reasons = set()
    return bool(reasons & {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded"})


def _execute(request_factory):
    """
    执行一次 YouTube API 请求。遇到配额错误时自动切到下一个 key 重试；
    所有 key 都耗尽后设置 quota_exhausted=True 并把异常继续抛出 —— 调用方
    （main.py 现有的 `except HttpError` 分支）据此判断"本次配额彻底用完，停止搜索"。
    """
    global quota_exhausted
    while True:
        try:
            return request_factory().execute()
        except HttpError as e:
            if _is_quota_error(e):
                if _rotate_key():
                    continue
                quota_exhausted = True
            raise


def _search_video_ids(query: str, published_after: str | None, published_before: str | None, max_results: int) -> list[str]:
    video_ids: list[str] = []
    page_token = None
    remaining = max_results

    while remaining > 0:
        params = {
            "q": query,
            "type": "video",
            "part": "id",
            "maxResults": min(remaining, 50),
            "order": "date",
        }
        if published_after:
            params["publishedAfter"] = published_after
        if published_before:
            params["publishedBefore"] = published_before
        if page_token:
            params["pageToken"] = page_token

        resp = _execute(lambda p=params: _build_client().search().list(**p))
        items = resp.get("items", [])
        video_ids.extend(item["id"]["videoId"] for item in items)
        remaining -= len(items)

        page_token = resp.get("nextPageToken")
        if not page_token or not items:
            break

    return video_ids


def _get_video_details(video_ids: list[str]) -> list[dict]:
    if not video_ids:
        return []
    results = []

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = _execute(lambda b=batch: _build_client().videos().list(id=",".join(b), part="snippet"))
        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            results.append({
                "video_id":      item["id"],
                "video_url":     f"https://www.youtube.com/watch?v={item['id']}",
                "channel_title": snippet.get("channelTitle", ""),
                "title":         snippet.get("title", ""),
                "description":   snippet.get("description", ""),
                "published_at":  snippet.get("publishedAt", ""),
            })
    return results


def fetch_videos_for_query(query: str, published_after: str | None = None, published_before: str | None = None) -> list[dict]:
    mode = f"{published_after} ~ {published_before}" if published_after else "冷启动"
    print(f"  [YouTube] {query!r}  [{mode}]")

    video_ids = _search_video_ids(query, published_after, published_before, SEARCH_MAX_RESULTS)
    if not video_ids:
        print(f"  [YouTube] 无新结果")
        return []

    videos = _get_video_details(video_ids)
    print(f"  [YouTube] 获取到 {len(videos)} 条视频")
    return videos
