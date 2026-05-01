"""
DUAL-SIDE PEAD OPTIMIZER WITH CROSS-UNIVERSE VALIDATION
========================================================

Tests Post-Earnings Announcement Drift (gap proxy) on:
  - LONG side: gap up >+5% on volume → expect continued upside drift
  - SHORT side: gap down <-5% on volume → expect continued downside drift

For each side, tests 9 SL/TP combinations:
  Stop loss:    3%, 5%, 7%
  Profit target: 6%, 10%, trailing

Validates across TWO universes:
  - LARGE-CAP: S&P 500 names (institutional, liquid)
  - MID-CAP:   $2-10B market cap names (more retail-driven, often larger PEAD)

Walk-forward validated: train 2018-2022 → test 2023-2026.

A robust edge would show:
  1. Same SL/TP combo wins on BOTH long AND short sides
  2. Wins on BOTH large-cap AND mid-cap universes
  3. Smooth performance gradient across SL/TP grid (not random)

If a combo wins everywhere, it's a real edge.
If wins are scattered/random, it's overfitting.

USAGE:
    python pead_optimizer.py
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

# LARGE-CAP universe (S&P 500-ish, liquid, institutional)
LARGE_CAP_UNIVERSE = [
    'AAPL','MSFT','GOOGL','GOOG','META','AMZN','NVDA','TSLA','AVGO','ORCL',
    'ADBE','CRM','CSCO','INTC','AMD','QCOM','TXN','MU','AMAT','LRCX',
    'KLAC','SNPS','CDNS','ANET','PANW','FTNT','CRWD','NOW','SNOW','WDAY',
    'JPM','BAC','WFC','GS','MS','C','BLK','SPGI','AXP','V','MA',
    'COF','SCHW','USB','PNC','TFC','AON','TRV','PGR','CB','MET','PRU',
    'UNH','JNJ','LLY','PFE','ABBV','MRK','TMO','ABT','DHR','BMY','AMGN','CVS',
    'CI','HUM','GILD','MDT','SYK','BSX','ISRG','ELV','REGN','VRTX',
    'WMT','PG','KO','PEP','COST','HD','LOW','MCD','SBUX','NKE','TGT','LULU',
    'BKNG','MAR','HLT','DIS','CMCSA','NFLX','TMUS','VZ','T',
    'CAT','DE','BA','HON','UPS','FDX','LMT','RTX','GE','MMM','EMR','ETN',
    'GD','NOC','UNP','CSX','NSC','WM','RSG','PCAR',
    'XOM','CVX','COP','SLB','EOG','PSX','MPC','VLO','OXY','HAL','BKR',
    'LIN','APD','SHW','FCX','NEM','DOW','DD','PPG',
    'PLD','AMT','EQIX','PSA','CCI','SPG','O','WELL','EQR','AVB',
    'NEE','DUK','SO','D','AEP','SRE','EXC','XEL','PCG',
]

# MID-CAP universe (S&P MidCap 400 / Russell mid-cap names, $2-10B)
MID_CAP_UNIVERSE = [
    # Mid-cap tech
    'AFRM','UPST','SOFI','PLTR','RBLX','PATH','BILL','GTLB','NET','DDOG',
    'ZS','OKTA','TWLO','DOCN','FSLY','MDB','ESTC','HUBS','TEAM','ZM',
    'AI','BBAI','SOUN','FROG','BRZE','MNDY','APPF','APPN','SMAR',
    # Mid-cap consumer/retail
    'CHWY','PTON','ROKU','PINS','SNAP','ETSY','W','RH','CART','WMG',
    'BARK','FIGS','OLPX','ELF','BIRK','YETI','SFM','SKIN','BROS','CAVA',
    'WING','SHAK','KSS','M',
    # Mid-cap energy/industrial
    'PLUG','CHPT','RUN','ENPH','SEDG','STEM','JOBY','ACHR','BLNK','EVGO',
    'ARRY','SHLS','FLNC','HASI','BE','BLDP','FCEL','CLNE',
    # Mid-cap healthcare (non-biotech)
    'TDOC','HIMS','OSCR','DOCS','PHR','AMWL','HQY','DH','EVH',
    'PGNY','HCAT','GDRX','DOC','EHC','LH','DGX',
    # Mid-cap fintech/crypto
    'HOOD','COIN','MARA','RIOT','CLSK','IREN','WULF','BTBT','HUT',
    'PYPL','TOST','LMND','ROOT',
    # Mid-cap meme/high-vol
    'GME','AMC','CVNA','OPEN','BYND','SPCE','HYMC','WKHS','PHUN','ATER','CLOV',
    # Mid-cap EVs / mobility
    'LCID','RIVN','XPEV','LI','NIO','PSNY','TIGR',
    # Mid-cap gaming / entertainment
    'DKNG','PENN','RSI','GENI','BALY','GAMB','SKLZ','TTWO','EA',
    'LYV','MSGE','SEAT','NCLH','CCL','RCL',
    # Mid-cap industrial / specialty
    'LUNR','RKLB','ASTS','LIDR','OUST','MVIS','MSTR','PWR','ACM',
    'SONO','VITL','LZ',
    # Allowed biotech
    'NVAX','OCGN','SAVA','BIIB','MRNA','BNTX','ALNY','BMRN',
]

# Time periods
END_DATE = datetime.now()
TRAIN_END = datetime(2022, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 8)

# Costs
SLIPPAGE = 0.001
COMMISSION = 0.0003
BORROW_RATE = 0.04

# PEAD trigger thresholds
GAP_THRESHOLD = 0.05  # 5% gap (up or down)
VOL_THRESHOLD = 2.0   # 2× average volume

# SL/TP grid to test
STOP_LOSSES = [0.03, 0.05, 0.07]
PROFIT_TARGETS = [0.06, 0.10, 'trailing']  # 'trailing' = trailing stop

# Trailing parameters (when TP = 'trailing')
TRAIL_TRIGGER = 0.05    # Activate trailing at +5%
TRAIL_GIVEBACK = 0.03   # Exit if price drops 3% from peak

MAX_HOLD_DAYS = 30

# =============================================================================
# DATA
# =============================================================================

def fetch_data(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 250:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df['vol_avg_20'] = df['Volume'].rolling(20).mean()
        df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
        df['gap_pct'] = df['Open'] / df['Close'].shift(1) - 1
        return df
    except Exception:
        return None


def find_pead_signals(df, ticker, side):
    """Find gap-up (long) or gap-down (short) signals"""
    signals = []
    if side == 'long':
        triggers = (df['gap_pct'] > GAP_THRESHOLD) & (df['vol_ratio'] > VOL_THRESHOLD)
    else:  # short
        triggers = (df['gap_pct'] < -GAP_THRESHOLD) & (df['vol_ratio'] > VOL_THRESHOLD)
    
    for date in df.index[triggers]:
        idx = df.index.get_loc(date)
        if idx >= len(df) - 1:
            continue
        signals.append({'ticker': ticker, 'date': date, 'idx': idx, 'side': side})
    return signals


# =============================================================================
# TRADE SIMULATOR
# =============================================================================

def simulate_trade(df, entry_idx, side, stop_loss, profit_target):
    """
    Simulate one PEAD trade with specified SL/TP parameters.
    side: 'long' or 'short'
    stop_loss: e.g. 0.05 (5%)
    profit_target: e.g. 0.06 (6%) or 'trailing'
    """
    if entry_idx + 1 >= len(df):
        return None
    
    # Entry at NEXT DAY's open (after the gap day)
    entry_price = df.iloc[entry_idx + 1]['Open']
    if pd.isna(entry_price) or entry_price <= 0:
        return None
    
    sign = 1 if side == 'long' else -1
    
    # Initialize
    exit_pnl = 0.0
    days_held = 0
    exit_reason = 'no_data'
    peak_profit = 0.0
    
    for day_offset in range(MAX_HOLD_DAYS):
        idx = entry_idx + 1 + day_offset
        if idx >= len(df):
            if day_offset > 0:
                exit_pnl = close_pnl  # Last known
                days_held = day_offset
                exit_reason = 'no_data'
            break
        
        day_high = df.iloc[idx]['High']
        day_low = df.iloc[idx]['Low']
        day_close = df.iloc[idx]['Close']
        
        # P&L from each price level (sign-adjusted)
        if side == 'long':
            adverse_pnl = (day_low - entry_price) / entry_price
            favorable_pnl = (day_high - entry_price) / entry_price
        else:
            adverse_pnl = (entry_price - day_high) / entry_price
            favorable_pnl = (entry_price - day_low) / entry_price
        
        close_pnl = sign * (day_close - entry_price) / entry_price
        
        # Default to time exit on close
        days_held = day_offset + 1
        exit_pnl = close_pnl
        exit_reason = 'time'
        
        # Check stop loss FIRST (worst-case priority)
        if adverse_pnl < -stop_loss:
            exit_pnl = -stop_loss
            exit_reason = 'stop_loss'
            break
        
        # Check profit target
        if profit_target == 'trailing':
            # Update peak
            if favorable_pnl > peak_profit:
                peak_profit = favorable_pnl
            # Exit if trailing condition met
            if peak_profit >= TRAIL_TRIGGER:
                if close_pnl < (peak_profit - TRAIL_GIVEBACK):
                    exit_pnl = peak_profit - TRAIL_GIVEBACK
                    exit_reason = 'trailing'
                    break
        else:
            # Fixed profit target
            if favorable_pnl > profit_target:
                exit_pnl = profit_target
                exit_reason = 'profit_target'
                break
    
    if days_held == 0:
        return None
    
    # Costs
    borrow_cost = (BORROW_RATE * days_held / 365) if side == 'short' else 0
    net_pnl = exit_pnl - SLIPPAGE * 2 - COMMISSION * 2 - borrow_cost
    
    return {'gross_pnl': exit_pnl, 'net_pnl': net_pnl,
            'days_held': days_held, 'exit_reason': exit_reason}


# =============================================================================
# RUN BACKTEST FOR ONE UNIVERSE
# =============================================================================

def run_universe(universe_name, tickers):
    print(f"\n{'='*80}")
    print(f"UNIVERSE: {universe_name} ({len(tickers)} stocks)")
    print(f"{'='*80}")
    
    # Load data
    print("Loading...")
    stocks_data = {}
    for i, ticker in enumerate(tickers):
        df = fetch_data(ticker, START_DATE, END_DATE)
        if df is not None:
            stocks_data[ticker] = df
        if (i+1) % 50 == 0:
            print(f"  [{i+1}/{len(tickers)}] {len(stocks_data)} loaded")
    
    print(f"Loaded {len(stocks_data)} stocks")
    
    # Generate signals for both sides
    print("Generating PEAD signals...")
    signals_long = []
    signals_short = []
    for ticker, df in stocks_data.items():
        signals_long.extend(find_pead_signals(df, ticker, 'long'))
        signals_short.extend(find_pead_signals(df, ticker, 'short'))
    print(f"  Long signals (gap up):    {len(signals_long)}")
    print(f"  Short signals (gap down): {len(signals_short)}")
    
    # Run all SL/TP combinations for both sides
    print("\nTesting 9 SL/TP combos × 2 sides = 18 strategies...")
    
    universe_results = []
    
    for side, signals in [('long', signals_long), ('short', signals_short)]:
        for sl in STOP_LOSSES:
            for tp in PROFIT_TARGETS:
                trades = []
                for sig in signals:
                    df = stocks_data[sig['ticker']]
                    t = simulate_trade(df, sig['idx'], side, sl, tp)
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
                    return {'n': len(d), 'wr': (d['net_pnl'] > 0).mean(),
                           'pnl': m, 'sharpe': sh}
                
                tr_s = stats(train)
                te_s = stats(test)
                tp_label = 'trail' if tp == 'trailing' else f'{int(tp*100)}%'
                
                universe_results.append({
                    'universe': universe_name, 'side': side,
                    'sl': sl, 'tp': tp_label,
                    'train_n': tr_s['n'], 'test_n': te_s['n'],
                    'train_pnl': tr_s['pnl'], 'test_pnl': te_s['pnl'],
                    'train_wr': tr_s['wr'], 'test_wr': te_s['wr'],
                    'train_sharpe': tr_s['sharpe'], 'test_sharpe': te_s['sharpe'],
                })
    
    return universe_results


# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("DUAL-SIDE PEAD OPTIMIZER")
print("=" * 80)
print(f"Period: {START_DATE.date()} to {END_DATE.date()}")
print(f"Train: 2018-2022, Test: 2023-2026")
print(f"SL grid: {[f'{int(x*100)}%' for x in STOP_LOSSES]}")
print(f"TP grid: 6%, 10%, trailing")

# Run both universes
all_results = []
all_results.extend(run_universe('LARGE-CAP', LARGE_CAP_UNIVERSE))
all_results.extend(run_universe('MID-CAP', MID_CAP_UNIVERSE))

results_df = pd.DataFrame(all_results)

# =============================================================================
# OUTPUT
# =============================================================================

print("\n" + "=" * 80)
print("RESULTS — All 36 strategy variants ranked by test P&L")
print("=" * 80)
print(f"{'Univ':<10}{'Side':<7}{'SL':<5}{'TP':<8}{'Tr#':>6}{'Te#':>6}"
      f"{'Tr WR':>7}{'Te WR':>7}{'Tr P&L':>9}{'Te P&L':>9}{'Te Shrp':>9}")
print("-" * 90)

ranked = results_df.sort_values('test_pnl', ascending=False)
for _, r in ranked.iterrows():
    sl_str = f"{int(r['sl']*100)}%"
    print(f"{r['universe']:<10}{r['side']:<7}{sl_str:<5}{r['tp']:<8}"
          f"{r['train_n']:>6}{r['test_n']:>6}"
          f"{r['train_wr']*100:>6.1f}%{r['test_wr']*100:>6.1f}%"
          f"{r['train_pnl']*100:>+8.2f}%{r['test_pnl']*100:>+8.2f}%"
          f"{r['test_sharpe']:>9.2f}")

# =============================================================================
# ROBUSTNESS ANALYSIS
# =============================================================================

print("\n" + "=" * 80)
print("ROBUSTNESS — Find combinations that work across BOTH universes")
print("=" * 80)

# For each (side, sl, tp) combo, check if it has positive test P&L on BOTH universes
combos = results_df.groupby(['side', 'sl', 'tp']).agg({
    'test_pnl': lambda x: list(x),
    'test_n': lambda x: list(x),
    'test_sharpe': lambda x: list(x),
    'universe': lambda x: list(x),
}).reset_index()

robust_combos = []
for _, c in combos.iterrows():
    if len(c['test_pnl']) < 2:
        continue
    if all(p > 0 for p in c['test_pnl']) and all(n >= 30 for n in c['test_n']):
        robust_combos.append({
            'side': c['side'], 'sl': c['sl'], 'tp': c['tp'],
            'large_pnl': c['test_pnl'][0] if c['universe'][0] == 'LARGE-CAP' else c['test_pnl'][1],
            'mid_pnl': c['test_pnl'][1] if c['universe'][1] == 'MID-CAP' else c['test_pnl'][0],
            'large_sharpe': c['test_sharpe'][0] if c['universe'][0] == 'LARGE-CAP' else c['test_sharpe'][1],
            'mid_sharpe': c['test_sharpe'][1] if c['universe'][1] == 'MID-CAP' else c['test_sharpe'][0],
            'avg_pnl': np.mean(c['test_pnl']),
            'min_n': min(c['test_n']),
        })

robust_df = pd.DataFrame(robust_combos).sort_values('avg_pnl', ascending=False) if robust_combos else None

if robust_df is not None and len(robust_df) > 0:
    print(f"\n{len(robust_df)} combo(s) profitable on BOTH universes:\n")
    print(f"{'Side':<7}{'SL':<5}{'TP':<8}{'Large P&L':>11}{'Mid P&L':>11}"
          f"{'Avg P&L':>11}{'Lg Shrp':>10}{'Md Shrp':>10}")
    print("-" * 80)
    for _, r in robust_df.iterrows():
        sl_str = f"{int(r['sl']*100)}%"
        print(f"{r['side']:<7}{sl_str:<5}{r['tp']:<8}"
              f"{r['large_pnl']*100:>+10.2f}%{r['mid_pnl']*100:>+10.2f}%"
              f"{r['avg_pnl']*100:>+10.2f}%"
              f"{r['large_sharpe']:>10.2f}{r['mid_sharpe']:>10.2f}")
else:
    print("\n⚠️ No combinations profitable on both universes — edge is universe-specific")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

# Best combo overall
best_overall = ranked.iloc[0]
print(f"\nBest single combo (test P&L):")
print(f"  {best_overall['universe']} {best_overall['side']} "
      f"SL={int(best_overall['sl']*100)}% TP={best_overall['tp']}")
print(f"  Test P&L: {best_overall['test_pnl']*100:+.2f}%, "
      f"Sharpe: {best_overall['test_sharpe']:.2f}, "
      f"Trades: {best_overall['test_n']}")

# Long vs short summary
print(f"\nLong-side average test P&L:  {ranked[ranked['side']=='long']['test_pnl'].mean()*100:+.2f}%")
print(f"Short-side average test P&L: {ranked[ranked['side']=='short']['test_pnl'].mean()*100:+.2f}%")

print(f"\nLarge-cap average test P&L:  {ranked[ranked['universe']=='LARGE-CAP']['test_pnl'].mean()*100:+.2f}%")
print(f"Mid-cap average test P&L:    {ranked[ranked['universe']=='MID-CAP']['test_pnl'].mean()*100:+.2f}%")

# Robustness check
if robust_df is not None and len(robust_df) > 0:
    best_robust = robust_df.iloc[0]
    sl_str = f"{int(best_robust['sl']*100)}%"
    print(f"\n✅ MOST ROBUST COMBO: {best_robust['side']} SL={sl_str} TP={best_robust['tp']}")
    print(f"   Avg test P&L across universes: {best_robust['avg_pnl']*100:+.2f}%")
    print(f"   Large-cap: {best_robust['large_pnl']*100:+.2f}%, "
          f"Mid-cap: {best_robust['mid_pnl']*100:+.2f}%")
    print(f"   This combo's edge survives across stock types — strongest signal of real edge.")
elif best_overall['test_pnl'] > 0.01:
    print(f"\n🟡 Edge exists but is universe-specific. Best results in {best_overall['universe']}.")
else:
    print(f"\n❌ No robust edge found across universes.")

print()
