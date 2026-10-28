"""Forensic extraction test on saved markdown using the user's OWN active provider/model. No DB writes."""
import asyncio, hashlib, json, os, re, sys, time
from pathlib import Path

os.chdir("D:/2.0/backend")
sys.path.insert(0, "D:/2.0/backend/src")
from app.infrastructure.runtime.event_loop import configure_event_loop
configure_event_loop()
import uuid
from app.infrastructure.config.settings import get_settings
from app.bootstrap.container import build_infrastructure, build_discovery_deps
from app.infrastructure.extraction.llm_adapters import LlmOpportunityExtractor, ExtractionError
from app.domain.discovery.playbook import ExtractionStrategy

USER = uuid.UUID("01a17f7a-5670-77d4-9186-ce8818d03735")
P = Path("D:/2.0/.audit/pages")
def slug(u): return hashlib.sha1(u.encode()).hexdigest()[:10]
CASES = [
    ("study-abroad / trafilatura", "https://www.study-abroad.org/blog/scholarship-deadlines-calendar-2026/", "browser.md"),
    ("study-abroad / bs-fallback", "https://www.study-abroad.org/blog/scholarship-deadlines-calendar-2026/", "browser.fallback.md"),
    ("scholarships.com / trafilatura", "https://www.scholarships.com/financial-aid/college-scholarships/scholarships-by-grade", "browser.md"),
    ("scholarships.com / bs-fallback", "https://www.scholarships.com/financial-aid/college-scholarships/scholarships-by-grade", "browser.fallback.md"),
    ("opportunitiesradar home / trafilatura", "https://opportunitiesradar.com/", "browser.md"),
    ("opportunitiesradar home / bs-fallback", "https://opportunitiesradar.com/", "browser.fallback.md"),
]
ONLY = sys.argv[1:]

async def main():
    infra = build_infrastructure(get_settings())
    deps = build_discovery_deps(infra)
    llm = await deps.chat.execute(USER)
    print(json.dumps({"model": llm.model}), flush=True)
    ex = LlmOpportunityExtractor()
    for label, url, kind in CASES:
        if ONLY and not any(o in label for o in ONLY):
            continue
        f = P / f"{slug(url)}.{kind}"
        if not f.exists():
            # refetch used the shorter URL for scholarships.com
            alt = [x for x in P.glob(f"*.{kind}") if x.stat().st_size > 0]
            print(json.dumps({"case": label, "missing": str(f)})); continue
        md = f.read_text(encoding="utf-8")
        heads = len(re.findall(r"^#{2,4} ", md, re.M))
        links = md.count("](")
        t = time.perf_counter()
        try:
            cc = int(os.environ.get('CHUNK', '0'))
            strat = ExtractionStrategy(chunk_chars=cc, overlap_chars=400, max_tokens=16384, level=2) if cc else None
            res = await ex.extract(llm, md, url, strat)
            d, items = res.diagnostics, res.extracts
            out = {"ok": True, "items": len(items),
                   "with_deadline": sum(1 for i in items if i.deadline),
                   "portal_is_listing_page": sum(1 for i in items if (i.portal_url or "").rstrip("/") == url.rstrip("/")),
                   "titles": [i.title[:60] for i in items][:25]}
        except ExtractionError as e:
            d, out = e.diagnostics, {"ok": False, "error": e.message}
        except Exception as e:  # noqa
            d, out = {}, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        out |= {"case": label, "md_chars": len(md), "md_headings": heads, "md_links": links,
                "secs": round(time.perf_counter() - t, 1),
                "attempts": [{k: a.get(k) for k in ("window", "chars", "finish_reason", "reply_chars", "completion_tokens", "retried", "error")} for a in d.get("attempts", [])]}
        print(json.dumps(out), flush=True)

asyncio.run(main())
