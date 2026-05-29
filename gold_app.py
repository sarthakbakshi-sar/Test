"""
GOLD LIVE SIGNAL — Streamlit App
==================================
Run: streamlit run gold_app.py

Auto-refreshes every 5 minutes.
Shows macro score, session status, active setup with entry/SL/TP levels.
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import time
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG ────────────────────────────────────────────────────────────────────
DUBAI_OFFSET   = 4
SESS_OPEN_UTC  = (13, 30)
SESS_CLOSE_UTC = (17,  0)
ENTRY_START_UTC= (14,  0)   # skip first 30min chop
ENTRY_END_UTC  = (16, 30)   # stop 30min before session close
ACCOUNT        = 10_000
RISK_PCT       = 0.03
SL_MULT        = 0.75
TP1_MULT       = 1.5
TP2_MULT       = 2.5
REFRESH_SECS   = 300        # 5 minutes
MIN_SCORE      = 2.0
LOOKBACK_HOURS = 12

RSS_FEEDS = [
    ("Kitco",       "https://www.kitco.com/rss/KitcoNews.xml"),
    ("Investing",   "https://www.investing.com/rss/commodities_Gold.rss"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
]
GOLD_BULLISH = {
    'rate cut':3,'dovish':3,'lower rates':3,'yields fall':3,'falling yields':3,
    'dollar weak':3,'dollar falls':3,'safe haven':3,'geopolitical':2,'war':2,
    'conflict':2,'tension':2,'crisis':2,'risk-off':2,'recession':2,
    'gold rallies':3,'gold surges':3,'gold jumps':3,'gold climbs':2,
    'central bank buying':3,'record high':2,
}
GOLD_BEARISH = {
    'rate hike':3,'hawkish':3,'higher rates':3,'yields rise':3,'rising yields':3,
    'dollar strong':3,'dollar rises':3,'dollar surges':3,'risk-on':2,
    'strong jobs':2,'payrolls beat':2,'inflation hot':3,'cpi hot':3,
    'gold falls':2,'gold drops':3,'gold slides':3,'gold tumbles':3,
}

st.set_page_config(
    page_title="Gold Live Signal",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ── CACHED DATA FUNCTIONS ─────────────────────────────────────────────────────
@st.cache_data(ttl=300)   # refresh every 5 min
def get_macro():
    out = {}
    try:
        g = yf.download("GC=F", period="90d", interval="1d", progress=False)
        g.columns = [c[0].lower() for c in g.columns]
        out['gold_px']   = float(g['close'].iloc[-1])
        out['gold_ema50']= float(g['close'].ewm(span=50, adjust=False).mean().iloc[-1])
        out['gold_trend']= 'LONG' if out['gold_px'] > out['gold_ema50'] else 'SHORT'
        out['gold_mom5'] = float((g['close'].iloc[-1]/g['close'].iloc[-6]-1)*100)
        out['gold_ret1'] = float((g['close'].iloc[-1]/g['close'].iloc[-2]-1)*100)
    except: pass
    try:
        tnx = yf.download("^TNX", period="10d", interval="1d", progress=False)
        tnx.columns = [c[0].lower() for c in tnx.columns]
        out['yield_chg'] = float(tnx['close'].iloc[-1]-tnx['close'].iloc[-2])
        out['yield_dir'] = 'FALLING' if out['yield_chg']<0 else 'RISING'
        out['yield_val'] = float(tnx['close'].iloc[-1])
    except: pass
    try:
        dxy = yf.download("DX-Y.NYB", period="10d", interval="1d", progress=False)
        dxy.columns = [c[0].lower() for c in dxy.columns]
        out['dxy_chg'] = float((dxy['close'].iloc[-1]/dxy['close'].iloc[-2]-1)*100)
        out['dxy_dir'] = 'DOWN' if out['dxy_chg']<0 else 'UP'
        out['dxy_val'] = float(dxy['close'].iloc[-1])
    except: pass
    try:
        gdx = yf.download("GDX", period="10d", interval="1d", progress=False)
        gdx.columns = [c[0].lower() for c in gdx.columns]
        out['gdx_ret1'] = float((gdx['close'].iloc[-1]/gdx['close'].iloc[-2]-1)*100)
        out['gdx_lead'] = out['gdx_ret1'] > out.get('gold_ret1', 0)
    except: pass
    return out

@st.cache_data(ttl=300)
def get_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    heads, bull, bear = [], 0, 0
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
                heads.append({'title': title, 'text': (title+' '+desc).lower(), 'source': name})
        except: pass
    for h in heads:
        bull += sum(w for kw,w in GOLD_BULLISH.items() if kw in h['text'])
        bear += sum(w for kw,w in GOLD_BEARISH.items() if kw in h['text'])
    total = bull + bear
    score = round((bull-bear)/total*10, 1) if total else 0
    relevant = [h for h in heads if
                any(kw in h['text'] for kw in list(GOLD_BULLISH)+list(GOLD_BEARISH))]
    return score, len(heads), relevant[:5]

@st.cache_data(ttl=60)    # price refreshes every 1 min
def get_price_and_levels():
    out = {}
    try:
        df = yf.download("GC=F", period="10d", interval="1h", progress=False)
        df.columns = [c[0].lower() for c in df.columns]
        df.index   = pd.to_datetime(df.index).tz_localize(None)
        df.dropna(inplace=True)

        # Current price
        out['price'] = float(df['close'].iloc[-1])

        # EMA9/21 on 1h
        df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        tr = pd.concat([df['high']-df['low'],
                        (df['high']-df['close'].shift()).abs(),
                        (df['low'] -df['close'].shift()).abs()],axis=1).max(axis=1)
        df['atr'] = tr.ewm(span=14, adjust=False).mean()

        out['ema9']  = float(df['ema9'].iloc[-1])
        out['ema21'] = float(df['ema21'].iloc[-1])
        out['atr']   = float(df['atr'].iloc[-1])
        out['ema_bull'] = out['ema9'] > out['ema21']

        # Session VWAP
        now   = datetime.utcnow()
        today = now.date()
        o_ts  = pd.Timestamp(today) + pd.Timedelta(hours=SESS_OPEN_UTC[0], minutes=SESS_OPEN_UTC[1])
        sess  = df[df.index >= o_ts]
        if len(sess) >= 2:
            tp  = (sess['high']+sess['low']+sess['close'])/3
            vol = sess['volume'].replace(0,1)
            out['vwap']     = float((tp*vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
            out['vwap_std'] = float(tp.std()) if len(tp)>1 else float(out['atr'])
            out['vwap_label'] = "Today's session VWAP"
        else:
            yesterday = today - timedelta(days=1)
            y_ts = pd.Timestamp(yesterday) + pd.Timedelta(hours=SESS_OPEN_UTC[0], minutes=SESS_OPEN_UTC[1])
            y_end= pd.Timestamp(yesterday) + pd.Timedelta(hours=SESS_CLOSE_UTC[0], minutes=SESS_CLOSE_UTC[1])
            ysess = df[(df.index>=y_ts)&(df.index<=y_end)]
            if len(ysess)>=2:
                tp  = (ysess['high']+ysess['low']+ysess['close'])/3
                vol = ysess['volume'].replace(0,1)
                out['vwap']     = float((tp*vol).cumsum().iloc[-1]/vol.cumsum().iloc[-1])
                out['vwap_std'] = float(tp.std()) if len(tp)>1 else float(out['atr'])
                out['vwap_label'] = "Yesterday's session VWAP"
            else:
                out['vwap'] = out['price']; out['vwap_std'] = out['atr']
                out['vwap_label'] = "Est. VWAP"

        # 4H context
        g4 = df.resample('4h', label='left', closed='left').agg(
            {'close':'last','high':'max','low':'min','open':'first','volume':'sum'}).dropna()
        g4['ema9']  = g4['close'].ewm(span=9,  adjust=False).mean()
        g4['ema21'] = g4['close'].ewm(span=21, adjust=False).mean()
        g4['ema50'] = g4['close'].ewm(span=50, adjust=False).mean()
        out['h4_ema9']  = float(g4['ema9'].iloc[-1])
        out['h4_ema21'] = float(g4['ema21'].iloc[-1])
        out['h4_ema50'] = float(g4['ema50'].iloc[-1])
        out['h4_bull']  = out['h4_ema9'] > out['h4_ema21']

        # Prev session H/L
        prev_day = df[df.index.date == (today - timedelta(days=1))]
        ps = prev_day[(prev_day.index.time >= pd.Timestamp('13:30').time()) &
                      (prev_day.index.time <= pd.Timestamp('17:00').time())]
        out['pdh'] = float(ps['high'].max()) if len(ps) else None
        out['pdl'] = float(ps['low'].min())  if len(ps) else None

    except Exception as e:
        out['error'] = str(e)
    return out

# ── COMPUTE MACRO SCORE ────────────────────────────────────────────────────────
def compute_score(macro):
    raw = 0; reasons = []
    if macro.get('gold_trend')=='LONG':
        raw+=1; reasons.append(('Gold above 50 EMA', True))
    elif macro.get('gold_trend')=='SHORT':
        raw-=1; reasons.append(('Gold below 50 EMA', False))
    if macro.get('yield_dir')=='FALLING':
        raw+=1; reasons.append((f"Yields falling {macro.get('yield_chg',0):+.2f}bp", True))
    elif macro.get('yield_dir')=='RISING':
        raw-=1; reasons.append((f"Yields rising {macro.get('yield_chg',0):+.2f}bp", False))
    if macro.get('dxy_dir')=='DOWN':
        raw+=1; reasons.append((f"Dollar down {macro.get('dxy_chg',0):+.2f}%", True))
    elif macro.get('dxy_dir')=='UP':
        raw-=1; reasons.append((f"Dollar up {macro.get('dxy_chg',0):+.2f}%", False))
    mom = macro.get('gold_mom5', 0)
    if mom > 0:
        raw+=1; reasons.append((f"5d momentum {mom:+.2f}%", True))
    else:
        raw-=1; reasons.append((f"5d momentum {mom:+.2f}%", False))
    if macro.get('gdx_lead'):
        raw+=1; reasons.append((f"Miners leading gold", True))
    else:
        raw-=1; reasons.append((f"Miners lagging gold", False))
    n = len(reasons)
    score = round(raw/n*10, 1) if n else 0
    return score, reasons

# ── SESSION STATE ─────────────────────────────────────────────────────────────
def get_session_state():
    now = datetime.utcnow()
    h   = now.hour + now.minute/60
    sess_open  = SESS_OPEN_UTC[0]  + SESS_OPEN_UTC[1]/60
    sess_close = SESS_CLOSE_UTC[0] + SESS_CLOSE_UTC[1]/60
    entry_start= ENTRY_START_UTC[0]+ ENTRY_START_UTC[1]/60
    entry_end  = ENTRY_END_UTC[0]  + ENTRY_END_UTC[1]/60

    if h < sess_open:
        mins = int((sess_open - h) * 60)
        return 'PRE', f"Session opens in {mins}min"
    elif sess_open <= h < entry_start:
        return 'CHOP', "Opening window — wait (13:30–14:00 UTC)"
    elif entry_start <= h <= entry_end:
        return 'ENTRY', "Entry window OPEN ✅"
    elif entry_end < h <= sess_close:
        return 'LATE', "Entry window closed — manage open trades only"
    else:
        return 'CLOSED', "Session closed"

# ── SETUP DETECTION ────────────────────────────────────────────────────────────
def detect_setup(levels, macro_dir, score):
    if not levels or 'price' not in levels: return None
    px   = levels['price']
    vwap = levels.get('vwap', px)
    std  = max(levels.get('vwap_std', 15), 1)
    atr  = levels.get('atr', 15)
    dist = (px - vwap) / std

    ema_ok = (macro_dir==1 and levels.get('ema_bull', False)) or \
             (macro_dir==-1 and not levels.get('ema_bull', True))
    h4_ok  = (macro_dir==1 and levels.get('h4_bull', False)) or \
             (macro_dir==-1 and not levels.get('h4_bull', True))
    short_gate = macro_dir==-1 and px > levels.get('h4_ema50', px+1)

    vwap_zone = (macro_dir==1 and -1.0<=dist<=0.3) or \
                (macro_dir==-1 and -0.3<=dist<=1.0)

    entry = round(vwap if macro_dir==1 else vwap, 1)
    if macro_dir==1:
        sl  = round(entry - SL_MULT*atr, 1)
        tp1 = round(entry + TP1_MULT*atr, 1)
        tp2 = round(entry + TP2_MULT*atr, 1)
    else:
        sl  = round(entry + SL_MULT*atr, 1)
        tp1 = round(entry - TP1_MULT*atr, 1)
        tp2 = round(entry - TP2_MULT*atr, 1)

    risk_usd   = ACCOUNT * RISK_PCT
    size_oz    = round(risk_usd / (SL_MULT*atr), 1)
    size_label = "FULL SIZE" if abs(score)>=5 else "HALF SIZE"

    return {
        'ema_ok': ema_ok, 'h4_ok': h4_ok, 'short_gate': short_gate,
        'vwap_zone': vwap_zone, 'dist': dist,
        'entry': entry, 'sl': sl, 'tp1': tp1, 'tp2': tp2,
        'size_oz': size_oz, 'size_label': size_label,
        'all_clear': ema_ok and h4_ok and vwap_zone and not short_gate
    }

# ══════════════════════════════════════════════════════════════════════════════
#  RENDER UI
# ══════════════════════════════════════════════════════════════════════════════

now_utc   = datetime.now(timezone.utc)
now_dubai = now_utc + timedelta(hours=DUBAI_OFFSET)
sess_state, sess_msg = get_session_state()

# Header
st.markdown(f"""
<h1 style='text-align:center; margin-bottom:0'>📊 Gold Live Signal</h1>
<p style='text-align:center; color:gray; margin-top:4px'>
{now_utc.strftime('%Y-%m-%d %H:%M')} UTC &nbsp;|&nbsp;
<b>{now_dubai.strftime('%H:%M')} Dubai</b> &nbsp;|&nbsp;
Auto-refreshes every 5 min
</p>
""", unsafe_allow_html=True)

st.divider()

with st.spinner("Loading data..."):
    macro  = get_macro()
    levels = get_price_and_levels()
    news_score, n_heads, relevant = get_news()

score, reasons = compute_score(macro)
combined = round(0.60*score + 0.40*news_score, 1)
macro_dir = 1 if combined > MIN_SCORE else (-1 if combined < -MIN_SCORE else 0)
setup = detect_setup(levels, macro_dir, combined) if macro_dir != 0 else None

# ── ROW 1: Price + Score + Session ────────────────────────────────────────────
col1, col2, col3 = st.columns([1.2, 1.2, 1.2])

with col1:
    px = levels.get('price', 0)
    color = "#00cc44" if levels.get('ema_bull') else "#ff4444"
    st.markdown(f"""
    <div style='background:#1a1a2e;border-radius:12px;padding:20px;text-align:center'>
    <div style='color:gray;font-size:13px'>GOLD (XAU/USD)</div>
    <div style='color:{color};font-size:42px;font-weight:bold'>${px:,.1f}</div>
    <div style='color:gray;font-size:12px'>ATR: ${levels.get('atr',0):.1f} &nbsp;|&nbsp; EMA9: ${levels.get('ema9',0):,.1f}</div>
    </div>""", unsafe_allow_html=True)

with col2:
    if   combined >= 5:   sc_color, sc_label = "#00cc44", "BULLISH"
    elif combined >= 2:   sc_color, sc_label = "#66dd66", "MILD BULLISH"
    elif combined > -2:   sc_color, sc_label = "#aaaaaa", "NEUTRAL"
    elif combined > -5:   sc_color, sc_label = "#ff8844", "MILD BEARISH"
    else:                 sc_color, sc_label = "#ff4444", "BEARISH"
    st.markdown(f"""
    <div style='background:#1a1a2e;border-radius:12px;padding:20px;text-align:center'>
    <div style='color:gray;font-size:13px'>OVERALL SCORE</div>
    <div style='color:{sc_color};font-size:42px;font-weight:bold'>{combined:+.1f}</div>
    <div style='color:{sc_color};font-size:16px;font-weight:bold'>{sc_label}</div>
    </div>""", unsafe_allow_html=True)

with col3:
    if   sess_state == 'ENTRY':  ss_color, ss_icon = "#00cc44", "🟢"
    elif sess_state == 'PRE':    ss_color, ss_icon = "#aaaaaa", "⏳"
    elif sess_state == 'CHOP':   ss_color, ss_icon = "#ffaa00", "⚠️"
    elif sess_state == 'LATE':   ss_color, ss_icon = "#ff8844", "🔶"
    else:                        ss_color, ss_icon = "#666666", "⛔"
    st.markdown(f"""
    <div style='background:#1a1a2e;border-radius:12px;padding:20px;text-align:center'>
    <div style='color:gray;font-size:13px'>SESSION (DUBAI)</div>
    <div style='font-size:28px'>{ss_icon}</div>
    <div style='color:{ss_color};font-size:16px;font-weight:bold'>{sess_msg}</div>
    <div style='color:gray;font-size:11px;margin-top:6px'>Entry window: 18:00–20:30 Dubai</div>
    </div>""", unsafe_allow_html=True)

st.divider()

# ── ROW 2: Setup + Macro Components ───────────────────────────────────────────
col_setup, col_macro = st.columns([1.4, 1])

with col_setup:
    st.subheader("🎯 Active Setup")
    if macro_dir == 0:
        st.info("**No trade today** — Macro score is neutral. Trade technicals only or sit out.")
    elif sess_state == 'CLOSED':
        st.warning("Session closed. Check back tomorrow at 17:00 Dubai.")
    elif setup:
        dir_label = "▲ LONG" if macro_dir==1 else "▼ SHORT"
        dir_color = "green" if macro_dir==1 else "red"

        checks = [
            ("EMA9/21 aligned (1H)",       setup['ema_ok']),
            ("4H trend agrees",             setup['h4_ok']),
            ("Price in VWAP zone",          setup['vwap_zone']),
            ("Short gate clear (4H EMA50)", not setup['short_gate']),
        ]
        all_green = all(v for _,v in checks)

        if all_green and sess_state == 'ENTRY':
            st.success(f"**{dir_label} SETUP ACTIVE** — All conditions met")
        elif all_green:
            st.warning(f"**{dir_label} setup ready** — Wait for entry window (18:00 Dubai)")
        else:
            st.warning(f"Bias: **{dir_label}** — Waiting for conditions")

        for label, ok in checks:
            icon = "✅" if ok else "⏳"
            st.markdown(f"{icon} {label}")

        if all_green:
            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                st.metric("Entry zone", f"${levels.get('vwap',0):,.1f}", help="VWAP level")
                st.metric("Stop Loss",  f"${setup['sl']:,.1f}",
                          delta=f"{setup['sl']-setup['entry']:+.1f}",
                          delta_color="inverse")
            with c2:
                st.metric("TP1 (60%)", f"${setup['tp1']:,.1f}",
                          delta=f"{setup['tp1']-setup['entry']:+.1f}")
                st.metric("TP2 (40%)", f"${setup['tp2']:,.1f}",
                          delta=f"{setup['tp2']-setup['entry']:+.1f}")

            risk_pts = abs(setup['entry']-setup['sl'])
            st.info(f"**Size: {setup['size_oz']} oz** ({setup['size_label']})  "
                    f"| Risk: ${ACCOUNT*RISK_PCT:.0f}  "
                    f"| SL: {risk_pts:.1f} pts")
    else:
        st.info("Calculating setup...")

with col_macro:
    st.subheader("📊 Macro Components")
    for label, bullish in reasons:
        icon = "🟢" if bullish else "🔴"
        st.markdown(f"{icon} {label}")

    st.divider()
    st.caption(f"Macro score: **{score:+.1f}**/10 (60% weight)")
    st.caption(f"News score:  **{news_score:+.1f}**/10 (40% weight) — {n_heads} headlines")

# ── ROW 3: Key Levels + News ───────────────────────────────────────────────────
col_levels, col_news = st.columns([1, 1.4])

with col_levels:
    st.subheader("📍 Key Levels")
    vwap   = levels.get('vwap', 0)
    atr    = levels.get('atr', 15)
    px     = levels.get('price', 0)

    level_data = {
        "Level": [],
        "Price": [],
        "Distance": []
    }
    for name, val in [
        ("Prev Session High", levels.get('pdh')),
        ("VWAP",              vwap),
        ("Prev Session Low",  levels.get('pdl')),
        ("4H EMA50",          levels.get('h4_ema50')),
    ]:
        if val:
            dist = px - val
            level_data["Level"].append(name)
            level_data["Price"].append(f"${val:,.1f}")
            level_data["Distance"].append(f"{dist:+.1f}")

    if level_data["Level"]:
        st.dataframe(pd.DataFrame(level_data), hide_index=True, use_container_width=True)

    st.caption(f"VWAP band: ${vwap-1.5*levels.get('vwap_std',atr):,.1f} – ${vwap+1.5*levels.get('vwap_std',atr):,.1f}")

with col_news:
    st.subheader(f"📰 News Sentiment ({news_score:+.1f}/10)")
    if relevant:
        for h in relevant[:4]:
            bull_hit = any(kw in h['text'] for kw in GOLD_BULLISH)
            icon = "▲" if bull_hit else "▼"
            color = "green" if bull_hit else "red"
            st.markdown(f":{color}[{icon}] **{h['source']}** — {h['title'][:70]}")
    else:
        st.caption("No gold-relevant headlines in last 12h")

# ── FOOTER: Session schedule ───────────────────────────────────────────────────
st.divider()
sched = {
    "17:30": ("Session opens", sess_state == 'PRE' or sess_state == 'CHOP'),
    "17:30–18:00": ("Wait — opening chop", sess_state == 'CHOP'),
    "18:00–20:30": ("✅ Entry window", sess_state == 'ENTRY'),
    "20:30": ("Entry window closes — manage only", sess_state == 'LATE'),
    "21:00": ("Session close — exit all", sess_state == 'CLOSED'),
}
cols = st.columns(5)
for i, (t, (label, active)) in enumerate(sched.items()):
    with cols[i]:
        bg = "#003322" if active else "#111122"
        st.markdown(f"""<div style='background:{bg};border-radius:8px;padding:10px;text-align:center'>
        <div style='color:#aaa;font-size:11px'>{t} Dubai</div>
        <div style='font-size:12px;margin-top:4px'>{label}</div>
        </div>""", unsafe_allow_html=True)

# ── AUTO-REFRESH ──────────────────────────────────────────────────────────────
st.divider()
last_update = now_utc.strftime('%H:%M:%S UTC')
col_r1, col_r2 = st.columns([3,1])
with col_r1:
    st.caption(f"Last updated: {last_update}  |  Next refresh in ~5 min")
with col_r2:
    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()

# Auto-refresh countdown
time.sleep(REFRESH_SECS)
st.cache_data.clear()
st.rerun()
