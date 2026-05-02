"""
UAE MOMENTUM WITH REGIME FILTER
================================
Tests UAE momentum strategy with a regime filter:
  - Only enter trades when DFM Index is above its 200-day SMA
  - Same momentum entry rules as before (5d return, RSI, trend)
  - Same walk-forward validation
  - Same pre-committed acceptance criteria

The regime filter is a standard, decades-old academic indicator.
It was NOT designed to fix our specific results — it's a generic
"is the market healthy" filter used widely in trend-following.

If the strategy STILL fails after applying this filter, we have
strong evidence the apparent edge was regime-coincident noise.

If the strategy passes, we have evidence of regime-dependent edge
that's still real (since we apply the filter only with past data
available at trade time — no look-ahead bias).
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# CONFIG
# =============================================================================

UAE_UNIVERSE = [
    'EMAAR.AE', 'EMIRATESNBD.AE', 'DIB.AE', 'EMAARDEV.AE', 'SALIK.AE',
    'DFM.AE', 'PARKIN.AE', 'AIRARABIA.AE', 'UPP.AE', 'GFH.AE',
    'DEYAAR.AE', 'ARMX.AE', 'DIC.AE', 'DU.AE', 'AMLAK.AE',
    'MASQ.AE', 'TECOM.AE', 'TABREED.AE', 'CBD.AE',
]

# DFM General Index ticker — broad market proxy for regime
# We try multiple variants since Yahoo is inconsistent
DFM_INDEX_CANDIDATES = ['^DFMGI', 'DFMGI.AE', '^DFM', 'DFM.DU']

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 4)

# Costs
COMMISSION = 0.00275
SLIPPAGE = 0.0025
ROUND_TRIP_COST = COMMISSION * 2 + SLIPPAGE * 2

# Liquidity floor
MIN_DOLLAR_VOLUME = 1_000_000

# Trade exit parameters
STOP_LOSS_PCT = 0.07
TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD_DAYS = 20

# Regime filter parameter (the only "knob" we're using)
REGIME_SMA_PERIOD = 200  # Standard academic choice

# Acceptance criteria (same as before)
class Criteria:
    MIN_TEST_TRADES = 50
    MIN_TEST_SHARPE = 0.8
    MAX_CONCENTRATION = 0.60
    MAX_DECAY = 0.50
    REQUIRE_BOTH_POSITIVE = True

# =============================================================================
# DATA
# =============================================================================

def fetch_stock(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 100:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df['ret_5d'] = df['Close'].pct_change(5)
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

def fetch_dfm_index():
    """Try to fetch DFM General Index. Falls back to constructing from EMAAR if needed."""
    for ticker in DFM_INDEX_CANDIDATES:
        try:
            df = yf.download(ticker, start=START_DATE - timedelta(days=400), 
                           end=END_DATE, progress=False, auto_adjust=True)
            if df is not None and len(df) > 200:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                print(f"  ✓ DFM index fetched via {ticker}")
                df['sma_200'] = df['Close'].rolling(REGIME_SMA_PERIOD).mean()
                df['regime_up'] = df['Close'] > df['sma_200']
                return df, ticker
        except Exception:
            continue
    
    print("  ⚠ DFM index ticker failed; constructing equal-weight composite from universe")
    return None, None

def construct_market_proxy(stocks_data):
    """Build equal-weight market proxy from available UAE stocks (fallback if index fails)"""
    closes = pd.DataFrame({t: df['Close'] for t, df in stocks_data.items()})
    closes = closes.dropna(how='all')
    # Normalize each to start at 100
    normalized = closes.div(closes.iloc[0]) * 100
    composite = normalized.mean(axis=1)
    proxy = pd.DataFrame({'Close': composite})
    proxy['sma_200'] = proxy['Close'].rolling(REGIME_SMA_PERIOD).mean()
    proxy['regime_up'] = proxy['Close'] > proxy['sma_200']
    return proxy

# =============================================================================
# SIGNAL GENERATION (with regime filter applied)
# =============================================================================

def find_momentum_signals(df, ticker, regime_df, threshold):
    """Generate momentum signals only when DFM index regime is up."""
    signals = []
    
    # Pre-compute regime status by date for fast lookup
    regime_lookup = regime_df['regime_up']
    
    triggers = ((df['ret_5d'] > threshold) & 
                (df['Close'] > df['sma_50']) & 
                (df['rsi'] > 50) & 
                df['liquid'])
    
    for date in df.index[triggers]:
        # Look up regime status on the SIGNAL DATE
        # (we use information available at that day's close)
        date_match = regime_lookup.index[regime_lookup.index <= date]
        if len(date_match) == 0:
            continue
        nearest_regime_date = date_match[-1]
        
        # Critical: regime status must be available BEFORE we trade
        # We use the prior day's regime status to be safe (no look-ahead)
        if nearest_regime_date == date:
            # Take the prior available value
            prior_idx = regime_lookup.index.get_loc(nearest_regime_date) - 1
            if prior_idx < 0:
                continue
            in_uptrend = regime_lookup.iloc[prior_idx]
        else:
            in_uptrend = regime_lookup.loc[nearest_regime_date]
        
        if not in_uptrend:
            continue  # Skip — regime is not up
        
        idx = df.index.get_loc(date)
        if idx >= len(df) - 1:
            continue
        signals.append({'ticker': ticker, 'date': date, 'idx': idx})
    
    return signals

# =============================================================================
# TRADE SIMULATOR
# =============================================================================

def simulate_trade(df, entry_idx, sl, tp_type):
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
        
        adverse = (day_low - entry_price) / entry_price
        favorable = (day_high - entry_price) / entry_price
        close_pnl = (day_close - entry_price) / entry_price
        
        days_held = day_offset + 1
        exit_pnl = close_pnl
        exit_reason = 'time'
        
        if adverse < -sl:
            exit_pnl = -sl
            exit_reason = 'stop_loss'
            break
        
        if tp_type == 'trailing':
            if favorable > peak_profit:
                peak_profit = favorable
            if peak_profit >= TRAIL_TRIGGER:
                if close_pnl < (peak_profit - TRAIL_GIVEBACK):
                    exit_pnl = peak_profit - TRAIL_GIVEBACK
                    exit_reason = 'trailing'
                    break
    
    if days_held == 0:
        return None
    
    net_pnl = exit_pnl - ROUND_TRIP_COST
    return {'gross_pnl': exit_pnl, 'net_pnl': net_pnl,
            'days_held': days_held, 'exit_reason': exit_reason}

# =============================================================================
# RUN STRATEGY
# =============================================================================

def run_strategy(stocks_data, regime_df, threshold, sl, tp, label):
    all_trades = []
    for ticker, df in stocks_data.items():
        signals = find_momentum_signals(df, ticker, regime_df, threshold)
        for sig in signals:
            t = simulate_trade(df, sig['idx'], sl, tp)
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
            return {'n': 0, 'wr': 0, 'pnl': 0, 'sharpe': 0}
        s = d['net_pnl'].std()
        m = d['net_pnl'].mean()
        avg_d = d['days_held'].mean()
        sh = (m / s * np.sqrt(252 / avg_d)) if s > 0 and avg_d > 0 else 0
        return {'n': len(d), 'wr': (d['net_pnl'] > 0).mean(),
                'pnl': m, 'sharpe': sh}
    
    tr_s = stats(train)
    te_s = stats(test)
    
    # Concentration analysis (test set)
    if len(test) > 0:
        per_stock = test.groupby('ticker')['net_pnl'].sum()
        if per_stock.sum() > 0:
            top3 = per_stock.nlargest(3).sum()
            concentration = top3 / per_stock.sum()
        else:
            concentration = 1.0
    else:
        concentration = 1.0
    
    return {
        'label': label,
        'train_n': tr_s['n'], 'test_n': te_s['n'],
        'train_pnl': tr_s['pnl'], 'test_pnl': te_s['pnl'],
        'train_wr': tr_s['wr'], 'test_wr': te_s['wr'],
        'train_sharpe': tr_s['sharpe'], 'test_sharpe': te_s['sharpe'],
        'concentration': concentration,
        'trades_per_stock': test.groupby('ticker').size().to_dict() if len(test) > 0 else {},
    }

def passes_criteria(r):
    reasons = []
    if r['test_n'] < Criteria.MIN_TEST_TRADES:
        reasons.append(f"test_n {r['test_n']}<{Criteria.MIN_TEST_TRADES}")
    if r['test_pnl'] <= 0:
        reasons.append(f"test P&L {r['test_pnl']*100:+.2f}%")
    if r['train_pnl'] <= 0:
        reasons.append(f"train P&L {r['train_pnl']*100:+.2f}%")
    if r['test_sharpe'] < Criteria.MIN_TEST_SHARPE:
        reasons.append(f"Sharpe {r['test_sharpe']:.2f}")
    if r['concentration'] > Criteria.MAX_CONCENTRATION:
        reasons.append(f"conc {r['concentration']*100:.0f}%")
    if r['train_pnl'] > 0 and r['test_pnl'] > 0:
        decay = (r['train_pnl'] - r['test_pnl']) / r['train_pnl']
        if decay > Criteria.MAX_DECAY:
            reasons.append(f"decay {decay*100:.0f}%")
    return len(reasons) == 0, reasons

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("UAE MOMENTUM WITH REGIME FILTER")
print("=" * 80)
print(f"Universe: {len(UAE_UNIVERSE)} stocks")
print(f"Period: {START_DATE.date()} → {END_DATE.date()}")
print(f"Train: → {TRAIN_END.date()} | Test: {TRAIN_END.date()} →")
print(f"Regime filter: trade only when DFM index > {REGIME_SMA_PERIOD}d SMA")
print(f"Costs: {ROUND_TRIP_COST*100:.2f}% round-trip")
print()

# Load stock data
print("Loading stock data...")
stocks_data = {}
for ticker in UAE_UNIVERSE:
    df = fetch_stock(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks_data[ticker] = df
print(f"  Loaded {len(stocks_data)} stocks")

# Load regime indicator (DFM index)
print("\nFetching DFM Index for regime filter...")
regime_df, regime_ticker = fetch_dfm_index()

if regime_df is None:
    print("  Building equal-weight UAE composite as regime proxy...")
    regime_df = construct_market_proxy(stocks_data)
    regime_ticker = "Equal-weight UAE composite"

# Show regime breakdown
total_days = len(regime_df.dropna(subset=['regime_up']))
up_days = regime_df['regime_up'].sum()
print(f"  Regime filter source: {regime_ticker}")
print(f"  Total days: {total_days}")
print(f"  Days in uptrend regime: {up_days} ({up_days/total_days*100:.1f}%)")
print(f"  Days in non-uptrend (filtered out): {total_days-up_days} ({(total_days-up_days)/total_days*100:.1f}%)")

# Show regime breakdown by period
train_regime = regime_df[regime_df.index <= TRAIN_END]
test_regime = regime_df[regime_df.index > TRAIN_END]
train_up = train_regime['regime_up'].sum()
train_total = len(train_regime.dropna(subset=['regime_up']))
test_up = test_regime['regime_up'].sum()
test_total = len(test_regime.dropna(subset=['regime_up']))

print(f"\n  Train period (2022-2023):")
print(f"    Uptrend days: {train_up}/{train_total} ({train_up/max(train_total,1)*100:.1f}%)")
print(f"  Test period (2024-2026):")
print(f"    Uptrend days: {test_up}/{test_total} ({test_up/max(test_total,1)*100:.1f}%)")

# Run momentum variants
print("\n" + "=" * 80)
print("Running momentum variants WITH regime filter...")
print("=" * 80)

variants = []
for threshold in [0.05, 0.08]:
    for sl in [0.05, 0.07]:
        for tp in [0.10, 'trailing']:
            label = f"Mom t{int(threshold*100)}_sl{int(sl*100)}_tp{'tr' if tp=='trailing' else int(tp*100)}"
            variants.append((threshold, sl, tp, label))

results = []
for threshold, sl, tp, label in variants:
    r = run_strategy(stocks_data, regime_df, threshold, sl, tp, label)
    if r is not None:
        results.append(r)

results_df = pd.DataFrame(results).sort_values('test_pnl', ascending=False)

# =============================================================================
# OUTPUT
# =============================================================================

print(f"\n{'Strategy':<32}{'Tr#':>5}{'Te#':>5}{'TrP&L':>9}{'TeP&L':>9}"
      f"{'TrWR':>7}{'TeWR':>7}{'Shrp':>7}{'Conc':>7}")
print("-" * 96)
for _, r in results_df.iterrows():
    print(f"{r['label']:<32}{r['train_n']:>5}{r['test_n']:>5}"
          f"{r['train_pnl']*100:>+8.2f}%{r['test_pnl']*100:>+8.2f}%"
          f"{r['train_wr']*100:>6.1f}%{r['test_wr']*100:>6.1f}%"
          f"{r['test_sharpe']:>7.2f}{r['concentration']*100:>6.0f}%")

# =============================================================================
# COMPARE TO ORIGINAL (NO REGIME FILTER) RESULTS
# =============================================================================

print("\n" + "=" * 80)
print("COMPARISON — what the regime filter changed")
print("=" * 80)
print()
print("Original results (NO regime filter, from prior test):")
print("  Mom t8 sl7 tptr: Train -0.50%, Test +2.66%, Sharpe 1.70")
print("  Mom t5 sl7 tptr: Train -0.35%, Test +2.28%, Sharpe 1.33")
print()
print("Now with regime filter applied:")
for _, r in results_df.head(2).iterrows():
    print(f"  {r['label']}: Train {r['train_pnl']*100:+.2f}%, "
          f"Test {r['test_pnl']*100:+.2f}%, Sharpe {r['test_sharpe']:.2f}")

# =============================================================================
# CRITERIA CHECK
# =============================================================================

print("\n" + "=" * 80)
print("ACCEPTANCE CRITERIA CHECK")
print("=" * 80)

passers = []
for _, r in results_df.iterrows():
    passed, reasons = passes_criteria(r.to_dict())
    if passed:
        passers.append(r.to_dict())

if passers:
    print(f"\n✅ {len(passers)} variant(s) PASSED all 5 criteria with regime filter:\n")
    for p in passers:
        print(f"  {p['label']}")
        print(f"    Train: n={p['train_n']}, P&L={p['train_pnl']*100:+.2f}%, "
              f"WR={p['train_wr']*100:.1f}%")
        print(f"    Test:  n={p['test_n']}, P&L={p['test_pnl']*100:+.2f}%, "
              f"WR={p['test_wr']*100:.1f}%, Sharpe={p['test_sharpe']:.2f}")
        print(f"    Concentration: {p['concentration']*100:.0f}% in top 3 stocks")
        print()
    
    print("INTERPRETATION:")
    print("  The regime filter rescued these variants. The momentum edge is real")
    print("  but only operates during uptrend regimes. Trading them blindly")
    print("  through all regimes destroys the edge.")
else:
    print("\n❌ Zero variants passed even with the regime filter.")
    print("\nThis tells us the apparent 'momentum edge' was NOT just a regime effect.")
    print("The filter didn't save it because:")
    print("  - Either the train period's uptrend days also lost money (so still negative)")
    print("  - Or the test sample shrunk too much to hit 50 trade minimum")
    print("  - Or both")
    print("\nReal answer: this is not a deployable edge with available data.")

# Show why specific variants failed
print("\n" + "=" * 80)
print("Reasons for failure (by variant):")
print("=" * 80)
for _, r in results_df.iterrows():
    passed, reasons = passes_criteria(r.to_dict())
    if not passed:
        reasons_str = ", ".join(reasons)
        print(f"  {r['label']}: {reasons_str}")

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

if passers:
    best = passers[0]
    print(f"\nReal regime-dependent edge identified.")
    print(f"Best: {best['label']}")
    print(f"  Test P&L: {best['test_pnl']*100:+.2f}%, Sharpe {best['test_sharpe']:.2f}")
    print(f"  Trades only when DFM index > 200d SMA")
    print(f"\nNext step: paper trade for 60-90 days to validate forward.")
else:
    print(f"\nNo strategy passed criteria even with regime filter.")
    print(f"The 'momentum edge' was not a true regime-dependent signal.")
    print(f"It was either small-sample noise or required a specific regime")
    print(f"that the filter doesn't fully capture.")
    print(f"\nRecommendation: stop iterating, deploy US PEAD which works.")

print()
