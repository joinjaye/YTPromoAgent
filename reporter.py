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
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    CONTENT_TOPIC_KEYWORDS, CORE_COMPETITOR_DISPLAY_NAMES,
    CORE_COMPETITOR_KEYWORDS, EARLY_PERFORMANCE_MIN_SAMPLE,
    WOW_HIGHLIGHT_ABS_CHANGE, WOW_HIGHLIGHT_PERCENT,
)
from link_extractor import _PLATFORMS
from competitor_analysis import build_report_payload, build_video_facts

# Display-cased platform names for the core competitor set (e.g. "weex" -> "Weex"),
# resolved once from the same brand list link_extractor.py uses for matching — so
# editing config.CORE_COMPETITOR_KEYWORDS automatically flows through to the
# 竞品声量 tab without touching this file.
CORE_COMPETITOR_PLATFORMS = list(CORE_COMPETITOR_DISPLAY_NAMES.values())

DB_PATH  = Path(__file__).parent / "data" / "leads.db"
OUT_DIR  = Path(__file__).parent / "site"
OUT_PATH = OUT_DIR / "index.html"
CHANNELS_OUT_PATH = OUT_DIR / "channels" / "index.html"
CHANNELS_LARK_URL = "https://skyrocket.sg.larksuite.com/base/A35sbGSpRamdsistOZqlbWrOg9b?table=tblXmEt7PVgj9sll&view=vewAyQ6xXp"
VOLUME_OUT_PATH = OUT_DIR / "volume" / "index.html"
INSIGHT_PATH = Path(__file__).parent / "data" / "weekly_insight.json"

CST = timezone(timedelta(hours=8))

