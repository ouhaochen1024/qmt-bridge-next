"""CLI 入口：python -m qmt_bridge"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler


def _setup_logging(settings) -> None:
    """控制台 + mailbox/logs/bridge-YYYYMMDD.log 双写（失败不影响启动）。"""
    level = settings.log_level.upper()
    logging.basicConfig(level=level)

    try:
        from datetime import datetime
        from pathlib import Path

        log_dir = Path(settings.resolved_mailbox()) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logfile = log_dir / ("bridge-%s.log" % datetime.now().strftime("%Y%m%d"))
        handler = RotatingFileHandler(
            logfile, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        logging.getLogger("qmt_bridge").addHandler(handler)
    except OSError:
        logging.getLogger("qmt_bridge").warning(
            "file logging disabled (cannot create mailbox/logs)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="qmt-bridge-next HTTP/WS bridge")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args, _ = parser.parse_known_args()

    from .config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port

    _setup_logging(settings)
    log = logging.getLogger("qmt_bridge")
    log.info("qmt-bridge-next starting: host=%s port=%s mailbox=%s",
             host, port, settings.resolved_mailbox())
    if not (settings.api_key or "").strip():
        log.warning("QMT_BRIDGE_API_KEY 未配置：账户/交易接口无鉴权，"
                    "请勿监听 0.0.0.0 或暴露到局域网")

    import uvicorn

    uvicorn.run(
        "qmt_bridge.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
