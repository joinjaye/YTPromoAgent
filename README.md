# PromoLeads — YouTube CEX 推广线索与竞品分析

PromoLeads 按北京时间抓取 YouTube 上推广加密货币交易所（CEX）的视频，解析推广
链接与频道信息，写入本地 SQLite，同步两套飞书多维表格、推送新增推广线索，并生成
三个彼此独立的 GitHub Pages 静态页面。

系统维护两个数据维度：

- **`leads`（推广记录）**：仅保存识别到已知交易所推广链接的记录。一条视频命中
  多个平台或链接时可以产生多条记录，是原飞书表格、群推送及主看板的数据来源。
- **`channels`（频道）**：按 YouTube `channel_id` 去重。无论视频是否命中推广链接
  都会保留，并持续合并视频、市场、联系方式、推广平台和播放量等信息，是频道飞书
  表格与频道分析页的数据来源。

两张表都位于同一个文件 `data/leads.db`。文件名虽然沿用历史命名，但提交这个文件会
同时持久化 `leads` 和 `channels` 的变化。

## 项目结构

```text
promoLeads/
├── main.py                         # 抓取、解析、落库、飞书同步与群推送
├── config.py                       # 搜索词、核心竞品、配额和市场映射
├── youtube_fetcher.py              # YouTube search/videos/channels API
├── link_extractor.py               # 推广链接、联系方式、语言、市场、Hashtag
├── db.py                           # SQLite：leads + channels
├── feishu_client.py                # 两套 Bitable 同步与群机器人推送
├── reporter.py                     # 一次生成三个独立静态页面
├── weekly_insight.py               # 周度数据快照 + Cursor Agent Winsight
├── insight_instruction.md          # Winsight 分析口径与写作约束
├── backfill_feishu.py              # 一次性回填 leads 飞书历史数据
├── requirements.txt
├── data/
│   ├── leads.db                    # 同时包含 leads/channels 两张表
│   └── weekly_insight.json         # 最近一次成功的周度 AI 结果
├── site/                           # reporter.py 的构建产物
│   ├── index.html                  # 主看板：/
│   ├── channels/index.html         # 频道分析：/channels/
│   └── volume/index.html           # 竞品声量：/volume/
└── .github/workflows/
    ├── crawl.yml                   # 执行抓取并提交 data/leads.db
    ├── pages.yml                   # 构建并部署整个 site 目录
    └── weekly-insight.yml          # 生成并提交 weekly_insight.json
```

## 日常处理流程

```text
PromoLeads Crawl
  │
  ├─ 1. 抓取北京时间前一天发布的视频
  │     ├─ search.list：逐关键词搜索
  │     ├─ videos.list：标题、描述、发布时间、播放量
  │     └─ channels.list：订阅数、国家、频道总视频/播放量
  │
  ├─ 2. 同一批视频走两条数据路径
  │     ├─ leads：描述中识别到推广链接才写入
  │     └─ channels：无论是否命中推广链接都按 channel_id 合并
  │
  ├─ 3. 飞书
  │     ├─ leads：新增记录写入原推广线索表
  │     └─ channels：新频道新增、已有频道原行更新
  │
  ├─ 4. 群推送
  │     └─ 只通知本轮真正新增的 leads，补同步历史记录不重复通知
  │
  └─ 5. 提交 data/leads.db
        └─ 同一个文件同时包含 leads 与 channels 的更新

Deploy Dashboard（在 Crawl 成功完成后触发）
  └─ reporter.py 一次生成 /、/channels/、/volume/ 并整体部署到 Pages
```

Channel 的落库与飞书同步发生在“本轮没有推广链接则返回”之前。因此，即使某天抓到
的视频都没有识别出推广链接，频道数据仍然可以更新。单个关键词失败、YouTube 配额
耗尽或飞书暂时不可用时，已有数据会尽量完成落库；飞书失败不会回滚本地 SQLite。

定时触发策略由 GitHub 侧统一配置；各 workflow 同时保留手动触发入口，便于补跑。

### Channel-only 安全测试模式

手动触发 `PromoLeads Crawl` 时可以选择 `run_mode=channels_only`。该模式用于真实验证
每日 Channel 链路，同时保护原 Leads 业务链路：

```text
照常执行：YouTube 搜索 → videos/channels API → 解析平台与联系方式
          → channels 本地合并 → Channel 飞书对账 → Channel 飞书新增/更新

明确跳过：Leads 飞书表结构初始化 → leads SQLite 写入 → Leads 飞书同步 → 群推送
```

