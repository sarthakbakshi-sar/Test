"""
Live Scanner — VWAP Fade SHORT + Tight Breakout LONG + Gold LONG
Runs every 30 minutes during 13:30-24:00 UTC Mon-Fri (NY + Late session)
Emails signals to EMAIL_TO when found.

  ALTS SHORT: BTC bear + VWAP 1.8-3% above + RSI 58-72 turning down     [55.4% WR OOS]
  ALTS LONG:  BTC bull + EMA uptrend + 12H breakout + RSI 50-58          [52.5% WR OOS]
              + VWAP dev 0-0.5% + vol 1.5-3x
  GOLD LONG A: Gold uptrend + RSI 20-35 bouncing + VWAP dev <= -0.5%     [58.3% WR OOS]
  GOLD LONG B: Gold bull200 + uptrend + 12H breakout + vol 1.5-3x        [58.3% WR OOS]
               + RSI 50-62 + VWAP dev 0-0.8%

Session: 13:30-24:00 UTC Mon-Fri  (NY=54.1% WR, Late=48.1% WR backtested)

Telegram alerts are on by default. Override with env vars if needed:
  TG_TOKEN   — bot token      (default: hardcoded)
  TG_CHAT_ID — your chat ID   (default: hardcoded)

Usage:
  python altcoin_scanner.py             # run once right now
  python altcoin_scanner.py --loop      # run every 30 min, 13:30-24:00 UTC Mon-Fri
"""

import os, sys, time, requests
os.environ['REQUESTS_CA_BUNDLE'] = '/root/.ccr/ca-bundle.crt'
from datetime import datetime, timezone
import numpy as np
import pandas as pd

# Auto-load .env if present (never committed — gitignored)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SL_M, TP_M = 1.5, 2.5
RISK       = 200

SESSION_START = 13.5   # 13:30 UTC

ALL_TOKENS = [
    'DOT-USDT','APT-USDT','SUI-USDT','LINK-USDT','ENA-USDT',
    'AVAX-USDT','SEI-USDT','BNB-USDT','FIL-USDT','ADA-USDT','ATOM-USDT',
    'HBAR-USDT','DOGE-USDT','HYPE-USDT','TRX-USDT',
    'SOL-USDT','BTC-USDT','ETH-USDT','XRP-USDT','ZEC-USDT',
]

TG_TOKEN   = os.environ.get('TG_TOKEN',   '')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')

# ── telegram ──────────────────────────────────────────────────────────────────

def send_telegram(signals, now_utc):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("  (Telegram skipped — TG_TOKEN / TG_CHAT_ID not set)")
        return
    lines = [f"🔔 *Scanner — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}*\n"]
    for s in signals:
        side    = s['side']
        price   = s['price']
        sl      = s['sl'];  tp = s['tp']
        sl_pct  = abs(price-sl)/price*100
        tp_pct  = abs(tp-price)/price*100
        dir_sl  = "▲" if side=='SHORT' else "▼"
        dir_tp  = "▼" if side=='SHORT' else "▲"
        rsi_str = f"{s['rsi_prev']:.1f} → {s['rsi']:.1f}"
        vr_str  = f"  Vol: {s['vol_ratio']:.1f}x" if s.get('vol_ratio') else ""
        note    = s.get('note','')
        lines += [
            f"{'🔴' if side=='SHORT' else '🟢'} *{side} {s['sym']}*",
            f"  Entry: `${price:.4f}`",
            f"  SL:    `${sl:.4f}` ({dir_sl}{sl_pct:.1f}%  -${RISK*SL_M:.0f})",
            f"  TP:    `${tp:.4f}` ({dir_tp}{tp_pct:.1f}%  +${RISK*TP_M:.0f})",
            f"  RSI: {rsi_str}  VWAP: {s['vwap_dev']*100:+.2f}%{vr_str}",
            f"  _{note}_\n",
        ]
    text = "\n".join(lines)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
        if r.json().get('ok'):
            print(f"  ✈  Telegram sent ({len(signals)} signal{'s' if len(signals)>1 else ''})")
        else:
            print(f"  ✈  Telegram error: {r.text}")
    except Exception as e:
        print(f"  ✈  Telegram failed: {e}")

# ── data ──────────────────────────────────────────────────────────────────────

