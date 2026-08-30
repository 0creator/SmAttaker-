"""SmAttaker — V45 Strategy Package.

A single ML strategy engine that handles ALL asset classes
(crypto, gold, forex, stocks) through the leak-free V45
meta-labeling pipeline.
"""
from backend.strategies.v45_strategy.strategy import V45Strategy  # noqa: F401

__all__ = ["V45Strategy"]
