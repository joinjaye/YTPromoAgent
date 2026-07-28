#!/usr/bin/env python3
"""
reporter.py — Generate a self-contained HTML dashboard from data/leads.db.

Modeled on youtubeLeads/reporter.py: one static HTML file (Chart.js via CDN,
vanilla JS for search/sort/pagination/grouping), no server, no external data
fetch at runtime. Two tabs:
  - 最新更新 (Latest Update): snapshot of the most recent crawl batch.
  - 全局视图 (Global View): all-time charts/table with a date-range filter.

All aggregation (KPIs, platform/Youtuber breakdowns, video grouping, trend)
happens client-side in JS from one raw lead array, so both tabs share exactly
the same computation logic — Python only loads and serializes the rows.

Output is written to site/index.html and picked up by the "Deploy Dashboard"
GitHub Actions workflow.

Usage:
    python3 reporter.py
"""

import json
import html
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import CORE_COMPETITOR_KEYWORDS
from link_extractor import _PLATFORMS

# Display-cased platform names for the core competitor set (e.g. "weex" -> "Weex"),
# resolved once from the same brand list link_extractor.py uses for matching — so
# editing config.CORE_COMPETITOR_KEYWORDS automatically flows through to the
# 竞品声量 tab without touching this file.
CORE_COMPETITOR_PLATFORMS = [display for brand, _, display in _PLATFORMS if brand in CORE_COMPETITOR_KEYWORDS]

DB_PATH  = Path(__file__).parent / "data" / "leads.db"
OUT_DIR  = Path(__file__).parent / "site"
OUT_PATH = OUT_DIR / "index.html"
CHANNELS_OUT_PATH = OUT_DIR / "channels" / "index.html"
VOLUME_OUT_PATH = OUT_DIR / "volume" / "index.html"
INSIGHT_PATH = Path(__file__).parent / "data" / "weekly_insight.json"

CST = timezone(timedelta(hours=8))

CHART_COLORS = [
    "#3B82F6", "#22D3EE", "#8B5CF6", "#10B981",
    "#F59E0B", "#EF4444", "#EC4899", "#F97316",
    "#A78BFA", "#34D399", "#60A5FA", "#FCD34D",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pub_date_fmt(published_at: str) -> str:
    """YouTube publishedAt (RFC3339 UTC) -> China Standard Time calendar date.
    Backfilled leads have no published_at (Feishu never stored it) -> blank."""
    if not published_at:
        return ""
    try:
        dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d")
    except ValueError:
        return published_at[:10]


def _ms_date_fmt(ms: int | None) -> str:
    """ms-epoch (as stored by db.py's _now_ms) -> China Standard Time display string."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CST).strftime("%Y-%m-%d %H:%M")


# ── Data loading ──────────────────────────────────────────────────────────────

def load_leads() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, youtuber, promo_platform, promo_link, video_url, published_at, created_at
            FROM leads ORDER BY created_at DESC
        """).fetchall()
    except sqlite3.OperationalError:
        # leads table doesn't exist yet (fresh DB, no crawl/backfill run)
        rows = []
    conn.close()

    leads = []
    for r in rows:
        d = dict(r)
        d["date"] = _pub_date_fmt(d["published_at"])
        leads.append(d)
    return leads


def _row_dicts(leads: list[dict]) -> list[dict]:
    """Minimal per-record shape embedded as JSON; all aggregation happens in JS."""
    return [
        {
            "id": l["id"], "youtuber": l["youtuber"], "platform": l["promo_platform"],
            "promo_link": l["promo_link"], "video_url": l["video_url"], "date": l["date"],
        }
        for l in leads
    ]


def load_channels(include_descriptions: bool = False) -> list[dict]:
    """One row per channel, merged/deduped across every video seen for it —
    including channels with no matched promo platform (kept for BD review,
    unlike `leads` which only ever holds matched records)."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT channel_id, account_name, profile_url, followers, country, language, market,
                   channel_video_cnt, channel_view_cnt, keyword, promo_platform, contact,
                   videos, total_views, first_crawled_at, last_crawled_at
            FROM channels ORDER BY last_crawled_at DESC
        """).fetchall()
    except sqlite3.OperationalError:
        # channels table doesn't exist yet (fresh DB, no crawl run since this feature shipped)
        rows = []
    conn.close()

    channels = []
    for r in rows:
        d = dict(r)
        try:
            raw_videos = json.loads(d["videos"]) if d["videos"] else []
        except (json.JSONDecodeError, TypeError):
            raw_videos = []
        # description 只在本地 DB 里保留（供未来做更多文本分析用），不随看板
        # 一起嵌进 site/index.html —— 原文可能几千字，几十上百个频道乘起来会
        # 显著推大页面体积，而看板本身用不到原文，早就有 hashtags 预提取好了。
        d["videos"] = raw_videos if include_descriptions else [
            {k: v for k, v in vid.items() if k != "description"} for vid in raw_videos
        ]
        # 首次抓取：本系统第一次抓到这个频道的时间（系统侧时钟，不是视频发布时间——
        # 页面上原来标「首见」容易被当成频道自己的活跃时间，跟旁边的「活跃时间」
        # 混淆，所以统一用「首次抓取」把这是系统抓取动作说清楚）
        d["first_seen_date"] = _ms_date_fmt(d["first_crawled_at"])[:10] if d["first_crawled_at"] else ""
        d["last_crawled_date"] = _ms_date_fmt(d["last_crawled_at"])[:10] if d["last_crawled_at"] else ""
        # 最近推广：该频道目前抓到的视频里，发布时间最新的一条 —— videos 已经按
        # published_at 倒序存储（见 db.upsert_channel），第一项就是最新的
        d["latest_promo_date"] = _pub_date_fmt(d["videos"][0]["published_at"]) if d["videos"] else ""
        channels.append(d)
    return channels


def _channel_row_dicts(channels: list[dict]) -> list[dict]:
    """Minimal per-channel shape embedded as JSON; table rendering (incl. the
    per-video expand detail) happens in JS."""
    return [
        {
            "channel_id": c["channel_id"], "account_name": c["account_name"], "profile_url": c["profile_url"],
            "keyword": c["keyword"], "followers": c["followers"], "country": c["country"],
            "language": c["language"], "market": c["market"],
            "channel_video_cnt": c["channel_video_cnt"], "channel_view_cnt": c["channel_view_cnt"],
            "promo_platform": c["promo_platform"], "contact": c["contact"],
            "videos": c["videos"], "total_views": c["total_views"],
            "first_seen_date": c["first_seen_date"], "last_crawled_date": c["last_crawled_date"],
            "latest_promo_date": c["latest_promo_date"],
        }
        for c in channels
    ]


# ── HTML generation ───────────────────────────────────────────────────────────

def load_weekly_insight() -> dict:
    if not INSIGHT_PATH.exists():
        return {}
    try:
        payload = json.loads(INSIGHT_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _insight_html(payload: dict) -> str:
    insight = payload.get("insight") if isinstance(payload.get("insight"), dict) else {}
    if not insight:
        return '<div class="ai-empty">本周 Winsight 尚未生成。配置 CURSOR_API_KEY 并执行 Weekly AI Winsight workflow 后将在此显示。</div>'

    esc = lambda value: html.escape(str(value or ""))
    bullets = lambda values: "".join(f"<li>{esc(v)}</li>" for v in values if isinstance(v, str))
    platform_cards = []
    for item in insight.get("platforms", []):
        if not isinstance(item, dict):
            continue
        analysis = item.get("analysis")
        content = f'<p>{esc(analysis)}</p>' if isinstance(analysis, str) else f'<ul>{bullets(item.get("bullets", []))}</ul>'
        platform_cards.append(f'<article class="ai-platform"><h4>{esc(item.get("name"))}</h4>{content}</article>')
    window = payload.get("window", {})
    return f"""
      <div class="ai-insight-head">
        <div><span class="ai-label">CURSOR AGENT · WEEKLY WINSIGHT</span>
          <h3>{esc(insight.get("headline"))}</h3></div>
        <div class="ai-period">数据窗口 {esc(window.get("start"))} — {esc(window.get("end"))}<br>生成于 {esc(payload.get("generated_at"))}</div>
      </div>
      {f'<div class="ai-summary"><ul>{bullets(insight.get("summary", []))}</ul></div>' if insight.get('summary') else ''}
      {f'<div class="ai-platform-grid">{"".join(platform_cards)}</div>' if platform_cards else ''}
      {f'<div class="ai-caveat"><strong>口径提示</strong> {esc(insight.get("caveat"))}</div>' if insight.get('caveat') else ''}
    """


def _remove_between(
    text: str, start: str, end: str, include_end: bool = False, last_end: bool = False,
) -> str:
    """Remove one generated HTML/JS section delimited by stable template markers."""
    start_at = text.find(start)
    if start_at < 0:
        raise ValueError(f"generated page marker not found: {start}")
    end_at = text.rfind(end) if last_end else text.find(end, start_at)
    if end_at < 0:
        raise ValueError(f"generated page marker not found: {end}")
    if include_end:
        end_at += len(end)
    return text[:start_at] + text[end_at:]


def _prune_generated_page(html_text: str, page_mode: str) -> str:
    """Physically exclude unrelated page DOM and executable code from output."""
    if page_mode == "main":
        html_text = _remove_between(
            html_text, "  <!-- ── Tab: 频道视图", "  <!-- ── Tab: 竞品声量"
        )
        html_text = _remove_between(
            html_text, "const channelsTable = createChannelTableController", "// ── Latest tab"
        )
        html_text = _remove_between(
            html_text, "  <!-- ── Tab: 竞品声量", "\n</div>\n\n<script>"
        )
        html_text = _remove_between(
            html_text, "// ── 竞品声量 tab", "renderVolume();", include_end=True, last_end=True
        )
        return html_text

    if page_mode == "channels":
        html_text = _remove_between(
            html_text, "  <!-- ── Tab: 最新更新", "  <!-- ── Tab: 频道视图"
        )
        html_text = _remove_between(
            html_text, "  <!-- ── Tab: 竞品声量", "\n</div>\n\n<script>"
        )
        html_text = _remove_between(
            html_text, "// ── Latest tab", "// ── 竞品声量 tab"
        )
        html_text = _remove_between(
            html_text, "// ── 竞品声量 tab", "renderVolume();", include_end=True, last_end=True
        )
        return html_text

    # volume page: retain only the competitor-volume DOM and its runtime.
    html_text = _remove_between(
        html_text, "  <!-- ── Tab: 最新更新", "  <!-- ── Tab: 频道视图"
    )
    html_text = _remove_between(
        html_text, "  <!-- ── Tab: 频道视图", "  <!-- ── Tab: 竞品声量"
    )
    html_text = _remove_between(
        html_text, "const channelsTable = createChannelTableController", "// ── Latest tab"
    )
    html_text = _remove_between(
        html_text, "// ── Latest tab", "// ── 竞品声量 tab"
    )
    return html_text


def generate_html(
    leads: list[dict], channels: list[dict], run_date: str,
    weekly_insight: dict | None = None, page_mode: str = "main",
) -> str:
    colors_js         = json.dumps(CHART_COLORS)
    all_js            = json.dumps([] if page_mode == "channels" else _row_dicts(leads), ensure_ascii=False)
    channels_js       = json.dumps(
        [] if page_mode == "main" else _channel_row_dicts(channels), ensure_ascii=False
    )
    core_platforms_js = json.dumps(CORE_COMPETITOR_PLATFORMS, ensure_ascii=False)
    insight_html      = _insight_html(weekly_insight or {})
    channels_page = page_mode == "channels"
    volume_page = page_mode == "volume"
    page_title = (
        "PromoLeads · 频道视图" if channels_page
        else "PromoLeads · 竞品声量" if volume_page
        else "PromoLeads 看板"
    )
    if channels_page or volume_page:
        tabbar_html = ""
    else:
        nav_html = """
  <button class="tab-btn active" id="tabBtnLatest" onclick="switchTab('latest', this)">最新更新</button>
  <button class="tab-btn" id="tabBtnGlobal" onclick="switchTab('global', this)">全局视图</button>"""
        tabbar_html = f'<div class="tabbar">\n{nav_html}\n</div>'
    latest_active = "" if channels_page else " active"
    channels_active = " active" if channels_page else ""
    if volume_page:
        latest_active = ""
    volume_active = " active" if volume_page else ""

    html_output = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg:#070C18; --surface:#0C1526; --card:#0F1A2E; --card-h:#132038;
  --border:rgba(34,211,238,0.1); --border-h:rgba(34,211,238,0.35);
  --blue:#3B82F6; --cyan:#22D3EE; --amber:#F59E0B; --green:#10B981; --red:#EF4444;
  --text:#CBD5E1; --text-1:#E2E8F0; --text-2:#475569; --text-dim:#2D3F55;
  --font-mono:'JetBrains Mono',monospace; --font-sans:'Inter',sans-serif;
  --radius:10px; --glow-blue:0 0 18px rgba(59,130,246,0.25);
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  background:var(--bg); color:var(--text); font-family:var(--font-sans);
  background-image:
    linear-gradient(rgba(34,211,238,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(34,211,238,0.03) 1px, transparent 1px);
  background-size:40px 40px;
  min-height:100vh;
}}
.topbar {{
  position:sticky; top:0; z-index:20; height:58px; display:flex; align-items:center;
  justify-content:space-between; padding:0 24px; background:var(--surface);
  border-bottom:1px solid var(--border);
}}
.topbar .brand {{ font-family:var(--font-mono); font-weight:700; color:var(--text-1); letter-spacing:0.5px; }}
.topbar .brand span {{ color:var(--cyan); }}
.status {{ display:flex; align-items:center; gap:8px; font-family:var(--font-mono); font-size:12px; color:var(--text-2); }}
.dot {{ width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.4; }} }}

