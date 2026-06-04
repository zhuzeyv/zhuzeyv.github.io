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

        body = json.dumps(result, ensure_ascii=False)
        self.wfile.write(body.encode("utf-8"))

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
            "FIRST_TIME":    ft(r.get("首次封板时间")),
            "LAST_TIME":     ft(r.get("最后封板时间")),
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
# ─────────────────────────────────────────────────────────────
def handle_recommend():
    """
    交易时段 (09:25–15:00)：用实时行情筛选涨幅3-5%、量比>1、市值50-300亿
    非交易时段：用昨日涨停池做次日候选推荐
    """
    bj = bj_now()
    in_session = (bj.weekday() < 5) and (
        (9 * 60 + 25) <= (bj.hour * 60 + bj.minute) <= (15 * 60)
    )

    if in_session:
        return recommend_realtime()
    else:
        return recommend_from_zt()


# ── 交易时段：全市场实时筛选 ────────────────────────────────────
def recommend_realtime():
    import akshare as ak

    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        return recommend_from_zt()          # 备用方案

    # 硬筛选
    df = df[~df["名称"].astype(str).str.contains(r"ST|退|^N|^C", na=False, regex=True)]
    df = df[(df["涨跌幅"]  >= 3.0) & (df["涨跌幅"]  <= 5.0)]
    df = df[ df["量比"]    >  1.0]
    df = df[(df["总市值"]  >= 5e9) & (df["总市值"]  <= 3e10)]
    df = df[ df["最新价"]  >  3.0]

    if df.empty:
        return {
            "data": [], "qdate": today_str(), "total": 0,
            "message": "当前无符合条件的股票，请在 14:30–15:00 查询",
        }

    hot = _hot_sectors()
    rows = [_score_realtime(r, hot) for _, r in df.iterrows()]
    rows.sort(key=lambda x: x["score"], reverse=True)

    return {
        "data":    rows[:10],
        "qdate":   today_str(),
        "total":   len(rows[:10]),
        "message": "",
        "mode":    "realtime",
    }


def _score_realtime(r, hot):
    chg  = _f(r.get("涨跌幅"))
    vr   = _f(r.get("量比"))
    hs   = _f(r.get("换手率"))
    mv   = _f(r.get("总市值")) / 1e8
    amt  = _f(r.get("成交额"))
    spd5 = _f(r.get("5分钟涨跌"))
    ind  = str(r.get("行业", "") or r.get("所属行业", "") or "")

    score   = 50
    reasons = []

    if 3.5 <= chg <= 4.5:
        score += 20; reasons.append("涨幅甜蜜区")
    elif 3.0 <= chg < 3.5 or 4.5 < chg <= 5.0:
        score += 10

    if   vr >= 2.5: score += 22; reasons.append("成交量大幅递增")
    elif vr >= 1.8: score += 16; reasons.append("成交量明显递增")
    elif vr >= 1.3: score += 10; reasons.append("成交量温和递增")
    else:           score += 3

    if   100 <= mv <= 200: score += 12; reasons.append("市值适中")
    elif  50 <= mv <  100 or 200 < mv <= 300: score += 6

    if   spd5 >  0.5: score += 10; reasons.append("尾盘上行")
    elif spd5 >  0:   score += 5
    elif spd5 < -0.5: score -= 8

    if ind and ind in hot:
        score += 10; reasons.append("热门板块")

    if 3 <= hs <= 8: score += 5
    elif hs > 15:    score -= 5

    score = max(5, min(99, round(score)))

    vol_label = (
        "📈 成交量大幅递增" if vr >= 2.0 else
        "📈 成交量递增"     if vr >= 1.3 else
        "➡️ 成交量平稳"
    )

    if   score >= 75: sug = "⭐ 强烈关注！量价配合好，2:30–2:50分批建仓"
    elif score >= 62: sug = "👍 值得关注，量比活跃，可在2:30轻仓参与"
    elif score >= 50: sug = "🔍 一般，建议等量比进一步放大再入场"
    else:             sug = "⚠️ 量能偏弱，观望为主"

    return {
        "code":          str(r.get("代码", "")),
        "name":          str(r.get("名称", "")),
        "industry":      ind,
        "change_rate":   round(chg, 2),
        "price":         round(_f(r.get("最新价")), 2),
        "turnover":      round(hs, 2),
        "vol_ratio":     round(vr, 2),
        "total_mv":      round(mv, 1),
        "amount":        round(amt, 0),
        "spd5":          round(spd5, 2),
        "vol_increasing": vr >= 1.3,
        "vol_label":     vol_label,
        "score":         score,
        "reasons":       reasons[:4],
        "suggestion":    sug,
        "buyable":       True,
    }


