"""CLI 入口：python -m qmt_bridge"""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description="qmt-bridge-next HTTP/WS bridge")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args, _ = parser.parse_known_args()

    from .config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port

    logging.basicConfig(level=settings.log_level.upper())
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
