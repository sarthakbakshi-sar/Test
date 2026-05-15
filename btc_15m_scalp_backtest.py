"""
BTC 15-MIN SCALPING SYSTEM — BACKTEST
======================================
Implements exact same logic as the Pine Script indicator.

5 CONDITIONS (all must be true to enter):
  1. Trend    : EMA 8 > EMA 21 (long) or EMA 8 < EMA 21 (short)
  2. Level    : Price at VWAP / swing high / swing low / round number
  3. Pattern  : Hammer / engulfing / pin bar on trigger candle
  4. Volume   : Above 20-bar average
  5. RSI      : 35-60 for longs / 40-65 for shorts

EXIT:
  TP1 = 1.5x SL distance → close 60% → move stop to breakeven
  TP2 = 2.5x SL distance → close 40%
  Time stop = 3 candles (45 min) if no movement
  Session end = 10pm Dubai (18:00 UTC)

Run: pip install requests pandas numpy scipy yfinance
     python btc_15m_scalp_backtest.py
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, time, timedelta
import time as time_module
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT      = 10000.0
RISK_PCT     = 0.03      # 3% risk per trade (aggressive for your target)
MAX_SL_PCT   = 0.004     # 0.4% max stop loss — skip trade if wider
TP1_MULT     = 1.5       # TP1 = 1.5x SL distance
TP2_MULT     = 2.5       # TP2 = 2.5x SL distance
TP1_CLOSE    = 0.60      # close 60% at TP1
TP2_CLOSE    = 0.40      # close 40% at TP2
TIME_STOP    = 3         # exit after 3 candles if no movement
COMMISSION   = 0.00055   # 0.055% per side (Bybit taker)
EMA_FAST     = 8
EMA_SLOW     = 21
RSI_LEN      = 14
VOL_LEN      = 20
SWING_LEN    = 5         # bars each side for swing high/low
RN_GRID      = 500       # round number grid ($500)
AT_LEVEL_PCT = 0.0012    # within 0.12% = at level
MIN_TREND_SEP= 0.0005    # 0.05% EMA separation = clear trend

# Dubai window: 10am-10pm = 06:00-18:00 UTC
DUBAI_OPEN   = 6
DUBAI_CLOSE  = 18

TD_KEY = "06f050cd7d9940a895f9461e7f0ff7e3"   # your Twelve Data key

print("="*68)
print("  BTC 15-MIN SCALPING SYSTEM — BACKTEST")
print("  5-condition entry | Split exit (60/40) | Dubai window")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_binance(symbol="BTCUSDT", interval="15m", days=365):
    """
    Binance public API — no auth required.
    Paginates backwards to get full history.
    """
    print(f"\nLoading {days} days from Binance ({interval})...")
    url      = "https://api.binance.com/api/v3/klines"
    all_bars = []
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff   = end_ms - days * 86400 * 1000
    page     = 0

    while True:
        try:
            params = {"symbol": symbol, "interval": interval,
                      "limit": 1000, "endTime": end_ms}
            r = requests.get(url, params=params, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"  Binance error {r.status_code}")
                break
            data = r.json()
            if not data:
                break

            for bar in data:
                ts = int(bar[0])
                if ts < cutoff:
                    all_bars.sort(key=lambda x: x[0])
                    print(f"  Binance: {len(all_bars)} bars loaded")
                    return _to_df(all_bars)
                all_bars.append(bar)

            end_ms = int(data[0][0]) - 1
            page  += 1
            if page % 10 == 0:
                dt = datetime.fromtimestamp(end_ms/1000, tz=timezone.utc)
                print(f"  Page {page}: {len(all_bars)} bars | {dt.date()}")
            time_module.sleep(0.1)

        except Exception as e:
            print(f"  Binance failed: {e}")
            break

    if all_bars:
        all_bars.sort(key=lambda x: x[0])
        print(f"  Binance: {len(all_bars)} bars loaded")
        return _to_df(all_bars)
    return None

def _to_df(raw):
    df = pd.DataFrame(raw, columns=[
        'ts','open','high','low','close','volume',
        'close_ts','qv','trades','tbbv','tbqv','ignore'])
    df.index = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True).dt.tz_convert(None)
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    return df[['open','high','low','close','volume']].dropna()

def load_twelve_data_btc(interval="15min"):
    """Twelve Data with your API key."""
    if not TD_KEY:
        return None
    print(f"\nLoading BTC from Twelve Data ({interval})...")
    all_frames = []
    end_date   = None

    for page in range(8):
        try:
            url = (f"https://api.twelvedata.com/time_series?"
                   f"symbol=BTC/USD&interval={interval}"
                   f"&outputsize=5000&order=DESC&apikey={TD_KEY}")
            if end_date:
                url += f"&end_date={end_date}"
            r   = requests.get(url, timeout=30)
            raw = r.json()
            if 'values' not in raw:
                print(f"  Twelve Data: {raw.get('message','error')}")
                break
            df = pd.DataFrame(raw['values'])
            df.index = pd.to_datetime(df['datetime'])
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df = df[cols].copy()
            for c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df.dropna(inplace=True)
            if 'volume' not in df.columns:
                df['volume'] = 100000.0
            if len(df) == 0:
                break
            all_frames.append(df)
            oldest    = df.index.min()
            end_date  = (oldest - pd.Timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"  Page {page+1}: {len(df)} bars | oldest: {oldest.date()}")
            time_module.sleep(8)
        except Exception as e:
            print(f"  Twelve Data error: {e}")
            break

    if not all_frames:
        return None
    result = pd.concat(all_frames)
    result = result[~result.index.duplicated(keep='first')].sort_index()
    print(f"  Total: {len(result)} bars | {result.index[0].date()} → {result.index[-1].date()}")
    return result

def load_yfinance_btc():
    """yfinance fallback — 60d."""
    print("\nLoading BTC from yfinance (60d)...")
    try:
        df = yf.download("BTC-USD", period="60d", interval="15m", progress=False)
        df.columns = [c[0].lower() for c in df.columns]
        df = df[['open','high','low','close','volume']].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None) if df.index.tz is None \
                   else pd.to_datetime(df.index).tz_convert(None)
        df.dropna(inplace=True)
        print(f"  yfinance: {len(df)} bars")
        return df
    except Exception as e:
        print(f"  yfinance failed: {e}")
        return None

# Try sources in order
df = load_binance("BTCUSDT", "15m", 365)
if df is None or len(df) < 500:
    df = load_twelve_data_btc("15min")
if df is None or len(df) < 500:
    df = load_yfinance_btc()
if df is None:
    print("ERROR: No data source available")
    exit()

print(f"\nData loaded: {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
print(f"Price range: ${df['close'].min():,.0f} – ${df['close'].max():,.0f}")

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════
print("\nComputing indicators...")

# EMAs
df['ema_f'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
df['ema_s'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
df['uptrend']  = df['ema_f'] > df['ema_s']
df['ema_sep']  = (df['ema_f'] - df['ema_s']).abs() / df['close']
df['trend_clear'] = df['ema_sep'] > MIN_TREND_SEP

# RSI
delta  = df['close'].diff()
gain   = delta.clip(lower=0).rolling(RSI_LEN).mean()
loss   = (-delta.clip(upper=0)).rolling(RSI_LEN).mean()
df['rsi'] = 100 - (100 / (1 + gain / (loss + 1e-10)))

# Volume MA
df['vol_ma'] = df['volume'].rolling(VOL_LEN).mean()
df['vol_ok'] = df['volume'] >= df['vol_ma']

# Session VWAP (reset each UTC day)
df['date']  = df.index.normalize()
df['typ']   = (df['high'] + df['low'] + df['close']) / 3
df['ctv']   = df.groupby('date').apply(
    lambda g: (g['typ'] * g['volume']).cumsum()
).reset_index(level=0, drop=True)
df['cv']    = df.groupby('date')['volume'].cumsum()
df['vwap']  = df['ctv'] / (df['cv'] + 1e-10)

# Swing highs/lows
df['swing_hi'] = df['high'].rolling(SWING_LEN * 2 + 1, center=True).max()
df['swing_lo'] = df['low'].rolling(SWING_LEN * 2 + 1,  center=True).min()

# At level checks
df['at_vwap']  = (df['close'] - df['vwap']).abs() / df['close'] < AT_LEVEL_PCT
df['at_sw_hi'] = (df['close'] - df['swing_hi']).abs() / df['close'] < AT_LEVEL_PCT * 1.5
df['at_sw_lo'] = (df['close'] - df['swing_lo']).abs() / df['close'] < AT_LEVEL_PCT * 1.5

# Round number
df['rn']     = (df['close'] / RN_GRID).round() * RN_GRID
df['at_rn']  = (df['close'] - df['rn']).abs() / df['close'] < AT_LEVEL_PCT

df['at_level'] = df['at_vwap'] | df['at_sw_hi'] | df['at_sw_lo'] | df['at_rn']

# Session / Dubai window
df['utc_hour'] = df.index.hour
df['in_dubai'] = (df['utc_hour'] >= DUBAI_OPEN) & (df['utc_hour'] < DUBAI_CLOSE)

# ATR
df['tr']  = np.maximum(df['high']-df['low'],
            np.maximum((df['high']-df['close'].shift(1)).abs(),
                       (df['low'] -df['close'].shift(1)).abs()))
df['atr'] = df['tr'].rolling(14).mean()

df.dropna(inplace=True)

# ══════════════════════════════════════════════════════════════════════════════
#  CANDLE PATTERNS
# ══════════════════════════════════════════════════════════════════════════════

def detect_patterns(df):
    body       = (df['close'] - df['open']).abs()
    range_     = df['high'] - df['low']
    upper_wick = df['high'] - df[['close','open']].max(axis=1)
    lower_wick = df[['close','open']].min(axis=1) - df['low']
    bull       = df['close'] > df['open']
    bear       = df['close'] < df['open']

    # Bullish patterns
    hammer      = bull & (lower_wick >= body * 2.0) & (upper_wick <= body * 0.5)
    bull_engulf = (bull &
                   (df['close'] > df['high'].shift(1)) &
                   (df['open']  < df['close'].shift(1)) &
                   (body > body.shift(1)))
    bull_pin    = bull & (lower_wick >= range_ * 0.6) & (body <= range_ * 0.35)

    # Bearish patterns
    shoot_star  = bear & (upper_wick >= body * 2.0) & (lower_wick <= body * 0.5)
    bear_engulf = (bear &
                   (df['close'] < df['low'].shift(1)) &
                   (df['open']  > df['close'].shift(1)) &
                   (body > body.shift(1)))
    bear_pin    = bear & (upper_wick >= range_ * 0.6) & (body <= range_ * 0.35)

    df['bull_pat'] = hammer | bull_engulf | bull_pin
    df['bear_pat'] = shoot_star | bear_engulf | bear_pin
    return df

df = detect_patterns(df)

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY SIGNALS
# ══════════════════════════════════════════════════════════════════════════════

# LONG: all 5 conditions
df['long_cond'] = (
    df['uptrend']    &   # 1. trend
    df['trend_clear']&
    df['at_level']   &   # 2. level
    df['bull_pat']   &   # 3. pattern
    df['vol_ok']     &   # 4. volume
    (df['rsi'] >= 35) &  # 5. RSI
    (df['rsi'] <= 60) &
    df['in_dubai']
)

# SHORT: all 5 conditions
df['short_cond'] = (
    ~df['uptrend']   &   # 1. trend
    df['trend_clear']&
    df['at_level']   &   # 2. level
    df['bear_pat']   &   # 3. pattern
    df['vol_ok']     &   # 4. volume
    (df['rsi'] >= 40) &  # 5. RSI
    (df['rsi'] <= 65) &
    df['in_dubai']
)

print(f"Long signals:  {df['long_cond'].sum()}")
print(f"Short signals: {df['short_cond'].sum()}")

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE — SPLIT EXIT (60% at TP1, 40% at TP2)
# ══════════════════════════════════════════════════════════════════════════════
print("\nRunning backtest...")

equity    = ACCOUNT
in_trade  = False
entry     = {}
trades    = []
eq_c      = [ACCOUNT]
eq_d      = [df.index[0]]

for i in range(len(df)):
    row  = df.iloc[i]
    idx  = df.index[i]
    cp   = row['close']
    atr  = row['atr']

    if pd.isna(atr) or atr == 0:
        continue

    # ── EXIT ──────────────────────────────────────────────────────────────────
    if in_trade:
        d_      = entry['dir']
        ep      = entry['price']
        sl      = entry['sl']
        tp1     = entry['tp1']
        tp2     = entry['tp2']
        be_sl   = entry.get('be_sl', sl)   # breakeven stop after TP1
        tp1_hit = entry.get('tp1_hit', False)
        bars_in = i - entry['bar_idx']

        # Use breakeven stop after TP1
        active_sl = be_sl if tp1_hit else sl

        hit_tp1 = not tp1_hit and ((d_=='LONG' and cp>=tp1) or (d_=='SHORT' and cp<=tp1))
        hit_tp2 = tp1_hit    and ((d_=='LONG' and cp>=tp2) or (d_=='SHORT' and cp<=tp2))
        hit_sl  = (d_=='LONG'  and cp<=active_sl) or (d_=='SHORT' and cp>=active_sl)
        hit_time= bars_in >= TIME_STOP and not tp1_hit
        hit_sess= row['utc_hour'] >= DUBAI_CLOSE and entry.get('in_dubai_at_entry', True)

        if hit_tp1:
            # Close 60% at TP1
            xp     = tp1
            raw    = (xp-ep) if d_=='LONG' else (ep-xp)
            pct    = raw/ep - COMMISSION
            usd    = equity * pct * entry['sm'] * TP1_CLOSE
            equity += usd
            entry['tp1_hit']  = True
            entry['be_sl']    = ep  # move stop to breakeven
            entry['tp1_pnl']  = usd
            entry['tp1_pct']  = pct * 100
            # Adjust size for remaining position
            entry['sm']       = entry['sm'] * TP2_CLOSE

        elif hit_tp2 or hit_sl or hit_time or hit_sess:
            xp  = tp2 if hit_tp2 else active_sl if hit_sl else cp
            xt  = 'TP2' if hit_tp2 else 'SL' if hit_sl else 'TIME' if hit_time else 'SESS'
            raw = (xp-ep) if d_=='LONG' else (ep-xp)
            pct = raw/ep - COMMISSION
            usd = equity * pct * entry['sm']
            equity += usd

            # Total trade result
            total_usd = usd + entry.get('tp1_pnl', 0)
            total_pct = total_usd / (equity - usd) * 100 if (equity-usd) > 0 else 0

            trades.append({
                'entry_time':  str(entry['time'])[:16],
                'exit_time':   str(idx)[:16],
                'dir':         d_,
                'entry_px':    round(ep, 2),
                'exit_px':     round(xp, 2),
                'tp1_hit':     entry.get('tp1_hit', False),
                'tp1_pct':     round(entry.get('tp1_pct', 0), 4),
                'exit_pct':    round(pct * 100, 4),
                'total_pnl%':  round(total_pct, 4),
                'total_usd':   round(total_usd, 2),
                'equity':      round(equity, 2),
                'exit':        xt,
                'bars':        bars_in,
                'at_level':    entry.get('level', ''),
                'rsi_at_entry':round(entry.get('rsi', 0), 1),
                'vol_ratio':   round(entry.get('vol_r', 0), 2),
            })
            eq_c.append(equity); eq_d.append(idx)
            in_trade = False

    # ── ENTRY ──────────────────────────────────────────────────────────────────
    if not in_trade:
        if row['long_cond']:
            sl_px  = row['low']
            sl_pct = (cp - sl_px) / cp
            if sl_pct > MAX_SL_PCT or sl_pct <= 0:
                continue  # SL too wide or zero — skip
            tp1_px = cp + (cp - sl_px) * TP1_MULT
            tp2_px = cp + (cp - sl_px) * TP2_MULT
            risk_usd = equity * RISK_PCT
            oz     = risk_usd / (cp * sl_pct)
            sm     = min((oz * cp) / equity, 5.0)
            in_trade = True
            level_name = ('VWAP' if row['at_vwap'] else
                          'SwingL' if row['at_sw_lo'] else
                          'RoundN' if row['at_rn'] else 'SwingH')
            entry = dict(time=idx, bar_idx=i, price=cp, dir='LONG',
                         sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                         tp1_hit=False, be_sl=sl_px,
                         level=level_name, rsi=row['rsi'],
                         vol_r=row['volume']/row['vol_ma'],
                         in_dubai_at_entry=row['in_dubai'])

        elif row['short_cond']:
            sl_px  = row['high']
            sl_pct = (sl_px - cp) / cp
            if sl_pct > MAX_SL_PCT or sl_pct <= 0:
                continue
            tp1_px = cp - (sl_px - cp) * TP1_MULT
            tp2_px = cp - (sl_px - cp) * TP2_MULT
            risk_usd = equity * RISK_PCT
            oz     = risk_usd / (cp * sl_pct)
            sm     = min((oz * cp) / equity, 5.0)
            in_trade = True
            level_name = ('VWAP' if row['at_vwap'] else
                          'SwingH' if row['at_sw_hi'] else
                          'RoundN' if row['at_rn'] else 'SwingL')
            entry = dict(time=idx, bar_idx=i, price=cp, dir='SHORT',
                         sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                         tp1_hit=False, be_sl=sl_px,
                         level=level_name, rsi=row['rsi'],
                         vol_r=row['volume']/row['vol_ma'],
                         in_dubai_at_entry=row['in_dubai'])

# ══════════════════════════════════════════════════════════════════════════════
#  RESULTS
# ══════════════════════════════════════════════════════════════════════════════
if not trades:
    print("\nNo trades taken — check data and signal conditions")
    exit()

tdf   = pd.DataFrame(trades)
wins  = tdf[tdf['total_usd'] > 0]
loss  = tdf[tdf['total_usd'] <= 0]
wr    = len(wins) / len(tdf)
roi   = (equity / ACCOUNT - 1) * 100
pf    = wins['total_usd'].sum() / abs(loss['total_usd'].sum()) \
        if len(loss)>0 and loss['total_usd'].sum()!=0 else 99
eq_s  = pd.Series(eq_c, index=eq_d)
mdd   = float(((eq_s - eq_s.expanding().max()) / eq_s.expanding().max() * 100).min())
days  = (pd.to_datetime(tdf['exit_time'].max()) -
         pd.to_datetime(tdf['entry_time'].min())).days
tpd   = len(tdf) / max(days, 1) * 5   # trades per trading day (5d week)
aw    = wins['total_pnl%'].mean() if len(wins) > 0 else 0
al    = abs(loss['total_pnl%'].mean()) if len(loss) > 0 else 0
be_wr = al / (aw + al) if (aw + al) > 0 else 0.5

# TP1 hit rate
tp1_rate = tdf['tp1_hit'].mean() * 100

# By direction
by_dir = tdf.groupby('dir').apply(lambda x: pd.Series({
    'n':     len(x),
    'wr%':   round((x['total_usd']>0).mean()*100, 1),
    'avg%':  round(x['total_pnl%'].mean(), 3),
    'pnl$':  round(x['total_usd'].sum(), 2),
}), include_groups=False)

# By level
by_level = tdf.groupby('at_level').apply(lambda x: pd.Series({
    'n':    len(x),
    'wr%':  round((x['total_usd']>0).mean()*100, 1),
    'avg%': round(x['total_pnl%'].mean(), 3),
    'pnl$': round(x['total_usd'].sum(), 2),
}), include_groups=False)

# By exit type
by_exit = tdf.groupby('exit').apply(lambda x: pd.Series({
    'n':    len(x),
    'wr%':  round((x['total_usd']>0).mean()*100, 1),
    'avg%': round(x['total_pnl%'].mean(), 3),
    'pnl$': round(x['total_usd'].sum(), 2),
}), include_groups=False)

# Monthly
tdf['month'] = pd.to_datetime(tdf['entry_time']).dt.to_period('M')
mo = tdf.groupby('month').agg(
    n=('total_usd','count'), pnl=('total_usd','sum'),
    wr=('total_usd', lambda x: round((x>0).mean()*100,1))
).reset_index()
mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

# Stats test
t_stat, p_val = stats.ttest_1samp(tdf['total_pnl%'], 0) if len(tdf)>5 else (0,1)

print(f"\n{'='*68}")
print(f"  BTC 15-MIN SCALPING SYSTEM — RESULTS")
print(f"  Period: {tdf['entry_time'].min()[:10]} → {tdf['exit_time'].max()[:10]}")
print(f"{'='*68}")
print(f"  Total trades    : {len(tdf)}")
print(f"  Win rate        : {wr*100:.1f}%  (break-even: {be_wr*100:.1f}%,  edge: {(wr-be_wr)*100:+.1f}%)")
print(f"  TP1 hit rate    : {tp1_rate:.1f}%  (how often price reached TP1)")
print(f"  Profit factor   : {pf:.2f}")
print(f"  ROI             : {roi:+.2f}%")
print(f"  Final equity    : ${equity:,.2f}")
print(f"  Net P&L         : ${equity-ACCOUNT:+,.2f}")
print(f"  Max drawdown    : {mdd:.2f}%")
print(f"  Avg win         : +{aw:.4f}%  (${wins['total_usd'].mean():+.2f})")
print(f"  Avg loss        : -{al:.4f}%  (${loss['total_usd'].mean():+.2f})")
print(f"  RR ratio        : {aw/al:.2f}:1" if al>0 else "  RR ratio        : ∞")
print(f"  Avg hold        : {tdf['bars'].mean():.1f} bars ({tdf['bars'].mean()*15:.0f} min)")
print(f"  Trades/day      : {tpd:.1f}")
print(f"  +ve months      : {(mo['pnl']>0).sum()}/{len(mo)}")
print(f"  p-value         : {p_val:.4f}  {'✓ SIGNIFICANT' if p_val<0.05 else f'— need more trades'}")

print(f"\n  BY DIRECTION:")
print(by_dir.to_string())

print(f"\n  BY LEVEL TYPE:")
print(by_level.to_string())

print(f"\n  BY EXIT TYPE:")
print(by_exit.to_string())

print(f"\n  MONTHLY P&L:")
print(mo[['month','n','pnl','wr','roi%']].to_string(index=False))
print(f"\n  +ve months: {(mo['pnl']>0).sum()}/{len(mo)}")
print(f"  Avg monthly P&L: ${mo['pnl'].mean():+,.2f}")
print(f"  Best month:      ${mo['pnl'].max():+,.2f}")
print(f"  Worst month:     ${mo['pnl'].min():+,.2f}")

print(f"\n{'='*68}")
print(f"  WHAT THESE NUMBERS MEAN")
print(f"{'='*68}")
print(f"""
  Break-even WR   : {be_wr*100:.1f}%  (you need WR above this to profit)
  Actual WR       : {wr*100:.1f}%
  Edge            : {(wr-be_wr)*100:+.1f}%

  At 3% risk per trade with ${ACCOUNT:,.0f} starting capital:
    Each trade risks: ${ACCOUNT*RISK_PCT:,.0f}
    Avg winning trade: +${wins['total_usd'].mean():,.2f}
    Avg losing trade:  ${loss['total_usd'].mean():,.2f}

  To make $5,000 profit at this rate:
    Trades needed: {int(5000/(wins['total_usd'].mean()*wr - abs(loss['total_usd'].mean())*(1-wr)))+1 if (wins['total_usd'].mean()*wr - abs(loss['total_usd'].mean())*(1-wr)) > 0 else 'N/A'}
    At {tpd:.1f} trades/day that is ~{int(5000/(wins['total_usd'].mean()*wr - abs(loss['total_usd'].mean())*(1-wr))/tpd)+1 if (wins['total_usd'].mean()*wr - abs(loss['total_usd'].mean())*(1-wr)) > 0 else 'N/A'} trading days
""")

print(f"{'='*68}")
tdf.to_csv("btc_15m_scalp_trades.csv", index=False)
mo.to_csv("btc_15m_scalp_monthly.csv", index=False)
print("Saved: btc_15m_scalp_trades.csv + btc_15m_scalp_monthly.csv")
print("Paste full results here for analysis.")
