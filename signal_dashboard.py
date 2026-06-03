#!/usr/bin/env python3
"""
SIGNAL COMMAND CENTER — Gold · Oil · Copper
Live dashboard (Python version of signal_combined.gs)
"""

import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
import warnings
warnings.filterwarnings('ignore')

# ── CONFIG
ACCOUNT  = 10000
RISK_PCT = 0.03
SL_MULT  = 0.75
TP1_MULT = 1.50
TP2_MULT = 2.50
TP1_FRAC = 0.60

# Sessions (UTC decimal hours)
AU_SESS_OPEN, AU_SESS_CLOSE   = 13.50, 17.00
AU_ENTRY_START, AU_ENTRY_END  = 14.00, 16.50
CL_SESS_OPEN, CL_SESS_CLOSE   = 14.00, 19.00
CL_ENTRY_START, CL_ENTRY_END  = 14.50, 18.50
HG_SESS_OPEN, HG_SESS_CLOSE   = 13.00, 17.50
HG_ENTRY_START, HG_ENTRY_END  = 13.50, 17.00

AU_SKIP_MONTHS = [5]
CL_SKIP_MONTHS = [0, 3, 5, 8, 9]
CL_SKIP_WED    = True
HG_SKIP_MONTHS = [0, 4, 6, 11]
HG_SKIP_FRI    = True
CL_SCORE_MIN   = 2
HG_SCORE_MIN   = 2

MNAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
DOW    = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']

# ── COLORS (ANSI)
R  = '\033[91m'   # red
G  = '\033[92m'   # green
Y  = '\033[93m'   # yellow
B  = '\033[94m'   # blue
C  = '\033[96m'   # cyan
M  = '\033[95m'   # magenta
W  = '\033[97m'   # white
DIM= '\033[90m'   # dim
GOLD_C = '\033[33m'
OIL_C  = '\033[38;5;208m'
CU_C   = '\033[38;5;130m'
RESET  = '\033[0m'
BOLD   = '\033[1m'

def col(text, color): return f"{color}{text}{RESET}"
def bold(text): return f"{BOLD}{text}{RESET}"

# ── EMA
def ema(series, n):
    return pd.Series(series).ewm(span=n, adjust=False).mean().values

# ── ATR14 from bar DataFrame (needs h, l, c columns)
def calc_atr14(bars):
    if bars is None or len(bars) < 15:
        return None
    h, l, c = bars['h'].values, bars['l'].values, bars['c'].values
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    return float(pd.Series(tr).ewm(span=14, adjust=False).mean().iloc[-1])

# ── Daily ATR from 1h bars
def get_daily_atr(bars1h):
    if bars1h is None or len(bars1h) < 15:
        return None
    daily = bars1h.resample('D').agg({'h': 'max', 'l': 'min', 'c': 'last'}).dropna()
    if len(daily) < 3:
        return None
    h, l, c = daily['h'].values, daily['l'].values, daily['c'].values
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    return float(pd.Series(tr).ewm(span=14, adjust=False).mean().iloc[-1])

# ── Session VWAP from 15m bars
def sess_vwap(bars15, utc_open_h, utc_open_m=0):
    if bars15 is None or len(bars15) == 0:
        return {'vwap': 0, 'std': 0.0001, 'n': 0}
    now_utc = datetime.now(timezone.utc)
    sess_start = now_utc.replace(hour=utc_open_h, minute=utc_open_m, second=0, microsecond=0)
    sess = bars15[bars15.index >= sess_start]
    if len(sess) < 2:
        return {'vwap': 0, 'std': 0.0001, 'n': len(sess)}
    tp = (sess['h'] + sess['l'] + sess['c']) / 3
    avg = tp.mean()
    std = max(tp.std(ddof=0), 0.0001)
    return {'vwap': float(avg), 'std': float(std), 'n': len(sess)}

# ── Fetch daily closes
def fetch_daily(syms):
    out = {}
    for sym in syms:
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period='3mo', interval='1d', auto_adjust=True)
            if len(hist) > 5:
                out[sym] = hist['Close'].dropna().values
        except Exception as e:
            pass
    return out

# ── Fetch intraday bars as DataFrame with h/l/c columns, UTC-indexed
def fetch_bars(sym, interval, period):
    try:
        tk = yf.Ticker(sym)
        hist = tk.history(period=period, interval=interval, auto_adjust=True)
        if hist.empty:
            return None
        hist.index = hist.index.tz_convert('UTC')
        df = hist[['High','Low','Close']].rename(columns={'High':'h','Low':'l','Close':'c'})
        return df
    except:
        return None

# ── Session state
def sess_state(nowH, open_, entry_start, entry_end, close_):
    if nowH < open_:        return 'BEFORE'
    if nowH < entry_start:  return 'CHOP'
    if nowH <= entry_end:   return 'ENTRY'
    if nowH <= close_:      return 'LATE'
    return 'CLOSED'

# ══════════════════════════════════════════════════════════════
# MACRO SCORES
# ══════════════════════════════════════════════════════════════

