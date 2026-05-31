#!/usr/bin/env python3
"""
Forex RORO Backtest V15 — Expanded 9-Pair Universe
===================================================
Allowed pairs (FTMO-verified):
  AUD/CHF  AUD/JPY  AUD/NZD  AUD/USD
  CAD/CHF  CAD/JPY
  CHF/JPY
  EUR/AUD  EUR/CAD

Dropped: GBP/JPY (removed from allowed list → no ORB)
Strategy: TREND (VWAP pullback) on all 9 pairs — both sessions.

Key insight: doubling the pair universe roughly doubles trade frequency
and expected monthly P&L, pushing the challenge pass rate toward 50%+.

MACRO SCORE LOGIC PER PAIR
===========================
Standard (score ≥6 = LONG, ≤-6 = SHORT, each component ±2):
  AUD/JPY  : copper + SPX + TNX direction           (existing)
  AUD/CHF  : copper + SPX + VIX inv                 (risk-on = AUD up vs CHF)
  AUD/NZD  : copper + SPX + oil                     (AUD-NZD relative, weak signal)
  AUD/USD  : copper + SPX + DXY inv                 (commodity vs reserve currency)
  CAD/CHF  : oil + SPX + VIX inv                    (existing)
  CAD/JPY  : oil + TNX + SPX                        (existing)
  CHF/JPY  : SPX + VIX inv + TNX                    (risk-on = JPY sold harder)

Inverted pairs (score ≥6 = LONG = risk-off, ≤-6 = SHORT = risk-on):
  EUR/AUD  : copper inv + SPX inv + VIX             (risk-on = SHORT EUR/AUD)
  EUR/CAD  : oil inv   + SPX inv + VIX             (risk-on + oil-bull = SHORT EUR/CAD)

REGIME RULES
============
OIL_WEAK (oil < EMA50):
  - Skip CAD/JPY (CAD weakens with oil — avoid carry pair)
  - CAD/CHF short threshold → -4 (oil bear = CAD already weak)
  - EUR/CAD LONG natural (CAD weak → EUR/CAD rises naturally)

VIX_SPIKE (VIX > EMA20 × 1.10):
  - Skip AUD/JPY, CAD/JPY, CHF/JPY (JPY pairs spike unpredictably)
  - 0.50× size on remaining pairs

RISK TIERS
==========
CAD/CHF 4%  AUD/JPY 3%  AUD/CHF 3%  EUR/AUD 3%  EUR/CAD 3%  AUD/USD 2.5%
CAD/JPY 2%  CHF/JPY 2%  AUD/NZD 1.5%

FLOOR PROTECTION (triple layer — from V14)
==========================================
Layer 1: absolute equity (≤97%→0.75×, ≤95%→0.55×, ≤93%→0.35×, ≤91%→0.25×)
Layer 2: monthly progress (≥9.5%→0.30×, ≥10%→0.20× lock)
Layer 3: pre-trade worst-case — skip if full SL pushes monthly past -9.9%
Daily cap: 5.0% equity (FTMO hard limit)
Monthly stop: -9.5%
"""

import os, time, warnings
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from scipy import stats

warnings.filterwarnings('ignore')

