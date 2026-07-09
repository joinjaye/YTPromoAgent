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
        → 逐关键词查询，支持多个 YOUTUBE_API_KEYS 轮换：
          单个 key 配额耗尽（429 / 403 quotaExceeded）自动切下一个重试
        → 跨关键词 video_id 去重（同一视频只处理一次）
        → 全部 key 都耗尽、或单个关键词请求异常时，跳过继续，不中断整体流程
        → 已收集的数据正常往下走完整个流程
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
Step 2.6 以飞书线上数据为准校准同步状态
        → 拉取飞书表格当前全部记录，校正本地 feishu_record_id 缓存
        → 自愈：之前写成功但本地没记上 / 记录被删或表被改 → 下面重新同步
        │
        ▼
Step 3  写入飞书多维表格
        → 待同步 = 本次新增 + 之前运行失败遗留的历史记录，每轮自动重试直到成功
        → 每条记录分配自增 ID（SQLite record_counter）
        → batch_create_records 批量写入；失败只跳过本次同步，不影响后续步骤
        │
        ▼
Step 4  群卡片推送
        → 只推本次真正新抓到的记录，不重复通知历史补同步部分
        → 附「查看数据详情」（飞书表格）+「查看可视化看板」（GitHub Pages）两个按钮
        → 卡片标题带视频发布日期；飞书写入失败也照常推送
        │
        ▼
Step 5  持久化
        → leads.db commit 回 GitHub 仓库（if: always()，即使 Run crawler 意外失败也尝试提交）
        │
        ▼
（独立触发）Deploy Dashboard workflow
        → 监听 PromoLeads Crawl 完成事件，check out main 分支最新提交
        → 用最新的 leads.db 重新生成看板并发布到 GitHub Pages
```

> Step 1~4 里的每一步失败（YouTube 单关键词异常、飞书写入/推送失败）都只跳过当前这一步，
> 不会让整个 `main.py` 崩溃退出——这样 Step 5 的 commit 和后面的 Pages 部署始终能正常触发，
> 已经抓到的数据不会因为某个下游服务临时故障就丢失或"卡住不更新"。

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
| 发布时间 | 视频的 YouTube 发布时间（DateTime 字段） |

---

## 去重说明

| 层级 | 机制 | 说明 |
| --- | --- | --- |
| 跨轮次 | 固定的「前一天」窗口 | 每轮只抓前一天发布的视频；若同一天内重复运行，会重新抓到同一批视频 |
| 本地存储 | `leads` 表 UNIQUE(video_url, promo_platform, promo_link) | 重复记录写入本地时会被静默跳过 |
| 同轮跨关键词 | seen_video_ids set | 同一视频被多个关键词命中时只处理一次 |
| 同视频多平台/多链接 | 不去重 | 一条视频推广了多个平台、或同平台有多条链接时，每条链接独立成一条记录 |
| 飞书写入 | `leads.feishu_record_id` + 线上校准 | 每轮运行前用 `fetch_all_records()` 拉取飞书当前数据校正本地缓存，只对"本地存在但飞书还没有"的记录 create；不会因为重复运行产生重复的飞书记录，之前失败的记录会在后续运行自动重试直到同步成功 |

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
| `YOUTUBE_API_KEYS` | config.py / `.env` | — | 多个 YouTube API Key，逗号分隔，配额耗尽自动轮换下一个；不设则回退读 `YOUTUBE_API_KEY`（也支持同样逗号分隔多个） |
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

## 更新日志

### 2026-07-09 — 容错修复 + Pages 部署 bug

起因：一次运行在写飞书多维表格时遇到 `TableIdNotFound`，`main.py` 直接崩溃退出，
排查过程中又发现 GitHub Pages 长期"看板不更新"是另一个独立的 bug。这次一起修复：

- **飞书/YouTube 故障不再让整个脚本崩溃**：`main.py` 里 YouTube 单个关键词异常、
  飞书批量写入失败、群推送失败，现在都只跳过当前这一步、打日志，不会中断后续流程。
  这样即使某个下游服务临时故障，`crawl.yml` 的 `Run crawler` 步骤依然会正常退出（exit 0），
  `Persist crawl log` 能正常提交、`Deploy Dashboard` 能正常触发。
- **飞书同步改为"以线上数据为准"校准 + 失败自动重试**：新增 `feishu_client.fetch_all_records()` /
  `db.reconcile_feishu_sync()`，每轮运行前拉取飞书当前实际数据校正本地 `feishu_record_id` 缓存
  （之前这个字段其实从来没被正确写过）。本次新增的、以及之前运行同步失败遗留的记录，
  都会在 `get_unsynced_leads()` 里被重新尝试同步，直到真正成功为止，不会因为一次失败就永久丢失。
- **多 YouTube API Key 轮换**：新增 `YOUTUBE_API_KEYS`（逗号分隔），单个 key 配额耗尽
  （429 / 403 quotaExceeded）自动切下一个重试，全部耗尽才停止当轮搜索；已收集的数据照常处理。
- **修复 `pages.yml` 的部署 bug（影响所有历史运行，不止这一次）**：`Deploy Dashboard` 之前用
  `github.event.workflow_run.head_sha` 作为 checkout 的 `ref`，但这个值是 `PromoLeads Crawl`
  **开始运行时**的 commit，而 crawl 自己在运行过程中还会 push 一次新的数据 commit——用
  `head_sha` checkout 出来的永远是那次数据提交*之前*的版本，导致看板一直落后一个爬取周期。
  现在改成固定 `ref: main`，触发时 crawl 早已完成并 push，`main` 上就是最新数据。
- **`crawl.yml` 的 `Persist crawl log` 步骤加了 `if: always()`** 作为最后一道防线：即使
  `Run crawler` 出现完全没预料到的崩溃，只要本地 `leads.db` 里已经有数据，也会尝试提交。
- 推送卡片标题加了视频发布日期（如 `🔍 发现 15 条新推广记录 · 视频日期 2026-07-08`）。
- 校准过程中确认了当时 GitHub / 飞书 / 本地三方数据一致（432 条，双向 diff 为 0）。

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
