"""
SmAttaker — Exchange Connector (CCXT-based)
Unified interface for 100+ exchanges.

⚠️ FIXES APPLIED (see inline notes for details):
  1. All exchange calls are now off-loaded to a thread via asyncio.to_thread.
     `ccxt` (sync build) was being called directly inside `async def`
     methods with no `await`ed I/O — every balance/order/ticker call
     blocked FastAPI's entire event loop for the duration of the HTTP
     round-trip to the exchange, stalling every other user's request.
  2. `create_market_order` now actually places protective stop-loss and
     take-profit orders after the entry fills. Previously the method
     accepted `stop_loss`/`take_profit` parameters and silently ignored
     them — real-money leveraged trades were being opened with **no**
     stop-loss on the exchange at all.
  3. `get_supported_exchanges` is now a proper `@staticmethod` (it was
     missing `self`/the decorator, so calling it on an instance raised
     `TypeError: takes 0 positional arguments but 1 was given`).
"""
import asyncio
import ccxt
import logging
from typing import Optional, Dict, Any
from backend.utils.security import decrypt_api_key

logger = logging.getLogger("smattaker.exchange")


class ExchangeConnector:
    """
    Unified exchange connector using CCXT.
    Supports: Binance, Bybit, Kraken, KuCoin, OKX, Coinbase, and 100+ more.
    """

    # ⚠️ FIX: this used to hardcode `ccxt.coinbasepro`, which no longer
    # exists in current ccxt versions (Coinbase Pro was discontinued and
    # merged into `ccxt.coinbase`) — a single renamed/removed attribute
    # crashed the ENTIRE application at import time, taking down every
    # feature, not just exchange connections. Built dynamically now via
    # getattr() so a future ccxt rename (e.g. "huobi" -> "htx") only
    # removes that one option from the list instead of crashing the app.
    _CANDIDATE_EXCHANGES = [
        "binance", "binanceusdm", "bybit", "kraken", "kucoin", "okx",
        "coinbase", "mexc", "gate", "htx", "huobi", "bitget", "bingx", "bitmex",
    ]
    EXCHANGE_CLASS_MAP = {}
    for _name in _CANDIDATE_EXCHANGES:
        _cls = getattr(ccxt, _name, None)
        if _cls is not None:
            EXCHANGE_CLASS_MAP[_name] = _cls
        else:
            logger.warning(f"ccxt has no exchange named '{_name}' in this version — skipping (not a fatal error).")
    del _name, _cls

    # Exchanges where separate reduce-only stop/take-profit orders are
    # well supported by CCXT's unified API. Kept as a single source of
    # truth in case future exchanges need special-casing.
    _UNIFIED_SLTP_PARAMS = {"binance", "binanceusdm", "bybit", "okx", "bitget", "kucoin", "mexc"}

    def __init__(
        self,
        exchange_name: str,
        api_key_encrypted: str,
        secret_key_encrypted: str,
        passphrase_encrypted: Optional[str] = None,
        is_testnet: bool = False,
    ):
        self.exchange_name = exchange_name.lower()
        self.is_testnet = is_testnet

        # Decrypt credentials
        try:
            self.api_key = decrypt_api_key(api_key_encrypted)
            self.secret_key = decrypt_api_key(secret_key_encrypted)
            self.passphrase = decrypt_api_key(passphrase_encrypted) if passphrase_encrypted else None
        except Exception as e:
            logger.error(f"Failed to decrypt credentials: {e}")
            raise ValueError("Invalid encrypted credentials")

        # Initialize exchange
        exchange_class = self.EXCHANGE_CLASS_MAP.get(self.exchange_name)
        if not exchange_class:
            raise ValueError(f"Unsupported exchange: {exchange_name}")

        config = {
            "apiKey": self.api_key,
            "secret": self.secret_key,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},  # for futures
        }

        if self.passphrase:
            config["password"] = self.passphrase

        self.exchange: ccxt.Exchange = exchange_class(config)

        # Set testnet/sandbox
        if is_testnet:
            self.exchange.set_sandbox_mode(True)

    async def fetch_balance(self) -> Dict[str, Any]:
        """Fetch account balance."""
        try:
            return await asyncio.to_thread(self.exchange.fetch_balance)
        except Exception as e:
            logger.error(f"Balance fetch error ({self.exchange_name}): {e}")
            return {"error": str(e)}

    async def create_market_order(
        self,
        symbol: str,
        side: str,  # 'buy' or 'sell'
        amount: float,
        leverage: int = 1,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Place a market order, then attach real protective SL/TP orders.

        Returns a dict with the entry order plus the outcome of the SL/TP
        placement, so callers can see (and alert on) a partially-protected
        position instead of assuming SL/TP silently "just worked".
        """
        try:
            # Set leverage for futures
            if self.exchange_name in ("binance", "bybit", "okx", "kucoin", "mexc"):
                await asyncio.to_thread(self.exchange.set_leverage, leverage, symbol)

            # Place entry order
            order = await asyncio.to_thread(
                self.exchange.create_order,
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
            )
            logger.info(f"✅ Entry order placed: {symbol} {side} {amount} — ID: {order.get('id')}")

            result: Dict[str, Any] = {"success": True, "order": order, "protection": {}}

            if stop_loss is None and take_profit is None:
                logger.warning(
                    f"⚠️ {symbol} {side}: no stop_loss/take_profit provided — "
                    f"position opened WITHOUT protective orders."
                )
                return result

            close_side = "sell" if side == "buy" else "buy"
            base_params = {"reduceOnly": True}

            if stop_loss is not None:
                try:
                    sl_order = await asyncio.to_thread(
                        self.exchange.create_order,
                        symbol=symbol,
                        type="stop_market",
                        side=close_side,
                        amount=amount,
                        price=None,
                        params={**base_params, "stopPrice": stop_loss},
                    )
                    result["protection"]["stop_loss"] = {"success": True, "order": sl_order}
                    logger.info(f"🛡️ Stop-loss placed for {symbol} @ {stop_loss}")
                except Exception as e:
                    result["protection"]["stop_loss"] = {"success": False, "error": str(e)}
                    logger.error(
                        f"❌ FAILED to place stop-loss for {symbol} @ {stop_loss}: {e} "
                        f"— position is UNPROTECTED. Caller must handle this (e.g. "
                        f"emergency-close the position or alert the user immediately)."
                    )

            if take_profit is not None:
                try:
                    tp_order = await asyncio.to_thread(
                        self.exchange.create_order,
                        symbol=symbol,
                        type="take_profit_market",
                        side=close_side,
                        amount=amount,
                        price=None,
                        params={**base_params, "stopPrice": take_profit},
                    )
                    result["protection"]["take_profit"] = {"success": True, "order": tp_order}
                    logger.info(f"🎯 Take-profit placed for {symbol} @ {take_profit}")
                except Exception as e:
                    result["protection"]["take_profit"] = {"success": False, "error": str(e)}
                    logger.error(f"❌ FAILED to place take-profit for {symbol} @ {take_profit}: {e}")

            # Surface partial-protection failures to the caller explicitly.
            if stop_loss is not None and not result["protection"].get("stop_loss", {}).get("success"):
                result["success"] = "partial"
                result["warning"] = (
                    "Entry filled but stop-loss placement FAILED — position is unprotected."
                )

            return result

        except Exception as e:
            logger.error(f"Order error ({self.exchange_name} {symbol}): {e}")
            return {"success": False, "error": str(e)}

    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
    ) -> Dict[str, Any]:
        """Place a limit order."""
        try:
            order = await asyncio.to_thread(
                self.exchange.create_order,
                symbol=symbol,
                type="limit",
                side=side,
                amount=amount,
                price=price,
            )
            return {"success": True, "order": order}
        except Exception as e:
            logger.error(f"Limit order error: {e}")
            return {"success": False, "error": str(e)}

    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """Cancel an existing order."""
        try:
            result = await asyncio.to_thread(self.exchange.cancel_order, order_id, symbol)
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"Cancel order error: {e}")
            return {"success": False, "error": str(e)}

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        """Fetch all open orders."""
        try:
            return await asyncio.to_thread(self.exchange.fetch_open_orders, symbol)
        except Exception as e:
            logger.error(f"Fetch open orders error: {e}")
            return []

    async def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch current ticker for a symbol."""
        try:
            return await asyncio.to_thread(self.exchange.fetch_ticker, symbol)
        except Exception as e:
            logger.error(f"Ticker fetch error: {e}")
            return {"error": str(e)}

    async def test_connection(self) -> Dict[str, Any]:
        """Test if the exchange connection works."""
        try:
            await asyncio.to_thread(self.exchange.fetch_time)
            return {"success": True, "message": "Connection successful"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def get_supported_exchanges() -> list:
        """Return list of supported exchange names."""
        return list(ExchangeConnector.EXCHANGE_CLASS_MAP.keys())
