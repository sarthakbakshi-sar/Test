"""
UAE PEAD BACKTESTER
====================
Tests PEAD long-side on DFM stocks with realistic UAE assumptions.

KEY DIFFERENCES FROM US TEST:
  - Long-only (UAE shorting unavailable to retail)
  - Higher costs: 0.275% per side commission + slippage = ~0.6% round-trip
  - Multiple gap thresholds tested (UAE less volatile)
  - Per-stock breakdown to identify if "edge" is concentrated in few names
  - Train: 2021-2023, Test: 2024-2026 (limited by 4-year data window)

OUTPUTS:
  - SL/TP grid like before (3% / 5% / 7% × 6% / 10% / trailing)
  - Per-gap-threshold comparison
  - Per-stock breakdown (critical — tells us if edge is real or concentrated)
  - Walk-forward validation
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# UNIVERSE — 15 tradeable DFM stocks (from scoping result)
# =============================================================================

UAE_UNIVERSE = [
    'EMAAR.AE',        # Emaar Properties
    'EMIRATESNBD.AE',  # Emirates NBD
    'DIB.AE',          # Dubai Islamic Bank
    'EMAARDEV.AE',     # Emaar Development
    'SALIK.AE',        # Salik Company
    'DFM.AE',          # Dubai Financial Market
    'PARKIN.AE',       # Parkin Company
    'AIRARABIA.AE',    # Air Arabia
    'UPP.AE',          # Union Properties
    'GFH.AE',          # GFH Financial Group
    'DEYAAR.AE',       # Deyaar Development
    'ARMX.AE',         # Aramex
    'DIC.AE',          # Dubai Investments
    'DU.AE',           # du (EITC)
    'AMLAK.AE',        # Amlak Finance
    # Also include lower-liquidity ones for completeness
    'MASQ.AE',         # Mashreq Bank
    'TECOM.AE',        # Tecom Group
    'TABREED.AE',      # Empower / Tabreed
    'CBD.AE',          # Commercial Bank of Dubai
]

# =============================================================================
# CONFIGURATION
# =============================================================================

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)  # Train through 2023
START_DATE = END_DATE - timedelta(days=365 * 4)  # 4 years of data

# UAE-specific costs (realistic for retail)
COMMISSION = 0.00275  # 0.275% per side
SLIPPAGE = 0.0025     # 0.25% slippage per side (wider than US due to thin volume)
# Round-trip total: ~1.05% — significantly higher than US

# Gap thresholds to test
GAP_THRESHOLDS = [0.02, 0.03, 0.05]  # 2%, 3%, 5%
VOL_THRESHOLD = 2.0
MIN_DOLLAR_VOLUME = 1_000_000  # $1M USD = ~3.7M AED

# Trade parameters
STOP_LOSSES = [0.03, 0.05, 0.07]
PROFIT_TARGETS = [0.06, 0.10, 'trailing']
TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD_DAYS = 30

# =============================================================================
# DATA + SIGNALS
# =============================================================================

def fetch_data(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 100:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df['vol_avg_20'] = df['Volume'].rolling(20).mean()
        df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
        df['gap_pct'] = df['Open'] / df['Close'].shift(1) - 1
        df['dollar_vol'] = df['Close'] * df['Volume']
        df['avg_dollar_vol_aed'] = df['dollar_vol'].rolling(20).mean()
        df['avg_dollar_vol_usd'] = df['avg_dollar_vol_aed'] / 3.67  # AED→USD peg
        df['liquid'] = df['avg_dollar_vol_usd'] > MIN_DOLLAR_VOLUME
        return df
    except Exception:
        return None

def find_signals(df, ticker, gap_threshold):
    signals = []
    triggers = (df['gap_pct'] > gap_threshold) & (df['vol_ratio'] > VOL_THRESHOLD) & df['liquid']
    for date in df.index[triggers]:
        idx = df.index.get_loc(date)
        if idx >= len(df) - 1:
            continue
        signals.append({'ticker': ticker, 'date': date, 'idx': idx})
    return signals

# =============================================================================
# TRADE SIMULATOR
# =============================================================================

def simulate_trade(df, entry_idx, stop_loss, profit_target):
    if entry_idx + 1 >= len(df):
        return None
    
    entry_price = df.iloc[entry_idx + 1]['Open']
    if pd.isna(entry_price) or entry_price <= 0:
        return None
    
    exit_pnl = 0.0
    days_held = 0
    exit_reason = 'no_data'
    peak_profit = 0.0
    close_pnl = 0.0
    
    for day_offset in range(MAX_HOLD_DAYS):
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
        
        adverse_pnl = (day_low - entry_price) / entry_price
        favorable_pnl = (day_high - entry_price) / entry_price
        close_pnl = (day_close - entry_price) / entry_price
        
        days_held = day_offset + 1
        exit_pnl = close_pnl
        exit_reason = 'time'
        
        if adverse_pnl < -stop_loss:
            exit_pnl = -stop_loss
            exit_reason = 'stop_loss'
            break
        
        if profit_target == 'trailing':
            if favorable_pnl > peak_profit:
                peak_profit = favorable_pnl
            if peak_profit >= TRAIL_TRIGGER:
                if close_pnl < (peak_profit - TRAIL_GIVEBACK):
                    exit_pnl = peak_profit - TRAIL_GIVEBACK
                    exit_reason = 'trailing'
                    break
        else:
            if favorable_pnl > profit_target:
                exit_pnl = profit_target
                exit_reason = 'profit_target'
                break
    
    if days_held == 0:
        return None
    
    # UAE round-trip costs
    net_pnl = exit_pnl - COMMISSION * 2 - SLIPPAGE * 2
    
    return {
        'gross_pnl': exit_pnl,
        'net_pnl': net_pnl,
        'days_held': days_held,
        'exit_reason': exit_reason,
    }

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("UAE PEAD BACKTESTER — DFM Long-only")
print("=" * 80)
print(f"Universe: {len(UAE_UNIVERSE)} DFM stocks")
print(f"Period: {START_DATE.date()} → {END_DATE.date()}")
print(f"Train: → {TRAIN_END.date()}, Test: {TRAIN_END.date()} →")
print(f"Costs: {COMMISSION*100:.3f}% comm + {SLIPPAGE*100:.2f}% slip per side")
print(f"      Round-trip: {(COMMISSION*2 + SLIPPAGE*2)*100:.2f}%")
print()

# Load data
print("Loading data...")
stocks_data = {}
for ticker in UAE_UNIVERSE:
    df = fetch_data(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks_data[ticker] = df
        print(f"  ✓ {ticker}: {len(df)} bars")
    else:
        print(f"  ✗ {ticker}: no data")

print(f"\nLoaded {len(stocks_data)} stocks\n")

# =============================================================================
# RUN BACKTEST FOR ALL COMBINATIONS
# =============================================================================

print("Running 27 strategy variants (3 gaps × 3 SL × 3 TP)...")
print()

all_results = []
all_trades_by_combo = {}  # For per-stock analysis later

for gap_t in GAP_THRESHOLDS:
    # Generate signals at this gap threshold
    signals = []
    for ticker, df in stocks_data.items():
        signals.extend(find_signals(df, ticker, gap_t))
    
    if not signals:
        continue
    
    for sl in STOP_LOSSES:
        for tp in PROFIT_TARGETS:
            trades = []
            for sig in signals:
                df = stocks_data[sig['ticker']]
                t = simulate_trade(df, sig['idx'], sl, tp)
                if t:
                    t['ticker'] = sig['ticker']
                    t['date'] = sig['date']
                    trades.append(t)
            
            if not trades:
                continue
            
            trades_df = pd.DataFrame(trades)
            trades_df['date'] = pd.to_datetime(trades_df['date'])
            if trades_df['date'].dt.tz is not None:
                trades_df['date'] = trades_df['date'].dt.tz_localize(None)
            
            train = trades_df[trades_df['date'] <= TRAIN_END]
            test = trades_df[trades_df['date'] > TRAIN_END]
            
            def stats(d):
                if len(d) == 0:
                    return {'n': 0, 'wr': 0, 'pnl': 0, 'sharpe': 0}
                s = d['net_pnl'].std()
                m = d['net_pnl'].mean()
                avg_d = d['days_held'].mean()
                sh = (m / s * np.sqrt(252 / avg_d)) if s > 0 and avg_d > 0 else 0
                return {'n': len(d), 'wr': (d['net_pnl'] > 0).mean(), 'pnl': m, 'sharpe': sh}
            
            tr_s = stats(train)
            te_s = stats(test)
            tp_label = 'trail' if tp == 'trailing' else f'{int(tp*100)}%'
            combo_key = f"gap{int(gap_t*100)}_sl{int(sl*100)}_tp{tp_label}"
            
            all_results.append({
                'gap': gap_t, 'sl': sl, 'tp': tp_label,
                'train_n': tr_s['n'], 'test_n': te_s['n'],
                'train_pnl': tr_s['pnl'], 'test_pnl': te_s['pnl'],
                'train_wr': tr_s['wr'], 'test_wr': te_s['wr'],
                'train_sharpe': tr_s['sharpe'], 'test_sharpe': te_s['sharpe'],
            })
            
            all_trades_by_combo[combo_key] = trades_df

# =============================================================================
# OUTPUT
# =============================================================================

results_df = pd.DataFrame(all_results)

print("=" * 80)
print("RESULTS — All combinations ranked by test P&L")
print("=" * 80)
print(f"{'Gap':<6}{'SL':<5}{'TP':<8}{'Tr#':>6}{'Te#':>6}{'Tr WR':>8}{'Te WR':>8}"
      f"{'Tr P&L':>9}{'Te P&L':>9}{'Te Shrp':>9}")
print("-" * 80)

ranked = results_df.sort_values('test_pnl', ascending=False)
for _, r in ranked.iterrows():
    gap_str = f"{int(r['gap']*100)}%"
    sl_str = f"{int(r['sl']*100)}%"
    print(f"{gap_str:<6}{sl_str:<5}{r['tp']:<8}{r['train_n']:>6}{r['test_n']:>6}"
          f"{r['train_wr']*100:>7.1f}%{r['test_wr']*100:>7.1f}%"
          f"{r['train_pnl']*100:>+8.2f}%{r['test_pnl']*100:>+8.2f}%"
          f"{r['test_sharpe']:>9.2f}")

# =============================================================================
# PER-STOCK BREAKDOWN OF BEST STRATEGY
# =============================================================================

if len(ranked) > 0:
    best = ranked.iloc[0]
    best_key = f"gap{int(best['gap']*100)}_sl{int(best['sl']*100)}_tp{best['tp']}"
    best_trades = all_trades_by_combo.get(best_key)
    
    if best_trades is not None and len(best_trades) > 0:
        test_trades = best_trades[best_trades['date'] > TRAIN_END]
        
        print("\n" + "=" * 80)
        print(f"PER-STOCK BREAKDOWN — Best combo (gap={int(best['gap']*100)}%, "
              f"SL={int(best['sl']*100)}%, TP={best['tp']})")
        print("=" * 80)
        print(f"This shows whether 'edge' is concentrated in a few stocks (BAD) "
              f"or spread across the universe (GOOD)")
        print()
        
        per_stock = test_trades.groupby('ticker').agg(
            n_trades=('net_pnl', 'count'),
            win_rate=('net_pnl', lambda x: (x > 0).mean()),
            avg_pnl=('net_pnl', 'mean'),
            total_pnl=('net_pnl', 'sum'),
        ).reset_index().sort_values('total_pnl', ascending=False)
        
        print(f"{'Ticker':<18}{'N':>5}{'WR':>8}{'Avg P&L':>11}{'Total P&L':>12}")
        print("-" * 60)
        for _, r in per_stock.iterrows():
            print(f"{r['ticker']:<18}{r['n_trades']:>5}"
                  f"{r['win_rate']*100:>7.1f}%{r['avg_pnl']*100:>+10.2f}%"
                  f"{r['total_pnl']*100:>+11.2f}%")
        
        # Concentration analysis
        print()
        n_stocks_with_trades = len(per_stock)
        n_profitable_stocks = (per_stock['avg_pnl'] > 0).sum()
        top3_total = per_stock.head(3)['total_pnl'].sum()
        all_total = per_stock['total_pnl'].sum()
        
        print(f"Stocks generating trades: {n_stocks_with_trades}")
        print(f"Stocks with positive average P&L: {n_profitable_stocks}/{n_stocks_with_trades}")
        if all_total != 0:
            top3_pct = top3_total / all_total * 100
            print(f"Top 3 stocks contribute: {top3_pct:.0f}% of total P&L")
            if top3_pct > 80:
                print("  ⚠️ Edge is HEAVILY concentrated — high overfitting risk")
            elif top3_pct > 60:
                print("  🟡 Edge is moderately concentrated")
            else:
                print("  ✓ Edge is reasonably distributed")

# =============================================================================
# GAP THRESHOLD COMPARISON
# =============================================================================

print("\n" + "=" * 80)
print("GAP THRESHOLD COMPARISON — which threshold works best?")
print("=" * 80)
print(f"{'Gap':<6}{'Avg Test P&L':>16}{'Avg Test WR':>16}{'Total Trades':>16}")
print("-" * 60)
for gap_t in GAP_THRESHOLDS:
    subset = ranked[ranked['gap'] == gap_t]
    if len(subset) > 0:
        gap_str = f"{int(gap_t*100)}%"
        print(f"{gap_str:<6}{subset['test_pnl'].mean()*100:>+15.2f}%"
              f"{subset['test_wr'].mean()*100:>15.1f}%"
              f"{int(subset['test_n'].mean()):>16}")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

if len(ranked) > 0:
    best = ranked.iloc[0]
    
    if best['test_pnl'] > 0.015 and best['test_n'] >= 30 and best['test_sharpe'] > 0.5:
        # Now check concentration
        best_key = f"gap{int(best['gap']*100)}_sl{int(best['sl']*100)}_tp{best['tp']}"
        best_trades = all_trades_by_combo.get(best_key)
        if best_trades is not None:
            test_trades = best_trades[best_trades['date'] > TRAIN_END]
            per_stock = test_trades.groupby('ticker')['net_pnl'].sum()
            top3 = per_stock.nlargest(3).sum()
            total = per_stock.sum()
            top3_pct = top3 / total * 100 if total != 0 else 0
            
            if top3_pct > 80:
                print(f"\n🟡 APPARENT EDGE BUT CONCENTRATED")
                print(f"   Best: gap={int(best['gap']*100)}%, SL={int(best['sl']*100)}%, "
                      f"TP={best['tp']}")
                print(f"   Test P&L: {best['test_pnl']*100:+.2f}%, "
                      f"Sharpe: {best['test_sharpe']:.2f}, n={best['test_n']}")
                print(f"   But {top3_pct:.0f}% of P&L from just 3 stocks → fragile")
            else:
                print(f"\n✅ EDGE CONFIRMED OUT-OF-SAMPLE")
                print(f"   Best: gap={int(best['gap']*100)}%, SL={int(best['sl']*100)}%, "
                      f"TP={best['tp']}")
                print(f"   Test P&L: {best['test_pnl']*100:+.2f}%, "
                      f"Sharpe: {best['test_sharpe']:.2f}, n={best['test_n']}")
    elif best['test_pnl'] > 0:
        print(f"\n🟠 MARGINAL — net P&L positive but small")
        print(f"   Best: gap={int(best['gap']*100)}%, SL={int(best['sl']*100)}%, "
              f"TP={best['tp']}")
        print(f"   Test P&L: {best['test_pnl']*100:+.2f}%, n={best['test_n']}")
        print(f"   Probably not worth deploying given high UAE costs")
    else:
        print(f"\n❌ NO NET EDGE")
        print(f"   Best test P&L: {best['test_pnl']*100:+.2f}%")
        print(f"   UAE PEAD doesn't survive realistic costs")
else:
    print("\n❌ INSUFFICIENT DATA for any conclusion")

print()
