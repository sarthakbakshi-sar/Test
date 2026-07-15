// ─────────────────────────────────────────────────────────────
//  Trading Scanner — Pure Google Apps Script
//  No Python, no GitHub Actions, no server needed.
//
//  SETUP (2 steps, do once):
//  1. Paste this into your Google Sheet → Extensions → Apps Script
//  2. Run setupTrigger() from the Run menu → authorise when prompted
//
//  Runs every 30 min, Mon–Fri 13:30–24:00 UTC automatically.
//  Logs every scan to the sheet. Sends Telegram every run.
// ─────────────────────────────────────────────────────────────

var SL_M = 1.5, TP_M = 2.5, RISK = 200;
var SESSION_START = 13.5; // 13:30 UTC

var ALL_TOKENS = [
  'DOT-USDT','APT-USDT','SUI-USDT','LINK-USDT','ENA-USDT',
  'AVAX-USDT','SEI-USDT','BNB-USDT','FIL-USDT','ADA-USDT','ATOM-USDT',
  'HBAR-USDT','DOGE-USDT','HYPE-USDT','TRX-USDT',
  'SOL-USDT','BTC-USDT','ETH-USDT','XRP-USDT','ZEC-USDT'
];

// ── trigger ────────────────────────────────────────────────────
// Run this once from the Run menu to schedule the scanner.

function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'runScanner') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('runScanner').timeBased().everyMinutes(30).create();
  Logger.log('Done — runScanner will fire every 30 min.');
}

// ── main ───────────────────────────────────────────────────────