.tabbar {{
  position:sticky; top:58px; z-index:19; display:flex; gap:4px; padding:0 24px;
  background:var(--surface); border-bottom:1px solid var(--border);
}}
.tab-btn {{
  background:none; border:none; color:var(--text-2); font-family:var(--font-sans);
  font-size:14px; font-weight:600; padding:14px 18px; cursor:pointer;
  border-bottom:2px solid transparent; transition:all 0.15s;
  text-decoration:none; display:inline-flex; align-items:center;
}}
.tab-btn:hover {{ color:var(--text-1); }}
.tab-btn.active {{ color:var(--cyan); border-bottom-color:var(--cyan); }}
.tab-content {{ display:none; }}
.tab-content.active {{ display:block; }}

.container {{ max-width:1440px; margin:0 auto; padding:24px; }}
.kpi-grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:16px; margin-bottom:24px; }}
.kpi-grid.kpi-grid-5 {{ grid-template-columns:repeat(5,1fr); }}
.kpi-card {{
  background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
  padding:18px 20px; transition:all 0.2s;
}}
.kpi-card:hover {{ border-color:var(--border-h); box-shadow:var(--glow-blue); }}
.kpi-label {{ font-size:12px; color:var(--text-2); text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; }}
.kpi-value {{ font-family:var(--font-mono); font-size:26px; font-weight:700; color:var(--text-1); text-shadow:0 0 12px rgba(34,211,238,0.2); }}

.chip-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:24px; }}
.chip {{
  display:flex; align-items:center; gap:6px; background:var(--card); border:1px solid var(--border);
  border-radius:20px; padding:6px 14px; font-size:12px; font-family:var(--font-mono); color:var(--text-1);
}}
.chip b {{ color:var(--cyan); }}

.chart-grid {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px; margin-bottom:16px; }}
.chart-grid > *, .kpi-grid > * {{ min-width:0; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:16px; }}
.card h3 {{ font-size:14px; color:var(--text-1); font-weight:600; margin-bottom:16px; }}
.chart-wrap {{ position:relative; height:280px; }}
.hint {{ font-size:12px; color:var(--text-2); font-family:var(--font-mono); margin-top:10px; }}

.filter-bar {{
  display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:16px;
  background:var(--card); border:1px solid var(--border); border-radius:var(--radius); padding:14px 18px;
}}
.filter-bar label {{ font-size:12px; color:var(--text-2); font-family:var(--font-mono); }}
.filter-bar input[type="date"], .filter-bar input[type="text"] {{
  background:var(--surface); border:1px solid var(--border); border-radius:6px;
  padding:7px 10px; color:var(--text-1); font-family:var(--font-sans); font-size:13px; outline:none;
}}
.filter-bar input[type="number"], .filter-bar select {{
  background:var(--surface); border:1px solid var(--border); border-radius:6px;
  padding:7px 10px; color:var(--text-1); font-family:var(--font-sans); font-size:13px; outline:none;
}}
.filter-bar .filter-search {{ min-width:240px; flex:1 1 240px; }}
.quick-filter-group {{ display:flex; gap:6px; flex-wrap:wrap; }}
.quick-filter-btn {{
  min-width:44px; background:var(--surface); border:1px solid var(--border); border-radius:6px;
  color:#94A3B8; padding:7px 10px; cursor:pointer; font:600 12px var(--font-mono); transition:all .15s;
}}
.quick-filter-btn:hover {{ color:var(--text-1); border-color:var(--border-h); }}
.quick-filter-btn.active {{ color:var(--cyan); border-color:var(--cyan); background:rgba(34,211,238,.12); }}
.quick-filter-btn:focus-visible {{ outline:2px solid var(--cyan); outline-offset:2px; }}
.filter-bar input:focus {{ border-color:var(--border-h); }}
.filter-bar button {{
  background:var(--surface); border:1px solid var(--border); border-radius:6px;
  color:var(--text-1); padding:7px 14px; cursor:pointer; font-size:13px;
}}
.filter-bar button:hover {{ border-color:var(--border-h); color:var(--cyan); }}

.yt-detail-panel {{ background:var(--card); border:1px solid var(--border); border-radius:var(--radius); padding:20px; margin-bottom:16px; display:none; }}
.yt-detail-panel h4 {{ color:var(--text-1); margin-bottom:14px; font-size:14px; }}
.yt-detail-row {{ display:flex; align-items:flex-start; gap:12px; padding:10px 0; border-bottom:1px solid var(--border); flex-wrap:wrap; }}
.yt-detail-row:last-child {{ border-bottom:none; }}
.yt-detail-meta {{ font-family:var(--font-mono); font-size:12px; color:var(--text-2); white-space:nowrap; }}
.yt-links {{ display:flex; flex-direction:column; gap:4px; font-size:12px; flex:1; min-width:200px; }}

.table-card {{ background:var(--card); border:1px solid var(--border); border-radius:var(--radius); padding:20px; }}
.table-card > h3 {{ font-size:14px; color:var(--text-1); font-weight:600; margin-bottom:16px; }}
.table-toolbar {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; gap:12px; flex-wrap:wrap; }}
.table-toolbar input {{
  background:var(--surface); border:1px solid var(--border); border-radius:6px;
  padding:8px 12px; color:var(--text-1); font-family:var(--font-sans); font-size:13px;
  min-width:260px; outline:none;
}}
.table-toolbar input:focus {{ border-color:var(--border-h); }}
.table-meta {{ font-family:var(--font-mono); font-size:12px; color:var(--text-2); }}
.table-wrap {{ overflow-x:auto; max-width:100%; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }}
th {{ color:var(--text-2); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; cursor:pointer; user-select:none; }}
th:hover {{ color:var(--cyan); }}
th.sort-asc::after {{ content:' ▲'; color:var(--cyan); }}
th.sort-desc::after {{ content:' ▼'; color:var(--cyan); }}
td {{ color:var(--text); max-width:320px; overflow:hidden; text-overflow:ellipsis; }}
td a {{ color:var(--blue); text-decoration:none; }}
td a:hover {{ color:var(--cyan); text-decoration:underline; }}
.badge {{
  display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; margin:1px 3px 1px 0;
  font-family:var(--font-mono); background:rgba(59,130,246,0.12); color:var(--blue);
  border:1px solid rgba(59,130,246,0.25);
}}
.video-row {{ cursor:pointer; }}
.video-row:hover {{ background:rgba(34,211,238,0.05); }}
.expand-toggle {{ color:var(--cyan); font-family:var(--font-mono); font-size:12px; white-space:nowrap; }}

/* Compact compound cells (频道视图): two-line stacked value, keeps every number
   the wide table used to show in its own column, just grouped by what they're
   about instead of spread across the row. */
.ch-name {{ display:flex; flex-direction:column; gap:2px; }}
.ch-name a {{ font-weight:600; }}
.ch-meta {{ font-size:11px; color:var(--text-2); font-family:var(--font-mono); display:flex; gap:6px; }}
.ch-stat-primary {{ color:var(--text-1); font-family:var(--font-mono); font-size:13px; }}
.ch-stat-secondary {{ font-size:11px; color:var(--text-2); font-family:var(--font-mono); }}
.market-badge {{
  display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px;
  font-family:var(--font-mono); background:rgba(16,185,129,0.12); color:var(--green);
  border:1px solid rgba(16,185,129,0.25);
}}
.contact-pills {{ display:flex; flex-wrap:wrap; gap:4px; }}
.contact-pill {{
  display:inline-flex; align-items:center; gap:3px; padding:2px 8px; border-radius:10px;
  font-size:11px; font-family:var(--font-mono); background:rgba(34,211,238,0.08);
  color:var(--cyan); border:1px solid var(--border); text-decoration:none; white-space:nowrap;
}}
.contact-pill:hover {{ background:rgba(34,211,238,0.16); border-color:var(--border-h); color:var(--cyan); }}

