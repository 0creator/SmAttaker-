"""
SmAttaker — TradingView Symbol Mapping (STATIC layer, v45.4.7)
======================================================
Maps internal platform symbols (BTC, ETH/USDT, SNAP, USD/JPY, XAU/USD,
USOIL/USD, US500, etc.) to the exact TradingView symbol format the
chart widget expects — PLUS an ordered fallback chain for every asset.

v45.4.7 — VERIFIED AGAINST TRADINGVIEW'S OWN SYMBOL-SEARCH API:
  1. ⚠️ CRITICAL FIX — Gold/Silver on OANDA lost their underscores.
     The old 'OANDA:XAU_USD' / 'OANDA:XAG_USD' formats were RETIRED by
     TradingView → the Mini App chart showed "This symbol doesn't
     exist" for every gold signal (see Screenshot_20260826-164038).
     Verified correct formats (live API check):
        OANDA:XAUUSD   (Gold Spot / U.S. Dollar)
        OANDA:XAGUSD   (Silver Spot / U.S. Dollar)
  2. ⚠️ CRITICAL FIX — Crypto is NO LONGER hardwired to BINANCE.
     Four of the 35 v45.4.1 crypto assets (FARTCOIN, PIPPIN, KAS,
     RIVER) are NOT listed on Binance spot — the old resolver emitted
     'BINANCE:FARTCOINUSDT' → "This symbol doesn't exist" (see the
     FARTCOIN/PIPPIN screenshots). Data for these coins is fetched via
     the CCXT chain that starts at MEXC, and their verified TradingView
     spot venues are MEXC/KuCoin/Gate/etc. See tv_resolver.py for the
     smart runtime layer; the verified static overrides live here.
  3. FIX — Index futures: the v45.4.1 registry stores platform symbols
     'US500' / 'NAS100' / 'US30' (asset_class='futures'). The old code
     only recognised 'ES=F'-style names, so US500 fell through to the
     crypto branch and produced garbage like 'BINANCE:US500USDT'.
     Verified mappings now included.
  4. NEW — tv_fallback_chain(): every asset now returns an ordered list
     of alternate TradingView symbols (alternate exchange, alternate
     namespace, bare search term). The Mini App / API surface these so
     a delisted primary never leaves the user with a dead chart.

Production symbol formats (from model_registry.py):
  - Crypto:    platform_symbol = 'ETH/USDT', 'BTC/USDT'   (WITH slash)
  - Forex:     platform_symbol = 'GBP/JPY', 'USD/CAD'     (WITH slash)
  - Gold:      platform_symbol = 'XAU/USD'                (WITH slash)
  - Silver:    platform_symbol = 'XAG/USD'                (WITH slash)
  - Oil:       platform_symbol = 'USOIL/USD'              (WITH slash)
  - Stocks:    platform_symbol = 'AAPL', 'SNAP'           (NO slash)
  - Index fut: platform_symbol = 'US500', 'NAS100', 'US30' (also legacy 'ES=F')
"""
import logging
from typing import List, Optional

logger = logging.getLogger("smattaker.tv_symbols")


# ── Manual overrides — symbols whose TradingView prefix isn't the obvious one ──
#   (e.g. SNAP is NYSE not NASDAQ, BABA is NYSE, HOOD is NASDAQ, etc.)
#   Source: each company's actual primary listing exchange.
_STOCK_TV_EXCHANGE = {
    # NYSE-listed
    "BABA": "NYSE", "BAC": "NYSE", "BMY": "NYSE", "C": "NYSE",
    "CVX": "NYSE", "GME": "NYSE", "ORCL": "NYSE", "SAND": "NYSE",
    "SNAP": "NYSE",   # ⚠️ SNAP trades on NYSE, NOT NASDAQ
    "T": "NYSE", "WFC": "NYSE",
    # NASDAQ-listed
    "HOOD": "NASDAQ",  # Robinhood is NASDAQ
    "PYPL": "NASDAQ", "SOFI": "NASDAQ",
    "MU": "NASDAQ", "AAL": "NASDAQ", "AMD": "NASDAQ", "AMZN": "NASDAQ",
    "AAPL": "NASDAQ", "AVGO": "NASDAQ", "GOOGL": "NASDAQ",
    "MSTR": "NASDAQ", "META": "NASDAQ", "MSFT": "NASDAQ",
    "NFLX": "NASDAQ", "NVDA": "NASDAQ", "QQQ": "NASDAQ",
    "TSLA": "NASDAQ", "HUT": "NASDAQ", "RIVER": "NASDAQ",
    # ⚠️ v45.4.8 live-verified corrections (TV symbol-search, 2026-08-26):
    "UPST": "NASDAQ",  # was wrongly NYSE — Upstart trades on NASDAQ
    "WMT": "NASDAQ",  # was NYSE historically — Walmart MOVED to NASDAQ (Dec 2024)
}

