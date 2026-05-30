#!/usr/bin/env python3
"""
Forex RORO Backtest V2 — FTMO $120k Challenge
==============================================
Cross-asset + macro + micro improvements over V1:

  MACRO (pair-specific fundamental drivers):
    CAD pairs  → WTI crude oil vs EMA50 (oil = CAD's primary driver)
    AUD pairs  → Copper (HG=F) vs EMA50 (China/commodity demand proxy)
    JPY pairs  → US 10Y yield direction (carry trade signal)
    CHF pairs  → VIX vs 20d MA (safe-haven demand, inverse)
    All pairs  → SPX regime + pair vs EMA50 + pair vs EMA200

  SESSIONS (three windows):
    Asian       00:00–02:30 UTC  (AUD/JPY, AUD/NZD only)
    London open 07:00–09:00 UTC  (all pairs)
    NY overlap  13:30–16:30 UTC  (all pairs)

  MICRO filters:
    1H RSI(14)  — no overbought longs, no oversold shorts
    Min 4 VWAP bars — discard low-info early-session entries
    VWAP zone  — same -1σ/+0.3σ logic as gold/BTC systems
    4H + 1H + 15m EMA alignment — three-TF confirmation
    ATR% filter — pair-specific volatility range

  NEWS:
    NFP avoidance — skip NY session on first Friday of month
    CBK high-impact — FOMC / BoJ Wednesday windows skipped if VIX > 25

  SIZING:
    |score| ≥ 8 → 1.25× base risk
    |score| ≥ 6 → 1.00× base risk
    FTMO constraints: daily -$6k floor, absolute -$12k floor
"""

import os, time, calendar, warnings
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
RISK_PCT      = 0.01
SL_MULT       = 0.75
TP1_MULT      = 1.50
TP2_MULT      = 2.50
TP1_FRAC      = 0.60
TP2_FRAC      = 0.40
SCORE_LONG    =  6.0
SCORE_SHORT   = -6.0
TIME_BARS     = 8      # max bars before time-exit

FTMO_DAILY_LIMIT = ACCOUNT * 0.05    # $6,000
FTMO_FLOOR       = ACCOUNT * 0.90    # $108,000

PAIRS = ['AUD/JPY', 'AUD/CHF', 'AUD/NZD', 'AUD/USD', 'CAD/CHF', 'CAD/JPY', 'CHF/JPY']

# Sessions: (VWAP reset hour, entry start, entry end) in decimal UTC
SESSIONS = {
    'asian':       {'vwap_h': 0.0,  'entry_s': 0.5,  'entry_e': 2.5},
    'london_open': {'vwap_h': 7.0,  'entry_s': 7.5,  'entry_e': 9.0},
    'ny_overlap':  {'vwap_h': 13.5, 'entry_s': 14.0, 'entry_e': 16.5},
}

PAIR_SESSIONS = {
    'AUD/JPY': ['asian', 'london_open', 'ny_overlap'],
    'AUD/CHF': ['london_open', 'ny_overlap'],
    'AUD/NZD': ['asian', 'london_open', 'ny_overlap'],
    'AUD/USD': ['london_open', 'ny_overlap'],
    'CAD/CHF': ['london_open', 'ny_overlap'],
    'CAD/JPY': ['london_open', 'ny_overlap'],
    'CHF/JPY': ['london_open', 'ny_overlap'],
}

PAIR_ATR = {         # (min_pct, max_pct) for 15m ATR/price
    'AUD/JPY': (0.0006, 0.0100),
    'AUD/CHF': (0.0003, 0.0070),
    'AUD/NZD': (0.0002, 0.0050),
    'AUD/USD': (0.0003, 0.0070),
    'CAD/CHF': (0.0003, 0.0070),
    'CAD/JPY': (0.0006, 0.0100),
    'CHF/JPY': (0.0004, 0.0100),
}

# ── MATH HELPERS ──────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d     = s.diff()
    gain  = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    loss  = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