# ── 非交易时段：用昨日涨停池推荐次日候选 ────────────────────────
def recommend_from_zt():
    """
    非交易时段（夜间/周末）备用逻辑：
    取昨日炸板股 → 按换手率+连板+热门板块评分 → 作为次日2:30参考
    """
    import akshare as ak

    date = last_trade_day()
    hot  = _hot_sectors(date)
    rows = []

    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                rows.append(_score_zt(r, hot, "今日涨停"))
    except Exception:
        pass

    if not rows:
        return {
            "data": [], "qdate": date, "total": 0,
            "message": "暂无数据，推荐功能需在交易日使用",
        }

    # 筛选换手率3-15%
    rows = [r for r in rows if 3 <= r["turnover"] <= 15]
    rows.sort(key=lambda x: x["score"], reverse=True)

    for r in rows[:10]:
        r["suggestion"] = "📅 次日参考（非交易时段）｜" + r["suggestion"]

    return {
        "data":    rows[:10],
        "qdate":   date,
        "total":   len(rows[:10]),
        "message": "非交易时段，显示昨日数据供次日参考",
        "mode":    "offline",
    }


def _score_zt(r, hot, source):
    on  = _i(r.get("炸板次数"))
    hs  = _f(r.get("换手率"))
    lbs = max(1, _i(r.get("连板数")))
    ind = str(r.get("所属行业", "") or "")
    chg = _f(r.get("涨跌幅"))
    amt = _f(r.get("成交额"))

    score = 50; reasons = []

    # 炸板=可次日买入，封死=需看高开后再决策
    if   on == 0: score += 8;  reasons.append("封死（关注高开）")
    elif on == 1: score += 18; reasons.append("炸板1次可买")
    elif on == 2: score += 10; reasons.append("炸板2次")
    else:         score -= 5

    if   7 <= hs <= 10: score += 18; reasons.append("换手甜蜜区")
    elif 5 <= hs < 7 or 10 < hs <= 12: score += 10
    elif 3 <= hs < 5:   score += 4

    if   lbs == 2: score += 12; reasons.append("连2板")
    elif lbs == 3: score += 8;  reasons.append("连3板")
    elif lbs == 1: score += 6
    elif lbs >= 4: score += 2

    if ind in hot: score += 10; reasons.append("热门板块")

    if amt >= 5e8: score += 6
    elif amt < 5e7: score -= 4

    fbt_raw = r.get("首次封板时间")
    try:
        t = int(float(str(fbt_raw)))
        if t < 93500: score += 10; reasons.append("早盘封板")
        elif t < 100000: score += 6
        elif t >= 140000: score -= 6
    except Exception:
        pass

    score = max(5, min(99, round(score)))

    if score >= 72 and on <= 1:
        sug = "⭐ 次日可重点关注，2:30附近可介入"
    elif score >= 60:
        sug = "👍 次日值得关注，注意量比和分时走势"
    else:
        sug = "🔍 次日一般，可观望"

    mv = _f(r.get("流通市值")) / 1e8

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
        "vol_label":     "炸板可买入" if on >= 1 else "封死（关注高开）",
        "score":         score,
        "reasons":       reasons[:4],
        "suggestion":    sug,
        "buyable":       on >= 1,
        "open_num":      on,
        "consec":        lbs,
        "source":        source,
    }


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────
def _hot_sectors(date=None):
    try:
        import akshare as ak
        from collections import Counter
        d = date or last_trade_day()
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

def ft(val):
    try:
        s = str(int(float(str(val)))).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except: return ""

def bj_now():
    return datetime.utcnow() + timedelta(hours=8)

def today_str():
    return bj_now().strftime("%Y%m%d")

def normalize_date(s):
    if not s: return last_trade_day()
    c = s.replace("-", "")
    return c if len(c) == 8 else last_trade_day()

def last_trade_day():
    d = bj_now()
    if d.hour < 15: d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
