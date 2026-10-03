# 🦅 SmAttaker Trading System — Complete Production System

> **Elite AI-Powered Trading Signal & Portfolio Management**
> Crypto · Gold · Forex · Stocks — Telegram Bot + API

---

## 📊 System Overview

| Component | Status | Size |
|-----------|--------|------|
| Backend API (FastAPI) | ✅ 7 route modules | 4.2KB main |
| Telegram Bot | ✅ 12 handlers + keyboards | 4.7KB bot |
| SmAttaker V43 Strategy (unified) | ✅ Production ML | 25.5KB |
| Strategy Engines | ✅ unified engine + registry | 75KB total |
| Data Fetcher (CCXT + yfinance) | ✅ Live OHLCV | 11.2KB |
| ML Models (Crypto) | ✅ 17 models | ~300KB |
| ML Models (Aurum) | ✅ 3 core models | ~3.2MB |
| BTC Regime (Live) | ✅ 5MB npz + live compute | 5MB |
| Exchange Connector (CCXT) | ✅ 100+ exchanges | 5.7KB |
| Analytics Engine | ✅ Sharpe, EV, Rankings | 12.6KB |
| Payments (Crypto) | ✅ NOWPayments | 8.8KB |
| Risk Management | ✅ Full flexibility | 6.7KB model |
| Database Schema | ✅ 9 tables | 10.6KB SQL |
| Deployment | ✅ Docker + Render | ready |

**Total: 65+ files, production-ready.**

---

## 🚀 Quick Start — From Zero to Live

### Step 0: Prerequisites
```
- Python 3.12+
- Git
- A Telegram Bot Token (from @BotFather)
- Free Supabase account
- Free Upstash Redis account
```

### Step 1: Clone & Install
```bash
cd smattaker
pip install -r requirements.txt
```

### Step 2: Set Up Database (Supabase — Free)
1. Go to https://supabase.com → Create Project
2. In SQL Editor, paste and run: `scripts/init_db.sql`
3. Copy the connection string (Settings → Database → Connection String → URI)
4. Replace `[YOUR-PASSWORD]` in the URI

### Step 3: Set Up Redis (Upstash — Free)
1. Go to https://upstash.com → Create Redis Database
2. Copy the `REDIS_URL`

### Step 4: Configure `.env`
```bash
cp .env.example .env
```
Fill in:
```
TELEGRAM_BOT_TOKEN=your_bot_token
DATABASE_URL=postgresql+asyncpg://postgres:password@host:6543/postgres
REDIS_URL=redis://default:password@host:6379
SECRET_KEY=generate-a-random-string
ENCRYPTION_KEY=generate-with-fernet
ADMIN_EMAIL=amanossama@gmail.com
NOWPAYMENTS_API_KEY=your_api_key  # for crypto payments
```

### Step 5: Seed Admin
```bash
python scripts/seed_admin.py
# Then update the admin telegram_id in the database
```

