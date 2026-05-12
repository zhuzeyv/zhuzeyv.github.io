// api/data.js
// push2ex 只提供当日实时数据，用 type 参数区分：
//   zt   = 今日涨停股池（含炸板次数 ztbs）
//   zb   = 今日炸板股池（专池）
//   prev = 昨日涨停股池
// 日期仅用于校验是否为交易日

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  if (req.method === 'OPTIONS') return res.status(200).end();

  const { type = 'zt', debug } = req.query;
  const logs = [];
  const log = (msg) => { logs.push(msg); console.log('[炸板雷达]', msg); };

  // 接口映射
  const endpoints = {
    zt:   { url: 'http://push2ex.eastmoney.com/getTopicZTPool',         sort: 'fbt:asc',  desc: '今日涨停股池' },
    zb:   { url: 'http://push2ex.eastmoney.com/getTopicZBPool',         sort: 'fbt:asc',  desc: '今日炸板股池' },
    dt:   { url: 'http://push2ex.eastmoney.com/getTopicDTPool',         sort: 'fund:asc', desc: '今日跌停股池' },
    prev: { url: 'http://push2ex.eastmoney.com/getTopicPreviousZTPool', sort: 'fbt:asc',  desc: '昨日涨停股池' },
  };

  const ep = endpoints[type] || endpoints.zt;
  log(`请求接口: ${ep.desc}`);

  // 固定 _ 时间戳（和 AKShare 保持一致，避免被拦截）
  const params = new URLSearchParams({
    ut: '7eea3edcaed734bea9cbfc24409ed989',
    dpt: 'wz.ztzt',
    Pageindex: '0',
    pagesize: '10000',
    sort: ep.sort,
    _: '1621590489736',
  });

  const url = `${ep.url}?${params}`;
  log(`URL: ${url}`);

  try {
    const r = await fetchTimeout(url, 12000, {
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
      'Referer': 'http://quote.eastmoney.com/',
      'Accept': 'application/json, */*',
      'Accept-Language': 'zh-CN,zh;q=0.9',
    });

    const text = await r.text();
    log(`HTTP ${r.status}，响应: ${text.slice(0, 300)}`);

    const json = JSON.parse(text);

    if (!json.data?.pool || !Array.isArray(json.data.pool)) {
      return res.status(200).json({
        data: [],
        qdate: json.data?.qdate || null,
        message: '数据池为空，可能在非交易时段或今日数据尚未更新',
        ...(debug ? { logs, raw: json } : {}),
      });
    }

    const pool = json.data.pool;
    log(`✅ 成功，pool.length = ${pool.length}`);

    // 根据接口类型规范化字段
    const data = type === 'dt' ? normalizeDT(pool) : normalizeZT(pool);

    res.setHeader('Cache-Control', 's-maxage=120, stale-while-revalidate=30');
    return res.status(200).json({
      data,
      total: data.length,
      qdate: json.data.qdate,
      source: ep.desc,
      ...(debug ? { logs } : {}),
    });

  } catch (e) {
    log(`❌ 错误: ${e.message}`);
    return res.status(500).json({ error: e.message, ...(debug ? { logs } : {}) });
  }
}

// ── 涨停/炸板字段规范化 ───────────────────────────────────────
function normalizeZT(pool) {
  return pool.map(s => ({
    SECURITY_CODE:    String(s.c   || ''),
    SECURITY_NAME:    String(s.n   || ''),
    CLOSE_PRICE:     (s.p   || 0) / 1000,
    HIGH_PRICE:      (s.p   || 0) / 1000,
    CHANGE_RATE:      s.zdp  || 0,
    DEAL_AMOUNT:      s.e    || 0,
    FLOAT_MV:         s.ltsz || 0,
    TOTAL_MARKET_CAP: s.zf   || 0,
    TURNOVERRATE:     s.hs   || 0,
    CONSEC_BOARDS:    s.lbs  || 0,
    FIRST_TIME:       fmtTime(s.fbt),
    LAST_TIME:        fmtTime(s.lbt),
    SEAL_FUND:        s.fund || 0,
    OPEN_NUM:         s.ztbs || 0,   // 炸板次数
    INDUSTRY:         String(s.hybk || ''),
    ZT_STAT: s.ztstat
      ? `${s.ztstat.days || 0}/${s.ztstat.ct || 0}`
      : '',
  }));
}

function normalizeDT(pool) {
  return pool.map(s => ({
    SECURITY_CODE:    String(s.c || ''),
    SECURITY_NAME:    String(s.n || ''),
    CLOSE_PRICE:     (s.p || 0) / 1000,
    CHANGE_RATE:      s.zdp || 0,
    DEAL_AMOUNT:      s.e   || 0,
    TOTAL_MARKET_CAP: s.zf  || 0,
    TURNOVERRATE:     s.hs  || 0,
    OPEN_NUM:         s.otbs|| 0,   // 开板次数（跌停）
    LAST_TIME:        fmtTime(s.lbt),
    INDUSTRY:         String(s.hybk || ''),
  }));
}

async function fetchTimeout(url, ms, headers) {
  const ctrl = new AbortController();
  const tid  = setTimeout(() => ctrl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctrl.signal, headers });
    clearTimeout(tid);
    return r;
  } catch (e) {
    clearTimeout(tid);
    throw e;
  }
}

function fmtTime(val) {
  if (!val) return '';
  const s = String(Math.floor(val)).padStart(6, '0');
  return `${s.slice(0, 2)}:${s.slice(2, 4)}`;
}
