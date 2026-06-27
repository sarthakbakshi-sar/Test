#!/usr/bin/env python3
"""
gold_gcm_v3.py — Gold Convergence Model v3  (GCM-3)
======================================================
Unique 3-layer multi-timeframe gold system.
Fires when macro, swing, and entry all agree simultaneously.

  M = Macro Bias  (daily):  TIPS real yield + Breakeven inflation + EUR/USD   [-3 to +3]
  S = Swing State (4H):     EMA50 + MACD histogram + RSI zone                  [-3 to +3]
  E = Entry Gate  (15m):    MACD(8,21,5) cross + 5-bar breakout+vol + EMA9/21 [-3 to +3]

Session-tiered fire conditions (asymmetric — research-grounded):
  LONDON 07-12 UTC  LONG:  M≥1 + m3>0 + S=+3 + E≥2   (perfect 4H + strong entry)
  NY     13:30-20   LONG:  M≥1 + m3>0 + S≥2 + E≥1   (majority 4H + any entry signal)
  BOTH sessions     SHORT: M≤-2 + S≤-2 + E≤-2 + VIX<22

Design rationale:
  • m3 (EUR/USD) mandatory for LONG: strongest single predictor from factor analysis.
    NARDL research: dollar DOWN causes LARGER gold up moves than dollar UP causes
    gold down — so EUR/USD rising (dollar falling) is the clearest LONG signal.
  • London needs S=+3: London gold is choppier, only trade when 4H is perfectly stacked.
  • NY relaxes entry: US session has more institutional directional flow.
  • VIX<22 for SHORT: Baur & Lucey (2010) — gold is safe haven above VIX 20,
    shorting against safe-haven demand is low-probability.
  • June ABSOLUTE SKIP (seasonal pattern, never override).
  • 2-consecutive-loss halt: prevents cascading losses on bad macro days.

Backtest results (Apr 2025 – May 2026, OKX XAU-USDT-SWAP):
  111 trades (95L/16S) | WR=52% | PF=1.63 | +29.9R total
  MaxDD=-8.1R | 2.1 trades/wk | LONG WR=53% SHORT WR=50%

Risk parameters:
  SL=2.0×ATR(14)  TP1=3.0×ATR (60%)  TP2=5.0×ATR (40%)
  R per trade (TP1): 1.5R | (TP2): 2.5R | Max hold: 8h (32 bars)
  Risk per trade: $300
"""

import urllib.request, json, time, io, sys
import numpy as np
import pandas as pd

# ── CONSTANTS ─────────────────────────────────────────────────────────────
OKX_BASE = 'https://www.okx.com'
INST     = 'XAU-USDT-SWAP'

SL_MULT  = 2.0    # × ATR(14, 15m)
TP1_MULT = 3.0    # → 1.5R when hit
TP2_MULT = 5.0    # → 2.5R when hit
TP1_PCT  = 0.60
TP2_PCT  = 0.40
MAX_HOLD = 32     # 15m bars = 8 hours
COOLDOWN = 8      # 15m bars = 2 hours

# Session windows (UTC hour, float)
LONDON = (7.0,  12.0)
NY     = (13.5, 20.0)

# Fire thresholds per session
LONDON_S = 3;  LONDON_E = 2   # London: strict (S=+3, E≥2)
NY_S     = 2;  NY_E     = 1   # NY: relaxed (S≥2, E≥1)
M_MIN    = 1                   # Both: M≥+1 for LONG
SHORT_M  = -2; SHORT_S  = -2; SHORT_E = -2
VIX_GATE = 22.0                # SHORT blocked when VIX ≥ 22
MAX_CONSEC_LOSS = 2            # Halt trading for rest of day after 2 consecutive losses

BARS_PER_DAY = 92

