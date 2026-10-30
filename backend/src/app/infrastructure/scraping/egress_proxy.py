"""SSRF-enforcing egress proxy for headless Chromium.

Chromium is launched with `--proxy-server=http://127.0.0.1:<port>` and
`--proxy-bypass-list=<-loopback>` (so even loopback targets go through here). Every connection the
browser makes, including navigations, redirect hops, subresources, XHR, and WebSockets, arrives
as `CONNECT host:port` (HTTPS) or an absolute-URI request (plain HTTP). The proxy validates the
destination with the SSRF guard, resolves it ONCE, and connects to that pinned IP (no DNS
rebinding between check and use).

HTTPS remains an end-to-end TLS tunnel, so sites see the real browser's TLS and HTTP
fingerprint. Intercepting requests in Node (`route.fetch`) instead gets blocked by bot protection
(HTTP 403), which is the failure this design replaces.
"""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit

from app.domain.shared.errors import DnsResolutionError, UnsafeUrlError
from app.infrastructure.logging import get_logger
from app.infrastructure.scraping.ssrf_guard import SsrfGuard

log = get_logger(__name__)

_MAX_HEADER = 64 * 1024
STOP_TIMEOUT_S = 1.5


class EgressProxy:
    def __init__(self, guard: SsrfGuard) -> None:
        self._guard = guard
        self._server: asyncio.base_events.Server | None = None
        self.port = 0
        self.blocked: list[str] = []
        self.errors: list[tuple[str, str]] = []  # (url, dns_error|tunnel_connect_error)
        # Every live socket (client and upstream side) and handler task, so `stop()` can
        # force-close tunnels whose peer never sends EOF (otherwise Python 3.12's
        # `Server.wait_closed()` waits on them forever).
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    @property
    def open_connections(self) -> int:
        return len(self._writers)

    async def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Close the listener, abort every open tunnel, and never wait longer than `timeout`."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for w in list(self._writers):
            transport = w.transport
            with contextlib.suppress(Exception):
                transport.abort()
        self._writers.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(list(self._tasks), timeout=timeout)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=timeout)

    def _track(self, *writers: asyncio.StreamWriter) -> None:
        self._writers.update(writers)

    def _untrack(self, *writers: asyncio.StreamWriter) -> None:
        for w in writers:
            self._writers.discard(w)
            with contextlib.suppress(Exception):
                w.close()

    async def _resolve(self, url: str) -> tuple[str, int]:
        """Validate and return the pinned (ip, port) to connect to."""
        await self._guard.validate(url)
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ips = await self._guard.resolve_pinned(host, port)
        return ips[0], port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._track(writer)
        try:
            await self._serve(reader, writer)
        except asyncio.CancelledError:
            pass
        finally:
            self._untrack(writer)
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            ConnectionError,
        ):
            writer.close()
            return
        if len(head) > _MAX_HEADER:
            writer.close()
            return
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            writer.close()
            return
        if method.upper() == "CONNECT":
            await self._tunnel(target, reader, writer)
        else:
            await self._forward(method, target, version, rest, reader, writer)

    async def _deny(self, writer: asyncio.StreamWriter, url: str, reason: str) -> None:
        self.blocked.append(url)
        log.info("egress.blocked", url=url, reason=reason)
        with contextlib.suppress(Exception):
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nX-OOTG-Blocked: ssrf\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        writer.close()

    async def _gateway_error(self, writer: asyncio.StreamWriter, url: str, kind: str) -> None:
        """Upstream unreachable (`dns_error` / `tunnel_connect_error`): NOT an SSRF block."""
        self.errors.append((url, kind))
        with contextlib.suppress(Exception):
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nX-OOTG-Error: " + kind.encode() + b"\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        writer.close()

    async def _tunnel(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, _, port_text = target.rpartition(":")
        url = f"https://{host}:{port_text}/" if port_text != "443" else f"https://{host}/"
        try:
            ip, port = await self._resolve(url)
            upstream_r, upstream_w = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=20
            )
        except DnsResolutionError:
            await self._gateway_error(writer, url, "dns_error")
            return
        except UnsafeUrlError as exc:
            await self._deny(writer, url, exc.message)
            return
        except (OSError, TimeoutError):
            await self._gateway_error(writer, url, "tunnel_connect_error")
            return
        self._track(upstream_w)
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _pipe_both(reader, writer, upstream_r, upstream_w)
        finally:
            self._untrack(upstream_w)

    async def _forward(
        self,
        method: str,
        target: str,
        version: str,
        rest: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not target.startswith("http://"):
            await self._deny(writer, target, "only absolute http URLs are proxied")
            return
        try:
            ip, port = await self._resolve(target)
            upstream_r, upstream_w = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=20
            )
        except DnsResolutionError:
            await self._gateway_error(writer, target, "dns_error")
            return
        except UnsafeUrlError as exc:
            await self._deny(writer, target, exc.message)
            return
        except (OSError, TimeoutError):
            await self._gateway_error(writer, target, "tunnel_connect_error")
            return
        parts = urlsplit(target)
        origin_form = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        # One request per connection: a kept-alive connection would carry the browser's NEXT
        # request (absolute-URI form, possibly another host) straight to this pinned upstream
        # without validation. `Connection: close` makes Chromium open a fresh proxy connection.
        headers = [
            line
            for line in rest.split(b"\r\n")
            if line
            and not line.lower().startswith(
                (b"proxy-connection:", b"proxy-authorization:", b"connection:", b"keep-alive:")
            )
        ]
        headers.append(b"Connection: close")
        self._track(upstream_w)
        try:
            upstream_w.write(
                f"{method} {origin_form} {version}\r\n".encode("latin-1")
                + b"\r\n".join(headers)
                + b"\r\n\r\n"
            )
            await upstream_w.drain()
            await _pipe_both(reader, writer, upstream_r, upstream_w)
        finally:
            self._untrack(upstream_w)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            dst.close()


async def _pipe_both(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
) -> None:
    """Relay both directions; as soon as EITHER side finishes, close both (a peer that keeps
    its half open must not pin the tunnel, and with it the proxy shutdown, forever)."""
    tasks = [
        asyncio.ensure_future(_pipe(client_r, upstream_w)),
        asyncio.ensure_future(_pipe(upstream_r, client_w)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in (client_w, upstream_w):
            with contextlib.suppress(Exception):
                w.close()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
