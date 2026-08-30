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
  4. `test_connection` now tolerates CloudFront 403 geo-block errors.
     Bybit's CloudFront distribution is configured to block access from
     certain server regions (including Render's US-based IPs). This is
     a NETWORK restriction — it tells us NOTHING about whether the
     user's API credentials are valid. Previously we marked the
     connection as "error" and the user saw a scary red banner, even
     though their credentials might be perfectly fine. Now we recognize
     the CloudFront geo-block signature and treat it as a SOFT WARNING:
     the connection is saved (so trades can still be attempted later),
     the user sees a yellow "saved but server-side test was blocked"
     notice, and the credentials remain usable for any future call
     that happens to succeed (e.g. when the server's IP rotates, or
     when running locally from a non-blocked region).
  5. Constructor now passes `options.fetchCurrencies = False` to the
     CCXT exchange config. CCXT's Bybit driver, during `load_markets()`,
     calls `fetch_currencies()` which hits `/v5/asset/coin/query-info`
     — and that endpoint is the one CloudFront geo-blocks first. We
     don't actually need currency metadata for our use case (we deal
     in symbols like BTC/USDT, not deposit/withdrawal coin info), so
     disabling it both speeds up init AND avoids the specific endpoint
     that was triggering the 403 in the connection test.
"""
import asyncio
import ccxt
import logging
from typing import Optional, Dict, Any
from backend.utils.security import decrypt_api_key

logger = logging.getLogger("smattaker.exchange")


def _is_geo_block_error(err: Exception) -> bool:
    """Detect whether an CCXT/HTTP error is a CloudFront-style geo-block.

    Bybit's CloudFront distribution returns a 403 with a body like:
        {"error": "The Amazon CloudFront distribution is configured to
                   block access from your country."}
    when the calling IP is in a restricted region. This is NOT an auth
    failure — credentials are still potentially valid — so callers
    should treat it differently from a real 401/403.

    Also detects Binance's HTTP 451 "Service unavailable from a
    restricted location" regulatory block, since the same logic applies.
    """
    err_str = str(err).lower()
    # CloudFront signature (Bybit, and any other CloudFront-fronted API)
    if "cloudfront" in err_str and "block" in err_str:
        return True
    if "block access from your country" in err_str:
        return True
    # Binance 451 regulatory block
    if "451" in err_str and ("restricted" in err_str or "service unavailable" in err_str):
        return True
    # Generic geo-block phrasing
    if "geo-block" in err_str or "geo block" in err_str or "restricted location" in err_str:
        return True
    # Check the underlying HTTP status code if CCXT attached one
    http_status = getattr(err, "http_status", None) or getattr(err, "status_code", None)
    if http_status == 451:
        return True
    return False


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
            "options": {
                "defaultType": "swap",  # for futures
                # ⚠️ FIX: CCXT's Bybit driver calls `/v5/asset/coin/query-info`
                # during `load_markets()` to fetch currency metadata — and that
                # endpoint is the one Bybit's CloudFront geo-blocks FIRST. We
                # don't need currency metadata (we trade by symbol, not by
                # deposit/withdrawal coin), so disabling `fetchCurrencies`
                # both speeds up initialization AND avoids triggering the 403
                # during the connection test. Same applies to other exchanges
                # where CCXT may attempt similar geo-restricted metadata calls.
                "fetchCurrencies": False,
            },
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
        """Test if the exchange connection works.

        ⚠️ Geo-block tolerance: Bybit's CloudFront distribution is
        configured to block access from certain server regions
        (including Render's US-based IPs and several cloud providers).
        When that happens, the test returns a "soft success" — the
        credentials are saved and the connection is marked as "ok",
        but the user is shown a yellow warning explaining that the
        server couldn't reach the exchange from its region. The
        credentials may still be perfectly valid; the user can verify
        by running the bot from a different region or testing locally.
        """
        try:
            await asyncio.to_thread(self.exchange.fetch_time)
            return {"success": True, "message": "Connection successful"}
        except Exception as e:
            if _is_geo_block_error(e):
                logger.warning(
                    f"⚠️ {self.exchange_name}: connection test hit a geo-block "
                    f"from this server region — credentials saved anyway. Error: {e}"
                )
                return {
                    "success": True,  # Soft success — credentials saved
                    "warning": (
                        f"The server couldn't reach {self.exchange_name.title()} "
                        f"from its current region (geo-block). Your credentials "
                        f"were saved and may still be valid — try a small test "
                        f"trade to confirm, or run the bot from a different region."
                    ),
                    "geo_blocked": True,
                }
            logger.error(f"Connection test failed ({self.exchange_name}): {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def get_supported_exchanges() -> list:
        """Return list of supported exchange names."""
        return list(ExchangeConnector.EXCHANGE_CLASS_MAP.keys())
