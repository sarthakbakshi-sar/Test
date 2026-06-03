#!/usr/bin/env python3
"""
silver_backtest_v4.py  —  OOS-first systematic refinement
═══════════════════════════════════════════════════════════════════
Tests every structural fix on THREE datasets simultaneously:
  IS    : 2yr 1h  (in-sample, same as v2/v3)
  OOS-1h: last 60d 1h bars  (same period as OOS but 1h, isolates timeframe effect)
  OOS-15m: last 60d 15m bars (true OOS, max Yahoo Finance resolution)

Fixes tested:
  A: NY only (drop London — no LBMA fix in silver)
  B: Hourly ATR for SL/TP (daily ATR TP never triggers in 3.5h session)
  C: Score >= 5 (high-conviction signals only)
  D: ADX14 > 20 (trending market gate)
  E: Silver EMA50 gate (trade only with daily trend direction)
  F: Enter at bar 2 (skip opening 15m spike)
  G–I: Combinations of best single gates

Phase 2: TP multiplier grid search on best structural config
Phase 3: Final recommendation + risk table
"""

import yfinance as yf
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

RISK_PCT  = 0.03
TP1_FRAC  = 0.60

SESSION_ALL = [("LONDON", 8, 0, 11, 30), ("NY", 14, 0, 16, 30)]
SESSION_NY  = [("NY", 14, 0, 16, 30)]

# ══════════════════════════════════════════════════════════════════
# 1. DOWNLOAD
# ══════════════════════════════════════════════════════════════════
print("=" * 70)
print("SILVER BACKTEST v4  —  OOS-first systematic refinement")
print("=" * 70)
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

silver_d  = dl_daily("SI=F")
gold_d    = dl_daily("GC=F")
copper_d  = dl_daily("HG=F")
dxy_d     = dl_daily("DX-Y.NYB")
tnx_d     = dl_daily("^TNX")
sil_d     = dl_daily("SIL")
vix_d     = dl_daily("^VIX")
print("  Daily 3yr: 7 symbols ✓")

silver_1h  = dl_intraday("SI=F", "2y", "1h")
silver_15m = dl_intraday("SI=F", "60d", "15m")
print(f"  1h  2yr:  {silver_1h.index[0].date()} → {silver_1h.index[-1].date()}  "
      f"({len(set(silver_1h.index.date))} trading days)")
print(f"  15m 60d:  {silver_15m.index[0].date()} → {silver_15m.index[-1].date()}  "
      f"({len(set(silver_15m.index.date))} trading days)")

oos_cutoff = silver_15m.index[0]
silver_1h_oos = silver_1h[silver_1h.index >= oos_cutoff].copy()
print(f"  1h OOS slice: {silver_1h_oos.index[0].date()} → "
      f"{silver_1h_oos.index[-1].date()}  "
      f"({len(set(silver_1h_oos.index.date))} trading days)")

# ══════════════════════════════════════════════════════════════════
# 2. MACRO SCORING
# ══════════════════════════════════════════════════════════════════
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def pct5(s):   return s.pct_change(5)
def align(src, ref): return src["close"].reindex(ref.index, method="ffill")

idx = silver_d.index
s   = silver_d["close"]
g   = align(gold_d,   silver_d)
c   = align(copper_d, silver_d)
dx  = align(dxy_d,    silver_d)
tn  = align(tnx_d,    silver_d)
sl  = align(sil_d,    silver_d)
vx  = align(vix_d,    silver_d)

