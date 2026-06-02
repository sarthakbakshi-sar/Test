"""
SILVER SIGNAL — BACKTEST v1
============================
XAG/USD (SI=F) VWAP Pullback — adapted from gold proven model.

Tests silver-specific macro components before adding ANY to live signal:
  1. Silver vs EMA50             (trend gate)
  2. Gold/Silver ratio direction  (ratio falling = silver cheap = bullish)
  3. Copper (HG=F) 5d momentum   (industrial demand proxy)
  4. DXY direction               (USD inverse)
  5. 10Y yield direction         (real rates)
  6. SIL miners vs silver        (like GDX/gold lead signal)
  7. Silver 5d momentum          (momentum)

Extra gates tested:
  - VIX threshold: 25 vs 30 (silver more volatile)
  - Month skips: all 12 tested individually
  - Gold alignment: only trade when gold trending same direction
  - Copper EMA gate: only trade when copper above/below 20d EMA

Session: 13:30–17:00 UTC (COMEX NY open, same as gold)
Entry:   14:00–16:30 UTC
Risk:    SL=0.75×ATR | TP1=1.5×ATR (60%) | TP2=2.5×ATR (40%)
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

print("="*68)
print("  SILVER BACKTEST v1  —  XAG/USD VWAP Pullback")
print("="*68)

# ── DATA DOWNLOAD ─────────────────────────────────────────────────────────────
print("\nDownloading daily data...")

def dl(ticker, name):
    try:
        d = yf.download(ticker, period="3y", interval="1d", progress=False, auto_adjust=True)
        if hasattr(d.columns, 'levels'):
            d.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in d.columns]
        else:
            d.columns = [c.lower() for c in d.columns]
        d.index = pd.to_datetime(d.index).tz_localize(None)
        print(f"  {name:30s}: {len(d)} days  ({d.index[0].date()} → {d.index[-1].date()})")
        return d['close']
    except Exception as e:
        print(f"  {name:30s}: FAILED — {e}")
        return None

silver_d = dl("SI=F",      "Silver futures (SI=F)")
gold_d   = dl("GC=F",      "Gold futures (GC=F)")
copper_d = dl("HG=F",      "Copper futures (HG=F)")
dxy_d    = dl("DX-Y.NYB",  "DXY")
tnx_d    = dl("^TNX",      "10Y yield")
sil_d    = dl("SIL",       "Silver miners ETF (SIL)")
vix_d    = dl("^VIX",      "VIX")

print("\nDownloading hourly silver bars (2yr, for session simulation)...")
silver_h = yf.download("SI=F", period="2y", interval="1h", progress=False, auto_adjust=True)
if hasattr(silver_h.columns, 'levels'):
    silver_h.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in silver_h.columns]
else:
    silver_h.columns = [c.lower() for c in silver_h.columns]
silver_h.index = pd.to_datetime(silver_h.index).tz_localize(None)
print(f"  Silver hourly: {len(silver_h)} bars")

# ── BUILD MACRO COMPONENTS ────────────────────────────────────────────────────
print("\nBuilding macro components...")

df = pd.DataFrame({'silver': silver_d})
for name, ser in [('gold', gold_d), ('copper', copper_d), ('dxy', dxy_d),
                  ('tnx', tnx_d), ('sil', sil_d), ('vix', vix_d)]:
    if ser is not None:
        df[name] = ser.reindex(df.index, method='ffill')

df.dropna(subset=['silver'], inplace=True)

# 1. Silver trend vs EMA50
df['silver_ema50'] = df['silver'].ewm(span=50, adjust=False).mean()
df['s_trend']      = np.where(df['silver'] > df['silver_ema50'], 1, -1)

# 2. Gold/Silver ratio direction (5d change)
if 'gold' in df.columns:
    df['gs_ratio']     = df['gold'] / df['silver']
    df['gs_ratio_chg'] = df['gs_ratio'].pct_change(5)
    # Ratio falling = silver outperforming gold = BULLISH for silver
    df['s_gs_ratio']   = np.where(df['gs_ratio_chg'] < 0, 1, -1)
else:
    df['s_gs_ratio'] = 0

# 3. Copper 5d momentum (industrial demand)
if 'copper' in df.columns:
    df['copper_mom5'] = df['copper'].pct_change(5)
    df['s_copper']    = np.where(df['copper_mom5'] > 0, 1, -1)
else:
    df['s_copper'] = 0

# 4. DXY direction (1d change)
if 'dxy' in df.columns:
    df['s_dxy'] = np.where(df['dxy'].pct_change() < 0, 1, -1)
else:
    df['s_dxy'] = 0

# 5. 10Y yield direction (1d change)
if 'tnx' in df.columns:
    df['s_yield'] = np.where(df['tnx'].diff() < 0, 1, -1)
else:
    df['s_yield'] = 0

# 6. SIL miners vs silver (1d relative return — lead signal)
if 'sil' in df.columns:
    df['sil_ret']    = df['sil'].pct_change()
    df['silver_ret'] = df['silver'].pct_change()
    df['s_sil']      = np.where(df['sil_ret'] > df['silver_ret'], 1, -1)
else:
    df['s_sil'] = 0

# 7. Silver 5d momentum
df['silver_mom5'] = df['silver'].pct_change(5)
df['s_mom']       = np.where(df['silver_mom5'] > 0, 1, -1)

# Derived: Gold alignment (gold trending same direction as silver signal)
if 'gold' in df.columns:
    df['gold_ema50']  = df['gold'].ewm(span=50, adjust=False).mean()
    df['gold_trend']  = np.where(df['gold'] > df['gold_ema50'], 1, -1)
else:
    df['gold_trend'] = 0

# Derived: Copper EMA20 gate
if 'copper' in df.columns:
    df['copper_ema20'] = df['copper'].ewm(span=20, adjust=False).mean()
    df['copper_above_ema20'] = df['copper'] > df['copper_ema20']

df.dropna(subset=['silver_mom5'], inplace=True)
print(f"  {len(df)} scoring days | {df.index[0].date()} → {df.index[-1].date()}")

# ── SESSION RETURNS (hourly) ───────────────────────────────────────────────────
print("\nMatching session returns (14:00–17:00 UTC)...")

COMP_COLS = ['s_trend','s_gs_ratio','s_copper','s_dxy','s_yield','s_sil','s_mom']

rows = []
for date, row in df.iterrows():
    d = date.date()
    # Entry: 14:00 UTC open; Exit: 17:00 UTC close (approximated by 16:45-17:00 bar)
    entry_ts = pd.Timestamp(d) + pd.Timedelta(hours=14)
    exit_ts  = pd.Timestamp(d) + pd.Timedelta(hours=17)

    entry_bars = silver_h.loc[(silver_h.index >= entry_ts) &
                              (silver_h.index < entry_ts + pd.Timedelta(hours=1))]
    exit_bars  = silver_h.loc[(silver_h.index >= exit_ts - pd.Timedelta(hours=1)) &
                              (silver_h.index <= exit_ts)]

    if entry_bars.empty or exit_bars.empty: continue

    entry_px = float(entry_bars['open'].iloc[0])
    exit_px  = float(exit_bars['close'].iloc[-1])
    if entry_px <= 0: continue

    ret = (exit_px / entry_px - 1) * 100

    r = {'date': date, 'ret': ret, 'entry_px': entry_px, 'exit_px': exit_px,
         'month': date.month, 'vix': row.get('vix', np.nan)}
    for c in COMP_COLS:
        r[c] = row[c]
    r['gold_trend'] = row['gold_trend']
    if 'copper_above_ema20' in df.columns:
        r['copper_ema_gate'] = row['copper_above_ema20']
    rows.append(r)

sess = pd.DataFrame(rows).set_index('date')
print(f"  Matched session days: {len(sess)}")
print(f"  Period: {sess.index[0].date()} → {sess.index[-1].date()}")

# ── ATR SIMULATION (SL / TP) ──────────────────────────────────────────────────
print("\nComputing daily ATR for SL/TP simulation...")
silver_daily = yf.download("SI=F", period="2y", interval="1d", progress=False, auto_adjust=True)
if hasattr(silver_daily.columns, 'levels'):
    silver_daily.columns = [c[0].lower() if isinstance(c,tuple) else c.lower() for c in silver_daily.columns]
else:
    silver_daily.columns = [c.lower() for c in silver_daily.columns]
silver_daily.index = pd.to_datetime(silver_daily.index).tz_localize(None)

# True range and ATR14
silver_daily['tr'] = np.maximum(
    silver_daily['high'] - silver_daily['low'],
    np.maximum(
        abs(silver_daily['high'] - silver_daily['close'].shift(1)),
        abs(silver_daily['low']  - silver_daily['close'].shift(1))
    )
)
silver_daily['atr14'] = silver_daily['tr'].ewm(span=14, adjust=False).mean()

sess['atr'] = silver_daily['atr14'].reindex(sess.index, method='ffill')

def simulate_trades(sub, direction_col=None):
    """
    For each row in sub, simulate entry at session open with SL/TP.
    Uses hourly bars within the session to determine outcome.
    direction: +1=LONG, -1=SHORT derived from score.
    Returns list of trade P&L percentages.
    """
    results = []
    for date, row in sub.iterrows():
        atr = row.get('atr', np.nan)
        if np.isnan(atr) or atr <= 0: continue
        entry = row['entry_px']
        if entry <= 0: continue

        # Direction from score
        if direction_col:
            score = row[direction_col]
        else:
            score = row.get('score', 0)
        if score == 0: continue
        direction = 1 if score > 0 else -1

        sl_dist  = 0.75 * atr
        tp1_dist = 1.50 * atr
        tp2_dist = 2.50 * atr

        if direction == 1:   # LONG
            sl  = entry - sl_dist
            tp1 = entry + tp1_dist
            tp2 = entry + tp2_dist
        else:                # SHORT
            sl  = entry + sl_dist
            tp1 = entry - tp1_dist
            tp2 = entry - tp2_dist

        # Walk through hourly bars 14:00–16:30
        d = date.date()
        session_bars = silver_h.loc[
            (silver_h.index >= pd.Timestamp(d) + pd.Timedelta(hours=14)) &
            (silver_h.index <= pd.Timestamp(d) + pd.Timedelta(hours=16, minutes=30))
        ]

        pnl_pct = None
        tp1_hit = False

        for _, bar in session_bars.iterrows():
            h, l = bar['high'], bar['low']
            if direction == 1:
                if l <= sl:                        # stopped out
                    if not tp1_hit:
                        pnl_pct = -sl_dist / entry * 100
                    else:
                        pnl_pct = 0  # SL on TP2 portion (breakeven after TP1)
                    break
                if not tp1_hit and h >= tp1:       # TP1 hit
                    tp1_hit = True
                if tp1_hit and h >= tp2:           # TP2 hit
                    pnl_pct = (0.60 * tp1_dist + 0.40 * tp2_dist) / entry * 100
                    break
            else:  # SHORT
                if h >= sl:
                    if not tp1_hit:
                        pnl_pct = -sl_dist / entry * 100
                    else:
                        pnl_pct = 0
                    break
                if not tp1_hit and l <= tp1:
                    tp1_hit = True
                if tp1_hit and l <= tp2:
                    pnl_pct = (0.60 * tp1_dist + 0.40 * tp2_dist) / entry * 100
                    break

        # Time stop: session closed without hitting SL or TP2
        if pnl_pct is None:
            exit_bar = silver_h.loc[
                (silver_h.index >= pd.Timestamp(d) + pd.Timedelta(hours=16, minutes=45)) &
                (silver_h.index <= pd.Timestamp(d) + pd.Timedelta(hours=17, minutes=15))
            ]
            if exit_bar.empty: continue
            exit_px = float(exit_bar['close'].iloc[-1])
            if tp1_hit:
                # TP1 already closed 60% at tp1; remaining 40% exits at close
                pnl_pct = (0.60 * tp1_dist + 0.40 * abs(exit_px - entry) *
                           direction) / entry * 100
            else:
                pnl_pct = (exit_px - entry) * direction / entry * 100

        results.append(pnl_pct)

    return results

def sharpe(returns):
    arr = np.array(returns)
    if len(arr) < 5 or arr.std() == 0: return 0
    return arr.mean() / arr.std() * np.sqrt(252)

def wr(returns):
    arr = np.array(returns)
    return (arr > 0).mean() * 100 if len(arr) else 0

def max_dd(returns):
    arr = np.array(returns)
    cumulative = np.cumsum(arr)
    peak = np.maximum.accumulate(cumulative)
    dd = cumulative - peak
    return dd.min() if len(dd) else 0

# ── PHASE 1: COMPONENT ISOLATION TEST ────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 1 — EACH COMPONENT IN ISOLATION")
print("  (Does this signal predict session direction?)")
print("="*68)

component_results = {}
for col, label in [
    ('s_trend',    'Silver vs EMA50        '),
    ('s_gs_ratio', 'Gold/Silver ratio dir  '),
    ('s_copper',   'Copper 5d momentum     '),
    ('s_dxy',      'DXY direction          '),
    ('s_yield',    '10Y yield direction    '),
    ('s_sil',      'SIL miners vs silver   '),
    ('s_mom',      'Silver 5d momentum     '),
]:
    bull = sess[sess[col] ==  1]['ret']
    bear = sess[sess[col] == -1]['ret']
    if len(bull) < 5 or len(bear) < 5:
        print(f"\n  {label}: insufficient data"); continue

    # Direction-adjusted return (bull=ret, bear=-ret)
    aligned = pd.concat([bull, -bear])
    t, p = stats.ttest_1samp(aligned, 0)
    corr, pc = stats.pearsonr(sess[col].dropna(), sess['ret'].reindex(sess[col].dropna().index))

    bull_wr = (bull > 0).mean() * 100
    bear_wr = (bear < 0).mean() * 100

    sig = " ✓ SIGNIFICANT" if p < 0.05 else (" ~ marginal" if p < 0.10 else "")
    print(f"\n  {label}")
    print(f"    Bull (+1): {len(bull):3d} days | WR {bull_wr:5.1f}% | avg {bull.mean():+.3f}%")
    print(f"    Bear (-1): {len(bear):3d} days | WR {bear_wr:5.1f}% (as bear) | avg {(-bear).mean():+.3f}%")
    print(f"    p={p:.4f}  r={corr:+.4f}{sig}")

    component_results[col] = {'p': p, 'corr': corr, 'bull_wr': bull_wr, 'bear_wr': bear_wr}

# ── PHASE 2: BUILD COMBINED SCORES ───────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 2 — COMBINED SCORE COMBINATIONS")
print("="*68)

# Select components with p < 0.15 for combination testing
good_comps = [c for c, r in component_results.items() if r['p'] < 0.15]
print(f"\n  Components with p < 0.15: {good_comps}")

# Full 7-component score
sess['score_full'] = sess[COMP_COLS].sum(axis=1)

# Best-of-5 (drop weakest 2 if any)
sorted_comps = sorted(component_results.items(), key=lambda x: x[1]['p'])
top5 = [c for c, _ in sorted_comps[:5]]
sess['score_top5'] = sess[top5].sum(axis=1) if len(top5) >= 5 else sess['score_full']

# Core 3 (trend + DXY + yield, same as gold base)
sess['score_core3'] = sess['s_trend'] + sess['s_dxy'] + sess['s_yield']

print(f"\n  {'Score':>10} | {'Full 7':>8} | {'Top 5':>8} | {'Core 3':>8}")
print(f"  {'─'*46}")

for score_name, score_col in [('Full 7', 'score_full'), ('Top 5', 'score_top5'), ('Core 3', 'score_core3')]:
    scores = sess[score_col]
    print(f"\n  === {score_name} components ===")
    print(f"  {'Score':>7} {'Days':>6} {'Sess avg':>9} {'WR':>7}")
    for sc in sorted(scores.unique()):
        sub = sess[scores == sc]
        avg = sub['ret'].mean()
        w   = (sub['ret'] * np.sign(sc) > 0).mean() * 100 if sc != 0 else float('nan')
        flag = " ←" if abs(sc) >= len(top5) * 0.6 else ""
        print(f"  {sc:>+6.0f}  {len(sub):>6}  {avg:>+8.3f}%  {w:>5.1f}%{flag}")

    corr, p = stats.pearsonr(scores, sess['ret'])
    print(f"  r={corr:+.4f}  p={p:.4f}  {'✓ SIGNIFICANT' if p < 0.05 else '—'}")

# ── PHASE 3: GATE TESTING ────────────────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 3 — GATE TESTING")
print("="*68)

# Score threshold for filtering: |score| >= 3 (majority of 5) or >= 5 (majority of 7)
for thresh_label, thresh_fn in [
    ("score_full |≥5| (5/7 agree)", lambda s: s['score_full'].abs() >= 5),
    ("score_full |≥3| (any majority)", lambda s: s['score_full'].abs() >= 3),
]:
    active = sess[thresh_fn(sess)]
    if len(active) < 10:
        print(f"\n  {thresh_label}: only {len(active)} days"); continue

    bull = active[active['score_full'] > 0]['ret']
    bear = active[active['score_full'] < 0]['ret']
    aligned = pd.concat([bull, -bear])
    t, p = stats.ttest_1samp(aligned, 0)
    print(f"\n  Filter: {thresh_label}")
    print(f"    {len(active)} days  |  Bull WR {(bull>0).mean()*100:.1f}%  Bear WR {(bear<0).mean()*100:.1f}%  p={p:.4f}")

# VIX gate: 25 vs 30
print(f"\n  --- VIX THRESHOLD ---")
base = sess[sess['score_full'].abs() >= 3]
for vix_thresh in [25, 30, 35, None]:
    if vix_thresh:
        sub = base[base['vix'] < vix_thresh]
        label = f"VIX < {vix_thresh}"
    else:
        sub = base
        label = "No VIX gate"
    if len(sub) < 10: continue
    bull = sub[sub['score_full'] > 0]['ret']
    bear = sub[sub['score_full'] < 0]['ret']
    aligned = pd.concat([bull, -bear])
    t, p = stats.ttest_1samp(aligned, 0)
    avg = aligned.mean()
    w   = (aligned > 0).mean() * 100
    print(f"    {label:15s}: {len(sub):3d} days | WR {w:5.1f}% | avg {avg:+.3f}% | p={p:.4f}")

# Month analysis
print(f"\n  --- MONTH ANALYSIS (skip worst months?) ---")
month_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
base2 = sess[sess['score_full'].abs() >= 3].copy()
print(f"  {'Month':>6} {'Days':>6} {'WR':>7} {'Avg ret':>9} {'Sharpe':>8}")
month_sharpes = {}
for m in range(1, 13):
    sub = base2[base2['month'] == m]
    if len(sub) < 3: continue
    bull = sub[sub['score_full'] > 0]['ret']
    bear = sub[sub['score_full'] < 0]['ret']
    aligned = pd.concat([bull, -bear])
    w   = (aligned > 0).mean() * 100
    avg = aligned.mean()
    sh  = sharpe(aligned.values)
    print(f"  {month_names[m-1]:>6} {len(sub):>6}   {w:>5.1f}%  {avg:>+8.3f}%   {sh:>7.2f}")
    month_sharpes[m] = sh

bad_months = [m for m, s in month_sharpes.items() if s < 0]
print(f"\n  Months with negative Sharpe: {[month_names[m-1] for m in bad_months]}")

# Gold alignment gate
print(f"\n  --- GOLD ALIGNMENT GATE ---")
for gold_aligned in [True, False]:
    if gold_aligned:
        sub = base2[base2['score_full'] * base2['gold_trend'] > 0]
        label = "Gold aligned"
    else:
        sub = base2[base2['score_full'] * base2['gold_trend'] < 0]
        label = "Gold opposed"
    if len(sub) < 5: continue
    bull = sub[sub['score_full'] > 0]['ret']
    bear = sub[sub['score_full'] < 0]['ret']
    aligned = pd.concat([bull, -bear])
    w   = (aligned > 0).mean() * 100
    avg = aligned.mean()
    print(f"    {label:15s}: {len(sub):3d} days | WR {w:5.1f}% | avg {avg:+.3f}%")

# Copper EMA gate
if 'copper_ema_gate' in sess.columns:
    print(f"\n  --- COPPER EMA20 GATE ---")
    for above in [True, False]:
        sub = base2[base2['copper_ema_gate'] == above]
        label = "Copper > EMA20" if above else "Copper < EMA20"
        if len(sub) < 5: continue
        bull = sub[sub['score_full'] > 0]['ret']
        bear = sub[sub['score_full'] < 0]['ret']
        aligned = pd.concat([bull, -bear])
        w   = (aligned > 0).mean() * 100
        avg = aligned.mean()
        print(f"    {label:18s}: {len(sub):3d} days | WR {w:5.1f}% | avg {avg:+.3f}%")

# ── PHASE 4: FULL ATR SIMULATION ─────────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 4 — ATR-BASED SL/TP SIMULATION")
print("  (SL=0.75×ATR  TP1=1.5×ATR 60%  TP2=2.5×ATR 40%)")
print("="*68)

# Build optimal filter set
good_months = [m for m, s in month_sharpes.items() if s >= 0]
vix_best = 30  # will confirm from phase 3

def run_simulation(label, filter_fn, score_col='score_full', min_score=3):
    sub = sess.copy()
    sub['score'] = sub[score_col]
    sub = sub[sub['score'].abs() >= min_score]
    sub = filter_fn(sub)
    if len(sub) < 10:
        print(f"\n  {label}: only {len(sub)} trades — skipping"); return None

    trades = simulate_trades(sub, direction_col='score')
    if not trades:
        print(f"\n  {label}: no trades simulated"); return None

    arr = np.array(trades)
    sh  = sharpe(arr)
    w   = wr(arr)
    dd  = max_dd(arr)
    avg = arr.mean()
    n   = len(arr)
    wins = (arr > 0).sum(); losses = (arr < 0).sum()
    avg_win  = arr[arr > 0].mean() if wins else 0
    avg_loss = arr[arr < 0].mean() if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    print(f"\n  {label}")
    print(f"  {'─'*50}")
    print(f"    Trades : {n}  |  WR {w:.1f}%  |  Sharpe {sh:.2f}")
    print(f"    Avg P&L: {avg:+.3f}%  |  Max DD {dd:+.3f}%")
    print(f"    Avg Win: {avg_win:+.3f}%  |  Avg Loss: {avg_loss:+.3f}%  |  R:R {rr:.2f}")
    # Monthly Sharpe breakdown for this filter
    sub2 = sub.copy(); sub2['trade_pnl'] = np.nan
    # Approximate equity curve
    cumret = np.cumsum(arr)
    total_ret = cumret[-1] if len(cumret) else 0
    print(f"    Cum ret: {total_ret:+.2f}%")
    return {'n': n, 'sharpe': sh, 'wr': w, 'dd': dd, 'avg': avg, 'trades': arr}

# Baseline: no gates
r0 = run_simulation(
    "BASELINE — score≥3, no other gates",
    lambda s: s,
)

# + VIX gate
r1 = run_simulation(
    "+ VIX < 30",
    lambda s: s[s['vix'] < 30],
)

# + VIX + month skip
skip_set = set(bad_months)
r2 = run_simulation(
    f"+ VIX < 30 + skip bad months ({[month_names[m-1] for m in sorted(skip_set)]})",
    lambda s: s[(s['vix'] < 30) & (~s['month'].isin(skip_set))],
)

# + VIX + month skip + gold aligned
r3 = run_simulation(
    "+ VIX < 30 + skip bad months + gold aligned",
    lambda s: s[(s['vix'] < 30) & (~s['month'].isin(skip_set)) &
                (s['score_full'] * s['gold_trend'] > 0)],
)

# Strong signal only (≥5/7)
r4 = run_simulation(
    "STRONG signal only (≥5/7) + VIX < 30",
    lambda s: s[s['vix'] < 30],
    min_score=5
)

# ── PHASE 5: FINAL COMPARISON ────────────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 5 — FINAL COMPARISON & RECOMMENDATION")
print("="*68)

print(f"""
  {'Model':40s} {'Trades':>7} {'WR':>7} {'Sharpe':>8} {'Max DD':>9}""")
print(f"  {'─'*74}")
for label, r in [
    ("Baseline (score≥3)",                    r0),
    ("+ VIX<30",                              r1),
    ("+ VIX<30 + month skips",                r2),
    ("+ VIX<30 + months + gold aligned",      r3),
    ("Strong only (≥5/7) + VIX<30",           r4),
]:
    if r:
        print(f"  {label:40s} {r['n']:>7}  {r['wr']:>5.1f}%  {r['sharpe']:>7.2f}  {r['dd']:>+8.2f}%")
    else:
        print(f"  {label:40s} {'—':>7}  {'—':>5}  {'—':>7}  {'—':>8}")

print(f"""
  COMPONENT SIGNIFICANCE SUMMARY:
  {'─'*50}""")
comp_names = {
    's_trend':    'Silver vs EMA50',
    's_gs_ratio': 'Gold/Silver ratio dir',
    's_copper':   'Copper 5d momentum',
    's_dxy':      'DXY direction',
    's_yield':    '10Y yield direction',
    's_sil':      'SIL miners vs silver',
    's_mom':      'Silver 5d momentum',
}
for col, name in comp_names.items():
    if col in component_results:
        r = component_results[col]
        sig = "✓ KEEP" if r['p'] < 0.05 else ("~ TEST" if r['p'] < 0.10 else "✗ SKIP")
        print(f"  {name:28s} p={r['p']:.4f}  r={r['corr']:+.4f}  {sig}")

print("\n" + "="*68)
print("  VERDICT:")
results_list = [(r['sharpe'], label, r) for label, r in [
    ("Baseline", r0), ("VIX<30", r1), ("VIX+months", r2),
    ("VIX+months+gold", r3), ("Strong 5/7", r4)
] if r]
if results_list:
    best_sh, best_label, best_r = max(results_list, key=lambda x: x[0])
    print(f"  Best model: {best_label}")
    print(f"  Sharpe {best_sh:.2f}  |  WR {best_r['wr']:.1f}%  |  {best_r['n']} trades")
    if best_sh >= 1.5:
        print(f"\n  ✓ SILVER MODEL HAS EDGE — build live signal")
        print(f"  Sharpe {best_sh:.2f} >= 1.5 threshold")
    else:
        print(f"\n  ✗ SHARPE {best_sh:.2f} < 1.5 — do not build live signal yet")
        print(f"  Need more refinement or different approach")
print("="*68 + "\n")
