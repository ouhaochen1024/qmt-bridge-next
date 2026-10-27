"""公开行情源 —— 东方财富为主、腾讯行情兜底（桥接端自取，不经过 QMT）。

信箱执行器只负责账户与交易；行情数据由桥接进程直接从公开 HTTP 接口
获取并翻译成旧版 xtdata 形状，使 Skill 层无感切换。

已知限制：
- 数据非 QMT 本地源，可能有秒级延迟；盘后/休市返回最近收盘数据。
- 五档盘口暂不提供（askPrice/bidPrice 填 0）。
- 上游接口为非官方公开端点，字段若变更需在此处适配。
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HEADERS_EM = {"User-Agent": _UA, "Accept": "*/*",
               "Referer": "https://quote.eastmoney.com/"}
_HEADERS_PLAIN = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
_TIMEOUT = 4
_RETRY_DELAYS = (0.5, 1.2)
_CACHE: dict[str, tuple[float, Any]] = {}


def _headers_for(url: str) -> dict:
    # 腾讯行情对带第三方 Referer/Chrome 完整 UA 的请求会直接断连，
    # 只用最朴素的头；EM 侧则要浏览器式头。
    if "gtimg.cn" in url or "qq.com" in url:
        return _HEADERS_PLAIN
    return _HEADERS_EM

# Eastmoney klt 与 xtdata period 映射
_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1d": 101,
        "1w": 102, "1M": 103}
# xtdata dividend_type → Eastmoney fqt（0 不复权 1 前复权 2 后复权）
_FQT = {"none": 0, "front": 1, "back": 2, "front_ratio": 1}


def _get_json(url: str, cache_ttl: float = 0.0) -> Any:
    key = url
    if cache_ttl:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < cache_ttl:
            return hit[1]
    last: Exception | None = None
    for i, delay in enumerate((0.0,) + _RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers=_headers_for(url))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if cache_ttl:
                _CACHE[key] = (time.time(), data)
            return data
        except Exception as exc:
            last = exc
    raise last  # type: ignore[misc]


def _get_text(url: str, encoding: str = "gbk") -> str:
    last: Exception | None = None
    for delay in (0.0,) + _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers=_headers_for(url))
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.read().decode(encoding, "replace")
        except Exception as exc:
            last = exc
    raise last  # type: ignore[misc]


def _num(value: Any, default: float = 0.0) -> float:
    """Eastmoney 在停牌/非交易时段会回 '-' 之类占位串。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def secid(code: str) -> str:
    """600519.SH → 1.600519；000001.SZ → 0.000001；399001.SZ → 0.399001。"""
    c = code.strip().upper()
    sym, _, mkt = c.partition(".")
    if mkt == "SH":
        return "1." + sym
    if mkt == "SZ":
        return "0." + sym
    if mkt == "BJ":
        return "0." + sym
    raise ValueError("unsupported market in code: %s" % code)


def _secids(codes: list[str]) -> str:
    return ",".join(secid(c) for c in codes)


def _parse_yyyymmdd(s: str) -> str:
    """YYYYMMDD → YYYY-MM-DD（Eastmoney kline beg/end 参数格式）。"""
    s = (s or "").strip()
    if len(s) >= 8:
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return ""


# ----------------------------------------------------------------------
# 实时快照（full_tick / indices / batch_stock_name）
# ----------------------------------------------------------------------

_EM_ULIST = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
             "?fltt=2&invt=2&pn=1&pz=60&po=1"
             "&fields=f2,f5,f6,f12,f13,f14,f15,f16,f17,f18"
             "&secids={secids}")


def _fetch_em_quotes(codes: list[str]) -> dict[str, dict]:
    """批量实时快照 → {code: xtdata-tick 形状}。"""
    out: dict[str, dict] = {}
    # ulist 单次上限 ~60，分批
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        url = _EM_ULIST.format(secids=_secids(chunk))
        resp = _get_json(url, cache_ttl=3.0)
        diff = ((resp or {}).get("data") or {}).get("diff") or []
        rows = diff.values() if isinstance(diff, dict) else diff
        for row in rows:
            sym = str(row.get("f12") or "")
            mkt = str(row.get("f13") or "")
            full = ("%s.%s" % (sym, "SH" if mkt == "1" else "SZ"))
            last = _num(row.get("f2"))
            pre = _num(row.get("f18"))
            out[full] = {
                "time": int(time.time() * 1000),
                "lastPrice": last,
                "lastClosed": pre,
                "lastClose": pre,
                "open": _num(row.get("f17")),
                "high": _num(row.get("f15")),
                "low": _num(row.get("f16")),
                "volume": int(_num(row.get("f5"))) * 100,   # 手 → 股
                "amount": _num(row.get("f6")),
                "stockName": str(row.get("f14") or ""),
                "askPrice": [0.0] * 5,
                "bidPrice": [0.0] * 5,
                "askVol": [0] * 5,
                "bidVol": [0] * 5,
            }
    return out


