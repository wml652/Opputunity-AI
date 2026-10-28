"""Milestone 2 integration: profile PII encryption + RLS, encrypted document vault, reference
essays + versioned fingerprints, and pgvector semantic recall at the 0.85 threshold."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest
import respx

from app.bootstrap.container import Infrastructure
from app.infrastructure.llm.embeddings import EmbeddingProviderFactory
from tests.conftest import ApiUser, DatabaseUrls, register
from tests.fixtures import ControlledEmbeddingFactory, at_cosine, make_pdf, unit

PHONE = "+92-300-5550199"
NATIONAL_ID = "35202-9876543-1"
ADDRESS = "House 12, Street 4, Lahore"

PROFILE = {
    "full_name": "Ayesha Khan",
    "pii": {
        "home_address": ADDRESS,
        "phone": PHONE,
        "date_of_birth": "2004-05-17",
        "national_id": NATIONAL_ID,
    },
    "degree_level": "BACHELOR",
    "major": "Computer Science",
    "institution": "LUMS",
    "gpa": 3.62,
    "gpa_scale": 4,
    "nationality": "Pakistan",
    "country_of_residence": "Pakistan",
    "graduation_year": 2027,
    "skills": ["Python", "python", "Machine Learning"],
    "languages": [{"name": "English", "level": "C1"}, {"name": "Urdu", "level": "Native"}],
    "experiences": [
        {
            "title": "Research Intern",
            "organization": "NLP Lab",
            "start": "2025-06",
            "end": "2025-08",
            "description": "Built evaluation tooling.",
        }
    ],
}


async def _owner_fetch(urls: DatabaseUrls, sql: str, *args: object) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(urls.owner)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


# ---- profile ------------------------------------------------------------------------------------


async def test_profile_round_trip_with_encrypted_pii(user: ApiUser, clean_db: DatabaseUrls) -> None:
    assert (await user.get("/api/v1/profile")).status_code == 404
    saved = await user.client.put(
        "/api/v1/profile", json=PROFILE, headers={"x-csrf-token": user.csrf}
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["pii"]["phone"] == PHONE and body["pii_encrypted"] is True
    assert body["pii_key_version"] == 1
    assert body["gpa_normalized_4"] == 3.62
    assert body["skills"] == ["Python", "Machine Learning"]  # de-duplicated
    again = (await user.get("/api/v1/profile")).json()
    assert again["pii"]["national_id"] == NATIONAL_ID
    assert again["experiences"][0]["organization"] == "NLP Lab"

    rows = await _owner_fetch(clean_db, "SELECT * FROM student_profiles")
    raw = bytes(rows[0]["pii_ciphertext"])
    for secret in (PHONE, NATIONAL_ID, ADDRESS, "2004-05-17"):
        assert secret.encode() not in raw
    # PII never appears in plaintext columns, events, or the audit trail.
    dump = json.dumps([dict(r) for r in rows], default=str)
    events = await _owner_fetch(clean_db, "SELECT payload::text AS p FROM pipeline_events")
    audit = await _owner_fetch(clean_db, "SELECT metadata::text AS m FROM audit_log")
    for blob in (dump, json.dumps([r["p"] for r in events]), json.dumps([r["m"] for r in audit])):
        assert PHONE not in blob and NATIONAL_ID not in blob


async def test_pii_ciphertext_is_row_bound(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient, clean_db: DatabaseUrls
) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(other_client, "bob@example.com")
    for u in (alice, bob):
        r = await u.client.put("/api/v1/profile", json=PROFILE, headers={"x-csrf-token": u.csrf})
        assert r.status_code == 200
    # An attacker with DB write access copies Alice's ciphertext into Bob's row.
    conn = await asyncpg.connect(clean_db.owner)
    try:
        await conn.execute(
            """UPDATE student_profiles b SET pii_ciphertext = a.pii_ciphertext,
                   pii_nonce = a.pii_nonce
               FROM student_profiles a WHERE a.user_id = $1 AND b.user_id = $2""",
            alice.id,
            bob.id,
        )
    finally:
        await conn.close()
    stolen = await bob.get("/api/v1/profile")
    assert stolen.status_code == 500  # AAD mismatch -> authentication failure, no plaintext
    assert PHONE not in stolen.text


async def test_profile_validation(user: ApiUser) -> None:
    bad = {**PROFILE, "gpa": 4.5, "gpa_scale": 4}
    r = await user.client.put("/api/v1/profile", json=bad, headers={"x-csrf-token": user.csrf})
    assert r.status_code == 422


async def test_profiles_are_tenant_isolated(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(other_client, "bob@example.com")
    await alice.client.put("/api/v1/profile", json=PROFILE, headers={"x-csrf-token": alice.csrf})
    assert (await bob.get("/api/v1/profile")).status_code == 404


# ---- documents ----------------------------------------------------------------------------------


async def _upload(
    u: ApiUser, data: bytes, kind: str = "CV", name: str = "cv.pdf"
) -> httpx.Response:
    return await u.client.post(
        "/api/v1/documents",
        files={"file": (name, data, "application/pdf")},
        data={"kind": kind},
        headers={"x-csrf-token": u.csrf},
    )


async def test_document_upload_encrypts_blob_and_text(
    user: ApiUser, infra: Infrastructure, clean_db: DatabaseUrls
) -> None:
    pdf = make_pdf(["Ayesha Khan - Curriculum Vitae", f"Phone {PHONE}"])
    resp = await _upload(user, pdf)
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["kind"] == "CV" and doc["page_count"] == 1 and doc["size_bytes"] == len(pdf)
    assert len(doc["sha256"]) == 64 and doc["text_chars"] > 0

    preview = (await user.get(f"/api/v1/documents/{doc['id']}")).json()
    assert "Curriculum Vitae" in preview["text"]

    blob_path = Path(infra.settings.storage_dir) / f"{user.id}/{doc['id']}.bin"
    stored = blob_path.read_bytes()
    assert not stored.startswith(b"%PDF-") and b"Curriculum" not in stored
    rows = await _owner_fetch(clean_db, "SELECT text_ciphertext FROM documents")
    assert PHONE.encode() not in bytes(rows[0]["text_ciphertext"])

    download = await user.get(f"/api/v1/documents/{doc['id']}/download")
    assert download.status_code == 200 and download.content == pdf
    assert download.headers["content-type"] == "application/pdf"

    dup = await _upload(user, pdf, name="copy.pdf")
    assert dup.status_code == 409

    assert (await user.delete(f"/api/v1/documents/{doc['id']}")).status_code == 204
    assert not blob_path.exists()
    assert (await user.get("/api/v1/documents")).json() == []


async def test_spoofed_and_oversized_uploads_rejected(user: ApiUser) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    spoofed = await _upload(user, png, name="scan.pdf")
    assert spoofed.status_code == 422 and spoofed.json()["error"]["code"] == "unsupported_file"
    corrupt = await _upload(user, b"%PDF-1.4 garbage that is not a pdf")
    assert corrupt.status_code == 422
    huge = await _upload(user, b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024 + 1))
    assert huge.status_code == 413
    assert (await user.get("/api/v1/documents")).json() == []


async def test_documents_are_tenant_isolated(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(other_client, "bob@example.com")
    doc = (await _upload(alice, make_pdf(["private transcript"]), kind="TRANSCRIPT")).json()
    assert (await bob.get("/api/v1/documents")).json() == []
    assert (await bob.get(f"/api/v1/documents/{doc['id']}")).status_code == 404
    assert (await bob.get(f"/api/v1/documents/{doc['id']}/download")).status_code == 404
    assert (await bob.delete(f"/api/v1/documents/{doc['id']}")).status_code == 404


# ---- essays + fingerprint -----------------------------------------------------------------------

ESSAY = (
    "I grew up in a house where the radio was always on. My father fixed them for neighbours, "
    "and I learned to listen before I learned to read. However, listening was not enough.\n\n"
    "At fifteen I built my first receiver. It failed. I rebuilt it twice; the third version "
    "picked up a station from Karachi, faint but real. That night taught me patience.\n\n"
    "Today I study computer science because it feels like the same work: tracing a signal "
    "through noise until it becomes something a person can use. As a result, I want research."
)


async def test_essays_version_fingerprint_and_embed_chunks(
    user: ApiUser, clean_db: DatabaseUrls, embedding_factory: ControlledEmbeddingFactory
) -> None:
    status = (await user.get("/api/v1/essays/fingerprint")).json()
    assert status["fingerprint"] is None and status["max_essays"] == 5

    created = await user.post(
        "/api/v1/essays", json={"title": "Why CS", "body": ESSAY, "prompt_context": "SoP"}
    )
    assert created.status_code == 201, created.text
    fp1 = created.json()["fingerprint"]
    assert fp1["version"] == 1 and fp1["essay_count"] == 1
    s = fp1["metrics"]["sentence_length"]
    assert s["burstiness"] == pytest.approx(s["std"] / s["mean"], abs=1e-3)
    assert fp1["metrics"]["lexical"]["mtld"] > 0
    assert any(t["phrase"] == "however" for t in fp1["metrics"]["transitions"]["top"])

    second = await user.post("/api/v1/essays", json={"title": "Second", "body": ESSAY + " Again."})
    assert second.json()["fingerprint"]["version"] == 2
    assert second.json()["fingerprint"]["essay_count"] == 2

    rows = await _owner_fetch(
        clean_db,
        "SELECT count(*) AS n, min(vector_dims(embedding)) AS d, min(embedding_model) AS m "
        "FROM essay_chunks",
    )
    assert rows[0]["n"] >= 2 and rows[0]["d"] == 1024
    assert rows[0]["m"] == "test/controlled-1024"

    essay_id = created.json()["essay"]["id"]
    deleted = await user.delete(f"/api/v1/essays/{essay_id}")
    assert deleted.json()["fingerprint"]["version"] == 3
    status = (await user.get("/api/v1/essays/fingerprint")).json()
    assert status["essay_count"] == 1 and status["fingerprint"]["version"] == 3
    chunks = await _owner_fetch(
        clean_db, "SELECT count(*) AS n FROM essay_chunks WHERE essay_id = $1", uuid.UUID(essay_id)
    )
    assert chunks[0]["n"] == 0  # cascade


async def test_essay_limits(user: ApiUser) -> None:
    short = await user.post("/api/v1/essays", json={"title": "Short", "body": "Too short."})
    assert short.status_code == 422
    for i in range(5):
        r = await user.post("/api/v1/essays", json={"title": f"E{i}", "body": ESSAY + f" v{i}."})
        assert r.status_code == 201
    sixth = await user.post("/api/v1/essays", json={"title": "E6", "body": ESSAY})
    assert sixth.status_code == 409


# ---- semantic memory ----------------------------------------------------------------------------

BASE_Q = "What is your intended field of study?"


async def test_recall_threshold_with_pgvector(
    user: ApiUser, embedding_factory: ControlledEmbeddingFactory
) -> None:
    embedding_factory.overrides.update(
        {
            BASE_Q: unit((0, 1.0)),
            "q-0.95": at_cosine(0.95),
            "q-0.86": at_cosine(0.86),
            "q-0.84": at_cosine(0.84),
            "q-0.30": at_cosine(0.30),
            "Unrelated: describe a failure.": unit((7, 1.0)),
        }
    )
    learned = await user.post(
        "/api/v1/memory", json={"question": BASE_Q, "answer": "Computer Science"}
    )
    assert learned.status_code == 201, learned.text
    await user.post(
        "/api/v1/memory", json={"question": "Unrelated: describe a failure.", "answer": "..."}
    )

    async def recall(q: str) -> dict[str, object]:
        r = await user.post("/api/v1/memory/recall", json={"question": q, "k": 3})
        assert r.status_code == 200, r.text
        data: dict[str, object] = r.json()
        return data

    exact = await recall(BASE_Q)
    assert exact["auto_fill"] is True
    assert exact["best"]["similarity"] == pytest.approx(1.0, abs=1e-4)  # type: ignore[index]
    assert exact["best"]["memory"]["answer"] == "Computer Science"  # type: ignore[index]

    for q, expected_sim, auto in (
        ("q-0.95", 0.95, True),
        ("q-0.86", 0.86, True),
        ("q-0.84", 0.84, False),
        ("q-0.30", 0.30, False),
    ):
        result = await recall(q)
        best = result["best"]
        assert best["similarity"] == pytest.approx(expected_sim, abs=1e-3), q  # type: ignore[index]
        assert result["auto_fill"] is auto, q
        assert result["threshold"] == 0.85
    ranked = (await recall("q-0.95"))["matches"]
    assert next(m["memory"]["question"] for m in ranked) == BASE_Q  # type: ignore[index]


async def test_recall_never_crosses_tenants(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    alice = await register(client, "alice@example.com")
    bob = await register(other_client, "bob@example.com")
    await alice.post("/api/v1/memory", json={"question": BASE_Q, "answer": "Alice's secret"})
    result = (await bob.post("/api/v1/memory/recall", json={"question": BASE_Q})).json()
    assert result["matches"] == [] and result["auto_fill"] is False
    assert (await bob.get("/api/v1/memory")).json()["total"] == 0


async def test_memory_crud_dedupe_and_reembed(
    user: ApiUser, embedding_factory: ControlledEmbeddingFactory, clean_db: DatabaseUrls
) -> None:
    created = (await user.post("/api/v1/memory", json={"question": BASE_Q, "answer": "CS"})).json()
    dup = await user.post(
        "/api/v1/memory",
        json={"question": "  what is your INTENDED field of study ", "answer": "x"},
    )
    assert dup.status_code == 409
    listing = (await user.get("/api/v1/memory", params={"q": "field"})).json()
    assert listing["total"] == 1
    assert (await user.get("/api/v1/memory", params={"q": "nothing-matches"})).json()["total"] == 0

    calls_before = embedding_factory.calls
    patched = await user.patch(f"/api/v1/memory/{created['id']}", json={"answer": "Physics"})
    assert patched.json()["answer"] == "Physics"
    assert embedding_factory.calls == calls_before  # answer-only edit: no re-embedding
    await user.patch(f"/api/v1/memory/{created['id']}", json={"question": "Which major?"})
    assert embedding_factory.calls == calls_before + 1
    rec = (await user.post("/api/v1/memory/recall", json={"question": "Which major?"})).json()
    assert rec["auto_fill"] is True and rec["best"]["memory"]["answer"] == "Physics"

    assert (await user.delete(f"/api/v1/memory/{created['id']}")).status_code == 204
    assert (await user.get("/api/v1/memory")).json()["total"] == 0


async def test_vault_tables_have_rls_and_hnsw(clean_db: DatabaseUrls) -> None:
    tables = (
        "student_profiles",
        "documents",
        "reference_essays",
        "essay_chunks",
        "style_fingerprints",
        "knowledge_memory_qa",
    )
    rows = await _owner_fetch(
        clean_db,
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY($1)",
        list(tables),
    )
    assert {r["relname"] for r in rows} == set(tables)
    assert all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows)
    indexes = await _owner_fetch(
        clean_db, "SELECT indexname, indexdef FROM pg_indexes WHERE indexdef ILIKE '%hnsw%'"
    )
    defs = {r["indexname"]: r["indexdef"] for r in indexes}
    assert "vector_cosine_ops" in defs["ix_knowledge_memory_qa_embedding_hnsw"]
    assert "vector_cosine_ops" in defs["ix_essay_chunks_embedding_hnsw"]


async def test_rls_blocks_cross_tenant_vector_reads(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient, infra: Infrastructure
) -> None:
    from sqlalchemy import text

    alice = await register(client, "alice@example.com")
    bob = await register(other_client, "bob@example.com")
    await alice.post("/api/v1/memory", json={"question": BASE_Q, "answer": "secret"})
    async with infra.engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(bob.id)})
        leaked = await conn.execute(
            text("SELECT count(*) FROM knowledge_memory_qa WHERE user_id = :a"), {"a": alice.id}
        )
        assert leaked.scalar() == 0


@respx.mock
async def test_real_nim_embedding_path_uses_passage_and_query(
    user: ApiUser, infra: Infrastructure
) -> None:
    """With an NVIDIA NIM provider configured, memory embeds questions symmetrically
    (`input_type=query` when learning AND recalling) at `dimensions=1024`; essay chunks are
    indexed as passages."""
    infra.embedding_factory = EmbeddingProviderFactory(  # type: ignore[assignment]
        http_client=infra.llm_factory.http_client, timeout=2
    )
    # Rebuild the app container so the use cases see the real factory.
    from app.bootstrap.container import build_api_container

    user.client._transport.app.state.container = build_api_container(infra)  # type: ignore[attr-defined]
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": unit((3, 1.0))}], "model": body["model"]}
        )

    respx.post("https://integrate.api.nvidia.com/v1/embeddings").mock(side_effect=handler)
    created = await user.post(
        "/api/v1/llm/providers",
        json={
            "kind": "NVIDIA_NIM",
            "model": "test/chat-model",
            "embedding_model": "user/embedder-1024",
            "api_key": "nvapi-test-key-000000000000",
        },
    )
    assert created.status_code == 201
    learned = await user.post("/api/v1/memory", json={"question": BASE_Q, "answer": "CS"})
    assert learned.json()["embedding_model"] == "user/embedder-1024"
    rec = (await user.post("/api/v1/memory/recall", json={"question": BASE_Q})).json()
    assert rec["embedding_model"] == "user/embedder-1024"
    assert rec["auto_fill"] is True
    assert [c["input_type"] for c in calls] == ["query", "query"]
    assert all(c["dimensions"] == 1024 for c in calls)
    essay = await user.post("/api/v1/essays", json={"title": "Why CS", "body": ESSAY})
    assert essay.status_code == 201
    assert calls[-1]["input_type"] == "passage"
