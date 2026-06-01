/**
 * FTMO PORTFOLIO SIGNAL DASHBOARD  V15
 * =====================================
 * Pairs : CAD/CHF (L+S) · AUD/JPY (LONG) · CAD/JPY (LONG) · EUR/CAD (L+S) · AUD/NZD (L+S) · CHF/JPY (L+S)
 * All TREND (VWAP pullback) · No ORB · Strict V13 filters · Triple-layer floor protection
 *
 * V15 Backtest (Sep 2024 – May 2026):
 *   $120k → $451k (+276.5% ROI) | Sharpe 2.59 | Max DD 13.6% | 9/20 passes (45%) | p=0.0006 ✓
 *
 * Risk tiers  CAD/CHF 4% WR82% · AUD/JPY 3% WR77% · CAD/JPY 2% WR55%
 *             EUR/CAD 3% WR41% · CHF/JPY 2% WR39% · AUD/NZD 1.5% WR45%
 *
 * Triple-layer floor protection:
 *   Layer 1 (equity vs $120k): ≤97%→0.75× · ≤95%→0.55× · ≤93%→0.35× · ≤91%→0.25×
 *   Layer 2 (month progress):  ≥9.5%→0.30× · ≥10%→0.20× lock
 *   Layer 3: pre-trade check — skip if worst-case month P&L < −9.9%
 *
 * Sessions (Dubai UTC+4):
 *   LONDON  11:30–14:00 DXB = 07:30–10:00 UTC
 *   NY      18:00–21:00 DXB = 14:00–17:00 UTC
 *
 * SETUP:
 *   1. sheets.new → Extensions → Apps Script → paste → Save
 *   2. Run setupTrigger() once (authorize)
 *   3. Run updateDashboard() immediately
 *   4. Daily: G2 = current MT5 equity · G3 = day-start equity · G4 = month-start equity
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var TWELVE_KEY  = "06f050cd7d9940a895f9461e7f0ff7e3";
var ACCOUNT     = 120000;
var FTMO_FLOOR  = 108000;
var FTMO_TARGET = 132000;
var FTMO_DAILY  = 6000;
var DAILY_CAP_PCT = 0.050;   // 5% daily risk cap

// V15 risk tiers — sized by statistical confidence
var RISK_TIER = {
  'CADCHF': 0.040, 'AUDJPY': 0.030, 'CADJPY': 0.020,
  'EURCAD': 0.030, 'AUDNZD': 0.015, 'CHFJPY': 0.020
};

// Pip sizes: JPY pairs 0.01, others 0.0001
var PIP_SIZE = {
  'CADCHF': 0.0001, 'AUDJPY': 0.01, 'CADJPY': 0.01,
  'EURCAD': 0.0001, 'AUDNZD': 0.0001, 'CHFJPY': 0.01
};

// Pip value per standard lot (~USD) — verify in MT5 Market Watch
var PIP_VALUE = {
  'CADCHF': 8.50,  // 10 CHF ÷ USDCHF ≈ $8.50
  'AUDJPY': 6.70,  // 1000 JPY ÷ USDJPY ≈ $6.70
  'CADJPY': 6.70,
  'EURCAD': 7.25,  // 10 CAD ÷ USDCAD ≈ $7.25
  'AUDNZD': 6.00,  // 10 NZD × NZDUSD ≈ $6.00
  'CHFJPY': 6.70   // 1000 JPY ÷ USDJPY ≈ $6.70
};

// ATR validity range (atr/price ratio)
var ATR_RANGE = {
  'CADCHF': [0.0002, 0.0080], 'AUDJPY': [0.0004, 0.0120], 'CADJPY': [0.0004, 0.0120],
  'EURCAD': [0.0003, 0.0100], 'AUDNZD': [0.0001, 0.0050], 'CHFJPY': [0.0003, 0.0120]
};

// LONG ONLY pairs (carry trade — never short)
var LONG_ONLY = { 'AUDJPY': true, 'CADJPY': true };

// SL / TP multipliers
var SL_MULT  = 0.75;
var TP1_MULT = 1.50;
var TP2_MULT = 2.50;
var TP1_FRAC = 0.60;
var TP2_FRAC = 0.40;

// Sessions (UTC decimal hours)
var SESS_LONDON = { vwap: 7.0, open: 7.5,  close: 10.0 };
var SESS_NY     = { vwap: 13.5, open: 14.0, close: 17.0 };
var DUBAI_OFFSET_H = 4;

// Triple-layer floor protection thresholds
var CHAL_PROTECT_AT  = 10.0;
var CHAL_CAUTIOUS_AT = 9.5;

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ V15 Dashboard — Auto-refresh every 5 minutes.\n\n' +
      'Run updateDashboard() to load now.\n\n' +
      'Update these cells each trading day:\n' +
      '  G2 = current MT5 equity (update whenever you open sheet)\n' +
      '  G3 = equity at START of today\n' +
      '  G4 = equity at START of this month (FTMO challenge start)\n\n' +
      'Dubai check times:\n' +
      '  11:15 DXB — Pre-London check (most important)\n' +
      '  11:30 DXB — London open, TREND entries begin\n' +
      '  18:00 DXB — NY open, TREND entries resume\n' +
      '  21:00 DXB — NY close, manage open trades'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('PORTFOLIO SIGNAL') || ss.insertSheet('PORTFOLIO SIGNAL');

  var now    = new Date();
  var nowUtc = now.getUTCHours() + now.getUTCMinutes() / 60;

  // User-editable equity inputs
  var curEq      = dash.getRange('G2').getValue() || ACCOUNT;
  var dayStartEq = dash.getRange('G3').getValue() || ACCOUNT;
  var monStartEq = dash.getRange('G4').getValue() || ACCOUNT;

  var macroData = fetchAllMacro();
  var regime    = calcRegime(macroData);
  var chalScale = calcChallengeScale(curEq, monStartEq);

  var pairs   = ['CADCHF','AUDJPY','CADJPY','EURCAD','AUDNZD','CHFJPY'];
  var results = {};

  pairs.forEach(function(p, idx) {
    if (idx > 0) Utilities.sleep(4000);
    var sym = tdSymbol(p);
    var bars15 = fetchBars(sym, '15min', 80);
    Utilities.sleep(4000);
    var bars1h = fetchBars(sym, '1h', 260);

    var macro = calcMacroForPair(p, macroData);
    var tech  = buildTechnicals(p, bars15, bars1h, nowUtc);
    var setup = evalSetup(p, macro, tech, nowUtc, macroData, regime);
    var trade = (setup.active)
      ? calcTrade(p, setup, tech, curEq, monStartEq, regime, chalScale)
      : null;
    results[p] = { macro: macro, tech: tech, setup: setup, trade: trade };
  });

  var ftmo = calcFtmo(curEq, dayStartEq, monStartEq);
  writeSheet(dash, now, nowUtc, results, ftmo, curEq, dayStartEq, monStartEq, regime, chalScale);
}

// ── SYMBOL MAP ────────────────────────────────────────────────────────────────
function tdSymbol(p) {
  var m = {
    'CADCHF':'CAD/CHF','AUDJPY':'AUD/JPY','CADJPY':'CAD/JPY',
    'EURCAD':'EUR/CAD','AUDNZD':'AUD/NZD','CHFJPY':'CHF/JPY'
  };
  return m[p];
}

// ── FETCH BARS (Twelve Data) ──────────────────────────────────────────────────
function fetchBars(symbol, interval, n) {
  try {
    var url = 'https://api.twelvedata.com/time_series?symbol=' +
              encodeURIComponent(symbol) + '&interval=' + interval +
              '&outputsize=' + n + '&apikey=' + TWELVE_KEY + '&timezone=UTC';
    var r = UrlFetchApp.fetch(url, {muteHttpExceptions: true});
    var j = JSON.parse(r.getContentText());
    if (!j.values || j.status === 'error') return [];
    return j.values.map(function(v) {
      return { dt: v.datetime, o: +v.open, h: +v.high, l: +v.low, c: +v.close, v: +v.volume || 0 };
    }).reverse();
  } catch(e) { return []; }
}

// ── FETCH ALL MACRO (Yahoo Finance, cached 6h) ────────────────────────────────
function fetchAllMacro() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key    = 'v15_macro_' + today;
  var cached = cache.get(key);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yf(sym, range) {
    try {
      var url = 'https://query1.finance.yahoo.com/v8/finance/chart/' +
                encodeURIComponent(sym) + '?interval=1d&range=' + range;
      var r = UrlFetchApp.fetch(url, { muteHttpExceptions: true, headers: {'User-Agent':'Mozilla/5.0'} });
      var j = JSON.parse(r.getContentText());
      return j.chart.result[0].indicators.quote[0].close.filter(function(c){ return c != null; });
    } catch(e) { return null; }
  }

  var data = {
    // Pair prices (for RORO score s1/s5)
    cadchf: yf('CADCHF=X', '2y'), audjpy: yf('AUDJPY=X', '2y'), cadjpy: yf('CADJPY=X', '2y'),
    eurcad: yf('EURCAD=X', '2y'), audnzd: yf('AUDNZD=X', '2y'), chfjpy: yf('CHFJPY=X', '2y'),
    // Macro drivers
    oil:    yf('CL=F',   '1y'),
    cop:    yf('HG=F',   '1y'),
    spx:    yf('^GSPC',  '1y'),
    vix:    yf('^VIX',   '6mo'),
    tnx:    yf('^TNX',   '1y'),
    usdjpy: yf('JPY=X',  '1y')
  };

  cache.put(key, JSON.stringify(data), 21600);
  return data;
}

// ── REGIME ENGINE ─────────────────────────────────────────────────────────────
function calcRegime(d) {
  var oilWeak = false, vixSpike = false, copBull = false, spxBull = false;
  var oilNote = '—', vixNote = '—', copNote = '—', spxNote = '—';
  var oilPrice = null, oilEma = null;

  if (d.oil && d.oil.length >= 52) {
    var oe = calcEma(d.oil, 50); var om = d.oil.length;
    oilPrice = d.oil[om-2]; oilEma = oe[om-2];
    oilWeak = oilPrice < oilEma;
    oilNote = 'Oil $' + oilPrice.toFixed(1) + ' vs EMA50 $' + oilEma.toFixed(1) +
              (oilWeak ? ' → WEAK ↓' : ' → OK ↑');
  }
  if (d.vix && d.vix.length >= 22) {
    var ve = calcEma(d.vix, 20); var vm = d.vix.length;
    vixSpike = d.vix[vm-2] > ve[vm-2] * 1.10;
    vixNote = 'VIX ' + d.vix[vm-2].toFixed(1) + ' vs EMA20×1.1 ' + (ve[vm-2]*1.10).toFixed(1) +
              (vixSpike ? ' → SPIKE ⚠' : ' → calm ✓');
  }
  if (d.cop && d.cop.length >= 52) {
    var ce = calcEma(d.cop, 50); var cm = d.cop.length;
    copBull = d.cop[cm-2] > ce[cm-2];
    copNote = 'Copper $' + d.cop[cm-2].toFixed(2) + ' vs EMA50 $' + ce[cm-2].toFixed(2) + (copBull?' ↑':' ↓');
  }
  if (d.spx && d.spx.length >= 52) {
    var se = calcEma(d.spx, 50); var sm = d.spx.length;
    spxBull = d.spx[sm-2] > se[sm-2];
    spxNote = 'SPX ' + Math.round(d.spx[sm-2]) + ' vs EMA50 ' + Math.round(se[sm-2]) + (spxBull?' ↑':' ↓');
  }

  var bull = !oilWeak && !vixSpike && copBull && spxBull;
  var components = [
    { name:'WTI Oil vs EMA50',    note:oilNote, ok:!oilWeak },
    { name:'VIX vs EMA20×1.1',   note:vixNote, ok:!vixSpike },
    { name:'Copper vs EMA50',     note:copNote, ok:copBull },
    { name:'S&P 500 vs EMA50',    note:spxNote, ok:spxBull }
  ];

  if (vixSpike) return {
    label:'VIX_SPIKE', color:'#ff5252', bg:'#2a0a0a',
    description:'VIX surging — AUD/JPY · CAD/JPY · CHF/JPY SKIPPED — size 0.50×',
    sizeAdj:0.50, skipPairs:{ AUDJPY:true, CADJPY:true, CHFJPY:true },
    cadchfShortThresh:-6, oilPrice:oilPrice, oilEma:oilEma, components:components
  };
  if (oilWeak) return {
    label:'OIL_WEAK', color:'#ff9800', bg:'#2a1a00',
    description:'Oil below EMA50 — CAD/JPY SKIPPED — EUR/CAD LONG signal strengthens — size 0.75×',
    sizeAdj:0.75, skipPairs:{ CADJPY:true },
    cadchfShortThresh:-4, oilPrice:oilPrice, oilEma:oilEma, components:components
  };
  if (bull) return {
    label:'BULL', color:'#00e676', bg:'#0a2a14',
    description:'Full bull — all 6 pairs active — size 1.0×',
    sizeAdj:1.0, skipPairs:{}, cadchfShortThresh:-6,
    oilPrice:oilPrice, oilEma:oilEma, components:components
  };
  return {
    label:'NORMAL', color:'#00bcd4', bg:'#0a1628',
    description:'Normal regime — all 6 pairs active — size 1.0×',
    sizeAdj:1.0, skipPairs:{}, cadchfShortThresh:-6,
    oilPrice:oilPrice, oilEma:oilEma, components:components
  };
}

// ── TRIPLE-LAYER FLOOR PROTECTION ─────────────────────────────────────────────
function calcChallengeScale(curEq, monStartEq) {
  // Layer 1: absolute equity vs $120k
  var eqPct = curEq / ACCOUNT;
  var absScale;
  if      (eqPct <= 0.91) absScale = 0.25;
  else if (eqPct <= 0.93) absScale = 0.35;
  else if (eqPct <= 0.95) absScale = 0.55;
  else if (eqPct <= 0.97) absScale = 0.75;
  else                     absScale = 1.0;

  // Layer 2: month P&L progress
  var monPnlPct = monStartEq > 0 ? (curEq - monStartEq) / monStartEq * 100 : 0;
  var monScale;
  if      (monPnlPct >= CHAL_PROTECT_AT)  monScale = 0.20;
  else if (monPnlPct >= CHAL_CAUTIOUS_AT) monScale = 0.30;
  else                                     monScale = 1.0;

  var scale = Math.min(absScale, monScale);
  var reason = [];
  if (absScale < 1.0) reason.push('equity at ' + (eqPct*100).toFixed(1) + '% → ' + (absScale*100).toFixed(0) + '×');
  if (monScale < 1.0) reason.push('month +' + monPnlPct.toFixed(1) + '% → ' + (monScale*100).toFixed(0) + '×');
  if (reason.length === 0) reason.push('full size (no floor protection active)');

  return {
    scale:     scale,
    absScale:  absScale,
    monScale:  monScale,
    monPnlPct: monPnlPct,
    eqPct:     eqPct,
    reason:    reason.join(' · ')
  };
}

// ── MACRO SCORE PER PAIR ──────────────────────────────────────────────────────
function calcMacroForPair(pairKey, d) {
  var pairPrices = d[pairKey.toLowerCase()];
  if (!pairPrices || pairPrices.length < 60)
    return { score:0, dir:0, gated:false, components:[], error:'Insufficient data' };

  var n = pairPrices.length, p = pairPrices;
  var e20  = calcEma(p, 20);
  var e50  = calcEma(p, 50);
  var e200 = calcEma(p, 200);

  var s1   = p[n-2] > e50[n-2] ? 1 : -1;
  var s5   = e200.length >= 200 ? (p[n-2] > e200[n-2] ? 1 : -1) : 0;
  var gate = p[n-2] > e20[n-2] ? 1 : -1;

  var s2=0, s3=0, s4=0;
  var s2note='—', s3note='—', s4note='—';
  var s2name='—', s3name='—', s4name='—';

  function emaComp(arr, n_ema, label, inv) {
    if (!arr || arr.length < n_ema+2) return { val:0, note:'No data', name:label };
    var e = calcEma(arr, n_ema); var m = arr.length;
    var v = arr[m-2] > e[m-2] ? 1 : -1;
    if (inv) v = -v;
    return { val:v, note:arr[m-2].toFixed(arr[m-2]>100?1:2) + ' vs EMA'+n_ema+' ' + e[m-2].toFixed(arr[m-2]>100?1:2), name:label };
  }
  function momComp(arr, shift, label) {
    if (!arr || arr.length < shift+2) return { val:0, note:'No data', name:label };
    var m = arr.length;
    var v = arr[m-2] > arr[m-2-shift] ? 1 : -1;
    return { val:v, note:arr[m-2].toFixed(2)+'% vs '+shift+'d ago '+arr[m-2-shift].toFixed(2)+'%', name:label };
  }

  if (pairKey === 'CADCHF') {
    var c2 = emaComp(d.oil, 50, 'WTI Oil vs EMA50 (CAD+)', false);
    var c3 = emaComp(d.spx, 50, 'S&P 500 vs EMA50 (risk-on→CHF weak)', false);
    var c4 = emaComp(d.vix, 20, 'VIX vs EMA20 inverted (low VIX→CHF weak)', true);
    s2=c2.val; s3=c3.val; s4=c4.val; s2note=c2.note; s3note=c3.note; s4note=c4.note;
    s2name=c2.name; s3name=c3.name; s4name=c4.name;

  } else if (pairKey === 'AUDJPY') {
    var c2 = emaComp(d.cop, 50, 'Copper vs EMA50 (AUD+)', false);
    var c3 = emaComp(d.spx, 50, 'S&P 500 vs EMA50 (risk-on→JPY weak)', false);
    var c4 = momComp(d.tnx, 10, 'US 10Y yield 10d momentum (carry+)');
    s2=c2.val; s3=c3.val; s4=c4.val; s2note=c2.note; s3note=c3.note; s4note=c4.note;
    s2name=c2.name; s3name=c3.name; s4name=c4.name;

  } else if (pairKey === 'CADJPY') {
    var c2 = emaComp(d.oil, 50, 'WTI Oil vs EMA50 (CAD+)', false);
    var c3 = momComp(d.tnx, 10, 'US 10Y yield 10d momentum (carry+)');
    var c4 = emaComp(d.spx, 50, 'S&P 500 vs EMA50 (risk-on→JPY weak)', false);
    s2=c2.val; s3=c3.val; s4=c4.val; s2note=c2.note; s3note=c3.note; s4note=c4.note;
    s2name=c2.name; s3name=c3.name; s4name=c4.name;

  } else if (pairKey === 'EURCAD') {
    // INVERTED RORO: risk-on → EUR/CAD DOWN (CAD up) → score ≤−6 → SHORT
    // OIL_WEAK → CAD weak → EUR/CAD UP → score ≥+6 → LONG
    var c2 = emaComp(d.oil, 50, 'WTI Oil inverted (oil↑ → CAD↑ → EUR/CAD↓)', true);
    var c3 = emaComp(d.spx, 50, 'S&P 500 inverted (risk-on → EUR/CAD↓)', true);
    var c4 = emaComp(d.vix, 20, 'VIX vs EMA20 (VIX↑ → EUR safe haven → EUR/CAD↑)', false);
    s2=c2.val; s3=c3.val; s4=c4.val; s2note=c2.note; s3note=c3.note; s4note=c4.note;
    s2name=c2.name; s3name=c3.name; s4name=c4.name;

  } else if (pairKey === 'AUDNZD') {
    var c2 = emaComp(d.cop, 50, 'Copper vs EMA50 (AUD+ vs NZD)', false);
    var c3 = emaComp(d.oil, 50, 'WTI Oil vs EMA50 (AUD oil-exposed)', false);
    var c4 = emaComp(d.spx, 50, 'S&P 500 vs EMA50 (risk-on→AUD+)', false);
    s2=c2.val; s3=c3.val; s4=c4.val; s2note=c2.note; s3note=c3.note; s4note=c4.note;
    s2name=c2.name; s3name=c3.name; s4name=c4.name;

  } else if (pairKey === 'CHFJPY') {
    // Both safe havens; JPY weakens more in risk-on → CHF/JPY up
    var c2 = emaComp(d.spx, 50, 'S&P 500 vs EMA50 (risk-on→JPY weak>CHF)', false);
    var c3 = emaComp(d.vix, 20, 'VIX inverted (low VIX → JPY carry selling)', true);
    var c4 = momComp(d.tnx, 10, 'US 10Y yield 10d momentum (JPY funding−)');
    s2=c2.val; s3=c3.val; s4=c4.val; s2note=c2.note; s3note=c3.note; s4note=c4.note;
    s2name=c2.name; s3name=c3.name; s4name=c4.name;
  }

  var raw  = s1 + s2 + s3 + s4 + s5;
  var score = Math.round(raw / 5 * 100) / 10;
  var gated = (raw !== 0 && Math.sign(score) !== Math.sign(gate));
  var gatedScore = gated ? 0 : score;
  var dir  = gatedScore >= 6 ? 1 : gatedScore <= -6 ? -1 : 0;

  if (LONG_ONLY[pairKey] && dir === -1) dir = 0;

  var pl = (PIP_SIZE[pairKey] === 0.01) ? 3 : 5;
  return {
    score: gatedScore, rawScore: score, dir: dir, gated: gated, gate: gate,
    pairPrice: p[n-1], ema20: e20[n-2], ema50: e50[n-2],
    ema200: e200.length >= 200 ? e200[n-2] : null,
    components: [
      { name: pairKey.slice(0,3)+'/'+pairKey.slice(3)+' vs EMA50', val:s1,
        note: p[n-2].toFixed(pl)+' vs EMA50 '+e50[n-2].toFixed(pl)+(s1>0?' → uptrend':' → downtrend') },
      { name: s2name, val: s2, note: s2note },
      { name: s3name, val: s3, note: s3note },
      { name: s4name, val: s4, note: s4note },
      { name: pairKey.slice(0,3)+'/'+pairKey.slice(3)+' vs EMA200', val:s5,
        note: e200.length >= 200
          ? p[n-2].toFixed(pl)+' vs EMA200 '+e200[n-2].toFixed(pl)+(s5>0?' → bull':' → bear')
          : 'Insufficient history (need 200 days)' }
    ],
    error: null
  };
}

// ── JPY CARRY GUARD ───────────────────────────────────────────────────────────
function jpyCarryGuard(macroData) {
  var usdjpy = macroData.usdjpy;
  if (!usdjpy || usdjpy.length < 7) return { blocked:false, note:'No USDJPY data' };
  var n = usdjpy.length, chg = (usdjpy[n-1] - usdjpy[n-6]) / usdjpy[n-6];
  return {
    blocked: chg < -0.02,
    note: 'USD/JPY ' + (chg*100).toFixed(1) + '% over 5d' +
          (chg < -0.02 ? ' → JPY surging, carry BLOCKED ⚠' : ' → JPY stable ✓')
  };
}

// ── BUILD TECHNICALS ──────────────────────────────────────────────────────────
function buildTechnicals(pairKey, bars15, bars1h, nowUtcH) {
  if (!bars15 || bars15.length < 35) return null;
  var c15  = bars15.map(function(b){ return b.c; });
  var e9   = calcEma(c15, 9);
  var e21  = calcEma(c15, 21);
  var atrV = calcAtr14(bars15);
  var last = bars15[bars15.length - 1];
  var price = last.c;

  // V13 technical filters
  var macdHist = calcMacdHistogram(c15);
  var rsi15m   = calcRsi14(c15);

  var vols15  = bars15.map(function(b){ return b.v || 0; });
  var volN    = Math.min(20, vols15.length - 1), volSum = 0;
  for (var vi = vols15.length - 1 - volN; vi < vols15.length - 1; vi++) volSum += vols15[vi];
  var avgVol  = volN > 0 ? volSum / volN : 0;
  var volRatio = avgVol > 0 ? vols15[vols15.length-1] / avgVol : null;
  var obvAboveEma = calcObvAboveEma(c15, vols15);

  var e9_1h=null, e21_1h=null, e50_1h=null, rsi1h=null, e9_4h=null, e21_4h=null;
  if (bars1h && bars1h.length >= 55) {
    var c1h = bars1h.map(function(b){ return b.c; });
    var ea9 = calcEma(c1h,9), ea21 = calcEma(c1h,21), ea50 = calcEma(c1h,50);
    e9_1h = ea9[ea9.length-1]; e21_1h = ea21[ea21.length-1]; e50_1h = ea50[ea50.length-1];
    rsi1h = calcRsi14(c1h);
    var b4h = resample4h(bars1h);
    if (b4h.length >= 22) {
      var c4h = b4h.map(function(b){ return b.c; });
      var f9 = calcEma(c4h,9), f21 = calcEma(c4h,21);
      e9_4h = f9[f9.length-1]; e21_4h = f21[f21.length-1];
    }
  }

  var activeVwapH = nowUtcH >= SESS_NY.vwap ? SESS_NY.vwap : SESS_LONDON.vwap;
  var sv = calcSessionVwap(bars15, activeVwapH);

  return {
    price:price, high:last.h, low:last.l,
    ema9:e9[e9.length-1], ema21:e21[e21.length-1],
    atr:atrV, atrPct:atrV/price, atrPips:atrV/PIP_SIZE[pairKey],
    e9_1h:e9_1h, e21_1h:e21_1h, e50_1h:e50_1h,
    e9_4h:e9_4h, e21_4h:e21_4h, rsi:rsi1h,
    vwap:sv.vwap, vwapStd:sv.std, vwapBars:sv.n,
    vwapDist: sv.std > 1e-8 && sv.vwap ? (price - sv.vwap) / sv.std : 99,
    macdHist:macdHist, rsi15m:rsi15m,
    volRatio:volRatio, avgVol:avgVol, lastVol:vols15[vols15.length-1],
    obvAboveEma:obvAboveEma
  };
}

// ── SESSION VWAP ──────────────────────────────────────────────────────────────
function calcSessionVwap(bars15, vwapStartH) {
  var now  = new Date();
  var ymd  = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var hh   = pad(Math.floor(vwapStartH));
  var mm   = pad(Math.round((vwapStartH % 1) * 60));
  var open = new Date(ymd + 'T' + hh + ':' + mm + ':00Z');
  var todayPrefix = ymd + ' ';
  var sess = bars15.filter(function(b) {
    var barDate = new Date(b.dt.replace(' ','T') + 'Z');
    return barDate >= open && b.dt.indexOf(todayPrefix) === 0;
  });
  if (sess.length < 2) return { vwap:0, std:1e-5, n:0 };
  var tp = sess.map(function(b){ return (b.h+b.l+b.c)/3; });
  var avg = tp.reduce(function(a,v){ return a+v; },0) / tp.length;
  var v2  = tp.reduce(function(a,x){ return a+Math.pow(x-avg,2); },0) / tp.length;
  return { vwap:avg, std:Math.max(Math.sqrt(v2),1e-5), n:tp.length };
}

// ── EVAL SETUP PER PAIR ───────────────────────────────────────────────────────
function evalSetup(pairKey, macro, tech, nowUtcH, macroData, regime) {
  if (regime && regime.skipPairs && regime.skipPairs[pairKey])
    return { active:false, dir:null,
             reason:'SKIPPED — ' + regime.label + ' regime ('+pairKey+' excluded)', checks:[] };

  var shortThresh = (regime && regime.cadchfShortThresh) ? regime.cadchfShortThresh : -6;
  var adjustedDir = macro.dir;
  if (pairKey === 'CADCHF' && macro.score <= shortThresh && macro.score > -6) adjustedDir = -1;

  if (!tech || macro.error || (macro.dir === 0 && adjustedDir === 0))
    return { active:false, dir:null,
             reason: macro.error || (macro.gated
               ? 'EMA20 gate: pair on wrong side of trend (score=' + (macro.score||0).toFixed(1) + ')'
               : 'No macro signal  |  score=' + (macro.score||0).toFixed(1) + ' (need ≥6 or ≤−6)'),
             checks:[] };

  var dir = adjustedDir || macro.dir;
  var price = tech.price;
  var isJpy = !!LONG_ONLY[pairKey];

  var inEntry = false, sessName = '';
  if (nowUtcH >= SESS_LONDON.open && nowUtcH <= SESS_LONDON.close) {
    inEntry = true; sessName = 'LONDON';
  } else if (nowUtcH >= SESS_NY.open && nowUtcH <= SESS_NY.close) {
    var now2 = new Date();
    var isNfp = now2.getUTCDay() === 5 && now2.getUTCDate() <= 7;
    var vixNow = (macroData.vix && macroData.vix.length) ? macroData.vix[macroData.vix.length-1] : 0;
    var isVixWed = now2.getUTCDay() === 3 && vixNow > 25;
    if (!isNfp && !isVixWed) { inEntry = true; sessName = 'NY'; }
  }

  var h4ok  = (tech.e9_4h && tech.e21_4h) ? (dir===1 ? tech.e9_4h>tech.e21_4h : tech.e9_4h<tech.e21_4h) : null;
  var h1ok  = (tech.e9_1h && tech.e21_1h) ? (dir===1 ? tech.e9_1h>tech.e21_1h : tech.e9_1h<tech.e21_1h) : null;
  var e50ok = tech.e50_1h ? (dir===1 ? price>tech.e50_1h : price<tech.e50_1h) : null;
  var m15ok = dir===1 ? tech.ema9>tech.ema21 : tech.ema9<tech.ema21;
  var vwapOk = tech.vwapBars >= 4 &&
    (dir===1 ? (tech.vwapDist >= -1.0 && tech.vwapDist <= 0.3)
             : (tech.vwapDist >= -0.3 && tech.vwapDist <= 1.0));
  var rsiOk  = tech.rsi === null ? true : (dir===1 ? tech.rsi <= 70 : tech.rsi >= 30);
  var atrOk  = ATR_RANGE[pairKey] && tech.atrPct >= ATR_RANGE[pairKey][0] && tech.atrPct <= ATR_RANGE[pairKey][1];

  var carryGuard = isJpy && dir === 1 ? jpyCarryGuard(macroData) : null;
  var carryOk = carryGuard ? !carryGuard.blocked : true;

  var dp = PIP_SIZE[pairKey], places = dp === 0.01 ? 3 : 5;

  var macdOk = tech.macdHist !== null
    ? (dir===1 ? tech.macdHist>0 : tech.macdHist<0) : true;
  var rsi15Ok = tech.rsi15m !== null
    ? (dir===1 ? tech.rsi15m<55 : tech.rsi15m>45) : true;
  var volOk  = tech.volRatio !== null ? tech.volRatio >= 1.20 : true;
  var obvOk  = tech.obvAboveEma !== null
    ? (dir===1 ? tech.obvAboveEma===true : tech.obvAboveEma===false) : true;

  var checks = [
    { label:'Entry window',      ok:inEntry,
      note: inEntry ? sessName+' session active' : 'Outside windows — London 11:30–14:00 DXB · NY 18:00–21:00 DXB' },
    { label:'4H EMA9 > EMA21',   ok: h4ok!==null?h4ok:true,
      note: h4ok===null?'No 4H data': h4ok?'EMA9 '+(tech.e9_4h||0).toFixed(places)+' aligned':'misaligned' },
    { label:'1H EMA9 > EMA21',   ok: h1ok!==null?h1ok:true,
      note: h1ok===null?'No 1H data': h1ok?'EMA9 '+(tech.e9_1h||0).toFixed(places)+' aligned':'misaligned' },
    { label:'1H price vs EMA50', ok: e50ok!==null?e50ok:true,
      note: e50ok===null?'No data': e50ok
        ?'Price '+price.toFixed(places)+(dir===1?' > ':' < ')+'EMA50 '+(tech.e50_1h||0).toFixed(places)
        :'Price on wrong side of 1H EMA50 '+(tech.e50_1h||0).toFixed(places) },
    { label:'15m EMA9 > EMA21',  ok:m15ok,
      note: m15ok?'Aligned':'EMA9 '+tech.ema9.toFixed(places)+' / EMA21 '+tech.ema21.toFixed(places)+' misaligned' },
    { label:'VWAP pullback zone (≥4 bars)', ok:vwapOk,
      note: vwapOk
        ? 'dist='+tech.vwapDist.toFixed(2)+'σ ✓  VWAP='+tech.vwap.toFixed(places)
        : tech.vwapBars<4 ? 'Only '+tech.vwapBars+' VWAP bars'
          : 'dist='+tech.vwapDist.toFixed(2)+'σ — outside zone [need −1.0 to +0.3σ long / −0.3 to +1.0σ short]' },
    { label:'1H RSI(14)', ok:rsiOk,
      note: tech.rsi===null?'—':rsiOk?'RSI '+(tech.rsi||0).toFixed(0)+(dir===1?' ≤70 ✓':' ≥30 ✓')
              :'RSI '+(tech.rsi||0).toFixed(0)+(dir===1?' overbought ≥70 ✗':' oversold ≤30 ✗') },
    { label:'ATR in valid range', ok:atrOk,
      note:'ATR='+Math.round(tech.atrPips)+'p ('+  (tech.atrPct*100).toFixed(3)+'%)' + (atrOk?' ✓':' — outside range') },
    { label:'MACD(12,26,9) histogram '+( dir===1?'>0':'<0'), ok:macdOk,
      note: tech.macdHist!==null?'hist='+(tech.macdHist||0).toFixed(6)+(macdOk?' ✓':' ✗'):'insufficient data' },
    { label:'RSI(14) 15m '+(dir===1?'< 55 (buy pullback)':'> 45 (short pullback)'), ok:rsi15Ok,
      note: tech.rsi15m!==null?'RSI15m='+(tech.rsi15m||0).toFixed(1)+(rsi15Ok?' ✓':' ✗'):'insufficient data' },
    { label:'Volume ≥ 120% of avg', ok:volOk,
      note: tech.volRatio!==null?(tech.volRatio*100).toFixed(0)+'% of avg'+(volOk?' ✓':' ✗ — low volume'):'no volume data' },
    { label:'OBV vs OBV EMA10', ok:obvOk,
      note: tech.obvAboveEma!==null?(tech.obvAboveEma?'OBV above':'OBV below')+' EMA10'+(obvOk?' ✓':' ✗'):'insufficient data' }
  ];

  if (carryGuard) checks.push({ label:'JPY carry guard', ok:carryOk, note:carryGuard.note });

  var allOk = inEntry && (h4ok!==false) && (h1ok!==false) && (e50ok!==false)
              && m15ok && vwapOk && rsiOk && atrOk && carryOk
              && macdOk && rsi15Ok && volOk && obvOk;

  return { active:allOk, dir:dir, sessName:sessName, checks:checks };
}

// ── CALC TRADE ────────────────────────────────────────────────────────────────
function calcTrade(pairKey, setup, tech, curEq, monStartEq, regime, chalScale) {
  var dir    = setup.dir;
  var price  = tech.price;
  var atr    = tech.atr;
  var dp     = PIP_SIZE[pairKey];
  var places = dp === 0.01 ? 3 : 5;

  var slPts  = SL_MULT  * atr;
  var tp1Pts = TP1_MULT * atr;
  var tp2Pts = TP2_MULT * atr;

  var sl, tp1, tp2;
  if (dir === 1) { sl = price-slPts; tp1 = price+tp1Pts; tp2 = price+tp2Pts; }
  else           { sl = price+slPts; tp1 = price-tp1Pts; tp2 = price-tp2Pts; }

  var slPips  = Math.round(slPts  / dp);
  var tp1Pips = Math.round(tp1Pts / dp);
  var tp2Pips = Math.round(tp2Pts / dp);

  // Regime size multiplier
  var sizeAdj = regime && regime.sizeAdj ? regime.sizeAdj : 1.0;
  // Challenge scale (triple-layer floor protection)
  var scale   = chalScale ? chalScale.scale : 1.0;

  var riskPct = RISK_TIER[pairKey] || 0.020;
  var risk    = curEq * riskPct * sizeAdj * scale;

  // Layer 3: pre-trade worst-case floor check
  var worstCaseMonPnl = monStartEq > 0
    ? (curEq - risk - monStartEq) / monStartEq * 100 : 0;
  if (worstCaseMonPnl < -9.9) return null;  // skip — would breach floor

  var pv   = PIP_VALUE[pairKey] || 7.0;
  var lots = slPips > 0 ? Math.max(0.01, Math.round(risk / (slPips * pv) * 100) / 100) : 0.01;
  var lots1 = Math.max(0.01, Math.round(lots * TP1_FRAC * 100) / 100);
  var lots2 = Math.max(0.01, Math.round(lots * TP2_FRAC * 100) / 100);

  return {
    dir: dir===1?'LONG':'SHORT', entry:price, sl:sl, tp1:tp1, tp2:tp2,
    slPips:slPips, tp1Pips:tp1Pips, tp2Pips:tp2Pips,
    rr1:(tp1Pips/slPips).toFixed(1)+':1', rr2:(tp2Pips/slPips).toFixed(1)+':1',
    lots:lots, lots1:lots1, lots2:lots2, risk:risk,
    riskPct:riskPct, sizeAdj:sizeAdj, scale:scale,
    atr:atr, atrPips:Math.round(atr/dp), places:places,
    worstCaseMonPnl:worstCaseMonPnl
  };
}

// ── FTMO TRACKER ──────────────────────────────────────────────────────────────
function calcFtmo(curEq, dayStartEq, monStartEq) {
  var dayPnl    = curEq - dayStartEq;
  var monPnl    = curEq - monStartEq;
  var monPnlPct = monStartEq > 0 ? monPnl / monStartEq * 100 : 0;
  var dailyLeft = FTMO_DAILY + dayPnl;
  var toFloor   = curEq - FTMO_FLOOR;
  var toTarget  = FTMO_TARGET - curEq;
  var pctDone   = Math.max(0, (curEq - ACCOUNT) / (FTMO_TARGET - ACCOUNT) * 100);
  return {
    curEq:curEq, dayPnl:dayPnl, monPnl:monPnl, monPnlPct:monPnlPct,
    dailyLeft:dailyLeft, toFloor:toFloor, toTarget:toTarget, pctDone:pctDone,
    floorBreached: curEq <= FTMO_FLOOR,
    dailyBreached: dailyLeft <= 0
  };
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, nowUtcH, results, ftmo, curEq, dayStartEq, monStartEq, regime, chalScale) {
  // Save equity inputs BEFORE clearing — sheet.clear() wipes them
  var savedG2 = sheet.getRange('G2').getValue();
  var savedG3 = sheet.getRange('G3').getValue();
  var savedG4 = sheet.getRange('G4').getValue();

  sheet.clear();
  [220, 95, 95, 95, 95, 95, 140].forEach(function(w,i){ sheet.setColumnWidth(i+1,w); });
  // Prevent auto-currency format on column G bleeding into price cells
  sheet.getRange(1, 7, 300, 1).setNumberFormat('@');

  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm \'DXB (UTC+4)\'');
  var utcStr   = Utilities.formatDate(now, 'UTC', 'HH:mm \'UTC\'');
  var r = 1;

  // ═══ TITLE ════════════════════════════════════════════════════════════════
  mr(sheet, r, 6, '  FTMO PORTFOLIO SIGNAL  V15  —  6 PAIRS  —  VWAP PULLBACK',
     {bg:'#0a1628', fg:'#00bcd4', sz:15, bold:true, h:44}); r++;
  mr(sheet, r, 6, dubaiStr + '  ·  ' + utcStr + '  ·  Auto-refresh 5 min  ·  Equity inputs: G2=now  G3=day-start  G4=month-start',
     {bg:'#060e1a', fg:'#334455', sz:10, h:24}); r++;

  // ═══ REGIME BAR ══════════════════════════════════════════════════════════
  r++;
  if (regime) {
    mr(sheet, r, 6, '▶  REGIME: ' + regime.label + '   —   ' + regime.description,
       {bg:regime.bg, fg:regime.color, sz:13, bold:true, h:36}); r++;
    regime.components.forEach(function(rc) {
      mr(sheet, r, 6, (rc.ok?'✓':'✗')+'  '+rc.name+'  —  '+rc.note,
         {bg:rc.ok?'#0a1a0a':'#1a0a0a', fg:rc.ok?'#00c853':'#ff5252', sz:9, h:22}); r++;
    });
    // Oil context for key pairs
    if (regime.oilPrice && regime.oilEma) {
      var oilMsg = 'Oil $' + regime.oilPrice.toFixed(2) + ' vs EMA50 $' + regime.oilEma.toFixed(2);
      if (regime.label === 'OIL_WEAK') {
        oilMsg += '  →  CAD/JPY SKIPPED · EUR/CAD LONG bias · CAD/CHF short threshold −4';
      } else {
        oilMsg += '  →  Oil above EMA50 — CAD pairs fully active';
      }
      mr(sheet, r, 6, oilMsg, {bg:'#0c1220', fg:'#607d8b', sz:9, h:20}); r++;
    }
  }

  // ═══ SESSION CLOCK ════════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, 'SESSION CLOCK  (Dubai UTC+4)', {bg:'#111e2e', fg:'#445577', sz:9, bold:true, h:22}); r++;

  var sessions = [
    { name:'LONDON TREND',     dxb:'11:30–14:00', utcO:7.5,  utcC:10.0, pairs:'CAD/CHF · AUD/JPY · CAD/JPY · EUR/CAD · AUD/NZD · CHF/JPY' },
    { name:'NY TREND',         dxb:'18:00–21:00', utcO:14.0, utcC:17.0, pairs:'CAD/CHF · AUD/JPY · CAD/JPY · EUR/CAD · AUD/NZD · CHF/JPY' },
    { name:'Pre-London check', dxb:'11:15–11:30', utcO:7.25, utcC:7.5,  pairs:'Review all signals · confirm RORO scores' },
    { name:'Pre-NY check',     dxb:'17:45–18:00', utcO:13.75, utcC:14.0, pairs:'Review all signals before NY session' }
  ];
  sessions.forEach(function(s) {
    var active = nowUtcH >= s.utcO && nowUtcH <= s.utcC;
    var soon   = !active && nowUtcH >= s.utcO-0.25 && nowUtcH < s.utcO;
    var bg = active?'#0a2a14':soon?'#2a1f00':'#0a0e14';
    var fg = active?'#00e676':soon?'#ffc107':'#334455';
    cl(sheet, r,1, (active?'● LIVE':soon?'◌ SOON':'○')+'  '+s.name, {bg:bg,fg:fg,sz:11,bold:true,h:28});
    cl(sheet, r,2, s.dxb, {bg:bg,fg:active?'#e0e0e0':'#607d8b',sz:10});
    sheet.getRange(r,3,1,4).merge().setValue(s.pairs)
      .setBackground(bg).setFontColor(active?'#00bcd4':'#445566').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ═══ CHALLENGE SCALE STATUS ═══════════════════════════════════════════════
  r++;
  var scaleColor = chalScale.scale < 0.5 ? '#ff5252' : chalScale.scale < 1.0 ? '#ff9800' : '#00e676';
  var scaleBg    = chalScale.scale < 0.5 ? '#2a0a0a' : chalScale.scale < 1.0 ? '#2a1800' : '#0a2a14';
  mr(sheet, r, 6,
     '⚖  CHALLENGE SCALE: ' + (chalScale.scale*100).toFixed(0) + '×  —  ' + chalScale.reason +
     '  |  Month P&L: ' + (chalScale.monPnlPct>=0?'+':'') + chalScale.monPnlPct.toFixed(1) + '%  of +10% target',
     {bg:scaleBg, fg:scaleColor, sz:11, bold:true, h:32}); r++;

  // ═══ WHAT TO DO RIGHT NOW ═════════════════════════════════════════════════
  r++;
  var inLondon = nowUtcH >= SESS_LONDON.open && nowUtcH <= SESS_LONDON.close;
  var inNy     = nowUtcH >= SESS_NY.open && nowUtcH <= SESS_NY.close;
  var inAny    = inLondon || inNy;
  var sessNow  = inLondon ? 'LONDON' : inNy ? 'NY' : null;

  var actionBg = inAny ? '#0a2a14' : '#111e2e';
  var actionFg = inAny ? '#00ff88' : '#445577';
  mr(sheet, r, 6,
     inAny ? ('⚡  WHAT TO DO NOW  —  ' + sessNow + ' SESSION OPEN  (' + (inLondon?'11:30–14:00':'18:00–21:00') + ' DXB)')
           : '⏰  WHAT TO DO NOW  —  NO SESSION OPEN',
     {bg:actionBg, fg:actionFg, sz:12, bold:true, h:36}); r++;

  if (!inAny) {
    // Show next session time
    var nextSess, nextDxb, nextUtc;
    if (nowUtcH < SESS_LONDON.open || nowUtcH > SESS_NY.close + 12) {
      nextSess='LONDON'; nextDxb='11:30'; nextUtc='07:30 UTC';
    } else if (nowUtcH >= SESS_LONDON.close && nowUtcH < SESS_NY.open) {
      nextSess='NY'; nextDxb='18:00'; nextUtc='14:00 UTC';
    } else {
      nextSess='LONDON (tomorrow)'; nextDxb='11:30'; nextUtc='07:30 UTC';
    }
    mr(sheet, r, 6, 'Next entry window: ' + nextSess + ' at ' + nextDxb + ' DXB (' + nextUtc + ')',
       {bg:'#0a0e14', fg:'#607d8b', sz:10, h:26}); r++;
    mr(sheet, r, 6, 'At 11:15 DXB: Open sheet · check regime · review RORO scores for all 6 pairs',
       {bg:'#0a0e14', fg:'#607d8b', sz:10, h:24}); r++;
    mr(sheet, r, 6, 'At 17:45 DXB: Pre-NY check · confirm macro unchanged · watch for VWAP setups',
       {bg:'#0a0e14', fg:'#607d8b', sz:10, h:24}); r++;
  } else {
    var pairLabels = {
      'CADCHF':'CAD/CHF','AUDJPY':'AUD/JPY','CADJPY':'CAD/JPY',
      'EURCAD':'EUR/CAD','AUDNZD':'AUD/NZD','CHFJPY':'CHF/JPY'
    };
    ['CADCHF','AUDJPY','CADJPY','EURCAD','AUDNZD','CHFJPY'].forEach(function(p) {
      var res = results[p], trade = res.trade, setup = res.setup, macro = res.macro;
      var label = pairLabels[p];
      var dp = PIP_SIZE[p], pl = dp===0.01?3:5;

      var rowBg, rowFg, actionText, detailText;
      if (regime && regime.skipPairs && regime.skipPairs[p]) {
        rowBg = '#100808'; rowFg = '#553333';
        actionText = '⊘  ' + label + '  SKIPPED — ' + regime.label;
        detailText = setup.reason || '';
      } else if (trade) {
        rowBg = trade.dir==='LONG' ? '#0a2a14' : '#2a0a0a';
        rowFg = trade.dir==='LONG' ? '#00ff88' : '#ff5252';
        actionText = (trade.dir==='LONG'?'▲':'▼') + '  ' + label + '  → ENTER ' + trade.dir + ' NOW';
        detailText = 'Entry: ' + trade.entry.toFixed(pl) +
                     '  ·  SL: ' + trade.sl.toFixed(pl) + ' (−' + trade.slPips + 'p)' +
                     '  ·  TP1: ' + trade.tp1.toFixed(pl) + ' (+' + trade.tp1Pips + 'p)' +
                     '  ·  TP2: ' + trade.tp2.toFixed(pl) + ' (+' + trade.tp2Pips + 'p)' +
                     '  ·  Lots: ' + trade.lots.toFixed(2) +
                     '  ·  Risk: $' + Math.round(trade.risk) +
                     '  ·  Scale: ' + (trade.scale*100).toFixed(0) + '×';
      } else if (macro.dir !== 0 && res.tech) {
        // Has macro signal but not in entry zone
        var failed = (setup.checks||[]).filter(function(c){ return !c.ok; });
        var blockedStr = failed.length
          ? 'Waiting on: ' + failed.map(function(c){ return c.label; }).join(' · ')
          : 'All gates ok — waiting for price';
        rowBg = '#0c1a10'; rowFg = '#4a8a5a';
        actionText = '○  ' + label + '  — WATCH  (macro ' + (macro.dir===1?'▲LONG':'▼SHORT') +
                     '  score ' + (macro.score||0).toFixed(1) + ')';
        detailText = blockedStr;
      } else {
        rowBg = '#0a0e14'; rowFg = '#334455';
        actionText = '—  ' + label + '  — WAIT  (score: ' + (macro.score||0).toFixed(1) + ')';
        detailText = macro.error || 'No macro signal  ·  ' + (macro.gated?'EMA20 gate':'score too low — need ≥6');
      }

      cl(sheet, r,1, actionText, {bg:rowBg, fg:rowFg, sz:11, bold:true, h:32});
      sheet.getRange(r,2,1,5).merge().setValue(detailText)
        .setBackground(rowBg).setFontColor(rowFg==='#00ff88'?'#aaddc0':rowFg==='#ff5252'?'#cc7777':'#607d8b')
        .setFontSize(9).setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      r++;

      // VWAP zone row — shown for all pairs with a macro signal (WATCH or ACTIVE)
      var skipped = regime && regime.skipPairs && regime.skipPairs[p];
      var showVwap = !skipped && macro.dir !== 0 && res.tech && res.tech.vwap && res.tech.vwapStd > 0.001;
      if (showVwap) {
        var vDir = macro.dir;
        var vwapV = res.tech.vwap, stdV = res.tech.vwapStd, distV = res.tech.vwapDist || 99;
        var zoneLo  = vDir===1 ? vwapV - stdV        : vwapV - 0.3*stdV;
        var zoneHi  = vDir===1 ? vwapV + 0.3*stdV    : vwapV + stdV;
        var inZone  = vDir===1 ? (distV>=-1.0 && distV<=0.3) : (distV>=-0.3 && distV<=1.0);
        var vwapMsg = 'VWAP ' + res.tech.vwap.toFixed(pl) +
                      '  ±1σ=' + stdV.toFixed(pl) +
                      '  |  ' + (vDir===1?'LONG':'SHORT') + ' entry zone: ' +
                      zoneLo.toFixed(pl) + ' – ' + zoneHi.toFixed(pl) +
                      '  |  dist=' + distV.toFixed(2) + 'σ' +
                      (inZone ? '  ◄ IN ZONE — check all gates' : '  ← wait for pullback to zone') +
                      (res.tech.vwapBars < 4 ? '  (only ' + res.tech.vwapBars + ' bars — still building)' : '');
        var vzBg = inZone ? '#0a2a0a' : '#0a1420';
        var vzFg = inZone ? '#00e676' : '#00bcd4';
        mr(sheet, r, 6, vwapMsg, {bg:vzBg, fg:vzFg, sz:10, bold:inZone, h:28}); r++;
      }

      // Full MT5 instruction row for active trades
      if (trade) {
        var instrText = 'MT5: ' + (trade.dir==='LONG'?'BUY':'SELL') + ' @ market  ·  SL ' + trade.sl.toFixed(pl) +
                        '  ·  TP1 ' + trade.tp1.toFixed(pl) + ' (close ' + trade.lots1.toFixed(2) + ' lots → move SL to breakeven)' +
                        '  ·  TP2 ' + trade.tp2.toFixed(pl) + ' (close ' + trade.lots2.toFixed(2) + ' lots)' +
                        '  ·  R:R ' + trade.rr1 + ' → ' + trade.rr2 +
                        '  ·  If no TP by session end: close at market';
        mr(sheet, r, 6, instrText, {bg:'#060e1a', fg:'#5577aa', sz:9, h:32}); r++;
      }
    });
  }

  // ═══ SIGNAL SUMMARY TABLE ═════════════════════════════════════════════════
  r++;
  mr(sheet, r, 7, 'SIGNAL SUMMARY', {bg:'#111e2e', fg:'#445577', sz:9, bold:true, h:22}); r++;
  ['PAIR','SIGNAL','SCORE','ENTRY','SL','TP1/TP2','VWAP ZONE'].forEach(function(h,i){
    cl(sheet, r, i+1, h, {bg:'#0c1828', fg:'#334455', sz:9, bold:true, h:22});
  });
  r++;

  var pairLabels2 = {
    'CADCHF':'CAD/CHF','AUDJPY':'AUD/JPY ▲only','CADJPY':'CAD/JPY ▲only',
    'EURCAD':'EUR/CAD','AUDNZD':'AUD/NZD','CHFJPY':'CHF/JPY'
  };
  ['CADCHF','AUDJPY','CADJPY','EURCAD','AUDNZD','CHFJPY'].forEach(function(p) {
    var res = results[p], macro = res.macro, trade = res.trade, tech = res.tech;
    var score = macro.score || 0;
    var dp = PIP_SIZE[p], pl = dp===0.01?3:5;
    var skipped = regime && regime.skipPairs && regime.skipPairs[p];

    var sigText, sigBg, sigFg;
    if (skipped) {
      sigText='⊘ SKIP '+regime.label; sigBg='#100808'; sigFg='#553333';
    } else if (trade) {
      sigText = trade.dir==='LONG'?'▲  LONG':'▼  SHORT';
      sigBg = trade.dir==='LONG'?'#0a2a14':'#2a0a0a';
      sigFg = trade.dir==='LONG'?'#00e676':'#ff5252';
    } else {
      sigText = LONG_ONLY[p]?'▲only — WAIT':'—  WAIT';
      sigBg='#0a0e14'; sigFg='#445566';
    }
    var scoreFg = score>=6?'#00e676':score<=-6?'#ff5252':score!==0?'#ffc107':'#445566';

    // VWAP zone text for column 7
    var vwapZoneText = '—', vwapZoneBg = '#0a0e14', vwapZoneFg = '#334455';
    if (!skipped && tech && tech.vwap && tech.vwapStd > 0.001 && macro.dir !== 0) {
      var vd = macro.dir, vv = tech.vwap, vs = tech.vwapStd, vdist = tech.vwapDist || 99;
      var vlo = vd===1 ? vv-vs       : vv-0.3*vs;
      var vhi = vd===1 ? vv+0.3*vs   : vv+vs;
      var vin = vd===1 ? (vdist>=-1.0&&vdist<=0.3) : (vdist>=-0.3&&vdist<=1.0);
      vwapZoneText  = vlo.toFixed(pl) + '\n– ' + vhi.toFixed(pl);
      vwapZoneBg    = vin ? '#0a2a14' : '#0a1428';
      vwapZoneFg    = vin ? '#00e676' : '#00bcd4';
    }

    cl(sheet, r,1, pairLabels2[p], {bg:'#0a0e14', fg:'#c0ccd6', sz:10, bold:true, h:44});
    cl(sheet, r,2, sigText, {bg:sigBg, fg:sigFg, sz:10, bold:true});
    cl(sheet, r,3, macro.gated?score.toFixed(1)+'\n[gated]':score!==0?score.toFixed(1):'—', {bg:'#0a0e14', fg:scoreFg, sz:11, bold:true});
    if (trade) {
      cl(sheet, r,4, trade.entry.toFixed(pl), {bg:'#0a0e14', fg:'#e0e0e0', sz:10, bold:true});
      cl(sheet, r,5, trade.sl.toFixed(pl)+'\n−'+trade.slPips+'p', {bg:'#0a0e14', fg:'#ff5252', sz:9});
      cl(sheet, r,6, trade.tp1.toFixed(pl)+'\n+'+trade.tp1Pips+'p\n→ '+trade.tp2.toFixed(pl)+'\n+'+trade.tp2Pips+'p', {bg:'#0a0e14', fg:'#00e676', sz:9});
    } else {
      [4,5,6].forEach(function(c){ cl(sheet, r,c, '—', {bg:'#0a0e14', fg:'#334455', sz:10}); });
    }
    cl(sheet, r,7, vwapZoneText, {bg:vwapZoneBg, fg:vwapZoneFg, sz:9, bold:vwapZoneFg==='#00e676'});
    r++;
  });

  // ═══ FTMO TRACKER ══════════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, 'FTMO CHALLENGE TRACKER', {bg:'#0a1628', fg:'#445577', sz:10, bold:true, h:22}); r++;

  // User-editable cell labels
  // Equity input cells — restore saved values (clear() wiped them above)
  sheet.getRange('G1').setValue('EQUITY INPUTS ↓').setFontColor('#445577').setFontSize(8)
    .setBackground('#060e1a').setHorizontalAlignment('center');
  var g2 = savedG2 && savedG2 > 0 ? savedG2 : ACCOUNT;
  var g3 = savedG3 && savedG3 > 0 ? savedG3 : ACCOUNT;
  var g4 = savedG4 && savedG4 > 0 ? savedG4 : ACCOUNT;
  sheet.getRange('G2').setValue(g2).setBackground('#0a1a2a').setFontColor('#FFD700')
    .setFontSize(11).setFontWeight('bold').setHorizontalAlignment('center').setNumberFormat('#,##0')
    .setNote('G2 = Current MT5 equity — update this whenever you open the sheet');
  sheet.getRange('G3').setValue(g3).setBackground('#0a1a0a').setFontColor('#aaddaa')
    .setFontSize(10).setFontWeight('bold').setHorizontalAlignment('center').setNumberFormat('#,##0')
    .setNote('G3 = Equity at START of today (set once in the morning)');
  sheet.getRange('G4').setValue(g4).setBackground('#1a1a0a').setFontColor('#aaaadd')
    .setFontSize(10).setFontWeight('bold').setHorizontalAlignment('center').setNumberFormat('#,##0')
    .setNote('G4 = Equity at START of this FTMO month — set once on day 1 ($120,000)');

  var ftmoRows = [
    ['Current equity',   '$'+Math.round(ftmo.curEq).toLocaleString(),
     ftmo.curEq>=FTMO_TARGET?'#00e676':ftmo.curEq<=FTMO_FLOOR?'#ff1744':'#e0e0e0'],
    ['Today P&L',        (ftmo.dayPnl>=0?'+':'')+'$'+Math.round(ftmo.dayPnl).toLocaleString(),
     ftmo.dayPnl>=0?'#00c853':'#ff5252'],
    ['Daily headroom',   '$'+Math.round(Math.max(0,ftmo.dailyLeft)).toLocaleString()+' left of $6,000',
     ftmo.dailyLeft<1000?'#ff1744':ftmo.dailyLeft<3000?'#ff9800':'#00c853'],
    ['Month P&L',        (ftmo.monPnlPct>=0?'+':'')+ftmo.monPnlPct.toFixed(2)+'%  ($'+(ftmo.monPnl>=0?'+':'')+Math.round(ftmo.monPnl).toLocaleString()+')',
     ftmo.monPnlPct>=10?'#00e676':ftmo.monPnlPct>=5?'#ffd700':ftmo.monPnlPct<-5?'#ff5252':'#e0e0e0'],
    ['Distance to target','$'+Math.round(Math.max(0,ftmo.toTarget)).toLocaleString()+' needed for +10%',
     ftmo.toTarget<=0?'#00e676':'#607d8b'],
    ['Distance to floor', '$'+Math.round(Math.max(0,ftmo.toFloor)).toLocaleString()+' above $108k',
     ftmo.toFloor<2000?'#ff1744':ftmo.toFloor<5000?'#ff9800':'#00c853'],
    ['Challenge progress',Math.max(0,Math.round(ftmo.pctDone))+'%  of +10% target',
     ftmo.pctDone>=100?'#00e676':ftmo.pctDone>=50?'#ffd700':'#607d8b'],
    ['Position scale now',(chalScale.scale*100).toFixed(0)+'×  —  '+chalScale.reason,
     chalScale.scale<0.5?'#ff5252':chalScale.scale<1.0?'#ff9800':'#00c853']
  ];

  ftmoRows.forEach(function(row) {
    cl(sheet, r,1, row[0], {bg:'#080e1a', fg:'#445566', sz:10, bold:true, h:30});
    sheet.getRange(r,2,1,5).merge().setValue(row[1])
      .setBackground('#080e1a').setFontColor(row[2]).setFontSize(13).setFontWeight('bold')
      .setVerticalAlignment('middle').setHorizontalAlignment('center');
    r++;
  });

  if (ftmo.floorBreached) {
    mr(sheet, r, 6, '🚨 FLOOR BREACHED — STOP ALL TRADING — Equity ≤ $108,000',
       {bg:'#3a0000', fg:'#ff1744', sz:13, bold:true, h:40}); r++;
  } else if (ftmo.dailyBreached) {
    mr(sheet, r, 6, '⛔ DAILY LIMIT HIT — No more trades today — −$6,000 reached',
       {bg:'#2a1000', fg:'#ff9800', sz:12, bold:true, h:36}); r++;
  } else if (ftmo.toFloor < 3000) {
    mr(sheet, r, 6, '⚠ WARNING: Only $'+Math.round(ftmo.toFloor)+' above floor — reduce size or stop',
       {bg:'#2a1500', fg:'#ff9800', sz:11, bold:true, h:32}); r++;
  }

  // ═══ PER-PAIR DETAIL ══════════════════════════════════════════════════════
  var pairLabels3 = {
    'CADCHF':'CAD/CHF','AUDJPY':'AUD/JPY','CADJPY':'CAD/JPY',
    'EURCAD':'EUR/CAD','AUDNZD':'AUD/NZD','CHFJPY':'CHF/JPY'
  };
  ['CADCHF','AUDJPY','CADJPY','EURCAD','AUDNZD','CHFJPY'].forEach(function(p) {
    var res    = results[p], macro=res.macro, tech=res.tech, setup=res.setup, trade=res.trade;
    var label  = pairLabels3[p];
    var dp     = PIP_SIZE[p], pl = dp===0.01?3:5;
    var skipped = regime && regime.skipPairs && regime.skipPairs[p];
    var longOnly = !!LONG_ONLY[p];

    r++;
    var scolor = trade?'#00ff88':skipped?'#553333':'#607d8b';
    var stitlebg = trade?'#0a2a14':skipped?'#100808':'#0a0e14';
    var stitletext = trade
      ? '●  '+label+'  →  '+trade.dir+'  ·  '+RISK_TIER[p]*100+'% risk · $'+Math.round(trade.risk)+' · '+trade.lots.toFixed(2)+' lots · '+trade.sizeAdj+'× regime · '+(trade.scale*100).toFixed(0)+'× floor scale'
      : skipped ? '⊘  '+label+'  →  SKIPPED ('+regime.label+')'
      : '○  '+label+'  →  STANDBY'+(longOnly?'  [LONG ONLY]':'');
    mr(sheet, r, 6, stitletext, {bg:stitlebg, fg:scolor, sz:11, bold:true, h:36}); r++;

    // Price bar
    var price = macro.pairPrice, scoreV = macro.score || 0;
    ['PRICE','SCORE','EMA20','EMA50','EMA200','ATR(pips)'].forEach(function(h,i){
      cl(sheet, r, i+1, h, {bg:'#0c1828', fg:'#445566', sz:9, bold:true, h:22});
    });
    r++;
    var sfg = scoreV>=6?'#00e676':scoreV<=-6?'#ff5252':'#607d8b';
    cl(sheet, r,1, price?price.toFixed(pl):'—', {bg:'#080e1a', fg:'#e0e0e0', sz:13, bold:true, h:36});
    cl(sheet, r,2, macro.gated?scoreV.toFixed(1)+'\n[gated]':scoreV!==0?scoreV.toFixed(1):'—', {bg:'#080e1a', fg:sfg, sz:13, bold:true});
    cl(sheet, r,3, macro.ema20?macro.ema20.toFixed(pl):'—', {bg:'#080e1a', fg:'#7c99b5', sz:11});
    cl(sheet, r,4, macro.ema50?macro.ema50.toFixed(pl):'—', {bg:'#080e1a', fg:'#5c7a95', sz:11});
    cl(sheet, r,5, macro.ema200?macro.ema200.toFixed(pl):'—', {bg:'#080e1a', fg:'#3c5a75', sz:11});
    cl(sheet, r,6, tech?Math.round(tech.atrPips)+'p  ('+( tech.atrPct*100).toFixed(3)+'%)':'—', {bg:'#080e1a', fg:'#607d8b', sz:10});
    r++;

    // EMA20 gate
    var gateOk = macro.gate === 1;
    mr(sheet, r, 6,
       (gateOk?'✅':'🔴') + ' Daily EMA20 gate: pair ' + (gateOk?'ABOVE':'BELOW') + ' EMA20 (' + (macro.ema20||0).toFixed(pl) + ')  —  ' +
       (longOnly ? (gateOk?'LONGS ALLOWED':'LONGS BLOCKED (long-only pair)')
                 : (gateOk?'LONGS + SHORTS ALLOWED':'ONLY SHORTS ALLOWED')),
       {bg:gateOk?'#0a2a14':'#1a0a0a', fg:gateOk?'#00c853':'#ff5252', sz:10, bold:true, h:28}); r++;

    if (longOnly) {
      mr(sheet, r, 6, '🔼  LONG ONLY (carry trade) — NEVER SHORT this pair',
         {bg:'#100a20', fg:'#9575cd', sz:10, h:24}); r++;
    }

    // VWAP zone (per-pair detail)
    if (tech && tech.vwap && tech.vwapStd > 0.001 && macro.dir !== 0) {
      var vd = macro.dir, vv = tech.vwap, vs = tech.vwapStd, vdist = tech.vwapDist || 99;
      var vlo = vd===1 ? vv-vs       : vv-0.3*vs;
      var vhi = vd===1 ? vv+0.3*vs   : vv+vs;
      var vin = vd===1 ? (vdist>=-1.0&&vdist<=0.3) : (vdist>=-0.3&&vdist<=1.0);
      var vpl = dp===0.01?3:5;
      ['VWAP','±1σ','ENTRY ZONE LOW','ENTRY ZONE HIGH','DIST NOW','STATUS'].forEach(function(h,i){
        cl(sheet,r,i+1,h,{bg:'#0a1828',fg:'#334455',sz:9,bold:true,h:20});
      });
      r++;
      cl(sheet,r,1, vv.toFixed(vpl),      {bg:'#0a1a28',fg:'#e0e0e0',sz:12,bold:true,h:36});
      cl(sheet,r,2, vs.toFixed(vpl),       {bg:'#0a1a28',fg:'#7090a0',sz:11});
      cl(sheet,r,3, vlo.toFixed(vpl),      {bg:'#0a1a28',fg:vin?'#00e676':'#00bcd4',sz:11,bold:vin});
      cl(sheet,r,4, vhi.toFixed(vpl),      {bg:'#0a1a28',fg:vin?'#00e676':'#00bcd4',sz:11,bold:vin});
      cl(sheet,r,5, vdist.toFixed(2)+'σ',  {bg:'#0a1a28',fg:vin?'#00e676':'#607d8b',sz:11,bold:vin});
      cl(sheet,r,6, vin?'IN ZONE ◄':'wait',{bg:vin?'#0a2a14':'#0a1a28',fg:vin?'#00ff88':'#445566',sz:10,bold:vin});
      r++;
      mr(sheet,r,6,
         (vd===1?'LONG':'SHORT')+' entry zone: '+vlo.toFixed(vpl)+' – '+vhi.toFixed(vpl)+
         '  (VWAP '+vv.toFixed(vpl)+' ± '+vs.toFixed(vpl)+')' +
         (tech.vwapBars<4?'  — only '+tech.vwapBars+' bars, still building':'  — '+tech.vwapBars+' bars since session open'),
         {bg:'#081420',fg:vin?'#00e676':'#00bcd4',sz:10,bold:false,h:26}); r++;
    } else if (tech) {
      mr(sheet,r,6,'VWAP: building — session not open yet or < 4 bars',
         {bg:'#0a1428',fg:'#445566',sz:9,h:22}); r++;
    }
    r++;

    // Macro components
    mr(sheet, r, 6, 'MACRO COMPONENTS  (5 of 5, score ≥6=LONG  ≤−6=SHORT)',
       {bg:'#111e2e', fg:'#334466', sz:9, bold:true, h:20}); r++;
    if (macro.components) {
      macro.components.forEach(function(c) {
        var bg = c.val>0?'#0a1a0a':c.val<0?'#1a0a0a':'#0c0c0c';
        var fg = c.val>0?'#00c853':c.val<0?'#ff5252':'#607d8b';
        cl(sheet, r,1, (c.val>0?'▲':c.val<0?'▼':'—')+'  '+c.name, {bg:bg, fg:fg, sz:9, bold:true, h:26});
        sheet.getRange(r,2,1,4).merge().setValue(c.note)
          .setBackground(bg).setFontColor('#7a8899').setFontSize(9)
          .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
        cl(sheet, r,6, c.val>0?'+1':c.val<0?'−1':'0', {bg:bg, fg:fg, sz:12, bold:true});
        r++;
      });
    }

    // Setup checklist
    r++;
    mr(sheet, r, 6, 'SETUP CHECKLIST  (all 12 must pass for ENTRY)', {bg:'#111e2e', fg:'#334466', sz:9, bold:true, h:20}); r++;
    if (setup.checks && setup.checks.length) {
      setup.checks.forEach(function(c) {
        mr(sheet, r, 6, (c.ok?'✓  ':'✗  ') + c.label + '   —   ' + c.note,
           {bg:c.ok?'#0a1a0a':'#1a0a0a', fg:c.ok?'#00c853':'#ff5252', sz:10, h:26}); r++;
      });
    } else {
      mr(sheet, r, 6, setup.reason || 'No signal', {bg:'#0a0e14', fg:'#445566', sz:10, h:26}); r++;
    }

    // Trade setup block
    if (trade) {
      r++;
      mr(sheet, r, 6,
         '⚡ TRADE SETUP — ' + trade.dir + '  ' + label + '  ·  ' + setup.sessName + ' SESSION',
         {bg:'#0a2a14', fg:'#00ff88', sz:12, bold:true, h:32}); r++;

      ['ENTRY','STOP LOSS','TP1  (close 60%)','TP2  (close 40%)','LOTS (total)','RISK $USD'].forEach(function(h,i){
        cl(sheet, r, i+1, h, {bg:'#111111', fg:'#445566', sz:9, bold:true, h:22});
      });
      r++;

      cl(sheet, r,1, trade.entry.toFixed(pl), {bg:'#0a2a14', fg:'#e0e0e0', sz:14, bold:true, h:48});
      cl(sheet, r,2, trade.sl.toFixed(pl),   {bg:'#2a0a0a', fg:'#ff5252', sz:14, bold:true});
      cl(sheet, r,3, trade.tp1.toFixed(pl),  {bg:'#0a2a0a', fg:'#00e676', sz:14, bold:true});
      cl(sheet, r,4, trade.tp2.toFixed(pl),  {bg:'#0a1a2a', fg:'#00bcd4', sz:14, bold:true});
      cl(sheet, r,5, trade.lots.toFixed(2),  {bg:'#1a1a0a', fg:'#ffd700', sz:14, bold:true});
      cl(sheet, r,6, '$'+Math.round(trade.risk), {bg:'#1a1a0a', fg:'#ffd700', sz:12, bold:true});
      r++;

      cl(sheet, r,1, 'ATR: '+trade.atrPips+'p', {bg:'#0a0a0a', fg:'#445566', sz:9, h:28});
      cl(sheet, r,2, '−'+trade.slPips+' pips',  {bg:'#0a0a0a', fg:'#cc4444', sz:11, bold:true});
      cl(sheet, r,3, '+'+trade.tp1Pips+'p  R:R '+trade.rr1, {bg:'#0a0a0a', fg:'#00aa44', sz:11, bold:true});
      cl(sheet, r,4, '+'+trade.tp2Pips+'p  R:R '+trade.rr2, {bg:'#0a0a0a', fg:'#0088bb', sz:11, bold:true});
      cl(sheet, r,5, trade.lots1.toFixed(2)+' lots (TP1)', {bg:'#0a0a0a', fg:'#aaa000', sz:9});
      cl(sheet, r,6, trade.lots2.toFixed(2)+' lots (TP2)', {bg:'#0a0a0a', fg:'#aaa000', sz:9});
      r++;

      mr(sheet, r, 6,
         'STEP 1 → MT5: ' + (trade.dir==='LONG'?'BUY':'SELL') +
         ' @ market  ·  SL ' + trade.sl.toFixed(pl) +
         '  ·  TP1 ' + trade.tp1.toFixed(pl) +
         '  ·  TP2 ' + trade.tp2.toFixed(pl) +
         '  ·  Total ' + trade.lots.toFixed(2) + ' lots',
         {bg:'#080e1a', fg:'#4488cc', sz:10, bold:true, h:32}); r++;
      mr(sheet, r, 6,
         'STEP 2 (when TP1 hit) → Close ' + trade.lots1.toFixed(2) + ' lots  ·  Move SL to ' + trade.entry.toFixed(pl) + ' (breakeven)',
         {bg:'#060e1a', fg:'#336699', sz:10, h:28}); r++;
      mr(sheet, r, 6,
         'STEP 3 (when TP2 hit) → Close remaining ' + trade.lots2.toFixed(2) + ' lots  ·  Trade complete',
         {bg:'#050a12', fg:'#2a5577', sz:10, h:28}); r++;
      mr(sheet, r, 6,
         'EXIT RULE: If session ends (14:00/21:00 DXB) with no TP hit → close at market · Do NOT hold overnight',
         {bg:'#050a12', fg:'#554422', sz:9, h:24}); r++;
      mr(sheet, r, 6,
         RISK_TIER[p]*100+'% risk tier  ·  ' + (trade.sizeAdj*100).toFixed(0)+'% regime size (' + (regime?regime.label:'') + ')  ·  ' + (trade.scale*100).toFixed(0)+'% floor scale  ·  Pip value ≈ $'+(PIP_VALUE[p]||7).toFixed(2)+'/lot — verify in MT5',
         {bg:'#040810', fg:'#2a3a4a', sz:8, h:24}); r++;
    }
  });

  // ═══ DAILY SCHEDULE ═══════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, '⏰  DAILY SCHEDULE — DUBAI (UTC+4)', {bg:'#111e2e', fg:'#445577', sz:9, bold:true, h:22}); r++;

  var schedule = [
    ['11:15 DXB', 'Pre-London',  '#2a2a00', 'Open dashboard · check regime · review RORO scores · identify setups · set price alerts near VWAP zones'],
    ['11:30 DXB', '▶ LON OPEN', '#0a2a14', 'London session opens — VWAP forms · watch for pullback entries · all 6 pairs active (except regime skips)'],
    ['12:00 DXB', 'Mid-London', '#0a1628', 'Check open positions · monitor for TP1 hits · if TP1 hit → move SL to breakeven'],
    ['14:00 DXB', 'LON CLOSE',  '#0a0e14', 'London closes — no new TREND entries · close any open trade that has not hit TP'],
    ['17:45 DXB', 'Pre-NY',     '#2a2a00', 'Open dashboard · check if macro/regime changed · prepare NY session entries'],
    ['18:00 DXB', '▶ NY OPEN',  '#0a2a14', 'NY session opens — new VWAP session starts · VWAP pullback entries allowed'],
    ['19:30 DXB', 'Mid-NY',     '#0a1628', 'Check open positions · TP1 management · monitor daily P&L vs $6k limit'],
    ['21:00 DXB', 'NY CLOSE',   '#0a0e14', 'Close all open trades · no overnight holds · record P&L in G2/G3 for tomorrow'],
    ['21:05 DXB', 'End of day',  '#060e10', 'Update G2 = closing equity · update G3 = same for tomorrow\'s day-start · review challenge progress']
  ];
  schedule.forEach(function(row) {
    var active = false;
    cl(sheet, r,1, row[0], {bg:row[2], fg:row[2]==='#0a2a14'?'#00e676':'#c0ccd6', sz:11, bold:true, h:30});
    cl(sheet, r,2, row[1], {bg:row[2], fg:'#aabbcc', sz:10});
    sheet.getRange(r,3,1,4).merge().setValue(row[3])
      .setBackground(row[2]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ═══ FOOTER ═══════════════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6,
     'V15 Backtest Sep 2024–May 2026 · $120k→$451k (+276.5%) · Sharpe 2.59 · Max DD 13.6% · 9/20 passes (45%) · p=0.0006 ✓ · Floor ✓',
     {bg:'#040810', fg:'#1a2a3a', sz:9, h:24}); r++;
  mr(sheet, r, 6,
     'CAD/CHF 4% WR82% +1.431R · AUD/JPY 3% WR77% +1.192R (L) · CAD/JPY 2% WR55% +0.697R (L) · EUR/CAD 3% WR41% +0.057R · AUD/NZD 1.5% WR45% +0.156R · CHF/JPY 2% WR39% +0.172R',
     {bg:'#040810', fg:'#1a2a3a', sz:8, h:22}); r++;
  mr(sheet, r, 6,
     'Filters: MACD(12,26,9)hist>0 · RSI15m<55/>45 · Vol≥120% · OBV>OBV_EMA10 · VWAP±1σ · 4H/1H/15m EMA aligned · Regime: BULL 1.0× · OIL_WEAK 0.75× · VIX_SPIKE 0.50×',
     {bg:'#040810', fg:'#1a2a3a', sz:8, h:22});

  SpreadsheetApp.flush();
}

// ── MATH HELPERS ─────────────────────────────────────────────────────────────
function calcEma(arr, n) {
  var k = 2/(n+1), out = [arr[0]];
  for (var i=1; i<arr.length; i++) out.push(arr[i]*k + out[i-1]*(1-k));
  return out;
}

function calcRsi14(closes) {
  var g=[], l=[];
  for (var i=1; i<closes.length; i++) {
    var d=closes[i]-closes[i-1]; g.push(d>0?d:0); l.push(d<0?-d:0);
  }
  var k=1/14, ag=g[0], al=l[0];
  for (var j=1; j<g.length; j++) { ag=ag*(1-k)+g[j]*k; al=al*(1-k)+l[j]*k; }
  return al===0?100:100-100/(1+ag/al);
}

function calcAtr14(bars) {
  var tr = bars.map(function(b,i) {
    if (i===0) return b.h-b.l;
    return Math.max(b.h-b.l, Math.abs(b.h-bars[i-1].c), Math.abs(b.l-bars[i-1].c));
  });
  var e = calcEma(tr, 14);
  return e[e.length-1];
}

function resample4h(bars1h) {
  var out=[], cur=null;
  bars1h.forEach(function(b) {
    var dt = new Date(b.dt.replace(' ','T')+'Z');
    var slot = Math.floor(dt.getUTCHours()/4)*4;
    var key = dt.getUTCFullYear()+'-'+dt.getUTCMonth()+'-'+dt.getUTCDate()+'-'+slot;
    if (!cur||cur.key!==key) { if(cur)out.push(cur); cur={key:key,o:b.o,h:b.h,l:b.l,c:b.c}; }
    else { cur.h=Math.max(cur.h,b.h); cur.l=Math.min(cur.l,b.l); cur.c=b.c; }
  });
  if(cur)out.push(cur); return out;
}

function pad(n) { return n<10?'0'+n:''+n; }

function calcMacdHistogram(closes) {
  if (closes.length < 35) return null;
  var e12 = calcEma(closes,12), e26 = calcEma(closes,26);
  var macd = e12.map(function(v,i){ return v-e26[i]; });
  var sig  = calcEma(macd,9);
  return macd[macd.length-1] - sig[sig.length-1];
}

function calcObvAboveEma(closes, volumes) {
  if (!closes||closes.length<12||closes.length!==volumes.length) return null;
  var obv=[0];
  for (var i=1; i<closes.length; i++) {
    var sign = closes[i]>closes[i-1]?1:closes[i]<closes[i-1]?-1:0;
    obv.push(obv[i-1]+sign*volumes[i]);
  }
  var e = calcEma(obv,10);
  return obv[obv.length-1] > e[e.length-1];
}

// ── CELL HELPERS ─────────────────────────────────────────────────────────────
function cl(sheet, row, col, val, opts) {
  var o=opts||{};
  sheet.getRange(row,col)
    .setValue(val).setBackground(o.bg||'#080e1a').setFontColor(o.fg||'#c0ccd6')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if(o.h) sheet.setRowHeight(row,o.h);
}

function mr(sheet, row, cols, val, opts) {
  var o=opts||{};
  sheet.getRange(row,1,1,cols).merge()
    .setValue(val).setBackground(o.bg||'#080e1a').setFontColor(o.fg||'#c0ccd6')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(row, o.h||30);
}
