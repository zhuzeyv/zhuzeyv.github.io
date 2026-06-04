"""
GET /api/recommend
返回当日适合「2:30 买入，次日冲高卖出」的推荐股票
筛选逻辑：
  1. 今日封死涨停（炸板次数=0）
  2. 换手率 5-10%（中等活跃度）
  3. 所属行业在今日热门板块（该板块涨停数≥2）
  4. 连板数 1-3（首板/二板溢价空间大）
  5. 综合评分排序，取前8只
"""

from http.server import BaseHTTPRequestHandler
from datetime import datetime, timedelta
import json


def bj_now():
    return datetime.utcnow() + timedelta(hours=8)


def last_trade_day():
    d = bj_now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def get_recommendations():
    import akshare as ak

    date = last_trade_day()

    # 今日涨停池
    df = ak.stock_zt_pool_em(date=date)
    if df is None or df.empty:
        return [], date

    # ── 筛选 ─────────────────────────────────────────────────
    # 封死（炸板次数=0）
    df = df[df["炸板次数"] == 0].copy()

    # 换手率 5-10%
    df = df[(df["换手率"] >= 5) & (df["换手率"] <= 10)].copy()

    # 连板数 1-3（不追高）
    df = df[(df["连板数"] >= 1) & (df["连板数"] <= 3)].copy()

    if df.empty:
        return [], date

    # 热门板块（今日涨停≥2只的板块）
    sector_counts = df["所属行业"].value_counts()
    hot_sectors = set(sector_counts[sector_counts >= 2].index.tolist())
    if hot_sectors:
        df_hot = df[df["所属行业"].isin(hot_sectors)].copy()
        if not df_hot.empty:
            df = df_hot

    # ── 评分 ─────────────────────────────────────────────────
    def score(row):
        s = 60.0
        # 换手率接近 8% 最佳
        hs = float(row.get("换手率", 0) or 0)
        s -= abs(hs - 8) * 2

        # 连板数：2板最优
        lbs = int(row.get("连板数", 1) or 1)
        if lbs == 2:    s += 15
        elif lbs == 1:  s += 8
        else:           s += 3   # 3板风险稍大

        # 封板时间越早越好（格式 HHMMSS 整数）
        fbt = row.get("首次封板时间")
        try:
            t = int(float(str(fbt)))
            if t < 93500:   s += 15   # 开盘即封
            elif t < 100000: s += 10
            elif t < 110000: s += 5
            elif t < 130000: s += 0
            else:            s -= 10  # 下午封板谨慎
        except:
            pass

        # 所属热门板块加分
        if row.get("所属行业") in hot_sectors:
            cnt = sector_counts.get(row["所属行业"], 1)
            s += min(cnt * 3, 15)

        # 封板资金充足加分
        fund = float(row.get("封板资金", 0) or 0)
        if fund >= 5e8:  s += 10
        elif fund >= 2e8: s += 5

        return round(min(99, max(5, s)))

    df["评分"] = df.apply(score, axis=1)
    df = df.sort_values("评分", ascending=False).head(8)

    results = []
    for _, r in df.iterrows():
        fbt_raw = r.get("首次封板时间")
        try:
            t = str(int(float(str(fbt_raw)))).zfill(6)
            fbt = f"{t[:2]}:{t[2:4]}"
        except:
            fbt = "—"

        code = str(r.get("代码", ""))
        cons = int(r.get("连板数", 1) or 1)

        # 推荐理由
        reasons = []
        hs = float(r.get("换手率", 0) or 0)
        if hs >= 6 and hs <= 9:   reasons.append("换手适中")
        if cons >= 2:              reasons.append(f"连{cons}板强势")
        if fbt and fbt < "10:30": reasons.append("早盘封板")
        fund = float(r.get("封板资金", 0) or 0)
        if fund >= 3e8:            reasons.append("封板资金充裕")
        if r.get("所属行业") in hot_sectors:
            reasons.append("热门板块")
        if not reasons:            reasons.append("涨停封死")

        results.append({
            "code":       code,
            "name":       str(r.get("名称", "")),
            "industry":   str(r.get("所属行业", "")),
            "change_rate": float(r.get("涨跌幅", 0) or 0),
            "price":      float(r.get("最新价", 0) or 0),
            "turnover":   float(r.get("换手率", 0) or 0),
            "consec":     cons,
            "first_time": fbt,
            "seal_fund":  float(r.get("封板资金", 0) or 0),
            "amount":     float(r.get("成交额", 0) or 0),
            "score":      int(r.get("评分", 50)),
            "reasons":    reasons[:3],
            "zt_stat":    str(r.get("涨停统计", "") or ""),
        })

    return results, date


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=60")
        self.end_headers()
        try:
            data, date = get_recommendations()
            resp = {"data": data, "qdate": date, "total": len(data)}
        except Exception as e:
            import traceback
            resp = {"data": [], "error": str(e), "trace": traceback.format_exc()}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())

    def log_message(self, fmt, *args): pass
