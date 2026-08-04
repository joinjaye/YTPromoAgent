import os
from dotenv import load_dotenv

load_dotenv()


def _parse_youtube_keys() -> list[str]:
    """
    支持多个 YouTube API Key 轮换。优先读 YOUTUBE_API_KEYS，取不到再读旧的
    YOUTUBE_API_KEY —— 两个变量都按逗号分隔支持写多个 key（.env 里已经在
    YOUTUBE_API_KEY 塞了多个用逗号分开的 key，所以这里两个都要切分，
    不能假设旧变量名下只有一个 key，否则会把整串逗号文本当成一个非法 key）。
    """
    raw = os.getenv("YOUTUBE_API_KEYS") or os.getenv("YOUTUBE_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


YOUTUBE_API_KEYS            = _parse_youtube_keys()
YOUTUBE_API_KEY             = YOUTUBE_API_KEYS[0] if YOUTUBE_API_KEYS else ""  # 向后兼容
FEISHU_APP_ID              = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET          = os.getenv("FEISHU_APP_SECRET")
FEISHU_BITABLE_APP_TOKEN   = os.getenv("FEISHU_BITABLE_APP_TOKEN")
FEISHU_BITABLE_TABLE_ID    = os.getenv("FEISHU_BITABLE_TABLE_ID")
# 频道级（channels）同步用的是独立的 Base/Table，跟上面 leads 那份不是同一个
FEISHU_BITABLE_CHANNEL_APP_TOKEN = os.getenv("FEISHU_BITABLE_CHANNEL_APP_TOKEN")
FEISHU_BITABLE_CHANNEL_TABLE_ID  = os.getenv("FEISHU_BITABLE_CHANNEL_TABLE_ID")
FEISHU_WEBHOOK_URL         = os.getenv("FEISHU_WEBHOOK_URL")

# 可视化看板（GitHub Pages）
DASHBOARD_URL              = os.getenv("DASHBOARD_URL", "https://joinjaye.github.io/YTPromoAgent/")

SEARCH_KEYWORDS = [
'coinbase exchange','binance','kraken','okx','bitget','bybit','mexc','gemini','bingx','bitvavo','crypto.com','hashkey exchange','gate','bitso','bitunix','lbank','kucoin','ourbit','coinstore','bitstamp by robinhood','coinw','bullish','binance us','toobit','bitkub','bitkan','whitebit','bitcointry','bit2me','luno','digifinex','upbit','weex','hashkey global','btse','bitbank','backpack exchange','cointr','bitmart','byte exchange','niza.io','nonkyc.io','zoomex','bitazza','deribit spot','pionex','bitfinex','valr','bitmex','max maicoin','htx','bitrue','bybit eu','bittime','gmo coin japan','coins.ph','gate us','okj','bithumb','hibt','itbit','bitflyer','bydfi','biconomy.com','p2b','xt.com','coinone','bitlo','emirex','phemex','grovex','cex.io','levex','korbit','azbit','coinex','independent reserve','btcturk | kripto','bittrade','websea','ascendex (bitmax)','bitopro','pointpay','xbo.com','tapbit','difx','orangex','kcex','blofin','tokpie','dex-trade','nami exchange','tokocrypto','blockchain.com','figure markets','coindcx','tothemoon','koinpark','orbix','mercado bitcoin'
]

# Max videos fetched per keyword per run（每 50 条一页，每页 = 一次 search.list =
# 100 配额单位；youtube_fetcher._search_video_ids 会一直翻页直到拿满这个上限或
# 当天实际没有更多结果为止，所以这不是"固定成本"，而是"单个关键词当天最多允许
# 翻几页"的安全上限——大部分关键词大部分日子根本用不到这么多页，真正花的配额
# 取决于当天实际匹配到的视频数）。
#
# 分层策略：与其把 101 个关键词的翻页上限一起调（要么整体太紧、要么整体太松），
# 不如只对真正要做时间窗口分析的核心竞品放宽上限，其余关键词维持原来的 1 页
# （50 条/天）——这样不需要精简 SEARCH_KEYWORDS 也能保证核心竞品数据基本完整，
# 配额压力也可控：
#   核心竞品：len(CORE_COMPETITOR_KEYWORDS) × 最多 10 页 × 100 单位 ≈ 5,000 单位
#   其余关键词：(100 - 5) × 1 页 × 100 单位 ≈ 9,500 单位
#   两者合计的理论上限 ≈ 14,500 单位/天，在 YOUTUBE_API_KEYS 配了 3 个 key 轮换
#   （理论总预算 30,000 单位/天）的前提下留有余量；这是"全部核心词都刚好在
#   当天用满 10 页"的最坏情况，正常情况下远用不到（翻页循环会在结果拿完后
#   自然停止，见 _search_video_ids 里的 break 条件）。
#
# 「竞品声量」看板 Tab 的窗口分析也只呈现 CORE_COMPETITOR_KEYWORDS 对应的平台
# ——不是巧合，是故意的：时间窗口对比只有在数据基本完整时才有意义，非核心关键词
# 仍然只有 1 页/天，拿去做周度声量对比会跟"数据被截断"的问题一样，所以看板直接
# 不展示那些词，避免出现看似可信、实际不完整的对比图表。
CORE_COMPETITOR_KEYWORDS = {"weex", "bitunix", "blofin", "bingx", "zoomex"}
CORE_COMPETITOR_DISPLAY_NAMES = {
    "weex": "Weex", "bitunix": "Bitunix", "blofin": "Blofin",
    "bingx": "BingX", "zoomex": "Zoomex",
}

# Only explicitly maintained channel IDs are treated as official.  An empty
# set deliberately means "unknown" rather than guessing from channel names.
OFFICIAL_COMPETITOR_CHANNEL_IDS: dict[str, set[str]] = {
    "Weex": set(),
    "Bitunix": set(),
    "Blofin": set(),
    "BingX": set(),
    "Zoomex": set(),
}

# Explainable, multi-label topic rules.  Keep these here (rather than in the
# report template) so crawl-time enrichment, reports and tests share one source
# of truth.  Matching is case-insensitive against title + description + hashtags.
CONTENT_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Activity": (
        "campaign", "competition", "contest", "giveaway", "bonus", "reward",
        "airdrop", "event", "活动", "大赛", "赠金", "奖励", "抽奖",
    ),
    "Product": (
        "product", "feature", "app", "platform", "copy trading", "futures",
        "spot trading", "wallet", "产品", "功能", "合约", "现货", "跟单",
    ),
    "Tutorial": (
        "how to", "tutorial", "guide", "step by step", "setup", "deposit",
        "withdraw", "register", "教程", "指南", "如何", "注册", "充值", "提现",
    ),
    "Market Analysis": (
        "market analysis", "price analysis", "technical analysis", "forecast",
        "outlook", "行情", "市场分析", "技术分析", "走势", "预测",
    ),
    "Trading Signal": (
        "trading signal", "trade signal", "entry", "take profit", "stop loss",
        "long signal", "short signal", "交易信号", "入场", "止盈", "止损",
    ),
    "Review/Comparison": (
        "review", "comparison", "compare", " vs ", "versus", "pros and cons",
        "评测", "测评", "对比", "比较", "优缺点",
    ),
    "Listing": (
        "listing", "listed", "new pair", "launchpool", "上币", "上线", "新币",
    ),
    "Brand Introduction": (
        "what is", "introduction", "overview", "explained", "介绍", "是什么", "品牌",
    ),
}