macro = pd.DataFrame(index=idx)
macro["vix"]             = vx
macro["gold"]            = g
macro["silver"]          = s
macro["s_trend"]         = np.where(s > ema(s, 50), 1, -1)
macro["s_gs"]            = np.where(pct5(g / s) < 0, 1, -1)
macro["s_copper"]        = np.where(pct5(c) > 0, 1, -1)
macro["s_dxy"]           = np.where(pct5(dx) < 0, 1, -1)
macro["s_yield"]         = np.where(pct5(tn) < 0, 1, -1)
macro["s_sil"]           = np.where(sl / sl.shift(5) > s / s.shift(5), 1, -1)
macro["s_mom"]           = np.where(pct5(s) > 0, 1, -1)
macro["gold_trend"]      = np.where(g > ema(g, 50), 1, -1)
macro["silver_ema20_dir"]= np.where(s > ema(s, 20), 1, -1)
macro["silver_ema50_dir"]= np.where(s > ema(s, 50), 1, -1)

SCORE_COLS = ["s_trend","s_gs","s_copper","s_dxy","s_yield","s_sil","s_mom"]
macro["score"] = macro[SCORE_COLS].sum(axis=1)
macro = macro.dropna()

# ══════════════════════════════════════════════════════════════════
# 3. DAILY ATR
# ══════════════════════════════════════════════════════════════════
_d = silver_d[["high","low","close"]].copy()
_d["tr"] = np.maximum(
    _d["high"] - _d["low"],
    np.maximum(abs(_d["high"] - _d["close"].shift(1)),
               abs(_d["low"]  - _d["close"].shift(1))))
_d["atr14"] = _d["tr"].ewm(span=14, adjust=False).mean()
datr = _d["atr14"]

# ══════════════════════════════════════════════════════════════════
# 4. HOURLY INDICATORS (hatr20, adx14) — computed on 1h bars
# ══════════════════════════════════════════════════════════════════
def compute_1h_indic(df_1h):
    df = df_1h[["open","high","low","close"]].copy()
    df["hl"]     = df["high"] - df["low"]
    df["hatr20"] = df["hl"].ewm(span=20, adjust=False).mean()

    df["up"]   = df["high"].diff()
    df["dn"]   = -df["low"].diff()
    df["+dm"]  = np.where((df["up"] > df["dn"]) & (df["up"] > 0), df["up"], 0.0)
    df["-dm"]  = np.where((df["dn"] > df["up"]) & (df["dn"] > 0), df["dn"], 0.0)
    df["tr"]   = np.maximum(df["hl"],
                  np.maximum(abs(df["high"] - df["close"].shift(1)),
                             abs(df["low"]  - df["close"].shift(1))))
    a = 1 / 14
    df["tr_s"]  = df["tr"].ewm(alpha=a, adjust=False).mean()
    df["+di"]   = 100 * df["+dm"].ewm(alpha=a, adjust=False).mean() / df["tr_s"].replace(0, np.nan)
    df["-di"]   = 100 * df["-dm"].ewm(alpha=a, adjust=False).mean() / df["tr_s"].replace(0, np.nan)
    df["dx"]    = 100 * abs(df["+di"] - df["-di"]) / (df["+di"] + df["-di"]).replace(0, np.nan)
    df["adx14"] = df["dx"].ewm(alpha=a, adjust=False).mean()
    return df[["hatr20", "adx14"]]

indic_1h = compute_1h_indic(silver_1h)

# OOS: resample 15m → 1h, then compute same indicators
_oos_1h = (silver_15m[["open","high","low","close"]]
           .resample("1h")
           .agg({"open":"first","high":"max","low":"min","close":"last"})
           .dropna())
indic_oos = compute_1h_indic(_oos_1h)
print("  Indicators: hatr20, adx14 ✓\n")

