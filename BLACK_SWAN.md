# 🦅 Black Swan — Strategy #2 Operations Guide

> **What it is:** the second strategy of the SmAttaker platform. Black Swan is
> the production port of the **SNIPER BODY NOLDN v22** research engine
> (its validated RR/APEX book), built to the same institutional standards as
> the V45.4.1 ML strategy that already runs the platform.
>
> **Frozen book reference (2018→2026, honest backtest, full anti-overfit
> protocol):** N=1242 · WR 51.2% · expR +0.683R · payoff 2.264 · CAGR 165.0%
> · MaxDD 19.7% · t-stat 8.7 · bootstrap(2000) CI90 [+0.555, +0.813].

---

## 1. What runs in production

| Component | File | Role |
|---|---|---|
| Engine | `backend/strategies/engines/black_swan_engine.py` | The frozen pipeline (30m grid, H1 pattern bars, resting-limit entries, DYNA/RATCHET exits, funding + daily-slope gates) + live data fetchers |
| Strategy | `backend/strategies/black_swan_strategy/` | BaseStrategy adapter: platform signal mapping, branding, confidence, gates disclosure |
| Runner | `backend/strategies/black_swan_runner.py` | Isolated scheduler task: validate → market gate → window-deadline gate → 2-layer global dedup → commit-first/broadcast-second |
| Scheduler | `backend/main.py` | Job `black_swan_run`: every 30 min at **:03/:33 UTC** + a startup one-shot |
| Config | `backend/config.py` | `BLACK_SWAN_ENABLED` / `BLACK_SWAN_SIGNAL_EXPIRY_MINUTES` / `BLACK_SWAN_FETCH_BARS` |
| Monitor | `backend/services/signal_monitor.py` | (surgical patch) timeout reads each signal's own `expiry_minutes` |

**Zero-touch guarantee:** `backend/strategies/runner.py` (V45's runner) and
`signal_broadcast.py` are byte-identical to before the integration. Black Swan
shares the Signal table but has its own runner, scheduler job, timeout, and
diagnostics keys (`bs_*` in `/api/system/scheduler-status`).

## 2. The book, in one page

- **Assets:** BTCUSDT (4 gated streams) + SOLUSDT (1 stream). Longs only in
  production v1 (the frozen book's shorts are OOS-evidenced cuts).
- **Streams (PRIMARY):** `BTC:pull`, `BTC:eng`, `BTC:sweep`, `BTC:thrust`,
  `SOL:thrust`.
- **Deferred overflow units:** `BTC:pullB/pullC/thrustB/thrustC` — they need
  cross-tick slot-occupancy state to execute safely; shipping them without it
  would trade signals the book would have rejected. Deferred, disclosed in
  every signal's metadata.
- **Entry:** resting limit at `open − delta×ATR(H1)` (delta 0.25–0.70 per
  stream), valid **2×30m bars**; unfilled ⇒ cancelled. The book NEVER chases.
- **Stop:** `entry − sl×ATR` (sl 2.0–2.5 per stream). **TP1 (monitored):**
  the stream's guaranteed-win-trigger (pull/eng/sweep 2.0R, thrust 2.5R) —
  the excursion where the engine's own locks secure ≥ +1.0R, so marking the
  card "won" at TP1 never overclaims. **TP2 (informational):** the engine cap
  (5R–12R per stream).
- **Time stop:** 168–240 × 30m bars per stream.
- **BTC-long gates:** funding-crowding (last settled rate > 0.0003 blocks) +
  daily EMA50 5-day slope must be inside [−0.02, +0.05]. Both fail-safe:
  missing data ⇒ gate goes no-op, logged as a disclosed degradation.
- **Confidence:** the stream's realized frozen-book win rate — no fabricated
  ML probability.

## 3. Lifecycle & cadence

- Scheduler ticks at **:03 and :33**. The engine's executable bars are the
  **:30 opens**; the :33 tick evaluates the fresh bar (3-min lag), the :03
  tick is a retry net (its window gate rejects stale bars — the book never
  chases).
- Card window: `expiry_minutes = 7440` (124h = 5d 4h) — the book's longest
  full trade lifecycle (240×30m time stop = 120h) + the 2×30m order window.
- The monitor's timeout, the force-expire sweeper, and the expiry
  notifications all read `signal.expiry_minutes`, so V45's 8h cards are
  untouched and Black Swan's 124h cards expire correctly.

## 4. Deduplication (portfolio rule)

The runner applies the same two-layer scheme as V45's, **globally**:

1. skip if ANY ACTIVE signal exists for the same (symbol, direction) — across
   both strategies;
2. skip if a signal for the same (symbol, direction) was created in the last
   4h, regardless of strategy/status.

This means Black Swan and V45 cannot stack opposing positions on the same
symbol+direction. Intentional.

## 5. Diagnostics

`GET /api/system/scheduler-status` now includes:

```json
{
  "black_swan": { "enabled": true,
                  "schedule": "every 30 min at :03/:33 UTC (aligned to 30m exec grid)",
                  "next_scheduled_run": "..." },
  "bs_last_run_started_at": "...", "bs_last_run_finished_at": "...",
  "bs_last_run_error": null,
  "bs_last_run_signal_counts": { "bs_signals": 0, "bs_saved": 0, ... },
  "bs_total_runs": 42
}
```

Runner summary keys: `bs_saved`, `bs_broadcasts`, `bs_duplicates_skipped`,
`bs_validation_failures`, `bs_market_closed_skipped`,
`bs_stale_window_skipped`.

## 6. Ops runbook

- **Disable Black Swan:** set `BLACK_SWAN_ENABLED=false` in `.env` and restart.
  Nothing else changes — V45 keeps running (the job isn't even registered).
- **Data degradation:** OHLCV chain is Binance → MEXC → KuCoin → BinanceUSDM
  (every leg switch is logged). Funding comes from Binance USDT-M; if it
  fails the funding gate goes inactive (logged) and BTC longs are UNGATED —
  this matches the research engine's disclosed fail-safe.
- **No DB migration:** `strategy_type` is a String(32) column; "black_swan"
  just adds a new value. Old queries that filter by `strategy_type`
  are unaffected; queries that don't filter see both strategies — intended.
- **Tuning policy:** the engine's trading constants are FROZEN. Do not
  "tune" them from the dashboard/env — that desyncs live from the validated
  book. Any change must go through a full re-validation stage.

## 7. Verification ledger (what was proven before shipping)

1. **Engine parity** — the port's pipeline (to_h1/features/pattern_masks/
   masks/gates) is bit-exact vs the research engine on the full real history;
   primary-stream book reconstruction matches trade-for-trade.
2. **Live-path parity** — at known historical book trades, the live evaluation
   reproduces the book's entry geometry (limit/SL/TP) exactly.
3. **Contract tests** — `validate_signal` passes; monitor equivalence:
   480 min → byte-identical legacy behavior; 7440 → 124h; garbage → 8h
   fallback; notification strings byte-identical for V45.
4. **Import/E2E** — full `backend.main` imports with both strategies
   registered; scheduler-status exposes the Black Swan block.
5. **Live smoke** — real Binance paginated fetch (6000 bars/asset), real
   funding read, and a replay of the most recent book trades on the
   live-fetched data at 0.000% deviation.
