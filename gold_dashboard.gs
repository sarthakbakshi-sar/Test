/**
 * gold_dashboard.gs — Gold Trading Dashboard (GCM-3 + V4+)
 * ==========================================================
 * SETUP (run once):
 *   1. Create a new Google Sheet
 *   2. Extensions → Apps Script → paste this entire file
 *   3. Save (Ctrl+S)
 *   4. Run  setupDashboard()  from the Run menu (or the "Gold Dashboard" menu that appears)
 *   5. Approve permissions when prompted
 *
 * After setup:
 *   - GCM-3 refreshes automatically every 15 minutes
 *   - V4+ refreshes daily at 00:15 UTC
 *   - Email alert fires whenever a LONG or SHORT signal triggers
 *   - Use "Gold Dashboard" → "Refresh Now" to force an immediate update
 *
 * ABSOLUTE RULES (hardcoded):
 *   - GCM-3: JUNE = absolute skip, no trades, no exceptions
 *   - V4+:   June is NOT skipped (bidirectional, June 2026 = +13.89R)
 *   - Risk per trade: $300
 */

// ══════════════════════════════════════════════════════════════════════
// CONFIG
// ══════════════════════════════════════════════════════════════════════
const CFG = {
  SHEET_NAME:    'GOLD DASHBOARD',
  INST:          'XAU-USDT-SWAP',
  OKX_BASE:      'https://www.okx.com',
  RISK_PER_TRADE: 300,
  ALERT_EMAIL:   Session.getActiveUser().getEmail(),

  // ── GCM-3 ──────────────────────────────────────────────────────────
  GCM_SL:         2.0,
  GCM_TP1:        3.0,
  GCM_TP2:        5.0,
  GCM_TP1_PCT:    0.60,
  GCM_TP2_PCT:    0.40,
  LONDON_START:   7.0,
  LONDON_END:    12.0,
  NY_START:      13.5,
  NY_END:        20.0,
  LONDON_S_MIN:   3,
  LONDON_E_MIN:   2,
  NY_S_MIN:       2,
  NY_E_MIN:       1,
  M_MIN:          1,
  SHORT_M_MAX:   -2,
  SHORT_S_MAX:   -2,
  SHORT_E_MAX:   -2,
  VIX_GATE:      22.0,

  // ── V4+ ────────────────────────────────────────────────────────────
  V4_SL:          0.75,
  V4_TP1:         1.5,
  V4_TP2:         2.5,
  V4_TP1_PCT:     0.60,
  V4_TP2_PCT:     0.40,
  V4_MAX_HOLD:    5,
  V4_THRESHOLD:   5,
  LB_12M:       252,
  LB_6M:        126,
  LB_3M:         63,
};

// ══════════════════════════════════════════════════════════════════════
// MATHS HELPERS
// ══════════════════════════════════════════════════════════════════════

/** Exponential moving average (Pandas ewm equiv, adjust=False). */
function calcEMA(values, period) {
  const k = 2 / (period + 1);
  const out = new Array(values.length).fill(NaN);
  let sum = 0, count = 0, started = false;
  for (let i = 0; i < values.length; i++) {
    if (isNaN(values[i])) { out[i] = NaN; continue; }
    if (!started) {
      sum += values[i]; count++;
      // seed with SMA of first `period` values then switch to EMA
      if (count === period) {
        out[i] = sum / period;
        started = true;
      }
      continue;
    }
    out[i] = values[i] * k + out[i - 1] * (1 - k);
  }
  return out;
}

/** Wilder RSI(14). */
function calcRSI(closes, period) {
  const out = new Array(closes.length).fill(NaN);
  if (closes.length < period + 1) return out;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) avgGain += d; else avgLoss -= d;
  }
  avgGain /= period; avgLoss /= period;
  out[period] = 100 - 100 / (1 + avgGain / (avgLoss || 1e-9));
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = d > 0 ? d : 0;
    const l = d < 0 ? -d : 0;
    avgGain = (avgGain * (period - 1) + g) / period;
    avgLoss = (avgLoss * (period - 1) + l) / period;
    out[i] = 100 - 100 / (1 + avgGain / (avgLoss || 1e-9));
  }
  return out;
}

/** MACD: returns { macd[], signal[], hist[] }. */
function calcMACD(closes, fast, slow, sigPeriod) {
  const emaFast = calcEMA(closes, fast);
  const emaSlow = calcEMA(closes, slow);
  const macdLine = emaFast.map((v, i) => isNaN(v) || isNaN(emaSlow[i]) ? NaN : v - emaSlow[i]);
  const sigLine  = calcEMA(macdLine, sigPeriod);
  const hist     = macdLine.map((v, i) => isNaN(v) || isNaN(sigLine[i]) ? NaN : v - sigLine[i]);
  return { macd: macdLine, signal: sigLine, hist };
}

/** ATR(14). */
function calcATR(highs, lows, closes, period) {
  const tr  = new Array(closes.length).fill(NaN);
  const out = new Array(closes.length).fill(NaN);
  for (let i = 1; i < closes.length; i++) {
    const hl = highs[i] - lows[i];
    const hc = Math.abs(highs[i] - closes[i - 1]);
    const lc = Math.abs(lows[i] - closes[i - 1]);
    tr[i] = Math.max(hl, hc, lc);
  }
  let sum = 0, cnt = 0;
  for (let i = 1; i <= period && i < tr.length; i++) { sum += tr[i]; cnt++; }
  if (cnt < period) return out;
  out[period] = sum / period;
  for (let i = period + 1; i < tr.length; i++) {
    out[i] = (out[i - 1] * (period - 1) + tr[i]) / period;
  }
  return out;
}

/** Rolling N-bar SMA. */
function rollMean(arr, n) {
  const out = new Array(arr.length).fill(NaN);
  for (let i = n - 1; i < arr.length; i++) {
    let s = 0, cnt = 0;
    for (let j = i - n + 1; j <= i; j++) { if (!isNaN(arr[j])) { s += arr[j]; cnt++; } }
    if (cnt === n) out[i] = s / n;
  }
  return out;
}

// ══════════════════════════════════════════════════════════════════════
// DATA FETCHERS
// ══════════════════════════════════════════════════════════════════════

/**
 * Fetch OKX candles.  bar = '15m' | '4H' | '1D'.
 * Returns array of {ts, o, h, l, c, vol} sorted oldest→newest.
 */
function fetchOKX(bar, nBatches) {
  nBatches = nBatches || 4;
  const allBars = [];
  let afterTs = null;
  const headers = { 'User-Agent': 'gold-dashboard-gas/1.0' };

  for (let i = 0; i < nBatches; i++) {
    let url;
    if (afterTs === null) {
      url = `${CFG.OKX_BASE}/api/v5/market/candles?instId=${CFG.INST}&bar=${bar}&limit=300`;
    } else {
      url = `${CFG.OKX_BASE}/api/v5/market/history-candles?instId=${CFG.INST}&bar=${bar}&after=${afterTs}&limit=300`;
    }
    try {
      const resp = UrlFetchApp.fetch(url, { headers, muteHttpExceptions: true });
      const data = JSON.parse(resp.getContentText());
      const bars = data.data || [];
      if (bars.length === 0) break;
      for (const b of bars) {
        allBars.push({
          ts:  parseInt(b[0]),
          o:   parseFloat(b[1]),
          h:   parseFloat(b[2]),
          l:   parseFloat(b[3]),
          c:   parseFloat(b[4]),
          vol: parseFloat(b[5])
        });
      }
      afterTs = bars[bars.length - 1][0];
      Utilities.sleep(250);
    } catch (e) {
      Logger.log(`OKX ${bar} batch ${i}: ${e}`);
      break;
    }
  }

  // sort oldest→newest, remove dupes by ts
  allBars.sort((a, b) => a.ts - b.ts);
  const seen = new Set();
  return allBars.filter(b => {
    if (seen.has(b.ts)) return false;
    seen.add(b.ts); return true;
  });
}

