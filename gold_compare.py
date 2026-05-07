"""
GOLD 15-MIN SYSTEM — SCENARIO COMPARISON
==========================================
Scenario A: LONG + SHORT trades
Scenario B: SHORT only

Run: pip install yfinance pandas numpy scipy && python gold_compare.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request, io
from scipy import stats
from datetime import time
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
VWAP_SIG_THR = 1.2
MIN_SIGNALS  = 2
COMMISSION   = 0.00008
LOOKBACK     = "60d"

# NY session only (13:30-17:00 UTC = 17:30-21:00 Dubai)
SESSIONS = [("NY", time(13, 30), time(17, 0))]

# ── STEP 1: LOAD DATA ─────────────────────────────────────────────────────────
print("="*68)
print("  GOLD 15-MIN — SCENARIO COMPARISON")
print("  Scenario A: LONG + SHORT  vs  Scenario B: SHORT only")
print("="*68)
print("\nDownloading data...")

def load_stooq_gold():
    try:
        url = "https://stooq.com/q/d/l/?s=gc.f&i=15"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode()
        df  = pd.read_csv(io.StringIO(raw))
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns and 'time' in df.columns:
            df.index = pd.to_datetime(df['date'].astype(str) + ' ' + df['time'].astype(str))
        elif 'date' in df.columns:
            df.index = pd.to_datetime(df['date'])
        df = df[['open','high','low','close','volume']].dropna()
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"  Stooq failed: {e}")
        return None

stooq = load_stooq_gold()
if stooq is not None and len(stooq) > 500:
    gold = stooq
    print(f"  Stooq gold : {len(gold)} bars | {gold.index[0].date()} → {gold.index[-1].date()}")
else:
    print("  Stooq unavailable — using yfinance (60d cap)")
    gold = yf.download("GC=F", period=LOOKBACK, interval="15m", progress=False)
    gold.columns = [c[0].lower() for c in gold.columns]
    gold = gold[['open','high','low','close','volume']].copy()
    gold.index = pd.to_datetime(gold.index).tz_localize(None) if gold.index.tz is None \
                 else pd.to_datetime(gold.index).tz_convert(None)
gold.dropna(inplace=True)
print(f"  Price range: ${gold['close'].min():.0f} – ${gold['close'].max():.0f}")

def get_daily(sym, name):
    try:
        d = yf.download(sym, period=LOOKBACK, interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index   = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name:<12}: {len(d)} days | last: {d['close'].iloc[-1]:.3f}")
        return d['close']
    except:
        return None

dxy  = get_daily("DX-Y.NYB", "DXY")
tnx  = get_daily("^TNX",     "10Y yield")
vix  = get_daily("^VIX",     "VIX")
tips = get_daily("TIP",      "TIPS ETF")

# ── STEP 2: MACRO BIAS ────────────────────────────────────────────────────────
print("\nComputing macro bias...")
dates = pd.DatetimeIndex(sorted(set(gold.index.normalize())))
macro = pd.DataFrame(index=dates)

if tnx  is not None: macro['tnx_5d']  = tnx.pct_change(5).reindex(macro.index, method='ffill')
else:                macro['tnx_5d']  = 0.0
if tips is not None: macro['tips_5d'] = tips.pct_change(5).reindex(macro.index, method='ffill')
else:                macro['tips_5d'] = 0.0
if dxy  is not None: macro['dxy_3d']  = dxy.pct_change(3).reindex(macro.index, method='ffill')
else:                macro['dxy_3d']  = 0.0
if vix  is not None: macro['vix_1d']  = vix.pct_change(1).reindex(macro.index, method='ffill')
else:                macro['vix_1d']  = 0.0

macro['score'] = (
    -(macro['tnx_5d'] * 2 - macro['tips_5d']) * 0.45 +
    -macro['dxy_3d'] * 0.35 +
     macro['vix_1d'] * 0.20 * 0.5
)
roll_std = macro['score'].rolling(30, min_periods=5).std()
macro['score_z'] = (macro['score'] / (roll_std + 1e-10)).clip(-2, 2) / 2
macro['bias'] = macro['score_z'].apply(
    lambda x: 'LONG' if x > 0.05 else ('SHORT' if x < -0.05 else 'FLAT')
)
print(f"  Bias dist: {dict(macro['bias'].value_counts())}")

# ── STEP 3: INDICATORS ────────────────────────────────────────────────────────
print("Computing indicators...")
df = gold.copy()
df['ema_fast']  = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
df['ema_slow']  = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
df['ema_bull']  = df['ema_fast'] > df['ema_slow']

df['tr'] = np.maximum(df['high']-df['low'],
           np.maximum(abs(df['high']-df['close'].shift(1)),
                      abs(df['low'] -df['close'].shift(1))))
df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()

df['date']    = df.index.normalize()
df['typical'] = (df['high'] + df['low'] + df['close']) / 3
df['cum_tv']  = df.groupby('date').apply(
    lambda g: (g['typical'] * g['volume']).cumsum()
).reset_index(level=0, drop=True)
df['cum_v']   = df.groupby('date')['volume'].cumsum()
df['vwap']    = df['cum_tv'] / (df['cum_v'] + 1e-10)
df['sq_dev']  = (df['typical'] - df['vwap'])**2 * df['volume']
df['cum_sq']  = df.groupby('date')['sq_dev'].cumsum()
df['vwap_std']= np.sqrt(df['cum_sq'] / (df['cum_v'] + 1e-10)).clip(lower=df['close']*0.0003)
df['vwap_dev']= (df['close'] - df['vwap']) / (df['vwap_std'] + 1e-10)

daily_hl   = df.groupby('date').agg(dh=('high','max'), dl=('low','min'))
prev_h     = daily_hl['dh'].shift(1)
prev_l     = daily_hl['dl'].shift(1)
df['prev_h']= df['date'].map(prev_h.to_dict())
df['prev_l']= df['date'].map(prev_l.to_dict())
df['near_h']= abs(df['close'] - df['prev_h']) / df['close'] < 0.0015
df['near_l']= abs(df['close'] - df['prev_l']) / df['close'] < 0.0015
df['mom3']  = df['close'] - df['close'].shift(3)

def get_sess(ts):
    t = ts.time() if hasattr(ts,'time') else ts
    for name,s,e in SESSIONS:
        if s <= t < e: return name
    return None

df['session']    = df.index.map(get_sess)
df['in_session'] = df['session'].notna()
df['macro_bias'] = df['date'].map(macro['bias'].to_dict())

df.dropna(subset=['atr','vwap','ema_fast','macro_bias'], inplace=True)

# Signals
def lsigs(row):
    n = 0
    if row['ema_bull']:                                         n+=1
    if row['vwap_dev'] <= -VWAP_SIG_THR and row['mom3'] > 0:  n+=1
    if row['near_l']   and row['mom3'] > 0:                    n+=1
    if row['mom3']     > row['atr'] * 0.3:                     n+=1
    return n

def ssigs(row):
    n = 0
    if not row['ema_bull']:                                     n+=1
    if row['vwap_dev'] >= VWAP_SIG_THR  and row['mom3'] < 0:  n+=1
    if row['near_h']   and row['mom3'] < 0:                    n+=1
    if row['mom3']     < -row['atr'] * 0.3:                    n+=1
    return n

df['ls'] = df.apply(lsigs, axis=1)
df['ss'] = df.apply(ssigs, axis=1)

df['go_long']  = (df['ls'] >= MIN_SIGNALS) & (df['macro_bias']=='LONG')  & df['in_session']
df['go_short'] = (df['ss'] >= MIN_SIGNALS) & (df['macro_bias']=='SHORT') & df['in_session']

print(f"  Long signals:  {df['go_long'].sum()}")
print(f"  Short signals: {df['go_short'].sum()}")

# ── STEP 4: BACKTEST ENGINE ────────────────────────────────────────────────────
def run_backtest(df, allow_long=True, allow_short=True, label=""):
    equity   = ACCOUNT
    in_trade = False
    entry    = {}
    trades   = []
    eq_c     = [ACCOUNT]
    eq_d     = [df.index[0]]

    for i in range(len(df)):
        row = df.iloc[i]
        idx = df.index[i]
        cp  = row['close']
        atr = row['atr']
        sess= row['session']

        if pd.isna(atr) or atr == 0: continue

        # EXIT
        if in_trade:
            d_  = entry['dir']
            ep  = entry['price']
            tp  = entry['tp']
            sl  = entry['sl']

            hit_tp  = (d_=='LONG' and cp>=tp) or (d_=='SHORT' and cp<=tp)
            hit_sl  = (d_=='LONG' and cp<=sl) or (d_=='SHORT' and cp>=sl)
            time_out= sess is None and entry['session'] is not None

            if hit_tp or hit_sl or time_out:
                xp  = tp if hit_tp else sl if hit_sl else cp
                xt  = 'TP' if hit_tp else 'SL' if hit_sl else 'TIME'
                raw = (xp-ep) if d_=='LONG' else (ep-xp)
                pct = raw/ep - 2*COMMISSION
                usd = equity * pct * entry['sm']
                equity += usd
                trades.append({
                    'entry_time': str(entry['time'])[:16],
                    'exit_time':  str(idx)[:16],
                    'dir':        d_,
                    'entry_px':   round(ep,2),
                    'exit_px':    round(xp,2),
                    'n_sigs':     entry['ns'],
                    'macro':      entry['bias'],
                    'net_pnl%':   round(pct*100,4),
                    'pnl_usd':    round(usd,2),
                    'equity':     round(equity,2),
                    'exit':       xt,
                    'bars':       i - entry['bi'],
                })
                eq_c.append(equity); eq_d.append(idx)
                in_trade = False

        # ENTRY
        if not in_trade:
            if allow_long and row['go_long']:
                tp_  = cp + atr*TP_ATR_MULT
                sl_  = cp - atr*SL_ATR_MULT
                oz   = (equity*RISK_PCT)/(cp-sl_+1e-10)
                sm   = min((oz*cp)/equity, 5.0)
                in_trade = True
                entry = dict(time=idx,bi=i,price=cp,dir='LONG',tp=tp_,sl=sl_,
                             atr=atr,session=sess,bias=row['macro_bias'],ns=row['ls'],sm=sm)

            elif allow_short and row['go_short']:
                tp_  = cp - atr*TP_ATR_MULT
                sl_  = cp + atr*SL_ATR_MULT
                oz   = (equity*RISK_PCT)/(sl_-cp+1e-10)
                sm   = min((oz*cp)/equity, 5.0)
                in_trade = True
                entry = dict(time=idx,bi=i,price=cp,dir='SHORT',tp=tp_,sl=sl_,
                             atr=atr,session=sess,bias=row['macro_bias'],ns=row['ss'],sm=sm)

    if not trades:
        return None

    tdf  = pd.DataFrame(trades)
    wins = tdf[tdf['net_pnl%']>0]
    loss = tdf[tdf['net_pnl%']<=0]
    wr   = len(wins)/len(tdf)
    roi  = (equity/ACCOUNT-1)*100
    pf   = wins['pnl_usd'].sum()/abs(loss['pnl_usd'].sum()) \
           if len(loss)>0 and loss['pnl_usd'].sum()!=0 else 99
    eq_s = pd.Series(eq_c,index=eq_d)
    mdd  = float(((eq_s-eq_s.expanding().max())/eq_s.expanding().max()*100).min())
    avg_w= wins['net_pnl%'].mean() if len(wins)>0 else 0
    avg_l= abs(loss['net_pnl%'].mean()) if len(loss)>0 else 0
    be_wr= avg_l/(avg_w+avg_l) if (avg_w+avg_l)>0 else 0.5

    t_stat, p_val = (0,1)
    if len(tdf)>5:
        t_stat,p_val = stats.ttest_1samp(tdf['net_pnl%'],0)

    tdf['month'] = pd.to_datetime(tdf['entry_time']).dt.to_period('M')
    mo = tdf.groupby('month').agg(
        trades=('pnl_usd','count'), pnl=('pnl_usd','sum'),
        wr=('net_pnl%', lambda x:round((x>0).mean()*100,1))
    ).reset_index()
    mo['roi%'] = (mo['pnl']/ACCOUNT*100).round(2)

    by_dir = tdf.groupby('dir').apply(lambda x: pd.Series({
        'n':   len(x),
        'wr%': round((x['net_pnl%']>0).mean()*100,1),
        'avg': round(x['net_pnl%'].mean(),4),
        'pnl': round(x['pnl_usd'].sum(),2),
    }), include_groups=False)

    return {
        'label':label,'tdf':tdf,'mo':mo,'by_dir':by_dir,
        'n':len(tdf),'wr':wr,'roi':roi,'pf':pf,'mdd':mdd,
        'equity':equity,'be_wr':be_wr,'t':t_stat,'p':p_val,
        'avg_w':avg_w,'avg_l':avg_l,
        'pos_mo':(mo['pnl']>0).sum(),'tot_mo':len(mo),
    }

# ── STEP 5: RUN BOTH SCENARIOS ────────────────────────────────────────────────
print("\nRunning scenarios...")
A = run_backtest(df, allow_long=True,  allow_short=True,  label="Scenario A: LONG + SHORT")
B = run_backtest(df, allow_long=False, allow_short=True,  label="Scenario B: SHORT only")

# ── STEP 6: PRINT COMPARISON ──────────────────────────────────────────────────
def print_scenario(r):
    if r is None:
        print("  No trades taken.")
        return
    print(f"\n  {'─'*64}")
    print(f"  {r['label']}")
    print(f"  {'─'*64}")
    print(f"  Trades       : {r['n']}")
    print(f"  Win rate     : {r['wr']*100:.1f}%  (break-even: {r['be_wr']*100:.1f}%,  edge: {(r['wr']-r['be_wr'])*100:+.1f}%)")
    print(f"  Profit factor: {r['pf']:.2f}")
    print(f"  ROI          : {r['roi']:+.2f}%")
    print(f"  Final equity : ${r['equity']:,.2f}")
    print(f"  Net P&L      : ${r['equity']-ACCOUNT:+,.2f}")
    print(f"  Max drawdown : {r['mdd']:.2f}%")
    print(f"  Avg win      : +{r['avg_w']:.4f}%")
    print(f"  Avg loss     : -{r['avg_l']:.4f}%")
    print(f"  +ve months   : {r['pos_mo']}/{r['tot_mo']}")
    print(f"  p-value      : {r['p']:.4f}  {'✓ SIGNIFICANT' if r['p']<0.05 else '— not significant'}")
    print(f"\n  By direction:")
    print(r['by_dir'].to_string())
    print(f"\n  Monthly P&L:")
    print(r['mo'][['month','trades','pnl','wr','roi%']].to_string(index=False))

print(f"\n{'='*68}")
print(f"  RESULTS")
print(f"{'='*68}")
print_scenario(A)
print_scenario(B)

# ── STEP 7: HEAD-TO-HEAD SUMMARY ──────────────────────────────────────────────
print(f"\n{'='*68}")
print(f"  HEAD-TO-HEAD COMPARISON")
print(f"{'='*68}")
print(f"\n  {'Metric':<22} {'Scenario A':>18} {'Scenario B':>18}")
print(f"  {'─'*60}")

rows = [
    ("Trades",       f"{A['n']}" if A else "—",             f"{B['n']}" if B else "—"),
    ("Win rate",     f"{A['wr']*100:.1f}%" if A else "—",   f"{B['wr']*100:.1f}%" if B else "—"),
    ("Edge vs BE",   f"{(A['wr']-A['be_wr'])*100:+.1f}%" if A else "—", f"{(B['wr']-B['be_wr'])*100:+.1f}%" if B else "—"),
    ("Profit factor",f"{A['pf']:.2f}" if A else "—",        f"{B['pf']:.2f}" if B else "—"),
    ("ROI",          f"{A['roi']:+.2f}%" if A else "—",     f"{B['roi']:+.2f}%" if B else "—"),
    ("Net P&L",      f"${A['equity']-ACCOUNT:+,.2f}" if A else "—", f"${B['equity']-ACCOUNT:+,.2f}" if B else "—"),
    ("Max drawdown", f"{A['mdd']:.2f}%" if A else "—",      f"{B['mdd']:.2f}%" if B else "—"),
    ("+ve months",   f"{A['pos_mo']}/{A['tot_mo']}" if A else "—", f"{B['pos_mo']}/{B['tot_mo']}" if B else "—"),
    ("p-value",      f"{A['p']:.4f}" if A else "—",         f"{B['p']:.4f}" if B else "—"),
]
for name, va, vb in rows:
    print(f"  {name:<22} {va:>18} {vb:>18}")

print(f"\n  VERDICT:")
if A and B:
    if B['roi'] > A['roi'] and B['wr'] > A['wr']:
        print(f"  SHORT ONLY wins — higher ROI ({B['roi']:+.1f}% vs {A['roi']:+.1f}%)")
        print(f"  and higher WR ({B['wr']*100:.1f}% vs {A['wr']*100:.1f}%)")
        print(f"  Long trades are diluting the edge — remove them.")
    elif A['roi'] > B['roi']:
        print(f"  LONG+SHORT wins on ROI ({A['roi']:+.1f}% vs {B['roi']:+.1f}%)")
        print(f"  Long trades are adding value in this data period.")
    else:
        print(f"  Mixed — ROI similar but different risk profiles.")

print(f"\n  NOTE: Statistical significance requires 200+ trades.")
print(f"  Data shown covers only {A['n'] if A else 0} / {B['n'] if B else 0} trades.")
print(f"  Run for longer period or lower MIN_SIGNALS for more data.")
print(f"{'='*68}")

# Save
if A: A['tdf'].to_csv("scenario_A_trades.csv", index=False)
if B: B['tdf'].to_csv("scenario_B_trades.csv", index=False)
print("\nSaved: scenario_A_trades.csv + scenario_B_trades.csv")
