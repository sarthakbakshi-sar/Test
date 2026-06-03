/**
 * SIGNAL COMMAND CENTER  —  Gold · Oil · Copper
 * ================================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste → Save
 *   2. Run setupTrigger() once — authorises + creates 5-min auto-refresh
 *   3. Run updateAll() immediately to load data
 *
 * SHEETS CREATED:
 *   DASHBOARD  — live signal for all 3 instruments, full data
 *   SCORE LOG  — appends every 5 min: price + score history per instrument
 *
 * BACKTEST RESULTS (2yr):
 *   Gold  (GC=F)  235 trades · WR 48.5% · Sharpe 2.63 · ROI +2070% cumulative · MaxDD −30%
 *   Oil   (CL=F)  141 trades · WR 65.2% · Sharpe 5.86 · 1.89%/mo @ 3% · MaxDD −7.5%
 *   Copper(HG=F)  145 trades · WR 59.3% · Sharpe 4.49 · 2.22%/mo @ 3% · MaxDD −9%
 *
 * SESSIONS (Dubai / UTC):
 *   Copper  17:00 VWAP builds  |  17:30 entry opens  |  21:30 HARD CLOSE  (13:00–17:30 UTC)
 *   Gold    17:30 VWAP builds  |  18:00 entry opens  |  21:00 HARD CLOSE  (13:30–17:00 UTC)
 *   Oil     18:00 VWAP builds  |  18:30 entry opens  |  23:00 HARD CLOSE  (14:00–19:00 UTC)
 */

// ═══════════════════════════════════════════════════════════════════════
// GLOBAL CONFIG  (change ACCOUNT to match your broker)
// ═══════════════════════════════════════════════════════════════════════
var ACCOUNT  = 10000;
var RISK_PCT = 0.03;
var SL_MULT  = 0.75;
var TP1_MULT = 1.50;
var TP2_MULT = 2.50;
var TP1_FRAC = 0.60;

// ── GOLD (GC=F — $/oz)
var AU_SESS_OPEN   = 13.50;  // 17:30 Dubai
var AU_SESS_CLOSE  = 17.00;  // 21:00 Dubai  ← HARD CLOSE
var AU_ENTRY_START = 14.00;  // 18:00 Dubai  ← entry opens
var AU_ENTRY_END   = 16.50;  // 20:30 Dubai  ← last entry

// ── OIL (CL=F — $/bbl)
var CL_SESS_OPEN   = 14.00;  // 18:00 Dubai
var CL_SESS_CLOSE  = 19.00;  // 23:00 Dubai  ← HARD CLOSE
var CL_ENTRY_START = 14.50;  // 18:30 Dubai
var CL_ENTRY_END   = 18.50;  // 22:30 Dubai

// ── COPPER (HG=F — $/lb)
var HG_SESS_OPEN   = 13.00;  // 17:00 Dubai  (VWAP builds from here)
var HG_SESS_CLOSE  = 17.50;  // 21:30 Dubai  ← HARD CLOSE
var HG_ENTRY_START = 13.50;  // 17:30 Dubai
var HG_ENTRY_END   = 17.00;  // 21:00 Dubai

// Skip rules (A/B tested in backtests)
var AU_SKIP_MONTHS = [5];            // June
var CL_SKIP_MONTHS = [0,3,5,8,9];   // Jan Apr Jun Sep Oct
var CL_SKIP_WED    = true;           // EIA Wednesday
var HG_SKIP_MONTHS = [0,4,6,11];    // Jan May Jul Dec
var HG_SKIP_FRI    = true;           // gap risk

var CL_SCORE_MIN   = 2;   // |raw| ≥ 2 out of ±7
var HG_SCORE_MIN   = 2;   // |raw| ≥ 2 out of ±5
// Gold: no score minimum — any majority fires (5 binary components always odd)

// ═══════════════════════════════════════════════════════════════════════
// TRIGGER
// ═══════════════════════════════════════════════════════════════════════
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateAll') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateAll').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ SIGNAL COMMAND CENTER — 5-min refresh active.\n\n' +
      'SESSIONS (Dubai):\n' +
      '  Copper : 17:30–21:30  entry 17:30–21:00\n' +
      '  Gold   : 17:30–21:00  entry 18:00–20:30\n' +
      '  Oil    : 18:30–23:00  entry 18:30–22:30\n\n' +
      'SCORE LOG tab appends every 5 min for historical tracking.\n' +
      'Never hold any position overnight.'
    );
  } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════════════
function updateAll() {
  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;
  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('DASHBOARD') || ss.insertSheet('DASHBOARD');
  var slog = ss.getSheetByName('SCORE LOG')  || ss.insertSheet('SCORE LOG');

  var daily = fetchAllDaily();   // one call for all 12 tickers

  // ── Gold
  var auM  = getAuMacro(daily);
  var au15 = yfBars('GC%3DF', '15m', '5d',   80, 'au15');
  var au1h = yfBars('GC%3DF', '1h',  '60d', 260, 'au1h');
  var auSS = auSessState(nowH);
  var auT  = getAuTech(au15, au1h);
  var auSU = getAuSetup(auM, auT, auSS, now);

  // ── Oil
  var clM  = getClMacro(daily);
  var cl15 = yfBars('CL%3DF', '15m', '5d',   80, 'cl15');
  var cl1h = yfBars('CL%3DF', '1h',  '60d', 260, 'cl1h');
  var clSS = clSessState(now, nowH);
  var clT  = getClTech(cl15, cl1h);
  var clSU = getClSetup(clM, clT, clSS, now);

  // ── Copper
  var hgM  = getHgMacro(daily);
  var hg15 = yfBars('HG%3DF', '15m', '5d',   80, 'hg15');
  var hg1h = yfBars('HG%3DF', '1h',  '60d', 260, 'hg1h');
  var hgSS = hgSessState(nowH);
  var hgT  = getHgTech(hg15, hg1h);
  var hgSU = getHgSetup(hgM, hgT, hgSS, now);

  writeDashboard(dash, now, nowH,
    auM, auT, auSS, auSU,
    clM, clT, clSS, clSU,
    hgM, hgT, hgSS, hgSU);

  appendScoreLog(slog, now, auM, clM, hgM, auT, clT, hgT, auSS, clSS, hgSS);
}

// ═══════════════════════════════════════════════════════════════════════
// DAILY DATA FETCHER  (all 12 tickers, cached per day)
// ═══════════════════════════════════════════════════════════════════════
function fetchAllDaily() {
  var cache = CacheService.getScriptCache();
  var today = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var syms  = ['GC=F','DX-Y.NYB','^TNX','GDX','CL=F','HG=F','SPY','RB=F','BZ=F','USO','USL','^VIX'];
  var out   = {};
  syms.forEach(function(sym) {
    var k = 'day_' + today + '_' + sym;
    var hit = null;
    try { hit = cache.get(k); } catch(e) {}
    if (hit) { try { out[sym] = JSON.parse(hit); return; } catch(e) {} }
    var range = (sym === '^VIX') ? '5d' : '3mo';
    out[sym] = yfd(sym, range);
    if (out[sym]) {
      var ttl = 21600;
      try { cache.put(k, JSON.stringify(out[sym]), ttl); } catch(e) {}
    }
  });
  return out;
}

function yfd(sym, range) {
  var hosts = ['query1.finance.yahoo.com','query2.finance.yahoo.com'];
  var opts  = {muteHttpExceptions:true, headers:{'User-Agent':'Mozilla/5.0','Accept':'application/json'}};
  for (var hi = 0; hi < hosts.length; hi++) {
    try {
      var url = 'https://'+hosts[hi]+'/v8/finance/chart/'+encodeURIComponent(sym)+'?interval=1d&range='+range;
      var j   = JSON.parse(UrlFetchApp.fetch(url, opts).getContentText());
      if (!j.chart || !j.chart.result || !j.chart.result[0]) continue;
      var cls = j.chart.result[0].indicators.quote[0].close.filter(function(c){return c!=null;});
      if (cls.length > 0) return cls;
    } catch(e) { Logger.log('yfd '+sym+': '+e); }
  }
  return null;
}

// ═══════════════════════════════════════════════════════════════════════
// BAR FETCHER  (15m and 1h, by instrument)
// ═══════════════════════════════════════════════════════════════════════
function yfBars(symEnc, interval, range, n, cacheKey) {
  var sc  = CacheService.getScriptCache();
  var ttl = interval === '1h' ? 3600 : 600;
  try { var hit = sc.get(cacheKey); if (hit) return JSON.parse(hit); } catch(e) {}
  var hosts = ['query1.finance.yahoo.com','query2.finance.yahoo.com'];
  var opts  = {muteHttpExceptions:true, headers:{'User-Agent':'Mozilla/5.0','Accept':'application/json'}};
  for (var hi = 0; hi < hosts.length; hi++) {
    try {
      var url  = 'https://'+hosts[hi]+'/v8/finance/chart/'+symEnc+'?interval='+interval+'&range='+range;
      var j    = JSON.parse(UrlFetchApp.fetch(url, opts).getContentText());
      if (!j.chart || !j.chart.result || !j.chart.result[0]) continue;
      var res  = j.chart.result[0]; var ts = res.timestamp; var q = res.indicators.quote[0];
      var bars = [];
      for (var i = 0; i < ts.length; i++) {
        if (!q.open[i]||!q.close[i]) continue;
        var d = new Date(ts[i]*1000);
        var p = function(x){return x<10?'0'+x:''+x;};
        bars.push({dt: d.getUTCFullYear()+'-'+p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate())+
                       ' '+p(d.getUTCHours())+':'+p(d.getUTCMinutes()),
                   o:q.open[i], h:q.high[i], l:q.low[i], c:q.close[i], v:q.volume[i]||0});
      }
      if (!bars.length) continue;
      var out = bars.slice(-n);
      try { sc.put(cacheKey, JSON.stringify(out), ttl); } catch(e) {}
      return out;
    } catch(e) { Logger.log('yfBars '+cacheKey+': '+e); }
  }
  return [];
}