/**
 * Fetch Treasury.gov CSV (TIPS or Nominal 10Y).
 * rateType: 'daily_treasury_real_yield_curve' | 'daily_treasury_yield_curve'
 * Returns array of {date: Date, val: number} sorted oldest→newest.
 */
function fetchTreasury(rateType) {
  const years = [2023, 2024, 2025, 2026];
  const rows = [];
  for (const yr of years) {
    const url = `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/${yr}/all?type=${rateType}&field_tdr_date_value=${yr}&download=true`;
    try {
      const resp = UrlFetchApp.fetch(url, {
        headers: { 'User-Agent': 'gold-dashboard-gas/1.0' },
        muteHttpExceptions: true
      });
      const text = resp.getContentText();
      const lines = text.split('\n').filter(l => l.trim());
      if (lines.length < 2) continue;
      const hdr = lines[0].split(',').map(h => h.trim().replace(/"/g, ''));
      // find the 10Y column
      const col10 = hdr.findIndex(h => h.includes('10 YR') || h.includes('10 Yr'));
      const dateCol = hdr.findIndex(h => h === 'Date');
      if (col10 < 0 || dateCol < 0) continue;
      for (let i = 1; i < lines.length; i++) {
        const parts = lines[i].split(',').map(p => p.trim().replace(/"/g, ''));
        const d = new Date(parts[dateCol]);
        const v = parseFloat(parts[col10]);
        if (!isNaN(d.getTime()) && !isNaN(v)) rows.push({ date: d, val: v });
      }
      Utilities.sleep(300);
    } catch (e) {
      Logger.log(`Treasury ${yr}: ${e}`);
    }
  }
  rows.sort((a, b) => a.date - b.date);
  // dedup by date string
  const seen = new Set();
  return rows.filter(r => {
    const k = r.date.toISOString().slice(0, 10);
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
}

/** Fetch latest VIX from CBOE. Returns number or null. */
function fetchVIX() {
  const url = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv';
  try {
    const resp = UrlFetchApp.fetch(url, {
      headers: { 'User-Agent': 'gold-dashboard-gas/1.0' },
      muteHttpExceptions: true
    });
    const lines = resp.getContentText().split('\n').filter(l => l.trim());
    // last non-empty line
    for (let i = lines.length - 1; i >= 0; i--) {
      const parts = lines[i].split(',');
      const v = parseFloat(parts[parts.length - 1]);
      if (!isNaN(v) && v > 0) return v;
    }
  } catch (e) { Logger.log(`VIX: ${e}`); }
  return null;
}

/** Fetch EUR/USD from ECB JSON API. Returns number or null. */
function fetchEURUSD() {
  const url = 'https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?detail=dataonly&format=jsondata';
  try {
    const resp = UrlFetchApp.fetch(url, {
      headers: { 'User-Agent': 'gold-dashboard-gas/1.0', 'Accept': 'application/json' },
      muteHttpExceptions: true
    });
    const data = JSON.parse(resp.getContentText());
    const obs = data.dataSets[0].series['0:0:0:0:0'].observations;
    const dates = data.structure.dimensions.observation[0].values;
    const pairs = Object.entries(obs).map(([k, v]) => ({
      date: new Date(dates[parseInt(k)].id),
      val: parseFloat(v[0])
    }));
    pairs.sort((a, b) => a.date - b.date);
    // return last 6 as {latest, d5ago}
    if (pairs.length < 6) return null;
    return { latest: pairs[pairs.length - 1].val, d5ago: pairs[pairs.length - 6].val };
  } catch (e) { Logger.log(`EUR/USD: ${e}`); return null; }
}

// ══════════════════════════════════════════════════════════════════════
// GCM-3 COMPUTE
// ══════════════════════════════════════════════════════════════════════

function computeGCM3() {
  const now = new Date();
  const utcH = now.getUTCHours() + now.getUTCMinutes() / 60;
  const month = now.getUTCMonth() + 1; // 1-based

  const result = {
    timestamp: now.toISOString(),
    month,
    juneSkip: month === 6,
    session: 'OFF-HOURS',
    s_min: CFG.NY_S_MIN,
    e_min: CFG.NY_E_MIN,
    price: NaN,
    atr15: NaN,
    // M layer
    m1: 0, m2: 0, m3: 0, M: 0,
    m1_note: 'no data', m2_note: 'no data', m3_note: 'no data',
    // S layer
    s1: 0, s2: 0, s3: 0, S: 0,
    s1_note: 'no data', s2_note: 'no data', s3_note: 'no data',
    // E layer
    e1: 0, e2: 0, e3: 0, E: 0,
    e1_note: '', e2_note: '', e3_note: '',
    // VIX
    vix: null, vixBlocked: false,
    // Signal
    signal: 'NO SIGNAL',
    entry: NaN, sl: NaN, tp1: NaN, tp2: NaN,
    sl_pts: NaN, tp1_pts: NaN, tp2_pts: NaN,
    riskUSD: CFG.RISK_PER_TRADE,
  };

  if (result.juneSkip) return result;

  // Session
  if (utcH >= CFG.LONDON_START && utcH < CFG.LONDON_END) {
    result.session = 'LONDON'; result.s_min = CFG.LONDON_S_MIN; result.e_min = CFG.LONDON_E_MIN;
  } else if (utcH >= CFG.NY_START && utcH < CFG.NY_END) {
    result.session = 'NEW YORK'; result.s_min = CFG.NY_S_MIN; result.e_min = CFG.NY_E_MIN;
  }

  // ── Fetch data ──────────────────────────────────────────────────────
  Logger.log('GCM-3: fetching 15m bars...');
  const bars15 = fetchOKX('15m', 6);
  Logger.log('GCM-3: fetching 4H bars...');
  const bars4h  = fetchOKX('4H', 5);

  if (!bars15 || bars15.length < 50) {
    result.signal = 'ERROR: no 15m data';
    return result;
  }

  // Current price + ATR(15m)
  const n15 = bars15.length;
  const c15 = bars15.map(b => b.c);
  const h15 = bars15.map(b => b.h);
  const l15 = bars15.map(b => b.l);
  const v15 = bars15.map(b => b.vol);
  const atr15arr = calcATR(h15, l15, c15, 14);

  result.price = c15[n15 - 1];
  result.atr15 = atr15arr[n15 - 1];

  // ── M LAYER ─────────────────────────────────────────────────────────
  Logger.log('GCM-3: fetching TIPS...');
  try {
    const tipsRows = fetchTreasury('daily_treasury_real_yield_curve');
    if (tipsRows.length >= 2) {
      const tn = tipsRows[tipsRows.length - 1].val;
      const t1 = tipsRows[tipsRows.length - 2].val;
      const delta = tn - t1;
      result.m1 = tn < t1 - 0.001 ? 1 : (tn > t1 + 0.001 ? -1 : 0);
      result.m1_note = `${tn.toFixed(2)}% prev=${t1.toFixed(2)}% Δ${delta > 0 ? '+' : ''}${delta.toFixed(3)}`;
    }
  } catch (e) { result.m1_note = `error: ${e}`; }

  Logger.log('GCM-3: fetching Breakeven...');
  try {
    const nomRows  = fetchTreasury('daily_treasury_yield_curve');
    const tipsRows = fetchTreasury('daily_treasury_real_yield_curve');
    if (nomRows.length >= 6 && tipsRows.length >= 6) {
      // build breakeven series: align by date string
      const nomMap = {};
      nomRows.forEach(r => { nomMap[r.date.toISOString().slice(0,10)] = r.val; });
      const bkevSeries = tipsRows
        .map(r => ({ date: r.date, val: (nomMap[r.date.toISOString().slice(0,10)] || NaN) - r.val }))
        .filter(r => !isNaN(r.val));
      if (bkevSeries.length >= 6) {
        const bn = bkevSeries[bkevSeries.length - 1].val;
        const b5 = bkevSeries[bkevSeries.length - 6].val;
        const delta = bn - b5;
        result.m2 = bn > b5 + 0.01 ? 1 : (bn < b5 - 0.01 ? -1 : 0);
        result.m2_note = `${bn.toFixed(2)}% 5d-ago=${b5.toFixed(2)}% Δ${delta > 0 ? '+' : ''}${delta.toFixed(3)}`;
      }
    }
  } catch (e) { result.m2_note = `error: ${e}`; }

  Logger.log('GCM-3: fetching EUR/USD...');
  try {
    const eur = fetchEURUSD();
    if (eur) {
      const ratio = eur.latest / eur.d5ago;
      result.m3 = ratio > 1.002 ? 1 : (ratio < 0.998 ? -1 : 0);
      result.m3_note = `${eur.latest.toFixed(4)} 5d-ago=${eur.d5ago.toFixed(4)} ratio=${ratio.toFixed(4)}`;
    }
  } catch (e) { result.m3_note = `error: ${e}`; }

  result.M = result.m1 + result.m2 + result.m3;

  // ── S LAYER (last completed 4H bar) ─────────────────────────────────
  if (bars4h && bars4h.length >= 55) {
    const n4 = bars4h.length;
    const nowMs = now.getTime();
    // only completed 4H bars (bar open + 4h <= now)
    let comp4h = bars4h.filter(b => b.ts + 4 * 3600 * 1000 <= nowMs);
    if (!comp4h.length) comp4h = bars4h.slice(0, -1);
    const last4h = comp4h[comp4h.length - 1];

    const c4 = bars4h.map(b => b.c);
    const h4 = bars4h.map(b => b.h);
    const l4 = bars4h.map(b => b.l);
    const ema50_4h = calcEMA(c4, 50);
    const macd4h   = calcMACD(c4, 12, 26, 9);
    const rsi4h    = calcRSI(c4, 14);

    // find index of last completed bar
    const idx4 = bars4h.findIndex(b => b.ts === last4h.ts);
    if (idx4 >= 50) {
      const ve = ema50_4h[idx4];
      const vh = macd4h.hist[idx4];
      const vr = rsi4h[idx4];

      if (!isNaN(ve)) {
        result.s1 = result.price > ve ? 1 : -1;
        result.s1_note = `price ${result.price.toFixed(1)} vs 4H EMA50 ${ve.toFixed(1)}`;
      }
      if (!isNaN(vh)) {
        result.s2 = vh > 0 ? 1 : -1;
        result.s2_note = `MACD hist=${vh.toFixed(4)}`;
      }
      if (!isNaN(vr)) {
        result.s3 = vr > 55 ? 1 : (vr < 45 ? -1 : 0);
        result.s3_note = `RSI=${vr.toFixed(1)}`;
      }
    }
  }
  result.S = result.s1 + result.s2 + result.s3;

  // ── E LAYER (last 2 completed 15m bars) ─────────────────────────────
  const macd15 = calcMACD(c15, 8, 21, 5);
  const ema9   = calcEMA(c15, 9);
  const ema21  = calcEMA(c15, 21);
  const vol20  = rollMean(v15, 20);

  // e1: MACD(8,21,5) histogram crosses zero in last 2 bars
  for (const k of [-1, -2]) {
    const ki = n15 + k;
    const hn = macd15.hist[ki];
    const hp = macd15.hist[ki - 1];
    if (isNaN(hn) || isNaN(hp)) continue;
    if (hn > 0 && hp <= 0)  { result.e1 =  1; result.e1_note = `bull cross bar${k} (h=${hn.toFixed(4)})`; break; }
    if (hn < 0 && hp >= 0)  { result.e1 = -1; result.e1_note = `bear cross bar${k} (h=${hn.toFixed(4)})`; break; }
  }
  if (!result.e1_note) result.e1_note = `no cross hist=${macd15.hist[n15-1].toFixed(4)}`;

  // e2: 5-bar high/low breakout with volume >1.2× 20-bar avg
  const curH = h15[n15 - 1]; const curL = l15[n15 - 1]; const curV = v15[n15 - 1];
  const prevH5 = Math.max(...h15.slice(n15 - 6, n15 - 1).filter(v => !isNaN(v)));
  const prevL5 = Math.min(...l15.slice(n15 - 6, n15 - 1).filter(v => !isNaN(v)));
  const avg20V = vol20[n15 - 1];
  if (!isNaN(avg20V) && avg20V > 0) {
    const volSurge = curV > avg20V * 1.2;
    if (curH > prevH5 && volSurge)  { result.e2 =  1; result.e2_note = `breakout high ${curH.toFixed(1)} vol ${(curV/avg20V).toFixed(2)}×`; }
    else if (curL < prevL5 && volSurge) { result.e2 = -1; result.e2_note = `breakout low ${curL.toFixed(1)} vol ${(curV/avg20V).toFixed(2)}×`; }
    else result.e2_note = `no breakout vol=${isNaN(avg20V) ? 'n/a' : (curV/avg20V).toFixed(2)}×`;
  }

  // e3: EMA9 vs EMA21
  const e9 = ema9[n15 - 1]; const e21 = ema21[n15 - 1];
  if (!isNaN(e9) && !isNaN(e21)) {
    result.e3 = e9 > e21 ? 1 : -1;
    result.e3_note = `EMA9=${e9.toFixed(1)} vs EMA21=${e21.toFixed(1)}`;
  }

  result.E = result.e1 + result.e2 + result.e3;

  // ── VIX ─────────────────────────────────────────────────────────────
  Logger.log('GCM-3: fetching VIX...');
  result.vix = fetchVIX();
  result.vixBlocked = result.vix !== null && result.vix >= CFG.VIX_GATE;

  // ── SIGNAL ──────────────────────────────────────────────────────────
  const sl_pts  = CFG.GCM_SL  * result.atr15;
  const tp1_pts = CFG.GCM_TP1 * result.atr15;
  const tp2_pts = CFG.GCM_TP2 * result.atr15;

  const longOk  = result.M >= CFG.M_MIN && result.m3 > 0 && result.S >= result.s_min && result.E >= result.e_min;
  const shortOk = result.M <= CFG.SHORT_M_MAX && result.S <= CFG.SHORT_S_MAX && result.E <= CFG.SHORT_E_MAX && !result.vixBlocked;

  if (longOk) {
    result.signal = 'LONG';
    result.entry  = result.price;
    result.sl     = result.price - sl_pts;
    result.tp1    = result.price + tp1_pts;
    result.tp2    = result.price + tp2_pts;
  } else if (shortOk) {
    result.signal = 'SHORT';
    result.entry  = result.price;
    result.sl     = result.price + sl_pts;
    result.tp1    = result.price - tp1_pts;
    result.tp2    = result.price - tp2_pts;
  } else {
    result.signal = 'NO SIGNAL';
  }
  result.sl_pts  = sl_pts;
  result.tp1_pts = tp1_pts;
  result.tp2_pts = tp2_pts;

  return result;
}

// ══════════════════════════════════════════════════════════════════════
// V4+ COMPUTE
// ══════════════════════════════════════════════════════════════════════

function computeV4Plus() {
  const now = new Date();
  const result = {
    timestamp: now.toISOString(),
    price: NaN,
    atrDaily: NaN,
    // 9 factors
    s1: 0, s2: 0, s3: 0, s4: 0, s5: 0, s6: 0, s7: 0, s8: 0, s9: 0,
    s1_note: 'no data', s2_note: 'no data', s3_note: 'no data',
    s4_note: '', s5_note: '', s6_note: '', s7_note: '', s8_note: '', s9_note: '',
    rawScore: 0,
    techGatePass: false,
    techGateNote: '',
    signal: 'NO SIGNAL',
    entry: NaN, sl: NaN, tp1: NaN, tp2: NaN,
    sl_pts: NaN, tp1_pts: NaN, tp2_pts: NaN,
    riskUSD: CFG.RISK_PER_TRADE,
    maxHoldDays: CFG.V4_MAX_HOLD,
  };

  // ── Fetch daily bars ─────────────────────────────────────────────────
  Logger.log('V4+: fetching 1D bars...');
  const barsD = fetchOKX('1D', 8);
  if (!barsD || barsD.length < CFG.LB_6M + 30) {
    result.signal = 'ERROR: insufficient daily data';
    return result;
  }

  const n = barsD.length;
  const closes = barsD.map(b => b.c);
  const highs  = barsD.map(b => b.h);
  const lows   = barsD.map(b => b.l);
  const vols   = barsD.map(b => b.vol);

  // Use second-to-last bar (last completed daily bar, not current in-progress bar)
  const i = n - 2;
  result.price = closes[i];

  const ema50arr  = calcEMA(closes, 50);
  const rsi14arr  = calcRSI(closes, 14);
  const macdData  = calcMACD(closes, 12, 26, 9);
  const atr14arr  = calcATR(highs, lows, closes, 14);
  const vol20arr  = rollMean(vols, 20);

  result.atrDaily = atr14arr[i];

  // ── TIPS (s1) ────────────────────────────────────────────────────────
  Logger.log('V4+: fetching TIPS...');
  try {
    const tipsRows = fetchTreasury('daily_treasury_real_yield_curve');
    if (tipsRows.length >= 2) {
      const tn = tipsRows[tipsRows.length - 1].val;
      const t1 = tipsRows[tipsRows.length - 2].val;
      result.s1 = tn < t1 ? 1 : -1;
      result.s1_note = `TIPS 10Y ${tn.toFixed(2)}% prev=${t1.toFixed(2)}% ${tn < t1 ? '↓ bullish' : '↑ bearish'}`;
    }
  } catch (e) { result.s1_note = `error: ${e}`; }

  // ── Breakeven (s2) ───────────────────────────────────────────────────
  Logger.log('V4+: fetching Breakeven...');
  try {
    const nomRows  = fetchTreasury('daily_treasury_yield_curve');
    const tipsRows2 = fetchTreasury('daily_treasury_real_yield_curve');
    if (nomRows.length >= 2 && tipsRows2.length >= 2) {
      const nomMap = {};
      nomRows.forEach(r => { nomMap[r.date.toISOString().slice(0,10)] = r.val; });
      const bkevSeries = tipsRows2
        .map(r => ({ date: r.date, val: (nomMap[r.date.toISOString().slice(0,10)] || NaN) - r.val }))
        .filter(r => !isNaN(r.val));
      if (bkevSeries.length >= 2) {
        const bn = bkevSeries[bkevSeries.length - 1].val;
        const b1 = bkevSeries[bkevSeries.length - 2].val;
        result.s2 = bn > b1 ? 1 : -1;
        result.s2_note = `Breakeven ${bn.toFixed(2)}% prev=${b1.toFixed(2)}% ${bn > b1 ? '↑ bullish' : '↓ bearish'}`;
      }
    }
  } catch (e) { result.s2_note = `error: ${e}`; }

  // ── Momentum (s3, s4, s5) ────────────────────────────────────────────
  if (i >= CFG.LB_12M) {
    result.s3 = closes[i] > closes[i - CFG.LB_12M] ? 1 : -1;
    result.s3_note = `12m mom: now=${closes[i].toFixed(0)} vs ${closes[i-CFG.LB_12M].toFixed(0)} ${result.s3>0?'↑':'↓'}`;
  }
  result.s4 = closes[i] > closes[i - CFG.LB_6M] ? 1 : -1;
  result.s4_note = `6m mom: now=${closes[i].toFixed(0)} vs ${closes[i-CFG.LB_6M].toFixed(0)} ${result.s4>0?'↑':'↓'}`;
  result.s5 = closes[i] > closes[i - CFG.LB_3M] ? 1 : -1;
  result.s5_note = `3m mom: now=${closes[i].toFixed(0)} vs ${closes[i-CFG.LB_3M].toFixed(0)} ${result.s5>0?'↑':'↓'}`;

  // ── EMA50 (s6) ───────────────────────────────────────────────────────
  const e50 = ema50arr[i];
  if (!isNaN(e50)) {
    result.s6 = closes[i] > e50 ? 1 : -1;
    result.s6_note = `price=${closes[i].toFixed(0)} vs EMA50=${e50.toFixed(0)} ${result.s6>0?'above':'below'}`;
  }

  // ── RSI(14) (s7) ─────────────────────────────────────────────────────
  const rsiV = rsi14arr[i];
  if (!isNaN(rsiV)) {
    result.s7 = rsiV > 60 ? 1 : (rsiV < 40 ? -1 : 0);
    result.s7_note = `RSI=${rsiV.toFixed(1)} ${result.s7===1?'>60 bull':result.s7===-1?'<40 bear':'neutral 40-60'}`;
  }

  // ── Volume confirmation (s8) ─────────────────────────────────────────
  const volV  = vols[i];
  const vol20 = vol20arr[i];
  if (!isNaN(volV) && !isNaN(vol20) && vol20 > 0) {
    const vr    = volV / vol20;
    const dayUp = closes[i] > closes[i - 1];
    if (vr > 1.3)      result.s8 =  dayUp ?  1 : -1;
    else if (vr < 0.7) result.s8 = dayUp ? -1 :  1;
    else               result.s8 = 0;
    result.s8_note = `vol=${vr.toFixed(2)}× avg (${dayUp?'up':'down'} day) → ${result.s8===1?'bull':result.s8===-1?'bear':'neutral'}`;
  }

  // ── MACD fresh cross within 2 bars (s9) ─────────────────────────────
  for (const k of [i, i - 1]) {
    if (isNaN(macdData.macd[k]) || isNaN(macdData.signal[k])) continue;
    const aboveNow  = macdData.macd[k]     > macdData.signal[k];
    const abovePrev = macdData.macd[k - 1] > macdData.signal[k - 1];
    if (aboveNow && !abovePrev)  { result.s9 =  1; result.s9_note = `bull cross bar-${i-k} MACD>${macdData.macd[k].toFixed(1)}`; break; }
    if (!aboveNow && abovePrev)  { result.s9 = -1; result.s9_note = `bear cross bar-${i-k} MACD<${macdData.macd[k].toFixed(1)}`; break; }
  }
  if (!result.s9_note) {
    const m = macdData.macd[i];
    const s = macdData.signal[i];
    result.s9_note = `no recent cross MACD=${isNaN(m)?'n/a':m.toFixed(1)} sig=${isNaN(s)?'n/a':s.toFixed(1)}`;
  }

  // ── Score + direction ─────────────────────────────────────────────────
  const raw = result.s1 + result.s2 + result.s3 + result.s4 + result.s5 +
              result.s6 + result.s7 + result.s8 + result.s9;
  result.rawScore = raw;
  const direction = raw > 0 ? 'LONG' : (raw < 0 ? 'SHORT' : null);

  if (!direction || Math.abs(raw) < CFG.V4_THRESHOLD) {
    result.signal = `NO SIGNAL (score ${raw > 0 ? '+' : ''}${raw}/9, need ±${CFG.V4_THRESHOLD})`;
    return result;
  }

  // ── Tech freshness gate ───────────────────────────────────────────────
  const gatePass = direction === 'LONG'
    ? (result.s8 >= 0 && result.s9 >= 0)
    : (result.s8 <= 0 && result.s9 <= 0);
  result.techGatePass = gatePass;
  result.techGateNote = direction === 'LONG'
    ? `need s8≥0(${result.s8>=0?'✓':'✗'}) AND s9≥0(${result.s9>=0?'✓':'✗'})`
    : `need s8≤0(${result.s8<=0?'✓':'✗'}) AND s9≤0(${result.s9<=0?'✓':'✗'})`;

  if (!gatePass) {
    result.signal = `NO SIGNAL — tech gate blocked (${result.techGateNote})`;
    return result;
  }

  // ── Levels ───────────────────────────────────────────────────────────
  const atrV    = result.atrDaily;
  const sl_pts  = CFG.V4_SL  * atrV;
  const tp1_pts = CFG.V4_TP1 * atrV;
  const tp2_pts = CFG.V4_TP2 * atrV;
  result.sl_pts  = sl_pts;
  result.tp1_pts = tp1_pts;
  result.tp2_pts = tp2_pts;

  result.signal = direction;
  result.entry  = result.price;
  result.sl     = direction === 'LONG' ? result.price - sl_pts : result.price + sl_pts;
  result.tp1    = direction === 'LONG' ? result.price + tp1_pts : result.price - tp1_pts;
  result.tp2    = direction === 'LONG' ? result.price + tp2_pts : result.price - tp2_pts;

  return result;
}

// ══════════════════════════════════════════════════════════════════════
// DASHBOARD WRITER
// ══════════════════════════════════════════════════════════════════════

function getOrCreateSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(CFG.SHEET_NAME);
  if (!sh) sh = ss.insertSheet(CFG.SHEET_NAME);
  return sh;
}

function writeDashboard(gcm, v4) {
  const sh = getOrCreateSheet();
  sh.clearContents();
  sh.clearFormats();

  const BG_HEADER  = '#1a1a2e';
  const BG_SECTION = '#16213e';
  const BG_LONG    = '#1a3a2e';
  const BG_SHORT   = '#3a1a1e';
  const BG_NONE    = '#2a2a2a';
  const BG_ROW     = '#0f3460';
  const BG_ALT     = '#12234a';
  const FG_WHITE   = '#ffffff';
  const FG_GOLD    = '#ffd700';
  const FG_GREEN   = '#00e676';
  const FG_RED     = '#ff5252';
  const FG_YELLOW  = '#ffeb3b';
  const FG_DIM     = '#9e9e9e';
  const FG_CYAN    = '#00e5ff';

  const rows = [];

  // helper to push a row: [col_A_val, col_B_val, ...], then format overrides
  const r = (vals, fmt) => rows.push({ vals, fmt: fmt || {} });

  const now = new Date();
  const ts = Utilities.formatDate(now, 'UTC', "yyyy-MM-dd HH:mm") + ' UTC';

  // ══ HEADER ══════════════════════════════════════════════════════════
  r(['⚡ GOLD TRADING DASHBOARD', '', '', '', `Updated: ${ts}`],
    { bg: BG_HEADER, fg: FG_GOLD, bold: true, size: 14 });

  r(['XAU/USDT-SWAP  ·  OKX Perpetual  ·  Risk $300/trade', '', '', '', ''],
    { bg: BG_HEADER, fg: FG_DIM, size: 10 });

  r([]);

  // ── PRICE SNAPSHOT ──────────────────────────────────────────────────
  r(['PRICE SNAPSHOT', '', '', '', ''], { bg: BG_SECTION, fg: FG_CYAN, bold: true });

  const priceStr = isNaN(gcm.price) ? 'n/a' : `$${gcm.price.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})}`;
  const atr15Str = isNaN(gcm.atr15)  ? 'n/a' : `$${gcm.atr15.toFixed(2)}`;
  const atrDStr  = isNaN(v4.atrDaily) ? 'n/a' : `$${v4.atrDaily.toFixed(2)}`;

  r(['Current Price', priceStr, '', 'ATR(14) 15m', atr15Str], { bg: BG_ROW, fg: FG_WHITE, bold: [false,true,false,false,true] });
  r(['', '', '', 'ATR(14) Daily', atrDStr],                   { bg: BG_ALT, fg: FG_WHITE, bold: [false,false,false,false,true] });

  r([]);

  // ════════════════════════════════════════════════════════════════════
  // GCM-3 SECTION
  // ════════════════════════════════════════════════════════════════════
  const gcmSignalBg = gcm.signal === 'LONG' ? BG_LONG : gcm.signal === 'SHORT' ? BG_SHORT : BG_NONE;
  const gcmSignalFg = gcm.signal === 'LONG' ? FG_GREEN : gcm.signal === 'SHORT' ? FG_RED : FG_YELLOW;

  r(['══ GCM-3  ·  15-MINUTE MODEL ══', '', '', '', ''], { bg: BG_HEADER, fg: FG_GOLD, bold: true, size: 12 });

  if (gcm.juneSkip) {
    r(['🚫 JUNE ABSOLUTE SKIP', 'No trades this month. Model resumes 1 July.', '', '', ''],
      { bg: '#3a2a00', fg: FG_YELLOW, bold: true });
  } else {
    // Session
    const sessCol = gcm.session === 'OFF-HOURS' ? FG_DIM : FG_GREEN;
    r(['Session', gcm.session, '', `S-min=${gcm.s_min}  E-min=${gcm.e_min}`, ''],
      { bg: BG_ROW, fg: FG_WHITE, fgCols: { 1: sessCol } });

    r([]);

    // ── M Layer ──────────────────────────────────────────────────────
    r(['M · MACRO LAYER (daily)', '', '', `Score: ${gcm.M > 0 ? '+' : ''}${gcm.M} / 3`, ''],
      { bg: BG_SECTION, fg: FG_CYAN, bold: true });

    const mRows = [
      ['m1', 'TIPS 10Y direction',   gcm.m1, gcm.m1_note],
      ['m2', 'Breakeven 5d trend',   gcm.m2, gcm.m2_note],
      ['m3', 'EUR/USD 5d trend',     gcm.m3, gcm.m3_note],
    ];
    for (const [key, label, val, note] of mRows) {
      const scoreFg = val === 1 ? FG_GREEN : val === -1 ? FG_RED : FG_YELLOW;
      const scoreStr = val === 1 ? '+1' : val === -1 ? '-1' : '0';
      const bg = key === 'm1' ? BG_ROW : BG_ALT;
      r([key, label, scoreStr, note, ''], { bg, fg: FG_WHITE, fgCols: { 2: scoreFg } });
    }
    const mTotFg = gcm.M >= 1 ? FG_GREEN : gcm.M <= -2 ? FG_RED : FG_YELLOW;
    r(['TOTAL M', '', `${gcm.M > 0?'+':''}${gcm.M}`, `LONG needs M≥1  SHORT needs M≤-2`, ''],
      { bg: BG_SECTION, fg: FG_WHITE, bold: true, fgCols: { 2: mTotFg } });

    r([]);

    // ── S Layer ──────────────────────────────────────────────────────
    r(['S · SWING LAYER (4H)', '', '', `Score: ${gcm.S > 0 ? '+' : ''}${gcm.S} / 3`, ''],
      { bg: BG_SECTION, fg: FG_CYAN, bold: true });

    const sRows = [
      ['s1', '4H EMA50 regime',      gcm.s1, gcm.s1_note],
      ['s2', '4H MACD histogram',    gcm.s2, gcm.s2_note],
      ['s3', '4H RSI zone',          gcm.s3, gcm.s3_note],
    ];
    for (const [key, label, val, note] of sRows) {
      const scoreFg = val === 1 ? FG_GREEN : val === -1 ? FG_RED : FG_YELLOW;
      const scoreStr = val === 1 ? '+1' : val === -1 ? '-1' : '0';
      const bg = key === 's1' ? BG_ROW : BG_ALT;
      r([key, label, scoreStr, note, ''], { bg, fg: FG_WHITE, fgCols: { 2: scoreFg } });
    }
    const sTotFg = gcm.S >= gcm.s_min ? FG_GREEN : gcm.S <= CFG.SHORT_S_MAX ? FG_RED : FG_YELLOW;
    r(['TOTAL S', '', `${gcm.S > 0?'+':''}${gcm.S}`, `LONG≥${gcm.s_min}(${gcm.session})  SHORT≤-2`, ''],
      { bg: BG_SECTION, fg: FG_WHITE, bold: true, fgCols: { 2: sTotFg } });

    r([]);

    // ── E Layer ──────────────────────────────────────────────────────
    r(['E · ENTRY LAYER (15m)', '', '', `Score: ${gcm.E > 0 ? '+' : ''}${gcm.E} / 3`, ''],
      { bg: BG_SECTION, fg: FG_CYAN, bold: true });

    const eRows = [
      ['e1', 'MACD(8,21,5) cross',   gcm.e1, gcm.e1_note],
      ['e2', '5-bar breakout + vol', gcm.e2, gcm.e2_note],
      ['e3', 'EMA9 vs EMA21',        gcm.e3, gcm.e3_note],
    ];
    for (const [key, label, val, note] of eRows) {
      const scoreFg = val === 1 ? FG_GREEN : val === -1 ? FG_RED : FG_YELLOW;
      const scoreStr = val === 1 ? '+1' : val === -1 ? '-1' : '0';
      const bg = key === 'e1' ? BG_ROW : BG_ALT;
      r([key, label, scoreStr, note, ''], { bg, fg: FG_WHITE, fgCols: { 2: scoreFg } });
    }
    const eTotFg = gcm.E >= gcm.e_min ? FG_GREEN : gcm.E <= CFG.SHORT_E_MAX ? FG_RED : FG_YELLOW;
    r(['TOTAL E', '', `${gcm.E > 0?'+':''}${gcm.E}`, `LONG≥${gcm.e_min}(${gcm.session})  SHORT≤-2`, ''],
      { bg: BG_SECTION, fg: FG_WHITE, bold: true, fgCols: { 2: eTotFg } });

    r([]);

    // ── VIX ──────────────────────────────────────────────────────────
    const vixStr  = gcm.vix !== null ? gcm.vix.toFixed(2) : 'n/a';
    const vixNote = gcm.vixBlocked ? `⚠ ≥${CFG.VIX_GATE} — SHORT blocked` : `< ${CFG.VIX_GATE} — SHORT ok`;
    const vixFg   = gcm.vixBlocked ? FG_RED : FG_GREEN;
    r(['VIX', vixStr, '', vixNote, ''], { bg: BG_ROW, fg: FG_WHITE, fgCols: { 1: vixFg } });

    r([]);

    // ── GCM-3 SIGNAL ─────────────────────────────────────────────────
    r(['◆ GCM-3 SIGNAL', gcm.signal, '', '', ''],
      { bg: gcmSignalBg, fg: gcmSignalFg, bold: true, size: 13 });

    if (gcm.signal === 'LONG' || gcm.signal === 'SHORT') {
      const dir = gcm.signal;
      r(['Entry',  `$${gcm.entry.toFixed(2)}`,  '', `ATR(15m) = $${gcm.atr15.toFixed(2)}`, ''],
        { bg: gcmSignalBg, fg: FG_WHITE });
      r(['Stop Loss',  `$${gcm.sl.toFixed(2)}`,  '', `${CFG.GCM_SL}× ATR = $${gcm.sl_pts.toFixed(2)} (−1R)`, ''],
        { bg: gcmSignalBg, fg: FG_RED });
      r(['TP1 (60%)', `$${gcm.tp1.toFixed(2)}`, '', `${CFG.GCM_TP1}× ATR = $${gcm.tp1_pts.toFixed(2)} (+1.5R)`, ''],
        { bg: gcmSignalBg, fg: FG_GREEN });
      r(['TP2 (40%)', `$${gcm.tp2.toFixed(2)}`, '', `${CFG.GCM_TP2}× ATR = $${gcm.tp2_pts.toFixed(2)} (+2.5R)`, ''],
        { bg: gcmSignalBg, fg: FG_GREEN });
      const riskD = CFG.RISK_PER_TRADE;
      r(['Risk $', `$${riskD}`, '', `Reward TP1 $${(riskD*1.5).toFixed(0)}  TP2 $${(riskD*2.5).toFixed(0)}`, ''],
        { bg: gcmSignalBg, fg: FG_YELLOW });
      r(['Max hold', '8 hours', '', 'Close at session end if not hit', ''],
        { bg: gcmSignalBg, fg: FG_DIM });
    } else {
      const longStatus = `M(${gcm.M}≥1:${gcm.M>=1?'✓':'✗'}) m3(${gcm.m3}>0:${gcm.m3>0?'✓':'✗'}) S(${gcm.S}≥${gcm.s_min}:${gcm.S>=gcm.s_min?'✓':'✗'}) E(${gcm.E}≥${gcm.e_min}:${gcm.E>=gcm.e_min?'✓':'✗'})`;
      const shortStatus = `M(${gcm.M}≤-2:${gcm.M<=-2?'✓':'✗'}) S(${gcm.S}≤-2:${gcm.S<=-2?'✓':'✗'}) E(${gcm.E}≤-2:${gcm.E<=-2?'✓':'✗'}) VIX(${gcm.vixBlocked?'blocked':'ok'})`;
      r(['Why no signal', '', '', '', ''], { bg: BG_NONE, fg: FG_DIM });
      r(['LONG gate',  longStatus,  '', '', ''], { bg: BG_NONE, fg: FG_DIM, size: 9 });
      r(['SHORT gate', shortStatus, '', '', ''], { bg: BG_NONE, fg: FG_DIM, size: 9 });
    }
  }

  r([]);
  r([]);

  // ════════════════════════════════════════════════════════════════════
  // V4+ SECTION
  // ════════════════════════════════════════════════════════════════════
  const v4SigBg = v4.signal === 'LONG' ? BG_LONG : v4.signal === 'SHORT' ? BG_SHORT : BG_NONE;
  const v4SigFg = v4.signal === 'LONG' ? FG_GREEN : v4.signal === 'SHORT' ? FG_RED : FG_YELLOW;

  r(['══ V4+  ·  DAILY MACRO MODEL ══', '', '', '', ''], { bg: BG_HEADER, fg: FG_GOLD, bold: true, size: 12 });
  r(['Refreshes daily 00:15 UTC  ·  No June skip (bidirectional)', '', '', '', ''],
    { bg: BG_HEADER, fg: FG_DIM, size: 9 });

  r([]);

  // Factor table header
  r(['Factor', 'Description', 'Score', 'Detail', ''],
    { bg: BG_SECTION, fg: FG_CYAN, bold: true });

  const factors = [
    ['s1', 'TIPS 10Y real yield Δ1d',    v4.s1, v4.s1_note, '  primary macro driver'],
    ['s2', 'Breakeven inflation Δ1d',     v4.s2, v4.s2_note, '  inflation expectations'],
    ['s3', '12-month momentum',           v4.s3, v4.s3_note, '  AQR-validated'],
    ['s4', '6-month momentum',            v4.s4, v4.s4_note, '  medium-term trend'],
    ['s5', '3-month momentum',            v4.s5, v4.s5_note, '  tactical continuation'],
    ['s6', 'EMA50 regime',                v4.s6, v4.s6_note, ''],
    ['s7', 'RSI(14) zone',                v4.s7, v4.s7_note, '  >60 bull / <40 bear'],
    ['s8', 'Volume vs 20d avg ★gate',     v4.s8, v4.s8_note, '  freshness gate'],
    ['s9', 'MACD(12,26,9) fresh cross ★', v4.s9, v4.s9_note, '  freshness gate'],
  ];
  for (let fi = 0; fi < factors.length; fi++) {
    const [key, desc, val, note] = factors[fi];
    const scoreFg  = val === 1 ? FG_GREEN : val === -1 ? FG_RED : FG_YELLOW;
    const scoreStr = val === 1 ? '+1' : val === -1 ? '-1' : '0';
    const bg = fi % 2 === 0 ? BG_ROW : BG_ALT;
    r([key, desc, scoreStr, note, ''], { bg, fg: FG_WHITE, fgCols: { 2: scoreFg } });
  }

  r([]);

  // Score summary
  const absScore = Math.abs(v4.rawScore);
  const scoreFg2 = absScore >= CFG.V4_THRESHOLD ? FG_GREEN : FG_YELLOW;
  const dirStr   = v4.rawScore > 0 ? 'LONG bias' : v4.rawScore < 0 ? 'SHORT bias' : 'neutral';
  r(['RAW SCORE', `${v4.rawScore > 0 ? '+' : ''}${v4.rawScore} / 9`, dirStr, `Threshold: ±${CFG.V4_THRESHOLD}`, ''],
    { bg: BG_SECTION, fg: FG_WHITE, bold: true, fgCols: { 1: scoreFg2 } });

  // Tech gate
  const gateFg = v4.techGatePass ? FG_GREEN : FG_RED;
  r(['TECH FRESHNESS GATE ★', v4.techGatePass ? 'PASS ✓' : 'BLOCKED ✗', '', v4.techGateNote, ''],
    { bg: BG_SECTION, fg: FG_WHITE, fgCols: { 1: gateFg } });

  r([]);

  // ── V4+ SIGNAL ─────────────────────────────────────────────────────
  r(['◆ V4+ SIGNAL', v4.signal.replace('NO SIGNAL', 'NO SIGNAL'), '', '', ''],
    { bg: v4SigBg, fg: v4SigFg, bold: true, size: 13 });

  if (v4.signal === 'LONG' || v4.signal === 'SHORT') {
    r(['Entry (next open)', `$${v4.entry.toFixed(2)}`, '', `ATR(14) daily = $${v4.atrDaily.toFixed(2)}`, ''],
      { bg: v4SigBg, fg: FG_WHITE });
    r(['Stop Loss',  `$${v4.sl.toFixed(2)}`,  '', `${CFG.V4_SL}× ATR = $${v4.sl_pts.toFixed(2)} (−1R)`, ''],
      { bg: v4SigBg, fg: FG_RED });
    r(['TP1 (60%)', `$${v4.tp1.toFixed(2)}`, '', `${CFG.V4_TP1}× ATR = $${v4.tp1_pts.toFixed(2)} (+2R)`, ''],
      { bg: v4SigBg, fg: FG_GREEN });
    r(['TP2 (40%)', `$${v4.tp2.toFixed(2)}`, '', `${CFG.V4_TP2}× ATR = $${v4.tp2_pts.toFixed(2)} (+3.33R)`, ''],
      { bg: v4SigBg, fg: FG_GREEN });
    const riskD = CFG.RISK_PER_TRADE;
    r(['Risk $', `$${riskD}`, '', `Reward TP1 $${(riskD*2).toFixed(0)}  TP2 $${(riskD*3.33).toFixed(0)}`, ''],
      { bg: v4SigBg, fg: FG_YELLOW });
    r(['Max hold', `${CFG.V4_MAX_HOLD} trading days`, '', 'Close if no TP hit', ''],
      { bg: v4SigBg, fg: FG_DIM });
  } else {
    r([v4.signal, '', '', '', ''], { bg: BG_NONE, fg: FG_DIM });
  }

  r([]);

  // ── MARKET STATE SUMMARY ─────────────────────────────────────────────
  r(['══ MARKET STATE SUMMARY ══', '', '', '', ''], { bg: BG_HEADER, fg: FG_GOLD, bold: true });

  let bias = 'NEUTRAL';
  let biasFg = FG_YELLOW;
  const bullCount = [gcm.m1, gcm.m2, gcm.m3, gcm.s1, gcm.s2, gcm.s3,
                     v4.s3, v4.s4, v4.s5, v4.s6, v4.s7].filter(v => v === 1).length;
  const bearCount = [gcm.m1, gcm.m2, gcm.m3, gcm.s1, gcm.s2, gcm.s3,
                     v4.s3, v4.s4, v4.s5, v4.s6, v4.s7].filter(v => v === -1).length;
  if (bullCount > bearCount + 2) { bias = 'BULLISH'; biasFg = FG_GREEN; }
  else if (bearCount > bullCount + 2) { bias = 'BEARISH'; biasFg = FG_RED; }

  r(['Overall Market Bias', bias, '', `${bullCount} bull signals vs ${bearCount} bear across both models`, ''],
    { bg: BG_ROW, fg: FG_WHITE, bold: [false, true], fgCols: { 1: biasFg } });

  const gcmDirStr  = gcm.juneSkip ? 'JUNE SKIP' : gcm.signal;
  const gcmDirFg   = gcm.signal === 'LONG' ? FG_GREEN : gcm.signal === 'SHORT' ? FG_RED : FG_YELLOW;
  const v4DirFg    = v4.signal  === 'LONG' ? FG_GREEN : v4.signal  === 'SHORT' ? FG_RED : FG_YELLOW;

  r(['GCM-3 (15m)', gcmDirStr, `M=${gcm.M} S=${gcm.S} E=${gcm.E}`, gcm.session, ''],
    { bg: BG_ALT, fg: FG_WHITE, fgCols: { 1: gcmDirFg } });
  r(['V4+  (daily)', v4.signal === 'LONG' || v4.signal === 'SHORT' ? v4.signal : 'NO SIGNAL',
     `score ${v4.rawScore>0?'+':''}${v4.rawScore}/9`, `gate: ${v4.techGatePass ? 'pass' : 'blocked'}`, ''],
    { bg: BG_ROW, fg: FG_WHITE, fgCols: { 1: v4DirFg } });

  r([]);
  r(['Backtest stats (for context)', '', '', '', ''], { bg: BG_SECTION, fg: FG_DIM, bold: true });
  r(['GCM-3', 'WR=51% PF=1.56 +25.37R', 'NY-only: WR=59% +31.37R', '104 trades / 2yr', ''],
    { bg: BG_ALT, fg: FG_DIM });
  r(['V4+',  'WR=64% PF=3.56 +33.98R', 'MaxDD=-4.29R $1,088/mo', '39 trades / 10mo', ''],
    { bg: BG_ROW, fg: FG_DIM });

  // ── WRITE TO SHEET ──────────────────────────────────────────────────
  const maxCols = 5;
  // pre-fill with empty so batch setValues doesn't fail
  const data = rows.map(row => {
    const v = (row.vals || []).slice(0, maxCols);
    while (v.length < maxCols) v.push('');
    return v;
  });

  if (data.length === 0) return;

  sh.getRange(1, 1, data.length, maxCols).setValues(data);

  // Apply formatting row by row
  for (let ri = 0; ri < rows.length; ri++) {
    const fmt = rows[ri].fmt || {};
    if (Object.keys(fmt).length === 0) continue;
    const range = sh.getRange(ri + 1, 1, 1, maxCols);
    if (fmt.bg)   range.setBackground(fmt.bg);
    if (fmt.fg)   range.setFontColor(fmt.fg);
    if (fmt.bold) range.setFontWeight('bold');
    if (fmt.size) range.setFontSize(fmt.size);
    // per-column font color overrides
    if (fmt.fgCols) {
      for (const [col, color] of Object.entries(fmt.fgCols)) {
        sh.getRange(ri + 1, parseInt(col) + 1, 1, 1).setFontColor(color);
      }
    }
  }

  // Column widths
  sh.setColumnWidth(1, 160);
  sh.setColumnWidth(2, 160);
  sh.setColumnWidth(3, 100);
  sh.setColumnWidth(4, 340);
  sh.setColumnWidth(5, 80);

  // Freeze header rows
  sh.setFrozenRows(2);

  SpreadsheetApp.flush();
}

// ══════════════════════════════════════════════════════════════════════
// EMAIL ALERTS
// ══════════════════════════════════════════════════════════════════════

function sendEmailAlert(gcm, v4) {
  const signals = [];
  if (gcm.signal === 'LONG' || gcm.signal === 'SHORT') signals.push('GCM-3: ' + gcm.signal);
  if (v4.signal  === 'LONG' || v4.signal  === 'SHORT') signals.push('V4+: '  + v4.signal);
  if (signals.length === 0) return;

  const subject = `🚨 GOLD SIGNAL: ${signals.join(' | ')}`;

  let body = `GOLD TRADING ALERT\n${'='.repeat(50)}\n\n`;
  body += `Time: ${new Date().toUTCString()}\n`;
  body += `Price: $${(gcm.price || v4.price || 0).toFixed(2)}\n\n`;

  if (gcm.signal === 'LONG' || gcm.signal === 'SHORT') {
    body += `\n── GCM-3 (15m Model) ──────────────────\n`;
    body += `Signal:   ${gcm.signal}\n`;
    body += `Session:  ${gcm.session}\n`;
    body += `M-Score:  ${gcm.M} (m1=${gcm.m1} m2=${gcm.m2} m3=${gcm.m3})\n`;
    body += `S-Score:  ${gcm.S} (s1=${gcm.s1} s2=${gcm.s2} s3=${gcm.s3})\n`;
    body += `E-Score:  ${gcm.E} (e1=${gcm.e1} e2=${gcm.e2} e3=${gcm.e3})\n`;
    body += `VIX:      ${gcm.vix !== null ? gcm.vix.toFixed(2) : 'n/a'}${gcm.vixBlocked?' [BLOCKED]':''}\n`;
    body += `Entry:    $${gcm.entry.toFixed(2)}\n`;
    body += `Stop:     $${gcm.sl.toFixed(2)}  (−$${gcm.sl_pts.toFixed(2)})\n`;
    body += `TP1 60%:  $${gcm.tp1.toFixed(2)}  (+$${gcm.tp1_pts.toFixed(2)})\n`;
    body += `TP2 40%:  $${gcm.tp2.toFixed(2)}  (+$${gcm.tp2_pts.toFixed(2)})\n`;
    body += `Max hold: 8 hours\n`;
  }

  if (v4.signal === 'LONG' || v4.signal === 'SHORT') {
    body += `\n── V4+ (Daily Model) ──────────────────\n`;
    body += `Signal:   ${v4.signal}\n`;
    body += `Score:    ${v4.rawScore > 0 ? '+' : ''}${v4.rawScore}/9 (threshold ±5)\n`;
    body += `Tech gate: ${v4.techGatePass ? 'PASS' : 'BLOCKED'}\n`;
    body += `Entry:    $${v4.entry.toFixed(2)} (next open)\n`;
    body += `Stop:     $${v4.sl.toFixed(2)}  (−$${v4.sl_pts.toFixed(2)})\n`;
    body += `TP1 60%:  $${v4.tp1.toFixed(2)}  (+$${v4.tp1_pts.toFixed(2)})\n`;
    body += `TP2 40%:  $${v4.tp2.toFixed(2)}  (+$${v4.tp2_pts.toFixed(2)})\n`;
    body += `Max hold: ${v4.maxHoldDays} trading days\n`;
  }

  body += `\n${'='.repeat(50)}\n`;
  body += `Risk per trade: $${CFG.RISK_PER_TRADE}\n`;
  body += `Dashboard: ${SpreadsheetApp.getActiveSpreadsheet().getUrl()}\n`;

  try {
    MailApp.sendEmail({
      to: CFG.ALERT_EMAIL,
      subject,
      body
    });
    Logger.log(`Email alert sent to ${CFG.ALERT_EMAIL}`);
  } catch (e) {
    Logger.log(`Email failed: ${e}`);
  }
}

// ══════════════════════════════════════════════════════════════════════
// MAIN REFRESH FUNCTION
// ══════════════════════════════════════════════════════════════════════

/**
 * refreshAll() — called by the 15-min trigger AND the daily trigger.
 * Both models are always run; V4+ signal is daily but we still display
 * the cached result on 15m ticks once we've computed it.
 */
function refreshAll() {
  try {
    Logger.log('=== refreshAll START ===');

    // Determine if we should recompute V4+ (only on daily trigger or forced)
    // We store last V4+ result in script properties to avoid redundant Treasury calls
    const props   = PropertiesService.getScriptProperties();
    const now     = new Date();
    const todayStr = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
    const lastV4Day = props.getProperty('LAST_V4_DAY') || '';

    let v4;
    if (lastV4Day !== todayStr || now.getUTCHours() < 1) {
      // Recompute V4+ once per day (before 01:00 UTC or if never run today)
      Logger.log('V4+: computing...');
      v4 = computeV4Plus();
      props.setProperty('LAST_V4_DAY', todayStr);
      props.setProperty('LAST_V4_RESULT', JSON.stringify(v4));
    } else {
      // Use cached result for 15m refreshes
      Logger.log('V4+: using cached result from today');
      const cached = props.getProperty('LAST_V4_RESULT');
      v4 = cached ? JSON.parse(cached) : computeV4Plus();
    }

    Logger.log('GCM-3: computing...');
    const gcm = computeGCM3();

    Logger.log('Writing dashboard...');
    writeDashboard(gcm, v4);

    // Send alert if any signal fires
    if (gcm.signal === 'LONG' || gcm.signal === 'SHORT' ||
        v4.signal  === 'LONG' || v4.signal  === 'SHORT') {
      // Throttle: don't resend same signal within 30 min
      const alertKey = `${gcm.signal}|${v4.signal}|${todayStr}`;
      const lastAlert = props.getProperty('LAST_ALERT_KEY') || '';
      const lastAlertTs = parseInt(props.getProperty('LAST_ALERT_TS') || '0');
      const nowMs = now.getTime();
      if (alertKey !== lastAlert || (nowMs - lastAlertTs) > 30 * 60 * 1000) {
        sendEmailAlert(gcm, v4);
        props.setProperty('LAST_ALERT_KEY', alertKey);
        props.setProperty('LAST_ALERT_TS', nowMs.toString());
      }
    }

    Logger.log('=== refreshAll DONE ===');
  } catch (e) {
    Logger.log(`refreshAll error: ${e}`);
    // Write error to sheet
    try {
      const sh = getOrCreateSheet();
      sh.getRange(1, 1).setValue(`ERROR: ${e} at ${new Date().toISOString()}`);
    } catch (_) {}
  }
}

// Wrapper called by the 15-min time trigger
function refreshGCM() { refreshAll(); }

// Wrapper called by the daily trigger (00:15 UTC)
function refreshV4Daily() {
  // Force V4+ recompute
  PropertiesService.getScriptProperties().deleteProperty('LAST_V4_DAY');
  refreshAll();
}

// ══════════════════════════════════════════════════════════════════════
// TRIGGER SETUP
// ══════════════════════════════════════════════════════════════════════

function setupTriggers() {
  // Delete any existing triggers to avoid duplicates
  const triggers = ScriptApp.getProjectTriggers();
  for (const t of triggers) {
    if (['refreshGCM', 'refreshV4Daily', 'refreshAll'].includes(t.getHandlerFunction())) {
      ScriptApp.deleteTrigger(t);
    }
  }

  // Every 15 minutes — GCM-3 (and use cached V4+)
  ScriptApp.newTrigger('refreshGCM')
    .timeBased()
    .everyMinutes(15)
    .create();

  // Daily at 00:15 UTC — force V4+ recompute (run between 00:00 and 01:00)
  ScriptApp.newTrigger('refreshV4Daily')
    .timeBased()
    .atHour(0)          // midnight UTC (Apps Script uses local tz but we store UTC)
    .everyDays(1)
    .create();

  Logger.log('Triggers created: 15-min GCM + daily V4+');
  SpreadsheetApp.getUi().alert(
    '✓ Triggers set!\n\n' +
    '• GCM-3 refreshes every 15 minutes\n' +
    '• V4+ refreshes daily at midnight UTC\n' +
    '• Email alerts → ' + CFG.ALERT_EMAIL + '\n\n' +
    'Running first refresh now...'
  );
}

// ══════════════════════════════════════════════════════════════════════
// ONE-TIME SETUP
// ══════════════════════════════════════════════════════════════════════

/**
 * Run this ONCE to initialise everything.
 */
function setupDashboard() {
  setupTriggers();
  refreshAll();
}

// ══════════════════════════════════════════════════════════════════════
// MENU
// ══════════════════════════════════════════════════════════════════════

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('⚡ Gold Dashboard')
    .addItem('Refresh Now (both models)',  'refreshAll')
    .addItem('Refresh GCM-3 only',         'refreshGCM')
    .addItem('Force V4+ recompute',        'refreshV4Daily')
    .addSeparator()
    .addItem('Setup triggers (run once)',   'setupTriggers')
    .addItem('Full setup (first time)',     'setupDashboard')
    .addSeparator()
    .addItem('View logs',                  'openLogs')
    .addToUi();
}

function openLogs() {
  SpreadsheetApp.getUi().alert('Check View → Logs in the Apps Script editor for debug output.');
}