# ── DATA FETCHERS ─────────────────────────────────────────────────────────
def fetch_okx(inst, bar, n_batches=10):
    all_bars, after_ts = [], None
    hdr = {'User-Agent': 'gold-gcm3/1.0'}
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
            all_bars.extend(bars); after_ts = bars[-1][0]; time.sleep(0.22)
        except Exception as e:
            print(f'  {bar} batch {b}: {e}'); break
    if not all_bars: return None
    df = pd.DataFrame(all_bars, columns=['ts','o','h','l','c','vol','vc','vq','cf'])
    df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True)
    for col in ['o','h','l','c','vol']: df[col] = pd.to_numeric(df[col])
    return df.sort_values('ts').drop_duplicates('ts').set_index('ts')

def fetch_treasury(rate_type, years=(2023,2024,2025,2026)):
    frames = []
    for yr in years:
        url = (f'https://home.treasury.gov/resource-center/data-chart-center/'
               f'interest-rates/daily-treasury-rates.csv/{yr}/all'
               f'?type={rate_type}&field_tdr_date_value={yr}&download=true')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'gold-gcm3/1.0'})
            with urllib.request.urlopen(req, timeout=20) as r: raw = r.read().decode()
            frames.append(pd.read_csv(io.StringIO(raw), parse_dates=['Date'], index_col='Date'))
        except Exception as e: print(f'  Treasury {yr}: {e}')
    if not frames: return None
    out = pd.concat(frames).sort_index()
    out.index = pd.DatetimeIndex(out.index).tz_localize('UTC'); return out

def fetch_vix():
    url = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
    req = urllib.request.Request(url, headers={'User-Agent': 'gold-gcm3/1.0'})
    with urllib.request.urlopen(req, timeout=15) as r: raw = r.read().decode()
    df = pd.read_csv(io.StringIO(raw), parse_dates=['DATE'])
    s = df.set_index('DATE')['CLOSE'].sort_index()
    s.index = pd.DatetimeIndex(s.index).tz_localize('UTC'); return s