// ═══════════════════════════════════════════════════════════════════════
// GOLD — MACRO SCORE
// ═══════════════════════════════════════════════════════════════════════
function getAuMacro(d) {
  var gold = d['GC=F']; var dxy = d['DX-Y.NYB']; var tnx = d['^TNX'];
  var gdx  = d['GDX'];  var vix = d['^VIX'];
  var res  = {raw:0, score:0, dir:1, strength:'WEAK', components:[], error:null, price:null, vix:null, key:'AU'};
  if (!gold || gold.length < 10) { res.error = 'GC=F unavailable'; return res; }
  var n = gold.length;
  res.price = gold[n-1];
  res.vix   = vix && vix.length ? Math.round(vix[vix.length-1]*10)/10 : null;
  var e50   = emaArr(gold, Math.min(50,n));
  var s1 = gold[n-2] > e50[n-2] ? 1 : -1;
  var s2 = (tnx && tnx.length >= 3) ? (tnx[tnx.length-2] < tnx[tnx.length-3] ? 1 : -1) : 0;
  var s3 = (dxy && dxy.length >= 3) ? (dxy[dxy.length-2] < dxy[dxy.length-3] ? 1 : -1) : 0;
  var s4 = n >= 8 ? (gold[n-2] > gold[n-7] ? 1 : -1) : 0;
  var s5 = (gdx && gdx.length >= 3 && n >= 3)
    ? ((gdx[gdx.length-2]/gdx[gdx.length-3]) > (gold[n-2]/gold[n-3]) ? 1 : -1) : 0;
  var raw = s1+s2+s3+s4+s5;
  res.raw   = raw;
  res.score = Math.round(raw/5*100)/10;   // −100 to +100 normalized
  res.dir   = raw >= 0 ? 1 : -1;
  var ag    = Math.abs(raw);
  res.strength = ag >= 5 ? 'STRONG' : ag >= 3 ? 'MODERATE' : 'WEAK';
  var mom5 = n >= 8 ? Math.round((gold[n-2]/gold[n-7]-1)*1000)/10 : 0;
  res.components = [
    {name:'Gold vs EMA50',    val:s1, note:'Gold '+(s1>0?'ABOVE':'BELOW')+' EMA50 ($'+Math.round(e50[n-2])+') — macro trend'},
    {name:'10Y yield dir',    val:s2, note:'Yield '+(s2===0?'n/a':s2>0?'falling ↓ (bullish gold)':'rising ↑ (bearish gold)')},
    {name:'DXY direction',    val:s3, note:'DXY '+(s3===0?'n/a':s3>0?'falling ↓ (gold +)':'rising ↑ (gold −)')},
    {name:'Gold 5d momentum', val:s4, note:(mom5>=0?'+':'')+mom5+'% over 5 days'},
    {name:'GDX vs Gold',      val:s5, note:'Miners '+(s5===0?'n/a':s5>0?'leading ↑ (bullish)':'lagging ↓ (weak)')}
  ];
  return res;
}

// ═══════════════════════════════════════════════════════════════════════
// OIL — MACRO SCORE
// ═══════════════════════════════════════════════════════════════════════
function getClMacro(d) {
  var cl=d['CL=F']; var hg=d['HG=F']; var spy=d['SPY']; var rb=d['RB=F'];
  var bz=d['BZ=F']; var uso=d['USO']; var usl=d['USL']; var vix=d['^VIX'];
  var res = {raw:0, score:0, dir:1, strength:'WEAK', components:[], error:null, price:null, vix:null, key:'CL'};
  if (!cl || cl.length < 12) { res.error = 'CL=F unavailable'; return res; }
  var n = cl.length;
  res.price = cl[n-1];
  res.vix   = vix && vix.length ? Math.round(vix[vix.length-1]*10)/10 : null;
  var e50 = emaArr(cl, Math.min(50,n));
  var c1 = cl[n-2] > e50[n-2] ? 1 : -1;
  var c2 = (hg && hg.length >= 8) ? (hg[hg.length-2]/hg[hg.length-7] > 1 ? 1 : -1) : 0;
  var c3 = n >= 8 ? (cl[n-2] > cl[n-7] ? 1 : -1) : 0;
  var c4 = (spy && spy.length >= 8) ? (spy[spy.length-2]/spy[spy.length-7] > 1 ? 1 : -1) : 0;
  // RBOB crack spread
  var c5 = 0, crackNow = null;
  if (rb && rb.length >= 8 && n >= 8) {
    var mn = Math.min(rb.length, n);
    crackNow = Math.round(rb[mn-2]*42 - cl[n-2]);
    c5 = (rb[mn-2]*42 - cl[n-2]) > (rb[mn-7]*42 - cl[n-7]) ? 1 : -1;
  }
  // Brent-WTI z-score
  var c6 = 0, bwtiZ = null;
  if (bz && bz.length >= 8 && n >= 8) {
    var len = Math.min(bz.length,n), bwArr = [];
    for (var i=0;i<len;i++) bwArr.push(bz[i]-cl[i]);
    var wN = Math.min(60, bwArr.length-1);
    var win = bwArr.slice(bwArr.length-1-wN, bwArr.length-1);
    var wm = win.reduce(function(a,v){return a+v;},0)/win.length;
    var ws = Math.sqrt(win.reduce(function(a,v){return a+Math.pow(v-wm,2);},0)/win.length);
    if (ws > 0.01) { bwtiZ = (bwArr[bwArr.length-2]-wm)/ws; c6 = bwtiZ<-0.2?1:bwtiZ>0.5?-1:0; }
  }
  // Term structure
  var c7 = 0, termR = null;
  if (uso && uso.length >= 8 && usl && usl.length >= 8) {
    var tl = Math.min(uso.length,usl.length);
    termR = Math.round(uso[tl-2]/usl[tl-2]*1000)/1000;
    c7 = (uso[tl-2]/usl[tl-2]) > (uso[tl-7]/usl[tl-7]) ? 1 : -1;
  }
  var raw = c1+c2+c3+c4+c5+c6+c7;
  res.raw = raw; res.score = raw; res.dir = raw >= 0 ? 1 : -1;
  var ar = Math.abs(raw);
  res.strength = ar >= 5 ? 'STRONG' : ar >= 3 ? 'MODERATE' : 'WEAK';
  var mom5 = n >= 8 ? Math.round((cl[n-2]/cl[n-7]-1)*1000)/10 : 0;
  var bwSpread = (bz && bz.length >= 2 && n >= 2) ? Math.round((bz[bz.length-2]-cl[n-2])*100)/100 : null;
  res.components = [
    {name:'CL vs EMA50',          val:c1, note:'CL '+(c1>0?'ABOVE':'BELOW')+' EMA50 ($'+Math.round(e50[n-2]*100)/100+') — macro trend'},
    {name:'Copper 5d dir',        val:c2, note:'HG=F '+(c2===0?'n/a':c2>0?'rising ↑ (demand bullish oil)':'falling ↓ (demand bearish oil)')},
    {name:'Oil 5d momentum',      val:c3, note:(mom5>=0?'+':'')+mom5+'% over 5 days'},
    {name:'SPY 5d dir',           val:c4, note:'Equity '+(c4===0?'n/a':c4>0?'rising ↑ (risk-on = bullish oil)':'falling ↓ (risk-off = bearish)')},
    {name:'RBOB crack [BKZ2018]', val:c5, note:c5===0?'n/a':(crackNow!==null?'crack=$'+crackNow+'/bbl  ':'')+
      (c5>0?'widening ↑ (refiners buying crude)':'narrowing ↓ (demand weak)')},
    {name:'Brent-WTI z60 [GS]',  val:c6, note:bwtiZ!==null?'z='+Math.round(bwtiZ*100)/100+
      (bwSpread!==null?' spread=$'+bwSpread:'')+
      (c6===1?' narrowing → bullish WTI':c6===-1?' widening → bearish WTI':' neutral (−0.2 to +0.5)'):'n/a'},
    {name:'Term-struct USO/USL',  val:c7, note:c7===0?'n/a':(termR!==null?'ratio='+termR+'  ':'')+
      (c7>0?'rising ↑ backwardation (bullish)':'falling ↓ contango (bearish)')}
  ];
  return res;
}