# ── v45.4.7 VERIFIED crypto overrides ─────────────────────────────────
# Bases in the v45.4.1 universe that are NOT on Binance spot (checked
# against Binance exchangeInfo + TradingView symbol-search on 2026-08-26).
# Format: base -> (primary TV symbol, [fallback TV symbols])
# These venues carry real spot <BASE>USDT feeds on TradingView — exactly
# the exchanges the platform's CCXT data chain (MEXC → KuCoin → …) uses.
_CRYPTO_TV_OVERRIDES = {
    "FARTCOIN": ("MEXC:FARTCOINUSDT",
                 ["KUCOIN:FARTCOINUSDT", "GATEIO:FARTCOINUSDT",
                  "BITGET:FARTCOINUSDT", "BINANCE:FARTCOINUSDT.P"]),
    "PIPPIN":   ("MEXC:PIPPINUSDT",
                 ["BITGET:PIPPINUSDT", "HTX:PIPPINUSDT",
                  "GATEIO:PIPPINUSDT", "BINANCE:PIPPINUSDT.P"]),
    "KAS":      ("MEXC:KASUSDT",
                 ["KUCOIN:KASUSDT", "GATEIO:KASUSDT", "CRYPTO:KASUSD"]),
    "RIVER":    ("MEXC:RIVERUSDT",
                 ["BITGET:RIVERUSDT", "KRAKEN:RIVERUSD",
                  "BINANCE:RIVERUSDT.P"]),
}

# Exchange display names returned by TradingView's symbol-search API →
# the EXCHANGE: prefix the chart widget accepts. Used by tv_resolver.py.
_TV_EXCHANGE_PREFIX = {
    # crypto venues
    "Binance": "BINANCE", "BYBIT": "BYBIT", "Bybit": "BYBIT",
    "OKX": "OKX", "MEXC": "MEXC", "KuCoin": "KUCOIN",
    "Gate": "GATEIO", "GATE_IO": "GATEIO", "Bitget": "BITGET",
    "HTX": "HTX", "Huobi": "HTX", "Kraken": "KRAKEN",
    "Coinbase": "COINBASE", "BingX": "BINGX", "CoinEx": "COINEX",
    "Poloniex": "POLONIEX", "BloFin": "BLOFIN", "Bitunix": "BITUNIX",
    "Toobit": "TOOBIT", "WEEX": "WEEX", "LBank": "LBANK",
    "Phemex": "PHEMEX", "CRYPTO": "CRYPTO", "CRYPTOCAP": "CRYPTOCAP",
    # forex / CFD venues (v45.4.8 — live-verification ranking)
    "OANDA": "OANDA", "FOREX.com": "FOREXCOM", "FOREXCOM": "FOREXCOM",
    "FX_IDC": "FX_IDC", "FXCM": "FXCM", "Pepperstone": "PEPPERSTONE",
    "Saxo": "SAXO", "SAXO": "SAXO", "CMC Markets": "CMC",
    "ICE": "ICE", "Capital.com": "CAPITALCOM", "Eightcap": "EIGHTCAP",
    "MTS": "MTS", "TMS": "TMS", "FGCX": "FGCX",
    # stock venues
    "NASDAQ": "NASDAQ", "NYSE": "NYSE", "AMEX": "AMEX",
    "NYSE American": "AMEX", "NYSEARCA": "NYSEARCA", "BATS": "BATS",
}

