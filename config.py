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
FEISHU_WEBHOOK_URL         = os.getenv("FEISHU_WEBHOOK_URL")

# 可视化看板（GitHub Pages）
DASHBOARD_URL              = os.getenv("DASHBOARD_URL", "https://joinjaye.github.io/YTPromoAgent/")

SEARCH_KEYWORDS = [
'coinbase exchange','binance','kraken','okx','bitget','bybit','mexc','gemini','bingx','bitvavo','crypto.com','hashkey exchange','gate','bitso','bitunix','lbank','kucoin','ourbit','coinstore','bitstamp by robinhood','coinw','bullish','binance us','toobit','bitkub','bitkan','whitebit','bitcointry','bit2me','luno','digifinex','upbit','weex','hashkey global','btse','bitbank','backpack exchange','cointr','bitmart','byte exchange','niza.io','nonkyc.io','zoomex','bitazza','deribit spot','pionex','bitfinex','valr','bitmex','max maicoin','htx','bitrue','bybit eu','bittime','gmo coin japan','coins.ph','gate us','okj','bithumb','hibt','itbit','bitflyer','bydfi','biconomy.com','p2b','xt.com','coinone','bitlo','emirex','phemex','grovex','cex.io','levex','korbit','azbit','coinex','independent reserve','btcturk | kripto','bittrade','websea','ascendex (bitmax)','bitopro','pointpay','xbo.com','tapbit','difx','orangex','kcex','blofin','tokpie','dex-trade','nami exchange','tokocrypto','blockchain.com','figure markets','coindcx','tothemoon','koinpark','orbix','mercado bitcoin'
]

# Max videos fetched per keyword per run 
SEARCH_MAX_RESULTS = 50
