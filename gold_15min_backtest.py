"""
GOLD 15-MIN TRADING SYSTEM — BACKTEST
======================================
Run locally: pip install yfinance pandas numpy scipy && python gold_15min_backtest.py

ARCHITECTURE:
  Layer 1 — Macro bias     (daily)   : Real yields + DXY → LONG/SHORT/FLAT day
  Layer 2 — Session window (hourly)  : London open + NY open windows only
  Layer 3 — Entry signal   (15-min)  : EMA cross + VWAP dev + Prev S/R + ATR
  Layer 4 — Exit rules     (15-min)  : 2:1 RR, ATR-based, time stop at session end

SIGNAL LOGIC:
  Entry fires ONLY when:
    → Macro bias agrees with signal direction
    → Inside London or NY session window
    → At least 2 of 4 intraday signals confirm
    → Not already in a trade

EXIT logic:
    → TP: 1.5x ATR from entry
    → SL: 0.75x ATR from entry  (2:1 RR)
    → Time: close at session window end regardless
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, time, timedelta
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT      = 10000.0    # starting capital
RISK_PCT     = 0.01       # risk 1% per trade
TP_ATR_MULT  = 1.5        # TP = 1.5x ATR
SL_ATR_MULT  = 0.75       # SL = 0.75x ATR  → 2:1 RR
ATR_PERIOD   = 14         # ATR lookback
EMA_FAST     = 9
EMA_SLOW     = 21
VWAP_SIG_THR = 1.2        # VWAP deviation threshold in σ
MIN_SIGNALS  = 2          # minimum confirming signals — test 3 if WR too low
LOOKBACK     = "60d"       # yfinance lookback — need 200+ trades
COMMISSION   = 0.00008    # 0.008% per side (gold futures typical)

# Dubai session windows in UTC
# London open: 08:00-12:00 UTC  (12:00-16:00 Dubai)
# NY open:     13:30-17:00 UTC  (17:30-21:00 Dubai)
# NY session only — London showed 32.3% WR vs NY at 50%
# NY open: 13:30-17:00 UTC (17:30-21:00 Dubai)
SESSIONS = [
    ("NY",     time(13,30), time(17, 0)),
]

print("="*68)
print("  GOLD 15-MIN SYSTEM BACKTEST")
print("  Macro filter + Session windows + Multi-signal entry")
print("="*68)

# ── STEP 1: DOWNLOAD DATA ─────────────────────────────────────────────────────
print("\nDownloading data...")

# Gold 15-min
gold = yf.download("GC=F", period=LOOKBACK, interval="15m", progress=False)
gold.columns = [c[0].lower() for c in gold.columns]
gold = gold[['open','high','low','close','volume']].copy()
gold.index = pd.to_datetime(gold.index).tz_localize(None) if gold.index.tz is None \
             else pd.to_datetime(gold.index).tz_convert(None)
gold.dropna(inplace=True)
print(f"  Gold 15m : {len(gold)} bars | {gold.index[0].date()} → {gold.index[-1].date()}")
print(f"  Price range: ${gold['close'].min():.0f} – ${gold['close'].max():.0f}")

# Macro daily data
def get_daily(sym, name):
    try:
        d = yf.download(sym, period=LOOKBACK, interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name:<10}: {len(d)} days | last: {d['close'].iloc[-1]:.3f}")
        return d['close']
    except Exception as e:
        print(f"  {name:<10}: FAILED ({e})")
        return None

dxy = get_daily("DX-Y.NYB", "DXY")
tnx = get_daily("^TNX",     "10Y yield")
vix = get_daily("^VIX",     "VIX")
tips= get_daily("TIP",      "TIPS ETF")   # TIPS ETF as real yield proxy

# ── STEP 2: COMPUTE MACRO BIAS ────────────────────────────────────────────────
print("\nComputing macro bias...")

macro = pd.DataFrame(index=gold.index.normalize().unique())

# Real yield proxy: 10Y nominal yield minus TIPS change rate
if tnx is not None and tips is not None:
    # Real yield direction: falling = bullish gold, rising = bearish gold
    macro['tnx_5d']   = tnx.pct_change(5).reindex(macro.index, method='ffill')
    macro['tips_5d']  = tips.pct_change(5).reindex(macro.index, method='ffill')
    # Real yield rising (tnx up, tips down) = bearish gold
    macro['real_yield_bias'] = -(macro['tnx_5d'] * 2 - macro['tips_5d'])
elif tnx is not None:
    macro['real_yield_bias'] = -tnx.pct_change(5).reindex(macro.index, method='ffill')
else:
    macro['real_yield_bias'] = 0.0

# DXY bias: falling dollar = bullish gold
if dxy is not None:
    macro['dxy_bias'] = -dxy.pct_change(3).reindex(macro.index, method='ffill')
else:
    macro['dxy_bias'] = 0.0

# VIX bias: rising VIX = risk-off = gold bullish (safe haven)
if vix is not None:
    macro['vix_bias'] = vix.pct_change(1).reindex(macro.index, method='ffill') * 0.5
else:
    macro['vix_bias'] = 0.0

# Composite macro score (-1 to +1): positive = long bias, negative = short bias
macro['score'] = (
    macro['real_yield_bias'] * 0.45 +
    macro['dxy_bias']        * 0.35 +
    macro['vix_bias']        * 0.20
)

# Normalise to [-1, +1]
roll_std = macro['score'].rolling(30).std()
macro['score_z'] = macro['score'] / (roll_std + 1e-10)
macro['score_z'] = macro['score_z'].clip(-2, 2) / 2

# Macro verdict: >0.15 = LONG day, <-0.15 = SHORT day, else FLAT
macro['bias'] = macro['score_z'].apply(
    lambda x: 'LONG' if x > 0.05 else ('SHORT' if x < -0.05 else 'FLAT')
)

bias_counts = macro['bias'].value_counts()
print(f"  Macro bias distribution: {dict(bias_counts)}")

# ── STEP 3: COMPUTE 15-MIN INDICATORS ────────────────────────────────────────
print("\nComputing 15-min signals...")

df = gold.copy()

# EMAs
df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
df['ema_bull'] = df['ema_fast'] > df['ema_slow']
df['ema_cross_up']   = df['ema_bull'] & ~df['ema_bull'].shift(1).fillna(False)
df['ema_cross_down'] = ~df['ema_bull'] & df['ema_bull'].shift(1).fillna(True)

# ATR
df['tr'] = np.maximum(
    df['high'] - df['low'],
    np.maximum(
        abs(df['high'] - df['close'].shift(1)),
        abs(df['low']  - df['close'].shift(1))
    )
)
df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()

# Session VWAP (reset each calendar day)
df['date'] = df.index.date
df['typical'] = (df['high'] + df['low'] + df['close']) / 3
df['cum_tv']  = df.groupby('date').apply(
    lambda g: (g['typical'] * g['volume']).cumsum()
).reset_index(level=0, drop=True)
df['cum_v']   = df.groupby('date')['volume'].cumsum()
df['vwap']    = df['cum_tv'] / (df['cum_v'] + 1e-10)

# VWAP std dev (for band calculation)
df['sq_dev']    = (df['typical'] - df['vwap'])**2 * df['volume']
df['cum_sqdev'] = df.groupby('date')['sq_dev'].cumsum()
df['vwap_std']  = np.sqrt(df['cum_sqdev'] / (df['cum_v'] + 1e-10))
df['vwap_std']  = df['vwap_std'].clip(lower=df['close'] * 0.0003)
df['vwap_dev']  = (df['close'] - df['vwap']) / (df['vwap_std'] + 1e-10)

# Previous session high/low (prior calendar day's range)
daily_hl = df.groupby('date').agg(day_high=('high','max'), day_low=('low','min'))
daily_hl.index = pd.to_datetime(daily_hl.index)
df.index = pd.to_datetime(df.index)

prev_high = daily_hl['day_high'].shift(1)
prev_low  = daily_hl['day_low'].shift(1)
df['prev_high'] = df['date'].apply(
    lambda d: prev_high.get(pd.Timestamp(d), np.nan))
df['prev_low']  = df['date'].apply(
    lambda d: prev_low.get(pd.Timestamp(d), np.nan))

# Near previous high/low (within 0.15% = potential S/R test)
df['near_prev_high'] = abs(df['close'] - df['prev_high']) / df['close'] < 0.0015
df['near_prev_low']  = abs(df['close'] - df['prev_low'])  / df['close'] < 0.0015

# Momentum (close direction over last 3 bars)
df['mom3'] = df['close'] - df['close'].shift(3)

# Session detection
def get_session(ts):
    t = ts.time() if hasattr(ts, 'time') else ts
    for name, start, end in SESSIONS:
        if start <= t < end:
            return name
    return None

df['session'] = df.index.map(get_session)
df['in_session'] = df['session'].notna()

# Attach macro bias
df['macro_date'] = df.index.normalize()
df['macro_bias'] = df['macro_date'].map(macro['bias'])

df.dropna(subset=['atr','vwap','ema_fast','ema_slow'], inplace=True)

print(f"  Indicators computed: {len(df)} bars")
print(f"  In-session bars: {df['in_session'].sum()} ({df['in_session'].mean()*100:.1f}%)")

# ── STEP 4: DEFINE ENTRY SIGNALS ──────────────────────────────────────────────

def count_long_signals(row):
    """Count how many of 4 signals confirm a LONG entry."""
    count = 0
    # 1. EMA: fast above slow + recent cross up
    if row['ema_bull']:
        count += 1
    # 2. VWAP: price below VWAP (oversold within session) with positive momentum
    if row['vwap_dev'] <= -VWAP_SIG_THR and row['mom3'] > 0:
        count += 1
    # 3. Previous low test: bouncing off prior session low
    if row['near_prev_low'] and row['mom3'] > 0:
        count += 1
    # 4. ATR expansion in upward direction
    if row['mom3'] > row['atr'] * 0.3:
        count += 1
    return count

def count_short_signals(row):
    """Count how many of 4 signals confirm a SHORT entry."""
    count = 0
    # 1. EMA: fast below slow
    if not row['ema_bull']:
        count += 1
    # 2. VWAP: price above VWAP (overbought within session) with negative momentum
    if row['vwap_dev'] >= VWAP_SIG_THR and row['mom3'] < 0:
        count += 1
    # 3. Previous high test: rejected at prior session high
    if row['near_prev_high'] and row['mom3'] < 0:
        count += 1
    # 4. ATR expansion in downward direction
    if row['mom3'] < -row['atr'] * 0.3:
        count += 1
    return count

print("Computing signal scores...")
df['long_sigs']  = df.apply(count_long_signals, axis=1)
df['short_sigs'] = df.apply(count_short_signals, axis=1)

# Entry conditions
df['go_long']  = (
    (df['long_sigs']  >= MIN_SIGNALS) &
    (df['macro_bias'] == 'LONG') &
    df['in_session']
)
df['go_short'] = (
    (df['short_sigs'] >= MIN_SIGNALS) &
    (df['macro_bias'] == 'SHORT') &
    df['in_session']
)

print(f"  Long signals  (pre-filter): {df['go_long'].sum()}")
print(f"  Short signals (pre-filter): {df['go_short'].sum()}")

# ── STEP 5: BACKTEST ──────────────────────────────────────────────────────────
print("\nRunning backtest...")

equity    = ACCOUNT
in_trade  = False
entry     = {}
trades    = []
eq_curve  = [ACCOUNT]
eq_dates  = [df.index[0]]

for i in range(len(df)):
    row  = df.iloc[i]
    idx  = df.index[i]
    cp   = row['close']
    atr  = row['atr']
    sess = row['session']

    if pd.isna(atr) or atr == 0:
        continue

    # ── EXIT ──────────────────────────────────────────────────────────────────
    if in_trade:
        dir_  = entry['dir']
        ep    = entry['price']
        tp    = entry['tp']
        sl    = entry['sl']
        esess = entry['session']

        pnl = (cp - ep) if dir_ == 'LONG' else (ep - cp)

        hit_tp   = (dir_ == 'LONG'  and cp >= tp) or (dir_ == 'SHORT' and cp <= tp)
        hit_sl   = (dir_ == 'LONG'  and cp <= sl) or (dir_ == 'SHORT' and cp >= sl)
        end_sess = (sess != esess) and (sess is None or esess != sess)
        # Also exit if session ends (no longer in same session)
        time_out = sess is None and esess is not None

        if hit_tp or hit_sl or time_out:
            # Calculate actual exit price
            if hit_tp:
                exit_px = tp
                xt = 'TP'
            elif hit_sl:
                exit_px = sl
                xt = 'SL'
            else:
                exit_px = cp
                xt = 'TIME'

            raw_pnl = (exit_px - ep) if dir_ == 'LONG' else (ep - exit_px)
            raw_pnl_pct = raw_pnl / ep
            net_pnl_pct = raw_pnl_pct - 2 * COMMISSION
            usd_pnl     = equity * net_pnl_pct * entry['size_mult']
            equity      += usd_pnl

            trades.append({
                'entry_time':  str(entry['time'])[:16],
                'exit_time':   str(idx)[:16],
                'session':     esess,
                'dir':         dir_,
                'entry_px':    round(ep, 2),
                'exit_px':     round(exit_px, 2),
                'tp':          round(tp, 2),
                'sl':          round(sl, 2),
                'atr_at_entry':round(entry['atr'], 2),
                'macro_bias':  entry['bias'],
                'n_signals':   entry['n_sigs'],
                'raw_pnl%':    round(raw_pnl_pct * 100, 4),
                'net_pnl%':    round(net_pnl_pct * 100, 4),
                'pnl_usd':     round(usd_pnl, 2),
                'equity':      round(equity, 2),
                'exit':        xt,
                'bars':        i - entry['bar_idx'],
            })
            eq_curve.append(equity)
            eq_dates.append(idx)
            in_trade = False

    # ── ENTRY ─────────────────────────────────────────────────────────────────
    if not in_trade:
        if row['go_long']:
            tp_px = cp + atr * TP_ATR_MULT
            sl_px = cp - atr * SL_ATR_MULT
            risk_per_unit = cp - sl_px                        # $ per oz
            risk_dollars  = equity * RISK_PCT                  # $ at risk
            oz            = risk_dollars / (risk_per_unit + 1e-10)
            # size_mult = fraction of equity this position represents
            size_mult     = (oz * cp) / equity                 # position value / equity
            in_trade = True
            entry = {
                'time':    idx, 'bar_idx': i, 'price': cp,
                'dir':     'LONG', 'tp': tp_px, 'sl': sl_px,
                'atr':     atr, 'session': sess,
                'bias':    row['macro_bias'],
                'n_sigs':  row['long_sigs'],
                'size_mult': min(size_mult, 5.0),  # cap at 5x leverage
            }
        elif row['go_short']:
            tp_px = cp - atr * TP_ATR_MULT
            sl_px = cp + atr * SL_ATR_MULT
            risk_per_unit = sl_px - cp
            risk_dollars  = equity * RISK_PCT
            oz            = risk_dollars / (risk_per_unit + 1e-10)
            size_mult     = (oz * cp) / equity
            in_trade = True
            entry = {
                'time':    idx, 'bar_idx': i, 'price': cp,
                'dir':     'SHORT', 'tp': tp_px, 'sl': sl_px,
                'atr':     atr, 'session': sess,
                'bias':    row['macro_bias'],
                'n_sigs':  row['short_sigs'],
                'size_mult': min(size_mult, 5.0),
            }

# ── STEP 6: RESULTS ───────────────────────────────────────────────────────────
if not trades:
    print("\nNo trades taken. Try relaxing MIN_SIGNALS or extending lookback.")
    exit()

tdf  = pd.DataFrame(trades)
wins = tdf[tdf['net_pnl%'] > 0]
loss = tdf[tdf['net_pnl%'] <= 0]

wr   = len(wins) / len(tdf)
roi  = (equity / ACCOUNT - 1) * 100
pf   = wins['pnl_usd'].sum() / abs(loss['pnl_usd'].sum()) \
       if len(loss) > 0 and loss['pnl_usd'].sum() != 0 else 99

eq_s = pd.Series(eq_curve, index=eq_dates)
mdd  = ((eq_s - eq_s.expanding().max()) / eq_s.expanding().max() * 100).min()

longs  = tdf[tdf['dir'] == 'LONG']
shorts = tdf[tdf['dir'] == 'SHORT']

# Monthly breakdown
tdf['month'] = pd.to_datetime(tdf['entry_time']).dt.to_period('M')
mo = tdf.groupby('month').agg(
    trades=('pnl_usd','count'),
    pnl   =('pnl_usd','sum'),
    wr    =('net_pnl%', lambda x: round((x>0).mean()*100, 1))
).reset_index()
mo['roi%'] = (mo['pnl'] / ACCOUNT * 100).round(2)

# By exit type
by_exit = tdf.groupby('exit').agg(
    n    =('net_pnl%','count'),
    wr_pct=('net_pnl%', lambda x: round((x>0).mean()*100,1)),
    avg  =('net_pnl%','mean'),
    total=('pnl_usd','sum')
)

# By session
by_sess = tdf.groupby('session').agg(
    n    =('net_pnl%','count'),
    wr_pct=('net_pnl%', lambda x: round((x>0).mean()*100,1)),
    avg  =('net_pnl%','mean'),
    total=('pnl_usd','sum')
)

print(f"\n{'='*68}")
print(f"  GOLD 15-MIN SYSTEM — BACKTEST RESULTS")
print(f"  Period: {tdf['entry_time'].min()[:10]} → {tdf['exit_time'].max()[:10]}")
print(f"{'='*68}")
print(f"  Total trades   : {len(tdf)}")
print(f"  Win rate       : {wr*100:.1f}%")
print(f"  Profit factor  : {pf:.2f}")
print(f"  ROI            : {roi:+.2f}%")
print(f"  Max drawdown   : {float(mdd):.2f}%")
print(f"  Starting equity: ${ACCOUNT:,.2f}")
print(f"  Final equity   : ${equity:,.2f}")
print(f"  Net P&L        : ${equity-ACCOUNT:+,.2f}")
print(f"  Avg win        : +{wins['net_pnl%'].mean():.4f}%  (${wins['pnl_usd'].mean():+.2f})")
print(f"  Avg loss       : {loss['net_pnl%'].mean():.4f}%  (${loss['pnl_usd'].mean():+.2f})")
print(f"  Avg hold       : {tdf['bars'].mean():.1f} bars ({tdf['bars'].mean()*15:.0f} min)")
print(f"  Trades/month   : {len(tdf)/len(mo):.1f}")
print(f"\n  LONG  trades: {len(longs):>4} | WR: {(longs['net_pnl%']>0).mean()*100:.1f}%")
print(f"  SHORT trades: {len(shorts):>4} | WR: {(shorts['net_pnl%']>0).mean()*100:.1f}%")

print(f"\n  BY EXIT TYPE:")
print(by_exit.to_string())

print(f"\n  BY SESSION:")
print(by_sess.to_string())

print(f"\n  MONTHLY P&L:")
print(mo[['month','trades','pnl','wr','roi%']].to_string(index=False))
print(f"\n  Positive months: {(mo['pnl']>0).sum()}/{len(mo)}")
print(f"  Avg monthly P&L: ${mo['pnl'].mean():+,.2f}")
print(f"  Best month:      ${mo['pnl'].max():+,.2f}")
print(f"  Worst month:     ${mo['pnl'].min():+,.2f}")

# Break-even WR
avg_win  = wins['net_pnl%'].mean() if len(wins) > 0 else 0
avg_loss = abs(loss['net_pnl%'].mean()) if len(loss) > 0 else 0
be_wr    = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 0.5
print(f"\n  Break-even WR  : {be_wr*100:.1f}%")
print(f"  Actual WR      : {wr*100:.1f}%")
print(f"  Edge vs break-even: {(wr-be_wr)*100:+.1f}%")

# Statistical significance
if len(wins) > 5 and len(loss) > 5:
    t_stat, p_val = stats.ttest_1samp(tdf['net_pnl%'], 0)
    print(f"\n  Statistical significance:")
    print(f"  t-statistic: {t_stat:.3f}")
    print(f"  p-value:     {p_val:.4f} {'✓ SIGNIFICANT' if p_val < 0.05 else '— not significant'}")

print(f"\n{'='*68}")
print(f"  SIGNAL BREAKDOWN (avg confirming signals per trade)")
print(f"{'='*68}")
print(f"  Avg signals on winning trades: {wins['n_signals'].mean():.2f}")
print(f"  Avg signals on losing trades : {loss['n_signals'].mean():.2f}")

# Signal analysis — do more confirming signals = higher WR?
for n in range(MIN_SIGNALS, 5):
    sub = tdf[tdf['n_signals'] >= n]
    if len(sub) > 3:
        sub_wr = (sub['net_pnl%'] > 0).mean() * 100
        print(f"  ≥{n} signals: n={len(sub):>4} | WR={sub_wr:.1f}%")

print(f"\n{'='*68}")
print(f"  PARAMETER SENSITIVITY")
print(f"{'='*68}")
print(f"  Current: MIN_SIGNALS={MIN_SIGNALS} | TP={TP_ATR_MULT}x | SL={SL_ATR_MULT}x")
print(f"\n  Try adjusting:")
print(f"    MIN_SIGNALS = 3   → fewer trades, likely higher WR")
print(f"    MIN_SIGNALS = 2   → more trades, likely lower WR")
print(f"    TP_ATR_MULT = 2.0 → wider targets, lower WR but higher avg win")
print(f"    SL_ATR_MULT = 0.5 → tighter stops, lower WR but lower avg loss")
print(f"{'='*68}")

# Save results
tdf.to_csv("gold_15min_trades.csv", index=False)
mo.to_csv("gold_15min_monthly.csv", index=False)
print(f"\nSaved: gold_15min_trades.csv + gold_15min_monthly.csv")
print("Paste results here and I'll analyse them.")
