"""
CRYPTO BACKTEST — BTC & ETH vs GOLD
======================================
Same framework as gold_15m_v2.py (FIX3 winner):
  - Macro score (5 components, prior-day closes)
  - Session: 13:30–17:00 UTC  Entry window: 14:00–16:30 UTC
  - Entry: 4H EMA align + 15m EMA align + VWAP zone (-1 to +0.3σ long)
  - Exit: SL=0.75xATR, TP1=1.5xATR(60%), TP2=2.5xATR(40%), Time=6bars

BTC macro: EMA50 trend + yield dir + DXY dir + 5d momentum + ETH lead vs BTC
ETH macro: EMA50 trend + BTC trend + DXY dir + 5d momentum + ETH/BTC ratio
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import requests, time, urllib.parse
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

API_KEY   = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT   = 10_000.0
RISK_PCT  = 0.03
SL_MULT   = 0.75
TP1_MULT  = 1.5
TP2_MULT  = 2.5
TP1_FRAC  = 0.60
TP2_FRAC  = 0.40
TIME_STOP = 6
MAX_TR    = 2
MIN_GAP   = 4
SESS_O    = (13, 30)
SESS_C    = (17,  0)
E_START   = 14.0
E_END     = 16.5

# ── FETCH 15m DATA ─────────────────────────────────────────────────────────────
def fetch_15m(symbol, pages=22):
    all_frames = []
    end_date   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for page in range(pages):
        url = (f"https://api.twelvedata.com/time_series?symbol={urllib.parse.quote(symbol)}"
               f"&interval=15min&outputsize=5000&apikey={API_KEY}"
               f"&end_date={urllib.parse.quote(end_date)}&timezone=UTC")
        j = requests.get(url, timeout=30).json()
        if 'values' not in j: print(f"  [{symbol}] page {page+1}: stopped ({j.get('message','')})"); break
        dfp = pd.DataFrame(j['values'])
        dfp['datetime'] = pd.to_datetime(dfp['datetime'])
        dfp.set_index('datetime', inplace=True); dfp.sort_index(inplace=True)
        for c in ['open','high','low','close','volume']:
            if c in dfp.columns: dfp[c] = pd.to_numeric(dfp[c], errors='coerce')
        dfp.dropna(subset=['open','high','low','close'], inplace=True)
        dfp['volume'] = dfp.get('volume', pd.Series(1.0, index=dfp.index)).fillna(1.0).replace(0, 1.0)
        all_frames.append(dfp)
        earliest = dfp.index[0]
        print(f"  [{symbol}] page {page+1}: {earliest.date()} → {dfp.index[-1].date()}  ({len(dfp)} bars)")
        end_date = (earliest - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        if page < pages-1: time.sleep(8)
    df = pd.concat(all_frames).sort_index()
    return df[~df.index.duplicated(keep='first')]

# ── MACRO SCORES ───────────────────────────────────────────────────────────────
def dl(t):
    d = yf.download(t, period="3y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d['close'].dropna()

def build_macro(asset='BTC'):
    btc = dl("BTC-USD"); eth = dl("ETH-USD")
    dxy = dl("DX-Y.NYB"); tnx = dl("^TNX")

    if asset == 'BTC':
        base = btc
        ema50 = base.ewm(span=50, adjust=False).mean()
        s1 = np.where(base > ema50, 1, -1)                                     # BTC vs EMA50
        s2 = np.where(tnx.reindex(base.index, method='ffill').diff() < 0, 1, -1)  # yield falling
        s3 = np.where(dxy.reindex(base.index, method='ffill').pct_change() < 0, 1, -1)  # DXY falling
        s4 = np.where(base.pct_change(5) > 0, 1, -1)                          # 5d momentum
        eth_r = eth.reindex(base.index, method='ffill').pct_change()
        btc_r = base.pct_change()
        s5 = np.where(eth_r > btc_r, 1, -1)                                   # ETH leading BTC (risk-on)
    else:  # ETH
        base = eth
        ema50 = base.ewm(span=50, adjust=False).mean()
        btc_ema50 = btc.ewm(span=50, adjust=False).mean()
        s1 = np.where(base > ema50, 1, -1)                                     # ETH vs EMA50
        s2 = np.where(btc.reindex(base.index, method='ffill') >
                      btc_ema50.reindex(base.index, method='ffill'), 1, -1)   # BTC in uptrend
        s3 = np.where(dxy.reindex(base.index, method='ffill').pct_change() < 0, 1, -1)  # DXY falling
        s4 = np.where(base.pct_change(5) > 0, 1, -1)                          # ETH 5d momentum
        eth_btc = (base / btc.reindex(base.index, method='ffill'))
        s5 = np.where(eth_btc.pct_change(3) > 0, 1, -1)                       # ETH/BTC ratio rising

    raw = pd.Series(s1 + s2 + s3 + s4 + s5, index=base.index).shift(1)
    score = (raw / 5 * 10).round(1)
    dm = pd.DataFrame({'score': score, 'price': base}).dropna()
    return {d.date(): float(r['score']) for d, r in dm.iterrows()}

# ── INDICATORS ─────────────────────────────────────────────────────────────────
def add_indicators(df):
    df = df.copy()
    df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-df['close'].shift()).abs(),
                    (df['low'] -df['close'].shift()).abs()], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()
    return df

def build_4h(df15):
    g4 = df15.resample('4h', label='left', closed='left').agg(
        {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
    g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
    g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
    return g4

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def backtest(df, g4, macro_lkp):
    equity = ACCOUNT; trades = []

    def get4h(ts):
        past = g4[g4.index <= ts]
        return past.iloc[-1] if len(past) else None

    for date in sorted(set(df.index.date)):
        score = macro_lkp.get(date)
        if score is None or abs(score) < 2: continue
        macro_dir = 1 if score > 0 else -1
        ts   = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=SESS_O[0], minutes=SESS_O[1])
        c_ts = ts + pd.Timedelta(hours=SESS_C[0], minutes=SESS_C[1])
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4: continue
        ctx = get4h(o_ts)
        if ctx is None: continue
        if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']: continue

        tp_v  = (sbars['high']+sbars['low']+sbars['close'])/3
        vol   = sbars['volume'].replace(0, 1)
        sbars['vwap']     = (tp_v * vol).cumsum() / vol.cumsum()
        sbars['vwap_std'] = tp_v.expanding().std().bfill().fillna(1)

        in_trade=False; entry_px=sl=tp1_px=tp2_px=sz1=sz2=pnl1=0
        tp1_hit=False; bars_held=0; trade_dir=0
        trades_today=0; last_i=-99

        for i in range(1, len(sbars)):
            b = sbars.iloc[i]
            hi,lo,cls = b['high'],b['low'],b['close']
            bh = b.name.hour + b.name.minute/60
            if not (E_START <= bh <= E_END): continue
            vwap = b['vwap']; std = max(b['vwap_std'], 0.01)

            if in_trade:
                bars_held += 1
                if trade_dir == 1:
                    if lo <= sl:
                        trades.append(dict(date=date,dir='LONG',pnl=(sl-entry_px)*(sz1+sz2)+pnl1,reason='SL',score=score)); equity+=(sl-entry_px)*(sz1+sz2)+pnl1; in_trade=False; continue
                    if not tp1_hit and hi >= tp1_px:
                        pnl1=(tp1_px-entry_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                    if tp1_hit and hi >= tp2_px:
                        pnl2=(tp2_px-entry_px)*sz2; trades.append(dict(date=date,dir='LONG',pnl=pnl1+pnl2,reason='TP2',score=score)); equity+=pnl2; in_trade=False; continue
                else:
                    if hi >= sl:
                        trades.append(dict(date=date,dir='SHORT',pnl=(entry_px-sl)*(sz1+sz2)+pnl1,reason='SL',score=score)); equity+=(entry_px-sl)*(sz1+sz2)+pnl1; in_trade=False; continue
                    if not tp1_hit and lo <= tp1_px:
                        pnl1=(entry_px-tp1_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                    if tp1_hit and lo <= tp2_px:
                        pnl2=(entry_px-tp2_px)*sz2; trades.append(dict(date=date,dir='SHORT',pnl=pnl1+pnl2,reason='TP2',score=score)); equity+=pnl2; in_trade=False; continue
                if bars_held >= TIME_STOP:
                    rem=sz2 if tp1_hit else (sz1+sz2)
                    pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem+pnl1
                    trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',pnl=pnl,reason='TIME',score=score)); equity+=pnl; in_trade=False
            else:
                if trades_today >= MAX_TR or i-last_i < MIN_GAP: continue
                c4 = get4h(b.name)
                if c4 is None: continue
                h4ok = (macro_dir==1 and c4['ema9']>c4['ema21']) or (macro_dir==-1 and c4['ema9']<c4['ema21'])
                if not h4ok: continue
                emok = (macro_dir==1 and b['ema9']>b['ema21']) or (macro_dir==-1 and b['ema9']<b['ema21'])
                if not emok: continue
                dist = (cls-vwap)/std
                vwok = (macro_dir==1 and -1.0<=dist<=0.3) or (macro_dir==-1 and -0.3<=dist<=1.0)
                if not vwok: continue
                atr_v = float(b['atr'])
                if atr_v <= 0: continue
                tot_sz=(equity*RISK_PCT)/(SL_MULT*atr_v)
                sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC
                entry_px=cls
                if macro_dir==1: sl=cls-SL_MULT*atr_v; tp1_px=cls+TP1_MULT*atr_v; tp2_px=cls+TP2_MULT*atr_v
                else:             sl=cls+SL_MULT*atr_v; tp1_px=cls-TP1_MULT*atr_v; tp2_px=cls-TP2_MULT*atr_v
                in_trade=True; trade_dir=macro_dir; bars_held=0; tp1_hit=False; pnl1=0
                trades_today+=1; last_i=i

        if in_trade:
            cls=sbars.iloc[-1]['close']; rem=sz2 if tp1_hit else (sz1+sz2)
            pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem+pnl1
            trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',pnl=pnl,reason='SESS',score=score)); equity+=pnl

    return equity, trades

# ── RESULTS FORMATTER ──────────────────────────────────────────────────────────
def show(label, eq, trs, period=""):
    if not trs: print(f"\n{label}: NO TRADES"); return None
    t = pd.DataFrame(trs)
    t['date']=pd.to_datetime(t['date']); t['win']=t['pnl']>0; t['month']=t['date'].dt.to_period('M')
    n   = len(t)
    roi = (eq-ACCOUNT)/ACCOUNT*100
    wr  = t['win'].mean()*100
    _,pv= stats.ttest_1samp(t['pnl'],0)
    wks = max((t['date'].max()-t['date'].min()).days/7,1)
    tpw = n/wks
    sharpe = t['pnl'].mean()/t['pnl'].std()*np.sqrt(252) if t['pnl'].std()>0 else 0
    eq_c= ACCOUNT+t['pnl'].cumsum()
    mdd = ((eq_c-eq_c.cummax())/eq_c.cummax()*100).min()
    lo  = t[t['dir']=='LONG']; sh=t[t['dir']=='SHORT']

    print(f"\n{'='*60}")
    print(f"  {label}  {period}")
    print(f"{'='*60}")
    print(f"  $10,000  →  ${eq:,.0f}  ({roi:+.1f}% ROI)")
    print(f"  Sharpe   : {sharpe:.2f}    Max DD: {mdd:.1f}%")
    print(f"  Trades   : {n}  ({tpw:.1f}/week)   WR: {wr:.1f}%")
    print(f"  p-value  : {pv:.4f}  {'✓ SIGNIFICANT' if pv<0.05 else '~ borderline' if pv<0.10 else '— not significant'}")
    if len(lo): print(f"  Longs  {len(lo):>4}tr  WR {lo['win'].mean()*100:.0f}%  ${lo['pnl'].sum():+,.0f}")
    if len(sh): print(f"  Shorts {len(sh):>4}tr  WR {sh['win'].mean()*100:.0f}%  ${sh['pnl'].sum():+,.0f}")
    print(f"\n  Monthly:")
    run_eq=ACCOUNT; pos=0
    for mo,g in t.groupby('month'):
        run_eq+=g['pnl'].sum(); flag="✓" if g['pnl'].sum()>0 else "✗"
        if g['pnl'].sum()>0: pos+=1
        print(f"    {mo}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+7,.0f}  ${run_eq:>9,.0f} {flag}")
    print(f"  Positive months: {pos}/{t['month'].nunique()}")
    return dict(label=label, n=n, tpw=tpw, roi=roi, wr=wr, sharpe=sharpe, pv=pv, mdd=mdd, eq=eq)

# ── MAIN ───────────────────────────────────────────────────────────────────────
print("="*60)
print("  CRYPTO BACKTEST — BTC & ETH")
print("  Same framework as Gold 15m system")
print("="*60)

print("\n--- Fetching BTC/USD 15m ---")
btc_15m = fetch_15m("BTC/USD", pages=22)
print(f"  Total: {len(btc_15m)} bars | {btc_15m.index[0].date()} → {btc_15m.index[-1].date()}")

print("\n--- Fetching ETH/USD 15m ---")
time.sleep(10)
eth_15m = fetch_15m("ETH/USD", pages=22)
print(f"  Total: {len(eth_15m)} bars | {eth_15m.index[0].date()} → {eth_15m.index[-1].date()}")

print("\nLoading macro data...")
btc_macro = build_macro('BTC')
eth_macro  = build_macro('ETH')

print("Building indicators...")
btc_df = add_indicators(btc_15m)
eth_df = add_indicators(eth_15m)
btc_4h = build_4h(btc_15m)
eth_4h = build_4h(eth_15m)

print("\nRunning BTC backtest...")
btc_eq, btc_trs = backtest(btc_df, btc_4h, btc_macro)

print("Running ETH backtest...")
eth_eq, eth_trs = backtest(eth_df, eth_4h, eth_macro)

period = f"({btc_15m.index[0].date()} → {btc_15m.index[-1].date()})"
r_btc = show("BTC/USD", btc_eq, btc_trs, period)
r_eth = show("ETH/USD", eth_eq, eth_trs, period)

# ── COMPARISON ────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  FINAL COMPARISON — Same System, Same Period")
print(f"{'='*70}")
print(f"  {'Asset':<10} {'N':>5} {'TPW':>5} {'ROI':>9} {'WR':>7} {'Sharpe':>8} {'p-val':>9} {'MDD':>8}")
print(f"  {'─'*68}")
# Gold reference (from our best 15m test)
print(f"  {'GOLD':<10} {'125':>5} {'2.1':>5} {'+566.4%':>9} {'47.2%':>7} {'3.30':>8} {'0.022 ✓':>9} {'-28.5%':>8}")
for r in [r_btc, r_eth]:
    if r is None: continue
    sig = "✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "✗")
    print(f"  {r['label']:<10} {r['n']:>5} {r['tpw']:>4.1f} {r['roi']:>+8.1f}% {r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['pv']:>7.4f} {sig}  {r['mdd']:>7.1f}%")
print(f"{'='*70}")