def get_au_macro(d):
    gold = d.get('GC=F'); dxy = d.get('DX-Y.NYB'); tnx = d.get('^TNX')
    gdx  = d.get('GDX');  vix = d.get('^VIX')
    res  = {'raw':0,'score':0,'dir':1,'strength':'WEAK','components':[],'error':None,'price':None,'vix':None}
    if gold is None or len(gold) < 10:
        res['error'] = 'GC=F unavailable'; return res
    n = len(gold)
    res['price'] = gold[-1]
    res['vix']   = round(float(vix[-1]), 1) if vix is not None and len(vix) else None
    e50 = ema(gold, min(50, n))
    s1 = 1 if gold[-2] > e50[-2] else -1
    s2 = (1 if tnx[-2] < tnx[-3] else -1) if tnx is not None and len(tnx) >= 3 else 0
    s3 = (1 if dxy[-2] < dxy[-3] else -1) if dxy is not None and len(dxy) >= 3 else 0
    s4 = (1 if gold[-2] > gold[-7] else -1) if n >= 8 else 0
    s5 = 0
    if gdx is not None and len(gdx) >= 3 and n >= 3:
        s5 = 1 if (gdx[-2]/gdx[-3]) > (gold[-2]/gold[-3]) else -1
    raw = s1 + s2 + s3 + s4 + s5
    res['raw']    = raw
    res['score']  = round(raw/5*100, 1)
    res['dir']    = 1 if raw >= 0 else -1
    ag = abs(raw)
    res['strength'] = 'STRONG' if ag >= 5 else 'MODERATE' if ag >= 3 else 'WEAK'
    mom5 = round((gold[-2]/gold[-7]-1)*100, 2) if n >= 8 else 0
    dxy_note = 'n/a' if s3==0 else ('falling ↓ (gold +)' if s3>0 else 'rising ↑ (gold −)')
    tnx_note = 'n/a' if s2==0 else ('falling ↓ (bullish)' if s2>0 else 'rising ↑ (bearish)')
    gdx_note = 'n/a' if s5==0 else ('leading ↑ (bullish)' if s5>0 else 'lagging ↓ (weak)')
    res['components'] = [
        {'name':'Gold vs EMA50',    'val':s1, 'note':f'{"ABOVE" if s1>0 else "BELOW"} EMA50 (${round(e50[-2])})'},
        {'name':'10Y yield dir',    'val':s2, 'note':tnx_note},
        {'name':'DXY direction',    'val':s3, 'note':dxy_note},
        {'name':'Gold 5d momentum', 'val':s4, 'note':f'{"+"+str(mom5) if mom5>=0 else mom5}% 5d'},
        {'name':'GDX vs Gold',      'val':s5, 'note':gdx_note},
    ]
    return res

def get_cl_macro(d):
    cl=d.get('CL=F'); hg=d.get('HG=F'); spy=d.get('SPY'); rb=d.get('RB=F')
    bz=d.get('BZ=F'); uso=d.get('USO'); usl=d.get('USL'); vix=d.get('^VIX')
    res = {'raw':0,'score':0,'dir':1,'strength':'WEAK','components':[],'error':None,'price':None,'vix':None}
    if cl is None or len(cl) < 12:
        res['error'] = 'CL=F unavailable'; return res
    n = len(cl)
    res['price'] = cl[-1]
    res['vix']   = round(float(vix[-1]), 1) if vix is not None and len(vix) else None
    e50 = ema(cl, min(50, n))
    c1 = 1 if cl[-2] > e50[-2] else -1
    c2 = (1 if hg[-2]/hg[-7] > 1 else -1) if hg is not None and len(hg) >= 8 else 0
    c3 = (1 if cl[-2] > cl[-7] else -1) if n >= 8 else 0
    c4 = (1 if spy[-2]/spy[-7] > 1 else -1) if spy is not None and len(spy) >= 8 else 0
    c5 = 0; crack_now = None
    if rb is not None and len(rb) >= 8 and n >= 8:
        mn = min(len(rb), n)
        crack_now = round(rb[mn-2]*42 - cl[n-2])
        c5 = 1 if (rb[mn-2]*42 - cl[n-2]) > (rb[mn-7]*42 - cl[n-7]) else -1
    c6 = 0; bwti_z = None
    if bz is not None and len(bz) >= 8 and n >= 8:
        mn2 = min(len(bz), n)
        bw = [bz[i] - cl[i] for i in range(mn2)]
        win = bw[max(0, len(bw)-61):-1]
        wm = np.mean(win); ws = np.std(win)
        if ws > 0.01:
            bwti_z = (bw[-2] - wm) / ws
            c6 = 1 if bwti_z < -0.2 else (-1 if bwti_z > 0.5 else 0)
    c7 = 0; term_r = None
    if uso is not None and len(uso) >= 8 and usl is not None and len(usl) >= 8:
        tl = min(len(uso), len(usl))
        term_r = round(uso[tl-2]/usl[tl-2], 3)
        c7 = 1 if (uso[tl-2]/usl[tl-2]) > (uso[tl-7]/usl[tl-7]) else -1
    raw = c1+c2+c3+c4+c5+c6+c7
    res['raw'] = raw; res['score'] = raw; res['dir'] = 1 if raw >= 0 else -1
    ar = abs(raw)
    res['strength'] = 'STRONG' if ar >= 5 else 'MODERATE' if ar >= 3 else 'WEAK'
    mom5 = round((cl[-2]/cl[-7]-1)*100, 2) if n >= 8 else 0
    bw_spread = round(float(bz[-2] - cl[-2]), 2) if bz is not None and len(bz) >= 2 and n >= 2 else None
    res['components'] = [
        {'name':'CL vs EMA50',        'val':c1, 'note':f'{"ABOVE" if c1>0 else "BELOW"} EMA50 (${round(e50[-2],2)})'},
        {'name':'Copper 5d dir',      'val':c2, 'note':'rising ↑' if c2>0 else ('falling ↓' if c2<0 else 'n/a')},
        {'name':'Oil 5d momentum',    'val':c3, 'note':f'{"+"+str(mom5) if mom5>=0 else mom5}% 5d'},
        {'name':'SPY 5d dir',         'val':c4, 'note':'rising ↑ (risk-on)' if c4>0 else ('falling ↓ (risk-off)' if c4<0 else 'n/a')},
        {'name':'RBOB crack',         'val':c5, 'note':f'crack=${crack_now}/bbl  {"widening ↑" if c5>0 else "narrowing ↓"}' if crack_now is not None else 'n/a'},
        {'name':'Brent-WTI z60',      'val':c6, 'note':f'z={round(bwti_z,2)}, spread=${bw_spread}  {"bullish WTI" if c6==1 else "bearish WTI" if c6==-1 else "neutral"}' if bwti_z is not None else 'n/a'},
        {'name':'Term-struct USO/USL','val':c7, 'note':f'ratio={term_r}  {"backwardation ↑" if c7>0 else "contango ↓"}' if term_r is not None else 'n/a'},
    ]
    return res