### Step 6: Run
```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

The bot starts automatically with the server!

---

## 🤖 Telegram Bot Commands

| Command | Function |
|---------|----------|
| `/start` | Launch & authenticate |
| `/menu` | Main navigation menu |
| `/portfolio` | Demo + Real portfolio |
| `/signals` | Active trading signals |
| `/trades` | Trading journal |
| `/analytics` | Performance analytics |
| `/risk` | Risk management settings |
| `/subscribe` | Subscription plans |
| `/admin` | Admin control panel |
| `/language` | EN ↔ عربي |
| `/help` | Help center |

---

## 🧠 The Strategies

### Strategy 1 — V45.4.1 APEX Unified (crypto + gold + forex + stocks)
- **34 crypto + 11 forex + 17 stocks + 2 commodities**, 64 trained models
- 26 leak-free triggers (shift(1)) + meta-labeling filter (per-fold features)
- Single source of truth: live inference imports the engine's own
  `build_features()` — zero drift between training and live paths
- Kelly sizing, per-symbol thresholds, honest PRO signal cards
- Data: 1h, CCXT multi-exchange chain / Twelve Data / yfinance
- Runs hourly at :02 UTC (`strategy_run` job)

### Strategy 2 — 🔱 OMEGA QUANT (certified three-sword M15 book)
- **Bit-exact port of the sealed OMEGA_QUANT_SYSTEM (T2.10 ERA, 1109
  trials / 25 phases)** — the strongest certified configuration of that
  research program. The port harness reproduces the sealed anchors from
  raw M15 data through the shipped code:
  - MANDATE  PF 4.542 · n=2009 · WR 0.570 · DD 0.32% · tpy 304
  - MIDDLE   PF 4.769 · n=1832 · WR 0.578 · DD 0.41% · tpy 277
  - QUALITY  PF 4.349 · n=1360 · WR 0.549 · DD 0.51% · tpy 206
  - Event identity vs the sealed evcache: BTC/ETH/XAU bit-exact
    (sig_ts/t0/dir/entry_px/atr_e).
- **Recipe (frozen)**: weekly order-block in-zone retest mask (z12b60
  displacement birth, sane zone < 32×ATR, h4-bias + weekly-bias sign
  filters, 2-bar gap dedupe) → certified cell `longNotHi66` (long-only,
  vol regime < 0.66) → per-book volrank gate (trailing 2880-bar ATR
  percentile: MANDATE BTC 0.35/ETH 0.20/XAU 0.20 · MIDDLE
  0.30/0.15/0.15 · QUALITY 0.15 global) → K-slot occupancy (48/36/24)
  → certified bracket exits (tp1 leg at 1R for MANDATE/MIDDLE, 192-bar
  time stop, 384-bar vertical barrier; RR 2.25; SL behind the zone far
  edge in H4-ATR units). **BE / trailing stops are banned — structurally
  absent from the exit engine.**
- **Certified line (BTC/ETH/XAU) runs exactly as certified.** Every
  other asset runs the SAME recipe as a PORTED book and is NOT
  independently certified — the signal card says so and quotes the
  asset's own measured expanding stats (>=30 closed trades) or the
  champion reference.
- **Asset health guard (owner's avoidance law)**: a ported asset whose
  expanding PF(R_net) < 1.0 over >=30 measured closed trades is AVOIDED
  for new entries until its causal record recovers. The certified line
  is exempt (risk governed by the frozen causal weights + the
  PHASE-XVIII deployment protocol).
- Universe toggle: `OMEGA_UNIVERSE` — default `ALL` (the whole registry;
  the guard avoids the measured-bad assets), `CERTIFIED` (BTC/ETH/XAU
  only), or a comma list like `BTC,ETH,XAU,SOL,DOGE`.
- Signals are LONG-ONLY (the certified cell is long-only — the mirror
  refusal is sealed), evaluated on the LAST CLOSED M15 bar and broadcast
  immediately (the certified entry convention is the next bar open;
  the ledger repairs the entry to the actual open and measures the
  residual slippage).
- Runs at :02/:17/:32/:47 UTC (`omega_quant_run` job — 2 minutes after
  each 15m bar close). Signals carry the 🔱 OMEGA QUANT badge.
- Toggle: `OMEGA_ENABLED` (default on). State ledger (occupancy + guard)
  lives in `backend/strategies/omega_quant/cache/omega_state.json`.

### Aurum Core v2 — Gold / Forex / Stocks (legacy models retained)
- **16 assets** with trained models (XAUUSD, EURUSD, GBPUSD, AAPL, TSLA, etc.)
- CUSUM event detection (sample only when information arrives)
- 3 event sources: London Breakout + NY Fade + CUSUM
- Walk-forward asymmetric barrier optimization (PT:SL per source+regime)
- Regime-conditional stacked ensemble (Trend + Range + Global)
- Isotonic calibration (raw probs → real probabilities)
- Continuous Kelly position sizing
- Triple-Barrier labeling (close-only, ZERO intrabar illusions)
- Data: M30/H1 from yfinance

---

## 📊 Analytics (Institutional Grade)

| Metric | Description |
|--------|-------------|
| Win Rate % | (Winning / Total) × 100 |
| Profit Factor | Gross Profit / Gross Loss |
| Expected Value (EV) | Avg R per trade |
| Sharpe Ratio | Risk-adjusted return |
| Sortino Ratio | Downside risk-adjusted |
| Max Drawdown | Peak-to-trough decline |
| Equity Curve | Portfolio growth over time |
| R-Heatmap | Monthly P&L heatmap |
| Instrument Ranking | Per-symbol WR%, PF, Streaks |

---

## 💳 Payment System

- **Crypto only** via NOWPayments
- 300+ coins supported (USDT, BTC, ETH, etc.)
- Automatic IPN webhook confirmation
- Manual TX hash verification fallback
- Admin can confirm/reject payments

### Subscription Flow:
```
User → Request Trial (3 days)
   → Admin approves via /admin panel
   → Account activated

