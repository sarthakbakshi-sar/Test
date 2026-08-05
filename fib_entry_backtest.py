"""
fib_entry_backtest.py — The actual trading test

After each impulse ends, wait up to MAX_ENTRY_BARS for price to
pull back to a Fibonacci level, then enter in the impulse direction.
SL = 1.5×ATR, TP = 2.5×ATR (break-even WR = 37.5%).

Compares under identical rules:
  A. Accepted impulses  (score ≥ 0.57)
  B. Rejected candidates (ATR pass, score < 0.57)
  C. Random entries     (immediate entry at random bar, same SL/TP)
"""

import os
import numpy as np
import pandas as pd
from market_structure import ImpulseEngine, Direction

# ── Parameters ───────────────────────────────────────────────────────────────
FIB_LEVELS     = [0.382, 0.500, 0.618, 0.705, 0.786]
SL_MULT        = 1.5
TP_MULT        = 2.5
MAX_ENTRY_BARS = 50     # bars to wait for Fib touch after impulse ends
MAX_HOLD_BARS  = 100    # bars to hold after entry before timeout close
ATR_PERIOD     = 14
MIN_SCORE      = 0.57
N_RANDOM       = 2000   # random baseline trades (half bull, half bear)

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

hi = df['high'].values
lo = df['low'].values
cl = df['close'].values
N  = len(df)
print(f"Data: {N:,} bars  {df.index[0].date()} → {df.index[-1].date()}")

# ── ATR ───────────────────────────────────────────────────────────────────────
h, l, c_prev = df['high'], df['low'], df['close'].shift(1)
tr  = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
atr = tr.rolling(ATR_PERIOD, min_periods=1).mean().values

# ── Core simulation ───────────────────────────────────────────────────────────
def simulate(start_i, direction, entry_px, sl_px, tp_px):
    """
    From bar start_i onward, find which of SL/TP is hit first.
    Returns ('win'|'loss'|'timeout', r_multiple).
    Assumes SL wins on same-bar collision (conservative).
    """
    end_i  = min(start_i + MAX_HOLD_BARS, N)
    hi_fwd = hi[start_i: end_i]
    lo_fwd = lo[start_i: end_i]
    cl_fwd = cl[start_i: end_i]

    if direction == Direction.BULLISH:
        sl_bars = np.where(lo_fwd <= sl_px)[0]
        tp_bars = np.where(hi_fwd >= tp_px)[0]
    else:
        sl_bars = np.where(hi_fwd >= sl_px)[0]
        tp_bars = np.where(lo_fwd <= tp_px)[0]

    sl_i = sl_bars[0] if len(sl_bars) else len(hi_fwd)
    tp_i = tp_bars[0] if len(tp_bars) else len(hi_fwd)

    if sl_i <= tp_i and sl_i < len(hi_fwd):
        return 'loss', -SL_MULT
    if tp_i < sl_i:
        return 'win', TP_MULT

    # Timeout: close at last bar
    exit_px  = cl_fwd[-1] if len(cl_fwd) else entry_px
    atr_dist = abs(entry_px - sl_px)
    if atr_dist < 1e-9:
        return 'timeout', 0.0
    r = (exit_px - entry_px) if direction == Direction.BULLISH else (entry_px - exit_px)
    return 'timeout', round(r / atr_dist, 3)


def fib_entry(end_idx, direction, imp_high, imp_low, fib_ratio):
    """
    Scan for a Fib touch within MAX_ENTRY_BARS of end_idx.
    Returns (entry_idx, entry_price) or (None, None) if no fill.
    """
    span      = imp_high - imp_low
    fib_price = (imp_high - fib_ratio * span) if direction == Direction.BULLISH \
                else (imp_low  + fib_ratio * span)

    scan_end = min(end_idx + 1 + MAX_ENTRY_BARS, N)
    for i in range(end_idx + 1, scan_end):
        if direction == Direction.BULLISH and lo[i] <= fib_price:
            return i, fib_price
        if direction == Direction.BEARISH and hi[i] >= fib_price:
            return i, fib_price
    return None, None


