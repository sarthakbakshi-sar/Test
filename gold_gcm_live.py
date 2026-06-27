#!/usr/bin/env python3
"""
gold_gcm_live.py — GCM-3 Live Signal Scanner
=============================================
Prints current M / S / E layer scores and fires when
London or NY session conditions are fully met.

Fire conditions (must all be true simultaneously):
  LONG  London  M≥1 + m3>0 + S=+3 + E≥2
  LONG  NY      M≥1 + m3>0 + S≥2  + E≥1
  SHORT (both)  M≤-2 + S≤-2 + E≤-2 + VIX<22

June = ABSOLUTE SKIP. No trades, no exceptions.
"""

import urllib.request, json, time, io, sys
from datetime import datetime, timezone
import numpy as np
import pandas as pd

# ── CONSTANTS ────────────────────────────────────────────────────────
OKX_BASE = 'https://www.okx.com'
INST     = 'XAU-USDT-SWAP'

SL_MULT  = 2.0
TP1_MULT = 3.0
TP2_MULT = 5.0

LONDON = (7.0,  12.0)
NY     = (13.5, 20.0)

LONDON_S = 3;  LONDON_E = 2
NY_S     = 2;  NY_E     = 1
M_MIN    = 1
SHORT_M  = -2;  SHORT_S = -2;  SHORT_E = -2
VIX_GATE = 22.0

# ANSI colour helpers
C    = '\033[96m';  Y  = '\033[93m';  G = '\033[92m'
R    = '\033[91m';  DIM = '\033[90m'; B = '\033[1m';  RST = '\033[0m'

def sep():  print(f'{B}{C}{"═"*66}{RST}')
def hdr(s): sep(); print(f'{B}{C}  {s}{RST}'); sep()
def ok(s):  print(f'{G}  ✓{RST} {s}')
def warn(s):print(f'{Y}  ⚠{RST} {s}')
def err(s): print(f'{R}  ✗{RST} {s}')
def dim(s): print(f'  {DIM}{s}{RST}', end=' ', flush=True)

def sc_col(v, pos=1, neg=-1):
    if v >= pos: return G + B
    if v <= neg: return R + B
    return Y

def score_row(label, val, fmt, note=''):
    col = sc_col(val)
    print(f'    {DIM}{label:<24}{RST} {col}{fmt:>8}{RST}  {DIM}{note}{RST}')

# ── FETCHERS ─────────────────────────────────────────────────────────
def fetch_okx(inst, bar, n_batches=4):
    all_bars, after_ts = [], None
    hh = {'User-Agent': 'gold-gcm3-live/1.0'}
    for _ in range(n_batches):
        url = (f'{OKX_BASE}/api/v5/market/candles?instId={inst}&bar={bar}&limit=300'
               if after_ts is None else
               f'{OKX_BASE}/api/v5/market/history-candles?instId={inst}&bar={bar}'
               f'&after={after_ts}&limit=300')
        req = urllib.request.Request(url, headers=hh)
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read())
        bars = data.get('data', [])
        if not bars: break
        all_bars.extend(bars); after_ts = bars[-1][0]; time.sleep(0.22)
    if not all_bars: return None
    df = pd.DataFrame(all_bars, columns=['ts','o','h','l','c','vol','vc','vq','cf'])
    df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', utc=True)
    for col in ['o','h','l','c','vol']: df[col] = pd.to_numeric(df[col])
    return df.sort_values('ts').drop_duplicates('ts').set_index('ts')

def fetch_treasury(rate_type, years=(2025, 2026)):
    frames = []
    for yr in years:
        url = (f'https://home.treasury.gov/resource-center/data-chart-center/'
               f'interest-rates/daily-treasury-rates.csv/{yr}/all'
               f'?type={rate_type}&field_tdr_date_value={yr}&download=true')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'gold-gcm3-live/1.0'})
            with urllib.request.urlopen(req, timeout=20) as r: raw = r.read().decode()
            frames.append(pd.read_csv(io.StringIO(raw), parse_dates=['Date'], index_col='Date'))
        except Exception as e:
            print(f'    Treasury {yr}: {e}')
    if not frames: return None
    out = pd.concat(frames).sort_index()
    out.index = pd.DatetimeIndex(out.index).tz_localize('UTC')
    return out

