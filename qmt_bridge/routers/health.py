"""健康检查与执行器状态路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..executor_status import ExecutorStatus
from .deps import ok_response

router = APIRouter(tags=["health"])


def _health_payload(request: Request) -> dict:
    status_probe: ExecutorStatus = request.app.state.executor_status
    snap = status_probe.snapshot()
    client = request.app.state.mailbox
    return {
        "status": "healthy" if snap["alive"] else "degraded",
        "executor": snap,
        "events_buffered": len(client._events) if client else 0,
        "service": "qmt-bridge-next",
    }


@router.get("/health")
def health(request: Request):
    snap = _health_payload(request)
    if not snap["executor"]["alive"]:
        # 503 + degraded：Skill 层可据此明确报告 "executor not alive"
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=snap)
    return ok_response(snap)


@router.get("/executor/status")
def executor_status(request: Request):
    return ok_response(_health_payload(request))


@router.get("/api/meta/health")
def meta_health(request: Request):
    """旧客户端 Skill 的 health_check() 走此路径。"""
    snap = _health_payload(request)
    return ok_response({
        "status": snap["status"],
        "service": "qmt-bridge-next",
        "executor": snap["executor"],
    })