# ── Collect ALL swing candidates (accepted + rejected) ────────────────────────
engine = ImpulseEngine(swing_lookback=5, min_atr_mult=1.5, min_score=0.0, atr_period=ATR_PERIOD)
atr_s  = engine._atr(df)
swings = engine._alternate(engine._raw_swings(df))

candidates = []  # (end_idx, direction, imp_high, imp_low, atr_at_end, score)
for i in range(len(swings) - 1):
    s, e = swings[i], swings[i + 1]
    if   s.kind == 'low'  and e.kind == 'high': direction = Direction.BULLISH
    elif s.kind == 'high' and e.kind == 'low':  direction = Direction.BEARISH
    else: continue

    n_bars   = e.idx - s.idx
    abs_move = abs(e.price - s.price)
    avg_atr  = atr_s.iloc[s.idx: e.idx].mean()
    if n_bars < 3 or pd.isna(avg_atr) or avg_atr == 0: continue
    if abs_move < 1.5 * avg_atr:                        continue

    score, _ = engine._score(df, s.idx, e.idx, direction, atr_s)
    seg      = df.iloc[s.idx: e.idx + 1]
    imp_high = float(seg['high'].max())
    imp_low  = float(seg['low'].min())
    atr_end  = float(atr[e.idx])

    if e.idx + MAX_ENTRY_BARS + MAX_HOLD_BARS >= N: continue  # need room ahead
    candidates.append((e.idx, direction, imp_high, imp_low, atr_end, score))

print(f"Candidates: {len(candidates)} total  "
      f"({sum(1 for c in candidates if c[5] >= MIN_SCORE)} accepted, "
      f"{sum(1 for c in candidates if c[5] < MIN_SCORE)} rejected)")


# ── Run Fib entry backtest for each group ─────────────────────────────────────
def run_fib_backtest(cands, label):
    results = {fib: [] for fib in FIB_LEVELS}
    for (end_idx, direction, imp_high, imp_low, atr_end, score) in cands:
        for fib in FIB_LEVELS:
            entry_i, entry_px = fib_entry(end_idx, direction, imp_high, imp_low, fib)
            if entry_i is None:
                continue
            atr_e  = atr[entry_i] if atr[entry_i] > 0 else atr_end
            sl_px  = (entry_px - SL_MULT * atr_e) if direction == Direction.BULLISH \
                     else (entry_px + SL_MULT * atr_e)
            tp_px  = (entry_px + TP_MULT * atr_e) if direction == Direction.BULLISH \
                     else (entry_px - TP_MULT * atr_e)
            outcome, r = simulate(entry_i + 1, direction, entry_px, sl_px, tp_px)
            results[fib].append((direction.value, outcome, r, entry_i))
    return results

print("\nRunning backtest on accepted …")
res_acc  = run_fib_backtest([c for c in candidates if c[5] >= MIN_SCORE], 'Accepted')
print("Running backtest on rejected …")
res_rej  = run_fib_backtest([c for c in candidates if c[5] <  MIN_SCORE], 'Rejected')


# ── Random baseline ───────────────────────────────────────────────────────────
print("Running random baseline …")
rng     = np.random.default_rng(42)
rand_i  = rng.integers(ATR_PERIOD, N - MAX_HOLD_BARS - 1, size=N_RANDOM)
rand_d  = [Direction.BULLISH if x else Direction.BEARISH for x in rng.integers(0, 2, size=N_RANDOM)]

rand_results = []
for i, direction in zip(rand_i, rand_d):
    entry_px = cl[i]
    atr_e    = atr[i] if atr[i] > 0 else 1.0
    sl_px    = (entry_px - SL_MULT * atr_e) if direction == Direction.BULLISH \
               else (entry_px + SL_MULT * atr_e)
    tp_px    = (entry_px + TP_MULT * atr_e) if direction == Direction.BULLISH \
               else (entry_px - TP_MULT * atr_e)
    outcome, r = simulate(i + 1, direction, entry_px, sl_px, tp_px)
    rand_results.append((direction.value, outcome, r))


