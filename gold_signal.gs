/**
 * GOLD SIGNAL DASHBOARD  V2 — Google Apps Script
 * =================================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste → Save
 *   2. Run setupTrigger() once (authorize)
 *   3. Run updateDashboard() to load immediately
 *   Auto-refreshes every 5 minutes.
 *
 * Session: 17:30–21:00 Dubai  |  Entry window: 18:00–20:30 Dubai
 *          (13:30–17:00 UTC)                 (14:00–16:30 UTC)
 *
 * V2: Step-by-step MT5 execution instructions.
 *
 * FILTER VALIDATION — V3 backtests (gold_v3_backtest.py, 2yr data):
 *   VIX ≥ 30        — ADD  (ΔSharpe +0.18; combined +0.44; crash regime Sharpe −0.60)
 *   Skip June       — ADD  (ΔSharpe +0.27; June historical Sharpe −1.67)
 *   Daily ATR 40-80% — INFO (ΔSharpe +1.38, p=0.083 borderline — shown as context)
 *   ADX 20-35        — SKIP (ΔSharpe −2.41 — system is mean-reversion, fails in trend filter)
 *   London ORB       — SKIP (ΔSharpe −0.49 — doesn't help)
 *   Skip Monday      — SKIP (ΔSharpe −0.95 — hurts)
 *   London session   — SKIP (ΔSharpe −0.40 — lower quality than NY window)
 *
 * V13 FILTER VALIDATION (gold_v13_filter_test.py):
 *   MACD histogram   — SKIP  (ΔSharpe −0.22, hurts)
 *   RSI < 55 / > 45  — SKIP  (ΔSharpe −1.25, hurts badly)
 *   Volume ≥ 120%    — SKIP  (0 trades — Twelve Data vol is synthetic tick-count)
 *   OBV > EMA10      — SKIP  (synthetic volume makes OBV unvalidatable)
 *
 * Macro signal: 5 components, score ≥0 = LONG bias, <0 = SHORT bias.
 * Entry requires 6 validated gates: session window, 4H EMA, 15m EMA, VWAP zone, VIX<30, not-June.
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var TWELVE_KEY   = "06f050cd7d9940a895f9461e7f0ff7e3";
var ACCOUNT      = 10000;
var RISK_PCT     = 0.03;
var SL_MULT      = 0.75;
var TP1_MULT     = 1.5;
var TP2_MULT     = 2.5;
var TP1_FRAC     = 0.60;
var TP2_FRAC     = 0.40;
var TIME_STOP    = 6;        // bars before time exit (90 min)

var SESS_OPEN    = 13.50;    // 13:30 UTC = 17:30 Dubai
var SESS_CLOSE   = 17.00;    // 17:00 UTC = 21:00 Dubai
var ENTRY_START  = 14.00;    // 14:00 UTC = 18:00 Dubai
var ENTRY_END    = 16.50;    // 16:30 UTC = 20:30 Dubai

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ GOLD SIGNAL V2 — Auto-refresh every 5 minutes.\n\n' +
      'Run updateDashboard() to populate now.\n\n' +
      'Dubai check times:\n' +
      '  17:15 — Pre-session check: review macro score\n' +
      '  18:00 — Entry window opens: watch for VWAP pullback\n' +
      '  20:30 — Entry window closes: manage open trades\n' +
      '  21:00 — Session ends: close all open trades'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('GOLD SIGNAL') || ss.insertSheet('GOLD SIGNAL');

  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  var macro     = getMacroScore();
  var bars15m   = get15mBars(80);    // need 35+ for MACD warmup
  var bars1h    = get1hBars(260);    // 4H resampling needs ~65 4H bars
  var sessState = getSessionState(nowH);

  var tech  = computeTechnicals(bars15m, bars1h);
  var setup = computeSetup(macro, tech, sessState);

  writeSheet(dash, now, nowH, macro, tech, sessState, setup);
}

// ── MACRO SCORE (Yahoo Finance, cached 6h) ────────────────────────────────────
function getMacroScore() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var cached = cache.get('gold_macro_v2_' + today);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yf(sym, range) {
    try {
      var url = 'https://query1.finance.yahoo.com/v8/finance/chart/' +
                encodeURIComponent(sym) + '?interval=1d&range=' + range;
      var r = UrlFetchApp.fetch(url, { muteHttpExceptions:true, headers:{'User-Agent':'Mozilla/5.0'} });
      var j = JSON.parse(r.getContentText());
      return j.chart.result[0].indicators.quote[0].close.filter(function(c){ return c != null; });
    } catch(e) { return null; }
  }

  var gold = yf('GC=F',      '3mo');
  var dxy  = yf('DX-Y.NYB',  '3mo');
  var tnx  = yf('%5ETNX',    '3mo');
  var gdx  = yf('GDX',       '3mo');
  var vix  = yf('%5EVIX',    '5d');

  var result = { score:0, dir:1, components:[], error:null, goldPriceD:null, vix:null };
  if (!gold || gold.length < 55) { result.error = 'Macro data unavailable'; return result; }

  result.vix = vix && vix.length > 0 ? Math.round(vix[vix.length-1] * 10) / 10 : null;

  var n    = gold.length;
  result.goldPriceD = gold[n-1];
  var e50  = emaArr(gold, 50);

  var s_trend = gold[n-2] > e50[n-2]                               ? 1 : -1;
  var s_yield = (tnx && tnx.length >= 3)
    ? (tnx[tnx.length-2] < tnx[tnx.length-3]                      ? 1 : -1) : 0;
  var s_dxy   = (dxy && dxy.length >= 3)
    ? ((dxy[dxy.length-2]/dxy[dxy.length-3] - 1) < 0               ? 1 : -1) : 0;
  var s_mom   = n >= 8  ? (gold[n-2] > gold[n-7]                   ? 1 : -1) : 0;
  var s_gdx   = (gdx && gdx.length >= 3 && n >= 3)
    ? ((gdx[gdx.length-2]/gdx[gdx.length-3] - 1) >
       (gold[n-2]/gold[n-3] - 1)                                   ? 1 : -1) : 0;

  var raw      = s_trend + s_yield + s_dxy + s_mom + s_gdx;
  result.score = Math.round(raw / 5 * 100) / 10;
  result.dir   = result.score >= 0 ? 1 : -1;

  var mom5pct  = n >= 8 ? Math.round((gold[n-2]/gold[n-7] - 1)*1000)/10 : 0;
  var gdxDelta = (gdx && gdx.length >= 3 && n >= 3)
    ? Math.round(((gdx[gdx.length-2]/gdx[gdx.length-3]) - (gold[n-2]/gold[n-3]))*1000)/10 : 0;

  result.components = [
    { name:'Gold vs EMA50',    val:s_trend, note:'Gold '+(s_trend>0?'above':'below')+' daily EMA50 ('+Math.round(e50[n-2])+')' },
    { name:'10Y yield',        val:s_yield, note:'Yield '+(s_yield>0?'falling ↓ (gold bullish)':'rising ↑ (gold bearish)') },
    { name:'DXY direction',    val:s_dxy,   note:'DXY '+(s_dxy>0?'falling ↓':'rising ↑') },
    { name:'Gold 5d momentum', val:s_mom,   note:(mom5pct>=0?'+':'')+mom5pct+'% over 5 days' },
    { name:'GDX lead',         val:s_gdx,   note:'Miners '+(s_gdx>0?'leading (+'+gdxDelta+'%)':'lagging ('+gdxDelta+'%)') }
  ];

  cache.put('gold_macro_v2_' + today, JSON.stringify(result), 21600);
  return result;
}

// ── BAR FETCH (Twelve Data — includes volume) ─────────────────────────────────
function tdFetch(interval, outputsize) {
  try {
    var url = 'https://api.twelvedata.com/time_series?symbol=XAU/USD' +
              '&interval=' + interval + '&outputsize=' + outputsize +
              '&apikey=' + TWELVE_KEY + '&timezone=UTC';
    var r = UrlFetchApp.fetch(url, {muteHttpExceptions:true});
    var j = JSON.parse(r.getContentText());
    if (!j.values || j.status === 'error') return [];
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

  var closes = bars15m.map(function(b){ return b.c; });
  var vols   = bars15m.map(function(b){ return b.v || 0; });
  var e9     = emaArr(closes, 9);
  var e21    = emaArr(closes, 21);
  var atrV   = calcAtr14(bars15m);
  var last   = bars15m[bars15m.length - 1];

  // V13 technical filter values
  var macdHist = calcMacdHistogram(closes);
  var rsi15m   = calcRsi14(closes);

  var volN = Math.min(20, vols.length - 1), volSum = 0;
  for (var vi = vols.length - 1 - volN; vi < vols.length - 1; vi++) volSum += vols[vi];
  var avgVol   = volN > 0 ? volSum / volN : 0;
  var volRatio = avgVol > 0 ? vols[vols.length-1] / avgVol : null;
  var obvAbove = calcObvAboveEma(closes, vols);

  // 1H EMA
  var h1e9=null, h1e21=null;
  if (bars1h && bars1h.length >= 22) {
    var c1h = bars1h.map(function(b){ return b.c; });
    var ha9  = emaArr(c1h, 9), ha21 = emaArr(c1h, 21);
    h1e9  = ha9[ha9.length-1];
    h1e21 = ha21[ha21.length-1];
  }

  // 4H EMA
  var bars4h = resampleTo4H(bars1h);
  var c4h    = bars4h.map(function(b){ return b.c; });
  var h4e9   = emaArr(c4h, 9), h4e21 = emaArr(c4h, 21), h4e50 = emaArr(c4h, 50);
  var li     = c4h.length - 1;

  var sv = sessionVwap(bars15m);

  // Daily range ratio from 1h bars (proxy for ATR percentile)
  var dailyRanges = {};
  (bars1h || []).forEach(function(b) {
    var day = b.dt.substring(0, 10);
    if (!dailyRanges[day]) dailyRanges[day] = { h: b.h, l: b.l };
    else { dailyRanges[day].h = Math.max(dailyRanges[day].h, b.h);
           dailyRanges[day].l = Math.min(dailyRanges[day].l, b.l); }
  });
  var drDays    = Object.keys(dailyRanges).sort();
  var drArr     = drDays.map(function(d){ return dailyRanges[d].h - dailyRanges[d].l; });
  var drAvg     = drArr.length > 2 ? drArr.slice(0, -1).reduce(function(a,v){return a+v;},0) / Math.max(drArr.length-1,1) : null;
  var drToday   = drArr.length > 0 ? drArr[drArr.length-1] : null;
  var drRatio   = (drAvg && drToday) ? drToday / drAvg : null;
  var drFlag    = drRatio ? (drRatio < 0.6 ? 'LOW' : drRatio > 2.0 ? 'HIGH' : 'OK') : null;

  return {
    price: last.c, high: last.h, low: last.l,
    ema9: e9[e9.length-1], ema21: e21[e21.length-1],
    atr: atrV,
    dailyRange: drToday, dailyRangeAvg: drAvg, drRatio: drRatio, drFlag: drFlag,
    h1ema9: h1e9, h1ema21: h1e21,
    h4ema9: h4e9[li], h4ema21: h4e21[li], h4ema50: h4e50[li],
    vwap: sv.vwap, vwapStd: sv.std, vwapBars: sv.n,
    vwapDist: sv.std > 0.01 && sv.vwap ? (last.c - sv.vwap) / sv.std : 99,
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
  var sOpen = new Date(ymd + 'T13:30:00Z');
  var sess = bars15m.filter(function(b){ return new Date(b.dt.replace(' ','T')+'Z') >= sOpen; });
  if (sess.length < 2) return { vwap:0, std:1, n:0 };
  var tp  = sess.map(function(b){ return (b.h+b.l+b.c)/3; });
  var avg = tp.reduce(function(a,v){ return a+v; },0) / tp.length;
  var v2  = tp.reduce(function(a,x){ return a+Math.pow(x-avg,2); },0) / tp.length;
  return { vwap:avg, std:Math.max(Math.sqrt(v2),0.01), n:tp.length };
}

// ── SESSION STATE ─────────────────────────────────────────────────────────────
function getSessionState(nowH) {
  if (nowH < SESS_OPEN)   return 'BEFORE';
  if (nowH < ENTRY_START) return 'CHOP';
  if (nowH <= ENTRY_END)  return 'ENTRY';
  if (nowH <= SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}

// ── SETUP LOGIC ───────────────────────────────────────────────────────────────
function computeSetup(macro, tech, sessState) {
  if (!tech || macro.error) return { action:'WAIT', reason:macro.error||'No data' };

  var dir   = macro.dir;
  var price = tech.price;
  var atrV  = tech.atr;
  var nowM  = new Date().getUTCMonth(); // 0-indexed: June = 5

  // ── Day-level regime gates (tested in gold_v3_backtest.py) ───────────────
  var vixOk  = macro.vix === null || macro.vix < 30;  // crash regime: Sharpe −0.60 at VIX≥30
  var junOk  = nowM !== 5;                             // June: historical Sharpe −1.67

  // Short gate: blocked if price above 4H EMA50 (gold in bull trend — avoid shorting uptrend)
  if (dir === -1 && tech.h4ema50 && price > tech.h4ema50)
    return { action:'SKIP', dir:'SHORT',
             reason:'Short blocked — gold above 4H EMA50 ('+fix(tech.h4ema50)+') — major bull trend' };

  var dist = tech.vwapStd > 0 ? (price - tech.vwap) / tech.vwapStd : 99;

  // ── Validated entry gates (6 required) ───────────────────────────────────
  var h4ok   = tech.h4ema9 && tech.h4ema21
    ? (dir===1 ? tech.h4ema9>tech.h4ema21 : tech.h4ema9<tech.h4ema21) : null;
  var emaOk  = dir===1 ? tech.ema9>tech.ema21 : tech.ema9<tech.ema21;
  var vwOk   = tech.vwapBars >= 4 &&
    (dir===1 ? (dist>=-1.0 && dist<=0.3) : (dist>=-0.3 && dist<=1.0));
  var sessOk = sessState === 'ENTRY';

  var vixStr = macro.vix !== null ? 'VIX='+macro.vix.toFixed(1) : 'VIX data unavailable';
  var checks = [
    { label:'Session entry window',         ok:sessOk,
      note: sessOk?'✓ 18:00–20:30 Dubai active':'Outside entry window (18:00–20:30 DXB / 14:00–16:30 UTC)' },
    { label:'4H EMA9 > EMA21',              ok:h4ok!==null?h4ok:true,
      note: h4ok===null?'No 4H data':h4ok?'4H EMA9 '+fix(tech.h4ema9)+' aligned':'4H EMA9/21 misaligned — wrong trend' },
    { label:'15m EMA9 > EMA21',             ok:emaOk,
      note: emaOk?'EMA9 '+fix(tech.ema9)+' aligned':'EMA9 '+fix(tech.ema9)+' / EMA21 '+fix(tech.ema21)+' misaligned' },
    { label:'VWAP pullback zone (≥4 bars)', ok:vwOk,
      note: vwOk
        ? 'dist='+Math.round(dist*100)/100+'σ ✓  VWAP=$'+fix(tech.vwap)
        : tech.vwapBars<4 ? 'Only '+tech.vwapBars+' session bars (need ≥4)'
          : 'dist='+Math.round(dist*100)/100+'σ — need '+(dir===1?'-1.0 to +0.3σ':'-0.3 to +1.0σ')+'  VWAP=$'+fix(tech.vwap) },
    { label:'VIX regime (< 30)',            ok:vixOk,
      note: vixOk
        ? vixStr+' — normal regime (optimal zone: 20–25, backtest Sharpe +1.81)'
        : vixStr+' ≥ 30 — CRASH regime: gold Sharpe −0.60 in backtest. Skip today.' },
    { label:'Month filter (not June)',       ok:junOk,
      note: junOk ? 'Month OK — not June' : 'June — worst month for gold historically (Sharpe −1.67). Skip.' }
  ];

  var allOk = sessOk && (h4ok!==false) && emaOk && vwOk && vixOk && junOk;

  if (allOk) {
    var totalSz = (ACCOUNT * RISK_PCT) / (SL_MULT * atrV);
    var sl, tp1, tp2;
    if (dir === 1) { sl=price-SL_MULT*atrV; tp1=price+TP1_MULT*atrV; tp2=price+TP2_MULT*atrV; }
    else           { sl=price+SL_MULT*atrV; tp1=price-TP1_MULT*atrV; tp2=price-TP2_MULT*atrV; }
    return {
      action:'ACTIVE', dir:dir===1?'LONG':'SHORT',
      entry:price, sl:sl, tp1:tp1, tp2:tp2,
      size:Math.round(totalSz*100)/100,
      sz1:Math.round(totalSz*TP1_FRAC*100)/100,
      sz2:Math.round(totalSz*TP2_FRAC*100)/100,
      riskDollar:Math.round(ACCOUNT*RISK_PCT),
      atr:atrV, slPts:SL_MULT*atrV, tp1Pts:TP1_MULT*atrV, tp2Pts:TP2_MULT*atrV,
      checks:checks
    };
  }

  return {
    action:'WAIT', dir:dir===1?'LONG':'SHORT',
    reason:checks.filter(function(c){return!c.ok;}).map(function(c){return c.note;}).join('\n'),
    checks:checks, atr:atrV,
    // Show what price is needed for VWAP zone
    vwapEntry: tech.vwap && tech.vwapStd
      ? { low:  dir===1 ? fix(tech.vwap - tech.vwapStd)       : fix(tech.vwap - 0.3*tech.vwapStd),
          high: dir===1 ? fix(tech.vwap + 0.3*tech.vwapStd)  : fix(tech.vwap + tech.vwapStd) }
      : null
  };
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, nowH, macro, tech, sessState, setup) {
  sheet.clear();
  [185, 130, 130, 130, 130].forEach(function(w,i){ sheet.setColumnWidth(i+1,w); });

  var utcStr   = Utilities.formatDate(now, 'UTC',        'HH:mm') + ' UTC';
  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm') + ' Dubai';
  var r = 1;

  // ── Header ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5, '⚡  GOLD SIGNAL  V2  —  XAU/USD  VWAP PULLBACK',
       {bg:'#1a1a2e', fg:'#FFD700', sz:15, bold:true, h:44}); r++;
  mrow(sheet, r, 5, utcStr+'  ·  '+dubaiStr+'  ·  Auto-refresh 5 min',
       {bg:'#12122a', fg:'#404070', sz:10, h:22}); r++; r++;

  // ── Price / Score / Session bar ───────────────────────────────────────────
  var price   = tech ? tech.price : (macro.goldPriceD || 0);
  var score   = macro.score || 0;
  var scoreFg = score >= 6?'#00e676':score>=2?'#76ff03':score>=0?'#c8ff00':
                score>=-2?'#ff9800':score>=-6?'#ff5722':'#ff1744';
  var sMap = {BEFORE:'⏳ Pre-session',CHOP:'⚠ No entries yet (chop)',ENTRY:'✅ ENTRY OPEN',
              LATE:'🕐 Entry closed',CLOSED:'🔒 Session closed'};
  var sFg  = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};

  var vixNow   = macro.vix;
  var vixFg    = !vixNow ? '#808080' : vixNow >= 30 ? '#ff1744' : vixNow >= 20 ? '#ff9800' : '#00e676';
  var vixLabel = !vixNow ? '—' : vixNow.toFixed(1) + (vixNow >= 30 ? ' ⚠' : '');
  ['GOLD PRICE','MACRO SCORE','SESSION','VIX','4H TREND'].forEach(function(h,i){
    cell(sheet, r, i+1, h, {bg:'#1c1c3a', fg:'#8888aa', sz:9, bold:true, h:22});
  });
  r++;

  var h4Trend = tech ? (tech.h4ema9>tech.h4ema21?'↑ Bullish':'↓ Bearish') : '—';
  cell(sheet, r,1, price?'$'+fix(price):'—',     {bg:'#0d0d20', fg:'#FFD700', sz:16, bold:true, h:44});
  cell(sheet, r,2, score.toFixed(1),              {bg:'#0d0d20', fg:scoreFg,  sz:16, bold:true});
  cell(sheet, r,3, sMap[sessState]||sessState,    {bg:'#0d0d20', fg:sFg[sessState]||'#808080', sz:10, bold:true});
  cell(sheet, r,4, vixLabel,                      {bg:'#0d0d20', fg:vixFg, sz:16, bold:true});
  cell(sheet, r,5, h4Trend,                       {bg:'#0d0d20', fg:tech?(tech.h4ema9>tech.h4ema21?'#00e676':'#ff5252'):'#808080', sz:11, bold:true});
  r++;
  // ATR subrow
  cell(sheet, r,1, tech?'ATR $'+fix(tech.atr):'—',{bg:'#080818', fg:'#445566', sz:9, h:20});
  sheet.getRange(r,2,1,4).merge().setValue(
    tech && tech.dailyRange ? 'Daily range: $'+fix(tech.dailyRange)+(tech.drRatio?'  ('+Math.round(tech.drRatio*100)+'% of '+fix(tech.dailyRangeAvg)+' avg'+
      (tech.drFlag==='LOW'?' — LOW VOL ⚠':tech.drFlag==='HIGH'?' — HIGH VOL ⚠':' — normal')+')':''):'')
    .setBackground('#080818').setFontColor(
      tech&&tech.drFlag==='LOW'?'#ff9800':tech&&tech.drFlag==='HIGH'?'#ff5252':'#445566')
    .setFontSize(9).setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
  r++; r++;

  // ── WHAT TO DO NOW ────────────────────────────────────────────────────────
  var inEntry = sessState === 'ENTRY';
  var actionBg = setup.action==='ACTIVE'?'#0a3d0a':inEntry?'#1a2a0a':'#1a1a1a';
  var actionFg = setup.action==='ACTIVE'?'#00ff88':inEntry?'#aaddaa':'#9e9e9e';
  mrow(sheet, r, 5,
       setup.action==='ACTIVE'
         ? '⚡  ENTER '+(setup.dir||'')+'  NOW  —  All 6 entry gates passed'
         : inEntry
           ? '⏱  IN SESSION — watching for setup  (Bias: '+(setup.dir||'—')+')'
           : '⏳  NEXT SESSION: 18:00 Dubai (14:00 UTC)',
       {bg:actionBg, fg:actionFg, sz:13, bold:true, h:36}); r++;

  if (setup.action === 'ACTIVE') {
    // Trade block
    ['ENTRY','STOP LOSS','TP1 (close 60%)','TP2 (close 40%)','SIZE (oz)'].forEach(function(h,i){
      cell(sheet, r, i+1, h, {bg:'#151528', fg:'#606090', sz:9, bold:true, h:22});
    });
    r++;
    var fgs5 = ['#FFD700','#ff5252','#00e676','#00bcd4','#ffffff'];
    var v5   = ['$'+fix(setup.entry),'$'+fix(setup.sl),'$'+fix(setup.tp1),'$'+fix(setup.tp2),setup.size+' oz'];
    v5.forEach(function(v,i){ cell(sheet, r, i+1, v, {bg:actionBg, fg:fgs5[i], sz:13, bold:true, h:44}); });
    r++;

    var dir1 = setup.dir==='LONG';
    cell(sheet, r,1,'ATR $'+fix(setup.atr),                                          {bg:'#0a0a1a',fg:'#445566',sz:9,h:26});
    cell(sheet, r,2,'−'+fix(setup.slPts)+' ($'+fix(setup.slPts)+')',                  {bg:'#0a0a1a',fg:'#cc4444',sz:9,bold:true});
    cell(sheet, r,3,'+'+fix(setup.tp1Pts)+'  R:R 1.5:1',                              {bg:'#0a0a1a',fg:'#00aa44',sz:10,bold:true});
    cell(sheet, r,4,'+'+fix(setup.tp2Pts)+'  R:R 2.5:1',                              {bg:'#0a0a1a',fg:'#0088bb',sz:10,bold:true});
    cell(sheet, r,5,setup.sz1+' + '+setup.sz2+' oz',                                  {bg:'#0a0a1a',fg:'#aaa000',sz:9}); r++;

    mrow(sheet, r, 5,
         'STEP 1 → MT5: '+(dir1?'BUY':'SELL')+' @ market  ·  SL $'+fix(setup.sl)+'  ·  TP1 $'+fix(setup.tp1)+'  ·  TP2 $'+fix(setup.tp2)+'  ·  Total '+setup.size+' oz',
         {bg:'#080e1a', fg:'#4488cc', sz:10, bold:true, h:32}); r++;
    mrow(sheet, r, 5,
         'STEP 2 (when TP1 hit) → Close '+setup.sz1+' oz  ·  Move SL to $'+fix(setup.entry)+' (breakeven)',
         {bg:'#060e1a', fg:'#336699', sz:10, h:28}); r++;
    mrow(sheet, r, 5,
         'STEP 3 (when TP2 hit) → Close remaining '+setup.sz2+' oz  ·  Trade done',
         {bg:'#050a12', fg:'#2a5577', sz:10, h:28}); r++;
    mrow(sheet, r, 5,
         'EXIT RULE: No TP by 20:30 Dubai → close at market  ·  Do NOT hold overnight  ·  Risk $'+setup.riskDollar+'  ·  '+RISK_PCT*100+'% of $'+ACCOUNT,
         {bg:'#040810', fg:'#553322', sz:9, h:26}); r++;

  } else {
    // Wait mode: show checklist + VWAP entry zone
    if (setup.checks) {
      var failedFirst = setup.checks.slice().sort(function(a,b){ return (a.ok?1:0)-(b.ok?1:0); });
      failedFirst.forEach(function(c) {
        mrow(sheet, r, 5, (c.ok?'✓  ':'✗  ')+c.label+'   —   '+c.note,
             {bg:c.ok?'#0a1a0a':'#1a0a0a', fg:c.ok?'#00c853':'#ff5252', sz:10, h:26}); r++;
      });
    }

    // Show the entry zone price range
    if (setup.vwapEntry && tech) {
      r++;
      mrow(sheet, r, 5,
           'VWAP entry zone  '+(setup.dir==='LONG'?'▲ LONG':'▼ SHORT')+
           ':  $'+setup.vwapEntry.low+' – $'+setup.vwapEntry.high+
           '  (VWAP=$'+fix(tech.vwap)+'  ±1σ=$'+fix(tech.vwapStd)+')',
           {bg:'#0a1628', fg:'#00bcd4', sz:10, bold:true, h:28}); r++;
    }
  }
  r++;

  // ── Macro Components ──────────────────────────────────────────────────────
  mrow(sheet, r, 5, 'MACRO COMPONENTS  (score '+score.toFixed(1)+'  |  ≥0=LONG  <0=SHORT)',
       {bg:'#1a1a2e', fg:'#6060a0', sz:9, bold:true, h:22}); r++;
  (macro.components || []).forEach(function(c) {
    var bg2=c.val>0?'#0a1a0a':'#1a0a0a', fg2=c.val>0?'#00c853':'#ff5252';
    cell(sheet, r,1, (c.val>0?'▲ ':'▼ ')+c.name, {bg:bg2, fg:fg2, sz:9, bold:true, h:28});
    sheet.getRange(r,2,1,3).merge().setValue(c.note)
      .setBackground(bg2).setFontColor('#888888').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet, r,5, c.val>0?'+1':'−1', {bg:bg2, fg:fg2, sz:12, bold:true}); r++;
  });
  r++;

  // ── V3 Regime Context ─────────────────────────────────────────────────────
  if (tech) {
    var vixNow2  = macro.vix;
    var drRatio2 = tech.drRatio;
    var drFlag2  = tech.drFlag;
    mrow(sheet, r, 5, 'V3 REGIME CONTEXT  (daily filters — VIX & ATR)',
         {bg:'#1a2a1a', fg:'#50a050', sz:9, bold:true, h:22}); r++;
    // VIX row
    var vixOk2 = !vixNow2 || vixNow2 < 30;
    var v3Rows = [
      ['VIX level',
       vixNow2 ? vixNow2.toFixed(1) : '—',
       vixOk2 ? (vixNow2 ? (vixNow2 < 20 ? '✓ low vol (<20)' : vixNow2 < 25 ? '✓ sweet spot (20-25)' : '✓ elevated, watch') : '✓ n/a') : '✗ SKIP — crash regime',
       vixOk2],
      ['Daily range vs avg',
       drRatio2 ? Math.round(drRatio2*100)+'%' : '—',
       !drRatio2 ? '—' : drFlag2==='OK' ? '✓ normal vol range (40-80th pctile)' : drFlag2==='LOW' ? '⚠ LOW vol — thin day, consider skip' : '⚠ HIGH vol — chaotic day, consider skip',
       !drRatio2 ? null : drFlag2==='OK']
    ];
    v3Rows.forEach(function(row) {
      var ok = row[3];
      var bg3 = ok===null?'#111111':ok?'#0a1a0a':'#1a0a0a';
      var fg3 = ok===null?'#607d8b':ok?'#00c853':'#ff5252';
      cell(sheet, r,1, row[0], {bg:bg3, fg:'#7a8899', sz:9, bold:false, h:28});
      cell(sheet, r,2, row[1], {bg:bg3, fg:'#e0e0e0', sz:12, bold:true});
      sheet.getRange(r,3,1,2).merge().setValue(row[2]||'—')
        .setBackground(bg3).setFontColor(fg3).setFontSize(10).setFontWeight('bold')
        .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
      cell(sheet, r,5, ok===null?'INFO':ok?'PASS':'SKIP', {bg:bg3, fg:fg3, sz:10, bold:true}); r++;
    });
    r++;
  }

  // ── V13 Technical Filter Status ───────────────────────────────────────────
  if (tech) {
    mrow(sheet, r, 5, 'V13 CONTEXT  (A/B tested — informational only, NOT entry gates)',
         {bg:'#1a1a2e', fg:'#505080', sz:9, bold:true, h:22}); r++;
    var dir = macro.dir;
    var filterRows = [
      ['MACD(12,26,9) histogram', tech.macdHist!==null?(Math.round(tech.macdHist*100)/100).toFixed(4):'—',
       tech.macdHist!==null?(dir===1?tech.macdHist>0:tech.macdHist<0)?'✓ aligned':'✗ against':null],
      ['RSI(14) 15m', tech.rsi15m!==null?Math.round(tech.rsi15m).toFixed(0):'—',
       tech.rsi15m!==null?(dir===1?tech.rsi15m<55:tech.rsi15m>45)?'✓ pullback zone':'✗ outside zone':null],
      ['Volume vs 20-bar avg', tech.volRatio!==null?Math.round(tech.volRatio*100)+'%':'—',
       tech.volRatio!==null?tech.volRatio>=1.20?'✓ surge':'✗ low vol':null],
      ['OBV vs OBV EMA10', tech.obvAbove!==null?(tech.obvAbove?'Above':'Below'):'—',
       tech.obvAbove!==null?(dir===1?tech.obvAbove===true:tech.obvAbove===false)?'✓ aligned':'✗ diverging':null]
    ];
    filterRows.forEach(function(row) {
      var ok = row[2] === null ? null : row[2].charAt(0) === '✓';
      var bg3 = ok===null?'#111111':ok?'#0a1a0a':'#1a0a0a';
      var fg3 = ok===null?'#607d8b':ok?'#00c853':'#ff5252';
      cell(sheet, r,1, row[0], {bg:bg3, fg:'#7a8899', sz:9, bold:false, h:28});
      cell(sheet, r,2, row[1], {bg:bg3, fg:'#e0e0e0', sz:12, bold:true});
      sheet.getRange(r,3,1,2).merge().setValue(row[2]||'—')
        .setBackground(bg3).setFontColor(fg3).setFontSize(10).setFontWeight('bold')
        .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
      cell(sheet, r,5, ok===null?'—':ok?'PASS':'FAIL', {bg:bg3, fg:fg3, sz:10, bold:true}); r++;
    });
    r++;

    // ── Key Levels ──────────────────────────────────────────────────────────
    mrow(sheet, r, 5, 'KEY LEVELS', {bg:'#1a1a2e', fg:'#6060a0', sz:9, bold:true, h:22}); r++;
    var dir2 = macro.dir;
    var lvls = [
      ['Session VWAP',   '$'+fix(tech.vwap),   tech.vwapBars+' bars since 17:30 Dubai'],
      ['+1σ band (short entry zone)', '$'+fix(tech.vwap+tech.vwapStd), 'Price range for SHORT entries'],
      ['−1σ band (long entry zone)',  '$'+fix(tech.vwap-tech.vwapStd), 'Price range for LONG entries'],
      ['4H EMA50',       '$'+fix(tech.h4ema50), 'Short gate — only short BELOW this'],
      ['1H EMA9',        '$'+fix(tech.h1ema9),  '1H structural alignment'],
      ['1H EMA21',       '$'+fix(tech.h1ema21), '1H structural alignment'],
      ['15m EMA9',       '$'+fix(tech.ema9),    '15m entry filter'],
      ['15m EMA21',      '$'+fix(tech.ema21),   '15m entry filter']
    ];
    lvls.forEach(function(lv) {
      cell(sheet, r,1, lv[0], {bg:'#111128', fg:'#808090', sz:9, bold:true, h:26});
      cell(sheet, r,2, lv[1], {bg:'#111128', fg:'#e0e0e0', sz:11, bold:true});
      sheet.getRange(r,3,1,3).merge().setValue(lv[2])
        .setBackground('#111128').setFontColor('#505060').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left');
      r++;
    });
    r++;
  }

  // ── Daily Schedule ────────────────────────────────────────────────────────
  mrow(sheet, r, 5, '⏰  DAILY SCHEDULE (Dubai)', {bg:'#1a1a2e', fg:'#6060a0', sz:9, bold:true, h:22}); r++;
  var now2 = new Date();
  var schedule = [
    ['17:15 DXB', 'Pre-session',  '#1a1a0a', 'Review macro score · set price alerts near VWAP zone · identify long or short bias'],
    ['17:30 DXB', 'Session open', '#0a0a1a', 'VWAP starts building from 13:30 UTC — do not trade yet (chop zone)'],
    ['18:00 DXB', '▶ ENTRY OPEN','#0a2a14', 'Entry window opens — all 6 gates must pass · watch for VWAP pullback'],
    ['19:00 DXB', 'Mid-session',  '#0a1628', 'Check open positions · if TP1 hit → close 60%, move SL to breakeven'],
    ['20:30 DXB', 'Entry closes', '#1a0a0a', 'No new entries after this — manage existing positions only'],
    ['21:00 DXB', 'Session ends', '#0a0a0a', 'Close ALL open trades — no overnight holds — record P&L']
  ];
  schedule.forEach(function(row) {
    cell(sheet, r,1, row[0], {bg:row[2], fg:row[2]==='#0a2a14'?'#00e676':'#c0ccd6', sz:10, bold:true, h:28});
    cell(sheet, r,2, row[1], {bg:row[2], fg:'#aabbcc', sz:9});
    sheet.getRange(r,3,1,3).merge().setValue(row[3])
      .setBackground(row[2]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ── Footer ────────────────────────────────────────────────────────────────
  r++;
  mrow(sheet, r, 5,
       'VALIDATED GATES (6): session window · 4H EMA · 15m EMA · VWAP ±1σ · VIX<30 · not-June  |  +4H EMA50 short gate  |  ATR daily range: INFO only (p=0.083)  |  MACD/RSI/Vol/OBV: context only (A/B tested — no improvement)',
       {bg:'#080810', fg:'#282840', sz:8, h:22}); r++;
  mrow(sheet, r, 5,
       'Risk: '+RISK_PCT*100+'% ($'+Math.round(ACCOUNT*RISK_PCT)+') · SL=0.75×ATR · TP1=1.5×ATR (60%) · TP2=2.5×ATR (40%) · Time stop 90min · Max 2 trades/session',
       {bg:'#080810', fg:'#282840', sz:8, h:22});

  SpreadsheetApp.flush();
}

// ── MATH HELPERS ─────────────────────────────────────────────────────────────
function emaArr(arr, n) {
  var k=2/(n+1), out=[arr[0]];
  for (var i=1; i<arr.length; i++) out.push(arr[i]*k + out[i-1]*(1-k));
  return out;
}

function calcAtr14(bars) {
  var tr = bars.map(function(b,i) {
    if (i===0) return b.h-b.l;
    return Math.max(b.h-b.l, Math.abs(b.h-bars[i-1].c), Math.abs(b.l-bars[i-1].c));
  });
  var e = emaArr(tr, 14);
  return e[e.length-1];
}

function calcMacdHistogram(closes) {
  if (closes.length < 35) return null;
  var e12 = emaArr(closes,12), e26 = emaArr(closes,26);
  var macd = e12.map(function(v,i){ return v-e26[i]; });
  var sig  = emaArr(macd, 9);
  return macd[macd.length-1] - sig[sig.length-1];
}

function calcRsi14(closes) {
  if (closes.length < 16) return null;
  var g=[], l=[];
  for (var i=1; i<closes.length; i++) {
    var d=closes[i]-closes[i-1]; g.push(d>0?d:0); l.push(d<0?-d:0);
  }
  var k=1/14, ag=g[0], al=l[0];
  for (var j=1; j<g.length; j++) { ag=ag*(1-k)+g[j]*k; al=al*(1-k)+l[j]*k; }
  return al===0?100:100-100/(1+ag/al);
}

function calcObvAboveEma(closes, volumes) {
  if (!closes || closes.length < 12 || closes.length !== volumes.length) return null;
  var obv=[0];
  for (var i=1; i<closes.length; i++) {
    var sign=closes[i]>closes[i-1]?1:closes[i]<closes[i-1]?-1:0;
    obv.push(obv[i-1]+sign*volumes[i]);
  }
  var e = emaArr(obv, 10);
  return obv[obv.length-1] > e[e.length-1];
}

function resampleTo4H(bars1h) {
  var out=[], cur=null;
  (bars1h||[]).forEach(function(b) {
    var dt = new Date(b.dt.replace(' ','T')+'Z');
    var slot=Math.floor(dt.getUTCHours()/4)*4;
    var key=dt.getUTCFullYear()+'-'+dt.getUTCMonth()+'-'+dt.getUTCDate()+'-'+slot;
    if (!cur||cur.key!==key){ if(cur)out.push(cur); cur={key:key,o:b.o,h:b.h,l:b.l,c:b.c}; }
    else { cur.h=Math.max(cur.h,b.h); cur.l=Math.min(cur.l,b.l); cur.c=b.c; }
  });
  if(cur)out.push(cur); return out;
}

function fix(n) { return n!=null?(Math.round(n*100)/100).toFixed(2):'—'; }

function cell(sheet, row, col, val, opts) {
  var o=opts||{};
  sheet.getRange(row,col).setValue(val)
    .setBackground(o.bg||'#0d0d20').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if(o.h) sheet.setRowHeight(row,o.h);
}

function mrow(sheet, row, cols, val, opts) {
  var o=opts||{};
  sheet.getRange(row,1,1,cols).merge().setValue(val)
    .setBackground(o.bg||'#0d0d20').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(row, o.h||32);
}
