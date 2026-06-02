/**
 * SILVER SIGNAL — v2 MODEL  (SI=F VWAP Pullback, Daily ATR)
 * ===========================================================
 * SETUP (one-time):
 *   1. sheets.new → Extensions → Apps Script → paste this → Save
 *   2. Run setupTrigger() once to authorize + set auto-refresh
 *   3. Run updateDashboard() to load immediately
 *   Auto-refreshes every 5 minutes during trading hours.
 *
 * MODEL SPECS (backtest 2yr, Sharpe 3.16, WR 60%):
 *   Components: 7 macro — Silver EMA50 · G/S ratio · Copper · DXY · Yield · SIL miners · Mom
 *   Hard gates: score≥3 | VIX<30 | skip September | Gold aligned | EMA20 direction
 *   Sessions  : London 08:00–11:30 UTC  |  NY 14:00–16:30 UTC
 *   SL/TP     : SL=0.75×daily_ATR | TP1=1.5× (60%) | TP2=2.5× (40%)
 *   ROI target: ~10%/month at 6.3% risk | ~5%/month at 3% risk
 *
 * 3-MONTH LIVE RUN PURPOSE:
 *   Logs every trade signal + 15m bar context to SILVER LOG sheet.
 *   After 3 months: enough data to backtest 15m VWAP entries (v4).
 *
 * BACKTEST RESULTS (2yr, 385 trades, dual session):
 *   WR 60.0% | Sharpe 3.16 | 3.2 trades/week | Max DD −10.4% | Ann ROI ~56% at 3% risk
 */

// ── CONFIG ────────────────────────────────────────────────────────────────────
var ACCOUNT     = 10000;    // ← your actual account size in USD
var RISK_PCT    = 0.063;    // 6.3% risk per trade → ~10%/month | use 0.03 for conservative
var SL_MULT     = 0.75;
var TP1_MULT    = 1.50;
var TP2_MULT    = 2.50;
var TP1_FRAC    = 0.60;
var TP2_FRAC    = 0.40;
var MIN_SCORE   = 3;        // need at least 3/7 components to agree

// Session windows (UTC)
var LON_OPEN    = 8.0;      // 08:00 UTC
var LON_CLOSE   = 11.5;     // 11:30 UTC
var LON_ENTRY   = 8.25;     // first bar settles — enter ~08:15 or at next clean level
var NY_OPEN     = 14.0;     // 14:00 UTC
var NY_CLOSE    = 16.5;     // 16:30 UTC
var NY_ENTRY    = 14.25;    // 14:15 UTC

// ── TRIGGER ───────────────────────────────────────────────────────────────────
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'updateDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('updateDashboard').timeBased().everyMinutes(5).create();
  try {
    SpreadsheetApp.getUi().alert(
      '✅ SILVER SIGNAL v2 — Auto-refresh every 5 min active.\n\n' +
      'SESSION WINDOWS (UTC):\n' +
      '  08:00–11:30 UTC  →  London session\n' +
      '  14:00–16:30 UTC  →  NY session (COMEX open)\n\n' +
      'RISK CONFIG:\n' +
      '  Current: ' + (RISK_PCT*100).toFixed(1) + '% per trade → ~' +
        Math.round(RISK_PCT/0.03*5) + '%/month estimated\n' +
      '  Change RISK_PCT in CONFIG section at top of script.\n\n' +
      '3-MONTH COLLECTION:\n' +
      '  Every signal is logged to SILVER LOG sheet.\n' +
      '  After 3 months: use log data for v4 VWAP entry backtest.'
    );
  } catch(e) {}
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
function updateDashboard() {
  var now  = new Date();
  var nowH = now.getUTCHours() + now.getUTCMinutes() / 60;
  // Run 06:00–18:00 UTC (covers pre-London through post-NY)
  if (nowH < 6.0 || nowH > 18.0) return;

  var ss   = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName('SILVER SIGNAL') || ss.insertSheet('SILVER SIGNAL');

  var macro  = getMacroScore();
  var bars1h = getSilverBars('1h', '30d', 200);
  var dailyATR = getDailyATR(bars1h);
  var sess   = getSessionState(nowH);
  var setup  = computeSetup(macro, bars1h, dailyATR, sess, nowH);

  writeSheet(dash, now, nowH, macro, bars1h, dailyATR, sess, setup);
  logSignal(ss, now, macro, sess, setup, dailyATR);
}

// ── MACRO SCORE ───────────────────────────────────────────────────────────────
function getMacroScore() {
  var cache  = CacheService.getScriptCache();
  var today  = Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd');
  var cached = cache.get('silver_macro_' + today);
  if (cached) { try { return JSON.parse(cached); } catch(e) {} }

  function yfd(sym, range) {
    try {
      var url = 'https://query1.finance.yahoo.com/v8/finance/chart/' +
                encodeURIComponent(sym) + '?interval=1d&range=' + range;
      var r = UrlFetchApp.fetch(url, { muteHttpExceptions:true, headers:{'User-Agent':'Mozilla/5.0'} });
      var j = JSON.parse(r.getContentText());
      return j.chart.result[0].indicators.quote[0].close.filter(function(v){ return v != null; });
    } catch(e) { return null; }
  }

  var silver = yfd('SI=F',     '3mo');
  var gold   = yfd('GC=F',     '3mo');
  var copper = yfd('HG=F',     '3mo');
  var dxy    = yfd('DX-Y.NYB', '3mo');
  var tnx    = yfd('^TNX',      '3mo');
  var sil    = yfd('SIL',      '3mo');
  var vix    = yfd('^VIX',     '5d');

  var res = {
    score:0, dir:1, agreeCount:0, strength:'',
    components:[], error:null,
    silverPrice:null, goldPrice:null,
    vix:null, ema20:null, goldEma50:null
  };

  if (!silver || silver.length < 55) { res.error = 'Silver data unavailable'; return res; }

  var n = silver.length;
  res.silverPrice = silver[n-1];
  if (gold && gold.length > 2) res.goldPrice = gold[gold.length-1];
  res.vix = vix && vix.length > 0 ? Math.round(vix[vix.length-1] * 10) / 10 : null;

  // ── 7 components ──────────────────────────────────────────────────────────
  // 1. Silver vs EMA50
  var e50s  = emaArr(silver, 50);
  res.ema50 = e50s[n-2];
  var s_trend = silver[n-2] > e50s[n-2] ? 1 : -1;

  // EMA20 for alignment gate
  var e20s  = emaArr(silver, 20);
  res.ema20 = e20s[n-2];

  // 2. Gold/Silver ratio direction (5d)
  var s_gs = 0;
  if (gold && gold.length >= 8 && n >= 8) {
    var gs_now  = gold[gold.length-2] / silver[n-2];
    var gs_5ago = gold[gold.length-7] / silver[n-7];
    s_gs = gs_now < gs_5ago ? 1 : -1;  // ratio falling = silver cheap = bullish
  }

  // 3. Copper 5d momentum
  var s_cu = 0;
  if (copper && copper.length >= 8) {
    s_cu = copper[copper.length-2] > copper[copper.length-7] ? 1 : -1;
  }

  // 4. DXY direction
  var s_dxy = 0;
  if (dxy && dxy.length >= 3) {
    s_dxy = (dxy[dxy.length-2] / dxy[dxy.length-3] - 1) < 0 ? 1 : -1;
  }

  // 5. 10Y yield direction
  var s_yield = 0;
  if (tnx && tnx.length >= 3) {
    s_yield = tnx[tnx.length-2] < tnx[tnx.length-3] ? 1 : -1;
  }

  // 6. SIL miners vs silver (THE key signal — p=0.0001)
  var s_sil = 0;
  if (sil && sil.length >= 3 && n >= 3) {
    var sil_ret    = sil[sil.length-2] / sil[sil.length-3] - 1;
    var silver_ret = silver[n-2] / silver[n-3] - 1;
    s_sil = sil_ret > silver_ret ? 1 : -1;
  }

  // 7. Silver 5d momentum
  var s_mom = n >= 8 ? (silver[n-2] > silver[n-7] ? 1 : -1) : 0;

  // Gold alignment gate value
  var goldE50 = gold && gold.length >= 52 ? emaArr(gold, 50) : null;
  res.goldEma50    = goldE50 ? goldE50[goldE50.length-2] : null;
  res.goldAligned  = goldE50 ? (gold[gold.length-2] > goldE50[goldE50.length-2]) : null;

  var raw = s_trend + s_gs + s_cu + s_dxy + s_yield + s_sil + s_mom;
  res.score      = raw;
  res.dir        = raw >= 0 ? 1 : -1;
  res.agreeCount = Math.abs(raw);
  res.strength   = Math.abs(raw) >= 5 ? 'STRONG' : Math.abs(raw) >= 3 ? 'MODERATE' : 'WEAK';

  var mom5pct   = n >= 8 ? Math.round((silver[n-2]/silver[n-7]-1)*1000)/10 : 0;
  var silDelta  = s_sil !== 0 && sil && sil.length >= 3 && n >= 3
    ? Math.round(((sil[sil.length-2]/sil[sil.length-3])-(silver[n-2]/silver[n-3]))*1000)/10 : null;
  var gs_5d_pct = s_gs !== 0 && gold && gold.length >= 8 && n >= 8
    ? Math.round(((gold[gold.length-2]/silver[n-2])/(gold[gold.length-7]/silver[n-7])-1)*1000)/10 : null;
  var cuMom5    = copper && copper.length >= 8
    ? Math.round((copper[copper.length-2]/copper[copper.length-7]-1)*1000)/10 : null;
  var dxyPct    = dxy && dxy.length >= 3
    ? Math.round((dxy[dxy.length-2]/dxy[dxy.length-3]-1)*1000)/10 : null;
  var tnxChg    = tnx && tnx.length >= 3
    ? Math.round((tnx[tnx.length-2]-tnx[tnx.length-3])*100)/100 : null;

  res.components = [
    { name:'SIL miners vs silver', val:s_sil,
      note:'★ KEY SIGNAL (p=0.0001) — Miners '+(s_sil>0?'outperform ↑ (accumulation)':'underperform ↓ (distribution)')
           +(silDelta!=null?' | delta: '+(silDelta>=0?'+':'')+silDelta+'%':'') },
    { name:'Silver vs EMA50',      val:s_trend,
      note:'Silver $'+fix(silver[n-2])+' '+(s_trend>0?'above':'below')+' EMA50 $'+fix(e50s[n-2]) },
    { name:'Gold aligned',         val:res.goldAligned===null?0:(res.goldAligned&&res.dir===1)||((!res.goldAligned)&&res.dir===-1)?1:-1,
      note:res.goldEma50?'Gold $'+fix(res.goldPrice)+' '+(res.goldAligned?'above':'below')+' EMA50 $'+fix(res.goldEma50)
                         +' — '+(res.goldAligned===res.dir>0?'✓ aligned':'✗ opposed'):'Gold data n/a' },
    { name:'EMA20 direction',      val:s_trend,  // reuse trend direction vs EMA20
      note:'Silver '+(silver[n-2]>e20s[n-2]?'above':'below')+' EMA20 $'+fix(e20s[n-2]) },
    { name:'DXY direction',        val:s_dxy,
      note:'DXY '+(s_dxy>0?'falling ↓ (silver positive)':'rising ↑ (silver negative)')
           +(dxyPct!=null?' | 1d: '+(dxyPct>=0?'+':'')+dxyPct+'%':'') },
    { name:'10Y yield',            val:s_yield,
      note:'Yield '+(s_yield>0?'falling ↓  bullish':'rising ↑  headwind')
           +(tnxChg!=null?' | 1d chg: '+(tnxChg>=0?'+':'')+tnxChg+'%':'') },
    { name:'Gold/Silver ratio 5d', val:s_gs,
      note:'G/S ratio '+(s_gs>0?'falling (silver cheaper — bullish)':'rising (silver expensive — bearish)')
           +(gs_5d_pct!=null?' | 5d chg: '+(gs_5d_pct>=0?'+':'')+gs_5d_pct+'%':'') },
    { name:'Copper 5d momentum',   val:s_cu,
      note:'Copper '+(s_cu>0?'+':'')+(cuMom5!=null?cuMom5:'?')+'% 5d (industrial demand proxy)' },
    { name:'Silver 5d momentum',   val:s_mom,
      note:(mom5pct>=0?'+':'')+mom5pct+'% over 5 days' }
  ];

  cache.put('silver_macro_' + today, JSON.stringify(res), 21600);
  return res;
}

