"""
SmAttaker — Smart TradingView Symbol Resolver (v45.4.8)
========================================================
The BRAIN behind chart symbols. While tv_symbols.py is the static,
offline mapping, THIS module adds smart verification layers on top so a
chart never dies with "This symbol doesn't exist" again:

  L1  STATIC VERIFIED MAP  — deterministic for gold/silver/oil/futures,
      plus verified crypto overrides (FARTCOIN, PIPPIN, KAS, RIVER →
      MEXC spot) and verified forex sets (majors → OANDA, exotics →
      ICE/Saxo/CMC).

  L2  LIVE VERIFICATION    — for anything the static map only GUESSES:
      • unknown crypto bases   → ranked `<BASE>USDT` spot/perp venues
      • non-OANDA forex pairs  → ranked real forex feeds (OANDA/
        FOREXCOM/FX_IDC/Saxo/CMC/ICE)   ← v45.4.8: this layer now also
        covers forex & stocks. This is what killed the dead
        'OANDA:CADINR' class of bugs for good (v45.4.7 only verified
        crypto live, so exotic forex pairs still slipped through).
      • unknown stock tickers  → ranked NYSE/NASDAQ/AMEX rows

  L3  CACHE                — every live resolution is cached for 24h
      (Redis when available, in-process dict otherwise), so the TV
      search API is hit at most once per symbol per day.

Failure philosophy: EVERY layer degrades gracefully. If the TV search
API is unreachable (Render egress blocked, rate limit, outage), we fall
back to L1's static answer — never an exception, never a None crash.
"""
import asyncio
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger("smattaker.tv_resolver")

# ── Tunables ───────────────────────────────────────────────────────────
_TV_SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/text/"
_TV_SEARCH_TIMEOUT_S = 6.0     # short — a slow TV API must not stall the API route
_CACHE_TTL_S = 24 * 3600       # 24h — venues move rarely
_NEGATIVE_CACHE_TTL_S = 6 * 3600  # if TV search found nothing, don't re-query for 6h

# TradingView requires an Origin header from their own site, otherwise 403.
_TV_SEARCH_HEADERS = {
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
}

# ── In-process cache (works even without Redis) ────────────────────────
# key: crypto base  →  (monotonic_deadline, {"tv_symbol":…, "tv_fallbacks":[…], "source":…})
_memory_cache: Dict[str, tuple] = {}


def _cache_get(key: str) -> Optional[dict]:
    hit = _memory_cache.get(key)
    if not hit:
        return None
    deadline, payload = hit
    if time.monotonic() > deadline:
        _memory_cache.pop(key, None)
        return None
    return payload


def _cache_set(key: str, payload: dict, ttl: float = _CACHE_TTL_S) -> None:
    _memory_cache[key] = (time.monotonic() + ttl, payload)


async def _cache_get_redis(key: str) -> Optional[dict]:
    """Best-effort Redis read — returns None on any problem."""
    try:
        import json as _json
        from backend.redis_client import redis_client as _rc
        if _rc is None:
            return None
        raw = await _rc.get(f"tv:{key}")
        if not raw:
            return None
        wrapped = _json.loads(raw)
        if wrapped.get("exp", 0) < time.time():
            return None
        return wrapped.get("data")
    except Exception:
        return None


async def _cache_set_redis(key: str, payload: dict, ttl: float = _CACHE_TTL_S) -> None:
    """Best-effort Redis write — never raises."""
    try:
        import json as _json
        from backend.redis_client import redis_client as _rc
        if _rc is None:
            return
        await _rc.setex(
            f"tv:{key}", int(ttl),
            _json.dumps({"exp": time.time() + ttl, "data": payload}),
        )
    except Exception:
        pass


# ── TradingView symbol-search client ───────────────────────────────────
_search_lock = asyncio.Semaphore(4)   # politeness cap — 4 concurrent lookups max


