from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from app.application.vault_deps import VaultDeps
from app.domain.events.models import NewEvent
from app.domain.llm.provider import EncryptedSecret
from app.domain.ports.crypto import aad
from app.domain.ports.vault import StoredDocument
from app.domain.profile.documents import (
    MAX_EXTRACTED_CHARS,
    Document,
    safe_filename,
    validate_pdf,
)
from app.domain.profile.models import (
    GPA,
    DegreeLevel,
    Experience,
    Language,
    StudentPII,
    StudentProfile,
    clean_skills,
)
from app.domain.shared.enums import Actor, DocKind
from app.domain.shared.errors import ConflictError, NotFoundError, ValidationError
from app.domain.shared.ids import new_uuid7

PROFILE_TABLE = "student_profiles"
DOCUMENT_TABLE = "documents"


# ---- profile ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileInput:
    full_name: str
    pii: StudentPII
    degree_level: DegreeLevel | None = None
    major: str | None = None
    institution: str | None = None
    gpa: float | None = None
    gpa_scale: float | None = None
    nationality: str | None = None
    country_of_residence: str | None = None
    graduation_year: int | None = None
    skills: list[str] = field(default_factory=list)
    languages: list[Language] = field(default_factory=list)
    experiences: list[Experience] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProfileView:
    profile: StudentProfile  # with decrypted PII (owner-only view)
    pii_encrypted: bool
    pii_key_version: int | None


def _clean(value: str | None, limit: int = 200) -> str | None:
    if value is None:
        return None
    value = value.strip()[:limit]
    return value or None


def _pii_aad(user_id: uuid.UUID, row_id: uuid.UUID) -> bytes:
    return aad(PROFILE_TABLE, "pii_ciphertext", user_id, row_id)


@dataclass(slots=True)
class GetProfile:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID) -> ProfileView | None:
        async with self.deps.uow_factory(user_id) as uow:
            stored = await uow.profiles.get(user_id)
        if stored is None:
            return None
        profile = stored.profile
        if stored.pii is not None:
            raw = self.deps.cipher.decrypt(stored.pii, _pii_aad(user_id, profile.id))
            profile.pii = StudentPII.from_dict(json.loads(raw))
        return ProfileView(
            profile=profile,
            pii_encrypted=stored.pii is not None,
            pii_key_version=stored.pii.key_version if stored.pii else None,
        )


@dataclass(slots=True)
class SaveProfile:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, data: ProfileInput) -> ProfileView:
        gpa: GPA | None = None
        if data.gpa is not None:
            if data.gpa_scale is None:
                raise ValidationError("gpa_scale is required when gpa is set")
            gpa = GPA.of(data.gpa, data.gpa_scale)
        now = self.deps.clock.now()
        pii = StudentPII.from_dict({k: _clean(v, 500) for k, v in data.pii.to_dict().items()})
        async with self.deps.uow_factory(user_id) as uow:
            existing = await uow.profiles.get(user_id)
            row_id = existing.profile.id if existing else new_uuid7()
            profile = StudentProfile(
                id=row_id,
                user_id=user_id,
                full_name=(data.full_name or "").strip()[:200],
                pii=pii,
                degree_level=data.degree_level,
                major=_clean(data.major),
                institution=_clean(data.institution),
                gpa=gpa,
                nationality=_clean(data.nationality, 100),
                country_of_residence=_clean(data.country_of_residence, 100),
                graduation_year=data.graduation_year,
                skills=clean_skills(data.skills),
                languages=[
                    Language(name=lang.name.strip()[:60], level=lang.level.strip()[:40])
                    for lang in data.languages
                    if lang.name.strip()
                ],
                experiences=[
                    Experience(
                        title=e.title.strip()[:200],
                        organization=e.organization.strip()[:200],
                        start=_clean(e.start, 10),
                        end=_clean(e.end, 10),
                        description=e.description.strip()[:2000],
                    )
                    for e in data.experiences
                    if e.title.strip()
                ],
                created_at=existing.profile.created_at if existing else now,
                updated_at=now,
            )
            profile.validate()
            secret: EncryptedSecret | None = None
            if not pii.is_empty():
                payload = json.dumps(pii.to_dict(), separators=(",", ":")).encode()
                secret = self.deps.cipher.encrypt(payload, _pii_aad(user_id, row_id))
            await uow.profiles.save(profile, secret)
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="vault.profile_saved",
                entity=PROFILE_TABLE,
                entity_id=row_id,
            )
            # Never put PII in events: they are streamed and stored in plaintext.
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="vault.profile_updated",
                    payload={
                        "skills": len(profile.skills),
                        "experiences": len(profile.experiences),
                    },
                )
            )
            await uow.commit()
        return ProfileView(
            profile=profile,
            pii_encrypted=secret is not None,
            pii_key_version=secret.key_version if secret else None,
        )


