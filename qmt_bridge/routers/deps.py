"""FastAPI 依赖与响应助手。"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from ..config import get_settings


def require_api_key(
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> str:
    """账户/交易门禁：配置了 api_key 时必须携带匹配的 X-API-Key。

    `api_key` 为空（未配置）时放行，并在启动日志中告警；
    配置后所有 /api/trading/*、/account/* 请求都必须带 key。
    用 compare_digest 做常量时间比较，避免计时侧信道。
    """
    expected = (get_settings().api_key or "").strip()
    if expected and not secrets.compare_digest(str(x_api_key or ""), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )
    return x_api_key


def require_write_enabled() -> None:
    """ORDER_WRITES 关闭时拒绝下单/撤单（只挂在写路由上，不影响只读查询）。"""
    if not get_settings().order_writes_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="order writes disabled (QMT_BRIDGE_ORDER_WRITES_DISABLED=1)",
        )


def ok_response(data, **extra) -> dict:
    return {"code": 0, "message": "ok", "data": data, **extra}
