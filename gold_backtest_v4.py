#!/usr/bin/env python3
"""
gold_backtest_v4.py — Professional gold quant model
Based on Erb/Harvey, AQR research, and CTA best practices.

Factors (9, each −1/0/+1):
  s1:  TIPS 10Y real yield direction  — THE primary macro driver
  s2:  Breakeven inflation direction  — inflation expectations
  s3:  12-month momentum              — AQR-validated, strongest lookback
  s4:  6-month momentum               — medium-term trend
  s5:  3-month momentum               — tactical continuation
  s6:  EMA50 regime
  s7:  RSI(14) zone  (>60 bull / <40 bear)
  s8:  Volume confirmation vs 20-day avg
  s9:  MACD(12,26,9) fresh cross within 2 bars

Note on s3 (12m): only fires when ≥252 bars of history exist — 0 otherwise.
VIX regime multiplier skipped (no reliable source in this environment).

Data: OKX XAU-USDT-SWAP (OHLCV) + Treasury.gov (TIPS 10Y, Nominal 10Y → Breakeven)
"""

import urllib.request, json, time, io, sys
import numpy as np
import pandas as pd

OKX_BASE = 'https://www.okx.com'
SL_MULT  = 0.75
TP1_MULT = 1.5
TP2_MULT = 2.5
TP1_PCT  = 0.60
TP2_PCT  = 0.40
MAX_HOLD = 5
LB_12m   = 252
LB_6m    = 126
LB_3m    = 63

# ── DATA FETCHERS ───────────────────────────────────────────────────────
def fetch_okx_daily(inst='XAU-USDT-SWAP', n_batches=8):
    all_bars, after_ts = [], None
    hdr = {'User-Agent': 'gold-v4/1.0'}
    for batch in range(n_batches):
        url = (f'{OKX_BASE}/api/v5/market/candles?instId={inst}&bar=1D&limit=300'
               if after_ts is None else
               f'{OKX_BASE}/api/v5/market/history-candles?instId={inst}&bar=1D&after={after_ts}&limit=300')
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            bars = data.get('data', [])
            if not bars: break
            all_bars.extend(bars)
            after_ts = bars[-1][0]
            time.sleep(0.3)
        except Exception as e:
            print(f'  OKX batch {batch} error: {e}'); break
    if not all_bars: return None
    df = pd.DataFrame(all_bars, columns=['ts','o','h','l','c','vol','volccy','volquote','confirm'])
    df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True).dt.normalize()
    for col in ['o','h','l','c','vol']:
        df[col] = pd.to_numeric(df[col])
    return df.sort_values('ts').drop_duplicates('ts').set_index('ts')

def fetch_treasury(rate_type, years=(2023, 2024, 2025, 2026)):
    """Fetch Treasury.gov yield curve data for multiple years."""
    frames = []
    for year in years:
        url = (f'https://home.treasury.gov/resource-center/data-chart-center/'
               f'interest-rates/daily-treasury-rates.csv/{year}/all'
               f'?type={rate_type}&field_tdr_date_value={year}&download=true')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'gold-v4/1.0'})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
            df = pd.read_csv(io.StringIO(raw), parse_dates=['Date'], index_col='Date')
            frames.append(df)
        except Exception as e:
            print(f'  Treasury {rate_type} {year}: {e}')
    if not frames: return None
    out = pd.concat(frames).sort_index()
    out.index = pd.DatetimeIndex(out.index).tz_localize('UTC')
    return out

def align(series, idx):
    if series is None: return pd.Series(np.nan, index=idx)
    return series.reindex(idx, method='ffill')

# ── INDICATORS ─────────────────────────────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))
def macd(s, f=12, sl=26, sig=9):
    m = ema(s, f) - ema(s, sl)
    return m, ema(m, sig)