// ═══════════════════════════════════════════════════════════════════════
// COPPER — MACRO SCORE
// ═══════════════════════════════════════════════════════════════════════
function getHgMacro(d) {
  var hg=d['HG=F']; var dx=d['DX-Y.NYB']; var spy=d['SPY']; var cl=d['CL=F']; var vix=d['^VIX'];
  var res = {raw:0, score:0, dir:1, strength:'WEAK', components:[], error:null, price:null, vix:null, key:'HG'};
  if (!hg || hg.length < 12) { res.error = 'HG=F unavailable'; return res; }
  var n = hg.length;
  res.price = hg[n-1];
  res.vix   = vix && vix.length ? Math.round(vix[vix.length-1]*10)/10 : null;
  var e50 = emaArr(hg, Math.min(50,n));
  var h1 = hg[n-2] > e50[n-2] ? 1 : -1;
  var h2 = (dx && dx.length >= 8) ? (dx[dx.length-2]/dx[dx.length-7] < 1 ? 1 : -1) : 0;
  var h3 = n >= 8 ? (hg[n-2] > hg[n-7] ? 1 : -1) : 0;
  var h4 = (spy && spy.length >= 8) ? (spy[spy.length-2]/spy[spy.length-7] > 1 ? 1 : -1) : 0;
  var h5 = (cl && cl.length >= 8) ? (cl[cl.length-2]/cl[cl.length-7] > 1 ? 1 : -1) : 0;
  var raw = h1+h2+h3+h4+h5;
  res.raw = raw; res.score = raw; res.dir = raw >= 0 ? 1 : -1;
  var ar = Math.abs(raw);
  res.strength = ar >= 4 ? 'STRONG' : ar >= 2 ? 'MODERATE' : 'WEAK';
  var mom5 = n >= 8 ? Math.round((hg[n-2]/hg[n-7]-1)*1000)/10 : 0;
  var dxyPct = (dx && dx.length >= 8) ? Math.round((dx[dx.length-2]/dx[dx.length-7]-1)*1000)/10 : null;
  var oilPct = (cl && cl.length >= 8) ? Math.round((cl[cl.length-2]/cl[cl.length-7]-1)*1000)/10 : null;
  res.components = [
    {name:'HG vs EMA50',   val:h1, note:'Copper '+(h1>0?'ABOVE':'BELOW')+' EMA50 ($'+Math.round(e50[n-2]*1000)/1000+'/lb)'},
    {name:'DXY 5d dir',    val:h2, note:'DXY '+(h2===0?'n/a':h2>0?'falling ↓ (dollar weak = bullish Cu)':'rising ↑ (dollar strong = bearish Cu)')+
      (dxyPct!==null?'  '+dxyPct+'% 5d':'')},
    {name:'Copper 5d mom', val:h3, note:(mom5>=0?'+':'')+mom5+'% over 5 days'},
    {name:'SPY 5d dir',    val:h4, note:'Equity '+(h4===0?'n/a':h4>0?'rising ↑ (risk-on bullish Cu)':'falling ↓ (risk-off bearish Cu)')},
    {name:'Oil 5d dir',    val:h5, note:'WTI '+(h5===0?'n/a':h5>0?'rising ↑ (global demand bullish)':'falling ↓ (demand contraction)')+
      (oilPct!==null?'  '+oilPct+'% 5d':'')}
  ];
  return res;
}

// ═══════════════════════════════════════════════════════════════════════
// TECHNICALS — Gold (15m ATR, 4H EMA)
// ═══════════════════════════════════════════════════════════════════════
function getAuTech(b15, b1h) {
  if (!b15 || b15.length < 35) return null;
  var cls  = b15.map(function(b){return b.c;});
  var e9   = emaArr(cls,9); var e21 = emaArr(cls,21);
  var last = b15[b15.length-1];
  var atr  = calcAtr14(b15);   // gold uses 15m ATR for SL/TP
  var b4h  = resampleTo4H(b1h);
  var c4h  = b4h.map(function(b){return b.c;});
  var li   = c4h.length-1;
  var h4e9=null,h4e21=null,h4e50=null;
  if (li >= 0) {
    h4e9=emaArr(c4h,9)[li]; h4e21=emaArr(c4h,21)[li]; h4e50=emaArr(c4h,Math.min(50,c4h.length))[li];
  }
  var sv = sessVwap(b15, '13:30');
  return {price:last.c, h:last.h, l:last.l,
    ema9:e9[e9.length-1], ema21:e21[e21.length-1],
    h4e9:h4e9, h4e21:h4e21, h4e50:h4e50,
    atr:atr, vwap:sv.vwap, vwapStd:sv.std, vwapBars:sv.n,
    vwapDist: sv.std>0.01 ? (last.c-sv.vwap)/sv.std : 99};
}

// ── TECHNICALS — Oil (daily ATR from 1h)
function getClTech(b15, b1h) {
  if (!b15 || b15.length < 35) return null;
  var cls  = b15.map(function(b){return b.c;});
  var e9   = emaArr(cls,9); var e21 = emaArr(cls,21);
  var last = b15[b15.length-1];
  var atr  = getDailyAtr(b1h) || calcAtr14(b15)*6;
  var sv   = sessVwap(b15, '14:00');
  return {price:last.c, h:last.h, l:last.l,
    ema9:e9[e9.length-1], ema21:e21[e21.length-1],
    atr:atr, atrDay:getDailyAtr(b1h), atr15:calcAtr14(b15),
    vwap:sv.vwap, vwapStd:sv.std, vwapBars:sv.n,
    vwapDist: sv.std>0.01 ? (last.c-sv.vwap)/sv.std : 99};
}

// ── TECHNICALS — Copper (daily ATR from 1h)
function getHgTech(b15, b1h) {
  if (!b15 || b15.length < 35) return null;
  var cls  = b15.map(function(b){return b.c;});
  var e9   = emaArr(cls,9); var e21 = emaArr(cls,21);
  var last = b15[b15.length-1];
  var atr  = getDailyAtr(b1h) || calcAtr14(b15)*6;
  var sv   = sessVwap(b15, '13:00');
  return {price:last.c, h:last.h, l:last.l,
    ema9:e9[e9.length-1], ema21:e21[e21.length-1],
    atr:atr, atrDay:getDailyAtr(b1h), atr15:calcAtr14(b15),
    vwap:sv.vwap, vwapStd:sv.std, vwapBars:sv.n,
    vwapDist: sv.std>0.01 ? (last.c-sv.vwap)/sv.std : 99};
}

// ═══════════════════════════════════════════════════════════════════════
// SESSION VWAP
// ═══════════════════════════════════════════════════════════════════════
function sessVwap(b15, utcOpen) {
  var now  = new Date();
  var ymd  = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
  var sO   = new Date(ymd+'T'+utcOpen+':00Z');
  var pfx  = ymd+' ';
  var sess = b15.filter(function(b){
    return new Date(b.dt.replace(' ','T')+'Z') >= sO && b.dt.indexOf(pfx) === 0;
  });
  if (sess.length < 2) return {vwap:0, std:0.0001, n:sess.length};
  var tp  = sess.map(function(b){return (b.h+b.l+b.c)/3;});
  var avg = tp.reduce(function(a,v){return a+v;},0)/tp.length;
  var v2  = tp.reduce(function(a,x){return a+Math.pow(x-avg,2);},0)/tp.length;
  return {vwap:avg, std:Math.max(Math.sqrt(v2),0.0001), n:tp.length};
}

