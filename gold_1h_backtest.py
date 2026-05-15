"""
1H GOLD TREND SYSTEM — BACKTEST
=================================
Exact implementation of the described system:

TREND FILTER : Daily 50 EMA — above = LONG days, below = SHORT days
WINDOW       : 13:30-17:00 UTC (17:30-21:00 Dubai) — London/NY overlap only
ENTRY        : EMA 9 crosses EMA 21 in trend direction
               AND price is 1.5σ from session VWAP (returning toward it)
SL           : 0.75x ATR from entry
TP1          : 1.5x ATR → close 60% → move stop to breakeven
TP2          : 2.5x ATR → close remaining 40%
TIME STOP    : 3 bars (3 hours) if no meaningful movement
SESSION STOP : Close at 17:00 UTC regardless

Run: pip install yfinance pandas numpy scipy requests
     python gold_1h_backtest.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
from datetime import time
import requests, warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT      = 10000.0
RISK_PCT     = 0.03       # 3% risk per trade
SL_ATR_MULT  = 0.75       # SL = 0.75x ATR
TP1_ATR_MULT = 1.5        # TP1 = 1.5x ATR
TP2_ATR_MULT = 2.5        # TP2 = 2.5x ATR
TP1_CLOSE    = 0.60       # close 60% at TP1
TP2_CLOSE    = 0.40       # close 40% at TP2
TIME_STOP    = 3          # exit after 3 bars if no move
ATR_PERIOD   = 14
EMA_FAST     = 9
EMA_SLOW     = 21
EMA_TREND    = 50         # daily trend EMA
VWAP_SIG_THR = 1.5        # 1.5σ from VWAP to qualify
COMMISSION   = 0.00008    # 0.008% per side

# Session: 13:30-17:00 UTC (London/NY overlap = 17:30-21:00 Dubai)
SESS_OPEN  = time(13, 30)
SESS_CLOSE = time(17,  0)

TD_KEY = "06f050cd7d9940a895f9461e7f0ff7e3"

print("="*68)
print("  1H GOLD TREND SYSTEM — BACKTEST")
print("  Daily 50 EMA trend | EMA cross + VWAP | ATR exits")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════════════════════════
print("\nDownloading data...")

# 1h gold (730 days)
raw_1h = yf.download("GC=F", period="730d", interval="1h", progress=False)
raw_1h.columns = [c[0].lower() for c in raw_1h.columns]
raw_1h.index   = pd.to_datetime(raw_1h.index).tz_localize(None) if raw_1h.index.tz is None \
                 else pd.to_datetime(raw_1h.index).tz_convert(None)
raw_1h.dropna(inplace=True)
df = raw_1h[['open','high','low','close','volume']].copy()
print(f"  1h data : {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
print(f"  Price   : ${df['close'].min():,.0f} – ${df['close'].max():,.0f}")

# Daily gold for trend filter
raw_d = yf.download("GC=F", period="5y", interval="1d", progress=False)
raw_d.columns = [c[0].lower() for c in raw_d.columns]
raw_d.index   = pd.to_datetime(raw_d.index).tz_localize(None)
raw_d.dropna(inplace=True)
gold_daily = raw_d['close']
print(f"  Daily   : {len(raw_d)} days")

# ══════════════════════════════════════════════════════════════════════════════
#  DAILY TREND FILTER — 50 EMA on daily gold
# ══════════════════════════════════════════════════════════════════════════════
print("\nComputing daily trend filter...")

daily_ema50 = gold_daily.ewm(span=EMA_TREND, adjust=False).mean()
daily_trend = pd.Series(
    np.where(gold_daily > daily_ema50, 'LONG', 'SHORT'),
    index=gold_daily.index
)

# Forward fill to 1h bars
df['date']     = df.index.normalize()
df['trend_day']= df['date'].map(daily_trend.to_dict())
df['trend_day'] = df['trend_day'].ffill()

long_days  = (df['trend_day']=='LONG').mean()*100
short_days = (df['trend_day']=='SHORT').mean()*100
print(f"  LONG bars : {long_days:.1f}% | SHORT bars: {short_days:.1f}%")

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════
print("Computing indicators...")

# EMA 9 and 21
df['ema_f']  = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
df['ema_s']  = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

# EMA crossover (the event — not just the state)
df['ema_above']    = df['ema_f'] > df['ema_s']
df['cross_up']     = df['ema_above'] & ~df['ema_above'].shift(1).fillna(False)
df['cross_down']   = ~df['ema_above'] & df['ema_above'].shift(1).fillna(True)

# EMA separation (need clear trend not tangled)
df['ema_sep'] = (df['ema_f'] - df['ema_s']).abs() / df['close']
df['ema_clear'] = df['ema_sep'] > 0.0003

# ATR
df['tr']  = np.maximum(df['high']-df['low'],
            np.maximum((df['high']-df['close'].shift(1)).abs(),
                       (df['low'] -df['close'].shift(1)).abs()))
df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()

# Session VWAP — resets each UTC day
df['typ']    = (df['high']+df['low']+df['close'])/3
df['ctv']    = df.groupby('date').apply(
    lambda g:(g['typ']*g['volume']).cumsum()).reset_index(level=0,drop=True)
df['cv']     = df.groupby('date')['volume'].cumsum()
df['vwap']   = df['ctv']/(df['cv']+1e-10)
df['sq_dev'] = (df['typ']-df['vwap'])**2 * df['volume']
df['csq']    = df.groupby('date')['sq_dev'].cumsum()
df['vstd']   = np.sqrt(df['csq']/(df['cv']+1e-10)).clip(lower=df['close']*0.0002)

# VWAP deviation in standard deviations
df['vwap_dev'] = (df['close']-df['vwap'])/(df['vstd']+1e-10)

# Session filter
def in_session(ts):
    t = ts.time() if hasattr(ts,'time') else ts
    return SESS_OPEN <= t < SESS_CLOSE

df['in_sess'] = df.index.map(in_session)

# Session bar count (for warmup — need at least 4 bars for good VWAP)
df['sess_bar'] = df.groupby('date').cumcount()

df.dropna(subset=['atr','vwap','ema_f','ema_s','trend_day'], inplace=True)

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY CONDITIONS
#
#  LONG: daily trend = LONG
#        AND EMA 9 crosses ABOVE EMA 21 on this bar
#        AND price is below VWAP by 1.5σ (oversold, returning to VWAP)
#        AND in session window
#        AND at least 4 bars into session (VWAP has enough data)
#
#  SHORT: daily trend = SHORT
#         AND EMA 9 crosses BELOW EMA 21 on this bar
#         AND price is above VWAP by 1.5σ (overbought, returning to VWAP)
#         AND in session window
# ══════════════════════════════════════════════════════════════════════════════

df['long_entry'] = (
    (df['trend_day'] == 'LONG')      &   # daily trend: LONG
    df['cross_up']                   &   # EMA 9 crosses above EMA 21
    df['ema_clear']                  &   # EMAs clearly separated
    (df['vwap_dev'] <= -VWAP_SIG_THR)&   # price 1.5σ below VWAP
    df['in_sess']                    &   # in session window
    (df['sess_bar'] >= 4)                # VWAP has warmed up
)

df['short_entry'] = (
    (df['trend_day'] == 'SHORT')     &   # daily trend: SHORT
    df['cross_down']                 &   # EMA 9 crosses below EMA 21
    df['ema_clear']                  &   # EMAs clearly separated
    (df['vwap_dev'] >= VWAP_SIG_THR) &   # price 1.5σ above VWAP
    df['in_sess']                    &   # in session window
    (df['sess_bar'] >= 4)                # VWAP has warmed up
)

print(f"  Long signals  : {df['long_entry'].sum()}")
print(f"  Short signals : {df['short_entry'].sum()}")

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════
print("\nRunning backtest...")

equity   = ACCOUNT
in_trade = False
entry    = {}
trades   = []
eq_c     = [ACCOUNT]
eq_d     = [df.index[0]]

for i in range(len(df)):
    row = df.iloc[i]
    idx = df.index[i]
    cp  = row['close']
    atr = row['atr']

    if pd.isna(atr) or atr == 0:
        continue

    # ── EXIT ─────────────────────────────────────────────────────────────────
    if in_trade:
        d_       = entry['dir']
        ep       = entry['price']
        sl       = entry['sl']
        tp1      = entry['tp1']
        tp2      = entry['tp2']
        be_sl    = entry.get('be_sl', sl)
        tp1_hit  = entry.get('tp1_hit', False)
        bars_in  = i - entry['bar_idx']
        active_sl= be_sl if tp1_hit else sl

        hit_tp1   = not tp1_hit and (
            (d_=='LONG'  and cp >= tp1) or
            (d_=='SHORT' and cp <= tp1))
        hit_tp2   = tp1_hit and (
            (d_=='LONG'  and cp >= tp2) or
            (d_=='SHORT' and cp <= tp2))
        hit_sl    = (d_=='LONG'  and cp <= active_sl) or \
                    (d_=='SHORT' and cp >= active_sl)
        hit_time  = bars_in >= TIME_STOP and not tp1_hit
        hit_sess  = not row['in_sess'] and entry.get('was_in_sess', True)

        if hit_tp1:
            xp  = tp1
            raw = (xp-ep) if d_=='LONG' else (ep-xp)
            pct = raw/ep - COMMISSION
            usd = equity * pct * entry['sm'] * TP1_CLOSE
            equity += usd
            entry['tp1_hit']  = True
            entry['be_sl']    = ep                    # move stop to breakeven
            entry['tp1_pnl']  = usd
            entry['tp1_pct']  = round(pct*100, 4)
            entry['sm']      *= TP2_CLOSE             # resize for remaining

        elif hit_tp2 or hit_sl or hit_time or hit_sess:
            xp  = tp2 if hit_tp2 else active_sl if hit_sl else cp
            xt  = 'TP2' if hit_tp2 else 'SL' if hit_sl else \
                  'TIME' if hit_time else 'SESS'
            raw = (xp-ep) if d_=='LONG' else (ep-xp)
            pct = raw/ep - COMMISSION
            usd = equity * pct * entry['sm']
            equity += usd

            total_usd = usd + entry.get('tp1_pnl', 0)
            net_pct   = (xp/ep-1)*100 if d_=='LONG' else (ep/xp-1)*100

            trades.append({
                'entry_time':  str(entry['time'])[:16],
                'exit_time':   str(idx)[:16],
                'dir':         d_,
                'entry_px':    round(ep, 2),
                'exit_px':     round(xp, 2),
                'atr_entry':   round(entry['atr_e'], 2),
                'sl':          round(sl, 2),
                'tp1':         round(tp1, 2),
                'tp2':         round(tp2, 2),
                'tp1_hit':     entry.get('tp1_hit', False),
                'tp1_pct':     entry.get('tp1_pct', 0),
                'exit_pct':    round(pct*100, 4),
                'total_usd':   round(total_usd, 2),
                'equity':      round(equity, 2),
                'exit':        xt,
                'bars':        bars_in,
                'vwap_dev':    round(entry['vwap_dev'], 3),
                'trend':       entry['trend'],
            })
            eq_c.append(equity)
            eq_d.append(idx)
            in_trade = False

    # ── ENTRY ─────────────────────────────────────────────────────────────────
    if not in_trade:
        if row['long_entry']:
            sl_px  = cp - atr * SL_ATR_MULT
            tp1_px = cp + atr * TP1_ATR_MULT
            tp2_px = cp + atr * TP2_ATR_MULT
            sl_pct = (cp - sl_px) / cp
            oz     = (equity * RISK_PCT) / (cp * sl_pct + 1e-10)
            sm     = min((oz * cp) / equity, 5.0)
            in_trade = True
            entry  = dict(time=idx, bar_idx=i, price=cp, dir='LONG',
                          sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                          tp1_hit=False, be_sl=sl_px,
                          atr_e=atr, vwap_dev=row['vwap_dev'],
                          trend=row['trend_day'], was_in_sess=True)

        elif row['short_entry']:
            sl_px  = cp + atr * SL_ATR_MULT
            tp1_px = cp - atr * TP1_ATR_MULT
            tp2_px = cp - atr * TP2_ATR_MULT
            sl_pct = (sl_px - cp) / cp
            oz     = (equity * RISK_PCT) / (cp * sl_pct + 1e-10)
            sm     = min((oz * cp) / equity, 5.0)
            in_trade = True
            entry  = dict(time=idx, bar_idx=i, price=cp, dir='SHORT',
                          sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                          tp1_hit=False, be_sl=sl_px,
                          atr_e=atr, vwap_dev=row['vwap_dev'],
                          trend=row['trend_day'], was_in_sess=True)

# ══════════════════════════════════════════════════════════════════════════════
#  RESULTS
# ══════════════════════════════════════════════════════════════════════════════
if not trades:
    print("\nNo trades taken.")
    print("Tips: VWAP threshold may be too high, or EMA cross + VWAP rarely align.")
    print("Try lowering VWAP_SIG_THR from 1.5 to 1.0 and rerun.")
    exit()

tdf   = pd.DataFrame(trades)
wins  = tdf[tdf['total_usd'] > 0]
loss  = tdf[tdf['total_usd'] <= 0]
wr    = len(wins)/len(tdf)
roi   = (equity/ACCOUNT-1)*100
pf    = wins['total_usd'].sum()/abs(loss['total_usd'].sum()) \
        if len(loss)>0 and loss['total_usd'].sum()!=0 else 99
eq_s  = pd.Series(eq_c, index=eq_d)
mdd   = float(((eq_s-eq_s.expanding().max())/eq_s.expanding().max()*100).min())
aw    = wins['total_usd'].mean()  if len(wins)>0 else 0
al    = abs(loss['total_usd'].mean()) if len(loss)>0 else 0
aw_pct= wins['total_usd'].sum()/wins['total_usd'].count()/ACCOUNT*100 if len(wins)>0 else 0
al_pct= abs(loss['total_usd'].sum()/loss['total_usd'].count()/ACCOUNT*100) if len(loss)>0 else 0
be_wr = al/(aw+al) if (aw+al)>0 else 0.5
tp1_r = tdf['tp1_hit'].mean()*100
days  = (pd.to_datetime(tdf['exit_time'].max())-
         pd.to_datetime(tdf['entry_time'].min())).days
tpw   = len(tdf)/max(days,1)*5   # per trading day

# By direction
by_dir = tdf.groupby('dir').apply(lambda x: pd.Series({
    'n':    len(x),
    'wr%':  round((x['total_usd']>0).mean()*100, 1),
    'avg$': round(x['total_usd'].mean(), 2),
    'pnl$': round(x['total_usd'].sum(), 2),
}), include_groups=False)

# By exit type
by_exit = tdf.groupby('exit').apply(lambda x: pd.Series({
    'n':    len(x),
    'wr%':  round((x['total_usd']>0).mean()*100, 1),
    'avg$': round(x['total_usd'].mean(), 2),
    'pnl$': round(x['total_usd'].sum(), 2),
}), include_groups=False)

# Monthly
tdf['month'] = pd.to_datetime(tdf['entry_time']).dt.to_period('M')
mo = tdf.groupby('month').agg(
    n   =('total_usd','count'),
    pnl =('total_usd','sum'),
    wr  =('total_usd', lambda x:round((x>0).mean()*100,1))
).reset_index()
mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

# Stats
t_stat, p_val = stats.ttest_1samp(tdf['total_usd'],0) if len(tdf)>5 else (0,1)

print(f"\n{'='*68}")
print(f"  1H GOLD TREND SYSTEM — RESULTS")
print(f"  Period : {tdf['entry_time'].min()[:10]} → {tdf['exit_time'].max()[:10]}")
print(f"  Config : 3% risk | 0.75x ATR SL | 1.5x/2.5x TP | {VWAP_SIG_THR}σ VWAP")
print(f"{'='*68}")
print(f"  Total trades    : {len(tdf)}")
print(f"  Win rate        : {wr*100:.1f}%  (break-even: {be_wr*100:.1f}%,  edge: {(wr-be_wr)*100:+.1f}%)")
print(f"  TP1 hit rate    : {tp1_r:.1f}%")
print(f"  Profit factor   : {pf:.2f}")
print(f"  ROI             : {roi:+.2f}%")
print(f"  Final equity    : ${equity:,.2f}")
print(f"  Net P&L         : ${equity-ACCOUNT:+,.2f}")
print(f"  Max drawdown    : {mdd:.2f}%")
print(f"  Avg win         : +${aw:,.2f} (+{aw_pct:.3f}%)")
print(f"  Avg loss        : -${al:,.2f} (-{al_pct:.3f}%)")
print(f"  RR ratio        : {aw/al:.2f}:1" if al>0 else "  RR ratio        : ∞")
print(f"  Avg hold        : {tdf['bars'].mean():.1f} bars ({tdf['bars'].mean():.0f}h)")
print(f"  Trades/week     : {tpw:.1f}")
print(f"  +ve months      : {(mo['pnl']>0).sum()}/{len(mo)}")
print(f"  p-value         : {p_val:.4f}  {'✓ SIGNIFICANT' if p_val<0.05 else '— not significant'}")

print(f"\n  BY DIRECTION:")
print(by_dir.to_string())

print(f"\n  BY EXIT TYPE:")
print(by_exit.to_string())

print(f"\n  MONTHLY P&L:")
print(mo[['month','n','pnl','wr','roi%']].to_string(index=False))
print(f"\n  +ve months      : {(mo['pnl']>0).sum()}/{len(mo)}")
print(f"  Avg monthly P&L : ${mo['pnl'].mean():+,.2f}")
print(f"  Best month      : ${mo['pnl'].max():+,.2f}")
print(f"  Worst month     : ${mo['pnl'].min():+,.2f}")

# Expected value
ev_per_trade = wr*aw - (1-wr)*al
print(f"\n{'='*68}")
print(f"  EXPECTED VALUE")
print(f"{'='*68}")
print(f"  EV per trade    : ${ev_per_trade:+,.2f}")
print(f"  Trades needed   : {int(5000/ev_per_trade)+1 if ev_per_trade>0 else 'N/A'} to make $5,000")
print(f"  At {tpw:.1f} trades/day: ~{int(5000/ev_per_trade/tpw)+1 if ev_per_trade>0 else 'N/A'} trading days")
print(f"\n  NOTE: If 0 trades taken, lower VWAP_SIG_THR to 1.0 and rerun.")
print(f"        EMA cross + 1.5σ VWAP is strict — may fire rarely.")
print(f"{'='*68}")

tdf.to_csv("gold_1h_trades.csv", index=False)
mo.to_csv("gold_1h_monthly.csv", index=False)
print("\nSaved: gold_1h_trades.csv + gold_1h_monthly.csv")
print("Paste full results here for analysis.")