/* 竞品声量 tab: togglable platform chips, WoW deltas, clickable volume cells */
.platform-chip {{
  display:inline-flex; align-items:center; gap:6px; background:var(--surface); border:1px solid var(--border);
  border-radius:20px; padding:6px 14px; font-size:12px; font-family:var(--font-mono); color:var(--text-2);
  cursor:pointer; user-select:none; transition:all 0.15s;
}}
.platform-chip:hover {{ border-color:var(--border-h); color:var(--text-1); }}
.platform-chip.active {{ background:rgba(34,211,238,0.12); border-color:var(--border-h); color:var(--cyan); }}
.vol-cell {{ cursor:pointer; font-family:var(--font-mono); }}
.vol-cell:hover {{ color:var(--cyan); text-decoration:underline; }}
.vol-cell.selected {{ background:rgba(34,211,238,0.08); border-radius:4px; }}
.wow-up {{ color:var(--green); }}
.wow-down {{ color:var(--red); }}
.wow-flag {{ font-weight:700; }}
.wow-flat {{ color:var(--text-2); }}
.concentration-flag {{
  display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px;
  font-family:var(--font-mono); background:rgba(239,68,68,0.12); color:var(--red);
  border:1px solid rgba(239,68,68,0.25);
}}
.ai-insight {{
  position:relative; overflow:hidden; padding:24px;
  background:linear-gradient(135deg,rgba(59,130,246,.13),rgba(34,211,238,.04) 48%,var(--card));
  border-color:rgba(34,211,238,.28);
}}
.ai-insight::before {{ content:''; position:absolute; inset:0 auto 0 0; width:3px; background:var(--cyan); }}
.ai-insight-head {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:18px; }}
.ai-insight-head h3 {{ font-size:20px; line-height:1.45; margin:7px 0 0; max-width:900px; }}
.ai-label {{ font:600 11px var(--font-mono); letter-spacing:1.2px; color:var(--cyan); }}
.ai-period {{ flex:0 0 auto; text-align:right; font:11px/1.7 var(--font-mono); color:#94A3B8; }}
.ai-summary {{ padding:16px 18px; border:1px solid var(--border); border-radius:8px; background:rgba(7,12,24,.45); }}
.ai-insight ul {{ padding-left:18px; display:grid; gap:8px; }}
.ai-insight li {{ line-height:1.65; color:var(--text); }}
.ai-platform-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:14px; }}
.ai-platform {{ padding:16px 18px; background:rgba(12,21,38,.72); border:1px solid var(--border); border-radius:8px; }}
.ai-platform h4,.ai-foot-grid h4 {{ color:var(--text-1); font-size:13px; margin-bottom:10px; }}
.ai-platform p {{ color:var(--text); line-height:1.75; }}
.ai-caveat {{ margin-top:14px; padding:12px 16px; color:#94A3B8; font-size:12px; border-top:1px solid var(--border); }}
.ai-foot-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:14px; }}
.ai-foot-grid section {{ padding:16px 18px; border-top:1px solid var(--border); }}
.ai-empty {{ padding:28px; text-align:center; color:#94A3B8; font:12px/1.7 var(--font-mono); }}
.detail-row td {{ background:var(--surface); padding:14px 20px; white-space:normal; }}
.detail-list {{ display:flex; flex-direction:column; gap:8px; }}
.detail-item {{ display:flex; align-items:center; gap:10px; font-size:13px; flex-wrap:wrap; }}
.pagination {{ display:flex; justify-content:center; align-items:center; gap:12px; margin-top:16px; font-family:var(--font-mono); font-size:13px; }}
.pagination button {{
  background:var(--surface); border:1px solid var(--border); border-radius:6px;
  color:var(--text-1); padding:6px 14px; cursor:pointer; font-family:var(--font-mono);
}}
.pagination button:hover:not(:disabled) {{ border-color:var(--border-h); color:var(--cyan); }}
.pagination button:disabled {{ opacity:0.35; cursor:not-allowed; }}
.empty-state {{ text-align:center; padding:40px; color:var(--text-2); font-family:var(--font-mono); }}
@media (max-width:1100px) {{
  .kpi-grid {{ grid-template-columns:repeat(2,1fr); }}
  .chart-grid {{ grid-template-columns:1fr; }}
  .ai-platform-grid,.ai-foot-grid {{ grid-template-columns:1fr; }}
  .ai-insight-head {{ flex-direction:column; }}
  .ai-period {{ text-align:left; }}
}}
@media (max-width:600px) {{
  .container {{ padding:12px; }}
  .kpi-grid, .kpi-grid.kpi-grid-5 {{ grid-template-columns:1fr; }}
  .filter-bar {{ align-items:stretch; }}
  .filter-bar label {{ margin-top:4px; }}
}}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">PROMO<span>LEADS</span> · 看板</div>
  <div class="status"><span class="dot"></span>更新于 {run_date}</div>
</div>

{tabbar_html}

<div class="container">

  <!-- ── Tab: 最新更新 ────────────────────────────────────────────────── -->
  <div id="tab-latest" class="tab-content{latest_active}">
    <div class="kpi-grid kpi-grid-5">
      <div class="kpi-card"><div class="kpi-label">最近发布日期</div><div class="kpi-value" style="font-size:20px;" id="kpi-latest-date">—</div></div>
      <div class="kpi-card"><div class="kpi-label">本轮新增视频数</div><div class="kpi-value" id="kpi-latest-videos">0</div></div>
      <div class="kpi-card"><div class="kpi-label">本轮新增推广记录数</div><div class="kpi-value" id="kpi-latest-records">0</div></div>
      <div class="kpi-card"><div class="kpi-label">涉及 Youtuber 数</div><div class="kpi-value" id="kpi-latest-youtubers">0</div></div>
      <div class="kpi-card"><div class="kpi-label">涉及推广平台数</div><div class="kpi-value" id="kpi-latest-platforms">0</div></div>
    </div>

    <div class="chip-row" id="latestChips"></div>

    <div class="table-card">
      <h3>本轮推广详情</h3>
      <div class="table-toolbar">
        <input id="search-latest" type="text" placeholder="搜索 Youtuber / 平台 / 视频 / 链接...">
        <div class="table-meta" id="meta-latest"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr id="thead-latest">
            <th data-key="date">发布日期</th>
            <th data-key="youtuber">Youtuber</th>
            <th data-key="video_url">视频链接</th>
            <th data-key="platforms">推广平台</th>
            <th></th>
          </tr></thead>
          <tbody id="tbody-latest"></tbody>
        </table>
        <div id="empty-latest" class="empty-state" style="display:none;">暂无匹配的推广记录</div>
      </div>
      <div class="pagination">
        <button id="prev-latest">← 上一页</button>
        <span id="pageInfo-latest"></span>
        <button id="next-latest">下一页 →</button>
      </div>
    </div>
  </div>

  <!-- ── Tab: 全局视图 ────────────────────────────────────────────────── -->
  <div id="tab-global" class="tab-content">
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-label">总推广视频</div><div class="kpi-value" id="kpi-global-total-videos">0</div></div>
      <div class="kpi-card"><div class="kpi-label">推广记录总数</div><div class="kpi-value" id="kpi-global-total-records">0</div></div>
      <div class="kpi-card"><div class="kpi-label">Youtuber 数</div><div class="kpi-value" id="kpi-global-youtubers">0</div></div>
      <div class="kpi-card"><div class="kpi-label">推广平台数</div><div class="kpi-value" id="kpi-global-platforms">0</div></div>
      <div class="kpi-card"><div class="kpi-label">近 7 天发布视频（全部数据）</div><div class="kpi-value" id="kpi-global-new-7d">0</div></div>
      <div class="kpi-card"><div class="kpi-label">近 30 天发布视频（全部数据）</div><div class="kpi-value" id="kpi-global-new-30d">0</div></div>
    </div>

    <div class="filter-bar">
      <label>按视频发布日期筛选：从</label>
      <input type="date" id="dateFrom">
      <label>到</label>
      <input type="date" id="dateTo">
      <button id="resetDate">重置</button>
      <span class="table-meta" id="filterMeta"></span>
    </div>

    <div class="card">
      <h3>每日发布推广视频趋势</h3>
      <div class="chart-wrap"><canvas id="cTrend"></canvas></div>
    </div>

    <div class="chart-grid">
      <div class="card">
        <h3>推广平台分布 · 按推广视频数</h3>
        <div class="chart-wrap"><canvas id="cPlatformByVideo"></canvas></div>
      </div>
      <div class="card">
        <h3>推广平台分布 · 按 Youtuber 数</h3>
        <div class="chart-wrap"><canvas id="cPlatformByYt"></canvas></div>
      </div>
    </div>

    <div class="card">
      <h3>Top 15 Youtuber（按推广视频数排序）</h3>
      <div class="chart-wrap"><canvas id="cTopYt"></canvas></div>
      <div class="hint">💡 点击柱状图查看该 Youtuber 的推广平台明细与链接</div>
    </div>

    <div class="yt-detail-panel" id="ytDetailPanel"></div>

    <div class="table-card">
      <h3>全部推广详情</h3>
      <div class="table-toolbar">
        <input id="search-global" type="text" placeholder="搜索 Youtuber / 平台 / 视频 / 链接...">
        <div class="table-meta" id="meta-global"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr id="thead-global">
            <th data-key="date">发布日期</th>
            <th data-key="youtuber">Youtuber</th>
            <th data-key="video_url">视频链接</th>
            <th data-key="platforms">推广平台</th>
            <th></th>
          </tr></thead>
          <tbody id="tbody-global"></tbody>
        </table>
        <div id="empty-global" class="empty-state" style="display:none;">暂无匹配的推广记录</div>
      </div>
      <div class="pagination">
        <button id="prev-global">← 上一页</button>
        <span id="pageInfo-global"></span>
        <button id="next-global">下一页 →</button>
      </div>
    </div>
  </div>

  <!-- ── Tab: 频道视图 ────────────────────────────────────────────────── -->
  <div id="tab-channels" class="tab-content{channels_active}">
    <div class="filter-bar" aria-label="频道视图全局筛选">
      <label for="chMarketFilter">市场</label>
      <select id="chMarketFilter"><option value="">全部市场</option></select>
      <div class="quick-filter-group" id="chMarketQuickFilters" aria-label="主要市场快捷筛选">
        <button type="button" class="quick-filter-btn" data-market="KR" aria-pressed="false">KR</button>
        <button type="button" class="quick-filter-btn" data-market="FR" aria-pressed="false">FR</button>
        <button type="button" class="quick-filter-btn" data-market="ID" aria-pressed="false">ID</button>
        <button type="button" class="quick-filter-btn" data-market="VN" aria-pressed="false">VN</button>
      </div>
      <label for="chFollowerFilter">粉丝量级</label>
      <select id="chFollowerFilter">
        <option value="">全部量级</option><option value="lt1k">&lt;1K</option><option value="1k10k">1K–10K</option>
        <option value="10k100k">10K–100K</option><option value="100k1m">100K–1M</option><option value="1m">1M+</option>
      </select>
      <label for="chMinViews">最低累计观看</label>
      <input id="chMinViews" type="number" min="0" step="100" placeholder="0">
      <label for="chDateField">日期口径</label>
      <select id="chDateField">
        <option value="latest_promo_date">内容活跃日期</option>
        <option value="first_seen_date">首次抓取日期</option>
        <option value="last_crawled_date">最近抓取日期</option>
      </select>
      <label for="chDateFrom">从</label><input id="chDateFrom" type="date">
      <label for="chDateTo">到</label><input id="chDateTo" type="date">
      <input id="search-channels" class="filter-search" type="text" aria-label="搜索频道" placeholder="搜索频道名 / 平台 / 国家 / 关键词...">
      <button id="chResetFilters">重置</button>
      <span class="table-meta" id="chFilterMeta"></span>
    </div>

    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-label">频道数</div><div class="kpi-value" id="kpi-ch-total">0</div></div>
      <div class="kpi-card"><div class="kpi-label">覆盖市场数</div><div class="kpi-value" id="kpi-ch-markets">0</div></div>
      <div class="kpi-card"><div class="kpi-label">粉丝总量</div><div class="kpi-value" id="kpi-ch-followers">0</div></div>
      <div class="kpi-card"><div class="kpi-label">抓取视频累计观看</div><div class="kpi-value" id="kpi-ch-views">0</div></div>
      <div class="kpi-card"><div class="kpi-label">期间新增频道</div><div class="kpi-value" id="kpi-ch-new">0</div></div>
      <div class="kpi-card"><div class="kpi-label">期间内容活跃</div><div class="kpi-value" id="kpi-ch-active">0</div></div>
    </div>

    <div class="chart-grid">
      <div class="card">
        <h3>市场组合 · 频道数 × 累计观看 × 粉丝规模</h3>
        <div class="chart-wrap"><canvas id="cChMarketPortfolio"></canvas></div>
        <div class="hint">横轴为频道数，纵轴为抓取视频累计观看；气泡越大代表该市场粉丝总量越高</div>
      </div>
      <div class="card">
        <h3>频道质量 · 粉丝数 × 累计观看</h3>
        <div class="chart-wrap"><canvas id="cChQuality"></canvas></div>
        <div class="hint">每个点代表一个频道；点击数据点可定位并展开下方频道明细</div>
      </div>
    </div>

    <div class="table-card">
      <h3>频道明细（按频道去重合并，含未识别推广平台的频道）</h3>
      <div class="hint">点击一行展开，查看该频道逐条抓到的视频（发布时间 / 观看数）</div>
      <div class="table-toolbar">
        <div class="table-meta" id="meta-channels"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr id="thead-channels">
            <th data-key="account_name">频道</th>
            <th data-key="market">市场</th>
            <th data-key="followers">粉丝数</th>
            <th data-key="channel_view_cnt">频道规模</th>
            <th data-key="promo_platform">推广平台</th>
            <th>联系方式</th>
            <th data-key="total_views">本次抓取</th>
            <th data-key="latest_promo_date">活跃时间</th>
            <th></th>
          </tr></thead>
          <tbody id="tbody-channels"></tbody>
        </table>
        <div id="empty-channels" class="empty-state" style="display:none;">暂无频道数据</div>
      </div>
      <div class="pagination">
        <button id="prev-channels">← 上一页</button>
        <span id="pageInfo-channels"></span>
        <button id="next-channels">下一页 →</button>
      </div>
    </div>
  </div>

  <!-- ── Tab: 竞品声量 ────────────────────────────────────────────────── -->
  <div id="tab-volume" class="tab-content{volume_active}">
    <section class="card ai-insight" aria-label="每周 AI Winsight">
      {insight_html}
    </section>

    <div class="filter-bar">
      <label>窗口长度</label>
      <select id="volWindowSize">
        <option value="7">7 天</option>
        <option value="14">14 天</option>
        <option value="30">30 天</option>
      </select>
      <label>对比窗口数</label>
      <select id="volWindowCount">
        <option value="4">4</option>
        <option value="6">6</option>
        <option value="8">8</option>
      </select>
      <span class="table-meta" id="volMeta"></span>
    </div>

    <div class="card">
      <h3>对比平台</h3>
      <div class="chip-row" id="volPlatformChips"></div>
      <div class="hint">💡 只列出 config.CORE_COMPETITOR_KEYWORDS 里的核心竞品（这几个词享受宽松翻页上限，数据基本完整，其余关键词仍是 1 页/天不适合做时间窗口对比）；默认全选，点击徽标可增减对比对象。声量 = 该窗口内识别到推广链接的视频数，不含评论区提及，口径与旧版周报的社媒监听数据不同</div>
    </div>

    <div class="table-card">
      <h3>周度声量对照（按视频发布日期分窗，最新窗口在最上面）</h3>
      <div class="table-wrap">
        <table id="volTable"><thead><tr id="volTableHead"></tr></thead><tbody id="volTableBody"></tbody></table>
      </div>
      <div class="hint">💡 |WoW| ≥ 30% 会加粗高亮；点击任意声量数字可在下方展开该窗口/平台的视频明细</div>
    </div>

    <div class="chart-grid">
      <div class="card">
        <h3>视频数量趋势</h3>
        <div class="chart-wrap"><canvas id="cVolTrend"></canvas></div>
      </div>
      <div class="card">
        <h3>视频数量 × 累计播放量（最新窗口）</h3>
        <div class="chart-wrap"><canvas id="cVolPerformance"></canvas></div>
        <div class="hint" id="volViewsCoverage"></div>
      </div>
    </div>

    <div class="chart-grid">
      <div class="card">
        <h3>语言构成（当前窗口范围，按平台）</h3>
        <div class="chart-wrap"><canvas id="cVolLang"></canvas></div>
        <div class="hint" id="volLangCoverage"></div>
      </div>
      <div class="card">
        <h3>Top 15 Hashtag（当前窗口范围，视频标题）</h3>
        <div class="chart-wrap"><canvas id="cVolHashtag"></canvas></div>
        <div class="hint" id="volHashtagCoverage"></div>
      </div>
    </div>

    <div class="table-card">
      <h3>账号集中度（最新窗口）</h3>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>平台</th><th>视频数</th><th>已覆盖视频播放量</th><th>涉及账号数</th><th>Top1 账号</th><th>Top1 占比</th><th>标题重复度</th>
          </tr></thead>
          <tbody id="volConcentrationBody"></tbody>
        </table>
      </div>
      <div class="hint">💡 Top1 占比越高，说明声量越集中于单一账号，"KOL 矩阵"可能只是个例账号在批量挂标签，持续性存疑；标题重复度低（去重数远小于视频数）是同一批模板化标题刷量的另一个信号——建议点开下方明细核实内容形式</div>
    </div>

    <div id="volDrillPanel" class="yt-detail-panel"></div>
  </div>

</div>

<script>
const COLORS = {colors_js};
const ALL_LEADS = {all_js};
const ALL_CHANNELS = {channels_js};
const CORE_COMPETITOR_PLATFORMS = {core_platforms_js};
const PAGE_MODE = {json.dumps(page_mode)};

function switchTab(id, btn) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}}

