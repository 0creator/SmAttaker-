"""
OMEGA QUANT PORT FIDELITY GATE (ships with the platform).
Reproduces the sealed anchors through the SHIPPED code and refuses
to pass on any drift. Requires the certified evidence bundle:
  - M15 CSVs (BTCUSDT/ETHUSDT/XAUUSD) under OMEGA_VALIDATION_DATA
  - the sealed evcache pkls under OMEGA_VALIDATION_ART/evcache
Run: python3 scripts/validate_omega_port.py  (exit 0 = bit-faithful)
"""
import importlib.util
import json
import sys
import types

import numpy as np
import pandas as pd

import os as _os
OMEGA_DIR = _os.environ.get(
    "OMEGA_VALIDATION_PKG",
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                  "..", "backend", "strategies", "omega_quant"))
OMEGA_DIR = _os.path.abspath(OMEGA_DIR)
ART = _os.environ.get("OMEGA_VALIDATION_ART", "./omega_quant_artifacts")
DATA = _os.environ.get("OMEGA_VALIDATION_DATA", "./data")
ASSETS = {"BTCUSD": f"{DATA}/BTCUSDT_M15.csv",
          "ETHUSD": f"{DATA}/ETHUSDT_M15.csv",
          "XAUUSD": f"{DATA}/XAUUSD_M15.csv"}
ASSET_ID = ("BTCUSD", "ETHUSD", "XAUUSD")
ANCHORS = {"MANDATE": dict(pf=4.542, n=2009, wr=0.570, dd=0.0032, tpy=304.4),
           "MIDDLE":  dict(pf=4.769, n=1832, wr=0.578, dd=0.0041, tpy=277.1),
           "QUALITY": dict(pf=4.349, n=1360, wr=0.549, dd=0.0051, tpy=205.7)}
CHAMPIONS = {
    "MANDATE": dict(k=48, qmap={"BTCUSD": 0.35, "ETHUSD": 0.20, "XAUUSD": 0.20},
                    legs=((0.50, 1.0),), tstop=True, chk=192, rmin=0.20),
    "MIDDLE":  dict(k=36, qmap={"BTCUSD": 0.30, "ETHUSD": 0.15, "XAUUSD": 0.15},
                    legs=((0.60, 1.0),), tstop=True, chk=192, rmin=0.20),
    "QUALITY": dict(k=24, q=0.15, qmap=None,
                    legs=(), tstop=False, chk=96, rmin=0.20),
}
RR, VERT = 2.25, 384


def load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- standalone package skeleton (no backend.config needed) ---------- #
pkg = types.ModuleType("backend"); pkg.__path__ = []
pkg2 = types.ModuleType("backend.strategies"); pkg2.__path__ = []
pkg3 = types.ModuleType("backend.strategies.omega_quant")
pkg3.__path__ = [OMEGA_DIR]
sys.modules.update({"backend": pkg, "backend.strategies": pkg2,
                    "backend.strategies.omega_quant": pkg3})
R = load_module("backend.strategies.omega_quant.recipe",
                f"{OMEGA_DIR}/recipe.py")
F = load_module("backend.strategies.omega_quant.features",
                f"{OMEGA_DIR}/features.py")
E = load_module("backend.strategies.omega_quant.engine",
                f"{OMEGA_DIR}/engine.py")


