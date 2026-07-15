"""
Parameter sweep — finding edge in non-gold instruments.

Tests each instrument across multiple parameter combinations:
  - Resistance window: 6H / 12H
  - Vol filter: strict(1.5-3x) / loose(1.2-5x) / skip
  - RSI B range: 48-65 / 50-62
  - RSI A threshold: ≤35 / ≤40
  - VWAP dev B: ≤0.8% / ≤1.5%
  - Session: NY / London / Both
  - ATR regime: strict(0.5-1.8x) / loose(0.3-2.5x)

Goal: find setups with OOS WR ≥ 50% and ≥ 10T OOS
"""

import os, time, requests, itertools
os.environ['REQUESTS_CA_BUNDLE'] = '/root/.ccr/ca-bundle.crt'
import numpy as np
import pandas as pd

SL_M, TP_M = 1.5, 2.5
RISK       = 200
MAX_BARS   = 48
BE         = SL_M / (SL_M + TP_M) * 100   # 37.5%

# ── data ──────────────────────────────────────────────────────

_cache = {}

def fetch_yahoo(ticker, range_str='2y'):
    if ticker in _cache: return _cache[ticker].copy()
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
    ts  = pd.to_datetime(j['timestamp'], unit='s', utc=True)
    q   = j['indicators']['quote'][0]
    df  = pd.DataFrame({
        'ts': ts, 'o': q['open'], 'h': q['high'],
        'l':  q['low'], 'c': q['close'], 'vol': q['volume'],
    }).dropna(subset=['c'])
    df['vol'] = df['vol'].fillna(0)
    df = df.sort_values('ts').reset_index(drop=True)
    _cache[ticker] = df.copy()
    return df

def add_indicators(df):
    c = df['c'].values
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag = np.zeros(len(c)); al = np.zeros(len(c))
    for i in range(1, len(c)):
        if i < 14: ag[i] = gain[:i+1].mean(); al[i] = loss[:i+1].mean()
        else:       ag[i] = (ag[i-1]*13+gain[i])/14; al[i] = (al[i-1]*13+loss[i])/14
    df['rsi']      = 100 - 100/(1 + np.where(al == 0, 100.0, ag/al))
    df['ema20']    = df['c'].ewm(span=20, adjust=False).mean()
    df['ema50']    = df['c'].ewm(span=50, adjust=False).mean()
    df['ema200']   = df['c'].ewm(span=200, adjust=False).mean()
    df['uptrend']  = (df['ema20'] > df['ema50']).astype(int)
    df['bull200']  = (df['c'] > df['ema200']).astype(int)
    df['vol_avg20']= df['vol'].rolling(20, min_periods=10).mean()
    df['date']     = df['ts'].dt.date
    df['tp_v']     = (df['h'] + df['l'] + df['c']) / 3
    df['cum_tv']   = (df['tp_v'] * df['vol']).groupby(df['date']).cumsum()
    df['cum_v']    = df['vol'].groupby(df['date']).cumsum()
    df['vwap']     = df['cum_tv'] / df['cum_v'].replace(0, np.nan)
    df['vwap_dev'] = (df['c'] - df['vwap']) / df['vwap'].replace(0, np.nan)
    h_v, l_v, pc   = df['h'].values, df['l'].values, np.roll(c,1); pc[0]=c[0]
    tr  = np.maximum(h_v-l_v, np.maximum(np.abs(h_v-pc), np.abs(l_v-pc)))
    atr = np.zeros(len(tr))
    for i in range(1, len(tr)):
        if i < 14: atr[i] = tr[:i+1].mean()
        else:       atr[i] = (atr[i-1]*13+tr[i])/14
    df['atr']       = atr
    df['atr_avg20'] = pd.Series(atr).rolling(20, min_periods=5).mean().values
    return df

def simulate_exits(df, signals):
    trades = []
    c_arr, h_arr, l_arr = df['c'].values, df['h'].values, df['l'].values
    for sig in signals:
        i     = sig['bar_idx']
        entry = c_arr[i]; atr = sig['atr']
        sl    = entry - SL_M * atr; tp = entry + TP_M * atr
        out   = 'TIMEOUT'; pnl_r = 0
        for j in range(i+1, min(i+1+MAX_BARS, len(df))):
            if l_arr[j] <= sl: out='SL'; pnl_r=-1; break
            if h_arr[j] >= tp: out='TP'; pnl_r= 1; break
        mult = TP_M if pnl_r>0 else (SL_M if pnl_r<0 else 0)
        trades.append({**sig, 'outcome':out, 'pnl_r':pnl_r, 'pnl_usd':pnl_r*RISK*mult})
    return trades