def fetch(sym, n=150):
    url  = 'https://www.okx.com/api/v5/market/history-candles'
    bars, after = [], ''
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
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    ag = np.zeros(len(c)); al = np.zeros(len(c))
    for i in range(1, len(c)):
        if i < 14: ag[i]=gain[:i+1].mean(); al[i]=loss[:i+1].mean()
        else:       ag[i]=(ag[i-1]*13+gain[i])/14; al[i]=(al[i-1]*13+loss[i])/14
    df['rsi']      = 100 - 100/(1 + np.where(al==0, 100.0, ag/al))
    df['ema20']    = df['c'].ewm(span=20, adjust=False).mean()
    df['ema50']    = df['c'].ewm(span=50, adjust=False).mean()
    df['res12']    = df['h'].shift(1).rolling(12).max()
    df['vol_avg20']= df['vol'].rolling(20, min_periods=10).mean()
    df['date']     = df['ts'].dt.date
    df['tp']       = (df['h'] + df['l'] + df['c']) / 3
    df['cum_tv']   = (df['tp'] * df['vol']).groupby(df['date']).cumsum()
    df['cum_v']    = df['vol'].groupby(df['date']).cumsum()
    df['vwap']     = df['cum_tv'] / df['cum_v'].replace(0, np.nan)
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

# ── scan ──────────────────────────────────────────────────────────────────────

