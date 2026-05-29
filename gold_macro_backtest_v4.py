"""
GOLD MACRO SCORE — BACKTEST v4
================================
Adds two new components to v3:

  5. REAL YIELDS — nominal 10Y minus inflation breakeven (^T10YIE)
     Real yields falling = gold bullish (stronger signal than nominal alone)

  6. GDX LEAD — miners relative to spot gold (1d return)
     GDX outperforming gold = forward-looking accumulation = bullish

SCORE COMPONENTS (-6..+6, normalized to -10..+10):
  Gold vs 50 EMA        ±1
  10Y nominal yield     ±1  (falling=+1)
  DXY direction         ±1  (falling=+1)
  Gold 5d momentum      ±1  (positive=+1)
  Real yield direction  ±1  (falling=+1)   ← NEW
  GDX vs gold lead      ±1  (GDX > gold=+1) ← NEW

Run: pip install yfinance pandas numpy scipy
     python gold_macro_backtest_v4.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

print("="*68)
print("  GOLD MACRO SCORE v4 — + REAL YIELDS + GDX LEAD")
print("="*68)

# ── DATA ──────────────────────────────────────────────────────────────────────
print("\nDownloading daily data...")

def dl(ticker, name):
    try:
        d = yf.download(ticker, period="3y", interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name}: {len(d)} days")
        return d['close']
    except Exception as e:
        print(f"  {name} failed: {e}"); return None

gold_d = dl("GC=F",     "Gold")
dxy_d  = dl("DX-Y.NYB", "DXY")
tnx_d  = dl("^TNX",     "10Y nominal yield")
t10yie = dl("^T10YIE",  "10Y breakeven inflation")
gdx_d  = dl("GDX",      "GDX miners")

print("\nDownloading hourly gold for session returns...")
gold_h = yf.download("GC=F", period="2y", interval="1h", progress=False)
gold_h.columns = [c[0].lower() for c in gold_h.columns]
gold_h.index = pd.to_datetime(gold_h.index).tz_localize(None)
print(f"  Gold hourly: {len(gold_h)} bars")

# ── BUILD SCORES ──────────────────────────────────────────────────────────────
df = pd.DataFrame({'gold': gold_d})
if dxy_d  is not None: df['dxy']    = dxy_d.reindex(df.index,  method='ffill')
if tnx_d  is not None: df['tnx']    = tnx_d.reindex(df.index,  method='ffill')
if t10yie is not None: df['t10yie'] = t10yie.reindex(df.index, method='ffill')
if gdx_d  is not None: df['gdx']    = gdx_d.reindex(df.index,  method='ffill')
df.dropna(subset=['gold'], inplace=True)

# Shared components (same as v3)
df['gold_ema50'] = df['gold'].ewm(span=50, adjust=False).mean()
df['s_trend']    = np.where(df['gold'] > df['gold_ema50'], 1, -1)
df['s_yield']    = np.where(df['tnx'].diff() < 0, 1, -1) if 'tnx' in df else 0
df['s_dxy']      = np.where(df['dxy'].pct_change() < 0, 1, -1) if 'dxy' in df else 0
df['gold_mom5']  = df['gold'].pct_change(5) * 100
df['s_mom']      = np.where(df['gold_mom5'] > 0, 1, -1)

# New: real yield = nominal - breakeven; falling real yield = +1
if 'tnx' in df and 't10yie' in df:
    df['real_yield']     = df['tnx'] - df['t10yie']
    df['real_yield_chg'] = df['real_yield'].diff()
    df['s_real_yield']   = np.where(df['real_yield_chg'] < 0, 1, -1)
    print(f"\n  Real yield data: {df['s_real_yield'].notna().sum()} days")
else:
    df['s_real_yield'] = 0
    print("\n  Real yield: unavailable")

# New: GDX lead — GDX 1d return vs gold 1d return
if 'gdx' in df:
    df['gdx_ret']  = df['gdx'].pct_change() * 100
    df['gold_ret'] = df['gold'].pct_change() * 100
    df['s_gdx']    = np.where(df['gdx_ret'] > df['gold_ret'], 1, -1)
    print(f"  GDX data: {df['s_gdx'].notna().sum()} days")
else:
    df['s_gdx'] = 0
    print("  GDX: unavailable")

# Scores
df['score_v3'] = (df['s_trend'] + df['s_yield'] + df['s_dxy'] + df['s_mom']) / 4 * 10
df['score_v4'] = (df['s_trend'] + df['s_yield'] + df['s_dxy'] +
                  df['s_mom']   + df['s_real_yield'] + df['s_gdx']) / 6 * 10

df.dropna(subset=['gold_mom5'], inplace=True)
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
    rows.append({'date': date, 'score_v3': row['score_v3'], 'score_v4': row['score_v4'],
                 's_real_yield': row['s_real_yield'], 's_gdx': row['s_gdx'], 'ret': ret})

sess = pd.DataFrame(rows).set_index('date')
print(f"  Matched session days: {len(sess)}\n")

# ── TEST FUNCTION ─────────────────────────────────────────────────────────────
def show_results(label, score_col):
    scores = sess[score_col]
    corr, p = stats.pearsonr(scores, sess['ret'])
    print(f"{'='*68}")
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

    print(f"\n  Threshold filter:")
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

    print(f"\n  Overall: r={corr:+.4f}  p={p:.4f}  {'✓ SIGNIFICANT' if p<0.05 else '— not significant'}\n")
    return corr, p

corr_v3, p_v3 = show_results("v3 — EMA + yield + DXY + momentum (4 components)", "score_v3")
corr_v4, p_v4 = show_results("v4 — v3 + real yields + GDX lead (6 components)", "score_v4")

# ── NEW COMPONENTS INDIVIDUALLY ───────────────────────────────────────────────
print(f"{'='*68}")
print(f"  NEW COMPONENTS IN ISOLATION")
print(f"{'='*68}\n")

for col, name in [('s_real_yield', 'Real yield direction'),
                  ('s_gdx',        'GDX lead vs gold')]:
    sub_bull = sess[sess[col] ==  1]
    sub_bear = sess[sess[col] == -1]
    wr_bull  = (sub_bull['ret'] > 0).mean() * 100
    wr_bear  = (sub_bear['ret'] < 0).mean() * 100
    corr_c, p_c = stats.pearsonr(sess[col], sess['ret'])
    print(f"  {name}")
    print(f"    Signal +1: {len(sub_bull)} days | 4h WR {wr_bull:.1f}%")
    print(f"    Signal -1: {len(sub_bear)} days | 4h WR {wr_bear:.1f}% (as bear)")
    print(f"    r={corr_c:+.4f}  p={p_c:.4f}  {'✓ SIGNIFICANT' if p_c<0.05 else '— not significant'}\n")

# ── SUMMARY ───────────────────────────────────────────────────────────────────
print(f"{'='*68}")
print(f"  SUMMARY")
print(f"{'='*68}")
print(f"""
  v3 (4 components): r={corr_v3:+.4f}  p={p_v3:.4f}
  v4 (6 components): r={corr_v4:+.4f}  p={p_v4:.4f}
""")
if corr_v4 > corr_v3:
    print(f"  ✓ v4 correlation improved by {corr_v4-corr_v3:+.4f}")
else:
    print(f"  ✗ v4 correlation did not improve ({corr_v4-corr_v3:+.4f})")
print("="*68)
