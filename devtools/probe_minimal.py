#!/usr/bin/env python3
"""probe_minimal.py — 第 1 步最小回路验证（外部 Python，不依赖 QMT）。

按任务书第 1 步要求，用最原始的文件操作模拟执行器最小行为：
1. 写 hello.json / agent.hb（桥接端识别"执行器存活"的依据）；
2. 写入一条 {"id":1,"method":"query_stock_asset",...} 请求帧；
3. 驱动 mock pump（模拟执行器每 tick 推进一步），扫 out/ 校验 resp。

不经过 MailboxClient —— 这是对协议本身的裸验证；MailboxClient 层
的回路由 tests/test_mailbox_flow.py 覆盖。

用法： python devtools/probe_minimal.py --mailbox ./mailbox
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devtools.mock_executor import pump  # noqa: E402

HERE = Path(__file__).resolve().parent


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mailbox", default="./mailbox")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()

    mailbox = Path(args.mailbox).resolve()
    (mailbox / "in").mkdir(parents=True, exist_ok=True)
    (mailbox / "out").mkdir(parents=True, exist_ok=True)

    # 1) 模拟执行器宣告存活
    write_atomic(mailbox / "hello.json", (
        json.dumps({"type": "hello", "version": "1.3",
                    "account_id": "8886001888", "account_type": "STOCK",
                    "strategy": "probe", "build": "probe",
                    "started_at": int(time.time() * 1000)}) + "\n"
    ).encode("utf-8"))
    write_atomic(mailbox / "agent.hb", (
        json.dumps({"ts": int(time.time() * 1000), "build": "probe",
                    "seq": 1, "pending": 0}) + "\n"
    ).encode("utf-8"))
    print("[probe] hello.json / agent.hb written -> %s" % mailbox)

    # 2) 写入固定 id 的请求帧（重试也用同一 id：执行器幂等重放）
    frame = {"id": 1, "method": "query_stock_asset", "params": {},
             "nonce": "t1", "ts": int(time.time() * 1000)}
    req_path = mailbox / "in" / "probe-req.json"
    write_atomic(req_path, (json.dumps(frame) + "\n").encode("utf-8"))

    # 3) 每 tick 推进一步，扫 out/ 找 id=1 的 resp
    for i in range(args.rounds):
        pump(mailbox, "confirm")
        for name in sorted((mailbox / "out").glob("*.json")):
            try:
                data = name.read_bytes()
                frame_out = json.loads(data.decode("utf-8"))
            except Exception:
                continue  # 可能仍在写入
            if frame_out.get("type") == "resp" and frame_out.get("id") == 1:
                name.unlink(missing_ok=True)
                if frame_out.get("ok"):
                    print("[probe] OK: query_stock_asset -> %s"
                          % json.dumps(frame_out["result"], ensure_ascii=False))
                    return 0
                print("[probe] FAIL: executor error: %s"
                      % frame_out.get("error"))
                return 1
        time.sleep(0.1)

    print("[probe] FAIL: no resp for id=1 after %d rounds" % args.rounds)
    return 1


if __name__ == "__main__":
    sys.exit(main())
