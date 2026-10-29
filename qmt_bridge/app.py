"""FastAPI 应用工厂。

启动时创建 MailboxClient（唯一信箱入口）、ExecutorStatus 探测器与
EventBroker（WS 事件泵）。不 import xtquant；本构建只支持 mailbox
executor —— 残留的旧 miniQMT 代码路径已全部移除。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings, get_settings
from .executor_status import ExecutorStatus
from .mailbox_client import MailboxClient
from .routers.unsupported import MSG as UNSUPPORTED_MSG
from .ws.events import EventBroker

logger = logging.getLogger("qmt_bridge")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    client = MailboxClient(
        settings.resolved_mailbox(),
        request_timeout_sec=settings.request_timeout_sec,
        poll_interval_sec=settings.poll_interval_sec,
        event_queue_max=settings.event_queue_max,
    )
    client.start()
    app.state.mailbox = client
    app.state.executor_status = ExecutorStatus(
        client.mailbox, hb_max_age_sec=settings.hb_max_age_sec
    )
    broker = EventBroker()
    broker.start(client)
    app.state.event_broker = broker
    logger.info("mailbox bridge started: mailbox=%s", client.mailbox)
    try:
        yield
    finally:
        await broker.stop()
        client.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="QMT Bridge Next",
        description=(
            "Big-QMT mailbox trading bridge (no xtquant, no miniQMT). "
            "Trading/account via mailbox executor; quotes/breadth/sector via "
            "public sources. Uncovered groups return 501: " + UNSUPPORTED_MSG
        ),
        version="3.1.0",
        lifespan=_lifespan,
    )
    app.state.settings = settings

    from .routers import (account, health, market_data, market_flow, trading,
                          unsupported)
    from .ws import events as ws_events

    app.include_router(health.router)
    app.include_router(market_data.router)   # 公开行情源：须先于 501 存根注册
    app.include_router(market_flow.router)   # 宽度/板块/北向/分笔：同样先于存根
    app.include_router(trading.router)
    app.include_router(account.router)
    app.include_router(unsupported.router)
    app.include_router(ws_events.router)
    return app
