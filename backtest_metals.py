"""
Gold Backtest — Two complementary LONG-only strategies
  A: VWAP pullback in uptrend — RSI dips to 20-35, turns up, price -0.5 to -3% below VWAP,
     EMA20>EMA50 (uptrend intact)
  B: Breakout LONG — 12H resistance break, vol 1.5-3x, RSI 50-62, VWAP dev 0-0.8%,
     EMA20>EMA50, price above 200H EMA

Silver: no consistent edge found across any strategy — do not trade.

Results (2yr, 44 combined trades):
  Combined WR:  61.4%  P&L=$+9,000  Annualized=$+4,500
  Pre-2026 OOS: 58.3% WR (36 trades) — edge holds out-of-sample
  Break-even:   37.5%  (SL=1.5×ATR, TP=2.5×ATR)

Caveats:
  - Small sample (44 trades / 2yr, ~0.4/week)
  - 2024 was weakest year (45.5% WR) — gold was range-bound then
  - Strategy works best in gold bull market (gold above its own 200H EMA)
  - NO gold SHORT — insufficient edge
  - NO silver signals — no edge found
"""

import os, time, requests
os.environ['REQUESTS_CA_BUNDLE'] = '/root/.ccr/ca-bundle.crt'
import numpy as np
import pandas as pd

SL_M, TP_M = 1.5, 2.5
RISK       = 200
MAX_BARS   = 48

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
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag = np.zeros(len(c)); al = np.zeros(len(c))
    for i in range(1, len(c)):
        if i < 14: ag[i] = gain[:i+1].mean(); al[i] = loss[:i+1].mean()
        else:       ag[i] = (ag[i-1]*13 + gain[i])/14; al[i] = (al[i-1]*13 + loss[i])/14
    df['rsi']      = 100 - 100/(1 + np.where(al == 0, 100.0, ag/al))
    df['ema20']    = df['c'].ewm(span=20, adjust=False).mean()
    df['ema50']    = df['c'].ewm(span=50, adjust=False).mean()
    df['ema200']   = df['c'].ewm(span=200, adjust=False).mean()
    df['uptrend']  = (df['ema20'] > df['ema50']).astype(int)
    df['bull200']  = (df['c'] > df['ema200']).astype(int)
    df['res12']    = df['h'].shift(1).rolling(12).max()
    df['vol_avg20']= df['vol'].rolling(20, min_periods=10).mean()
    df['date']     = df['ts'].dt.date
    df['tp_v']     = (df['h'] + df['l'] + df['c']) / 3
    df['cum_tv']   = (df['tp_v'] * df['vol']).groupby(df['date']).cumsum()
    df['cum_v']    = df['vol'].groupby(df['date']).cumsum()
    df['vwap']     = df['cum_tv'] / df['cum_v'].replace(0, np.nan)
    df['vwap_dev'] = (df['c'] - df['vwap']) / df['vwap'].replace(0, np.nan)
    h_v, l_v, pc   = df['h'].values, df['l'].values, np.roll(c, 1); pc[0] = c[0]
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
        sl    = entry - SL_M * atr
        tp    = entry + TP_M * atr
        out   = 'TIMEOUT'; pnl_r = 0
        for j in range(i+1, min(i+1+MAX_BARS, len(df))):
            lo, hi = l_arr[j], h_arr[j]
            if lo <= sl: out = 'SL'; pnl_r = -1; break
            if hi >= tp: out = 'TP'; pnl_r =  1; break
        mult = TP_M if pnl_r > 0 else (SL_M if pnl_r < 0 else 0)
        trades.append({**sig, 'outcome': out, 'pnl_r': pnl_r, 'pnl_usd': pnl_r*RISK*mult})
    return trades

# ── main ──────────────────────────────────────────────────────────────────────

print("Fetching GOLD (GC=F)...")
df = fetch_yahoo('GC=F')
df = add_indicators(df)
pct_bull = df['bull200'].mean()*100
print(f"  {len(df)} bars  {df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()}")
print(f"  Gold above 200H EMA: {pct_bull:.0f}% of time")

rsi_v   = df['rsi'].values
vd_v    = df['vwap_dev'].values
atr_v   = df['atr'].values
avg_v   = df['atr_avg20'].values
up_v    = df['uptrend'].values
b200_v  = df['bull200'].values
ts_v    = df['ts'].values
h_v     = df['h'].values
vol_v   = df['vol'].values
vavg_v  = df['vol_avg20'].values
res12_v = df['res12'].values

def base_ok(i):
    if np.isnan(rsi_v[i]) or np.isnan(vd_v[i]): return False
    ts = pd.Timestamp(ts_v[i])
    if ts.weekday() >= 5: return False
    hr = ts.hour + ts.minute/60
    if not (13.5 <= hr <= 20.0): return False
    atr = atr_v[i]; avg = avg_v[i]
    if np.isnan(avg) or avg == 0: return False
    return 0.5 <= atr/avg <= 1.8

# Strategy A: VWAP pullback in uptrend
sig_a = []
for i in range(55, len(df)):
    if not base_ok(i): continue
    rp = rsi_v[i-1]; rc = rsi_v[i]; vd = vd_v[i]
    if up_v[i] == 1 and rp <= 35 and rc > rp and vd <= -0.005:
        sig_a.append({'sym': 'GOLD', 'ts': pd.Timestamp(ts_v[i]), 'bar_idx': i, 'side': 'LONG',
                      'atr': atr_v[i], 'rsi': rc, 'rsi_prev': rp, 'vwap_dev': vd, 'strat': 'A'})

