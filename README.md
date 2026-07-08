# YTPromoAgent

自动从 YouTube 搜索竞品交易所关键词，提取视频 description 中的推广（affiliate）链接，写入飞书多维表格、推送群卡片通知，并生成可视化看板发布到 GitHub Pages。

通过 GitHub Actions 每天自动运行，固定抓取「前一天」（北京时间）发布的视频，与实际运行时间无关。

---

## 整体流程

```text
每天定时触发（GitHub Actions，由 cron-job.org 调用 workflow_dispatch）
        │
        ▼
Step 1  YouTube 搜索
        窗口固定为北京时间前一天 00:00~24:00
        （published_after / published_before，与上次运行时间无关）
        → 逐关键词查询
        → 跨关键词 video_id 去重（同一视频只处理一次）
        → 遇到 429 限流时停止搜索，已收集数据继续处理
        │
        ▼
Step 2  提取推广链接
        解析每条视频的 description
        → 识别 YouTube redirect 跳转链接并还原真实 URL
        → 过滤社交/工具类域名（YouTube / Twitter / Discord 等）
        → 匹配平台域名（规则来自 SEARCH_KEYWORDS）
        → 一条视频可拆出多条记录（每个平台链接独立一行）
        │
        ▼
Step 2.5 本地持久化
        → 写入本地 leads 表（SQLite），即使飞书写入失败也不丢数据
        → 看板数据来源于此，不依赖飞书 API
        │
        ▼
Step 3  写入飞书多维表格
        → 每条记录分配自增 ID（SQLite record_counter）
        → batch_create_records 批量写入
        │
        ▼
Step 4  群卡片推送
        → 本轮新增记录逐条展示
        → 附「查看数据详情」（飞书表格）+「查看可视化看板」（GitHub Pages）两个按钮
        → 无新记录时静默结束
        │
        ▼
Step 5  持久化
        → leads.db commit 回 GitHub 仓库
        │
        ▼
（独立触发）Deploy Dashboard workflow
        → 监听 PromoLeads Crawl 完成事件
        → 用最新的 leads.db 重新生成看板并发布到 GitHub Pages
```

看板地址：`config.py` 中的 `DASHBOARD_URL`（默认 `https://joinjaye.github.io/YTPromoAgent/`）。

---

## 飞书多维表格字段

| 字段 | 说明 |
| --- | --- |
| 主键列 | 自增整数 ID，从 1 开始，每条记录唯一 |
| Youtuber | YouTube 频道名 |
| 推广平台 | 识别到的交易所名称 |
| 推广链接 | description 中提取的原始 affiliate 链接 |
| Video 链接 | YouTube 视频地址 |

飞书只存这 4 个字段，视频发布时间等元数据只保存在本地 `leads` 表中，供看板使用。

---

## 去重说明

| 层级 | 机制 | 说明 |
| --- | --- | --- |
| 跨轮次 | 固定的「前一天」窗口 | 每轮只抓前一天发布的视频；若同一天内重复运行，会重新抓到同一批视频 |
| 本地存储 | `leads` 表 UNIQUE(video_url, promo_platform, promo_link) | 重复记录写入本地时会被静默跳过 |
| 同轮跨关键词 | seen_video_ids set | 同一视频被多个关键词命中时只处理一次 |
| 同视频多平台/多链接 | 不去重 | 一条视频推广了多个平台、或同平台有多条链接时，每条链接独立成一条记录 |
| 飞书写入 | 不去重 | 每条记录直接 create，不做 upsert；同一天内重复运行会产生重复的飞书记录 |

---

## 搜索关键词

来源：CoinGecko CEX 榜单。同时作为 YouTube 搜索词和推广平台识别规则，新增平台只需在 `config.py` 的 `SEARCH_KEYWORDS` 中追加一行。

