"""
BTC 15M — V13 FILTER A/B TEST
================================
Tests 4 V13 filters individually and in combination vs the V2 baseline.

V13 FILTERS UNDER TEST:
  F1: MACD(12,26,9) histogram direction matches trade direction
  F2: RSI(14) tighter threshold  < 55 longs (was 35-60) / > 45 shorts (was 40-65)
  F3: Volume >= 120% of 20-bar average  (was just >= avg)
  F4: OBV > OBV EMA10  (new — not in V2 at all)

BASELINE: V2 system (1h EMA50 trend filter + ATR SL + VWAP/Swing only)
  - vol >= avg,  RSI 35-60 longs / 40-65 shorts

BTC uses REAL Binance exchange volume — all 4 filters are valid to test.
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, timedelta
import time as time_module
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

ACCOUNT      = 10_000.0
RISK_PCT     = 0.03
MAX_SL_PCT   = 0.008
TP1_MULT     = 1.5
TP2_MULT     = 2.5
TP1_CLOSE    = 0.60
TP2_CLOSE    = 0.40
TIME_STOP    = 3
COMMISSION   = 0.00055
EMA_FAST     = 8
EMA_SLOW     = 21
RSI_LEN      = 14
VOL_LEN      = 20
SWING_LEN    = 5
AT_LEVEL_PCT = 0.0012
MIN_TREND_SEP= 0.0005
DUBAI_OPEN   = 6
DUBAI_CLOSE  = 18
TD_KEY       = "06f050cd7d9940a895f9461e7f0ff7e3"

print("="*72)
print("  BTC 15M — V13 FILTER A/B TEST")
print("  Baseline: V2 (1h EMA50 + ATR-SL + VWAP/Swing + vol≥avg + RSI35-60)")
print("="*72)

# ── DATA LOADING ──────────────────────────────────────────────────────────────
def load_binance(symbol="BTCUSDT", interval="15m", days=365):
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

def load_yfinance_btc():
    print("\nLoading BTC from yfinance (60d fallback)...")
    try:
        df = yf.download("BTC-USD", period="60d", interval="15m", progress=False)
        df.columns = [c[0].lower() for c in df.columns]
        df = df[['open','high','low','close','volume']].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None) if df.index.tz is None \
                   else pd.to_datetime(df.index).tz_convert(None)
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"  yfinance failed: {e}")
        return None

df = load_binance("BTCUSDT", "15m", 365)
if df is None or len(df) < 500:
    df = load_yfinance_btc()
if df is None:
    print("ERROR: No data source available")
    exit()
print(f"\nData: {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")

# ── 1H TREND FILTER ───────────────────────────────────────────────────────────
print('\nLoading 1h trend filter...')
try:
    df_1h = yf.download('BTC-USD', period='2y', interval='1h', progress=False)
    df_1h.columns = [c[0].lower() for c in df_1h.columns]
    df_1h.index   = pd.to_datetime(df_1h.index).tz_localize(None) if df_1h.index.tz is None \
                    else pd.to_datetime(df_1h.index).tz_convert(None)
    df_1h['ema50_1h'] = df_1h['close'].ewm(span=50, adjust=False).mean()
    df_1h['trend_1h'] = np.where(df_1h['close'] > df_1h['ema50_1h'], 'LONG', 'SHORT')
    trend_series = df_1h['trend_1h'].reindex(df.index, method='ffill')
    df['trend_1h'] = trend_series.fillna('LONG')
    print(f"  1h trend: LONG {(df['trend_1h']=='LONG').mean()*100:.1f}% | SHORT {(df['trend_1h']=='SHORT').mean()*100:.1f}%")
except Exception as e:
    print(f"  1h trend failed ({e}) — defaulting LONG")
    df['trend_1h'] = 'LONG'

# ── INDICATORS ────────────────────────────────────────────────────────────────
print("\nComputing indicators...")

df['ema_f']      = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
df['ema_s']      = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
df['uptrend']    = df['ema_f'] > df['ema_s']
df['ema_sep']    = (df['ema_f'] - df['ema_s']).abs() / df['close']
df['trend_clear']= df['ema_sep'] > MIN_TREND_SEP

# RSI
delta       = df['close'].diff()
gain        = delta.clip(lower=0).rolling(RSI_LEN).mean()
loss        = (-delta.clip(upper=0)).rolling(RSI_LEN).mean()
df['rsi']   = 100 - (100 / (1 + gain / (loss + 1e-10)))

# Volume
df['vol_ma']    = df['volume'].rolling(VOL_LEN).mean()
df['vol_ok']    = df['volume'] >= df['vol_ma']
df['vol_surge'] = df['volume'] >= df['vol_ma'] * 1.20

# Session VWAP
df['date'] = df.index.normalize()
df['typ']  = (df['high'] + df['low'] + df['close']) / 3
df['ctv']  = df.groupby('date').apply(
    lambda g: (g['typ'] * g['volume']).cumsum()
).reset_index(level=0, drop=True)
df['cv']   = df.groupby('date')['volume'].cumsum()
df['vwap'] = df['ctv'] / (df['cv'] + 1e-10)

# Swing highs/lows
df['swing_hi'] = df['high'].rolling(SWING_LEN * 2 + 1, center=True).max()
df['swing_lo'] = df['low'].rolling(SWING_LEN * 2 + 1,  center=True).min()
df['at_vwap']  = (df['close'] - df['vwap']).abs() / df['close'] < AT_LEVEL_PCT
df['at_sw_hi'] = (df['close'] - df['swing_hi']).abs() / df['close'] < AT_LEVEL_PCT * 1.5
df['at_sw_lo'] = (df['close'] - df['swing_lo']).abs() / df['close'] < AT_LEVEL_PCT * 1.5
df['at_level'] = df['at_vwap'] | df['at_sw_hi'] | df['at_sw_lo']

# Session window
df['utc_hour'] = df.index.hour
df['in_dubai'] = (df['utc_hour'] >= DUBAI_OPEN) & (df['utc_hour'] < DUBAI_CLOSE)

# ATR
df['tr']  = np.maximum(df['high'] - df['low'],
            np.maximum((df['high'] - df['close'].shift(1)).abs(),
                       (df['low']  - df['close'].shift(1)).abs()))
df['atr'] = df['tr'].rolling(14).mean()

# V13 F1: MACD(12,26,9) histogram
ema12          = df['close'].ewm(span=12, adjust=False).mean()
ema26          = df['close'].ewm(span=26, adjust=False).mean()
macd_line      = ema12 - ema26
signal_line    = macd_line.ewm(span=9, adjust=False).mean()
df['macd_hist'] = macd_line - signal_line

# V13 F4: OBV > OBV EMA10
obv             = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
df['obv']       = obv
df['obv_ema10'] = obv.ewm(span=10, adjust=False).mean()
df['obv_above'] = df['obv'] > df['obv_ema10']

df.dropna(inplace=True)
print(f"  Indicators computed — {len(df)} bars remaining")

# ── CANDLE PATTERNS ───────────────────────────────────────────────────────────
def detect_patterns(df):
    body       = (df['close'] - df['open']).abs()
    range_     = df['high'] - df['low']
    upper_wick = df['high'] - df[['close','open']].max(axis=1)
    lower_wick = df[['close','open']].min(axis=1) - df['low']
    bull       = df['close'] > df['open']
    bear       = df['close'] < df['open']
    hammer      = bull & (lower_wick >= body * 2.0) & (upper_wick <= body * 0.5)
    bull_engulf = (bull & (df['close'] > df['high'].shift(1)) &
                   (df['open']  < df['close'].shift(1)) & (body > body.shift(1)))
    bull_pin    = bull & (lower_wick >= range_ * 0.6) & (body <= range_ * 0.35)
    shoot_star  = bear & (upper_wick >= body * 2.0) & (lower_wick <= body * 0.5)
    bear_engulf = (bear & (df['close'] < df['low'].shift(1)) &
                   (df['open']  > df['close'].shift(1)) & (body > body.shift(1)))
    bear_pin    = bear & (upper_wick >= range_ * 0.6) & (body <= range_ * 0.35)
    df['bull_pat'] = hammer | bull_engulf | bull_pin
    df['bear_pat'] = shoot_star | bear_engulf | bear_pin
    return df

df = detect_patterns(df)

# ── BACKTEST FUNCTION ─────────────────────────────────────────────────────────
def run(use_macd=False, use_rsi_tight=False, use_vol_surge=False,
        use_obv=False, label=""):
    """
    use_macd      : require MACD histogram direction to match trade direction
    use_rsi_tight : RSI<55 longs / >45 shorts (instead of 35-60 / 40-65)
    use_vol_surge : volume >= 1.20x avg (instead of just >= avg)
    use_obv       : OBV > OBV EMA10 for longs, < for shorts
    """
    # Build entry conditions
    base_long = (
        df['uptrend'] & df['trend_clear'] &
        (df['trend_1h'] == 'LONG') &
        df['at_level']  &
        df['bull_pat']  &
        df['in_dubai']
    )
    base_short = (
        ~df['uptrend'] & df['trend_clear'] &
        (df['trend_1h'] == 'SHORT') &
        df['at_level']  &
        df['bear_pat']  &
        df['in_dubai']
    )

    # Volume
    vol_mask = df['vol_surge'] if use_vol_surge else df['vol_ok']
    base_long  = base_long  & vol_mask
    base_short = base_short & vol_mask

    # RSI
    if use_rsi_tight:
        base_long  = base_long  & (df['rsi'] < 55)
        base_short = base_short & (df['rsi'] > 45)
    else:
        base_long  = base_long  & (df['rsi'] >= 35) & (df['rsi'] <= 60)
        base_short = base_short & (df['rsi'] >= 40) & (df['rsi'] <= 65)

    # MACD
    if use_macd:
        base_long  = base_long  & (df['macd_hist'] > 0)
        base_short = base_short & (df['macd_hist'] < 0)

    # OBV
    if use_obv:
        base_long  = base_long  &  df['obv_above']
        base_short = base_short & ~df['obv_above']

    # Backtest loop
    equity   = ACCOUNT
    in_trade = False
    entry    = {}
    trades   = []
    eq_c     = [ACCOUNT]
    eq_d     = [df.index[0]]

    for i in range(len(df)):
        row = df.iloc[i]
        idx = df.index[i]
        cp  = row['close']
        atr = row['atr']
        if pd.isna(atr) or atr == 0:
            continue

        # ── EXIT ──────────────────────────────────────────────────────────────
        if in_trade:
            d_      = entry['dir']
            ep      = entry['price']
            sl      = entry['sl']
            tp1     = entry['tp1']
            tp2     = entry['tp2']
            tp1_hit = entry.get('tp1_hit', False)
            be_sl   = entry.get('be_sl', sl)
            bars_in = i - entry['bar_idx']
            active_sl = be_sl if tp1_hit else sl

            hit_tp1  = not tp1_hit and ((d_ == 'LONG' and cp >= tp1) or (d_ == 'SHORT' and cp <= tp1))
            hit_tp2  = tp1_hit    and ((d_ == 'LONG' and cp >= tp2) or (d_ == 'SHORT' and cp <= tp2))
            hit_sl   = (d_ == 'LONG' and cp <= active_sl) or (d_ == 'SHORT' and cp >= active_sl)
            hit_time = bars_in >= TIME_STOP and not tp1_hit
            hit_sess = row['utc_hour'] >= DUBAI_CLOSE

            if hit_tp1:
                xp   = tp1
                raw  = (xp - ep) if d_ == 'LONG' else (ep - xp)
                pct  = raw / ep - COMMISSION
                usd  = equity * pct * entry['sm'] * TP1_CLOSE
                equity += usd
                entry['tp1_hit'] = True
                entry['be_sl']   = ep
                entry['tp1_pnl'] = usd
                entry['tp1_pct'] = pct * 100
                entry['sm']      = entry['sm'] * TP2_CLOSE

            elif hit_tp2 or hit_sl or hit_time or hit_sess:
                xp  = tp2 if hit_tp2 else active_sl if hit_sl else cp
                xt  = 'TP2' if hit_tp2 else 'SL' if hit_sl else 'TIME' if hit_time else 'SESS'
                raw = (xp - ep) if d_ == 'LONG' else (ep - xp)
                pct = raw / ep - COMMISSION
                usd = equity * pct * entry['sm']
                equity += usd
                total_usd = usd + entry.get('tp1_pnl', 0)
                total_pct = total_usd / (equity - usd) * 100 if (equity - usd) > 0 else 0
                trades.append({
                    'entry_time': str(entry['time'])[:16],
                    'exit_time':  str(idx)[:16],
                    'dir':        d_,
                    'total_usd':  round(total_usd, 2),
                    'total_pct':  round(total_pct, 4),
                    'exit':       xt,
                    'tp1_hit':    entry.get('tp1_hit', False),
                    'rsi':        round(entry.get('rsi', 0), 1),
                    'vol_ratio':  round(entry.get('vol_r', 0), 2),
                    'macd_hist':  round(entry.get('macd_h', 0), 4),
                })
                eq_c.append(equity)
                eq_d.append(idx)
                in_trade = False

        # ── ENTRY ──────────────────────────────────────────────────────────────
        if not in_trade:
            direction = None
            if base_long.iloc[i]:
                direction = 'LONG'
            elif base_short.iloc[i]:
                direction = 'SHORT'

            if direction is not None:
                sl_dist = atr * 1.0
                sl_pct  = sl_dist / cp
                if sl_pct > MAX_SL_PCT or sl_pct <= 0:
                    continue
                if direction == 'LONG':
                    sl_px  = cp - sl_dist
                    tp1_px = cp + sl_dist * TP1_MULT
                    tp2_px = cp + sl_dist * TP2_MULT
                else:
                    sl_px  = cp + sl_dist
                    tp1_px = cp - sl_dist * TP1_MULT
                    tp2_px = cp - sl_dist * TP2_MULT
                risk_usd = equity * RISK_PCT
                oz  = risk_usd / (cp * sl_pct)
                sm  = min((oz * cp) / equity, 5.0)
                in_trade = True
                entry = dict(
                    time=idx, bar_idx=i, price=cp, dir=direction,
                    sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                    tp1_hit=False, be_sl=sl_px,
                    rsi=row['rsi'],
                    vol_r=row['volume'] / row['vol_ma'],
                    macd_h=row['macd_hist'],
                )

    if not trades:
        return None, []

    tdf = pd.DataFrame(trades)
    return equity, tdf


# ── VARIANT DEFINITIONS ───────────────────────────────────────────────────────
variants = [
    ("BASELINE           (vol≥avg, RSI35-60/40-65)",
     dict(use_macd=False, use_rsi_tight=False, use_vol_surge=False, use_obv=False)),
    ("+ F1 MACD hist",
     dict(use_macd=True,  use_rsi_tight=False, use_vol_surge=False, use_obv=False)),
    ("+ F2 RSI tight (<55/>45)",
     dict(use_macd=False, use_rsi_tight=True,  use_vol_surge=False, use_obv=False)),
    ("+ F3 Vol surge (≥120%)",
     dict(use_macd=False, use_rsi_tight=False, use_vol_surge=True,  use_obv=False)),
    ("+ F4 OBV > EMA10",
     dict(use_macd=False, use_rsi_tight=False, use_vol_surge=False, use_obv=True)),
    ("+ F1+F2 MACD+RSI tight",
     dict(use_macd=True,  use_rsi_tight=True,  use_vol_surge=False, use_obv=False)),
    ("+ F1+F3 MACD+Vol surge",
     dict(use_macd=True,  use_rsi_tight=False, use_vol_surge=True,  use_obv=False)),
    ("+ F1+F4 MACD+OBV",
     dict(use_macd=True,  use_rsi_tight=False, use_vol_surge=False, use_obv=True)),
    ("+ F2+F3 RSI tight+Vol surge",
     dict(use_macd=False, use_rsi_tight=True,  use_vol_surge=True,  use_obv=False)),
    ("+ F2+F4 RSI tight+OBV",
     dict(use_macd=False, use_rsi_tight=True,  use_vol_surge=False, use_obv=True)),
    ("+ F3+F4 Vol surge+OBV",
     dict(use_macd=False, use_rsi_tight=False, use_vol_surge=True,  use_obv=True)),
    ("+ F1+F2+F3 MACD+RSI+Vol",
     dict(use_macd=True,  use_rsi_tight=True,  use_vol_surge=True,  use_obv=False)),
    ("+ F1+F2+F4 MACD+RSI+OBV",
     dict(use_macd=True,  use_rsi_tight=True,  use_vol_surge=False, use_obv=True)),
    ("+ ALL F1-F4",
     dict(use_macd=True,  use_rsi_tight=True,  use_vol_surge=True,  use_obv=True)),
]

# ── RUN ALL VARIANTS ──────────────────────────────────────────────────────────
print("\nRunning variants...")
results = []

for label, kwargs in variants:
    equity, tdf = run(**kwargs)
    if tdf is None or len(tdf) == 0:
        results.append((label, 0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0))
        print(f"  {label[:55]:<55}  n=   0  NO TRADES")
        continue
    n      = len(tdf)
    roi    = (equity - ACCOUNT) / ACCOUNT * 100
    wins   = tdf[tdf['total_usd'] > 0]
    wr     = len(wins) / n * 100
    days   = (pd.to_datetime(tdf['exit_time'].max()) -
              pd.to_datetime(tdf['entry_time'].min())).days
    tpd    = n / max(days, 1) * 5
    sharpe = tdf['total_pct'].mean() / tdf['total_pct'].std() * np.sqrt(252) \
             if tdf['total_pct'].std() > 0 else 0
    eq_s   = pd.Series([ACCOUNT] + list(ACCOUNT + tdf['total_usd'].cumsum()))
    mdd    = float(((eq_s - eq_s.expanding().max()) / eq_s.expanding().max() * 100).min())
    _, pv  = stats.ttest_1samp(tdf['total_pct'], 0) if n > 5 else (0, 1.0)
    results.append((label, n, tpd, roi, wr, sharpe, pv, mdd))
    sig = " ✓" if pv < 0.05 else ("~" if pv < 0.10 else "  ")
    print(f"  {label[:55]:<55}  n={n:>4}  p={pv:.4f}{sig}  ROI={roi:>+7.1f}%  WR={wr:.1f}%")

# ── COMPARISON TABLE ──────────────────────────────────────────────────────────
print(f"\n{'='*105}")
print(f"  BTC V13 FILTER COMPARISON")
print(f"{'='*105}")
print(f"  {'Variant':<50} {'N':>5} {'TPD':>5} {'ROI':>8} {'WR':>7} {'Sharpe':>7} {'p-val':>8} {'MDD':>7}")
print(f"  {'─'*100}")

baseline_sharpe = results[0][5]
baseline_wr     = results[0][4]

for i, (label, n, tpd, roi, wr, sharpe, pv, mdd) in enumerate(results):
    sig     = " ✓" if pv < 0.05 else ("~" if pv < 0.10 else "  ")
    ds      = f" ({sharpe-baseline_sharpe:+.2f})" if i > 0 and n > 0 else ""
    flag    = "  ← BASELINE" if i == 0 else ""
    print(f"  {label:<50} {n:>5} {tpd:>4.1f} {roi:>+7.1f}% {wr:>6.1f}% {sharpe:>7.2f}{ds:<8} {pv:>8.4f}{sig:>3} {mdd:>6.1f}%{flag}")

# ── BEST VARIANT DETAIL ───────────────────────────────────────────────────────
valid = [(l,n,tpd,roi,wr,sh,pv,mdd) for (l,n,tpd,roi,wr,sh,pv,mdd) in results if n >= 30]
best  = max(valid, key=lambda x: x[5]) if valid else results[0]
best_label  = best[0]
best_kwargs = next(k for l, k in [(v[0], v[1]) for v in variants] if l == best_label)

print(f"\n{'='*105}")
print(f"  BEST BY SHARPE: {best_label.strip()}")
print(f"{'='*105}")
equity_best, tdf_best = run(**best_kwargs)
n   = len(tdf_best)
roi = (equity_best - ACCOUNT) / ACCOUNT * 100
wins = tdf_best[tdf_best['total_usd'] > 0]
wr  = len(wins) / n * 100
_, pv = stats.ttest_1samp(tdf_best['total_pct'], 0)
sharpe = tdf_best['total_pct'].mean() / tdf_best['total_pct'].std() * np.sqrt(252)
eq_s = pd.Series([ACCOUNT] + list(ACCOUNT + tdf_best['total_usd'].cumsum()))
mdd  = float(((eq_s - eq_s.expanding().max()) / eq_s.expanding().max() * 100).min())

print(f"""
  $10,000 → ${equity_best:,.0f}  ({roi:+.1f}% ROI)
  Sharpe : {sharpe:.2f}    Max drawdown: {mdd:.1f}%
  Trades : {n}
  WR     : {wr:.1f}%   Avg P&L: ${tdf_best['total_usd'].mean():+,.2f}
  p-value: {pv:.4f}  {'✓ SIGNIFICANT' if pv < 0.05 else '~ borderline' if pv < 0.10 else '— not significant'}
