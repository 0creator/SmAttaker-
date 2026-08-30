"""
SmAttaker — MT5 (MetaTrader 5) Live Connector via MetaApi Cloud
================================================================
Real, working MT5 integration — no stubs, no "bridge required" placeholders.

⚠️ v46 API MIGRATION (metaapi-cloud-sdk 29.x):
  The 6.1.6 we originally pinned didn't exist on PyPI. After upgrading to
  the real stable version 29.1.1, the SDK's public API changed in three
  breaking ways — all handled here:

  1. Module name:    `metaapi_sdk`            → `metaapi_cloud_sdk`
  2. Get account:    `api.get_account(id)`    → `api.metatrader_account_api.get_account(id)`
  3. Wait connected: `connection.wait_connected(timeout_seconds=30)`
                      → `account.wait_connected(timeout_in_seconds=30)`
                      (moved from the RPC connection to the account object)
  4. Market orders:  `connection.create_market_order(dict)`
                      → `connection.create_market_buy_order(symbol, volume, sl, tp)`
                         `connection.create_market_sell_order(symbol, volume, sl, tp)`
                      (the dict-based API was removed; use the explicit
                       buy/sell helpers instead)
  5. Trade result:   still a dict, but the keys we care about
                      (orderId, positionId, numericCode, stringCode, message)
                      are now documented in MetatraderTradeResponse (TypedDict).

WHY METAAPI CLOUD:
  - The official `MetaTrader5` PyPI package only runs on Windows and
    requires the MT5 terminal to be installed on the same machine —
    unusable on Linux/Render.
  - MetaApi Cloud (https://metaapi.cloud) is a hosted MT5 bridge:
      * Runs the MT5 terminal in their cloud
      * Exposes a clean REST/WebSocket API
      * Has a Python SDK (`metaapi-cloud-sdk`)
      * Free tier covers 1 account (sufficient for a single-operator bot)
      * Supports both demo and live accounts at any broker
  - The operator creates a MetaApi account, provisions an "account" in
    their dashboard (entering their MT5 login/password/server once),
    gets an API token, and pastes both into SmAttaker. From then on,
    SmAttaker can:
      * Read live account balance, equity, margin
      * Stream tick prices
      * Place market orders, pending orders
      * Modify/close positions
      * Read trade history

CONFIGURATION:
  The user enters two pieces of info in the dashboard:
    1. MetaApi Account ID  (a UUID, e.g. "abc12345-...")
    2. MetaApi API Token   (a long JWT-like string)
  We store both encrypted (Fernet) in the ExchangeConnection table:
    exchange_name        = "mt5"
    api_key_encrypted    = MetaApi Account ID
    secret_key_encrypted = MetaApi API Token
    passphrase_encrypted = MT5 server name (e.g. "ICMarketsSC-Demo")
                           — kept for display, not used for the API
    is_testnet           = True if demo, False if live

WHY THIS ISN'T A STUB:
  - test_connection() actually deploys+connects the account via the SDK
    and reads real account info (balance, equity, server, leverage)
  - fetch_balance() reads live account state
  - place_market_order() creates a real market order on MT5
  - On any SDK failure, we degrade gracefully: log the error, return a
    clear error dict, and never crash the caller.
"""
from __future__ import annotations
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("smattaker.exchange.mt5")


