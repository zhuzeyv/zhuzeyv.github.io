// api/data.js — Vercel 服务器函数
// 修复：使用正确的东方财富接口 push2ex.eastmoney.com/getTopicZTPool
// 之前用的 datacenter.eastmoney.com/RPT_PCBZB_ZT 已失效

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  if (req.method === 'OPTIONS') return res.status(200).end();

  const { date } = req.query;
  if (!date) return res.status(400).json({ error: '缺少 date 参数，格式：YYYY-MM-DD' });

  // 兼容 YYYY-MM-DD 和 YYYYMMDD 两种格式
  const emDate = date.replace(/-/g, '');
  if (!/^\d{8}$/.test(emDate)) {
    return res.status(400).json({ error: 'date 格式错误，应为 YYYY-MM-DD 或 YYYYMMDD' });
  }

  const params = new URLSearchParams({
    ut: '7eea3edcaed734bea9cbfc24409ed989',
    dpt: 'wz.ztzt',
    Pageindex: '0',
    pagesize: '10000',
    sort: 'ztbs:desc',   // ztbs = 炸板次数，降序
    date: emDate,
    _: Date.now().toString(),
  });

  const url = `http://push2ex.eastmoney.com/getTopicZTPool?${params}`;

  try {
    const r = await fetch(url, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'http://quote.eastmoney.com/',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
      },
    });

    if (!r.ok) throw new Error(`东方财富返回 HTTP ${r.status}`);

    const json = await r.json();

    // 非交易日 data 为 null
    if (!json.data || !Array.isArray(json.data.pool)) {
      return res.status(200).json({
        data: [],
        message: '该日期暂无数据，可能是非交易日',
      });
    }

    // ── 字段说明（来自 AKShare 逆向）──────────────────────────
    // c   = 股票代码
    // n   = 股票名称
    // p   = 最新价（单位：厘，需除以1000）
    // zdp = 涨跌幅（%）
    // e   = 成交额（元）
    // ltsz= 流通市值（元）
    // zf  = 总市值（元）
    // hs  = 换手率（%）
    // lbs = 连板数
    // fbt = 首次封板时间（HHMMSS 整数）
    // lbt = 最后封板时间（HHMMSS 整数）
    // fund= 封板资金（元）
    // ztbs= 炸板次数（0=封死未炸，>0=炸过板）
    // hybk= 所属行业
    // ztstat = 涨停统计 { days, ct }

    const normalized = json.data.pool.map(item => ({
      SECURITY_CODE: String(item.c || ''),
      SECURITY_NAME: String(item.n || ''),
      CLOSE_PRICE:   (item.p || 0) / 1000,
      HIGH_PRICE:    (item.p || 0) / 1000,
      CHANGE_RATE:   item.zdp || 0,
      DEAL_AMOUNT:   item.e   || 0,
      FLOAT_MV:      item.ltsz|| 0,
      TOTAL_MARKET_CAP: item.zf || 0,
      TURNOVERRATE:  item.hs  || 0,
      CONSEC_BOARDS: item.lbs || 0,
      FIRST_TIME:    fmtTime(item.fbt),
      LAST_TIME:     fmtTime(item.lbt),
      SEAL_FUND:     item.fund|| 0,
      OPEN_NUM:      item.ztbs|| 0,   // ← 核心：炸板次数
      INDUSTRY:      String(item.hybk || ''),
      ZT_STAT: item.ztstat
        ? `${item.ztstat.days || 0}/${item.ztstat.ct || 0}`
        : '',
    }));

    // 缓存 5 分钟（历史数据可缓存更久）
    res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=60');
    return res.status(200).json({ data: normalized, total: normalized.length });

  } catch (err) {
    console.error('[炸板雷达 API 错误]', err.message);
    return res.status(500).json({ error: `获取失败：${err.message}` });
  }
}

// HHMMSS 整数 → "HH:MM" 字符串
function fmtTime(val) {
  if (!val) return '';
  const s = String(Math.floor(val)).padStart(6, '0');
  return `${s.slice(0, 2)}:${s.slice(2, 4)}`;
}
