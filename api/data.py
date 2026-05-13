"""
Vercel Python Serverless Function
GET /api/data?type=zt&date=20260512

type 参数：
  zt   - 涨停股池（含炸板次数 OPEN_NUM）
  zb   - 炸板股池
  prev - 昨日涨停股池
  
date 参数：YYYYMMDD 或 YYYY-MM-DD，默认最近交易日
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import traceback
from datetime import datetime, timedelta


def get_last_trade_date():
    """获取最近交易日（简单跳过周末）"""
    d = datetime.now()
    # 北京时间加8小时
    d = d + timedelta(hours=8) - timedelta(hours=d.utcoffset().seconds // 3600 if d.utcoffset() else 0)
    # 收盘前用前一天
    if d.hour < 15:
        d = d - timedelta(days=1)
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d = d - timedelta(days=1)
    return d.strftime("%Y%m%d")


def normalize_date(date_str):
    """YYYY-MM-DD 或 YYYYMMDD 统一转成 YYYYMMDD"""
    if not date_str:
        return get_last_trade_date()
    return date_str.replace("-", "")


def fetch_data(pool_type, date_str):
    import akshare as ak

    if pool_type == "zb":
        # 炸板股池
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
    elif pool_type == "prev":
        # 昨日涨停
        df = ak.stock_zt_pool_previous_em(date=date_str)
    else:
        # 默认：今日涨停股池（含炸板次数）
        df = ak.stock_zt_pool_em(date=date_str)

    if df is None or df.empty:
        return []

    results = []
    for _, row in df.iterrows():
        item = {
            "SECURITY_CODE":  str(row.get("代码", "") or ""),
            "SECURITY_NAME":  str(row.get("名称", "") or ""),
            "CHANGE_RATE":    float(row.get("涨跌幅", 0) or 0),
            "CLOSE_PRICE":    float(row.get("最新价", 0) or 0),
            "OPEN_NUM":       int(row.get("炸板次数", 0) or 0),
            "FIRST_TIME":     fmt_time(row.get("首次封板时间", "")),
            "LAST_TIME":      fmt_time(row.get("最后封板时间", "")),
            "TURNOVERRATE":   float(row.get("换手率", 0) or 0),
            "DEAL_AMOUNT":    float(row.get("成交额", 0) or 0),
            "FLOAT_MV":       float(row.get("流通市值", 0) or 0),
            "CONSEC_BOARDS":  int(row.get("连板数", 0) or 0),
            "INDUSTRY":       str(row.get("所属行业", "") or ""),
            "ZT_STAT":        str(row.get("涨停统计", "") or ""),
            "SEAL_FUND":      float(row.get("封板资金", 0) or 0),
        }
        results.append(item)

    return results


def fmt_time(val):
    """92500 → '09:25'，兼容字符串和数字"""
    if not val or str(val).strip() in ("", "nan", "None"):
        return ""
    s = str(int(float(str(val)))).zfill(6)
    return f"{s[:2]}:{s[2:4]}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # CORS headers
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=60")
        self.end_headers()

        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            pool_type = (qs.get("type", ["zt"])[0]).lower()
            raw_date  = qs.get("date", [""])[0]
            date_str  = normalize_date(raw_date)

            data = fetch_data(pool_type, date_str)

            type_labels = {
                "zt":   "今日涨停股池",
                "zb":   "今日炸板股池",
                "prev": "昨日涨停股池",
            }

            resp = {
                "data":    data,
                "total":   len(data),
                "qdate":   date_str,
                "source":  f"AKShare · {type_labels.get(pool_type, pool_type)}",
                "message": "" if data else f"{date_str} 暂无数据（可能是非交易日）",
            }

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[炸板雷达 ERROR] {tb}")
            resp = {
                "data":  [],
                "total": 0,
                "error": str(e),
                "trace": tb,
            }

        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

