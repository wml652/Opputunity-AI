import asyncio, sys, time
sys.path.insert(0, "D:/2.0/backend/src")
from app.infrastructure.runtime.event_loop import configure_event_loop
configure_event_loop()
from app.infrastructure.scraping.ssrf_guard import SsrfGuard
from app.infrastructure.scraping.fetchers import PlaywrightFetcher

async def step(name, coro, t=30):
    s = time.perf_counter()
    try:
        await asyncio.wait_for(coro, t); print(name, "ok", round(time.perf_counter()-s, 1), flush=True)
    except TimeoutError:
        print(name, f"HUNG >{t}s", flush=True)

async def main():
    f = PlaywrightFetcher(SsrfGuard(allow_http=True))
    for u in sys.argv[1:]:
        p = await f.fetch(u); print("fetched", p.status, len(p.html), u, flush=True)
    srv = f._proxy._server
    print("proxy active connections:", getattr(srv, "_active_count", "?"), flush=True)
    await step("browser.close", f._browser.close())
    print("proxy active after browser close:", getattr(srv, "_active_count", "?"), flush=True)
    await step("pw.stop", f._pw.stop())
    await step("proxy.stop", f._proxy.stop())
    print("active after:", getattr(srv, "_active_count", "?"))
asyncio.run(main())