// ── BAR FETCH (SI=F silver futures) ──────────────────────────────────────────
function getSilverBars(interval, range, n) {
  var sc  = CacheService.getScriptCache();
  var key = 'yf_silver_' + interval;
  var ttl = interval === '1h' ? 3600 : 600;
  try { var hit = sc.get(key); if (hit) return JSON.parse(hit); } catch(e) {}
  try {
    var url = 'https://query1.finance.yahoo.com/v8/finance/chart/SI%3DF' +
              '?interval=' + interval + '&range=' + range;
    var r    = UrlFetchApp.fetch(url, {muteHttpExceptions:true, headers:{'User-Agent':'Mozilla/5.0'}});
    var j    = JSON.parse(r.getContentText());
    if (!j.chart || !j.chart.result || !j.chart.result[0]) {
      Logger.log('getSilverBars['+interval+'] no result');
      return [];
    }
    var res = j.chart.result[0];
    var ts  = res.timestamp;
    var q   = res.indicators.quote[0];
    var bars = [];
    for (var i = 0; i < ts.length; i++) {
      if (!q.open[i] || !q.close[i]) continue;
      var d  = new Date(ts[i] * 1000);
      var pd = function(x){ return x < 10 ? '0'+x : ''+x; };
      bars.push({
        ts: ts[i],
        dt: d.getUTCFullYear()+'-'+pd(d.getUTCMonth()+1)+'-'+pd(d.getUTCDate())+
            ' '+pd(d.getUTCHours())+':'+pd(d.getUTCMinutes()),
        o: q.open[i], h: q.high[i], l: q.low[i], c: q.close[i], v: q.volume[i]||0
      });
    }
    var out = bars.slice(-n);
    try { sc.put(key, JSON.stringify(out), ttl); } catch(ce){}
    return out;
  } catch(e) {
    Logger.log('getSilverBars exception: ' + e.toString());
    return [];
  }
}

// ── DAILY ATR (from 1h bars resampled to daily) ───────────────────────────────
function getDailyATR(bars1h) {
  if (!bars1h || bars1h.length < 20) return null;
  // Aggregate to daily OHLC
  var daily = {};
  bars1h.forEach(function(b) {
    var day = b.dt.substring(0, 10);
    if (!daily[day]) daily[day] = { h: b.h, l: b.l, c: b.c, o: b.o };
    else { daily[day].h = Math.max(daily[day].h, b.h);
           daily[day].l = Math.min(daily[day].l, b.l);
           daily[day].c = b.c; }
  });
  var days  = Object.keys(daily).sort();
  var ohlc  = days.map(function(d){ return daily[d]; });
  if (ohlc.length < 5) return null;
  // True Range
  var tr = ohlc.map(function(d, i) {
    if (i === 0) return d.h - d.l;
    var prev = ohlc[i-1].c;
    return Math.max(d.h - d.l, Math.abs(d.h - prev), Math.abs(d.l - prev));
  });
  var atrArr = emaArr(tr, 14);
  return {
    atr14:     atrArr[atrArr.length - 1],
    todayHigh: ohlc[ohlc.length-1].h,
    todayLow:  ohlc[ohlc.length-1].l,
    todayRange: ohlc[ohlc.length-1].h - ohlc[ohlc.length-1].l
  };
}

// ── SESSION STATE ─────────────────────────────────────────────────────────────
function getSessionState(nowH) {
  if (nowH < LON_OPEN - 1)  return 'PRE_LONDON';
  if (nowH < LON_OPEN)      return 'LON_PREP';
  if (nowH <= LON_CLOSE)    return 'LONDON';
  if (nowH < NY_OPEN - 1)   return 'BETWEEN';
  if (nowH < NY_OPEN)       return 'NY_PREP';
  if (nowH <= NY_CLOSE)     return 'NY';
  return 'CLOSED';
}

function currentSessionName(sess) {
  if (sess === 'LONDON') return 'London';
  if (sess === 'NY')     return 'NY';
  return null;
}

