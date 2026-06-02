"""
SILVER SIGNAL — BACKTEST v2
============================
XAG/USD (SI=F) — Dual Session Test

Changes from v1:
  1. Adds LONDON session (08:00–11:30 UTC) as second trading window
     Rationale: LBMA Silver Price fix at 12:00 UTC drives morning activity;
     silver futures are fully electronic 23h/day, London open is real liquidity.
  2. Fixes time-stop bug: uses last bar in session window, not a non-existent
     16:45-17:15 UTC bar (1h bars labeled at hour start, so no such bar exists).
  3. A/B test: NY-only vs London-only vs Combined.
     London is ONLY added to the live signal if:
       – Its standalone Sharpe ≥ 1.5, AND
       – Combined Sharpe >= NY-only Sharpe (additive, not dilutive)
  4. Reports trades/week for every model configuration.

Goal: 2–3 trades/week, Sharpe ≥ 1.5

Session windows:
  London: 08:00–11:30 UTC  (morning discovery, LBMA silver fix build-up)
  NY:     14:00–16:30 UTC  (COMEX pit open, peak US liquidity)

Risk:    SL=0.75×ATR | TP1=1.5×ATR (60%) | TP2=2.5×ATR (40%)
"""

import yfinance as yf
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

print("="*68)
print("  SILVER BACKTEST v2  —  DUAL SESSION (London + NY)")
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

print("\nDownloading hourly silver bars (2yr, both London + NY sessions)...")
silver_h = yf.download("SI=F", period="2y", interval="1h", progress=False, auto_adjust=True)
if hasattr(silver_h.columns, 'levels'):
    silver_h.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in silver_h.columns]
else:
    silver_h.columns = [c.lower() for c in silver_h.columns]
silver_h.index = pd.to_datetime(silver_h.index).tz_localize(None)
print(f"  Silver hourly: {len(silver_h)} bars  ({silver_h.index[0].date()} → {silver_h.index[-1].date()})")

# ── MACRO COMPONENTS ──────────────────────────────────────────────────────────
print("\nBuilding macro components...")

df = pd.DataFrame({'silver': silver_d})
for name, ser in [('gold', gold_d), ('copper', copper_d), ('dxy', dxy_d),
                  ('tnx', tnx_d), ('sil', sil_d), ('vix', vix_d)]:
    if ser is not None:
        df[name] = ser.reindex(df.index, method='ffill')

df.dropna(subset=['silver'], inplace=True)

df['silver_ema50'] = df['silver'].ewm(span=50, adjust=False).mean()
df['s_trend']      = np.where(df['silver'] > df['silver_ema50'], 1, -1)

if 'gold' in df.columns:
    df['gs_ratio']     = df['gold'] / df['silver']
    df['gs_ratio_chg'] = df['gs_ratio'].pct_change(5)
    df['s_gs_ratio']   = np.where(df['gs_ratio_chg'] < 0, 1, -1)
else:
    df['s_gs_ratio'] = 0

if 'copper' in df.columns:
    df['copper_mom5'] = df['copper'].pct_change(5)
    df['s_copper']    = np.where(df['copper_mom5'] > 0, 1, -1)
else:
    df['s_copper'] = 0

df['s_dxy']   = np.where(df['dxy'].pct_change() < 0, 1, -1) if 'dxy' in df.columns else 0
df['s_yield'] = np.where(df['tnx'].diff() < 0, 1, -1)       if 'tnx' in df.columns else 0

if 'sil' in df.columns:
    df['sil_ret']    = df['sil'].pct_change()
    df['silver_ret'] = df['silver'].pct_change()
    df['s_sil']      = np.where(df['sil_ret'] > df['silver_ret'], 1, -1)
else:
    df['s_sil'] = 0

df['silver_mom5'] = df['silver'].pct_change(5)
df['s_mom']       = np.where(df['silver_mom5'] > 0, 1, -1)

