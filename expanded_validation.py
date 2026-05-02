"""
EXPANDED UNIVERSE QUICK VALIDATION
====================================
Verify EEM-filtered UAE momentum strategy still works with 21 stocks
(19 DFM + ADCB.AB + ETISALAT.AB) before building the Apps Script.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

UNIVERSE = [
    # DFM (19)
    'EMAAR.AE', 'EMIRATESNBD.AE', 'DIB.AE', 'EMAARDEV.AE', 'SALIK.AE',
    'DFM.AE', 'PARKIN.AE', 'AIRARABIA.AE', 'UPP.AE', 'GFH.AE',
    'DEYAAR.AE', 'ARMX.AE', 'DIC.AE', 'DU.AE', 'AMLAK.AE',
    'MASQ.AE', 'TECOM.AE', 'TABREED.AE', 'CBD.AE',
    # ADX (2)
    'ADCB.AB',      # Abu Dhabi Commercial Bank
    'ETISALAT.AB',  # e&
]

END_DATE = datetime.now()
TRAIN_END = datetime(2023, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 4)

ROUND_TRIP_COST = 0.0105
MIN_DOLLAR_VOLUME = 1_000_000
MOMENTUM_THRESHOLD = 0.05
SL_PCT = 0.07
TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD = 20
EEM_SMA = 200

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

def simulate_trade(df, entry_idx):
    if entry_idx + 1 >= len(df):
        return None
    entry_price = df.iloc[entry_idx + 1]['Open']
    if pd.isna(entry_price) or entry_price <= 0:
        return None
    
    peak = 0
    exit_pnl = 0
    days_held = 0
    
    for offset in range(MAX_HOLD):
        idx = entry_idx + 1 + offset
        if idx >= len(df):
            exit_pnl = (df['Close'].iloc[-1] - entry_price) / entry_price
            days_held = offset
            break
        
        high = df['High'].iloc[idx]
        low = df['Low'].iloc[idx]
        close = df['Close'].iloc[idx]
        
        adverse = (low - entry_price) / entry_price
        favorable = (high - entry_price) / entry_price
        close_pnl = (close - entry_price) / entry_price
        
        days_held = offset + 1
        exit_pnl = close_pnl
        
        if adverse < -SL_PCT:
            exit_pnl = -SL_PCT
            break
        if favorable > peak:
            peak = favorable
        if peak >= TRAIL_TRIGGER and close_pnl < (peak - TRAIL_GIVEBACK):
            exit_pnl = peak - TRAIL_GIVEBACK
            break
    
    return {
        'gross_pnl': exit_pnl,
        'net_pnl': exit_pnl - ROUND_TRIP_COST,
        'days_held': days_held,
    }

print("=" * 70)
print("EXPANDED UNIVERSE VALIDATION (21 stocks)")
print("=" * 70)

print(f"\nLoading {len(UNIVERSE)} stocks...")
stocks = {}
for ticker in UNIVERSE:
    df = fetch_stock(ticker, START_DATE, END_DATE)
    if df is not None:
        stocks[ticker] = df
        market = 'ADX' if ticker.endswith('.AB') or ticker == 'FAB' else 'DFM'
        print(f"  ✓ {ticker:<18} ({market})")
print(f"\nLoaded {len(stocks)} of {len(UNIVERSE)}")

# EEM regime
print("\nLoading EEM for regime filter...")
eem = yf.download('EEM', start=START_DATE - timedelta(days=300), end=END_DATE, progress=False, auto_adjust=True)
if isinstance(eem.columns, pd.MultiIndex):
    eem.columns = eem.columns.get_level_values(0)
eem['sma_200'] = eem['Close'].rolling(EEM_SMA).mean()
eem['regime_up'] = eem['Close'] > eem['sma_200']
if eem.index.tz is not None:
    eem.index = eem.index.tz_localize(None)

print(f"EEM loaded ({len(eem)} bars)\n")

# Run strategy with EEM filter
print("Running EEM-filtered momentum on expanded universe...")
trades = []
for ticker, df in stocks.items():
    triggers = ((df['ret_5d'] > MOMENTUM_THRESHOLD) & 
                (df['Close'] > df['sma_50']) & 
                (df['rsi'] > 50) & 
                df['liquid'])
    
    for date in df.index[triggers]:
        # EEM regime check
        date_naive = pd.Timestamp(date).normalize()
        if date_naive.tz is not None:
            date_naive = date_naive.tz_localize(None)
        
        try:
            eem_idx = eem.index.get_indexer([date_naive], method='ffill')[0]
            if eem_idx <= 0:
                continue
            if not eem['regime_up'].iloc[eem_idx - 1]:
                continue
        except Exception:
            continue
        
        idx = df.index.get_loc(date)
        t = simulate_trade(df, idx)
        if t:
            t['ticker'] = ticker
            t['date'] = date
            trades.append(t)

trades_df = pd.DataFrame(trades)
trades_df['date'] = pd.to_datetime(trades_df['date'])
if trades_df['date'].dt.tz is not None:
    trades_df['date'] = trades_df['date'].dt.tz_localize(None)

train = trades_df[trades_df['date'] <= TRAIN_END]
test = trades_df[trades_df['date'] > TRAIN_END]

# =============================================================================
# RESULTS
# =============================================================================

print("\n" + "=" * 70)
print("RESULTS — Expanded universe (21 stocks)")
print("=" * 70)

def stats(d, label):
    if len(d) == 0:
        print(f"\n{label}: no trades")
        return
    n = len(d)
    pnl = d['net_pnl'].mean()
    wr = (d['net_pnl'] > 0).mean()
    std = d['net_pnl'].std()
    avg_d = d['days_held'].mean()
    sharpe = pnl / std * np.sqrt(252 / avg_d) if std > 0 and avg_d > 0 else 0
    
    print(f"\n{label}:")
    print(f"  Trades: {n}")
    print(f"  Avg P&L: {pnl*100:+.2f}%")
    print(f"  Win rate: {wr*100:.1f}%")
    print(f"  Sharpe: {sharpe:.2f}")
    print(f"  Avg days held: {avg_d:.1f}")

stats(train, "TRAIN (2022-2023)")
stats(test, "TEST (2024-2026)")

# Concentration check on test
print("\n" + "=" * 70)
print("PER-STOCK BREAKDOWN (test set)")
print("=" * 70)
per_stock = test.groupby('ticker').agg(
    n=('net_pnl', 'count'),
    wr=('net_pnl', lambda x: (x > 0).mean()),
    avg_pnl=('net_pnl', 'mean'),
    total=('net_pnl', 'sum')
).reset_index().sort_values('total', ascending=False)

print(f"\n{'Ticker':<18}{'Mkt':<6}{'N':>4}{'WR':>8}{'Avg P&L':>11}{'Total':>10}")
print("-" * 60)
for _, r in per_stock.iterrows():
    market = 'ADX' if r['ticker'].endswith('.AB') else 'DFM'
    print(f"{r['ticker']:<18}{market:<6}{r['n']:>4}{r['wr']*100:>7.1f}%"
          f"{r['avg_pnl']*100:>+10.2f}%{r['total']*100:>+9.1f}%")

# Concentration metric
total = per_stock['total'].sum()
top3 = per_stock.head(3)['total'].sum()
top3_pct = top3 / total * 100 if total > 0 else 0
print(f"\nTop 3 stocks: {top3_pct:.0f}% of total P&L")

# How much do the new ADX stocks contribute?
adx_trades = test[test['ticker'].str.endswith('.AB')]
print(f"\nADX-only contribution:")
print(f"  ADX trades: {len(adx_trades)}")
if len(adx_trades) > 0:
    print(f"  ADX avg P&L: {adx_trades['net_pnl'].mean()*100:+.2f}%")
    print(f"  ADX win rate: {(adx_trades['net_pnl']>0).mean()*100:.1f}%")

# =============================================================================
# COMPARISON TO ORIGINAL 19-STOCK RESULT
# =============================================================================

print("\n" + "=" * 70)
print("COMPARISON: Original 19 DFM-only vs Expanded 21-stock")
print("=" * 70)
print("""
Original (19 DFM, EEM filter):
  Train: n=168, +1.08%, WR 56.5%
  Test:  n=654, +2.40%, WR 67.0%

