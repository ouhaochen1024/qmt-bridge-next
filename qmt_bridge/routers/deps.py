"""FastAPI 依赖与响应助手。"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from ..config import get_settings


def require_api_key(
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> str:
    """写操作门禁：配置了 api_key 时必须携带匹配的 X-API-Key。"""
    expected = (get_settings().api_key or "").strip()
    if expected and x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )
    return x_api_key


def require_write_enabled() -> None:
    """ORDER_WRITES 关闭时拒绝一切下单/撤单。"""
    if not get_settings().order_writes_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="order writes disabled (QMT_BRIDGE_ORDER_WRITES_ENABLED)",
        )


def ok_response(data, **extra) -> dict:
    return {"code": 0, "message": "ok", "data": data, **extra}
