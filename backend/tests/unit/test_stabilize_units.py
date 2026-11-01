"""Track 0 units: fatal provider codes, campaign outcome, title clipping, log noise, memory
admission and readiness."""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime

import pytest

from app.application.system.use_cases import CheckReadiness
from app.domain.discovery.models import MAX_PAGE_TITLE, CrawledPage
from app.domain.discovery.progress import campaign_outcome, campaign_progress
from app.domain.llm.provider import ConnectionErrorCode, LLMProviderError, is_fatal
from app.domain.shared.enums import CrawlStatus
from app.infrastructure.llm.http_client import LLMHttpError
from app.infrastructure.logging import BenignDisconnectFilter
from app.infrastructure.runtime import resources


@pytest.mark.parametrize(
    ("code", "fatal"),
    [
        (ConnectionErrorCode.UNAUTHORIZED, True),
        (ConnectionErrorCode.FORBIDDEN, True),
        (ConnectionErrorCode.MODEL_NOT_FOUND, True),
        (ConnectionErrorCode.RATE_LIMITED, False),
        (ConnectionErrorCode.TIMEOUT, False),
        (ConnectionErrorCode.UPSTREAM, False),
    ],
)
def test_only_key_and_model_rejections_are_fatal(code: ConnectionErrorCode, fatal: bool) -> None:
    assert is_fatal(LLMHttpError(code, "x")) is fatal


def test_non_provider_errors_are_never_fatal() -> None:
    assert not is_fatal(LLMProviderError("generic"))
    assert not is_fatal(ValueError("forbidden"))


@pytest.mark.parametrize(
    ("counters", "outcome"),
    [
        ({}, "ok"),
        ({"extract_done": 10, "extract_failed": 0, "opportunities_found": 4}, "ok"),
        ({"extract_done": 10, "extract_failed": 3, "opportunities_found": 4}, "ok"),
        ({"extract_done": 10, "extract_failed": 4, "opportunities_found": 4}, "degraded"),
        ({"extract_done": 10, "extract_failed": 9, "opportunities_found": 1}, "degraded"),
        ({"extract_done": 56, "extract_failed": 56, "opportunities_found": 0}, "failed"),
    ],
)
def test_campaign_outcome_thresholds(counters: dict[str, int], outcome: str) -> None:
    assert campaign_outcome(counters)[0] == outcome


def test_outcome_appears_only_once_the_campaign_is_final() -> None:
    counters = {"extract_done": 5, "extract_failed": 5, "opportunities_found": 0}
    running = campaign_progress("EXTRACTING", counters, auto_stage=False)
    assert running.outcome is None
    done = campaign_progress("COMPLETED", counters, auto_stage=False)
    assert done.outcome == "failed" and done.current.label == "Completed - nothing extracted"
    assert done.outcome_detail == "5 of 5 pages failed extraction"


def _page(title: str | None) -> CrawledPage:
    return CrawledPage(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), None, "https://x.org", None, 200,
        CrawlStatus.DISTILLED, title, None, None, [], None, datetime.now(UTC),
    )  # fmt: skip


def test_crawled_page_title_fits_the_column() -> None:
    assert len(_page("A" * 1000).title or "") == MAX_PAGE_TITLE
    assert _page("Short").title == "Short"
    assert _page(None).title is None


def _record(exc: BaseException | None) -> logging.LogRecord:
    info = (type(exc), exc, None) if exc else None
    return logging.LogRecord(
        "asyncio", logging.ERROR, __file__, 1, "Exception in callback", (), info
    )


def test_benign_disconnects_are_demoted_and_real_errors_kept() -> None:
    f = BenignDisconnectFilter()
    reset = _record(ConnectionResetError(10054, "forcibly closed"))
    assert f.filter(reset) is logging.getLogger().isEnabledFor(logging.DEBUG)
    assert reset.levelno == logging.DEBUG
    real = _record(RuntimeError("boom"))
    assert f.filter(real) is True and real.levelno == logging.ERROR
    assert f.filter(_record(None)) is True


class _Mem:
    def __init__(self, values: list[int | None]) -> None:
        self.values = values

    def free_memory_mb(self) -> int | None:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


async def test_memory_admission_waits_then_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(resources.asyncio, "sleep", no_sleep)
    assert await resources.wait_for_memory(_Mem([4000]), 1200) is True
    assert await resources.wait_for_memory(_Mem([None]), 1200) is True  # unmeasurable: go
    assert await resources.wait_for_memory(_Mem([500, 800, 1500]), 1200) is True
    assert await resources.wait_for_memory(_Mem([500]), 1200, max_wait_s=20) is False


def test_psutil_reports_memory() -> None:
    free = resources.PsutilResources().free_memory_mb()
    assert free is not None and free > 0


class _Probe:
    def __init__(self, name: str, ok: bool) -> None:
        self.name, self.ok = name, ok

    async def check(self) -> bool:
        return self.ok


async def test_readiness_is_the_and_of_its_probes() -> None:
    mem = _Mem([2048])
    ok = await CheckReadiness([_Probe("database", True), _Probe("redis", True)], mem).execute()
    assert ok.ready and ok.free_memory_mb == 2048
    bad = await CheckReadiness([_Probe("database", False), _Probe("redis", True)], mem).execute()
    assert not bad.ready and bad.checks == {"database": False, "redis": True}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows memory API")
def test_launcher_measures_free_memory() -> None:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import launcher  # type: ignore[import-not-found]

    free = launcher.free_memory_mb()
    assert free is not None and free > 0
    assert launcher.pid_alive(__import__("os").getpid())