```text
coinbase exchange, binance, kraken, okx, bitget, bybit, mexc, gemini,
bingx, bitvavo, crypto.com, hashkey exchange, gate, bitso, bitunix,
lbank, kucoin, ourbit, coinstore, bitstamp by robinhood, coinw, bullish,
binance us, toobit, bitkub, bitkan, whitebit, bitcointry, bit2me, luno,
digifinex, upbit, weex, hashkey global, btse, bitbank, backpack exchange,
cointr, bitmart, byte exchange, niza.io, nonkyc.io, zoomex, bitazza,
deribit spot, pionex, bitfinex, valr, bitmex, max maicoin, htx, bitrue,
bybit eu, bittime, gmo coin japan, coins.ph, gate us, okj, bithumb,
hibt, itbit, bitflyer, bydfi, biconomy.com, p2b, xt.com, coinone, bitlo,
emirex, phemex, grovex, cex.io, levex, korbit, azbit, coinex,
independent reserve, btcturk | kripto, bittrade, websea, ascendex (bitmax),
bitopro, pointpay, xbo.com, tapbit, difx, orangex, kcex, blofin, tokpie,
dex-trade, nami exchange, tokocrypto, blockchain.com, figure markets,
coindcx, tothemoon, koinpark, orbix, mercado bitcoin
```

每个关键词每次最多拉取 `SEARCH_MAX_RESULTS`（当前 50）条视频。

---

## 关键参数

| 参数 | 位置 | 当前值 | 说明 |
| --- | --- | --- | --- |
| `SEARCH_MAX_RESULTS` | config.py | 50 | 每个关键词每次最多拉取视频数 |
| 抓取窗口 | main.py | 北京时间前一天全天 | `published_after`/`published_before`，与运行时刻无关 |
| `DASHBOARD_URL` | config.py / `.env` | `https://joinjaye.github.io/YTPromoAgent/` | 看板地址，写入 Lark 卡片按钮 |
| 定时时间 | cron-job.org | — | 外部服务调用 `workflow_dispatch` 触发 crawl.yml |

---

## 本地运行

```bash
cp .env.example .env   # 填入凭证
pip install -r requirements.txt

python main.py              # 运行一次爬虫（YouTube → 本地 leads 表 → 飞书 → Lark 卡片）
python backfill_feishu.py   # 一次性：从飞书多维表格回填历史记录到本地 leads 表
python reporter.py          # 从本地 leads.db 生成看板 → site/index.html
```

---

## 部署（GitHub Actions）

### 环境变量配置

在仓库 **Settings → Secrets and variables → Actions** 添加一个 Secret：

| Secret 名称 | 内容 |
| --- | --- |
| `ENV_FILE` | 将本地 `.env` 文件全部内容粘贴进去 |

### 自动运行

由外部服务 [cron-job.org](https://cron-job.org) 定时调用 GitHub API 触发 `PromoLeads Crawl`（`workflow_dispatch`）。也可在 **Actions → PromoLeads Crawl → Run workflow** 手动触发。

每次运行结束后：
- `data/leads.db` 自动 commit 回仓库（commit message 含 `[skip ci]`）。
- `Deploy Dashboard` workflow 监听 `PromoLeads Crawl` 的完成事件自动触发（不受 `[skip ci]` 影响），用最新数据重新生成看板并发布到 GitHub Pages。

首次启用需要在仓库 **Settings → Pages** 中把 Source 设为 **GitHub Actions**。

---

## 项目结构

```text
YTPromoAgent/
├── main.py                      # 主入口，顺序执行爬取 → 提取 → 本地持久化 → 飞书 → 群推送
├── youtube_fetcher.py           # YouTube Data API：search + videos.list
├── link_extractor.py            # description 解析，平台域名匹配
├── feishu_client.py             # 飞书多维表格写入 + 群卡片推送
├── backfill_feishu.py           # 一次性：从飞书多维表格回填历史记录到本地 leads 表
├── reporter.py                  # 从 data/leads.db 生成可视化看板（site/index.html）
├── db.py                        # SQLite：leads + record_counter
├── config.py                    # 环境变量 + SEARCH_KEYWORDS + 参数
├── .env.example                 # 环境变量模板
├── .github/
│   └── workflows/
│       ├── crawl.yml            # 爬虫任务（workflow_dispatch）
│       └── pages.yml            # 看板构建 + 发布到 GitHub Pages
├── site/                        # reporter.py 生成的看板（gitignored，构建产物）
└── data/
    └── leads.db                 # 运行时自动创建，随仓库持久化
```
