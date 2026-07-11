"""
Live Scanner — VWAP Fade SHORT + Tight Breakout LONG
Run every hour at :01 past the hour during NY session (13:30-20:00 UTC) Mon-Fri

  SHORT: BTC bear (below 200H EMA) + price 1.8-3% above VWAP + RSI 58-72 turning down
  LONG:  BTC bull (above 200H EMA) + alt EMA20>EMA50 + 12H resistance break
         + RSI 50-58 + VWAP dev 0-0.5% + volume 1.5-3x avg

Usage:
  python scanner.py           # run once right now
  python scanner.py --loop    # run every hour at :01 automatically
"""

import sys, time, requests
from datetime import datetime, timezone
import numpy as np
import pandas as pd

SL_M, TP_M = 1.5, 2.5
RISK       = 200   # USD risk per trade

ALL_TOKENS = [
    'DOT-USDT','APT-USDT','SUI-USDT','LINK-USDT','ENA-USDT',
    'AVAX-USDT','SEI-USDT','BNB-USDT','FIL-USDT','ADA-USDT','ATOM-USDT',
    'HBAR-USDT','DOGE-USDT','HYPE-USDT','TRX-USDT',
]

# ── data ─────────────────────────────────────────────────────────────────────

def fetch(sym, n=150):
    url  = 'https://www.okx.com/api/v5/market/history-candles'
    bars = []
    after = ''
    while len(bars) < n:
        p = {'instId': sym, 'bar': '1H', 'limit': '100'}
        if after: p['after'] = after
        try:
            d = requests.get(url, params=p, timeout=10).json().get('data', [])
        except Exception:
            time.sleep(1); continue
        if not d: break
        bars.extend(d)
        if len(bars) >= n: break
        after = d[-1][0]
        time.sleep(0.1)
    df = pd.DataFrame(bars[:n], columns=['ts','o','h','l','c','vol','volCcy','volCcyQuote','confirm'])
    df['ts'] = pd.to_datetime(df['ts'].astype(np.int64), unit='ms', utc=True)
    for col in ['o','h','l','c','vol']: df[col] = df[col].astype(float)
    return df.sort_values('ts').reset_index(drop=True)

def indicators(df):
    c = df['c'].values
    # RSI-14
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag = np.zeros(len(c)); al = np.zeros(len(c))
    for i in range(1, len(c)):
        if i < 14: ag[i]=gain[:i+1].mean(); al[i]=loss[:i+1].mean()
        else:       ag[i]=(ag[i-1]*13+gain[i])/14; al[i]=(al[i-1]*13+loss[i])/14
    df['rsi'] = 100 - 100/(1 + np.where(al==0, 100.0, ag/al))

    df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    df['res12'] = df['h'].shift(1).rolling(12).max()
    df['vol_avg20'] = df['vol'].rolling(20, min_periods=10).mean()

    df['date']   = df['ts'].dt.date
    df['tp']     = (df['h'] + df['l'] + df['c']) / 3
    df['cum_tv'] = (df['tp'] * df['vol']).groupby(df['date']).cumsum()
    df['cum_v']  = df['vol'].groupby(df['date']).cumsum()
    df['vwap']   = df['cum_tv'] / df['cum_v'].replace(0, np.nan)
    df['vwap_dev'] = (df['c'] - df['vwap']) / df['vwap'].replace(0, np.nan)

    h, l, pc = df['h'].values, df['l'].values, np.roll(c, 1); pc[0] = c[0]
    tr  = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(len(tr))
    for i in range(1, len(tr)):
        if i < 14: atr[i] = tr[:i+1].mean()
        else:       atr[i] = (atr[i-1]*13+tr[i])/14
    df['atr']       = atr
    df['atr_avg20'] = pd.Series(atr).rolling(20, min_periods=5).mean().values
    return df

# ── scan ─────────────────────────────────────────────────────────────────────

