# YoutubePromoAgent

自动从 YouTube 搜索竞品交易所关键词，提取视频 description 中的推广（affiliate）链接，写入飞书多维表格并推送群卡片通知。

通过 GitHub Actions 每天 UTC 01:00 自动运行，基于 crawl_log 做增量爬取，不重复处理已访问过的视频。

---

## 整体流程

```text
每天 UTC 01:00 自动触发（GitHub Actions）
        │
        ▼
Step 1  YouTube 搜索
        逐关键词查询，published_after = 上次爬取时间（crawl_log）
        → 只返回该时间之后发布的新视频
        → 爬完立即更新 crawl_log
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
Step 3  写入飞书多维表格
        → 每条记录分配自增 ID（SQLite record_counter）
        → batch_create_records 批量写入
        │
        ▼
Step 4  群卡片推送
        → 本轮新增记录逐条展示
        → 无新记录时静默结束
        │
        ▼
Step 5  持久化
        → leads.db commit 回 GitHub 仓库
        → 下次运行时 crawl_log 时间戳不丢失
```

---

## 飞书多维表格字段

| 字段 | 说明 |
| --- | --- |
| 主键列 | 自增整数 ID，从 1 开始，每条记录唯一 |
| Youtuber | YouTube 频道名 |
| 推广平台 | 识别到的交易所名称 |
| 推广链接 | description 中提取的原始 affiliate 链接 |
| Video 链接 | YouTube 视频地址 |

---

## 去重 & 防重爬说明

| 层级 | 机制 | 说明 |
| --- | --- | --- |
| 跨轮次 | crawl_log + YouTube publishedAfter | 每轮只拉上次时间之后的新视频，不重复爬取 |
| 同轮跨关键词 | seen_video_ids set | 同一视频被多个关键词命中时只处理一次 |
| 同视频多平台 | 不去重 | 一条视频推广了多个平台时，每个链接独立成一条记录 |
| 飞书写入 | 不去重 | 每条记录直接 create，不做 upsert |

---

## 搜索关键词（当前共 96 个）

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
| 定时时间 | .github/workflows/crawl.yml | UTC 01:00 | cron: `0 1 * * *` |

---

## YouTube API 配额说明

YouTube Data API v3 每日上限 **10,000 units**：
- `search.list`：每次调用消耗 **100 units**
- `videos.list`：每次调用消耗 **1 unit**（每批最多 50 条）

96 个关键词 × 100 units = 9,600 units（search 部分），加上 videos.list 约占满每日配额。如需提额，前往 Google Cloud Console → APIs & Services → YouTube Data API v3 → Quotas 申请。

遇到 429 限流时，程序会停止继续搜索，但已拉取到的视频会正常完成提取、写入和推送。

---

## 本地运行

```bash
cp .env.example .env   # 填入凭证
pip install -r requirements.txt
python main.py         # 运行一次后退出
```

---

## 部署（GitHub Actions）

### 环境变量配置

在仓库 **Settings → Secrets and variables → Actions** 添加一个 Secret：

| Secret 名称 | 内容 |
| --- | --- |
| `ENV_FILE` | 将本地 `.env` 文件全部内容粘贴进去 |

### 自动运行

推送到 `main` 分支后，每天 UTC 01:00 自动执行。

也可在 **Actions → PromoLeads Crawl → Run workflow** 手动触发。

每次运行结束后，`data/leads.db` 会自动 commit 回仓库（commit message 含 `[skip ci]`，不会触发新的 Actions）。

---

## 项目结构

```text
YoutubePromoAgent/
├── main.py                      # 主入口，顺序执行四步流程
├── youtube_fetcher.py           # YouTube Data API：search + videos.list
├── link_extractor.py            # description 解析，平台域名匹配
├── feishu_client.py             # 飞书多维表格写入 + 群卡片推送
├── db.py                        # SQLite：crawl_log + record_counter
├── config.py                    # 环境变量 + SEARCH_KEYWORDS + 参数
├── .env.example                 # 环境变量模板
├── .github/
│   └── workflows/
│       └── crawl.yml            # GitHub Actions 定时任务
└── data/
    └── leads.db                 # 运行时自动创建，随仓库持久化
```