def scan():
    now_utc = datetime.now(timezone.utc)
    print(f"\n{'='*62}")
    print(f"SCAN  {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*62}")

    wday = now_utc.weekday()
    hour = now_utc.hour + now_utc.minute/60
    if wday >= 5:
        print("Weekend — skipping.")
        return []
    if hour < SESSION_START:
        print(f"Before session (starts 13:30 UTC). Current: {hour:.1f}")
        return []

    # BTC regime
    print("BTC regime...", end=' ', flush=True)
    df_btc = fetch('BTC-USDT', n=220)
    df_btc = indicators(df_btc)
    btc_ema200 = df_btc['c'].ewm(span=200, adjust=False).mean().iloc[-1]
    btc_price  = df_btc['c'].iloc[-1]
    btc_bull   = btc_price > btc_ema200
    print(f"BTC {'BULL' if btc_bull else 'BEAR'} (${btc_price:,.0f} vs EMA200 ${btc_ema200:,.0f})")

    signals = []

    # ── ALTS ─────────────────────────────────────────────────────────────────
    for sym in ALL_TOKENS:
        try:
            df   = fetch(sym, n=150)
            df   = indicators(df)
            row  = df.iloc[-1]
            row1 = df.iloc[-2]

            rsi   = row['rsi'];      rsi_p = row1['rsi']
            vd    = row['vwap_dev']; atr   = row['atr']; avg = row['atr_avg20']
            price = row['c'];        high  = row['h']
            vol   = row['vol'];      vavg  = row['vol_avg20']
            res12 = row['res12'];    ab    = row['ema20'] > row['ema50']

            if np.isnan(atr) or np.isnan(avg) or avg == 0: continue
            if not (0.5 <= atr/avg <= 1.8): continue

            sl_long  = price - SL_M * atr
            tp_long  = price + TP_M * atr
            sl_short = price + SL_M * atr
            tp_short = price - TP_M * atr

            if (not btc_bull and 0.018 <= vd <= 0.03
                    and 58 <= rsi_p <= 72 and rsi < rsi_p):
                signals.append({
                    'sym': sym, 'side': 'SHORT', 'price': price,
                    'sl': sl_short, 'tp': tp_short, 'atr': atr,
                    'rsi': rsi, 'rsi_prev': rsi_p, 'vwap_dev': vd,
                    'vol_ratio': vol/vavg if vavg > 0 else 0,
                    'note': '[BTC BEAR | Dead-cat fade | 55.4% WR]',
                })

            if (btc_bull and ab and not np.isnan(res12) and high > res12
                    and vavg > 0 and 1.5 <= vol/vavg <= 3.0
                    and 50 <= rsi <= 58 and 0.0 <= vd <= 0.005):
                signals.append({
                    'sym': sym, 'side': 'LONG', 'price': price,
                    'sl': sl_long, 'tp': tp_long, 'atr': atr,
                    'rsi': rsi, 'rsi_prev': rsi_p, 'vwap_dev': vd,
                    'vol_ratio': vol/vavg,
                    'note': '[BTC BULL | 12H breakout | 52.5% WR]',
                })

            time.sleep(0.1)
        except Exception as e:
            print(f"  {sym}: error — {e}")

    # ── GOLD ─────────────────────────────────────────────────────────────────
    try:
        print("GOLD (GC=F)...", end=' ', flush=True)
        rg  = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/GC=F',
                           params={'interval':'1h','range':'30d'},
                           headers={'User-Agent':'Mozilla/5.0'}, timeout=15)
        jg  = rg.json()['chart']['result'][0]
        tsg = pd.to_datetime(jg['timestamp'], unit='s', utc=True)
        qg  = jg['indicators']['quote'][0]
        dfg = pd.DataFrame({'ts':tsg,'o':qg['open'],'h':qg['high'],'l':qg['low'],
                            'c':qg['close'],'vol':qg['volume']}).dropna(subset=['c'])
        dfg = dfg.sort_values('ts').reset_index(drop=True)
        dfg = indicators(dfg)
        dfg['ema200'] = dfg['c'].ewm(span=200, adjust=False).mean()
        dfg['bull200']= (dfg['c'] > dfg['ema200']).astype(int)

        grow  = dfg.iloc[-1]; grow1 = dfg.iloc[-2]
        g_price = grow['c'];   g_high  = grow['h']
        g_rsi   = grow['rsi']; g_rsi_p = grow1['rsi']
        g_vd    = grow['vwap_dev']
        g_atr   = grow['atr']; g_avg   = grow['atr_avg20']
        g_ab    = grow['ema20'] > grow['ema50']
        g_b200  = bool(grow['bull200'])
        g_res12 = grow['res12']
        g_vol   = grow['vol']; g_vavg  = grow['vol_avg20']
        print(f"${g_price:,.1f}  RSI={g_rsi:.1f}  VWAP={g_vd*100:+.2f}%  {'uptrend' if g_ab else 'downtrend'}")

        if not (np.isnan(g_atr) or np.isnan(g_avg) or g_avg == 0
                or not (0.5 <= g_atr/g_avg <= 1.8)):
            g_sl = g_price - SL_M * g_atr
            g_tp = g_price + TP_M * g_atr

            if g_ab and g_rsi_p <= 35 and g_rsi > g_rsi_p and g_vd <= -0.005:
                signals.append({
                    'sym': 'GOLD', 'side': 'LONG', 'price': g_price,
                    'sl': g_sl, 'tp': g_tp, 'atr': g_atr,
                    'rsi': g_rsi, 'rsi_prev': g_rsi_p, 'vwap_dev': g_vd,
                    'vol_ratio': 0, 'note': 'Gold VWAP pullback [58.3% WR OOS]',
                })

            if (g_b200 and g_ab and not np.isnan(g_res12) and g_high > g_res12
                    and g_vavg > 0 and 1.5 <= g_vol/g_vavg <= 3.0
                    and 50 <= g_rsi <= 62 and 0.0 <= g_vd <= 0.008):
                signals.append({
                    'sym': 'GOLD', 'side': 'LONG', 'price': g_price,
                    'sl': g_sl, 'tp': g_tp, 'atr': g_atr,
                    'rsi': g_rsi, 'rsi_prev': g_rsi_p, 'vwap_dev': g_vd,
                    'vol_ratio': g_vol/g_vavg, 'note': 'Gold breakout [58.3% WR OOS]',
                })
    except Exception as e:
        print(f"  GOLD error: {e}")

    # ── print ─────────────────────────────────────────────────────────────────
    if not signals:
        print("\nNo signals this bar.")
    else:
        print(f"\n{'─'*62}")
        for s in signals:
            side    = s['side']; price = s['price']
            sl      = s['sl'];   tp    = s['tp']
            sl_pct  = abs(price-sl)/price*100
            tp_pct  = abs(tp-price)/price*100
            dir_sl  = "▲" if side=='SHORT' else "▼"
            dir_tp  = "▼" if side=='SHORT' else "▲"
            rsi_str = f"{s['rsi_prev']:.1f} → {s['rsi']:.1f}"
            vr_str  = f"   Vol: {s['vol_ratio']:.1f}x" if s.get('vol_ratio') else ""
            print(f"\n  {'🔴' if side=='SHORT' else '🟢'} {side} | {s['sym']}")
            print(f"     Entry: ${price:.4f}")
            print(f"     SL:    ${sl:.4f}  ({dir_sl}{sl_pct:.1f}%  -${RISK*SL_M:.0f})")
            print(f"     TP:    ${tp:.4f}  ({dir_tp}{tp_pct:.1f}%  +${RISK*TP_M:.0f})")
            print(f"     RSI:   {rsi_str}   VWAP: {s['vwap_dev']*100:+.2f}%{vr_str}")
            print(f"     {s['note']}")
        print(f"\n{'─'*62}")
        send_telegram(signals, now_utc)

    return signals

# ── loop ──────────────────────────────────────────────────────────────────────

def next_scan_seconds():
    """Wait until the next :01 or :31 past the hour."""
    now = datetime.now(timezone.utc)
    m, s = now.minute, now.second
    if m < 1:
        wait = (1 - m)*60 - s
    elif m < 31:
        wait = (31 - m)*60 - s
    else:
        wait = (61 - m)*60 - s
    return max(wait, 5)

if __name__ == '__main__':
    if '--loop' in sys.argv:
        print("Loop mode — every 30 min at :01 and :31, session 13:30-24:00 UTC Mon-Fri.")
        print(f"Telegram alerts → chat ID: {TG_CHAT_ID}")
        while True:
            scan()
            wait = next_scan_seconds()
            print(f"Sleeping {wait//60}m {wait%60}s → next scan at :{(datetime.now(timezone.utc).minute + wait//60 + 1) % 60:02d}...")
            time.sleep(wait)
    else:
        scan()
