"""
EEM vs UAE MOMENTUM HEAD-TO-HEAD COMPARISON
============================================

Tests whether the EEM-filtered UAE momentum strategy is actually better
than just buying EEM directly during EEM uptrends.

Three strategies compared on the same time period:

  1) EEM_BUY_HOLD: Buy EEM when above 200d SMA, hold while above, sell when below
  2) EEM_PULLBACK: Buy EEM when above 200d SMA after a 5%+ pullback, exit on bounce/stop
  3) UAE_MOM_FILTERED: UAE momentum signals only when EEM > 200d SMA (our finding)

If buying EEM directly produces similar or better risk-adjusted returns,
the UAE complexity isn't worth it. Just trade EEM.

If UAE strategy materially outperforms EEM on its own, there's real
UAE-specific edge worth pursuing.
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

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 4)

# Costs
UAE_COMMISSION = 0.00275
UAE_SLIPPAGE = 0.0025
UAE_ROUND_TRIP = UAE_COMMISSION * 2 + UAE_SLIPPAGE * 2  # 1.05%

US_COMMISSION = 0.0005
US_SLIPPAGE = 0.001
US_ROUND_TRIP = US_COMMISSION * 2 + US_SLIPPAGE * 2  # 0.30%

# UAE strategy params
MIN_DOLLAR_VOLUME = 1_000_000
MOMENTUM_THRESHOLD = 0.05
SL_PCT = 0.07
TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD_DAYS = 20

# EEM SMA
EEM_SMA_PERIOD = 200

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
        return df
    except Exception:
        return None

def fetch_uae_stock(ticker, start, end):
    df = fetch_data(ticker, start, end)
    if df is None:
        return None
    df['ret_5d'] = df['Close'].pct_change(5)
    df['sma_50'] = df['Close'].rolling(50).mean()
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss))
    df['avg_dollar_vol_usd'] = (df['Close'] * df['Volume']).rolling(20).mean() / 3.67
    df['liquid'] = df['avg_dollar_vol_usd'] > MIN_DOLLAR_VOLUME
    return df

# =============================================================================
# STRATEGY 1: BUY-AND-HOLD EEM WHEN ABOVE SMA
# =============================================================================

def strategy_1_eem_buy_hold(eem_df):
    """
    Buy EEM at the open after it crosses above 200d SMA.
    Sell at the open after it crosses below 200d SMA.
    Compute equity curve and trade list.
    """
    df = eem_df.copy()
    df['sma_200'] = df['Close'].rolling(EEM_SMA_PERIOD).mean()
    df['above'] = df['Close'] > df['sma_200']
    df['cross_up'] = df['above'] & ~df['above'].shift(1).fillna(False)
    df['cross_down'] = ~df['above'] & df['above'].shift(1).fillna(False)
    
    trades = []
    in_position = False
    entry_price = None
    entry_date = None
    
    for i in range(1, len(df)):
        # Sell signal
        if in_position and df['cross_down'].iloc[i]:
            if i + 1 < len(df):
                exit_price = df['Open'].iloc[i + 1]
                pnl = (exit_price - entry_price) / entry_price
                net_pnl = pnl - US_ROUND_TRIP
                trades.append({
                    'entry_date': entry_date,
                    'exit_date': df.index[i + 1],
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'gross_pnl': pnl,
                    'net_pnl': net_pnl,
                    'days_held': (df.index[i + 1] - entry_date).days,
                })
                in_position = False
        
        # Buy signal
        if not in_position and df['cross_up'].iloc[i]:
            if i + 1 < len(df):
                entry_price = df['Open'].iloc[i + 1]
                entry_date = df.index[i + 1]
                in_position = True
    
    # Close any open position at end
    if in_position:
        exit_price = df['Close'].iloc[-1]
        pnl = (exit_price - entry_price) / entry_price
        net_pnl = pnl - US_ROUND_TRIP
        trades.append({
            'entry_date': entry_date,
            'exit_date': df.index[-1],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'gross_pnl': pnl,
            'net_pnl': net_pnl,
            'days_held': (df.index[-1] - entry_date).days,
        })
    
    return pd.DataFrame(trades)

# =============================================================================
# STRATEGY 2: EEM PULLBACK ENTRY
# =============================================================================

def strategy_2_eem_pullback(eem_df):
    """
    Only enter EEM when above 200d SMA AND after a 5%+ pullback from recent high.
    Exit on 7% stop or trailing stop.
    """
    df = eem_df.copy()
    df['sma_200'] = df['Close'].rolling(EEM_SMA_PERIOD).mean()
    df['high_20d'] = df['High'].rolling(20).max()
    df['pullback'] = (df['high_20d'] - df['Close']) / df['high_20d']
    df['ret_3d'] = df['Close'].pct_change(3)
    
    triggers = ((df['Close'] > df['sma_200']) & 
                (df['pullback'] > 0.05) & 
                (df['ret_3d'] < -0.02))  # 3-day decline
    
    trades = []
    cooldown_until = None
    
    for date in df.index[triggers]:
        if cooldown_until and date < cooldown_until:
            continue
        
        idx = df.index.get_loc(date)
        if idx + 1 >= len(df):
            continue
        
        entry_price = df['Open'].iloc[idx + 1]
        entry_date = df.index[idx + 1]
        
        # Simulate trade
        peak = 0
        exit_pnl = 0
        exit_reason = 'time'
        days_held = 0
        
        for offset in range(MAX_HOLD_DAYS):
            sim_idx = idx + 1 + offset
            if sim_idx >= len(df):
                exit_pnl = (df['Close'].iloc[-1] - entry_price) / entry_price
                days_held = offset
                break
            
            high = df['High'].iloc[sim_idx]
            low = df['Low'].iloc[sim_idx]
            close = df['Close'].iloc[sim_idx]
            
            adverse = (low - entry_price) / entry_price
            favorable = (high - entry_price) / entry_price
            close_pnl = (close - entry_price) / entry_price
            
            days_held = offset + 1
            
            if adverse < -SL_PCT:
                exit_pnl = -SL_PCT
                exit_reason = 'stop'
                break
            
            if favorable > peak:
                peak = favorable
            if peak >= TRAIL_TRIGGER and close_pnl < (peak - TRAIL_GIVEBACK):
                exit_pnl = peak - TRAIL_GIVEBACK
                exit_reason = 'trail'
                break
            
            exit_pnl = close_pnl
        
        net_pnl = exit_pnl - US_ROUND_TRIP
        trades.append({
            'entry_date': entry_date,
            'exit_date': df.index[min(idx + 1 + days_held, len(df) - 1)],
            'gross_pnl': exit_pnl,
            'net_pnl': net_pnl,
            'days_held': days_held,
            'exit_reason': exit_reason,
        })
        
        # Cooldown of 5 days to avoid overlapping trades
        cooldown_until = df.index[min(idx + days_held + 5, len(df) - 1)]
    
    return pd.DataFrame(trades)

# =============================================================================
# STRATEGY 3: UAE MOMENTUM FILTERED BY EEM > 200d SMA
# =============================================================================

def strategy_3_uae_momentum_eem_filter(stocks_data, eem_df):
    """UAE momentum signals, only when EEM > 200d SMA"""
    df_eem = eem_df.copy()
    df_eem['sma_200'] = df_eem['Close'].rolling(EEM_SMA_PERIOD).mean()
    df_eem['regime'] = df_eem['Close'] > df_eem['sma_200']
    
    if df_eem.index.tz is not None:
        df_eem.index = df_eem.index.tz_localize(None)
    
    trades = []
    
    for ticker, df in stocks_data.items():
        triggers = ((df['ret_5d'] > MOMENTUM_THRESHOLD) & 
                    (df['Close'] > df['sma_50']) & 
                    (df['rsi'] > 50) & 
                    df['liquid'])
        
        for date in df.index[triggers]:
            # Check EEM regime (prior day)
            date_naive = pd.Timestamp(date).normalize()
            if date_naive.tz is not None:
                date_naive = date_naive.tz_localize(None)
            
            try:
                eem_idx = df_eem.index.get_indexer([date_naive], method='ffill')[0]
                if eem_idx <= 0:
                    continue
                if not df_eem['regime'].iloc[eem_idx - 1]:
                    continue
            except Exception:
                continue
            
            entry_idx = df.index.get_loc(date)
            if entry_idx + 1 >= len(df):
                continue
            
            entry_price = df['Open'].iloc[entry_idx + 1]
            if pd.isna(entry_price):
                continue
            
            # Simulate
            peak = 0
            exit_pnl = 0
            days_held = 0
            
            for offset in range(MAX_HOLD_DAYS):
                sim_idx = entry_idx + 1 + offset
                if sim_idx >= len(df):
                    exit_pnl = (df['Close'].iloc[-1] - entry_price) / entry_price
                    days_held = offset
                    break
                
                high = df['High'].iloc[sim_idx]
                low = df['Low'].iloc[sim_idx]
                close = df['Close'].iloc[sim_idx]
                
                adverse = (low - entry_price) / entry_price
                favorable = (high - entry_price) / entry_price
                close_pnl = (close - entry_price) / entry_price
                
                days_held = offset + 1
                
                if adverse < -SL_PCT:
                    exit_pnl = -SL_PCT
                    break
                if favorable > peak:
                    peak = favorable
                if peak >= TRAIL_TRIGGER and close_pnl < (peak - TRAIL_GIVEBACK):
                    exit_pnl = peak - TRAIL_GIVEBACK
                    break
                exit_pnl = close_pnl
            
            net_pnl = exit_pnl - UAE_ROUND_TRIP
            trades.append({
                'ticker': ticker,
                'entry_date': date,
                'gross_pnl': exit_pnl,
                'net_pnl': net_pnl,
                'days_held': days_held,
            })
    
    return pd.DataFrame(trades)

# =============================================================================
# COMPARE METRICS
# =============================================================================

def compute_metrics(trades, name, total_period_days):
    if len(trades) == 0:
        return None
    
    total_pnl = trades['net_pnl'].sum()
    avg_pnl = trades['net_pnl'].mean()
    win_rate = (trades['net_pnl'] > 0).mean()
    n = len(trades)
    
    # Sharpe (annualized)
    std = trades['net_pnl'].std()
    avg_days = trades['days_held'].mean() if 'days_held' in trades.columns else 1
    if std > 0 and avg_days > 0:
        sharpe = avg_pnl / std * np.sqrt(252 / avg_days)
    else:
        sharpe = 0
    
    # Total return assuming sequential trading (compounded)
    # Simplification: assume non-overlapping for sequential
    compounded = (1 + trades['net_pnl']).prod() - 1
    
    # Annualized
    years = total_period_days / 365
    if years > 0:
        annualized = (1 + compounded) ** (1 / years) - 1
    else:
        annualized = 0
    
    # Max drawdown of equity curve
    equity = (1 + trades['net_pnl']).cumprod()
    peak = equity.cummax()
    dd = (equity - peak) / peak
    max_dd = dd.min()
    
    return {
        'name': name,
        'n_trades': n,
        'avg_pnl': avg_pnl,
        'win_rate': win_rate,
        'sharpe': sharpe,
        'total_return': compounded,
        'annualized': annualized,
        'max_dd': max_dd,
        'avg_days_held': avg_days,
    }

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("EEM vs UAE MOMENTUM — HEAD-TO-HEAD COMPARISON")
print("=" * 80)
print(f"\nPeriod: {START_DATE.date()} to {END_DATE.date()}")
print(f"Costs: UAE round-trip {UAE_ROUND_TRIP*100:.2f}%, US round-trip {US_ROUND_TRIP*100:.2f}%")

# Fetch EEM
print("\nFetching EEM data...")
eem_df = fetch_data('EEM', START_DATE - timedelta(days=300), END_DATE)
if eem_df is None or len(eem_df) < 200:
    print("ERROR: Could not fetch EEM")
    exit()
print(f"  EEM: {len(eem_df)} bars")

# Fetch UAE stocks
print("\nFetching UAE stocks...")
stocks_data = {}
for ticker in UAE_UNIVERSE:
    df = fetch_uae_stock(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks_data[ticker] = df
print(f"  Loaded {len(stocks_data)} UAE stocks")

# Compute total period
period_days = (END_DATE - START_DATE).days

# Run all 3 strategies
print("\n" + "=" * 80)
print("Running strategies...")
print("=" * 80)

print("\n[1] EEM Buy-and-Hold (when above 200d SMA)...")
s1 = strategy_1_eem_buy_hold(eem_df)
print(f"    Generated {len(s1)} trades")

print("\n[2] EEM Pullback Entry (5%+ pullback in uptrend)...")
s2 = strategy_2_eem_pullback(eem_df)
print(f"    Generated {len(s2)} trades")

print("\n[3] UAE Momentum filtered by EEM > 200d SMA...")
s3 = strategy_3_uae_momentum_eem_filter(stocks_data, eem_df)
print(f"    Generated {len(s3)} trades")

# Compute metrics
m1 = compute_metrics(s1, "1. EEM Buy-Hold", period_days)
m2 = compute_metrics(s2, "2. EEM Pullback", period_days)
m3 = compute_metrics(s3, "3. UAE Mom + EEM filter", period_days)

# =============================================================================
# RESULTS
# =============================================================================

print("\n" + "=" * 80)
print("RESULTS — All metrics")
print("=" * 80)

print(f"\n{'Strategy':<28}{'Trades':>8}{'Avg P&L':>10}{'WR':>7}{'Sharpe':>8}"
      f"{'Total':>10}{'Annual':>10}{'MaxDD':>9}")
print("-" * 90)

for m in [m1, m2, m3]:
    if m is None:
        continue
    print(f"{m['name']:<28}{m['n_trades']:>8}"
          f"{m['avg_pnl']*100:>+9.2f}%{m['win_rate']*100:>6.1f}%"
          f"{m['sharpe']:>8.2f}"
          f"{m['total_return']*100:>+9.1f}%{m['annualized']*100:>+9.1f}%"
          f"{m['max_dd']*100:>+8.1f}%")

# =============================================================================
# TRAIN/TEST BREAKDOWN
# =============================================================================

print("\n" + "=" * 80)
print("BREAKDOWN — Train (2022-2023) vs Test (2024-2026)")
print("=" * 80)

def split_metrics(trades, name):
    if len(trades) == 0:
        return
    if 'entry_date' in trades.columns:
        date_col = 'entry_date'
    else:
        return
    trades = trades.copy()
    trades[date_col] = pd.to_datetime(trades[date_col])
    if trades[date_col].dt.tz is not None:
        trades[date_col] = trades[date_col].dt.tz_localize(None)
    
    train = trades[trades[date_col] <= TRAIN_END]
    test = trades[trades[date_col] > TRAIN_END]
    
    print(f"\n{name}")
    if len(train) > 0:
        print(f"  Train: n={len(train)}, avg P&L={train['net_pnl'].mean()*100:+.2f}%, "
              f"WR={(train['net_pnl']>0).mean()*100:.1f}%, "
              f"total={(((1+train['net_pnl']).prod())-1)*100:+.1f}%")
    if len(test) > 0:
        print(f"  Test:  n={len(test)}, avg P&L={test['net_pnl'].mean()*100:+.2f}%, "
              f"WR={(test['net_pnl']>0).mean()*100:.1f}%, "
              f"total={(((1+test['net_pnl']).prod())-1)*100:+.1f}%")

split_metrics(s1, "1. EEM Buy-Hold")
split_metrics(s2, "2. EEM Pullback")
split_metrics(s3, "3. UAE Momentum + EEM filter")

# =============================================================================
# CAPITAL EFFICIENCY ANALYSIS
# =============================================================================

print("\n" + "=" * 80)
print("CAPITAL EFFICIENCY — Comparing the strategies fairly")
print("=" * 80)

print("""
Important caveat: these strategies have different capital characteristics.

