"""
SmAttaker — ML Model Registry
Maps platform symbols to trained model files and configuration.
"""
from pathlib import Path
from typing import Optional

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # -> backend/
CRYPTO_MODELS_DIR = BASE_DIR / "models_ml" / "crypto"
AURUM_MODELS_DIR = BASE_DIR / "models_ml" / "aurum"

# ============================================================================
# CRYPTO — Singularity v40 (22 models, M30 / 30-minute data from Binance)
# ============================================================================
# Format: platform_symbol -> (model_filename, binance_symbol)
CRYPTO_REGISTRY = {
    # Major caps
    "BTC/USDT":   ("BTCUSDT_model.joblib",   "BTCUSDT"),
    "ETH/USDT":   ("ETHUSDT_model.joblib",   "ETHUSDT"),
    "BNB/USDT":   ("BNBUSDT_model.joblib",   "BNBUSDT"),
    "XRP/USDT":   ("XRPUSDT_model.joblib",   "XRPUSDT"),
    "SOL/USDT":   ("SOLUSDT_model.joblib",   "SOLUSDT"),
    "ADA/USDT":   (None, "ADAUSDT"),       # no M30 model trained
    "DOGE/USDT":  ("DOGEUSDT_model.joblib",  "DOGEUSDT"),
    "AVAX/USDT":  ("AVAXUSDT_model.joblib",  "AVAXUSDT"),
    "LINK/USDT":  ("LINKUSDT_model.joblib",  "LINKUSDT"),
    "POL/USDT":   ("MATICUSDT_model.joblib", "POLUSDT"),  # Polygon migrated MATIC→POL Sep 2024
    "LTC/USDT":   ("LTCUSDT_model.joblib",   "LTCUSDT"),
    "TRX/USDT":   ("TRXUSDT_model.joblib",   "TRXUSDT"),
    "ATOM/USDT":  ("ATOMUSDT_model.joblib",  "ATOMUSDT"),
    "XLM/USDT":   ("XLMUSDT_model.joblib",   "XLMUSDT"),
    # Mid caps / DeFi
    "NEAR/USDT":  ("NEARUSDT_model.joblib",  "NEARUSDT"),
    "OP/USDT":    ("OPUSDT_model.joblib",    "OPUSDT"),
    "ICP/USDT":   ("ICPUSDT_model.joblib",   "ICPUSDT"),
    "GALA/USDT":  ("GALAUSDT_model.joblib",  "GALAUSDT"),
    "RAY/USDT":   ("RAYUSDT_model.joblib",   "RAYUSDT"),
    "PEOPLE/USDT":("PEOPLEUSDT_model.joblib","PEOPLEUSDT"),
    "SPELL/USDT": ("SPELLUSDT_model.joblib", "SPELLUSDT"),
    "PENDLE/USDT":("PENDLEUSDT_model.joblib","PENDLEUSDT"),
    "SUI/USDT":   ("SUIUSDT_model.joblib",   "SUIUSDT"),
}

# ============================================================================
# GOLD / FOREX / STOCKS — Aurum Core v2 (16 models)
# ============================================================================
# Format: platform_symbol -> (model_filename, asset_class, yfinance_ticker, is_24h)
AURUM_REGISTRY = {
    # Gold
    "XAU/USD":  ("aurum_v2_XAUUSD_M30_model.joblib", "gold",   None,   True),

    # Forex — 7 major pairs
    "EUR/USD":  ("aurum_v2_EURUSD_M30_model.joblib", "forex",  None,   True),
    "GBP/USD":  ("aurum_v2_GBPUSD_M30_model.joblib", "forex",  None,   True),
    "EUR/AUD":  ("aurum_v2_EURAUD_M30_model.joblib", "forex",  None,   True),
    "EUR/GBP":  ("aurum_v2_EURGBP_M30_model.joblib", "forex",  None,   True),
    "USD/JPY":  ("aurum_v2_USDJPY_M30_model.joblib", "forex",  None,   True),
    "USD/CHF":  ("aurum_v2_USDCHF_M30_model.joblib", "forex",  None,   True),
    "USD/CAD":  ("aurum_v2_USDCAD_M30_model.joblib", "forex",  None,   True),
    # AUD/USD, NZD/USD — no model trained

    # Stocks — M30 data (30K+ bars, strong results)
    "AAPL":     ("aurum_v2_AAPL_M30_model.joblib",   "stocks", "AAPL", False),
    "TSLA":     ("aurum_v2_TSLA_M30_model.joblib",   "stocks", "TSLA", False),
    "NFLX":     ("aurum_v2_NFLX_M30_model.joblib",   "stocks", "NFLX", False),

    # Stocks — H1 data (5K bars, yfinance, preliminary)
    "GOOGL":    ("aurum_v2_GOOGL_H1_model.joblib",   "stocks", "GOOGL", False),
    "AMD":      ("aurum_v2_AMD_H1_model.joblib",     "stocks", "AMD",   False),
    "HP":       ("aurum_v2_HP_H1_model.joblib",      "stocks", "HPQ",   False),  # HPQ on Yahoo
    "NVDA":     ("aurum_v2_NVDA_H1_model.joblib",    "stocks", "NVDA",  False),
    "MSFT":     ("aurum_v2_MSFT_H1_model.joblib",    "stocks", "MSFT",  False),
}


def get_crypto_model_path(platform_symbol: str) -> Optional[Path]:
    """Get the crypto model file path for a platform symbol."""
    entry = CRYPTO_REGISTRY.get(platform_symbol)
    if entry and entry[0]:
        return CRYPTO_MODELS_DIR / entry[0]
    return None


def get_crypto_binance_symbol(platform_symbol: str) -> Optional[str]:
    """Get the Binance symbol for CCXT data fetching."""
    entry = CRYPTO_REGISTRY.get(platform_symbol)
    if entry and entry[1]:
        return entry[1]
    return None


def get_aurum_model_path(platform_symbol: str) -> Optional[Path]:
    """Get the Aurum model file path for a platform symbol."""
    entry = AURUM_REGISTRY.get(platform_symbol)
    if entry and entry[0]:
        return AURUM_MODELS_DIR / entry[0]
    return None


def get_aurum_asset_info(platform_symbol: str) -> Optional[tuple]:
    """Get (asset_class, yfinance_ticker, is_24h) for an Aurum symbol."""
    entry = AURUM_REGISTRY.get(platform_symbol)
    if entry:
        return (entry[1], entry[2], entry[3])  # asset_class, yf_ticker, is_24h
    return None


def get_all_supported_crypto_symbols() -> list[str]:
    """Return all crypto symbols that have trained models."""
    return [s for s, (f, _) in CRYPTO_REGISTRY.items() if f is not None]


def get_all_supported_aurum_symbols() -> list[str]:
    """Return all gold/forex/stock symbols that have trained models."""
    return list(AURUM_REGISTRY.keys())
