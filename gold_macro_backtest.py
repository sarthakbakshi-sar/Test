"""
GOLD MACRO SCORE — BACKTEST
=============================
Tests whether the macro score from the daily brief actually predicts
gold direction. This is the BACKTESTABLE portion (60% weight) of the
master brief.

THE MACRO SCORE (reconstructed for every historical day):
  +1 if gold above 50-day EMA, -1 if below
  +1 if 10Y yield falling, -1 if rising
  +1 if dollar (DXY) falling, -1 if rising
  → range -3 to +3, normalized to -10 to +10

THE TEST:
  For each day, compute the macro score using ONLY data available
  that day (no lookahead). Then measure forward gold returns at
  1, 3, 5, 10 days. Does a higher score predict higher returns?

HONEST CAVEAT:
  News sentiment (40% of the live brief) CANNOT be backtested —
  no free historical news archive exists. This tests macro only.
  If macro predicts direction, the brief has real value.
  If not, it's noise and should be dropped.

Run: pip install yfinance pandas numpy scipy
     python gold_macro_backtest.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

print("="*68)
print("  GOLD MACRO SCORE — PREDICTIVE BACKTEST")
print("  Does the brief's macro score predict gold direction?")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════════════════════════
print("\nDownloading 3 years of data...")

def dl(ticker, name):
    try:
        d = yf.download(ticker, period="3y", interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name}: {len(d)} days")
        return d['close']
    except Exception as e:
        print(f"  {name} failed: {e}")
        return None

gold  = dl("GC=F", "Gold")
dxy   = dl("DX-Y.NYB", "DXY")
tnx   = dl("^TNX", "10Y yield")

if gold is None:
    print("ERROR: no gold data"); exit()

# Align all series
df = pd.DataFrame({'gold': gold})
if dxy is not None: df['dxy'] = dxy.reindex(df.index, method='ffill')
if tnx is not None: df['tnx'] = tnx.reindex(df.index, method='ffill')
df.dropna(inplace=True)
print(f"\nAligned: {len(df)} days | {df.index[0].date()} → {df.index[-1].date()}")

# ══════════════════════════════════════════════════════════════════════════════
#  RECONSTRUCT MACRO SCORE (no lookahead)
# ══════════════════════════════════════════════════════════════════════════════
print("\nReconstructing macro score for each day...")

# Gold vs 50 EMA
df['gold_ema50'] = df['gold'].ewm(span=50, adjust=False).mean()
df['s_trend'] = np.where(df['gold'] > df['gold_ema50'], 1, -1)

# Yield direction (1-day change) — FALLING yields = bullish gold
df['tnx_chg'] = df['tnx'].diff()
df['s_yield'] = np.where(df['tnx_chg'] < 0, 1, -1)   # falling = +1

# Dollar direction (1-day change) — FALLING dollar = bullish gold
df['dxy_chg'] = df['dxy'].diff()
df['s_dollar'] = np.where(df['dxy_chg'] < 0, 1, -1)  # falling = +1

# Combined macro score (-3..+3 → -10..+10)
df['macro_raw']  = df['s_trend'] + df['s_yield'] + df['s_dollar']
df['macro_score']= df['macro_raw'] / 3 * 10

# ══════════════════════════════════════════════════════════════════════════════
#  FORWARD RETURNS
# ══════════════════════════════════════════════════════════════════════════════
for h in [1, 3, 5, 10]:
    df[f'fwd_{h}'] = (df['gold'].shift(-h) / df['gold'] - 1) * 100

df.dropna(inplace=True)
print(f"  {len(df)} days with complete score + forward returns")

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 1 — Forward returns by score bucket
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  TEST 1: Forward gold returns by macro score")
print(f"{'='*68}")

print(f"\n  {'Score':>8} {'Days':>6} {'1d avg':>9} {'1d WR':>8} {'3d avg':>9} {'5d avg':>9} {'5d WR':>8}")
print(f"  {'─'*62}")
for raw in [-3, -1, 1, 3]:
    sub = df[df['macro_raw'] == raw]
    if len(sub) == 0: continue
    score10 = raw/3*10
    r1  = sub['fwd_1'].mean()
    wr1 = (sub['fwd_1'] > 0).mean()*100
    r3  = sub['fwd_3'].mean()
    r5  = sub['fwd_5'].mean()
    wr5 = (sub['fwd_5'] > 0).mean()*100
    flag = " ←bullish" if raw>0 else " ←bearish" if raw<0 else ""
    print(f"  {score10:>+7.1f} {len(sub):>6} {r1:>+8.2f}% {wr1:>6.1f}% {r3:>+8.2f}% {r5:>+8.2f}% {wr5:>6.1f}%{flag}")

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 2 — Correlation between score and forward return
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  TEST 2: Correlation (score → forward return)")
print(f"{'='*68}")
print(f"\n  {'Horizon':>10} {'Correlation':>14} {'p-value':>10} {'Significant?':>14}")
print(f"  {'─'*50}")
for h in [1, 3, 5, 10]:
    corr, p = stats.pearsonr(df['macro_score'], df[f'fwd_{h}'])
    sig = "✓ YES" if p < 0.05 else "— no"
    print(f"  {h:>8}d  {corr:>+13.4f} {p:>10.4f} {sig:>14}")

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 3 — Score as directional filter (trade WITH the score)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  TEST 3: Trade WITH the score — 5-day hold")
print(f"  (Long when score > 0, short when score < 0)")
print(f"{'='*68}")

# Strong-signal version: only act on extreme scores (+/-3)
extreme = df[df['macro_raw'].abs() == 3].copy()
extreme['signal'] = np.where(extreme['macro_raw'] > 0, 1, -1)
extreme['strat_ret'] = extreme['signal'] * extreme['fwd_5']

n = len(extreme)
wr = (extreme['strat_ret'] > 0).mean()*100
avg = extreme['strat_ret'].mean()
t, p = stats.ttest_1samp(extreme['strat_ret'], 0) if n > 5 else (0, 1)

print(f"\n  Extreme score days (±3 only):")
print(f"    Trades        : {n}")
print(f"    Win rate      : {wr:.1f}%")
print(f"    Avg 5d return : {avg:+.2f}% (in signal direction)")
print(f"    t-stat        : {t:.3f}")
print(f"    p-value       : {p:.4f}  {'✓ SIGNIFICANT' if p<0.05 else '— not significant'}")

# All-days version
df['signal'] = np.where(df['macro_raw'] > 0, 1, np.where(df['macro_raw'] < 0, -1, 0))
df['strat_ret'] = df['signal'] * df['fwd_5']
active = df[df['signal'] != 0]
wr_all = (active['strat_ret'] > 0).mean()*100
avg_all = active['strat_ret'].mean()
t_all, p_all = stats.ttest_1samp(active['strat_ret'], 0)

print(f"\n  All signal days:")
print(f"    Trades        : {len(active)}")
print(f"    Win rate      : {wr_all:.1f}%")
print(f"    Avg 5d return : {avg_all:+.2f}%")
print(f"    p-value       : {p_all:.4f}  {'✓ SIGNIFICANT' if p_all<0.05 else '— not significant'}")

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 4 — Each component individually
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  TEST 4: Which component carries the signal?")
print(f"{'='*68}")
print(f"\n  {'Component':>14} {'5d corr':>10} {'p-value':>10} {'Significant?':>14}")
print(f"  {'─'*52}")
for comp, name in [('s_trend','Gold vs 50EMA'),
                   ('s_yield','Yield direction'),
                   ('s_dollar','Dollar direction')]:
    corr, p = stats.pearsonr(df[comp], df['fwd_5'])
    sig = "✓ YES" if p < 0.05 else "— no"
    print(f"  {name:>14} {corr:>+9.4f} {p:>10.4f} {sig:>14}")

# ══════════════════════════════════════════════════════════════════════════════
#  VERDICT
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  VERDICT")
print(f"{'='*68}")

corr5, p5 = stats.pearsonr(df['macro_score'], df['fwd_5'])
print(f"""
  Macro score → 5-day gold return correlation: {corr5:+.4f} (p={p5:.4f})

  INTERPRETATION:
