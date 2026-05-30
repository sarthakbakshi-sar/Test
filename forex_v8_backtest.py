#!/usr/bin/env python3
"""
Forex RORO Backtest V8 — Selective Portfolio (Quality + Frequency)
===================================================================
V7 lesson: score=2 trades have WR=8% (pure noise), breakout signal WR=0%.
V8 fixes this and keeps only what worked from V7:

  KEPT from V7:
    Asian session 00:00-03:00 UTC for JPY pairs (Asian WR=38%, +$4k profit)
    Extended London 07:30-10:00 UTC (more entry time, same quality)
    Extended NY 14:00-17:00 UTC
    Portfolio-level FTMO (unified equity, 1 trade per pair per day)

  REVERTED to V6:
    SCORE_LONG = 6.0 (4/5 macro components — proven edge)
    4H EMA filter restored (helps WR for CAD/CHF)
    VWAP zone unchanged: long -1.0σ to +0.3σ, short -0.3σ to +1.0σ
    No range breakout (V7: 0% WR, all stopped out)

  NEW vs V6:
    RISK_PCT = 0.02 (2% base risk, up from 1%)
    3-tier sizing: 1.5× for score=10, 1.25× for score=6, 1.0× base

Portfolio target: ~3-4 trades/week = 12-18 trades per 30-day challenge.
At 50% WR, 1.5:1 R:R, 2% risk: expected +$6-9k/month (5-7.5% ROI).
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
RISK_PCT      = 0.02      # V8: 2% base risk (up from 1% in V6)
SL_MULT       = 0.75
TP1_MULT      = 1.50
TP2_MULT      = 2.50
TP1_FRAC      = 0.60
TP2_FRAC      = 0.40
SCORE_LONG    =  6.0      # V8: restored from V6 (4/5 components must agree)
SCORE_SHORT   = -6.0
TIME_BARS     = 10        # 10×15m = 2.5h max hold

FTMO_DAILY_LIMIT = ACCOUNT * 0.05   # $6,000
FTMO_FLOOR       = ACCOUNT * 0.90   # $108,000

PAIRS = ['AUD/JPY', 'AUD/CHF', 'AUD/NZD', 'AUD/USD', 'CAD/CHF', 'CAD/JPY', 'CHF/JPY']

# V8: Asian session added for JPY pairs, extended London and NY
SESSIONS = {
    'asian':       {'vwap_h': 0.0,  'entry_s': 0.5,  'entry_e': 3.0},
    'london_open': {'vwap_h': 7.0,  'entry_s': 7.5,  'entry_e': 10.0},
    'ny_overlap':  {'vwap_h': 13.5, 'entry_s': 14.0, 'entry_e': 17.0},
}

PAIR_SESSIONS = {
    'AUD/JPY': ['asian', 'london_open', 'ny_overlap'],
    'CAD/JPY': ['asian', 'london_open', 'ny_overlap'],
    'CHF/JPY': ['asian', 'london_open', 'ny_overlap'],
    'AUD/CHF': ['london_open', 'ny_overlap'],
    'AUD/NZD': ['london_open', 'ny_overlap'],
    'AUD/USD': ['london_open', 'ny_overlap'],
    'CAD/CHF': ['london_open', 'ny_overlap'],
}

PAIR_ATR = {
    'AUD/JPY': (0.0004, 0.0120),
    'AUD/CHF': (0.0002, 0.0080),
    'AUD/NZD': (0.0002, 0.0055),
    'AUD/USD': (0.0002, 0.0080),
    'CAD/CHF': (0.0002, 0.0080),
    'CAD/JPY': (0.0004, 0.0120),
    'CHF/JPY': (0.0003, 0.0120),
}

JPY_PAIRS       = {'AUD/JPY', 'CAD/JPY', 'CHF/JPY'}
LONG_ONLY_PAIRS = {'AUD/JPY', 'CAD/JPY', 'CHF/JPY'}

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
    if macro_abs >= 8.0: return 1.50   # score=10: all 5 agree → 3% effective risk
    if macro_abs >= 6.0: return 1.25   # score=6:  4/5 agree → 2.5%
    return 1.00                         # fallback

# ── DATA LOADERS (same as V6) ─────────────────────────────────────────────────
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

# ── MACRO SCORE (V6 pair-specific logic + EMA20 gate) ────────────────────────
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

# ── TRADE GENERATOR (per-pair, no FTMO) ───────────────────────────────────────
def generate_pair_trades(pair, df, macro_score, vix_series, usdjpy_series=None):
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
        ema9_4h  = row.get('ema9_4h',  np.nan)
        ema21_4h = row.get('ema21_4h', np.nan)
        rsi1h    = row.get('rsi_1h',   np.nan)
        vwap     = row.get('vwap',     np.nan)
        vstd     = row.get('vwap_std', np.nan)
        vbars    = row.get('vwap_bars', 0)

        if any(pd.isna(v) for v in [atr, atr_pct, ema9, ema21, vwap, vstd]):
            i += 1; continue

        # ── Filters (V6-identical) ────────────────────────────────────────────

        if not pd.isna(ema50_1h):
            if direction == 1  and price < ema50_1h: i += 1; continue
            if direction == -1 and price > ema50_1h: i += 1; continue

        if is_jpy and direction == 1 and usdjpy is not None:
            if jpy_carry_guard(date_key, usdjpy):
                i += 1; continue

        if not (atr_min <= atr_pct <= atr_max):
            i += 1; continue

        if vbars < 4:
            i += 1; continue

        # 4H EMA (restored from V6 — V7 removed it and WR dropped)
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

        # VWAP zone (V6 original — not widened)
        dist = (price - vwap) / vstd
        if direction == 1  and not (-1.0 <= dist <= 0.3): i += 1; continue
        if direction == -1 and not (-0.3 <= dist <= 1.0): i += 1; continue

        # ── Entry ─────────────────────────────────────────────────────────────
        entry_px  = price
        entry_dt  = row.name
        size_mult = get_size_mult(abs(macro))

        if direction == 1:
            sl_px  = entry_px - SL_MULT  * atr
            tp1_px = entry_px + TP1_MULT * atr
            tp2_px = entry_px + TP2_MULT * atr
        else:
            sl_px  = entry_px + SL_MULT  * atr
            tp1_px = entry_px - TP1_MULT * atr
            tp2_px = entry_px - TP2_MULT * atr

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
            pair      = pair,
            entry_dt  = entry_dt,
            exit_dt   = exit_dt,
            direction = direction,
            outcome   = outcome,
            session   = sess_name,
            r_mult    = r_mult,
            macro     = macro,
            size_mult = size_mult,
        ))
        i = j + 1

    return pd.DataFrame(trades_out)

# ── PORTFOLIO ENGINE ──────────────────────────────────────────────────────────
def run_portfolio(pair_trades_dict):
    """
    Unified FTMO-aware portfolio backtest.
    Merges all pair candidates, sorts by entry time, applies:
      - Daily -$6k limit (portfolio P&L, not per-pair)
      - $108k absolute floor
      - 1 trade per pair per calendar day
    Recomputes P&L using live compound equity.
    """
    all_cands = []
    for pair, tdf in pair_trades_dict.items():
        if tdf.empty:
            continue
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
            challenge_ok = False
            break

        if equity - day_start_eq[entry_date] <= -FTMO_DAILY_LIMIT:
            continue

        key = (trade['pair'], entry_date)
        if key in last_pair_day:
            continue

        risk_amt = equity * RISK_PCT * trade['size_mult']
        pnl      = trade['r_mult'] * risk_amt
        equity  += pnl

        last_pair_day.add(key)
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
    tdf2  = tdf.copy()
    tdf2['month'] = tdf2['entry_dt'].dt.to_period('M')
    monthly = (tdf2.groupby('month')
               .agg(n=('pnl', 'count'), pnl=('pnl', 'sum'),
                    wr=('r_mult', lambda x: (x > 0).mean()))
               .reset_index())
    return dict(n=n, wr=wr, roi=roi, sharpe=sr, p_val=p1, mdd=mdd,
                tpw=tpw, monthly=monthly)

# ── REPORTING ─────────────────────────────────────────────────────────────────
def print_report(ptdf, final_eq, challenge_ok, pair_trades_dict):
    if ptdf.empty:
        print("\nNo portfolio trades."); return

    st = compute_stats(ptdf, ACCOUNT, final_eq)
    if not st:
        return

    sep = '=' * 80
    print(f"\n{sep}")
    print(f"  PORTFOLIO V8 — Quality + Frequency (threshold=6, Asian session, 2% risk)")
    print(f"{sep}")
    print(f"  ${ACCOUNT:,.0f}  →  ${final_eq:,.0f}  ({st['roi']*100:+.1f}% ROI)")
    print(f"  Sharpe  : {st['sharpe']:.2f}   Max DD: {st['mdd']*100:.1f}%")
    print(f"  Trades  : {st['n']}  ({st['tpw']:.2f}/week  ≈  {st['tpw']/5:.2f}/day)")
    sig = '✓' if st['p_val'] < 0.05 else ('~' if st['p_val'] < 0.10 else '✗')
    print(f"  p-val   : {st['p_val']:.4f} {sig}   WR: {st['wr']*100:.1f}%")
    if not challenge_ok:
        print(f"  ⚠️  Portfolio hit $108k floor during backtest")

    print(f"\n  Candidates vs portfolio (1-per-pair-per-day filter):")
    print(f"  {'Pair':<10} {'Raw N':>6} {'Port N':>7} {'WR':>7} {'P&L':>12}  Sig")
    print(f"  {'─'*66}")
    for pair in PAIRS:
        raw_n = len(pair_trades_dict.get(pair, pd.DataFrame()))
        sub   = ptdf[ptdf['pair'] == pair]
        if sub.empty:
            print(f"  {pair:<10} {raw_n:>6} {'0':>7}")
            continue
        n   = len(sub)
        wr  = (sub['r_mult'] > 0).mean()
        pnl = sub['pnl'].sum()
        r   = sub['r_mult'].values
        if len(r) >= 5:
            t2, p2 = stats.ttest_1samp(r, 0)
            pv = p2/2 if t2 > 0 else 1.0
            sv = '✓' if pv < 0.05 else ('~' if pv < 0.10 else '✗')
        else:
            sv = '?'
        print(f"  {pair:<10} {raw_n:>6} {n:>7} {wr*100:>6.1f}% ${pnl:>+11,.0f}  {sv}")

    print(f"\n  By session:")
    for sess in ['asian', 'london_open', 'ny_overlap']:
        sub = ptdf[ptdf['session'] == sess]
        if sub.empty: continue
        wr  = (sub['r_mult'] > 0).mean()
        pnl = sub['pnl'].sum()
        tpw_s = len(sub) / max((ptdf['exit_dt'].iloc[-1] - ptdf['entry_dt'].iloc[0]).days / 7, 1)
        print(f"    {sess:<14} {len(sub):>4}tr  WR {wr*100:.0f}%  "
              f"${pnl:>+11,.0f}  {tpw_s:.2f}/wk")

    print(f"\n  Score tier:")
    for lbl, lo, hi in [('score=6  (4/5)', 5.5, 9.5),
                         ('score=10 (5/5)', 9.5, 11.0)]:
        sub = ptdf[(ptdf['macro'].abs() >= lo) & (ptdf['macro'].abs() < hi)]
        if sub.empty: continue
        wr  = (sub['r_mult'] > 0).mean()
        avg = sub['r_mult'].mean()
        pnl = sub['pnl'].sum()
        print(f"    {lbl:<20} {len(sub):>4}tr  WR {wr*100:.0f}%  avg {avg:+.3f}R  ${pnl:>+11,.0f}")

    print(f"\n  Monthly P&L:")
    eq_run = ACCOUNT
    pos    = 0
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

    # Simulated 30-day challenge window
    tpw = st['tpw']
    tpm = tpw * 4.33
    exp_r = st['wr'] * (TP1_MULT/SL_MULT*TP1_FRAC + TP2_MULT/SL_MULT*TP2_FRAC*0.5) - (1 - st['wr'])
    exp_pnl = tpm * exp_r * ACCOUNT * RISK_PCT
    print(f"\n  30-day challenge projection (rough estimate):")
    print(f"    Trades/month  : ~{tpm:.0f}")
    print(f"    Avg R/trade   : {exp_r:+.3f}R (at {st['wr']*100:.0f}% WR)")
    print(f"    Expected P&L  : ~${exp_pnl:+,.0f}  ({exp_pnl/ACCOUNT*100:+.1f}%)")
    print(f"    Need          : $12,000 (+10%)")
    print(f"{sep}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("V8: Quality portfolio — threshold=6, Asian session, 2% risk")
    print(f"    Score: ±{SCORE_LONG} (4/5 macro components)")
    print(f"    Sessions: Asian (JPY only) + London 07:30-10:00 + NY 14:00-17:00")
    print(f"    4H EMA filter: RESTORED  |  Range breakout: REMOVED")
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

        tdf = generate_pair_trades(pair, df15, macro_score, vix_lookup, usdjpy_series)
        pair_trades[pair] = tdf

        if tdf.empty:
            print(f"  [{pair}] No candidates")
        else:
            n    = len(tdf)
            d0   = tdf['entry_dt'].iloc[0]
            d1   = tdf['exit_dt'].iloc[-1]
            days = max((d1 - d0).days, 1)
            tpw  = n / (days / 7)
            wr   = (tdf['r_mult'] > 0).mean()
            avgr = tdf['r_mult'].mean()
            sess_counts = tdf['session'].value_counts().to_dict()
            print(f"  [{pair}] {n} candidates  {tpw:.2f}/wk  WR={wr*100:.0f}%  "
                  f"avgR={avgr:+.3f}  {sess_counts}")

    print("\n\nRunning portfolio engine (unified FTMO)...")
    ptdf, final_eq, challenge_ok = run_portfolio(pair_trades)

    print_report(ptdf, final_eq, challenge_ok, pair_trades)

if __name__ == '__main__':
    main()