function runScanner() {
  var now    = new Date();
  var hour   = now.getUTCHours() + now.getUTCMinutes() / 60;
  var wday   = now.getUTCDay(); // 0=Sun, 6=Sat
  var ts     = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd HH:mm') + ' UTC';

  if (wday === 0 || wday === 6) { Logger.log('Weekend — skip'); return; }
  if (hour < SESSION_START)     { Logger.log('Pre-session — skip'); return; }

  Logger.log('=== SCAN ' + ts + ' ===');

  // BTC regime
  var btcDf = fetchOKX('BTC-USDT', 220);
  if (!btcDf) { Logger.log('BTC fetch failed'); return; }
  calcIndicators(btcDf);
  var btcEma200 = calcEMA(btcDf.c, 200);
  var btcPrice  = last(btcDf.c);
  var btcE200   = last(btcEma200);
  var btcBull   = btcPrice > btcE200;
  Logger.log('BTC ' + (btcBull ? 'BULL' : 'BEAR') + ' $' + Math.round(btcPrice) + ' EMA200 $' + Math.round(btcE200));

  var signals = [];

  // Scan alts
  for (var i = 0; i < ALL_TOKENS.length; i++) {
    var sym = ALL_TOKENS[i];
    try {
      var df = fetchOKX(sym, 150);
      if (!df) continue;
      calcIndicators(df);
      var n     = df.c.length;
      var rsi   = df.rsi[n-1],   rsiP  = df.rsi[n-2];
      var vd    = df.vd[n-1];
      var atr   = df.atr[n-1],   avg   = df.atrAvg[n-1];
      var price = df.c[n-1],     high  = df.h[n-1];
      var vol   = df.vol[n-1],   vavg  = df.volAvg[n-1];
      var res12 = df.res12[n-1];
      var ab    = df.ema20[n-1] > df.ema50[n-1];

      if (!isFinite(atr) || !isFinite(avg) || avg === 0) continue;
      if (atr / avg < 0.5 || atr / avg > 1.8) continue;
      var vr   = vavg > 0 ? vol / vavg : 0;
      var slL  = price - SL_M * atr, tpL = price + TP_M * atr;
      var slS  = price + SL_M * atr, tpS = price - TP_M * atr;

      if (!btcBull && vd >= 0.018 && vd <= 0.03 && rsiP >= 58 && rsiP <= 72 && rsi < rsiP) {
        signals.push({sym:sym, side:'SHORT', price:price, sl:slS, tp:tpS,
                      rsi:rsi, rsi_prev:rsiP, vwap_dev:vd, vol_ratio:vr,
                      note:'BTC BEAR | VWAP fade | 55.4% WR'});
      }
      if (btcBull && ab && res12 !== null && high > res12
          && vr >= 1.5 && vr <= 3.0 && rsi >= 50 && rsi <= 58 && vd >= 0 && vd <= 0.005) {
        signals.push({sym:sym, side:'LONG', price:price, sl:slL, tp:tpL,
                      rsi:rsi, rsi_prev:rsiP, vwap_dev:vd, vol_ratio:vr,
                      note:'BTC BULL | 12H breakout | 52.5% WR'});
      }
      Utilities.sleep(100);
    } catch(e) { Logger.log(sym + ': ' + e); }
  }

  // Gold
  var gCtx = {};
  try {
    var dfg = fetchYahoo('GC=F');
    if (dfg) {
      calcIndicators(dfg);
      var ng     = dfg.c.length;
      var gEma200 = calcEMA(dfg.c, 200);
      var gBull200 = dfg.c[ng-1] > gEma200[ng-1];
      var gRsi   = dfg.rsi[ng-1], gRsiP = dfg.rsi[ng-2];
      var gVd    = dfg.vd[ng-1];
      var gAtr   = dfg.atr[ng-1], gAvg = dfg.atrAvg[ng-1];
      var gAb    = dfg.ema20[ng-1] > dfg.ema50[ng-1];
      var gRes12 = dfg.res12[ng-1], gHigh = dfg.h[ng-1];
      var gVol   = dfg.vol[ng-1],  gVavg = dfg.volAvg[ng-1];
      var gPrice = dfg.c[ng-1],    gVr   = gVavg > 0 ? gVol / gVavg : 0;
      gCtx = {g_price:gPrice, g_rsi:gRsi, g_vd:gVd, g_trend: gAb ? 'uptrend' : 'downtrend'};
      Logger.log('GOLD $' + gPrice.toFixed(1) + ' RSI=' + gRsi.toFixed(1) + ' VWAP=' + (gVd*100).toFixed(2) + '% ' + gCtx.g_trend);

      var gAtrOk = isFinite(gAtr) && isFinite(gAvg) && gAvg > 0 && gAtr/gAvg >= 0.5 && gAtr/gAvg <= 1.8;
      if (gAtrOk) {
        var gSl = gPrice - SL_M*gAtr, gTp = gPrice + TP_M*gAtr;
        if (gAb && gRsiP <= 35 && gRsi > gRsiP && gVd <= -0.005) {
          signals.push({sym:'GOLD', side:'LONG', price:gPrice, sl:gSl, tp:gTp,
                        rsi:gRsi, rsi_prev:gRsiP, vwap_dev:gVd, vol_ratio:0,
                        note:'Gold VWAP pullback | 58.3% WR'});
        }
        if (gBull200 && gAb && gRes12 !== null && gHigh > gRes12
            && gVr >= 1.5 && gVr <= 3.0 && gRsi >= 50 && gRsi <= 62 && gVd >= 0 && gVd <= 0.008) {
          signals.push({sym:'GOLD', side:'LONG', price:gPrice, sl:gSl, tp:gTp,
                        rsi:gRsi, rsi_prev:gRsiP, vwap_dev:gVd, vol_ratio:gVr,
                        note:'Gold breakout | 58.3% WR'});
        }
      }
    }
  } catch(e) { Logger.log('GOLD error: ' + e); }

  var ctx = {btc_regime: btcBull ? 'BULL' : 'BEAR', btc_price: btcPrice, btc_ema200: btcE200};
  for (var k in gCtx) ctx[k] = gCtx[k];

  logToSheet(signals, ts, ctx);
  sendTelegram(signals, ts, ctx);
  Logger.log(signals.length > 0 ? signals.length + ' signal(s) sent.' : 'No signal.');
}

// ── data fetching ──────────────────────────────────────────────

function fetchOKX(sym, n) {
  var url  = 'https://www.okx.com/api/v5/market/history-candles';
  var bars = [], after = '';
  for (var attempt = 0; bars.length < n && attempt < 3; attempt++) {
    try {
      var qs   = '?instId=' + sym + '&bar=1H&limit=100' + (after ? '&after=' + after : '');
      var data = JSON.parse(UrlFetchApp.fetch(url + qs, {muteHttpExceptions:true}).getContentText()).data;
      if (!data || !data.length) break;
      bars = bars.concat(data);
      after = data[data.length-1][0];
      Utilities.sleep(100);
    } catch(e) { Utilities.sleep(1000); }
  }
  if (!bars.length) return null;
  bars = bars.slice(0, n).reverse();
  var ts=[],o=[],h=[],l=[],c=[],vol=[];
  bars.forEach(function(b){
    ts.push(+b[0]); o.push(+b[1]); h.push(+b[2]);
    l.push(+b[3]);  c.push(+b[4]); vol.push(+b[5]);
  });
  return {ts:ts,o:o,h:h,l:l,c:c,vol:vol};
}

