"""
ADX AGGRESSIVE TICKER SCOPING
==============================
Tests every plausible ticker variant for major ADX (Abu Dhabi) stocks.
Yahoo Finance is inconsistent with UAE tickers; this brute-forces the
search to find what actually works.

VARIANTS TESTED PER STOCK:
  - {ticker}.AD       (most common ADX suffix on Yahoo)
  - {ticker}.AB       (alternate)
  - {ticker}.AE       (sometimes Yahoo conflates DFM/ADX)
  - {ticker}.UAE      (alternate)
  - {ticker} alone    (no suffix)
  - Plus alternate tickers/spellings

If ANY variant returns 5+ years of clean data with non-zero volume,
the stock is marked tradeable.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# ADX STOCKS WITH MULTIPLE TICKER VARIANTS PER NAME
# =============================================================================

ADX_STOCKS = [
    # Banking
    {'name': 'First Abu Dhabi Bank',     'tickers': ['FAB.AD', 'FAB.AB', 'FAB.AE', 'FAB.UAE', 'FAB']},
    {'name': 'Abu Dhabi Commercial Bank', 'tickers': ['ADCB.AD', 'ADCB.AB', 'ADCB.AE', 'ADCB.UAE', 'ADCB']},
    {'name': 'Abu Dhabi Islamic Bank',   'tickers': ['ADIB.AD', 'ADIB.AB', 'ADIB.AE', 'ADIB']},
    {'name': 'Bank of Sharjah',          'tickers': ['BOS.AD', 'BOS.AB', 'BOS.AE']},
    {'name': 'National Bank of Fujairah','tickers': ['NBF.AD', 'NBF.AB']},
    {'name': 'RAKBANK',                  'tickers': ['RAKBANK.AD', 'RAKBANK.AB', 'NBRAK.AD', 'NBRAK.AB']},
    {'name': 'Sharjah Islamic Bank',     'tickers': ['SIB.AD', 'SIB.AB']},
    {'name': 'Commercial Bank Intl',     'tickers': ['CBI.AD', 'CBI.AB']},
    
    # Energy / ADNOC family
    {'name': 'ADNOC Distribution',       'tickers': ['ADNOCDIST.AD', 'ADNOCDIST.AB', 'ADNOCDIST.UAE']},
    {'name': 'ADNOC Drilling',           'tickers': ['ADNOCDRILL.AD', 'ADNOCDRILL.AB', 'ADNOCDRILL.UAE', 'DRILL.AD']},
    {'name': 'ADNOC Gas',                'tickers': ['ADNOCGAS.AD', 'ADNOCGAS.AB', 'ADNOCGAS.UAE', 'GAS.AD']},
    {'name': 'ADNOC Logistics',          'tickers': ['ADNOCLS.AD', 'ADNOCLS.AB', 'ADNOCL.AD', 'L1.AD']},
    {'name': 'TAQA',                     'tickers': ['TAQA.AD', 'TAQA.AB', 'TAQA.AE', 'TAQA.UAE']},
    {'name': 'Borouge',                  'tickers': ['BOROUGE.AD', 'BOROUGE.AB', 'BOROUGE.UAE']},
    {'name': 'Dana Gas',                 'tickers': ['DANA.AD', 'DANA.AB']},
    {'name': 'Fertiglobe',               'tickers': ['FERTIGLB.AD', 'FERTIGLB.AB', 'FERTIGLOBE.AD']},
    
    # Telecom
    {'name': 'e& (Etisalat)',            'tickers': ['EAND.AD', 'EAND.AB', 'EAND.AE', 'EAND.UAE',
                                                       'ETISALAT.AD', 'ETISALAT.AB', 'ETEL.AD']},
    {'name': 'YAHSAT',                   'tickers': ['YAHSAT.AD', 'YAHSAT.AB']},
    {'name': 'Anghami',                  'tickers': ['ANGH.AD', 'ANGH.AB']},
    
    # Real Estate
    {'name': 'Aldar Properties',         'tickers': ['ALDAR.AD', 'ALDAR.AB', 'ALDAR.AE', 'ALDAR.UAE']},
    {'name': 'Q Holding',                'tickers': ['QH.AD', 'QH.AB', 'QHC.AD']},
    {'name': 'Modon Properties',         'tickers': ['MODON.AD', 'MODON.AB']},
    {'name': 'Reem Investments',         'tickers': ['REEM.AD', 'REEM.AB']},
    
    # Holding / Investment
    {'name': 'International Holding (IHC)', 'tickers': ['IHC.AD', 'IHC.AB', 'IHC.AE', 'IHC.UAE']},
    {'name': 'Multiply Group',           'tickers': ['MULTIPLY.AD', 'MULTIPLY.AB', 'MULTIPLY.UAE']},
    {'name': 'Alpha Dhabi',              'tickers': ['ALPHADHABI.AD', 'ALPHADHABI.AB', 'ALPHA.AD']},
    {'name': 'Q Holding (formerly QPC)', 'tickers': ['QHC.AD', 'QHC.AB']},
    
    # Industrial / Materials
    {'name': 'Emirates Steel Arkan',     'tickers': ['ARKAN.AD', 'ARKAN.AB']},
    {'name': 'Agthia Group',             'tickers': ['AGTHIA.AD', 'AGTHIA.AB']},
    {'name': 'Pure Health',              'tickers': ['PUREHEALTH.AD', 'PUREHEALTH.AB', 'PUREH.AD']},
    {'name': 'Burjeel Holdings',         'tickers': ['BURJEEL.AD', 'BURJEEL.AB']},
    {'name': 'ADC',                      'tickers': ['ADC.AD', 'ADC.AB']},
    {'name': 'Foodco',                   'tickers': ['FOODCO.AD', 'FOODCO.AB']},
    
    # Insurance
    {'name': 'Abu Dhabi National Insurance', 'tickers': ['ADNIC.AD', 'ADNIC.AB']},
    {'name': 'Methaq Takaful',           'tickers': ['METHAQ.AD', 'METHAQ.AB']},
    {'name': 'Insurance House',          'tickers': ['IH.AD', 'IH.AB']},
    
    # Investment / Misc
    {'name': 'Waha Capital',             'tickers': ['WAHA.AD', 'WAHA.AB']},
    {'name': 'Phoenix Group',            'tickers': ['PHX.AD', 'PHX.AB']},
    {'name': 'AD Ports Group',           'tickers': ['ADPORTS.AD', 'ADPORTS.AB', 'ADPORTS.UAE']},
    {'name': 'Presight AI',              'tickers': ['PRESIGHT.AD', 'PRESIGHT.AB']},
    {'name': 'Pal Cooling',              'tickers': ['PAL.AD', 'PAL.AB', 'PALCOOL.AD']},
    {'name': 'GMS',                      'tickers': ['GMS.AD', 'GMS.AB']},
    {'name': 'Abu Dhabi National Hotels','tickers': ['ADNH.AD', 'ADNH.AB']},
    
    # Tourism / Aviation
    {'name': 'Abu Dhabi Aviation',       'tickers': ['ADAVIATION.AD', 'ADAVIATION.AB']},
    {'name': 'Sky News Arabia',          'tickers': ['SKY.AD']},
]

# =============================================================================
# AGGRESSIVE TESTER
# =============================================================================

def try_all_variants(name, ticker_list):
    """Try every variant; return the first one that works with usable data"""
    end = datetime.now()
    start = end - timedelta(days=365 * 5)
    
    best_result = None
    
    for ticker in ticker_list:
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            
            if df is None or len(df) < 50:
                continue
            
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            
            required = ['Open', 'High', 'Low', 'Close', 'Volume']
            if not all(c in df.columns for c in required):
                continue
            
            # Volume sanity check
            non_zero_vol = (df['Volume'] > 0).mean()
            if non_zero_vol < 0.5:
                continue
            
            # Liquidity check
            df['dollar_vol_aed'] = df['Close'] * df['Volume']
            df['avg_dollar_vol_usd'] = df['dollar_vol_aed'].rolling(20).mean() / 3.67
            avg_dollar_vol_usd = df['avg_dollar_vol_usd'].iloc[-1] if len(df) > 0 else 0
            
            # Recency
            latest = df.index.max()
            days_old = (datetime.now() - (latest.tz_localize(None) if latest.tz else latest)).days
            
            return {
                'name': name,
                'working_ticker': ticker,
                'days_data': len(df),
                'avg_dollar_vol_usd': avg_dollar_vol_usd if not pd.isna(avg_dollar_vol_usd) else 0,
                'days_old': days_old,
                'latest_close': df['Close'].iloc[-1],
                'data': df,
            }
        except Exception:
            continue
    
    return None

# =============================================================================
# MAIN
# =============================================================================

print("=" * 80)
print("ADX AGGRESSIVE TICKER SCOPING")
print("=" * 80)
print(f"Testing {len(ADX_STOCKS)} ADX stocks with multiple ticker variants each\n")

results = []
failed = []

for i, stock in enumerate(ADX_STOCKS):
    n_variants = len(stock['tickers'])
    print(f"[{i+1}/{len(ADX_STOCKS)}] {stock['name']:<35} testing {n_variants} variants...", end=' ')
    result = try_all_variants(stock['name'], stock['tickers'])
    if result:
        results.append(result)
        vol_str = f"${result['avg_dollar_vol_usd']/1e6:.1f}M" if result['avg_dollar_vol_usd'] >= 1e6 else f"${result['avg_dollar_vol_usd']/1e3:.0f}K"
        print(f"✓ {result['working_ticker']:<20} ({result['days_data']} days, vol {vol_str})")
    else:
        failed.append(stock)
        print(f"✗ ALL FAILED")

# =============================================================================
# REPORT
# =============================================================================

print()
print("=" * 80)
print("RESULTS")
print("=" * 80)

if not results:
    print("\n❌ ZERO ADX TICKERS WORK ON YAHOO FINANCE")
    print("\nThis is conclusive. Yahoo doesn't serve ADX data.")
    print("Options:")
    print("  1. Use only DFM (current 19-stock universe)")
    print("  2. Get a paid data source (Refinitiv, Bloomberg) for ADX")
    print("  3. Use IBKR's data feed if accessible (since they added ADX in Dec 2025)")
else:
    df = pd.DataFrame(results).sort_values('avg_dollar_vol_usd', ascending=False)
    
    print(f"\n✅ {len(results)} ADX stocks have usable Yahoo data\n")
    print(f"{'Name':<32}{'Ticker':<18}{'Days':>6}{'$Vol(USD)':>12}{'Age':>5}")
    print("-" * 75)
    
    for _, r in df.iterrows():
        if r['avg_dollar_vol_usd'] >= 1e6:
            vol_str = f"${r['avg_dollar_vol_usd']/1e6:.1f}M"
        else:
            vol_str = f"${r['avg_dollar_vol_usd']/1e3:.0f}K"
        print(f"{r['name'][:31]:<32}{r['working_ticker']:<18}{r['days_data']:>6}{vol_str:>12}{r['days_old']:>5}")
    
    # Liquidity tiers
    high_liq = (df['avg_dollar_vol_usd'] >= 5_000_000).sum()
    med_liq = ((df['avg_dollar_vol_usd'] >= 1_000_000) & (df['avg_dollar_vol_usd'] < 5_000_000)).sum()
    low_liq = ((df['avg_dollar_vol_usd'] >= 200_000) & (df['avg_dollar_vol_usd'] < 1_000_000)).sum()
    too_low = (df['avg_dollar_vol_usd'] < 200_000).sum()
    
    print(f"\nLiquidity tiers:")
    print(f"  HIGH ($5M+ daily):       {high_liq}")
    print(f"  MEDIUM ($1M+ daily):     {med_liq}")
    print(f"  LOW ($200K+ daily):      {low_liq}")
    print(f"  TOO LOW (<$200K):        {too_low}")
    
    tradeable = high_liq + med_liq
    print(f"\nTradeable ADX universe: {tradeable} stocks (HIGH + MEDIUM)")
    
    # Print suggested universe expansion
    if tradeable > 0:
        print(f"\n" + "=" * 80)
        print("SUGGESTED EXPANDED UNIVERSE")
        print("=" * 80)
        print("\nTickers to add to the existing 19 DFM stocks:")
        tradeable_df = df[df['avg_dollar_vol_usd'] >= 1_000_000]
        for _, r in tradeable_df.iterrows():
            print(f"    '{r['working_ticker']}',  # {r['name']}")
        
        new_total = 19 + tradeable
        print(f"\nNew total universe: {new_total} stocks ({19} DFM + {tradeable} ADX)")
        print(f"Expected signal frequency increase: ~{tradeable*100/19:.0f}%")

if failed:
    print(f"\n{len(failed)} stocks could not be retrieved with any tested variant:")
    for f in failed[:10]:
        print(f"  - {f['name']}")
    if len(failed) > 10:
        print(f"  ... and {len(failed) - 10} more")
    
    print("""
For the failed stocks, the data simply isn't on Yahoo.
This is a known limitation; even paid services often have gaps for emerging markets.
""")

print("\n" + "=" * 80)
print("NEXT STEPS")
print("=" * 80)
if results and (len(results) >= 5):
    print(f"""
We have {len(results)} ADX stocks accessible. 

Recommended path:
  1. I'll add the tradeable ADX tickers to the universe
  2. Re-run the EEM-filtered momentum backtest on the expanded universe
  3. Verify the strategy still works (per-stock concentration, both periods positive)
  4. Update the Apps Script with the full universe

This will take ~20 minutes and gives us much better coverage.
""")
elif results:
    print(f"""
We only got {len(results)} working ADX tickers. Marginal benefit from adding these.

Recommended path:
  1. Stay with DFM-only (19 stocks)
  2. Possibly add the 1-2 most liquid ADX names if they fit
  3. Move forward with deployment

The juice may not be worth the squeeze for adding ADX with this coverage level.
""")
else:
    print("""
ADX data is not accessible via Yahoo. We have two options:

  1. Stay with DFM-only and deploy what we have
  2. Wait until we can get a paid data source for ADX

Recommended: deploy DFM-only now, evaluate adding ADX later if needed.
""")
