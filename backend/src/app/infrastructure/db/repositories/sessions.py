"""Browser sessions (encrypted storage_state) and per-user search API credentials."""

from __future__ import annotations

import builtins
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery.sessions import BrowserSession, SearchCredential
from app.domain.llm.provider import EncryptedSecret
from app.infrastructure.db.models import BrowserSessionRow, SearchCredentialRow


def _session(r: BrowserSessionRow) -> BrowserSession:
    return BrowserSession(
        id=r.id,
        user_id=r.user_id,
        domain=r.domain,
        kind=r.kind,
        storage_state=EncryptedSecret(
            ciphertext=r.storage_state_ciphertext,
            nonce=r.storage_state_nonce,
            key_version=r.storage_state_key_version,
        ),
        created_at=r.created_at,
        tos_acknowledged_at=r.tos_acknowledged_at,
        last_used_at=r.last_used_at,
        expires_at=r.expires_at,
        daily_action_cap=r.daily_action_cap,
        actions_today=r.actions_today,
        actions_day=r.actions_day,
    )


class SqlBrowserSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list(self, user_id: uuid.UUID) -> builtins.list[BrowserSession]:
        rows = await self._s.scalars(
            select(BrowserSessionRow)
            .where(BrowserSessionRow.user_id == user_id)
            .order_by(BrowserSessionRow.domain, BrowserSessionRow.kind)
        )
        return [_session(r) for r in rows]

    async def get(self, user_id: uuid.UUID, session_id: uuid.UUID) -> BrowserSession | None:
        row = await self._s.scalar(
            select(BrowserSessionRow).where(
                BrowserSessionRow.user_id == user_id, BrowserSessionRow.id == session_id
            )
        )
        return _session(row) if row else None

    async def find(self, user_id: uuid.UUID, domain: str, kind: str) -> BrowserSession | None:
        row = await self._s.scalar(
            select(BrowserSessionRow).where(
                BrowserSessionRow.user_id == user_id,
                BrowserSessionRow.domain == domain,
                BrowserSessionRow.kind == kind,
            )
        )
        return _session(row) if row else None

    async def upsert(self, session: BrowserSession) -> None:
        values = {
            "storage_state_ciphertext": session.storage_state.ciphertext,
            "storage_state_nonce": session.storage_state.nonce,
            "storage_state_key_version": session.storage_state.key_version,
            "tos_acknowledged_at": session.tos_acknowledged_at,
            "last_used_at": session.last_used_at,
            "expires_at": session.expires_at,
            "daily_action_cap": session.daily_action_cap,
            "actions_today": session.actions_today,
            "actions_day": session.actions_day,
        }
        stmt = pg_insert(BrowserSessionRow).values(
            id=session.id,
            user_id=session.user_id,
            domain=session.domain,
            kind=session.kind,
            created_at=session.created_at,
            **values,
        )
        await self._s.execute(
            stmt.on_conflict_do_update(
                constraint="uq_browser_sessions_user_domain_kind", set_=values
            )
        )

    async def update_usage(self, session: BrowserSession) -> None:
        await self._s.execute(
            update(BrowserSessionRow)
            .where(BrowserSessionRow.user_id == session.user_id, BrowserSessionRow.id == session.id)
            .values(
                last_used_at=session.last_used_at,
                actions_today=session.actions_today,
                actions_day=session.actions_day,
                daily_action_cap=session.daily_action_cap,
            )
        )

    async def delete(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        result = await self._s.execute(
            delete(BrowserSessionRow)
            .where(BrowserSessionRow.user_id == user_id, BrowserSessionRow.id == session_id)
            .returning(BrowserSessionRow.id)
        )
        return result.first() is not None


class SqlSearchCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list(self, user_id: uuid.UUID) -> builtins.list[SearchCredential]:
        rows = await self._s.scalars(
            select(SearchCredentialRow)
            .where(SearchCredentialRow.user_id == user_id)
            .order_by(SearchCredentialRow.provider)
        )
        return [
            SearchCredential(
                id=r.id,
                user_id=r.user_id,
                provider=r.provider,
                key=EncryptedSecret(
                    ciphertext=r.key_ciphertext, nonce=r.key_nonce, key_version=r.key_version
                ),
                key_last4=r.key_last4,
                updated_at=r.updated_at,
            )
            for r in rows
        ]

    async def upsert(self, credential: SearchCredential) -> None:
        values = {
            "id": credential.id,
            "key_ciphertext": credential.key.ciphertext,
            "key_nonce": credential.key.nonce,
            "key_version": credential.key.key_version,
            "key_last4": credential.key_last4,
            "updated_at": credential.updated_at,
        }
        stmt = pg_insert(SearchCredentialRow).values(
            user_id=credential.user_id, provider=credential.provider, **values
        )
        # The row id is part of the AAD, so a replaced key gets the new row id too.
        await self._s.execute(
            stmt.on_conflict_do_update(
                constraint="uq_search_credentials_user_provider", set_=values
            )
        )

    async def delete(self, user_id: uuid.UUID, provider: str) -> bool:
        result = await self._s.execute(
            delete(SearchCredentialRow)
            .where(SearchCredentialRow.user_id == user_id, SearchCredentialRow.provider == provider)
            .returning(SearchCredentialRow.id)
        )
        return result.first() is not None