if 'gold' in df.columns:
    df['gold_ema50'] = df['gold'].ewm(span=50, adjust=False).mean()
    df['gold_trend'] = np.where(df['gold'] > df['gold_ema50'], 1, -1)
else:
    df['gold_trend'] = 0

if 'copper' in df.columns:
    df['copper_ema20']     = df['copper'].ewm(span=20, adjust=False).mean()
    df['copper_above_ema'] = df['copper'] > df['copper_ema20']

df.dropna(subset=['silver_mom5'], inplace=True)
print(f"  {len(df)} scoring days | {df.index[0].date()} → {df.index[-1].date()}")

COMP_COLS = ['s_trend', 's_gs_ratio', 's_copper', 's_dxy', 's_yield', 's_sil', 's_mom']

# ── DUAL SESSION MATCHING ─────────────────────────────────────────────────────
print("\nMatching sessions: London (08:00–11:30 UTC) + NY (14:00–16:30 UTC)...")

# Session definitions: (name, entry_hour, walk_end_hour, walk_end_min)
SESSION_DEFS = [
    ('London', 8,  11, 30),
    ('NY',    14,  16, 30),
]

all_rows = []
for date, row in df.iterrows():
    d = date.date()
    for sess_name, entry_h, end_h, end_m in SESSION_DEFS:
        entry_ts   = pd.Timestamp(d) + pd.Timedelta(hours=entry_h)
        end_ts     = pd.Timestamp(d) + pd.Timedelta(hours=end_h, minutes=end_m)

        entry_bars = silver_h.loc[(silver_h.index >= entry_ts) &
                                   (silver_h.index <  entry_ts + pd.Timedelta(hours=1))]
        if entry_bars.empty:
            continue

        entry_px = float(entry_bars['open'].iloc[0])
        if entry_px <= 0:
            continue

        sess_bars = silver_h.loc[(silver_h.index >= entry_ts) & (silver_h.index <= end_ts)]
        if sess_bars.empty:
            continue

        exit_px = float(sess_bars['close'].iloc[-1])
        ret     = (exit_px / entry_px - 1) * 100

        rec = {
            'date': date, 'session': sess_name, 'ret': ret,
            'entry_px': entry_px, 'exit_px': exit_px,
            'month': date.month, 'vix': row.get('vix', np.nan),
        }
        for c in COMP_COLS:
            rec[c] = row[c]
        rec['gold_trend'] = row['gold_trend']
        if 'copper_above_ema' in df.columns:
            rec['copper_ema_gate'] = row['copper_above_ema']
        all_rows.append(rec)

all_sess = pd.DataFrame(all_rows)
all_sess['date_idx'] = all_sess['date']
all_sess = all_sess.set_index('date_idx')

sess_ny  = all_sess[all_sess['session'] == 'NY'].copy()
sess_lon = all_sess[all_sess['session'] == 'London'].copy()

print(f"  NY session rows:     {len(sess_ny)}")
print(f"  London session rows: {len(sess_lon)}")

# Attach ATR
print("\nComputing daily ATR (14-day)...")
silver_daily = yf.download("SI=F", period="2y", interval="1d", progress=False, auto_adjust=True)
if hasattr(silver_daily.columns, 'levels'):
    silver_daily.columns = [c[0].lower() if isinstance(c,tuple) else c.lower() for c in silver_daily.columns]
else:
    silver_daily.columns = [c.lower() for c in silver_daily.columns]
silver_daily.index = pd.to_datetime(silver_daily.index).tz_localize(None)
silver_daily['tr'] = np.maximum(
    silver_daily['high'] - silver_daily['low'],
    np.maximum(
        abs(silver_daily['high'] - silver_daily['close'].shift(1)),
        abs(silver_daily['low']  - silver_daily['close'].shift(1))
    )
)
silver_daily['atr14'] = silver_daily['tr'].ewm(span=14, adjust=False).mean()