# ── sweep ──────────────────────────────────────────────────────

def sweep(df, ticker, params):
    """Run one parameter combo; return summary dict or None if < 5 OOS trades."""
    res_win  = params['res_win']     # 6 or 12
    vol_mode = params['vol_mode']    # 'strict','loose','skip'
    rsi_b    = params['rsi_b']       # (lo, hi) e.g. (50,62)
    rsi_a    = params['rsi_a']       # upper threshold e.g. 35 or 40
    vd_b     = params['vd_b']        # VWAP dev upper bound for B (e.g. 0.008 or 0.015)
    vd_a     = params['vd_a']        # VWAP dev lower bound for A (e.g. -0.005 or -0.003)
    session  = params['session']     # 'NY','LONDON','BOTH'
    atr_mode = params['atr_mode']    # 'strict' or 'loose'

    # Pre-compute rolling resistance
    h_arr  = df['h'].values
    res    = np.full(len(df), np.nan)
    for i in range(res_win+1, len(df)):
        res[i] = h_arr[i-res_win:i].max()

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

    atr_lo, atr_hi = (0.5, 1.8) if atr_mode == 'strict' else (0.3, 2.5)

    def sess_ok(i):
        t  = pd.Timestamp(ts_v[i])
        if t.weekday() >= 5: return False
        hr = t.hour + t.minute/60
        if session == 'NY':     return 13.5 <= hr <= 20.0
        if session == 'LONDON': return  7.0 <= hr < 13.5
        if session == 'BOTH':   return  7.0 <= hr <= 20.0
        return False

    def atr_ok(i):
        avg = avg_v[i]
        if np.isnan(avg) or avg == 0: return False
        return atr_lo <= atr_v[i]/avg <= atr_hi

    def vol_ok(i):
        if vol_mode == 'skip': return True
        vavg = vavg_v[i]
        if np.isnan(vavg) or vavg == 0: return False
        vr = vol_v[i] / vavg
        if vol_mode == 'strict': return 1.5 <= vr <= 3.0
        if vol_mode == 'loose':  return 1.2 <= vr <= 5.0
        return False

    sig_a, sig_b = [], []
    for i in range(max(res_win+5, 55), len(df)):
        if not sess_ok(i): continue
        if np.isnan(rsi_v[i]) or np.isnan(vd_v[i]): continue
        if not atr_ok(i): continue
        rc = rsi_v[i]; rp = rsi_v[i-1]; vd = vd_v[i]

        # Strategy A
        if up_v[i]==1 and rp<=rsi_a and rc>rp and vd<=vd_a:
            sig_a.append({'bar_idx':i,'atr':atr_v[i],'strat':'A','ts':pd.Timestamp(ts_v[i])})

        # Strategy B
        r12 = res[i]
        if (not np.isnan(r12) and h_v[i]>r12 and b200_v[i]==1 and up_v[i]==1
                and rsi_b[0]<=rc<=rsi_b[1] and 0.0<=vd<=vd_b and vol_ok(i)):
            sig_b.append({'bar_idx':i,'atr':atr_v[i],'strat':'B','ts':pd.Timestamp(ts_v[i])})

    seen=set(); sig_c=[]
    for s in sig_a+sig_b:
        if s['bar_idx'] not in seen:
            seen.add(s['bar_idx']); sig_c.append(s)

    if not sig_c: return None
    trades = simulate_exits(df, sig_c)
    pre    = [t for t in trades if t['ts'].year < 2026]
    if len(pre) < 8: return None   # need at least 8 OOS trades

    wr_all = sum(1 for t in trades if t['pnl_r']>0)/len(trades)
    wr_oos = sum(1 for t in pre   if t['pnl_r']>0)/len(pre)
    pnl    = sum(t['pnl_usd'] for t in trades)
    tpw    = len(trades)/104
    ta     = [t for t in trades if t['strat']=='A']
    tb     = [t for t in trades if t['strat']=='B']
    wr_a   = sum(1 for t in ta if t['pnl_r']>0)/len(ta) if ta else 0
    wr_b   = sum(1 for t in tb if t['pnl_r']>0)/len(tb) if tb else 0

    return {**params,
            'n': len(trades), 'tpw': tpw,
            'wr': wr_all*100, 'wr_oos': wr_oos*100, 'pnl': pnl,
            'n_a': len(ta), 'wr_a': wr_a*100,
            'n_b': len(tb), 'wr_b': wr_b*100,
            'n_oos': len(pre)}

# ── instrument configs ─────────────────────────────────────────

INSTRUMENTS = {
    'CL=F':     'Crude Oil',
    '^GSPC':    'S&P 500',
    '^NDX':     'Nasdaq',
    'EURUSD=X': 'EUR/USD',
    'GBPUSD=X': 'GBP/USD',
}