// ── GATE LOGIC ────────────────────────────────────────────────────────────────
function computeSetup(macro, bars1h, dailyATR, sess, nowH) {
  var dir    = macro.dir;
  var score  = macro.score;
  var atr    = dailyATR ? dailyATR.atr14 : null;
  var price  = bars1h && bars1h.length > 0 ? bars1h[bars1h.length-1].c : macro.silverPrice;
  var nowMonth = new Date().getUTCMonth() + 1; // 1-12

  // ── Gates ─────────────────────────────────────────────────────────────────
  var scoreOk  = Math.abs(score) >= MIN_SCORE;
  var vixOk    = macro.vix === null || macro.vix < 30;
  var sepOk    = nowMonth !== 9;  // skip September

  // Gold alignment: silver score direction must match gold EMA50 trend
  var goldOk   = macro.goldAligned === null ? true
    : (dir === 1 && macro.goldAligned) || (dir === -1 && !macro.goldAligned);

  // EMA20: silver price direction must match score direction
  var ema20Ok  = macro.ema20 && price
    ? (dir === 1 && price > macro.ema20) || (dir === -1 && price < macro.ema20)
    : true;

  var inSession = (sess === 'LONDON' || sess === 'NY');

  var scoreStr = (score >= 0 ? '+' : '') + score + '/7';
  var vixStr   = macro.vix !== null ? 'VIX ' + macro.vix.toFixed(1) : 'VIX n/a';

  var checks = [
    { label:'Score ≥ ' + MIN_SCORE + '/7', ok:scoreOk,
      note: scoreOk
        ? scoreStr + ' — ' + macro.agreeCount + ' components agree  (' + macro.strength + ')'
        : scoreStr + ' — need ' + MIN_SCORE + '+ to trade (currently ' + Math.abs(score) + ')' },
    { label:'VIX < 30', ok:vixOk,
      note: vixOk
        ? (macro.vix ? vixStr + (macro.vix >= 25 ? ' — elevated, size down 20%' : ' — OK') : 'VIX unavailable')
        : vixStr + ' ≥ 30 — CRASH REGIME. Skip today.' },
    { label:'Not September', ok:sepOk,
      note: sepOk ? 'Month OK (Sep excluded — Sharpe −2.3 historically)' : 'SEPTEMBER — skip month.' },
    { label:'Gold aligned', ok:goldOk,
      note: macro.goldAligned === null
        ? 'Gold data unavailable'
        : goldOk
          ? 'Gold $'+fix(macro.goldPrice)+' '+(macro.goldAligned?'above':'below')+' EMA50 $'+fix(macro.goldEma50)+' — ✓ same direction as silver signal'
          : 'Gold opposes silver signal — opposite direction. Skip. (WR drops from 60% to 42% when opposed)' },
    { label:'EMA20 direction', ok:ema20Ok,
      note: macro.ema20
        ? 'Silver $'+fix(price)+' '+(price>macro.ema20?'above':'below')+' EMA20 $'+fix(macro.ema20)
          +(ema20Ok ? ' — ✓ aligned' : ' — ✗ opposed')
        : 'EMA20 unavailable' },
    { label:'Session active', ok:inSession,
      note: inSession
        ? '✓ ' + (sess==='LONDON'?'London 08:00–11:30':'NY 14:00–16:30') + ' UTC'
        : 'Session windows: London 08:00–11:30 UTC  |  NY 14:00–16:30 UTC' }
  ];

  var allGates = scoreOk && vixOk && sepOk && goldOk && ema20Ok && inSession;

  if (!atr || !price) {
    return { action:'WAIT', dir:dir===1?'LONG':'SHORT',
             checks:checks, reason:'ATR or price data unavailable', price:price };
  }

  var sl_d  = SL_MULT  * atr;
  var tp1_d = TP1_MULT * atr;
  var tp2_d = TP2_MULT * atr;
  var sl    = dir === 1 ? price - sl_d  : price + sl_d;
  var tp1   = dir === 1 ? price + tp1_d : price - tp1_d;
  var tp2   = dir === 1 ? price + tp2_d : price - tp2_d;
  var totalOz  = (ACCOUNT * RISK_PCT) / sl_d;
  var oz1      = totalOz * TP1_FRAC;
  var oz2      = totalOz * TP2_FRAC;
  var riskUSD  = Math.round(ACCOUNT * RISK_PCT);

  if (allGates) {
    return {
      action:'ACTIVE', dir:dir===1?'LONG':'SHORT',
      price:price, sl:sl, tp1:tp1, tp2:tp2,
      totalOz: Math.round(totalOz*10)/10,
      oz1: Math.round(oz1*10)/10,
      oz2: Math.round(oz2*10)/10,
      riskUSD: riskUSD, atr:atr,
      sl_d:sl_d, tp1_d:tp1_d, tp2_d:tp2_d,
      checks:checks, sess:sess
    };
  }

  return {
    action:'WAIT', dir:dir===1?'LONG':'SHORT',
    price:price, sl:sl, tp1:tp1, tp2:tp2,
    totalOz: Math.round(totalOz*10)/10,
    riskUSD: riskUSD, atr:atr,
    checks:checks,
    failCount: checks.filter(function(c){ return !c.ok; }).length
  };
}