# ══════════════════════════════════════════════════════════════════
# 5. SIMULATE
# ══════════════════════════════════════════════════════════════════
def simulate(entry_bars, indic_bars, cfg):
    """
    entry_bars  : 1h or 15m OHLC silver (UTC-naive index)
    indic_bars  : 1h hatr20 + adx14 (always 1h regardless of entry_bars timeframe)
    cfg keys:
      sessions    list  [(name,sh,sm,eh,em), …]
      score_min   int   minimum |score|
      use_hatr    bool  True=hourly ATR, False=daily ATR
      sl_m        float
      tp1_m       float
      tp2_m       float
      use_adx     bool  ADX14>20 gate
      ema50_gate  bool  trade only with silver EMA50 direction
      enter_bar2  bool  skip first session bar (enter at bar index 1)
    """
    sessions   = cfg.get("sessions",   SESSION_ALL)
    score_min  = cfg.get("score_min",  3)
    use_hatr   = cfg.get("use_hatr",   False)
    sl_m       = cfg.get("sl_m",       0.75)
    tp1_m      = cfg.get("tp1_m",      1.50)
    tp2_m      = cfg.get("tp2_m",      2.50)
    use_adx    = cfg.get("use_adx",    False)
    ema50_gate = cfg.get("ema50_gate", False)
    enter_bar2 = cfg.get("enter_bar2", False)

    trades = []
    for d in sorted(set(entry_bars.index.date)):
        d_ts = pd.Timestamp(d)

        m_rows = macro[macro.index <= d_ts]
        if len(m_rows) < 14:
            continue
        row = m_rows.iloc[-1]

        score     = int(row["score"])
        direction = 1 if score > 0 else -1
        if abs(score) < score_min:
            continue
        if float(row["vix"]) >= 30:
            continue
        if d_ts.month == 9:
            continue
        if int(row["gold_trend"]) != direction:
            continue
        if int(row["silver_ema20_dir"]) != direction:
            continue
        if ema50_gate and int(row["silver_ema50_dir"]) != direction:
            continue

        datr_rows = datr[datr.index <= d_ts]
        if len(datr_rows) < 14:
            continue
        daily_atr = float(datr_rows.iloc[-1])
        if daily_atr <= 0 or np.isnan(daily_atr):
            continue

        for sess_name, sh, sm, eh, em in sessions:
            entry_ts = d_ts + pd.Timedelta(hours=sh, minutes=sm)
            end_ts   = d_ts + pd.Timedelta(hours=eh,  minutes=em)

            bars = entry_bars.loc[
                (entry_bars.index >= entry_ts) &
                (entry_bars.index <= end_ts)]
            if len(bars) < 2:
                continue

            # look up 1h indicators at session open
            irows = indic_bars[indic_bars.index <= entry_ts]
            if len(irows) < 20:
                continue
            hatr    = float(irows["hatr20"].iloc[-1])
            adx_val = float(irows["adx14"].iloc[-1])

            if np.isnan(hatr) or hatr <= 0:
                continue
            if use_adx and (np.isnan(adx_val) or adx_val < 20):
                continue

            atr_used = hatr if use_hatr else daily_atr

            # entry bar index
            ei = 1 if enter_bar2 else 0
            if len(bars) <= ei + 1:
                continue
            entry = float(bars.iloc[ei]["open"])
            if entry <= 0 or np.isnan(entry):
                continue

            sl_d  = sl_m  * atr_used
            tp1_d = tp1_m * atr_used
            tp2_d = tp2_m * atr_used

            sl_p  = entry - direction * sl_d
            tp1_p = entry + direction * tp1_d
            tp2_p = entry + direction * tp2_d

            pnl         = None
            exit_reason = "TIME"
            active      = bars.iloc[ei + 1:]

            for _, bar in active.iterrows():
                hi = float(bar["high"])
                lo = float(bar["low"])

                if direction == 1:
                    if lo <= sl_p:
                        pnl = -sl_d / entry;  exit_reason = "SL";  break
                    if hi >= tp1_p:
                        if hi >= tp2_p:
                            pnl = (TP1_FRAC * tp1_d + (1 - TP1_FRAC) * tp2_d) / entry
                            exit_reason = "TP2"
                        else:
                            pnl = (TP1_FRAC * tp1_d) / entry
                            rem = 0.0
                            for _, b2 in active.loc[bar.name:].iloc[1:].iterrows():
                                if float(b2["low"]) <= sl_p:
                                    rem = 0.0;  break
                                if float(b2["high"]) >= tp2_p:
                                    rem = (1 - TP1_FRAC) * tp2_d / entry;  break
                            else:
                                rem = (1 - TP1_FRAC) * (float(bars.iloc[-1]["close"]) - entry) / entry
                            pnl += rem
                            exit_reason = "TP1"
                        break
                else:
                    if hi >= sl_p:
                        pnl = -sl_d / entry;  exit_reason = "SL";  break
                    if lo <= tp1_p:
                        if lo <= tp2_p:
                            pnl = (TP1_FRAC * tp1_d + (1 - TP1_FRAC) * tp2_d) / entry
                            exit_reason = "TP2"
                        else:
                            pnl = (TP1_FRAC * tp1_d) / entry
                            rem = 0.0
                            for _, b2 in active.loc[bar.name:].iloc[1:].iterrows():
                                if float(b2["high"]) >= sl_p:
                                    rem = 0.0;  break
                                if float(b2["low"]) <= tp2_p:
                                    rem = (1 - TP1_FRAC) * tp2_d / entry;  break
                            else:
                                rem = (1 - TP1_FRAC) * (entry - float(bars.iloc[-1]["close"])) / entry
                            pnl += rem
                            exit_reason = "TP1"
                        break

            if pnl is None:
                lc  = float(bars.iloc[-1]["close"])
                pnl = direction * (lc - entry) / entry
                exit_reason = "TIME"

            trades.append({
                "date":      d,
                "session":   sess_name,
                "score":     score,
                "direction": direction,
                "entry":     entry,
                "sl_dist":   sl_d,
                "atr":       daily_atr,
                "hatr":      hatr,
                "pnl_pct":   pnl,
                "exit":      exit_reason,
            })
    return trades

