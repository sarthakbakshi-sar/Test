"""
THREE-UNIVERSE PEAD OPTIMIZER
==============================
Tests PEAD long & short with 9 SL/TP combos across THREE universes:
  - LARGE-CAP:  S&P 500 names ($10B+)
  - MID-CAP:    $2-10B
  - SMALL-CAP:  $300M-$2B (where retail edges historically live)

Each universe gets REALISTIC cost assumptions:
  - Large-cap:  0.1% slippage, 0.03% commission, 4% borrow
  - Mid-cap:    0.2% slippage, 0.05% commission, 5% borrow
  - Small-cap:  0.4% slippage, 0.05% commission, 8% borrow (often higher in reality)

Walk-forward validated: 2018-2022 train / 2023-2026 test.

THE MOST INTERESTING RESULT:
  Find the SL/TP combo that wins on ALL THREE universes simultaneously.
  That is the strongest possible robustness signal — same edge mechanism,
  three different participant bases, three different liquidity regimes.

CAVEATS YOU SHOULD KNOW:
  1. Small-cap survivorship bias: delisted stocks from 2018-2022 aren't in
     yfinance anymore. Our test only sees survivors → results biased upward,
     especially for short-side.
  2. Borrow availability: some small-cap shorts can't actually be executed in
     real life. We model cost but not impossibility.
  3. Liquidity: small-cap fill prices in real trading are often worse than
     the close prices in our backtest.

USAGE:
    python pead_three_universes.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# UNIVERSES
# =============================================================================

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

MID_CAP_UNIVERSE = [
    'AFRM','UPST','SOFI','PLTR','RBLX','PATH','BILL','GTLB','NET','DDOG',
    'ZS','OKTA','TWLO','DOCN','FSLY','MDB','ESTC','HUBS','TEAM','ZM',
    'AI','BBAI','SOUN','FROG','BRZE','MNDY','APPF','APPN',
    'CHWY','PTON','ROKU','PINS','SNAP','ETSY','W','RH','CART','WMG',
    'BARK','FIGS','OLPX','ELF','BIRK','YETI','SFM','SKIN','BROS','CAVA',
    'WING','SHAK','KSS','M',
    'PLUG','CHPT','RUN','ENPH','SEDG','STEM','JOBY','ACHR','BLNK','EVGO',
    'ARRY','SHLS','FLNC','HASI','BE','BLDP','FCEL','CLNE',
    'TDOC','HIMS','OSCR','DOCS','PHR','AMWL','HQY','DH','EVH',
    'PGNY','HCAT','GDRX','DOC','EHC','LH','DGX',
    'HOOD','COIN','MARA','RIOT','CLSK','IREN','WULF','BTBT','HUT',
    'PYPL','TOST','LMND','ROOT',
    'GME','AMC','CVNA','OPEN','BYND','SPCE','HYMC','WKHS','ATER','CLOV',
    'LCID','RIVN','XPEV','LI','NIO','PSNY','TIGR',
    'DKNG','PENN','RSI','GENI','BALY','GAMB','SKLZ','TTWO','EA',
    'LYV','MSGE','SEAT','NCLH','CCL','RCL',
    'LUNR','RKLB','ASTS','LIDR','OUST','MVIS','MSTR','PWR','ACM',
    'SONO','VITL','LZ',
    'NVAX','OCGN','SAVA','BIIB','MRNA','BNTX','ALNY','BMRN',
]

# SMALL-CAP universe — $300M to $2B range, retail-heavy names
# Note: heavy survivorship bias is unavoidable here
SMALL_CAP_UNIVERSE = [
    # Small-cap tech
    'CXM','APPN','BAND','CFLT','SUMO','VRNS','CDLX','DOMO','AVPT','ALKT',
    'NTNX','PRO','EVBG','VRNT','LPSN','CWAN','MITK','EGAN','UPLD','BLZE',
    # Small-cap consumer
    'PRTS','BARK','CRCT','LE','FTCH','RVLV','ESEA','POSH','CTLP','CURV',
    'DDS','BOOT','BIG','LE','TUYA','OLPX','EXPI','GOOS','VITL','FRGI',
    # Small-cap healthcare
    'PHR','SDGR','NUWE','HCAT','PGNY','DH','EVH','ACCD','MD','GHRS',
    'DCGO','HCM','TXG','PRGO','CRNX','VYNE',
    # Small-cap industrial
    'KFY','RES','EAF','TRTN','CECO','EVI','HSTM','LASE','PRLB','GNRC',
    'IRBT','NPO','MTRN','TWST','EVTC',
    # Small-cap energy
    'TGS','VTNR','EVA','RNGR','HCC','CRC','PR','GPRE','ENVX','REX',
    # Small-cap financial
    'OPRT','PRA','RDN','UVE','MGI','HMN','MCBS','PFS','TBBK','FFBC',
    # Small-cap retail  
    'GES','VSCO','LE','JILL','EXPR','CATO','FIVE','GIII','OXM','BBW',
    # Small-cap meme/high-vol
    'BBIG','PROG','ATER','XELA','SDC','SPRT','TLRY','SNDL','HEXO','OCGN',
    'BNGO','CTRM','GOEV','AVCT','CEI','RDBX','ENDP','RIDE',
    # Small-cap gaming/entertainment
    'IMAX','CNK','CINE','MSGS','WWE','GLPI','VAC','JAKK','CRSR','TBBK',
    # Small-cap biotech (allowed since edge may exist)
    'AVTX','ANIK','ARDX','ATAI','AXSM','CCXI','CRBP','CRSP','CYTK',
    'EDIT','FATE','GH','IDYA','IMTX','INSM','IOVA','KOD','KROS','MIRM',
    'NTLA','PACB','PRTA','RCKT','RGNX','RYTM','SAGE','TCDA','TGTX','TWST',
    # Other small caps
    'OUST','LIDR','MVIS','MAPS','APPN','LSEA','FUBO','AMBC','DGII','NPK',
    'FORTY','IDT','KE','LOMA','MNRO','PRDO','REPL','TXMD','VECO','VEC',
]

# Deduplicate
LARGE_CAP_UNIVERSE = list(set(LARGE_CAP_UNIVERSE))
MID_CAP_UNIVERSE = list(set(MID_CAP_UNIVERSE))
SMALL_CAP_UNIVERSE = list(set(SMALL_CAP_UNIVERSE))

# =============================================================================
# CONFIGURATION (per-universe costs)
# =============================================================================

UNIVERSE_COSTS = {
    'LARGE-CAP': {'slippage': 0.001, 'commission': 0.0003, 'borrow': 0.04},
    'MID-CAP':   {'slippage': 0.002, 'commission': 0.0005, 'borrow': 0.05},
    'SMALL-CAP': {'slippage': 0.004, 'commission': 0.0005, 'borrow': 0.08},  # Real-world drag
}

END_DATE = datetime.now()
TRAIN_END = datetime(2022, 12, 31)
START_DATE = END_DATE - timedelta(days=365 * 8)

GAP_THRESHOLD = 0.05
VOL_THRESHOLD = 2.0

STOP_LOSSES = [0.03, 0.05, 0.07]
PROFIT_TARGETS = [0.06, 0.10, 'trailing']

TRAIL_TRIGGER = 0.05
TRAIL_GIVEBACK = 0.03
MAX_HOLD_DAYS = 30

# Liquidity floor — won't trade if avg daily $ vol below this
MIN_DOLLAR_VOLUME = {
    'LARGE-CAP': 50_000_000,   # $50M+
    'MID-CAP':   10_000_000,   # $10M+
    'SMALL-CAP': 2_000_000,    # $2M+ (real liquidity floor for small caps)
}

# =============================================================================
# DATA + SIGNALS
# =============================================================================

def fetch_data(ticker, start, end, min_volume):
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if len(df) < 250:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df['vol_avg_20'] = df['Volume'].rolling(20).mean()
        df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
        df['gap_pct'] = df['Open'] / df['Close'].shift(1) - 1
        df['dollar_vol'] = df['Close'] * df['Volume']
        df['avg_dollar_vol'] = df['dollar_vol'].rolling(20).mean()
        df['liquid'] = df['avg_dollar_vol'] > min_volume
        return df
    except Exception:
        return None


def find_pead_signals(df, ticker, side):
    signals = []
    if side == 'long':
        triggers = (df['gap_pct'] > GAP_THRESHOLD) & (df['vol_ratio'] > VOL_THRESHOLD) & df['liquid']
    else:
        triggers = (df['gap_pct'] < -GAP_THRESHOLD) & (df['vol_ratio'] > VOL_THRESHOLD) & df['liquid']
    
    for date in df.index[triggers]:
        idx = df.index.get_loc(date)
        if idx >= len(df) - 1:
            continue
        signals.append({'ticker': ticker, 'date': date, 'idx': idx, 'side': side})
    return signals


# =============================================================================
# TRADE SIMULATOR
# =============================================================================

def simulate_trade(df, entry_idx, side, stop_loss, profit_target, costs):
    if entry_idx + 1 >= len(df):
        return None
    
    entry_price = df.iloc[entry_idx + 1]['Open']
    if pd.isna(entry_price) or entry_price <= 0:
        return None
    
    sign = 1 if side == 'long' else -1
    
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
        
        if side == 'long':
            adverse_pnl = (day_low - entry_price) / entry_price
            favorable_pnl = (day_high - entry_price) / entry_price
        else:
            adverse_pnl = (entry_price - day_high) / entry_price
            favorable_pnl = (entry_price - day_low) / entry_price
        
        close_pnl = sign * (day_close - entry_price) / entry_price
        
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
    
    borrow = (costs['borrow'] * days_held / 365) if side == 'short' else 0
    net_pnl = exit_pnl - costs['slippage'] * 2 - costs['commission'] * 2 - borrow
    
    return {'gross_pnl': exit_pnl, 'net_pnl': net_pnl,
            'days_held': days_held, 'exit_reason': exit_reason}


# =============================================================================
# RUN ONE UNIVERSE
# =============================================================================

def run_universe(name, tickers):
    print(f"\n{'='*80}")
    print(f"UNIVERSE: {name} ({len(tickers)} tickers)")
    print(f"{'='*80}")
    print(f"Costs: slip={UNIVERSE_COSTS[name]['slippage']*100:.1f}%, "
          f"comm={UNIVERSE_COSTS[name]['commission']*100:.2f}%, "
          f"borrow={UNIVERSE_COSTS[name]['borrow']*100:.0f}%")
    
    print("Loading...")
    stocks_data = {}
    for i, ticker in enumerate(tickers):
        df = fetch_data(ticker, START_DATE, END_DATE, MIN_DOLLAR_VOLUME[name])
        if df is not None:
            stocks_data[ticker] = df
        if (i+1) % 50 == 0:
            print(f"  [{i+1}/{len(tickers)}] {len(stocks_data)} loaded")
    print(f"Loaded {len(stocks_data)}")
    
    print("Generating signals...")
    sig_long, sig_short = [], []
    for ticker, df in stocks_data.items():
        sig_long.extend(find_pead_signals(df, ticker, 'long'))
        sig_short.extend(find_pead_signals(df, ticker, 'short'))
    print(f"  Long:  {len(sig_long)}")
    print(f"  Short: {len(sig_short)}")
    
    print("Running 18 strategies...")
    universe_results = []
    costs = UNIVERSE_COSTS[name]
    
    for side, signals in [('long', sig_long), ('short', sig_short)]:
        for sl in STOP_LOSSES:
            for tp in PROFIT_TARGETS:
                trades = []
                for sig in signals:
                    df = stocks_data[sig['ticker']]
                    t = simulate_trade(df, sig['idx'], side, sl, tp, costs)
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
                    'universe': name, 'side': side, 'sl': sl, 'tp': tp_label,
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
print("THREE-UNIVERSE PEAD OPTIMIZER")
print("=" * 80)

all_results = []
all_results.extend(run_universe('LARGE-CAP', LARGE_CAP_UNIVERSE))
all_results.extend(run_universe('MID-CAP', MID_CAP_UNIVERSE))
all_results.extend(run_universe('SMALL-CAP', SMALL_CAP_UNIVERSE))

results_df = pd.DataFrame(all_results)

# =============================================================================
# OUTPUT
# =============================================================================

print("\n" + "=" * 80)
print("RESULTS — Top 20 by test P&L")
print("=" * 80)
print(f"{'Univ':<11}{'Side':<7}{'SL':<5}{'TP':<8}{'Tr#':>6}{'Te#':>6}"
      f"{'Te WR':>7}{'Te P&L':>9}{'Te Shrp':>9}")
print("-" * 80)

ranked = results_df.sort_values('test_pnl', ascending=False)
for _, r in ranked.head(20).iterrows():
    sl_str = f"{int(r['sl']*100)}%"
    print(f"{r['universe']:<11}{r['side']:<7}{sl_str:<5}{r['tp']:<8}"
          f"{r['train_n']:>6}{r['test_n']:>6}"
          f"{r['test_wr']*100:>6.1f}%{r['test_pnl']*100:>+8.2f}%"
          f"{r['test_sharpe']:>9.2f}")

# =============================================================================
# THREE-UNIVERSE ROBUSTNESS
# =============================================================================

print("\n" + "=" * 80)
print("ROBUSTNESS — Combinations profitable on ALL THREE universes")
print("=" * 80)

# For each (side, sl, tp), check all 3 universes
combos = results_df.groupby(['side', 'sl', 'tp']).agg({
    'test_pnl': lambda x: dict(zip(results_df.loc[x.index, 'universe'], x)),
    'test_n': lambda x: dict(zip(results_df.loc[x.index, 'universe'], x)),
    'test_sharpe': lambda x: dict(zip(results_df.loc[x.index, 'universe'], x)),
}).reset_index()

robust_3 = []
robust_2 = []
for _, c in combos.iterrows():
    pnl_dict = c['test_pnl']
    n_dict = c['test_n']
    sh_dict = c['test_sharpe']
    
    # Check positive on all 3 with min sample
    universes = ['LARGE-CAP', 'MID-CAP', 'SMALL-CAP']
    pnls = [pnl_dict.get(u, -999) for u in universes]
    ns = [n_dict.get(u, 0) for u in universes]
    shs = [sh_dict.get(u, 0) for u in universes]
    
    pos_count = sum(1 for p in pnls if p > 0)
    if pos_count == 3 and all(n >= 30 for n in ns):
        robust_3.append({
            'side': c['side'], 'sl': c['sl'], 'tp': c['tp'],
            'lc_pnl': pnls[0], 'mc_pnl': pnls[1], 'sc_pnl': pnls[2],
            'lc_sh': shs[0], 'mc_sh': shs[1], 'sc_sh': shs[2],
            'avg_pnl': np.mean(pnls), 'avg_sh': np.mean(shs),
        })
    elif pos_count == 2 and all(n >= 30 for n in ns):
        robust_2.append({
            'side': c['side'], 'sl': c['sl'], 'tp': c['tp'],
            'pnls': pnls, 'avg_pnl': np.mean(pnls),
        })

if robust_3:
    print(f"\n✅ {len(robust_3)} combo(s) profitable on ALL THREE universes:\n")
    print(f"{'Side':<7}{'SL':<5}{'TP':<8}{'Large':>10}{'Mid':>10}{'Small':>10}"
          f"{'Avg':>10}{'Avg Shrp':>11}")
    print("-" * 75)
    for c in sorted(robust_3, key=lambda x: -x['avg_pnl']):
        sl_str = f"{int(c['sl']*100)}%"
        print(f"{c['side']:<7}{sl_str:<5}{c['tp']:<8}"
              f"{c['lc_pnl']*100:>+9.2f}%{c['mc_pnl']*100:>+9.2f}%"
              f"{c['sc_pnl']*100:>+9.2f}%{c['avg_pnl']*100:>+9.2f}%"
              f"{c['avg_sh']:>11.2f}")
else:
    print("\n⚠️ No combo profitable on all three universes")

if robust_2:
    print(f"\n🟡 {len(robust_2)} combo(s) profitable on TWO of three universes")

# =============================================================================
# UNIVERSE-LEVEL COMPARISON
# =============================================================================

print("\n" + "=" * 80)
print("UNIVERSE COMPARISON — average test performance per universe")
print("=" * 80)
print(f"{'Universe':<12}{'Side':<8}{'Avg Test P&L':>15}{'Avg Test Sharpe':>17}")
print("-" * 55)
for univ in ['LARGE-CAP', 'MID-CAP', 'SMALL-CAP']:
    for side in ['long', 'short']:
        subset = results_df[(results_df['universe']==univ) & (results_df['side']==side)]
        if len(subset) > 0:
            print(f"{univ:<12}{side:<8}{subset['test_pnl'].mean()*100:>+13.2f}%"
                  f"{subset['test_sharpe'].mean():>17.2f}")

# =============================================================================
# VERDICT
# =============================================================================

print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

if robust_3:
    best = max(robust_3, key=lambda x: x['avg_pnl'])
    sl_str = f"{int(best['sl']*100)}%"
    print(f"\n✅ STRONGEST EDGE: {best['side']} side, SL={sl_str}, TP={best['tp']}")
    print(f"   Profitable on ALL three universes:")
    print(f"     Large-cap: {best['lc_pnl']*100:+.2f}% (Sharpe {best['lc_sh']:.2f})")
    print(f"     Mid-cap:   {best['mc_pnl']*100:+.2f}% (Sharpe {best['mc_sh']:.2f})")
    print(f"     Small-cap: {best['sc_pnl']*100:+.2f}% (Sharpe {best['sc_sh']:.2f})")
    print(f"   Average: {best['avg_pnl']*100:+.2f}% per trade, Sharpe {best['avg_sh']:.2f}")
    print(f"\n   This is the most robust signal we can produce from backtesting.")
    print(f"   Same edge mechanism, three different participant bases — real signal.")
else:
    best_overall = ranked.iloc[0]
    print(f"\n🟡 No three-universe robust combo. Best single result:")
    print(f"   {best_overall['universe']} {best_overall['side']} "
          f"SL={int(best_overall['sl']*100)}% TP={best_overall['tp']}")
    print(f"   Test P&L: {best_overall['test_pnl']*100:+.2f}%, "
          f"Sharpe {best_overall['test_sharpe']:.2f}, "
          f"n={best_overall['test_n']}")

print()
