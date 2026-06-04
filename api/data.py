"""
Vercel Python Serverless Function — api/data.py
GET /api/data?type=prev|zt|zb|recommend&date=YYYYMMDD
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
import json, traceback


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._cors(); self.end_headers()

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
                resp = {"data": data, "total": len(data), "qdate": date,
                        "source": source,
                        "message": "" if data else f"{date} 暂无数据（可能是非交易日）"}
        except Exception as e:
            resp = {"data": [], "total": 0, "error": str(e), "trace": traceback.format_exc()}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())

    def _cors(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def log_message(self, fmt, *args): pass


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
            "SECURITY_CODE": str(r.get("代码","") or ""),
            "SECURITY_NAME": str(r.get("名称","") or ""),
            "CHANGE_RATE":   _f(r.get("涨跌幅")),
            "CLOSE_PRICE":   _f(r.get("最新价")),
            "OPEN_NUM":      _i(r.get("炸板次数")),
            "FIRST_TIME":    fmt_time(r.get("首次封板时间")),
            "LAST_TIME":     fmt_time(r.get("最后封板时间")),
            "TURNOVERRATE":  _f(r.get("换手率")),
            "DEAL_AMOUNT":   _f(r.get("成交额")),
            "FLOAT_MV":      _f(r.get("流通市值")),
            "CONSEC_BOARDS": _i(r.get("连板数")),
            "INDUSTRY":      str(r.get("所属行业","") or ""),
            "ZT_STAT":       str(r.get("涨停统计","") or ""),
            "SEAL_FUND":     _f(r.get("封板资金")),
        })
    return rows, f"AKShare · {labels.get(ptype, ptype)}"


# ── 短线推荐（2:30买入策略）────────────────────────────────────
def get_recommend():
    """
    策略核心：买在炸板后跌回来的股票（2:30可以成交）
    封死涨停 = 买不进 → 评分大幅降低
    炸板开板 = 可以买 → 优先推荐
    """
    import akshare as ak
    from collections import Counter

    date = last_trade_day()
    rows = []

    # 今日涨停池（含炸板）
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                rows.append(_to_rec(r, "今日"))
    except Exception:
        pass

    # 昨日涨停池（今日可能有延续）
    try:
        df2 = ak.stock_zt_pool_previous_em(date=date)
        if df2 is not None and not df2.empty:
            exist = {r["code"] for r in rows}
            for _, r in df2.iterrows():
                code = str(r.get("代码",""))
                if code not in exist:
                    rows.append(_to_rec(r, "昨日"))
    except Exception:
        pass

    if not rows:
        return {"data": [], "qdate": date, "total": 0,
                "message": "暂无数据（交易日09:30后方有数据）"}

    # 换手率粗筛
    rows = [r for r in rows if 3 <= r["turnover"] <= 20]

    # 热门板块（涨停数≥2的板块）
    sector_cnt = Counter(r["industry"] for r in rows if r["industry"])
    hot = {k: v for k, v in sector_cnt.items() if v >= 2}

    # 评分
    for s in rows:
        s["score"], s["reasons"], s["suggestion"], s["buyable"] = _score_230(s, hot)

    # 优先展示可买入的股票，再按分数排
    rows.sort(key=lambda x: (x["buyable"], x["score"]), reverse=True)

    return {"data": rows[:10], "qdate": date, "total": len(rows[:10]),
            "message": ""}


def _to_rec(r, source):
    return {
        "code":        str(r.get("代码","") or ""),
        "name":        str(r.get("名称","") or ""),
        "industry":    str(r.get("所属行业","") or ""),
        "change_rate": _f(r.get("涨跌幅")),
        "price":       _f(r.get("最新价")),
        "turnover":    _f(r.get("换手率")),
        "open_num":    _i(r.get("炸板次数")),
        "consec":      max(1, _i(r.get("连板数"))),
        "first_time":  fmt_time(r.get("首次封板时间")),
        "last_time":   fmt_time(r.get("最后封板时间")),
        "seal_fund":   _f(r.get("封板资金")),
        "amount":      _f(r.get("成交额")),
        "zt_stat":     str(r.get("涨停统计","") or ""),
        "source":      source,
        "score": 0, "reasons": [], "suggestion": "", "buyable": False,
    }


def _score_230(s, hot):
    """
    2:30买入评分逻辑：
    - 炸板开板的股票（open_num>=1）才能买入
    - 封死涨停（open_num=0）无法成交，评分大幅降低并标注"无法买入"
    """
    score = 50
    reasons = []
    buyable = False

    on = s["open_num"]

    # ── 核心：是否可以买入 ───────────────────────────────────
    if on == 0:
        # 封死涨停，2:30根本买不进
        score -= 30
        reasons.append("封死无法买入")
        buyable = False
    elif on == 1:
        # 炸板1次，目前在涨停下方，可以买入，次日有冲板预期
        score += 25
        reasons.append("炸板可买入")
        buyable = True
    elif on == 2:
        score += 15
        reasons.append("炸2次可买入")
        buyable = True
    elif on == 3:
        score += 5
        reasons.append("炸3次偏弱")
        buyable = True
    else:
        score -= 10
        reasons.append(f"炸{on}次较弱")
        buyable = True

    # ── 换手率（活跃度）────────────────────────────────────
    hs = s["turnover"]
    if 8 <= hs <= 15:
        score += 18; reasons.append("换手活跃")
    elif 5 <= hs < 8 or 15 < hs <= 18:
        score += 10
    elif 3 <= hs < 5:
        score += 4
    else:
        score -= 5

    # ── 连板（主力持续意愿）────────────────────────────────
    lbs = s["consec"]
    if lbs == 2:
        score += 12; reasons.append("连2板强势")
    elif lbs == 3:
        score += 8;  reasons.append("连3板")
    elif lbs == 1:
        score += 6
    elif lbs >= 4:
        score += 2   # 高位风险

    # ── 首封时间（越早说明主力越积极）──────────────────────
    ft = s["first_time"]
    try:
        h, m = int(ft[:2]), int(ft[3:5])
        mins = h * 60 + m
        if mins <= 9 * 60 + 35:
            score += 12; reasons.append("早盘封板")
        elif mins <= 10 * 60:
            score += 8
        elif mins <= 11 * 60 + 30:
            score += 4
        elif mins >= 14 * 60:
            score -= 5
    except Exception:
        pass

    # ── 热门板块 ────────────────────────────────────────────
    cnt = hot.get(s["industry"], 0)
    if cnt >= 4:
        score += 15; reasons.append("超热板块")
    elif cnt >= 2:
        score += 8;  reasons.append("热门板块")

    # ── 成交额（流动性）────────────────────────────────────
    amt = s["amount"]
    if amt >= 5e8:
        score += 8
    elif amt >= 2e8:
        score += 4
    elif amt < 5e7:
        score -= 5   # 流动性太差，难出货

    score = max(5, min(99, round(score)))

    # 操作建议
    if not buyable:
        sug = "🚫 封死涨停，2:30无法买入"
    elif score >= 75:
        sug = "⭐ 强烈关注！炸板+热门板块，次日冲板概率高"
    elif score >= 62:
        sug = "👍 值得关注，可在2:30附近轻仓买入"
    elif score >= 50:
        sug = "🔍 一般，可小仓位试探，注意止损"
    else:
        sug = "⚠️ 偏弱，风险较大，建议观望"

    return score, reasons[:4], sug, buyable


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
    except: return ""

def normalize_date(s):
    if not s: return last_trade_day()
    c = s.replace("-","")
    return c if len(c) == 8 else last_trade_day()

def last_trade_day():
    d = datetime.utcnow() + timedelta(hours=8)
    if d.hour < 15: d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
