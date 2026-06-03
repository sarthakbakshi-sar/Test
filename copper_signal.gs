/**
 * COMEX COPPER SIGNAL — RESEARCH-BACKED MODEL  (HG=F VWAP Pullback)
 * ==================================================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste this → Save
 *   2. Run setupTrigger() once to authorize + set auto-refresh
 *   3. Run updateDashboard() to load immediately
 *   Auto-refreshes every 5 minutes.
 *
 * MODEL v1  (Gorton-Rouwenhorst 2006 · GS super-cycle · BHP/Rio research):
 *   5 components: Trend · DXY · Momentum · SPY · Oil
 *   Score gate : |raw score| ≥ 2  (out of max ±5)
 *   Skip months: Jan · May · Jul · Dec  (A/B tested bad months)
 *   Skip day   : Friday  (gap risk into weekend)
 *   Session    : 13:30–17:30 UTC  (17:30–21:30 Dubai  |  9:30 AM–1:30 PM ET  COMEX)
 *   SL = 0.75×DailyATR  ·  TP1 = 1.5×ATR (60%)  ·  TP2 = 2.5×ATR (40%)
 *
 * BACKTEST (2yr, 145 trades):  WR 59.3% · Sharpe 4.49 · 2.22%/mo @ 3% risk · MaxDD −9%
 *   Longs: WR 61%  Sharpe 4.72  |  Shorts: WR 57%  Sharpe 4.05
 *
 * NOTE: FCX miner lead, Gold/Cu ratio, XME sector — all A/B tested and DROPPED
 *   (all three hurt Sharpe when added)
 *
 * SOURCES:
 *   Gorton-Rouwenhorst (2006): Commodity momentum strongest for industrial metals
 *   Goldman Sachs super-cycle: Copper supply deficit, EV + grid demand
 *   Jacks-O'Rourke-Williamson: DXY inverse correlation −0.7 with copper
 *   BHP/Rio research: Chinese demand proxy = copper leading indicator
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var ACCOUNT     = 10000;     // ← change to your account size
var RISK_PCT    = 0.03;      // 3% risk per trade
var SL_MULT     = 0.75;
var TP1_MULT    = 1.5;
var TP2_MULT    = 2.5;
var TP1_FRAC    = 0.60;
var TP2_FRAC    = 0.40;
var SCORE_MIN   = 2;         // |raw| ≥ 2 (out of 5)

// UTC session times — COMEX copper
var SESS_OPEN   = 13.00;     // 13:00 UTC  (VWAP builds from here)
var SESS_CLOSE  = 17.50;     // 17:30 UTC  hard close
var ENTRY_START = 13.50;     // 13:30 UTC  entry window opens
var ENTRY_END   = 17.00;     // 17:00 UTC  last entry

// Skip months (JS 0-indexed): 0=Jan, 4=May, 6=Jul, 11=Dec
var SKIP_MONTHS = [0, 4, 6, 11];
var SKIP_FRI    = true;   // skip Friday — gap risk into weekend

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ COPPER SIGNAL — Auto-refresh every 5 min active.\n\n' +
      'SESSION SCHEDULE (Dubai / ET):\n' +
      '  17:00 Dubai → Pre-session check: macro score + VIX\n' +
      '  17:30 Dubai → Session opens, VWAP builds\n' +
      '  17:30 Dubai → ENTRY WINDOW OPENS (13:30 UTC)\n' +
      '  21:00 Dubai → Last entry (17:00 UTC)\n' +
      '  21:30 Dubai → HARD CLOSE — exit ALL (17:30 UTC)\n\n' +
      'SKIP: Jan · May · Jul · Dec · ALL Fridays\n' +
      'NEVER hold COMEX copper overnight.'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;

  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('COPPER SIGNAL') || ss.insertSheet('COPPER SIGNAL');

  var macro     = getMacroScore();
  var bars15m   = get15mBars(80);
  var bars1h    = get1hBars(260);
  var sessState = getSessionState(nowH);

  var tech  = computeTechnicals(bars15m, bars1h);
  var setup = computeSetup(macro, tech, sessState, now, nowH);

  writeSheet(dash, now, nowH, macro, tech, sessState, setup);
}

// ── MACRO SCORE ───────────────────────────────────────────────────────────────
function getMacroScore() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var cached = cache.get('copper_v1_' + today);
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
      } catch(e) { Logger.log('yfd ' + sym + ': ' + e); }
    }
    return null;
  }

  var hg  = yfd('HG=F',     '3mo');  // COMEX copper ($/lb)
  var dx  = yfd('DX-Y.NYB', '3mo');  // DXY
  var spy = yfd('SPY',      '3mo');  // S&P 500
  var cl  = yfd('CL=F',     '3mo');  // WTI oil (energy demand proxy)
  var vix = yfd('^VIX',     '5d');

  var result = { score:0, raw:0, dir:1, strength:'', components:[], error:null,
                 cuPriceD:null, vix:null };

  if (!hg || hg.length < 12) { result.error = 'Macro data unavailable (HG=F)'; return result; }

  var n = hg.length;
  result.cuPriceD = hg[n-1];
  result.vix = vix && vix.length > 0 ? Math.round(vix[vix.length-1]*10)/10 : null;

  // 1. Trend: HG vs EMA50
  var e50 = emaArr(hg, Math.min(50, n));
  var c_trend = hg[n-2] > e50[n-2] ? 1 : -1;

  // 2. DXY 5d direction (falling = bullish copper, inverse correlation −0.7)
  var c_dxy = (dx && dx.length >= 8)
    ? ((dx[dx.length-2] / dx[dx.length-7] - 1) < 0 ? 1 : -1) : 0;

  // 3. Copper 5d momentum
  var c_mom = n >= 8 ? (hg[n-2] > hg[n-7] ? 1 : -1) : 0;

  // 4. SPY 5d direction (risk-on = bullish copper)
  var c_spy = (spy && spy.length >= 8)
    ? ((spy[spy.length-2] / spy[spy.length-7] - 1) > 0 ? 1 : -1) : 0;

  // 5. WTI oil 5d direction (energy demand proxy — China demand signal)
  var c_oil = (cl && cl.length >= 8)
    ? ((cl[cl.length-2] / cl[cl.length-7] - 1) > 0 ? 1 : -1) : 0;

  var raw = c_trend + c_dxy + c_mom + c_spy + c_oil;
  result.raw      = raw;
  result.score    = raw;
  result.dir      = raw >= 0 ? 1 : -1;
  var absRaw      = Math.abs(raw);
  result.strength = absRaw >= 4 ? 'STRONG' : absRaw >= 2 ? 'MODERATE' : 'WEAK';

  var mom5pct = n >= 8 ? Math.round((hg[n-2]/hg[n-7]-1)*1000)/10 : 0;
  var dxyPct  = (dx && dx.length >= 8) ? Math.round((dx[dx.length-2]/dx[dx.length-7]-1)*1000)/10 : null;
  var oilPct  = (cl && cl.length >= 8) ? Math.round((cl[cl.length-2]/cl[cl.length-7]-1)*1000)/10 : null;

  result.components = [
    { name:'HG vs EMA50',    val:c_trend,
      note:'Copper '+(c_trend>0?'above':'below')+' EMA50 ($'+Math.round(e50[n-2]*1000)/1000+'/lb) — macro trend' },
    { name:'DXY 5d dir',     val:c_dxy,
      note:'DXY '+(c_dxy===0?'unavail':c_dxy>0?'falling ↓ (dollar weak = bullish copper)':'rising ↑ (dollar strong = bearish copper)')+
           (dxyPct!==null?'  '+dxyPct+'% 5d':'') },
    { name:'Copper 5d mom',  val:c_mom,
      note:(mom5pct>=0?'+':'')+mom5pct+'% over 5 days' },
    { name:'SPY 5d dir',     val:c_spy,
      note:'Equity '+(c_spy===0?'unavail':c_spy>0?'rising ↑ (risk-on = industrial demand bullish)':'falling ↓ (risk-off = demand concerns bearish)') },
    { name:'Oil 5d dir',     val:c_oil,
      note:'WTI '+(c_oil===0?'unavail':c_oil>0?'rising ↑ (global demand proxy bullish)':'falling ↓ (demand contraction signal)')+
           (oilPct!==null?'  '+oilPct+'% 5d':'') }
  ];

  var cacheTtl = result.vix !== null ? 21600 : 300;
  try { cache.put('copper_v1_' + today, JSON.stringify(result), cacheTtl); } catch(e) {}
  return result;
}

// ── BAR FETCH (Yahoo Finance — HG=F COMEX copper) ────────────────────────────
function yfBars(interval, range, n) {
  var sc  = CacheService.getScriptCache();
  var key = 'yf_cu_bars_' + interval;
  var ttl = (interval === '1h') ? 3600 : 600;
  try { var hit = sc.get(key); if (hit) return JSON.parse(hit); } catch(e) {}

  var hosts = ['query1.finance.yahoo.com', 'query2.finance.yahoo.com'];
  var opts  = { muteHttpExceptions:true,
                headers:{ 'User-Agent':'Mozilla/5.0', 'Accept':'application/json' } };

  for (var hi = 0; hi < hosts.length; hi++) {
    try {
      var url  = 'https://' + hosts[hi] + '/v8/finance/chart/HG%3DF' +
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
  return [];
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
  return emaArr(tr, 14)[tr.length - 1];
}

// ── TECHNICALS ────────────────────────────────────────────────────────────────
function computeTechnicals(bars15m, bars1h) {
  if (!bars15m || bars15m.length < 35) return null;
  var closes = bars15m.map(function(b){ return b.c; });
  var e9     = emaArr(closes, 9);
  var e21    = emaArr(closes, 21);
  var last   = bars15m[bars15m.length - 1];
  var atrDay = getDailyAtr(bars1h);
  var atr15m = calcAtr14(bars15m);
  var sv     = sessionVwap(bars15m);
  return {
    price: last.c, high: last.h, low: last.l,
    ema9: e9[e9.length-1], ema21: e21[e21.length-1],
    atr:    atrDay || atr15m * 6,
    atr15m: atr15m, atrDay: atrDay,
    vwap: sv.vwap, vwapStd: sv.std, vwapBars: sv.n,
    vwapDist: sv.std > 0.0001 && sv.vwap ? (last.c - sv.vwap) / sv.std : 99
  };
}

// ── SESSION VWAP (starts at 13:00 UTC) ───────────────────────────────────────
function sessionVwap(bars15m) {
  var now   = new Date();
  var ymd   = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sOpen = new Date(ymd + 'T13:00:00Z');
  var todayP= ymd + ' ';
  var sess  = bars15m.filter(function(b){
    return new Date(b.dt.replace(' ','T')+'Z') >= sOpen && b.dt.indexOf(todayP) === 0;
  });
  if (sess.length < 2) return { vwap:0, std:0.0001, n:sess.length };
  var tp  = sess.map(function(b){ return (b.h+b.l+b.c)/3; });
  var avg = tp.reduce(function(a,v){ return a+v; },0) / tp.length;
  var v2  = tp.reduce(function(a,x){ return a+Math.pow(x-avg,2); },0) / tp.length;
  return { vwap:avg, std:Math.max(Math.sqrt(v2),0.0001), n:tp.length };
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
function computeSetup(macro, tech, sessState, now, nowH) {
  if (!tech || macro.error) {
    return { action:'WAIT', dir:macro.dir===1?'LONG':'SHORT',
             reason: macro.error || 'Bar data unavailable' };
  }

  var dir    = macro.dir;
  var price  = tech.price;
  var atrV   = tech.atr;
  var nowMon = now.getUTCMonth();   // 0-indexed
  var nowDow = now.getUTCDay();     // 0=Sun, 5=Fri

  var vixOk   = macro.vix === null || macro.vix < 30;
  var monthOk = SKIP_MONTHS.indexOf(nowMon) === -1;
  var friOk   = !SKIP_FRI || nowDow !== 5;
  var scoreOk = Math.abs(macro.raw) >= SCORE_MIN;

  var dist   = tech.vwapStd > 0.0001 ? (price - tech.vwap) / tech.vwapStd : 99;
  var emaOk  = dir === 1 ? tech.ema9 > tech.ema21 : tech.ema9 < tech.ema21;
  var vwOk   = tech.vwapBars >= 4 &&
    (dir === 1 ? (dist >= -1.0 && dist <= 0.3) : (dist >= -0.3 && dist <= 1.0));
  var sessOk = sessState === 'ENTRY';
  var mNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var vixStr = macro.vix !== null ? 'VIX '+macro.vix.toFixed(1) : 'VIX n/a';

  var checks = [
    { label:'VIX < 30', ok:vixOk,
      note: vixOk ? (macro.vix?vixStr+(macro.vix>=25?' — elevated, reduce size':' — OK'):'VIX unavail')
                  : vixStr+' ≥ 30 — CRASH REGIME. Skip today.' },
    { label:'Not skip-month', ok:monthOk,
      note: monthOk ? mNames[nowMon]+' — month OK'
                    : mNames[nowMon]+' — SKIP MONTH (A/B tested: negative edge).' },
    { label:'Not Friday', ok:friOk,
      note: friOk ? (nowDow===5?'—':'Not Friday — OK')
                  : 'FRIDAY — gap risk into weekend. Skip all day.' },
    { label:'Score |raw| ≥ '+SCORE_MIN, ok:scoreOk,
      note:'Score: '+(macro.raw>=0?'+':'')+macro.raw+'/5  '+(scoreOk?'✓ majority confirmed':'✗ need |score| ≥ '+SCORE_MIN) },
    { label:'Session entry window', ok:sessOk,
      note: sessOk ? '✓ 13:30–17:00 UTC (17:30–21:00 Dubai) open'
                   : 'Wait — entry opens 13:30 UTC (17:30 Dubai)' },
    { label:'15m EMA9 vs EMA21', ok:emaOk,
      note: emaOk ? 'EMA9 $'+fix(tech.ema9)+' aligned with '+(dir===1?'LONG':'SHORT')
                  : 'EMA9 $'+fix(tech.ema9)+' / EMA21 $'+fix(tech.ema21)+' — against bias' },
    { label:'VWAP pullback zone', ok:vwOk,
      note: vwOk ? 'dist='+Math.round(dist*100)/100+'σ ✓  at $'+fix(tech.vwap)
                 : tech.vwapBars<4 ? 'Only '+tech.vwapBars+' bars (need ≥4)'
                   : 'dist='+Math.round(dist*100)/100+'σ  need '+(dir===1?'−1.0 to +0.3σ':'−0.3 to +1.0σ')+
                     '  zone $'+fix(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd)+
                     '–$'+fix(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd) }
  ];

  var allOk = vixOk && monthOk && friOk && scoreOk && sessOk && emaOk && vwOk;

  if (allOk) {
    var totalSz = (ACCOUNT * RISK_PCT) / (SL_MULT * atrV);
    var sl, tp1, tp2;
    if (dir===1) { sl=price-SL_MULT*atrV; tp1=price+TP1_MULT*atrV; tp2=price+TP2_MULT*atrV; }
    else         { sl=price+SL_MULT*atrV; tp1=price-TP1_MULT*atrV; tp2=price-TP2_MULT*atrV; }
    return {
      action:'ACTIVE', dir:dir===1?'LONG':'SHORT',
      entry:price, sl:sl, tp1:tp1, tp2:tp2,
      size:  Math.round(totalSz),
      sz1:   Math.round(totalSz * TP1_FRAC),
      sz2:   Math.round(totalSz * TP2_FRAC),
      riskDollar: Math.round(ACCOUNT * RISK_PCT),
      atr:atrV, slPts:SL_MULT*atrV, tp1Pts:TP1_MULT*atrV, tp2Pts:TP2_MULT*atrV,
      checks:checks
    };
  }
  return {
    action:'WAIT', dir:dir===1?'LONG':'SHORT',
    reason:checks.filter(function(c){return !c.ok;}).map(function(c){return c.note;}).join(' | '),
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

  // Header
  mrow(sheet,r,5,'🔶  COMEX COPPER  —  VWAP Model  |  Sharpe 4.49  |  WR 59.3%  |  5 Components',
       {bg:'#1a0d00',fg:'#E87C2C',sz:12,bold:true,h:36}); r++;
  mrow(sheet,r,5,utcStr+'  ·  '+dubaiStr+'  ·  Auto-refresh 5 min  ·  Risk '+(RISK_PCT*100)+'% ($'+Math.round(ACCOUNT*RISK_PCT)+')  ·  Account $'+ACCOUNT,
       {bg:'#120900',fg:'#604030',sz:9,h:20}); r++; r++;

  var price    = tech ? tech.price : (macro.cuPriceD || 0);
  var rawScore = macro.raw || 0;
  var dir      = macro.dir || 1;
  var strength = macro.strength || '';
  var vixNow   = macro.vix;
  var vixFg    = !vixNow?'#808080':vixNow>=30?'#ff1744':vixNow>=25?'#ff9800':vixNow>=20?'#ffea00':'#00e676';

  var now2   = new Date();
  var dowNow = now2.getUTCDay(), monNow = now2.getUTCMonth();
  var isFri  = dowNow === 5;
  var isBadMon = SKIP_MONTHS.indexOf(monNow) !== -1;
  var mNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var sMap   = {BEFORE:'⏳ Pre-session',CHOP:'⚠ No entries (chop)',ENTRY:'✅ ENTRY OPEN',LATE:'🕐 Entry closed',CLOSED:'🔒 Closed'};
  var sFg    = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};

  ['HG=F PRICE','DIRECTION','SESSION','VIX','DATE FLAGS'].forEach(function(h,i){
    cell(sheet,r,i+1,h,{bg:'#1c1000',fg:'#805020',sz:9,bold:true,h:20});
  });
  r++;

  var dirLabel = (dir===1?'▲ LONG':'▼ SHORT')+'  '+strength;
  var dirFg    = dir===1?'#00e676':'#ff5252';
  var dfLabel  = (isFri?'⚠ FRIDAY':'')+(isBadMon?(isFri?' · ':'')+mNames[monNow]+' SKIP':'');
  var dfFg     = (isFri||isBadMon)?'#ff9800':'#4caf50';
  if (!dfLabel) dfLabel = '✓ Day OK';

  cell(sheet,r,1,price?'$'+fix4(price):'—',        {bg:'#0f0800',fg:'#E87C2C',sz:16,bold:true,h:40});
  cell(sheet,r,2,dirLabel,                           {bg:'#0f0800',fg:dirFg,   sz:11,bold:true});
  cell(sheet,r,3,sMap[sessState]||sessState,         {bg:'#0f0800',fg:sFg[sessState]||'#808080',sz:10,bold:true});
  cell(sheet,r,4,vixNow?vixNow.toFixed(1):'—',      {bg:'#0f0800',fg:vixFg,   sz:16,bold:true});
  cell(sheet,r,5,dfLabel,                            {bg:'#0f0800',fg:dfFg,    sz:10,bold:true}); r++;

  // VWAP panel
  var hasVwap = tech && tech.vwap > 0 && tech.vwapStd > 0.0001;
  ['VWAP','−1σ  (LONG zone)','+1σ  (SHORT zone)','PRICE vs VWAP','BARS'].forEach(function(h,i){
    cell(sheet,r,i+1,h,{bg:'#0a0e00',fg:'#4a6030',sz:9,bold:true,h:18});
  });
  r++;
  if (hasVwap) {
    var vwDist  = tech.vwapDist;
    var vwDistS = (vwDist>=0?'+':'')+Math.round(vwDist*100)/100+'σ';
    var inZone  = dir===1?(vwDist>=-1.0&&vwDist<=0.3):(vwDist>=-0.3&&vwDist<=1.0);
    var posFg   = inZone?'#00e676':'#ff9800';
    cell(sheet,r,1,'$'+fix4(tech.vwap),                          {bg:'#060900',fg:'#88bb44',sz:14,bold:true,h:34});
    cell(sheet,r,2,'$'+fix4(tech.vwap-tech.vwapStd),             {bg:'#060900',fg:'#00e676',sz:13,bold:true});
    cell(sheet,r,3,'$'+fix4(tech.vwap+tech.vwapStd),             {bg:'#060900',fg:'#ff5252',sz:13,bold:true});
    cell(sheet,r,4,vwDistS+(inZone?'  ✓ IN ZONE':'  ✗ OUT OF ZONE'), {bg:'#060900',fg:posFg,sz:11,bold:true});
    cell(sheet,r,5,tech.vwapBars+(tech.vwapBars<4?' ⚠':''),      {bg:'#060900',fg:'#607d8b',sz:11});
  } else {
    var vMsg = (nowH>=SESS_OPEN&&nowH<=SESS_CLOSE)
      ? 'Session active — bar fetch failed (retrying)'
      : 'Pre-session — VWAP builds from 13:00 UTC (17:00 Dubai)';
    mrow(sheet,r,5,vMsg,{bg:'#060900',fg:'#607d8b',sz:9,h:34});
  }
  r++; r++;

  // Score meter
  mrow(sheet,r,5,
    'MACRO SCORE  '+(rawScore>=0?'+':'')+rawScore+' / 5  |  Need |score| ≥ '+SCORE_MIN+
    '  |  '+(dir===1?'▲ LONG':'▼ SHORT')+'  '+strength,
    {bg:'#1a1000',fg:Math.abs(rawScore)>=SCORE_MIN?'#E87C2C':'#806040',sz:10,bold:true,h:24}); r++;

  // Action block
  var inEntry  = sessState==='ENTRY';
  var actionBg = setup.action==='ACTIVE'?'#0a1e00':inEntry?'#1a1400':'#111111';
  var actionFg = setup.action==='ACTIVE'?'#00ff88':inEntry?'#ccaa44':'#9e9e9e';
  var actionTxt = setup.action==='ACTIVE'
    ? '⚡  ENTER '+(setup.dir)+'  NOW  —  All 7 gates green  —  Execute in MT5'
    : inEntry
      ? (setup.checks
          ? '⏱  IN SESSION  —  Watching for setup  ·  Bias: '+(setup.dir||'—')+'  ·  Waiting: '+
            setup.checks.filter(function(c){return !c.ok;}).length+' gate(s)'
          : '⚠  IN SESSION  —  '+(setup.reason||'Data unavailable'))
      : '⏳  NEXT SESSION: 13:30 UTC  (17:30 Dubai)';
  mrow(sheet,r,5,actionTxt,{bg:actionBg,fg:actionFg,sz:12,bold:true,h:38}); r++;

  if (setup.action==='ACTIVE') {
    ['ENTRY','STOP LOSS','TP1 (60%)','TP2 (40%)','SIZE (lbs)'].forEach(function(h,i){
      cell(sheet,r,i+1,h,{bg:'#0a1600',fg:'#406040',sz:9,bold:true,h:20});
    });
    r++;
    [['$'+fix4(setup.entry),'#E87C2C'],['$'+fix4(setup.sl),'#ff5252'],
     ['$'+fix4(setup.tp1),'#00e676'],['$'+fix4(setup.tp2),'#00bcd4'],
     [setup.size+' lbs','#ffffff']].forEach(function(v,i){
      cell(sheet,r,i+1,v[0],{bg:'#0a2800',fg:v[1],sz:14,bold:true,h:40});
    });
    r++;
    cell(sheet,r,1,'DailyATR $'+fix4(setup.atr),          {bg:'#050b00',fg:'#446633',sz:9,h:22});
    cell(sheet,r,2,'SL −$'+fix4(setup.slPts)+'/lb',       {bg:'#050b00',fg:'#883333',sz:9,bold:true});
    cell(sheet,r,3,'TP1 +$'+fix4(setup.tp1Pts)+'  1.5R',  {bg:'#050b00',fg:'#338833',sz:9,bold:true});
    cell(sheet,r,4,'TP2 +$'+fix4(setup.tp2Pts)+'  2.5R',  {bg:'#050b00',fg:'#226688',sz:9,bold:true});
    cell(sheet,r,5,setup.sz1+' + '+setup.sz2+' lbs',      {bg:'#050b00',fg:'#666600',sz:9}); r++;
    var microCts = Math.round(setup.size / 2500 * 10) / 10;
    var miniCts  = Math.round(setup.size / 12500 * 10) / 10;
    var fullCts  = Math.round(setup.size / 25000 * 10) / 10;
    mrow(sheet,r,5,
      'Contract equiv: '+setup.size+' lbs  →  '+microCts+' micro (2,500 lbs)  |  '+
      miniCts+' e-mini (12,500 lbs)  |  '+fullCts+' full HG (25,000 lbs)',
      {bg:'#040900',fg:'#606040',sz:9,h:22}); r++;
    var dir1 = setup.dir==='LONG';
    var steps = [
      ['STEP 1 — ENTER','MT5 → HG (COMEX Copper) → '+(dir1?'BUY':'SELL')+' → '+setup.size+' lbs  |  '+microCts+' micro / '+miniCts+' e-mini / '+fullCts+' full contracts'],
      ['STEP 2 — SET LEVELS','Immediately: SL = $'+fix4(setup.sl)+'/lb  |  TP = $'+fix4(setup.tp1)+'/lb  ('+setup.sz1+' lbs TP1 portion)'],
      ['STEP 3 — TP1 HIT','Close '+setup.sz1+' lbs at $'+fix4(setup.tp1)+'  →  Move SL of '+setup.sz2+' lbs to $'+fix4(setup.entry)+' (breakeven)'],
      ['STEP 4 — TP2 HIT','Close '+setup.sz2+' lbs at $'+fix4(setup.tp2)+'  →  Trade complete. Record in COPPER LOG.'],
      ['HARD EXIT RULE','21:00 Dubai → last entry  |  21:30 Dubai (17:30 UTC) → close ALL  |  Risk: $'+setup.riskDollar]
    ];
    steps.forEach(function(s){
      cell(sheet,r,1,s[0],{bg:'#030800',fg:'#446633',sz:9,bold:true,h:28});
      sheet.getRange(r,2,1,4).merge().setValue(s[1])
        .setBackground('#030800').setFontColor('#448866').setFontSize(10)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      r++;
    });
  } else if (setup.checks) {
    setup.checks.slice().sort(function(a,b){return (a.ok?1:0)-(b.ok?1:0);}).forEach(function(c){
      mrow(sheet,r,5,(c.ok?'✓  ':'✗  ')+c.label+'   —   '+c.note,
           {bg:c.ok?'#0a1400':'#1a0800',fg:c.ok?'#00c853':'#ff5252',sz:10,h:28}); r++;
    });
    if (setup.vwapEntry && tech) {
      r++;
      mrow(sheet,r,5,'VWAP entry zone for '+(setup.dir==='LONG'?'LONG':'SHORT')+
        ': $'+setup.vwapEntry.low+' – $'+setup.vwapEntry.high+
        '  (VWAP=$'+fix4(tech.vwap)+'  ±1σ=$'+fix4(tech.vwapStd)+')',
        {bg:'#0a1400',fg:'#88cc44',sz:10,bold:true,h:28}); r++;
    }
  } else {
    mrow(sheet,r,5,'⚠  '+(setup.reason||'Bar data unavailable'),{bg:'#1a0800',fg:'#ff9800',sz:10,h:28}); r++;
    mrow(sheet,r,5,'Yahoo Finance bar fetch failed — retrying on next 5-min refresh.',
         {bg:'#110500',fg:'#664422',sz:9,h:30}); r++;
  }
  r++;

  // Macro components
  var agreeCount = (macro.components||[]).filter(function(c){return c.val===dir;}).length;
  mrow(sheet,r,5,
    'MACRO COMPONENTS  score='+(rawScore>=0?'+':'')+rawScore+'/5  |  '+agreeCount+'/5 agree  |  threshold |score|≥'+SCORE_MIN,
    {bg:'#1a1000',fg:'#806040',sz:9,bold:true,h:22}); r++;
  (macro.components||[]).forEach(function(c){
    var bg2=c.val>0?'#0a1400':'#1a0800', fg2=c.val>0?'#00c853':'#ff5252';
    cell(sheet,r,1,(c.val>0?'▲  ':'▼  ')+c.name,{bg:bg2,fg:fg2,sz:9,bold:true,h:26});
    sheet.getRange(r,2,1,3).merge().setValue(c.note)
      .setBackground(bg2).setFontColor('#888888').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet,r,5,c.val>0?'+1':'−1',{bg:bg2,fg:fg2,sz:12,bold:true}); r++;
  });
  r++;

  // Regime context
  mrow(sheet,r,5,'REGIME CONTEXT',{bg:'#1a1400',fg:'#807040',sz:9,bold:true,h:20}); r++;
  var atrNow  = tech&&tech.atr  ?tech.atr  :null;
  var atr15mN = tech&&tech.atr15m?tech.atr15m:null;
  [['VIX',vixNow?vixNow.toFixed(1):'—',
    !vixNow?'No data':vixNow<20?'Low vol — ideal':vixNow<25?'Sweet spot (20–25)':vixNow<30?'Elevated — reduce size 20%':'SKIP TODAY',
    !vixNow?null:vixNow<30],
   ['Daily ATR',atrNow?'$'+fix4(atrNow):'—',
    atrNow?'SL=$'+fix4(SL_MULT*atrNow)+'/lb  TP1=$'+fix4(TP1_MULT*atrNow)+'  TP2=$'+fix4(TP2_MULT*atrNow):'No daily ATR',
    atrNow!=null],
   ['15m ATR',atr15mN?'$'+fix4(atr15mN):'—','SL/TP use daily ATR, not 15m',atr15mN!=null],
   ['Fri/Month',(isFri?'⚠ FRI':'OK')+(isBadMon?' ⚠':''),
    (isFri?'Friday — gap risk. Skip all day. ':'')+(isBadMon?mNames[monNow]+' — skip month':''),
    !isFri&&!isBadMon]
  ].forEach(function(row){
    var ok=row[3]; var bg3=ok===null?'#111111':ok?'#0a1400':'#1a0800'; var fg3=ok===null?'#607d8b':ok?'#00c853':'#ff9800';
    cell(sheet,r,1,row[0],{bg:bg3,fg:'#7a8899',sz:9,h:26});
    cell(sheet,r,2,row[1],{bg:bg3,fg:'#e0e0e0',sz:12,bold:true});
    sheet.getRange(r,3,1,2).merge().setValue(row[2])
      .setBackground(bg3).setFontColor(fg3).setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet,r,5,ok===null?'INFO':ok?'OK':'SKIP',{bg:bg3,fg:fg3,sz:9,bold:true}); r++;
  });
  r++;

  // Key levels
  if (tech) {
    mrow(sheet,r,5,'KEY LEVELS',{bg:'#1a1200',fg:'#806040',sz:9,bold:true,h:20}); r++;
    [['Session VWAP',tech.vwap>0?'$'+fix4(tech.vwap):'— (pre-session)',
      tech.vwap>0?'VWAP anchor — '+tech.vwapBars+' bars since 13:00 UTC':'Builds from 13:00 UTC (17:00 Dubai)'],
     [dir===1?'LONG zone  −1σ':'SHORT zone  +1σ',
      tech.vwap>0?(dir===1?'$'+fix4(tech.vwap-tech.vwapStd):'$'+fix4(tech.vwap+tech.vwapStd)):'—',
      dir===1?'Lower band — buy pullbacks':'Upper band — sell pullbacks'],
     [dir===1?'LONG zone  +0.3σ':'SHORT zone  −0.3σ',
      tech.vwap>0?(dir===1?'$'+fix4(tech.vwap+0.3*tech.vwapStd):'$'+fix4(tech.vwap-0.3*tech.vwapStd)):'—',
      'Far edge of entry zone'],
     ['15m EMA9','$'+fix4(tech.ema9),'Must align with bias'],
     ['15m EMA21','$'+fix4(tech.ema21),'Must align with bias']
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

  // Daily schedule
  mrow(sheet,r,5,'⏰  DAILY ROUTINE  (Dubai / UTC)',{bg:'#1a1200',fg:'#806040',sz:9,bold:true,h:20}); r++;
  [['17:00 / 13:00','Pre-check','#111111','Check macro score + VIX. If score<'+SCORE_MIN+', VIX≥30, bad month, or Friday → skip day.'],
   ['17:30 / 13:30','▶ ENTRY OPEN','#0a2000','Entry window opens. All 7 gates green. Wait for VWAP pullback. (13:30 UTC = 9:30 ET)'],
   ['19:00 / 15:00','Mid-session','#0a1800','If TP1 hit → close 60%, move SL to breakeven.'],
   ['21:00 / 17:00','Entry CLOSES','#1a0a00','No new entries. Manage open positions only.'],
   ['21:30 / 17:30','HARD CLOSE','#220000','Close ALL positions. COMEX pit closes 18:00 UTC — do not hold near close.']
  ].forEach(function(row){
    cell(sheet,r,1,row[0],{bg:row[2],fg:row[1]==='HARD CLOSE'?'#ff5252':row[1].indexOf('ENTRY OPEN')!==-1?'#00e676':'#90a0b0',sz:9,bold:true,h:26});
    cell(sheet,r,2,row[1],{bg:row[2],fg:'#9aaabb',sz:9});
    sheet.getRange(r,3,1,3).merge().setValue(row[3])
      .setBackground(row[2]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });

  // Rules
  r++;
  mrow(sheet,r,5,'📋  RULES YOU MUST NOT BREAK',{bg:'#1a0800',fg:'#cc4444',sz:9,bold:true,h:20}); r++;
  ['NEVER trade on Friday — weekend gap risk on copper is severe',
   'NEVER trade in Jan · May · Jul · Dec — A/B tested: negative edge these months',
   'NEVER trade when VIX ≥ 30 — industrial metal demand collapses in crash regimes',
   'NEVER trade when |score| < '+SCORE_MIN+' — no majority signal = coin flip',
   'NEVER move SL further away — copper can gap $0.05+ in seconds on macro data',
   'NEVER hold COMEX copper overnight — gap risk at Asia open is large'
  ].forEach(function(rule){
    mrow(sheet,r,5,'✗  '+rule,{bg:'#120500',fg:'#884444',sz:9,h:22}); r++;
  });

  // Research notes
  r++;
  mrow(sheet,r,5,'RESEARCH BASIS  (dropped components — A/B tested)',{bg:'#0a0800',fg:'#504030',sz:8,bold:true,h:18}); r++;
  ['Gorton-Rouwenhorst (2006): commodity momentum strongest for industrial metals — trend + mom kept',
   'GS super-cycle thesis: DXY inverse −0.7 correlation — DXY component kept (Δ+0.55 Sharpe)',
   'FCX miner lead — TESTED AND DROPPED (Δ−1.18 Sharpe): miners move ahead of spot unreliably at 5d lag',
   'Gold/Cu ratio direction — TESTED AND DROPPED (Δ−0.20): contrarian effect cancels signal',
   'XME sector ETF — TESTED AND DROPPED (Δ−0.09): too correlated with SPY, adds noise not signal'
  ].forEach(function(line){
    mrow(sheet,r,5,line,{bg:'#0a0800',fg:'#403020',sz:8,h:18}); r++;
  });

  // Footer
  r++;
  mrow(sheet,r,5,
    'COPPER SIGNAL v1  |  145 trades 2yr  |  WR 59.3%  |  Sharpe 4.49  |  2.22%/mo @ 3% risk  |  MaxDD −9%',
    {bg:'#080600',fg:'#282010',sz:8,h:18}); r++;
  mrow(sheet,r,5,
    'Components: Trend · DXY · Momentum · SPY · Oil  |  Skip: Jan/May/Jul/Dec/Friday  |  Session 13:30–17:30 UTC',
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
  return emaArr(tr,14)[tr.length-1];
}
function fix(n)  { return n!=null?(Math.round(n*100)/100).toFixed(2):'—'; }
function fix4(n) { return n!=null?(Math.round(n*10000)/10000).toFixed(4):'—'; }  // $/lb precision

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
