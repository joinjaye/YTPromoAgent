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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH  = Path(__file__).parent / "data" / "leads.db"
OUT_DIR  = Path(__file__).parent / "site"
OUT_PATH = OUT_DIR / "index.html"

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


# ── HTML generation ───────────────────────────────────────────────────────────

def generate_html(leads: list[dict], run_date: str) -> str:
    colors_js = json.dumps(CHART_COLORS)
    all_js    = json.dumps(_row_dicts(leads), ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PromoLeads 看板</title>
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

.chart-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
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
.table-wrap {{ overflow-x:auto; }}
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
}}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">PROMO<span>LEADS</span> · 看板</div>
  <div class="status"><span class="dot"></span>更新于 {run_date}</div>
</div>

<div class="tabbar">
  <button class="tab-btn active" id="tabBtnLatest" onclick="switchTab('latest', this)">最新更新</button>
  <button class="tab-btn" id="tabBtnGlobal" onclick="switchTab('global', this)">全局视图</button>
</div>

<div class="container">

  <!-- ── Tab: 最新更新 ────────────────────────────────────────────────── -->
  <div id="tab-latest" class="tab-content active">
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

</div>

<script>
const COLORS = {colors_js};
const ALL_LEADS = {all_js};

function switchTab(id, btn) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
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
</script>
</body>
</html>
"""


def run():
    leads = load_leads()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = generate_html(leads, run_date)

    OUT_DIR.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[Report] Generated {OUT_PATH} ({len(leads)} leads)")


if __name__ == "__main__":
    run()