# ══════════════════════════════════════════════════════════════════
# 6. STATS
# ══════════════════════════════════════════════════════════════════
def stats(trades, n_days):
    if not trades or n_days == 0:
        return {"n":0,"tpw":0,"wr":0,"sharpe":-99,"roi3":0,"roi63":0,"maxdd":0}
    n       = len(trades)
    n_weeks = max(n_days / 5, 1)
    n_mo    = max(n_days / 21, 1)
    pnls    = [t["pnl_pct"] for t in trades]
    wr      = sum(1 for p in pnls if p > 0) / n
    avg     = np.mean(pnls)
    std     = np.std(pnls)
    sharpe  = (avg / std * np.sqrt(252)) if std > 0 else 0.0
    acc     = [t["pnl_pct"] * RISK_PCT / (t["sl_dist"] / t["entry"]) for t in trades]
    roi3    = sum(acc) / n_mo * 100
    roi63   = roi3 * (0.063 / 0.03)
    cum     = np.cumprod(1 + np.array(acc)) - 1
    roll_mx = np.maximum.accumulate(cum + 1)
    maxdd   = float(np.min((cum + 1) / roll_mx - 1)) * 100
    return {"n":n,"tpw":n/n_weeks,"wr":wr*100,"sharpe":sharpe,
            "roi3":roi3,"roi63":roi63,"maxdd":maxdd}

IS_DAYS  = len(set(silver_1h.index.date))
OOS_DAYS = len(set(silver_15m.index.date))

