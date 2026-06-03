#!/usr/bin/env python3
"""
oil_backtest_v2.py  —  WTI Crude Oil: Research-Backed Signal Model
═══════════════════════════════════════════════════════════════════════
Built from academic papers and professional practitioner frameworks:

  Kilian (2009) AER:         Demand decomposition — copper+SPY = global demand shock
  Baumeister-Kilian-Zhou 2018: RBOB crack spread = #1 product spread predictor
  Goldman Sachs desk:        Brent-WTI spread + backwardation = physical tightness
  Pierre Andurand framework: Positioning z-score + fundamental balance
  CME Group practice:        ORB outperforms VWAP pullback for trending commodities

Components tested vs v1 (which used trend+copper+mom+SPY):
  NEW:  RBOB crack spread      RB=F×42 − CL=F 5d direction
  NEW:  Brent-WTI spread       BZ=F − CL=F z-score narrowing
  NEW:  Term structure proxy   USO/USL ratio direction (backwardation signal)
  NEW:  HO crack spread        HO=F×42 − CL=F direction (heating oil demand)
  NEW:  OIH vs CL divergence   OIH/CL ratio direction (sustainability signal)
  KEEP: CL EMA50 trend         Macro price trend
  KEEP: Copper 5d momentum     Kilian aggregate demand shock proxy
  KEEP: CL 5d momentum         Raw momentum
  KEEP: SPY 5d momentum        Risk-on / risk-off

Regime gates tested:
  XOP-SPY 30d correlation      < 0.65 = fundamental regime (use oil signals)
  EIA Wednesday handling       Skip / trade / ORB-post-EIA
  Bad month skip               From v1: Jan Apr Jun Sep Oct
  Score threshold              3, 5, 7, 9 (of max 9 possible)

Entry methods tested:
  Method A: Enter at session open (14:30 UTC)
  Method B: Enter at bar 2 (15:30 UTC) — post-EIA on Wednesday; calmer on others
"""

import yfinance as yf
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

RISK_PCT  = 0.03
SL_MULT   = 0.75
TP1_MULT  = 1.50
TP2_MULT  = 2.50
TP1_FRAC  = 0.60

SESS_START   = 14.5    # 14:30 UTC = 10:30 ET (after pre-open noise)
SESS_CLOSE   = 19.0    # 19:00 UTC = 15:00 ET (pit close)
EIA_UTC      = 15.5    # 15:30 UTC = 10:30 ET (Wednesday EIA release)

# ══════════════════════════════════════════════════════════════════
# 1. DOWNLOAD
# ══════════════════════════════════════════════════════════════════
print("=" * 70)
print("WTI CRUDE OIL  v2  —  Research-Backed Signal Model")
print("=" * 70)
print("\nSources: Kilian 2009 · Baumeister-Kilian-Zhou 2018 · GS commodity desk")
print("         Andurand framework · CME Group practitioner research\n")
print("Downloading 12 tickers…")

def dl(sym, period="3y"):
    df = yf.download(sym, period=period, interval="1d",
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c,tuple) else c.lower()
                  for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df

def dl_h(sym):
    df = yf.download(sym, period="2y", interval="1h",
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c,tuple) else c.lower()
                  for c in df.columns]
    if df.index.tz:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df

cl_d   = dl("CL=F")           # WTI crude
bz_d   = dl("BZ=F")           # Brent crude
rb_d   = dl("RB=F")           # RBOB gasoline ($/gal → ×42 for $/bbl)
ho_d   = dl("HO=F")           # Heating oil  ($/gal → ×42 for $/bbl)
hg_d   = dl("HG=F")           # Copper (Dr. Copper — aggregate demand proxy)
dxy_d  = dl("DX-Y.NYB")       # DXY
spy_d  = dl("SPY")             # S&P 500 (risk-on)
xop_d  = dl("XOP")             # E&P ETF (regime gate)
oih_d  = dl("OIH")             # Oil services (lagging sustainability signal)
uso_d  = dl("USO")             # Front-month oil ETF
usl_d  = dl("USL")             # 12-month oil ETF → USO/USL = term structure proxy
vix_d  = dl("^VIX")
print("  Daily 3yr: 12 symbols ✓")

