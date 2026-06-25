#!/usr/bin/env python3
"""
gold_backtest_v2.py — Gold model v1 vs v2 vs v3 comparison
Data: OKX XAU-USDT-SWAP (OHLCV) + FRED (yields, DXY, VIX)
No June skip · LONG+SHORT · 2022-01-01 to today

V1 factors (5, daily macro):
  s1: Gold vs EMA50
  s2: 10Y yield direction (falling = bullish)
  s3: DXY direction (falling = bullish)
  s4: Gold 5d momentum
  s5: VIX direction (rising fear = safe haven bid = bullish gold)

V2 adds 5 technical factors:
  s6:  MACD(12,26,9) fresh cross (last 2 bars)
  s7:  Volume confirmation (high vol day in direction of close)
  s8:  RSI(14) momentum (>60=bull, <40=bear)
  s9:  Fast MACD(5,13,4) direction — intraday proxy
  s10: Gold 2d momentum (short-term continuation)

V3 (9 factors): drops VIX (s5), keeps yields+DXY+all technicals
"""

import urllib.request, json, time, io
import numpy as np
import pandas as pd
from datetime import datetime, timezone

# ── CONFIG ──────────────────────────────────────────────────────────────
OKX_BASE = 'https://www.okx.com'
SL_MULT  = 0.75
TP1_MULT = 1.5    # = 2.0R
TP2_MULT = 2.5    # = 3.33R
TP1_PCT  = 0.60
TP2_PCT  = 0.40
MAX_HOLD = 5      # trading days

# ── FETCH OKX DAILY BARS ────────────────────────────────────────────────
def fetch_okx_daily(inst='XAU-USDT-SWAP', n_batches=6):
    """Fetch ~1800 daily bars via pagination (6 × 300)"""
    all_bars = []
    after_ts = None
    hdr = {'User-Agent': 'gold-backtest/1.0'}

    for batch in range(n_batches):
        if after_ts is None:
            url = f'{OKX_BASE}/api/v5/market/candles?instId={inst}&bar=1D&limit=300'
        else:
            url = f'{OKX_BASE}/api/v5/market/history-candles?instId={inst}&bar=1D&after={after_ts}&limit=300'
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            bars = data.get('data', [])
            if not bars:
                break
            all_bars.extend(bars)
            after_ts = bars[-1][0]  # oldest ts in this batch
            time.sleep(0.3)
        except Exception as e:
            print(f'  OKX fetch error batch {batch}: {e}')
            break

    if not all_bars:
        return None

    # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    df = pd.DataFrame(all_bars, columns=['ts','o','h','l','c','vol','volccy','volquote','confirm'])
    df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True).dt.normalize()
    for col in ['o','h','l','c','vol']:
        df[col] = pd.to_numeric(df[col])
    df = df.sort_values('ts').drop_duplicates('ts').set_index('ts')
    return df

