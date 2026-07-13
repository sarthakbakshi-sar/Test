"""
Gold & Silver Backtest — VWAP Mean Reversion
  LONG:  RSI oversold (prev bar) turning up + price below VWAP
  SHORT: RSI overbought (prev bar) turning down + price above VWAP

Same framework as sys_breakout2.py:
  - SL = 1.5 × ATR, TP = 2.5 × ATR, max 48 bars
  - NY session only: 13:30–20:00 UTC Mon–Fri
  - ATR regime filter: 0.5–1.8× 20-bar avg ATR
  - Parameter breakdowns to find optimal thresholds

Data: Yahoo Finance 1H (GC=F gold, SI=F silver) — 2 years
"""

import os, time, requests
os.environ['REQUESTS_CA_BUNDLE'] = '/root/.ccr/ca-bundle.crt'
import numpy as np
import pandas as pd

SL_M, TP_M = 1.5, 2.5
RISK       = 200
MAX_BARS   = 48

METALS = [('GC=F', 'GOLD'), ('SI=F', 'SILVER')]

# ── data ──────────────────────────────────────────────────────────────────────

def fetch_yahoo(ticker, range_str='2y'):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
    p = {'interval': '1h', 'range': range_str}
    h = {'User-Agent': 'Mozilla/5.0'}
    for attempt in range(4):
        try:
            r = requests.get(url, params=p, headers=h, timeout=20)
            j = r.json()['chart']['result'][0]
            break
        except Exception as e:
            if attempt == 3: raise
            time.sleep(2 ** attempt)
    ts = pd.to_datetime(j['timestamp'], unit='s', utc=True)
    q  = j['indicators']['quote'][0]
    df = pd.DataFrame({
        'ts':  ts,
        'o':   q['open'],
        'h':   q['high'],
        'l':   q['low'],
        'c':   q['close'],
        'vol': q['volume'],
    }).dropna(subset=['c'])
    return df.sort_values('ts').reset_index(drop=True)

def add_indicators(df):
    c = df['c'].values
    # RSI-14 (Wilder)
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag = np.zeros(len(c)); al = np.zeros(len(c))
    for i in range(1, len(c)):
        if i < 14: ag[i] = gain[:i+1].mean(); al[i] = loss[:i+1].mean()
        else:       ag[i] = (ag[i-1]*13 + gain[i])/14; al[i] = (al[i-1]*13 + loss[i])/14
    df['rsi'] = 100 - 100/(1 + np.where(al == 0, 100.0, ag/al))

    # EMA trend
    df['ema20']    = df['c'].ewm(span=20, adjust=False).mean()
    df['ema50']    = df['c'].ewm(span=50, adjust=False).mean()
    df['uptrend']  = (df['ema20'] > df['ema50']).astype(int)

    # Daily VWAP (midnight UTC reset)
    df['date']    = df['ts'].dt.date
    df['tp']      = (df['h'] + df['l'] + df['c']) / 3
    df['cum_tv']  = (df['tp'] * df['vol']).groupby(df['date']).cumsum()
    df['cum_v']   = df['vol'].groupby(df['date']).cumsum()
    df['vwap']    = df['cum_tv'] / df['cum_v'].replace(0, np.nan)
    df['vwap_dev']= (df['c'] - df['vwap']) / df['vwap'].replace(0, np.nan)

    # ATR-14 (Wilder)
    h_v, l_v, pc = df['h'].values, df['l'].values, np.roll(c, 1); pc[0] = c[0]
    tr  = np.maximum(h_v - l_v, np.maximum(np.abs(h_v - pc), np.abs(l_v - pc)))
    atr = np.zeros(len(tr))
    for i in range(1, len(tr)):
        if i < 14: atr[i] = tr[:i+1].mean()
        else:       atr[i] = (atr[i-1]*13 + tr[i])/14
    df['atr']       = atr
    df['atr_avg20'] = pd.Series(atr).rolling(20, min_periods=5).mean().values
    return df

def simulate_exits(df, signals):
    trades = []
    c_arr, h_arr, l_arr = df['c'].values, df['h'].values, df['l'].values
    for sig in signals:
        i     = sig['bar_idx']
        entry = c_arr[i]
        atr   = sig['atr']
        sl = entry - SL_M*atr if sig['side'] == 'LONG' else entry + SL_M*atr
        tp = entry + TP_M*atr if sig['side'] == 'LONG' else entry - TP_M*atr
        outcome = 'TIMEOUT'; pnl_r = 0
        for j in range(i+1, min(i+1+MAX_BARS, len(df))):
            lo, hi = l_arr[j], h_arr[j]
            if sig['side'] == 'LONG':
                if lo <= sl: outcome = 'SL'; pnl_r = -1; break
                if hi >= tp: outcome = 'TP'; pnl_r =  1; break
            else:
                if hi >= sl: outcome = 'SL'; pnl_r = -1; break
                if lo <= tp: outcome = 'TP'; pnl_r =  1; break
        mult = TP_M if pnl_r > 0 else (SL_M if pnl_r < 0 else 0)
        trades.append({**sig, 'outcome': outcome, 'pnl_r': pnl_r, 'pnl_usd': pnl_r*RISK*mult})
    return trades

# ── main ──────────────────────────────────────────────────────────────────────

all_trades = []

for ticker, name in METALS:
    print(f"\nFetching {name} ({ticker})...")
    df = fetch_yahoo(ticker)
    df = add_indicators(df)
    print(f"  {len(df)} bars  {df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()}")
    time.sleep(0.5)

    rsi_v  = df['rsi'].values
    vd_v   = df['vwap_dev'].values
    atr_v  = df['atr'].values
    avg_v  = df['atr_avg20'].values
    up_v   = df['uptrend'].values
    ts_v   = df['ts'].values
    signals = []

    for i in range(55, len(df)):
        if np.isnan(rsi_v[i]) or np.isnan(vd_v[i]): continue
        ts = pd.Timestamp(ts_v[i])
        if ts.weekday() >= 5: continue
        hr = ts.hour + ts.minute/60
        if not (13.5 <= hr <= 20.0): continue
        atr = atr_v[i]; avg = avg_v[i]
        if np.isnan(avg) or avg == 0: continue
        if not (0.5 <= atr/avg <= 1.8): continue

        rsi_p = rsi_v[i-1]
        rsi_c = rsi_v[i]
        vd    = vd_v[i]

        # ── LONG: RSI oversold turning up + below VWAP ───────────────────────
        if rsi_p <= 35 and rsi_c > rsi_p and vd <= -0.005:
            signals.append({
                'sym': name, 'ts': ts, 'bar_idx': i, 'side': 'LONG',
                'atr': atr, 'rsi': rsi_c, 'rsi_prev': rsi_p, 'vwap_dev': vd,
            })

        # ── SHORT: RSI overbought turning down + above VWAP ─────────────────
        if rsi_p >= 65 and rsi_c < rsi_p and vd >= 0.005:
            signals.append({
                'sym': name, 'ts': ts, 'bar_idx': i, 'side': 'SHORT',
                'atr': atr, 'rsi': rsi_c, 'rsi_prev': rsi_p, 'vwap_dev': vd,
            })

    trades = simulate_exits(df, signals)
    all_trades.extend(trades)
    wins = sum(1 for t in trades if t['pnl_r'] > 0)
    lt = [t for t in trades if t['side'] == 'LONG']
    st = [t for t in trades if t['side'] == 'SHORT']
    lstr = f"L:{sum(1 for t in lt if t['pnl_r']>0)}/{len(lt)}" if lt else "L:0"
    sstr = f"S:{sum(1 for t in st if t['pnl_r']>0)}/{len(st)}" if st else "S:0"
    print(f"  {len(trades)}T  WR={wins/len(trades)*100:.0f}%  [{lstr}  {sstr}]" if trades else "  0 trades")

if not all_trades:
    print("No trades."); exit()

res = pd.DataFrame(all_trades)
res['year'] = pd.DatetimeIndex(res['ts']).year
res['win']  = res['pnl_r'] > 0

