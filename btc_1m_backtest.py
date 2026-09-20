"""
btc_1m_backtest.py
Faithful Python translation of:
  "BTC 15M → 1M Structure Break + Retest | 3R"
Downloads BTC/USDT 1M + 15M data from Binance, runs the strategy,
and reports all requested statistics.
"""

import requests, time as _time, sys
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, timezone

# ── Strategy parameters (mirror Pine Script inputs) ─────────────────────────
INITIAL_CAPITAL  = 840.0
RISK_PCT         = 1.0          # % of equity per trade
RR               = 3.0          # reward / risk
PIVOT_LEFT       = 5            # larger lookback → meaningful structure only
PIVOT_RIGHT      = 5
RETEST_BARS      = 10
RETEST_TOL_PCT   = 0.10 / 100  # 0.10% zone around the level
MIN_RISK_PCT     = 0.15 / 100  # skip if SL is < 0.15% from entry
MAX_TRADES_DAY   = 1            # quality over quantity
COOLDOWN_BARS    = 30
USE_15M_TREND    = True
SESSION_START_H  = 7            # UTC hour: London open
SESSION_END_H    = 21           # UTC hour: NY afternoon
COMMISSION_PCT   = 0.055 / 100  # per side
DAYS_BACK_15M    = 59           # yfinance 15m limit ≈ 60 days
DAYS_BACK_1M     = 28           # yfinance 1m limit ≈ 30 days
DAYS_BACK        = DAYS_BACK_1M # reported in stats

SYMBOL = "BTC-USD"   # yfinance ticker

# ── Data download (yfinance, chunked) ────────────────────────────────────────
def download(symbol, interval, days_back):
    yf_interval = {"1m": "1m", "15m": "15m"}[interval]
    chunk_days  = 7 if interval == "1m" else 59
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    frames = []
    print(f"  Downloading {symbol} {interval} ({days_back}d) …", end="", flush=True)
    cur_end = now
    while cur_end > start:
        cur_start = max(start, cur_end - timedelta(days=chunk_days))
        try:
            df = yf.download(symbol, start=cur_start, end=cur_end,
                             interval=yf_interval, progress=False, auto_adjust=True)
        except Exception as e:
            print(f"\n  Error: {e}"); break
        if df is not None and len(df) > 0:
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            df.index.name = "ts"
            # yfinance may return MultiIndex columns
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            frames.append(df[["open","high","low","close"]])
        cur_end = cur_start
        _time.sleep(0.1)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    start_ts = pd.Timestamp(start, tz="UTC") if start.tzinfo is None else pd.Timestamp(start)
    out = out[out.index >= start_ts]
    print(f" {len(out):,} bars")
    return out

# ── Pivot detection ───────────────────────────────────────────────────────────
# Pine Script ta.pivothigh(src, left, right):
#   At bar i, returns src[i-right] if it is the max of src[i-right-left .. i]
#   (i.e., confirmed `right` bars after the actual pivot bar)

def pivot_highs(arr, left, right):
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(left + right, n):
        pivot_i = i - right
        lo = max(0, pivot_i - left)
        if arr[pivot_i] == arr[lo: i + 1].max():
            out[i] = arr[pivot_i]
    return out

def pivot_lows(arr, left, right):
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(left + right, n):
        pivot_i = i - right
        lo = max(0, pivot_i - left)
        if arr[pivot_i] == arr[lo: i + 1].min():
            out[i] = arr[pivot_i]
    return out

# ── Build 15M trend state ─────────────────────────────────────────────────────
def build_15m_state(df15):
    ph = pivot_highs(df15["high"].values, PIVOT_LEFT, PIVOT_RIGHT)
    pl = pivot_lows (df15["low"].values,  PIVOT_LEFT, PIVOT_RIGHT)

    lastH = prevH = lastL = prevL = np.nan
    bull = bear = False
    rows = []
    for i in range(len(df15)):
        if not np.isnan(ph[i]):
            prevH = lastH; lastH = ph[i]
        if not np.isnan(pl[i]):
            prevL = lastL; lastL = pl[i]
        if not (np.isnan(lastH) or np.isnan(prevH) or
                np.isnan(lastL) or np.isnan(prevL)):
            bull = lastH > prevH and lastL > prevL
            bear = lastH < prevH and lastL < prevL
        rows.append({"bull15": bull, "bear15": bear})

    st = pd.DataFrame(rows, index=df15.index)
    return st

# ── Align 15M state to 1M bars ────────────────────────────────────────────────
def align_15m_to_1m(df1, st15):
    st15r = st15.reset_index().rename(columns={"ts": "ts15"})
    df1r  = df1.reset_index()
    merged = pd.merge_asof(df1r, st15r, left_on="ts", right_on="ts15",
                           direction="backward")
    merged.set_index("ts", inplace=True)
    merged["bull15"] = merged["bull15"].fillna(False)
    merged["bear15"] = merged["bear15"].fillna(False)
    return merged