CACHE_DIR = '/home/user/Test/forex_cache'
os.makedirs(CACHE_DIR, exist_ok=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWELVE_KEY  = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT     = 120_000
SL_MULT     = 0.75
TP1_MULT    = 1.50
TP2_MULT    = 2.50
TP1_FRAC    = 0.60
TP2_FRAC    = 0.40
SCORE_LONG  =  6.0
SCORE_SHORT = -6.0
TIME_BARS   = 10

# ── V15 RISK TIERS — 9 pairs, all trend ───────────────────────────────────────
RISK_TIER = {
    'CAD/CHF': 0.040,   # best historically — 2× V13
    'AUD/JPY': 0.030,   # proven carry + copper — 2× V13
    'AUD/CHF': 0.030,   # RORO: copper + safe-haven spread
    'EUR/AUD': 0.030,   # inverted RORO: risk-on = SHORT EUR/AUD
    'EUR/CAD': 0.030,   # inverted RORO: oil-on = SHORT EUR/CAD
    'AUD/USD': 0.025,   # copper vs reserve currency
    'CAD/JPY': 0.020,   # proven but OIL_WEAK-skipped — 2× V13
    'CHF/JPY': 0.020,   # dual safe-haven spread, uncertain
    'AUD/NZD': 0.015,   # relative value, weakest RORO signal
}

# ── REGIME CONFIG ─────────────────────────────────────────────────────────────
OIL_WEAK_SHORT_THRESH = -4.0
VIX_SPIKE_MULT        = 0.10
VIX_SPIKE_SIZE        = 0.50

# ── FTMO RULES ────────────────────────────────────────────────────────────────
FTMO_TARGET      = ACCOUNT * 1.10
FTMO_FLOOR       = ACCOUNT * 0.90
FTMO_DAILY_LIMIT = ACCOUNT * 0.05
FTMO_DAILY_CAP   = 0.050

CHAL_PROTECT_AT  =  10.0
CHAL_CAUTIOUS_AT =   9.5
CHAL_STOP_AT     =  -9.5

# ── PAIR UNIVERSE ─────────────────────────────────────────────────────────────
ACTIVE_PAIRS = ['CAD/CHF', 'AUD/JPY', 'CAD/JPY',
                'EUR/CAD', 'AUD/NZD', 'CHF/JPY']

PAIRS_MACRO = ['AUD/JPY', 'AUD/CHF', 'AUD/NZD', 'AUD/USD',
               'CAD/CHF', 'CAD/JPY', 'CHF/JPY', 'EUR/AUD', 'EUR/CAD']

MIN_DATE = pd.Timestamp('2024-09-01', tz='UTC')

SESSIONS = {
    'london_open': {'vwap_h': 7.0,  'entry_s': 7.5,  'entry_e': 10.0},
    'ny_overlap':  {'vwap_h': 13.5, 'entry_s': 14.0, 'entry_e': 17.0},
}
PAIR_SESSIONS = {p: ['london_open', 'ny_overlap'] for p in ACTIVE_PAIRS}

PAIR_ATR = {
    'AUD/JPY': (0.0004, 0.0120),
    'AUD/CHF': (0.0002, 0.0080),
    'AUD/NZD': (0.0001, 0.0050),
    'AUD/USD': (0.0003, 0.0100),
    'CAD/CHF': (0.0002, 0.0080),
    'CAD/JPY': (0.0004, 0.0120),
    'CHF/JPY': (0.0003, 0.0120),
    'EUR/AUD': (0.0003, 0.0120),
    'EUR/CAD': (0.0003, 0.0100),
}

JPY_PAIRS       = {'AUD/JPY', 'CAD/JPY', 'CHF/JPY'}
LONG_ONLY_PAIRS = {'AUD/JPY', 'CAD/JPY'}   # CHF/JPY: both directions (CHF ≠ pure carry)

# OIL_WEAK: pairs to skip entirely
OIL_WEAK_SKIP = {'CAD/JPY'}
# VIX_SPIKE: JPY pairs become unpredictable — skip all three
VIX_SPIKE_SKIP = {'AUD/JPY', 'CAD/JPY', 'CHF/JPY'}

# ── MATH HELPERS ──────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d    = s.diff()
    gain = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

def is_nfp_day(date):
    d = pd.Timestamp(date)
    return d.weekday() == 4 and d.day <= 7

def is_high_vix_day(date, vix_series):
    v = vix_series.get(pd.Timestamp(date).normalize(), np.nan)
    return not pd.isna(v) and float(v) > 30.0

def jpy_carry_guard(date, usdjpy_series):
    if usdjpy_series is None or len(usdjpy_series) < 6:
        return False
    d     = pd.Timestamp(date).normalize()
    avail = usdjpy_series.index[usdjpy_series.index <= d]
    if len(avail) < 6:
        return False
    recent = float(usdjpy_series.iloc[usdjpy_series.index.get_loc(avail[-1])])
    past   = float(usdjpy_series.iloc[usdjpy_series.index.get_loc(avail[-6])])
    if past == 0 or pd.isna(recent) or pd.isna(past):
        return False
    return (recent - past) / past < -0.02

def get_size_mult(macro_abs):
    if macro_abs >= 8.0: return 1.50
    if macro_abs >= 6.0: return 1.25
    return 1.00

# ── DATA LOADERS ──────────────────────────────────────────────────────────────
def load_15m(symbol):
    slug  = symbol.replace('/', '') + '__15min_v2'
    fpath = os.path.join(CACHE_DIR, slug + '.csv')
    if os.path.exists(fpath):
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        return df
    print(f"--- Fetching {symbol} 15min (not cached) ---")
    all_rows, end_dt = [], None
    for page in range(1, 13):
        params = dict(symbol=symbol, interval='15min', outputsize=5000,
                      apikey=TWELVE_KEY, timezone='UTC')
        if end_dt:
            params['end_date'] = end_dt
        try:
            r = requests.get('https://api.twelvedata.com/time_series',
                             params=params, timeout=30)
            j = r.json()
        except Exception as e:
            print(f"  Error: {e}"); break
        if j.get('status') == 'error':
            print(f"  API: {j.get('message', '?')}"); break
        vals = j.get('values', [])
        if not vals:
            break
        all_rows.extend(vals)
        oldest = vals[-1]['datetime']
        print(f"  [{symbol}] p{page}: {oldest} → {vals[0]['datetime']}")
        dt_oldest = pd.Timestamp(oldest, tz='UTC')
        end_dt = (dt_oldest - pd.Timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        time.sleep(12)
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows)
    df['datetime'] = pd.to_datetime(df['datetime'], utc=True)
    cols = ['open', 'high', 'low', 'close']
    if 'volume' in all_rows[0]:
        cols.append('volume')
    df = df.set_index('datetime').sort_index()[cols].astype(float)
    df = df[~df.index.duplicated()]
    df.to_csv(fpath)
    return df

def load_1h(pair):
    sym   = pair.replace('/', '') + '=X'
    fpath = os.path.join(CACHE_DIR, sym + '__1h_yf.csv')
    if os.path.exists(fpath):
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        return df
    try:
        raw = yf.download(sym, period='2y', interval='1h',
                          progress=False, auto_adjust=True)
        if raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)
        df = raw[['Open', 'High', 'Low', 'Close']].copy()
        df.columns = ['open', 'high', 'low', 'close']
        df.index.name = 'datetime'
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df = df.dropna()
        df.to_csv(fpath)
        return df
    except:
        return pd.DataFrame()

def load_macro():
    print("Loading macro data (yfinance)...")
    macro = {}
    fetch_map = {
        'spx': '^GSPC', 'vix': '^VIX', 'dxy': 'DX-Y.NYB',
        'tnx': '^TNX',  'oil': 'CL=F', 'cop': 'HG=F',
        'aud': 'AUDUSD=X', 'nzd': 'NZDUSD=X',
        'usdjpy': 'JPY=X', 'gbp': 'GBPUSD=X',
    }
    for k, sym in fetch_map.items():
        try:
            d = yf.download(sym, period='5y', interval='1d',
                            progress=False, auto_adjust=True)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.droplevel(1)
            macro[k] = d['Close'].dropna()
            print(f"  {sym:<14} {len(macro[k])} days")
        except Exception as e:
            print(f"  Warning: {sym} failed ({e})")
            macro[k] = pd.Series(dtype=float)
    for pair in PAIRS_MACRO:
        sym = pair.replace('/', '') + '=X'
        try:
            d = yf.download(sym, period='5y', interval='1d',
                            progress=False, auto_adjust=True)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.droplevel(1)
            macro[pair] = d['Close'].dropna()
        except:
            macro[pair] = pd.Series(dtype=float)
    return macro

