"""市场宽度 / 板块排行 / 北向资金 / 分笔明细 —— 公开源聚合。

补齐「每日行情复盘」最常缺的三类数据，全部由桥接进程直连公开接口，
不经过 QMT、不 import xtquant：

- **涨跌家数（赚钱效应）**：东方财富涨跌分布（含涨停/跌停家数），
  腾讯行业板块汇总兜底；两市成交额可附在返回里。
- **板块排行**：腾讯板块排行（行业 hy / 概念 gn / 地域 dy），
  含涨跌幅、主力净流入、板块内涨跌家数、领涨股、5/20/60 日涨幅。
- **北向资金**：净买入自 2024-08-19 起交易所停止披露，公开接口净额
  字段恒为 null —— 本模块**显式返回 available=False**，只给 T+1 的
  沪深股通成交总额，绝不编造估算值。

已知限制：公开非官方端点，字段可能变更；缓存 20~300s 缓解限流；
休市时段返回最近收盘快照。
"""

from __future__ import annotations

import time
from typing import Any

from . import market_public as mp

# ----------------------------------------------------------------------
# 上游端点
# ----------------------------------------------------------------------

# 东方财富涨跌分布（涨停/跌停在 ±11 桶）
_EM_FENBU = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
             "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt")

# 腾讯板块排行 / 板块成分（board_type: hy 行业, gn 概念, dy 地域）
_TX_BOARD_RANK = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
                  "?board_type={bt}&sort_type=PriceRatio&direct=down"
                  "&offset={offset}&count={count}&type=1")
_TX_BOARD_STOCKS = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/"
                    "getBoardRankList?board_code={code}"
                    "&sort_type=PriceRatio&direct=down"
                    "&offset={offset}&count={count}")

# 东方财富分笔明细（pos 为负数表示取最近 N 笔）
_EM_TICK = ("https://push2.eastmoney.com/api/qt/stock/details/get"
            "?secid={secid}&fields1=f1,f2,f3,f4"
            "&fields2=f51,f52,f53,f54,f55&pos=-{count}")

# 东方财富沪深港通历史（T+1 收盘后公布；含成交总额，净额为 null）
_EM_MUTUAL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
              "?reportName=RPT_MUTUAL_DEAL_HISTORY&columns=ALL&pageSize=20"
              "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")

BOARD_TYPES: dict[str, str] = {"hy": "行业", "gn": "概念", "dy": "地域"}

# MUTUAL_TYPE：001 沪股通 / 003 深股通 / 005 北向合计
#               002 港股通(沪) / 004 港股通(深) / 006 南向合计
_MUTUAL_NORTH = {"001": "sh_connect", "003": "sz_connect", "005": "north_total"}
_MUTUAL_SOUTH = {"002": "hk_sh", "004": "hk_sz", "006": "south_total"}

# 上游 DEAL_AMT / NET_DEAL_AMT 单位：百万元 → 亿元（除以 100）。
# 校准依据：北向合计日成交额换算后 2200~3100 亿，与沪深股通历史量级一致。
_BAIWAN_TO_YI = 100.0

_TX_CODE_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def _f(value: Any, default: float | None = 0.0) -> float | None:
    """上游大量字段是字符串（含占位符），统一转 float。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


def _tx_code(raw: Any) -> str:
    """sh600997 → 600997.SH；已带后缀的原样返回。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    if "." in s:
        return s.upper()
    prefix, sym = s[:2].lower(), s[2:]
    if prefix in _TX_CODE_SUFFIX and len(sym) == 6:
        return "%s.%s" % (sym, _TX_CODE_SUFFIX[prefix])
    return s


# ----------------------------------------------------------------------
# 1. 涨跌家数（赚钱效应）
# ----------------------------------------------------------------------

def _two_market_amount() -> float | None:
    """沪深两市成交额（亿元），失败不影响主流程。"""
    try:
        ticks = mp.get_full_tick(["000001.SH", "399001.SZ"])
        total = sum(float(t.get("amount") or 0) for t in ticks.values())
        return round(total / 1e8, 2) if total else None
    except Exception:
        return None


def _breadth_em() -> dict:
    resp = mp._get_json(_EM_FENBU, cache_ttl=20.0)
    data = (resp or {}).get("data") or {}
    counts: dict[int, int] = {}
    for item in data.get("fenbu") or []:
        if not isinstance(item, dict):
            continue
        for key, val in item.items():
            try:
                counts[int(key)] = int(val)
            except (TypeError, ValueError):
                continue
    if not counts:
        raise RuntimeError("eastmoney fenbu empty")

    up = sum(v for k, v in counts.items() if k > 0)
    down = sum(v for k, v in counts.items() if k < 0)
    flat = counts.get(0, 0)
    # 桶 ±11 是「≥11% / ≤-11%」，即涨停 / 跌停（含 20cm 板）
    limit_up = counts.get(11, 0)
    limit_down = counts.get(-11, 0)

    return {
        "date": str(data.get("qdate") or ""),
        "source": "eastmoney:getTopicZDFenBu",
        "total": up + down + flat,
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "up_ge_5": sum(v for k, v in counts.items() if 5 <= k < 11),
        "down_le_minus5": sum(v for k, v in counts.items() if -11 < k <= -5),
        "up_ratio": round(up / (up + down), 4) if (up + down) else 0.0,
        "net_advances": up - down,
        "distribution": [{"bucket": k, "count": counts[k]}
                         for k in sorted(counts)],
        "turnover_yi": _two_market_amount(),
    }


def _breadth_tx() -> dict:
    """腾讯兜底：按行业板块汇总（只给涨/跌家数，无涨停统计）。"""
    boards = get_boards("hy")
    up = sum(b["up_count"] or 0 for b in boards)
    total = sum(b["member_count"] or 0 for b in boards)
    if not total:
        raise RuntimeError("tencent board summary empty")
    return {
        "date": time.strftime("%Y%m%d"),
        "source": "tencent:board_summary",
        "total": total,
        "up": up,
        "down": total - up,
        "flat": None,
        "limit_up": None,
        "limit_down": None,
        "up_ge_5": None,
        "down_le_minus5": None,
        "up_ratio": round(up / total, 4) if total else 0.0,
        "net_advances": up - (total - up),
        "distribution": None,
        "turnover_yi": _two_market_amount(),
    }


def get_breadth() -> dict:
    """涨跌家数 / 涨停跌停 / 涨跌分布 / 两市成交额。"""
    errors: list[str] = []
    for fn in (_breadth_em, _breadth_tx):
        try:
            return fn()
        except Exception as exc:                      # noqa: BLE001
            errors.append("%s: %s" % (fn.__name__, exc))
    raise RuntimeError("breadth sources failed: %s" % "; ".join(errors))


# ----------------------------------------------------------------------
# 2. 板块排行 / 成分
# ----------------------------------------------------------------------

_PAGE = 200


def _norm_board(row: dict, board_type: str) -> dict:
    # zgb 形如 "21/33"：上涨家数 / 成分总数
    raw_zgb = str(row.get("zgb") or "")
    up_cnt = tot_cnt = None
    if "/" in raw_zgb:
        a, _, b = raw_zgb.partition("/")
        up_cnt = int(a) if a.strip().isdigit() else None
        tot_cnt = int(b) if b.strip().isdigit() else None
    down_cnt = (tot_cnt - up_cnt
                if up_cnt is not None and tot_cnt is not None else None)
    leader = row.get("lzg") or {}
    return {
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "type": board_type,
        "type_name": BOARD_TYPES[board_type],
        "pct_chg": _f(row.get("zdf")),
        "change": _f(row.get("zd")),
        "main_net_inflow_wan": _f(row.get("zljlr")),
        "turnover_wan": _f(row.get("turnover")),
        "volume_lot": _f(row.get("volume")),
        "turnover_rate": _f(row.get("hsl")),
        "vol_ratio": _f(row.get("lb")),
        "float_mktcap_yi": _f(row.get("ltsz")),
        "speed": _f(row.get("speed")),
        "pct_chg_5d": _f(row.get("zdf_d5")),
        "pct_chg_20d": _f(row.get("zdf_d20")),
        "pct_chg_60d": _f(row.get("zdf_d60")),
        "pct_chg_52w": _f(row.get("zdf_w52")),
        "pct_chg_ytd": _f(row.get("zdf_y")),
        "up_count": up_cnt,
        "down_count": down_cnt,
        "member_count": tot_cnt,
        "leader": ({
            "code": _tx_code(leader.get("code")),
            "name": str(leader.get("name") or ""),
            "pct_chg": _f(leader.get("zdf")),
            "price": _f(leader.get("zxj")),
        } if leader else None),
    }


def get_boards(board_type: str = "hy") -> list[dict]:
    """板块全量列表（已按涨跌幅降序，上游即按此排序）。"""
    bt = (board_type or "hy").strip().lower()
    if bt not in BOARD_TYPES:
        raise ValueError("unsupported board_type: %s (hy/gn/dy)" % board_type)

    out: list[dict] = []
    offset = 0
    while offset < 2000:
        url = _TX_BOARD_RANK.format(bt=bt, offset=offset, count=_PAGE)
        resp = mp._get_json(url, cache_ttl=60.0)
        data = (resp or {}).get("data") or {}
        rows = data.get("rank_list") or []
        if not rows:
            break
        out.extend(_norm_board(r, bt) for r in rows)
        total = int(_f(data.get("total"), 0) or 0)
        offset += len(rows)
        if total and offset >= total:
            break
        if len(rows) < _PAGE:
            break
    if not out:
        raise RuntimeError("tencent board rank empty: %s" % bt)
    return out


# 排序字段白名单（上游只支持 PriceRatio/turnover/speed，其余本地排序）
SORT_FIELDS = (
    "pct_chg", "main_net_inflow_wan", "turnover_wan", "turnover_rate",
    "vol_ratio", "float_mktcap_yi", "pct_chg_5d", "pct_chg_20d",
    "pct_chg_60d", "pct_chg_ytd", "up_count",
)


def get_sector_rank(board_type: str = "hy", sort: str = "pct_chg",
                    order: str = "desc", limit: int = 20,
                    min_member: int = 0) -> dict:
    """板块排行：{type, count, items:[...]}。"""
    boards = get_boards(board_type)
    if min_member:
        boards = [b for b in boards if (b["member_count"] or 0) >= min_member]
    key = sort if sort in SORT_FIELDS else "pct_chg"
    boards = [b for b in boards if b.get(key) is not None]
    boards.sort(key=lambda b: b[key], reverse=(order != "asc"))
    n = max(1, min(int(limit or 20), 200))
    return {
        "type": board_type,
        "sort": key,
        "order": "asc" if order == "asc" else "desc",
        "count": len(boards),
        "items": boards[:n],
    }


def resolve_board(sector: str, board_type: str = "") -> tuple[str, str]:
    """板块名或板块代码 → (code, name)；名称匹配支持「行业板块.煤炭」写法。"""
    s = (sector or "").strip()
    if not s:
        raise ValueError("sector is required")
    want = s.split(".")[-1].strip() if "." in s else s
    types = [board_type] if board_type in BOARD_TYPES else list(BOARD_TYPES)
    for bt in types:
        for b in get_boards(bt):
            if s == b["code"] or want == b["name"]:
                return b["code"], b["name"]
    raise ValueError("unknown sector: %s (use /api/sector/list)" % sector)


def _norm_stock(row: dict) -> dict:
    return {
        "code": _tx_code(row.get("code")),
        "name": str(row.get("name") or ""),
        "price": _f(row.get("zxj")),
        "pct_chg": _f(row.get("zdf")),
        "change": _f(row.get("zd")),
        "amplitude": _f(row.get("zf")),
        "turnover_wan": _f(row.get("turnover")),
        "volume_lot": _f(row.get("volume")),
        "turnover_rate": _f(row.get("hsl")),
        "vol_ratio": _f(row.get("lb")),
        "pe_ttm": _f(row.get("pe_ttm"), None),
        "pb": _f(row.get("pn"), None),
        "float_mktcap_yi": _f(row.get("ltsz")),
        "total_mktcap_yi": _f(row.get("zsz")),
    }


def get_sector_stocks(sector: str, board_type: str = "", sort: str = "pct_chg",
                      order: str = "desc", limit: int = 0) -> dict:
    """板块成分股（含行情），可按涨幅/成交额等在本地排序。"""
    code, name = resolve_board(sector, board_type)

    rows: list[dict] = []
    offset = 0
    while offset < 3000:
        url = _TX_BOARD_STOCKS.format(code=code, offset=offset, count=100)
        resp = mp._get_json(url, cache_ttl=20.0)
        data = (resp or {}).get("data") or {}
        chunk = data.get("rank_list") or []
        if not chunk:
            break
        rows.extend(_norm_stock(r) for r in chunk)
        total = int(_f(data.get("total"), 0) or 0)
        offset += len(chunk)
        if total and offset >= total:
            break
        if len(chunk) < 100:
            break

    key = sort if sort in SORT_FIELDS else "pct_chg"
    rows.sort(key=lambda r: r.get(key) or 0.0, reverse=(order != "asc"))
    total_members = len(rows)
    if limit and int(limit) > 0:
        rows = rows[:int(limit)]
    return {
        "sector": name,
        "code": code,
        "member_count": total_members,
        "sort": key,
        "order": "asc" if order == "asc" else "desc",
        "stocks": rows,
        "codes": [r["code"] for r in rows],
    }


# ----------------------------------------------------------------------
# 3. 北向资金
# ----------------------------------------------------------------------

_NORTH_NOTE = (
    "北向资金净买入自 2024-08-19 起被交易所停止实时披露，公开接口净额字段"
    "恒为 null，本接口不提供任何估算/替代数值；仅提供 T+1 收盘后公布的"
    "沪深股通成交总额（不等于净买入，不可用于判断外资净流入方向）。"
)


def _mutual_rows() -> list[dict]:
    resp = mp._get_json(_EM_MUTUAL, cache_ttl=300.0)
    return ((resp or {}).get("result") or {}).get("data") or []


def get_northbound() -> dict:
    """北向资金：净额显式不可用，成交总额 T+1。"""
    rows = _mutual_rows()
    if not rows:
        raise RuntimeError("eastmoney mutual rows empty")

    latest = str(rows[0].get("TRADE_DATE") or "")[:10]
    north: dict[str, dict] = {}
    south: dict[str, dict] = {}
    for row in rows:
        if str(row.get("TRADE_DATE") or "")[:10] != latest:
            continue
        mtype = str(row.get("MUTUAL_TYPE") or "")
        deal = _f(row.get("DEAL_AMT"), None)
        net = _f(row.get("NET_DEAL_AMT"), None)
        item = {
            "turnover_yi": round(deal / _BAIWAN_TO_YI, 2) if deal else None,
            "net_inflow_yi": round(net / _BAIWAN_TO_YI, 2) if net is not None else None,
        }
        if mtype in _MUTUAL_NORTH:
            north[_MUTUAL_NORTH[mtype]] = item
        elif mtype in _MUTUAL_SOUTH:
            south[_MUTUAL_SOUTH[mtype]] = item

    today = time.strftime("%Y-%m-%d")
    north_total = (north.get("north_total") or {})
    return {
        "date": latest,
        "is_today": latest == today,
        "available": False,
        "net_inflow_yi": None,
        "north": {
            "net_inflow_yi": None,
            "turnover_yi": north_total.get("turnover_yi"),
            "sh_connect_turnover_yi": (north.get("sh_connect") or {}).get("turnover_yi"),
            "sz_connect_turnover_yi": (north.get("sz_connect") or {}).get("turnover_yi"),
        },
        "south": {
            "turnover_yi": (south.get("south_total") or {}).get("turnover_yi"),
            "hk_sh_turnover_yi": (south.get("hk_sh") or {}).get("turnover_yi"),
            "hk_sz_turnover_yi": (south.get("hk_sz") or {}).get("turnover_yi"),
        },
        "note": _NORTH_NOTE,
        "units": {"turnover": "亿元", "net_inflow": "亿元（恒为 null）"},
        "source": "eastmoney:datacenter RPT_MUTUAL_DEAL_HISTORY",
    }


# ----------------------------------------------------------------------
# 4. 分笔明细
# ----------------------------------------------------------------------

# 腾讯分笔（东财 details 被限流时的兜底）：先查 info 拿分页，再取最后一页
_TX_DETAIL_INFO = ("https://stock.gtimg.cn/data/index.php"
                   "?appn=detail&action=info&c={tx}")