# ── Reporting ─────────────────────────────────────────────────────────────────
def stats(trades, label=''):
    if not trades:
        return
    df_t  = pd.DataFrame(trades, columns=['dir', 'outcome', 'r'] + (['entry_i'] if len(trades[0]) > 3 else []))
    wins  = df_t[df_t['outcome'] == 'win']
    losses= df_t[df_t['outcome'] == 'loss']
    decided = len(wins) + len(losses)
    wr    = len(wins) / decided * 100 if decided else 0
    exp   = df_t['r'].mean()
    fill  = len(df_t)
    return wr, exp, fill, decided

def print_group(res, label):
    print(f"\n  ── {label} ──────────────────────────────────────")
    print(f"  {'Fib':>6}  {'Fills':>6}  {'Decided':>8}  {'WR%':>6}  {'Avg R':>7}  {'Timeouts%':>10}")
    for fib in FIB_LEVELS:
        trades = res[fib]
        if not trades:
            print(f"  {fib:>6.3f}  {'—':>6}")
            continue
        df_t    = pd.DataFrame(trades, columns=['dir', 'outcome', 'r', 'entry_i'])
        wins    = (df_t['outcome'] == 'win').sum()
        losses  = (df_t['outcome'] == 'loss').sum()
        timeouts= (df_t['outcome'] == 'timeout').sum()
        decided = wins + losses
        wr      = wins / decided * 100 if decided else 0
        avg_r   = df_t['r'].mean()
        to_pct  = timeouts / len(df_t) * 100
        print(f"  {fib:>6.3f}  {len(df_t):>6}  {decided:>8}  {wr:>6.1f}%  {avg_r:>7.3f}  {to_pct:>9.1f}%")

print("\n════════════════════════════════════════════════════════════════")
print("  FIB ENTRY BACKTEST  |  SL=1.5×ATR  TP=2.5×ATR  |  Break-even=37.5%")
print("════════════════════════════════════════════════════════════════")

print_group(res_acc, f'A. ACCEPTED impulses (score ≥ {MIN_SCORE})')
print_group(res_rej, f'B. REJECTED candidates (score < {MIN_SCORE})')

# Random
df_rand = pd.DataFrame(rand_results, columns=['dir', 'outcome', 'r'])
wins    = (df_rand['outcome'] == 'win').sum()
losses  = (df_rand['outcome'] == 'loss').sum()
decided = wins + losses
wr      = wins / decided * 100 if decided else 0
avg_r   = df_rand['r'].mean()
to_pct  = (df_rand['outcome'] == 'timeout').sum() / len(df_rand) * 100
print(f"\n  ── C. RANDOM entries (immediate, {N_RANDOM} trades) ──────────────")
print(f"  {'Entry':>6}  {'Fills':>6}  {'Decided':>8}  {'WR%':>6}  {'Avg R':>7}  {'Timeouts%':>10}")
print(f"  {'market':>6}  {N_RANDOM:>6}  {decided:>8}  {wr:>6.1f}%  {avg_r:>7.3f}  {to_pct:>9.1f}%")

# ── Direction breakdown for best Fib level ────────────────────────────────────
print("\n════════════════════════════════════════════════════════════════")
print("  DIRECTION BREAKDOWN @ each Fib — Accepted vs Rejected vs Random")
print("  (Win Rate of decided trades only)")
print(f"  {'Group':<30}  {'Fib':>6}  {'Bull WR':>8}  {'Bear WR':>8}  {'Bull n':>7}  {'Bear n':>7}")
print(f"  {'-'*68}")

for fib in FIB_LEVELS:
    for group_label, res in [('Accepted', res_acc), ('Rejected', res_rej)]:
        trades = res[fib]
        if not trades:
            continue
        df_t = pd.DataFrame(trades, columns=['dir', 'outcome', 'r', 'entry_i'])
        for d in ['bullish', 'bearish']:
            sub = df_t[df_t['dir'] == d]
            dec = sub[sub['outcome'].isin(['win', 'loss'])]
            wr  = (dec['outcome'] == 'win').sum() / len(dec) * 100 if len(dec) else 0