# ── REGIME DETECTION ──────────────────────────────────────────────────────────
def compute_regime_series(macro_all):
    oil = macro_all.get('oil', pd.Series(dtype=float))
    cop = macro_all.get('cop', pd.Series(dtype=float))
    vix = macro_all.get('vix', pd.Series(dtype=float))
    spx = macro_all.get('spx', pd.Series(dtype=float))
    if len(oil) < 60 or len(vix) < 30:
        return pd.Series(dtype=str)
    idx = spx.index
    def al(s): return s.reindex(idx, method='ffill')
    oil_bull  = al(oil) > ema(al(oil), 50)
    cop_bull  = al(cop) > ema(al(cop), 50)
    spx_bull  = al(spx) > ema(al(spx), 50)
    vix_e20   = ema(al(vix), 20)
    vix_spike = al(vix) > vix_e20 * (1 + VIX_SPIKE_MULT)
    regime = pd.Series('NORMAL', index=idx)
    regime[oil_bull & cop_bull & spx_bull & ~vix_spike] = 'BULL'
    regime[~oil_bull & ~vix_spike] = 'OIL_WEAK'
    regime[vix_spike] = 'VIX_SPIKE'
    return regime.shift(1, fill_value='NORMAL')

_REGIME_LOOKUP: dict = {}

def get_regime(date):
    d = pd.Timestamp(date).normalize()
    if d.tz is not None:
        d = d.tz_localize(None)
    v = _REGIME_LOOKUP.get(d)
    if v is None:
        for offset in range(1, 6):
            v = _REGIME_LOOKUP.get(d - pd.Timedelta(days=offset))
            if v is not None:
                break
    return v if v is not None else 'NORMAL'

# ── MACRO SCORES — all 9 pairs ────────────────────────────────────────────────
def build_macro(pair, macro):
    p   = macro.get(pair, pd.Series(dtype=float))
    spx = macro.get('spx', pd.Series(dtype=float))
    vix = macro.get('vix', pd.Series(dtype=float))
    oil = macro.get('oil', pd.Series(dtype=float))
    cop = macro.get('cop', pd.Series(dtype=float))
    tnx = macro.get('tnx', pd.Series(dtype=float))
    dxy = macro.get('dxy', pd.Series(dtype=float))
    if len(p) < 60:
        return pd.Series(dtype=float)
    idx = p.index
    s1  = pd.Series(np.where(p > ema(p, 50),  1.0, -1.0), index=idx)
    s5  = pd.Series(np.where(p > ema(p, 200), 1.0, -1.0), index=idx)

    def safe(series, n_ema=None, inv=False, compare_shift=None):
        if len(series) < 10:
            return pd.Series(0.0, index=idx)
        s = series.reindex(idx, method='ffill')
        if compare_shift is not None:
            v = np.where(s > s.shift(compare_shift), 1.0, -1.0)
        elif n_ema:
            v = np.where(s > ema(s, n_ema), 1.0, -1.0)
        else:
            v = np.where(s > 0, 1.0, -1.0)
        if inv:
            v = -v
        return pd.Series(v, index=idx)

    # ── Per-pair macro score components ──────────────────────────────────────
    if pair == 'CAD/CHF':
        s2 = safe(oil, n_ema=50)
        s3 = safe(spx, n_ema=50)
        s4 = safe(vix, n_ema=20, inv=True)

    elif pair == 'CAD/JPY':
        s2 = safe(oil, n_ema=50)
        s3 = safe(tnx, compare_shift=10)
        s4 = safe(spx, n_ema=50)

    elif pair == 'AUD/JPY':
        s2 = safe(cop, n_ema=50)
        s3 = safe(spx, n_ema=50)
        s4 = safe(tnx, compare_shift=10)

    elif pair == 'AUD/CHF':
        # Copper bull + risk-on → AUD up vs CHF (safe haven)
        s2 = safe(cop, n_ema=50)
        s3 = safe(spx, n_ema=50)
        s4 = safe(vix, n_ema=20, inv=True)   # VIX up = CHF rally = AUD/CHF down

    elif pair == 'AUD/NZD':
        # Both commodity currencies; AUD more copper/China, NZD more dairy
        # Copper outperformance → AUD/NZD up
        s2 = safe(cop, n_ema=50)
        s3 = safe(oil, n_ema=50)             # AUD also oil-exposed
        s4 = safe(spx, n_ema=50)

    elif pair == 'AUD/USD':
        # Commodity/risk currency vs reserve currency
        s2 = safe(cop, n_ema=50)
        s3 = safe(spx, n_ema=50)
        s4 = safe(dxy, n_ema=50, inv=True)   # DXY up → USD strong → AUD/USD down

    elif pair == 'CHF/JPY':
        # Both safe havens; JPY is deeper carry funding → JPY sold harder in risk-on
        # CHF/JPY rises in risk-on (JPY weakens more than CHF)
        s2 = safe(spx, n_ema=50)
        s3 = safe(vix, n_ema=20, inv=True)   # VIX down = risk-on = CHF/JPY up
        s4 = safe(tnx, compare_shift=10)     # higher yields → JPY weakens → CHF/JPY up

    elif pair == 'EUR/AUD':
        # EUR defensive, AUD risk/commodity — INVERTED
        # Risk-on → AUD up, EUR/AUD DOWN → score ≤-6 → SHORT EUR/AUD
        # Risk-off → EUR up, EUR/AUD UP  → score ≥+6 → LONG EUR/AUD
        s2 = safe(cop, n_ema=50, inv=True)   # copper bull → AUD up → EUR/AUD down
        s3 = safe(spx, n_ema=50, inv=True)   # risk-on → EUR/AUD down
        s4 = safe(vix, n_ema=20)             # VIX up → EUR/AUD up (EUR safe haven)

    elif pair == 'EUR/CAD':
        # EUR defensive, CAD oil/commodity — INVERTED
        # Oil + risk-on → CAD up, EUR/CAD DOWN → score ≤-6 → SHORT EUR/CAD
        s2 = safe(oil, n_ema=50, inv=True)   # oil bull → CAD up → EUR/CAD down
        s3 = safe(spx, n_ema=50, inv=True)   # risk-on → EUR/CAD down
        s4 = safe(vix, n_ema=20)             # VIX up → EUR/CAD up

    else:
        s2 = safe(cop, n_ema=50)
        s3 = safe(spx, n_ema=50)
        s4 = safe(vix, n_ema=20, inv=True)

    raw   = s1 + s2 + s3 + s4 + s5
    score = (raw / 5 * 10).round(1)
    s1_gate = pd.Series(np.where(p > ema(p, 20), 1.0, -1.0), index=idx)
    aligned = (np.sign(score) == np.sign(s1_gate))
    score   = score.where(aligned, 0)
    return score.shift(1)

