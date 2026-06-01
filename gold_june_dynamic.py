"""
JUNE DYNAMIC ADJUSTMENTS
=========================
Problem: June Sharpe -0.41 (shorts) to -0.97 (longs). Both fail.
Root cause: 7/17 June trades hit SL — pullbacks don't snap back.
            Low institutional volume = no order flow to mean-revert.

Dynamic fixes to test:
  A: Higher score threshold in June (≥6 = 4/5 components agree = clearer day)
  B: Tighter VWAP zone in June (-0.3σ to 0σ longs / 0σ to +0.3σ shorts)
  C: Confirm bar in June (wait for next 15m bar to confirm before entering)
  D: ATR filter in June (only trade if daily ATR is in normal range)
  E: Faster TP in June (TP1=1.0×ATR, TP2=1.5×ATR — less follow-through expected)
  F: Shorter time stop in June (4 bars = 60 min vs 6 bars = 90 min)
  G: Combinations of the above

Verdict: if any June config gives Sharpe > 0.5 → update the rule.
         If not → June skip stays.
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
MAX_TRADES = 2
MIN_BARS_BETWEEN = 4
SESS_OPEN  = (13, 30)
SESS_CLOSE = (17,  0)

print("="*72)
print("  JUNE DYNAMIC ADJUSTMENTS")
print("="*72)

# ── DATA FETCH ────────────────────────────────────────────────────────────────
print("\nFetching 3 years of 15m data...")

def fetch_page(end_date):
    end_enc = urllib.parse.quote(end_date)
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol=XAU/USD&interval=15min&outputsize=5000"
           f"&apikey={API_KEY}&end_date={end_enc}&timezone=UTC")
    r = requests.get(url, timeout=30)
    j = r.json()
    if 'values' not in j: return None
    df = pd.DataFrame(j['values'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    df.sort_index(inplace=True)
    for c in ['open','high','low','close','volume']:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
    df.dropna(subset=['open','high','low','close'], inplace=True)
    return df

all_frames = []
end_date     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
target_start = (datetime.utcnow() - timedelta(days=1100)).strftime("%Y-%m-%d")

for page in range(25):
    print(f"  Page {page+1:02d}  end={end_date[:10]}...", end=" ", flush=True)
    dfp = fetch_page(end_date)
    if dfp is None or len(dfp) == 0: print("empty"); break
    print(f"{len(dfp)} bars  ({dfp.index[0].date()} → {dfp.index[-1].date()})")
    all_frames.append(dfp)
    earliest = dfp.index[0]
    if earliest.strftime("%Y-%m-%d") <= target_start:
        print(f"  Reached target start"); break
    end_date = (earliest - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    if page < 24: time.sleep(12)

gold_15m = pd.concat(all_frames).sort_index()
gold_15m = gold_15m[~gold_15m.index.duplicated(keep='first')]
gold_15m['volume'] = gold_15m.get('volume', pd.Series(1.0, index=gold_15m.index)).fillna(1.0).replace(0, 1.0)
print(f"  Total: {len(gold_15m)} bars | {gold_15m.index[0].date()} → {gold_15m.index[-1].date()}")

# ── MACRO DATA ────────────────────────────────────────────────────────────────
print("\nLoading macro data...")
def dl(t):
    try:
        d = yf.download(t, period="5y", interval="1d", progress=False)
        d.columns = [c[0].lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        return d['close'].dropna()
    except: return None

gold_d = dl("GC=F"); dxy_d = dl("DX-Y.NYB"); tnx_d = dl("^TNX")
gdx_d  = dl("GDX");  vix_d = dl("^VIX")

dm = pd.DataFrame({'gold': gold_d}).dropna()
for name, data in [('dxy',dxy_d),('tnx',tnx_d),('gdx',gdx_d),('vix',vix_d)]:
    if data is not None:
        dm[name] = data.reindex(dm.index, method='ffill')
dm.dropna(inplace=True)

dm['ema50']   = dm['gold'].ewm(span=50, adjust=False).mean()
dm['s_trend'] = np.where(dm['gold'] > dm['ema50'], 1, -1)
dm['s_yield'] = np.where(dm['tnx'].diff() < 0, 1, -1)
dm['s_dxy']   = np.where(dm['dxy'].pct_change() < 0, 1, -1)
dm['s_mom']   = np.where(dm['gold'].pct_change(5) > 0, 1, -1)
dm['s_gdx']   = np.where(dm['gdx'].pct_change() > dm['gold'].pct_change(), 1, -1)
raw = (dm['s_trend'] + dm['s_yield'] + dm['s_dxy'] + dm['s_mom'] + dm['s_gdx']).shift(1)
dm['score'] = (raw / 5 * 10).round(1)
dm.dropna(inplace=True)

macro_scores = {d.date(): float(r['score']) for d, r in dm.iterrows()}
macro_vix    = {d.date(): float(r['vix'])   for d, r in dm.iterrows()}

# ── DAILY ATR PERCENTILE ──────────────────────────────────────────────────────
_gd = yf.download("GC=F", period="5y", interval="1d", progress=False)
_gd.columns  = [c[0].lower() for c in _gd.columns]
_gd.index    = pd.to_datetime(_gd.index).tz_localize(None)
_gd.dropna(inplace=True)
_tr_d = pd.concat([_gd['high']-_gd['low'],
                   (_gd['high']-_gd['close'].shift()).abs(),
                   (_gd['low'] -_gd['close'].shift()).abs()], axis=1).max(axis=1)
_atr_d = _tr_d.ewm(span=14, adjust=False).mean()
daily_atr_pct = {d.date(): float(p) for d, p in _atr_d.rank(pct=True).items()}
print(f"  Macro data: {len(dm)} days | Daily ATR pct: {len(daily_atr_pct)} days")

# ── 4H CONTEXT ────────────────────────────────────────────────────────────────
gold_1h = yf.download("GC=F", period="720d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index   = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)
g4 = gold_1h.resample('4h', label='left', closed='left').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
def get_4h(ts):
    past = g4[g4.index <= ts]
    return past.iloc[-1] if len(past) else None

df = gold_15m.copy()
df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
tr = pd.concat([df['high']-df['low'],
                (df['high']-df['close'].shift()).abs(),
                (df['low'] -df['close'].shift()).abs()], axis=1).max(axis=1)
df['atr'] = tr.ewm(span=14, adjust=False).mean()
df.dropna(inplace=True)
print(f"  4H context: {g4.index[0].date()} → {g4.index[-1].date()}")

# ── FLEXIBLE BACKTEST ENGINE ───────────────────────────────────────────────────
def run(min_score=2,
        skip_vix_above=30,
        only_months=None,
        skip_months=None,
        june_min_score=None,      # higher score threshold in June
        june_vwap_tight=False,    # tighter VWAP band in June
        june_confirm_bar=False,   # wait for confirm bar in June
        june_atr_filter=False,    # only trade June when ATR in 40-80th pct
        june_tp1_mult=None,       # different TP1 in June (e.g. 1.0)
        june_tp2_mult=None,       # different TP2 in June (e.g. 1.5)
        june_time_stop=None,      # shorter time stop in June
        june_force_dir=None,      # force direction in June (1=long, -1=short)
        entry_window=(14.0, 16.5),
        label=""):

    equity = ACCOUNT
    trades = []

    for date in sorted(set(df.index.date)):
        score = macro_scores.get(date)
        if score is None or abs(score) < min_score: continue
        if skip_months  and date.month in skip_months:    continue
        if only_months  and date.month not in only_months: continue
        if skip_vix_above is not None:
            vix_val = macro_vix.get(date)
            if vix_val is not None and vix_val >= skip_vix_above: continue

        is_june = (date.month == 6)

        # June-specific score threshold
        eff_min_score = (june_min_score if is_june and june_min_score else min_score)
        if abs(score) < eff_min_score: continue

        # June-specific ATR filter
        if is_june and june_atr_filter:
            atr_p = daily_atr_pct.get(date)
            if atr_p is not None and not (0.40 <= atr_p <= 0.80): continue

        macro_dir = score > 0 and 1 or -1
        if is_june and june_force_dir:
            macro_dir = june_force_dir

        ts   = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
        c_ts = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4: continue
        ctx = get_4h(o_ts)
        if ctx is None: continue
        if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']: continue

        tp  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
        vol = sbars['volume'].replace(0, 1)
        sbars = sbars.copy()
        sbars['vwap']     = (tp * vol).cumsum() / vol.cumsum()
        sbars['vwap_std'] = tp.expanding().std().bfill().fillna(1)

        # June-specific params
        eff_tp1  = june_tp1_mult   if (is_june and june_tp1_mult)   else TP1_MULT
        eff_tp2  = june_tp2_mult   if (is_june and june_tp2_mult)   else TP2_MULT
        eff_tstop= june_time_stop  if (is_june and june_time_stop)  else TIME_STOP
        eff_vwap_tight = (is_june and june_vwap_tight)
        eff_confirm    = (is_june and june_confirm_bar)

        in_trade=False; entry_px=sl=tp1_px=tp2_px=sz1=sz2=pnl1=0
        tp1_hit=False; bars_held=0; trade_dir=0
        trades_today=0; last_entry_i=-99; pending=False

        for i in range(1, len(sbars)):
            b   = sbars.iloc[i]
            prev= sbars.iloc[i-1]
            hi, lo, cls = b['high'], b['low'], b['close']
            vwap = b['vwap']
            std  = max(b['vwap_std'], 0.01)

            if entry_window:
                h = b.name.hour + b.name.minute/60
                if not (entry_window[0] <= h <= entry_window[1]):
                    pending = False; continue

            if in_trade:
                bars_held += 1
                if trade_dir == 1:
                    if lo <= sl:
                        pnl=(sl-entry_px)*(sz1+sz2)+pnl1
                        trades.append(dict(date=date,dir='LONG',entry=entry_px,exit=sl,pnl=pnl,reason='SL',bars=bars_held,june=is_june)); equity+=pnl; in_trade=False; continue
                    if not tp1_hit and hi>=tp1_px:
                        pnl1=(tp1_px-entry_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                    if tp1_hit and hi>=tp2_px:
                        pnl2=(tp2_px-entry_px)*sz2
                        trades.append(dict(date=date,dir='LONG',entry=entry_px,exit=tp2_px,pnl=pnl1+pnl2,reason='TP2',bars=bars_held,june=is_june)); equity+=pnl2; in_trade=False; continue
                else:
                    if hi>=sl:
                        pnl=(entry_px-sl)*(sz1+sz2)+pnl1
                        trades.append(dict(date=date,dir='SHORT',entry=entry_px,exit=sl,pnl=pnl,reason='SL',bars=bars_held,june=is_june)); equity+=pnl; in_trade=False; continue
                    if not tp1_hit and lo<=tp1_px:
                        pnl1=(entry_px-tp1_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                    if tp1_hit and lo<=tp2_px:
                        pnl2=(entry_px-tp2_px)*sz2
                        trades.append(dict(date=date,dir='SHORT',entry=entry_px,exit=tp2_px,pnl=pnl1+pnl2,reason='TP2',bars=bars_held,june=is_june)); equity+=pnl2; in_trade=False; continue
                if bars_held>=eff_tstop:
                    rem=sz2 if tp1_hit else (sz1+sz2)
                    pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem+pnl1
                    trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',entry=entry_px,exit=cls,pnl=pnl,reason='TIME',bars=bars_held,june=is_june)); equity+=pnl; in_trade=False
            else:
                # Confirm bar logic
                if eff_confirm and pending:
                    confirmed = (macro_dir==1 and cls>prev['close']) or (macro_dir==-1 and cls<prev['close'])
                    if confirmed:
                        atr_v=float(b['atr'])
                        if atr_v>0:
                            tot_sz=(equity*RISK_PCT)/(SL_MULT*atr_v)
                            sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC
                            entry_px=cls
                            if macro_dir==1:
                                sl=entry_px-SL_MULT*atr_v; tp1_px=entry_px+eff_tp1*atr_v; tp2_px=entry_px+eff_tp2*atr_v
                            else:
                                sl=entry_px+SL_MULT*atr_v; tp1_px=entry_px-eff_tp1*atr_v; tp2_px=entry_px-eff_tp2*atr_v
                            in_trade=True; trade_dir=macro_dir; bars_held=0; tp1_hit=False; pnl1=0
                            trades_today+=1; last_entry_i=i
                    pending=False; continue

                if trades_today>=MAX_TRADES: continue
                if i-last_entry_i<MIN_BARS_BETWEEN: continue
                ctx_now=get_4h(b.name)
                if ctx_now is None: continue
                h4_ok=(macro_dir==1 and ctx_now['ema9']>ctx_now['ema21']) or \
                      (macro_dir==-1 and ctx_now['ema9']<ctx_now['ema21'])
                if not h4_ok: continue
                ema_ok=(macro_dir==1 and b['ema9']>b['ema21']) or \
                       (macro_dir==-1 and b['ema9']<b['ema21'])
                if not ema_ok: continue

                dist=(cls-vwap)/std
                if eff_vwap_tight:
                    # Tighter zone: only very close to VWAP
                    vwap_ok = (macro_dir==1 and -0.5<=dist<=0.0) or \
                              (macro_dir==-1 and  0.0<=dist<=0.5)
                else:
                    vwap_ok = (macro_dir==1 and -1.0<=dist<=0.3) or \
                              (macro_dir==-1 and -0.3<=dist<=1.0)
                if not vwap_ok: continue

                if eff_confirm:
                    pending=True; continue

                atr_v=float(b['atr'])
                if atr_v<=0: continue
                tot_sz=(equity*RISK_PCT)/(SL_MULT*atr_v)
                sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC
                entry_px=cls
                if macro_dir==1:
                    sl=entry_px-SL_MULT*atr_v; tp1_px=entry_px+eff_tp1*atr_v; tp2_px=entry_px+eff_tp2*atr_v
                else:
                    sl=entry_px+SL_MULT*atr_v; tp1_px=entry_px-eff_tp1*atr_v; tp2_px=entry_px-eff_tp2*atr_v
                in_trade=True; trade_dir=macro_dir; bars_held=0; tp1_hit=False; pnl1=0
                trades_today+=1; last_entry_i=i

        if in_trade:
            cls=sbars.iloc[-1]['close']; rem=sz2 if tp1_hit else (sz1+sz2)
            pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem+pnl1
            trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',entry=entry_px,exit=cls,pnl=pnl,reason='SESS',bars=bars_held,june=is_june)); equity+=pnl

    return equity, trades


def summarize(eq, trs, label=""):
    if not trs:
        return dict(label=label,n=0,tpw=0,roi=0,wr=0,sharpe=0,pv=1.0,mdd=0,sl_rate=0)
    t   = pd.DataFrame(trs)
    n   = len(t)
    roi = (eq-ACCOUNT)/ACCOUNT*100
    wr  = t['pnl'].gt(0).mean()*100
    wks = max((pd.to_datetime(t['date']).max()-pd.to_datetime(t['date']).min()).days/7,1)
    sh  = t['pnl'].mean()/t['pnl'].std()*np.sqrt(252) if t['pnl'].std()>0 else 0
    ec  = ACCOUNT+t['pnl'].cumsum()
    mdd = ((ec-ec.cummax())/ec.cummax()*100).min()
    _,pv= stats.ttest_1samp(t['pnl'],0) if n>5 else (0,1.0)
    sl_rate = (t['reason']=='SL').mean()*100
    return dict(label=label,n=n,roi=round(roi,1),wr=round(wr,1),
                sharpe=round(sh,2),pv=round(pv,4),mdd=round(mdd,1),sl_rate=round(sl_rate,1))


# ── RUN TESTS ─────────────────────────────────────────────────────────────────
print("\nRunning June dynamic adjustment tests...\n")

# Reference values
BASE = dict(min_score=2, skip_vix_above=30)

tests = [
    # Baselines
    ("BASELINE  proven skip June",
     {**BASE, 'skip_months':[6]}),
    ("BASELINE  June as-is (no adjustment)",
     {**BASE, 'only_months':[6]}),

    # A: Score threshold
    ("A: June score ≥6  (4/5 components must agree)",
     {**BASE, 'only_months':[6], 'june_min_score':6}),

    # B: Tighter VWAP
    ("B: June tight VWAP  (−0.5σ to 0σ only)",
     {**BASE, 'only_months':[6], 'june_vwap_tight':True}),

    # C: Confirm bar
    ("C: June confirm bar  (wait for next 15m to confirm)",
     {**BASE, 'only_months':[6], 'june_confirm_bar':True}),

    # D: ATR filter
    ("D: June ATR filter  (40-80th pctile days only)",
     {**BASE, 'only_months':[6], 'june_atr_filter':True}),

    # E: Faster TP
    ("E: June fast TP  (TP1=1.0×ATR, TP2=1.5×ATR)",
     {**BASE, 'only_months':[6], 'june_tp1_mult':1.0, 'june_tp2_mult':1.5}),

    # F: Shorter time stop
    ("F: June time stop 4 bars  (60 min vs 90 min)",
     {**BASE, 'only_months':[6], 'june_time_stop':4}),

    # Combos
    ("G: Score≥6 + confirm bar",
     {**BASE, 'only_months':[6], 'june_min_score':6, 'june_confirm_bar':True}),
    ("G: Score≥6 + ATR filter",
     {**BASE, 'only_months':[6], 'june_min_score':6, 'june_atr_filter':True}),
    ("G: Score≥6 + tight VWAP",
     {**BASE, 'only_months':[6], 'june_min_score':6, 'june_vwap_tight':True}),
    ("G: Tight VWAP + confirm bar",
     {**BASE, 'only_months':[6], 'june_vwap_tight':True, 'june_confirm_bar':True}),
    ("G: ATR filter + confirm bar",
     {**BASE, 'only_months':[6], 'june_atr_filter':True, 'june_confirm_bar':True}),
    ("G: ATR filter + fast TP",
     {**BASE, 'only_months':[6], 'june_atr_filter':True, 'june_tp1_mult':1.0, 'june_tp2_mult':1.5}),
    ("G: Score≥6 + ATR + confirm bar",
     {**BASE, 'only_months':[6], 'june_min_score':6, 'june_atr_filter':True, 'june_confirm_bar':True}),
    ("G: Score≥6 + ATR + fast TP",
     {**BASE, 'only_months':[6], 'june_min_score':6, 'june_atr_filter':True,
      'june_tp1_mult':1.0, 'june_tp2_mult':1.5}),
    ("G: ALL filters combined",
     {**BASE, 'only_months':[6], 'june_min_score':6, 'june_atr_filter':True,
      'june_confirm_bar':True, 'june_tp1_mult':1.0, 'june_tp2_mult':1.5}),
]

results = []
for label, kwargs in tests:
    eq, trs = run(**kwargs, label=label)
    r = summarize(eq, trs, label)
    results.append(r)
    sig = " ✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "  ")
    print(f"  {label:<55} n={r['n']:>3}  WR={r['wr']:>5.1f}%  Sh={r['sharpe']:>6.2f}  SL%={r['sl_rate']:>4.0f}%  p={r['pv']:.4f}{sig}")

# ── RESULTS TABLE ─────────────────────────────────────────────────────────────
print(f"\n{'='*105}")
print("  JUNE DYNAMIC ADJUSTMENTS — FULL RESULTS")
print(f"{'='*105}")
print(f"  {'Test':<55} {'N':>4} {'WR':>6} {'Sharpe':>7} {'SL%':>6} {'ROI':>8} {'MDD':>7}")
print(f"  {'─'*100}")
for r in results:
    sig = " ✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "  ")
    print(f"  {r['label'][:55]:<55} {r['n']:>4} {r['wr']:>5.1f}% {r['sharpe']:>7.2f} "
          f"{r['sl_rate']:>5.0f}% {r['roi']:>+7.1f}% {r['mdd']:>6.1f}%{sig}")

# ── FIND THE BEST JUNE CONFIG ─────────────────────────────────────────────────
june_results = results[1:]  # exclude the proven-skip-june baseline
best_june = max(june_results, key=lambda x: x['sharpe'])

print(f"\n{'='*105}")
print("  BEST JUNE CONFIGURATION")
print(f"{'='*105}")
print(f"\n  Best: {best_june['label']}")
print(f"  Sharpe: {best_june['sharpe']}  WR: {best_june['wr']}%  Trades: {best_june['n']}  SL rate: {best_june['sl_rate']}%")

proven_sharpe = results[0]['sharpe']

# Build the combined model: proven (non-June) + best June config
best_june_label = best_june['label']
best_june_kwargs = next(k for l, k in [(t[0], t[1]) for t in tests] if l == best_june_label)

eq_nj, trs_nj = run(**{**BASE, 'skip_months':[6]})
# Run best June config but get the actual kwargs
eq_j,  trs_j  = run(**best_june_kwargs)

combined_trs = trs_nj + trs_j
combined_eq  = ACCOUNT + sum(t['pnl'] for t in combined_trs)
r_comb = summarize(combined_eq, combined_trs, "Combined: proven + best June")

print(f"\n  Combined model (proven + best June config):")
print(f"  Sharpe: {r_comb['sharpe']}  WR: {r_comb['wr']}%  Trades: {r_comb['n']}  ROI: {r_comb['roi']:+.1f}%")
print(f"  vs Skip-June proven: Sharpe {proven_sharpe}  n={results[0]['n']}")

print(f"\n{'='*105}")
print("  VERDICT")
print(f"{'='*105}")

if best_june['sharpe'] > 0.5 and best_june['n'] >= 5:
    print(f"\n  ✓ Found a June config that works: {best_june['label']}")
    print(f"    Sharpe: {best_june['sharpe']}  WR: {best_june['wr']}%")
    if r_comb['sharpe'] >= proven_sharpe - 0.1:
        print(f"    → Combined Sharpe ({r_comb['sharpe']}) ≈ skip-June Sharpe ({proven_sharpe})")
        print(f"    → UPDATE THE RULE: apply this June config instead of skipping June")
    else:
        print(f"    → Combined Sharpe ({r_comb['sharpe']}) < skip-June Sharpe ({proven_sharpe})")
        print(f"    → June trades still drag overall performance. Skip June remains better.")
else:
    print(f"\n  ✗ No June adjustment found with Sharpe > 0.5 and ≥5 trades.")
    print(f"    Best June Sharpe: {best_june['sharpe']} ({best_june['n']} trades)")
    print(f"\n    The June problem is structural — not fixable with entry filters alone.")
    print(f"    Likely cause: low summer institutional volume kills mean-reversion edge")
    print(f"    in BOTH directions regardless of entry quality.")
    print(f"\n    Recommendation: SKIP JUNE. The month off is protecting the account.")

print(f"\n{'='*105}")
