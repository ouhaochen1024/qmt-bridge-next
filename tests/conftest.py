"""共享 fixture：mock 执行器后台驱动 + 信箱目录。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))

from devtools import mock_executor as mock_exec  # noqa: E402


class MockRunner:
    """在后台线程里按 tick 驱动 mock_executor.pump。"""

    def __init__(self, mailbox: Path, mode: str = "confirm"):
        self.mailbox = mailbox
        self.mode = mode
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                mock_exec.pump(self.mailbox, self.mode)
            except Exception:
                pass
            self._stop.wait(0.1)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


@pytest.fixture()
def mailbox(tmp_path):
    return tmp_path / "mailbox"


@pytest.fixture()
def runner(mailbox):
    r = MockRunner(mailbox).start()
    yield r
    r.stop()
