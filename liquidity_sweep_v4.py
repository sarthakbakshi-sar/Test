"""
LIQUIDITY SWEEP v4 — RESEARCH-BACKED REBUILD
=============================================
v2 showed: 50.9% WR, +3.5% ROI, 9/14 +ve months — REAL but unrefined
v3 showed: 47.9% WR, -5.1% — over-filtering removed good trades

KEY INSIGHT: v2's TP2 made +$3,728. SL lost -$3,222.
The system finds good setups. The problem is trade MANAGEMENT.

v4 CHANGES (based on academic research):

1. TRIPLE-BARRIER EXITS (Lopez de Prado 2018):
   - SL: 0.5x ATR initial
   - After 0.3x ATR favorable move (2 bars) → SL to breakeven
   - After 0.6x ATR favorable move → trail SL 0.3x ATR behind
   - TP1: 1.0x ATR (closer than v3) → close 60%
   - TP2: 2.5x ATR (let runners run)

2. SIGNAL SCORING (Carver 2015):
   Not all sweeps equal. Score 1-10:
   - Volume ratio: 1.5x=1pt, 2.0x=2pt, 2.5x+=3pt
   - Body strength: 65-75%=1pt, 75-85%=2pt, 85%+=3pt
   - Confluence: 1 level=0pt, 2 within 0.2%=2pt, 3=3pt
   - Trend alignment: in direction of EMA50 slope=1pt
   Only trade score >= 5. Size proportional to score.

3. KILL ZONE REFINEMENT (ICT methodology):
   First 90 MINUTES of NY Prime only (14:00-15:30 UTC).
   Volume concentrates at session open.

4. VIX REGIME FILTER (Avellaneda 2010):
   Mean reversion works in low-vol. VIX below 22 only.

5. LEVELS: VWAP + PrevHigh + PrevLow (no RoundNum)

GOAL: Push 50.9% → 55%+ WR with p < 0.05
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
ACCOUNT      = 10000.0
BASE_RISK    = 0.015                # 1.5% base risk (scales with score)
TP1_MULT     = 1.0                  # TIGHTER target
TP2_MULT     = 2.5                  # Let runners run
TP1_CLOSE    = 0.60
TP2_CLOSE    = 0.40
TIME_STOP    = 5
COMMISSION   = 0.00055

# Triple-barrier params
BE_TRIGGER   = 0.3                  # 0.3x ATR favorable → move SL to BE
TRAIL_TRIGGER= 0.6                  # 0.6x ATR favorable → start trailing
TRAIL_DIST   = 0.3                  # trail at 0.3x ATR

# Signal scoring
MIN_SCORE    = 5                    # Only trade score >= 5
SWEEP_BUFFER = 0.0012
MIN_VOL      = 1.5
BODY_MIN     = 0.65
EMA_TREND    = 50

# VIX regime filter
VIX_MAX      = 22

# v4 Sessions: FIRST 90 MIN of NY Prime
BTC_SESS = [("NY_kill", time(14, 0), time(15, 30))]   # 18:00-19:30 Dubai

TD_KEY = "06f050cd7d9940a895f9461e7f0ff7e3"

print("="*68)
print("  LIQUIDITY SWEEP v4 — RESEARCH-BACKED")
print("  Triple-barrier | Score-based sizing | VIX filter | First 90min")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA
# ══════════════════════════════════════════════════════════════════════════════

def load_binance_btc(days=365):
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
        except Exception as e:
            print(f"  Err: {e}"); break
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

def load_vix():
    """Load VIX daily for regime filter."""
    print("Loading VIX...")
    try:
        v = yf.download("^VIX", period="2y", interval="1d", progress=False)
        v.columns = [c[0].lower() for c in v.columns]
        v.index = pd.to_datetime(v.index).tz_localize(None)
        print(f"  VIX: {len(v)} days | last: {v['close'].iloc[-1]:.2f}")
        return v['close']
    except Exception as e:
        print(f"  VIX failed: {e}")
        return None

def load_btc_twelve():
    import urllib.parse
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
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def build(df, vix=None):
    d = df.copy()
    d['date'] = d.index.normalize()

    dhl = d.groupby('date').agg(sh=('high','max'), sl=('low','min'))
    d['prev_h'] = d['date'].map(dhl['sh'].shift(1).to_dict())
    d['prev_l'] = d['date'].map(dhl['sl'].shift(1).to_dict())

    d['typ']  = (d['high']+d['low']+d['close'])/3
    d['ctv']  = d.groupby('date').apply(
        lambda g:(g['typ']*g['volume']).cumsum()).reset_index(level=0,drop=True)
    d['cv']   = d.groupby('date')['volume'].cumsum()
    d['vwap'] = d['ctv']/(d['cv']+1e-10)

    d['vol_ma'] = d['volume'].rolling(20).mean()
    d['vol_ratio'] = d['volume'] / (d['vol_ma'] + 1e-10)

    d['tr']  = np.maximum(d['high']-d['low'],
               np.maximum((d['high']-d['close'].shift(1)).abs(),
                          (d['low'] -d['close'].shift(1)).abs()))
    d['atr'] = d['tr'].rolling(14).mean()

    d['ema50']      = d['close'].ewm(span=EMA_TREND, adjust=False).mean()
    d['ema50_slope']= d['ema50'].diff(5)
    d['uptrend']    = d['close'] > d['ema50']

    # VIX regime
    if vix is not None:
        d['vix'] = d['date'].map(vix.to_dict())
        d['vix'] = d['vix'].ffill()
        d['vix_ok'] = d['vix'] < VIX_MAX
    else:
        d['vix_ok'] = True

    # Session: first 90 min only
    def gs(ts):
        t = ts.time() if hasattr(ts,'time') else ts
        for nm,s,e in BTC_SESS:
            if s<=t<e: return nm
        return None
    d['session']  = d.index.map(gs)
    d['in_sess']  = d['session'].notna()
    d['sess_bar'] = d.groupby(['date','session']).cumcount()

    d.dropna(subset=['atr','vwap','prev_h','prev_l','ema50'], inplace=True)
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  SCORED SWEEP DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_v4(df, skip_vol=False):
    d = df.copy()
    long_signals  = []
    short_signals = []
    long_scores   = []
    short_scores  = []
    long_levels   = []
    short_levels  = []
    # When volume is synthetic (Twelve Data), lower threshold by 1pt
    min_score_eff = MIN_SCORE - (1 if skip_vol else 0)

    LEVELS = ['vwap', 'prev_l', 'prev_h']

    def score_setup(row, direction, hit_levels):
        score = 0
        # Volume
        vr = row['vol_ratio']
        if   vr >= 2.5: score += 3
        elif vr >= 2.0: score += 2
        elif vr >= 1.5: score += 1
        # Body strength
        cr = row['high'] - row['low']
        if cr > 0:
            if direction == 'LONG':
                body_pct = (row['close'] - row['low']) / cr
            else:
                body_pct = (row['high'] - row['close']) / cr
            if   body_pct >= 0.85: score += 3
            elif body_pct >= 0.75: score += 2
            elif body_pct >= 0.65: score += 1
        # Confluence (multiple levels close)
        if   len(hit_levels) >= 3: score += 3
        elif len(hit_levels) >= 2: score += 2
        # Trend alignment with EMA slope
        slope = row['ema50_slope']
        if direction == 'LONG' and slope > 0: score += 1
        if direction == 'SHORT' and slope < 0: score += 1
        return score

    def find_long_sweeps(row):
        hit = []
        for col in LEVELS:
            level = row.get(col, np.nan)
            if pd.isna(level) or level <= 0: continue
            cr = row['high']-row['low']
            if cr <= 0: continue
            if not skip_vol and row['volume'] < row['vol_ma']*MIN_VOL: continue
            if not (row['low'] < level*(1-0.0001) and row['low']>=level*(1-SWEEP_BUFFER)):
                continue
            if row['close'] <= level: continue
            if (row['close']-row['low'])/cr < BODY_MIN: continue
            hit.append(col)
        return hit

    def find_short_sweeps(row):
        hit = []
        for col in LEVELS:
            level = row.get(col, np.nan)
            if pd.isna(level) or level <= 0: continue
            cr = row['high']-row['low']
            if cr <= 0: continue
            if not skip_vol and row['volume'] < row['vol_ma']*MIN_VOL: continue
            if not (row['high'] > level*(1+0.0001) and row['high']<=level*(1+SWEEP_BUFFER)):
                continue
            if row['close'] >= level: continue
            if (row['high']-row['close'])/cr < BODY_MIN: continue
            hit.append(col)
        return hit

    for i, (idx, row) in enumerate(d.iterrows()):
        if not row['in_sess'] or row.get('sess_bar', 0) < 1 or not row.get('vix_ok', True):
            long_signals.append(False); long_scores.append(0); long_levels.append('')
            short_signals.append(False); short_scores.append(0); short_levels.append('')
            continue

        up = row['uptrend']

        # Long: trend up + sweep below support
        if up:
            hits = find_long_sweeps(row)
            if hits:
                sc = score_setup(row, 'LONG', hits)
                if sc >= min_score_eff:
                    long_signals.append(True)
                    long_scores.append(sc)
                    long_levels.append('+'.join(hits))
                    short_signals.append(False); short_scores.append(0); short_levels.append('')
                    continue

        # Short: trend down + sweep above resistance
        if not up:
            hits = find_short_sweeps(row)
            if hits:
                sc = score_setup(row, 'SHORT', hits)
                if sc >= min_score_eff:
                    short_signals.append(True)
                    short_scores.append(sc)
                    short_levels.append('+'.join(hits))
                    long_signals.append(False); long_scores.append(0); long_levels.append('')
                    continue

        long_signals.append(False); long_scores.append(0); long_levels.append('')
        short_signals.append(False); short_scores.append(0); short_levels.append('')

    d['long_sweep']  = long_signals
    d['short_sweep'] = short_signals
    d['long_score']  = long_scores
    d['short_score'] = short_scores
    d['long_lvl']    = long_levels
    d['short_lvl']   = short_levels

    # Enter on next candle, no-gap filter
    d['long_entry']  = d['long_sweep'].shift(1).fillna(False)
    d['short_entry'] = d['short_sweep'].shift(1).fillna(False)
    d['ent_score']   = np.where(d['long_entry'], d['long_score'].shift(1),
                       np.where(d['short_entry'], d['short_score'].shift(1), 0))
    d['ent_lvl']     = np.where(d['long_entry'], d['long_lvl'].shift(1),
                       np.where(d['short_entry'], d['short_lvl'].shift(1), ''))

    d['open_gap'] = (d['open']-d['close'].shift(1)).abs()/d['close'].shift(1)
    d['long_entry']  = d['long_entry']  & (d['open_gap'] < 0.002)
    d['short_entry'] = d['short_entry'] & (d['open_gap'] < 0.002)
    return d

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST WITH TRIPLE-BARRIER EXITS
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
        hi  = row['high']
        lo  = row['low']
        atr = row['atr']
        if pd.isna(atr) or atr==0: continue

        if in_t:
            d_,ep = ent['dir'], ent['price']
            sl_init = ent['sl_init']
            tp1, tp2 = ent['tp1'], ent['tp2']
            sl  = ent.get('sl_dyn', sl_init)
            t1h = ent.get('t1h', False)
            bi  = i - ent['bi']

            # Triple-barrier: dynamic SL management
            if d_ == 'LONG':
                fav_move = hi - ep
                # Trail stop
                if fav_move >= atr*TRAIL_TRIGGER and not t1h:
                    new_sl = hi - atr*TRAIL_DIST
                    if new_sl > sl: ent['sl_dyn'] = new_sl; sl = new_sl
                # Breakeven trigger
                elif fav_move >= atr*BE_TRIGGER and not t1h:
                    if ep > sl: ent['sl_dyn'] = ep; sl = ep
            else:
                fav_move = ep - lo
                if fav_move >= atr*TRAIL_TRIGGER and not t1h:
                    new_sl = lo + atr*TRAIL_DIST
                    if new_sl < sl: ent['sl_dyn'] = new_sl; sl = new_sl
                elif fav_move >= atr*BE_TRIGGER and not t1h:
                    if ep < sl: ent['sl_dyn'] = ep; sl = ep

            # Exit checks
            h_t1 = not t1h and ((d_=='LONG' and hi>=tp1) or (d_=='SHORT' and lo<=tp1))
            h_t2 = t1h    and ((d_=='LONG' and hi>=tp2) or (d_=='SHORT' and lo<=tp2))
            h_sl = (d_=='LONG' and lo<=sl) or (d_=='SHORT' and hi>=sl)
            h_tm = bi >= TIME_STOP and not t1h
            h_se = row['session'] is None and ent['sess'] is not None

            if h_t1:
                xp  = tp1
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity * pct * ent['sm'] * TP1_CLOSE
                equity += usd
                ent.update({'t1h':True,'sl_dyn':ep,'t1p':usd,
                            'sm':ent['sm']*TP2_CLOSE})

            elif h_t2 or h_sl or h_tm or h_se:
                xp  = tp2 if h_t2 else sl if h_sl else cp
                xt  = 'TP2' if h_t2 else 'SL' if h_sl else 'TIME' if h_tm else 'SESS'
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - COMMISSION
                usd = equity * pct * ent['sm']
                equity += usd
                tot = usd + ent.get('t1p', 0)
                trades.append({
                    'time':str(ent['t'])[:16],'dir':d_,'level':ent['lv'],
                    'entry':round(ep,2),'exit':round(xp,2),
                    'score':ent['sc'],
                    't1hit':ent.get('t1h',False),
                    'pnl':round(tot,2),'eq':round(equity,2),
                    'xt':xt,'bars':bi,
                })
                eq_c.append(equity); eq_d.append(idx)
                in_t = False

        if not in_t:
            score = row.get('ent_score', 0)
            if row.get('long_entry', False) and score >= MIN_SCORE:
                sl_ = cp - atr*0.5
                slp = (cp-sl_)/cp
                if 0 < slp <= 0.012:
                    tp1_=cp+atr*TP1_MULT
                    tp2_=cp+atr*TP2_MULT
                    # Score-based risk: 1.5% base, scales up to 2.5% at score 10
                    risk = BASE_RISK * (1 + (score - MIN_SCORE) * 0.1)
                    risk = min(risk, 0.025)
                    oz = (equity*risk)/(cp*slp+1e-10)
                    sm = min((oz*cp)/equity, 4.0)
                    in_t = True
                    ent = dict(t=idx,bi=i,price=cp,dir='LONG',
                               sl_init=sl_,sl_dyn=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                               t1h=False,sess=row['session'],
                               lv=row.get('ent_lvl',''),sc=int(score))

            elif row.get('short_entry', False) and score >= MIN_SCORE:
                sl_ = cp + atr*0.5
                slp = (sl_-cp)/cp
                if 0 < slp <= 0.012:
                    tp1_=cp-atr*TP1_MULT
                    tp2_=cp-atr*TP2_MULT
                    risk = BASE_RISK * (1 + (score - MIN_SCORE) * 0.1)
                    risk = min(risk, 0.025)
                    oz = (equity*risk)/(cp*slp+1e-10)
                    sm = min((oz*cp)/equity, 4.0)
                    in_t = True
                    ent = dict(t=idx,bi=i,price=cp,dir='SHORT',
                               sl_init=sl_,sl_dyn=sl_,tp1=tp1_,tp2=tp2_,sm=sm,
                               t1h=False,sess=row['session'],
                               lv=row.get('ent_lvl',''),sc=int(score))

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

    # By score
    by_score = tdf.groupby('score').apply(lambda x: pd.Series({
        'n': len(x),
        'wr%': round((x['pnl']>0).mean()*100, 1),
        'avg$': round(x['pnl'].mean(), 2),
        'pnl$': round(x['pnl'].sum(), 2),
    }), include_groups=False)

    by_exit = tdf.groupby('xt').apply(lambda x: pd.Series({
        'n':len(x),'wr%':round((x['pnl']>0).mean()*100,1),
        'avg':round(x['pnl'].mean(),2),'pnl':round(x['pnl'].sum(),2)
    }), include_groups=False)

    return dict(label=label,tdf=tdf,mo=mo,by_score=by_score,by_exit=by_exit,
                n=len(tdf),wr=wr,roi=roi,pf=pf,mdd=mdd,equity=equity,
                be=be,t=t,p=p,aw=aw,al=al,t1r=t1r,
                pm=(mo['pnl']>0).sum(),tm=len(mo))

# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

skip_vol = False
btc = load_binance_btc(365)
if btc is None:
    btc = load_btc_twelve()
    skip_vol = True
if btc is None: print("ERROR: No data"); exit()

vix = load_vix()

print("\nBuilding v4 indicators...")
btc = build(btc, vix=vix)
btc = detect_v4(btc, skip_vol=skip_vol)
print(f"  Long signals : {btc['long_sweep'].sum()}")
print(f"  Short signals: {btc['short_sweep'].sum()}")
print(f"  After confirm: {btc['long_entry'].sum()} long / {btc['short_entry'].sum()} short")

# Score distribution
if btc['long_score'].sum() > 0 or btc['short_score'].sum() > 0:
    all_scores = pd.concat([
        btc[btc['long_sweep']]['long_score'],
        btc[btc['short_sweep']]['short_score']
    ])
    print(f"\n  Signal score distribution:")
    print(all_scores.value_counts().sort_index().to_string())

r = backtest(btc, "BTC v4 — Triple-barrier + Scoring + VIX + First 90min")

if r is None:
    print("\nNo trades. Try MIN_SCORE=4 or wider SWEEP_BUFFER.")
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
print(f"  +ve mo   : {r['pm']}/{r['tm']}")
print(f"  p-value  : {r['p']:.4f}  {'✓ SIGNIFICANT' if r['p']<0.05 else '— need more samples'}")

print(f"\n  BY SCORE (higher score = better setup?):")
print(r['by_score'].to_string())

print(f"\n  BY EXIT:")
print(r['by_exit'].to_string())

print(f"\n  MONTHLY:")
print(r['mo'][['month','n','pnl','wr','roi%']].to_string(index=False))

# Comparison
print(f"\n{'='*68}")
print(f"  EVOLUTION: v2 → v3 → v4")
print(f"{'='*68}")
v2 = dict(n=116, wr=50.9, roi=3.50, t1r=50.9, pm=9, tm=14, p=0.72)
v3 = dict(n=142, wr=47.9, roi=-5.14, t1r=47.9, pm=7, tm=14, p=0.60)
print(f"\n  {'Metric':<14} {'v2':>10} {'v3':>10} {'v4':>10}")
print(f"  {'─'*48}")
metrics = [
    ("Trades",     v2['n'],            v3['n'],            r['n']),
    ("WR %",       f"{v2['wr']:.1f}",  f"{v3['wr']:.1f}",  f"{r['wr']*100:.1f}"),
    ("ROI %",      f"{v2['roi']:+.1f}",f"{v3['roi']:+.1f}",f"{r['roi']:+.1f}"),
    ("TP1 %",      f"{v2['t1r']:.1f}", f"{v3['t1r']:.1f}", f"{r['t1r']:.1f}"),
    ("+ve mo",     f"{v2['pm']}/{v2['tm']}", f"{v3['pm']}/{v3['tm']}", f"{r['pm']}/{r['tm']}"),
    ("p-value",    f"{v2['p']:.3f}",   f"{v3['p']:.3f}",   f"{r['p']:.4f}"),
]
for nm, a, b, c in metrics:
    print(f"  {nm:<14} {str(a):>10} {str(b):>10} {str(c):>10}")

r['tdf'].to_csv("btc_sweep_v4.csv", index=False)
print(f"\nSaved: btc_sweep_v4.csv")
