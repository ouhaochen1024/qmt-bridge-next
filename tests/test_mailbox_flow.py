"""MailboxClient 与 mock 执行器的回路测试（无 FastAPI、无 QMT）。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))

from devtools import mock_executor as mock_exec  # noqa: E402,F401
from qmt_bridge.mailbox_client import (  # noqa: E402
    MailboxClient,
    MailboxError,
)
from tests.conftest import MockRunner  # noqa: E402,F401


def make_client(mailbox: Path) -> MailboxClient:
    return MailboxClient(mailbox, request_timeout_sec=8,
                         poll_interval_sec=0.05)


def test_request_query_asset(mailbox, runner):
    client = make_client(mailbox)
    client.start()
    try:
        deadline = time.time() + 5
        result = None
        while time.time() < deadline:
            try:
                result = client.request("query_stock_asset", {}, timeout=1.0)
                break
            except Exception:
                continue
        assert result is not None
        assert result["total_asset"] == pytest.approx(1_250_000.0)
        assert result["account_id"]
    finally:
        client.stop()


def test_order_two_phase_flow(mailbox, runner):
    client = make_client(mailbox)
    client.start()
    try:
        deadline = time.time() + 5
        result = None
        while time.time() < deadline:
            try:
                result = client.request("order_stock", {
                    "stock_code": "000001.SZ", "order_type": 23,
                    "order_volume": 100, "price_type": 5, "price": 0.0,
                }, timeout=1.0)
                break
            except Exception:
                continue
        assert result is not None
        assert result["status"] == "submitted"
        tag = result["tag"]
        assert tag

        # 等 order_confirmed 事件
        seq = 0
        confirmed = None
        deadline = time.time() + 6
        while time.time() < deadline and confirmed is None:
            for frame in client.poll_events(seq):
                seq = max(seq, frame.get("seq", 0))
                if frame.get("event") == "order_confirmed":
                    confirmed = frame["data"]
            time.sleep(0.05)
        assert confirmed is not None, "order_confirmed event not received"
        assert confirmed["tag"] == tag
        assert str(confirmed["order_id"]) not in ("", "0", "None")
    finally:
        client.stop()


def test_order_confirm_timeout_mode(mailbox, tmp_path):
    r = MockRunner(mailbox, mode="timeout").start()
    try:
        client = make_client(mailbox)
        client.start()
        result = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                result = client.request("order_stock", {
                    "stock_code": "000001.SZ", "order_type": 24,
                    "order_volume": 100,
                }, timeout=1.0)
                break
            except Exception:
                continue
        assert result is not None
        seq, seen_timeout = 0, False
        deadline = time.time() + 6
        while time.time() < deadline and not seen_timeout:
            for frame in client.poll_events(seq):
                seq = max(seq, frame.get("seq", 0))
                if frame.get("event") == "order_confirm_timeout":
                    seen_timeout = True
            time.sleep(0.05)
        assert seen_timeout
        client.stop()
    finally:
        r.stop()


def test_idempotent_redelivery_same_id(mailbox, runner):
    """桥接端超时后必须用同一个 id 重投递；执行器从缓存重放。"""
    client = make_client(mailbox)
    client.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                client.request("query_stock_positions", {}, timeout=1.0)
                break
            except Exception:
                continue
        r1 = client.request("query_stock_positions", {}, timeout=2.0)
        assert isinstance(r1, list)

        # 直接向信箱重投递同一 id 帧，验证执行器幂等（重放而非再执行）
        import json as _json
        frame = {
            "id": "replay-01", "method": "order_stock",
            "params": {"stock_code": "600519.SH", "order_type": 23,
                       "order_volume": 100},
            "nonce": "rr01", "ts": int(time.time() * 1000),
        }
        inbox = mailbox / "in"
        inbox.mkdir(parents=True, exist_ok=True)
        before = len(mock_exec.ORDERS)
        for _ in range(2):
            tmp = inbox / "replay.json.tmp"
            tmp.write_bytes((_json.dumps(frame) + "\n").encode("utf-8"))
            (inbox / ("replay-%d.json" % time.time_ns())).write_bytes(
                (_json.dumps(frame) + "\n").encode("utf-8"))
            time.sleep(0.4)
        time.sleep(0.6)
        # 只应产生一笔委托（同一 id+nonce 只执行一次）
        assert len(mock_exec.ORDERS) - before == 1, (
            "duplicate execution detected: %d new orders"
            % (len(mock_exec.ORDERS) - before))
    finally:
        client.stop()


def test_unsupported_method_maps_to_error(mailbox, runner):
    client = make_client(mailbox)
    client.start()
    try:
        deadline = time.time() + 5
        err = None
        while time.time() < deadline:
            try:
                client.request("get_market_data_ex", {}, timeout=1.0)
                err = None
                break
            except MailboxError as exc:
                err = exc
                break
            except Exception:
                continue
        assert err is not None and "unsupported" in str(err)
    finally:
        client.stop()