cl_1h = dl_h("CL=F")
print(f"  1h   2yr: {cl_1h.index[0].date()} → {cl_1h.index[-1].date()}  "
      f"({len(set(cl_1h.index.date))} trading days)\n")

# ══════════════════════════════════════════════════════════════════
# 2. MACRO SIGNALS
# ══════════════════════════════════════════════════════════════════
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def pct5(s):   return s.pct_change(5)
def align(src, ref): return src["close"].reindex(ref.index, method="ffill")

cl  = cl_d["close"]
bz  = align(bz_d,  cl_d)
rb  = align(rb_d,  cl_d) * 42   # $/gal → $/bbl
ho  = align(ho_d,  cl_d) * 42
hg  = align(hg_d,  cl_d)
dx  = align(dxy_d, cl_d)
spy = align(spy_d, cl_d)
xop = align(xop_d, cl_d)
oih = align(oih_d, cl_d)
uso = align(uso_d, cl_d)
usl = align(usl_d, cl_d)
vix = align(vix_d, cl_d)

# Crack spreads (Baumeister-Kilian-Zhou 2018: RBOB > HO > 3-2-1)
crack_rb = rb - cl         # RBOB crack  ($/bbl) — BKZ 2018 #1 product spread
crack_ho = ho - cl         # Heating oil crack

# Brent-WTI spread z-score (60d) — physical tightness
bwti        = bz - cl
bwti_z60    = (bwti - bwti.rolling(60).mean()) / (bwti.rolling(60).std() + 1e-9)

# Term structure proxy: USO (front-month) / USL (12-month)
# Rising ratio = front outperforming = backwardation = bullish
termo       = uso / usl

# OIH/CL ratio: oil services vs crude price
# OIH leading CL on 20d basis = producers believe price sustainable = bullish
oih_cl      = oih / cl

# XOP-SPY correlation (regime detector — not a directional signal)
xop_ret     = xop.pct_change()
spy_ret     = spy.pct_change()
xop_spy_corr= xop_ret.rolling(30).corr(spy_ret)

macro = pd.DataFrame(index=cl_d.index)
macro["cl"]         = cl
macro["vix"]        = vix
macro["bwti_z60"]   = bwti_z60
macro["xop_spy_corr"]= xop_spy_corr

# 9 directional components (each ±1)
macro["c_trend"]    = np.where(cl > ema(cl, 50), 1, -1)              # EMA50 trend
macro["c_copper"]   = np.where(pct5(hg) > 0, 1, -1)                 # Kilian demand shock
macro["c_mom"]      = np.where(pct5(cl) > 0, 1, -1)                  # Momentum
macro["c_spy"]      = np.where(pct5(spy) > 0, 1, -1)                 # Risk-on
macro["c_crack_rb"] = np.where(crack_rb > crack_rb.shift(5), 1, -1)  # BKZ 2018 #1 signal
macro["c_crack_ho"] = np.where(crack_ho > crack_ho.shift(5), 1, -1)  # Heating oil demand
macro["c_brent"]    = np.where(bwti_z60 < -0.2, 1,                   # Spread narrowing = bullish WTI
                      np.where(bwti_z60 >  0.5, -1, 0))               # 0 = neutral spread
macro["c_termstruct"]= np.where(pct5(termo) > 0, 1, -1)              # USO outperforms USL = backwardation
macro["c_oih"]      = np.where(pct5(oih_cl) > 0, 1, -1)             # OIH leading crude = sustainable
macro["month"]      = pd.to_datetime(macro.index).month
macro               = macro.dropna()

ALL_COLS  = ["c_trend","c_copper","c_mom","c_spy",
             "c_crack_rb","c_crack_ho","c_brent","c_termstruct","c_oih"]
V1_COLS   = ["c_trend","c_copper","c_mom","c_spy"]   # v1 best

# ══════════════════════════════════════════════════════════════════
# 3. DAILY ATR
# ══════════════════════════════════════════════════════════════════
_d = cl_d[["high","low","close"]].copy()
_d["tr"] = np.maximum(_d["high"]-_d["low"],
            np.maximum(abs(_d["high"]-_d["close"].shift(1)),
                       abs(_d["low"] -_d["close"].shift(1))))
