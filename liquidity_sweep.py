"""
LIQUIDITY SWEEP SCALPING SYSTEM — BACKTEST
============================================
Based on methodology used by professional scalpers:
  - ICT (Inner Circle Trader)
  - Paul Rotter
  - Tom Hougaard
  - Ross Cameron

CORE CONCEPT:
  Price moves to a key level → sweeps through it (triggering stops) →
  volume spikes → price reverses back through the level → YOU ENTER

NO LAGGING INDICATORS. No MACD, RSI, OBV.
Only: Price action + Key levels + Volume + Session timing

KEY LEVELS (in order of importance):
  1. Previous session high/low — most watched by institutions
  2. VWAP — institutional benchmark, resets each session
  3. Round numbers — options clusters, algo triggers
  4. Intraday swing high/low — recent structure within session

SESSION WINDOWS (when institutions are active):
  London open : 08:00-10:00 UTC (12:00-14:00 Dubai)
  NY open     : 13:30-15:30 UTC (17:30-19:30 Dubai)
  NY prime    : 14:00-17:00 UTC (18:00-21:00 Dubai)

THE SWEEP SETUP:
  LONG: Price dips below key level (sweeps lows/stops) →
        closes back above level on same candle or next →
        volume above average → enter long
        SL: below the sweep wick
        TP: next key level above

  SHORT: Price pops above key level (sweeps highs/stops) →
         closes back below level → volume above average → enter short
         SL: above the sweep wick
         TP: next key level below

Run: pip install yfinance pandas numpy scipy requests
     python liquidity_sweep.py
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, time, timedelta
import time as time_mod
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT       = 10000.0
RISK_PCT      = 0.02       # 2% risk per trade
TP1_MULT      = 1.5        # TP1 = 1.5x SL distance
TP2_MULT      = 2.5        # TP2 = 2.5x SL distance
TP1_CLOSE     = 0.60       # close 60% at TP1
TP2_CLOSE     = 0.40       # close 40% at TP2
TIME_STOP     = 6          # exit after 6 bars (90 min on 15m) if no move
COMMISSION    = 0.00055    # 0.055% per side

# Sweep parameters
SWEEP_BUFFER  = 0.0050     # 0.50% — how far price can breach level to qualify as sweep
MIN_VOL_MULT  = 1.3        # volume must be 1.3x average on sweep candle
LEVEL_RADIUS  = 0.0020     # 0.20% — how close price must be to a key level

# Round number grids
BTC_GRID      = 1000       # $1,000 grid for BTC
GOLD_GRID     = 50         # $50 grid for gold

TD_KEY        = "06f050cd7d9940a895f9461e7f0ff7e3"

# Session windows UTC
SESSIONS = [
    ("London",   time(8,  0), time(10, 0)),
    ("NY_open",  time(13,30), time(15,30)),
    ("NY_prime", time(14, 0), time(17, 0)),
]

print("="*68)
print("  LIQUIDITY SWEEP SCALPING SYSTEM")
print("  Price Action + Key Levels + Volume | No Lagging Indicators")
print("  Testing: BTC (15m) and Gold (15m)")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_binance_btc(days=365):
    print(f"\nLoading BTC 15m from Binance ({days} days)...")
    url      = "https://api.binance.com/api/v3/klines"
    all_bars = []
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff   = end_ms - days * 86400 * 1000

    while True:
        try:
            r = requests.get(url, params={
                "symbol":"BTCUSDT","interval":"15m",
                "limit":1000,"endTime":end_ms},
                timeout=15, headers={"User-Agent":"Mozilla/5.0"})
            data = r.json()
            if not data: break
            done = False
            for bar in data:
                if int(bar[0]) < cutoff:
                    done = True
                    break
                all_bars.append(bar)
            if done: break
            end_ms = int(data[0][0]) - 1
            time_mod.sleep(0.1)
        except Exception as e:
            print(f"  Binance error: {e}")
            break

    if not all_bars:
        return None
    all_bars.sort(key=lambda x: x[0])
    df = pd.DataFrame(all_bars, columns=[
        'ts','open','high','low','close','volume',
        'close_ts','qv','trades','tbbv','tbqv','ignore'])
    df.index = pd.to_datetime(df['ts'].astype(float),
                              unit='ms', utc=True).dt.tz_convert(None)
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    df = df[['open','high','low','close','volume']].dropna()
    print(f"  BTC: {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
    return df

def _load_twelve(symbol, label, pages=8):
    import urllib.parse
    all_frames = []
    end_date   = None
    for page in range(pages):
        try:
            url = (f"https://api.twelvedata.com/time_series?"
                   f"symbol={urllib.parse.quote(symbol)}&interval=15min"
                   f"&outputsize=5000&order=DESC&apikey={TD_KEY}")
            if end_date:
                url += f"&end_date={urllib.parse.quote(end_date)}"
            r   = requests.get(url, timeout=30)
            raw = r.json()
            if 'values' not in raw:
                print(f"  Error: {raw.get('message','unknown')}")
                break
            df = pd.DataFrame(raw['values'])
            df.index = pd.to_datetime(df['datetime'])
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df = df[cols].copy()
            for c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df.dropna(inplace=True)
            if 'volume' not in df.columns:
                df['volume'] = 1.0
            if len(df) == 0: break
            all_frames.append(df)
            oldest   = df.index.min()
            end_date = (oldest - pd.Timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"  Page {page+1}: {len(df)} bars | oldest {oldest.date()}")
            time_mod.sleep(8)
        except Exception as e:
            print(f"  Error: {e}")
            break
    if not all_frames:
        return None
    result = pd.concat(all_frames)
    result = result[~result.index.duplicated(keep='first')].sort_index()
    print(f"  {label}: {len(result)} bars | {result.index[0].date()} → {result.index[-1].date()}")
    return result

def load_gold_twelve():
    print(f"\nLoading Gold 15m from Twelve Data...")
    return _load_twelve("XAU/USD", "Gold")

def load_btc_twelve():
    print(f"\nLoading BTC 15m from Twelve Data...")
    return _load_twelve("BTC/USD", "BTC")

# ══════════════════════════════════════════════════════════════════════════════
#  KEY LEVEL COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_levels(df, rn_grid):
    """
    Compute all key levels for each bar:
      1. Previous session high/low (prior day's range)
      2. Session VWAP
      3. Nearest round number
      4. Intraday swing high/low (last 20 bars)
    """
    d = df.copy()
    d['date'] = d.index.normalize()

    # ── Previous session high/low ──────────────────────────────────────────
    daily = d.groupby('date').agg(sess_h=('high','max'), sess_l=('low','min'))
    d['prev_h'] = d['date'].map(daily['sess_h'].shift(1).to_dict())
    d['prev_l'] = d['date'].map(daily['sess_l'].shift(1).to_dict())

    # ── Session VWAP ───────────────────────────────────────────────────────
    d['typ']    = (d['high'] + d['low'] + d['close']) / 3
    d['ctv']    = d.groupby('date').apply(
        lambda g:(g['typ']*g['volume']).cumsum()
    ).reset_index(level=0, drop=True)
    d['cv']     = d.groupby('date')['volume'].cumsum()
    d['vwap']   = d['ctv'] / (d['cv'] + 1e-10)

    # ── Round numbers ──────────────────────────────────────────────────────
    d['rn'] = (d['close'] / rn_grid).round() * rn_grid

    # ── Intraday swing high/low (last 20 bars, prior bars only) ──────────────
    # shift(1) so the current bar doesn't set its own sweep level
    d['swing_h'] = d['high'].shift(1).rolling(20).max()
    d['swing_l'] = d['low'].shift(1).rolling(20).min()

    # ── Volume MA ──────────────────────────────────────────────────────────
    d['vol_ma'] = d['volume'].rolling(20).mean()

    # ── ATR ───────────────────────────────────────────────────────────────
    d['tr']  = np.maximum(d['high']-d['low'],
               np.maximum((d['high']-d['close'].shift(1)).abs(),
                          (d['low'] -d['close'].shift(1)).abs()))
    d['atr'] = d['tr'].rolling(14).mean()

    # ── Session ───────────────────────────────────────────────────────────
    def get_sess(ts):
        t = ts.time() if hasattr(ts,'time') else ts
        for nm,s,e in SESSIONS:
            if s <= t < e: return nm
        return None

    d['session']  = d.index.map(get_sess)
    d['in_sess']  = d['session'].notna()
    d['sess_bar'] = d.groupby(['date','session']).cumcount()

    d.dropna(subset=['atr','vwap','prev_h','prev_l'], inplace=True)
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  SWEEP DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_sweeps(df, vol_mult=None):
    """
    Detect liquidity sweep setups at key levels.

    BULLISH SWEEP (long setup):
      - Low dips below a key level (sweeps stops below)
      - Close recovers back above the level
      - Volume elevated on the sweep candle
      - Body closes in upper 40% of candle range

    BEARISH SWEEP (short setup):
      - High pops above a key level (sweeps stops above)
      - Close falls back below the level
      - Volume elevated
      - Body closes in lower 40% of candle range
    """
    d = df.copy()
    effective_vol_mult = vol_mult if vol_mult is not None else MIN_VOL_MULT

    def check_level_sweep(row, level, direction):
        """Check if current bar sweeps a specific level."""
        if pd.isna(level) or level <= 0:
            return False

        if effective_vol_mult > 1.0:
            if pd.isna(row['vol_ma']) or row['volume'] < row['vol_ma'] * effective_vol_mult:
                return False

        candle_range = row['high'] - row['low']
        if candle_range <= 0:
            return False

        if direction == 'LONG':
            # Low must breach below level
            swept = row['low'] < level * (1 - 0.0001)
            # But not too far (still a sweep, not a breakdown)
            not_too_deep = row['low'] >= level * (1 - SWEEP_BUFFER)
            # Close must recover above level
            recovered = row['close'] > level
            # Body in upper portion (bullish candle)
            bull_body = (row['close'] - row['low']) / candle_range >= 0.55
            return swept and not_too_deep and recovered and bull_body

        else:  # SHORT
            # High must breach above level
            swept = row['high'] > level * (1 + 0.0001)
            # But not too far
            not_too_high = row['high'] <= level * (1 + SWEEP_BUFFER)
            # Close must fall back below level
            recovered = row['close'] < level
            # Body in lower portion (bearish candle)
            bear_body = (row['high'] - row['close']) / candle_range >= 0.55
            return swept and not_too_high and recovered and bear_body

    # Collect all sweep signals
    long_signals  = []
    short_signals = []

    levels_to_check = ['prev_h', 'prev_l', 'vwap', 'rn', 'swing_h', 'swing_l']

    for i, (idx, row) in enumerate(d.iterrows()):
        if not row['in_sess']:
            long_signals.append(False)
            short_signals.append(False)
            continue

        # Need at least 4 session bars for VWAP to be reliable
        if row.get('sess_bar', 0) < 4:
            long_signals.append(False)
            short_signals.append(False)
            continue

        found_long  = False
        found_short = False

        for lvl_col in levels_to_check:
            level = row.get(lvl_col, np.nan)
            if pd.isna(level): continue

            if check_level_sweep(row, level, 'LONG'):
                found_long = True
            if check_level_sweep(row, level, 'SHORT'):
                found_short = True

        long_signals.append(found_long)
        short_signals.append(found_short)

    d['long_sweep']  = long_signals
    d['short_sweep'] = short_signals
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(df, label):
    if df is None or len(df) == 0:
        return None
    equity   = ACCOUNT
    in_trade = False
    entry    = {}
    trades   = []
    eq_c     = [ACCOUNT]
    eq_d     = [df.index[0]]

    for i in range(len(df)):
        row  = df.iloc[i]
        idx  = df.index[i]
        cp   = row['close']
        atr  = row['atr']
        sess = row['session']

        if pd.isna(atr) or atr == 0:
            continue

        # ── EXIT ─────────────────────────────────────────────────────────────
        if in_trade:
            d_      = entry['dir']
            ep      = entry['price']
            sl      = entry['sl']
            tp1     = entry['tp1']
            tp2     = entry['tp2']
            be_sl   = entry.get('be_sl', sl)
            tp1_hit = entry.get('tp1_hit', False)
            bars_in = i - entry['bar_idx']
            act_sl  = be_sl if tp1_hit else sl

            hit_tp1 = not tp1_hit and (
                (d_=='LONG' and cp>=tp1) or (d_=='SHORT' and cp<=tp1))
            hit_tp2 = tp1_hit and (
                (d_=='LONG' and cp>=tp2) or (d_=='SHORT' and cp<=tp2))
            hit_sl  = (d_=='LONG' and cp<=act_sl) or (d_=='SHORT' and cp>=act_sl)
            hit_time= bars_in >= TIME_STOP and not tp1_hit
            hit_sess= sess is None and entry['session'] is not None

            if hit_tp1:
                xp  = tp1
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity * pct * entry['sm'] * TP1_CLOSE
                equity += usd
                entry['tp1_hit'] = True
                entry['be_sl']   = ep
                entry['tp1_pnl'] = usd
                entry['sm']     *= TP2_CLOSE

            elif hit_tp2 or hit_sl or hit_time or hit_sess:
                xp  = tp2 if hit_tp2 else act_sl if hit_sl else cp
                xt  = 'TP2' if hit_tp2 else 'SL' if hit_sl else \
                      'TIME' if hit_time else 'SESS'
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity * pct * entry['sm']
                equity += usd
                total = usd + entry.get('tp1_pnl', 0)
                trades.append({
                    'entry_time': str(entry['time'])[:16],
                    'exit_time':  str(idx)[:16],
                    'dir':        d_,
                    'session':    entry['session'],
                    'level':      entry['level'],
                    'entry_px':   round(ep, 2),
                    'exit_px':    round(xp, 2),
                    'sl_dist%':   round((abs(ep-entry['sl'])/ep)*100, 4),
                    'tp1_hit':    entry.get('tp1_hit', False),
                    'total_usd':  round(total, 2),
                    'equity':     round(equity, 2),
                    'exit':       xt,
                    'bars':       bars_in,
                })
                eq_c.append(equity); eq_d.append(idx)
                in_trade = False

        # ── ENTRY ─────────────────────────────────────────────────────────────
        if not in_trade:
            if row['long_sweep']:
                sl_px  = row['low'] - atr * 0.3
                sl_pct = (cp - sl_px) / cp
                if sl_pct <= 0 or sl_pct > 0.015: continue
                tp1_px = cp + (cp - sl_px) * TP1_MULT
                tp2_px = cp + (cp - sl_px) * TP2_MULT
                oz     = (equity * RISK_PCT) / (cp * sl_pct + 1e-10)
                sm     = min((oz * cp) / equity, 5.0)

                # Find which level triggered
                level_name = _find_level(row, 'LONG')
                in_trade = True
                entry = dict(time=idx, bar_idx=i, price=cp, dir='LONG',
                             sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                             tp1_hit=False, be_sl=sl_px,
                             session=sess, level=level_name)

            elif row['short_sweep']:
                sl_px  = row['high'] + atr * 0.3
                sl_pct = (sl_px - cp) / cp
                if sl_pct <= 0 or sl_pct > 0.015: continue
                tp1_px = cp - (sl_px - cp) * TP1_MULT
                tp2_px = cp - (sl_px - cp) * TP2_MULT
                oz     = (equity * RISK_PCT) / (cp * sl_pct + 1e-10)
                sm     = min((oz * cp) / equity, 5.0)
                level_name = _find_level(row, 'SHORT')
                in_trade = True
                entry = dict(time=idx, bar_idx=i, price=cp, dir='SHORT',
                             sl=sl_px, tp1=tp1_px, tp2=tp2_px, sm=sm,
                             tp1_hit=False, be_sl=sl_px,
                             session=sess, level=level_name)

    if not trades:
        return None

    tdf  = pd.DataFrame(trades)
    wins = tdf[tdf['total_usd'] > 0]
    loss = tdf[tdf['total_usd'] <= 0]
    wr   = len(wins) / len(tdf)
    roi  = (equity / ACCOUNT - 1) * 100
    pf   = wins['total_usd'].sum() / abs(loss['total_usd'].sum()) \
           if len(loss) > 0 and loss['total_usd'].sum() != 0 else 99
    eq_s = pd.Series(eq_c, index=eq_d)
    mdd  = float(((eq_s-eq_s.expanding().max())/eq_s.expanding().max()*100).min())
    aw   = wins['total_usd'].mean() if len(wins) > 0 else 0
    al   = abs(loss['total_usd'].mean()) if len(loss) > 0 else 0
    be   = al / (aw + al) if (aw + al) > 0 else 0.5
    t, p = stats.ttest_1samp(tdf['total_usd'], 0) if len(tdf) > 5 else (0, 1)
    tp1r = tdf['tp1_hit'].mean() * 100

    tdf['month'] = pd.to_datetime(tdf['entry_time']).dt.to_period('M')
    mo = tdf.groupby('month').agg(
        n   =('total_usd','count'),
        pnl =('total_usd','sum'),
        wr  =('total_usd', lambda x:round((x>0).mean()*100, 1))
    ).reset_index()
    mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

    by_sess = tdf.groupby('session').apply(lambda x: pd.Series({
        'n':   len(x),
        'wr%': round((x['total_usd']>0).mean()*100, 1),
        'avg': round(x['total_usd'].mean(), 2),
        'pnl': round(x['total_usd'].sum(), 2),
    }), include_groups=False)

    by_level = tdf.groupby('level').apply(lambda x: pd.Series({
        'n':   len(x),
        'wr%': round((x['total_usd']>0).mean()*100, 1),
        'avg': round(x['total_usd'].mean(), 2),
        'pnl': round(x['total_usd'].sum(), 2),
    }), include_groups=False)

    by_dir = tdf.groupby('dir').apply(lambda x: pd.Series({
        'n':   len(x),
        'wr%': round((x['total_usd']>0).mean()*100, 1),
        'avg': round(x['total_usd'].mean(), 2),
        'pnl': round(x['total_usd'].sum(), 2),
    }), include_groups=False)

    return dict(
        label=label, tdf=tdf, mo=mo,
        by_sess=by_sess, by_level=by_level, by_dir=by_dir,
        n=len(tdf), wr=wr, roi=roi, pf=pf, mdd=mdd,
        equity=equity, be=be, t=t, p=p, aw=aw, al=al,
        tp1r=tp1r, pm=(mo['pnl']>0).sum(), tm=len(mo)
    )

def _find_level(row, direction):
    """Identify which key level triggered the sweep."""
    cp    = row['close']
    check = {
        'PrevHigh': row.get('prev_h', np.nan),
        'PrevLow':  row.get('prev_l', np.nan),
        'VWAP':     row.get('vwap',   np.nan),
        'RoundNum': row.get('rn',     np.nan),
        'SwingH':   row.get('swing_h',np.nan),
        'SwingL':   row.get('swing_l',np.nan),
    }
    best, best_dist = 'Unknown', 99
    for name, val in check.items():
        if pd.isna(val): continue
        d = abs(cp - val) / cp
        if d < best_dist:
            best_dist = d
            best = name
    return best

# ══════════════════════════════════════════════════════════════════════════════
#  PRINT RESULTS
# ══════════════════════════════════════════════════════════════════════════════

def print_result(r):
    if r is None:
        print("  No trades generated.")
        return
    print(f"\n  {'─'*64}")
    print(f"  {r['label']}")
    print(f"  {'─'*64}")
    print(f"  Period  : {r['tdf']['entry_time'].min()[:10]} → {r['tdf']['exit_time'].max()[:10]}")
    print(f"  Trades  : {r['n']}")
    print(f"  Win rate: {r['wr']*100:.1f}%  (break-even: {r['be']*100:.1f}%,  edge: {(r['wr']-r['be'])*100:+.1f}%)")
    print(f"  TP1 rate: {r['tp1r']:.1f}%")
    print(f"  PF      : {r['pf']:.2f}")
    print(f"  ROI     : {r['roi']:+.2f}%")
    print(f"  P&L     : ${r['equity']-ACCOUNT:+,.2f}")
    print(f"  Max DD  : {r['mdd']:.2f}%")
    print(f"  Avg win : +${r['aw']:,.2f}  |  Avg loss: -${r['al']:,.2f}")
    print(f"  +ve mo  : {r['pm']}/{r['tm']}")
    print(f"  p-value : {r['p']:.4f}  {'✓ SIGNIFICANT' if r['p']<0.05 else '— not significant'}")
    print(f"\n  By direction:")
    print(r['by_dir'].to_string())
    print(f"\n  By session:")
    print(r['by_sess'].to_string())
    print(f"\n  By level:")
    print(r['by_level'].to_string())
    print(f"\n  Monthly:")
    print(r['mo'][['month','n','pnl','wr','roi%']].to_string(index=False))

# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

results = {}

# BTC — try Binance, fall back to Twelve Data
btc_raw = load_binance_btc(days=365)
if btc_raw is None:
    btc_raw = load_btc_twelve()
if btc_raw is not None:
    print("\nComputing BTC levels and sweeps...")
    btc_df = compute_levels(btc_raw, BTC_GRID)
    btc_df['vol_ma'] = btc_df['volume'].rolling(20).mean()
    btc_df = detect_sweeps(btc_df, vol_mult=1.0)
    print(f"  BTC long sweeps : {btc_df['long_sweep'].sum()}")
    print(f"  BTC short sweeps: {btc_df['short_sweep'].sum()}")
    r_btc = run_backtest(btc_df, "BTC 15m — Liquidity Sweep")
    results['BTC'] = r_btc
    print_result(r_btc)
    if r_btc:
        r_btc['tdf'].to_csv("btc_sweep_trades.csv", index=False)

# Gold — Twelve Data has no real volume; skip volume filter (vol_mult=1.0)
gold_raw = load_gold_twelve()
if gold_raw is not None:
    print("\nComputing Gold levels and sweeps...")
    gold_df = compute_levels(gold_raw, GOLD_GRID)
    gold_df = detect_sweeps(gold_df, vol_mult=1.0)
    print(f"  Gold long sweeps : {gold_df['long_sweep'].sum()}")
    print(f"  Gold short sweeps: {gold_df['short_sweep'].sum()}")
    r_gold = run_backtest(gold_df, "Gold 15m — Liquidity Sweep")
    results['Gold'] = r_gold
    print_result(r_gold)
    if r_gold:
        r_gold['tdf'].to_csv("gold_sweep_trades.csv", index=False)

# Comparison
if len(results) >= 2:
    print(f"\n{'='*68}")
    print(f"  HEAD TO HEAD — BTC vs GOLD")
    print(f"{'='*68}")
    rb = results.get('BTC')
    rg = results.get('Gold')
    metrics = [
        ("Trades",        lambda r: str(r['n'])),
        ("Win rate",      lambda r: f"{r['wr']*100:.1f}%"),
        ("Edge vs BE",    lambda r: f"{(r['wr']-r['be'])*100:+.1f}%"),
        ("Profit factor", lambda r: f"{r['pf']:.2f}"),
        ("ROI",           lambda r: f"{r['roi']:+.2f}%"),
        ("P&L",           lambda r: f"${r['equity']-ACCOUNT:+,.0f}"),
        ("Max drawdown",  lambda r: f"{r['mdd']:.2f}%"),
        ("+ve months",    lambda r: f"{r['pm']}/{r['tm']}"),
        ("p-value",       lambda r: f"{r['p']:.4f}"),
        ("TP1 hit rate",  lambda r: f"{r['tp1r']:.1f}%"),
    ]
    print(f"\n  {'Metric':<22} {'BTC':>18} {'Gold':>18}")
    print(f"  {'─'*60}")
    for name, fn in metrics:
        bv = fn(rb) if rb else "—"
        gv = fn(rg) if rg else "—"
        print(f"  {name:<22} {bv:>18} {gv:>18}")

print(f"\n{'='*68}")
print("Saved: btc_sweep_trades.csv + gold_sweep_trades.csv")
print("Paste full results for analysis.")
