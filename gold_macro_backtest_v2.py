"""
GOLD MACRO SCORE — ENHANCED BACKTEST v2
=========================================
Four improvements over v1:

  1. MAGNITUDE scoring on yields/dollar — not just direction
     (a 10bp yield move scores more than a 0.5bp move)

  2. GOLD MOMENTUM as 4th component
     (5-day return direction + magnitude)

  3. EXTREME SCORE FILTER — only act on strong signals (|score| >= threshold)
     (middle scores are noise; filter them out)

  4. PRE-SESSION yield/dollar — use 13:00 UTC value vs prior close
     (not stale prior-day close; captures London session move before yours)

SCORE COMPONENTS (range -7 to +7, normalized to -10..+10):
  Gold vs 50 EMA      ±1  (above=+1, below=-1)
  Yield magnitude     ±2  (>5bp fall=+2, 1-5bp=+1, <1bp=0, 1-5bp rise=-1, >5bp rise=-2)
  DXY magnitude       ±2  (>0.3% fall=+2, 0.1-0.3%=+1, <0.1%=0, 0.1-0.3% rise=-1, >0.3% rise=-2)
  Gold 5d momentum    ±2  (>1.5%=+2, 0-1.5%=+1, -1.5-0%=-1, <-1.5%=-2)

Run: pip install yfinance pandas numpy scipy
     python gold_macro_backtest_v2.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

SESSION_OPEN_H  = 13   # 13:30 UTC = 17:30 Dubai
SESSION_OPEN_M  = 30
SESSION_CLOSE_H = 17
SESSION_CLOSE_M = 30
PRE_SESS_H      = 13   # 13:00 UTC — pre-session snapshot for yield/DXY

YIELD_STRONG    = 5.0   # bp
YIELD_WEAK      = 1.0   # bp
DXY_STRONG      = 0.30  # %
DXY_WEAK        = 0.10  # %
MOM_STRONG      = 1.5   # % 5-day gold return
MOM_WEAK        = 0.0   # %

print("="*68)
print("  GOLD MACRO SCORE v2 — ENHANCED PREDICTIVE BACKTEST")
print("  Magnitude scoring + momentum + pre-session data + threshold filter")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD DATA
# ══════════════════════════════════════════════════════════════════════════════
print("\nDownloading data...")

def dl_daily(ticker, name):
    try:
        d = yf.download(ticker, period="3y", interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name}: {len(d)} days")
        return d['close']
    except Exception as e:
        print(f"  {name} failed: {e}"); return None

def dl_hourly(ticker, name):
    try:
        d = yf.download(ticker, period="2y", interval="1h", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name}: {len(d)} hourly bars")
        return d
    except Exception as e:
        print(f"  {name} hourly failed: {e}"); return None

gold_d  = dl_daily("GC=F",     "Gold (daily)")
dxy_d   = dl_daily("DX-Y.NYB", "DXY (daily)")
tnx_d   = dl_daily("^TNX",     "10Y yield (daily)")
gold_h  = dl_hourly("GC=F",    "Gold (hourly)")
dxy_h   = dl_hourly("DX-Y.NYB","DXY (hourly)")
tnx_h   = dl_hourly("^TNX",    "10Y yield (hourly)")

if gold_d is None or gold_h is None:
    print("ERROR: missing gold data"); exit()

# ══════════════════════════════════════════════════════════════════════════════
#  BUILD DAILY SCORE WITH ALL 4 COMPONENTS
# ══════════════════════════════════════════════════════════════════════════════
print("\nBuilding enhanced score for each day...")

df = pd.DataFrame({'gold': gold_d})
if dxy_d is not None: df['dxy_prev'] = dxy_d.reindex(df.index, method='ffill')
if tnx_d is not None: df['tnx_prev'] = tnx_d.reindex(df.index, method='ffill')
df.dropna(inplace=True)

# Component 1: Gold vs 50 EMA
df['gold_ema50'] = df['gold'].ewm(span=50, adjust=False).mean()
df['s_trend'] = np.where(df['gold'] > df['gold_ema50'], 1, -1)

# Component 4: Gold 5-day momentum (prior 5 days only — no lookahead)
df['gold_mom5'] = df['gold'].pct_change(5) * 100
def mom_score(r):
    if   r >  MOM_STRONG: return  2
    elif r >  MOM_WEAK:   return  1
    elif r > -MOM_STRONG: return -1
    else:                 return -2
df['s_mom'] = df['gold_mom5'].apply(lambda r: mom_score(r) if pd.notna(r) else 0)

# Pre-session yield and DXY: get 13:00 UTC bar on each day, compare to prior daily close
def get_presession_series(hourly_df, daily_close, label):
    if hourly_df is None or daily_close is None:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    rows_chg = {}
    prev_closes = daily_close.shift(1)
    for date in df.index:
        d = date.date()
        snap_ts = pd.Timestamp(d) + pd.Timedelta(hours=PRE_SESS_H)
        cands = hourly_df.loc[(hourly_df.index >= snap_ts) &
                              (hourly_df.index < snap_ts + pd.Timedelta(hours=1))]
        if cands.empty:
            rows_chg[date] = np.nan; continue
        snap_val = float(cands['close'].iloc[0])
        prev_val = prev_closes.get(date, np.nan)
        if pd.isna(prev_val):
            rows_chg[date] = np.nan; continue
        rows_chg[date] = snap_val - prev_val if label == 'yield' else (snap_val/prev_val - 1)*100
    return pd.Series(rows_chg)

print("  Computing pre-session yield changes...")
df['tnx_presess']  = get_presession_series(tnx_h, tnx_d, 'yield')
print("  Computing pre-session DXY changes...")
df['dxy_presess']  = get_presession_series(dxy_h, dxy_d, 'dxy')

# Fall back to prior-day change if pre-session unavailable
df['tnx_chg'] = df['tnx_presess'].combine_first(df['tnx_prev'].diff())
df['dxy_chg'] = df['dxy_presess'].combine_first(df['dxy_prev'].pct_change()*100)

# Component 2: Yield magnitude (falling = bullish gold → positive score)
def yield_score(chg):
    if pd.isna(chg): return 0
    if   chg < -YIELD_STRONG: return  2
    elif chg < -YIELD_WEAK:   return  1
    elif chg <  YIELD_WEAK:   return  0
    elif chg <  YIELD_STRONG: return -1
    else:                     return -2
df['s_yield'] = df['tnx_chg'].apply(yield_score)

# Component 3: DXY magnitude (falling = bullish gold → positive score)
def dxy_score(chg):
    if pd.isna(chg): return 0
    if   chg < -DXY_STRONG: return  2
    elif chg < -DXY_WEAK:   return  1
    elif chg <  DXY_WEAK:   return  0
    elif chg <  DXY_STRONG: return -1
    else:                   return -2
df['s_dxy'] = df['dxy_chg'].apply(dxy_score)

# Combined score: range -7..+7 → normalize to -10..+10
df['score_raw'] = df['s_trend'] + df['s_yield'] + df['s_dxy'] + df['s_mom']
df['score']     = (df['score_raw'] / 7 * 10).round(1)
df.dropna(subset=['gold_mom5'], inplace=True)

print(f"\n  Score distribution:")
for raw in sorted(df['score_raw'].unique()):
    n = (df['score_raw'] == raw).sum()
    bar = '█' * (n // 5)
    print(f"    {raw/7*10:>+6.1f}  {n:>4}d  {bar}")

# ══════════════════════════════════════════════════════════════════════════════
#  4H SESSION WINDOW TEST
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  SESSION TEST: 13:30–17:30 UTC  ({len(df)} scoring days)")
print(f"{'='*68}")

rows = []
for date, row in df.iterrows():
    d = date.date()
    o_ts = pd.Timestamp(d) + pd.Timedelta(hours=SESSION_OPEN_H,  minutes=SESSION_OPEN_M)
    c_ts = pd.Timestamp(d) + pd.Timedelta(hours=SESSION_CLOSE_H, minutes=SESSION_CLOSE_M)
    oc = gold_h.loc[(gold_h.index >= o_ts) & (gold_h.index <= o_ts + pd.Timedelta(hours=1))]
    cc = gold_h.loc[(gold_h.index >= c_ts) & (gold_h.index <= c_ts + pd.Timedelta(hours=1))]
    if oc.empty or cc.empty: continue
    ret = (float(cc['close'].iloc[0]) / float(oc['open'].iloc[0]) - 1) * 100
    rows.append({'date': date, 'score_raw': row['score_raw'],
                 'score': row['score'], 'ret': ret})

sess = pd.DataFrame(rows).set_index('date')
print(f"  Matched session days: {len(sess)}")

# Full breakdown by score bucket
print(f"\n  ALL SCORES:")
print(f"  {'Score':>8} {'Days':>6} {'Sess avg':>10} {'Sess WR':>9}")
print(f"  {'─'*38}")
for raw in sorted(sess['score_raw'].unique()):
    sub = sess[sess['score_raw'] == raw]
    score10 = raw/7*10
    avg = sub['ret'].mean()
    wr  = (sub['ret'] > 0).mean()*100
    flag = " ←bull" if raw>0 else (" ←bear" if raw<0 else "")
    print(f"  {score10:>+7.1f} {len(sub):>6} {avg:>+9.2f}% {wr:>7.1f}%{flag}")

corr, p = stats.pearsonr(sess['score'], sess['ret'])
print(f"\n  Overall correlation: {corr:+.4f} (p={p:.4f})  {'✓ SIGNIFICANT' if p<0.05 else '— not significant'}")

# ══════════════════════════════════════════════════════════════════════════════
#  THRESHOLD FILTER TEST
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  THRESHOLD FILTER: only trade when |score| >= threshold")
print(f"{'='*68}")
print(f"\n  {'Threshold':>12} {'Days':>6} {'Bull WR':>9} {'Bear WR':>9} {'p-value':>10} {'Result':>16}")
print(f"  {'─'*66}")

for thresh in [3, 5, 7, 8, 9, 10]:
    bull = sess[sess['score'] >=  thresh]
    bear = sess[sess['score'] <= -thresh]
    n = len(bull) + len(bear)
    if n < 10: continue
    bull_wr = (bull['ret'] > 0).mean()*100 if len(bull) else float('nan')
    bear_wr = (bear['ret'] < 0).mean()*100 if len(bear) else float('nan')
    combined = bull['ret'].tolist() + (-bear['ret']).tolist()
    t, pt = stats.ttest_1samp(combined, 0)
    sig = "✓ SIGNIFICANT" if pt < 0.05 else "— not sig"
    print(f"  {thresh:>+10.0f}+ {n:>6} {bull_wr:>8.1f}% {bear_wr:>8.1f}% {pt:>10.4f} {sig:>16}")

# ══════════════════════════════════════════════════════════════════════════════
#  COMPONENT CONTRIBUTION
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  COMPONENT CONTRIBUTION (correlation with 4h session return)")
print(f"{'='*68}")
print(f"\n  {'Component':>20} {'Corr':>10} {'p-value':>10} {'Sig?':>10}")
print(f"  {'─'*54}")
for col, name in [('s_trend','Gold vs 50 EMA'),('s_yield','Yield magnitude'),
                  ('s_dxy','DXY magnitude'),('s_mom','Gold momentum 5d')]:
    merged = df[[col]].join(sess[['ret']], how='inner').dropna()
    if len(merged) < 10: continue
    corr_c, p_c = stats.pearsonr(merged[col], merged['ret'])
    print(f"  {name:>20} {corr_c:>+9.4f} {p_c:>10.4f} {'✓ YES' if p_c<0.05 else '— no':>10}")

# ══════════════════════════════════════════════════════════════════════════════
#  v1 vs v2 COMPARISON
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  v1 vs v2 SUMMARY")
print(f"{'='*68}")

# Reconstruct v1 score on same days
v1 = df.copy()
v1['s_yield_v1'] = np.where(df['tnx_prev'].diff() < 0, 1, -1)
v1['s_dxy_v1']   = np.where(df['dxy_prev'].pct_change() < 0, 1, -1)
v1['score_v1']   = (v1['s_trend'] + v1['s_yield_v1'] + v1['s_dxy_v1']) / 3 * 10
v1_sess = v1[['score_v1']].join(sess[['ret']], how='inner').dropna()
corr_v1, p_v1 = stats.pearsonr(v1_sess['score_v1'], v1_sess['ret'])
corr_v2, p_v2 = stats.pearsonr(sess['score'], sess['ret'])

bull_v1 = v1_sess[v1_sess['score_v1'] >= 10/3*3]
bear_v1 = v1_sess[v1_sess['score_v1'] <= -10/3*3]
bull_v2 = sess[sess['score'] >= 10/7*7]
bear_v2 = sess[sess['score'] <= -10/7*7]

print(f"""
  v1 (3 binary components):
    Correlation: {corr_v1:+.4f}  p={p_v1:.4f}
    Extreme bull WR: {(bull_v1['ret']>0).mean()*100:.1f}%  ({len(bull_v1)} days)
    Extreme bear WR: {(bear_v1['ret']<0).mean()*100:.1f}%  ({len(bear_v1)} days)

  v2 (4 magnitude components + pre-session data):
    Correlation: {corr_v2:+.4f}  p={p_v2:.4f}
    Extreme bull WR: {(bull_v2['ret']>0).mean()*100:.1f}%  ({len(bull_v2)} days)
    Extreme bear WR: {(bear_v2['ret']<0).mean()*100:.1f}%  ({len(bear_v2)} days)
""")
print("="*68)