async def tv_symbol_search(query: str, limit: int = 20) -> List[dict]:
    """Query TradingView's public symbol-search endpoint.

    Returns a list of rows: {symbol, exchange, description, type, …}.
    Empty list on ANY failure (network blocked, rate limit, bad JSON) —
    callers must treat that as "verification unavailable", not "no venue".
    """
    import aiohttp
    try:
        async with _search_lock:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TV_SEARCH_TIMEOUT_S),
                headers=_TV_SEARCH_HEADERS,
            ) as session:
                async with session.get(
                    _TV_SEARCH_URL,
                    params={"text": query, "limit": str(limit)},
                ) as resp:
                    if resp.status != 200:
                        logger.debug(f"TV search HTTP {resp.status} for '{query}'")
                        return []
                    return await resp.json(content_type=None) or []
    except Exception as e:
        logger.debug(f"TV search failed for '{query}': {e}")
        return []


def _rank_rows(rows: List[dict], wanted: str, priority: List[str]) -> tuple:
    """Generic venue ranking: among TV-search rows whose SYMBOL equals
    `wanted` (e.g. 'CADINR', 'WMT', 'FARTCOINUSDT'), pick the best venue
    by `priority` (ordered TV exchange prefixes).

    Returns (primary_tv_symbol | None, [alternates]).
    """
    from backend.utils.tv_symbols import _TV_EXCHANGE_PREFIX

    def _prio(exchange_name: str) -> int:
        prefix = _TV_EXCHANGE_PREFIX.get(exchange_name,
                                         str(exchange_name).upper())
        try:
            return priority.index(prefix)
        except ValueError:
            return 900  # unknown venue — allowed but ranked last

    hits = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        if sym == wanted:
            hits.append((str(r.get("exchange", "")), r))
    hits.sort(key=lambda x: _prio(x[0]))

    def _tv(rowtuple):
        if not rowtuple:
            return None
        ex, r = rowtuple
        prefix = _TV_EXCHANGE_PREFIX.get(ex, str(ex).upper())
        return f"{prefix}:{r.get('symbol', '')}"

    primary = _tv(hits[0] if hits else None)
    alternates = []
    seen = {primary}
    for ex, r in hits[1:4]:
        s = _tv((ex, r))
        if s and s not in seen:
            alternates.append(s)
            seen.add(s)
    return primary, alternates


def _rank_rows_for_base(rows: List[dict], base: str) -> tuple:
    """Pick the best (tv_symbol, alternates) for a crypto base from raw
    TV search rows.

    Strategy:
      1. exact spot  `<BASE>USDT`  ranked by exchange priority
      2. exact perp  `<BASE>USDT.P` ranked by exchange priority
      3. exact USD   `<BASE>USD`   (Kraken-style) ranked by priority
    Everything else (indexes, .D derivatives-metrics, foreign listings)
    is ignored — those caused the original 'symbol doesn't exist' class
    of bugs.
    """
    from backend.utils.tv_symbols import _CRYPTO_EXCHANGE_PRIORITY, _TV_EXCHANGE_PREFIX

    def _prio(exchange_name: str) -> int:
        prefix = _TV_EXCHANGE_PREFIX.get(exchange_name, exchange_name.upper())
        try:
            return _CRYPTO_EXCHANGE_PRIORITY.index(prefix)
        except ValueError:
            return 900  # unknown venue — allowed but ranked last

    spot_rows, perp_rows, usd_rows = [], [], []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        ex = str(r.get("exchange", ""))
        if sym == f"{base}USDT":
            spot_rows.append((ex, r))
        elif sym == f"{base}USDT.P":
            perp_rows.append((ex, r))
        elif sym == f"{base}USD":
            usd_rows.append((ex, r))

    spot_rows.sort(key=lambda x: _prio(x[0]))
    perp_rows.sort(key=lambda x: _prio(x[0]))
    usd_rows.sort(key=lambda x: _prio(x[0]))

    def _tv(rowtuple) -> Optional[str]:
        if not rowtuple:
            return None
        ex, r = rowtuple
        prefix = _TV_EXCHANGE_PREFIX.get(ex, str(ex).upper())
        return f"{prefix}:{r.get('symbol', '')}"

    primary = _tv(spot_rows[0] if spot_rows else (perp_rows[0] if perp_rows else usd_rows[0] if usd_rows else None))

    alternates: List[str] = []
    seen = {primary}
    # next-best spots (up to 2)
    for ex, r in spot_rows[1:3]:
        s = _tv((ex, r))
        if s and s not in seen:
            alternates.append(s)
            seen.add(s)
    # best perp (if not already primary)
    p = _tv(perp_rows[0] if perp_rows else None)
    if p and p not in seen:
        alternates.append(p)
        seen.add(p)
    # best USD feed (Kraken/CRYPTO index style)
    u = _tv(usd_rows[0] if usd_rows else None)
    if u and u not in seen:
        alternates.append(u)
        seen.add(u)

    return primary, alternates