# Small samples should not receive percentile or structural certainty labels.
EARLY_PERFORMANCE_MIN_SAMPLE = 5
WOW_HIGHLIGHT_PERCENT = 30
WOW_HIGHLIGHT_ABS_CHANGE = 3

SEARCH_MAX_RESULTS = 50            # 默认（非核心关键词）翻页上限：1 页
CORE_SEARCH_MAX_RESULTS = 500      # 核心竞品关键词翻页上限：10 页

# Market classification for the channel-level table. Priority: a channel's real
# `country` (from channels.list) always wins when present — any ISO 3166-1
# alpha-2 code is accepted directly, not just a curated shortlist. `language`
# (langdetect on title+description) is only a fallback for channels the API
# didn't report a country for, mapped to one representative market per
# language below. This is inherently approximate for languages spoken across
# many countries (Arabic, Spanish, English, ...) — pick the single best guess,
# don't try to be exhaustive. A channel resolves to "" only when neither a
# country nor a detectable language is available.
MARKET_BY_LANGUAGE = {
    "af": "ZA", "ar": "SA", "bg": "BG", "bn": "BD", "ca": "ES", "cs": "CZ",
    "cy": "GB", "da": "DK", "de": "DE", "el": "GR", "en": "US", "es": "ES",
    "et": "EE", "fa": "IR", "fi": "FI", "fr": "FR", "gu": "IN", "he": "IL",
    "hi": "IN", "hr": "HR", "hu": "HU", "id": "ID", "it": "IT", "ja": "JP",
    "kn": "IN", "ko": "KR", "lt": "LT", "lv": "LV", "mk": "MK", "ml": "IN",
    "mr": "IN", "ne": "NP", "nl": "NL", "no": "NO", "pa": "IN", "pl": "PL",
    "pt": "BR", "ro": "RO", "ru": "RU", "sk": "SK", "sl": "SI", "so": "SO",
    "sq": "AL", "sv": "SE", "sw": "KE", "ta": "IN", "te": "IN", "th": "TH",
    "tl": "PH", "tr": "TR", "uk": "UA", "ur": "PK", "vi": "VN",
    "zh-cn": "CN", "zh-tw": "TW",
}
