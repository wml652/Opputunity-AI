from __future__ import annotations

import pytest

from app.domain.shared.errors import UnsafeUrlError
from app.infrastructure.scraping.ssrf_guard import SsrfGuard, is_public_ip

RESOLUTION = {
    "api.example.com": ["93.184.216.34"],
    "intranet.example.com": ["10.1.2.3"],
    "metadata.example.com": ["169.254.169.254"],
    "mixed.example.com": ["93.184.216.34", "127.0.0.1"],
    "v6.example.com": ["2606:2800:220:1:248:1893:25c8:1946"],
    "mapped.example.com": ["::ffff:192.168.1.10"],
    "cgnat.example.com": ["100.64.0.7"],
}


async def resolver(host: str, _port: int) -> list[str]:
    if host not in RESOLUTION:
        raise OSError("nxdomain")
    return RESOLUTION[host]


@pytest.fixture
def guard() -> SsrfGuard:
    return SsrfGuard(resolver=resolver)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("https://API.Example.com./v1/", "https://api.example.com/v1/"),
        ("https://v6.example.com/v1", "https://v6.example.com/v1"),
        ("https://api.example.com/v1#frag", "https://api.example.com/v1"),
    ],
)
async def test_public_urls_pass(guard: SsrfGuard, url: str, expected: str) -> None:
    assert await guard.validate(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",  # plain http
        "ftp://api.example.com/",
        "file:///etc/passwd",
        "https://user:pw@api.example.com/",
        "https://localhost/v1",
        "https://svc.internal/v1",
        "https://intranet.example.com/",
        "https://metadata.example.com/latest/meta-data",
        "https://mixed.example.com/",  # any non-public address poisons the host
        "https://mapped.example.com/",
        "https://cgnat.example.com/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://api.example.com:6379/",
        "https://unknown.example.com/",
        "https:///nohost",
    ],
)
async def test_unsafe_urls_rejected(guard: SsrfGuard, url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await guard.validate(url)


async def test_private_hosts_allowed_when_opted_in() -> None:
    guard = SsrfGuard(allow_private=True, resolver=resolver)
    assert await guard.validate("http://localhost:11434/v1") == "http://localhost:11434/v1"


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("8.8.8.8", True),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.1.1", False),
        ("0.0.0.0", False),  # noqa: S104
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00:ec2::254", False),
        ("::ffff:10.0.0.1", False),
        ("2002:0a00:0001::1", False),  # 6to4 wrapping 10.0.0.1
        ("2606:4700:4700::1111", True),
        ("garbage", False),
    ],
)
def test_is_public_ip(ip: str, public: bool) -> None:
    assert is_public_ip(ip) is public
