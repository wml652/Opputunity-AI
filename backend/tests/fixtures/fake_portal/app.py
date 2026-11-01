"""A realistic 3-step application portal for form-filler tests.

  /apply            step 1: name, email, phone, GPA, university, degree level (select)
  /apply/step2      step 2: essay textarea + a CUSTOM question no profile field answers
  /apply/step3      step 3: CV upload (PDF) + the final "Submit application" button
  POST /__wipe      drop every server-side draft (simulates an expired portal session)
  GET  /__state     inspect drafts, submissions and request counters

Drafts live server-side keyed by a session cookie, so resuming requires the browser's saved
storage_state; after /__wipe the portal redirects to step 1 and the filler must replay.
`?v=2` serves the same form under a different opportunity (same custom question).
`/programme/{slug}` is an overview page whose "Apply Now" button leads to the form.
`?spa=1` mounts the step-1 form from JavaScript after a delay (SPA hydration).
Pathfinder pages: `/agg/article/{slug}` (an aggregator post - served on the 127.0.0.2 mirror,
with a lookalike form that reports keystrokes), `/uni/overview` -> `/uni/how-to-apply` (an
'Apply Online' button that window.open()s the form), `/slate/landing` (sign-in gate),
`/uni/email-apply` (email only) and `/uni/closed` (applications closed).
`?v=members` sits behind a sign-in wall unless the browser carries the `portal_auth=ok` cookie.
"""

from __future__ import annotations

import asyncio
import html
import threading
import uuid
from collections import Counter
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

CUSTOM_QUESTION = "Which programming languages are you most comfortable with?"
SLOW_PAGE_SECONDS = 4.0
INVISIBLE_RECAPTCHA = (
    "<script src='https://www.google.com/recaptcha/api.js?render=site-key' async defer></script>"
    "<div class='g-recaptcha' data-size='invisible'></div>"
)
COOKIE_BANNER = (
    "<div id='cookie-consent' role='dialog' style='position:fixed;bottom:0;left:0;right:0;"
    "background:#eee;padding:12px'>We use cookies to improve this site."
    "<button type='button' onclick=\"document.getElementById('cookie-consent').remove()\">"
    "Accept all</button></div>"
)
ESSAY_LABEL = "Why do you want to join this research program? (essay)"


class PortalState:
    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, Any]] = {}
        self.submissions: list[dict[str, Any]] = []
        self.hits: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.official_base = ""  # the 127.0.0.1 server: the "official university" host

    def reset(self) -> None:
        with self.lock:
            self.drafts.clear()
            self.submissions.clear()
            self.hits.clear()