# ── Main backtest ─────────────────────────────────────────────────────────────
def backtest(df):
    """
    df: 1M OHLC aligned with bull15/bear15 columns.
    Returns list of trade dicts.
    """
    op  = df["open"].values
    hi  = df["high"].values
    lo  = df["low"].values
    cl  = df["close"].values
    bull15 = df["bull15"].values.astype(bool)
    bear15 = df["bear15"].values.astype(bool)
    dates  = df.index
    N = len(df)

    # 1M pivot arrays (computed without lookahead)
    ph1 = pivot_highs(hi, PIVOT_LEFT, PIVOT_RIGHT)
    pl1 = pivot_lows (lo, PIVOT_LEFT, PIVOT_RIGHT)

    # Running state
    lastHigh1 = lastLow1 = np.nan

    waitingLong  = waitingShort = False
    longLevel    = shortLevel   = np.nan
    longSL_level = shortSL_level= np.nan
    longBreakBar = shortBreakBar= -999

    equity        = INITIAL_CAPITAL
    trades        = []
    tradesToday   = 0
    last_date     = None
    cooldown_until= -1
    pos_size      = 0.0
    in_long       = in_short = False
    entry_price   = sl_price = tp_price = 0.0
    entry_bar     = -1
    entry_long    = False

    for i in range(N):
        bar_date = dates[i].date()
        bar_hour = dates[i].hour

        # New day reset
        if bar_date != last_date:
            tradesToday = 0
            last_date   = bar_date

        # Session filter
        in_session = (SESSION_START_H <= bar_hour < SESSION_END_H)

        # Update 1M pivots
        if not np.isnan(ph1[i]):
            lastHigh1 = ph1[i]
        if not np.isnan(pl1[i]):
            lastLow1  = pl1[i]

        # ── Manage open position ─────────────────────────────────────────────
        if in_long or in_short:
            # Check SL then TP within this bar (SL wins on same bar = conservative)
            sl_hit = (lo[i] <= sl_price) if in_long else (hi[i] >= sl_price)
            tp_hit = (hi[i] >= tp_price) if in_long else (lo[i] <= tp_price)

            if sl_hit:
                exit_price = sl_price
                gross_pnl  = (exit_price - entry_price) * pos_size if in_long \
                             else (entry_price - exit_price) * pos_size
                comm       = (entry_price + exit_price) * pos_size * COMMISSION_PCT
                net_pnl    = gross_pnl - comm
                equity    += net_pnl
                trades.append({"entry_ts": dates[entry_bar], "exit_ts": dates[i],
                                "side": "LONG" if in_long else "SHORT",
                                "entry": entry_price, "exit": exit_price,
                                "pnl": net_pnl, "result": "LOSS"})
                in_long = in_short = False
                cooldown_until = i + COOLDOWN_BARS

            elif tp_hit:
                exit_price = tp_price
                gross_pnl  = (exit_price - entry_price) * pos_size if in_long \
                             else (entry_price - exit_price) * pos_size
                comm       = (entry_price + exit_price) * pos_size * COMMISSION_PCT
                net_pnl    = gross_pnl - comm
                equity    += net_pnl
                trades.append({"entry_ts": dates[entry_bar], "exit_ts": dates[i],
                                "side": "LONG" if in_long else "SHORT",
                                "entry": entry_price, "exit": exit_price,
                                "pnl": net_pnl, "result": "WIN"})
                in_long = in_short = False
            continue

        # ── Break detection ──────────────────────────────────────────────────
        bull_break = (not np.isnan(lastHigh1) and
                      cl[i] > lastHigh1 and cl[i-1] <= lastHigh1 and i > 0)
        bear_break = (not np.isnan(lastLow1) and
                      cl[i] < lastLow1  and cl[i-1] >= lastLow1  and i > 0)

        if bull_break and (not USE_15M_TREND or bull15[i]):
            waitingLong   = True
            waitingShort  = False
            longLevel     = lastHigh1
            longBreakBar  = i
            longSL_level  = lastLow1   # frozen at break time

        if bear_break and (not USE_15M_TREND or bear15[i]):
            waitingShort  = True
            waitingLong   = False
            shortLevel    = lastLow1
            shortBreakBar = i
            shortSL_level = lastHigh1  # frozen at break time

        # Expire retest windows
        if waitingLong  and (i - longBreakBar)  > RETEST_BARS: waitingLong  = False
        if waitingShort and (i - shortBreakBar) > RETEST_BARS: waitingShort = False

        # ── Retest detection ─────────────────────────────────────────────────
        can_trade = (tradesToday < MAX_TRADES_DAY and i > cooldown_until
                     and in_session)

        if waitingLong and can_trade:
            lu = longLevel * (1 + RETEST_TOL_PCT)
            ll = longLevel * (1 - RETEST_TOL_PCT)
            long_retest = (lo[i] <= lu and hi[i] >= ll and cl[i] >= longLevel)
            bullish_bar = cl[i] > op[i]                  # confirmation candle
            valid_sl    = (not np.isnan(longSL_level) and longSL_level < cl[i])

            if long_retest and bullish_bar and valid_sl and (not USE_15M_TREND or bull15[i]):
                risk = cl[i] - longSL_level
                if risk > 0 and risk / cl[i] >= MIN_RISK_PCT:
                    risk_cash  = equity * RISK_PCT / 100
                    pos_size   = risk_cash / risk
                    entry_price= cl[i]
                    sl_price   = longSL_level
                    tp_price   = cl[i] + risk * RR
                    in_long    = True
                    entry_bar  = i
                    tradesToday += 1
                    waitingLong = False

        if waitingShort and can_trade and not in_long:
            su = shortLevel * (1 + RETEST_TOL_PCT)
            sl_ = shortLevel * (1 - RETEST_TOL_PCT)
            short_retest = (hi[i] >= sl_ and lo[i] <= su and cl[i] <= shortLevel)
            bearish_bar  = cl[i] < op[i]                 # confirmation candle
            valid_sl     = (not np.isnan(shortSL_level) and shortSL_level > cl[i])

            if short_retest and bearish_bar and valid_sl and (not USE_15M_TREND or bear15[i]):
                risk = shortSL_level - cl[i]
                if risk > 0 and risk / cl[i] >= MIN_RISK_PCT:
                    risk_cash  = equity * RISK_PCT / 100
                    pos_size   = risk_cash / risk
                    entry_price= cl[i]
                    sl_price   = shortSL_level
                    tp_price   = cl[i] - risk * RR
                    in_short   = True
                    entry_bar  = i
                    tradesToday += 1
                    waitingShort = False

    return trades, equity

