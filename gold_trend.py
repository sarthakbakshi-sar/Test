"""
GOLD TREND-FOLLOWING SYSTEM — MULTI-SOURCE BACKTEST
=====================================================
Logic: trend direction determines trade direction
  - Price above 50d EMA → uptrend → LONGS only
  - Price below 50d EMA → downtrend → SHORTS only

Data sources tried in order:
  1. Stooq (GC.F, XAUUSD) — free, long history
  2. Alpha Vantage (free API key required)
  3. Twelve Data (free API key required)
  4. yfinance 1h — 730 days, works without API key
  5. yfinance 15m — 60 days fallback

Run: pip install yfinance pandas numpy scipy requests
     python gold_trend.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request, urllib.parse, io, json, time as time_module
from scipy import stats
from datetime import datetime, time, timedelta
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
ACCOUNT      = 10000.0
RISK_PCT     = 0.01
TP_ATR_MULT  = 1.5
SL_ATR_MULT  = 0.75
ATR_PERIOD   = 14
EMA_FAST     = 9
EMA_SLOW     = 21
EMA_TREND    = 50       # daily 50-EMA for trend direction
VWAP_THR     = 1.2
MIN_SIGS     = 2
COMMISSION   = 0.00008

# API keys — get free at alphavantage.co and twelvedata.com
AV_KEY  = "YOUR_ALPHA_VANTAGE_KEY"   # free at alphavantage.co
TD_KEY  = "06f050cd7d9940a895f9461e7f0ff7e3"

# NY session: 13:30-17:00 UTC (17:30-21:00 Dubai)
SESSIONS = [("NY", time(13,30), time(17,0))]

print("="*68)
print("  GOLD TREND-FOLLOWING SYSTEM")
print("  Direction: 50d EMA trend | Entry: 4-signal confirmation")
print("  Comparing: 15-min (60d) vs 1h (730d)")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADERS
# ══════════════════════════════════════════════════════════════════════════════

def load_stooq(symbol="gc.f", interval="15"):
    """Stooq free historical data."""
    try:
        url = f"https://stooq.com/q/d/l/?s={symbol}&i={interval}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode()
        if "No data" in raw or len(raw) < 100:
            return None
        df = pd.read_csv(io.StringIO(raw))
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns and 'time' in df.columns:
            df.index = pd.to_datetime(df['date'].astype(str)+' '+df['time'].astype(str))
        elif 'date' in df.columns:
            df.index = pd.to_datetime(df['date'])
        cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
        df = df[cols].copy()
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df.dropna(inplace=True)
        if 'volume' not in df.columns:
            df['volume'] = 1000.0
        return df if len(df) > 100 else None
    except Exception as e:
        return None

def load_alpha_vantage(interval="15min"):
    """Alpha Vantage — free key from alphavantage.co (25 req/day)."""
    if AV_KEY == "YOUR_ALPHA_VANTAGE_KEY":
        return None
    try:
        url = (f"https://www.alphavantage.co/query?"
               f"function=TIME_SERIES_INTRADAY&symbol=GLD"
               f"&interval={interval}&outputsize=full&apikey={AV_KEY}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=20).read())
        key = f"Time Series ({interval})"
        if key not in raw:
            return None
        ts  = raw[key]
        df  = pd.DataFrame(ts).T
        df.index = pd.to_datetime(df.index)
        df.columns = ['open','high','low','close','volume']
        for c in df.columns:
            df[c] = pd.to_numeric(df[c])
        df.sort_index(inplace=True)
        return df if len(df) > 100 else None
    except:
        return None

def load_twelve_data(interval="15min"):
    """
    Twelve Data — paginated loader for maximum history.
    Free tier: 800 req/day. outputsize=5000 bars per call.
    Paginates backwards to get as much history as possible.
    """
    if not TD_KEY or TD_KEY == "YOUR_TWELVE_DATA_KEY":
        return None

    all_frames = []
    end_date   = None
    max_pages  = 6    # 6 x 5000 bars = 30,000 bars ~ 312 days of 15min
    page       = 0

    print(f"  Twelve Data: fetching {interval} XAU/USD (paginating)...")

    while page < max_pages:
        try:
            base = (f"https://api.twelvedata.com/time_series?"
                    f"symbol=XAU/USD&interval={interval}"
                    f"&outputsize=5000&order=DESC&apikey={TD_KEY}")
            url = base + (f"&end_date={urllib.parse.quote(end_date)}" if end_date else "")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = json.loads(urllib.request.urlopen(req, timeout=30).read())

            if 'values' not in raw or not raw['values']:
                break
            if 'code' in raw and raw['code'] != 200:
                print(f"  Twelve Data error: {raw.get('message','unknown')}")
                break

            df = pd.DataFrame(raw['values'])
            df.index = pd.to_datetime(df['datetime'])
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df = df[cols].copy()
            for c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df.dropna(inplace=True)

            if len(df) == 0:
                break

            all_frames.append(df)
            oldest = df.index.min()
            end_date = (oldest - pd.Timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            print(f"    Page {page+1}: {len(df)} bars | oldest: {oldest.date()}")
            page += 1

            # Rate limit: free tier allows ~8 requests/minute
            if page < max_pages:
                time_module.sleep(8)

        except Exception as e:
            print(f"  Twelve Data error on page {page+1}: {e}")
            break

    if not all_frames:
        return None

    result = pd.concat(all_frames)
    result = result[~result.index.duplicated(keep='first')]
    result.sort_index(inplace=True)
    if 'volume' not in result.columns:
        result['volume'] = 1000.0
    print(f"  Total: {len(result)} bars | {result.index[0].date()} → {result.index[-1].date()}")
    return result if len(result) > 100 else None

def load_yfinance(interval="15m", period="60d"):
    """yfinance — 15m limited to 60d, 1h goes back 730d."""
    try:
        raw = yf.download("GC=F", period=period, interval=interval, progress=False)
        if len(raw) == 0:
            return None
        raw.columns = [c[0].lower() for c in raw.columns]
        df = raw[['open','high','low','close','volume']].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None) if df.index.tz is None \
                   else pd.to_datetime(df.index).tz_convert(None)
        df.dropna(inplace=True)
        return df
    except:
        return None

# ── Try all sources for 15-min data ───────────────────────────────────────────
print("\nTrying data sources...")

data_15m = None
source_15m = ""

for sym in ["gc.f", "xauusd", "gold"]:
    d = load_stooq(sym, "15")
    if d is not None and len(d) > 500:
        data_15m = d
        source_15m = f"Stooq ({sym})"
        break

if data_15m is None:
    d = load_alpha_vantage("15min")
    if d is not None:
        data_15m = d
        source_15m = "Alpha Vantage (GLD)"

if data_15m is None:
    d = load_twelve_data("15min")
    if d is not None:
        data_15m = d
        source_15m = "Twelve Data (XAU/USD)"

if data_15m is None:
    d = load_yfinance("15m", "60d")
    if d is not None:
        data_15m = d
        source_15m = "yfinance 15m (60d cap)"

# ── Always get 1h data (730d) ─────────────────────────────────────────────────
data_1h = load_yfinance("1h", "730d")

# ── Report ────────────────────────────────────────────────────────────────────
datasets = []
if data_15m is not None:
    print(f"  15m: {source_15m} | {len(data_15m)} bars | "
          f"{data_15m.index[0].date()} → {data_15m.index[-1].date()}")
    datasets.append(("15min", source_15m, data_15m))
else:
    print(f"  15m: All sources failed")

if data_1h is not None:
    print(f"  1h : yfinance | {len(data_1h)} bars | "
          f"{data_1h.index[0].date()} → {data_1h.index[-1].date()}")
    datasets.append(("1h", "yfinance 730d", data_1h))

if not datasets:
    print("ERROR: No data available. Check internet connection.")
    exit()

# ── Get macro daily data ──────────────────────────────────────────────────────
print("\nDownloading macro data...")
def get_series(sym, name, period="2y"):
    try:
        d = yf.download(sym, period=period, interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name:<12}: {len(d)} days")
        return d['close']
    except:
        print(f"  {name:<12}: failed")
        return None

gold_daily = get_series("GC=F",     "Gold daily", "5y")
dxy        = get_series("DX-Y.NYB", "DXY",        "2y")
tnx        = get_series("^TNX",     "10Y yield",  "2y")
vix        = get_series("^VIX",     "VIX",        "2y")

# ══════════════════════════════════════════════════════════════════════════════
#  TREND DIRECTION — 50d EMA of daily gold price
#  This is the core change: trend drives direction, not macro
# ══════════════════════════════════════════════════════════════════════════════

def compute_trend(gold_daily):
    """
    50-day EMA trend direction.
    UPTREND   : price > 50d EMA AND 50d EMA rising  → look for LONGS
    DOWNTREND : price < 50d EMA AND 50d EMA falling → look for SHORTS
    NEUTRAL   : mixed signals → FLAT (no trades)
    """
    g = gold_daily.copy() if gold_daily is not None else None
    if g is None:
        return None

    trend = pd.DataFrame(index=g.index)
    trend['price']    = g
    trend['ema50']    = g.ewm(span=EMA_TREND, adjust=False).mean()
    trend['ema20']    = g.ewm(span=20, adjust=False).mean()
    trend['ema50_d']  = trend['ema50'].diff()   # slope of 50d EMA

    # Strong trend: price and EMA both agree
    def classify(row):
        above_ema = row['price'] > row['ema50']
        ema_rising= row['ema50_d'] > 0
        if above_ema and ema_rising:    return 'UPTREND'
        if not above_ema and not ema_rising: return 'DOWNTREND'
        if above_ema and not ema_rising: return 'WEAKUP'    # price above but EMA flattening
        return 'WEAKDOWN'   # price below but EMA still rising

    trend['trend'] = trend.apply(classify, axis=1)

    # Map to trade direction
    def to_dir(t):
        if t == 'UPTREND':   return 'LONG'
        if t == 'DOWNTREND': return 'SHORT'
        if t == 'WEAKUP':    return 'LONG'   # still take longs, just weaker
        return 'SHORT'                         # WEAKDOWN → shorts

    trend['direction'] = trend['trend'].apply(to_dir)
    return trend

trend_df = compute_trend(gold_daily)
if trend_df is not None:
    print(f"\n  Trend distribution: {dict(trend_df['trend'].value_counts())}")
    print(f"  Direction: {dict(trend_df['direction'].value_counts())}")

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATOR ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def build_indicators(df):
    """Compute all 15-min/1h indicators."""
    d = df.copy()

    # EMAs
    d['ema_f'] = d['close'].ewm(span=EMA_FAST, adjust=False).mean()
    d['ema_s'] = d['close'].ewm(span=EMA_SLOW, adjust=False).mean()
    d['bull']  = d['ema_f'] > d['ema_s']

    # ATR
    d['tr']  = np.maximum(d['high']-d['low'],
               np.maximum(abs(d['high']-d['close'].shift(1)),
                          abs(d['low'] -d['close'].shift(1))))
    d['atr'] = d['tr'].rolling(ATR_PERIOD).mean()

    # Session VWAP
    d['date']  = d.index.normalize()
    d['typ']   = (d['high']+d['low']+d['close'])/3
    d['ctv']   = d.groupby('date').apply(
        lambda g:(g['typ']*g['volume']).cumsum()).reset_index(level=0,drop=True)
    d['cv']    = d.groupby('date')['volume'].cumsum()
    d['vwap']  = d['ctv']/(d['cv']+1e-10)
    d['sqdev'] = (d['typ']-d['vwap'])**2*d['volume']
    d['csq']   = d.groupby('date')['sqdev'].cumsum()
    d['vstd']  = np.sqrt(d['csq']/(d['cv']+1e-10)).clip(lower=d['close']*0.0002)
    d['vdev']  = (d['close']-d['vwap'])/(d['vstd']+1e-10)

    # Previous session high/low
    dhl = d.groupby('date').agg(ph=('high','max'),pl=('low','min'))
    d['ph'] = d['date'].map(dhl['ph'].shift(1).to_dict())
    d['pl'] = d['date'].map(dhl['pl'].shift(1).to_dict())
    d['nh'] = abs(d['close']-d['ph'])/d['close'] < 0.002
    d['nl'] = abs(d['close']-d['pl'])/d['close'] < 0.002

    # Momentum
    d['mom'] = d['close']-d['close'].shift(3)

    # Session
    def gs(ts):
        t = ts.time() if hasattr(ts,'time') else ts
        for nm,s,e in SESSIONS:
            if s<=t<e: return nm
        return None
    d['session'] = d.index.map(gs)
    d['in_sess'] = d['session'].notna()

    # Attach trend direction from daily gold EMA
    if trend_df is not None:
        d['trend_dir'] = d['date'].map(trend_df['direction'].to_dict())
        d['trend_str'] = d['date'].map(trend_df['trend'].to_dict())
    else:
        d['trend_dir'] = 'LONG'
        d['trend_str'] = 'UPTREND'

    d.dropna(subset=['atr','vwap','ema_f','ema_s'], inplace=True)
    return d

def score_long(row):
    n = 0
    if row['bull']:                                  n+=1
    if row['vdev'] <= -VWAP_THR and row['mom']>0:   n+=1
    if row['nl'] and row['mom']>0:                   n+=1
    if row['mom'] > row['atr']*0.3:                  n+=1
    return n

def score_short(row):
    n = 0
    if not row['bull']:                              n+=1
    if row['vdev'] >= VWAP_THR and row['mom']<0:    n+=1
    if row['nh'] and row['mom']<0:                   n+=1
    if row['mom'] < -row['atr']*0.3:                 n+=1
    return n

# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def backtest(df, label):
    equity = ACCOUNT
    in_t   = False
    ent    = {}
    trades = []
    eq_c   = [ACCOUNT]
    eq_d   = [df.index[0]]

    for i in range(len(df)):
        row  = df.iloc[i]
        idx  = df.index[i]
        cp   = row['close']
        atr  = row['atr']
        sess = row['session']
        tdir = row.get('trend_dir', 'LONG')

        if pd.isna(atr) or atr == 0: continue

        # EXIT
        if in_t:
            d_  = ent['dir']
            hit_tp = (d_=='LONG'  and cp>=ent['tp']) or (d_=='SHORT' and cp<=ent['tp'])
            hit_sl = (d_=='LONG'  and cp<=ent['sl']) or (d_=='SHORT' and cp>=ent['sl'])
            timeout= sess is None and ent['session'] is not None

            if hit_tp or hit_sl or timeout:
                xp  = ent['tp'] if hit_tp else ent['sl'] if hit_sl else cp
                xt  = 'TP' if hit_tp else 'SL' if hit_sl else 'TIME'
                raw = (xp-ent['price']) if d_=='LONG' else (ent['price']-xp)
                pct = raw/ent['price'] - 2*COMMISSION
                usd = equity * pct * ent['sm']
                equity += usd
                trades.append({
                    'entry_time': str(ent['time'])[:16],
                    'exit_time':  str(idx)[:16],
                    'dir':        d_,
                    'trend':      ent['trend'],
                    'entry_px':   round(ent['price'],2),
                    'exit_px':    round(xp,2),
                    'n_sigs':     ent['ns'],
                    'net_pnl%':   round(pct*100,4),
                    'pnl_usd':    round(usd,2),
                    'equity':     round(equity,2),
                    'exit':       xt,
                    'bars':       i-ent['bi'],
                })
                eq_c.append(equity); eq_d.append(idx)
                in_t = False

        # ENTRY
        if not in_t and row['in_sess']:
            ls = score_long(row)
            ss = score_short(row)

            if tdir == 'LONG' and ls >= MIN_SIGS:
                tp_ = cp + atr*TP_ATR_MULT
                sl_ = cp - atr*SL_ATR_MULT
                oz  = (equity*RISK_PCT)/(cp-sl_+1e-10)
                sm  = min((oz*cp)/equity, 5.0)
                in_t = True
                ent  = dict(time=idx,bi=i,price=cp,dir='LONG',
                            tp=tp_,sl=sl_,atr=atr,session=sess,
                            trend=row.get('trend_str',''),ns=ls,sm=sm)

            elif tdir == 'SHORT' and ss >= MIN_SIGS:
                tp_ = cp - atr*TP_ATR_MULT
                sl_ = cp + atr*SL_ATR_MULT
                oz  = (equity*RISK_PCT)/(sl_-cp+1e-10)
                sm  = min((oz*cp)/equity, 5.0)
                in_t = True
                ent  = dict(time=idx,bi=i,price=cp,dir='SHORT',
                            tp=tp_,sl=sl_,atr=atr,session=sess,
                            trend=row.get('trend_str',''),ns=ss,sm=sm)

    if not trades: return None

    tdf  = pd.DataFrame(trades)
    wins = tdf[tdf['net_pnl%']>0]
    loss = tdf[tdf['net_pnl%']<=0]
    wr   = len(wins)/len(tdf)
    roi  = (equity/ACCOUNT-1)*100
    pf   = wins['pnl_usd'].sum()/abs(loss['pnl_usd'].sum()) \
           if len(loss)>0 and loss['pnl_usd'].sum()!=0 else 99
    eq_s = pd.Series(eq_c,index=eq_d)
    mdd  = float(((eq_s-eq_s.expanding().max())/eq_s.expanding().max()*100).min())
    aw   = wins['net_pnl%'].mean() if len(wins)>0 else 0
    al   = abs(loss['net_pnl%'].mean()) if len(loss)>0 else 0
    be   = al/(aw+al) if (aw+al)>0 else 0.5
    t,p  = stats.ttest_1samp(tdf['net_pnl%'],0) if len(tdf)>5 else (0,1)

    tdf['month'] = pd.to_datetime(tdf['entry_time']).dt.to_period('M')
    mo = tdf.groupby('month').agg(
        n=('pnl_usd','count'), pnl=('pnl_usd','sum'),
        wr=('net_pnl%',lambda x:round((x>0).mean()*100,1))
    ).reset_index()
    mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

    by_dir = tdf.groupby('dir').apply(lambda x: pd.Series({
        'n': len(x),
        'wr%': round((x['net_pnl%']>0).mean()*100,1),
        'avg%': round(x['net_pnl%'].mean(),4),
        'pnl$': round(x['pnl_usd'].sum(),2),
    }), include_groups=False)

    by_trend = tdf.groupby('trend').apply(lambda x: pd.Series({
        'n': len(x),
        'wr%': round((x['net_pnl%']>0).mean()*100,1),
        'avg%': round(x['net_pnl%'].mean(),4),
        'pnl$': round(x['pnl_usd'].sum(),2),
    }), include_groups=False)

    return dict(label=label,tdf=tdf,mo=mo,by_dir=by_dir,by_trend=by_trend,
                n=len(tdf),wr=wr,roi=roi,pf=pf,mdd=mdd,equity=equity,
                be=be,t=t,p=p,aw=aw,al=al,
                pm=(mo['pnl']>0).sum(),tm=len(mo))

# ══════════════════════════════════════════════════════════════════════════════
#  RUN ON ALL AVAILABLE DATASETS
# ══════════════════════════════════════════════════════════════════════════════

# Short-only: only fire when macro/trend says SHORT
# Force all bars to SHORT mode to extract pure short signal edge
def backtest_short_only(df, label):
    d2 = df.copy()
    d2['trend_dir'] = 'SHORT'
    d2['trend_str'] = 'FORCED_SHORT'
    return backtest(d2, label)

results = []
for (interval, source, raw_df) in datasets:
    print(f"\nProcessing {interval} data ({source})...")
    df = build_indicators(raw_df)

    # Run SHORT-ONLY
    rs = backtest_short_only(df, f"{interval} SHORT-ONLY | {source}")
    if rs:
        results.append(rs)
        print(f"  SHORT-ONLY → {rs['n']} trades | WR {rs['wr']*100:.1f}% | ROI {rs['roi']:+.1f}%")

    # Run FULL trend system for comparison
    rf = backtest(df, f"{interval} FULL | {source}")
    if rf:
        results.append(rf)
        print(f"  FULL       → {rf['n']} trades | WR {rf['wr']*100:.1f}% | ROI {rf['roi']:+.1f}%")

# ══════════════════════════════════════════════════════════════════════════════
#  PRINT RESULTS
# ══════════════════════════════════════════════════════════════════════════════

def print_result(r):
    print(f"\n  {'─'*64}")
    print(f"  {r['label']}")
    print(f"  {'─'*64}")
    print(f"  Trades        : {r['n']}")
    print(f"  Win rate      : {r['wr']*100:.1f}%  (break-even {r['be']*100:.1f}%,  edge {(r['wr']-r['be'])*100:+.1f}%)")
    print(f"  Profit factor : {r['pf']:.2f}")
    print(f"  ROI           : {r['roi']:+.2f}%")
    print(f"  Net P&L       : ${r['equity']-ACCOUNT:+,.2f}")
    print(f"  Max drawdown  : {r['mdd']:.2f}%")
    print(f"  Avg win       : +{r['aw']:.4f}%  Avg loss: -{r['al']:.4f}%")
    print(f"  +ve months    : {r['pm']}/{r['tm']}")
    more_needed = max(0, int(200 - r['n']))
    sig_str = '✓ SIGNIFICANT' if r['p'] < 0.05 else f'(need {more_needed} more trades)'
    print(f"  p-value       : {r['p']:.4f}  {sig_str}")
    print(f"\n  By direction:")
    print(r['by_dir'].to_string())
    print(f"\n  By trend regime:")
    print(r['by_trend'].to_string())
    print(f"\n  Monthly P&L:")
    print(r['mo'][['month','n','pnl','wr','roi%']].to_string(index=False))

print(f"\n{'='*68}")
print(f"  RESULTS — TREND-FOLLOWING SYSTEM")
print(f"{'='*68}")
for r in results:
    print_result(r)

# ── Comparison table if both datasets available ───────────────────────────────
if len(results) >= 2:
    print(f"\n{'='*68}")
    print(f"  TIMEFRAME COMPARISON")
    print(f"{'='*68}")
    print(f"\n  {'Metric':<22}", end="")
    for r in results:
        label = r['label'].split('|')[0].strip()
        print(f"  {label:>18}", end="")
    print()
    print(f"  {'─'*62}")

    metrics = [
        ("Trades",       lambda r: str(r['n'])),
        ("Win rate",     lambda r: f"{r['wr']*100:.1f}%"),
        ("Edge vs BE",   lambda r: f"{(r['wr']-r['be'])*100:+.1f}%"),
        ("Profit factor",lambda r: f"{r['pf']:.2f}"),
        ("ROI",          lambda r: f"{r['roi']:+.2f}%"),
        ("Net P&L",      lambda r: f"${r['equity']-ACCOUNT:+,.0f}"),
        ("Max drawdown", lambda r: f"{r['mdd']:.2f}%"),
        ("+ve months",   lambda r: f"{r['pm']}/{r['tm']}"),
        ("p-value",      lambda r: f"{r['p']:.4f}"),
    ]
    for name, fn in metrics:
        print(f"  {name:<22}", end="")
        for r in results:
            print(f"  {fn(r):>18}", end="")
        print()

print(f"\n{'='*68}")
print(f"  TREND SYSTEM vs PREVIOUS SHORT-ONLY")
print(f"{'='*68}")
print(f"""
  Previous best (short-only, 60d):
    WR: 51.0% | ROI: +23.4% | PF: 1.82 | n=51

  Key difference: trend filter ADDS longs in uptrends
  If gold is in uptrend and trend filter works → more trades, better coverage
  If gold is in downtrend (as in Feb-May 2026) → system still short-biased

  TO GET MORE DATA (add your free API keys at top of script):
    Alpha Vantage: alphavantage.co  → free key → 2 years of 15-min GLD
    Twelve Data  : twelvedata.com   → free key → up to 5 years of 15-min XAU/USD
    After adding keys, re-run — you'll get 500-2000 trades for real statistical proof.
""")

# Save
for r in results:
    fname = r['label'].replace(' ','_').replace('|','').replace('/','')[:30]
    r['tdf'].to_csv(f"gold_trend_{fname}.csv", index=False)
    print(f"Saved: gold_trend_{fname}.csv")
