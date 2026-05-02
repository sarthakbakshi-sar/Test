"""
UAE MOMENTUM DIAGNOSTIC + REGIME-FITTED EXPLORATION
====================================================

⚠️ METHODOLOGICAL DISCLAIMER ⚠️
This script does TWO things:

PART 1 — DIAGNOSTIC (legitimate)
  Tag every momentum trade with macro/market conditions at entry.
  Show how trade performance varies across different conditions.
  This is descriptive analysis. Not validation. Tells us what the
  strategy actually responds to.

PART 2 — REGIME-FITTED FILTER EXPLORATION (NOT validation)
  Take the strongest discriminator from Part 1, apply it as a filter,
  re-run the backtest. This is in-sample optimization by definition.
  The result will look good because we picked the filter knowing
  what worked. Useful for understanding mechanics, NOT for deployment.

True validation requires forward live tracking. This script cannot
provide that.
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

MACRO_TICKERS = {
    'DXY': 'DX-Y.NYB',
    'BRENT': 'BZ=F',
    'VIX': '^VIX',
    'TNX': '^TNX',
    'SPY': 'SPY',
    'EEM': 'EEM',
    'DFMGI': 'DFMGI.AE',
}

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 4)

COMMISSION = 0.00275
SLIPPAGE = 0.0025
ROUND_TRIP_COST = COMMISSION * 2 + SLIPPAGE * 2
MIN_DOLLAR_VOLUME = 1_000_000

STOP_LOSS_PCT = 0.07
TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD_DAYS = 20

MOMENTUM_THRESHOLD = 0.05  # Use lower threshold for more sample
SL_PCT = 0.07

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
    data = {}
    for name, ticker in MACRO_TICKERS.items():
        try:
            df = yf.download(ticker, start=START_DATE - timedelta(days=400),
                           end=END_DATE, progress=False, auto_adjust=True)
            if df is None or len(df) < 100:
                print(f"  ⚠ {name}: skipped")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            # Compute multiple indicators per macro factor
            df['sma_50'] = df['Close'].rolling(50).mean()
            df['sma_200'] = df['Close'].rolling(200).mean()
            df['ret_20d'] = df['Close'].pct_change(20)
            df['vol_20d'] = df['Close'].pct_change().rolling(20).std()
            data[name] = df
            print(f"  ✓ {name}")
        except Exception:
            pass
    return data

# =============================================================================
# TRADE SIMULATOR
# =============================================================================

def simulate_trade(df, entry_idx, sl, tp_type='trailing'):
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
# GENERATE TRADES WITH FEATURES
# =============================================================================

def get_macro_value(macro_data, name, date, column):
    """Look up a macro value at a given date (using prior day to avoid look-ahead)"""
    if name not in macro_data:
        return None
    df = macro_data[name]
    try:
        date_naive = pd.Timestamp(date).normalize()
        if date_naive.tz is not None:
            date_naive = date_naive.tz_localize(None)
        if df.index.tz is not None:
            df_idx = df.index.tz_localize(None)
        else:
            df_idx = df.index
        idx_arr = df_idx.get_indexer([date_naive], method='ffill')
        if len(idx_arr) == 0 or idx_arr[0] <= 0:
            return None
        # Use prior day to avoid look-ahead bias
        return df[column].iloc[idx_arr[0] - 1]
    except Exception:
        return None

def generate_trades_with_features(stocks_data, macro_data):
    all_trades = []
    
    for ticker, df in stocks_data.items():
        triggers = ((df['ret_5d'] > MOMENTUM_THRESHOLD) & 
                    (df['Close'] > df['sma_50']) & 
                    (df['rsi'] > 50) & 
                    df['liquid'])
        
        for date in df.index[triggers]:
            idx = df.index.get_loc(date)
            if idx >= len(df) - 1:
                continue
            
            t = simulate_trade(df, idx, SL_PCT)
            if not t:
                continue
            
            t['ticker'] = ticker
            t['date'] = date
            
            # Tag with macro features at entry
            for macro_name in macro_data.keys():
                close_val = get_macro_value(macro_data, macro_name, date, 'Close')
                sma200 = get_macro_value(macro_data, macro_name, date, 'sma_200')
                sma50 = get_macro_value(macro_data, macro_name, date, 'sma_50')
                ret20 = get_macro_value(macro_data, macro_name, date, 'ret_20d')
                vol20 = get_macro_value(macro_data, macro_name, date, 'vol_20d')
                
                t[f'{macro_name}_close'] = close_val
                t[f'{macro_name}_above_sma200'] = (close_val > sma200) if (close_val and sma200) else None
                t[f'{macro_name}_above_sma50'] = (close_val > sma50) if (close_val and sma50) else None
                t[f'{macro_name}_ret20d'] = ret20
                t[f'{macro_name}_vol20d'] = vol20
            
            all_trades.append(t)
    
    return pd.DataFrame(all_trades)

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("UAE MOMENTUM DIAGNOSTIC + REGIME-FITTED EXPLORATION")
print("=" * 80)
print()

# Load data
print("Loading UAE stocks...")
stocks_data = {}
for ticker in UAE_UNIVERSE:
    df = fetch_stock(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks_data[ticker] = df
print(f"Loaded {len(stocks_data)} stocks")

print("\nLoading macro indicators...")
macro_data = fetch_macro_indicators()

# Generate trades with features
print("\nGenerating momentum trades with macro features...")
trades_df = generate_trades_with_features(stocks_data, macro_data)
trades_df['date'] = pd.to_datetime(trades_df['date'])
if trades_df['date'].dt.tz is not None:
    trades_df['date'] = trades_df['date'].dt.tz_localize(None)
trades_df['period'] = trades_df['date'].apply(lambda d: 'train' if d <= TRAIN_END else 'test')
trades_df['win'] = trades_df['net_pnl'] > 0

print(f"Total trades: {len(trades_df)}")
print(f"  Train (2022-2023): {(trades_df['period']=='train').sum()}")
print(f"  Test (2024-2026):  {(trades_df['period']=='test').sum()}")

# =============================================================================
# PART 1 — DIAGNOSTIC
# =============================================================================

print("\n" + "=" * 80)
print("PART 1 — DIAGNOSTIC: How does performance vary by macro condition?")
print("=" * 80)

# Test each binary condition
candidate_features = []
for macro_name in macro_data.keys():
    for indicator in ['above_sma200', 'above_sma50']:
        col = f'{macro_name}_{indicator}'
        if col in trades_df.columns:
            candidate_features.append(col)

# Compute mean P&L for each condition (true vs false), separately for train/test
print("\nBINARY CONDITIONS — P&L when condition TRUE vs FALSE")
print(f"{'Condition':<30}{'WhenTrue':<25}{'WhenFalse':<25}{'Spread':<12}")
print(f"{'':<30}{'Train / Test (n)':<25}{'Train / Test (n)':<25}{'(Test)':<12}")
print("-" * 95)

feature_results = []
for col in candidate_features:
    sub = trades_df.dropna(subset=[col])
    if len(sub) == 0:
        continue
    
    true_train = sub[(sub[col] == True) & (sub['period'] == 'train')]
    true_test = sub[(sub[col] == True) & (sub['period'] == 'test')]
    false_train = sub[(sub[col] == False) & (sub['period'] == 'train')]
    false_test = sub[(sub[col] == False) & (sub['period'] == 'test')]
    
    if len(true_test) < 20 or len(false_test) < 20:
        continue  # Skip if too few samples
    
    true_pnl_train = true_train['net_pnl'].mean() if len(true_train) > 0 else 0
    true_pnl_test = true_test['net_pnl'].mean() if len(true_test) > 0 else 0
    false_pnl_train = false_train['net_pnl'].mean() if len(false_train) > 0 else 0
    false_pnl_test = false_test['net_pnl'].mean() if len(false_test) > 0 else 0
    spread = true_pnl_test - false_pnl_test
    
    true_str = f"{true_pnl_train*100:+.2f}%/{true_pnl_test*100:+.2f}% ({len(true_train)}/{len(true_test)})"
    false_str = f"{false_pnl_train*100:+.2f}%/{false_pnl_test*100:+.2f}% ({len(false_train)}/{len(false_test)})"
    
    print(f"{col:<30}{true_str:<25}{false_str:<25}{spread*100:>+8.2f}pp")
    
    feature_results.append({
        'feature': col,
        'true_train_pnl': true_pnl_train,
        'true_test_pnl': true_pnl_test,
        'true_train_n': len(true_train),
        'true_test_n': len(true_test),
        'false_train_pnl': false_pnl_train,
        'false_test_pnl': false_pnl_test,
        'spread': spread,
        # Score: how good when TRUE, both periods positive
        'true_both_positive': true_pnl_train > 0 and true_pnl_test > 0,
        'true_test_pnl_value': true_pnl_test,
    })

# Continuous features (regime quintiles)
print(f"\n\nCONTINUOUS CONDITIONS — Performance by quintile (test set)")
print(f"{'Indicator':<25}{'Q1 (low)':<15}{'Q2':<15}{'Q3':<15}{'Q4':<15}{'Q5 (high)':<15}")
print("-" * 95)

for macro_name in macro_data.keys():
    col = f'{macro_name}_close'
    if col not in trades_df.columns:
        continue
    
    test_trades = trades_df[trades_df['period'] == 'test'].dropna(subset=[col])
    if len(test_trades) < 100:
        continue
    
    # Compute quintiles
    quintiles = pd.qcut(test_trades[col], q=5, labels=[1,2,3,4,5], duplicates='drop')
    if quintiles is None:
        continue
    
    test_trades = test_trades.copy()
    test_trades['quintile'] = quintiles
    
    pnl_by_q = test_trades.groupby('quintile', observed=True)['net_pnl'].agg(['mean', 'count'])
    
    row_parts = []
    for q in [1, 2, 3, 4, 5]:
        if q in pnl_by_q.index:
            m = pnl_by_q.loc[q, 'mean'] * 100
            n = int(pnl_by_q.loc[q, 'count'])
            row_parts.append(f"{m:+.2f}% ({n})")
        else:
            row_parts.append("--")
    
    print(f"{macro_name + '_close':<25}", end='')
    for part in row_parts:
        print(f"{part:<15}", end='')
    print()

# Identify the strongest single discriminator from Part 1
print("\n" + "=" * 80)
print("STRONGEST SINGLE DISCRIMINATORS (test set spread)")
print("=" * 80)

if feature_results:
    fr_sorted = sorted(feature_results, key=lambda x: -x['spread'])
    print(f"\n{'Feature':<30}{'Test spread':<15}{'Test P&L when TRUE':<25}{'TrueN':<10}")
    print("-" * 80)
    for f in fr_sorted[:8]:
        print(f"{f['feature']:<30}{f['spread']*100:>+8.2f}pp     "
              f"{f['true_test_pnl']*100:>+8.2f}% ({f['true_train_pnl']*100:+.2f}% train)  "
              f"{f['true_test_n']:<10}")

# =============================================================================
# PART 2 — REGIME-FITTED FILTER EXPLORATION
# =============================================================================

print("\n" + "=" * 80)
print("PART 2 — REGIME-FITTED FILTER EXPLORATION")
print("⚠️  THIS IS IN-SAMPLE OPTIMIZATION, NOT VALIDATION")
print("=" * 80)
print()
print("Taking the top 3 single-feature filters from Part 1 and applying them.")
print("By construction these will look good. This is for understanding only.\n")

if feature_results:
    # Take top 3 features by test spread (where TRUE is good)
    top_features = [f for f in fr_sorted if f['spread'] > 0][:3]
    
    print("Filters to test:")
    for f in top_features:
        print(f"  - Require {f['feature']} == True")
    
    # Run filtered backtest for each individual top filter
    print("\n" + "-" * 80)
    print(f"{'Filter applied':<32}{'Train':<22}{'Test':<22}{'Pass criteria?':<15}")
    print(f"{'':<32}{'P&L / N / WR':<22}{'P&L / N / WR':<22}")
    print("-" * 80)
    
    # Baseline: no filter
    train = trades_df[trades_df['period'] == 'train']
    test = trades_df[trades_df['period'] == 'test']
    print(f"{'NONE (baseline)':<32}"
          f"{train['net_pnl'].mean()*100:+.2f}% / {len(train)} / {(train['net_pnl']>0).mean()*100:.0f}%   "
          f"{test['net_pnl'].mean()*100:+.2f}% / {len(test)} / {(test['net_pnl']>0).mean()*100:.0f}%")
    
    # Each top feature
    for f in top_features:
        col = f['feature']
        sub = trades_df.dropna(subset=[col])
        sub = sub[sub[col] == True]
        s_train = sub[sub['period'] == 'train']
        s_test = sub[sub['period'] == 'test']
        
        if len(s_train) == 0 or len(s_test) == 0:
            continue
        
        train_pnl = s_train['net_pnl'].mean()
        test_pnl = s_test['net_pnl'].mean()
        train_wr = (s_train['net_pnl'] > 0).mean()
        test_wr = (s_test['net_pnl'] > 0).mean()
        
        # Concentration check
        per_stock = s_test.groupby('ticker')['net_pnl'].sum()
        if per_stock.sum() > 0:
            top3_conc = per_stock.nlargest(3).sum() / per_stock.sum()
        else:
            top3_conc = 1.0
        
        # Compute Sharpe
        std = s_test['net_pnl'].std()
        if std > 0:
            avg_d = s_test['days_held'].mean()
            sharpe = s_test['net_pnl'].mean() / std * np.sqrt(252 / max(avg_d, 1))
        else:
            sharpe = 0
        
        passes = (train_pnl > 0 and test_pnl > 0 and len(s_test) >= 50 and 
                  sharpe >= 0.8 and top3_conc <= 0.6)
        pass_str = "✓ PASS" if passes else "✗ fail"
        
        print(f"{col:<32}"
              f"{train_pnl*100:+.2f}% / {len(s_train)} / {train_wr*100:.0f}%   "
              f"{test_pnl*100:+.2f}% / {len(s_test)} / {test_wr*100:.0f}%   "
              f"{pass_str}")
    
    # Try combining top 2 filters (AND)
    if len(top_features) >= 2:
        print("\nCombined filters:")
        f1, f2 = top_features[0], top_features[1]
        col1, col2 = f1['feature'], f2['feature']
        sub = trades_df.dropna(subset=[col1, col2])
        sub = sub[(sub[col1] == True) & (sub[col2] == True)]
        s_train = sub[sub['period'] == 'train']
        s_test = sub[sub['period'] == 'test']
        
        if len(s_train) > 0 and len(s_test) > 0:
            train_pnl = s_train['net_pnl'].mean()
            test_pnl = s_test['net_pnl'].mean()
            train_wr = (s_train['net_pnl'] > 0).mean()
            test_wr = (s_test['net_pnl'] > 0).mean()
            
            label = f"{col1.split('_')[0]}+{col2.split('_')[0]}"
            print(f"{label:<32}"
                  f"{train_pnl*100:+.2f}% / {len(s_train)} / {train_wr*100:.0f}%   "
                  f"{test_pnl*100:+.2f}% / {len(s_test)} / {test_wr*100:.0f}%")

# =============================================================================
# DISCLAIMER
# =============================================================================

print("\n" + "=" * 80)
print("⚠️  WHAT TO DO WITH THIS DATA")
print("=" * 80)
print("""
PART 1 (diagnostic) findings:
  These tell us which conditions correlated with momentum success in our 4-year
  data window. They are descriptive of what happened, not predictive of what
  will happen. Use them to UNDERSTAND the strategy's sensitivity, not to
  design new filters.

PART 2 (regime-fitted) findings:
  Even if some filters appear to "pass" criteria, this is by construction.
  We picked them BECAUSE they correlated with our regime split. Of course
  they look good on the same data we picked them from.

THE HONEST CONCLUSION:
  - Our 4-year UAE data contains one regime change (Fed cycle 2022→2024)
  - Any analysis on this data shows what worked DURING that specific change
  - We cannot tell from this data whether the patterns hold in OTHER changes
  - Forward live tracking is the only path to real validation

NEXT STEP (if you want to pursue UAE):
  - Pick ONE momentum strategy (with or without your favorite Part 1 finding)
  - Paper trade it forward for 6-12 months at small size
  - Log every signal and outcome
  - Compare actual forward performance to expectations
  - Decide based on UNSEEN data, not optimized historical data
""")