// ═══════════════════════════════════════════════════════════════════════
// SESSION STATE HELPERS
// ═══════════════════════════════════════════════════════════════════════
function auSessState(nowH) {
  if (nowH < AU_SESS_OPEN)   return 'BEFORE';
  if (nowH < AU_ENTRY_START) return 'CHOP';
  if (nowH <= AU_ENTRY_END)  return 'ENTRY';
  if (nowH <= AU_SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}
function clSessState(now, nowH) {
  if (nowH < CL_SESS_OPEN)   return 'BEFORE';
  if (nowH < CL_ENTRY_START) return 'CHOP';
  if (nowH <= CL_ENTRY_END)  return 'ENTRY';
  if (nowH <= CL_SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}
function hgSessState(nowH) {
  if (nowH < HG_SESS_OPEN)   return 'BEFORE';
  if (nowH < HG_ENTRY_START) return 'CHOP';
  if (nowH <= HG_ENTRY_END)  return 'ENTRY';
  if (nowH <= HG_SESS_CLOSE) return 'LATE';
  return 'CLOSED';
}

// ═══════════════════════════════════════════════════════════════════════
// SETUP — Gold
// ═══════════════════════════════════════════════════════════════════════
function getAuSetup(macro, tech, ss, now) {
  var empty = {action:'WAIT', dir:macro.dir===1?'LONG':'SHORT',
               reason:macro.error||'Bar data unavailable', checks:null};
  if (!tech || macro.error) return empty;
  var dir = macro.dir; var price = tech.price; var atr = tech.atr;
  var mon = now.getUTCMonth();
  var vixOk  = macro.vix===null || macro.vix < 30;
  var monOk  = AU_SKIP_MONTHS.indexOf(mon) === -1;
  var dist   = tech.vwapStd > 0.0001 ? (price-tech.vwap)/tech.vwapStd : 99;
  var h4ok   = (tech.h4e9 && tech.h4e21) ? (dir===1?tech.h4e9>tech.h4e21:tech.h4e9<tech.h4e21) : null;
  var emaOk  = dir===1 ? tech.ema9>tech.ema21 : tech.ema9<tech.ema21;
  var vwOk   = tech.vwapBars>=4 && (dir===1?(dist>=-1.0&&dist<=0.3):(dist>=-0.3&&dist<=1.0));
  var sessOk = ss==='ENTRY';
  var MNAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var vStr   = macro.vix!==null?'VIX '+macro.vix.toFixed(1):'VIX n/a';
  // Short blocked above 4H EMA50
  if (dir===-1 && tech.h4e50 && price > tech.h4e50)
    return {action:'SKIP', dir:'SHORT', reason:'SHORT blocked — gold above 4H EMA50 ($'+fix2(tech.h4e50)+') — major uptrend', checks:null};
  var checks = [
    {label:'VIX < 30',           ok:vixOk,
     note:vixOk?(macro.vix?vStr+(macro.vix>=25?' — elevated, reduce size':' — OK'):'VIX unavail'):vStr+' ≥ 30 — skip day'},
    {label:'Not June',           ok:monOk,
     note:monOk?MNAMES[mon]+' — OK':'JUNE — skip entire month (Sharpe −1.67 historically)'},
    {label:'Session (entry)',    ok:sessOk,
     note:sessOk?'18:00–20:30 Dubai (14:00–16:30 UTC) — entry open':'Wait — entry opens 18:00 Dubai'},
    {label:'4H EMA aligned',     ok:h4ok!==false,
     note:h4ok===null?'No 4H data':h4ok?'4H EMA9 $'+fix2(tech.h4e9)+' aligned':'4H EMA misaligned — counter-trend'},
    {label:'15m EMA9 > EMA21',   ok:emaOk,
     note:emaOk?'EMA9 $'+fix2(tech.ema9)+' aligned with '+( dir===1?'LONG':'SHORT'):'EMA9 $'+fix2(tech.ema9)+' / EMA21 $'+fix2(tech.ema21)+' — against bias'},
    {label:'VWAP pullback zone', ok:vwOk,
     note:vwOk?'dist='+Math.round(dist*100)/100+'σ ✓  VWAP=$'+fix2(tech.vwap):
       tech.vwapBars<4?'Only '+tech.vwapBars+' bars (need ≥4)':
       'dist='+Math.round(dist*100)/100+'σ  need '+(dir===1?'−1.0 to +0.3σ':'−0.3 to +1.0σ')+
       '  zone $'+fix2(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd)+'–$'+
       fix2(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd)}
  ];
  var allOk = vixOk && monOk && sessOk && h4ok!==false && emaOk && vwOk;
  if (allOk) return buildTrade(dir, price, atr, checks, 'oz');
  return {action:'WAIT', dir:dir===1?'LONG':'SHORT', checks:checks, atr:atr,
    vwapZone:{lo:fix2(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd),
              hi:fix2(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd)}};
}

// ═══════════════════════════════════════════════════════════════════════
// SETUP — Oil
// ═══════════════════════════════════════════════════════════════════════
function getClSetup(macro, tech, ss, now) {
  var empty = {action:'WAIT', dir:macro.dir===1?'LONG':'SHORT', reason:macro.error||'Bar data unavailable', checks:null};
  if (!tech || macro.error) return empty;
  var dir = macro.dir; var price = tech.price; var atr = tech.atr;
  var mon = now.getUTCMonth(); var dow = now.getUTCDay();
  var vixOk   = macro.vix===null || macro.vix < 30;
  var monOk   = CL_SKIP_MONTHS.indexOf(mon) === -1;
  var wedOk   = !CL_SKIP_WED || dow !== 3;
  var scoreOk = Math.abs(macro.raw) >= CL_SCORE_MIN;
  var dist    = tech.vwapStd > 0.0001 ? (price-tech.vwap)/tech.vwapStd : 99;
  var emaOk   = dir===1 ? tech.ema9>tech.ema21 : tech.ema9<tech.ema21;
  var vwOk    = tech.vwapBars>=4 && (dir===1?(dist>=-1.0&&dist<=0.3):(dist>=-0.3&&dist<=1.0));
  var sessOk  = ss==='ENTRY';
  var MNAMES  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var vStr    = macro.vix!==null?'VIX '+macro.vix.toFixed(1):'VIX n/a';
  var checks = [
    {label:'VIX < 30',           ok:vixOk,
     note:vixOk?(macro.vix?vStr+(macro.vix>=25?' — elevated, reduce size':' — OK'):'VIX unavail'):vStr+' ≥ 30 — skip day'},
    {label:'Not skip-month',     ok:monOk,
     note:monOk?MNAMES[mon]+' — OK':MNAMES[mon]+' — SKIP MONTH (Jan/Apr/Jun/Sep/Oct)'},
    {label:'Not Wednesday (EIA)',ok:wedOk,
     note:wedOk?(dow===3?'—':'Not Wed — OK'):'WEDNESDAY — EIA 15:30 UTC ±$1.5/bbl spike. Skip all day.'},
    {label:'Score |raw| ≥ '+CL_SCORE_MIN, ok:scoreOk,
     note:'Raw: '+(macro.raw>=0?'+':'')+macro.raw+'/7  '+(scoreOk?'✓ majority':'✗ need |raw| ≥ '+CL_SCORE_MIN)},
    {label:'Session (entry)',    ok:sessOk,
     note:sessOk?'18:30–22:30 Dubai (14:30–18:30 UTC) open':'Wait — entry opens 18:30 Dubai'},
    {label:'15m EMA aligned',    ok:emaOk,
     note:emaOk?'EMA9 $'+fix2(tech.ema9)+' aligned':'EMA9 $'+fix2(tech.ema9)+' / EMA21 $'+fix2(tech.ema21)+' — against bias'},
    {label:'VWAP pullback zone', ok:vwOk,
     note:vwOk?'dist='+Math.round(dist*100)/100+'σ ✓  VWAP=$'+fix2(tech.vwap):
       tech.vwapBars<4?'Only '+tech.vwapBars+' bars (need ≥4)':
       'dist='+Math.round(dist*100)/100+'σ  need '+(dir===1?'−1.0 to +0.3σ':'−0.3 to +1.0σ')+
       '  zone $'+fix2(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd)+'–$'+
       fix2(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd)}
  ];
  var allOk = vixOk && monOk && wedOk && scoreOk && sessOk && emaOk && vwOk;
  if (allOk) return buildTrade(dir, price, atr, checks, 'bbl');
  return {action:'WAIT', dir:dir===1?'LONG':'SHORT', checks:checks, atr:atr,
    vwapZone:{lo:fix2(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd),
              hi:fix2(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd)}};
}

// ═══════════════════════════════════════════════════════════════════════
// SETUP — Copper
// ═══════════════════════════════════════════════════════════════════════
function getHgSetup(macro, tech, ss, now) {
  var empty = {action:'WAIT', dir:macro.dir===1?'LONG':'SHORT', reason:macro.error||'Bar data unavailable', checks:null};
  if (!tech || macro.error) return empty;
  var dir = macro.dir; var price = tech.price; var atr = tech.atr;
  var mon = now.getUTCMonth(); var dow = now.getUTCDay();
  var vixOk   = macro.vix===null || macro.vix < 30;
  var monOk   = HG_SKIP_MONTHS.indexOf(mon) === -1;
  var friOk   = !HG_SKIP_FRI || dow !== 5;
  var scoreOk = Math.abs(macro.raw) >= HG_SCORE_MIN;
  var dist    = tech.vwapStd > 0.0001 ? (price-tech.vwap)/tech.vwapStd : 99;
  var emaOk   = dir===1 ? tech.ema9>tech.ema21 : tech.ema9<tech.ema21;
  var vwOk    = tech.vwapBars>=4 && (dir===1?(dist>=-1.0&&dist<=0.3):(dist>=-0.3&&dist<=1.0));
  var sessOk  = ss==='ENTRY';
  var MNAMES  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var vStr    = macro.vix!==null?'VIX '+macro.vix.toFixed(1):'VIX n/a';
  var checks = [
    {label:'VIX < 30',           ok:vixOk,
     note:vixOk?(macro.vix?vStr+(macro.vix>=25?' — elevated, reduce size':' — OK'):'VIX unavail'):vStr+' ≥ 30 — skip day'},
    {label:'Not skip-month',     ok:monOk,
     note:monOk?MNAMES[mon]+' — OK':MNAMES[mon]+' — SKIP MONTH (Jan/May/Jul/Dec)'},
    {label:'Not Friday',         ok:friOk,
     note:friOk?(dow===5?'—':'Not Fri — OK'):'FRIDAY — gap risk into weekend. Skip all day.'},
    {label:'Score |raw| ≥ '+HG_SCORE_MIN, ok:scoreOk,
     note:'Raw: '+(macro.raw>=0?'+':'')+macro.raw+'/5  '+(scoreOk?'✓ majority':'✗ need |raw| ≥ '+HG_SCORE_MIN)},
    {label:'Session (entry)',    ok:sessOk,
     note:sessOk?'17:30–21:00 Dubai (13:30–17:00 UTC) open':'Wait — entry opens 17:30 Dubai'},
    {label:'15m EMA aligned',    ok:emaOk,
     note:emaOk?'EMA9 $'+fix4(tech.ema9)+' aligned':'EMA9 $'+fix4(tech.ema9)+' / EMA21 $'+fix4(tech.ema21)+' — against bias'},
    {label:'VWAP pullback zone', ok:vwOk,
     note:vwOk?'dist='+Math.round(dist*100)/100+'σ ✓  VWAP=$'+fix4(tech.vwap):
       tech.vwapBars<4?'Only '+tech.vwapBars+' bars (need ≥4)':
       'dist='+Math.round(dist*100)/100+'σ  need '+(dir===1?'−1.0 to +0.3σ':'−0.3 to +1.0σ')+
       '  zone $'+fix4(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd)+'–$'+
       fix4(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd)}
  ];
  var allOk = vixOk && monOk && friOk && scoreOk && sessOk && emaOk && vwOk;
  if (allOk) return buildTrade(dir, price, atr, checks, 'lbs');
  return {action:'WAIT', dir:dir===1?'LONG':'SHORT', checks:checks, atr:atr,
    vwapZone:{lo:fix4(dir===1?tech.vwap-tech.vwapStd:tech.vwap-0.3*tech.vwapStd),
              hi:fix4(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap+tech.vwapStd)}};
}

// ── Shared trade-level builder
function buildTrade(dir, price, atr, checks, unit) {
  var sl  = dir===1 ? price-SL_MULT*atr  : price+SL_MULT*atr;
  var tp1 = dir===1 ? price+TP1_MULT*atr : price-TP1_MULT*atr;
  var tp2 = dir===1 ? price+TP2_MULT*atr : price-TP2_MULT*atr;
  var sz  = (ACCOUNT*RISK_PCT)/(SL_MULT*atr);
  return {action:'ACTIVE', dir:dir===1?'LONG':'SHORT',
    entry:price, sl:sl, tp1:tp1, tp2:tp2,
    size:Math.round(sz*100)/100,
    sz1: Math.round(sz*TP1_FRAC*100)/100,
    sz2: Math.round(sz*(1-TP1_FRAC)*100)/100,
    riskDollar:Math.round(ACCOUNT*RISK_PCT),
    atr:atr, slPts:SL_MULT*atr, tp1Pts:TP1_MULT*atr, tp2Pts:TP2_MULT*atr,
    unit:unit, checks:checks};
}

// ═══════════════════════════════════════════════════════════════════════
// DASHBOARD WRITER
// ═══════════════════════════════════════════════════════════════════════
function writeDashboard(sh, now, nowH,
  auM, auT, auSS, auSU,
  clM, clT, clSS, clSU,
  hgM, hgT, hgSS, hgSU) {

  sh.clear();
  // 5-col layout: A=label, B=Gold, C=Oil, D=Copper, E=Status
  [200, 160, 160, 160, 110].forEach(function(w,i){ sh.setColumnWidth(i+1,w); });

  var utc = Utilities.formatDate(now,'UTC','HH:mm')+' UTC';
  var dxb = Utilities.formatDate(now,'Asia/Dubai','HH:mm')+' DXB';
  var r = 1;

  // ── MASTER HEADER
  mrow(sh,r,5,'SIGNAL COMMAND CENTER  ·  Gold · Oil · Copper  ·  '+utc+'  /  '+dxb+'  ·  Auto-refresh 5 min',
    {bg:'#0d0d14',fg:'#9090cc',sz:11,bold:true,h:30}); r++;
  mrow(sh,r,5,'Account $'+ACCOUNT+'  ·  Risk '+Math.round(RISK_PCT*100)+'%/trade ($'+Math.round(ACCOUNT*RISK_PCT)+')  ·  SL=0.75×ATR  TP1=1.5×ATR(60%)  TP2=2.5×ATR(40%)',
    {bg:'#0a0a10',fg:'#404060',sz:9,h:18}); r++; r++;

  // ── COMMAND CENTER TABLE
  var MNAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var DOW    = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var nowMon = now.getUTCMonth(); var nowDow = now.getUTCDay();

  // Column headers
  cell(sh,r,1,DOW[nowDow]+' '+MNAMES[nowMon]+' '+now.getUTCDate(),{bg:'#111120',fg:'#505070',sz:9,bold:true,h:22});
  cell(sh,r,2,'⚡  GOLD  (GC=F)',{bg:'#1a1a0a',fg:'#FFD700',sz:10,bold:true});
  cell(sh,r,3,'⚡  OIL  (CL=F)', {bg:'#1a0e00',fg:'#ff9922',sz:10,bold:true});
  cell(sh,r,4,'⚡  COPPER (HG=F)',{bg:'#1a0c00',fg:'#E87C2C',sz:10,bold:true});
  cell(sh,r,5,'STATUS',           {bg:'#111120',fg:'#505070',sz:9,bold:true}); r++;

  // Price row
  var auP = auT?auT.price:(auM.price||0); var clP = clT?clT.price:(clM.price||0); var hgP = hgT?hgT.price:(hgM.price||0);
  cell(sh,r,1,'PRICE',                       {bg:'#0d0d1a',fg:'#505070',sz:9,h:36});
  cell(sh,r,2,auP?'$'+fix2(auP):'—',         {bg:'#0d0d0a',fg:'#FFD700',sz:15,bold:true});
  cell(sh,r,3,clP?'$'+fix2(clP)+'  /bbl':'—',{bg:'#0d0800',fg:'#ff9922',sz:13,bold:true});
  cell(sh,r,4,hgP?'$'+fix4(hgP)+'  /lb':'—', {bg:'#0d0800',fg:'#E87C2C',sz:13,bold:true});
  cell(sh,r,5,'',{bg:'#0d0d1a',fg:'#606070',sz:9}); r++;

  // Direction + score row
  var auDir = auM.dir===1?'▲ LONG':'▼ SHORT';
  var clDir = clM.dir===1?'▲ LONG':'▼ SHORT';
  var hgDir = hgM.dir===1?'▲ LONG':'▼ SHORT';
  cell(sh,r,1,'DIRECTION + SCORE',                   {bg:'#0d0d1a',fg:'#505070',sz:9,h:32});
  cell(sh,r,2,auDir+'  '+auM.strength+'\n'+auM.score.toFixed(1)+'% ('+auM.raw+'/5)',
    {bg:'#0d0d0a',fg:auM.dir===1?'#00e676':'#ff5252',sz:10,bold:true});
  cell(sh,r,3,clDir+'  '+clM.strength+'\n'+(clM.raw>=0?'+':'')+clM.raw+'/7',
    {bg:'#0d0800',fg:clM.dir===1?'#00e676':'#ff5252',sz:10,bold:true});
  cell(sh,r,4,hgDir+'  '+hgM.strength+'\n'+(hgM.raw>=0?'+':'')+hgM.raw+'/5',
    {bg:'#0d0800',fg:hgM.dir===1?'#00e676':'#ff5252',sz:10,bold:true});
  var vixNow = auM.vix||clM.vix||hgM.vix;
  var vixFg  = !vixNow?'#808080':vixNow>=30?'#ff1744':vixNow>=25?'#ff9800':vixNow>=20?'#ffea00':'#00e676';
  cell(sh,r,5,vixNow?'VIX\n'+vixNow.toFixed(1):'VIX\nn/a', {bg:'#0d0d1a',fg:vixFg,sz:11,bold:true}); r++;

  // Session row
  var sSt = {BEFORE:'⏳ Pre-sess',CHOP:'⚠ Chop zone',ENTRY:'✅ ENTRY',LATE:'🕐 Late',CLOSED:'🔒 Closed'};
  var sFg = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};
  cell(sh,r,1,'SESSION',        {bg:'#0d0d1a',fg:'#505070',sz:9,h:28});
  cell(sh,r,2,sSt[auSS]||auSS, {bg:'#0d0d0a',fg:sFg[auSS]||'#808080',sz:10,bold:auSS==='ENTRY'});
  cell(sh,r,3,sSt[clSS]||clSS, {bg:'#0d0800',fg:sFg[clSS]||'#808080',sz:10,bold:clSS==='ENTRY'});
  cell(sh,r,4,sSt[hgSS]||hgSS, {bg:'#0d0800',fg:sFg[hgSS]||'#808080',sz:10,bold:hgSS==='ENTRY'});
  var dayFlags = (nowDow===3?'⚠ WED (oil skip)':'')+(nowDow===5?' ⚠ FRI (Cu skip)':'');
  cell(sh,r,5,dayFlags||'Day OK',{bg:'#0d0d1a',fg:dayFlags?'#ff9800':'#4caf50',sz:9}); r++;

  // Action row
  function actionLabel(su) {
    if (su.action==='ACTIVE') return '⚡ ENTER '+su.dir;
    if (su.action==='SKIP')   return '🚫 BLOCKED';
    if (!su.checks)           return '⚠ NO DATA';
    var fail = su.checks.filter(function(c){return !c.ok;}).length;
    return (su.dir||'—')+'\n'+fail+' gate'+(fail!==1?'s':'')+' pending';
  }
  function actionFg(su) {
    return su.action==='ACTIVE'?'#00ff88':su.action==='SKIP'?'#ff9800':'#9e9e9e';
  }
  cell(sh,r,1,'SIGNAL',       {bg:'#0d0d1a',fg:'#505070',sz:9,h:34});
  cell(sh,r,2,actionLabel(auSU),{bg:auSU.action==='ACTIVE'?'#0a2a0a':'#0a0a18',fg:actionFg(auSU),sz:10,bold:auSU.action==='ACTIVE'});
  cell(sh,r,3,actionLabel(clSU),{bg:clSU.action==='ACTIVE'?'#1a1000':'#0d0800',fg:actionFg(clSU),sz:10,bold:clSU.action==='ACTIVE'});
  cell(sh,r,4,actionLabel(hgSU),{bg:hgSU.action==='ACTIVE'?'#1a0800':'#0d0800',fg:actionFg(hgSU),sz:10,bold:hgSU.action==='ACTIVE'});
  cell(sh,r,5,'',{bg:'#0d0d1a'}); r++; r++;

  // ════════════════════════════════════════════════════
  // GOLD SECTION
  // ════════════════════════════════════════════════════
  r = writeModelSection(sh, r, 'GOLD', 'GC=F', '$/oz', '#FFD700', '#1a1a0a', '#0d0d0a',
    '2yr: 235 trades · WR 48.5% · Sharpe 2.63 · ROI +2070% cumulative · MaxDD −30%',
    '5 gates: VIX<30 · not-June · session · 4H EMA · 15m EMA · VWAP zone  |  SL/TP = 15m ATR  |  Skip: June only',
    auM, auT, auSS, auSU, fix2,
    [
      '17:00 DXB / 13:00 UTC  Pre-check: macro score + VIX. If VIX≥30 or June → skip day.',
      '17:30 DXB / 13:30 UTC  VWAP starts building. Do NOT enter yet — first 30 min is chop.',
      '18:00 DXB / 14:00 UTC  ▶ ENTRY WINDOW OPENS. All gates green? Wait for VWAP pullback.',
      '20:30 DXB / 16:30 UTC  Entry closes. No new trades. Manage open positions only.',
      '21:00 DXB / 17:00 UTC  ⚠ HARD CLOSE — exit ALL positions. No exceptions.'
    ],
    [
      'NEVER trade in June (entire month) — Sharpe historically negative',
      'NEVER trade when VIX ≥ 30 — gold correlation with SPY inverts in crash regimes',
      'NEVER short gold above 4H EMA50 — major uptrend = short-side edge disappears',
      'NEVER move SL further away after entry — $20 gap moves happen in seconds',
      'NEVER hold XAU/USD overnight — gap risk at London/Asia opens is severe',
      'NEVER take more than 2 trades in one session'
    ]);

  r++;

  // ════════════════════════════════════════════════════
  // OIL SECTION
  // ════════════════════════════════════════════════════
  r = writeModelSection(sh, r, 'OIL', 'CL=F', '$/bbl', '#ff9922', '#1a0e00', '#0d0800',
    '2yr: 141 trades · WR 65.2% · Sharpe 5.86 · 1.89%/mo @ 3% risk · MaxDD −7.5%',
    '7 gates: VIX<30 · not-skip-month · not-Wed(EIA) · |score|≥2 · session · 15m EMA · VWAP zone  |  SL/TP = daily ATR  |  Skip: Jan Apr Jun Sep Oct + Wednesdays',
    clM, clT, clSS, clSU, fix2,
    [
      '18:00 DXB / 14:00 UTC  VWAP starts building. Pre-check: score + VIX + Wednesday.',
      '18:30 DXB / 14:30 UTC  ▶ ENTRY WINDOW OPENS. All 7 gates green? Wait for pullback.',
      '19:30 DXB / 15:30 UTC  EIA Wednesday: DO NOT TRADE — skip entire day if Wednesday.',
      '22:30 DXB / 18:30 UTC  Entry closes. No new trades. Manage open positions only.',
      '23:00 DXB / 19:00 UTC  ⚠ HARD CLOSE — exit ALL positions. NYMEX settlement near.'
    ],
    [
      'NEVER trade on Wednesday — EIA inventory at 15:30 UTC causes ±$1.50/bbl spikes',
      'NEVER trade in Jan/Apr/Jun/Sep/Oct — A/B tested: negative edge in backtest',
      'NEVER trade when |score| < 2 — only 4/7 or fewer components agree = coin flip',
      'NEVER trade when VIX ≥ 30 — oil demand collapses in crash regimes, no edge',
      'NEVER hold CL overnight — gap risk at Asia open and geopolitical events',
      'NEVER add to a losing position — SL is the final word'
    ]);

  r++;

  // ════════════════════════════════════════════════════
  // COPPER SECTION
  // ════════════════════════════════════════════════════
  r = writeModelSection(sh, r, 'COPPER', 'HG=F', '$/lb', '#E87C2C', '#1a0c00', '#0d0800',
    '2yr: 145 trades · WR 59.3% · Sharpe 4.49 · 2.22%/mo @ 3% risk · MaxDD −9%',
    '7 gates: VIX<30 · not-skip-month · not-Fri · |score|≥2 · session · 15m EMA · VWAP zone  |  SL/TP = daily ATR  |  Skip: Jan May Jul Dec + Fridays',
    hgM, hgT, hgSS, hgSU, fix4,
    [
      '17:00 DXB / 13:00 UTC  VWAP starts building (COMEX premarket). Pre-check score + VIX.',
      '17:30 DXB / 13:30 UTC  ▶ ENTRY WINDOW OPENS. COMEX copper market opens (9:30 ET).',
      '21:00 DXB / 17:00 UTC  Entry closes. No new trades after this.',
      '21:30 DXB / 17:30 UTC  ⚠ HARD CLOSE — exit ALL. COMEX pit closes 18:00 UTC.'
    ],
    [
      'NEVER trade on Friday — weekend gap risk on copper is severe ($0.05+ gap possible)',
      'NEVER trade in Jan/May/Jul/Dec — A/B tested: negative edge these months',
      'NEVER trade when VIX ≥ 30 — industrial metal demand collapses in crash regimes',
      'NEVER trade when |score| < 2 — no majority = coin flip (5 components, need ≥3 agree)',
      'NEVER hold COMEX copper overnight — Asia open gap risk is large',
      'Copper price in $/lb — position size in lbs. Micro HG = 2,500 lbs'
    ]);

  SpreadsheetApp.flush();
  return r;
}

// ─────────────────────────────────────────────────────────
// MODEL SECTION WRITER  (shared by all 3 models)
// ─────────────────────────────────────────────────────────
function writeModelSection(sh, r, name, sym, unit, accentC, headerBg, bodyBg,
  statsLine, filterLine, macro, tech, ss, setup, fxFn, sched, rules) {

  var dirC  = macro.dir===1?'#00e676':'#ff5252';
  var price = tech?tech.price:(macro.price||0);
  var dir   = macro.dir;

  // ── Section header
  sh.getRange(r,1,1,5).merge().setValue('━━  '+name+'  ('+sym+')  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    .setBackground(headerBg).setFontColor(accentC).setFontSize(11).setFontWeight('bold')
    .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(false);
  sh.setRowHeight(r,26); r++;
  mrow(sh,r,5,statsLine,{bg:headerBg,fg:'#606060',sz:9,h:18}); r++;
  mrow(sh,r,5,filterLine,{bg:headerBg,fg:'#404050',sz:8,h:16}); r++;

  // ── Status row: price · direction · session · VIX · date
  var vixN = macro.vix;
  var vixFg2 = !vixN?'#808080':vixN>=30?'#ff1744':vixN>=25?'#ff9800':vixN>=20?'#ffea00':'#00e676';
  var MNAMES2 = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var now2 = new Date(); var dNow = now2.getUTCDay(); var mNow = now2.getUTCMonth();
  ['PRICE','DIRECTION','SESSION','VIX','DATE FLAGS'].forEach(function(h,i){
    cell(sh,r,i+1,h,{bg:'#111118',fg:'#404055',sz:8,bold:true,h:18});});
  r++;
  var sSt2 = {BEFORE:'⏳ Pre-session',CHOP:'⚠ No entries',ENTRY:'✅ ENTRY OPEN',LATE:'🕐 Late',CLOSED:'🔒 Closed'};
  var sFg2 = {BEFORE:'#607d8b',CHOP:'#ff9800',ENTRY:'#00e676',LATE:'#ff9800',CLOSED:'#607d8b'};
  cell(sh,r,1,price?fxFn(price):'—',            {bg:bodyBg,fg:accentC,sz:14,bold:true,h:38});
  cell(sh,r,2,(dir===1?'▲ LONG':'▼ SHORT')+'  '+macro.strength, {bg:bodyBg,fg:dirC,sz:11,bold:true});
  cell(sh,r,3,sSt2[ss]||ss,                     {bg:bodyBg,fg:sFg2[ss]||'#808080',sz:10,bold:ss==='ENTRY'});
  cell(sh,r,4,vixN?vixN.toFixed(1):'—',         {bg:bodyBg,fg:vixFg2,sz:14,bold:true});
  var df = (dNow===3?' ⚠ WED':'')+(dNow===5?' ⚠ FRI':'')+
           ([0,3,5,8,9].indexOf(mNow)!==-1&&sym==='CL=F'?' ⚠ SKIP-MO':'')+
           ([0,4,6,11].indexOf(mNow)!==-1&&sym==='HG=F'?' ⚠ SKIP-MO':'')+
           (mNow===5&&sym==='GC=F'?' ⚠ JUNE':'');
  cell(sh,r,5,df||'✓ Day OK', {bg:bodyBg,fg:df?'#ff9800':'#4caf50',sz:10,bold:!!df}); r++;

  // ── VWAP & Standard Deviation Ladder
  mrow(sh,r,5,'VWAP  +  STANDARD DEVIATION LADDER',{bg:'#0a0e14',fg:'#3a5060',sz:9,bold:true,h:20}); r++;
  ['LEVEL','PRICE','σ DIST','ZONE','CURRENT POSITION'].forEach(function(h,i){
    cell(sh,r,i+1,h,{bg:'#080c10',fg:'#2a4050',sz:8,bold:true,h:18});
  }); r++;

  if (tech && tech.vwap > 0 && tech.vwapStd > 0.0001) {
    var vw = tech.vwap; var sd = tech.vwapStd; var curD = tech.vwapDist;
    var levels = [
      ['+2σ', vw+2*sd, 2.0,   'Extreme extension — FADE zone',       '#ff1744'],
      ['+1σ', vw+sd,   1.0,   dir===1?'Top of SHORT entry zone':'Top of SHORT entry zone', '#ff5252'],
      ['+0.3σ',vw+0.3*sd,0.3, dir===1?'LONG zone far edge — last long entry':'SHORT zone near edge', '#ff9800'],
      ['VWAP', vw,     0.0,   'Session anchor — mean reversion level', '#00bcd4'],
      ['−0.3σ',vw-0.3*sd,-0.3,dir===1?'SHORT zone near edge':'LONG zone far edge — last short entry','#ff9800'],
      ['−1σ', vw-sd,  -1.0,  dir===1?'Bottom of LONG entry zone':'Bottom of LONG entry zone','#00e676'],
      ['−2σ', vw-2*sd,-2.0,  'Extreme extension — FADE zone',        '#00c853']
    ];
    levels.forEach(function(lv){
      var lbl=lv[0], lp=lv[1], ls=lv[2], ldesc=lv[3], lfc=lv[4];
      var isLongZone  = dir===1 && ls>=-1.0 && ls<=0.3;
      var isShortZone = dir===-1 && ls>=-0.3 && ls<=1.0;
      var isEntry = isLongZone||isShortZone;
      var isCur   = Math.abs(curD-ls) < 0.15;
      var bg3 = lbl==='VWAP'?'#060e14': isEntry?'#0a1a0a':'#0a0a10';
      var posNote = isCur ? '← PRICE HERE ('+Math.round(curD*100)/100+'σ)' : '';
      cell(sh,r,1,lbl,      {bg:bg3,fg:lfc,sz:9,bold:isEntry||lbl==='VWAP',h:22});
      cell(sh,r,2,fxFn(lp), {bg:bg3,fg:'#e0e0e0',sz:11,bold:isEntry||lbl==='VWAP'});
      cell(sh,r,3,(ls>=0?'+':'')+ls.toFixed(1)+'σ',{bg:bg3,fg:lfc,sz:10,bold:true});
      cell(sh,r,4,ldesc,    {bg:bg3,fg:isEntry?'#66aa66':'#404050',sz:8});
      cell(sh,r,5,posNote,  {bg:bg3,fg:isCur?'#ffea00':'',sz:9,bold:isCur}); r++;
    });
    // Current price summary
    var inLongZone  = dir===1  && curD>=-1.0 && curD<=0.3;
    var inShortZone = dir===-1 && curD>=-0.3 && curD<=1.0;
    var inZone = inLongZone || inShortZone;
    mrow(sh,r,5,'Price dist from VWAP: '+(curD>=0?'+':'')+Math.round(curD*100)/100+'σ   '+(inZone?'✓ IN ENTRY ZONE':'✗ OUT OF ENTRY ZONE')+
      '   |   1σ = $'+fxFn(sd)+'   |   Entry zone: $'+fxFn(dir===1?vw-sd:vw-0.3*sd)+' – $'+fxFn(dir===1?vw+0.3*sd:vw+sd),
      {bg:inZone?'#0a1a0a':'#1a0a0a',fg:inZone?'#00e676':'#ff5252',sz:9,bold:true,h:22}); r++;
  } else {
    var vMsg2 = 'VWAP builds from session open — pre-session or bar fetch failed';
    mrow(sh,r,5,vMsg2,{bg:'#060c10',fg:'#404050',sz:9,h:22*8}); r++;
  }
  r++;

  // ── Score meter
  var scoreDisp = (sym==='GC=F')
    ? (macro.score>=0?'+':'')+macro.score.toFixed(1)+'%  (raw '+macro.raw+'/5)'
    : (macro.raw>=0?'+':'')+macro.raw+'/'+(sym==='CL=F'?7:5);
  var scoreMax  = sym==='CL=F'?7:5;
  var scoreOkC  = sym==='GC=F' ? '#888888' : (Math.abs(macro.raw)>=2?'#00e676':'#ff5252');
  mrow(sh,r,5,'MACRO SCORE  '+scoreDisp+'  |  Direction: '+(dir===1?'▲ LONG':'▼ SHORT')+'  '+macro.strength+
    (sym!=='GC=F'?'  |  Threshold: |raw| ≥ 2':'  |  Any majority fires direction'),
    {bg:headerBg,fg:scoreOkC,sz:10,bold:true,h:24}); r++;

  // ── Macro components
  mrow(sh,r,5,'COMPONENTS  (each = +1 bullish or −1 bearish — score = sum)',
    {bg:headerBg,fg:'#505050',sz:8,bold:true,h:18}); r++;
  (macro.components||[]).forEach(function(c){
    var cBg = c.val>0?'#0a1400':'#1a0800', cFg = c.val>0?'#00c853':'#ff5252';
    cell(sh,r,1,(c.val>0?'▲  ':'▼  ')+c.name,{bg:cBg,fg:cFg,sz:9,bold:true,h:24});
    sh.getRange(r,2,1,3).merge().setValue(c.note)
      .setBackground(cBg).setFontColor('#888888').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sh,r,5,c.val>0?'+1':'−1',{bg:cBg,fg:cFg,sz:11,bold:true}); r++;
  }); r++;

  // ── Gate checklist / trade levels
  mrow(sh,r,5,'GATE CHECKLIST  (all must be green to enter)',
    {bg:'#0a0e14',fg:'#3a5060',sz:9,bold:true,h:20}); r++;

  if (setup.action==='ACTIVE') {
    // All gates green — show full trade plan
    var fxP = sym==='HG=F' ? fix4 : fix2;
    (setup.checks||[]).forEach(function(c){
      mrow(sh,r,5,'✓  '+c.label+'  —  '+c.note,{bg:'#0a1400',fg:'#00c853',sz:9,h:24}); r++;
    });
    r++;
    mrow(sh,r,5,'⚡  ALL GATES GREEN  —  ENTER '+setup.dir+'  NOW  —  Execute in MT5',
      {bg:'#0a2800',fg:'#00ff88',sz:13,bold:true,h:40}); r++;
    ['ENTRY','STOP LOSS','TP1 (60%)','TP2 (40%)','SIZE ('+setup.unit+')'].forEach(function(h,i){
      cell(sh,r,i+1,h,{bg:'#060e06',fg:'#305030',sz:8,bold:true,h:18});
    }); r++;
    [[fxP(setup.entry),accentC],[fxP(setup.sl),'#ff5252'],
     [fxP(setup.tp1),'#00e676'],[fxP(setup.tp2),'#00bcd4'],
     [setup.size+' '+setup.unit,'#ffffff']].forEach(function(v,i){
      cell(sh,r,i+1,v[0],{bg:'#0a2800',fg:v[1],sz:14,bold:true,h:40});
    }); r++;
    cell(sh,r,1,'ATR $'+fxP(setup.atr),         {bg:'#040c04',fg:'#336633',sz:9,h:20});
    cell(sh,r,2,'SL −$'+fxP(setup.slPts),        {bg:'#040c04',fg:'#883333',sz:9,bold:true});
    cell(sh,r,3,'TP1 +$'+fxP(setup.tp1Pts)+' 1.5R',{bg:'#040c04',fg:'#338833',sz:9,bold:true});
    cell(sh,r,4,'TP2 +$'+fxP(setup.tp2Pts)+' 2.5R',{bg:'#040c04',fg:'#226688',sz:9,bold:true});
    cell(sh,r,5,setup.sz1+' + '+setup.sz2,        {bg:'#040c04',fg:'#666600',sz:9}); r++;
    // Contract size info for futures
    if (sym==='GC=F') {
      mrow(sh,r,5,'MT5: XAU/USD → Volume '+setup.size+' oz  |  Risk $'+setup.riskDollar+
        '  |  Move SL to breakeven after TP1 hit',
        {bg:'#040c04',fg:'#446644',sz:9,h:20}); r++;
    } else if (sym==='CL=F') {
      var microCL = Math.round(setup.size/100*10)/10;
      mrow(sh,r,5,'MT5: CL/USD → Volume '+setup.size+' bbl  |  micro-CL=100bbl → '+microCL+' contracts  |  Risk $'+setup.riskDollar,
        {bg:'#040c04',fg:'#446644',sz:9,h:20}); r++;
    } else {
      var microHG = Math.round(setup.size/2500*10)/10;
      mrow(sh,r,5,'MT5: HG (Copper) → Volume '+setup.size+' lbs  |  micro HG=2,500lbs → '+microHG+' contracts  |  Risk $'+setup.riskDollar,
        {bg:'#040c04',fg:'#446644',sz:9,h:20}); r++;
    }
    r++;
    // MT5 step instructions
    var d1 = setup.dir==='LONG';
    var steps = [
      ['STEP 1 — ENTER',     'Market order '+(d1?'BUY':'SELL')+' '+setup.size+' '+setup.unit+'  at $'+fxP(setup.entry)],
      ['STEP 2 — SET SL/TP', 'Immediately set SL = $'+fxP(setup.sl)+'  |  TP = $'+fxP(setup.tp1)+' (for '+setup.sz1+' '+setup.unit+' — TP1 portion)'],
      ['STEP 3 — TP1 HIT',   'Close '+setup.sz1+' '+setup.unit+' at $'+fxP(setup.tp1)+'  →  drag SL of remaining '+setup.sz2+' to $'+fxP(setup.entry)+' (breakeven)'],
      ['STEP 4 — TP2 HIT',   'Close remaining '+setup.sz2+' '+setup.unit+' at $'+fxP(setup.tp2)+'  →  complete. Log the trade.'],
      ['IF NO TP HIT',       'Exit ALL at session close. Never hold overnight. Loss capped at $'+setup.riskDollar+'.']
    ];
    steps.forEach(function(s){
      cell(sh,r,1,s[0],{bg:'#030904',fg:'#336655',sz:9,bold:true,h:26});
      sh.getRange(r,2,1,4).merge().setValue(s[1])
        .setBackground('#030904').setFontColor('#449966').setFontSize(10)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      r++;
    });

  } else if (setup.action==='SKIP') {
    mrow(sh,r,5,'🚫  BLOCKED  —  '+setup.reason,{bg:'#1a0800',fg:'#ff9800',sz:10,bold:true,h:30}); r++;

  } else if (setup.checks) {
    // Show sorted gate list (failing first)
    setup.checks.slice().sort(function(a,b){return (a.ok?1:0)-(b.ok?1:0);}).forEach(function(c){
      mrow(sh,r,5,(c.ok?'✓  ':'✗  ')+c.label+'   —   '+c.note,
        {bg:c.ok?'#0a1400':'#1a0800',fg:c.ok?'#00c853':'#ff5252',sz:9,h:26}); r++;
    });
    // Show VWAP entry zone
    if (setup.vwapZone && tech && tech.vwap > 0) {
      r++;
      mrow(sh,r,5,'VWAP ENTRY ZONE for '+(dir===1?'LONG':'SHORT')+':  $'+setup.vwapZone.lo+' – $'+setup.vwapZone.hi+
        '  (VWAP=$'+fxFn(tech.vwap)+'  ±1σ=$'+fxFn(tech.vwapStd)+')',
        {bg:'#0a1428',fg:'#00bcd4',sz:10,bold:true,h:26}); r++;
    }
  } else {
    mrow(sh,r,5,'⚠  '+( setup.reason||'Bar data unavailable — Yahoo Finance may be down. Retries on next 5-min refresh.'),
      {bg:'#1a0800',fg:'#ff9800',sz:9,h:26}); r++;
  }
  r++;

  // ── Key levels (EMAs + VWAP anchors)
  mrow(sh,r,5,'KEY LEVELS',{bg:headerBg,fg:'#505050',sz:9,bold:true,h:20}); r++;
  if (tech) {
    var klevels = [];
    if (sym==='GC=F' && tech.h4e50) {
      klevels.push(['4H EMA50','$'+fix2(tech.h4e50),'Short gate — shorts blocked above this level (major trend filter)']);
      klevels.push(['4H EMA9', '$'+fix2(tech.h4e9||0), 'Must be above EMA21 for LONG bias']);
      klevels.push(['4H EMA21','$'+fix2(tech.h4e21||0),'4H trend confirmation']);
    }
    klevels.push(['15m EMA9', '$'+fxFn(tech.ema9),  'Fast momentum — must align with bias direction']);
    klevels.push(['15m EMA21','$'+fxFn(tech.ema21), 'Slow momentum — must align with bias direction']);
    if (tech.vwap > 0) {
      klevels.push(['Session VWAP','$'+fxFn(tech.vwap), 'Mean-reversion anchor ('+tech.vwapBars+' bars since session open)']);
      klevels.push([dir===1?'LONG entry −1σ':'SHORT entry +1σ',
        '$'+fxFn(dir===1?tech.vwap-tech.vwapStd:tech.vwap+tech.vwapStd),
        dir===1?'Lower boundary — buy pullbacks here':'Upper boundary — sell pullbacks here']);
      klevels.push([dir===1?'LONG entry +0.3σ':'SHORT entry −0.3σ',
        '$'+fxFn(dir===1?tech.vwap+0.3*tech.vwapStd:tech.vwap-0.3*tech.vwapStd),
        'Far edge of entry zone — last acceptable entry']);
    }
    if (tech.atrDay) klevels.push(['Daily ATR','$'+fxFn(tech.atrDay),'Used for SL/TP sizing']);
    klevels.forEach(function(lv){
      cell(sh,r,1,lv[0],{bg:'#0e0e18',fg:'#607080',sz:9,bold:true,h:22});
      cell(sh,r,2,lv[1],{bg:'#0e0e18',fg:'#e0e0e0',sz:11,bold:true});
      sh.getRange(r,3,1,3).merge().setValue(lv[2])
        .setBackground('#0e0e18').setFontColor('#404450').setFontSize(9)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      r++;
    });
  }
  r++;

  // ── Daily schedule
  mrow(sh,r,5,'⏰  DAILY SCHEDULE  (Dubai / UTC)',{bg:headerBg,fg:'#505050',sz:9,bold:true,h:20}); r++;
  sched.forEach(function(line){
    var isEntry = line.indexOf('ENTRY WINDOW OPENS') !== -1;
    var isClose = line.indexOf('HARD CLOSE') !== -1;
    var fg3 = isClose?'#ff5252':isEntry?'#00e676':'#607d8b';
    mrow(sh,r,5,line,{bg:'#0a0a12',fg:fg3,sz:9,h:24}); r++;
  });
  r++;

  // ── Rules
  mrow(sh,r,5,'⛔  RULES YOU MUST NOT BREAK',{bg:'#1a0808',fg:'#cc4444',sz:9,bold:true,h:20}); r++;
  rules.forEach(function(rule){
    mrow(sh,r,5,'✗  '+rule,{bg:'#120606',fg:'#884444',sz:9,h:22}); r++;
  });

  return r;
}

// ═══════════════════════════════════════════════════════════════════════
// SCORE LOG  (appends every 5 min — historical score + price tracker)
// ═══════════════════════════════════════════════════════════════════════
function appendScoreLog(sh, now, auM, clM, hgM, auT, clT, hgT, auSS, clSS, hgSS) {
  var MNAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var DOW    = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

  // Set up headers if sheet is empty
  if (sh.getLastRow() === 0) {
    var headers = [
      'Timestamp UTC','Dubai Time','Day','Month',
      'Gold Price ($/oz)','Gold Raw (-5 to +5)','Gold Normalized Score','Gold Direction','Gold Strength','Gold Session',
      'Oil Price ($/bbl)','Oil Raw (-7 to +7)','Oil Direction','Oil Strength','Oil Session',
      'Copper Price ($/lb)','Copper Raw (-5 to +5)','Copper Direction','Copper Strength','Copper Session',
      'VIX'
    ];
    sh.getRange(1,1,1,headers.length).setValues([headers])
      .setBackground('#111120').setFontColor('#8888cc').setFontWeight('bold').setFontSize(9);
    sh.setFrozenRows(1);
    // Column widths
    [140,100,50,50, 110,100,110,80,80,80, 110,80,80,80,80, 110,100,80,80,80, 60].forEach(function(w,i){
      sh.setColumnWidth(i+1,w);
    });
  }

  var utcStr = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd HH:mm');
  var dxbStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm');
  var auP = auT ? auT.price : (auM.price||0);
  var clP = clT ? clT.price : (clM.price||0);
  var hgP = hgT ? hgT.price : (hgM.price||0);

  var newRow = [
    utcStr, dxbStr, DOW[now.getUTCDay()], MNAMES[now.getUTCMonth()],
    auP ? Math.round(auP*100)/100 : '', auM.raw||0, auM.score||0, auM.dir===1?'LONG':'SHORT', auM.strength||'', auSS||'',
    clP ? Math.round(clP*100)/100 : '', clM.raw||0, clM.dir===1?'LONG':'SHORT', clM.strength||'', clSS||'',
    hgP ? Math.round(hgP*10000)/10000 : '', hgM.raw||0, hgM.dir===1?'LONG':'SHORT', hgM.strength||'', hgSS||'',
    auM.vix||clM.vix||hgM.vix||''
  ];

  var nextRow = sh.getLastRow() + 1;
  sh.getRange(nextRow, 1, 1, newRow.length).setValues([newRow]);

  // Color-code the direction cells
  var dirColsIdx = [8, 13, 18];  // 1-based: 8=AU dir, 13=CL dir, 18=HG dir
  dirColsIdx.forEach(function(col) {
    var val = sh.getRange(nextRow, col).getValue();
    sh.getRange(nextRow, col)
      .setBackground(val==='LONG'?'#0a1a0a':'#1a0a0a')
      .setFontColor(val==='LONG'?'#00c853':'#ff5252');
  });

  // Color-code score cells by magnitude
  [[6, auM.raw, 5],[12, clM.raw, 7],[17, hgM.raw, 5]].forEach(function(sc){
    var col=sc[0], raw=sc[1], maxS=sc[2];
    var ab = Math.abs(raw||0); var pct = ab/maxS;
    var fg = pct>=0.8?'#00e676':pct>=0.4?'#ffea00':'#ff5252';
    sh.getRange(nextRow,col).setBackground('#111118').setFontColor(fg).setFontSize(9);
  });

  // Freeze first row, auto-resize nothing (too slow)
  // Keep last 5000 rows max
  if (nextRow > 5002) {
    sh.deleteRows(2, nextRow - 5001);
  }
}

// ═══════════════════════════════════════════════════════════════════════
// MATH HELPERS
// ═══════════════════════════════════════════════════════════════════════
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
function getDailyAtr(bars1h) {
  if (!bars1h||bars1h.length<15) return null;
  var days={}, order=[];
  bars1h.forEach(function(b){
    var d=b.dt.substring(0,10);
    if(!days[d]){days[d]={h:b.h,l:b.l,c:b.c};order.push(d);}
    else{days[d].h=Math.max(days[d].h,b.h);days[d].l=Math.min(days[d].l,b.l);days[d].c=b.c;}
  });
  if(order.length<3) return null;
  var daily=order.map(function(d){return days[d];});
  var tr=daily.map(function(b,i){
    if(i===0) return b.h-b.l;
    return Math.max(b.h-b.l, Math.abs(b.h-daily[i-1].c), Math.abs(b.l-daily[i-1].c));
  });
  return emaArr(tr,14)[tr.length-1];
}
function resampleTo4H(bars1h) {
  var out=[],cur=null;
  (bars1h||[]).forEach(function(b){
    var dt=new Date(b.dt.replace(' ','T')+'Z');
    var slot=Math.floor(dt.getUTCHours()/4)*4;
    var key=dt.getUTCFullYear()+'-'+dt.getUTCMonth()+'-'+dt.getUTCDate()+'-'+slot;
    if(!cur||cur.key!==key){if(cur)out.push(cur);cur={key:key,o:b.o,h:b.h,l:b.l,c:b.c};}
    else{cur.h=Math.max(cur.h,b.h);cur.l=Math.min(cur.l,b.l);cur.c=b.c;}
  });
  if(cur)out.push(cur); return out;
}
function fix2(n){ return n!=null?(Math.round(n*100)/100).toFixed(2):'—'; }
function fix4(n){ return n!=null?(Math.round(n*10000)/10000).toFixed(4):'—'; }

// ── Cell helpers (5-column dashboard)
function cell(sh,row,col,val,opts){
  var o=opts||{};
  sh.getRange(row,col).setValue(val)
    .setBackground(o.bg||'#0d0d18').setFontColor(o.fg||'#d0d0e0')
    .setFontSize(o.sz||10).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if(o.h) sh.setRowHeight(row,o.h);
}
function mrow(sh,row,cols,val,opts){
  var o=opts||{};
  sh.getRange(row,1,1,cols).merge().setValue(val)
    .setBackground(o.bg||'#0d0d18').setFontColor(o.fg||'#d0d0e0')
    .setFontSize(o.sz||10).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sh.setRowHeight(row,o.h||28);
}