# Crypto venues ranked by (TradingView feed reliability × liquidity).
# Used by tv_resolver.py to pick the best spot venue from live results.
_CRYPTO_EXCHANGE_PRIORITY = [
    "BINANCE", "BYBIT", "OKX", "MEXC", "KUCOIN", "GATEIO", "BITGET",
    "HTX", "KRAKEN", "COINBASE", "BINGX", "COINEX", "PHEMEX",
    "POLONIEX", "BLOFIN", "BITUNIX", "TOOBIT", "WEEX", "LBANK",
]

_CryptoExchange = "BINANCE"  # default crypto venue when no override/verification

# ── v45.4.8 VERIFIED forex layer ──────────────────────────────────────
# Live-checked against TradingView's own symbol-search API (2026-08-26)
# after CAD/INR produced a dead 'OANDA:CADINR' chart (see
# Screenshot_20260826-200643 — "This symbol doesn't exist").
#
# KEY FACT: OANDA carries only majors + common crosses. Exotic pairs are
# NOT on OANDA — they live on ICE / Saxo / CMC / FX_IDC:
#     CAD/INR → ICE:CADINR        (ONLY real feed — OANDA has none)
#     CAD/EUR → SAXO:CADEUR       (Saxo ranked first by TV's own search)
#     USD/NZD → ICE:USDNZD       (inverted pair; NZD/USD is the OANDA one)
#
# Pairs CONFIRMED on OANDA — safe to map without a network call:
_FOREX_OANDA_VERIFIED = {
    "CADJPY", "GBPAUD", "GBPCAD", "GBPJPY", "GBPNZD",
    "USDCAD", "USDCHF", "USDJPY",
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "EURGBP", "EURJPY",
}

# Exotic / inverted registry pairs → their VERIFIED actual TV venues.
# Format: bare pair -> (primary TV symbol, [verified fallback symbols])
_FOREX_EXOTIC_TO_TV = {
    "CADEUR": ("SAXO:CADEUR", ["ICE:CADEUR", "CMC:CADEUR"]),
    "CADINR": ("ICE:CADINR",  ["CADINR"]),            # bare term → TV search
    "USDNZD": ("ICE:USDNZD",  ["CMC:USDNZD", "USDNZD"]),
}

# Venue ranking for LIVE forex verification (tv_resolver.py). Anything
# found here is a real, widget-embeddable forex feed.
_FOREX_VENUE_PRIORITY = [
    "OANDA", "FOREXCOM", "FX_IDC", "FXCM", "PEPPERSTONE",
    "SAXO", "CMC", "ICE", "CAPITALCOM", "EIGHTCAP", "MTS", "FGCX",
]

# Venue ranking for LIVE stock-ticker verification (tv_resolver.py).
_STOCK_VENUE_PRIORITY = ["NASDAQ", "NYSE", "AMEX", "NYSEARCA", "BATS"]


def _is_index_future(symbol: str) -> bool:
    """Detect yfinance-style index futures like 'ES=F', 'NQ=F', 'YM=F'."""
    return bool(symbol) and "=" in symbol


# v45.4.1 futures platform symbols (model_registry) → verified TV symbols.
# Primary venue: OANDA CFDs (24/5 feed, tight spread mirror of the
# futures the strategy trades). Fallbacks include the TV-native index
# feeds which never delist.
_INDEX_PLATFORM_TO_TV = {
    "US500":  ("OANDA:SPX500USD", ["TVC:SP500", "PEPPERSTONE:US500"]),
    "NAS100": ("OANDA:NAS100USD", ["NASDAQ:NDX", "PEPPERSTONE:NAS100"]),
    "US30":   ("TVC:DJI",         ["PEPPERSTONE:US30", "FOREXCOM:US30"]),
}

# Legacy yfinance-style futures names (kept for backward compatibility)
_LEGACY_FUTURE_TO_TV = {
    "ES=F": ("CME_MINI:ES1!",  ["TVC:SP500",  "OANDA:SPX500USD"]),
    "NQ=F": ("CME_MINI:NQ1!",  ["NASDAQ:NDX", "OANDA:NAS100USD"]),
    "YM=F": ("CBOT_MINI:YM1!", ["TVC:DJI",    "PEPPERSTONE:US30"]),
}


