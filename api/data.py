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
        # 推荐不缓存（每次都取最新），其他数据缓存120秒
        qs_raw = parse_qs(urlparse(self.path).query)
        if qs_raw.get("type", [""])[0].lower() == "recommend":
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "s-maxage=120, stale-while-revalidate=30")
        self.end_headers()

        result = {}
        try:
            ptype = qs_raw.get("type", ["prev"])[0].lower()
            date  = normalize_date(qs_raw.get("date", [""])[0])

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
# 涨停数据（炸板雷达主表格）
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
# 短线推荐（2:30–3:00 策略）
# 合并三个数据池：
#   A. 今日涨停池（含炸板）— stock_zt_pool_em
#   B. 今日炸板专池          — stock_zt_pool_zbgc_em
#   C. 昨日涨停炸板未回封    — stock_zt_pool_previous_em
# 关键过滤：涨跌幅 < 9.5% 才是"当前真正可买入"
# ─────────────────────────────────────────────────────────────
def handle_recommend():
    import akshare as ak

    date = last_trade_day()
    hot  = _hot_sectors(date)
    seen = set()
    candidates = []

    # ── A. 今日涨停池（实时，含炸板和封死）─────────────────────
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get("代码", ""))
                if code and code not in seen:
                    seen.add(code)
                    candidates.append(_build(r, hot, "今日"))
    except Exception:
        pass

    # ── B. 今日炸板专池（补充细节）──────────────────────────────
    try:
        df2 = ak.stock_zt_pool_zbgc_em(date=date)
        if df2 is not None and not df2.empty:
            for _, r in df2.iterrows():
                code = str(r.get("代码", ""))
                if code and code not in seen:
                    seen.add(code)
                    candidates.append(_build(r, hot, "今日炸板"))
    except Exception:
        pass

    # ── C. 昨日涨停池（炸板未回封，次日延续候选）────────────────
    try:
        df3 = ak.stock_zt_pool_previous_em(date=date)
        if df3 is not None and not df3.empty:
            for _, r in df3.iterrows():
                code = str(r.get("代码", ""))
                if code and code not in seen:
                    seen.add(code)
                    r_copy = r.copy()
                    candidates.append(_build(r_copy, hot, "昨日涨停"))
    except Exception:
        pass

    if not candidates:
        return {
            "data": [], "qdate": date, "total": 0,
            "message": "暂无数据，请确认今日为交易日且已开盘（09:30后）",
        }

    # ── 换手率粗筛 ───────────────────────────────────────────────
    candidates = [c for c in candidates if 3 <= c["turnover"] <= 20]

    # ── 分类：可买入 vs 不可买入 ──────────────────────────────────
    # 关键：涨跌幅 < 9.5% 才是"当前未在涨停价"，可以成交
    buyable     = [c for c in candidates if c["truly_buyable"]]
    not_buyable = [c for c in candidates if not c["truly_buyable"]]

    buyable.sort(    key=lambda x: x["score"], reverse=True)
    not_buyable.sort(key=lambda x: x["score"], reverse=True)

    # 可买入优先，封死/回封补位
    combined = buyable[:8] + not_buyable[:2]

    return {
        "data":    combined[:10],
        "qdate":   date,
        "total":   len(combined[:10]),
        "message": f"可买入 {len(buyable)} 只，封死/回封 {len(not_buyable)} 只",
    }


def _build(r, hot, source):
    on   = _i(r.get("炸板次数"))
    chg  = _f(r.get("涨跌幅"))
    hs   = _f(r.get("换手率"))
    lbs  = max(1, _i(r.get("连板数")))
    ind  = str(r.get("所属行业", "") or "")
    amt  = _f(r.get("成交额"))
    mv   = _f(r.get("流通市值")) / 1e8
    fund = _f(r.get("封板资金"))

    # 真正可买入条件：
    # 1. 炸板过（open_num>=1）
    # 2. 当前涨幅 < 9.5%（说明目前未在涨停价，可正常成交）
    truly_buyable = (on >= 1) and (chg < 9.5)

    score   = 50
    reasons = []

    # 可买入状态评分
    if truly_buyable:
        if on == 1:
            score += 20; reasons.append("炸板1次可买")
        elif on == 2:
            score += 13; reasons.append("炸板2次")
        elif on == 3:
            score += 6;  reasons.append("炸板3次偏弱")
        else:
            score -= 5
    elif on >= 1 and chg >= 9.5:
        # 炸板后回封涨停了——暂不可买
        score -= 5;      reasons.append("已回封涨停")
    else:
        # 封死
        score -= 0;      reasons.append("封死（看次日）")

    # 换手率
    if   7 <= hs <= 12: score += 18; reasons.append("换手活跃")
    elif 5 <= hs < 7 or 12 < hs <= 15: score += 10
    elif 3 <= hs < 5:   score += 5
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

    # 操作建议
    if truly_buyable and score >= 72:
        sug = "⭐ 强烈推荐，2:30可分批买入"
    elif truly_buyable and score >= 60:
        sug = "👍 值得介入，注意分时走势"
    elif truly_buyable:
        sug = "🔍 可少量试探，止损3%"
    elif on >= 1 and chg >= 9.5:
        sug = "🔒 已回封涨停，暂无法买入，明日关注高开"
    else:
        sug = "📅 封死观望，看明日是否高开"

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
        "vol_increasing": truly_buyable,
        "vol_label":     (
            "✅ 炸板可买入" if truly_buyable else
            "🔒 回封涨停"   if (on >= 1 and chg >= 9.5) else
            "🔒 封死涨停"
        ),
        "open_num":      on,
        "consec":        lbs,
        "score":         score,
        "reasons":       reasons[:4],
        "suggestion":    sug,
        "buyable":       truly_buyable,
        "truly_buyable": truly_buyable,
        "first_time":    fmt_t(r.get("首次封板时间")),
        "last_time":     fmt_t(r.get("最后封板时间")),
        "seal_fund":     fund,
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