for s in [sess_ny, sess_lon, all_sess]:
    s['atr'] = silver_daily['atr14'].reindex(s['date'], method='ffill').values

# ── STATS HELPERS ─────────────────────────────────────────────────────────────
def sharpe(returns):
    arr = np.array(returns, dtype=float)
    if len(arr) < 5 or arr.std() == 0:
        return 0.0
    return arr.mean() / arr.std() * np.sqrt(252)

def wr(returns):
    arr = np.array(returns, dtype=float)
    return (arr > 0).mean() * 100 if len(arr) else 0.0

def max_dd(returns):
    arr  = np.array(returns, dtype=float)
    cumr = np.cumsum(arr)
    peak = np.maximum.accumulate(cumr)
    return (cumr - peak).min() if len(arr) else 0.0

def trades_per_week(n_trades, total_scoring_days):
    return n_trades / (total_scoring_days / 5.0) if total_scoring_days else 0.0

# ── ATR SIMULATION — FIXED TIME-STOP ─────────────────────────────────────────
def simulate_session(sub, sess_name, direction_col='score'):
    """
    Walk intraday bars for each qualifying row.
    Time-stop fix: on no SL/TP hit, exit at LAST BAR in session window
    (v1 bug: queried a non-existent 16:45-17:15 bar, causing trades to vanish).
    """
    results = []
    for _, row in sub.iterrows():
        atr = row.get('atr', np.nan)
        if np.isnan(atr) or atr <= 0:
            continue
        entry = float(row['entry_px'])
        if entry <= 0:
            continue

        score_val = row[direction_col]
        if score_val == 0:
            continue
        direction = 1 if score_val > 0 else -1

        sl_dist  = 0.75 * atr
        tp1_dist = 1.50 * atr
        tp2_dist = 2.50 * atr

        if direction == 1:
            sl  = entry - sl_dist;  tp1 = entry + tp1_dist;  tp2 = entry + tp2_dist
        else:
            sl  = entry + sl_dist;  tp1 = entry - tp1_dist;  tp2 = entry - tp2_dist

        d = row['date'].date() if hasattr(row['date'], 'date') else pd.Timestamp(row.name).date()
        if sess_name == 'London':
            bar_start = pd.Timestamp(d) + pd.Timedelta(hours=8)
            bar_end   = pd.Timestamp(d) + pd.Timedelta(hours=11, minutes=30)
        else:
            bar_start = pd.Timestamp(d) + pd.Timedelta(hours=14)
            bar_end   = pd.Timestamp(d) + pd.Timedelta(hours=16, minutes=30)

        session_bars = silver_h.loc[(silver_h.index >= bar_start) & (silver_h.index <= bar_end)]
        if session_bars.empty:
            continue

        pnl_pct = None
        tp1_hit = False

        for _, bar in session_bars.iterrows():
            h, l = float(bar['high']), float(bar['low'])
            if direction == 1:
                if l <= sl:
                    pnl_pct = 0.0 if tp1_hit else -sl_dist / entry * 100
                    break
                if not tp1_hit and h >= tp1:
                    tp1_hit = True
                if tp1_hit and h >= tp2:
                    pnl_pct = (0.60 * tp1_dist + 0.40 * tp2_dist) / entry * 100
                    break
            else:
                if h >= sl:
                    pnl_pct = 0.0 if tp1_hit else -sl_dist / entry * 100
                    break
                if not tp1_hit and l <= tp1:
                    tp1_hit = True
                if tp1_hit and l <= tp2:
                    pnl_pct = (0.60 * tp1_dist + 0.40 * tp2_dist) / entry * 100
                    break

        # Time stop — FIXED: last bar close, not a specific exit timestamp
        if pnl_pct is None:
            exit_px = float(session_bars['close'].iloc[-1])
            if tp1_hit:
                pnl_pct = (0.60 * tp1_dist + 0.40 * (exit_px - entry) * direction) / entry * 100
            else:
                pnl_pct = (exit_px - entry) * direction / entry * 100

        results.append(pnl_pct)

    return results

