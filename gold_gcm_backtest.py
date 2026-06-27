#!/usr/bin/env python3
"""
gold_gcm_backtest.py — Gold Convergence Model (GCM) v1.0

3-layer gate: ALL layers must agree before a trade fires.
  M = Macro Bias  (daily):  TIPS + Breakeven + EUR/USD      [-3 to +3]
  S = Swing State (4H):     EMA50 + MACD hist + RSI         [-3 to +3]
  E = Entry Gate  (15m):    MACD cross + Volume + RSI bounce [-3 to +3]

LONG:  M >= +1  AND  S >= +2  AND  E >= +2
SHORT: M <= -2  AND  S <= -2  AND  E <= -2  AND  VIX < 20

Asymmetric thresholds: LONG easier (NARDL: dollar down = bigger gold up)
VIX gate: no SHORTS when VIX >= 20 (Baur & Lucey safe-haven research)
Session: London 07:00-12:00 UTC / NY 13:30-20:00 UTC
June: ABSOLUTE SKIP — never override
Cooldown: 8 bars (2h) after any trade exit
"""

import urllib.request, json, time, io, sys
import numpy as np
import pandas as pd

OKX_BASE = 'https://www.okx.com'
INST     = 'XAU-USDT-SWAP'

SL_MULT  = 1.5
TP1_MULT = 2.5
TP2_MULT = 4.0
TP1_PCT  = 0.60
TP2_PCT  = 0.40
MAX_HOLD = 32      # 15m bars = 8 hours
COOLDOWN = 8       # 15m bars = 2 hours

LONDON = (7.0,  12.0)
NY     = (13.5, 20.0)

M_LONG=1; M_SHORT=-2; S_LONG=2; S_SHORT=-2; E_LONG=2; E_SHORT=-2
VIX_GATE = 20.0

BARS_PER_DAY = 92  # ~23h × 4 bars/h for gold on OKX

def fetch_okx(inst, bar, n_batches=10):
    all_bars, after_ts = [], None
    hdr = {'User-Agent': 'gold-gcm/1.0'}
    for b in range(n_batches):
        url = (f'{OKX_BASE}/api/v5/market/candles?instId={inst}&bar={bar}&limit=300'
               if after_ts is None else
               f'{OKX_BASE}/api/v5/market/history-candles?instId={inst}&bar={bar}&after={after_ts}&limit=300')
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read())
            bars = data.get('data', [])
            if not bars: break
            all_bars.extend(bars)
            after_ts = bars[-1][0]
            time.sleep(0.22)
        except Exception as e:
            print(f'  {bar} batch {b}: {e}'); break
    if not all_bars: return None
    df = pd.DataFrame(all_bars,
                      columns=['ts','o','h','l','c','vol','volccy','volquote','confirm'])
    df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True)
    for col in ['o','h','l','c','vol']:
        df[col] = pd.to_numeric(df[col])
    return df.sort_values('ts').drop_duplicates('ts').set_index('ts')

def fetch_treasury(rate_type, years=(2023,2024,2025,2026)):
    frames = []
    for yr in years:
        url = (f'https://home.treasury.gov/resource-center/data-chart-center/'
               f'interest-rates/daily-treasury-rates.csv/{yr}/all'
               f'?type={rate_type}&field_tdr_date_value={yr}&download=true')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'gold-gcm/1.0'})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
            df = pd.read_csv(io.StringIO(raw), parse_dates=['Date'], index_col='Date')
            frames.append(df)
        except Exception as e:
            print(f'  Treasury {yr}: {e}')
    if not frames: return None
    out = pd.concat(frames).sort_index()
    out.index = pd.DatetimeIndex(out.index).tz_localize('UTC')
    return out

