"""桥接 HTTP/WS 集成测试（TestClient + mock 执行器，无 QMT）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from tests.conftest import MockRunner  # noqa: E402


@pytest.fixture()
def bridge(mailbox, runner):
    """已连接 mock 执行器的桥接应用。"""
    from qmt_bridge.app import create_app
    from qmt_bridge.config import reset_settings, Settings

    reset_settings(Settings(mailbox=str(mailbox)))
    app = create_app()
    with TestClient(app) as tc:
        yield tc
    reset_settings(None)


@pytest.fixture()
def bridge_no_executor(mailbox):
    """执行器未启动（无 hello/agent.hb）。"""
    from qmt_bridge.app import create_app
    from qmt_bridge.config import reset_settings, Settings

    reset_settings(Settings(mailbox=str(mailbox)))
    app = create_app()
    with TestClient(app) as tc:
        yield tc
    reset_settings(None)


def test_health_degraded_without_executor(bridge_no_executor):
    resp = bridge_no_executor.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "executor not alive" in body["executor"]["reason"]


def test_order_rejected_when_executor_down(bridge_no_executor):
    resp = bridge_no_executor.post("/api/trading/order", json={
        "stock_code": "000001.SZ", "order_type": 23, "order_volume": 100,
    })
    assert resp.status_code == 503
    assert "executor not alive" in resp.json()["detail"]


def test_health_healthy_with_mock(bridge):
    resp = bridge.get("/health")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "healthy"
    assert body["executor"]["account_id"]


def test_account_asset_via_alias(bridge):
    resp = bridge.get("/account/asset")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["cash"] == pytest.approx(1_000_000.0)
    assert data["total_asset"] == pytest.approx(1_250_000.0)


def test_positions_empty_is_ok_not_error(bridge):
    resp = bridge.get("/api/trading/positions")
    assert resp.status_code == 200
    assert isinstance(resp.json()["data"], list)


def test_order_two_phase_and_ws_confirmation(bridge):
    # 1) 下单 → submitted + tag，order_id=0 合法
    resp = bridge.post("/api/trading/order", json={
        "stock_code": "000001.SZ", "order_type": 23,
        "order_volume": 100, "price_type": 5, "price": 0.0,
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "submitted"
    tag = data["tag"]
    assert tag
    assert data["order_id"] == 0

    # 2) WS /ws/order 收到 order_confirmed
    with bridge.websocket_connect("/ws/order") as ws:
        hello = loads_safe(ws.receive_text())
        assert hello["type"] == "hello"
        deadline = time.time() + 8
        confirmed = None
        while time.time() < deadline and confirmed is None:
            frame = loads_safe(ws.receive_text())
            if frame.get("event") == "order_confirmed":
                confirmed = frame["data"]
        assert confirmed is not None, "no order_confirmed on /ws/order"
        assert confirmed["tag"] == tag
        assert str(confirmed["order_id"]) not in ("", "0", "None")


def test_cancel_workflow(bridge):
    resp = bridge.post("/api/trading/order", json={
        "stock_code": "600519.SH", "order_type": 23, "order_volume": 100,
    })
    tag = resp.json()["data"]["tag"]
    # 等 mock 确认产生 order_id
    deadline = time.time() + 6
    order_id = None
    while time.time() < deadline and not order_id:
        rows = bridge.get("/api/trading/orders",
                          params={"account_id": ""}).json()["data"]
        for row in rows:
            if row.get("order_remark") == tag and row.get("order_id") not in (0, "", None):
                order_id = str(row["order_id"])
        time.sleep(0.2)
    assert order_id, "order never confirmed in mock"

    resp = bridge.post("/api/trading/cancel", json={"order_id": order_id})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    rows = bridge.get("/api/trading/orders").json()["data"]
    status = next(r["order_status"] for r in rows
                  if str(r.get("order_id")) == order_id)
    assert status == 56  # 已撤


def test_unsupported_market_routes_501(bridge):
    for path in ("/api/market/kline", "/api/tick/full_tick",
                 "/api/financial/data", "/api/market/local_data",
                 "/api/sector/list", "/api/formula/macd"):
        resp = bridge.get(path)
        assert resp.status_code == 501, path
        assert "unsupported" in resp.json()["message"]


def test_capabilities_endpoint(bridge):
    resp = bridge.get("/capabilities")
    assert resp.status_code == 200
    caps = resp.json()["data"]
    assert "/ws/order" in caps["supported"]["ws"]
    assert "/api/market" in caps["unsupported_groups"]


def loads_safe(text):
    return json.loads(text)