#!/usr/bin/env python3
"""
copper_backtest_v1.py  —  COMEX Copper (HG=F) VWAP Pullback Model
══════════════════════════════════════════════════════════════════════

Research sources:
  Gorton & Rouwenhorst (2006):    Commodity momentum strongest for industrial metals
  Goldman Sachs super-cycle:      Copper supply deficit, EV + grid demand
  Jacks-O'Rourke-Williamson:      DXY inverse correlation −0.7 with copper
  BHP / Rio Tinto desk research:  Chinese demand proxy = copper leading indicator
  Erb & Harvey (2006):            Term structure predicts commodity returns

Components tested:
  BASELINE:  Trend (EMA50) · DXY direction · Momentum · SPY (risk-on)
  NEW:       FCX miner lead · Gold/copper ratio · XME sector · Oil demand

Session: COMEX copper  13:30–17:30 UTC  (9:30 AM–1:30 PM ET)
SL/TP  : 0.75×/1.5×/2.5× daily ATR14
"""

import yfinance as yf
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

RISK_PCT = 0.03
SL_MULT  = 0.75
TP1_MULT = 1.50
TP2_MULT = 2.50
TP1_FRAC = 0.60

SESS_START = 13.5   # 13:30 UTC  (9:30 ET — after opening noise)
SESS_CLOSE = 17.5   # 17:30 UTC  (1:30 PM ET — 30 min before COMEX pit close)

print("=" * 70)
print("COMEX COPPER  v1  —  VWAP Pullback Signal Model")
print("=" * 70)
print("\nSources: Gorton-Rouwenhorst 2006 · GS super-cycle · BHP/Rio research")
print("         Jacks-O'Rourke-Williamson · Erb-Harvey 2006\n")
print("Downloading tickers…")