# ── Public API ─────────────────────────────────────────────────────────

async def resolve_tv_symbol(symbol: str, asset_class: Optional[str] = None) -> dict:
    """Resolve a platform symbol to a VERIFIED TradingView symbol bundle.

    Returns:
        {
          "tv_symbol":    "MEXC:FARTCOINUSDT",
          "tv_fallbacks": ["KUCOIN:FARTCOINUSDT", …],
          "source":       "static-verified | live-verified | static-default",
        }

    v45.4.8: crypto keeps its dedicated spot/perp/USD ranking; forex and
    stocks now go through LIVE verification too whenever the static map
    is not explicitly confirmed (this is what fixed the dead
    'OANDA:CADINR' chart and prevents the whole bug class for any pair
    or ticker added in the future). NEVER raises.
    """
    from backend.utils.tv_symbols import (
        map_symbol_to_tv,
        tv_fallback_chain,
        get_tv_search_symbol,
        _crypto_base,
        _CRYPTO_TV_OVERRIDES,
        _FOREX_OANDA_VERIFIED,
        _FOREX_EXOTIC_TO_TV,
        _FOREX_VENUE_PRIORITY,
        _STOCK_TV_EXCHANGE,
        _STOCK_VENUE_PRIORITY,
    )

    try:
        sym = (symbol or "").strip().upper()
        cls = (asset_class or "").lower()

        # ── FOREX ────────────────────────────────────────────────────
        if cls == "forex":
            bare = sym.replace("/", "")
            # Confirmed-static: verified OANDA majors + verified exotics
            if bare in _FOREX_EXOTIC_TO_TV:
                primary, fb = _FOREX_EXOTIC_TO_TV[bare]
                return {"tv_symbol": primary,
                        "tv_fallbacks": list(fb),
                        "source": "static-verified"}
            if bare in _FOREX_OANDA_VERIFIED:
                return {"tv_symbol": f"OANDA:{bare}",
                        "tv_fallbacks": tv_fallback_chain(sym, cls),
                        "source": "static-verified"}
            # Unverified pair → cache → live TV verification
            cache_key = f"forex:{bare}"
            cached = _cache_get(cache_key) or await _cache_get_redis(cache_key)
            if cached:
                return cached
            rows = await tv_symbol_search(bare, limit=40)
            if rows:
                primary, alternates = _rank_rows(rows, bare, _FOREX_VENUE_PRIORITY)
                if primary:
                    payload = {
                        "tv_symbol": primary,
                        "tv_fallbacks": (alternates
                                         + [f"OANDA:{bare}", bare])[:5],
                        "source": "live-verified",
                    }
                    _cache_set(cache_key, payload)
                    await _cache_set_redis(cache_key, payload)
                    logger.info(f"TV resolver: forex {bare} → {primary} (live-verified)")
                    return payload
            # TV unreachable or nothing usable → static default (FX_IDC)
            return {
                "tv_symbol": map_symbol_to_tv(sym, cls),
                "tv_fallbacks": tv_fallback_chain(sym, cls),
                "source": "static-default",
            }

        # ── STOCKS ───────────────────────────────────────────────────
        if cls == "stocks":
            known = _STOCK_TV_EXCHANGE.get(sym)
            if known:  # hand-verified → instant, no network
                return {"tv_symbol": f"{known}:{sym}",
                        "tv_fallbacks": tv_fallback_chain(sym, cls),
                        "source": "static-verified"}
            cache_key = f"stock:{sym}"
            cached = _cache_get(cache_key) or await _cache_get_redis(cache_key)
            if cached:
                return cached
            rows = await tv_symbol_search(sym, limit=40)
            if rows:
                primary, alternates = _rank_rows(rows, sym, _STOCK_VENUE_PRIORITY)
                if primary:
                    payload = {
                        "tv_symbol": primary,
                        "tv_fallbacks": (alternates + [sym])[:5],
                        "source": "live-verified",
                    }
                    _cache_set(cache_key, payload)
                    await _cache_set_redis(cache_key, payload)
                    logger.info(f"TV resolver: stock {sym} → {primary} (live-verified)")
                    return payload
            return {
                "tv_symbol": map_symbol_to_tv(sym, cls),
                "tv_fallbacks": tv_fallback_chain(sym, cls),
                "source": "static-default",
            }

        # ── Non-crypto (gold/silver/oil/futures): static verified map ──
        if cls != "crypto":
            return {
                "tv_symbol": map_symbol_to_tv(sym, cls),
                "tv_fallbacks": tv_fallback_chain(sym, cls),
                "source": "static-verified",
            }

        base = _crypto_base(sym)
        if not base:
            return {"tv_symbol": map_symbol_to_tv(sym, cls),
                    "tv_fallbacks": [], "source": "static-default"}

        # ── L3a: cache ──
        cache_key = f"crypto:{base}"
        cached = _cache_get(cache_key) or await _cache_get_redis(cache_key)
        if cached:
            return cached

        # ── L1: hand-verified overrides (FARTCOIN/PIPPIN/KAS/RIVER …) ──
        if base in _CRYPTO_TV_OVERRIDES:
            primary, fallbacks = _CRYPTO_TV_OVERRIDES[base]
            payload = {
                "tv_symbol": primary,
                "tv_fallbacks": fallbacks,
                "source": "static-verified",
            }
            _cache_set(cache_key, payload)
            await _cache_set_redis(cache_key, payload)
            return payload

        # ── L2: live verification via TradingView symbol search ──
        rows = await tv_symbol_search(f"{base}USDT", limit=30)
        if rows:
            primary, alternates = _rank_rows_for_base(rows, base)
            if primary:
                payload = {
                    "tv_symbol": primary,
                    "tv_fallbacks": alternates + tv_fallback_chain(sym, "crypto"),
                    "source": "live-verified",
                }
                # de-dupe fallbacks while preserving order
                seen = {primary}
                deduped = []
                for s in payload["tv_fallbacks"]:
                    if s and s not in seen:
                        deduped.append(s)
                        seen.add(s)
                payload["tv_fallbacks"] = deduped[:5]
                _cache_set(cache_key, payload)
                await _cache_set_redis(cache_key, payload)
                logger.info(
                    f"TV resolver: {base} → {primary} "
                    f"(live-verified, {len(rows)} rows)"
                )
                return payload
            # TV answered but nothing usable → negative-cache to avoid hammering
            payload = {
                "tv_symbol": map_symbol_to_tv(sym, "crypto"),
                "tv_fallbacks": tv_fallback_chain(sym, "crypto"),
                "source": "static-default",
            }
            _cache_set(cache_key, payload, ttl=_NEGATIVE_CACHE_TTL_S)
            await _cache_set_redis(cache_key, payload, ttl=_NEGATIVE_CACHE_TTL_S)
            return payload

        # ── TV search unreachable → static default (no negative caching —
        #    we WANT to re-verify when connectivity returns) ──
        return {
            "tv_symbol": map_symbol_to_tv(sym, "crypto"),
            "tv_fallbacks": tv_fallback_chain(sym, "crypto"),
            "source": "static-default",
        }
    except Exception as e:
        # Absolute last resort — mirror the old behaviour, never crash.
        logger.warning(f"resolve_tv_symbol failed for {symbol}: {e}")
        from backend.utils.tv_symbols import map_symbol_to_tv
        return {
            "tv_symbol": map_symbol_to_tv(symbol, asset_class),
            "tv_fallbacks": [],
            "source": "static-default",
        }


def search_url_for(symbol: str) -> str:
    """TradingView search URL — used by the Mini App error card so the
    user is one tap away from a working chart even if everything failed."""
    from urllib.parse import quote
    q = (symbol or "").replace("/", "").replace("=F", "")
    return f"https://www.tradingview.com/search/?text={quote(q)}"
