/**
 * FTMO PORTFOLIO SIGNAL DASHBOARD
 * =================================
 * Pairs  : CAD/CHF (long + short)  |  AUD/JPY (LONG ONLY)  |  CAD/JPY (LONG ONLY)
 * Account: $120,000  |  Target: $132,000 (+10%)  |  Floor: $108,000 (-10%)
 * Daily limit: -$6,000
 *
 * V13 Portfolio Stats (Sep 2024 – May 2026):
 *   ROI +109.8%  |  Sharpe 2.94  |  Max DD 9.6%  |  p=0.0001 ✓  |  Floor ✓  |  1.61 trades/week
 *   TREND: CAD/CHF 2%  WR 63%  avgR +0.900  (long + short)  — 4 technical filters
 *          AUD/JPY 1.5% WR 56%  avgR +0.633  (LONG ONLY — carry trade)
 *          CAD/JPY 1%   WR 55%  avgR +0.697  (LONG ONLY — carry trade)
 *   ORB:   GBP/JPY 1%   WR 57%  avgR +0.137  (LONG ONLY — London open breakout)
 *
 * V13 TECHNICAL FILTERS (all 4 must pass for entry):
 *   1. MACD(12,26,9) histogram > 0  (strict momentum confirmation)
 *   2. RSI(14) 15m < 55 for longs, > 45 for shorts  (pullback zone)
 *   3. Volume ≥ 120% of 20-bar average  (volume surge)
 *   4. OBV > OBV EMA10  (cumulative flow aligned)
 *
 * V13 REGIME ENGINE (daily, from macro EMAs):
 *   BULL:      Oil>EMA50 + Copper>EMA50 + VIX<EMA20 + SPX>EMA50 → 1.0× size, all pairs
 *   OIL_WEAK:  Oil<EMA50 → skip CAD/JPY, CAD/CHF short threshold −4, 0.75× size
 *   VIX_SPIKE: VIX >10% above EMA20 → skip AUD/JPY + CAD/JPY, 0.50× size
 *   NORMAL:    catch-all → 1.0× size, all pairs
 *
 * DUBAI SESSION WINDOWS (UTC+4):
 *   10:30 – 11:30  PRE-LONDON ORB RANGE  → GBP/JPY range building
 *   11:30 – 13:00  LONDON ORB ENTRY      → GBP/JPY breakout entry window
 *   11:30 – 14:00  LONDON TREND          → CAD/CHF  AUD/JPY  CAD/JPY
 *   18:00 – 21:00  NY TREND              → CAD/CHF  AUD/JPY  CAD/JPY
 *
 * WHEN TO CHECK (Dubai time):
 *   10:15  Pre-ORB     — Check Asian session bias for GBP/JPY
 *   11:15  Pre-London  — All pairs, most important check of day
 *   11:30  London open — ORB breakout trigger + trend entries begin
 *   18:00  NY opens    — Trend pairs evening check
 *   21:00  NY closes   — Manage any open trades
 *
 * SETUP:
 *   1. sheets.new → Extensions → Apps Script → paste → Save
 *   2. Run setupTrigger() once (authorize when prompted)
 *   3. Run updateDashboard() to populate immediately
 *   4. Each trading day: enter MT5 balance in G2, day-start balance in G3
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var TWELVE_KEY   = "06f050cd7d9940a895f9461e7f0ff7e3";
var ACCOUNT      = 120000;
var FTMO_FLOOR   = 108000;
var FTMO_TARGET  = 132000;
var FTMO_DAILY   = 6000;
var SL_MULT      = 0.75;
var TP1_MULT     = 1.50;
var TP2_MULT     = 2.50;
var TP1_FRAC     = 0.60;
var TP2_FRAC     = 0.40;

// Risk tiers: sized by statistical confidence of each pair's edge
var RISK_TIER = { 'CADCHF': 0.020, 'AUDJPY': 0.015, 'CADJPY': 0.010 };

// Pip sizes (JPY pairs use 0.01, others use 0.0001)
var PIP_SIZE = { 'CADCHF': 0.0001, 'AUDJPY': 0.01, 'CADJPY': 0.01, 'GBPJPY': 0.01 };

// Pip value per standard lot in USD (approximations — verify in MT4 Market Watch)
// CAD/CHF: 10 CHF/pip × CHF/USD ≈ $8.50
// AUD/JPY & CAD/JPY & GBP/JPY: 1000 JPY/pip ÷ USD/JPY — ~$6.70-$7.40 depending on rate
var PIP_VALUE = { 'CADCHF': 8.50, 'AUDJPY': 6.70, 'CADJPY': 6.70, 'GBPJPY': 7.40 };

// ATR validity range (atr/price ratio) — outside = skip noisy/illiquid bars
var ATR_RANGE = { 'CADCHF': [0.0002, 0.0080], 'AUDJPY': [0.0004, 0.0120], 'CADJPY': [0.0004, 0.0120] };

// Long-only pairs: carry trade drift — never short JPY pairs (BoJ shock recovery)
var LONG_ONLY = { 'AUDJPY': true, 'CADJPY': true };

// Sessions (UTC decimal hours)
var SESS_LONDON = { vwap: 7.0,  open: 7.5,  close: 10.0 };
var SESS_NY     = { vwap: 13.5, open: 14.0, close: 17.0 };

// ORB (Opening Range Breakout) config — GBP/JPY only, London open
var ORB_RANGE_START_H = 6.5;    // 06:30 UTC = 10:30 DXB — pre-London range begins
var ORB_RANGE_END_H   = 7.5;    // 07:30 UTC = 11:30 DXB — London opens, range locked
var ORB_ENTRY_END_H   = 9.0;    // 09:00 UTC = 13:00 DXB — last ORB entry bar
var ORB_MIN_BARS      = 3;      // minimum 15m bars to define valid range
var ORB_BREAKOUT_CONF = 0.10;   // price must breach 10% of range beyond edge
var ORB_RISK          = 0.010;  // 1% risk per ORB trade
var ORB_TP1_MULT      = 1.5;    // TP1 = entry + 1.5×range
var ORB_TP2_MULT      = 2.5;    // TP2 = entry + 2.5×range

// Dubai offset (UTC+4, no DST — UAE never changes clocks)
var DUBAI_OFFSET_H = 4;

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ Auto-refresh every 5 minutes.\n\n' +
      'Run updateDashboard() to load now.\n\n' +
      'Each trading day:\n' +
      '  G2 = your current MT5 equity\n' +
      '  G3 = equity at start of today\n\n' +
      'Dubai check times:\n' +
      '  10:15  Pre-ORB — Asian bias for GBP/JPY\n' +
      '  11:15  Pre-London — all pairs + ORB range set\n' +
      '  11:30  London open — ORB breakout + TREND entries\n' +
      '  18:00  NY opens (TREND pairs only)\n' +
      '  21:00  NY closes (manage open trades)'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('PORTFOLIO SIGNAL') || ss.insertSheet('PORTFOLIO SIGNAL');

  var now    = new Date();
  var nowUtc = now.getUTCHours() + now.getUTCMinutes() / 60;

  // FTMO equity inputs (user edits these each day)
  var curEq      = dash.getRange('G2').getValue() || ACCOUNT;
  var dayStartEq = dash.getRange('G3').getValue() || ACCOUNT;

  // Fetch all macro data once (shared across pairs, cached 6h)
  var macroData = fetchAllMacro();

  // Compute daily regime (BULL / OIL_WEAK / VIX_SPIKE / NORMAL)
  var regime = calcRegime(macroData);

  // Per-pair TREND signals (CAD/CHF, AUD/JPY, CAD/JPY)
  var pairs    = ['CADCHF', 'AUDJPY', 'CADJPY'];
  var results  = {};

  pairs.forEach(function(p, idx) {
    // Space Twelve Data requests to respect 8 req/min free tier
    if (idx > 0) Utilities.sleep(9000);
    var sym15 = tdSymbol(p);
    var bars15 = fetchBars(sym15, '15min', 80);
    Utilities.sleep(9000);
    var bars1h = fetchBars(sym15, '1h', 260);

    var macro  = calcMacroForPair(p, macroData);
    var tech   = buildTechnicals(p, bars15, bars1h, nowUtc);
    var setup  = evalSetup(p, macro, tech, nowUtc, macroData, regime);
    var trade  = setup.active ? calcTrade(p, setup, tech, curEq, regime) : null;
    results[p] = { macro: macro, tech: tech, setup: setup, trade: trade };
  });

  // GBP/JPY ORB signal (London open breakout)
  Utilities.sleep(9000);
  var gbpjpyBars15 = fetchBars('GBP/JPY', '15min', 100);
  var orbSignal = evalOrbSignal(gbpjpyBars15, nowUtc, macroData, curEq, regime);

  var ftmo = calcFtmo(curEq, dayStartEq);

  writeSheet(dash, now, nowUtc, results, orbSignal, ftmo, curEq, dayStartEq, regime);
}

// ── TWELVE DATA SYMBOL MAP ────────────────────────────────────────────────────
function tdSymbol(p) {
  return { 'CADCHF': 'CAD/CHF', 'AUDJPY': 'AUD/JPY', 'CADJPY': 'CAD/JPY' }[p];
}

// ── GBP/JPY ORB SIGNAL ────────────────────────────────────────────────────────
function evalOrbSignal(bars15, nowUtcH, macroData, curEq, regime) {
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var places = 3;  // JPY pair

  function barDay(b) { return b.dt.substring(0, 10); }
  function barHour(b) {
    var t = b.dt.length > 10 ? b.dt.substring(11) : '00:00:00';
    var parts = t.split(':');
    return parseInt(parts[0]) + parseInt(parts[1]) / 60;
  }

  // 1. Asian session (00:00-06:30 UTC = 04:00-10:30 DXB)
  var asianBars = (bars15 || []).filter(function(b) {
    var h = barHour(b);
    return barDay(b) === today && h >= 0 && h < ORB_RANGE_START_H;
  });
  var asianHigh = null, asianLow = null, asianClose = null, asianBias = 0;
  if (asianBars.length >= 4) {
    asianHigh  = Math.max.apply(null, asianBars.map(function(b){ return b.h; }));
    asianLow   = Math.min.apply(null, asianBars.map(function(b){ return b.l; }));
    asianClose = asianBars[asianBars.length - 1].c;
    asianBias  = asianClose > (asianHigh + asianLow) / 2 ? 1 : -1;
  }

  // 2. ORB range (06:30-07:30 UTC = 10:30-11:30 DXB)
  var rangeBars = (bars15 || []).filter(function(b) {
    var h = barHour(b);
    return barDay(b) === today && h >= ORB_RANGE_START_H && h < ORB_RANGE_END_H;
  });
  var rangeHigh = null, rangeLow = null, orbRange = null;
  if (rangeBars.length >= ORB_MIN_BARS) {
    rangeHigh = Math.max.apply(null, rangeBars.map(function(b){ return b.h; }));
    rangeLow  = Math.min.apply(null, rangeBars.map(function(b){ return b.l; }));
    orbRange  = rangeHigh - rangeLow;
  }

  // 3. Breakout detection (07:30-09:00 UTC entry window)
  var inEntry = nowUtcH >= ORB_RANGE_END_H && nowUtcH <= ORB_ENTRY_END_H;
  var lastBar = (bars15 && bars15.length) ? bars15[bars15.length - 1] : null;
  var price   = lastBar ? lastBar.c : null;

  var breakLong = false, breakShort = false, direction = 0;
  if (rangeHigh && price && inEntry && orbRange > 0) {
    var conf = ORB_BREAKOUT_CONF * orbRange;
    breakLong  = price > rangeHigh + conf;
    breakShort = price < rangeLow  - conf;
    direction  = breakLong ? 1 : (breakShort ? -1 : 0);
  }

  // 4. GBP/JPY is LONG ONLY (carry trade)
  if (direction === -1) direction = 0;

  // 5. Asian bias filter — skip LONG if Asian session was bearish
  var asianBlock = (direction === 1 && asianBias === -1);
  if (asianBlock) direction = 0;

  // 6. JPY carry guard
  var carryOk = true, carryNote = '';
  if (direction === 1) {
    var cg = jpyCarryGuard(macroData);
    carryOk   = !cg.blocked;
    carryNote = cg.note;
    if (!carryOk) direction = 0;
  }

  // 7. V13 technical filters for ORB LONG
  var orbTechChecks = [];
  var orbTechBlocked = false;
  if (direction === 1 && bars15 && bars15.length >= 35) {
    var c15orb   = bars15.map(function(b){ return b.c; });
    var v15orb   = bars15.map(function(b){ return b.v || 0; });
    // MACD histogram > 0
    var orbMacd  = calcMacdHistogram(c15orb);
    var orbMacdOk = orbMacd !== null ? orbMacd > 0 : true;
    orbTechChecks.push({ label: 'MACD hist > 0', ok: orbMacdOk,
      note: orbMacd !== null ? 'hist=' + orbMacd.toFixed(6) : '—' });
    // RSI 15m > 50 (momentum building for breakout)
    var orbRsi   = calcRsi14(c15orb);
    var orbRsiOk = orbRsi !== null ? orbRsi > 50 : true;
    orbTechChecks.push({ label: 'RSI(14) 15m > 50', ok: orbRsiOk,
      note: orbRsi !== null ? 'RSI=' + orbRsi.toFixed(1) : '—' });
    // Volume ≥ 120%
    var orbVolN  = Math.min(20, v15orb.length - 1), orbVolSum = 0;
    for (var ovi = v15orb.length - 1 - orbVolN; ovi < v15orb.length - 1; ovi++) orbVolSum += v15orb[ovi];
    var orbAvgVol = orbVolN > 0 ? orbVolSum / orbVolN : 0;
    var orbVolRatio = orbAvgVol > 0 ? v15orb[v15orb.length-1] / orbAvgVol : null;
    var orbVolOk = orbVolRatio !== null ? orbVolRatio >= 1.20 : true;
    orbTechChecks.push({ label: 'Volume ≥ 120% avg', ok: orbVolOk,
      note: orbVolRatio !== null ? (orbVolRatio*100).toFixed(0) + '%' : 'no data' });
    // OBV > EMA10
    var orbObv   = calcObvAboveEma(c15orb, v15orb);
    var orbObvOk = orbObv !== null ? orbObv === true : true;
    orbTechChecks.push({ label: 'OBV > OBV EMA10', ok: orbObvOk,
      note: orbObv !== null ? (orbObv ? 'bullish flow ✓' : 'bearish flow ✗') : '—' });

    if (!orbMacdOk || !orbRsiOk || !orbVolOk || !orbObvOk) {
      orbTechBlocked = true;
      direction = 0;
    }
  }

  // 8. Regime size adjustment for ORB
  var orbSizeAdj = (regime && regime.sizeAdj) ? regime.sizeAdj : 1.0;

  // 9. Build trade if signal active
  var trade = null;
  if (direction === 1 && rangeHigh && price && rangeLow) {
    var slPx   = rangeLow;
    var tp1Px  = price + ORB_TP1_MULT * orbRange;
    var tp2Px  = price + ORB_TP2_MULT * orbRange;
    var slDist = price - slPx;
    var slPips = Math.round(slDist / PIP_SIZE['GBPJPY']);
    var risk   = curEq * ORB_RISK * orbSizeAdj;
    var lots   = slPips > 0 ? Math.max(0.01, Math.round(risk / (slPips * PIP_VALUE['GBPJPY']) * 100) / 100) : 0;
    trade = {
      dir: 'LONG', entry: price,
      sl: slPx, tp1: tp1Px, tp2: tp2Px,
      slPips:  slPips,
      tp1Pips: Math.round(ORB_TP1_MULT * orbRange / PIP_SIZE['GBPJPY']),
      tp2Pips: Math.round(ORB_TP2_MULT * orbRange / PIP_SIZE['GBPJPY']),
      rangePips: Math.round(orbRange / PIP_SIZE['GBPJPY']),
      lots: lots,
      lots1: Math.max(0.01, Math.round(lots * TP1_FRAC * 100) / 100),
      lots2: Math.max(0.01, Math.round(lots * TP2_FRAC * 100) / 100),
      risk: risk, places: places
    };
  }

  // Status label
  var status;
  if (orbTechBlocked) {
    var failedChecks = orbTechChecks.filter(function(c){ return !c.ok; }).map(function(c){ return c.label; });
    status = 'BLOCKED — V13 filters failed: ' + failedChecks.join(', ');
  } else if (nowUtcH < ORB_RANGE_START_H) {
    status = 'WAITING — Asian session in progress (range builds at 10:30 DXB)';
  } else if (nowUtcH < ORB_RANGE_END_H) {
    status = 'BUILDING RANGE — ' + (rangeBars.length) + ' bars so far (10:30–11:30 DXB)';
  } else if (!inEntry) {
    status = 'ENTRY WINDOW CLOSED (after 13:00 DXB)';
  } else if (!rangeHigh) {
    status = 'NO RANGE — insufficient bars during 10:30–11:30 DXB';
  } else if (direction === 1) {
    status = 'ACTIVE BREAKOUT — LONG GBP/JPY';
  } else if (asianBlock) {
    status = 'BLOCKED — Asian session bearish (no long ORB)';
  } else if (!carryOk) {
    status = 'BLOCKED — JPY carry guard (JPY surging)';
  } else {
    status = 'IN WINDOW — waiting for breakout above ' + (rangeHigh + ORB_BREAKOUT_CONF * orbRange).toFixed(places);
  }

  return {
    asianBias: asianBias, asianHigh: asianHigh, asianLow: asianLow, asianClose: asianClose,
    rangeHigh: rangeHigh, rangeLow: rangeLow, orbRange: orbRange,
    rangeBarsCount: rangeBars.length, inEntry: inEntry,
    breakLong: breakLong, breakShort: breakShort, asianBlock: asianBlock,
    direction: direction, price: price, carryOk: carryOk, carryNote: carryNote,
    orbTechChecks: orbTechChecks, orbTechBlocked: orbTechBlocked,
    trade: trade, status: status, nowUtcH: nowUtcH, places: places
  };
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

// ── FETCH ALL MACRO DATA (Yahoo Finance, cached 6h) ───────────────────────────
function fetchAllMacro() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key    = 'portfolio_macro_' + today;
  var cached = cache.get(key);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yf(sym, range) {
    try {
      var url = 'https://query1.finance.yahoo.com/v8/finance/chart/' +
                encodeURIComponent(sym) + '?interval=1d&range=' + range;
      var r   = UrlFetchApp.fetch(url, {
        muteHttpExceptions: true,
        headers: {'User-Agent': 'Mozilla/5.0'}
      });
      var j = JSON.parse(r.getContentText());
      return j.chart.result[0].indicators.quote[0].close
             .filter(function(c) { return c != null; });
    } catch(e) { return null; }
  }

  var data = {
    cadchf: yf('CADCHF=X', '2y'),
    audjpy: yf('AUDJPY=X', '2y'),
    cadjpy: yf('CADJPY=X', '2y'),
    gbpjpy: yf('GBPJPY=X', '2y'),
    oil:    yf('CL=F',     '1y'),
    cop:    yf('HG=F',     '1y'),
    spx:    yf('^GSPC',    '1y'),
    vix:    yf('^VIX',     '6mo'),
    tnx:    yf('^TNX',     '1y'),
    usdjpy: yf('JPY=X',    '1y')
  };

  cache.put(key, JSON.stringify(data), 21600);
  return data;
}

// ── REGIME ENGINE ─────────────────────────────────────────────────────────────
function calcRegime(d) {
  var oil = d.oil, vix = d.vix, cop = d.cop, spx = d.spx;
  var oilWeak = false, vixSpike = false, copBull = false, spxBull = false;
  var oilNote = '—', vixNote = '—', copNote = '—', spxNote = '—';

  if (oil && oil.length >= 52) {
    var oe = calcEma(oil, 50); var om = oil.length;
    oilWeak = oil[om-2] < oe[om-2];
    oilNote = 'Oil $' + oil[om-2].toFixed(1) + ' vs EMA50 $' + oe[om-2].toFixed(1) +
              (oilWeak ? ' → WEAK ↓' : ' → OK ↑');
  }
  if (vix && vix.length >= 22) {
    var ve = calcEma(vix, 20); var vm = vix.length;
    vixSpike = vix[vm-2] > ve[vm-2] * 1.10;
    vixNote = 'VIX ' + vix[vm-2].toFixed(1) + ' vs EMA20×1.1 ' + (ve[vm-2]*1.10).toFixed(1) +
              (vixSpike ? ' → SPIKE ⚠' : ' → calm ✓');
  }
  if (cop && cop.length >= 52) {
    var ce = calcEma(cop, 50); var cm = cop.length;
    copBull = cop[cm-2] > ce[cm-2];
    copNote = 'Copper $' + cop[cm-2].toFixed(2) + ' vs EMA50 $' + ce[cm-2].toFixed(2);
  }
  if (spx && spx.length >= 52) {
    var se = calcEma(spx, 50); var sm = spx.length;
    spxBull = spx[sm-2] > se[sm-2];
    spxNote = 'SPX ' + Math.round(spx[sm-2]) + ' vs EMA50 ' + Math.round(se[sm-2]);
  }

  var bull = !oilWeak && !vixSpike && copBull && spxBull;
  var components = [
    { name: 'Oil vs EMA50', note: oilNote, ok: !oilWeak },
    { name: 'VIX vs EMA20×1.1', note: vixNote, ok: !vixSpike },
    { name: 'Copper vs EMA50', note: copNote, ok: copBull },
    { name: 'SPX vs EMA50', note: spxNote, ok: spxBull }
  ];

  if (vixSpike) return {
    label: 'VIX_SPIKE', color: '#ff5252', bg: '#2a0a0a',
    description: 'VIX >10% above EMA20 — skip AUD/JPY + CAD/JPY — size 0.50×',
    sizeAdj: 0.50, skipPairs: { AUDJPY: true, CADJPY: true }, cadchfShortThresh: -6,
    components: components
  };
  if (oilWeak) return {
    label: 'OIL_WEAK', color: '#ff9800', bg: '#2a1a00',
    description: 'Oil below EMA50 — skip CAD/JPY — CAD/CHF short at −4 — size 0.75×',
    sizeAdj: 0.75, skipPairs: { CADJPY: true }, cadchfShortThresh: -4,
    components: components
  };
  if (bull) return {
    label: 'BULL', color: '#00e676', bg: '#0a2a14',
    description: 'Full bull regime — all pairs active — size 1.0×',
    sizeAdj: 1.0, skipPairs: {}, cadchfShortThresh: -6,
    components: components
  };
  return {
    label: 'NORMAL', color: '#00bcd4', bg: '#0a1628',
    description: 'Normal regime — all pairs active — size 1.0×',
    sizeAdj: 1.0, skipPairs: {}, cadchfShortThresh: -6,
    components: components
  };
}

// ── CALC MACRO SCORE PER PAIR ─────────────────────────────────────────────────
function calcMacroForPair(pairKey, d) {
  var pairPrices = d[pairKey.toLowerCase()];
  if (!pairPrices || pairPrices.length < 60)
    return { score: 0, dir: 0, rawScore: 0, gated: false, components: [], error: 'Insufficient data' };

  var n    = pairPrices.length;
  var p    = pairPrices;
  var e20  = calcEma(p, 20);
  var e50  = calcEma(p, 50);
  var e200 = calcEma(p, 200);

  // s1: pair vs EMA50
  var s1 = p[n-2] > e50[n-2] ? 1 : -1;
  // s5: pair vs EMA200
  var s5 = e200.length >= 200 ? (p[n-2] > e200[n-2] ? 1 : -1) : 0;
  // V6 EMA20 gate: suppress signal when pair on wrong side
  var gate = p[n-2] > e20[n-2] ? 1 : -1;

  var s2 = 0, s3 = 0, s4 = 0;
  var s2note = '—', s3note = '—', s4note = '—';
  var s2name = '—', s3name = '—', s4name = '—';

  if (pairKey === 'CADCHF') {
    // s2: WTI oil (up → CAD strong)
    s2name = 'WTI Oil vs EMA50'; var oil = d.oil;
    if (oil && oil.length >= 52) {
      var oe = calcEma(oil, 50); var om = oil.length;
      s2 = oil[om-2] > oe[om-2] ? 1 : -1;
      s2note = 'Oil $' + oil[om-2].toFixed(0) + ' vs EMA50 $' + oe[om-2].toFixed(0) +
               (s2>0 ? ' → CAD supported ↑' : ' → CAD weak ↓');
    }
    // s3: SPX (risk-on → CHF weaker → CADCHF up)
    s3name = 'S&P 500 vs EMA50'; var spx = d.spx;
    if (spx && spx.length >= 52) {
      var se = calcEma(spx, 50); var sm = spx.length;
      s3 = spx[sm-2] > se[sm-2] ? 1 : -1;
      s3note = 'SPX ' + spx[sm-2].toFixed(0) + ' vs EMA50 ' + se[sm-2].toFixed(0) +
               (s3>0 ? ' → risk-on ↑' : ' → risk-off ↓');
    }
    // s4: VIX INVERTED (low VIX → CHF weak → CADCHF up)
    s4name = 'VIX calm (inverted)'; var vix = d.vix;
    if (vix && vix.length >= 22) {
      var ve = calcEma(vix, 20); var vm = vix.length;
      s4 = vix[vm-2] < ve[vm-2] ? 1 : -1;
      s4note = 'VIX ' + vix[vm-2].toFixed(1) + ' vs EMA20 ' + ve[vm-2].toFixed(1) +
               (s4>0 ? ' → CHF weak (calm mkt)' : ' → CHF bid (fear up)');
    }

  } else if (pairKey === 'AUDJPY') {
    // s2: Copper (up → AUD strong, commodity/China proxy)
    s2name = 'Copper vs EMA50'; var cop = d.cop;
    if (cop && cop.length >= 52) {
      var ce = calcEma(cop, 50); var cm = cop.length;
      s2 = cop[cm-2] > ce[cm-2] ? 1 : -1;
      s2note = 'Copper $' + cop[cm-2].toFixed(2) + ' vs EMA50 $' + ce[cm-2].toFixed(2) +
               (s2>0 ? ' → AUD supported ↑' : ' → AUD weak ↓');
    }
    // s3: SPX (risk-on → AUD up, JPY down)
    s3name = 'S&P 500 vs EMA50';
    if (d.spx && d.spx.length >= 52) {
      var se2 = calcEma(d.spx, 50); var sm2 = d.spx.length;
      s3 = d.spx[sm2-2] > se2[sm2-2] ? 1 : -1;
      s3note = 'SPX ' + d.spx[sm2-2].toFixed(0) + ' vs EMA50 ' + se2[sm2-2].toFixed(0) +
               (s3>0 ? ' → risk-on, JPY weak ↑' : ' → risk-off, JPY bid ↓');
    }
    // s4: US 10Y yield momentum (up → carry trade favourable)
    s4name = 'US 10Y yield (10d mom)'; var tnx = d.tnx;
    if (tnx && tnx.length >= 12) {
      var tm = tnx.length;
      s4 = tnx[tm-2] > tnx[tm-12] ? 1 : -1;
      s4note = 'TNX ' + tnx[tm-2].toFixed(2) + '% vs 10d ago ' + tnx[tm-12].toFixed(2) +
               '% → carry ' + (s4>0 ? 'improving ↑' : 'fading ↓');
    }

  } else { // CADJPY
    // s2: WTI oil (up → CAD strong)
    s2name = 'WTI Oil vs EMA50';
    if (d.oil && d.oil.length >= 52) {
      var oe2 = calcEma(d.oil, 50); var om2 = d.oil.length;
      s2 = d.oil[om2-2] > oe2[om2-2] ? 1 : -1;
      s2note = 'Oil $' + d.oil[om2-2].toFixed(0) + ' vs EMA50 $' + oe2[om2-2].toFixed(0) +
               (s2>0 ? ' → CAD supported ↑' : ' → CAD weak ↓');
    }
    // s3: US 10Y yield momentum
    s3name = 'US 10Y yield (10d mom)';
    if (d.tnx && d.tnx.length >= 12) {
      var tm2 = d.tnx.length;
      s3 = d.tnx[tm2-2] > d.tnx[tm2-12] ? 1 : -1;
      s3note = 'TNX ' + d.tnx[tm2-2].toFixed(2) + '% vs 10d ago ' + d.tnx[tm2-12].toFixed(2) +
               '% → carry ' + (s3>0 ? 'improving ↑' : 'fading ↓');
    }
    // s4: SPX
    s4name = 'S&P 500 vs EMA50';
    if (d.spx && d.spx.length >= 52) {
      var se3 = calcEma(d.spx, 50); var sm3 = d.spx.length;
      s4 = d.spx[sm3-2] > se3[sm3-2] ? 1 : -1;
      s4note = 'SPX ' + d.spx[sm3-2].toFixed(0) + ' vs EMA50 ' + se3[sm3-2].toFixed(0) +
               (s4>0 ? ' → risk-on ↑' : ' → risk-off ↓');
    }
  }

  var raw   = s1 + s2 + s3 + s4 + s5;
  var score = Math.round(raw / 5 * 100) / 10;
  var gated = (raw !== 0 && Math.sign(score) !== Math.sign(gate));
  var gatedScore = gated ? 0 : score;
  var dir   = gatedScore >= 6 ? 1 : gatedScore <= -6 ? -1 : 0;

  // Long-only constraint: never fire short for JPY pairs
  if (LONG_ONLY[pairKey] && dir === -1) dir = 0;

  return {
    score:     gatedScore,
    rawScore:  score,
    dir:       dir,
    gated:     gated,
    gate:      gate,
    pairPrice: p[n-1],
    ema20:     e20[n-2],
    ema50:     e50[n-2],
    ema200:    e200.length >= 200 ? e200[n-2] : null,
    components: [
      { name: pairKey.slice(0,3)+'/'+pairKey.slice(3)+' vs EMA50', val: s1,
        note: p[n-2].toFixed(pairKey.slice(3)==='JPY'?3:5) +
              ' vs EMA50 ' + e50[n-2].toFixed(pairKey.slice(3)==='JPY'?3:5) +
              (s1>0?' → uptrend'  :' → downtrend') },
      { name: s2name, val: s2, note: s2note },
      { name: s3name, val: s3, note: s3note },
      { name: s4name, val: s4, note: s4note },
      { name: pairKey.slice(0,3)+'/'+pairKey.slice(3)+' vs EMA200', val: s5,
        note: e200.length >= 200
          ? p[n-2].toFixed(pairKey.slice(3)==='JPY'?3:5) +
            ' vs EMA200 ' + e200[n-2].toFixed(pairKey.slice(3)==='JPY'?3:5) +
            (s5>0?' → bull regime':' → bear regime')
          : 'Insufficient history (need 200 days)' }
    ],
    error: null
  };
}

// ── JPY CARRY GUARD ───────────────────────────────────────────────────────────
function jpyCarryGuard(macroData) {
  var usdjpy = macroData.usdjpy;
  if (!usdjpy || usdjpy.length < 7) return { blocked: false, note: 'No USDJPY data' };
  var n       = usdjpy.length;
  var recent  = usdjpy[n-1];
  var prev5   = usdjpy[n-6];
  var chg     = (recent - prev5) / prev5;
  var blocked = chg < -0.02;
  return {
    blocked: blocked,
    chg: chg,
    note: 'USDJPY ' + (chg*100).toFixed(1) + '% over 5 days' +
          (blocked ? ' → JPY surging, CARRY GUARD BLOCKED ⚠️' : ' → JPY stable ✓')
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

  // V13 technical filters — 15m indicators
  var macdHist = calcMacdHistogram(c15);
  var rsi15m   = calcRsi14(c15);

  // Volume ratio: last bar / 20-bar rolling average
  var vols15  = bars15.map(function(b){ return b.v || 0; });
  var volSum  = 0;
  var volN    = Math.min(20, vols15.length - 1);
  for (var vi = vols15.length - 1 - volN; vi < vols15.length - 1; vi++) volSum += vols15[vi];
  var avgVol  = volN > 0 ? volSum / volN : 0;
  var volRatio = (avgVol > 0) ? (vols15[vols15.length - 1] / avgVol) : null;

  // OBV above EMA10
  var obvAboveEma = calcObvAboveEma(c15, vols15);

  var e9_1h = null, e21_1h = null, e50_1h = null, rsi = null;
  var e9_4h = null, e21_4h = null;

  if (bars1h && bars1h.length >= 55) {
    var c1h  = bars1h.map(function(b){ return b.c; });
    var ea9  = calcEma(c1h, 9);
    var ea21 = calcEma(c1h, 21);
    var ea50 = calcEma(c1h, 50);
    e9_1h    = ea9[ea9.length-1];
    e21_1h   = ea21[ea21.length-1];
    e50_1h   = ea50[ea50.length-1];
    rsi      = calcRsi14(c1h);

    var b4h  = resample4h(bars1h);
    if (b4h.length >= 22) {
      var c4h  = b4h.map(function(b){ return b.c; });
      var f9   = calcEma(c4h, 9);
      var f21  = calcEma(c4h, 21);
      e9_4h    = f9[f9.length-1];
      e21_4h   = f21[f21.length-1];
    }
  }

  // Session VWAP: London or NY only
  var activeVwapH = nowUtcH >= SESS_NY.vwap ? SESS_NY.vwap : SESS_LONDON.vwap;
  var sv = calcSessionVwap(bars15, activeVwapH);

  return {
    price: price, high: last.h, low: last.l,
    ema9:  e9[e9.length-1],   ema21: e21[e21.length-1],
    atr:   atrV,               atrPct: atrV / price,
    atrPips: atrV / PIP_SIZE[pairKey],
    e9_1h: e9_1h,  e21_1h: e21_1h,  e50_1h: e50_1h,
    e9_4h: e9_4h,  e21_4h: e21_4h,
    rsi:   rsi,
    vwap:  sv.vwap, vwapStd: sv.std, vwapBars: sv.n,
    vwapDist: sv.std > 1e-8 && sv.vwap ? (price - sv.vwap) / sv.std : 99,
    // V13 technical filter values
    macdHist:   macdHist,
    rsi15m:     rsi15m,
    volRatio:   volRatio,
    avgVol:     avgVol,
    lastVol:    vols15[vols15.length - 1],
    obvAboveEma: obvAboveEma
  };
}

// ── SESSION VWAP ──────────────────────────────────────────────────────────────
function calcSessionVwap(bars15, vwapStartH) {
  var now  = new Date();
  var ymd  = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var hh   = pad(Math.floor(vwapStartH));
  var mm   = pad(Math.round((vwapStartH % 1) * 60));
  var open = new Date(ymd + 'T' + hh + ':' + mm + ':00Z');
  var sess = bars15.filter(function(b) {
    return new Date(b.dt.replace(' ', 'T') + 'Z') >= open;
  });
  if (sess.length < 2) return { vwap: 0, std: 1e-5, n: 0 };
  var tp  = sess.map(function(b){ return (b.h + b.l + b.c) / 3; });
  var avg = tp.reduce(function(a,v){ return a+v; }, 0) / tp.length;
  var v2  = tp.reduce(function(a,x){ return a + Math.pow(x-avg,2); }, 0) / tp.length;
  return { vwap: avg, std: Math.max(Math.sqrt(v2), 1e-5), n: tp.length };
}

// ── EVAL SETUP PER PAIR ───────────────────────────────────────────────────────
function evalSetup(pairKey, macro, tech, nowUtcH, macroData, regime) {
  // Regime pair exclusion
  if (regime && regime.skipPairs && regime.skipPairs[pairKey])
    return { active: false, dir: null,
             reason: 'SKIPPED — regime ' + regime.label + ' excludes ' + pairKey,
             checks: [] };

  // Apply OIL_WEAK lower short threshold for CAD/CHF
  var shortThresh = (regime && regime.cadchfShortThresh) ? regime.cadchfShortThresh : -6;
  var adjustedDir = macro.dir;
  if (pairKey === 'CADCHF' && macro.score <= shortThresh && macro.score > -6) adjustedDir = -1;

  if (!tech || macro.error || (macro.dir === 0 && adjustedDir === 0))
    return { active: false, dir: null,
             reason: macro.error || (macro.gated ? 'EMA20 gate — pair on wrong side of trend' : 'No macro signal (|score| < 6)'),
             checks: [] };

  var dir    = adjustedDir || macro.dir;   // 1 = LONG, -1 = SHORT
  var price  = tech.price;
  var isJpy  = LONG_ONLY[pairKey];

  // Which sessions are valid for this pair? London + NY only
  var inEntry = false, sessName = '';
  if (nowUtcH >= SESS_LONDON.open && nowUtcH <= SESS_LONDON.close) {
    inEntry = true; sessName = 'LONDON';
  } else if (nowUtcH >= SESS_NY.open && nowUtcH <= SESS_NY.close) {
    // Skip NY on NFP (first Friday of month)
    var now2 = new Date();
    var isNfp = now2.getUTCDay() === 5 && now2.getUTCDate() <= 7;
    // Skip NY on high-VIX Wednesday (when VIX > 25)
    var vixNow = (macroData.vix && macroData.vix.length) ? macroData.vix[macroData.vix.length-1] : 0;
    var isVixWed = now2.getUTCDay() === 3 && vixNow > 25;
    if (!isNfp && !isVixWed) { inEntry = true; sessName = 'NY'; }
  }

  // Checks (all must pass for active signal)
  var h4ok  = (tech.e9_4h && tech.e21_4h)
    ? (dir===1 ? tech.e9_4h > tech.e21_4h : tech.e9_4h < tech.e21_4h) : null;
  var h1ok  = (tech.e9_1h && tech.e21_1h)
    ? (dir===1 ? tech.e9_1h > tech.e21_1h : tech.e9_1h < tech.e21_1h) : null;
  var e50ok = tech.e50_1h
    ? (dir===1 ? price > tech.e50_1h : price < tech.e50_1h) : null;
  var m15ok = dir===1 ? tech.ema9 > tech.ema21 : tech.ema9 < tech.ema21;
  var vwapOk = tech.vwapBars >= 4 &&
    (dir===1 ? (tech.vwapDist >= -1.0 && tech.vwapDist <= 0.3)
             : (tech.vwapDist >= -0.3 && tech.vwapDist <= 1.0));
  var rsiOk  = tech.rsi === null ? true
    : (dir===1 ? tech.rsi <= 70 : tech.rsi >= 30);
  var atrOk  = tech.atrPct >= ATR_RANGE[pairKey][0] && tech.atrPct <= ATR_RANGE[pairKey][1];

  var carryGuard = isJpy && dir === 1 ? jpyCarryGuard(macroData) : null;
  var carryOk = carryGuard ? !carryGuard.blocked : true;

  var dp     = PIP_SIZE[pairKey];
  var places = dp === 0.01 ? 3 : 5;

  var checks = [
    { label: 'Entry window',         ok: inEntry,
      note: inEntry ? sessName + ' session active' :
            'Outside entry windows (London 11:30–14:00 DXB  |  NY 18:00–21:00 DXB)' },
    { label: '4H EMA9 > EMA21',      ok: h4ok !== null ? h4ok : true,
      note: h4ok === null ? 'No 4H data' :
            h4ok ? 'EMA9 ' + (tech.e9_4h||0).toFixed(places) + ' > EMA21 ' + (tech.e21_4h||0).toFixed(places)
                 : 'EMA9 ' + (tech.e9_4h||0).toFixed(places) + ' < EMA21 ' + (tech.e21_4h||0).toFixed(places) },
    { label: '1H EMA9 > EMA21',      ok: h1ok !== null ? h1ok : true,
      note: h1ok === null ? 'No 1H data' :
            h1ok ? 'EMA9 ' + (tech.e9_1h||0).toFixed(places) + ' aligned'
                 : 'EMA9/21 not aligned' },
    { label: '1H EMA50 trend',        ok: e50ok !== null ? e50ok : true,
      note: e50ok === null ? 'No 1H EMA50 data' :
            e50ok ? 'Price ' + price.toFixed(places) + (dir===1?' > ':' < ') + 'EMA50 ' + (tech.e50_1h||0).toFixed(places)
                  : 'Price on wrong side of 1H EMA50 ' + (tech.e50_1h||0).toFixed(places) },
    { label: '15m EMA9 > EMA21',     ok: m15ok,
      note: m15ok ? 'EMA9 ' + tech.ema9.toFixed(places) + ' aligned'
                  : 'EMA9 ' + tech.ema9.toFixed(places) + ' / EMA21 ' + tech.ema21.toFixed(places) + ' not aligned' },
    { label: 'VWAP zone (≥4 bars)',  ok: vwapOk,
      note: vwapOk
        ? 'dist=' + tech.vwapDist.toFixed(2) + 'σ ✓  VWAP=' + tech.vwap.toFixed(places)
        : tech.vwapBars < 4
          ? 'Only ' + tech.vwapBars + ' VWAP bars (need ≥4)'
          : 'dist=' + tech.vwapDist.toFixed(2) + 'σ outside zone  VWAP=' + tech.vwap.toFixed(places) },
    { label: '1H RSI(14)',           ok: rsiOk,
      note: tech.rsi === null ? '—' :
            rsiOk ? 'RSI ' + Math.round(tech.rsi) + (dir===1 ? ' (not overbought ✓)' : ' (not oversold ✓)')
                  : 'RSI ' + Math.round(tech.rsi) + (dir===1 ? ' ≥70 overbought ✗' : ' ≤30 oversold ✗') },
    { label: 'ATR in valid range',   ok: atrOk,
      note: 'ATR=' + Math.round(tech.atrPips) + 'p (' + (tech.atrPct*100).toFixed(3) + '%)' +
            (atrOk ? ' ✓' : ' outside range') }
  ];

  if (carryGuard) {
    checks.push({ label: 'JPY carry guard', ok: carryOk, note: carryGuard.note });
  }

  // V13 TECHNICAL FILTERS
  // 1. MACD(12,26,9) histogram > 0
  var macdOk = tech.macdHist !== null
    ? (dir === 1 ? tech.macdHist > 0 : tech.macdHist < 0) : true;
  checks.push({ label: 'MACD(12,26,9) histogram',
    ok: macdOk,
    note: tech.macdHist !== null
      ? 'hist=' + tech.macdHist.toFixed(6) + (macdOk ? ' ✓ momentum aligned' : ' ✗ momentum against')
      : 'insufficient data' });

  // 2. RSI 15m: <55 longs (pullback), >45 shorts
  var rsi15Thresh = dir === 1 ? 55 : 45;
  var rsi15Ok = tech.rsi15m !== null
    ? (dir === 1 ? tech.rsi15m < rsi15Thresh : tech.rsi15m > rsi15Thresh) : true;
  checks.push({ label: 'RSI(14) 15m ' + (dir===1?'< 55 (buy pullback)':'> 45 (short pullback)'),
    ok: rsi15Ok,
    note: tech.rsi15m !== null
      ? 'RSI15m=' + tech.rsi15m.toFixed(1) + (rsi15Ok ? ' ✓' : ' ✗ outside pullback zone')
      : 'insufficient data' });

  // 3. Volume ≥ 120% of 20-bar average
  var volOk = tech.volRatio !== null ? tech.volRatio >= 1.20 : true;
  checks.push({ label: 'Volume ≥ 120% of avg',
    ok: volOk,
    note: tech.volRatio !== null
      ? (tech.volRatio * 100).toFixed(0) + '% of avg (last=' + Math.round(tech.lastVol) + ' avg=' + Math.round(tech.avgVol) + ')' + (volOk ? ' ✓' : ' ✗ low volume')
      : 'no volume data — check TwelveData subscription' });

  // 4. OBV above EMA10
  var obvOk = tech.obvAboveEma !== null
    ? (dir === 1 ? tech.obvAboveEma === true : tech.obvAboveEma === false) : true;
  checks.push({ label: 'OBV vs OBV EMA10',
    ok: obvOk,
    note: tech.obvAboveEma !== null
      ? (tech.obvAboveEma ? 'OBV above EMA10' : 'OBV below EMA10') + (obvOk ? ' ✓ flow aligned' : ' ✗ flow diverging')
      : 'insufficient data' });

  var allOk = inEntry && (h4ok !== false) && (h1ok !== false) && (e50ok !== false)
              && m15ok && vwapOk && rsiOk && atrOk && carryOk
              && macdOk && rsi15Ok && volOk && obvOk;

  return { active: allOk, dir: dir, sessName: sessName, checks: checks };
}

// ── CALC TRADE (entry, SL, TP, lots) ─────────────────────────────────────────
function calcTrade(pairKey, setup, tech, currentEq, regime) {
  var dir    = setup.dir;
  var price  = tech.price;
  var atr    = tech.atr;
  var dp     = PIP_SIZE[pairKey];
  var places = dp === 0.01 ? 3 : 5;

  var slPts = SL_MULT  * atr;
  var tp1Pts = TP1_MULT * atr;
  var tp2Pts = TP2_MULT * atr;

  var sl, tp1, tp2;
  if (dir === 1) { sl = price - slPts; tp1 = price + tp1Pts; tp2 = price + tp2Pts; }
  else           { sl = price + slPts; tp1 = price - tp1Pts; tp2 = price - tp2Pts; }

  var slPips  = Math.round(slPts  / dp);
  var tp1Pips = Math.round(tp1Pts / dp);
  var tp2Pips = Math.round(tp2Pts / dp);

  var sizeAdj = (regime && regime.sizeAdj) ? regime.sizeAdj : 1.0;
  var risk  = currentEq * RISK_TIER[pairKey] * sizeAdj;
  var pv    = PIP_VALUE[pairKey];
  var lots  = Math.max(0.01, Math.round(risk / (slPips * pv) * 100) / 100);
  var lots1 = Math.max(0.01, Math.round(lots * TP1_FRAC * 100) / 100);
  var lots2 = Math.max(0.01, Math.round(lots * TP2_FRAC * 100) / 100);

  var rr1 = (tp1Pips / slPips).toFixed(1) + ':1';
  var rr2 = (tp2Pips / slPips).toFixed(1) + ':1';

  return {
    dir: dir===1 ? 'LONG' : 'SHORT',
    entry: price, sl: sl, tp1: tp1, tp2: tp2,
    slPips: slPips, tp1Pips: tp1Pips, tp2Pips: tp2Pips,
    rr1: rr1, rr2: rr2, lots: lots, lots1: lots1, lots2: lots2,
    risk: risk, atr: atr, atrPips: Math.round(atr / dp),
    places: places, sizeAdj: sizeAdj
  };
}

// ── FTMO TRACKER ─────────────────────────────────────────────────────────────
function calcFtmo(eq, dayStart) {
  var dayPnl     = eq - dayStart;
  var dailyLeft  = FTMO_DAILY + dayPnl;
  var toFloor    = eq - FTMO_FLOOR;
  var toTarget   = FTMO_TARGET - eq;
  var pctDone    = Math.max(0, (eq - ACCOUNT) / (FTMO_TARGET - ACCOUNT) * 100);
  return { eq: eq, dayStart: dayStart, dayPnl: dayPnl,
           dailyLeft: dailyLeft, toFloor: toFloor, toTarget: toTarget,
           pctDone: pctDone, floorBreached: eq <= FTMO_FLOOR,
           dailyBreached: dailyLeft <= 0 };
}

// ── SHEET WRITER ─────────────────────────────────────────────────────────────
function writeSheet(sheet, now, nowUtcH, results, orbSignal, ftmo, curEq, dayStartEq, regime) {
  sheet.clear();
  // Column widths: A=200, B=90, C=90, D=90, E=90, F=90, G=130
  [200, 90, 90, 90, 90, 90, 130].forEach(function(w, i) { sheet.setColumnWidth(i+1, w); });

  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm \'DXB (UTC+4)\'');
  var utcStr   = Utilities.formatDate(now, 'UTC', 'HH:mm \'UTC\'');
  var r        = 1;

  // ═══ TITLE ════════════════════════════════════════════════════════════════
  mr(sheet, r, 6, '  FTMO PORTFOLIO SIGNAL DASHBOARD',
     {bg:'#0a1628', fg:'#00bcd4', sz:16, bold:true, h:44}); r++;
  mr(sheet, r, 6, dubaiStr + '  ·  ' + utcStr + '  ·  Auto-refreshes every 5 min',
     {bg:'#060e1a', fg:'#334455', sz:10, h:24}); r++;

  // ═══ REGIME BAR ══════════════════════════════════════════════════════════
  r++;
  if (regime) {
    var regBg = regime.bg || '#0a1628';
    var regFg = regime.color || '#00bcd4';
    mr(sheet, r, 6,
       '▶  REGIME: ' + regime.label + '   —   ' + regime.description,
       {bg: regBg, fg: regFg, sz: 13, bold: true, h: 36}); r++;
    regime.components.forEach(function(rc) {
      var rcBg = rc.ok ? '#0a1a0a' : '#1a0a0a';
      var rcFg = rc.ok ? '#00c853' : '#ff5252';
      mr(sheet, r, 6,
         (rc.ok ? '✓' : '✗') + '  ' + rc.name + '  —  ' + rc.note,
         {bg: rcBg, fg: rcFg, sz: 9, h: 22}); r++;
    });
  }

  // ═══ SESSION CLOCK ════════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, 'SESSION CLOCK (Dubai / UTC+4)',
     {bg:'#111e2e', fg:'#445577', sz:9, bold:true, h:22}); r++;

  var sessions = [
    { name:'GBP/JPY ORB RANGE', dxbOpen:'10:30', dxbClose:'11:30', utcOpen: 6.5, utcClose:7.5,  pairs:'GBP/JPY  (range building)' },
    { name:'GBP/JPY ORB ENTRY', dxbOpen:'11:30', dxbClose:'13:00', utcOpen: 7.5, utcClose:9.0,  pairs:'GBP/JPY  (breakout entry)' },
    { name:'LONDON TREND',      dxbOpen:'11:30', dxbClose:'14:00', utcOpen: 7.5, utcClose:10.0, pairs:'CAD/CHF  AUD/JPY  CAD/JPY' },
    { name:'NY TREND',          dxbOpen:'18:00', dxbClose:'21:00', utcOpen:14.0, utcClose:17.0, pairs:'CAD/CHF  AUD/JPY  CAD/JPY' }
  ];
  sessions.forEach(function(s) {
    var active = nowUtcH >= s.utcOpen && nowUtcH <= s.utcClose;
    var soon   = !active && nowUtcH >= s.utcOpen - 0.25 && nowUtcH < s.utcOpen;
    var bg     = active ? '#0a2a14' : soon ? '#2a1f00' : '#0a0e14';
    var fg     = active ? '#00e676' : soon ? '#ffc107' : '#445566';
    var status = active ? '● LIVE' : soon ? '◌ SOON' : '○ CLOSED';
    cl(sheet, r, 1, status + '  ' + s.name, {bg:bg, fg:fg, sz:11, bold:true, h:28});
    cl(sheet, r, 2, s.dxbOpen + ' – ' + s.dxbClose, {bg:bg, fg:active?'#e0e0e0':'#607d8b', sz:10});
    cl(sheet, r, 3, '(UTC '+(s.utcOpen<10?'0':'')+Math.floor(s.utcOpen)+':00)', {bg:bg, fg:'#334455', sz:9});
    sheet.getRange(r, 4, 1, 3).merge().setValue(s.pairs)
      .setBackground(bg).setFontColor(active?'#00bcd4':'#445566').setFontSize(10)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ═══ SIGNAL SUMMARY TABLE ═════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, 'ACTIVE SIGNALS', {bg:'#111e2e', fg:'#445577', sz:9, bold:true, h:22}); r++;

  // Headers
  var hdrs = ['PAIR','SIGNAL','SCORE','ENTRY','SL','TP1','TP2'];
  hdrs.forEach(function(h, i) {
    cl(sheet, r, i+1, h, {bg:'#0c1828', fg:'#334455', sz:9, bold:true, h:22});
  });
  r++;

  var pairLabels = { 'CADCHF':'CAD/CHF', 'AUDJPY':'AUD/JPY', 'CADJPY':'CAD/JPY' };
  ['CADCHF','AUDJPY','CADJPY'].forEach(function(p) {
    var res   = results[p];
    var macro = res.macro;
    var trade = res.trade;
    var score = macro.score || 0;
    var longOnly = LONG_ONLY[p];

    var sigText, sigBg, sigFg;
    if (trade) {
      sigText = trade.dir === 'LONG' ? '▲  LONG' : '▼  SHORT';
      sigBg   = trade.dir === 'LONG' ? '#0a2a14' : '#2a0a0a';
      sigFg   = trade.dir === 'LONG' ? '#00e676' : '#ff5252';
    } else {
      sigText = longOnly ? 'LONG ONLY — waiting' : '—  WAIT';
      sigBg   = '#0a0e14'; sigFg = '#445566';
    }

    var scoreFg = score >= 6 ? '#00e676' : score <= -6 ? '#ff5252' :
                  score !== 0 ? '#ffc107' : '#445566';
    var scoreTxt = macro.gated ? score.toFixed(1)+' [gated]'
                 : score !== 0 ? score.toFixed(1) : '—';

    var dp = PIP_SIZE[p];
    var pl = dp === 0.01 ? 3 : 5;

    cl(sheet, r, 1, pairLabels[p] + (longOnly ? '  🔼' : ''), {bg:'#0a0e14', fg:'#c0ccd6', sz:11, bold:true, h:36});
    cl(sheet, r, 2, sigText,                  {bg:sigBg,    fg:sigFg,   sz:11, bold:true});
    cl(sheet, r, 3, scoreTxt,                 {bg:'#0a0e14', fg:scoreFg, sz:11, bold:true});
    if (trade) {
      cl(sheet, r, 4, trade.entry.toFixed(pl),       {bg:'#0a0e14', fg:'#e0e0e0', sz:10, bold:true});
      cl(sheet, r, 5, trade.sl.toFixed(pl)+'\n−'+trade.slPips+'p', {bg:'#0a0e14', fg:'#ff5252', sz:9});
      cl(sheet, r, 6, trade.tp1.toFixed(pl)+'\n+'+trade.tp1Pips+'p', {bg:'#0a0e14', fg:'#00e676', sz:9});
      cl(sheet, r, 7, trade.tp2.toFixed(pl)+'\n+'+trade.tp2Pips+'p', {bg:'#0a0e14', fg:'#00bcd4', sz:9});
    } else {
      [4,5,6,7].forEach(function(c){ cl(sheet, r, c, '—', {bg:'#0a0e14', fg:'#334455', sz:10}); });
    }
    r++;
  });

  // GBP/JPY ORB row in summary table
  var orbTrade = orbSignal.trade;
  var orbSigText = orbTrade ? '▲  LONG  [ORB]' : '○  ' + (
    orbSignal.nowUtcH < ORB_RANGE_START_H ? 'PRE-RANGE' :
    orbSignal.nowUtcH < ORB_RANGE_END_H   ? 'BUILDING RANGE' :
    orbSignal.inEntry                      ? 'IN ENTRY WINDOW' : 'CLOSED');
  var orbSigBg = orbTrade ? '#0a2a14' : '#0a0e14';
  var orbSigFg = orbTrade ? '#00e676' : '#334466';
  cl(sheet, r, 1, 'GBP/JPY  🔼  [ORB]', {bg:'#0a0e1e', fg:'#9575cd', sz:11, bold:true, h:36});
  cl(sheet, r, 2, orbSigText, {bg:orbSigBg, fg:orbSigFg, sz:11, bold:true});
  cl(sheet, r, 3, 'ORB', {bg:'#0a0e1e', fg:'#7e57c2', sz:10, bold:true});
  if (orbTrade) {
    cl(sheet, r, 4, orbTrade.entry.toFixed(orbTrade.places), {bg:'#0a0e14', fg:'#e0e0e0', sz:10, bold:true});
    cl(sheet, r, 5, orbTrade.sl.toFixed(orbTrade.places)+'\n−'+orbTrade.slPips+'p', {bg:'#0a0e14', fg:'#ff5252', sz:9});
    cl(sheet, r, 6, orbTrade.tp1.toFixed(orbTrade.places)+'\n+'+orbTrade.tp1Pips+'p', {bg:'#0a0e14', fg:'#00e676', sz:9});
    cl(sheet, r, 7, orbTrade.tp2.toFixed(orbTrade.places)+'\n+'+orbTrade.tp2Pips+'p', {bg:'#0a0e14', fg:'#00bcd4', sz:9});
  } else {
    [4,5,6,7].forEach(function(c){ cl(sheet, r, c, '—', {bg:'#0a0e1e', fg:'#2a1a4a', sz:10}); });
  }
  r++;

  // ═══ PER-PAIR DETAIL ══════════════════════════════════════════════════════
  ['CADCHF','AUDJPY','CADJPY'].forEach(function(p) {
    var res    = results[p];
    var macro  = res.macro;
    var tech   = res.tech;
    var setup  = res.setup;
    var trade  = res.trade;
    var longOnly = LONG_ONLY[p];
    var dp     = PIP_SIZE[p];
    var pl     = dp === 0.01 ? 3 : 5;
    var label  = pairLabels[p];

    r++;
    // Section title
    var scolor = trade ? '#00ff88' : '#607d8b';
    var stitle = trade
      ? '●  ' + label + '  →  ' + trade.dir + '  (' + trade.risk.toFixed(0) + ' USD risk · ' + RISK_TIER[p]*100 + '% · ' + trade.lots + ' lots)'
      : '○  ' + label + '  →  STANDBY' + (longOnly ? '  [LONG ONLY]' : '');
    mr(sheet, r, 6, stitle, {bg: trade ? '#0a2a14' : '#0a0e14', fg:scolor, sz:12, bold:true, h:36}); r++;

    // Price + Score bar
    var price  = macro.pairPrice;
    var scoreV = macro.score || 0;
    cl(sheet, r, 1, 'PRICE',        {bg:'#0c1828', fg:'#445566', sz:9, bold:true, h:22});
    cl(sheet, r, 2, 'SCORE',        {bg:'#0c1828', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 3, 'EMA20',        {bg:'#0c1828', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 4, 'EMA50',        {bg:'#0c1828', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 5, 'EMA200',       {bg:'#0c1828', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 6, 'ATR (pips)',   {bg:'#0c1828', fg:'#445566', sz:9, bold:true});
    r++;

    var sfg2 = scoreV >= 6 ? '#00e676' : scoreV <= -6 ? '#ff5252' : '#607d8b';
    cl(sheet, r, 1, price ? price.toFixed(pl) : '—',     {bg:'#080e1a', fg:'#e0e0e0', sz:13, bold:true, h:36});
    cl(sheet, r, 2, macro.gated ? scoreV.toFixed(1)+'\n[gated]' : (scoreV !== 0 ? scoreV.toFixed(1) : '—'),
       {bg:'#080e1a', fg:sfg2, sz:13, bold:true});
    cl(sheet, r, 3, macro.ema20 ? macro.ema20.toFixed(pl) : '—', {bg:'#080e1a', fg:'#7c99b5', sz:11});
    cl(sheet, r, 4, macro.ema50 ? macro.ema50.toFixed(pl) : '—', {bg:'#080e1a', fg:'#5c7a95', sz:11});
    cl(sheet, r, 5, macro.ema200 ? macro.ema200.toFixed(pl) : '—', {bg:'#080e1a', fg:'#3c5a75', sz:11});
    cl(sheet, r, 6, tech ? Math.round(tech.atrPips) + 'p  (' + (tech.atrPct*100).toFixed(3) + '%)' : '—',
       {bg:'#080e1a', fg:'#607d8b', sz:10});
    r++;

    // V6 EMA20 Gate
    var gateOk  = macro.gate === 1;
    var gateMsg = gateOk
      ? '✅ Daily EMA20 gate: pair ABOVE EMA20 (' + (macro.ema20||0).toFixed(pl) + ') — ' + (longOnly?'LONGS':'LONGS + SHORTS') + ' ALLOWED'
      : '🔴 Daily EMA20 gate: pair BELOW EMA20 (' + (macro.ema20||0).toFixed(pl) + ') — ' + (longOnly?'LONGS BLOCKED (long-only pair)':'ONLY SHORTS ALLOWED');
    mr(sheet, r, 6, gateMsg, {bg: gateOk?'#0a2a14':'#1a0a0a', fg:gateOk?'#00c853':'#ff5252', sz:10, bold:true, h:28}); r++;

    if (longOnly) {
      mr(sheet, r, 6, '🔼  CARRY TRADE — LONG ONLY  |  Never short (BoJ carry bomb protection)',
         {bg:'#100a20', fg:'#9575cd', sz:10, h:24}); r++;
    }

    // Macro components
    mr(sheet, r, 6, 'MACRO COMPONENTS  (need 4 of 5 ≥ score 6)',
       {bg:'#111e2e', fg:'#334466', sz:9, bold:true, h:20}); r++;
    if (macro.components) {
      macro.components.forEach(function(c) {
        var bg = c.val > 0 ? '#0a1a0a' : c.val < 0 ? '#1a0a0a' : '#0c0c0c';
        var fg = c.val > 0 ? '#00c853' : c.val < 0 ? '#ff5252' : '#607d8b';
        var icon = c.val > 0 ? '▲' : c.val < 0 ? '▼' : '—';
        cl(sheet, r, 1, icon + '  ' + c.name, {bg:bg, fg:fg, sz:10, bold:true, h:28});
        sheet.getRange(r, 2, 1, 4).merge().setValue(c.note)
          .setBackground(bg).setFontColor('#7a8899').setFontSize(9)
          .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
        cl(sheet, r, 6, c.val > 0 ? '+1' : c.val < 0 ? '−1' : '0',
           {bg:bg, fg:fg, sz:12, bold:true});
        r++;
      });
    }

    // Setup checklist
    r++;
    mr(sheet, r, 6, 'SETUP CHECKLIST', {bg:'#111e2e', fg:'#334466', sz:9, bold:true, h:20}); r++;
    if (setup.checks && setup.checks.length) {
      setup.checks.forEach(function(c) {
        var bg = c.ok ? '#0a1a0a' : '#1a0a0a';
        var fg = c.ok ? '#00c853' : '#ff5252';
        mr(sheet, r, 6,
           (c.ok ? '✓  ' : '✗  ') + c.label + '   —   ' + c.note,
           {bg:bg, fg:fg, sz:10, h:26});
        r++;
      });
    } else {
      mr(sheet, r, 6, setup.reason || 'No signal — check macro score',
         {bg:'#0a0e14', fg:'#445566', sz:10, h:26}); r++;
    }

    // Trade setup (only if active)
    if (trade) {
      r++;
      mr(sheet, r, 6,
         '⚡ TRADE SETUP — ' + trade.dir + ' ' + label + '  ·  ' + setup.sessName + ' SESSION',
         {bg:'#0a2a14', fg:'#00ff88', sz:12, bold:true, h:32}); r++;

      // Row: ENTRY / SL / TP1 / TP2 labels
      cl(sheet, r, 1, 'ENTRY',          {bg:'#111111', fg:'#445566', sz:9, bold:true, h:22});
      cl(sheet, r, 2, 'STOP LOSS',      {bg:'#111111', fg:'#445566', sz:9, bold:true});
      cl(sheet, r, 3, 'TP1  (close 60%)',{bg:'#111111',fg:'#445566', sz:9, bold:true});
      cl(sheet, r, 4, 'TP2  (close 40%)',{bg:'#111111',fg:'#445566', sz:9, bold:true});
      cl(sheet, r, 5, 'LOTS (total)',   {bg:'#111111', fg:'#445566', sz:9, bold:true});
      cl(sheet, r, 6, 'RISK',           {bg:'#111111', fg:'#445566', sz:9, bold:true});
      r++;

      // Row: values
      cl(sheet, r, 1, trade.entry.toFixed(pl),  {bg:'#0a2a14', fg:'#e0e0e0', sz:14, bold:true, h:44});
      cl(sheet, r, 2, trade.sl.toFixed(pl),     {bg:'#2a0a0a', fg:'#ff5252', sz:14, bold:true});
      cl(sheet, r, 3, trade.tp1.toFixed(pl),    {bg:'#0a2a0a', fg:'#00e676', sz:14, bold:true});
      cl(sheet, r, 4, trade.tp2.toFixed(pl),    {bg:'#0a1a2a', fg:'#00bcd4', sz:14, bold:true});
      cl(sheet, r, 5, trade.lots.toFixed(2),    {bg:'#1a1a0a', fg:'#ffd700', sz:14, bold:true});
      cl(sheet, r, 6, '$' + Math.round(trade.risk), {bg:'#1a1a0a', fg:'#ffd700', sz:11, bold:true});
      r++;

      // SL / TP distance in pips + R:R
      cl(sheet, r, 1, 'ATR: ' + trade.atrPips + 'p',         {bg:'#0a0a0a', fg:'#445566', sz:9, h:26});
      cl(sheet, r, 2, '−' + trade.slPips + ' pips',           {bg:'#0a0a0a', fg:'#cc4444', sz:10, bold:true});
      cl(sheet, r, 3, '+' + trade.tp1Pips + 'p  R:R ' + trade.rr1, {bg:'#0a0a0a', fg:'#00aa44', sz:10, bold:true});
      cl(sheet, r, 4, '+' + trade.tp2Pips + 'p  R:R ' + trade.rr2, {bg:'#0a0a0a', fg:'#0088bb', sz:10, bold:true});
      cl(sheet, r, 5, trade.lots1.toFixed(2) + ' lots',        {bg:'#0a0a0a', fg:'#aaa000', sz:9});
      cl(sheet, r, 6, trade.lots2.toFixed(2) + ' lots',        {bg:'#0a0a0a', fg:'#aaa000', sz:9});
      r++;

      // Instructions row
      mr(sheet, r, 6,
         'MT4/MT5: Place ' + trade.dir + ' @ market or limit ' + trade.entry.toFixed(pl) +
         '  |  SL ' + trade.sl.toFixed(pl) + '  |  TP1 ' + trade.tp1.toFixed(pl) + ' (close ' + trade.lots1.toFixed(2) + ' lots)' +
         '  |  When TP1 hit → move SL to ' + trade.entry.toFixed(pl) + ' (breakeven)  |  TP2 ' + trade.tp2.toFixed(pl) + ' (close ' + trade.lots2.toFixed(2) + ' lots)',
         {bg:'#080e1a', fg:'#5577aa', sz:9, h:36}); r++;
      mr(sheet, r, 6,
         'Pip value ≈ $' + PIP_VALUE[p].toFixed(2) + '/lot  |  ' + RISK_TIER[p]*100 + '% risk tier' +
         (trade.sizeAdj < 1 ? '  |  ' + (trade.sizeAdj*100).toFixed(0) + '% size (regime ' + (regime?regime.label:'') + ')' : '') +
         '  |  Verify exact lot size & pip value in MT4 Market Watch before entering',
         {bg:'#050a10', fg:'#334455', sz:8, h:24}); r++;
    }
  });

  // ═══ GBP/JPY ORB DETAIL ═══════════════════════════════════════════════════
  r++;
  var orbActive = !!orbSignal.trade;
  var orbTitleColor = orbActive ? '#00ff88' : '#7e57c2';
  var orbTitleBg    = orbActive ? '#0a2a14' : '#0a0e1e';
  mr(sheet, r, 6,
     (orbActive ? '●  GBP/JPY  →  ORB LONG  ·  1% risk  ·  London open breakout' :
                  '○  GBP/JPY  →  ORB  [LONG ONLY — carry trade]'),
     {bg:orbTitleBg, fg:orbTitleColor, sz:12, bold:true, h:36}); r++;

  // Status banner
  var statusBg = orbActive ? '#0a2a14' : (orbSignal.asianBlock ? '#2a0a0a' : '#0a0e14');
  var statusFg = orbActive ? '#00e676' : (orbSignal.asianBlock ? '#ff5252' : '#607d8b');
  mr(sheet, r, 6, orbSignal.status, {bg:statusBg, fg:statusFg, sz:10, bold:true, h:28}); r++;

  // Asian session row
  mr(sheet, r, 6, 'ASIAN SESSION BIAS (04:00–10:30 DXB)', {bg:'#0a0e1e', fg:'#334466', sz:9, bold:true, h:20}); r++;
  if (orbSignal.asianHigh) {
    var asianBg = orbSignal.asianBias === 1 ? '#0a1a0a' : '#1a0a0a';
    var asiaBiasText = orbSignal.asianBias === 1
      ? '▲ BULLISH — close ' + (orbSignal.asianClose||0).toFixed(3) + ' above mid ' + ((orbSignal.asianHigh+orbSignal.asianLow)/2).toFixed(3) + '  →  LONG ORB enabled'
      : '▼ BEARISH — close ' + (orbSignal.asianClose||0).toFixed(3) + ' below mid ' + ((orbSignal.asianHigh+orbSignal.asianLow)/2).toFixed(3) + '  →  LONG ORB BLOCKED';
    cl(sheet, r, 1, 'Asian High', {bg:'#0a0e1e', fg:'#445566', sz:9, bold:true, h:26});
    cl(sheet, r, 2, (orbSignal.asianHigh||0).toFixed(3), {bg:'#0a0e1e', fg:'#9575cd', sz:11});
    cl(sheet, r, 3, 'Asian Low', {bg:'#0a0e1e', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 4, (orbSignal.asianLow||0).toFixed(3), {bg:'#0a0e1e', fg:'#9575cd', sz:11});
    sheet.getRange(r, 5, 1, 2).merge().setValue(asiaBiasText)
      .setBackground(asiaBg).setFontColor(orbSignal.asianBias===1?'#00c853':'#ff5252')
      .setFontSize(9).setFontWeight('bold').setVerticalAlignment('middle')
      .setHorizontalAlignment('left').setWrap(true);
    r++;
  } else {
    mr(sheet, r, 6, 'No Asian session data (market may be closed or pre-04:00 DXB)', {bg:'#0a0e1e', fg:'#445566', sz:9, h:24}); r++;
  }

  // ORB range row
  mr(sheet, r, 6, 'PRE-LONDON RANGE (10:30–11:30 DXB = 06:30–07:30 UTC)', {bg:'#0a0e1e', fg:'#334466', sz:9, bold:true, h:20}); r++;
  if (orbSignal.rangeHigh) {
    var rngPips = Math.round((orbSignal.orbRange||0) / PIP_SIZE['GBPJPY']);
    var triggerLong  = (orbSignal.rangeHigh + ORB_BREAKOUT_CONF * orbSignal.orbRange).toFixed(3);
    cl(sheet, r, 1, 'Range High', {bg:'#0a0e1e', fg:'#445566', sz:9, bold:true, h:30});
    cl(sheet, r, 2, (orbSignal.rangeHigh||0).toFixed(3), {bg:'#0a0e1e', fg:'#00bcd4', sz:12, bold:true});
    cl(sheet, r, 3, 'Range Low', {bg:'#0a0e1e', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 4, (orbSignal.rangeLow||0).toFixed(3), {bg:'#0a0e1e', fg:'#00bcd4', sz:12, bold:true});
    cl(sheet, r, 5, 'Range', {bg:'#0a0e1e', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 6, rngPips + ' pips  (' + orbSignal.rangeBarsCount + ' bars)', {bg:'#0a0e1e', fg:'#ffc107', sz:11, bold:true});
    r++;
    mr(sheet, r, 6, '⚡ LONG trigger: price > ' + triggerLong + '  (range_high + 10% of range)',
       {bg:'#0a1a0a', fg:'#00c853', sz:10, h:26}); r++;
  } else if (orbSignal.nowUtcH >= ORB_RANGE_START_H) {
    mr(sheet, r, 6, 'Building range (' + orbSignal.rangeBarsCount + ' bars so far — need ' + ORB_MIN_BARS + ')',
       {bg:'#0a0e1e', fg:'#607d8b', sz:10, h:26}); r++;
  } else {
    mr(sheet, r, 6, 'Range builds 10:30–11:30 DXB. Check back at 10:30.',
       {bg:'#0a0e1e', fg:'#445566', sz:9, h:24}); r++;
  }

  // Current price in entry window
  if (orbSignal.inEntry && orbSignal.price) {
    mr(sheet, r, 6, 'Entry window OPEN (11:30–13:00 DXB)  |  Current GBP/JPY: ' + orbSignal.price.toFixed(3),
       {bg:'#0a1a0a', fg:'#00e676', sz:10, bold:true, h:26}); r++;
  }

  // Trade setup if active
  if (orbTrade) {
    r++;
    mr(sheet, r, 6, '⚡ TRADE SETUP — LONG GBP/JPY  ·  LONDON ORB  ·  SL=range low  TP1=1.5×range  TP2=2.5×range',
       {bg:'#0a2a14', fg:'#00ff88', sz:12, bold:true, h:32}); r++;

    cl(sheet, r, 1, 'ENTRY',              {bg:'#111111', fg:'#445566', sz:9, bold:true, h:22});
    cl(sheet, r, 2, 'STOP LOSS',          {bg:'#111111', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 3, 'TP1  (close 60%)',   {bg:'#111111', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 4, 'TP2  (close 40%)',   {bg:'#111111', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 5, 'LOTS (total)',        {bg:'#111111', fg:'#445566', sz:9, bold:true});
    cl(sheet, r, 6, 'RISK',               {bg:'#111111', fg:'#445566', sz:9, bold:true}); r++;

    var op = orbTrade.places;
    cl(sheet, r, 1, orbTrade.entry.toFixed(op), {bg:'#0a2a14', fg:'#e0e0e0', sz:14, bold:true, h:44});
    cl(sheet, r, 2, orbTrade.sl.toFixed(op),    {bg:'#2a0a0a', fg:'#ff5252', sz:14, bold:true});
    cl(sheet, r, 3, orbTrade.tp1.toFixed(op),   {bg:'#0a2a0a', fg:'#00e676', sz:14, bold:true});
    cl(sheet, r, 4, orbTrade.tp2.toFixed(op),   {bg:'#0a1a2a', fg:'#00bcd4', sz:14, bold:true});
    cl(sheet, r, 5, orbTrade.lots.toFixed(2),   {bg:'#1a1a0a', fg:'#ffd700', sz:14, bold:true});
    cl(sheet, r, 6, '$' + Math.round(orbTrade.risk), {bg:'#1a1a0a', fg:'#ffd700', sz:11, bold:true}); r++;

    cl(sheet, r, 1, 'Range: '+orbTrade.rangePips+'p', {bg:'#0a0a0a', fg:'#445566', sz:9, h:26});
    cl(sheet, r, 2, '−' + orbTrade.slPips + ' pips',    {bg:'#0a0a0a', fg:'#cc4444', sz:10, bold:true});
    cl(sheet, r, 3, '+'+orbTrade.tp1Pips+'p  R:R '+(orbTrade.tp1Pips/orbTrade.slPips).toFixed(1)+':1', {bg:'#0a0a0a', fg:'#00aa44', sz:10, bold:true});
    cl(sheet, r, 4, '+'+orbTrade.tp2Pips+'p  R:R '+(orbTrade.tp2Pips/orbTrade.slPips).toFixed(1)+':1', {bg:'#0a0a0a', fg:'#0088bb', sz:10, bold:true});
    cl(sheet, r, 5, orbTrade.lots1.toFixed(2)+' lots', {bg:'#0a0a0a', fg:'#aaa000', sz:9});
    cl(sheet, r, 6, orbTrade.lots2.toFixed(2)+' lots', {bg:'#0a0a0a', fg:'#aaa000', sz:9}); r++;

    mr(sheet, r, 6,
       'MT4/MT5: BUY @ market  |  SL ' + orbTrade.sl.toFixed(op) + '  |  TP1 ' + orbTrade.tp1.toFixed(op) +
       ' (close ' + orbTrade.lots1.toFixed(2) + ' lots → move SL to entry)  |  TP2 ' + orbTrade.tp2.toFixed(op) +
       ' (close ' + orbTrade.lots2.toFixed(2) + ' lots)  |  Time-exit if no TP by 13:00 DXB',
       {bg:'#080e1a', fg:'#5577aa', sz:9, h:36}); r++;
    mr(sheet, r, 6,
       'ORB edge: 57% WR  avg +0.137R  (V13 backtest Sep 2024–May 2026, 97 trades, p=0.0001)  |  1% risk  |  London open only  |  4 tech filters active',
       {bg:'#050a10', fg:'#334455', sz:8, h:24}); r++;
  } else {
    // Show carry guard status if relevant
    if (orbSignal.carryNote) {
      mr(sheet, r, 6, orbSignal.carryNote, {bg:'#0a0e1e', fg:'#607d8b', sz:9, h:24}); r++;
    }
    // Show V13 tech filter results if in entry window
    if (orbSignal.orbTechChecks && orbSignal.orbTechChecks.length) {
      mr(sheet, r, 6, 'V13 TECHNICAL FILTERS (ORB)', {bg:'#0a0e1e', fg:'#334466', sz:9, bold:true, h:20}); r++;
      orbSignal.orbTechChecks.forEach(function(c) {
        var bg = c.ok ? '#0a1a0a' : '#1a0a0a';
        var fg = c.ok ? '#00c853' : '#ff5252';
        mr(sheet, r, 6, (c.ok ? '✓  ' : '✗  ') + c.label + '   —   ' + c.note,
           {bg:bg, fg:fg, sz:10, h:24}); r++;
      });
    }
  }

  // ═══ FTMO TRACKER ══════════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, 'FTMO CHALLENGE TRACKER', {bg:'#0a1628', fg:'#445577', sz:10, bold:true, h:22}); r++;

  // Labels for user-editable G2/G3
  sheet.getRange('G1').setValue('↓ EDIT G2/G3 DAILY').setFontColor('#334455').setFontSize(8).setBackground('#040810');
  sheet.getRange('G2').setBackground('#0a1a2a').setFontColor('#e0e0e0').setFontSize(12)
    .setFontWeight('bold').setHorizontalAlignment('center')
    .setNote('Current MT5 equity — update whenever you open the dashboard');
  sheet.getRange('G3').setBackground('#0a1a0a').setFontColor('#e0e0e0').setFontSize(12)
    .setFontWeight('bold').setHorizontalAlignment('center')
    .setNote('Your account balance at the START of today (reset each morning)');
  if (!sheet.getRange('G2').getValue()) sheet.getRange('G2').setValue(ACCOUNT);
  if (!sheet.getRange('G3').getValue()) sheet.getRange('G3').setValue(ACCOUNT);

  var ftmoRows = [
    ['Current equity',    '$' + Math.round(ftmo.eq).toLocaleString(),
     ftmo.eq >= FTMO_TARGET ? '#00e676' : ftmo.eq <= FTMO_FLOOR ? '#ff1744' : '#e0e0e0'],
    ['Today P&L',         (ftmo.dayPnl >= 0 ? '+' : '') + '$' + Math.round(ftmo.dayPnl).toLocaleString(),
     ftmo.dayPnl >= 0 ? '#00c853' : '#ff5252'],
    ['Daily limit used',  '$' + Math.round(Math.max(0, -ftmo.dayPnl)).toLocaleString() + ' / $6,000',
     ftmo.dailyBreached ? '#ff1744' : ftmo.dailyLeft < 2000 ? '#ff9800' : '#00c853'],
    ['Daily headroom',    '$' + Math.round(Math.max(0, ftmo.dailyLeft)).toLocaleString() + ' left',
     ftmo.dailyLeft < 1000 ? '#ff1744' : ftmo.dailyLeft < 3000 ? '#ff9800' : '#00c853'],
    ['Distance to floor', '+$' + Math.round(Math.max(0, ftmo.toFloor)).toLocaleString() + ' above $108k',
     ftmo.toFloor < 2000 ? '#ff1744' : ftmo.toFloor < 5000 ? '#ff9800' : '#00c853'],
    ['Profit needed',     '$' + Math.round(Math.max(0, ftmo.toTarget)).toLocaleString() + ' to reach $132k',
     '#607d8b'],
    ['Challenge progress', Math.max(0, Math.round(ftmo.pctDone)) + '%  of 10% target',
     ftmo.pctDone >= 100 ? '#00e676' : ftmo.pctDone >= 50 ? '#ffd700' : '#607d8b']
  ];

  ftmoRows.forEach(function(row) {
    cl(sheet, r, 1, row[0], {bg:'#080e1a', fg:'#445566', sz:10, bold:true, h:30});
    sheet.getRange(r, 2, 1, 5).merge().setValue(row[1])
      .setBackground('#080e1a').setFontColor(row[2]).setFontSize(14).setFontWeight('bold')
      .setVerticalAlignment('middle').setHorizontalAlignment('center');
    r++;
  });

  if (ftmo.floorBreached) {
    mr(sheet, r, 6, '🚨 FLOOR BREACHED — STOP ALL TRADING — Equity hit $108,000 floor',
       {bg:'#3a0000', fg:'#ff1744', sz:12, bold:true, h:36}); r++;
  } else if (ftmo.dailyBreached) {
    mr(sheet, r, 6, '⛔ DAILY LIMIT HIT — No more trades today — −$6,000 reached',
       {bg:'#2a1000', fg:'#ff9800', sz:11, bold:true, h:32}); r++;
  } else if (ftmo.toFloor < 3000) {
    mr(sheet, r, 6, '⚠️ WARNING: $' + Math.round(ftmo.toFloor) + ' from floor — reduce size or stop trading',
       {bg:'#2a1500', fg:'#ff9800', sz:10, bold:true, h:28}); r++;
  }

  // ═══ WHEN TO CHECK (DUBAI) ════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6, '⏰  WHEN TO CHECK — DUBAI (UTC+4)',
     {bg:'#111e2e', fg:'#445577', sz:9, bold:true, h:22}); r++;

  var schedule = [
    ['10:15 DXB', 'Pre-ORB',     'GBP/JPY',     '#1a0a2a', 'Check Asian session direction — is Asian bias bullish? If not, no ORB today'],
    ['10:30 DXB', '▶ RANGE',     'GBP/JPY ORB', '#0a1020', 'Pre-London range begins building (06:30 UTC) — do NOT trade yet'],
    ['11:15 DXB', 'Pre-London',  'All pairs',   '#2a2a00', 'Most important check — review TREND signals + confirm ORB range set'],
    ['11:30 DXB', '▶ LON OPEN',  'All pairs',   '#0a2a14', 'ORB breakout entry + TREND session opens (UTC 07:30)'],
    ['13:00 DXB', 'ORB closes',  'GBP/JPY',     '#0a0e14', 'Last ORB entry bar. Exit any open ORB that has not hit TP'],
    ['14:00 DXB', 'LON closes',  '—',           '#0a0e14', 'No more London TREND entries — wait for NY'],
    ['18:00 DXB', '▶ NY OPEN',   'TREND pairs', '#0a2a14', 'Secondary session — CAD/CHF AUD/JPY CAD/JPY (UTC 14:00–17:00)'],
    ['21:00 DXB', 'NY closes',   '—',           '#0a0e14', 'Close any open trades, review day P&L']
  ];
  schedule.forEach(function(row) {
    cl(sheet, r, 1, row[0], {bg:row[3], fg: row[3]==='#0a2a14'?'#00e676':'#c0ccd6', sz:11, bold:true, h:30});
    cl(sheet, r, 2, row[1], {bg:row[3], fg:'#c0ccd6', sz:10});
    cl(sheet, r, 3, row[2], {bg:row[3], fg:'#00bcd4', sz:10});
    sheet.getRange(r, 4, 1, 3).merge().setValue(row[4])
      .setBackground(row[3]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ═══ FOOTER ═══════════════════════════════════════════════════════════════
  r++;
  mr(sheet, r, 6,
     'V13 Portfolio · Sep 2024–May 2026 · +109.8% ROI · Sharpe 2.94 · Max DD 9.6% · p=0.0001 ✓ · Floor ✓ · 1.61 trades/wk · 18/21 positive months',
     {bg:'#040810', fg:'#1a2a3a', sz:9, h:24}); r++;
  mr(sheet, r, 6,
     'TREND: CAD/CHF 2% WR63% avgR+0.90  |  AUD/JPY 1.5% WR56% avgR+0.63 (long only)  |  CAD/JPY 1% WR55% avgR+0.70 (long only)',
     {bg:'#040810', fg:'#1a2a3a', sz:8, h:22}); r++;
  mr(sheet, r, 6,
     'ORB: GBP/JPY 1% WR57% avgR+0.14 (long only, London open)  |  Trend SL=0.75×ATR  TP1=1.5×ATR  TP2=2.5×ATR  |  ORB SL=range low  TP1=1.5×range  TP2=2.5×range',
     {bg:'#040810', fg:'#1a2a3a', sz:8, h:22}); r++;
  mr(sheet, r, 6,
     'V13 filters: MACD(12,26,9) hist>0 · RSI15m <55 longs/>45 shorts · Volume ≥120% avg · OBV > OBV_EMA10  |  Regime: BULL 1.0× · OIL_WEAK 0.75× · VIX_SPIKE 0.50×',
     {bg:'#040810', fg:'#1a2a3a', sz:8, h:22});

  SpreadsheetApp.flush();
}

// ── MATH HELPERS ─────────────────────────────────────────────────────────────
function calcEma(arr, n) {
  var k = 2 / (n + 1), out = [arr[0]];
  for (var i = 1; i < arr.length; i++) out.push(arr[i]*k + out[i-1]*(1-k));
  return out;
}

function calcRsi14(closes) {
  var gains = [], losses = [];
  for (var i = 1; i < closes.length; i++) {
    var d = closes[i] - closes[i-1];
    gains.push(d > 0 ? d : 0);
    losses.push(d < 0 ? -d : 0);
  }
  var k = 1/14, ag = gains[0], al = losses[0];
  for (var j = 1; j < gains.length; j++) {
    ag = ag*(1-k) + gains[j]*k; al = al*(1-k) + losses[j]*k;
  }
  return al === 0 ? 100 : 100 - 100 / (1 + ag/al);
}

function calcAtr14(bars) {
  var tr = bars.map(function(b, i) {
    if (i === 0) return b.h - b.l;
    return Math.max(b.h - b.l, Math.abs(b.h - bars[i-1].c), Math.abs(b.l - bars[i-1].c));
  });
  var e = calcEma(tr, 14);
  return e[e.length - 1];
}

function resample4h(bars1h) {
  var out = [], cur = null;
  bars1h.forEach(function(b) {
    var dt   = new Date(b.dt.replace(' ', 'T') + 'Z');
    var slot = Math.floor(dt.getUTCHours() / 4) * 4;
    var key  = dt.getUTCFullYear() + '-' + dt.getUTCMonth() + '-' + dt.getUTCDate() + '-' + slot;
    if (!cur || cur.key !== key) {
      if (cur) out.push(cur);
      cur = { key:key, o:b.o, h:b.h, l:b.l, c:b.c };
    } else { cur.h = Math.max(cur.h, b.h); cur.l = Math.min(cur.l, b.l); cur.c = b.c; }
  });
  if (cur) out.push(cur);
  return out;
}

function pad(n) { return n < 10 ? '0' + n : '' + n; }

function calcMacdHistogram(closes) {
  if (closes.length < 35) return null;
  var ema12    = calcEma(closes, 12);
  var ema26    = calcEma(closes, 26);
  var macdLine = ema12.map(function(v, i) { return v - ema26[i]; });
  var signal   = calcEma(macdLine, 9);
  return macdLine[macdLine.length - 1] - signal[signal.length - 1];
}

function calcObvAboveEma(closes, volumes) {
  if (!closes || closes.length < 12 || closes.length !== volumes.length) return null;
  var obv = [0];
  for (var i = 1; i < closes.length; i++) {
    var sign = closes[i] > closes[i-1] ? 1 : closes[i] < closes[i-1] ? -1 : 0;
    obv.push(obv[i-1] + sign * volumes[i]);
  }
  var obvEma = calcEma(obv, 10);
  return obv[obv.length - 1] > obvEma[obvEma.length - 1];
}

// ── CELL HELPERS ─────────────────────────────────────────────────────────────
function cl(sheet, row, col, val, opts) {
  var o = opts || {};
  sheet.getRange(row, col)
    .setValue(val).setBackground(o.bg||'#080e1a').setFontColor(o.fg||'#c0ccd6')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if (o.h) sheet.setRowHeight(row, o.h);
}

function mr(sheet, row, cols, val, opts) {
  var o = opts || {};
  sheet.getRange(row, 1, 1, cols).merge()
    .setValue(val).setBackground(o.bg||'#080e1a').setFontColor(o.fg||'#c0ccd6')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(row, o.h || 30);
}
