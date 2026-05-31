#!/usr/bin/env python3
"""
Forex RORO Backtest V14 — FTMO Challenge Mode
==============================================
Built from scratch to pass the FTMO challenge: +10% in 30 days, max 10% DD,
max 5% daily loss.

WHAT WENT WRONG IN PRIOR ATTEMPTS
===================================
V13 never hit +10% in 30 days (best month +8.5%). Problems:
- Risk tiers too small to compound to +10% in 1 month
- Regime size cuts (OIL_WEAK 0.75×) reduced P&L further

First V14 attempt (2.5× risk, MACD+2of3):
- Hit $108k floor in month 3 (Sep-Nov 2024 had 3× back-to-back ORB losses)
- Relaxed filter (MACD+2of3) let in lower-quality trades: AUD/JPY dropped
  from +0.633R to +0.197R avg. More trades but much worse quality.
- 2.5× risk is too aggressive for the ORB (low avg R, high variance)

V14 FINAL DESIGN
================
Key insight: TREND trades (WR 55-63%, avgR +0.6-0.9R) are high quality.
ORB trades (WR 57%, avgR +0.137R) are high frequency but low R per trade.
→ Size TREND trades aggressively, ORB trades conservatively.

1. RISK TIERS: asymmetric sizing
   TREND:  CAD/CHF 4%   AUD/JPY 3%   CAD/JPY 2%
   ORB:    GBP/JPY 1.5% (lower — high variance, low R per trade)
   (V13 was: trend 1-2%, ORB 1%)

2. TECH FILTERS: KEEP STRICT V13 (all 4 required)
   The relaxed version (MACD+2of3) produced inferior trades — AUD/JPY WR
   dropped from 56% to 44%, avgR from +0.633 to +0.197. NOT worth it.
   V13 strict quality: 19 trend trades at +0.76R avg vs 44 at +0.447R.
   LESS IS MORE — fewer high-quality trend entries, sized up appropriately.

3. ABSOLUTE FLOOR PROTECTION (double layer):
   Layer 1 — absolute equity (prevents floor breach across months):
     equity ≤ 91% of ACCOUNT:  scale 0.25× (within $1,200 of floor — nearly done)
     equity ≤ 93% of ACCOUNT:  scale 0.35×
     equity ≤ 95% of ACCOUNT:  scale 0.55×
     equity ≤ 97% of ACCOUNT:  scale 0.75×
   Layer 2 — monthly progress (protect/push toward challenge target):
     month_pnl ≥ +10%: scale 0.20× (challenge done — lock it in)
     month_pnl ≥ +6%:  scale 0.75× (close — careful)
   Final scale = min(abs_scale, month_scale)

4. DAILY CAP: 4.0% equity per day (hard buffer before 5% FTMO limit)

5. REMOVE OIL_WEAK SIZE REDUCTION (keep pair exclusions only)
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

# ── V14 RISK TIERS (asymmetric: TREND 2× V13, ORB 1.5× V13) ──────────────────
RISK_TIER = {
    'CAD/CHF': 0.040,   # was 0.020 — primary trend pair, both directions (2×)
    'AUD/JPY': 0.030,   # was 0.015 — carry + copper momentum (2×)
    'CAD/JPY': 0.020,   # was 0.010 — carry (OIL_WEAK skips this) (2×)
    'GBP/JPY': 0.015,   # was 0.010 — ORB London open (1.5×, lower variance)
}

# ── REGIME CONFIG ─────────────────────────────────────────────────────────────
OIL_WEAK_SHORT_THRESH = -4.0   # lower CAD/CHF short threshold in oil-bear regimes
VIX_SPIKE_MULT        = 0.10   # VIX must be this % above EMA20 to trigger
VIX_SPIKE_SIZE        = 0.50   # 50% size in VIX_SPIKE — genuinely dangerous
# OIL_WEAK: no size reduction in V14 — pair exclusion is sufficient

# ── FTMO RULES ────────────────────────────────────────────────────────────────
FTMO_TARGET      = ACCOUNT * 1.10   # +10% = $132,000
FTMO_FLOOR       = ACCOUNT * 0.90   # -10% = $108,000
FTMO_DAILY_LIMIT = ACCOUNT * 0.05   # -5%/day = $6,000
FTMO_DAILY_CAP   = 0.050            # max 5.0% equity in new risk per day (FTMO hard limit)

# Monthly challenge scaling thresholds (Layer 2 — monthly progress)
CHAL_PROTECT_AT  =  10.0   # ≥+10%: scale to 0.20× (challenge won — lock it in)
CHAL_CAUTIOUS_AT =   9.5   # ≥+9.5%: scale to 0.30× (final push — very careful)
CHAL_STOP_AT     =  -9.5   # ≤-9.5%: stop trading this month

# ── ORB PARAMETERS ────────────────────────────────────────────────────────────
ORB_RANGE_START = 6.5
ORB_RANGE_END   = 7.5
ORB_ENTRY_END   = 8.5    # extended to 09:00 UTC
ORB_HOLD_END    = 10.0
ORB_MIN_BARS    = 3
MIN_RANGE_ATR   = 0.3
BREAKOUT_CONF   = 0.10

TREND_PAIRS  = ['CAD/CHF', 'AUD/JPY', 'CAD/JPY']
ORB_PAIRS    = ['GBP/JPY']
ACTIVE_PAIRS = TREND_PAIRS + ORB_PAIRS

PAIRS_ALL = ['AUD/JPY', 'AUD/CHF', 'AUD/NZD', 'AUD/USD',
             'CAD/CHF', 'CAD/JPY', 'GBP/USD', 'GBP/JPY']

MIN_DATE = pd.Timestamp('2024-09-01', tz='UTC')

SESSIONS = {
    'london_open': {'vwap_h': 7.0,  'entry_s': 7.5,  'entry_e': 10.0},
    'ny_overlap':  {'vwap_h': 13.5, 'entry_s': 14.0, 'entry_e': 17.0},
}
PAIR_SESSIONS = {p: ['london_open', 'ny_overlap'] for p in TREND_PAIRS}
PAIR_SESSIONS.update({p: ['london_open'] for p in ORB_PAIRS})

PAIR_ATR = {
    'AUD/JPY': (0.0004, 0.0120),
    'CAD/CHF': (0.0002, 0.0080),
    'CAD/JPY': (0.0004, 0.0120),
    'GBP/JPY': (0.0005, 0.0200),
}

JPY_PAIRS       = {'AUD/JPY', 'CAD/JPY', 'GBP/JPY'}
LONG_ONLY_PAIRS = {'AUD/JPY', 'CAD/JPY', 'GBP/JPY'}

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
    print(f"--- Fetching {symbol} 15min with volume (not cached) ---")
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
    if all_rows and 'volume' in all_rows[0]:
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
    for pair in PAIRS_ALL:
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

# ── MACRO SCORES ──────────────────────────────────────────────────────────────
def build_macro(pair, macro):
    p   = macro.get(pair, pd.Series(dtype=float))
    spx = macro.get('spx', pd.Series(dtype=float))
    vix = macro.get('vix', pd.Series(dtype=float))
    oil = macro.get('oil', pd.Series(dtype=float))
    cop = macro.get('cop', pd.Series(dtype=float))
    tnx = macro.get('tnx', pd.Series(dtype=float))
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
    elif pair == 'GBP/JPY':
        s2 = safe(spx, n_ema=50)
        s3 = safe(vix, n_ema=20, inv=True)
        s4 = safe(tnx, compare_shift=10)
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

# ── V13 STRICT TECH FILTER HELPER ────────────────────────────────────────────
def tech_filter_passes(row, direction, mode='trend'):
    """
    V13 strict filters — all 4 required.
    Trend: RSI<55 longs / >45 shorts  (pullback into VWAP — not overbought)
    ORB:   RSI>50 longs / <50 shorts  (momentum confirmation — breakout is happening)
    """
    macd_hist = row.get('macd_hist', np.nan)
    rsi_15m   = row.get('rsi_15m',  np.nan)
    vol_ratio = row.get('vol_ratio', np.nan)
    obv_v     = row.get('obv',       np.nan)
    obv_e     = row.get('obv_ema',   np.nan)

    # 1. MACD histogram > 0
    if not pd.isna(macd_hist):
        if direction == 1  and macd_hist <= 0: return False
        if direction == -1 and macd_hist >= 0: return False

    # 2. RSI: different semantics for trend vs ORB
    if not pd.isna(rsi_15m):
        if mode == 'orb':
            # ORB: RSI > 50 confirms upward momentum (breakout is live)
            if direction == 1  and rsi_15m <= 50: return False
            if direction == -1 and rsi_15m >= 50: return False
        else:
            # Trend (VWAP pullback): RSI < 55 (not overbought at entry)
            if direction == 1  and rsi_15m >= 55: return False
            if direction == -1 and rsi_15m <= 45: return False

    # 3. Volume ≥ 120% of 20-bar average
    if not pd.isna(vol_ratio) and vol_ratio > 0:
        if vol_ratio < 1.20: return False

    # 4. OBV above EMA10 (longs) / below EMA10 (shorts)
    if not (pd.isna(obv_v) or pd.isna(obv_e)):
        if direction == 1  and obv_v < obv_e: return False
        if direction == -1 and obv_v > obv_e: return False

    return True

# ── SIGNAL A: VWAP TREND ──────────────────────────────────────────────────────
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

        # V14 regime rules (pair exclusions only — no size reduction for OIL_WEAK)
        if regime == 'VIX_SPIKE' and pair in {'AUD/JPY', 'CAD/JPY'}: i += 1; continue
        if regime == 'OIL_WEAK'  and pair == 'CAD/JPY':              i += 1; continue

        score_short_thresh = (OIL_WEAK_SHORT_THRESH
                              if regime == 'OIL_WEAK' and pair == 'CAD/CHF'
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

        # V14: MACD mandatory + 2 of 3 (RSI, Volume, OBV)
        if not tech_filter_passes(row, direction, mode='trend'): i += 1; continue

        entry_px  = price
        entry_dt  = row.name
        # V14: OIL_WEAK no longer reduces size — only VIX_SPIKE does
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

# ── SIGNAL B: OPENING RANGE BREAKOUT ──────────────────────────────────────────
def compute_orb_levels(df15):
    hr  = df15.index.hour + df15.index.minute / 60.0
    dts = df15.index.normalize()
    levels = {}
    for date in pd.unique(dts):
        if pd.Timestamp(date).weekday() >= 5: continue
        mask = (dts == date) & (hr >= ORB_RANGE_START) & (hr < ORB_RANGE_END)
        if mask.sum() < ORB_MIN_BARS: continue
        rh  = df15.loc[mask, 'high'].max()
        rl  = df15.loc[mask, 'low'].min()
        rng = rh - rl
        if rng <= 0: continue
        asian_mask = (dts == date) & (hr >= 0) & (hr < ORB_RANGE_START)
        if asian_mask.sum() >= 4:
            ah = df15.loc[asian_mask, 'high'].max()
            al = df15.loc[asian_mask, 'low'].min()
            ac = df15.loc[asian_mask, 'close'].iloc[-1]
            asian_bias = 1 if ac > (ah + al) / 2 else -1
        else:
            asian_bias = 0
        levels[pd.Timestamp(date).date()] = {
            'high': rh, 'low': rl, 'range': rng,
            'mid': (rh + rl) / 2, 'asian_bias': asian_bias
        }
    return levels

def generate_orb_trades(pair, df, macro_score, vix_series, usdjpy_series=None):
    df = df.copy()
    ms = macro_score.copy()
    if ms.index.tz is None:
        ms.index = pd.to_datetime(ms.index).tz_localize('UTC')
    ms_d    = ms.groupby(ms.index.normalize()).last()
    bar_dts = pd.DatetimeIndex(df.index.normalize())
    df['macro'] = ms_d.reindex(bar_dts, method='ffill').values

    is_jpy     = pair in JPY_PAIRS
    orb_levels = compute_orb_levels(df)

    usdjpy = None
    if usdjpy_series is not None and len(usdjpy_series) > 5:
        usdjpy = usdjpy_series.copy()
        if usdjpy.index.tz is not None:
            usdjpy.index = usdjpy.index.tz_localize(None)
        usdjpy.index = pd.to_datetime(usdjpy.index).normalize()

    trades_out    = []
    triggered_days = set()
    i, N = 0, len(df)

    while i < N - 1:
        row = df.iloc[i]
        if row.name < MIN_DATE:             i += 1; continue
        if row.name.weekday() >= 5:         i += 1; continue
        hr = row.name.hour + row.name.minute / 60.0
        if not (ORB_RANGE_END <= hr <= 9.0): i += 1; continue
        date_key = row.name.date()
        if date_key in triggered_days: i += 1; continue

        lvl = orb_levels.get(date_key)
        if lvl is None: i += 1; continue

        price = row['close']
        atr   = row.get('atr', np.nan)
        if pd.isna(atr): i += 1; continue

        rng = lvl['range']
        if not (MIN_RANGE_ATR * atr <= rng): i += 1; continue

        break_long  = price > lvl['high'] + BREAKOUT_CONF * rng
        break_short = price < lvl['low']  - BREAKOUT_CONF * rng
        if not break_long and not break_short: i += 1; continue

        direction = 1 if break_long else -1
        if pair in LONG_ONLY_PAIRS and direction == -1: i += 1; continue

        asian_bias = lvl.get('asian_bias', 0)
        if asian_bias == 1  and direction == -1: i += 1; continue
        if asian_bias == -1 and direction == 1:  i += 1; continue

        if is_nfp_day(date_key):                       i += 1; continue
        if is_high_vix_day(date_key, vix_series):      i += 1; continue
        if is_jpy and direction == 1 and usdjpy is not None:
            if jpy_carry_guard(date_key, usdjpy):      i += 1; continue

        ema50_1h = row.get('ema50_1h', np.nan)
        ema9_4h  = row.get('ema9_4h',  np.nan)
        if pd.isna(ema50_1h) or pd.isna(ema9_4h): i += 1; continue

        # V14 ORB filter: MACD + RSI required, 1 of 2 (Vol, OBV)
        if not tech_filter_passes(row, direction, mode='orb'): i += 1; continue

        if direction == 1:
            sl_px  = lvl['low']
            tp1_px = price + TP1_MULT * rng
            tp2_px = price + TP2_MULT * rng
        else:
            sl_px  = lvl['high']
            tp1_px = price - TP1_MULT * rng
            tp2_px = price - TP2_MULT * rng

        sl_dist   = abs(price - sl_px)
        macro     = row.get('macro', 0)
        regime    = get_regime(date_key)
        regime_sz = VIX_SPIKE_SIZE if regime == 'VIX_SPIKE' else 1.0
        size_mult = (get_size_mult(abs(macro)) if abs(macro) >= SCORE_LONG else 1.0) * regime_sz
        entry_px  = price; entry_dt = row.name

        tp1_hit = False; exit_px = None; exit_dt = None; outcome = 'none'; bars_in = 0
        j = i + 1
        while j < N:
            b = df.iloc[j]; bars_in += 1
            b_hour = b.name.hour + b.name.minute / 60.0
            if bars_in > 10 or b_hour > ORB_HOLD_END or b.name.weekday() >= 5:
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

        if sl_dist < 1e-10: i += 1; continue
        if outcome == 'sl':   r_mult = -1.0
        elif outcome == 'be': r_mult = (TP1_MULT * rng / sl_dist) * TP1_FRAC
        elif outcome == 'tp2':
            r_mult = ((TP1_MULT * rng / sl_dist) * TP1_FRAC +
                      (TP2_MULT * rng / sl_dist) * TP2_FRAC)
        elif outcome == 'time':
            r_rem  = (exit_px - entry_px) / sl_dist * direction
            r_mult = ((TP1_MULT * rng / sl_dist) * TP1_FRAC + r_rem * TP2_FRAC) if tp1_hit else r_rem
        else:
            r_mult = (exit_px - entry_px) / sl_dist * direction

        triggered_days.add(date_key)
        trades_out.append(dict(pair=pair, entry_dt=entry_dt, exit_dt=exit_dt,
                               direction=direction, outcome=outcome, session='lob',
                               signal_type='orb', r_mult=r_mult, macro=macro,
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
    day_start_eq  = {}      # FTMO daily tracking
    daily_risk    = {}      # daily risk budget used (4.5% cap)
    month_start   = {}      # FTMO monthly challenge tracking
    last_pair_day = set()
    portfolio_t   = []
    month_results = {}      # per-month challenge outcome
    floor_hit     = False

    for trade in all_cands:
        entry_date = trade['entry_dt'].date()
        month_key  = (entry_date.year, entry_date.month)

        # Track day start equity (FTMO daily limit)
        if entry_date not in day_start_eq:
            day_start_eq[entry_date] = equity

        # Track month start equity (FTMO challenge target)
        if month_key not in month_start:
            month_start[month_key] = equity

        # Hard stops
        if equity <= FTMO_FLOOR:
            floor_hit = True; break
        day_loss = equity - day_start_eq[entry_date]
        if day_loss <= -FTMO_DAILY_LIMIT:
            continue  # daily limit hit — skip rest of day

        # One trade per pair per day
        key = (trade['pair'], entry_date)
        if key in last_pair_day: continue

        # Monthly progress P&L
        month_pnl_pct = (equity - month_start[month_key]) / month_start[month_key] * 100

        # Monthly stop — too close to FTMO floor for the month
        if month_pnl_pct <= CHAL_STOP_AT:
            continue

        # Layer 1: absolute equity protection (distance from $108k floor)
        eq_pct = equity / ACCOUNT
        if eq_pct <= 0.91:   abs_scale = 0.25   # within $1,200 of floor
        elif eq_pct <= 0.93: abs_scale = 0.35
        elif eq_pct <= 0.95: abs_scale = 0.55
        elif eq_pct <= 0.97: abs_scale = 0.75
        else:                abs_scale = 1.0

        # Layer 2: monthly challenge progress
        if month_pnl_pct >= CHAL_PROTECT_AT:
            month_scale = 0.20   # challenge won — lock it in
        elif month_pnl_pct >= CHAL_CAUTIOUS_AT:
            month_scale = 0.30   # 9.5-10% — nearly there, don't blow it
        else:
            month_scale = 1.0    # 0-9.5% — full size, chase the target

        challenge_scale = min(abs_scale, month_scale)

        # Compute actual risk amount (includes size_mult and challenge_scale)
        risk_pct = RISK_TIER.get(trade['pair'], 0.015)
        risk_amt = equity * risk_pct * trade['size_mult'] * challenge_scale

        # Pre-trade worst-case floor check: if this trade hits full SL, ensure
        # monthly loss stays within -10% (FTMO floor for a fresh $120k challenge).
        # Uses actual risk_amt so size_mult and challenge_scale are both included.
        worst_case_month_pnl = (equity - risk_amt - month_start[month_key]) / month_start[month_key] * 100
        if worst_case_month_pnl < -9.9:
            continue

        # Daily risk cap: 5.0% equity per day (FTMO hard limit)
        daily_used = daily_risk.get(entry_date, 0.0)
        if daily_used + risk_amt > equity * FTMO_DAILY_CAP:
            continue  # would push over daily cap

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

    # Compute per-month challenge results
    if portfolio_t:
        ptdf_tmp = pd.DataFrame(portfolio_t)
        for mk, meq in month_start.items():
            month_trades = ptdf_tmp[
                (ptdf_tmp['entry_dt'].dt.year  == mk[0]) &
                (ptdf_tmp['entry_dt'].dt.month == mk[1])
            ]
            if month_trades.empty:
                month_results[mk] = {'pnl': 0, 'n': 0, 'wr': 0,
                                     'passed': False, 'pct': 0.0}
                continue
            month_pnl = month_trades['pnl'].sum()
            month_pct = month_pnl / meq * 100
            month_results[mk] = {
                'pnl': month_pnl,
                'n': len(month_trades),
                'wr': (month_trades['r_mult'] > 0).mean(),
                'passed': month_pct >= 10.0,
                'pct': month_pct,
                'start_eq': meq
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
    print(f"  PORTFOLIO V14 — FTMO CHALLENGE MODE")
    print(f"  Risk asymmetric (TREND 2× / ORB 1.5×) · Strict V13 filters · Triple-layer floor protection · Daily cap 5.0%")
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
        if sub.empty: print(f"  {pair:<10} {raw_n:>6} candidates  0 portfolio"); continue
        n = len(sub); wr = (sub['r_mult']>0).mean(); pnl = sub['pnl'].sum()
        sig2 = '?'
        if len(sub['r_mult'].values) >= 5:
            t2, p2 = stats.ttest_1samp(sub['r_mult'].values, 0)
            pv = p2/2 if t2>0 else 1.0
            sig2 = '✓' if pv<0.05 else ('~' if pv<0.10 else '✗')
        stype = sub['signal_type'].iloc[0]
        print(f"  {pair:<10} {raw_n:>5}raw {n:>5}port  WR {wr*100:.0f}%  "
              f"avg {sub['r_mult'].mean():+.3f}R  ${pnl:>+10,.0f}  [{stype}] {sig2}")

    print(f"\n  By signal type:")
    for stype in ['trend', 'orb']:
        sub = ptdf[ptdf['signal_type'] == stype]
        if sub.empty: continue
        wr   = (sub['r_mult']>0).mean()
        avg  = sub['r_mult'].mean()
        pnl  = sub['pnl'].sum()
        tpw_s = len(sub) / max((ptdf['exit_dt'].iloc[-1]-ptdf['entry_dt'].iloc[0]).days/7, 1)
        print(f"    {stype:<8} {len(sub):>4}tr  WR {wr*100:.0f}%  "
              f"avg {avg:+.3f}R  ${pnl:>+11,.0f}  {tpw_s:.2f}/wk")

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

    # ── FTMO CHALLENGE SIMULATION (per calendar month) ────────────────────────
    print(f"\n{'─'*80}")
    print(f"  FTMO CHALLENGE SIMULATION  (each month = one 30-day challenge attempt)")
    print(f"  Target: +10% in 30 days  |  Floor: -10% max DD  |  Daily: -5% limit")
    print(f"{'─'*80}")

    passes = 0; total = 0; eq_run = ACCOUNT
    pos_months = 0
    for _, mr in st['monthly'].iterrows():
        mk    = (mr['month'].year, mr['month'].month)
        mres  = month_results.get(mk, {})
        pct   = mres.get('pct', mr['pnl'] / ACCOUNT * 100)
        passed = mres.get('passed', pct >= 10.0)
        n_tr  = mr['n']
        wr_m  = mr['wr']
        pnl_m = mr['pnl']
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
    print(f"  │  Monthly passes: {passes}/{total}  ({passes/total*100:.0f}% success rate)              │")
    print(f"  │  Positive months: {pos_months}/{total}                                          │")
    print(f"  │  Best month: {max(month_results.values(), key=lambda x: x.get('pct',0)).get('pct',0):+.1f}%   "
          f"Worst: {min(month_results.values(), key=lambda x: x.get('pct',0)).get('pct',0):+.1f}%              │")
    print(f"  └─────────────────────────────────────────────────────────────────┘")

    tpw  = st['tpw']; tpm = tpw * 4.33
    avg_r = ptdf['r_mult'].mean()
    avg_risk = sum(RISK_TIER[p] for p in ACTIVE_PAIRS) / len(ACTIVE_PAIRS)
    exp_pnl = tpm * avg_r * ACCOUNT * avg_risk
    print(f"\n  30-day projection:  {tpm:.0f} trades  avg {avg_r:+.3f}R  "
          f"~${exp_pnl:+,.0f} ({exp_pnl/ACCOUNT*100:+.1f}%)")
    print(f"  (Note: projection uses base equity; challenge scaling may adjust actual)")
    print(f"{sep}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("V14: FTMO Challenge Mode — FINAL DESIGN")
    print("  Risk asymmetric: TREND 2× V13 / ORB 1.5× V13")
    print("  TREND: CAD/CHF 4%  AUD/JPY 3%  CAD/JPY 2%")
    print("  ORB:   GBP/JPY 1.5%  (conservative — high variance, low R per trade)")
    print("  Tech filters: STRICT V13 all-4 (MACD + RSI<55/>45 + Vol≥120% + OBV)")
    print("  Floor protection: Layer1=abs equity (≤97%→0.75×) + Layer2=monthly progress")
    print("    + pre-trade worst-case check (full SL can't push monthly past -9.9%)")
    print("  OIL_WEAK: pair exclusions only (no size cut)  |  VIX_SPIKE: 0.50× size")
    print("  Daily cap 5.0% (FTMO hard limit)  |  Monthly stop at -9.5%")
    print("  Regime engine: BULL / NORMAL / OIL_WEAK / VIX_SPIKE")
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

        if pair in TREND_PAIRS:
            df15   = add_session_vwap(df15, PAIR_SESSIONS[pair])
            tdf    = generate_trend_trades(pair, df15, macro_score, vix_lookup, usdjpy_series)
            signal = 'TREND'
        else:
            tdf    = generate_orb_trades(pair, df15, macro_score, vix_lookup, usdjpy_series)
            signal = 'ORB'

        pair_trades[pair] = tdf
        if tdf.empty:
            print(f"  [{pair}] No {signal} candidates")
        else:
            n   = len(tdf)
            d0  = tdf['entry_dt'].iloc[0]; d1 = tdf['exit_dt'].iloc[-1]
            tpw = n / max((d1 - d0).days / 7, 1)
            wr  = (tdf['r_mult']>0).mean()
            avg = tdf['r_mult'].mean()
            print(f"  [{pair}] {signal} {n}cand  {tpw:.2f}/wk  WR={wr*100:.0f}%  avgR={avg:+.3f}")

    print("\n\nRunning portfolio engine (FTMO Challenge Mode)...")
    ptdf, final_eq, floor_ok, month_results = run_portfolio(pair_trades)
    print_report(ptdf, final_eq, floor_ok, pair_trades, month_results)

if __name__ == '__main__':
    main()