执行结束后仍会提交 `data/leads.db`，其中只有 `channels` 表发生本轮业务更新；随后
Pages 可以重新生成 `/channels/`。主看板虽然会随同一个 Pages artifact 重新构建，
但其数据来源 `leads` 没有变化，因此不会出现新的 Leads 数据。竞品声量页依赖
Channels 的播放量、语言等关联信息，相关覆盖数据可能随 Channel 更新而变化，但不会
生成新的周度 AI Insight。

本地也可以使用同一模式：

```bash
PROMOLEADS_RUN_MODE=channels_only python3 main.py
```

## SQLite 数据

### `leads`

核心字段：

| 字段 | 说明 |
|---|---|
| `id` | 本地主键 |
| `youtuber` | 视频频道名 |
| `promo_platform` | 识别到的推广平台 |
| `promo_link` | 推广链接 |
| `video_url` | YouTube 视频链接 |
| `published_at` | 视频发布时间（UTC RFC3339） |
| `created_at` | 本地写入时间 |
| `feishu_record_id` | 原 leads 飞书表中的记录 ID |

唯一约束为 `(video_url, promo_platform, promo_link)`，避免重复抓取时重复落库和推送。

### `channels`

| 字段 | 说明 |
|---|---|
| `channel_id` | YouTube 频道 ID，主键 |
| `account_name` / `profile_url` | 频道名与主页 |
| `followers` | 当前订阅数 |
| `country` / `language` / `market` | 国家、识别语言和市场 |
| `channel_video_cnt` / `channel_view_cnt` | YouTube 返回的频道全站规模 |
| `keyword` | 命中过该频道的搜索词并集 |
| `promo_platform` / `promo_link` | 历史识别到的平台和链接并集 |
| `videos` | 去重视频 JSON 数组 |
| `total_views` | `videos` 中已抓取视频播放量之和 |
| `contact` | 社媒和邮箱集合 |
| `first_crawled_at` / `last_crawled_at` | 首次和最近抓取时间 |
| `feishu_record_id` | Channel 飞书表中的记录 ID |

`videos` 每项保存：

```json
{
  "video_url": "...",
  "video_title": "...",
  "description": "...",
  "published_at": "...",
  "view_count": 0,
  "hashtags": []
}
```

同一视频再次出现时按 `video_url` 更新，不重复追加。`description` 只保留在本地
SQLite，普通静态页面不会嵌入原文；周度 Winsight 会在生成阶段读取、清洗和限长后
用于内容分析。

`total_views` 是系统抓到的视频累计播放量，不等于 `channel_view_cnt`。后者是频道
所有公开视频的全站历史播放量。

## 三个独立页面

`reporter.py` 每次运行都会生成以下页面。它们不嵌入彼此的 DOM、运行脚本或跳转
按钮，可通过各自的 GitHub Pages URL 独立访问。

### 主看板 `/`

- **最新更新**：最新抓取日期对应的推广视频、记录数、Youtuber 和平台。
- **全局视图**：全部 leads 的日期筛选、趋势、平台分布、Top Youtuber 和推广明细。

主看板只嵌入自身需要的数据，不包含频道页或竞品声量页的页面逻辑。

### 频道分析 `/channels/`

数据来源以 `channels` 表为主。

全局筛选会同时驱动 KPI、图表和明细表：

- 市场下拉框，以及 KR / FR / ID / VN 快捷按钮
- 粉丝量级
- 最低累计观看量
- 日期口径：内容活跃、首次抓取、最近抓取
- 起止日期
- 频道、平台、国家和关键词搜索

核心内容：

- KPI：频道数、覆盖市场数、粉丝总量、抓取视频累计观看、期间新增、期间活跃
- 市场组合气泡图：频道数 × 累计观看，气泡大小表示粉丝总量
- 频道质量散点图：粉丝数 × 累计观看；点击数据点定位到明细
- 频道明细：市场、粉丝、频道规模、推广平台、联系方式、抓取视频、累计观看、
  最近内容日期、最近抓取日期、首次抓取日期
- 点击频道行可展开逐条视频的发布时间、播放量、标题和链接

### 竞品声量 `/volume/`

只呈现 `CORE_COMPETITOR_KEYWORDS`：Weex、Bitunix、Blofin、BingX、Zoomex、
LBank、Phemex。核心竞品使用更宽的搜索翻页上限，非核心关键词不进入时间窗口对比。

