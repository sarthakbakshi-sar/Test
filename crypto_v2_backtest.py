"""
CRYPTO V2 BACKTEST — BTC & ETH (Redesigned)
=============================================
Root-cause fixes vs V1:

  MACRO
  ──────
  1. QQQ replaces 10Y yield as risk-on proxy  (BTC/QQQ r≈0.75; BTC/TNX r≈0.2)
  2. BTC EMA200 replaces ETH-lead as 5th BTC component (long-term regime)
  3. ETH/BTC ratio vs EMA20 kept, but using 10d momentum (was 3d — too noisy)
  4. Threshold tightened: ±6 (3-of-5 factors must agree, was ±2 = 1-of-5)

  ENTRIES
  ────────
  5. 1H EMA9/21 alignment added as middle timeframe gate (4H→1H→15m)
  6. Volatility filter: skip if 15m ATR < 0.10% or > 1.50% of price
     (filters news-spike bars and dead-market bars)

  RISK & SESSIONS
  ────────────────
  7. Risk reduced 3% → 2% per trade (crypto's higher vol needs lower notional)
  8. Tests London (08:00–12:00 UTC, entry 08:30–11:30) AND
     NY (13:30–17:00 UTC, entry 14:00–16:30) — let data pick the winner

  BUG FIX
  ────────
  9. TP1-then-SL/TIME double-counted pnl1 in equity — fixed. Now only the
     incremental pnl of each leg is added to equity when it occurs.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import requests, time, urllib.parse
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

API_KEY     = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT     = 10_000.0
RISK_PCT    = 0.02            # 2% risk (was 3%)
SL_MULT     = 0.75
TP1_MULT    = 1.5
TP2_MULT    = 2.5
TP1_FRAC    = 0.60
TP2_FRAC    = 0.40
TIME_STOP   = 6
MAX_TR      = 2
MIN_GAP     = 4
SCORE_LONG  =  6.0            # 3/5 factors bullish  (score range: -10 to +10)
SCORE_SHORT = -6.0            # 3/5 factors bearish
ATR_PCT_MIN = 0.0010          # 0.10% — skip flat/thin bars
ATR_PCT_MAX = 0.0150          # 1.50% — skip spike/news bars

SESSIONS = {
    'London': dict(open_h=8,  open_m=0,  close_h=12, close_m=0,  e_start=8.5,  e_end=11.5),
    'NY':     dict(open_h=13, open_m=30, close_h=17, close_m=0,  e_start=14.0, e_end=16.5),
}

# ── DATA FETCH ─────────────────────────────────────────────────────────────────
def fetch_15m(symbol, pages=22):
    all_frames = []; end_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for page in range(pages):
        url = (f"https://api.twelvedata.com/time_series?symbol={urllib.parse.quote(symbol)}"
               f"&interval=15min&outputsize=5000&apikey={API_KEY}"
               f"&end_date={urllib.parse.quote(end_date)}&timezone=UTC")
        j = requests.get(url, timeout=30).json()
        if 'values' not in j:
            print(f"  [{symbol}] page {page+1}: stopped ({j.get('message','')})"); break
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
    if not all_frames: return pd.DataFrame()
    df = pd.concat(all_frames).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    print(f"  Total: {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}\n")
    return df

def dl(ticker, period='4y'):
    d = yf.download(ticker, period=period, interval='1d', progress=False, auto_adjust=True)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    # Handle MultiIndex (multi-ticker download) or flat columns (single ticker)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [str(c[0]).lower() for c in d.columns]
    else:
        d.columns = [str(c).lower() for c in d.columns]
    for col in ['close', 'adj close']:
        if col in d.columns:
            return d[col].dropna()
    return d.iloc[:, 3].dropna()   # fallback: 4th column is usually Close

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

# ── MACRO SCORE ────────────────────────────────────────────────────────────────
def build_macro(asset, btc_d, eth_d, qqq_d, dxy_d):
    """
    BTC: EMA50 | QQQ_EMA50 | DXY_inv | 10d_return | EMA200
    ETH: EMA50 | BTC_EMA50 | QQQ_EMA50 | 10d_return | ETH/BTC_ratio_EMA20
    Score: raw(-5..+5) scaled to -10..+10, shifted 1 day (no look-ahead)
    Threshold: ≥+6 long (3/5 bullish), ≤-6 short (3/5 bearish)
    """
    if asset == 'BTC':
        base = btc_d
        s1 = np.where(base > ema(base, 50),  1, -1)                           # BTC mid-trend
        q  = qqq_d.reindex(base.index, method='ffill')
        s2 = np.where(q > ema(q, 50),         1, -1)                          # risk-on (QQQ)
        x  = dxy_d.reindex(base.index, method='ffill')
        s3 = np.where(x < ema(x, 20),         1, -1)                          # DXY weak = BTC +
        s4 = np.where(base.pct_change(10) > 0, 1, -1)                         # 10d momentum
        s5 = np.where(base > ema(base, 200),  1, -1)                          # long-term structure
        raw = pd.Series(s1+s2+s3+s4+s5, index=base.index)
    else:  # ETH
        base = eth_d
        b  = btc_d.reindex(base.index, method='ffill')
        s1 = np.where(base > ema(base, 50),   1, -1)                          # ETH mid-trend
        s2 = np.where(b    > ema(b, 50),       1, -1)                         # BTC structural gate
        q  = qqq_d.reindex(base.index, method='ffill')
        s3 = np.where(q    > ema(q, 50),       1, -1)                         # risk-on (QQQ)
        s4 = np.where(base.pct_change(10) > 0, 1, -1)                         # 10d momentum
        eb = base / b
        s5 = np.where(eb   > ema(eb, 20),      1, -1)                         # alt-season signal
        raw = pd.Series(s1+s2+s3+s4+s5, index=base.index)

    score = (raw / 5 * 10).round(1).shift(1)    # prior-day data only
    dm = pd.DataFrame({'score': score, 'price': base}).dropna()
    return {d.date(): float(r['score']) for d, r in dm.iterrows()}

# ── INDICATORS ─────────────────────────────────────────────────────────────────
def add_indicators(df15):
    df = df15.copy()
    # 15m EMAs
    df['ema9_15m']  = df['close'].ewm(span=9,  adjust=False).mean()
    df['ema21_15m'] = df['close'].ewm(span=21, adjust=False).mean()
    # ATR
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-df['close'].shift()).abs(),
                    (df['low'] -df['close'].shift()).abs()], axis=1).max(axis=1)
    df['atr']     = tr.ewm(span=14, adjust=False).mean()
    df['atr_pct'] = df['atr'] / df['close']
    # 1H EMAs  — built from 15m, forward-filled back to 15m index
    h1 = df['close'].resample('1h').last().dropna()
    df['ema9_1h']  = ema(h1, 9).reindex(df.index,  method='ffill')
    df['ema21_1h'] = ema(h1, 21).reindex(df.index, method='ffill')
    return df

def build_4h(df15):
    g4 = df15.resample('4h', label='left', closed='left').agg(
        {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
    g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
    return g4

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def backtest(df, g4, macro_lkp, sess):
    open_h, open_m   = sess['open_h'], sess['open_m']
    close_h, close_m = sess['close_h'], sess['close_m']
    e_start, e_end   = sess['e_start'], sess['e_end']

    equity = ACCOUNT; trades = []

    def get4h(ts):
        past = g4[g4.index <= ts]
        return past.iloc[-1] if len(past) else None

    for date in sorted(set(df.index.date)):
        score = macro_lkp.get(date)
        # Skip days where macro doesn't have 3/5 factor consensus
        if score is None or SCORE_SHORT < score < SCORE_LONG: continue
        macro_dir = 1 if score >= SCORE_LONG else -1

        ts   = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=open_h,  minutes=open_m)
        c_ts = ts + pd.Timedelta(hours=close_h, minutes=close_m)
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4: continue

        ctx = get4h(o_ts)
        if ctx is None: continue

        # Session-anchored VWAP (resets at session open each day)
        tp_v = (sbars['high']+sbars['low']+sbars['close'])/3
        vol  = sbars['volume'].replace(0, 1.0)
        sbars['vwap']     = (tp_v*vol).cumsum()/vol.cumsum()
        # VWAP std: forward-fill NaN at first bar with ATR (no look-ahead)
        std_s = (tp_v - sbars['vwap']).expanding().std()
        sbars['vwap_std'] = std_s.fillna(sbars['atr']).clip(lower=sbars['atr']*0.1)

        in_trade=False; entry_px=sl=tp1_px=tp2_px=sz1=sz2=pnl1=0
        tp1_hit=False; bars_held=0; trade_dir=0; trades_today=0; last_i=-99

        for i in range(1, len(sbars)):
            b = sbars.iloc[i]
            hi,lo,cls = b['high'],b['low'],b['close']
            bh = b.name.hour + b.name.minute/60

            if in_trade:
                bars_held += 1
                rem = sz2 if tp1_hit else (sz1+sz2)

                if trade_dir == 1:
                    if lo <= sl:                                      # SL hit
                        sl_pnl = (sl-entry_px)*rem
                        equity += sl_pnl
                        trades.append(dict(date=date,dir='LONG',pnl=pnl1+sl_pnl,reason='SL',score=score))
                        in_trade=False; continue
                    if not tp1_hit and hi >= tp1_px:                  # TP1 partial close
                        pnl1=(tp1_px-entry_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True; rem=sz2
                    if tp1_hit and hi >= tp2_px:                      # TP2 full close
                        pnl2=(tp2_px-entry_px)*rem; equity+=pnl2
                        trades.append(dict(date=date,dir='LONG',pnl=pnl1+pnl2,reason='TP2',score=score))
                        in_trade=False; continue
                else:
                    if hi >= sl:
                        sl_pnl = (entry_px-sl)*rem
                        equity += sl_pnl
                        trades.append(dict(date=date,dir='SHORT',pnl=pnl1+sl_pnl,reason='SL',score=score))
                        in_trade=False; continue
                    if not tp1_hit and lo <= tp1_px:
                        pnl1=(entry_px-tp1_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True; rem=sz2
                    if tp1_hit and lo <= tp2_px:
                        pnl2=(entry_px-tp2_px)*rem; equity+=pnl2
                        trades.append(dict(date=date,dir='SHORT',pnl=pnl1+pnl2,reason='TP2',score=score))
                        in_trade=False; continue

                if bars_held >= TIME_STOP:
                    t_pnl = ((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem
                    equity += t_pnl
                    trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',
                                       pnl=pnl1+t_pnl,reason='TIME',score=score))
                    in_trade=False

            else:
                if trades_today >= MAX_TR or i-last_i < MIN_GAP: continue
                if not (e_start <= bh <= e_end): continue

                # ── Volatility gate ──────────────────────────────────────────
                atr_pct = b.get('atr_pct', 0)
                if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX): continue

                # ── VWAP zone ────────────────────────────────────────────────
                vwap=b['vwap']; vstd=max(b['vwap_std'], 0.01)
                dist=(cls-vwap)/vstd
                vwok=(macro_dir==1 and -1.0<=dist<=0.3) or (macro_dir==-1 and -0.3<=dist<=1.0)
                if not vwok: continue

                # ── 4H EMA alignment ─────────────────────────────────────────
                c4=get4h(b.name)
                if c4 is None: continue
                h4ok=(macro_dir==1 and c4['ema9']>c4['ema21']) or \
                     (macro_dir==-1 and c4['ema9']<c4['ema21'])
                if not h4ok: continue

                # ── 1H EMA alignment (new gate) ──────────────────────────────
                h1ok=(macro_dir==1 and b['ema9_1h']>b['ema21_1h']) or \
                     (macro_dir==-1 and b['ema9_1h']<b['ema21_1h'])
                if not h1ok: continue

                # ── 15m EMA alignment ────────────────────────────────────────
                emok=(macro_dir==1 and b['ema9_15m']>b['ema21_15m']) or \
                     (macro_dir==-1 and b['ema9_15m']<b['ema21_15m'])
                if not emok: continue

                atr_v=float(b['atr'])
                if atr_v<=0: continue
                tot_sz=(equity*RISK_PCT)/(SL_MULT*atr_v)
                sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC
                entry_px=cls
                if macro_dir==1:
                    sl=cls-SL_MULT*atr_v; tp1_px=cls+TP1_MULT*atr_v; tp2_px=cls+TP2_MULT*atr_v
                else:
                    sl=cls+SL_MULT*atr_v; tp1_px=cls-TP1_MULT*atr_v; tp2_px=cls-TP2_MULT*atr_v
                in_trade=True; trade_dir=macro_dir; bars_held=0; tp1_hit=False; pnl1=0
                trades_today+=1; last_i=i

        if in_trade:
            cls=sbars.iloc[-1]['close']; rem=sz2 if tp1_hit else (sz1+sz2)
            s_pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem
            equity+=s_pnl
            trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',
                               pnl=pnl1+s_pnl,reason='SESS',score=score))
    return equity, trades

# ── RESULTS FORMATTER ──────────────────────────────────────────────────────────
def show(label, eq, trs, period=""):
    if not trs: print(f"\n{label}: NO TRADES"); return None
    t=pd.DataFrame(trs); t['date']=pd.to_datetime(t['date'])
    t['win']=t['pnl']>0; t['month']=t['date'].dt.to_period('M')
    n=len(t); roi=(eq-ACCOUNT)/ACCOUNT*100; wr=t['win'].mean()*100
    _,pv=stats.ttest_1samp(t['pnl'],0)
    wks=max((t['date'].max()-t['date'].min()).days/7,1); tpw=n/wks
    sharpe=t['pnl'].mean()/t['pnl'].std()*np.sqrt(252) if t['pnl'].std()>0 else 0
    eq_c=ACCOUNT+t['pnl'].cumsum(); mdd=((eq_c-eq_c.cummax())/eq_c.cummax()*100).min()
    lo=t[t['dir']=='LONG']; sh=t[t['dir']=='SHORT']

    print(f"\n{'='*62}")
    print(f"  {label}  {period}")
    print(f"{'='*62}")
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
        print(f"    {mo}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+8,.0f}  ${run_eq:>10,.0f} {flag}")
    print(f"  Positive months: {pos}/{t['month'].nunique()}")
    return dict(label=label, n=n, tpw=tpw, roi=roi, wr=wr,
                sharpe=sharpe, pv=pv, mdd=mdd, eq=eq)

# ── MAIN ───────────────────────────────────────────────────────────────────────
print("="*70)
print("  CRYPTO V2 — QQQ macro | EMA200 | ±6 threshold | 1H gate | vol filter")
print("="*70)

print("\n--- Fetching BTC/USD 15m ---")
btc_15m = fetch_15m("BTC/USD", pages=22)

print("--- Fetching ETH/USD 15m ---")
time.sleep(10)
eth_15m = fetch_15m("ETH/USD", pages=22)

print("Loading macro data (BTC-USD, ETH-USD, QQQ, DX-Y.NYB)...")
btc_d = dl("BTC-USD");  eth_d = dl("ETH-USD")
qqq_d = dl("QQQ");      dxy_d = dl("DX-Y.NYB")

btc_macro = build_macro('BTC', btc_d, eth_d, qqq_d, dxy_d)
eth_macro  = build_macro('ETH', btc_d, eth_d, qqq_d, dxy_d)

print("Building indicators...")
btc_df = add_indicators(btc_15m); btc_4h = build_4h(btc_15m)
eth_df = add_indicators(eth_15m); eth_4h = build_4h(eth_15m)

period = f"({btc_15m.index[0].date()} → {btc_15m.index[-1].date()})"

all_results = []
for sname, sess in SESSIONS.items():
    print(f"\nRunning BTC — {sname} session...")
    eq, trs = backtest(btc_df, btc_4h, btc_macro, sess)
    r = show(f"BTC/USD [{sname}]", eq, trs, period)
    if r: r['session']=sname; r['asset']='BTC'; all_results.append(r)

    print(f"\nRunning ETH — {sname} session...")
    eq, trs = backtest(eth_df, eth_4h, eth_macro, sess)
    r = show(f"ETH/USD [{sname}]", eq, trs, period)
    if r: r['session']=sname; r['asset']='ETH'; all_results.append(r)

# ── FINAL COMPARISON ──────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"  FINAL COMPARISON — V2 (redesigned) vs V1 (broken) vs Gold (proven)")
print(f"{'='*80}")
print(f"  {'Asset & Session':<26} {'N':>5} {'TPW':>5} {'ROI':>9} {'WR':>7} {'Sharpe':>8} {'p-val':>9} {'MDD':>8}")
print(f"  {'─'*77}")
# Reference rows
print(f"  {'GOLD  [NY 13:30–17:00]':<26} {'125':>5} {'2.1':>5} {'+566%':>9} {'47.2%':>7} {'3.30':>8} {'0.022 ✓':>9} {'-28.5%':>8}")
print(f"  {'BTC V1 [NY — broken]':<26} {'466':>5} {'3.0':>5} {'+2249%':>9} {'41.4%':>7} {'0.48':>8} {'0.515 ✗':>9} {'-83.3%':>8}")
print(f"  {'ETH V1 [NY — broken]':<26} {'534':>5} {'3.4':>5} {'+6290%':>9} {'42.3%':>7} {'0.41':>8} {'0.546 ✗':>9} {'-68.7%':>8}")
print(f"  {'─'*77}")
for r in all_results:
    sig = "✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "✗")
    lbl = f"{r['asset']}/USD V2 [{r['session']}]"
    print(f"  {lbl:<26} {r['n']:>5} {r['tpw']:>4.1f} {r['roi']:>+8.1f}% "
          f"{r['wr']:>6.1f}% {r['sharpe']:>8.2f} {r['pv']:>7.4f} {sig}  {r['mdd']:>7.1f}%")
print(f"{'='*80}")
