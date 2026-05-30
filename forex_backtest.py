#!/usr/bin/env python3
"""
Forex RORO Backtest — FTMO $120k Challenge
==========================================
Pairs : AUDCHF, AUDJPY, AUDNZD, AUDUSD, CADCHF, CADJPY, CHFJPY
System: Risk-on/Risk-off macro regime + 4H/1H/15m EMA alignment + VWAP entry
Risk  : 1% per trade, FTMO daily -5% circuit breaker, max -10% total DD

Macro framework:
  RORO pairs (all except AUDNZD):
    s1 = pair vs daily EMA50
    s2 = SPX vs daily EMA50   (risk-on = buy AUD/CAD vs JPY/CHF)
    s3 = pair 10d momentum
    s4 = pair vs daily EMA200
    s5 = VIX vs 20d MA        (low VIX = risk-on = +1)
  AUDNZD:
    s1 = AUDNZD vs EMA50
    s2 = AUDUSD vs EMA50
    s3 = NZDUSD vs EMA50 (inverted — NZD weak = AUDNZD up)
    s4 = AUDNZD 10d momentum
    s5 = AUDNZD vs EMA200

  Score = (raw/5 * 10), Long if ≥ +6, Short if ≤ -6

Sessions: London open 07:00–09:30 UTC  |  NY overlap 13:30–16:30 UTC
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
TWELVE_KEY   = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT      = 120_000
RISK_PCT     = 0.01       # 1% per trade
SL_MULT      = 0.75
TP1_MULT     = 1.50
TP2_MULT     = 2.50
TP1_FRAC     = 0.60
TP2_FRAC     = 0.40
SCORE_LONG   =  6.0
SCORE_SHORT  = -6.0
ATR_PCT_MIN  = 0.0002     # 0.02% floor
ATR_PCT_MAX  = 0.0080     # 0.80% ceiling

FTMO_DAILY_LIMIT  = ACCOUNT * 0.05   # $6,000  (absolute, based on initial balance)
FTMO_FLOOR        = ACCOUNT * 0.90   # $108,000 (fail if equity drops below this)

PAIRS = ['AUD/JPY', 'AUD/CHF', 'AUD/NZD', 'AUD/USD', 'CAD/CHF', 'CAD/JPY', 'CHF/JPY']

# Sessions (UTC decimal hours): vwap_h=VWAP reset, entry_s/e=valid entry window
SESSIONS = {
    'london_open': {'vwap_h': 7.0,  'entry_s': 7.5,  'entry_e': 9.0},
    'ny_overlap':  {'vwap_h': 13.5, 'entry_s': 14.0, 'entry_e': 16.5},
}

# ── EMA HELPER ────────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

# ── DATA: TWELVE DATA ─────────────────────────────────────────────────────────
def fetch_td(symbol, interval, max_pages=12):
    """Fetch historical bars from Twelve Data, with disk cache."""
    slug  = symbol.replace('/', '') + '__' + interval
    fpath = os.path.join(CACHE_DIR, slug + '.csv')

    if os.path.exists(fpath):
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        print(f"  [{symbol} {interval}] cache: {len(df)} bars "
              f"{df.index[0].date()} → {df.index[-1].date()}")
        return df

    print(f"--- Fetching {symbol} {interval} ---")
    all_rows, end_dt = [], None

    for page in range(1, max_pages + 1):
        params = dict(symbol=symbol, interval=interval,
                      outputsize=5000, apikey=TWELVE_KEY, timezone='UTC')
        if end_dt:
            params['end_date'] = end_dt
        try:
            r = requests.get('https://api.twelvedata.com/time_series',
                             params=params, timeout=30)
            j = r.json()
        except Exception as e:
            print(f"  Fetch error: {e}")
            break

        if j.get('status') == 'error':
            print(f"  API error: {j.get('message', '?')}")
            break

        vals = j.get('values', [])
        if not vals:
            break

        all_rows.extend(vals)
        oldest, newest = vals[-1]['datetime'], vals[0]['datetime']
        print(f"  [{symbol}] page {page}: {oldest} → {newest}  ({len(vals)} bars)")

        dt_oldest = pd.Timestamp(oldest, tz='UTC')
        # step back one interval
        step = {'15min': 15, '1h': 60, '4h': 240}.get(interval, 15)
        end_dt = (dt_oldest - pd.Timedelta(minutes=step)).strftime('%Y-%m-%d %H:%M:%S')
        time.sleep(12)   # 5 req/min — safely under 8/min Twelve Data limit

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df['datetime'] = pd.to_datetime(df['datetime'], utc=True)
    df = (df.set_index('datetime')
            .sort_index()
            [['open', 'high', 'low', 'close']]
            .astype(float))
    df = df[~df.index.duplicated()]
    df.to_csv(fpath)
    print(f"  [{symbol}] saved: {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}")
    return df

# ── DATA: 1H (yfinance, free, up to 730 days) ────────────────────────────────
def fetch_yf_1h(pair):
    sym   = pair.replace('/', '') + '=X'
    fpath = os.path.join(CACHE_DIR, sym + '__1h_yf.csv')
    if os.path.exists(fpath):
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        print(f"  [{pair} 1h] cache: {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}")
        return df
    try:
        raw = yf.download(sym, period='2y', interval='1h', progress=False, auto_adjust=True)
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
        print(f"  [{pair} 1h] {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}")
        return df
    except Exception as e:
        print(f"  Warning: yfinance 1h failed for {pair}: {e}")
        return pd.DataFrame()

# ── DATA: MACRO (yfinance daily) ──────────────────────────────────────────────
def load_macro():
    print("Loading macro data (yfinance)...")
    macro = {}
    yf_symbols = {
        'spx':  '^GSPC',
        'vix':  '^VIX',
        'dxy':  'DX-Y.NYB',
        'tnx':  '^TNX',
        'aud':  'AUDUSD=X',
        'nzd':  'NZDUSD=X',
    }
    for k, sym in yf_symbols.items():
        try:
            d = yf.download(sym, period='5y', interval='1d',
                            progress=False, auto_adjust=True)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.droplevel(1)
            macro[k] = d['Close'].dropna()
        except Exception as e:
            print(f"  Warning: {sym} failed: {e}")
            macro[k] = pd.Series(dtype=float)

    # Daily pair closes
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
            print(f"  Warning: daily {pair} not loaded")
    return macro

# ── MACRO SCORE ───────────────────────────────────────────────────────────────
def build_macro_score(pair, macro):
    """
    Returns a daily Series of macro scores, shifted 1 day
    (yesterday's close drives today's signal).
    """
    p   = macro.get(pair, pd.Series(dtype=float))
    spx = macro.get('spx', pd.Series(dtype=float))
    vix = macro.get('vix', pd.Series(dtype=float))
    aud = macro.get('aud', pd.Series(dtype=float))
    nzd = macro.get('nzd', pd.Series(dtype=float))

    if len(p) < 60:
        return pd.Series(dtype=float)

    idx = p.index

    # Component 1: pair vs daily EMA50
    s1 = pd.Series(np.where(p > ema(p, 50), 1.0, -1.0), index=idx)

    # Component 3: pair 10-day momentum
    s3 = pd.Series(np.where(p.pct_change(10) > 0, 1.0, -1.0), index=idx)

    # Component 4: pair vs daily EMA200
    s4 = pd.Series(np.where(p > ema(p, 200), 1.0, -1.0), index=idx)

    if pair == 'AUD/NZD':
        # Spread: AUD strong AND NZD weak
        aud_r = aud.reindex(idx, method='ffill')
        nzd_r = nzd.reindex(idx, method='ffill')
        s2 = pd.Series(np.where(aud_r > ema(aud_r, 50), 1.0, -1.0), index=idx)
        s5 = pd.Series(np.where(nzd_r < ema(nzd_r, 50), 1.0, -1.0), index=idx)
    else:
        # RORO pairs: SPX regime + VIX regime
        spx_r = spx.reindex(idx, method='ffill')
        s2    = pd.Series(np.where(spx_r > ema(spx_r, 50), 1.0, -1.0), index=idx)
        vix_r = vix.reindex(idx, method='ffill')
        s5    = pd.Series(np.where(vix_r < ema(vix_r, 20), 1.0, -1.0), index=idx)

    raw   = s1 + s2 + s3 + s4 + s5
    score = (raw / 5 * 10).round(1)
    return score.shift(1)   # use prior day's close

# ── INDICATORS ────────────────────────────────────────────────────────────────
def add_indicators(df15, df1h):
    df = df15.copy()

    # 15m EMA9/21 and ATR
    df['ema9']  = ema(df['close'], 9)
    df['ema21'] = ema(df['close'], 21)
    prev = df['close'].shift(1)
    tr   = pd.concat([df['high'] - df['low'],
                      (df['high'] - prev).abs(),
                      (df['low']  - prev).abs()], axis=1).max(axis=1)
    df['atr']     = tr.ewm(span=14, adjust=False).mean()
    df['atr_pct'] = df['atr'] / df['close']

    # 1H EMA9/21 (directly from 1h bars)
    if not df1h.empty:
        h1e9  = ema(df1h['close'], 9)
        h1e21 = ema(df1h['close'], 21)
        df['ema9_1h']  = h1e9.reindex(df.index, method='ffill')
        df['ema21_1h'] = h1e21.reindex(df.index, method='ffill')
    else:
        df['ema9_1h'] = df['ema21_1h'] = np.nan

    # 4H EMA9/21 (resample 1h → 4h)
    if not df1h.empty:
        h4 = df1h['close'].resample('4h').last().dropna()
        h4e9  = ema(h4, 9)
        h4e21 = ema(h4, 21)
        df['ema9_4h']  = h4e9.reindex(df.index, method='ffill')
        df['ema21_4h'] = h4e21.reindex(df.index, method='ffill')
    else:
        df['ema9_4h'] = df['ema21_4h'] = np.nan

    return df

# ── SESSION VWAP ──────────────────────────────────────────────────────────────
def add_session_vwap(df):
    df      = df.copy()
    tp      = (df['high'] + df['low'] + df['close']) / 3.0
    hour    = df.index.hour + df.index.minute / 60.0
    dates   = df.index.normalize()

    df['vwap']      = np.nan
    df['vwap_std']  = np.nan
    df['in_entry']  = False
    df['curr_sess_vwap_h'] = np.nan

    for sess_name, sess in SESSIONS.items():
        vh = sess['vwap_h']
        es = sess['entry_s']
        ee = sess['entry_e']

        # Bars that belong to this session (from VWAP reset up to entry end + buffer)
        in_sess  = (hour >= vh) & (hour <= ee + 1.5) & (df.index.weekday < 5)
        in_entry = (hour >= es) & (hour <= ee) & (df.index.weekday < 5)

        for date in pd.unique(dates[in_sess]):
            day_mask = (dates == date) & in_sess
            if day_mask.sum() < 2:
                continue

            tp_day   = tp.values[day_mask]
            cum_tp   = np.cumsum(tp_day)
            cum_n    = np.arange(1, len(tp_day) + 1, dtype=float)
            vwap_day = cum_tp / cum_n

            # Expanding std
            std_day = np.zeros(len(tp_day))
            for k in range(1, len(tp_day)):
                std_day[k] = np.std(tp_day[:k + 1], ddof=0)
            std_day[std_day < 1e-8] = tp_day.mean() * 0.0005

            idx_pos = np.where(day_mask)[0]
            df.iloc[idx_pos, df.columns.get_loc('vwap')]     = vwap_day
            df.iloc[idx_pos, df.columns.get_loc('vwap_std')] = np.maximum(std_day, 1e-8)

        # Mark entry bars
        entry_mask = in_sess & in_entry
        df.loc[entry_mask, 'in_entry']           = True
        df.loc[entry_mask, 'curr_sess_vwap_h']   = vh

    return df

# ── BACKTEST ENGINE ───────────────────────────────────────────────────────────
def run_backtest(pair, df, macro_score):
    """
    Simulates trading. P&L computed as R-multiples × risk_amount.
    R-multiple formula is currency-agnostic:
      r = price_change / (SL_MULT × ATR)  [dimensionless ratio]
      pnl = r × risk_amount_in_USD
    """
    df = df.copy()

    # Attach macro score (daily) onto 15m index
    ms_idx = macro_score.copy()
    if ms_idx.index.tz is None:
        ms_idx.index = pd.to_datetime(ms_idx.index).tz_localize('UTC')
    ms_daily = ms_idx.groupby(ms_idx.index.normalize()).last()
    bar_dates = pd.DatetimeIndex(df.index.normalize())
    df['macro'] = ms_daily.reindex(bar_dates, method='ffill').values

    equity          = float(ACCOUNT)
    day_pnl         = {}      # date → cumulative pnl that day
    day_start_eq    = {}      # date → equity at start of day
    trades_out      = []
    challenge_failed = False

    i, N = 0, len(df)

    while i < N - 1:
        row = df.iloc[i]

        # Weekend skip
        if row.name.weekday() >= 5:
            i += 1
            continue

        date_key = row.name.date()

        # FTMO: equity must stay above 90% of INITIAL balance at all times
        if equity <= FTMO_FLOOR:
            challenge_failed = True
            print(f"  !! Challenge FAILED: equity ${equity:,.0f} < floor ${FTMO_FLOOR:,.0f} "
                  f"at {row.name.date()}")
            break

        # Track start-of-day equity (for daily limit calculation)
        if date_key not in day_start_eq:
            day_start_eq[date_key] = equity

        # FTMO daily limit: stop trading today if day's loss ≥ $6k from start of day
        today_loss = equity - day_start_eq[date_key]
        if today_loss <= -FTMO_DAILY_LIMIT:
            i += 1
            continue

        # Gate: must be in entry window
        if not row.get('in_entry', False):
            i += 1
            continue

        macro = row.get('macro', np.nan)
        if pd.isna(macro):
            i += 1
            continue

        if macro >= SCORE_LONG:
            direction = 1
        elif macro <= SCORE_SHORT:
            direction = -1
        else:
            i += 1
            continue

        price    = row['close']
        atr      = row.get('atr', np.nan)
        atr_pct  = row.get('atr_pct', np.nan)
        ema9     = row.get('ema9', np.nan)
        ema21    = row.get('ema21', np.nan)
        ema9_1h  = row.get('ema9_1h', np.nan)
        ema21_1h = row.get('ema21_1h', np.nan)
        ema9_4h  = row.get('ema9_4h', np.nan)
        ema21_4h = row.get('ema21_4h', np.nan)
        vwap     = row.get('vwap', np.nan)
        vstd     = row.get('vwap_std', np.nan)

        if any(pd.isna(v) for v in [atr, atr_pct, ema9, ema21, vwap, vstd]):
            i += 1
            continue

        if atr <= 0 or vstd <= 0:
            i += 1
            continue

        # ── FILTERS ───────────────────────────────────────────────────────────
        # Volatility filter
        if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
            i += 1
            continue

        # 4H EMA alignment
        if not (pd.isna(ema9_4h) or pd.isna(ema21_4h)):
            if direction == 1 and ema9_4h <= ema21_4h:
                i += 1; continue
            if direction == -1 and ema9_4h >= ema21_4h:
                i += 1; continue

        # 1H EMA alignment
        if not (pd.isna(ema9_1h) or pd.isna(ema21_1h)):
            if direction == 1 and ema9_1h <= ema21_1h:
                i += 1; continue
            if direction == -1 and ema9_1h >= ema21_1h:
                i += 1; continue

        # 15m EMA alignment
        if direction == 1 and ema9 <= ema21:
            i += 1; continue
        if direction == -1 and ema9 >= ema21:
            i += 1; continue

        # VWAP zone
        dist = (price - vwap) / vstd
        if direction == 1  and not (-1.0 <= dist <= 0.3):
            i += 1; continue
        if direction == -1 and not (-0.3 <= dist <= 1.0):
            i += 1; continue

        # ── ENTRY ─────────────────────────────────────────────────────────────
        entry_px = price
        entry_dt = row.name
        risk_amt = equity * RISK_PCT

        if direction == 1:
            sl_px  = entry_px - SL_MULT  * atr
            tp1_px = entry_px + TP1_MULT * atr
            tp2_px = entry_px + TP2_MULT * atr
        else:
            sl_px  = entry_px + SL_MULT  * atr
            tp1_px = entry_px - TP1_MULT * atr
            tp2_px = entry_px - TP2_MULT * atr

        # ── TRADE SIMULATION ──────────────────────────────────────────────────
        tp1_hit  = False
        r_pnl    = 0.0
        exit_px  = None
        exit_dt  = None
        outcome  = 'none'
        TIME_BARS = 6

        j = i + 1
        bars_in  = 0

        while j < N:
            b    = df.iloc[j]
            bhi  = b['high']
            blo  = b['low']
            bh   = b.name.hour + b.name.minute / 60.0
            bars_in += 1

            # Time stop: 6 bars max, or exit when past any session entry window
            past_any_sess = all(bh > s['entry_e'] + 2.0 for s in SESSIONS.values())
            if bars_in > TIME_BARS or past_any_sess or b.name.weekday() >= 5:
                exit_px = b['close']
                exit_dt = b.name
                outcome = 'time'
                break

            if not tp1_hit:
                if direction == 1:
                    if bhi >= tp1_px:
                        tp1_hit = True
                        sl_px   = entry_px   # move to BE
                    elif blo <= sl_px:
                        exit_px = sl_px
                        exit_dt = b.name
                        outcome = 'sl'
                        break
                else:
                    if blo <= tp1_px:
                        tp1_hit = True
                        sl_px   = entry_px
                    elif bhi >= sl_px:
                        exit_px = sl_px
                        exit_dt = b.name
                        outcome = 'sl'
                        break
            else:
                if direction == 1:
                    if bhi >= tp2_px:
                        exit_px = tp2_px
                        exit_dt = b.name
                        outcome = 'tp2'
                        break
                    elif blo <= sl_px:   # sl_px == entry_px (BE)
                        exit_px = sl_px
                        exit_dt = b.name
                        outcome = 'be'
                        break
                else:
                    if blo <= tp2_px:
                        exit_px = tp2_px
                        exit_dt = b.name
                        outcome = 'tp2'
                        break
                    elif bhi >= sl_px:
                        exit_px = sl_px
                        exit_dt = b.name
                        outcome = 'be'
                        break
            j += 1

        if exit_px is None:
            exit_px = df.iloc[min(j, N-1)]['close']
            exit_dt = df.iloc[min(j, N-1)].name
            outcome = 'time'

        # ── P&L (currency-agnostic R-multiples) ───────────────────────────────
        # pnl = r_multiple × risk_amount
        # r = price_change / (SL_MULT × ATR) — dimensionless
        def r_of(px_from, px_to, dirn):
            return (px_to - px_from) / (SL_MULT * atr) * dirn

        if outcome == 'sl':
            r_pnl = -1.0
        elif outcome == 'be':
            # TP1 was hit (TP1_FRAC locked), remainder closed at BE (entry)
            r_pnl = (TP1_MULT / SL_MULT) * TP1_FRAC
        elif outcome == 'tp2':
            r_pnl = ((TP1_MULT / SL_MULT) * TP1_FRAC +
                     (TP2_MULT / SL_MULT) * TP2_FRAC)
        elif outcome == 'time':
            if tp1_hit:
                r_rem = r_of(entry_px, exit_px, direction)
                r_pnl = (TP1_MULT / SL_MULT) * TP1_FRAC + r_rem * TP2_FRAC
            else:
                r_pnl = r_of(entry_px, exit_px, direction)
        else:
            r_pnl = r_of(entry_px, exit_px, direction)

        pnl    = r_pnl * risk_amt
        equity += pnl

        trades_out.append(dict(
            entry_dt  = entry_dt,
            exit_dt   = exit_dt,
            direction = direction,
            outcome   = outcome,
            r_mult    = r_pnl,
            pnl       = pnl,
            equity    = equity,
            macro     = macro,
            atr_pct   = atr_pct,
        ))

        i = j + 1  # skip past the trade's bars

    return pd.DataFrame(trades_out), equity, challenge_failed

# ── STATISTICS ────────────────────────────────────────────────────────────────
def compute_stats(tdf, start_equity, end_equity):
    if tdf.empty or len(tdf) < 5:
        return {}

    r    = tdf['r_mult'].values
    n    = len(r)
    wr   = (r > 0).mean()
    days = max((tdf['exit_dt'].iloc[-1] - tdf['entry_dt'].iloc[0]).days, 1)
    tpw  = n / (days / 7)

    # Annualised Sharpe
    ann  = tpw * 52
    sr   = (r.mean() / (r.std(ddof=1) + 1e-9)) * np.sqrt(ann)

    # p-value (one-tailed t-test: H₀ mean ≤ 0)
    t, p2 = stats.ttest_1samp(r, 0)
    p1    = p2 / 2 if t > 0 else 1.0

    # Max drawdown from equity curve
    eq   = tdf['equity'].values
    pk   = np.maximum.accumulate(eq)
    mdd  = ((pk - eq) / pk).max()

    roi = (end_equity - start_equity) / start_equity

    longs  = tdf[tdf['direction'] ==  1]
    shorts = tdf[tdf['direction'] == -1]

    # Monthly breakdown
    tdf2 = tdf.copy()
    tdf2['month'] = tdf2['entry_dt'].dt.to_period('M')
    monthly = (tdf2.groupby('month')
               .agg(n_trades=('pnl', 'count'),
                    pnl=('pnl', 'sum'),
                    wr=('r_mult', lambda x: (x > 0).mean()))
               .reset_index())

    return dict(n=n, wr=wr, roi=roi, sharpe=sr, p_val=p1, mdd=mdd, tpw=tpw,
                longs=longs, shorts=shorts, monthly=monthly, days=days)

# ── REPORTING ─────────────────────────────────────────────────────────────────
def print_pair_report(pair, st, start_eq, end_eq, challenge_failed, start_date, end_date):
    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  {pair}  ({start_date} → {end_date})")
    print(f"{sep}")
    roi_pct = st['roi'] * 100
    print(f"  ${start_eq:,.0f}  →  ${end_eq:,.0f}  ({roi_pct:+.1f}% ROI)")
    sig = '✓' if st['p_val'] < 0.05 else '✗'
    print(f"  Sharpe   : {st['sharpe']:.2f}    Max DD: {-st['mdd']*100:.1f}%")
    print(f"  Trades   : {st['n']}  ({st['tpw']:.1f}/week)   WR: {st['wr']*100:.1f}%")
    print(f"  p-value  : {st['p_val']:.4f}  — "
          f"{'significant' if st['p_val'] < 0.05 else 'not significant'} {sig}")
    if challenge_failed:
        print(f"  ⚠️  Would have FAILED challenge (hit max DD)")

    lg = st['longs']
    sh = st['shorts']
    lg_pnl = lg['pnl'].sum() if len(lg) else 0
    sh_pnl = sh['pnl'].sum() if len(sh) else 0
    lg_wr  = (lg['r_mult'] > 0).mean() * 100 if len(lg) else 0
    sh_wr  = (sh['r_mult'] > 0).mean() * 100 if len(sh) else 0
    print(f"  Longs  {len(lg):>3}tr  WR {lg_wr:.0f}%  ${lg_pnl:+,.0f}")
    print(f"  Shorts {len(sh):>3}tr  WR {sh_wr:.0f}%  ${sh_pnl:+,.0f}")

    print(f"\n  Monthly:")
    eq_running = start_eq
    pos_months = 0
    for _, mrow in st['monthly'].iterrows():
        eq_running += mrow['pnl']
        tick = '✓' if mrow['pnl'] > 0 else '✗'
        if mrow['pnl'] > 0:
            pos_months += 1
        print(f"    {mrow['month']}  {mrow['n_trades']:>3}tr  "
              f"WR {mrow['wr']*100:.0f}%  "
              f"${mrow['pnl']:>+10,.0f}  "
              f"${eq_running:>10,.0f} {tick}")
    total = len(st['monthly'])
    print(f"  Positive months: {pos_months}/{total}")

    # FTMO readiness
    ftmo_profit = roi_pct >= 10.0
    ftmo_dd     = st['mdd'] <= 0.10
    print(f"\n  FTMO check  Profit {'✓' if ftmo_profit else '✗'} ({roi_pct:+.1f}%, need +10%)"
          f"   Max DD {'✓' if ftmo_dd else '✗'} ({st['mdd']*100:.1f}%, limit 10%)")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    # ── Fetch all 15m (Twelve Data) and 1h (yfinance) ────────────────────────
    print("Fetching 15m data from Twelve Data (cached pairs skip)...")
    data_15m = {}
    for pair in PAIRS:
        data_15m[pair] = fetch_td(pair, '15min', max_pages=12)
        # Small gap between pairs to avoid burst rate-limiting
        time.sleep(5)

    print("\nFetching 1h data from yfinance (free, no rate limit)...")
    data_1h = {}
    for pair in PAIRS:
        data_1h[pair] = fetch_yf_1h(pair)

    # ── Load macro ────────────────────────────────────────────────────────────
    macro_all = load_macro()

    # ── Run per-pair backtests ────────────────────────────────────────────────
    summary = []

    for pair in PAIRS:
        df15 = data_15m[pair]
        df1h = data_1h[pair]

        if df15.empty:
            print(f"\n  [{pair}] No 15m data — skipping")
            continue

        macro_score = build_macro_score(pair, macro_all)
        df15 = add_indicators(df15, df1h)
        df15 = add_session_vwap(df15)

        tdf, final_eq, failed = run_backtest(pair, df15, macro_score)

        if tdf.empty:
            print(f"\n  [{pair}] No trades generated")
            continue

        st         = compute_stats(tdf, ACCOUNT, final_eq)
        start_date = tdf['entry_dt'].iloc[0].date()
        end_date   = tdf['exit_dt'].iloc[-1].date()

        print_pair_report(pair, st, ACCOUNT, final_eq, failed, start_date, end_date)

        summary.append(dict(
            pair    = pair,
            n       = st['n'],
            tpw     = st['tpw'],
            roi     = st['roi'],
            wr      = st['wr'],
            sharpe  = st['sharpe'],
            p_val   = st['p_val'],
            mdd     = st['mdd'],
            failed  = failed,
        ))

    # ── Final comparison table ────────────────────────────────────────────────
    if not summary:
        print("\nNo results.")
        return

    print(f"\n{'='*82}")
    print(f"  FINAL COMPARISON — All Pairs (RORO System, London Open + NY Overlap)")
    print(f"{'='*82}")
    print(f"  {'Pair':<10} {'N':>4} {'TPW':>5} {'ROI':>9} {'WR':>7} "
          f"{'Sharpe':>8} {'p-val':>8} {'MDD':>7} {'FTMO'}")
    print(f"  {'─'*78}")

    for r in sorted(summary, key=lambda x: x['sharpe'], reverse=True):
        sig       = '✓' if r['p_val'] < 0.05 else '✗'
        ftmo_ok   = (r['roi'] >= 0.10) and (r['mdd'] <= 0.10) and not r['failed']
        ftmo_str  = '✅ PASS' if ftmo_ok else ('❌' if r['failed'] else '⚠️')
        print(f"  {r['pair']:<10} {r['n']:>4} {r['tpw']:>5.1f} {r['roi']*100:>8.1f}% "
              f"{r['wr']*100:>6.1f}% {r['sharpe']:>8.2f} {r['p_val']:>6.4f} {sig}  "
              f"{r['mdd']*100:>5.1f}%  {ftmo_str}")

    print(f"{'='*82}")
    print(f"\n  Risk: 1% per trade  ·  SL 0.75×ATR  ·  TP1 1.5×ATR (60%)  ·  TP2 2.5×ATR (40%)")
    print(f"  FTMO constraints simulated: daily -$6k limit, max -$12k total DD")

if __name__ == '__main__':
    main()