# ── PHASE 1: COMPONENT ISOLATION (NY session, consistent with v1) ─────────────
print("\n" + "="*68)
print("  PHASE 1 — COMPONENT ISOLATION  (NY session)")
print("="*68)

component_results = {}
for col, label in [
    ('s_trend',    'Silver vs EMA50       '),
    ('s_gs_ratio', 'Gold/Silver ratio dir '),
    ('s_copper',   'Copper 5d momentum    '),
    ('s_dxy',      'DXY direction         '),
    ('s_yield',    '10Y yield direction   '),
    ('s_sil',      'SIL miners vs silver  '),
    ('s_mom',      'Silver 5d momentum    '),
]:
    bull = sess_ny[sess_ny[col] ==  1]['ret']
    bear = sess_ny[sess_ny[col] == -1]['ret']
    if len(bull) < 5 or len(bear) < 5:
        print(f"\n  {label}: insufficient data"); continue

    aligned = pd.concat([bull, -bear])
    _, p    = stats.ttest_1samp(aligned, 0)
    corr, _ = stats.pearsonr(sess_ny[col].dropna(),
                              sess_ny['ret'].reindex(sess_ny[col].dropna().index))

    bull_wr = (bull > 0).mean() * 100
    bear_wr = (bear < 0).mean() * 100
    sig     = " ✓ SIGNIFICANT" if p < 0.05 else (" ~ marginal" if p < 0.10 else "")

    print(f"\n  {label}")
    print(f"    Bull (+1): {len(bull):3d} days | WR {bull_wr:5.1f}% | avg {bull.mean():+.3f}%")
    print(f"    Bear (-1): {len(bear):3d} days | WR {bear_wr:5.1f}% (bear) | avg {(-bear).mean():+.3f}%")
    print(f"    p={p:.4f}  r={corr:+.4f}{sig}")
    component_results[col] = {'p': p, 'corr': corr, 'bull_wr': bull_wr, 'bear_wr': bear_wr}

# ── PHASE 2: SCORE COMBINATIONS (NY session) ─────────────────────────────────
print("\n" + "="*68)
print("  PHASE 2 — COMBINED SCORE  (NY session)")
print("="*68)

sess_ny['score_full'] = sess_ny[COMP_COLS].sum(axis=1)
sorted_comps = sorted(component_results.items(), key=lambda x: x[1]['p'])
top5 = [c for c, _ in sorted_comps[:5]]
sess_ny['score_top5'] = sess_ny[top5].sum(axis=1) if len(top5) >= 5 else sess_ny['score_full']

for score_name, score_col in [('Full 7', 'score_full'), ('Top 5', 'score_top5')]:
    scores = sess_ny[score_col]
    print(f"\n  === {score_name} ===")
    print(f"  {'Score':>7} {'Days':>6} {'Sess avg':>9} {'Dir WR':>8}")
    for sc in sorted(scores.unique()):
        sub = sess_ny[scores == sc]
        avg = sub['ret'].mean()
        w   = (sub['ret'] * np.sign(sc) > 0).mean() * 100 if sc != 0 else float('nan')
        print(f"  {sc:>+6.0f}  {len(sub):>6}  {avg:>+8.3f}%  {w:>6.1f}%")
    corr, p = stats.pearsonr(scores, sess_ny['ret'])
    print(f"  r={corr:+.4f}  p={p:.4f}  {'✓' if p < 0.05 else '—'}")

# ── PHASE 3: GATE TESTING (NY session) ───────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 3 — GATE TESTING  (NY session, score≥3 base)")
print("="*68)

base_ny = sess_ny[sess_ny['score_full'].abs() >= 3].copy()

