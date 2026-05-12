// api/data.js — Vercel 服务器函数，服务器直连东方财富，无跨域问题
export default async function handler(req, res) {
  // 允许任何来源访问（前端调这个接口不会跨域）
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  if (req.method === 'OPTIONS') return res.status(200).end();

  const { date } = req.query;

  // 验证日期格式
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return res.status(400).json({ error: '请传入正确的日期参数，格式：YYYY-MM-DD' });
  }

  const fields = [
    'SECURITY_CODE', 'SECURITY_NAME', 'CHANGE_RATE', 'CLOSE_PRICE', 'HIGH_PRICE',
    'OPEN_NUM', 'FIRST_TIME', 'LAST_TIME', 'TURNOVERRATE', 'DEAL_AMOUNT',
    'TOTAL_MARKET_CAP', 'INDUSTRY', 'SECURITY_TYPE_CODE'
  ].join(',');

  const params = new URLSearchParams({
    reportName: 'RPT_PCBZB_ZT',
    columns: fields,
    pageNumber: '1',
    pageSize: '500',
    sortTypes: '-1',
    sortColumns: 'OPEN_NUM',
    filter: `(TRADE_DATE='${date}')`,
    source: 'DataCenter',
    client: 'PC',
  });

  const emUrl = `https://datacenter.eastmoney.com/api/data/v1/get?${params}`;

  try {
    const response = await fetch(emUrl, {
      headers: {
        // 伪装成浏览器访问，避免被反爬
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://data.eastmoney.com/',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
      },
    });

    if (!response.ok) {
      throw new Error(`东方财富返回 HTTP ${response.status}`);
    }

    const json = await response.json();
    const data = json?.result?.data;

    if (!Array.isArray(data)) {
      // 非交易日或无数据
      return res.status(200).json({ data: [], message: '该日期暂无数据，可能是非交易日' });
    }

    // 缓存 10 分钟（当天数据频繁刷新没意义）
    res.setHeader('Cache-Control', 's-maxage=600, stale-while-revalidate=60');
    return res.status(200).json({ data, total: data.length });

  } catch (err) {
    console.error('[炸板雷达 API]', err.message);
    return res.status(500).json({ error: `获取失败：${err.message}` });
  }
}
