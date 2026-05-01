"""
ACADEMIC ANOMALIES BACKTESTER
==============================
Tests 3 academic anomalies × 4 exit strategies = 12 variants.

ANOMALIES TESTED:
  1. PEAD (gap proxy): Stocks that gap up >5% on a high-volume day
     drift higher for weeks. Documented behavioral underreaction.
  2. Overnight Effect: Almost all equity returns come from close→open.
     Buy at close, sell at open. Most documented anomaly in finance.
  3. Cross-sectional Momentum: 12-month winners outperform 12-month losers
     in next 1-3 months. Foundational academic anomaly.

EXIT STRATEGIES TESTED:
  A. Fixed time: hold N days, exit at close
  B. ATR stops: 2×ATR stop loss, 3×ATR profit target, time backstop
  C. Trailing: lock-in trailing stop after profit threshold
  D. Thesis-based: anomaly-specific smart exit

VALIDATION:
  - Train: 2018-2022 (5 years)
  - Test:  2023-2026 (3 years, OUT-OF-SAMPLE)
  - Single ranked table output: which (anomaly + exit) survives

USAGE:
    python anomalies_backtester.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION
# =============================================================================

# Universe: S&P 500-like large/mid caps (better data quality, more institutional)
UNIVERSE = [
    # Large-cap tech
    'AAPL','MSFT','GOOGL','GOOG','META','AMZN','NVDA','TSLA','AVGO','ORCL',
    'ADBE','CRM','CSCO','INTC','AMD','QCOM','TXN','MU','AMAT','LRCX',
    'KLAC','SNPS','CDNS','ANET','PANW','FTNT','CRWD','ZS','NET','DDOG',
    'NOW','SNOW','WDAY','VEEV','INTU','ADP','PAYX',
    # Financials
    'JPM','BAC','WFC','GS','MS','C','BLK','SPGI','AXP','V','MA','PYPL',
    'COF','SCHW','USB','PNC','TFC','MMC','AON','TRV','PGR','CB','MET','PRU',
    # Healthcare
    'UNH','JNJ','LLY','PFE','ABBV','MRK','TMO','ABT','DHR','BMY','AMGN','CVS',
    'CI','HUM','GILD','MDT','SYK','BSX','ISRG','ELV','REGN','VRTX','MRNA',
    # Consumer
    'WMT','PG','KO','PEP','COST','HD','LOW','MCD','SBUX','NKE','TGT','LULU',
    'BKNG','MAR','HLT','DIS','CMCSA','NFLX','TMUS','VZ','T',
    # Industrial
    'CAT','DE','BA','HON','UPS','FDX','LMT','RTX','GE','MMM','EMR','ETN',
    'GD','NOC','UNP','CSX','NSC','WM','RSG','PCAR',
    # Energy / Materials
    'XOM','CVX','COP','SLB','EOG','PSX','MPC','VLO','OXY','PXD','HAL','BKR',
    'LIN','APD','SHW','FCX','NEM','DOW','DD','PPG',
    # REITs / Utilities
    'PLD','AMT','EQIX','PSA','CCI','SPG','O','WELL','VICI','EQR','AVB',
    'NEE','DUK','SO','D','AEP','SRE','EXC','XEL','PCG','ED',
    # Mid-cap / additional growth
    'UBER','LYFT','SHOP','SQ','SNAP','PINS','ROKU','ZM','TWLO','OKTA',
    'TEAM','MDB','HUBS','DOCU','U','PLTR','RBLX','ABNB','DASH','COIN',
    'AFRM','UPST','SOFI','HOOD','MARA','RIOT','GME','AMC','BBBY','CVNA',
]
UNIVERSE = list(set(UNIVERSE))

# Time periods
END_DATE = datetime.now()
TRAIN_END = datetime(2022, 12, 31)
TEST_START = datetime(2023, 1, 1)
START_DATE = END_DATE - timedelta(days=365 * 8)

# Costs (realistic retail trading)
SLIPPAGE = 0.001     # 0.1% slippage per side (large-cap, tighter spreads)
COMMISSION = 0.0003  # 0.03% per side
BORROW_RATE = 0.04   # 4% annualized for shorts (large caps are cheaper to borrow)

# =============================================================================
# DATA LOADING
# =============================================================================

def fetch_data(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 250:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        # Add some pre-computed indicators we'll need
        df['ret_1d'] = df['Close'].pct_change(1)
        df['atr_14'] = compute_atr(df, 14)
        df['vol_avg_20'] = df['Volume'].rolling(20).mean()
        df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
        return df
    except Exception:
        return None

def compute_atr(df, period=14):
    """Average True Range — captures stock-specific volatility"""
    high_low = df['High'] - df['Low']
    high_close = abs(df['High'] - df['Close'].shift(1))
    low_close = abs(df['Low'] - df['Close'].shift(1))
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# =============================================================================
# ANOMALY DETECTORS
# =============================================================================

def find_pead_signals(df, ticker):
    """
    PEAD-gap proxy: Stock gaps up >5% on >2× volume.
    This proxies an earnings surprise (gap = info shock, volume = institutional reaction).
    Long signal: gap up. Short signal: gap down.
    Academic backing: "Earnings Announcement Gap" effect.
    """
    df = df.copy()
    df['gap_pct'] = df['Open'] / df['Close'].shift(1) - 1
    df['gap_up_signal'] = (df['gap_pct'] > 0.05) & (df['vol_ratio'] > 2.0)
    df['gap_down_signal'] = (df['gap_pct'] < -0.05) & (df['vol_ratio'] > 2.0)
    
    signals = []
    for date in df.index[df['gap_up_signal']]:
        idx = df.index.get_loc(date)
        signals.append({'ticker': ticker, 'date': date, 'idx': idx, 'direction': 'long'})
    return signals  # PEAD long-only for cleanness


def find_overnight_signals(df, ticker):
    """
    Overnight Effect: Buy at close, sell at next open.
    Captures the close-to-open premium that has historically been ~all of US equity returns.
    Signal fires every single day (it's a structural effect, not an event).
    For testing purposes, sample on Mondays/Wednesdays/Fridays to limit trade count.
    """
    signals = []
    for date in df.index:
        # Sample 3 days/week for realistic implementation
        if date.dayofweek not in [0, 2, 4]:  # Mon, Wed, Fri
            continue
        idx = df.index.get_loc(date)
        if idx < 50 or idx >= len(df) - 1:
            continue
        signals.append({'ticker': ticker, 'date': date, 'idx': idx, 'direction': 'long'})
    return signals


def find_momentum_signals(stocks_data, rebalance_dates):
    """
    Cross-sectional momentum: at each rebalance date, rank universe by 12-month return
    (skipping last 1 month — standard academic adjustment to avoid 1-month reversal).
    Long top 10%, short bottom 10%. Hold 1 month.
    """
    signals = []
    for rebalance_date in rebalance_dates:
        # Compute 12-month-minus-1 return for each stock
        returns = []
        for ticker, df in stocks_data.items():
            if rebalance_date not in df.index:
                continue
            idx = df.index.get_loc(rebalance_date)
            if idx < 252:
                continue
            # 12-month return ending 1 month ago
            price_now = df.iloc[idx - 21]['Close']  # 1 month ago
            price_then = df.iloc[idx - 252]['Close']  # 12 months ago
            mom_return = (price_now / price_then) - 1
            returns.append({'ticker': ticker, 'idx': idx, 'mom': mom_return,
                          'date': rebalance_date})
        
        if len(returns) < 20:
            continue
        
        rdf = pd.DataFrame(returns).sort_values('mom')
        n = len(rdf)
        
        # Top decile = long; bottom decile = short
        for _, row in rdf.tail(int(n * 0.1)).iterrows():
            signals.append({'ticker': row['ticker'], 'date': row['date'],
                          'idx': row['idx'], 'direction': 'long'})
        for _, row in rdf.head(int(n * 0.1)).iterrows():
            signals.append({'ticker': row['ticker'], 'date': row['date'],
                          'idx': row['idx'], 'direction': 'short'})
    return signals

# =============================================================================
# EXIT STRATEGIES (the trade simulator with 4 exit types)
# =============================================================================

def simulate_trade(df, entry_idx, direction, exit_strategy, anomaly):
    """
    Simulate a single trade with the chosen exit strategy.
    direction: 'long' or 'short'
    exit_strategy: 'fixed', 'atr', 'trailing', 'thesis'
    anomaly: 'pead', 'overnight', 'momentum' (used for thesis-based exits)
    
    Returns dict with net_pnl, days_held, exit_reason, or None if invalid.
    """
    if entry_idx + 1 >= len(df):
        return None
    
    entry_price = df.iloc[entry_idx]['Close']
    entry_atr = df.iloc[entry_idx]['atr_14']
    
    if pd.isna(entry_price) or pd.isna(entry_atr) or entry_atr == 0:
        return None
    
    # Sign multiplier: +1 for long, -1 for short
    sign = 1 if direction == 'long' else -1
    
    # Anomaly-specific exit parameters
    if anomaly == 'pead':
        max_hold = 30  # PEAD drift period
    elif anomaly == 'overnight':
        max_hold = 1   # Overnight is by definition 1 day
    elif anomaly == 'momentum':
        max_hold = 21  # Monthly rebalance
    
    # Initialize tracking
    exit_pnl_pct = 0.0
    days_held = 0
    exit_reason = 'no_data'
    peak_profit_pct = 0.0
    last_close_pnl_pct = 0.0
    
    # ATR-based levels
    atr_pct = entry_atr / entry_price
    stop_loss_pct = 2.0 * atr_pct
    profit_target_pct = 3.0 * atr_pct
    
    # Trailing exit parameters
    trailing_trigger = 0.05  # 5% profit
    trailing_giveback = 0.03  # 3% from peak
    
    # OVERNIGHT special-case: buy close → sell next open, no looping
    if anomaly == 'overnight' and exit_strategy == 'thesis':
        next_open = df.iloc[entry_idx + 1]['Open']
        pnl = sign * (next_open - entry_price) / entry_price
        days_held = 1
        # Apply costs (one round trip)
        net = pnl - SLIPPAGE * 2 - COMMISSION * 2
        return {'gross_pnl': pnl, 'net_pnl': net, 'days_held': 1, 'exit_reason': 'overnight'}
    
    # Standard loop for all other strategies
    for day_offset in range(max_hold):
        idx = entry_idx + 1 + day_offset
        if idx >= len(df):
            if day_offset > 0:
                exit_pnl_pct = last_close_pnl_pct
                days_held = day_offset
                exit_reason = 'no_data'
            break
        
        day_high = df.iloc[idx]['High']
        day_low = df.iloc[idx]['Low']
        day_close = df.iloc[idx]['Close']
        
        # P&L from each day's price extremes (sign-adjusted)
        if direction == 'long':
            adverse_pnl = (day_low - entry_price) / entry_price   # Worst case for long
            favorable_pnl = (day_high - entry_price) / entry_price  # Best case for long
        else:  # short
            adverse_pnl = (entry_price - day_high) / entry_price  # Worst case for short
            favorable_pnl = (entry_price - day_low) / entry_price # Best case for short
        
        close_pnl = sign * (day_close - entry_price) / entry_price
        last_close_pnl_pct = close_pnl
        
        days_held = day_offset + 1
        exit_pnl_pct = close_pnl
        exit_reason = 'time'
        
        # ----- ATR exits -----
        if exit_strategy == 'atr':
            if adverse_pnl < -stop_loss_pct:
                exit_pnl_pct = -stop_loss_pct
                exit_reason = 'stop_loss'
                break
            if favorable_pnl > profit_target_pct:
                exit_pnl_pct = profit_target_pct
                exit_reason = 'profit_target'
                break
        
        # ----- Trailing exits -----
        elif exit_strategy == 'trailing':
            if adverse_pnl < -0.05:  # Hard 5% stop
                exit_pnl_pct = -0.05
                exit_reason = 'stop_loss'
                break
            if favorable_pnl > peak_profit_pct:
                peak_profit_pct = favorable_pnl
            if peak_profit_pct >= trailing_trigger:
                if close_pnl < (peak_profit_pct - trailing_giveback):
                    exit_pnl_pct = peak_profit_pct - trailing_giveback
                    exit_reason = 'trailing'
                    break
        
        # ----- Thesis-based exits -----
        elif exit_strategy == 'thesis':
            if anomaly == 'pead':
                # Exit when drift target reached (academic ~6% in 60 days)
                if close_pnl > 0.06:
                    exit_pnl_pct = close_pnl
                    exit_reason = 'thesis_target'
                    break
                # OR if drift reverses sharply (signal failed)
                if close_pnl < -0.04:
                    exit_pnl_pct = close_pnl
                    exit_reason = 'thesis_invalidated'
                    break
            elif anomaly == 'momentum':
                # Exit if monthly momentum reverses
                if close_pnl < -0.05:
                    exit_pnl_pct = close_pnl
                    exit_reason = 'thesis_invalidated'
                    break
        
        # 'fixed' strategy: just hold to time exit, do nothing here
    
    if days_held == 0:
        return None
    
    # Apply costs
    borrow_cost = (BORROW_RATE * days_held / 365) if direction == 'short' else 0
    total_cost = SLIPPAGE * 2 + COMMISSION * 2 + borrow_cost
    net_pnl = exit_pnl_pct - total_cost
    
    return {'gross_pnl': exit_pnl_pct, 'net_pnl': net_pnl,
            'days_held': days_held, 'exit_reason': exit_reason}


# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("ACADEMIC ANOMALIES BACKTESTER")
print("=" * 80)
print(f"Universe: {len(UNIVERSE)} large/mid-cap stocks")
print(f"Period: 2018 → 2026 (train 2018-2022, test 2023-2026)")
print(f"Anomalies: PEAD-gap, Overnight, Momentum")
print(f"Exits: fixed, atr, trailing, thesis")
print()

# Load data
print("Loading price data...")
stocks_data = {}
for i, ticker in enumerate(UNIVERSE):
    df = fetch_data(ticker, START_DATE, END_DATE)
    if df is None:
        continue
    stocks_data[ticker] = df
    if (i+1) % 50 == 0:
        print(f"  [{i+1}/{len(UNIVERSE)}] {len(stocks_data)} loaded")

print(f"\nLoaded {len(stocks_data)} stocks")

# Generate signals for each anomaly
print("\nGenerating signals...")

all_signals = {'pead': [], 'overnight': [], 'momentum': []}

# PEAD signals
for ticker, df in stocks_data.items():
    all_signals['pead'].extend(find_pead_signals(df, ticker))
print(f"  PEAD-gap: {len(all_signals['pead'])} signals")

# Overnight signals (sample to keep manageable)
for ticker, df in stocks_data.items():
    all_signals['overnight'].extend(find_overnight_signals(df, ticker))
print(f"  Overnight: {len(all_signals['overnight'])} signals")

# Momentum signals (monthly rebalance)
# Generate rebalance dates: 1st trading day of each month
all_dates = sorted(set().union(*(set(df.index) for df in stocks_data.values())))
rebalance_dates = []
last_month = None
for d in all_dates:
    if d.month != last_month:
        rebalance_dates.append(d)
        last_month = d.month
all_signals['momentum'] = find_momentum_signals(stocks_data, rebalance_dates)
print(f"  Momentum: {len(all_signals['momentum'])} signals")

# Run all combinations
print("\nRunning 12 strategy variants...")
results = []

for anomaly in ['pead', 'overnight', 'momentum']:
    for exit_strat in ['fixed', 'atr', 'trailing', 'thesis']:
        # Skip incompatible combinations
        if anomaly == 'overnight' and exit_strat != 'thesis':
            # Overnight is structurally a 1-day trade; only thesis exit makes sense
            # We'll still test fixed (hold 1 day, exit at close instead of open)
            pass
        
        trades = []
        for sig in all_signals[anomaly]:
            ticker = sig['ticker']
            df = stocks_data[ticker]
            t = simulate_trade(df, sig['idx'], sig['direction'], exit_strat, anomaly)
            if t:
                t['ticker'] = ticker
                t['date'] = sig['date']
                t['direction'] = sig['direction']
                trades.append(t)
        
        if not trades:
            continue
        
        trades_df = pd.DataFrame(trades)
        trades_df['date'] = pd.to_datetime(trades_df['date'])
        if trades_df['date'].dt.tz is not None:
            trades_df['date'] = trades_df['date'].dt.tz_localize(None)
        
        train = trades_df[trades_df['date'] <= TRAIN_END]
        test = trades_df[trades_df['date'] > TRAIN_END]
        
        def stats(df):
            if len(df) == 0:
                return {'n': 0, 'wr': 0, 'pnl': 0, 'sharpe': 0}
            std = df['net_pnl'].std()
            mean = df['net_pnl'].mean()
            wr = (df['net_pnl'] > 0).mean()
            avg_days = df['days_held'].mean()
            sharpe = (mean / std * np.sqrt(252 / avg_days)) if std > 0 and avg_days > 0 else 0
            return {'n': len(df), 'wr': wr, 'pnl': mean, 'sharpe': sharpe}
        
        train_s = stats(train)
        test_s = stats(test)
        
        results.append({
            'anomaly': anomaly,
            'exit': exit_strat,
            'train_n': train_s['n'], 'test_n': test_s['n'],
            'train_wr': train_s['wr'], 'test_wr': test_s['wr'],
            'train_pnl': train_s['pnl'], 'test_pnl': test_s['pnl'],
            'train_sharpe': train_s['sharpe'], 'test_sharpe': test_s['sharpe'],
        })
        
        print(f"  {anomaly:<10} + {exit_strat:<10}: train={train_s['n']:>5}, "
              f"test={test_s['n']:>5}, test P&L={test_s['pnl']*100:+.2f}%")

# =============================================================================
# OUTPUT
# =============================================================================

results_df = pd.DataFrame(results).sort_values('test_pnl', ascending=False)

print("\n" + "=" * 80)
print("RESULTS — Ranked by out-of-sample (test) P&L")
print("=" * 80)
print(f"{'Anomaly':<12}{'Exit':<10}{'Tr#':>6}{'Te#':>6}"
      f"{'Tr WR':>8}{'Te WR':>8}{'Tr P&L':>9}{'Te P&L':>9}"
      f"{'Tr Shrp':>9}{'Te Shrp':>9}")
print("-" * 90)
for _, r in results_df.iterrows():
    print(f"{r['anomaly']:<12}{r['exit']:<10}{r['train_n']:>6}{r['test_n']:>6}"
          f"{r['train_wr']*100:>7.1f}%{r['test_wr']*100:>7.1f}%"
          f"{r['train_pnl']*100:>+8.2f}%{r['test_pnl']*100:>+8.2f}%"
          f"{r['train_sharpe']:>9.2f}{r['test_sharpe']:>9.2f}")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT — Strategies that survived out-of-sample")
print("=" * 80)

robust = results_df[
    (results_df['test_pnl'] > 0.005) &  # Positive net P&L
    (results_df['test_n'] >= 50) &       # Enough sample
    (results_df['test_sharpe'] > 0.5)    # Decent risk-adjusted return
]

if len(robust) > 0:
    print(f"\n✅ {len(robust)} strategy(ies) showed real out-of-sample edge:\n")
    for _, r in robust.iterrows():
        decay = (r['train_pnl'] - r['test_pnl']) * 100
        print(f"  • {r['anomaly'].upper()} + {r['exit']}: "
              f"{r['test_pnl']*100:+.2f}% per trade, "
              f"WR {r['test_wr']*100:.1f}%, "
              f"Sharpe {r['test_sharpe']:.2f}, "
              f"decay {decay:+.2f}pp")
    
    best = robust.iloc[0]
    print(f"\nBest: {best['anomaly'].upper()} + {best['exit']}")
    print(f"  At $50k account, ${15000} per position:")
    
    # Estimate annual return
    n_trades_per_year_test = best['test_n'] / 3  # 3 years of test data
    annual_pnl_dollars = 15000 * best['test_pnl'] * n_trades_per_year_test
    print(f"  Trades/year: ~{n_trades_per_year_test:.0f}")
    print(f"  Expected annual return: ~${annual_pnl_dollars:,.0f}")
    print(f"  Expected CAGR on $50k: ~{annual_pnl_dollars/50000*100:.1f}%")
else:
    print("\n❌ NO strategies survived out-of-sample with meaningful edge")
    print("   Either the anomalies have decayed or the implementation needs work")
    
    # Show the closest ones anyway
    top3 = results_df.head(3)
    print(f"\nClosest performers (still not edge):")
    for _, r in top3.iterrows():
        print(f"  {r['anomaly']} + {r['exit']}: test P&L {r['test_pnl']*100:+.2f}%, "
              f"Sharpe {r['test_sharpe']:.2f}")

print()