def _fetch_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """腾讯行情：qt.gtimg.cn/q=sz000001,sh600519（含五档盘口）。

    代码必须是【前缀格式】sz000001 / sh600519（市场前缀 + 6 位代码）。
    行内字段（~ 分隔）：1 名称, 2 代码, 3 现价, 4 昨收, 5 今开, 6 成交量(手),
    33 最高, 34 最低, 37 成交额(万)；9~18 为买一..买五的价/量交错，
    19~28 为卖一..卖五的价/量交错。
    """
    tx_codes = []
    for code in codes:
        sym, _, mkt = code.strip().upper().partition(".")
        if mkt not in ("SH", "SZ"):
            raise ValueError("tencent source unsupported market: %s" % code)
        tx_codes.append((mkt.lower() + sym, code))
    text = _get_text("https://qt.gtimg.cn/q=" + ",".join(t for t, _ in tx_codes))
    out: dict[str, dict] = {}
    lines = [ln.split("=", 1) for ln in text.split(";") if "=" in ln]
    by_prefix = dict(tx_codes)
    for key, body in lines:
        key = key.strip()
        if not key.startswith("v_") or key == "v_pv_none_match":
            continue
        parts = body.strip('"').split("~")
        if len(parts) < 38:
            continue
        mkt_prefix = key[2:4]          # "sz" / "sh"
        sym = key[4:]
        full = "%s.%s" % (sym, mkt_prefix.upper())
        orig = by_prefix.get(mkt_prefix + sym) or (
            "%s.%s" % (parts[2], mkt_prefix.upper()))
        _ = orig  # 兼容旧变量名；full 已可直接映射

        def px(i: int) -> float:
            return _num(parts[i]) if 0 < i < len(parts) else 0.0

        # 腾讯字段：9~18 = 买一..买五（价/量交错），19~28 = 卖一..卖五。
        # 买盘价由高到低（买一最高），卖盘价由低到高（卖一最低）。
        bid_price = [px(9), px(11), px(13), px(15), px(17)]
        bid_vol = [int(px(10)), int(px(12)), int(px(14)), int(px(16)), int(px(18))]
        ask_price = [px(19), px(21), px(23), px(25), px(27)]
        ask_vol = [int(px(20)), int(px(22)), int(px(24)), int(px(26)), int(px(28))]
        out[full] = {
            "time": int(time.time() * 1000),
            "lastPrice": _num(parts[3]),
            "lastClosed": _num(parts[4]),
            # 兼容旧 qmt-bridge / xtdata 客户端的字段名（Skill 侧长期读 lastClose）。
            "lastClose": _num(parts[4]),
            "open": _num(parts[5]),
            "high": _num(parts[33]) if len(parts) > 33 else 0.0,
            "low": _num(parts[34]) if len(parts) > 34 else 0.0,
            "volume": int(_num(parts[6])) * 100,
            "amount": _num(parts[37]) * 10000 if len(parts) > 37 else 0.0,
            "stockName": parts[1],
            "askPrice": ask_price,
            "bidPrice": bid_price,
            "askVol": ask_vol,
            "bidVol": bid_vol,
        }
    return out