def get_hg_macro(d):
    hg=d.get('HG=F'); dx=d.get('DX-Y.NYB'); spy=d.get('SPY'); cl=d.get('CL=F'); vix=d.get('^VIX')
    res = {'raw':0,'score':0,'dir':1,'strength':'WEAK','components':[],'error':None,'price':None,'vix':None}
    if hg is None or len(hg) < 12:
        res['error'] = 'HG=F unavailable'; return res
    n = len(hg)
    res['price'] = hg[-1]
    res['vix']   = round(float(vix[-1]), 1) if vix is not None and len(vix) else None
    e50 = ema(hg, min(50, n))
    h1 = 1 if hg[-2] > e50[-2] else -1
    h2 = (1 if dx[-2]/dx[-7] < 1 else -1) if dx is not None and len(dx) >= 8 else 0
    h3 = (1 if hg[-2] > hg[-7] else -1) if n >= 8 else 0
    h4 = (1 if spy[-2]/spy[-7] > 1 else -1) if spy is not None and len(spy) >= 8 else 0
    h5 = (1 if cl[-2]/cl[-7] > 1 else -1) if cl is not None and len(cl) >= 8 else 0
    raw = h1+h2+h3+h4+h5
    res['raw'] = raw; res['score'] = raw; res['dir'] = 1 if raw >= 0 else -1
    ar = abs(raw)
    res['strength'] = 'STRONG' if ar >= 4 else 'MODERATE' if ar >= 2 else 'WEAK'
    mom5 = round((hg[-2]/hg[-7]-1)*100, 2) if n >= 8 else 0
    dxy_pct = round((dx[-2]/dx[-7]-1)*100, 2) if dx is not None and len(dx) >= 8 else None
    oil_pct = round((cl[-2]/cl[-7]-1)*100, 2) if cl is not None and len(cl) >= 8 else None
    res['components'] = [
        {'name':'HG vs EMA50',   'val':h1, 'note':f'{"ABOVE" if h1>0 else "BELOW"} EMA50 (${round(e50[-2],4)}/lb)'},
        {'name':'DXY 5d dir',    'val':h2, 'note':f'{"falling ↓ (bullish Cu)" if h2>0 else "rising ↑ (bearish Cu)"}  {dxy_pct}% 5d' if dxy_pct is not None else 'n/a'},
        {'name':'Copper 5d mom', 'val':h3, 'note':f'{"+"+str(mom5) if mom5>=0 else mom5}% 5d'},
        {'name':'SPY 5d dir',    'val':h4, 'note':'rising ↑ (risk-on)' if h4>0 else ('falling ↓ (risk-off)' if h4<0 else 'n/a')},
        {'name':'Oil 5d dir',    'val':h5, 'note':f'{"rising ↑ (demand +)" if h5>0 else "falling ↓ (demand −)"}  {oil_pct}% 5d' if oil_pct is not None else 'n/a'},
    ]
    return res

# ══════════════════════════════════════════════════════════════
# TECHNICALS
# ══════════════════════════════════════════════════════════════

def get_au_tech(b15, b1h):
    if b15 is None or len(b15) < 35: return None
    cls = b15['c'].values
    e9 = ema(cls, 9); e21 = ema(cls, 21)
    atr = calc_atr14(b15)
    sv  = sess_vwap(b15, 13, 30)
    # 4H EMA from 1h bars
    h4e9 = h4e21 = h4e50 = None
    if b1h is not None and len(b1h) >= 9:
        b4h = b1h.resample('4h').agg({'h':'max','l':'min','c':'last'}).dropna()
        if len(b4h) >= 9:
            c4h = b4h['c'].values
            h4e9  = float(ema(c4h, 9)[-1])
            h4e21 = float(ema(c4h, min(21, len(c4h)))[-1])
            h4e50 = float(ema(c4h, min(50, len(c4h)))[-1])
    dist = (cls[-1] - sv['vwap']) / sv['std'] if sv['std'] > 0.01 else 99
    return {'price':float(cls[-1]), 'ema9':float(e9[-1]), 'ema21':float(e21[-1]),
            'h4e9':h4e9, 'h4e21':h4e21, 'h4e50':h4e50,
            'atr':atr, 'vwap':sv['vwap'], 'vwap_std':sv['std'], 'vwap_bars':sv['n'], 'vwap_dist':dist}