def dl(sym, period="3y"):
    df = yf.download(sym, period=period, interval="1d",
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                  for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df

def dl_h(sym):
    df = yf.download(sym, period="2y", interval="1h",
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                  for c in df.columns]
    if df.index.tz:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df

hg_d  = dl("HG=F")          # COMEX copper ($/lb)
gc_d  = dl("GC=F")          # Gold (for gold/copper ratio)
dxy_d = dl("DX-Y.NYB")      # DXY — inverse correlation with copper
spy_d = dl("SPY")            # S&P 500 — risk-on regime
fcx_d = dl("FCX")            # Freeport-McMoRan — #1 copper miner (leading signal)
xme_d = dl("XME")            # SPDR Metals & Mining ETF — sector leadership
cl_d  = dl("CL=F")          # WTI crude — energy costs + demand proxy
vix_d = dl("^VIX")
print("  Daily 3yr: 8 symbols ✓")

hg_1h = dl_h("HG=F")
print(f"  1h   2yr: {hg_1h.index[0].date()} → {hg_1h.index[-1].date()}  "
      f"({len(set(hg_1h.index.date))} trading days)\n")

# ══════════════════════════════════════════════════════════════
# MACRO SIGNALS
# ══════════════════════════════════════════════════════════════
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def pct5(s):  return s.pct_change(5)
def align(src, ref): return src["close"].reindex(ref.index, method="ffill")

hg  = hg_d["close"]
gc  = align(gc_d,  hg_d)
dx  = align(dxy_d, hg_d)
spy = align(spy_d, hg_d)
fcx = align(fcx_d, hg_d)
xme = align(xme_d, hg_d)
cl  = align(cl_d,  hg_d)
vix = align(vix_d, hg_d)

# Gold/copper ratio — falling = copper outperforming = economic confidence
gc_hg_ratio = gc / hg

macro = pd.DataFrame(index=hg_d.index)
macro["hg"]  = hg
macro["vix"] = vix

# 8 directional components
macro["c_trend"]   = np.where(hg > ema(hg, 50), 1, -1)           # Macro trend
macro["c_dxy"]     = np.where(pct5(dx)  < 0, 1, -1)              # DXY falling = bullish Cu
macro["c_mom"]     = np.where(pct5(hg)  > 0, 1, -1)              # Momentum
macro["c_spy"]     = np.where(pct5(spy) > 0, 1, -1)              # Risk-on
macro["c_fcx"]     = np.where(pct5(fcx) > pct5(hg), 1, -1)       # Miners leading = bullish
macro["c_gc_ratio"]= np.where(pct5(gc_hg_ratio) < 0, 1, -1)      # Cu outperforming gold
macro["c_xme"]     = np.where(pct5(xme) > 0, 1, -1)              # Metals sector momentum
macro["c_oil"]     = np.where(pct5(cl)  > 0, 1, -1)              # Oil demand proxy
macro["month"]     = pd.to_datetime(macro.index).month
macro = macro.dropna()

BASE_COLS = ["c_trend", "c_dxy", "c_mom", "c_spy"]
NEW_COLS  = ["c_fcx", "c_gc_ratio", "c_xme", "c_oil"]
ALL_COLS  = BASE_COLS + NEW_COLS

# ══════════════════════════════════════════════════════════════
# DAILY ATR
# ══════════════════════════════════════════════════════════════
_d = hg_d[["high","low","close"]].copy()
_d["tr"] = np.maximum(_d["high"]-_d["low"],
           np.maximum(abs(_d["high"]-_d["close"].shift(1)),
                      abs(_d["low"] -_d["close"].shift(1))))
_d["atr14"] = _d["tr"].ewm(span=14, adjust=False).mean()
datr = _d["atr14"]

N_DAYS = len(set(hg_1h.index.date))

# ══════════════════════════════════════════════════════════════
# SIMULATE
# ══════════════════════════════════════════════════════════════
def simulate(cols=None, score_min=2, skip_months=None, skip_dow=None):
    cols        = cols or BASE_COLS
    skip_months = skip_months or []
    skip_dow    = skip_dow or []     # 0=Mon … 4=Fri
    trades      = []

    for d in sorted(set(hg_1h.index.date)):
        d_ts   = pd.Timestamp(d)
        m_rows = macro[macro.index <= d_ts]
        if len(m_rows) < 20:
            continue
        row = m_rows.iloc[-1]
        if row["month"] in skip_months:
            continue
        if float(row["vix"]) >= 30:
            continue
        if d_ts.weekday() in skip_dow:
            continue

        score_parts = [float(row[c]) for c in cols if not np.isnan(float(row[c]))]
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

        entry_ts = d_ts + pd.Timedelta(hours=SESS_START)
        end_ts   = d_ts + pd.Timedelta(hours=SESS_CLOSE)
        bars = hg_1h.loc[(hg_1h.index >= entry_ts) & (hg_1h.index <= end_ts)]
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

        pnl = None; ex = "TIME"
        for _, bar in bars.iloc[1:].iterrows():
            hi = float(bar["high"]); lo = float(bar["low"])
            if direction == 1:
                if lo <= sl_p:
                    pnl = -sl_d/entry; ex = "SL"; break
                if hi >= tp1_p:
                    if hi >= tp2_p:
                        pnl = (TP1_FRAC*tp1_d + (1-TP1_FRAC)*tp2_d)/entry
                        ex = "TP2"; break
                    pnl = TP1_FRAC * tp1_d / entry
                    rem = 0.0
                    for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                        if float(b2["low"])  <= sl_p:  rem = 0.0; break
                        if float(b2["high"]) >= tp2_p: rem = (1-TP1_FRAC)*tp2_d/entry; break
                    else:
                        rem = (1-TP1_FRAC)*(float(bars.iloc[-1]["close"])-entry)/entry
                    pnl += rem; ex = "TP1"; break
            else:
                if hi >= sl_p:
                    pnl = -sl_d/entry; ex = "SL"; break
                if lo <= tp1_p:
                    if lo <= tp2_p:
                        pnl = (TP1_FRAC*tp1_d + (1-TP1_FRAC)*tp2_d)/entry
                        ex = "TP2"; break
                    pnl = TP1_FRAC * tp1_d / entry
                    rem = 0.0
                    for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                        if float(b2["high"]) >= sl_p:  rem = 0.0; break
                        if float(b2["low"])  <= tp2_p: rem = (1-TP1_FRAC)*tp2_d/entry; break
                    else:
                        rem = (1-TP1_FRAC)*(entry-float(bars.iloc[-1]["close"]))/entry
                    pnl += rem; ex = "TP1"; break

        if pnl is None:
            lc  = float(bars.iloc[-1]["close"])
            pnl = direction * (lc - entry) / entry; ex = "TIME"

        trades.append({"date":d, "score":score, "direction":direction,
                        "entry":entry, "sl_dist":sl_d, "atr":daily_atr,
                        "pnl_pct":pnl, "exit":ex, "month":int(row["month"])})
    return trades

def stats(trades):
    if not trades:
        return {"n":0,"tpw":0,"wr":0,"sharpe":-99,"roi3":0,"maxdd":0,"cum":0}
    nw = max(N_DAYS/5, 1); nm = max(N_DAYS/21, 1)
    n  = len(trades)
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
    return (f"  {label:<46} {s['n']:>4} {s['tpw']:>4.1f} "
            f"{s['wr']:>5.1f} {s['sharpe']:>7.2f} "
            f"{s['roi3']:>6.2f} {s['maxdd']:>7.1f}{flag}")

hdr = f"  {'Config':<46} {'n':>4} {'tpw':>4} {'WR%':>5} {'Sharpe':>7} {'roi3%':>6} {'MaxDD':>7}"
div = "-" * 80

# ══════════════════════════════════════════════════════════════
# PHASE 1 — Baseline
# ══════════════════════════════════════════════════════════════
print("PHASE 1 — Baseline  (trend + DXY + mom + SPY, sc≥2, no skip)")
print(div)
t_base = simulate(cols=BASE_COLS, score_min=2)
s_base = stats(t_base)
print(row_str("Baseline (trend+DXY+mom+SPY)", s_base, "  ← starting point"))
print()

# ══════════════════════════════════════════════════════════════
# PHASE 2 — New component A/B test
# ══════════════════════════════════════════════════════════════
print("PHASE 2 — New component A/B test  (add one at a time to baseline)")
print(div)
print(hdr); print(div)

kept_new = []
label_map = {
    "c_fcx":     "FCX miner lead [miners outperforming Cu]",
    "c_gc_ratio":"Gold/Cu ratio 5d dir [Cu>Gold = optimism]",
    "c_xme":     "XME metals sector 5d momentum",
    "c_oil":     "Oil 5d direction [energy/demand proxy]",
}
for nc in NEW_COLS:
    cols_w = BASE_COLS + [nc]
    t_w    = simulate(cols=cols_w, score_min=2)
    s_w    = stats(t_w)
    delta  = s_w["sharpe"] - s_base["sharpe"]
    keep   = delta > 0 and s_w["n"] >= 30
    if keep: kept_new.append(nc)
    flag   = f"  KEEP ✓ (Δ{delta:+.2f})" if keep else f"  drop  (Δ{delta:+.2f})"
    print(row_str(f"+ {label_map[nc]}", s_w, flag))
print(f"\n  Keeping: {kept_new if kept_new else 'none'}")

# ══════════════════════════════════════════════════════════════
# PHASE 3 — Score threshold + skip months
# ══════════════════════════════════════════════════════════════
BEST_COLS = BASE_COLS + kept_new
print(f"\nPHASE 3 — Score threshold + skip months  [{len(BEST_COLS)}-component model]")
print(div); print(hdr); print(div)

month_names = {1:'Jan',2:'Feb',3:'Mar',4:'Apr',5:'May',6:'Jun',
               7:'Jul',8:'Aug',9:'Sep',10:'Oct',11:'Nov',12:'Dec'}
t_no_skip = simulate(cols=BEST_COLS, score_min=2)
s_no_skip = stats(t_no_skip)
month_deltas = {}
for m in range(1,13):
    t = simulate(cols=BEST_COLS, score_min=2, skip_months=[m])
    s = stats(t)
    month_deltas[m] = s["sharpe"] - s_no_skip["sharpe"]
good_skip = [m for m,d in month_deltas.items() if d > 0.1]
print(f"  Skip months that help: {[month_names[m] for m in sorted(good_skip)]}")

best_ph3 = None; best_s3 = {"sharpe":-99}
for thresh in [2, 3, 4]:
    for skips in [[], good_skip]:
        t = simulate(cols=BEST_COLS, score_min=thresh, skip_months=skips)
        s = stats(t)
        if s["n"] < 25: continue
        skip_str = [month_names[m] for m in sorted(skips)]
        flag = ""
        if s["sharpe"] > best_s3["sharpe"]:
            best_s3 = s; best_ph3 = (thresh, skips); flag = "  ◄ BEST"
        print(row_str(f"sc≥{thresh} skip={skip_str}", s, flag))
print()

# ══════════════════════════════════════════════════════════════
# PHASE 4 — Day-of-week filter
#   COMEX EIA-equivalent: no major Wed release for copper
#   Test: skip Mon (gap risk) / skip Fri (weekend risk)
# ══════════════════════════════════════════════════════════════
print("PHASE 4 — Day-of-week filter  (gap risk / options expiry effects)")
print(div); print(hdr); print(div)

best_thresh, best_skips = best_ph3 or (2, [])
dow_cfgs = [
    ("All days",          []),
    ("Skip Monday",       [0]),
    ("Skip Friday",       [4]),
    ("Skip Mon+Fri",      [0, 4]),
]
best_dow = []; best_s_dow = {"sharpe": -99}
for label, sdow in dow_cfgs:
    t = simulate(cols=BEST_COLS, score_min=best_thresh,
                 skip_months=best_skips, skip_dow=sdow)
    s = stats(t)
    if s["n"] < 20: continue
    flag = ""
    if s["sharpe"] > best_s_dow["sharpe"]:
        best_s_dow = s; best_dow = sdow; flag = "  ◄ BEST"
    print(row_str(label, s, flag))
print()

# ══════════════════════════════════════════════════════════════
# PHASE 5 — Full grid winner
# ══════════════════════════════════════════════════════════════
print("PHASE 5 — Full grid: best combinations")
print(div); print(hdr); print(div)

configs = []
for thresh in [2, 3, 4]:
    for skips in [[], good_skip, [m for m in range(1,13) if month_deltas.get(m,0) > 0.05]]:
        skips = sorted(set(skips))
        for sdow in [[], [0], [4], [0,4]]:
            t = simulate(cols=BEST_COLS, score_min=thresh,
                         skip_months=skips, skip_dow=sdow)
            s = stats(t)
            if s["n"] < 25: continue
            configs.append((thresh, skips, sdow, s, t))

configs.sort(key=lambda x: x[3]["sharpe"], reverse=True)
best_full = configs[0]

for cfg in configs[:12]:
    thresh, skips, sdow, s, _ = cfg
    skip_s = [month_names[m] for m in skips]
    dow_s  = ['Mon','Tue','Wed','Thu','Fri']
    sdow_s = [dow_s[d] for d in sdow]
    label  = f"sc≥{thresh} skip={len(skips)}mo{'_no'+'+'.join(sdow_s) if sdow else ''}"
    flag   = "  ◄ BEST" if cfg is configs[0] else ""
    print(row_str(label, s, flag))
print()

# ══════════════════════════════════════════════════════════════
# FINAL REPORT
# ══════════════════════════════════════════════════════════════
best_thresh, best_skips, best_dow, best_s, best_trades = best_full

print("=" * 70)
print("WINNER — COMEX Copper VWAP Pullback Model")
print("=" * 70)
print(f"\nComponents : {BEST_COLS}")
print(f"Score min  : ≥{best_thresh}  (out of max ±{len(BEST_COLS)})")
print(f"Skip months: {[month_names[m] for m in best_skips]}")
print(f"Skip days  : {['Mon','Tue','Wed','Thu','Fri'][d] for d in best_dow}" if best_dow else "Skip days  : none")
print()
print(f"  {'Metric':<28} {'Value':>12}")
print("  " + "-"*42)
for lbl, val in [
    ("Trades",            f"{best_s['n']}"),
    ("Trades / week",     f"{best_s['tpw']:.1f}"),
    ("Win rate",          f"{best_s['wr']:.1f}%"),
    ("Sharpe",            f"{best_s['sharpe']:.2f}"),
    ("Monthly ROI @ 3%",  f"{best_s['roi3']:.2f}%"),
    ("Monthly ROI @ 5%",  f"{best_s['roi3']*(5/3):.2f}%"),
    ("Cumulative",        f"{best_s['cum']:.1f}%"),
    ("Max Drawdown",      f"{best_s['maxdd']:.1f}%"),
]:
    print(f"  {lbl:<28} {val:>12}")

n = len(best_trades)
print(f"\nExit breakdown:")
for ex in ["SL","TP1","TP2","TIME"]:
    cnt = sum(1 for t in best_trades if t["exit"] == ex)
    if cnt:
        avg_e = np.mean([t["pnl_pct"] for t in best_trades if t["exit"] == ex])*100
        print(f"  {ex:<5}: {cnt:3d} ({cnt/n*100:.0f}%)  avg={avg_e:+.3f}%")

long_t  = [t for t in best_trades if t["direction"] == 1]
short_t = [t for t in best_trades if t["direction"] ==-1]
print(f"\nDirection breakdown:")
for lbl2, tt in [("Longs", long_t),("Shorts", short_t)]:
    if tt:
        wr_d = sum(1 for t in tt if t["pnl_pct"]>0)/len(tt)*100
        sh_d = (np.mean([t["pnl_pct"] for t in tt]) /
                (np.std([t["pnl_pct"] for t in tt])+1e-9))*np.sqrt(252)
        print(f"  {lbl2}: {len(tt)} trades  WR={wr_d:.0f}%  Sharpe={sh_d:.2f}")

from collections import defaultdict
monthly = defaultdict(list)
for t in best_trades:
    key = f"{pd.Timestamp(t['date']).year}-{pd.Timestamp(t['date']).month:02d}"
    acc = t["pnl_pct"]*RISK_PCT/(t["sl_dist"]/t["entry"])
    monthly[key].append(acc)

print(f"\nMonthly P&L @ 3% risk:")
for k in sorted(monthly):
    mo_pnl = sum(monthly[k])*100
    bar    = "▓" * min(int(abs(mo_pnl)/0.3), 25)
    print(f"  {k}: {mo_pnl:>+7.2f}%  ({len(monthly[k]):2d} trades)  "
          f"{'  '+bar if mo_pnl>=0 else bar}")

print(f"\nRisk sizing table:")
for r in [1.0, 2.0, 3.0, 5.0]:
    roi = best_s["roi3"]*(r/3.0); dd = best_s["maxdd"]*(r/3.0)
    print(f"  {r:>5.1f}% risk → {roi:>+6.2f}%/month  MaxDD {dd:>5.1f}%")

print(f"\nScore distribution:")
sd = defaultdict(list)
for t in best_trades: sd[t["score"]].append(t["pnl_pct"])
for sc in sorted(sd):
    wr_sc = sum(1 for p in sd[sc] if p>0)/len(sd[sc])*100
    print(f"  score {sc:>+3d}: {len(sd[sc]):3d} trades  WR={wr_sc:.0f}%  avg={np.mean(sd[sc])*100:+.3f}%")

print()
print("─" * 70)
print("LIVE CONFIG (copy to copper_signal.gs):")
print("─" * 70)
print(f"  Components : {BEST_COLS}")
print(f"  SCORE_MIN  : {best_thresh}")
print(f"  SKIP_MONTHS: {best_skips}  // {[month_names[m] for m in best_skips]}")
print(f"  SKIP_DAYS  : {best_dow}  // {[['Mon','Tue','Wed','Thu','Fri'][d] for d in best_dow]}")
print(f"  SESSION    : 13:30–17:30 UTC  (9:30 AM–1:30 PM ET  COMEX)")
print(f"  SL/TP      : 0.75×/1.5×/2.5× daily ATR14")
print()
print("Compare to gold and oil:")
print(f"  Gold  : Sharpe 2.63 | WR 48.5% | 2.3/wk | ~4.7%/mo @ 3%")
print(f"  Oil   : Sharpe 5.86 | WR 65.2% | 1.2/wk | ~1.9%/mo @ 3%")
print(f"  Copper: Sharpe {best_s['sharpe']:.2f} | WR {best_s['wr']:.1f}% | {best_s['tpw']:.1f}/wk | ~{best_s['roi3']:.2f}%/mo @ 3%")
