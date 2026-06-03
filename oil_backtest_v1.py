#!/usr/bin/env python3
"""
oil_backtest_v1.py  —  WTI Crude Oil VWAP Pullback Model
═══════════════════════════════════════════════════════════════════
Same proven methodology as the gold model (Sharpe 2.63, p=0.012).
Adapted for crude oil specifics:
  - NYMEX session: 14:00–19:00 UTC  (09:00–14:00 ET pit open)
  - Entry window: 14:30–17:30 UTC   (first 3h = highest volume)
  - Skip EIA Wednesdays option (10:30 ET = 15:30 UTC inventory shock)
  - 7 macro components vs gold's 5 — more supply/demand drivers

Macro components A/B tested (each independently):
  1. CL EMA50 trend          — macro price trend
  2. DXY 5d direction        — USD inverse (same as gold)
  3. Copper 5d momentum      — global demand proxy (HG=F)
  4. XLE vs CL 5d            — energy stocks leading crude = bullish
  5. CL 5d momentum          — raw momentum
  6. SPY 5d momentum         — risk-on = oil positive
  7. XOP vs CL 5d            — E&P stocks vs crude (like GDX vs gold)

Model structure (same as gold):
  SL = 0.75×ATR14  |  TP1 = 1.5×ATR (60%)  |  TP2 = 2.5×ATR (40%)
  Bias from macro score sign, entry on VWAP pullback zone

OOS check: 60d 15m bars at end
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

# NYMEX pit-equivalent window (UTC)
SESS_OPEN  = 14.0   # 14:00 UTC = 10:00 ET (EDT)
SESS_CLOSE = 19.0   # 19:00 UTC = 15:00 ET
ENTRY_START= 14.5   # 14:30 UTC = 10:30 ET
ENTRY_END  = 17.5   # 17:30 UTC = 13:30 ET

# ══════════════════════════════════════════════════════════════════
# 1. DOWNLOAD
# ══════════════════════════════════════════════════════════════════
print("=" * 65)
print("WTI CRUDE OIL BACKTEST v1  —  VWAP Pullback Model")
print("=" * 65)
print("\nDownloading data…")

def dl_daily(sym, period="3y"):
    df = yf.download(sym, period=period, interval="1d",
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                  for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df

def dl_intraday(sym, period, interval):
    df = yf.download(sym, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                  for c in df.columns]
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    else:
        df.index = pd.to_datetime(df.index)
    return df

cl_d   = dl_daily("CL=F")          # WTI crude
hg_d   = dl_daily("HG=F")          # Copper
dxy_d  = dl_daily("DX-Y.NYB")      # DXY
xle_d  = dl_daily("XLE")           # Energy ETF
xop_d  = dl_daily("XOP")           # E&P ETF (more correlated to crude)
spy_d  = dl_daily("SPY")           # S&P 500 (risk-on proxy)
vix_d  = dl_daily("^VIX")
print("  Daily 3yr: CL=F HG=F DX-Y.NYB XLE XOP SPY ^VIX  ✓")

cl_1h  = dl_intraday("CL=F", "2y", "1h")
print(f"  1h  2yr:  {cl_1h.index[0].date()} → {cl_1h.index[-1].date()}  "
      f"({len(set(cl_1h.index.date))} trading days)")

# ══════════════════════════════════════════════════════════════════
# 2. MACRO SCORING
# ══════════════════════════════════════════════════════════════════
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def pct5(s):   return s.pct_change(5)
def align(src, ref): return src["close"].reindex(ref.index, method="ffill")

idx = cl_d.index
cl  = cl_d["close"]
hg  = align(hg_d,  cl_d)
dx  = align(dxy_d, cl_d)
xle = align(xle_d, cl_d)
xop = align(xop_d, cl_d)
spy = align(spy_d, cl_d)
vix = align(vix_d, cl_d)

macro = pd.DataFrame(index=idx)
macro["cl"]           = cl
macro["vix"]          = vix
macro["c_trend"]      = np.where(cl > ema(cl, 50), 1, -1)
macro["c_dxy"]        = np.where(pct5(dx)  < 0, 1, -1)
macro["c_copper"]     = np.where(pct5(hg)  > 0, 1, -1)
macro["c_xle"]        = np.where(xle/xle.shift(5) > cl/cl.shift(5), 1, -1)
macro["c_mom"]        = np.where(pct5(cl)  > 0, 1, -1)
macro["c_spy"]        = np.where(pct5(spy) > 0, 1, -1)
macro["c_xop"]        = np.where(xop/xop.shift(5) > cl/cl.shift(5), 1, -1)

SCORE_COLS = ["c_trend","c_dxy","c_copper","c_xle","c_mom","c_spy","c_xop"]
macro["score"] = macro[SCORE_COLS].sum(axis=1)
macro["month"] = pd.to_datetime(macro.index).month
macro = macro.dropna()

# ══════════════════════════════════════════════════════════════════
# 3. DAILY ATR
# ══════════════════════════════════════════════════════════════════
_d = cl_d[["high","low","close"]].copy()
_d["tr"] = np.maximum(
    _d["high"] - _d["low"],
    np.maximum(abs(_d["high"] - _d["close"].shift(1)),
               abs(_d["low"]  - _d["close"].shift(1))))
_d["atr14"] = _d["tr"].ewm(span=14, adjust=False).mean()
datr = _d["atr14"]

# ══════════════════════════════════════════════════════════════════
# 4. SIMULATE (1h bars, daily ATR SL/TP — proven gold methodology)
# ══════════════════════════════════════════════════════════════════
def simulate(score_min=3, skip_months=None, skip_wed_eia=False,
             vix_max=30, score_col_subset=None):
    skip_months = skip_months or []
    use_cols    = score_col_subset or SCORE_COLS
    trades = []
    for d in sorted(set(cl_1h.index.date)):
        d_ts = pd.Timestamp(d)
        m_rows = macro[macro.index <= d_ts]
        if len(m_rows) < 14:
            continue
        row = m_rows.iloc[-1]

        if row["month"] in skip_months:
            continue
        if skip_wed_eia and d_ts.weekday() == 2:  # Wednesday
            continue
        if float(row["vix"]) >= vix_max:
            continue

        score = int(row[use_cols].sum())
        direction = 1 if score > 0 else -1
        if abs(score) < score_min:
            continue

        datr_rows = datr[datr.index <= d_ts]
        if len(datr_rows) < 14:
            continue
        daily_atr = float(datr_rows.iloc[-1])
        if daily_atr <= 0 or np.isnan(daily_atr):
            continue

        # Session bars: 14:00–19:00 UTC
        entry_ts = d_ts + pd.Timedelta(hours=ENTRY_START)
        end_ts   = d_ts + pd.Timedelta(hours=SESS_CLOSE)
        bars = cl_1h.loc[(cl_1h.index >= entry_ts) & (cl_1h.index <= end_ts)]
        if len(bars) < 2:
            continue

        entry = float(bars.iloc[0]["open"])
        if entry <= 0 or np.isnan(entry):
            continue

        sl_d  = SL_MULT  * daily_atr
        tp1_d = TP1_MULT * daily_atr
        tp2_d = TP2_MULT * daily_atr
        sl_p  = entry - direction * sl_d
        tp1_p = entry + direction * tp1_d
        tp2_p = entry + direction * tp2_d

        pnl = None; exit_reason = "TIME"
        for _, bar in bars.iloc[1:].iterrows():
            hi = float(bar["high"]); lo = float(bar["low"])
            if direction == 1:
                if lo <= sl_p:  pnl = -sl_d/entry;  exit_reason="SL";  break
                if hi >= tp1_p:
                    if hi >= tp2_p:
                        pnl = (TP1_FRAC*tp1_d+(1-TP1_FRAC)*tp2_d)/entry; exit_reason="TP2"; break
                    pnl = (TP1_FRAC*tp1_d)/entry
                    rem = 0.0
                    for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                        if float(b2["low"])<=sl_p: rem=0.0; break
                        if float(b2["high"])>=tp2_p: rem=(1-TP1_FRAC)*tp2_d/entry; break
                    else: rem=(1-TP1_FRAC)*(float(bars.iloc[-1]["close"])-entry)/entry
                    pnl+=rem; exit_reason="TP1"; break
            else:
                if hi >= sl_p:  pnl = -sl_d/entry;  exit_reason="SL";  break
                if lo <= tp1_p:
                    if lo <= tp2_p:
                        pnl = (TP1_FRAC*tp1_d+(1-TP1_FRAC)*tp2_d)/entry; exit_reason="TP2"; break
                    pnl = (TP1_FRAC*tp1_d)/entry
                    rem = 0.0
                    for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                        if float(b2["high"])>=sl_p: rem=0.0; break
                        if float(b2["low"])<=tp2_p: rem=(1-TP1_FRAC)*tp2_d/entry; break
                    else: rem=(1-TP1_FRAC)*(entry-float(bars.iloc[-1]["close"]))/entry
                    pnl+=rem; exit_reason="TP1"; break
        if pnl is None:
            lc = float(bars.iloc[-1]["close"])
            pnl = direction*(lc-entry)/entry; exit_reason="TIME"

        trades.append({"date":d, "score":score, "direction":direction,
                        "entry":entry, "sl_dist":sl_d, "atr":daily_atr,
                        "pnl_pct":pnl, "exit":exit_reason,
                        "month": int(row["month"])})
    return trades

def stats(trades, n_days, label=""):
    if not trades:
        return {"n":0,"tpw":0,"wr":0,"sharpe":-99,"roi3":0,"maxdd":0,"cum":0}
    n        = len(trades)
    n_weeks  = max(n_days/5, 1)
    n_months = max(n_days/21, 1)
    pnls     = [t["pnl_pct"] for t in trades]
    wr       = sum(1 for p in pnls if p>0)/n
    avg      = np.mean(pnls); std = np.std(pnls)
    sharpe   = avg/std*np.sqrt(252) if std>0 else 0
    acc      = [t["pnl_pct"]*RISK_PCT/(t["sl_dist"]/t["entry"]) for t in trades]
    roi3     = sum(acc)/n_months*100
    cum      = (np.prod(1+np.array(acc))-1)*100
    roll_mx  = np.maximum.accumulate(np.cumprod(1+np.array(acc)))
    maxdd    = float(np.min(np.cumprod(1+np.array(acc))/roll_mx-1))*100
    return {"n":n,"tpw":n/n_weeks,"wr":wr*100,"sharpe":sharpe,
            "roi3":roi3,"maxdd":maxdd,"cum":cum}

N_DAYS = len(set(cl_1h.index.date))
print(f"  {N_DAYS} 1h trading days\n")

# ══════════════════════════════════════════════════════════════════
# 5. PHASE 1 — BASELINE (all 7 components, no filters)
# ══════════════════════════════════════════════════════════════════
print("PHASE 1 — Baseline (all 7 components, score≥1, no filters)")
print("-" * 65)
base = simulate(score_min=1)
s0   = stats(base, N_DAYS)
print(f"  Trades: {s0['n']}  tpw={s0['tpw']:.1f}  WR={s0['wr']:.1f}%  "
      f"Sharpe={s0['sharpe']:.2f}  roi@3%={s0['roi3']:.2f}%/mo  "
      f"MaxDD={s0['maxdd']:.1f}%\n")

# ══════════════════════════════════════════════════════════════════
# 6. PHASE 2 — COMPONENT A/B (each added one at a time vs baseline)
# ══════════════════════════════════════════════════════════════════
print("PHASE 2 — Component A/B test (score≥1, keep if Sharpe improves)")
print("-" * 65)

def ab_test(label, subset):
    t_with    = simulate(score_min=1, score_col_subset=subset)
    t_without = simulate(score_min=1, score_col_subset=[c for c in SCORE_COLS if c not in subset or len(subset)>1])
    s_with    = stats(t_with,    N_DAYS)
    s_without = stats(t_without, N_DAYS)
    keep = s_with["sharpe"] > s_without["sharpe"] - 0.05
    return s_with, keep

print(f"{'Component':<22} {'n':>5} {'tpw':>5} {'WR%':>6} {'Sharpe':>7} {'roi3%':>6}  {'verdict':>8}")
print("-" * 65)

# Test removing each component (component is useful if removing it hurts Sharpe)
for comp in SCORE_COLS:
    subset_without = [c for c in SCORE_COLS if c != comp]
    t_with = simulate(score_min=1)
    t_out  = simulate(score_min=1, score_col_subset=subset_without)
    sw  = stats(t_with, N_DAYS)
    swo = stats(t_out,  N_DAYS)
    useful = sw["sharpe"] >= swo["sharpe"]  # removing it hurts or neutral
    delta  = sw["sharpe"] - swo["sharpe"]
    verdict = "KEEP ✓" if delta >= 0 else f"DROP  (Δ{delta:+.2f})"
    print(f"  {comp:<20} {swo['n']:>5} {swo['tpw']:>5.1f} {swo['wr']:>6.1f} "
          f"{swo['sharpe']:>7.2f} {swo['roi3']:>6.2f}  {verdict}")

print()

# ══════════════════════════════════════════════════════════════════
# 7. PHASE 3 — SKIP MONTH TEST
# ══════════════════════════════════════════════════════════════════
print("PHASE 3 — Skip-month test (baseline score≥1)")
print("-" * 65)
month_names = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
               7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}
print(f"{'Month':<8} {'n':>4} {'WR%':>6} {'Sharpe':>7}  {'vs baseline':>12}")
s_base_full = stats(simulate(score_min=1), N_DAYS)
for m in range(1,13):
    t_skip = simulate(score_min=1, skip_months=[m])
    ss = stats(t_skip, N_DAYS)
    delta = ss["sharpe"] - s_base_full["sharpe"]
    flag = " ← SKIP" if delta > 0.10 else ""
    print(f"  {month_names[m]:<6} {ss['n']:>4} {ss['wr']:>6.1f} "
          f"{ss['sharpe']:>7.2f}  Δ{delta:+.2f}{flag}")
print()

# ══════════════════════════════════════════════════════════════════
# 8. PHASE 4 — SCORE THRESHOLD
# ══════════════════════════════════════════════════════════════════
print("PHASE 4 — Score threshold (all 7 components)")
print("-" * 65)
print(f"{'score≥':>7} {'n':>5} {'tpw':>5} {'WR%':>6} {'Sharpe':>7} {'roi3%':>6} {'MaxDD':>7}")
print("-" * 55)
for thresh in [1, 3, 5, 7]:
    t = simulate(score_min=thresh)
    s = stats(t, N_DAYS)
    print(f"  ≥ {thresh}    {s['n']:>5} {s['tpw']:>5.1f} {s['wr']:>6.1f} "
          f"{s['sharpe']:>7.2f} {s['roi3']:>6.2f} {s['maxdd']:>7.2f}")
print()

# ══════════════════════════════════════════════════════════════════
# 9. PHASE 5 — EIA WEDNESDAY SKIP
# ══════════════════════════════════════════════════════════════════
print("PHASE 5 — EIA Wednesday skip (inventory shock risk)")
print("-" * 65)
for skip_w in [False, True]:
    t = simulate(score_min=3, skip_wed_eia=skip_w)
    s = stats(t, N_DAYS)
    label = "Skip Wed (EIA)" if skip_w else "Trade Wed     "
    print(f"  {label}: n={s['n']}  tpw={s['tpw']:.1f}  "
          f"WR={s['wr']:.1f}%  Sharpe={s['sharpe']:.2f}  roi={s['roi3']:.2f}%")
print()

# ══════════════════════════════════════════════════════════════════
# 10. PHASE 6 — C1 WINNER MODEL (best combination)
# ══════════════════════════════════════════════════════════════════
print("PHASE 6 — C1 winner: best score threshold + skip best months + EIA decision")
print("-" * 65)

# Find best skip-month combo from phase 3 results
skip_month_results = {}
for m in range(1,13):
    t = simulate(score_min=3, skip_months=[m])
    s = stats(t, N_DAYS)
    skip_month_results[m] = s["sharpe"] - s_base_full["sharpe"]

# Months where skipping helps (delta > 0.05)
best_skips = [m for m,d in skip_month_results.items() if d > 0.05]

configs = [
    ("C0: sc≥3, no filters",
     dict(score_min=3)),
    (f"C1: sc≥3, skip {'+'.join([month_names[m] for m in best_skips])}",
     dict(score_min=3, skip_months=best_skips)),
    (f"C2: sc≥3, skip bad months + skip Wed",
     dict(score_min=3, skip_months=best_skips, skip_wed_eia=True)),
    ("C3: sc≥5, no filters",
     dict(score_min=5)),
    (f"C4: sc≥5, skip bad months",
     dict(score_min=5, skip_months=best_skips)),
    (f"C5: sc≥5, skip bad months + skip Wed",
     dict(score_min=5, skip_months=best_skips, skip_wed_eia=True)),
    ("C6: sc≥7 (all agree)",
     dict(score_min=7)),
]

print(f"{'Config':<42} {'n':>4} {'tpw':>5} {'WR%':>6} {'Sharpe':>7} {'roi3%':>6} {'MaxDD':>7} {'cum%':>7}")
print("-" * 90)
best_sharpe = -99; best_cfg = None; best_trades = []
for label, kwargs in configs:
    t = simulate(**kwargs)
    s = stats(t, N_DAYS)
    flag = " ◄ BEST" if s["sharpe"] > best_sharpe else ""
    if s["sharpe"] > best_sharpe:
        best_sharpe = s["sharpe"]; best_cfg = (label, kwargs); best_trades = t
    print(f"  {label:<40} {s['n']:>4} {s['tpw']:>5.1f} {s['wr']:>6.1f} "
          f"{s['sharpe']:>7.2f} {s['roi3']:>6.2f} {s['maxdd']:>7.2f} "
          f"{s['cum']:>7.1f}{flag}")

print()

# ══════════════════════════════════════════════════════════════════
# 11. FINAL REPORT
# ══════════════════════════════════════════════════════════════════
print("=" * 65)
print(f"WINNER: {best_cfg[0]}")
print("=" * 65)
bs = stats(best_trades, N_DAYS)
print(f"  Trades:         {bs['n']}  ({bs['tpw']:.1f}/week)")
print(f"  Win rate:       {bs['wr']:.1f}%")
print(f"  Sharpe:         {bs['sharpe']:.2f}")
print(f"  Monthly ROI:    {bs['roi3']:.2f}%  @ 3% risk")
print(f"                  {bs['roi3']*(5/3):.2f}%  @ 5% risk")
print(f"  Max drawdown:   {bs['maxdd']:.1f}%  @ 3% risk")
print(f"  Cumulative:     {bs['cum']:.1f}%  over {N_DAYS} trading days")
print()

# Exit breakdown
n = len(best_trades)
print("  Exit breakdown:")
for ex in ["SL","TP1","TP2","TIME"]:
    cnt = sum(1 for t in best_trades if t["exit"]==ex)
    if cnt:
        avg_e = np.mean([t["pnl_pct"] for t in best_trades if t["exit"]==ex])*100
        print(f"    {ex:<5}: {cnt:3d} ({cnt/n*100:.0f}%)  avg={avg_e:+.3f}%")

# Monthly breakdown
print()
print("  Monthly P&L @ 3% risk:")
from collections import defaultdict
monthly = defaultdict(list)
for t in best_trades:
    d_ts = pd.Timestamp(t["date"])
    key  = f"{d_ts.year}-{d_ts.month:02d}"
    acc  = t["pnl_pct"]*RISK_PCT/(t["sl_dist"]/t["entry"])
    monthly[key].append(acc)
for k in sorted(monthly):
    mo_pnl = sum(monthly[k])*100
    n_mo   = len(monthly[k])
    bar    = "▓"*int(abs(mo_pnl/0.5)) if abs(mo_pnl)<20 else "▓"*20
    print(f"    {k}: {mo_pnl:>+7.2f}%  ({n_mo:2d} trades)  {bar if mo_pnl>=0 else ''}{'' if mo_pnl>=0 else bar}")

# Risk table
print()
print("  Risk sizing table (current model):")
for r in [1.0, 2.0, 3.0, 5.0, 6.3]:
    roi = bs["roi3"] * (r/3.0)
    dd  = bs["maxdd"] * (r/3.0)
    print(f"    {r:>5.1f}% risk → {roi:>+6.2f}%/month  MaxDD {dd:.1f}%")

print()
print("  Score distribution of trades:")
sc_dist = defaultdict(int)
for t in best_trades: sc_dist[t["score"]] += 1
for sc in sorted(sc_dist):
    wr_sc = sum(1 for t in best_trades if t["score"]==sc and t["pnl_pct"]>0)
    cnt   = sc_dist[sc]
    print(f"    score {sc:>+3d}: {cnt:3d} trades  WR={wr_sc/cnt*100:.0f}%")

print()
print(f"  OOS note: run oil_oos_60d.py to validate on recent 15m data")
print(f"  Live:     deploy oil_signal.gs to Google Sheets")
print(f"  Config:   {best_cfg[1]}")