if (PAGE_MODE === 'main') {{
  const requestedTab = new URLSearchParams(window.location.search).get('tab');
  const tabButtons = {{ latest: 'tabBtnLatest', global: 'tabBtnGlobal' }};
  if (requestedTab && tabButtons[requestedTab]) switchTab(requestedTab, document.getElementById(tabButtons[requestedTab]));
}}

function truncate(s, n) {{
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
}}
function linkCell(url) {{
  if (!url) return '';
  return `<a href="${{url}}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${{truncate(url, 40)}}</a>`;
}}

// ── Group raw leads into one row per (date, youtuber, video_url) ─────────
function groupByVideo(leads) {{
  const map = new Map();
  leads.forEach(l => {{
    const key = `${{l.date}}||${{l.youtuber}}||${{l.video_url}}`;
    if (!map.has(key)) {{
      map.set(key, {{ date: l.date, youtuber: l.youtuber, video_url: l.video_url, details: [], platformSet: new Set() }});
    }}
    const g = map.get(key);
    g.details.push({{ platform: l.platform, promo_link: l.promo_link }});
    g.platformSet.add(l.platform || '未知');
  }});
  return Array.from(map.values()).map(g => ({{
    date: g.date, youtuber: g.youtuber, video_url: g.video_url,
    platforms: Array.from(g.platformSet),
    details: g.details,
  }}));
}}

// ── Derive every view (KPIs, breakdowns, table rows) from a raw lead array ─
function deriveViews(leads) {{
  const videoRows = groupByVideo(leads);
  const totalVideos = new Set(leads.map(l => l.video_url)).size;
  const totalRecords = leads.length;
  const youtubers = new Set(leads.filter(l => l.youtuber).map(l => l.youtuber)).size;
  const platforms = new Set(leads.filter(l => l.platform).map(l => l.platform)).size;

  const platVideoMap = new Map();
  const platYtMap = new Map();
  leads.forEach(l => {{
    const p = l.platform || '未知';
    if (!platVideoMap.has(p)) platVideoMap.set(p, new Set());
    platVideoMap.get(p).add(l.video_url);
    if (!platYtMap.has(p)) platYtMap.set(p, new Set());
    platYtMap.get(p).add(l.youtuber);
  }});
  const platformByVideo = Array.from(platVideoMap.entries()).map(([p, s]) => [p, s.size]).sort((a, b) => b[1] - a[1]).slice(0, 12);
  const platformByYoutuber = Array.from(platYtMap.entries()).map(([p, s]) => [p, s.size]).sort((a, b) => b[1] - a[1]).slice(0, 12);

  const ytVideoMap = new Map();
  const ytPlatformMap = new Map();
  leads.forEach(l => {{
    const y = l.youtuber || '未知';
    if (!ytVideoMap.has(y)) ytVideoMap.set(y, new Set());
    ytVideoMap.get(y).add(l.video_url);

    if (!ytPlatformMap.has(y)) ytPlatformMap.set(y, new Map());
    const pMap = ytPlatformMap.get(y);
    const p = l.platform || '未知';
    if (!pMap.has(p)) pMap.set(p, {{ videos: new Set(), links: new Set() }});
    pMap.get(p).videos.add(l.video_url);
    pMap.get(p).links.add(l.promo_link);
  }});
  const topYoutubers = Array.from(ytVideoMap.entries())
    .map(([y, s]) => ({{ youtuber: y, videoCount: s.size }}))
    .sort((a, b) => b.videoCount - a.videoCount)
    .slice(0, 15);

  const dayVideoMap = new Map();
  leads.forEach(l => {{
    if (!l.date) return;
    if (!dayVideoMap.has(l.date)) dayVideoMap.set(l.date, new Set());
    dayVideoMap.get(l.date).add(l.video_url);
  }});
  const trend = Array.from(dayVideoMap.entries()).map(([d, s]) => [d, s.size]).sort((a, b) => a[0].localeCompare(b[0]));

  return {{ videoRows, totalVideos, totalRecords, youtubers, platforms, platformByVideo, platformByYoutuber, topYoutubers, ytPlatformMap, trend }};
}}

// ── Generic grouped-row table: search + sort + pagination + row expand ───
function createVideoTableController(cfg) {{
  let filtered = cfg.data.slice();
  let sortKey = cfg.sortKeyDefault || 'date';
  let sortDir = cfg.sortDirDefault || 'desc';
  let page = 1;
  const expanded = new Set();
  const els = cfg.elIds;

  function rowKey(r) {{ return `${{r.date}}||${{r.youtuber}}||${{r.video_url}}`; }}

  function applyFilter() {{
    const q = els.search ? document.getElementById(els.search).value.trim().toLowerCase() : '';
    filtered = !q ? cfg.data.slice() : cfg.data.filter(r =>
      (r.youtuber || '').toLowerCase().includes(q) ||
      (r.video_url || '').toLowerCase().includes(q) ||
      r.platforms.some(p => p.toLowerCase().includes(q)) ||
      r.details.some(d => (d.promo_link || '').toLowerCase().includes(q))
    );
    applySort();
    page = 1;
    render();
  }}

  function applySort() {{
    filtered.sort((a, b) => {{
      let va, vb;
      if (sortKey === 'platforms') {{ va = a.platforms.length; vb = b.platforms.length; }}
      else {{ va = (a[sortKey] || '').toString(); vb = (b[sortKey] || '').toString(); }}
      const cmp = typeof va === 'number' ? va - vb : va.localeCompare(vb, undefined, {{ numeric: true }});
      return sortDir === 'asc' ? cmp : -cmp;
    }});
  }}

  function render() {{
    const tbody = document.getElementById(els.tbody);
    const totalPages = Math.max(1, Math.ceil(filtered.length / cfg.pageSize));
    page = Math.min(page, totalPages);
    const start = (page - 1) * cfg.pageSize;
    const rows = filtered.slice(start, start + cfg.pageSize);

    if (rows.length === 0) {{
      tbody.innerHTML = '';
      if (els.empty) document.getElementById(els.empty).style.display = 'block';
    }} else {{
      if (els.empty) document.getElementById(els.empty).style.display = 'none';
      tbody.innerHTML = rows.map(r => {{
        const key = rowKey(r);
        const isOpen = expanded.has(key);
        const badges = r.platforms.map(p => `<span class="badge">${{p}}</span>`).join('');
        const detailRow = isOpen ? `
          <tr class="detail-row">
            <td colspan="5">
              <div class="detail-list">
                ${{r.details.map(d => `<div class="detail-item"><span class="badge">${{d.platform || '未知'}}</span>${{linkCell(d.promo_link)}}</div>`).join('')}}
              </div>
            </td>
          </tr>` : '';
        return `
          <tr class="video-row" data-key="${{key}}">
            <td>${{r.date || ''}}</td>
            <td>${{r.youtuber || ''}}</td>
            <td>${{linkCell(r.video_url)}}</td>
            <td>${{badges}}</td>
            <td class="expand-toggle">${{isOpen ? '▾ 收起' : '▸ 展开'}}</td>
          </tr>${{detailRow}}`;
      }}).join('');

      tbody.querySelectorAll('tr.video-row').forEach(tr => {{
        tr.addEventListener('click', () => {{
          const key = tr.dataset.key;
          if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
          render();
        }});
      }});
    }}

    if (els.meta) document.getElementById(els.meta).textContent = `共 ${{filtered.length}} 个视频`;
    if (els.pageInfo) document.getElementById(els.pageInfo).textContent = `第 ${{page}} / ${{totalPages}} 页`;
    if (els.prevBtn) document.getElementById(els.prevBtn).disabled = page <= 1;
    if (els.nextBtn) document.getElementById(els.nextBtn).disabled = page >= totalPages;

    document.querySelectorAll(`#${{els.theadRow}} th[data-key]`).forEach(th => {{
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.key === sortKey) th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    }});
  }}

  if (els.search) document.getElementById(els.search).addEventListener('input', applyFilter);
  if (els.prevBtn) document.getElementById(els.prevBtn).addEventListener('click', () => {{ page--; render(); }});
  if (els.nextBtn) document.getElementById(els.nextBtn).addEventListener('click', () => {{ page++; render(); }});
  document.querySelectorAll(`#${{els.theadRow}} th[data-key]`).forEach(th => {{
    th.addEventListener('click', () => {{
      const key = th.dataset.key;
      if (sortKey === key) {{ sortDir = sortDir === 'asc' ? 'desc' : 'asc'; }}
      else {{ sortKey = key; sortDir = 'desc'; }}
      applySort();
      render();
    }});
  }});

  applySort();
  render();

  return {{ setData(newData) {{ cfg.data = newData; applyFilter(); }} }};
}}