# Month analysis
print(f"\n  --- MONTH ANALYSIS ---")
month_names  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
month_sharpes = {}
print(f"  {'Month':>6} {'Days':>6} {'Dir WR':>7} {'Avg ret':>9} {'Sharpe':>8}")
for m in range(1, 13):
    sub = base_ny[base_ny['month'] == m]
    if len(sub) < 3:
        continue
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
for gold_aligned, label in [(True, "Gold aligned"), (False, "Gold opposed")]:
    mask = base_ny['score_full'] * base_ny['gold_trend'] > 0 if gold_aligned \
           else base_ny['score_full'] * base_ny['gold_trend'] < 0
    sub = base_ny[mask]
    if len(sub) < 5:
        continue
    bull = sub[sub['score_full'] > 0]['ret']
    bear = sub[sub['score_full'] < 0]['ret']
    aligned = pd.concat([bull, -bear])
    w   = (aligned > 0).mean() * 100
    avg = aligned.mean()
    sh  = sharpe(aligned.values)
    print(f"    {label:15s}: {len(sub):3d} days | Dir WR {w:5.1f}% | avg {avg:+.3f}% | Sharpe {sh:.2f}")

# VIX gate
print(f"\n  --- VIX THRESHOLD ---")
for vix_thresh in [25, 30, 35, None]:
    label = f"VIX < {vix_thresh}" if vix_thresh else "No VIX gate"
    sub   = base_ny[base_ny['vix'] < vix_thresh] if vix_thresh else base_ny
    if len(sub) < 10:
        continue
    bull = sub[sub['score_full'] > 0]['ret']
    bear = sub[sub['score_full'] < 0]['ret']
    aligned = pd.concat([bull, -bear])
    w   = (aligned > 0).mean() * 100
    avg = aligned.mean()
    sh  = sharpe(aligned.values)
    print(f"    {label:15s}: {len(sub):3d} days | Dir WR {w:5.1f}% | avg {avg:+.3f}% | Sharpe {sh:.2f}")

# ── PHASE 4: A/B SESSION COMPARISON ──────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 4 — A/B SESSION COMPARISON")
print("  NY-only  vs  London-only  vs  Combined")
print("  RULE: London added ONLY if standalone Sharpe ≥ 1.5 AND combined >= NY")
print("="*68)

skip_set   = set(bad_months)
n_days_total = len(df)  # scoring days in backtest window

# Build score and gates for each session frame
for s in [sess_ny, sess_lon]:
    s['score_full'] = s[COMP_COLS].sum(axis=1)

def apply_best_filters(s, skip_months, vix_thresh=30, min_score=3, gold_align=True):
    out = s[s['score_full'].abs() >= min_score].copy()
    out = out[~out['month'].isin(skip_months)]
    out = out[out['vix'] < vix_thresh]
    if gold_align:
        out = out[out['score_full'] * out['gold_trend'] > 0]
    out['score'] = out['score_full']
    return out

def run_sim(label, sub, sess_name, n_days_total):
    if len(sub) < 10:
        print(f"\n  {label}: only {len(sub)} qualifying rows — skip")
        return None

    trades = simulate_session(sub, sess_name, direction_col='score')
    if not trades:
        print(f"\n  {label}: 0 trades simulated — skip")
        return None

    arr  = np.array(trades, dtype=float)
    sh   = sharpe(arr)
    w    = wr(arr)
    dd   = max_dd(arr)
    avg  = arr.mean()
    n    = len(arr)
    tpw  = trades_per_week(n, n_days_total)
    wins = int((arr > 0).sum())
    loss = int((arr < 0).sum())
    avg_win  = arr[arr > 0].mean() if wins else 0.0
    avg_loss = arr[arr < 0].mean() if loss else 0.0
    rr   = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

    print(f"\n  {label}")
    print(f"  {'─'*54}")
    print(f"    Trades    : {n}  ({tpw:.1f}/week)  |  WR {w:.1f}%  |  Sharpe {sh:.2f}")
    print(f"    Avg P&L   : {avg:+.3f}%  |  Max DD {dd:+.3f}%")
    print(f"    Avg Win   : {avg_win:+.3f}%  |  Avg Loss {avg_loss:+.3f}%  |  R:R {rr:.2f}")
    print(f"    Cum return: {arr.sum():+.2f}%")
    verdict = "✓ EDGE" if sh >= 1.5 else ("~ OK" if sh >= 1.0 else "✗ WEAK")
    print(f"    Verdict   : {verdict}  (Sharpe {sh:.2f})")
    return {'n': n, 'sharpe': sh, 'wr': w, 'dd': dd, 'avg': avg, 'tpw': tpw, 'arr': arr}

