/**
 * GOLD SIGNAL DASHBOARD — Google Apps Script
 * =============================================
 * SETUP (one-time, takes 2 minutes):
 *   1. Go to sheets.new — create a blank sheet
 *   2. Extensions > Apps Script > delete default code > paste this
 *   3. Click Save (floppy icon), name the project "Gold Signal"
 *   4. Run setupTrigger() once  (top dropdown → select function → Run)
 *   5. Authorize when Google prompts
 *   6. Run updateDashboard() once to populate immediately
 *
 * The sheet will then auto-refresh every 5 minutes.
 * Access it on phone via Google Sheets app — works anywhere.
 *
 * Session: 17:30–21:00 Dubai  |  Entry window: 18:00–20:30 Dubai
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
var TIME_STOP    = 6;      // bars before time exit

// Session times in decimal UTC hours
var SESS_OPEN    = 13.50;  // 13:30 UTC = 17:30 Dubai
var SESS_CLOSE   = 17.00;  // 17:00 UTC = 21:00 Dubai
var ENTRY_START  = 14.00;  // 14:00 UTC = 18:00 Dubai (skip opening chop)
var ENTRY_END    = 16.50;  // 16:30 UTC = 20:30 Dubai

// ── TRIGGER SETUP ─────────────────────────────────────────────────────────────
function setupTrigger() {
  // Remove any existing triggers for this function
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  // Every 5 minutes
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert('✅ Auto-refresh set to every 5 minutes.\nRun updateDashboard() to populate now.');
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('GOLD SIGNAL') || ss.insertSheet('GOLD SIGNAL');

  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  // Fetch all data
  var macro   = getMacroScore();
  var bars15m = get15mBars(55);
  var bars1h  = get1hBars(220);  // ~55 bars × 4 for 4H EMA50
  var sessState = getSessionState(nowH);

  // Compute technicals
  var tech  = computeTechnicals(bars15m, bars1h);

  // Compute setup signal
  var setup = computeSetup(macro, tech, sessState);

  // Write to sheet
  writeSheet(dash, now, macro, tech, sessState, setup);
}

// ── DATA: MACRO (Yahoo Finance, cached 6h) ────────────────────────────────────
function getMacroScore() {
  var cache = CacheService.getScriptCache();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var cached = cache.get('macro_' + today);
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

  var gold = yf('GC=F',    '3mo');
  var dxy  = yf('DX-Y.NYB','3mo');
  var tnx  = yf('%5ETNX',  '3mo');
  var gdx  = yf('GDX',     '3mo');

  var result = {score: 0, dir: 1, components: [], error: null, goldPriceD: null};

  if (!gold || gold.length < 55) {
    result.error = 'Macro data unavailable';
    return result;
  }

  var n = gold.length;
  result.goldPriceD = gold[n-1];

  // EMA50 on gold
  var ema50 = ema(gold, 50);

  // All components use prior-day close (shift 1 in backtest)
  var s_trend = gold[n-2] > ema50[n-2] ? 1 : -1;
  var s_yield = (tnx && tnx.length >= 3) ? (tnx[tnx.length-2] < tnx[tnx.length-3] ? 1 : -1) : 0;
  var s_dxy   = (dxy && dxy.length >= 3) ? ((dxy[dxy.length-2]/dxy[dxy.length-3] - 1) < 0 ? 1 : -1) : 0;
  var s_mom   = (n >= 8) ? (gold[n-2] > gold[n-7] ? 1 : -1) : 0;
  var s_gdx   = (gdx && gdx.length >= 3 && n >= 3) ?
    ((gdx[gdx.length-2]/gdx[gdx.length-3] - 1) > (gold[n-2]/gold[n-3] - 1) ? 1 : -1) : 0;

  var raw = s_trend + s_yield + s_dxy + s_mom + s_gdx;
  result.score = Math.round(raw / 5 * 100) / 10;
  result.dir   = result.score >= 0 ? 1 : -1;

  var mom5pct = n >= 8 ? Math.round((gold[n-2]/gold[n-7] - 1)*1000)/10 : 0;
  var gdxVsGold = (gdx && gdx.length >= 3 && n >= 3) ?
    Math.round(((gdx[gdx.length-2]/gdx[gdx.length-3]) - (gold[n-2]/gold[n-3]))*1000)/10 : 0;

  result.components = [
    {name: 'Gold vs EMA50',    val: s_trend, note: 'Gold ' + (s_trend>0?'above':'below') + ' daily EMA50 (' + Math.round(ema50[n-2]) + ')'},
    {name: 'Yield direction',  val: s_yield, note: '10Y yield ' + (s_yield>0?'falling ↓':'rising ↑')},
    {name: 'DXY direction',    val: s_dxy,   note: 'DXY ' + (s_dxy>0?'falling ↓':'rising ↑')},
    {name: 'Gold 5d momentum', val: s_mom,   note: (mom5pct >= 0 ? '+' : '') + mom5pct + '% over 5 days'},
    {name: 'GDX lead',         val: s_gdx,   note: 'Miners ' + (s_gdx>0?'leading (+'+gdxVsGold+'%)':'lagging ('+gdxVsGold+'%)')}
  ];

  cache.put('macro_' + today, JSON.stringify(result), 21600);
  return result;
}

// ── DATA: TWELVE DATA ─────────────────────────────────────────────────────────
function tdFetch(interval, outputsize) {
  try {
    var url = 'https://api.twelvedata.com/time_series?symbol=XAU/USD' +
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
      cur = {key:key, o:b.o, h:b.h, l:b.l, c:b.c};
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
  // VWAP from 13:30 UTC today
  var now   = new Date();
  var ymd   = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sOpen = new Date(ymd + 'T13:30:00Z');

  var sessBars = bars15m.filter(function(b) {
    return new Date(b.dt.replace(' ', 'T') + 'Z') >= sOpen;
  });

  if (sessBars.length < 2) return {vwap: 0, std: 1, n: 0};

  var tpArr = sessBars.map(function(b) { return (b.h + b.l + b.c) / 3; });
  var sum = tpArr.reduce(function(a, v) { return a + v; }, 0);
  var v   = sum / tpArr.length;
  var variance = tpArr.reduce(function(a, tp) { return a + Math.pow(tp - v, 2); }, 0) / tpArr.length;
  return {vwap: v, std: Math.max(Math.sqrt(variance), 0.01), n: tpArr.length};
}

function computeTechnicals(bars15m, bars1h) {
  if (!bars15m || bars15m.length < 25) return null;

  var closes = bars15m.map(function(b) { return b.c; });
  var ema9v  = ema(closes, 9);
  var ema21v = ema(closes, 21);
  var atrV   = atr14(bars15m);
  var last   = bars15m[bars15m.length - 1];

  // 4H bars for structural trend
  var bars4h  = resampleTo4H(bars1h);
  var c4h     = bars4h.map(function(b) { return b.c; });
  var h4e9    = ema(c4h, 9);
  var h4e21   = ema(c4h, 21);
  var h4e50   = ema(c4h, 50);
  var li      = c4h.length - 1;

  var sv = sessionVwap(bars15m);

  return {
    price:    last.c,
    ema9:     ema9v[ema9v.length-1],
    ema21:    ema21v[ema21v.length-1],
    atr:      atrV,
    h4ema9:   h4e9[li],
    h4ema21:  h4e21[li],
    h4ema50:  h4e50[li],
    vwap:     sv.vwap,
    vwapStd:  sv.std,
    sessBarCount: sv.n
  };
}

// ── SESSION STATE ─────────────────────────────────────────────────────────────
function getSessionState(nowH) {
  if (nowH < SESS_OPEN)    return 'BEFORE';
  if (nowH < ENTRY_START)  return 'CHOP';
  if (nowH <= ENTRY_END)   return 'ENTRY';
  if (nowH <= SESS_CLOSE)  return 'LATE';
  return 'CLOSED';
}

// ── SETUP LOGIC ───────────────────────────────────────────────────────────────
function computeSetup(macro, tech, sessState) {
  if (!tech || macro.error) return {action:'WAIT', reason: macro.error || 'No data'};

  var dir   = macro.dir;
  var price = tech.price;
  var atrV  = tech.atr;

  // Short gate: skip if price above 4H EMA50 (gold bull trend)
  if (dir === -1 && price > tech.h4ema50) {
    return {
      action: 'SKIP',
      dir: 'SHORT',
      reason: 'Short blocked — gold above 4H EMA50 (' + fix(tech.h4ema50) + ')'
    };
  }

  // Condition checks
  var h4ok  = (dir===1 && tech.h4ema9 > tech.h4ema21) || (dir===-1 && tech.h4ema9 < tech.h4ema21);
  var emaOk = (dir===1 && tech.ema9 > tech.ema21)      || (dir===-1 && tech.ema9 < tech.ema21);
  var dist  = tech.vwapStd > 0 ? (price - tech.vwap) / tech.vwapStd : 99;
  var vwOk  = (dir===1 && dist>=-1.0 && dist<=0.3) || (dir===-1 && dist>=-0.3 && dist<=1.0);
  var sessOk = sessState === 'ENTRY';

  var checks = [
    {label: '4H trend aligned',   ok: h4ok,  note: h4ok  ? '✓' : '✗ 4H EMA9/21 not aligned'},
    {label: '15m EMA aligned',    ok: emaOk, note: emaOk ? '✓' : '✗ 15m EMA9/21 not aligned'},
    {label: 'Price in VWAP zone', ok: vwOk,  note: vwOk  ? '✓' : '✗ dist=' + Math.round(dist*100)/100 + 'σ (need |dist|<1)'},
    {label: 'Entry window open',  ok: sessOk,note: sessOk ? '✓' : '✗ Session: ' + sessState}
  ];

  if (h4ok && emaOk && vwOk && sessOk) {
    var totalSz = (ACCOUNT * RISK_PCT) / (SL_MULT * atrV);
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
      action:    'ACTIVE',
      dir:        dir === 1 ? 'LONG' : 'SHORT',
      entry:      price,
      sl:         sl,
      tp1:        tp1,
      tp2:        tp2,
      size:       Math.round(totalSz * 100) / 100,
      riskDollar: Math.round(ACCOUNT * RISK_PCT),
      atr:        atrV,
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
function writeSheet(sheet, now, macro, tech, sessState, setup) {
  sheet.clear();

  // Column widths (A–E)
  [170, 140, 140, 140, 140].forEach(function(w, i) { sheet.setColumnWidth(i+1, w); });

  var utcStr   = Utilities.formatDate(now, 'UTC',        'HH:mm') + ' UTC';
  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm') + ' Dubai';
  var r = 1;

  // ── Header ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5, '⚡  GOLD SIGNAL', {bg:'#1a1a2e', fg:'#FFD700', sz:16, bold:true}); r++;
  mrow(sheet, r, 5, '🔄 ' + utcStr + '  |  ' + dubaiStr,
       {bg:'#12122a', fg:'#606090', sz:10}); r++; r++;

  // ── Price | Score | Session ───────────────────────────────────────────────
  var price    = tech ? tech.price : (macro.goldPriceD || 0);
  var score    = macro.score || 0;
  var scoreFg  = score >= 6 ? '#00e676' : score >= 2 ? '#76ff03' : score >= 0 ? '#c8ff00' :
                 score >= -2 ? '#ff9800' : score >= -6 ? '#ff5722' : '#ff1744';
  var sMap     = {BEFORE:'⏳ Pre-session',CHOP:'⚠️ No entries yet',ENTRY:'✅ ENTRY OPEN',
                  LATE:'🕐 Entry closed',CLOSED:'🔒 Session closed'};
  var sFg      = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};

  cell(sheet, r, 1, 'GOLD PRICE',   {bg:'#1c1c3a', fg:'#8888aa', sz:10, bold:true});
  cell(sheet, r, 2, 'MACRO SCORE',  {bg:'#1c1c3a', fg:'#8888aa', sz:10, bold:true});
  cell(sheet, r, 3, 'SESSION',      {bg:'#1c1c3a', fg:'#8888aa', sz:10, bold:true});
  cell(sheet, r, 4, '4H TREND',     {bg:'#1c1c3a', fg:'#8888aa', sz:10, bold:true});
  cell(sheet, r, 5, 'ATR',          {bg:'#1c1c3a', fg:'#8888aa', sz:10, bold:true}); r++;

  var h4Trend = tech ? (tech.h4ema9 > tech.h4ema21 ? '↑ Bullish' : '↓ Bearish') : '—';
  var h4TFg   = tech ? (tech.h4ema9 > tech.h4ema21 ? '#00e676' : '#ff5252') : '#808080';
  cell(sheet, r, 1, price ? '$' + price.toFixed(2) : '—', {bg:'#0d0d20', fg:'#FFD700', sz:16, bold:true, h:44});
  cell(sheet, r, 2, score.toFixed(1),                      {bg:'#0d0d20', fg:scoreFg,   sz:16, bold:true});
  cell(sheet, r, 3, sMap[sessState]||sessState,             {bg:'#0d0d20', fg:sFg[sessState]||'#808080', sz:11, bold:true});
  cell(sheet, r, 4, h4Trend,                               {bg:'#0d0d20', fg:h4TFg, sz:12, bold:true});
  cell(sheet, r, 5, tech ? '$' + fix(tech.atr) : '—',      {bg:'#0d0d20', fg:'#b0bec5', sz:12}); r++; r++;

  // ── Setup Panel ───────────────────────────────────────────────────────────
  var sbg = setup.action==='ACTIVE' ? '#0a3d0a' : setup.action==='SKIP' ? '#3d1a0a' : '#1a1a1a';
  var sfg = setup.action==='ACTIVE' ? '#00ff88' : setup.action==='SKIP' ? '#ff6d00' : '#9e9e9e';
  var stitle = setup.action==='ACTIVE' ? '🟢  ACTIVE SETUP — ' + setup.dir :
               setup.action==='SKIP'   ? '🔴  SKIP — ' + (setup.dir||'') :
               '⏳  WAITING — Bias: ' + (setup.dir||'—');
  mrow(sheet, r, 5, stitle, {bg:sbg, fg:sfg, sz:14, bold:true}); r++;

  if (setup.action === 'ACTIVE') {
    var cols5 = ['ENTRY', 'STOP LOSS', 'TP1 (close 60%)', 'TP2 (close 40%)', 'POSITION SIZE'];
    var vals5 = ['$'+fix(setup.entry), '$'+fix(setup.sl), '$'+fix(setup.tp1), '$'+fix(setup.tp2), fix(setup.size)+' oz'];
    var fgs5  = ['#FFD700', '#ff5252', '#00e676', '#00bcd4', '#ffffff'];
    cols5.forEach(function(lbl,i) { cell(sheet, r,   i+1, lbl,     {bg:'#151515', fg:'#606060', sz:9});  });
    vals5.forEach(function(v,  i) { cell(sheet, r+1, i+1, v,       {bg:sbg,       fg:fgs5[i],  sz:13, bold:true, h:42}); });
    r += 2;
    mrow(sheet, r, 5,
         'Risk $' + setup.riskDollar + '  ·  ATR $' + fix(setup.atr) + '  ·  When TP1 hit → move SL to entry',
         {bg:'#111111', fg:'#505050', sz:9}); r++;
  } else {
    // Checklist
    if (setup.checks) {
      setup.checks.forEach(function(c) {
        var bg2 = c.ok ? '#0a2a0a' : '#2a0a0a';
        var fg2 = c.ok ? '#00c853' : '#ff5252';
        mrow(sheet, r, 5, (c.ok ? '✓  ' : '✗  ') + c.label + (c.ok ? '' : '  —  ' + c.note.replace('✗ ','')),
             {bg:bg2, fg:fg2, sz:10}); r++;
      });
    } else {
      mrow(sheet, r, 5, setup.reason || '—', {bg:sbg, fg:'#808080', sz:10}); r++;
    }
  }
  r++;

  // ── Macro Components ──────────────────────────────────────────────────────
  mrow(sheet, r, 5, 'MACRO COMPONENTS', {bg:'#1a1a2e', fg:'#6060a0', sz:10, bold:true}); r++;
  if (macro.components && macro.components.length) {
    macro.components.forEach(function(c) {
      var bg2 = c.val > 0 ? '#0a2a0a' : '#2a0a0a';
      var fg2 = c.val > 0 ? '#00c853' : '#ff5252';
      var arrow = c.val > 0 ? '▲ ' : '▼ ';
      cell(sheet, r, 1, arrow + c.name, {bg:bg2, fg:fg2, sz:10, bold:true});
      sheet.getRange(r, 2, 1, 3).merge().setValue(c.note)
        .setBackground(bg2).setFontColor('#888888').setFontSize(10)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      cell(sheet, r, 5, c.val > 0 ? '+1' : '−1', {bg:bg2, fg:fg2, sz:12, bold:true});
      sheet.setRowHeight(r, 30); r++;
    });
  }
  r++;

  // ── Key Levels ────────────────────────────────────────────────────────────
  if (tech) {
    mrow(sheet, r, 5, 'KEY LEVELS', {bg:'#1a1a2e', fg:'#6060a0', sz:10, bold:true}); r++;
    var lvls = [
      ['Session VWAP', '$'+fix(tech.vwap),  (tech.sessBarCount||0)+' bars since 13:30 UTC'],
      ['+1σ band',     '$'+fix(tech.vwap + tech.vwapStd), 'Upper VWAP band'],
      ['−1σ band',     '$'+fix(tech.vwap - tech.vwapStd), 'Lower VWAP band'],
      ['4H EMA50',     '$'+fix(tech.h4ema50), 'Short gate — only short below this'],
      ['15m EMA9',     '$'+fix(tech.ema9),  ''], ['15m EMA21', '$'+fix(tech.ema21), '']
    ];
    lvls.forEach(function(lv) {
      cell(sheet, r, 1, lv[0], {bg:'#111111', fg:'#808080', sz:10, bold:true});
      cell(sheet, r, 2, lv[1], {bg:'#111111', fg:'#e0e0e0', sz:11, bold:true});
      sheet.getRange(r, 3, 1, 3).merge().setValue(lv[2])
        .setBackground('#111111').setFontColor('#505050').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left');
      sheet.setRowHeight(r, 28); r++;
    });
    r++;
  }

  // ── Footer ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5,
       'Session 17:30–21:00 Dubai  ·  Entry window 18:00–20:30  ·  Max 2 trades/session  ·  p=0.036 ✓',
       {bg:'#080810', fg:'#303050', sz:9}); r++;
  mrow(sheet, r, 5,
       'SL=0.75×ATR  ·  TP1=1.5×ATR (60%, then move SL to BE)  ·  TP2=2.5×ATR (40%)  ·  Time stop 90min',
       {bg:'#080810', fg:'#303050', sz:9});

  SpreadsheetApp.flush();
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function fix(n) { return n !== undefined && n !== null ? (Math.round(n * 100) / 100).toFixed(2) : '—'; }

function cell(sheet, row, col, val, opts) {
  var o = opts || {};
  var rng = sheet.getRange(row, col);
  rng.setValue(val)
     .setBackground(o.bg || '#0d0d20')
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
       .setBackground(o.bg || '#0d0d20')
       .setFontColor(o.fg || '#e0e0e0')
       .setFontSize(o.sz || 11)
       .setFontWeight(o.bold ? 'bold' : 'normal')
       .setVerticalAlignment('middle')
       .setHorizontalAlignment('center')
       .setWrap(true);
  sheet.setRowHeight(row, o.h || 32);
}
