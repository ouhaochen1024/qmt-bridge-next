"""/account/* 兼容别名路由（路径形状与旧桥接/Skill 预期一致）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .deps import require_api_key, require_write_enabled
from .trading import (
    _account_params,
    _mb_request,
    ok_response,
)

router = APIRouter(prefix="/account", tags=["account"],
                   dependencies=[Depends(require_api_key)])


@router.get("/asset")
def account_asset(request: Request, account_id: str = "",
                  account_type: str = ""):
    return ok_response(_mb_request(
        request, "query_stock_asset", _account_params(account_id, account_type)
    ))


@router.get("/positions")
def account_positions(request: Request, account_id: str = "",
                      account_type: str = ""):
    return ok_response(_mb_request(
        request, "query_stock_positions",
        _account_params(account_id, account_type),
    ) or [])


@router.get("/orders")
def account_orders(request: Request, account_id: str = "",
                   account_type: str = "", cancelable_only: bool = False):
    params = _account_params(account_id, account_type)
    if cancelable_only:
        params["cancelable_only"] = True
    return ok_response(_mb_request(
        request, "query_stock_orders", params) or [])


@router.get("/trades")
def account_trades(request: Request, account_id: str = "",
                   account_type: str = ""):
    return ok_response(_mb_request(
        request, "query_stock_trades", _account_params(account_id, account_type)
    ) or [])


@router.post("/order", dependencies=[Depends(require_write_enabled)])
def account_order(body: dict, request: Request):
    from .trading import place_order
    return place_order(body, request)


@router.post("/cancel", dependencies=[Depends(require_write_enabled)])
def account_cancel(body: dict, request: Request):
    from .trading import cancel_order
    return cancel_order(body, request)


@router.get("/status")
def account_status(request: Request):
    from .trading import account_status as _status
    return _status(request)


@router.get("/binding")
def account_binding(request: Request):
    """账户绑定证明：来自执行器 hello.json（非终端日志）。"""
    snap = request.app.state.executor_status.snapshot()
    if not snap["alive"]:
        raise HTTPException(status_code=503, detail=snap["reason"])
    return ok_response({
        "bound": bool(snap["account_id"]),
        "account_id": snap["account_id"],
        "account_type": snap["account_type"],
        "executor_build": snap["executor_build"],
    })
