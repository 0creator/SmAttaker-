"""SmAttaker — V43 Strategy Package.

A single ML strategy engine that handles ALL asset classes
(crypto, gold, forex, stocks) through the leak-free V43
meta-labeling pipeline.
"""
from backend.strategies.v43_strategy.strategy import V43Strategy  # noqa: F401

__all__ = ["V43Strategy"]