_TX_DETAIL_DATA = ("https://stock.gtimg.cn/data/index.php"
                   "?appn=detail&action=data&c={tx}&p={page}")

# 东财 5 号字段：1 卖盘 / 2 买盘 / 4 中性盘
_EM_SIDE = {"1": "sell", "2": "buy", "4": "neutral"}
# 腾讯尾字母：B 买 / S 卖 / M 中性
_TX_SIDE = {"B": "buy", "S": "sell", "M": "neutral"}


def _tx_symbol(code: str) -> str:
    sym, _, mkt = code.strip().upper().partition(".")
    if mkt not in ("SH", "SZ"):
        raise ValueError("tencent detail unsupported market: %s" % code)
    return mkt.lower() + sym


def _ticks_em(code: str, count: int) -> dict:
    url = _EM_TICK.format(secid=mp.secid(code), count=count)
    resp = mp._get_json(url, cache_ttl=2.0)
    data = (resp or {}).get("data") or {}
    ticks: list[dict] = []
    for line in data.get("details") or []:
        parts = str(line).split(",")
        if len(parts) < 4:
            continue
        volume_lot = _f(parts[2], 0.0) or 0.0
        price = _f(parts[1], 0.0) or 0.0
        flag = parts[4] if len(parts) > 4 else ""
        ticks.append({
            "time": parts[0],
            "price": price,
            "volume_lot": volume_lot,
            "volume": int(volume_lot * 100),
            "amount": round(price * volume_lot * 100, 2),
            "direction": _EM_SIDE.get(flag),
            "flag": flag,
        })
    if not ticks:
        raise RuntimeError("eastmoney details empty")
    return {
        "stock": code.upper(),
        "pre_close": _f(data.get("prePrice"), None),
        "count": len(ticks),
        "ticks": ticks,
        "source": "eastmoney:stock/details",
    }


def _tx_pages(info: str) -> int:
    """info 返回 20261029,"09:25:01~09:33:24|09:33:27~..."，段数即页数。"""
    text = str(info)
    if '"' not in text:
        return 1
    body = text.split('"')[1]
    return max(1, len([s for s in body.split("|") if s.strip()]))


def _ticks_tx(code: str, count: int) -> dict:
    tx = _tx_symbol(code)
    pages = _tx_pages(mp._get_text(_TX_DETAIL_INFO.format(tx=tx)))

    body = mp._get_text(_TX_DETAIL_DATA.format(tx=tx, page=pages - 1))
    rows: list[dict] = []
    for item in str(body).split('"')[1:2]:
        for seg in item.split("|"):
            parts = seg.split("/")
            if len(parts) < 7:
                continue
            volume_lot = _f(parts[4], 0.0) or 0.0
            rows.append({
                "time": parts[1],
                "price": _f(parts[2], 0.0) or 0.0,
                "change": _f(parts[3], 0.0) or 0.0,
                "volume_lot": volume_lot,
                "volume": int(volume_lot * 100),
                "amount": _f(parts[5], 0.0) or 0.0,
                "direction": _TX_SIDE.get(parts[6].strip().upper()),
                "flag": parts[6].strip(),
            })
    if not rows:
        raise RuntimeError("tencent detail empty")
    rows = rows[-count:]
    return {
        "stock": code.upper(),
        "pre_close": _pre_close(code),
        "count": len(rows),
        "ticks": rows,
        "source": "tencent:stock.gtimg.cn detail",
    }


def _pre_close(code: str) -> float | None:
    try:
        tick = mp.get_full_tick([code]).get(code) or {}
        return _f(tick.get("lastClose"), None)
    except Exception:                                 # noqa: BLE001
        return None


def get_ticks(code: str, count: int = 30) -> dict:
    """最近 N 笔分笔成交（东财主源，腾讯兜底；成交量单位：手/股并列）。"""
    n = max(1, min(int(count or 30), 500))
    errors: list[str] = []
    for fn in (_ticks_em, _ticks_tx):
        try:
            return fn(code, n)
        except Exception as exc:                      # noqa: BLE001
            errors.append("%s: %s" % (fn.__name__, exc))
    raise RuntimeError("tick sources failed: %s" % "; ".join(errors))
