"""
GET /api/kline?code=000001&days=60
返回股票日K线数据 + 近20日涨停标记
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
import json


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=600")
        self.end_headers()
        try:
            qs   = parse_qs(urlparse(self.path).query)
            code = qs.get("code", ["000001"])[0].strip()
            days = int(qs.get("days", ["60"])[0])
            resp = get_kline(code, days)
        except Exception as e:
            import traceback
            resp = {"error": str(e), "trace": traceback.format_exc()}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())

    def log_message(self, fmt, *args): pass


def get_kline(code, days=60):
    import akshare as ak

    now = datetime.utcnow() + timedelta(hours=8)
    end_date = now.strftime("%Y%m%d")
    # 多取一些日历日，确保覆盖足够的交易日
    start = now - timedelta(days=days * 2)
    start_date = start.strftime("%Y%m%d")

    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )
    # Columns: 日期 开盘 收盘 最高 最低 成交量 成交额 振幅 涨跌幅 涨跌额 换手率

    if df is None or df.empty:
        return {"dates": [], "ohlc": [], "volumes": [], "zt_dates": [], "ma": {}}

    # 取最近 days 个交易日
    df = df.tail(days).copy()
    df = df.reset_index(drop=True)

    dates   = df["日期"].astype(str).tolist()
    ohlc    = df[["开盘", "收盘", "最高", "最低"]].round(3).values.tolist()
    volumes = df["成交量"].tolist()
    chg     = df["涨跌幅"].tolist()

    # 涨停日标记（涨跌幅 >= 9.9%）
    zt_dates = [dates[i] for i, c in enumerate(chg) if c is not None and float(c) >= 9.9]

    # MA 均线
    closes = df["收盘"].tolist()
    def ma(n):
        result = []
        for i in range(len(closes)):
            if i < n - 1:
                result.append(None)
            else:
                avg = sum(closes[i - n + 1: i + 1]) / n
                result.append(round(avg, 3))
        return result

    # 实时价（尽量获取，失败则用最新收盘）
    realtime_price = None
    try:
        spot = ak.stock_zh_a_spot_em()
        row  = spot[spot["代码"] == code]
        if not row.empty:
            realtime_price = float(row.iloc[0]["最新价"])
    except:
        pass

    return {
        "code":            code,
        "dates":           dates,
        "ohlc":            ohlc,
        "volumes":         volumes,
        "changes":         [round(c, 2) if c is not None else 0 for c in chg],
        "zt_dates":        zt_dates,
        "ma": {
            "ma5":  ma(5),
            "ma10": ma(10),
            "ma20": ma(20),
        },
        "realtime_price":  realtime_price,
        "latest_close":    closes[-1] if closes else None,
    }
