"""Forensic re-fetch: no DB writes. Saves HTML/markdown under .audit/pages/."""
import asyncio, json, re, sys, time, hashlib
from pathlib import Path
from urllib.parse import urlsplit, urljoin

sys.path.insert(0, "D:/2.0/backend/src")
from app.infrastructure.runtime.event_loop import configure_event_loop
configure_event_loop()
from bs4 import BeautifulSoup
import trafilatura
from app.infrastructure.scraping.ssrf_guard import SsrfGuard
from app.infrastructure.scraping.fetchers import PlaywrightFetcher, SafeHttpFetcher
from app.infrastructure.scraping.distiller import HtmlContentProcessor, _fallback_markdown
from app.infrastructure.scraping.sanitizer import sanitize_html

OUT = Path("D:/2.0/.audit/pages"); OUT.mkdir(parents=True, exist_ok=True)
URLS = [
    "https://www.scholarshiptab.com/undergraduate",
    "https://www.scholarshiptab.com/undergraduate/fully-funded",
    "https://ifbr.jhmi.edu/student-internship-program",
    "https://www.iitk.ac.in/oir/iitk-rise",
    "https://colemanpublications.com/education/getfund-opens-2026-2027-undergraduate-local-scholarship/",
    "https://bold.org/scholarships/by-year/undergraduate-scholarships/",
    "https://studentscholarships.org/scholarships",
    "https://www.study-abroad.org/blog/scholarship-deadlines-calendar-2026/",
    "https://www.collegedecider.com/blog/posts/college-scholarship-deadlines-calendar-2026",
    "https://www.scholarships.com/financial-aid/college-scholarships/scholarships-by-grade",
    "https://teachwink.com/internships-in-pakistan/",
    "https://opportunitiesradar.com/",
    "https://opportunitiesradar.com/category/internships/",
    "https://www.opportunitiesforyouth.org/",
    "https://uscholarships.us/",
    "https://www.mitacs.ca/our-programs/globalink-research-internship/",
    "https://www.cbdpunjab.gov.pk/ycp2026",
]
ONLY = sys.argv[1:]  # optional substring filter

def slug(u): return hashlib.sha1(u.encode()).hexdigest()[:10]

def listing_stats(html, url, md):
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(url).hostname
    detail = set()
    for a in soup.find_all("a", href=True):
        h = urljoin(url, a["href"]).split("#")[0]
        p = urlsplit(h)
        if p.hostname != host or p.path.rstrip("/") in ("", urlsplit(url).path.rstrip("/")):
            continue
        segs = [s for s in p.path.split("/") if s]
        text = " ".join(a.get_text(" ").split())
        # heuristic "card/detail link": slug-like last segment with >=3 hyphen words, or long anchor text
        if segs and (segs[-1].count("-") >= 2 or len(text) >= 25) and not re.search(r"/(category|tag|page|author|about|contact|privacy|login|signup)\b", p.path):
            detail.add(h.rstrip("/"))
    in_md = {d for d in detail if d in md or d + "/" in md}
    return len(detail), len(in_md)

async def one(pw, http, url):
    rec = {"url": url}
    for name, f in (("browser", pw), ("http", http)):
        t = time.perf_counter()
        try:
            page = await asyncio.wait_for(f.fetch(url), timeout=90)
        except Exception as e:  # noqa
            rec[name] = {"error": f"{type(e).__name__}: {e}"[:200]}
            continue
        r = {"status": page.status, "final": page.final_url, "html_bytes": len(page.html or ""),
             "blocked": page.blocked, "error": (page.error or "")[:160],
             "secs": round(time.perf_counter() - t, 1),
             "failed_steps": [f"{s['stage']}:{s['detail'].get('error','')}"[:140] for s in page.trace if s["status"] == "fail"]}
        if page.html:
            soup, _ = sanitize_html(page.html)
            tra_default = trafilatura.extract(str(soup), url=url, output_format="markdown", include_links=True, include_tables=True) or ""
            tra_recall = trafilatura.extract(str(soup), url=url, output_format="markdown", include_links=True, include_tables=True, favor_recall=True) or ""
            fb = _fallback_markdown(soup, url)
            final = HtmlContentProcessor().process(page.html, url).markdown
            nd, nmd = listing_stats(page.html, page.final_url or url, final)
            _, nfb = listing_stats(page.html, page.final_url or url, fb)
            r |= {"trafilatura_default": len(tra_default), "trafilatura_recall": len(tra_recall),
                  "bs_fallback": len(fb), "pipeline_md": len(final),
                  "md_links": final.count("]("), "detail_links_in_html": nd,
                  "detail_links_in_pipeline_md": nmd, "detail_links_in_bs_fallback": nfb,
                  "title": (BeautifulSoup(page.html, "html.parser").title or BeautifulSoup("<t></t>", "html.parser")).get_text()[:80]}
            (OUT / f"{slug(url)}.{name}.html").write_text(page.html, encoding="utf-8")
            (OUT / f"{slug(url)}.{name}.md").write_text(final, encoding="utf-8")
            (OUT / f"{slug(url)}.{name}.fallback.md").write_text(fb, encoding="utf-8")
        rec[name] = r
    return rec

async def main():
    guard = SsrfGuard(allow_http=True)
    pw, http = PlaywrightFetcher(guard), SafeHttpFetcher(guard)
    urls = [u for u in URLS if not ONLY or any(o in u for o in ONLY)]
    for u in urls:
        rec = await one(pw, http, u)
        print(json.dumps(rec), flush=True)
    t = time.perf_counter()
    try:
        await asyncio.wait_for(pw.aclose(), timeout=60)
        print(json.dumps({"aclose_secs": round(time.perf_counter() - t, 1)}))
    except TimeoutError:
        print(json.dumps({"aclose_secs": "HUNG >60s"}))
    await http.aclose()

asyncio.run(main())
