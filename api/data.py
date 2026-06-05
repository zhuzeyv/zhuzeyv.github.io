# -*- coding: utf-8 -*-
"""
Vercel Python Serverless Function - api/data.py
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
        qs = parse_qs(urlparse(self.path).query)
        ptype = qs.get("type", ["prev"])[0].lower()
        if ptype == "recommend":
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "s-maxage=120, stale-while-revalidate=30")
        self.end_headers()

        try:
            date = normalize_date(qs.get("date", [""])[0])
            if ptype == "recommend":
                resp = handle_recommend()
            else:
                rows, source = fetch_zt(ptype, date)
                resp = {
                    "data": rows,
                    "total": len(rows),
                    "qdate": date,
                    "source": source,
                    "message": "" if rows else date + " 暂无数据",
                }
        except Exception as e:
            resp = {
                "data": [],
                "total": 0,
                "error": str(e),
                "trace": traceback.format_exc(),
            }

        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass


# ─── 涨停主数据 ────────────────────────────────────────────────
def fetch_zt(ptype, date):
    import akshare as ak
    label_map = {
        "zt":   "今日涨停股池",
        "zb":   "今日炸板股池",
        "prev": "昨日涨停股池",
    }
    if ptype == "zb":
        df = ak.stock_zt_pool_zbgc_em(date=date)
    elif ptype == "prev":
        df = ak.stock_zt_pool_previous_em(date=date)
    else:
        df = ak.stock_zt_pool_em(date=date)

    if df is None or df.empty:
        return [], "AKShare - " + label_map.get(ptype, ptype)

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "SECURITY_CODE": str(r.get("代码", "") or ""),
            "SECURITY_NAME": str(r.get("名称", "") or ""),
            "CHANGE_RATE":   safe_float(r.get("涨跌幅")),
            "CLOSE_PRICE":   safe_float(r.get("最新价")),
            "OPEN_NUM":      safe_int(r.get("炸板次数")),
            "FIRST_TIME":    fmt_time(r.get("首次封板时间")),
            "LAST_TIME":     fmt_time(r.get("最后封板时间")),
            "TURNOVERRATE":  safe_float(r.get("换手率")),
            "DEAL_AMOUNT":   safe_float(r.get("成交额")),
            "FLOAT_MV":      safe_float(r.get("流通市值")),
            "CONSEC_BOARDS": safe_int(r.get("连板数")),
            "INDUSTRY":      str(r.get("所属行业", "") or ""),
            "ZT_STAT":       str(r.get("涨停统计", "") or ""),
            "SEAL_FUND":     safe_float(r.get("封板资金")),
        })
    return rows, "AKShare - " + label_map.get(ptype, ptype)


# ─── 实时价格（东方财富批量接口）──────────────────────────────
def get_realtime_prices(codes):
    """
    codes: list of 6-digit stock codes
    returns: dict {code_str: {"price": float, "change_rate": float}}
    """
    import requests

    if not codes:
        return {}

    secids = []
    for c in codes:
        c = str(c)
        if c.startswith("6") or c.startswith("5"):
            secids.append("1." + c)
        else:
            secids.append("0." + c)

    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fltt": "2",
        "invt": "2",
        "secids": ",".join(secids),
        "fields": "f2,f3,f57",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    headers = {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    result = {}
    # diff can be dict {"0": {...}, "1": {...}} or list [{...}, {...}]
    raw_diff = (data.get("data") or {}).get("diff", {})
    if isinstance(raw_diff, dict):
        items = list(raw_diff.values())
    elif isinstance(raw_diff, list):
        items = raw_diff
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("f57", ""))
        price = item.get("f2")
        chg = item.get("f3")
        if not code:
            continue
        try:
            p = float(price)
            c = float(chg)
            if p > 0:
                result[code] = {"price": round(p, 2), "change_rate": round(c, 2)}
        except (TypeError, ValueError):
            pass

    return result


# ─── 短线推荐 ──────────────────────────────────────────────────
def handle_recommend():
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor

    date = last_trade_day()
    pool_results = {"zt": None, "zb": None, "prev": None}

    def fetch_pool(key, fn, d):
        try:
            df = fn(date=d)
            pool_results[key] = df if (df is not None and not df.empty) else None
        except Exception:
            pool_results[key] = None

    # 并行拉取三个池
    with ThreadPoolExecutor(max_workers=3) as ex:
        ex.submit(fetch_pool, "zt",   ak.stock_zt_pool_em,           date)
        ex.submit(fetch_pool, "zb",   ak.stock_zt_pool_zbgc_em,      date)
        ex.submit(fetch_pool, "prev", ak.stock_zt_pool_previous_em,   date)

    # 合并去重
    seen = set()
    candidates = []
    source_labels = {"zt": "今日涨停", "zb": "今日炸板", "prev": "昨日涨停"}
    for key in ("zt", "zb", "prev"):
        df = pool_results[key]
        if df is None:
            continue
        for _, r in df.iterrows():
            code = str(r.get("代码", ""))
            if code and code not in seen:
                seen.add(code)
                candidates.append((r, source_labels[key]))

    if not candidates:
        return {
            "data": [],
            "qdate": date,
            "total": 0,
            "message": "暂无数据，请在交易日 09:30 后使用",
        }

    # 换手率粗筛
    candidates = [
        (r, s) for r, s in candidates
        if 3 <= safe_float(r.get("换手率")) <= 20
    ]

    # 获取实时价格
    codes = [str(r.get("代码", "")) for r, _ in candidates]
    prices = {}
    try:
        prices = get_realtime_prices(codes)
    except Exception:
        pass

    # 热门板块
    hot = set()
    try:
        zt_df = pool_results.get("zt")
        if zt_df is not None and not zt_df.empty:
            from collections import Counter
            sc = Counter(zt_df["所属行业"].dropna().tolist())
            hot = {k for k, v in sc.items() if v >= 2}
    except Exception:
        pass

    # 评分
    buyable = []
    not_buyable = []
    for r, src in candidates:
        code = str(r.get("代码", ""))
        rt = prices.get(code)
        real_chg = rt["change_rate"] if rt else safe_float(r.get("涨跌幅"))
        real_price = rt["price"] if rt else safe_float(r.get("最新价"))
        item = build_item(r, src, hot, real_chg, real_price)
        if item["buyable"]:
            buyable.append(item)
        else:
            not_buyable.append(item)

    buyable.sort(key=lambda x: x["score"], reverse=True)
    not_buyable.sort(key=lambda x: x["score"], reverse=True)

    combined = (buyable[:8] + not_buyable[:2])[:10]
    price_updated = len(prices) > 0

    return {
        "data": combined,
        "qdate": date,
        "total": len(combined),
        "message": ("实时价格已更新" if price_updated else "使用涨停池数据（实时价格获取失败）")
                   + " - 可买入 " + str(len(buyable)) + " 只",
    }


def build_item(r, source, hot, real_chg, real_price):
    on = safe_int(r.get("炸板次数"))
    hs = safe_float(r.get("换手率"))
    lbs = max(1, safe_int(r.get("连板数")))
    ind = str(r.get("所属行业", "") or "")
    amt = safe_float(r.get("成交额"))
    mv = safe_float(r.get("流通市值")) / 1e8

    # 真正可买入：炸板过 且 当前实时涨幅 < 9.5%（未在涨停价）
    buyable = (on >= 1) and (real_chg < 9.5)

    score = 50
    reasons = []

    if buyable:
        if 3.0 <= real_chg <= 5.0:
            score += 15
            reasons.append("涨幅适中")
        elif 5.0 < real_chg < 9.5:
            score += 8
        else:
            score += 3

        if on == 1:
            score += 20
            reasons.append("炸板1次")
        elif on == 2:
            score += 13
            reasons.append("炸板2次")
        elif on == 3:
            score += 6
            reasons.append("炸板3次")
        else:
            score -= 5
    elif on >= 1:
        score -= 5
        reasons.append("已回封涨停")
    else:
        reasons.append("封死涨停")

    if 7 <= hs <= 12:
        score += 18
        reasons.append("换手活跃")
    elif 5 <= hs < 7 or 12 < hs <= 15:
        score += 10
    elif 3 <= hs < 5:
        score += 5
    elif hs > 20:
        score -= 5

    if lbs == 2:
        score += 12
        reasons.append("连2板")
    elif lbs == 3:
        score += 8
        reasons.append("连3板")
    elif lbs == 1:
        score += 6
    elif lbs >= 4:
        score += 2

    try:
        t = int(float(str(r.get("首次封板时间", 0))))
        if t < 93500:
            score += 12
            reasons.append("开盘即封")
        elif t < 100000:
            score += 8
        elif t < 113000:
            score += 4
        elif t >= 140000:
            score -= 6
    except Exception:
        pass

    if ind and ind in hot:
        score += 10
        reasons.append("热门板块")

    if amt >= 5e8:
        score += 6
    elif amt >= 2e8:
        score += 3
    elif amt < 3e7:
        score -= 5

    score = max(5, min(99, round(score)))

    if buyable and score >= 72:
        sug = "强烈推荐，2:30可分批买入"
    elif buyable and score >= 60:
        sug = "值得介入，注意分时走势"
    elif buyable:
        sug = "可少量试探，止损3%"
    elif on >= 1:
        sug = "已回封涨停，明日关注高开"
    else:
        sug = "封死，明日关注是否高开"

    vol_label = "炸板可买入" if buyable else ("已回封涨停" if on >= 1 else "封死涨停")

    return {
        "code": str(r.get("代码", "")),
        "name": str(r.get("名称", "")),
        "industry": ind,
        "change_rate": round(real_chg, 2),
        "price": round(real_price, 2),
        "turnover": round(hs, 2),
        "vol_ratio": 0.0,
        "total_mv": round(mv, 1),
        "amount": round(amt, 0),
        "spd5": 0.0,
        "vol_increasing": buyable,
        "vol_label": vol_label,
        "open_num": on,
        "consec": lbs,
        "score": score,
        "reasons": reasons[:4],
        "suggestion": sug,
        "buyable": buyable,
        "truly_buyable": buyable,
        "first_time": fmt_time(r.get("首次封板时间")),
        "last_time": fmt_time(r.get("最后封板时间")),
        "seal_fund": safe_float(r.get("封板资金")),
        "zt_stat": str(r.get("涨停统计", "") or ""),
        "source": source,
    }


# ─── 工具函数 ──────────────────────────────────────────────────
def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def safe_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fmt_time(val):
    try:
        s = str(int(float(str(val)))).zfill(6)
        return s[:2] + ":" + s[2:4]
    except Exception:
        return ""


def bj_now():
    return datetime.utcnow() + timedelta(hours=8)


def normalize_date(s):
    if not s:
        return last_trade_day()
    c = s.replace("-", "")
    return c if len(c) == 8 else last_trade_day()


def last_trade_day():
    d = bj_now()
    mins = d.hour * 60 + d.minute
    # 只有在 09:25 之前（市场尚未开盘）才取上一个交易日
    # 09:25 到午夜：取今天（交易中或已收盘）
    if mins < 9 * 60 + 25:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