def scan():
    now_utc = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"SCAN  {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    # Check session
    wday = now_utc.weekday()
    hour = now_utc.hour + now_utc.minute/60
    if wday >= 5:
        print("Weekend — no NY session. Exiting.")
        return []
    if not (13.0 <= hour <= 20.5):
        print(f"Outside NY session (13:30-20:00 UTC). Current hour: {hour:.1f}")
        return []

    # BTC regime
    print("BTC regime...", end=' ', flush=True)
    df_btc = fetch('BTC-USDT', n=220)
    df_btc = indicators(df_btc)
    btc_ema200 = df_btc['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    btc_price  = df_btc['c'].iloc[-1]
    btc_bull   = btc_price > btc_ema200
    regime_str = f"BTC {'BULL' if btc_bull else 'BEAR'} (${btc_price:,.0f} vs EMA200 ${btc_ema200:,.0f})"
    print(regime_str)

    signals = []

    for sym in ALL_TOKENS:
        try:
            df = fetch(sym, n=150)
            df = indicators(df)
            row  = df.iloc[-1]   # most recent CLOSED bar
            row1 = df.iloc[-2]   # bar before

            rsi   = row['rsi']
            rsi_p = row1['rsi']
            vd    = row['vwap_dev']
            atr   = row['atr']
            avg   = row['atr_avg20']
            price = row['c']
            high  = row['h']
            vol   = row['vol']
            vavg  = row['vol_avg20']
            res12 = row['res12']
            ab    = row['ema20'] > row['ema50']

            if np.isnan(atr) or np.isnan(avg) or avg == 0: continue
            if not (0.5 <= atr/avg <= 1.8): continue

            sl_long  = price - SL_M * atr
            tp_long  = price + TP_M * atr
            sl_short = price + SL_M * atr
            tp_short = price - TP_M * atr

            # ── SHORT ────────────────────────────────────────────────────────
            if (not btc_bull
                    and 0.018 <= vd <= 0.03
                    and 58 <= rsi_p <= 72
                    and rsi < rsi_p):
                signals.append({
                    'sym': sym, 'side': 'SHORT', 'price': price,
                    'sl': sl_short, 'tp': tp_short, 'atr': atr,
                    'rsi': rsi, 'rsi_prev': rsi_p, 'vwap_dev': vd,
                    'vol_ratio': vol/vavg if vavg > 0 else 0,
                })

            # ── LONG ─────────────────────────────────────────────────────────
            if (btc_bull
                    and ab
                    and not np.isnan(res12)
                    and high > res12
                    and vavg > 0
                    and 1.5 <= vol/vavg <= 3.0
                    and 50 <= rsi <= 58
                    and 0.0 <= vd <= 0.005):
                signals.append({
                    'sym': sym, 'side': 'LONG', 'price': price,
                    'sl': sl_long, 'tp': tp_long, 'atr': atr,
                    'rsi': rsi, 'rsi_prev': rsi_p, 'vwap_dev': vd,
                    'vol_ratio': vol/vavg,
                })

            time.sleep(0.1)
        except Exception as e:
            print(f"  {sym}: error — {e}")
            continue

    # ── output ───────────────────────────────────────────────────────────────
    if not signals:
        print("\nNo signals this bar.")
    else:
        print(f"\n{'':─<60}")
        for s in signals:
            side     = s['side']
            sym      = s['sym']
            price    = s['price']
            sl       = s['sl']
            tp       = s['tp']
            sl_pct   = abs(price - sl) / price * 100
            tp_pct   = abs(tp - price) / price * 100
            tag      = "SHORT" if side == 'SHORT' else "LONG "
            dir_sl   = "▲" if side == 'SHORT' else "▼"
            dir_tp   = "▼" if side == 'SHORT' else "▲"

            print(f"\n  {'🔴' if side=='SHORT' else '🟢'} {tag} | {sym:<12}")
            print(f"     Entry: ${price:.4f}")
            print(f"     SL:    ${sl:.4f}  ({dir_sl}{sl_pct:.1f}%  -${RISK*SL_M:.0f})")
            print(f"     TP:    ${tp:.4f}  ({dir_tp}{tp_pct:.1f}%  +${RISK*TP_M:.0f})")
            print(f"     RSI:   {s['rsi_prev']:.1f} → {s['rsi']:.1f}   VWAP dev: {s['vwap_dev']*100:+.2f}%   Vol: {s['vol_ratio']:.1f}x avg")
            if side == 'LONG':
                print(f"     [BTC BULL | Alt uptrend | 12H breakout + volume]")
            else:
                print(f"     [BTC BEAR | Dead-cat fade]")
        print(f"\n{'':─<60}")

    print(f"\nNext scan: top of next hour (:01 past)")
    return signals

# ── loop mode ────────────────────────────────────────────────────────────────

def next_scan_seconds():
    now = datetime.now(timezone.utc)
    # Fire at :01 past each hour
    target_min = 1
    secs_past  = (now.minute - target_min) * 60 + now.second
    if secs_past < 0:
        wait = -secs_past
    else:
        wait = 3600 - secs_past
    return max(wait, 5)

if __name__ == '__main__':
    if '--loop' in sys.argv:
        print("Loop mode — scanning every hour at :01 past. Ctrl+C to stop.")
        while True:
            scan()
            wait = next_scan_seconds()
            print(f"Sleeping {wait//60}m {wait%60}s until next scan...")
            time.sleep(wait)
    else:
        scan()
