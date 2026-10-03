"""
OMEGA QUANT — SmAttaker platform strategy #2 (replaces the retired
BLACK SWAN v32 book).

The strategy IS the certified three-sword OMEGA QUANT recipe, ported
bit-exact from the sealed root system (T2.10 ERA, 1109 trials) and
proven against it by the anchor harness:

  MANDATE  PF 4.542  n=2009  WR 0.570  DD 0.0032
  MIDDLE   PF 4.769  n=1832  WR 0.578  DD 0.0041
  QUALITY  PF 4.349  n=1360  WR 0.549  DD 0.0051

Live pipeline (every 15 minutes at :02/:17/:32/:47 UTC):
  1. data.refresh_universe_symbol() per configured asset (15m).
  2. engine.frontier_scan(): the certified mask is evaluated on the
     LAST CLOSED M15 bar — a fired stream that also passes the sealed
     cell (long-only, vol_regime < 0.66) becomes a platform signal at
     the certified geometry (SL behind the weekly-OB far edge in H4-ATR
     units, TP ladder from the book's bracket).
  3. Each book (MANDATE / MIDDLE / QUALITY) admits the signal through
     its OWN frozen gate (volrank qmap) and K-slot occupancy. The
     strictest admitting book tags the signal.
  4. A JSON state ledger simulates every admitted trade through the
     certified _scan17 bracket to (a) keep the K-slot occupancy honest
     and (b) maintain the per-asset causal record the health guard and
     the confidence card quote.

Honesty contract:
  * The certified line (BTC / ETH / XAU) runs EXACTLY as certified —
    gates, occupancy, brackets untouched. confidence quotes the book's
    certified WR with its anchor provenance.
  * Every OTHER asset runs the SAME recipe as a PORTED book. It is NOT
    independently certified — the card says so and quotes the asset's
    OWN measured expanding stats (>=30 closed trades) or the champion
    reference.
  * Asset health guard (owner's avoidance law): a ported asset whose
    expanding PF(R_net) < 1.0 over >=30 measured closed trades is
    AVOIDED for new entries until the causal record recovers. The
    certified line is exempt (its risk is governed by the frozen
    weights + the PHASE-XVIII deployment protocol instead).
  * The certified cell is LONG-ONLY (the PHASE-XIX mirror refusal is
    sealed: shorts never passed). No short is ever emitted.
  * BE / trailing stops are banned — structurally absent from _scan17.
  * confidence_score is always a MEASURED number, never invented.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from backend.config import settings
from backend.strategies.base import BaseStrategy
from backend.strategies.omega_quant import data, engine
from backend.strategies.omega_quant import recipe as R

logger = logging.getLogger("smattaker.omega")

STRATEGY_TYPE = "omega_quant"
STRATEGY_VERSION = "1.0.0"           # port of the certified T2.10 root
SIGNAL_NAME = "OMEGA QUANT"
SIGNAL_EMOJI = "🔱"

_STATE_LOCK = asyncio.Lock()


# ------------------------------------------------------------------ state
class StateStore:
    """JSON ledger: occupancy slots + closed-trade record + guard state.

    Layout:
      open[key][book]  = [{t0, t1_sim|None, entry, sd, dr, legs, tstop,
                           chk, rmin, entry_hint, ...}, ...]
      closed[key]      = [{book, t0, t1, entry, sd, R, R_net, reason,
                           extra_cost}, ...]
      guard[key]       = {n, pf, avoided, updated}
    """

    def __init__(self, path: str):
        self.path = path
        self.state: dict = {"open": {}, "closed": {}, "guard": {}}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.state = json.load(f)
            except Exception as e:
                logger.warning("OMEGA state load failed (%s) — fresh "
                               "ledger starts", e)
                self.state = {"open": {}, "closed": {}, "guard": {}}
        self.state.setdefault("open", {})
        self.state.setdefault("closed", {})
        self.state.setdefault("guard", {})

    def save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("OMEGA state save skipped: %s", e)

    # ---- occupancy ---------------------------------------------------
    def admit(self, key: str, book: str, trade: dict):
        self.state["open"].setdefault(key, {}).setdefault(book, []) \
            .append(trade)

    def open_trades(self, key: str, book: str) -> list[dict]:
        return self.state["open"].get(key, {}).get(book, [])

    # ---- ledger advance ----------------------------------------------
    def advance(self, key: str, bars: pd.DataFrame, b: dict):
        """Re-simulate every open trade of this asset through the newest
        bars; resolve finished trades into the closed record."""
        if len(bars) == 0:
            return
        i_last = len(bars) - 1
        ts = b["ts"]
        for book, trades in self.state.get("open", {}).get(key, {}) \
                .items():
            still = []
            for tr in trades:
                # entry-bar position (ts is an int64-seconds array —
                # compare in the same domain, exactly like relabel17)
                t0s = int(pd.Timestamp(tr["t0"]).tz_convert("UTC")
                          .tz_localize(None).value // 10**9)
                i0 = int(np.searchsorted(ts, t0s, side="left"))
                if i0 >= len(ts) or ts[i0] != t0s:
                    still.append(tr)         # entry bar not closed yet
                    continue
                # repair the entry to the ACTUAL certified anchor
                if not tr.get("entry_actual"):
                    tr["entry"] = float(b["o"][i0])
                    a = float(tr["atr"])
                    zl = float(tr["zl"])
                    if np.isfinite(a) and a > 0 and tr["entry"] > 0 \
                            and np.isfinite(zl):
                        sl = min(zl - R.PAD * a, tr["entry"] - R.FLOOR_ATR * a)
                        if tr["entry"] - sl > R.CAP * a:
                            sl = tr["entry"] - R.CAP * a
                        tr["sd"] = tr["entry"] - sl
                        tr["entry_actual"] = True
                if tr.get("sd", 0) <= 0:
                    continue
                resolved, R_, t1s, rs, ex = engine.simulate_exit_from(
                    b, i0, tr["entry"], tr["sd"], 1, tr["legs"],
                    tr["tstop"], tr["chk"], tr["rmin"], i_last)
                if resolved:
                    cost = R.cost_bps_for(key, tr.get("asset_class",
                                                      "crypto"))
                    r_net = R_ - cost * 1e-4 * tr["entry"] / tr["sd"] \
                        - cost * 1e-4 * tr["entry"] / tr["sd"] * ex
                    self.state.setdefault("closed", {}).setdefault(key, []) \
                        .append(dict(
                            book=book, t0=tr["t0"],
                            t1=str(pd.Timestamp(int(t1s), unit="s",
                                                tz="UTC").isoformat()),
                            entry=tr["entry"], sd=tr["sd"], R=R_,
                            R_net=r_net, reason=rs, extra_cost=ex))
                else:
                    tr["t1_sim"] = None
                    still.append(tr)
            self.state["open"].setdefault(key, {})[book] = still

    # ---- health guard (ported assets only) ----------------------------
    def refresh_guard(self, key: str, is_certified: bool):
        if is_certified:
            self.state["guard"][key] = dict(
                n=None, pf=None, avoided=False,
                note="certified line — guard exempt (frozen weights + "
                     "deployment protocol govern risk)")
            return
        rec = self.state.get("closed", {}).get(key, [])
        n = len(rec)
        if n < R.GUARD_MIN_TRADES:
            self.state["guard"][key] = dict(
                n=n, pf=None, avoided=False,
                note=f"warming up ({n}/{R.GUARD_MIN_TRADES} measured "
                     "trades)")
            return
        Rn = np.array([t["R_net"] for t in rec], dtype=float)
        w_, l_ = Rn[Rn > 0], Rn[Rn <= 0]
        pf = float(w_.sum() / max(1e-9, -l_.sum())) if len(l_) else 20.0
        avoided = pf < R.GUARD_PF_FLOOR
        self.state["guard"][key] = dict(
            n=n, pf=round(pf, 4), avoided=bool(avoided),
            note=("AVOIDED — expanding PF below the 1.0 floor (owner "
                  "avoidance law)" if avoided else "active"))


# ---------------------------------------------------------------- strategy
class OmegaQuantStrategy(BaseStrategy):
    strategy_type = STRATEGY_TYPE
    strategy_version = STRATEGY_VERSION
    asset_class = "multi"

    def __init__(self):
        self._loaded = False
        self._universe_cache = None
        self._store = None

    # ---- universe ----------------------------------------------------
    def _universe(self) -> list[dict]:
        """Configured asset universe.

        settings.OMEGA_UNIVERSE:
          "ALL"       — the whole platform registry (owner's default:
                        run everywhere, the health guard avoids the
                        measured-bad assets).
          "CERTIFIED" — the certified line only (BTC, ETH, XAU).
          comma list  — registry symbols, e.g. "BTC,ETH,XAU,SOL,DOGE".
        """
        if self._universe_cache is not None:
            return self._universe_cache
        entries = data.platform_universe()
        want = (getattr(settings, "OMEGA_UNIVERSE", "ALL")
                or "ALL").strip().upper()
        if want == "CERTIFIED":
            want_set = set(R.CERTIFIED_ASSETS)
        elif want != "ALL":
            want_set = {s.strip().upper()
                        for s in want.split(",") if s.strip()}
        else:
            want_set = None
        if want_set is not None:
            known = {e["symbol"].upper() for e in entries}
            unknown = sorted(want_set - known)
            if unknown:
                logger.warning("OMEGA universe: unknown symbol(s) "
                               "ignored: %s", ", ".join(unknown))
            want_set -= set(unknown)
            entries = [e for e in entries
                       if e["symbol"].upper() in want_set]
        self._universe_cache = entries
        logger.info("🔱 OMEGA QUANT universe: %d asset(s) (%s)",
                    len(entries), want)
        return entries

    def _store_path(self) -> str:
        return os.path.join(data.CACHE_DIR, "omega_state.json")

    # ---- lifecycle ---------------------------------------------------
    async def load_model(self):
        """Warm the data caches + state ledger (idempotent)."""
        if self._loaded:
            return
        async with _STATE_LOCK:
            if self._loaded:
                return
            self._store = StateStore(self._store_path())
            await asyncio.to_thread(self._warm_sync)
            self._loaded = True

    def _warm_sync(self):
        ok = 0
        for entry in self._universe():
            try:
                data.refresh_universe_symbol(entry)
                ok += 1
            except FileNotFoundError:
                logger.info("OMEGA warmup: no data yet for %s",
                            entry["key"])
            except Exception as e:
                logger.warning("OMEGA warmup failed for %s: %s",
                               entry["key"], e)
        logger.info("OMEGA warmup: %d/%d asset(s) warmed",
                    ok, len(self._universe()))

    # ---- scan --------------------------------------------------------
    async def analyze(self, symbols=None) -> list[dict]:
        now = pd.Timestamp.now(tz="UTC")
        payloads = await asyncio.to_thread(self._scan_sync, now)
        out = []
        for p in payloads:
            sig = self._to_platform_signal(p, now)
            if self.validate_signal(sig):
                out.append(sig)
            else:
                logger.warning("OMEGA signal failed validation: %s", p)
        logger.info("🔱 OMEGA QUANT scan @ %s -> %d signal(s)",
                    now.strftime("%H:%M UTC"), len(out))
        return out

    def _scan_sync(self, now: pd.Timestamp) -> list[dict]:
        store = self._store or StateStore(self._store_path())
        frames, metas = {}, {}
        for entry in self._universe():
            key = entry["key"]
            try:
                frame = data.refresh_universe_symbol(entry)
            except FileNotFoundError:
                logger.info("OMEGA scan: no data for %s (skipped)", key)
                continue
            except Exception as e:
                logger.warning("OMEGA scan data failed for %s: %s",
                               key, e)
                continue
            frames[key] = frame
            metas[key] = {"tag": entry["symbol"],
                          "symbol": entry["symbol"],
                          "display": entry["display"],
                          "asset_class": entry["asset_class"],
                          "source": entry["source"]}
        # advance the ledger + guard on the freshest frames first
        for key, frame in frames.items():
            try:
                store.advance(key, frame, engine.bars_dict(frame))
                disp = metas[key]["symbol"].upper()
                store.refresh_guard(key, disp in R.CERTIFIED_ASSETS)
            except Exception as e:
                logger.warning("OMEGA ledger advance failed for %s: %s",
                               key, e)
        scan = engine.frontier_scan(now, frames, metas, store.state,
                                    lambda k, raw: self._features(k, raw))
        # register the admitted trades in the occupancy ledger (the entry
        # bar is not closed yet; the next cycle repairs the entry to the
        # actual open and simulates the exit through the certified bracket)
        for s in scan["signals"]:
            store.admit(s["scan_key"], s["book"], dict(
                t0=s["t0"], t1_sim=None, entry=float(s["entry"]),
                sd=float(s["sd"]), atr=float(s["atr"]), zl=float(s["zl"]),
                legs=s["legs"], tstop=s["tstop"], chk=s["chk"],
                rmin=s["rmin"], asset_class=s["asset_class"],
                sig_ts=s["sig_ts"], entry_actual=False))
        store.save()
        payloads = []
        for s in scan["signals"]:
            s["_context"] = {"assets": scan["context"]["assets"].get(
                s["scan_key"], {})}
            payloads.append(s)
        covered = len(scan["context"]["assets"])
        logger.info("OMEGA universe coverage: %d/%d asset(s) scanned",
                    covered, len(metas))
        return payloads

    @staticmethod
    def _features(key: str, raw: pd.DataFrame):
        from backend.strategies.omega_quant.features import \
            build_lean_features
        return build_lean_features(raw)

    # ---- honesty card ------------------------------------------------
    def _confidence(self, p: dict) -> tuple[float, str]:
        book = p["book"]
        m = R.MEASURED[book]
        if p.get("is_certified"):
            return round(m["wr"] * 100, 1), (
                f"certified {book} book ({R.ROOT_SYSTEM}; PF {m['pf']}, "
                f"n={m['n']}, WR {m['wr']}, DD {m['dd']}; anchors "
                "bit-exact)")
        guard = (p.get("_context") or {}).get("guard", {}) or {}
        n, pf = guard.get("n"), guard.get("pf")
        if n and n >= R.GUARD_MIN_TRADES and pf:
            wr_meas = self._measured_wr(p["scan_key"])
            if wr_meas is not None:
                return round(wr_meas * 100, 1), (
                    f"asset-measured over {n} closed trades of this "
                    "asset's available history (same certified rules "
                    "ported; expanding PF "
                    f"{pf}; descriptive, not independently certified)")
        return round(m["wr"] * 100, 1), (
            f"insufficient measured history on this asset — certified "
            f"{book} WR reference quoted (PF {m['pf']}, n={m['n']}); "
            "per-asset stats accrue with the ledger")

    def _measured_wr(self, key: str) -> float | None:
        if not self._store:
            return None
        rec = self._store.state.get("closed", {}).get(key, [])
        if len(rec) < R.GUARD_MIN_TRADES:
            return None
        Rn = np.array([t["R_net"] for t in rec], dtype=float)
        return float((Rn > 0).mean())

    # ---- platform signal ----------------------------------------------
    def _to_platform_signal(self, s: dict, now: pd.Timestamp) -> dict:
        symbol = s["symbol"]
        entry = float(s["entry"])
        sl = float(s["sl"])
        sd = float(s["sd"])
        legs = s["legs"]                       # [[frac, pr], ...]
        final_rr = R.RR
        levels = []
        remaining = 100.0
        for lvl_i, (fr, pr) in enumerate(legs, start=1):
            size = round(float(fr) * 100, 1)
            levels.append({"level": lvl_i,
                           "price": round(entry + pr * sd, 8),
                           "pct": round(pr * 100, 2),
                           "size_pct": size})
            remaining -= size
        levels.append({"level": len(levels) + 1,
                       "price": round(entry + final_rr * sd, 8),
                       "pct": round(final_rr * 100, 2),
                       "size_pct": round(max(remaining, 0.0), 1)})
        entry_pct = round(abs((entry - sl) / entry) * 100, 4)
        conf, provenance = self._confidence(s)
        ctx = s.get("_context", {}) or {}
        books_admitted = ", ".join(s.get("admitted", []) or [s["book"]])
        return {
            "symbol": symbol,
            "direction": "long",
            "entry_price": round(entry, 8),
            "stop_loss": round(sl, 8),
            "stop_loss_pct": entry_pct,
            "take_profit_levels": levels,
            "risk_reward_ratio": float(final_rr),
            # honesty: a MEASURED win rate + provenance — never invented
            "confidence_score": conf,
            "entry_time": pd.Timestamp(s["t0"]).isoformat(),
            "exchange": "ccxt" if s.get("source") == "ccxt" else
            "aggregator",
            "asset_class": s.get("asset_class", "crypto"),
            "strategy_type": STRATEGY_TYPE,
            "strategy_version": STRATEGY_VERSION,
            "expiry_minutes": R.ORDER_EXPIRY_MINUTES,
            "branded_symbol": symbol,
            "display_name": str(symbol).split("/")[0],
            "ml_metadata": {
                "engine": SIGNAL_NAME,
                "strategy": SIGNAL_NAME,
                "root_system": R.ROOT_SYSTEM,
                "book": s["book"],
                "books_admitted": books_admitted,
                "policy": "certified" if s.get("is_certified")
                else "ported",
                "rule": R.CHAMPIONS[s["book"]]["rule"],
                "cell": "longNotHi66 (long-only)",
                "gate_q": s["gate_q"],
                "volrank_p": round(s["p"], 4),
                "confH": s["confH"],
                "w_conf": s["w_conf"],
                "w_vol": s["w_vol"],
                "k_slots": s["k"],
                "bracket": dict(legs=s["legs"], tstop=s["tstop"],
                                chk=s["chk"], rmin=s["rmin"],
                                vert=R.VERT, rr=R.RR),
                "exit_rule": ("tp1 @ 1R (" +
                              f"{legs[0][0] * 100:g}%)" if legs else
                              "full TP @ 2.25R") +
                             (" + 192-bar time stop" if s["tstop"]
                              else "") + " + 384-bar vertical barrier",
                "measured": dict(R.MEASURED[s["book"]]),
                "confidence_provenance": provenance,
                "health_guard": ctx.get("guard", {}),
                "slippage_note": ("entry anchored at sig-bar close; the "
                                  "ledger repairs it to the actual next "
                                  "open and measures the residual"),
                "provenance": ("OMEGA QUANT certified recipe, bit-exact "
                               "port of the sealed root system; anchors "
                               "MANDATE/MIDDLE/QUALITY reproduced by the "
                               "harness; BE/TRAIL banned; long-only "
                               "certified cell"),
            },
            "technical_snapshot": {
                "strategy": SIGNAL_NAME,
                "book": s["book"],
                "asset_class": s.get("asset_class", "crypto"),
                "policy": "certified" if s.get("is_certified")
                else "ported",
                "sig_bar": s["sig_ts"],
                "entry_anchor": round(entry, 8),
                "sl_distance_R": 1.0,
                "zone_bot": None,
                "bar_time": s["sig_ts"],
            },
        }


def _fmt_utc(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%d %H:%M UTC")
