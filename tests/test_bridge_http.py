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
    # 曾经错列过 /api/trades（该路径实测 404），必须是真实存在的路径
    assert "/api/trades" not in caps["supported"]["account"]
    assert "/api/trading/trades" in caps["supported"]["account"]
    # /api/market 是"部分支持"，不能再整组标成 unsupported
    assert "/api/market" not in caps["unsupported_groups"]
    assert "/api/market" in caps["partial_groups"]
    assert "/api/market/full_tick" in caps["partial_groups"]["/api/market"]["supported"]


def test_capabilities_paths_are_registered(bridge):
    """capabilities 里声称支持的路径必须真实注册（防止契约漂移）。"""
    caps = bridge.get("/capabilities").json()["data"]
    registered = {
        getattr(r, "path", None) for r in bridge.app.routes
    } - {None}
    for spec in bridge.app.openapi()["paths"]:
        registered.add(spec)
    for group, paths in caps["supported"].items():
        if group == "ws":
            continue
        for path in paths:
            assert path in registered, "%s claims %s but route missing" % (group, path)


def test_api_key_required_for_account_and_trading(mailbox, runner):
    """配置 api_key 后，账户/交易接口无 key 或错 key 必须 401。"""
    from qmt_bridge.app import create_app
    from qmt_bridge.config import reset_settings, Settings

    reset_settings(Settings(mailbox=str(mailbox), api_key="SECRET-KEY-123"))
    app = create_app()
    with TestClient(app) as tc:
        assert tc.get("/api/trading/asset").status_code == 401
        assert tc.get("/account/positions").status_code == 401
        assert tc.get("/api/trading/asset",
                      headers={"X-API-Key": "wrong"}).status_code == 401
        ok = tc.get("/api/trading/asset",
                    headers={"X-API-Key": "SECRET-KEY-123"})
        assert ok.status_code == 200
        # 健康检查保持公开（不泄露账号）
        assert tc.get("/api/meta/health").status_code == 200
    reset_settings(None)


def test_order_param_errors_return_422(bridge):
    """可预期的参数错误必须是 422，不能变成 500。"""
    base = {"stock_code": "000001.SZ", "order_type": 23, "order_volume": 100}
    cases = [
        dict(base, price_type="abc"),     # 曾经 500
        dict(base, price="abc"),          # 曾经 500
        dict(base, order_type=99),
        dict(base, order_type="buy"),
        dict(base, stock_code=None),
        dict(base, stock_code="600519"),
        dict(base, order_volume=0),
    ]
    for body in cases:
        resp = bridge.post("/api/trading/order", json=body)
        assert resp.status_code == 422, (body, resp.status_code, resp.text)


def test_read_endpoints_survive_writes_disabled(mailbox, runner):
    """关闭写门禁时，只读接口不应被误伤为 403。"""
    from qmt_bridge.app import create_app
    from qmt_bridge.config import reset_settings, Settings

    reset_settings(Settings(mailbox=str(mailbox), order_writes_enabled=False))
    app = create_app()
    with TestClient(app) as tc:
        assert tc.get("/api/trading/asset").status_code == 200
        assert tc.get("/account/positions").status_code == 200
        assert tc.post("/api/trading/order", json={
            "stock_code": "000001.SZ", "order_type": 23, "order_volume": 100,
        }).status_code == 403
    reset_settings(None)


def loads_safe(text):
    return json.loads(text)