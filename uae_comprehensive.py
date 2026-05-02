"""
UAE COMPREHENSIVE STRATEGY TEST
================================
Tests 7 distinct anomalies on UAE/DFM stocks with rigorous validation.

STRATEGIES:
  1. PEAD LONG: gap-up + volume → continuation
  2. MEAN REVERSION: short-term oversold bounce
  3. MOMENTUM: multi-day trend following
  4. VOLUME BREAKOUT: range break on high volume
  5. RAMADAN OVERLAY: calendar-based bias
  6. SUNDAY EFFECT: Sunday open anomaly (UAE-specific)
  7. GAP REVERSAL: short overheated gaps (mean revert)

EACH STRATEGY:
  - Multiple parameter variants
  - Walk-forward validated (train 2022-2023, test 2024-2026)
  - Per-stock concentration check
  - UAE-realistic cost model (1.05% round-trip)

ACCEPTANCE CRITERIA (pre-committed before viewing results):
  - Positive net P&L on BOTH train AND test
  - Test sample >= 50 trades
  - Test Sharpe >= 0.8
  - Top-3-stock concentration < 60% of total P&L
  - Train/test P&L decay < 50%

Strategies failing ANY criterion are rejected, even if test P&L looks great.
This is the discipline that separates real edge from data-mining artifacts.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# UNIVERSE
# =============================================================================

UAE_UNIVERSE = [
    'EMAAR.AE', 'EMIRATESNBD.AE', 'DIB.AE', 'EMAARDEV.AE', 'SALIK.AE',
    'DFM.AE', 'PARKIN.AE', 'AIRARABIA.AE', 'UPP.AE', 'GFH.AE',
    'DEYAAR.AE', 'ARMX.AE', 'DIC.AE', 'DU.AE', 'AMLAK.AE',
    'MASQ.AE', 'TECOM.AE', 'TABREED.AE', 'CBD.AE',
]

# =============================================================================
# CONFIG
# =============================================================================

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 4)

# UAE retail costs
COMMISSION = 0.00275
SLIPPAGE = 0.0025
ROUND_TRIP_COST = COMMISSION * 2 + SLIPPAGE * 2  # 1.05%

MIN_DOLLAR_VOLUME = 1_000_000

# Pre-committed acceptance criteria
class Criteria:
    MIN_TEST_TRADES = 50
    MIN_TEST_SHARPE = 0.8
    MAX_CONCENTRATION = 0.60  # Top 3 stocks < 60% of P&L
    MAX_DECAY = 0.50  # Train→test P&L decay < 50%
    REQUIRE_BOTH_POSITIVE = True

# Ramadan dates (approximate, for backtest)
RAMADAN_PERIODS = [
    ('2022-04-02', '2022-05-02'),
    ('2023-03-22', '2023-04-21'),
    ('2024-03-10', '2024-04-09'),
    ('2025-02-28', '2025-03-30'),
    ('2026-02-17', '2026-03-19'),
]

# =============================================================================
# DATA
# =============================================================================

def fetch_data(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 100:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df['ret_1d'] = df['Close'].pct_change(1)
        df['ret_3d'] = df['Close'].pct_change(3)
        df['ret_5d'] = df['Close'].pct_change(5)
        df['vol_avg_20'] = df['Volume'].rolling(20).mean()
        df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
        df['gap_pct'] = df['Open'] / df['Close'].shift(1) - 1
        df['high_20d'] = df['Close'].rolling(20).max()
        df['low_20d'] = df['Close'].rolling(20).min()
        df['sma_20'] = df['Close'].rolling(20).mean()
        df['sma_50'] = df['Close'].rolling(50).mean()
        delta = df['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + gain / loss))
        df['avg_dollar_vol_usd'] = (df['Close'] * df['Volume']).rolling(20).mean() / 3.67
        df['liquid'] = df['avg_dollar_vol_usd'] > MIN_DOLLAR_VOLUME
        return df
    except Exception:
        return None

def in_ramadan(date):
    d = pd.Timestamp(date).normalize()
    if d.tz is not None:
        d = d.tz_localize(None)
    for start, end in RAMADAN_PERIODS:
        if pd.Timestamp(start) <= d <= pd.Timestamp(end):
            return True
    return False

# =============================================================================
# STRATEGIES (each returns list of trade signals)
# =============================================================================

def strat_pead_long(df, ticker, params):
    """Gap up + volume → continuation. Same as our prior test."""
    triggers = (df['gap_pct'] > params['gap']) & (df['vol_ratio'] > 2.0) & df['liquid']
    return [{'ticker': ticker, 'date': d, 'idx': df.index.get_loc(d), 'side': 'long'}
            for d in df.index[triggers]]

def strat_mean_reversion(df, ticker, params):
    """Stock dropped X% in 3 days, RSI < 30, looking for bounce."""
    triggers = ((df['ret_3d'] < -params['drop']) & (df['rsi'] < 30) & 
                (df['Close'] > df['sma_50']) & df['liquid'])
    return [{'ticker': ticker, 'date': d, 'idx': df.index.get_loc(d), 'side': 'long'}
            for d in df.index[triggers]]

def strat_momentum(df, ticker, params):
    """Multi-day uptrend continuation. Above 50d SMA, 5-day return > X%."""
    triggers = ((df['ret_5d'] > params['threshold']) & 
                (df['Close'] > df['sma_50']) & 
                (df['rsi'] > 50) & df['liquid'])
    return [{'ticker': ticker, 'date': d, 'idx': df.index.get_loc(d), 'side': 'long'}
            for d in df.index[triggers]]

def strat_volume_breakout(df, ticker, params):
    """Close above 20d high on high volume."""
    triggers = ((df['Close'] > df['high_20d'].shift(1)) & 
                (df['vol_ratio'] > params['vol']) & df['liquid'])
    return [{'ticker': ticker, 'date': d, 'idx': df.index.get_loc(d), 'side': 'long'}
            for d in df.index[triggers]]

def strat_ramadan_long(df, ticker, params):
    """Long during first day of Ramadan, hold throughout."""
    signals = []
    for date in df.index:
        if in_ramadan(date):
            d = pd.Timestamp(date).normalize()
            if d.tz is not None:
                d = d.tz_localize(None)
            yesterday = d - pd.Timedelta(days=3)
            yesterday_in_ramadan = False
            for start, end in RAMADAN_PERIODS:
                if pd.Timestamp(start) <= yesterday <= pd.Timestamp(end):
                    yesterday_in_ramadan = True
                    break
            if not yesterday_in_ramadan and df.loc[date, 'liquid']:
                signals.append({'ticker': ticker, 'date': date,
                              'idx': df.index.get_loc(date), 'side': 'long'})
    return signals

def strat_sunday_open(df, ticker, params):
    """Buy at Sunday close (UAE Sun=Mon equivalent), hold N days."""
    signals = []
    for date in df.index:
        if date.dayofweek == 6 and df.loc[date, 'liquid']:  # Sunday
            signals.append({'ticker': ticker, 'date': date,
                          'idx': df.index.get_loc(date), 'side': 'long'})
    return signals

def strat_gap_reversal(df, ticker, params):
    """Short gap-ups (the opposite of PEAD): gap up + closes weak → expect reversion.
    Note: shorting unavailable in UAE retail, but we test for academic interest.
    Could also be implemented as "skip gap up days" filter."""
    triggers = ((df['gap_pct'] > params['gap']) & (df['vol_ratio'] > 2.0) & 
                df['liquid'])
    # Mark as short signals (reversal trade)
    return [{'ticker': ticker, 'date': d, 'idx': df.index.get_loc(d), 'side': 'short'}
            for d in df.index[triggers]]

# =============================================================================
# TRADE SIMULATOR (handles long & short, with stops + trailing)
# =============================================================================

def simulate_trade(df, entry_idx, side, sl, tp, max_hold):
    if entry_idx + 1 >= len(df):
        return None
    entry_price = df.iloc[entry_idx + 1]['Open']
    if pd.isna(entry_price) or entry_price <= 0:
        return None
    
    sign = 1 if side == 'long' else -1
    
    exit_pnl = 0.0
    days_held = 0
    exit_reason = 'no_data'
    peak_profit = 0.0
    close_pnl = 0.0
    
    for day_offset in range(max_hold):
        idx = entry_idx + 1 + day_offset
        if idx >= len(df):
            if day_offset > 0:
                exit_pnl = close_pnl
                days_held = day_offset
                exit_reason = 'no_data'
            break
        
        day_high = df.iloc[idx]['High']
        day_low = df.iloc[idx]['Low']
        day_close = df.iloc[idx]['Close']
        
        if side == 'long':
            adverse = (day_low - entry_price) / entry_price
            favorable = (day_high - entry_price) / entry_price
        else:
            adverse = (entry_price - day_high) / entry_price
            favorable = (entry_price - day_low) / entry_price
        
        close_pnl = sign * (day_close - entry_price) / entry_price
        
        days_held = day_offset + 1
        exit_pnl = close_pnl
        exit_reason = 'time'
        
        if adverse < -sl:
            exit_pnl = -sl
            exit_reason = 'stop_loss'
            break
        
        if tp == 'trailing':
            if favorable > peak_profit:
                peak_profit = favorable
            if peak_profit >= 0.05:
                if close_pnl < (peak_profit - 0.03):
                    exit_pnl = peak_profit - 0.03
                    exit_reason = 'trailing'
                    break
        else:
            if favorable > tp:
                exit_pnl = tp
                exit_reason = 'profit_target'
                break
    
    if days_held == 0:
        return None
    
    net_pnl = exit_pnl - ROUND_TRIP_COST
    return {'gross_pnl': exit_pnl, 'net_pnl': net_pnl,
            'days_held': days_held, 'exit_reason': exit_reason}

# =============================================================================
# RUN A STRATEGY VARIANT
# =============================================================================

def run_strategy(stocks_data, strat_func, strat_params, sl, tp, max_hold, label):
    """Run one strategy variant across the universe and return stats"""
    all_trades = []
    
    for ticker, df in stocks_data.items():
        signals = strat_func(df, ticker, strat_params)
        for sig in signals:
            t = simulate_trade(df, sig['idx'], sig['side'], sl, tp, max_hold)
            if t:
                t['ticker'] = ticker
                t['date'] = sig['date']
                all_trades.append(t)
    
    if not all_trades:
        return None
    
    trades_df = pd.DataFrame(all_trades)
    trades_df['date'] = pd.to_datetime(trades_df['date'])
    if trades_df['date'].dt.tz is not None:
        trades_df['date'] = trades_df['date'].dt.tz_localize(None)
    
    train = trades_df[trades_df['date'] <= TRAIN_END]
    test = trades_df[trades_df['date'] > TRAIN_END]
    
    def stats(d):
        if len(d) == 0:
            return {'n': 0, 'wr': 0, 'pnl': 0, 'sharpe': 0, 'std': 0}
        s = d['net_pnl'].std()
        m = d['net_pnl'].mean()
        avg_d = d['days_held'].mean()
        sh = (m / s * np.sqrt(252 / avg_d)) if s > 0 and avg_d > 0 else 0
        return {'n': len(d), 'wr': (d['net_pnl'] > 0).mean(),
                'pnl': m, 'sharpe': sh, 'std': s}
    
    tr_s = stats(train)
    te_s = stats(test)
    
    # Concentration analysis (test set only)
    if len(test) > 0:
        per_stock = test.groupby('ticker')['net_pnl'].sum()
        if per_stock.sum() > 0:
            top3 = per_stock.nlargest(3).sum()
            concentration = top3 / per_stock.sum()
        else:
            concentration = 1.0
        n_stocks = len(per_stock)
        n_pos_stocks = (test.groupby('ticker')['net_pnl'].mean() > 0).sum()
    else:
        concentration = 1.0
        n_stocks = 0
        n_pos_stocks = 0
    
    return {
        'label': label,
        'train_n': tr_s['n'], 'test_n': te_s['n'],
        'train_pnl': tr_s['pnl'], 'test_pnl': te_s['pnl'],
        'train_wr': tr_s['wr'], 'test_wr': te_s['wr'],
        'train_sharpe': tr_s['sharpe'], 'test_sharpe': te_s['sharpe'],
        'concentration': concentration,
        'n_stocks': n_stocks,
        'n_pos_stocks': n_pos_stocks,
    }

# =============================================================================
# CHECK CRITERIA
# =============================================================================

def passes_criteria(r):
    """Pre-committed acceptance criteria"""
    reasons = []
    if r['test_n'] < Criteria.MIN_TEST_TRADES:
        reasons.append(f"test_n {r['test_n']}<{Criteria.MIN_TEST_TRADES}")
    if r['test_pnl'] <= 0:
        reasons.append(f"test P&L not positive ({r['test_pnl']*100:+.2f}%)")
    if r['train_pnl'] <= 0 and Criteria.REQUIRE_BOTH_POSITIVE:
        reasons.append(f"train P&L not positive ({r['train_pnl']*100:+.2f}%)")
    if r['test_sharpe'] < Criteria.MIN_TEST_SHARPE:
        reasons.append(f"Sharpe {r['test_sharpe']:.2f}<{Criteria.MIN_TEST_SHARPE}")
    if r['concentration'] > Criteria.MAX_CONCENTRATION:
        reasons.append(f"top3 conc {r['concentration']*100:.0f}%>{Criteria.MAX_CONCENTRATION*100:.0f}%")
    if r['train_pnl'] > 0 and r['test_pnl'] > 0:
        decay = (r['train_pnl'] - r['test_pnl']) / r['train_pnl']
        if decay > Criteria.MAX_DECAY:
            reasons.append(f"decay {decay*100:.0f}%>{Criteria.MAX_DECAY*100:.0f}%")
    return len(reasons) == 0, reasons

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("UAE COMPREHENSIVE STRATEGY TEST")
print("=" * 80)
print(f"Universe: {len(UAE_UNIVERSE)} DFM stocks")
print(f"Period: {START_DATE.date()} → {END_DATE.date()}")
print(f"Costs: {ROUND_TRIP_COST*100:.2f}% round-trip")
print(f"\nPre-committed acceptance criteria:")
print(f"  • Test trades >= {Criteria.MIN_TEST_TRADES}")
print(f"  • Test Sharpe >= {Criteria.MIN_TEST_SHARPE}")
print(f"  • Top-3 concentration < {Criteria.MAX_CONCENTRATION*100:.0f}%")
print(f"  • Train/test decay < {Criteria.MAX_DECAY*100:.0f}%")
print(f"  • Both train AND test P&L positive")
print()

# Load data
print("Loading data...")
stocks_data = {}
for ticker in UAE_UNIVERSE:
    df = fetch_data(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks_data[ticker] = df
print(f"Loaded {len(stocks_data)} stocks\n")

# Define all strategy variants to test
print("Running all strategy variants...\n")

variants = []

# PEAD long: 3 gaps × 3 SL × 2 TP = 18
for gap in [0.02, 0.03, 0.05]:
    for sl in [0.05, 0.07]:
        for tp in [0.10, 'trailing']:
            label = f"PEAD_long g{int(gap*100)}_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
            variants.append((strat_pead_long, {'gap': gap}, sl, tp, 30, label))

# Mean reversion: 2 drops × 2 SL × 2 TP = 8
for drop in [0.05, 0.08]:
    for sl in [0.05, 0.07]:
        for tp in [0.05, 'trailing']:
            label = f"MeanRev d{int(drop*100)}_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
            variants.append((strat_mean_reversion, {'drop': drop}, sl, tp, 10, label))

# Momentum: 2 thresholds × 2 SL × 2 TP = 8
for threshold in [0.05, 0.08]:
    for sl in [0.05, 0.07]:
        for tp in [0.10, 'trailing']:
            label = f"Mom t{int(threshold*100)}_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
            variants.append((strat_momentum, {'threshold': threshold}, sl, tp, 20, label))

# Volume breakout: 2 vols × 2 SL × 2 TP = 8
for vol in [2.0, 3.0]:
    for sl in [0.05, 0.07]:
        for tp in [0.10, 'trailing']:
            label = f"VolBO v{vol:.0f}x_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
            variants.append((strat_volume_breakout, {'vol': vol}, sl, tp, 20, label))

# Ramadan: 1 variant × 2 SL × 2 TP = 4
for sl in [0.05, 0.10]:
    for tp in [0.10, 'trailing']:
        label = f"Ramadan_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
        variants.append((strat_ramadan_long, {}, sl, tp, 30, label))

# Sunday: hold 1, 3, 5 days
for hold in [1, 3, 5]:
    label = f"Sunday hold{hold}"
    variants.append((strat_sunday_open, {}, 0.10, 'trailing', hold, label))

# Gap reversal (short): 2 gaps × 2 SL × 2 TP = 8
for gap in [0.03, 0.05]:
    for sl in [0.05, 0.07]:
        for tp in [0.05, 'trailing']:
            label = f"GapRev g{int(gap*100)}_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
            variants.append((strat_gap_reversal, {'gap': gap}, sl, tp, 10, label))

print(f"Total variants: {len(variants)}")

results = []
for i, (func, params, sl, tp, hold, label) in enumerate(variants):
    r = run_strategy(stocks_data, func, params, sl, tp, hold, label)
    if r is not None:
        results.append(r)
    if (i+1) % 10 == 0:
        print(f"  [{i+1}/{len(variants)}] done")

print(f"\nCompleted {len(results)} strategy variants")

# =============================================================================
# OUTPUT
# =============================================================================

results_df = pd.DataFrame(results).sort_values('test_pnl', ascending=False)

print("\n" + "=" * 100)
print("RESULTS — top 20 by test P&L")
print("=" * 100)
print(f"{'Strategy':<38}{'Tr#':>5}{'Te#':>5}{'TrP&L':>8}{'TeP&L':>8}"
      f"{'TrWR':>7}{'TeWR':>7}{'Shrp':>7}{'Conc':>7}")
print("-" * 100)

for _, r in results_df.head(20).iterrows():
    print(f"{r['label']:<38}{r['train_n']:>5}{r['test_n']:>5}"
          f"{r['train_pnl']*100:>+7.2f}%{r['test_pnl']*100:>+7.2f}%"
          f"{r['train_wr']*100:>6.1f}%{r['test_wr']*100:>6.1f}%"
          f"{r['test_sharpe']:>7.2f}{r['concentration']*100:>6.0f}%")

# =============================================================================
# APPLY CRITERIA
# =============================================================================

print("\n" + "=" * 100)
print("STRATEGIES PASSING ALL ACCEPTANCE CRITERIA")
print("=" * 100)

passers = []
near_misses = []

for _, r in results_df.iterrows():
    passed, reasons = passes_criteria(r.to_dict())
    if passed:
        passers.append(r.to_dict())
    elif len(reasons) == 1:
        near_misses.append({'r': r.to_dict(), 'reasons': reasons})

if passers:
    print(f"\n✅ {len(passers)} strategy(ies) passed:\n")
    for p in passers:
        print(f"  {p['label']}")
        print(f"    Train: n={p['train_n']}, P&L={p['train_pnl']*100:+.2f}%, "
              f"WR={p['train_wr']*100:.1f}%")
        print(f"    Test:  n={p['test_n']}, P&L={p['test_pnl']*100:+.2f}%, "
              f"WR={p['test_wr']*100:.1f}%, Sharpe={p['test_sharpe']:.2f}")
        print(f"    Concentration: {p['concentration']*100:.0f}% in top 3 stocks")
        print()
else:
    print("\n❌ Zero strategies passed all criteria")
    
    if near_misses:
        print(f"\n{len(near_misses)} strategies missed by ONLY ONE criterion:")
        for nm in near_misses[:5]:
            r = nm['r']
            print(f"\n  {r['label']}")
            print(f"    Failed: {nm['reasons'][0]}")
            print(f"    Stats: train P&L {r['train_pnl']*100:+.2f}%, "
                  f"test P&L {r['test_pnl']*100:+.2f}%, "
                  f"Sharpe {r['test_sharpe']:.2f}, "
                  f"conc {r['concentration']*100:.0f}%")
        print("\n  These are NOT validated edges. They look close but fail discipline.")
        print("  Pursuing them would be exactly the overfitting trap.")

# =============================================================================
# STRATEGY-LEVEL SUMMARY
# =============================================================================

print("\n" + "=" * 80)
print("STRATEGY-LEVEL SUMMARY — average performance per strategy type")
print("=" * 80)

results_df['strat_type'] = results_df['label'].str.split(' ').str[0]
type_summary = results_df.groupby('strat_type').agg(
    n_variants=('label', 'count'),
    avg_test_pnl=('test_pnl', 'mean'),
    best_test_pnl=('test_pnl', 'max'),
    avg_test_sharpe=('test_sharpe', 'mean'),
    avg_concentration=('concentration', 'mean'),
).reset_index().sort_values('avg_test_pnl', ascending=False)

print(f"{'Strategy':<15}{'Variants':>10}{'Avg P&L':>10}{'Best P&L':>11}"
      f"{'Avg Shrp':>10}{'Avg Conc':>11}")
print("-" * 80)
for _, r in type_summary.iterrows():
    print(f"{r['strat_type']:<15}{r['n_variants']:>10}"
          f"{r['avg_test_pnl']*100:>+9.2f}%{r['best_test_pnl']*100:>+10.2f}%"
          f"{r['avg_test_sharpe']:>10.2f}{r['avg_concentration']*100:>10.0f}%")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

if passers:
    print(f"\n✅ {len(passers)} strategy/strategies survived rigorous validation.")
    print("These are the only candidates worth considering for UAE deployment.")
    print("Note: even passing strategies should be paper-traded for 30-60 days")
    print("before deploying real capital.")
else:
    print("\n❌ NO strategies passed all five acceptance criteria.")
    print("\nThis is the answer the data is giving us. UAE markets, as accessed by")
    print("retail traders with realistic costs and Yahoo Finance data quality,")
    print("do not produce systematic edges that survive disciplined validation.")
    print("\nThis doesn't mean UAE markets are 'efficient.' It means the structural")
    print("barriers (high costs, thin liquidity, small sample) prevent retail")
    print("systematic capture of any edge that might exist.")
    print("\nDeploying any of the 'best' results above would be data mining, not")
    print("trading edge. We pre-committed to this discipline. We honor it.")

print()
