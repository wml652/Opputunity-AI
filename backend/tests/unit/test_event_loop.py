from __future__ import annotations

import asyncio
import sys

import pytest

from app.infrastructure.runtime.event_loop import (
    configure_event_loop,
    loop_factory,
    run,
    supports_subprocesses,
)


async def _spawn_child() -> str:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('child-ok')", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


def test_run_spawns_subprocesses() -> None:
    """This is what Playwright needs; a Selector loop on Windows raises NotImplementedError."""
    assert run(_spawn_child()) == "child-ok"


async def test_pytest_loop_supports_subprocesses() -> None:
    assert supports_subprocesses(asyncio.get_running_loop())
    assert await _spawn_child() == "child-ok"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific policy")
def test_windows_uses_proactor() -> None:
    configure_event_loop()
    configure_event_loop()  # idempotent
    loop = loop_factory()()
    try:
        assert isinstance(loop, asyncio.ProactorEventLoop)  # type: ignore[attr-defined,unused-ignore]
    finally:
        loop.close()
    if sys.version_info < (3, 14):
        policy = asyncio.get_event_loop_policy()
        assert isinstance(policy, asyncio.WindowsProactorEventLoopPolicy)  # type: ignore[attr-defined,unused-ignore]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific failure mode")
def test_selector_loop_would_fail_on_windows() -> None:
    """Documents the bug the policy prevents."""
    loop = asyncio.SelectorEventLoop()
    try:
        assert not supports_subprocesses(loop)
        with pytest.raises(NotImplementedError):
            loop.run_until_complete(_spawn_child())
    finally:
        loop.close()
