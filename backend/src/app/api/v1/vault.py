"""Student vault routes: profile, documents, reference essays + fingerprint, semantic memory."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from app.api.deps import Container, CurrentUserId
from app.api.v1.vault_schemas import (
    DocumentOut,
    DocumentTextOut,
    EssayIn,
    EssayMutationOut,
    EssayOut,
    FingerprintOut,
    FingerprintStatusOut,
    MemoryIn,
    MemoryOut,
    MemoryPageOut,
    MemoryPatch,
    ProfileIn,
    ProfileOut,
    RecallIn,
    RecallOut,
)
from app.application.essay.use_cases import EssayInput
from app.application.profile.use_cases import ProfileInput
from app.domain.profile.documents import MAX_DOCUMENT_BYTES, FileTooLargeError
from app.domain.profile.models import Experience, Language, ProfileDetails, StudentPII
from app.domain.shared.enums import DocKind
from app.domain.shared.errors import NotFoundError

router = APIRouter(tags=["vault"])

TEXT_PREVIEW_CHARS = 20_000

# ---- profile ------------------------------------------------------------------------------------


@router.get("/profile", response_model=ProfileOut)
async def get_profile(user_id: CurrentUserId, container: Container) -> ProfileOut:
    view = await container.vault.get_profile.execute(user_id)
    if view is None:
        raise NotFoundError("no profile yet")
    return ProfileOut.of(view)


@router.put("/profile", response_model=ProfileOut)
async def save_profile(body: ProfileIn, user_id: CurrentUserId, container: Container) -> ProfileOut:
    view = await container.vault.save_profile.execute(
        user_id,
        ProfileInput(
            full_name=body.full_name,
            pii=StudentPII.from_dict(body.pii.flat()),
            degree_level=body.degree_level,
            major=body.major,
            institution=body.institution,
            gpa=body.gpa,
            gpa_scale=body.gpa_scale,
            nationality=body.nationality,
            country_of_residence=body.country_of_residence,
            graduation_year=body.graduation_year,
            skills=body.skills,
            languages=[Language(name=lang.name, level=lang.level) for lang in body.languages],
            experiences=[Experience(**e.model_dump()) for e in body.experiences],
            details=ProfileDetails.from_dict(body.details.model_dump(mode="json")),
        ),
    )
    return ProfileOut.of(view)


# ---- documents ----------------------------------------------------------------------------------


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise FileTooLargeError(f"file exceeds {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    user_id: CurrentUserId,
    container: Container,
    file: UploadFile = File(...),
    kind: DocKind = Form(DocKind.OTHER),
) -> DocumentOut:
    data = await _read_limited(file, MAX_DOCUMENT_BYTES)
    document = await container.vault.upload_document.execute(
        user_id, kind=kind, filename=file.filename or "document.pdf", data=data
    )
    return DocumentOut.of(document)


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(user_id: CurrentUserId, container: Container) -> list[DocumentOut]:
    return [DocumentOut.of(d) for d in await container.vault.list_documents.execute(user_id)]


@router.get("/documents/{document_id}", response_model=DocumentTextOut)
async def document_text(
    document_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> DocumentTextOut:
    document, text = await container.vault.document_text.execute(user_id, document_id)
    return DocumentTextOut(
        document=DocumentOut.of(document),
        text=text[:TEXT_PREVIEW_CHARS],
        truncated=len(text) > TEXT_PREVIEW_CHARS,
    )


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    document, data = await container.vault.download_document.execute(user_id, document_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.filename)}",
            "Cache-Control": "no-store",
        },
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> Response:
    await container.vault.delete_document.execute(user_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- essays -------------------------------------------------------------------------------------


@router.get("/essays", response_model=list[EssayOut])
async def list_essays(user_id: CurrentUserId, container: Container) -> list[EssayOut]:
    return [EssayOut.of(e) for e in await container.vault.list_essays.execute(user_id)]


@router.get("/essays/fingerprint", response_model=FingerprintStatusOut)
async def fingerprint(user_id: CurrentUserId, container: Container) -> FingerprintStatusOut:
    return FingerprintStatusOut.of(await container.vault.get_fingerprint.execute(user_id))


@router.post("/essays", response_model=EssayMutationOut, status_code=status.HTTP_201_CREATED)
async def create_essay(
    body: EssayIn, user_id: CurrentUserId, container: Container
) -> EssayMutationOut:
    essay, fp = await container.vault.create_essay.execute(
        user_id, EssayInput(title=body.title, body=body.body, prompt_context=body.prompt_context)
    )
    return EssayMutationOut(
        essay=EssayOut.of(essay), fingerprint=FingerprintOut.of(fp) if fp else None
    )


@router.put("/essays/{essay_id}", response_model=EssayMutationOut)
async def update_essay(
    essay_id: uuid.UUID, body: EssayIn, user_id: CurrentUserId, container: Container
) -> EssayMutationOut:
    essay, fp = await container.vault.update_essay.execute(
        user_id,
        essay_id,
        EssayInput(title=body.title, body=body.body, prompt_context=body.prompt_context),
    )
    return EssayMutationOut(
        essay=EssayOut.of(essay), fingerprint=FingerprintOut.of(fp) if fp else None
    )


@router.delete("/essays/{essay_id}", response_model=EssayMutationOut)
async def delete_essay(
    essay_id: uuid.UUID, user_id: CurrentUserId, container: Container
) -> EssayMutationOut:
    fp = await container.vault.delete_essay.execute(user_id, essay_id)
    return EssayMutationOut(essay=None, fingerprint=FingerprintOut.of(fp) if fp else None)


# ---- semantic memory ----------------------------------------------------------------------------


@router.get("/memory", response_model=MemoryPageOut)
async def list_memory(
    user_id: CurrentUserId,
    container: Container,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MemoryPageOut:
    items, total = await container.vault.list_memory.execute(
        user_id, search=q, limit=limit, offset=offset
    )
    return MemoryPageOut(items=[MemoryOut.of(i) for i in items], total=total)


@router.post("/memory", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def learn(body: MemoryIn, user_id: CurrentUserId, container: Container) -> MemoryOut:
    qa = await container.vault.learn_answer.execute(
        user_id, question=body.question, answer=body.answer
    )
    return MemoryOut.of(qa)


@router.patch("/memory/{qa_id}", response_model=MemoryOut)
async def update_memory(
    qa_id: uuid.UUID, body: MemoryPatch, user_id: CurrentUserId, container: Container
) -> MemoryOut:
    qa = await container.vault.update_memory.execute(
        user_id, qa_id, question=body.question, answer=body.answer
    )
    return MemoryOut.of(qa)


@router.delete("/memory/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(qa_id: uuid.UUID, user_id: CurrentUserId, container: Container) -> Response:
    await container.vault.delete_memory.execute(user_id, qa_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/memory/recall", response_model=RecallOut)
async def recall(body: RecallIn, user_id: CurrentUserId, container: Container) -> RecallOut:
    return RecallOut.of(await container.vault.recall.execute(user_id, body.question, k=body.k))
