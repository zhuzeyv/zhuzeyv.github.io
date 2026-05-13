"""
Vercel Python Serverless Function — api/data.py
GET /api/data?type=prev&date=20260512
 
type: prev=昨日涨停  zt=今日涨停  zb=今日炸板
date: YYYYMMDD 或 YYYY-MM-DD（可选，不填自动取最近交易日）
"""
 
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta
import json
 
 
class handler(BaseHTTPRequestHandler):
 
    def do_OPTIONS(self):
        self._cors()
        self.end_headers()
 
    def do_GET(self):
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=300, stale-while-revalidate=60")
        self.end_headers()
 
        try:
            qs    = parse_qs(urlparse(self.path).query)
            ptype = (qs.get("type", ["prev"])[0]).lower()
            date  = normalize_date(qs.get("date", [""])[0])
 
            data, source = fetch(ptype, date)
 
            resp = {
                "data":   data,
                "total":  len(data),
                "qdate":  date,
                "source": source,
                "message": "" if data else f"{date} 暂无数据（可能是非交易日）",
            }
 
        except Exception as e:
            import traceback
            resp = {"data": [], "total": 0, "error": str(e), "trace": traceback.format_exc()}
 
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
 
    def _cors(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
 
    # 屏蔽 BaseHTTPRequestHandler 的默认日志输出
    def log_message(self, fmt, *args):
        pass
 
 
# ── 数据获取 ─────────────────────────────────────────────────
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
            "SECURITY_CODE":  str(r.get("代码", "") or ""),
            "SECURITY_NAME":  str(r.get("名称", "") or ""),
            "CHANGE_RATE":    _f(r.get("涨跌幅")),
            "CLOSE_PRICE":    _f(r.get("最新价")),
            "OPEN_NUM":       _i(r.get("炸板次数")),
            "FIRST_TIME":     fmt_time(r.get("首次封板时间")),
            "LAST_TIME":      fmt_time(r.get("最后封板时间")),
            "TURNOVERRATE":   _f(r.get("换手率")),
            "DEAL_AMOUNT":    _f(r.get("成交额")),
            "FLOAT_MV":       _f(r.get("流通市值")),
            "CONSEC_BOARDS":  _i(r.get("连板数")),
            "INDUSTRY":       str(r.get("所属行业", "") or ""),
            "ZT_STAT":        str(r.get("涨停统计", "") or ""),
            "SEAL_FUND":      _f(r.get("封板资金")),
        })
 
    return rows, f"AKShare · {labels.get(ptype, ptype)}"
 
 
# ── 工具函数 ─────────────────────────────────────────────────
def _f(v):
    try:   return float(v)
    except: return 0.0
 
def _i(v):
    try:   return int(float(v))
    except: return 0
 
def fmt_time(val):
    """92500 / '092500' → '09:25'"""
    try:
        s = str(int(float(str(val)))).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except:
        return ""
 
def normalize_date(s):
    """YYYY-MM-DD 或 YYYYMMDD → YYYYMMDD；空则返回最近交易日"""
    if not s:
        return last_trade_day()
    clean = s.replace("-", "")
    return clean if len(clean) == 8 else last_trade_day()
 
def last_trade_day():
    """返回最近交易日 YYYYMMDD（简单跳过周末，不处理节假日）"""
    d = datetime.utcnow() + timedelta(hours=8)   # 北京时间
    if d.hour < 15:                               # 收盘前用前一日
        d -= timedelta(days=1)
    while d.weekday() >= 5:                       # 跳过周末
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
