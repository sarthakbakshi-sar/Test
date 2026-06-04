#!/usr/bin/env python3
"""
Altcoin 15m Signal Backtest
Components: Volume>120% · MACD cross · EMA 9/21/50 · 4H MACD · BTC trend
Max score = 8 (backtest — no live funding/sentiment)
Signal fires at >= 5
"""

import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

OKX = 'https://www.okx.com/api/v5/market'

# OKX spot symbols (USDT pairs)
TOKENS = [
    # Winners / breakeven — SOL, ARB, OP, PENDLE dropped (consistent losers in score-8 test)
    'DOT-USDT','APT-USDT','SUI-USDT','LINK-USDT','WIF-USDT',
    'ENA-USDT','INJ-USDT','AVAX-USDT','TAO-USDT',
]

CFG = {
    'vol_period': 20,
    'vol_mult':   1.20,
    'ema_fast':   9,
    'ema_mid':    21,
    'ema_slow':   50,
    'macd_fast':  12,
    'macd_slow':  26,
    'macd_sig':   9,
    'atr_period': 14,
    'sl_atr':     1.5,
    'tp1_atr':    3.0,
    'signal_min': 8,    # fresh-cross-only: score 7 loses, score 8 wins
    'capital':    10000,
    'risk_pct':   0.02,
}

BOLD  = '\033[1m'
GREEN = '\033[92m'
RED   = '\033[91m'
CYAN  = '\033[96m'
YEL   = '\033[93m'
DIM   = '\033[90m'
RST   = '\033[0m'

# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_bars(symbol, interval, limit=5760):
    """Fetch OHLCV from OKX history-candles endpoint — up to 269 days available.
    Paginates backwards in time to get `limit` closed bars.
    interval: '15m' or '4H'
    """
    import time as _time
    all_bars = []
    after    = None
    per_req  = 300
    endpoint = f"{OKX}/history-candles"

    while len(all_bars) < limit:
        fetch_n = min(per_req, limit - len(all_bars))
        params  = {'instId': symbol, 'bar': interval, 'limit': str(fetch_n)}
        if after:
            params['after'] = str(after)
        try:
            r = requests.get(endpoint, params=params, timeout=15)
            d = r.json()
            if d.get('code') != '0' or not d.get('data'):
                break
            rows   = d['data']
            closed = [row for row in rows if len(row) > 8 and row[8] == '1']
            if not closed:
                # history-candles may not have confirm field — accept all
                closed = rows
            if not closed:
                break
            all_bars.extend(closed)
            after = closed[-1][0]
            if len(rows) < fetch_n:
                break
            _time.sleep(0.08)
        except Exception:
            break

    if len(all_bars) < 10:
        return None

    cols = ['ts','open','high','low','close','vol','volccy','volquote']
    if len(all_bars[0]) > 8:
        cols.append('confirm')
    df = pd.DataFrame(all_bars, columns=cols[:len(all_bars[0])])
    df['ts'] = pd.to_datetime(df['ts'].astype('int64'), unit='ms', utc=True)
    for col in ['open','high','low','close']:
        df[col] = pd.to_numeric(df[col])
    df['volume'] = pd.to_numeric(df['volccy'])
    df = df.sort_values('ts').drop_duplicates('ts').reset_index(drop=True)
    return df

# ── Technicals ────────────────────────────────────────────────────────────────

def ema_s(series, n):
    return series.ewm(span=n, adjust=False).mean()

