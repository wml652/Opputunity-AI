from __future__ import annotations

from dataclasses import dataclass

from app.application.llm.use_cases import GetEmbeddingProvider
from app.domain.ports.crypto import ICipher
from app.domain.ports.uow import IUnitOfWorkFactory
from app.domain.ports.vault import IObjectStorage, IPdfTextExtractor, IStylometryAnalyzer
from app.domain.shared.clock import Clock


@dataclass(frozen=True, slots=True)
class VaultDeps:
    uow_factory: IUnitOfWorkFactory
    cipher: ICipher
    clock: Clock
    storage: IObjectStorage
    pdf: IPdfTextExtractor
    stylometry: IStylometryAnalyzer
    embeddings: GetEmbeddingProvider  # resolves the user's 1024-d embedding provider