function fetchYahoo(sym) {
  try {
    var url  = 'https://query1.finance.yahoo.com/v8/finance/chart/' + sym + '?interval=1h&range=30d';
    var resp = UrlFetchApp.fetch(url, {headers:{'User-Agent':'Mozilla/5.0'}, muteHttpExceptions:true});
    var r    = JSON.parse(resp.getContentText()).chart.result[0];
    var q    = r.indicators.quote[0];
    var ts=[],o=[],h=[],l=[],c=[],vol=[];
    r.timestamp.forEach(function(t,i){
      if (q.close[i]==null) return;
      ts.push(t*1000); o.push(q.open[i]||0); h.push(q.high[i]||0);
      l.push(q.low[i]||0); c.push(q.close[i]); vol.push(q.volume[i]||0);
    });
    return {ts:ts,o:o,h:h,l:l,c:c,vol:vol};
  } catch(e) { Logger.log('Yahoo: '+e); return null; }
}

// ── indicators ─────────────────────────────────────────────────

function last(arr) { return arr[arr.length-1]; }

function calcEMA(arr, span) {
  var k = 2/(span+1), out = [arr[0]];
  for (var i=1;i<arr.length;i++) out.push(arr[i]*k + out[i-1]*(1-k));
  return out;
}

function calcIndicators(df) {
  var c=df.c, h=df.h, l=df.l, vol=df.vol, ts=df.ts, n=c.length;

  // RSI
  var rsi=new Array(n).fill(50), ag=0, al=0;
  for (var i=1;i<n;i++){
    var d=c[i]-c[i-1], g=d>0?d:0, ls=d<0?-d:0;
    if(i<14){ag=(ag*(i-1)+g)/i; al=(al*(i-1)+ls)/i;}
    else     {ag=(ag*13+g)/14;   al=(al*13+ls)/14;}
    rsi[i]=al===0?100:100-100/(1+ag/al);
  }
  df.rsi=rsi;

  // EMA20 / EMA50
  df.ema20=calcEMA(c,20); df.ema50=calcEMA(c,50);

  // ATR + 20-bar avg
  var atr=new Array(n).fill(0), atrAvg=new Array(n).fill(NaN);
  for (var i=1;i<n;i++){
    var tr=Math.max(h[i]-l[i],Math.abs(h[i]-c[i-1]),Math.abs(l[i]-c[i-1]));
    atr[i]=i<14?(atr[i-1]*(i-1)+tr)/i:(atr[i-1]*13+tr)/14;
  }
  for (var i=5;i<n;i++){
    var w=Math.min(i+1,20),s=0;
    for(var j=i-w+1;j<=i;j++) s+=atr[j];
    atrAvg[i]=s/w;
  }
  df.atr=atr; df.atrAvg=atrAvg;

  // VWAP dev (daily reset)
  var vd=new Array(n).fill(NaN), cumTV=0, cumV=0, prevDay=-1;
  for (var i=0;i<n;i++){
    var d=new Date(ts[i]);
    var day=d.getUTCFullYear()*10000+d.getUTCMonth()*100+d.getUTCDate();
    if(day!==prevDay){cumTV=0;cumV=0;prevDay=day;}
    var tp=(h[i]+l[i]+c[i])/3;
    cumTV+=tp*vol[i]; cumV+=vol[i];
    var v=cumV>0?cumTV/cumV:NaN;
    vd[i]=v&&v!==0?(c[i]-v)/v:NaN;
  }
  df.vd=vd;

  // 12H resistance (rolling max of prev 12 highs)
  var res12=new Array(n).fill(null);
  for (var i=13;i<n;i++){
    var mx=-Infinity;
    for(var j=i-12;j<i;j++) mx=Math.max(mx,h[j]);
    res12[i]=mx;
  }
  df.res12=res12;

  // 20-bar volume average
  var volAvg=new Array(n).fill(NaN);
  for (var i=9;i<n;i++){
    var w=Math.min(i+1,20),s=0;
    for(var j=i-w+1;j<=i;j++) s+=vol[j];
    volAvg[i]=s/w;
  }
  df.volAvg=volAvg;
}

// ── sheet logging ──────────────────────────────────────────────

