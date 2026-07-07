# promoLeads

自动从 YouTube 搜索竞品交易所关键词，提取视频 description 中的推广链接，写入飞书多维表格并推送群卡片通知。每 10 分钟自动执行一轮，通过 crawl_log 确保只拉取新发布的视频，不重复爬取。

---

## 整体流程

```text
┌─────────────────────────────────────────────────────────┐
│  每 10 分钟触发一轮 run()                                │
└────────────────────┬────────────────────────────────────┘
                     │
         ┌───────────▼───────────┐
         │  Step 1: YouTube 搜索  │
         │  逐关键词增量拉取视频   │
         └───────────┬───────────┘
                     │  published_after = crawl_log 中该关键词的上次爬取时间
                     │  → YouTube API 只返回该时间之后发布的新视频
                     │  → 爬完立即更新 crawl_log（记录本次时间戳）
                     │  → 跨关键词 video_id 去重（同一视频只处理一次）
                     │
         ┌───────────▼───────────┐
         │  Step 2: 提取推广链接  │
         │  解析每条视频 description│
         └───────────┬───────────┘
                     │  从 description 提取所有外链
                     │  → 过滤社交/工具类域名（YouTube / Twitter / Discord 等）
                     │  → 匹配 SEARCH_KEYWORDS 中的平台域名
                     │  → 一条视频可拆出多条记录（每个 promo link 独立一行）
                     │
         ┌───────────▼───────────┐
         │  Step 3: 写入飞书表格  │
         │  batch_create_records  │
         └───────────┬───────────┘
                     │  每条记录附带自增 ID（SQLite record_counter）
                     │  写入主键列作为唯一标识
                     │
         ┌───────────▼───────────┐
         │  Step 4: 群卡片推送    │
         │  notify_new_records    │
         └───────────────────────┘
                     逐条展示本轮新增详情（Youtuber / 推广平台 / 链接 / 视频）
```

---

## 去重 & 防重爬机制

| 层级 | 机制 | 作用 |
| --- | --- | --- |
| 跨轮次 | `crawl_log` + YouTube `publishedAfter` 参数 | 每轮只拉取上次爬取时间之后发布的新视频，彻底避免跨轮重复 |
| 同轮内 | `seen_video_ids` set | 同一视频被多个关键词命中时只处理一次，避免同一视频的 promo 链接重复提取 |
| 同视频多平台 | 不去重 | 一条视频 description 中有多个平台链接时，每个链接独立成一条记录（正确行为） |
| 飞书记录 | 不去重 | 每条记录单独写入，无 upsert；历史数据保持不变 |

---

## 推广链接提取逻辑

`link_extractor.extract_promo_links(description)` 的执行步骤：

1. **YouTube redirect 解析**：description 中常见 `youtube.com/redirect?q=<实际URL>` 形式的跳转链接，先解析出真实目标 URL
2. **直链扫描**：正则匹配所有 `https://` 开头的 URL
3. **跳过名单过滤**：排除 YouTube、Twitter、Telegram、Instagram、Discord、TikTok、Facebook、Reddit、App Store 等社交/工具域名
4. **平台匹配**：将 URL 域名与 `SEARCH_KEYWORDS` 派生的平台规则逐一比对
   - 标准品牌：检查域名边界（`bybit.com`、`accounts.binance.com`、`sub.bybit.com` 等均可命中）
   - 域名型关键词（`crypto.com`、`xt.com`、`coins.ph` 等）：精确匹配
   - 复合名称（`mercado bitcoin` → `mercadobitcoin.com.br`）：拼接形式匹配
5. **结果**：返回 `[{promo_link, promo_platform}, ...]`，一条 description 可返回多项

---

## 飞书多维表格

### 字段结构

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| 主键列 | 文本 | 自增整数 ID，由本地 SQLite `record_counter` 分配，写入时填入 |
| Youtuber | 文本 | YouTube 频道名（`channelTitle`） |
| 推广平台 | 文本 | 识别到的交易所名称 |
| 推广链接 | 文本 | description 中提取的原始 affiliate 链接 |
| Video 链接 | 文本 | YouTube 视频地址 |

### 群通知卡片格式

每轮有新记录时推送一张蓝色 interactive 卡片，标题显示本轮新增数量，正文逐条列出每条记录的 Youtuber / 推广平台 / 推广链接 / 视频链接，条目之间用分隔线区隔。

---

## 本地 SQLite（data/leads.db）

| 表 | 用途 |
| --- | --- |
| `crawl_log` | 记录每个搜索关键词的上次成功爬取时间（RFC3339），供下次搜索设置 `publishedAfter` |
| `record_counter` | 单行计数器，`allocate_record_ids(n)` 原子性地分配 n 个连续整数 ID |

---

## 关键词 & 平台匹配（关键词来源：Coingecko top100 CEX）

`config.py` 的 `SEARCH_KEYWORDS` 同时承担两个职责：

1. **YouTube 搜索词**：逐一查询，每次最多拉取 `SEARCH_MAX_RESULTS`条
2. **平台识别规则**：启动时由 `link_extractor._build_platforms()` 解析成 `(brand, brand_concat, display_name)` 三元组列表，用于 URL 域名匹配

新增平台只需在 `SEARCH_KEYWORDS` 中追加关键词，两个职责同步生效，无需改其他文件。

---

## 项目结构

```text
promoLeads/
├── main.py            # 主入口：run() 函数 + 10 分钟 while 循环
├── youtube_fetcher.py # YouTube Data API v3：search.list + videos.list
├── link_extractor.py  # description 解析：URL 提取、平台匹配
├── feishu_client.py   # 飞书多维表格写入 + 群卡片推送
├── db.py              # SQLite：crawl_log + record_counter
├── config.py          # 环境变量读取 + SEARCH_KEYWORDS 配置
├── .env               # 实际密钥（不入库）
├── .env.example       # 密钥模板
└── data/
    └── leads.db       # 运行时自动创建
```

---

## 环境配置

```bash
cp .env.example .env   # 填入各项凭证
pip install -r requirements.txt
python main.py
```

| 变量 | 说明 |
| --- | --- |
| `YOUTUBE_API_KEY` | Google Cloud Console → YouTube Data API v3 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书开放平台企业自建应用，需开通 `bitable:app` 权限 |
| `FEISHU_BITABLE_APP_TOKEN` | 多维表格 URL 中 `/base/` 后的字符串 |
| `FEISHU_BITABLE_TABLE_ID` | 多维表格 URL 中 `table=` 后的字符串 |
| `FEISHU_WEBHOOK_URL` | 飞书群机器人 Webhook 地址 |

首次运行自动初始化 SQLite 和飞书表字段，之后每 10 分钟循环执行。按 `Ctrl+C` 停止。
