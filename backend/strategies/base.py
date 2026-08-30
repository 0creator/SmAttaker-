"""
SmAttaker — Strategy Engine Base
Integration point for ML-based trading strategies.
Your Python/ML scripts plug in here.
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger("smattaker.strategy")


class BaseStrategy(ABC):
    """Base class for all trading strategies."""

    strategy_type: str = "base"
    strategy_version: str = "1.0.0"
    asset_class: str = "unknown"

    @abstractmethod
    async def analyze(self, symbols: list[str]) -> list[dict]:
        """
        Analyze symbols and return signals.
        Returns list of signal dicts:
        [
            {
                "symbol": "BTC/USDT",
                "direction": "long",
                "entry_price": 67250.0,
                "stop_loss": 66500.0,
                "take_profit_levels": [...],
                "confidence_score": 87.5,
                "entry_time": "2026-07-10T16:30:00Z",
            },
        ]
        """
        pass

    @abstractmethod
    async def load_model(self):
        """Load ML model / indicators."""
        pass

    def validate_signal(self, signal: dict) -> bool:
        """Basic signal validation."""
        required = ["symbol", "direction", "entry_price", "stop_loss"]
        for field in required:
            if field not in signal:
                logger.warning(f"Signal missing required field: {field}")
                return False
        if signal["direction"] not in ("long", "short"):
            return False
        if signal["entry_price"] <= 0 or signal["stop_loss"] <= 0:
            return False
        return True
