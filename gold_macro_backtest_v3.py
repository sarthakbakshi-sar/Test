"""
GOLD MACRO SCORE — CLEAN BACKTEST v3
======================================
Single targeted change from v1: add gold 5-day momentum as a 4th component.
Everything else stays identical to v1 (binary direction scoring, prior-day data).

SCORE COMPONENTS (-4..+4, normalized to -10..+10):
  Gold vs 50 EMA    ±1  (above=+1, below=-1)
  10Y yield 1d chg  ±1  (falling=+1, rising=-1)
  DXY 1d chg        ±1  (falling=+1, rising=-1)
  Gold 5d return    ±1  (positive=+1, negative=-1)   ← NEW

Tests the same 4h session window (13:30–17:30 UTC) as v1.

Run: pip install yfinance pandas numpy scipy
     python gold_macro_backtest_v3.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

print("="*68)
print("  GOLD MACRO SCORE v3 — v1 + MOMENTUM (clean comparison)")
print("="*68)

# ── DATA ──────────────────────────────────────────────────────────────────────
print("\nDownloading data...")

def dl(ticker, name, period="3y"):
    try:
        d = yf.download(ticker, period=period, interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name}: {len(d)} days")
        return d['close']
    except Exception as e:
        print(f"  {name} failed: {e}"); return None

gold_d = dl("GC=F",     "Gold (daily)")
dxy_d  = dl("DX-Y.NYB", "DXY (daily)")
tnx_d  = dl("^TNX",     "10Y yield (daily)")

print("\nDownloading hourly gold for session returns...")
try:
    gold_h = yf.download("GC=F", period="2y", interval="1h", progress=False)
    gold_h.columns = [c[0].lower() for c in gold_h.columns]
    gold_h.index = pd.to_datetime(gold_h.index).tz_localize(None)
    print(f"  Gold hourly: {len(gold_h)} bars")
except Exception as e:
    print(f"  Hourly failed: {e}"); exit()

# ── BUILD SCORES ──────────────────────────────────────────────────────────────
df = pd.DataFrame({'gold': gold_d})
if dxy_d is not None: df['dxy'] = dxy_d.reindex(df.index, method='ffill')
if tnx_d is not None: df['tnx'] = tnx_d.reindex(df.index, method='ffill')
df.dropna(inplace=True)

df['gold_ema50'] = df['gold'].ewm(span=50, adjust=False).mean()
df['s_trend']    = np.where(df['gold'] > df['gold_ema50'], 1, -1)
df['s_yield']    = np.where(df['tnx'].diff() < 0, 1, -1)
df['s_dxy']      = np.where(df['dxy'].pct_change() < 0, 1, -1)
df['gold_mom5']  = df['gold'].pct_change(5) * 100
df['s_mom']      = np.where(df['gold_mom5'] > 0, 1, -1)

# v1 score: 3 components, ±1 each → /3*10
df['score_v1']   = (df['s_trend'] + df['s_yield'] + df['s_dxy']) / 3 * 10

# v3 score: 4 components, ±1 each → /4*10
df['score_v3']   = (df['s_trend'] + df['s_yield'] + df['s_dxy'] + df['s_mom']) / 4 * 10

df.dropna(inplace=True)
print(f"\n  {len(df)} scoring days | {df.index[0].date()} → {df.index[-1].date()}")

# ── 4H SESSION RETURNS ────────────────────────────────────────────────────────
rows = []
for date, row in df.iterrows():
    d = date.date()
    o_ts = pd.Timestamp(d) + pd.Timedelta(hours=13, minutes=30)
    c_ts = pd.Timestamp(d) + pd.Timedelta(hours=17, minutes=30)
    oc = gold_h.loc[(gold_h.index >= o_ts) & (gold_h.index <= o_ts + pd.Timedelta(hours=1))]
    cc = gold_h.loc[(gold_h.index >= c_ts) & (gold_h.index <= c_ts + pd.Timedelta(hours=1))]
    if oc.empty or cc.empty: continue
    ret = (float(cc['close'].iloc[0]) / float(oc['open'].iloc[0]) - 1) * 100
    rows.append({'date': date,
                 'score_v1': row['score_v1'], 'score_v3': row['score_v3'],
                 's_mom': row['s_mom'], 'ret': ret})

sess = pd.DataFrame(rows).set_index('date')
print(f"  Matched session days: {len(sess)}")

# ── RESULTS ───────────────────────────────────────────────────────────────────
def show_results(label, score_col):
    scores = sess[score_col]
    corr, p = stats.pearsonr(scores, sess['ret'])
    print(f"\n{'='*68}")
    print(f"  {label}")
    print(f"{'='*68}")
    print(f"\n  {'Score':>8} {'Days':>6} {'4h avg':>9} {'4h WR':>8}")
    print(f"  {'─'*36}")
    for sc in sorted(scores.unique()):
        sub = sess[scores == sc]
        avg = sub['ret'].mean()
        wr  = (sub['ret'] > 0).mean() * 100
        flag = " ←bull" if sc > 0 else (" ←bear" if sc < 0 else "")
        print(f"  {sc:>+7.1f} {len(sub):>6} {avg:>+8.2f}% {wr:>6.1f}%{flag}")

    # threshold filter
    print(f"\n  Threshold filter (only trade strong signals):")
    print(f"  {'|score|≥':>10} {'Days':>6} {'Bull WR':>9} {'Bear WR':>9} {'p-value':>10}")
    print(f"  {'─'*48}")
    for thresh in sorted(scores.abs().unique()):
        if thresh == 0: continue
        bull = sess[scores >=  thresh]
        bear = sess[scores <= -thresh]
        n = len(bull) + len(bear)
        if n < 20: continue
        bwr = (bull['ret'] > 0).mean()*100 if len(bull) else float('nan')
        ewr = (bear['ret'] < 0).mean()*100 if len(bear) else float('nan')
        combined = bull['ret'].tolist() + (-bear['ret']).tolist()
        _, pt = stats.ttest_1samp(combined, 0)
        sig = " ✓" if pt < 0.05 else ""
        print(f"  {thresh:>+9.1f}  {n:>6} {bwr:>8.1f}% {ewr:>8.1f}% {pt:>10.4f}{sig}")

    print(f"\n  Overall: r={corr:+.4f}  p={p:.4f}  {'✓ SIGNIFICANT' if p<0.05 else '— not significant'}")
    return corr, p

corr_v1, p_v1 = show_results("v1 — EMA + yield + dollar (3 binary)", "score_v1")
corr_v3, p_v3 = show_results("v3 — EMA + yield + dollar + momentum (4 binary)", "score_v3")

# ── MOMENTUM ALONE ────────────────────────────────────────────────────────────
print(f"\n{'='*68}")
print(f"  MOMENTUM COMPONENT IN ISOLATION")
print(f"{'='*68}")
for sc, label in [(1, "Momentum positive (gold up 5d)"), (-1, "Momentum negative (gold down 5d)")]:
    sub = sess[sess['s_mom'] == sc]
    wr  = (sub['ret'] > 0).mean() * 100
    avg = sub['ret'].mean()
    print(f"  {label}: {len(sub)} days | 4h WR {wr:.1f}% | avg {avg:+.2f}%")
corr_m, p_m = stats.pearsonr(sess['s_mom'], sess['ret'])
print(f"  Correlation: {corr_m:+.4f}  p={p_m:.4f}  {'✓ SIGNIFICANT' if p_m<0.05 else '— not significant'}")

# ── HEAD-TO-HEAD SUMMARY ──────────────────────────────────────────────────────
print(f"\n{'='*68}")
print(f"  HEAD-TO-HEAD SUMMARY")
print(f"{'='*68}")
print(f"""
  v1 (3 components): r={corr_v1:+.4f}  p={p_v1:.4f}
  v3 (4 components): r={corr_v3:+.4f}  p={p_v3:.4f}

  Momentum alone:    r={corr_m:+.4f}  p={p_m:.4f}
""")
if corr_v3 > corr_v1 and p_v3 < p_v1:
    print("  ✓ Adding momentum improved BOTH correlation and significance.")
elif corr_v3 > corr_v1:
    print("  ~ Adding momentum improved correlation but not p-value.")
else:
    print("  ✗ Adding momentum did not improve the score.")
print("="*68)