def get_cl_tech(b15, b1h):
    if b15 is None or len(b15) < 35: return None
    cls = b15['c'].values
    e9 = ema(cls, 9); e21 = ema(cls, 21)
    atr_day = get_daily_atr(b1h)
    atr = atr_day if atr_day else (calc_atr14(b15) or 0) * 6
    sv  = sess_vwap(b15, 14, 0)
    dist = (cls[-1] - sv['vwap']) / sv['std'] if sv['std'] > 0.01 else 99
    return {'price':float(cls[-1]), 'ema9':float(e9[-1]), 'ema21':float(e21[-1]),
            'atr':atr, 'atr_day':atr_day, 'vwap':sv['vwap'], 'vwap_std':sv['std'],
            'vwap_bars':sv['n'], 'vwap_dist':dist}

def get_hg_tech(b15, b1h):
    if b15 is None or len(b15) < 35: return None
    cls = b15['c'].values
    e9 = ema(cls, 9); e21 = ema(cls, 21)
    atr_day = get_daily_atr(b1h)
    atr = atr_day if atr_day else (calc_atr14(b15) or 0) * 6
    sv  = sess_vwap(b15, 13, 0)
    dist = (cls[-1] - sv['vwap']) / sv['std'] if sv['std'] > 0.01 else 99
    return {'price':float(cls[-1]), 'ema9':float(e9[-1]), 'ema21':float(e21[-1]),
            'atr':atr, 'atr_day':atr_day, 'vwap':sv['vwap'], 'vwap_std':sv['std'],
            'vwap_bars':sv['n'], 'vwap_dist':dist}

# ══════════════════════════════════════════════════════════════
# SETUP EVALUATOR
# ══════════════════════════════════════════════════════════════

