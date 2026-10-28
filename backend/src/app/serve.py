"""`uv run serve` — API entrypoint that pins a subprocess-capable event loop on Windows."""

from __future__ import annotations

import argparse
import asyncio
import contextlib

import uvicorn

from app.infrastructure.runtime.event_loop import configure_event_loop, run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    configure_event_loop()
    config = uvicorn.Config(
        "app.bootstrap.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        loop="none",  # we own the loop (Proactor on Windows); uvicorn must not replace it
        proxy_headers=True,
        log_config=None,  # structlog owns logging (with redaction)
    )
    server = uvicorn.Server(config)
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        run(server.serve())


if __name__ == "__main__":
    main()
