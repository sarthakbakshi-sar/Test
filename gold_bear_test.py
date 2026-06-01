"""
GOLD BEAR MARKET SIMULATION
============================
Tests whether the proven model (score≥2 + VIX + June skip) holds up
in a declining gold market.

Tests run:
  1. PROVEN model — full period (baseline)
  2. BEAR period — May 2023 – Oct 2023 (gold fell ~$180 / -9%)
  3. BULL period — Nov 2023 – May 2026 (gold rallied ~+90%)
  4. SHORT trades only — across full period (does short side have edge?)
  5. No EMA50 short gate — removes bull-market guard, lets more shorts through
  6. INVERTED signals — flip all directions (simulates full bear market mechanics)
  7. BEAR period + inverted — worst case: pure bear, trading short pullbacks
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
print("  GOLD BEAR MARKET SIMULATION")
print("="*72)

# ── DATA FETCH ────────────────────────────────────────────────────────────────
print("\nFetching 3 years of 15m data from Twelve Data...")

def fetch_page(end_date):
    end_enc = urllib.parse.quote(end_date)
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol=XAU/USD&interval=15min&outputsize=5000"
           f"&apikey={API_KEY}&end_date={end_enc}&timezone=UTC")
    r = requests.get(url, timeout=30)
    j = r.json()
    if 'values' not in j:
        print(f"    API: {j.get('message','error')}")
        return None
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
    if dfp is None or len(dfp) == 0:
        print("empty"); break
    print(f"{len(dfp)} bars  ({dfp.index[0].date()} → {dfp.index[-1].date()})")
    all_frames.append(dfp)
    earliest = dfp.index[0]
    if earliest.strftime("%Y-%m-%d") <= target_start:
        print(f"  Reached target start ({target_start})")
        break
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

# Gold price direction by date (to label bear/bull)
gold_price_by_date = {d.date(): float(r['gold']) for d, r in dm.iterrows()}

# Determine bear/bull label for each date
dates_sorted = sorted(gold_price_by_date.keys())
gold_ema50_by_date = {d.date(): float(r['ema50']) for d, r in dm.iterrows()}
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
df['atr']   = tr.ewm(span=14, adjust=False).mean()
df.dropna(inplace=True)

# NFP days
def get_nfp_days(sy, ey):
    nfp = set()
    import datetime as dt
    for y in range(sy, ey+1):
        for m in range(1, 13):
            for d in range(1, 8):
                date = dt.date(y, m, d)
                if date.weekday() == 4:
                    nfp.add(date); break
    return nfp
nfp_days = get_nfp_days(2020, 2027)

print(f"  4H context: {g4.index[0].date()} → {g4.index[-1].date()}")

# ── IDENTIFY BEAR/BULL PERIODS ────────────────────────────────────────────────
# Gold bear period in data: May 2023 – Oct 2023 (gold fell from ~$2000 to ~$1820)
# Show gold price range in that window
bear_start = pd.Timestamp("2023-05-01").date()
bear_end   = pd.Timestamp("2023-10-31").date()
bull_start = pd.Timestamp("2023-11-01").date()

bear_prices = {d: p for d, p in gold_price_by_date.items() if bear_start <= d <= bear_end}
bull_prices = {d: p for d, p in gold_price_by_date.items() if d >= bull_start}

if bear_prices:
    bp = list(bear_prices.values())
    print(f"\n  Bear period (May–Oct 2023): gold ${min(bp):.0f} – ${max(bp):.0f}  "
          f"({(bp[-1]-bp[0])/bp[0]*100:+.1f}% change)")
if bull_prices:
    bp2 = list(bull_prices.values())
    print(f"  Bull period (Nov 2023–now): gold ${bp2[0]:.0f} → ${bp2[-1]:.0f}  "
          f"({(bp2[-1]-bp2[0])/bp2[0]*100:+.1f}% change)")

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def run(min_score=2,
        skip_vix_above=30,
        skip_months=None,
        skip_nfp=False,
        date_from=None,
        date_to=None,
        invert_signals=False,
        short_only=False,
        long_only=False,
        no_ema50_short_gate=False,
        entry_window=(14.0, 16.5),
        label=""):
    if skip_months is None:
        skip_months = [6]

    equity = ACCOUNT
    trades = []

    for date in sorted(set(df.index.date)):
        # Period filter
        if date_from and date < date_from: continue
        if date_to   and date > date_to:   continue

        score = macro_scores.get(date)
        if score is None or abs(score) < min_score:
            continue

        if skip_months and date.month in skip_months:
            continue
        if skip_nfp and date in nfp_days:
            continue
        if skip_vix_above is not None:
            vix_val = macro_vix.get(date)
            if vix_val is not None and vix_val >= skip_vix_above:
                continue

        macro_dir = 1 if score > 0 else -1
        if invert_signals:
            macro_dir = -macro_dir  # flip: simulate bear market

        if long_only  and macro_dir == -1: continue
        if short_only and macro_dir ==  1: continue

        ts   = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
        c_ts = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4: continue

        ctx = get_4h(o_ts)
        if ctx is None: continue

        # 4H EMA50 short gate (can be disabled)
        if not no_ema50_short_gate:
            if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']:
                continue

        tp  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
        vol = sbars['volume'].replace(0, 1)
        sbars = sbars.copy()
        sbars['vwap']     = (tp * vol).cumsum() / vol.cumsum()
        sbars['vwap_std'] = tp.expanding().std().bfill().fillna(1)

        in_trade = False
        entry_px = sl = tp1_px = tp2_px = sz1 = sz2 = pnl1 = 0
        tp1_hit  = False
        bars_held = 0
        trade_dir = 0
        trades_today = 0
        last_entry_i = -99

        for i in range(1, len(sbars)):
            b    = sbars.iloc[i]
            hi, lo, cls = b['high'], b['low'], b['close']
            vwap = b['vwap']
            std  = max(b['vwap_std'], 0.01)

            if entry_window is not None:
                h = b.name.hour + b.name.minute / 60
                if not (entry_window[0] <= h <= entry_window[1]):
                    continue

            if in_trade:
                bars_held += 1
                if trade_dir == 1:
                    if lo <= sl:
                        pnl = (sl - entry_px) * (sz1 + sz2) + pnl1
                        trades.append(dict(date=date, dir='LONG', entry=entry_px,
                            exit=sl, pnl=pnl, reason='SL', score=score, bars=bars_held))
                        equity += pnl; in_trade = False; continue
                    if not tp1_hit and hi >= tp1_px:
                        pnl1 = (tp1_px - entry_px) * sz1
                        equity += pnl1; sl = entry_px; tp1_hit = True
                    if tp1_hit and hi >= tp2_px:
                        pnl2 = (tp2_px - entry_px) * sz2
                        trades.append(dict(date=date, dir='LONG', entry=entry_px,
                            exit=tp2_px, pnl=pnl1+pnl2, reason='TP2', score=score, bars=bars_held))
                        equity += pnl2; in_trade = False; continue
                else:
                    if hi >= sl:
                        pnl = (entry_px - sl) * (sz1 + sz2) + pnl1
                        trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                            exit=sl, pnl=pnl, reason='SL', score=score, bars=bars_held))
                        equity += pnl; in_trade = False; continue
                    if not tp1_hit and lo <= tp1_px:
                        pnl1 = (entry_px - tp1_px) * sz1
                        equity += pnl1; sl = entry_px; tp1_hit = True
                    if tp1_hit and lo <= tp2_px:
                        pnl2 = (entry_px - tp2_px) * sz2
                        trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                            exit=tp2_px, pnl=pnl1+pnl2, reason='TP2', score=score, bars=bars_held))
                        equity += pnl2; in_trade = False; continue
                if bars_held >= TIME_STOP:
                    rem = sz2 if tp1_hit else (sz1 + sz2)
                    pnl = ((cls - entry_px) if trade_dir==1 else (entry_px - cls)) * rem + pnl1
                    trades.append(dict(date=date, dir='LONG' if trade_dir==1 else 'SHORT',
                        entry=entry_px, exit=cls, pnl=pnl, reason='TIME', score=score, bars=bars_held))
                    equity += pnl; in_trade = False
            else:
                if trades_today >= MAX_TRADES: continue
                if i - last_entry_i < MIN_BARS_BETWEEN: continue
                ctx_now = get_4h(b.name)
                if ctx_now is None: continue
                h4_ok = (macro_dir==1 and ctx_now['ema9']>ctx_now['ema21']) or \
                        (macro_dir==-1 and ctx_now['ema9']<ctx_now['ema21'])
                if not h4_ok: continue
                ema_ok = (macro_dir==1 and b['ema9']>b['ema21']) or \
                         (macro_dir==-1 and b['ema9']<b['ema21'])
                if not ema_ok: continue
                dist    = (cls - vwap) / std
                vwap_ok = (macro_dir==1 and -1.0<=dist<=0.3) or \
                          (macro_dir==-1 and -0.3<=dist<=1.0)
                if not vwap_ok: continue

                atr_v = float(b['atr'])
                if atr_v <= 0: continue
                tot_sz = (equity * RISK_PCT) / (SL_MULT * atr_v)
                sz1 = tot_sz * TP1_FRAC; sz2 = tot_sz * TP2_FRAC
                entry_px = cls
                if macro_dir == 1:
                    sl=entry_px-SL_MULT*atr_v; tp1_px=entry_px+TP1_MULT*atr_v; tp2_px=entry_px+TP2_MULT*atr_v
                else:
                    sl=entry_px+SL_MULT*atr_v; tp1_px=entry_px-TP1_MULT*atr_v; tp2_px=entry_px-TP2_MULT*atr_v
                in_trade=True; trade_dir=macro_dir
                bars_held=0; tp1_hit=False; pnl1=0
                trades_today+=1; last_entry_i=i

        if in_trade:
            cls = sbars.iloc[-1]['close']
            rem = sz2 if tp1_hit else (sz1+sz2)
            pnl = ((cls-entry_px) if trade_dir==1 else (entry_px-cls)) * rem + pnl1
            trades.append(dict(date=date, dir='LONG' if trade_dir==1 else 'SHORT',
                entry=entry_px, exit=cls, pnl=pnl, reason='SESS', score=score, bars=bars_held))
            equity += pnl

    return equity, trades


def summarize(eq, trs, label=""):
    if not trs:
        return dict(label=label, n=0, tpw=0, roi=0, wr=0, sharpe=0, pv=1.0, mdd=0,
                    avg_win=0, avg_loss=0, long_n=0, long_wr=0, short_n=0, short_wr=0)
    t      = pd.DataFrame(trs)
    n      = len(t)
    roi    = (eq - ACCOUNT) / ACCOUNT * 100
    wr     = t['pnl'].gt(0).mean() * 100
    wks    = max((pd.to_datetime(t['date']).max() - pd.to_datetime(t['date']).min()).days / 7, 1)
    tpw    = n / wks
    sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252) if t['pnl'].std() > 0 else 0
    eq_c   = ACCOUNT + t['pnl'].cumsum()
    mdd    = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
    _, pv  = stats.ttest_1samp(t['pnl'], 0) if n > 5 else (0, 1.0)
    aw     = t.loc[t['pnl']>0,'pnl'].mean() if t['pnl'].gt(0).any() else 0
    al     = t.loc[t['pnl']<0,'pnl'].mean() if t['pnl'].lt(0).any() else 0
    longs  = t[t['dir']=='LONG'];  long_wr  = longs['pnl'].gt(0).mean()*100 if len(longs) else 0
    shorts = t[t['dir']=='SHORT']; short_wr = shorts['pnl'].gt(0).mean()*100 if len(shorts) else 0
    return dict(label=label, n=n, tpw=round(tpw,1), roi=round(roi,1), wr=round(wr,1),
                sharpe=round(sharpe,2), pv=round(pv,4), mdd=round(mdd,1),
                avg_win=round(aw,0), avg_loss=round(al,0),
                long_n=len(longs), long_wr=round(long_wr,1),
                short_n=len(shorts), short_wr=round(short_wr,1))


# ── RUN TESTS ─────────────────────────────────────────────────────────────────
print("\nRunning bear market tests...\n")

PROVEN  = dict(min_score=2, skip_vix_above=30, skip_months=[6])
BEAR_P  = dict(date_from=bear_start, date_to=bear_end)
BULL_P  = dict(date_from=bull_start)

tests = [
    ("1. PROVEN model — full period",                  {**PROVEN}),
    ("2. BEAR period  (May–Oct 2023, gold -9%)",       {**PROVEN, **BEAR_P}),
    ("3. BULL period  (Nov 2023 – now, gold +90%)",    {**PROVEN, **BULL_P}),
    ("4. SHORT trades only  (full period)",             {**PROVEN, 'short_only':True}),
    ("5. SHORT trades + no EMA50 gate (full period)",  {**PROVEN, 'short_only':True, 'no_ema50_short_gate':True}),
    ("6. INVERTED signals  (simulate bear market)",    {**PROVEN, 'invert_signals':True}),
    ("7. BEAR period + INVERTED  (worst case)",        {**PROVEN, **BEAR_P, 'invert_signals':True}),
]

results = []
for label, kwargs in tests:
    eq, trs = run(**kwargs, label=label)
    r = summarize(eq, trs, label)
    results.append(r)
    sig = " ✓" if r['pv'] < 0.05 else ("~" if r['pv'] < 0.10 else "  ")
    print(f"  {label[:55]:<55} n={r['n']:>4}  WR={r['wr']:>5.1f}%  Sh={r['sharpe']:>6.2f}  p={r['pv']:.4f}{sig}  ROI={r['roi']:>+8.1f}%")

# ── RESULTS TABLE ─────────────────────────────────────────────────────────────
print(f"\n{'='*115}")
print("  BEAR MARKET SIMULATION — RESULTS")
print(f"{'='*115}")
print(f"  {'Test':<55} {'N':>5} {'WR':>7} {'Sharpe':>8} {'p-val':>8} {'ROI':>9} {'MDD':>7} {'AvgW':>7} {'AvgL':>7}")
print(f"  {'─'*110}")

for r in results:
    sig  = " ✓" if r['pv'] < 0.05 else ("~" if r['pv'] < 0.10 else "  ")
    print(f"  {r['label'][:55]:<55} {r['n']:>5} {r['wr']:>6.1f}% {r['sharpe']:>8.2f} "
          f"{r['pv']:>8.4f}{sig:>2} {r['roi']:>+8.1f}% {r['mdd']:>6.1f}% "
          f"${r['avg_win']:>+6.0f} ${r['avg_loss']:>+6.0f}")

# ── DIRECTION BREAKDOWN ────────────────────────────────────────────────────────
print(f"\n{'='*115}")
print("  LONG vs SHORT BREAKDOWN")
print(f"{'='*115}")
print(f"  {'Test':<55} {'LongN':>6} {'LongWR':>7} {'ShortN':>7} {'ShortWR':>8}")
print(f"  {'─'*90}")
for r in results:
    print(f"  {r['label'][:55]:<55} {r['long_n']:>6} {r['long_wr']:>6.1f}%  {r['short_n']:>6} {r['short_wr']:>7.1f}%")

# ── VERDICT ───────────────────────────────────────────────────────────────────
print(f"\n{'='*115}")
print("  BEAR MARKET VERDICT")
print(f"{'='*115}")

proven = results[0]
bear   = results[1]
bull   = results[2]
shorts = results[3]
inv    = results[5]
bear_inv = results[6]

print(f"""
  Full period (proven model):  Sharpe {proven['sharpe']}  WR {proven['wr']}%  ROI {proven['roi']:+.1f}%
  Bear period (May–Oct 2023):  Sharpe {bear['sharpe']}  WR {bear['wr']}%  ROI {bear['roi']:+.1f}%  (n={bear['n']})
  Bull period (Nov 2023–now):  Sharpe {bull['sharpe']}  WR {bull['wr']}%  ROI {bull['roi']:+.1f}%  (n={bull['n']})
""")

if bear['n'] < 10:
    print("  ⚠ Bear period has fewer than 10 trades — insufficient for statistical conclusion.")
    print("    The May–Oct 2023 period is the only gold decline in our data window.")

print(f"  Inverted signals (simulated full bear): Sharpe {inv['sharpe']}  WR {inv['wr']}%  ROI {inv['roi']:+.1f}%")
print(f"  Bear period + inverted (worst case):    Sharpe {bear_inv['sharpe']}  WR {bear_inv['wr']}%  ROI {bear_inv['roi']:+.1f}%")

verdict_lines = []
if bear['n'] >= 10 and bear['sharpe'] > 0.5:
    verdict_lines.append("✓ Proven model holds in the bear period — Sharpe positive")
elif bear['n'] < 10:
    verdict_lines.append("⚠ Not enough bear period data to confirm (only the Oct 2023 dip in our window)")
else:
    verdict_lines.append("✗ Proven model struggles in bear period")

if inv['sharpe'] > 0.5:
    verdict_lines.append("✓ Inverted model (simulated bear) works — VWAP pullback mechanics are direction-agnostic")
elif inv['sharpe'] > 0:
    verdict_lines.append("~ Inverted model marginal — some directional dependency")
else:
    verdict_lines.append("✗ Inverted model fails — system has strong bull-market bias")

if shorts['n'] >= 10 and shorts['short_wr'] >= 40:
    verdict_lines.append(f"✓ Short trades have WR {shorts['short_wr']}% — short side has edge")
else:
    verdict_lines.append(f"~ Short trades: WR {shorts['short_wr']}% n={shorts['n']} — inconclusive")

print()
for v in verdict_lines:
    print(f"  {v}")

print()
print(f"{'='*115}")

# Save trades for the proven model
eq0, trs0 = run(**dict(min_score=2, skip_vix_above=30, skip_months=[6]))
if trs0:
    t0 = pd.DataFrame(trs0)
    t0['date'] = pd.to_datetime(t0['date'])
    t0['bear_period'] = t0['date'].dt.date.apply(lambda d: bear_start <= d <= bear_end)
    t0.to_csv("gold_bear_trades.csv", index=False)
    print("Saved: gold_bear_trades.csv")
