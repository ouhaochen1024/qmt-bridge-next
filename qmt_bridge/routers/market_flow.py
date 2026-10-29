"""市场宽度 / 板块 / 北向资金 / 分笔 —— 公开源路由。

补齐旧构建返回 501 的「复盘三件套」：涨跌家数、板块排行、北向资金。
响应形状是本桥接自定义的扁平结构（旧 xtdata 无对应接口，无兼容包袱），
字段与单位写在返回里，Skill 层直接消费。

错误语义：参数/板块名错误 → 4xx（不要伪装成上游故障）；上游不可达 → 502。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import market_flow as mf

router = APIRouter(tags=["market-flow"])


def _call(fn, *args, bad_request_status: int = 400, **kwargs):
    """上游异常 → 502；ValueError（参数/板块名无效）→ 指定的 4xx。"""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=bad_request_status,
                            detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="public market data upstream failed: %s: %s"
                   % (type(exc).__name__, exc),
        ) from exc


@router.get("/api/market/breadth")
def market_breadth():
    """涨跌家数 / 涨停跌停 / 涨跌分布 / 两市成交额（赚钱效应用）。"""
    return {"data": _call(mf.get_breadth)}


@router.get("/api/market/northbound")
def market_northbound():
    """北向资金：净额不可用（2024-08-19 起停止披露），仅 T+1 成交总额。"""
    return {"data": _call(mf.get_northbound)}


@router.get("/api/sector/list")
def sector_list(
    type: str = Query("hy", description="hy 行业 / gn 概念 / dy 地域 / all 全部"),
):
    """板块列表（按涨跌幅降序）。`sectors` 为旧客户端兼容的名称数组。"""
    bt = (type or "hy").strip().lower()
    types = list(mf.BOARD_TYPES) if bt == "all" else [bt]
    boards: list[dict] = []
    for t in types:
        boards.extend(_call(mf.get_boards, t))
    boards.sort(key=lambda b: b["pct_chg"] or 0.0, reverse=True)
    return {
        "type": bt,
        "count": len(boards),
        "sectors": [b["name"] for b in boards],
        "boards": boards,
    }


@router.get("/api/sector/rank")
def sector_rank(
    type: str = Query("hy", description="hy / gn / dy"),
    sort: str = Query("pct_chg", description=",".join(mf.SORT_FIELDS)),
    order: str = Query("desc", description="desc / asc"),
    limit: int = Query(20, ge=1, le=200),
    min_member: int = Query(0, ge=0, description="成分股数量下限，过滤迷你板块"),
):
    """板块排行（含主力净流入、板块内涨跌家数、领涨股）。"""
    return {"data": _call(mf.get_sector_rank, type, sort, order,
                          limit, min_member)}


@router.get("/api/sector/stocks")
def sector_stocks(
    sector: str = Query(..., description="板块名称或代码，如 煤炭 / pt01801950"),
    type: str = Query("", description="可选：限定 hy/gn/dy 加速解析"),
    sort: str = Query("pct_chg"),
    order: str = Query("desc"),
    limit: int = Query(0, ge=0, le=3000, description="0 = 全部"),
):
    """板块成分股（含行情，本地排序）。

    `stocks` 是纯代码数组（旧客户端契约），`items` 是带行情的明细。
    """
    data = _call(mf.get_sector_stocks, sector, type, sort, order, limit,
                 bad_request_status=404)
    return {
        "sector": data["sector"],
        "code": data["code"],
        "member_count": data["member_count"],
        "count": len(data["stocks"]),
        "sort": data["sort"],
        "order": data["order"],
        "stocks": data["codes"],
        "items": data["stocks"],
    }


@router.get("/api/sector_stocks")
def sector_stocks_legacy(sector: str = Query(...)):
    """legacy 别名：只返回成分代码数组。"""
    data = _call(mf.get_sector_stocks, sector, "", "pct_chg", "desc", 0,
                 bad_request_status=404)
    return {"sector": data["sector"], "stocks": data["codes"]}


@router.get("/api/tick/details")
def tick_details(
    stock: str = Query(..., description="单只代码，如 600519.SH"),
    count: int = Query(30, ge=1, le=500),
):
    """最近 N 笔分笔成交（东财主源，腾讯兜底）。"""
    return {"data": _call(mf.get_ticks, stock.strip(), count)}
