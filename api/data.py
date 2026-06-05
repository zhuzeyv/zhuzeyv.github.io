"""
Vercel Python Serverless Function — api/data.py
GET /api/data?type=prev|zt|zb|recommend&date=YYYYMMDD
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
        qs_raw = parse_qs(urlparse(self.path).query)
        ptype  = qs_raw.get("type", ["prev"])[0].lower()
        # 推荐不缓存（实时价格），其他120秒
        if ptype == "recommend":
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "s-maxage=120, stale-while-revalidate=30")
        self.end_headers()

        result = {}
        try:
            date = normalize_date(qs_raw.get("date", [""])[0])
            if ptype == "recommend":
                result = handle_recommend()
            else:
                rows, source = fetch_zt(ptype, date)
                result = {
                    "data": rows, "total": len(rows), "qdate": date,
                    "source": source,
                    "message": "" if rows else f"{date} 暂无数据（可能是非交易日）",
                }
        except Exception as exc:
            result = {"data": [], "total": 0, "error": str(exc),
                      "trace": traceback.format_exc()}

        self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass


# ─────────────────────────────────────────────────────────────
# 涨停数据
# ─────────────────────────────────────────────────────────────
def fetch_zt(ptype, date):
    import akshare as ak
    label_map = {"zt": "今日涨停股池", "zb": "今日炸板股池", "prev": "昨日涨停股池"}
    if ptype == "zb":
        df = ak.stock_zt_pool_zbgc_em(date=date)
    elif ptype == "prev":
        df = ak.stock_zt_pool_previous_em(date=date)
    else:
        df = ak.stock_zt_pool_em(date=date)

    if df is None or df.empty:
        return [], f"AKShare · {label_map.get(ptype, ptype)}"
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "SECURITY_CODE": str(r.get("代码",  "") or ""),
            "SECURITY_NAME": str(r.get("名称",  "") or ""),
            "CHANGE_RATE":   _f(r.get("涨跌幅")),
            "CLOSE_PRICE":   _f(r.get("最新价")),
            "OPEN_NUM":      _i(r.get("炸板次数")),
            "FIRST_TIME":    fmt_t(r.get("首次封板时间")),
            "LAST_TIME":     fmt_t(r.get("最后封板时间")),
            "TURNOVERRATE":  _f(r.get("换手率")),
            "DEAL_AMOUNT":   _f(r.get("成交额")),
            "FLOAT_MV":      _f(r.get("流通市值")),
            "CONSEC_BOARDS": _i(r.get("连板数")),
            "INDUSTRY":      str(r.get("所属行业", "") or ""),
            "ZT_STAT":       str(r.get("涨停统计", "") or ""),
            "SEAL_FUND":     _f(r.get("封板资金")),
        })
    return rows, f"AKShare · {label_map.get(ptype, ptype)}"


# ─────────────────────────────────────────────────────────────
# 新浪财经批量实时价格（快，只查候选股）
# ─────────────────────────────────────────────────────────────
def get_sina_prices(codes):
    """
    输入股票代码列表，返回 {code: {price, change_rate}} 实时数据
    比 stock_zh_a_spot_em 快很多，只请求需要的股票
    """
    import requests

    sina_list = []
    for c in codes:
        c = str(c)
        if c.startswith('6') or c.startswith('5'):
            sina_list.append(f'sh{c}')
        elif c.startswith('8') or c.startswith('4'):
            sina_list.append(f'bj{c}')
        else:
            sina_list.append(f'sz{c}')

    result = {}
    # 每批50只，避免URL过长
    batch_size = 50
    for i in range(0, len(sina_list), batch_size):
        batch = sina_list[i:i + batch_size]
        url = f"https://hq.sinajs.cn/list={','.join(batch)}"
        try:
            resp = requests.get(url, headers={
                'Referer': 'https://finance.sina.com.cn',
                'User-Agent': 'Mozilla/5.0',
            }, timeout=8)
            for line in resp.text.strip().split('\n'):
                if '="' not in line:
                    continue
                key = line.split('=')[0].strip()
                # 去掉前缀得到6位代码
                code = (key.replace('var hq_str_sh', '')
                           .replace('var hq_str_sz', '')
                           .replace('var hq_str_bj', ''))
                val = line.split('"')[1] if '"' in line else ''
                if not val:
                    continue
                fields = val.split(',')
                if len(fields) < 10:
                    continue
                try:
                    prev_close = float(fields[2])
                    cur_price  = float(fields[3])
                    if prev_close > 0:
                        chg = (cur_price - prev_close) / prev_close * 100
                    else:
                        chg = 0.0
                    result[code] = {
                        'price':       round(cur_price, 2),
                        'change_rate': round(chg, 2),
                    }
                except Exception:
                    pass
        except Exception:
            pass
    return result


# ─────────────────────────────────────────────────────────────
# 短线推荐
# ─────────────────────────────────────────────────────────────
def handle_recommend():
    import akshare as ak

    date = last_trade_day()
    hot  = _hot_sectors(date)
    seen = set()
    pool = []   # 原始候选（涨停池数据）

    # A. 今日涨停池
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get("代码", ""))
                if code and code not in seen:
                    seen.add(code)
                    pool.append((r, "今日"))
    except Exception:
        pass

    # B. 今日炸板专池（补充）
    try:
        df2 = ak.stock_zt_pool_zbgc_em(date=date)
        if df2 is not None and not df2.empty:
            for _, r in df2.iterrows():
                code = str(r.get("代码", ""))
                if code and code not in seen:
                    seen.add(code)
                    pool.append((r, "今日炸板"))
    except Exception:
        pass

    # C. 昨日涨停池（昨炸板未回封的延续候选）
    try:
        df3 = ak.stock_zt_pool_previous_em(date=date)
        if df3 is not None and not df3.empty:
            for _, r in df3.iterrows():
                code = str(r.get("代码", ""))
                if code and code not in seen:
                    seen.add(code)
                    pool.append((r, "昨日涨停"))
    except Exception:
        pass

    if not pool:
        return {"data": [], "qdate": date, "total": 0,
                "message": "暂无数据，请在交易日 09:30 后使用"}

    # ── 用新浪实时接口更新当前价格 ───────────────────────────────
    codes = [str(r.get("代码", "")) for r, _ in pool]
    real_prices = {}
    try:
        real_prices = get_sina_prices(codes)
    except Exception:
        pass  # 失败则用涨停池原始数据

    # ── 构建候选并打分 ────────────────────────────────────────────
    candidates = []
    for (r, source) in pool:
        code = str(r.get("代码", ""))
        # 优先用新浪实时价格，否则用涨停池数据
        if code in real_prices:
            real_chg   = real_prices[code]['change_rate']
            real_price = real_prices[code]['price']
        else:
            real_chg   = _f(r.get("涨跌幅"))
            real_price = _f(r.get("最新价"))

        item = _build(r, source, hot, real_chg, real_price)
        candidates.append(item)

    # ── 换手率粗筛 & 分类 ────────────────────────────────────────
    candidates = [c for c in candidates if 3 <= c["turnover"] <= 20]

    buyable     = [c for c in candidates if c["truly_buyable"]]
    not_buyable = [c for c in candidates if not c["truly_buyable"]]

    buyable.sort(    key=lambda x: x["score"], reverse=True)
    not_buyable.sort(key=lambda x: x["score"], reverse=True)

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
    ind = str(r.get("所属行业", "") or "")
    amt = _f(r.get("成交额"))
    mv  = _f(r.get("流通市值")) / 1e8

    # ── 核心判断：用实时涨跌幅 ──────────────────────────────────
    # 真正可买入：炸板过 AND 当前实时涨幅 < 9.5%（未回封涨停）
    truly_buyable = (on >= 1) and (real_chg < 9.5)

    score   = 50
    reasons = []

    if truly_buyable:
        # 涨幅位置越好（3-6%）越有次日冲高空间
        if 3.0 <= real_chg <= 5.0:
            score += 15; reasons.append(f"涨幅适中{real_chg:.1f}%")
        elif 5.0 < real_chg < 9.5:
            score += 8
        elif real_chg < 3.0:
            score += 3

        if on == 1:
            score += 20; reasons.append("炸板1次")
        elif on == 2:
            score += 13; reasons.append("炸板2次")
        elif on == 3:
            score += 6;  reasons.append("炸板3次")
        else:
            score -= 5
    elif on >= 1 and real_chg >= 9.5:
        score -= 5; reasons.append("已回封涨停")
    else:
        reasons.append("封死涨停")

    # 换手率
    if   7 <= hs <= 12: score += 18; reasons.append("换手活跃")
    elif 5 <= hs <  7 or 12 < hs <= 15: score += 10
    elif 3 <= hs <  5:  score += 5
    elif hs > 20:       score -= 5

    # 连板
    if   lbs == 2: score += 12; reasons.append("连2板")
    elif lbs == 3: score += 8;  reasons.append("连3板")
    elif lbs == 1: score += 6
    elif lbs >= 4: score += 2

    # 首封时间
    try:
        t = int(float(str(r.get("首次封板时间", 0))))
        if   t < 93500:   score += 12; reasons.append("开盘即封")
        elif t < 100000:  score += 8
        elif t < 113000:  score += 4
        elif t >= 140000: score -= 6
    except Exception:
        pass

    # 热门板块
    if ind and ind in hot:
        score += 10; reasons.append("热门板块")

    # 成交额
    if   amt >= 5e8: score += 6
    elif amt >= 2e8: score += 3
    elif amt <  3e7: score -= 5

    score = max(5, min(99, round(score)))

    # 建议
    if truly_buyable and score >= 72:
        sug = "⭐ 强烈推荐，2:30可分批买入"
    elif truly_buyable and score >= 60:
        sug = "👍 值得介入，注意分时走势"
    elif truly_buyable:
        sug = "🔍 可少量试探，止损3%"
    elif on >= 1 and real_chg >= 9.5:
        sug = "🔒 已回封涨停，暂无法买入，明日关注高开"
    else:
        sug = "📅 封死，明日关注是否高开"

    vol_label = (
        "✅ 炸板可买入" if truly_buyable else
        "🔒 已回封涨停" if (on >= 1 and real_chg >= 9.5) else
        "🔒 封死涨停"
    )

    return {
        "code":          str(r.get("代码", "")),
        "name":          str(r.get("名称", "")),
        "industry":      ind,
        "change_rate":   round(real_chg, 2),    # 实时涨跌幅
        "price":         round(real_price, 2),   # 实时价格
        "turnover":      round(hs, 2),
        "vol_ratio":     0.0,
        "total_mv":      round(mv, 1),
        "amount":        round(amt, 0),
        "spd5":          0.0,
        "vol_increasing": truly_buyable,
        "vol_label":     vol_label,
        "open_num":      on,
        "consec":        lbs,
        "score":         score,
        "reasons":       reasons[:4],
        "suggestion":    sug,
        "buyable":       truly_buyable,
        "truly_buyable": truly_buyable,
        "first_time":    fmt_t(r.get("首次封板时间")),
        "last_time":     fmt_t(r.get("最后封板时间")),
        "seal_fund":     _f(r.get("封板资金")),
        "zt_stat":       str(r.get("涨停统计", "") or ""),
        "source":        source,
    }


# ─────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────
def _hot_sectors(date=None):
    try:
        import akshare as ak
        from collections import Counter
        d  = date or last_trade_day()
        df = ak.stock_zt_pool_em(date=d)
        if df is not None and not df.empty:
            sc = Counter(df["所属行业"].dropna().tolist())
            return {k for k, v in sc.items() if v >= 2}
    except Exception:
        pass
    return set()

def _f(v):
    try:   return float(v)
    except: return 0.0

def _i(v):
    try:   return int(float(v))
    except: return 0

def fmt_t(val):
    try:
        s = str(int(float(str(val)))).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except: return ""

def bj_now():
    return datetime.utcnow() + timedelta(hours=8)

def normalize_date(s):
    if not s: return last_trade_day()
    c = s.replace("-", "")
    return c if len(c) == 8 else last_trade_day()

def last_trade_day():
    d = bj_now()
    if d.hour < 15: d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