STATE = PortalState()


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{html.escape(title)}</title></head><body>"
        f"<main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def build_app() -> FastAPI:
    app = FastAPI()

    def session(request: Request) -> tuple[str, dict[str, Any] | None]:
        sid = request.cookies.get("portal_sid") or ""
        return sid, STATE.drafts.get(sid)

    @app.get("/apply")
    async def step1(request: Request, v: str = "1") -> Response:
        STATE.hits["GET /apply"] += 1
        if v == "members" and request.cookies.get("portal_auth") != "ok":
            STATE.hits["GET /apply login-wall"] += 1
            return _page(
                "Applicant Portal - Sign in",
                "<form method='post' action='/login'>"
                "<label for='u'>Email</label><input id='u' name='username' type='email' required>"
                "<label for='p'>Password</label><input id='p' name='password' type='password'"
                " required><button type='submit'>Sign in</button></form>",
            )
        sid, draft = session(request)
        if draft is None:
            sid = uuid.uuid4().hex
            STATE.drafts[sid] = draft = {"variant": v}
        body = (
            "<form method='post' action='/apply/step1'>"
            "<label for='full_name'>Full name</label><input id='full_name' name='full_name' required>"
            "<label for='email'>Email address</label><input id='email' name='email' type='email' required>"
            "<label for='phone'>Mobile number</label><input id='phone' name='phone' type='tel' required>"
            "<label for='gpa'>Cumulative GPA</label><input id='gpa' name='gpa' required>"
            "<label for='uni'>University</label><input id='uni' name='university' required>"
            "<label for='level'>Current level of study</label>"
            "<select id='level' name='level' required><option value=''>Choose</option>"
            "<option value='ug'>Undergraduate (Bachelor)</option><option value='pg'>Postgraduate</option></select>"
            "<label for='nick'>Nickname (optional)</label><input id='nick' name='nickname'>"
            "<button type='submit'>Next</button></form>"
        )
        if request.query_params.get("spa"):
            body = (
                "<div id='root'>Loading the application...</div>"
                f"<template id='tpl'>{body}</template>"
                "<script>setTimeout(() => { const r = document.getElementById('root');"
                " r.innerHTML = ''; r.appendChild(document.getElementById('tpl').content"
                ".cloneNode(true)); }, 900);</script>"
            )
        response = _page(f"Research Program Application {v} - Step 1 of 3", body)
        response.set_cookie("portal_sid", sid, httponly=True, samesite="lax")
        return response

    @app.post("/apply/step1")
    async def step1_post(
        request: Request,
        full_name: str = Form(...),
        email: str = Form(...),
        phone: str = Form(...),
        gpa: str = Form(...),
        university: str = Form(...),
        level: str = Form(...),
        nickname: str = Form(""),
    ) -> Response:
        STATE.hits["POST /apply/step1"] += 1
        _, draft = session(request)
        if draft is None:
            return RedirectResponse("/apply", status_code=303)
        draft["step1"] = {
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "gpa": gpa,
            "university": university,
            "level": level,
            "nickname": nickname,
        }
        return RedirectResponse("/apply/step2", status_code=303)

    @app.get("/apply/step2")
    async def step2(request: Request) -> Response:
        STATE.hits["GET /apply/step2"] += 1
        _, draft = session(request)
        if draft is None or "step1" not in draft:
            return RedirectResponse("/apply", status_code=303)  # draft lost -> back to start
        body = (
            "<form method='post' action='/apply/step2'>"
            f"<label for='essay'>{html.escape(ESSAY_LABEL)}</label>"
            "<textarea id='essay' name='motivation_essay' maxlength='3000' required></textarea>"
            f"<label for='langs'>{html.escape(CUSTOM_QUESTION)}</label>"
            "<input id='langs' name='programming_languages' required>"
            "<button type='submit'>Save and continue</button></form>"
        )
        return _page("Research Program Application - Step 2 of 3", body)

    @app.post("/apply/step2")
    async def step2_post(
        request: Request, motivation_essay: str = Form(...), programming_languages: str = Form(...)
    ) -> Response:
        STATE.hits["POST /apply/step2"] += 1
        _, draft = session(request)
        if draft is None or "step1" not in draft:
            return RedirectResponse("/apply", status_code=303)
        draft["step2"] = {
            "motivation_essay": motivation_essay,
            "programming_languages": programming_languages,
        }
        return RedirectResponse("/apply/step3", status_code=303)

    @app.get("/apply/step3")
    async def step3(request: Request) -> Response:
        STATE.hits["GET /apply/step3"] += 1
        _, draft = session(request)
        if draft is None or "step2" not in draft:
            return RedirectResponse("/apply", status_code=303)
        body = (
            "<form method='post' action='/apply/submit' enctype='multipart/form-data'>"
            "<label for='cv'>Upload your CV / Resume (PDF)</label>"
            "<input id='cv' name='cv_file' type='file' accept='application/pdf' required>"
            "<button type='submit'>Submit application</button></form>"
        )
        return _page("Research Program Application - Step 3 of 3", body)

    @app.post("/apply/submit")
    async def submit(request: Request, cv_file: UploadFile = File(...)) -> Response:
        STATE.hits["POST /apply/submit"] += 1
        _, draft = session(request)
        data = await cv_file.read()
        STATE.submissions.append({"draft": dict(draft or {}), "cv_bytes": len(data)})
        return _page("Application submitted", "<p>Thank you.</p>")

    @app.get("/official/{slug}")
    async def official(slug: str) -> Response:
        """An official programme page on another host (linked from a listicle hub)."""
        fields = {
            "Title": "Eta Global Research Fellowship",
            "Organization": "Eta Foundation",
            "Type": "RESEARCH",
            "Deadline": "2099-06-01",
            "Degree": "BACHELOR",
            "Apply": "/apply",
        }
        lines = "".join(f"<p>{k}: {v}</p>" for k, v in fields.items())
        prose = "<p>The Eta Foundation funds international undergraduate researchers.</p>" * 3
        return _page(
            fields["Title"], f"<article>{lines}{prose}</article><p>{html.escape(slug)}</p>"
        )

    @app.get("/programme/{slug}")
    async def programme(slug: str, request: Request) -> Response:
        """An overview / directory-style page: the extracted portal URL points HERE, not at the
        form. It carries a newsletter form, a "free counselling" lead form (both must never be
        filled) and an "Apply Now" button that leads to the real 3-step form."""
        STATE.hits["GET /programme"] += 1
        here = str(request.url).split("?")[0]
        facts = {
            "Title": "Zeta Summer Research Programme",
            "Organization": "Zeta Institute",
            "Type": "RESEARCH",
            "Deadline": "2099-07-01",
            "Degree": "BACHELOR",
            "Apply": here,
        }
        lines = "".join(f"<p>{k}: {html.escape(v)}</p>" for k, v in facts.items())
        prose = "<p>The Zeta Institute hosts international undergraduates every summer.</p>" * 3
        body = (
            f"<article>{lines}{prose}"
            "<p><a href='#how'>How to apply</a> &middot; "
            "<a href='https://www.facebook.com/sharer'>Share on Facebook</a></p>"
            "<p><a class='btn' href='/apply?v=2'>Apply Now</a></p></article>"
            "<aside><h2>Get Free Counselling</h2><form method='post' action='/lead'>"
            "<label for='sn'>Full Name *</label><input id='sn' name='StudentName' required>"
            "<label for='mb'>Your contact number*</label><input id='mb' name='Mobile' required>"
            "<button type='submit'>Submit</button></form></aside>"
            # Formless lead widget in the page body, as on real directory sites (studyabroad.pk).
            "<div class='col-lg-3'><div class='side-form'><h3>Study abroad expert advice</h3>"
            "<div class='fieldset'>Full Name * <input id='StudentName2' name='LeadName'></div>"
            "<div class='fieldset'>Your contact number* <input name='LeadMobile'></div>"
            "<button id='submitInquiry' type='button'>Submit Inquiry</button></div></div>"
            "<footer><form class='newsletter' method='post' action='/lead'>"
            "<label for='nl'>Subscribe to our newsletter</label>"
            "<input id='nl' name='newsletter_email' type='email'>"
            "<button type='submit'>Subscribe</button></form></footer>"
        )
        return _page(facts["Title"], body)

    @app.get("/directory/{slug}")
    async def directory(slug: str) -> Response:
        """A scholarship directory listing (like studyabroad.pk/scholarships): a search box and
        a formless "expert advice" inquiry widget with a Submit button - no application at all."""
        STATE.hits["GET /directory"] += 1
        cards = "".join(
            f"<article><h3><a href='/programme/{i}'>Scholarship {i}</a></h3></article>"
            for i in range(3)
        )
        body = (
            "<div class='search-box'><select name='level'><option>Bachelor</option>"
            "<option>Master</option></select><input name='keyword' placeholder='Keyword'>"
            "<button id='searchScholarshipsBtn' type='button'>Search</button></div>"
            f"{cards}"
            "<div class='col-lg-3'><div class='side-form'><h3>Study abroad expert advice</h3>"
            "<div class='fieldset'>Full Name * <input id='StudentName' name='StudentName'></div>"
            "<div class='fieldset'>Your contact number* <input name='Mobile'></div>"
            "<button id='submitInquiry' type='button'>Submit Inquiry</button></div></div>"
        )
        return _page(f"Scholarships directory {html.escape(slug)}", body)

    @app.get("/agg/article/{slug}")
    async def agg_article(slug: str, request: Request, nolink: str = "") -> Response:
        """An aggregator blog post about a university scholarship. It embeds a lookalike
        'application form' whose every keystroke is reported to /agg/keystroke - the filler must
        never type here - and an 'Official Link' (new tab) to the university."""
        STATE.hits["GET /agg/article"] += 1
        official = STATE.official_base
        facts = {
            "Title": "Omega University Scholarship",
            "Organization": "Omega University",
            "Type": "SCHOLARSHIP",
            "Deadline": "2099-08-01",
            "Degree": "BACHELOR",
        }
        lines = "".join(f"<p>{k}: {html.escape(v)}</p>" for k, v in facts.items())
        link = (
            ""
            if nolink
            else "<p><a href='https://ads.example.com/click?gclid=abc' rel='sponsored'>"
            "Apply Now</a></p>"
            f"<p><a href='{official}/uni/overview' target='_blank' rel='noopener'>"
            "Official Link</a></p>"
        )
        body = (
            f"<article>{lines}<p>Omega University offers full scholarships every year.</p>"
            "<p><a href='https://www.facebook.com/sharer/sharer.php'>Share on Facebook</a></p>"
            f"{link}</article>"
            "<section><h2>Scholarship application form</h2>"
            "<form method='post' action='/agg/apply'>"
            "<label for='an'>Full name</label><input id='an' name='applicant_name' required>"
            "<label for='ae'>Email address</label><input id='ae' name='applicant_email' required>"
            "<label for='ap'>Phone</label><input id='ap' name='applicant_phone' required>"
            "<button type='submit'>Submit application</button></form></section>"
            "<script>document.addEventListener('input', () => fetch('/agg/keystroke',"
            " {method: 'POST'}));</script>"
        )
        return _page(facts["Title"] + " - Aggregator Blog", body)

    @app.post("/agg/keystroke")
    async def agg_keystroke() -> Response:
        STATE.hits["POST /agg/keystroke"] += 1
        return JSONResponse({"ok": True})

    @app.get("/uni/overview")
    async def uni_overview() -> Response:
        STATE.hits["GET /uni/overview"] += 1
        body = (
            "<nav><a href='/uni/news'>News</a> <a href='/uni/research'>Research</a></nav>"
            "<article><p>The Omega University Scholarship covers tuition for international "
            "undergraduates.</p><p><a href='/uni/how-to-apply'>How to Apply</a></p></article>"
            "<footer><form class='newsletter'><label for='nl'>Newsletter</label>"
            "<input id='nl' name='newsletter_email'><button>Subscribe</button></form></footer>"
        )
        return _page("Omega University Scholarship", body)

    @app.get("/uni/how-to-apply")
    async def uni_how_to_apply() -> Response:
        STATE.hits["GET /uni/how-to-apply"] += 1
        body = (
            "<ol><li>Check your eligibility.</li><li>Prepare your CV.</li>"
            "<li>Complete the online application.</li></ol>"
            "<button type='button' onclick=\"window.open('/apply?v=2&spa=1', '_blank')\">"
            "Apply Online</button>"
        )
        return _page("How to apply - Omega University", body)

    @app.get("/slate/landing")
    async def slate_landing() -> Response:
        STATE.hits["GET /slate/landing"] += 1
        body = (
            "<div class='login-box'><h2>First-Time Users</h2>"
            "<p>Create an account to start a new application.</p>"
            "<a href='/slate/register'>Create an account</a>"
            "<h2>Returning Users</h2><p>Log in to continue an application in progress.</p>"
            "<a href='/slate/login'>Log in</a></div>"
        )
        return _page("Omega Graduate Application Portal", body)

    @app.get("/slate/login")
    async def slate_login() -> Response:
        STATE.hits["GET /slate/login"] += 1
        body = (
            "<form method='post' action='/slate/login'><label for='e'>Email</label>"
            "<input id='e' name='email' type='email'><label for='pw'>Password</label>"
            "<input id='pw' name='password' type='password'><button>Log in</button></form>"
        )
        return _page("Log in - Omega Graduate Application Portal", body)

    @app.get("/uni/email-apply")
    async def uni_email_apply() -> Response:
        body = (
            "<article><p>There is no online form for the Omega Visiting Fellowship.</p>"
            "<p>To apply, send your CV and a one-page statement of purpose to "
            "<a href='mailto:fellowships@omega.example.edu'>fellowships@omega.example.edu</a>"
            " before 1 August.</p></article>"
        )
        return _page("Omega Visiting Fellowship - How to apply", body)

    @app.get("/uni/closed")
    async def uni_closed() -> Response:
        body = (
            "<article><p>Thank you for your interest in the Omega Summer School.</p>"
            "<p>Applications for the 2099 cycle are now closed. Applications will reopen in "
            "September.</p></article>"
        )
        return _page("Omega Summer School - Apply", body)

    @app.get("/slowpage/{n}")
    async def slowpage(n: int) -> Response:
        """A slow, opportunity-free page: keeps a campaign crawling for a few seconds."""
        STATE.hits["GET /slowpage"] += 1
        await asyncio.sleep(SLOW_PAGE_SECONDS)
        prose = "<p>General news about education policy and campus events.</p>" * 6
        return _page(f"Education news {n}", f"<article>{prose}</article>")

    @app.get("/apply-v2")
    async def apply_v2() -> Response:
        """A one-step portal asking profile v2 questions (links, city, referees)."""
        STATE.hits["GET /apply-v2"] += 1
        body = (
            "<form method='post' action='/apply-v2'>"
            "<label for='li'>LinkedIn profile URL</label><input id='li' name='linkedin' required>"
            "<label for='gh'>GitHub</label><input id='gh' name='github'>"
            "<label for='ct'>City</label><input id='ct' name='city' required>"
            "<label for='r1'>Referee 1 Name</label><input id='r1' name='ref1_name' required>"
            "<label for='r2'>Referee 2 Email</label><input id='r2' name='ref2_email' required>"
            "<label for='en'>English Proficiency (IELTS/TOEFL)</label>"
            "<input id='en' name='english'>"
            "<button type='submit'>Submit application</button></form>"
        )
        return _page("Omega Research Programme Application", body)

    @app.get("/odd/start")
    async def odd_start() -> Response:
        """A portal whose buttons no keyword list understands ("Begin my journey", "Onwards"),
        behind a cookie banner, with an invisible reCAPTCHA v3 script on every page."""
        STATE.hits["GET /odd/start"] += 1
        body = (
            INVISIBLE_RECAPTCHA
            + COOKIE_BANNER
            + "<article><p>Welcome to the Omega Talent Programme.</p></article>"
            "<button type='button' onclick=\"location.href='/odd/form'\">Begin my journey</button>"
        )
        return _page("Omega Talent Programme", body)

    @app.get("/odd/form")
    async def odd_form() -> Response:
        STATE.hits["GET /odd/form"] += 1
        body = (
            INVISIBLE_RECAPTCHA + "<form method='post' action='/odd/form'>"
            "<label for='fn'>Full name</label><input id='fn' name='full_name' required>"
            "<label for='em'>Email address</label><input id='em' name='email' required>"
            "<label for='q'>Which programming languages are you most comfortable with?</label>"
            "<input id='q' name='langs' required>"
            "<button type='submit'>Onwards</button></form>"
        )
        return _page("Omega Talent Programme Application", body)

    @app.post("/odd/form")
    async def odd_form_post() -> Response:
        STATE.hits["POST /odd/form"] += 1
        return RedirectResponse("/odd/review", status_code=303)

    @app.get("/odd/review")
    async def odd_review() -> Response:
        STATE.hits["GET /odd/review"] += 1
        body = (
            "<p>Review your answers before you lodge the application.</p>"
            "<form method='post' action='/odd/lodge'><button type='submit'>Lodge my application"
            "</button></form>"
        )
        return _page("Omega Talent Programme Application - review", body)

    @app.post("/odd/lodge")
    async def odd_lodge() -> Response:
        STATE.hits["POST /odd/lodge"] += 1
        STATE.submissions.append({"draft": {"odd": True}, "cv_bytes": 0})
        return _page("Thank you", "<p>Thank you for your application. It has been received.</p>")

    @app.get("/talent/start")
    async def talent_a() -> Response:
        """Portal A: the entry step is a link whose label no keyword list or fake model knows."""
        STATE.hits["GET /talent/start"] += 1
        body = (
            "<header><nav><a href='/talent/news'>News</a> <a href='/talent/about'>About us</a>"
            "</nav></header><main class='layout-a'>"
            "<p>The Global Talent Programme funds international undergraduate researchers.</p>"
            "<a class='cta-alpha' href='/talent/form'>Commence the process</a></main>"
        )
        return _page("Global Talent Programme", body)

    @app.get("/talent-b/start")
    async def talent_b() -> Response:
        """Portal B (another host): the same step, different markup and CSS classes."""
        STATE.hits["GET /talent-b/start"] += 1
        body = (
            "<div class='shell-b'><section class='intro-b'>"
            "<p>The Global Talent Programme funds international undergraduate researchers.</p>"
            "<button class='btn-zeta' type='button' onclick=\"location.href='/talent-b/form'\">"
            "Commence the process</button></section>"
            "<footer><a href='/talent-b/help'>Help</a></footer></div>"
        )
        return _page("Global Talent Programme", body)

    @app.get("/talent/form")
    @app.get("/talent-b/form")
    async def talent_form() -> Response:
        STATE.hits["GET /talent form"] += 1
        body = (
            "<form method='post' action='/talent/done'>"
            "<label for='tn'>Full name</label><input id='tn' name='full_name' required>"
            "<label for='te'>Email address</label><input id='te' name='email' required>"
            "<label for='tp'>Mobile number</label><input id='tp' name='phone' required>"
            "<button type='submit'>Submit application</button></form>"
        )
        return _page("Global Talent Programme Application", body)

    @app.post("/lead")
    async def lead() -> Response:
        STATE.hits["POST /lead"] += 1
        return _page("Thanks", "<p>We will call you.</p>")

    @app.post("/__wipe")
    async def wipe() -> Response:
        STATE.drafts.clear()
        return JSONResponse({"wiped": True})

    @app.get("/__state")
    async def state() -> Response:
        return JSONResponse(
            {
                "drafts": list(STATE.drafts.values()),
                "submissions": len(STATE.submissions),
                "hits": dict(STATE.hits),
            }
        )

    return app


