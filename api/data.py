"""
Vercel Python Serverless Function — api/data.py
GET /api/data?type=prev&date=20260512   → 涨停数据
GET /api/data?type=recommend            → 短线推荐

type: prev=昨日涨停  zt=今日涨停  zb=今日炸板  recommend=短线推荐
date: YYYYMMDD 或 YYYY-MM-DD（可选）
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
import json
import traceback


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=180, stale-while-revalidate=30")
        self.end_headers()

        try:
            qs    = parse_qs(urlparse(self.path).query)
            ptype = qs.get("type", ["prev"])[0].lower()
            date  = normalize_date(qs.get("date", [""])[0])

            if ptype == "recommend":
                resp = get_recommend()
            else:
                data, source = fetch(ptype, date)
                resp = {
                    "data":    data,
                    "total":   len(data),
                    "qdate":   date,
                    "source":  source,
                    "message": "" if data else f"{date} 暂无数据（可能是非交易日）",
                }

        except Exception as e:
            resp = {"data": [], "total": 0, "error": str(e), "trace": traceback.format_exc()}

        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())

    def _cors(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def log_message(self, fmt, *args):
        pass


# ── 涨停数据 ──────────────────────────────────────────────────
def fetch(ptype, date):
    import akshare as ak

    labels = {"zt": "今日涨停股池", "zb": "今日炸板股池", "prev": "昨日涨停股池"}

    if ptype == "zb":
        df = ak.stock_zt_pool_zbgc_em(date=date)
    elif ptype == "prev":
        df = ak.stock_zt_pool_previous_em(date=date)
    else:
        df = ak.stock_zt_pool_em(date=date)

    if df is None or df.empty:
        return [], f"AKShare · {labels.get(ptype, ptype)}"

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "SECURITY_CODE": str(r.get("代码", "") or ""),
            "SECURITY_NAME": str(r.get("名称", "") or ""),
            "CHANGE_RATE":   _f(r.get("涨跌幅")),
            "CLOSE_PRICE":   _f(r.get("最新价")),
            "OPEN_NUM":      _i(r.get("炸板次数")),
            "FIRST_TIME":    fmt_time(r.get("首次封板时间")),
            "LAST_TIME":     fmt_time(r.get("最后封板时间")),
            "TURNOVERRATE":  _f(r.get("换手率")),
            "DEAL_AMOUNT":   _f(r.get("成交额")),
            "FLOAT_MV":      _f(r.get("流通市值")),
            "CONSEC_BOARDS": _i(r.get("连板数")),
            "INDUSTRY":      str(r.get("所属行业", "") or ""),
            "ZT_STAT":       str(r.get("涨停统计", "") or ""),
            "SEAL_FUND":     _f(r.get("封板资金")),
        })
    return rows, f"AKShare · {labels.get(ptype, ptype)}"


# ── 短线推荐 ──────────────────────────────────────────────────
def get_recommend():
    import akshare as ak
    from collections import Counter

    date = last_trade_day()
    rows = []

    # 今日涨停池
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                rows.append(_to_rec(r, "今日涨停"))
    except Exception:
        pass

    # 昨日涨停池（补充候选）
    try:
        df2 = ak.stock_zt_pool_previous_em(date=date)
        if df2 is not None and not df2.empty:
            exist = {r["code"] for r in rows}
            for _, r in df2.iterrows():
                code = str(r.get("代码", ""))
                if code not in exist:
                    rows.append(_to_rec(r, "昨日涨停"))
    except Exception:
        pass

    if not rows:
        return {"data": [], "qdate": date, "total": 0, "message": "暂无数据"}

    # 换手率粗筛 3-15%
    rows = [r for r in rows if 3 <= r["turnover"] <= 15]

    # 热门板块统计
    sector_cnt = Counter(r["industry"] for r in rows if r["industry"])
    hot = {k: v for k, v in sector_cnt.items() if v >= 2}

    # 评分
    for s in rows:
        s["score"], s["reasons"], s["suggestion"] = _score(s, hot)

    rows.sort(key=lambda x: x["score"], reverse=True)

    return {"data": rows[:10], "qdate": date, "total": len(rows[:10]), "message": ""}


def _to_rec(r, source):
    return {
        "code":        str(r.get("代码", "")),
        "name":        str(r.get("名称", "")),
        "industry":    str(r.get("所属行业", "") or ""),
        "change_rate": _f(r.get("涨跌幅")),
        "price":       _f(r.get("最新价")),
        "turnover":    _f(r.get("换手率")),
        "open_num":    _i(r.get("炸板次数")),
        "consec":      _i(r.get("连板数")) or 1,
        "first_time":  fmt_time(r.get("首次封板时间")),
        "last_time":   fmt_time(r.get("最后封板时间")),
        "seal_fund":   _f(r.get("封板资金")),
        "amount":      _f(r.get("成交额")),
        "zt_stat":     str(r.get("涨停统计", "") or ""),
        "source":      source,
        "score":       0,
        "reasons":     [],
        "suggestion":  "",
    }


def _score(s, hot):
    score = 50
    reasons = []

    # 换手率（7-10% 甜蜜区）
    hs = s["turnover"]
    if 7 <= hs <= 10:
        score += 20; reasons.append("换手甜蜜区")
    elif 5 <= hs < 7 or 10 < hs <= 12:
        score += 12
    elif 3 <= hs < 5 or 12 < hs <= 15:
        score += 5
    else:
        score -= 8

    # 封板质量
    on = s["open_num"]
    if on == 0:
        score += 15; reasons.append("封死稳健")
    elif on == 1:
        score += 5;  reasons.append("炸板1次")
    elif on == 2:
        score -= 5
    else:
        score -= 15

    # 连板（2板最优）
    lbs = s["consec"]
    if lbs == 2:
        score += 15; reasons.append("连2板")
    elif lbs == 3:
        score += 10; reasons.append("连3板")
    elif lbs == 1:
        score += 8
    elif lbs >= 4:
        score += 2

    # 首封时间
    ft = s["first_time"]
    try:
        h, m = int(ft[:2]), int(ft[3:5])
        mins = h * 60 + m
        if mins <= 9 * 60 + 35:
            score += 15; reasons.append("开盘即封")
        elif mins <= 10 * 60:
            score += 10
        elif mins <= 11 * 60 + 30:
            score += 5
        elif mins >= 14 * 60:
            score -= 8
    except Exception:
        pass

    # 热门板块
    cnt = hot.get(s["industry"], 0)
    if cnt >= 4:
        score += 15; reasons.append("超热板块")
    elif cnt >= 2:
        score += 8;  reasons.append("热门板块")

    # 封板资金
    fund = s["seal_fund"]
    if fund >= 5e8:
        score += 10; reasons.append("资金充裕")
    elif fund >= 2e8:
        score += 5

    if s["source"] == "今日涨停":
        score += 5

    score = max(5, min(99, round(score)))

    if score >= 75 and on == 0:
        sug = "⭐ 强烈关注，封死稳健，次日溢价概率高"
    elif score >= 65:
        sug = "👍 值得关注，注意次日高开走势"
    elif score >= 55:
        sug = "🔍 可以关注，控制仓位"
    else:
        sug = "⚠️ 风险偏大，谨慎参与"

    return score, reasons[:4], sug


# ── 工具函数 ──────────────────────────────────────────────────
def _f(v):
    try:   return float(v)
    except: return 0.0

def _i(v):
    try:   return int(float(v))
    except: return 0

def fmt_time(val):
    try:
        s = str(int(float(str(val)))).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except:
        return ""

def normalize_date(s):
    if not s:
        return last_trade_day()
    clean = s.replace("-", "")
    return clean if len(clean) == 8 else last_trade_day()

def last_trade_day():
    d = datetime.utcnow() + timedelta(hours=8)
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
