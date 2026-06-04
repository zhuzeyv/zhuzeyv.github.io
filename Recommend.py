"""
GET /api/recommend
短线推荐：2:30买入，次日冲高卖出
不限制是否涨停，综合评分挑选最适合的股票
"""

from http.server import BaseHTTPRequestHandler
from datetime import datetime, timedelta
import json
import traceback


def bj_now():
    return datetime.utcnow() + timedelta(hours=8)


def last_trade_day(offset=0):
    """返回最近第 offset 个交易日（offset=0=今天/最新，1=昨日）"""
    d = bj_now()
    # 收盘前用前一日
    if d.hour < 15 and offset == 0:
        d -= timedelta(days=1)
    d -= timedelta(days=offset)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def get_recommendations():
    import akshare as ak
    import pandas as pd

    today   = last_trade_day(0)
    yest    = last_trade_day(1)

    # ── 1. 获取今日涨停池（含炸板，不做封死限制）────────────────
    candidates = pd.DataFrame()
    zt_source  = "今日涨停池"

    try:
        df_zt = ak.stock_zt_pool_em(date=today)
        if df_zt is not None and not df_zt.empty:
            df_zt["_来源"] = "今日涨停"
            candidates = pd.concat([candidates, df_zt], ignore_index=True)
    except Exception as e:
        pass  # 盘中可能无完整数据

    # ── 2. 昨日涨停池（今日延续效应）────────────────────────────
    try:
        df_prev = ak.stock_zt_pool_previous_em(date=today)
        if df_prev is not None and not df_prev.empty:
            df_prev["_来源"] = "昨日涨停"
            candidates = pd.concat([candidates, df_prev], ignore_index=True)
    except:
        pass

    # 如果今日数据不足，用昨日数据
    if candidates.empty:
        try:
            df_zt2 = ak.stock_zt_pool_em(date=yest)
            if df_zt2 is not None and not df_zt2.empty:
                df_zt2["_来源"] = "昨日涨停"
                candidates = df_zt2
                today = yest
        except:
            pass

    if candidates.empty:
        return [], today, "暂无数据"

    # 去重（同一只股可能在两个池里都有）
    if "代码" in candidates.columns:
        candidates = candidates.drop_duplicates(subset=["代码"], keep="first")

    # ── 3. 识别热门板块 ──────────────────────────────────────────
    sector_col = "所属行业" if "所属行业" in candidates.columns else None
    hot_sectors = {}
    if sector_col:
        sc = candidates[sector_col].value_counts()
        hot_sectors = sc[sc >= 2].to_dict()   # 板块涨停≥2只

    # ── 4. 换手率筛选（3-15%，比之前更宽松）────────────────────
    if "换手率" in candidates.columns:
        candidates = candidates[
            (candidates["换手率"] >= 3) &
            (candidates["换手率"] <= 15)
        ].copy()

    if candidates.empty:
        return [], today, "换手率筛选后无股票"

    # ── 5. 综合评分 ──────────────────────────────────────────────
    def score_stock(row):
        score = 50.0
        reasons = []

        # — 换手率（满分20，甜蜜区7-10%）
        hs = float(row.get("换手率", 0) or 0)
        if 7 <= hs <= 10:
            score += 20; reasons.append("换手适中")
        elif 5 <= hs < 7 or 10 < hs <= 12:
            score += 12
        elif 3 <= hs < 5 or 12 < hs <= 15:
            score += 5
        else:
            score -= 5

        # — 封死 vs 炸板（封死+15，炸板按次数扣分）
        open_num = int(row.get("炸板次数", 0) or 0)
        if open_num == 0:
            score += 15; reasons.append("今日封死")
        elif open_num == 1:
            score += 5;  reasons.append("炸板1次")
        elif open_num == 2:
            score -= 5
        else:
            score -= 15

        # — 连板数（满分15，2板最优）
        lbs = int(row.get("连板数", 1) or 1)
        if lbs == 2:
            score += 15; reasons.append("连2板")
        elif lbs == 3:
            score += 10; reasons.append("连3板")
        elif lbs == 1:
            score += 8
        elif lbs >= 4:
            score += 3   # 高位风险大

        # — 首次封板时间（越早越好，满分15）
        fbt = row.get("首次封板时间")
        try:
            t = int(float(str(fbt)))
            if t < 93500:
                score += 15; reasons.append("开盘即封")
            elif t < 100000:
                score += 10
            elif t < 110000:
                score += 6
            elif t < 130000:
                score += 2
            else:
                score -= 8   # 尾盘封板不理想
        except:
            pass

        # — 热门板块加分（满分15）
        if sector_col:
            sec = row.get(sector_col, "")
            cnt = hot_sectors.get(sec, 0)
            if cnt >= 4:
                score += 15; reasons.append("超热板块")
            elif cnt >= 2:
                score += 10; reasons.append("热门板块")

        # — 封板资金（资金充足加分，满分10）
        fund = float(row.get("封板资金", 0) or 0)
        if fund >= 5e8:
            score += 10; reasons.append("封板资金充裕")
        elif fund >= 2e8:
            score += 6
        elif fund >= 5e7:
            score += 2

        # — 来源加分（昨日涨停今日在涨停池=持续性强）
        if row.get("_来源") == "今日涨停":
            score += 5

        # — 涨跌幅（接近涨停或已涨停更好）
        chg = float(row.get("涨跌幅", 0) or 0)
        if chg >= 9.5:
            score += 8
        elif chg >= 7:
            score += 4
        elif chg < 3:
            score -= 5

        score = max(5, min(99, round(score)))
        return score, reasons[:4]

    score_data = candidates.apply(lambda r: score_stock(r), axis=1)
    candidates["_score"]   = [x[0] for x in score_data]
    candidates["_reasons"] = [x[1] for x in score_data]
    candidates = candidates.sort_values("_score", ascending=False).head(10)

    # ── 6. 格式化输出 ────────────────────────────────────────────
    results = []
    for _, r in candidates.iterrows():
        fbt_raw = r.get("首次封板时间")
        try:
            t = str(int(float(str(fbt_raw)))).zfill(6)
            fbt = f"{t[:2]}:{t[2:4]}"
        except:
            fbt = "—"

        lbt_raw = r.get("最后封板时间")
        try:
            t2 = str(int(float(str(lbt_raw)))).zfill(6)
            lbt = f"{t2[:2]}:{t2[2:4]}"
        except:
            lbt = "—"

        hs   = float(r.get("换手率", 0) or 0)
        chg  = float(r.get("涨跌幅", 0) or 0)
        open_num = int(r.get("炸板次数", 0) or 0)
        lbs  = int(r.get("连板数", 1) or 1)
        fund = float(r.get("封板资金", 0) or 0)
        amt  = float(r.get("成交额", 0) or 0)
        sec  = str(r.get(sector_col, "") or "") if sector_col else ""
        score = int(r.get("_score", 50))
        reas  = list(r.get("_reasons", []))

        # 生成操作建议
        if score >= 75 and open_num == 0:
            suggestion = "⭐ 强烈关注，封死稳健，次日溢价概率高"
        elif score >= 65 and open_num <= 1:
            suggestion = "👍 值得关注，注意次日高开后的走势"
        elif score >= 55:
            suggestion = "🔍 可以关注，建议控制仓位"
        else:
            suggestion = "⚠️ 一般，风险偏大，谨慎参与"

        results.append({
            "code":        str(r.get("代码", "")),
            "name":        str(r.get("名称", "")),
            "industry":    sec,
            "change_rate": chg,
            "price":       float(r.get("最新价", 0) or 0),
            "turnover":    hs,
            "consec":      lbs,
            "open_num":    open_num,
            "first_time":  fbt,
            "last_time":   lbt,
            "seal_fund":   fund,
            "amount":      amt,
            "score":       score,
            "reasons":     reas,
            "suggestion":  suggestion,
            "source":      str(r.get("_来源", "")),
            "zt_stat":     str(r.get("涨停统计", "") or ""),
        })

    return results, today, ""


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
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=60")
        self.end_headers()

        try:
            data, date, msg = get_recommendations()
            resp = {
                "data":    data,
                "qdate":   date,
                "total":   len(data),
                "message": msg,
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
