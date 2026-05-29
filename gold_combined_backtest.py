"""
GOLD COMBINED SYSTEM — FULL BACKTEST
======================================
Backtests macro score + 1H technical system working together.

RULES:
  1. Each day: compute macro score from prior-day data (no lookahead)
     Score > 0 = bullish, Score < 0 = bearish, Score = 0 = skip

  2. Session window: 13:30–17:00 UTC (17:30–21:00 Dubai) only

  3. Entry: first bar where ALL of:
     a) EMA9/21 direction matches macro direction (EMA9>21 for bull, EMA9<21 for bear)
     b) Price crosses back through VWAP in macro direction
        (LONG: price moves from below VWAP to above VWAP)
        (SHORT: price moves from above VWAP to below VWAP)
     → Only first signal per session; skip if no signal

  4. Exits:
     TP1: +1.5×ATR → close 60%, move SL to breakeven
     TP2: +2.5×ATR → close remaining 40%
     SL:  −0.75×ATR from entry
     Time stop: exit after 3 bars if no TP1
     Session close: exit all at 17:00 UTC

  5. Position sizing: 3% risk per trade on current portfolio

$10,000 starting | 3% risk per trade
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

ACCOUNT   = 10_000.0
RISK_PCT  = 0.03
SL_MULT   = 0.75
TP1_MULT  = 1.5
TP2_MULT  = 2.5
TP1_FRAC  = 0.60
TP2_FRAC  = 0.40
TIME_STOP = 3
EMA_FAST  = 9
EMA_SLOW  = 21
ATR_PER   = 14
SESS_OPEN = (13, 30)
SESS_CLOSE= (17,  0)

print("="*68)
print("  GOLD COMBINED SYSTEM — MACRO + 1H TECHNICAL BACKTEST")
print("  $10,000 portfolio | 3% risk | ATR exits | VWAP cross entry")
print("="*68)

# ── DATA ──────────────────────────────────────────────────────────────────────
print("\nDownloading data...")

gold_1h = yf.download("GC=F", period="730d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index   = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)
print(f"  1h gold : {len(gold_1h)} bars | {gold_1h.index[0].date()} → {gold_1h.index[-1].date()}")

def dl(t, n):
    d = yf.download(t, period="3y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(None)
    print(f"  {n}: {len(d)} days")
    return d['close']

gold_d = dl("GC=F",     "Gold daily")
dxy_d  = dl("DX-Y.NYB", "DXY")
tnx_d  = dl("^TNX",     "10Y yield")
gdx_d  = dl("GDX",      "GDX miners")

# ── MACRO SCORES (shift 1 = use yesterday's data for today's score) ───────────
print("\nBuilding macro scores (no lookahead)...")

dm = pd.DataFrame({'gold': gold_d})
dm['dxy'] = dxy_d.reindex(dm.index, method='ffill')
dm['tnx'] = tnx_d.reindex(dm.index, method='ffill')
dm['gdx'] = gdx_d.reindex(dm.index, method='ffill')
dm.dropna(inplace=True)

dm['ema50']   = dm['gold'].ewm(span=50, adjust=False).mean()
dm['s_trend'] = np.where(dm['gold'] > dm['ema50'], 1, -1)
dm['s_yield'] = np.where(dm['tnx'].diff() < 0, 1, -1)
dm['s_dxy']   = np.where(dm['dxy'].pct_change() < 0, 1, -1)
dm['s_mom']   = np.where(dm['gold'].pct_change(5) > 0, 1, -1)
dm['s_gdx']   = np.where(dm['gdx'].pct_change() > dm['gold'].pct_change(), 1, -1)

# shift(1) so today's score only uses data known at prior close
raw = (dm['s_trend'] + dm['s_yield'] + dm['s_dxy'] + dm['s_mom'] + dm['s_gdx']).shift(1)
dm['score']  = (raw / 5 * 10).round(1)
dm['dir']    = np.where(dm['score'] > 0, 1, np.where(dm['score'] < 0, -1, 0))
dm.dropna(inplace=True)

# Build fast lookup date→(score, dir)
macro_lookup = {d.date(): (row['score'], int(row['dir'])) for d, row in dm.iterrows()}

# ── 1H INDICATORS (computed on full history → no lookahead on indicators) ─────
df = gold_1h.copy()
df['ema9']  = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
df['ema21'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
hi_lo_close = pd.concat([
    df['high'] - df['low'],
    (df['high'] - df['close'].shift()).abs(),
    (df['low']  - df['close'].shift()).abs()
], axis=1).max(axis=1)
df['atr'] = hi_lo_close.ewm(span=ATR_PER, adjust=False).mean()

# ── BACKTEST ──────────────────────────────────────────────────────────────────
print("Running backtest...")

equity  = ACCOUNT
trades  = []
daily_stats = []

session_dates = sorted(set(df.index.date))

for date in session_dates:
    entry = macro_lookup.get(date)
    if entry is None: continue
    m_score, direction = entry
    if direction == 0: continue

    ts = pd.Timestamp(date)
    o_ts = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
    c_ts = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
    sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
    if len(sbars) < 2: continue

    # Session VWAP — expanding within session
    tp    = (sbars['high'] + sbars['low'] + sbars['close']) / 3
    vol   = sbars['volume'].replace(0, 1)
    sbars = sbars.copy()
    sbars['vwap'] = (tp * vol).cumsum() / vol.cumsum()

    in_trade = False
    entry_px = sl = tp1_px = tp2_px = sz1 = sz2 = pnl1 = 0
    tp1_hit  = False
    bars_held= 0

    for i in range(1, len(sbars)):
        b    = sbars.iloc[i]
        prev = sbars.iloc[i - 1]
        hi, lo, cls = b['high'], b['low'], b['close']
        vwap = b['vwap']

        if in_trade:
            bars_held += 1
            if direction == 1:   # long
                if lo <= sl:
                    pnl = (sl - entry_px) * (sz1 + sz2) + pnl1
                    trades.append(dict(date=date, dir='LONG', entry=entry_px,
                        exit=sl, pnl=pnl, reason='SL', score=m_score, bars=bars_held))
                    equity += pnl; in_trade = False; continue
                if not tp1_hit and hi >= tp1_px:
                    pnl1 = (tp1_px - entry_px) * sz1
                    equity += pnl1; sl = entry_px; tp1_hit = True
                if tp1_hit and hi >= tp2_px:
                    pnl2 = (tp2_px - entry_px) * sz2
                    trades.append(dict(date=date, dir='LONG', entry=entry_px,
                        exit=tp2_px, pnl=pnl1+pnl2, reason='TP2', score=m_score, bars=bars_held))
                    equity += pnl2; in_trade = False; continue
            else:                # short
                if hi >= sl:
                    pnl = (entry_px - sl) * (sz1 + sz2) + pnl1
                    trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                        exit=sl, pnl=pnl, reason='SL', score=m_score, bars=bars_held))
                    equity += pnl; in_trade = False; continue
                if not tp1_hit and lo <= tp1_px:
                    pnl1 = (entry_px - tp1_px) * sz1
                    equity += pnl1; sl = entry_px; tp1_hit = True
                if tp1_hit and lo <= tp2_px:
                    pnl2 = (entry_px - tp2_px) * sz2
                    trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                        exit=tp2_px, pnl=pnl1+pnl2, reason='TP2', score=m_score, bars=bars_held))
                    equity += pnl2; in_trade = False; continue

            # Time stop
            if bars_held >= TIME_STOP:
                rem = sz2 if tp1_hit else (sz1 + sz2)
                pnl = ((cls - entry_px) if direction==1 else (entry_px - cls)) * rem + pnl1
                trades.append(dict(date=date, dir='LONG' if direction==1 else 'SHORT',
                    entry=entry_px, exit=cls, pnl=pnl, reason='TIME', score=m_score, bars=bars_held))
                equity += pnl; in_trade = False

        else:
            # Entry condition:
            # EMA9/21 aligned with macro direction
            ema_bull = b['ema9'] > b['ema21']
            ema_bear = b['ema9'] < b['ema21']
            ema_ok   = (direction == 1 and ema_bull) or (direction == -1 and ema_bear)
            if not ema_ok: continue

            # VWAP cross: price crosses from wrong side to right side
            cross_up   = prev['close'] < prev['vwap'] and cls >= vwap   # price crossed VWAP upward
            cross_down = prev['close'] > prev['vwap'] and cls <= vwap   # price crossed VWAP downward
            vwap_ok    = (direction == 1 and cross_up) or (direction == -1 and cross_down)
            if not vwap_ok: continue

            # Enter
            atr_v = b['atr']
            if atr_v <= 0: continue
            risk_usd = equity * RISK_PCT
            tot_sz   = risk_usd / (SL_MULT * atr_v)
            sz1 = tot_sz * TP1_FRAC
            sz2 = tot_sz * TP2_FRAC

            entry_px = cls
            if direction == 1:
                sl     = entry_px - SL_MULT  * atr_v
                tp1_px = entry_px + TP1_MULT * atr_v
                tp2_px = entry_px + TP2_MULT * atr_v
            else:
                sl     = entry_px + SL_MULT  * atr_v
                tp1_px = entry_px - TP1_MULT * atr_v
                tp2_px = entry_px - TP2_MULT * atr_v

            in_trade = True; bars_held = 0; tp1_hit = False; pnl1 = 0

    # Session close — exit if still open
    if in_trade:
        cls = sbars.iloc[-1]['close']
        rem = sz2 if tp1_hit else (sz1 + sz2)
        pnl = ((cls - entry_px) if direction==1 else (entry_px - cls)) * rem + pnl1
        trades.append(dict(date=date, dir='LONG' if direction==1 else 'SHORT',
            entry=entry_px, exit=cls, pnl=pnl, reason='SESS', score=m_score, bars=bars_held))
        equity += pnl

# ── RESULTS ───────────────────────────────────────────────────────────────────
if not trades:
    print("No trades generated — check date range overlap"); exit()

t = pd.DataFrame(trades)
t['date']  = pd.to_datetime(t['date'])
t['win']   = t['pnl'] > 0
t['month'] = t['date'].dt.to_period('M')
t['year']  = t['date'].dt.year

n        = len(t)
wr       = t['win'].mean() * 100
roi      = (equity - ACCOUNT) / ACCOUNT * 100
days_sp  = (t['date'].max() - t['date'].min()).days
wks      = max(days_sp / 7, 1)
tpw      = n / wks
avg_w    = t.loc[t['win'],'pnl'].mean()
avg_l    = t.loc[~t['win'],'pnl'].mean()
rr       = abs(avg_w / avg_l) if avg_l != 0 else 0
_, pval  = stats.ttest_1samp(t['pnl'], 0)
eq_c     = ACCOUNT + t['pnl'].cumsum()
mdd      = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
sharpe   = (t['pnl'].mean() / t['pnl'].std() * np.sqrt(252)) if t['pnl'].std() > 0 else 0

print(f"\n{'='*68}")
print(f"  RESULTS  |  {t['date'].min().date()} → {t['date'].max().date()}")
print(f"{'='*68}")
print(f"""
  Portfolio    : $10,000 starting  →  ${equity:,.0f} final
  Total ROI    : {roi:+.1f}%   (${equity-ACCOUNT:+,.0f})
  Sharpe ratio : {sharpe:.2f}
  Max drawdown : {mdd:.1f}%

  Trades       : {n}  ({tpw:.1f} per week)
  Win rate     : {wr:.1f}%
  Avg win      : ${avg_w:+,.1f}
  Avg loss     : ${avg_l:+,.1f}
  Risk/reward  : {rr:.2f}x
  p-value      : {pval:.4f}  {'✓ SIGNIFICANT' if pval<0.05 else '— not significant'}""")

print(f"\n  Exit breakdown:")
for r, g in t.groupby('reason'):
    print(f"    {r:<8}  {len(g):>4} trades | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

print(f"\n  Direction breakdown:")
for d, g in t.groupby('dir'):
    print(f"    {d:<6}  {len(g):>4} trades | WR {g['win'].mean()*100:.0f}% | total ${g['pnl'].sum():+,.0f}")

print(f"\n{'='*68}")
print(f"  MONTHLY BREAKDOWN")
print(f"{'='*68}")
print(f"  {'Month':>9} {'N':>4} {'WR':>7} {'P&L':>9} {'Equity':>9}")
print(f"  {'─'*44}")
run_eq = ACCOUNT
for mo, g in t.groupby('month'):
    run_eq += g['pnl'].sum()
    flag = "✓" if g['pnl'].sum() > 0 else "✗"
    print(f"  {str(mo):>9} {len(g):>4} {g['win'].mean()*100:>6.0f}% ${g['pnl'].sum():>+8,.0f}  ${run_eq:>7,.0f} {flag}")

pos_mo = sum(1 for _, g in t.groupby('month') if g['pnl'].sum() > 0)
tot_mo = t['month'].nunique()
print(f"\n  Positive months: {pos_mo}/{tot_mo}  ({pos_mo/tot_mo*100:.0f}%)")

print(f"\n{'='*68}")
print(f"  BY MACRO SCORE STRENGTH")
print(f"{'='*68}")
print(f"  {'Score band':>12} {'N':>5} {'WR':>7} {'Avg P&L':>9} {'Sig?':>6}")
print(f"  {'─'*44}")
bins   = [(-10.1,-6),(-6,-2),(-2,0),(0,2),(2,6),(6,10.1)]
labels = ['−10..−6','−6..−2','−2..0','0..+2','+2..+6','+6..+10']
for (lo,hi), lbl in zip(bins, labels):
    g = t[(t['score'] > lo) & (t['score'] <= hi)]
    if len(g) < 3: continue
    _, p = stats.ttest_1samp(g['pnl'], 0) if len(g) > 5 else (0,1)
    print(f"  {lbl:>12} {len(g):>5} {g['win'].mean()*100:>6.0f}% ${g['pnl'].mean():>+8,.0f}  {'✓' if p<0.05 else ''}")

print(f"\n{'='*68}")
print(f"  VERDICT")
print(f"{'='*68}")
if pval < 0.05 and roi > 0:
    print(f"  ✓ PROFITABLE AND SIGNIFICANT")
    print(f"    ROI {roi:+.1f}% | Sharpe {sharpe:.2f} | {tpw:.1f} trades/week | WR {wr:.0f}%")
elif roi > 0:
    print(f"  ~ Profitable but p={pval:.3f} (need more data to confirm)")
    print(f"    ROI {roi:+.1f}% | {tpw:.1f} trades/week")
else:
    print(f"  ✗ Not profitable (ROI {roi:+.1f}%, p={pval:.3f})")
print("="*68)

t.to_csv("gold_combined_trades.csv", index=False)
print("Saved: gold_combined_trades.csv")