// ── Flat channel table: search + sort + pagination (no row-expand needed —
// each row already is one channel, unlike the video-grouped tables above) ──
const CONTACT_LABELS = {{ twitter: '𝕏', telegram: 'TG', instagram: 'IG', tiktok: 'TT', facebook: 'FB' }};

function contactPills(contact) {{
  if (!contact) return '';
  const pills = [];
  contact.split('|').filter(Boolean).forEach(tok => {{
    const i = tok.indexOf(':');
    if (i < 0) return;
    const label = tok.slice(0, i), value = tok.slice(i + 1);
    if (!value) return;
    if (label === 'email') {{
      value.split(',').filter(Boolean).forEach(addr => {{
        pills.push(`<a class="contact-pill" href="mailto:${{addr}}" onclick="event.stopPropagation()" title="${{addr}}">✉ Email</a>`);
      }});
    }} else {{
      const short = CONTACT_LABELS[label] || label;
      pills.push(`<a class="contact-pill" href="${{value}}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="${{value}}">${{short}}</a>`);
    }}
  }});
  return pills.length ? `<div class="contact-pills">${{pills.join('')}}</div>` : '';
}}

function platformCell(promoPlatform) {{
  if (!promoPlatform) {{
    return '<span class="badge" style="background:rgba(239,68,68,0.12);color:var(--red);border-color:rgba(239,68,68,0.25);">未识别</span>';
  }}
  return promoPlatform.split(',').filter(Boolean).map(p => `<span class="badge">${{p}}</span>`).join('');
}}

// 1234 -> "1.2K", 2600000 -> "2.6M" — keeps wide numbers from forcing column width
function compactNum(n) {{
  n = n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\\.0$/, '') + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\\.0$/, '') + 'K';
  return n.toLocaleString();
}}

// ISO 3166-1 alpha-2 -> flag emoji via regional indicator code points, so this
// works for any country code the API returns without a lookup table.
function countryFlag(code) {{
  if (!code || code.length !== 2) return '';
  const base = 0x1F1E6;
  return String.fromCodePoint(...[...code.toUpperCase()].map(c => base + c.charCodeAt(0) - 65));
}}

function videoDetailCell(v) {{
  return `<div class="detail-item">
    <span class="yt-detail-meta">${{v.published_at ? v.published_at.slice(0, 10) : ''}}</span>
    <span class="yt-detail-meta">${{(v.view_count || 0).toLocaleString()}} 次观看</span>
    ${{linkCell(v.video_url)}}${{v.video_title ? ` — ${{truncate(v.video_title, 60)}}` : ''}}
  </div>`;
}}

function createChannelTableController(cfg) {{
  let filtered = cfg.data.slice();
  let sortKey = cfg.sortKeyDefault || 'latest_promo_date';
  let sortDir = cfg.sortDirDefault || 'desc';
  let page = 1;
  const expanded = new Set();
  const els = cfg.elIds;

  function applyFilter() {{
    const q = els.search ? document.getElementById(els.search).value.trim().toLowerCase() : '';
    filtered = !q ? cfg.data.slice() : cfg.data.filter(r =>
      (r.account_name || '').toLowerCase().includes(q) ||
      (r.keyword || '').toLowerCase().includes(q) ||
      (r.promo_platform || '').toLowerCase().includes(q) ||
      (r.country || '').toLowerCase().includes(q) ||
      (r.market || '').toLowerCase().includes(q) ||
      (r.contact || '').toLowerCase().includes(q)
    );
    applySort();
    page = 1;
    render();
  }}

  function applySort() {{
    filtered.sort((a, b) => {{
      let va = a[sortKey], vb = b[sortKey];
      if (typeof va === 'number' || typeof vb === 'number') {{
        va = va || 0; vb = vb || 0;
        return sortDir === 'asc' ? va - vb : vb - va;
      }}
      va = (va || '').toString(); vb = (vb || '').toString();
      const cmp = va.localeCompare(vb, undefined, {{ numeric: true }});
      return sortDir === 'asc' ? cmp : -cmp;
    }});
  }}

  function render() {{
    const tbody = document.getElementById(els.tbody);
    const totalPages = Math.max(1, Math.ceil(filtered.length / cfg.pageSize));
    page = Math.min(page, totalPages);
    const start = (page - 1) * cfg.pageSize;
    const rows = filtered.slice(start, start + cfg.pageSize);

    if (rows.length === 0) {{
      tbody.innerHTML = '';
      if (els.empty) document.getElementById(els.empty).style.display = 'block';
    }} else {{
      if (els.empty) document.getElementById(els.empty).style.display = 'none';
      tbody.innerHTML = rows.map(r => {{
        const key = r.channel_id;
        const isOpen = expanded.has(key);
        const videos = r.videos || [];
        const detailRow = isOpen ? `
          <tr class="detail-row">
            <td colspan="9">
              <div class="detail-list">
                ${{videos.length ? videos.map(videoDetailCell).join('') : '<div class="detail-item">暂无抓取到的视频记录</div>'}}
              </div>
            </td>
          </tr>` : '';
        const flag = countryFlag(r.country);
        const metaBits = [
          (flag || r.country) ? `${{flag}} ${{r.country || ''}}`.trim() : '',
          r.language || '',
        ].filter(Boolean);
        return `
          <tr class="video-row channel-row" data-key="${{key}}">
            <td>
              <div class="ch-name">
                ${{r.profile_url ? `<a href="${{r.profile_url}}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${{truncate(r.account_name || r.profile_url, 26)}}</a>` : (r.account_name || '')}}
                ${{metaBits.length ? `<span class="ch-meta">${{metaBits.join(' · ')}}</span>` : ''}}
              </div>
            </td>
            <td>${{r.market ? `<span class="market-badge">${{r.market}}</span>` : ''}}</td>
            <td>${{compactNum(r.followers)}}</td>
            <td>
              <div class="ch-stat-primary">${{compactNum(r.channel_view_cnt)}} 播放</div>
              <div class="ch-stat-secondary">${{compactNum(r.channel_video_cnt)}} 视频</div>
            </td>
            <td>${{platformCell(r.promo_platform)}}</td>
            <td>${{contactPills(r.contact)}}</td>
            <td>
              <div class="ch-stat-primary">${{compactNum(r.total_views)}} 观看</div>
              <div class="ch-stat-secondary">${{videos.length}} 个视频</div>
            </td>
            <td>
              <div class="ch-stat-primary">${{r.latest_promo_date || '—'}}</div>
              <div class="ch-stat-secondary">最近抓取 ${{r.last_crawled_date || '—'}}</div>
              <div class="ch-stat-secondary">首次抓取 ${{r.first_seen_date || '—'}}</div>
            </td>
            <td class="expand-toggle">${{isOpen ? '▾ 收起' : '▸ 展开'}}</td>
          </tr>${{detailRow}}`;
      }}).join('');

      tbody.querySelectorAll('tr.channel-row').forEach(tr => {{
        tr.addEventListener('click', () => {{
          const key = tr.dataset.key;
          if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
          render();
        }});
      }});
    }}

    if (els.meta) document.getElementById(els.meta).textContent = `共 ${{filtered.length}} 个频道`;
    if (els.pageInfo) document.getElementById(els.pageInfo).textContent = `第 ${{page}} / ${{totalPages}} 页`;
    if (els.prevBtn) document.getElementById(els.prevBtn).disabled = page <= 1;
    if (els.nextBtn) document.getElementById(els.nextBtn).disabled = page >= totalPages;

    document.querySelectorAll(`#${{els.theadRow}} th[data-key]`).forEach(th => {{
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.key === sortKey) th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    }});
  }}

  if (els.search) document.getElementById(els.search).addEventListener('input', applyFilter);
  if (els.prevBtn) document.getElementById(els.prevBtn).addEventListener('click', () => {{ page--; render(); }});
  if (els.nextBtn) document.getElementById(els.nextBtn).addEventListener('click', () => {{ page++; render(); }});
  document.querySelectorAll(`#${{els.theadRow}} th[data-key]`).forEach(th => {{
    th.addEventListener('click', () => {{
      const key = th.dataset.key;
      if (sortKey === key) {{ sortDir = sortDir === 'asc' ? 'desc' : 'asc'; }}
      else {{ sortKey = key; sortDir = 'desc'; }}
      applySort();
      render();
    }});
  }});

  applySort();
  render();

  return {{
    setData(newData) {{ cfg.data = newData; applyFilter(); }},
    expandChannel(channelId) {{
      expanded.add(channelId);
      const idx = filtered.findIndex(r => r.channel_id === channelId);
      if (idx >= 0) page = Math.floor(idx / cfg.pageSize) + 1;
      render();
      const el = document.querySelector(`#${{els.tbody}} tr[data-key="${{channelId}}"]`);
      if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
    }},
  }};
}}

const channelsTable = createChannelTableController({{
  data: ALL_CHANNELS,
  pageSize: 20,
  sortKeyDefault: 'latest_promo_date',
  sortDirDefault: 'desc',
  elIds: {{ tbody: 'tbody-channels', meta: 'meta-channels', pageInfo: 'pageInfo-channels', prevBtn: 'prev-channels', nextBtn: 'next-channels', empty: 'empty-channels', theadRow: 'thead-channels' }},
}});

// ── Channels tab: all filters drive KPIs, both charts, and the detail table. ──
const followerBands = {{
  lt1k: f => f < 1e3, '1k10k': f => f >= 1e3 && f < 1e4,
  '10k100k': f => f >= 1e4 && f < 1e5, '100k1m': f => f >= 1e5 && f < 1e6,
  '1m': f => f >= 1e6,
}};
const marketCounts = new Map();
ALL_CHANNELS.forEach(c => {{ const m = c.market || '未识别'; marketCounts.set(m, (marketCounts.get(m) || 0) + 1); }});
const markets = Array.from(marketCounts.entries()).sort((a, b) => b[1] - a[1]).map(x => x[0]);
document.getElementById('chMarketFilter').insertAdjacentHTML('beforeend', markets.map(m => `<option value="${{m}}">${{m}}（${{marketCounts.get(m)}}）</option>`).join(''));
const marketColor = new Map(markets.map((m, i) => [m, COLORS[i % COLORS.length]]));

const channelMarketChart = new Chart(document.getElementById('cChMarketPortfolio'), {{
  type: 'bubble', data: {{ datasets: [] }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => {{
      const r = ctx.raw; return `${{r.market}}：${{r.x}} 个频道 · ${{compactNum(r.y)}} 观看 · ${{compactNum(r.followers)}} 粉丝`;
    }} }} }} }},
    scales: {{
      x: {{ beginAtZero: true, title: {{ display: true, text: '频道数', color: '#94A3B8' }}, grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#64748B', precision: 0 }} }},
      y: {{ beginAtZero: true, title: {{ display: true, text: '抓取视频累计观看', color: '#94A3B8' }}, grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#64748B', callback: compactNum }} }},
    }},
  }},
}});