def eval_setup(macro, tech, ss, now, model):
    mon = now.month - 1  # 0-indexed
    dow = now.weekday()  # 0=Mon, 4=Fri, 6=Sun
    dir_ = macro['dir']
    dir_str = 'LONG' if dir_ == 1 else 'SHORT'

    if tech is None or macro.get('error'):
        return {'action':'WAIT', 'dir':dir_str, 'reason': macro.get('error','No bar data'), 'checks':None}

    price = tech['price']; atr = tech['atr']
    vix_ok   = macro['vix'] is None or macro['vix'] < 30
    dist     = tech['vwap_dist']
    ema_ok   = tech['ema9'] > tech['ema21'] if dir_==1 else tech['ema9'] < tech['ema21']
    vw_ok    = tech['vwap_bars'] >= 4 and ((-1.0 <= dist <= 0.3) if dir_==1 else (-0.3 <= dist <= 1.0))
    sess_ok  = ss == 'ENTRY'

    def vw_note():
        if tech['vwap_bars'] < 4: return f"Only {tech['vwap_bars']} bars (need ≥4)"
        zone = "−1.0 to +0.3σ" if dir_==1 else "−0.3 to +1.0σ"
        lo = tech['vwap'] - tech['vwap_std'] if dir_==1 else tech['vwap'] - 0.3*tech['vwap_std']
        hi = tech['vwap'] + 0.3*tech['vwap_std'] if dir_==1 else tech['vwap'] + tech['vwap_std']
        return f"dist={dist:.2f}σ  need {zone}  zone ${lo:.4f}–${hi:.4f}" if model=='HG' else \
               f"dist={dist:.2f}σ  need {zone}  zone ${lo:.2f}–${hi:.2f}"

    if model == 'AU':
        h4ok = None
        if tech.get('h4e9') and tech.get('h4e21'):
            h4ok = tech['h4e9'] > tech['h4e21'] if dir_==1 else tech['h4e9'] < tech['h4e21']
        # Block short above 4H EMA50
        if dir_==-1 and tech.get('h4e50') and price > tech['h4e50']:
            return {'action':'SKIP','dir':'SHORT','reason':f"SHORT blocked — above 4H EMA50 (${tech['h4e50']:.2f})"}
        mon_ok = mon not in AU_SKIP_MONTHS
        checks = [
            {'label':'VIX < 30',         'ok':vix_ok,  'note':f"VIX {macro['vix']} {'OK' if vix_ok else '≥30 skip'}"},
            {'label':'Not June',          'ok':mon_ok,  'note':f"{MNAMES[mon]} {'OK' if mon_ok else '— SKIP MONTH'}"},
            {'label':'Session (entry)',   'ok':sess_ok, 'note':'18:00–20:30 Dubai open' if sess_ok else 'Wait — opens 18:00 Dubai'},
            {'label':'4H EMA aligned',   'ok':h4ok is not False,
             'note':('No 4H data' if h4ok is None else (f"EMA9 ${tech['h4e9']:.2f} aligned" if h4ok else 'Misaligned'))},
            {'label':'15m EMA aligned',  'ok':ema_ok,  'note':f"EMA9 ${tech['ema9']:.2f} / EMA21 ${tech['ema21']:.2f}"},
            {'label':'VWAP pullback',     'ok':vw_ok,   'note':f"dist={dist:.2f}σ ✓  VWAP=${tech['vwap']:.2f}" if vw_ok else vw_note()},
        ]
        all_ok = vix_ok and mon_ok and sess_ok and (h4ok is not False) and ema_ok and vw_ok

    elif model == 'CL':
        mon_ok   = mon not in CL_SKIP_MONTHS
        wed_ok   = not CL_SKIP_WED or dow != 2
        score_ok = abs(macro['raw']) >= CL_SCORE_MIN
        checks = [
            {'label':'VIX < 30',              'ok':vix_ok,   'note':f"VIX {macro['vix']} {'OK' if vix_ok else '≥30 skip'}"},
            {'label':'Not skip-month',        'ok':mon_ok,   'note':f"{MNAMES[mon]} {'OK' if mon_ok else '— SKIP MONTH'}"},
            {'label':'Not Wednesday (EIA)',   'ok':wed_ok,   'note':'Not Wed — OK' if wed_ok else 'WEDNESDAY — EIA skip'},
            {'label':f'Score |raw| ≥ {CL_SCORE_MIN}','ok':score_ok,'note':f"Raw: {macro['raw']:+d}/7  {'✓' if score_ok else '✗'}"},
            {'label':'Session (entry)',       'ok':sess_ok,  'note':'18:30–22:30 Dubai open' if sess_ok else 'Wait — opens 18:30 Dubai'},
            {'label':'15m EMA aligned',       'ok':ema_ok,   'note':f"EMA9 ${tech['ema9']:.2f} / EMA21 ${tech['ema21']:.2f}"},
            {'label':'VWAP pullback',         'ok':vw_ok,    'note':f"dist={dist:.2f}σ ✓  VWAP=${tech['vwap']:.2f}" if vw_ok else vw_note()},
        ]
        all_ok = vix_ok and mon_ok and wed_ok and score_ok and sess_ok and ema_ok and vw_ok

    else:  # HG
        mon_ok   = mon not in HG_SKIP_MONTHS
        fri_ok   = not HG_SKIP_FRI or dow != 4
        score_ok = abs(macro['raw']) >= HG_SCORE_MIN
        checks = [
            {'label':'VIX < 30',              'ok':vix_ok,   'note':f"VIX {macro['vix']} {'OK' if vix_ok else '≥30 skip'}"},
            {'label':'Not skip-month',        'ok':mon_ok,   'note':f"{MNAMES[mon]} {'OK' if mon_ok else '— SKIP MONTH'}"},
            {'label':'Not Friday',            'ok':fri_ok,   'note':'Not Fri — OK' if fri_ok else 'FRIDAY — gap risk skip'},
            {'label':f'Score |raw| ≥ {HG_SCORE_MIN}','ok':score_ok,'note':f"Raw: {macro['raw']:+d}/5  {'✓' if score_ok else '✗'}"},
            {'label':'Session (entry)',       'ok':sess_ok,  'note':'17:30–21:00 Dubai open' if sess_ok else 'Wait — opens 17:30 Dubai'},
            {'label':'15m EMA aligned',       'ok':ema_ok,   'note':f"EMA9 ${tech['ema9']:.4f} / EMA21 ${tech['ema21']:.4f}"},
            {'label':'VWAP pullback',         'ok':vw_ok,    'note':f"dist={dist:.2f}σ ✓  VWAP=${tech['vwap']:.4f}" if vw_ok else vw_note()},
        ]
        all_ok = vix_ok and mon_ok and fri_ok and score_ok and sess_ok and ema_ok and vw_ok

    if all_ok:
        sl  = price - SL_MULT*atr  if dir_==1 else price + SL_MULT*atr
        tp1 = price + TP1_MULT*atr if dir_==1 else price - TP1_MULT*atr
        tp2 = price + TP2_MULT*atr if dir_==1 else price - TP2_MULT*atr
        sz  = (ACCOUNT * RISK_PCT) / (SL_MULT * atr)
        return {'action':'ACTIVE','dir':dir_str,'entry':price,'sl':sl,'tp1':tp1,'tp2':tp2,
                'size':round(sz,2),'risk_$':round(ACCOUNT*RISK_PCT),'atr':atr,'checks':checks}
    return {'action':'WAIT','dir':dir_str,'checks':checks}

# ══════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════

def print_header(now_utc, now_dxb, mon, dow):
    print()
    print(col("═"*78, DIM))
    print(col(f"  SIGNAL COMMAND CENTER  ·  Gold · Oil · Copper", W+BOLD))
    print(col(f"  {now_utc.strftime('%Y-%m-%d %H:%M')} UTC  /  {now_dxb.strftime('%H:%M')} Dubai  ·  {DOW[dow]} {MNAMES[mon]}", DIM))
    print(col(f"  Account ${ACCOUNT}  ·  Risk {int(RISK_PCT*100)}%/trade (${int(ACCOUNT*RISK_PCT)})  ·  SL=0.75×ATR  TP1=1.5×ATR  TP2=2.5×ATR", DIM))
    print(col("═"*78, DIM))

