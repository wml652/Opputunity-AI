"""A realistic 3-step application portal for form-filler tests.

  /apply            step 1: name, email, phone, GPA, university, degree level (select)
  /apply/step2      step 2: essay textarea + a CUSTOM question no profile field answers
  /apply/step3      step 3: CV upload (PDF) + the final "Submit application" button
  POST /__wipe      drop every server-side draft (simulates an expired portal session)
  GET  /__state     inspect drafts, submissions and request counters

Drafts live server-side keyed by a session cookie, so resuming requires the browser's saved
storage_state; after /__wipe the portal redirects to step 1 and the filler must replay.
`?v=2` serves the same form under a different opportunity (same custom question).
"""

from __future__ import annotations

import html
import threading
import uuid
from collections import Counter
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

CUSTOM_QUESTION = "Which programming languages are you most comfortable with?"
ESSAY_LABEL = "Why do you want to join this research program? (essay)"


class PortalState:
    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, Any]] = {}
        self.submissions: list[dict[str, Any]] = []
        self.hits: Counter[str] = Counter()
        self.lock = threading.Lock()

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

    def start(self) -> FakePortalServer:
        self.thread.start()
        import time

        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