const channelQualityChart = new Chart(document.getElementById('cChQuality'), {{
  type: 'scatter', data: {{ datasets: [{{ label: '频道', data: [], pointRadius: 4, pointHoverRadius: 7 }}] }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => {{
      const r = ctx.raw; return `${{r.name}} · ${{r.market}}：${{compactNum(r.x)}} 粉丝 · ${{compactNum(r.y)}} 观看`;
    }} }} }} }},
    scales: {{
      x: {{ type: 'logarithmic', title: {{ display: true, text: '粉丝数（对数轴）', color: '#94A3B8' }}, grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#64748B', callback: compactNum }} }},
      y: {{ type: 'logarithmic', title: {{ display: true, text: '抓取视频累计观看（对数轴）', color: '#94A3B8' }}, grid: {{ color: 'rgba(255,255,255,.05)' }}, ticks: {{ color: '#64748B', callback: compactNum }} }},
    }},
    onClick: (evt, elements) => {{ if (elements.length) channelsTable.expandChannel(channelQualityChart.data.datasets[0].data[elements[0].index].channelId); }},
  }},
}});

function refreshChannels() {{
  const market = document.getElementById('chMarketFilter').value;
  document.querySelectorAll('#chMarketQuickFilters .quick-filter-btn').forEach(btn => {{
    const active = btn.dataset.market === market;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  }});
  const band = document.getElementById('chFollowerFilter').value;
  const minViews = Number(document.getElementById('chMinViews').value || 0);
  const dateField = document.getElementById('chDateField').value;
  const dateFrom = document.getElementById('chDateFrom').value;
  const dateTo = document.getElementById('chDateTo').value;
  const query = document.getElementById('search-channels').value.trim().toLowerCase();
  const inPeriod = value => (!dateFrom || (value && value >= dateFrom)) && (!dateTo || (value && value <= dateTo));
  const baseFiltered = ALL_CHANNELS.filter(c => {{
    const rowMarket = c.market || '未识别';
    const haystack = [c.account_name, c.keyword, c.promo_platform, c.country, c.market, c.contact].join(' ').toLowerCase();
    return (!market || rowMarket === market) && (!band || followerBands[band](c.followers || 0)) &&
      (c.total_views || 0) >= minViews && (!query || haystack.includes(query));
  }});
  const filtered = baseFiltered.filter(c => (!dateFrom && !dateTo) || inPeriod(c[dateField]));

  const marketAgg = new Map();
  filtered.forEach(c => {{
    const m = c.market || '未识别';
    const agg = marketAgg.get(m) || {{ market: m, channels: 0, followers: 0, views: 0 }};
    agg.channels++; agg.followers += c.followers || 0; agg.views += c.total_views || 0; marketAgg.set(m, agg);
  }});
  const aggs = Array.from(marketAgg.values());
  const maxFollowers = Math.max(1, ...aggs.map(x => x.followers));
  channelMarketChart.data.datasets = aggs.map(a => ({{
    label: a.market, backgroundColor: marketColor.get(a.market) || COLORS[0], borderColor: 'rgba(255,255,255,.35)', borderWidth: 1,
    data: [{{ x: a.channels, y: a.views, r: 5 + Math.sqrt(a.followers / maxFollowers) * 20, market: a.market, followers: a.followers }}],
  }}));
  channelMarketChart.update();

  const quality = filtered.filter(c => (c.followers || 0) > 0 && (c.total_views || 0) > 0)
    .map(c => ({{ x: c.followers, y: c.total_views, name: c.account_name || c.channel_id, market: c.market || '未识别', channelId: c.channel_id }}));
  channelQualityChart.data.datasets[0].data = quality;
  channelQualityChart.data.datasets[0].backgroundColor = quality.map(x => marketColor.get(x.market) || COLORS[0]);
  channelQualityChart.update();

  document.getElementById('kpi-ch-total').textContent = filtered.length.toLocaleString();
  document.getElementById('kpi-ch-markets').textContent = marketAgg.size.toLocaleString();
  document.getElementById('kpi-ch-followers').textContent = compactNum(filtered.reduce((s, c) => s + (c.followers || 0), 0));
  document.getElementById('kpi-ch-views').textContent = compactNum(filtered.reduce((s, c) => s + (c.total_views || 0), 0));
  document.getElementById('kpi-ch-new').textContent = baseFiltered.filter(c => c.first_seen_date && inPeriod(c.first_seen_date)).length.toLocaleString();
  document.getElementById('kpi-ch-active').textContent = baseFiltered.filter(c => c.latest_promo_date && inPeriod(c.latest_promo_date)).length.toLocaleString();
  const dateLabel = dateFrom || dateTo ? `${{dateFrom || '最早'}}–${{dateTo || '最新'}}` : '全部日期';
  document.getElementById('chFilterMeta').textContent = `已筛选 ${{filtered.length}} / ${{ALL_CHANNELS.length}} 个频道 · ${{dateLabel}}`;
  channelsTable.setData(filtered);
}}

['chMarketFilter', 'chFollowerFilter', 'chMinViews', 'chDateField', 'chDateFrom', 'chDateTo', 'search-channels'].forEach(id =>
  document.getElementById(id).addEventListener(id === 'search-channels' || id === 'chMinViews' ? 'input' : 'change', refreshChannels));
document.querySelectorAll('#chMarketQuickFilters .quick-filter-btn').forEach(btn => btn.addEventListener('click', () => {{
  const marketSelect = document.getElementById('chMarketFilter');
  marketSelect.value = marketSelect.value === btn.dataset.market ? '' : btn.dataset.market;
  refreshChannels();
}}));
document.getElementById('chResetFilters').addEventListener('click', () => {{
  ['chMarketFilter', 'chFollowerFilter', 'chMinViews', 'chDateFrom', 'chDateTo', 'search-channels'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('chDateField').value = 'latest_promo_date';
  refreshChannels();
}});
refreshChannels();

// ── Latest tab: static snapshot of the most recent crawl date ────────────
const DATED_LEADS = ALL_LEADS.filter(l => l.date);
const LATEST_DATE = DATED_LEADS.reduce((m, l) => (l.date > m) ? l.date : m, '');
const LATEST_LEADS = LATEST_DATE ? DATED_LEADS.filter(l => l.date === LATEST_DATE) : [];
const latestViews = deriveViews(LATEST_LEADS);

document.getElementById('kpi-latest-date').textContent = LATEST_DATE || '—';
document.getElementById('kpi-latest-videos').textContent = latestViews.totalVideos;
document.getElementById('kpi-latest-records').textContent = latestViews.totalRecords;
document.getElementById('kpi-latest-youtubers').textContent = latestViews.youtubers;
document.getElementById('kpi-latest-platforms').textContent = latestViews.platforms;

document.getElementById('latestChips').innerHTML = latestViews.platformByVideo.length
  ? latestViews.platformByVideo.map(([p, c]) => `<div class="chip">${{p}} <b>${{c}}</b> 个视频</div>`).join('')
  : '<div class="table-meta">暂无数据</div>';

const latestTable = createVideoTableController({{
  data: latestViews.videoRows,
  pageSize: 10,
  sortKeyDefault: 'youtuber',
  sortDirDefault: 'asc',
  elIds: {{ search: 'search-latest', tbody: 'tbody-latest', meta: 'meta-latest', pageInfo: 'pageInfo-latest', prevBtn: 'prev-latest', nextBtn: 'next-latest', empty: 'empty-latest', theadRow: 'thead-latest' }},
}});

// ── Global tab: charts + date-filtered recompute ─────────────────────────
const trendChart = new Chart(document.getElementById('cTrend'), {{
  type: 'line',
  data: {{ labels: [], datasets: [{{ label: '新增推广视频', data: [], borderColor: COLORS[1], backgroundColor: 'rgba(34,211,238,0.12)', fill: true, tension: 0.3, pointRadius: 2 }}] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#475569', font: {{ family: 'JetBrains Mono', size: 10 }} }} }},
      y: {{ beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#475569', precision: 0 }} }},
    }},
  }},
}});

const platformByVideoChart = new Chart(document.getElementById('cPlatformByVideo'), {{
  type: 'pie',
  data: {{ labels: [], datasets: [{{ data: [], backgroundColor: COLORS, borderColor: '#0F1A2E', borderWidth: 2 }}] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'right', labels: {{ color: '#CBD5E1', font: {{ size: 11 }}, boxWidth: 12 }} }} }},
  }},
}});

const platformByYtChart = new Chart(document.getElementById('cPlatformByYt'), {{
  type: 'pie',
  data: {{ labels: [], datasets: [{{ data: [], backgroundColor: COLORS, borderColor: '#0F1A2E', borderWidth: 2 }}] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'right', labels: {{ color: '#CBD5E1', font: {{ size: 11 }}, boxWidth: 12 }} }} }},
  }},
}});

const topYtChart = new Chart(document.getElementById('cTopYt'), {{
  type: 'bar',
  data: {{ labels: [], datasets: [{{ label: '推广视频数', data: [], backgroundColor: COLORS[0] }}] }},
  options: {{
    indexAxis: 'y',
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#475569', precision: 0 }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ color: '#CBD5E1', font: {{ size: 11 }} }} }},
    }},
    onClick: (evt, elements) => {{
      if (!elements.length) return;
      showYoutuberDetail(topYtChart.data.labels[elements[0].index]);
    }},
  }},
}});

const globalTable = createVideoTableController({{
  data: [],
  pageSize: 20,
  sortKeyDefault: 'date',
  sortDirDefault: 'desc',
  elIds: {{ search: 'search-global', tbody: 'tbody-global', meta: 'meta-global', pageInfo: 'pageInfo-global', prevBtn: 'prev-global', nextBtn: 'next-global', empty: 'empty-global', theadRow: 'thead-global' }},
}});

let currentYtPlatformMap = new Map();

function showYoutuberDetail(youtuber) {{
  const panel = document.getElementById('ytDetailPanel');
  const pMap = currentYtPlatformMap.get(youtuber);
  if (!pMap) {{ panel.style.display = 'none'; return; }}
  const rows = Array.from(pMap.entries()).sort((a, b) => b[1].videos.size - a[1].videos.size);
  panel.innerHTML = `
    <h4>${{youtuber}} 的推广明细</h4>
    ${{rows.map(([platform, data]) => `
      <div class="yt-detail-row">
        <span class="badge">${{platform}}</span>
        <span class="yt-detail-meta">推了 ${{data.videos.size}} 个视频 · 共 ${{data.links.size}} 条链接</span>
        <div class="yt-links">${{Array.from(data.links).map(l => `<a href="${{l}}" target="_blank" rel="noopener">${{truncate(l, 60)}}</a>`).join('')}}</div>
      </div>
    `).join('')}}`;
  panel.style.display = 'block';
}}

function countNewVideos(leads, days) {{
  const cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  const set = new Set();
  leads.forEach(l => {{ if (l.date && l.date >= cutoff) set.add(l.video_url); }});
  return set.size;
}}

function refreshGlobal() {{
  const from = document.getElementById('dateFrom').value;
  const to   = document.getElementById('dateTo').value;
  const scoped = ALL_LEADS.filter(l =>
    (!from || (l.date && l.date >= from)) && (!to || (l.date && l.date <= to))
  );
  const views = deriveViews(scoped);

  document.getElementById('kpi-global-total-videos').textContent = views.totalVideos;
  document.getElementById('kpi-global-total-records').textContent = views.totalRecords;
  document.getElementById('kpi-global-youtubers').textContent = views.youtubers;
  document.getElementById('kpi-global-platforms').textContent = views.platforms;

  trendChart.data.labels = views.trend.map(x => x[0]);
  trendChart.data.datasets[0].data = views.trend.map(x => x[1]);
  trendChart.update();

  platformByVideoChart.data.labels = views.platformByVideo.map(x => x[0]);
  platformByVideoChart.data.datasets[0].data = views.platformByVideo.map(x => x[1]);
  platformByVideoChart.update();

  platformByYtChart.data.labels = views.platformByYoutuber.map(x => x[0]);
  platformByYtChart.data.datasets[0].data = views.platformByYoutuber.map(x => x[1]);
  platformByYtChart.update();

  topYtChart.data.labels = views.topYoutubers.map(x => x.youtuber);
  topYtChart.data.datasets[0].data = views.topYoutubers.map(x => x.videoCount);
  topYtChart.update();
  currentYtPlatformMap = views.ytPlatformMap;
  document.getElementById('ytDetailPanel').style.display = 'none';

  globalTable.setData(views.videoRows);

  document.getElementById('filterMeta').textContent =
    (from || to) ? `已筛选：${{from || '最早'}} ~ ${{to || '最新'}}` : '';
}}

document.getElementById('kpi-global-new-7d').textContent = countNewVideos(ALL_LEADS, 7);
document.getElementById('kpi-global-new-30d').textContent = countNewVideos(ALL_LEADS, 30);
document.getElementById('dateFrom').addEventListener('change', refreshGlobal);
document.getElementById('dateTo').addEventListener('change', refreshGlobal);
document.getElementById('resetDate').addEventListener('click', () => {{
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value = '';
  refreshGlobal();
}});

refreshGlobal();

// ── 竞品声量 tab: trailing N-day windows per platform, WoW%, trend,
// account-concentration, and a click-to-drill-down video list. Everything
// here is derived client-side from ALL_LEADS (already loaded above) — no
// new data plumbing needed. "声量" = count of matched promo videos (leads
// rows) in the window; this pipeline never scrapes comments, so it will not
// match a listening tool's comment-inclusive numbers — it's a same-shape,
// different-source proxy for the same weekly-tracking workflow.

function addDaysStr(dateStr, days) {{
  const d = new Date(dateStr + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}}

// 距 dateStr 最近的、<= dateStr 的那个周四（getUTCDay: 0=周日...4=周四...6=周六）。
function mostRecentThursday(dateStr) {{
  const d = new Date(dateStr + 'T00:00:00Z');
  const diff = (d.getUTCDay() - 4 + 7) % 7;
  d.setUTCDate(d.getUTCDate() - diff);
  return d.toISOString().slice(0, 10);
}}

function buildWindows(dates, windowSize, windowCount) {{
  if (!dates.length) return [];
  const latest = dates[dates.length - 1];
  const windows = [];

  if (windowSize === 7) {{
    // 7 天窗口固定按自然周对齐：周五 ~ 周四，不是从最新数据往回数 7 天——
    // 这样每周的边界是固定的（跟运营周报的统计口径对齐），不会因为哪天跑的
    // 看板生成而漂移。
    let end = mostRecentThursday(latest);
    for (let i = 0; i < windowCount; i++) {{
      const start = addDaysStr(end, -6);
      windows.push({{ start, end, label: `${{start}} ~ ${{end}}` }});
      end = addDaysStr(start, -1);
    }}
    return windows;
  }}

  // 14 / 30 天：维持原来"从最新数据往回数"的滑动窗口，没有自然对齐的概念。
  let end = latest;
  for (let i = 0; i < windowCount; i++) {{
    const start = addDaysStr(end, -(windowSize - 1));
    windows.push({{ start, end, label: start === end ? start : `${{start}} ~ ${{end}}` }});
    end = addDaysStr(start, -1);
  }}
  return windows; // most recent first
}}

function leadsInWindow(platform, win) {{
  return ALL_LEADS.filter(l => l.platform === platform && l.date >= win.start && l.date <= win.end);
}}

function leadsInRange(platform, startDate, endDate) {{
  return ALL_LEADS.filter(l => l.platform === platform && l.date >= startDate && l.date <= endDate);
}}

function wowCell(cur, prev) {{
  if (prev === 0) {{
    if (cur === 0) return {{ text: '—', cls: 'wow-flat' }};
    return {{ text: 'NEW', cls: 'wow-up wow-flag' }};
  }}
  const pct = (cur - prev) / prev * 100;
  const cls = pct > 0 ? 'wow-up' : (pct < 0 ? 'wow-down' : 'wow-flat');
  const flag = Math.abs(pct) >= 30 ? ' wow-flag' : '';
  const arrow = pct > 0 ? '▲' : (pct < 0 ? '▼' : '');
  return {{ text: `${{arrow}} ${{pct >= 0 ? '+' : ''}}${{pct.toFixed(0)}}%`, cls: cls + flag }};
}}

// 只呈现 config.CORE_COMPETITOR_KEYWORDS 对应的平台——这几个是唯一享受宽松
// 翻页上限（config.CORE_SEARCH_MAX_RESULTS）的关键词，数据基本完整；其余关键词
// 仍然只有 1 页/天，拿来做时间窗口对比意义不大，所以这个 tab 直接不展示。
// 即使某个核心竞品当前还没有任何匹配记录，也保留在列表里（count=0），不隐藏。
function volAllPlatforms() {{
  const counts = new Map();
  CORE_COMPETITOR_PLATFORMS.forEach(p => counts.set(p, 0));
  ALL_LEADS.forEach(l => {{
    if (!counts.has(l.platform)) return;
    counts.set(l.platform, counts.get(l.platform) + 1);
  }});
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
}}

const CHANNEL_BY_NAME = new Map(ALL_CHANNELS.map(c => [c.account_name, c]));
// leads only stores {{youtuber, video_url, ...}} — video_title/view_count live in
// channels.videos. This is a best-effort join (leads.youtuber -> channels.account_name),
// and only channels this system has fully processed will resolve — historical leads
// crawled before the channels table existed won't join. Coverage grows over time.
const VIDEO_BY_URL = new Map();
ALL_CHANNELS.forEach(c => (c.videos || []).forEach(v => VIDEO_BY_URL.set(v.video_url, v)));
const HASHTAG_RE = /#[^\\s#]+/g;

function normalizeTitle(t) {{
  return (t || '')
    .replace(HASHTAG_RE, '')
    .replace(/[^\\p{{L}}\\p{{N}}]+/gu, ' ')
    .trim()
    .toLowerCase();
}}

const volSelectedPlatforms = new Set();
let volDrillKey = null;

function renderVolChips() {{
  const all = volAllPlatforms();
  const container = document.getElementById('volPlatformChips');
  container.innerHTML = all.map(([p, c]) => `
    <div class="platform-chip${{volSelectedPlatforms.has(p) ? ' active' : ''}}" data-platform="${{p}}">${{p}} <b>${{c}}</b></div>
  `).join('');
  container.querySelectorAll('.platform-chip').forEach(chip => {{
    chip.addEventListener('click', () => {{
      const p = chip.dataset.platform;
      if (volSelectedPlatforms.has(p)) volSelectedPlatforms.delete(p); else volSelectedPlatforms.add(p);
      closeDrill();
      renderVolume();
    }});
  }});
}}

const volTrendChart = new Chart(document.getElementById('cVolTrend'), {{
  type: 'line',
  data: {{ labels: [], datasets: [] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#CBD5E1', font: {{ size: 11 }}, boxWidth: 12 }} }} }},
    scales: {{
      x: {{ grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#475569', font: {{ family: 'JetBrains Mono', size: 10 }} }} }},
      y: {{ beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#475569', precision: 0 }} }},
    }},
  }},
}});

const volPerformanceChart = new Chart(document.getElementById('cVolPerformance'), {{
  data: {{
    labels: [],
    datasets: [
      {{ type: 'bar', label: '视频数量', data: [], yAxisID: 'yCount', backgroundColor: 'rgba(34,211,238,.55)', borderColor: '#22D3EE', borderWidth: 1, borderRadius: 3 }},
      {{ type: 'line', label: '已覆盖累计播放量', data: [], yAxisID: 'yViews', borderColor: '#F59E0B', backgroundColor: '#F59E0B', pointRadius: 4, pointHoverRadius: 6, tension: .25 }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ color: '#CBD5E1', font: {{ size: 11 }}, boxWidth: 12 }} }},
      tooltip: {{ callbacks: {{
        label: ctx => ctx.dataset.yAxisID === 'yViews'
          ? `${{ctx.dataset.label}}：${{Number(ctx.raw || 0).toLocaleString()}}`
          : `${{ctx.dataset.label}}：${{Number(ctx.raw || 0).toLocaleString()}} 条`,
      }} }},
    }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ color: '#CBD5E1', font: {{ size: 11 }} }} }},
      yCount: {{ position: 'left', beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#22D3EE', precision: 0 }}, title: {{ display: true, text: '视频数', color: '#22D3EE' }} }},
      yViews: {{ position: 'right', beginAtZero: true, grid: {{ drawOnChartArea: false }}, ticks: {{ color: '#F59E0B', callback: compactNum }}, title: {{ display: true, text: '累计播放量', color: '#F59E0B' }} }},
    }},
  }},
}});

function compactNum(value) {{
  const n = Number(value || 0);
  if (n >= 1000000) return `${{(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}}M`;
  if (n >= 1000) return `${{(n / 1000).toFixed(n >= 100000 ? 0 : 1)}}K`;
  return n.toString();
}}

function videoMetrics(rows) {{
  const urls = new Set(rows.map(r => r.video_url).filter(Boolean));
  let views = 0, covered = 0;
  urls.forEach(url => {{
    const video = VIDEO_BY_URL.get(url);
    if (!video) return;
    covered++;
    views += Number(video.view_count || 0);
  }});
  return {{ views, covered, total: urls.size }};
}}

// 语言构成：part-to-whole across platforms -> percentage-stacked bar, categorical
// color per language (fixed slot order so the same language keeps the same hue
// across renders). Only counts leads whose channel joined successfully — the
// coverage hint below the chart states how many didn't.
const volLangChart = new Chart(document.getElementById('cVolLang'), {{
  type: 'bar',
  data: {{ labels: [], datasets: [] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'bottom', labels: {{ color: '#CBD5E1', font: {{ size: 11 }}, boxWidth: 12 }} }} }},
    scales: {{
      x: {{ stacked: true, grid: {{ display: false }}, ticks: {{ color: '#CBD5E1', font: {{ size: 11 }} }} }},
      y: {{ stacked: true, beginAtZero: true, max: 100, grid: {{ color: 'rgba(255,255,255,0.05)' }},
           ticks: {{ color: '#475569', callback: v => v + '%' }} }},
    }},
  }},
}});

// Hashtag 排名：单一测量、按出现次数排名 -> 单一色相横向条形图（跟 Top15 频道图同款）。
const volHashtagChart = new Chart(document.getElementById('cVolHashtag'), {{
  type: 'bar',
  data: {{ labels: [], datasets: [{{ label: '出现次数', data: [], backgroundColor: COLORS[0], borderRadius: 3 }}] }},
  options: {{
    indexAxis: 'y',
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#475569', precision: 0 }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ color: '#CBD5E1', font: {{ size: 11 }} }} }},
    }},
  }},
}});

function closeDrill() {{
  volDrillKey = null;
  document.getElementById('volDrillPanel').style.display = 'none';
  document.querySelectorAll('.vol-cell.selected').forEach(el => el.classList.remove('selected'));
}}

