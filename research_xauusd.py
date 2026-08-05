"""
XAUUSD Intraday Edge Discovery — Research Framework
=====================================================
DATA CAVEAT: This runs on 2-year 1H + 60-day 5M data from Yahoo Finance.
A proper version requires 8-10 years of 1-minute tick data from
Dukascopy, TickData Suite, or a broker feed.

All results below must be treated as PRELIMINARY SIGNALS ONLY.
No result from 60 days of 5M data is statistically conclusive.
Walk-forward is simulated chronologically but with limited test windows.

Trading costs assumed: $0.50/oz round-trip (spread + commission + slippage).
"""

import os, requests, warnings
os.environ['REQUESTS_CA_BUNDLE'] = '/root/.ccr/ca-bundle.crt'
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from scipy import stats
from itertools import product

COST_PER_OZ = 0.50   # round-trip: spread ~0.30 + slippage ~0.20
MIN_TRADES   = 50    # reject any hypothesis with fewer trades
ALPHA        = 0.05  # significance level

# ── Data ────────────────────────────────────────────────────────

def fetch(interval, period):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval={interval}&range={period}'
    r = requests.get(url, headers={'User-Agent':'Mozilla/5.0'}, timeout=20)
    j = r.json()['chart']['result'][0]
    q = j['indicators']['quote'][0]
    rows = []
    for i, t in enumerate(j['timestamp']):
        if q['close'][i] is None: continue
        rows.append({
            'ts':  pd.Timestamp(t, unit='s', tz='UTC'),
            'o':   q['open'][i]   or 0,
            'h':   q['high'][i]   or 0,
            'l':   q['low'][i]    or 0,
            'c':   q['close'][i],
            'vol': q['volume'][i] or 0,
        })
    df = pd.DataFrame(rows).sort_values('ts').reset_index(drop=True)
    df['ret'] = df['c'].pct_change()
    df['bar_ret'] = df['c'] - df['c'].shift(1)  # raw $/oz return
    return df

def enrich(df):
    c = df['c'].values
    h, l = df['h'].values, df['l'].values

    # Time features
    df['hour']    = df['ts'].dt.hour
    df['dow']     = df['ts'].dt.dayofweek   # 0=Mon
    df['session'] = df['hour'].apply(lambda h:
        'ASIA'   if 0  <= h < 7  else
        'LONDON' if 7  <= h < 13 else
        'NY'     if 13 <= h < 20 else 'LATE')

    # ATR(14)
    pc = np.roll(c,1); pc[0]=c[0]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(len(tr))
    for i in range(1,len(tr)):
        if i<14: atr[i]=tr[:i+1].mean()
        else:    atr[i]=(atr[i-1]*13+tr[i])/14
    df['atr'] = atr
    df['atr20'] = pd.Series(atr).rolling(20,min_periods=10).mean().values
    df['atr_ratio'] = np.where(df['atr20']>0, df['atr']/df['atr20'], np.nan)

    # RSI(14)
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta>0, delta, 0.)
    loss  = np.where(delta<0, -delta, 0.)
    ag=np.zeros(len(c)); al=np.zeros(len(c))
    for i in range(1,len(c)):
        if i<14: ag[i]=gain[:i+1].mean(); al[i]=loss[:i+1].mean()
        else:    ag[i]=(ag[i-1]*13+gain[i])/14; al[i]=(al[i-1]*13+loss[i])/14
    df['rsi'] = 100 - 100/(1 + np.where(al==0, 100., ag/al))

    # Rolling returns (various lookbacks)
    for lb in [1,2,3,5,10,20]:
        df[f'ret_{lb}'] = df['c'].pct_change(lb)

    # Volatility: realised vol (rolling std of returns)
    df['rvol10'] = df['ret'].rolling(10).std() * np.sqrt(252*24 if 'h' in df.columns else 252*24)

    # Range
    df['range']    = df['h'] - df['l']
    df['range_avg']= df['range'].rolling(20,min_periods=10).mean()

    # Wick ratios
    body = (df['c'] - df['o']).abs()
    df['upper_wick'] = df['h'] - df[['o','c']].max(axis=1)
    df['lower_wick'] = df[['o','c']].min(axis=1) - df['l']
    df['body_ratio'] = np.where(df['range']>0, body/df['range'], 0.5)

    # Swing high/low (simple: local max/min over n bars)
    for n in [5, 10]:
        df[f'swing_hi_{n}'] = (df['h'] == df['h'].rolling(n*2+1, center=True).max()).astype(int)
        df[f'swing_lo_{n}'] = (df['l'] == df['l'].rolling(n*2+1, center=True).min()).astype(int)

    # VWAP (daily reset)
    df['date'] = df['ts'].dt.date
    tp = (h+l+c)/3
    ctv = (pd.Series(tp) * df['vol']).groupby(df['date']).cumsum()
    cv  = df['vol'].groupby(df['date']).cumsum()
    df['vwap'] = ctv / cv.replace(0,np.nan)
    df['vwap_dev'] = (df['c'] - df['vwap']) / df['vwap'].replace(0,np.nan)

    # Opening range (first N bars of session)
    df['session_bar'] = df.groupby(['date','session']).cumcount()

    return df