def _is_forex_pair(symbol: str) -> bool:
    """Heuristic: 6-char USDJPY-style OR slash-formatted USD/JPY.

    TradingView accepts 'OANDA:USDJPY' (no slash, no underscore).
    The slash form 'FX:USD/JPY' is REJECTED by TradingView.
    """
    if not symbol:
        return False
    sym = symbol.strip().upper()
    fiats = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "INR"}
    # Format 1: 'USDJPY' (6 chars, no slash)
    if len(sym) == 6 and sym[:3] in fiats and sym[3:] in fiats:
        return True
    # Format 2: 'USD/JPY' (7 chars with slash)
    if "/" in sym:
        parts = sym.split("/")
        if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) == 3 \
                and parts[0] in fiats and parts[1] in fiats:
            return True
    return False


def _is_gold_silver(symbol: str) -> bool:
    """Detect XAU/XAG-style gold & silver tickers (with or without /USD)."""
    if not symbol:
        return False
    sym = symbol.strip().upper()
    return sym in ("XAU", "XAG") or sym.startswith("XAU/") or sym.startswith("XAG/")


def _is_oil(symbol: str) -> bool:
    """Detect crude oil ticker (USOIL or USOIL/USD)."""
    if not symbol:
        return False
    sym = symbol.strip().upper()
    return sym in ("USOIL", "UKOIL", "WTI") or sym.startswith(("USOIL/", "UKOIL/"))


def _strip_quote_suffix(symbol: str) -> str:
    """Strip a /USDT or /USD quote suffix from a crypto/forex symbol.

    'ETH/USDT' → 'ETH',  'BTC/USDT' → 'BTC',  'ETH' → 'ETH'
    """
    if not symbol:
        return ""
    sym = symbol.strip().upper()
    if "/" in sym:
        return sym.split("/")[0]
    return sym


def _crypto_base(symbol: str) -> str:
    """Crypto base ticker: 'FARTCOIN/USDT' → 'FARTCOIN', 'FARTCOINUSDT' → 'FARTCOIN'."""
    base = _strip_quote_suffix(symbol)
    if base.endswith("USDT") and len(base) > 4:
        base = base[:-4]
    return base


def _crypto_to_tv(symbol: str) -> str:
    """Map a crypto symbol to its TradingView format (static layer).

    'ETH/USDT'      → 'BINANCE:ETHUSDT'
    'FARTCOIN/USDT' → 'MEXC:FARTCOINUSDT'   (verified override — not on Binance)
    'BTC/USDT'      → 'BINANCE:BTCUSDT'
    """
    base = _crypto_base(symbol)
    if base in _CRYPTO_TV_OVERRIDES:
        return _CRYPTO_TV_OVERRIDES[base][0]
    return f"{_CryptoExchange}:{base}USDT"


def _forex_to_tv(symbol: str) -> str:
    """Map a forex pair to its VERIFIED TradingView format (v45.4.8).

    'GBP/JPY'  → 'OANDA:GBPJPY'   (majors/crosses confirmed on OANDA)
    'CAD/INR'  → 'ICE:CADINR'     (exotic — OANDA does NOT list it!)
    'CAD/EUR'  → 'SAXO:CADEUR'
    'USD/NZD'  → 'ICE:USDNZD'
    unknown    → 'FX_IDC:<PAIR>'  (widest fiat coverage; the resolver
                                  live-verifies this before it reaches a chart)

    NOTE: no underscores, no slashes — 'OANDA:GBP_JPY' and 'FX:GBP/JPY'
    are both rejected by TradingView.
    """
    sym = symbol.strip().upper().replace("/", "")
    if sym in _FOREX_OANDA_VERIFIED:
        return f"OANDA:{sym}"
    if sym in _FOREX_EXOTIC_TO_TV:
        return _FOREX_EXOTIC_TO_TV[sym][0]
    # Unverified pair — FX_IDC is TradingView's widest fiat namespace.
    # tv_resolver.py live-verifies the guess (24h cache) before display.
    return f"FX_IDC:{sym}"


