"""交易与账户查询路由（大 QMT 信箱执行器后端）。

语义对齐 mailbox 执行器：
- order_stock 两阶段：先返回 {"status":"submitted","tag":...,"order_id":0}，
  确认结果经 WS /ws/order 以 order_confirmed / order_confirm_timeout 推送。
- account_id 为空时透传（执行器用策略上下文绑定的账户），
  绝不填充硬编码默认值。
- 查询返回空列表是合法结果（空持仓/当日无成交），不当作错误；
  仅执行器 ok:false 且错误含 "account not bound" 时报账户错误。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from ..mailbox_client import MailboxError, MailboxTimeoutError
from .deps import ok_response, require_api_key, require_write_enabled

logger = logging.getLogger("qmt_bridge.trading")

# 允许的委托方向（QMT 常量：23 买入 / 24 卖出）
_ORDER_TYPES = (23, 24)
_STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

router = APIRouter(
    prefix="/api/trading",
    tags=["trading"],
    dependencies=[Depends(require_api_key)],
)


# ----------------------------------------------------------------------
# 信箱调用与异常翻译
# ----------------------------------------------------------------------

def _mb_request(request: Request, method: str, params: dict,
                timeout: float | None = None):
    """执行器必须存活；未存活时 503（错误串含 executor not alive）。"""
    app = request.app
    try:
        app.state.executor_status.require_alive()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return app.state.mailbox.request(method, params, timeout=timeout)
    except MailboxTimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="mailbox timeout: %s (request id kept idempotent; do NOT "
                   "re-submit blindly)" % exc,
        ) from exc
    except MailboxError as exc:
        msg = str(exc)
        if "account not bound" in msg:
            raise HTTPException(
                status_code=409,
                detail="executor: %s" % msg,
            ) from exc
        raise HTTPException(status_code=502, detail="executor: %s" % msg) from exc


def _account_params(account_id: str, account_type: str) -> dict:
    """透传账户参数；空值原样为空，不填充默认账户。"""
    out: dict = {}
    if account_id:
        out["account_id"] = account_id
    if account_type:
        out["account_type"] = account_type
    return out


# ----------------------------------------------------------------------
# 下单 / 撤单
# ----------------------------------------------------------------------

@router.post("/order", dependencies=[Depends(require_write_enabled)])
def place_order(body: dict, request: Request):
    """单笔委托（两阶段）。

    返回 data.status:
      - "confirmed": order_id 已知，直接可用；
      - "submitted": order_id=0 为合法状态，凭 tag 追踪，确认事件经
        WS /ws/order 推送（order_confirmed / order_confirm_timeout）。
        收到 order_confirm_timeout 时报告"已提交但未确认，请人工检查"，
        不要重新下单。
    """
    try:
        stock_code = str(body["stock_code"]).strip().upper()
        order_type = int(body["order_type"])
        order_volume = int(body["order_volume"])
        price_type = int(body.get("price_type", 5))
        price = float(body.get("price", 0.0))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="stock_code / order_type(23 buy|24 sell) / order_volume "
                   "required; price_type / price must be numeric",
        ) from exc
    if not _STOCK_CODE_RE.match(stock_code):
        raise HTTPException(
            status_code=422,
            detail="stock_code must look like 000001.SZ / 600519.SH",
        )
    if order_type not in _ORDER_TYPES:
        raise HTTPException(
            status_code=422, detail="order_type must be 23 (buy) or 24 (sell)"
        )
    if order_volume <= 0:
        raise HTTPException(status_code=422, detail="order_volume must be > 0")
    if price < 0:
        raise HTTPException(status_code=422, detail="price must be >= 0")

    params = {
        "stock_code": stock_code,
        "order_type": order_type,
        "order_volume": order_volume,
        "price_type": price_type,
        "price": price,
        "order_remark": str(body.get("order_remark", "")),
    }
    params.update(_account_params(
        str(body.get("account_id") or ""), str(body.get("account_type") or "")
    ))
    result = _mb_request(request, "order_stock", params)
    # submitted/confirmed 均为 200；由 data.status 区分，body 永远带 tag。
    return ok_response(result)


@router.post("/order_async", dependencies=[Depends(require_write_enabled)])
def place_order_async(body: dict, request: Request):
    """与 /order 相同（信箱协议下无独立 async 通道；确认经 WS 推送）。"""
    return place_order(body, request)


@router.post("/cancel", dependencies=[Depends(require_write_enabled)])
def cancel_order(body: dict, request: Request):
    """撤销委托。order_id 可为 order_ref / order_sysid / order_id 任一。"""
    order_id = body.get("order_id")
    if not order_id:
        raise HTTPException(status_code=422, detail="order_id required")
    params = {"order_id": str(order_id)}
    params.update(_account_params(
        str(body.get("account_id") or ""), str(body.get("account_type") or "")
    ))
    return ok_response(_mb_request(request, "cancel_order_stock", params))


@router.post("/cancel_async", dependencies=[Depends(require_write_enabled)])
def cancel_order_async(body: dict, request: Request):
    return cancel_order(body, request)


@router.post("/cancel_by_sysid", dependencies=[Depends(require_write_enabled)])
def cancel_by_sysid(body: dict, request: Request):
    sysid = body.get("sysid")
    if not sysid:
        raise HTTPException(status_code=422, detail="sysid required")
    params = {"sysid": str(sysid)}
    params.update(_account_params(
        str(body.get("account_id") or ""), str(body.get("account_type") or "")
    ))
    return ok_response(_mb_request(request, "cancel_order_stock_sysid", params))


@router.post("/cancel_by_sysid_async", dependencies=[Depends(require_write_enabled)])
def cancel_by_sysid_async(body: dict, request: Request):
    return cancel_by_sysid(body, request)


@router.post("/batch_order", dependencies=[Depends(require_write_enabled)])
def batch_order(body: dict, request: Request):
    """批量委托：{"orders": [ {…order 字段…}, … ]}，逐笔经信箱执行。"""
    orders = body.get("orders") or []
    if not isinstance(orders, list) or not orders:
        raise HTTPException(status_code=422, detail="orders[] required")
    results = []
    for item in orders:
        try:
            results.append(ok_response(place_order(item, request))["data"])
        except HTTPException as exc:
            results.append({"status": "error", "detail": exc.detail})
    return ok_response(results)


@router.post("/batch_cancel", dependencies=[Depends(require_write_enabled)])
def batch_cancel(body: dict, request: Request):
    cancels = body.get("order_ids") or []
    if not isinstance(cancels, list) or not cancels:
        raise HTTPException(status_code=422, detail="order_ids[] required")
    results = []
    for oid in cancels:
        try:
            results.append(ok_response(
                cancel_order({"order_id": oid}, request)
            )["data"])
        except HTTPException as exc:
            results.append({"order_id": oid, "status": "error",
                            "detail": exc.detail})
    return ok_response(results)


# ----------------------------------------------------------------------
# 查询（空结果是合法数据，不是错误）
# ----------------------------------------------------------------------

@router.get("/orders")
def list_orders(request: Request, account_id: str = "",
                account_type: str = "", cancelable_only: bool = False):
    params = _account_params(account_id, account_type)
    if cancelable_only:
        params["cancelable_only"] = True
    return ok_response(
        _mb_request(request, "query_stock_orders", params) or []
    )


@router.get("/positions")
def list_positions(request: Request, account_id: str = "",
                   account_type: str = ""):
    return ok_response(_mb_request(
        request, "query_stock_positions", _account_params(account_id, account_type)
    ) or [])


@router.get("/asset")
def get_asset(request: Request, account_id: str = "", account_type: str = ""):
    """账户资产。无 ACCOUNT 行时 data 为 null（合法，如未产生查询数据）。"""
    return ok_response(_mb_request(
        request, "query_stock_asset", _account_params(account_id, account_type)
    ))


@router.get("/trades")
def list_trades(request: Request, account_id: str = "",
                account_type: str = ""):
    return ok_response(_mb_request(
        request, "query_stock_trades", _account_params(account_id, account_type)
    ) or [])


@router.get("/order/{order_id}")
def get_order(order_id: str, request: Request, account_id: str = "",
              account_type: str = ""):
    params = {"order_id": order_id}
    params.update(_account_params(account_id, account_type))
    return ok_response(_mb_request(request, "query_stock_order", params))


@router.get("/position/{stock_code}")
def get_position(stock_code: str, request: Request, account_id: str = "",
                 account_type: str = ""):
    rows = _mb_request(
        request, "query_stock_positions", _account_params(account_id, account_type)
    ) or []
    want = stock_code.upper()
    matched = [r for r in rows
               if str(r.get("stock_code") or "").upper() == want]
    return ok_response(matched[0] if matched else None)


@router.get("/account_status")
def account_status(request: Request):
    snap = request.app.state.executor_status.snapshot()
    return ok_response({
        "alive": snap["alive"],
        "account_id": snap["account_id"],
        "account_type": snap["account_type"],
        "executor_build": snap["executor_build"],
        "hb_age_sec": snap["hb_age_sec"],
        "hb_pending": snap["hb_pending"],
    })


# ----------------------------------------------------------------------
# 旧版 miniQMT 专有能力：显式不支持，不假装成功
# ----------------------------------------------------------------------

@router.get("/history_trades")
@router.get("/history_orders")
@router.get("/new_purchase_limit")
@router.get("/ipo_data")
@router.get("/com_fund")
@router.get("/com_position")
@router.post("/export_data")
@router.post("/query_data")
@router.post("/sync_transaction")
def unsupported_trading():
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=501,
        content={"code": 501, "message": (
            "unsupported: big-QMT mailbox executor does not cover this "
            "xttrader API yet; see docs/limitations.md"), "data": None},
    )
