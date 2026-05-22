"""
v5 COMPARISON — VWAP-Only Sweeps vs Opening Range Breakout
============================================================
Two completely different methodologies, head-to-head on same BTC data.

OPTION A — VWAP-Only Sweep (strip v2 to its edge):
  - NY Prime full 3 hours (14:00-17:00 UTC)
  - ONLY VWAP sweeps (no PrevH/L, no RoundNum)
  - 50 EMA trend filter
  - Confirmation candle
  - v2 parameters that worked

OPTION C — Opening Range Breakout (ORB):
  - Research-validated strategy (Crabel, Zarattini et al)
  - Opening Range = first 15 min of NY session (13:30-13:45 UTC)
  - Trade breakouts above ORH or below ORL with volume confirmation
  - Stop on opposite side of range
  - Target: 2x range size
  - Session window: 13:45-17:00 UTC

Same data. Same period. Different methodologies. May the best edge win.

Run: python v5_compare.py
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, time
import time as time_mod
import warnings
warnings.filterwarnings('ignore')

# ── COMMON CONFIG ─────────────────────────────────────────────────────────────
ACCOUNT      = 10000.0
RISK_PCT     = 0.02
COMMISSION   = 0.00055

print("="*68)
print("  v5 — VWAP SWEEPS vs OPENING RANGE BREAKOUT")
print("  Same data, head-to-head comparison")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════════════════════════

def load_btc(days=365):
    print(f"\nLoading BTC ({days}d)...")
    url, all_bars = "https://api.binance.com/api/v3/klines", []
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff = end_ms - days * 86400 * 1000
    while True:
        try:
            r = requests.get(url, params={
                "symbol":"BTCUSDT","interval":"15m",
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
        except Exception as e: print(f"  Err: {e}"); break
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
    TD_KEY = "06f050cd7d9940a895f9461e7f0ff7e3"
    print("Loading BTC from Twelve Data (fallback)...")
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

# ══════════════════════════════════════════════════════════════════════════════
#  SHARED INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def add_common_indicators(df):
    d = df.copy()
    d['date'] = d.index.normalize()
    # ATR
    d['tr']  = np.maximum(d['high']-d['low'],
               np.maximum((d['high']-d['close'].shift(1)).abs(),
                          (d['low'] -d['close'].shift(1)).abs()))
    d['atr'] = d['tr'].rolling(14).mean()
    # Volume MA
    d['vol_ma']    = d['volume'].rolling(20).mean()
    d['vol_ratio'] = d['volume'] / (d['vol_ma'] + 1e-10)
    # EMA50 for trend
    d['ema50']   = d['close'].ewm(span=50, adjust=False).mean()
    d['uptrend'] = d['close'] > d['ema50']
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  OPTION A — VWAP-ONLY SWEEPS
# ══════════════════════════════════════════════════════════════════════════════

A_SWEEP_BUFFER = 0.0015
A_MIN_VOL      = 1.3
A_BODY         = 0.55
A_TP1_MULT     = 1.2
A_TP2_MULT     = 2.5
A_TP1_CLOSE    = 0.60
A_TP2_CLOSE    = 0.40
A_TIME_STOP    = 5
A_SESS_START   = time(14, 0)   # NY Prime 14:00-17:00 UTC
A_SESS_END     = time(17, 0)

def build_option_a(df):
    d = add_common_indicators(df)
    # Session VWAP
    d['typ']  = (d['high']+d['low']+d['close'])/3
    d['ctv']  = d.groupby('date').apply(
        lambda g:(g['typ']*g['volume']).cumsum()).reset_index(level=0,drop=True)
    d['cv']   = d.groupby('date')['volume'].cumsum()
    d['vwap'] = d['ctv']/(d['cv']+1e-10)
    # Session
    d['in_sess']  = d.index.map(lambda ts: A_SESS_START <= ts.time() < A_SESS_END)
    d['sess_bar'] = d[d['in_sess']].groupby('date').cumcount()
    d['sess_bar'] = d['sess_bar'].reindex(d.index)
    d.dropna(subset=['atr','vwap','ema50'], inplace=True)
    return d

def detect_option_a(df, skip_vol=False):
    d = df.copy()
    longs, shorts = [], []
    for i, (idx, row) in enumerate(d.iterrows()):
        if not row['in_sess'] or pd.isna(row.get('sess_bar')) or row['sess_bar'] < 4:
            longs.append(False); shorts.append(False); continue

        vwap = row['vwap']
        cr   = row['high'] - row['low']
        if cr <= 0 or pd.isna(vwap):
            longs.append(False); shorts.append(False); continue
        if not skip_vol and row['volume'] < row['vol_ma']*A_MIN_VOL:
            longs.append(False); shorts.append(False); continue

        # LONG: sweep below VWAP, close back above, only in uptrend
        long_swept = (row['low']  < vwap*(1-0.0001)) and (row['low'] >= vwap*(1-A_SWEEP_BUFFER))
        long_back  = row['close'] > vwap
        long_body  = (row['close'] - row['low']) / cr >= A_BODY
        is_long    = row['uptrend'] and long_swept and long_back and long_body

        # SHORT: sweep above VWAP, close back below, only in downtrend
        short_swept = (row['high'] > vwap*(1+0.0001)) and (row['high'] <= vwap*(1+A_SWEEP_BUFFER))
        short_back  = row['close'] < vwap
        short_body  = (row['high'] - row['close']) / cr >= A_BODY
        is_short    = (not row['uptrend']) and short_swept and short_back and short_body

        longs.append(is_long); shorts.append(is_short)

    d['long_sweep']  = longs
    d['short_sweep'] = shorts
    # Enter on next candle
    d['long_entry']  = d['long_sweep'].shift(1).fillna(False)
    d['short_entry'] = d['short_sweep'].shift(1).fillna(False)
    d['open_gap']    = (d['open']-d['close'].shift(1)).abs()/d['close'].shift(1)
    d['long_entry']  = d['long_entry']  & (d['open_gap'] < 0.002)
    d['short_entry'] = d['short_entry'] & (d['open_gap'] < 0.002)
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  OPTION C — OPENING RANGE BREAKOUT (Crabel / Zarattini method)
# ══════════════════════════════════════════════════════════════════════════════

C_OR_BARS     = 2          # Opening range = first 2 bars (30 min)
C_VOL_MIN     = 1.3        # Volume confirmation on breakout candle
C_TP_RANGE_X  = 2.0        # Target = 2x opening range
C_SL_BUFFER   = 0.001      # 0.1% beyond OR for SL
C_TP1_PCT     = 0.5        # Take half at 1x range
C_TP2_PCT     = 0.5        # Rest at 2x range
C_TIME_STOP   = 8          # 2 hours max
C_SESS_START  = time(13, 30)   # NY open 13:30 UTC
C_OR_END      = time(14, 0)    # OR = 13:30-14:00 UTC (30 min)
C_SESS_END    = time(17, 0)    # 14:00-17:00 = trading window after OR

def build_option_c(df):
    d = add_common_indicators(df)
    # Identify OR bars (first 2 bars of session)
    d['is_or_bar']     = d.index.map(lambda ts: C_SESS_START <= ts.time() < C_OR_END)
    d['is_trade_zone'] = d.index.map(lambda ts: C_OR_END <= ts.time() < C_SESS_END)

    # Per-day OR high and low
    def compute_or(grp):
        or_bars = grp[grp['is_or_bar']]
        if len(or_bars) == 0:
            return pd.DataFrame({'or_h': np.nan, 'or_l': np.nan, 'or_range': np.nan},
                                index=grp.index)
        or_h = or_bars['high'].max()
        or_l = or_bars['low'].min()
        return pd.DataFrame({
            'or_h':     or_h,
            'or_l':     or_l,
            'or_range': or_h - or_l,
        }, index=grp.index)

    or_df = d.groupby('date', group_keys=False).apply(compute_or)
    d['or_h']     = or_df['or_h']
    d['or_l']     = or_df['or_l']
    d['or_range'] = or_df['or_range']

    # Daily volume average for breakout confirmation
    d.dropna(subset=['atr','or_h','or_l','or_range'], inplace=True)
    return d

def detect_option_c(df, skip_vol=False):
    """
    ORB Entry Rules:
    - Wait for first 15-min candle CLOSE in trade zone above OR_H = LONG
    - Or first candle CLOSE below OR_L = SHORT
    - Volume above 1.3x average
    - OR range must be reasonable (not too narrow, not too wide)
    - One trade per day max
    """
    d = df.copy()
    longs, shorts = [], []
    daily_traded = {}   # track if already traded today

    for i, (idx, row) in enumerate(d.iterrows()):
        date = row['date']

        if date in daily_traded:
            longs.append(False); shorts.append(False); continue

        if not row.get('is_trade_zone', False):
            longs.append(False); shorts.append(False); continue

        or_h, or_l, rng = row['or_h'], row['or_l'], row['or_range']
        if pd.isna(or_h) or pd.isna(or_l) or rng <= 0:
            longs.append(False); shorts.append(False); continue

        # OR range filter: between 0.2% and 1.5% of price (not too tight, not too wild)
        rng_pct = rng / row['close']
        if rng_pct < 0.002 or rng_pct > 0.015:
            longs.append(False); shorts.append(False); continue

        # Volume confirmation
        if not skip_vol and row['volume'] < row['vol_ma'] * C_VOL_MIN:
            longs.append(False); shorts.append(False); continue

        # LONG: close above OR high
        if row['close'] > or_h * (1 + 0.0001):
            longs.append(True); shorts.append(False)
            daily_traded[date] = 'LONG'
            continue

        # SHORT: close below OR low
        if row['close'] < or_l * (1 - 0.0001):
            longs.append(False); shorts.append(True)
            daily_traded[date] = 'SHORT'
            continue

        longs.append(False); shorts.append(False)

    d['long_entry']  = longs
    d['short_entry'] = shorts
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  GENERIC BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def backtest_generic(df, label, system='A'):
    equity = ACCOUNT
    in_t   = False
    ent    = {}
    trades = []
    eq_c   = [ACCOUNT]; eq_d = [df.index[0]]

    for i in range(len(df)):
        row = df.iloc[i]
        idx = df.index[i]
        cp  = row['close']
        atr = row['atr']
        if pd.isna(atr) or atr==0: continue

        if in_t:
            d_,ep = ent['dir'], ent['price']
            sl,tp1,tp2 = ent['sl'], ent['tp1'], ent['tp2']
            be_sl = ent.get('be_sl', sl)
            t1h   = ent.get('t1h', False)
            bi    = i - ent['bi']
            asl   = be_sl if t1h else sl

            h_t1 = not t1h and ((d_=='LONG' and row['high']>=tp1) or (d_=='SHORT' and row['low']<=tp1))
            h_t2 = t1h    and ((d_=='LONG' and row['high']>=tp2) or (d_=='SHORT' and row['low']<=tp2))
            h_sl = (d_=='LONG' and row['low']<=asl) or (d_=='SHORT' and row['high']>=asl)
            time_lim = A_TIME_STOP if system=='A' else C_TIME_STOP
            h_tm = bi >= time_lim and not t1h

            # Session-end exit
            sess_end = C_SESS_END if system=='C' else A_SESS_END
            h_se = row.name.time() >= sess_end

            if h_t1:
                xp = tp1
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                close_pct = A_TP1_CLOSE if system=='A' else C_TP1_PCT
                usd = equity * pct * ent['sm'] * close_pct
                equity += usd
                ent.update({'t1h':True,'be_sl':ep,'t1p':usd,
                            'sm':ent['sm']*(1-close_pct)})

            elif h_t2 or h_sl or h_tm or h_se:
                xp  = tp2 if h_t2 else asl if h_sl else cp
                xt  = 'TP2' if h_t2 else 'SL' if h_sl else 'TIME' if h_tm else 'SESS'
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity * pct * ent['sm']
                equity += usd
                tot = usd + ent.get('t1p', 0)
                trades.append({
                    'time':str(ent['t'])[:16],'dir':d_,
                    'entry':round(ep,2),'exit':round(xp,2),
                    't1hit':ent.get('t1h',False),'pnl':round(tot,2),
                    'eq':round(equity,2),'xt':xt,'bars':bi,
                })
                eq_c.append(equity); eq_d.append(idx)
                in_t = False

        if not in_t:
            if row.get('long_entry', False):
                # Different SL/TP based on system
                if system == 'A':
                    sl_  = cp - atr*0.5
                    tp1_ = cp + atr*A_TP1_MULT
                    tp2_ = cp + atr*A_TP2_MULT
                else:  # C — ORB
                    sl_  = row['or_l'] * (1 - C_SL_BUFFER)
                    rng  = row['or_range']
                    tp1_ = cp + rng * 1.0          # TP1 at 1x range
                    tp2_ = cp + rng * C_TP_RANGE_X # TP2 at 2x range

                slp = (cp-sl_)/cp
                if 0 < slp <= 0.025:
                    oz = (equity*RISK_PCT)/(cp*slp+1e-10)
                    sm = min((oz*cp)/equity, 4.0)
                    in_t = True
                    ent = dict(t=idx,bi=i,price=cp,dir='LONG',
                               sl=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                               t1h=False,be_sl=sl_)

            elif row.get('short_entry', False):
                if system == 'A':
                    sl_  = cp + atr*0.5
                    tp1_ = cp - atr*A_TP1_MULT
                    tp2_ = cp - atr*A_TP2_MULT
                else:
                    sl_  = row['or_h'] * (1 + C_SL_BUFFER)
                    rng  = row['or_range']
                    tp1_ = cp - rng * 1.0
                    tp2_ = cp - rng * C_TP_RANGE_X

                slp = (sl_-cp)/cp
                if 0 < slp <= 0.025:
                    oz = (equity*RISK_PCT)/(cp*slp+1e-10)
                    sm = min((oz*cp)/equity, 4.0)
                    in_t = True
                    ent = dict(t=idx,bi=i,price=cp,dir='SHORT',
                               sl=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                               t1h=False,be_sl=sl_)

    if not trades: return None
    tdf  = pd.DataFrame(trades)
    wins = tdf[tdf['pnl']>0]; loss = tdf[tdf['pnl']<=0]
    wr   = len(wins)/len(tdf)
    roi  = (equity/ACCOUNT-1)*100
    pf   = wins['pnl'].sum()/abs(loss['pnl'].sum()) if len(loss)>0 and loss['pnl'].sum()!=0 else 99
    eq_s = pd.Series(eq_c,index=eq_d)
    mdd  = float(((eq_s-eq_s.expanding().max())/eq_s.expanding().max()*100).min())
    aw   = wins['pnl'].mean() if len(wins)>0 else 0
    al   = abs(loss['pnl'].mean()) if len(loss)>0 else 0
    be   = al/(aw+al) if (aw+al)>0 else 0.5
    t,p  = stats.ttest_1samp(tdf['pnl'],0) if len(tdf)>5 else (0,1)
    t1r  = tdf['t1hit'].mean()*100

    tdf['month'] = pd.to_datetime(tdf['time']).dt.to_period('M')
    mo = tdf.groupby('month').agg(
        n=('pnl','count'), pnl=('pnl','sum'),
        wr=('pnl', lambda x: round((x>0).mean()*100, 1))
    ).reset_index()
    mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

    by_dir = tdf.groupby('dir').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'pnl$':round(x['pnl'].sum(),2)
    }), include_groups=False)
    by_exit = tdf.groupby('xt').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'pnl$':round(x['pnl'].sum(),2)
    }), include_groups=False)

    return dict(label=label,tdf=tdf,mo=mo,by_dir=by_dir,by_exit=by_exit,
                n=len(tdf),wr=wr,roi=roi,pf=pf,mdd=mdd,equity=equity,
                be=be,t=t,p=p,aw=aw,al=al,t1r=t1r,
                pm=(mo['pnl']>0).sum(),tm=len(mo))

# ══════════════════════════════════════════════════════════════════════════════
#  RUN BOTH
# ══════════════════════════════════════════════════════════════════════════════

skip_vol = False
btc = load_btc(365)
if btc is None:
    btc = load_btc_twelve()
    skip_vol = True
if btc is None: print("ERROR: No data"); exit()

print("\n── OPTION A: VWAP-only Sweeps ───────────────────")
df_a = build_option_a(btc)
df_a = detect_option_a(df_a, skip_vol=skip_vol)
print(f"  Signals: {df_a['long_entry'].sum()} long / {df_a['short_entry'].sum()} short")
r_a = backtest_generic(df_a, "A — VWAP-only Sweep", system='A')

print("\n── OPTION C: Opening Range Breakout ──────────────")
df_c = build_option_c(btc)
df_c = detect_option_c(df_c, skip_vol=skip_vol)
print(f"  Signals: {df_c['long_entry'].sum()} long / {df_c['short_entry'].sum()} short")
r_c = backtest_generic(df_c, "C — Opening Range Breakout", system='C')

def print_r(r):
    if r is None: print("  No trades"); return
    print(f"\n  {'─'*64}")
    print(f"  {r['label']}")
    print(f"  {'─'*64}")
    print(f"  Trades  : {r['n']}  ({r['tdf']['time'].min()[:10]} → {r['tdf']['time'].max()[:10]})")
    print(f"  WR      : {r['wr']*100:.1f}%  (BE: {r['be']*100:.1f}%, edge: {(r['wr']-r['be'])*100:+.1f}%)")
    print(f"  TP1     : {r['t1r']:.1f}%")
    print(f"  PF      : {r['pf']:.2f}")
    print(f"  ROI     : {r['roi']:+.2f}%  P&L: ${r['equity']-ACCOUNT:+,.2f}")
    print(f"  MaxDD   : {r['mdd']:.2f}%")
    print(f"  +ve mo  : {r['pm']}/{r['tm']}")
    print(f"  p-value : {r['p']:.4f}  {'✓ SIGNIFICANT' if r['p']<0.05 else '— not significant'}")
    print(f"\n  By direction:\n{r['by_dir'].to_string()}")
    print(f"\n  By exit:\n{r['by_exit'].to_string()}")
    print(f"\n  Monthly:")
    print(r['mo'][['month','n','pnl','wr','roi%']].to_string(index=False))

print(f"\n{'='*68}")
print(f"  RESULTS")
print(f"{'='*68}")
print_r(r_a)
print_r(r_c)

# Head to head
print(f"\n{'='*68}")
print(f"  HEAD-TO-HEAD")
print(f"{'='*68}")
print(f"\n  {'Metric':<14} {'Option A':>14} {'Option C':>14} {'Winner':>10}")
print(f"  {'─'*54}")

metrics = [
    ("Trades",   lambda r: r['n'],                   'higher_n'),
    ("WR %",     lambda r: r['wr']*100,              'higher'),
    ("PF",       lambda r: r['pf'],                  'higher'),
    ("ROI %",    lambda r: r['roi'],                 'higher'),
    ("MaxDD %",  lambda r: r['mdd'],                 'higher'),  # less negative
    ("p-value",  lambda r: r['p'],                   'lower'),
    ("TP1 %",    lambda r: r['t1r'],                 'higher'),
]
for nm, fn, dir_ in metrics:
    if r_a and r_c:
        va, vc = fn(r_a), fn(r_c)
        if dir_ in ['higher','higher_n']: winner = 'A' if va > vc else 'C' if vc > va else 'tie'
        else:                              winner = 'A' if va < vc else 'C' if vc < va else 'tie'
        fmt = "{:.1f}" if isinstance(va, float) else "{}"
        print(f"  {nm:<14} {fmt.format(va):>14} {fmt.format(vc):>14} {winner:>10}")

if r_a: r_a['tdf'].to_csv("v5_option_a.csv", index=False)
if r_c: r_c['tdf'].to_csv("v5_option_c.csv", index=False)
print(f"\nSaved: v5_option_a.csv, v5_option_c.csv")
