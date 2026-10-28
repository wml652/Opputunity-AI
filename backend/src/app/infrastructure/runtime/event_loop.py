"""Event-loop setup shared by every entrypoint.

On Windows, Playwright launches browsers via `asyncio.create_subprocess_exec`, which only the
Proactor loop supports (the Selector loop raises `NotImplementedError`). Some libraries (uvicorn
reload, older arq paths) switch to a Selector loop, so entrypoints pin Proactor explicitly.
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from collections.abc import Callable, Coroutine
from typing import Any

IS_WINDOWS = sys.platform == "win32"


def configure_event_loop() -> None:
    """Install the Proactor policy on Windows (no-op elsewhere). Idempotent."""
    if not IS_WINDOWS:
        return
    if sys.version_info < (3, 14):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy = asyncio.get_event_loop_policy()
            if not isinstance(policy, asyncio.WindowsProactorEventLoopPolicy):  # type: ignore[attr-defined,unused-ignore]
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    if IS_WINDOWS:
        return asyncio.ProactorEventLoop  # type: ignore[attr-defined,no-any-return,unused-ignore]
    return asyncio.new_event_loop


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` with a subprocess-capable loop on every platform."""
    configure_event_loop()
    with asyncio.Runner(loop_factory=loop_factory()) as runner:
        return runner.run(coro)


def supports_subprocesses(loop: asyncio.AbstractEventLoop) -> bool:
    if not IS_WINDOWS:
        return True
    return isinstance(loop, asyncio.ProactorEventLoop)  # type: ignore[attr-defined,unused-ignore]