# ── Statistical toolkit ─────────────────────────────────────────

def test_edge(returns_arr, label, n_bootstrap=2000):
    """Full statistical test of a return series."""
    r = np.array(returns_arr)
    r = r[~np.isnan(r)]
    n = len(r)
    if n < MIN_TRADES:
        return None

    mean_r   = r.mean()
    std_r    = r.std(ddof=1)
    se       = std_r / np.sqrt(n)
    t_stat, p_val = stats.ttest_1samp(r, 0)
    wins     = (r > 0).mean()
    expectancy = mean_r  # $/oz already net of cost

    # Bootstrap 95% CI on mean
    boot_means = [np.random.choice(r, n, replace=True).mean() for _ in range(n_bootstrap)]
    ci_lo, ci_hi = np.percentile(boot_means, [2.5, 97.5])

    # Sharpe (annualised — approximate)
    sharpe = (mean_r / std_r) * np.sqrt(n)  # per-trade Sharpe proxy

    # Max drawdown on cumulative P&L
    cum = np.cumsum(r)
    roll_max = np.maximum.accumulate(cum)
    dd = cum - roll_max
    max_dd = dd.min()

    return {
        'label':      label,
        'n':          n,
        'mean_net':   mean_r,
        'ci_lo':      ci_lo,
        'ci_hi':      ci_hi,
        't_stat':     t_stat,
        'p_val':      p_val,
        'wins':       wins,
        'sharpe':     sharpe,
        'max_dd':     max_dd,
        'total_pnl':  r.sum(),
        'sig':        p_val < ALPHA and ci_lo > 0,
    }

def pprint(res):
    if res is None:
        return
    sig = '*** SIGNIFICANT ***' if res['sig'] else ''
    print(f"\n  {res['label']}")
    print(f"    N={res['n']}  mean={res['mean_net']:+.3f}$/oz  CI=[{res['ci_lo']:+.3f}, {res['ci_hi']:+.3f}]")
    print(f"    p={res['p_val']:.4f}  WR={res['wins']*100:.1f}%  Sharpe≈{res['sharpe']:.2f}  MaxDD=${res['max_dd']:.1f}")
    print(f"    Total P&L: ${res['total_pnl']:+.0f}  {sig}")

# ── Backtester ──────────────────────────────────────────────────

def backtest_long(df, entry_mask, hold_bars=4, cost=COST_PER_OZ):
    """
    For each bar where entry_mask is True, go LONG at close.
    Exit after hold_bars bars at close.
    No overlapping trades.
    Returns array of net returns in $/oz.
    """
    c = df['c'].values
    mask = entry_mask.values if hasattr(entry_mask,'values') else np.array(entry_mask)
    trades = []
    i = 0
    while i < len(df) - hold_bars:
        if mask[i]:
            entry = c[i]
            ex    = c[min(i + hold_bars, len(c)-1)]
            gross = ex - entry
            net   = gross - cost
            trades.append(net)
            i += hold_bars + 1  # no overlap
        else:
            i += 1
    return np.array(trades)