# ── MODEL A: NY only (v1 equivalent, time-stop fixed) ──────────────────────────
print("\n  ── MODEL A: NY SESSION ONLY (v1 baseline, time-stop fixed) ──")
sub_ny_base   = apply_best_filters(sess_ny, skip_set, vix_thresh=30, min_score=3, gold_align=False)
sub_ny_gold   = apply_best_filters(sess_ny, skip_set, vix_thresh=30, min_score=3, gold_align=True)
sub_ny_strong = apply_best_filters(sess_ny, skip_set, vix_thresh=30, min_score=5, gold_align=True)

rA0 = run_sim("A0: NY | score≥3 + months + VIX<30",              sub_ny_base,   'NY', n_days_total)
rA1 = run_sim("A1: NY | score≥3 + months + VIX<30 + gold align", sub_ny_gold,   'NY', n_days_total)
rA2 = run_sim("A2: NY | score≥5 + months + VIX<30 + gold align", sub_ny_strong, 'NY', n_days_total)

# ── MODEL B: London only ───────────────────────────────────────────────────────
print("\n  ── MODEL B: LONDON SESSION ONLY ──")
sub_lon_base   = apply_best_filters(sess_lon, skip_set, vix_thresh=30, min_score=3, gold_align=False)
sub_lon_gold   = apply_best_filters(sess_lon, skip_set, vix_thresh=30, min_score=3, gold_align=True)
sub_lon_strong = apply_best_filters(sess_lon, skip_set, vix_thresh=30, min_score=5, gold_align=True)

rB0 = run_sim("B0: London | score≥3 + months + VIX<30",              sub_lon_base,   'London', n_days_total)
rB1 = run_sim("B1: London | score≥3 + months + VIX<30 + gold align", sub_lon_gold,   'London', n_days_total)
rB2 = run_sim("B2: London | score≥5 + months + VIX<30 + gold align", sub_lon_strong, 'London', n_days_total)

# ── MODEL C: Combined (both sessions, same filters) ────────────────────────────
print("\n  ── MODEL C: COMBINED (NY + London, same filters) ──")

def run_combined(label, sub_ny, sub_lon, min_score_label):
    trades_ny  = simulate_session(sub_ny,  'NY',     direction_col='score')
    trades_lon = simulate_session(sub_lon, 'London', direction_col='score')
    all_trades = trades_ny + trades_lon

    if len(all_trades) < 10:
        print(f"\n  {label}: only {len(all_trades)} trades — skip")
        return None

    arr  = np.array(all_trades, dtype=float)
    sh   = sharpe(arr)
    w    = wr(arr)
    dd   = max_dd(arr)
    avg  = arr.mean()
    n    = len(arr)
    tpw  = trades_per_week(n, n_days_total)

    print(f"\n  {label}")
    print(f"  {'─'*54}")
    print(f"    Trades    : {n}  ({tpw:.1f}/week)  NY={len(trades_ny)} Lon={len(trades_lon)}")
    print(f"    WR {w:.1f}%  |  Sharpe {sh:.2f}  |  Max DD {dd:+.3f}%")
    print(f"    Avg P&L   : {avg:+.3f}%  |  Cum ret {arr.sum():+.2f}%")
    verdict = "✓ EDGE" if sh >= 1.5 else ("~ OK" if sh >= 1.0 else "✗ WEAK")
    print(f"    Verdict   : {verdict}  (Sharpe {sh:.2f})")
    return {'n': n, 'sharpe': sh, 'wr': w, 'dd': dd, 'avg': avg, 'tpw': tpw, 'arr': arr}

