// ─────────────────────────────────────────────────────────────
//  Trading Scanner — Google Apps Script
//  Paste this entire file into Tools → Apps Script in your sheet
//  Deploy as Web App: Execute as Me, Anyone can access
// ─────────────────────────────────────────────────────────────

var SHEET_NAME = 'Scanner Log';

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    logToSheet(data);
    sendTelegram(data);
    return ContentService
      .createTextOutput(JSON.stringify({ status: 'ok' }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: 'error', msg: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function logToSheet(data) {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    var hdr = ['Timestamp (UTC)', 'BTC Regime', 'BTC Price', 'EMA200',
               'Gold Price', 'Gold RSI', 'Gold VWAP%', 'Gold Trend',
               'Signals', 'Signal Count'];
    sheet.appendRow(hdr);
    sheet.getRange(1, 1, 1, hdr.length).setFontWeight('bold').setBackground('#222222').setFontColor('#ffffff');
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1, 160);
    sheet.setColumnWidth(9, 400);
  }

  var signals  = data.signals || [];
  var sigLines = signals.map(function (s) {
    var slPct = (Math.abs(s.price - s.sl) / s.price * 100).toFixed(1);
    var tpPct = (Math.abs(s.tp - s.price) / s.price * 100).toFixed(1);
    return s.side + ' ' + s.sym +
           ' Entry:$' + s.price.toFixed(4) +
           ' SL:$' + s.sl.toFixed(4) + '(-' + slPct + '%)' +
           ' TP:$' + s.tp.toFixed(4) + '(+' + tpPct + '%)' +
           ' RSI:' + s.rsi_prev.toFixed(1) + '→' + s.rsi.toFixed(1) +
           ' VWAP:' + (s.vwap_dev * 100).toFixed(2) + '%';
  });

  var row = [
    data.ts        || new Date().toISOString(),
    data.btc_regime || '',
    data.btc_price  || '',
    data.btc_ema200 || '',
    data.g_price    || '',
    data.g_rsi      != null ? Number(data.g_rsi).toFixed(1)  : '',
    data.g_vd       != null ? (data.g_vd * 100).toFixed(2) + '%' : '',
    data.g_trend    || '',
    sigLines.length > 0 ? sigLines.join(' | ') : 'No signal',
    signals.length
  ];

  var newRow = sheet.getLastRow() + 1;
  sheet.appendRow(row);

  // Colour-code by regime
  var regimeCell = sheet.getRange(newRow, 2);
  if (data.btc_regime === 'BULL') {
    regimeCell.setBackground('#d9ead3').setFontColor('#274e13');
  } else if (data.btc_regime === 'BEAR') {
    regimeCell.setBackground('#f4cccc').setFontColor('#7f0000');
  }

  // Highlight signal rows
  if (signals.length > 0) {
    sheet.getRange(newRow, 1, 1, row.length).setBackground('#fff2cc');
  }
}

function sendTelegram(data) {
  var props  = PropertiesService.getScriptProperties();
  var token  = props.getProperty('TG_TOKEN');
  var chatId = props.getProperty('TG_CHAT_ID');
  if (!token || !chatId) {
    Logger.log('TG_TOKEN / TG_CHAT_ID not set in Script Properties');
    return;
  }

  var signals = data.signals || [];
  var ts      = data.ts || '';
  var btcIcon = data.btc_regime === 'BULL' ? '🟢' : '🔴';
  var text;

  if (signals.length > 0) {
    text = '🚨 *SIGNAL — ' + ts + '*\n';
    text += btcIcon + ' BTC ' + data.btc_regime + ' $' + Math.round(data.btc_price).toLocaleString() + '\n\n';
    signals.forEach(function (s) {
      var icon   = s.side === 'SHORT' ? '🔴' : '🟢';
      var slPct  = (Math.abs(s.price - s.sl) / s.price * 100).toFixed(1);
      var tpPct  = (Math.abs(s.tp - s.price) / s.price * 100).toFixed(1);
      var volStr = s.vol_ratio ? ' Vol ' + s.vol_ratio.toFixed(1) + 'x' : '';
      text += icon + ' *' + s.side + ' ' + s.sym + '*\n';
      text += 'Entry `$' + s.price.toFixed(4) + '`\n';
      text += 'SL `$' + s.sl.toFixed(4) + '` (−' + slPct + '%)  TP `$' + s.tp.toFixed(4) + '` (+' + tpPct + '%)\n';
      text += 'RSI ' + s.rsi_prev.toFixed(1) + '→' + s.rsi.toFixed(1) +
              '  VWAP ' + (s.vwap_dev * 100).toFixed(2) + '%' + volStr + '\n';
      text += '_' + (s.note || '') + '_\n\n';
    });
    text += 'SL=' + data.sl_m + '×ATR · TP=' + data.tp_m + '×ATR · Risk $' + data.risk + '/trade';
  } else {
    // Brief no-signal status update
    var goldLine = data.g_price
      ? '  Gold $' + Math.round(data.g_price).toLocaleString() + ' RSI ' + Number(data.g_rsi).toFixed(0)
      : '';
    var mode = data.btc_regime === 'BULL' ? 'LONG mode' : 'SHORT mode';
    text = '⏳ *No signal — ' + ts + '*\n';
    text += btcIcon + ' BTC $' + Math.round(data.btc_price).toLocaleString() +
            ' vs EMA200 $' + Math.round(data.btc_ema200).toLocaleString() + '\n';
    text += goldLine + '\n' + mode;
  }

  var url     = 'https://api.telegram.org/bot' + token + '/sendMessage';
  var payload = { chat_id: chatId, text: text, parse_mode: 'Markdown' };
  UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
}