function openDrill(platform, win, key) {{
  volDrillKey = key;
  const rows = leadsInWindow(platform, win).sort((a, b) => (b.date || '').localeCompare(a.date || ''));
  const panel = document.getElementById('volDrillPanel');
  panel.innerHTML = `
    <h4>${{platform}} · ${{win.label}} · 共 ${{rows.length}} 条</h4>
    ${{rows.map(r => {{
      const ch = CHANNEL_BY_NAME.get(r.youtuber);
      const vid = VIDEO_BY_URL.get(r.video_url);
      const meta = ch ? [ch.country, ch.language, ch.market].filter(Boolean).join(' · ') : '';
      const metaLine = [meta, r.date, vid ? `${{(vid.view_count || 0).toLocaleString()}} 次观看` : ''].filter(Boolean).join(' · ');
      return `<div class="yt-detail-row">
        <span class="badge">${{r.youtuber || '未知'}}</span>
        ${{metaLine ? `<span class="yt-detail-meta">${{metaLine}}</span>` : ''}}
        <div class="yt-links">
          ${{vid && vid.video_title ? `<div>${{truncate(vid.video_title, 90)}}</div>` : '<div class="yt-detail-meta">（标题暂未关联到——该频道还没被 channels 表收录，见下方覆盖率提示）</div>'}}
          ${{linkCell(r.video_url)}}
        </div>
      </div>`;
    }}).join('')}}
  `;
  panel.style.display = 'block';
  document.querySelectorAll('.vol-cell.selected').forEach(el => el.classList.remove('selected'));
  document.querySelectorAll(`.vol-cell[data-key="${{key}}"]`).forEach(el => el.classList.add('selected'));
  panel.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
}}

function renderVolume() {{
  const windowSize = parseInt(document.getElementById('volWindowSize').value, 10);
  const windowCount = parseInt(document.getElementById('volWindowCount').value, 10);
  const dates = ALL_LEADS.map(l => l.date).filter(Boolean).sort();
  const windows = buildWindows(dates, windowSize, windowCount);

  if (!windows.length) {{
    document.getElementById('volMeta').textContent = '暂无数据';
    return;
  }}

  if (volSelectedPlatforms.size === 0) {{
    CORE_COMPETITOR_PLATFORMS.forEach(p => volSelectedPlatforms.add(p));
  }}
  renderVolChips();
  const platforms = volAllPlatforms().map(([p]) => p).filter(p => volSelectedPlatforms.has(p));

  document.getElementById('volMeta').textContent =
    `${{windows[windows.length - 1].start}} ~ ${{windows[0].end}}，共 ${{windows.length}} 个窗口 · ${{platforms.length}} 个平台`;

  const counts = {{}};
  platforms.forEach(p => {{ counts[p] = windows.map(w => leadsInWindow(p, w).length); }});

  document.getElementById('volTableHead').innerHTML =
    `<th>窗口</th>` + platforms.map(p => `<th>${{p}}</th><th>WoW</th>`).join('');

  let bodyHtml = windows.map((w, wi) => {{
    const cells = platforms.map(p => {{
      const cur = counts[p][wi];
      const prev = wi + 1 < windows.length ? counts[p][wi + 1] : null;
      const key = `${{p}}||${{wi}}`;
      const wow = prev === null ? {{ text: '', cls: '' }} : wowCell(cur, prev);
      return `<td class="vol-cell" data-key="${{key}}" data-platform="${{p}}" data-win="${{wi}}">${{cur}}</td><td class="${{wow.cls}}">${{wow.text}}</td>`;
    }}).join('');
    return `<tr><td>${{w.label}}</td>${{cells}}</tr>`;
  }}).join('');
  const sumCells = platforms.map(p =>
    `<td style="font-weight:600;font-family:var(--font-mono);">${{counts[p].reduce((a, b) => a + b, 0)}}</td><td></td>`
  ).join('');
  bodyHtml += `<tr style="border-top:2px solid var(--border-h);"><td style="font-weight:600;">合计</td>${{sumCells}}</tr>`;
  const tbody = document.getElementById('volTableBody');
  tbody.innerHTML = bodyHtml;

  tbody.querySelectorAll('td.vol-cell[data-platform]').forEach(td => {{
    td.addEventListener('click', () => {{
      const key = td.dataset.key;
      if (volDrillKey === key) {{ closeDrill(); return; }}
      openDrill(td.dataset.platform, windows[parseInt(td.dataset.win, 10)], key);
    }});
  }});
  if (volDrillKey) {{
    document.querySelectorAll(`.vol-cell[data-key="${{volDrillKey}}"]`).forEach(el => el.classList.add('selected'));
  }}

  const chronoWindows = windows.slice().reverse();
  volTrendChart.data.labels = chronoWindows.map(w => w.start);
  volTrendChart.data.datasets = platforms.map((p, i) => ({{
    label: p,
    data: chronoWindows.map(w => leadsInWindow(p, w).length),
    borderColor: COLORS[i % COLORS.length], backgroundColor: 'transparent',
    tension: 0.3, pointRadius: 3,
  }}));
  volTrendChart.update();

  const latestWin = windows[0];
  const performanceRows = platforms.map(p => {{
    const rows = leadsInWindow(p, latestWin);
    return {{ platform: p, count: rows.length, metrics: videoMetrics(rows) }};
  }}).sort((a, b) => b.count - a.count);
  volPerformanceChart.data.labels = performanceRows.map(x => x.platform);
  volPerformanceChart.data.datasets[0].data = performanceRows.map(x => x.count);
  volPerformanceChart.data.datasets[1].data = performanceRows.map(x => x.metrics.views);
  volPerformanceChart.update();
  const viewCovered = performanceRows.reduce((sum, x) => sum + x.metrics.covered, 0);
  const viewTotal = performanceRows.reduce((sum, x) => sum + x.metrics.total, 0);
  document.getElementById('volViewsCoverage').textContent = viewTotal
    ? `播放量覆盖 ${{viewCovered}}/${{viewTotal}} 个去重视频；未关联到 channels 的历史视频不计入播放量汇总`
    : '暂无可汇总的播放量数据';

  // ── 语言构成：跨选中平台 + 全部可见窗口（不只最新窗口，样本太小时占比没有意义）──
  const rangeStart = windows[windows.length - 1].start, rangeEnd = windows[0].end;
  const langTotals = new Map();
  const perPlatformLang = {{}};
  let langKnown = 0, langTotal = 0;
  platforms.forEach(p => {{
    const rows = leadsInRange(p, rangeStart, rangeEnd);
    const langs = new Map();
    rows.forEach(r => {{
      langTotal++;
      const ch = CHANNEL_BY_NAME.get(r.youtuber);
      const lang = ch && ch.language ? ch.language : '';
      if (!lang) return;
      langKnown++;
      langs.set(lang, (langs.get(lang) || 0) + 1);
      langTotals.set(lang, (langTotals.get(lang) || 0) + 1);
    }});
    perPlatformLang[p] = {{ langs, known: Array.from(langs.values()).reduce((a, b) => a + b, 0) }};
  }});
  const topLangs = Array.from(langTotals.entries()).sort((a, b) => b[1] - a[1]).slice(0, 6).map(x => x[0]);
  const langSlots = [...topLangs, '其他'];
  volLangChart.data.labels = platforms;
  volLangChart.data.datasets = langSlots.map((lang, i) => ({{
    label: lang,
    backgroundColor: COLORS[i % COLORS.length],
    data: platforms.map(p => {{
      const info = perPlatformLang[p];
      if (!info.known) return 0;
      const cnt = lang === '其他'
        ? info.known - topLangs.reduce((s, l) => s + (info.langs.get(l) || 0), 0)
        : (info.langs.get(lang) || 0);
      return cnt / info.known * 100;
    }}),
  }}));
  volLangChart.update();
  document.getElementById('volLangCoverage').textContent =
    langTotal ? `语言信息覆盖 ${{langKnown}}/${{langTotal}} 条（未覆盖的频道还没被 channels 表收录，后续抓取会自动补全，图表只统计已覆盖部分）` : '暂无数据';

  // ── Hashtag：同样跨选中平台 + 全部可见窗口。抓取时已经从标题+简介一起提取好
  // 存进 channels.videos[].hashtags 了（简介本身不落库，只留提取结果），这里
  // 直接读现成的数组，不再对标题重新跑一遍正则——标题里几乎不带 # 号，早期
  // 只从标题提取导致这张图基本总是空的，就是这个原因。
  const tagCounts = new Map();
  let tagKnown = 0, tagTotal = 0;
  platforms.forEach(p => {{
    leadsInRange(p, rangeStart, rangeEnd).forEach(r => {{
      tagTotal++;
      const vid = VIDEO_BY_URL.get(r.video_url);
      if (!vid) return;
      tagKnown++;
      (vid.hashtags || []).forEach(tag => {{
        tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1);
      }});
    }});
  }});
  const topTags = Array.from(tagCounts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 15);
  volHashtagChart.data.labels = topTags.map(x => x[0]);
  volHashtagChart.data.datasets[0].data = topTags.map(x => x[1]);
  volHashtagChart.update();
  document.getElementById('volHashtagCoverage').textContent =
    tagTotal ? `视频信息覆盖 ${{tagKnown}}/${{tagTotal}} 条（标题+简介，来自 channels 表）；共识别到 ${{tagCounts.size}} 个不同 hashtag` : '暂无数据';

  document.getElementById('volConcentrationBody').innerHTML = platforms.map(p => {{
    const rows = leadsInWindow(p, latestWin);
    if (!rows.length) return `<tr><td>${{p}}</td><td>0</td><td>0</td><td>0</td><td>—</td><td>—</td><td>—</td></tr>`;
    const metrics = videoMetrics(rows);
    const byYt = new Map();
    rows.forEach(r => byYt.set(r.youtuber, (byYt.get(r.youtuber) || 0) + 1));
    const [topYt, topCnt] = Array.from(byYt.entries()).sort((a, b) => b[1] - a[1])[0];
    const share = topCnt / rows.length * 100;
    const flag = share >= 50 ? ` <span class="concentration-flag">集中</span>` : '';

    const titles = rows.map(r => {{
      const vid = VIDEO_BY_URL.get(r.video_url);
      return vid && vid.video_title ? normalizeTitle(vid.video_title) : null;
    }}).filter(Boolean);
    const distinctTitles = new Set(titles).size;
    const templated = titles.length >= 3 && distinctTitles / titles.length <= 0.5;
    const titleCell = titles.length
      ? `${{distinctTitles}}/${{titles.length}}${{templated ? ` <span class="concentration-flag">疑似模板化</span>` : ''}}`
      : '—';

    return `<tr>
      <td>${{p}}</td><td>${{rows.length}}</td><td title="覆盖 ${{metrics.covered}}/${{metrics.total}} 个去重视频">${{metrics.views.toLocaleString()}}</td><td>${{byYt.size}}</td>
      <td>${{topYt || '未知'}}</td><td>${{share.toFixed(0)}}%${{flag}}</td>
      <td>${{titleCell}}</td>
    </tr>`;
  }}).join('');
}}

document.getElementById('volWindowSize').addEventListener('change', () => {{ closeDrill(); renderVolume(); }});
document.getElementById('volWindowCount').addEventListener('change', () => {{ closeDrill(); renderVolume(); }});
renderVolume();
</script>
</body>
</html>
"""
    return _prune_generated_page(html_output, page_mode)


def run():
    leads = load_leads()
    channels = load_channels()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    weekly_insight = load_weekly_insight()
    html = generate_html(leads, channels, run_date, weekly_insight, page_mode="main")
    channels_html = generate_html(leads, channels, run_date, weekly_insight, page_mode="channels")
    volume_html = generate_html(leads, channels, run_date, weekly_insight, page_mode="volume")

    OUT_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    CHANNELS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANNELS_OUT_PATH.write_text(channels_html, encoding="utf-8")
    VOLUME_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOLUME_OUT_PATH.write_text(volume_html, encoding="utf-8")
    print(
        f"[Report] Generated {OUT_PATH}, {CHANNELS_OUT_PATH}, and {VOLUME_OUT_PATH} "
        f"({len(leads)} leads, {len(channels)} channels)"
    )


if __name__ == "__main__":
    run()
