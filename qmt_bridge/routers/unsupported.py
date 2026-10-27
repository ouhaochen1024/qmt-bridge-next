"""501 unsupported 存根 —— 覆盖旧 qmt-bridge 中依赖 xtquant.xtdata /
miniQMT 专有 API 的路由组。

原则：不假装成功。信箱执行器（qmt_mailbox_executor.py 的 _HANDLERS）
只覆盖交易与账户查询；行情/板块/财务/公式等路由在这里显式返回 501，
并在 docs/limitations.md 中登记。执行器日后新增方法时，把对应路由
迁到真实后端，再从此处摘除。
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from .deps import ok_response

MSG = (
    "unsupported: big-QMT mailbox executor does not cover this "
    "xtdata/miniQMT API yet; see qmt-bridge-next/docs/limitations.md"
)


def _unsupported_response():
    return JSONResponse(
        status_code=501,
        content={"code": 501, "message": MSG, "data": None},
    )


# 原 qmt-bridge 的 xtdata 路由组（前缀 → 说明）
UNSUPPORTED_GROUPS: dict[str, str] = {
    "/api/market": "K线/快照/涨跌榜等行情数据 (xtdata)",
    "/api/tick": "分笔 tick (xtdata)",
    "/api/sector": "板块列表与成分 (xtdata)",
    "/api/calendar": "交易日历 (xtdata)",
    "/api/financial": "财务数据 (xtdata)",
    "/api/instrument": "合约信息 (xtdata)",
    "/api/option": "期权链 (xtdata)",
    "/api/etf": "ETF 行情与筛选 (xtdata)",
    "/api/cb": "可转债行情 (xtdata)",
    "/api/futures": "期货行情 (xtdata)",
    "/api/meta": "服务元信息 (xtdata)",
    "/api/download": "数据下载 (xtdata.downloader)",
    "/api/formula": "技术指标公式 (xtdata)",
    "/api/hk": "港股通 (xtdata)",
    "/api/tabular": "板块/表格数据 (xtdata)",
    "/api/utility": "工具类 (xtdata)",
    "/api/bank": "银行资金 (xttrader 专有)",
    "/api/credit": "信用两融 (xttrader 专有；执行器仅支持 STOCK 账户基础交易)",
    "/api/fund": "基金申赎 (xttrader 专有)",
    "/api/smt": "融资融券标的 (xtdata)",
}

# 旧版 legacy 短路径
UNSUPPORTED_LEGACY_PATHS = (
    "/api/history",
    "/api/batch_history",
    "/api/full_tick",
    "/api/sector_stocks",
    "/api/instrument_detail",
)

# 旧版 WS 端点（/ws/order 与 /ws/events 由本构建真实提供）
UNSUPPORTED_WS_PATHS = (
    "/ws/realtime",
    "/ws/whole_quote",
    "/ws/download_progress",
    "/ws/formula",
    "/ws/l2_thousand",
    "/ws/trade",
)

router = APIRouter(tags=["unsupported"])

_METHODS = ["GET", "POST", "DELETE", "PUT"]


def _register() -> None:
    for prefix, _desc in UNSUPPORTED_GROUPS.items():
        sub = APIRouter(prefix=prefix)

        @sub.api_route("/{rest:path}", methods=_METHODS, include_in_schema=False)
        async def _catch_all(rest: str):
            return _unsupported_response()

        router.include_router(sub)

    for path in UNSUPPORTED_LEGACY_PATHS:
        def _legacy(rest: str = ""):
            return _unsupported_response()

        router.add_api_route(
            path, _legacy, methods=_METHODS, include_in_schema=False
        )

    for path in UNSUPPORTED_WS_PATHS:
        async def _ws_unsupported(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_json({"type": "error", "message": MSG})
            await websocket.close(code=4501)

        router.add_websocket_route(path, _ws_unsupported)


_register()


@router.get("/capabilities")
async def capabilities():
    """本构建真实支持的能力清单（Skill 层据此降级）。"""
    return ok_response({
        "supported": {
            "trading": ["/api/trading/order", "/api/trading/order_async",
                        "/api/trading/cancel", "/api/trading/cancel_async",
                        "/api/trading/cancel_by_sysid",
                        "/api/trading/batch_order", "/api/trading/batch_cancel"],
            "account": ["/api/trading/orders", "/api/trading/positions",
                        "/api/trading/asset", "/api/trades",
                        "/api/trading/order/{order_id}",
                        "/account/asset", "/account/positions",
                        "/account/orders", "/account/trades"],
            "ws": ["/ws/order", "/ws/events"],
        },
        "unsupported_groups": sorted(UNSUPPORTED_GROUPS),
    })