def atr(h, l, c, n=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

# ── TRADE SIMULATOR ─────────────────────────────────────────────────────────
def simulate(entry, direction, atr_val, fut_hi, fut_lo, fut_cl):
    sl_pts  = SL_MULT * atr_val
    tp1_pts = TP1_MULT * atr_val
    tp2_pts = TP2_MULT * atr_val
    tp1_r   = tp1_pts / sl_pts
    tp2_r   = tp2_pts / sl_pts
    sl  = entry - sl_pts if direction == 'LONG' else entry + sl_pts
    tp1 = entry + tp1_pts if direction == 'LONG' else entry - tp1_pts
    tp2 = entry + tp2_pts if direction == 'LONG' else entry - tp2_pts
    total_r, tp1_hit = 0.0, False
    for h, l, c in zip(fut_hi, fut_lo, fut_cl):
        if np.isnan(h) or np.isnan(l): continue
        if direction == 'LONG':
            if l <= sl:   return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and h >= tp1:
                total_r += TP1_PCT * tp1_r; tp1_hit = True; sl = entry
            if tp1_hit and h >= tp2:
                total_r += TP2_PCT * tp2_r; return total_r, 'TP2'
        else:
            if h >= sl:   return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and l <= tp1:
                total_r += TP1_PCT * tp1_r; tp1_hit = True; sl = entry
            if tp1_hit and l <= tp2:
                total_r += TP2_PCT * tp2_r; return total_r, 'TP2'
    last_c = fut_cl[-1] if len(fut_cl) and not np.isnan(fut_cl[-1]) else entry
    pnl_r = (last_c - entry) / sl_pts if direction == 'LONG' else (entry - last_c) / sl_pts
    total_r += (TP2_PCT if tp1_hit else 1.0) * pnl_r
    return total_r, 'TIME'

# ── STATS PRINTER ─────────────────────────────────────────────────────────
def stats(df, label):
    if df is None or df.empty:
        print(f'\n  {label}: no trades'); return
    longs  = df[df['direction']=='LONG']
    shorts = df[df['direction']=='SHORT']
    wins   = df[df['r']>0]
    by_yr  = df.groupby('year')['r'].sum()
    cumr   = df.sort_values('date')['r'].cumsum()
    dd     = (cumr - cumr.cummax()).min()
    gw     = df[df['r']>0]['r'].sum()
    gl     = abs(df[df['r']<0]['r'].sum())
    pf     = gw / gl if gl > 0 else float('inf')
    n_wk   = max(1, (df['date'].max() - df['date'].min()).days / 7)
    print(f"\n{'═'*64}")
    print(f'  {label}')
    print(f"{'═'*64}")
    print(f"  Trades    {len(df):>4}   ({len(longs)}L / {len(shorts)}S)")
    print(f"  Total R   {df['r'].sum():>+8.2f}R")
    print(f"  Win rate  {len(wins)/len(df)*100:>7.1f}%")
    print(f"  Avg R     {df['r'].mean():>+8.3f}R/trade")
    print(f"  Per week  {len(df)/n_wk:>7.1f} trades/wk")
    print(f"  Max DD    {dd:>+8.2f}R")
    print(f"  Prof fac  {pf:>8.2f}")
    print(f"  Best/Worst  {df['r'].max():>+.2f}R / {df['r'].min():>+.2f}R")
    if len(longs):
        lwr = len(longs[longs['r']>0])/len(longs)*100
        print(f"  LONG      {longs['r'].sum():>+8.2f}R  WR={lwr:.0f}%  n={len(longs)}")
    if len(shorts):
        swr = len(shorts[shorts['r']>0])/len(shorts)*100
        print(f"  SHORT     {shorts['r'].sum():>+8.2f}R  WR={swr:.0f}%  n={len(shorts)}")
    print(f"  By year:")
    for yr, r in by_yr.items():
        print(f"    {yr}  {r:>+7.2f}R  ({len(df[df['year']==yr])} trades)")
    june = df[df['month']==6]
    if len(june):
        print(f"  June      {june['r'].sum():>+8.2f}R  ({len(june)} trades)")
    print(f"{'═'*64}")

# ── FETCH DATA ─────────────────────────────────────────────────────────────
print('Fetching OKX gold bars...', end='', flush=True)
okx = fetch_okx_daily(n_batches=8)
if okx is None or len(okx) < 100:
    print(' FAILED'); sys.exit(1)
print(f' {len(okx)} bars ({okx.index[0].date()} – {okx.index[-1].date()})')

print('Fetching Treasury.gov TIPS 10Y real yields (2023-2026)...', end='', flush=True)
tips_raw = fetch_treasury('daily_treasury_real_yield_curve', years=(2023,2024,2025,2026))
if tips_raw is not None:
    tips_raw = pd.to_numeric(tips_raw['10 YR'], errors='coerce').dropna()
    print(f' OK ({len(tips_raw)} rows, latest: {tips_raw.iloc[-1]:.2f}%)')
else:
    print(' FAILED')

print('Fetching Treasury.gov Nominal 10Y yields (2023-2026)...', end='', flush=True)
nom_raw = fetch_treasury('daily_treasury_yield_curve', years=(2023,2024,2025,2026))
if nom_raw is not None:
    col = next((c for c in nom_raw.columns if '10 Yr' in c or '10 YR' in c), None)
    nom_raw = pd.to_numeric(nom_raw[col], errors='coerce').dropna() if col else None
    print(f' OK ({len(nom_raw)} rows, latest: {nom_raw.iloc[-1]:.2f}%)' if nom_raw is not None else ' col not found')
else:
    print(' FAILED')

bkev_raw = None
if tips_raw is not None and nom_raw is not None:
    combined = tips_raw.rename('tips').to_frame().join(nom_raw.rename('nom'), how='inner')
    bkev_raw = (combined['nom'] - combined['tips']).dropna()
    print(f'Breakeven inflation computed: {len(bkev_raw)} rows, latest: {bkev_raw.iloc[-1]:.2f}%')

# ── ALIGN TO GOLD INDEX ──────────────────────────────────────────────────
idx      = okx.index
tips_a   = align(tips_raw, idx)
bkev_a   = align(bkev_raw, idx)

gc_cl  = okx['c'];  gc_hi = okx['h'];  gc_lo = okx['l']
gc_op  = okx['o'];  gc_vl = okx['vol']

gc_ema50        = ema(gc_cl, 50)
gc_atr14        = atr(gc_hi, gc_lo, gc_cl, 14)
gc_rsi14        = rsi(gc_cl, 14)
gc_macd, gc_sig = macd(gc_cl, 12, 26, 9)
vol_sma20       = gc_vl.rolling(20).mean()

dates   = gc_cl.index
records = []

tips_ok = tips_raw is not None
bkev_ok = bkev_raw is not None
print(f'\nMacro: TIPS={tips_ok}  Breakeven={bkev_ok}')
print(f'Running backtest (start from bar {LB_6m+30}, {len(dates)} total)...', end='', flush=True)

START_BAR = LB_6m + 30

for i in range(START_BAR, len(dates) - MAX_HOLD - 2):
    d = dates[i]
    if pd.isna(gc_cl.iloc[i]) or pd.isna(gc_atr14.iloc[i]) or gc_atr14.iloc[i] <= 0:
        continue
    atr_val = float(gc_atr14.iloc[i])

    # ── MACRO FACTORS ───────────────────────────────────────────────────
    s1 = 0
    if not pd.isna(tips_a.iloc[i]) and not pd.isna(tips_a.iloc[i-1]):
        s1 = 1 if tips_a.iloc[i] < tips_a.iloc[i-1] else -1

    s2 = 0
    if not pd.isna(bkev_a.iloc[i]) and not pd.isna(bkev_a.iloc[i-1]):
        s2 = 1 if bkev_a.iloc[i] > bkev_a.iloc[i-1] else -1

    # ── MOMENTUM FACTORS ──────────────────────────────────────────────
    s3 = 0
    if i >= LB_12m:
        s3 = 1 if gc_cl.iloc[i] > gc_cl.iloc[i - LB_12m] else -1

    s4 = 1 if gc_cl.iloc[i] > gc_cl.iloc[i - LB_6m] else -1
    s5 = 1 if gc_cl.iloc[i] > gc_cl.iloc[i - LB_3m] else -1

    # ── TECHNICAL FACTORS ──────────────────────────────────────────────
    s6 = 1 if gc_cl.iloc[i] > gc_ema50.iloc[i] else -1

    s7 = 0
    if not pd.isna(gc_rsi14.iloc[i]):
        rv = gc_rsi14.iloc[i]
        if rv > 60:   s7 =  1
        elif rv < 40: s7 = -1

    s8 = 0
    if not pd.isna(gc_vl.iloc[i]) and not pd.isna(vol_sma20.iloc[i]) and vol_sma20.iloc[i] > 0:
        vr     = gc_vl.iloc[i] / vol_sma20.iloc[i]
        day_up = gc_cl.iloc[i] > gc_cl.iloc[i-1]
        if vr > 1.3:   s8 = 1  if day_up else -1
        elif vr < 0.7: s8 = -1 if day_up else  1

    s9 = 0
    for k in [i, i-1]:
        if pd.isna(gc_macd.iloc[k]) or pd.isna(gc_sig.iloc[k]): continue
        an = gc_macd.iloc[k]   > gc_sig.iloc[k]
        ap = gc_macd.iloc[k-1] > gc_sig.iloc[k-1]
        if an and not ap:   s9 =  1; break
        elif not an and ap: s9 = -1; break

    raw = s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8 + s9
    direction = 'LONG' if raw > 0 else ('SHORT' if raw < 0 else None)
    if direction is None: continue

    entry_price = float(gc_op.iloc[i+1])
    if np.isnan(entry_price): entry_price = float(gc_cl.iloc[i+1])
    if np.isnan(entry_price): continue

    fut_hi = gc_hi.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fut_lo = gc_lo.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fut_cl = gc_cl.iloc[i+1:i+1+MAX_HOLD].values.astype(float)

    r, ex = simulate(entry_price, direction, atr_val, fut_hi, fut_lo, fut_cl)
    records.append({
        'date': d, 'month': d.month, 'year': d.year,
        'raw': raw, 'abs_raw': abs(raw),
        'direction': direction, 'entry': entry_price,
        'r': r, 'exit': ex,
        's1':s1,'s2':s2,'s3':s3,'s4':s4,'s5':s5,
        's6':s6,'s7':s7,'s8':s8,'s9':s9,
    })

print(' done')
df = pd.DataFrame(records)
if df.empty:
    print('No trades — check data'); sys.exit(1)

# ── RESULTS ─────────────────────────────────────────────────────────────
s0 = df['date'].min().date(); e0 = df['date'].max().date()
macro_label = ('TIPS+Breakeven' if tips_ok and bkev_ok
               else 'TIPS only' if tips_ok else 'No macro')

print(f"\n{'═'*64}")
print(f'  GOLD V4  ·  {s0} – {e0}')
print(f'  Macro: {macro_label}')
print(f'  9 factors: TIPS · Bkev · 12m · 6m · 3m · EMA · RSI · Vol · MACD')
print(f'  SL=0.75×ATR  TP1=1.5×ATR(60%)  TP2=2.5×ATR(40%)  Hold≤{MAX_HOLD}d')
print(f'  No June skip · LONG+SHORT · OKX XAU-USDT-SWAP')
print(f"{'═'*64}")

for t in [4, 5, 6, 7]:
    sub = df[df['abs_raw'] >= t]
    stats(sub, f'V4  |score|≥{t}/9')

# ── FACTOR ANALYSIS ──────────────────────────────────────────────────────
sub5 = df[df['abs_raw'] >= 5]
if not sub5.empty:
    print(f"\n{'═'*64}")
    print(f'  FACTOR ANALYSIS — wins vs losses  (|score|≥5, n={len(sub5)})')
    print(f"{'═'*64}")
    w = sub5[sub5['r']>0]; l = sub5[sub5['r']<=0]
    facs  = ['s1','s2','s3','s4','s5','s6','s7','s8','s9']
    names = ['TIPS real rate','Breakeven inf','12m momentum','6m momentum',
             '3m momentum','EMA50','RSI zone','Volume','MACD cross']
    print(f"  {'Factor':<5}{'Name':<18} {'All':>6} {'Wins':>6} {'Loss':>7}  {'Diff':>6}")
    for f, n in zip(facs, names):
        a  = sub5[f].mean()
        wm = w[f].mean() if len(w) else float('nan')
        lm = l[f].mean() if len(l) else float('nan')
        diff = wm - lm if not (np.isnan(wm) or np.isnan(lm)) else float('nan')
        flag = ' ◄ KEY' if not np.isnan(diff) and abs(diff) > 0.3 else ''
        print(f"  {f:<5}{n:<18} {a:>+6.2f} {wm:>+6.2f} {lm:>+7.2f}  {diff:>+6.2f}{flag}")

print(f"\n{'─'*64}")
print(f'  Quick compare: V2 vs V4')
print(f'  OLD V2: nominal yields · DXY · VIX · EMA · RSI · Vol · MACD · 5d/2d mom')
print(f'  NEW V4: REAL yields · Breakeven · 12m/6m/3m mom · EMA · RSI · Vol · MACD')
print(f"\n  Done.")