_d["atr14"] = _d["tr"].ewm(span=14, adjust=False).mean()
datr = _d["atr14"]

N_DAYS = len(set(cl_1h.index.date))

# ══════════════════════════════════════════════════════════════════
# 4. SIMULATE
# ══════════════════════════════════════════════════════════════════
def simulate(cols=None, score_min=3, skip_months=None, skip_wed=False,
             post_eia_wed=False, enter_bar2=False,
             regime_gate=False, regime_corr_max=0.65):
    cols        = cols or V1_COLS
    skip_months = skip_months or []
    trades      = []

    for d in sorted(set(cl_1h.index.date)):
        d_ts   = pd.Timestamp(d)
        m_rows = macro[macro.index <= d_ts]
        if len(m_rows) < 20:
            continue

        row = m_rows.iloc[-1]
        if row["month"] in skip_months:
            continue
        if float(row["vix"]) >= 30:
            continue

        is_wed = d_ts.weekday() == 2
        if skip_wed and is_wed:
            continue

        # Regime gate: only trade when XOP-SPY correlation is low (fundamental regime)
        if regime_gate:
            corr = float(row["xop_spy_corr"])
            if not np.isnan(corr) and corr > regime_corr_max:
                continue

        # Score from selected components (handle c_brent which can be 0)
        score_parts = []
        for c in cols:
            v = float(row[c])
            if not np.isnan(v):
                score_parts.append(v)
        score     = int(sum(score_parts))
        direction = 1 if score > 0 else -1
        if abs(score) < score_min:
            continue

        datr_r = datr[datr.index <= d_ts]
        if len(datr_r) < 14:
            continue
        daily_atr = float(datr_r.iloc[-1])
        if daily_atr <= 0 or np.isnan(daily_atr):
            continue

        # Entry bar timing
        if post_eia_wed and is_wed:
            bar_offset = 1      # enter at 2nd bar (15:30 UTC) — post-EIA direction confirmed
        elif enter_bar2:
            bar_offset = 1
        else:
            bar_offset = 0

        entry_ts = d_ts + pd.Timedelta(hours=SESS_START)
        end_ts   = d_ts + pd.Timedelta(hours=SESS_CLOSE)
        bars = cl_1h.loc[(cl_1h.index >= entry_ts) & (cl_1h.index <= end_ts)]
        if len(bars) <= bar_offset + 1:
            continue

        entry = float(bars.iloc[bar_offset]["open"])
        if entry <= 0 or np.isnan(entry):
            continue

        sl_d  = SL_MULT  * daily_atr
        tp1_d = TP1_MULT * daily_atr
        tp2_d = TP2_MULT * daily_atr
        sl_p  = entry - direction * sl_d
        tp1_p = entry + direction * tp1_d
        tp2_p = entry + direction * tp2_d

        pnl = None; ex = "TIME"
        for _, bar in bars.iloc[bar_offset + 1:].iterrows():
            hi = float(bar["high"]); lo = float(bar["low"])
            if direction == 1:
                if lo <= sl_p:
                    pnl = -sl_d/entry; ex = "SL"; break
                if hi >= tp1_p:
                    if hi >= tp2_p:
                        pnl = (TP1_FRAC*tp1_d + (1-TP1_FRAC)*tp2_d) / entry
                        ex = "TP2"; break
                    pnl = TP1_FRAC * tp1_d / entry
                    rem = 0.0
                    for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                        if float(b2["low"])  <= sl_p:  rem = 0.0; break
                        if float(b2["high"]) >= tp2_p: rem = (1-TP1_FRAC)*tp2_d/entry; break
                    else:
                        rem = (1-TP1_FRAC) * (float(bars.iloc[-1]["close"]) - entry) / entry
                    pnl += rem; ex = "TP1"; break
            else:
                if hi >= sl_p:
                    pnl = -sl_d/entry; ex = "SL"; break
                if lo <= tp1_p:
                    if lo <= tp2_p:
                        pnl = (TP1_FRAC*tp1_d + (1-TP1_FRAC)*tp2_d) / entry
                        ex = "TP2"; break
                    pnl = TP1_FRAC * tp1_d / entry
                    rem = 0.0
                    for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                        if float(b2["high"]) >= sl_p:  rem = 0.0; break
                        if float(b2["low"])  <= tp2_p: rem = (1-TP1_FRAC)*tp2_d/entry; break
                    else:
                        rem = (1-TP1_FRAC) * (entry - float(bars.iloc[-1]["close"])) / entry
                    pnl += rem; ex = "TP1"; break

        if pnl is None:
            lc  = float(bars.iloc[-1]["close"])
            pnl = direction * (lc - entry) / entry; ex = "TIME"

        trades.append({"date":d, "score":score, "direction":direction,
                        "entry":entry, "sl_dist":sl_d, "atr":daily_atr,
                        "pnl_pct":pnl, "exit":ex, "month":int(row["month"])})
    return trades