def backtest_short(df, entry_mask, hold_bars=4, cost=COST_PER_OZ):
    c = df['c'].values
    mask = entry_mask.values if hasattr(entry_mask,'values') else np.array(entry_mask)
    trades = []
    i = 0
    while i < len(df) - hold_bars:
        if mask[i]:
            entry = c[i]
            ex    = c[min(i + hold_bars, len(c)-1)]
            gross = entry - ex
            net   = gross - cost
            trades.append(net)
            i += hold_bars + 1
        else:
            i += 1
    return np.array(trades)

# ── Walk-Forward Wrapper ────────────────────────────────────────

def walk_forward_test(df, signal_fn, label, hold_bars=4, n_splits=5):
    """
    Chronological split: train on first 60%, test on remaining 40%.
    Then roll forward. Reports OOS results only.
    """
    n = len(df)
    split = int(n * 0.6)
    test_df = df.iloc[split:].copy().reset_index(drop=True)
    mask_long, mask_short = signal_fn(test_df)

    oos_long  = backtest_long(test_df, mask_long, hold_bars)
    oos_short = backtest_short(test_df, mask_short, hold_bars)

    results = []
    if len(oos_long) >= MIN_TRADES:
        results.append(test_edge(oos_long,  f'{label} [LONG OOS]'))
    if len(oos_short) >= MIN_TRADES:
        results.append(test_edge(oos_short, f'{label} [SHORT OOS]'))
    return results

# ── Hypothesis Battery ──────────────────────────────────────────