# ── INDICATORS ────────────────────────────────────────────────────────────────
def add_indicators(df15, df1h):
    df = df15.copy()
    df['ema9']  = ema(df['close'], 9)
    df['ema21'] = ema(df['close'], 21)
    prev = df['close'].shift(1)
    tr   = pd.concat([df['high'] - df['low'],
                      (df['high'] - prev).abs(),
                      (df['low']  - prev).abs()], axis=1).max(axis=1)
    df['atr']     = tr.ewm(span=14, adjust=False).mean()
    df['atr_pct'] = df['atr'] / df['close']

    ema12           = ema(df['close'], 12)
    ema26           = ema(df['close'], 26)
    df['macd']      = ema12 - ema26
    df['macd_sig']  = ema(df['macd'], 9)
    df['macd_hist'] = df['macd'] - df['macd_sig']
    df['rsi_15m']   = rsi(df['close'], 14)

    if 'volume' in df.columns:
        vol = df['volume'].replace(0, np.nan)
        df['vol_ma20']  = vol.rolling(20, min_periods=10).mean()
        df['vol_ratio'] = vol / (df['vol_ma20'] + 1e-10)
        if vol.dropna().std() < vol.dropna().mean() * 0.05:
            df['vol_ratio'] = np.nan
        direction    = np.sign(df['close'].diff()).fillna(0)
        df['obv']    = (direction * vol.fillna(0)).cumsum()
        df['obv_ema'] = ema(df['obv'], 10)
    else:
        df['vol_ma20'] = np.nan; df['vol_ratio'] = np.nan
        df['obv']      = np.nan; df['obv_ema']   = np.nan

    if not df1h.empty:
        c1h = df1h['close']
        df['ema9_1h']  = ema(c1h, 9).reindex(df.index, method='ffill')
        df['ema21_1h'] = ema(c1h, 21).reindex(df.index, method='ffill')
        df['ema50_1h'] = ema(c1h, 50).reindex(df.index, method='ffill')
        df['rsi_1h']   = rsi(c1h, 14).reindex(df.index, method='ffill')
        h4c = c1h.resample('4h').last().dropna()
        df['ema9_4h']  = ema(h4c, 9).reindex(df.index, method='ffill')
        df['ema21_4h'] = ema(h4c, 21).reindex(df.index, method='ffill')
    else:
        for col in ['ema9_1h','ema21_1h','ema50_1h','rsi_1h','ema9_4h','ema21_4h']:
            df[col] = np.nan
    return df

# ── SESSION VWAP ──────────────────────────────────────────────────────────────
def add_session_vwap(df, valid_sessions):
    df  = df.copy()
    tp  = (df['high'] + df['low'] + df['close']) / 3.0
    hr  = df.index.hour + df.index.minute / 60.0
    dts = df.index.normalize()
    df['vwap'] = np.nan; df['vwap_std'] = np.nan
    df['vwap_bars'] = 0;  df['in_entry'] = False; df['entry_sess'] = ''
    for sess_name, sess in SESSIONS.items():
        if sess_name not in valid_sessions:
            continue
        vh = sess['vwap_h']; es = sess['entry_s']; ee = sess['entry_e']
        in_sess = (hr >= vh) & (hr <= ee + 1.0) & (df.index.weekday < 5)
        for date in pd.unique(dts[in_sess]):
            mask = (dts == date) & in_sess
            if mask.sum() < 2: continue
            tp_d = tp.values[mask]; n_d = len(tp_d)
            cum  = np.cumsum(tp_d); cnt = np.arange(1, n_d+1, dtype=float)
            vw   = cum / cnt
            std  = np.zeros(n_d)
            for k in range(1, n_d):
                std[k] = np.std(tp_d[:k+1], ddof=0)
            std[std < 1e-8] = tp_d.mean() * 0.0005
            pos = np.where(mask)[0]
            df.iloc[pos, df.columns.get_loc('vwap')]      = vw
            df.iloc[pos, df.columns.get_loc('vwap_std')]  = np.maximum(std, 1e-8)
            df.iloc[pos, df.columns.get_loc('vwap_bars')] = cnt
        entry_mask = in_sess & (hr >= es) & (hr <= ee)
        df.loc[entry_mask, 'in_entry']   = True
        df.loc[entry_mask, 'entry_sess'] = sess_name
    return df