def stats(trades, n_days=None):
    nd = n_days or N_DAYS
    if not trades:
        return {"n":0,"tpw":0,"wr":0,"sharpe":-99,"roi3":0,"maxdd":0,"cum":0}
    n  = len(trades); nw = max(nd/5,1); nm = max(nd/21,1)
    pnls  = [t["pnl_pct"] for t in trades]
    wr    = sum(1 for p in pnls if p > 0) / n
    avg   = np.mean(pnls); std = np.std(pnls)
    sharpe= avg/std*np.sqrt(252) if std > 0 else 0
    acc   = [t["pnl_pct"]*RISK_PCT/(t["sl_dist"]/t["entry"]) for t in trades]
    roi3  = sum(acc)/nm*100
    cum   = np.cumprod(1+np.array(acc))-1
    rmx   = np.maximum.accumulate(cum+1)
    maxdd = float(np.min((cum+1)/rmx-1))*100
    return {"n":n,"tpw":n/nw,"wr":wr*100,"sharpe":sharpe,
            "roi3":roi3,"maxdd":maxdd,"cum":(cum[-1])*100}

def row_str(label, s, flag=""):
    return (f"  {label:<44} {s['n']:>4} {s['tpw']:>4.1f} "
            f"{s['wr']:>5.1f} {s['sharpe']:>7.2f} "
            f"{s['roi3']:>6.2f} {s['maxdd']:>7.1f}{flag}")

hdr = f"  {'Config':<44} {'n':>4} {'tpw':>4} {'WR%':>5} {'Sharpe':>7} {'roi3%':>6} {'MaxDD':>7}"
div = "-" * 78

# ══════════════════════════════════════════════════════════════════
# PHASE 1 — Establish v1 baseline
# ══════════════════════════════════════════════════════════════════
print("PHASE 1 — v1 baseline (trend+copper+mom+SPY, sc≥2, skip Jan/Apr/Jun/Sep/Oct, skip Wed)")
print(div)
BAD_MONTHS = [1, 4, 6, 9, 10]
v1_base  = simulate(cols=V1_COLS, score_min=2, skip_months=BAD_MONTHS, skip_wed=True)
sv1      = stats(v1_base)
print(row_str("v1 baseline", sv1, "  ← starting point"))
print()

# ══════════════════════════════════════════════════════════════════
# PHASE 2 — New component A/B test
#   Method: add each new component to the v1 base, see if it helps
# ══════════════════════════════════════════════════════════════════
print("PHASE 2 — New component A/B test (added one at a time to v1 base)")
print("(Kilian/BKZ/GS framework — testing research-backed signals)")
print(div)
print(hdr); print(div)

NEW_COLS = ["c_crack_rb","c_crack_ho","c_brent","c_termstruct","c_oih"]
new_col_keep = []
ab_results = {}

for nc in NEW_COLS:
    cols_with    = V1_COLS + [nc]
    t_with       = simulate(cols=cols_with,   score_min=2, skip_months=BAD_MONTHS, skip_wed=True)
    s_with       = stats(t_with)
    delta        = s_with["sharpe"] - sv1["sharpe"]
    keep         = delta > 0 and s_with["n"] >= 30
    ab_results[nc] = (s_with, keep, delta)
    if keep:
        new_col_keep.append(nc)
    flag = f"  KEEP ✓ (Δ{delta:+.2f})" if keep else f"  drop  (Δ{delta:+.2f})"
    label_map = {
        "c_crack_rb":  "RBOB crack spread [BKZ 2018 #1 signal]",
        "c_crack_ho":  "HO crack spread",
        "c_brent":     "Brent-WTI spread z-score [GS framework]",
        "c_termstruct":"Term structure USO/USL [backwardation proxy]",
        "c_oih":       "OIH/CL divergence [services sustainability]",
    }
    print(row_str(f"+ {label_map.get(nc, nc)}", s_with, flag))
