/**
 * CAD/CHF RORO SIGNAL — FTMO $120k Challenge
 * ============================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste this → Save
 *   2. Run setupTrigger() once; authorize when prompted
 *   3. Run updateDashboard() to populate immediately
 *   4. In column G rows 2-3: enter your MT5 current equity and today's start equity
 *
 * Auto-refreshes every 5 minutes while market is open.
 *
 * Sessions:   London open  07:30–09:00 UTC
 *             NY overlap   14:00–16:30 UTC
 *
 * Macro gate: CAD/CHF must be above daily EMA20 before any LONG fires.
 *             5-component macro score: oil + SPX + VIX(inv) + pair EMA50 + pair EMA200.
 *             Entry requires 4 of 5 components to agree (|score| ≥ 6).
 *
 * Backtest:   p=0.019 ✓ · Sharpe 1.42 · WR 51% · MDD 5.8% · +34.4% (V6, 115 weeks)
 * FTMO rules: $120k account · 10% profit target · 10% max loss from initial · 5% daily
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var TWELVE_KEY  = "06f050cd7d9940a895f9461e7f0ff7e3";
var ACCOUNT     = 120000;
var RISK_PCT    = 0.02;         // 2% per trade (aggressive for challenge)
var SL_MULT     = 0.75;
var TP1_MULT    = 1.50;
var TP2_MULT    = 2.50;
var TP1_FRAC    = 0.60;
var TP2_FRAC    = 0.40;

var FTMO_FLOOR  = ACCOUNT * 0.90;   // $108,000 — absolute stop
var FTMO_TARGET = ACCOUNT * 1.10;   // $132,000 — win condition
var FTMO_DAILY  = ACCOUNT * 0.05;   // $6,000 — daily loss limit

// London session
var LDN_VWAP_H  = 7.0;   // VWAP resets 07:00 UTC
var LDN_ENTRY_S = 7.5;   // 07:30 UTC — entry opens
var LDN_ENTRY_E = 9.0;   // 09:00 UTC — entry closes

// NY overlap session
var NY_VWAP_H   = 13.5;  // VWAP resets 13:30 UTC
var NY_ENTRY_S  = 14.0;  // 14:00 UTC — entry opens
var NY_ENTRY_E  = 16.5;  // 16:30 UTC — entry closes

// ── TRIGGER SETUP ─────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ Auto-refresh every 5 minutes.\n' +
      'Run updateDashboard() to populate now.\n\n' +
      'FTMO tracker: enter your MT5 balance in G2, day-start balance in G3.'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('CAD/CHF SIGNAL') || ss.insertSheet('CAD/CHF SIGNAL');

  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  // Read FTMO tracker values from column G (user-editable)
  var currentEq   = dash.getRange('G2').getValue() || ACCOUNT;
  var dayStartEq  = dash.getRange('G3').getValue() || ACCOUNT;

  var macro     = getMacroScore();
  var bars15m   = get15mBars(70);    // enough for VWAP + ATR14 + EMA21
  var bars1h    = get1hBars(250);    // enough for 4H EMA + RSI14
  var sessState = getSessionState(nowH);
  var tech      = computeTechnicals(bars15m, bars1h, nowH);
  var setup     = computeSetup(macro, tech, sessState);
  var ftmo      = computeFtmo(currentEq, dayStartEq);

  writeSheet(dash, now, macro, tech, sessState, setup, ftmo);
}

// ── MACRO SCORE ───────────────────────────────────────────────────────────────
function getMacroScore() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var cached = cache.get('cadchf_macro_' + today);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yf(sym, range) {
    try {
      var url = 'https://query1.finance.yahoo.com/v8/finance/chart/' +
                encodeURIComponent(sym) + '?interval=1d&range=' + range;
      var r = UrlFetchApp.fetch(url, {
        muteHttpExceptions: true,
        headers: {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
      });
      var j = JSON.parse(r.getContentText());
      var closes = j.chart.result[0].indicators.quote[0].close;
      return closes.filter(function(c) { return c !== null && c !== undefined; });
    } catch(e) { return null; }
  }

  var result = {
    score: 0, dir: 0, s1_gate: 0, rawScore: 0,
    components: [], error: null, pairPrice: null
  };

  var pair = yf('CADCHF%3DX', '1y');
  var oil  = yf('CL%3DF',     '1y');
  var spx  = yf('%5EGSPC',    '1y');
  var vix  = yf('%5EVIX',     '6mo');

  if (!pair || pair.length < 60) {
    result.error = 'CAD/CHF daily data unavailable';
    return result;
  }

  var n   = pair.length;
  result.pairPrice = pair[n - 1];

  var pEma20  = ema(pair, 20);
  var pEma50  = ema(pair, 50);
  var pEma200 = ema(pair, 200);

  // V6 daily trend gate: only fire long signals when pair > EMA20
  var s1_gate = pair[n - 2] > pEma20[n - 2] ? 1 : -1;
  result.s1_gate = s1_gate;

  // s1: pair vs EMA50 (medium-term trend)
  var s1 = pair[n - 2] > pEma50[n - 2] ? 1 : -1;

  // s2: oil vs EMA50 (CAD fundamental — oil up → CAD strengthens)
  var s2 = 0;
  var oilNote = 'Oil data unavailable';
  if (oil && oil.length >= 52) {
    var oEma50 = ema(oil, 50);
    var om = oil.length;
    s2 = oil[om - 2] > oEma50[om - 2] ? 1 : -1;
    oilNote = 'WTI $' + Math.round(oil[om-2]) + ' vs EMA50 $' + Math.round(oEma50[om-2]) + ' → CAD ' + (s2 > 0 ? 'supported' : 'weak');
  }

  // s3: SPX vs EMA50 (risk-on → CHF weaker → CAD/CHF up)
  var s3 = 0;
  var spxNote = 'SPX data unavailable';
  if (spx && spx.length >= 52) {
    var sEma50 = ema(spx, 50);
    var sm = spx.length;
    s3 = spx[sm - 2] > sEma50[sm - 2] ? 1 : -1;
    spxNote = 'SPX ' + Math.round(spx[sm-2]) + ' vs EMA50 ' + Math.round(sEma50[sm-2]) + ' → ' + (s3 > 0 ? 'risk-on ↑' : 'risk-off ↓');
  }

  // s4: VIX vs EMA20 (INVERTED — low VIX = risk calm = CHF weak = CADCHF up)
  var s4 = 0;
  var vixNote = 'VIX data unavailable';
  if (vix && vix.length >= 22) {
    var vEma20 = ema(vix, 20);
    var vm = vix.length;
    s4 = vix[vm - 2] < vEma20[vm - 2] ? 1 : -1;   // inverted
    vixNote = 'VIX ' + Math.round(vix[vm-2]*10)/10 + ' vs EMA20 ' + Math.round(vEma20[vm-2]*10)/10 + ' → CHF ' + (s4 > 0 ? 'weak (calm)' : 'bid (risk-off)');
  }

  // s5: pair vs EMA200 (long-term regime)
  var s5 = pEma200.length >= 200 ? (pair[n - 2] > pEma200[n - 2] ? 1 : -1) : 0;

  var raw   = s1 + s2 + s3 + s4 + s5;
  var score = Math.round(raw / 5 * 100) / 10;

  // V6 gate: suppress signal if pair opposes daily EMA20 trend
  var gatedScore = (raw !== 0 && Math.sign(score) === Math.sign(s1_gate)) ? score : 0;

  result.rawScore = score;
  result.score    = gatedScore;
  result.dir      = gatedScore >= 6 ? 1 : gatedScore <= -6 ? -1 : 0;

  var pairEma50v  = Math.round(pEma50[n - 2] * 10000) / 10000;
  var pairEma200v = pEma200.length >= 200 ? Math.round(pEma200[n - 2] * 10000) / 10000 : 0;
  var pairEma20v  = Math.round(pEma20[n - 2] * 10000) / 10000;

  result.components = [
    {name: 'CAD/CHF vs EMA50', val: s1,
     note: pair[n-2].toFixed(4) + ' vs EMA50 ' + pairEma50v + ' → ' + (s1 > 0 ? 'uptrend' : 'downtrend')},
    {name: 'Oil (WTI) trend',  val: s2, note: oilNote},
    {name: 'SPX regime',       val: s3, note: spxNote},
    {name: 'VIX calm (inv)',   val: s4, note: vixNote},
    {name: 'CAD/CHF vs EMA200',val: s5,
     note: pairEma200v > 0 ? pair[n-2].toFixed(4) + ' vs EMA200 ' + pairEma200v + ' → ' + (s5 > 0 ? 'bull regime' : 'bear regime') : 'insufficient history'}
  ];

  result.ema20  = pairEma20v;
  result.ema50  = pairEma50v;
  result.ema200 = pairEma200v;
  result.vixNow = (vix && vix.length) ? Math.round(vix[vix.length-1]*10)/10 : null;

  cache.put('cadchf_macro_' + today, JSON.stringify(result), 21600);
  return result;
}

// ── DATA: TWELVE DATA ─────────────────────────────────────────────────────────
function tdFetch(interval, outputsize) {
  try {
    var url = 'https://api.twelvedata.com/time_series?symbol=CAD/CHF' +
              '&interval=' + interval + '&outputsize=' + outputsize +
              '&apikey=' + TWELVE_KEY + '&timezone=UTC';
    var r = UrlFetchApp.fetch(url, {muteHttpExceptions: true});
    var j = JSON.parse(r.getContentText());
    if (!j.values || j.status === 'error') return [];
    return j.values.map(function(v) {
      return {dt: v.datetime, o: parseFloat(v.open), h: parseFloat(v.high),
              l: parseFloat(v.low),  c: parseFloat(v.close)};
    }).reverse();   // oldest → newest
  } catch(e) { return []; }
}

function get15mBars(n) { return tdFetch('15min', n); }
function get1hBars(n)  { return tdFetch('1h',    n); }

// ── MATH HELPERS ──────────────────────────────────────────────────────────────
function ema(arr, period) {
  var k = 2 / (period + 1);
  var out = [arr[0]];
  for (var i = 1; i < arr.length; i++) out.push(arr[i] * k + out[i-1] * (1-k));
  return out;
}

function rsi14(arr) {
  var gains = [], losses = [];
  for (var i = 1; i < arr.length; i++) {
    var d = arr[i] - arr[i-1];
    gains.push(d > 0 ? d : 0);
    losses.push(d < 0 ? -d : 0);
  }
  var k = 1 / 14;   // Wilder's smoothing
  var ag = gains[0], al = losses[0];
  for (var j = 1; j < gains.length; j++) {
    ag = ag * (1 - k) + gains[j] * k;
    al = al * (1 - k) + losses[j] * k;
  }
  return al === 0 ? 100 : 100 - 100 / (1 + ag / al);
}

function atr14(bars) {
  var tr = bars.map(function(b, i) {
    if (i === 0) return b.h - b.l;
    return Math.max(b.h - b.l, Math.abs(b.h - bars[i-1].c), Math.abs(b.l - bars[i-1].c));
  });
  var e = ema(tr, 14);
  return e[e.length - 1];
}

function resampleTo4H(bars1h) {
  var out = [], cur = null;
  bars1h.forEach(function(b) {
    var dt   = new Date(b.dt.replace(' ', 'T') + 'Z');
    var slot = Math.floor(dt.getUTCHours() / 4) * 4;
    var key  = dt.getUTCFullYear() + '-' + dt.getUTCMonth() + '-' + dt.getUTCDate() + '-' + slot;
    if (!cur || cur.key !== key) {
      if (cur) out.push(cur);
      cur = {key: key, o: b.o, h: b.h, l: b.l, c: b.c};
    } else { cur.h = Math.max(cur.h, b.h); cur.l = Math.min(cur.l, b.l); cur.c = b.c; }
  });
  if (cur) out.push(cur);
  return out;
}

function sessionVwap(bars15m, vwapStartH) {
  var now   = new Date();
  var ymd   = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sOpen = new Date(ymd + 'T' + pad(Math.floor(vwapStartH)) + ':' +
              pad(Math.round((vwapStartH % 1) * 60)) + ':00Z');
  var sessBars = bars15m.filter(function(b) {
    return new Date(b.dt.replace(' ', 'T') + 'Z') >= sOpen;
  });
  if (sessBars.length < 2) return {vwap: 0, std: 1e-5, n: 0};
  var tpArr    = sessBars.map(function(b) { return (b.h + b.l + b.c) / 3; });
  var vwapVal  = tpArr.reduce(function(a, v) { return a + v; }, 0) / tpArr.length;
  var variance = tpArr.reduce(function(a, tp) { return a + Math.pow(tp - vwapVal, 2); }, 0) / tpArr.length;
  return {vwap: vwapVal, std: Math.max(Math.sqrt(variance), 1e-5), n: tpArr.length};
}

function pad(n) { return n < 10 ? '0' + n : '' + n; }

// ── SESSION DETECTION ─────────────────────────────────────────────────────────
function getSessionState(nowH) {
  if      (nowH >= LDN_ENTRY_S && nowH <= LDN_ENTRY_E) return 'LONDON_ENTRY';
  else if (nowH >= LDN_VWAP_H  && nowH < LDN_ENTRY_S)  return 'LONDON_CHOP';
  else if (nowH >= NY_ENTRY_S  && nowH <= NY_ENTRY_E)   return 'NY_ENTRY';
  else if (nowH >= NY_VWAP_H   && nowH < NY_ENTRY_S)    return 'NY_CHOP';
  else if (nowH > LDN_ENTRY_E  && nowH < NY_VWAP_H)     return 'MIDDAY';
  else if (nowH > NY_ENTRY_E   && nowH < 22)             return 'LATE';
  return 'OVERNIGHT';
}

// ── TECHNICALS ────────────────────────────────────────────────────────────────
function computeTechnicals(bars15m, bars1h, nowH) {
  if (!bars15m || bars15m.length < 25) return null;

  var closes15 = bars15m.map(function(b) { return b.c; });
  var e9_15    = ema(closes15, 9);
  var e21_15   = ema(closes15, 21);
  var atrVal   = atr14(bars15m);
  var last15   = bars15m[bars15m.length - 1];

  // 1H indicators
  var rsi1h = null, e9_1h = null, e21_1h = null;
  if (bars1h && bars1h.length >= 30) {
    var c1h  = bars1h.map(function(b) { return b.c; });
    e9_1h    = ema(c1h, 9);
    e21_1h   = ema(c1h, 21);
    rsi1h    = rsi14(c1h);
  }

  // 4H (resample 1H)
  var e9_4h = null, e21_4h = null;
  if (bars1h && bars1h.length >= 50) {
    var b4h  = resampleTo4H(bars1h);
    var c4h  = b4h.map(function(b) { return b.c; });
    if (c4h.length >= 22) {
      e9_4h  = ema(c4h, 9);
      e21_4h = ema(c4h, 21);
    }
  }

  // Active session VWAP
  var activeVwapH = (nowH >= NY_VWAP_H) ? NY_VWAP_H : LDN_VWAP_H;
  var sv = sessionVwap(bars15m, activeVwapH);

  return {
    price:    last15.c,
    ema9_15:  e9_15[e9_15.length - 1],
    ema21_15: e21_15[e21_15.length - 1],
    atr:      atrVal,
    atr_pct:  atrVal / last15.c,
    ema9_1h:  e9_1h  ? e9_1h[e9_1h.length - 1]   : null,
    ema21_1h: e21_1h ? e21_1h[e21_1h.length - 1]  : null,
    ema9_4h:  e9_4h  ? e9_4h[e9_4h.length - 1]   : null,
    ema21_4h: e21_4h ? e21_4h[e21_4h.length - 1]  : null,
    rsi1h:    rsi1h,
    vwap:     sv.vwap,
    vwapStd:  sv.std,
    sessBarCount: sv.n
  };
}

// ── FTMO STATUS ───────────────────────────────────────────────────────────────
function computeFtmo(currentEq, dayStartEq) {
  var dayPnl    = currentEq - dayStartEq;
  var dailyLeft = FTMO_DAILY + dayPnl;   // remaining before daily limit
  var toFloor   = currentEq - FTMO_FLOOR;
  var toTarget  = FTMO_TARGET - currentEq;
  var pctDone   = (currentEq - ACCOUNT) / (FTMO_TARGET - ACCOUNT) * 100;
  return {
    equity:     currentEq,
    dayStartEq: dayStartEq,
    dayPnl:     dayPnl,
    dailyLeft:  dailyLeft,
    toFloor:    toFloor,
    toTarget:   toTarget,
    pctDone:    pctDone,
    floorBreached: currentEq <= FTMO_FLOOR,
    dailyBreached: dailyLeft <= 0
  };
}

// ── SETUP SIGNAL ──────────────────────────────────────────────────────────────
function computeSetup(macro, tech, sessState) {
  if (!tech || macro.error)
    return {action: 'WAIT', reason: macro.error || 'No price data'};

  var dir    = macro.dir;   // 1 = LONG, -1 = SHORT, 0 = neutral
  var price  = tech.price;
  var atrVal = tech.atr;

  if (dir === 0) return {action: 'WAIT', dir: 'NEUTRAL',
    reason: '|score| < 6 or daily EMA20 gate blocking — no macro signal',
    checks: [], atr: atrVal, s1_gate: macro.s1_gate};

  var inEntry = (sessState === 'LONDON_ENTRY' || sessState === 'NY_ENTRY');

  var h4ok  = tech.ema9_4h && tech.ema21_4h ?
    (dir === 1 ? tech.ema9_4h > tech.ema21_4h : tech.ema9_4h < tech.ema21_4h) : null;
  var h1ok  = tech.ema9_1h && tech.ema21_1h ?
    (dir === 1 ? tech.ema9_1h > tech.ema21_1h : tech.ema9_1h < tech.ema21_1h) : null;
  var m15ok = (dir === 1 ? tech.ema9_15 > tech.ema21_15 : tech.ema9_15 < tech.ema21_15);

  var dist  = tech.vwapStd > 1e-8 ? (price - tech.vwap) / tech.vwapStd : 99;
  var vwOk  = tech.sessBarCount >= 4 &&
    (dir === 1 ? (dist >= -1.0 && dist <= 0.3) : (dist >= -0.3 && dist <= 1.0));

  var rsiOk = true;
  var rsiNote = '';
  if (tech.rsi1h !== null) {
    if (dir === 1 && tech.rsi1h > 70) { rsiOk = false; rsiNote = '1H RSI=' + Math.round(tech.rsi1h) + ' overbought'; }
    if (dir === -1 && tech.rsi1h < 30) { rsiOk = false; rsiNote = '1H RSI=' + Math.round(tech.rsi1h) + ' oversold'; }
  }

  var checks = [
    {label: 'Entry window open', ok: inEntry,
     note: inEntry ? sessState : 'Outside entry window (' + sessState + ')'},
    {label: '4H EMA aligned',   ok: h4ok !== null ? h4ok : true,
     note: h4ok === null ? '—' : h4ok ? '4H EMA9 > EMA21' : '4H EMA not aligned'},
    {label: '1H EMA aligned',   ok: h1ok !== null ? h1ok : true,
     note: h1ok === null ? '—' : h1ok ? '1H EMA9 > EMA21' : '1H EMA not aligned'},
    {label: '15m EMA aligned',  ok: m15ok,
     note: m15ok ? '15m EMA9 > EMA21' : '15m EMA not aligned'},
    {label: 'VWAP zone (≥4 bars)',ok: vwOk,
     note: vwOk ? 'dist=' + Math.round(dist*100)/100 + 'σ ✓' :
           (tech.sessBarCount < 4 ? 'Only ' + tech.sessBarCount + ' VWAP bars — need 4' :
            'dist=' + Math.round(dist*100)/100 + 'σ outside zone')},
    {label: '1H RSI filter',    ok: rsiOk,
     note: rsiOk ? (tech.rsi1h ? 'RSI=' + Math.round(tech.rsi1h) + ' ✓' : '—') : rsiNote}
  ];

  var allOk = inEntry && (h4ok !== false) && (h1ok !== false) && m15ok && vwOk && rsiOk;

  if (allOk) {
    var risk  = ACCOUNT * RISK_PCT;
    var slPts = SL_MULT * atrVal;
    // CAD/CHF lot sizing: 1 standard lot = 100,000 CAD
    // pip value ≈ $8.50 per standard lot (CHF/USD ≈ 1.08 at current rates)
    // Lots = risk_usd / (sl_in_pips * pip_value_per_lot)
    var slPips    = slPts / 0.0001;   // convert to pips (4-decimal pair)
    var pipValue  = 8.50;             // USD per pip per standard lot (approx)
    var lots      = risk / (slPips * pipValue);
    lots = Math.max(0.01, Math.round(lots * 100) / 100);   // min 0.01 lot

    var sl, tp1, tp2;
    if (dir === 1) {
      sl  = price - slPts;
      tp1 = price + TP1_MULT * atrVal;
      tp2 = price + TP2_MULT * atrVal;
    } else {
      sl  = price + slPts;
      tp1 = price - TP1_MULT * atrVal;
      tp2 = price - TP2_MULT * atrVal;
    }
    return {
      action: 'ACTIVE', dir: dir === 1 ? 'LONG' : 'SHORT',
      entry: price, sl: sl, tp1: tp1, tp2: tp2,
      lots: lots, slPips: Math.round(slPips),
      risk: risk, atr: atrVal, checks: checks,
      s1_gate: macro.s1_gate
    };
  }

  var missing = checks.filter(function(c) { return !c.ok; }).map(function(c) { return c.note; });
  return {
    action: 'WAIT', dir: dir === 1 ? 'LONG' : 'SHORT',
    reason: missing.join(' | '),
    checks: checks, atr: atrVal, s1_gate: macro.s1_gate
  };
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, macro, tech, sessState, setup, ftmo) {
  sheet.clear();
  [180, 130, 130, 130, 130, 20, 130].forEach(function(w, i) { sheet.setColumnWidth(i+1, w); });

  var utcStr   = Utilities.formatDate(now, 'UTC',    'HH:mm UTC');
  var lonStr   = Utilities.formatDate(now, 'Europe/London', 'HH:mm LON');
  var nyStr    = Utilities.formatDate(now, 'America/New_York', 'HH:mm NY');
  var r = 1;

  // ── Header ────────────────────────────────────────────────────────────────
  sheet.getRange(r, 1, 1, 5).merge().setValue('🍁  CAD/CHF · RORO SIGNAL · FTMO')
    .setBackground('#0a1628').setFontColor('#e8f4f8').setFontSize(16).setFontWeight('bold')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(r, 44); r++;

  sheet.getRange(r, 1, 1, 5).merge().setValue(utcStr + '  ·  ' + lonStr + '  ·  ' + nyStr)
    .setBackground('#060e1a').setFontColor('#445566').setFontSize(10)
    .setVerticalAlignment('middle').setHorizontalAlignment('center');
  sheet.setRowHeight(r, 24); r++; r++;

  // ── Price | Score | Session | 4H Trend | ATR ────────────────────────────
  var price     = tech ? tech.price : (macro.pairPrice || 0);
  var score     = macro.score || 0;
  var rawScore  = macro.rawScore || 0;
  var gated     = (rawScore !== 0 && score === 0);
  var scoreFg   = score >= 6 ? '#00e676' : score >= 2 ? '#76ff03' : score > 0 ? '#c8ff00' :
                  score <= -6 ? '#ff1744' : score <= -2 ? '#ff5722' : '#888888';
  var sessLabels = {
    LONDON_ENTRY: '✅ LONDON ENTRY',  LONDON_CHOP: '⚠️ London open',
    NY_ENTRY:     '✅ NY ENTRY',      NY_CHOP:     '⚠️ NY pre-entry',
    MIDDAY:       '🕐 Midday gap',   LATE:        '🔒 Entry closed',
    OVERNIGHT:    '💤 Overnight'
  };
  var sessFgs = {
    LONDON_ENTRY: '#00e676', LONDON_CHOP: '#ffc107',
    NY_ENTRY:     '#00e676', NY_CHOP:    '#ffc107',
    MIDDAY:'#607d8b', LATE:'#607d8b', OVERNIGHT:'#37474f'
  };

  ['PRICE','MACRO SCORE','SESSION','4H TREND','ATR'].forEach(function(lbl, i) {
    cell(sheet, r, i+1, lbl, {bg:'#111e2e', fg:'#445566', sz:9, bold:true});
  });
  sheet.setRowHeight(r, 22); r++;

  var h4t   = tech && tech.ema9_4h && tech.ema21_4h ?
              (tech.ema9_4h > tech.ema21_4h ? '↑ Bullish' : '↓ Bearish') : '—';
  var h4tfg = tech && tech.ema9_4h && tech.ema21_4h ?
              (tech.ema9_4h > tech.ema21_4h ? '#00e676' : '#ff5252') : '#607d8b';
  var scoreText = gated ?
    (rawScore.toFixed(1) + ' [gated]') : score.toFixed(1);
  var scoreBg   = gated ? '#1a1a0a' : '#080e1a';

  cell(sheet, r, 1, price ? price.toFixed(5) : '—', {bg:'#080e1a', fg:'#e0e0e0', sz:15, bold:true, h:42});
  cell(sheet, r, 2, scoreText, {bg:scoreBg, fg: gated ? '#aa9900' : scoreFg, sz:15, bold:true});
  cell(sheet, r, 3, sessLabels[sessState]||sessState, {bg:'#080e1a', fg:sessFgs[sessState]||'#607d8b', sz:11, bold:true});
  cell(sheet, r, 4, h4t, {bg:'#080e1a', fg:h4tfg, sz:12, bold:true});
  cell(sheet, r, 5, tech ? Math.round(tech.atr*100000)/10 + ' p' : '—', {bg:'#080e1a', fg:'#607d8b', sz:11});
  r++; r++;

  // ── EMA20 Gate status ─────────────────────────────────────────────────────
  var gateOk = macro.s1_gate === 1;
  var gateText = gateOk
    ? '✅ Trend gate: pair ABOVE daily EMA20 (' + macro.ema20 + ') — LONGS ALLOWED'
    : '🔴 Trend gate: pair BELOW daily EMA20 (' + macro.ema20 + ') — LONGS BLOCKED · EMA50: ' + macro.ema50;
  mrow(sheet, r, 5, gateText, {bg: gateOk ? '#0a2a14' : '#2a0a0a', fg: gateOk ? '#00c853' : '#ff5252', sz:10, bold:true}); r++; r++;

  // ── Setup panel ───────────────────────────────────────────────────────────
  var sbg   = setup.action === 'ACTIVE' ? '#0a2a14' : '#0d1a0a';
  var sfg   = setup.action === 'ACTIVE' ? '#00ff88' : '#607d8b';
  var stitle = setup.action === 'ACTIVE'
    ? '🟢  ACTIVE SETUP — ' + setup.dir + ' CAD/CHF'
    : '⏳  WAITING — Bias: ' + (setup.dir || (score >= 6 ? 'LONG' : score <= -6 ? 'SHORT' : 'NEUTRAL'));
  mrow(sheet, r, 5, stitle, {bg:sbg, fg:sfg, sz:14, bold:true}); r++;

  if (setup.action === 'ACTIVE') {
    var hdrs5 = ['ENTRY', 'STOP LOSS', 'TP1 (60%)', 'TP2 (40%)', 'LOTS (MT5)'];
    var vals5 = [setup.entry.toFixed(5), setup.sl.toFixed(5), setup.tp1.toFixed(5),
                 setup.tp2.toFixed(5), setup.lots.toFixed(2)];
    var fgs5  = ['#e0e0e0', '#ff5252', '#00e676', '#00bcd4', '#ffd700'];
    hdrs5.forEach(function(lbl, i) { cell(sheet, r,   i+1, lbl, {bg:'#111111', fg:'#445566', sz:9}); });
    vals5.forEach(function(v,   i) { cell(sheet, r+1, i+1, v,   {bg:'#0a2a14', fg:fgs5[i], sz:13, bold:true, h:44}); });
    r += 2;
    mrow(sheet, r, 5,
      'Risk $' + Math.round(setup.risk) + '  ·  SL ' + setup.slPips + ' pips  ·  ATR ' + Math.round(setup.atr*100000)/10 + 'p  ·  When TP1 hit → move SL to entry (BE)',
      {bg:'#0a0a0a', fg:'#445566', sz:9}); r++;
    mrow(sheet, r, 5, 'Min size: 0.01 lot  ·  pip value ≈ $8.50/lot (CADCHF)  ·  verify in MT4 Market Watch',
      {bg:'#0a0a0a', fg:'#334455', sz:8}); r++;
  } else if (setup.checks && setup.checks.length) {
    setup.checks.forEach(function(c) {
      mrow(sheet, r, 5,
        (c.ok ? '✓  ' : '✗  ') + c.label + (c.ok ? '' : '  —  ' + c.note),
        {bg: c.ok ? '#0a1a0a' : '#1a0a0a', fg: c.ok ? '#00c853' : '#ff5252', sz:10});
      sheet.setRowHeight(r, 28); r++;
    });
  } else {
    mrow(sheet, r, 5, setup.reason || '—', {bg:'#0d0d0d', fg:'#445566', sz:10}); r++;
  }
  r++;

  // ── Macro Components ──────────────────────────────────────────────────────
  mrow(sheet, r, 5, 'MACRO COMPONENTS (4 of 5 must agree for signal)', {bg:'#111e2e', fg:'#334466', sz:10, bold:true}); r++;
  if (macro.components && macro.components.length) {
    macro.components.forEach(function(c) {
      var bg2 = c.val > 0 ? '#0a1a0a' : c.val < 0 ? '#1a0a0a' : '#111111';
      var fg2 = c.val > 0 ? '#00c853' : c.val < 0 ? '#ff5252' : '#607d8b';
      cell(sheet, r, 1, (c.val > 0 ? '▲ ' : c.val < 0 ? '▼ ' : '— ') + c.name, {bg:bg2, fg:fg2, sz:10, bold:true});
      sheet.getRange(r, 2, 1, 3).merge().setValue(c.note)
        .setBackground(bg2).setFontColor('#7a8899').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      cell(sheet, r, 5, c.val > 0 ? '+1' : c.val < 0 ? '−1' : '0', {bg:bg2, fg:fg2, sz:12, bold:true});
      sheet.setRowHeight(r, 30); r++;
    });
  }
  r++;

  // ── Key Levels ────────────────────────────────────────────────────────────
  if (tech) {
    mrow(sheet, r, 5, 'KEY LEVELS (current session)', {bg:'#111e2e', fg:'#334466', sz:10, bold:true}); r++;
    var rsiStr = tech.rsi1h ? Math.round(tech.rsi1h) + (tech.rsi1h > 70 ? ' ⚠️ OB' : tech.rsi1h < 30 ? ' ⚠️ OS' : ' ✓') : '—';
    var lvls = [
      ['Session VWAP',     tech.vwap.toFixed(5),    tech.sessBarCount + ' bars | ±1σ band: ' + (tech.vwap - tech.vwapStd).toFixed(5) + ' / ' + (tech.vwap + tech.vwapStd).toFixed(5)],
      ['VWAP zone (LONG)', (tech.vwap - tech.vwapStd).toFixed(5) + ' to ' + (tech.vwap + 0.3*tech.vwapStd).toFixed(5), 'Buy zone: −1σ to +0.3σ'],
      ['VWAP zone (SHORT)',(tech.vwap - 0.3*tech.vwapStd).toFixed(5) + ' to ' + (tech.vwap + tech.vwapStd).toFixed(5), 'Sell zone: −0.3σ to +1.0σ'],
      ['1H RSI(14)',       rsiStr,                  '>70 blocks longs · <30 blocks shorts'],
      ['15m EMA9/21',      tech.ema9_15.toFixed(5)+'  /  '+tech.ema21_15.toFixed(5), '15m momentum'],
      ['1H EMA9/21',       (tech.ema9_1h||0).toFixed(5)+'  /  '+(tech.ema21_1h||0).toFixed(5), '1H momentum'],
      ['4H EMA9/21',       (tech.ema9_4h||0).toFixed(5)+'  /  '+(tech.ema21_4h||0).toFixed(5), '4H trend']
    ];
    lvls.forEach(function(lv) {
      cell(sheet, r, 1, lv[0], {bg:'#0d0d0d', fg:'#607d8b', sz:9, bold:true});
      cell(sheet, r, 2, lv[1], {bg:'#0d0d0d', fg:'#c0ccd6', sz:10, bold:true});
      sheet.getRange(r, 3, 1, 3).merge().setValue(lv[2])
        .setBackground('#0d0d0d').setFontColor('#334455').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left');
      sheet.setRowHeight(r, 28); r++;
    });
    r++;
  }

  // ── FTMO Tracker ──────────────────────────────────────────────────────────
  mrow(sheet, r, 5, 'FTMO CHALLENGE TRACKER', {bg:'#0a1628', fg:'#334466', sz:10, bold:true}); r++;

  // Label row for user-editable cells
  cell(sheet, r, 6, '', {bg:'#000000', fg:'#000000', sz:8});  // spacer
  cell(sheet, r, 7, '← Edit G2/G3', {bg:'#0a1628', fg:'#334455', sz:8}); r++;

  var ftmoRows = [
    ['Current equity (MT5)',  '$' + Math.round(ftmo.equity).toLocaleString(), ftmo.equity >= FTMO_TARGET ? '#00e676' : ftmo.equity <= FTMO_FLOOR ? '#ff1744' : '#e0e0e0'],
    ['Day start equity',      '$' + Math.round(ftmo.dayStartEq).toLocaleString(), '#607d8b'],
    ['Day P&L',               (ftmo.dayPnl >= 0 ? '+' : '') + '$' + Math.round(Math.abs(ftmo.dayPnl)).toLocaleString(), ftmo.dayPnl >= 0 ? '#00c853' : '#ff5252'],
    ['Daily limit remaining', '$' + Math.round(Math.max(0, ftmo.dailyLeft)).toLocaleString(), ftmo.dailyBreached ? '#ff1744' : ftmo.dailyLeft < 2000 ? '#ff9800' : '#00c853'],
    ['Distance to floor',     '+$' + Math.round(Math.max(0, ftmo.toFloor)).toLocaleString(), ftmo.toFloor < 2000 ? '#ff1744' : ftmo.toFloor < 5000 ? '#ff9800' : '#00c853'],
    ['Target remaining',      '$' + Math.round(Math.max(0, ftmo.toTarget)).toLocaleString(), '#607d8b'],
    ['Challenge progress',    Math.max(0, Math.round(ftmo.pctDone)) + '%', ftmo.pctDone >= 100 ? '#00e676' : '#ffd700']
  ];

  ftmoRows.forEach(function(row) {
    cell(sheet, r, 1, row[0], {bg:'#080e1a', fg:'#445566', sz:10, bold:true});
    sheet.getRange(r, 2, 1, 4).merge().setValue(row[1])
      .setBackground('#080e1a').setFontColor(row[2]).setFontSize(13).setFontWeight('bold')
      .setVerticalAlignment('middle').setHorizontalAlignment('center');
    sheet.setRowHeight(r, 30); r++;
  });

  // Alert row
  if (ftmo.floorBreached) {
    mrow(sheet, r, 5, '🚨 FLOOR BREACHED — STOP ALL TRADING — $108k floor hit', {bg:'#3a0000', fg:'#ff1744', sz:12, bold:true, h:36}); r++;
  } else if (ftmo.dailyBreached) {
    mrow(sheet, r, 5, '⛔ DAILY LIMIT HIT — No more trades today (-$6k reached)', {bg:'#2a1000', fg:'#ff9800', sz:11, bold:true}); r++;
  } else if (ftmo.toFloor < 3000) {
    mrow(sheet, r, 5, '⚠️ WARNING: $' + Math.round(ftmo.toFloor) + ' from floor — reduce size or stop', {bg:'#2a1200', fg:'#ff9800', sz:10, bold:true}); r++;
  }
  r++;

  // ── Footer ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5,
    'London 07:30–09:00 UTC  ·  NY 14:00–16:30 UTC  ·  Skip NY on NFP Friday (1st Fri of month)',
    {bg:'#040810', fg:'#223344', sz:9}); r++;
  mrow(sheet, r, 5,
    'p=0.019 ✓ · Sharpe 1.42 · WR 51% · MDD 5.8% · +34.4% ROI (V6, 115 weeks, $120k, 1% risk)',
    {bg:'#040810', fg:'#1a2a3a', sz:9}); r++;
  mrow(sheet, r, 5,
    'SL=0.75×ATR · TP1=1.5×ATR close 60% then move SL to BE · TP2=2.5×ATR close 40%',
    {bg:'#040810', fg:'#1a2a3a', sz:9});

  // Set FTMO input cell labels (right side, col G)
  sheet.getRange('G1').setValue('FTMO INPUTS ↓').setFontColor('#334455').setFontSize(9).setBackground('#040810');
  sheet.getRange('G2').setBackground('#0a1a2a').setFontColor('#e0e0e0').setFontSize(11)
    .setFontWeight('bold').setHorizontalAlignment('center')
    .setNote('Enter your current MT5 account balance here (updates automatically if left blank)');
  sheet.getRange('G3').setBackground('#0a1a0a').setFontColor('#e0e0e0').setFontSize(11)
    .setFontWeight('bold').setHorizontalAlignment('center')
    .setNote('Enter your account balance at the START of today (reset each morning)');
  if (!sheet.getRange('G2').getValue()) sheet.getRange('G2').setValue(ACCOUNT);
  if (!sheet.getRange('G3').getValue()) sheet.getRange('G3').setValue(ACCOUNT);

  SpreadsheetApp.flush();
}

// ── CELL HELPERS ──────────────────────────────────────────────────────────────
function cell(sheet, row, col, val, opts) {
  var o   = opts || {};
  var rng = sheet.getRange(row, col);
  rng.setValue(val)
     .setBackground(o.bg || '#080e1a')
     .setFontColor(o.fg || '#c0ccd6')
     .setFontSize(o.sz || 11)
     .setFontWeight(o.bold ? 'bold' : 'normal')
     .setVerticalAlignment('middle')
     .setHorizontalAlignment('center')
     .setWrap(true);
  if (o.h) sheet.setRowHeight(row, o.h);
}

function mrow(sheet, row, cols, val, opts) {
  var o = opts || {};
  sheet.getRange(row, 1, 1, cols).merge()
       .setValue(val)
       .setBackground(o.bg || '#080e1a')
       .setFontColor(o.fg || '#c0ccd6')
       .setFontSize(o.sz || 11)
       .setFontWeight(o.bold ? 'bold' : 'normal')
       .setVerticalAlignment('middle')
       .setHorizontalAlignment('center')
       .setWrap(true);
  sheet.setRowHeight(row, o.h || 30);
}
