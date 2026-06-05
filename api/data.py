"""
Vercel Python Serverless Function — api/data.py
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
import json
import traceback


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        qs = parse_qs(urlparse(self.path).query)
        ptype = qs.get("type", ["prev"])[0].lower()
        self.send_header("Cache-Control",
                         "no-store" if ptype == "recommend"
                         else "s-maxage=120, stale-while-revalidate=30")
        self.end_headers()

        try:
            date = normalize_date(qs.get("date", [""])[0])
            if ptype == "recommend":
                resp = handle_recommend()
            else:
                rows, source = fetch_zt(ptype, date)
                resp = {"data": rows, "total": len(rows), "qdate": date,
                        "source": source,
                        "message": "" if rows else f"{date} 暂无数据"}
        except Exception as e:
            resp = {"data": [], "total": 0, "error": str(e),
                    "trace": traceback.format_exc()}

        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())

    def log_message(self, *a): pass


# ── 涨停主数据 ─────────────────────────────────────────────────
def fetch_zt(ptype, date):
    import akshare as ak
    labels = {"zt": "今日涨停股池", "zb": "今日炸板股池", "prev": "昨日涨停股池"}
    df = (ak.stock_zt_pool_zbgc_em    if ptype == "zb"   else
          ak.stock_zt_pool_previous_em if ptype == "prev" else
          ak.stock_zt_pool_em)(date=date)
    if df is None or df.empty:
        return [], f"AKShare · {labels.get(ptype, ptype)}"
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "SECURITY_CODE": str(r.get("代码","") or ""),
            "SECURITY_NAME": str(r.get("名称","") or ""),
            "CHANGE_RATE":   _f(r.get("涨跌幅")),
            "CLOSE_PRICE":   _f(r.get("最新价")),
            "OPEN_NUM":      _i(r.get("炸板次数")),
            "FIRST_TIME":    ft(r.get("首次封板时间")),
            "LAST_TIME":     ft(r.get("最后封板时间")),
            "TURNOVERRATE":  _f(r.get("换手率")),
            "DEAL_AMOUNT":   _f(r.get("成交额")),
            "FLOAT_MV":      _f(r.get("流通市值")),
            "CONSEC_BOARDS": _i(r.get("连板数")),
            "INDUSTRY":      str(r.get("所属行业","") or ""),
            "ZT_STAT":       str(r.get("涨停统计","") or ""),
            "SEAL_FUND":     _f(r.get("封板资金")),
        })
    return rows, f"AKShare · {labels.get(ptype, ptype)}"


# ── 东方财富批量实时报价 ────────────────────────────────────────
def em_batch_price(codes):
    """
    用东方财富 push2 API 批量获取实时价格
    返回 {code: {price, change_rate}}
    比 stock_zh_a_spot_em 快很多，且返回 JSON，无编码问题
    """
    import requests

    if not codes:
        return {}

    secids = []
    for c in codes:
        c = str(c)
        if c.startswith('6') or c.startswith('5'):
            secids.append(f'1.{c}')
        else:
            secids.append(f'0.{c}')

    # 东方财富 ulist API：批量查多只股票的实时行情
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        'fltt':   '2',           # 返回浮点数
        'invt':   '2',
        'secids': ','.join(secids),
        'fields': 'f2,f3,f57',   # f2=价格 f3=涨跌幅% f57=代码
        'ut':     'bd1d9ddb04089700cf9c27f6f7426281',
        '_':      int(datetime.utcnow().timestamp() * 1000),
    }
    resp = requests.get(url, params=params, timeout=8, headers={
        'Referer':    'https://quote.eastmoney.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    })
    data = resp.json()
    result = {}
    for item in (data.get('data') or {}).get('diff', []):
        code = str(item.get('f57', ''))
        price = item.get('f2')
        chg   = item.get('f3')
        if code and price not in (None, '-') and chg not in (None, '-'):
            try:
                result[code] = {
                    'price':       round(float(price), 2),
                    'change_rate': round(float(chg),   2),
                }
            except Exception:
                pass
    return result


# ── 短线推荐 ───────────────────────────────────────────────────
def handle_recommend():
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor

    date = last_trade_day()

    # ── 并行调用三个涨停池（从9秒→3秒）────────────────────────
    pool_data = {}

    def safe_fetch(name, fn, **kw):
        try:
            df = fn(**kw)
            pool_data[name] = df if (df is not None and not df.empty) else None
        except Exception:
            pool_data[name] = None

    with ThreadPoolExecutor(max_workers=3) as ex:
        ex.submit(safe_fetch, 'zt',   ak.stock_zt_pool_em,          date=date)
        ex.submit(safe_fetch, 'zb',   ak.stock_zt_pool_zbgc_em,     date=date)
        ex.submit(safe_fetch, 'prev', ak.stock_zt_pool_previous_em,  date=date)

    # ── 合并去重 ────────────────────────────────────────────────
    seen, pool = set(), []
    source_map = {'zt': '今日涨停', 'zb': '今日炸板', 'prev': '昨日涨停'}
    for key in ('zt', 'zb', 'prev'):
        df = pool_data.get(key)
        if df is None:
            continue
        for _, r in df.iterrows():
            code = str(r.get("代码", ""))
            if code and code not in seen:
                seen.add(code)
                pool.append((r, source_map[key]))

    if not pool:
        return {"data": [], "qdate": date, "total": 0,
                "message": "暂无数据，请在交易日 09:30 后使用"}

    # ── 换手率粗筛（3-20%）──────────────────────────────────────
    pool = [(r, s) for r, s in pool if 3 <= _f(r.get("换手率")) <= 20]

    # ── 东方财富批量实时报价 ─────────────────────────────────────
    codes = [str(r.get("代码","")) for r, _ in pool]
    prices = {}
    try:
        prices = em_batch_price(codes)
    except Exception:
        pass   # 失败则用涨停池原始数据（降级处理）

    # ── 热门板块 ─────────────────────────────────────────────────
    hot = _hot(pool_data.get('zt'))

    # ── 打分 ─────────────────────────────────────────────────────
    candidates = []
    for r, src in pool:
        code = str(r.get("代码",""))
        rt   = prices.get(code)                      # 实时数据（可能为None）
        real_chg   = rt['change_rate'] if rt else _f(r.get("涨跌幅"))
        real_price = rt['price']       if rt else _f(r.get("最新价"))
        candidates.append(_build(r, src, hot, real_chg, real_price))

    buyable     = sorted([c for c in candidates if c["buyable"]],
                         key=lambda x: x["score"], reverse=True)
    not_buyable = sorted([c for c in candidates if not c["buyable"]],
                         key=lambda x: x["score"], reverse=True)

    combined = buyable[:8] + not_buyable[:2]
    return {
        "data":    combined[:10],
        "qdate":   date,
        "total":   len(combined[:10]),
        "message": f"实时价格已更新 · 可买入 {len(buyable)} 只",
    }


def _build(r, source, hot, real_chg, real_price):
    on  = _i(r.get("炸板次数"))
    hs  = _f(r.get("换手率"))
    lbs = max(1, _i(r.get("连板数")))
    ind = str(r.get("所属行业","") or "")
    amt = _f(r.get("成交额"))
    mv  = _f(r.get("流通市值")) / 1e8

    # 真正可买入：炸板过 + 当前实时涨幅 < 9.5%（未在涨停价）
    buyable = (on >= 1) and (real_chg < 9.5)

    score, reasons = 50, []

    if buyable:
        if   3.0 <= real_chg <= 5.0: score += 15; reasons.append(f"涨幅适中")
        elif 5.0 <  real_chg <  9.5: score += 8
        else:                         score += 3
        if   on == 1: score += 20; reasons.append("炸板1次")
        elif on == 2: score += 13; reasons.append("炸板2次")
        elif on == 3: score += 6;  reasons.append("炸板3次")
        else:         score -= 5
    elif on >= 1:
        score -= 5; reasons.append("已回封涨停")
    else:
        reasons.append("封死涨停")

    if   7 <= hs <= 12: score += 18; reasons.append("换手活跃")
    elif 5 <= hs <  7 or 12 < hs <= 15: score += 10
    elif 3 <= hs <  5:  score += 5
    elif hs > 20:       score -= 5

    if   lbs == 2: score += 12; reasons.append("连2板")
    elif lbs == 3: score += 8;  reasons.append("连3板")
    elif lbs == 1: score += 6
    elif lbs >= 4: score += 2

    try:
        t = int(float(str(r.get("首次封板时间", 0))))
        if   t < 93500:   score += 12; reasons.append("开盘即封")
        elif t < 100000:  score += 8
        elif t < 113000:  score += 4
        elif t >= 140000: score -= 6
    except Exception: pass

    if ind and ind in hot: score += 10; reasons.append("热门板块")

    if   amt >= 5e8: score += 6
    elif amt >= 2e8: score += 3
    elif amt <  3e7: score -= 5

    score = max(5, min(99, round(score)))

    if   buyable and score >= 72: sug = "⭐ 强烈推荐，2:30可分批买入"
    elif buyable and score >= 60: sug = "👍 值得介入，注意分时走势"
    elif buyable:                 sug = "🔍 可少量试探，止损3%"
    elif on >= 1:                 sug = "🔒 已回封涨停，明日关注高开"
    else:                         sug = "📅 封死，明日关注是否高开"

    return {
        "code":          str(r.get("代码","")),
        "name":          str(r.get("名称","")),
        "industry":      ind,
        "change_rate":   round(real_chg, 2),   # ← 实时涨跌幅
        "price":         round(real_price, 2),  # ← 实时价格
        "turnover":      round(hs, 2),
        "vol_ratio":     0.0,
        "total_mv":      round(mv, 1),
        "amount":        round(amt, 0),
        "spd5":          0.0,
        "vol_increasing": buyable,
        "vol_label":     ("✅ 炸板可买入" if buyable else
                          "🔒 已回封涨停" if on >= 1 else "🔒 封死涨停"),
        "open_num":      on,
        "consec":        lbs,
        "score":         score,
        "reasons":       reasons[:4],
        "suggestion":    sug,
        "buyable":       buyable,
        "truly_buyable": buyable,
        "first_time":    ft(r.get("首次封板时间")),
        "last_time":     ft(r.get("最后封板时间")),
        "seal_fund":     _f(r.get("封板资金")),
        "zt_stat":       str(r.get("涨停统计","") or ""),
        "source":        source,
    }


# ── 热门板块 ────────────────────────────────────────────────────
def _hot(df=None):
    try:
        from collections import Counter
        if df is None:
            import akshare as ak
            df = ak.stock_zt_pool_em(date=last_trade_day())
        if df is not None and not df.empty:
            sc = Counter(df["所属行业"].dropna().tolist())
            return {k for k, v in sc.items() if v >= 2}
    except Exception: pass
    return set()


# ── 工具 ────────────────────────────────────────────────────────
def _f(v):
    try:   return float(v)
    except: return 0.0

def _i(v):
    try:   return int(float(v))
    except: return 0

def ft(val):
    try:
        s = str(int(float(str(val)))).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except: return ""

def bj_now():
    return datetime.utcnow() + timedelta(hours=8)

def normalize_date(s):
    if not s: return last_trade_day()
    c = s.replace("-","")
    return c if len(c) == 8 else last_trade_day()

def last_trade_day():
    d = bj_now()
    if d.hour < 15: d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
