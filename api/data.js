// api/data.js — 多接口自动切换 + 完整调试信息
export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  if (req.method === 'OPTIONS') return res.status(200).end();

  const { date, debug } = req.query;
  if (!date) return res.status(400).json({ error: '缺少 date 参数，格式：YYYY-MM-DD' });

  const emDate = date.replace(/-/g, '');
  if (!/^\d{8}$/.test(emDate)) {
    return res.status(400).json({ error: 'date 格式错误' });
  }

  const logs = [];
  const log = (msg) => { logs.push(msg); console.log('[炸板雷达]', msg); };

  // ── 尝试所有可能的接口 ────────────────────────────────────
  const strategies = [
    {
      name: 'push2ex-http',
      fn: () => tryPush2ex('http', emDate, log),
    },
    {
      name: 'push2ex-https',
      fn: () => tryPush2ex('https', emDate, log),
    },
    {
      name: 'datacenter-v2',
      fn: () => tryDatacenter(emDate, log),
    },
    {
      name: 'datacenter-v2-proxy',
      fn: () => tryDatacenterViaProxy(emDate, log),
    },
  ];

  for (const strategy of strategies) {
    log(`尝试 [${strategy.name}]...`);
    try {
      const result = await strategy.fn();
      if (result && result.length > 0) {
        log(`✅ [${strategy.name}] 成功，${result.length} 条数据`);
        res.setHeader('Cache-Control', 's-maxage=300');
        return res.status(200).json({
          data: result,
          total: result.length,
          source: strategy.name,
          ...(debug ? { logs } : {}),
        });
      }
      log(`⚠️ [${strategy.name}] 返回空数据`);
    } catch (e) {
      log(`❌ [${strategy.name}] 报错: ${e.message}`);
    }
  }

  // 所有策略都失败
  return res.status(200).json({
    data: [],
    message: '所有数据源均返回空，该日期可能是非交易日，或境外服务器被限制',
    logs,  // 始终返回日志方便调试
  });
}

// ── 策略1: push2ex.eastmoney.com ─────────────────────────────
async function tryPush2ex(scheme, emDate, log) {
  const params = new URLSearchParams({
    ut: '7eea3edcaed734bea9cbfc24409ed989',
    dpt: 'wz.ztzt',
    Pageindex: '0',
    pagesize: '10000',
    sort: 'ztbs:desc',
    date: emDate,
    _: Date.now(),
  });
  const url = `${scheme}://push2ex.eastmoney.com/getTopicZTPool?${params}`;
  log(`  请求: ${url.slice(0, 120)}`);

  const r = await fetchWithTimeout(url, 10000, {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'http://quote.eastmoney.com/',
    'Accept': 'application/json, */*',
  });
  log(`  HTTP状态: ${r.status}`);
  const text = await r.text();
  log(`  响应前200字: ${text.slice(0, 200)}`);

  const json = JSON.parse(text);
  if (!json.data?.pool) return [];
  return normalizePush2ex(json.data.pool);
}

// ── 策略2: datacenter.eastmoney.com ──────────────────────────
async function tryDatacenter(emDate, log) {
  // 把 YYYYMMDD 转回 YYYY-MM-DD（datacenter 用这个格式）
  const dateStr = `${emDate.slice(0,4)}-${emDate.slice(4,6)}-${emDate.slice(6,8)}`;
  const fields = 'SECURITY_CODE,SECURITY_NAME,CHANGE_RATE,CLOSE_PRICE,HIGH_PRICE,OPEN_NUM,FIRST_TIME,LAST_TIME,TURNOVERRATE,DEAL_AMOUNT,TOTAL_MARKET_CAP,INDUSTRY';
  const params = new URLSearchParams({
    reportName: 'RPT_PCBZB_ZT',
    columns: fields,
    pageNumber: '1',
    pageSize: '500',
    sortTypes: '-1',
    sortColumns: 'OPEN_NUM',
    filter: `(TRADE_DATE='${dateStr}')`,
    source: 'DataCenter',
    client: 'PC',
    _: Date.now(),
  });
  const url = `https://datacenter.eastmoney.com/api/data/v1/get?${params}`;
  log(`  请求: ${url.slice(0, 150)}`);

  const r = await fetchWithTimeout(url, 10000, {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    'Referer': 'https://data.eastmoney.com/',
    'Accept': 'application/json',
  });
  log(`  HTTP状态: ${r.status}`);
  const text = await r.text();
  log(`  响应前200字: ${text.slice(0, 200)}`);

  const json = JSON.parse(text);
  const data = json?.result?.data;
  if (!Array.isArray(data) || data.length === 0) return [];
  return data; // datacenter 字段已是规范格式
}

// ── 策略3: datacenter 走公共 CORS 代理 ───────────────────────
async function tryDatacenterViaProxy(emDate, log) {
  const dateStr = `${emDate.slice(0,4)}-${emDate.slice(4,6)}-${emDate.slice(6,8)}`;
  const fields = 'SECURITY_CODE,SECURITY_NAME,CHANGE_RATE,CLOSE_PRICE,HIGH_PRICE,OPEN_NUM,FIRST_TIME,LAST_TIME,TURNOVERRATE,DEAL_AMOUNT,TOTAL_MARKET_CAP,INDUSTRY';
  const params = new URLSearchParams({
    reportName: 'RPT_PCBZB_ZT',
    columns: fields,
    pageNumber: '1',
    pageSize: '500',
    sortTypes: '-1',
    sortColumns: 'OPEN_NUM',
    filter: `(TRADE_DATE='${dateStr}')`,
    source: 'DataCenter',
    client: 'PC',
  });
  const emUrl = `https://datacenter.eastmoney.com/api/data/v1/get?${params}`;
  const url = `https://api.allorigins.win/raw?url=${encodeURIComponent(emUrl)}`;
  log(`  请求(via allorigins): ${emUrl.slice(0, 100)}...`);

  const r = await fetchWithTimeout(url, 15000, {});
  log(`  HTTP状态: ${r.status}`);
  const text = await r.text();
  log(`  响应前200字: ${text.slice(0, 200)}`);

  const json = JSON.parse(text);
  const data = json?.result?.data;
  if (!Array.isArray(data) || data.length === 0) return [];
  return data;
}

// ── push2ex 数据规范化 ────────────────────────────────────────
function normalizePush2ex(pool) {
  return pool.map(item => ({
    SECURITY_CODE:    String(item.c  || ''),
    SECURITY_NAME:    String(item.n  || ''),
    CLOSE_PRICE:      (item.p  || 0) / 1000,
    HIGH_PRICE:       (item.p  || 0) / 1000,
    CHANGE_RATE:      item.zdp || 0,
    DEAL_AMOUNT:      item.e   || 0,
    FLOAT_MV:         item.ltsz|| 0,
    TOTAL_MARKET_CAP: item.zf  || 0,
    TURNOVERRATE:     item.hs  || 0,
    CONSEC_BOARDS:    item.lbs || 0,
    FIRST_TIME:       fmtTime(item.fbt),
    LAST_TIME:        fmtTime(item.lbt),
    SEAL_FUND:        item.fund|| 0,
    OPEN_NUM:         item.ztbs|| 0,
    INDUSTRY:         String(item.hybk || ''),
    ZT_STAT: item.ztstat
      ? `${item.ztstat.days||0}/${item.ztstat.ct||0}`
      : '',
  }));
}

// ── 工具 ─────────────────────────────────────────────────────
async function fetchWithTimeout(url, ms, headers) {
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
  return `${s.slice(0,2)}:${s.slice(2,4)}`;
}