def get_full_tick(codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    # 腾讯为主（稳定且带五档），EM 兜底；两者都失败才抛错。
    errors: list[str] = []
    try:
        result = _fetch_tencent_quotes(codes)
        if len(result) >= len(codes):
            return result
        missing = [c for c in codes if c not in result]
        errors.append("tencent partial: %d/%d" % (len(result), len(codes)))
        result.update(_fetch_em_quotes(missing))
        if result:
            return result
    except Exception as exc:
        errors.append("tencent: %s" % exc)
        try:
            result = _fetch_em_quotes(codes)
            if result:
                return result
        except Exception as exc2:
            errors.append("eastmoney: %s" % exc2)
    raise RuntimeError("quote sources failed: %s" % "; ".join(errors))


MAJOR_INDICES = [
    "000001.SH", "399001.SZ", "399006.SZ", "000300.SH",
    "000016.SH", "000905.SH", "000852.SH",
]


def get_stock_names(codes: list[str]) -> dict[str, str]:
    quotes = get_full_tick(codes)
    return {c: str(q.get("stockName") or "") for c, q in quotes.items()}


# ----------------------------------------------------------------------
# K 线（market_data_ex / market_data）
# ----------------------------------------------------------------------

_EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
             "?fields1=f1,f2,f3,f4,f5,f6"
             "&fields2=f51,f52,f53,f54,f55,f56,f57"
             "&klt={klt}&fqt={fqt}&end=20500101&lmt={lmt}&secid={secid}")


def _parse_period(period: str) -> int:
    return _KLT.get((period or "1d").strip(), 101)


def _kline_records(code: str, period: str, start_time: str, end_time: str,
                   count: int, dividend_type: str) -> list[dict]:
    try:
        records = _em_kline_records(code, period, start_time, end_time,
                                    count, dividend_type)
    except Exception:
        # EM 限流/不可达 → 腾讯日级/分钟级 K 线兜底（不含复权）。
        records = _tx_kline_records(code, period, count)
    # start/end 过滤对两个源统一生效
    start = (start_time or "").strip()
    end = (end_time or "").strip()
    if start or end:
        filtered = []
        for rec in records:
            compact = time.strftime("%Y%m%d%H%M%S", time.localtime(rec["time"] / 1000))
            if start and compact[:len(start)] < start:
                continue
            if end and compact[:len(end)] > end:
                continue
            filtered.append(rec)
        records = filtered
    if count and count > 0:
        records = records[-count:]
    return records


_TX_KLT = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60",
           "1d": "day"}


def _tx_kline_records(code: str, period: str, count: int) -> list[dict]:
    """腾讯 K 线兜底：web.ifzq.gtimg.cn fqkline（不复权）。"""
    sym, _, mkt = code.strip().upper().partition(".")
    if mkt not in ("SH", "SZ"):
        raise ValueError("tencent kline unsupported market: %s" % code)
    tx = mkt.lower() + sym
    klt = _TX_KLT.get((period or "1d").strip(), "day")
    n = count if count and count > 0 else 320
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=%s,%s,,,%d," % (tx, klt, min(n, 640)))
    resp = _get_json(url, cache_ttl=60.0)
    node = ((resp or {}).get("data") or {}).get(tx) or {}
    rows = node.get("qfqday") or node.get("day") or node.get(klt) or []
    records: list[dict] = []
    for row in rows:
        if len(row) < 6:
            continue
        date_str = str(row[0]).replace("-", "")
        compact = date_str + ("150000" if len(date_str) == 8 else "")
        try:
            ts = time.mktime(time.strptime(compact, "%Y%m%d%H%M%S"))
        except ValueError:
            continue
        records.append({
            "time": ts * 1000.0,
            "open": _num(row[1]),
            "close": _num(row[2]),
            "high": _num(row[3]),
            "low": _num(row[4]),
            "volume": int(_num(row[5])) * 100,
            "amount": _num(row[6]) if len(row) > 6 else 0.0,
        })
    return records


def _em_kline_records(code: str, period: str, start_time: str, end_time: str,
                      count: int, dividend_type: str) -> list[dict]:
    klt = _parse_period(period)
    fqt = _FQT.get((dividend_type or "none").strip(), 0)
    lmt = count if count and count > 0 else 800
    url = _EM_KLINE.format(klt=klt, fqt=fqt, lmt=lmt, secid=secid(code))
    resp = _get_json(url, cache_ttl=60.0)
    klines = ((resp or {}).get("data") or {}).get("klines") or []
    records: list[dict] = []
    start = (start_time or "").strip()
    end = (end_time or "").strip()
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        date_str = parts[0]                      # 2026-10-27 或 2026-10-27 15:00
        compact = date_str.replace("-", "").replace(":", "").replace(" ", "")
        if len(compact) == 8:
            compact += "150000" if klt == 101 else "000000"
        if start and compact[:len(start)] < start:
            continue
        if end and compact[:len(end)] > end:
            continue
        try:
            ts = time.mktime(time.strptime(compact, "%Y%m%d%H%M%S"))
        except ValueError:
            continue
        records.append({
            "time": ts * 1000.0,                 # xtdata ex 形状：epoch 毫秒
            "open": _num(parts[1]),
            "close": _num(parts[2]),
            "high": _num(parts[3]),
            "low": _num(parts[4]),
            "volume": int(_num(parts[5])) * 100,  # 手 → 股
            "amount": _num(parts[6]),
        })
    if count and count > 0:
        records = records[-count:]
    return records


