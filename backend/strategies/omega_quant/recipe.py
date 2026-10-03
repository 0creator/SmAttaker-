"""
OMEGA QUANT — SEALED RECIPE (ported verbatim from the certified root system).

Provenance: OMEGA_QUANT_SYSTEM.py T2.10 ERA (trials_cumulative 1109, 25
phases). The three-sword champions (MANDATE / MIDDLE / QUALITY) are the
T2.2/T2.3 certified configurations, re-verified live 3/3 anchors
bit-exact and launch-gate GO 8/8 (PHASE-XXV).

  MANDATE  PF 4.542  n=2009  WR 0.570  DD 0.0032
  MIDDLE   PF 4.769  n=1832  WR 0.578  DD 0.0041
  QUALITY  PF 4.349  n=1360  WR 0.549  DD 0.0051

SEALED LAW — read before touching anything in this package:
  * BE / trailing stops are BANNED in any overlay. No exception, ever.
  * sl0 is assigned ONCE per trade and never moves (_scan17 PURE rule).
  * Champions are not re-tuned here. The certified admission line is
    frozen: gates (qmap), occupancy (K), brackets (legs/tstop/chk/rmin)
    are copied from CHAMPIONS_T23 exactly.
  * Monotonic Improvement Pact: any change must go through the root
    certification program, never through a live edit.
"""
import os

# --- sealed geometry (PHASE-VI seal) -------------------------------- #
RR = 2.25            # final TP in risk units
VERT = 384           # vertical barrier: max hold in M15 bars (4 days)
PAD = 0.35           # stop pads beyond the zone far edge (ATR units)
CAP = 3.0            # stop distance cap (ATR units)
FLOOR_ATR = 0.50     # stop floor from entry (ATR units)
SL_ATR_MULT = 1.0    # risk unit = sl_dist (atr_e := sl_dist)

# --- sealed z-source (PHASE-IX saturation stream) ------------------- #
ZNAME, ZDISP, ZBODY = "z12b60", 1.20, 0.60   # weekly-OB birth thresholds
SANE, GAP = 32.0, 2                          # mask: sane_max / min_gap

# --- confirmation depths (PHASE-VII/X/XIV) -------------------------- #
DEPTHS = [0.20, 0.35, 0.50]
CONF_MAP_T19 = {"BTCUSD": 0.5, "ETHUSD": 0.5, "XAUUSD": 0.5}
W_CONF_UNCONFIRMED = 0.05                    # terminal (monotone-certified)
CAP_W = (0.10, 1.0)                          # asset-weight clip

# --- causal volrank (PHASE-XII) ------------------------------------- #
VOLRANK_WIN = 2880            # trailing M15 bars (30 days)
CALM_P = 0.20                 # calm-grade threshold (0.5x below)

# --- feature constants (SECTION 3 of the root system) ---------------- #
ATR_N = 14
SWING_H4_N, SWING_M15_N = 10, 5
H4_POOL_TTL, M15_POOL_TTL = 42, 100
FVG_MIN_ATR = 0.10
VOL_PCT_WIN = 2_000
TREND_EMA_D1, BIAS_EMA_H4 = 20, 50

# --- costs (per-side bps; summed for the R cost model) --------------- #
COSTS_BPS = {
    "BTCUSD": {"spread": 1.0, "slip": 1.0, "comm": 4.0},
    "ETHUSD": {"spread": 1.5, "slip": 1.5, "comm": 4.0},
    "XAUUSD": {"spread": 1.7, "slip": 0.5, "comm": 0.5},
}
# Ported (non-certified) assets use the PHASE-XXVIII convention: alt
# coins run the ETH-grade 1.5/1.5/4 = 7 bps; session assets (forex /
# stocks / futures / commodities) run a conservative 6 bps flat.
PORT_CRYPTO_BPS = 7.0
PORT_SESSION_BPS = 6.0

RISK_PCT = 0.01               # 1% concurrent equity-at-risk per asset
ANNUAL_BARS = 365 * 24 * 4    # M15 grid

# --- T2.3 champions: sealed deploy specs + expected anchors ---------- #
CHAMPIONS_T23 = {
    "MANDATE": dict(
        k=48, q=0.0, qmap={"BTCUSD": 0.35, "ETHUSD": 0.20, "XAUUSD": 0.20},
        rule="P50xT192+g35",
        exit=dict(legs=((0.50, 1.0),), tstop=True, chk=192, rmin=0.20,
                  vert_calm=None),
        status="crowned_strict_dominance",
        expect=dict(pf=4.542, n=2009, tpy=304.4, wr=0.570, dd=0.0032)),
    "MIDDLE": dict(
        k=36, q=0.0, qmap={"BTCUSD": 0.30, "ETHUSD": 0.15, "XAUUSD": 0.15},
        rule="P60xT192+g30",
        exit=dict(legs=((0.60, 1.0),), tstop=True, chk=192, rmin=0.20,
                  vert_calm=None),
        status="crowned_strict_dominance",
        expect=dict(pf=4.769, n=1832, tpy=277.1, wr=0.578, dd=0.0041)),
    "QUALITY": dict(
        k=24, q=0.15, qmap=None,
        rule="base-defender",
        exit=dict(legs=(), tstop=False, chk=96, rmin=0.20,
                  vert_calm=None),
        status="defender_held_recertified",
        expect=dict(pf=4.349, n=1360, tpy=205.7, wr=0.549, dd=0.0051)),
}
CHAMPIONS = CHAMPIONS_T23
BOOK_PRIORITY = ("MANDATE", "MIDDLE", "QUALITY")   # strictest gate first