# ── V13 STRICT TECH FILTER ────────────────────────────────────────────────────
def tech_filter_passes(row, direction):
    """All 4 required: MACD>0, RSI<55/>45, Vol≥120%, OBV aligned."""
    macd_hist = row.get('macd_hist', np.nan)
    rsi_15m   = row.get('rsi_15m',  np.nan)
    vol_ratio = row.get('vol_ratio', np.nan)
    obv_v     = row.get('obv',       np.nan)
    obv_e     = row.get('obv_ema',   np.nan)
    if not pd.isna(macd_hist):
        if direction == 1  and macd_hist <= 0: return False
        if direction == -1 and macd_hist >= 0: return False
    if not pd.isna(rsi_15m):
        if direction == 1  and rsi_15m >= 55: return False
        if direction == -1 and rsi_15m <= 45: return False
    if not pd.isna(vol_ratio) and vol_ratio > 0:
        if vol_ratio < 1.20: return False
    if not (pd.isna(obv_v) or pd.isna(obv_e)):
        if direction == 1  and obv_v < obv_e: return False
        if direction == -1 and obv_v > obv_e: return False
    return True

# ── SIGNAL: VWAP TREND ────────────────────────────────────────────────────────
def generate_trend_trades(pair, df, macro_score, vix_series, usdjpy_series=None):
    df = df.copy()
    ms = macro_score.copy()
    if ms.index.tz is None:
        ms.index = pd.to_datetime(ms.index).tz_localize('UTC')
    ms_d    = ms.groupby(ms.index.normalize()).last()
    bar_dts = pd.DatetimeIndex(df.index.normalize())
    df['macro'] = ms_d.reindex(bar_dts, method='ffill').values

    atr_min, atr_max = PAIR_ATR.get(pair, (0.0003, 0.0100))
    is_jpy = pair in JPY_PAIRS

    usdjpy = None
    if usdjpy_series is not None and len(usdjpy_series) > 5:
        usdjpy = usdjpy_series.copy()
        if usdjpy.index.tz is not None:
            usdjpy.index = usdjpy.index.tz_localize(None)
        usdjpy.index = pd.to_datetime(usdjpy.index).normalize()

    trades_out = []
    i, N = 0, len(df)

    while i < N - 1:
        row = df.iloc[i]
        if row.name < MIN_DATE:               i += 1; continue
        if row.name.weekday() >= 5:           i += 1; continue
        if not row.get('in_entry', False):    i += 1; continue

        sess_name = row.get('entry_sess', '')
        date_key  = row.name.date()
        if sess_name == 'ny_overlap' and is_nfp_day(date_key): i += 1; continue

        macro = row.get('macro', np.nan)
        if pd.isna(macro): i += 1; continue

        regime = get_regime(date_key)

        # Regime pair exclusions
        if regime == 'VIX_SPIKE' and pair in VIX_SPIKE_SKIP: i += 1; continue
        if regime == 'OIL_WEAK'  and pair in OIL_WEAK_SKIP:  i += 1; continue

        # Short threshold adjustment for CAD pairs in OIL_WEAK
        score_short_thresh = (OIL_WEAK_SHORT_THRESH
                              if regime == 'OIL_WEAK' and pair in {'CAD/CHF', 'CAD/JPY'}
                              else SCORE_SHORT)

        if macro >= SCORE_LONG:           direction = 1
        elif macro <= score_short_thresh: direction = -1
        else:                             i += 1; continue

        if pair in LONG_ONLY_PAIRS and direction == -1: i += 1; continue

        price    = row['close']
        atr      = row.get('atr',      np.nan)
        atr_pct  = row.get('atr_pct',  np.nan)
        ema9     = row.get('ema9',     np.nan)
        ema21    = row.get('ema21',    np.nan)
        ema9_1h  = row.get('ema9_1h',  np.nan)
        ema21_1h = row.get('ema21_1h', np.nan)
        ema50_1h = row.get('ema50_1h', np.nan)
        ema9_4h  = row.get('ema9_4h',  np.nan)
        ema21_4h = row.get('ema21_4h', np.nan)
        rsi1h    = row.get('rsi_1h',   np.nan)
        vwap     = row.get('vwap',     np.nan)
        vstd     = row.get('vwap_std', np.nan)
        vbars    = row.get('vwap_bars', 0)

        if any(pd.isna(v) for v in [atr, atr_pct, ema9, ema21, vwap, vstd]): i += 1; continue
        if pd.isna(ema50_1h) or pd.isna(ema9_4h):                             i += 1; continue
        if direction == 1  and price < ema50_1h: i += 1; continue
        if direction == -1 and price > ema50_1h: i += 1; continue
        if is_jpy and direction == 1 and usdjpy is not None:
            if jpy_carry_guard(date_key, usdjpy): i += 1; continue
        if not (atr_min <= atr_pct <= atr_max): i += 1; continue
        if vbars < 4: i += 1; continue
        if not (pd.isna(ema9_4h) or pd.isna(ema21_4h)):
            if direction == 1  and ema9_4h <= ema21_4h: i += 1; continue
            if direction == -1 and ema9_4h >= ema21_4h: i += 1; continue
        if not (pd.isna(ema9_1h) or pd.isna(ema21_1h)):
            if direction == 1  and ema9_1h <= ema21_1h: i += 1; continue
            if direction == -1 and ema9_1h >= ema21_1h: i += 1; continue
        if not pd.isna(rsi1h):
            if direction == 1  and rsi1h > 70: i += 1; continue
            if direction == -1 and rsi1h < 30: i += 1; continue
        if direction == 1  and ema9 <= ema21: i += 1; continue
        if direction == -1 and ema9 >= ema21: i += 1; continue
        dist = (price - vwap) / vstd
        if direction == 1  and not (-1.0 <= dist <= 0.3): i += 1; continue
        if direction == -1 and not (-0.3 <= dist <= 1.0): i += 1; continue

        if not tech_filter_passes(row, direction): i += 1; continue

        entry_px  = price
        entry_dt  = row.name
        regime_sz = VIX_SPIKE_SIZE if regime == 'VIX_SPIKE' else 1.0
        size_mult = get_size_mult(abs(macro)) * regime_sz
        sl_px  = entry_px - SL_MULT * atr if direction == 1 else entry_px + SL_MULT * atr
        tp1_px = entry_px + TP1_MULT * atr if direction == 1 else entry_px - TP1_MULT * atr
        tp2_px = entry_px + TP2_MULT * atr if direction == 1 else entry_px - TP2_MULT * atr

        tp1_hit = False; exit_px = None; exit_dt = None; outcome = 'none'; bars_in = 0
        j = i + 1
        while j < N:
            b = df.iloc[j]; bars_in += 1
            b_hour = b.name.hour + b.name.minute / 60.0
            past = all(b_hour > SESSIONS[s]['entry_e'] + 2.5
                       for s in PAIR_SESSIONS[pair] if s in SESSIONS)
            if bars_in > TIME_BARS or past or b.name.weekday() >= 5:
                exit_px = b['close']; exit_dt = b.name; outcome = 'time'; break
            if not tp1_hit:
                if direction == 1:
                    if b['high'] >= tp1_px: tp1_hit = True; sl_px = entry_px
                    elif b['low'] <= sl_px: exit_px = sl_px; exit_dt = b.name; outcome = 'sl'; break
                else:
                    if b['low'] <= tp1_px: tp1_hit = True; sl_px = entry_px
                    elif b['high'] >= sl_px: exit_px = sl_px; exit_dt = b.name; outcome = 'sl'; break
            else:
                if direction == 1:
                    if b['high'] >= tp2_px: exit_px = tp2_px; exit_dt = b.name; outcome = 'tp2'; break
                    elif b['low'] <= sl_px: exit_px = sl_px; exit_dt = b.name; outcome = 'be'; break
                else:
                    if b['low'] <= tp2_px: exit_px = tp2_px; exit_dt = b.name; outcome = 'tp2'; break
                    elif b['high'] >= sl_px: exit_px = sl_px; exit_dt = b.name; outcome = 'be'; break
            j += 1

        if exit_px is None:
            exit_px = df.iloc[min(j, N-1)]['close']; exit_dt = df.iloc[min(j, N-1)].name; outcome = 'time'

        atr_sl = SL_MULT * atr
        if outcome == 'sl':   r_mult = -1.0
        elif outcome == 'be': r_mult = (TP1_MULT/SL_MULT) * TP1_FRAC
        elif outcome == 'tp2':r_mult = (TP1_MULT/SL_MULT)*TP1_FRAC + (TP2_MULT/SL_MULT)*TP2_FRAC
        elif outcome == 'time':
            r_rem  = (exit_px - entry_px) / atr_sl * direction
            r_mult = ((TP1_MULT/SL_MULT)*TP1_FRAC + r_rem*TP2_FRAC) if tp1_hit else r_rem
        else:
            r_mult = (exit_px - entry_px) / atr_sl * direction

        trades_out.append(dict(pair=pair, entry_dt=entry_dt, exit_dt=exit_dt,
                               direction=direction, outcome=outcome, session=sess_name,
                               signal_type='trend', r_mult=r_mult, macro=macro,
                               size_mult=size_mult, regime=regime))
        i = j + 1

    return pd.DataFrame(trades_out)

