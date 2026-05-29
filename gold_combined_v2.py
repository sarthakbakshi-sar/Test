"""
GOLD SYSTEM V2 — MULTI-TIMEFRAME BACKTEST
==========================================
Fixes vs V1:
  1. SHORTS FIXED  — only short when gold below 4H EMA50 (trend gate)
  2. MORE TRADES   — up to 3 entries per session, VWAP zone touch (not just cross)
  3. MACRO GATE    — skip near-neutral days (|score| < 2)
  4. 4H CONTEXT    — uses resampled 4H for trend direction on all versions

TWO SYSTEMS TESTED:
  A) 4H Chart  — entry at session open bar, 4H ATR sizing, ~1-2/week
  B) 1H Chart  — entry on VWAP zone touches, 1H ATR sizing, ~3-5/week
     (1H is the closest yfinance proxy for 15m/30m intraday behaviour;
      15m section uses yfinance 60d if available)

$10,000 portfolio | 3% risk per trade
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

ACCOUNT    = 10_000.0
RISK_PCT   = 0.03
SL_MULT    = 0.75
TP1_MULT   = 1.5
TP2_MULT   = 2.5
TP1_FRAC   = 0.60
TP2_FRAC   = 0.40
TIME_STOP  = 3
MIN_SCORE  = 2        # skip |score| < 2
MAX_TRADES = 3        # max per session
MIN_BARS_BETWEEN = 2  # bars between re-entries

SESS_OPEN  = (13, 30)
SESS_CLOSE = (17,  0)

print("="*68)
print("  GOLD SYSTEM V2 — MULTI-TIMEFRAME BACKTEST")
print("  4H trend filter | VWAP zone entries | shorts fixed")
print("="*68)

# ── DATA ──────────────────────────────────────────────────────────────────────
print("\nDownloading data...")

gold_1h = yf.download("GC=F", period="730d", interval="1h", progress=False)
gold_1h.columns = [c[0].lower() for c in gold_1h.columns]
gold_1h.index   = pd.to_datetime(gold_1h.index).tz_localize(None)
gold_1h.dropna(inplace=True)
print(f"  1h: {len(gold_1h)} bars | {gold_1h.index[0].date()} → {gold_1h.index[-1].date()}")

try:
    gold_15m = yf.download("GC=F", period="60d", interval="15m", progress=False)
    gold_15m.columns = [c[0].lower() for c in gold_15m.columns]
    gold_15m.index   = pd.to_datetime(gold_15m.index).tz_localize(None)
    gold_15m.dropna(inplace=True)
    print(f"  15m: {len(gold_15m)} bars | {gold_15m.index[0].date()} → {gold_15m.index[-1].date()}")
    HAS_15M = True
except:
    HAS_15M = False
    print("  15m: unavailable")

def dl(t, n):
    d = yf.download(t, period="3y", interval="1d", progress=False)
    d.columns = [c[0].lower() for c in d.columns]
    d.index = pd.to_datetime(d.index).tz_localize(None)
    print(f"  {n}: {len(d)} days")
    return d['close']

gold_d = dl("GC=F", "Gold daily"); dxy_d = dl("DX-Y.NYB", "DXY")
tnx_d  = dl("^TNX", "10Y yield");  gdx_d = dl("GDX", "GDX")

# ── MACRO SCORES ──────────────────────────────────────────────────────────────
print("\nBuilding macro scores...")
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
raw           = (dm['s_trend']+dm['s_yield']+dm['s_dxy']+dm['s_mom']+dm['s_gdx']).shift(1)
dm['score']   = (raw / 5 * 10).round(1)
dm.dropna(inplace=True)
macro_lkp = {d.date(): float(r['score']) for d, r in dm.iterrows()}

# ── 4H CONTEXT (resampled from 1H) ────────────────────────────────────────────
print("Building 4H context...")
g4 = gold_1h.resample('4h', label='left', closed='left').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
tr4 = pd.concat([g4['high']-g4['low'],
                 (g4['high']-g4['close'].shift()).abs(),
                 (g4['low']-g4['close'].shift()).abs()],axis=1).max(axis=1)
g4['atr'] = tr4.ewm(span=14, adjust=False).mean()
# Build lookup: for each datetime, get 4H bar just before it
def get_4h_context(ts):
    past = g4[g4.index <= ts]
    if past.empty: return None
    return past.iloc[-1]

# ── INDICATOR FUNCTION ────────────────────────────────────────────────────────
def add_indicators(df):
    df = df.copy()
    df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    tr = pd.concat([df['high']-df['low'],
                    (df['high']-df['close'].shift()).abs(),
                    (df['low']-df['close'].shift()).abs()],axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()
    return df

gold_1h  = add_indicators(gold_1h)
if HAS_15M:
    gold_15m = add_indicators(gold_15m)

# ── CORE BACKTEST ENGINE ───────────────────────────────────────────────────────
def run_backtest(intraday_df, label, atr_source='self'):
    """
    atr_source: 'self' = use intraday ATR, '4h' = use 4H ATR for sizing
    """
    equity = ACCOUNT
    trades = []

    session_dates = sorted(set(intraday_df.index.date))

    for date in session_dates:
        score = macro_lkp.get(date)
        if score is None: continue
        if abs(score) < MIN_SCORE: continue

        macro_dir = 1 if score > 0 else -1

        ts = pd.Timestamp(date)
        o_ts = ts + pd.Timedelta(hours=SESS_OPEN[0],  minutes=SESS_OPEN[1])
        c_ts = ts + pd.Timedelta(hours=SESS_CLOSE[0], minutes=SESS_CLOSE[1])
        sbars = intraday_df.loc[(intraday_df.index >= o_ts) & (intraday_df.index <= c_ts)].copy()
        if len(sbars) < 3: continue

        # Session VWAP
        tp  = (sbars['high'] + sbars['low'] + sbars['close']) / 3
        vol = sbars['volume'].replace(0, 1)
        sbars = sbars.copy()
        sbars['vwap']     = (tp * vol).cumsum() / vol.cumsum()
        sbars['vwap_std'] = tp.expanding().std().bfill().fillna(1)

        # 4H context at session open
        ctx4h = get_4h_context(o_ts)
        if ctx4h is None: continue

        # SHORT gate: only short when gold below 4H EMA50
        if macro_dir == -1 and sbars.iloc[0]['close'] > ctx4h['ema50']:
            continue  # don't short above 4H EMA50

        # Get ATR for sizing
        if atr_source == '4h':
            atr_size = float(ctx4h['atr']) if ctx4h['atr'] > 0 else float(sbars['atr'].mean())
        else:
            atr_size = None  # will use per-bar ATR

        trades_today = 0
        last_entry_bar = -99
        in_trade = False
        entry_px = sl = tp1_px = tp2_px = sz1 = sz2 = pnl1 = 0
        tp1_hit = False
        bars_held = 0
        trade_dir = 0

        for i in range(1, len(sbars)):
            b    = sbars.iloc[i]
            prev = sbars.iloc[i - 1]
            hi, lo, cls = b['high'], b['low'], b['close']
            vwap = b['vwap']
            std  = max(b['vwap_std'], 0.01)

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
                    pnl = ((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem + pnl1
                    trades.append(dict(date=date, dir='LONG' if trade_dir==1 else 'SHORT',
                        entry=entry_px, exit=cls, pnl=pnl, reason='TIME', score=score, bars=bars_held))
                    equity += pnl; in_trade = False

            else:
                if trades_today >= MAX_TRADES: continue
                if i - last_entry_bar < MIN_BARS_BETWEEN: continue

                # 4H EMA trend must agree with macro
                ctx_now = get_4h_context(b.name)
                if ctx_now is None: continue
                h4_bull = ctx_now['ema9'] > ctx_now['ema21']
                h4_bear = ctx_now['ema9'] < ctx_now['ema21']
                h4_ok   = (macro_dir == 1 and h4_bull) or (macro_dir == -1 and h4_bear)
                if not h4_ok: continue

                # Intraday EMA direction
                ema_ok = (macro_dir == 1 and b['ema9'] > b['ema21']) or \
                         (macro_dir == -1 and b['ema9'] < b['ema21'])
                if not ema_ok: continue

                # VWAP zone touch (price within 1σ of VWAP = pullback zone)
                dist = (cls - vwap) / std
                vwap_touch = (macro_dir == 1 and -1.0 <= dist <= 0.3) or \
                             (macro_dir == -1 and -0.3 <= dist <= 1.0)
                if not vwap_touch: continue

                # Enter
                atr_v = atr_size if atr_source == '4h' else float(b['atr'])
                if atr_v <= 0: continue
                risk_usd = equity * RISK_PCT
                tot_sz   = risk_usd / (SL_MULT * atr_v)
                sz1 = tot_sz * TP1_FRAC
                sz2 = tot_sz * TP2_FRAC

                entry_px = cls
                if macro_dir == 1:
                    sl     = entry_px - SL_MULT  * atr_v
                    tp1_px = entry_px + TP1_MULT * atr_v
                    tp2_px = entry_px + TP2_MULT * atr_v
                else:
                    sl     = entry_px + SL_MULT  * atr_v
                    tp1_px = entry_px - TP1_MULT * atr_v
                    tp2_px = entry_px - TP2_MULT * atr_v

                in_trade = True; trade_dir = macro_dir
                bars_held = 0; tp1_hit = False; pnl1 = 0
                trades_today += 1; last_entry_bar = i

        if in_trade:
            rem = sz2 if tp1_hit else (sz1 + sz2)
            cls = sbars.iloc[-1]['close']
            pnl = ((cls-entry_px) if trade_dir==1 else (entry_px-cls))*rem + pnl1
            trades.append(dict(date=date, dir='LONG' if trade_dir==1 else 'SHORT',
                entry=entry_px, exit=cls, pnl=pnl, reason='SESS', score=score, bars=bars_held))
            equity += pnl

    return equity, trades

# ── RUN SYSTEMS ───────────────────────────────────────────────────────────────
def print_results(label, equity, trades, start_date=None):
    if not trades:
        print(f"\n  {label}: No trades generated"); return
    t = pd.DataFrame(trades)
    t['date']  = pd.to_datetime(t['date'])
    t['win']   = t['pnl'] > 0
    t['month'] = t['date'].dt.to_period('M')

    n      = len(t)
    wr     = t['win'].mean() * 100
    roi    = (equity - ACCOUNT) / ACCOUNT * 100
    days   = (t['date'].max() - t['date'].min()).days
    wks    = max(days / 7, 1)
    tpw    = n / wks
    avg_w  = t.loc[t['win'],'pnl'].mean()
    avg_l  = t.loc[~t['win'],'pnl'].mean()
    rr     = abs(avg_w / avg_l) if avg_l != 0 else 0
    _, pv  = stats.ttest_1samp(t['pnl'], 0)
    eq_c   = ACCOUNT + t['pnl'].cumsum()
    mdd    = ((eq_c - eq_c.cummax()) / eq_c.cummax() * 100).min()
    sharpe = t['pnl'].mean() / t['pnl'].std() * np.sqrt(252) if t['pnl'].std() > 0 else 0

    print(f"\n{'='*68}")
    print(f"  {label}")
    print(f"  {t['date'].min().date()} → {t['date'].max().date()}")
    print(f"{'='*68}")
    print(f"""
  $10,000  →  ${equity:,.0f}  ({roi:+.1f}% ROI / ${equity-ACCOUNT:+,.0f})
  Sharpe   : {sharpe:.2f}   Max drawdown: {mdd:.1f}%

  Trades   : {n}  ({tpw:.1f}/week)
  Win rate : {wr:.1f}%
  Avg win  : ${avg_w:+,.0f}   Avg loss: ${avg_l:+,.0f}   RR: {rr:.2f}x
  p-value  : {pv:.4f}  {'✓ SIGNIFICANT' if pv<0.05 else '— not significant'}""")

    print(f"\n  Direction:")
    for d, g in t.groupby('dir'):
        print(f"    {d:<6} {len(g):>4} trades | WR {g['win'].mean()*100:.0f}% | "
              f"avg ${g['pnl'].mean():+,.0f} | total ${g['pnl'].sum():+,.0f}")

    print(f"\n  Exit reasons:")
    for r, g in t.groupby('reason'):
        print(f"    {r:<6} {len(g):>4} | WR {g['win'].mean()*100:.0f}% | avg ${g['pnl'].mean():+,.0f}")

    print(f"\n  Monthly  (positive: {sum(1 for _,g in t.groupby('month') if g['pnl'].sum()>0)}"
          f"/{t['month'].nunique()}):")
    run = ACCOUNT
    for mo, g in t.groupby('month'):
        run += g['pnl'].sum()
        flag = "✓" if g['pnl'].sum() > 0 else "✗"
        print(f"    {str(mo)}  {len(g):>3}tr  WR {g['win'].mean()*100:.0f}%  "
              f"${g['pnl'].sum():>+7,.0f}  ${run:>8,.0f} {flag}")

    if pv < 0.05 and roi > 0:
        verdict = "✓ PROFITABLE & SIGNIFICANT"
    elif roi > 0:
        verdict = f"~ Profitable, p={pv:.3f} needs more data"
    else:
        verdict = f"✗ Not profitable"
    print(f"\n  VERDICT: {verdict}")

    return t

print("\n" + "="*68)
print("  SYSTEM A — 1H CHART (730-day backtest)")
print("  Entry: VWAP zone touch + EMA9>21 + 4H trend gate")
print("="*68)
eq_a, tr_a = run_backtest(gold_1h, "1H", atr_source='self')
t_a = print_results("SYSTEM A — 1H ENTRIES", eq_a, tr_a)

if HAS_15M:
    print("\n" + "="*68)
    print("  SYSTEM B — 15M CHART (60-day backtest)")
    print("  Same rules, 15m bars = more signals per session")
    print("="*68)
    eq_b, tr_b = run_backtest(gold_15m, "15M", atr_source='self')
    t_b = print_results("SYSTEM B — 15M ENTRIES", eq_b, tr_b)
else:
    print("\n  SYSTEM B (15m): data unavailable")

# Summary comparison
print(f"\n{'='*68}")
print(f"  COMPARISON SUMMARY")
print(f"{'='*68}")
if tr_a:
    t_a_df = pd.DataFrame(tr_a)
    wks_a  = max((t_a_df['date'].max() - t_a_df['date'].min()).days / 7, 1)
    _, pv_a = stats.ttest_1samp(t_a_df['pnl'], 0)
    roi_a  = (eq_a - ACCOUNT) / ACCOUNT * 100
    print(f"""
  1H System (730 days):
    ROI: {roi_a:+.1f}%  |  {len(tr_a)/wks_a:.1f} trades/week  |  p={pv_a:.4f}
    Longs: {sum(1 for x in tr_a if x['dir']=='LONG')} trades
    Shorts: {sum(1 for x in tr_a if x['dir']=='SHORT')} trades (filtered to 4H bear trend only)
""")
if HAS_15M and tr_b:
    t_b_df = pd.DataFrame(tr_b)
    wks_b  = max((t_b_df['date'].max() - t_b_df['date'].min()).days / 7, 1)
    _, pv_b = stats.ttest_1samp(t_b_df['pnl'], 0)
    roi_b  = (eq_b - ACCOUNT) / ACCOUNT * 100
    print(f"  15m System (60 days):")
    print(f"    ROI: {roi_b:+.1f}%  |  {len(tr_b)/wks_b:.1f} trades/week  |  p={pv_b:.4f}")
    print(f"    Longs: {sum(1 for x in tr_b if x['dir']=='LONG')} | Shorts: {sum(1 for x in tr_b if x['dir']=='SHORT')}")
print("="*68)

if tr_a:
    pd.DataFrame(tr_a).to_csv("gold_combined_v2_trades.csv", index=False)
    print("Saved: gold_combined_v2_trades.csv")