CHART_COLORS = [
    "#D85C72", "#FF7A45", "#F3BA4B", "#B96BC7",
    "#FF2D46", "#E88B6A", "#FFD166", "#9F5F80",
    "#F08A9B", "#D79057", "#C97A9A", "#E6A63C",
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
    def preview(value: str, limit: int = 92) -> str:
        """Compact, deterministic preview; the complete text stays expandable."""
        value = " ".join(value.split())
        if len(value) <= limit:
            return value
        for stop in ("。", "；"):
            cut = value.find(stop)
            if 24 <= cut < limit:
                return value[:cut + 1]
        return value[:limit].rstrip("，；。 ") + "…"
    platform_cards = []
    # v1 compatibility: keep displaying the last successful legacy payload.
    for item in insight.get("platforms", []):
        if isinstance(item, dict):
            analysis = item.get("analysis")
            content = f'<p>{esc(analysis)}</p>' if isinstance(analysis, str) else f'<ul>{bullets(item.get("bullets", []))}</ul>'
            platform_cards.append(f'<article class="ai-platform"><h4>{esc(item.get("name"))}</h4>{content}</article>')
    v2 = isinstance(insight.get("core_insights"), list)
    window = payload.get("window", {})
    core_insights = [item for item in insight.get("core_insights", []) if isinstance(item, str)]
    compact_insights = "".join(
        f'''<details class="ai-insight-item">
          <summary><span class="ai-insight-index">{index:02d}</span><span class="ai-insight-preview">{esc(preview(item))}</span><span class="ai-expand-label">展开</span></summary>
          <p class="ai-insight-detail">{esc(item)}</p>
        </details>'''
        for index, item in enumerate(core_insights, 1)
    )
    return f"""
      <div class="ai-insight-head">
        <div><span class="ai-label">CURSOR AGENT · WEEKLY WINSIGHT</span>
          <h3>{esc(insight.get("headline"))}</h3></div>
        <div class="ai-period"><span>数据窗口 {esc(window.get("start"))} — {esc(window.get("end"))}</span><span title="{esc(payload.get('generated_at'))}">已生成</span></div>
      </div>
      {f'<div class="ai-summary ai-brief-grid">{compact_insights}</div>' if v2 else (f'<div class="ai-summary"><ul>{bullets(insight.get("summary", []))}</ul></div>' if insight.get('summary') else '')}
      {f'<div class="ai-platform-grid">{"".join(platform_cards)}</div>' if platform_cards else ''}
      {f'<details class="ai-more"><summary><span>Zoomex 对照与下周关注</span><span class="ai-more-meta">{len(insight.get("next_week", []))} 项待验证</span></summary><div class="ai-foot-grid"><section><h4>Zoomex 对照</h4><p>{esc(insight.get("zoomex_comparison"))}</p></section><section><h4>下周关注</h4><ul>{bullets(insight.get("next_week", []))}</ul></section></div></details>' if v2 else ''}
      {'<div class="ai-caveat"><strong>历史版本提示</strong> 此结果由旧版结构生成；页面统一按首采快照口径理解，下一次成功生成后将自动升级为新版结构。</div>' if not v2 else ''}
      {f'<details class="ai-caveat"><summary>数据覆盖与口径提示</summary><p>{esc(insight.get("caveat"))}</p></details>' if v2 and insight.get('caveat') else (f'<div class="ai-caveat"><strong>口径提示</strong> {esc(insight.get("caveat"))}</div>' if insight.get('caveat') else '')}
    """


def _volume_guide_html() -> str:
    """Volume-only metric dictionary, kept next to the deterministic rules."""
    topic_meanings = {
        "Activity": "活动、竞赛、空投、奖励或赠金类内容",
        "Product": "产品、App、平台功能、合约、现货或跟单功能",
        "Tutorial": "教程、指南、注册、充值、提现或操作步骤",
        "Market Analysis": "行情、价格、技术分析、预测或市场展望",
        "Trading Signal": "入场、做多/做空、止盈、止损或交易信号",
        "Review/Comparison": "评测、对比、VS、优缺点或平台比较",
        "Listing": "上币、新币、新交易对或 Launchpool",
        "Brand Introduction": "品牌介绍、平台概览或“是什么”类内容",
    }
    topic_cards = []
    for topic, meaning in topic_meanings.items():
        examples = " / ".join(CONTENT_TOPIC_KEYWORDS.get(topic, ())[:4])
        topic_cards.append(
            f'<article class="guide-term"><h4><code>{html.escape(topic)}</code></h4>'
            f'<p>{meaning}。示例触发词：{html.escape(examples)}。</p></article>'
        )
    topic_cards.append(
        '<article class="guide-term"><h4><code>Other</code></h4>'
        '<p>标题、Description 和 Hashtag 都未命中已配置主题关键词。</p></article>'
    )
    return f"""
<div class="guide-backdrop" id="volGuideBackdrop" aria-hidden="true"></div>
<aside class="guide-drawer" id="volGuideDrawer" role="dialog" aria-modal="true" aria-labelledby="volGuideTitle" aria-hidden="true">
  <div class="guide-head">
    <div><span class="ai-label">VOLUME METRIC DICTIONARY</span><h2 id="volGuideTitle">口径说明</h2><p>理解竞品推广规模、账号结构、内容分类与首采信号。</p></div>
    <button type="button" class="guide-close" id="volGuideClose" aria-label="关闭使用指南">关闭</button>
  </div>
  <div class="guide-search"><label for="volGuideSearch">搜索指标或口径</label><input type="search" id="volGuideSearch" placeholder="例如：独立账号、brand_led、早期高表现" autocomplete="off"></div>
  <div class="guide-body" id="volGuideBody">
    <details class="guide-group" open><summary>核心计数与窗口</summary><div class="guide-terms">
      <article class="guide-term"><h4>推广记录数</h4><p>数据库中命中的推广链接记录数。同一视频可因多个链接产生多条记录，不用于竞品规模比较。</p></article>
      <article class="guide-term"><h4>推广视频数</h4><p>按 <code>competitor × video_url</code> 去重。同一视频推广多个竞品时，会分别计入对应竞品。</p></article>
      <article class="guide-term"><h4>Promotion Share</h4><p>某竞品去重推广视频数 ÷ 五个核心竞品视频数之和。仅表示当前系统覆盖范围，不是全网声量份额。</p></article>
      <article class="guide-term"><h4>GR / 前期</h4><p>GR（Growth Rate）为指定期间相对紧邻上一同长期间的视频数增长率。前期为 0 时不计算百分比，当前有量则显示 NEW。只有变化率绝对值≥{WOW_HIGHLIGHT_PERCENT}%，且视频数绝对变化≥{WOW_HIGHLIGHT_ABS_CHANGE}，才显著高亮。</p></article>
    </div></details>

    <details class="guide-group" open><summary>竞品矩阵与账号结构</summary><div class="guide-terms">
      <article class="guide-term"><h4>独立账号</h4><p>当前窗口内推广该竞品的去重 YouTube 账号数。优先用 Channel ID 去重，缺失时才使用账号名。</p></article>
      <article class="guide-term"><h4>“连续”与“均”</h4><p><strong>连续</strong>：指定期间和上一同长期间都推广该竞品的账号数。<strong>均</strong>：去重推广视频数 ÷ 独立账号数，即每个账号平均推广视频数。</p></article>
      <article class="guide-term"><h4>Top1 / Top3</h4><p>发布量最高的 1 个 / 3 个账号，其去重推广视频占该竞品视频总数的比例。</p></article>
      <article class="guide-term"><h4>账号集中信号</h4><p>样本&lt;3 为“样本不足”；Top1≥60% 为“高度集中”；40%–60% 为“相对集中”；低于40% 为“相对分散”。</p></article>
    </div></details>

    <details class="guide-group"><summary>推广方式判定</summary><div class="guide-intro">推广方式是可多选的规则标签，由标题、推广链接命中、同视频平台数和明确配置的官方频道共同决定。</div><div class="guide-terms">
      <article class="guide-term"><h4><code>brand_led</code></h4><p>竞品名称在视频标题中作为独立词命中，视为品牌导向内容。</p></article>
      <article class="guide-term"><h4><code>description_only</code></h4><p>推广链接命中了该竞品，但竞品名未出现在标题中。不因 Description 里有链接就判为独立品牌内容。</p></article>
      <article class="guide-term"><h4><code>multi_platform</code></h4><p>同一 YouTube 视频同时命中两个或以上交易平台的推广链接。</p></article>
      <article class="guide-term"><h4><code>unclassified</code></h4><p>兼容性保留标签。</p></article>
    </div></details>

    <details class="guide-group"><summary>内容主题判定</summary><div class="guide-intro">对标题 + Description + Hashtag 进行不区分大小写的关键词匹配。一条视频可同时命中多个主题；在视频明细中点击“查看判定依据”可稳定展示主题命中词。</div><div class="guide-terms">{"".join(topic_cards)}</div></details>

    <details class="guide-group"><summary>首采表现与观察时长</summary><div class="guide-terms">
      <article class="guide-term"><h4>首采播放中位数</h4><p>只使用视频首次被系统识别时保存的播放量，不是最终播放或完整生命周期表现。</p></article>
      <article class="guide-term"><h4>初始互动率</h4><p><code>(首采点赞 + 首采评论) ÷ 首采播放</code>。播放为 0 或必要字段缺失时显示“—”。</p></article>
      <article class="guide-term"><h4>观察时长</h4><p>首次抓取时间 − 视频发布时间，分为 0–12h、12–24h、24–36h、36–48h、48h+ 和 Unknown。</p></article>
      <article class="guide-term"><h4>早期高表现</h4><p>在相同观察时长区间内，首采播放进入核心竞品视频前 20%。每个区间样本至少 {EARLY_PERFORMANCE_MIN_SAMPLE} 条才计算。</p></article>
    </div></details>

    <details class="guide-group"><summary>结构信号与推断限制</summary><div class="guide-terms">
      <article class="guide-term"><h4>集中铺量 / 长尾扩张</h4><p>账号高度或相对集中时标记“集中铺量”；账号≥4 且 Top1&lt;40% 时标记“长尾扩张”。</p></article>
      <article class="guide-term"><h4>为主 / 内容增长</h4><p><code>description_only</code>≥60% 为“Description挂链为主”；<code>multi_platform</code>≥50% 为“Multi-platform为主”；Activity 或 Product 比前窗增加≥3条时标记内容增长。</p></article>

    </div></details>
    <div class="guide-empty" id="volGuideEmpty" hidden>未找到匹配的指标或口径。</div>
  </div>
</aside>
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
            html_text, "// ── 竞品声量 tab", "renderVolumeV2();", include_end=True, last_end=True
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
            html_text, "// ── 竞品声量 tab", "renderVolumeV2();", include_end=True, last_end=True
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
    html_text = _remove_between(
        html_text, "// ── 竞品声量 tab", "renderVolume();", include_end=True, last_end=True
    )
    return html_text


def generate_html(
    leads: list[dict], channels: list[dict], run_date: str,
    weekly_insight: dict | None = None, page_mode: str = "main",
) -> str:
    colors_js         = json.dumps(CHART_COLORS)
    # Standalone pages must not embed unrelated datasets. Volume is computed
    # server-side into VOLUME_DATA and never needs the raw all-platform leads or
    # channel table payload in the browser.
    all_js            = json.dumps(_row_dicts(leads) if page_mode == "main" else [], ensure_ascii=False)
    channels_js       = json.dumps(
        _channel_row_dicts(channels) if page_mode == "channels" else [], ensure_ascii=False
    )
    volume_facts = build_video_facts(leads, channels, CORE_COMPETITOR_PLATFORMS) if page_mode == "volume" else []
    volume_payload_js = json.dumps(
        build_report_payload(volume_facts, CORE_COMPETITOR_PLATFORMS) if volume_facts else {"facts": [], "windows": {}},
        ensure_ascii=False,
    )
    core_platforms_js = json.dumps(CORE_COMPETITOR_PLATFORMS, ensure_ascii=False)
    insight_html      = _insight_html(weekly_insight or {})
    channels_page = page_mode == "channels"
    volume_page = page_mode == "volume"
    page_title = (
        "PromoLeads · 频道视图" if channels_page
        else "PromoLeads · 竞品 YouTube 推广表现" if volume_page
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
    guide_button_html = (
        '<button type="button" class="guide-open" id="volGuideOpen" '
        'aria-controls="volGuideDrawer" aria-expanded="false">口径说明</button>'
        if volume_page else ""
    )
    guide_drawer_html = _volume_guide_html() if volume_page else ""
    channels_lark_link_html = (
        f'<a class="table-external-link" href="{CHANNELS_LARK_URL}" target="_blank" '
        'rel="noopener noreferrer" aria-label="在新窗口打开 Lark 频道表格">'
        '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M14 5h5v5"/><path d="M10 14 19 5"/>'
        '<path d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>'
        '</svg><span>打开 Lark 表格</span></a>'
        if channels_page else ""
    )

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
  --bg:#0C0A0B; --surface:#121011; --card:#171415; --card-h:#1E191B;
  --border:rgba(255,116,130,.14); --border-h:rgba(255,58,78,0.54);
  --blue:#7EA6F8; --cyan:#FF4D5E; --amber:#F6BD57; --green:#46C98B; --red:#FF6675;
  --text:#B8BDC7; --text-1:#F1F2F4; --text-2:#7D8491; --text-dim:#5F6672;
  --font-mono:'JetBrains Mono',monospace; --font-sans:'Inter','PingFang SC','Noto Sans SC','Microsoft YaHei',sans-serif;
  --radius:10px; --glow-blue:0 8px 24px rgba(0,0,0,0.28);
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  background:var(--bg); color:var(--text); font-family:var(--font-sans);
  background-image:
    linear-gradient(rgba(255,77,94,0.022) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,77,94,0.022) 1px, transparent 1px);
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
.topbar-actions {{ display:flex; align-items:center; gap:12px; }}
.status {{ display:flex; align-items:center; gap:8px; font-family:var(--font-mono); font-size:12px; color:var(--text-2); }}
.dot {{ width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.4; }} }}
.guide-open,.guide-close {{ min-height:44px; border:1px solid var(--border-h); border-radius:7px; padding:8px 14px; color:var(--text-1); background:rgba(255,0,51,.08); font:600 12px var(--font-sans); cursor:pointer; }}
.guide-open:hover,.guide-close:hover {{ color:var(--cyan); background:rgba(255,0,51,.15); }}
.guide-open:focus-visible,.guide-close:focus-visible {{ outline:2px solid var(--cyan); outline-offset:2px; }}
.guide-backdrop {{ position:fixed; inset:0; z-index:49; visibility:hidden; opacity:0; pointer-events:none; background:rgba(7,4,5,.72); backdrop-filter:blur(2px); transition:opacity .2s ease,visibility .2s ease; }}
.guide-backdrop.is-open {{ visibility:visible; opacity:1; pointer-events:auto; }}
.guide-drawer {{ position:fixed; z-index:50; inset:0 0 0 auto; width:min(560px,100vw); display:flex; flex-direction:column; color:var(--text); background:#111111; border-left:1px solid var(--border-h); box-shadow:-24px 0 64px rgba(0,0,0,.55); transform:translateX(100%); visibility:hidden; transition:transform .24s ease,visibility 0s linear .24s; }}
.guide-drawer.is-open {{ transform:translateX(0); visibility:visible; transition:transform .24s ease,visibility 0s; }}
.guide-head {{ flex:0 0 auto; display:flex; justify-content:space-between; align-items:flex-start; gap:20px; padding:22px 24px 18px; border-bottom:1px solid var(--border); background:linear-gradient(135deg,rgba(255,0,51,.16),rgba(255,77,103,.035)); }}
.guide-head h2 {{ margin:5px 0 4px; color:var(--text-1); font-size:22px; }}
.guide-head p {{ max-width:42ch; color:#94A3B8; font-size:12px; line-height:1.55; }}
.guide-close {{ flex:0 0 auto; background:var(--surface); }}
.guide-search {{ flex:0 0 auto; padding:14px 24px; border-bottom:1px solid var(--border); }}
.guide-search label {{ display:block; margin-bottom:6px; color:#94A3B8; font:10px var(--font-mono); }}
.guide-search input {{ width:100%; min-height:44px; border:1px solid var(--border); border-radius:7px; padding:9px 11px; color:var(--text-1); background:var(--surface); font:13px var(--font-sans); }}
.guide-search input:focus-visible {{ outline:2px solid var(--cyan); outline-offset:2px; }}
.guide-body {{ min-height:0; overflow:auto; overscroll-behavior:contain; padding:8px 24px 28px; scroll-padding-top:8px; }}
.guide-group {{ border-bottom:1px solid var(--border); }}
.guide-group > summary {{ min-height:52px; display:flex; align-items:center; gap:12px; list-style:none; color:var(--text-1); font-size:14px; font-weight:600; cursor:pointer; }}
.guide-group > summary::-webkit-details-marker {{ display:none; }}
.guide-group > summary::after {{ content:''; width:7px; height:7px; margin-left:auto; border-right:1.5px solid #64748B; border-bottom:1.5px solid #64748B; transform:rotate(45deg); transition:transform .2s ease; }}
.guide-group[open] > summary::after {{ transform:rotate(225deg); }}
.guide-group > summary:focus-visible {{ outline:2px solid var(--cyan); outline-offset:-2px; }}
.guide-intro {{ margin:-2px 0 10px; padding:10px 12px; border-radius:7px; color:#A3A3A3; background:rgba(255,0,51,.055); font-size:12px; line-height:1.6; }}
.guide-terms {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:0 0 14px; }}
.guide-term {{ padding:11px 12px; border:1px solid rgba(255,255,255,.08); border-radius:7px; background:rgba(0,0,0,.28); }}
.guide-term h4 {{ margin:0 0 5px; color:var(--text-1); font-size:12px; }}
.guide-term p {{ color:#A8B5C8; font-size:12px; line-height:1.62; }}
.guide-term strong {{ color:var(--text-1); }}
.guide-term code {{ padding:1px 4px; border-radius:4px; color:#FF8A9E; background:rgba(255,0,51,.09); font:10px var(--font-mono); }}
.guide-empty {{ padding:32px 12px; text-align:center; color:#64748B; font-size:12px; }}
body.guide-opened {{ overflow:hidden; }}

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
.kpi-card:hover {{ border-color:rgba(255,116,130,.30); box-shadow:var(--glow-blue); }}
.kpi-label {{ font-size:12px; color:var(--text-2); text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; }}
.kpi-value {{ font-family:var(--font-mono); font-size:26px; font-weight:700; color:#D9DCE2; }}
.kpi-card-primary {{ border-color:rgba(255,77,94,.30); background:linear-gradient(135deg,rgba(255,77,94,.10),var(--card) 62%); }}
.kpi-card-primary .kpi-value {{ color:#FF7180; }}
.kpi-card-accent .kpi-value {{ color:var(--text-1); }}
.kpi-card-accent {{ box-shadow:inset 0 2px 0 rgba(255,77,94,.30); }}

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
.quick-filter-btn.active {{ color:#FFF; border-color:var(--cyan); background:rgba(255,0,51,.18); }}
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
.table-card-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:10px; }}
.table-card-header h3 {{ min-width:0; color:var(--text-1); font-size:14px; font-weight:600; }}
.table-external-link {{
  flex:0 0 auto; min-height:44px; display:inline-flex; align-items:center; justify-content:center; gap:7px;
  padding:9px 14px; border:1px solid var(--border-h); border-radius:7px; color:var(--text-1);
  background:rgba(255,0,51,.08); font-size:12px; font-weight:600; line-height:1; text-decoration:none;
  cursor:pointer; touch-action:manipulation; transition:color .15s ease,background-color .15s ease,border-color .15s ease;
}}
.table-external-link:hover {{ color:var(--cyan); background:rgba(255,0,51,.15); }}
.table-external-link:active {{ background:rgba(255,0,51,.22); }}
.table-external-link:focus-visible {{ outline:2px solid var(--cyan); outline-offset:2px; }}
.table-external-link:disabled {{ opacity:0.35; cursor:not-allowed; }}
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
  font-family:var(--font-mono); background:rgba(255,0,51,0.11); color:#FF8A9E;
  border:1px solid rgba(255,0,51,0.24);
}}
.video-row {{ cursor:pointer; }}
.video-row:hover {{ background:rgba(255,0,51,0.055); }}
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
  font-size:11px; font-family:var(--font-mono); background:rgba(255,0,51,0.08);
  color:var(--cyan); border:1px solid var(--border); text-decoration:none; white-space:nowrap;
}}
.contact-pill:hover {{ background:rgba(255,0,51,0.16); border-color:var(--border-h); color:var(--cyan); }}

/* 竞品声量 tab: togglable platform chips, WoW deltas, clickable volume cells */
.platform-chip {{
  display:inline-flex; align-items:center; gap:6px; background:var(--surface); border:1px solid var(--border);
  border-radius:20px; padding:6px 14px; font-size:12px; font-family:var(--font-mono); color:var(--text-2);
  cursor:pointer; user-select:none; transition:all 0.15s;
}}
.platform-chip:hover {{ border-color:var(--border-h); color:var(--text-1); }}
.platform-chip.active {{ background:rgba(255,0,51,0.14); border-color:var(--border-h); color:#FF8A9E; }}
.vol-cell {{ cursor:pointer; font-family:var(--font-mono); }}
.vol-cell:hover {{ color:var(--cyan); text-decoration:underline; }}
.vol-cell.selected {{ background:rgba(255,0,51,0.09); border-radius:4px; }}
.wow-up {{ color:var(--green); }}
.wow-down {{ color:var(--red); }}
.wow-flag {{ font-weight:700; }}
.wow-flat {{ color:var(--text-2); }}
.concentration-flag {{
  display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px;
  font-family:var(--font-mono); background:rgba(239,68,68,0.12); color:var(--red);
  border:1px solid rgba(239,68,68,0.25);
}}
.volume-hero {{ display:flex; justify-content:space-between; align-items:flex-start; gap:28px; margin:4px 0 22px; padding:8px 2px; }}
.volume-hero h1 {{ color:var(--text-1); font-size:30px; line-height:1.2; margin:7px 0 10px; }}
.volume-hero p {{ max-width:900px; color:#94A3B8; line-height:1.65; font-size:14px; }}
.eyebrow {{ color:var(--cyan); font:600 11px var(--font-mono); letter-spacing:1.4px; }}
.scope-badge {{ flex:0 0 auto; border:1px solid var(--border-h); color:#FF8A9E; background:rgba(255,0,51,.09); border-radius:999px; padding:8px 12px; font:11px var(--font-mono); }}
.compact-card {{ padding-bottom:10px; }}
.global-filter-card {{ position:relative; border-color:rgba(255,116,130,.22); box-shadow:0 12px 32px rgba(0,0,0,.32); }}
.global-filter-grid {{ display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:10px; }}
.global-filter-grid label {{ display:flex; flex-direction:column; gap:6px; color:#94A3B8; font:11px var(--font-mono); }}
.global-filter-grid select,.global-filter-grid input {{ width:100%; min-height:44px; background:var(--surface); border:1px solid var(--border); border-radius:7px; padding:9px 10px; color:var(--text-1); font:13px var(--font-sans); }}
.global-filter-grid select:focus-visible,.global-filter-grid input:focus-visible,.filter-reset:focus-visible,.platform-chip:focus-visible {{ outline:2px solid var(--cyan); outline-offset:2px; }}
.filter-search-wide {{ grid-column:span 2; }}
.filter-reset {{ min-height:44px; padding:8px 14px; border-radius:7px; border:1px solid var(--border); color:#CBD5E1; background:var(--surface); cursor:pointer; }}
.filter-reset:hover {{ border-color:var(--border-h); color:var(--cyan); }}
.filter-reset:disabled {{ opacity:0.35; cursor:not-allowed; }}
.filter-platform-row {{ display:flex; align-items:flex-start; gap:14px; margin-top:14px; }}
.filter-platform-row > span {{ padding-top:12px; color:#94A3B8; font:11px var(--font-mono); }}
.filter-platform-row .chip-row {{ margin-bottom:0; }}
.filter-summary {{ display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-top:10px; }}
.section-head {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:14px; }}
.section-head h3 {{ margin-bottom:0; }}
.hint-inline,.kpi-sub {{ color:#64748B; font:11px/1.5 var(--font-mono); }}
.chart-filter-context {{ max-width:68%; color:#FF8A9E; font:10px/1.5 var(--font-mono); text-align:right; }}
.kpi-sub {{ margin-top:8px; }}
.kpi-text {{ font-size:20px; }}
.platform-chip {{ min-height:44px; }}
.matrix-row {{ cursor:pointer; }}
.matrix-row:hover {{ background:rgba(255,255,255,.025); }}
.matrix-row:focus-visible {{ outline:2px solid var(--cyan); outline-offset:-2px; }}
.signal-list {{ display:flex; flex-wrap:wrap; gap:4px; }}
.signal-pill {{ display:inline-flex; padding:2px 7px; border-radius:999px; border:1px solid var(--border); color:#94A3B8; font:10px var(--font-mono); }}
.method-pill {{ color:#FF8A9E; }}
.gr-significant {{ font-weight:700; color:var(--green); background:rgba(16,185,129,.08); }}
.gr-significant.negative {{ color:#FCA5A5; background:rgba(239,68,68,.08); }}
#volMatrix tbody td {{ color:#A4AAB5; }}
#volMatrix tbody td:first-child strong {{ color:var(--text-1); }}
#volMatrix .matrix-value {{ color:#D9DCE2; font:600 13px var(--font-mono); }}
#volMatrix td.metric-leader {{ background:linear-gradient(135deg,rgba(255,77,94,.075),rgba(255,77,94,.015)); }}
#volMatrix td.metric-leader .matrix-value {{ color:#FF7A88; }}
#volMatrix td.gr-significant {{ color:var(--green); }}
#volMatrix td.gr-significant.negative {{ color:#FF8A96; }}
#volTableBody tr:not(:first-child) td {{ color:#8D939E; }}
#volTableBody tr:first-child td {{ color:#E3E5E9; background:rgba(255,77,94,.045); }}
#volTableBody tr:first-child td:first-child {{ box-shadow:inset 3px 0 0 var(--cyan); }}
.heatmap-wrap {{ max-width:100%; overflow:auto; scrollbar-gutter:stable; }}
.heatmap {{ display:grid; gap:3px; min-width:560px; font:11px var(--font-mono); }}
.heatmap > div {{ min-height:34px; display:flex; align-items:center; justify-content:center; border:1px solid rgba(148,163,184,.08); border-radius:4px; padding:5px; color:var(--text-1); }}
.heatmap .heat-label {{ justify-content:flex-start; color:#94A3B8; background:transparent !important; border-color:transparent; }}
.heatmap .heat-col-label {{ min-height:52px; color:#94A3B8; background:transparent; border-color:transparent; text-align:center; line-height:1.35; white-space:normal; overflow-wrap:anywhere; word-break:normal; }}
.detail-table {{ width:100%; margin-top:10px; }}
.detail-table td {{ white-space:normal; vertical-align:top; }}
.detail-title-link {{ min-height:44px; display:inline-flex; align-items:flex-start; color:var(--text-1); text-decoration:none; line-height:1.5; }}
.detail-title-link:hover {{ color:var(--cyan); text-decoration:underline; text-underline-offset:3px; }}
.detail-title-link:focus-visible {{ border-radius:3px; outline:2px solid var(--cyan); outline-offset:3px; }}
.detail-title-link.is-missing {{ color:#94A3B8; font-style:italic; }}
.detail-tags {{ display:flex; flex-wrap:wrap; gap:4px; min-width:150px; }}
.tag-evidence {{ margin-top:7px; min-width:230px; border-top:1px solid var(--border); }}
.tag-evidence > summary {{ min-height:44px; display:flex; align-items:center; gap:8px; list-style:none; color:#FF8A9E; font:500 11px var(--font-mono); cursor:pointer; }}
.tag-evidence > summary::-webkit-details-marker {{ display:none; }}
.tag-evidence > summary::before {{ content:''; width:6px; height:6px; flex:0 0 auto; border-right:1.5px solid currentColor; border-bottom:1.5px solid currentColor; transform:rotate(-45deg); transition:transform .2s ease; }}
.tag-evidence[open] > summary::before {{ transform:rotate(45deg); }}
.tag-evidence > summary:hover {{ color:var(--cyan); }}
.tag-evidence > summary:focus-visible {{ outline:2px solid var(--cyan); outline-offset:-2px; }}
.tag-evidence-body {{ display:grid; gap:9px; padding:0 0 10px 14px; }}
.tag-evidence-group {{ display:grid; gap:5px; }}
.tag-evidence-group > b {{ color:#94A3B8; font:600 10px var(--font-mono); }}
.tag-evidence-item {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:7px; align-items:start; color:#94A3B8; font-size:11px; line-height:1.55; }}
.tag-evidence-item code {{ padding:1px 4px; border-radius:4px; color:#FF8A9E; background:rgba(255,0,51,.08); font:9px/1.5 var(--font-mono); }}
.ai-insight {{
  position:relative; overflow:hidden; padding:18px 20px;
  background:linear-gradient(135deg,rgba(255,77,94,.075),rgba(255,77,94,.018) 48%,var(--card));
  border-color:rgba(255,116,130,.20);
}}
.ai-insight::before {{ content:''; position:absolute; inset:0 auto 0 0; width:3px; background:var(--cyan); }}
.ai-insight-head {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:12px; }}
.ai-insight-head > div:first-child {{ min-width:0; }}
.ai-insight-head h3 {{ font-size:18px; font-weight:600; line-height:1.5; margin:5px 0 0; max-width:980px; letter-spacing:-.01em; }}
.ai-label {{ font:600 11px var(--font-mono); letter-spacing:1.2px; color:var(--cyan); }}
.ai-period {{ flex:0 0 auto; display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; font:10px/1.4 var(--font-mono); color:#938C90; }}
.ai-period span {{ padding:5px 8px; border:1px solid var(--border); border-radius:999px; background:rgba(12,8,9,.48); }}
.ai-summary {{ border:1px solid var(--border); border-radius:8px; background:rgba(12,8,9,.42); }}
.ai-insight ul {{ padding-left:18px; display:grid; gap:8px; }}
.ai-insight li {{ line-height:1.65; color:var(--text); }}
.ai-brief-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:1px; overflow:hidden; }}
.ai-insight-item {{ min-width:0; background:rgba(20,15,17,.64); }}
.ai-insight-item + .ai-insight-item {{ border-left:1px solid var(--border); }}
.ai-insight-item[open] {{ grid-column:1/-1; border-left:0; border-top:1px solid var(--border); background:rgba(25,18,20,.90); }}
.ai-insight-item summary,.ai-more summary,.ai-caveat summary {{ list-style:none; cursor:pointer; user-select:none; }}
.ai-insight-item summary::-webkit-details-marker,.ai-more summary::-webkit-details-marker,.ai-caveat summary::-webkit-details-marker {{ display:none; }}
.ai-insight-item summary {{ min-height:76px; display:grid; grid-template-columns:auto 1fr auto; align-items:start; gap:10px; padding:13px 14px; }}
.ai-insight-item summary:hover {{ background:rgba(255,0,51,.045); }}
.ai-insight-item summary:focus-visible,.ai-more summary:focus-visible,.ai-caveat summary:focus-visible {{ outline:2px solid var(--cyan); outline-offset:-2px; }}
.ai-insight-index {{ color:var(--cyan); font:600 10px/1.55 var(--font-mono); }}
.ai-insight-preview {{ display:-webkit-box; overflow:hidden; -webkit-line-clamp:2; -webkit-box-orient:vertical; color:var(--text-1); font-size:13px; line-height:1.55; }}
.ai-expand-label {{ color:#7F777C; font:10px/1.55 var(--font-mono); }}
.ai-insight-item[open] .ai-expand-label {{ font-size:0; }}
.ai-insight-item[open] .ai-expand-label::after {{ content:'收起'; font-size:10px; }}
.ai-insight-detail {{ max-width:78ch; margin:0 14px 15px 44px; color:#C0BABD; font-size:13px; line-height:1.72; }}
.ai-platform-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:14px; }}
.ai-platform {{ padding:16px 18px; background:rgba(22,16,18,.78); border:1px solid var(--border); border-radius:8px; }}
.ai-platform h4,.ai-foot-grid h4 {{ color:var(--text-1); font-size:13px; margin-bottom:10px; }}
.ai-platform p {{ color:var(--text); line-height:1.75; }}
.ai-more {{ margin-top:9px; border-top:1px solid var(--border); }}
.ai-more > summary {{ min-height:44px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:8px 10px; color:var(--text-1); font-size:12px; font-weight:600; }}
.ai-more > summary::after,.ai-caveat > summary::after {{ content:''; width:7px; height:7px; flex:0 0 auto; border-right:1.5px solid #64748B; border-bottom:1.5px solid #64748B; transform:rotate(45deg); transition:transform .2s ease; }}
.ai-more[open] > summary::after,.ai-caveat[open] > summary::after {{ transform:rotate(225deg); }}
.ai-more-meta {{ margin-left:auto; color:#7F777C; font:10px var(--font-mono); }}
.ai-caveat {{ margin-top:2px; color:#9B9498; font-size:11px; border-top:1px solid var(--border); }}
.ai-caveat > summary {{ min-height:44px; display:flex; align-items:center; gap:10px; padding:8px 10px; font:500 11px var(--font-mono); }}
.ai-caveat > summary::after {{ margin-left:auto; }}
.ai-caveat > p {{ max-width:100ch; padding:0 10px 12px; line-height:1.65; }}
.ai-foot-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:1px; margin-top:0; border-top:1px solid var(--border); background:var(--border); }}
.ai-foot-grid section {{ padding:14px 16px; background:rgba(25,18,20,.90); }}
.ai-foot-grid p,.ai-foot-grid li {{ font-size:12px; line-height:1.68; }}
.ai-empty {{ padding:28px; text-align:center; color:#9B9498; font:12px/1.7 var(--font-mono); }}
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
  .ai-period {{ justify-content:flex-start; }}
  .global-filter-grid {{ grid-template-columns:repeat(3,minmax(0,1fr)); }}
  .global-filter-card {{ position:relative; top:auto; }}
}}
@media (max-width:600px) {{
  .container {{ padding:12px; }}
  .kpi-grid, .kpi-grid.kpi-grid-5 {{ grid-template-columns:1fr; }}
  .ai-insight {{ padding:16px; }}
  .ai-insight-head h3 {{ font-size:16px; }}
  .ai-brief-grid {{ grid-template-columns:1fr; }}
  .ai-insight-item + .ai-insight-item {{ border-left:0; border-top:1px solid var(--border); }}
  .ai-insight-detail {{ margin-left:14px; }}
  .filter-bar {{ align-items:stretch; }}
  .filter-bar label {{ margin-top:4px; }}
  .volume-hero {{ flex-direction:column; }}
  .volume-hero h1 {{ font-size:24px; }}
  .scope-badge {{ align-self:flex-start; }}
  .global-filter-grid {{ grid-template-columns:1fr 1fr; }}
  .filter-search-wide {{ grid-column:1 / -1; }}
  .filter-platform-row {{ flex-direction:column; gap:4px; }}
  .filter-platform-row > span {{ padding-top:0; }}
  .topbar {{ padding:0 12px; }}
  .status {{ display:none; }}
  .guide-open {{ padding-inline:11px; }}
  .guide-head,.guide-search {{ padding-left:16px; padding-right:16px; }}
  .guide-body {{ padding-left:16px; padding-right:16px; }}
  .guide-terms {{ grid-template-columns:1fr; }}
}}
@media (prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ scroll-behavior:auto !important; animation:none !important; transition:none !important; }} }}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">PROMO<span>LEADS</span> · 看板</div>
  <div class="topbar-actions">{guide_button_html}<div class="status"><span class="dot"></span>更新于 {run_date}</div></div>
</div>

{guide_drawer_html}

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
      <div class="table-card-header">
        <h3>频道明细（按频道去重合并，含未识别推广平台的频道）</h3>
        <div style="display:flex;align-items:center;gap:10px">
          <button type="button" class="table-external-link" id="chExportDetailBtn" disabled title="下载当前筛选条件下呈现的全部频道数据">
            <svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
            <span>下载详情 CSV</span>
          </button>
          {channels_lark_link_html}
        </div>
      </div>
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
    <header class="volume-hero">
      <div><span class="eyebrow">COMPETITIVE YOUTUBE PERFORMANCE</span>
        <h1>竞品 YouTube 推广表现</h1>
        <p>数据为系统在指定期间内识别到的 YouTube 竞品推广视频。视频表现为发布次日首次抓取时的快照，不代表完整生命周期播放表现。</p>
      </div>
      <div class="scope-badge">当前系统覆盖范围 · 首采快照</div>
    </header>

    <section class="card global-filter-card" aria-label="全局筛选器">
      <div class="section-head"><h3>全局筛选器</h3><button type="button" class="filter-reset" id="volFilterReset">重置筛选</button></div>
      <div class="global-filter-grid">
        <label>快捷期间<select id="volWindowSize" aria-label="快捷期间"><option value="7">最近 7 天</option><option value="14">最近 14 天</option><option value="30">最近 30 天</option><option value="custom">自定义</option></select></label>
        <label>趋势期间数<select id="volWindowCount" aria-label="趋势期间数"><option value="4">4</option><option value="6">6</option><option value="8">8</option></select></label>
        <label>开始日期<input type="date" id="volDateFrom" aria-label="开始日期"></label>
        <label>结束日期<input type="date" id="volDateTo" aria-label="结束日期"></label>
        <label>市场<select id="volMarketFilter" aria-label="市场"><option value="">全部市场</option></select></label>
        <label>语言<select id="volLanguageFilter" aria-label="语言"><option value="">全部语言</option></select></label>
        <label>推广方式<select id="volMethodFilter" aria-label="推广方式"><option value="">全部方式</option></select></label>
        <label>内容主题<select id="volTopicFilter" aria-label="内容主题"><option value="">全部主题</option></select></label>
        <label class="filter-search-wide">账号 / 标题搜索<input type="search" id="volTextFilter" placeholder="输入账号或标题" autocomplete="off"></label>
      </div>
      <div class="filter-platform-row"><span>竞品</span><div class="chip-row" id="volPlatformChips" role="group" aria-label="选择竞品"></div></div>
      <div class="filter-summary"><span class="table-meta" id="volMeta"></span><span class="table-meta" id="volFilterMeta" aria-live="polite"></span></div>
    </section>

    <section class="card ai-insight" aria-label="每周 AI Winsight">
      {insight_html}
    </section>

    <div class="kpi-grid kpi-grid-5" id="volKpis">
      <div class="kpi-card kpi-card-primary"><div class="kpi-label">核心竞品推广视频</div><div class="kpi-value" id="volKpiVideos">0</div><div class="kpi-sub" id="volKpiWow">—</div></div>
      <div class="kpi-card kpi-card-accent"><div class="kpi-label">Promotion Share 最高</div><div class="kpi-value kpi-text" id="volKpiShare">—</div><div class="kpi-sub">系统覆盖内推广份额</div></div>
      <div class="kpi-card"><div class="kpi-label">独立推广账号</div><div class="kpi-value" id="volKpiAccounts">0</div><div class="kpi-sub">跨竞品去重账号</div></div>
      <div class="kpi-card"><div class="kpi-label">早期高表现视频</div><div class="kpi-value" id="volKpiEarly">0</div><div class="kpi-sub">同观察时长区间前 20%</div></div>
      <div class="kpi-card kpi-card-accent"><div class="kpi-label">Promotion Share 提升最大</div><div class="kpi-value kpi-text" id="volKpiMomentum">—</div><div class="kpi-sub">较上一同长期间的份额变化</div></div>
    </div>

    <div class="table-card">
      <div class="section-head"><h3>竞品表现矩阵</h3><span class="hint-inline" id="volMatrixPeriod">点击竞品行展开视频明细</span></div>
      <div class="table-wrap">
        <table id="volMatrix"><thead><tr>
          <th>竞品</th><th>推广视频</th><th>Promotion Share</th><th title="Growth Rate：与上一同长期间相比的视频数增长率">GR</th><th>独立账号</th><th>新观察账号</th><th>Top1</th><th>首采播放中位数</th><th>早期高表现</th><th>主要推广方式</th><th>核心主题</th><th>信号</th>
        </tr></thead><tbody id="volMatrixBody"></tbody></table>
      </div>
      <div class="hint">GR =（指定期间视频数 − 上一同长期间视频数）÷ 上一同长期间视频数；前期为 0 时显示 NEW，不计算百分比。仅在 |GR| ≥ 30% 且视频数绝对变化 ≥ 3 时显著高亮。</div>
    </div>

    <div class="table-card" id="volWindowOverview">
      <div class="section-head"><h3>分期规模与份额概览</h3><span class="hint-inline">按指定期间长度向前拆分；点击数字展开视频明细</span></div>
      <div class="table-wrap">
        <table id="volTable"><thead><tr id="volTableHead"></tr></thead><tbody id="volTableBody"></tbody></table>
      </div>
    </div>

    <div class="chart-grid">
      <div class="card">
        <h3>去重推广视频趋势</h3>
        <div class="chart-wrap"><canvas id="cVolTrend"></canvas></div>
      </div>
      <div class="card">
        <h3>Promotion Share 趋势</h3>
        <div class="chart-wrap"><canvas id="cVolShareTrend"></canvas></div>
      </div>
    </div>

    <div class="chart-grid">
      <div class="card">
        <h3>推广规模 × 独立内容结构</h3>
        <div class="chart-wrap"><canvas id="cVolBrandBubble" aria-label="推广视频数与品牌导向占比气泡图"></canvas></div>
        <div class="hint">X：去重推广视频数 · Y：brand_led 占比 · Size：独立账号数<br><span id="volBrandBubbleScale"></span></div>
      </div>
      <div class="card">
        <h3>推广规模 × 早期观看信号</h3>
        <div class="chart-wrap"><canvas id="cVolViewBubble" aria-label="推广视频数与首采播放量中位数气泡图"></canvas></div>
        <div class="hint">X：去重推广视频数 · Y：首采播放量中位数 · Size：独立账号数<br><span id="volViewBubbleScale"></span> · 首采播放仅作为早期观看信号。</div>
      </div>
    </div>

    <div class="chart-grid">
      <div class="card"><div class="section-head"><h3>竞品 × 内容主题</h3><span class="chart-filter-context" id="volTopicContext"></span></div><div class="heatmap-wrap" id="volTopicHeatmap"></div><div class="hint">一条视频可命中多个主题；选择主题后仅展示该主题在筛选后样本中的数量。</div></div>
      <div class="card"><div class="section-head"><h3>竞品 × 推广方式结构</h3><span class="chart-filter-context" id="volMethodContext"></span></div><div class="chart-wrap"><canvas id="cVolMethods"></canvas></div><div class="hint">brand_led 与 description_only 用于区分独立品牌导向和 Description 挂链；选择推广方式后仅展示该方式。</div></div>
    </div>

    <div class="chart-grid">
      <div class="card"><div class="section-head"><h3>竞品 × 语言 / 市场</h3><span class="chart-filter-context" id="volMarketContext"></span></div><div class="heatmap-wrap" id="volMarketHeatmap"></div><div class="hint">语言推断不等于真实用户市场；市场优先使用频道国家信息。选择市场或语言后仅展示对应维度。</div></div>
      <div class="card"><div class="section-head"><h3>账号结构对比</h3><span class="chart-filter-context" id="volAccountContext"></span></div><div class="chart-wrap"><canvas id="cVolAccounts"></canvas></div><div class="hint">展示当前筛选样本的 Top1 / Top3 集中度，以及新观察与连续推广账号。</div></div>
    </div>

    <div id="volDrillPanel" class="yt-detail-panel" tabindex="-1"></div>
  </div>

</div>

<script>
const COLORS = {colors_js};
const ALL_LEADS = {all_js};
const ALL_CHANNELS = {channels_js};
const VOLUME_DATA = {volume_payload_js};
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

// CSV 字段转义：值含引号/逗号/换行时加引号并转义内部引号。放在页面共享的
// 预处理区（channels/main/volume 三个页面裁剪脚本时都保留这段代码之前的内容），
// 这样各页各自的 CSV 导出函数都能直接调用，不必各自重复实现。
function csvFieldV2(value) {{
  const str = value === null || value === undefined ? '' : String(value);
  return /["\\n,]/.test(str) ? `"${{str.replace(/"/g, '""')}}"` : str;
}}

function videoDetailCell(v) {{
  return `<div class="detail-item">
    <span class="yt-detail-meta">${{v.published_at ? v.published_at.slice(0, 10) : ''}}</span>
    <span class="yt-detail-meta">${{(v.view_count || 0).toLocaleString()}} 次观看</span>
    ${{linkCell(v.video_url)}}${{v.video_title ? ` — ${{truncate(v.video_title, 60)}}` : ''}}
  </div>`;
}}

// 导出当前筛选条件下呈现的全部频道（不要求先展开行；channels 传入的是
// 过滤后的完整列表，与筛选条件、搜索框实时同步）。字段与频道表格列一一对应：
// 频道 / 市场 / 粉丝数 / 频道规模 / 推广平台 / 联系方式 / 本次抓取 / 活跃时间。
function exportChannelsDetailCsv(channels) {{
  if (!channels || !channels.length) return;
  const headers = ['频道名称','频道链接','国家','语言','市场','粉丝数','频道规模-累计播放','频道规模-累计视频数','推广平台','联系方式','本次抓取-观看数','本次抓取-视频数','活跃时间','最近抓取日期','首次抓取日期'];
  const lines = [headers.map(csvFieldV2).join(',')];
  channels.forEach(r => {{
    lines.push([
      r.account_name, r.profile_url, r.country, r.language, r.market, r.followers,
      r.channel_view_cnt, r.channel_video_cnt, r.promo_platform, r.contact,
      r.total_views, (r.videos || []).length, r.latest_promo_date, r.last_crawled_date, r.first_seen_date
    ].map(csvFieldV2).join(','));
  }});
  const blob = new Blob(['﻿' + lines.join('\\r\\n')], {{type:'text/csv;charset=utf-8;'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `channels-detail-${{new Date().toISOString().slice(0,10)}}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
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
    if (els.exportBtn) document.getElementById(els.exportBtn).disabled = filtered.length === 0;

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
    getFilteredChannels() {{ return filtered; }},
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
  elIds: {{ tbody: 'tbody-channels', meta: 'meta-channels', pageInfo: 'pageInfo-channels', prevBtn: 'prev-channels', nextBtn: 'next-channels', empty: 'empty-channels', theadRow: 'thead-channels', exportBtn: 'chExportDetailBtn' }},
}});
document.getElementById('chExportDetailBtn').addEventListener('click', () => exportChannelsDetailCsv(channelsTable.getFilteredChannels()));

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
  type: 'scatter', data: {{ datasets: [{{ label: '频道', data: [], pointRadius: 4, pointHoverRadius: 7, borderWidth: 0, hoverBorderWidth: 0 }}] }},
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
    label: a.market, backgroundColor: marketColor.get(a.market) || COLORS[0], borderWidth: 0, hoverBorderWidth: 0,
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
const LATEST_CHANNEL_UPDATE_DATE = ALL_CHANNELS.reduce(
  (latest, channel) => channel.last_crawled_date > latest ? channel.last_crawled_date : latest,
  '',
);
function resetChannelFiltersToDefaults() {{
  ['chMarketFilter', 'chFollowerFilter', 'chMinViews', 'search-channels'].forEach(
    id => document.getElementById(id).value = '',
  );
  document.getElementById('chDateField').value = 'last_crawled_date';
  document.getElementById('chDateFrom').value = LATEST_CHANNEL_UPDATE_DATE;
  document.getElementById('chDateTo').value = LATEST_CHANNEL_UPDATE_DATE;
}}
document.getElementById('chResetFilters').addEventListener('click', () => {{
  resetChannelFiltersToDefaults();
  refreshChannels();
}});
resetChannelFiltersToDefaults();
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
  data: {{ labels: [], datasets: [{{ label: '新增推广视频', data: [], borderColor: COLORS[1], backgroundColor: 'rgba(255,0,51,0.12)', fill: true, tension: 0.3, pointRadius: 2 }}] }},
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
  data: {{ labels: [], datasets: [{{ data: [], backgroundColor: COLORS, borderColor: '#161616', borderWidth: 2 }}] }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'right', labels: {{ color: '#CBD5E1', font: {{ size: 11 }}, boxWidth: 12 }} }} }},
  }},
}});

const platformByYtChart = new Chart(document.getElementById('cPlatformByYt'), {{
  type: 'pie',
  data: {{ labels: [], datasets: [{{ data: [], backgroundColor: COLORS, borderColor: '#161616', borderWidth: 2 }}] }},
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
      {{ type: 'bar', label: '视频数量', data: [], yAxisID: 'yCount', backgroundColor: 'rgba(255,0,51,.55)', borderColor: '#FF3355', borderWidth: 1, borderRadius: 3 }},
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
      yCount: {{ position: 'left', beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.05)' }}, ticks: {{ color: '#FF8A9E', precision: 0 }}, title: {{ display: true, text: '视频数', color: '#FF8A9E' }} }},
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

// ── Competitive YouTube Performance v2 (precomputed Python metrics) ─────
const volGuideOpen=document.getElementById('volGuideOpen');
const volGuideClose=document.getElementById('volGuideClose');
const volGuideDrawer=document.getElementById('volGuideDrawer');
const volGuideBackdrop=document.getElementById('volGuideBackdrop');
const volGuideSearch=document.getElementById('volGuideSearch');
let volGuideReturnFocus=null;
function setVolGuide(open) {{
  if(!volGuideDrawer)return;
  volGuideDrawer.classList.toggle('is-open',open);volGuideBackdrop.classList.toggle('is-open',open);
  volGuideDrawer.setAttribute('aria-hidden',String(!open));volGuideBackdrop.setAttribute('aria-hidden',String(!open));
  volGuideOpen.setAttribute('aria-expanded',String(open));document.body.classList.toggle('guide-opened',open);
  if(open){{volGuideReturnFocus=document.activeElement;requestAnimationFrame(()=>requestAnimationFrame(()=>volGuideClose.focus()));}}
  else if(volGuideReturnFocus){{volGuideReturnFocus.focus();}}
}}
volGuideOpen?.addEventListener('click',()=>setVolGuide(true));
volGuideClose?.addEventListener('click',()=>setVolGuide(false));
volGuideBackdrop?.addEventListener('click',()=>setVolGuide(false));
document.addEventListener('keydown',event=>{{
  if(!volGuideDrawer?.classList.contains('is-open'))return;
  if(event.key==='Escape'){{event.preventDefault();setVolGuide(false);return;}}
  if(event.key!=='Tab')return;
  const focusable=[...volGuideDrawer.querySelectorAll('button,input,summary,[href],[tabindex]:not([tabindex="-1"])')].filter(node=>!node.disabled&&node.offsetParent!==null);
  if(!focusable.length)return;
  const first=focusable[0],last=focusable.at(-1);
  if(event.shiftKey&&document.activeElement===first){{event.preventDefault();last.focus();}}
  else if(!event.shiftKey&&document.activeElement===last){{event.preventDefault();first.focus();}}
}});
volGuideSearch?.addEventListener('input',()=>{{
  const query=volGuideSearch.value.trim().toLowerCase();let visible=0;
  volGuideDrawer.querySelectorAll('.guide-group').forEach(group=>{{
    let groupVisible=0;
    group.querySelectorAll('.guide-term').forEach(term=>{{
      const match=!query||term.textContent.toLowerCase().includes(query);term.hidden=!match;if(match){{groupVisible++;visible++;}}
    }});
    const intro=group.querySelector('.guide-intro');
    const introMatch=Boolean(query&&intro?.textContent.toLowerCase().includes(query));
    if(introMatch){{groupVisible++;visible++;}}
    group.hidden=Boolean(query&&!groupVisible);if(query&&groupVisible)group.open=true;
  }});
  document.getElementById('volGuideEmpty').hidden=Boolean(visible||!query);
}});

const volSelectedV2 = new Set(CORE_COMPETITOR_PLATFORMS);
let volChartsV2 = {{}};
let volDrillStateV2 = null;
let volDrillRowsV2 = [];
const fmtNumV2 = value => value === null || value === undefined ? '—' : Number(value).toLocaleString();
const fmtPctV2 = value => value === null || value === undefined ? '—' : `${{(Number(value) * 100).toFixed(1)}}%`;
const escV2 = value => String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const topKeyV2 = values => Object.entries(values || {{}}).sort((a,b) => b[1] - a[1])[0]?.[0] || '—';
const colorV2 = platform => COLORS[Math.max(0, CORE_COMPETITOR_PLATFORMS.indexOf(platform)) % COLORS.length];
const FIRST_ACCOUNT_DATE_V2 = new Map();
(VOLUME_DATA.facts || []).forEach(row => {{
  if (!row.account_key || !row.date) return;
  const key = `${{row.platform}}||${{row.account_key}}`;
  if (!FIRST_ACCOUNT_DATE_V2.has(key) || row.date < FIRST_ACCOUNT_DATE_V2.get(key)) FIRST_ACCOUNT_DATE_V2.set(key,row.date);
}});

function activeVolFilterV2(row) {{
  const market = document.getElementById('volMarketFilter').value;
  const language = document.getElementById('volLanguageFilter').value;
  const method = document.getElementById('volMethodFilter').value;
  const topic = document.getElementById('volTopicFilter').value;
  const query = document.getElementById('volTextFilter').value.trim().toLowerCase();
  return (!market || row.market === market) && (!language || row.language === language) &&
    (!method || (row.promotion_methods || []).includes(method)) &&
    (!topic || (row.content_topics || []).includes(topic)) &&
    (!query || `${{row.account || ''}} ${{row.title || ''}}`.toLowerCase().includes(query));
}}

function initVolFiltersV2() {{
  const facts = VOLUME_DATA.facts || [];
  const fill = (id, values) => {{
    const select = document.getElementById(id);
    const first = select.options[0].outerHTML;
    select.innerHTML = first + [...new Set(values.filter(Boolean))].sort().map(value => `<option value="${{escV2(value)}}">${{escV2(value)}}</option>`).join('');
  }};
  fill('volMarketFilter', facts.map(row => row.market));
  fill('volLanguageFilter', facts.map(row => row.language));
  fill('volMethodFilter', facts.flatMap(row => row.promotion_methods || []));
  fill('volTopicFilter', facts.flatMap(row => row.content_topics || []));
  const defaultWindow = (VOLUME_DATA.windows?.['7'] || [])[0]?.window;
  if (defaultWindow) {{
    document.getElementById('volDateFrom').value = defaultWindow.start;
    document.getElementById('volDateTo').value = defaultWindow.end;
  }}
}}

const counterV2 = (rows,key) => {{
  const result={{}};
  rows.forEach(row => {{ const values=Array.isArray(row[key])?row[key]:[row[key]]; values.filter(Boolean).forEach(value=>result[value]=(result[value]||0)+1); }});
  return result;
}};
const medianV2 = values => {{ if(!values.length)return null; const sorted=values.slice().sort((a,b)=>a-b),mid=Math.floor(sorted.length/2); return sorted.length%2?sorted[mid]:(sorted[mid-1]+sorted[mid])/2; }};
const coverageV2 = (rows,test) => ({{covered:rows.filter(test).length,total:rows.length,rate:rows.length?rows.filter(test).length/rows.length:null}});

function earlyKeysV2(rows) {{
  const groups=new Map(),result=new Set();
  rows.forEach(row=>{{if(row.observation_bucket==='Unknown'||row.view_count==null)return;if(!groups.has(row.observation_bucket))groups.set(row.observation_bucket,[]);groups.get(row.observation_bucket).push(row);}});
  groups.forEach(items=>{{if(items.length<5)return;items.sort((a,b)=>b.view_count-a.view_count).slice(0,Math.ceil(items.length*.2)).forEach(row=>result.add(`${{row.platform}}||${{row.video_url}}`));}});
  return result;
}}

function metricFromFactsV2(platform,rows,previousRows,denominator,previousDenominator,early,currentStart) {{
  const accounts=counterV2(rows,'account_key'),ordered=Object.entries(accounts).sort((a,b)=>b[1]-a[1]);
  const previousAccounts=new Set(previousRows.map(row=>row.account_key).filter(Boolean));
  const currentAccounts=new Set(rows.map(row=>row.account_key).filter(Boolean));
  const methods=counterV2(rows,'promotion_methods'),topics=counterV2(rows,'content_topics');
  const previousMethods=counterV2(previousRows,'promotion_methods'),previousTopics=counterV2(previousRows,'content_topics');
  const views=rows.map(row=>row.view_count).filter(value=>value!=null);
  const engagement=rows.filter(row=>row.initial_engagement_rate!=null);
  const top1=ordered[0]?.[1]||0,top3=ordered.slice(0,3).reduce((sum,item)=>sum+item[1],0),count=rows.length;
  const top1Share=count?top1/count:null;
  const concentration=count<3?'样本不足':top1Share>=.6?'高度集中':top1Share>=.4?'相对集中':'相对分散';
  const methodShares=Object.fromEntries(Object.entries(methods).map(([key,value])=>[key,value/count]));
  const previousMethodShares=Object.fromEntries(Object.entries(previousMethods).map(([key,value])=>[key,value/(previousRows.length||1)]));
  const topicShares=Object.fromEntries(Object.entries(topics).map(([key,value])=>[key,value/count]));
  const previousTopicShares=Object.fromEntries(Object.entries(previousTopics).map(([key,value])=>[key,value/(previousRows.length||1)]));
  const change=count-previousRows.length,pct=previousRows.length?change/previousRows.length*100:null;
  const signals=[];
  if(count<3)signals.push('数据不足');
  else {{
    if(['高度集中','相对集中'].includes(concentration))signals.push('集中铺量');else if(currentAccounts.size>=4&&top1Share<.4)signals.push('长尾扩张');
    if((methodShares.description_only||0)>=.6)signals.push('Description挂链为主');
    if((methodShares.multi_platform||0)>=.5)signals.push('Multi-platform为主');
    if((methodShares.official||0)>=.5)signals.push('官方内容驱动');
    if((topics.Activity||0)-(previousTopics.Activity||0)>=3)signals.push('活动内容增长');
    if((topics.Product||0)-(previousTopics.Product||0)>=3)signals.push('产品内容增长');
  }}
  return {{
    platform,promotion_records:rows.reduce((sum,row)=>sum+(row.promotion_record_count||0),0),videos:count,
    promotion_share:denominator?count/denominator:null,previous_promotion_share:previousDenominator?previousRows.length/previousDenominator:null,
    accounts:currentAccounts.size,new_observed_accounts:[...currentAccounts].filter(key=>FIRST_ACCOUNT_DATE_V2.get(`${{platform}}||${{key}}`)>=currentStart).length,
    continuous_accounts:[...currentAccounts].filter(key=>previousAccounts.has(key)).length,top1_share:top1Share,top3_share:count?top3/count:null,
    concentration_signal:concentration,videos_per_account:currentAccounts.size?count/currentAccounts.size:null,
    method_counts:methods,method_shares:methodShares,topic_counts:topics,topic_shares:topicShares,
    language_counts:counterV2(rows,'language'),market_counts:counterV2(rows,'market'),first_views_median:medianV2(views),
    early_high_performers:rows.filter(row=>early.has(`${{platform}}||${{row.video_url}}`)).length,
    initial_engagement_rate:engagement.length&&engagement.reduce((s,r)=>s+(r.view_count||0),0)>0?engagement.reduce((s,r)=>s+(r.like_count+r.comment_count),0)/engagement.reduce((s,r)=>s+r.view_count,0):null,
    growth:{{current:count,previous:previousRows.length,change,percent:pct,significant:pct!=null&&Math.abs(pct)>=30&&Math.abs(change)>=3}},
    previous_structure:{{method_shares:previousMethodShares,topic_shares:previousTopicShares}},signals:(signals.length?signals:['暂无显著结构信号']).slice(0,3),
    coverage:{{video_details:coverageV2(rows,r=>r.detail_available),first_views:coverageV2(rows,r=>r.view_count!=null),likes_comments:coverageV2(rows,r=>r.like_count!=null&&r.comment_count!=null),late_snapshot:coverageV2(rows,r=>r.backfill_captured_at),promotion_method:coverageV2(rows,r=>r.promotion_methods?.length),content_topic:coverageV2(rows,r=>r.detail_available&&r.content_topics?.length),market:coverageV2(rows,r=>r.market),language:coverageV2(rows,r=>r.language),observation_age:coverageV2(rows,r=>r.observation_bucket!=='Unknown')}}
  }};
}}

function filteredSummaryV2(base) {{
  const facts=(VOLUME_DATA.facts||[]).filter(activeVolFilterV2);
  const current=facts.filter(row=>row.date>=base.window.start&&row.date<=base.window.end);
  const previous=facts.filter(row=>row.date>=base.previous_window.start&&row.date<=base.previous_window.end);
  const early=earlyKeysV2(current);
  return {{...base,total_videos:current.length,early_high_keys:[...early].map(key=>key.split('||')),platforms:CORE_COMPETITOR_PLATFORMS.map(platform=>metricFromFactsV2(platform,current.filter(row=>row.platform===platform),previous.filter(row=>row.platform===platform),current.length,previous.length,early,base.window.start))}};
}}

function shiftDateV2(value,days) {{ const d=new Date(`${{value}}T00:00:00Z`);d.setUTCDate(d.getUTCDate()+days);return d.toISOString().slice(0,10); }}
function selectedSpanV2(start,end) {{ return Math.round((new Date(`${{end}}T00:00:00Z`)-new Date(`${{start}}T00:00:00Z`))/86400000)+1; }}
function customWindowsV2(start,end,count) {{
  const span=selectedSpanV2(start,end);
  return Array.from({{length:count}},(_,index)=>{{
    const windowEnd=shiftDateV2(end,-index*span),windowStart=shiftDateV2(windowEnd,-span+1),previousEnd=shiftDateV2(windowStart,-1),previousStart=shiftDateV2(previousEnd,-span+1);
    return {{window:{{start:windowStart,end:windowEnd}},previous_window:{{start:previousStart,end:previousEnd}},platforms:[]}};
  }});
}}

function chartV2(id, config) {{
  if (volChartsV2[id]) volChartsV2[id].destroy();
  const canvas = document.getElementById(id);
  if (canvas) volChartsV2[id] = new Chart(canvas, config);
}}

function renderVolChipsV2(start,end) {{
  const totals = Object.fromEntries(CORE_COMPETITOR_PLATFORMS.map(p => [p, 0]));
  (VOLUME_DATA.facts || []).filter(row => activeVolFilterV2(row) && row.date >= start && row.date <= end)
    .forEach(row => totals[row.platform] = (totals[row.platform] || 0) + 1);
  const root = document.getElementById('volPlatformChips');
  root.innerHTML = CORE_COMPETITOR_PLATFORMS.map(p => `<button type="button" class="platform-chip${{volSelectedV2.has(p) ? ' active' : ''}}" data-platform="${{escV2(p)}}" aria-pressed="${{volSelectedV2.has(p)}}">${{escV2(p)}} <b>${{totals[p] || 0}}</b></button>`).join('');
  root.querySelectorAll('button').forEach(button => button.addEventListener('click', () => {{
    const platform = button.dataset.platform;
    if (volSelectedV2.has(platform) && volSelectedV2.size > 1) volSelectedV2.delete(platform);
    else volSelectedV2.add(platform);
    renderVolumeV2();
  }}));
}}

function renderHeatmapV2(id, metrics, field, labels) {{
  const maxValue = Math.max(1, ...metrics.flatMap(m => labels.map(label => Number((m[field] || {{}})[label] || 0))));
  const cols = `110px repeat(${{labels.length}},minmax(88px,1fr))`;
  let html = `<div class="heatmap" style="grid-template-columns:${{cols}}"><div class="heat-label">竞品</div>${{labels.map(label => `<div class="heat-col-label" title="${{escV2(label)}}">${{escV2(label)}}</div>`).join('')}}`;
  metrics.forEach(metric => {{
    html += `<div class="heat-label">${{escV2(metric.platform)}}</div>`;
    labels.forEach(label => {{
      const value = Number((metric[field] || {{}})[label] || 0);
      const alpha = value ? .12 + .68 * value / maxValue : .025;
      html += `<div style="background:rgba(255,0,51,${{alpha.toFixed(2)}})" title="${{escV2(metric.platform)}} · ${{escV2(label)}}：${{value}}">${{value}}</div>`;
    }});
  }});
  document.getElementById(id).innerHTML = html + '</div>';
}}

function methodEvidenceTextV2(method,row) {{
  if(method==='brand_led')return `标题中命中竞品名称“${{row.platform}}”`;
  if(method==='description_only')return `推广链接命中 ${{row.platform}}，但标题未出现竞品名`;
  if(method==='multi_platform')return `同一视频共命中 ${{row.promoted_platform_count}} 个推广平台`;
  if(method==='official')return `Channel ID 命中明确配置的 ${{row.platform}} 官方频道`;
  if(method==='unclassified')return '标题、链接与频道证据不足';
  return (row.promotion_method_evidence||{{}})[method]||'未记录具体依据';
}}

function evidenceDetailsV2(row) {{
  const methods=(row.promotion_methods||[]).map(method=>`<div class="tag-evidence-item"><code>${{escV2(method)}}</code><span>${{escV2(methodEvidenceTextV2(method,row))}}</span></div>`).join('');
  const topics=(row.content_topics||[]).map(topic=>{{
    const matched=(row.content_topic_evidence||{{}})[topic]||[];
    const text=topic==='Other'?'未命中已配置的主题关键词':(matched.length?`命中词：${{matched.join(' / ')}}`:'未记录命中词');
    return `<div class="tag-evidence-item"><code>${{escV2(topic)}}</code><span>${{escV2(text)}}</span></div>`;
  }}).join('');
  return `<details class="tag-evidence"><summary>查看判定依据</summary><div class="tag-evidence-body">${{methods?`<section class="tag-evidence-group"><b>推广方式</b>${{methods}}</section>`:''}}${{topics?`<section class="tag-evidence-group"><b>内容主题</b>${{topics}}</section>`:''}}</div></details>`;
}}

function closeVolDrillV2() {{
  volDrillStateV2 = null;
  volDrillRowsV2 = [];
  const panel = document.getElementById('volDrillPanel');
  panel.style.display = 'none';
  panel.innerHTML = '';
}}

function exportVolDrillCsv() {{
  const rows = volDrillRowsV2;
  if (!rows || !rows.length) return;
  const headers = ['日期','账号','市场','语言','推广方式','内容主题','首采播放','首采点赞','首采评论','首采互动率','观察时长分桶','观察时长(小时)','补采播放','补采点赞','补采评论','补采时间','视频标题','视频链接','推广平台数'];
  const lines = [headers.map(csvFieldV2).join(',')];
  rows.forEach(row => {{
    lines.push([
      row.date, row.account, row.market, row.language,
      (row.promotion_methods || []).join('；'), (row.content_topics || []).join('；'),
      row.view_count, row.like_count, row.comment_count, fmtPctV2(row.initial_engagement_rate),
      row.observation_bucket, row.observation_age_hours, row.backfill_view_count, row.backfill_like_count,
      row.backfill_comment_count, row.backfill_captured_at, row.title, row.video_url, row.promoted_platform_count
    ].map(csvFieldV2).join(','));
  }});
  const blob = new Blob(['﻿' + lines.join('\\r\\n')], {{type:'text/csv;charset=utf-8;'}});
  const url = URL.createObjectURL(blob);
  const state = volDrillStateV2 || {{}};
  const a = document.createElement('a');
  a.href = url;
  a.download = `volume-detail-${{state.platform || 'all'}}-${{state.start || ''}}-${{state.end || ''}}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}

function openVolDrillV2(platform, summary, options={{}}) {{
  const shouldFocus = options.focus !== false;
  const source = options.source || 'window';
  const start = summary.window.start, end = summary.window.end;
  const early = new Set((summary.early_high_keys || []).map(key => key.join('||')));
  const rows = (VOLUME_DATA.facts || []).filter(row => activeVolFilterV2(row) && row.platform === platform && row.date >= start && row.date <= end)
    .sort((a,b) => (b.view_count ?? -1) - (a.view_count ?? -1));
  const titleCovered = rows.filter(row => row.title).length;
  volDrillStateV2 = {{platform,start,end,source}};
  volDrillRowsV2 = rows;
  const panel = document.getElementById('volDrillPanel');
  panel.innerHTML = `<div class="section-head"><h3>${{escV2(platform)}} · 视频明细</h3><span style="display:flex;align-items:center;gap:14px"><span class="hint-inline">${{start}} — ${{end}} · ${{rows.length}} 个去重推广视频 · 标题覆盖 ${{titleCovered}}/${{rows.length}}</span><button type="button" class="filter-reset" onclick="exportVolDrillCsv()"${{rows.length ? '' : ' disabled'}}>下载 CSV</button></span></div>
    <div class="table-wrap"><table class="detail-table"><thead><tr><th>账号 / 市场</th><th>推广方式 / 主题</th><th>首采表现</th><th>观察时长</th><th>视频标题</th><th>推广平台</th></tr></thead><tbody>${{rows.map(row => {{
      const rate = row.initial_engagement_rate;
      const high = early.has(`${{row.platform}}||${{row.video_url}}`) ? '<span class="signal-pill">早期高表现</span>' : '';
      const late = row.backfill_captured_at ? `<div class="kpi-sub" title="历史补采时点的当前统计，不代表首采表现">补采播放 ${{fmtNumV2(row.backfill_view_count)}} · 赞 ${{fmtNumV2(row.backfill_like_count)}} · 评 ${{fmtNumV2(row.backfill_comment_count)}}</div>` : '';
      return `<tr><td><strong>${{escV2(row.account || '—')}}</strong><div class="kpi-sub">${{escV2([row.market,row.language].filter(Boolean).join(' · ') || '—')}}</div></td>
        <td><div class="detail-tags">${{(row.promotion_methods || []).map(x => `<span class="signal-pill method-pill">${{escV2(x)}}</span>`).join('')}}</div><div class="detail-tags">${{(row.content_topics || []).map(x => `<span class="signal-pill">${{escV2(x)}}</span>`).join('')}}</div>${{evidenceDetailsV2(row)}}</td>
        <td>首采播放 ${{fmtNumV2(row.view_count)}}<div class="kpi-sub">首采赞 ${{fmtNumV2(row.like_count)}} · 评 ${{fmtNumV2(row.comment_count)}} · 互动 ${{fmtPctV2(rate)}}</div>${{late}}${{high}}</td>
        <td>${{row.observation_bucket === 'Unknown' ? '—' : escV2(row.observation_bucket)}}<div class="kpi-sub">${{row.observation_age_hours == null ? '—' : `${{Number(row.observation_age_hours).toFixed(1)}}h`}}</div></td>
        <td><a class="detail-title-link${{row.title ? '' : ' is-missing'}}" href="${{escV2(row.video_url)}}" target="_blank" rel="noopener noreferrer">${{escV2(row.title || '标题暂未采集（点击查看原视频）')}}</a></td><td>${{row.promoted_platform_count}}</td></tr>`;
    }}).join('')}}</tbody></table></div>`;
  panel.style.display = 'block';
  if (shouldFocus) {{
    panel.focus({{preventScroll:true}});
    panel.scrollIntoView({{behavior:'smooth',block:'nearest'}});
  }}
}}

function renderVolumeV2() {{
  const count = Number(document.getElementById('volWindowCount').value);
  const dateFrom=document.getElementById('volDateFrom').value,dateTo=document.getElementById('volDateTo').value;
  if((dateFrom&&!dateTo)||(!dateFrom&&dateTo)||dateFrom>dateTo){{document.getElementById('volMeta').textContent='请选择完整且有效的开始、结束日期';return;}}
  const defaultWindow = (VOLUME_DATA.windows?.['7'] || [])[0]?.window;
  const activeStart = dateFrom || defaultWindow?.start, activeEnd = dateTo || defaultWindow?.end;
  const allWindows = activeStart&&activeEnd?customWindowsV2(activeStart,activeEnd,count):[];
  if (!allWindows.length) {{ document.getElementById('volMeta').textContent = '暂无数据'; return; }}
  const windows = allWindows.slice(0, count).map(filteredSummaryV2), latest = windows[0];
  const span = selectedSpanV2(latest.window.start,latest.window.end);
  renderVolChipsV2(latest.window.start,latest.window.end);
  const selected = CORE_COMPETITOR_PLATFORMS.filter(p => volSelectedV2.has(p));
  const metrics = latest.platforms.filter(m => volSelectedV2.has(m.platform));
  document.getElementById('volMeta').textContent = `指定期间 ${{latest.window.start}} — ${{latest.window.end}} · ${{span}} 天 · ${{selected.length}} 个竞品`;
  document.getElementById('volMatrixPeriod').textContent = `${{latest.window.start}} — ${{latest.window.end}} · 对比 ${{latest.previous_window.start}} — ${{latest.previous_window.end}}`;

  const totalVideos = metrics.reduce((sum,m) => sum + m.videos, 0);
  const totalPrevious = metrics.reduce((sum,m) => sum + m.growth.previous, 0);
  const totalChange = totalVideos - totalPrevious;
  const totalGrowth = totalPrevious ? totalChange / totalPrevious : null;
  const shareLeader = metrics.slice().sort((a,b) => (b.promotion_share || 0) - (a.promotion_share || 0))[0];
  const shareMomentum = metrics
    .filter(m => m.promotion_share != null && m.previous_promotion_share != null)
    .map(m => ({{...m,share_change:m.promotion_share-m.previous_promotion_share}}))
    .filter(m => m.share_change > 0)
    .sort((a,b) => b.share_change-a.share_change)[0];
  const scopedFacts = (VOLUME_DATA.facts || []).filter(row => activeVolFilterV2(row) && selected.includes(row.platform) && row.date >= latest.window.start && row.date <= latest.window.end);
  const distinctAccounts = new Set(scopedFacts.map(row => row.account_key).filter(Boolean)).size;
  document.getElementById('volKpiVideos').textContent = fmtNumV2(totalVideos);
  document.getElementById('volKpiWow').textContent = totalGrowth === null ? `较前期 ${{totalChange >= 0 ? '+' : ''}}${{totalChange}}` : `GR ${{totalGrowth >= 0 ? '+' : ''}}${{(totalGrowth*100).toFixed(1)}}% · ${{totalChange >= 0 ? '+' : ''}}${{totalChange}}`;
  document.getElementById('volKpiShare').textContent = shareLeader ? `${{shareLeader.platform}} ${{fmtPctV2(shareLeader.promotion_share)}}` : '—';
  document.getElementById('volKpiAccounts').textContent = fmtNumV2(distinctAccounts);
  document.getElementById('volKpiEarly').textContent = fmtNumV2(metrics.reduce((sum,m) => sum + m.early_high_performers, 0));
  document.getElementById('volKpiMomentum').textContent = shareMomentum ? `${{shareMomentum.platform}} +${{(shareMomentum.share_change*100).toFixed(1)}}pp` : '暂无提升';
  const activeLabels = [document.getElementById('volMarketFilter').value,document.getElementById('volLanguageFilter').value,document.getElementById('volMethodFilter').value,document.getElementById('volTopicFilter').value,document.getElementById('volTextFilter').value.trim()].filter(Boolean);
  document.getElementById('volFilterMeta').textContent = `指定期间命中 ${{scopedFacts.length}} 个去重竞品视频${{activeLabels.length ? ` · 已启用 ${{activeLabels.length}} 个条件` : ''}}`;
  const marketFilter=document.getElementById('volMarketFilter').value,languageFilter=document.getElementById('volLanguageFilter').value;
  const methodFilter=document.getElementById('volMethodFilter').value,topicFilter=document.getElementById('volTopicFilter').value,textFilter=document.getElementById('volTextFilter').value.trim();
  const structureFilters=[marketFilter&&`市场 ${{marketFilter}}`,languageFilter&&`语言 ${{languageFilter}}`,methodFilter&&`方式 ${{methodFilter}}`,topicFilter&&`主题 ${{topicFilter}}`,textFilter&&`搜索“${{textFilter}}”`,selected.length<CORE_COMPETITOR_PLATFORMS.length&&`竞品 ${{selected.join(' / ')}}`].filter(Boolean);
  const structureContext=`${{latest.window.start}} — ${{latest.window.end}} · ${{scopedFacts.length}} 条${{structureFilters.length?` · ${{structureFilters.join(' · ')}}`:' · 全部条件'}}`;
  ['volTopicContext','volMethodContext','volMarketContext','volAccountContext'].forEach(id=>document.getElementById(id).textContent=structureContext);

  const matrix = document.getElementById('volMatrixBody');
  const matrixLeaders = {{
    videos: Math.max(0,...metrics.map(metric=>metric.videos||0)),
    share: Math.max(0,...metrics.map(metric=>metric.promotion_share||0)),
  }};
  matrix.innerHTML = metrics.map(metric => {{
    const growth = metric.growth.percent;
    const growthText = growth === null ? (metric.videos ? `NEW (+${{metric.growth.change}})` : '—') : `${{growth >= 0 ? '+' : ''}}${{growth.toFixed(1)}}% (${{metric.growth.change >= 0 ? '+' : ''}}${{metric.growth.change}})`;
    const growthClass = metric.growth.significant ? `gr-significant${{growth < 0 ? ' negative' : ''}}` : '';
    const videoLeaderClass=metric.videos===matrixLeaders.videos?'metric-leader':'';
    const shareLeaderClass=metric.promotion_share===matrixLeaders.share?'metric-leader':'';
    return `<tr class="matrix-row" tabindex="0" data-platform="${{escV2(metric.platform)}}"><td><strong>${{escV2(metric.platform)}}</strong><div class="kpi-sub">记录 ${{metric.promotion_records}}</div></td><td class="${{videoLeaderClass}}"><span class="matrix-value">${{metric.videos}}</span></td><td class="${{shareLeaderClass}}"><span class="matrix-value">${{fmtPctV2(metric.promotion_share)}}</span><div class="kpi-sub">前期 ${{fmtPctV2(metric.previous_promotion_share)}}</div></td><td class="${{growthClass}}">${{growthText}}</td><td><span class="matrix-value">${{metric.accounts}}</span><div class="kpi-sub">连续 ${{metric.continuous_accounts}} · 均 ${{metric.videos_per_account == null ? '—' : metric.videos_per_account.toFixed(1)}}</div></td><td>${{metric.new_observed_accounts}}</td><td>${{fmtPctV2(metric.top1_share)}}<div class="kpi-sub">${{escV2(metric.concentration_signal)}}</div></td><td>${{fmtNumV2(metric.first_views_median)}}</td><td>${{metric.early_high_performers}}</td><td><span class="signal-pill method-pill">${{escV2(topKeyV2(metric.method_counts))}}</span></td><td><span class="signal-pill">${{escV2(topKeyV2(metric.topic_counts))}}</span></td><td><div class="signal-list">${{metric.signals.map(x => `<span class="signal-pill">${{escV2(x)}}</span>`).join('')}}</div></td></tr>`;
  }}).join('');
  matrix.querySelectorAll('.matrix-row').forEach(row => {{
    const open = () => openVolDrillV2(row.dataset.platform, latest,{{source:'latest'}});
    row.addEventListener('click', open);
    row.addEventListener('keydown', event => {{ if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); open(); }} }});
  }});

  const accountValues=metrics.map(m=>m.accounts||0),accountMin=Math.min(...accountValues),accountMax=Math.max(...accountValues);
  // Bubble area carries account count, but radius stays within 7–16px so the
  // third metric remains visible without obscuring neighbouring anchors.
  const accountRadius = value => {{
    if(accountMax===accountMin)return 11;
    const normalized=(value-accountMin)/(accountMax-accountMin);
    return Math.sqrt(7*7+normalized*(16*16-7*7));
  }};
  const smallestAccount=metrics.slice().sort((a,b)=>a.accounts-b.accounts)[0],largestAccount=metrics.slice().sort((a,b)=>b.accounts-a.accounts)[0];
  const bubbleScaleText=smallestAccount&&largestAccount?`Size 范围：${{smallestAccount.platform}} ${{smallestAccount.accounts}} 个账号 → ${{largestAccount.platform}} ${{largestAccount.accounts}} 个账号；圆面积按当前筛选结果归一化，半径限制 7–16px。`:'暂无账号规模数据';
  document.getElementById('volBrandBubbleScale').textContent=bubbleScaleText;
  document.getElementById('volViewBubbleScale').textContent=bubbleScaleText;
  const bubbleBase = (yKey, yTitle, percent) => ({{
    type: 'bubble',
    data: {{ datasets: metrics.map(m => ({{
      label: m.platform,
      data: [{{x:m.videos, y:m[yKey], accounts:m.accounts, r:accountRadius(m.accounts||0)}}],
      backgroundColor: `${{colorV2(m.platform)}}66`, borderColor: colorV2(m.platform), borderWidth: 2,
      hoverBackgroundColor: `${{colorV2(m.platform)}}99`, hoverBorderWidth: 2, hoverRadius: 2,
      pointStyle: 'circle',
    }})) }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins: {{
        legend: {{position:'bottom',labels:{{color:'#CBD5E1',usePointStyle:true}}}},
        tooltip: {{callbacks:{{label:ctx => `${{ctx.dataset.label}}：推广视频 ${{ctx.raw.x}} · ${{yTitle}} ${{percent ? fmtPctV2(ctx.raw.y) : fmtNumV2(ctx.raw.y)}} · 独立账号 ${{ctx.raw.accounts}}`}}}},
      }},
      scales: {{
        x: {{beginAtZero:true,title:{{display:true,text:'去重推广视频数',color:'#94A3B8'}},ticks:{{precision:0,color:'#64748B'}},grid:{{color:'rgba(255,255,255,.05)'}}}},
        y: {{beginAtZero:true,max:percent?1:undefined,title:{{display:true,text:yTitle,color:'#94A3B8'}},ticks:{{color:'#64748B',callback:v=>percent?`${{v*100}}%`:fmtNumV2(v)}},grid:{{color:'rgba(255,255,255,.05)'}}}},
      }},
    }},
  }});
  // Nested method share is flattened explicitly for Chart.js.
  metrics.forEach(m => m.brand_led_share = (m.method_shares || {{}}).brand_led || 0);
  chartV2('cVolBrandBubble', bubbleBase('brand_led_share','brand_led 占比',true));
  chartV2('cVolViewBubble', bubbleBase('first_views_median','首采播放量中位数',false));

  const chrono = windows.slice().reverse();
  const lineOptions = percent => ({{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom',labels:{{color:'#CBD5E1',usePointStyle:true}}}}}},scales:{{x:{{ticks:{{color:'#64748B'}},grid:{{display:false}}}},y:{{beginAtZero:true,max:percent?1:undefined,ticks:{{color:'#64748B',callback:v=>percent?`${{Math.round(v*100)}}%`:v}},grid:{{color:'rgba(255,255,255,.05)'}}}}}}}});
  chartV2('cVolTrend', {{type:'line',data:{{labels:chrono.map(w=>w.window.start),datasets:selected.map(p=>({{label:p,data:chrono.map(w=>w.platforms.find(m=>m.platform===p)?.videos||0),borderColor:colorV2(p),backgroundColor:'transparent',tension:.25,borderWidth:p==='Zoomex'?3.5:2,pointRadius:p==='Zoomex'?4:2.5}}))}},options:lineOptions(false)}});
  chartV2('cVolShareTrend', {{type:'line',data:{{labels:chrono.map(w=>w.window.start),datasets:selected.map(p=>({{label:p,data:chrono.map(w=>w.platforms.find(m=>m.platform===p)?.promotion_share||0),borderColor:colorV2(p),backgroundColor:'transparent',tension:.25,borderWidth:p==='Zoomex'?3.5:2,pointRadius:p==='Zoomex'?4:2.5}}))}},options:lineOptions(true)}});

  const topicLabels = topicFilter?[topicFilter]:['Activity','Product','Tutorial','Market Analysis','Trading Signal','Review/Comparison','Listing','Brand Introduction','Other'];
  renderHeatmapV2('volTopicHeatmap', metrics, 'topic_counts', topicLabels);
  const selectedGeoLabels=[marketFilter&&`市场 ${{marketFilter}}`,languageFilter&&`语言 ${{languageFilter}}`].filter(Boolean);
  const marketLabels = selectedGeoLabels.length?selectedGeoLabels:Array.from(new Set(metrics.flatMap(m => [...Object.keys(m.market_counts||{{}}).map(x=>`市场 ${{x}}`),...Object.keys(m.language_counts||{{}}).map(x=>`语言 ${{x}}`) ]))).slice(0,10);
  const heatMetrics = metrics.map(m => ({{...m,geo_counts:Object.fromEntries([...Object.entries(m.market_counts||{{}}).map(([k,v])=>[`市场 ${{k}}`,v]),...Object.entries(m.language_counts||{{}}).map(([k,v])=>[`语言 ${{k}}`,v])])}}));
  renderHeatmapV2('volMarketHeatmap', heatMetrics, 'geo_counts', marketLabels);

  const methodLabels = methodFilter?[methodFilter]:['brand_led','description_only','multi_platform','official','unclassified'];
  chartV2('cVolMethods', {{
    type:'bar',
    data:{{labels:metrics.map(m=>m.platform),datasets:methodLabels.map((method,i)=>({{label:method,data:metrics.map(m=>m.method_shares[method]||0),backgroundColor:COLORS[i%COLORS.length]}}))}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom',labels:{{color:'#CBD5E1',boxWidth:10}}}}}},scales:{{x:{{ticks:{{color:'#CBD5E1'}},grid:{{display:false}}}},y:{{beginAtZero:true,max:1,ticks:{{color:'#64748B',callback:v=>`${{v*100}}%`}},grid:{{color:'rgba(255,255,255,.05)'}}}}}}}},
  }});
  chartV2('cVolAccounts', {{
    type:'bar',
    data:{{labels:metrics.map(m=>m.platform),datasets:[
      {{label:'Top1 占比',data:metrics.map(m=>m.top1_share||0),backgroundColor:'#D85C72',yAxisID:'yPct'}},
      {{label:'Top3 占比',data:metrics.map(m=>m.top3_share||0),backgroundColor:'#B96BC7',yAxisID:'yPct'}},
      {{label:'新观察账号',data:metrics.map(m=>m.new_observed_accounts),backgroundColor:'#F3BA4B',yAxisID:'yCount'}},
      {{label:'连续推广账号',data:metrics.map(m=>m.continuous_accounts),backgroundColor:'#FF7A45',yAxisID:'yCount'}},
    ]}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom',labels:{{color:'#CBD5E1',boxWidth:10}}}}}},scales:{{x:{{ticks:{{color:'#CBD5E1'}},grid:{{display:false}}}},yPct:{{position:'left',beginAtZero:true,max:1,ticks:{{color:'#64748B',callback:v=>`${{v*100}}%`}}}},yCount:{{position:'right',beginAtZero:true,ticks:{{color:'#64748B',precision:0}},grid:{{drawOnChartArea:false}}}}}}}},
  }});

  document.getElementById('volTableHead').innerHTML = '<th>窗口</th>' + selected.map(p=>`<th>${{escV2(p)}}</th><th>Share</th>`).join('');
  document.getElementById('volTableBody').innerHTML = windows.map((summary,index)=>`<tr><td>${{summary.window.start}} — ${{summary.window.end}}</td>${{selected.map(p=>{{const m=summary.platforms.find(x=>x.platform===p);return `<td class="vol-cell" tabindex="0" data-index="${{index}}" data-platform="${{escV2(p)}}">${{m?.videos||0}}</td><td>${{fmtPctV2(m?.promotion_share)}}</td>`;}}).join('')}}</tr>`).join('');
  document.querySelectorAll('#volTableBody .vol-cell').forEach(cell => {{ const open=()=>openVolDrillV2(cell.dataset.platform,windows[Number(cell.dataset.index)],{{source:'window'}}); cell.addEventListener('click',open); cell.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' '){{e.preventDefault();open();}}}}); }});
  if (volDrillStateV2) {{
    const state = volDrillStateV2;
    const summary = state.source === 'latest' ? latest : windows.find(item => item.window.start === state.start && item.window.end === state.end);
    if (summary && selected.includes(state.platform)) openVolDrillV2(state.platform,summary,{{focus:false,source:state.source}});
    else closeVolDrillV2();
  }}
}}

initVolFiltersV2();
document.getElementById('volWindowSize').addEventListener('change',event=>{{
  if(event.target.value==='custom')return;
  const days=Number(event.target.value),defaultWindow=(VOLUME_DATA.windows?.['7']||[])[0]?.window;
  if(!defaultWindow)return;
  document.getElementById('volDateTo').value=defaultWindow.end;
  document.getElementById('volDateFrom').value=shiftDateV2(defaultWindow.end,-days+1);
  renderVolumeV2();
}});
['volDateFrom','volDateTo'].forEach(id=>document.getElementById(id).addEventListener('change',()=>{{
  const start=document.getElementById('volDateFrom').value,end=document.getElementById('volDateTo').value;
  if(start&&end&&start<=end){{
    const span=String(selectedSpanV2(start,end));
    document.getElementById('volWindowSize').value=['7','14','30'].includes(span)?span:'custom';
  }} else document.getElementById('volWindowSize').value='custom';
  renderVolumeV2();
}}));
['volWindowCount','volMarketFilter','volLanguageFilter','volMethodFilter','volTopicFilter'].forEach(id=>document.getElementById(id).addEventListener('change',renderVolumeV2));
let volSearchTimer;
document.getElementById('volTextFilter').addEventListener('input',()=>{{clearTimeout(volSearchTimer);volSearchTimer=setTimeout(renderVolumeV2,180);}});
document.getElementById('volFilterReset').addEventListener('click',()=>{{
  ['volMarketFilter','volLanguageFilter','volMethodFilter','volTopicFilter','volTextFilter'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('volWindowSize').value='7';document.getElementById('volWindowCount').value='4';
  const defaultWindow=(VOLUME_DATA.windows?.['7']||[])[0]?.window;
  if(defaultWindow){{document.getElementById('volDateFrom').value=defaultWindow.start;document.getElementById('volDateTo').value=defaultWindow.end;}}
  volSelectedV2.clear();CORE_COMPETITOR_PLATFORMS.forEach(platform=>volSelectedV2.add(platform));renderVolumeV2();
}});
renderVolumeV2();
</script>
</body>
</html>
"""
    return _prune_generated_page(html_output, page_mode)


def run(page: str = "all"):
    leads = load_leads()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    weekly_insight = load_weekly_insight()
    OUT_DIR.mkdir(exist_ok=True)
    generated = []
    public_channels = load_channels() if page in {"all", "main", "channels"} else []
    if page in {"all", "main"}:
        OUT_PATH.write_text(generate_html(leads, public_channels, run_date, weekly_insight, "main"), encoding="utf-8")
        generated.append(OUT_PATH)
    if page in {"all", "channels"}:
        CHANNELS_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CHANNELS_OUT_PATH.write_text(generate_html(leads, public_channels, run_date, weekly_insight, "channels"), encoding="utf-8")
        generated.append(CHANNELS_OUT_PATH)
    if page in {"all", "volume"}:
        volume_channels = load_channels(include_descriptions=True)
        VOLUME_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        VOLUME_OUT_PATH.write_text(generate_html(leads, volume_channels, run_date, weekly_insight, "volume"), encoding="utf-8")
        generated.append(VOLUME_OUT_PATH)
    print(f"[Report] Generated {', '.join(map(str, generated))} ({len(leads)} leads, {len(public_channels)} channels)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--page", choices=("all", "main", "channels", "volume"), default="all")
    run(parser.parse_args().page)