Expanded (21 stocks above):""")
print(f"  Train: n={len(train)}, {train['net_pnl'].mean()*100:+.2f}%, "
      f"WR {(train['net_pnl']>0).mean()*100:.1f}%" if len(train) > 0 else "  Train: no trades")
print(f"  Test:  n={len(test)}, {test['net_pnl'].mean()*100:+.2f}%, "
      f"WR {(test['net_pnl']>0).mean()*100:.1f}%" if len(test) > 0 else "  Test: no trades")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 70)
print("VERDICT")
print("=" * 70)

if len(train) > 0 and len(test) > 0:
    train_pos = train['net_pnl'].mean() > 0
    test_pos = test['net_pnl'].mean() > 0
    
    if train_pos and test_pos:
        std = test['net_pnl'].std()
        avg_d = test['days_held'].mean()
        sharpe = test['net_pnl'].mean() / std * np.sqrt(252 / avg_d) if std > 0 else 0
        
        print(f"""
✓ BOTH PERIODS POSITIVE — strategy holds on expanded universe
  Train: +{train['net_pnl'].mean()*100:.2f}% (n={len(train)})
  Test: +{test['net_pnl'].mean()*100:.2f}% (n={len(test)}, Sharpe {sharpe:.2f})
  Top 3 concentration: {top3_pct:.0f}%
  
Ready to build Apps Script with 21-stock universe.
""")
    else:
        print(f"""
⚠ Strategy degraded on expanded universe
  Train: {train['net_pnl'].mean()*100:+.2f}%
  Test:  {test['net_pnl'].mean()*100:+.2f}%
  
ADX additions may have hurt. Consider sticking with 19 DFM only.
""")