# ── PORTFOLIO ENGINE (FTMO CHALLENGE MODE) ────────────────────────────────────
def run_portfolio(pair_trades_dict):
    all_cands = []
    for pair, tdf in pair_trades_dict.items():
        if tdf.empty: continue
        for _, t in tdf.iterrows():
            all_cands.append(t.to_dict())
    if not all_cands:
        return pd.DataFrame(), float(ACCOUNT), True, {}

    all_cands.sort(key=lambda x: x['entry_dt'])
    equity        = float(ACCOUNT)
    day_start_eq  = {}
    daily_risk    = {}
    month_start   = {}
    last_pair_day = set()
    portfolio_t   = []
    month_results = {}
    floor_hit     = False

    for trade in all_cands:
        entry_date = trade['entry_dt'].date()
        month_key  = (entry_date.year, entry_date.month)

        if entry_date not in day_start_eq:
            day_start_eq[entry_date] = equity
        if month_key not in month_start:
            month_start[month_key] = equity

        if equity <= FTMO_FLOOR:
            floor_hit = True; break

        day_loss = equity - day_start_eq[entry_date]
        if day_loss <= -FTMO_DAILY_LIMIT:
            continue

        key = (trade['pair'], entry_date)
        if key in last_pair_day: continue

        month_pnl_pct = (equity - month_start[month_key]) / month_start[month_key] * 100

        if month_pnl_pct <= CHAL_STOP_AT:
            continue

        # Layer 1: absolute equity protection
        eq_pct = equity / ACCOUNT
        if eq_pct <= 0.91:   abs_scale = 0.25
        elif eq_pct <= 0.93: abs_scale = 0.35
        elif eq_pct <= 0.95: abs_scale = 0.55
        elif eq_pct <= 0.97: abs_scale = 0.75
        else:                abs_scale = 1.0

        # Layer 2: monthly progress
        if month_pnl_pct >= CHAL_PROTECT_AT:
            month_scale = 0.20
        elif month_pnl_pct >= CHAL_CAUTIOUS_AT:
            month_scale = 0.30
        else:
            month_scale = 1.0

        challenge_scale = min(abs_scale, month_scale)

        risk_pct = RISK_TIER.get(trade['pair'], 0.020)
        risk_amt = equity * risk_pct * trade['size_mult'] * challenge_scale

        # Layer 3: pre-trade worst-case floor check
        worst_case_month_pnl = (equity - risk_amt - month_start[month_key]) / month_start[month_key] * 100
        if worst_case_month_pnl < -9.9:
            continue

        daily_used = daily_risk.get(entry_date, 0.0)
        if daily_used + risk_amt > equity * FTMO_DAILY_CAP:
            continue

        pnl     = trade['r_mult'] * risk_amt
        equity += pnl
        daily_risk[entry_date] = daily_used + risk_amt
        last_pair_day.add(key)
        portfolio_t.append({
            **trade,
            'pnl': pnl, 'equity': equity,
            'challenge_scale': challenge_scale,
            'month_pnl_before': month_pnl_pct
        })

    if portfolio_t:
        ptdf_tmp = pd.DataFrame(portfolio_t)
        for mk, meq in month_start.items():
            month_trades = ptdf_tmp[
                (ptdf_tmp['entry_dt'].dt.year  == mk[0]) &
                (ptdf_tmp['entry_dt'].dt.month == mk[1])
            ]
            if month_trades.empty:
                month_results[mk] = {'pnl': 0, 'n': 0, 'wr': 0, 'passed': False, 'pct': 0.0}
                continue
            month_pnl = month_trades['pnl'].sum()
            month_pct = month_pnl / meq * 100
            month_results[mk] = {
                'pnl': month_pnl, 'n': len(month_trades),
                'wr': (month_trades['r_mult'] > 0).mean(),
                'passed': month_pct >= 10.0, 'pct': month_pct, 'start_eq': meq
            }

    return pd.DataFrame(portfolio_t), equity, not floor_hit, month_results

