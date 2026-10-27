#!/usr/bin/env python3
"""mock_executor.py — 大 QMT 信箱执行器模拟器（外部 Python，无需 QMT）。

在没有 QMT 客户端的环境里模拟 mailbox/qmt_mailbox_executor.py 的协议行为：
- 写 hello.json / agent.hb（让桥接端 /health 转 healthy）
- 处理 in/ 中的请求帧，按 _PROCESSED 语义对重复 id 幂等重放
- order_stock 两阶段：先回 submitted，再按 --mode / --confirm-delay
  追发 order_confirmed 或 order_confirm_timeout 事件帧
- 内存账本支持 查资产/持仓/委托/成交 → 下单 → 确认 → 撤单 的完整闭环

用法：
    python devtools/mock_executor.py --mailbox ./mailbox
    python devtools/mock_executor.py --mailbox ./mailbox --mode timeout
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BUILD = "mock-1.0"
ACCOUNT_ID = "8886001888"

# 内存账本
ASSET = {
    "account_id": ACCOUNT_ID,
    "cash": 1_000_000.0,
    "frozen_cash": 0.0,
    "market_value": 250_000.0,
    "total_asset": 1_250_000.0,
}
POSITIONS = [
    {
        "account_id": ACCOUNT_ID,
        "stock_code": "600519.SH",
        "volume": 100,
        "can_use_volume": 100,
        "frozen_volume": 0,
        "open_price": 1500.0,
        "market_value": 250_000.0,
    }
]
ORDERS: list[dict] = []
TRADES: list[dict] = []

_PROCESSED: dict[str, dict] = {}   # "id|nonce" -> resp frame
_PENDING_CONFIRM: dict[str, dict] = {}  # tag -> {deadline, order}
_order_seq = 9000
_frame_seq = 0


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def send(outbox: Path, payload: dict) -> None:
    global _frame_seq
    _frame_seq += 1
    name = "%016d-%06d.json" % (int(time.time() * 1000), _frame_seq % 1000000)
    write_atomic(outbox / name,
                 (json.dumps(payload) + "\n").encode("utf-8"))


def make_tag(remark: str) -> str:
    global _order_seq
    _order_seq += 1
    base = (remark or "").strip()
    suffix = "m%04x" % (_order_seq & 0xFFFF)
    if not base:
        return "q%08x%s" % (int(time.time() * 1000) & 0xFFFFFFFF, suffix)
    return (base[:10] + suffix)[:16]


def make_order(params: dict, tag: str) -> dict:
    return {
        "account_id": params.get("account_id") or ACCOUNT_ID,
        "stock_code": params["stock_code"],
        "order_id": 0,           # 提交时未知；确认后填充
        "order_ref": "",
        "order_sysid": "",
        "order_type": int(params["order_type"]),
        "order_volume": int(params["order_volume"]),
        "price_type": int(params.get("price_type", 5)),
        "price": float(params.get("price", 0.0)),
        "traded_volume": 0,
        "traded_price": 0.0,
        "order_status": 50,      # 待报
        "order_remark": tag,
    }


def handle_order(params: dict) -> dict:
    tag = make_tag(params.get("order_remark", ""))
    order = make_order(params, tag)
    ORDERS.append(order)
    _PENDING_CONFIRM[tag] = {
        "deadline": time.monotonic() + 1.0,
        "order": order,
    }
    return {"status": "submitted", "tag": tag, "order_id": 0}


def confirm_pending(outbox: Path, mode: str) -> None:
    now = time.monotonic()
    for tag, item in list(_PENDING_CONFIRM.items()):
        if now < item["deadline"]:
            continue
        order = item["order"]
        if mode == "timeout":
            _PENDING_CONFIRM.pop(tag, None)
            send(outbox, {"type": "event", "event": "order_confirm_timeout",
                          "data": {"tag": tag}})
            continue
        global _order_seq
        _order_seq += 1
        order["order_id"] = str(100000 + _order_seq)
        order["order_sysid"] = "SYS%s" % order["order_id"]
        order["order_status"] = 48  # 已报
        _PENDING_CONFIRM.pop(tag, None)
        send(outbox, {
            "type": "event", "event": "order_confirmed",
            "data": {"tag": tag, "order_id": order["order_id"],
                     "order_sysid": order["order_sysid"],
                     "order_status": order["order_status"]},
        })


def handle_cancel(params: dict) -> dict:
    want = str(params["order_id"])
    for order in ORDERS:
        ids = (str(order.get("order_id") or ""),
               str(order.get("order_ref") or ""),
               str(order.get("order_sysid") or ""),
               str(order.get("order_remark") or ""))
        if want in ids and order["order_status"] == 48:
            order["order_status"] = 56  # 已撤
            return 0
    raise RuntimeError("cancel failed: no cancelable order %s" % want)


def handle_method(method: str, params: dict, outbox: Path, mode: str):
    if method == "query_stock_asset":
        return dict(ASSET)
    if method == "query_stock_positions":
        return [dict(p) for p in POSITIONS]
    if method == "query_stock_orders":
        rows = [dict(o) for o in ORDERS]
        if params.get("cancelable_only"):
            rows = [r for r in rows if r["order_status"] not in (53, 54, 56, 57)]
        return rows
    if method == "query_stock_order":
        want = str(params.get("order_id") or "")
        for order in ORDERS:
            if want in (str(order.get("order_id") or ""),
                        str(order.get("order_ref") or ""),
                        str(order.get("order_sysid") or "")):
                return dict(order)
        return None
    if method == "query_stock_trades":
        return [dict(t) for t in TRADES]
    if method in ("order_stock", "order_stock_async"):
        return handle_order(params)
    if method in ("cancel_order_stock", "cancel_order_stock_async"):
        return handle_cancel(params)
    if method in ("cancel_order_stock_sysid", "cancel_order_stock_sysid_async"):
        return handle_method("cancel_order_stock",
                             {"order_id": params.get("sysid")}, outbox, mode)
    raise RuntimeError("unsupported method: %s" % method)


def pump(mailbox: Path, mode: str) -> None:
    inbox, outbox = mailbox / "in", mailbox / "out"
    mailbox.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(exist_ok=True)
    outbox.mkdir(exist_ok=True)

    write_atomic(mailbox / "hello.json", (
        json.dumps({
            "type": "hello", "version": "1.3",
            "account_id": ACCOUNT_ID, "account_type": "STOCK",
            "strategy": "mock", "build": BUILD,
            "started_at": int(time.time() * 1000),
        }) + "\n").encode("utf-8"))
    write_atomic(mailbox / "agent.hb", (
        json.dumps({"ts": int(time.time() * 1000), "build": BUILD,
                    "seq": int(time.time()), "pending": len(_PENDING_CONFIRM)})
        + "\n").encode("utf-8"))

    try:
        names = sorted(n for n in os.listdir(inbox) if n.endswith(".json"))
    except OSError:
        return
    for name in names:
        path = inbox / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data.strip():
            continue
        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        key = "%s|%s" % (msg.get("id"), msg.get("nonce") or "")
        cached = _PROCESSED.get(key)
        if cached is not None:
            send(outbox, cached)
        else:
            req_id = msg.get("id")
            try:
                result = handle_method(str(msg.get("method")),
                                       msg.get("params") or {}, outbox, mode)
                resp = {"type": "resp", "id": req_id, "ok": True,
                        "result": result}
            except Exception as exc:
                resp = {"type": "resp", "id": req_id, "ok": False,
                        "error": "%s: %s" % (type(exc).__name__, exc)}
            if req_id is not None:
                _PROCESSED[key] = resp
            send(outbox, resp)
        path.unlink(missing_ok=True)

    confirm_pending(outbox, mode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mailbox", default="./mailbox")
    parser.add_argument("--mode", choices=("confirm", "timeout"),
                        default="confirm",
                        help="confirm=下单后发 order_confirmed；"
                             "timeout=发 order_confirm_timeout")
    parser.add_argument("--interval", type=float, default=0.3)
    args = parser.parse_args()

    mailbox = Path(args.mailbox).resolve()
    print("[mock-exec] started mailbox=%s mode=%s build=%s"
          % (mailbox, args.mode, BUILD), flush=True)
    while True:
        try:
            pump(mailbox, args.mode)
        except KeyboardInterrupt:
            print("[mock-exec] stopped", flush=True)
            return 0
        except Exception as exc:
            print("[mock-exec] pump error: %s: %s"
                  % (type(exc).__name__, exc), file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
