"""SmAttaker — Strategies Package

Two independent strategy engines share the platform:

  1. V45.4.1 (APEX) — the unified ML strategy engine. It handles ALL asset
     classes (crypto, gold, commodities, forex, stocks, index futures) through
     one leak-free meta-labeling pipeline. strategy_type: "v45.4.1".

  2. Black Swan (strategy #2) — the SNIPER BODY NOLDN v22 production port
     (frozen RR/APEX book: 30m grid, resting-limit entries, DYNA/RATCHET
     exits, funding + daily-slope gates). strategy_type: "black_swan".

Both plug into the SAME BaseStrategy contract and the same Signal table.
Each has its own isolated runner (runner.py / black_swan_runner.py) and its
own scheduler job, so neither can break the other.
"""
from backend.strategies.base import BaseStrategy  # noqa: F401
from backend.strategies.v45_strategy.strategy import V45Strategy  # noqa: F401
from backend.strategies.black_swan_strategy.strategy import BlackSwanStrategy  # noqa: F401

__all__ = ["BaseStrategy", "V45Strategy", "BlackSwanStrategy"]