Strategy 1 (EEM Buy-Hold): position is held continuously when above SMA.
Capital is ~always deployed. Returns reflect time-in-market.

Strategy 2 (EEM Pullback): selective entries with cooldown.
Capital is deployed maybe 30-50% of the time.

Strategy 3 (UAE Momentum): many simultaneous positions possible.
Total capital required = max concurrent positions × position size.
Could require $30-50k+ for full execution if 3-5 trades active simultaneously.

For a fair retail comparison: assume $10k capital and look at actual return on capital.
""")

# Compute approximate capital efficiency
def time_in_market(trades, total_days):
    if len(trades) == 0:
        return 0
    if 'days_held' in trades.columns:
        return trades['days_held'].sum() / total_days
    return None

t1 = time_in_market(s1, period_days)
t2 = time_in_market(s2, period_days)
t3 = time_in_market(s3, period_days)

print(f"  Strategy 1 (EEM Buy-Hold):    time deployed ≈ {t1*100:.0f}%")
print(f"  Strategy 2 (EEM Pullback):    time deployed ≈ {t2*100:.0f}%")
print(f"  Strategy 3 (UAE filtered):    sum of days × position ≈ {t3*100:.0f}% (assumes 1 position)")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

# Compare annualized returns and Sharpes
candidates = [(m1, "EEM Buy-Hold"), (m2, "EEM Pullback"), (m3, "UAE Mom + EEM")]
candidates = [(m, n) for m, n in candidates if m is not None]

if not candidates:
    print("No strategies produced trades.")
else:
    # Sort by Sharpe
    candidates.sort(key=lambda x: -x[0]['sharpe'])
    
    print("\nRanked by Sharpe ratio:")
    for i, (m, n) in enumerate(candidates):
        print(f"  {i+1}. {n}: Sharpe={m['sharpe']:.2f}, "
              f"Annual={m['annualized']*100:+.1f}%, "
              f"MaxDD={m['max_dd']*100:+.1f}%")
    
    best = candidates[0]
    print(f"\nBest risk-adjusted: {best[1]}")
    
    # Check if UAE meaningfully outperforms EEM
    eem_best = max([m for m, n in candidates if 'EEM' in n and 'UAE' not in n], 
                    key=lambda x: x['sharpe'], default=None)
    uae_strat = next((m for m, n in candidates if 'UAE' in n), None)
    
    if eem_best and uae_strat:
        sharpe_diff = uae_strat['sharpe'] - eem_best['sharpe']
        annual_diff = uae_strat['annualized'] - eem_best['annualized']
        
        print(f"\nUAE vs Best EEM strategy:")
        print(f"  Sharpe difference: {sharpe_diff:+.2f}")
        print(f"  Annualized return difference: {annual_diff*100:+.1f}pp")
        
        if sharpe_diff > 0.3 and annual_diff > 0.05:
            print("\n→ UAE strategy meaningfully outperforms direct EEM trading.")
            print("  There may be UAE-specific edge worth pursuing.")
        elif sharpe_diff > 0:
            print("\n→ UAE strategy marginally better than EEM.")
            print("  Given UAE complexity (19 stocks, higher costs, thin liquidity),")
            print("  this may not be worth it. EEM is much simpler.")
        else:
            print("\n→ EEM beats or matches UAE strategy directly.")
            print("  Just trading EEM is simpler, cheaper, and at least as good.")
            print("  UAE complexity is unjustified.")

print()
