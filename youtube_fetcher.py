from googleapiclient.discovery import build
from config import YOUTUBE_API_KEY, SEARCH_MAX_RESULTS


def _build_client():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def _search_video_ids(query: str, published_after: str | None, published_before: str | None, max_results: int) -> list[str]:
    youtube = _build_client()
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

        resp = youtube.search().list(**params).execute()
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
    youtube = _build_client()
    results = []

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(
            id=",".join(batch),
            part="snippet",
        ).execute()
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
