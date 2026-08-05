"""
baseline_comparison.py
Controls for the market_structure.py continuation rate claims.

Measures, under the IDENTICAL protocol:
  A. Accepted impulses (score >= threshold)        — what we reported
  B. Rejected candidates (same swing pairs, failed threshold)
  C. All bars in the dataset (pure random baseline)

"continued" definition (same in all three):
  BULLISH: did price exceed the reference HIGH within the next fwd_window bars?
  BEARISH: did price go below the reference LOW  within the next fwd_window bars?
"""

import os
import numpy as np
import pandas as pd
from market_structure import ImpulseEngine, Direction

FWD_WINDOW   = 100
SWING_LB     = 5
MIN_ATR_MULT = 1.5
MIN_SCORE    = 0.57

# ── Load data ────────────────────────────────────────────────────────────────
UPLOAD = '/root/.claude/uploads/72260b9c-d25d-5454-aecb-8b4ff1e6383e'
raw = pd.read_csv(
    os.path.join(UPLOAD, '56b206c2-XAU_1h_data.csv'),
    sep=';', header=0, names=['ts','open','high','low','close','volume']
)
raw['ts'] = pd.to_datetime(raw['ts'], format='%Y.%m.%d %H:%M', utc=True)
raw.set_index('ts', inplace=True)
df = raw['2023-01-01':].copy()
df.columns = [c.lower() for c in df.columns]
print(f"Data: {len(df):,} bars  {df.index[0].date()} → {df.index[-1].date()}")

hi = df['high'].values
lo = df['low'].values
N  = len(df)

# Pre-build forward-window max/min arrays for speed
# fwd_max[i] = max high in bars i+1 … i+fwd_window
# fwd_min[i] = min low  in bars i+1 … i+fwd_window
fwd_max = np.full(N, np.nan)
fwd_min = np.full(N, np.nan)
for i in range(N - 1):
    end = min(i + 1 + FWD_WINDOW, N)
    fwd_max[i] = hi[i+1:end].max()
    fwd_min[i] = lo[i+1:end].min()

print("Forward arrays built.")

# ── Engine internals (bypass public detect() to collect rejecteds too) ───────
engine = ImpulseEngine(
    swing_lookback = SWING_LB,
    min_atr_mult   = MIN_ATR_MULT,
    min_score      = 0.0,          # collect ALL candidates first
    max_pullback   = 0.40,
    atr_period     = 14,
)
atr    = engine._atr(df)
swings = engine._alternate(engine._raw_swings(df))

accepted_cont  = []
accepted_dir   = []
rejected_cont  = []
rejected_dir   = []
scores_acc     = []
scores_rej     = []

for i in range(len(swings) - 1):
    s, e = swings[i], swings[i + 1]
    if   s.kind == 'low'  and e.kind == 'high': direction = Direction.BULLISH
    elif s.kind == 'high' and e.kind == 'low':  direction = Direction.BEARISH
    else: continue

    n_bars   = e.idx - s.idx
    abs_move = abs(e.price - s.price)
    avg_atr  = atr.iloc[s.idx: e.idx].mean()
    if n_bars < 3 or pd.isna(avg_atr) or avg_atr == 0:     continue
    if abs_move < MIN_ATR_MULT * avg_atr:                   continue   # ATR gate same for both

    # Score
    score, _ = engine._score(df, s.idx, e.idx, direction, atr)

    # Reference level = segment extreme (what price must exceed for "continuation")
    seg      = df.iloc[s.idx: e.idx + 1]
    ref_high = float(seg['high'].max())
    ref_low  = float(seg['low'].min())
    end_i    = e.idx

    if end_i >= N - 1:
        continue

    if direction == Direction.BULLISH:
        cont = int(fwd_max[end_i] > ref_high) if not np.isnan(fwd_max[end_i]) else 0
    else:
        cont = int(fwd_min[end_i] < ref_low)  if not np.isnan(fwd_min[end_i]) else 0

    if score >= MIN_SCORE:
        accepted_cont.append(cont)
        accepted_dir.append(direction.value)
        scores_acc.append(score)
    else:
        rejected_cont.append(cont)
        rejected_dir.append(direction.value)
        scores_rej.append(score)

# ── C. Random bars baseline ──────────────────────────────────────────────────
# For every bar in the dataset (both directions):
# BULLISH test: did fwd_max[i] exceed hi[i]?
# BEARISH test: did fwd_min[i] go below lo[i]?
valid_idx = np.arange(N - FWD_WINDOW - 1)   # bars with a full forward window

