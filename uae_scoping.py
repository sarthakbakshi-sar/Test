"""
UAE MARKET DATA QUALITY SCOPING
================================
Before building a full UAE PEAD backtester, we need to know:
  1. Which UAE tickers actually have usable Yahoo data
  2. How clean is that data (gaps, missing values, weird prices)
  3. How often would PEAD-style signals fire
  4. Are the stocks liquid enough to trade

This script does NOT backtest anything. It just tells us if backtesting is
even possible with available data.

UAE EXCHANGES:
  - DFM (Dubai Financial Market)        — yfinance suffix: .AE (sometimes .DU)
  - ADX (Abu Dhabi Securities Exchange) — yfinance suffix: .AD (sometimes .AE)
  - NASDAQ Dubai                        — yfinance suffix: varies
  
Yahoo's UAE coverage is incomplete. We test multiple suffix variants.

USAGE:
    python uae_scoping.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# UAE STOCK CANDIDATES
# =============================================================================
# These are major DFM and ADX listed companies. We'll test multiple ticker
# suffix variants because Yahoo is inconsistent with UAE data.

DFM_STOCKS = [
    # Real estate (largest sector on DFM)
    {'name': 'Emaar Properties', 'tickers': ['EMAAR.AE', 'EMAAR.DU']},
    {'name': 'Emaar Development', 'tickers': ['EMAARDEV.AE', 'EMAARDEV.DU']},
    {'name': 'Damac Properties', 'tickers': ['DAMAC.AE', 'DAMAC.DU']},
    {'name': 'Union Properties', 'tickers': ['UPP.AE', 'UPP.DU']},
    {'name': 'Deyaar Development', 'tickers': ['DEYAAR.AE', 'DEYAAR.DU']},
    
    # Banking
    {'name': 'Emirates NBD', 'tickers': ['EMIRATESNBD.AE', 'ENBD.DU']},
    {'name': 'Dubai Islamic Bank', 'tickers': ['DIB.AE', 'DIB.DU']},
    {'name': 'Mashreq Bank', 'tickers': ['MASQ.AE', 'MASQ.DU']},
    {'name': 'Commercial Bank of Dubai', 'tickers': ['CBD.AE', 'CBD.DU']},
    
    # Logistics / Industrial
    {'name': 'DP World', 'tickers': ['DPW.DU', 'DPW.AE']},
    {'name': 'Aramex', 'tickers': ['ARMX.AE', 'ARMX.DU']},
    {'name': 'Air Arabia', 'tickers': ['AIRARABIA.AE', 'AIRARABIA.DU']},
    
    # Financial services
    {'name': 'Dubai Financial Market', 'tickers': ['DFM.AE', 'DFM.DU']},
    {'name': 'Amlak Finance', 'tickers': ['AMLAK.AE', 'AMLAK.DU']},
    
    # Telecom
    {'name': 'du (EITC)', 'tickers': ['DU.AE', 'DU.DU']},
    
    # Insurance
    {'name': 'Dubai Insurance', 'tickers': ['DIN.AE', 'DIN.DU']},
    {'name': 'Oman Insurance', 'tickers': ['OIC.AE', 'OIC.DU']},
    
    # Others
    {'name': 'GFH Financial Group', 'tickers': ['GFH.AE', 'GFH.DU']},
    {'name': 'Dubai Investments', 'tickers': ['DIC.AE', 'DIC.DU']},
    {'name': 'Salik Company', 'tickers': ['SALIK.AE', 'SALIK.DU']},
    {'name': 'Empower (Tabreed)', 'tickers': ['TABREED.AE', 'TABREED.DU']},
    {'name': 'Tecom Group', 'tickers': ['TECOM.AE', 'TECOM.DU']},
    {'name': 'Parkin Company', 'tickers': ['PARKIN.AE', 'PARKIN.DU']},
]

ADX_STOCKS = [
    # Banking (dominant on ADX)
    {'name': 'First Abu Dhabi Bank', 'tickers': ['FAB.AD', 'FAB.AE']},
    {'name': 'Abu Dhabi Commercial Bank', 'tickers': ['ADCB.AD', 'ADCB.AE']},
    {'name': 'Abu Dhabi Islamic Bank', 'tickers': ['ADIB.AD', 'ADIB.AE']},
    {'name': 'Bank of Sharjah', 'tickers': ['BOS.AD', 'BOS.AE']},
    
    # Energy / Industrials
    {'name': 'ADNOC Distribution', 'tickers': ['ADNOCDIST.AD', 'ADNOCDIST.AE']},
    {'name': 'ADNOC Drilling', 'tickers': ['ADNOCDRILL.AD', 'ADNOCDRILL.AE']},
    {'name': 'ADNOC Gas', 'tickers': ['ADNOCGAS.AD', 'ADNOCGAS.AE']},
    {'name': 'ADNOC Logistics', 'tickers': ['ADNOCLS.AD', 'ADNOCLS.AE']},
    {'name': 'TAQA', 'tickers': ['TAQA.AD', 'TAQA.AE']},
    {'name': 'Borouge', 'tickers': ['BOROUGE.AD', 'BOROUGE.AE']},
    
    # Telecom
    {'name': 'e& (Etisalat)', 'tickers': ['EAND.AD', 'EAND.AE', 'ETISALAT.AD']},
    {'name': 'YAHSAT', 'tickers': ['YAHSAT.AD', 'YAHSAT.AE']},
    
    # Real Estate
    {'name': 'Aldar Properties', 'tickers': ['ALDAR.AD', 'ALDAR.AE']},
    {'name': 'Q Holding', 'tickers': ['QH.AD', 'QH.AE']},
    {'name': 'Modon Properties', 'tickers': ['MODON.AD', 'MODON.AE']},
    
    # Holding / Investment
    {'name': 'International Holding (IHC)', 'tickers': ['IHC.AD', 'IHC.AE']},
    {'name': 'Multiply Group', 'tickers': ['MULTIPLY.AD', 'MULTIPLY.AE']},
    {'name': 'Alpha Dhabi', 'tickers': ['ALPHADHABI.AD', 'ALPHADHABI.AE']},
    {'name': 'Q Holding (formerly QPC)', 'tickers': ['QHC.AD', 'QHC.AE']},
    
    # Industrial
    {'name': 'Emirates Steel Arkan', 'tickers': ['ARKAN.AD', 'ARKAN.AE']},
    {'name': 'Agthia Group', 'tickers': ['AGTHIA.AD', 'AGTHIA.AE']},
    {'name': 'Pure Health', 'tickers': ['PUREHEALTH.AD', 'PUREHEALTH.AE']},
    {'name': 'Burjeel Holdings', 'tickers': ['BURJEEL.AD', 'BURJEEL.AE']},
    
    # Insurance
    {'name': 'Abu Dhabi National Insurance', 'tickers': ['ADNIC.AD', 'ADNIC.AE']},
    {'name': 'Methaq Takaful', 'tickers': ['METHAQ.AD', 'METHAQ.AE']},
]

# =============================================================================
# DATA QUALITY ASSESSMENT
# =============================================================================

def try_ticker_variants(name, ticker_list):
    """Try each ticker variant and return the first one that works"""
    end = datetime.now()
    start = end - timedelta(days=365 * 5)  # 5 years
    
    for ticker in ticker_list:
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if df is None or len(df) < 50:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            
            # Check we have all OHLCV columns
            required = ['Open', 'High', 'Low', 'Close', 'Volume']
            if not all(c in df.columns for c in required):
                continue
            
            # Check we have non-zero volume on most days
            non_zero_vol_pct = (df['Volume'] > 0).mean()
            if non_zero_vol_pct < 0.5:
                continue
            
            return {
                'working_ticker': ticker,
                'name': name,
                'data': df,
            }
        except Exception:
            continue
    
    return None


def assess_data_quality(result):
    """Comprehensive data quality assessment"""
    df = result['data']
    
    # Coverage
    total_days = len(df)
    expected_days = (df.index.max() - df.index.min()).days * 5/7  # ~rough trading days
    coverage_pct = total_days / expected_days if expected_days > 0 else 0
    
    # Recency
    latest_date = df.index.max()
    days_old = (datetime.now() - latest_date.tz_localize(None) if latest_date.tz else 
                datetime.now() - latest_date).days
    
    # Gaps in data
    date_gaps = df.index.to_series().diff().dt.days
    big_gaps = (date_gaps > 7).sum()  # More than a week gap = issue
    
    # Volume data quality
    zero_volume_days = (df['Volume'] == 0).sum()
    zero_volume_pct = zero_volume_days / len(df)
    avg_volume = df['Volume'][df['Volume'] > 0].mean()
    
    # Price data quality
    avg_price_aed = df['Close'].mean()
    
    # Daily $ volume in AED
    df['dollar_vol_aed'] = df['Close'] * df['Volume']
    avg_dollar_vol_aed = df['dollar_vol_aed'][df['Volume'] > 0].mean()
    avg_dollar_vol_usd = avg_dollar_vol_aed / 3.67  # AED to USD peg
    
    # Volatility (proxy for whether stock actually moves)
    returns = df['Close'].pct_change()
    daily_vol = returns.std()
    
    # Liquidity rating
    if avg_dollar_vol_usd >= 5_000_000:
        liquidity = "HIGH"
    elif avg_dollar_vol_usd >= 1_000_000:
        liquidity = "MEDIUM"
    elif avg_dollar_vol_usd >= 200_000:
        liquidity = "LOW"
    else:
        liquidity = "TOO LOW"
    
    # PEAD signal pre-screening
    df['gap_pct'] = df['Open'] / df['Close'].shift(1) - 1
    df['vol_avg_20'] = df['Volume'].rolling(20).mean()
    df['vol_ratio'] = df['Volume'] / df['vol_avg_20']
    
    # Test 3 different gap thresholds (UAE moves less than US)
    signals_5pct = ((df['gap_pct'] > 0.05) & (df['vol_ratio'] > 2.0)).sum()
    signals_3pct = ((df['gap_pct'] > 0.03) & (df['vol_ratio'] > 2.0)).sum()
    signals_2pct = ((df['gap_pct'] > 0.02) & (df['vol_ratio'] > 2.0)).sum()
    
    return {
        'name': result['name'],
        'ticker': result['working_ticker'],
        'days_data': total_days,
        'coverage_pct': coverage_pct * 100,
        'data_age_days': days_old,
        'big_gaps': big_gaps,
        'zero_vol_pct': zero_volume_pct * 100,
        'avg_volume': avg_volume,
        'avg_price_aed': avg_price_aed,
        'avg_dollar_vol_usd': avg_dollar_vol_usd,
        'daily_vol_pct': daily_vol * 100,
        'liquidity': liquidity,
        'signals_5pct': signals_5pct,
        'signals_3pct': signals_3pct,
        'signals_2pct': signals_2pct,
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 78)
    print("UAE MARKET DATA QUALITY SCOPING")
    print("=" * 78)
    print(f"Testing: {len(DFM_STOCKS)} DFM stocks + {len(ADX_STOCKS)} ADX stocks")
    print(f"Period: last 5 years")
    print()
    
    all_results = []
    failed_tickers = []
    
    print("Testing DFM (Dubai) stocks...")
    print("-" * 78)
    for stock in DFM_STOCKS:
        result = try_ticker_variants(stock['name'], stock['tickers'])
        if result is None:
            print(f"  [FAIL] {stock['name']:<35} — no working ticker found")
            failed_tickers.append({'exchange': 'DFM', **stock})
        else:
            quality = assess_data_quality(result)
            quality['exchange'] = 'DFM'
            all_results.append(quality)
            print(f"  [OK]   {result['name']:<35} via {result['working_ticker']}")
    
    print()
    print("Testing ADX (Abu Dhabi) stocks...")
    print("-" * 78)
    for stock in ADX_STOCKS:
        result = try_ticker_variants(stock['name'], stock['tickers'])
        if result is None:
            print(f"  [FAIL] {stock['name']:<35} — no working ticker found")
            failed_tickers.append({'exchange': 'ADX', **stock})
        else:
            quality = assess_data_quality(result)
            quality['exchange'] = 'ADX'
            all_results.append(quality)
            print(f"  [OK]   {result['name']:<35} via {result['working_ticker']}")
    
    if not all_results:
        print("\nNo UAE tickers returned usable data from Yahoo Finance.")
        print("Backtesting UAE markets is not possible with this data source.")
        return
    
    # =========================================================================
    # DETAILED REPORT
    # =========================================================================
    
    df = pd.DataFrame(all_results)
    df = df.sort_values('avg_dollar_vol_usd', ascending=False)
    
    print()
    print("=" * 78)
    print("DATA QUALITY DETAILS — sorted by liquidity")
    print("=" * 78)
    print(f"{'Name':<32}{'Exch':<6}{'Days':>6}{'Cov%':>6}{'Age':>5}"
          f"{'$Vol(USD)':>12}{'Vol%':>6}{'Liq':>6}")
    print("-" * 78)
    
    for _, r in df.iterrows():
        if r['avg_dollar_vol_usd'] >= 1e6:
            vol_str = f"${r['avg_dollar_vol_usd']/1e6:.1f}M"
        else:
            vol_str = f"${r['avg_dollar_vol_usd']/1e3:.0f}K"
        print(f"{r['name'][:31]:<32}{r['exchange']:<6}{r['days_data']:>6}"
              f"{r['coverage_pct']:>5.0f}%{r['data_age_days']:>5}"
              f"{vol_str:>12}{r['daily_vol_pct']:>5.1f}%{r['liquidity']:>6}")
    
    # =========================================================================
    # SIGNAL FREQUENCY ANALYSIS
    # =========================================================================
    
    print()
    print("=" * 78)
    print("SIGNAL FREQUENCY — how often would PEAD trigger fire?")
    print("=" * 78)
    print(f"{'Name':<32}{'>5% gap':>10}{'>3% gap':>10}{'>2% gap':>10}")
    print("-" * 78)
    
    for _, r in df.iterrows():
        # Per-year normalized
        years = r['days_data'] / 252
        per_year_5 = r['signals_5pct'] / years if years > 0 else 0
        per_year_3 = r['signals_3pct'] / years if years > 0 else 0
        per_year_2 = r['signals_2pct'] / years if years > 0 else 0
        
        print(f"{r['name'][:31]:<32}"
              f"{r['signals_5pct']:>4} ({per_year_5:>3.0f}/y)"
              f"{r['signals_3pct']:>4} ({per_year_3:>3.0f}/y)"
              f"{r['signals_2pct']:>4} ({per_year_2:>3.0f}/y)")
    
    # =========================================================================
    # SUMMARY STATISTICS
    # =========================================================================
    
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    
    total_tested = len(DFM_STOCKS) + len(ADX_STOCKS)
    pct_working = len(all_results) / total_tested * 100
    
    high_liq = (df['liquidity'] == 'HIGH').sum()
    med_liq = (df['liquidity'] == 'MEDIUM').sum()
    low_liq = (df['liquidity'] == 'LOW').sum()
    too_low = (df['liquidity'] == 'TOO LOW').sum()
    
    total_signals_5 = df['signals_5pct'].sum()
    total_signals_3 = df['signals_3pct'].sum()
    avg_years = df['days_data'].mean() / 252
    
    print(f"\nData availability:")
    print(f"  Working tickers: {len(all_results)}/{total_tested} ({pct_working:.0f}%)")
    print(f"  Failed: {len(failed_tickers)}")
    
    print(f"\nLiquidity distribution:")
    print(f"  HIGH ($5M+ daily):    {high_liq} stocks")
    print(f"  MEDIUM ($1M+ daily):  {med_liq} stocks")
    print(f"  LOW ($200K+ daily):   {low_liq} stocks")
    print(f"  TOO LOW (untradeable): {too_low} stocks")
    
    print(f"\nTradeable universe: {high_liq + med_liq} stocks")
    print(f"  (anything below MEDIUM has too much slippage to be tradeable)")
    
    print(f"\nSignal frequency (across ALL working stocks, ~{avg_years:.1f} years each):")
    print(f"  >5% gap + 2× volume: {total_signals_5} total signals "
          f"({total_signals_5/avg_years:.0f}/year)")
    print(f"  >3% gap + 2× volume: {total_signals_3} total signals "
          f"({total_signals_3/avg_years:.0f}/year)")
    
    # =========================================================================
    # VERDICT
    # =========================================================================
    
    print()
    print("=" * 78)
    print("VERDICT — should we build a UAE PEAD backtester?")
    print("=" * 78)
    
    tradeable_count = high_liq + med_liq
    avg_signals_per_year = (total_signals_3 / avg_years) if avg_years > 0 else 0
    
    if tradeable_count >= 15 and avg_signals_per_year >= 30:
        print("\n✅ DATA IS GOOD ENOUGH FOR BACKTESTING")
        print(f"   - {tradeable_count} tradeable stocks (>=$1M daily $ volume)")
        print(f"   - ~{avg_signals_per_year:.0f} signals/year (using 3% gap threshold)")
        print(f"   - Recommend: lower gap threshold to 3% (vs 5% for US)")
        print(f"     — UAE stocks are less volatile day-to-day")
        print(f"   - Next step: build UAE PEAD backtester")
    elif tradeable_count >= 10 and avg_signals_per_year >= 15:
        print("\n🟡 MARGINAL — could try but expect noisy results")
        print(f"   - Only {tradeable_count} tradeable stocks")
        print(f"   - Only ~{avg_signals_per_year:.0f} signals/year")
        print(f"   - Sample size will be small for statistical confidence")
        print(f"   - Recommend: build backtester but interpret cautiously")
    else:
        print("\n❌ DATA INSUFFICIENT")
        print(f"   - Only {tradeable_count} tradeable stocks")
        print(f"   - Only ~{avg_signals_per_year:.0f} signals/year")
        print(f"   - Not enough sample size for meaningful backtesting")
        print(f"   - Yahoo's UAE coverage is too sparse")
        print(f"   - Would need paid data source (e.g. Refinitiv) to test properly")
    
    if failed_tickers:
        print(f"\nFailed tickers (no Yahoo data):")
        for ft in failed_tickers[:10]:
            print(f"  - {ft['name']} ({ft['exchange']})")
        if len(failed_tickers) > 10:
            print(f"  ... and {len(failed_tickers) - 10} more")
    
    print()


if __name__ == "__main__":
    main()