function logToSheet(signals, ts, ctx) {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('Scanner Log');
  if (!sheet) {
    sheet = ss.insertSheet('Scanner Log');
    var hdr = ['Timestamp (UTC)','BTC Regime','BTC Price','EMA200',
               'Gold Price','Gold RSI','Gold VWAP%','Gold Trend','Signals','Count'];
    sheet.appendRow(hdr);
    sheet.getRange(1,1,1,hdr.length).setFontWeight('bold')
         .setBackground('#222222').setFontColor('#ffffff');
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1,165); sheet.setColumnWidth(9,440);
  }

  var sigLines = signals.map(function(s){
    var slP=(Math.abs(s.price-s.sl)/s.price*100).toFixed(1);
    var tpP=(Math.abs(s.tp-s.price)/s.price*100).toFixed(1);
    return s.side+' '+s.sym+' $'+s.price.toFixed(4)+
           ' SL:$'+s.sl.toFixed(4)+'(-'+slP+'%)'+
           ' TP:$'+s.tp.toFixed(4)+'(+'+tpP+'%)'+
           ' RSI:'+s.rsi_prev.toFixed(1)+'→'+s.rsi.toFixed(1)+
           ' VWAP:'+(s.vwap_dev*100).toFixed(2)+'%';
  });

  var row=[
    ts,
    ctx.btc_regime||'',
    ctx.btc_price  ? Math.round(ctx.btc_price)  : '',
    ctx.btc_ema200 ? Math.round(ctx.btc_ema200) : '',
    ctx.g_price    ? (+ctx.g_price).toFixed(1)  : '',
    ctx.g_rsi      !=null ? (+ctx.g_rsi).toFixed(1)     : '',
    ctx.g_vd       !=null ? (ctx.g_vd*100).toFixed(2)+'%' : '',
    ctx.g_trend    ||'',
    sigLines.length ? sigLines.join(' | ') : 'No signal',
    signals.length
  ];

  var nr=sheet.getLastRow()+1;
  sheet.appendRow(row);
  var rc=sheet.getRange(nr,2);
  if (ctx.btc_regime==='BULL') rc.setBackground('#d9ead3').setFontColor('#274e13');
  else if (ctx.btc_regime==='BEAR') rc.setBackground('#f4cccc').setFontColor('#7f0000');
  if (signals.length>0) sheet.getRange(nr,1,1,row.length).setBackground('#fff2cc');
}

// ── telegram ───────────────────────────────────────────────────

function sendTelegram(signals, ts, ctx) {
  var token  = '8601364508:AAGn9nHsjkvc6UfS6hiRSQ_uL-5VTJPncZc';
  var chatId = '1346945081';
  ctx     = ctx     || {};
  signals = signals || [];

  var bull  = ctx.btc_regime === 'BULL';
  var bIcon = bull ? '🟢' : '🔴';
  var text;

  if (signals.length > 0) {
    text  = '🚨 *SIGNAL — ' + ts + '*\n';
    text += bIcon + ' BTC ' + (ctx.btc_regime||'') + ' $' + Math.round(ctx.btc_price||0).toLocaleString() + '\n\n';
    signals.forEach(function(s){
      var icon  = s.side==='SHORT' ? '🔴' : '🟢';
      var slP   = (Math.abs(s.price-s.sl)/s.price*100).toFixed(1);
      var tpP   = (Math.abs(s.tp-s.price)/s.price*100).toFixed(1);
      var volStr= s.vol_ratio ? ' Vol '+s.vol_ratio.toFixed(1)+'x' : '';
      text += icon+' *'+s.side+' '+s.sym+'*\n';
      text += 'Entry `$'+s.price.toFixed(4)+'`\n';
      text += 'SL `$'+s.sl.toFixed(4)+'` (−'+slP+'%)  TP `$'+s.tp.toFixed(4)+'` (+'+tpP+'%)\n';
      text += 'RSI '+s.rsi_prev.toFixed(1)+'→'+s.rsi.toFixed(1)+
              '  VWAP '+(s.vwap_dev*100).toFixed(2)+'%'+volStr+'\n';
      text += '_'+s.note+'_\n\n';
    });
    text += 'SL='+SL_M+'×ATR · TP='+TP_M+'×ATR · Risk $'+RISK+'/trade';
  } else {
    var goldLine = ctx.g_price
      ? '\nGold $'+Math.round(ctx.g_price).toLocaleString()+' RSI '+Number(ctx.g_rsi).toFixed(0)+' | '+(ctx.g_trend||'?')+' | VWAP '+(ctx.g_vd!=null?(ctx.g_vd*100).toFixed(2)+'%':'?')
      : '';
    text  = '⏳ *No signal — '+ts+'*\n';
    text += bIcon+' BTC $'+Math.round(ctx.btc_price||0).toLocaleString();
    text += ' vs EMA200 $'+Math.round(ctx.btc_ema200||0).toLocaleString();
    text += goldLine+'\n'+(bull ? 'LONG mode' : 'SHORT mode');
  }

  UrlFetchApp.fetch('https://api.telegram.org/bot'+token+'/sendMessage', {
    method:'post', contentType:'application/json',
    payload: JSON.stringify({chat_id:chatId, text:text, parse_mode:'Markdown'}),
    muteHttpExceptions: true
  });
}
