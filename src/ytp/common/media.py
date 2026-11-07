"""Rendered media: a private Blob container in Azure, a folder locally. Nothing is ever public."""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol

CONTAINER = "media"


class Media(Protocol):
    def put(self, name: str, path: Path, content_type: str = "video/mp4") -> str: ...
    def download(self, name: str, path: Path) -> Path: ...
    def size(self, name: str) -> int: ...
    def stream(self, name: str, start: int = 0, length: int | None = None) -> Iterator[bytes]: ...
    def sas_url(self, name: str, minutes: int = 15) -> str: ...
    def exists(self, name: str) -> bool: ...


class LocalMedia:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("bad media name")
        return path

    def put(self, name, path, content_type="video/mp4"):
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(path).resolve() != target:
            shutil.copyfile(path, target)
        return name

    def download(self, name, path):
        shutil.copyfile(self._path(name), path)
        return Path(path)

    def size(self, name):
        return self._path(name).stat().st_size

    def stream(self, name, start=0, length=None):
        with open(self._path(name), "rb") as handle:
            handle.seek(start)
            remaining = length if length is not None else self.size(name) - start
            while remaining > 0:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    def sas_url(self, name, minutes=15):
        return self._path(name).as_uri()

    def exists(self, name):
        return self._path(name).exists()


class BlobMedia:
    def __init__(self, account: str, credential):
        from azure.storage.blob import BlobServiceClient

        self.account = account
        self.service = BlobServiceClient(account_url=f"https://{account}.blob.core.windows.net", credential=credential)
        self.container = self.service.get_container_client(CONTAINER)

    def put(self, name, path, content_type="video/mp4"):
        from azure.storage.blob import ContentSettings

        with open(path, "rb") as handle:
            self.container.upload_blob(name, handle, overwrite=True, max_concurrency=4,
                                       content_settings=ContentSettings(content_type=content_type))
        return name

    def download(self, name, path):
        with open(path, "wb") as handle:
            self.container.download_blob(name, max_concurrency=4).readinto(handle)
        return Path(path)

    def size(self, name):
        return self.container.get_blob_client(name).get_blob_properties().size

    def stream(self, name, start=0, length=None):
        yield from self.container.download_blob(name, offset=start, length=length).chunks()

    def sas_url(self, name, minutes=15):
        """Fallback only (LLD D5): read-only, one blob, at most 15 minutes, signed with a user-delegation key."""
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        minutes = min(minutes, 15)
        now = datetime.now(timezone.utc)
        key = self.service.get_user_delegation_key(now - timedelta(minutes=2), now + timedelta(minutes=minutes))
        token = generate_blob_sas(self.account, CONTAINER, name, user_delegation_key=key,
                                  permission=BlobSasPermissions(read=True), expiry=now + timedelta(minutes=minutes),
                                  start=now - timedelta(minutes=2))
        return f"https://{self.account}.blob.core.windows.net/{CONTAINER}/{name}?{token}"

    def exists(self, name):
        return self.container.get_blob_client(name).exists()