声量口径是指定窗口内识别到推广平台的视频记录，不包含评论区提及，因此不能与外部
社媒监听工具的绝对值直接比较。

页面包含：

- 7/14/30 天窗口和 4/6/8 个对比窗口
- 7 天窗口固定为自然周周五至周四；14/30 天为滑动窗口
- 周度视频数量与 WoW 对照，`|WoW| ≥ 30%` 高亮
- 视频数量趋势
- 最新窗口“视频数量 × 已覆盖累计播放量”双轴组合图
- 语言构成、Top 15 Hashtag 和覆盖率提示
- 账号数、Top1 账号占比、集中信号、标题重复度和模板化信号
- 点击平台/窗口视频数展开账号、市场、语言、播放量、标题和链接
- 顶部展示最近一次成功生成的 Weekly AI Winsight

播放量按竞品和去重 `video_url` 求和。未能关联到 `channels.videos` 的历史记录不会
被虚构播放量，页面会显示实际覆盖率。

## Weekly AI Winsight

`weekly_insight.py` 读取最新完整的周五至周四自然周，并与前一周比较。程序负责从
SQLite 计算真实指标，Cursor Agent 只负责内容归纳。

输入包括：

- 视频数量及 WoW
- 已覆盖视频累计播放量和单条效率
- 独立账号数、Top1 占比及集中信号
- 标题重复度与模板化信号
- 语言、市场和 Hashtag
- 视频 description 内容样本及覆盖率

Description 样本优先选择高播放视频，同时保留不同作者的代表样本。URL、推广码等
噪声会被清洗，单条长度和单平台样本数受限；模型不得引用 description 原文或补造
活动信息。

输出格式为：一句大盘结论、7 个竞品各一段 3–4 句运营分析，以及必要的覆盖率提示。
每段重点区分铺量和有效观看，识别高播放内容、重复主题、语言/市场/KOL 结构，并给出
下周选题、KOL 投放或活动承接建议。

结果写入 `data/weekly_insight.json`：

- 使用临时文件后原子替换，只有完整生成成功才覆盖旧结果
- 日常 Crawl/Pages 只读取该文件，不调用 Cursor，也不改变 Insight
- Cursor 失败或输出校验失败时，上一份结果继续展示
- 下一次成功生成之前，同一份内容持续显示在 `/volume/` 顶部

## 飞书同步与群推送

### Leads 飞书表

由 `FEISHU_BITABLE_APP_TOKEN` / `FEISHU_BITABLE_TABLE_ID` 指定，采用 append-only
方式。同步前会读取线上记录校准本地 `feishu_record_id`，避免“线上已写入、本地未
标记”导致重复创建，也能识别线上记录被删除的情况。

### Channel 飞书表

由 `FEISHU_BITABLE_CHANNEL_APP_TOKEN` / `FEISHU_BITABLE_CHANNEL_TABLE_ID` 指定，
与 Leads 表是独立 Base/Table：

- 新频道：`batch_create_channel_records`
- 已有频道：`batch_update_channel_records` 原行更新
- 每次只同步本轮实际碰到的频道，不全表扫描
- 同步前按 `channel_id` 与线上表对账
- `setup_channel_table()` 可重复执行，自动补齐所需字段
- Channel 同步失败不会中断 Leads 落库、同步和群推送

两套表共用 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，飞书应用需要同时是两个 Base
的协作者。

### 群推送

群机器人只推送本轮真正新增的 Leads：

- 新增记录、Youtuber 和平台汇总
- Youtuber、推广平台、推广链接和视频链接
- 飞书表格入口和 `DASHBOARD_URL`

历史补同步不会重复发群消息。

## GitHub Actions 与 Pages

### `crawl.yml`

1. Checkout
2. 安装 Python 依赖
3. 从 `ENV_FILE` secret 创建 `.env`
4. 执行 `python main.py`
5. 即使抓取步骤异常，也尽量提交已经写入的 `data/leads.db`

提交 `data/leads.db` 会同时提交 Leads 与 Channels 两张表的变化。

### `pages.yml`

在 `PromoLeads Crawl` 或 `Weekly AI Winsight` 成功完成后：

1. Checkout 最新 `main`，而不是 workflow 启动前的旧 SHA
2. 执行 `python3 reporter.py`
3. 上传整个 `site` 目录
4. 部署三个独立 GitHub Pages 页面