def fetch_vix():
    url = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
    req = urllib.request.Request(url, headers={'User-Agent': 'gold-gcm/1.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode()
    df = pd.read_csv(io.StringIO(raw), parse_dates=['DATE'])
    s = df.set_index('DATE')['CLOSE'].sort_index()
    s.index = pd.DatetimeIndex(s.index).tz_localize('UTC')
    return s

def fetch_eurusd():
    url = ('https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A'
           '?detail=dataonly&format=jsondata')
    req = urllib.request.Request(
        url, headers={'User-Agent': 'gold-gcm/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read())
    obs   = data['dataSets'][0]['series']['0:0:0:0:0']['observations']
    dates = data['structure']['dimensions']['observation'][0]['values']
    recs  = {dates[int(k)]['id']: v[0] for k, v in obs.items()}
    s = pd.Series(recs).sort_index()
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).tz_localize('UTC')
    return s.astype(float)

def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))
def macd_ind(s, f=12, sl=26, sig=9):
    m = ema(s,f) - ema(s,sl)
    h = m - ema(m,sig)
    return h   # histogram only
def atr(h, l, c, n=14):
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def align_daily(series, intra_idx):
    if series is None: return pd.Series(np.nan, index=intra_idx)
    s = series.copy(); s.index = s.index.normalize()
    return s.reindex(intra_idx, method='ffill')

def align_4h(series_4h, idx_15m):
    """Shift by +4h (value available after 4H bar closes), then ffill."""
    if series_4h is None: return pd.Series(np.nan, index=idx_15m)
    s = series_4h.copy()
    s.index = s.index + pd.Timedelta(hours=4)
    return s.reindex(idx_15m, method='ffill')

def simulate(entry, direction, atr_val, fh, fl, fc):
    sl_pts = SL_MULT*atr_val; tp1_pts = TP1_MULT*atr_val; tp2_pts = TP2_MULT*atr_val
    tp1_r  = tp1_pts/sl_pts;  tp2_r   = tp2_pts/sl_pts
    sl  = entry - sl_pts  if direction=='LONG' else entry + sl_pts
    tp1 = entry + tp1_pts if direction=='LONG' else entry - tp1_pts
    tp2 = entry + tp2_pts if direction=='LONG' else entry - tp2_pts
    total_r, tp1_hit = 0.0, False
    for h, l, c in zip(fh, fl, fc):
        if np.isnan(h) or np.isnan(l): continue
        if direction=='LONG':
            if l<=sl:  return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and h>=tp1: total_r+=TP1_PCT*tp1_r; tp1_hit=True; sl=entry
            if tp1_hit and h>=tp2:    total_r+=TP2_PCT*tp2_r; return total_r,'TP2'
        else:
            if h>=sl:  return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and l<=tp1: total_r+=TP1_PCT*tp1_r; tp1_hit=True; sl=entry
            if tp1_hit and l<=tp2:    total_r+=TP2_PCT*tp2_r; return total_r,'TP2'
    last_c = fc[-1] if len(fc) and not np.isnan(fc[-1]) else entry
    pnl_r = (last_c-entry)/sl_pts if direction=='LONG' else (entry-last_c)/sl_pts
    total_r += (TP2_PCT if tp1_hit else 1.0)*pnl_r
    return total_r, 'TIME'

def stats(df, label):
    if df is None or df.empty: print(f'\n  {label}: no trades'); return
    longs=df[df['dir']=='LONG']; shorts=df[df['dir']=='SHORT']; wins=df[df['r']>0]
    cumr=(df.sort_values('ts')['r'].cumsum()); dd=(cumr-cumr.cummax()).min()
    gw=df[df['r']>0]['r'].sum(); gl=abs(df[df['r']<0]['r'].sum())
    pf=gw/gl if gl>0 else float('inf')
    n_wk=max(1,(df['ts'].max()-df['ts'].min()).total_seconds()/(7*86400))
    by_mo=df.groupby(df['ts'].dt.to_period('M'))['r'].agg(['sum','count'])
    print(f"\n{'═'*68}")
    print(f"  {label}")
    print(f"{'═'*68}")
    print(f"  Trades    {len(df):>4}   ({len(longs)}L / {len(shorts)}S)")
    print(f"  Total R   {df['r'].sum():>+8.2f}R")
    print(f"  Win rate  {len(wins)/len(df)*100:>7.1f}%")
    print(f"  Avg R     {df['r'].mean():>+8.3f}R/trade")
    print(f"  Per week  {len(df)/n_wk:>7.1f} trades/wk")
    print(f"  Max DD    {dd:>+8.2f}R")
    print(f"  Prof fac  {pf:>8.2f}")
    print(f"  Best/Worst {df['r'].max():>+.2f}R / {df['r'].min():>+.2f}R")
    if len(longs):
        print(f"  LONG      {longs['r'].sum():>+8.2f}R  WR={len(longs[longs['r']>0])/len(longs)*100:.0f}%  n={len(longs)}")
    if len(shorts):
        print(f"  SHORT     {shorts['r'].sum():>+8.2f}R  WR={len(shorts[shorts['r']>0])/len(shorts)*100:.0f}%  n={len(shorts)}")
    print(f"  Monthly:")
    for mo,(r,n) in by_mo.iterrows():
        print(f"    {mo}  {r:>+7.2f}R  ({int(n)} trades)")
    print(f"{'═'*68}")

# ═══════════════════════════════════════════════════════════════════════════
print('═'*68)
print('  GCM v1.0 — Gold Convergence Model — Data Fetch')
print('═'*68)

print('Fetching 15m bars...', end='', flush=True)
bars15 = fetch_okx(INST, '15m', n_batches=150)
if bars15 is None: print(' FAILED'); sys.exit(1)
print(f' {len(bars15)} bars  {bars15.index[0].date()} – {bars15.index[-1].date()}')

print('Fetching 4H bars...', end='', flush=True)
bars4h = fetch_okx(INST, '4H', n_batches=12)
if bars4h is None: print(' FAILED'); sys.exit(1)
print(f' {len(bars4h)} bars')

print('Fetching TIPS...', end='', flush=True)
tips_df = fetch_treasury('daily_treasury_real_yield_curve')
tips_raw = pd.to_numeric(tips_df['10 YR'],errors='coerce').dropna() if tips_df is not None else None
print(f' OK ({len(tips_raw)} rows)' if tips_raw is not None else ' FAILED')

print('Fetching Nominal...', end='', flush=True)
nom_df = fetch_treasury('daily_treasury_yield_curve')
if nom_df is not None:
    col = next((c for c in nom_df.columns if '10 Yr' in c or '10 YR' in c), None)
    nom_raw = pd.to_numeric(nom_df[col],errors='coerce').dropna() if col else None
    print(f' OK' if nom_raw is not None else ' col not found')
else:
    nom_raw = None; print(' FAILED')

bkev_raw = None
if tips_raw is not None and nom_raw is not None:
    comb = tips_raw.rename('t').to_frame().join(nom_raw.rename('n'),how='inner')
    bkev_raw = (comb['n']-comb['t']).dropna()
    print(f'Breakeven: {len(bkev_raw)} rows  latest={bkev_raw.iloc[-1]:.2f}%')

print('Fetching VIX...', end='', flush=True)
try:    vix_raw=fetch_vix();  print(f' OK  {len(vix_raw)} rows  latest={vix_raw.iloc[-1]:.2f}')
except Exception as e: vix_raw=None; print(f' FAILED: {e}')

print('Fetching EUR/USD...', end='', flush=True)
try:    eur_raw=fetch_eurusd(); print(f' OK  latest={eur_raw.iloc[-1]:.4f}')
except Exception as e: eur_raw=None; print(f' FAILED: {e}')

# ── 4H INDICATORS → shift +4H → ffill to 15m ─────────────────────────────
c4h=bars4h['c']
ema50_4h = ema(c4h,50)
hist_4h  = macd_ind(c4h,12,26,9)
rsi_4h   = rsi(c4h,14)

idx15=bars15.index
a_ema50_4h = align_4h(ema50_4h, idx15)
a_hist_4h  = align_4h(hist_4h,  idx15)
a_rsi_4h   = align_4h(rsi_4h,   idx15)

# ── 15m INDICATORS ────────────────────────────────────────────────────────
c15=bars15['c']; h15=bars15['h']; l15=bars15['l']; v15=bars15['vol']; o15=bars15['o']
atr15     = atr(h15,l15,c15,14)
hist15    = macd_ind(c15,8,21,5)
rsi15     = rsi(c15,14)
vol_sma20 = v15.rolling(20).mean()

# ── ALIGN DAILY MACRO → ffill to 15m ─────────────────────────────────────
tips_15m = align_daily(tips_raw, idx15)
bkev_15m = align_daily(bkev_raw, idx15)
vix_15m  = align_daily(vix_raw,  idx15)
eur_15m  = align_daily(eur_raw,  idx15)

LB1D = BARS_PER_DAY        # ~1 day
LB5D = BARS_PER_DAY * 5   # ~5 days

START_BAR = max(60, LB5D + 10)  # warm-up
records   = []
cooldown_until = 0

print(f'\nRunning backtest — {len(bars15)} 15m bars, start_bar={START_BAR}...', end='', flush=True)

for i in range(START_BAR, len(idx15) - MAX_HOLD - 2):
    ts = idx15[i]

    # ── ABSOLUTE FILTERS ─────────────────────────────────────────────
    if ts.month == 6: continue
    if ts.weekday() > 4: continue
    h = ts.hour + ts.minute/60
    if not ((LONDON[0] <= h < LONDON[1]) or (NY[0] <= h < NY[1])): continue
    if i < cooldown_until: continue

    # ── M: MACRO SCORE (daily data, forward-filled) ──────────────────
    m1=m2=m3=0

    # m1: TIPS daily direction
    t_now = tips_15m.iloc[i]
    t_1d  = tips_15m.iloc[i - LB1D] if i >= LB1D else np.nan
    if not pd.isna(t_now) and not pd.isna(t_1d):
        if t_now < t_1d - 0.001:   m1 =  1   # real yield falling = gold bullish
        elif t_now > t_1d + 0.001: m1 = -1

    # m2: Breakeven 5-day trend
    b_now = bkev_15m.iloc[i]
    b_5d  = bkev_15m.iloc[i - LB5D]
    if not pd.isna(b_now) and not pd.isna(b_5d):
        if b_now > b_5d + 0.01:   m2 =  1   # inflation expectations rising
        elif b_now < b_5d - 0.01: m2 = -1

    # m3: EUR/USD 5-day direction (dollar proxy)
    e_now = eur_15m.iloc[i]
    e_5d  = eur_15m.iloc[i - LB5D]
    if not pd.isna(e_now) and not pd.isna(e_5d):
        if e_now > e_5d * 1.002:   m3 =  1   # dollar weakening
        elif e_now < e_5d * 0.998: m3 = -1

    M = m1 + m2 + m3

    # ── S: SWING SCORE (4H indicators, lookahead-free) ───────────────
    s1=s2=s3=0

    v_ema50 = a_ema50_4h.iloc[i]
    if not pd.isna(v_ema50):
        s1 = 1 if c15.iloc[i] > v_ema50 else -1

    v_hist = a_hist_4h.iloc[i]
    if not pd.isna(v_hist):
        s2 = 1 if v_hist > 0 else -1

    v_rsi = a_rsi_4h.iloc[i]
    if not pd.isna(v_rsi):
        if v_rsi > 55:   s3 =  1
        elif v_rsi < 45: s3 = -1

    S = s1 + s2 + s3

    # ── E: ENTRY SCORE (15m) ─────────────────────────────────────────
    e1=e2=e3=0

    # e1: MACD(8,21,5) histogram crosses zero within last 3 bars
    for k in [i, i-1, i-2]:
        if k < 1: continue
        hn = hist15.iloc[k]; hp = hist15.iloc[k-1]
        if pd.isna(hn) or pd.isna(hp): continue
        if hn > 0 and hp <= 0:   e1 =  1; break
        elif hn < 0 and hp >= 0: e1 = -1; break

    # e2: Volume spike (>1.5× avg) on directional bar
    vsma = vol_sma20.iloc[i]
    if not pd.isna(vsma) and vsma > 0:
        vr = v15.iloc[i] / vsma
        if vr > 1.5:
            e2 = 1 if c15.iloc[i] > c15.iloc[i-1] else -1

    # e3: RSI bounce from extreme (within last 8 bars)
    rv_now = rsi15.iloc[i]
    if not pd.isna(rv_now) and i >= 8:
        rv_window = rsi15.iloc[i-8:i]
        rv_min = rv_window.min(); rv_max = rv_window.max()
        if not pd.isna(rv_min) and rv_min < 35 and rv_now > 40:   e3 =  1
        elif not pd.isna(rv_max) and rv_max > 65 and rv_now < 60: e3 = -1

    E = e1 + e2 + e3

    # ── VIX ──────────────────────────────────────────────────────────
    vix_val = float(vix_15m.iloc[i]) if not pd.isna(vix_15m.iloc[i]) else 18.0

    # ── FIRE ─────────────────────────────────────────────────────────
    if   M >= M_LONG  and S >= S_LONG  and E >= E_LONG:
        direction = 'LONG'
    elif M <= M_SHORT and S <= S_SHORT and E <= E_SHORT and vix_val < VIX_GATE:
        direction = 'SHORT'
    else:
        continue

    av = float(atr15.iloc[i])
    if pd.isna(av) or av <= 0: continue

    entry = float(o15.iloc[i+1])
    if np.isnan(entry): entry = float(c15.iloc[i+1])
    if np.isnan(entry): continue

    fh = h15.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fl = l15.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fc = c15.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    r, ex = simulate(entry, direction, av, fh, fl, fc)

    records.append({
        'ts':ts, 'month':ts.month, 'year':ts.year,
        'session':('London' if h<12 else 'NY'),
        'M':M,'S':S,'E':E,'m1':m1,'m2':m2,'m3':m3,
        's1':s1,'s2':s2,'s3':s3,'e1':e1,'e2':e2,'e3':e3,
        'vix':vix_val,'dir':direction,'entry':entry,'atr':av,'r':r,'exit':ex,
    })
    cooldown_until = i + 1 + COOLDOWN

print(f' done — {len(records)} signals')

if not records:
    print('No trades fired. Check thresholds.'); sys.exit(0)

df = pd.DataFrame(records)

# ── RESULTS ───────────────────────────────────────────────────────────────
print(f"\n{'═'*68}")
print(f"  GCM v1.0  ·  {df['ts'].min().date()} – {df['ts'].max().date()}")
print(f"  M layer: TIPS · Breakeven · EUR/USD (daily → 15m ffill)")
print(f"  S layer: 4H EMA50 · MACD hist · RSI")
print(f"  E layer: 15m MACD cross · Volume spike · RSI bounce")
print(f"  LONG: M≥1 S≥2 E≥2 | SHORT: M≤-2 S≤-2 E≤-2 + VIX<20")
print(f"  SL=1.5×ATR  TP1=2.5×ATR(60%)  TP2=4.0×ATR(40%)  Max=8h")
print(f"  Session: London 07-12 + NY 13:30-20 UTC  |  June: SKIP")
print(f"{'═'*68}")

stats(df, 'GCM v1.0 — All Signals')
stats(df[df['session']=='London'], 'London session only')
stats(df[df['session']=='NY'],     'NY session only')

# ── FACTOR ANALYSIS ───────────────────────────────────────────────────────
w = df[df['r']>0]; l = df[df['r']<=0]
print(f"\n{'═'*68}")
print(f"  FACTOR ANALYSIS  (n={len(df)}, wins={len(w)}, losses={len(l)})")
print(f"{'═'*68}")
print(f"  {'Factor':<6}{'Name':<20} {'Avg':>6} {'Win':>6} {'Loss':>7}  {'Diff':>6}")
for col,name in [
    ('M','Macro total'),('m1','TIPS direction'),('m2','Breakeven 5d'),('m3','EUR/USD 5d'),
    ('S','Swing total'),('s1','4H EMA50'),('s2','4H MACD hist'),('s3','4H RSI'),
    ('E','Entry total'),('e1','15m MACD cross'),('e2','Volume spike'),('e3','RSI bounce'),
]:
    a  = df[col].mean()
    wm = w[col].mean()  if len(w) else float('nan')
    lm = l[col].mean()  if len(l) else float('nan')
    d  = wm-lm if not (np.isnan(wm) or np.isnan(lm)) else float('nan')
    flag = ' ◄' if not np.isnan(d) and abs(d)>0.2 else ''
    print(f"  {col:<6}{name:<20} {a:>+6.2f} {wm:>+6.2f} {lm:>+7.2f}  {d:>+6.2f}{flag}")

# ── THRESHOLD SENSITIVITY ─────────────────────────────────────────────────
print(f"\n{'─'*68}")
print(f"  THRESHOLD SENSITIVITY (vary score minimums)")
print(f"{'─'*68}")
print(f"  {'Config':<30} {'Trades':>6} {'WR%':>6} {'PF':>7} {'TotalR':>8} {'DD':>8} {'Wk':>6}")
for ml,ms,sl,ss,el,es in [
    (1,-2,2,-2,2,-2),   # base config
    (1,-2,2,-2,1,-1),   # relax entry
    (1,-3,2,-2,2,-2),   # tighten SHORT macro
    (2,-2,2,-2,2,-2),   # tighten LONG macro
    (0,-2,1,-1,2,-2),   # relax all (looser)
]:
    sub_long  = df[(df['dir']=='LONG')  & (df['M']>=ml) & (df['S']>=sl) & (df['E']>=el)]
    sub_short = df[(df['dir']=='SHORT') & (df['M']<=ms) & (df['S']<=ss) & (df['E']<=es)]
    sub = pd.concat([sub_long,sub_short]).sort_values('ts')
    if sub.empty: print(f"  M≥{ml}/≤{ms} S≥{sl}/≤{ss} E≥{el}/≤{es}{'':>6} {'0':>6}"); continue
    wins=sub[sub['r']>0]; gw=sub[sub['r']>0]['r'].sum(); gl=abs(sub[sub['r']<0]['r'].sum())
    pf=gw/gl if gl>0 else float('inf')
    cumr=sub['r'].cumsum(); dd=(cumr-cumr.cummax()).min()
    nw=max(1,(sub['ts'].max()-sub['ts'].min()).total_seconds()/(7*86400))
    label=f"M≥{ml}/≤{ms} S≥{sl}/≤{ss} E≥{el}/≤{es}"
    print(f"  {label:<30} {len(sub):>6} {len(wins)/len(sub)*100:>6.1f} {pf:>7.2f} {sub['r'].sum():>+8.2f} {dd:>+8.2f} {len(sub)/nw:>6.1f}")

print(f"\n  Done.")
