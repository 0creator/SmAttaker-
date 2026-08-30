"""
SmAttaker — V45.4.1 (APEX) Model Registry
The single source of truth for the unified V45.4.1 strategy.

Maps every trained v45.4.1 asset to:
  - data source & fetch params (CCXT crypto vs yfinance/Twelve Data for the rest)
  - platform display symbol
  - validated out-of-sample win-rate (best_wr) and best static filter mode
    (best_mode), sourced from results/master_summary.csv of the v45.4.1
    training package, used to flag "best assets" with a badge in signal cards.

This registry is the single source of truth for V45.4.1 asset configuration.
Only assets that actually have a trained model on disk are listed here.

v45.4.1 adds 18 new assets over v43 (AMD, AMZN, APE, BABA, BMY, DOGE, HOOD,
MSFT, MSTR, PIPPIN, RIVER, SNAP, SOFI, T, TSLA, WMT, plus USOIL and the
ES=F/NQ=F/YM=F index futures) and drops 3 v43-only assets whose data source
stopped working (HBAR, HYPE, SUI). WOM and SPCX were attempted but excluded
from training (WOM: ticker delisted; SPCX: insufficient bars) -- see
results/README.txt in the training package for details.
"""
import os
import logging

logger = logging.getLogger("smattaker.v45_registry")

# -----------------------------------------------------------------------------
# Full asset universe -- 82 trained v45.4.1 models.
# asset_class values map to the platform's Signal.asset_class column and to
# the data_fetcher.fetch_ohlcv() dispatcher:
#   "crypto"     -> CCXT chain (MEXC->KuCoin->OKX->Bybit->Binance->Kraken)
#   "gold"       -> XAU via tokenized PAXGUSDT (CCXT, 24/7), XAG via Twelve Data/
#                   yfinance SI=F fallback
#   "commodity"  -> USOIL, Twelve Data/yfinance CL=F fallback, 24/5 like forex
#   "forex"      -> Twelve Data, yfinance =X fallback
#   "stocks"     -> Twelve Data, yfinance fallback
#   "futures"    -> equity index futures (ES=F/NQ=F/YM=F), yfinance, 24/5 like forex
# -----------------------------------------------------------------------------

