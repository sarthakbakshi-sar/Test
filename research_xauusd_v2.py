"""
XAUUSD Intraday Edge Discovery — v2 with Real Data
====================================================
Data: 21 years of XAUUSD (2004-2026), 15M + 1H bars
Source: Broker historical data (MT4/Dukascopy format)

Walk-forward design:
  IN-SAMPLE   : 2004-2015  (hypothesis observation only — no parameter tuning)
  OOS-1       : 2016-2019  (first out-of-sample test)
  OOS-2       : 2020-2026  (second OOS, different regime)
  ALL REPORTED RESULTS are from OOS periods only.

Trading cost: $0.50/oz round-trip (conservative — includes spread + slippage)
Significance: p < 0.05 after Bonferroni correction for multiple comparisons
"""

import os, warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
from scipy import stats

F15M = '/root/.claude/uploads/72260b9c-d25d-5454-aecb-8b4ff1e6383e/df2dfb04-XAU_15m_data.csv'
F1H  = '/root/.claude/uploads/72260b9c-d25d-5454-aecb-8b4ff1e6383e/56b206c2-XAU_1h_data.csv'

COST   = 0.50   # $/oz round-trip
ALPHA  = 0.05
MIN_T  = 100    # minimum trades to report

# ── Load & parse ────────────────────────────────────────────────

def load(path):
    df = pd.read_csv(path, sep=';', header=0,
                     names=['ts','o','h','l','c','vol'])
    df['ts'] = pd.to_datetime(df['ts'], format='%Y.%m.%d %H:%M', utc=True)
    df = df.sort_values('ts').reset_index(drop=True)
    df['year'] = df['ts'].dt.year
    df['month']= df['ts'].dt.month
    df['hour'] = df['ts'].dt.hour
    df['minute']= df['ts'].dt.minute
    df['dow']  = df['ts'].dt.dayofweek   # 0=Mon
    df['date'] = df['ts'].dt.date
    df['bar_ret'] = df['c'].diff()       # raw $/oz move
    df['pct_ret'] = df['c'].pct_change()
    return df

def enrich(df):
    c, h, l = df['c'].values, df['h'].values, df['l'].values

    # ATR(14)
    pc = np.roll(c,1); pc[0]=c[0]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(len(tr))
    for i in range(1,len(tr)):
        if i<14: atr[i]=tr[:i+1].mean()
        else:    atr[i]=(atr[i-1]*13+tr[i])/14
    df['atr'] = atr
    df['atr50']   = pd.Series(atr).rolling(50, min_periods=20).mean().values
    df['atr_pct'] = np.where(df['atr50']>0, df['atr']/df['atr50'], np.nan)

    # RSI(14)
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta>0,delta,0.); loss = np.where(delta<0,-delta,0.)
    ag=np.zeros(len(c)); al=np.zeros(len(c))
    for i in range(1,len(c)):
        if i<14: ag[i]=gain[:i+1].mean(); al[i]=loss[:i+1].mean()
        else:    ag[i]=(ag[i-1]*13+gain[i])/14; al[i]=(al[i-1]*13+loss[i])/14
    df['rsi'] = 100-100/(1+np.where(al==0,100.,ag/al))

    # EMA20, EMA50
    df['ema20'] = df['c'].ewm(span=20,adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50,adjust=False).mean()
    df['uptrend'] = (df['ema20'] > df['ema50']).astype(int)

    # VWAP (daily reset) — only if volume > 0
    tp = (h+l+c)/3
    df['tp'] = tp
    df['_ctv'] = (pd.Series(tp)*df['vol']).groupby(df['date']).cumsum()
    df['_cv']  = df['vol'].groupby(df['date']).cumsum()
    df['vwap'] = df['_ctv'] / df['_cv'].replace(0,np.nan)
    df['vd']   = (df['c']-df['vwap'])/df['vwap'].replace(0,np.nan)

    # N-bar range
    for n in [4, 8, 16, 32, 96]:
        df[f'hi{n}'] = df['h'].shift(1).rolling(n).max()
        df[f'lo{n}'] = df['l'].shift(1).rolling(n).min()

    # Session
    df['session'] = df['hour'].apply(lambda hr:
        'ASIA'   if 0  <= hr < 7  else
        'LONDON' if 7  <= hr < 13 else
        'NY'     if 13 <= hr < 20 else 'LATE')

    # Asian session range (00:00-07:00) computed daily
    asia = df[df['session']=='ASIA'].groupby('date').agg(
        asia_hi=('h','max'), asia_lo=('l','min')).reset_index()
    df = df.merge(asia, on='date', how='left')

    # Session bar counter
    df['sess_bar'] = df.groupby(['date','session']).cumcount()

    # Rolling percentile of ATR (for compression detection)
    df['atr_q25'] = pd.Series(atr).rolling(200,min_periods=50).quantile(0.25).values
    df['atr_q75'] = pd.Series(atr).rolling(200,min_periods=50).quantile(0.75).values
    df['compressed'] = (df['atr'] < df['atr_q25']).astype(int)
    df['expanded']   = (df['atr'] > df['atr_q75']).astype(int)

    # Momentum: N-bar return
    for n in [1,2,4,8,16]:
        df[f'mom{n}'] = df['c'].pct_change(n)

    # Realised volatility (rolling std)
    df['rvol20'] = df['pct_ret'].rolling(20).std()

    # Inside bar
    df['inside'] = ((df['h'] <= df['h'].shift(1)) & (df['l'] >= df['l'].shift(1))).astype(int)

    # Wick analysis
    body = (df['c'] - df['o']).abs()
    df['upper_wick'] = df['h'] - df[['o','c']].max(axis=1)
    df['lower_wick'] = df[['o','c']].min(axis=1) - df['l']
    df['range'] = df['h'] - df['l']
    df['body_frac'] = np.where(df['range']>0, body/df['range'], 0.5)
    df['wick_ratio'] = np.where(df['lower_wick']>0, df['upper_wick']/df['lower_wick'], np.nan)

    # Consecutive candles
    df['bull_candle'] = (df['c'] > df['o']).astype(int)
    df['consec_bull'] = df['bull_candle'].groupby(
        (df['bull_candle'] != df['bull_candle'].shift()).cumsum()).cumcount() + 1
    df.loc[df['bull_candle']==0,'consec_bull'] = 0
    df['consec_bear'] = ((1-df['bull_candle'])).groupby(
        ((1-df['bull_candle']) != (1-df['bull_candle']).shift()).cumsum()).cumcount() + 1
    df.loc[df['bull_candle']==1,'consec_bear'] = 0

    return df

