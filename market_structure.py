"""
market_structure.py — Objective Market Structure Engine
Detects impulse legs in BTC, Crypto, Gold, Forex from raw OHLCV data.

Pipeline:
  1. Swing point detection   (fractal, N-bar lookback)
  2. Impulse scoring         (7 objective conditions, threshold filter)
  3. Fibonacci levels        (0.236 → 0.786 retracements)
  4. BOS / CHoCH labeling
  5. Liquidity sweep detection
  6. Historical outcome statistics
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from enum import Enum


# ═══════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════

class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"

class StructureEvent(str, Enum):
    BOS   = "BOS"
    CHOCH = "CHoCH"

@dataclass
class SwingPoint:
    idx:   int
    ts:    pd.Timestamp
    price: float
    kind:  str          # 'high' | 'low'

@dataclass
class Impulse:
    direction:   Direction
    start_ts:    pd.Timestamp
    end_ts:      pd.Timestamp
    start_price: float
    end_price:   float
    high:        float
    low:         float
    pct_move:    float
    n_candles:   int
    score:       float          # 0–1, fraction of 7 conditions met
    conditions:  List[str]
    fib:         Dict[str, float] = field(default_factory=dict)
    vol_avg:     Optional[float] = None
    vol_ratio:   Optional[float] = None

@dataclass
class StructureLabel:
    event:        StructureEvent
    reference_ts: pd.Timestamp
    reference_px: float         # the level that must break
    broken_ts:    pd.Timestamp
    broken_px:    float

@dataclass
class LiquiditySweep:
    direction:   Direction       # BEARISH = swept below a low, BULLISH = swept above a high
    ts:          pd.Timestamp
    swept_level: float
    close_px:    float
    wick_ratio:  float           # wick_beyond_level / candle_range


# ═══════════════════════════════════════════════════════════════════
# 1. Impulse Engine
# ═══════════════════════════════════════════════════════════════════

class ImpulseEngine:
    """
    Detects scored impulse legs from OHLCV data.

    Parameters
    ----------
    swing_lookback : bars on each side for fractal swing detection (default 5)
    min_atr_mult   : minimum impulse size in ATR multiples (default 1.5)
    min_score      : minimum fraction of 7 conditions met, 4/7 ≈ 0.57 (default)
    max_pullback   : max intra-impulse retracement as fraction of total move
    atr_period     : period for ATR baseline
    """

    FIB_RATIOS = [('0.0', 0.0), ('0.236', 0.236), ('0.382', 0.382),
                  ('0.5', 0.5), ('0.618', 0.618), ('0.705', 0.705),
                  ('0.786', 0.786), ('1.0', 1.0)]

    def __init__(
        self,
        swing_lookback: int   = 5,
        min_atr_mult:   float = 1.5,
        min_score:      float = 0.57,
        max_pullback:   float = 0.40,
        atr_period:     int   = 14,
    ):
        self.swing_lookback = swing_lookback
        self.min_atr_mult   = min_atr_mult
        self.min_score      = min_score
        self.max_pullback   = max_pullback
        self.atr_period     = atr_period

    # ── Private helpers ──────────────────────────────────────────────────────

    def _atr(self, df: pd.DataFrame) -> pd.Series:
        h, l, c = df['high'], df['low'], df['close'].shift(1)
        tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
        return tr.rolling(self.atr_period, min_periods=1).mean()

    def _raw_swings(self, df: pd.DataFrame) -> List[SwingPoint]:
        n  = self.swing_lookback
        hi = df['high'].values
        lo = df['low'].values
        ts = df.index
        pts: List[SwingPoint] = []
        for i in range(n, len(df) - n):
            w_hi = hi[i - n: i + n + 1]
            w_lo = lo[i - n: i + n + 1]
            if hi[i] >= w_hi.max():
                pts.append(SwingPoint(i, ts[i], float(hi[i]), 'high'))
            if lo[i] <= w_lo.min():
                pts.append(SwingPoint(i, ts[i], float(lo[i]), 'low'))
        pts.sort(key=lambda p: p.idx)
        seen: dict = {}
        unique: List[SwingPoint] = []
        for p in pts:
            if p.idx not in seen:
                seen[p.idx] = p
                unique.append(p)
        return unique

    def _alternate(self, pts: List[SwingPoint]) -> List[SwingPoint]:
        """Force strict high→low→high alternation; when two same-kind points
        appear consecutively, keep the more extreme one."""
        if not pts:
            return []
        result = [pts[0]]
        for p in pts[1:]:
            last = result[-1]
            if p.kind != last.kind:
                result.append(p)
            else:
                # Same kind — keep more extreme
                replace = (p.kind == 'high' and p.price >= last.price) or \
                          (p.kind == 'low'  and p.price <= last.price)
                if replace:
                    result[-1] = p
        return result

    def _score(
        self,
        df:        pd.DataFrame,
        s_idx:     int,
        e_idx:     int,
        direction: Direction,
        atr:       pd.Series,
    ) -> Tuple[float, List[str]]:
        """Score candidate impulse against 7 objective conditions."""
        seg = df.iloc[s_idx: e_idx + 1]
        o = seg['open'].values
        h = seg['high'].values
        l = seg['low'].values
        c = seg['close'].values
        n = len(seg)
        met: List[str] = []
        bull = direction == Direction.BULLISH

        # C1 — Predominantly directional candles (≥ 60 %)
        dir_count = np.sum(c > o) if bull else np.sum(c < o)
        if dir_count / n >= 0.60:
            met.append("directional_candles")

        # C2 — Strong displacement vs ATR baseline
        raw_move = (c[-1] - o[0]) if bull else (o[0] - c[-1])
        avg_atr  = atr.iloc[s_idx: e_idx].mean()
        if not pd.isna(avg_atr) and avg_atr > 0 and raw_move >= self.min_atr_mult * avg_atr:
            met.append("strong_displacement")

        # C3 — Shallow pullbacks throughout the move
        total_swing = (h[-1] - l[0]) if bull else (h[0] - l[-1])
        max_pb = 0.0
        if bull:
            peak = l[0]
            for i in range(n):
                if h[i] > peak:
                    peak = h[i]
                if total_swing > 0:
                    max_pb = max(max_pb, (peak - l[i]) / total_swing)
        else:
            trough = h[0]
            for i in range(n):
                if l[i] < trough:
                    trough = l[i]
                if total_swing > 0:
                    max_pb = max(max_pb, (h[i] - trough) / total_swing)
        if max_pb <= self.max_pullback:
            met.append("shallow_pullbacks")

        # C4 — Bodies larger than preceding segment average
        rng   = np.where(h - l > 0, h - l, 1e-9)
        br    = np.mean(np.abs(c - o) / rng)
        pre   = df.iloc[max(0, s_idx - n): s_idx]
        if len(pre) >= 3:
            p_rng = np.where(pre['high'].values - pre['low'].values > 0,
                             pre['high'].values - pre['low'].values, 1e-9)
            p_br  = np.mean(np.abs(pre['close'].values - pre['open'].values) / p_rng)
            if br > p_br * 1.05:
                met.append("larger_bodies")
        else:
            if br >= 0.55:
                met.append("larger_bodies")

        # C5 — Closes consistently near candle extreme
        if bull:
            pos = np.mean((c - l) / rng)
        else:
            pos = np.mean((h - c) / rng)
        if pos >= 0.60:
            met.append("close_near_extreme")

        # C6 — Above-average volume (skip if no volume column or all-zero)
        if 'volume' in df.columns and df['volume'].sum() > 0:
            seg_vol  = seg['volume'].mean()
            base_vol = df['volume'].iloc[max(0, s_idx - 20): s_idx].mean()
            if base_vol > 0 and seg_vol >= base_vol * 1.25:
                met.append("elevated_volume")

        # C7 — Breaks previous swing high (bull) or swing low (bear)
        lookback = df.iloc[max(0, s_idx - 20): s_idx]
        if len(lookback) > 0:
            if bull and h[-1] > lookback['high'].max():
                met.append("breaks_prior_swing")
            elif not bull and l[-1] < lookback['low'].min():
                met.append("breaks_prior_swing")

        return len(met) / 7.0, met

    # ── Public API ───────────────────────────────────────────────────────────

    def swing_points(self, df: pd.DataFrame) -> List[SwingPoint]:
        """Return alternating swing highs/lows for the full dataframe."""
        df = df.copy(); df.columns = [c.lower() for c in df.columns]
        return self._alternate(self._raw_swings(df))

    def detect(self, df: pd.DataFrame) -> List[Impulse]:
        """
        Detect impulse legs from a DataFrame.

        Input columns (case-insensitive): open, high, low, close, [volume]
        Index: datetime-like (used as timestamps in output)

        Returns list of Impulse objects sorted by start_ts.
        """
        df = df.copy(); df.columns = [c.lower() for c in df.columns]
        atr    = self._atr(df)
        swings = self._alternate(self._raw_swings(df))
        impulses: List[Impulse] = []

        for i in range(len(swings) - 1):
            s, e = swings[i], swings[i + 1]

            if   s.kind == 'low'  and e.kind == 'high': direction = Direction.BULLISH
            elif s.kind == 'high' and e.kind == 'low':  direction = Direction.BEARISH
            else: continue

            n_bars   = e.idx - s.idx
            abs_move = abs(e.price - s.price)
            avg_atr  = atr.iloc[s.idx: e.idx].mean()

            if n_bars < 3:                                        continue
            if pd.isna(avg_atr) or avg_atr == 0:                 continue
            if abs_move < self.min_atr_mult * avg_atr:           continue

            pct_move = abs_move / s.price * 100 if s.price > 0 else 0.0
            score, conditions = self._score(df, s.idx, e.idx, direction, atr)
            if score < self.min_score:                            continue

            seg      = df.iloc[s.idx: e.idx + 1]
            imp_high = float(seg['high'].max())
            imp_low  = float(seg['low'].min())
            span     = imp_high - imp_low

            # Fibonacci retracements measured from the impulse
            fib: Dict[str, float] = {}
            for label, ratio in self.FIB_RATIOS:
                if direction == Direction.BULLISH:
                    fib[label] = round(imp_high - ratio * span, 4)
                else:
                    fib[label] = round(imp_low  + ratio * span, 4)

            vol_avg = vol_ratio = None
            if 'volume' in df.columns:
                vol_avg  = float(seg['volume'].mean())
                base_vol = df['volume'].iloc[max(0, s.idx - 20): s.idx].mean()
                if base_vol > 0:
                    vol_ratio = round(vol_avg / base_vol, 3)

            impulses.append(Impulse(
                direction   = direction,
                start_ts    = s.ts,
                end_ts      = e.ts,
                start_price = round(s.price, 4),
                end_price   = round(e.price, 4),
                high        = round(imp_high, 4),
                low         = round(imp_low,  4),
                pct_move    = round(pct_move, 3),
                n_candles   = n_bars,
                score       = round(score, 3),
                conditions  = conditions,
                fib         = fib,
                vol_avg     = round(vol_avg, 2) if vol_avg else None,
                vol_ratio   = vol_ratio,
            ))

        return impulses

    def to_dataframe(self, impulses: List[Impulse]) -> pd.DataFrame:
        """Flatten impulse list to a tidy DataFrame."""
        rows = []
        for imp in impulses:
            rows.append({
                'direction':   imp.direction.value,
                'start_ts':    imp.start_ts,
                'end_ts':      imp.end_ts,
                'start_price': imp.start_price,
                'end_price':   imp.end_price,
                'high':        imp.high,
                'low':         imp.low,
                'pct_move':    imp.pct_move,
                'n_candles':   imp.n_candles,
                'score':       imp.score,
                'conditions':  '|'.join(imp.conditions),
                'n_conditions':len(imp.conditions),
                'fib_0.382':   imp.fib.get('0.382'),
                'fib_0.5':     imp.fib.get('0.5'),
                'fib_0.618':   imp.fib.get('0.618'),
                'fib_0.705':   imp.fib.get('0.705'),
                'fib_0.786':   imp.fib.get('0.786'),
                'vol_avg':     imp.vol_avg,
                'vol_ratio':   imp.vol_ratio,
            })
        return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# 2. Structure Analyzer — BOS & CHoCH
# ═══════════════════════════════════════════════════════════════════

class StructureAnalyzer:
    """
    Labels Break of Structure (BOS) and Change of Character (CHoCH)
    after each detected impulse.

    BOS:   price continues past the impulse extreme (trend continuation)
    CHoCH: price breaks the opposite side of the impulse (trend reversal signal)
    The first event that occurs wins.
    """

    def label(
        self,
        impulses: List[Impulse],
        df:       pd.DataFrame,
        max_bars: int = 200,
    ) -> List[StructureLabel]:
        df = df.copy(); df.columns = [c.lower() for c in df.columns]
        labels: List[StructureLabel] = []

        for imp in impulses:
            after = df[df.index > imp.end_ts].iloc[:max_bars]
            if after.empty:
                continue

            if imp.direction == Direction.BULLISH:
                bos_mask   = after['high'] > imp.high
                choch_mask = after['low']  < imp.low
                bos_ref    = imp.high;  choch_ref = imp.low
                bos_col    = 'high';    choch_col = 'low'
            else:
                bos_mask   = after['low']  < imp.low
                choch_mask = after['high'] > imp.high
                bos_ref    = imp.low;   choch_ref = imp.high
                bos_col    = 'low';     choch_col = 'high'

            bos_ts   = after[bos_mask].index[0]   if bos_mask.any()   else None
            choch_ts = after[choch_mask].index[0]  if choch_mask.any() else None

            if bos_ts and (not choch_ts or bos_ts <= choch_ts):
                labels.append(StructureLabel(
                    event        = StructureEvent.BOS,
                    reference_ts = imp.end_ts,
                    reference_px = bos_ref,
                    broken_ts    = bos_ts,
                    broken_px    = float(df.loc[bos_ts, bos_col]),
                ))
            elif choch_ts:
                labels.append(StructureLabel(
                    event        = StructureEvent.CHOCH,
                    reference_ts = imp.end_ts,
                    reference_px = choch_ref,
                    broken_ts    = choch_ts,
                    broken_px    = float(df.loc[choch_ts, choch_col]),
                ))

        return labels

    def to_dataframe(self, labels: List[StructureLabel]) -> pd.DataFrame:
        return pd.DataFrame([{
            'event':        lbl.event.value,
            'reference_ts': lbl.reference_ts,
            'reference_px': lbl.reference_px,
            'broken_ts':    lbl.broken_ts,
            'broken_px':    lbl.broken_px,
        } for lbl in labels])


# ═══════════════════════════════════════════════════════════════════
# 3. Liquidity Sweep Detector
# ═══════════════════════════════════════════════════════════════════

class SweepDetector:
    """
    Finds liquidity sweeps: price briefly violates a prior swing level
    and immediately closes back on the other side (the rejection is the signal).

    Parameters
    ----------
    lookback_bars  : how many bars back to search for swing levels to sweep
    min_wick_ratio : minimum (wick_beyond_level / candle_range) to count
    """

    def __init__(self, lookback_bars: int = 50, min_wick_ratio: float = 0.25):
        self.lookback_bars  = lookback_bars
        self.min_wick_ratio = min_wick_ratio

    def detect(
        self,
        df:     pd.DataFrame,
        swings: List[SwingPoint],
    ) -> List[LiquiditySweep]:
        df = df.copy(); df.columns = [c.lower() for c in df.columns]
        sweeps: List[LiquiditySweep] = []
        idx_map = {ts: i for i, ts in enumerate(df.index)}

        for ts, row in df.iterrows():
            bar_i = idx_map[ts]
            if bar_i < self.lookback_bars:
                continue

            hi  = float(row['high'])
            lo  = float(row['low'])
            cl  = float(row['close'])
            rng = hi - lo
            if rng < 1e-9:
                continue

            # Check against all swing points in the lookback window
            for sp in swings:
                age = bar_i - sp.idx
                if age <= 0 or age > self.lookback_bars:
                    continue

                if sp.kind == 'low':
                    # Bearish sweep: wick goes below swing low, candle closes above
                    if lo < sp.price and cl > sp.price:
                        wick = (sp.price - lo) / rng
                        if wick >= self.min_wick_ratio:
                            sweeps.append(LiquiditySweep(
                                direction   = Direction.BEARISH,
                                ts          = ts,
                                swept_level = sp.price,
                                close_px    = cl,
                                wick_ratio  = round(wick, 3),
                            ))

                else:  # 'high'
                    # Bullish sweep: wick goes above swing high, candle closes below
                    if hi > sp.price and cl < sp.price:
                        wick = (hi - sp.price) / rng
                        if wick >= self.min_wick_ratio:
                            sweeps.append(LiquiditySweep(
                                direction   = Direction.BULLISH,
                                ts          = ts,
                                swept_level = sp.price,
                                close_px    = cl,
                                wick_ratio  = round(wick, 3),
                            ))

        return sweeps

    def to_dataframe(self, sweeps: List[LiquiditySweep]) -> pd.DataFrame:
        return pd.DataFrame([{
            'direction':   s.direction.value,
            'ts':          s.ts,
            'swept_level': s.swept_level,
            'close_px':    s.close_px,
            'wick_ratio':  s.wick_ratio,
        } for s in sweeps])


# ═══════════════════════════════════════════════════════════════════
# 4. Outcome Statistics
# ═══════════════════════════════════════════════════════════════════

class OutcomeStatistics:
    """
    Measures what happened in the N bars AFTER each impulse ended.

    Metrics per impulse:
    - MFE  : max favorable excursion (continuation direction)
    - MAE  : max adverse excursion   (against impulse direction)
    - continued : price broke past impulse extreme within fwd_window
    - first_fib : first Fibonacci retracement level price touched
    - fwd_ret   : percentage return at the end of the forward window

    Summary statistics are printed grouped by direction and score bucket.
    """

    FIB_CHECK = [0.236, 0.382, 0.500, 0.618, 0.705, 0.786, 1.000]

    def __init__(self, fwd_window: int = 50):
        self.fwd_window = fwd_window

    def compute(
        self,
        impulses: List[Impulse],
        df:       pd.DataFrame,
    ) -> pd.DataFrame:
        df = df.copy(); df.columns = [c.lower() for c in df.columns]
        records = []

        for imp in impulses:
            after = df[df.index > imp.end_ts].iloc[: self.fwd_window]
            if after.empty:
                continue

            span = imp.high - imp.low
            if span < 1e-9:
                continue

            if imp.direction == Direction.BULLISH:
                mfe       = (after['high'].max() - imp.end_price) / imp.end_price * 100
                mae       = (imp.end_price - after['low'].min())  / imp.end_price * 100
                continued = int(after['high'].max() > imp.high)
                fwd_ret   = (after['close'].iloc[-1] - imp.end_price) / imp.end_price * 100
                first_fib = next(
                    (lvl for lvl in self.FIB_CHECK
                     if (after['low'] <= imp.high - lvl * span).any()),
                    None
                )
            else:
                mfe       = (imp.end_price - after['low'].min())  / imp.end_price * 100
                mae       = (after['high'].max() - imp.end_price) / imp.end_price * 100
                continued = int(after['low'].min() < imp.low)
                fwd_ret   = (imp.end_price - after['close'].iloc[-1]) / imp.end_price * 100
                first_fib = next(
                    (lvl for lvl in self.FIB_CHECK
                     if (after['high'] >= imp.low + lvl * span).any()),
                    None
                )

            records.append({
                'direction':   imp.direction.value,
                'start_ts':    imp.start_ts,
                'end_ts':      imp.end_ts,
                'pct_move':    imp.pct_move,
                'n_candles':   imp.n_candles,
                'score':       imp.score,
                'n_conditions':len(imp.conditions),
                'mfe_pct':     round(mfe, 3),
                'mae_pct':     round(mae, 3),
                'continued':   continued,
                'first_fib':   first_fib,
                'fwd_ret_pct': round(fwd_ret, 3),
                'vol_ratio':   imp.vol_ratio,
            })

        df_out = pd.DataFrame(records)
        if not df_out.empty:
            self._print_summary(df_out)
        return df_out

    def _print_summary(self, df: pd.DataFrame) -> None:
        print("\n══ OUTCOME STATISTICS ══════════════════════════════════════════")
        for d in ['bullish', 'bearish']:
            sub = df[df['direction'] == d]
            if sub.empty:
                continue
            print(f"\n  {d.upper()} impulses: {len(sub)}")
            print(f"    Continuation rate : {sub['continued'].mean()*100:.1f}%")
            print(f"    Avg MFE           : {sub['mfe_pct'].mean():.2f}%")
            print(f"    Avg MAE           : {sub['mae_pct'].mean():.2f}%")
            print(f"    Avg fwd return    : {sub['fwd_ret_pct'].mean():.2f}%")
            if sub['first_fib'].notna().any():
                fc = sub['first_fib'].value_counts(normalize=True).head(4)
                print(f"    First Fib touched : {fc.to_dict()}")

        # Break down by score bucket
        print("\n  CONTINUATION RATE BY SCORE (all directions):")
        df['score_bucket'] = pd.cut(df['score'], bins=[0, 0.57, 0.71, 0.86, 1.01],
                                     labels=['4/7', '5/7', '6/7', '7/7'])
        grouped = df.groupby('score_bucket', observed=True)['continued'].agg(['mean', 'count'])
        for bucket, row in grouped.iterrows():
            print(f"    {bucket} conditions: {row['mean']*100:.1f}% continuation  (n={int(row['count'])})")
        print("════════════════════════════════════════════════════════════════")


# ═══════════════════════════════════════════════════════════════════
# 5. Full Pipeline wrapper
# ═══════════════════════════════════════════════════════════════════

class MarketStructurePipeline:
    """
    Convenience wrapper that runs the full pipeline in one call.

    Usage
    -----
    ms = MarketStructurePipeline()
    result = ms.run(df)

    result keys:
        'impulses'  : List[Impulse]
        'swings'    : List[SwingPoint]
        'structure' : List[StructureLabel]
        'sweeps'    : List[LiquiditySweep]
        'outcomes'  : pd.DataFrame
        'df_impulses': pd.DataFrame
        'df_structure': pd.DataFrame
        'df_sweeps' : pd.DataFrame
    """

    def __init__(
        self,
        engine_kwargs:  dict = None,
        fwd_window:     int  = 50,
        sweep_lookback: int  = 50,
    ):
        self.engine   = ImpulseEngine(**(engine_kwargs or {}))
        self.analyzer = StructureAnalyzer()
        self.sweeper  = SweepDetector(lookback_bars=sweep_lookback)
        self.stats    = OutcomeStatistics(fwd_window=fwd_window)

    def run(self, df: pd.DataFrame) -> dict:
        df = df.copy(); df.columns = [c.lower() for c in df.columns]

        swings   = self.engine.swing_points(df)
        impulses = self.engine.detect(df)
        labels   = self.analyzer.label(impulses, df)
        sweeps   = self.sweeper.detect(df, swings)
        outcomes = self.stats.compute(impulses, df)

        return {
            'impulses':     impulses,
            'swings':       swings,
            'structure':    labels,
            'sweeps':       sweeps,
            'outcomes':     outcomes,
            'df_impulses':  self.engine.to_dataframe(impulses),
            'df_structure': self.analyzer.to_dataframe(labels),
            'df_sweeps':    self.sweeper.to_dataframe(sweeps),
        }


# ═══════════════════════════════════════════════════════════════════
# Demo — run against uploaded XAUUSD 1H data
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import os

    UPLOAD_DIR = '/root/.claude/uploads/72260b9c-d25d-5454-aecb-8b4ff1e6383e'
    F1H = os.path.join(UPLOAD_DIR, '56b206c2-XAU_1h_data.csv')

    print("Loading XAUUSD 1H data …")
    raw = pd.read_csv(F1H, sep=';', header=0, names=['ts', 'open', 'high', 'low', 'close', 'volume'])
    raw['ts'] = pd.to_datetime(raw['ts'], format='%Y.%m.%d %H:%M', utc=True)
    raw.set_index('ts', inplace=True)

    # Use recent 3 years for a quick demo (still 26,000+ bars)
    df = raw['2023-01-01':]
    print(f"  Bars: {len(df):,}  ({df.index[0].date()} → {df.index[-1].date()})")

    ms = MarketStructurePipeline(
        engine_kwargs={'swing_lookback': 5, 'min_atr_mult': 1.5, 'min_score': 0.57},
        fwd_window=100,
    )

    print("\nRunning pipeline …")
    result = ms.run(df)

    di = result['df_impulses']
    ds = result['df_structure']
    dw = result['df_sweeps']

    print(f"\n── Impulses detected: {len(di)}")
    if not di.empty:
        b = di[di['direction'] == 'bullish']
        be = di[di['direction'] == 'bearish']
        print(f"   Bullish: {len(b)}   Bearish: {len(be)}")
        print(f"   Avg score:    {di['score'].mean():.3f}")
        print(f"   Avg % move:   {di['pct_move'].mean():.2f}%")
        print(f"   Avg candles:  {di['n_candles'].mean():.1f}")
        print(f"\n── Last 5 impulses:")
        pd.set_option('display.width', 120)
        cols = ['direction', 'start_ts', 'end_ts', 'pct_move', 'n_candles', 'score',
                'fib_0.5', 'fib_0.618', 'fib_0.786']
        print(di[cols].tail(5).to_string(index=False))

    print(f"\n── Structure labels: {len(ds)}")
    if not ds.empty:
        print(ds['event'].value_counts().to_string())
        print(f"\n── Last 5 labels:")
        print(ds.tail(5).to_string(index=False))

    print(f"\n── Liquidity sweeps: {len(dw)}")
    if not dw.empty:
        print(dw['direction'].value_counts().to_string())

    # Outcome statistics are printed inside stats.compute()
    print("\nDone.")