V45_ASSET_UNIVERSE = [

    # ======================================================================
    # CRYPTO — 35 assets
    # ======================================================================
    {"symbol": 'ALGO',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'ALGO/USDT',   "data_source": 'ccxt',      "binance_symbol": 'ALGOUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 53.5,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'APE',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'APE/USDT',    "data_source": 'ccxt',      "binance_symbol": 'APEUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 49.6,  "best_mode": 'smart_filter'},
    {"symbol": 'AVAX',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'AVAX/USDT',   "data_source": 'ccxt',      "binance_symbol": 'AVAXUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 48.2,  "best_mode": 'smart_filter'},
    {"symbol": 'BNB',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'BNB/USDT',    "data_source": 'ccxt',      "binance_symbol": 'BNBUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 56.5,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'BONK',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'BONK/USDT',   "data_source": 'ccxt',      "binance_symbol": 'BONKUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 53.9,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'BTC',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'BTC/USDT',    "data_source": 'ccxt',      "binance_symbol": 'BTCUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 56.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'DOGE',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'DOGE/USDT',   "data_source": 'ccxt',      "binance_symbol": 'DOGEUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 51.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'DOT',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'DOT/USDT',    "data_source": 'ccxt',      "binance_symbol": 'DOTUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 66.7,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'ENA',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'ENA/USDT',    "data_source": 'ccxt',      "binance_symbol": 'ENAUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 55.1,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'ETH',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'ETH/USDT',    "data_source": 'ccxt',      "binance_symbol": 'ETHUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 66.4,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'FARTCOIN', "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'FARTCOIN/USDT', "data_source": 'ccxt',      "binance_symbol": 'FARTCOINUSDT',  "yf_ticker": None,        "td_symbol": None,        "best_wr": 54.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'FET',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'FET/USDT',    "data_source": 'ccxt',      "binance_symbol": 'FETUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 53.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'FIL',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'FIL/USDT',    "data_source": 'ccxt',      "binance_symbol": 'FILUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 54.2,  "best_mode": 'strict_filter'},
    {"symbol": 'FLOKI',   "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'FLOKI/USDT',  "data_source": 'ccxt',      "binance_symbol": 'FLOKIUSDT',     "yf_ticker": None,        "td_symbol": None,        "best_wr": 53.1,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'JASMY',   "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'JASMY/USDT',  "data_source": 'ccxt',      "binance_symbol": 'JASMYUSDT',     "yf_ticker": None,        "td_symbol": None,        "best_wr": 64.5,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'KAS',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'KAS/USDT',    "data_source": 'ccxt',      "binance_symbol": 'KASUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 55.7,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'LINK',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'LINK/USDT',   "data_source": 'ccxt',      "binance_symbol": 'LINKUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 55.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'LTC',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'LTC/USDT',    "data_source": 'ccxt',      "binance_symbol": 'LTCUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 54.9,  "best_mode": 'strict_filter'},
    {"symbol": 'ONDO',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'ONDO/USDT',   "data_source": 'ccxt',      "binance_symbol": 'ONDOUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 55.8,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'OP',      "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'OP/USDT',     "data_source": 'ccxt',      "binance_symbol": 'OPUSDT',        "yf_ticker": None,        "td_symbol": None,        "best_wr": 50.6,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'ORDI',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'ORDI/USDT',   "data_source": 'ccxt',      "binance_symbol": 'ORDIUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 62.7,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'PENDLE',  "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'PENDLE/USDT', "data_source": 'ccxt',      "binance_symbol": 'PENDLEUSDT',    "yf_ticker": None,        "td_symbol": None,        "best_wr": 55.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'PIPPIN',  "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'PIPPIN/USDT', "data_source": 'ccxt',      "binance_symbol": 'PIPPINUSDT',    "yf_ticker": None,        "td_symbol": None,        "best_wr": 52.5,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'RENDER',  "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'RENDER/USDT', "data_source": 'ccxt',      "binance_symbol": 'RENDERUSDT',    "yf_ticker": None,        "td_symbol": None,        "best_wr": 50.8,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'RIVER',   "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'RIVER/USDT',  "data_source": 'ccxt',      "binance_symbol": 'RIVERUSDT',     "yf_ticker": None,        "td_symbol": None,        "best_wr": 53.7,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'SAND',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'SAND/USDT',   "data_source": 'ccxt',      "binance_symbol": 'SANDUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 73.4,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'SHIB',    "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'SHIB/USDT',   "data_source": 'ccxt',      "binance_symbol": 'SHIBUSDT',      "yf_ticker": None,        "td_symbol": None,        "best_wr": 68.0,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'SOL',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'SOL/USDT',    "data_source": 'ccxt',      "binance_symbol": 'SOLUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 52.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'THETA',   "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'THETA/USDT',  "data_source": 'ccxt',      "binance_symbol": 'THETAUSDT',     "yf_ticker": None,        "td_symbol": None,        "best_wr": 52.9,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'TRX',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'TRX/USDT',    "data_source": 'ccxt',      "binance_symbol": 'TRXUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 60.1,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'VIRTUAL', "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'VIRTUAL/USDT', "data_source": 'ccxt',      "binance_symbol": 'VIRTUALUSDT',   "yf_ticker": None,        "td_symbol": None,        "best_wr": 53.9,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'WLD',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'WLD/USDT',    "data_source": 'ccxt',      "binance_symbol": 'WLDUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 66.3,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'XLM',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'XLM/USDT',    "data_source": 'ccxt',      "binance_symbol": 'XLMUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 61.5,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'XRP',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'XRP/USDT',    "data_source": 'ccxt',      "binance_symbol": 'XRPUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 63.0,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'ZEC',     "category": 'CRY', "asset_class": 'crypto',    "platform_symbol": 'ZEC/USDT',    "data_source": 'ccxt',      "binance_symbol": 'ZECUSDT',       "yf_ticker": None,        "td_symbol": None,        "best_wr": 55.5,  "best_mode": 'smart_filter_opt'},

    # ======================================================================
    # COMMODITIES — 3 assets
    # ======================================================================
    {"symbol": 'USOIL',   "category": 'COM', "asset_class": 'commodity', "platform_symbol": 'USOIL/USD',   "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'CL=F',      "td_symbol": None,        "best_wr": 54.2,  "best_mode": 'strict_filter'},
    {"symbol": 'XAG',     "category": 'COM', "asset_class": 'gold',      "platform_symbol": 'XAG/USD',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'SI=F',      "td_symbol": 'XAG/USD',   "best_wr": 59.7,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'XAU',     "category": 'COM', "asset_class": 'gold',      "platform_symbol": 'XAU/USD',     "data_source": 'ccxt',      "binance_symbol": 'PAXGUSDT',      "yf_ticker": 'GC=F',      "td_symbol": 'XAU/USD',   "best_wr": 62.1,  "best_mode": 'smart_filter_opt'},

    # ======================================================================
    # FOREX — 11 assets
    # ======================================================================
    {"symbol": 'CADEUR',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'CAD/EUR',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'CADEUR=X',  "td_symbol": 'CAD/EUR',   "best_wr": 49.5,  "best_mode": 'smart_filter'},
    {"symbol": 'CADINR',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'CAD/INR',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'CADINR=X',  "td_symbol": 'CAD/INR',   "best_wr": 60.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'CADJPY',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'CAD/JPY',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'CADJPY=X',  "td_symbol": 'CAD/JPY',   "best_wr": 56.4,  "best_mode": 'smart_filter'},
    {"symbol": 'GBPAUD',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'GBP/AUD',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'GBPAUD=X',  "td_symbol": 'GBP/AUD',   "best_wr": 51.9,  "best_mode": 'smart_filter'},
    {"symbol": 'GBPCAD',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'GBP/CAD',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'GBPCAD=X',  "td_symbol": 'GBP/CAD',   "best_wr": 54.2,  "best_mode": 'smart_filter'},
    {"symbol": 'GBPJPY',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'GBP/JPY',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'GBPJPY=X',  "td_symbol": 'GBP/JPY',   "best_wr": 55.8,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'GBPNZD',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'GBP/NZD',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'GBPNZD=X',  "td_symbol": 'GBP/NZD',   "best_wr": 51.5,  "best_mode": 'smart_filter'},
    {"symbol": 'USDCAD',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'USD/CAD',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'USDCAD=X',  "td_symbol": 'USD/CAD',   "best_wr": 53.7,  "best_mode": 'smart_filter'},
    {"symbol": 'USDCHF',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'USD/CHF',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'USDCHF=X',  "td_symbol": 'USD/CHF',   "best_wr": 50.7,  "best_mode": 'smart_filter'},
    {"symbol": 'USDJPY',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'USD/JPY',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'USDJPY=X',  "td_symbol": 'USD/JPY',   "best_wr": 49.4,  "best_mode": 'smart_filter'},
    {"symbol": 'USDNZD',  "category": 'FOR', "asset_class": 'forex',     "platform_symbol": 'USD/NZD',     "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'USDNZD=X',  "td_symbol": 'USD/NZD',   "best_wr": 73.1,  "best_mode": 'strict_filter_opt'},

    # ======================================================================
    # STOCKS — 30 assets
    # ======================================================================
    {"symbol": 'AAL',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'AAL',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'AAL',       "td_symbol": 'AAL',       "best_wr": 58.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'AAPL',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'AAPL',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'AAPL',      "td_symbol": 'AAPL',      "best_wr": 57.1,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'AMD',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'AMD',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'AMD',       "td_symbol": 'AMD',       "best_wr": 56.7,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'AMZN',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'AMZN',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'AMZN',      "td_symbol": 'AMZN',      "best_wr": 57.7,  "best_mode": 'smart_filter'},
    {"symbol": 'AVGO',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'AVGO',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'AVGO',      "td_symbol": 'AVGO',      "best_wr": 58.8,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'BABA',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'BABA',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'BABA',      "td_symbol": 'BABA',      "best_wr": 48.5,  "best_mode": 'smart_filter'},
    {"symbol": 'BAC',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'BAC',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'BAC',       "td_symbol": 'BAC',       "best_wr": 53.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'BMY',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'BMY',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'BMY',       "td_symbol": 'BMY',       "best_wr": 53.4,  "best_mode": 'smart_filter'},
    {"symbol": 'C',       "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'C',           "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'C',         "td_symbol": 'C',         "best_wr": 59.6,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'CVX',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'CVX',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'CVX',       "td_symbol": 'CVX',       "best_wr": 63.5,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'GME',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'GME',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'GME',       "td_symbol": 'GME',       "best_wr": 54.5,  "best_mode": 'strict_filter'},
    {"symbol": 'GOOGL',   "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'GOOGL',       "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'GOOGL',     "td_symbol": 'GOOGL',     "best_wr": 54.5,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'HOOD',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'HOOD',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'HOOD',      "td_symbol": 'HOOD',      "best_wr": 60.1,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'HUT',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'HUT',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'HUT',       "td_symbol": 'HUT',       "best_wr": 59.5,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'META',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'META',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'META',      "td_symbol": 'META',      "best_wr": 45.5,  "best_mode": 'filter'},
    {"symbol": 'MSFT',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'MSFT',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'MSFT',      "td_symbol": 'MSFT',      "best_wr": 53.6,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'MSTR',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'MSTR',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'MSTR',      "td_symbol": 'MSTR',      "best_wr": 54.6,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'MU',      "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'MU',          "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'MU',        "td_symbol": 'MU',        "best_wr": 69.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'NFLX',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'NFLX',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'NFLX',      "td_symbol": 'NFLX',      "best_wr": 68.9,  "best_mode": 'strict_filter_opt'},
    {"symbol": 'NVDA',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'NVDA',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'NVDA',      "td_symbol": 'NVDA',      "best_wr": 44.2,  "best_mode": 'smart_filter'},
    {"symbol": 'ORCL',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'ORCL',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'ORCL',      "td_symbol": 'ORCL',      "best_wr": 57.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'PYPL',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'PYPL',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'PYPL',      "td_symbol": 'PYPL',      "best_wr": 54.9,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'QQQ',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'QQQ',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'QQQ',       "td_symbol": 'QQQ',       "best_wr": 50.2,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'SNAP',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'SNAP',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'SNAP',      "td_symbol": 'SNAP',      "best_wr": 55.6,  "best_mode": 'smart_filter'},
    {"symbol": 'SOFI',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'SOFI',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'SOFI',      "td_symbol": 'SOFI',      "best_wr": 55.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'T',       "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'T',           "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'T',         "td_symbol": 'T',         "best_wr": 52.1,  "best_mode": 'smart_filter'},
    {"symbol": 'TSLA',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'TSLA',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'TSLA',      "td_symbol": 'TSLA',      "best_wr": 51.0,  "best_mode": 'strict_filter'},
    {"symbol": 'UPST',    "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'UPST',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'UPST',      "td_symbol": 'UPST',      "best_wr": 54.8,  "best_mode": 'smart_filter'},
    {"symbol": 'WFC',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'WFC',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'WFC',       "td_symbol": 'WFC',       "best_wr": 60.3,  "best_mode": 'smart_filter_opt'},
    {"symbol": 'WMT',     "category": 'STK', "asset_class": 'stocks',    "platform_symbol": 'WMT',         "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'WMT',       "td_symbol": 'WMT',       "best_wr": 60.9,  "best_mode": 'strict_filter_opt'},

    # ======================================================================
    # FUTURES / INDICES — 3 assets
    # ======================================================================
    {"symbol": 'ES=F',    "category": 'IDX', "asset_class": 'futures',   "platform_symbol": 'US500',       "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'ES=F',      "td_symbol": None,        "best_wr": 54.1,  "best_mode": 'smart_filter'},
    {"symbol": 'NQ=F',    "category": 'IDX', "asset_class": 'futures',   "platform_symbol": 'NAS100',      "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'NQ=F',      "td_symbol": None,        "best_wr": 52.7,  "best_mode": 'smart_filter'},
    {"symbol": 'YM=F',    "category": 'IDX', "asset_class": 'futures',   "platform_symbol": 'US30',        "data_source": 'yfinance',  "binance_symbol": None,            "yf_ticker": 'YM=F',      "td_symbol": None,        "best_wr": 53.1,  "best_mode": 'smart_filter_opt'},
]


# -----------------------------------------------------------------------------
# Lookup indexes (built once at import)
# -----------------------------------------------------------------------------

# Resolve the v45.4.1 models base directory the same way the engine does.
_MODELS_BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "models_ml", "v45.4.1",
)

# Filter the universe down to assets that actually have a model dir on disk.
_AVAILABLE = []
for _entry in V45_ASSET_UNIVERSE:
    _mdir = os.path.join(_MODELS_BASE, _entry["symbol"], "final")
    if os.path.isdir(_mdir):
        _AVAILABLE.append(_entry)
    else:
        logger.debug("v45.4.1 model dir missing for %s (%s) -- excluded from live set" % (_entry["symbol"], _mdir))

# de-dup by symbol (the universe list is hand-maintained; guard against accidents)
_seen = set()
V45_ASSETS = []
for _e in _AVAILABLE:
    if _e["symbol"] in _seen:
        logger.warning("Duplicate v45.4.1 symbol '%s' in registry -- keeping first" % _e["symbol"])
        continue
    _seen.add(_e["symbol"])
    V45_ASSETS.append(_e)

# symbol -> entry
V45_BY_SYMBOL = {e["symbol"]: e for e in V45_ASSETS}

# platform_symbol -> entry  (the runner checks duplicates by platform_symbol)
V45_BY_PLATFORM = {e["platform_symbol"]: e for e in V45_ASSETS}


def get_v45_asset(symbol):
    """Look up an asset by its v45.4.1 model symbol (e.g. 'BTC')."""
    return V45_BY_SYMBOL.get(symbol)


def get_v45_assets_by_class(asset_class):
    """Return all available v45.4.1 assets for a given asset_class."""
    return [e for e in V45_ASSETS if e["asset_class"] == asset_class]


def all_v45_symbols():
    """Return the list of all v45.4.1 model symbols that have a model on disk."""
    return [e["symbol"] for e in V45_ASSETS]


# -----------------------------------------------------------------------------
# "BEST ASSETS" -- validated high-win-rate assets get a badge in signal cards.
# Threshold: best_wr >= 60% (from the static best-mode OOS split).
# The badge tier scales with win-rate so the strongest assets stand out.
# -----------------------------------------------------------------------------
BEST_ASSET_MIN_WR = 60.0


def is_best_asset(symbol):
    """True if this asset has a validated OOS win-rate >= BEST_ASSET_MIN_WR."""
    e = V45_BY_SYMBOL.get(symbol)
    if not e:
        return False
    return float(e.get("best_wr", 0.0)) >= BEST_ASSET_MIN_WR


def best_asset_tier(symbol):
    """
    Return a badge tier label for display.
      'elite'   : WR >= 80%
      'strong'  : WR >= 70%
      'solid'   : WR >= 60%
      ''        : not a best asset
    """
    e = V45_BY_SYMBOL.get(symbol)
    if not e:
        return ""
    wr = float(e.get("best_wr", 0.0))
    if wr >= 80.0:
        return "elite"
    if wr >= 70.0:
        return "strong"
    if wr >= 60.0:
        return "solid"
    return ""


def best_asset_wr(symbol):
    """Return the validated OOS win-rate (%) for an asset, or 0.0."""
    e = V45_BY_SYMBOL.get(symbol)
    if not e:
        return 0.0
    return float(e.get("best_wr", 0.0))


# -----------------------------------------------------------------------------
# Backward-compatible aliases (v43 -> v45.4.1 rename).
# A few call sites historically imported the v43-prefixed names directly;
# these aliases keep any such import working without hunting down every
# reference. New code should use the V45_* names above.
# -----------------------------------------------------------------------------
V43_ASSET_UNIVERSE = V45_ASSET_UNIVERSE
V43_ASSETS = V45_ASSETS
V43_BY_SYMBOL = V45_BY_SYMBOL
V43_BY_PLATFORM = V45_BY_PLATFORM
get_v43_asset = get_v45_asset
get_v43_assets_by_class = get_v45_assets_by_class
all_v43_symbols = all_v45_symbols