class FakePortalServer:
    """Serves the portal from a background thread (its own event loop)."""

    def __init__(self) -> None:
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        self.host = f"127.0.0.1:{self.port}"
        self.base = f"http://{self.host}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                build_app(), host="127.0.0.1", port=self.port, log_config=None, lifespan="off"
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.state = STATE
        STATE.official_base = self.base
        # The same app on a second loopback address: tests configure 127.0.0.2 as an
        # aggregator host and 127.0.0.1 as the official programme / university host.
        with socket.socket() as s:
            s.bind(("127.0.0.2", 0))
            self.agg_port = int(s.getsockname()[1])
        self.agg_host = f"127.0.0.2:{self.agg_port}"
        self.agg_base = f"http://{self.agg_host}"
        self.agg_server = uvicorn.Server(
            uvicorn.Config(
                build_app(), host="127.0.0.2", port=self.agg_port, log_config=None, lifespan="off"
            )
        )
        self.agg_thread = threading.Thread(target=self.agg_server.run, daemon=True)
        with socket.socket() as s:
            s.bind(("127.0.0.3", 0))
            self.b_port = int(s.getsockname()[1])
        self.b_host = f"127.0.0.3:{self.b_port}"
        self.b_base = f"http://{self.b_host}"
        self.b_server = uvicorn.Server(
            uvicorn.Config(
                build_app(), host="127.0.0.3", port=self.b_port, log_config=None, lifespan="off"
            )
        )
        self.b_thread = threading.Thread(target=self.b_server.run, daemon=True)

    def start(self) -> FakePortalServer:
        self.thread.start()
        self.agg_thread.start()
        self.b_thread.start()
        import time

        for _ in range(200):
            if self.server.started and self.agg_server.started and self.b_server.started:
                break
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.agg_server.should_exit = True
        self.b_server.should_exit = True
        self.thread.join(timeout=10)
        self.agg_thread.join(timeout=10)
        self.b_thread.join(timeout=10)