# ── STATISTICS ────────────────────────────────────────────────────────────────
def compute_stats(tdf, start_eq, end_eq):
    if tdf.empty or len(tdf) < 3: return {}
    r    = tdf['r_mult'].values
    n    = len(r)
    wr   = (r > 0).mean()
    d0   = tdf['entry_dt'].iloc[0]
    d1   = tdf['exit_dt'].iloc[-1]
    days = max((d1 - d0).days, 1)
    tpw  = n / (days / 7)
    ann  = tpw * 52
    sr   = (r.mean() / (r.std(ddof=1) + 1e-9)) * np.sqrt(ann)
    t, p2 = stats.ttest_1samp(r, 0)
    p1   = p2 / 2 if t > 0 else 1.0
    eq   = tdf['equity'].values
    pk   = np.maximum.accumulate(eq)
    mdd  = ((pk - eq) / pk).max()
    roi  = (end_eq - start_eq) / start_eq
    tdf2 = tdf.copy()
    tdf2['month'] = tdf2['entry_dt'].dt.to_period('M')
    monthly = (tdf2.groupby('month')
               .agg(n=('pnl','count'), pnl=('pnl','sum'),
                    wr=('r_mult', lambda x: (x>0).mean()))
               .reset_index())
    return dict(n=n, wr=wr, roi=roi, sharpe=sr, p_val=p1, mdd=mdd, tpw=tpw, monthly=monthly)

