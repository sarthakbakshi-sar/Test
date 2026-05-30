"""
BTC ETF-GATED BACKTEST (V3)
============================
Same as V2 (QQQ macro, ±6 threshold, 1H EMA gate, vol filter, 2% risk, NY session)
PLUS: Farside ETF netflow gate from Jan 11 2024 (Bitcoin spot ETF launch date)

ETF gate (uses prior-day 5d cumulative net flow, shifts 1 day to avoid look-ahead):
  Long  entry: 5d net flow > 0  (net inflows)
  Short entry: 5d net flow < 0  (net outflows)
  Before Jan 11 2024: no ETF gate (same as V2)

Data: 15m BTC cached to CSV to avoid repeat API downloads.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import requests, time, urllib.parse, os, warnings
from io import StringIO
from datetime import datetime, timedelta
warnings.filterwarnings('ignore')

API_KEY     = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT     = 10_000.0
RISK_PCT    = 0.02
SL_MULT     = 0.75
TP1_MULT    = 1.5
TP2_MULT    = 2.5
TP1_FRAC    = 0.60
TP2_FRAC    = 0.40
TIME_STOP   = 6
MAX_TR      = 2
MIN_GAP     = 4
SCORE_LONG  =  6.0
SCORE_SHORT = -6.0
ATR_PCT_MIN = 0.0010
ATR_PCT_MAX = 0.0150
ETF_LAUNCH  = pd.Timestamp('2024-01-11')
CACHE       = '/home/user/Test/btc_15m_cache.csv'
SESS        = dict(open_h=13, open_m=30, close_h=17, close_m=0, e_start=14.0, e_end=16.5)

# ── ETF FLOW DATA ──────────────────────────────────────────────────────────────
def parse_flow_val(v):
    """Convert '655.3' → 655.3 and '(95.1)' → -95.1."""
    if pd.isna(v): return np.nan
    s = str(v).strip()
    if s.startswith('(') and s.endswith(')'):
        try: return -float(s[1:-1].replace(',', ''))
        except: return np.nan
    try: return float(s.replace(',', ''))
    except: return np.nan

def fetch_etf_flows():
    """
    Fetch daily BTC ETF total net flows from Farside Investors.
    Returns Series indexed by date, values in $M (positive = net inflow).
    Gate applied using 5-day rolling sum, shifted 1 day (prior-day data only).
    """
    print("Fetching ETF flow data from Farside...")
    r = requests.get(
        'https://farside.co.uk/bitcoin-etf-flow/',
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
        timeout=25, verify=False
    )
    tables = pd.read_html(StringIO(r.text))
    # Table 1 has the ETF data (Date, IBIT, FBTC, ..., Total)
    df = None
    for t in tables:
        if 'Total' in t.columns and 'IBIT' in t.columns:
            df = t.copy(); break
    if df is None:
        raise RuntimeError("Could not find ETF table on Farside page")

    df['Date'] = pd.to_datetime(df['Date'], errors='coerce', dayfirst=True)
    df = df.dropna(subset=['Date'])
    df = df.sort_values('Date').set_index('Date')
    df['total_flow'] = df['Total'].apply(parse_flow_val)
    df = df[['total_flow']].dropna()

    print(f"  ETF flow data: {df.index[0].date()} → {df.index[-1].date()} ({len(df)} trading days)")
    print(f"  Inflow days: {(df['total_flow']>0).sum()}  |  Outflow days: {(df['total_flow']<0).sum()}")
    print(f"  Total period net: ${df['total_flow'].sum():+,.0f}M")
    print(f"  Largest inflow day:  ${df['total_flow'].max():+,.0f}M on {df['total_flow'].idxmax().date()}")
    print(f"  Largest outflow day: ${df['total_flow'].min():+,.0f}M on {df['total_flow'].idxmin().date()}")

    # 5-day rolling sum, shifted 1 day (use prior-day data, no look-ahead)
    flow_5d = df['total_flow'].rolling(5, min_periods=1).sum().shift(1)
    return flow_5d   # Series: date → 5d net flow

# ── 15m DATA (cached) ──────────────────────────────────────────────────────────
def fetch_15m(symbol, pages=22):
    all_frames = []; end_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for page in range(pages):
        url = (f"https://api.twelvedata.com/time_series?symbol={urllib.parse.quote(symbol)}"
               f"&interval=15min&outputsize=5000&apikey={API_KEY}"
               f"&end_date={urllib.parse.quote(end_date)}&timezone=UTC")
        j = requests.get(url, timeout=30).json()
        if 'values' not in j:
            print(f"  page {page+1}: stopped ({j.get('message','')})"); break
        dfp = pd.DataFrame(j['values'])
        dfp['datetime'] = pd.to_datetime(dfp['datetime'])
        dfp.set_index('datetime', inplace=True); dfp.sort_index(inplace=True)
        for c in ['open','high','low','close','volume']:
            if c in dfp.columns: dfp[c] = pd.to_numeric(dfp[c], errors='coerce')
        dfp.dropna(subset=['open','high','low','close'], inplace=True)
        dfp['volume'] = dfp.get('volume', pd.Series(1.0, index=dfp.index)).fillna(1.0).replace(0, 1.0)
        all_frames.append(dfp)
        print(f"  page {page+1}: {dfp.index[0].date()} → {dfp.index[-1].date()}  ({len(dfp)} bars)")
        end_date = (dfp.index[0] - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        if page < pages-1: time.sleep(8)
    df = pd.concat(all_frames).sort_index()
    return df[~df.index.duplicated(keep='first')]

def load_btc_15m():
    if os.path.exists(CACHE):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        print(f"  Loaded from cache: {df.index[0].date()} → {df.index[-1].date()} ({len(df)} bars)")
        return df
    print("  Cache miss — downloading from Twelve Data...")
    df = fetch_15m("BTC/USD", pages=22)
    print(f"  Total: {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
    df.to_csv(CACHE)
    print(f"  Saved to {CACHE}")
    return df

# ── MACRO & INDICATORS ─────────────────────────────────────────────────────────
def dl(ticker):
    d = yf.download(ticker, period='4y', interval='1d', progress=False, auto_adjust=True)
    d.index = pd.to_datetime(d.index).tz_localize(None)
    if isinstance(d.columns, pd.MultiIndex): d.columns = [str(c[0]).lower() for c in d.columns]
    else: d.columns = [str(c).lower() for c in d.columns]
    for col in ['close','adj close']:
        if col in d.columns: return d[col].dropna()
    return d.iloc[:,3].dropna()

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def build_macro(btc_d, eth_d, qqq_d, dxy_d):
    s1 = np.where(btc_d > ema(btc_d, 50),  1, -1)
    q  = qqq_d.reindex(btc_d.index, method='ffill')
    s2 = np.where(q > ema(q, 50),           1, -1)
    x  = dxy_d.reindex(btc_d.index, method='ffill')
    s3 = np.where(x < ema(x, 20),           1, -1)
    s4 = np.where(btc_d.pct_change(10) > 0, 1, -1)
    s5 = np.where(btc_d > ema(btc_d, 200),  1, -1)
    raw = pd.Series(s1+s2+s3+s4+s5, index=btc_d.index)
    score = (raw/5*10).round(1).shift(1)
    dm = pd.DataFrame({'score': score, 'price': btc_d}).dropna()
    return {d.date(): float(r['score']) for d, r in dm.iterrows()}

def add_indicators(df15):
    df = df15.copy()
    df['ema9_15m']  = df['close'].ewm(span=9,  adjust=False).mean()
    df['ema21_15m'] = df['close'].ewm(span=21, adjust=False).mean()
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-df['close'].shift()).abs(),
                    (df['low']-df['close'].shift()).abs()], axis=1).max(axis=1)
    df['atr']     = tr.ewm(span=14, adjust=False).mean()
    df['atr_pct'] = df['atr'] / df['close']
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
def backtest(df, g4, macro_lkp, flow_5d=None, use_etf_gate=True):
    """
    flow_5d: Series(date → $M 5d net flow, prior-day shifted).
    If use_etf_gate=True and date >= ETF_LAUNCH:
      Longs  blocked when flow_5d < 0 (net outflows)
      Shorts blocked when flow_5d > 0 (net inflows)
    """
    open_h, open_m   = SESS['open_h'], SESS['open_m']
    close_h, close_m = SESS['close_h'], SESS['close_m']
    e_start, e_end   = SESS['e_start'], SESS['e_end']

    equity = ACCOUNT; trades = []

    def get4h(ts):
        past = g4[g4.index <= ts]
        return past.iloc[-1] if len(past) else None

    def etf_allows(date, macro_dir):
        if not use_etf_gate: return True
        ts = pd.Timestamp(date)
        if ts < ETF_LAUNCH: return True            # no gate before ETF launch
        if flow_5d is None: return True
        try:
            flow = flow_5d.get(ts, np.nan)
            if pd.isna(flow): flow = flow_5d.asof(ts)
        except:
            return True
        if pd.isna(flow): return True
        if macro_dir == 1  and flow < 0: return False   # outflow day → no longs
        if macro_dir == -1 and flow > 0: return False   # inflow day  → no shorts
        return True

    for date in sorted(set(df.index.date)):
        score = macro_lkp.get(date)
        if score is None or SCORE_SHORT < score < SCORE_LONG: continue
        macro_dir = 1 if score >= SCORE_LONG else -1

        # ETF gate check (whole-day filter)
        if not etf_allows(date, macro_dir): continue

        ts   = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=open_h,  minutes=open_m)
        c_ts = ts + pd.Timedelta(hours=close_h, minutes=close_m)
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4: continue

        ctx = get4h(o_ts)
        if ctx is None: continue

        tp_v = (sbars['high']+sbars['low']+sbars['close'])/3
        vol  = sbars['volume'].replace(0, 1.0)
        sbars['vwap']     = (tp_v*vol).cumsum()/vol.cumsum()
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
                    if lo <= sl:
                        sl_pnl=(sl-entry_px)*rem; equity+=sl_pnl
                        trades.append(dict(date=date,dir='LONG',pnl=pnl1+sl_pnl,reason='SL',score=score))
                        in_trade=False; continue
                    if not tp1_hit and hi >= tp1_px:
                        pnl1=(tp1_px-entry_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True; rem=sz2
                    if tp1_hit and hi >= tp2_px:
                        pnl2=(tp2_px-entry_px)*rem; equity+=pnl2
                        trades.append(dict(date=date,dir='LONG',pnl=pnl1+pnl2,reason='TP2',score=score))
                        in_trade=False; continue
                else:
                    if hi >= sl:
                        sl_pnl=(entry_px-sl)*rem; equity+=sl_pnl
                        trades.append(dict(date=date,dir='SHORT',pnl=pnl1+sl_pnl,reason='SL',score=score))
                        in_trade=False; continue
                    if not tp1_hit and lo <= tp1_px:
                        pnl1=(entry_px-tp1_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True; rem=sz2
                    if tp1_hit and lo <= tp2_px:
                        pnl2=(entry_px-tp2_px)*rem; equity+=pnl2
                        trades.append(dict(date=date,dir='SHORT',pnl=pnl1+pnl2,reason='TP2',score=score))
                        in_trade=False; continue
                if bars_held >= TIME_STOP:
                    t_pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem; equity+=t_pnl
                    trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',
                                       pnl=pnl1+t_pnl,reason='TIME',score=score)); in_trade=False
            else:
                if trades_today>=MAX_TR or i-last_i<MIN_GAP: continue
                if not (e_start<=bh<=e_end): continue
                atr_pct=b.get('atr_pct',0)
                if not (ATR_PCT_MIN<=atr_pct<=ATR_PCT_MAX): continue
                vwap=b['vwap']; vstd=max(b['vwap_std'],0.01); dist=(cls-vwap)/vstd
                vwok=(macro_dir==1 and -1.0<=dist<=0.3) or (macro_dir==-1 and -0.3<=dist<=1.0)
                if not vwok: continue
                c4=get4h(b.name)
                if c4 is None: continue
                if not ((macro_dir==1 and c4['ema9']>c4['ema21']) or
                        (macro_dir==-1 and c4['ema9']<c4['ema21'])): continue
                if not ((macro_dir==1 and b['ema9_1h']>b['ema21_1h']) or
                        (macro_dir==-1 and b['ema9_1h']<b['ema21_1h'])): continue
                if not ((macro_dir==1 and b['ema9_15m']>b['ema21_15m']) or
                        (macro_dir==-1 and b['ema9_15m']<b['ema21_15m'])): continue
                atr_v=float(b['atr'])
                if atr_v<=0: continue
                tot_sz=(equity*RISK_PCT)/(SL_MULT*atr_v)
                sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC; entry_px=cls
                if macro_dir==1:
                    sl=cls-SL_MULT*atr_v; tp1_px=cls+TP1_MULT*atr_v; tp2_px=cls+TP2_MULT*atr_v
                else:
                    sl=cls+SL_MULT*atr_v; tp1_px=cls-TP1_MULT*atr_v; tp2_px=cls-TP2_MULT*atr_v
                in_trade=True; trade_dir=macro_dir; bars_held=0; tp1_hit=False; pnl1=0
                trades_today+=1; last_i=i

        if in_trade:
            cls=sbars.iloc[-1]['close']; rem=sz2 if tp1_hit else (sz1+sz2)
            s_pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem; equity+=s_pnl
            trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',
                               pnl=pnl1+s_pnl,reason='SESS',score=score))
    return equity, trades

# ── RESULTS ────────────────────────────────────────────────────────────────────
def show(label, eq, trs, period="", start_eq=ACCOUNT):
    if not trs: print(f"\n{label}: NO TRADES"); return None
    t=pd.DataFrame(trs); t['date']=pd.to_datetime(t['date'])
    t['win']=t['pnl']>0; t['month']=t['date'].dt.to_period('M')
    n=len(t); roi=(eq-start_eq)/start_eq*100; wr=t['win'].mean()*100
    _,pv=stats.ttest_1samp(t['pnl'],0)
    wks=max((t['date'].max()-t['date'].min()).days/7,1); tpw=n/wks
    sharpe=t['pnl'].mean()/t['pnl'].std()*np.sqrt(252) if t['pnl'].std()>0 else 0
    eq_c=start_eq+t['pnl'].cumsum(); mdd=((eq_c-eq_c.cummax())/eq_c.cummax()*100).min()
    lo=t[t['dir']=='LONG']; sh=t[t['dir']=='SHORT']
    print(f"\n{'='*62}")
    print(f"  {label}  {period}")
    print(f"{'='*62}")
    print(f"  ${start_eq:,.0f}  →  ${eq:,.0f}  ({roi:+.1f}% ROI)")
    print(f"  Sharpe   : {sharpe:.2f}    Max DD: {mdd:.1f}%")
    print(f"  Trades   : {n}  ({tpw:.1f}/week)   WR: {wr:.1f}%")
    print(f"  p-value  : {pv:.4f}  {'✓ SIGNIFICANT' if pv<0.05 else '~ borderline' if pv<0.10 else '— not significant'}")
    if len(lo): print(f"  Longs  {len(lo):>4}tr  WR {lo['win'].mean()*100:.0f}%  ${lo['pnl'].sum():+,.0f}")
    if len(sh): print(f"  Shorts {len(sh):>4}tr  WR {sh['win'].mean()*100:.0f}%  ${sh['pnl'].sum():+,.0f}")
    print(f"\n  Monthly:")
    run_eq=start_eq; pos=0
    for mo,g in t.groupby('month'):
        run_eq+=g['pnl'].sum(); flag="✓" if g['pnl'].sum()>0 else "✗"
        if g['pnl'].sum()>0: pos+=1
        print(f"    {mo}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+8,.0f}  ${run_eq:>10,.0f} {flag}")
    print(f"  Positive months: {pos}/{t['month'].nunique()}")
    return dict(label=label, n=n, tpw=tpw, roi=roi, wr=wr, sharpe=sharpe, pv=pv, mdd=mdd, eq=eq)

# ── MAIN ───────────────────────────────────────────────────────────────────────
print("="*70)
print("  BTC ETF-GATED BACKTEST (V3)")
print("  Farside netflows | same V2 system | gate from Jan 11 2024")
print("="*70)

# ETF flows
flow_5d = fetch_etf_flows()

# 15m data (cached)
print("\n--- BTC/USD 15m ---")
btc_raw = load_btc_15m()
period_full = f"({btc_raw.index[0].date()} → {btc_raw.index[-1].date()})"

# Macro
print("\nLoading macro data...")
btc_d=dl("BTC-USD"); eth_d=dl("ETH-USD")
qqq_d=dl("QQQ");     dxy_d=dl("DX-Y.NYB")
macro = build_macro(btc_d, eth_d, qqq_d, dxy_d)

# Indicators
print("Building indicators...")
df = add_indicators(btc_raw); g4 = build_4h(btc_raw)

# ── RUN 1: V2 baseline (no ETF gate) — full period ───────────────────────────
print("\nRunning V2 baseline (no ETF gate)...")
eq_v2, trs_v2 = backtest(df, g4, macro, flow_5d=None, use_etf_gate=False)
r_v2 = show("BTC V2 — No ETF gate [full period]", eq_v2, trs_v2, period_full)

# ── RUN 2: V3 (with ETF gate) — full period ──────────────────────────────────
print("\nRunning V3 with Farside ETF gate...")
eq_v3, trs_v3 = backtest(df, g4, macro, flow_5d=flow_5d, use_etf_gate=True)
r_v3 = show("BTC V3 — Farside ETF gate [full period]", eq_v3, trs_v3, period_full)

# ── RUN 3: 2024+ only — V2 vs V3 side-by-side ───────────────────────────────
etf_start = ETF_LAUNCH.date()
df_24   = df[df.index.date >= etf_start]
g4_24   = g4[g4.index.date >= etf_start]
macro_24 = {d: s for d, s in macro.items() if d >= etf_start}
period_24 = f"({etf_start} → {btc_raw.index[-1].date()})"

print(f"\nRunning 2024+ only — V2 (no ETF gate)...")
eq_v2_24, trs_v2_24 = backtest(df_24, g4_24, macro_24, flow_5d=None, use_etf_gate=False)
r_v2_24 = show("BTC V2 [2024+ only, no ETF gate]", eq_v2_24, trs_v2_24, period_24)

print(f"\nRunning 2024+ only — V3 (ETF gate)...")
eq_v3_24, trs_v3_24 = backtest(df_24, g4_24, macro_24, flow_5d=flow_5d, use_etf_gate=True)
r_v3_24 = show("BTC V3 [2024+ only, ETF gate]", eq_v3_24, trs_v3_24, period_24)

# ── COMPARISON TABLE ──────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"  COMPARISON")
print(f"{'='*80}")
print(f"  {'System':<38} {'N':>5} {'TPW':>4} {'ROI':>8} {'WR':>6} {'Sharpe':>7} {'p-val':>8} {'MDD':>7}")
print(f"  {'─'*77}")
# Reference gold
print(f"  {'GOLD V1 [NY, full 13m]':<38} {'125':>5} {'2.1':>4} {'+566%':>8} {'47.2%':>6} {'3.30':>7} {'0.022 ✓':>8} {'-28.5%':>7}")
for r in [r_v2, r_v3, r_v2_24, r_v3_24]:
    if r is None: continue
    sig = "✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "✗")
    print(f"  {r['label']:<38} {r['n']:>5} {r['tpw']:>3.1f} {r['roi']:>+7.1f}% "
          f"{r['wr']:>5.1f}% {r['sharpe']:>7.2f} {r['pv']:>6.4f} {sig}  {r['mdd']:>6.1f}%")
print(f"{'='*80}")

# ETF gate impact summary
if r_v2 and r_v3:
    removed = r_v2['n'] - r_v3['n']
    print(f"\n  ETF gate removed {removed} trades ({removed/r_v2['n']*100:.0f}% of V2 total)")
    print(f"  WR change:     {r_v2['wr']:.1f}% → {r_v3['wr']:.1f}%  ({r_v3['wr']-r_v2['wr']:+.1f}pp)")
    print(f"  Sharpe change: {r_v2['sharpe']:.2f} → {r_v3['sharpe']:.2f}")
    print(f"  MDD change:    {r_v2['mdd']:.1f}% → {r_v3['mdd']:.1f}%")
    print(f"  p-value:       {r_v2['pv']:.4f} → {r_v3['pv']:.4f}")
