"""
UAE MOMENTUM WITH MACRO REGIME FILTER
======================================

⚠️ READ THIS FIRST — METHODOLOGICAL DISCLAIMER ⚠️
==================================================
This is an IN-SAMPLE REGIME-FITTED BACKTEST, not a validation.

We designed this macro filter AFTER observing that 2022-2023 was a bad period
for momentum (Fed hiking, war) and 2024-2026 was a good period (Fed easing,
recovery). Our 4-year data contains exactly ONE Fed cycle change.

A filter built around "trade when Fed easing + DXY weak + VIX low" will, by
construction, filter OUT most of the bad train period and KEEP most of the
good test period. The strategy WILL likely look good. This does NOT mean it
will work going forward.

What this backtest CAN tell us:
  - Whether the macro overlay mechanically does what we expect
  - The shape of resulting P&L curves
  - Trade frequency under the filter
  - Sanity-check that we coded the filter correctly

What this backtest CANNOT tell us:
  - Whether the strategy will work in future regimes
  - Whether the filter is real or just data fitting
  - Whether to deploy with real money

True validation requires forward live tracking over 6-12 months of unseen data.

==================================================

MACRO REGIME FILTERS APPLIED:
  - DXY (US Dollar Index) below its 200-day SMA = USD weakening = good for EM
  - Brent crude oil above its 200-day SMA = supportive for UAE economy
  - VIX below 25 = risk-on environment
  - Fed funds rate flat or declining (proxy: 10y-2y curve trending steeper)

A trade fires only when ALL macro conditions favorable AND the underlying
momentum signal triggers.

USAGE:
    python uae_momentum_macro.py
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

# Macro indicators (all pulled from Yahoo)
MACRO_TICKERS = {
    'DXY': 'DX-Y.NYB',     # US Dollar Index
    'BRENT': 'BZ=F',        # Brent Crude
    'VIX': '^VIX',          # Volatility Index
    'TNX': '^TNX',          # 10-year Treasury yield (proxy for Fed direction)
}

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 4)

# Costs
COMMISSION = 0.00275
SLIPPAGE = 0.0025
ROUND_TRIP_COST = COMMISSION * 2 + SLIPPAGE * 2

# Liquidity
MIN_DOLLAR_VOLUME = 1_000_000

# Trade exit parameters
STOP_LOSS_PCT = 0.07
TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD_DAYS = 20

# Macro filter thresholds
MACRO_SMA = 200
VIX_THRESHOLD = 25  # Below this = risk-on

# Acceptance criteria
class Criteria:
    MIN_TEST_TRADES = 50
    MIN_TEST_SHARPE = 0.8
    MAX_CONCENTRATION = 0.60
    MAX_DECAY = 0.50

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

def fetch_macro_indicators():
    """Fetch all macro indicators and compute regime indicators"""
    data = {}
    for name, ticker in MACRO_TICKERS.items():
        try:
            df = yf.download(ticker, start=START_DATE - timedelta(days=400),
                           end=END_DATE, progress=False, auto_adjust=True)
            if df is None or len(df) < 200:
                print(f"  ⚠ {name} ({ticker}): no/insufficient data")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df['sma_200'] = df['Close'].rolling(MACRO_SMA).mean()
            data[name] = df
            print(f"  ✓ {name} ({ticker}): {len(df)} bars")
        except Exception as e:
            print(f"  ⚠ {name}: {e}")
    return data

def build_regime_signals(macro_data, dates):
    """For each date, determine if all macro conditions are favorable"""
    regime = pd.DataFrame(index=dates)
    
    if 'DXY' in macro_data:
        dxy = macro_data['DXY'].reindex(dates, method='ffill')
        regime['dxy_weak'] = dxy['Close'] < dxy['sma_200']
    else:
        regime['dxy_weak'] = True
    
    if 'BRENT' in macro_data:
        brent = macro_data['BRENT'].reindex(dates, method='ffill')
        regime['brent_strong'] = brent['Close'] > brent['sma_200']
    else:
        regime['brent_strong'] = True
    
    if 'VIX' in macro_data:
        vix = macro_data['VIX'].reindex(dates, method='ffill')
        regime['vix_low'] = vix['Close'] < VIX_THRESHOLD
    else:
        regime['vix_low'] = True
    
    if 'TNX' in macro_data:
        # Use 10y yield trend: if yield falling = Fed easing direction
        tnx = macro_data['TNX'].reindex(dates, method='ffill')
        # Yield below its 50-day average suggests easing direction
        tnx['sma_50'] = tnx['Close'].rolling(50).mean()
        regime['rates_easing'] = tnx['Close'] < tnx['sma_50']
    else:
        regime['rates_easing'] = True
    
    # All four conditions must be true for "favorable" regime
    regime['favorable'] = (regime['dxy_weak'] & regime['brent_strong'] & 
                          regime['vix_low'] & regime['rates_easing'])
    
    return regime

# =============================================================================
# SIGNAL + TRADE
# =============================================================================

def find_momentum_signals(df, ticker, regime_df, threshold, require_macro):
    """Generate momentum signals optionally filtered by macro regime"""
    signals = []
    
    triggers = ((df['ret_5d'] > threshold) & 
                (df['Close'] > df['sma_50']) & 
                (df['rsi'] > 50) & 
                df['liquid'])
    
    for date in df.index[triggers]:
        if require_macro:
            # Look up regime status using prior day to avoid look-ahead
            try:
                date_naive = pd.Timestamp(date).normalize()
                if date_naive.tz is not None:
                    date_naive = date_naive.tz_localize(None)
                regime_idx = regime_df.index.get_indexer([date_naive], method='ffill')[0]
                if regime_idx <= 0:
                    continue
                # Use prior day's regime status (causally clean)
                if not regime_df.iloc[regime_idx - 1]['favorable']:
                    continue
            except Exception:
                continue
        
        idx = df.index.get_loc(date)
        if idx >= len(df) - 1:
            continue
        signals.append({'ticker': ticker, 'date': date, 'idx': idx})
    
    return signals

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

def run_strategy(stocks_data, regime_df, threshold, sl, tp, require_macro, label):
    all_trades = []
    for ticker, df in stocks_data.items():
        signals = find_momentum_signals(df, ticker, regime_df, threshold, require_macro)
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
    
    # Concentration (test set)
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
    }

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("UAE MOMENTUM WITH MACRO REGIME FILTER")
print("=" * 80)
print()
print("⚠️  This is an IN-SAMPLE REGIME-FITTED BACKTEST.")
print("    The filter was designed AFTER observing the 2022-2023 vs 2024-2026 split.")
print("    Strong results here do NOT validate the strategy for live deployment.")
print("    True validation requires forward live tracking.")
print()

# Load stock data
print("Loading UAE stock data...")
stocks_data = {}
for ticker in UAE_UNIVERSE:
    df = fetch_stock(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks_data[ticker] = df
print(f"Loaded {len(stocks_data)} stocks")

# Load macro indicators
print("\nLoading macro indicators...")
macro_data = fetch_macro_indicators()

# Build regime signals
all_dates = sorted(set().union(*(set(df.index.tz_localize(None) if df.index.tz else df.index) 
                                  for df in stocks_data.values())))
regime_df = build_regime_signals(macro_data, all_dates)

# Show regime breakdown
total_days = len(regime_df.dropna(subset=['favorable']))
fav_days = regime_df['favorable'].sum()
train_regime = regime_df[regime_df.index <= TRAIN_END]
test_regime = regime_df[regime_df.index > TRAIN_END]
train_fav = train_regime['favorable'].sum()
test_fav = test_regime['favorable'].sum()

print(f"\nMacro regime breakdown (full period):")
print(f"  Favorable days: {fav_days}/{total_days} ({fav_days/total_days*100:.1f}%)")

print(f"\nIndividual filter breakdowns:")
for col in ['dxy_weak', 'brent_strong', 'vix_low', 'rates_easing']:
    if col in regime_df.columns:
        pct = regime_df[col].sum() / len(regime_df) * 100
        print(f"  {col}: {pct:.1f}% of days")

print(f"\nFavorable days by period:")
print(f"  Train (2022-2023): {train_fav}/{len(train_regime)} ({train_fav/max(len(train_regime),1)*100:.1f}%)")
print(f"  Test (2024-2026):  {test_fav}/{len(test_regime)} ({test_fav/max(len(test_regime),1)*100:.1f}%)")
print(f"\n  ⚠️  Big asymmetry between train and test favorable days is EXPECTED")
print(f"     and is exactly the regime fitting we warned about.")

# =============================================================================
# RUN COMPARISONS
# =============================================================================

print("\n" + "=" * 80)
print("Running comparisons: NO filter vs WITH macro filter")
print("=" * 80)

variants = []
for threshold in [0.05, 0.08]:
    for sl in [0.05, 0.07]:
        for require_macro in [False, True]:
            tag = "_MACRO" if require_macro else "_RAW"
            label = f"Mom t{int(threshold*100)}_sl{int(sl*100)}_tptr{tag}"
            variants.append((threshold, sl, 'trailing', require_macro, label))

results = []
for threshold, sl, tp, require_macro, label in variants:
    r = run_strategy(stocks_data, regime_df, threshold, sl, tp, require_macro, label)
    if r is not None:
        results.append(r)

results_df = pd.DataFrame(results)

# =============================================================================
# OUTPUT
# =============================================================================

print(f"\n{'Strategy':<35}{'Tr#':>5}{'Te#':>5}{'TrP&L':>9}{'TeP&L':>9}"
      f"{'TrWR':>7}{'TeWR':>7}{'Shrp':>7}{'Conc':>7}")
print("-" * 96)

# Sort: macro versions first within each threshold/SL group
sort_order = sorted(results, key=lambda x: (x['label'].split('_')[0:3], 'MACRO' not in x['label']))
for r in sort_order:
    print(f"{r['label']:<35}{r['train_n']:>5}{r['test_n']:>5}"
          f"{r['train_pnl']*100:>+8.2f}%{r['test_pnl']*100:>+8.2f}%"
          f"{r['train_wr']*100:>6.1f}%{r['test_wr']*100:>6.1f}%"
          f"{r['test_sharpe']:>7.2f}{r['concentration']*100:>6.0f}%")

# =============================================================================
# DIRECT COMPARISON
# =============================================================================

print("\n" + "=" * 80)
print("DIRECT COMPARISON — How the macro filter changed each variant")
print("=" * 80)

raw = {r['label']: r for r in results if '_RAW' in r['label']}
macro = {r['label'].replace('_MACRO', '_RAW'): r for r in results if '_MACRO' in r['label']}

print(f"\n{'Variant':<22}{'Train P&L':>22}{'Test P&L':>22}{'Test trades':>17}")
print(f"{'':<22}{'NO filter→MACRO':>22}{'NO filter→MACRO':>22}{'NO filter→MACRO':>17}")
print("-" * 85)

for raw_label, raw_r in raw.items():
    base = raw_label.replace('_RAW', '')
    macro_r_label = base + '_MACRO'
    macro_r = next((r for r in results if r['label'] == macro_r_label), None)
    if macro_r:
        train_compare = f"{raw_r['train_pnl']*100:+.2f}%→{macro_r['train_pnl']*100:+.2f}%"
        test_compare = f"{raw_r['test_pnl']*100:+.2f}%→{macro_r['test_pnl']*100:+.2f}%"
        n_compare = f"{raw_r['test_n']}→{macro_r['test_n']}"
        print(f"{base:<22}{train_compare:>22}{test_compare:>22}{n_compare:>17}")

# =============================================================================
# CRITERIA CHECK
# =============================================================================

print("\n" + "=" * 80)
print("ACCEPTANCE CRITERIA CHECK (macro variants only)")
print("=" * 80)

passers = []
for r in results:
    if '_MACRO' not in r['label']:
        continue
    failures = []
    if r['test_n'] < Criteria.MIN_TEST_TRADES:
        failures.append(f"test_n {r['test_n']}")
    if r['test_pnl'] <= 0:
        failures.append(f"test P&L {r['test_pnl']*100:+.2f}%")
    if r['train_pnl'] <= 0:
        failures.append(f"train P&L {r['train_pnl']*100:+.2f}%")
    if r['test_sharpe'] < Criteria.MIN_TEST_SHARPE:
        failures.append(f"Sharpe {r['test_sharpe']:.2f}")
    if r['concentration'] > Criteria.MAX_CONCENTRATION:
        failures.append(f"conc {r['concentration']*100:.0f}%")
    
    if not failures:
        passers.append(r)
    else:
        print(f"  ❌ {r['label']}: {', '.join(failures)}")

if passers:
    print(f"\n✅ {len(passers)} macro-filtered variant(s) passed all criteria:")
    for p in passers:
        print(f"  {p['label']}")
        print(f"    Train: n={p['train_n']}, P&L={p['train_pnl']*100:+.2f}%, "
              f"WR={p['train_wr']*100:.1f}%")
        print(f"    Test:  n={p['test_n']}, P&L={p['test_pnl']*100:+.2f}%, "
              f"WR={p['test_wr']*100:.1f}%, Sharpe={p['test_sharpe']:.2f}")

# =============================================================================
# CRITICAL DISCLAIMER
# =============================================================================

print("\n" + "=" * 80)
print("⚠️  CRITICAL DISCLAIMER — READ THIS")
print("=" * 80)

print("""
If macro-filtered variants passed criteria above, that result IS NOT
a strategy validation. Here's why:

1. We designed the macro filter AFTER observing that 2022-2023 was bad
   and 2024-2026 was good. The filter selects out 2022-2023 by construction.

2. Our 4-year data contains exactly ONE Fed cycle change. We have no way
   to know if the filter works in OTHER regime transitions.

3. Any backtest where you choose the filter to fit the regime split will
   look good. This is in-sample optimization, not out-of-sample validation.

4. To actually validate this, you need either:
   a) Multiple Fed cycles in the data (impossible with available data), OR
   b) Forward live tracking on data we haven't seen (the only real path)

WHAT THIS BACKTEST IS GOOD FOR:
  ✓ Confirming the macro filter mechanically does what we expect
  ✓ Showing trade frequency under the filter
  ✓ Sanity-checking the implementation
  ✓ Setting expectations for forward-tracked deployment

WHAT THIS BACKTEST IS NOT GOOD FOR:
  ✗ Deploying real money based on these numbers
  ✗ Concluding the strategy "works"
  ✗ Sizing positions or estimating live returns

The honest next step is forward live tracking with the macro overlay.
We deploy at small size, log every signal, and after 6-12 months we
have UNSEEN data that can actually validate or kill the strategy.
""")