# ── REPORTING ─────────────────────────────────────────────────────────────────
def print_report(ptdf, final_eq, floor_ok, pair_trades_dict, month_results):
    if ptdf.empty: print("\nNo portfolio trades."); return
    st = compute_stats(ptdf, ACCOUNT, final_eq)
    if not st: return

    sep = '=' * 80
    print(f"\n{sep}")
    print(f"  PORTFOLIO V15 — 9-PAIR EXPANDED UNIVERSE")
    print(f"  AUD/CHF AUD/JPY AUD/NZD AUD/USD | CAD/CHF CAD/JPY | CHF/JPY | EUR/AUD EUR/CAD")
    print(f"  Strict V13 filters · Triple-layer floor protection · Daily cap 5.0%")
    print(f"{sep}")
    print(f"  ${ACCOUNT:,.0f}  →  ${final_eq:,.0f}  ({st['roi']*100:+.1f}% ROI)")
    print(f"  Sharpe  : {st['sharpe']:.2f}   Max DD: {st['mdd']*100:.1f}%")
    print(f"  Trades  : {st['n']}  ({st['tpw']:.2f}/week  ≈  {st['tpw']/5:.2f}/day)")
    sig = '✓' if st['p_val'] < 0.05 else ('~' if st['p_val'] < 0.10 else '✗')
    print(f"  p-val   : {st['p_val']:.4f} {sig}   WR: {st['wr']*100:.1f}%")
    if not floor_ok: print(f"  ⚠️  Portfolio hit $108k floor during backtest")

    print(f"\n  By pair:")
    for pair in ACTIVE_PAIRS:
        raw_n = len(pair_trades_dict.get(pair, pd.DataFrame()))
        sub   = ptdf[ptdf['pair'] == pair]
        if sub.empty: print(f"  {pair:<12} {raw_n:>5}raw   0port"); continue
        n = len(sub); wr = (sub['r_mult']>0).mean(); pnl = sub['pnl'].sum()
        sig2 = '?'
        if len(sub['r_mult'].values) >= 5:
            t2, p2 = stats.ttest_1samp(sub['r_mult'].values, 0)
            pv = p2/2 if t2>0 else 1.0
            sig2 = '✓' if pv<0.05 else ('~' if pv<0.10 else '✗')
        print(f"  {pair:<12} {raw_n:>5}raw {n:>5}port  WR {wr*100:.0f}%  "
              f"avg {sub['r_mult'].mean():+.3f}R  ${pnl:>+10,.0f}  {sig2}")

    if 'regime' in ptdf.columns:
        print(f"\n  By regime:")
        for reg in ['BULL', 'NORMAL', 'OIL_WEAK', 'VIX_SPIKE']:
            sub = ptdf[ptdf['regime'] == reg]
            if sub.empty: continue
            wr  = (sub['r_mult'] > 0).mean()
            avg = sub['r_mult'].mean()
            pnl = sub['pnl'].sum()
            print(f"    {reg:<12} {len(sub):>4}tr  WR {wr*100:.0f}%  "
                  f"avg {avg:+.3f}R  ${pnl:>+11,.0f}")

    print(f"\n{'─'*80}")
    print(f"  FTMO CHALLENGE SIMULATION  (each month = one 30-day challenge attempt)")
    print(f"  Target: +10% in 30 days  |  Floor: -10% max DD  |  Daily: -5% limit")
    print(f"{'─'*80}")

    passes = 0; total = 0; eq_run = ACCOUNT; pos_months = 0
    for _, mr in st['monthly'].iterrows():
        mk    = (mr['month'].year, mr['month'].month)
        mres  = month_results.get(mk, {})
        pct   = mres.get('pct', mr['pnl'] / ACCOUNT * 100)
        passed = mres.get('passed', pct >= 10.0)
        n_tr  = mr['n']; wr_m = mr['wr']; pnl_m = mr['pnl']
        eq_run += pnl_m
        total += 1
        if passed: passes += 1
        if pnl_m > 0: pos_months += 1
        flag  = '  ★ CHALLENGE PASS ✓' if passed else ('  ✗ loss' if pnl_m < 0 else '')
        print(f"    {mr['month']}  {n_tr:>3}tr  WR {wr_m*100:.0f}%  "
              f"${pnl_m:>+10,.0f} ({pct:>+6.1f}%)  ${eq_run:>10,.0f}{flag}")

    ftmo_p = st['roi'] >= 0.10; ftmo_d = st['mdd'] <= 0.10
    print(f"\n  FTMO overall  Profit {'✓' if ftmo_p else '✗'} ({st['roi']*100:+.1f}%)  "
          f"DD {'✓' if ftmo_d else '✗'} ({st['mdd']*100:.1f}%)  "
          f"Floor {'✓' if floor_ok else '✗'}")
    print(f"\n  ┌─ CHALLENGE RESULT ──────────────────────────────────────────────┐")
    print(f"  │  Monthly passes: {passes}/{total}  ({passes/total*100:.0f}% success rate)             │")
    print(f"  │  Positive months: {pos_months}/{total}                                         │")
    if month_results:
        best = max(month_results.values(), key=lambda x: x.get('pct', 0))
        worst = min(month_results.values(), key=lambda x: x.get('pct', 0))
        print(f"  │  Best month: {best.get('pct',0):+.1f}%   Worst: {worst.get('pct',0):+.1f}%             │")
    print(f"  └─────────────────────────────────────────────────────────────────┘")

    avg_risk = sum(RISK_TIER.get(p, 0.02) for p in ACTIVE_PAIRS) / len(ACTIVE_PAIRS)
    avg_r    = ptdf['r_mult'].mean()
    tpm      = st['tpw'] * 4.33
    exp_pnl  = tpm * avg_r * ACCOUNT * avg_risk
    print(f"\n  30-day projection: {tpm:.0f} trades  avg {avg_r:+.3f}R  ~${exp_pnl:+,.0f} ({exp_pnl/ACCOUNT*100:+.1f}%)")
    print(f"{sep}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("V15: 9-Pair Expanded Universe — FTMO Challenge Mode")
    print("  Pairs: AUD/CHF AUD/JPY AUD/NZD AUD/USD CAD/CHF CAD/JPY CHF/JPY EUR/AUD EUR/CAD")
    print("  All TREND (VWAP pullback) · No ORB (GBP/JPY removed)")
    print("  Strict V13 filters · Triple-layer floor protection · Daily cap 5.0%")
    print()

    print("Loading 15m data...")
    data_15m = {p: load_15m(p) for p in ACTIVE_PAIRS}

    print("Loading 1h data (yfinance)...")
    data_1h = {p: load_1h(p) for p in ACTIVE_PAIRS}

    macro_all = load_macro()

    print("Computing regime series...")
    regime_series = compute_regime_series(macro_all)
    global _REGIME_LOOKUP
    for dt, reg in regime_series.items():
        d = pd.Timestamp(dt).normalize()
        if d.tz is not None:
            d = d.tz_localize(None)
        _REGIME_LOOKUP[d] = reg
    regime_counts = pd.Series(regime_series.values).value_counts()
    print(f"  Regime days: {dict(regime_counts)}")

    vix_d = macro_all.get('vix', pd.Series(dtype=float))
    if vix_d.index.tz is not None:
        vix_d.index = vix_d.index.tz_localize(None)
    vix_lookup    = vix_d.to_dict()
    usdjpy_series = macro_all.get('usdjpy', None)

    pair_trades = {}
    for pair in ACTIVE_PAIRS:
        df15 = data_15m.get(pair, pd.DataFrame())
        df1h = data_1h.get(pair, pd.DataFrame())
        if df15.empty:
            print(f"\n[{pair}] No 15m data — skipping")
            pair_trades[pair] = pd.DataFrame(); continue

        print(f"\nBuilding {pair}...")
        macro_score = build_macro(pair, macro_all)
        df15        = add_indicators(df15, df1h)
        df15        = add_session_vwap(df15, PAIR_SESSIONS[pair])
        tdf         = generate_trend_trades(pair, df15, macro_score, vix_lookup, usdjpy_series)
        pair_trades[pair] = tdf

        if tdf.empty:
            print(f"  [{pair}] No candidates")
        else:
            n   = len(tdf)
            d0  = tdf['entry_dt'].iloc[0]; d1 = tdf['exit_dt'].iloc[-1]
            tpw = n / max((d1 - d0).days / 7, 1)
            wr  = (tdf['r_mult']>0).mean()
            avg = tdf['r_mult'].mean()
            longs  = (tdf['direction'] == 1).sum()
            shorts = (tdf['direction'] == -1).sum()
            print(f"  [{pair}] {n}cand  {tpw:.2f}/wk  WR={wr*100:.0f}%  avgR={avg:+.3f}  L:{longs} S:{shorts}")

    print("\n\nRunning portfolio engine (FTMO Challenge Mode)...")
    ptdf, final_eq, floor_ok, month_results = run_portfolio(pair_trades)
    print_report(ptdf, final_eq, floor_ok, pair_trades, month_results)

if __name__ == '__main__':
    main()