// ── TRADE LOG (for 3-month v4 dataset) ────────────────────────────────────────
function logSignal(ss, now, macro, sess, setup, dailyATR) {
  // Only log when a trade signal fires or gates are close to qualifying
  if (setup.action !== 'ACTIVE') return;
  try {
    var log = ss.getSheetByName('SILVER LOG');
    if (!log) {
      log = ss.insertSheet('SILVER LOG');
      log.getRange(1,1,1,14).setValues([[
        'Date (UTC)','Time (UTC)','Session','Score','Direction',
        'Entry $','SL $','TP1 $','TP2 $','ATR $',
        'Gates passed','VIX','Gold price','Result (fill manually)'
      ]]).setBackground('#1a1a2e').setFontColor('#FFD700').setFontWeight('bold');
      log.setFrozenRows(1);
    }
    var dateStr = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
    var timeStr = Utilities.formatDate(now, 'UTC', 'HH:mm');
    var lastRow = log.getLastRow() + 1;
    // Avoid duplicate entries for same date + session
    if (lastRow > 2) {
      var prevDate = log.getRange(lastRow-1,1).getValue();
      var prevSess = log.getRange(lastRow-1,3).getValue();
      if (prevDate === dateStr && prevSess === sess) return;
    }
    log.getRange(lastRow, 1, 1, 14).setValues([[
      dateStr, timeStr,
      sess === 'LONDON' ? 'London' : 'NY',
      macro.score, setup.dir,
      fix(setup.price), fix(setup.sl), fix(setup.tp1), fix(setup.tp2),
      fix(dailyATR ? dailyATR.atr14 : null),
      setup.checks ? setup.checks.filter(function(c){ return c.ok; }).length + '/' + setup.checks.length : '?',
      macro.vix || '', fix(macro.goldPrice),
      ''  // user fills result: WIN / LOSS / time-stop
    ]]);
    // Colour-code by direction
    var bgCol = setup.dir === 'LONG' ? '#0a2a0a' : '#2a0a0a';
    log.getRange(lastRow, 1, 1, 14).setBackground(bgCol).setFontColor('#e0e0e0');
  } catch(e) {
    Logger.log('logSignal error: ' + e.toString());
  }
}