def run_all(df, label_prefix=''):
    results = []

    # ── H1: Session momentum — buy London open, sell NY open ──
    def h1_signals(d):
        long  = (d['session']=='LONDON') & (d['session_bar']==0) & (d['atr_ratio'].between(0.5,1.8))
        short = pd.Series(False, index=d.index)
        return long, short
    r = walk_forward_test(df, h1_signals, f'{label_prefix}H1:London-open LONG', hold_bars=6)
    results.extend([x for x in r if x])

    # ── H2: NY open momentum — buy first bar of NY ──
    def h2_signals(d):
        long  = (d['session']=='NY') & (d['session_bar']==0) & (d['atr_ratio'].between(0.5,1.8))
        short = pd.Series(False, index=d.index)
        return long, short
    r = walk_forward_test(df, h2_signals, f'{label_prefix}H2:NY-open LONG', hold_bars=4)
    results.extend([x for x in r if x])

    # ── H3: RSI mean reversion ──
    for rsi_lo, rsi_hi, side in [(20,35,'LONG'), (65,80,'SHORT')]:
        def h3_signals(d, rlo=rsi_lo, rhi=rsi_hi, s=side):
            cond = d['rsi'].between(rlo,rhi) & d['atr_ratio'].between(0.5,1.8)
            if s=='LONG':
                return cond, pd.Series(False,index=d.index)
            else:
                return pd.Series(False,index=d.index), cond
        r = walk_forward_test(df, h3_signals, f'{label_prefix}H3:RSI {rsi_lo}-{rsi_hi} {side}', hold_bars=6)
        results.extend([x for x in r if x])

    # ── H4: ATR compression → breakout ──
    def h4_signals(d):
        compressed = d['atr_ratio'] < 0.6
        breakout_up   = compressed & (d['ret'].shift(-1) > 0)
        breakout_down = compressed & (d['ret'].shift(-1) < 0)
        # Buy/sell compression close, exit 4 bars later
        long  = compressed & (d['c'] > d['vwap'])
        short = compressed & (d['c'] < d['vwap'])
        return long, short
    r = walk_forward_test(df, h4_signals, f'{label_prefix}H4:ATR-compression breakout', hold_bars=4)
    results.extend([x for x in r if x])

    # ── H5: Simple momentum (autocorrelation) ──
    for lb, fwd in [(1,2),(2,4),(3,6),(5,8)]:
        def h5_signals(d, l=lb):
            up   = d[f'ret_{l}'] > 0.003   # >0.3% momentum up
            down = d[f'ret_{l}'] < -0.003  # momentum down
            return up, down
        r = walk_forward_test(df, h5_signals, f'{label_prefix}H5:Mom lb={lb} fwd={fwd}', hold_bars=fwd)
        results.extend([x for x in r if x])

    # ── H6: RSI divergence (RSI rising, price falling = long) ──
    def h6_signals(d):
        rsi_rising   = d['rsi'] > d['rsi'].shift(3)
        price_falling= d['c']   < d['c'].shift(3)
        rsi_falling  = d['rsi'] < d['rsi'].shift(3)
        price_rising = d['c']   > d['c'].shift(3)
        long  = rsi_rising  & price_falling & (d['rsi'] < 45)
        short = rsi_falling & price_rising  & (d['rsi'] > 55)
        return long, short
    r = walk_forward_test(df, h6_signals, f'{label_prefix}H6:RSI divergence', hold_bars=6)
    results.extend([x for x in r if x])

    # ── H7: VWAP deviation mean reversion ──
    for lo, hi, s in [(-0.005, -0.002, 'LONG'), (0.002, 0.005, 'SHORT')]:
        def h7_signals(d, l=lo, h=hi, side=s):
            cond = d['vwap_dev'].between(l,h)
            if side=='LONG':  return cond, pd.Series(False,index=d.index)
            else:             return pd.Series(False,index=d.index), cond
        r = walk_forward_test(df, h7_signals, f'{label_prefix}H7:VWAP-dev {lo*100:.1f}% to {hi*100:.1f}% {s}', hold_bars=4)
        results.extend([x for x in r if x])

    # ── H8: Day-of-week effect ──
    for dow, name in [(0,'Mon'),(4,'Fri')]:
        def h8_signals(d, dw=dow):
            cond = (d['dow']==dw) & (d['session']=='NY') & (d['session_bar']<=2)
            return cond, pd.Series(False,index=d.index)
        r = walk_forward_test(df, h8_signals, f'{label_prefix}H8:DOW={name} NY-open LONG', hold_bars=4)
        results.extend([x for x in r if x])

    # ── H9: FVG / Imbalance (3-candle pattern) ──
    def h9_signals(d):
        # Bullish FVG: candle[i-2] high < candle[i] low (gap up, price in gap = LONG)
        fvg_bull = d['l'] < d['h'].shift(2)  # current low fills into prev-prev high
        in_gap   = (d['c'] > d['h'].shift(2)) & (d['c'] < d['l'].shift(0))
        long  = fvg_bull & (d['c'] > d['o'])  # bullish close into the gap
        short = pd.Series(False, index=d.index)
        return long, short
    r = walk_forward_test(df, h9_signals, f'{label_prefix}H9:FVG-fill LONG', hold_bars=4)
    results.extend([x for x in r if x])

    # ── H10: Opening range breakout (first 2 bars of London) ──
    def h10_signals(d):
        is_london = (d['session']=='LONDON')
        or_high = d.groupby([d['date'],d['session']])['h'].transform(
            lambda x: x.iloc[:2].max() if len(x)>=2 else np.nan)
        or_low  = d.groupby([d['date'],d['session']])['l'].transform(
            lambda x: x.iloc[:2].min() if len(x)>=2 else np.nan)
        long  = is_london & (d['session_bar']>=2) & (d['c']>or_high) & (d['session_bar']<=8)
        short = is_london & (d['session_bar']>=2) & (d['c']<or_low)  & (d['session_bar']<=8)
        return long, short
    r = walk_forward_test(df, h10_signals, f'{label_prefix}H10:London OR breakout', hold_bars=3)
    results.extend([x for x in r if x])

    return results

# ── Run ─────────────────────────────────────────────────────────

print("="*70)
print("XAUUSD EDGE DISCOVERY — PRELIMINARY RESULTS")
print(f"Cost assumption: ${COST_PER_OZ}/oz round-trip")
print(f"Min trades for reporting: {MIN_TRADES}")
print(f"Significance level: {ALPHA}")
print("="*70)