bull_random = (fwd_max[valid_idx] > hi[valid_idx]).astype(int)
bear_random = (fwd_min[valid_idx] < lo[valid_idx]).astype(int)

# ── Print results ─────────────────────────────────────────────────────────────
def rate(arr, label='', subset=None):
    a = np.array(arr)
    if subset:
        mask = np.array(subset)
        a = a[mask]
    if len(a) == 0:
        return f"{label}: n=0"
    return f"{len(a):5d}  {a.mean()*100:.1f}%"

print("\n══ CONTINUATION RATE COMPARISON ══════════════════════════════════════")
print(f"  Measurement: price exceeds impulse extreme within next {FWD_WINDOW} bars")
print()
print(f"  {'Group':<40} {'n':>6}  {'rate':>6}")
print(f"  {'-'*55}")

acc = np.array(accepted_cont);  acc_d = np.array(accepted_dir)
rej = np.array(rejected_cont);  rej_d = np.array(rejected_dir)

# Accepted — split by direction
for d in ['bullish', 'bearish']:
    m = acc_d == d
    sub = acc[m]
    tag = f"A. Accepted {d} (score ≥ {MIN_SCORE})"
    print(f"  {tag:<40} {len(sub):>6}  {sub.mean()*100:.1f}%")

print()
# Rejected — split by direction
for d in ['bullish', 'bearish']:
    m = rej_d == d
    sub = rej[m]
    tag = f"B. Rejected {d} (ATR pass, score < {MIN_SCORE})"
    print(f"  {tag:<40} {len(sub):>6}  {sub.mean()*100:.1f}%")

print()
# All candidates (both accepted and rejected) — unfiltered score
all_cont = np.concatenate([accepted_cont, rejected_cont])
all_dir  = np.concatenate([accepted_dir, rejected_dir])
for d in ['bullish', 'bearish']:
    m = all_dir == d
    sub = all_cont[m]
    tag = f"A+B. All swing candidates {d}"
    print(f"  {tag:<40} {len(sub):>6}  {sub.mean()*100:.1f}%")

print()
# Random bars
print(f"  {'C. Random bars bullish (any bar)':<40} {len(bull_random):>6}  {bull_random.mean()*100:.1f}%")
print(f"  {'C. Random bars bearish (any bar)':<40} {len(bear_random):>6}  {bear_random.mean()*100:.1f}%")

print()
print("══ SCORE DISTRIBUTION ════════════════════════════════════════════════")
print(f"\n  Accepted  n={len(scores_acc)}  score: "
      f"mean={np.mean(scores_acc):.3f}  min={np.min(scores_acc):.3f}  max={np.max(scores_acc):.3f}")
print(f"  Rejected  n={len(scores_rej)}  score: "
      f"mean={np.mean(scores_rej):.3f}  min={np.min(scores_rej):.3f}  max={np.max(scores_rej):.3f}")

# Score vs continuation for accepted
print("\n══ ACCEPTED: CONTINUATION BY SCORE BUCKET ═══════════════════════════")
df_acc = pd.DataFrame({'score': scores_acc, 'cont': accepted_cont, 'dir': accepted_dir})
bins   = [0.56, 0.70, 0.85, 1.01]
labels = ['4/7 (0.57)', '5/7 (0.71)', '6-7/7 (0.86+)']
df_acc['bucket'] = pd.cut(df_acc['score'], bins=bins, labels=labels)
for d in ['bullish', 'bearish']:
    sub = df_acc[df_acc['dir'] == d]
    print(f"\n  {d.upper()}")
    for bucket, grp in sub.groupby('bucket', observed=True):
        print(f"    {bucket}: {grp['cont'].mean()*100:.1f}%  (n={len(grp)})")

print("\n══ REJECTED: CONTINUATION BY SCORE BUCKET ════════════════════════════")
df_rej = pd.DataFrame({'score': scores_rej, 'cont': rejected_cont, 'dir': rejected_dir})
df_rej['bucket'] = pd.cut(df_rej['score'], bins=[0, 0.28, 0.42, 0.57],
                           labels=['1/7', '2/7', '3/7'])
for d in ['bullish', 'bearish']:
    sub = df_rej[df_rej['dir'] == d]
    if sub.empty: continue
    print(f"\n  {d.upper()}")
    for bucket, grp in sub.groupby('bucket', observed=True):
        print(f"    {bucket}: {grp['cont'].mean()*100:.1f}%  (n={len(grp)})")