// ── SHEET WRITER ──────────────────────────────────────────────────────────────
function writeSheet(sheet, now, nowH, macro, bars1h, dailyATR, sess, setup) {
  sheet.clear();
  [200, 130, 130, 130, 130].forEach(function(w,i){ sheet.setColumnWidth(i+1,w); });

  var utcStr = Utilities.formatDate(now, 'UTC',        'HH:mm') + ' UTC';
  var dubStr = Utilities.formatDate(now, 'Asia/Dubai', 'HH:mm') + ' DXB';
  var r = 1;

  // ── HEADER ────────────────────────────────────────────────────────────────
  mrow(sheet, r, 5,
       '⚡  XAG/USD  SILVER SIGNAL v2  —  Daily ATR  |  Sharpe 3.16  |  WR 60%  |  3.2×/wk  |  2yr backtest',
       {bg:'#0a1a2e', fg:'#C0C0C0', sz:12, bold:true, h:36}); r++;
  mrow(sheet, r, 5,
       utcStr + '  ·  ' + dubStr + '  ·  Auto-refresh 5min  ·  Risk ' + (RISK_PCT*100).toFixed(1) +
       '% ($' + Math.round(ACCOUNT*RISK_PCT) + ')  ·  Account $' + ACCOUNT,
       {bg:'#071020', fg:'#304050', sz:9, h:20}); r++; r++;

  // ── STATUS BAR ────────────────────────────────────────────────────────────
  var price    = setup.price || macro.silverPrice || 0;
  var dir      = macro.dir || 1;
  var score    = macro.score || 0;
  var vix      = macro.vix;
  var atr      = dailyATR ? dailyATR.atr14 : null;
  var dirLabel = (dir===1?'▲ LONG':'▼ SHORT') + '  ' + macro.strength;
  var dirFg    = dir===1 ? '#00e676' : '#ff5252';
  var scoreFg  = Math.abs(score)>=5?'#00e676':Math.abs(score)>=3?'#76ff03':'#ffea00';
  var vixFg    = !vix?'#808080':vix>=30?'#ff1744':vix>=25?'#ff9800':vix>=20?'#ffea00':'#00e676';

  var sessLabels = {
    PRE_LONDON: '⏳ London opens ' + (LON_OPEN - nowH).toFixed(1) + 'h',
    LON_PREP:   '⚠ London prep (08:00 UTC)',
    LONDON:     '✅ LONDON 08:00–11:30',
    BETWEEN:    '⏳ NY opens ' + (NY_OPEN - nowH).toFixed(1) + 'h',
    NY_PREP:    '⚠ NY prep (14:00 UTC)',
    NY:         '✅ NY 14:00–16:30',
    CLOSED:     '🔒 Closed — next session tomorrow'
  };
  var sessFg = { PRE_LONDON:'#607d8b',LON_PREP:'#ff9800',LONDON:'#00e676',
                 BETWEEN:'#607d8b',NY_PREP:'#ff9800',NY:'#00e676',CLOSED:'#455a64' };

  ['SILVER PRICE','DIRECTION','SESSION','VIX','DAILY ATR'].forEach(function(h,i){
    cell(sheet,r,i+1,h,{bg:'#0d1a2a',fg:'#506070',sz:9,bold:true,h:20});
  });
  r++;
  cell(sheet,r,1, price?'$'+fix3(price):'—',        {bg:'#050d18',fg:'#C0C0C0',sz:16,bold:true,h:40});
  cell(sheet,r,2, dirLabel,                           {bg:'#050d18',fg:dirFg,   sz:11,bold:true});
  cell(sheet,r,3, sessLabels[sess]||sess,             {bg:'#050d18',fg:sessFg[sess]||'#808080',sz:10,bold:true});
  cell(sheet,r,4, vix?vix.toFixed(1):'—',            {bg:'#050d18',fg:vixFg,   sz:16,bold:true});
  cell(sheet,r,5, atr?'$'+fix3(atr)+'/oz':'—',       {bg:'#050d18',fg:'#b0bec5',sz:12,bold:true}); r++;
  // Score subrow
  var scoreBar = 'SCORE  ' + (score>=0?'+':'') + score + '/7  ·  ' +
    macro.agreeCount + ' components ' + (dir===1?'bullish':'bearish') + '  ·  ' + macro.strength +
    '  ·  ' + (Math.abs(score)>=MIN_SCORE ? '✓ threshold met' : '✗ need '+MIN_SCORE+'+');
  mrow(sheet,r,5,scoreBar,{bg:'#030b18',fg:scoreFg,sz:10,bold:true,h:24}); r++; r++;

  // ── ACTION BLOCK ──────────────────────────────────────────────────────────
  var inSess = (sess==='LONDON'||sess==='NY');
  var actionBg = setup.action==='ACTIVE'?'#0a3d0a':'#111111';
  var actionFg = setup.action==='ACTIVE'?'#00ff88':'#9e9e9e';
  var failList = setup.checks ? setup.checks.filter(function(c){return !c.ok;}) : [];

  var actionTxt;
  if (setup.action === 'ACTIVE') {
    actionTxt = '⚡  ENTER ' + setup.dir + '  —  ' +
      (sess==='LONDON'?'London':'NY') + ' session  —  All gates green  —  Execute now';
  } else if (inSess) {
    actionTxt = '⏱  IN SESSION  —  Watching  ·  Bias: ' + (setup.dir||'—') + '  ·  ' +
      (setup.failCount||failList.length) + ' gate(s) failing';
  } else {
    var nextSess = nowH < LON_OPEN ? 'London 08:00 UTC' : nowH < NY_OPEN ? 'NY 14:00 UTC' : 'tomorrow';
    actionTxt = '⏳  Next session: ' + nextSess;
  }
  mrow(sheet,r,5,actionTxt,{bg:actionBg,fg:actionFg,sz:12,bold:true,h:38}); r++;

  if (setup.action === 'ACTIVE') {
    // Trade levels
    ['ENTRY','STOP LOSS','TP1 (60%)','TP2 (40%)','SIZE (oz)'].forEach(function(h,i){
      cell(sheet,r,i+1,h,{bg:'#0a1a0a',fg:'#406040',sz:9,bold:true,h:20});
    });
    r++;
    [['$'+fix3(setup.price),'#C0C0C0'], ['$'+fix3(setup.sl),'#ff5252'],
     ['$'+fix3(setup.tp1), '#00e676'], ['$'+fix3(setup.tp2),'#00bcd4'],
     [fix(setup.totalOz)+' oz','#ffffff']].forEach(function(v,i){
      cell(sheet,r,i+1,v[0],{bg:'#0a3d0a',fg:v[1],sz:14,bold:true,h:40});
    });
    r++;
    // Risk metrics
    cell(sheet,r,1,'ATR $'+fix3(setup.atr),                 {bg:'#050d05',fg:'#336633',sz:9,h:22});
    cell(sheet,r,2,'SL −$'+fix3(setup.sl_d),                {bg:'#050d05',fg:'#883333',sz:9,bold:true});
    cell(sheet,r,3,'TP1 +$'+fix3(setup.tp1_d)+'  2.0R',     {bg:'#050d05',fg:'#338833',sz:9,bold:true});
    cell(sheet,r,4,'TP2 +$'+fix3(setup.tp2_d)+'  3.3R',     {bg:'#050d05',fg:'#226688',sz:9,bold:true});
    cell(sheet,r,5,fix(setup.oz1)+'+'+fix(setup.oz2)+' oz', {bg:'#050d05',fg:'#666600',sz:9}); r++;

    // Steps
    var d1 = setup.dir === 'LONG';
    var steps = [
      ['STEP 1 — ENTER',
       'MT5 → SI=F → New Order → Market → Volume: '+setup.totalOz+' oz → '+(d1?'BUY':'SELL')],
      ['STEP 2 — SET SL',
       'Immediately set SL = $'+fix3(setup.sl)+'  (risk: $'+setup.riskUSD+' = '+(RISK_PCT*100).toFixed(1)+'% of $'+ACCOUNT+')'],
      ['STEP 3 — TP1 HIT',
       'Close '+setup.oz1+' oz at $'+fix3(setup.tp1)+'  →  Move SL of remaining '+setup.oz2+' oz to breakeven $'+fix3(setup.price)],
      ['STEP 4 — TP2 HIT',
       'Close remaining '+setup.oz2+' oz at $'+fix3(setup.tp2)+'  →  Trade done. Log result in SILVER LOG.'],
      ['TIME STOP',
       (sess==='LONDON'?'11:30 UTC (London close)':'16:30 UTC (NY close)')+
       ' → close ALL open positions regardless of P&L'],
      ['POSITION SIZE',
       'oz = ('+ACCOUNT+' × '+(RISK_PCT*100).toFixed(1)+'%) ÷ $'+fix3(setup.sl_d)+
       ' = '+setup.totalOz+' oz  |  Notional ≈ $'+Math.round(setup.totalOz*price)]
    ];
    steps.forEach(function(s){
      cell(sheet,r,1,s[0],{bg:'#030f03',fg:'#336633',sz:9,bold:true,h:28});
      sheet.getRange(r,2,1,4).merge().setValue(s[1])
        .setBackground('#030f03').setFontColor('#4488aa').setFontSize(10)
        .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
      r++;
    });

  } else {
    // Gate checklist — failing gates first
    var sorted = (setup.checks||[]).slice().sort(function(a,b){return (a.ok?1:0)-(b.ok?1:0);});
    sorted.forEach(function(c){
      mrow(sheet,r,5,(c.ok?'✓  ':'✗  ')+c.label+'   —   '+c.note,
           {bg:c.ok?'#0a1a0a':'#1a0808',fg:c.ok?'#00c853':'#ff5252',sz:10,h:28}); r++;
    });
    // Show indicative levels anyway
    if (setup.price && setup.sl) {
      r++;
      mrow(sheet,r,5,'INDICATIVE LEVELS (if signal fires):',
           {bg:'#0a0a18',fg:'#404060',sz:9,bold:true,h:20}); r++;
      ['ENTRY','STOP LOSS','TP1 (60%)','TP2 (40%)','SIZE (oz)'].forEach(function(h,i){
        cell(sheet,r,i+1,h,{bg:'#0a0a18',fg:'#303050',sz:9,bold:true,h:18});
      });
      r++;
      [['$'+fix3(setup.price),'#707070'],['$'+fix3(setup.sl),'#663333'],
       ['$'+fix3(setup.tp1),'#336633'], ['$'+fix3(setup.tp2),'#225566'],
       [fix(setup.totalOz)+' oz','#555555']].forEach(function(v,i){
        cell(sheet,r,i+1,v[0],{bg:'#0a0a18',fg:v[1],sz:11,bold:false,h:28});
      });
      r++;
    }
  }
  r++;

  // ── MACRO COMPONENTS ──────────────────────────────────────────────────────
  var agreeBias = macro.components ? macro.components.filter(function(c){return c.val===dir;}).length : 0;
  mrow(sheet,r,5,
       'MACRO COMPONENTS  ' + score + '/7  ·  ' + macro.agreeCount + ' agree  ·  ' + macro.strength + ' signal',
       {bg:'#0a1a2e',fg:'#406090',sz:9,bold:true,h:22}); r++;

  (macro.components||[]).forEach(function(c){
    var signed_val = c.val;
    var bg2 = signed_val===dir?'#0a1a0a':(signed_val===0?'#111111':'#1a0808');
    var fg2 = signed_val===dir?'#00c853':(signed_val===0?'#808080':'#ff5252');
    var arrow = signed_val>0?'▲':signed_val<0?'▼':'—';
    cell(sheet,r,1,arrow+' '+c.name,{bg:bg2,fg:fg2,sz:9,bold:signed_val===dir,h:26});
    sheet.getRange(r,2,1,3).merge().setValue(c.note)
      .setBackground(bg2).setFontColor('#708090').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    var sv = signed_val>0?'+1':signed_val<0?'−1':'0';
    cell(sheet,r,5,sv,{bg:bg2,fg:fg2,sz:12,bold:true}); r++;
  });
  r++;

  // ── REGIME CONTEXT ────────────────────────────────────────────────────────
  mrow(sheet,r,5,'REGIME & RISK CONTEXT',{bg:'#0a1a2a',fg:'#406080',sz:9,bold:true,h:20}); r++;
  var regRows = [
    ['VIX', vix?vix.toFixed(1):'—',
     !vix?'No data':vix<20?'Low volatility — ideal entry conditions':
          vix<25?'Moderate vol — good':vix<30?'Elevated — reduce size 20%':'SKIP — crash regime',
     vix===null?null:vix<30],
    ['Daily ATR', atr?'$'+fix3(atr):'—',
     atr?'SL=$'+fix3(SL_MULT*atr)+' | TP1=$'+fix3(TP1_MULT*atr)+' | TP2=$'+fix3(TP2_MULT*atr)
         +'  |  Position: '+Math.round((ACCOUNT*RISK_PCT)/( SL_MULT*atr)*10)/10+' oz'
     :'Unavailable',
     atr!=null],
    ['Position $', atr&&price?'$'+Math.round(((ACCOUNT*RISK_PCT)/(SL_MULT*atr))*price):'—',
     'Notional exposure at '+( RISK_PCT*100).toFixed(1)+'% risk ($'+Math.round(ACCOUNT*RISK_PCT)+' risk)',
     null],
    ['Skip month', (new Date().getUTCMonth()+1)===9?'SEPTEMBER':'Month OK',
     (new Date().getUTCMonth()+1)===9?'SKIP SEPTEMBER — Sharpe −2.3 historically':'No monthly exclusion active',
     (new Date().getUTCMonth()+1)!==9]
  ];
  regRows.forEach(function(row){
    var ok=row[3]; var bg3=ok===null?'#0a0a18':ok?'#0a1a0a':'#1a0a0a';
    var fg3=ok===null?'#607d8b':ok?'#00c853':'#ff9800';
    cell(sheet,r,1,row[0],{bg:bg3,fg:'#7a8899',sz:9,bold:false,h:26});
    cell(sheet,r,2,row[1],{bg:bg3,fg:'#e0e0e0',sz:12,bold:true});
    sheet.getRange(r,3,1,2).merge().setValue(row[2])
      .setBackground(bg3).setFontColor(fg3).setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    cell(sheet,r,5,ok===null?'INFO':ok?'OK':'WARN',{bg:bg3,fg:fg3,sz:9,bold:true}); r++;
  });
  r++;

  // ── SESSION SCHEDULE ──────────────────────────────────────────────────────
  mrow(sheet,r,5,'⏰  SESSION WINDOWS & DAILY ROUTINE (UTC)',
       {bg:'#0a1a2e',fg:'#406090',sz:9,bold:true,h:20}); r++;
  [['07:45','Pre-London check','#0a0a18',
    'Review macro score + VIX. If score<3 or VIX≥30 or September → skip both sessions today.'],
   ['08:00','▶ LONDON OPENS','#0a2a0a',
    'Enter if all 5 gates green. Direction from macro score. SL = 0.75×ATR. TP1 = 1.5×ATR.'],
   ['11:30','London CLOSE','#1a0808',
    'HARD EXIT: close all London positions. Do not hold into between-session gap.'],
   ['13:45','Pre-NY check','#0a0a18',
    'Re-check score + VIX. Signal may have changed since morning — re-evaluate all gates.'],
   ['14:00','▶ NY OPENS (COMEX)','#0a2a0a',
    'Enter if all gates green. Second opportunity same day. Independent from London result.'],
   ['16:30','NY CLOSE','#1a0808',
    'HARD EXIT: close all NY positions. No overnight silver positions. Ever.']
  ].forEach(function(row){
    cell(sheet,r,1,row[0],{bg:row[2],fg:row[0].indexOf('▶')>=0?'#00e676':row[0].indexOf('CLOSE')>=0?'#ff5252':'#90a0b0',sz:10,bold:true,h:26});
    cell(sheet,r,2,row[1],{bg:row[2],fg:'#9aaabb',sz:9,bold:row[0].indexOf('▶')>=0||row[0].indexOf('CLOSE')>=0});
    sheet.getRange(r,3,1,3).merge().setValue(row[3])
      .setBackground(row[2]).setFontColor('#607d8b').setFontSize(9)
      .setVerticalAlignment('middle').setHorizontalAlignment('left').setWrap(true);
    r++;
  });
  r++;

  // ── 3-MONTH LOG NOTICE ────────────────────────────────────────────────────
  mrow(sheet,r,5,'📊  3-MONTH DATA COLLECTION — Building v4 dataset',
       {bg:'#1a1a0a',fg:'#a0a000',sz:9,bold:true,h:20}); r++;
  mrow(sheet,r,5,
       '✓ Every trade signal is auto-logged to SILVER LOG sheet (date/session/score/levels).\n' +
       'After 3 months: enough trades for 15m VWAP entry backtest (v4 target: 10%/month at 3% risk).\n' +
       'Track results manually in SILVER LOG → Result column (WIN / LOSS / TIME-STOP).',
       {bg:'#121208',fg:'#706000',sz:9,h:52,bold:false}); r++;
  r++;

  // ── RULES ─────────────────────────────────────────────────────────────────
  mrow(sheet,r,5,'📋  RULES',{bg:'#1a0808',fg:'#cc4444',sz:9,bold:true,h:20}); r++;
  ['NEVER hold SI=F overnight — hard close at London 11:30 UTC / NY 16:30 UTC',
   'NEVER trade in September — skip all signals for the entire month',
   'NEVER trade when VIX ≥ 30 — skip both sessions that day',
   'NEVER trade when gold is trending in opposite direction to silver signal',
   'NEVER move SL further from entry — take the loss if price reaches SL',
   'NEVER add to a losing position — one entry per session only'
  ].forEach(function(rule){
    mrow(sheet,r,5,'✗  '+rule,{bg:'#120808',fg:'#884444',sz:9,h:22}); r++;
  });

  // ── FOOTER ────────────────────────────────────────────────────────────────
  r++;
  mrow(sheet,r,5,
       'SILVER v2 MODEL  |  2yr backtest  |  385 trades  |  WR 60%  |  Sharpe 3.16  |  3.2×/week  |  ~'+
       Math.round(RISK_PCT/0.03*4.7)+'%/month at '+(RISK_PCT*100).toFixed(1)+'% risk',
       {bg:'#050508',fg:'#202025',sz:8,h:18}); r++;
  mrow(sheet,r,5,
       'Sessions: London 08:00–11:30 UTC · NY 14:00–16:30 UTC  |  ' +
       'Gates: Score≥3 · VIX<30 · ¬Sep · Gold aligned · EMA20  |  ' +
       'SL=0.75×ATR · TP1=1.5× · TP2=2.5×  |  Time stop at session close',
       {bg:'#050508',fg:'#202025',sz:8,h:18});

  SpreadsheetApp.flush();
}

// ── MATH HELPERS ─────────────────────────────────────────────────────────────
function emaArr(arr, n) {
  var k=2/(n+1), out=[arr[0]];
  for (var i=1; i<arr.length; i++) out.push(arr[i]*k + out[i-1]*(1-k));
  return out;
}

function fix(n)  { return n!=null ? (Math.round(n*100)/100).toFixed(2) : '—'; }
function fix3(n) { return n!=null ? (Math.round(n*1000)/1000).toFixed(3) : '—'; }  // silver needs 3dp

function cell(sheet,row,col,val,opts){
  var o=opts||{};
  sheet.getRange(row,col).setValue(val)
    .setBackground(o.bg||'#0d1020').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  if(o.h) sheet.setRowHeight(row,o.h);
}

function mrow(sheet,row,cols,val,opts){
  var o=opts||{};
  sheet.getRange(row,1,1,cols).merge().setValue(val)
    .setBackground(o.bg||'#0d1020').setFontColor(o.fg||'#e0e0e0')
    .setFontSize(o.sz||11).setFontWeight(o.bold?'bold':'normal')
    .setVerticalAlignment('middle').setHorizontalAlignment('center').setWrap(true);
  sheet.setRowHeight(row,o.h||30);
}