print()
print(f"  Keeping new components: {new_col_keep if new_col_keep else 'none'}")

# ══════════════════════════════════════════════════════════════════
# PHASE 3 — Best component set: re-test skip months + score threshold
# ══════════════════════════════════════════════════════════════════
BEST_COLS = V1_COLS + new_col_keep
MAX_SCORE = len(BEST_COLS)   # useful for absolute score thresholds

print(f"\nPHASE 3 — Optimise score threshold + skip months  [{len(BEST_COLS)}-component model]")
print(div)
print(hdr); print(div)

# Skip month test
month_names = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
               7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}
base_all = simulate(cols=BEST_COLS, score_min=2, skip_months=[], skip_wed=True)
sb_all   = stats(base_all)
month_deltas = {}
for m in range(1,13):
    t = simulate(cols=BEST_COLS, score_min=2, skip_months=[m], skip_wed=True)
    s = stats(t)
    month_deltas[m] = s["sharpe"] - sb_all["sharpe"]

good_skip = [m for m,d in month_deltas.items() if d > 0.08]
print(f"  Skip months that help: {[month_names[m] for m in sorted(good_skip)]}")

# Grid: threshold × skip months
best_cfg = None; best_s_all = {"sharpe":-99}
for thresh in [2, 3, 5]:
    for skips in [[], good_skip, BAD_MONTHS]:
        t = simulate(cols=BEST_COLS, score_min=thresh, skip_months=skips, skip_wed=True)
        s = stats(t)
        if s["n"] < 25: continue
        skip_str = [month_names[m] for m in sorted(skips)]
        label    = f"sc≥{thresh} skip={skip_str[:3]}"
        flag     = ""
        if s["sharpe"] > best_s_all["sharpe"]:
            best_s_all = s; best_cfg = (thresh, skips); flag = "  ◄ BEST"
        print(row_str(label, s, flag))
print()

# ══════════════════════════════════════════════════════════════════
# PHASE 4 — EIA Wednesday & entry timing
# ══════════════════════════════════════════════════════════════════
print("PHASE 4 — EIA Wednesday handling + entry timing")
print("(Research: EIA ±$1.5/bbl in 30s; ORB outperforms VWAP for trending CL)")
print(div)
print(hdr); print(div)

best_thresh, best_skips = best_cfg or (3, BAD_MONTHS)
eia_configs = [
    ("Skip Wed entirely    ",        dict(skip_wed=True,  post_eia_wed=False, enter_bar2=False)),
    ("Trade Wed (normal)   ",        dict(skip_wed=False, post_eia_wed=False, enter_bar2=False)),
    ("Wed: post-EIA bar2   ",        dict(skip_wed=False, post_eia_wed=True,  enter_bar2=False)),
    ("All days bar2 entry  ",        dict(skip_wed=True,  post_eia_wed=False, enter_bar2=True)),
    ("Wed post-EIA+bar2    ",        dict(skip_wed=False, post_eia_wed=True,  enter_bar2=True)),
]
best_eia = None; best_s_eia = {"sharpe":-99}
for label, kwargs in eia_configs:
    t = simulate(cols=BEST_COLS, score_min=best_thresh,
                 skip_months=best_skips, **kwargs)
    s = stats(t)
    if s["n"] < 20: continue
    flag = ""
    if s["sharpe"] > best_s_eia["sharpe"]:
        best_s_eia = s; best_eia = kwargs; flag = "  ◄ BEST"
    print(row_str(label, s, flag))
print()

# ══════════════════════════════════════════════════════════════════
# PHASE 5 — Regime gate: XOP-SPY correlation filter
#   Low correlation = fundamental regime → oil-specific signals work better
# ══════════════════════════════════════════════════════════════════
print("PHASE 5 — XOP-SPY regime gate (fundamental vs macro-driven regime)")
print("(Research: high corr = risk-on/off dominates; low corr = fundamentals)")
print(div)
print(hdr); print(div)