def fetch_vix():
    url = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'
    req = urllib.request.Request(url, headers={'User-Agent': 'gold-gcm3-live/1.0'})
    with urllib.request.urlopen(req, timeout=15) as r: raw = r.read().decode()
    df = pd.read_csv(io.StringIO(raw), parse_dates=['DATE'])
    s = df.set_index('DATE')['CLOSE'].sort_index()
    s.index = pd.DatetimeIndex(s.index).tz_localize('UTC')
    return s

def fetch_eurusd():
    url = ('https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A'
           '?detail=dataonly&format=jsondata')
    req = urllib.request.Request(url,
          headers={'User-Agent': 'gold-gcm3-live/1.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r: data = json.loads(r.read())
    obs   = data['dataSets'][0]['series']['0:0:0:0:0']['observations']
    dates = data['structure']['dimensions']['observation'][0]['values']
    s = pd.Series({dates[int(k)]['id']: v[0] for k, v in obs.items()}).sort_index()
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).tz_localize('UTC')
    return s.astype(float)

# ── INDICATORS ───────────────────────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))
def macd_hist(s, f=12, sl=26, sig=9):
    m = ema(s, f) - ema(s, sl); return m - ema(m, sig)
def atr14(h, l, c):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()

# ─────────────────────────────────────────────────────────────────────
now_utc = datetime.now(timezone.utc)
hf = now_utc.hour + now_utc.minute / 60
hdr(f'GCM-3 LIVE SIGNAL  ·  {now_utc.strftime("%Y-%m-%d %H:%M")} UTC')

# ── JUNE SKIP ────────────────────────────────────────────────────────
if now_utc.month == 6:
    warn('JUNE — ABSOLUTE SKIP. No trades this month.')
    warn('Model resumes 1 July.')
    print()
    sys.exit(0)

# ── SESSION CHECK ─────────────────────────────────────────────────────
is_london  = LONDON[0] <= hf < LONDON[1]
is_ny      = NY[0]     <= hf < NY[1]
in_session = is_london or is_ny

if is_london:
    print(f'\n  Session  {B}{G}LONDON{RST}  07:00–12:00 UTC  (strict: S=+3, E≥2)')
    s_min = LONDON_S;  e_min = LONDON_E
elif is_ny:
    print(f'\n  Session  {B}{G}NEW YORK{RST}  13:30–20:00 UTC  (relaxed: S≥2, E≥1)')
    s_min = NY_S;  e_min = NY_E
else:
    print(f'\n  Session  {DIM}OFF-HOURS{RST}  (no trading — next window: London 07:00 UTC)')
    s_min = NY_S;  e_min = NY_E

# ── FETCH DATA ───────────────────────────────────────────────────────
print(f'\n  {DIM}Fetching market data...{RST}')

dim('15m bars...')
try:
    bars15 = fetch_okx(INST, '15m', n_batches=6)
    ok(f'15m  {len(bars15)} bars  last={bars15.index[-1].strftime("%H:%M")} UTC  '
       f'price={bars15["c"].iloc[-1]:,.2f}')
except Exception as e:
    err(f'15m fetch failed: {e}'); sys.exit(1)

dim('4H bars...')
try:
    bars4h = fetch_okx(INST, '4H', n_batches=5)
    ok(f'4H   {len(bars4h)} bars  last={bars4h.index[-1].strftime("%Y-%m-%d %H:%M")} UTC')
except Exception as e:
    err(f'4H fetch failed: {e}'); sys.exit(1)

dim('TIPS 10Y...')
tips_raw = None
try:
    tips_df  = fetch_treasury('daily_treasury_real_yield_curve')
    tips_raw = pd.to_numeric(tips_df['10 YR'], errors='coerce').dropna() if tips_df is not None else None
    ok(f'TIPS  latest={tips_raw.iloc[-1]:.2f}%  ({tips_raw.index[-1].date()})')
except Exception as e:
    warn(f'TIPS failed: {e}')

dim('Nominal 10Y...')
nom_raw = None
try:
    nom_df = fetch_treasury('daily_treasury_yield_curve')
    if nom_df is not None:
        col = next((c for c in nom_df.columns if '10 Yr' in c or '10 YR' in c), None)
        nom_raw = pd.to_numeric(nom_df[col], errors='coerce').dropna() if col else None
    ok(f'Nominal  latest={nom_raw.iloc[-1]:.2f}%')
except Exception as e:
    warn(f'Nominal failed: {e}')

bkev_raw = None
if tips_raw is not None and nom_raw is not None:
    comb     = tips_raw.rename('t').to_frame().join(nom_raw.rename('n'), how='inner')
    bkev_raw = (comb['n'] - comb['t']).dropna()
    print(f'  {G}✓{RST} Breakeven  {bkev_raw.iloc[-1]:.2f}%')

dim('VIX (CBOE)...')
vix_raw = None
try:
    vix_raw = fetch_vix()
    ok(f'VIX  {vix_raw.iloc[-1]:.2f}  ({vix_raw.index[-1].date()})')
except Exception as e:
    warn(f'VIX failed: {e}')

dim('EUR/USD (ECB)...')
eur_raw = None
try:
    eur_raw = fetch_eurusd()
    ok(f'EUR/USD  {eur_raw.iloc[-1]:.4f}  ({eur_raw.index[-1].date()})')
except Exception as e:
    warn(f'EUR/USD failed: {e}')

# ── BUILD INDICATORS ─────────────────────────────────────────────────
c15    = bars15['c']; h15 = bars15['h']; l15 = bars15['l']; v15 = bars15['vol']
atr15v  = atr14(h15, l15, c15)
hist15  = macd_hist(c15, 8, 21, 5)
ema9_15 = ema(c15, 9)
ema21_15= ema(c15, 21)
vols20  = v15.rolling(20).mean()

c4h     = bars4h['c']
ema50_4h= ema(c4h, 50)
hist4h  = macd_hist(c4h, 12, 26, 9)
rsi4h   = rsi(c4h, 14)

# Use only completed 4H bars (bar close time ≤ now)
now_ts       = pd.Timestamp.now(tz='UTC')
completed_4h = bars4h[bars4h.index + pd.Timedelta(hours=4) <= now_ts]
if len(completed_4h) == 0:
    completed_4h = bars4h.iloc[:-1]

cur_price = float(c15.iloc[-1])
cur_atr   = float(atr15v.iloc[-1])

# ── M · MACRO SCORE (daily sources) ─────────────────────────────────
m1 = m2 = m3 = 0
tips_note = bkev_note = eur_note = 'no data'

if tips_raw is not None and len(tips_raw) >= 2:
    t_n = float(tips_raw.iloc[-1]); t_1 = float(tips_raw.iloc[-2])
    delta_t = t_n - t_1
    m1 = 1 if t_n < t_1 - 0.001 else (-1 if t_n > t_1 + 0.001 else 0)
    tips_note = f'{t_n:.2f}% prev={t_1:.2f}% Δ{delta_t:+.3f}'

if bkev_raw is not None and len(bkev_raw) >= 6:
    b_n = float(bkev_raw.iloc[-1]); b_5 = float(bkev_raw.iloc[-6])
    delta_b = b_n - b_5
    m2 = 1 if b_n > b_5 + 0.01 else (-1 if b_n < b_5 - 0.01 else 0)
    bkev_note = f'{b_n:.2f}% 5d-ago={b_5:.2f}% Δ{delta_b:+.3f}'

if eur_raw is not None and len(eur_raw) >= 6:
    e_n = float(eur_raw.iloc[-1]); e_5 = float(eur_raw.iloc[-6])
    ratio = e_n / e_5
    m3 = 1 if ratio > 1.002 else (-1 if ratio < 0.998 else 0)
    eur_note = f'{e_n:.4f} 5d-ago={e_5:.4f} ratio={ratio:.4f}'

M = m1 + m2 + m3

# ── S · SWING SCORE (last completed 4H bar) ─────────────────────────
s1 = s2 = s3 = 0
ema50_note = hist4h_note = rsi4h_note = 'no data'

idx4h = completed_4h.index[-1]
ve = float(ema50_4h.loc[idx4h]) if idx4h in ema50_4h.index else np.nan
vh = float(hist4h.loc[idx4h])   if idx4h in hist4h.index   else np.nan
vr = float(rsi4h.loc[idx4h])    if idx4h in rsi4h.index    else np.nan

if not np.isnan(ve):
    s1 = 1 if cur_price > ve else -1
    ema50_note = f'price {cur_price:,.1f} vs 4H EMA50 {ve:,.1f}'
if not np.isnan(vh):
    s2 = 1 if vh > 0 else -1
    hist4h_note = f'hist={vh:.4f}'
if not np.isnan(vr):
    s3 = 1 if vr > 55 else (-1 if vr < 45 else 0)
    rsi4h_note = f'RSI={vr:.1f}'

S = s1 + s2 + s3

# ── E · ENTRY GATE (15m — last 2 completed bars) ────────────────────
e1 = e2 = e3 = 0
e1_note = e2_note = e3_note = ''

# e1: MACD(8,21,5) histogram crosses zero in last 2 bars
for k in [-1, -2]:
    hn = float(hist15.iloc[k]); hp = float(hist15.iloc[k-1])
    if np.isnan(hn) or np.isnan(hp): continue
    if hn > 0 and hp <= 0:
        e1 = 1;  e1_note = f'bull cross bar{k} (h={hn:.4f})'; break
    elif hn < 0 and hp >= 0:
        e1 = -1; e1_note = f'bear cross bar{k} (h={hn:.4f})'; break
if not e1_note:
    e1_note = f'no cross  hist={float(hist15.iloc[-1]):.4f}'

# e2: 5-bar range breakout with volume surge (backtest: prev 5 bars vs current bar)
vsma = float(vols20.iloc[-1])
if not np.isnan(vsma) and vsma > 0:
    hi5  = float(h15.iloc[-6:-1].max())
    lo5  = float(l15.iloc[-6:-1].min())
    vvol = float(v15.iloc[-1]) / vsma
    c_now = float(c15.iloc[-1])
    if c_now > hi5 and vvol > 1.2:
        e2 = 1;  e2_note = f'bull breakout (vol×{vvol:.2f}, hi5={hi5:,.1f})'
    elif c_now < lo5 and vvol > 1.2:
        e2 = -1; e2_note = f'bear breakout (vol×{vvol:.2f}, lo5={lo5:,.1f})'
    else:
        e2_note = f'no breakout  vol×{vvol:.2f}  range [{lo5:,.1f}–{hi5:,.1f}]'

# e3: EMA9 vs EMA21 alignment on 15m
e9  = float(ema9_15.iloc[-1]); e21 = float(ema21_15.iloc[-1])
if not np.isnan(e9) and not np.isnan(e21):
    e3 = 1 if e9 > e21 else -1
    e3_note = f'EMA9={e9:,.2f} vs EMA21={e21:,.2f}'

E = e1 + e2 + e3

# ── VIX ─────────────────────────────────────────────────────────────
vix_val   = float(vix_raw.iloc[-1]) if vix_raw is not None else 18.0
vix_blocked = vix_val >= VIX_GATE

# ── PRINT SCORES ─────────────────────────────────────────────────────
print(f'\n{B}{C}  ── M · MACRO BIAS  (daily data){RST}')
score_row('m1  TIPS real yield',  m1, f'{m1:+d}/±1', tips_note)
score_row('m2  Breakeven 5d',     m2, f'{m2:+d}/±1', bkev_note)
score_row('m3  EUR/USD 5d  ★',   m3, f'{m3:+d}/±1', eur_note)
mc = sc_col(M, 1, -2)
print(f'    {DIM}{"M total  [-3 .. +3]":<24}{RST} {mc}{M:+d}/±3{RST}  '
      f'{DIM}LONG: M≥+1 + m3>0  |  SHORT: M≤-2{RST}')

print(f'\n{B}{C}  ── S · SWING STATE  (4H  bar {idx4h.strftime("%Y-%m-%d %H:%M")} UTC){RST}')
score_row('s1  4H EMA50',         s1, f'{s1:+d}/±1', ema50_note)
score_row('s2  4H MACD hist',     s2, f'{s2:+d}/±1', hist4h_note)
score_row('s3  4H RSI zone',      s3, f'{s3:+d}/±1', rsi4h_note)
sc2 = sc_col(S, s_min, SHORT_S)
print(f'    {DIM}{"S total  [-3 .. +3]":<24}{RST} {sc2}{S:+d}/±3{RST}  '
      f'{DIM}London: S=+3  |  NY: S≥+2{RST}')

print(f'\n{B}{C}  ── E · ENTRY GATE  (15m  last bar {bars15.index[-1].strftime("%H:%M")} UTC){RST}')
score_row('e1  MACD(8,21,5) cross',  e1, f'{e1:+d}/±1', e1_note)
score_row('e2  5-bar breakout+vol',  e2, f'{e2:+d}/±1', e2_note)
score_row('e3  EMA9 vs EMA21',       e3, f'{e3:+d}/±1', e3_note)
ec = sc_col(E, e_min, SHORT_E)
print(f'    {DIM}{"E total  [-3 .. +3]":<24}{RST} {ec}{E:+d}/±3{RST}  '
      f'{DIM}London: E≥+2  |  NY: E≥+1{RST}')

print(f'\n{B}{C}  ── VIX{RST}')
vc = (R + B) if vix_blocked else (G + B)
vix_status = 'SHORT BLOCKED ≥22' if vix_blocked else 'SHORT ok <22'
print(f'    {DIM}{"VIX (CBOE daily)":<24}{RST} {vc}{vix_val:.2f}{RST}  {DIM}{vix_status}{RST}')

# ── FIRE CHECK ───────────────────────────────────────────────────────
direction      = None
no_fire_reason = []

if in_session:
    if M >= M_MIN and m3 > 0 and S >= s_min and E >= e_min:
        direction = 'LONG'
    if M <= SHORT_M and S <= SHORT_S and E <= SHORT_E and not vix_blocked:
        direction = 'SHORT'

if direction is None:
    if not in_session:
        no_fire_reason.append('off-hours')
    else:
        # Explain why LONG did not fire
        if M < M_MIN:    no_fire_reason.append(f'M={M} (need ≥{M_MIN})')
        if m3 <= 0:      no_fire_reason.append(f'm3={m3} (EUR/USD mandatory >0)')
        if S < s_min:    no_fire_reason.append(f'S={S} (need ≥{s_min})')
        if E < e_min:    no_fire_reason.append(f'E={E} (need ≥{e_min})')

# ── PRINT RESULT ─────────────────────────────────────────────────────
print()
sep()

if direction == 'LONG':
    sl_pts  = SL_MULT  * cur_atr
    tp1_pts = TP1_MULT * cur_atr
    tp2_pts = TP2_MULT * cur_atr
    entry   = cur_price
    print(f'{B}{G}  ▲  LONG SIGNAL FIRES  '
          f'({"London" if is_london else "NY"})  M={M}  S={S}  E={E}{RST}')
    print()
    print(f'  Current price  {entry:>12,.2f}')
    print(f'  ATR(14, 15m)   {cur_atr:>12,.2f}')
    print()
    print(f'  Entry  (market){entry:>12,.2f}')
    print(f'  SL             {entry - sl_pts:>12,.2f}  '
          f'({DIM}−{sl_pts:.2f}  =  2.0×ATR{RST})')
    print(f'  TP1  60% size  {entry + tp1_pts:>12,.2f}  '
          f'({DIM}+{tp1_pts:.2f}  =  +1.5R when hit → trail SL to entry{RST})')
    print(f'  TP2  40% size  {entry + tp2_pts:>12,.2f}  '
          f'({DIM}+{tp2_pts:.2f}  =  +2.5R when hit{RST})')
    print()
    print(f'  {DIM}$300 risk → TP1 ${300*1.5:.0f}  |  TP2 ${300*2.5:.0f}  |  Max hold 8h{RST}')

elif direction == 'SHORT':
    sl_pts  = SL_MULT  * cur_atr
    tp1_pts = TP1_MULT * cur_atr
    tp2_pts = TP2_MULT * cur_atr
    entry   = cur_price
    print(f'{B}{R}  ▼  SHORT SIGNAL FIRES  '
          f'({"London" if is_london else "NY"})  M={M}  S={S}  E={E}{RST}')
    print()
    print(f'  Current price  {entry:>12,.2f}')
    print(f'  ATR(14, 15m)   {cur_atr:>12,.2f}')
    print()
    print(f'  Entry  (market){entry:>12,.2f}')
    print(f'  SL             {entry + sl_pts:>12,.2f}  '
          f'({DIM}+{sl_pts:.2f}  =  2.0×ATR{RST})')
    print(f'  TP1  60% size  {entry - tp1_pts:>12,.2f}  '
          f'({DIM}−{tp1_pts:.2f}  =  +1.5R when hit → trail SL to entry{RST})')
    print(f'  TP2  40% size  {entry - tp2_pts:>12,.2f}  '
          f'({DIM}−{tp2_pts:.2f}  =  +2.5R when hit{RST})')
    print()
    print(f'  {DIM}$300 risk → TP1 ${300*1.5:.0f}  |  TP2 ${300*2.5:.0f}  |  Max hold 8h{RST}')

else:
    sess_label = 'London' if is_london else ('NY' if is_ny else 'off-hours')
    print(f'{B}{Y}  ─  NO SIGNAL  ({sess_label}){RST}')
    print()
    if no_fire_reason:
        print(f'  Why: {", ".join(no_fire_reason)}')
    print(f'  Scores:  M={M}  S={S}  E={E}  m3={m3}  VIX={vix_val:.1f}')
    if in_session:
        # Show exactly what's missing for LONG
        need = []
        if M  < M_MIN:  need.append(f'M≥{M_MIN}  (have {M})')
        if m3 <= 0:     need.append(f'm3>0  (have {m3})')
        if S  < s_min:  need.append(f'S≥{s_min}  (have {S})')
        if E  < e_min:  need.append(f'E≥{e_min}  (have {E})')
        if need:
            print(f'  LONG missing: {" · ".join(need)}')
        # Show what's missing for SHORT
        need_s = []
        if M  > SHORT_M:    need_s.append(f'M≤{SHORT_M}  (have {M})')
        if S  > SHORT_S:    need_s.append(f'S≤{SHORT_S}  (have {S})')
        if E  > SHORT_E:    need_s.append(f'E≤{SHORT_E}  (have {E})')
        if vix_blocked:     need_s.append(f'VIX<{VIX_GATE}  (have {vix_val:.1f})')
        if need_s:
            print(f'  SHORT missing: {" · ".join(need_s)}')

sep()
print(f'  {DIM}SL=2×ATR  TP1=3×ATR(60%)  TP2=5×ATR(40%)  Max hold 8h  Cooldown 2h{RST}')
print(f'  {DIM}London 07-12 UTC  ·  NY 13:30-20 UTC  ·  June = ABSOLUTE SKIP{RST}')
print(f'  {DIM}Expected: ~1.5 trades/wk (NY)  WR≈59%  AvgR=+0.43{RST}')
print()
