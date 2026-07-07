import os
from dotenv import load_dotenv

load_dotenv()

YOUTUBE_API_KEY            = os.getenv("YOUTUBE_API_KEY")
FEISHU_APP_ID              = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET          = os.getenv("FEISHU_APP_SECRET")
FEISHU_BITABLE_APP_TOKEN   = os.getenv("FEISHU_BITABLE_APP_TOKEN")
FEISHU_BITABLE_TABLE_ID    = os.getenv("FEISHU_BITABLE_TABLE_ID")
FEISHU_WEBHOOK_URL         = os.getenv("FEISHU_WEBHOOK_URL")

'''SEARCH_KEYWORDS = [
'coinbase exchange','binance','kraken','okx','bitget','bybit','mexc','gemini','bingx','bitvavo','crypto.com','hashkey exchange','gate','bitso','bitunix','lbank','kucoin','ourbit','coinstore','bitstamp by robinhood','coinw','bullish','binance us','toobit','bitkub','bitkan','whitebit','bitcointry','bit2me','luno','digifinex','upbit','weex','hashkey global','btse','bitbank','backpack exchange','cointr','bitmart','byte exchange','niza.io','nonkyc.io','zoomex','bitazza','deribit spot','pionex','bitfinex','valr','bitmex','max maicoin','htx','bitrue','bybit eu','bittime','gmo coin japan','coins.ph','gate us','okj','bithumb','hibt','itbit','bitflyer','bydfi','biconomy.com','p2b','xt.com','coinone','bitlo','emirex','phemex','grovex','cex.io','levex','korbit','azbit','coinex','independent reserve','btcturk | kripto','bittrade','websea','ascendex (bitmax)','bitopro','pointpay','xbo.com','tapbit','difx','orangex','kcex','blofin','tokpie','dex-trade','nami exchange','tokocrypto','blockchain.com','figure markets','coindcx','tothemoon','koinpark','orbix','mercado bitcoin'
]'''

SEARCH_KEYWORDS = ['bitunix']

# Max videos fetched per keyword per run 
SEARCH_MAX_RESULTS = 100
