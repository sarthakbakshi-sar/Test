#!/usr/bin/env python3
"""
Forex RORO Backtest V12 — Technical Entry Filters on V11 Regime-Adaptive System
=================================================================================
V11 post-mortem: AUD/JPY WR 37%, system fires on macro+VWAP alone → whipsaws
in ranging/choppy conditions. Fixing by requiring 4 technical confirmations at
the moment of entry (layered on top of all V11 regime + macro + VWAP filters):

  1. MACD(12,26,9) histogram must align: >0 for longs, <0 for shorts.
     Ensures intraday momentum supports the direction at entry.

  2. RSI(14) on 15m bars: <50 for longs (buying a pullback within an uptrend),
     >50 for shorts (selling a bounce within a downtrend).
     For ORB breakouts: RSI >50 for LONG breakouts (momentum building),
     RSI <50 for SHORT breakouts.

  3. Volume ≥ 120% of 20-bar average (tick volume from TwelveData).
     Filters false/thin entries; confirms institutional participation.
     ORB breakouts especially rely on this — thin-volume breaks fail.

  4. OBV must trend in entry direction: OBV > OBV_EMA10 for longs,
     OBV < OBV_EMA10 for shorts. Confirms cumulative buying/selling pressure.

REGIME ENGINE (V11, unchanged):
  BULL      oil>E50 AND copper>E50 AND VIX<E20 AND SPX>E50
  OIL_WEAK  oil<E50  → skip CAD/JPY; lower CAD/CHF short threshold; 0.75× size
  VIX_SPIKE VIX > EMA20*1.10 → skip carry pairs; 0.50× size
  NORMAL    catch-all
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

# ── REGIME CONFIG ─────────────────────────────────────────────────────────────
# OIL_WEAK regime: lower CAD/CHF short threshold so oil-bear shorts fire
OIL_WEAK_SHORT_THRESH = -4.0   # vs normal -6.0
OIL_WEAK_SIZE         = 0.75   # reduce trend size when oil signals are unclear
# VIX_SPIKE regime: skip carry pairs, halve all sizes
VIX_SPIKE_MULT        = 0.10   # VIX must be this % above EMA20 to trigger
VIX_SPIKE_SIZE        = 0.50   # 50% size — fewer, more conservative trades

FTMO_DAILY_LIMIT = ACCOUNT * 0.05
FTMO_FLOOR       = ACCOUNT * 0.90

RISK_TIER = {
    'CAD/CHF': 0.020,
    'AUD/JPY': 0.015,
    'CAD/JPY': 0.010,
    'GBP/USD': 0.015,   # ORB — higher WR justifies equal risk to AUD/JPY
    'GBP/JPY': 0.010,   # ORB carry — long only, conservative
}

# ORB-specific parameters
ORB_RANGE_START = 6.5    # 06:30 UTC — pre-London range begins
ORB_RANGE_END   = 7.5    # 07:30 UTC — London opens, range locked
ORB_ENTRY_END   = 8.5    # 08:30 UTC — last ORB entry bar
ORB_HOLD_END    = 10.0   # 10:00 UTC — time-exit London session
ORB_MIN_BARS    = 3      # minimum bars to define valid range

MIN_RANGE_ATR   = 0.3    # range must be ≥ 0.3× ATR (not too tight)
MAX_RANGE_ATR   = 8.0    # range ≤ 8× 15m ATR: 1-hour range covers ~4 bars, so 4× ATR is normal
BREAKOUT_CONF   = 0.10   # price must break by 10% of range (filter false breaks)

# All pairs
TREND_PAIRS = ['CAD/CHF', 'AUD/JPY', 'CAD/JPY']
ORB_PAIRS   = ['GBP/JPY']        # GBP/USD ORB drops to 50% WR / +0.027R after filters — no edge after spreads
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
    'AUD/CHF': (0.0002, 0.0080),
    'CAD/CHF': (0.0002, 0.0080),
    'CAD/JPY': (0.0004, 0.0120),
    'GBP/USD': (0.0003, 0.0120),
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
    d = pd.Timestamp(date).normalize()
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
    # v2 cache includes volume column — forces re-fetch from v1 caches
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
    # Include volume if present in API response
    cols = ['open', 'high', 'low', 'close']
    if all_rows and 'volume' in all_rows[0]:
        cols.append('volume')
    df = (df.set_index('datetime').sort_index()[cols].astype(float))
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
        'spx':    '^GSPC',
        'vix':    '^VIX',
        'dxy':    'DX-Y.NYB',
        'tnx':    '^TNX',
        'oil':    'CL=F',
        'cop':    'HG=F',
        'aud':    'AUDUSD=X',
        'nzd':    'NZDUSD=X',
        'usdjpy': 'JPY=X',
        'gbp':    'GBPUSD=X',
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
    """
    Returns a daily pd.Series with regime label for each date.
    Labels: 'BULL' | 'VIX_SPIKE' | 'OIL_WEAK' | 'NORMAL'
    Priority: VIX_SPIKE > OIL_WEAK > BULL > NORMAL
    """
    oil    = macro_all.get('oil',  pd.Series(dtype=float))
    cop    = macro_all.get('cop',  pd.Series(dtype=float))
    vix    = macro_all.get('vix',  pd.Series(dtype=float))
    spx    = macro_all.get('spx',  pd.Series(dtype=float))

    if len(oil) < 60 or len(vix) < 30:
        return pd.Series(dtype=str)

    # Align all to SPX index (broadest)
    idx = spx.index
    def al(s): return s.reindex(idx, method='ffill')

    oil_bull  = al(oil) > ema(al(oil), 50)
    cop_bull  = al(cop) > ema(al(cop), 50)
    spx_bull  = al(spx) > ema(al(spx), 50)
    vix_e20   = ema(al(vix), 20)
    vix_spike = al(vix) > vix_e20 * (1 + VIX_SPIKE_MULT)  # VIX >10% above EMA20

    regime = pd.Series('NORMAL', index=idx)

    # BULL: all four risk-on factors aligned (use as forward-looking bonus)
    bull_mask = oil_bull & cop_bull & spx_bull & ~vix_spike
    regime[bull_mask] = 'BULL'

    # OIL_WEAK: oil below EMA50 (CAD signal degraded) — overrides BULL
    regime[~oil_bull & ~vix_spike] = 'OIL_WEAK'

    # VIX_SPIKE: VIX rising above EMA20 — overrides everything (highest priority)
    regime[vix_spike] = 'VIX_SPIKE'

    # Shift by 1 day (yesterday's regime informs today's trading)
    return regime.shift(1, fill_value='NORMAL')

# Cached lookup dict built once in main()
_REGIME_LOOKUP: dict = {}

def get_regime(date):
    """Returns regime string for a given date (date object or Timestamp)."""
    d = pd.Timestamp(date).normalize()
    if d.tz is not None:
        d = d.tz_localize(None)
    v = _REGIME_LOOKUP.get(d)
    if v is None:
        # Walk back up to 5 days if exact date not found (weekends/holidays)
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
    dxy = macro.get('dxy', pd.Series(dtype=float))
    gbp = macro.get('gbp', pd.Series(dtype=float))

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
    elif pair == 'GBP/USD':
        # GBP vs USD: inverse DXY strength, SPX risk, yield spread
        s2 = safe(dxy, n_ema=20, inv=True)   # USD weak → GBP/USD up
        s3 = safe(spx, n_ema=50)              # risk-on → GBP up
        s4 = safe(gbp, n_ema=50)              # GBP momentum vs EMA50
    elif pair == 'GBP/JPY':
        # GBP + carry vs JPY: risk-on + carry favourable
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

    # ── MACD(12,26,9) on 15m ──────────────────────────────────────────────────
    ema12           = ema(df['close'], 12)
    ema26           = ema(df['close'], 26)
    df['macd']      = ema12 - ema26
    df['macd_sig']  = ema(df['macd'], 9)
    df['macd_hist'] = df['macd'] - df['macd_sig']

    # ── RSI(14) on 15m ────────────────────────────────────────────────────────
    df['rsi_15m'] = rsi(df['close'], 14)

    # ── Volume indicators (tick volume from TwelveData) ───────────────────────
    if 'volume' in df.columns:
        vol = df['volume'].replace(0, np.nan)
        df['vol_ma20']  = vol.rolling(20, min_periods=10).mean()
        df['vol_ratio'] = vol / (df['vol_ma20'] + 1e-10)
        # If volume is suspiciously flat (constant tick counts) treat as unavailable
        if vol.dropna().std() < vol.dropna().mean() * 0.05:
            df['vol_ratio'] = np.nan
        # OBV from tick volume
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
        for col in ['ema9_1h', 'ema21_1h', 'ema50_1h', 'rsi_1h', 'ema9_4h', 'ema21_4h']:
            df[col] = np.nan

    return df

# ── SESSION VWAP (for TREND pairs) ────────────────────────────────────────────
def add_session_vwap(df, valid_sessions):
    df  = df.copy()
    tp  = (df['high'] + df['low'] + df['close']) / 3.0
    hr  = df.index.hour + df.index.minute / 60.0
    dts = df.index.normalize()

    df['vwap']       = np.nan
    df['vwap_std']   = np.nan
    df['vwap_bars']  = 0
    df['in_entry']   = False
    df['entry_sess'] = ''

    for sess_name, sess in SESSIONS.items():
        if sess_name not in valid_sessions:
            continue
        vh = sess['vwap_h']
        es = sess['entry_s']
        ee = sess['entry_e']
        in_sess = (hr >= vh) & (hr <= ee + 1.0) & (df.index.weekday < 5)

        for date in pd.unique(dts[in_sess]):
            mask = (dts == date) & in_sess
            if mask.sum() < 2:
                continue
            tp_d = tp.values[mask]
            n_d  = len(tp_d)
            cum  = np.cumsum(tp_d)
            cnt  = np.arange(1, n_d + 1, dtype=float)
            vw   = cum / cnt
            std  = np.zeros(n_d)
            for k in range(1, n_d):
                std[k] = np.std(tp_d[:k + 1], ddof=0)
            std[std < 1e-8] = tp_d.mean() * 0.0005
            pos = np.where(mask)[0]
            df.iloc[pos, df.columns.get_loc('vwap')]      = vw
            df.iloc[pos, df.columns.get_loc('vwap_std')]  = np.maximum(std, 1e-8)
            df.iloc[pos, df.columns.get_loc('vwap_bars')] = cnt

        entry_mask = in_sess & (hr >= es) & (hr <= ee)
        df.loc[entry_mask, 'in_entry']   = True
        df.loc[entry_mask, 'entry_sess'] = sess_name

    return df

# ── SIGNAL A: VWAP TREND (existing V8 logic) ──────────────────────────────────
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
        if row.name < MIN_DATE: i += 1; continue
        if row.name.weekday() >= 5: i += 1; continue
        if not row.get('in_entry', False): i += 1; continue

        sess_name = row.get('entry_sess', '')
        date_key  = row.name.date()

        if sess_name == 'ny_overlap' and is_nfp_day(date_key): i += 1; continue

        macro = row.get('macro', np.nan)
        if pd.isna(macro): i += 1; continue

        # ── Regime-adaptive rules ─────────────────────────────────────────
        regime = get_regime(date_key)

        # VIX_SPIKE: skip carry pairs entirely — VWAP entries whipsaw in chop
        if regime == 'VIX_SPIKE' and pair in {'AUD/JPY', 'CAD/JPY'}:
            i += 1; continue

        # OIL_WEAK: skip CAD/JPY (LONG carry has no edge when oil drives CAD down;
        # can't short because LONG_ONLY constraint)
        if regime == 'OIL_WEAK' and pair == 'CAD/JPY':
            i += 1; continue

        # Regime-based score threshold: OIL_WEAK lowers CAD/CHF short threshold
        score_short_thresh = (OIL_WEAK_SHORT_THRESH
                              if regime == 'OIL_WEAK' and pair == 'CAD/CHF'
                              else SCORE_SHORT)

        if macro >= SCORE_LONG:              direction = 1
        elif macro <= score_short_thresh:    direction = -1
        else:                                i += 1; continue

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
        if pd.isna(ema50_1h) or pd.isna(ema9_4h): i += 1; continue
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

        # ── Technical entry confirmations (V12) ──────────────────────────────
        macd_hist = row.get('macd_hist', np.nan)
        rsi_15m   = row.get('rsi_15m',  np.nan)
        vol_ratio = row.get('vol_ratio', np.nan)
        obv_v     = row.get('obv',      np.nan)
        obv_e     = row.get('obv_ema',  np.nan)

        # 1. MACD histogram must align with direction
        if not pd.isna(macd_hist):
            if direction == 1  and macd_hist <= 0: i += 1; continue
            if direction == -1 and macd_hist >= 0: i += 1; continue

        # 2. RSI(15m): <50 for longs (buying pullback), >50 for shorts (selling bounce)
        if not pd.isna(rsi_15m):
            if direction == 1  and rsi_15m >= 50: i += 1; continue
            if direction == -1 and rsi_15m <= 50: i += 1; continue

        # 3. Volume ≥ 120% of 20-bar average (only if volume data is meaningful)
        if not pd.isna(vol_ratio) and vol_ratio > 0:
            if vol_ratio < 1.20: i += 1; continue

        # 4. OBV must trend with direction
        if not (pd.isna(obv_v) or pd.isna(obv_e)):
            if direction == 1  and obv_v < obv_e: i += 1; continue
            if direction == -1 and obv_v > obv_e: i += 1; continue

        entry_px   = price
        entry_dt   = row.name
        regime_sz  = (VIX_SPIKE_SIZE if regime == 'VIX_SPIKE'
                      else OIL_WEAK_SIZE if regime == 'OIL_WEAK'
                      else 1.0)
        size_mult  = get_size_mult(abs(macro)) * regime_sz
        sl_px      = entry_px - SL_MULT * atr if direction == 1 else entry_px + SL_MULT * atr
        tp1_px    = entry_px + TP1_MULT * atr if direction == 1 else entry_px - TP1_MULT * atr
        tp2_px    = entry_px + TP2_MULT * atr if direction == 1 else entry_px - TP2_MULT * atr

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
        if outcome == 'sl':             r_mult = -1.0
        elif outcome == 'be':           r_mult = (TP1_MULT/SL_MULT) * TP1_FRAC
        elif outcome == 'tp2':          r_mult = (TP1_MULT/SL_MULT)*TP1_FRAC + (TP2_MULT/SL_MULT)*TP2_FRAC
        elif outcome == 'time':
            r_rem = (exit_px - entry_px) / atr_sl * direction
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
    """
    Pre-London range (06:30-07:30 UTC) + Asian session direction per day.
    Asian direction: which half of the Asian range did price close in?
    London ORB is taken in the Asian-session-aligned direction (London
    tends to continue the Asian session's momentum in the first hour).
    """
    hr  = df15.index.hour + df15.index.minute / 60.0
    dts = df15.index.normalize()
    levels = {}
    for date in pd.unique(dts):
        if pd.Timestamp(date).weekday() >= 5:
            continue
        mask = (dts == date) & (hr >= ORB_RANGE_START) & (hr < ORB_RANGE_END)
        if mask.sum() < ORB_MIN_BARS:
            continue
        rh  = df15.loc[mask, 'high'].max()
        rl  = df15.loc[mask, 'low'].min()
        rng = rh - rl
        if rng <= 0:
            continue
        # Asian session direction bias (00:00-06:30 UTC)
        asian_mask = (dts == date) & (hr >= 0) & (hr < ORB_RANGE_START)
        if asian_mask.sum() >= 4:
            ah = df15.loc[asian_mask, 'high'].max()
            al = df15.loc[asian_mask, 'low'].min()
            ac = df15.loc[asian_mask, 'close'].iloc[-1]
            asian_bias = 1 if ac > (ah + al) / 2 else -1
        else:
            asian_bias = 0  # neutral: take either direction
        date_key = pd.Timestamp(date).date()
        levels[date_key] = {'high': rh, 'low': rl, 'range': rng,
                            'mid': (rh + rl) / 2, 'asian_bias': asian_bias}
    return levels

def generate_orb_trades(pair, df, macro_score, vix_series, usdjpy_series=None):
    """
    Opening Range Breakout at London open (07:30-09:00 UTC).
    Direction filter: Asian session bias (London tends to continue Asian momentum).
    GBP/JPY: LONG ONLY regardless.
    No RORO macro filter — ORB is intraday momentum, not macro trend-following.
    """
    df = df.copy()
    ms = macro_score.copy()
    if ms.index.tz is None:
        ms.index = pd.to_datetime(ms.index).tz_localize('UTC')
    ms_d    = ms.groupby(ms.index.normalize()).last()
    bar_dts = pd.DatetimeIndex(df.index.normalize())
    df['macro'] = ms_d.reindex(bar_dts, method='ffill').values

    is_jpy   = pair in JPY_PAIRS
    orb_levels = compute_orb_levels(df)

    usdjpy = None
    if usdjpy_series is not None and len(usdjpy_series) > 5:
        usdjpy = usdjpy_series.copy()
        if usdjpy.index.tz is not None:
            usdjpy.index = usdjpy.index.tz_localize(None)
        usdjpy.index = pd.to_datetime(usdjpy.index).normalize()

    trades_out = []
    triggered_days = set()
    i, N = 0, len(df)

    while i < N - 1:
        row = df.iloc[i]
        if row.name < MIN_DATE: i += 1; continue
        if row.name.weekday() >= 5: i += 1; continue

        hr = row.name.hour + row.name.minute / 60.0
        # Extended entry window to 09:00 UTC (some breakouts are delayed)
        if not (ORB_RANGE_END <= hr <= 9.0): i += 1; continue

        date_key = row.name.date()
        if date_key in triggered_days: i += 1; continue

        lvl = orb_levels.get(date_key)
        if lvl is None: i += 1; continue

        price = row['close']
        atr   = row.get('atr', np.nan)
        if pd.isna(atr): i += 1; continue

        rng = lvl['range']
        if not (MIN_RANGE_ATR * atr <= rng): i += 1; continue  # only min check, no max

        # Determine breakout direction from range
        break_long  = price > lvl['high'] + BREAKOUT_CONF * rng
        break_short = price < lvl['low']  - BREAKOUT_CONF * rng
        if not break_long and not break_short: i += 1; continue

        direction = 1 if break_long else -1

        # Long-only constraint for carry pairs
        if pair in LONG_ONLY_PAIRS and direction == -1: i += 1; continue

        # Asian session direction filter: skip breakouts opposing Asian momentum
        # (neutral asian_bias = 0 allows either direction)
        asian_bias = lvl.get('asian_bias', 0)
        if asian_bias == 1  and direction == -1: i += 1; continue
        if asian_bias == -1 and direction == 1:  i += 1; continue

        # Standard event risk filters
        if is_nfp_day(date_key): i += 1; continue
        if is_high_vix_day(date_key, vix_series): i += 1; continue

        # JPY carry guard
        if is_jpy and direction == 1 and usdjpy is not None:
            if jpy_carry_guard(date_key, usdjpy): i += 1; continue

        # Data quality: require 1H EMA warmup (past the unstable early period)
        ema50_1h = row.get('ema50_1h', np.nan)
        ema9_4h  = row.get('ema9_4h',  np.nan)
        if pd.isna(ema50_1h) or pd.isna(ema9_4h): i += 1; continue

        # ── Technical entry confirmations for ORB (V12) ──────────────────────
        macd_hist = row.get('macd_hist', np.nan)
        rsi_15m   = row.get('rsi_15m',  np.nan)
        vol_ratio = row.get('vol_ratio', np.nan)
        obv_v     = row.get('obv',      np.nan)
        obv_e     = row.get('obv_ema',  np.nan)

        # 1. MACD histogram aligns with breakout direction
        if not pd.isna(macd_hist):
            if direction == 1  and macd_hist <= 0: i += 1; continue
            if direction == -1 and macd_hist >= 0: i += 1; continue

        # 2. RSI for ORB: >50 confirms bullish breakout momentum; <50 bearish
        if not pd.isna(rsi_15m):
            if direction == 1  and rsi_15m <= 50: i += 1; continue
            if direction == -1 and rsi_15m >= 50: i += 1; continue

        # 3. Volume surge ≥ 120% average — false ORB breakouts are thin-volume
        if not pd.isna(vol_ratio) and vol_ratio > 0:
            if vol_ratio < 1.20: i += 1; continue

        # 4. OBV direction confirms breakout
        if not (pd.isna(obv_v) or pd.isna(obv_e)):
            if direction == 1  and obv_v < obv_e: i += 1; continue
            if direction == -1 and obv_v > obv_e: i += 1; continue

        # SL = opposite range edge (natural invalidation of breakout)
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
        entry_px  = price
        entry_dt  = row.name

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

# ── PORTFOLIO ENGINE ──────────────────────────────────────────────────────────
def run_portfolio(pair_trades_dict):
    all_cands = []
    for pair, tdf in pair_trades_dict.items():
        if tdf.empty: continue
        for _, t in tdf.iterrows():
            all_cands.append(t.to_dict())
    if not all_cands:
        return pd.DataFrame(), float(ACCOUNT), True

    all_cands.sort(key=lambda x: x['entry_dt'])
    equity        = float(ACCOUNT)
    day_start_eq  = {}
    last_pair_day = set()
    portfolio_t   = []
    challenge_ok  = True

    for trade in all_cands:
        entry_date = trade['entry_dt'].date()
        if entry_date not in day_start_eq:
            day_start_eq[entry_date] = equity
        if equity <= FTMO_FLOOR:
            challenge_ok = False; break
        if equity - day_start_eq[entry_date] <= -FTMO_DAILY_LIMIT:
            continue
        key = (trade['pair'], entry_date)
        if key in last_pair_day:
            continue

        risk_pct = RISK_TIER.get(trade['pair'], 0.015)
        risk_amt = equity * risk_pct * trade['size_mult']
        pnl      = trade['r_mult'] * risk_amt
        equity  += pnl
        last_pair_day.add(key)
        portfolio_t.append({**trade, 'pnl': pnl, 'equity': equity})

    return pd.DataFrame(portfolio_t), equity, challenge_ok

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
def print_report(ptdf, final_eq, challenge_ok, pair_trades_dict):
    if ptdf.empty: print("\nNo portfolio trades."); return
    st = compute_stats(ptdf, ACCOUNT, final_eq)
    if not st: return

    sep = '=' * 80
    print(f"\n{sep}")
    print(f"  PORTFOLIO V12 — TECHNICAL FILTERS: MACD + RSI(15m) + Volume + OBV")
    print(f"{sep}")
    print(f"  ${ACCOUNT:,.0f}  →  ${final_eq:,.0f}  ({st['roi']*100:+.1f}% ROI)")
    print(f"  Sharpe  : {st['sharpe']:.2f}   Max DD: {st['mdd']*100:.1f}%")
    print(f"  Trades  : {st['n']}  ({st['tpw']:.2f}/week  ≈  {st['tpw']/5:.2f}/day)")
    sig = '✓' if st['p_val'] < 0.05 else ('~' if st['p_val'] < 0.10 else '✗')
    print(f"  p-val   : {st['p_val']:.4f} {sig}   WR: {st['wr']*100:.1f}%")
    if not challenge_ok: print(f"  ⚠️  Portfolio hit $108k floor during backtest")

    print(f"\n  By pair:")
    for pair in ACTIVE_PAIRS:
        raw_n = len(pair_trades_dict.get(pair, pd.DataFrame()))
        sub   = ptdf[ptdf['pair'] == pair]
        if sub.empty: print(f"  {pair:<10} {raw_n:>6} candidates  0 portfolio"); continue
        n = len(sub); wr = (sub['r_mult']>0).mean(); pnl = sub['pnl'].sum()
        sig2 = '?'
        if len(sub['r_mult'].values) >= 5:
            t2, p2 = stats.ttest_1samp(sub['r_mult'].values, 0)
            pv = p2/2 if t2 > 0 else 1.0
            sig2 = '✓' if pv < 0.05 else ('~' if pv < 0.10 else '✗')
        stype = sub['signal_type'].iloc[0]
        print(f"  {pair:<10} {raw_n:>5}raw {n:>5}port  WR {wr*100:.0f}%  avg {sub['r_mult'].mean():+.3f}R  ${pnl:>+10,.0f}  [{stype}] {sig2}")

    print(f"\n  By signal type:")
    for stype in ['trend', 'orb']:
        sub = ptdf[ptdf['signal_type'] == stype]
        if sub.empty: continue
        wr  = (sub['r_mult']>0).mean()
        avg = sub['r_mult'].mean()
        pnl = sub['pnl'].sum()
        tpw_s = len(sub) / max((ptdf['exit_dt'].iloc[-1] - ptdf['entry_dt'].iloc[0]).days / 7, 1)
        print(f"    {stype:<8} {len(sub):>4}tr  WR {wr*100:.0f}%  avg {avg:+.3f}R  ${pnl:>+11,.0f}  {tpw_s:.2f}/wk")

    if 'regime' in ptdf.columns:
        print(f"\n  By regime:")
        for reg in ['BULL', 'NORMAL', 'OIL_WEAK', 'VIX_SPIKE']:
            sub = ptdf[ptdf['regime'] == reg]
            if sub.empty: continue
            wr  = (sub['r_mult'] > 0).mean()
            avg = sub['r_mult'].mean()
            pnl = sub['pnl'].sum()
            print(f"    {reg:<12} {len(sub):>4}tr  WR {wr*100:.0f}%  avg {avg:+.3f}R  ${pnl:>+11,.0f}")

    print(f"\n  Monthly P&L:")
    eq_run = ACCOUNT; pos = 0; months_12 = 0
    for _, mr in st['monthly'].iterrows():
        eq_run += mr['pnl']
        tick = '✓' if mr['pnl'] > 0 else '✗'
        if mr['pnl'] > 0: pos += 1
        pct = mr['pnl'] / ACCOUNT * 100
        if pct >= 12: months_12 += 1
        flag = ' ★' if pct >= 12 else ''
        print(f"    {mr['month']}  {mr['n']:>3}tr  WR {mr['wr']*100:.0f}%  "
              f"${mr['pnl']:>+10,.0f} ({pct:+.1f}%)  ${eq_run:>10,.0f} {tick}{flag}")
    print(f"  Positive: {pos}/{len(st['monthly'])}  |  ≥12%: {months_12}/{len(st['monthly'])}")

    ftmo_p = st['roi'] >= 0.10; ftmo_d = st['mdd'] <= 0.10
    print(f"\n  FTMO  Profit {'✓' if ftmo_p else '✗'} ({st['roi']*100:+.1f}%)  "
          f"DD {'✓' if ftmo_d else '✗'} ({st['mdd']*100:.1f}%)  "
          f"Floor {'✓' if challenge_ok else '✗'}")

    tpw = st['tpw']; tpm = tpw * 4.33
    avg_r = ptdf['r_mult'].mean()
    avg_risk = sum(RISK_TIER[p] for p in ACTIVE_PAIRS) / len(ACTIVE_PAIRS)
    exp_pnl = tpm * avg_r * ACCOUNT * avg_risk
    print(f"\n  30-day projection:  {tpm:.0f} trades  avg {avg_r:+.3f}R  ~${exp_pnl:+,.0f} ({exp_pnl/ACCOUNT*100:+.1f}%)")
    print(f"{sep}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("V12: Technical entry filters (MACD + RSI 15m + Volume + OBV) on V11 base")
    print("  TREND: CAD/CHF 2%  AUD/JPY 1.5%  CAD/JPY 1%  (macro+VWAP+4 tech filters)")
    print("  ORB:   GBP/JPY 1%  (ORB breakout + volume surge + MACD + OBV confirm)")
    print("  Regime engine: BULL / NORMAL / OIL_WEAK / VIX_SPIKE")
    print()

    print("Loading 15m data...")
    data_15m = {p: load_15m(p) for p in ACTIVE_PAIRS}

    print("Loading 1h data (yfinance)...")
    data_1h = {p: load_1h(p) for p in ACTIVE_PAIRS}

    macro_all = load_macro()

    # Build regime lookup (used by get_regime() inside trade generators)
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
    vix_lookup = vix_d.to_dict()
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

    print("\n\nRunning portfolio engine (unified FTMO)...")
    ptdf, final_eq, challenge_ok = run_portfolio(pair_trades)
    print_report(ptdf, final_eq, challenge_ok, pair_trades)

if __name__ == '__main__':
    main()
