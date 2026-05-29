"""
GOLD MASTER BRIEF — Calendar + Macro + News in ONE Score
==========================================================
Combines all three signal layers into a single integrated brief:

  LAYER 1 — EVENT RISK (gate):    Economic calendar → GREEN/YELLOW/RED
  LAYER 2 — MACRO BIAS (60%):     Gold trend + yields + dollar
  LAYER 3 — NEWS SENTIMENT (40%): Gold-specific news scoring

OUTPUT:
  • One overall gold bias score: -10 (bearish) to +10 (bullish)
  • One trading condition: GREEN / YELLOW / RED
  • One clear recommendation aligned with your 4H system

WEIGHTING LOGIC (honest):
  Macro factors are PROVEN drivers → 60% weight
  News sentiment is a SUPPORTING signal → 40% weight
  Event risk is a GATE, not a score → can veto trading entirely

Run before each session (5:15pm Dubai):
  pip install requests pandas yfinance
  python gold_master_brief.py

No API keys required.
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

from email.utils import parsedate_to_datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
DUBAI_OFFSET     = 4
LOOKBACK_HOURS   = 12
EVENT_BUFFER_MIN = 30
MACRO_WEIGHT     = 0.60     # macro is the proven driver
NEWS_WEIGHT      = 0.40     # news sentiment is supporting

# Your session (UTC)
SESS_START_H, SESS_START_M = 13, 30   # 17:30 Dubai
SESS_END_H,   SESS_END_M   = 17, 0    # 21:00 Dubai

GOLD_MOVERS = ['CPI','Core CPI','Inflation','PCE','Core PCE','FOMC',
    'Federal Funds','Interest Rate','Fed Chair','Powell','Warsh',
    'Non-Farm','Nonfarm','NFP','Unemployment','Payroll','GDP','PPI',
    'Retail Sales','Jobless Claims','FOMC Statement','Press Conference']

RSS_FEEDS = [
    ("Kitco",         "https://www.kitco.com/rss/KitcoNews.xml"),
    ("Investing Gold","https://www.investing.com/rss/commodities_Gold.rss"),
    ("Investing Econ","https://www.investing.com/rss/news_25.rss"),
    ("MarketWatch",   "https://feeds.marketwatch.com/marketwatch/topstories/"),
]

GOLD_BULLISH = {
    'rate cut':3,'rate cuts':3,'dovish':3,'cutting rates':3,'ease':2,'easing':2,
    'lower rates':3,'stimulus':2,'yields fall':3,'falling yields':3,'lower yields':2,
    'dollar weak':3,'dollar falls':3,'weaker dollar':3,'dollar slides':2,
    'safe haven':3,'safe-haven':3,'flight to safety':3,'geopolitical':2,'war':2,
    'conflict':2,'tension':2,'crisis':2,'risk-off':2,'recession':2,'slowdown':2,
    'banking stress':3,'bank failure':3,'inflation cools':2,'cooling inflation':2,
    'disinflation':2,'gold rallies':3,'gold surges':3,'gold jumps':3,'gold climbs':2,
    'gold gains':2,'record high':2,'central bank buying':3,'bullish gold':3,
    'de-dollarization':2,
}
GOLD_BEARISH = {
    'rate hike':3,'rate hikes':3,'hawkish':3,'raising rates':3,'higher rates':3,
    'rate hold':2,'hold rates':2,'tightening':2,'no cut':2,'no cuts':2,'restrictive':2,
    'yields rise':3,'rising yields':3,'higher yields':3,'yields jump':3,'yields surge':3,
    'dollar strong':3,'dollar rises':3,'stronger dollar':3,'dollar surges':3,
    'dollar rallies':3,'risk-on':2,'strong economy':2,'strong jobs':2,'jobs beat':2,
    'payrolls beat':2,'soft landing':2,'inflation hot':3,'hot inflation':3,
    'inflation jumps':2,'cpi beat':2,'cpi hot':3,'sticky inflation':2,
    'inflation accelerates':3,'gold falls':2,'gold drops':3,'gold slides':3,
    'gold tumbles':3,'gold lower':2,'bearish gold':3,'gold sells off':3,
    'etf outflows':2,'profit taking':1,
}

print("="*68)
print("  GOLD MASTER BRIEF — Integrated Signal")
now_utc = datetime.now(timezone.utc)
print(f"  {now_utc.strftime('%Y-%m-%d %H:%M')} UTC "
      f"({(now_utc+timedelta(hours=DUBAI_OFFSET)).strftime('%H:%M')} Dubai)")
print("="*68)

# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 1 — CALENDAR
# ══════════════════════════════════════════════════════════════════════════════

def get_calendar():
    print("\n[1/3] Economic calendar...")
    for url in ["https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
                "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"]:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code != 200: continue
            root = ET.fromstring(r.content)
            events = []
            for e in root.findall('event'):
                g = lambda t: (e.find(t).text if e.find(t) is not None else "")
                if g('country') != 'USD': continue
                if g('impact') not in ('High','Medium'): continue
                events.append({'title':g('title'),'impact':g('impact'),
                               'date':g('date'),'time':g('time')})
            print(f"  {len(events)} USD events this week")
            return events
        except Exception as ex:
            print(f"  source failed: {str(ex)[:40]}")
    print("  ⚠ unavailable — check forexfactory.com manually")
    return []

def parse_dt(date_str, time_str):
    try:
        d = None
        for fmt in ['%m-%d-%Y','%Y-%m-%d']:
            try: d = datetime.strptime(date_str, fmt); break
            except: continue
        if d is None: return None
        if not time_str or time_str.lower() in ('all day','tentative',''): return None
        t = datetime.strptime(time_str.strip().lower(), '%I:%M%p').time()
        return (datetime.combine(d.date(), t) + timedelta(hours=4)).replace(tzinfo=timezone.utc)
    except: return None

# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 2 — MACRO
# ══════════════════════════════════════════════════════════════════════════════

def get_macro():
    print("\n[2/3] Macro factors...")
    out = {}
    try:
        g = yf.download("GC=F", period="90d", interval="1d", progress=False)
        g.columns = [c[0].lower() for c in g.columns]
        out['gold_px']    = float(g['close'].iloc[-1])
        out['gold_ema50'] = float(g['close'].ewm(span=50, adjust=False).mean().iloc[-1])
        out['gold_trend'] = 'LONG' if out['gold_px'] > out['gold_ema50'] else 'SHORT'
        out['gold_mom5']  = float((g['close'].iloc[-1] / g['close'].iloc[-6] - 1) * 100)
        out['gold_ret1']  = float((g['close'].iloc[-1] / g['close'].iloc[-2] - 1) * 100)
        print(f"  Gold ${out['gold_px']:,.0f} | 50EMA ${out['gold_ema50']:,.0f} | {out['gold_trend']} | 5d mom {out['gold_mom5']:+.2f}%")
    except Exception as e: print(f"  gold failed: {e}")
    try:
        gdx = yf.download("GDX", period="10d", interval="1d", progress=False)
        gdx.columns = [c[0].lower() for c in gdx.columns]
        out['gdx_ret1'] = float((gdx['close'].iloc[-1] / gdx['close'].iloc[-2] - 1) * 100)
        out['gdx_lead'] = 'LEADING' if out['gdx_ret1'] > out.get('gold_ret1', 0) else 'LAGGING'
        print(f"  GDX {out['gdx_ret1']:+.2f}% vs Gold {out.get('gold_ret1',0):+.2f}% → miners {out['gdx_lead']}")
    except Exception as e: print(f"  gdx failed: {e}")
    try:
        dxy = yf.download("DX-Y.NYB", period="10d", interval="1d", progress=False)
        dxy.columns = [c[0].lower() for c in dxy.columns]
        out['dxy']     = float(dxy['close'].iloc[-1])
        out['dxy_chg'] = float((dxy['close'].iloc[-1]/dxy['close'].iloc[-2]-1)*100)
        out['dxy_dir'] = 'UP' if out['dxy_chg'] > 0 else 'DOWN'
        print(f"  DXY {out['dxy']:.2f} | {out['dxy_chg']:+.2f}% ({out['dxy_dir']})")
    except Exception as e: print(f"  dxy failed: {e}")
    try:
        tnx = yf.download("^TNX", period="10d", interval="1d", progress=False)
        tnx.columns = [c[0].lower() for c in tnx.columns]
        out['yield']     = float(tnx['close'].iloc[-1])
        out['yield_chg'] = float(tnx['close'].iloc[-1]-tnx['close'].iloc[-2])
        out['yield_dir'] = 'RISING' if out['yield_chg'] > 0 else 'FALLING'
        print(f"  10Y {out['yield']:.2f}% | {out['yield_chg']:+.2f} ({out['yield_dir']})")
    except Exception as e: print(f"  yield failed: {e}")
    return out

# ══════════════════════════════════════════════════════════════════════════════
#  LAYER 3 — NEWS
# ══════════════════════════════════════════════════════════════════════════════

def get_news_score():
    print("\n[3/3] News sentiment...")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    heads = []
    for name, url in RSS_FEEDS:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            for item in root.iter('item'):
                title = (item.findtext('title') or '').strip()
                desc  = (item.findtext('description') or '')[:200]
                pub   = item.findtext('pubDate') or ''
                if pub:
                    try:
                        pub_dt = parsedate_to_datetime(pub)
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                        if pub_dt < cutoff:
                            continue
                    except Exception:
                        pass
                heads.append({'source':name,'title':title,
                              'text':(title+' '+desc).lower()})
        except Exception:
            pass
    print(f"  {len(heads)} headlines in last {LOOKBACK_HOURS}h")
    bull = bear = 0
    relevant = []
    for h in heads:
        b = sum(w for kw,w in GOLD_BULLISH.items() if kw in h['text'])
        s = sum(w for kw,w in GOLD_BEARISH.items() if kw in h['text'])
        if b or s:
            relevant.append({**h,'net':b-s})
            bull += b; bear += s
    total = bull + bear
    score = round((bull-bear)/total*10, 1) if total else 0
    return score, sorted(relevant, key=lambda x: abs(x['net']), reverse=True)

# ══════════════════════════════════════════════════════════════════════════════
#  COMBINE
# ══════════════════════════════════════════════════════════════════════════════

events       = get_calendar()
macro        = get_macro()
news_score, relevant = get_news_score()

# --- Macro score: -4..+4 → normalize to -10..+10 ---
macro_raw = 0
macro_reasons = []
if macro.get('gold_trend') == 'LONG':
    macro_raw += 1; macro_reasons.append("Gold above 50 EMA (+)")
elif macro.get('gold_trend') == 'SHORT':
    macro_raw -= 1; macro_reasons.append("Gold below 50 EMA (−)")
if macro.get('yield_dir') == 'FALLING':
    macro_raw += 1; macro_reasons.append(f"Yields falling {macro.get('yield_chg',0):+.2f} (+)")
elif macro.get('yield_dir') == 'RISING':
    macro_raw -= 1; macro_reasons.append(f"Yields rising {macro.get('yield_chg',0):+.2f} (−)")
if macro.get('dxy_dir') == 'DOWN':
    macro_raw += 1; macro_reasons.append(f"Dollar down {macro.get('dxy_chg',0):+.2f}% (+)")
elif macro.get('dxy_dir') == 'UP':
    macro_raw -= 1; macro_reasons.append(f"Dollar up {macro.get('dxy_chg',0):+.2f}% (−)")
mom = macro.get('gold_mom5', 0)
if mom > 0:
    macro_raw += 1; macro_reasons.append(f"Gold 5d momentum {mom:+.2f}% (+)")
else:
    macro_raw -= 1; macro_reasons.append(f"Gold 5d momentum {mom:+.2f}% (−)")
if macro.get('gdx_lead') == 'LEADING':
    macro_raw += 1; macro_reasons.append(f"Miners leading gold {macro.get('gdx_ret1',0):+.2f}% vs {macro.get('gold_ret1',0):+.2f}% (+)")
elif macro.get('gdx_lead') == 'LAGGING':
    macro_raw -= 1; macro_reasons.append(f"Miners lagging gold {macro.get('gdx_ret1',0):+.2f}% vs {macro.get('gold_ret1',0):+.2f}% (−)")
n_components = 5 if macro.get('gdx_lead') else 4
macro_norm = macro_raw / n_components * 10   # -10..+10

# --- Combined score ---
combined = round(MACRO_WEIGHT * macro_norm + NEWS_WEIGHT * news_score, 1)

# --- Event risk gate ---
today = now_utc.date()
today_movers = []
for e in events:
    dt = parse_dt(e['date'], e['time'])
    if dt and dt.date() == today and (e['impact']=='High' or
            any(kw.lower() in e['title'].lower() for kw in GOLD_MOVERS)):
        today_movers.append({**e, 'dt':dt,
            'dubai':(dt+timedelta(hours=DUBAI_OFFSET)).strftime('%H:%M')})
today_movers.sort(key=lambda x: x['dt'])

condition = "GREEN"
risk_windows = []
sess_start = now_utc.replace(hour=SESS_START_H, minute=SESS_START_M, second=0, microsecond=0)
sess_end   = now_utc.replace(hour=SESS_END_H,   minute=SESS_END_M,   second=0, microsecond=0)
for m in today_movers:
    bs = m['dt'] - timedelta(minutes=EVENT_BUFFER_MIN)
    be = m['dt'] + timedelta(minutes=EVENT_BUFFER_MIN)
    if bs < sess_end and be > sess_start:
        condition = "RED" if m['impact']=='High' else ("YELLOW" if condition=="GREEN" else condition)
        risk_windows.append((m['title'],
            (bs+timedelta(hours=DUBAI_OFFSET)).strftime('%H:%M'),
            (be+timedelta(hours=DUBAI_OFFSET)).strftime('%H:%M')))

# ══════════════════════════════════════════════════════════════════════════════
#  OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "="*68)
print("  ★ GOLD MASTER BRIEF ★")
print("="*68)

print(f"\n  SIGNAL BREAKDOWN:")
print(f"  {'─'*60}")
print(f"  Macro bias   : {macro_norm:+.1f}/10  (weight {int(MACRO_WEIGHT*100)}%)")
for r in macro_reasons: print(f"      • {r}")
print(f"  News sentiment: {news_score:+.1f}/10  (weight {int(NEWS_WEIGHT*100)}%)")
if relevant:
    for h in relevant[:4]:
        a = "▲" if h['net']>0 else "▼"
        print(f"      {a} {h['title'][:50]}")
else:
    print(f"      (no gold-relevant headlines)")

print(f"\n  {'='*60}")
print(f"  ★ OVERALL GOLD SCORE: {combined:+.1f} / 10")
if combined >= 4:    bias = "🟢 BULLISH — favor LONG setups"
elif combined >= 1.5:bias = "🟢 MILD BULLISH — slight long bias"
elif combined > -1.5:bias = "⚪ NEUTRAL — trade technicals, no macro edge"
elif combined > -4:  bias = "🔴 MILD BEARISH — slight short bias"
else:                bias = "🔴 BEARISH — favor SHORT setups"
print(f"  {bias}")
print(f"  {'='*60}")

print(f"\n  EVENT RISK: ", end="")
if condition == "GREEN":
    print("🟢 GREEN — no high-impact events in session")
elif condition == "YELLOW":
    print("🟡 YELLOW — medium event risk")
else:
    print("🔴 RED — high-impact event in session")

if today_movers:
    print(f"\n  Today's events:")
    for m in today_movers:
        star = "🔴" if m['impact']=='High' else "🟠"
        print(f"    {star} {m['dubai']} Dubai — {m['title']}")
if risk_windows:
    print(f"\n  ⚠ AVOID (Dubai time):")
    for t,s,e in risk_windows:
        print(f"    {s}–{e}  {t}")

# Final recommendation
print(f"\n  {'─'*60}")
print(f"  ► RECOMMENDATION:")
if condition == "RED":
    print(f"    WAIT. High-impact event in your session window.")
    print(f"    Skip the pre-event chop. Trade 30-60 min AFTER release,")
    print(f"    in the direction the score + post-event price action agree on.")
elif abs(combined) < 1.5:
    print(f"    Trade your 4H system on technicals alone. No macro edge today.")
    print(f"    Take only the cleanest VAL/POC setups. Normal size.")
else:
    direction = "LONG" if combined > 0 else "SHORT"
    size = "full size" if condition=="GREEN" else "half size"
    print(f"    Macro + news agree: {direction} bias. When your 4H system")
    print(f"    gives a {direction} setup at VWAP/POC, take it at {size}.")
    print(f"    If your 4H setup is OPPOSITE to this bias → skip it.")
print(f"  {'─'*60}")

# Log
with open("gold_master_log.txt", "a") as f:
    f.write(f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC | score {combined:+.1f} | "
            f"{condition} | macro {macro_norm:+.1f} news {news_score:+.1f} | "
            f"gold ${macro.get('gold_px',0):,.0f}\n")
print(f"\n  Logged to gold_master_log.txt")
print("="*68)