# ---- documents ----------------------------------------------------------------------------------


def _blob_aad(user_id: uuid.UUID, row_id: uuid.UUID) -> bytes:
    return aad(DOCUMENT_TABLE, "blob", user_id, row_id)


def _text_aad(user_id: uuid.UUID, row_id: uuid.UUID) -> bytes:
    return aad(DOCUMENT_TABLE, "extracted_text", user_id, row_id)


@dataclass(slots=True)
class UploadDocument:
    deps: VaultDeps

    async def execute(
        self, user_id: uuid.UUID, *, kind: DocKind, filename: str, data: bytes
    ) -> Document:
        validate_pdf(data)
        digest = hashlib.sha256(data).hexdigest()
        async with self.deps.uow_factory(user_id) as uow:
            duplicate = await uow.documents.find_by_sha(user_id, digest)
        if duplicate is not None:
            raise ConflictError(f"this file is already in the vault as '{duplicate.filename}'")
        extracted = await self.deps.pdf.extract(data)
        text = extracted.text[:MAX_EXTRACTED_CHARS]

        row_id = new_uuid7()
        blob = self.deps.cipher.encrypt(data, _blob_aad(user_id, row_id))
        text_secret = (
            self.deps.cipher.encrypt(text.encode(), _text_aad(user_id, row_id)) if text else None
        )
        document = Document(
            id=row_id,
            user_id=user_id,
            kind=kind,
            filename=safe_filename(filename),
            mime="application/pdf",
            size_bytes=len(data),
            sha256=digest,
            storage_key=f"{user_id}/{row_id}.bin",
            page_count=extracted.page_count,
            text_chars=len(text),
            created_at=self.deps.clock.now(),
        )
        await self.deps.storage.put(document.storage_key, blob.ciphertext)
        try:
            async with self.deps.uow_factory(user_id) as uow:
                await uow.documents.add(
                    StoredDocument(
                        document=document,
                        blob_nonce=blob.nonce,
                        blob_key_version=blob.key_version,
                        text=text_secret,
                    )
                )
                await uow.audit.record(
                    user_id=user_id,
                    actor=Actor.USER,
                    action="vault.document_uploaded",
                    entity=DOCUMENT_TABLE,
                    entity_id=row_id,
                    metadata={"kind": kind.value, "bytes": len(data)},
                )
                await uow.events.append(
                    NewEvent(
                        user_id=user_id,
                        type="vault.document_added",
                        payload={
                            "document_id": str(row_id),
                            "kind": kind.value,
                            "pages": extracted.page_count,
                            "bytes": len(data),
                        },
                    )
                )
                await uow.commit()
        except BaseException:
            await self.deps.storage.delete(document.storage_key)
            raise
        return document


@dataclass(slots=True)
class ListDocuments:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID) -> list[Document]:
        async with self.deps.uow_factory(user_id) as uow:
            return await uow.documents.list(user_id)


@dataclass(slots=True)
class GetDocumentText:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, document_id: uuid.UUID) -> tuple[Document, str]:
        async with self.deps.uow_factory(user_id) as uow:
            stored = await uow.documents.get(user_id, document_id)
        if stored is None:
            raise NotFoundError("document not found")
        text = ""
        if stored.text is not None:
            text = self.deps.cipher.decrypt(stored.text, _text_aad(user_id, document_id)).decode()
        return stored.document, text


@dataclass(slots=True)
class DownloadDocument:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, document_id: uuid.UUID) -> tuple[Document, bytes]:
        async with self.deps.uow_factory(user_id) as uow:
            stored = await uow.documents.get(user_id, document_id)
        if stored is None:
            raise NotFoundError("document not found")
        ciphertext = await self.deps.storage.get(stored.document.storage_key)
        data = self.deps.cipher.decrypt(
            EncryptedSecret(
                ciphertext=ciphertext, nonce=stored.blob_nonce, key_version=stored.blob_key_version
            ),
            _blob_aad(user_id, document_id),
        )
        return stored.document, data


@dataclass(slots=True)
class DeleteDocument:
    deps: VaultDeps

    async def execute(self, user_id: uuid.UUID, document_id: uuid.UUID) -> None:
        async with self.deps.uow_factory(user_id) as uow:
            stored = await uow.documents.get(user_id, document_id)
            if stored is None or not await uow.documents.delete(user_id, document_id):
                raise NotFoundError("document not found")
            await uow.audit.record(
                user_id=user_id,
                actor=Actor.USER,
                action="vault.document_deleted",
                entity=DOCUMENT_TABLE,
                entity_id=document_id,
            )
            await uow.events.append(
                NewEvent(
                    user_id=user_id,
                    type="vault.document_deleted",
                    payload={"document_id": str(document_id)},
                )
            )
            await uow.commit()
        await self.deps.storage.delete(stored.document.storage_key)
