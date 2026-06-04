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
        self.send_header("Cache-Control", "s-maxage=120, stale-while-revalidate=30")
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
            resp = {"data": [], "total": 0, "error": str(e),
                    "trace": traceback.format_exc()}
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


# ── 短线推荐（2:30-3:00 买入）────────────────────────────────
def get_recommend():
    """
    筛选条件：
      · 涨幅 3%-5%（2:30可以买入，不在涨停）
      · 量比 > 1（今日成交活跃于均值）
      · 总市值 50-300亿
      · 成交量递增（量比 > 1 且当日成交量 > 近5日均量）
    评分：涨幅位置 + 量比强度 + 市值甜蜜点 + 热门板块 + 成交量趋势
    """
    import akshare as ak

    # ── 1. 拉全市场实时行情 ──────────────────────────────────
    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        return {"data": [], "qdate": today_str(),
                "total": 0, "message": "无法获取实时行情"}

    # ── 2. 硬筛选 ────────────────────────────────────────────
    # 过滤ST、退市、新股（代码不规范的）
    valid_prefix = ('0','1','2','3','6','8')
    df = df[df['代码'].astype(str).str[:1].isin(valid_prefix)]
    df = df[~df['名称'].astype(str).str.contains('ST|退|N|C', regex=True)]

    # 涨幅 3%-5%
    df = df[(df['涨跌幅'] >= 3.0) & (df['涨跌幅'] <= 5.0)]

    # 量比 > 1（今日量 > 近期均量）
    df = df[df['量比'] > 1.0]

    # 总市值 50亿-300亿（单位：元；50亿=5e9，300亿=3e10）
    df = df[(df['总市值'] >= 5e9) & (df['总市值'] <= 3e10)]

    # 最新价 > 3元（剔除垃圾股）
    df = df[df['最新价'] > 3.0]

    if df.empty:
        return {"data": [], "qdate": today_str(),
                "total": 0, "message": "当前无符合条件的股票（建议在14:30-15:00查询）"}

    # ── 3. 获取近5日K线验证成交量递增 ────────────────────────
    # 对候选股（最多40只）检查量的趋势
    end_date = today_str()
    beg_date = (datetime.utcnow() + timedelta(hours=8) - timedelta(days=14)).strftime("%Y%m%d")

    candidates = df.copy()
    vol_trend  = {}   # code → True/False（成交量是否递增）

    # 只对前40个候选检查（避免超时）
    check_codes = candidates.nlargest(40, '量比')['代码'].tolist()

    for code in check_codes:
        try:
            kdf = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=beg_date, end_date=end_date,
                adjust="qfq"
            )
            if kdf is not None and len(kdf) >= 3:
                vols = kdf['成交量'].tail(5).tolist()
                # 近3日成交量递增趋势：后两日均大于前一日
                if len(vols) >= 3:
                    increasing = (vols[-1] > vols[-2]) or (vols[-2] > vols[-3])
                    vol_trend[code] = increasing
        except Exception:
            pass

    # ── 4. 热门板块（从涨停池识别）───────────────────────────
    hot_sectors = set()
    try:
        date = last_trade_day()
        zt_df = ak.stock_zt_pool_em(date=date)
        if zt_df is not None and not zt_df.empty:
            from collections import Counter
            sc = Counter(zt_df['所属行业'].dropna().tolist())
            hot_sectors = {k for k, v in sc.items() if v >= 2}
    except Exception:
        pass

    # ── 5. 评分 ──────────────────────────────────────────────
    rows = []
    for _, r in candidates.iterrows():
        code    = str(r.get('代码', ''))
        name    = str(r.get('名称', ''))
        chg     = _f(r.get('涨跌幅'))
        price   = _f(r.get('最新价'))
        vol_r   = _f(r.get('量比'))
        hs      = _f(r.get('换手率'))
        tot_mv  = _f(r.get('总市值'))  # 元
        amt     = _f(r.get('成交额'))
        spd5    = _f(r.get('5分钟涨跌'))  # 5分钟涨跌幅
        # 行业字段名可能不同
        industry = str(r.get('行业','') or r.get('所属行业','') or '')

        score   = 50
        reasons = []

        # 涨幅位置（3.5-4.5%最优，留有空间但不太离谱）
        if 3.5 <= chg <= 4.5:
            score += 20; reasons.append("涨幅甜蜜区")
        elif 3.0 <= chg < 3.5 or 4.5 < chg <= 5.0:
            score += 10
        
        # 量比强度（越高说明今日越活跃）
        if vol_r >= 2.5:
            score += 20; reasons.append("量比超强")
        elif vol_r >= 1.8:
            score += 15; reasons.append("量比较强")
        elif vol_r >= 1.3:
            score += 8
        else:
            score += 3

        # 成交量递增趋势
        if vol_trend.get(code, False):
            score += 12; reasons.append("量能递增")
        elif code not in vol_trend:
            score += 5  # 未检查的给中性分

        # 市值甜蜜点（100-200亿最优，流动性好且不太大）
        mv_y = tot_mv / 1e8  # 转为亿
        if 100 <= mv_y <= 200:
            score += 12; reasons.append("市值适中")
        elif 50 <= mv_y < 100 or 200 < mv_y <= 300:
            score += 6

        # 5分钟涨跌（尾盘仍在上涨说明有持续性）
        if spd5 > 0.5:
            score += 10; reasons.append("尾盘上行")
        elif spd5 > 0:
            score += 5
        elif spd5 < -0.5:
            score -= 8

        # 热门板块
        if industry and industry in hot_sectors:
            score += 10; reasons.append("热门板块")

        # 换手率（活跃但不过热）
        if 3 <= hs <= 8:
            score += 5
        elif hs > 15:
            score -= 5  # 过度换手，风险大

        score = max(5, min(99, round(score)))

        # 操作建议
        if score >= 75:
            sug = "⭐ 强烈关注！量价配合好，2:30-2:50可分批建仓"
        elif score >= 62:
            sug = "👍 值得关注，量比活跃，可轻仓参与"
        elif score >= 50:
            sug = "🔍 一般，建议等量比进一步放大再入场"
        else:
            sug = "⚠️ 量能偏弱，观望为主"

        rows.append({
            "code":        code,
            "name":        name,
            "industry":    industry,
            "change_rate": round(chg, 2),
            "price":       round(price, 2),
            "turnover":    round(hs, 2),
            "vol_ratio":   round(vol_r, 2),
            "total_mv":    round(tot_mv / 1e8, 1),  # 亿
            "amount":      round(amt, 0),
            "spd5":        round(spd5, 2),
            "vol_increasing": vol_trend.get(code, None),
            "score":       score,
            "reasons":     reasons[:4],
            "suggestion":  sug,
            "buyable":     True,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    return {"data": rows[:10], "qdate": today_str(),
            "total": len(rows[:10]), "message": ""}


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

def today_str():
    d = datetime.utcnow() + timedelta(hours=8)
    return d.strftime("%Y%m%d")

def normalize_date(s):
    if not s: return last_trade_day()
    c = s.replace("-", "")
    return c if len(c) == 8 else last_trade_day()

def last_trade_day():
    d = datetime.utcnow() + timedelta(hours=8)
    if d.hour < 15: d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