best_all_kwargs = dict(cols=BEST_COLS, score_min=best_thresh,
                       skip_months=best_skips, **(best_eia or {}))
t_no_gate = simulate(**best_all_kwargs)
s_no_gate = stats(t_no_gate)
print(row_str("No regime gate (all regimes)", s_no_gate))
for corr_thr in [0.85, 0.75, 0.65, 0.55]:
    t_g = simulate(**best_all_kwargs, regime_gate=True, regime_corr_max=corr_thr)
    s_g = stats(t_g)
    if s_g["n"] < 20: continue
    delta = s_g["sharpe"] - s_no_gate["sharpe"]
    flag  = f"  Δ{delta:+.2f}" + (" ← KEEP" if delta > 0.05 else "")
    print(row_str(f"Regime gate: XOP-SPY corr < {corr_thr}", s_g, flag))
print()

# ══════════════════════════════════════════════════════════════════
# PHASE 6 — C1 WINNER MODEL
# ══════════════════════════════════════════════════════════════════
print("PHASE 6 — C1 winner: full grid of best combinations")
print(div)
print(hdr); print(div)

configs = []
for thresh in [2, 3, 5]:
    for skips in [good_skip, BAD_MONTHS, good_skip + [m for m in BAD_MONTHS if m not in good_skip]]:
        skips = sorted(set(skips))
        for sw, pew, eb in [(True,False,False),(False,True,False),(False,True,True)]:
            for rg, rc in [(False,0.65),(True,0.65),(True,0.75)]:
                t = simulate(cols=BEST_COLS, score_min=thresh,
                             skip_months=skips, skip_wed=sw,
                             post_eia_wed=pew, enter_bar2=eb,
                             regime_gate=rg, regime_corr_max=rc)
                s = stats(t)
                if s["n"] < 25: continue
                configs.append((thresh, skips, sw, pew, eb, rg, rc, s, t))

configs.sort(key=lambda x: x[7]["sharpe"], reverse=True)
best_full = configs[0]

for cfg in configs[:12]:
    thresh, skips, sw, pew, eb, rg, rc, s, _ = cfg
    skip_s = [month_names[m] for m in skips]
    label  = (f"sc≥{thresh} skip={len(skips)}mo"
              f" {'skipWed' if sw else 'postEIA' if pew else 'allWed'}"
              f"{'_bar2' if eb else ''}"
              f"{f' rg<{rc}' if rg else ''}")
    flag   = "  ◄ BEST" if cfg is configs[0] else ""
    print(row_str(label, s, flag))
print()

# ══════════════════════════════════════════════════════════════════
# FINAL REPORT
# ══════════════════════════════════════════════════════════════════
best_thresh, best_skips, best_sw, best_pew, best_eb, best_rg, best_rc, best_s, best_trades = best_full

print("=" * 70)
print("WINNER — WTI Crude Oil Research-Backed Model")
print("=" * 70)
print(f"\nComponents:    {BEST_COLS}")
print(f"Score min:     ≥{best_thresh}  (out of max ±{len(BEST_COLS)})")
print(f"Skip months:   {[month_names[m] for m in best_skips]}")
print(f"Wednesday:     {'Skip entirely' if best_sw else 'Post-EIA bar2' if best_pew else 'Normal'}")
print(f"Entry:         {'Bar 2 (skip open noise)' if best_eb else 'Bar 1 (session open)'}")
print(f"Regime gate:   {'XOP-SPY corr < '+str(best_rc) if best_rg else 'Off'}")
print()

print(f"  {'Metric':<28} {'Value':>12}")
print("  " + "-"*42)
for lbl, val in [
    ("Trades",         f"{best_s['n']}"),
    ("Trades / week",  f"{best_s['tpw']:.1f}"),
    ("Win rate",       f"{best_s['wr']:.1f}%"),
    ("Sharpe",         f"{best_s['sharpe']:.2f}"),
    ("Monthly ROI @ 3%", f"{best_s['roi3']:.2f}%"),
    ("Monthly ROI @ 5%", f"{best_s['roi3']*(5/3):.2f}%"),
    ("Cumulative",     f"{best_s['cum']:.1f}%"),
    ("Max Drawdown",   f"{best_s['maxdd']:.1f}%"),
]:
    print(f"  {lbl:<28} {val:>12}")

