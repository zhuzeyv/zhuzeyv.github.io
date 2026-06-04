"""
GET /api/recommend
短线推荐：2:30买入，次日冲高卖出
综合评分：换手率 + 封板质量 + 热门板块 + 连板 + 资金面
"""

from http.server import BaseHTTPRequestHandler
from datetime import datetime, timedelta
import json
import traceback


def bj_now():
    return datetime.utcnow() + timedelta(hours=8)


def fmt_time(val):
    try:
        s = str(int(float(str(val)))).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except Exception:
        return "—"


def fmt_money(val):
    try:
        v = float(val or 0)
        if v >= 1e8:
            return f"{v/1e8:.1f}亿"
        if v >= 1e4:
            return f"{v/1e4:.0f}万"
        return str(int(v))
    except Exception:
        return "—"


def last_trade_day():
    d = bj_now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def get_data():
    import akshare as ak

    date = last_trade_day()
    rows = []

    # ── 今日涨停池（含炸板）────────────────────────────────────
    try:
        df = ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                rows.append({
                    "code":       str(r.get("代码", "")),
                    "name":       str(r.get("名称", "")),
                    "industry":   str(r.get("所属行业", "") or ""),
                    "change_rate": float(r.get("涨跌幅", 0) or 0),
                    "price":      float(r.get("最新价", 0) or 0),
                    "turnover":   float(r.get("换手率", 0) or 0),
                    "open_num":   int(r.get("炸板次数", 0) or 0),
                    "consec":     int(r.get("连板数", 1) or 1),
                    "first_time": fmt_time(r.get("首次封板时间")),
                    "last_time":  fmt_time(r.get("最后封板时间")),
                    "seal_fund":  float(r.get("封板资金", 0) or 0),
                    "amount":     float(r.get("成交额", 0) or 0),
                    "zt_stat":    str(r.get("涨停统计", "") or ""),
                    "source":     "今日涨停",
                })
    except Exception:
        pass

    # ── 昨日涨停池（今日延续候选）────────────────────────────────
    try:
        df2 = ak.stock_zt_pool_previous_em(date=date)
        if df2 is not None and not df2.empty:
            exist_codes = {r["code"] for r in rows}
            for _, r in df2.iterrows():
                code = str(r.get("代码", ""))
                if code in exist_codes:
                    continue
                rows.append({
                    "code":       code,
                    "name":       str(r.get("名称", "")),
                    "industry":   str(r.get("所属行业", "") or ""),
                    "change_rate": float(r.get("涨跌幅", 0) or 0),
                    "price":      float(r.get("最新价", 0) or 0),
                    "turnover":   float(r.get("换手率", 0) or 0),
                    "open_num":   int(r.get("炸板次数", 0) or 0),
                    "consec":     int(r.get("连板数", 1) or 1),
                    "first_time": fmt_time(r.get("首次封板时间")),
                    "last_time":  fmt_time(r.get("最后封板时间")),
                    "seal_fund":  float(r.get("封板资金", 0) or 0),
                    "amount":     float(r.get("成交额", 0) or 0),
                    "zt_stat":    str(r.get("涨停统计", "") or ""),
                    "source":     "昨日涨停",
                })
    except Exception:
        pass

    return rows, date


def score_stock(s, hot_sectors):
    """
    短线评分（满分100）
    适合 2:30 买入、次日冲高卖出的策略
    """
    score = 50
    reasons = []

    # 换手率（最优区间 7-10%）
    hs = s["turnover"]
    if 7 <= hs <= 10:
        score += 20; reasons.append("换手甜蜜区")
    elif 5 <= hs < 7 or 10 < hs <= 12:
        score += 12
    elif 3 <= hs < 5 or 12 < hs <= 15:
        score += 5
    else:
        score -= 8

    # 封板质量（封死 > 少炸 > 多炸）
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
        score += 2   # 高位风险

    # 首次封板时间（越早越好）
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
    ind = s["industry"]
    cnt = hot_sectors.get(ind, 0)
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
    elif fund >= 5e7:
        score += 2

    # 今日数据比昨日更可靠
    if s["source"] == "今日涨停":
        score += 5

    score = max(5, min(99, round(score)))

    # 操作建议
    if score >= 75 and on == 0:
        suggestion = "⭐ 强烈关注，封死稳健，次日溢价概率高"
    elif score >= 65:
        suggestion = "👍 值得关注，注意次日高开走势"
    elif score >= 55:
        suggestion = "🔍 可以关注，控制仓位，谨慎参与"
    else:
        suggestion = "⚠️ 一般，风险偏大"

    return score, reasons[:4], suggestion


def recommend(rows):
    # 换手率粗筛（3-15%）
    candidates = [r for r in rows if 3 <= r["turnover"] <= 15]
    if not candidates:
        candidates = rows  # 无数据则用全部

    # 统计热门板块
    from collections import Counter
    sector_cnt = Counter(r["industry"] for r in rows if r["industry"])
    hot_sectors = {k: v for k, v in sector_cnt.items() if v >= 2}

    # 评分
    for s in candidates:
        sc, reas, sug = score_stock(s, hot_sectors)
        s["score"]      = sc
        s["reasons"]    = reas
        s["suggestion"] = sug

    # 排序取前10
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:10]


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

        try:
            rows, date = get_data()
            results    = recommend(rows)
            resp = {
                "data":    results,
                "qdate":   date,
                "total":   len(results),
                "message": "" if results else "暂无数据（需交易日有涨停板数据）",
            }
        except Exception as e:
            resp = {
                "data":  [],
                "error": str(e),
                "trace": traceback.format_exc(),
            }

        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass
