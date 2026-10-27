"""WS 事件端点：/ws/order（订单确认事件）与 /ws/events（全部事件）。

事件源是 MailboxClient 的事件队列；由一个 asyncio 任务按
poll_events(since_seq) 增量拉取并广播给订阅者。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("qmt_bridge.ws")

router = APIRouter()

ORDER_EVENTS = ("order_confirmed", "order_confirm_timeout", "async_response")
POLL_SEC = 0.1


class EventBroker:
    """把 MailboxClient 事件队列桥接到 asyncio 订阅者。"""

    def __init__(self):
        self._subscribers: dict[int, asyncio.Queue] = {}
        self._next_id = 0
        self._since_seq = 0
        self._task: asyncio.Task | None = None

    def start(self, client):
        self._client = client
        # 回放积压事件从 0 开始（poll_events 会带上全部缓冲事件）。
        self._task = asyncio.get_running_loop().create_task(self._pump())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._next_id += 1
        self._subscribers[self._next_id] = queue
        return self._next_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    async def _pump(self) -> None:
        while True:
            try:
                events = self._client.poll_events(self._since_seq)
            except Exception:
                events = []
            for frame in events:
                self._since_seq = max(self._since_seq, frame.get("seq", 0))
                for queue in list(self._subscribers.values()):
                    try:
                        queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        logger.warning("ws subscriber queue full, drop frame")
            await asyncio.sleep(POLL_SEC)


async def _ws_serve(ws: WebSocket, *, order_only: bool) -> None:
    await ws.accept()
    broker: EventBroker = ws.app.state.event_broker
    sub_id, queue = broker.subscribe()
    try:
        # 先发欢迎帧，再持续推送。
        await ws.send_text(json.dumps(
            {"type": "hello", "channel": "order" if order_only else "events"}
        ))
        while True:
            frame = await queue.get()
            if order_only and frame.get("event") not in ORDER_EVENTS:
                continue
            await ws.send_text(json.dumps(frame, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        broker.unsubscribe(sub_id)


@router.websocket("/ws/order")
async def ws_order(ws: WebSocket):
    """订单确认通道：推送 order_confirmed / order_confirm_timeout /
    async_response 事件帧：{"type":"event","event":...,"data":{...},"seq":N}。"""
    await _ws_serve(ws, order_only=True)


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """全量事件通道（含执行器推来的所有 event 帧）。"""
    await _ws_serve(ws, order_only=False)