n = len(best_trades)
print(f"\nExit breakdown:")
for ex in ["SL","TP1","TP2","TIME"]:
    cnt = sum(1 for t in best_trades if t["exit"] == ex)
    if cnt:
        avg_e = np.mean([t["pnl_pct"] for t in best_trades if t["exit"] == ex]) * 100
        print(f"  {ex:<5}: {cnt:3d} ({cnt/n*100:.0f}%)  avg={avg_e:+.3f}%")

# Long vs short breakdown
long_t  = [t for t in best_trades if t["direction"] == 1]
short_t = [t for t in best_trades if t["direction"] ==-1]
print(f"\nDirection breakdown:")
for label, tt in [("Longs", long_t), ("Shorts", short_t)]:
    if tt:
        wr_d = sum(1 for t in tt if t["pnl_pct"] > 0) / len(tt) * 100
        sharpe_d = (np.mean([t["pnl_pct"] for t in tt]) /
                    (np.std([t["pnl_pct"] for t in tt]) + 1e-9)) * np.sqrt(252)
        print(f"  {label}: {len(tt)} trades  WR={wr_d:.0f}%  Sharpe={sharpe_d:.2f}")

# Monthly P&L
from collections import defaultdict
monthly = defaultdict(list)
for t in best_trades:
    key = f"{pd.Timestamp(t['date']).year}-{pd.Timestamp(t['date']).month:02d}"
    acc = t["pnl_pct"] * RISK_PCT / (t["sl_dist"] / t["entry"])
    monthly[key].append(acc)

print(f"\nMonthly P&L @ 3% risk:")
for k in sorted(monthly):
    mo_pnl = sum(monthly[k]) * 100
    bar    = "▓" * min(int(abs(mo_pnl) / 0.3), 25)
    print(f"  {k}: {mo_pnl:>+7.2f}%  ({len(monthly[k]):2d} trades)  "
          f"{'  '+bar if mo_pnl>=0 else bar}")

# Risk table
print(f"\nRisk sizing table:")
for r in [1.0, 2.0, 3.0, 5.0, 6.3]:
    roi = best_s["roi3"] * (r / 3.0)
    dd  = best_s["maxdd"] * (r / 3.0)
    print(f"  {r:>5.1f}% risk → {roi:>+6.2f}%/month  MaxDD {dd:>5.1f}%")

# Signal strength by score
print(f"\nScore distribution:")
sd = defaultdict(list)
for t in best_trades: sd[t["score"]].append(t["pnl_pct"])
for sc in sorted(sd):
    wr_sc = sum(1 for p in sd[sc] if p > 0) / len(sd[sc]) * 100
    print(f"  score {sc:>+3d}: {len(sd[sc]):3d} trades  WR={wr_sc:.0f}%  "
          f"avg={np.mean(sd[sc])*100:+.3f}%")

print()
print("─" * 70)
print("LIVE CONFIG (copy to oil_signal.gs):")
print("─" * 70)
print(f"  Components : {BEST_COLS}")
print(f"  SCORE_MIN  : {best_thresh}")
print(f"  SKIP_MONTHS: {best_skips}  // {[month_names[m] for m in best_skips]}")
print(f"  SKIP_WED   : {str(best_sw).upper()}")
print(f"  POST_EIA   : {str(best_pew).upper()}")
print(f"  REGIME_GATE: {str(best_rg).upper()}")
if best_rg: print(f"  CORR_MAX   : {best_rc}")
print(f"  SESSION    : 14:30–19:00 UTC  (10:30–15:00 ET)")
print(f"  SL/TP      : 0.75×/1.5×/2.5× daily ATR14")
print()
print("Compare to gold model:")
print(f"  Gold:  Sharpe 2.63 | WR 48.5% | 2.3/wk | ~4.7%/mo @ 3% risk")
print(f"  Oil:   Sharpe {best_s['sharpe']:.2f} | WR {best_s['wr']:.1f}% | "
      f"{best_s['tpw']:.1f}/wk | ~{best_s['roi3']:.2f}%/mo @ 3% risk")
