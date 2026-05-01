"""
WALK-FORWARD BACKTESTER + FEATURE ANALYSIS
===========================================
Goal: improve win rate from 42% → higher, but only with filters that survive
out-of-sample validation. Anything else is overfitting.

What this script does:

PHASE 1 — Feature Engineering
  Compute 12+ candidate filters that might predict short-trade success.
  Examples: market regime, recent volatility, day-of-week, gap %, prior trend.

PHASE 2 — Train/Test Split
  Train period: 2018-2022 (5 years) — find which filters work
  Test period:  2023-2026 (3 years) — verify they still work

PHASE 3 — Single-Feature Analysis
  For each candidate filter, measure win-rate lift on training set.
  Rank features by predictive power.

PHASE 4 — Build Filtered Strategy
  Take top 3-5 filters that show real edge.
  Combine them into "Setup A+" — only trade when filters confirm.

PHASE 5 — Out-of-Sample Validation
  Run filtered strategy on 2023-2026 (unseen data).
  If win rate holds, real improvement. If it collapses, overfit.

PHASE 6 — Equity Curve + Drawdown
  Simulate $50k account trading the filtered strategy.
  Show equity curve, max drawdown, monthly returns.

OUTPUT:
  Realistic deployment-ready answer to "is this worth trading."
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# Same universe as before
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
    'LCID','RIVN','NKLA','XPEV','LI','NIO','PSNY','VFS','TIGR','LU',
    'DKNG','PENN','RSI','GENI','BALY','GAMB','SKLZ','TTWO','EA',
    'LYV','MSGE','SEAT','NCLH','CCL','RCL','DIS',
    'LUNR','RKLB','ASTS','LIDR','OUST','MVIS','MSTR','PWR','ACM',
    'NUS','BRBR','SG','SONO','VITL','NRDS','LZ',
    'NVAX','OCGN','SAVA','BIIB','VRTX','MRNA','BNTX','REGN','ALNY','BMRN',
    'PCOR','ZLAB','FUTU','UP','ALLY',
]
UNIVERSE = list(set(UNIVERSE))

# Period split
END_DATE = datetime.now()
TRAIN_END = datetime(2022, 12, 31)
TEST_START = datetime(2023, 1, 1)
START_DATE = END_DATE - timedelta(days=365 * 8)

# Setup A
RET_5D_THRESHOLD = 0.15
VOL_RATIO_THRESHOLD = 3.0
EXTENSION_THRESHOLD = 0.10
MIN_AVG_DOLLAR_VOLUME = 5_000_000

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
               'BYND','NKLA','NVAX','OCGN','SAVA','SPCE','CVNA','CHWY','BTBT','OPEN'}


# =============================================================================
# DATA + FEATURES
# =============================================================================

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


def compute_features(df, spy_df=None):
    """Compute all candidate features for filter analysis"""
    df = df.copy()
    
    # Core trigger components
    df['ret_5d'] = df['Close'].pct_change(5)
    df['ret_10d'] = df['Close'].pct_change(10)
    df['ret_20d'] = df['Close'].pct_change(20)
    df['ret_1d'] = df['Close'].pct_change(1)
    
    df['vol_avg_20'] = df['Volume'].rolling(20).mean()
    df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
    
    df['sma_20'] = df['Close'].rolling(20).mean()
    df['sma_50'] = df['Close'].rolling(50).mean()
    df['extension_20'] = (df['Close'] - df['sma_20']) / df['sma_20']
    df['extension_50'] = (df['Close'] - df['sma_50']) / df['sma_50']
    
    # CANDIDATE FILTERS — these are the features we'll test
    
    # F1: Magnitude of recent move (bigger run = more exhaustion?)
    df['F1_ret5d_above_25'] = df['ret_5d'] > 0.25
    df['F1_ret5d_above_35'] = df['ret_5d'] > 0.35
    
    # F2: Volume intensity (extreme volume = climax?)
    df['F2_vol_above_5x'] = df['vol_ratio'] > 5.0
    df['F2_vol_above_8x'] = df['vol_ratio'] > 8.0
    
    # F3: Price extension (more extended = more reversion?)
    df['F3_ext_above_20'] = df['extension_20'] > 0.20
    df['F3_ext_above_30'] = df['extension_20'] > 0.30
    
    # F4: Gap up on trigger day (gap = euphoria peak?)
    df['gap_pct'] = (df['Open'] / df['Close'].shift(1) - 1)
    df['F4_gap_up_5'] = df['gap_pct'] > 0.05
    df['F4_gap_up_10'] = df['gap_pct'] > 0.10
    
    # F5: Closed weak (intraday weakness = reversal already started?)
    df['day_range'] = df['High'] - df['Low']
    df['close_pos'] = np.where(df['day_range'] > 0,
                               (df['Close'] - df['Low']) / df['day_range'], 0.5)
    df['F5_closed_low_30'] = df['close_pos'] < 0.30
    df['F5_closed_low_50'] = df['close_pos'] < 0.50
    
    # F6: Multi-day green streak (parabolic move?)
    df['green_day'] = df['Close'] > df['Close'].shift(1)
    df['green_streak'] = df['green_day'].rolling(5).sum()
    df['F6_streak_4plus'] = df['green_streak'] >= 4
    df['F6_streak_5plus'] = df['green_streak'] >= 5
    
    # F7: Volatility regime (high vol = more reversal?)
    df['atr_20'] = df['Close'].pct_change().rolling(20).std()
    df['F7_high_vol'] = df['atr_20'] > df['atr_20'].rolling(60).quantile(0.7)
    df['F7_low_vol'] = df['atr_20'] < df['atr_20'].rolling(60).quantile(0.3)
    
    # F8: Day of week (Monday/Friday effects?)
    df['F8_monday'] = df.index.dayofweek == 0
    df['F8_friday'] = df.index.dayofweek == 4
    df['F8_midweek'] = df.index.dayofweek.isin([1, 2, 3])
    
    # F9: Market regime (SPY trend) — requires SPY data
    if spy_df is not None:
        spy_aligned = spy_df.reindex(df.index, method='ffill')
        df['spy_above_sma50'] = spy_aligned['Close'] > spy_aligned['Close'].rolling(50).mean()
        df['spy_above_sma200'] = spy_aligned['Close'] > spy_aligned['Close'].rolling(200).mean()
        df['F9_market_uptrend'] = df['spy_above_sma50'] & df['spy_above_sma200']
        df['F9_market_downtrend'] = ~df['spy_above_sma50']
        df['F9_market_choppy'] = df['spy_above_sma50'] & ~df['spy_above_sma200']
    
    # F10: RSI (overbought?)
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    df['F10_rsi_above_75'] = df['rsi_14'] > 75
    df['F10_rsi_above_85'] = df['rsi_14'] > 85
    
    # F11: Prior context (was it sleepy before the run?)
    df['prior_20d_range'] = (df['Close'].rolling(20).max().shift(5) - 
                              df['Close'].rolling(20).min().shift(5)) / df['Close'].shift(5)
    df['F11_was_sleepy'] = df['prior_20d_range'] < 0.15
    df['F11_was_active'] = df['prior_20d_range'] > 0.30
    
    # F12: Distance from 52-week high
    df['high_52w'] = df['Close'].rolling(252).max()
    df['near_52w_high'] = df['Close'] >= df['high_52w'] * 0.95
    df['F12_at_52w_high'] = df['near_52w_high']
    
    # Trigger
    df['trigger'] = (
        (df['ret_5d'] > RET_5D_THRESHOLD) &
        (df['vol_ratio'] > VOL_RATIO_THRESHOLD) &
        (df['extension_20'] > EXTENSION_THRESHOLD) &
        (df['liquid'])
    )
    
    return df


# =============================================================================
# TRADE SIMULATOR
# =============================================================================

def simulate_short_trade(df, entry_idx, borrow_rate):
    """Same as before — close entry with stop+trailing exits"""
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
print("WALK-FORWARD BACKTESTER + FEATURE ANALYSIS")
print("=" * 80)
print(f"Train period: 2018 → 2022")
print(f"Test period:  2023 → present")
print(f"Universe:     {len(UNIVERSE)} stocks")
print()

# Fetch SPY for market regime
print("Fetching SPY for market regime context...")
spy = fetch_data('SPY', START_DATE, END_DATE)
if spy is None:
    print("WARNING: SPY fetch failed, market regime features disabled")

# Fetch all stocks
print("Fetching universe data...")
stocks_data = {}
skipped = 0
for i, ticker in enumerate(UNIVERSE):
    df = fetch_data(ticker, START_DATE, END_DATE)
    if df is None:
        skipped += 1
        continue
    stocks_data[ticker] = df
    if (i+1) % 50 == 0:
        print(f"  [{i+1}/{len(UNIVERSE)}] loaded {len(stocks_data)}, skipped {skipped}")

print(f"\nLoaded {len(stocks_data)} stocks")

# Build full trade dataset with all features
print("\nBuilding trade dataset with features...")
all_trades = []
feature_cols = [c for c in [
    'F1_ret5d_above_25','F1_ret5d_above_35',
    'F2_vol_above_5x','F2_vol_above_8x',
    'F3_ext_above_20','F3_ext_above_30',
    'F4_gap_up_5','F4_gap_up_10',
    'F5_closed_low_30','F5_closed_low_50',
    'F6_streak_4plus','F6_streak_5plus',
    'F7_high_vol','F7_low_vol',
    'F8_monday','F8_friday','F8_midweek',
    'F9_market_uptrend','F9_market_downtrend','F9_market_choppy',
    'F10_rsi_above_75','F10_rsi_above_85',
    'F11_was_sleepy','F11_was_active',
    'F12_at_52w_high',
]]

for ticker, df in stocks_data.items():
    df_feat = compute_features(df, spy)
    triggers = df_feat[df_feat['trigger']].index
    borrow = BORROW_HIGH if ticker in HIGH_BORROW else BORROW_NORMAL
    
    for date in triggers:
        idx = df_feat.index.get_loc(date)
        trade = simulate_short_trade(df_feat, idx, borrow)
        if trade is None:
            continue
        trade['ticker'] = ticker
        trade['date'] = date
        # Capture features at trigger
        for col in feature_cols:
            if col in df_feat.columns:
                trade[col] = bool(df_feat.iloc[idx][col]) if pd.notna(df_feat.iloc[idx][col]) else False
        all_trades.append(trade)

trades_df = pd.DataFrame(all_trades)
trades_df['date'] = pd.to_datetime(trades_df['date'])
trades_df['period'] = trades_df['date'].apply(lambda d: 'train' if d <= TRAIN_END else 'test')
trades_df['win'] = trades_df['net_pnl'] > 0

print(f"Total trades captured: {len(trades_df)}")
print(f"  Train (2018-2022): {(trades_df['period']=='train').sum()}")
print(f"  Test (2023-2026):  {(trades_df['period']=='test').sum()}")

# =============================================================================
# PHASE 3: Single-feature analysis on TRAIN set
# =============================================================================
print("\n" + "=" * 80)
print("PHASE 3 — Feature analysis on TRAIN set (2018-2022)")
print("=" * 80)
print()
print(f"Train baseline: {(trades_df[trades_df['period']=='train']['win']).mean()*100:.1f}% win, "
      f"{trades_df[trades_df['period']=='train']['net_pnl'].mean()*100:+.2f}% avg P&L")
print()

train = trades_df[trades_df['period']=='train']
baseline_wr = train['win'].mean()
baseline_pnl = train['net_pnl'].mean()

feature_results = []
for col in feature_cols:
    if col not in train.columns:
        continue
    
    when_true = train[train[col] == True]
    when_false = train[train[col] == False]
    
    if len(when_true) < 30:
        continue
    
    wr_lift = when_true['win'].mean() - baseline_wr
    pnl_lift = when_true['net_pnl'].mean() - baseline_pnl
    
    feature_results.append({
        'feature': col,
        'n_when_true': len(when_true),
        'win_rate_when_true': when_true['win'].mean(),
        'pnl_when_true': when_true['net_pnl'].mean(),
        'wr_lift': wr_lift,
        'pnl_lift': pnl_lift,
    })

fr = pd.DataFrame(feature_results).sort_values('pnl_lift', ascending=False)

print(f"{'Feature':<28}{'N':>8}{'WR%':>8}{'P&L%':>10}{'WR lift':>10}{'P&L lift':>11}")
print("-" * 80)
for _, r in fr.iterrows():
    print(f"{r['feature']:<28}{r['n_when_true']:>8}"
          f"{r['win_rate_when_true']*100:>7.1f}%{r['pnl_when_true']*100:>9.2f}%"
          f"{r['wr_lift']*100:>+9.1f}pp{r['pnl_lift']*100:>+10.2f}pp")

# =============================================================================
# PHASE 4: Build filtered strategy from TOP features
# =============================================================================
print("\n" + "=" * 80)
print("PHASE 4 — Build 'Setup A+' from top features")
print("=" * 80)

top_features = fr[fr['pnl_lift'] > 0.005].head(5)['feature'].tolist()
print(f"\nTop features (require ALL to fire on TRAIN data):")
for f in top_features:
    row = fr[fr['feature']==f].iloc[0]
    print(f"  • {f}: +{row['pnl_lift']*100:.2f}pp P&L lift, "
          f"+{row['wr_lift']*100:.1f}pp WR lift")

# Apply filter
def apply_filter(row, features):
    return all(row[f] for f in features if f in row.index)

if top_features:
    train['filtered'] = train.apply(lambda r: apply_filter(r, top_features), axis=1)
    train_filtered = train[train['filtered']]
    
    test = trades_df[trades_df['period']=='test'].copy()
    test['filtered'] = test.apply(lambda r: apply_filter(r, top_features), axis=1)
    test_filtered = test[test['filtered']]
    
    # =============================================================================
    # PHASE 5: OUT-OF-SAMPLE VALIDATION
    # =============================================================================
    print("\n" + "=" * 80)
    print("PHASE 5 — Out-of-sample validation (2023-2026 unseen data)")
    print("=" * 80)
    print()
    print("              TRAIN (2018-2022)    TEST (2023-2026)")
    print("-" * 80)
    
    print(f"Setup A (no filter):")
    print(f"  Trades:     {len(train):>10}              {len(test):>10}")
    print(f"  Win rate:   {train['win'].mean()*100:>9.1f}%              "
          f"{test['win'].mean()*100:>9.1f}%")
    print(f"  Avg P&L:    {train['net_pnl'].mean()*100:>+9.2f}%              "
          f"{test['net_pnl'].mean()*100:>+9.2f}%")
    
    print(f"\nSetup A+ (with top filters):")
    print(f"  Trades:     {len(train_filtered):>10}              {len(test_filtered):>10}")
    if len(train_filtered) > 0:
        print(f"  Win rate:   {train_filtered['win'].mean()*100:>9.1f}%", end='')
    else:
        print(f"  Win rate:   {'n/a':>10}", end='')
    if len(test_filtered) > 0:
        print(f"              {test_filtered['win'].mean()*100:>9.1f}%")
    else:
        print(f"              {'n/a':>10}")
    if len(train_filtered) > 0:
        print(f"  Avg P&L:    {train_filtered['net_pnl'].mean()*100:>+9.2f}%", end='')
    else:
        print(f"  Avg P&L:    {'n/a':>10}", end='')
    if len(test_filtered) > 0:
        print(f"              {test_filtered['net_pnl'].mean()*100:>+9.2f}%")
    else:
        print(f"              {'n/a':>10}")
    
    # =============================================================================
    # PHASE 6: VERDICT
    # =============================================================================
    print("\n" + "=" * 80)
    print("PHASE 6 — Equity curve simulation (out-of-sample only)")
    print("=" * 80)
    
    if len(test_filtered) > 0:
        test_sorted = test_filtered.sort_values('date')
        STARTING_CAPITAL = 50000
        POSITION_SIZE = 15000  # 30% per trade
        
        equity = [STARTING_CAPITAL]
        for _, t in test_sorted.iterrows():
            pnl_dollars = POSITION_SIZE * t['net_pnl']
            equity.append(equity[-1] + pnl_dollars)
        
        peak = STARTING_CAPITAL
        max_dd = 0
        for e in equity:
            peak = max(peak, e)
            dd = (e - peak) / peak
            max_dd = min(max_dd, dd)
        
        final = equity[-1]
        total_return = (final - STARTING_CAPITAL) / STARTING_CAPITAL
        years = (test_sorted['date'].max() - test_sorted['date'].min()).days / 365
        cagr = (final / STARTING_CAPITAL) ** (1/years) - 1 if years > 0 else 0
        
        print(f"\nStarting capital: ${STARTING_CAPITAL:,}")
        print(f"Position size:    ${POSITION_SIZE:,} per trade")
        print(f"Trades simulated: {len(test_sorted)}")
        print(f"Period:           {test_sorted['date'].min().date()} to {test_sorted['date'].max().date()}")
        print(f"Final equity:     ${final:,.0f}")
        print(f"Total return:     {total_return*100:+.1f}%")
        print(f"CAGR:             {cagr*100:+.1f}%")
        print(f"Max drawdown:     {max_dd*100:.1f}%")
    
    # Final verdict
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    
    if len(test_filtered) > 30:
        oos_wr = test_filtered['win'].mean()
        oos_pnl = test_filtered['net_pnl'].mean()
        train_wr = train_filtered['win'].mean()
        wr_drop = train_wr - oos_wr
        
        if oos_wr > 0.50 and oos_pnl > 0.015:
            print("\n✅ EDGE SURVIVED OUT-OF-SAMPLE")
            print(f"   Filtered system shows {oos_wr*100:.1f}% WR and {oos_pnl*100:+.2f}% P&L on UNSEEN data.")
            print(f"   Win rate degradation from train→test: {wr_drop*100:+.1f}pp (acceptable if <5pp)")
        elif oos_pnl > 0:
            print("\n🟡 PARTIAL VALIDATION")
            print(f"   System works but lift is smaller than train suggested.")
            print(f"   OOS P&L: {oos_pnl*100:+.2f}%, WR: {oos_wr*100:.1f}%")
        else:
            print("\n❌ OVERFIT — filters didn't survive out-of-sample")
            print(f"   Train P&L: {train_filtered['net_pnl'].mean()*100:+.2f}%")
            print(f"   Test P&L:  {oos_pnl*100:+.2f}%")
            print(f"   The 'improvement' was historical noise. Use unfiltered Setup A.")
    else:
        print("\n⚠️ Filter too restrictive — too few OOS trades to validate")

print()