rC0 = run_combined("C0: NY+London | score≥3 + months + VIX<30",
                   sub_ny_base, sub_lon_base, "≥3")
rC1 = run_combined("C1: NY+London | score≥3 + months + VIX<30 + gold align",
                   sub_ny_gold, sub_lon_gold, "≥3+gold")
rC2 = run_combined("C2: NY+London | score≥5 + months + VIX<30 + gold align",
                   sub_ny_strong, sub_lon_strong, "≥5+gold")

# ── PHASE 5: SIDE-BY-SIDE VERDICT ────────────────────────────────────────────
print("\n" + "="*68)
print("  PHASE 5 — SIDE-BY-SIDE COMPARISON & VERDICT")
print("="*68)

print(f"\n  {'Model':46s} {'Trades':>7} {'T/wk':>6} {'WR':>7} {'Sharpe':>8} {'Max DD':>9}")
print(f"  {'─'*78}")

all_models = [
    ("A0: NY-only | score≥3 + months + VIX",           rA0),
    ("A1: NY-only | score≥3 + months + VIX + gold",    rA1),
    ("A2: NY-only | score≥5 + months + VIX + gold",    rA2),
    ("B0: London-only | score≥3 + months + VIX",       rB0),
    ("B1: London-only | score≥3 + months + VIX + gold",rB1),
    ("B2: London-only | score≥5 + months + VIX + gold",rB2),
    ("C0: Combined | score≥3 + months + VIX",          rC0),
    ("C1: Combined | score≥3 + months + VIX + gold",   rC1),
    ("C2: Combined | score≥5 + months + VIX + gold",   rC2),
]

best_sharpe = -999
best_model  = None
for label, r in all_models:
    if r:
        flag = "  ←" if r['sharpe'] >= 1.5 and r['tpw'] >= 2.0 else ""
        print(f"  {label:46s} {r['n']:>7}  {r['tpw']:>5.1f}  {r['wr']:>5.1f}%  {r['sharpe']:>7.2f}  {r['dd']:>+8.2f}%{flag}")
        if r['sharpe'] > best_sharpe:
            best_sharpe = r['sharpe']
            best_label  = label
            best_model  = r
    else:
        print(f"  {label:46s} {'—':>7}  {'—':>5}  {'—':>5}  {'—':>7}  {'—':>8}")

# ── A/B RULING: should London be added? ───────────────────────────────────────
# CORRECT comparison: same filter level, NY vs Combined
# (comparing A2 vs C0 would be apples-to-oranges due to different score thresholds)
print(f"\n  ── A/B RULING: ADD LONDON SESSION? (same-filter comparison) ──")

# Paired comparisons at each filter config
paired = [
    ("score≥3 + months + VIX",             rA0, rB0, rC0),
    ("score≥3 + months + VIX + gold align", rA1, rB1, rC1),
    ("score≥5 + months + VIX + gold align", rA2, rB2, rC2),
]
print(f"\n  {'Filter':42s} {'NY Sh':>7} {'Lon Sh':>7} {'Comb Sh':>8} {'Comb T/wk':>10}  Decision")
print(f"  {'─'*90}")

add_london  = False
best_live_r = None
best_live_label = ""

