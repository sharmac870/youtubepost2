"""Builds the concrete services for the configured backend (local SQLite/folder or Azure)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .config import Settings, get_settings
from .media import BlobMedia, LocalMedia, Media
from .queues import Queues, SqliteQueues, StorageQueues
from .repo import Repo
from .secrets import Secrets
from .store import SqliteStore, Store, TableStore


@dataclass
class Runtime:
    settings: Settings
    store: Store
    queues: Queues
    media: Media
    secrets: Secrets

    @property
    def repo(self) -> Repo:
        return Repo(self.store, self.queues)


def build(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    if settings.backend == "azure":
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(managed_identity_client_id=settings.azure_client_id or None)
        return Runtime(settings, TableStore(settings.storage_account, credential),
                       StorageQueues(settings.storage_account, credential),
                       BlobMedia(settings.storage_account, credential),
                       Secrets(settings.key_vault_uri, credential))
    data = settings.data_dir
    return Runtime(settings, SqliteStore(data / "state.db"), SqliteQueues(data / "queues.db"),
                   LocalMedia(data / "media"), Secrets())


@lru_cache(maxsize=1)
def default() -> Runtime:
    return build()