def pst(label, df, indent=2):
    if len(df) == 0: return
    wr  = df['win'].mean()*100
    pnl = df['pnl_usd'].sum()
    print(f"{'  '*indent}{label:45s} {len(df):4d}T  WR={wr:5.1f}%  P&L=${pnl:+,.0f}")

be = SL_M/(SL_M+TP_M)*100

print("\n" + "="*76)
print("GOLD & SILVER — VWAP MEAN REVERSION — 2-YEAR RESULTS")
print("="*76)
print(f"  SL={SL_M}×ATR  TP={TP_M}×ATR  Break-even WR: {be:.1f}%  Max hold: {MAX_BARS}H")

for metal in ['GOLD', 'SILVER']:
    md = res[res['sym'] == metal]
    if len(md) == 0: continue
    print(f"\n{'─'*76}")
    print(f"  {metal}")
    print(f"{'─'*76}")
    pst("ALL", md)
    pst("LONG  (oversold bounce below VWAP)", md[md['side']=='LONG'])
    pst("SHORT (overbought fade above VWAP)", md[md['side']=='SHORT'])

    print(f"\n  [By Year]")
    for yr in sorted(md['year'].unique()):
        yd = md[md['year']==yr]
        pst(str(yr), yd)
        for side in ['LONG','SHORT']:
            sd = yd[yd['side']==side]
            if len(sd): pst(f"  {side}", sd, indent=3)

    pre26 = md[md['year'] < 2026]
    in26  = md[md['year'] == 2026]
    print(f"\n  [OOS vs In-sample]")
    pst("Pre-2026 (OOS)", pre26)
    pst("  LONG",  pre26[pre26['side']=='LONG'],  indent=3)
    pst("  SHORT", pre26[pre26['side']=='SHORT'], indent=3)
    pst("2026 (in-sample)", in26)
    pst("  LONG",  in26[in26['side']=='LONG'],  indent=3)
    pst("  SHORT", in26[in26['side']=='SHORT'], indent=3)

    # Parameter breakdowns
    lg = md[md['side']=='LONG'].copy()
    sh = md[md['side']=='SHORT'].copy()

    if len(lg) >= 5:
        print(f"\n  [LONG: RSI_prev at entry]")
        lg['rsi_bin'] = pd.cut(lg['rsi_prev'], bins=[20,25,28,30,32,35])
        for b, g in lg.groupby('rsi_bin', observed=True):
            pst(f"RSI_prev {b}", g, indent=3)

        print(f"\n  [LONG: VWAP dev at entry]")
        lg['vd_bin'] = pd.cut(lg['vwap_dev'], bins=[-0.05,-0.02,-0.015,-0.01,-0.005])
        for b, g in lg.groupby('vd_bin', observed=True):
            pst(f"VWAP dev {b}", g, indent=3)

    if len(sh) >= 5:
        print(f"\n  [SHORT: RSI_prev at entry]")
        sh['rsi_bin'] = pd.cut(sh['rsi_prev'], bins=[65,68,70,72,75,80])
        for b, g in sh.groupby('rsi_bin', observed=True):
            pst(f"RSI_prev {b}", g, indent=3)

        print(f"\n  [SHORT: VWAP dev at entry]")
        sh['vd_bin'] = pd.cut(sh['vwap_dev'], bins=[0.005,0.008,0.01,0.015,0.02,0.03])
        for b, g in sh.groupby('vd_bin', observed=True):
            pst(f"VWAP dev {b}", g, indent=3)

print("\n" + "="*76)
print("COMBINED METALS SUMMARY")
print("="*76)
pst("ALL", res)
pst("LONG",  res[res['side']=='LONG'])
pst("SHORT", res[res['side']=='SHORT'])
pnl = res['pnl_usd'].sum()
print(f"\n  Total P&L:    ${pnl:+,.0f}  |  Annualized: ${pnl/2:+,.0f}")
print(f"  Trades/week:  {len(res)/104:.1f}")
print(f"  Break-even:   {be:.1f}%")