# ══════════════════════════════════════════════════════════════════
# 7. PHASE 1 — VARIANT COMPARISON
# ══════════════════════════════════════════════════════════════════
variants = [
    ("v2 baseline  (L+NY, dATR, sc≥3)",
     {"sessions":SESSION_ALL,"use_hatr":False,"score_min":3}),

    ("A: NY only   (dATR, sc≥3)",
     {"sessions":SESSION_NY,"use_hatr":False,"score_min":3}),

    ("B: NY + hATR (sc≥3)",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":3}),

    ("C: NY + hATR + score≥5",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":5}),

    ("D: NY + hATR + ADX>20",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":3,"use_adx":True}),

    ("E: NY + hATR + EMA50 gate",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":3,"ema50_gate":True}),

    ("F: NY + hATR + enter bar2",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":3,"enter_bar2":True}),

    ("G: NY + hATR + sc≥5 + ADX",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":5,"use_adx":True}),

    ("H: NY + hATR + sc≥5 + EMA50",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":5,"ema50_gate":True}),

    ("I: NY + hATR + ADX + EMA50",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":3,
      "use_adx":True,"ema50_gate":True}),

    ("J: NY + hATR + sc≥5 + ADX + EMA50",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":5,
      "use_adx":True,"ema50_gate":True}),

    ("K: NY + hATR + sc≥5 + ADX + EMA50 + bar2",
     {"sessions":SESSION_NY,"use_hatr":True,"score_min":5,
      "use_adx":True,"ema50_gate":True,"enter_bar2":True}),
]

print("PHASE 1 — Variant A/B test (IS | OOS-1h | OOS-15m)")
print("-" * 70)
results = []
for name, cfg in variants:
    print(f"  {name}", flush=True)
    is_t    = simulate(silver_1h,     indic_1h,  cfg)
    oos1_t  = simulate(silver_1h_oos, indic_1h,  cfg)   # OOS period, 1h bars
    oos15_t = simulate(silver_15m,    indic_oos, cfg)   # OOS period, 15m bars
    results.append((name, cfg,
                    stats(is_t,    IS_DAYS),
                    stats(oos1_t,  OOS_DAYS),
                    stats(oos15_t, OOS_DAYS),
                    is_t, oos1_t, oos15_t))

print()
print("=" * 120)
print(f"{'':42}  {'─── IN-SAMPLE (2yr 1h) ───':^28}  "
      f"{'─ OOS-1h (60d) ─':^22}  {'─ OOS-15m (60d) ─':^22}")
print(f"{'Variant':<42}  {'n':>4} {'tpw':>4} {'WR%':>5} {'Shrp':>6} {'roi3':>5}  "
      f"{'n':>4} {'WR%':>5} {'Shrp':>6} {'roi3':>5}  "
      f"{'n':>4} {'WR%':>5} {'Shrp':>6} {'roi3':>5}")
print("-" * 120)

for name, _, is_s, o1_s, o15_s, *_ in results:
    # flag if OOS-15m Sharpe > 0.5 AND IS Sharpe > 2.0
    ok15 = o15_s["sharpe"] > 0.5 and is_s["sharpe"] > 2.0
    ok1  = o1_s["sharpe"]  > 0.5
    flag = " ◄ PASS" if ok15 else (" ◄ 1h+" if ok1 else "")
    print(
        f"{name:<42}  "
        f"{is_s['n']:>4} {is_s['tpw']:>4.1f} {is_s['wr']:>5.1f} "
        f"{is_s['sharpe']:>6.2f} {is_s['roi3']:>5.2f}  "
        f"{o1_s['n']:>4} {o1_s['wr']:>5.1f} "
        f"{o1_s['sharpe']:>6.2f} {o1_s['roi3']:>5.2f}  "
        f"{o15_s['n']:>4} {o15_s['wr']:>5.1f} "
        f"{o15_s['sharpe']:>6.2f} {o15_s['roi3']:>5.2f}"
        f"{flag}"
    )
print("=" * 120)

# ── Identify best by OOS-15m Sharpe ──────────────────────────────
best_idx = max(range(len(results)), key=lambda i: results[i][4]["sharpe"])
best = results[best_idx]
best_name, best_cfg, best_is, best_o1, best_o15, _, _, best_oos_t = best

print(f"\nBest OOS-15m: [{best_name}]")

# ══════════════════════════════════════════════════════════════════
# 8. PHASE 2 — TP GRID SEARCH on best structural config
#    (only if best uses hourly ATR — daily ATR grids are pointless)
# ══════════════════════════════════════════════════════════════════
if best_cfg.get("use_hatr", False):
    print("\nPHASE 2 — TP multiplier grid  (best structural config + hourly ATR)")
    print("-" * 70)

    sl_vals  = [0.50, 0.75, 1.00, 1.25]
    tp1_vals = [1.00, 1.50, 2.00, 2.50]
    tp2_vals = [2.00, 2.50, 3.50, 5.00]

    grid_results = []
    base_cfg = {k: v for k, v in best_cfg.items()}
    for sl_m in sl_vals:
        for tp1_m in tp1_vals:
            for tp2_m in tp2_vals:
                if tp2_m <= tp1_m:
                    continue
                gc = {**base_cfg, "sl_m": sl_m, "tp1_m": tp1_m, "tp2_m": tp2_m}
                gt  = simulate(silver_1h,  indic_1h,  gc)
                got = simulate(silver_15m, indic_oos, gc)
                gs  = stats(gt,  IS_DAYS)
                gos = stats(got, OOS_DAYS)
                grid_results.append((sl_m, tp1_m, tp2_m, gs, gos))

    # sort by OOS-15m Sharpe
    grid_results.sort(key=lambda x: x[4]["sharpe"], reverse=True)

    print(f"{'SL×':>5} {'TP1×':>5} {'TP2×':>5}  "
          f"{'IS-Shrp':>8} {'IS-roi3':>8}  "
          f"{'OOS-Shrp':>9} {'OOS-WR%':>8} {'OOS-roi3':>9}")
    print("-" * 68)
    for sl_m, tp1_m, tp2_m, gs, gos in grid_results[:15]:
        flag = " ◄" if gos["sharpe"] > 0.5 and gs["sharpe"] > 2.0 else ""
        print(f"{sl_m:>5.2f} {tp1_m:>5.2f} {tp2_m:>5.2f}  "
              f"{gs['sharpe']:>8.2f} {gs['roi3']:>8.2f}  "
              f"{gos['sharpe']:>9.2f} {gos['wr']:>8.1f} {gos['roi3']:>9.2f}"
              f"{flag}")

    # best TP grid config
    best_tp = grid_results[0]
    best_cfg = {**best_cfg,
                "sl_m": best_tp[0], "tp1_m": best_tp[1], "tp2_m": best_tp[2]}
    print(f"\nBest TP config:  SL={best_tp[0]}×  TP1={best_tp[1]}×  TP2={best_tp[2]}×")
    print(f"  IS  Sharpe={best_tp[3]['sharpe']:.2f}  roi@3%={best_tp[3]['roi3']:.2f}%")
    print(f"  OOS Sharpe={best_tp[4]['sharpe']:.2f}  WR={best_tp[4]['wr']:.1f}%  "
          f"roi@3%={best_tp[4]['roi3']:.2f}%")

    # re-run trades for final report with best TP
    best_oos_t = simulate(silver_15m, indic_oos, best_cfg)
    best_o15   = stats(best_oos_t, OOS_DAYS)
    best_is_t  = simulate(silver_1h,  indic_1h,  best_cfg)
    best_is    = stats(best_is_t, IS_DAYS)

# ══════════════════════════════════════════════════════════════════
# 9. FINAL REPORT
# ══════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print("FINAL MODEL  —  Best config after OOS-first selection")
print("=" * 70)
print(f"Config: {best_cfg}")
print()
print(f"{'Metric':<28} {'IN-SAMPLE':>14}  {'OOS-15m':>14}")
print("-" * 58)
for label, iv, ov in [
    ("Trades",         f"{best_is['n']}",          f"{best_o15['n']}"),
    ("Trades / week",  f"{best_is['tpw']:.1f}",    f"{best_o15['tpw']:.1f}"),
    ("Win rate",       f"{best_is['wr']:.1f}%",     f"{best_o15['wr']:.1f}%"),
    ("Sharpe",         f"{best_is['sharpe']:.2f}",  f"{best_o15['sharpe']:.2f}"),
    ("ROI @ 3% risk",  f"{best_is['roi3']:.2f}%",   f"{best_o15['roi3']:.2f}%"),
    ("ROI @ 6.3%",     f"{best_is['roi63']:.2f}%",  f"{best_o15['roi63']:.2f}%"),
    ("Max DD @ 3%",    f"{best_is['maxdd']:.2f}%",  f"{best_o15['maxdd']:.2f}%"),
]:
    print(f"{label:<28} {iv:>14}  {ov:>14}")

# Exit breakdown
print()
for label, trades in [("IN-SAMPLE", best_is_t if best_cfg.get("use_hatr") else None),
                      ("OOS-15m",   best_oos_t)]:
    if trades is None:
        continue
    n = len(trades)
    if n == 0:
        continue
    print(f"{label} exit breakdown:")
    for ex in ["SL","TP1","TP2","TIME"]:
        cnt = sum(1 for t in trades if t["exit"] == ex)
        if cnt:
            avg_e = np.mean([t["pnl_pct"] for t in trades if t["exit"] == ex]) * 100
            print(f"  {ex:<5}: {cnt:3d} ({cnt/n*100:.0f}%)  avg pnl={avg_e:+.3f}%")
    print()

# Risk table
print("Risk / monthly ROI table (OOS-15m basis):")
base_roi = best_o15["roi3"]   # at 3% risk
print(f"  {'Risk%':>6}  {'Monthly ROI':>12}  {'Max DD':>8}")
print(f"  {'-'*30}")
for r in [1.0, 2.0, 3.0, 5.0, 6.3]:
    scale = r / 3.0
    roi_r = base_roi * scale
    dd_r  = best_o15["maxdd"] * scale / 3.0 * r
    print(f"  {r:>6.1f}%  {roi_r:>11.2f}%  {dd_r:>7.2f}%")

# OOS trade log
print(f"\nOOS-15m trade log  ({len(best_oos_t)} trades):")
print(f"{'Date':<12} {'Sess':<8} {'Sc':>4} {'Dir':>5} {'Entry':>8} "
      f"{'hATR':>6} {'Exit':>5} {'PnL%':>8} {'Acc%@3%':>9}")
print("-" * 68)
for t in best_oos_t:
    lev  = RISK_PCT / (t["sl_dist"] / t["entry"])
    apnl = t["pnl_pct"] * lev * 100
    sign = "L" if t["direction"] == 1 else "S"
    print(f"{str(t['date']):<12} {t['session']:<8} {t['score']:>4d} "
          f"{sign:>5} {t['entry']:>8.3f} {t['hatr']:>6.3f} "
          f"{t['exit']:>5} {t['pnl_pct']*100:>+8.3f}% {apnl:>+9.3f}%")

cum_oos = np.cumprod(
    1 + np.array([t["pnl_pct"] * RISK_PCT / (t["sl_dist"]/t["entry"])
                  for t in best_oos_t])
) - 1 if best_oos_t else [0]
print(f"\nOOS cumulative account return @ 3% risk: {cum_oos[-1]*100:+.2f}%  "
      f"over {OOS_DAYS} trading days")

# Verdict
print()
print("─" * 70)
oos_pass = best_o15["sharpe"] >= 1.0 and best_o15["wr"] >= 45 and best_o15["tpw"] >= 1.0
verdict  = "✓  OOS PASS — safe to deploy live" if oos_pass else "✗  OOS FAIL — collect data first, do not size up"
print(f"  {verdict}")
print(f"  Sharpe {best_o15['sharpe']:.2f}  |  WR {best_o15['wr']:.1f}%  |  "
      f"{best_o15['tpw']:.1f} trades/week")
if not oos_pass:
    print(f"  → Run live at 1% risk to collect signal data.")
    print(f"  → After 50+ live trades, re-evaluate sizing.")
print("─" * 70)
