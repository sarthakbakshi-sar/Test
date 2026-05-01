"""
FINAL VALIDATION TEST
=====================
Tests Setup A with:
  • Single filter: F2_vol_above_8x (volume >= 8× 20-day average)
  • Earnings exclusion: skip trades within 5 days of any earnings date
  • Full walk-forward: train 2018-2022, test 2023-2026
  • Equity curve simulation on out-of-sample data

This is the cleanest, highest-statistical-power test of the thesis.
After this run, the answer is "deploy" or "kill" — no more iteration.

USAGE:
    python final_validation.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# Universe
UNIVERSE = [
    'AFRM','UPST','SOFI','PLTR','RBLX','PATH','BILL','GTLB','NET','DDOG','ZS',
    'CRWD','OKTA','TWLO','DOCN','FSLY','MDB','ESTC','HUBS','TEAM','NOW','SNOW',
    'WDAY','VEEV','TYL','PCTY','PAYC','ZM','RNG','FIVN','BAND','CFLT',
    'AI','BBAI','SOUN','CXM','FROG','BRZE','GLBE','MNDY','APPF','APPN',
    'CHWY','PTON','WBD','ROKU','PINS','SNAP','ETSY','W','RH','REAL','CART',
    'WMG','ABNB','UBER','LYFT','DASH','BARK','FIGS','OLPX','ELF','BIRK',
    'YETI','SFM','SKIN','BROS','CAVA','WING','SHAK','CMG','KSS','M',
    'PLUG','CHPT','RUN','ENPH','SEDG','STEM','JOBY','ACHR','BLNK','EVGO','ARRY',
    'SHLS','FLNC','HASI','BE','BLDP','FCEL','CLNE','AMTX',
    'TDOC','HIMS','OSCR','DOCS','PHR','AMWL','HQY','DH','EVH',
    'PGNY','CCRN','HCAT','SDGR','GDRX','DOC','MD','EHC','LH','DGX',
    'HOOD','COIN','MARA','RIOT','CLSK','IREN','WULF','BTBT','HUT','BTDR',
    'CIFR','APLD','APP','PYPL','MQ','TOST','LMND','ROOT',
    'GME','AMC','CVNA','OPEN','BYND','SPCE','BBBY','NEGG','HYMC','WKHS',
    'PHUN','ATER','CLOV',
    'LCID','RIVN','XPEV','LI','NIO','PSNY','VFS','TIGR','LU',
    'DKNG','PENN','RSI','GENI','BALY','GAMB','SKLZ','TTWO','EA',
    'LYV','MSGE','SEAT','NCLH','CCL','RCL','DIS',
    'LUNR','RKLB','ASTS','LIDR','OUST','MVIS','MSTR','PWR','ACM',
    'NUS','BRBR','SG','SONO','VITL','NRDS','LZ',
    'NVAX','OCGN','SAVA','BIIB','VRTX','MRNA','BNTX','REGN','ALNY','BMRN',
    'PCOR','ZLAB','FUTU','UP','ALLY',
]
UNIVERSE = list(set(UNIVERSE))

END_DATE = datetime.now()
TRAIN_END = datetime(2022, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 8)

# Setup A
RET_5D_THRESHOLD = 0.15
VOL_RATIO_THRESHOLD = 3.0
EXTENSION_THRESHOLD = 0.10
MIN_AVG_DOLLAR_VOLUME = 5_000_000

# THE ONE FILTER
EXTREME_VOL_MULTIPLIER = 8.0  # F2_vol_above_8x

# Earnings exclusion window (days before AND after earnings date)
EARNINGS_BUFFER_DAYS = 5

# Costs
SLIPPAGE = 0.002
COMMISSION = 0.0005
BORROW_NORMAL = 0.05
BORROW_HIGH = 0.20

# Stops
STOP_LOSS_PCT = 0.05
TRAILING_TRIGGER = 0.04
TRAILING_GIVEBACK = 0.025
MAX_HOLD_DAYS = 7

HIGH_BORROW = {'GME','AMC','BBBY','NEGG','HYMC','WKHS','PHUN','ATER','CLOV',
               'BYND','NVAX','OCGN','SAVA','SPCE','CVNA','CHWY','BTBT','OPEN'}


def fetch_data(ticker, start, end):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 250:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        avg_dollar_vol = (df['Close'] * df['Volume']).rolling(20).mean()
        df['liquid'] = avg_dollar_vol > MIN_AVG_DOLLAR_VOLUME
        return df
    except Exception:
        return None


def fetch_earnings_dates(ticker):
    """Get historical earnings dates for a ticker. Returns set of dates."""
    try:
        t = yf.Ticker(ticker)
        # earnings_dates is a DataFrame indexed by datetime
        ed = t.earnings_dates
        if ed is None or len(ed) == 0:
            return set()
        dates = set()
        for ts in ed.index:
            if pd.isna(ts):
                continue
            dates.add(pd.Timestamp(ts).normalize().tz_localize(None) if ts.tz else pd.Timestamp(ts).normalize())
        return dates
    except Exception:
        return set()


def is_near_earnings(trigger_date, earnings_set, buffer_days):
    """Check if trigger_date is within buffer_days of any earnings date"""
    if not earnings_set:
        return False
    trigger_naive = pd.Timestamp(trigger_date).normalize()
    if trigger_naive.tz is not None:
        trigger_naive = trigger_naive.tz_localize(None)
    for ed in earnings_set:
        delta = abs((trigger_naive - ed).days)
        if delta <= buffer_days:
            return True
    return False


def compute_signals(df):
    df = df.copy()
    df['ret_5d'] = df['Close'].pct_change(5)
    df['vol_avg_20'] = df['Volume'].rolling(20).mean()
    df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
    df['sma_20'] = df['Close'].rolling(20).mean()
    df['extension_20'] = (df['Close'] - df['sma_20']) / df['sma_20']
    
    # Setup A
    df['trigger_A'] = (
        (df['ret_5d'] > RET_5D_THRESHOLD) &
        (df['vol_ratio'] > VOL_RATIO_THRESHOLD) &
        (df['extension_20'] > EXTENSION_THRESHOLD) &
        (df['liquid'])
    )
    
    # Setup A+: also requires extreme volume
    df['trigger_Aplus'] = df['trigger_A'] & (df['vol_ratio'] > EXTREME_VOL_MULTIPLIER)
    
    return df


def simulate_short_trade(df, entry_idx, borrow_rate):
    entry_price = df.iloc[entry_idx]['Close']
    sim_start = entry_idx + 1
    
    exit_pnl = 0.0
    days_held = 0
    exit_reason = 'no_data'
    peak_profit_pct = 0
    close_pnl_pct = 0.0
    
    for day_offset in range(MAX_HOLD_DAYS):
        idx = sim_start + day_offset
        if idx >= len(df):
            if day_offset > 0:
                exit_pnl = close_pnl_pct
                days_held = day_offset
                exit_reason = 'no_data'
            break
        
        day_high = df.iloc[idx]['High']
        day_low = df.iloc[idx]['Low']
        day_close = df.iloc[idx]['Close']
        
        worst_pnl_pct = (entry_price - day_high) / entry_price
        best_pnl_pct = (entry_price - day_low) / entry_price
        close_pnl_pct = (entry_price - day_close) / entry_price
        
        days_held = day_offset + 1
        exit_pnl = close_pnl_pct
        exit_reason = 'time'
        
        if worst_pnl_pct < -STOP_LOSS_PCT:
            exit_pnl = -STOP_LOSS_PCT
            exit_reason = 'stop_loss'
            break
        if best_pnl_pct > peak_profit_pct:
            peak_profit_pct = best_pnl_pct
        if peak_profit_pct >= TRAILING_TRIGGER:
            if close_pnl_pct < (peak_profit_pct - TRAILING_GIVEBACK):
                exit_pnl = peak_profit_pct - TRAILING_GIVEBACK
                exit_reason = 'trailing'
                break
    
    if days_held == 0:
        return None
    
    borrow_cost = borrow_rate * (days_held / 365)
    net_pnl = exit_pnl - borrow_cost - SLIPPAGE * 2 - COMMISSION * 2
    return {'gross_pnl': exit_pnl, 'net_pnl': net_pnl, 'days_held': days_held,
            'exit_reason': exit_reason}


# =============================================================================
# RUN
# =============================================================================

print("=" * 80)
print("FINAL VALIDATION TEST")
print("=" * 80)
print(f"Period: 2018 → 2026 (split at end of 2022)")
print(f"Universe: {len(UNIVERSE)} stocks")
print()
print("Strategies tested:")
print(f"  1. Setup A          — base trigger (ret>15% + vol>3× + ext>10%)")
print(f"  2. Setup A + extreme vol  — adds vol > {EXTREME_VOL_MULTIPLIER}× filter")
print(f"  3. Setup A + extreme vol + no earnings — adds earnings exclusion")
print()

# Fetch all stock data
print("Fetching price data...")
stocks_data = {}
for i, ticker in enumerate(UNIVERSE):
    df = fetch_data(ticker, START_DATE, END_DATE)
    if df is None:
        continue
    stocks_data[ticker] = df
    if (i+1) % 50 == 0:
        print(f"  [{i+1}/{len(UNIVERSE)}] {len(stocks_data)} loaded")

print(f"\nLoaded {len(stocks_data)} stocks")

# Fetch earnings dates (slower, separate API call per ticker)
print("\nFetching earnings calendars (this takes a few minutes)...")
earnings_data = {}
for i, ticker in enumerate(stocks_data.keys()):
    earnings_data[ticker] = fetch_earnings_dates(ticker)
    if (i+1) % 50 == 0:
        n_with_earnings = sum(1 for v in earnings_data.values() if v)
        print(f"  [{i+1}/{len(stocks_data)}] earnings data for {n_with_earnings} stocks")

n_with_earnings = sum(1 for v in earnings_data.values() if v)
print(f"\nGot earnings calendars for {n_with_earnings} stocks")

# Generate trades for all 3 strategies
print("\nGenerating trades...")
trades_A = []        # Setup A only
trades_Aplus = []    # Setup A + extreme volume
trades_Afinal = []   # Setup A + extreme volume + no earnings

for ticker, df in stocks_data.items():
    df_sig = compute_signals(df)
    borrow = BORROW_HIGH if ticker in HIGH_BORROW else BORROW_NORMAL
    earnings_set = earnings_data.get(ticker, set())
    
    # Setup A
    for date in df_sig.index[df_sig['trigger_A']]:
        idx = df_sig.index.get_loc(date)
        t = simulate_short_trade(df_sig, idx, borrow)
        if t:
            t['ticker'] = ticker; t['date'] = date
            trades_A.append(t)
    
    # Setup A+
    for date in df_sig.index[df_sig['trigger_Aplus']]:
        idx = df_sig.index.get_loc(date)
        t = simulate_short_trade(df_sig, idx, borrow)
        if t:
            t['ticker'] = ticker; t['date'] = date
            trades_Aplus.append(t)
            
            # Setup A_final: also exclude earnings
            if not is_near_earnings(date, earnings_set, EARNINGS_BUFFER_DAYS):
                trades_Afinal.append(t.copy())

# Summarize
def summarize(trades, label):
    if not trades:
        return None
    df = pd.DataFrame(trades)
    df['date'] = pd.to_datetime(df['date'])
    if df['date'].dt.tz is not None:
        df['date'] = df['date'].dt.tz_localize(None)
    
    train = df[df['date'] <= TRAIN_END]
    test = df[df['date'] > TRAIN_END]
    
    return {
        'label': label,
        'total': len(df),
        'train_n': len(train),
        'test_n': len(test),
        'train_wr': train['net_pnl'].apply(lambda x: x > 0).mean() if len(train) > 0 else 0,
        'test_wr':  test['net_pnl'].apply(lambda x: x > 0).mean() if len(test) > 0 else 0,
        'train_pnl': train['net_pnl'].mean() if len(train) > 0 else 0,
        'test_pnl':  test['net_pnl'].mean() if len(test) > 0 else 0,
        'test_median': test['net_pnl'].median() if len(test) > 0 else 0,
        'test_worst': test['net_pnl'].min() if len(test) > 0 else 0,
        'test_best': test['net_pnl'].max() if len(test) > 0 else 0,
        'test_std': test['net_pnl'].std() if len(test) > 0 else 0,
        'test_df': test,  # For equity curve
    }

s_A = summarize(trades_A, 'Setup A')
s_Aplus = summarize(trades_Aplus, 'Setup A + extreme vol')
s_Afinal = summarize(trades_Afinal, 'Setup A + extreme vol + no earnings')

# =============================================================================
# OUTPUT
# =============================================================================

print("\n" + "=" * 80)
print("RESULTS — Train vs Test Comparison")
print("=" * 80)
print()
print(f"{'Strategy':<40}{'Train trades':>14}{'Test trades':>14}")
print("-" * 80)
for s in [s_A, s_Aplus, s_Afinal]:
    if s:
        print(f"{s['label']:<40}{s['train_n']:>14}{s['test_n']:>14}")

print()
print(f"{'Strategy':<40}{'Train WR':>12}{'Test WR':>12}{'WR drop':>11}")
print("-" * 80)
for s in [s_A, s_Aplus, s_Afinal]:
    if s:
        drop = (s['train_wr'] - s['test_wr']) * 100
        print(f"{s['label']:<40}{s['train_wr']*100:>11.1f}%{s['test_wr']*100:>11.1f}%"
              f"{drop:>+10.1f}pp")

print()
print(f"{'Strategy':<40}{'Train P&L':>12}{'Test P&L':>12}{'P&L drop':>11}")
print("-" * 80)
for s in [s_A, s_Aplus, s_Afinal]:
    if s:
        drop = (s['train_pnl'] - s['test_pnl']) * 100
        print(f"{s['label']:<40}{s['train_pnl']*100:>+11.2f}%{s['test_pnl']*100:>+11.2f}%"
              f"{drop:>+10.2f}pp")

# Test set details
print("\n" + "=" * 80)
print("OUT-OF-SAMPLE DETAILS (2023-2026)")
print("=" * 80)
for s in [s_A, s_Aplus, s_Afinal]:
    if s and s['test_n'] > 0:
        print(f"\n{s['label']} (n={s['test_n']}):")
        print(f"  Win rate: {s['test_wr']*100:.1f}%")
        print(f"  Avg P&L: {s['test_pnl']*100:+.2f}%")
        print(f"  Median P&L: {s['test_median']*100:+.2f}%")
        print(f"  Best trade: {s['test_best']*100:+.2f}%")
        print(f"  Worst trade: {s['test_worst']*100:+.2f}%")
        print(f"  Std dev: {s['test_std']*100:.2f}%")

# Equity curve for the best strategy
print("\n" + "=" * 80)
print("EQUITY CURVE — $50k starting, $15k per trade (out-of-sample only)")
print("=" * 80)

for s in [s_A, s_Aplus, s_Afinal]:
    if not s or s['test_n'] < 10:
        continue
    
    test_sorted = s['test_df'].sort_values('date')
    STARTING_CAPITAL = 50000
    POSITION_SIZE = 15000
    
    equity = [STARTING_CAPITAL]
    dates_eq = [test_sorted['date'].min()]
    for _, t in test_sorted.iterrows():
        equity.append(equity[-1] + POSITION_SIZE * t['net_pnl'])
        dates_eq.append(t['date'])
    
    peak = STARTING_CAPITAL
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        dd = (e - peak) / peak
        max_dd = min(max_dd, dd)
    
    final = equity[-1]
    total_return = (final - STARTING_CAPITAL) / STARTING_CAPITAL
    years = (test_sorted['date'].max() - test_sorted['date'].min()).days / 365
    cagr = (final / STARTING_CAPITAL) ** (1/years) - 1 if years > 0 and final > 0 else 0
    
    # Streak analysis
    pnls = test_sorted['net_pnl'].tolist()
    longest_lose_streak = 0
    current_streak = 0
    for p in pnls:
        if p <= 0:
            current_streak += 1
            longest_lose_streak = max(longest_lose_streak, current_streak)
        else:
            current_streak = 0
    
    print(f"\n{s['label']}:")
    print(f"  Trades:         {s['test_n']}")
    print(f"  Years:          {years:.1f}")
    print(f"  Trades/year:    {s['test_n']/years:.0f}")
    print(f"  Final equity:   ${final:,.0f}")
    print(f"  Total return:   {total_return*100:+.1f}%")
    print(f"  CAGR:           {cagr*100:+.1f}%")
    print(f"  Max drawdown:   {max_dd*100:.1f}%")
    print(f"  Longest losing streak: {longest_lose_streak} trades")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

# Best strategy by test P&L
candidates = [s for s in [s_A, s_Aplus, s_Afinal] if s and s['test_n'] >= 30]
if candidates:
    best = max(candidates, key=lambda s: s['test_pnl'])
    
    print(f"\nBest out-of-sample: {best['label']}")
    print(f"  Test trades: {best['test_n']}")
    print(f"  Test win rate: {best['test_wr']*100:.1f}%")
    print(f"  Test avg P&L: {best['test_pnl']*100:+.2f}%")
    
    # Statistical significance check
    if best['test_n'] >= 30 and best['test_std'] > 0:
        t_stat = best['test_pnl'] / (best['test_std'] / np.sqrt(best['test_n']))
        print(f"  T-statistic: {t_stat:.2f} (need >2 for ~95% confidence)")
    
    # Final call
    if best['test_pnl'] > 0.02 and best['test_wr'] > 0.45 and best['test_n'] >= 50:
        wr_drop = (best['train_wr'] - best['test_wr']) * 100
        if wr_drop < 10:
            print(f"\n✅ EDGE CONFIRMED OUT-OF-SAMPLE")
            print(f"   System is ready for paper trading.")
            print(f"   Expected at $50k account: ~${50000 * best['test_pnl'] * (best['test_n'] / 3):,.0f}/year")
        else:
            print(f"\n🟡 EDGE WEAKER OUT-OF-SAMPLE")
            print(f"   WR dropped {wr_drop:.1f}pp from train. Real but smaller than hoped.")
    elif best['test_pnl'] > 0:
        print(f"\n🟠 MARGINAL — barely positive on test set")
        print(f"   Live deployment risky. Sample size or P&L may be insufficient.")
    else:
        print(f"\n❌ NO EDGE — best strategy lost money out-of-sample")
        print(f"   Thesis is dead. Time to stop iterating and accept the answer.")
else:
    print(f"\n⚠️ Not enough out-of-sample trades to validate any strategy")

print()
