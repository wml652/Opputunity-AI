from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity.models import RefreshToken, User
from app.infrastructure.db.models import RefreshTokenRow, UserRow


def _user(row: UserRow) -> User:
    return User(
        id=row.id,
        email=row.email,
        password_hash=row.password_hash,
        display_name=row.display_name,
        timezone=row.timezone,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _token(row: RefreshTokenRow) -> RefreshToken:
    return RefreshToken(
        id=row.id,
        user_id=row.user_id,
        family_id=row.family_id,
        token_hash=row.token_hash,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        replaced_by=row.replaced_by,
        created_at=row.created_at,
    )


class SqlUserRepository:
    """`users` is the tenant root and is not RLS-scoped (login must look up by email)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, user: User) -> None:
        self._s.add(
            UserRow(
                id=user.id,
                email=user.email,
                password_hash=user.password_hash,
                display_name=user.display_name,
                timezone=user.timezone,
                is_active=user.is_active,
                created_at=user.created_at,
                updated_at=user.created_at,
            )
        )
        await self._s.flush()

    async def get(self, user_id: uuid.UUID) -> User | None:
        row = await self._s.get(UserRow, user_id)
        return _user(row) if row else None

    async def get_by_email(self, email: str) -> User | None:
        row = await self._s.scalar(
            select(UserRow).where(func.lower(UserRow.email) == email.lower())
        )
        return _user(row) if row else None

    async def update_password_hash(self, user_id: uuid.UUID, password_hash: str) -> None:
        await self._s.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(password_hash=password_hash, updated_at=func.now())
        )


class SqlRefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, token: RefreshToken) -> None:
        self._s.add(
            RefreshTokenRow(
                id=token.id,
                user_id=token.user_id,
                family_id=token.family_id,
                token_hash=token.token_hash,
                expires_at=token.expires_at,
                revoked_at=token.revoked_at,
                replaced_by=token.replaced_by,
                created_at=token.created_at,
            )
        )
        await self._s.flush()

    async def get(self, user_id: uuid.UUID, token_id: uuid.UUID) -> RefreshToken | None:
        row = await self._s.scalar(
            select(RefreshTokenRow).where(
                RefreshTokenRow.user_id == user_id, RefreshTokenRow.id == token_id
            )
        )
        return _token(row) if row else None

    async def revoke(
        self,
        user_id: uuid.UUID,
        token_id: uuid.UUID,
        at: datetime,
        replaced_by: uuid.UUID | None,
    ) -> None:
        await self._s.execute(
            update(RefreshTokenRow)
            .where(
                RefreshTokenRow.user_id == user_id,
                RefreshTokenRow.id == token_id,
                RefreshTokenRow.revoked_at.is_(None),
            )
            .values(revoked_at=at, replaced_by=replaced_by)
        )

    async def revoke_family(self, user_id: uuid.UUID, family_id: uuid.UUID, at: datetime) -> int:
        result = await self._s.execute(
            update(RefreshTokenRow)
            .where(
                RefreshTokenRow.user_id == user_id,
                RefreshTokenRow.family_id == family_id,
                RefreshTokenRow.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
