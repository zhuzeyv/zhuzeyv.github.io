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
        self.send_header("Cache-Control", "s-maxage=180, stale-while-revalidate=30")
        self.end_headers()

        result = {}
        try:
            qs    = parse_qs(urlparse(self.path).query)
            ptype = qs.get("type", ["prev"])[0].lower()
            date  = normalize_date(qs.get("date", [""])[0])

            if ptype == "recommend":
                result = handle_recommend()
            else:
                rows, source = fetch_zt(ptype, date)
                result = {
                    "data":    rows,
                    "total":   len(rows),
                    "qdate":   date,
                    "source":  source,
                    "message": "" if rows else f"{date} 暂无数据（可能是非交易日）",
                }
        except Exception as exc:
            result = {
                "data":  [],
                "total": 0,
                "error": str(exc),
                "trace": traceback.format_exc(),
            }

        self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass


# ─────────────────────────────────────────────────────────────
# 涨停数据
# ─────────────────────────────────────────────────────────────
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
# 短线推荐
# 策略：用今日涨停池的炸板股，避免拉全市场5000+数据超时
# 炸板股 = 今日曾触涨停但已开板 = 当前价格在3-9%区间 = 2:30可买入
# ─────────────────────────────────────────────────────────────
def handle_recommend():
    import akshare as ak

    date = last_trade_day()
    hot  = _hot_sectors(date)
    rows_all = []
    rows_zb  = []   # 炸板股（可买入）
    rows_zt  = []   # 封死股（参考）

    # 今日涨停池（含炸板）
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                scored = _score(r, hot)
                if scored["open_num"] >= 1:
                    rows_zb.append(scored)
                else:
                    rows_zt.append(scored)
    except Exception:
        pass

    # 昨日涨停池补充（今日延续候选）
    try:
        df2 = ak.stock_zt_pool_previous_em(date=date)
        if df2 is not None and not df2.empty:
            exist = {r["code"] for r in rows_zb + rows_zt}
            for _, r in df2.iterrows():
                if str(r.get("代码","")) not in exist:
                    scored = _score(r, hot)
                    scored["source"] = "昨日涨停"
                    if scored["open_num"] >= 1:
                        rows_zb.append(scored)
                    else:
                        rows_zt.append(scored)
    except Exception:
        pass

    if not rows_zb and not rows_zt:
        return {
            "data": [], "qdate": date, "total": 0,
            "message": "暂无数据，请确认今日为交易日且已开盘",
        }

    # 换手率粗筛（3-20%）
    rows_zb = [r for r in rows_zb if 3 <= r["turnover"] <= 20]
    rows_zt = [r for r in rows_zt if 3 <= r["turnover"] <= 20]

    # 炸板股优先，封死股补位
    rows_zb.sort(key=lambda x: x["score"], reverse=True)
    rows_zt.sort(key=lambda x: x["score"], reverse=True)
    combined = rows_zb[:8] + rows_zt[:2]

    # 补充 2:30 建议
    for r in combined:
        if r.get("open_num", 0) >= 1:
            r["suggestion"] = "✅ 炸板可买入｜" + r.get("suggestion", "")
        else:
            r["suggestion"] = "⚠️ 封死暂不可买｜关注次日高开｜" + r.get("suggestion", "")

    return {
        "data":    combined[:10],
        "qdate":   date,
        "total":   len(combined[:10]),
        "message": "",
    }


def _score(r, hot):
    """
    2:30买入评分
    炸板股（OPEN_NUM>=1）才是真正可买入的
    封死股做参考（次日可能高开）
    """
    on  = _i(r.get("炸板次数"))
    hs  = _f(r.get("换手率"))
    lbs = max(1, _i(r.get("连板数")))
    ind = str(r.get("所属行业", "") or "")
    chg = _f(r.get("涨跌幅"))
    amt = _f(r.get("成交额"))
    mv  = _f(r.get("流通市值")) / 1e8

    score = 50
    reasons = []

    # ── 1. 是否可买入（最重要）─────────────────────────────────
    if on == 0:
        score += 0           # 封死，买不进
    elif on == 1:
        score += 20; reasons.append("炸板1次可买入")
    elif on == 2:
        score += 12; reasons.append("炸板2次")
    elif on == 3:
        score += 5;  reasons.append("炸板3次偏弱")
    else:
        score -= 8

    # ── 2. 换手率（活跃度）──────────────────────────────────────
    if   7 <= hs <= 12: score += 18; reasons.append("换手活跃")
    elif 5 <= hs <  7 or 12 < hs <= 15: score += 10
    elif 3 <= hs <  5:  score += 5
    elif hs > 20:       score -= 5

    # ── 3. 连板（主力持续）──────────────────────────────────────
    if   lbs == 2: score += 12; reasons.append("连2板")
    elif lbs == 3: score += 8;  reasons.append("连3板")
    elif lbs == 1: score += 6
    elif lbs >= 4: score += 2   # 高位谨慎

    # ── 4. 首次封板时间（越早越有主力）─────────────────────────
    raw_fbt = r.get("首次封板时间")
    try:
        t = int(float(str(raw_fbt)))
        if   t < 93500:  score += 12; reasons.append("开盘即封")
        elif t < 100000: score += 8
        elif t < 113000: score += 4
        elif t >= 140000: score -= 6
    except Exception:
        pass

    # ── 5. 热门板块加分 ─────────────────────────────────────────
    if ind and ind in hot:
        score += 10; reasons.append("热门板块")

    # ── 6. 成交额（流动性）──────────────────────────────────────
    if   amt >= 5e8: score += 6
    elif amt >= 2e8: score += 3
    elif amt <  3e7: score -= 5   # 流动性太差

    score = max(5, min(99, round(score)))

    # 操作建议
    if score >= 72 and on >= 1:
        sug = "⭐ 强烈推荐，2:30可分批买入"
    elif score >= 60 and on >= 1:
        sug = "👍 值得介入，注意分时走势"
    elif score >= 50 and on >= 1:
        sug = "🔍 可少量试探，止损3%"
    elif on == 0:
        sug = "📅 封死观望，看次日是否高开"
    else:
        sug = "⚠️ 偏弱，建议观望"

    return {
        "code":          str(r.get("代码", "")),
        "name":          str(r.get("名称", "")),
        "industry":      ind,
        "change_rate":   round(chg, 2),
        "price":         round(_f(r.get("最新价")), 2),
        "turnover":      round(hs, 2),
        "vol_ratio":     0.0,
        "total_mv":      round(mv, 1),
        "amount":        round(amt, 0),
        "spd5":          0.0,
        "vol_increasing": on >= 1,
        "vol_label":     "炸板已开板可买" if on >= 1 else "封死暂不可买",
        "open_num":      on,
        "consec":        lbs,
        "score":         score,
        "reasons":       reasons[:4],
        "suggestion":    sug,
        "buyable":       on >= 1,
        "first_time":    fmt_t(r.get("首次封板时间")),
        "last_time":     fmt_t(r.get("最后封板时间")),
        "seal_fund":     _f(r.get("封板资金")),
        "zt_stat":       str(r.get("涨停统计", "") or ""),
        "source":        "今日涨停",
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