def print_snapshot(auM, clM, hgM, auSS, clSS, hgSS, auSU, clSU, hgSU, now_utc):
    # Quick snapshot table
    print()
    print(col("  ┌─────────────────────────────────┬─────────────────────────────────┬─────────────────────────────────┐", DIM))
    print(col("  │", DIM) + col(f"  ⚡  GOLD  (GC=F)".center(33), GOLD_C+BOLD) +
          col("│", DIM) + col(f"  ⚡  OIL  (CL=F)".center(33), OIL_C+BOLD) +
          col("│", DIM) + col(f"  ⚡  COPPER (HG=F)".center(33), CU_C+BOLD) + col("│", DIM))
    print(col("  ├─────────────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤", DIM))

    def pricecol(m, prec=2):
        p = m.get('price')
        return f"${p:.{prec}f}" if p else "—"

    print(col("  │", DIM) +
          col(f"  {pricecol(auM)}".ljust(33), GOLD_C+BOLD) +
          col("│", DIM) +
          col(f"  {pricecol(clM)} /bbl".ljust(33), OIL_C+BOLD) +
          col("│", DIM) +
          col(f"  {pricecol(hgM,4)} /lb".ljust(33), CU_C+BOLD) +
          col("│", DIM))

    def dir_str(m, maxR):
        d = '▲ LONG' if m['dir']==1 else '▼ SHORT'
        if maxR == 5:
            return f"{d}  {m['strength']}  {m['score']:+.1f}% ({m['raw']:+d}/5)"
        return f"{d}  {m['strength']}  {m['raw']:+d}/{maxR}"

    def dir_col(m): return G if m['dir']==1 else R

    au_ds = dir_str(auM, 5); cl_ds = dir_str(clM, 7); hg_ds = dir_str(hgM, 5)
    print(col("  │", DIM) +
          col(f"  {au_ds}".ljust(33), dir_col(auM)) +
          col("│", DIM) +
          col(f"  {cl_ds}".ljust(33), dir_col(clM)) +
          col("│", DIM) +
          col(f"  {hg_ds}".ljust(33), dir_col(hgM)) +
          col("│", DIM))

    SS_LABEL = {'BEFORE':'⏳ Pre-session','CHOP':'⚠ Chop zone','ENTRY':'✅ ENTRY OPEN','LATE':'🕐 Late','CLOSED':'🔒 Closed'}
    SS_COL   = {'BEFORE':DIM,'CHOP':Y,'ENTRY':G+BOLD,'LATE':Y,'CLOSED':DIM}
    print(col("  │", DIM) +
          col(f"  {SS_LABEL.get(auSS,auSS)}".ljust(33), SS_COL.get(auSS,DIM)) +
          col("│", DIM) +
          col(f"  {SS_LABEL.get(clSS,clSS)}".ljust(33), SS_COL.get(clSS,DIM)) +
          col("│", DIM) +
          col(f"  {SS_LABEL.get(hgSS,hgSS)}".ljust(33), SS_COL.get(hgSS,DIM)) +
          col("│", DIM))

    def sig_label(su):
        if su['action']=='ACTIVE': return f"⚡ ENTER {su['dir']}"
        if su['action']=='SKIP':   return "🚫 BLOCKED"
        if not su.get('checks'):   return "⚠ NO DATA"
        fail = sum(1 for c in su['checks'] if not c['ok'])
        return f"{su['dir']}  {fail} gate{'s' if fail!=1 else ''} pending"
    def sig_col(su): return G+BOLD if su['action']=='ACTIVE' else (Y if su['action']=='SKIP' else DIM)

    print(col("  │", DIM) +
          col(f"  {sig_label(auSU)}".ljust(33), sig_col(auSU)) +
          col("│", DIM) +
          col(f"  {sig_label(clSU)}".ljust(33), sig_col(clSU)) +
          col("│", DIM) +
          col(f"  {sig_label(hgSU)}".ljust(33), sig_col(hgSU)) +
          col("│", DIM))

    vix = auM.get('vix') or clM.get('vix') or hgM.get('vix')
    vix_s = f"VIX: {vix}" if vix else "VIX: n/a"
    vix_c = R if vix and vix >= 30 else (Y if vix and vix >= 25 else G)
    print(col("  └─────────────────────────────────┴─────────────────────────────────┴─────────────────────────────────┘", DIM))
    print(col(f"  {vix_s}", vix_c))