def _gold_to_tv(symbol: str) -> str:
    """'XAU/USD' → 'OANDA:XAUUSD'  (v45.4.7: underscore format RETIRED by TV)."""
    return "OANDA:XAUUSD"


def _silver_to_tv(symbol: str) -> str:
    """'XAG/USD' → 'OANDA:XAGUSD'  (v45.4.7: underscore format RETIRED by TV)."""
    return "OANDA:XAGUSD"


def _oil_to_tv(symbol: str) -> str:
    """Map oil symbol to its TradingView format."""
    sym = symbol.strip().upper()
    if sym.startswith("UKOIL"):
        return "TVC:UKOIL"
    return "TVC:USOIL"  # also covers WTI alias


# ── Public API ─────────────────────────────────────────────────────────

def map_symbol_to_tv(symbol: str, asset_class: Optional[str] = None) -> str:
    """Convert an internal platform symbol to its (verified) TradingView symbol.

    Examples:
        >>> map_symbol_to_tv("ETH/USDT", "crypto")
        'BINANCE:ETHUSDT'
        >>> map_symbol_to_tv("FARTCOIN/USDT", "crypto")
        'MEXC:FARTCOINUSDT'
        >>> map_symbol_to_tv("SNAP", "stocks")
        'NYSE:SNAP'
        >>> map_symbol_to_tv("GBP/JPY", "forex")
        'OANDA:GBPJPY'
        >>> map_symbol_to_tv("XAU/USD", "gold")
        'OANDA:XAUUSD'
        >>> map_symbol_to_tv("USOIL/USD", "commodity")
        'TVC:USOIL'
        >>> map_symbol_to_tv("US500", "futures")
        'OANDA:SPX500USD'
        >>> map_symbol_to_tv("ES=F")
        'CME_MINI:ES1!'
    """
    if not symbol:
        return ""

    sym = symbol.strip().upper()
    cls = (asset_class or "").lower()

    # ── Futures / indices — v45.4.1 platform names FIRST (US500/NAS100/US30),
    #    then legacy yfinance 'ES=F' style ──
    if sym in _INDEX_PLATFORM_TO_TV:
        return _INDEX_PLATFORM_TO_TV[sym][0]
    if _is_index_future(sym):
        if sym in _LEGACY_FUTURE_TO_TV:
            return _LEGACY_FUTURE_TO_TV[sym][0]
        return f"TVC:{sym.replace('=F', '')}"
    if cls in ("futures", "index", "indices"):
        # Unknown futures platform symbol — try the index map, then TVC bare
        return _INDEX_PLATFORM_TO_TV.get(sym, (f"TVC:{sym}", []))[0]

    # ── Gold / Silver ── (XAU or XAU/USD)
    if _is_gold_silver(sym) or cls == "gold":
        if sym.startswith("XAG") or sym == "XAG":
            return _silver_to_tv(sym)
        return _gold_to_tv(sym)

    # ── Oil / commodity ──
    if _is_oil(sym) or cls == "commodity":
        return _oil_to_tv(sym)

    # ── Forex pairs (USDJPY, USD/JPY, GBP/JPY, etc.) ──
    # Must be checked BEFORE crypto default — otherwise 'USD/JPY'
    # would fall into the crypto branch and become 'BINANCE:USDUSDT'!
    if _is_forex_pair(sym) or cls == "forex":
        return _forex_to_tv(sym)

    # ── Stocks ──
    if cls == "stocks":
        exchange = _STOCK_TV_EXCHANGE.get(sym, "NASDAQ")
        return f"{exchange}:{sym}"
    if sym in _STOCK_TV_EXCHANGE:
        return f"{_STOCK_TV_EXCHANGE[sym]}:{sym}"

    # ── Crypto default ──
    if cls == "crypto" or cls == "":
        return _crypto_to_tv(sym)

    # Final fallback: bare symbol — TradingView will search its database
    # and show the most popular match.
    return sym


def tv_fallback_chain(symbol: str, asset_class: Optional[str] = None) -> List[str]:
    """Ordered alternate TradingView symbols to try if the primary fails.

    v45.4.7: replaces the old ad-hoc fallback lists that were built inside
    api/signals.py (which still contained the dead 'BINANCE:<base>BUSD'
    quote — BUSD was deprecated by Binance and carries no feed).

    Returns a list WITHOUT the primary symbol (the caller prepends it).
    May be empty for exotic inputs — that's fine.
    """
    if not symbol:
        return []

    sym = symbol.strip().upper()
    cls = (asset_class or "").lower()

    # Futures / indices
    if sym in _INDEX_PLATFORM_TO_TV:
        return list(_INDEX_PLATFORM_TO_TV[sym][1])
    if _is_index_future(sym) and sym in _LEGACY_FUTURE_TO_TV:
        return list(_LEGACY_FUTURE_TO_TV[sym][1])

    # Gold / Silver
    if _is_gold_silver(sym) or cls == "gold":
        if sym.startswith("XAG") or sym == "XAG":
            return ["FOREXCOM:XAGUSD", "FXCM:XAGUSD", "XAG/USD"]
        return ["TVC:GOLD", "FOREXCOM:XAUUSD", "FXCM:XAUUSD"]

    # Oil
    if _is_oil(sym) or cls == "commodity":
        if sym.startswith("UKOIL"):
            return ["PEPPERSTONE:UKOIL", "FXCM:UKOIL"]
        return ["FXCM:USOIL", "FOREXCOM:USOIL", "PEPPERSTONE:USOIL"]

    # Forex — v45.4.8: verified exotic chains; FX: prefix was dead
    if _is_forex_pair(sym) or cls == "forex":
        bare = sym.replace("/", "")
        if bare in _FOREX_EXOTIC_TO_TV:
            return list(_FOREX_EXOTIC_TO_TV[bare][1])
        if bare in _FOREX_OANDA_VERIFIED:
            return [f"FOREXCOM:{bare}", f"FX_IDC:{bare}"]
        return [f"OANDA:{bare}", f"FOREXCOM:{bare}", bare]

    # Stocks — alternate exchange guess, then bare ticker
    if cls == "stocks" or sym in _STOCK_TV_EXCHANGE:
        known = _STOCK_TV_EXCHANGE.get(sym)
        if known == "NYSE":
            return [f"NASDAQ:{sym}"]
        if known == "NASDAQ":
            return [f"NYSE:{sym}"]
        return [f"NASDAQ:{sym}", f"NYSE:{sym}"]

    # Crypto — verified override chain, else BINANCE → big venue perps
    base = _crypto_base(sym)
    if base in _CRYPTO_TV_OVERRIDES:
        return list(_CRYPTO_TV_OVERRIDES[base][1])
    if cls in ("crypto", ""):
        return [
            f"BINANCE:{base}USDT.P",   # Binance perpetual (many alts only exist here)
            f"MEXC:{base}USDT",        # MEXC spot — widest alt listing on TV
            f"KUCOIN:{base}USDT",
            f"{base}USDT",             # bare — let TradingView search
        ]
    return []


def get_tv_search_symbol(symbol: str, asset_class: Optional[str] = None) -> str:
    """Fallback search query if the mapped symbol fails.

    Bare ticker/query — TradingView's widget search (or the "open on
    TradingView" link) resolves it to the most popular matching symbol.
    """
    if not symbol:
        return ""
    sym = symbol.strip().upper()
    cls = (asset_class or "").lower()

    if sym in _INDEX_PLATFORM_TO_TV:
        return {"US500": "S&P 500", "NAS100": "Nasdaq 100", "US30": "Dow Jones"}[sym]
    if _is_index_future(sym):
        return {
            "ES=F": "S&P 500 Futures",
            "NQ=F": "NASDAQ 100 Futures",
            "YM=F": "Dow Jones Futures",
        }.get(sym, sym)

    if _is_gold_silver(sym):
        return "XAUUSD" if sym.startswith("XAU") else "XAGUSD"

    if _is_oil(sym):
        return "US Oil" if sym.startswith("USOIL") else "UK Oil"

    if _is_forex_pair(sym) or cls == "forex":
        return sym.replace("/", "")

    if cls == "crypto" or cls == "":
        return _crypto_base(sym)

    return sym