User → Pay with Crypto
   → NOWPayments invoice generated
   → Webhook auto-confirms OR admin verifies
   → Account activated ($99/month)
```

---

## 👑 Admin Panel (`/admin` in bot)

- **User Management**: Add, ban, delete users
- **Trial Approvals**: Accept/reject free trial requests
- **Price Control**: Change subscription price
- **Broadcast**: Send messages to all users
- **Notifications**: Real-time alerts for registrations, payments
- **Analytics**: Revenue, user counts, system health

---

## 🏗 Deploy to Production

### Option A: Render (Free Tier)
1. Push to GitHub
2. Connect to render.com
3. `render.yaml` handles everything
4. Set environment variables in Render dashboard
5. **Use UptimeRobot** (free) to ping `/health` every 5 min → bot never sleeps

### Option B: Docker
```bash
docker-compose -f docker/docker-compose.yml up -d
```

### Option C: Any VPS
```bash
git clone ...
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

---

## 📁 Complete File Structure
```
smattaker/
├── backend/
│   ├── main.py                    # FastAPI entry + bot startup
│   ├── config.py                  # All settings
│   ├── database.py                # Async PostgreSQL
│   ├── redis_client.py            # Redis connection
│   ├── api/                       # 7 REST route modules
│   │   ├── auth.py, users.py, signals.py
│   │   ├── trades.py, analytics.py, payments.py
│   ├── bot/                       # Telegram Bot
│   │   ├── bot.py                 # Init + handlers
│   │   ├── handlers/              # 12 command handlers
│   │   ├── keyboards/             # Inline keyboards
│   │   └── templates/             # EN + AR messages
│   ├── models/                    # 9 SQLAlchemy models
│   ├── schemas/                   # 7 Pydantic schemas
│   ├── services/                  # Signal broadcast + executor
│   ├── strategies/                # ML Strategy Engine
│   │   ├── v45_strategy/          # Strategy 1: V45.4.1 APEX unified
│   │   ├── omega_quant/           # Strategy 2: certified three-sword book
│   │   ├── engines/               # v45 engine + model registry
│   │   ├── data_fetcher.py        # CCXT + Twelve Data + yfinance
│   │   └── runner.py              # Scheduled strategy runner
│   ├── exchange/                  # CCXT connector (100+ exchanges)
│   ├── models_ml/                 # Trained ML models
│   │   ├── crypto/                # 23 files (models + regime + features)
│   │   └── aurum/                 # 16 joblib model bundles
│   └── utils/                     # Security + helpers
├── scripts/
│   ├── init_db.sql                # Full database schema
│   └── seed_admin.py              # Create admin user
├── docker/
│   ├── docker-compose.yml
│   └── Dockerfile.backend
├── requirements.txt               # All Python dependencies
├── .env.example                   # Environment template
└── render.yaml                    # Render deploy config
```

---

## ⚠️ Notes

1. **BTC Regime**: Now computed LIVE from Binance at every strategy run. The `btc_regime.npz` file is a fallback only.
2. **Missing Models**: 7 crypto + 13 aurum models are still in the ZIP. Upload them to `backend/models_ml/` to enable those symbols. System skips gracefully.
3. **Admin Telegram ID**: Update `seed_admin.py` with your actual Telegram ID after running it.
4. **NOWPayments**: Sign up at nowpayments.io (free) to get your API key for crypto payments.

---

**🦅 SmAttaker — Built for elite traders. Ready for production.**