def get_market_data_ex(stocks: list[str], period: str = "1d",
                       start_time: str = "", end_time: str = "",
                       count: int = -1, dividend_type: str = "none",
                       ) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    errors: list[str] = []
    for code in stocks:
        try:
            out[code] = _kline_records(code, period, start_time, end_time,
                                       count, dividend_type)
        except Exception as exc:
            errors.append("%s: %s" % (code, exc))
            out[code] = []
    if errors and not any(out.values()):
        raise RuntimeError("all kline sources failed: %s" % "; ".join(errors[:3]))
    return out


def get_market_data(stocks: list[str], fields: str = "open,high,low,close,volume",
                    period: str = "1d", start_time: str = "",
                    end_time: str = "", count: int = -1,
                    dividend_type: str = "none") -> dict[str, dict]:
    """xtdata.get_market_data 形状：{field: {stock: [values]}}。"""
    ex = get_market_data_ex(stocks, period, start_time, end_time, count,
                            dividend_type)
    want = [f.strip() for f in (fields or "").split(",") if f.strip()]
    out: dict[str, dict] = {}
    for field in want:
        out[field] = {
            code: [rec.get(field) for rec in recs]
            for code, recs in ex.items()
        }
    return out


# ----------------------------------------------------------------------
# 交易日历（以上证指数日 K 的日期序列为准）
# ----------------------------------------------------------------------

def get_trading_dates(market: str = "SH", start_time: str = "",
                      end_time: str = "", count: int = -1) -> list[float]:
    code = "000001.SH" if (market or "SH").upper() in ("SH", "SZ") else "000001.SH"
    records = _kline_records(code, "1d", start_time, end_time,
                             count if count and count > 0 else -1, "none")
    dates = [r["time"] for r in records]
    if count and count > 0:
        dates = dates[-count:]
    return dates


# ----------------------------------------------------------------------
# 交易日历（is_trading / prev / next / period）
# ----------------------------------------------------------------------

def _calendar_dates() -> list[str]:
    """升序交易日列表（YYYYMMDD），缓存 1 小时。"""
    hit = _CACHE.get("__calendar__")
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    records = _kline_records("000001.SH", "1d", "", "", -1, "none")
    dates = sorted(time.strftime("%Y%m%d", time.localtime(r["time"] / 1000))
                   for r in records)
    _CACHE["__calendar__"] = (time.time(), dates)
    return dates


def _normalize_date(date: str) -> str:
    return (date or "").strip().replace("-", "")


def is_trading_date(market: str, date: str) -> bool:
    d = _normalize_date(date) or time.strftime("%Y%m%d")
    return d in _calendar_dates()


def prev_trading_date(market: str, date: str) -> str | None:
    d = _normalize_date(date) or time.strftime("%Y%m%d")
    dates = _calendar_dates()
    prev = [x for x in dates if x < d]
    return prev[-1] if prev else None


def next_trading_date(market: str, date: str) -> str | None:
    d = _normalize_date(date) or time.strftime("%Y%m%d")
    dates = _calendar_dates()
    nxt = [x for x in dates if x > d]
    return nxt[0] if nxt else None


def trading_period(market: str = "SH") -> dict:
    """按北京时间推断当前交易时段（本地实现，无上游请求）。"""
    now = time.localtime()
    hm = now.tm_hour * 100 + now.tm_min
    if 915 <= hm < 930:
        period = "auction"        # 集合竞价
    elif (930 <= hm < 1130) or (1300 <= hm < 1500):
        period = "trading"         # 连续交易
    else:
        period = "closed"          # 休市
    return {
        "market": market,
        "period": period,
        "phases": ["auction 09:15-09:25", "trading 09:30-11:30",
                   "trading 13:00-15:00"],
        "trading_day": time.strftime("%Y%m%d", now),
    }