# ── Statistics ────────────────────────────────────────────────────────────────
def report(trades, final_equity):
    if not trades:
        print("No trades generated."); return

    df = pd.DataFrame(trades)
    wins   = df[df["result"] == "WIN"]
    losses = df[df["result"] == "LOSS"]
    n      = len(df)

    net_profit   = final_equity - INITIAL_CAPITAL
    net_pct      = net_profit / INITIAL_CAPITAL * 100
    win_rate     = len(wins) / n * 100 if n else 0
    gross_profit = wins["pnl"].sum()   if len(wins)   else 0
    gross_loss   = abs(losses["pnl"].sum()) if len(losses) else 1e-9
    pf           = gross_profit / gross_loss if gross_loss > 0 else np.inf
    avg_trade    = df["pnl"].mean()
    avg_win      = wins["pnl"].mean()   if len(wins)   else 0
    avg_loss     = losses["pnl"].mean() if len(losses) else 0

    # Max drawdown on equity curve
    cumulative = INITIAL_CAPITAL + df["pnl"].cumsum()
    peak       = cumulative.cummax()
    dd_curve   = (peak - cumulative)
    max_dd     = dd_curve.max()
    max_dd_pct = (max_dd / peak[dd_curve.idxmax()]) * 100 if n else 0

    print("\n══════════════════════════════════════════════════════")
    print(f"  BTC 1M Structure Break + Retest | 3R  — {DAYS_BACK}d backtest")
    print(f"  {df['entry_ts'].min().date()} → {df['entry_ts'].max().date()}")
    print("══════════════════════════════════════════════════════")
    print(f"  Total trades      : {n}")
    print(f"  Win rate          : {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Net profit        : ${net_profit:+.2f}  ({net_pct:+.1f}%)")
    print(f"  Profit factor     : {pf:.2f}")
    print(f"  Max drawdown      : ${max_dd:.2f}  ({max_dd_pct:.1f}%)")
    print(f"  Average trade     : ${avg_trade:+.2f}")
    print(f"  Avg winning trade : ${avg_win:+.2f}")
    print(f"  Avg losing trade  : ${avg_loss:+.2f}")
    print(f"  Final equity      : ${final_equity:.2f}")
    print("══════════════════════════════════════════════════════")

    # Monthly breakdown
    df["month"] = df["entry_ts"].dt.to_period("M")
    print("\n  Monthly breakdown:")
    print(f"  {'Month':<10}  {'Trades':>6}  {'WR%':>6}  {'P&L':>10}")
    for mo, grp in df.groupby("month"):
        w = (grp["result"]=="WIN").sum()
        print(f"  {str(mo):<10}  {len(grp):>6}  {w/len(grp)*100:>5.1f}%  ${grp['pnl'].sum():>+9.2f}")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Downloading data …")
    df15 = download(SYMBOL, "15m", DAYS_BACK_15M)
    df1  = download(SYMBOL, "1m",  DAYS_BACK_1M)

    if df15.empty or df1.empty:
        print("Download failed — check proxy / connectivity"); sys.exit(1)

    print("Computing 15M structure …")
    st15 = build_15m_state(df15)

    print("Aligning 15M → 1M …")
    df1_full = align_15m_to_1m(df1, st15)

    print("Running backtest …")
    trades, final_equity = backtest(df1_full)

    report(trades, final_equity)
