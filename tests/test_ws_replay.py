"""订单确认事件回放：锁定"先下单、后连 WS"不再丢确认。

旧实现里 EventBroker 无论有没有订阅者都推进 `_since_seq`，导致晚连上来的
客户端永远收不到 order_confirmed（只能看到 submitted/order_id=0）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ORDER = {"stock_code": "000001.SZ", "order_type": 23, "order_volume": 100}


def _drain(queue) -> list:
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


def test_confirmation_replayed_to_late_subscriber(bridge):
    # 下单时没有任何 WS 订阅者
    resp = bridge.post("/api/trading/order", json=ORDER)
    assert resp.status_code == 200

    broker = bridge.app.state.event_broker
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if any(f.get("event") == "order_confirmed" for f in broker._recent):
            break
        time.sleep(0.2)
    assert any(f.get("event") == "order_confirmed" for f in broker._recent), \
        "执行器确认事件没有被 broker 收集到"

    # 晚连上来的订单通道必须收到回放
    sub_id, queue = broker.subscribe(order_only=True)
    try:
        frames = _drain(queue)
    finally:
        broker.unsubscribe(sub_id)
    events = [f.get("event") for f in frames]
    assert "order_confirmed" in events, events


def test_replay_can_be_filtered_to_order_channel(bridge):
    """order_only 订阅不应拿到非订单类事件。"""
    broker = bridge.app.state.event_broker
    broker._recent.append({"type": "event", "event": "something_else", "seq": 10 ** 6})
    sub_id, queue = broker.subscribe(order_only=True)
    try:
        events = [f.get("event") for f in _drain(queue)]
    finally:
        broker.unsubscribe(sub_id)
    assert "something_else" not in events
