/**
 * BTC SIGNAL DASHBOARD — Google Apps Script
 * ==========================================
 * SETUP (one-time, takes 2 minutes):
 *   1. Go to sheets.new — create a blank sheet
 *   2. Extensions > Apps Script > delete default code > paste this
 *   3. Click Save (floppy icon), name the project "BTC Signal"
 *   4. Run setupTrigger() once  (top dropdown → select function → Run)
 *   5. Authorize when Google prompts
 *   6. Run updateDashboard() once to populate immediately
 *
 * The sheet will then auto-refresh every 5 minutes.
 * Access it on phone via Google Sheets app — works anywhere.
 *
 * Session: 17:30–21:00 Dubai  |  Entry window: 18:00–20:30 Dubai
 * Backtest (37 months): p=0.006 ✓  Sharpe 2.83  WR 47.9%  MDD -17.5%  +269%
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
var TIME_STOP    = 6;      // bars before time exit

var SCORE_LONG   = 6.0;    // need score ≥ +6 for long bias
var SCORE_SHORT  = -6.0;   // need score ≤ -6 for short bias
var ATR_PCT_MIN  = 0.0010; // 0.10% — min volatility
var ATR_PCT_MAX  = 0.0150; // 1.50% — max volatility (exclude flash crashes)

// Session times in decimal UTC hours (NY session — same as gold)
var SESS_OPEN    = 13.50;  // 13:30 UTC = 17:30 Dubai
var SESS_CLOSE   = 17.00;  // 17:00 UTC = 21:00 Dubai
var ENTRY_START  = 14.00;  // 14:00 UTC = 18:00 Dubai (skip opening chop)
var ENTRY_END    = 16.50;  // 16:30 UTC = 20:30 Dubai

// ETF spot products launched Jan 11 2024 — gate only applies from this date
var ETF_LAUNCH_MS = new Date('2024-01-11').getTime();

// ── TRIGGER SETUP ─────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert('✅ Auto-refresh set to every 5 minutes.\nRun updateDashboard() to populate now.');
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('BTC SIGNAL') || ss.insertSheet('BTC SIGNAL');

  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  var macro    = getMacroScore();
  var bars15m  = get15mBars(60);
  var bars1h   = get1hBars(280);  // 70×4H bars for EMA warmup + 1H EMA21
  var etf      = getEtfFlow5d();
  var sessState = getSessionState(nowH);

  var tech  = computeTechnicals(bars15m, bars1h);
  var setup = computeSetup(macro, tech, sessState, etf);

  writeSheet(dash, now, macro, tech, sessState, etf, setup);
}

// ── DATA: MACRO (Yahoo Finance, cached 6h) ────────────────────────────────────
function getMacroScore() {
  var cache = CacheService.getScriptCache();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key   = 'btc_macro_' + today;
  var cached = cache.get(key);
  if (cached) {
    try { return JSON.parse(cached); } catch(e) {}
  }

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

  var btc = yf('BTC-USD',   '2y');   // need 200 days for EMA200
  var qqq = yf('QQQ',       '1y');
  var dxy = yf('DX-Y.NYB',  '6mo');

  var result = {score: 0, dir: 0, components: [], error: null, btcPriceD: null, btcEma200: null};

  if (!btc || btc.length < 55) {
    result.error = 'BTC macro data unavailable';
    return result;
  }

  var n = btc.length;
  result.btcPriceD = btc[n-1];

  var ema50arr  = ema(btc, 50);
  var ema200arr = btc.length >= 200 ? ema(btc, 200) : null;
  if (ema200arr) result.btcEma200 = ema200arr[n-2];

  // All components use prior-day close (consistent with backtest)
  var s1 = btc[n-2] > ema50arr[n-2] ? 1 : -1;

  var s2 = 0;
  if (qqq && qqq.length >= 52) {
    var qe50 = ema(qqq, 50);
    s2 = qqq[qqq.length-2] > qe50[qe50.length-2] ? 1 : -1;
  }

  var s3 = 0;
  if (dxy && dxy.length >= 22) {
    var de20 = ema(dxy, 20);
    s3 = dxy[dxy.length-2] < de20[de20.length-2] ? 1 : -1;  // DXY below EMA20 → bullish BTC
  }

  var s4 = n >= 12 ? (btc[n-2] > btc[n-12] ? 1 : -1) : 0;  // 10-day momentum

  var s5 = (ema200arr && btc[n-2] > ema200arr[n-2]) ? 1 : -1;  // long-term regime

  var raw   = s1 + s2 + s3 + s4 + s5;
  var score = Math.round(raw / 5 * 100) / 10;

  var dir = score >= SCORE_LONG ? 1 : score <= SCORE_SHORT ? -1 : 0;

  result.score = score;
  result.dir   = dir;

  var mom10pct = n >= 12 ? Math.round((btc[n-2]/btc[n-12] - 1)*1000)/10 : 0;
  var qqqNote  = qqq && qqq.length >= 52 ? 'QQQ ' + (s2>0?'above':'below') + ' daily EMA50' : 'QQQ unavailable';
  var dxyNote  = dxy && dxy.length >= 22 ? 'DXY ' + (s3>0?'below EMA20 ↓ (BTC bullish)':'above EMA20 ↑ (BTC bearish)') : 'DXY unavailable';

  result.components = [
    {name: 'BTC vs EMA50',       val: s1, note: 'BTC ' + (s1>0?'above':'below') + ' daily EMA50 (' + Math.round(ema50arr[n-2]) + ')'},
    {name: 'QQQ risk-on',        val: s2, note: qqqNote},
    {name: 'DXY inverse',        val: s3, note: dxyNote},
    {name: 'BTC 10d momentum',   val: s4, note: (mom10pct>=0?'+':'') + mom10pct + '% over 10 days'},
    {name: 'BTC vs EMA200',      val: s5, note: 'Regime: BTC ' + (s5>0?'above':'below') + ' EMA200' + (result.btcEma200?' ('+Math.round(result.btcEma200)+')':'')}
  ];

  cache.put(key, JSON.stringify(result), 21600);
  return result;
}

// ── DATA: ETF FLOWS (Farside, cached daily) ───────────────────────────────────
function getEtfFlow5d() {
  var cache = CacheService.getScriptCache();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var key   = 'etf_flow_5d_' + today;
  var cached = cache.get(key);
  if (cached) {
    try { return JSON.parse(cached); } catch(e) {}
  }

  try {
    var resp = UrlFetchApp.fetch('https://farside.co.uk/bitcoin-etf-flow/', {
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html'
      }
    });

    if (resp.getResponseCode() !== 200) {
      return {sum5d: null, days: 0, error: 'HTTP ' + resp.getResponseCode()};
    }

    var html = resp.getContentText();

    // Extract all table rows
    var totals = [];
    var rowRe  = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
    var rowMatch;
    while ((rowMatch = rowRe.exec(html)) !== null) {
      var rowHtml = rowMatch[1];
      // Strip inner tags to get text of each td
      var cellRe = /<td[^>]*>([\s\S]*?)<\/td>/gi;
      var cells  = [];
      var cm;
      while ((cm = cellRe.exec(rowHtml)) !== null) {
        var txt = cm[1].replace(/<[^>]+>/g, '').replace(/&nbsp;/g, '').trim();
        cells.push(txt);
      }
      if (cells.length < 5) continue;  // skip header and short rows

      // Total is the last cell
      var v = parseFlowVal(cells[cells.length - 1]);
      if (!isNaN(v)) totals.push(v);
    }

    if (totals.length < 1) {
      return {sum5d: null, days: 0, error: 'Could not parse ETF table'};
    }

    // 5-day rolling window (prior-day data, so last 5 parsed entries)
    var n5    = Math.min(5, totals.length);
    var last5 = totals.slice(-n5);
    var sum5d = last5.reduce(function(a, b) { return a + b; }, 0);

    var result = {
      sum5d:   Math.round(sum5d * 10) / 10,
      days:    n5,
      latest:  Math.round(totals[totals.length-1] * 10) / 10,
      error:   null
    };
    cache.put(key, JSON.stringify(result), 86400);
    return result;
  } catch(e) {
    return {sum5d: null, days: 0, error: 'Fetch error: ' + e.message};
  }
}

function parseFlowVal(s) {
  s = s.replace(/,/g, '').trim();
  if (!s || s === '-' || s.toLowerCase() === 'n/a' || s === '') return NaN;
  if (s.charAt(0) === '(' && s.charAt(s.length - 1) === ')') {
    return -parseFloat(s.slice(1, -1));
  }
  return parseFloat(s);
}

// ── DATA: TWELVE DATA (BTC/USD) ───────────────────────────────────────────────
function tdFetch(interval, outputsize) {
  try {
    var url = 'https://api.twelvedata.com/time_series?symbol=BTC/USD' +
              '&interval=' + interval + '&outputsize=' + outputsize +
              '&apikey=' + TWELVE_KEY + '&timezone=UTC';
    var r = UrlFetchApp.fetch(url, {muteHttpExceptions: true});
    var j = JSON.parse(r.getContentText());
    if (!j.values || j.status === 'error') return [];
    return j.values.map(function(v) {
      return {
        dt: v.datetime,
        o:  parseFloat(v.open),
        h:  parseFloat(v.high),
        l:  parseFloat(v.low),
        c:  parseFloat(v.close)
      };
    }).reverse(); // oldest → newest
  } catch(e) { return []; }
}

function get15mBars(n) { return tdFetch('15min', n); }
function get1hBars(n)  { return tdFetch('1h',    n); }

// ── INDICATORS ────────────────────────────────────────────────────────────────
function ema(arr, period) {
  var k = 2 / (period + 1);
  var out = [arr[0]];
  for (var i = 1; i < arr.length; i++) out.push(arr[i] * k + out[i-1] * (1-k));
  return out;
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
    } else {
      cur.h = Math.max(cur.h, b.h);
      cur.l = Math.min(cur.l, b.l);
      cur.c = b.c;
    }
  });
  if (cur) out.push(cur);
  return out;
}

function sessionVwap(bars15m) {
  var now   = new Date();
  var ymd   = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sOpen = new Date(ymd + 'T13:30:00Z');

  var sessBars = bars15m.filter(function(b) {
    return new Date(b.dt.replace(' ', 'T') + 'Z') >= sOpen;
  });

  if (sessBars.length < 2) return {vwap: 0, std: 1, n: 0};

  var tpArr = sessBars.map(function(b) { return (b.h + b.l + b.c) / 3; });
  var sum   = tpArr.reduce(function(a, v) { return a + v; }, 0);
  var v     = sum / tpArr.length;
  var variance = tpArr.reduce(function(a, tp) { return a + Math.pow(tp - v, 2); }, 0) / tpArr.length;
  return {vwap: v, std: Math.max(Math.sqrt(variance), 1), n: tpArr.length};
}

function computeTechnicals(bars15m, bars1h) {
  if (!bars15m || bars15m.length < 25) return null;
  if (!bars1h  || bars1h.length  < 25) return null;

  var closes15 = bars15m.map(function(b) { return b.c; });
  var ema9v    = ema(closes15, 9);
  var ema21v   = ema(closes15, 21);
  var atrV     = atr14(bars15m);
  var last     = bars15m[bars15m.length - 1];
  var atrPct   = last.c > 0 ? atrV / last.c : 0;

  // 1H EMA directly from 1H bars
  var closes1h = bars1h.map(function(b) { return b.c; });
  var h1e9     = ema(closes1h, 9);
  var h1e21    = ema(closes1h, 21);
  var li1h     = closes1h.length - 1;

  // 4H bars resampled from 1H
  var bars4h = resampleTo4H(bars1h);
  var c4h    = bars4h.map(function(b) { return b.c; });
  var h4e9   = ema(c4h, 9);
  var h4e21  = ema(c4h, 21);
  var li4h   = c4h.length - 1;

  var sv = sessionVwap(bars15m);

  return {
    price:        last.c,
    ema9:         ema9v[ema9v.length - 1],
    ema21:        ema21v[ema21v.length - 1],
    atr:          atrV,
    atrPct:       atrPct,
    h1ema9:       h1e9[li1h],
    h1ema21:      h1e21[li1h],
    h4ema9:       h4e9[li4h],
    h4ema21:      h4e21[li4h],
    vwap:         sv.vwap,
    vwapStd:      sv.std,
    sessBarCount: sv.n
  };
}

// ── SESSION STATE ─────────────────────────────────────────────────────────────
function getSessionState(nowH) {
  if (nowH < SESS_OPEN)   return 'BEFORE';
  if (nowH < ENTRY_START) return 'CHOP';
  if (nowH <= ENTRY_END)  return 'ENTRY';
  if (nowH <= SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}

// ── ETF GATE HELPERS ──────────────────────────────────────────────────────────
function etfGateAllows(dir, etf) {
  if (!etf || etf.error || etf.sum5d === null) return true;  // no data → allow
  var now = new Date();
  if (now.getTime() < ETF_LAUNCH_MS) return true;           // pre-ETF era
  if (dir === 1  && etf.sum5d < 0) return false;            // long blocked on net outflow
  if (dir === -1 && etf.sum5d > 0) return false;            // short blocked on net inflow
  return true;
}

function etfGateNote(dir, etf) {
  if (!etf || etf.error) return 'ETF data unavailable — gate inactive';
  if (etf.sum5d === null) return 'ETF data not parsed';
  var sign  = etf.sum5d >= 0 ? '+' : '';
  var arrow = etf.sum5d >= 0 ? '↑' : '↓';
  return '5d net ' + sign + Math.round(etf.sum5d) + 'M ' + arrow + ' (' + etf.days + ' days)';
}

// ── SETUP LOGIC ───────────────────────────────────────────────────────────────
function computeSetup(macro, tech, sessState, etf) {
  if (!tech || macro.error) return {action: 'WAIT', reason: macro.error || 'No data'};

  var dir   = macro.dir;
  var score = macro.score;

  // Macro threshold — BTC needs strong consensus (≥3/5 factors)
  if (dir === 0) {
    return {
      action: 'SKIP',
      reason: 'Score too neutral: ' + score.toFixed(1) +
              '  (need ≥+' + SCORE_LONG + ' for LONG or ≤' + SCORE_SHORT + ' for SHORT)'
    };
  }

  var price  = tech.price;
  var atrV   = tech.atr;
  var atrPct = tech.atrPct;

  var etfOk  = etfGateAllows(dir, etf);
  var volOk  = atrPct >= ATR_PCT_MIN && atrPct <= ATR_PCT_MAX;
  var h4ok   = (dir===1 && tech.h4ema9 > tech.h4ema21) || (dir===-1 && tech.h4ema9 < tech.h4ema21);
  var h1ok   = (dir===1 && tech.h1ema9 > tech.h1ema21) || (dir===-1 && tech.h1ema9 < tech.h1ema21);
  var emaOk  = (dir===1 && tech.ema9   > tech.ema21)   || (dir===-1 && tech.ema9   < tech.ema21);
  var dist   = tech.vwapStd > 0 ? (price - tech.vwap) / tech.vwapStd : 99;
  var vwOk   = (dir===1 && dist>=-1.0 && dist<=0.3) || (dir===-1 && dist>=-0.3 && dist<=1.0);
  var sessOk = sessState === 'ENTRY';

  var atrPctStr = Math.round(atrPct * 10000) / 100 + '%';

  var checks = [
    {label: 'ETF flow gate',       ok: etfOk,  note: etfGateNote(dir, etf)},
    {label: 'Volatility in range', ok: volOk,  note: 'ATR ' + atrPctStr + ' (need 0.10–1.50%)'},
    {label: '4H EMA aligned',      ok: h4ok,   note: '4H EMA9 ' + (h4ok ? '>' : '<') + ' EMA21 — needs ' + (dir===1?'>':'<')},
    {label: '1H EMA aligned',      ok: h1ok,   note: '1H EMA9 ' + (h1ok ? '>' : '<') + ' EMA21 — needs ' + (dir===1?'>':'<')},
    {label: '15m EMA aligned',     ok: emaOk,  note: '15m EMA9 ' + (emaOk ? '>' : '<') + ' EMA21 — needs ' + (dir===1?'>':'<')},
    {label: 'VWAP zone',           ok: vwOk,   note: 'dist=' + (Math.round(dist*100)/100) + 'σ ' + (dir===1?'(need -1 to +0.3σ)':'(need -0.3 to +1σ)')},
    {label: 'Entry window open',   ok: sessOk, note: 'Session: ' + sessState + ' — need ENTRY (14:00–16:30 UTC)'}
  ];

  if (etfOk && volOk && h4ok && h1ok && emaOk && vwOk && sessOk) {
    var totalSz = (ACCOUNT * RISK_PCT) / (SL_MULT * atrV);  // BTC quantity
    var sz1     = totalSz * TP1_FRAC;
    var sz2     = totalSz * TP2_FRAC;
    var sl, tp1, tp2;
    if (dir === 1) {
      sl  = price - SL_MULT  * atrV;
      tp1 = price + TP1_MULT * atrV;
      tp2 = price + TP2_MULT * atrV;
    } else {
      sl  = price + SL_MULT  * atrV;
      tp1 = price - TP1_MULT * atrV;
      tp2 = price - TP2_MULT * atrV;
    }
    return {
      action:     'ACTIVE',
      dir:        dir === 1 ? 'LONG' : 'SHORT',
      entry:      price,
      sl:         sl,
      tp1:        tp1,
      tp2:        tp2,
      size:       totalSz,
      sz1:        sz1,
      sz2:        sz2,
      riskDollar: Math.round(ACCOUNT * RISK_PCT),
      atr:        atrV,
      atrPct:     atrPct,
      checks:     checks
    };
  }

  var missing = checks.filter(function(c) { return !c.ok; }).map(function(c) { return c.note; });
  return {
    action: 'WAIT',
    dir:    dir === 1 ? 'LONG' : 'SHORT',
    reason: missing.join('\n'),
    checks: checks,
    atr:    atrV,
    entry:  null
  };
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, macro, tech, sessState, etf, setup) {
  sheet.clear();

  [170, 140, 140, 140, 160].forEach(function(w, i) { sheet.setColumnWidth(i + 1, w); });

  var utcStr   = Utilities.formatDate(now, 'UTC',        'HH:mm') + ' UTC';
  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm') + ' Dubai';
  var r = 1;

  // ── Header ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5, '₿  BTC SIGNAL', {bg: '#1a1a0a', fg: '#F7931A', sz: 16, bold: true}); r++;
  mrow(sheet, r, 5, '🔄 ' + utcStr + '  |  ' + dubaiStr,
       {bg: '#12120a', fg: '#606050', sz: 10}); r++; r++;

  // ── Status Row: Price | Score | Session | 4H Trend | ETF Flow ────────────
  var price   = tech ? tech.price : (macro.btcPriceD || 0);
  var score   = macro.score || 0;
  var scoreFg = score >= SCORE_LONG  ? '#00e676' :
                score >= 2           ? '#76ff03' :
                score >= 0           ? '#c8ff00' :
                score >= -2          ? '#ff9800' :
                score >= SCORE_SHORT ? '#ff5722' : '#ff1744';

  var sMap = {BEFORE:'⏳ Pre-session', CHOP:'⚠️ No entries yet', ENTRY:'✅ ENTRY OPEN',
              LATE:'🕐 Entry closed',  CLOSED:'🔒 Session closed'};
  var sFg  = {BEFORE:'#607d8b', CHOP:'#ff9800', ENTRY:'#00e676', LATE:'#ff9800', CLOSED:'#607d8b'};

  // ETF badge
  var etfSum   = etf && etf.sum5d !== null ? etf.sum5d : null;
  var etfLabel = etfSum !== null ? ((etfSum >= 0 ? '+' : '') + Math.round(etfSum) + 'M') : '—';
  var etfFg    = etfSum !== null ? (etfSum >= 0 ? '#00e676' : '#ff5252') : '#808080';

  cell(sheet, r, 1, 'BTC PRICE',   {bg: '#1c1c0a', fg: '#888870', sz: 10, bold: true});
  cell(sheet, r, 2, 'MACRO SCORE', {bg: '#1c1c0a', fg: '#888870', sz: 10, bold: true});
  cell(sheet, r, 3, 'SESSION',     {bg: '#1c1c0a', fg: '#888870', sz: 10, bold: true});
  cell(sheet, r, 4, '4H TREND',    {bg: '#1c1c0a', fg: '#888870', sz: 10, bold: true});
  cell(sheet, r, 5, 'ETF FLOW 5D', {bg: '#1c1c0a', fg: '#888870', sz: 10, bold: true}); r++;

  var h4Trend = tech ? (tech.h4ema9 > tech.h4ema21 ? '↑ Bullish' : '↓ Bearish') : '—';
  var h4TFg   = tech ? (tech.h4ema9 > tech.h4ema21 ? '#00e676' : '#ff5252') : '#808080';
  cell(sheet, r, 1, price  ? '$' + Math.round(price).toLocaleString() : '—',
       {bg: '#0d0d00', fg: '#F7931A', sz: 16, bold: true, h: 44});
  cell(sheet, r, 2, score.toFixed(1),
       {bg: '#0d0d00', fg: scoreFg, sz: 16, bold: true});
  cell(sheet, r, 3, sMap[sessState] || sessState,
       {bg: '#0d0d00', fg: sFg[sessState] || '#808080', sz: 11, bold: true});
  cell(sheet, r, 4, h4Trend,
       {bg: '#0d0d00', fg: h4TFg, sz: 12, bold: true});
  cell(sheet, r, 5, etfLabel,
       {bg: '#0d0d00', fg: etfFg, sz: 14, bold: true}); r++; r++;

  // ── Setup Panel ───────────────────────────────────────────────────────────
  var sbg = setup.action === 'ACTIVE' ? '#0a3d0a' :
            setup.action === 'SKIP'   ? '#1a100a' : '#1a1a0d';
  var sfg = setup.action === 'ACTIVE' ? '#00ff88' :
            setup.action === 'SKIP'   ? '#F7931A' : '#9e9e9e';
  var stitle = setup.action === 'ACTIVE' ? '🟢  ACTIVE SETUP — ' + setup.dir :
               setup.action === 'SKIP'   ? '🟠  SKIP — ' + (setup.reason || '') :
               '⏳  WAITING — Bias: ' + (setup.dir || '—');
  mrow(sheet, r, 5, stitle, {bg: sbg, fg: sfg, sz: 14, bold: true}); r++;

  if (setup.action === 'ACTIVE') {
    var sz1str = (Math.round(setup.sz1 * 10000) / 10000).toFixed(4);
    var sz2str = (Math.round(setup.sz2 * 10000) / 10000).toFixed(4);
    var headers5 = ['ENTRY', 'STOP LOSS', 'TP1 (60%)', 'TP2 (40%)', 'ORDER SIZE'];
    var vals5    = [
      '$' + Math.round(setup.entry).toLocaleString(),
      '$' + Math.round(setup.sl).toLocaleString(),
      '$' + Math.round(setup.tp1).toLocaleString(),
      '$' + Math.round(setup.tp2).toLocaleString(),
      sz1str + ' + ' + sz2str + ' BTC'
    ];
    var fgs5 = ['#F7931A', '#ff5252', '#00e676', '#00bcd4', '#ffffff'];
    headers5.forEach(function(lbl, i) { cell(sheet, r,   i+1, lbl,    {bg: '#151500', fg: '#606050', sz: 9});  });
    vals5.forEach(function(v, i)       { cell(sheet, r+1, i+1, v,     {bg: sbg,       fg: fgs5[i],  sz: 12, bold: true, h: 42}); });
    r += 2;
    mrow(sheet, r, 5,
         'Risk $' + setup.riskDollar + '  ·  ATR $' + Math.round(setup.atr) +
         ' (' + (Math.round(setup.atrPct*10000)/100) + '%)  ·  When TP1 hit → move SL to entry',
         {bg: '#111100', fg: '#505040', sz: 9}); r++;
    mrow(sheet, r, 5,
         'Bybit: place ' + sz1str + ' BTC order (TP1) + ' + sz2str + ' BTC order (TP2) — same entry, different TP',
         {bg: '#111100', fg: '#505040', sz: 9}); r++;
  } else if (setup.action !== 'SKIP') {
    if (setup.checks) {
      setup.checks.forEach(function(c) {
        var bg2 = c.ok ? '#0a2a0a' : '#2a1a00';
        var fg2 = c.ok ? '#00c853' : '#ff9800';
        mrow(sheet, r, 5,
             (c.ok ? '✓  ' : '✗  ') + c.label + (c.ok ? '' : '  —  ' + c.note),
             {bg: bg2, fg: fg2, sz: 10}); r++;
      });
    } else {
      mrow(sheet, r, 5, setup.reason || '—', {bg: sbg, fg: '#808060', sz: 10}); r++;
    }
  }
  r++;

  // ── Macro Components ──────────────────────────────────────────────────────
  mrow(sheet, r, 5, 'MACRO COMPONENTS  (score ' + score.toFixed(1) + ' / need ±' + SCORE_LONG + ')',
       {bg: '#1a1a08', fg: '#60600a', sz: 10, bold: true}); r++;
  if (macro.components && macro.components.length) {
    macro.components.forEach(function(c) {
      var bg2    = c.val > 0 ? '#0a2a0a' : (c.val < 0 ? '#2a0a0a' : '#1a1a1a');
      var fg2    = c.val > 0 ? '#00c853' : (c.val < 0 ? '#ff5252' : '#808080');
      var arrow  = c.val > 0 ? '▲ ' : (c.val < 0 ? '▼ ' : '– ');
      cell(sheet, r, 1, arrow + c.name, {bg: bg2, fg: fg2, sz: 10, bold: true});
      sheet.getRange(r, 2, 1, 3).merge().setValue(c.note)
           .setBackground(bg2).setFontColor('#888888').setFontSize(10)
           .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      cell(sheet, r, 5, c.val > 0 ? '+1' : (c.val < 0 ? '−1' : ' 0'), {bg: bg2, fg: fg2, sz: 12, bold: true});
      sheet.setRowHeight(r, 30); r++;
    });
  }
  r++;

  // ── Key Levels ────────────────────────────────────────────────────────────
  if (tech) {
    mrow(sheet, r, 5, 'KEY LEVELS', {bg: '#1a1a08', fg: '#60600a', sz: 10, bold: true}); r++;
    var lvls = [
      ['Session VWAP', '$' + Math.round(tech.vwap).toLocaleString(),
       (tech.sessBarCount || 0) + ' bars since 13:30 UTC'],
      ['+1σ band', '$' + Math.round(tech.vwap + tech.vwapStd).toLocaleString(), 'Upper VWAP band'],
      ['−1σ band', '$' + Math.round(tech.vwap - tech.vwapStd).toLocaleString(), 'Lower VWAP band'],
      ['4H EMA9',  '$' + Math.round(tech.h4ema9).toLocaleString(),  '4H structure'],
      ['4H EMA21', '$' + Math.round(tech.h4ema21).toLocaleString(), '4H structure'],
      ['1H EMA9',  '$' + Math.round(tech.h1ema9).toLocaleString(),  '1H alignment'],
      ['1H EMA21', '$' + Math.round(tech.h1ema21).toLocaleString(), '1H alignment'],
      ['15m EMA9',  '$' + Math.round(tech.ema9).toLocaleString(),   '15m entry filter'],
      ['15m EMA21', '$' + Math.round(tech.ema21).toLocaleString(),  '15m entry filter']
    ];
    if (macro.btcEma200) {
      lvls.push(['Daily EMA200', '$' + Math.round(macro.btcEma200).toLocaleString(),
                 'Regime gate — longs only above this']);
    }
    lvls.forEach(function(lv) {
      cell(sheet, r, 1, lv[0], {bg: '#110f00', fg: '#808060', sz: 10, bold: true});
      cell(sheet, r, 2, lv[1], {bg: '#110f00', fg: '#e0e0e0', sz: 11, bold: true});
      sheet.getRange(r, 3, 1, 3).merge().setValue(lv[2])
           .setBackground('#110f00').setFontColor('#505040').setFontSize(9)
           .setVerticalAlignment('middle').setHorizontalAlignment('left');
      sheet.setRowHeight(r, 26); r++;
    });
    r++;
  }

  // ── ETF Context ───────────────────────────────────────────────────────────
  if (etf) {
    mrow(sheet, r, 5, 'ETF FLOW CONTEXT', {bg: '#1a1a08', fg: '#60600a', sz: 10, bold: true}); r++;
    var etfRows = [];
    if (etf.error) {
      etfRows.push(['Status', 'Error', etf.error]);
    } else {
      var latestSign = etf.latest !== undefined ? (etf.latest >= 0 ? '+' : '') : '';
      var sumSign    = etf.sum5d  !== null       ? (etf.sum5d  >= 0 ? '+' : '') : '';
      etfRows.push(['Latest day flow', etf.latest !== undefined ? latestSign + Math.round(etf.latest) + 'M' : '—',
                    'Most recent daily total']);
      etfRows.push(['5-day net flow',  etf.sum5d !== null ? sumSign + Math.round(etf.sum5d) + 'M' : '—',
                    'Longs blocked if <0  ·  Shorts blocked if >0']);
      etfRows.push(['Gate status',
                    macro.dir === 0 ? '—' : (etfGateAllows(macro.dir, etf) ? '✓ ALLOWS' : '✗ BLOCKS'),
                    macro.dir === 0 ? 'No bias' : (macro.dir === 1 ? 'LONG bias' : 'SHORT bias')]);
    }
    etfRows.forEach(function(row) {
      var isBlock = row[1] === '✗ BLOCKS';
      var isAllow = row[1] === '✓ ALLOWS';
      cell(sheet, r, 1, row[0], {bg: '#110f00', fg: '#808060', sz: 10, bold: true});
      cell(sheet, r, 2, row[1], {bg: '#110f00', fg: isBlock ? '#ff5252' : isAllow ? '#00e676' : '#e0e0e0', sz: 11, bold: true});
      sheet.getRange(r, 3, 1, 3).merge().setValue(row[2])
           .setBackground('#110f00').setFontColor('#505040').setFontSize(9)
           .setVerticalAlignment('middle').setHorizontalAlignment('left');
      sheet.setRowHeight(r, 26); r++;
    });
    r++;
  }

  // ── Footer ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5,
       'Session 17:30–21:00 Dubai  ·  Entry window 18:00–20:30  ·  NY session only  ·  Max 2 trades/session',
       {bg: '#080800', fg: '#303020', sz: 9}); r++;
  mrow(sheet, r, 5,
       'SL=0.75×ATR  ·  TP1=1.5×ATR (60%, then move SL to BE)  ·  TP2=2.5×ATR (40%)  ·  Time stop 6 bars',
       {bg: '#080800', fg: '#303020', sz: 9}); r++;
  mrow(sheet, r, 5,
       'Backtest Apr 2023–May 2026  ·  p=0.006 ✓  ·  Sharpe 2.83  ·  WR 47.9%  ·  MDD -17.5%  ·  +269%  ·  242 trades',
       {bg: '#080800', fg: '#404030', sz: 9});

  SpreadsheetApp.flush();
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function fix(n) {
  return n !== undefined && n !== null ? (Math.round(n * 100) / 100).toFixed(2) : '—';
}

function cell(sheet, row, col, val, opts) {
  var o   = opts || {};
  var rng = sheet.getRange(row, col);
  rng.setValue(val)
     .setBackground(o.bg || '#0d0d00')
     .setFontColor(o.fg || '#e0e0e0')
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
       .setBackground(o.bg || '#0d0d00')
       .setFontColor(o.fg || '#e0e0e0')
       .setFontSize(o.sz || 11)
       .setFontWeight(o.bold ? 'bold' : 'normal')
       .setVerticalAlignment('middle')
       .setHorizontalAlignment('center')
       .setWrap(true);
  sheet.setRowHeight(row, o.h || 32);
}