# Cleaner: print as a table by Fib level
print()
for fib in FIB_LEVELS:
    print(f"\n  Fib = {fib:.3f}")
    for group_label, res in [('Accepted', res_acc), ('Rejected', res_rej)]:
        trades = res[fib]
        if not trades:
            continue
        df_t = pd.DataFrame(trades, columns=['dir', 'outcome', 'r', 'entry_i'])
        parts = []
        for d in ['bullish', 'bearish']:
            sub = df_t[df_t['dir'] == d]
            dec = sub[sub['outcome'].isin(['win', 'loss'])]
            wr  = (dec['outcome'] == 'win').sum() / len(dec) * 100 if len(dec) else 0
            parts.append(f"{d[:4]} {wr:.0f}% (n={len(dec)})")
        print(f"    {group_label:<12}: {' | '.join(parts)}")
    # Random
    df_rand2 = df_rand.copy()
    parts = []
    for d in ['bullish', 'bearish']:
        sub = df_rand2[df_rand2['dir'] == d]
        dec = sub[sub['outcome'].isin(['win', 'loss'])]
        wr  = (dec['outcome'] == 'win').sum() / len(dec) * 100 if len(dec) else 0
        parts.append(f"{d[:4]} {wr:.0f}% (n={len(dec)})")
    print(f"    {'Random':<12}: {' | '.join(parts)}")

# ── Accepted score bucket breakdown at 0.618 ─────────────────────────────────
print("\n════════════════════════════════════════════════════════════════")
print("  ACCEPTED: WR by score bucket @ Fib 0.618")
print("════════════════════════════════════════════════════════════════")
trades_618 = res_acc[0.618]
if trades_618:
    df_618   = pd.DataFrame(trades_618, columns=['dir', 'outcome', 'r', 'entry_i'])
    scores_a = [c[5] for c in candidates if c[5] >= MIN_SCORE]
    entry_is = df_618['entry_i'].values

    # Re-map entry to original candidate scores (approximate: use order)
    # Build (end_idx → score) lookup
    acc_cands = [c for c in candidates if c[5] >= MIN_SCORE]
    end_to_score = {c[0]: c[5] for c in acc_cands}
    # Find the impulse for each trade entry (entry is within MAX_ENTRY_BARS of end_idx)
    # Simplest: group by score bucket using candidate order
    bucket_results = {'4/7': [], '5/7': [], '6-7/7': []}
    for c in acc_cands:
        end_idx = c[0]; score = c[5]
        entry_i, entry_px = fib_entry(end_idx, c[1], c[2], c[3], 0.618)
        if entry_i is None: continue
        atr_e = atr[entry_i] if atr[entry_i] > 0 else c[4]
        sl_px = (entry_px - SL_MULT * atr_e) if c[1] == Direction.BULLISH else (entry_px + SL_MULT * atr_e)
        tp_px = (entry_px + TP_MULT * atr_e) if c[1] == Direction.BULLISH else (entry_px - TP_MULT * atr_e)
        outcome, r = simulate(entry_i + 1, c[1], entry_px, sl_px, tp_px)
        if   score < 0.715: bucket = '4/7'
        elif score < 0.858: bucket = '5/7'
        else:               bucket = '6-7/7'
        bucket_results[bucket].append((c[1].value, outcome, r))

    for bucket, trades in bucket_results.items():
        if not trades: continue
        df_b = pd.DataFrame(trades, columns=['dir', 'outcome', 'r'])
        dec  = df_b[df_b['outcome'].isin(['win', 'loss'])]
        wr   = (dec['outcome'] == 'win').sum() / len(dec) * 100 if len(dec) else 0
        avg_r= df_b['r'].mean()
        print(f"  {bucket} conditions: WR={wr:.1f}%  Avg R={avg_r:.3f}  n={len(dec)} decided / {len(df_b)} total")

print("\nDone.")
