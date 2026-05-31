"""
GOLD 15M — V13 FILTER A/B TEST
================================
Tests 4 V13 filters individually and in combination on top of the V2 baseline.

V13 FILTERS UNDER TEST:
  F1: MACD(12,26,9) histogram direction matches macro_dir
  F2: RSI(14) pullback zone  < 55 for longs / > 45 for shorts
  F3: Volume >= 120% of 20-bar average
  F4: OBV > OBV EMA10 for longs / OBV < OBV EMA10 for shorts

⚠ IMPORTANT — GOLD VOLUME CAVEAT:
  Twelve Data XAU/USD 'volume' = tick count (price-change events per bar).
  It is NOT exchange volume. The data pipeline in gold_15m_v2.py already
  fills NaN/zero with 1.0, meaning many bars have synthetic volume.
  F3 (Vol≥120%) and F4 (OBV) are tested anyway to confirm/deny their utility.
  Expect them to add noise, not signal.

BASELINE: V2 best config — score≥4, no confirm bar, window 14:00–16:30 UTC
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import requests, time, urllib.parse
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

API_KEY    = "06f050cd7d9940a895f9461e7f0ff7e3"
ACCOUNT    = 10_000.0
RISK_PCT   = 0.03
SL_MULT    = 0.75
TP1_MULT   = 1.5
TP2_MULT   = 2.5
TP1_FRAC   = 0.60
TP2_FRAC   = 0.40
TIME_STOP  = 6
MAX_TRADES = 2
MIN_BARS_BETWEEN = 4
SESS_OPEN  = (13, 30)
SESS_CLOSE = (17,  0)

print("="*72)
print("  GOLD 15M — V13 FILTER A/B TEST")
print("  Baseline: score≥4, window 14:00-16:30 UTC (V2 best config)")
print("="*72)

# ── DATA LOAD ─────────────────────────────────────────────────────────────────
print("\nFetching 15m data from Twelve Data...")

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
end_date   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
target_start = (datetime.utcnow() - timedelta(days=380)).strftime("%Y-%m-%d")
for page in range(8):
    print(f"  Page {page+1}  end={end_date[:10]}...", end=" ")
    dfp = fetch_page(end_date)
    if dfp is None or len(dfp) == 0: print("empty"); break
    print(f"{len(dfp)} bars  ({dfp.index[0].date()} → {dfp.index[-1].date()})")
    all_frames.append(dfp)
    earliest = dfp.index[0]
    if earliest.strftime("%Y-%m-%d") <= target_start: break
    end_date = (earliest - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    if page < 7: time.sleep(12)

gold_15m = pd.concat(all_frames).sort_index()
gold_15m = gold_15m[~gold_15m.index.duplicated(keep='first')]
gold_15m['volume'] = gold_15m.get('volume', pd.Series(1.0, index=gold_15m.index)).fillna(1.0).replace(0, 1.0)
print(f"  Total: {len(gold_15m)} bars | {gold_15m.index[0].date()} → {gold_15m.index[-1].date()}")

# Volume reality check
vol_ones = (gold_15m['volume'] == 1.0).mean() * 100
print(f"  ⚠ Volume = 1.0 (synthetic) on {vol_ones:.1f}% of bars — Vol/OBV filters are suspect")

print("\nLoading macro + 4H context...")
def dl(t, n):
    d = yf.download(t, period="3y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d['close']

gold_d = dl("GC=F",    "Gold")
dxy_d  = dl("DX-Y.NYB","DXY")
tnx_d  = dl("^TNX",   "10Y")
gdx_d  = dl("GDX",    "GDX")

dm = pd.DataFrame({'gold': gold_d})
dm['dxy'] = dxy_d.reindex(dm.index, method='ffill')
dm['tnx'] = tnx_d.reindex(dm.index, method='ffill')
dm['gdx'] = gdx_d.reindex(dm.index, method='ffill')
dm.dropna(inplace=True)
dm['ema50']    = dm['gold'].ewm(span=50, adjust=False).mean()
dm['s_trend']  = np.where(dm['gold'] > dm['ema50'], 1, -1)
dm['s_yield']  = np.where(dm['tnx'].diff() < 0, 1, -1)
dm['s_dxy']    = np.where(dm['dxy'].pct_change() < 0, 1, -1)
dm['s_mom']    = np.where(dm['gold'].pct_change(5) > 0, 1, -1)
dm['s_gdx']    = np.where(dm['gdx'].pct_change() > dm['gold'].pct_change(), 1, -1)
raw            = (dm['s_trend'] + dm['s_yield'] + dm['s_dxy'] + dm['s_mom'] + dm['s_gdx']).shift(1)
dm['score']    = (raw / 5 * 10).round(1)
dm.dropna(inplace=True)
macro_lkp = {d.date(): float(r['score']) for d, r in dm.iterrows()}

gold_1h = yf.download("GC=F", period="730d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index   = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)
g4 = gold_1h.resample('4h', label='left', closed='left').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
tr4 = pd.concat([g4['high'] - g4['low'],
                 (g4['high'] - g4['close'].shift()).abs(),
                 (g4['low']  - g4['close'].shift()).abs()], axis=1).max(axis=1)
g4['atr'] = tr4.ewm(span=14, adjust=False).mean()
def get_4h(ts):
    past = g4[g4.index <= ts]
    return past.iloc[-1] if len(past) else None

# ── 15m INDICATORS (including V13 additions) ─────────────────────────────────
df = gold_15m.copy()
df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
tr = pd.concat([df['high'] - df['low'],
                (df['high'] - df['close'].shift()).abs(),
                (df['low']  - df['close'].shift()).abs()], axis=1).max(axis=1)
df['atr'] = tr.ewm(span=14, adjust=False).mean()

# V13 F1: MACD(12,26,9) histogram
ema12          = df['close'].ewm(span=12, adjust=False).mean()
ema26          = df['close'].ewm(span=26, adjust=False).mean()
macd_line      = ema12 - ema26
signal_line    = macd_line.ewm(span=9, adjust=False).mean()
df['macd_hist'] = macd_line - signal_line

# V13 F2: RSI(14)
delta = df['close'].diff()
gain  = delta.clip(lower=0).rolling(14).mean()
loss  = (-delta.clip(upper=0)).rolling(14).mean()
df['rsi14'] = 100 - (100 / (1 + gain / (loss + 1e-10)))

# V13 F3: Volume surge (volume col already present, potentially synthetic)
df['vol_ma20'] = df['volume'].rolling(20).mean()
df['vol_surge'] = df['volume'] >= df['vol_ma20'] * 1.20

# V13 F4: OBV > OBV EMA10
obv            = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
df['obv']      = obv
df['obv_ema10'] = obv.ewm(span=10, adjust=False).mean()
df['obv_above'] = df['obv'] > df['obv_ema10']

df.dropna(inplace=True)
print(f"  Indicators computed on {len(df)} bars")

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def run(min_score=4, confirm_bar=False, entry_window=(14.0, 16.5),
        use_macd=False, use_rsi=False, use_vol=False, use_obv=False, label=""):
    equity = ACCOUNT
    trades = []

    for date in sorted(set(df.index.date)):
        score = macro_lkp.get(date)
        if score is None or abs(score) < min_score:
            continue
        macro_dir = 1 if score > 0 else -1
        ts    = pd.Timestamp(date)
        o_ts  = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
        c_ts  = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
        sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        if len(sbars) < 4:
            continue
        ctx = get_4h(o_ts)
        if ctx is None:
            continue
        # 4H EMA50 short gate (from V1)
        if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']:
            continue

        # Session VWAP
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
        pending_entry = False

        for i in range(1, len(sbars)):
            b    = sbars.iloc[i]
            prev = sbars.iloc[i - 1]
            hi, lo, cls = b['high'], b['low'], b['close']
            vwap = b['vwap']
            std  = max(b['vwap_std'], 0.01)

            # Entry window filter
            if entry_window is not None:
                h = b.name.hour + b.name.minute / 60
                if not (entry_window[0] <= h <= entry_window[1]):
                    pending_entry = False
                    continue

            if in_trade:
                bars_held += 1
                if trade_dir == 1:
                    if lo <= sl:
                        pnl = (sl - entry_px) * (sz1 + sz2) + pnl1
                        trades.append(dict(date=date, dir='LONG', entry=entry_px,
                            exit=sl, pnl=pnl, reason='SL', score=score, bars=bars_held))
                        equity += pnl
                        in_trade = False
                        continue
                    if not tp1_hit and hi >= tp1_px:
                        pnl1 = (tp1_px - entry_px) * sz1
                        equity += pnl1
                        sl = entry_px
                        tp1_hit = True
                    if tp1_hit and hi >= tp2_px:
                        pnl2 = (tp2_px - entry_px) * sz2
                        trades.append(dict(date=date, dir='LONG', entry=entry_px,
                            exit=tp2_px, pnl=pnl1 + pnl2, reason='TP2', score=score, bars=bars_held))
                        equity += pnl2
                        in_trade = False
                        continue
                else:
                    if hi >= sl:
                        pnl = (entry_px - sl) * (sz1 + sz2) + pnl1
                        trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                            exit=sl, pnl=pnl, reason='SL', score=score, bars=bars_held))
                        equity += pnl
                        in_trade = False
                        continue
                    if not tp1_hit and lo <= tp1_px:
                        pnl1 = (entry_px - tp1_px) * sz1
                        equity += pnl1
                        sl = entry_px
                        tp1_hit = True
                    if tp1_hit and lo <= tp2_px:
                        pnl2 = (entry_px - tp2_px) * sz2
                        trades.append(dict(date=date, dir='SHORT', entry=entry_px,
                            exit=tp2_px, pnl=pnl1 + pnl2, reason='TP2', score=score, bars=bars_held))
                        equity += pnl2
                        in_trade = False
                        continue
                if bars_held >= TIME_STOP:
                    rem = sz2 if tp1_hit else (sz1 + sz2)
                    pnl = ((cls - entry_px) if trade_dir == 1 else (entry_px - cls)) * rem + pnl1
                    trades.append(dict(date=date, dir='LONG' if trade_dir == 1 else 'SHORT',
                        entry=entry_px, exit=cls, pnl=pnl, reason='TIME', score=score, bars=bars_held))
                    equity += pnl
                    in_trade = False
            else:
                # Pending confirmation entry
                if confirm_bar and pending_entry:
                    confirmed = (macro_dir == 1 and cls > prev['close']) or \
                                (macro_dir == -1 and cls < prev['close'])
                    if confirmed:
                        atr_v = float(b['atr'])
                        if atr_v > 0:
                            tot_sz = (equity * RISK_PCT) / (SL_MULT * atr_v)
                            sz1 = tot_sz * TP1_FRAC
                            sz2 = tot_sz * TP2_FRAC
                            entry_px = cls
                            if macro_dir == 1:
                                sl     = entry_px - SL_MULT * atr_v
                                tp1_px = entry_px + TP1_MULT * atr_v
                                tp2_px = entry_px + TP2_MULT * atr_v
                            else:
                                sl     = entry_px + SL_MULT * atr_v
                                tp1_px = entry_px - TP1_MULT * atr_v
                                tp2_px = entry_px - TP2_MULT * atr_v
                            in_trade   = True
                            trade_dir  = macro_dir
                            bars_held  = 0
                            tp1_hit    = False
                            pnl1       = 0
                            trades_today += 1
                            last_entry_i = i
                    pending_entry = False
                    continue

                if trades_today >= MAX_TRADES:
                    continue
                if i - last_entry_i < MIN_BARS_BETWEEN:
                    continue

                ctx_now = get_4h(b.name)
                if ctx_now is None:
                    continue

                # 4H EMA9/21 trend
                h4_ok = (macro_dir == 1 and ctx_now['ema9'] > ctx_now['ema21']) or \
                        (macro_dir == -1 and ctx_now['ema9'] < ctx_now['ema21'])
                if not h4_ok:
                    continue

                # 15m EMA9/21 trend
                ema_ok = (macro_dir == 1 and b['ema9'] > b['ema21']) or \
                         (macro_dir == -1 and b['ema9'] < b['ema21'])
                if not ema_ok:
                    continue

                # VWAP pullback zone
                dist    = (cls - vwap) / std
                vwap_ok = (macro_dir == 1 and -1.0 <= dist <= 0.3) or \
                          (macro_dir == -1 and -0.3 <= dist <= 1.0)
                if not vwap_ok:
                    continue

                # ── V13 FILTERS ────────────────────────────────────────────
                if use_macd:
                    mh = b.get('macd_hist')
                    if pd.notna(mh):
                        if macro_dir == 1 and mh <= 0: continue
                        if macro_dir == -1 and mh >= 0: continue

                if use_rsi:
                    rv = b.get('rsi14')
                    if pd.notna(rv):
                        if macro_dir == 1 and rv >= 55: continue
                        if macro_dir == -1 and rv <= 45: continue

                if use_vol:
                    if not b.get('vol_surge', True): continue

                if use_obv:
                    obv_ok = b.get('obv_above', True)
                    if macro_dir == 1 and not obv_ok: continue
                    if macro_dir == -1 and obv_ok:    continue
                # ──────────────────────────────────────────────────────────

                if confirm_bar:
                    pending_entry = True
                    continue

                atr_v = float(b['atr'])
                if atr_v <= 0:
                    continue
                tot_sz   = (equity * RISK_PCT) / (SL_MULT * atr_v)
                sz1      = tot_sz * TP1_FRAC
                sz2      = tot_sz * TP2_FRAC
                entry_px = cls
                if macro_dir == 1:
                    sl     = entry_px - SL_MULT * atr_v
                    tp1_px = entry_px + TP1_MULT * atr_v
                    tp2_px = entry_px + TP2_MULT * atr_v
                else:
                    sl     = entry_px + SL_MULT * atr_v
                    tp1_px = entry_px - TP1_MULT * atr_v
                    tp2_px = entry_px - TP2_MULT * atr_v
                in_trade   = True
                trade_dir  = macro_dir
                bars_held  = 0
                tp1_hit    = False
                pnl1       = 0
                trades_today += 1
                last_entry_i = i

        if in_trade:
            cls = sbars.iloc[-1]['close']
            rem = sz2 if tp1_hit else (sz1 + sz2)
            pnl = ((cls - entry_px) if trade_dir == 1 else (entry_px - cls)) * rem + pnl1
            trades.append(dict(date=date, dir='LONG' if trade_dir == 1 else 'SHORT',
                entry=entry_px, exit=cls, pnl=pnl, reason='SESS', score=score, bars=bars_held))
            equity += pnl

    return equity, trades


# ── VARIANT DEFINITIONS ───────────────────────────────────────────────────────
BASE = dict(min_score=4, confirm_bar=False, entry_window=(14.0, 16.5))

variants = [
    ("BASELINE         (V2 best: score≥4, window)",
     {**BASE, 'use_macd':False, 'use_rsi':False, 'use_vol':False, 'use_obv':False}),
    ("+ F1 MACD hist",
     {**BASE, 'use_macd':True,  'use_rsi':False, 'use_vol':False, 'use_obv':False}),
    ("+ F2 RSI pullback (<55/>45)",
     {**BASE, 'use_macd':False, 'use_rsi':True,  'use_vol':False, 'use_obv':False}),
    ("+ F3 Vol surge (≥120%) ⚠synthetic",
     {**BASE, 'use_macd':False, 'use_rsi':False, 'use_vol':True,  'use_obv':False}),
    ("+ F4 OBV > EMA10 ⚠synthetic",
     {**BASE, 'use_macd':False, 'use_rsi':False, 'use_vol':False, 'use_obv':True}),
    ("+ F1+F2 MACD+RSI",
     {**BASE, 'use_macd':True,  'use_rsi':True,  'use_vol':False, 'use_obv':False}),
    ("+ F1+F3 MACD+Vol ⚠synthetic vol",
     {**BASE, 'use_macd':True,  'use_rsi':False, 'use_vol':True,  'use_obv':False}),
    ("+ F1+F4 MACD+OBV ⚠synthetic obv",
     {**BASE, 'use_macd':True,  'use_rsi':False, 'use_vol':False, 'use_obv':True}),
    ("+ F2+F3 RSI+Vol ⚠synthetic vol",
     {**BASE, 'use_macd':False, 'use_rsi':True,  'use_vol':True,  'use_obv':False}),
    ("+ F2+F4 RSI+OBV ⚠synthetic obv",
     {**BASE, 'use_macd':False, 'use_rsi':True,  'use_vol':False, 'use_obv':True}),
    ("+ ALL F1-F4 ⚠synthetic vol/obv",
     {**BASE, 'use_macd':True,  'use_rsi':True,  'use_vol':True,  'use_obv':True}),
]

# ── RUN ALL VARIANTS ──────────────────────────────────────────────────────────
print("\nRunning variants...")
results = []
for label, kwargs in variants:
    eq, trs = run(**kwargs)
    if not trs:
        results.append((label, 0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0))
        print(f"  {label[:50]:<50}  n=   0  NO TRADES")
        continue
    t      = pd.DataFrame(trs)
    n      = len(t)
    roi    = (eq - ACCOUNT) / ACCOUNT * 100
    wr     = t['pnl'].gt(0).mean() * 100
    wks    = max((pd.to_datetime(t['date']).max() -
                  pd.to_datetime(t['date']).min()).days / 7, 1)
    tpw    = n / wks
    sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252) if t['pnl'].std() > 0 else 0
    eq_c   = ACCOUNT + t['pnl'].cumsum()
    mdd    = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
    _, pv  = stats.ttest_1samp(t['pnl'], 0)
    results.append((label, n, tpw, roi, wr, sharpe, pv, mdd))
    sig = " ✓" if pv < 0.05 else ("~" if pv < 0.10 else " ")
    print(f"  {label[:50]:<50}  n={n:>4}  p={pv:.4f}{sig}  ROI={roi:>+7.1f}%  WR={wr:.1f}%")

# ── COMPARISON TABLE ──────────────────────────────────────────────────────────
print(f"\n{'='*100}")
print(f"  GOLD V13 FILTER COMPARISON")
print(f"{'='*100}")
print(f"  {'Variant':<48} {'N':>5} {'TPW':>5} {'ROI':>8} {'WR':>7} {'Sharpe':>7} {'p-val':>8} {'MDD':>7}")
print(f"  {'─'*95}")

baseline_sharpe = results[0][5]
baseline_wr     = results[0][4]

for i, (label, n, tpw, roi, wr, sharpe, pv, mdd) in enumerate(results):
    sig     = " ✓" if pv < 0.05 else ("~" if pv < 0.10 else "  ")
    delta_s = f" ({sharpe-baseline_sharpe:+.2f})" if i > 0 and n > 0 else ""
    flag    = "  ← BASELINE" if i == 0 else ""
    print(f"  {label:<48} {n:>5} {tpw:>4.1f} {roi:>+7.1f}% {wr:>6.1f}% {sharpe:>7.2f}{delta_s:<8} {pv:>8.4f}{sig:>3} {mdd:>6.1f}%{flag}")

# ── BEST VARIANT DETAIL ───────────────────────────────────────────────────────
valid = [(l,n,tpw,roi,wr,sh,pv,mdd) for (l,n,tpw,roi,wr,sh,pv,mdd) in results if n >= 30]
best  = max(valid, key=lambda x: x[5]) if valid else results[0]
best_label  = best[0]
best_kwargs = next(k for l, k in [(v[0], v[1]) for v in variants] if l == best_label)

print(f"\n{'='*100}")
print(f"  BEST BY SHARPE: {best_label.strip()}")
print(f"{'='*100}")
eq_best, trs_best = run(**best_kwargs)
t = pd.DataFrame(trs_best)
t['date'] = pd.to_datetime(t['date'])
t['win']  = t['pnl'] > 0
t['month'] = t['date'].dt.to_period('M')
n   = len(t)
_, pv = stats.ttest_1samp(t['pnl'], 0)
roi = (eq_best - ACCOUNT) / ACCOUNT * 100
wks = max((t['date'].max() - t['date'].min()).days / 7, 1)
eq_c   = ACCOUNT + t['pnl'].cumsum()
mdd    = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252)
print(f"""
  $10,000 → ${eq_best:,.0f}  ({roi:+.1f}% ROI)
  Sharpe : {sharpe:.2f}    Max drawdown: {mdd:.1f}%
  Trades : {n}  ({n/wks:.1f}/week)
  WR     : {t['win'].mean()*100:.1f}%   Avg P&L: ${t['pnl'].mean():+,.0f}
  p-value: {pv:.4f}  {'✓ SIGNIFICANT' if pv < 0.05 else '~ borderline' if pv < 0.10 else '— not significant'}
""")

print("  Direction:")
for d, g in t.groupby('dir'):
    print(f"    {d:<6} {len(g):>4}tr | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

print("\n  Exit reasons:")
for r, g in t.groupby('reason'):
    print(f"    {r:<6} {len(g):>4} | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

print("\n  Monthly:")
pos = 0
run_eq = ACCOUNT
for mo, g in t.groupby('month'):
    run_eq += g['pnl'].sum()
    flag = "✓" if g['pnl'].sum() > 0 else "✗"
    if g['pnl'].sum() > 0: pos += 1
    print(f"    {str(mo)}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+8,.0f}  ${run_eq:>8,.0f} {flag}")
print(f"\n  Positive months: {pos}/{t['month'].nunique()}")

# ── VERDICT ───────────────────────────────────────────────────────────────────
print(f"\n{'='*100}")
print("  VERDICT — what to add to gold_signal.gs")
print(f"{'='*100}")
for label, n, tpw, roi, wr, sharpe, pv, mdd in results[1:]:
    delta = sharpe - baseline_sharpe
    dwr   = wr - baseline_wr
    rec   = "ADD" if (delta > 0.1 and dwr > 0) else ("NEUTRAL" if abs(delta) <= 0.1 else "SKIP")
    print(f"  {rec:<8} {label[:55]:<55}  ΔSharpe={delta:+.2f}  ΔWR={dwr:+.1f}%")

t.to_csv("gold_v13_filter_trades.csv", index=False)
print("\nSaved: gold_v13_filter_trades.csv")
print("="*100)
