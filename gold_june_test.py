"""
JUNE DEEP DIVE — Should we short-only in June instead of skipping it?

Tests:
  1. Proven model — skip June (current rule)
  2. June only — both directions (baseline: how bad is June really?)
  3. June only — LONG only  (is June bad because longs fail?)
  4. June only — SHORT only (is June bad because shorts fail, or do they actually work?)
  5. Proven model — but SHORT only in June instead of skipping

If test 4 (June shorts) has positive Sharpe → change the rule.
If not → keep skipping June.
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
print("  JUNE DEEP DIVE — Short only vs skip entirely")
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
print(f"  Macro data: {len(dm)} trading days")

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

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def run(min_score=2,
        skip_vix_above=30,
        only_months=None,      # restrict to these months only
        skip_months=None,      # skip these months
        force_dir=None,        # override macro: 1=long only, -1=short only
        no_ema50_short_gate=False,
        entry_window=(14.0, 16.5),
        label=""):

    equity = ACCOUNT
    trades = []

    for date in sorted(set(df.index.date)):
        score = macro_scores.get(date)
        if score is None or abs(score) < min_score:
            continue
        if skip_months  and date.month in skip_months:  continue
        if only_months  and date.month not in only_months: continue
        if skip_vix_above is not None:
            vix_val = macro_vix.get(date)
            if vix_val is not None and vix_val >= skip_vix_above:
                continue

        macro_dir = force_dir if force_dir else (1 if score > 0 else -1)

        ts   = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
        c_ts = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4: continue

        ctx = get_4h(o_ts)
        if ctx is None: continue

        # EMA50 short gate
        if not no_ema50_short_gate:
            if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']:
                continue

        tp  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
        vol = sbars['volume'].replace(0, 1)
        sbars = sbars.copy()
        sbars['vwap']     = (tp * vol).cumsum() / vol.cumsum()
        sbars['vwap_std'] = tp.expanding().std().bfill().fillna(1)

        in_trade=False; entry_px=sl=tp1_px=tp2_px=sz1=sz2=pnl1=0
        tp1_hit=False; bars_held=0; trade_dir=0
        trades_today=0; last_entry_i=-99

        for i in range(1, len(sbars)):
            b   = sbars.iloc[i]
            hi, lo, cls = b['high'], b['low'], b['close']
            vwap = b['vwap']
            std  = max(b['vwap_std'], 0.01)

            if entry_window:
                h = b.name.hour + b.name.minute/60
                if not (entry_window[0] <= h <= entry_window[1]): continue

            if in_trade:
                bars_held += 1
                if trade_dir == 1:
                    if lo <= sl:
                        pnl=(sl-entry_px)*(sz1+sz2)+pnl1
                        trades.append(dict(date=date,dir='LONG',entry=entry_px,exit=sl,pnl=pnl,reason='SL',score=score,bars=bars_held)); equity+=pnl; in_trade=False; continue
                    if not tp1_hit and hi>=tp1_px:
                        pnl1=(tp1_px-entry_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                    if tp1_hit and hi>=tp2_px:
                        pnl2=(tp2_px-entry_px)*sz2
                        trades.append(dict(date=date,dir='LONG',entry=entry_px,exit=tp2_px,pnl=pnl1+pnl2,reason='TP2',score=score,bars=bars_held)); equity+=pnl2; in_trade=False; continue
                else:
                    if hi>=sl:
                        pnl=(entry_px-sl)*(sz1+sz2)+pnl1
                        trades.append(dict(date=date,dir='SHORT',entry=entry_px,exit=sl,pnl=pnl,reason='SL',score=score,bars=bars_held)); equity+=pnl; in_trade=False; continue
                    if not tp1_hit and lo<=tp1_px:
                        pnl1=(entry_px-tp1_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                    if tp1_hit and lo<=tp2_px:
                        pnl2=(entry_px-tp2_px)*sz2
                        trades.append(dict(date=date,dir='SHORT',entry=entry_px,exit=tp2_px,pnl=pnl1+pnl2,reason='TP2',score=score,bars=bars_held)); equity+=pnl2; in_trade=False; continue
                if bars_held>=TIME_STOP:
                    rem=sz2 if tp1_hit else (sz1+sz2)
                    pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem+pnl1
                    trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',entry=entry_px,exit=cls,pnl=pnl,reason='TIME',score=score,bars=bars_held)); equity+=pnl; in_trade=False
            else:
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
                vwap_ok=(macro_dir==1 and -1.0<=dist<=0.3) or \
                        (macro_dir==-1 and -0.3<=dist<=1.0)
                if not vwap_ok: continue
                atr_v=float(b['atr'])
                if atr_v<=0: continue
                tot_sz=(equity*RISK_PCT)/(SL_MULT*atr_v)
                sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC
                entry_px=cls
                if macro_dir==1:
                    sl=entry_px-SL_MULT*atr_v; tp1_px=entry_px+TP1_MULT*atr_v; tp2_px=entry_px+TP2_MULT*atr_v
                else:
                    sl=entry_px+SL_MULT*atr_v; tp1_px=entry_px-TP1_MULT*atr_v; tp2_px=entry_px-TP2_MULT*atr_v
                in_trade=True; trade_dir=macro_dir
                bars_held=0; tp1_hit=False; pnl1=0
                trades_today+=1; last_entry_i=i

        if in_trade:
            cls=sbars.iloc[-1]['close']; rem=sz2 if tp1_hit else (sz1+sz2)
            pnl=((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem+pnl1
            trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',entry=entry_px,exit=cls,pnl=pnl,reason='SESS',score=score,bars=bars_held)); equity+=pnl

    return equity, trades


def summarize(eq, trs, label=""):
    if not trs:
        return dict(label=label,n=0,tpw=0,roi=0,wr=0,sharpe=0,pv=1.0,mdd=0,
                    avg_win=0,avg_loss=0,long_n=0,long_wr=0,short_n=0,short_wr=0)
    t   = pd.DataFrame(trs)
    n   = len(t)
    roi = (eq-ACCOUNT)/ACCOUNT*100
    wr  = t['pnl'].gt(0).mean()*100
    wks = max((pd.to_datetime(t['date']).max()-pd.to_datetime(t['date']).min()).days/7,1)
    sh  = t['pnl'].mean()/t['pnl'].std()*np.sqrt(252) if t['pnl'].std()>0 else 0
    ec  = ACCOUNT+t['pnl'].cumsum()
    mdd = ((ec-ec.cummax())/ec.cummax()*100).min()
    _,pv= stats.ttest_1samp(t['pnl'],0) if n>5 else (0,1.0)
    aw  = t.loc[t['pnl']>0,'pnl'].mean() if t['pnl'].gt(0).any() else 0
    al  = t.loc[t['pnl']<0,'pnl'].mean() if t['pnl'].lt(0).any() else 0
    lng = t[t['dir']=='LONG'];  long_wr  = lng['pnl'].gt(0).mean()*100 if len(lng) else 0
    sht = t[t['dir']=='SHORT']; short_wr = sht['pnl'].gt(0).mean()*100 if len(sht) else 0
    return dict(label=label,n=n,tpw=round(n/wks,1),roi=round(roi,1),wr=round(wr,1),
                sharpe=round(sh,2),pv=round(pv,4),mdd=round(mdd,1),
                avg_win=round(aw,0),avg_loss=round(al,0),
                long_n=len(lng),long_wr=round(long_wr,1),
                short_n=len(sht),short_wr=round(short_wr,1))


# ── RUN TESTS ─────────────────────────────────────────────────────────────────
print("\nRunning June analysis...\n")

PROVEN = dict(min_score=2, skip_vix_above=30)

tests = [
    # Full period reference
    ("PROVEN (skip June)            — full period",
     {**PROVEN, 'skip_months':[6]}),
    # June breakdown
    ("June — both directions        (macro-driven)",
     {**PROVEN, 'only_months':[6]}),
    ("June — LONG only              (forced long)",
     {**PROVEN, 'only_months':[6], 'force_dir':1}),
    ("June — SHORT only             (macro-driven short)",
     {**PROVEN, 'only_months':[6], 'force_dir':-1}),
    ("June — SHORT only + no EMA50 gate (more shorts allowed)",
     {**PROVEN, 'only_months':[6], 'force_dir':-1, 'no_ema50_short_gate':True}),
    # The real question: proven model but short-only in June
    ("PROVEN + short-only June      (new rule candidate)",
     {**PROVEN, 'skip_months':[], 'only_months':None}),  # placeholder, built below
]

# Test 6 needs custom logic: proven model normally + short-only in June
# Run proven (non-June) + June shorts separately, then combine
eq_noJun,  trs_noJun  = run(**{**PROVEN, 'skip_months':[6]})
eq_junSht, trs_junSht = run(**{**PROVEN, 'only_months':[6], 'force_dir':-1, 'no_ema50_short_gate':True})

# Combine: proven outside June + short-only in June
combined_trs = trs_noJun + trs_junSht
combined_eq  = ACCOUNT + sum(t['pnl'] for t in combined_trs)
res_combined = summarize(combined_eq, combined_trs, "PROVEN + June shorts combined")

# Run the 5 main tests
results = []
for label, kwargs in tests[:5]:
    eq, trs = run(**kwargs, label=label)
    r = summarize(eq, trs, label)
    results.append(r)
    sig = " ✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "  ")
    print(f"  {label:<55} n={r['n']:>4}  WR={r['wr']:>5.1f}%  Sh={r['sharpe']:>6.2f}  p={r['pv']:.4f}{sig}  ROI={r['roi']:>+8.1f}%")

results.append(res_combined)
r = res_combined
sig = " ✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "  ")
print(f"  {'PROVEN + June shorts combined':<55} n={r['n']:>4}  WR={r['wr']:>5.1f}%  Sh={r['sharpe']:>6.2f}  p={r['pv']:.4f}{sig}  ROI={r['roi']:>+8.1f}%")

# ── RESULTS TABLE ─────────────────────────────────────────────────────────────
print(f"\n{'='*110}")
print("  JUNE ANALYSIS — FULL RESULTS")
print(f"{'='*110}")
print(f"  {'Test':<55} {'N':>5} {'WR':>6} {'Sharpe':>7} {'p-val':>8} {'ROI':>9} {'MDD':>7} {'LongN/WR':>12} {'ShortN/WR':>12}")
print(f"  {'─'*108}")
for r in results:
    sig = " ✓" if r['pv']<0.05 else ("~" if r['pv']<0.10 else "  ")
    print(f"  {r['label'][:55]:<55} {r['n']:>5} {r['wr']:>5.1f}% {r['sharpe']:>7.2f} "
          f"{r['pv']:>8.4f}{sig:>2} {r['roi']:>+8.1f}% {r['mdd']:>6.1f}%  "
          f"L:{r['long_n']}@{r['long_wr']:.0f}%  S:{r['short_n']}@{r['short_wr']:.0f}%")

# ── JUNE TRADE DETAIL ─────────────────────────────────────────────────────────
if trs_junSht:
    print(f"\n{'='*110}")
    print("  JUNE SHORTS — TRADE DETAIL")
    print(f"{'='*110}")
    t = pd.DataFrame(trs_junSht)
    t['date'] = pd.to_datetime(t['date'])
    t['win']  = t['pnl'] > 0
    print(f"  Trades: {len(t)}  |  WR: {t['win'].mean()*100:.1f}%  |  Avg win: ${t.loc[t['win'],'pnl'].mean():+.0f}  |  Avg loss: ${t.loc[~t['win'],'pnl'].mean():+.0f}")
    print(f"\n  Exit breakdown:")
    for reason, g in t.groupby('reason'):
        print(f"    {reason:<6}  {len(g):>3} trades  WR {g['win'].mean()*100:.0f}%  avg ${g['pnl'].mean():+.0f}")
    print(f"\n  By June year:")
    for yr, g in t.groupby(t['date'].dt.year):
        print(f"    {yr}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  total ${g['pnl'].sum():+,.0f}")

# ── VERDICT ───────────────────────────────────────────────────────────────────
print(f"\n{'='*110}")
print("  VERDICT")
print(f"{'='*110}")

june_both  = results[1]
june_long  = results[2]
june_short = results[3]
june_short_nogate = results[4]
proven_skip = results[0]

print(f"""
  June — both directions:   Sharpe {june_both['sharpe']:>6.2f}  WR {june_both['wr']:.1f}%  (n={june_both['n']})
  June — longs only:        Sharpe {june_long['sharpe']:>6.2f}  WR {june_long['wr']:.1f}%  (n={june_long['n']})
  June — shorts only:       Sharpe {june_short['sharpe']:>6.2f}  WR {june_short['wr']:.1f}%  (n={june_short['n']})
  June — shorts, no gate:   Sharpe {june_short_nogate['sharpe']:>6.2f}  WR {june_short_nogate['wr']:.1f}%  (n={june_short_nogate['n']})
  Proven skip June:         Sharpe {proven_skip['sharpe']:>6.2f}  WR {proven_skip['wr']:.1f}%  (n={proven_skip['n']})
  Proven + June shorts:     Sharpe {res_combined['sharpe']:>6.2f}  WR {res_combined['wr']:.1f}%  (n={res_combined['n']})
""")

best_june_sharpe = max(june_short['sharpe'], june_short_nogate['sharpe'])
if best_june_sharpe > 0.5:
    print("  ✓ JUNE SHORTS WORK — update the rule:")
    print("    Instead of skipping June entirely, SHORT ONLY in June.")
    print(f"    Combined model Sharpe: {res_combined['sharpe']} (vs {proven_skip['sharpe']} skip-June)")
    if res_combined['sharpe'] > proven_skip['sharpe']:
        print("    → Combined is BETTER than skip-June. Change the rule.")
    else:
        print("    → Combined is NOT better overall. June shorts add trades but hurt Sharpe.")
        print("      Recommendation: keep skipping June.")
elif best_june_sharpe > 0:
    print("  ~ June shorts are marginal — not enough edge to justify trading.")
    print("    Keep skipping June.")
else:
    print("  ✗ June shorts FAIL — both directions are bad in June.")
    print("    The problem is the market conditions (chop/low vol), not the direction.")
    print("    Keep skipping June entirely.")

print(f"\n{'='*110}")