def print_model(name, sym, color, macro, tech, ss, setup, now_utc):
    print()
    print(col(f"{'━'*78}", color))
    print(col(f"  {name}  ({sym})", color+BOLD))
    print(col(f"{'━'*78}", color))

    # Price + status
    price = tech['price'] if tech else macro.get('price', 0)
    prec = 4 if sym == 'HG=F' else 2
    print(f"  Price: {col(f'${price:.{prec}f}', color+BOLD)}   "
          f"Direction: {col(('▲ LONG' if macro['dir']==1 else '▼ SHORT')+' '+macro['strength'], G if macro['dir']==1 else R)}")

    # Score
    if sym == 'GC=F':
        sc = f"{macro['score']:+.1f}% (raw {macro['raw']:+d}/5) — any majority fires direction"
    else:
        maxR = 7 if sym=='CL=F' else 5
        ok = abs(macro['raw']) >= (CL_SCORE_MIN if sym=='CL=F' else HG_SCORE_MIN)
        sc = f"{macro['raw']:+d}/{maxR}  {col('✓ threshold met', G) if ok else col('✗ below threshold', R)}"
    print(f"  Score: {sc}")

    # VWAP σ ladder
    if tech and tech['vwap'] > 0:
        vw = tech['vwap']; sd = tech['vwap_std']; dist = tech['vwap_dist']
        dir_ = macro['dir']
        print(f"\n  {col('VWAP  +  STANDARD DEVIATION LADDER', C)}")
        levels = [
            ('+2σ',  vw+2*sd,  2.0,  'Extreme extension'),
            ('+1σ',  vw+sd,    1.0,  'SHORT entry zone top' if dir_==1 else 'SHORT entry zone top'),
            ('+0.3σ',vw+.3*sd, 0.3,  'LONG zone far edge' if dir_==1 else 'SHORT zone near edge'),
            ('VWAP', vw,       0.0,  'Session anchor'),
            ('−0.3σ',vw-.3*sd,-0.3,  'SHORT zone near edge' if dir_==1 else 'LONG zone far edge'),
            ('−1σ',  vw-sd,   -1.0,  'LONG entry zone bottom'),
            ('−2σ',  vw-2*sd, -2.0,  'Extreme extension'),
        ]
        for lbl, lp, ls, desc in levels:
            is_long_zone  = dir_== 1 and -1.0 <= ls <= 0.3
            is_short_zone = dir_==-1 and -0.3 <= ls <= 1.0
            is_entry = is_long_zone or is_short_zone
            is_cur   = abs(dist - ls) < 0.15
            lp_str = f"${lp:.{prec}f}"
            zone_c = (G if is_entry else DIM) if lbl != 'VWAP' else C
            cur_mark = col(f" ← PRICE HERE ({dist:.2f}σ)", Y+BOLD) if is_cur else ''
            entry_mark = col(' [ENTRY ZONE]', G) if is_entry else ''
            print(f"    {col(lbl.rjust(5), zone_c+BOLD)}  {col(lp_str.rjust(10 if prec==2 else 12), W)}"
                  f"  {col(f'{ls:+.1f}σ', zone_c)}  {col(desc, DIM)}{entry_mark}{cur_mark}")
        in_zone = (dir_== 1 and -1.0 <= dist <= 0.3) or (dir_==-1 and -0.3 <= dist <= 1.0)
        zone_str = col(f"dist={dist:+.2f}σ  ✓ IN ENTRY ZONE", G+BOLD) if in_zone else col(f"dist={dist:+.2f}σ  ✗ OUT OF ZONE", R)
        print(f"  → {zone_str}  |  1σ = ${sd:.{prec}f}  |  VWAP = ${vw:.{prec}f}")
    else:
        print(col("  VWAP: not yet built (pre-session or fetch failed)", DIM))

    # Macro components
    print(f"\n  {col('COMPONENTS', C)}")
    for c in macro.get('components', []):
        arrow = col('▲', G) if c['val'] > 0 else col('▼', R)
        score_c = col('+1', G) if c['val'] > 0 else col('−1', R)
        print(f"    {arrow} {c['name'].ljust(20)} {score_c}  {col(c['note'], DIM)}")

    # Gate checklist
    print(f"\n  {col('GATE CHECKLIST', C)}")
    if setup.get('checks'):
        sorted_checks = sorted(setup['checks'], key=lambda c: 1 if c['ok'] else 0)
        for c in sorted_checks:
            mark = col('✓', G) if c['ok'] else col('✗', R)
            label_c = col(c['label'].ljust(28), G if c['ok'] else R)
            print(f"    {mark} {label_c}  {col(c['note'], DIM)}")
    elif setup.get('reason'):
        print(col(f"    ⚠  {setup['reason']}", Y))

    # Trade plan if active
    if setup['action'] == 'ACTIVE':
        print()
        print(col(f"  ⚡  ALL GATES GREEN  —  ENTER {setup['dir']}  NOW", G+BOLD))
        print(col(f"  {'─'*60}", DIM))
        prec2 = 4 if sym=='HG=F' else 2
        e_str  = f"${setup['entry']:.{prec2}f}"
        sl_str = f"${setup['sl']:.{prec2}f}"
        tp1_str= f"${setup['tp1']:.{prec2}f}"
        tp2_str= f"${setup['tp2']:.{prec2}f}"
        atr_sl = f"${setup['atr']*SL_MULT:.{prec2}f}"
        atr_t1 = f"${setup['atr']*TP1_MULT:.{prec2}f}"
        atr_t2 = f"${setup['atr']*TP2_MULT:.{prec2}f}"
        print(f"    Entry:     {col(e_str, color+BOLD)}")
        print(f"    Stop Loss: {col(sl_str, R+BOLD)}   ({SL_MULT}×ATR = {atr_sl})")
        print(f"    TP1 (60%): {col(tp1_str, G+BOLD)}   ({TP1_MULT}×ATR = +{atr_t1})")
        print(f"    TP2 (40%): {col(tp2_str, C+BOLD)}   ({TP2_MULT}×ATR = +{atr_t2})")
        unit = 'oz' if sym=='GC=F' else ('bbl' if sym=='CL=F' else 'lbs')
        print(f"    Size:      {col(str(setup['size'])+' '+unit, W+BOLD)}   (risk ${setup['risk_$']})")
        print(f"    ATR:       ${setup['atr']:.{prec2}f}")
    elif setup['action'] == 'SKIP':
        print()
        print(col(f"  🚫  BLOCKED  —  {setup.get('reason','')}", Y+BOLD))

    # Key levels
    if tech:
        print(f"\n  {col('KEY LEVELS', C)}")
        prec2 = 4 if sym=='HG=F' else 2
        if sym == 'GC=F' and tech.get('h4e50'):
            print(f"    {'4H EMA50'.ljust(22)} {col('$'+str(round(tech['h4e50'],2)), W)}  {col('short gate (blocked above)', DIM)}")
            print(f"    {'4H EMA9'.ljust(22)} {col('$'+str(round(tech['h4e9'],2)), W)}")
            print(f"    {'4H EMA21'.ljust(22)} {col('$'+str(round(tech['h4e21'],2)), W)}")
        e9_s  = f"${tech['ema9']:.{prec2}f}"
        e21_s = f"${tech['ema21']:.{prec2}f}"
        print(f"    {'15m EMA9'.ljust(22)} {col(e9_s, W)}")
        print(f"    {'15m EMA21'.ljust(22)} {col(e21_s, W)}")
        if tech['vwap'] > 0:
            dir_ = macro['dir']
            lo = tech['vwap'] - tech['vwap_std'] if dir_==1 else tech['vwap'] - 0.3*tech['vwap_std']
            hi = tech['vwap'] + 0.3*tech['vwap_std'] if dir_==1 else tech['vwap'] + tech['vwap_std']
            vw_s  = f"${tech['vwap']:.{prec2}f}"
            ez_s  = f"${lo:.{prec2}f} – ${hi:.{prec2}f}"
            print(f"    {'VWAP'.ljust(22)} {col(vw_s, C)}  ({tech['vwap_bars']} bars)")
            print(f"    {'Entry zone'.ljust(22)} {col(ez_s, G)}")
        if tech.get('atr_day'):
            atr_s = f"${tech['atr_day']:.{prec2}f}"
            print(f"    {'Daily ATR'.ljust(22)} {col(atr_s, W)}")

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    now_utc = datetime.now(timezone.utc)
    now_dxb = datetime.now(timezone(timedelta(hours=4)))
    nowH = now_utc.hour + now_utc.minute / 60
    mon  = now_utc.month - 1   # 0-indexed
    dow  = now_utc.weekday()   # 0=Mon

    print(f"\n{col('Fetching daily data...', DIM)}", end='', flush=True)
    SYMS = ['GC=F','DX-Y.NYB','^TNX','GDX','CL=F','HG=F','SPY','RB=F','BZ=F','USO','USL','^VIX']
    daily = fetch_daily(SYMS)
    print(col(f" done ({len(daily)}/{len(SYMS)} symbols)", DIM))

    print(col('Fetching intraday bars (6 requests)...', DIM), end='', flush=True)
    au15 = fetch_bars('GC=F',  '15m', '5d')
    au1h = fetch_bars('GC=F',  '1h',  '60d')
    cl15 = fetch_bars('CL=F',  '15m', '5d')
    cl1h = fetch_bars('CL=F',  '1h',  '60d')
    hg15 = fetch_bars('HG=F',  '15m', '5d')
    hg1h = fetch_bars('HG=F',  '1h',  '60d')
    print(col(' done', DIM))

    # Macro scores
    auM = get_au_macro(daily)
    clM = get_cl_macro(daily)
    hgM = get_hg_macro(daily)

    # Technicals
    auT = get_au_tech(au15, au1h)
    clT = get_cl_tech(cl15, cl1h)
    hgT = get_hg_tech(hg15, hg1h)

    # Session states
    auSS = sess_state(nowH, AU_SESS_OPEN, AU_ENTRY_START, AU_ENTRY_END, AU_SESS_CLOSE)
    clSS = sess_state(nowH, CL_SESS_OPEN, CL_ENTRY_START, CL_ENTRY_END, CL_SESS_CLOSE)
    hgSS = sess_state(nowH, HG_SESS_OPEN, HG_ENTRY_START, HG_ENTRY_END, HG_SESS_CLOSE)

    # Setups
    auSU = eval_setup(auM, auT, auSS, now_utc, 'AU')
    clSU = eval_setup(clM, clT, clSS, now_utc, 'CL')
    hgSU = eval_setup(hgM, hgT, hgSS, now_utc, 'HG')

    # Print
    print_header(now_utc, now_dxb, mon, dow)
    print_snapshot(auM, clM, hgM, auSS, clSS, hgSS, auSU, clSU, hgSU, now_utc)
    print_model('GOLD',   'GC=F',  GOLD_C, auM, auT, auSS, auSU, now_utc)
    print_model('OIL',    'CL=F',  OIL_C,  clM, clT, clSS, clSU, now_utc)
    print_model('COPPER', 'HG=F',  CU_C,   hgM, hgT, hgSS, hgSU, now_utc)
    print()
    print(col("═"*78, DIM))
    print(col("  SCORE LOG (latest reading)", W+BOLD))
    print(col("═"*78, DIM))
    vix = auM.get('vix') or clM.get('vix') or hgM.get('vix')
    print(f"  {now_utc.strftime('%Y-%m-%d %H:%M')} UTC  /  {now_dxb.strftime('%H:%M')} Dubai  |  VIX: {col(str(vix) if vix else 'n/a', R if vix and vix>=30 else (Y if vix and vix>=25 else G))}")
    au_sc = f"{auM['score']:+.1f}% ({auM['raw']:+d}/5)"
    cl_sc = f"{clM['raw']:+d}/7"
    hg_sc = f"{hgM['raw']:+d}/5"
    au_d  = '▲ LONG' if auM['dir']==1 else '▼ SHORT'
    cl_d  = '▲ LONG' if clM['dir']==1 else '▼ SHORT'
    hg_d  = '▲ LONG' if hgM['dir']==1 else '▼ SHORT'
    print(f"  Gold:   {col(au_sc, GOLD_C)}  {au_d}  {auM['strength']}  {auSS}")
    print(f"  Oil:    {col(cl_sc, OIL_C)}  {cl_d}  {clM['strength']}  {clSS}")
    print(f"  Copper: {col(hg_sc, CU_C)}  {hg_d}  {hgM['strength']}  {hgSS}")
    print(col("═"*78, DIM))
    print()

if __name__ == '__main__':
    main()
