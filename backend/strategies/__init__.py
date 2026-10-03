"""SmAttaker — Strategies Package

Two strategy engines:

  1. V45.4.1 (APEX) — the unified ML engine: all asset classes (crypto,
     gold, commodities, forex, stocks, index futures) through one leak-free
     meta-labeling pipeline. strategy_type "v45.4.1". Hourly at :02 UTC.

  2. OMEGA QUANT — the certified three-sword M15 book (bit-exact port of
     the sealed OMEGA_QUANT_SYSTEM T2.10: MANDATE 4.542 / MIDDLE 4.769 /
     QUALITY 4.349, anchors bit-exact-proven; BE/TRAIL banned; certified
     cell long-only). strategy_type "omega_quant". Cron :02/:17/:32/:47
     UTC. Runs the certified line (BTC/ETH/XAU) frozen plus the ported
     universe behind a per-asset health guard. Replaces the retired
     BLACK SWAN book.
"""
from backend.strategies.base import BaseStrategy  # noqa: F401
from backend.strategies.v45_strategy.strategy import V45Strategy  # noqa: F401
from backend.strategies.omega_quant import OmegaQuantStrategy  # noqa: F401

__all__ = ["BaseStrategy", "V45Strategy", "OmegaQuantStrategy"]