# ── Statistical toolkit ─────────────────────────────────────────

def test_edge(returns, label, n_boot=5000):
    r = np.array(returns); r = r[~np.isnan(r)]
    n = len(r)
    if n < MIN_T: return None
    mean_r = r.mean()
    std_r  = r.std(ddof=1)
    t_stat, p_val = stats.ttest_1samp(r, 0)
    wins = (r > 0).mean()
    boot = np.array([np.random.choice(r,n,replace=True).mean() for _ in range(n_boot)])
    ci_lo, ci_hi = np.percentile(boot,[2.5,97.5])
    cum = np.cumsum(r)
    max_dd = (cum - np.maximum.accumulate(cum)).min()
    sharpe_ann = (mean_r / std_r) * np.sqrt(n) if std_r>0 else 0
    return dict(label=label, n=n, mean=mean_r, ci_lo=ci_lo, ci_hi=ci_hi,
                p=p_val, wins=wins, sharpe=sharpe_ann, max_dd=max_dd,
                total=r.sum(), sig=(p_val<ALPHA and ci_lo>0))

def backtest(df, mask, direction='long', hold=4):
    c = df['c'].values
    m = mask.values if hasattr(mask,'values') else np.array(mask, dtype=bool)
    trades = []; i = 0
    while i < len(df)-hold:
        if m[i]:
            entry = c[i]
            ex    = c[i+hold]
            gross = (ex-entry) if direction=='long' else (entry-ex)
            trades.append(gross - COST)
            i += hold+1
        else:
            i += 1
    return np.array(trades)

def pprint(r, indent='  '):
    if r is None: return
    sig = '  *** SIGNIFICANT ***' if r['sig'] else ''
    print(f"{indent}{r['label']}")
    print(f"{indent}  N={r['n']:,}  mean={r['mean']:+.3f}$/oz  "
          f"CI=[{r['ci_lo']:+.3f},{r['ci_hi']:+.3f}]  p={r['p']:.4f}")
    print(f"{indent}  WR={r['wins']*100:.1f}%  Sharpe≈{r['sharpe']:.2f}  "
          f"MaxDD=${r['max_dd']:,.0f}  Total=${r['total']:+,.0f}{sig}")

# ── Load data ────────────────────────────────────────────────────

print("Loading data...")
df15 = load(F15M); df15 = enrich(df15)
df1h = load(F1H);  df1h = enrich(df1h)

print(f"  15M: {len(df15):,} bars  {df15['ts'].iloc[0].date()} → {df15['ts'].iloc[-1].date()}")
print(f"  1H:  {len(df1h):,} bars  {df1h['ts'].iloc[0].date()} → {df1h['ts'].iloc[-1].date()}")

# Walk-forward splits
splits = {
    'IS':   (2004, 2015),  # in-sample — observation only
    'OOS1': (2016, 2019),  # first OOS
    'OOS2': (2020, 2026),  # second OOS (different regime)
    'ALL_OOS': (2016, 2026),
}

def period(df, yr_lo, yr_hi):
    return df[(df['year']>=yr_lo)&(df['year']<=yr_hi)].copy().reset_index(drop=True)

print(f"\nSplit sizes (15M):")
for name,(a,b) in splits.items():
    p = period(df15,a,b)
    print(f"  {name:10s} {a}-{b}: {len(p):>8,} bars")

# ─────────────────────────────────────────────────────────────────
# HYPOTHESIS BATTERY — all pre-specified before looking at OOS
# ─────────────────────────────────────────────────────────────────

all_results = {}

def run(df, tag):
    res = []

    # ── H1: Hour-of-day fixed effect ───────────────────────────
    print(f"  [{tag}] H1: Hour-of-day effect")
    for hr in range(0,24):
        for direction in ['long','short']:
            mask = (df['hour']==hr) & df['atr_pct'].between(0.4,2.0)
            t = backtest(df, mask, direction, hold=4)
            r = test_edge(t, f'H1:hr={hr:02d} {direction.upper()}')
            if r: res.append(r)

    # ── H2: Session open momentum ───────────────────────────────
    print(f"  [{tag}] H2: Session open momentum")
    for sess, bars in [('LONDON',2),('NY',2)]:
        for direction in ['long','short']:
            mask = (df['session']==sess) & (df['sess_bar']==0) & df['atr_pct'].between(0.5,1.8)
            t = backtest(df, mask, direction, hold=4)
            r = test_edge(t, f'H2:{sess}-open {direction.upper()}')
            if r: res.append(r)

    # ── H3: Asian range breakout at London open ─────────────────
    print(f"  [{tag}] H3: Asian range breakout")
    for direction, col in [('long','asia_hi'),('short','asia_lo')]:
        if col not in df.columns: continue
        mask = (df['session']=='LONDON') & (df['sess_bar']<=4)
        if direction=='long':
            mask = mask & (df['c'] > df['asia_hi'])
        else:
            mask = mask & (df['c'] < df['asia_lo'])
        mask = mask & df['atr_pct'].between(0.5,1.8)
        t = backtest(df, mask, direction, hold=4)
        r = test_edge(t, f'H3:Asia-range-breakout {direction.upper()}')
        if r: res.append(r)

    # ── H4: N-bar resistance breakout ──────────────────────────
    print(f"  [{tag}] H4: N-bar breakout")
    for n, hold_bars in [(4,4),(8,4),(16,6),(32,8),(96,8)]:
        for direction, hicol, locol in [('long',f'hi{n}',f'lo{n}'),
                                        ('short',f'hi{n}',f'lo{n}')]:
            if direction=='long':
                mask = (df['h'] > df[hicol]) & df['atr_pct'].between(0.5,1.8)
            else:
                mask = (df['l'] < df[locol]) & df['atr_pct'].between(0.5,1.8)
            t = backtest(df, mask, direction, hold=hold_bars)
            r = test_edge(t, f'H4:{n}bar-breakout {direction.upper()}')
            if r: res.append(r)

    # ── H5: ATR compression breakout ───────────────────────────
    print(f"  [{tag}] H5: ATR compression → expansion")
    for n in [8,16]:
        mask_long  = (df['compressed']==1) & (df['h']>df[f'hi{n}'])
        mask_short = (df['compressed']==1) & (df['l']<df[f'lo{n}'])
        for mask,d in [(mask_long,'long'),(mask_short,'short')]:
            t = backtest(df, mask, d, hold=4)
            r = test_edge(t, f'H5:compression+breakout-{n} {d.upper()}')
            if r: res.append(r)

    # ── H6: RSI extremes ────────────────────────────────────────
    print(f"  [{tag}] H6: RSI extremes")
    for rlo,rhi,d,hold in [(20,30,'long',8),(25,35,'long',6),
                            (65,75,'short',8),(70,80,'short',6)]:
        mask = df['rsi'].between(rlo,rhi) & df['atr_pct'].between(0.4,2.0)
        t = backtest(df, mask, d, hold=hold)
        r = test_edge(t, f'H6:RSI {rlo}-{rhi} {d.upper()} h={hold}')
        if r: res.append(r)

    # ── H7: VWAP deviation mean reversion ──────────────────────
    print(f"  [{tag}] H7: VWAP deviation")
    for lo,hi,d in [(-0.006,-0.002,'long'),(-0.010,-0.004,'long'),
                    (0.002,0.006,'short'),(0.004,0.010,'short')]:
        mask = df['vd'].between(lo,hi) & df['atr_pct'].between(0.4,1.8)
        t = backtest(df, mask, d, hold=4)
        r = test_edge(t, f'H7:VWAP {lo*100:.1f}%→{hi*100:.1f}% {d.upper()}')
        if r: res.append(r)

    # ── H8: Momentum (autocorrelation) ─────────────────────────
    print(f"  [{tag}] H8: Momentum")
    for lb,fwd in [(1,2),(2,4),(4,4),(8,8),(16,8)]:
        up   = df[f'mom{lb}']>0.002
        down = df[f'mom{lb}']<-0.002
        for mask,d in [(up,'long'),(down,'short'),(up,'short'),(down,'long')]:
            t = backtest(df, mask, d, hold=fwd)
            r = test_edge(t, f'H8:mom{lb} {d.upper()} fwd={fwd}')
            if r: res.append(r)

    # ── H9: Inside bar breakout ─────────────────────────────────
    print(f"  [{tag}] H9: Inside bar breakout")
    mask_long  = (df['inside'].shift(1)==1) & (df['c']>df['h'].shift(1))
    mask_short = (df['inside'].shift(1)==1) & (df['c']<df['l'].shift(1))
    for mask,d in [(mask_long,'long'),(mask_short,'short')]:
        t = backtest(df, mask, d, hold=4)
        r = test_edge(t, f'H9:InsideBar-breakout {d.upper()}')
        if r: res.append(r)

    # ── H10: Day-of-week ───────────────────────────────────────
    print(f"  [{tag}] H10: Day-of-week")
    dow_names = ['Mon','Tue','Wed','Thu','Fri']
    for dw in range(5):
        for d in ['long','short']:
            mask = (df['dow']==dw) & (df['sess_bar']==0) & (df['session']=='NY')
            t = backtest(df, mask, d, hold=4)
            r = test_edge(t, f'H10:DOW={dow_names[dw]} NY-open {d.upper()}')
            if r: res.append(r)

    # ── H11: Liquidity sweep + reversal ────────────────────────
    print(f"  [{tag}] H11: Liquidity sweep reversal")
    for n in [8, 16]:
        # Bearish sweep: price dips below N-bar low then closes back above it
        sweep_bull = (df['l']<df[f'lo{n}']) & (df['c']>df[f'lo{n}'])
        sweep_bear = (df['h']>df[f'hi{n}']) & (df['c']<df[f'hi{n}'])
        for mask,d in [(sweep_bull,'long'),(sweep_bear,'short')]:
            mask = mask & df['atr_pct'].between(0.5,2.0)
            t = backtest(df, mask, d, hold=4)
            r = test_edge(t, f'H11:SweepReversal-{n}bar {d.upper()}')
            if r: res.append(r)

    # ── H12: Trend alignment (uptrend + dip buy) ───────────────
    print(f"  [{tag}] H12: Trend-aligned dip")
    long_dip  = (df['uptrend']==1) & (df['rsi']<40) & df['vd'].between(-0.008,-0.001)
    short_rip = (df['uptrend']==0) & (df['rsi']>60) & df['vd'].between(0.001,0.008)
    for mask,d in [(long_dip,'long'),(short_rip,'short')]:
        t = backtest(df, mask, d, hold=6)
        r = test_edge(t, f'H12:Trend-dip {d.upper()}')
        if r: res.append(r)

    # ── H13: Consecutive candles exhaustion ────────────────────
    print(f"  [{tag}] H13: Consecutive candle exhaustion")
    for n in [3,4,5]:
        bull_exhaust = (df['consec_bull']>=n) & (df['rsi']>60)
        bear_exhaust = (df['consec_bear']>=n) & (df['rsi']<40)
        for mask,d in [(bull_exhaust,'short'),(bear_exhaust,'long')]:
            t = backtest(df, mask, d, hold=4)
            r = test_edge(t, f'H13:Consec{n}candles {d.upper()}')
            if r: res.append(r)

    # ── H14: Opening range breakout (London first 4 bars) ──────
    print(f"  [{tag}] H14: Opening range breakout")
    lon = df[df['session']=='LONDON'].copy()
    if len(lon):
        lon_or_hi = lon.groupby('date')['h'].transform(lambda x: x.iloc[:4].max())
        lon_or_lo = lon.groupby('date')['l'].transform(lambda x: x.iloc[:4].min())
        df.loc[df['session']=='LONDON','or_hi'] = lon_or_hi.values
        df.loc[df['session']=='LONDON','or_lo'] = lon_or_lo.values
        df[['or_hi','or_lo']] = df[['or_hi','or_lo']].ffill()
        mask_long  = (df['session']=='LONDON') & (df['sess_bar']>=4) & (df['sess_bar']<=16) & (df['c']>df['or_hi'])
        mask_short = (df['session']=='LONDON') & (df['sess_bar']>=4) & (df['sess_bar']<=16) & (df['c']<df['or_lo'])
        for mask,d in [(mask_long,'long'),(mask_short,'short')]:
            t = backtest(df, mask, d, hold=4)
            r = test_edge(t, f'H14:London-OR-breakout {d.upper()}')
            if r: res.append(r)

    return [r for r in res if r is not None]

# ── Run on OOS periods ──────────────────────────────────────────

print("\n" + "="*70)
print("RUNNING HYPOTHESIS BATTERY ON 15M DATA")
print("="*70)

for period_name, (yr_lo, yr_hi) in [('OOS1',splits['OOS1']),
                                      ('OOS2',splits['OOS2'])]:
    print(f"\n{'─'*60}")
    print(f"PERIOD: {period_name} ({yr_lo}-{yr_hi})")
    print(f"{'─'*60}")
    sub = period(df15, yr_lo, yr_hi)
    print(f"  Bars: {len(sub):,}")
    all_results[period_name] = run(sub, period_name)

# ── Statistical analysis ────────────────────────────────────────

print("\n" + "="*70)
print("RESULTS SUMMARY")
print("="*70)

for period_name in ['OOS1','OOS2']:
    res = all_results[period_name]
    n_hyp  = len(res)
    n_sig  = sum(1 for r in res if r['sig'])
    exp_fp = n_hyp * ALPHA
    bonf   = ALPHA / n_hyp if n_hyp else ALPHA

    print(f"\n{'─'*60}")
    print(f"  {period_name} — {n_hyp} hypotheses tested")
    print(f"  Significant (p<{ALPHA}):     {n_sig}")
    print(f"  Expected false positives: {exp_fp:.1f}")
    print(f"  Bonferroni threshold:     p < {bonf:.5f}")
    print(f"  Signal above noise:       {max(0,n_sig-exp_fp):.1f} excess results")
    print()

    # Sort by p-value
    sorted_res = sorted(res, key=lambda x: x['p'])

    print(f"  TOP 20 by p-value:")
    for r in sorted_res[:20]:
        pprint(r)
        print()

    # Bonferroni-corrected significant results
    bonf_sig = [r for r in res if r['p'] < bonf and r['ci_lo'] > 0]
    if bonf_sig:
        print(f"\n  BONFERRONI-SIGNIFICANT (p < {bonf:.5f}):")
        for r in bonf_sig:
            pprint(r, '    ')
    else:
        print(f"\n  No results survive Bonferroni correction.")

# ── Time-of-day heatmap ─────────────────────────────────────────

print("\n" + "="*70)
print("TIME-OF-DAY EFFECT (15M OOS 2016-2026 — raw mean return $/oz by hour)")
print("="*70)
oos_df = period(df15, 2016, 2026)
hourly = oos_df.groupby('hour')['bar_ret'].agg(['mean','std','count'])
hourly['se'] = hourly['std']/np.sqrt(hourly['count'])
hourly['t']  = hourly['mean']/hourly['se']
hourly['p']  = hourly.apply(lambda row: 2*(1-stats.t.cdf(abs(row['t']),
               df=max(1,row['count']-1))), axis=1)
n_hours = len(hourly)
print(f"  Bonferroni threshold: p < {ALPHA/n_hours:.4f}")
for hr, row in hourly.iterrows():
    sess = 'ASIA' if hr<7 else 'LONDON' if hr<13 else 'NY' if hr<20 else 'LATE'
    raw_sig  = ' *'  if row['p'] < ALPHA else ''
    bonf_sig = ' **BONF' if row['p'] < ALPHA/n_hours else ''
    print(f"  {hr:02d}:xx {sess:7s}  mean={row['mean']:+.4f}  n={int(row['count']):6,}  p={row['p']:.4f}{raw_sig}{bonf_sig}")

# ── Autocorrelation ─────────────────────────────────────────────

print("\n" + "="*70)
print("AUTOCORRELATION (15M OOS 2016-2026)")
print("="*70)
oos_ret = oos_df['bar_ret'].dropna()
for lag in [1,2,3,4,8,16,32,96]:
    ac = oos_ret.autocorr(lag=lag)
    n  = len(oos_ret)-lag
    se = 1/np.sqrt(n)
    sig = ' *' if abs(ac) > 2*se else ''
    print(f"  lag={lag:3d} bars ({lag*15:4d}min)  autocorr={ac:+.5f}  2σ={2*se:.5f}{sig}")

# ── Session comparison ──────────────────────────────────────────

print("\n" + "="*70)
print("VOLATILITY AND DIRECTIONAL BIAS BY SESSION (15M OOS 2016-2026)")
print("="*70)
for sess in ['ASIA','LONDON','NY','LATE']:
    sub = oos_df[oos_df['session']==sess]['bar_ret'].dropna()
    if len(sub) < 100: continue
    t,p = stats.ttest_1samp(sub, 0)
    print(f"  {sess:7s}  n={len(sub):6,}  mean={sub.mean():+.4f}  "
          f"std={sub.std():.4f}  p={p:.4f}{'  *' if p<0.05 else ''}")

# ── Regime comparison ───────────────────────────────────────────

print("\n" + "="*70)
print("YEAR-BY-YEAR MEAN RETURN (bias check across regimes)")
print("="*70)
for yr in sorted(oos_df['year'].unique()):
    sub = oos_df[oos_df['year']==yr]['bar_ret'].dropna()
    t,p = stats.ttest_1samp(sub, 0)
    mean_r = sub.mean()
    print(f"  {yr}  n={len(sub):6,}  mean={mean_r:+.4f}$/bar  p={p:.4f}{'  *' if p<0.05 else ''}")

# ── Final verdict ───────────────────────────────────────────────

print("\n" + "="*70)
print("SCIENTIFIC VERDICT")
print("="*70)
all_oos = all_results.get('OOS1',[]) + all_results.get('OOS2',[])
n_total = len(all_oos)
n_sig   = sum(1 for r in all_oos if r['sig'])
n_bonf  = sum(1 for r in all_oos if r.get('p',1) < ALPHA/max(n_total,1) and r.get('ci_lo',0)>0)
exp_fp  = n_total * ALPHA

print(f"\n  Total hypotheses tested (both OOS periods): {n_total}")
print(f"  Significant at p<{ALPHA}:     {n_sig}  (expected by chance: {exp_fp:.1f})")
print(f"  Survive Bonferroni (p<{ALPHA/max(n_total,1):.5f}): {n_bonf}")

if n_bonf > 0:
    print(f"\n  POSITIVE FINDING: {n_bonf} result(s) survive multiple-testing correction.")
    bonf_list = sorted([r for r in all_oos if r.get('p',1)<ALPHA/max(n_total,1) and r.get('ci_lo',0)>0],
                       key=lambda x: x['p'])
    for r in bonf_list:
        pprint(r, '    ')
elif n_sig > exp_fp * 1.5:
    print(f"\n  WEAK SIGNAL: {n_sig} significant results vs {exp_fp:.1f} expected.")
    print(f"  These hypotheses may contain a real but small edge.")
    print(f"  Requires further investigation with larger dataset and Monte Carlo.")
else:
    print(f"\n  NULL RESULT: {n_sig} significant results vs {exp_fp:.1f} expected.")
    print(f"  No evidence of statistically robust, economically tradeable edge")
    print(f"  in XAUUSD at 15M timeframe using these hypothesis families.")
    print(f"  Cannot reject the null hypothesis of no edge above trading costs.")

print(f"\n  COST ASSUMPTION: ${COST}/oz round-trip")
print(f"  A real edge must overcome this barrier consistently.")
print(f"  Many setups that appear profitable gross disappear net of cost.")