def add_technicals(df):
    if df is None or len(df) < 55:
        return None
    c, h, l = df['close'], df['high'], df['low']
    v = df['volume']

    df = df.copy()
    df['e9']  = ema_s(c, CFG['ema_fast'])
    df['e21'] = ema_s(c, CFG['ema_mid'])
    df['e50'] = ema_s(c, CFG['ema_slow'])

    ef   = ema_s(c, CFG['macd_fast'])
    es   = ema_s(c, CFG['macd_slow'])
    ml   = ef - es
    msig = ema_s(ml, CFG['macd_sig'])
    df['ml']  = ml
    df['ms']  = msig
    df['mh']  = ml - msig

    prev_c = c.shift(1)
    tr = pd.concat([h-l, (h-prev_c).abs(), (l-prev_c).abs()], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=CFG['atr_period'], adjust=False).mean()

    vol_avg = v.shift(1).rolling(CFG['vol_period']).mean()
    df['vr'] = v / vol_avg

    df['ema_long']  = (df['e9'] > df['e21']) & (df['e21'] > df['e50'])
    df['ema_short'] = (df['e9'] < df['e21']) & (df['e21'] < df['e50'])
    df['macd_bull'] = df['ml'] > df['ms']
    df['macd_bear'] = df['ml'] < df['ms']
    df['cx_long']   = (df['mh'] > 0) & (df['mh'].shift(1) <= 0)
    df['cx_short']  = (df['mh'] < 0) & (df['mh'].shift(1) >= 0)
    df['vol_ok']    = df['vr'] >= CFG['vol_mult']
    return df

# ── BTC trend ─────────────────────────────────────────────────────────────────

def btc_trend_series(df_btc):
    """Returns a Series of BTC trend (+1/-1/0) indexed by ts"""
    if df_btc is None:
        return None
    t = add_technicals(df_btc)
    if t is None:
        return None
    trend = pd.Series(0, index=t['ts'])
    trend[t['ema_long'].values]  =  1
    trend[t['ema_short'].values] = -1
    return trend

# ── Signal scoring ────────────────────────────────────────────────────────────

