"""
GOLD MULTI-SESSION BACKTEST
============================
Tests the same entry logic across three separate sessions:

  SESSION A  — Asia         00:00–06:00 UTC  (04:00–10:00 Dubai)
  SESSION B  — London open  08:00–13:30 UTC  (12:00–17:30 Dubai)
  SESSION C  — London/NY    13:30–17:00 UTC  (17:30–21:00 Dubai) ← proven

Each session gets its own VWAP (reset at session open).
Entry logic unchanged: EMA9/21 alignment, VWAP zone, 4H trend filter.
Best entry window for London/NY already known (14:00–16:30 UTC).
Asia and London open use the full window minus first/last 30 min.
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

SESSIONS = {
    'Asia':       {'open': (0,0),   'close': (6,0),   'entry_start': 0.5,   'entry_end': 5.5},
    'London':     {'open': (8,0),   'close': (13,30),  'entry_start': 8.5,   'entry_end': 12.5},
    'London_NY':  {'open': (13,30), 'close': (17,0),   'entry_start': 14.0,  'entry_end': 16.5},
}

print("="*68)
print("  GOLD MULTI-SESSION BACKTEST")
print("  Asia vs London Open vs London/NY overlap")
print("="*68)

# ── FETCH 15M DATA ─────────────────────────────────────────────────────────────
print("\nFetching 15m XAU/USD from Twelve Data...")

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

# ── MACRO + 4H CONTEXT ────────────────────────────────────────────────────────
print("\nLoading macro + 4H data from yfinance...")

def dl(ticker):
    d = yf.download(ticker, period="3y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d['close']

gold_d = dl("GC=F"); dxy_d = dl("DX-Y.NYB")
tnx_d  = dl("^TNX"); gdx_d = dl("GDX")

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
raw = (dm['s_trend'] + dm['s_yield'] + dm['s_dxy'] + dm['s_mom'] + dm['s_gdx']).shift(1)
dm['score'] = (raw / 5 * 10).round(1)
dm.dropna(inplace=True)
macro_lkp = {d.date(): float(r['score']) for d, r in dm.iterrows()}

gold_1h = yf.download("GC=F", period="730d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)
g4 = gold_1h.resample('4h', label='left', closed='left').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
tr4 = pd.concat([g4['high']-g4['low'],
                 (g4['high']-g4['close'].shift()).abs(),
                 (g4['low'] -g4['close'].shift()).abs()], axis=1).max(axis=1)
g4['atr'] = tr4.ewm(span=14, adjust=False).mean()

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

# ── BACKTEST ENGINE ────────────────────────────────────────────────────────────
def run_session(sess_cfg, label=""):
    o_h, o_m   = sess_cfg['open']
    c_h, c_m   = sess_cfg['close']
    ew_start   = sess_cfg['entry_start']
    ew_end     = sess_cfg['entry_end']

    equity = ACCOUNT; trades = []

    for date in sorted(set(df.index.date)):
        score = macro_lkp.get(date)
        if score is None or abs(score) < 2: continue
        macro_dir = 1 if score > 0 else -1

        ts    = pd.Timestamp(date)
        o_ts  = ts + pd.Timedelta(hours=o_h, minutes=o_m)
        c_ts  = ts + pd.Timedelta(hours=c_h, minutes=c_m)

        # Asia session crosses midnight — bars span into next calendar day
        if o_h == 0 and c_h <= 6:
            sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()
        else:
            sbars = df.loc[(df.index >= o_ts) & (df.index <= c_ts)].copy()

        if len(sbars) < 4: continue

        ctx = get_4h(o_ts)
        if ctx is None: continue

        # 4H short gate: only short when price below 4H EMA50
        if macro_dir == -1 and sbars.iloc[0]['close'] > ctx['ema50']:
            continue

        # Session VWAP (reset at session open for this session)
        tp_val  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
        vol_val = sbars['volume'].replace(0, 1)
        sbars = sbars.copy()
        sbars['vwap']     = (tp_val * vol_val).cumsum() / vol_val.cumsum()
        sbars['vwap_std'] = tp_val.expanding().std().bfill().fillna(1)

        in_trade = False
        entry_px = sl = tp1_px = tp2_px = sz1 = sz2 = pnl1 = 0
        tp1_hit  = False; bars_held = 0; trade_dir = 0
        trades_today = 0; last_entry_i = -99

        for i in range(1, len(sbars)):
            b    = sbars.iloc[i]
            hi, lo, cls = b['high'], b['low'], b['close']
            vwap = b['vwap']
            std  = max(b['vwap_std'], 0.01)

            # Entry window filter
            bh = b.name.hour + b.name.minute / 60
            in_window = ew_start <= bh <= ew_end
            if not in_window: continue

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
                    pnl = ((cls - entry_px) if trade_dir == 1 else (entry_px - cls)) * rem + pnl1
                    trades.append(dict(date=date, dir='LONG' if trade_dir==1 else 'SHORT',
                        entry=entry_px, exit=cls, pnl=pnl, reason='TIME', score=score, bars=bars_held))
                    equity += pnl; in_trade = False
            else:
                if trades_today >= MAX_TRADES: continue
                if i - last_entry_i < MIN_BARS_BETWEEN: continue

                ctx_now = get_4h(b.name)
                if ctx_now is None: continue
                h4_ok = ((macro_dir == 1 and ctx_now['ema9'] > ctx_now['ema21']) or
                         (macro_dir == -1 and ctx_now['ema9'] < ctx_now['ema21']))
                if not h4_ok: continue

                ema_ok = ((macro_dir == 1 and b['ema9'] > b['ema21']) or
                          (macro_dir == -1 and b['ema9'] < b['ema21']))
                if not ema_ok: continue

                dist   = (cls - vwap) / std
                vwap_ok = ((macro_dir == 1 and -1.0 <= dist <= 0.3) or
                           (macro_dir == -1 and -0.3 <= dist <= 1.0))
                if not vwap_ok: continue

                atr_v = float(b['atr'])
                if atr_v <= 0: continue
                tot_sz = (equity * RISK_PCT) / (SL_MULT * atr_v)
                sz1 = tot_sz * TP1_FRAC; sz2 = tot_sz * TP2_FRAC
                entry_px = cls
                if macro_dir == 1:
                    sl    = entry_px - SL_MULT * atr_v
                    tp1_px = entry_px + TP1_MULT * atr_v
                    tp2_px = entry_px + TP2_MULT * atr_v
                else:
                    sl    = entry_px + SL_MULT * atr_v
                    tp1_px = entry_px - TP1_MULT * atr_v
                    tp2_px = entry_px - TP2_MULT * atr_v
                in_trade = True; trade_dir = macro_dir
                bars_held = 0; tp1_hit = False; pnl1 = 0
                trades_today += 1; last_entry_i = i

        if in_trade:
            cls = sbars.iloc[-1]['close']
            rem = sz2 if tp1_hit else (sz1 + sz2)
            pnl = ((cls - entry_px) if trade_dir == 1 else (entry_px - cls)) * rem + pnl1
            trades.append(dict(date=date, dir='LONG' if trade_dir==1 else 'SHORT',
                entry=entry_px, exit=cls, pnl=pnl, reason='SESS', score=score, bars=bars_held))
            equity += pnl

    return equity, trades

# ── RUN ALL THREE SESSIONS ─────────────────────────────────────────────────────
print("\nRunning sessions...")
session_results = {}
for name, cfg in SESSIONS.items():
    print(f"  {name}...", end=" ", flush=True)
    eq, trs = run_session(cfg, label=name)
    session_results[name] = (eq, trs)
    print(f"done ({len(trs)} trades)")

# ── SUMMARY TABLE ─────────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"  SESSION COMPARISON  (same entry logic, same macro filter, same exits)")
print(f"{'='*80}")
print(f"  {'Session':<14} {'UTC Window':<18} {'Dubai':<18} {'N':>5} {'TPW':>5} {'ROI':>8} {'WR':>7} {'Sharpe':>7} {'p-val':>8} {'MDD':>7}")
print(f"  {'─'*96}")

utc_labels = {
    'Asia':      '00:30–05:30',
    'London':    '08:30–12:30',
    'London_NY': '14:00–16:30',
}
dubai_labels = {
    'Asia':      '04:30–09:30',
    'London':    '12:30–16:30',
    'London_NY': '18:00–20:30',
}

for name, (eq, trs) in session_results.items():
    if not trs:
        print(f"  {name:<14} {utc_labels[name]:<18} {dubai_labels[name]:<18} {'0':>5}  {'—':>5}  {'—':>8}  {'—':>7}  {'—':>7}  {'—':>8}  {'—':>7}")
        continue
    t = pd.DataFrame(trs)
    n     = len(t)
    roi   = (eq - ACCOUNT) / ACCOUNT * 100
    wr    = t['pnl'].gt(0).mean() * 100
    _,pv  = stats.ttest_1samp(t['pnl'], 0)
    wks   = max((pd.to_datetime(t['date']).max() - pd.to_datetime(t['date']).min()).days / 7, 1)
    tpw   = n / wks
    sharpe= t['pnl'].mean() / t['pnl'].std() * np.sqrt(252) if t['pnl'].std() > 0 else 0
    eq_c  = ACCOUNT + t['pnl'].cumsum()
    mdd   = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
    sig   = " ✓ SIGNIFICANT" if pv < 0.05 else (" ~ borderline" if pv < 0.10 else "")
    print(f"  {name:<14} {utc_labels[name]:<18} {dubai_labels[name]:<18} {n:>5} {tpw:>4.1f} {roi:>+7.1f}% {wr:>6.1f}% {sharpe:>7.2f} {pv:>8.4f}{sig}")

# ── DETAILED BREAKDOWN PER SESSION ────────────────────────────────────────────
for name, (eq, trs) in session_results.items():
    if not trs: continue
    t = pd.DataFrame(trs)
    t['date'] = pd.to_datetime(t['date'])
    t['win']  = t['pnl'] > 0
    t['month'] = t['date'].dt.to_period('M')
    n = len(t)
    _, pv = stats.ttest_1samp(t['pnl'], 0)
    roi = (eq - ACCOUNT) / ACCOUNT * 100
    wks = max((t['date'].max() - t['date'].min()).days / 7, 1)
    sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252) if t['pnl'].std() > 0 else 0
    eq_c = ACCOUNT + t['pnl'].cumsum()
    mdd = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()

    print(f"\n{'='*68}")
    print(f"  {name.upper().replace('_',' ')} SESSION  ({utc_labels[name]} UTC / {dubai_labels[name]} Dubai)")
    print(f"{'='*68}")
    print(f"""
  $10,000  →  ${eq:,.0f}  ({roi:+.1f}% ROI)
  Sharpe   : {sharpe:.2f}    Max drawdown: {mdd:.1f}%
  Trades   : {n}  ({n/wks:.1f}/week)
  Win rate : {t['win'].mean()*100:.1f}%   Avg P&L: ${t['pnl'].mean():+,.0f}
  p-value  : {pv:.4f}  {'✓ SIGNIFICANT' if pv<0.05 else '~ borderline' if pv<0.10 else '— not significant'}""")

    print(f"\n  Direction breakdown:")
    for d, g in t.groupby('dir'):
        print(f"    {d:<6}  {len(g):>4} trades | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f} | total ${g['pnl'].sum():+,.0f}")

    print(f"\n  Exit reasons:")
    for r, g in t.groupby('reason'):
        print(f"    {r:<6}  {len(g):>4} | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

    print(f"\n  Monthly P&L:")
    run_eq = ACCOUNT; pos = 0
    for mo, g in t.groupby('month'):
        run_eq += g['pnl'].sum()
        flag = "✓" if g['pnl'].sum() > 0 else "✗"
        if g['pnl'].sum() > 0: pos += 1
        print(f"    {str(mo)}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  ${g['pnl'].sum():>+8,.0f}  ${run_eq:>9,.0f} {flag}")
    print(f"\n  Positive months: {pos}/{t['month'].nunique()}")

    t.to_csv(f"gold_{name.lower()}_trades.csv", index=False)
    print(f"  Saved: gold_{name.lower()}_trades.csv")

print(f"\n{'='*68}")
print("  VERDICT")
print(f"{'='*68}")
for name, (eq, trs) in session_results.items():
    if not trs:
        print(f"  {name:<14} ❌ No trades generated")
        continue
    t = pd.DataFrame(trs)
    _, pv = stats.ttest_1samp(t['pnl'], 0)
    roi = (eq - ACCOUNT) / ACCOUNT * 100
    if pv < 0.05 and roi > 0:
        verdict = "✅ TRADEABLE EDGE"
    elif pv < 0.10 and roi > 0:
        verdict = "🟡 POSSIBLE EDGE — needs more data"
    elif roi < 0:
        verdict = "❌ LOSING SESSION"
    else:
        verdict = "⚪ NO EDGE (p not significant)"
    print(f"  {name:<14}  p={pv:.4f}  ROI={roi:+.1f}%  {verdict}")
print("="*68)
