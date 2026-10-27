"""执行器存活与账户绑定探测。

门禁的"最终证明"来自信箱文件本身，而不是桥接端日志：
- mailbox/hello.json     执行器初始化成功的宣告（含 account_id/build）
- mailbox/agent.hb       每 ~2s 刷新的心跳（mtime 超龄 = 失联）
- mailbox/agent.stopped  存在即表示执行器已停止（不是正常状态）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class ExecutorStatus:
    def __init__(self, mailbox_dir: Path | str, hb_max_age_sec: float = 5.0):
        self.mailbox = Path(mailbox_dir)
        self.hb_max_age_sec = hb_max_age_sec

    @property
    def _hello_path(self) -> Path:
        return self.mailbox / "hello.json"

    @property
    def _hb_path(self) -> Path:
        return self.mailbox / "agent.hb"

    @property
    def _stopped_path(self) -> Path:
        return self.mailbox / "agent.stopped"

    def _read_json(self, path: Path) -> dict | None:
        try:
            with open(path, "rb") as fh:
                data = fh.read(1 << 20)
            obj = json.loads(data.decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except (OSError, ValueError):
            return None

    def snapshot(self) -> dict[str, Any]:
        """返回 {alive, status, reason, account_id, build, hb_age_sec, ...}。"""
        hello = self._read_json(self._hello_path)
        hb = self._read_json(self._hb_path)
        stopped = self._stopped_path.exists()

        hb_age: float | None = None
        try:
            hb_age = max(0.0, time.time() - os.path.getmtime(self._hb_path))
        except OSError:
            pass

        reason = ""
        if stopped:
            status, alive = "stopped", False
            reason = "executor stopped (agent.stopped present)"
        elif hello is None:
            status, alive = "missing", False
            reason = "executor not alive: hello.json not found"
        elif hb is None or hb_age is None or hb_age > self.hb_max_age_sec:
            status, alive = "stale", False
            reason = "executor not alive: agent.hb missing or older than %.1fs" % (
                self.hb_max_age_sec
            )
        else:
            status, alive = "ok", True

        return {
            "alive": alive,
            "status": status,
            "reason": reason,
            "account_id": str((hello or {}).get("account_id") or ""),
            "account_type": str((hello or {}).get("account_type") or ""),
            "executor_build": str((hello or {}).get("build") or ""),
            "hb_seq": (hb or {}).get("hb_seq") if isinstance(hb, dict) else None,
            "hb_pending": (hb or {}).get("pending") if isinstance(hb, dict) else None,
            "hb_age_sec": round(hb_age, 3) if hb_age is not None else None,
        }

    def require_alive(self) -> dict:
        """不存活时抛 RuntimeError（携带明确 reason）。"""
        snap = self.snapshot()
        if not snap["alive"]:
            raise RuntimeError(snap["reason"])
        return snap
