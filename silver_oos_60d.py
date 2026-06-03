#!/usr/bin/env python3
"""
silver_oos_60d.py  —  Out-of-sample validation on last 60 days of 15-min silver data

Yahoo Finance caps 15m intraday history at ~60 days.
Since the v2 model was built on 2yr of 1h data, this 60-day window is
100% out-of-sample — the model never saw these bars during development.

Applies v2 model exactly:
  Macro score ≥ 3/7  |  VIX < 30  |  Skip September
  Gold EMA50 aligned  |  Silver EMA20 aligned
  Daily ATR SL/TP:  SL=0.75×  TP1=1.50×  TP2=2.50×  (60/40 split)
  Sessions:  London 08:00–11:30 UTC  |  NY 14:00–16:30 UTC

OOS pass thresholds:
  Sharpe ≥ 1.0  |  WR ≥ 45%  |  ≥ 1.0 trades/week
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

SESSION_DEFS = [
    ('LONDON',  8,  0, 11, 30),
    ('NY',     14,  0, 16, 30),
]

# ── Download data ─────────────────────────────────────────────────────────────
print("=" * 60)
print("Downloading data…")

def dl_daily(sym, period="3y"):
    df = yf.download(sym, period=period, interval="1d",
                     progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                  for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df

silver_d  = dl_daily("SI=F")
gold_d    = dl_daily("GC=F")
copper_d  = dl_daily("HG=F")
dxy_d     = dl_daily("DX-Y.NYB")
tnx_d     = dl_daily("^TNX")
sil_d     = dl_daily("SIL")
vix_d     = dl_daily("^VIX")
print("  Daily (3yr): SI=F GC=F HG=F DX-Y.NYB ^TNX SIL ^VIX  ✓")

raw_15m = yf.download("SI=F", period="60d", interval="15m",
                       progress=False, auto_adjust=True)
raw_15m.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                   for c in raw_15m.columns]
# Normalise to UTC naive
if raw_15m.index.tz is not None:
    raw_15m.index = raw_15m.index.tz_convert("UTC").tz_localize(None)
else:
    raw_15m.index = pd.to_datetime(raw_15m.index)

silver_15m = raw_15m
oos_start  = silver_15m.index[0].date()
oos_end    = silver_15m.index[-1].date()
print(f"  15m (60d):  SI=F  — {oos_start} → {oos_end}  ({len(silver_15m)} bars)  ✓")

# ── Build macro scoring dataframe (daily) ─────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def pct5(s):    return s.pct_change(5)

def align(src_df, ref_df):
    return src_df["close"].reindex(ref_df.index, method="ffill")

idx = silver_d.index
s   = silver_d["close"]
g   = align(gold_d,   silver_d)
c   = align(copper_d, silver_d)
dx  = align(dxy_d,    silver_d)
tnx = align(tnx_d,    silver_d)
sil = align(sil_d,    silver_d)
vix = align(vix_d,    silver_d)

gs_ratio = g / s

df = pd.DataFrame(index=idx)
df["silver"]          = s
df["gold"]            = g
df["vix"]             = vix
df["s_trend"]         = np.where(s > ema(s, 50), 1, -1)
df["s_gs"]            = np.where(pct5(gs_ratio) < 0, 1, -1)
df["s_copper"]        = np.where(pct5(c) > 0, 1, -1)
df["s_dxy"]           = np.where(pct5(dx) < 0, 1, -1)
df["s_yield"]         = np.where(pct5(tnx) < 0, 1, -1)
df["s_sil"]           = np.where(sil / sil.shift(5) > s / s.shift(5), 1, -1)
df["s_mom"]           = np.where(pct5(s) > 0, 1, -1)
df["gold_trend"]      = np.where(g > ema(g, 50), 1, -1)
df["silver_ema20_dir"]= np.where(s > ema(s, 20), 1, -1)

MACRO_COLS = ["s_trend","s_gs","s_copper","s_dxy","s_yield","s_sil","s_mom"]
df["score"] = df[MACRO_COLS].sum(axis=1)
df = df.dropna()

# ── Daily ATR (from silver_d) ─────────────────────────────────────────────────
atr_df = silver_d[["high","low","close"]].copy()
atr_df["tr"] = np.maximum(
    atr_df["high"] - atr_df["low"],
    np.maximum(
        abs(atr_df["high"] - atr_df["close"].shift(1)),
        abs(atr_df["low"]  - atr_df["close"].shift(1))
    )
)
atr_df["atr14"] = atr_df["tr"].ewm(span=14, adjust=False).mean()

# ── OOS simulation ────────────────────────────────────────────────────────────
print("\nSimulating v2 model on 15m OOS bars…")

oos_trades  = []
oos_days    = sorted(set(silver_15m.index.date))
skip_counts = {"score<3": 0, "vix≥30": 0, "sep": 0,
               "gold_gate": 0, "ema20_gate": 0, "no_bars": 0}

for d in oos_days:
    d_ts = pd.Timestamp(d)

    daily_rows = df[df.index <= d_ts]
    if len(daily_rows) < 14:
        continue
    row = daily_rows.iloc[-1]

    score     = int(row["score"])
    direction = 1 if score > 0 else -1
    abs_score = abs(score)

    if abs_score < 3:
        skip_counts["score<3"] += 1
        continue
    if float(row["vix"]) >= 30:
        skip_counts["vix≥30"] += 1
        continue
    if d_ts.month == 9:
        skip_counts["sep"] += 1
        continue
    if int(row["gold_trend"]) != direction:
        skip_counts["gold_gate"] += 1
        continue
    if int(row["silver_ema20_dir"]) != direction:
        skip_counts["ema20_gate"] += 1
        continue

    atr_rows = atr_df[atr_df.index <= d_ts]
    if len(atr_rows) < 14:
        continue
    daily_atr = float(atr_rows["atr14"].iloc[-1])
    if daily_atr <= 0 or np.isnan(daily_atr):
        continue

    sl_d  = SL_MULT  * daily_atr
    tp1_d = TP1_MULT * daily_atr
    tp2_d = TP2_MULT * daily_atr

    for sess_name, sh, sm, eh, em in SESSION_DEFS:
        entry_ts = d_ts + pd.Timedelta(hours=sh, minutes=sm)
        end_ts   = d_ts + pd.Timedelta(hours=eh,  minutes=em)

        bars = silver_15m.loc[
            (silver_15m.index >= entry_ts) &
            (silver_15m.index <= end_ts)
        ]
        if len(bars) < 2:
            skip_counts["no_bars"] += 1
            continue

        entry = float(bars.iloc[0]["open"])
        if entry <= 0 or np.isnan(entry):
            continue

        sl_price  = entry - direction * sl_d
        tp1_price = entry + direction * tp1_d
        tp2_price = entry + direction * tp2_d

        pnl         = None
        exit_reason = "TIME"

        for _, bar in bars.iloc[1:].iterrows():
            hi = float(bar["high"])
            lo = float(bar["low"])

            if direction == 1:
                if lo <= sl_price:
                    pnl = -sl_d / entry
                    exit_reason = "SL"
                    break
                if hi >= tp1_price:
                    if hi >= tp2_price:
                        pnl = (TP1_FRAC * tp1_d + (1 - TP1_FRAC) * tp2_d) / entry
                        exit_reason = "TP2"
                    else:
                        pnl = (TP1_FRAC * tp1_d) / entry
                        rem = 0.0
                        for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                            if float(b2["low"]) <= sl_price:
                                rem = 0.0; break
                            if float(b2["high"]) >= tp2_price:
                                rem = (1 - TP1_FRAC) * tp2_d / entry; break
                        else:
                            lc = float(bars.iloc[-1]["close"])
                            rem = (1 - TP1_FRAC) * (lc - entry) / entry
                        pnl += rem
                        exit_reason = "TP1"
                    break
            else:
                if hi >= sl_price:
                    pnl = -sl_d / entry
                    exit_reason = "SL"
                    break
                if lo <= tp1_price:
                    if lo <= tp2_price:
                        pnl = (TP1_FRAC * tp1_d + (1 - TP1_FRAC) * tp2_d) / entry
                        exit_reason = "TP2"
                    else:
                        pnl = (TP1_FRAC * tp1_d) / entry
                        rem = 0.0
                        for _, b2 in bars.loc[bar.name:].iloc[1:].iterrows():
                            if float(b2["high"]) >= sl_price:
                                rem = 0.0; break
                            if float(b2["low"]) <= tp2_price:
                                rem = (1 - TP1_FRAC) * tp2_d / entry; break
                        else:
                            lc = float(bars.iloc[-1]["close"])
                            rem = (1 - TP1_FRAC) * (entry - lc) / entry
                        pnl += rem
                        exit_reason = "TP1"
                    break

        if pnl is None:
            lc = float(bars.iloc[-1]["close"])
            pnl = direction * (lc - entry) / entry
            exit_reason = "TIME"

        oos_trades.append({
            "date":      d,
            "session":   sess_name,
            "score":     score,
            "direction": direction,
            "entry":     entry,
            "sl_dist":   sl_d,
            "atr":       daily_atr,
            "pnl_pct":   pnl,
            "exit":      exit_reason,
            "vix":       float(row["vix"]),
        })

# ── Results ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print(f"OOS RESULTS  —  {oos_start} → {oos_end}  (15m bars)")
print("=" * 60)

n_days  = len(oos_days)
n_weeks = n_days / 5.0

print(f"\nFilter pass/fail on {n_days} trading days:")
for k, v in skip_counts.items():
    print(f"  skipped ({k}): {v} days")

if not oos_trades:
    print("\nNo trades triggered — check data availability.")
else:
    n   = len(oos_trades)
    tpw = n / n_weeks
    wr  = sum(1 for t in oos_trades if t["pnl_pct"] > 0) / n
    pnls = [t["pnl_pct"] for t in oos_trades]
    avg_p = np.mean(pnls)
    std_p = np.std(pnls)
    sharpe = (avg_p / std_p * np.sqrt(252)) if std_p > 0 else 0.0

    acc_pnls = [t["pnl_pct"] * RISK_PCT / (t["sl_dist"] / t["entry"])
                for t in oos_trades]
    cum_returns = np.cumprod(1 + np.array(acc_pnls)) - 1
    monthly_roi = sum(acc_pnls) / (n_days / 21.0)

    rolling_max = np.maximum.accumulate(cum_returns + 1)
    drawdowns   = (cum_returns + 1) / rolling_max - 1
    max_dd      = float(np.min(drawdowns)) * 100

    print(f"\n{'Metric':<28} {'OOS (60d 15m)':>16}   {'In-sample (2yr 1h)':>18}")
    print("-" * 66)
    print(f"{'Period':<28} {str(oos_start)+' → '+str(oos_end):>16}   {'2yr hourly':>18}")
    print(f"{'Trades':<28} {n:>16d}   {'385':>18}")
    print(f"{'Trades / week':<28} {tpw:>15.1f}   {'3.2':>18}")
    print(f"{'Win rate':<28} {wr*100:>15.1f}%   {'59.6%':>18}")
    print(f"{'Avg trade pnl':<28} {avg_p*100:>15.3f}%   {'—':>18}")
    print(f"{'Sharpe (annualised)':<28} {sharpe:>16.2f}   {'3.16':>18}")
    print(f"{'Monthly ROI @ 3% risk':<28} {monthly_roi*100:>15.2f}%   {'+4.7%':>18}")
    print(f"{'Monthly ROI @ 6.3% risk':<28} {monthly_roi*(0.063/0.03)*100:>15.2f}%   {'+9.9%':>18}")
    print(f"{'Max drawdown @ 3% risk':<28} {max_dd:>15.2f}%   {'—':>18}")

    print(f"\nExit breakdown:")
    for ex in ["SL", "TP1", "TP2", "TIME"]:
        cnt = sum(1 for t in oos_trades if t["exit"] == ex)
        if cnt:
            wrc = sum(1 for t in oos_trades if t["exit"] == ex and t["pnl_pct"] > 0)
            avg_e = np.mean([t["pnl_pct"] for t in oos_trades if t["exit"] == ex]) * 100
            print(f"  {ex:<6}: {cnt:3d} ({cnt/n*100:.0f}%)  avg={avg_e:+.3f}%")

    print(f"\nSession breakdown:")
    for sess in ["LONDON", "NY"]:
        st = [t for t in oos_trades if t["session"] == sess]
        if st:
            swr  = sum(1 for t in st if t["pnl_pct"] > 0) / len(st)
            savg = np.mean([t["pnl_pct"] for t in st]) * 100
            sa   = np.std([t["pnl_pct"] for t in st])
            ss   = (np.mean([t["pnl_pct"] for t in st]) / sa * np.sqrt(252)) if sa > 0 else 0
            print(f"  {sess:<8}: {len(st):3d} trades  WR={swr*100:.0f}%  avg={savg:+.3f}%  Sharpe={ss:.2f}")

    # Verdict
    oos_pass = sharpe >= 1.0 and wr >= 0.45 and tpw >= 1.0
    print()
    print("─" * 60)
    verdict = "✓  OOS PASS" if oos_pass else "✗  OOS FAIL"
    print(f"  {verdict}")
    if not oos_pass:
        if sharpe < 1.0:
            print(f"  → Sharpe {sharpe:.2f} < 1.0  (model edge degraded out-of-sample)")
        if wr < 0.45:
            print(f"  → WR {wr*100:.1f}% < 45%  (too many losses)")
        if tpw < 1.0:
            print(f"  → {tpw:.1f} trades/week < 1.0  (insufficient signal frequency)")
    else:
        print(f"  → Model holds on unseen data. Safe to run live.")
    print("─" * 60)

    print(f"\nFull trade log:")
    print(f"{'Date':<12} {'Sess':<8} {'Sc':>3} {'Dir':>5} {'Entry':>8} "
          f"{'ATR':>6} {'VIX':>5} {'Exit':>5} {'PnL%':>7} {'Acc%@3%':>8}")
    print("-" * 70)
    for t in oos_trades:
        lev  = RISK_PCT / (t["sl_dist"] / t["entry"])
        apnl = t["pnl_pct"] * lev * 100
        sign = "L" if t["direction"] == 1 else "S"
        print(f"{str(t['date']):<12} {t['session']:<8} {t['score']:>3d} "
              f"{sign:>5} {t['entry']:>8.3f} {t['atr']:>6.3f} "
              f"{t['vix']:>5.1f} {t['exit']:>5} "
              f"{t['pnl_pct']*100:>+7.3f}% {apnl:>+8.3f}%")

    print(f"\nCumulative account return @ 3% risk:  "
          f"{cum_returns[-1]*100:+.2f}%  over {n_days} trading days")
