"""
GOLD 15M SYSTEM V3 — COMPREHENSIVE IMPROVEMENT
================================================
Data-driven improvements based on 5-year cross-asset correlation analysis:

WHAT THE DATA SHOWED:
  - DXY 1-day lag correlation with gold: +0.0023  ← nearly zero!
  - TNX 1-day lag correlation with gold: +0.0373  ← weak
  - GDX is the single best predictor (0.09 corr) — already in score
  - VIX 30+: gold Sharpe = -0.60  ← we were trading through crashes
  - VIX 20-25: gold Sharpe = +1.81 ← sweet spot for gold
  - June: daily Sharpe = -1.67    ← worst month, skip it
  - May: daily Sharpe = -0.47     ← weak month, test skip
  - Session 14:00 UTC: peak range $23.49/bar  ← current window confirmed
  - London 07:00-10:00: range $13-15/bar     ← lower quality

IMPROVEMENTS TESTED:
  A: Extend data 3 years (main fix — 79 → ~200+ trades → p-value)
  B: VIX regime filter (skip days where VIX ≥ 30)
  C: Month filter (skip June, Sharpe -1.67)
  D: NFP day skip (first Friday of month — high vol, unpredictable direction)
  E: May filter (skip May, Sharpe -0.47)
  F: Score component swap — replace DXY/TNX with TIP/QQQ/JPY (test only)
  G: London session addition (07:00-10:00 UTC) — more trades, lower quality
  H: Long-only mode (gold in multi-year bull market)
  --- research-backed additions ---
  I: ADX 20-35 filter (not dead range <20, not runaway trend >35)
  J: Daily ATR percentile 40-80th (avoid ultra-quiet or chaotic days)
  K: London ORB direction alignment (only trade with 07:00-09:00 ORB direction)
  L: Skip Monday entries (worst day-of-week for gold historically)

Baseline: V2 best config — score≥4, window 14:00-16:30 UTC
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import requests, time, urllib.parse
from datetime import datetime, timedelta
from calendar import monthrange
import warnings
warnings.filterwarnings('ignore')

API_KEY    = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT    = 10_000.0
RISK_PCT   = 0.03
SL_MULT    = 0.75
TP1_MULT   = 1.5
TP2_MULT   = 2.5
TP1_FRAC   = 0.60
TP2_FRAC   = 0.40
TIME_STOP  = 6
MAX_TRADES = 2
MIN_BARS_BETWEEN = 4
SESS_OPEN  = (13, 30)   # NY session
SESS_CLOSE = (17,  0)
LON_OPEN   = (7,  0)    # London session
LON_CLOSE  = (10, 0)

print("="*72)
print("  GOLD 15M SYSTEM V3 — DATA-DRIVEN IMPROVEMENTS")
print("="*72)

# ── DATA FETCH (3 years) ──────────────────────────────────────────────────────
print("\nFetching 3 years of 15m data from Twelve Data (~22 pages, ~5 min)...")

def fetch_page(end_date):
    end_enc = urllib.parse.quote(end_date)
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol=XAU/USD&interval=15min&outputsize=5000"
           f"&apikey={API_KEY}&end_date={end_enc}&timezone=UTC")
    r = requests.get(url, timeout=30)
    j = r.json()
    if 'values' not in j:
        print(f"    API: {j.get('message','error')}")
        return None
    df = pd.DataFrame(j['values'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    df.sort_index(inplace=True)
    for c in ['open','high','low','close','volume']:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
    df.dropna(subset=['open','high','low','close'], inplace=True)
    return df

all_frames = []
end_date     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
target_start = (datetime.utcnow() - timedelta(days=1100)).strftime("%Y-%m-%d")

for page in range(25):
    print(f"  Page {page+1:02d}  end={end_date[:10]}...", end=" ", flush=True)
    dfp = fetch_page(end_date)
    if dfp is None or len(dfp) == 0:
        print("empty"); break
    print(f"{len(dfp)} bars  ({dfp.index[0].date()} → {dfp.index[-1].date()})")
    all_frames.append(dfp)
    earliest = dfp.index[0]
    if earliest.strftime("%Y-%m-%d") <= target_start:
        print(f"  Reached target start ({target_start})")
        break
    end_date = (earliest - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    if page < 24: time.sleep(12)

gold_15m = pd.concat(all_frames).sort_index()
gold_15m = gold_15m[~gold_15m.index.duplicated(keep='first')]
gold_15m['volume'] = gold_15m.get('volume', pd.Series(1.0, index=gold_15m.index)).fillna(1.0).replace(0, 1.0)
print(f"  Total: {len(gold_15m)} bars | {gold_15m.index[0].date()} → {gold_15m.index[-1].date()}")

# ── MACRO DATA (5 years for full cross-asset) ─────────────────────────────────
print("\nLoading macro data (5y daily)...")
def dl(t):
    try:
        d = yf.download(t, period="5y", interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        return d['close'].dropna()
    except Exception as e:
        print(f"  {t} failed: {e}")
        return None

gold_d = dl("GC=F"); dxy_d = dl("DX-Y.NYB"); tnx_d = dl("^TNX")
gdx_d  = dl("GDX");  tip_d = dl("TIP");       vix_d = dl("^VIX")
qqq_d  = dl("QQQ");  jpy_d = dl("JPY=X")

# ── ORIGINAL MACRO SCORE (5 components) ──────────────────────────────────────
dm = pd.DataFrame({'gold': gold_d}).dropna()
for name, data in [('dxy',dxy_d),('tnx',tnx_d),('gdx',gdx_d),
                   ('tip',tip_d),('vix',vix_d),('qqq',qqq_d),('jpy',jpy_d)]:
    if data is not None:
        dm[name] = data.reindex(dm.index, method='ffill')
dm.dropna(inplace=True)

dm['ema50']   = dm['gold'].ewm(span=50, adjust=False).mean()
# Original 5 components
dm['s_trend'] = np.where(dm['gold'] > dm['ema50'], 1, -1)
dm['s_yield'] = np.where(dm['tnx'].diff() < 0, 1, -1)
dm['s_dxy']   = np.where(dm['dxy'].pct_change() < 0, 1, -1)
dm['s_mom']   = np.where(dm['gold'].pct_change(5) > 0, 1, -1)
dm['s_gdx']   = np.where(dm['gdx'].pct_change() > dm['gold'].pct_change(), 1, -1)
raw_orig = (dm['s_trend'] + dm['s_yield'] + dm['s_dxy'] + dm['s_mom'] + dm['s_gdx']).shift(1)
dm['score_orig'] = (raw_orig / 5 * 10).round(1)

# Alternative 6 components: replace DXY/TNX with TIP/QQQ, add JPY
qe50 = dm['qqq'].ewm(span=50, adjust=False).mean()
dm['s_tip'] = np.where(dm['tip'].pct_change() > 0, 1, -1)   # TIP up = real yields down
dm['s_qqq'] = np.where(dm['qqq'] > qe50, 1, -1)             # risk-on regime
dm['s_jpy'] = np.where(dm['jpy'].pct_change() < 0, 1, -1)   # USD weaker vs JPY = gold up
raw_alt = (dm['s_trend'] + dm['s_tip'] + dm['s_qqq'] + dm['s_mom'] + dm['s_gdx'] + dm['s_jpy']).shift(1)
dm['score_alt'] = (raw_alt / 6 * 10).round(1)

dm.dropna(inplace=True)
macro_orig = {d.date(): float(r['score_orig']) for d, r in dm.iterrows()}
macro_alt  = {d.date(): float(r['score_alt'])  for d, r in dm.iterrows()}
macro_vix  = {d.date(): float(r['vix'])         for d, r in dm.iterrows()}

print(f"  Macro data: {len(dm)} trading days")

# ── 4H CONTEXT ────────────────────────────────────────────────────────────────
gold_1h = yf.download("GC=F", period="720d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index   = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)
print(f"  4H context: {len(gold_1h)} hourly bars, {gold_1h.index[0].date() if len(gold_1h) > 0 else 'n/a'} → {gold_1h.index[-1].date() if len(gold_1h) > 0 else 'n/a'}")
g4 = gold_1h.resample('4h', label='left', closed='left').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
tr4 = pd.concat([g4['high']-g4['low'],
                 (g4['high']-g4['close'].shift()).abs(),
                 (g4['low'] -g4['close'].shift()).abs()], axis=1).max(axis=1)
g4['atr'] = tr4.ewm(span=14, adjust=False).mean()
def get_4h(ts):
    past = g4[g4.index <= ts]
    return past.iloc[-1] if len(past) else None

df = gold_15m.copy()
df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
tr = pd.concat([df['high']-df['low'],
                (df['high']-df['close'].shift()).abs(),
                (df['low'] -df['close'].shift()).abs()], axis=1).max(axis=1)
df['atr'] = tr.ewm(span=14, adjust=False).mean()

# ── ADX on 15m bars (Wilder's method) ────────────────────────────────────────
_pdm  = df['high'].diff()
_mdm  = -df['low'].diff()
_pdm  = _pdm.where((_pdm > 0) & (_pdm > _mdm), 0.0)
_mdm  = _mdm.where((_mdm > 0) & (_mdm > _pdm), 0.0)
_atr14 = tr.ewm(span=14, adjust=False).mean()
_pdi  = 100 * _pdm.ewm(span=14, adjust=False).mean() / _atr14
_mdi  = 100 * _mdm.ewm(span=14, adjust=False).mean() / _atr14
_dx   = 100 * (_pdi - _mdi).abs() / (_pdi + _mdi).replace(0, np.nan)
df['adx'] = _dx.ewm(span=14, adjust=False).mean()
df.dropna(inplace=True)

# ── ECONOMIC CALENDAR: NFP DAYS ───────────────────────────────────────────────
def get_nfp_days(start_year, end_year):
    """First Friday of each month = NFP release day."""
    nfp_days = set()
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # Find first Friday
            for day in range(1, 8):
                import datetime as dt
                d = dt.date(year, month, day)
                if d.weekday() == 4:  # Friday
                    nfp_days.add(d)
                    break
    return nfp_days

nfp_days = get_nfp_days(2020, 2027)
print(f"  NFP filter: {len(nfp_days)} first-Friday days")

# ── DAILY ATR PERCENTILE ──────────────────────────────────────────────────────
print("Computing daily ATR percentile...")
_gd = yf.download("GC=F", period="5y", interval="1d", progress=False)
_gd.columns  = [c[0].lower() for c in _gd.columns]
_gd.index    = pd.to_datetime(_gd.index).tz_localize(None)
_gd.dropna(inplace=True)
_tr_d = pd.concat([_gd['high'] - _gd['low'],
                   (_gd['high'] - _gd['close'].shift()).abs(),
                   (_gd['low']  - _gd['close'].shift()).abs()], axis=1).max(axis=1)
_atr_d = _tr_d.ewm(span=14, adjust=False).mean()
_atr_pct = _atr_d.rank(pct=True)
daily_atr_pct = {d.date(): float(p) for d, p in _atr_pct.items()}
print(f"  Daily ATR percentile: {len(daily_atr_pct)} days")

# ── LONDON ORB DIRECTION (07:00-09:00 UTC) ────────────────────────────────────
print("Computing London ORB direction...")
orb_direction = {}
for _date in sorted(set(df.index.date)):
    _ts       = pd.Timestamp(_date)
    _orb_bars = df.loc[(df.index >= _ts + pd.Timedelta(hours=7)) &
                       (df.index <  _ts + pd.Timedelta(hours=9))]
    if len(_orb_bars) < 3:
        continue
    _orb_mid = (_orb_bars['high'].max() + _orb_bars['low'].min()) / 2
    orb_direction[_date] = 1 if _orb_bars.iloc[-1]['close'] > _orb_mid else -1
print(f"  ORB direction: {len(orb_direction)} days")

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def run(min_score=4,
        confirm_bar=False,
        entry_window=(14.0, 16.5),
        macro_scores=None,
        skip_vix_above=None,
        skip_months=None,
        skip_nfp=False,
        long_only=False,
        add_london_session=False,
        require_adx_range=False,
        skip_low_atr=False,
        use_orb_bias=False,
        skip_monday=False,
        label=""):
    """
    min_score       : |macro score| threshold to trade a day
    confirm_bar     : wait for next bar to confirm direction
    entry_window    : (start_h, end_h) UTC for NY session entries
    macro_scores    : dict of date→score (use macro_orig or macro_alt)
    skip_vix_above  : skip day if VIX >= this value (e.g. 30)
    skip_months     : list of month numbers to skip (e.g. [6] for June)
    skip_nfp        : skip first Friday of month (NFP day)
    long_only       : only take long trades
    add_london_session: also run London session 07:00-10:00 UTC
    require_adx_range : only enter if 15m ADX in 20-35 (not chop, not runaway)
    skip_low_atr    : skip days where daily ATR outside 40th-80th percentile
    use_orb_bias    : only trade in direction of London ORB (07:00-09:00 UTC)
    skip_monday     : skip Monday entries (worst day-of-week historically)
    """
    if macro_scores is None:
        macro_scores = macro_orig

    equity = ACCOUNT
    trades = []

    # Build session list: NY always, optionally London
    sessions = [
        (SESS_OPEN, SESS_CLOSE, entry_window),
    ]
    if add_london_session:
        sessions = [
            (LON_OPEN,  LON_CLOSE,  (7.5, 9.5)),   # London: entry 07:30-09:30
            (SESS_OPEN, SESS_CLOSE, entry_window),   # NY session
        ]

    for date in sorted(set(df.index.date)):
        score = macro_scores.get(date)
        if score is None or abs(score) < min_score:
            continue

        # Regime filters
        if skip_months and date.month in skip_months:
            continue
        if skip_nfp and date in nfp_days:
            continue
        if skip_vix_above is not None:
            vix_val = macro_vix.get(date)
            if vix_val is not None and vix_val >= skip_vix_above:
                continue
        if skip_monday and date.weekday() == 0:
            continue
        if skip_low_atr:
            atr_p = daily_atr_pct.get(date)
            if atr_p is not None and not (0.40 <= atr_p <= 0.80):
                continue

        macro_dir = 1 if score > 0 else -1
        if use_orb_bias:
            orb_dir = orb_direction.get(date)
            if orb_dir is not None and orb_dir != macro_dir:
                continue
        if long_only and macro_dir == -1:
            continue

        # Run each session for this day
        for (s_open, s_close, e_window) in sessions:
            ts    = pd.Timestamp(date)
            o_ts  = ts + pd.Timedelta(hours=s_open[0], minutes=s_open[1])
            c_ts  = ts + pd.Timedelta(hours=s_close[0], minutes=s_close[1])
            sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
            if len(sbars) < 4:
                continue
            ctx = get_4h(o_ts)
            if ctx is None:
                continue
            if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']:
                continue

            tp  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
            vol = sbars['volume'].replace(0, 1)
            sbars = sbars.copy()
            sbars['vwap']     = (tp * vol).cumsum() / vol.cumsum()
            sbars['vwap_std'] = tp.expanding().std().bfill().fillna(1)

            in_trade = False
            entry_px = sl = tp1_px = tp2_px = sz1 = sz2 = pnl1 = 0
            tp1_hit  = False
            bars_held = 0
            trade_dir = 0
            trades_today = 0
            last_entry_i = -99
            pending_entry = False

            for i in range(1, len(sbars)):
                b    = sbars.iloc[i]
                prev = sbars.iloc[i - 1]
                hi, lo, cls = b['high'], b['low'], b['close']
                vwap = b['vwap']
                std  = max(b['vwap_std'], 0.01)

                if e_window is not None:
                    h = b.name.hour + b.name.minute / 60
                    if not (e_window[0] <= h <= e_window[1]):
                        pending_entry = False
                        continue

                if in_trade:
                    bars_held += 1
                    if trade_dir == 1:
                        if lo <= sl:
                            pnl = (sl - entry_px) * (sz1 + sz2) + pnl1
                            trades.append(dict(date=date, dir='LONG', entry=entry_px,
                                exit=sl, pnl=pnl, reason='SL', score=score, bars=bars_held,
                                session='LON' if s_open[0] < 12 else 'NY'))
                            equity += pnl; in_trade = False; continue
                        if not tp1_hit and hi >= tp1_px:
                            pnl1 = (tp1_px - entry_px) * sz1
                            equity += pnl1; sl = entry_px; tp1_hit = True
                        if tp1_hit and hi >= tp2_px:
                            pnl2 = (tp2_px - entry_px) * sz2
                            trades.append(dict(date=date, dir='LONG', entry=entry_px,
                                exit=tp2_px, pnl=pnl1 + pnl2, reason='TP2', score=score, bars=bars_held,
                                session='LON' if s_open[0] < 12 else 'NY'))
                            equity += pnl2; in_trade = False; continue
                    else:
                        if hi >= sl:
                            pnl = (entry_px - sl) * (sz1 + sz2) + pnl1
                            trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                                exit=sl, pnl=pnl, reason='SL', score=score, bars=bars_held,
                                session='LON' if s_open[0] < 12 else 'NY'))
                            equity += pnl; in_trade = False; continue
                        if not tp1_hit and lo <= tp1_px:
                            pnl1 = (entry_px - tp1_px) * sz1
                            equity += pnl1; sl = entry_px; tp1_hit = True
                        if tp1_hit and lo <= tp2_px:
                            pnl2 = (entry_px - tp2_px) * sz2
                            trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                                exit=tp2_px, pnl=pnl1 + pnl2, reason='TP2', score=score, bars=bars_held,
                                session='LON' if s_open[0] < 12 else 'NY'))
                            equity += pnl2; in_trade = False; continue
                    if bars_held >= TIME_STOP:
                        rem = sz2 if tp1_hit else (sz1 + sz2)
                        pnl = ((cls - entry_px) if trade_dir == 1 else (entry_px - cls)) * rem + pnl1
                        trades.append(dict(date=date, dir='LONG' if trade_dir == 1 else 'SHORT',
                            entry=entry_px, exit=cls, pnl=pnl, reason='TIME', score=score, bars=bars_held,
                            session='LON' if s_open[0] < 12 else 'NY'))
                        equity += pnl; in_trade = False
                else:
                    if confirm_bar and pending_entry:
                        confirmed = (macro_dir == 1 and cls > prev['close']) or \
                                    (macro_dir == -1 and cls < prev['close'])
                        if confirmed:
                            atr_v = float(b['atr'])
                            if atr_v > 0:
                                tot_sz = (equity * RISK_PCT) / (SL_MULT * atr_v)
                                sz1 = tot_sz * TP1_FRAC; sz2 = tot_sz * TP2_FRAC
                                entry_px = cls
                                if macro_dir == 1:
                                    sl = entry_px - SL_MULT*atr_v; tp1_px = entry_px + TP1_MULT*atr_v; tp2_px = entry_px + TP2_MULT*atr_v
                                else:
                                    sl = entry_px + SL_MULT*atr_v; tp1_px = entry_px - TP1_MULT*atr_v; tp2_px = entry_px - TP2_MULT*atr_v
                                in_trade = True; trade_dir = macro_dir
                                bars_held = 0; tp1_hit = False; pnl1 = 0
                                trades_today += 1; last_entry_i = i
                        pending_entry = False
                        continue

                    if trades_today >= MAX_TRADES: continue
                    if i - last_entry_i < MIN_BARS_BETWEEN: continue
                    ctx_now = get_4h(b.name)
                    if ctx_now is None: continue
                    h4_ok = (macro_dir == 1 and ctx_now['ema9'] > ctx_now['ema21']) or \
                            (macro_dir == -1 and ctx_now['ema9'] < ctx_now['ema21'])
                    if not h4_ok: continue
                    ema_ok = (macro_dir == 1 and b['ema9'] > b['ema21']) or \
                             (macro_dir == -1 and b['ema9'] < b['ema21'])
                    if not ema_ok: continue
                    if require_adx_range and not (20 <= b['adx'] <= 35):
                        continue
                    dist    = (cls - vwap) / std
                    vwap_ok = (macro_dir == 1 and -1.0 <= dist <= 0.3) or \
                              (macro_dir == -1 and -0.3 <= dist <= 1.0)
                    if not vwap_ok: continue

                    if confirm_bar:
                        pending_entry = True
                        continue

                    atr_v = float(b['atr'])
                    if atr_v <= 0: continue
                    tot_sz = (equity * RISK_PCT) / (SL_MULT * atr_v)
                    sz1    = tot_sz * TP1_FRAC; sz2 = tot_sz * TP2_FRAC
                    entry_px = cls
                    if macro_dir == 1:
                        sl = entry_px - SL_MULT*atr_v; tp1_px = entry_px + TP1_MULT*atr_v; tp2_px = entry_px + TP2_MULT*atr_v
                    else:
                        sl = entry_px + SL_MULT*atr_v; tp1_px = entry_px - TP1_MULT*atr_v; tp2_px = entry_px - TP2_MULT*atr_v
                    in_trade = True; trade_dir = macro_dir
                    bars_held = 0; tp1_hit = False; pnl1 = 0
                    trades_today += 1; last_entry_i = i

            if in_trade:
                cls = sbars.iloc[-1]['close']
                rem = sz2 if tp1_hit else (sz1 + sz2)
                pnl = ((cls - entry_px) if trade_dir == 1 else (entry_px - cls)) * rem + pnl1
                trades.append(dict(date=date, dir='LONG' if trade_dir == 1 else 'SHORT',
                    entry=entry_px, exit=cls, pnl=pnl, reason='SESS', score=score, bars=bars_held,
                    session='LON' if s_open[0] < 12 else 'NY'))
                equity += pnl

    return equity, trades


def summarize(eq, trs, label=""):
    if not trs:
        return (label, 0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    t      = pd.DataFrame(trs)
    n      = len(t)
    roi    = (eq - ACCOUNT) / ACCOUNT * 100
    wr     = t['pnl'].gt(0).mean() * 100
    wks    = max((pd.to_datetime(t['date']).max() - pd.to_datetime(t['date']).min()).days / 7, 1)
    tpw    = n / wks
    sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252) if t['pnl'].std() > 0 else 0
    eq_c   = ACCOUNT + t['pnl'].cumsum()
    mdd    = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
    _, pv  = stats.ttest_1samp(t['pnl'], 0) if n > 5 else (0, 1.0)
    return (label, n, tpw, roi, wr, sharpe, pv, mdd)


# ── VARIANT DEFINITIONS ───────────────────────────────────────────────────────
BASE_NY    = dict(min_score=4, confirm_bar=False, entry_window=(14.0, 16.5),
                  macro_scores=macro_orig)
BASE_SCORE2= dict(min_score=2, confirm_bar=False, entry_window=(14.0, 16.5),
                  macro_scores=macro_orig)

variants = [
    # Reference
    ("BASELINE        (V2 best: score≥4, NY only)",
     {**BASE_NY}),
    # A: Score threshold
    ("A: score≥2      (more trades, weaker filter)",
     {**BASE_SCORE2}),
    ("A: score≥6      (stricter filter, higher quality)",
     {**BASE_NY, 'min_score':6}),
    # B: VIX regime filter
    ("B: + skip VIX≥30",
     {**BASE_NY, 'skip_vix_above':30}),
    ("B: + skip VIX≥25",
     {**BASE_NY, 'skip_vix_above':25}),
    # C: Month filter
    ("C: + skip June  (Sharpe -1.67 historically)",
     {**BASE_NY, 'skip_months':[6]}),
    ("C: + skip Jun+May (Sharpe -0.47 historically)",
     {**BASE_NY, 'skip_months':[5,6]}),
    # D: NFP skip
    ("D: + skip NFP day (1st Friday)",
     {**BASE_NY, 'skip_nfp':True}),
    # E: Combo regime filters
    ("E: VIX≥30 + June skip",
     {**BASE_NY, 'skip_vix_above':30, 'skip_months':[6]}),
    ("E: VIX≥30 + Jun+May skip",
     {**BASE_NY, 'skip_vix_above':30, 'skip_months':[5,6]}),
    ("E: VIX≥30 + Jun skip + NFP skip",
     {**BASE_NY, 'skip_vix_above':30, 'skip_months':[6], 'skip_nfp':True}),
    # F: Alternative macro score
    ("F: Alt score (TIP+QQQ+JPY vs DXY+TNX)",
     {**BASE_NY, 'macro_scores':macro_alt}),
    ("F: Alt score + VIX≥30 + Jun skip",
     {**BASE_NY, 'macro_scores':macro_alt, 'skip_vix_above':30, 'skip_months':[6]}),
    # G: London session
    ("G: + London session 07:30-09:30",
     {**BASE_NY, 'add_london_session':True}),
    ("G: London only (07:30-09:30)",
     {**BASE_NY, 'entry_window':(7.5, 9.5),
      'add_london_session':False}),  # override: no NY
    # H: Long-only
    ("H: Long-only    (bull market filter)",
     {**BASE_NY, 'long_only':True}),
    ("H: Long-only + VIX≥30 + Jun skip",
     {**BASE_NY, 'long_only':True, 'skip_vix_above':30, 'skip_months':[6]}),
    # Best combo to test
    ("BEST COMBO?     score≥4 + VIX≥30 + Jun skip + NFP + long-only",
     {**BASE_NY, 'skip_vix_above':30, 'skip_months':[6],
      'skip_nfp':True, 'long_only':True}),
    ("BEST COMBO?     score≥2 + VIX≥30 + Jun skip",
     {**BASE_SCORE2, 'skip_vix_above':30, 'skip_months':[6]}),
    # I: ADX regime filter (research: ADX 20-35 = VWAP pullback sweet spot)
    ("I: + ADX 20-35  (not chop <20, not runaway >35)",
     {**BASE_NY, 'require_adx_range':True}),
    ("I: ADX 20-35 + VIX≥30 + Jun skip",
     {**BASE_NY, 'require_adx_range':True, 'skip_vix_above':30, 'skip_months':[6]}),
    # J: Daily ATR percentile filter (research: 40th-80th pctile is sweet spot)
    ("J: + ATR 40-80th pctile (not quiet, not chaotic)",
     {**BASE_NY, 'skip_low_atr':True}),
    ("J: ATR filter + VIX≥30 + Jun skip",
     {**BASE_NY, 'skip_low_atr':True, 'skip_vix_above':30, 'skip_months':[6]}),
    # K: London ORB direction bias (research: 65-70% directional accuracy)
    ("K: + ORB direction align (trade with London breakout)",
     {**BASE_NY, 'use_orb_bias':True}),
    ("K: ORB + VIX≥30 + Jun skip",
     {**BASE_NY, 'use_orb_bias':True, 'skip_vix_above':30, 'skip_months':[6]}),
    # L: Skip Monday (research: Monday = worst day for gold)
    ("L: + skip Mondays (worst day-of-week for gold)",
     {**BASE_NY, 'skip_monday':True}),
    # Best of new research filters
    ("NEW BEST?  ADX + ORB + VIX≥30 + Jun skip",
     {**BASE_NY, 'require_adx_range':True, 'use_orb_bias':True,
      'skip_vix_above':30, 'skip_months':[6]}),
    ("NEW BEST?  ADX + ATR + ORB + VIX≥30 + Jun skip",
     {**BASE_NY, 'require_adx_range':True, 'skip_low_atr':True,
      'use_orb_bias':True, 'skip_vix_above':30, 'skip_months':[6]}),
    ("NEW BEST?  ADX + ORB + VIX≥30 + Jun + NFP + No-Mon",
     {**BASE_NY, 'require_adx_range':True, 'use_orb_bias':True,
      'skip_vix_above':30, 'skip_months':[6], 'skip_nfp':True, 'skip_monday':True}),
]

# ── RUN ALL VARIANTS ──────────────────────────────────────────────────────────
print("\nRunning variants...")
results = []
for label, kwargs in variants:
    eq, trs = run(**kwargs, label=label)
    res = summarize(eq, trs, label)
    results.append(res)
    label_s, n, tpw, roi, wr, sharpe, pv, mdd = res
    sig = " ✓" if pv < 0.05 else ("~" if pv < 0.10 else "  ")
    print(f"  {label[:55]:<55} n={n:>4}  p={pv:.4f}{sig}  ROI={roi:>+7.1f}%  WR={wr:.1f}%  Sh={sharpe:.2f}")

# ── COMPARISON TABLE ──────────────────────────────────────────────────────────
print(f"\n{'='*110}")
print(f"  GOLD V3 — VARIANT COMPARISON (3-year data)")
print(f"{'='*110}")
print(f"  {'Variant':<55} {'N':>5} {'TPW':>5} {'ROI':>8} {'WR':>7} {'Sharpe':>7} {'p-val':>8} {'MDD':>7}")
print(f"  {'─'*105}")

baseline_sharpe = results[0][5]
baseline_wr     = results[0][4]

for i, (label, n, tpw, roi, wr, sharpe, pv, mdd) in enumerate(results):
    sig  = " ✓" if pv < 0.05 else ("~" if pv < 0.10 else "  ")
    ds   = f" ({sharpe-baseline_sharpe:+.2f})" if i > 0 and n > 0 else ""
    flag = "  ← BASELINE" if i == 0 else ""
    print(f"  {label:<55} {n:>5} {tpw:>4.1f} {roi:>+7.1f}% {wr:>6.1f}% {sharpe:>7.2f}{ds:<9} {pv:>8.4f}{sig:>3} {mdd:>6.1f}%{flag}")

# ── BEST VARIANT DETAIL ───────────────────────────────────────────────────────
valid = [(l,n,tpw,roi,wr,sh,pv,mdd) for (l,n,tpw,roi,wr,sh,pv,mdd) in results
         if n >= 50 and pv < 0.10]
if not valid:
    valid = [(l,n,tpw,roi,wr,sh,pv,mdd) for (l,n,tpw,roi,wr,sh,pv,mdd) in results if n >= 30]
best       = max(valid, key=lambda x: x[5]) if valid else results[0]
best_label = best[0]
best_kwargs = next(k for l, k in [(v[0], v[1]) for v in variants] if l == best_label)

print(f"\n{'='*110}")
print(f"  BEST: {best_label.strip()}")
print(f"{'='*110}")
eq_best, trs_best = run(**best_kwargs)
if not trs_best:
    print("  No trades generated — check data/score alignment.")
    import sys; sys.exit(0)
t = pd.DataFrame(trs_best)
t['date']  = pd.to_datetime(t['date'])
t['win']   = t['pnl'] > 0
t['month'] = t['date'].dt.to_period('M')
n    = len(t)
_, pv = stats.ttest_1samp(t['pnl'], 0)
roi  = (eq_best - ACCOUNT) / ACCOUNT * 100
wks  = max((t['date'].max() - t['date'].min()).days / 7, 1)
eq_c = ACCOUNT + t['pnl'].cumsum()
mdd  = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252)
aw = t.loc[t['win'], 'pnl'].mean() if t['win'].any() else 0
al = t.loc[~t['win'], 'pnl'].mean() if (~t['win']).any() else 0
print(f"""
  $10,000 → ${eq_best:,.0f}  ({roi:+.1f}% ROI)
  Sharpe : {sharpe:.2f}    Max drawdown : {mdd:.1f}%
  Trades : {n}  ({n/wks:.1f}/week)
  WR     : {t['win'].mean()*100:.1f}%    Avg win ${aw:+,.0f}  |  Avg loss ${al:+,.0f}
  R:R    : {abs(aw/al):.2f}:1  (break-even WR: {abs(al)/(aw+abs(al))*100:.1f}%)
  p-value: {pv:.4f}  {'✓ SIGNIFICANT' if pv < 0.05 else '~ borderline (p<0.10)' if pv < 0.10 else '— not significant'}