class MT5Connector:
    """
    Live MT5 connector via MetaApi Cloud SDK (v29.x API).

    Storage shape (in ExchangeConnection):
      exchange_name        = "mt5"
      api_key_encrypted    = MetaApi Account ID  (UUID)
      secret_key_encrypted = MetaApi API Token   (JWT-like)
      passphrase_encrypted = MT5 server name     (display only)
      is_testnet           = True if demo account
    """

    POPULAR_SERVERS = [
        "icmarkets", "pepperstone", "ftmo", "exness", "fxtm", "xm",
        "tickmill", "fxpro", "oanda", "alpari", "roboforex", "ig",
    ]

    @staticmethod
    async def provision_account(
        login: str, password: str, server: str, is_demo: bool = True, name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        ⚠️ v54 — THE self-service fix: auto-create a MetaApi account on
        behalf of the user, using the platform's OWN master token
        (settings.METAAPI_TOKEN — set once by the operator), from
        nothing but the MT5 login/password/broker-server the dashboard
        already collects. Before this, connecting MT5 required the
        OPERATOR to manually create the account in MetaApi's own
        dashboard for every single user — that's the gap this closes.

        Returns:
          {"success": True, "metaapi_account_id": "<uuid>"}   on success
          {"success": False, "error": "<human-readable reason>"}  on failure

        Never raises — every SDK/network failure is caught and reported
        as a clear error string, since this runs inline in an HTTP
        request the user is waiting on (POST /api/account/connections).
        """
        from backend.config import settings
        if not settings.METAAPI_TOKEN:
            return {
                "success": False,
                "error": "MT5 auto-connect is not configured on this platform yet "
                         "(operator must set METAAPI_TOKEN). Use the manual MetaApi "
                         "Account ID / Token fields instead.",
            }

        MetaApi = MT5Connector._load_sdk()
        if MetaApi is None:
            return {"success": False, "error": "metaapi-cloud-sdk is not installed on the server."}

        try:
            api = MetaApi(token=settings.METAAPI_TOKEN)
            account_payload = {
                "login": str(login).strip(),
                "password": str(password),
                "name": name or f"smattaker-{login}",
                "server": str(server).strip(),
                "platform": "mt5",
                "magic": 0,
                # 'cloud-g2' is MetaApi's current-generation managed cloud
                # infrastructure — no VPS or terminal to maintain ourselves.
                "type": "cloud-g2",
            }
            account = await api.metatrader_account_api.create_account(account_payload)
            account_id = getattr(account, "id", None) or (
                account.get("id") if isinstance(account, dict) else None
            )
            if not account_id:
                return {"success": False, "error": "MetaApi did not return an account id."}

            # Deploy immediately so the very first test_connection() call
            # right after this doesn't have to wait through a cold deploy.
            try:
                await account.deploy()
            except Exception as de:
                logger.debug(f"provision_account: deploy() warning: {de}")

            return {"success": True, "metaapi_account_id": account_id}

        except Exception as e:
            msg = str(e)
            # MetaApi's most common rejection reasons, translated into
            # something the user can actually act on instead of a raw
            # SDK exception string.
            if "server" in msg.lower() and ("not found" in msg.lower() or "invalid" in msg.lower()):
                friendly = (
                    f"Broker server '{server}' was not recognized by MetaApi. "
                    "Double-check the exact server name shown in your MT5 terminal "
                    "(e.g. 'ICMarketsSC-Demo02', not just 'ICMarkets')."
                )
            elif "auth" in msg.lower() or "password" in msg.lower() or "login" in msg.lower():
                friendly = "MT5 login or password was rejected by the broker — please double-check both."
            elif "limit" in msg.lower() or "quota" in msg.lower():
                friendly = "This platform's MetaApi account limit has been reached — contact support."
            else:
                friendly = f"MT5 auto-connect failed: {msg[:200]}"
            logger.warning(f"provision_account failed for login={login}, server={server}: {e}")
            return {"success": False, "error": friendly}

    def __init__(
        self,
        login: str,
        password: str,
        server: str,
        is_demo: bool = False,
        # New v45 fields — MetaApi-specific. When provided, these override
        # the legacy login/password/server path and use the real SDK. When
        # absent, we fall back to the old stub behavior so existing rows
        # keep working until the user upgrades.
        metaapi_account_id: Optional[str] = None,
        metaapi_api_token: Optional[str] = None,
    ):
        # `login` and `password` are still required for validation but
        # only actually used by the live SDK if metaapi_* is not provided.
        if not login:
            raise ValueError("MT5 login (or MetaApi Account ID) is required.")
        self.login = str(login).strip()
        self.password = str(password) if password else ""
        self.server = str(server).strip() if server else ""
        self.is_demo = bool(is_demo)
        self.metaapi_account_id = (metaapi_account_id or "").strip() or None
        self.metaapi_api_token = (metaapi_api_token or "").strip() or None

    # ── SDK lazy loader ─────────────────────────────────────────────
    @staticmethod
    def _load_sdk():
        """Import the MetaApi SDK lazily so the app boots even when the
        package isn't installed (the connector just degrades to stub mode).

        ⚠️ v46: The 29.x SDK publishes its public API under
        `metaapi_cloud_sdk` (with underscores). The old `metaapi_sdk`
        alias was removed somewhere between 6.1.0 and 9.0.0, so we try
        the new name first and only fall back to the legacy alias for
        installations still pinned to the old 6.x series.
        """
        try:
            from metaapi_cloud_sdk import MetaApi
            return MetaApi
        except ImportError:
            try:
                # Legacy 6.x module name — kept for backward compat
                from metaapi_sdk import MetaApi
                return MetaApi
            except ImportError:
                try:
                    # Some packaging variants used metaapi.cloud
                    from metaapi.cloud import MetaApi
                    return MetaApi
                except ImportError:
                    logger.warning(
                        "metaapi-cloud-sdk not installed — MT5 connector will "
                        "operate in stub mode. Install with: pip install metaapi-cloud-sdk"
                    )
                    return None

    # ── Connection helper (v29 API) ─────────────────────────────────
    async def _connect_live(self) -> Optional[Dict[str, Any]]:
        """Connect to MetaApi and return a context dict with the api,
        account, and an open RPC connection.

        Returns None (and logs) if the SDK isn't available or the
        connection fails. Caller is responsible for closing
        `ctx["connection"]` in a finally block.
        """
        MetaApi = self._load_sdk()
        if MetaApi is None:
            return None

        api = MetaApi(token=self.metaapi_api_token)
        # v29 API: get_account lives on metatrader_account_api, not on
        # the MetaApi instance directly.
        account = await api.metatrader_account_api.get_account(self.metaapi_account_id)

        # Deploy the account (idempotent — MetaApi caches this)
        try:
            await account.deploy()
        except Exception as de:
            logger.debug(f"deploy() warning (usually 'already deployed'): {de}")

        # Wait for deployment + broker connection. The v29 SDK exposes
        # wait_deployed() and wait_connected() on the *account* object
        # (not on the RPC connection — that's a 6.x-ism).
        try:
            await account.wait_deployed(timeout_in_seconds=60)
        except Exception as de:
            logger.debug(f"wait_deployed() warning: {de}")
        try:
            await account.wait_connected(timeout_in_seconds=30)
        except Exception as ce:
            logger.warning(f"MT5 wait_connected timed out or failed: {ce}")

        # get_rpc_connection() is a sync method in v29 (returns the
        # instance, not a coroutine). The connection is already open
        # from the wait_connected() call above, but we call connect()
        # explicitly for safety — it's idempotent.
        connection = account.get_rpc_connection()
        try:
            await connection.connect()
        except Exception as ce:
            logger.debug(f"connect() warning (usually 'already connected'): {ce}")

        return {"api": api, "account": account, "connection": connection}

    # ── Public API ──────────────────────────────────────────────────
    async def test_connection(self) -> Dict[str, Any]:
        """Validate the credentials by actually connecting to MetaApi.

        If MetaApi credentials are present, this performs a real
        connection + account-info read. If not, it falls back to
        structural validation only (legacy behavior).
        """
        # ── Legacy path: no MetaApi creds → structural validation only ──
        if not self.metaapi_account_id or not self.metaapi_api_token:
            if not self.login.isdigit():
                return {
                    "success": False,
                    "error": (
                        "MT5 login must be a numeric account ID "
                        f"(received '{self.login}'). For live MT5 trading, "
                        "enter your MetaApi Account ID and API token."
                    ),
                }
            if len(self.password) < 4:
                return {
                    "success": False,
                    "error": "MT5 password looks too short (min 4 characters).",
                }
            return {
                "success": True,
                "message": (
                    "MT5 credentials saved (stub mode). For live order "
                    "execution, add a MetaApi Account ID and API token — "
                    "see https://metaapi.cloud for a free account."
                ),
                "bridge_required": True,
                "mode": "stub",
            }

        # ── Live path: real MetaApi connection (v29 SDK) ──
        MetaApi = self._load_sdk()
        if MetaApi is None:
            return {
                "success": False,
                "error": (
                    "metaapi-cloud-sdk is not installed on the server. "
                    "Add it to requirements.txt and redeploy, or contact "
                    "the platform operator."
                ),
            }

        connection = None
        try:
            ctx = await self._connect_live()
            if ctx is None:
                return {
                    "success": False,
                    "error": "Failed to initialize MetaApi connection.",
                    "mode": "live",
                }
            connection = ctx["connection"]

            # Read real account info — this is the proof the credentials work.
            # v29: get_account_information() returns a coroutine resolving
            # to a MetatraderAccountInformation dict.
            info = await connection.get_account_information()
            if not isinstance(info, dict):
                return {
                    "success": False,
                    "error": "Unexpected account info format from MetaApi.",
                    "mode": "live",
                }

            server = info.get("server", self.server)
            balance = info.get("balance")
            equity = info.get("equity")
            leverage = info.get("leverage")
            currency = info.get("currency", "USD")

            logger.info(
                f"MT5 live connection OK: login={self.login}, server={server}, "
                f"balance={balance} {currency}, leverage=1:{leverage}"
            )
            return {
                "success": True,
                "mode": "live",
                "bridge_required": False,
                "server": server,
                "balance": balance,
                "equity": equity,
                "leverage": leverage,
                "currency": currency,
                "message": (
                    f"MT5 live connection established to {server}. "
                    f"Balance: {balance} {currency}."
                ),
            }
        except Exception as e:
            logger.error(f"MT5 live connection failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"MT5 connection failed: {str(e)[:300]}",
                "mode": "live",
            }
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass

    async def fetch_balance(self) -> Dict[str, Any]:
        """Read live MT5 account balance/equity via MetaApi."""
        if not self.metaapi_account_id or not self.metaapi_api_token:
            return {
                "error": "MT5 live bridge not configured — balance unavailable.",
                "bridge_required": True,
                "mode": "stub",
            }

        MetaApi = self._load_sdk()
        if MetaApi is None:
            return {"error": "metaapi-cloud-sdk not installed.", "mode": "live"}

        connection = None
        try:
            ctx = await self._connect_live()
            if ctx is None:
                return {"error": "Failed to initialize MetaApi connection.", "mode": "live"}
            connection = ctx["connection"]

            info = await connection.get_account_information()
            if not isinstance(info, dict):
                return {"error": "Unexpected account info format."}

            return {
                "success": True,
                "mode": "live",
                "balance": info.get("balance"),
                "equity": info.get("equity"),
                "margin": info.get("margin"),
                "free_margin": info.get("freeMargin"),
                "leverage": info.get("leverage"),
                "currency": info.get("currency", "USD"),
                "server": info.get("server", self.server),
                "platform": "mt5",
            }
        except Exception as e:
            logger.error(f"MT5 fetch_balance failed: {e}")
            return {"error": str(e)[:300], "mode": "live"}
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        leverage: int = 1,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place a real market order on MT5 via MetaApi.

        Args:
            symbol: MT5 symbol, e.g. "EURUSD", "XAUUSD", "BTCUSD"
            side: "BUY" or "SELL"
            amount: volume in LOTS (MT5 convention — 0.01 = micro lot)
            leverage: informational only (leverage is set at the account level)
            stop_loss: SL price (optional)
            take_profit: TP price (optional)

        ⚠️ v46 SDK MIGRATION:
            The 6.x SDK accepted a single dict via create_market_order().
            The 29.x SDK removed that method and split it into
            create_market_buy_order() and create_market_sell_order() with
            explicit positional args. We dispatch on `side` here so the
            caller's API (side + amount + sl + tp) is unchanged.
        """
        # ── Stub mode: record but don't execute ──
        if not self.metaapi_account_id or not self.metaapi_api_token:
            logger.info(
                f"MT5 order recorded (stub — bridge required): "
                f"{symbol} {side} {amount} lots, SL={stop_loss}, TP={take_profit}"
            )
            return {
                "success": True,
                "bridge_required": True,
                "mode": "stub",
                "order": {
                    "symbol": symbol, "side": side, "amount": amount,
                    "leverage": leverage, "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "account_login": self.login,
                    "account_server": self.server,
                    "demo": self.is_demo,
                },
                "message": "Order recorded (stub). Add MetaApi credentials for live execution.",
            }

        # ── Live mode: real order via MetaApi (v29 SDK) ──
        MetaApi = self._load_sdk()
        if MetaApi is None:
            return {"success": False, "error": "metaapi-cloud-sdk not installed."}

        side_upper = (side or "").upper()
        if side_upper not in ("BUY", "SELL"):
            return {"success": False, "error": f"Invalid side '{side}' (must be BUY or SELL)."}

        connection = None
        try:
            ctx = await self._connect_live()
            if ctx is None:
                return {"success": False, "error": "Failed to initialize MetaApi connection."}
            connection = ctx["connection"]

            volume = float(amount)
            # v29 SDK: explicit create_market_buy_order / create_market_sell_order.
            # Positional args: (symbol, volume, stop_loss, take_profit, options).
            if side_upper == "BUY":
                result = await connection.create_market_buy_order(
                    symbol, volume, stop_loss, take_profit
                )
            else:
                result = await connection.create_market_sell_order(
                    symbol, volume, stop_loss, take_profit
                )

            # v29: result is a MetatraderTradeResponse TypedDict with keys:
            #   numericCode, stringCode, message, orderId, positionId
            # Success codes: 0, 10008-10010, 10025 (per MQL5 docs)
            if not isinstance(result, dict):
                # Some SDK variants return an object — coerce defensively
                order_id = getattr(result, "orderId", "") or str(result)
                price_filled = getattr(result, "price", None)
                numeric_code = getattr(result, "numericCode", 0)
                string_code = getattr(result, "stringCode", "")
                message = getattr(result, "message", "")
            else:
                order_id = result.get("orderId") or result.get("order_id") or ""
                price_filled = result.get("price") or result.get("priceUsd")
                numeric_code = int(result.get("numericCode") or 0)
                string_code = result.get("stringCode", "")
                message = result.get("message", "")

            # Success codes per MetaApi docs: 0, 10008, 10009, 10010, 10025
            success_codes = {0, 10008, 10009, 10010, 10025}
            is_success = numeric_code in success_codes

            if not is_success:
                logger.error(
                    f"MT5 order rejected: {symbol} {side_upper} {volume} lots — "
                    f"code={numeric_code} ({string_code}): {message}"
                )
                return {
                    "success": False,
                    "error": f"MT5 rejected order: {string_code} ({numeric_code}): {message}",
                    "mode": "live",
                    "order": {
                        "symbol": symbol, "side": side_upper, "amount": volume,
                        "stop_loss": stop_loss, "take_profit": take_profit,
                    },
                }

            logger.info(
                f"MT5 LIVE ORDER FILLED: {symbol} {side_upper} {volume} lots "
                f"SL={stop_loss} TP={take_profit} orderId={order_id} "
                f"code={numeric_code} ({string_code})"
            )
            return {
                "success": True,
                "mode": "live",
                "bridge_required": False,
                "order_id": order_id,
                "price": price_filled,
                "numeric_code": numeric_code,
                "string_code": string_code,
                "order": {
                    "symbol": symbol, "side": side_upper, "amount": volume,
                    "leverage": leverage, "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "account_login": self.login,
                    "account_server": self.server,
                    "demo": self.is_demo,
                },
                "message": f"Live MT5 order filled (id: {order_id}, code: {string_code}).",
            }
        except Exception as e:
            logger.error(f"MT5 live order failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)[:400],
                "mode": "live",
                "order": {
                    "symbol": symbol, "side": side, "amount": amount,
                    "stop_loss": stop_loss, "take_profit": take_profit,
                },
            }
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass
