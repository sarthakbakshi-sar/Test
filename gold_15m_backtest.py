"""
GOLD 15M SYSTEM — FULL BACKTEST (Twelve Data)
===============================================
Fetches 1 year of 15m XAU/USD from Twelve Data API.
Runs same rules as gold_combined_v2.py:
  - Macro score gate (|score| >= 2)
  - 4H EMA trend filter (shorts only below 4H EMA50)
  - EMA9/21 on 15m for direction
  - VWAP zone touch entry
  - Up to 3 entries per session
  - SL 0.75×ATR, TP1 1.5×ATR (60%), TP2 2.5×ATR (40%)
  - Session: 13:30–17:00 UTC

$10,000 portfolio | 3% risk per trade
"""

import requests, time, urllib.parse
import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
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
TIME_STOP = 6           # 6 × 15m = 90 min time stop
MAX_TRADES= 3
MIN_BARS_BETWEEN = 4    # 4 × 15m = 60 min between re-entries
MIN_SCORE = 2
SESS_OPEN = (13, 30)
SESS_CLOSE= (17,  0)

print("="*68)
print("  GOLD 15M SYSTEM — TWELVE DATA BACKTEST")
print("  $10,000 | 3% risk | 15m bars | 1 year")
print("="*68)

# ── FETCH 15M DATA FROM TWELVE DATA ──────────────────────────────────────────
print("\nFetching 15m XAU/USD from Twelve Data (~1 year, 7 pages)...")

def fetch_page(end_date):
    end_enc = urllib.parse.quote(end_date)
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol=XAU/USD&interval=15min&outputsize=5000"
           f"&apikey={API_KEY}&end_date={end_enc}&timezone=UTC")
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}"); return None
    j = r.json()
    if 'values' not in j:
        print(f"  API error: {j.get('message','unknown')}"); return None
    rows = j['values']
    df = pd.DataFrame(rows)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    df.sort_index(inplace=True)
    for c in ['open','high','low','close','volume']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    df.dropna(subset=['open','high','low','close'], inplace=True)
    return df

all_frames = []
end_date   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
target_start = (datetime.utcnow() - timedelta(days=380)).strftime("%Y-%m-%d")

for page in range(8):
    print(f"  Page {page+1}/8  end={end_date[:10]}...", end=" ")
    df_page = fetch_page(end_date)
    if df_page is None or len(df_page) == 0:
        print("empty — stopping"); break
    print(f"{len(df_page)} bars  ({df_page.index[0].date()} → {df_page.index[-1].date()})")
    all_frames.append(df_page)
    earliest = df_page.index[0]
    if earliest.strftime("%Y-%m-%d") <= target_start:
        print(f"  Reached target start date — done"); break
    end_date = (earliest - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    if page < 7:
        time.sleep(12)

if not all_frames:
    print("ERROR: no data fetched"); exit()

gold_15m = pd.concat(all_frames).sort_index()
gold_15m = gold_15m[~gold_15m.index.duplicated(keep='first')]
if 'volume' not in gold_15m.columns:
    gold_15m['volume'] = 1.0
gold_15m['volume'] = gold_15m['volume'].fillna(1.0).replace(0, 1.0)
print(f"\n  Total: {len(gold_15m)} bars | {gold_15m.index[0].date()} → {gold_15m.index[-1].date()}")

# ── DAILY MACRO DATA ──────────────────────────────────────────────────────────
print("\nDownloading macro data...")

def dl(t, n):
    d = yf.download(t, period="3y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(None)
    print(f"  {n}: {len(d)} days"); return d['close']

gold_d = dl("GC=F","Gold"); dxy_d = dl("DX-Y.NYB","DXY")
tnx_d  = dl("^TNX","10Y");  gdx_d = dl("GDX","GDX")

# ── MACRO SCORES ──────────────────────────────────────────────────────────────
dm = pd.DataFrame({'gold': gold_d})
dm['dxy'] = dxy_d.reindex(dm.index, method='ffill')
dm['tnx'] = tnx_d.reindex(dm.index, method='ffill')
dm['gdx'] = gdx_d.reindex(dm.index, method='ffill')
dm.dropna(inplace=True)
dm['ema50']   = dm['gold'].ewm(span=50, adjust=False).mean()
dm['s_trend'] = np.where(dm['gold'] > dm['ema50'], 1, -1)
dm['s_yield'] = np.where(dm['tnx'].diff() < 0, 1, -1)
dm['s_dxy']   = np.where(dm['dxy'].pct_change() < 0, 1, -1)
dm['s_mom']   = np.where(dm['gold'].pct_change(5) > 0, 1, -1)
dm['s_gdx']   = np.where(dm['gdx'].pct_change() > dm['gold'].pct_change(), 1, -1)
raw = (dm['s_trend']+dm['s_yield']+dm['s_dxy']+dm['s_mom']+dm['s_gdx']).shift(1)
dm['score'] = (raw / 5 * 10).round(1)
dm.dropna(inplace=True)
macro_lkp = {d.date(): float(r['score']) for d, r in dm.iterrows()}

# ── 4H CONTEXT FROM 1H DATA ───────────────────────────────────────────────────
print("Building 4H context...")
gold_1h = yf.download("GC=F", period="730d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index   = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)

g4 = gold_1h.resample('4h', label='left', closed='left').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
tr4 = pd.concat([g4['high']-g4['low'],
                 (g4['high']-g4['close'].shift()).abs(),
                 (g4['low'] -g4['close'].shift()).abs()],axis=1).max(axis=1)
g4['atr'] = tr4.ewm(span=14, adjust=False).mean()

def get_4h(ts):
    past = g4[g4.index <= ts]
    return past.iloc[-1] if len(past) else None

# ── 15M INDICATORS ────────────────────────────────────────────────────────────
print("Computing 15m indicators...")
df = gold_15m.copy()
df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
tr = pd.concat([df['high']-df['low'],
                (df['high']-df['close'].shift()).abs(),
                (df['low'] -df['close'].shift()).abs()],axis=1).max(axis=1)
df['atr'] = tr.ewm(span=14, adjust=False).mean()

# ── BACKTEST ──────────────────────────────────────────────────────────────────
print("Running 15m backtest...")

equity = ACCOUNT
trades = []

for date in sorted(set(df.index.date)):
    score = macro_lkp.get(date)
    if score is None or abs(score) < MIN_SCORE: continue
    macro_dir = 1 if score > 0 else -1

    ts  = pd.Timestamp(date)
    o_ts = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
    c_ts = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
    sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
    if len(sbars) < 4: continue

    # 4H short gate
    ctx = get_4h(o_ts)
    if ctx is None: continue
    if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']:
        continue

    # Session VWAP
    tp  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
    vol = sbars['volume'].replace(0, 1)
    sbars = sbars.copy()
    sbars['vwap']     = (tp * vol).cumsum() / vol.cumsum()
    sbars['vwap_std'] = tp.expanding().std().bfill().fillna(1)

    in_trade  = False
    entry_px = sl = tp1_px = tp2_px = sz1 = sz2 = pnl1 = 0
    tp1_hit = False; bars_held = 0; trade_dir = 0
    trades_today = 0; last_entry_i = -99

    for i in range(1, len(sbars)):
        b = sbars.iloc[i]
        hi, lo, cls = b['high'], b['low'], b['close']
        vwap = b['vwap']
        std  = max(b['vwap_std'], 0.01)

        if in_trade:
            bars_held += 1
            if trade_dir == 1:
                if lo <= sl:
                    pnl = (sl - entry_px)*(sz1+sz2) + pnl1
                    trades.append(dict(date=date,dir='LONG',entry=entry_px,exit=sl,
                        pnl=pnl,reason='SL',score=score,bars=bars_held)); equity+=pnl; in_trade=False; continue
                if not tp1_hit and hi >= tp1_px:
                    pnl1=(tp1_px-entry_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                if tp1_hit and hi >= tp2_px:
                    pnl2=(tp2_px-entry_px)*sz2
                    trades.append(dict(date=date,dir='LONG',entry=entry_px,exit=tp2_px,
                        pnl=pnl1+pnl2,reason='TP2',score=score,bars=bars_held)); equity+=pnl2; in_trade=False; continue
            else:
                if hi >= sl:
                    pnl = (entry_px-sl)*(sz1+sz2) + pnl1
                    trades.append(dict(date=date,dir='SHORT',entry=entry_px,exit=sl,
                        pnl=pnl,reason='SL',score=score,bars=bars_held)); equity+=pnl; in_trade=False; continue
                if not tp1_hit and lo <= tp1_px:
                    pnl1=(entry_px-tp1_px)*sz1; equity+=pnl1; sl=entry_px; tp1_hit=True
                if tp1_hit and lo <= tp2_px:
                    pnl2=(entry_px-tp2_px)*sz2
                    trades.append(dict(date=date,dir='SHORT',entry=entry_px,exit=tp2_px,
                        pnl=pnl1+pnl2,reason='TP2',score=score,bars=bars_held)); equity+=pnl2; in_trade=False; continue
            if bars_held >= TIME_STOP:
                rem = sz2 if tp1_hit else (sz1+sz2)
                pnl = ((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem + pnl1
                trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',
                    entry=entry_px,exit=cls,pnl=pnl,reason='TIME',score=score,bars=bars_held))
                equity+=pnl; in_trade=False
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

            dist = (cls - vwap) / std
            vwap_ok = (macro_dir==1 and -1.0<=dist<=0.3) or \
                      (macro_dir==-1 and -0.3<=dist<=1.0)
            if not vwap_ok: continue

            atr_v = float(b['atr'])
            if atr_v <= 0: continue
            tot_sz = (equity*RISK_PCT) / (SL_MULT*atr_v)
            sz1=tot_sz*TP1_FRAC; sz2=tot_sz*TP2_FRAC
            entry_px=cls
            if macro_dir==1:
                sl=entry_px-SL_MULT*atr_v; tp1_px=entry_px+TP1_MULT*atr_v; tp2_px=entry_px+TP2_MULT*atr_v
            else:
                sl=entry_px+SL_MULT*atr_v; tp1_px=entry_px-TP1_MULT*atr_v; tp2_px=entry_px-TP2_MULT*atr_v
            in_trade=True; trade_dir=macro_dir; bars_held=0; tp1_hit=False; pnl1=0
            trades_today+=1; last_entry_i=i

    if in_trade:
        cls = sbars.iloc[-1]['close']
        rem = sz2 if tp1_hit else (sz1+sz2)
        pnl = ((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem + pnl1
        trades.append(dict(date=date,dir='LONG' if trade_dir==1 else 'SHORT',
            entry=entry_px,exit=cls,pnl=pnl,reason='SESS',score=score,bars=bars_held))
        equity+=pnl

# ── RESULTS ───────────────────────────────────────────────────────────────────
if not trades:
    print("No trades generated"); exit()

t = pd.DataFrame(trades)
t['date']  = pd.to_datetime(t['date'])
t['win']   = t['pnl'] > 0
t['month'] = t['date'].dt.to_period('M')

n      = len(t)
wr     = t['win'].mean()*100
roi    = (equity-ACCOUNT)/ACCOUNT*100
days   = (t['date'].max()-t['date'].min()).days
wks    = max(days/7, 1)
tpw    = n/wks
avg_w  = t.loc[t['win'],'pnl'].mean()
avg_l  = t.loc[~t['win'],'pnl'].mean()
rr     = abs(avg_w/avg_l) if avg_l!=0 else 0
_,pval = stats.ttest_1samp(t['pnl'], 0)
eq_c   = ACCOUNT + t['pnl'].cumsum()
mdd    = ((eq_c-eq_c.cummax())/eq_c.cummax()*100).min()
sharpe = t['pnl'].mean()/t['pnl'].std()*np.sqrt(252) if t['pnl'].std()>0 else 0

print(f"\n{'='*68}")
print(f"  RESULTS — 15M SYSTEM")
print(f"  {t['date'].min().date()} → {t['date'].max().date()}")
print(f"{'='*68}")
print(f"""
  $10,000  →  ${equity:,.0f}  ({roi:+.1f}% ROI)
  Sharpe   : {sharpe:.2f}    Max drawdown: {mdd:.1f}%

  Trades   : {n}  ({tpw:.1f}/week)
  Win rate : {wr:.1f}%
  Avg win  : ${avg_w:+,.0f}    Avg loss: ${avg_l:+,.0f}    RR: {rr:.2f}x
  p-value  : {pval:.4f}  {'✓ SIGNIFICANT' if pval<0.05 else '— not significant'}""")

print(f"\n  Direction:")
for d, g in t.groupby('dir'):
    print(f"    {d:<6} {len(g):>4}tr | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f} | total ${g['pnl'].sum():+,.0f}")

print(f"\n  Exit reasons:")
for r, g in t.groupby('reason'):
    print(f"    {r:<6} {len(g):>4} | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

print(f"\n  Monthly:")
run = ACCOUNT
pos = 0
for mo, g in t.groupby('month'):
    run += g['pnl'].sum()
    flag = "✓" if g['pnl'].sum()>0 else "✗"
    if g['pnl'].sum()>0: pos+=1
    print(f"    {str(mo)}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+8,.0f}  ${run:>8,.0f} {flag}")
print(f"\n  Positive months: {pos}/{t['month'].nunique()}")

print(f"\n  By macro score strength:")
print(f"  {'Score':>10} {'N':>5} {'WR':>7} {'Avg P&L':>9}")
for (lo,hi),lbl in [((-10.1,-6),'≤−6'),((-6,-2),'−6..−2'),((2,6),'+2..+6'),((6,10.1),'≥+6')]:
    g = t[(t['score']>lo)&(t['score']<=hi)]
    if len(g)<3: continue
    print(f"  {lbl:>10} {len(g):>5} {g['win'].mean()*100:>6.0f}% ${g['pnl'].mean():>+8,.0f}")

print(f"\n{'='*68}")
if pval<0.05 and roi>0:
    print(f"  ✓ PROFITABLE & SIGNIFICANT — {roi:+.1f}% ROI, {tpw:.1f} trades/week")
elif roi>0:
    print(f"  ~ Profitable (p={pval:.3f}) — {roi:+.1f}% ROI, {tpw:.1f} trades/week")
else:
    print(f"  ✗ Not profitable — {roi:+.1f}% ROI, {tpw:.1f} trades/week")
print("="*68)

t.to_csv("gold_15m_trades.csv", index=False)
print("Saved: gold_15m_trades.csv")