""")

tdf_best['month'] = pd.to_datetime(tdf_best['entry_time']).dt.to_period('M')
print("  Monthly:")
pos, run_eq = 0, ACCOUNT
for mo, g in tdf_best.groupby('month'):
    run_eq += g['total_usd'].sum()
    flag    = "✓" if g['total_usd'].sum() > 0 else "✗"
    if g['total_usd'].sum() > 0: pos += 1
    print(f"    {str(mo)}  {len(g):>3}tr  WR {(g['total_usd']>0).mean()*100:.0f}%  ${g['total_usd'].sum():>+8,.2f}  ${run_eq:>8,.0f} {flag}")
print(f"\n  Positive months: {pos}/{tdf_best['month'].nunique()}")

# ── DIRECTION BREAKDOWN for BASELINE ─────────────────────────────────────────
print(f"\n{'='*105}")
print("  DIRECTION BREAKDOWN (baseline) — long vs short split")
print(f"{'='*105}")
_, base_tdf = run(**variants[0][1])
if base_tdf is not None and len(base_tdf) > 0:
    for d, g in base_tdf.groupby('dir'):
        w = (g['total_usd'] > 0).mean() * 100
        print(f"  {d:<6}  {len(g):>4}tr | WR {w:.1f}% | avg ${g['total_usd'].mean():+.2f} | total ${g['total_usd'].sum():+,.0f}")

# ── VERDICT ───────────────────────────────────────────────────────────────────
print(f"\n{'='*105}")
print("  VERDICT — what to add to btc_signal.gs")
print(f"{'='*105}")
for label, n, tpd, roi, wr, sharpe, pv, mdd in results[1:]:
    delta = sharpe - baseline_sharpe
    dwr   = wr - baseline_wr
    if n < 20:
        rec = "TOO FEW TRADES"
    elif delta > 0.15 and dwr > 1.0:
        rec = "ADD ✓"
    elif delta > 0.05:
        rec = "CONSIDER"
    elif abs(delta) <= 0.05:
        rec = "NEUTRAL"
    else:
        rec = "SKIP ✗"
    print(f"  {rec:<16} {label[:58]:<58}  ΔSharpe={delta:+.2f}  ΔWR={dwr:+.1f}%")

tdf_best.to_csv("btc_v13_filter_trades.csv", index=False)
print("\nSaved: btc_v13_filter_trades.csv")
print("="*105)