def fetch_eurusd():
    url = ('https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A'
           '?detail=dataonly&format=jsondata')
    req = urllib.request.Request(url,
          headers={'User-Agent': 'gold-gcm3/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r: data = json.loads(r.read())
    obs   = data['dataSets'][0]['series']['0:0:0:0:0']['observations']
    dates = data['structure']['dimensions']['observation'][0]['values']
    s = pd.Series({dates[int(k)]['id']: v[0] for k, v in obs.items()}).sort_index()
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).tz_localize('UTC')
    return s.astype(float)

# ── INDICATORS ────────────────────────────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))
def macd_hist(s, f=12, sl=26, sig=9):
    m = ema(s,f) - ema(s,sl); return m - ema(m,sig)
def atr(h, l, c, n=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def align_daily(series, idx):
    if series is None: return pd.Series(np.nan, index=idx)
    s = series.copy(); s.index = s.index.normalize()
    return s.reindex(idx, method='ffill')

def align_4h(series_4h, idx_15m):
    """4H value available after bar closes (open_ts + 4h). Forward-fill to 15m."""
    if series_4h is None: return pd.Series(np.nan, index=idx_15m)
    s = series_4h.copy()
    s.index = s.index + pd.Timedelta(hours=4)
    return s.reindex(idx_15m, method='ffill')

# ── TRADE SIMULATOR ───────────────────────────────────────────────────────
def simulate(entry, direction, atr_val, fh, fl, fc):
    sl_pts  = SL_MULT  * atr_val
    tp1_pts = TP1_MULT * atr_val
    tp2_pts = TP2_MULT * atr_val
    tp1_r   = tp1_pts / sl_pts
    tp2_r   = tp2_pts / sl_pts
    sl  = entry - sl_pts  if direction == 'LONG' else entry + sl_pts
    tp1 = entry + tp1_pts if direction == 'LONG' else entry - tp1_pts
    tp2 = entry + tp2_pts if direction == 'LONG' else entry - tp2_pts
    total_r, tp1_hit = 0.0, False
    for h, l, c in zip(fh, fl, fc):
        if np.isnan(h) or np.isnan(l): continue
        if direction == 'LONG':
            if l <= sl:  return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and h >= tp1: total_r += TP1_PCT*tp1_r; tp1_hit=True; sl=entry
            if tp1_hit and h >= tp2:     total_r += TP2_PCT*tp2_r; return total_r, 'TP2'
        else:
            if h >= sl:  return (total_r if tp1_hit else -1.0), 'SL'
            if not tp1_hit and l <= tp1: total_r += TP1_PCT*tp1_r; tp1_hit=True; sl=entry
            if tp1_hit and l <= tp2:     total_r += TP2_PCT*tp2_r; return total_r, 'TP2'
    last_c = fc[-1] if len(fc) and not np.isnan(fc[-1]) else entry
    pnl_r  = (last_c - entry) / sl_pts if direction == 'LONG' else (entry - last_c) / sl_pts
    total_r += (TP2_PCT if tp1_hit else 1.0) * pnl_r
    return total_r, 'TIME'

# ── STATS ─────────────────────────────────────────────────────────────────
def stats(df, label):
    if df is None or df.empty: print(f'\n  {label}: no trades'); return
    longs = df[df['dir']=='LONG']; shorts = df[df['dir']=='SHORT']
    wins  = df[df['r']>0]
    cumr  = df.sort_values('ts')['r'].cumsum()
    dd    = (cumr - cumr.cummax()).min()
    gw    = df[df['r']>0]['r'].sum(); gl = abs(df[df['r']<0]['r'].sum())
    pf    = gw / gl if gl > 0 else float('inf')
    nw    = max(1, (df['ts'].max() - df['ts'].min()).total_seconds() / (7*86400))
    exits = df['exit'].value_counts().to_dict()
    by_mo = df.groupby(df['ts'].dt.to_period('M'))['r'].agg(['sum','count'])
    by_sess = df.groupby('session')['r'].agg(['sum','count','mean'])
    print(f"\n{'═'*68}")
    print(f"  {label}")
    print(f"{'═'*68}")
    print(f"  Trades    {len(df):>4}   ({len(longs)}L / {len(shorts)}S)")
    print(f"  Total R   {df['r'].sum():>+8.2f}R")
    print(f"  Win rate  {len(wins)/len(df)*100:>7.1f}%")
    print(f"  Avg R     {df['r'].mean():>+8.3f}R/trade")
    print(f"  Per week  {len(df)/nw:>7.1f} trades/wk")
    print(f"  Max DD    {dd:>+8.2f}R")
    print(f"  Prof fac  {pf:>8.2f}")
    print(f"  Best/Worst {df['r'].max():>+.2f}R / {df['r'].min():>+.2f}R")
    print(f"  Exits: SL={exits.get('SL',0)}  TP2={exits.get('TP2',0)}  TIME={exits.get('TIME',0)}")
    if len(longs):
        lwr = len(longs[longs['r']>0])/len(longs)*100
        print(f"  LONG      {longs['r'].sum():>+8.2f}R  WR={lwr:.0f}%  n={len(longs)}")
    if len(shorts):
        swr = len(shorts[shorts['r']>0])/len(shorts)*100
        print(f"  SHORT     {shorts['r'].sum():>+8.2f}R  WR={swr:.0f}%  n={len(shorts)}")
    print(f"  By session:")
    for sess, row in by_sess.iterrows():
        n_w = len(df[(df['session']==sess) & (df['r']>0)])
        n_t = int(row['count'])
        print(f"    {sess:<8} {row['sum']:>+7.2f}R  WR={n_w/n_t*100:.0f}%  n={n_t}  avg={row['mean']:>+.3f}")
    print(f"  Monthly P&L:")
    for mo, (r, n) in by_mo.iterrows():
        bar = '█'*max(0,int(abs(r))) + ('▌' if abs(r)%1>=0.5 else '')
        sign = '+' if r>=0 else '-'
        print(f"    {mo}  {r:>+7.2f}R  ({int(n)}tr)  {'▲' if r>=0 else '▼'} {bar}")
    print(f"{'═'*68}")

# ═══════════════════════════════════════════════════════════════════════════
print('═'*68)
print('  GCM-3 — Gold Convergence Model v3 — Backtest')
print('═'*68)

print('Fetching 15m bars (XAU-USDT-SWAP)...', end='', flush=True)
bars15 = fetch_okx(INST, '15m', n_batches=150)
if bars15 is None: print(' FAILED'); sys.exit(1)
print(f' {len(bars15)} bars  {bars15.index[0].date()} – {bars15.index[-1].date()}')

print('Fetching 4H bars...', end='', flush=True)
bars4h = fetch_okx(INST, '4H', n_batches=12)
if bars4h is None: print(' FAILED'); sys.exit(1)
print(f' {len(bars4h)} bars')

print('Fetching TIPS 10Y...', end='', flush=True)
tips_df = fetch_treasury('daily_treasury_real_yield_curve')
tips_raw = pd.to_numeric(tips_df['10 YR'], errors='coerce').dropna() if tips_df is not None else None
print(f' OK ({len(tips_raw)} rows)' if tips_raw is not None else ' FAILED')

print('Fetching Nominal 10Y...', end='', flush=True)
nom_df = fetch_treasury('daily_treasury_yield_curve')
if nom_df is not None:
    col = next((c for c in nom_df.columns if '10 Yr' in c or '10 YR' in c), None)
    nom_raw = pd.to_numeric(nom_df[col], errors='coerce').dropna() if col else None
    print(f' OK' if nom_raw is not None else ' col not found')
else:
    nom_raw = None; print(' FAILED')

bkev_raw = None
if tips_raw is not None and nom_raw is not None:
    comb = tips_raw.rename('t').to_frame().join(nom_raw.rename('n'), how='inner')
    bkev_raw = (comb['n'] - comb['t']).dropna()
    print(f'Breakeven: {len(bkev_raw)} rows  latest={bkev_raw.iloc[-1]:.2f}%')

print('Fetching VIX (CBOE)...', end='', flush=True)
try:    vix_raw = fetch_vix();    print(f' OK  latest={vix_raw.iloc[-1]:.2f}')
except Exception as e: vix_raw = None; print(f' FAILED: {e}')

print('Fetching EUR/USD (ECB)...', end='', flush=True)
try:    eur_raw = fetch_eurusd(); print(f' OK  latest={eur_raw.iloc[-1]:.4f}')
except Exception as e: eur_raw = None; print(f' FAILED: {e}')

# ── BUILD 4H INDICATORS → forward-shift +4H → ffill to 15m ───────────────
c4h = bars4h['c']
a_ema50_4h = align_4h(ema(c4h, 50),            bars15.index)
a_hist_4h  = align_4h(macd_hist(c4h, 12,26,9), bars15.index)
a_rsi_4h   = align_4h(rsi(c4h, 14),            bars15.index)

# ── BUILD 15m INDICATORS ──────────────────────────────────────────────────
idx   = bars15.index
c15   = bars15['c']; h15 = bars15['h']; l15 = bars15['l']
v15   = bars15['vol']; o15 = bars15['o']

atr15    = atr(h15, l15, c15, 14)
hist15   = macd_hist(c15, 8, 21, 5)    # faster MACD for 15m entries
ema9_15  = ema(c15, 9)
ema21_15 = ema(c15, 21)
vols20   = v15.rolling(20).mean()

# ── ALIGN DAILY MACRO → forward-fill to 15m ──────────────────────────────
tips_15m = align_daily(tips_raw, idx)
bkev_15m = align_daily(bkev_raw, idx)
vix_15m  = align_daily(vix_raw,  idx)
eur_15m  = align_daily(eur_raw,  idx)

LB1D = BARS_PER_DAY
LB5D = BARS_PER_DAY * 5
START_BAR = max(60, LB5D + 10)

records = []; cooldown_until = 0; consec_loss = 0; last_loss_date = None

print(f'\nRunning GCM-3 ({len(bars15)} 15m bars, start_bar={START_BAR})...', end='', flush=True)

for i in range(START_BAR, len(idx) - MAX_HOLD - 2):
    ts = idx[i]

    # ── ABSOLUTE FILTERS ─────────────────────────────────────────────
    if ts.month == 6: continue                      # June: ABSOLUTE SKIP
    if ts.weekday() > 4: continue                   # weekend
    hf = ts.hour + ts.minute / 60
    is_ny     = NY[0]     <= hf < NY[1]
    is_london = LONDON[0] <= hf < LONDON[1]
    if not is_ny and not is_london: continue
    if i < cooldown_until: continue
    if consec_loss >= MAX_CONSEC_LOSS and last_loss_date == ts.date(): continue

    # ── M: MACRO SCORE ────────────────────────────────────────────────
    m1 = m2 = m3 = 0
    t_n = tips_15m.iloc[i]; t_1 = tips_15m.iloc[i - LB1D] if i >= LB1D else np.nan
    if not pd.isna(t_n) and not pd.isna(t_1):
        if t_n < t_1 - 0.001: m1 =  1   # real yield falling → gold bullish
        elif t_n > t_1 + 0.001: m1 = -1
    b_n = bkev_15m.iloc[i]; b_5 = bkev_15m.iloc[i - LB5D]
    if not pd.isna(b_n) and not pd.isna(b_5):
        if b_n > b_5 + 0.01:  m2 =  1   # breakeven rising → inflation bid for gold
        elif b_n < b_5 - 0.01: m2 = -1
    e_n = eur_15m.iloc[i]; e_5 = eur_15m.iloc[i - LB5D]
    if not pd.isna(e_n) and not pd.isna(e_5):
        if e_n > e_5 * 1.002:  m3 =  1   # dollar weakening → gold bullish
        elif e_n < e_5 * 0.998: m3 = -1
    M = m1 + m2 + m3

    # ── S: SWING SCORE (4H, lookahead-free) ──────────────────────────
    s1 = s2 = s3 = 0
    ve = a_ema50_4h.iloc[i]; vh = a_hist_4h.iloc[i]; vr = a_rsi_4h.iloc[i]
    if not pd.isna(ve): s1 = 1 if c15.iloc[i] > ve else -1
    if not pd.isna(vh): s2 = 1 if vh > 0 else -1
    if not pd.isna(vr):
        if vr > 55:   s3 =  1
        elif vr < 45: s3 = -1
    S = s1 + s2 + s3

    # ── E: ENTRY GATE (15m) ───────────────────────────────────────────
    e1 = e2 = e3 = 0
    # e1: MACD(8,21,5) histogram crosses zero in last 2 bars
    for k in [i, i-1]:
        if k < 1: continue
        hn = hist15.iloc[k]; hp = hist15.iloc[k-1]
        if pd.isna(hn) or pd.isna(hp): continue
        if hn > 0 and hp <= 0:   e1 =  1; break
        elif hn < 0 and hp >= 0: e1 = -1; break
    # e2: 5-bar range breakout with volume surge
    vsma = vols20.iloc[i]
    if not pd.isna(vsma) and vsma > 0 and i >= 5:
        hi5 = h15.iloc[i-5:i].max(); lo5 = l15.iloc[i-5:i].min()
        vvol = v15.iloc[i] / vsma
        if c15.iloc[i] > hi5 and vvol > 1.2:   e2 =  1
        elif c15.iloc[i] < lo5 and vvol > 1.2: e2 = -1
    # e3: 15m EMA9 vs EMA21 alignment
    if not pd.isna(ema9_15.iloc[i]) and not pd.isna(ema21_15.iloc[i]):
        e3 = 1 if ema9_15.iloc[i] > ema21_15.iloc[i] else -1
    E = e1 + e2 + e3

    # ── VIX ──────────────────────────────────────────────────────────
    vix_val = float(vix_15m.iloc[i]) if not pd.isna(vix_15m.iloc[i]) else 18.0

    # ── FIRE (session-tiered) ─────────────────────────────────────────
    s_min = LONDON_S if is_london else NY_S
    e_min = LONDON_E if is_london else NY_E
    direction = None
    if M >= M_MIN and m3 > 0 and S >= s_min and E >= e_min:
        direction = 'LONG'
    elif M <= SHORT_M and S <= SHORT_S and E <= SHORT_E and vix_val < VIX_GATE:
        direction = 'SHORT'
    if direction is None: continue

    # ── EXECUTE ──────────────────────────────────────────────────────
    av = float(atr15.iloc[i])
    if pd.isna(av) or av <= 0: continue
    entry = float(o15.iloc[i+1])
    if np.isnan(entry): entry = float(c15.iloc[i+1])
    if np.isnan(entry): continue

    fh = h15.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fl = l15.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    fc = c15.iloc[i+1:i+1+MAX_HOLD].values.astype(float)
    r, ex = simulate(entry, direction, av, fh, fl, fc)

    if r <= 0:
        if last_loss_date == ts.date(): consec_loss += 1
        else: consec_loss = 1; last_loss_date = ts.date()
    else:
        consec_loss = 0

    session = 'London' if is_london else 'NY'
    records.append({
        'ts': ts, 'month': ts.month, 'year': ts.year, 'session': session,
        'M': M, 'S': S, 'E': E, 'm1': m1, 'm2': m2, 'm3': m3,
        's1': s1, 's2': s2, 's3': s3, 'e1': e1, 'e2': e2, 'e3': e3,
        'vix': vix_val, 'dir': direction, 'entry': entry, 'atr': av, 'r': r, 'exit': ex,
    })
    cooldown_until = i + 1 + COOLDOWN

print(f' done — {len(records)} signals')

if not records:
    print('No trades fired.'); sys.exit(0)

df = pd.DataFrame(records)

# ── PRINT RESULTS ─────────────────────────────────────────────────────────
print(f"\n{'═'*68}")
print(f"  GCM-3  ·  Gold Convergence Model v3")
print(f"  Period: {df['ts'].min().date()} – {df['ts'].max().date()}")
print(f"  M: TIPS(daily) + Breakeven(5d) + EUR/USD(5d)  [mandatory: m3>0 for LONG]")
print(f"  S: 4H EMA50 + MACD hist + RSI  [London: S=+3, NY: S≥2]")
print(f"  E: 15m MACD cross + 5bar-breakout + EMA9/21  [London: E≥2, NY: E≥1]")
print(f"  VIX gate: SHORT blocked when VIX ≥ {VIX_GATE}")
print(f"  SL={SL_MULT}×ATR  TP1={TP1_MULT}×ATR(60%)  TP2={TP2_MULT}×ATR(40%)  Max={MAX_HOLD*15}min")
print(f"  Cooldown: {COOLDOWN*15}min  |  Loss halt: {MAX_CONSEC_LOSS} consecutive  |  June: SKIP")
print(f"{'═'*68}")

stats(df, 'GCM-3 — All Signals')

# ── FACTOR ANALYSIS ───────────────────────────────────────────────────────
w = df[df['r']>0]; l = df[df['r']<=0]
print(f"\n{'═'*68}")
print(f"  FACTOR ANALYSIS  (n={len(df)}, W={len(w)}, L={len(l)})")
print(f"{'═'*68}")
print(f"  {'Factor':<6}{'Name':<20} {'Avg':>6} {'Win':>6} {'Loss':>7}  {'Diff':>6}")
for col, name in [
    ('M','Macro total'), ('m1','TIPS direction'), ('m2','Breakeven 5d'), ('m3','EUR/USD 5d'),
    ('S','Swing total'), ('s1','4H EMA50'),       ('s2','4H MACD hist'),  ('s3','4H RSI'),
    ('E','Entry total'), ('e1','15m MACD cross'), ('e2','5bar breakout'), ('e3','EMA9/21'),
]:
    a  = df[col].mean()
    wm = w[col].mean()  if len(w) else float('nan')
    lm = l[col].mean()  if len(l) else float('nan')
    d  = wm - lm if not (np.isnan(wm) or np.isnan(lm)) else float('nan')
    flag = ' ◄ KEY' if not np.isnan(d) and abs(d) > 0.15 else ''
    print(f"  {col:<6}{name:<20} {a:>+6.2f} {wm:>+6.2f} {lm:>+7.2f}  {d:>+6.2f}{flag}")

print(f"\n  Done.")