### `weekly-insight.yml`

安装 Cursor Agent CLI，读取 `CURSOR_API_KEY`，执行 `weekly_insight.py`，提交
`data/weekly_insight.json`。任务成功后由 `pages.yml` 重新构建 `/volume/`。

## 安装与环境变量

```bash
pip install -r requirements.txt
cp .env.example .env
```

| 变量 | 说明 |
|---|---|
| `YOUTUBE_API_KEYS` | 多个 YouTube Data API v3 Key，逗号分隔并自动轮换 |
| `YOUTUBE_API_KEY` | 兼容旧配置，前者为空时使用 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 两套飞书同步共用的应用凭证 |
| `FEISHU_BITABLE_APP_TOKEN` | Leads Base token |
| `FEISHU_BITABLE_TABLE_ID` | Leads table ID |
| `FEISHU_BITABLE_CHANNEL_APP_TOKEN` | Channel Base token |
| `FEISHU_BITABLE_CHANNEL_TABLE_ID` | Channel table ID |
| `FEISHU_WEBHOOK_URL` | 飞书群机器人 Webhook |
| `DASHBOARD_URL` | 群卡片中的主看板地址 |
| `CURSOR_API_KEY` | Weekly Insight workflow 使用；建议配置为 GitHub Actions Secret |
| `PROMOLEADS_RUN_MODE` | `normal`（默认）或 `channels_only`；通常由 workflow 输入设置 |

GitHub Actions 的 Crawl workflow 使用 `ENV_FILE` secret 写出完整 `.env`；
`CURSOR_API_KEY` 在 Weekly workflow 中单独注入。

飞书应用至少需要多维表格读写权限，并需要被添加为两个 Base 的协作者。

## 常用命令

```bash
# 抓取前一天数据，落库、同步两套飞书表并推送群消息
python3 main.py

# 从当前 leads.db + weekly_insight.json 生成三个静态页面
python3 reporter.py

# 手动生成最新完整自然周 Winsight（需要 CURSOR_API_KEY）
python3 weekly_insight.py

# 真实执行 Channel 链路，但不写 Leads、不更新 Leads 飞书、不发群消息
PROMOLEADS_RUN_MODE=channels_only python3 main.py

# 一次性回填 Leads 飞书历史数据
python3 backfill_feishu.py
```

## 核心配置

### 搜索与配额

`SEARCH_KEYWORDS` 同时用于 YouTube 搜索和推广平台匹配。新增交易所关键词时应确认
对应域名/文本能够被 `link_extractor.py` 正确识别。

```python
SEARCH_MAX_RESULTS = 50
CORE_SEARCH_MAX_RESULTS = 500
CORE_COMPETITOR_KEYWORDS = {
    "weex", "bitunix", "blofin", "bingx", "zoomex", "lbank", "phemex"
}
```

- 非核心关键词每次最多 50 条
- 核心竞品每次最多 500 条
- `search.list` 每页消耗 100 配额；多个 API Key 会在配额耗尽时轮换
- 竞品声量页只使用核心竞品，避免把被 50 条上限截断的数据当成完整周度趋势

### 市场归类

`classify_market(country, language)` 的优先级是：

1. YouTube channel country：合法两位国家代码直接作为 market
2. country 缺失时，使用识别语言查询 `MARKET_BY_LANGUAGE`
3. 两者都不可用时留空

语言到市场的映射是近似兜底，不应当被解释为频道的真实注册国家。历史频道会在下次
被抓到时刷新其最新语言/市场信息，不做全库自动回填。

## 数据口径与已知限制

- YouTube 搜索 API 的发布时间过滤会在本地再次校验，确保落在北京时间前一天窗口。
- Leads 的“视频数量”本质是推广记录计数；播放量聚合会按 `video_url` 去重。
- 历史 Leads 早于 Channels 功能上线时，可能无法关联语言、标题、Hashtag、Description
  或播放量。页面和 Winsight 必须按覆盖率降级，不回推缺失数字。
- `channel_view_cnt` 是频道全站规模；`total_views` 是系统抓取到的视频播放量，两者不能
  混用。
- 视频播放量是生成时累计快照。不同发布日期的累积时间不同，因此 Winsight 不做播放量
  WoW，只用当前窗口播放量判断内容效率。
- 系统只抓 YouTube，不包含评论区或其他社交平台，不能与社媒监听绝对值直接比较。