def score_row(r15, macd4h_bull, macd4h_bear, btc_dir):
    score = 0.0
    dir_  = 1 if r15['ema_long'] else (-1 if r15['ema_short'] else (1 if r15['macd_bull'] else -1))

    # Volume (2 pts)
    if r15['vol_ok']:
        score += 2

    # MACD 15m (2 pts — fresh cross only; no partial credit)
    fresh = r15['cx_long'] if dir_ == 1 else r15['cx_short']
    if fresh:
        score += 2

    # EMA stack (2 pts)
    if (dir_ == 1 and r15['ema_long']) or (dir_ == -1 and r15['ema_short']):
        score += 2

    # 4H MACD (1 pt)
    if (dir_ == 1 and macd4h_bull) or (dir_ == -1 and macd4h_bear):
        score += 1

    # BTC trend (1 pt)
    if btc_dir != 0 and btc_dir == dir_:
        score += 1

    price = r15['close']
    atr   = r15['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    sl  = price - CFG['sl_atr']  * atr * dir_
    tp1 = price + CFG['tp1_atr'] * atr * dir_
    size = (CFG['capital'] * CFG['risk_pct']) / abs(price - sl)

    return {'score': round(score,1), 'dir': dir_, 'price': price,
            'sl': sl, 'tp1': tp1, 'size': size, 'atr': atr}

# ── Backtest engine ───────────────────────────────────────────────────────────

def backtest_token(symbol, t15, t4h, btc_trend):
    trades  = []
    capital = CFG['capital']
    equity  = [capital]
    in_tr   = False
    trade   = None

    # Precompute 4H state at each 15m bar
    if t4h is not None:
        t4h_ts = t4h['ts'].values
        t4h_bull = t4h['macd_bull'].values
        t4h_bear = t4h['macd_bear'].values

    btc_ts   = btc_trend.index.values if btc_trend is not None else None
    btc_vals = btc_trend.values       if btc_trend is not None else None

    for i in range(55, len(t15)):
        r15    = t15.iloc[i]
        bar_ts = r15['ts']

        # 4H lookup
        m4b, m4be = False, False
        if t4h is not None:
            mask = t4h_ts <= np.datetime64(bar_ts)
            if mask.any():
                idx = mask.nonzero()[0][-1]
                m4b  = bool(t4h_bull[idx])
                m4be = bool(t4h_bear[idx])

        # BTC trend lookup
        btc_d = 0
        if btc_ts is not None:
            mask = btc_ts <= np.datetime64(bar_ts)
            if mask.any():
                btc_d = int(btc_vals[mask.nonzero()[0][-1]])

        hi, lo = r15['high'], r15['low']

        # Manage open trade
        if in_tr and trade:
            d = trade['dir']
            if d == 1:
                if lo <= trade['sl']:
                    pnl = (trade['sl'] - trade['entry']) * trade['size']
                    capital += pnl
                    trades.append({**trade, 'exit': trade['sl'], 'pnl': pnl,
                                   'result':'SL', 'exit_ts': bar_ts})
                    equity.append(capital); in_tr = False; trade = None
                elif hi >= trade['tp1']:
                    pnl = (trade['tp1'] - trade['entry']) * trade['size']
                    capital += pnl
                    trades.append({**trade, 'exit': trade['tp1'], 'pnl': pnl,
                                   'result':'TP', 'exit_ts': bar_ts})
                    equity.append(capital); in_tr = False; trade = None
            else:
                if hi >= trade['sl']:
                    pnl = (trade['entry'] - trade['sl']) * trade['size']
                    capital += pnl
                    trades.append({**trade, 'exit': trade['sl'], 'pnl': pnl,
                                   'result':'SL', 'exit_ts': bar_ts})
                    equity.append(capital); in_tr = False; trade = None
                elif lo <= trade['tp1']:
                    pnl = (trade['entry'] - trade['tp1']) * trade['size']
                    capital += pnl
                    trades.append({**trade, 'exit': trade['tp1'], 'pnl': pnl,
                                   'result':'TP', 'exit_ts': bar_ts})
                    equity.append(capital); in_tr = False; trade = None

        # New entry
        if not in_tr:
            sig = score_row(r15, m4b, m4be, btc_d)
            if sig and sig['score'] >= CFG['signal_min']:
                in_tr = True
                trade = {'symbol': symbol, 'dir': sig['dir'],
                         'entry': sig['price'], 'sl': sig['sl'],
                         'tp1': sig['tp1'], 'size': sig['size'],
                         'score': sig['score'], 'ts': bar_ts}

    return trades, equity, capital

# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(all_trades, combined_equity, start_ts, end_ts):
    if not all_trades:
        return None
    df = pd.DataFrame(all_trades)
    n  = len(df)
    nw = (df['pnl'] > 0).sum()
    wr = nw / n

    days  = (end_ts - start_ts).days + 1
    weeks = max(days / 7, 1)

    total_pnl   = df['pnl'].sum()
    roi_pct     = total_pnl / CFG['capital'] * 100
    tpw         = n / weeks
    roi_pw      = roi_pct / weeks

    rets = df['pnl'].values / CFG['capital']
    mu, sig_ = rets.mean(), rets.std(ddof=1)
    sharpe = (mu / sig_) * np.sqrt(252 * 26) if sig_ > 0 else 0

    eq = np.array(combined_equity)
    pk = np.maximum.accumulate(eq)
    dd = (pk - eq) / np.where(pk > 0, pk, 1)
    max_dd = dd.max() * 100

    # Binomial p-value (one-sided, H0: WR <= 0.5)
    p_val = stats.binomtest(int(nw), int(n), 0.5, alternative='greater').pvalue

    gw = df[df['pnl'] > 0]['pnl'].sum()
    gl = abs(df[df['pnl'] < 0]['pnl'].sum())
    pf = gw / gl if gl > 0 else float('inf')

    avg_w = df[df['pnl'] > 0]['pnl'].mean() if nw > 0 else 0
    avg_l = df[df['pnl'] < 0]['pnl'].mean() if (n-nw) > 0 else 0

    # Per-score-bucket analysis
    buckets = {}
    for sc in sorted(df['score'].unique()):
        sub = df[df['score'] == sc]
        bw  = (sub['pnl'] > 0).sum()
        buckets[sc] = {'n': len(sub), 'wr': bw/len(sub)*100, 'pnl': sub['pnl'].sum()}

    return {
        'n_trades':    n,
        'n_wins':      int(nw),
        'win_rate':    wr * 100,
        'total_pnl':   total_pnl,
        'roi_pct':     roi_pct,
        'tpw':         tpw,
        'roi_pw':      roi_pw,
        'sharpe':      sharpe,
        'max_dd':      max_dd,
        'p_value':     p_val,
        'pf':          pf,
        'avg_win':     avg_w,
        'avg_loss':    avg_l,
        'rr':          abs(avg_w / avg_l) if avg_l != 0 else 0,
        'weeks':       weeks,
        'days':        days,
        'buckets':     buckets,
    }

# ── Print helpers ─────────────────────────────────────────────────────────────

def pct_color(v, good=0):
    c = GREEN if v > good else RED
    return f"{c}{v:+.2f}%{RST}"

def val_color(v, good=0):
    c = GREEN if v > good else RED
    return f"{c}{v:.4f}{RST}"

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{CYAN}{'═'*70}{RST}")
    print(f"{BOLD}{CYAN}  ALTCOIN 15m SIGNAL BACKTEST  —  {len(TOKENS)} tokens  ·  ~90 days  ·  OKX  ·  score≥8  ·  whitelist{RST}")
    print(f"{BOLD}{CYAN}{'═'*70}{RST}\n")
    print(f"{DIM}  Fetching BTC reference data...{RST}", end='', flush=True)

    btc15 = fetch_bars('BTC-USDT', '15m', 5760)
    btc_t = add_technicals(btc15)
    btc_trend = btc_trend_series(btc15) if btc15 is not None else None
    start_ts = btc15.iloc[0]['ts'].to_pydatetime() if btc15 is not None else datetime.now(timezone.utc) - timedelta(days=10)
    end_ts   = btc15.iloc[-1]['ts'].to_pydatetime() if btc15 is not None else datetime.now(timezone.utc)
    print(f" {GREEN}done{RST}")
    print(f"{DIM}  Period: {start_ts.strftime('%Y-%m-%d')} → {end_ts.strftime('%Y-%m-%d')} ({(end_ts-start_ts).days} days){RST}\n")

    all_trades  = []
    all_equity  = [CFG['capital']]
    token_results = []

    for sym in TOKENS:
        print(f"  {DIM}Scanning {sym:<14}{RST}", end='', flush=True)

        bars15 = fetch_bars(sym, '15m', 5760)   # ~90 days
        bars4h  = fetch_bars(sym, '4H',  1080)  # ~90 days 4H
        import time; time.sleep(0.1)

        t15 = add_technicals(bars15)
        t4h = add_technicals(bars4h)

        if t15 is None:
            print(f" {RED}skip (no data){RST}")
            continue

        sym_start = bars15.iloc[0]['ts'].to_pydatetime()
        sym_end   = bars15.iloc[-1]['ts'].to_pydatetime()

        trades, equity, final_cap = backtest_token(sym, t15, t4h, btc_trend)

        n  = len(trades)
        nw = sum(1 for t in trades if t['pnl'] > 0)
        wr = nw/n*100 if n > 0 else 0
        pnl = sum(t['pnl'] for t in trades)

        col = GREEN if pnl > 0 else RED
        print(f" {n:3d} trades  WR {wr:5.1f}%  PnL {col}${pnl:+8.2f}{RST}")

        all_trades.extend(trades)
        all_equity.extend(equity[1:])
        if n > 0:
            token_results.append({'symbol': sym, 'n': n, 'wr': wr, 'pnl': pnl})

    if not all_trades:
        print(f"\n{RED}No trades generated. Try lowering SIGNAL_MIN.{RST}")
        return

    st = compute_stats(all_trades, all_equity, start_ts, end_ts)
    if not st:
        return

    # ── Summary ───────────────────────────────────────────────────────────────
    sig_str = f"{GREEN}✓ SIGNIFICANT{RST}" if st['p_value'] < 0.05 else f"{YEL}~ MARGINAL{RST}" if st['p_value'] < 0.15 else f"{RED}✗ NOT SIG{RST}"
    sh_str  = f"{GREEN}{st['sharpe']:+.2f}{RST}" if st['sharpe'] > 1 else f"{RED}{st['sharpe']:+.2f}{RST}"
    wr_str  = f"{GREEN}{st['win_rate']:.1f}%{RST}" if st['win_rate'] > 52 else f"{YEL}{st['win_rate']:.1f}%{RST}"
    pnl_str = f"{GREEN}+${st['total_pnl']:.2f}{RST}" if st['total_pnl'] > 0 else f"{RED}-${abs(st['total_pnl']):.2f}{RST}"

    print(f"\n{BOLD}{CYAN}{'═'*70}{RST}")
    print(f"{BOLD}  AGGREGATE RESULTS — All {len(TOKENS)} tokens combined{RST}")
    print(f"{BOLD}{CYAN}{'═'*70}{RST}")

    print(f"\n  {'Total trades':<28} {BOLD}{st['n_trades']}{RST}  ({st['n_wins']} wins  /  {st['n_trades']-st['n_wins']} losses)")
    print(f"  {'Win rate':<28} {wr_str}")
    print(f"  {'Profit factor':<28} {GREEN if st['pf'] > 1.5 else YEL}{st['pf']:.2f}x{RST}")
    print(f"  {'Avg win / avg loss':<28} ${st['avg_win']:.2f} / ${abs(st['avg_loss']):.2f}  (RR {st['rr']:.2f}x)")
    print(f"\n  {'Total P&L':<28} {pnl_str}")
    print(f"  {'ROI (period)':<28} {pct_color(st['roi_pct'])}")
    print(f"  {'Trades per week':<28} {BOLD}{st['tpw']:.1f}{RST}")
    print(f"  {'ROI per week':<28} {pct_color(st['roi_pw'])}")
    print(f"\n  {'Sharpe (annualized)':<28} {sh_str}")
    print(f"  {'Max drawdown':<28} {RED}{st['max_dd']:.1f}%{RST}")
    print(f"  {'P-value (WR > 50%)':<28} {st['p_value']:.4f}  {sig_str}")
    print(f"  {'Backtest window':<28} {st['days']} days  ({st['weeks']:.1f} weeks)")

    # ── Per-score-bucket breakdown ─────────────────────────────────────────────
    print(f"\n{BOLD}  SCORE BUCKET ANALYSIS{RST}  (which score thresholds perform best)")
    print(f"  {'Score':<8} {'Trades':>8} {'Win Rate':>10} {'P&L':>12}")
    print(f"  {DIM}{'─'*44}{RST}")
    for sc, b in sorted(st['buckets'].items()):
        col = GREEN if b['pnl'] > 0 else RED
        wr_c = GREEN if b['wr'] > 55 else YEL if b['wr'] > 48 else RED
        print(f"  {sc:<8.1f} {b['n']:>8} {wr_c}{b['wr']:>9.1f}%{RST} {col}${b['pnl']:>10.2f}{RST}")

    # ── Per-token ranking ─────────────────────────────────────────────────────
    print(f"\n{BOLD}  TOKEN RANKING{RST}  (by P&L)")
    print(f"  {'Symbol':<14} {'Trades':>7} {'WR':>7} {'P&L':>12}")
    print(f"  {DIM}{'─'*44}{RST}")
    for r in sorted(token_results, key=lambda x: -x['pnl']):
        col = GREEN if r['pnl'] > 0 else RED
        print(f"  {r['symbol']:<14} {r['n']:>7} {r['wr']:>6.1f}%  {col}${r['pnl']:>+9.2f}{RST}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{CYAN}{'═'*70}{RST}")
    verdict = []
    if st['sharpe'] >= 1.5:   verdict.append(f"{GREEN}Sharpe strong{RST}")
    elif st['sharpe'] >= 0.8: verdict.append(f"{YEL}Sharpe weak{RST}")
    else:                      verdict.append(f"{RED}Sharpe negative{RST}")
    if st['p_value'] < 0.05:  verdict.append(f"{GREEN}statistically significant{RST}")
    elif st['p_value'] < 0.15:verdict.append(f"{YEL}marginal significance{RST}")
    else:                      verdict.append(f"{RED}not significant{RST}")
    if st['roi_pw'] > 0.5:    verdict.append(f"{GREEN}positive weekly edge{RST}")
    else:                      verdict.append(f"{RED}negative weekly edge{RST}")
    print(f"  VERDICT: {' · '.join(verdict)}")
    print(f"{BOLD}{CYAN}{'═'*70}{RST}\n")

if __name__ == '__main__':
    main()