def is_nfp_day(date):
    """True if date is the first Friday of the month (US NFP release day)."""
    d = pd.Timestamp(date)
    return d.weekday() == 4 and d.day <= 7

def is_high_vix_cbk_day(date, vix_series):
    """True if VIX > 25 on a Wednesday (proxy for FOMC/BoJ day caution)."""
    d = pd.Timestamp(date)
    if d.weekday() != 2:  # 2 = Wednesday
        return False
    v = vix_series.get(d.normalize(), np.nan)
    if pd.isna(v):
        return False
    return float(v) > 25.0

# ── DATA: 15m (Twelve Data, cached) ───────────────────────────────────────────
def load_15m(symbol):
    slug  = symbol.replace('/', '') + '__15min'
    fpath = os.path.join(CACHE_DIR, slug + '.csv')
    if os.path.exists(fpath):
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        return df
    # Fetch if not cached (same logic as forex_backtest.py)
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

# ── DATA: 1H (yfinance, free) ─────────────────────────────────────────────────
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

# ── DATA: MACRO (yfinance daily + cross-asset) ────────────────────────────────
def load_macro():
    print("Loading macro + cross-asset data (yfinance)...")
    macro = {}
    fetch_map = {
        'spx':   '^GSPC',    # S&P 500 — risk-on/off
        'vix':   '^VIX',     # volatility regime
        'dxy':   'DX-Y.NYB', # USD strength
        'tnx':   '^TNX',     # US 10Y yield — carry signal
        'oil':   'CL=F',     # WTI crude — CAD fundamental driver
        'cop':   'HG=F',     # Copper — AUD/China demand proxy
        'aud':   'AUDUSD=X', # AUD direction (for AUD/NZD macro)
        'nzd':   'NZDUSD=X', # NZD direction (for AUD/NZD macro)
        'nkx':   '^N225',    # Nikkei — JPY weakness signal
        'bhp':   'BHP',      # BHP (iron ore proxy for AUD)
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

    # Daily pair closes for EMA50/EMA200
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

# ── MACRO SCORE (pair-specific) ───────────────────────────────────────────────
def build_macro(pair, macro):
    """
    Five-component macro score, fully pair-specific.
    Always: s1 = pair vs EMA50   s5 = pair vs EMA200 (regime gate)
    Middle three (s2, s3, s4) depend on what drives each pair.
    Returns daily Series, shifted 1 day (yesterday's signal → today's trade).
    """
    p   = macro.get(pair, pd.Series(dtype=float))
    spx = macro.get('spx', pd.Series(dtype=float))
    vix = macro.get('vix', pd.Series(dtype=float))
    oil = macro.get('oil', pd.Series(dtype=float))
    cop = macro.get('cop', pd.Series(dtype=float))
    tnx = macro.get('tnx', pd.Series(dtype=float))
    dxy = macro.get('dxy', pd.Series(dtype=float))
    aud = macro.get('aud', pd.Series(dtype=float))
    nzd = macro.get('nzd', pd.Series(dtype=float))
    nkx = macro.get('nkx', pd.Series(dtype=float))

    if len(p) < 60:
        return pd.Series(dtype=float)

    idx = p.index

    # Universal: s1 = pair vs EMA50, s5 = pair vs EMA200
    s1 = pd.Series(np.where(p > ema(p, 50), 1.0, -1.0), index=idx)
    s5 = pd.Series(np.where(p > ema(p, 200), 1.0, -1.0), index=idx)

    def safe(series, n_ema=None, inv=False, compare_shift=None):
        """Reindex and compute EMA direction for a yfinance macro series."""
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
        # Oil drives CAD; VIX (inverted) = CHF safe-haven demand; SPX = risk-on
        s2 = safe(oil, n_ema=50)           # oil rising  → CAD up → CAD/CHF up
        s3 = safe(spx, n_ema=50)           # risk-on     → CHF weaker → CAD/CHF up
        s4 = safe(vix, n_ema=20, inv=True) # low VIX     → CHF weaker → CAD/CHF up

    elif pair == 'CAD/JPY':
        # Oil drives CAD; US-Japan carry (TNX direction) drives JPY
        s2 = safe(oil, n_ema=50)           # oil rising  → CAD up → CAD/JPY up
        s3 = safe(tnx, compare_shift=10)   # US yields rising → JPY weaker → CAD/JPY up
        s4 = safe(spx, n_ema=50)           # risk-on     → JPY weaker → CAD/JPY up

    elif pair == 'AUD/JPY':
        # Copper (China demand) drives AUD; TNX = carry; Nikkei = JPY signal
        s2 = safe(cop, n_ema=50)           # copper up   → AUD up → AUD/JPY up
        s3 = safe(spx, n_ema=50)           # risk-on     → JPY weaker → AUD/JPY up
        s4 = safe(tnx, compare_shift=10)   # US yields rising → carry trade → AUD/JPY up

    elif pair == 'AUD/CHF':
        # Copper drives AUD; VIX drives CHF (inverse)
        s2 = safe(cop, n_ema=50)           # copper up   → AUD up → AUD/CHF up
        s3 = safe(spx, n_ema=50)           # risk-on     → CHF weaker → AUD/CHF up
        s4 = safe(vix, n_ema=20, inv=True) # low VIX     → CHF weaker → AUD/CHF up

    elif pair == 'AUD/USD':
        # Copper drives AUD; DXY drives USD (inverse); SPX = risk sentiment
        s2 = safe(cop, n_ema=50)           # copper up   → AUD up → AUD/USD up
        s3 = safe(dxy, n_ema=20, inv=True) # DXY falling → USD weaker → AUD/USD up
        s4 = safe(spx, n_ema=50)           # risk-on     → AUD outperforms USD

    elif pair == 'AUD/NZD':
        # AUD vs NZD spread: need AUD strong AND NZD weak
        s2 = safe(aud, n_ema=50)           # AUD/USD up  → AUD strengthening
        s3 = safe(nzd, n_ema=50, inv=True) # NZD/USD down → NZD weakening → AUD/NZD up
        s4 = safe(cop, n_ema=50)           # copper up   → AUD (not NZD) outperforms

    else:  # CHF/JPY
        # Both safe havens: JPY weakens more from carry; TNX = carry trigger
        s2 = safe(tnx, compare_shift=10)   # US yields rising → JPY weakens more than CHF
        s3 = safe(spx, n_ema=50)           # risk-on     → JPY weakens more (carry unwind)
        s4 = safe(vix, n_ema=20, inv=True) # low VIX     → risk-on → CHF/JPY up (JPY weaker)

    raw   = s1 + s2 + s3 + s4 + s5
    score = (raw / 5 * 10).round(1)
    return score.shift(1)   # prior-day close drives today's signal

# ── INDICATORS (15m + 1H RSI) ─────────────────────────────────────────────────
def add_indicators(df15, df1h):
    df = df15.copy()

    # 15m EMA9/21 and ATR14
    df['ema9']  = ema(df['close'], 9)
    df['ema21'] = ema(df['close'], 21)
    prev  = df['close'].shift(1)
    tr    = pd.concat([df['high'] - df['low'],
                       (df['high'] - prev).abs(),
                       (df['low']  - prev).abs()], axis=1).max(axis=1)
    df['atr']     = tr.ewm(span=14, adjust=False).mean()
    df['atr_pct'] = df['atr'] / df['close']

    if not df1h.empty:
        c1h = df1h['close']
        # 1H EMA9/21
        df['ema9_1h']  = ema(c1h, 9).reindex(df.index, method='ffill')
        df['ema21_1h'] = ema(c1h, 21).reindex(df.index, method='ffill')
        # 1H RSI(14) — overbought/oversold filter
        df['rsi_1h'] = rsi(c1h, 14).reindex(df.index, method='ffill')
        # 4H EMA9/21 (resample 1H → 4H)
        h4c = c1h.resample('4h').last().dropna()
        df['ema9_4h']  = ema(h4c, 9).reindex(df.index, method='ffill')
        df['ema21_4h'] = ema(h4c, 21).reindex(df.index, method='ffill')
    else:
        for col in ['ema9_1h', 'ema21_1h', 'rsi_1h', 'ema9_4h', 'ema21_4h']:
            df[col] = np.nan

    return df

# ── SESSION VWAP (three sessions) ────────────────────────────────────────────
def add_session_vwap(df, valid_sessions):
    df   = df.copy()
    tp   = (df['high'] + df['low'] + df['close']) / 3.0
    hour = df.index.hour + df.index.minute / 60.0
    dates = df.index.normalize()

    df['vwap']          = np.nan
    df['vwap_std']      = np.nan
    df['vwap_bars']     = 0
    df['in_entry']      = False
    df['entry_sess']    = ''

    for sess_name, sess in SESSIONS.items():
        if sess_name not in valid_sessions:
            continue

        vh = sess['vwap_h']
        es = sess['entry_s']
        ee = sess['entry_e']

        # Bars belonging to this session window
        in_sess  = (hour >= vh) & (hour <= ee + 1.0) & (df.index.weekday < 5)

        # Compute per-day VWAP within session
        for date in pd.unique(dates[in_sess]):
            mask = (dates == date) & in_sess
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

        # Mark entry-eligible bars
        entry_mask = in_sess & (hour >= es) & (hour <= ee)
        df.loc[entry_mask, 'in_entry']   = True
        df.loc[entry_mask, 'entry_sess'] = sess_name

    return df

# ── BACKTEST ENGINE ───────────────────────────────────────────────────────────
def run_backtest(pair, df, macro_score, vix_series):
    df = df.copy()

    # Attach daily macro score to 15m bars
    ms = macro_score.copy()
    if ms.index.tz is None:
        ms.index = pd.to_datetime(ms.index).tz_localize('UTC')
    ms_d = ms.groupby(ms.index.normalize()).last()
    bar_dates = pd.DatetimeIndex(df.index.normalize())
    df['macro'] = ms_d.reindex(bar_dates, method='ffill').values

    atr_min, atr_max = PAIR_ATR.get(pair, (0.0003, 0.0080))

    equity       = float(ACCOUNT)
    day_start_eq = {}
    trades_out   = []
    challenge_ok = True

    i, N = 0, len(df)

    while i < N - 1:
        row      = df.iloc[i]
        date_key = row.name.date()

        if row.name.weekday() >= 5:
            i += 1; continue

        # FTMO floor check
        if equity <= FTMO_FLOOR:
            challenge_ok = False
            print(f"  !! Challenge FAILED: ${equity:,.0f} at {date_key}")
            break

        if date_key not in day_start_eq:
            day_start_eq[date_key] = equity

        # FTMO daily limit
        if equity - day_start_eq[date_key] <= -FTMO_DAILY_LIMIT:
            i += 1; continue

        # Must be in an entry window
        if not row.get('in_entry', False):
            i += 1; continue

        sess_name = row.get('entry_sess', '')

        # NFP avoidance — skip NY overlap on first Friday of month
        if sess_name == 'ny_overlap' and is_nfp_day(date_key):
            i += 1; continue

        # High-VIX central-bank Wednesday — skip NY if VIX > 25
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

        price    = row['close']
        atr      = row.get('atr', np.nan)
        atr_pct  = row.get('atr_pct', np.nan)
        ema9     = row.get('ema9', np.nan)
        ema21    = row.get('ema21', np.nan)
        ema9_1h  = row.get('ema9_1h', np.nan)
        ema21_1h = row.get('ema21_1h', np.nan)
        ema9_4h  = row.get('ema9_4h', np.nan)
        ema21_4h = row.get('ema21_4h', np.nan)
        rsi1h    = row.get('rsi_1h', np.nan)
        vwap     = row.get('vwap', np.nan)
        vstd     = row.get('vwap_std', np.nan)
        vbars    = row.get('vwap_bars', 0)

        if any(pd.isna(v) for v in [atr, atr_pct, ema9, ema21, vwap, vstd]):
            i += 1; continue

        # ── FILTERS ───────────────────────────────────────────────────────────

        # Volatility in pair-specific range
        if not (atr_min <= atr_pct <= atr_max):
            i += 1; continue

        # Require at least 4 VWAP bars (session has developed)
        if vbars < 4:
            i += 1; continue

        # 4H EMA alignment
        if not (pd.isna(ema9_4h) or pd.isna(ema21_4h)):
            if direction == 1  and ema9_4h <= ema21_4h: i += 1; continue
            if direction == -1 and ema9_4h >= ema21_4h: i += 1; continue

        # 1H EMA alignment
        if not (pd.isna(ema9_1h) or pd.isna(ema21_1h)):
            if direction == 1  and ema9_1h <= ema21_1h: i += 1; continue
            if direction == -1 and ema9_1h >= ema21_1h: i += 1; continue

        # 1H RSI — avoid chasing extremes
        if not pd.isna(rsi1h):
            if direction == 1  and rsi1h > 70: i += 1; continue   # overbought
            if direction == -1 and rsi1h < 30: i += 1; continue   # oversold

        # 15m EMA alignment
        if direction == 1  and ema9 <= ema21: i += 1; continue
        if direction == -1 and ema9 >= ema21: i += 1; continue

        # VWAP zone
        dist = (price - vwap) / vstd
        if direction == 1  and not (-1.0 <= dist <= 0.3): i += 1; continue
        if direction == -1 and not (-0.3 <= dist <= 1.0): i += 1; continue

        # ── ENTRY ─────────────────────────────────────────────────────────────
        entry_px = price
        entry_dt = row.name

        # Score-scaled position sizing
        abs_score = abs(macro)
        size_mult = 1.25 if abs_score >= 8.0 else 1.0
        risk_amt  = equity * RISK_PCT * size_mult

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
                b_hour > SESSIONS[s]['entry_e'] + 2.0
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

        # ── P&L (currency-agnostic R-multiples) ───────────────────────────────
        def r(px_a, px_b, dirn): return (px_b - px_a) / (SL_MULT * atr) * dirn

        if outcome == 'sl':
            r_mult = -1.0
        elif outcome == 'be':
            r_mult = (TP1_MULT / SL_MULT) * TP1_FRAC    # TP1 locked, BE on rest
        elif outcome == 'tp2':
            r_mult = ((TP1_MULT / SL_MULT) * TP1_FRAC +
                      (TP2_MULT / SL_MULT) * TP2_FRAC)
        elif outcome == 'time':
            r_rem = r(entry_px, exit_px, direction)
            if tp1_hit:
                r_mult = (TP1_MULT / SL_MULT) * TP1_FRAC + r_rem * TP2_FRAC
            else:
                r_mult = r_rem
        else:
            r_mult = r(entry_px, exit_px, direction)

        pnl    = r_mult * risk_amt
        equity += pnl

        trades_out.append(dict(
            entry_dt  = entry_dt,
            exit_dt   = exit_dt,
            direction = direction,
            outcome   = outcome,
            session   = sess_name,
            r_mult    = r_mult,
            pnl       = pnl,
            equity    = equity,
            macro     = macro,
            size_mult = size_mult,
        ))
        i = j + 1

    return pd.DataFrame(trades_out), equity, challenge_ok

# ── STATISTICS ────────────────────────────────────────────────────────────────
def compute_stats(tdf, start_eq, end_eq):
    if tdf.empty or len(tdf) < 5:
        return {}
    r    = tdf['r_mult'].values
    n    = len(r)
    wr   = (r > 0).mean()
    days = max((tdf['exit_dt'].iloc[-1] - tdf['entry_dt'].iloc[0]).days, 1)
    tpw  = n / (days / 7)
    ann  = tpw * 52
    sr   = (r.mean() / (r.std(ddof=1) + 1e-9)) * np.sqrt(ann)
    t, p2 = stats.ttest_1samp(r, 0)
    p1    = p2 / 2 if t > 0 else 1.0
    eq    = tdf['equity'].values
    pk    = np.maximum.accumulate(eq)
    mdd   = ((pk - eq) / pk).max()
    roi   = (end_eq - start_eq) / start_eq

    longs  = tdf[tdf['direction'] ==  1]
    shorts = tdf[tdf['direction'] == -1]

    tdf2 = tdf.copy()
    tdf2['month'] = tdf2['entry_dt'].dt.to_period('M')
    monthly = (tdf2.groupby('month')
               .agg(n=('pnl', 'count'), pnl=('pnl', 'sum'),
                    wr=('r_mult', lambda x: (x > 0).mean()))
               .reset_index())

    # Session breakdown
    sess_stat = (tdf2.groupby('session')
                 .agg(n=('pnl', 'count'), pnl=('pnl', 'sum'),
                      wr=('r_mult', lambda x: (x > 0).mean()))
                 .reset_index())

    return dict(n=n, wr=wr, roi=roi, sharpe=sr, p_val=p1, mdd=mdd,
                tpw=tpw, longs=longs, shorts=shorts,
                monthly=monthly, sess_stat=sess_stat)

# ── REPORTING ─────────────────────────────────────────────────────────────────
def print_report(pair, st, start_eq, end_eq, challenge_ok, start_date, end_date):
    sep = '=' * 66
    print(f"\n{sep}")
    print(f"  {pair}  ({start_date} → {end_date})")
    print(f"{sep}")
    roi_pct = st['roi'] * 100
    sig     = '✓' if st['p_val'] < 0.05 else '✗'
    print(f"  ${start_eq:,.0f}  →  ${end_eq:,.0f}  ({roi_pct:+.1f}% ROI)")
    print(f"  Sharpe  : {st['sharpe']:.2f}    Max DD: {st['mdd']*100:.1f}%")
    print(f"  Trades  : {st['n']}  ({st['tpw']:.1f}/week)   WR: {st['wr']*100:.1f}%")
    print(f"  p-value : {st['p_val']:.4f}  — "
          f"{'significant' if st['p_val'] < 0.05 else 'not significant'} {sig}")
    if not challenge_ok:
        print(f"  ⚠️  Would have FAILED challenge (hit $108k floor)")

    lg, sh = st['longs'], st['shorts']
    print(f"  Longs  {len(lg):>3}tr  WR {(lg['r_mult']>0).mean()*100:.0f}%  "
          f"${lg['pnl'].sum():>+10,.0f}")
    print(f"  Shorts {len(sh):>3}tr  WR {(sh['r_mult']>0).mean()*100:.0f}%  "
          f"${sh['pnl'].sum():>+10,.0f}")

    # Session breakdown
    print(f"\n  By session:")
    for _, sr in st['sess_stat'].iterrows():
        print(f"    {sr['session']:<14} {sr['n']:>3}tr  WR {sr['wr']*100:.0f}%  "
              f"${sr['pnl']:>+9,.0f}")

    # Monthly
    print(f"\n  Monthly:")
    eq_run, pos = start_eq, 0
    for _, mr in st['monthly'].iterrows():
        eq_run += mr['pnl']
        tick    = '✓' if mr['pnl'] > 0 else '✗'
        if mr['pnl'] > 0:
            pos += 1
        print(f"    {mr['month']}  {mr['n']:>3}tr  WR {mr['wr']*100:.0f}%  "
              f"${mr['pnl']:>+10,.0f}  ${eq_run:>10,.0f} {tick}")
    print(f"  Positive months: {pos}/{len(st['monthly'])}")

    ftmo_p = roi_pct >= 10.0
    ftmo_d = st['mdd'] <= 0.10
    ftmo_c = challenge_ok
    print(f"\n  FTMO  Profit {'✓' if ftmo_p else '✗'} ({roi_pct:+.1f}%)   "
          f"DD {'✓' if ftmo_d else '✗'} ({st['mdd']*100:.1f}%)   "
          f"Floor {'✓' if ftmo_c else '✗'}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    # ── Load price data (cached 15m, fresh 1h) ─────────────────────────────
    print("Loading 15m data (from cache)...")
    data_15m = {p: load_15m(p) for p in PAIRS}

    print("Loading 1h data (yfinance)...")
    data_1h  = {p: load_1h(p)  for p in PAIRS}

    # ── Load macro + cross-asset ────────────────────────────────────────────
    macro_all = load_macro()

    # VIX daily as dict for fast lookup in backtest
    vix_d = macro_all.get('vix', pd.Series(dtype=float))
    if vix_d.index.tz is not None:
        vix_d.index = vix_d.index.tz_localize(None)
    vix_lookup = vix_d.to_dict()

    # ── Per-pair backtests ──────────────────────────────────────────────────
    summary = []

    for pair in PAIRS:
        df15 = data_15m[pair]
        df1h = data_1h[pair]

        if df15.empty:
            print(f"\n[{pair}] No 15m data — skipping")
            continue

        print(f"\nBuilding indicators for {pair}...")
        macro_score = build_macro(pair, macro_all)
        df15 = add_indicators(df15, df1h)
        df15 = add_session_vwap(df15, PAIR_SESSIONS[pair])

        print(f"Running backtest {pair}...")
        tdf, final_eq, ch_ok = run_backtest(pair, df15, macro_score, vix_lookup)

        if tdf.empty:
            print(f"  [{pair}] No trades generated")
            continue

        st         = compute_stats(tdf, ACCOUNT, final_eq)
        start_date = tdf['entry_dt'].iloc[0].date()
        end_date   = tdf['exit_dt'].iloc[-1].date()

        print_report(pair, st, ACCOUNT, final_eq, ch_ok, start_date, end_date)

        summary.append(dict(
            pair    = pair,
            n       = st['n'],
            tpw     = st['tpw'],
            roi     = st['roi'],
            wr      = st['wr'],
            sharpe  = st['sharpe'],
            p_val   = st['p_val'],
            mdd     = st['mdd'],
            ok      = ch_ok,
        ))

    # ── Final comparison table ──────────────────────────────────────────────
    if not summary:
        print("\nNo results."); return

    print(f"\n{'='*88}")
    print(f"  FINAL COMPARISON V2 — Pair-Specific Macro + Cross-Asset + RSI + NFP Filter")
    print(f"{'='*88}")
    print(f"  {'Pair':<10} {'N':>4} {'TPW':>5} {'ROI':>9} {'WR':>7} "
          f"{'Sharpe':>8} {'p-val':>8} {'MDD':>7}  FTMO")
    print(f"  {'─'*84}")
    for r in sorted(summary, key=lambda x: x['sharpe'], reverse=True):
        sig     = '✓' if r['p_val'] < 0.05 else '✗'
        pass_v  = (r['roi'] >= 0.10) and (r['mdd'] <= 0.10) and r['ok']
        ftmo_s  = '✅ PASS' if pass_v else ('❌ FAIL' if not r['ok'] else '⚠️ MDD')
        print(f"  {r['pair']:<10} {r['n']:>4} {r['tpw']:>5.1f} {r['roi']*100:>8.1f}% "
              f"{r['wr']*100:>6.1f}% {r['sharpe']:>8.2f} "
              f"{r['p_val']:>6.4f} {sig}  {r['mdd']*100:>5.1f}%  {ftmo_s}")
    print(f"{'='*88}")
    print(f"\n  SIZING: 1.00× risk at |score|≥6  ·  1.25× risk at |score|≥8")
    print(f"  NEWS  : NFP-day NY skipped  ·  VIX>25 Wednesday NY skipped")
    print(f"  FTMO  : daily -$6k limit  ·  absolute floor $108k  ·  1% base risk")

if __name__ == '__main__':
    main()