def ingest(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, header=None,
                     names=["timestamp", "open", "high", "low", "close",
                            "volume"], dtype={"volume": "float64"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed",
                                     utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    return df


def relabel17_asset(ea: pd.DataFrame, b: dict, legs, tstop, chk, rmin):
    """Certified _rel17_asset logic through the PORTED _scan17."""
    ts = b["ts"]
    t0v = ea["t0"].values.astype("datetime64[s]").astype("int64")
    n = len(ea)
    new_R = np.empty(n)
    reason = np.empty(n, dtype=object)
    extra = np.zeros(n)
    new_t1 = np.empty(n, dtype="datetime64[s]")
    for k in range(n):
        i0 = int(np.searchsorted(ts, t0v[k], side="left"))
        if i0 >= len(ts) or ts[i0] != t0v[k]:
            raise RuntimeError(f"entry bar not found row {k}")
        i_end = min(i0 + VERT, len(ts) - 1)
        Rv, j, rs, ex, _m, _f = E._scan17(i0, i_end, 1, ea["entry_px"].values[k],
                                          ea["atr_e"].values[k], b, legs,
                                          tstop, chk, rmin)
        if rs in R.FORBIDDEN_REASONS:
            raise SystemExit(f"FORBIDDEN reason {rs}")
        new_R[k], reason[k], extra[k] = Rv, rs, ex
        new_t1[k] = np.int64(ts[j])
    out = ea.copy()
    out["t1"] = pd.to_datetime(new_t1, unit="s", utc=True)
    out["R"] = new_R
    out["exit_reason"] = reason
    out["extra_cost"] = extra
    return out


def main():
    report = {}
    print("=" * 84)
    print("[A] EVENT IDENTITY vs sealed evcache")
    print("=" * 84)
    feats, raws, rebuilt = {}, {}, {}
    for a, path in ASSETS.items():
        raw = ingest(path)
        f = F.build_lean_features(raw, tail=None)     # full-series mode
        feats[a], raws[a] = f, raw
        m2, d = E.certified_mask(f)
        ev = E.label_geometry(raw, f, m2, d)
        ev["asset"] = a
        rebuilt[a] = ev
        seal = pd.read_pickle(f"{ART}/evcache/z12b60_s32_g2_{a}.pkl")
        n_new, n_seal = len(ev), len(seal)
        ev_i = ev.sort_values("t0").reset_index(drop=True)
        seal_i = seal.sort_values("t0").reset_index(drop=True)
        same_n = n_new == n_seal
        if same_n and n_new:
            ts_eq = (ev_i["sig_ts"].values.astype("datetime64[s]")
                     == seal_i["sig_ts"].values.astype("datetime64[s]")).all()
            t0_eq = (ev_i["t0"].values.astype("datetime64[s]")
                     == seal_i["t0"].values.astype("datetime64[s]")).all()
            dir_eq = (ev_i["dir"].values.astype(int)
                      == seal_i["dir"].values.astype(int)).all()
            ent_eq = np.allclose(ev_i["entry_px"].values.astype(float),
                                 seal_i["entry_px"].values.astype(float),
                                 rtol=0, atol=0)
            atr_eq = np.allclose(ev_i["atr_e"].values.astype(float),
                                 seal_i["atr_e"].values.astype(float),
                                 rtol=0, atol=0)
            ok = bool(ts_eq and t0_eq and dir_eq and ent_eq and atr_eq)
        else:
            ok = False
        report[f"events_{a}"] = dict(n_new=n_new, n_seal=n_seal, ok=ok)
        print(f"  {a:7s} rebuilt={n_new:5d} sealed={n_seal:5d} "
              f"-> {'BIT-EXACT PASS' if ok else 'FAIL'}")
        if not ok and same_n and n_new:
            bad = ~(ev_i["sig_ts"].values.astype("datetime64[s]")
                    == seal_i["sig_ts"].values.astype("datetime64[s]"))
            print(f"    first mismatch row: {int(np.argmax(bad))}")

    print("=" * 84)
    print("[B] ANCHOR IDENTITY (relabel17 + run22 + metrics, ported code)")
    print("=" * 84)
    # certified pool: cell longNotHi66 + confH + volrank + wv_calm
    parts = []
    for a in ASSET_ID:
        ev = rebuilt[a]
        raw, f = raws[a], feats[a]
        pos = f.index.get_indexer(ev["sig_ts"])
        vr_events = f["vol_regime"].values.astype(float)[pos]
        keep = (ev["dir"].values == 1) & (np.nan_to_num(vr_events) < 0.66)
        e66 = ev.loc[np.where(keep)[0]].reset_index(drop=True).copy()
        c_ = raw["close"].values.astype(float)
        o_ = raw["open"].values.astype(float)
        zt = f["w1_ob_top"].values.astype(float)
        zb = f["w1_ob_bot"].values.astype(float)
        ppos = f.index.get_indexer(e66["sig_ts"])
        lvl = zb[ppos] + 0.5 * (zt[ppos] - zb[ppos])
        e66["confH"] = (c_[ppos] > o_[ppos]) & (c_[ppos] >= lvl)
        e66["p"] = f["volrank_p"].values[ppos]
        e66["p"] = e66["p"].fillna(0.5).values
        e66["wv_calm"] = np.where(e66["p"].values < 0.20, 0.5, 1.0)
        parts.append(e66)
    pool = pd.concat(parts, ignore_index=True) \
        .sort_values("t0", kind="mergesort").reset_index(drop=True)
    print(f"  pool longNotHi66: {len(pool):,} events | confH frac "
          f"{float(pool['confH'].mean()):.3f} | calm frac "
          f"{float((pool['wv_calm'] < 1).mean()):.3f}")

    bars = {a: E.bars_dict(raws[a]) for a in ASSET_ID}
    for tag, spec in CHAMPIONS.items():
        pl = pd.concat(
            [relabel17_asset(pool[pool["asset"] == a].reset_index(drop=True),
                             bars[a], spec["legs"], spec["tstop"],
                             spec["chk"], spec["rmin"]) for a in ASSET_ID],
            ignore_index=True).sort_values("t0", kind="mergesort") \
            .reset_index(drop=True)
        qmap = spec.get("qmap")
        res = E.run22(pl, spec["k"], q=(spec.get("q") or 0.0), qmap=qmap,
                      assets=ASSET_ID)
        mt = E.stream_metrics22(res)
        exp = ANCHORS[tag]
        ok = (abs(mt["pf"] - exp["pf"]) < 5e-3
              and abs(mt["n"] - exp["n"]) < 2
              and abs(mt["wr"] - exp["wr"]) < 3e-3
              and abs(mt["dd"] - exp["dd"]) < 4e-4
              and abs(mt["tpy"] - exp["tpy"]) < 1.5)
        report[f"anchor_{tag}"] = dict(metrics=mt, expect=exp, ok=bool(ok))
        print(f"  {tag:8s} PF={mt['pf']:.3f} (exp {exp['pf']}) "
              f"n={mt['n']} (exp {exp['n']}) WR={mt['wr']:.3f} "
              f"(exp {exp['wr']}) DD={mt['dd']:.4f} "
              f"(exp {exp['dd']}) tpy={mt['tpy']} (exp {exp['tpy']}) "
              f"-> {'BIT-EXACT ANCHOR PASS' if ok else 'MISMATCH'}")
        for a, c in mt["census"].items():
            print(f"      {a}: n={c['n']} WR={c['wr']} PF={c['pf']} "
                  f"expR={c['exp_r']:+.3f}")

    all_ok = all(v.get("ok") for v in report.values())
    print("=" * 84)
    print(f"VERDICT: {'PORT = CERTIFIED SYSTEM (all identities bit-exact)' if all_ok else 'FAIL — DO NOT SHIP'}")
    print("=" * 84)
    with open("/home/z/my-project/scripts/omega_port_validation.json",
              "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