""")

print("  Direction:")
for d, g in t.groupby('dir'):
    print(f"    {d:<6} {len(g):>4}tr | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f} | total ${g['pnl'].sum():+,.0f}")

print("\n  Exit reasons:")
for r, g in t.groupby('reason'):
    print(f"    {r:<6} {len(g):>4} | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

if 'session' in t.columns:
    print("\n  By session:")
    for s, g in t.groupby('session'):
        print(f"    {s:<4}  {len(g):>4}tr | WR {(g['pnl']>0).mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

print("\n  Monthly P&L:")
pos = 0; run_eq = ACCOUNT
for mo, g in t.groupby('month'):
    run_eq += g['pnl'].sum()
    flag = "✓" if g['pnl'].sum() > 0 else "✗"
    if g['pnl'].sum() > 0: pos += 1
    print(f"    {str(mo)}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+8,.0f}  ${run_eq:>8,.0f} {flag}")
print(f"\n  Positive months: {pos}/{t['month'].nunique()}")

# ── WHAT CHANGED — ACTIONABLE VERDICT ─────────────────────────────────────────
print(f"\n{'='*110}")
print("  VERDICT — signal changes to add to gold_signal.gs")
print(f"{'='*110}")
print(f"  {'Improvement':<45} {'ΔSharpe':>9} {'ΔWR':>8} {'ΔN':>6}  Rec")
print(f"  {'─'*85}")
for label, n, tpw, roi, wr, sharpe, pv, mdd in results[1:]:
    if n == 0: continue
    ds = sharpe - baseline_sharpe
    dwr = wr - baseline_wr
    dn  = n - results[0][1]
    rec = "✓ ADD" if (ds > 0.2 and pv < 0.10) else ("~ TEST" if ds > 0.05 else ("✗ SKIP"))
    print(f"  {label[:45]:<45} {ds:>+9.2f} {dwr:>+7.1f}% {dn:>+6}  {rec}")

t.to_csv("gold_v3_trades.csv", index=False)
print("\nSaved: gold_v3_trades.csv")
print("="*110)