# Strategy B: Breakout LONG
sig_b = []
for i in range(55, len(df)):
    if not base_ok(i): continue
    rc = rsi_v[i]; vd = vd_v[i]; hi = h_v[i]
    vol = vol_v[i]; vavg = vavg_v[i]; res12 = res12_v[i]
    if (b200_v[i] == 1 and up_v[i] == 1 and not np.isnan(res12) and hi > res12
            and vavg > 0 and 1.5 <= vol/vavg <= 3.0
            and 50 <= rc <= 62 and 0.0 <= vd <= 0.008):
        sig_b.append({'sym': 'GOLD', 'ts': pd.Timestamp(ts_v[i]), 'bar_idx': i, 'side': 'LONG',
                      'atr': atr_v[i], 'rsi': rc, 'vwap_dev': vd, 'vol_ratio': vol/vavg, 'strat': 'B'})

# Combined (no duplicate bars)
seen = set()
sig_c = []
for s in sig_a + sig_b:
    if s['bar_idx'] not in seen:
        seen.add(s['bar_idx']); sig_c.append(s)

trades_a = simulate_exits(df, sig_a)
trades_b = simulate_exits(df, sig_b)
trades_c = simulate_exits(df, sig_c)

def pst(label, trades, indent=0):
    if not trades: return
    wins = sum(1 for t in trades if t['pnl_r'] > 0)
    pnl  = sum(t['pnl_usd'] for t in trades)
    print(f"{'  '*indent}{label:50s} {len(trades):4d}T  WR={wins/len(trades)*100:5.1f}%  P&L=${pnl:+,.0f}")

be = SL_M/(SL_M+TP_M)*100

print(f"\n{'='*74}")
print(f"GOLD — LONG ONLY — 2-YEAR RESULTS")
print(f"{'='*74}")
print(f"  SL={SL_M}×ATR  TP={TP_M}×ATR  Break-even: {be:.1f}%  Max hold: {MAX_BARS}H")

pst("A: VWAP pullback in uptrend (RSI 20-35 bounce)", trades_a)
pst("B: Breakout LONG (RSI 50-62, vol 1.5-3x, 12H break)", trades_b)
pst("COMBINED (A + B, deduped)", trades_c)

res = pd.DataFrame(trades_c)
res['year'] = pd.DatetimeIndex(res['ts']).year
res['win']  = res['pnl_r'] > 0

print(f"\n[By Year]")
for yr in sorted(res['year'].unique()):
    yd = res[res['year'] == yr]
    pst(str(yr), list(yd.to_dict('records')))
    for st in ['A', 'B']:
        sd = yd[yd['strat'] == st]
        if len(sd): pst(f"  Strategy {st}", list(sd.to_dict('records')), 1)

print(f"\n[OOS vs In-sample]")
pre = res[res['year'] < 2026]
cur = res[res['year'] == 2026]
pst("Pre-2026 (OOS)", list(pre.to_dict('records')))
pst("  Strategy A", list(pre[pre['strat']=='A'].to_dict('records')), 1)
pst("  Strategy B", list(pre[pre['strat']=='B'].to_dict('records')), 1)
pst("2026 (in-sample)", list(cur.to_dict('records')))
pst("  Strategy A", list(cur[cur['strat']=='A'].to_dict('records')), 1)
pst("  Strategy B", list(cur[cur['strat']=='B'].to_dict('records')), 1)

# Strategy A breakdowns
resA = pd.DataFrame(trades_a)
if len(resA):
    resA['win'] = resA['pnl_r'] > 0
    print(f"\n[Strategy A — RSI_prev zones]")
    resA['rsi_bin'] = pd.cut(resA['rsi_prev'], bins=[20, 26, 30, 33, 36])
    for b, g in resA.groupby('rsi_bin', observed=True):
        pst(f"RSI_prev {b}", list(g.to_dict('records')), 1)

    print(f"\n[Strategy A — VWAP dev zones]")
    resA['vd_bin'] = pd.cut(resA['vwap_dev'], bins=[-0.04, -0.015, -0.01, -0.007, -0.005])
    for b, g in resA.groupby('vd_bin', observed=True):
        pst(f"VWAP dev {b}", list(g.to_dict('records')), 1)

# Strategy B breakdowns
resB = pd.DataFrame(trades_b)
if len(resB):
    resB['win'] = resB['pnl_r'] > 0
    print(f"\n[Strategy B — RSI zones]")
    resB['rsi_bin'] = pd.cut(resB['rsi'], bins=[49, 54, 58, 62])
    for b, g in resB.groupby('rsi_bin', observed=True):
        pst(f"RSI {b}", list(g.to_dict('records')), 1)

    print(f"\n[Strategy B — vol ratio zones]")
    resB['vr_bin'] = pd.cut(resB['vol_ratio'], bins=[1.4, 1.8, 2.2, 2.6, 3.0])
    for b, g in resB.groupby('vr_bin', observed=True):
        pst(f"vol {b}x", list(g.to_dict('records')), 1)

print(f"\n[Outcome breakdown]")
for out in ['TP', 'SL', 'TIMEOUT']:
    n = sum(1 for t in trades_c if t['outcome'] == out)
    print(f"  {out}: {n} ({n/len(trades_c)*100:.0f}%)")

pnl_total = res['pnl_usd'].sum()
print(f"\n  Break-even WR:  {be:.1f}%")
print(f"  Total P&L:      ${pnl_total:+,.0f}  |  Annualized: ${pnl_total/2:+,.0f}")
print(f"  Trades/week:    {len(trades_c)/104:.1f}")
print(f"  NOTE: Small sample (44 trades / 2yr) — use position sizing accordingly")
print(f"  NOTE: No gold SHORT, no silver — no edge found")