# ── FETCH FRED SERIES ────────────────────────────────────────────────────
def fetch_fred(series_id):
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    req = urllib.request.Request(url, headers={'User-Agent': 'gold-backtest/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
        df = pd.read_csv(io.StringIO(raw), parse_dates=[0], index_col=0)
        df.index = pd.DatetimeIndex(df.index).tz_localize('UTC')
        df.columns = ['val']
        df['val'] = pd.to_numeric(df['val'], errors='coerce')
        return df['val'].dropna()
    except Exception as e:
        print(f'  FRED {series_id} error: {e}')
        return None

# ── INDICATORS ───────────────────────────────────────────────────────────
def ema_s(s, n):   return s.ewm(span=n, adjust=False).mean()
def rsi_s(s, n=14):
    d  = s.diff()
    g  = d.clip(lower=0).rolling(n).mean()
    l  = (-d.clip(upper=0)).rolling(n).mean()
    rs = g / l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)
def macd_s(s, f=12, sl=26, sig=9):
    m = ema_s(s, f) - ema_s(s, sl)
    return m, ema_s(m, sig)
def atr_s(h, l, c, n=14):
    tr = pd.concat([h - l,
                    (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

# ── SIMULATE TRADE ───────────────────────────────────────────────────────
def simulate(entry, direction, atr_val, fut_hi, fut_lo, fut_cl):
    sl_pts  = SL_MULT  * atr_val
    tp1_pts = TP1_MULT * atr_val
    tp2_pts = TP2_MULT * atr_val
    tp1_r   = tp1_pts / sl_pts   # 2.0R
    tp2_r   = tp2_pts / sl_pts   # 3.33R

    sl = entry - sl_pts if direction == 'LONG' else entry + sl_pts
    tp1 = entry + tp1_pts if direction == 'LONG' else entry - tp1_pts
    tp2 = entry + tp2_pts if direction == 'LONG' else entry - tp2_pts

    total_r = 0.0
    tp1_hit = False

    for h, l, c in zip(fut_hi, fut_lo, fut_cl):
        if np.isnan(h) or np.isnan(l):
            continue
        if direction == 'LONG':
            if l <= sl:
                return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and h >= tp1:
                total_r += TP1_PCT * tp1_r
                tp1_hit = True
                sl = entry
            if tp1_hit and h >= tp2:
                total_r += TP2_PCT * tp2_r
                return total_r, 'TP2'
        else:
            if h >= sl:
                return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and l <= tp1:
                total_r += TP1_PCT * tp1_r
                tp1_hit = True
                sl = entry
            if tp1_hit and l <= tp2:
                total_r += TP2_PCT * tp2_r
                return total_r, 'TP2'

    last_c = fut_cl[-1] if len(fut_cl) and not np.isnan(fut_cl[-1]) else entry
    pnl_r = (last_c - entry) / sl_pts if direction == 'LONG' else (entry - last_c) / sl_pts
    total_r += (TP2_PCT if tp1_hit else 1.0) * pnl_r
    return total_r, 'TIME'

# ── PRINT STATS ──────────────────────────────────────────────────────────
def stats(df, label):
    if df is None or df.empty:
        print(f'\n  {label}: no trades')
        return
    longs  = df[df['direction'] == 'LONG']
    shorts = df[df['direction'] == 'SHORT']
    wins   = df[df['r'] > 0]
    by_yr  = df.groupby('year')['r'].sum()

    # max drawdown (on R curve)
    cumr   = df.sort_values('date')['r'].cumsum()
    peak   = cumr.cummax()
    dd     = (cumr - peak).min()

    # profit factor
    gross_w = df[df['r'] > 0]['r'].sum()
    gross_l = abs(df[df['r'] < 0]['r'].sum())
    pf = gross_w / gross_l if gross_l > 0 else float('inf')

    n_weeks = max(1, (df['date'].max() - df['date'].min()).days / 7)

    print(f"\n{'═'*64}")
    print(f'  {label}')
    print(f"{'═'*64}")
    print(f"  Trades    {len(df):>4}   ({len(longs)}L / {len(shorts)}S)")
    print(f"  Total R   {df['r'].sum():>+8.2f}R")
    print(f"  Win rate  {len(wins)/len(df)*100:>7.1f}%")
    print(f"  Avg R     {df['r'].mean():>+8.3f}R/trade")
    print(f"  Per week  {len(df)/n_weeks:>7.1f} trades/wk")
    print(f"  Max DD    {dd:>+8.2f}R")
    print(f"  Prof fac  {pf:>8.2f}")
    print(f"  Best      {df['r'].max():>+8.2f}R   Worst {df['r'].min():>+.2f}R")
    if len(longs):
        lwr = len(longs[longs['r'] > 0]) / len(longs) * 100
        print(f"  LONG      {longs['r'].sum():>+8.2f}R   WR={lwr:.0f}%  n={len(longs)}")
    if len(shorts):
        swr = len(shorts[shorts['r'] > 0]) / len(shorts) * 100
        print(f"  SHORT     {shorts['r'].sum():>+8.2f}R   WR={swr:.0f}%  n={len(shorts)}")
    print(f"  By year:")
    for yr, r in by_yr.items():
        cnt = len(df[df['year'] == yr])
        print(f"    {yr}  {r:>+7.2f}R  ({cnt} trades)")
    june = df[df['month'] == 6]
    if len(june):
        print(f"  June      {june['r'].sum():>+8.2f}R   ({len(june)} trades)")
    print(f"{'═'*64}")

# ── MAIN ─────────────────────────────────────────────────────────────────
print('Fetching OKX gold daily bars...', end='', flush=True)
okx = fetch_okx_daily(n_batches=7)
if okx is None or len(okx) < 100:
    print(' FAILED — cannot run backtest')
    exit(1)
print(f' {len(okx)} bars  ({okx.index[0].date()} – {okx.index[-1].date()})')

print('Fetching FRED macro data...', end='', flush=True)
tnx = fetch_fred('DGS10')    # 10Y Treasury yield
dxy = fetch_fred('DTWEXBGS') # Broad dollar index
vix = fetch_fred('VIXCLS')   # VIX
print(' done')

# Align macro to gold's date index (forward-fill weekends/holidays)
def align(series, idx):
    if series is None:
        return pd.Series(np.nan, index=idx)
    return series.reindex(idx, method='ffill')

gold_idx = okx.index
tnx_a = align(tnx, gold_idx)
dxy_a = align(dxy, gold_idx)
vix_a = align(vix, gold_idx)

gc_cl = okx['c']
gc_hi = okx['h']
gc_lo = okx['l']
gc_op = okx['o']
gc_vl = okx['vol']

# Indicators
gc_ema50         = ema_s(gc_cl, 50)
gc_atr14         = atr_s(gc_hi, gc_lo, gc_cl, 14)
gc_rsi           = rsi_s(gc_cl, 14)
gc_macd, gc_msig = macd_s(gc_cl, 12, 26, 9)
gc_fmacd, gc_fsig = macd_s(gc_cl, 5, 13, 4)
gc_ret           = gc_cl.pct_change()
vol_sma20        = gc_vl.rolling(20).mean()

dates = gc_cl.index
records = []

print(f'Running backtest ({len(dates)} bars)...', end='', flush=True)
START_DATE = pd.Timestamp('2022-01-01', tz='UTC')

for i in range(60, len(dates) - MAX_HOLD - 2):
    d = dates[i]
    if d < START_DATE:
        continue
    if pd.isna(gc_cl.iloc[i]) or pd.isna(gc_atr14.iloc[i]) or gc_atr14.iloc[i] <= 0:
        continue

    atr_val = float(gc_atr14.iloc[i])

    # ── V1 FACTORS ──────────────────────────────────────────────────────
    s1 = 1 if gc_cl.iloc[i] > gc_ema50.iloc[i] else -1

    s2 = 0
    if not pd.isna(tnx_a.iloc[i]) and not pd.isna(tnx_a.iloc[i-1]):
        s2 = 1 if tnx_a.iloc[i] < tnx_a.iloc[i-1] else -1

    s3 = 0
    if not pd.isna(dxy_a.iloc[i]) and not pd.isna(dxy_a.iloc[i-1]):
        s3 = 1 if dxy_a.iloc[i] < dxy_a.iloc[i-1] else -1

    s4 = (1 if gc_cl.iloc[i] > gc_cl.iloc[i-5] else -1) if i >= 5 else 0

    # s5: VIX direction (rising fear = gold safe haven bid)
    s5 = 0
    if not pd.isna(vix_a.iloc[i]) and not pd.isna(vix_a.iloc[i-1]):
        s5 = 1 if vix_a.iloc[i] > vix_a.iloc[i-1] else -1

    v1_raw = s1 + s2 + s3 + s4 + s5

    # ── V2 EXTRA FACTORS ────────────────────────────────────────────────
    # s6: MACD fresh cross (within last 2 bars)
    s6 = 0
    if i >= 2 and not pd.isna(gc_macd.iloc[i]):
        for k in [i, i-1]:
            if pd.isna(gc_macd.iloc[k]) or pd.isna(gc_msig.iloc[k]):
                continue
            above_now  = gc_macd.iloc[k]   > gc_msig.iloc[k]
            above_prev = gc_macd.iloc[k-1] > gc_msig.iloc[k-1]
            if above_now and not above_prev:
                s6 = 1; break
            elif not above_now and above_prev:
                s6 = -1; break

    # s7: Volume confirmation (high-volume day in direction of close)
    s7 = 0
    if not pd.isna(gc_vl.iloc[i]) and not pd.isna(vol_sma20.iloc[i]) and vol_sma20.iloc[i] > 0:
        vr     = gc_vl.iloc[i] / vol_sma20.iloc[i]
        day_up = gc_cl.iloc[i] > gc_cl.iloc[i-1]
        if vr > 1.3:
            s7 = 1 if day_up else -1
        elif vr < 0.7:
            s7 = -1 if day_up else 1

    # s8: RSI momentum
    s8 = 0
    if not pd.isna(gc_rsi.iloc[i]):
        rv = gc_rsi.iloc[i]
        if rv > 60:   s8 =  1
        elif rv < 40: s8 = -1

    # s9: Fast MACD direction (intraday proxy)
    s9 = 0
    if not pd.isna(gc_fmacd.iloc[i]) and not pd.isna(gc_fsig.iloc[i]):
        s9 = 1 if gc_fmacd.iloc[i] > gc_fsig.iloc[i] else -1

    # s10: Gold 2d momentum
    s10 = (1 if gc_cl.iloc[i] > gc_cl.iloc[i-2] else -1) if i >= 2 else 0

    v2_raw = v1_raw + s6 + s7 + s8 + s9 + s10

    # V3: drop VIX (s5), keep yields+DXY (s2,s3), keep all technicals
    # 9 factors: s1 s2 s3 s4 s6 s7 s8 s9 s10
    v3_raw = s1 + s2 + s3 + s4 + s6 + s7 + s8 + s9 + s10

    # ── ENTRY PRICE (next bar open) ──────────────────────────────────────
    entry_price = float(gc_op.iloc[i+1])
    if np.isnan(entry_price):
        entry_price = float(gc_cl.iloc[i+1])
    if np.isnan(entry_price):
        continue

    fut_hi = gc_hi.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fut_lo = gc_lo.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fut_cl = gc_cl.iloc[i+1:i+1+MAX_HOLD].values.astype(float)

    month = d.month
    year  = d.year

    for (raw_score, model_lbl) in [(v1_raw, 'v1'), (v2_raw, 'v2'), (v3_raw, 'v3')]:
        direction = 'LONG' if raw_score > 0 else ('SHORT' if raw_score < 0 else None)
        if direction is None:
            continue
        r, ex = simulate(entry_price, direction, atr_val, fut_hi, fut_lo, fut_cl)
        records.append({
            'date': d, 'month': month, 'year': year,
            'model': model_lbl, 'score': raw_score,
            'abs_score': abs(raw_score),
            'direction': direction, 'entry': entry_price,
            'r': r, 'exit': ex,
            's1':s1,'s2':s2,'s3':s3,'s4':s4,'s5':s5,
            's6':s6,'s7':s7,'s8':s8,'s9':s9,'s10':s10,
        })

print(' done')
df_all = pd.DataFrame(records)

# ── RESULTS ──────────────────────────────────────────────────────────────
start_dt = df_all['date'].min().date() if not df_all.empty else '?'
end_dt   = df_all['date'].max().date() if not df_all.empty else '?'

print(f"\n{'═'*64}")
print(f'  GOLD BACKTEST v1 vs v2 vs v3  ·  {start_dt} – {end_dt}')
print(f'  SL=0.75×ATR  TP1=1.5×ATR(60%)  TP2=2.5×ATR(40%)  Hold≤{MAX_HOLD}d')
print(f'  No June skip · LONG+SHORT · OKX XAU-USDT-SWAP')
print(f"{'═'*64}")

df_v1 = df_all[df_all['model'] == 'v1']
df_v2 = df_all[df_all['model'] == 'v2']
df_v3 = df_all[df_all['model'] == 'v3']

# V1 thresholds
for t in [3, 4, 5]:
    sub = df_v1[df_v1['abs_score'] >= t]
    stats(sub, f'V1 (5 macro factors)  |score|≥{t}/5')

# V1 original settings (LONG-only, June skip)
sub_orig = df_v1[
    (df_v1['abs_score'] >= 3) &
    (df_v1['direction'] == 'LONG') &
    (df_v1['month'] != 6)
]
stats(sub_orig, 'V1 ORIGINAL — LONG-only · June skip · ≥3/5')

# V2 thresholds
for t in [5, 6, 7, 8]:
    sub = df_v2[df_v2['abs_score'] >= t]
    stats(sub, f'V2 (10 factors macro+tech)  |score|≥{t}/10')

# V3 thresholds (9 factors: no VIX, yields+DXY+technicals)
for t in [4, 5, 6, 7]:
    sub = df_v3[df_v3['abs_score'] >= t]
    stats(sub, f'V3 (9 factors: yields+DXY+tech, no VIX)  |score|≥{t}/9')

# ── FACTOR ANALYSIS ──────────────────────────────────────────────────────
print(f"\n{'═'*64}")
print('  V2 FACTOR ANALYSIS (avg value in winning vs losing trades, ≥6/10)')
print(f"{'═'*64}")
df_v2_6 = df_v2[df_v2['abs_score'] >= 6].copy()
if not df_v2_6.empty:
    wins_v2  = df_v2_6[df_v2_6['r'] > 0]
    loses_v2 = df_v2_6[df_v2_6['r'] <= 0]
    facs = ['s1','s2','s3','s4','s5','s6','s7','s8','s9','s10']
    names = ['EMA50','Yield dir','DXY dir','5d mom','VIX dir',
             'MACD cross','Volume','RSI mom','Fast MACD','2d mom']
    print(f"  {'Factor':<12} {'Name':<12} {'All':>6} {'Wins':>6} {'Losses':>7}")
    for f, n in zip(facs, names):
        a  = df_v2_6[f].mean()
        w  = wins_v2[f].mean()  if len(wins_v2)  else float('nan')
        l  = loses_v2[f].mean() if len(loses_v2) else float('nan')
        print(f"  {f:<12} {n:<12} {a:>+6.2f} {w:>+6.2f} {l:>+7.2f}")

print(f"\n  Done.")
