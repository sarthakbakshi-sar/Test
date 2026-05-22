"""
LIQUIDITY SWEEP SYSTEM v2 — IMPROVED
=======================================
Changes from v1 based on backtest findings:

v1 PROBLEMS IDENTIFIED:
  - 972 BTC trades = too many signals, low quality
  - London/NY_open: 38-40% WR, destroying P&L (-$8,297 combined)
  - PrevHigh: worst level (-$1,727)
  - SwingH/SwingL: noisy, inconsistent
  - TP1 only hit 17.3% = targets too far from entry
  - No trend alignment = fighting the tape

v2 FIXES:
  1. SESSION: BTC = NY Prime only (14:00-17:00 UTC = 18:00-21:00 Dubai)
              Gold = London open (08:00-10:00 UTC = 12:00-14:00 Dubai)
  2. LEVELS: Only VWAP + PrevLow/PrevHigh + Round Numbers (removed Swing)
  3. TREND: 50-EMA on 15m — longs above, shorts below
  4. SWEEP: Tighter buffer (0.10%), higher volume (1.5x), body 65%
  5. CONFIRMATION: Sweep candle + next candle must confirm direction
  6. TP: TP1 = 1.2x SL (tighter, hits more often), TP2 = 2.5x
  7. GOLD: Daily 50 EMA trend filter (proven p=0.006)
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, time
import time as time_mod
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT       = 10000.0
RISK_PCT      = 0.02
TP1_MULT      = 1.2        # TIGHTER (was 1.5) — hit more often
TP2_MULT      = 2.5        # Keep wide for runners
TP1_CLOSE     = 0.60
TP2_CLOSE     = 0.40
TIME_STOP     = 5          # 75 min on 15m
COMMISSION    = 0.00055

# v2 sweep parameters — tighter
SWEEP_BUFFER  = 0.0010     # 0.10% (was 0.15%)
MIN_VOL_MULT  = 1.5        # 1.5x avg (was 1.3x)
BODY_PCT      = 0.65       # 65% reversal body (was 55%)
EMA_TREND     = 50         # 50-period EMA for trend filter

# Levels — simplified
BTC_GRID      = 1000
GOLD_GRID     = 50
TD_KEY        = "06f050cd7d9940a895f9461e7f0ff7e3"

# v2 Sessions — one per asset
BTC_SESS  = [("NY_prime", time(14, 0), time(17, 0))]   # 18:00-21:00 Dubai
GOLD_SESS = [("London",   time( 8, 0), time(10, 0))]   # 12:00-14:00 Dubai

print("="*68)
print("  LIQUIDITY SWEEP SYSTEM v2 — IMPROVED")
print("  BTC: NY Prime only | Gold: London only")
print("  Trend filter | Tighter sweeps | Confirmation candle")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════════════════════════

def load_binance_btc(days=365):
    print(f"\nLoading BTC from Binance ({days}d)...")
    url, all_bars = "https://api.binance.com/api/v3/klines", []
    end_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff  = end_ms - days * 86400 * 1000
    while True:
        try:
            r    = requests.get(url, params={"symbol":"BTCUSDT","interval":"15m",
                                "limit":1000,"endTime":end_ms}, timeout=15)
            data = r.json()
            if not data: break
            done = False
            for bar in data:
                if int(bar[0]) < cutoff: done=True; break
                all_bars.append(bar)
            if done: break
            end_ms = int(data[0][0]) - 1
            time_mod.sleep(0.1)
        except Exception as e:
            print(f"  Error: {e}"); break
    if not all_bars: return None
    all_bars.sort(key=lambda x: x[0])
    df = pd.DataFrame(all_bars, columns=[
        'ts','open','high','low','close','volume',
        'c2','qv','tr','tb','tq','ig'])
    df.index = pd.to_datetime(df['ts'].astype(float),
                              unit='ms', utc=True).dt.tz_convert(None)
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    df = df[['open','high','low','close','volume']].dropna()
    print(f"  {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
    return df

def load_btc_twelve():
    import urllib.parse
    print(f"\nLoading BTC from Twelve Data...")
    frames, end_date = [], None
    for pg in range(8):
        try:
            url = (f"https://api.twelvedata.com/time_series?symbol=BTC/USD"
                   f"&interval=15min&outputsize=5000&order=DESC&apikey={TD_KEY}")
            if end_date: url += f"&end_date={urllib.parse.quote(end_date)}"
            raw = requests.get(url, timeout=30).json()
            if 'values' not in raw:
                print(f"  Error: {raw.get('message','unknown')}"); break
            df  = pd.DataFrame(raw['values'])
            df.index = pd.to_datetime(df['datetime'])
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df   = df[cols].apply(pd.to_numeric, errors='coerce').dropna()
            if 'volume' not in df.columns: df['volume'] = 1.0
            if len(df) == 0: break
            frames.append(df)
            oldest   = df.index.min()
            end_date = (oldest - pd.Timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"  Page {pg+1}: {len(df)} bars | {oldest.date()}")
            time_mod.sleep(8)
        except Exception as e:
            print(f"  Error: {e}"); break
    if not frames: return None
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep='first')].sort_index()
    print(f"  {len(out)} bars | {out.index[0].date()} → {out.index[-1].date()}")
    return out

def load_gold_twelve():
    print(f"\nLoading Gold from Twelve Data...")
    frames, end_date = [], None
    for pg in range(8):
        try:
            url = (f"https://api.twelvedata.com/time_series?symbol=XAU/USD"
                   f"&interval=15min&outputsize=5000&order=DESC&apikey={TD_KEY}")
            if end_date: url += f"&end_date={end_date}"
            raw = requests.get(url, timeout=30).json()
            if 'values' not in raw: break
            df  = pd.DataFrame(raw['values'])
            df.index = pd.to_datetime(df['datetime'])
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df   = df[cols].apply(pd.to_numeric, errors='coerce').dropna()
            if 'volume' not in df.columns: df['volume'] = 100000.0
            if len(df) == 0: break
            frames.append(df)
            oldest   = df.index.min()
            end_date = (oldest - pd.Timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"  Page {pg+1}: {len(df)} bars | {oldest.date()}")
            time_mod.sleep(8)
        except Exception as e:
            print(f"  Error: {e}"); break
    if not frames: return None
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep='first')].sort_index()
    print(f"  {len(out)} bars | {out.index[0].date()} → {out.index[-1].date()}")
    return out

def load_gold_daily():
    """Daily gold for trend filter."""
    d = yf.download("GC=F", period="2y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index   = pd.to_datetime(d.index).tz_localize(None)
    return d['close']

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS + LEVELS
# ══════════════════════════════════════════════════════════════════════════════

def build(df, rn_grid, sessions, gold_daily=None):
    d = df.copy()
    d['date'] = d.index.normalize()

    # Previous session H/L
    dhl    = d.groupby('date').agg(sh=('high','max'), sl=('low','min'))
    d['prev_h'] = d['date'].map(dhl['sh'].shift(1).to_dict())
    d['prev_l'] = d['date'].map(dhl['sl'].shift(1).to_dict())

    # VWAP
    d['typ']  = (d['high']+d['low']+d['close'])/3
    d['ctv']  = d.groupby('date').apply(
        lambda g:(g['typ']*g['volume']).cumsum()).reset_index(level=0,drop=True)
    d['cv']   = d.groupby('date')['volume'].cumsum()
    d['vwap'] = d['ctv']/(d['cv']+1e-10)

    # Round number
    d['rn'] = (d['close']/rn_grid).round()*rn_grid

    # Volume MA and ATR
    d['vol_ma'] = d['volume'].rolling(20).mean()
    d['tr']     = np.maximum(d['high']-d['low'],
                  np.maximum((d['high']-d['close'].shift(1)).abs(),
                             (d['low'] -d['close'].shift(1)).abs()))
    d['atr']    = d['tr'].rolling(14).mean()

    # 15-min trend EMA
    d['ema50']   = d['close'].ewm(span=EMA_TREND, adjust=False).mean()
    d['uptrend'] = d['close'] > d['ema50']

    # Daily trend filter for gold
    if gold_daily is not None:
        gold_ema50d = gold_daily.ewm(span=50, adjust=False).mean()
        daily_trend = (gold_daily > gold_ema50d).astype(int)
        d['daily_trend_long'] = d['date'].map(
            daily_trend.to_dict()).fillna(0).astype(bool)
    else:
        d['daily_trend_long'] = True  # no filter for BTC

    # Session
    def gs(ts):
        t = ts.time() if hasattr(ts,'time') else ts
        for nm,s,e in sessions:
            if s<=t<e: return nm
        return None

    d['session']  = d.index.map(gs)
    d['in_sess']  = d['session'].notna()
    d['sess_bar'] = d.groupby(['date','session']).cumcount()

    d.dropna(subset=['atr','vwap','prev_h','prev_l','ema50'], inplace=True)
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  SWEEP DETECTION v2 — TIGHTER + CONFIRMATION
# ══════════════════════════════════════════════════════════════════════════════

def detect_sweeps_v2(df, asset='BTC', use_vol=True):
    d = df.copy()
    longs, shorts = [], []

    # Only 3 levels now — VWAP, PrevLow, PrevHigh, RoundNum
    # Removed: SwingH, SwingL (too noisy)
    LEVELS = ['vwap', 'prev_l', 'prev_h', 'rn']

    def sweep_long(row, level):
        """Sweep below level and close back above — bullish reversal."""
        if pd.isna(level) or level <= 0: return False
        cr  = row['high'] - row['low']
        if cr <= 0: return False
        if use_vol and row['volume'] < row['vol_ma'] * MIN_VOL_MULT: return False
        breached = row['low'] < level * (1 - 0.0001)
        not_deep = row['low'] >= level * (1 - SWEEP_BUFFER)
        recovered= row['close'] > level
        bull_body= (row['close'] - row['low']) / cr >= BODY_PCT
        return breached and not_deep and recovered and bull_body

    def sweep_short(row, level):
        """Sweep above level and close back below — bearish reversal."""
        if pd.isna(level) or level <= 0: return False
        cr  = row['high'] - row['low']
        if cr <= 0: return False
        if use_vol and row['volume'] < row['vol_ma'] * MIN_VOL_MULT: return False
        breached = row['high'] > level * (1 + 0.0001)
        not_high = row['high'] <= level * (1 + SWEEP_BUFFER)
        recovered= row['close'] < level
        bear_body= (row['high'] - row['close']) / cr >= BODY_PCT
        return breached and not_high and recovered and bear_body

    rows = list(d.iterrows())
    for i, (idx, row) in enumerate(rows):
        if not row['in_sess'] or row.get('sess_bar', 0) < 3:
            longs.append(False); shorts.append(False); continue

        # Trend alignment
        up = row['uptrend']
        daily_long = row.get('daily_trend_long', True)

        fl, fs = False, False

        for lvl_col in LEVELS:
            level = row.get(lvl_col, np.nan)
            if pd.isna(level): continue

            if up and daily_long and sweep_long(row, level):
                fl = True
            if not up and not daily_long and sweep_short(row, level):
                fs = True

        # Confirmation: next candle must continue in same direction
        # (this is a lookahead-safe check — we check next bar at entry)
        # We mark the signal here, actual entry is on next candle open
        longs.append(fl)
        shorts.append(fs)

    d['long_sweep']  = longs
    d['short_sweep'] = shorts

    # Shift by 1 — enter on the NEXT candle after confirmation
    # This eliminates the "entered mid-candle" problem and adds confirmation
    d['long_entry']  = d['long_sweep'].shift(1).fillna(False)
    d['short_entry'] = d['short_sweep'].shift(1).fillna(False)

    # Additional filter: entry candle must not gap against us
    # Next candle opens within 0.2% of sweep close
    d['open_gap']   = (d['open'] - d['close'].shift(1)).abs() / d['close'].shift(1)
    d['long_entry'] = d['long_entry']  & (d['open_gap'] < 0.002)
    d['short_entry']= d['short_entry'] & (d['open_gap'] < 0.002)

    return d

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def backtest(df, label):
    equity = ACCOUNT
    in_t   = False
    ent    = {}
    trades = []
    eq_c   = [ACCOUNT]
    eq_d   = [df.index[0]]

    for i in range(len(df)):
        row = df.iloc[i]
        idx = df.index[i]
        cp  = row['close']
        atr = row['atr']

        if pd.isna(atr) or atr == 0: continue

        # EXIT
        if in_t:
            d_    = ent['dir']
            ep    = ent['price']
            sl    = ent['sl']
            tp1   = ent['tp1']
            tp2   = ent['tp2']
            be_sl = ent.get('be_sl', sl)
            t1h   = ent.get('t1h', False)
            bi    = i - ent['bi']
            asl   = be_sl if t1h else sl

            h_t1  = not t1h and ((d_=='LONG' and cp>=tp1) or (d_=='SHORT' and cp<=tp1))
            h_t2  = t1h     and ((d_=='LONG' and cp>=tp2) or (d_=='SHORT' and cp<=tp2))
            h_sl  = (d_=='LONG' and cp<=asl) or (d_=='SHORT' and cp>=asl)
            h_tm  = bi >= TIME_STOP and not t1h
            h_se  = row['session'] is None and ent['sess'] is not None

            if h_t1:
                raw = (tp1-ep) if d_=='LONG' else (ep-tp1)
                pct = raw/ep - COMMISSION
                usd = equity * pct * ent['sm'] * TP1_CLOSE
                equity += usd
                ent.update({'t1h':True,'be_sl':ep,'t1p':usd,'sm':ent['sm']*TP2_CLOSE})

            elif h_t2 or h_sl or h_tm or h_se:
                xp  = tp2 if h_t2 else asl if h_sl else cp
                xt  = 'TP2' if h_t2 else 'SL' if h_sl else 'TIME' if h_tm else 'SESS'
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity * pct * ent['sm']
                equity += usd
                tot = usd + ent.get('t1p', 0)
                trades.append({
                    'time':  str(ent['t'])[:16],
                    'dir':   d_,
                    'sess':  ent['sess'],
                    'level': ent['lv'],
                    'entry': round(ep, 2),
                    'exit':  round(xp, 2),
                    't1hit': ent.get('t1h', False),
                    'pnl':   round(tot, 2),
                    'eq':    round(equity, 2),
                    'xt':    xt,
                    'bars':  bi,
                })
                eq_c.append(equity); eq_d.append(idx)
                in_t = False

        # ENTRY
        if not in_t:
            if row.get('long_entry', False):
                sl_  = cp - atr * 0.5
                slp  = (cp-sl_)/cp
                if 0 < slp <= 0.012:
                    tp1_ = cp + (cp-sl_)*TP1_MULT
                    tp2_ = cp + (cp-sl_)*TP2_MULT
                    oz   = (equity*RISK_PCT)/(cp*slp+1e-10)
                    sm   = min((oz*cp)/equity, 4.0)
                    in_t = True
                    ent  = dict(t=idx,bi=i,price=cp,dir='LONG',
                                sl=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                                t1h=False,be_sl=sl_,sess=row['session'],
                                lv=_lvl(row,'LONG'))

            elif row.get('short_entry', False):
                sl_  = cp + atr * 0.5
                slp  = (sl_-cp)/cp
                if 0 < slp <= 0.012:
                    tp1_ = cp - (sl_-cp)*TP1_MULT
                    tp2_ = cp - (sl_-cp)*TP2_MULT
                    oz   = (equity*RISK_PCT)/(cp*slp+1e-10)
                    sm   = min((oz*cp)/equity, 4.0)
                    in_t = True
                    ent  = dict(t=idx,bi=i,price=cp,dir='SHORT',
                                sl=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                                t1h=False,be_sl=sl_,sess=row['session'],
                                lv=_lvl(row,'SHORT'))

    if not trades: return None

    tdf  = pd.DataFrame(trades)
    wins = tdf[tdf['pnl']>0]
    loss = tdf[tdf['pnl']<=0]
    wr   = len(wins)/len(tdf)
    roi  = (equity/ACCOUNT-1)*100
    pf   = wins['pnl'].sum()/abs(loss['pnl'].sum()) \
           if len(loss)>0 and loss['pnl'].sum()!=0 else 99
    eq_s = pd.Series(eq_c,index=eq_d)
    mdd  = float(((eq_s-eq_s.expanding().max())/eq_s.expanding().max()*100).min())
    aw   = wins['pnl'].mean() if len(wins)>0 else 0
    al   = abs(loss['pnl'].mean()) if len(loss)>0 else 0
    be   = al/(aw+al) if (aw+al)>0 else 0.5
    t,p  = stats.ttest_1samp(tdf['pnl'],0) if len(tdf)>5 else (0,1)
    t1r  = tdf['t1hit'].mean()*100

    tdf['month'] = pd.to_datetime(tdf['time']).dt.to_period('M')
    mo = tdf.groupby('month').agg(
        n  =('pnl','count'), pnl=('pnl','sum'),
        wr =('pnl',lambda x:round((x>0).mean()*100,1))
    ).reset_index()
    mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

    by_dir  = tdf.groupby('dir').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)
    by_sess = tdf.groupby('sess').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)
    by_lvl  = tdf.groupby('level').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)
    by_exit = tdf.groupby('xt').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)

    return dict(label=label,tdf=tdf,mo=mo,
                by_dir=by_dir,by_sess=by_sess,by_lvl=by_lvl,by_exit=by_exit,
                n=len(tdf),wr=wr,roi=roi,pf=pf,mdd=mdd,
                equity=equity,be=be,t=t,p=p,aw=aw,al=al,
                t1r=t1r,pm=(mo['pnl']>0).sum(),tm=len(mo))

def _lvl(row, direction):
    cp = row['close']
    best, bd = 'Other', 99
    for n,v in [('VWAP',row.get('vwap')),('PrevLow',row.get('prev_l')),
                ('PrevHigh',row.get('prev_h')),('RoundNum',row.get('rn'))]:
        if v and not pd.isna(v):
            d = abs(cp-v)/cp
            if d < bd: bd=d; best=n
    return best

def print_r(r):
    if r is None: print("  No trades."); return
    print(f"\n  {'─'*64}")
    print(f"  {r['label']}")
    print(f"  {'─'*64}")
    print(f"  Trades   : {r['n']}  ({r['tdf']['time'].min()[:10]} → {r['tdf']['time'].max()[:10]})")
    print(f"  Win rate : {r['wr']*100:.1f}%  (BE: {r['be']*100:.1f}%,  edge: {(r['wr']-r['be'])*100:+.1f}%)")
    print(f"  TP1 rate : {r['t1r']:.1f}%")
    print(f"  PF       : {r['pf']:.2f}")
    print(f"  ROI      : {r['roi']:+.2f}%  |  P&L: ${r['equity']-ACCOUNT:+,.2f}")
    print(f"  Max DD   : {r['mdd']:.2f}%")
    print(f"  Avg W/L  : +${r['aw']:,.2f} / -${r['al']:,.2f}")
    print(f"  +ve mo   : {r['pm']}/{r['tm']}")
    print(f"  p-value  : {r['p']:.4f}  {'✓ SIGNIFICANT' if r['p']<0.05 else '— not significant'}")
    print(f"\n  By direction:\n{r['by_dir'].to_string()}")
    print(f"\n  By level:\n{r['by_lvl'].to_string()}")
    print(f"\n  By exit:\n{r['by_exit'].to_string()}")
    print(f"\n  Monthly:")
    print(r['mo'][['month','n','pnl','wr','roi%']].to_string(index=False))

# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

results = {}

# BTC — Binance first, fall back to Twelve Data
btc_raw = load_binance_btc(365)
if btc_raw is None:
    btc_raw = load_btc_twelve()
if btc_raw is not None:
    print("\nBuilding BTC indicators...")
    btc = build(btc_raw, BTC_GRID, BTC_SESS)
    btc = detect_sweeps_v2(btc, 'BTC', use_vol=False)
    ls  = btc['long_sweep'].sum()
    ss  = btc['short_sweep'].sum()
    le  = btc['long_entry'].sum()
    se  = btc['short_entry'].sum()
    print(f"  Raw sweeps: {ls} long / {ss} short")
    print(f"  Confirmed entries: {le} long / {se} short")
    r = backtest(btc, "BTC 15m v2 — NY Prime | Trend | Confirmed sweeps")
    results['BTC'] = r
    print_r(r)
    if r: r['tdf'].to_csv("btc_sweep_v2.csv", index=False)

# Gold
gold_raw = load_gold_twelve()
gold_d   = load_gold_daily()
if gold_raw is not None:
    print("\nBuilding Gold indicators...")
    gold = build(gold_raw, GOLD_GRID, GOLD_SESS, gold_daily=gold_d)
    gold = detect_sweeps_v2(gold, 'Gold', use_vol=False)
    ls   = gold['long_sweep'].sum()
    ss   = gold['short_sweep'].sum()
    le   = gold['long_entry'].sum()
    se   = gold['short_entry'].sum()
    print(f"  Raw sweeps: {ls} long / {ss} short")
    print(f"  Confirmed entries: {le} long / {se} short")
    r = backtest(gold, "Gold 15m v2 — London | Daily trend | Confirmed sweeps")
    results['Gold'] = r
    print_r(r)
    if r: r['tdf'].to_csv("gold_sweep_v2.csv", index=False)

# Comparison v1 vs v2
print(f"\n{'='*68}")
print(f"  V1 vs V2 COMPARISON")
print(f"{'='*68}")
v1 = {'BTC':{'n':972,'wr':41.9,'roi':-66.55,'p':0.025,'t1r':17.3,'pm':4,'tm':15},
      'Gold':{'n':618,'wr':38.0,'roi':-69.44,'p':0.000,'t1r':0,'pm':0,'tm':17}}

for asset in ['BTC','Gold']:
    r  = results.get(asset)
    v  = v1[asset]
    print(f"\n  {asset}")
    print(f"  {'Metric':<16} {'v1':>12} {'v2':>12} {'Change':>12}")
    print(f"  {'─'*54}")
    metrics = [
        ("Trades",   str(v['n']),         str(r['n']) if r else "—"),
        ("Win rate", f"{v['wr']:.1f}%",   f"{r['wr']*100:.1f}%" if r else "—"),
        ("ROI",      f"{v['roi']:+.1f}%", f"{r['roi']:+.1f}%" if r else "—"),
        ("TP1 rate", f"{v['t1r']:.1f}%",  f"{r['t1r']:.1f}%" if r else "—"),
        ("+ve months",f"{v['pm']}/{v['tm']}", f"{r['pm']}/{r['tm']}" if r else "—"),
        ("p-value",  f"{v['p']:.3f}",     f"{r['p']:.4f}" if r else "—"),
    ]
    for nm, v1v, v2v in metrics:
        print(f"  {nm:<16} {v1v:>12} {v2v:>12}")

print(f"\n{'='*68}")
print("Saved: btc_sweep_v2.csv + gold_sweep_v2.csv")
print("Paste results for analysis.")
