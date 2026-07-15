"""
Instrument backtest — applying Gold A/B strategy logic to other instruments
Tests 2Y of 1H data on: Crude Oil, Natural Gas, S&P 500, Nasdaq, EUR/USD

Strategy A: VWAP pullback — RSI ≤35 bounce, VWAP ≤-0.5%, EMA20>EMA50 (uptrend)
Strategy B: 12H breakout  — price > 12H high, vol 1.5-3x, RSI 50-62, VWAP 0-0.8%,
                             EMA20>EMA50, price > 200H EMA

Session filter: Mon-Fri 13:30-20:00 UTC (NY session) for all instruments.
               EUR/USD also tested with London session (07:00-13:30 UTC).

SL=1.5×ATR, TP=2.5×ATR, Max hold=48H, Break-even WR=37.5%
"""

import os, time, requests
os.environ['REQUESTS_CA_BUNDLE'] = '/root/.ccr/ca-bundle.crt'
import numpy as np
import pandas as pd

SL_M, TP_M = 1.5, 2.5
RISK       = 200
MAX_BARS   = 48

INSTRUMENTS = [
    ('CL=F',    'Crude Oil',   'NY'),
    ('NG=F',    'Nat Gas',     'NY'),
    ('^GSPC',   'S&P 500',     'NY'),
    ('^NDX',    'Nasdaq',      'NY'),
    ('EURUSD=X','EUR/USD',     'BOTH'),  # test NY + London
    ('GC=F',    'Gold',        'NY'),    # baseline for comparison
]

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
        'ts':  ts, 'o': q['open'], 'h': q['high'],
        'l':   q['low'], 'c': q['close'], 'vol': q['volume'],
    }).dropna(subset=['c'])
    df['vol'] = df['vol'].fillna(0)
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
        trades.append({**sig, 'outcome': out, 'pnl_r': pnl_r,
                       'pnl_usd': pnl_r * RISK * mult})
    return trades

def has_vol(df):
    return df['vol'].sum() > 100  # FX has near-zero volume

def pst(label, trades, indent=0):
    if not trades: return
    wins = sum(1 for t in trades if t['pnl_r'] > 0)
    pnl  = sum(t['pnl_usd'] for t in trades)
    tpw  = len(trades) / 104
    print(f"{'  '*indent}{label:52s} {len(trades):4d}T  {tpw:.2f}T/wk  WR={wins/len(trades)*100:5.1f}%  P&L=${pnl:+,.0f}")

def run_backtest(ticker, name, session_mode):
    print(f"\nFetching {name} ({ticker})...")
    try:
        df = fetch_yahoo(ticker)
    except Exception as e:
        print(f"  FAILED: {e}"); return

    df = add_indicators(df)
    vol_ok = has_vol(df)
    print(f"  {len(df)} bars  {df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()}  vol={'YES' if vol_ok else 'NO (FX)'}")

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

    def atr_ok(i):
        atr = atr_v[i]; avg = avg_v[i]
        if np.isnan(avg) or avg == 0: return False
        return 0.5 <= atr/avg <= 1.8

    def session_ok(i, session):
        ts = pd.Timestamp(ts_v[i])
        if ts.weekday() >= 5: return False
        hr = ts.hour + ts.minute/60
        if session == 'NY':     return 13.5 <= hr <= 20.0
        if session == 'LONDON': return 7.0 <= hr < 13.5
        if session == 'BOTH':   return 7.0 <= hr <= 20.0
        return False

    sessions = ['NY', 'LONDON'] if session_mode == 'BOTH' else ['NY']

    all_results = {}
    for sess in sessions:
        sig_a, sig_b = [], []
        for i in range(55, len(df)):
            if not session_ok(i, sess): continue
            if np.isnan(rsi_v[i]) or np.isnan(vd_v[i]): continue
            if not atr_ok(i): continue
            rp = rsi_v[i-1]; rc = rsi_v[i]; vd = vd_v[i]

            # Strategy A: VWAP pullback in uptrend
            if up_v[i] == 1 and rp <= 35 and rc > rp and vd <= -0.005:
                sig_a.append({'sym': name, 'ts': pd.Timestamp(ts_v[i]),
                              'bar_idx': i, 'side': 'LONG', 'atr': atr_v[i],
                              'rsi': rc, 'rsi_prev': rp, 'vwap_dev': vd, 'strat': 'A'})

            # Strategy B: Breakout
            hi = h_v[i]; res12 = res12_v[i]
            vol = vol_v[i]; vavg = vavg_v[i]
            vol_cond = (not vol_ok) or (vavg > 0 and 1.5 <= vol/vavg <= 3.0)
            if (b200_v[i] == 1 and up_v[i] == 1 and not np.isnan(res12)
                    and hi > res12 and vol_cond
                    and 50 <= rc <= 62 and 0.0 <= vd <= 0.008):
                sig_b.append({'sym': name, 'ts': pd.Timestamp(ts_v[i]),
                              'bar_idx': i, 'side': 'LONG', 'atr': atr_v[i],
                              'rsi': rc, 'vwap_dev': vd, 'strat': 'B'})

        trades_a = simulate_exits(df, sig_a)
        trades_b = simulate_exits(df, sig_b)
        # Dedup combined
        seen = set(); sig_c = []
        for s in sig_a + sig_b:
            if s['bar_idx'] not in seen:
                seen.add(s['bar_idx']); sig_c.append(s)
        trades_c = simulate_exits(df, sig_c)
        all_results[sess] = (trades_a, trades_b, trades_c)

    print(f"\n  {'='*68}")
    print(f"  {name} ({ticker})")
    print(f"  {'='*68}")
    for sess, (ta, tb, tc) in all_results.items():
        print(f"  [{sess} session]")
        pst(f"  A: VWAP pullback (RSI≤35 bounce, uptrend)", ta, 1)
        pst(f"  B: 12H Breakout  (RSI 50-62, vol 1.5-3x)", tb, 1)
        pst(f"  COMBINED (deduped)",                        tc, 1)
        if tc:
            res = pd.DataFrame(tc)
            res['year'] = pd.DatetimeIndex(res['ts']).year
            print(f"  [By Year]")
            for yr in sorted(res['year'].unique()):
                yd = res[res['year'] == yr]
                pst(f"    {yr}", list(yd.to_dict('records')), 2)
            pre = res[res['year'] < 2026]
            if len(pre):
                wins = sum(1 for t in pre.to_dict('records') if t['pnl_r'] > 0)
                print(f"  OOS (pre-2026): {len(pre)}T  WR={wins/len(pre)*100:.1f}%")

be = SL_M/(SL_M+TP_M)*100
print(f"\nSL={SL_M}×ATR  TP={TP_M}×ATR  Break-even: {be:.1f}%  Max hold: {MAX_BARS}H")
print(f"Vol filter skipped for FX (EUR/USD) — VWAP breakout only\n")

for ticker, name, sess in INSTRUMENTS:
    run_backtest(ticker, name, sess)

print(f"\n{'='*72}")
print(f"SUMMARY — instruments ranked by OOS WR (pre-2026)")
print(f"Break-even: {be:.1f}% | Only consider instruments above 50% WR OOS")
print(f"{'='*72}")
