"""MailboxClient — 信箱协议外部端（桥接进程唯一信箱入口）。

协议契约见 mailbox/qmt_mailbox_executor.py 模块头注释，此处只做客户端：

- 请求帧:  {"id":..., "method":..., "params":{...}, "nonce":..., "ts":<ms>}
- 响应帧:  {"type":"resp", "id":..., "ok":true, "result":...}
- 事件帧:  {"type":"event", "event":..., "data":...}

可靠性规则（与执行器对齐）：
- 请求先写 <name>.tmp 再 os.replace 原子入 in/（同一卷）。
- 超时后用【同一个 id + nonce】重新投递 —— 执行器会从 _PROCESSED 缓存
  重放响应，不会重新执行。绝不生成新 id 重跑一遍。
- out/ 中的帧被本端消费后删除；执行器只写不读 out/，无竞态。
- 读文件立即读完即关（不长期持有句柄），执行器可能随时 MoveFileExW。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


class MailboxError(RuntimeError):
    """执行器返回 ok:false 或帧不可解析。"""

    def __init__(self, message: str, *, method: str = ""):
        super().__init__(message)
        self.method = method


class MailboxTimeoutError(TimeoutError):
    """在 timeout 内未拿到响应（已按同一 id 重投递过）。"""

    def __init__(self, method: str, request_id: str, attempts: int):
        super().__init__(
            "mailbox request timeout: method=%s id=%s attempts=%d"
            % (method, request_id, attempts)
        )
        self.method = method
        self.request_id = request_id


class _Pending:
    __slots__ = ("frame", "event", "resp", "attempts")

    def __init__(self, frame: dict):
        self.frame = frame
        self.event = threading.Event()
        self.resp: dict | None = None
        self.attempts = 0


class MailboxClient:
    """线程安全的信箱客户端。start() 后由泵线程统一扫描 out/。"""

    def __init__(
        self,
        mailbox_dir: Path | str,
        *,
        request_timeout_sec: float = 15.0,
        poll_interval_sec: float = 0.05,
        max_redeliveries: int = 2,
        event_queue_max: int = 2000,
    ):
        self.mailbox = Path(mailbox_dir)
        self.inbox = self.mailbox / "in"
        self.outbox = self.mailbox / "out"
        self.request_timeout_sec = request_timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.max_redeliveries = max_redeliveries

        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._orphans: dict[str, dict] = {}  # resp arrived before request()
        self._events: deque = deque(maxlen=event_queue_max)
        self._event_seq = 0
        self._seq_lock = threading.Lock()
        self._frame_seq = 0
        self._stop = threading.Event()
        self._pump_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def ensure_dirs(self) -> None:
        for d in (self.mailbox, self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        self.ensure_dirs()
        if self._pump_thread is not None and self._pump_thread.is_alive():
            return
        self._stop.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="mailbox-pump", daemon=True
        )
        self._pump_thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None) -> dict:
        """发送请求并等待响应。ok:true → 返回 result；ok:false → 抛 MailboxError。

        超时重投递使用同一个 id（执行器幂等缓存保证不重复执行）。
        """
        deadline = time.monotonic() + (timeout or self.request_timeout_sec)
        frame = {
            "id": uuid.uuid4().hex[:12],
            "method": method,
            "params": params or {},
            "nonce": uuid.uuid4().hex[:6],
            "ts": int(time.time() * 1000),
        }
        request_id = str(frame["id"])

        with self._lock:
            orphan = self._orphans.pop(request_id, None)
            pending = _Pending(frame)
            self._pending[request_id] = pending
        if orphan is not None:
            pending.resp = orphan
            pending.event.set()

        try:
            self._deliver(frame)
            attempts = 1
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MailboxTimeoutError(
                        method, request_id, pending.attempts or attempts
                    )
                if pending.event.wait(timeout=min(remaining, 1.0)):
                    resp = pending.resp or {}
                    if resp.get("ok"):
                        return resp.get("result")
                    raise MailboxError(
                        str(resp.get("error") or "unknown error"),
                        method=method,
                    )
                # 超时未达：同 id 重投递（执行器从 _PROCESSED 重放）。
                if time.monotonic() >= deadline:
                    continue
                if pending.attempts < self.max_redeliveries:
                    self._deliver(frame)
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def poll_events(self, since_seq: int) -> list[dict]:
        """返回 seq > since_seq 的事件（每帧附带本地递增 seq 字段）。"""
        with self._seq_lock:
            return [e for e in list(self._events) if e["seq"] > since_seq]

    # ------------------------------------------------------------------
    # 投递与泵
    # ------------------------------------------------------------------

    def _deliver(self, frame: dict) -> None:
        """原子写入 in/。先写同目录 .tmp 再 os.replace（同卷原子）。"""
        with self._lock:
            pending = self._pending.get(str(frame["id"]))
            if pending is not None:
                pending.attempts += 1
            self._frame_seq += 1
            seq = self._frame_seq
        name = "%016d-%06d-%s.json" % (
            int(time.time() * 1000), seq % 1000000, frame["nonce"]
        )
        final = self.inbox / name
        tmp = final.with_suffix(".json.tmp")
        data = (json.dumps(frame) + "\n").encode("utf-8")
        self.ensure_dirs()
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
        os.replace(tmp, final)  # 同卷原子替换

    def _pump_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_outbox()
            except Exception:
                pass
            self._stop.wait(self.poll_interval_sec)

    def _scan_outbox(self) -> None:
        try:
            names = [n for n in os.listdir(self.outbox) if n.endswith(".json")]
        except OSError:
            return
        names.sort()  # 文件名含时间戳+序号，排序即到达顺序
        for name in names:
            path = self.outbox / name
            try:
                with open(path, "rb") as fh:
                    data = fh.read(1 << 20)
            except OSError:
                continue  # 执行器可能正在写；下一轮再取
            if not data.strip():
                continue  # 尚在写入，留给下一轮
            try:
                frame = json.loads(data.decode("utf-8"))
            except Exception:
                self._quarantine(path)
                continue
            if not isinstance(frame, dict):
                self._quarantine(path)
                continue
            self._dispatch_frame(frame)
            try:
                os.remove(path)  # 消费后删除；执行器只写不读
            except OSError:
                pass

    def _dispatch_frame(self, frame: dict) -> None:
        ftype = frame.get("type")
        if ftype == "resp":
            request_id = str(frame.get("id"))
            with self._lock:
                pending = self._pending.get(request_id)
                if pending is not None:
                    pending.resp = frame
                    pending.event.set()
                else:
                    self._orphans[request_id] = frame
                    if len(self._orphans) > 200:
                        self._orphans.pop(next(iter(self._orphans)))
        elif ftype == "event":
            with self._seq_lock:
                self._event_seq += 1
                frame = dict(frame)
                frame["seq"] = self._event_seq
                frame["_ts"] = int(time.time() * 1000)
                self._events.append(frame)

    def _quarantine(self, path: Path) -> None:
        bad = self.outbox / "bad"
        try:
            bad.mkdir(exist_ok=True)
            os.replace(path, bad / path.name)
        except OSError:
            try:
                os.remove(path)
            except OSError:
                pass