# --- deployment protocol (owner law, PHASE-XVIII, adopted XIX) ------- #
DEPLOYMENT_PROTOCOL = dict(
    step0_btc_floor="FRESH DEPLOYMENTS ONLY: BTC at w=0.10 for the first "
        "365d, then the causal w_asset layer takes over (PHASE-XIX step0).",
    step1_carry_state="NEVER cold-start with full risk: initialize w_asset "
        "from a >=365d tracked record or run year one at probe risk.",
    step2_capital_floor="equity must exceed min_capital_usd per book "
        "(MANDATE $100k / MIDDLE $50k / QUALITY $50k).",
    step3_breaker="circuit breaker AVAILABLE but DEFAULT OFF.",
    step4_standdown="auto stand-down: rolling-100-trade PF < 1.0 over >=30 "
        "closed trades -> no new entries until human review.",
    step5_btc="BTC exposure suppressed causally by the weight layer; do "
        "NOT override manually.",
    banned="BE / trailing stops remain banned in ANY overlay - no "
        "exception, ever.",
)
MIN_CAPITAL_USD = {"MANDATE": 100_000, "MIDDLE": 50_000, "QUALITY": 50_000}

# --- asset health guard (ported universe; the owner's avoidance law) -- #
# Law: "اذا يوجد اصل اداءه سيء جدا فقم بتجنبه" — an asset whose own
# CAUSAL measured performance on the ported book is net-losing is
# skipped for new entries until its expanding record recovers.
# The certified line (BTC/ETH/XAU) is NOT touched by the guard: its
# admission is frozen by certification (bit-exact anchors); its risk is
# governed by the causal w_asset layer + step0/step4 protocol instead.
GUARD_MIN_BARS = 3_000      # M15 warmup before a ported asset may fire
GUARD_MIN_TRADES = 30       # measured closed trades before the guard judges
GUARD_PF_FLOOR = 1.0        # expanding PF below this -> asset avoided

FORBIDDEN_REASONS = ("be", "trail", "run")
CLS17 = {"TP_FULL": ("tp",), "TP_PART": ("tp1+tp",), "PART_SL": ("tp1+sl",),
         "PART_VB": ("tp1+vb",), "SL": ("sl",), "TSTOP": ("tstop",),
         "VB": ("vb",)}

# --- honesty card constants (quoted on every signal) ----------------- #
MEASURED = {
    "MANDATE": {"pf": 4.542, "n": 2009, "wr": 0.570, "dd": 0.0032,
                "tpy": 304.4},
    "MIDDLE":  {"pf": 4.769, "n": 1832, "wr": 0.578, "dd": 0.0041,
                "tpy": 277.1},
    "QUALITY": {"pf": 4.349, "n": 1360, "wr": 0.549, "dd": 0.0051,
                "tpy": 205.7},
}
ROOT_SYSTEM = "OMEGA_QUANT_SYSTEM.py T2.10 ERA (1109 trials / 25 phases)"

CERTIFIED_ASSETS = ("BTC", "ETH", "XAU")     # the certified long-only line
ASSET_TO_CERTKEY = {"BTC": "BTCUSD", "ETH": "ETHUSD", "XAU": "XAUUSD"}

# --- live cadence / order semantics ---------------------------------- #
SCAN_OFFSET_MIN = 2          # run at H:02/H:17/H:32/H:47 UTC
ORDER_EXPIRY_MINUTES = 15    # one M15 bar: the certified entry is the
                             # NEXT bar open — fills must happen now
CERTIFIED_LONG_ONLY = True   # cell longNotHi66: the certified line is
                             # long-only (PHASE-XIX mirror refusal stands)


def cost_bps_for(asset_key: str, asset_class: str) -> float:
    """Total per-side cost bps for an asset key ("BTCUSDT"/"PAXGUSDT"/...).

    The certified line must ALWAYS get its certified cost model, including
    XAU: live gold flows through the tokenized PAXGUSDT scan key, so the
    prefix map carries PAXG -> XAUUSD explicitly (a plain XAU prefix check
    would miss it and silently over-charge the certified XAU ledger with
    the 7 bps port costs)."""
    base = str(asset_key).upper()
    _CERT_PREFIX = (("BTC", "BTCUSD"), ("ETH", "ETHUSD"),
                    ("PAXG", "XAUUSD"), ("XAU", "XAUUSD"))
    for pref, cert in _CERT_PREFIX:
        if base.startswith(pref):
            return float(sum(COSTS_BPS[cert].values()))
    return PORT_CRYPTO_BPS if (asset_class or "crypto") == "crypto" \
        else PORT_SESSION_BPS
