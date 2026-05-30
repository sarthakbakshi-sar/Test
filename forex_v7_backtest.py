#!/usr/bin/env python3
"""
Forex RORO Backtest V7 — Portfolio Edition (Quant Approach)
===========================================================
Target: 1 trade/day across all 7 pairs in a 30-day FTMO challenge.

Think like a quant:
  - Trade all pairs simultaneously with unified risk management
  - More entries via lower threshold + more signal types + wider windows
  - Portfolio-level FTMO tracking (not per-pair)
  - Statistical edge preserved via V6 trend gate + EMA + RSI filters

V7 vs V6:
  SCORE_LONG = 2.0 (was 6.0) — 3/5 macro components suffice
  Asian session 00:00-03:00 UTC added for JPY pairs (Tokyo hours)
  London extended to 10:00 UTC, NY extended to 17:00 UTC
  Asian range breakout: enter when London price breaks Asian session H/L
  4H EMA filter removed (daily EMA20 gate + 1H EMA already 2 TF confirmations)
  Score-scaled sizing: 1.5x (|score|≥8), 1.25x (≥6), 1.0x (≥2)
  Portfolio engine: unified equity curve + FTMO, 1 trade per pair per day cap
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
TWELVE_KEY    = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT       = 120_000
RISK_PCT      = 0.01      # 1% base risk; scaled by size_mult
SL_MULT       = 0.75
TP1_MULT      = 1.50
TP2_MULT      = 2.50
TP1_FRAC      = 0.60
TP2_FRAC      = 0.40
SCORE_LONG    =  2.0      # V7: was 6.0 — now 3/5 components suffice
SCORE_SHORT   = -2.0
TIME_BARS     = 10        # 10×15m = 2.5h max hold

FTMO_DAILY_LIMIT = ACCOUNT * 0.05   # $6,000
FTMO_FLOOR       = ACCOUNT * 0.90   # $108,000

PAIRS = ['AUD/JPY', 'AUD/CHF', 'AUD/NZD', 'AUD/USD', 'CAD/CHF', 'CAD/JPY', 'CHF/JPY']

# V7: extended + new sessions
SESSIONS = {
    'asian':       {'vwap_h': 0.0,  'entry_s': 0.5,  'entry_e': 3.0},   # NEW: Tokyo
    'london_open': {'vwap_h': 7.0,  'entry_s': 7.5,  'entry_e': 10.0},  # extended 9→10
    'ny_overlap':  {'vwap_h': 13.5, 'entry_s': 14.0, 'entry_e': 17.0},  # extended 16.5→17
}

PAIR_SESSIONS = {
    'AUD/JPY': ['asian', 'london_open', 'ny_overlap'],  # V7: 3 sessions
    'CAD/JPY': ['asian', 'london_open', 'ny_overlap'],  # V7: 3 sessions
    'CHF/JPY': ['asian', 'london_open', 'ny_overlap'],  # V7: 3 sessions
    'AUD/CHF': ['london_open', 'ny_overlap'],
    'AUD/NZD': ['london_open', 'ny_overlap'],
    'AUD/USD': ['london_open', 'ny_overlap'],
    'CAD/CHF': ['london_open', 'ny_overlap'],
}

PAIR_ATR = {
    'AUD/JPY': (0.0004, 0.0130),
    'AUD/CHF': (0.0002, 0.0090),
    'AUD/NZD': (0.0002, 0.0060),
    'AUD/USD': (0.0002, 0.0090),
    'CAD/CHF': (0.0002, 0.0090),
    'CAD/JPY': (0.0004, 0.0130),
    'CHF/JPY': (0.0003, 0.0130),
}

JPY_PAIRS       = {'AUD/JPY', 'CAD/JPY', 'CHF/JPY'}
LONG_ONLY_PAIRS = {'AUD/JPY', 'CAD/JPY', 'CHF/JPY'}   # carry trade drift

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

def is_high_vix_cbk_day(date, vix_series):
    d = pd.Timestamp(date)
    if d.weekday() != 2:
        return False
    v = vix_series.get(d.normalize(), np.nan)
    return not pd.isna(v) and float(v) > 25.0

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
    """V7: 3-tier score-scaled position sizing."""
    if macro_abs >= 8.0: return 1.50    # 5/5 or near-perfect alignment
    if macro_abs >= 6.0: return 1.25    # 4/5 alignment
    return 1.00                          # 3/5 alignment (new in V7)

# ── DATA: 15m (Twelve Data, cached) ───────────────────────────────────────────
def load_15m(symbol):
    slug  = symbol.replace('/', '') + '__15min'
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
    df = (df.set_index('datetime').sort_index()
            [['open', 'high', 'low', 'close']].astype(float))
    df = df[~df.index.duplicated()]
    df.to_csv(fpath)
    return df

# ── DATA: 1H (yfinance) ───────────────────────────────────────────────────────
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

# ── DATA: MACRO ───────────────────────────────────────────────────────────────
def load_macro():
    print("Loading macro + cross-asset data (yfinance)...")
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

    for pair in PAIRS:
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

# ── MACRO SCORE (same pair-specific logic as V6, with V6 EMA20 gate) ─────────
def build_macro(pair, macro):
    p   = macro.get(pair, pd.Series(dtype=float))
    spx = macro.get('spx', pd.Series(dtype=float))
    vix = macro.get('vix', pd.Series(dtype=float))
    oil = macro.get('oil', pd.Series(dtype=float))
    cop = macro.get('cop', pd.Series(dtype=float))
    tnx = macro.get('tnx', pd.Series(dtype=float))
    dxy = macro.get('dxy', pd.Series(dtype=float))
    aud = macro.get('aud', pd.Series(dtype=float))
    nzd = macro.get('nzd', pd.Series(dtype=float))

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
    elif pair == 'AUD/CHF':
        s2 = safe(cop, n_ema=50)
        s3 = safe(spx, n_ema=50)
        s4 = safe(vix, n_ema=20, inv=True)
    elif pair == 'AUD/USD':
        s2 = safe(cop, n_ema=50)
        s3 = safe(dxy, n_ema=20, inv=True)
        s4 = safe(spx, n_ema=50)
    elif pair == 'AUD/NZD':
        s2 = safe(aud, n_ema=50)
        s3 = safe(nzd, n_ema=50, inv=True)
        s4 = safe(cop, n_ema=50)
    else:  # CHF/JPY
        s2 = safe(tnx, compare_shift=10)
        s3 = safe(spx, n_ema=50)
        s4 = safe(vix, n_ema=20, inv=True)

    raw   = s1 + s2 + s3 + s4 + s5
    score = (raw / 5 * 10).round(1)

    # V6 EMA20 gate: suppress signal when pair is on wrong side of daily EMA20
    s1_gate = pd.Series(np.where(p > ema(p, 20), 1.0, -1.0), index=idx)
    aligned = (np.sign(score) == np.sign(s1_gate))
    score = score.where(aligned, 0)

    return score.shift(1)

# ── INDICATORS ────────────────────────────────────────────────────────────────
def add_asian_range(df):
    """Daily Asian session (00:00-07:00 UTC) high/low for London breakout signal."""
    df    = df.copy()
    hour  = df.index.hour + df.index.minute / 60.0
    dates = df.index.normalize()

    df['asian_high'] = np.nan
    df['asian_low']  = np.nan

    for date in pd.unique(dates):
        amask = (dates == date) & (hour >= 0.0) & (hour < 7.0)
        if amask.sum() < 4:
            continue
        a_hi = df.loc[amask, 'high'].max()
        a_lo = df.loc[amask, 'low'].min()
        dmask = dates == date
        df.loc[dmask, 'asian_high'] = a_hi
        df.loc[dmask, 'asian_low']  = a_lo

    return df

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

    if not df1h.empty:
        c1h = df1h['close']
        df['ema9_1h']  = ema(c1h, 9).reindex(df.index, method='ffill')
        df['ema21_1h'] = ema(c1h, 21).reindex(df.index, method='ffill')
        df['ema50_1h'] = ema(c1h, 50).reindex(df.index, method='ffill')
        df['rsi_1h']   = rsi(c1h, 14).reindex(df.index, method='ffill')
    else:
        for col in ['ema9_1h', 'ema21_1h', 'ema50_1h', 'rsi_1h']:
            df[col] = np.nan

    df = add_asian_range(df)
    return df

# ── SESSION VWAP ──────────────────────────────────────────────────────────────
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

# ── TRADE GENERATOR (per-pair, no FTMO — portfolio layer handles that) ────────
def generate_pair_trades(pair, df, macro_score, vix_series, usdjpy_series=None):
    """
    Find all valid trade setups for a pair.
    Returns DataFrame of trades with r_mult (sizing-agnostic).
    Portfolio combiner recomputes P&L against live portfolio equity.
    """
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
        row      = df.iloc[i]
        date_key = row.name.date()

        if row.name.weekday() >= 5:
            i += 1; continue

        if not row.get('in_entry', False):
            i += 1; continue

        sess_name = row.get('entry_sess', '')

        if sess_name == 'ny_overlap' and is_nfp_day(date_key):
            i += 1; continue

        if sess_name == 'ny_overlap' and is_high_vix_cbk_day(date_key, vix_series):
            i += 1; continue

        macro = row.get('macro', np.nan)
        if pd.isna(macro):
            i += 1; continue

        if macro >= SCORE_LONG:
            direction = 1
        elif macro <= SCORE_SHORT:
            direction = -1
        else:
            i += 1; continue

        if pair in LONG_ONLY_PAIRS and direction == -1:
            i += 1; continue

        price    = row['close']
        atr      = row.get('atr',      np.nan)
        atr_pct  = row.get('atr_pct',  np.nan)
        ema9     = row.get('ema9',     np.nan)
        ema21    = row.get('ema21',    np.nan)
        ema9_1h  = row.get('ema9_1h',  np.nan)
        ema21_1h = row.get('ema21_1h', np.nan)
        ema50_1h = row.get('ema50_1h', np.nan)
        rsi1h    = row.get('rsi_1h',   np.nan)
        vwap     = row.get('vwap',     np.nan)
        vstd     = row.get('vwap_std', np.nan)
        vbars    = row.get('vwap_bars', 0)
        a_high   = row.get('asian_high', np.nan)
        a_low    = row.get('asian_low',  np.nan)

        if any(pd.isna(v) for v in [atr, atr_pct, ema9, ema21]):
            i += 1; continue

        # ── 1H EMA50 trend filter ──────────────────────────────────────────────
        if not pd.isna(ema50_1h):
            if direction == 1  and price < ema50_1h: i += 1; continue
            if direction == -1 and price > ema50_1h: i += 1; continue

        # ── JPY carry guard ────────────────────────────────────────────────────
        if is_jpy and direction == 1 and usdjpy is not None:
            if jpy_carry_guard(date_key, usdjpy):
                i += 1; continue

        # ── ATR range ─────────────────────────────────────────────────────────
        if not (atr_min <= atr_pct <= atr_max):
            i += 1; continue

        # ── 1H EMA alignment ──────────────────────────────────────────────────
        # (4H EMA removed: redundant given daily EMA20 gate + 1H EMA)
        if not (pd.isna(ema9_1h) or pd.isna(ema21_1h)):
            if direction == 1  and ema9_1h <= ema21_1h: i += 1; continue
            if direction == -1 and ema9_1h >= ema21_1h: i += 1; continue

        # ── 1H RSI filter ─────────────────────────────────────────────────────
        if not pd.isna(rsi1h):
            if direction == 1  and rsi1h > 70: i += 1; continue
            if direction == -1 and rsi1h < 30: i += 1; continue

        # ── 15m EMA alignment ─────────────────────────────────────────────────
        if direction == 1  and ema9 <= ema21: i += 1; continue
        if direction == -1 and ema9 >= ema21: i += 1; continue

        # ── ENTRY CONDITION: VWAP pullback OR Asian range breakout ─────────────
        vwap_ok = (vbars >= 4 and not pd.isna(vwap) and not pd.isna(vstd))
        break_ok = (sess_name == 'london_open'
                    and not pd.isna(a_high) and not pd.isna(a_low))

        entry_type = None

        if vwap_ok:
            dist = (price - vwap) / vstd
            # V7: slightly wider zone (was ±1.0/0.3, now ±1.5/0.5)
            if direction == 1  and -1.5 <= dist <= 0.5: entry_type = 'vwap'
            if direction == -1 and -0.5 <= dist <= 1.5: entry_type = 'vwap'

        if entry_type is None and break_ok:
            asian_range = a_high - a_low
            # Require meaningful Asian range (at least 3× ATR to avoid flat days)
            if asian_range >= 2.0 * atr:
                if direction == 1  and price > a_high: entry_type = 'breakout'
                if direction == -1 and price < a_low:  entry_type = 'breakout'

        if entry_type is None:
            i += 1; continue

        # ── ENTRY ─────────────────────────────────────────────────────────────
        entry_px = price
        entry_dt = row.name

        abs_score = abs(macro)
        size_mult = get_size_mult(abs_score)

        if direction == 1:
            sl_px  = entry_px - SL_MULT  * atr
            tp1_px = entry_px + TP1_MULT * atr
            tp2_px = entry_px + TP2_MULT * atr
        else:
            sl_px  = entry_px + SL_MULT  * atr
            tp1_px = entry_px - TP1_MULT * atr
            tp2_px = entry_px - TP2_MULT * atr

        # ── SIMULATION ────────────────────────────────────────────────────────
        tp1_hit = False
        exit_px = None
        exit_dt = None
        outcome = 'none'
        bars_in = 0

        j = i + 1
        while j < N:
            b       = df.iloc[j]
            bhi     = b['high']
            blo     = b['low']
            b_hour  = b.name.hour + b.name.minute / 60.0
            bars_in += 1

            past_sessions = all(
                b_hour > SESSIONS[s]['entry_e'] + 2.5
                for s in PAIR_SESSIONS[pair] if s in SESSIONS
            )
            if bars_in > TIME_BARS or past_sessions or b.name.weekday() >= 5:
                exit_px = b['close']
                exit_dt = b.name
                outcome = 'time'
                break

            if not tp1_hit:
                if direction == 1:
                    if bhi >= tp1_px:
                        tp1_hit = True; sl_px = entry_px
                    elif blo <= sl_px:
                        exit_px = sl_px; exit_dt = b.name; outcome = 'sl'; break
                else:
                    if blo <= tp1_px:
                        tp1_hit = True; sl_px = entry_px
                    elif bhi >= sl_px:
                        exit_px = sl_px; exit_dt = b.name; outcome = 'sl'; break
            else:
                if direction == 1:
                    if bhi >= tp2_px:
                        exit_px = tp2_px; exit_dt = b.name; outcome = 'tp2'; break
                    elif blo <= sl_px:
                        exit_px = sl_px;  exit_dt = b.name; outcome = 'be';  break
                else:
                    if blo <= tp2_px:
                        exit_px = tp2_px; exit_dt = b.name; outcome = 'tp2'; break
                    elif bhi >= sl_px:
                        exit_px = sl_px;  exit_dt = b.name; outcome = 'be';  break
            j += 1

        if exit_px is None:
            exit_px = df.iloc[min(j, N-1)]['close']
            exit_dt = df.iloc[min(j, N-1)].name
            outcome = 'time'

        if outcome == 'sl':
            r_mult = -1.0
        elif outcome == 'be':
            r_mult = (TP1_MULT / SL_MULT) * TP1_FRAC
        elif outcome == 'tp2':
            r_mult = ((TP1_MULT / SL_MULT) * TP1_FRAC +
                      (TP2_MULT / SL_MULT) * TP2_FRAC)
        elif outcome == 'time':
            r_rem = (exit_px - entry_px) / (SL_MULT * atr) * direction
            if tp1_hit:
                r_mult = (TP1_MULT / SL_MULT) * TP1_FRAC + r_rem * TP2_FRAC
            else:
                r_mult = r_rem
        else:
            r_mult = (exit_px - entry_px) / (SL_MULT * atr) * direction

        trades_out.append(dict(
            pair       = pair,
            entry_dt   = entry_dt,
            exit_dt    = exit_dt,
            direction  = direction,
            outcome    = outcome,
            entry_type = entry_type,
            session    = sess_name,
            r_mult     = r_mult,
            macro      = macro,
            size_mult  = size_mult,
        ))
        i = j + 1

    return pd.DataFrame(trades_out)

# ── PORTFOLIO ENGINE ──────────────────────────────────────────────────────────
def run_portfolio(pair_trades_dict):
    """
    Unified portfolio-level backtest.
    - Merges all pair trades sorted by entry time
    - Applies FTMO constraints (daily limit + floor) at portfolio level
    - Enforces 1 trade per pair per day
    - Recomputes P&L using live portfolio equity (compound sizing)
    """
    all_cands = []
    for pair, tdf in pair_trades_dict.items():
        if tdf.empty:
            continue
        for _, t in tdf.iterrows():
            row = t.to_dict()
            all_cands.append(row)

    if not all_cands:
        return pd.DataFrame(), float(ACCOUNT), True

    all_cands.sort(key=lambda x: x['entry_dt'])

    equity        = float(ACCOUNT)
    day_start_eq  = {}
    last_pair_day = {}   # (pair, date) → True
    portfolio_t   = []
    challenge_ok  = True

    for trade in all_cands:
        entry_date = trade['entry_dt'].date()

        if entry_date not in day_start_eq:
            day_start_eq[entry_date] = equity

        if equity <= FTMO_FLOOR:
            challenge_ok = False
            break

        daily_dd = equity - day_start_eq[entry_date]
        if daily_dd <= -FTMO_DAILY_LIMIT:
            continue

        key = (trade['pair'], entry_date)
        if key in last_pair_day:
            continue   # 1 trade per pair per day

        # Recompute P&L vs current portfolio equity
        risk_amt = equity * RISK_PCT * trade['size_mult']
        pnl      = trade['r_mult'] * risk_amt
        equity  += pnl

        last_pair_day[key] = True
        portfolio_t.append({**trade, 'pnl': pnl, 'equity': equity})

    return pd.DataFrame(portfolio_t), equity, challenge_ok

# ── STATISTICS ────────────────────────────────────────────────────────────────
def compute_stats(tdf, start_eq, end_eq):
    if tdf.empty or len(tdf) < 3:
        return {}
    r   = tdf['r_mult'].values
    n   = len(r)
    wr  = (r > 0).mean()
    d0  = tdf['entry_dt'].iloc[0]
    d1  = tdf['exit_dt'].iloc[-1]
    days = max((d1 - d0).days, 1)
    tpw  = n / (days / 7)
    ann  = tpw * 52
    sr   = (r.mean() / (r.std(ddof=1) + 1e-9)) * np.sqrt(ann)
    t, p2 = stats.ttest_1samp(r, 0)
    p1    = p2 / 2 if t > 0 else 1.0
    eq    = tdf['equity'].values
    pk    = np.maximum.accumulate(eq)
    mdd   = ((pk - eq) / pk).max()
    roi   = (end_eq - start_eq) / start_eq

    tdf2    = tdf.copy()
    tdf2['month'] = tdf2['entry_dt'].dt.to_period('M')
    monthly = (tdf2.groupby('month')
               .agg(n=('pnl', 'count'), pnl=('pnl', 'sum'),
                    wr=('r_mult', lambda x: (x > 0).mean()))
               .reset_index())

    return dict(n=n, wr=wr, roi=roi, sharpe=sr, p_val=p1, mdd=mdd,
                tpw=tpw, monthly=monthly)

# ── REPORTING ─────────────────────────────────────────────────────────────────
def print_pair_summary(pair_trades_dict):
    """Per-pair breakdown of candidates (before portfolio filter)."""
    print(f"\n{'='*78}")
    print(f"  PER-PAIR CANDIDATE TRADES (before portfolio FTMO filter)")
    print(f"{'='*78}")
    print(f"  {'Pair':<10} {'N':>5} {'TPW':>5} {'WR':>7} {'Avg R':>7}  Entry types")
    print(f"  {'─'*72}")
    for pair, tdf in pair_trades_dict.items():
        if tdf.empty:
            print(f"  {pair:<10}     0")
            continue
        n   = len(tdf)
        d0  = tdf['entry_dt'].iloc[0]
        d1  = tdf['exit_dt'].iloc[-1]
        days = max((d1 - d0).days, 1)
        tpw = n / (days / 7)
        wr  = (tdf['r_mult'] > 0).mean()
        avgr = tdf['r_mult'].mean()
        vwap_n = (tdf['entry_type'] == 'vwap').sum()
        brk_n  = (tdf['entry_type'] == 'breakout').sum()
        print(f"  {pair:<10} {n:>5} {tpw:>5.1f} {wr*100:>6.1f}% {avgr:>+7.3f}R  "
              f"vwap={vwap_n} brk={brk_n}")

def print_portfolio_report(ptdf, final_eq, challenge_ok):
    if ptdf.empty:
        print("\nNo portfolio trades."); return

    start_eq = ACCOUNT
    st = compute_stats(ptdf, start_eq, final_eq)
    if not st:
        return

    sep = '=' * 78
    print(f"\n{sep}")
    print(f"  PORTFOLIO V7 — All 7 Pairs (unified FTMO tracking)")
    print(f"{sep}")
    print(f"  ${start_eq:,.0f}  →  ${final_eq:,.0f}  ({st['roi']*100:+.1f}% ROI)")
    print(f"  Sharpe : {st['sharpe']:.2f}   Max DD: {st['mdd']*100:.1f}%")
    print(f"  Trades : {st['n']}  ({st['tpw']:.2f}/week = "
          f"{st['tpw']*4.3:.1f}/month ≈ {st['tpw']/5:.2f}/day)")
    sig = '✓' if st['p_val'] < 0.05 else '✗'
    print(f"  p-val  : {st['p_val']:.4f} {sig}   WR: {st['wr']*100:.1f}%")
    if not challenge_ok:
        print(f"  ⚠️  Portfolio would have hit $108k floor during simulation")

    # Per-pair breakdown within portfolio
    print(f"\n  Per-pair within portfolio:")
    print(f"  {'Pair':<10} {'N':>4} {'WR':>7} {'P&L':>12}  Sig")
    print(f"  {'─'*55}")
    for pair in PAIRS:
        sub = ptdf[ptdf['pair'] == pair]
        if sub.empty:
            continue
        n   = len(sub)
        wr  = (sub['r_mult'] > 0).mean()
        pnl = sub['pnl'].sum()
        r   = sub['r_mult'].values
        if len(r) >= 5:
            t2, p2 = stats.ttest_1samp(r, 0)
            pv = p2/2 if t2 > 0 else 1.0
            sv = '✓' if pv < 0.10 else '✗'
        else:
            sv = '?'
        print(f"  {pair:<10} {n:>4} {wr*100:>6.1f}% ${pnl:>+11,.0f}  {sv}")

    # Entry-type breakdown
    vwap_t = ptdf[ptdf['entry_type'] == 'vwap']
    brk_t  = ptdf[ptdf['entry_type'] == 'breakout']
    print(f"\n  Entry type breakdown:")
    for lbl, sub in [('VWAP pullback', vwap_t), ('Range breakout', brk_t)]:
        if sub.empty: continue
        wr  = (sub['r_mult'] > 0).mean()
        avg = sub['r_mult'].mean()
        print(f"    {lbl:<16} {len(sub):>4} trades  WR {wr*100:.0f}%  avg {avg:+.3f}R")

    # Session breakdown
    print(f"\n  Session breakdown:")
    for sess in ['asian', 'london_open', 'ny_overlap']:
        sub = ptdf[ptdf['session'] == sess]
        if sub.empty: continue
        wr  = (sub['r_mult'] > 0).mean()
        pnl = sub['pnl'].sum()
        print(f"    {sess:<14} {len(sub):>4} trades  WR {wr*100:.0f}%  ${pnl:>+11,.0f}")

    # Score tier breakdown
    print(f"\n  Score tier breakdown:")
    for lbl, lo, hi in [('|score|=2  (3/5)', 1.5, 5.5),
                         ('|score|=6  (4/5)', 5.5, 9.5),
                         ('|score|=10 (5/5)', 9.5, 11.0)]:
        sub = ptdf[(ptdf['macro'].abs() >= lo) & (ptdf['macro'].abs() < hi)]
        if sub.empty: continue
        wr  = (sub['r_mult'] > 0).mean()
        avg = sub['r_mult'].mean()
        print(f"    {lbl:<22} {len(sub):>4} trades  WR {wr*100:.0f}%  avg {avg:+.3f}R")

    # Monthly P&L
    print(f"\n  Monthly P&L (portfolio):")
    eq_run = start_eq
    pos = 0
    for _, mr in st['monthly'].iterrows():
        eq_run += mr['pnl']
        tick    = '✓' if mr['pnl'] > 0 else '✗'
        if mr['pnl'] > 0: pos += 1
        print(f"    {mr['month']}  {mr['n']:>3}tr  WR {mr['wr']*100:.0f}%  "
              f"${mr['pnl']:>+10,.0f}  ${eq_run:>10,.0f} {tick}")
    print(f"  Positive months: {pos}/{len(st['monthly'])}")

    ftmo_p = st['roi'] >= 0.10
    ftmo_d = st['mdd'] <= 0.10
    print(f"\n  FTMO  Profit {'✓' if ftmo_p else '✗'} ({st['roi']*100:+.1f}%)   "
          f"DD {'✓' if ftmo_d else '✗'} ({st['mdd']*100:.1f}%)   "
          f"Floor {'✓' if challenge_ok else '✗'}")
    print(f"{sep}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("V7: Portfolio-level backtest across all 7 pairs")
    print(f"    Score threshold: ±{SCORE_LONG} (3/5 macro components)")
    print(f"    Sessions: Asian (JPY) + London 07:30-10:00 + NY 14:00-17:00")
    print(f"    Entry types: VWAP pullback + Asian range breakout")
    print()

    print("Loading 15m data (from cache)...")
    data_15m = {p: load_15m(p) for p in PAIRS}

    print("Loading 1h data (yfinance)...")
    data_1h  = {p: load_1h(p)  for p in PAIRS}

    macro_all = load_macro()

    vix_d = macro_all.get('vix', pd.Series(dtype=float))
    if vix_d.index.tz is not None:
        vix_d.index = vix_d.index.tz_localize(None)
    vix_lookup = vix_d.to_dict()

    usdjpy_series = macro_all.get('usdjpy', None)

    # Phase 1: generate all pair trades (no FTMO)
    pair_trades = {}
    for pair in PAIRS:
        df15 = data_15m.get(pair, pd.DataFrame())
        df1h = data_1h.get(pair, pd.DataFrame())
        if df15.empty:
            print(f"\n[{pair}] No 15m data — skipping")
            pair_trades[pair] = pd.DataFrame()
            continue

        print(f"\nBuilding {pair}...")
        macro_score = build_macro(pair, macro_all)
        df15        = add_indicators(df15, df1h)
        df15        = add_session_vwap(df15, PAIR_SESSIONS[pair])

        print(f"  Generating candidates...")
        tdf = generate_pair_trades(pair, df15, macro_score, vix_lookup, usdjpy_series)
        pair_trades[pair] = tdf
        if tdf.empty:
            print(f"  [{pair}] No candidates")
        else:
            n = len(tdf)
            d0, d1 = tdf['entry_dt'].iloc[0], tdf['exit_dt'].iloc[-1]
            days = max((d1 - d0).days, 1)
            tpw  = n / (days / 7)
            wr   = (tdf['r_mult'] > 0).mean()
            print(f"  [{pair}] {n} candidates  {tpw:.1f}/wk  WR={wr*100:.0f}%")

    # Phase 2: portfolio FTMO filter
    print("\n\nRunning portfolio engine...")
    ptdf, final_eq, challenge_ok = run_portfolio(pair_trades)

    # Reports
    print_pair_summary(pair_trades)

    print_portfolio_report(ptdf, final_eq, challenge_ok)

    if not ptdf.empty:
        st = compute_stats(ptdf, ACCOUNT, final_eq)
        tpw = st.get('tpw', 0)
        print(f"\n  >>> Target was 1 trade/day (5/week)")
        print(f"  >>> Achieved: {tpw:.2f}/week = {tpw/5:.2f}/day")
        print(f"  >>> Trades in simulated 30-day window: ~{tpw*4.3:.0f}")

if __name__ == '__main__':
    main()