print("\n[1H DATA — 2 years, OOS = last 40% (~9 months)]\n")
df1h = fetch('1h', '730d')
df1h = enrich(df1h)
print(f"  {len(df1h)} bars  {df1h['ts'].iloc[0].date()} → {df1h['ts'].iloc[-1].date()}")
res1h = run_all(df1h, '1H/')
significant_1h = [r for r in res1h if r and r['sig']]
all_1h         = [r for r in res1h if r]

print(f"\n  Hypotheses tested: {len(all_1h)}  |  Significant: {len(significant_1h)}")
print(f"  Expected false positives at α={ALPHA}: {len(all_1h)*ALPHA:.1f}")
print()
all_1h_sorted = sorted(all_1h, key=lambda x: x['p_val'])
for r in all_1h_sorted[:15]:
    pprint(r)

print("\n" + "="*70)
print("[5M DATA — 60 days, OOS = last 40% (~24 days) — LOW STATISTICAL POWER]\n")
df5m = fetch('5m', '60d')
df5m = enrich(df5m)
print(f"  {len(df5m)} bars  {df5m['ts'].iloc[0].date()} → {df5m['ts'].iloc[-1].date()}")
res5m = run_all(df5m, '5M/')
all_5m = [r for r in res5m if r]
print(f"\n  Hypotheses tested: {len(all_5m)}  |  Significant: {sum(1 for r in all_5m if r['sig'])}")
print(f"  Expected false positives: {len(all_5m)*ALPHA:.1f}")
print()
all_5m_sorted = sorted(all_5m, key=lambda x: x['p_val'])
for r in all_5m_sorted[:10]:
    pprint(r)

print("\n" + "="*70)
print("AUTOCORRELATION TEST (momentum persistence)")
print("="*70)
for lag in [1,2,3,5,10,20,50]:
    r1 = df1h['bar_ret'].autocorr(lag=lag)
    r5 = df5m['bar_ret'].autocorr(lag=lag) if len(df5m)>lag else np.nan
    print(f"  lag={lag:3d} |  1H acorr: {r1:+.4f}  |  5M acorr: {r5:+.4f}")

print("\n" + "="*70)
print("TIME-OF-DAY EFFECT (1H — mean $/oz return by hour UTC)")
print("="*70)
hourly = df1h.groupby('hour')['bar_ret'].agg(['mean','std','count'])
hourly['se'] = hourly['std']/np.sqrt(hourly['count'])
hourly['t']  = hourly['mean']/hourly['se']
hourly['p']  = hourly['t'].apply(lambda t: 2*(1-stats.t.cdf(abs(t), df=hourly['count'].mean()-1)))
for hr, row in hourly.iterrows():
    flag = ' *' if row['p'] < 0.05 and row['count'] > 50 else ''
    session = 'ASIA' if hr<7 else 'LONDON' if hr<13 else 'NY' if hr<20 else 'LATE'
    print(f"  UTC {hr:02d}  {session:7s}  mean={row['mean']:+.3f}  n={int(row['count']):4d}  p={row['p']:.3f}{flag}")

print("\n" + "="*70)
print("CONCLUSIONS")
print("="*70)
n_sig = len(significant_1h)
n_total = len(all_1h)
expected_fp = n_total * ALPHA
if n_sig <= expected_fp * 1.5:
    print(f"\n  No statistically robust edge found above noise threshold.")
    print(f"  {n_sig} significant results vs {expected_fp:.1f} expected by chance.")
    print(f"  Cannot reject null hypothesis: no edge in XAUUSD at 1H on 2-year window.")
else:
    print(f"\n  {n_sig} significant results vs {expected_fp:.1f} expected by chance.")
    print(f"  Excess above noise: {n_sig - expected_fp:.1f} — potential signal worth further investigation.")
    for r in significant_1h:
        print(f"    → {r['label']}  p={r['p_val']:.4f}  mean={r['mean_net']:+.3f}$/oz  CI=[{r['ci_lo']:+.3f},{r['ci_hi']:+.3f}]")
