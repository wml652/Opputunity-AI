"""Part 1 (flight recorder, self-healing) and Milestone 4 pure logic."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.application.services.field_mapper import (
    DocumentRef,
    FieldMapper,
    match_option,
    match_profile_key,
)
from app.domain.applications.filling import FieldResolution, FieldSource, FormField, dom_fingerprint
from app.domain.discovery.playbook import (
    DomainPlaybook,
    ExtractionStrategy,
    FetchStrategy,
    Stage,
    StepStatus,
    Trace,
    escalate_extraction,
    escalate_fetch,
    fetch_error_code,
)
from app.domain.essay.synthesis import (
    EssayDraft,
    burstiness_score,
    find_cliches,
    voice_match_score,
    word_limit_ok,
)
from app.domain.memory.models import (
    ConfidenceScore,
    KnowledgeQA,
    MemorySource,
    RecallMatch,
    RecallResult,
)
from app.domain.notification.models import Template, dedupe_key, mask_address, render
from app.domain.notification.signatures import verify_meta_signature, verify_twilio_signature
from app.domain.ports.llm import Completion
from app.domain.shared.enums import DocKind, NotifChannel
from app.infrastructure.extraction.llm_adapters import (
    ExtractionError,
    LlmOpportunityExtractor,
    recover_partial_list,
    split_windows,
)

# ---- flight recorder + self-healing --------------------------------------------------------------


def test_trace_records_timing_status_and_errors() -> None:
    trace = Trace()
    with trace.step(Stage.BROWSER_FETCH, url="https://x.org") as step:
        step.detail["http_status"] = 200
    with pytest.raises(RuntimeError), trace.step(Stage.LLM_EXTRACTION):
        raise RuntimeError("model exploded")
    trace.add(Stage.MEMORY, StepStatus.SKIP, action="skipped_dead_url")
    ok, failed, skipped = trace.steps
    assert ok["status"] == "ok" and ok["detail"]["http_status"] == 200 and ok["duration_ms"] >= 0
    assert failed["status"] == "fail" and "model exploded" in failed["detail"]["error"]
    assert skipped["stage"] == "MEMORY" and skipped["status"] == "skip"


@pytest.mark.parametrize(
    ("status", "error", "html_len", "code"),
    [
        (200, None, 5000, None),
        (403, None, 100, "http_403"),
        (429, None, 0, "http_429"),
        (404, None, 50, "http_404"),
        (None, "TimeoutError: Page.goto: Timeout 25000ms", 0, "timeout"),
        (200, None, 50, "empty_content"),
        (None, "SSRF guard: blocked", 0, "ssrf_blocked"),
        (None, "dns_error: host does not resolve", 0, "dns_error"),
        (None, "tunnel_connect_error: upstream unreachable", 0, "tunnel_connect_error"),
        (None, "Error: net::ERR_TUNNEL_CONNECTION_FAILED at https://x", 0, "tunnel_connect_error"),
        (403, "bot_challenge: cloudflare interstitial", 5000, "bot_challenge"),
    ],
)
def test_fetch_error_codes(
    status: int | None, error: str | None, html_len: int, code: str | None
) -> None:
    assert fetch_error_code(status, error, html_len) == code


def test_fetch_escalation_paths() -> None:
    base = FetchStrategy()
    slow = escalate_fetch(base, "timeout")
    assert (
        slow is not None
        and slow.wait_until == "load"
        and slow.timeout_ms == 45_000
        and slow.level == 1
    )
    limited = escalate_fetch(base, "http_429")
    assert limited is not None and limited.backoff_s >= 5
    blocked = escalate_fetch(base, "http_403")
    assert blocked is not None and blocked.mode == "http"
    assert escalate_fetch(blocked, "http_403").mode == "browser"  # type: ignore[union-attr]
    js = escalate_fetch(base, "empty_content")
    assert js is not None and js.wait_until == "networkidle"
    assert escalate_fetch(base, "http_404") is None  # permanent: retrying cannot help
    assert escalate_fetch(FetchStrategy(level=3), "timeout") is None  # bounded


def test_extraction_escalation_shrinks_windows_and_grows_budget() -> None:
    first = escalate_extraction(ExtractionStrategy())
    assert first is not None and first.chunk_chars == 3_000 and first.max_tokens == 16_384
    second = escalate_extraction(first)
    assert second is not None and second.chunk_chars == 2_500
    assert escalate_extraction(second) is None


def test_playbook_records_failures_and_dead_urls() -> None:
    pb = DomainPlaybook.new(uuid.uuid4(), "example.org", datetime.now(UTC))
    cid = uuid.uuid4()
    pb.record_failure(Stage.BROWSER_FETCH, "http_403", cid)
    pb.record_failure(Stage.BROWSER_FETCH, "http_403", cid)
    pb.mark_dead("https://example.org/gone", "http_404", cid, datetime.now(UTC))
    assert pb.failures == {"BROWSER_FETCH:http_403": 2}
    assert pb.dead_reason("https://example.org/gone")["code"] == "http_404"  # type: ignore[index]
    assert pb.last_campaign_id == cid


def test_split_windows_paragraph_aligned_with_overlap() -> None:
    text = "\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(30))  # ~15k chars
    windows = split_windows(text, 6_000, 400)
    assert len(windows) >= 3 and all(len(w) <= 6_400 + 600 for w in windows)
    assert windows[0].startswith("Paragraph 0") and "Paragraph 29" in windows[-1]
    assert split_windows("short", 6_000, 400) == ["short"]


def test_recover_partial_list_from_truncated_reply() -> None:
    truncated = (
        '```json\n{"opportunities": [{"title": "Alpha Grant", "type": "SCHOLARSHIP"}, '
        '{"title": "Beta Award", "summary": "x"}, {"title": "Gamma Fel'
    )
    items = recover_partial_list(truncated, "opportunities")
    assert [i["title"] for i in items] == ["Alpha Grant", "Beta Award"]
    assert recover_partial_list("nothing here", "opportunities") == []


class WindowLLM:
    """Answers per window; windows over 6,500 chars come back truncated (finish_reason=length)."""

    model = "user/model"

    def __init__(self) -> None:
        self.windows: list[int] = []

    async def complete(
        self, messages: Any, *, max_tokens: int = 0, temperature: float = 0
    ) -> Completion:
        doc = messages[1].content
        self.windows.append(len(doc))
        if len(doc) > 6_500:
            return Completion(
                '{"opportunities": [{"title": "Trunc',
                self.model,
                None,
                None,
                finish_reason="length",
            )
        found = [{"title": t} for t in ("Omega Long-Read Fellowship", "Sigma Grant") if t in doc]
        return Completion(
            json.dumps({"opportunities": found}), self.model, None, None, finish_reason="stop"
        )


async def test_14k_page_is_chunked_and_extracted() -> None:
    filler = "\n\n".join("General scholarship advice paragraph. " * 8 for _ in range(30))
    page = (
        filler
        + "\n\nOmega Long-Read Fellowship opens now.\n\n"
        + filler
        + "\n\nSigma Grant is available.\n\n"
    )
    assert len(page) > 14_000
    llm = WindowLLM()
    result = await LlmOpportunityExtractor().extract(llm, page, "https://blog.example/post")  # type: ignore[arg-type]
    titles = sorted(e.title for e in result.extracts)
    assert titles == ["Omega Long-Read Fellowship", "Sigma Grant"]
    assert result.diagnostics["chunked"] is True and result.diagnostics["chunks"] >= 3
    assert result.diagnostics["failed_windows"] == 0
    assert all(n < 6_500 for n in llm.windows)


async def test_unchunked_truncation_reports_finish_reason() -> None:
    llm = WindowLLM()
    with pytest.raises(ExtractionError) as info:
        await LlmOpportunityExtractor().extract(  # type: ignore[arg-type]
            llm, "x" * 8_000, "https://blog.example/post", ExtractionStrategy(chunk_chars=20_000)
        )
    assert "finish_reason=length" in info.value.message
    assert info.value.diagnostics["attempts"][0]["finish_reason"] == "length"


# ---- field mapper -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Full name", "full_name"),
        ("First name", "first_name"),
        ("Family name / Surname", "last_name"),
        ("Email address", "email"),
        ("Mobile number (WhatsApp)", "phone"),
        ("Cumulative GPA", "gpa"),
        ("Current level of study", "degree_level"),
        ("Country of residence", "country_of_residence"),
        (
            "Which programming languages are you most comfortable with?",
            None,
        ),  # not spoken languages!
        ("Languages you speak", "languages"),
        ("Why do you want to join?", None),
    ],
)
def test_profile_synonyms(label: str, key: str | None) -> None:
    assert match_profile_key(label) == key


def test_option_matching_for_degree_and_free_text() -> None:
    options = ["Undergraduate (Bachelor)", "Postgraduate"]
    assert match_option("BACHELOR", options, "degree_level") == "Undergraduate (Bachelor)"
    assert match_option("Postgraduate", options) == "Postgraduate"
    assert match_option("PhD", options, "degree_level") is None  # never invent an option


def _recall(similarity: float, answer: str) -> RecallResult:
    now = datetime.now(UTC)
    qa = KnowledgeQA(
        uuid.uuid4(), uuid.uuid4(), "Q?", "q", answer, "m", MemorySource.USER, 0, None, now, now
    )
    return RecallResult(
        "q", "m", 0.85, [RecallMatch(qa, ConfidenceScore.from_cosine(similarity))], 1, 0
    )


async def test_field_mapper_priority_and_threshold() -> None:
    essays: list[str] = []

    async def essay(f: FormField) -> EssayDraft:
        essays.append(f.key)
        return EssayDraft(uuid.uuid4(), uuid.uuid4(), None, None, f.key, f.label, None, "My essay")

    sims = {
        "Which programming languages are you most comfortable with?": (0.91, "Python, Go"),
        "Unknown thing?": (0.62, "maybe"),
    }

    async def recall(question: str) -> RecallResult:
        sim, ans = sims.get(question, (0.0, ""))
        return _recall(sim, ans) if sim else RecallResult(question, "m", 0.85, [], 0, 0)

    cv = DocumentRef(uuid.uuid4(), DocKind.CV, "cv.pdf")
    mapper = FieldMapper(
        facts={"full_name": "Ayesha Khan", "degree_level": "BACHELOR"},
        documents=[cv],
        recall=recall,
        essay=essay,
        known={"nickname": FieldResolution("nickname", "Ash", FieldSource.USER, 1.0)},
    )
    name = await mapper.resolve(FormField("full_name", "Full name", "text", "[name=full_name]"))
    assert (name.value, name.source, name.fillable) == ("Ayesha Khan", FieldSource.PROFILE, True)
    level = await mapper.resolve(
        FormField("level", "Level of study", "select", "s", ["Undergraduate (Bachelor)"])
    )
    assert level.value == "Undergraduate (Bachelor)"
    upload = await mapper.resolve(FormField("cv_file", "Upload your CV / Resume", "file", "f"))
    assert upload.source is FieldSource.DOCUMENT and upload.document_id == cv.document_id
    text = await mapper.resolve(
        FormField("essay", "Why do you want to join? (essay)", "textarea", "t", max_length=3000)
    )
    assert text.source is FieldSource.ESSAY and essays == ["essay"]
    langs = await mapper.resolve(
        FormField("pl", "Which programming languages are you most comfortable with?", "text", "p")
    )
    assert (langs.value, langs.source, round(langs.confidence, 2)) == (
        "Python, Go",
        FieldSource.MEMORY,
        0.91,
    )
    unsure = await mapper.resolve(FormField("u", "Unknown thing?", "text", "u", required=True))
    assert unsure.value is None and not unsure.fillable and unsure.suggestion == "maybe"
    settled = await mapper.resolve(FormField("nickname", "Nickname", "text", "n"))
    assert settled.value == "Ash" and settled.source is FieldSource.USER


def test_dom_fingerprint_ignores_order_and_values() -> None:
    a = [FormField("x", "X", "text", "s"), FormField("y", "Y", "email", "s")]
    assert dom_fingerprint(a) == dom_fingerprint(list(reversed(a)))
    assert dom_fingerprint(a) != dom_fingerprint([*a, FormField("z", "Z", "text", "s")])


# ---- essays -------------------------------------------------------------------------------------


def test_voice_match_and_burstiness_scores() -> None:
    v = [0.3, 0.5, 0.2, 0.9]
    assert voice_match_score(v, v) == 1.0
    assert voice_match_score(v, [0.0, 0.0, 0.0, 0.0]) == 0.0
    assert 0.0 < voice_match_score(v, [0.3, 0.1, 0.9, 0.1]) < 1.0
    assert burstiness_score(0.5, 0.5) == 1.0
    assert burstiness_score(0.5, 0.25) == 0.5  # 1 - |0.25-0.5|/0.5
    assert burstiness_score(0.5, 1.5) == 0.0  # clamped
    assert burstiness_score(0.0, 0.4) == 0.0


def test_cliche_scan_and_word_limit() -> None:
    text = "I want to delve into a rich tapestry; it is a testament to my unwavering resolve. Moreover..."
    assert {"delve", "tapestry", "testament to", "unwavering", "moreover"} <= set(
        find_cliches(text)
    )
    assert find_cliches("Plain, specific writing about a solar charger I built.") == []
    assert word_limit_ok("one two three", 3) and not word_limit_ok("one two three four", 3)


# ---- notifications ------------------------------------------------------------------------------


def test_render_contains_no_pii_and_links_to_console() -> None:
    msg = render(
        Template.QUESTION_NEEDED,
        {"title": "Alpha Grant", "questions": 2, "path": "/applications/x"},
        "http://localhost:3000",
    )
    assert "Alpha Grant" in msg.subject and "http://localhost:3000/applications/x" in msg.text
    assert "2 answer(s)" in msg.text
    ready = render(Template.READY_FOR_OK, {"title": "Alpha"}, "http://c")
    assert "Nothing has been submitted" in ready.text


def test_masking_and_dedupe_keys() -> None:
    assert mask_address(NotifChannel.EMAIL, "ayesha@example.com") == "ay***@example.com"
    assert mask_address(NotifChannel.WHATSAPP, "+923001234567") == "+***4567"
    app = uuid.uuid4()
    assert dedupe_key(Template.READY_FOR_OK, app, "READY:1") == f"ready_for_ok:{app}:READY:1"


def test_twilio_signature() -> None:
    token, url = "twilio-auth-token", "https://api.example.com/api/v1/webhooks/twilio"
    params = {"MessageSid": "SM1", "MessageStatus": "delivered"}
    payload = url + "MessageSidSM1MessageStatusdelivered"
    good = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    assert verify_twilio_signature(token, url, params, good)
    assert not verify_twilio_signature(token, url, {**params, "MessageStatus": "failed"}, good)
    assert not verify_twilio_signature(token, url, params, "")


def test_meta_signature() -> None:
    body = b'{"entry": []}'
    good = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature("app-secret", body, good)
    assert not verify_meta_signature("app-secret", body + b" ", good)
    assert not verify_meta_signature("other", body, good)