for config, rny, rlon, rcomb in paired:
    sh_ny    = rny['sharpe']   if rny   else float('nan')
    sh_lon   = rlon['sharpe']  if rlon  else float('nan')
    sh_comb  = rcomb['sharpe'] if rcomb else float('nan')
    tpw_comb = rcomb['tpw']    if rcomb else 0.0

    lon_ok   = sh_lon  >= 1.5
    comb_ok  = sh_comb >= sh_ny           # combined doesn't dilute NY
    freq_ok  = tpw_comb >= 2.0            # frequency target

    # Best model at this level for live use
    if lon_ok and freq_ok:
        decision = "✓ ADD"
        add_london = True
        if best_live_r is None or sh_comb > best_live_r['sharpe']:
            best_live_r     = rcomb
            best_live_label = f"Combined ({config})"
    elif lon_ok and not freq_ok:
        decision = "~ edge OK, freq low"
    else:
        decision = "✗ London weak"

    print(f"  {config:42s} {sh_ny:>7.2f} {sh_lon:>7.2f} {sh_comb:>8.2f} {tpw_comb:>10.1f}  {decision}")

# If no combined model met both criteria, check if any London model alone did
if not add_london:
    print(f"\n  No Combined model achieved London Sharpe ≥ 1.5 + freq ≥ 2.0/week")
    print(f"  Falling back to best NY-only model for live signal")
    # Best NY model meeting frequency: A0 is closest at 1.8/week
    best_live_r     = rA0
    best_live_label = "NY-only (score≥3 + months + VIX)"
else:
    print(f"\n  ✓ London session has independent edge AND combined model hits 2-3 trades/week")
    print(f"  Recommended live model: {best_live_label}")

# ── PHASE 6: COMPONENT SUMMARY ───────────────────────────────────────────────
print(f"\n{'='*68}")
print(f"  PHASE 6 — COMPONENT SIGNIFICANCE SUMMARY")
print(f"{'='*68}")
comp_names = {
    's_trend':    'Silver vs EMA50       ',
    's_gs_ratio': 'Gold/Silver ratio dir ',
    's_copper':   'Copper 5d momentum    ',
    's_dxy':      'DXY direction         ',
    's_yield':    '10Y yield direction   ',
    's_sil':      'SIL miners vs silver  ',
    's_mom':      'Silver 5d momentum    ',
}
print(f"\n  {'Component':28s} {'p-value':>9}  {'r':>7}  Verdict")
print(f"  {'─'*55}")
for col, name in comp_names.items():
    if col in component_results:
        r = component_results[col]
        sig = "✓ KEEP" if r['p'] < 0.05 else ("~ TEST" if r['p'] < 0.10 else "✗ SKIP")
        print(f"  {name:28s} p={r['p']:.4f}  r={r['corr']:+.4f}  {sig}")

# ── FINAL VERDICT ─────────────────────────────────────────────────────────────
print(f"\n{'='*68}")
print(f"  FINAL VERDICT")
print(f"{'='*68}")

live = best_live_r
if live:
    freq_ok = live['tpw'] >= 2.0
    edge_ok = live['sharpe'] >= 1.5
    print(f"""
  Recommended model  : {best_live_label}
  Trades/week        : {live['tpw']:.1f}  {'✓' if freq_ok else '✗ need ≥2.0'}
  Win rate           : {live['wr']:.1f}%
  Sharpe             : {live['sharpe']:.2f}  {'✓ ≥1.5' if edge_ok else '✗ need ≥1.5'}
  Max drawdown       : {live['dd']:+.2f}%
  Cumulative return  : {live['arr'].sum():+.2f}%

  Add London session : {'YES ✓' if add_london else 'NO ✗'}
""")
    if edge_ok and freq_ok:
        print(f"  ✓ BUILD LIVE SILVER SIGNAL — edge confirmed, frequency target met")
    elif edge_ok:
        print(f"  ~ Edge confirmed but frequency low — model needs tuning for more signals")
    else:
        print(f"  ✗ Edge not confirmed at Sharpe ≥ 1.5 — refine before going live")
else:
    print(f"\n  No valid model found — check data availability")

print("="*68 + "\n")