""")
if p5 < 0.05 and abs(corr5) > 0.1:
    print(f"  ✓ The macro score has STATISTICALLY SIGNIFICANT predictive value.")
    print(f"    The daily brief's macro layer is worth using as a filter.")
elif p5 < 0.05:
    print(f"  ~ Statistically significant but WEAK correlation.")
    print(f"    The macro score has marginal value — use as a light filter only.")
else:
    print(f"  ✗ The macro score does NOT significantly predict gold direction.")
    print(f"    As a standalone predictor it's noise. BUT it may still work as")
    print(f"    a CONFIRMATION filter on your 1H system (different from prediction).")

print(f"""
  REMEMBER: This only tests the MACRO layer (60% of the brief).
  The NEWS layer (40%) cannot be backtested without historical news data.
  And this tests PREDICTION, not its value as a confirmation filter on
  your proven 1H system — which is a separate question.
""")
print("="*68)

df[['gold','macro_score','macro_raw','fwd_1','fwd_5']].to_csv("gold_macro_backtest.csv")
print("Saved: gold_macro_backtest.csv")

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 5 — 4-HOUR SESSION WINDOW (13:30–17:30 UTC = your Dubai session)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  TEST 5: 4-hour session window (13:30–17:30 UTC)")
print(f"  Does the morning macro score predict YOUR session?")
print(f"{'='*68}")

print("\nDownloading 2 years of hourly gold data...")
try:
    gh = yf.download("GC=F", period="2y", interval="1h", progress=False)
    gh.columns = [c[0].lower() for c in gh.columns]
    gh.index = pd.to_datetime(gh.index).tz_localize(None)
    print(f"  Hourly bars: {len(gh)}")

    SESS_OPEN_H, SESS_OPEN_M   = 13, 30
    SESS_CLOSE_H, SESS_CLOSE_M = 17, 30

    rows = []
    for date, score_row in df.iterrows():
        d = date.date()
        open_ts  = pd.Timestamp(d) + pd.Timedelta(hours=SESS_OPEN_H,  minutes=SESS_OPEN_M)
        close_ts = pd.Timestamp(d) + pd.Timedelta(hours=SESS_CLOSE_H, minutes=SESS_CLOSE_M)
        # find closest bar at or after session open
        candidates = gh.loc[(gh.index >= open_ts) & (gh.index <= open_ts + pd.Timedelta(hours=1))]
        end_cands  = gh.loc[(gh.index >= close_ts) & (gh.index <= close_ts + pd.Timedelta(hours=1))]
        if candidates.empty or end_cands.empty:
            continue
        p_open  = float(candidates['open'].iloc[0])
        p_close = float(end_cands['close'].iloc[0])
        sess_ret = (p_close / p_open - 1) * 100
        rows.append({'date': date, 'macro_raw': score_row['macro_raw'],
                     'macro_score': score_row['macro_score'], 'sess_ret': sess_ret})

    sess = pd.DataFrame(rows).set_index('date')
    print(f"  Matched session days: {len(sess)}")

    print(f"\n  {'Score':>8} {'Days':>6} {'Sess avg':>10} {'Sess WR':>9}")
    print(f"  {'─'*38}")
    for raw in [-3, -1, 1, 3]:
        sub = sess[sess['macro_raw'] == raw]
        if len(sub) == 0: continue
        score10 = raw/3*10
        avg = sub['sess_ret'].mean()
        wr  = (sub['sess_ret'] > 0).mean()*100
        flag = " ←bullish" if raw>0 else " ←bearish" if raw<0 else ""
        print(f"  {score10:>+7.1f} {len(sub):>6} {avg:>+9.2f}% {wr:>7.1f}%{flag}")

    corr, p = stats.pearsonr(sess['macro_score'], sess['sess_ret'])
    t, pt   = stats.ttest_1samp(
        sess.loc[sess['macro_raw']>0,'sess_ret'].values.tolist() +
        (-sess.loc[sess['macro_raw']<0,'sess_ret']).values.tolist(), 0)
    print(f"\n  Correlation (score → session return): {corr:+.4f} (p={p:.4f})")
    print(f"  Trade-with-score p-value: {pt:.4f}  {'✓ SIGNIFICANT' if pt<0.05 else '— not significant'}")

    if p < 0.05:
        print(f"\n  ✓ Macro score predicts your 4h session direction.")
    else:
        print(f"\n  ✗ Macro score does not predict your 4h session.")

except Exception as e:
    print(f"  Hourly data failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  TEST 6 — 1-HOUR WINDOW (13:30–14:30 UTC = first hour of session)
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*68}")
print(f"  TEST 6: 1-hour window (13:30–14:30 UTC)")
print(f"  Does the macro score predict the first hour of your session?")
print(f"{'='*68}")

try:
    rows1h = []
    for date, score_row in df.iterrows():
        d = date.date()
        open_ts  = pd.Timestamp(d) + pd.Timedelta(hours=13, minutes=30)
        close_ts = pd.Timestamp(d) + pd.Timedelta(hours=14, minutes=30)
        candidates = gh.loc[(gh.index >= open_ts) & (gh.index <= open_ts + pd.Timedelta(hours=1))]
        end_cands  = gh.loc[(gh.index >= close_ts) & (gh.index <= close_ts + pd.Timedelta(hours=1))]
        if candidates.empty or end_cands.empty:
            continue
        p_open  = float(candidates['open'].iloc[0])
        p_close = float(end_cands['close'].iloc[0])
        ret = (p_close / p_open - 1) * 100
        rows1h.append({'date': date, 'macro_raw': score_row['macro_raw'],
                       'macro_score': score_row['macro_score'], 'sess_ret': ret})

    s1h = pd.DataFrame(rows1h).set_index('date')
    print(f"  Matched days: {len(s1h)}")

    print(f"\n  {'Score':>8} {'Days':>6} {'1h avg':>10} {'1h WR':>9}")
    print(f"  {'─'*38}")
    for raw in [-3, -1, 1, 3]:
        sub = s1h[s1h['macro_raw'] == raw]
        if len(sub) == 0: continue
        score10 = raw/3*10
        avg = sub['sess_ret'].mean()
        wr  = (sub['sess_ret'] > 0).mean()*100
        flag = " ←bullish" if raw>0 else " ←bearish" if raw<0 else ""
        print(f"  {score10:>+7.1f} {len(sub):>6} {avg:>+9.2f}% {wr:>7.1f}%{flag}")

    corr, p = stats.pearsonr(s1h['macro_score'], s1h['sess_ret'])
    t, pt   = stats.ttest_1samp(
        s1h.loc[s1h['macro_raw']>0,'sess_ret'].values.tolist() +
        (-s1h.loc[s1h['macro_raw']<0,'sess_ret']).values.tolist(), 0)
    print(f"\n  Correlation (score → 1h return): {corr:+.4f} (p={p:.4f})")
    print(f"  Trade-with-score p-value: {pt:.4f}  {'✓ SIGNIFICANT' if pt<0.05 else '— not significant'}")
    if p < 0.05:
        print(f"\n  ✓ Macro score predicts the first hour of your session.")
    else:
        print(f"\n  ✗ Macro score does not predict the first hour.")

except Exception as e:
    print(f"  1h test failed: {e}")
