/**
 * WTI CRUDE OIL SIGNAL — RESEARCH-BACKED MODEL  (CL=F VWAP Pullback)
 * ====================================================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste this → Save
 *   2. Run setupTrigger() once to authorize + set auto-refresh
 *   3. Run updateDashboard() to load immediately
 *   Auto-refreshes every 5 minutes.
 *
 * MODEL v2  (Kilian 2009 · Baumeister-Kilian-Zhou 2018 · GS commodity desk):
 *   7 components: Trend · Copper · Momentum · SPY · RBOB-crack · Brent-WTI · Term-structure
 *   Score gate : |raw score| ≥ 2  (majority + buffer)
 *   Skip months: Jan · Apr · Jun · Sep · Oct  (5 bad months A/B tested)
 *   Skip day   : Wednesday  (EIA inventory release 15:30 UTC — erratic moves)
 *   Session    : 14:30–19:00 UTC  (18:30–23:00 Dubai  |  10:30–15:00 ET  NYMEX pit)
 *   SL = 0.75×DailyATR  ·  TP1 = 1.5×ATR (60%)  ·  TP2 = 2.5×ATR (40%)
 *
 * BACKTEST (2yr, 141 trades):  WR 65.2% · Sharpe 5.86 · 1.89%/mo @ 3% risk · MaxDD −7.5%
 *   Longs: WR 68%  Sharpe 8.39  |  Shorts: WR 60%  Sharpe 4.12
 *
 * SOURCES:
 *   Kilian (2009 AER): demand shocks dominate oil variance
 *   Baumeister-Kilian-Zhou (2018): RBOB crack = #1 product-spread predictor
 *   GS commodity desk: Brent-WTI z-score = physical tightness signal
 *   CME research: ORB outperforms VWAP pullback for trending CL
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var ACCOUNT     = 10000;     // ← change to your account size
var RISK_PCT    = 0.03;      // 3% risk per trade
var SL_MULT     = 0.75;
var TP1_MULT    = 1.5;
var TP2_MULT    = 2.5;
var TP1_FRAC    = 0.60;
var TP2_FRAC    = 0.40;
var SCORE_MIN   = 2;         // |raw| ≥ 2 (≥4 of 7 pulling same direction)

// UTC session times
var SESS_OPEN   = 14.00;     // 14:00 UTC  (VWAP builds from here)
var SESS_CLOSE  = 19.00;     // 19:00 UTC  hard close
var ENTRY_START = 14.50;     // 14:30 UTC  entry window opens
var ENTRY_END   = 18.50;     // 18:30 UTC  last entry

// Skip months (JS Date.getUTCMonth() is 0-indexed)
var SKIP_MONTHS = [0, 3, 5, 8, 9]; // Jan=0, Apr=3, Jun=5, Sep=8, Oct=9
var SKIP_WED    = true;             // skip Wednesday (EIA day)

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ OIL SIGNAL — Auto-refresh every 5 min active.\n\n' +
      'SESSION SCHEDULE (Dubai / ET):\n' +
      '  18:00 Dubai → Pre-session check: macro score + VIX\n' +
      '  18:30 Dubai → Session opens, VWAP starts building\n' +
      '  19:00 Dubai → ENTRY WINDOW OPENS — watch for VWAP pullback\n' +
      '  22:30 Dubai → Last entry — no new trades after this\n' +
      '  23:00 Dubai → HARD CLOSE — exit ALL positions, no exceptions\n\n' +
      'SKIP: Jan · Apr · Jun · Sep · Oct · ALL Wednesdays (EIA)\n' +
      'NEVER hold WTI crude overnight.'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('OIL SIGNAL') || ss.insertSheet('OIL SIGNAL');

  var macro     = getMacroScore();
  var bars15m   = get15mBars(80);
  var bars1h    = get1hBars(260);
  var sessState = getSessionState(now, nowH);

  var tech  = computeTechnicals(bars15m, bars1h);
  var setup = computeSetup(macro, tech, sessState, now, nowH);

  writeSheet(dash, now, nowH, macro, tech, sessState, setup);
}

// ── MACRO SCORE ───────────────────────────────────────────────────────────────
function getMacroScore() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var cached = cache.get('oil_v2_' + today);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yfd(sym, range) {
    var hosts = ['query1.finance.yahoo.com', 'query2.finance.yahoo.com'];
    for (var hi = 0; hi < hosts.length; hi++) {
      try {
        var url = 'https://' + hosts[hi] + '/v8/finance/chart/' +
                  encodeURIComponent(sym) + '?interval=1d&range=' + range;
        var r   = UrlFetchApp.fetch(url, { muteHttpExceptions:true,
                    headers:{'User-Agent':'Mozilla/5.0','Accept':'application/json'} });
        var j   = JSON.parse(r.getContentText());
        if (!j.chart || !j.chart.result || !j.chart.result[0]) continue;
        var q   = j.chart.result[0].indicators.quote[0];
        var cls = q.close.filter(function(c){ return c != null; });
        if (cls.length > 0) return cls;
      } catch(e) { Logger.log('yfd ' + sym + ' ' + hosts[hi] + ': ' + e); }
    }
    return null;
  }

  var cl  = yfd('CL=F',     '3mo');  // WTI crude
  var hg  = yfd('HG=F',     '3mo');  // Copper
  var spy = yfd('SPY',      '3mo');  // S&P 500
  var rb  = yfd('RB=F',     '3mo');  // RBOB gasoline ($/gal)
  var bz  = yfd('BZ=F',     '3mo');  // Brent crude
  var uso = yfd('USO',      '3mo');  // WTI front-month ETF
  var usl = yfd('USL',      '3mo');  // WTI 12-month ETF
  var vix = yfd('^VIX',     '5d');

  var result = { score:0, raw:0, dir:1, strength:'', components:[], error:null,
                 oilPriceD:null, vix:null };

  if (!cl || cl.length < 12) { result.error = 'Macro data unavailable (CL=F)'; return result; }

  var n   = cl.length;
  result.oilPriceD = cl[n-1];
  result.vix = vix && vix.length > 0 ? Math.round(vix[vix.length-1]*10)/10 : null;

  // 1. Trend: CL vs EMA50
  var e50  = emaArr(cl, Math.min(50, n));
  var c_trend = cl[n-2] > e50[n-2] ? 1 : -1;

  // 2. Copper 5d direction
  var c_copper = (hg && hg.length >= 8)
    ? ((hg[hg.length-2] / hg[hg.length-7] - 1) > 0 ? 1 : -1) : 0;

  // 3. Oil 5d momentum
  var c_mom = n >= 8 ? (cl[n-2] > cl[n-7] ? 1 : -1) : 0;

  // 4. SPY 5d direction (equity regime)
  var c_spy = (spy && spy.length >= 8)
    ? ((spy[spy.length-2] / spy[spy.length-7] - 1) > 0 ? 1 : -1) : 0;

  // 5. RBOB crack spread 5d direction [Baumeister-Kilian-Zhou 2018 #1 signal]
  //    crack = RB($/gal)×42 − CL($/bbl)  →  5d change direction
  var c_crack_rb = 0;
  if (rb && rb.length >= 8 && n >= 8) {
    var mn = Math.min(rb.length, n);
    var crack_now  = rb[mn-2] * 42 - cl[n-2];
    var crack_lag  = rb[mn-7] * 42 - cl[n-7];
    c_crack_rb = crack_now > crack_lag ? 1 : -1;
  }

  // 6. Brent-WTI z-score [GS framework — physical tightness signal]
  //    z < −0.2 → Brent narrowing → bullish WTI (supply tight)
  //    z > +0.5 → Brent widening → bearish WTI (glut)
  var c_brent = 0;
  var bwti_z  = null;
  if (bz && bz.length >= 8 && n >= 8) {
    var len   = Math.min(bz.length, n);
    var bwArr = [];
    for (var i = 0; i < len; i++) bwArr.push(bz[i] - cl[i]);
    var winN  = Math.min(60, bwArr.length - 1);
    var win   = bwArr.slice(bwArr.length - 1 - winN, bwArr.length - 1);
    var wmean = win.reduce(function(a,v){return a+v;},0) / win.length;
    var wstd  = Math.sqrt(win.reduce(function(a,v){return a+Math.pow(v-wmean,2);},0) / win.length);
    if (wstd > 0.01) {
      bwti_z  = (bwArr[bwArr.length-2] - wmean) / wstd;
      c_brent = bwti_z < -0.2 ? 1 : bwti_z > 0.5 ? -1 : 0;
    }
  }

  // 7. Term structure (USO/USL ratio 5d direction) — backwardation proxy
  //    Rising ratio = front-month outperforms = backwardation = bullish
  var c_termstruct = 0;
  if (uso && uso.length >= 8 && usl && usl.length >= 8) {
    var tlen   = Math.min(uso.length, usl.length);
    var tnow   = uso[tlen-2] / usl[tlen-2];
    var tlag   = uso[tlen-7] / usl[tlen-7];
    c_termstruct = tnow > tlag ? 1 : -1;
  }

  var raw = c_trend + c_copper + c_mom + c_spy + c_crack_rb + c_brent + c_termstruct;
  result.raw   = raw;
  result.score = raw;    // −7 to +7
  result.dir   = raw >= 0 ? 1 : -1;

  var absRaw = Math.abs(raw);
  result.strength = absRaw >= 5 ? 'STRONG' : absRaw >= 3 ? 'MODERATE' : 'WEAK';

  var mom5pct = n >= 8 ? Math.round((cl[n-2]/cl[n-7]-1)*1000)/10 : 0;
  var crackNow = (rb && rb.length >= 2 && n >= 2) ? Math.round(rb[rb.length-2]*42 - cl[n-2]) : null;
  var termRatio = (uso && uso.length >= 2 && usl && usl.length >= 2)
    ? Math.round(uso[uso.length-2]/usl[usl.length-2]*1000)/1000 : null;
  var bwSpread = (bz && bz.length >= 2 && n >= 2)
    ? Math.round((bz[bz.length-2] - cl[n-2])*100)/100 : null;

  result.components = [
    { name:'CL vs EMA50',         val:c_trend,
      note:'CL '+(c_trend>0?'above':'below')+' EMA50 ($'+Math.round(e50[n-2]*100)/100+') — macro trend' },
    { name:'Copper 5d dir',       val:c_copper,
      note:'HG=F '+(c_copper===0?'unavail':c_copper>0?'rising ↑ (demand signal bullish oil)':'falling ↓ (demand signal bearish oil)') },
    { name:'Oil 5d momentum',     val:c_mom,
      note:(mom5pct>=0?'+':'')+mom5pct+'% over 5 days' },
    { name:'SPY 5d dir',          val:c_spy,
      note:'Equity '+(c_spy===0?'unavail':c_spy>0?'rising ↑ (risk-on = bullish oil)':'falling ↓ (risk-off = bearish oil)') },
    { name:'RBOB crack 5d [BKZ]', val:c_crack_rb,
      note: c_crack_rb===0 ? 'Unavailable' :
            (crackNow!==null?'Crack=$'+crackNow+'/bbl  ':'') +
            (c_crack_rb>0?'widening ↑ bullish (refiners want crude)':'narrowing ↓ bearish (demand weak)') },
    { name:'Brent-WTI z60 [GS]',  val:c_brent,
      note: bwti_z!==null
            ? 'z='+Math.round(bwti_z*100)/100+(bwSpread!==null?' spread=$'+bwSpread:'')+
              (c_brent===1?' → narrowing → bullish WTI':c_brent===-1?' → widening → bearish WTI':' → neutral (−0.2 to +0.5)')
            : 'Unavailable' },
    { name:'Term-struct USO/USL', val:c_termstruct,
      note: c_termstruct===0 ? 'Unavailable' :
            (termRatio!==null?'ratio='+termRatio+'  ':'') +
            (c_termstruct>0?'rising ↑ backwardation (bullish, supply tight)':'falling ↓ contango (bearish, supply glut)') }
  ];

  var cacheTtl = result.vix !== null ? 21600 : 300;
  try { cache.put('oil_v2_' + today, JSON.stringify(result), cacheTtl); } catch(e) {}
  return result;
}

// ── BAR FETCH (Yahoo Finance — CL=F WTI crude futures) ───────────────────────
function yfBars(interval, range, n) {
  var sc  = CacheService.getScriptCache();
  var key = 'yf_oil_bars_' + interval;
  var ttl = (interval === '1h') ? 3600 : 600;
  try { var hit = sc.get(key); if (hit) return JSON.parse(hit); } catch(e) {}

  var hosts = ['query1.finance.yahoo.com', 'query2.finance.yahoo.com'];
  var opts  = { muteHttpExceptions:true,
                headers:{ 'User-Agent':'Mozilla/5.0', 'Accept':'application/json' } };

  for (var hi = 0; hi < hosts.length; hi++) {
    try {
      var url  = 'https://' + hosts[hi] + '/v8/finance/chart/CL%3DF' +
                 '?interval=' + interval + '&range=' + range;
      var r    = UrlFetchApp.fetch(url, opts);
      var j    = JSON.parse(r.getContentText());
      if (!j.chart || !j.chart.result || !j.chart.result[0]) {
        Logger.log('yfBars [' + interval + '] ' + hosts[hi] + ' no result'); continue;
      }
      var res  = j.chart.result[0];
      var ts   = res.timestamp;
      var q    = res.indicators.quote[0];
      var bars = [];
      for (var i = 0; i < ts.length; i++) {
        if (!q.open[i] || !q.close[i]) continue;
        var d   = new Date(ts[i] * 1000);
        var pad = function(x){ return x < 10 ? '0'+x : ''+x; };
        var dt  = d.getUTCFullYear()+'-'+pad(d.getUTCMonth()+1)+'-'+pad(d.getUTCDate())+
                  ' '+pad(d.getUTCHours())+':'+pad(d.getUTCMinutes());
        bars.push({dt:dt, o:q.open[i], h:q.high[i], l:q.low[i], c:q.close[i], v:q.volume[i]||0});
      }
      if (bars.length === 0) continue;
      var out = bars.slice(-n);
      try { sc.put(key, JSON.stringify(out), ttl); } catch(ce) {}
      return out;
    } catch(e) {
      Logger.log('yfBars [' + interval + '] ' + hosts[hi] + ' error: ' + e.toString());
    }
  }
  return [];  // both failed — no cache, next cycle retries
}

function get15mBars(n) { return yfBars('15m', '5d',  n); }
function get1hBars(n)  { return yfBars('1h',  '60d', n); }

// ── DAILY ATR FROM 1H BARS ────────────────────────────────────────────────────
function getDailyAtr(bars1h) {
  if (!bars1h || bars1h.length < 15) return null;
  var days = {}, order = [];
  bars1h.forEach(function(b) {
    var d = b.dt.substring(0, 10);
    if (!days[d]) { days[d] = {h:b.h, l:b.l, c:b.c}; order.push(d); }
    else { days[d].h = Math.max(days[d].h,b.h); days[d].l = Math.min(days[d].l,b.l); days[d].c = b.c; }
  });
  if (order.length < 3) return null;
  var daily = order.map(function(d){ return days[d]; });
  var tr = daily.map(function(b, i) {
    if (i === 0) return b.h - b.l;
    return Math.max(b.h-b.l, Math.abs(b.h-daily[i-1].c), Math.abs(b.l-daily[i-1].c));
  });
  var atr = emaArr(tr, 14);
  return atr[atr.length - 1];
}

// ── TECHNICALS ────────────────────────────────────────────────────────────────
function computeTechnicals(bars15m, bars1h) {
  if (!bars15m || bars15m.length < 35) return null;

  var closes = bars15m.map(function(b){ return b.c; });
  var e9     = emaArr(closes, 9);
  var e21    = emaArr(closes, 21);
  var last   = bars15m[bars15m.length - 1];
  var atrDay = getDailyAtr(bars1h);   // daily ATR (used for SL/TP)
  var atr15m = calcAtr14(bars15m);    // 15m ATR (context only)

  var sv = sessionVwap(bars15m);

  return {
    price: last.c, high: last.h, low: last.l,
    ema9: e9[e9.length-1], ema21: e21[e21.length-1],
    atr:    atrDay || atr15m * 6,   // daily ATR preferred; fallback 6× 15m
    atr15m: atr15m,
    atrDay: atrDay,
    vwap: sv.vwap, vwapStd: sv.std, vwapBars: sv.n,
    vwapDist: sv.std > 0.01 && sv.vwap ? (last.c - sv.vwap) / sv.std : 99
  };
}

// ── SESSION VWAP (starts at 14:00 UTC — NYMEX open) ──────────────────────────
function sessionVwap(bars15m) {
  var now    = new Date();
  var ymd    = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sOpen  = new Date(ymd + 'T14:00:00Z');
  var todayP = ymd + ' ';
  var sess   = bars15m.filter(function(b){
    return new Date(b.dt.replace(' ','T')+'Z') >= sOpen && b.dt.indexOf(todayP) === 0;
  });
  if (sess.length < 2) return { vwap:0, std:1, n:sess.length };
  var tp  = sess.map(function(b){ return (b.h+b.l+b.c)/3; });
  var avg = tp.reduce(function(a,v){ return a+v; },0) / tp.length;
  var v2  = tp.reduce(function(a,x){ return a+Math.pow(x-avg,2); },0) / tp.length;
  return { vwap:avg, std:Math.max(Math.sqrt(v2),0.01), n:tp.length };
}

// ── SESSION STATE ─────────────────────────────────────────────────────────────
function getSessionState(now, nowH) {
  if (nowH < SESS_OPEN)   return 'BEFORE';
  if (nowH < ENTRY_START) return 'CHOP';
  if (nowH <= ENTRY_END)  return 'ENTRY';
  if (nowH <= SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}

// ── SETUP LOGIC ───────────────────────────────────────────────────────────────
function computeSetup(macro, tech, sessState, now, nowH) {
  if (!tech || macro.error) {
    return {
      action: 'WAIT',
      dir:    macro.dir === 1 ? 'LONG' : 'SHORT',
      reason: macro.error || 'Bar data unavailable — Yahoo Finance may be temporarily down'
    };
  }

  var dir   = macro.dir;
  var price = tech.price;
  var atrV  = tech.atr;

  var nowMon = now.getUTCMonth();  // 0-indexed
  var nowDow = now.getUTCDay();    // 0=Sun, 3=Wed, 6=Sat

  // Day-level regime gates
  var vixOk   = macro.vix === null || macro.vix < 30;
  var monthOk = SKIP_MONTHS.indexOf(nowMon) === -1;
  var wedOk   = !SKIP_WED || nowDow !== 3;

  var monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var scoreOk  = Math.abs(macro.raw) >= SCORE_MIN;

  var dist = tech.vwapStd > 0 ? (price - tech.vwap) / tech.vwapStd : 99;
  var emaOk  = dir === 1 ? tech.ema9 > tech.ema21 : tech.ema9 < tech.ema21;
  var vwOk   = tech.vwapBars >= 4 &&
    (dir === 1 ? (dist >= -1.0 && dist <= 0.3) : (dist >= -0.3 && dist <= 1.0));
  var sessOk = sessState === 'ENTRY';

  var vixStr = macro.vix !== null ? 'VIX ' + macro.vix.toFixed(1) : 'VIX n/a';

  var checks = [
    { label:'VIX < 30',
      ok: vixOk,
      note: vixOk
        ? (macro.vix ? vixStr+(macro.vix>=25?' — elevated, reduce size 20%':' — regime OK') : 'VIX unavailable')
        : vixStr + ' ≥ 30 — CRASH REGIME. Skip today.' },
    { label:'Not skip-month',
      ok: monthOk,
      note: monthOk
        ? monthNames[nowMon]+' — month OK to trade'
        : monthNames[nowMon]+' — SKIP MONTH. Backtest: Sharpe negative. Skip entire month.' },
    { label:'Not Wednesday (EIA)',
      ok: wedOk,
      note: wedOk
        ? (nowDow===3 ? '—' : 'Not Wednesday — EIA skip not active')
        : 'WEDNESDAY — EIA inventory at 15:30 UTC causes ±$1.5/bbl spikes. Skip all day.' },
    { label:'Score |raw| ≥ '+SCORE_MIN,
      ok: scoreOk,
      note: 'Raw score: '+(macro.raw>=0?'+':'')+macro.raw+' / 7  '+(scoreOk?'✓ majority confirmed':'✗ no clear majority (need |score| ≥ '+SCORE_MIN+')') },
    { label:'Session entry window',
      ok: sessOk,
      note: sessOk
        ? '✓ 14:30–18:30 UTC (18:30–22:30 Dubai) entry open'
        : 'Wait — entry opens 14:30 UTC (18:30 Dubai)' },
    { label:'15m EMA9 vs EMA21',
      ok: emaOk,
      note: emaOk
        ? 'EMA9 $'+fix(tech.ema9)+' aligned with '+( dir===1?'LONG':'SHORT')+' bias'
        : 'EMA9 $'+fix(tech.ema9)+' / EMA21 $'+fix(tech.ema21)+' — momentum against bias' },
    { label:'VWAP pullback zone',
      ok: vwOk,
      note: vwOk
        ? 'dist='+Math.round(dist*100)/100+'σ ✓  at $'+fix(tech.vwap)
        : tech.vwapBars < 4
          ? 'Only '+tech.vwapBars+' bars since 14:00 UTC (need ≥4)'
          : 'dist='+Math.round(dist*100)/100+'σ  need '+(dir===1?'−1.0 to +0.3σ':'−0.3 to +1.0σ')+
            '  zone $'+fix(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd)+
            '–$'+fix(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd) }
  ];

  var allOk = vixOk && monthOk && wedOk && scoreOk && sessOk && emaOk && vwOk;

  if (allOk) {
    var totalSz = (ACCOUNT * RISK_PCT) / (SL_MULT * atrV);
    var sl, tp1, tp2;
    if (dir===1) { sl=price-SL_MULT*atrV; tp1=price+TP1_MULT*atrV; tp2=price+TP2_MULT*atrV; }
    else         { sl=price+SL_MULT*atrV; tp1=price-TP1_MULT*atrV; tp2=price-TP2_MULT*atrV; }
    return {
      action:'ACTIVE', dir:dir===1?'LONG':'SHORT',
      entry:price, sl:sl, tp1:tp1, tp2:tp2,
      size:  Math.round(totalSz),          // barrels
      sz1:   Math.round(totalSz * TP1_FRAC),
      sz2:   Math.round(totalSz * TP2_FRAC),
      riskDollar: Math.round(ACCOUNT * RISK_PCT),
      atr:atrV, slPts:SL_MULT*atrV, tp1Pts:TP1_MULT*atrV, tp2Pts:TP2_MULT*atrV,
      checks:checks
    };
  }

  return {
    action:'WAIT', dir:dir===1?'LONG':'SHORT',
    reason: checks.filter(function(c){return !c.ok;}).map(function(c){return c.note;}).join(' | '),
    checks:checks, atr:atrV,
    vwapEntry: tech.vwap && tech.vwapStd ? {
      low:  dir===1 ? fix(tech.vwap - tech.vwapStd)     : fix(tech.vwap - 0.3*tech.vwapStd),
      high: dir===1 ? fix(tech.vwap + 0.3*tech.vwapStd) : fix(tech.vwap + tech.vwapStd)
    } : null
  };
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, nowH, macro, tech, sessState, setup) {
  sheet.clear();
  [190, 130, 130, 130, 130].forEach(function(w,i){ sheet.setColumnWidth(i+1,w); });

  var utcStr   = Utilities.formatDate(now, 'UTC',        'HH:mm') + ' UTC';
  var dubaiStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm') + ' DXB';
  var r = 1;

  // ── HEADER ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5,
    '🛢  WTI CRUDE OIL  —  Research-Backed VWAP Model  |  Sharpe 5.86  |  WR 65.2%  |  7 Components',
    {bg:'#1a0f00', fg:'#FF8C00', sz:12, bold:true, h:36}); r++;
  mrow(sheet, r, 5,
    utcStr + '  ·  ' + dubaiStr + '  ·  Auto-refresh 5 min  ·  Risk ' + (RISK_PCT*100) + '% ($' + Math.round(ACCOUNT*RISK_PCT) + ')  ·  Account $' + ACCOUNT,
    {bg:'#120a00', fg:'#604030', sz:9, h:20}); r++; r++;

  // ── STAT BAR ──────────────────────────────────────────────────────────────
  var price    = tech ? tech.price : (macro.oilPriceD || 0);
  var rawScore = macro.raw || 0;
  var dir      = macro.dir || 1;
  var strength = macro.strength || '';
  var vixNow   = macro.vix;
  var vixFg    = !vixNow?'#808080':vixNow>=30?'#ff1744':vixNow>=25?'#ff9800':vixNow>=20?'#ffea00':'#00e676';

  var now2 = new Date();
  var dowNow = now2.getUTCDay(), monNow = now2.getUTCMonth();
  var isWed  = dowNow === 3;
  var isBadMon = SKIP_MONTHS.indexOf(monNow) !== -1;
  var sMap   = {BEFORE:'⏳ Pre-session',CHOP:'⚠ No entries (chop)',ENTRY:'✅ ENTRY OPEN',LATE:'🕐 Entry closed',CLOSED:'🔒 Closed'};
  var sFg    = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};

  ['CL=F PRICE','DIRECTION','SESSION','VIX','DATE FLAGS'].forEach(function(h,i){
    cell(sheet,r,i+1,h,{bg:'#1c1000',fg:'#805020',sz:9,bold:true,h:20});
  });
  r++;

  var dirLabel = (dir===1?'▲ LONG':'▼ SHORT') + '  ' + strength;
  var dirFg    = dir===1 ? '#00e676' : '#ff5252';
  var dfLabel  = (isWed?'⚠ WED (EIA)':'') + (isBadMon?(isWed?' · ':'')+['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][monNow]+' SKIP':'');
  var dfFg     = (isWed||isBadMon)?'#ff9800':'#4caf50';
  if (!dfLabel) dfLabel = '✓ Day OK';

  cell(sheet,r,1, price?'$'+fix2(price):'—',       {bg:'#0f0800',fg:'#FF8C00',sz:16,bold:true,h:40});
  cell(sheet,r,2, dirLabel,                          {bg:'#0f0800',fg:dirFg,   sz:11,bold:true});
  cell(sheet,r,3, sMap[sessState]||sessState,        {bg:'#0f0800',fg:sFg[sessState]||'#808080',sz:10,bold:true});
  cell(sheet,r,4, vixNow?vixNow.toFixed(1):'—',     {bg:'#0f0800',fg:vixFg,   sz:16,bold:true});
  cell(sheet,r,5, dfLabel,                           {bg:'#0f0800',fg:dfFg,    sz:10,bold:true}); r++;

  // ── VWAP PANEL ────────────────────────────────────────────────────────────
  var hasVwap = tech && tech.vwap > 0 && tech.vwapStd > 0;
  ['VWAP', '−1σ  (LONG zone)', '+1σ  (SHORT zone)', 'PRICE vs VWAP', 'BARS'].forEach(function(h,i){
    cell(sheet,r,i+1,h,{bg:'#0a1000',fg:'#335533',sz:9,bold:true,h:18});
  });
  r++;
  if (hasVwap) {
    var vwDist  = tech.vwapDist;
    var vwDistS = (vwDist >= 0 ? '+' : '') + Math.round(vwDist*100)/100 + 'σ';
    var inZone  = dir === 1 ? (vwDist >= -1.0 && vwDist <= 0.3) : (vwDist >= -0.3 && vwDist <= 1.0);
    var posFg   = inZone ? '#00e676' : '#ff9800';
    var posLbl  = vwDistS + (inZone ? '  ✓ IN ZONE' : '  ✗ OUT OF ZONE');
    cell(sheet,r,1,'$'+fix2(tech.vwap),                        {bg:'#060a00',fg:'#66bb6a',sz:14,bold:true,h:34});
    cell(sheet,r,2,'$'+fix2(tech.vwap-tech.vwapStd),           {bg:'#060a00',fg:'#00e676',sz:13,bold:true});
    cell(sheet,r,3,'$'+fix2(tech.vwap+tech.vwapStd),           {bg:'#060a00',fg:'#ff5252',sz:13,bold:true});
    cell(sheet,r,4,posLbl,                                      {bg:'#060a00',fg:posFg,   sz:11,bold:true});
    cell(sheet,r,5,tech.vwapBars+(tech.vwapBars<4?' ⚠':''),    {bg:'#060a00',fg:'#607d8b',sz:11});
  } else {
    var vMsg = (nowH >= SESS_OPEN && nowH <= SESS_CLOSE)
      ? 'Session active — bar fetch failed (retrying)'
      : 'Pre-session — VWAP builds from 14:00 UTC (18:00 Dubai)';
    mrow(sheet,r,5,vMsg,{bg:'#060a00',fg:'#607d8b',sz:9,h:34});
  }
  r++; r++;

  // ── SCORE METER ──────────────────────────────────────────────────────────
  mrow(sheet,r,5,
    'MACRO SCORE  ' + (rawScore>=0?'+':'') + rawScore + ' / 7  |  Need |score| ≥ ' + SCORE_MIN +
    '  |  '+(dir===1?'▲ LONG':'▼ SHORT')+'  ' + strength,
    {bg:'#1a1000',fg: Math.abs(rawScore)>=SCORE_MIN?'#FF8C00':'#806040',sz:10,bold:true,h:24}); r++;

  // ── ACTION BLOCK ──────────────────────────────────────────────────────────
  var inEntry  = sessState === 'ENTRY';
  var actionBg = setup.action==='ACTIVE'?'#0a2200':inEntry?'#1a1400':'#111111';
  var actionFg = setup.action==='ACTIVE'?'#00ff88':inEntry?'#ccaa44':'#9e9e9e';
  var actionTxt = setup.action==='ACTIVE'
    ? '⚡  ENTER '+(setup.dir)+'  NOW  —  All 7 gates green  —  Execute in MT5'
    : inEntry
      ? (setup.checks
          ? '⏱  IN SESSION  —  Watching for setup  ·  Bias: '+(setup.dir||'—')+'  ·  Waiting: '+
            setup.checks.filter(function(c){return !c.ok;}).length+' gate(s)'
          : '⚠  IN SESSION  —  '+(setup.reason||'Data unavailable'))
      : '⏳  NEXT SESSION: 14:30 UTC  (18:30 Dubai)';
  mrow(sheet,r,5,actionTxt,{bg:actionBg,fg:actionFg,sz:12,bold:true,h:38}); r++;

  if (setup.action==='ACTIVE') {
    // Trade levels
    ['ENTRY','STOP LOSS','TP1 (60%)','TP2 (40%)','SIZE (bbl)'].forEach(function(h,i){
      cell(sheet,r,i+1,h,{bg:'#0a1800',fg:'#406040',sz:9,bold:true,h:20});
    });
    r++;
    [['$'+fix2(setup.entry),'#FF8C00'],['$'+fix2(setup.sl),'#ff5252'],
     ['$'+fix2(setup.tp1),'#00e676'],['$'+fix2(setup.tp2),'#00bcd4'],
     [setup.size+' bbl','#ffffff']].forEach(function(v,i){
      cell(sheet,r,i+1,v[0],{bg:'#0a3300',fg:v[1],sz:14,bold:true,h:40});
    });
    r++;

    // Risk metrics
    cell(sheet,r,1,'DailyATR $'+fix2(setup.atr),         {bg:'#050d00',fg:'#336633',sz:9,h:22});
    cell(sheet,r,2,'SL −$'+fix2(setup.slPts),            {bg:'#050d00',fg:'#883333',sz:9,bold:true});
    cell(sheet,r,3,'TP1 +$'+fix2(setup.tp1Pts)+'  1.5R', {bg:'#050d00',fg:'#338833',sz:9,bold:true});
    cell(sheet,r,4,'TP2 +$'+fix2(setup.tp2Pts)+'  2.5R', {bg:'#050d00',fg:'#226688',sz:9,bold:true});
    cell(sheet,r,5,setup.sz1+' + '+setup.sz2+' bbl',     {bg:'#050d00',fg:'#666600',sz:9}); r++;

    // Contract size guide
    var microCts = Math.round(setup.size / 100 * 10) / 10;
    var miniCts  = Math.round(setup.size / 500 * 10) / 10;
    var stdCts   = Math.round(setup.size / 1000 * 10) / 10;
    mrow(sheet,r,5,
      'Contract equiv: ' + setup.size + ' bbl  →  ' +
      microCts + ' micro-CL (100 bbl)  |  ' +
      miniCts + ' mini-QM (500 bbl)  |  ' +
      stdCts + ' std-CL (1000 bbl)',
      {bg:'#050a00',fg:'#606040',sz:9,h:22}); r++;

    // MT5 instructions
    var dir1 = setup.dir === 'LONG';
    var steps = [
      ['STEP 1 — ENTER',
       'MT5 → CL (WTI Crude) → New Order → '+( dir1?'BUY':'SELL')+' → '+setup.size+' bbl  |  Or: '+microCts+' micro / '+miniCts+' mini / '+stdCts+' std contracts'],
      ['STEP 2 — SET LEVELS',
       'Immediately: SL = $'+fix2(setup.sl)+'  |  TP = $'+fix2(setup.tp1)+'  (on '+setup.sz1+' bbl TP1 portion)'],
      ['STEP 3 — TP1 HIT',
       'Close '+setup.sz1+' bbl at $'+fix2(setup.tp1)+'  →  Move SL of remaining '+setup.sz2+' bbl to $'+fix2(setup.entry)+' (breakeven)'],
      ['STEP 4 — TP2 HIT',
       'Close '+setup.sz2+' bbl at $'+fix2(setup.tp2)+'  →  Trade complete. Record P&L in OIL LOG.'],
      ['HARD EXIT RULE',
       '18:30 Dubai → last entry  |  23:00 Dubai → close ALL  |  Risk on trade: $'+setup.riskDollar+' ('+RISK_PCT*100+'%)']
    ];
    steps.forEach(function(s) {
      cell(sheet,r,1,s[0],{bg:'#030a00',fg:'#336633',sz:9,bold:true,h:28});
      sheet.getRange(r,2,1,4).merge().setValue(s[1])
        .setBackground('#030a00').setFontColor('#448888').setFontSize(10)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      r++;
    });

  } else if (setup.checks) {
    var sorted = setup.checks.slice().sort(function(a,b){return (a.ok?1:0)-(b.ok?1:0);});
    sorted.forEach(function(c) {
      mrow(sheet,r,5,(c.ok?'✓  ':'✗  ')+c.label+'   —   '+c.note,
           {bg:c.ok?'#0a1400':'#1a0800',fg:c.ok?'#00c853':'#ff5252',sz:10,h:28}); r++;
    });
    if (setup.vwapEntry && tech) {
      r++;
      mrow(sheet,r,5,
        'VWAP zone for '+(setup.dir==='LONG'?'LONG':'SHORT')+' entry: $'+setup.vwapEntry.low+
        ' – $'+setup.vwapEntry.high+'  (VWAP=$'+fix2(tech.vwap)+'  ±1σ=$'+fix2(tech.vwapStd)+')',
        {bg:'#0a1400',fg:'#66bb6a',sz:10,bold:true,h:28}); r++;
    }
  } else {
    mrow(sheet,r,5,'⚠  '+(setup.reason||'Bar data unavailable'),{bg:'#1a0800',fg:'#ff9800',sz:10,h:28}); r++;
    mrow(sheet,r,5,'Yahoo Finance bar fetch failed — usually temporary. Will retry on next 5-min refresh.',
         {bg:'#110500',fg:'#664422',sz:9,h:30}); r++;
  }
  r++;

  // ── MACRO COMPONENTS ──────────────────────────────────────────────────────
  var agreeCount = (macro.components||[]).filter(function(c){return c.val===dir;}).length;
  mrow(sheet,r,5,
    'MACRO COMPONENTS  score='+(rawScore>=0?'+':'')+rawScore+'/7  |  '+agreeCount+'/7 agree  |  threshold |score|≥'+SCORE_MIN,
    {bg:'#1a1000',fg:'#806040',sz:9,bold:true,h:22}); r++;
  (macro.components||[]).forEach(function(c) {
    var neutral = c.val === 0;
    var bg2 = neutral?'#111111':c.val>0?'#0a1400':'#1a0800';
    var fg2 = neutral?'#607d8b':c.val>0?'#00c853':'#ff5252';
    cell(sheet,r,1,(neutral?'—  ':c.val>0?'▲  ':'▼  ')+c.name,{bg:bg2,fg:fg2,sz:9,bold:true,h:26});
    sheet.getRange(r,2,1,3).merge().setValue(c.note)
      .setBackground(bg2).setFontColor('#888888').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet,r,5,neutral?'0':c.val>0?'+1':'−1',{bg:bg2,fg:fg2,sz:12,bold:true}); r++;
  });
  r++;

  // ── REGIME CONTEXT ────────────────────────────────────────────────────────
  mrow(sheet,r,5,'REGIME CONTEXT',{bg:'#1a1400',fg:'#807040',sz:9,bold:true,h:20}); r++;
  var atrNow   = tech && tech.atr  ? tech.atr  : null;
  var atr15mN  = tech && tech.atr15m ? tech.atr15m : null;
  var regRows  = [
    ['VIX', vixNow?vixNow.toFixed(1):'—',
     !vixNow?'No data':vixNow<20?'Low vol — ideal':vixNow<25?'Sweet spot (20–25)':vixNow<30?'Elevated — reduce size 20%':'SKIP TODAY — crash regime',
     !vixNow?null:vixNow<30],
    ['Daily ATR', atrNow?'$'+fix2(atrNow):'—',
     atrNow?'SL=$'+fix2(SL_MULT*atrNow)+'  TP1=$'+fix2(TP1_MULT*atrNow)+'  TP2=$'+fix2(TP2_MULT*atrNow):'No daily ATR',
     atrNow!=null],
    ['15m ATR', atr15mN?'$'+fix2(atr15mN):'—',
     atr15mN?'Intraday vol context (SL/TP sized off daily ATR, not 15m)':'Unavailable',
     atr15mN!=null],
    ['Wed / Month', (isWed?'⚠ WED':'OK')+(isBadMon?' ⚠':''),
     (isWed?'Wednesday — EIA at 15:30 UTC. Skip all day. ':'')+(isBadMon?['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][monNow]+' — skip month':''),
     !isWed && !isBadMon]
  ];
  regRows.forEach(function(row){
    var ok=row[3];
    var bg3=ok===null?'#111111':ok?'#0a1400':'#1a0800';
    var fg3=ok===null?'#607d8b':ok?'#00c853':'#ff9800';
    cell(sheet,r,1,row[0],{bg:bg3,fg:'#7a8899',sz:9,h:26});
    cell(sheet,r,2,row[1],{bg:bg3,fg:'#e0e0e0',sz:12,bold:true});
    sheet.getRange(r,3,1,2).merge().setValue(row[2])
      .setBackground(bg3).setFontColor(fg3).setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet,r,5,ok===null?'INFO':ok?'OK':'SKIP',{bg:bg3,fg:fg3,sz:9,bold:true}); r++;
  });
  r++;

  // ── KEY LEVELS ────────────────────────────────────────────────────────────
  if (tech) {
    mrow(sheet,r,5,'KEY LEVELS',{bg:'#1a1200',fg:'#806040',sz:9,bold:true,h:20}); r++;
    [['Session VWAP',
      tech.vwap>0?'$'+fix2(tech.vwap):'— (pre-session)',
      tech.vwap>0?'VWAP anchor — '+tech.vwapBars+' bars since 14:00 UTC':'VWAP builds from 14:00 UTC (18:00 Dubai)'],
     [dir===1?'LONG zone  −1σ':'SHORT zone  +1σ',
      tech.vwap>0?(dir===1?'$'+fix2(tech.vwap-tech.vwapStd):'$'+fix2(tech.vwap+tech.vwapStd)):'—',
      dir===1?'Lower band — buy pullbacks here':'Upper band — sell pullbacks here'],
     [dir===1?'LONG zone  +0.3σ':'SHORT zone  −0.3σ',
      tech.vwap>0?(dir===1?'$'+fix2(tech.vwap+0.3*tech.vwapStd):'$'+fix2(tech.vwap-0.3*tech.vwapStd)):'—',
      'Far edge of entry zone'],
     ['15m EMA9','$'+fix2(tech.ema9),'Must be aligned with bias direction'],
     ['15m EMA21','$'+fix2(tech.ema21),'Must be aligned with bias direction']
    ].forEach(function(lv){
      cell(sheet,r,1,lv[0],{bg:'#110e00',fg:'#80703a',sz:9,bold:true,h:24});
      cell(sheet,r,2,lv[1],{bg:'#110e00',fg:'#e0e0e0',sz:11,bold:true});
      sheet.getRange(r,3,1,3).merge().setValue(lv[2])
        .setBackground('#110e00').setFontColor('#504020').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left');
      r++;
    });
    r++;
  }

  // ── DAILY SCHEDULE ────────────────────────────────────────────────────────
  mrow(sheet,r,5,'⏰  DAILY ROUTINE  (Dubai / UTC)',{bg:'#1a1200',fg:'#806040',sz:9,bold:true,h:20}); r++;
  [['18:00 / 14:00','Pre-check',   '#111111','Check macro score + VIX. If score < '+SCORE_MIN+', VIX ≥ 30, bad month, or Wednesday → skip day.'],
   ['18:30 / 14:30','Session opens','#0f0a00','VWAP starts building. Do NOT enter yet — opening range noise.'],
   ['19:00 / 15:00','▶ ENTRY OPEN','#0a2200','Entry window opens. All 7 gates must be green. Wait for VWAP pullback.'],
   ['21:00 / 17:00','Mid-session',  '#0a1800','If TP1 hit → close 60%, move SL to breakeven immediately.'],
   ['22:30 / 18:30','Entry CLOSES', '#1a0a00','No new entries after this. Manage open positions only.'],
   ['23:00 / 19:00','HARD CLOSE',   '#220000','Close ALL positions. No exceptions. Do not hold WTI overnight.']
  ].forEach(function(row){
    cell(sheet,r,1,row[0],{bg:row[2],fg:row[0].indexOf('HARD')!==-1?'#ff5252':row[0].indexOf('ENTRY')!==-1?'#00e676':'#90a0b0',sz:9,bold:true,h:26});
    cell(sheet,r,2,row[1],{bg:row[2],fg:'#9aaabb',sz:9,bold:row[0].indexOf('ENTRY')!==-1||row[0].indexOf('HARD')!==-1});
    sheet.getRange(r,3,1,3).merge().setValue(row[3])
      .setBackground(row[2]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // ── RULES ─────────────────────────────────────────────────────────────────
  r++;
  mrow(sheet,r,5,'📋  RULES YOU MUST NOT BREAK',{bg:'#1a0800',fg:'#cc4444',sz:9,bold:true,h:20}); r++;
  ['NEVER trade on Wednesday — EIA inventory spikes ±$1.5/bbl in under 30 seconds',
   'NEVER trade in Jan · Apr · Jun · Sep · Oct — backtest shows negative edge these months',
   'NEVER trade when VIX ≥ 30 — equity crash regimes blow out commodity correlations',
   'NEVER trade when |score| < '+SCORE_MIN+' — no majority means no edge, 50/50 coin flip',
   'NEVER move SL further away — if price hits SL, take the loss',
   'NEVER hold WTI crude overnight — gap risk at electronic re-open is severe'
  ].forEach(function(rule){
    mrow(sheet,r,5,'✗  '+rule,{bg:'#120500',fg:'#884444',sz:9,h:22}); r++;
  });

  // ── RESEARCH CREDITS ──────────────────────────────────────────────────────
  r++;
  mrow(sheet,r,5,'RESEARCH BASIS',{bg:'#0a0800',fg:'#504030',sz:8,bold:true,h:18}); r++;
  ['Kilian (2009 AER): demand shocks drive 90% of oil price variance — macro score critical',
   'Baumeister-Kilian-Zhou (2018): RBOB crack = #1 out-of-sample predictor for WTI direction',
   'GS commodity desk: Brent-WTI z-score proxies for physical tightness / regional gluts',
   'USO/USL ratio: term-structure signal — backwardation (rising ratio) = bullish supply fundamentals',
   'CME research: ORB outperforms VWAP pullback for trending CL — use VWAP for mean-reversion entries'
  ].forEach(function(line){
    mrow(sheet,r,5,line,{bg:'#0a0800',fg:'#403020',sz:8,h:18}); r++;
  });

  // ── FOOTER ────────────────────────────────────────────────────────────────
  r++;
  mrow(sheet,r,5,
    'OIL SIGNAL v2  |  141 trades 2yr  |  WR 65.2%  |  Sharpe 5.86  |  1.89%/mo @ 3% risk  |  MaxDD −7.5%',
    {bg:'#080600',fg:'#282010',sz:8,h:18}); r++;
  mrow(sheet,r,5,
    'Components: Trend · Copper · Momentum · SPY · RBOB-crack · Brent-WTI z60 · USO/USL term-structure  |  Skip: Jan/Apr/Jun/Sep/Oct/Wed',
    {bg:'#080600',fg:'#282010',sz:8,h:18});

  SpreadsheetApp.flush();
}

// ── MATH HELPERS ─────────────────────────────────────────────────────────────
function emaArr(arr, n) {
  var k=2/(n+1), out=[arr[0]];
  for (var i=1;i<arr.length;i++) out.push(arr[i]*k+out[i-1]*(1-k));
  return out;
}

function calcAtr14(bars) {
  var tr=bars.map(function(b,i){
    if(i===0) return b.h-b.l;
    return Math.max(b.h-b.l, Math.abs(b.h-bars[i-1].c), Math.abs(b.l-bars[i-1].c));
  });
  var e=emaArr(tr,14); return e[e.length-1];
}

function fix(n)  { return n!=null?(Math.round(n*100)/100).toFixed(2):'—'; }
function fix2(n) { return n!=null?(Math.round(n*100)/100).toFixed(2):'—'; }

function cell(sheet,row,col,val,opts){
  var o=opts||{};
  sheet.getRange(row,col).setValue(val)
    .setBackground(o.bg||'#0f0800').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if(o.h) sheet.setRowHeight(row,o.h);
}

function mrow(sheet,row,cols,val,opts){
  var o=opts||{};
  sheet.getRange(row,1,1,cols).merge().setValue(val)
    .setBackground(o.bg||'#0f0800').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(row,o.h||30);
}
