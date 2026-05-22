"""
LIQUIDITY SWEEP v3 — BTC NY PRIME ONLY
========================================
Focused on the one setup with edge from v2.

v2 RESULTS (BTC):
  116 trades | 50.9% WR | +3.5% ROI | 9/14 +ve months | p=0.72 (no edge confirmed)

v2 KEY FINDINGS:
  - VWAP: +$381 at 58.3% WR ← BEST LEVEL
  - PrevHigh: +$151 at 56% WR ← GOOD
  - RoundNum: -$270 ← DROP
  - SL exits: 27.1% WR ← entering at falling knives

v3 CHANGES (3 surgical tweaks):
  1. LEVELS: Only VWAP + PrevHigh + PrevLow (dropped RoundNum)
  2. MOMENTUM FILTER: Skip longs if BTC dropped >1.5% in last 4h
                      Skip shorts if BTC rose >1.5% in last 4h
                      (prevents catching falling knives)
  3. VOLATILITY FILTER: Skip when ATR > 2x its 50-period mean
                        (avoids extreme conditions where sweeps don't work)

GOAL: Push WR from 50.9% → 55%+ with p<0.05

Run: python liquidity_sweep_v3.py
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timezone, time
import time as time_mod
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT       = 10000.0
RISK_PCT      = 0.02
TP1_MULT      = 1.2
TP2_MULT      = 2.5
TP1_CLOSE     = 0.60
TP2_CLOSE     = 0.40
TIME_STOP     = 5
COMMISSION    = 0.00055

SWEEP_BUFFER  = 0.0010
MIN_VOL_MULT  = 1.5
BODY_PCT      = 0.65
EMA_TREND     = 50

# v3 NEW: Momentum and volatility filters
MOM_4H_MAX    = 0.015      # 1.5% — skip if move in sweep direction exceeded this
ATR_MAX_MULT  = 2.0        # skip when ATR > 2x its average

BTC_GRID      = 1000  # kept for compat, not used in v3 levels
BTC_SESS      = [("NY_prime", time(14, 0), time(17, 0))]

print("="*68)
print("  LIQUIDITY SWEEP v3 — BTC NY PRIME ONLY")
print("  VWAP + PrevH/L levels | Momentum + Volatility filters")
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
            r    = requests.get(url, params={
                "symbol":"BTCUSDT","interval":"15m",
                "limit":1000,"endTime":end_ms}, timeout=15)
            data = r.json()
            if not data: break
            done = False
            for bar in data:
                if int(bar[0]) < cutoff:
                    done = True; break
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
    print(f"\nLoading BTC from Twelve Data (fallback)...")
    frames, end_date = [], None
    for pg in range(8):
        try:
            url = (f"https://api.twelvedata.com/time_series?symbol=BTC/USD"
                   f"&interval=15min&outputsize=5000&order=DESC&apikey=06f050cd7d9940a895f9461e7f0ff7e3")
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
    return out, True   # True = synthetic volume, skip vol filter

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS + LEVELS
# ══════════════════════════════════════════════════════════════════════════════

def build(df):
    d = df.copy()
    d['date'] = d.index.normalize()

    # Previous session H/L
    dhl    = d.groupby('date').agg(sh=('high','max'), sl=('low','min'))
    d['prev_h'] = d['date'].map(dhl['sh'].shift(1).to_dict())
    d['prev_l'] = d['date'].map(dhl['sl'].shift(1).to_dict())

    # VWAP (session)
    d['typ']  = (d['high']+d['low']+d['close'])/3
    d['ctv']  = d.groupby('date').apply(
        lambda g:(g['typ']*g['volume']).cumsum()).reset_index(level=0,drop=True)
    d['cv']   = d.groupby('date')['volume'].cumsum()
    d['vwap'] = d['ctv']/(d['cv']+1e-10)

    # Volume MA + ATR
    d['vol_ma'] = d['volume'].rolling(20).mean()
    d['tr']     = np.maximum(d['high']-d['low'],
                  np.maximum((d['high']-d['close'].shift(1)).abs(),
                             (d['low'] -d['close'].shift(1)).abs()))
    d['atr']    = d['tr'].rolling(14).mean()
    d['atr_ma'] = d['atr'].rolling(50).mean()

    # 50-period EMA trend filter
    d['ema50']   = d['close'].ewm(span=EMA_TREND, adjust=False).mean()
    d['uptrend'] = d['close'] > d['ema50']

    # v3 NEW: 4-hour momentum (16 bars on 15-min)
    d['mom_4h'] = (d['close'] / d['close'].shift(16) - 1)

    # Session
    def gs(ts):
        t = ts.time() if hasattr(ts,'time') else ts
        for nm,s,e in BTC_SESS:
            if s<=t<e: return nm
        return None
    d['session']  = d.index.map(gs)
    d['in_sess']  = d['session'].notna()
    d['sess_bar'] = d.groupby(['date','session']).cumcount()

    d.dropna(subset=['atr','vwap','prev_h','prev_l','ema50','mom_4h'], inplace=True)
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  SWEEP DETECTION v3 — Levels: VWAP, PrevH, PrevL only
# ══════════════════════════════════════════════════════════════════════════════

def detect_sweeps_v3(df, skip_vol=False):
    d = df.copy()
    longs, shorts = [], []

    LEVELS = ['vwap', 'prev_l', 'prev_h']   # v3: removed RoundNum

    def sweep_long(row, level):
        if pd.isna(level) or level <= 0: return False
        cr = row['high']-row['low']
        if cr<=0: return False
        if not skip_vol and row['volume'] < row['vol_ma']*MIN_VOL_MULT: return False
        if not (row['low'] < level*(1-0.0001) and row['low']>=level*(1-SWEEP_BUFFER)):
            return False
        if row['close'] <= level: return False
        return (row['close']-row['low'])/cr >= BODY_PCT

    def sweep_short(row, level):
        if pd.isna(level) or level <= 0: return False
        cr = row['high']-row['low']
        if cr<=0: return False
        if not skip_vol and row['volume'] < row['vol_ma']*MIN_VOL_MULT: return False
        if not (row['high'] > level*(1+0.0001) and row['high']<=level*(1+SWEEP_BUFFER)):
            return False
        if row['close'] >= level: return False
        return (row['high']-row['close'])/cr >= BODY_PCT

    for i, (idx, row) in enumerate(d.iterrows()):
        if not row['in_sess'] or row.get('sess_bar', 0) < 3:
            longs.append(False); shorts.append(False); continue

        # v3 Filter 1: Volatility — skip extreme ATR
        if row['atr'] > row['atr_ma'] * ATR_MAX_MULT:
            longs.append(False); shorts.append(False); continue

        # Trend alignment
        up   = row['uptrend']
        mom  = row['mom_4h']

        fl, fs = False, False

        # v3 Filter 2: Momentum — don't catch falling knives
        # For LONG: skip if 4h return is too negative (already crashed)
        # For SHORT: skip if 4h return is too positive (already spiked)
        long_mom_ok  = mom > -MOM_4H_MAX
        short_mom_ok = mom <  MOM_4H_MAX

        for lvl_col in LEVELS:
            level = row.get(lvl_col, np.nan)
            if pd.isna(level): continue
            if up and long_mom_ok and sweep_long(row, level):  fl = True
            if not up and short_mom_ok and sweep_short(row, level): fs = True

        longs.append(fl); shorts.append(fs)

    d['long_sweep']  = longs
    d['short_sweep'] = shorts

    # Shift by 1 — enter on NEXT candle
    d['long_entry']  = d['long_sweep'].shift(1).fillna(False)
    d['short_entry'] = d['short_sweep'].shift(1).fillna(False)

    # No-gap filter
    d['open_gap']    = (d['open']-d['close'].shift(1)).abs()/d['close'].shift(1)
    d['long_entry']  = d['long_entry']  & (d['open_gap'] < 0.002)
    d['short_entry'] = d['short_entry'] & (d['open_gap'] < 0.002)

    return d

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def backtest(df, label):
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
            d_,ep,sl,tp1,tp2 = ent['dir'],ent['price'],ent['sl'],ent['tp1'],ent['tp2']
            be_sl = ent.get('be_sl', sl)
            t1h   = ent.get('t1h', False)
            bi    = i - ent['bi']
            asl   = be_sl if t1h else sl

            h_t1 = not t1h and ((d_=='LONG' and cp>=tp1) or (d_=='SHORT' and cp<=tp1))
            h_t2 = t1h    and ((d_=='LONG' and cp>=tp2) or (d_=='SHORT' and cp<=tp2))
            h_sl = (d_=='LONG' and cp<=asl) or (d_=='SHORT' and cp>=asl)
            h_tm = bi >= TIME_STOP and not t1h
            h_se = row['session'] is None and ent['sess'] is not None

            if h_t1:
                raw = (tp1-ep) if d_=='LONG' else (ep-tp1)
                pct = raw/ep - COMMISSION
                usd = equity*pct*ent['sm']*TP1_CLOSE
                equity += usd
                ent.update({'t1h':True,'be_sl':ep,'t1p':usd,'sm':ent['sm']*TP2_CLOSE})

            elif h_t2 or h_sl or h_tm or h_se:
                xp  = tp2 if h_t2 else asl if h_sl else cp
                xt  = 'TP2' if h_t2 else 'SL' if h_sl else 'TIME' if h_tm else 'SESS'
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity*pct*ent['sm']
                equity += usd
                tot = usd + ent.get('t1p', 0)
                trades.append({
                    'time':str(ent['t'])[:16],'dir':d_,'level':ent['lv'],
                    'entry':round(ep,2),'exit':round(xp,2),
                    't1hit':ent.get('t1h',False),'pnl':round(tot,2),
                    'eq':round(equity,2),'xt':xt,'bars':bi,
                    'mom4h':round(ent.get('mom4h',0)*100,2),
                })
                eq_c.append(equity); eq_d.append(idx)
                in_t = False

        if not in_t:
            if row.get('long_entry', False):
                sl_ = cp - atr*0.5
                slp = (cp-sl_)/cp
                if 0 < slp <= 0.012:
                    tp1_=cp+(cp-sl_)*TP1_MULT
                    tp2_=cp+(cp-sl_)*TP2_MULT
                    oz=(equity*RISK_PCT)/(cp*slp+1e-10)
                    sm=min((oz*cp)/equity, 4.0)
                    in_t=True
                    ent=dict(t=idx,bi=i,price=cp,dir='LONG',
                             sl=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                             t1h=False,be_sl=sl_,sess=row['session'],
                             lv=_lvl(row), mom4h=row['mom_4h'])
            elif row.get('short_entry', False):
                sl_ = cp + atr*0.5
                slp = (sl_-cp)/cp
                if 0 < slp <= 0.012:
                    tp1_=cp-(sl_-cp)*TP1_MULT
                    tp2_=cp-(sl_-cp)*TP2_MULT
                    oz=(equity*RISK_PCT)/(cp*slp+1e-10)
                    sm=min((oz*cp)/equity, 4.0)
                    in_t=True
                    ent=dict(t=idx,bi=i,price=cp,dir='SHORT',
                             sl=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                             t1h=False,be_sl=sl_,sess=row['session'],
                             lv=_lvl(row), mom4h=row['mom_4h'])

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
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)
    by_lvl = tdf.groupby('level').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)
    by_exit = tdf.groupby('xt').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)

    return dict(label=label,tdf=tdf,mo=mo,by_dir=by_dir,by_lvl=by_lvl,by_exit=by_exit,
                n=len(tdf),wr=wr,roi=roi,pf=pf,mdd=mdd,equity=equity,be=be,t=t,p=p,
                aw=aw,al=al,t1r=t1r,pm=(mo['pnl']>0).sum(),tm=len(mo))

def _lvl(row):
    cp = row['close']
    best, bd = 'Other', 99
    for n,v in [('VWAP',row.get('vwap')),('PrevLow',row.get('prev_l')),
                ('PrevHigh',row.get('prev_h'))]:
        if v and not pd.isna(v):
            d = abs(cp-v)/cp
            if d < bd: bd=d; best=n
    return best

# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

skip_vol = False
btc = load_binance_btc(365)
if btc is None:
    result = load_btc_twelve()
    if result is None:
        print("ERROR: No data"); exit()
    btc, skip_vol = result

print("\nBuilding v3 indicators + filters...")
btc = build(btc)
btc = detect_sweeps_v3(btc, skip_vol=skip_vol)
print(f"  Raw sweeps: {btc['long_sweep'].sum()} long / {btc['short_sweep'].sum()} short")
print(f"  After confirmation filter: {btc['long_entry'].sum()} long / {btc['short_entry'].sum()} short")

r = backtest(btc, "BTC 15m v3 — VWAP/PrevHL | Trend | Mom filter | Vol filter")

if r is None:
    print("\nNo trades — filters too strict. Try widening MOM_4H_MAX or ATR_MAX_MULT.")
    exit()

print(f"\n{'='*68}")
print(f"  {r['label']}")
print(f"{'='*68}")
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

# v2 vs v3 comparison
print(f"\n{'='*68}")
print(f"  v2 vs v3 COMPARISON")
print(f"{'='*68}")
v2 = dict(n=116, wr=50.9, roi=3.50, pf=1.16, t1r=50.9, pm=9, tm=14, p=0.72, mdd=14.51)
print(f"\n  {'Metric':<14} {'v2':>12} {'v3':>12} {'Change':>14}")
print(f"  {'─'*54}")
metrics = [
    ("Trades",     str(v2['n']),         str(r['n']),               f"{r['n']-v2['n']:+d}"),
    ("Win rate",   f"{v2['wr']:.1f}%",   f"{r['wr']*100:.1f}%",     f"{r['wr']*100-v2['wr']:+.1f}pp"),
    ("ROI",        f"{v2['roi']:+.1f}%", f"{r['roi']:+.1f}%",       f"{r['roi']-v2['roi']:+.1f}pp"),
    ("TP1 rate",   f"{v2['t1r']:.1f}%",  f"{r['t1r']:.1f}%",        f"{r['t1r']-v2['t1r']:+.1f}pp"),
    ("+ve months", f"{v2['pm']}/{v2['tm']}", f"{r['pm']}/{r['tm']}", "—"),
    ("p-value",    f"{v2['p']:.3f}",     f"{r['p']:.4f}",           "—"),
    ("Max DD",     f"-{v2['mdd']:.1f}%", f"{r['mdd']:.1f}%",        "—"),
]
for nm, a, b, c in metrics:
    print(f"  {nm:<14} {a:>12} {b:>12} {c:>14}")

r['tdf'].to_csv("btc_sweep_v3.csv", index=False)
print(f"\nSaved: btc_sweep_v3.csv")
print("Paste results.")
