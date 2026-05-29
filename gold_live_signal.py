"""
GOLD LIVE SIGNAL — Combined Macro + 1H Technical
===================================================
Run this at 17:00–17:15 Dubai (30 min before session).
Re-run any time during the session for updated signals.

MACRO LAYER (session bias):
  5 components → score -10..+10
  Gold vs 50 EMA | Yield direction | DXY direction | 5d momentum | GDX lead

TECHNICAL LAYER (entry signals, same rules as gold_1h_backtest.py):
  Window   : 13:30–17:00 UTC (17:30–21:00 Dubai)
  Entry    : EMA 9/21 cross in trend direction + price within 1.5σ of VWAP
  SL       : 0.75 × ATR(14)
  TP1      : 1.5 × ATR  → 60% position
  TP2      : 2.5 × ATR  → 40% position
  Time stop: 3 bars (3 hours) if no meaningful movement

COMBINED RULE:
  Only flag setups where technical signal direction = macro bias direction.
  Strong macro (|score| ≥ 5) → full size
  Weak macro (|score| < 5)   → half size or skip

Run: python gold_live_signal.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
import requests
import warnings
warnings.filterwarnings('ignore')
from email.utils import parsedate_to_datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
DUBAI_OFFSET    = 4
SESS_OPEN_UTC   = (13, 30)
SESS_CLOSE_UTC  = (17,  0)
EMA_FAST        = 9
EMA_SLOW        = 21
EMA_TREND_DAILY = 50
ATR_PERIOD      = 14
SL_MULT         = 0.75
TP1_MULT        = 1.5
TP2_MULT        = 2.5
VWAP_SIG_THR    = 1.5    # standard deviations from VWAP to qualify
ACCOUNT         = 10000
RISK_PCT        = 0.03
LOOKBACK_HOURS  = 12

RSS_FEEDS = [
    ("Kitco",        "https://www.kitco.com/rss/KitcoNews.xml"),
    ("Investing",    "https://www.investing.com/rss/commodities_Gold.rss"),
    ("MarketWatch",  "https://feeds.marketwatch.com/marketwatch/topstories/"),
]
GOLD_BULLISH = {
    'rate cut':3,'dovish':3,'lower rates':3,'yields fall':3,'falling yields':3,
    'dollar weak':3,'dollar falls':3,'safe haven':3,'geopolitical':2,'war':2,
    'conflict':2,'tension':2,'crisis':2,'risk-off':2,'recession':2,
    'gold rallies':3,'gold surges':3,'gold jumps':3,'gold climbs':2,
    'central bank buying':3,'record high':2,'de-dollarization':2,
}
GOLD_BEARISH = {
    'rate hike':3,'hawkish':3,'higher rates':3,'yields rise':3,'rising yields':3,
    'dollar strong':3,'dollar rises':3,'dollar surges':3,'risk-on':2,
    'strong jobs':2,'payrolls beat':2,'inflation hot':3,'cpi hot':3,
    'gold falls':2,'gold drops':3,'gold slides':3,'gold tumbles':3,
}

now_utc   = datetime.now(timezone.utc)
now_dubai = now_utc + timedelta(hours=DUBAI_OFFSET)

print("="*68)
print("  GOLD LIVE SIGNAL")
print(f"  {now_utc.strftime('%Y-%m-%d %H:%M')} UTC  |  {now_dubai.strftime('%H:%M')} Dubai")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  MACRO SCORE
# ══════════════════════════════════════════════════════════════════════════════
print("\n── MACRO ────────────────────────────────────────────────────────────")

macro = {}
macro_raw = 0
macro_reasons = []

try:
    g = yf.download("GC=F", period="90d", interval="1d", progress=False)
    g.columns = [c[0].lower() for c in g.columns]
    macro['gold_px']   = float(g['close'].iloc[-1])
    macro['ema50']     = float(g['close'].ewm(span=50, adjust=False).mean().iloc[-1])
    macro['mom5']      = float((g['close'].iloc[-1] / g['close'].iloc[-6] - 1) * 100)
    macro['gold_ret1'] = float((g['close'].iloc[-1] / g['close'].iloc[-2] - 1) * 100)
    print(f"  Gold  ${macro['gold_px']:,.0f}  |  50EMA ${macro['ema50']:,.0f}  |  5d mom {macro['mom5']:+.2f}%")
    if macro['gold_px'] > macro['ema50']:
        macro_raw += 1; macro_reasons.append("Gold above 50 EMA (+)")
    else:
        macro_raw -= 1; macro_reasons.append("Gold below 50 EMA (−)")
    if macro['mom5'] > 0:
        macro_raw += 1; macro_reasons.append(f"5d momentum {macro['mom5']:+.2f}% (+)")
    else:
        macro_raw -= 1; macro_reasons.append(f"5d momentum {macro['mom5']:+.2f}% (−)")
except Exception as e: print(f"  gold failed: {e}")

try:
    tnx = yf.download("^TNX", period="10d", interval="1d", progress=False)
    tnx.columns = [c[0].lower() for c in tnx.columns]
    chg = float(tnx['close'].iloc[-1] - tnx['close'].iloc[-2])
    macro['yield_chg'] = chg
    print(f"  10Y   {tnx['close'].iloc[-1]:.2f}%  |  {chg:+.2f}bp")
    if chg < 0:
        macro_raw += 1; macro_reasons.append(f"Yields falling {chg:+.2f}bp (+)")
    else:
        macro_raw -= 1; macro_reasons.append(f"Yields rising {chg:+.2f}bp (−)")
except Exception as e: print(f"  yields failed: {e}")

try:
    dxy = yf.download("DX-Y.NYB", period="10d", interval="1d", progress=False)
    dxy.columns = [c[0].lower() for c in dxy.columns]
    chg = float((dxy['close'].iloc[-1] / dxy['close'].iloc[-2] - 1) * 100)
    macro['dxy_chg'] = chg
    print(f"  DXY   {dxy['close'].iloc[-1]:.2f}  |  {chg:+.2f}%")
    if chg < 0:
        macro_raw += 1; macro_reasons.append(f"Dollar down {chg:+.2f}% (+)")
    else:
        macro_raw -= 1; macro_reasons.append(f"Dollar up {chg:+.2f}% (−)")
except Exception as e: print(f"  dxy failed: {e}")

try:
    gdx = yf.download("GDX", period="10d", interval="1d", progress=False)
    gdx.columns = [c[0].lower() for c in gdx.columns]
    gdx_ret = float((gdx['close'].iloc[-1] / gdx['close'].iloc[-2] - 1) * 100)
    gold_ret = macro.get('gold_ret1', 0)
    print(f"  GDX   {gdx_ret:+.2f}%  vs  Gold {gold_ret:+.2f}%")
    if gdx_ret > gold_ret:
        macro_raw += 1; macro_reasons.append(f"Miners leading gold (+)")
    else:
        macro_raw -= 1; macro_reasons.append(f"Miners lagging gold (−)")
except Exception as e: print(f"  gdx failed: {e}")

n_comp     = len(macro_reasons)
macro_score = round(macro_raw / n_comp * 10, 1) if n_comp else 0

# News score
news_score = 0
try:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    heads  = []
    for name, url in RSS_FEEDS:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code != 200: continue
            root = ET.fromstring(r.content)
            for item in root.iter('item'):
                title = (item.findtext('title') or '').strip()
                desc  = (item.findtext('description') or '')[:200]
                pub   = item.findtext('pubDate') or ''
                if pub:
                    try:
                        pub_dt = parsedate_to_datetime(pub)
                        if pub_dt.tzinfo is None: pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                        if pub_dt < cutoff: continue
                    except: pass
                heads.append({'title': title, 'text': (title+' '+desc).lower()})
        except: pass
    bull = bear = 0
    for h in heads:
        bull += sum(w for kw, w in GOLD_BULLISH.items() if kw in h['text'])
        bear += sum(w for kw, w in GOLD_BEARISH.items() if kw in h['text'])
    total = bull + bear
    news_score = round((bull - bear) / total * 10, 1) if total else 0
    print(f"  News  {len(heads)} headlines  |  sentiment {news_score:+.1f}/10")
except Exception as e:
    print(f"  news failed: {e}")

combined = round(0.60 * macro_score + 0.40 * news_score, 1)

# ══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL LAYER
# ══════════════════════════════════════════════════════════════════════════════
print("\n── TECHNICALS ───────────────────────────────────────────────────────")

df1h = yf.download("GC=F", period="10d", interval="1h", progress=False)
df1h.columns = [c[0].lower() for c in df1h.columns]
df1h.index   = pd.to_datetime(df1h.index).tz_localize(None)
df1h.dropna(inplace=True)

# EMAs + ATR on 1H
df1h['ema9']  = df1h['close'].ewm(span=EMA_FAST, adjust=False).mean()
df1h['ema21'] = df1h['close'].ewm(span=EMA_SLOW, adjust=False).mean()
tr = pd.concat([
    df1h['high'] - df1h['low'],
    (df1h['high'] - df1h['close'].shift()).abs(),
    (df1h['low']  - df1h['close'].shift()).abs()
], axis=1).max(axis=1)
df1h['atr'] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

# Session VWAP — use today's session or last session
today = now_utc.date()
sess_open_ts  = pd.Timestamp(today) + pd.Timedelta(hours=SESS_OPEN_UTC[0],  minutes=SESS_OPEN_UTC[1])
sess_close_ts = pd.Timestamp(today) + pd.Timedelta(hours=SESS_CLOSE_UTC[0], minutes=SESS_CLOSE_UTC[1])
sess_bars = df1h.loc[(df1h.index >= sess_open_ts) & (df1h.index <= now_utc.replace(tzinfo=None))]

# If pre-session, use yesterday's session VWAP as reference level
if len(sess_bars) < 2:
    yesterday = today - timedelta(days=1)
    sess_open_y  = pd.Timestamp(yesterday) + pd.Timedelta(hours=SESS_OPEN_UTC[0],  minutes=SESS_OPEN_UTC[1])
    sess_close_y = pd.Timestamp(yesterday) + pd.Timedelta(hours=SESS_CLOSE_UTC[0], minutes=SESS_CLOSE_UTC[1])
    sess_bars = df1h.loc[(df1h.index >= sess_open_y) & (df1h.index <= sess_close_y)]
    vwap_label = "Yesterday's session VWAP"
else:
    vwap_label = "Today's session VWAP (live)"

if len(sess_bars) >= 2:
    tp = (sess_bars['high'] + sess_bars['low'] + sess_bars['close']) / 3
    cum_vol = sess_bars['volume'].replace(0, 1).cumsum()
    vwap    = (tp * sess_bars['volume'].replace(0, 1)).cumsum() / cum_vol
    vwap_now = float(vwap.iloc[-1])
    vwap_std = float(tp.std())
else:
    vwap_now = float(df1h['close'].iloc[-1])
    vwap_std = float(df1h['close'].tail(20).std())
    vwap_label = "Approx VWAP (no session bars)"

# Key levels
prev_day_bars = df1h[df1h.index.date == (today - timedelta(days=1))]
prev_sess     = prev_day_bars.loc[(prev_day_bars.index.time >= pd.Timestamp('13:30').time()) &
                                   (prev_day_bars.index.time <= pd.Timestamp('17:00').time())]
if len(prev_sess):
    pdh = float(prev_sess['high'].max())
    pdl = float(prev_sess['low'].min())
else:
    prev_all = prev_day_bars
    pdh = float(prev_all['high'].max()) if len(prev_all) else None
    pdl = float(prev_all['low'].min())  if len(prev_all) else None

px       = float(df1h['close'].iloc[-1])
ema9_now = float(df1h['ema9'].iloc[-1])
ema21_now= float(df1h['ema21'].iloc[-1])
atr_now  = float(df1h['atr'].iloc[-1])

# Round levels ($25 increments)
def nearest_rounds(price, step=25, n=2):
    base = round(price / step) * step
    return [base + i*step for i in range(-n, n+1)]

rounds = nearest_rounds(px, step=25, n=2)

print(f"  Price now  : ${px:,.1f}")
print(f"  EMA 9/21   : ${ema9_now:,.1f} / ${ema21_now:,.1f}  ({'EMA9 > EMA21 ▲' if ema9_now > ema21_now else 'EMA9 < EMA21 ▼'})")
print(f"  ATR(14)    : ${atr_now:.1f}")
print(f"  {vwap_label}: ${vwap_now:,.1f}  (±1.5σ = ${vwap_now - 1.5*vwap_std:,.1f} – ${vwap_now + 1.5*vwap_std:,.1f})")
if pdh: print(f"  Prev session H/L: ${pdh:,.1f} / ${pdl:,.1f}")

# EMA cross detection (last 3 bars)
recent = df1h.tail(4)
cross_bull = any(
    recent['ema9'].iloc[i-1] <= recent['ema21'].iloc[i-1] and
    recent['ema9'].iloc[i]   >  recent['ema21'].iloc[i]
    for i in range(1, len(recent))
)
cross_bear = any(
    recent['ema9'].iloc[i-1] >= recent['ema21'].iloc[i-1] and
    recent['ema9'].iloc[i]   <  recent['ema21'].iloc[i]
    for i in range(1, len(recent))
)

# Price distance from VWAP in σ
vwap_dist_sig = (px - vwap_now) / vwap_std if vwap_std > 0 else 0

# ══════════════════════════════════════════════════════════════════════════════
#  COMBINED OUTPUT
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*68)
print("  ★ GOLD LIVE SIGNAL ★")
print("="*68)

# Macro bias label
if   combined >= 5:   bias_label = "BULLISH"; bias_sym = "🟢"
elif combined >= 2:   bias_label = "MILD BULLISH"; bias_sym = "🟢"
elif combined > -2:   bias_label = "NEUTRAL"; bias_sym = "⚪"
elif combined > -5:   bias_label = "MILD BEARISH"; bias_sym = "🔴"
else:                 bias_label = "BEARISH"; bias_sym = "🔴"

macro_dir = 1 if combined > 1 else (-1 if combined < -1 else 0)

print(f"\n  {bias_sym} MACRO SCORE: {combined:+.1f}/10  {bias_label}")
for r in macro_reasons:
    tick = "  ✓" if '+' in r else "  ✗"
    print(f"  {tick}  {r}")
print(f"  News sentiment: {news_score:+.1f}/10")

# Size guidance
if abs(combined) >= 5:
    size_label = "FULL SIZE"
elif abs(combined) >= 2:
    size_label = "HALF SIZE"
else:
    size_label = "SKIP (neutral — trade technicals only)"

# Technical bias
tech_dir = 1 if ema9_now > ema21_now else -1
tech_label = "LONG  (EMA9 > EMA21)" if tech_dir == 1 else "SHORT (EMA9 < EMA21)"

print(f"\n  {'─'*60}")
print(f"  TECHNICAL BIAS: {tech_label}")
if cross_bull: print(f"  ⚡ BULLISH CROSS just occurred (last 3 bars)")
if cross_bear: print(f"  ⚡ BEARISH CROSS just occurred (last 3 bars)")
print(f"  Price vs VWAP: {'ABOVE' if px > vwap_now else 'BELOW'} ({vwap_dist_sig:+.1f}σ)")

# ── SETUP DETECTION ──────────────────────────────────────────────────────────
print(f"\n  {'─'*60}")
print(f"  SESSION SETUPS  (13:30–17:00 UTC = 17:30–21:00 Dubai)")
print(f"  {'─'*60}")

setups = []

# LONG setup conditions
long_vwap_zone = -VWAP_SIG_THR <= vwap_dist_sig <= 0.5   # near/below VWAP
long_ema_ok    = ema9_now > ema21_now or cross_bull
long_macro_ok  = macro_dir >= 0  # neutral or bullish

# SHORT setup conditions
short_vwap_zone = -0.5 <= vwap_dist_sig <= VWAP_SIG_THR  # near/above VWAP
short_ema_ok    = ema9_now < ema21_now or cross_bear
short_macro_ok  = macro_dir <= 0  # neutral or bearish

for direction, vwap_ok, ema_ok, mac_ok, label in [
    ('LONG',  long_vwap_zone,  long_ema_ok,  long_macro_ok,  '▲ LONG'),
    ('SHORT', short_vwap_zone, short_ema_ok, short_macro_ok, '▼ SHORT'),
]:
    agree = mac_ok and ema_ok and vwap_ok
    conflict = (mac_ok and ema_ok and not vwap_ok)

    if direction == 'LONG':
        entry    = round(min(px, vwap_now) - 0.5 * atr_now * 0.1, 1)  # entry near pullback zone
        entry    = round(vwap_now if px > vwap_now else px, 1)
        sl       = round(entry - SL_MULT  * atr_now, 1)
        tp1      = round(entry + TP1_MULT * atr_now, 1)
        tp2      = round(entry + TP2_MULT * atr_now, 1)
        zone_desc = f"pullback to VWAP ${vwap_now:,.1f}"
        if pdl: zone_desc += f" or PDL ${pdl:,.1f}"
    else:
        entry    = round(vwap_now if px < vwap_now else px, 1)
        sl       = round(entry + SL_MULT  * atr_now, 1)
        tp1      = round(entry - TP1_MULT * atr_now, 1)
        tp2      = round(entry - TP2_MULT * atr_now, 1)
        zone_desc = f"rally to VWAP ${vwap_now:,.1f}"
        if pdh: zone_desc += f" or PDH ${pdh:,.1f}"

    risk_pts  = abs(entry - sl)
    risk_usd  = ACCOUNT * RISK_PCT
    contracts = round(risk_usd / (risk_pts * 100), 2) if risk_pts > 0 else 0

    if agree:
        status = "✅ ACTIVE — macro + technical AGREE"
        setups.append((direction, entry, sl, tp1, tp2, contracts, status))
    elif mac_ok and not ema_ok:
        status = f"⏳ WAITING — macro agrees but EMA not yet crossed {'bullish' if direction=='LONG' else 'bearish'}"
        setups.append((direction, entry, sl, tp1, tp2, contracts, status))
    elif ema_ok and not mac_ok:
        status = "⛔ SKIP — technical signal CONFLICTS with macro"
        setups.append((direction, entry, sl, tp1, tp2, contracts, status))
    else:
        status = "— No setup"

    if "No setup" not in status:
        print(f"\n  {label}  {status}")
        if "SKIP" not in status:
            print(f"    Entry zone : {zone_desc}")
            print(f"    Entry price: ${entry:,.1f}")
            print(f"    Stop loss  : ${sl:,.1f}  ({risk_pts:.1f} pts = ${risk_pts*100:.0f}/contract)")
            print(f"    TP1 (60%)  : ${tp1:,.1f}  → then move SL to breakeven")
            print(f"    TP2 (40%)  : ${tp2:,.1f}")
            print(f"    Size       : {contracts} contracts  ({size_label})")
        else:
            print(f"    Reason: macro says {bias_label}, technical says opposite — do NOT take this trade")

if not any(s for s in setups if 'SKIP' not in s[6]):
    print(f"\n  No aligned setups right now.")
    if macro_dir != 0:
        d = "LONG" if macro_dir > 0 else "SHORT"
        print(f"  Macro says {bias_label} → watch for {d} entry at:")
        print(f"    • VWAP: ${vwap_now:,.1f}")
        if pdl and macro_dir > 0: print(f"    • PDL support: ${pdl:,.1f}")
        if pdh and macro_dir < 0: print(f"    • PDH resistance: ${pdh:,.1f}")
        print(f"    Waiting for EMA9/21 cross to confirm.")

# ── SESSION SCHEDULE ──────────────────────────────────────────────────────────
print(f"\n  {'─'*60}")
print(f"  SESSION SCHEDULE (Dubai time)")
print(f"  {'─'*60}")
sess_hours = [
    (17, 30, "Session opens — wait for first setup"),
    (18, 30, "Hour 2 — primary trend usually established by now"),
    (19, 30, "Hour 3 — time stop approaching if no move; reassess"),
    (20, 30, "Hour 4 — last clean entry window"),
    (21,  0, "Session closes — exit all positions"),
]
for h, m, note in sess_hours:
    print(f"    {h:02d}:{m:02d}  {note}")

print(f"\n  {'─'*60}")
print(f"  MACRO COMPONENTS:")
for r in macro_reasons:
    tick = "  +" if '+' in r else "  -"
    print(f"  {tick}  {r}")
print(f"\n  Combined score: {combined:+.1f}/10  →  {size_label}")
print("="*68)
