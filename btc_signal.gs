/**
 * BTC SIGNAL DASHBOARD  V2 — Google Apps Script
 * ================================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste → Save
 *   2. Run setupTrigger() once (authorize)
 *   3. Run updateDashboard() to load immediately
 *   Auto-refreshes every 5 minutes.
 *
 * Session: 17:30–21:00 Dubai  |  Entry window: 18:00–20:30 Dubai
 *          (13:30–17:00 UTC)                 (14:00–16:30 UTC)
 *
 * Backtest (37 months Apr 2023–May 2026):
 *   p=0.006 ✓  ·  Sharpe 2.83  ·  WR 47.9%  ·  MDD −17.5%  ·  +269%  ·  242 trades
 *
 * V2: Step-by-step Bybit execution instructions + VWAP entry zone display.
 *
 * FILTER VALIDATION (A/B backtest — btc_v13_filter_test.py):
 *   Binance blocked (451) → tested on Twelve Data (10 months). Baseline system
 *   was net-negative in that period — no filter reached a positive Sharpe.
 *   MACD histogram  — SKIP  (ΔSharpe −9.01, hurts badly)
 *   RSI < 55 / > 45 — inconclusive (ΔSharpe +2.66 but system overall negative)
 *   Volume ≥ 120%   — SKIP  (0 trades — Twelve Data vol is constant, synthetic)
 *   OBV > EMA10     — SKIP  (ΔSharpe −1.02)
 *   All 4 V13 filters shown as INFORMATIONAL CONTEXT only — not entry gates.
 *   Validated gates (7): ETF gate · vol% gate · 4H/1H/15m EMA · VWAP · session
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var TWELVE_KEY   = "06f050cd7d9940a895f9461e7f0ff7e3";
var ACCOUNT      = 8000;
var RISK_PCT     = 0.02;
var SL_MULT      = 0.75;
var TP1_MULT     = 1.5;
var TP2_MULT     = 2.5;
var TP1_FRAC     = 0.60;
var TP2_FRAC     = 0.40;
var TIME_STOP    = 6;          // bars (90 min) before time exit

var SCORE_LONG   = 6.0;        // need score ≥+6 for LONG bias (4/5 factors)
var SCORE_SHORT  = -6.0;       // need score ≤−6 for SHORT bias
var ATR_PCT_MIN  = 0.0010;     // 0.10% min — exclude flat markets
var ATR_PCT_MAX  = 0.0150;     // 1.50% max — exclude flash crashes

var SESS_OPEN    = 13.50;      // 13:30 UTC = 17:30 Dubai
var SESS_CLOSE   = 17.00;      // 17:00 UTC = 21:00 Dubai
var ENTRY_START  = 14.00;      // 14:00 UTC = 18:00 Dubai
var ENTRY_END    = 16.50;      // 16:30 UTC = 20:30 Dubai

var ETF_LAUNCH_MS = new Date('2024-01-11').getTime();

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ BTC SIGNAL V2 — Auto-refresh every 5 minutes.\n\n' +
      'Run updateDashboard() to populate now.\n\n' +
      'Dubai check times:\n' +
      '  17:15 — Pre-session: review macro score + ETF flows\n' +
      '  18:00 — Entry window opens: watch for VWAP pullback\n' +
      '  20:30 — Entry window closes: manage trades only\n' +
      '  21:00 — Session ends: close ALL trades (no overnight)'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('BTC SIGNAL') || ss.insertSheet('BTC SIGNAL');

  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  var macro     = getMacroScore();
  var bars15m   = get15mBars(80);    // 35+ needed for MACD warmup
  var bars1h    = get1hBars(280);
  var etf       = getEtfFlow5d();
  var sessState = getSessionState(nowH);

  var tech  = computeTechnicals(bars15m, bars1h);
  var setup = computeSetup(macro, tech, sessState, etf);

  writeSheet(dash, now, nowH, macro, tech, sessState, etf, setup);
}

// ── MACRO SCORE (Yahoo Finance, cached 6h) ────────────────────────────────────
function getMacroScore() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key    = 'btc_macro_v2_' + today;
  var cached = cache.get(key);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yf(sym, range) {
    try {
      var url = 'https://query1.finance.yahoo.com/v8/finance/chart/' +
                encodeURIComponent(sym) + '?interval=1d&range=' + range;
      var r = UrlFetchApp.fetch(url, { muteHttpExceptions:true, headers:{'User-Agent':'Mozilla/5.0'} });
      var j = JSON.parse(r.getContentText());
      return j.chart.result[0].indicators.quote[0].close.filter(function(c){ return c!=null; });
    } catch(e) { return null; }
  }

  var btc = yf('BTC-USD',  '2y');
  var qqq = yf('QQQ',      '1y');
  var dxy = yf('DX-Y.NYB', '6mo');

  var result = { score:0, dir:0, components:[], error:null, btcPriceD:null, btcEma200:null };
  if (!btc || btc.length < 55) { result.error = 'BTC macro data unavailable'; return result; }

  var n      = btc.length;
  result.btcPriceD = btc[n-1];
  var e50arr = emaArr(btc, 50);
  var e200   = btc.length >= 200 ? emaArr(btc, 200) : null;
  if (e200) result.btcEma200 = e200[n-2];

  var s1 = btc[n-2] > e50arr[n-2] ? 1 : -1;
  var s2 = 0;
  if (qqq && qqq.length >= 52) {
    var qe50 = emaArr(qqq, 50);
    s2 = qqq[qqq.length-2] > qe50[qe50.length-2] ? 1 : -1;
  }
  var s3 = 0;
  if (dxy && dxy.length >= 22) {
    var de20 = emaArr(dxy, 20);
    s3 = dxy[dxy.length-2] < de20[de20.length-2] ? 1 : -1;
  }
  var s4 = n >= 12 ? (btc[n-2] > btc[n-12] ? 1 : -1) : 0;
  var s5 = (e200 && btc[n-2] > e200[n-2]) ? 1 : -1;

  var raw   = s1+s2+s3+s4+s5;
  var score = Math.round(raw/5*100)/10;
  result.score = score;
  result.dir   = score >= SCORE_LONG ? 1 : score <= SCORE_SHORT ? -1 : 0;

  var mom10pct = n>=12?Math.round((btc[n-2]/btc[n-12]-1)*1000)/10:0;
  result.components = [
    { name:'BTC vs EMA50',     val:s1, note:'BTC '+(s1>0?'above':'below')+' daily EMA50 ('+Math.round(e50arr[n-2])+')' },
    { name:'QQQ risk-on',      val:s2, note:qqq&&qqq.length>=52?'QQQ '+(s2>0?'above':'below')+' daily EMA50':'QQQ unavailable' },
    { name:'DXY inverse',      val:s3, note:dxy&&dxy.length>=22?'DXY '+(s3>0?'below EMA20 ↓ (BTC bullish)':'above EMA20 ↑ (BTC bearish)'):'DXY unavailable' },
    { name:'BTC 10d momentum', val:s4, note:(mom10pct>=0?'+':'')+mom10pct+'% over 10 days' },
    { name:'BTC vs EMA200',    val:s5, note:'Regime: BTC '+(s5>0?'above':'below')+' EMA200'+(result.btcEma200?' ('+Math.round(result.btcEma200)+')':'') }
  ];

  cache.put(key, JSON.stringify(result), 21600);
  return result;
}

// ── ETF FLOWS (Farside, cached daily) ─────────────────────────────────────────
function getEtfFlow5d() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key    = 'etf_flow_5d_' + today;
  var cached = cache.get(key);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  try {
    var resp = UrlFetchApp.fetch('https://farside.co.uk/bitcoin-etf-flow/', {
      muteHttpExceptions:true, followRedirects:true,
      headers:{'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36','Accept':'text/html'}
    });
    if (resp.getResponseCode() !== 200) return { sum5d:null, days:0, error:'HTTP '+resp.getResponseCode() };

    var html = resp.getContentText(), totals = [];
    var rowRe = /<tr[^>]*>([\s\S]*?)<\/tr>/gi, rowMatch;
    while ((rowMatch = rowRe.exec(html)) !== null) {
      var rowHtml = rowMatch[1], cells = [], cm;
      var cellRe = /<td[^>]*>([\s\S]*?)<\/td>/gi;
      while ((cm = cellRe.exec(rowHtml)) !== null)
        cells.push(cm[1].replace(/<[^>]+>/g,'').replace(/&nbsp;/g,'').trim());
      if (cells.length < 5) continue;
      var v = parseFlowVal(cells[cells.length-1]);
      if (!isNaN(v)) totals.push(v);
    }
    if (!totals.length) return { sum5d:null, days:0, error:'Could not parse ETF table' };

    var n5=Math.min(5,totals.length), last5=totals.slice(-n5);
    var result = {
      sum5d:Math.round(last5.reduce(function(a,b){return a+b;},0)*10)/10,
      days:n5, latest:Math.round(totals[totals.length-1]*10)/10, error:null
    };
    cache.put(key, JSON.stringify(result), 86400);
    return result;
  } catch(e) { return { sum5d:null, days:0, error:'Fetch error: '+e.message }; }
}

function parseFlowVal(s) {
  s = s.replace(/,/g,'').trim();
  if (!s||s==='-'||s.toLowerCase()==='n/a'||s==='') return NaN;
  if (s.charAt(0)==='('&&s.charAt(s.length-1)===')') return -parseFloat(s.slice(1,-1));
  return parseFloat(s);
}

// ── BAR FETCH (Twelve Data — includes volume) ─────────────────────────────────
function tdFetch(interval, outputsize) {
  try {
    var url = 'https://api.twelvedata.com/time_series?symbol=BTC/USD' +
              '&interval='+interval+'&outputsize='+outputsize+
              '&apikey='+TWELVE_KEY+'&timezone=UTC';
    var r = UrlFetchApp.fetch(url, {muteHttpExceptions:true});
    var j = JSON.parse(r.getContentText());
    if (!j.values || j.status==='error') return [];
    return j.values.map(function(v) {
      return { dt:v.datetime, o:+v.open, h:+v.high, l:+v.low, c:+v.close, v:+v.volume||0 };
    }).reverse();
  } catch(e) { return []; }
}
function get15mBars(n) { return tdFetch('15min', n); }
function get1hBars(n)  { return tdFetch('1h',    n); }

// ── TECHNICALS ────────────────────────────────────────────────────────────────
function computeTechnicals(bars15m, bars1h) {
  if (!bars15m || bars15m.length < 35) return null;
  if (!bars1h  || bars1h.length  < 25) return null;

  var closes = bars15m.map(function(b){ return b.c; });
  var vols   = bars15m.map(function(b){ return b.v || 0; });
  var e9     = emaArr(closes, 9);
  var e21    = emaArr(closes, 21);
  var atrV   = calcAtr14(bars15m);
  var last   = bars15m[bars15m.length-1];
  var atrPct = last.c > 0 ? atrV / last.c : 0;

  // V13 filter values
  var macdHist = calcMacdHistogram(closes);
  var rsi15m   = calcRsi14(closes);

  var volN = Math.min(20, vols.length-1), volSum=0;
  for (var vi=vols.length-1-volN; vi<vols.length-1; vi++) volSum+=vols[vi];
  var avgVol   = volN>0 ? volSum/volN : 0;
  var volRatio = avgVol>0 ? vols[vols.length-1]/avgVol : null;
  var obvAbove = calcObvAboveEma(closes, vols);

  // 1H EMA
  var c1h = bars1h.map(function(b){ return b.c; });
  var h1e9  = emaArr(c1h, 9), h1e21 = emaArr(c1h, 21);

  // 4H EMA
  var bars4h = resampleTo4H(bars1h);
  var c4h    = bars4h.map(function(b){ return b.c; });
  var h4e9   = emaArr(c4h, 9), h4e21 = emaArr(c4h, 21);
  var li4h   = c4h.length-1, li1h = c1h.length-1;

  var sv = sessionVwap(bars15m);

  return {
    price: last.c, high:last.h, low:last.l,
    ema9: e9[e9.length-1], ema21: e21[e21.length-1],
    atr: atrV, atrPct: atrPct,
    h1ema9: h1e9[li1h], h1ema21: h1e21[li1h],
    h4ema9: h4e9[li4h], h4ema21: h4e21[li4h],
    vwap: sv.vwap, vwapStd: sv.std, vwapBars: sv.n,
    vwapDist: sv.std>1?((last.c-sv.vwap)/sv.std):99,
    // V13 filter values
    macdHist: macdHist, rsi15m: rsi15m,
    volRatio: volRatio, avgVol: avgVol, lastVol: vols[vols.length-1],
    obvAbove: obvAbove
  };
}

// ── SESSION VWAP ──────────────────────────────────────────────────────────────
function sessionVwap(bars15m) {
  var now  = new Date();
  var ymd  = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sOpen = new Date(ymd+'T13:30:00Z');
  var sess = bars15m.filter(function(b){ return new Date(b.dt.replace(' ','T')+'Z')>=sOpen; });
  if (sess.length<2) return {vwap:0,std:1,n:0};
  var tp  = sess.map(function(b){ return (b.h+b.l+b.c)/3; });
  var avg = tp.reduce(function(a,v){return a+v;},0)/tp.length;
  var v2  = tp.reduce(function(a,x){return a+Math.pow(x-avg,2);},0)/tp.length;
  return {vwap:avg, std:Math.max(Math.sqrt(v2),1), n:tp.length};
}

// ── SESSION STATE ─────────────────────────────────────────────────────────────
function getSessionState(nowH) {
  if (nowH<SESS_OPEN)   return 'BEFORE';
  if (nowH<ENTRY_START) return 'CHOP';
  if (nowH<=ENTRY_END)  return 'ENTRY';
  if (nowH<=SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}

// ── ETF GATE ──────────────────────────────────────────────────────────────────
function etfGateAllows(dir, etf) {
  if (!etf||etf.error||etf.sum5d===null) return true;
  if (new Date().getTime() < ETF_LAUNCH_MS) return true;
  if (dir===1  && etf.sum5d<0) return false;
  if (dir===-1 && etf.sum5d>0) return false;
  return true;
}
function etfNote(etf) {
  if (!etf||etf.error) return 'ETF data unavailable — gate inactive';
  if (etf.sum5d===null) return 'ETF data not parsed';
  return '5d net '+(etf.sum5d>=0?'+':'')+Math.round(etf.sum5d)+'M ('+etf.days+'d)';
}

// ── SETUP LOGIC ───────────────────────────────────────────────────────────────
function computeSetup(macro, tech, sessState, etf) {
  if (!tech||macro.error) return {action:'WAIT', reason:macro.error||'No data'};

  var dir   = macro.dir;
  var score = macro.score;

  if (dir===0) return {
    action:'SKIP',
    reason:'Score '+score.toFixed(1)+' too neutral — need ≥+'+SCORE_LONG+' for LONG or ≤'+SCORE_SHORT+' for SHORT'
  };

  var price  = tech.price;
  var atrV   = tech.atr;
  var atrPct = tech.atrPct;
  var dist   = tech.vwapDist;

  var etfOk  = etfGateAllows(dir, etf);
  var volGtOk = atrPct>=ATR_PCT_MIN && atrPct<=ATR_PCT_MAX;
  var h4ok   = dir===1?tech.h4ema9>tech.h4ema21:tech.h4ema9<tech.h4ema21;
  var h1ok   = dir===1?tech.h1ema9>tech.h1ema21:tech.h1ema9<tech.h1ema21;
  var emaOk  = dir===1?tech.ema9>tech.ema21:tech.ema9<tech.ema21;
  var vwOk   = tech.vwapBars>=4 &&
    (dir===1?(dist>=-1.0&&dist<=0.3):(dist>=-0.3&&dist<=1.0));
  var sessOk = sessState==='ENTRY';

  var atrPctStr = Math.round(atrPct*10000)/100+'%';

  // ── Validated entry gates (7 required) ───────────────────────────────────
  var checks = [
    { label:'ETF flow gate',               ok:etfOk,
      note: etfNote(etf)+(etfOk?' ✓':' ✗ — '+etf.sum5d+'M net '+(dir===1?'outflow blocks LONG':'inflow blocks SHORT')) },
    { label:'Volatility in range',         ok:volGtOk,
      note:'ATR '+atrPctStr+' (need 0.10–1.50%)'+(volGtOk?' ✓':' ✗') },
    { label:'4H EMA9 > EMA21',             ok:h4ok,
      note:'4H EMA9 '+(h4ok?'>':'<')+' EMA21'+(h4ok?' ✓':' ✗ misaligned') },
    { label:'1H EMA9 > EMA21',             ok:h1ok,
      note:'1H EMA9 '+(h1ok?'>':'<')+' EMA21'+(h1ok?' ✓':' ✗ misaligned') },
    { label:'15m EMA9 > EMA21',            ok:emaOk,
      note:'15m EMA9=$'+Math.round(tech.ema9)+' '+(emaOk?'>':'<')+' EMA21=$'+Math.round(tech.ema21)+(emaOk?' ✓':' ✗') },
    { label:'VWAP pullback zone (≥4 bars)', ok:vwOk,
      note:'dist='+Math.round(dist*100)/100+'σ'+(vwOk?' ✓':' ✗ — need '+(dir===1?'-1.0 to +0.3σ':'-0.3 to +1.0σ'))+'  VWAP=$'+Math.round(tech.vwap) },
    { label:'Entry window open',           ok:sessOk,
      note:sessOk?'18:00–20:30 Dubai ✓':'Session: '+sessState+' — need ENTRY window (18:00–20:30 DXB)' }
  ];

  var allOk = etfOk&&volGtOk&&h4ok&&h1ok&&emaOk&&vwOk&&sessOk;

  if (allOk) {
    var totalSz = (ACCOUNT*RISK_PCT)/(SL_MULT*atrV);
    var sz1=totalSz*TP1_FRAC, sz2=totalSz*TP2_FRAC;
    var sl, tp1, tp2;
    if (dir===1){ sl=price-SL_MULT*atrV; tp1=price+TP1_MULT*atrV; tp2=price+TP2_MULT*atrV; }
    else        { sl=price+SL_MULT*atrV; tp1=price-TP1_MULT*atrV; tp2=price-TP2_MULT*atrV; }
    return {
      action:'ACTIVE', dir:dir===1?'LONG':'SHORT',
      entry:price, sl:sl, tp1:tp1, tp2:tp2,
      size:totalSz, sz1:sz1, sz2:sz2,
      riskDollar:Math.round(ACCOUNT*RISK_PCT),
      atr:atrV, atrPct:atrPct, checks:checks
    };
  }

  return {
    action:'WAIT', dir:dir===1?'LONG':'SHORT',
    reason:checks.filter(function(c){return!c.ok;}).map(function(c){return c.note;}).join('\n'),
    checks:checks, atr:atrV,
    vwapEntry: tech.vwap&&tech.vwapStd
      ? { low:  dir===1?Math.round(tech.vwap-tech.vwapStd):Math.round(tech.vwap-0.3*tech.vwapStd),
          high: dir===1?Math.round(tech.vwap+0.3*tech.vwapStd):Math.round(tech.vwap+tech.vwapStd) }
      : null
  };
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, nowH, macro, tech, sessState, etf, setup) {
  sheet.clear();
  [185, 130, 130, 130, 140].forEach(function(w,i){ sheet.setColumnWidth(i+1,w); });

  var utcStr   = Utilities.formatDate(now, 'UTC',        'HH:mm') + ' UTC';
  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm') + ' Dubai';
  var r = 1;

  // ── Header ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5, '₿  BTC SIGNAL  V2  —  BTC/USD  VWAP PULLBACK',
       {bg:'#1a1a0a', fg:'#F7931A', sz:15, bold:true, h:44}); r++;
  mrow(sheet, r, 5, utcStr+'  ·  '+dubaiStr+'  ·  Auto-refresh 5 min  ·  7 validated gates',
       {bg:'#12120a', fg:'#404030', sz:10, h:22}); r++; r++;

  // ── Status row ────────────────────────────────────────────────────────────
  var price   = tech?tech.price:(macro.btcPriceD||0);
  var score   = macro.score||0;
  var scoreFg = score>=SCORE_LONG?'#00e676':score>=2?'#76ff03':score>=0?'#c8ff00':
                score>=-2?'#ff9800':score>=SCORE_SHORT?'#ff5722':'#ff1744';
  var sMap = {BEFORE:'⏳ Pre-session',CHOP:'⚠ No entries yet',ENTRY:'✅ ENTRY OPEN',
              LATE:'🕐 Entry closed', CLOSED:'🔒 Session closed'};
  var sFg  = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};
  var etfSum = etf&&etf.sum5d!==null?etf.sum5d:null;

  ['BTC PRICE','MACRO SCORE','SESSION','4H TREND','ETF FLOW 5D'].forEach(function(h,i){
    cell(sheet, r, i+1, h, {bg:'#1c1c0a', fg:'#888870', sz:9, bold:true, h:22});
  });
  r++;

  cell(sheet, r,1, price?'$'+Math.round(price).toLocaleString():'—', {bg:'#0d0d00', fg:'#F7931A', sz:15, bold:true, h:44});
  cell(sheet, r,2, score.toFixed(1), {bg:'#0d0d00', fg:scoreFg, sz:15, bold:true});
  cell(sheet, r,3, sMap[sessState]||sessState, {bg:'#0d0d00', fg:sFg[sessState]||'#808080', sz:10, bold:true});
  cell(sheet, r,4, tech?(tech.h4ema9>tech.h4ema21?'↑ Bullish':'↓ Bearish'):'—',
       {bg:'#0d0d00', fg:tech?(tech.h4ema9>tech.h4ema21?'#00e676':'#ff5252'):'#808080', sz:11, bold:true});
  cell(sheet, r,5, etfSum!==null?(etfSum>=0?'+':'')+Math.round(etfSum)+'M':'—',
       {bg:'#0d0d00', fg:etfSum!==null?etfSum>=0?'#00e676':'#ff5252':'#808080', sz:13, bold:true}); r++; r++;

  // ── WHAT TO DO NOW ────────────────────────────────────────────────────────
  var inEntry = sessState==='ENTRY';
  var actionBg = setup.action==='ACTIVE'?'#0a3d0a':inEntry?'#1a2a0a':'#1a1a0d';
  var actionFg = setup.action==='ACTIVE'?'#00ff88':inEntry?'#aaddaa':'#9e9e9e';
  mrow(sheet, r, 5,
       setup.action==='ACTIVE'
         ? '⚡  ENTER '+(setup.dir||'')+'  NOW  —  All 7 entry gates passed'
         : setup.action==='SKIP'
           ? '⊘  NO TRADE — Macro score '+score.toFixed(1)+' (need ≥+6 or ≤−6)'
           : inEntry
             ? '⏱  IN SESSION — watching for '+(setup.dir||'—')+' setup  (bias: score '+score.toFixed(1)+')'
             : '⏳  NEXT SESSION: 18:00 Dubai (14:00 UTC)',
       {bg:actionBg, fg:actionFg, sz:13, bold:true, h:36}); r++;

  if (setup.action==='ACTIVE') {
    var sz1str = (Math.round(setup.sz1*10000)/10000).toFixed(4);
    var sz2str = (Math.round(setup.sz2*10000)/10000).toFixed(4);
    ['ENTRY','STOP LOSS','TP1 (60%)','TP2 (40%)','TOTAL SIZE'].forEach(function(h,i){
      cell(sheet, r, i+1, h, {bg:'#151500', fg:'#606050', sz:9, bold:true, h:22});
    });
    r++;
    var fgs5=['#F7931A','#ff5252','#00e676','#00bcd4','#ffffff'];
    var v5=['$'+Math.round(setup.entry).toLocaleString(),'$'+Math.round(setup.sl).toLocaleString(),
            '$'+Math.round(setup.tp1).toLocaleString(),'$'+Math.round(setup.tp2).toLocaleString(),
            sz1str+'+'+sz2str+' BTC'];
    v5.forEach(function(v,i){ cell(sheet, r, i+1, v, {bg:actionBg, fg:fgs5[i], sz:12, bold:true, h:44}); });
    r++;

    var slAmt=Math.abs(setup.entry-setup.sl), tp1Amt=Math.abs(setup.tp1-setup.entry);
    var tp2Amt=Math.abs(setup.tp2-setup.entry);
    cell(sheet, r,1,'ATR $'+Math.round(setup.atr)+'  ('+Math.round(setup.atrPct*10000)/100+'%)',{bg:'#0a0a00',fg:'#445566',sz:9,h:26});
    cell(sheet, r,2,'SL −$'+Math.round(slAmt),  {bg:'#0a0a00',fg:'#cc4444',sz:10,bold:true});
    cell(sheet, r,3,'TP1 +$'+Math.round(tp1Amt)+'  1.5:1',{bg:'#0a0a00',fg:'#00aa44',sz:10,bold:true});
    cell(sheet, r,4,'TP2 +$'+Math.round(tp2Amt)+'  2.5:1',{bg:'#0a0a00',fg:'#0088bb',sz:10,bold:true});
    cell(sheet, r,5,sz1str+' / '+sz2str,{bg:'#0a0a00',fg:'#aaa000',sz:9}); r++;

    var dirStr=setup.dir==='LONG'?'BUY':'SELL';
    mrow(sheet, r, 5,
         'STEP 1 → Bybit: '+dirStr+' @ market  ·  SL $'+Math.round(setup.sl).toLocaleString()+
         '  ·  Place TWO TP orders: TP1 $'+Math.round(setup.tp1).toLocaleString()+' ('+sz1str+' BTC) · TP2 $'+Math.round(setup.tp2).toLocaleString()+' ('+sz2str+' BTC)',
         {bg:'#080e00', fg:'#4488cc', sz:10, bold:true, h:32}); r++;
    mrow(sheet, r, 5,
         'STEP 2 (when TP1 hit) → TP1 order fills automatically  ·  Then manually move SL to $'+Math.round(setup.entry).toLocaleString()+' (breakeven)',
         {bg:'#060e00', fg:'#336699', sz:10, h:28}); r++;
    mrow(sheet, r, 5,
         'STEP 3 (when TP2 hit) → TP2 order fills automatically  ·  Trade complete',
         {bg:'#050a00', fg:'#2a5577', sz:10, h:28}); r++;
    mrow(sheet, r, 5,
         'EXIT RULE: No TP by 20:30 Dubai → close at market  ·  No overnight holds  ·  Risk $'+setup.riskDollar+'  ·  '+RISK_PCT*100+'% of $'+ACCOUNT,
         {bg:'#040800', fg:'#553322', sz:9, h:26}); r++;

  } else if (setup.action!=='SKIP') {
    if (setup.checks) {
      setup.checks.forEach(function(c) {
        mrow(sheet, r, 5, (c.ok?'✓  ':'✗  ')+c.label+'   —   '+c.note,
             {bg:c.ok?'#0a1a0a':'#1a0a00', fg:c.ok?'#00c853':'#ff9800', sz:10, h:26}); r++;
      });
    }
    if (setup.vwapEntry) {
      r++;
      mrow(sheet, r, 5,
           'VWAP entry zone  '+(setup.dir==='LONG'?'▲ LONG':'▼ SHORT')+
           ':  $'+setup.vwapEntry.low.toLocaleString()+' – $'+setup.vwapEntry.high.toLocaleString()+
           '  (VWAP=$'+Math.round(tech&&tech.vwap?tech.vwap:0).toLocaleString()+')',
           {bg:'#0a1628', fg:'#00bcd4', sz:10, bold:true, h:28}); r++;
    }
  } else {
    mrow(sheet, r, 5, setup.reason, {bg:'#1a1a0d', fg:'#888860', sz:10, h:32}); r++;
  }
  r++;

  // ── Macro Components ──────────────────────────────────────────────────────
  mrow(sheet, r, 5, 'MACRO COMPONENTS  (score '+score.toFixed(1)+'  |  need ≥+'+SCORE_LONG+' LONG or ≤'+SCORE_SHORT+' SHORT)',
       {bg:'#1a1a08', fg:'#60600a', sz:9, bold:true, h:22}); r++;
  (macro.components||[]).forEach(function(c) {
    var bg2=c.val>0?'#0a1a0a':c.val<0?'#1a0a0a':'#111111';
    var fg2=c.val>0?'#00c853':c.val<0?'#ff5252':'#808080';
    cell(sheet, r,1,(c.val>0?'▲ ':c.val<0?'▼ ':'– ')+c.name, {bg:bg2,fg:fg2,sz:9,bold:true,h:28});
    sheet.getRange(r,2,1,3).merge().setValue(c.note)
      .setBackground(bg2).setFontColor('#888888').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet, r,5, c.val>0?'+1':c.val<0?'−1':' 0', {bg:bg2,fg:fg2,sz:12,bold:true}); r++;
  });
  r++;

  // ── V13 Technical Filter Status ───────────────────────────────────────────
  if (tech) {
    mrow(sheet, r, 5, 'V13 CONTEXT  (A/B tested — informational only, NOT entry gates)',
         {bg:'#1a1a08', fg:'#505008', sz:9, bold:true, h:22}); r++;
    var dir = macro.dir;
    var filterRows = [
      ['MACD(12,26,9) histogram', tech.macdHist!==null?Math.round(tech.macdHist):'—',
       tech.macdHist!==null?(dir===1?tech.macdHist>0:dir===-1?tech.macdHist<0:null)?'✓ aligned':'✗ against':null],
      ['RSI(14) 15m', tech.rsi15m!==null?Math.round(tech.rsi15m):'—',
       tech.rsi15m!==null?(dir===1?tech.rsi15m<55:dir===-1?tech.rsi15m>45:null)?'✓ pullback':'✗ outside':null],
      ['Volume vs 20-bar avg', tech.volRatio!==null?Math.round(tech.volRatio*100)+'%':'—',
       tech.volRatio!==null?tech.volRatio>=1.20?'✓ surge':'✗ low vol':null],
      ['OBV vs OBV EMA10', tech.obvAbove!==null?(tech.obvAbove?'Above':'Below'):'—',
       tech.obvAbove!==null?(dir===1?tech.obvAbove===true:dir===-1?tech.obvAbove===false:null)?'✓ aligned':'✗ diverging':null]
    ];
    filterRows.forEach(function(row) {
      var ok = row[2]===null?null:row[2].charAt(0)==='✓';
      var bg3=ok===null?'#111100':ok?'#0a1a0a':'#1a0a00';
      var fg3=ok===null?'#607d8b':ok?'#00c853':'#ff5252';
      cell(sheet, r,1, row[0], {bg:bg3,fg:'#7a8870',sz:9,h:28});
      cell(sheet, r,2, row[1], {bg:bg3,fg:'#e0e0e0',sz:12,bold:true});
      sheet.getRange(r,3,1,2).merge().setValue(row[2]||'—')
        .setBackground(bg3).setFontColor(fg3).setFontSize(10).setFontWeight('bold')
        .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
      cell(sheet, r,5,ok===null?'—':ok?'PASS':'FAIL',{bg:bg3,fg:fg3,sz:10,bold:true}); r++;
    });
    r++;

    // ── Key Levels ──────────────────────────────────────────────────────────
    mrow(sheet, r, 5, 'KEY LEVELS', {bg:'#1a1a08', fg:'#60600a', sz:9, bold:true, h:22}); r++;
    var lvls = [
      ['Session VWAP', '$'+Math.round(tech.vwap).toLocaleString(), tech.vwapBars+' bars since 17:30 Dubai'],
      ['+1σ (short zone)', '$'+Math.round(tech.vwap+tech.vwapStd).toLocaleString(), 'Short entry zone upper bound'],
      ['−1σ (long zone)',  '$'+Math.round(tech.vwap-tech.vwapStd).toLocaleString(), 'Long entry zone lower bound'],
      ['4H EMA9',  '$'+Math.round(tech.h4ema9).toLocaleString(),  '4H structure'],
      ['4H EMA21', '$'+Math.round(tech.h4ema21).toLocaleString(), '4H structure'],
      ['1H EMA9',  '$'+Math.round(tech.h1ema9).toLocaleString(),  '1H alignment'],
      ['1H EMA21', '$'+Math.round(tech.h1ema21).toLocaleString(), '1H alignment'],
      ['15m EMA9',  '$'+Math.round(tech.ema9).toLocaleString(),   '15m entry'],
      ['15m EMA21', '$'+Math.round(tech.ema21).toLocaleString(),  '15m entry']
    ];
    if (macro.btcEma200) lvls.push(['Daily EMA200','$'+Math.round(macro.btcEma200).toLocaleString(),'Regime — longs only above this']);
    lvls.forEach(function(lv) {
      cell(sheet, r,1, lv[0], {bg:'#110f00',fg:'#808060',sz:9,bold:true,h:26});
      cell(sheet, r,2, lv[1], {bg:'#110f00',fg:'#e0e0e0',sz:11,bold:true});
      sheet.getRange(r,3,1,3).merge().setValue(lv[2])
        .setBackground('#110f00').setFontColor('#505040').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left'); r++;
    });
    r++;
  }

  // ── ETF Context ───────────────────────────────────────────────────────────
  if (etf) {
    mrow(sheet, r, 5, 'ETF FLOW CONTEXT  (gate active since Jan 11 2024)',
         {bg:'#1a1a08', fg:'#60600a', sz:9, bold:true, h:22}); r++;
    var etfRows = etf.error
      ? [['Status','Error',etf.error]]
      : [
          ['Latest day','$'+(etf.latest!==undefined?(etf.latest>=0?'+':'')+Math.round(etf.latest):'—')+'M','Most recent daily total'],
          ['5-day net','$'+(etf.sum5d!==null?(etf.sum5d>=0?'+':'')+Math.round(etf.sum5d):'—')+'M','Longs blocked <0 · Shorts blocked >0'],
          ['Gate status',macro.dir===0?'—':etfGateAllows(macro.dir,etf)?'✓ ALLOWS':'✗ BLOCKS',
           macro.dir===0?'No bias':macro.dir===1?'LONG bias':'SHORT bias']
        ];
    etfRows.forEach(function(row) {
      var isBlock=row[1]==='✗ BLOCKS', isAllow=row[1]==='✓ ALLOWS';
      cell(sheet, r,1, row[0], {bg:'#110f00',fg:'#808060',sz:9,bold:true,h:26});
      cell(sheet, r,2, row[1], {bg:'#110f00',fg:isBlock?'#ff5252':isAllow?'#00e676':'#e0e0e0',sz:11,bold:true});
      sheet.getRange(r,3,1,3).merge().setValue(row[2])
        .setBackground('#110f00').setFontColor('#505040').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left'); r++;
    });
    r++;
  }

  // ── Daily Schedule ────────────────────────────────────────────────────────
  mrow(sheet, r, 5, '⏰  DAILY SCHEDULE (Dubai)', {bg:'#1a1a08', fg:'#60600a', sz:9, bold:true, h:22}); r++;
  var schedule = [
    ['17:15 DXB','Pre-session','#1a1a0a','Review macro score + ETF flows · set price alerts near VWAP zone'],
    ['17:30 DXB','Session open','#0a0a00','VWAP starts building from 13:30 UTC — no trades yet (chop)'],
    ['18:00 DXB','▶ ENTRY OPEN','#0a2a00','Entry window opens — all 7 gates must pass · VWAP pullback setup'],
    ['19:00 DXB','Mid-session','#0a1200','Check positions · if TP1 hit → move SL to breakeven'],
    ['20:30 DXB','Entry closes','#1a0800','No new entries — manage existing only'],
    ['21:00 DXB','Session ends','#0a0800','Close ALL trades — no overnight holds']
  ];
  schedule.forEach(function(row) {
    cell(sheet, r,1, row[0], {bg:row[2],fg:row[2]==='#0a2a00'?'#00e676':'#c0ccd6',sz:10,bold:true,h:28});
    cell(sheet, r,2, row[1], {bg:row[2],fg:'#aabb99',sz:9});
    sheet.getRange(r,3,1,3).merge().setValue(row[3])
      .setBackground(row[2]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ── Footer ────────────────────────────────────────────────────────────────
  r++;
  mrow(sheet, r, 5,
       'VALIDATED GATES (7): ETF flow · vol% range · 4H/1H/15m EMA · VWAP ±1σ · session window  |  MACD/RSI/Vol/OBV shown as context (A/B tested — inconclusive with Twelve Data)',
       {bg:'#080800', fg:'#282810', sz:8, h:22}); r++;
  mrow(sheet, r, 5,
       'Backtest Apr 2023–May 2026 · p=0.006 ✓ · Sharpe 2.83 · WR 47.9% · MDD −17.5% · +269% · 242 trades',
       {bg:'#080800', fg:'#404020', sz:8, h:22}); r++;
  mrow(sheet, r, 5,
       'Risk '+RISK_PCT*100+'% ($'+Math.round(ACCOUNT*RISK_PCT)+') · SL=0.75×ATR · TP1=1.5×ATR (60%) · TP2=2.5×ATR (40%) · Time stop 90min · Max 2 trades/session',
       {bg:'#080800', fg:'#282810', sz:8, h:22});

  SpreadsheetApp.flush();
}

// ── MATH HELPERS ─────────────────────────────────────────────────────────────
function emaArr(arr, n) {
  var k=2/(n+1), out=[arr[0]];
  for (var i=1; i<arr.length; i++) out.push(arr[i]*k+out[i-1]*(1-k));
  return out;
}

function calcAtr14(bars) {
  var tr=bars.map(function(b,i) {
    if(i===0)return b.h-b.l;
    return Math.max(b.h-b.l,Math.abs(b.h-bars[i-1].c),Math.abs(b.l-bars[i-1].c));
  });
  var e=emaArr(tr,14); return e[e.length-1];
}

function calcMacdHistogram(closes) {
  if (closes.length<35) return null;
  var e12=emaArr(closes,12), e26=emaArr(closes,26);
  var macd=e12.map(function(v,i){return v-e26[i];});
  var sig=emaArr(macd,9);
  return macd[macd.length-1]-sig[sig.length-1];
}

function calcRsi14(closes) {
  if (closes.length<16) return null;
  var g=[],l=[];
  for (var i=1; i<closes.length; i++) {
    var d=closes[i]-closes[i-1]; g.push(d>0?d:0); l.push(d<0?-d:0);
  }
  var k=1/14, ag=g[0], al=l[0];
  for (var j=1; j<g.length; j++){ag=ag*(1-k)+g[j]*k; al=al*(1-k)+l[j]*k;}
  return al===0?100:100-100/(1+ag/al);
}

function calcObvAboveEma(closes, volumes) {
  if (!closes||closes.length<12||closes.length!==volumes.length) return null;
  var obv=[0];
  for (var i=1; i<closes.length; i++) {
    var sign=closes[i]>closes[i-1]?1:closes[i]<closes[i-1]?-1:0;
    obv.push(obv[i-1]+sign*volumes[i]);
  }
  var e=emaArr(obv,10);
  return obv[obv.length-1]>e[e.length-1];
}

function resampleTo4H(bars1h) {
  var out=[],cur=null;
  (bars1h||[]).forEach(function(b) {
    var dt=new Date(b.dt.replace(' ','T')+'Z');
    var slot=Math.floor(dt.getUTCHours()/4)*4;
    var key=dt.getUTCFullYear()+'-'+dt.getUTCMonth()+'-'+dt.getUTCDate()+'-'+slot;
    if(!cur||cur.key!==key){if(cur)out.push(cur);cur={key:key,o:b.o,h:b.h,l:b.l,c:b.c};}
    else{cur.h=Math.max(cur.h,b.h);cur.l=Math.min(cur.l,b.l);cur.c=b.c;}
  });
  if(cur)out.push(cur); return out;
}

function cell(sheet, row, col, val, opts) {
  var o=opts||{};
  sheet.getRange(row,col).setValue(val)
    .setBackground(o.bg||'#0d0d00').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if(o.h) sheet.setRowHeight(row,o.h);
}

function mrow(sheet, row, cols, val, opts) {
  var o=opts||{};
  sheet.getRange(row,1,1,cols).merge().setValue(val)
    .setBackground(o.bg||'#0d0d00').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(row,o.h||32);
}