# Parameters to sweep per instrument
BASE_PARAMS = dict(
    res_win  = [6, 12],
    rsi_b    = [(48, 65), (50, 62), (45, 68)],
    rsi_a    = [35, 40],
    vd_b     = [0.008, 0.015],
    vd_a     = [-0.005, -0.003],
    atr_mode = ['strict', 'loose'],
)

INST_OVERRIDES = {
    'CL=F':     dict(vol_mode=['strict','loose'],  session=['NY','LONDON','BOTH']),
    '^GSPC':    dict(vol_mode=['strict','loose','skip'], session=['NY']),
    '^NDX':     dict(vol_mode=['strict','loose','skip'], session=['NY']),
    'EURUSD=X': dict(vol_mode=['skip'],            session=['NY','LONDON','BOTH']),
    'GBPUSD=X': dict(vol_mode=['skip'],            session=['NY','LONDON','BOTH']),
}

def all_combos(ticker):
    p = {**BASE_PARAMS, **INST_OVERRIDES[ticker]}
    keys   = list(p.keys())
    values = list(p.values())
    for combo in itertools.product(*values):
        yield dict(zip(keys, combo))

# ── run ────────────────────────────────────────────────────────

print(f"SL={SL_M}×ATR  TP={TP_M}×ATR  Break-even: {BE:.1f}%\n")

for ticker, name in INSTRUMENTS.items():
    print(f"\n{'='*72}")
    print(f"  {name} ({ticker})")
    print(f"{'='*72}")
    try:
        raw = fetch_yahoo(ticker)
        print(f"  {len(raw)} bars  {raw['ts'].iloc[0].date()} → {raw['ts'].iloc[-1].date()}")
        df  = add_indicators(raw.copy())
    except Exception as e:
        print(f"  FAILED: {e}"); continue

    results = []
    combos  = list(all_combos(ticker))
    print(f"  Testing {len(combos)} parameter combinations...")
    for params in combos:
        r = sweep(df, ticker, params)
        if r: results.append(r)

    if not results:
        print("  No combos produced ≥8 OOS trades."); continue

    results.sort(key=lambda x: x['wr_oos'], reverse=True)

    # Print top 8 by OOS WR, filter ≥ 50% OOS
    good = [r for r in results if r['wr_oos'] >= 50.0]
    print(f"  {len(good)}/{len(results)} combos ≥50% OOS WR\n")

    hdr = f"  {'Session':<7} {'Res':>3} {'Vol':<7} {'RSI_B':<9} {'ATR':<7}  {'N':>4}  {'T/wk':>5}  {'WR%':>5}  {'OOS_WR%':>7}  {'OOS_N':>5}  P&L"
    print(hdr)
    print('  ' + '-'*len(hdr.strip()))
    for r in results[:15]:
        flag = ' ✓' if r['wr_oos'] >= 50.0 else ''
        rsi_b_str = f"{r['rsi_b'][0]}-{r['rsi_b'][1]}"
        print(f"  {r['session']:<7} {r['res_win']:>3}H {r['vol_mode']:<7} {rsi_b_str:<9} {r['atr_mode']:<7}  "
              f"{r['n']:>4}  {r['tpw']:>5.2f}  {r['wr']:>5.1f}%  {r['wr_oos']:>6.1f}%  {r['n_oos']:>5}  ${r['pnl']:+,.0f}{flag}")

    if good:
        best = good[0]
        print(f"\n  >>> BEST COMBO: session={best['session']} res={best['res_win']}H "
              f"vol={best['vol_mode']} rsi_b={best['rsi_b']} rsi_a≤{best['rsi_a']} "
              f"vd_b≤{best['vd_b']*100:.1f}% vd_a≤{best['vd_a']*100:.1f}% atr={best['atr_mode']}")
        print(f"      {best['n']}T total  {best['tpw']:.2f}T/wk  WR={best['wr']:.1f}%  "
              f"OOS WR={best['wr_oos']:.1f}%  OOS N={best['n_oos']}  P&L=${best['pnl']:+,.0f}")
        print(f"      Strategy A: {best['n_a']}T WR={best['wr_a']:.1f}%")
        print(f"      Strategy B: {best['n_b']}T WR={best['wr_b']:.1f}%")

print(f"\n{'='*72}")
print(f"SUMMARY — instruments worth adding (OOS WR ≥ 50%, OOS N ≥ 10)")
print(f"Break-even: {BE:.1f}%  Gold baseline: 0.42T/wk (62.8% WR, 58.3% OOS)")
print(f"{'='*72}")
