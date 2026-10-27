"""公开行情源路由 —— 东方财富/腾讯公开接口，不经过 QMT。

覆盖 Skill 实际依赖的 6 条行情路由，响应形状与旧版 xtdata 一致，
Skill 层无感切换。其余行情路由仍走 501 unsupported 存根
（见 routers/unsupported.py 与 docs/limitations.md）。

数据说明：非 QMT 本地源，可能有秒级延迟；休市时段返回最近收盘数据；
五档盘口暂不提供（填 0）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import market_public as mp
from .deps import ok_response

router = APIRouter(tags=["market-public"])


def _codes(raw: str) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _upstream_guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=502,
            detail="public market data upstream failed: %s: %s"
                   % (type(exc).__name__, exc),
        ) from exc


@router.get("/api/market/full_tick")
def full_tick(stocks: str = Query(..., description="逗号分隔，如 000001.SZ,600519.SH")):
    return {"data": _upstream_guard(mp.get_full_tick, _codes(stocks))}


@router.get("/api/full_tick")
def full_tick_legacy(stocks: str = Query(..., description="同上（legacy 别名）")):
    return full_tick(stocks=stocks)


@router.get("/api/history")
def history(
    stock: str = Query(..., description="单只代码"),
    period: str = Query("1d"),
    count: int = Query(100),
    fields: str = Query("open,high,low,close,volume"),
):
    """legacy 单股 K 线：data 为记录列表（与 market_data_ex 同形状）。"""
    data = _upstream_guard(
        mp.get_market_data_ex, [stock.strip()], period, "", "", count, "none"
    )
    return {"data": data.get(stock.strip(), [])}


@router.get("/api/market/indices")
def major_indices():
    return {
        "indices": mp.MAJOR_INDICES,
        "data": _upstream_guard(mp.get_full_tick, mp.MAJOR_INDICES),
    }


@router.get("/api/market/market_data_ex")
def market_data_ex(
    stocks: str = Query(..., description="逗号分隔"),
    period: str = Query("1d"),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int = Query(-1),
    dividend_type: str = Query("none"),
):
    return {"data": _upstream_guard(
        mp.get_market_data_ex, _codes(stocks), period, start_time,
        end_time, count, dividend_type,
    )}


@router.get("/api/market/market_data")
def market_data(
    stocks: str = Query(..., description="逗号分隔"),
    fields: str = Query("open,high,low,close,volume"),
    period: str = Query("1d"),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int = Query(-1),
    dividend_type: str = Query("none"),
):
    return {"data": _upstream_guard(
        mp.get_market_data, _codes(stocks), fields, period, start_time,
        end_time, count, dividend_type,
    )}


@router.get("/api/calendar/trading_dates")
def trading_dates(
    market: str = Query("SH"),
    start_time: str = Query(""),
    end_time: str = Query(""),
    count: int = Query(-1),
):
    return {"market": market, "dates": _upstream_guard(
        mp.get_trading_dates, market, start_time, end_time, count,
    )}


@router.get("/api/calendar/is_trading_date")
def calendar_is_trading_date(market: str = Query("SH"),
                             date: str = Query(...)):
    return {"market": market, "date": date,
            "is_trading": _upstream_guard(mp.is_trading_date, market, date)}


@router.get("/api/calendar/prev_trading_date")
def calendar_prev_trading_date(market: str = Query("SH"), date: str = Query("")):
    return {"market": market, "date": date, "prev_trading_date":
            _upstream_guard(mp.prev_trading_date, market, date)}


@router.get("/api/calendar/next_trading_date")
def calendar_next_trading_date(market: str = Query("SH"), date: str = Query("")):
    return {"market": market, "date": date, "next_trading_date":
            _upstream_guard(mp.next_trading_date, market, date)}


@router.get("/api/calendar/trading_period")
def calendar_trading_period(market: str = Query("SH")):
    return ok_response(_upstream_guard(mp.trading_period, market))


@router.get("/api/utility/batch_stock_name")
def batch_stock_name(stocks: str = Query(..., description="逗号分隔")):
    codes = _codes(stocks)
    names = _upstream_guard(mp.get_stock_names, codes)
    return {"data": {c: names.get(c, "") for c in codes}}


@router.get("/api/trading/order_detail")
def order_detail(request: Request, order_id: str = Query(...),
                 account_id: str = "", account_type: str = ""):
    """单笔委托详情（信箱 query_stock_order）。"""
    from .trading import _account_params, _mb_request

    params = {"order_id": str(order_id)}
    params.update(_account_params(account_id, account_type))
    return ok_response(_mb_request(request, "query_stock_order", params))
