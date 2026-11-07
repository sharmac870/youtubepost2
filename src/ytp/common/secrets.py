"""Secrets (LLD Section 8.3). In Azure they come only from Key Vault, read with the workload's managed
identity, cached in memory, and never written anywhere. Locally they come from environment variables
(ANTHROPIC_API_KEY for `anthropic-api-key`, and so on).

Secret names:
  anthropic-api-key, youtube-api-key, yt-client-id, yt-client-secret, yt-refresh-token,
  ig-access-token, meta-app-secret (optional, enables appsecret_proof)
"""
from __future__ import annotations

import os
import threading
import time


class MissingSecret(Exception):
    pass


class Secrets:
    TTL_S = 900

    def __init__(self, vault_uri: str = "", credential=None):
        self._client = None
        if vault_uri:
            from azure.keyvault.secrets import SecretClient

            self._client = SecretClient(vault_url=vault_uri, credential=credential)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, name: str, required: bool = True) -> str:
        with self._lock:
            hit = self._cache.get(name)
            if hit and time.monotonic() - hit[0] < self.TTL_S:
                return hit[1]
        value = ""
        if self._client is not None:
            from azure.core.exceptions import ResourceNotFoundError

            try:
                value = self._client.get_secret(name).value or ""
            except ResourceNotFoundError:
                value = ""
        else:
            value = os.environ.get(name.upper().replace("-", "_"), "")
        value = value.strip().strip("\"'").strip()   # pasted tokens often carry quotes or newlines
        if not value and required:
            raise MissingSecret(f"secret {name!r} is not set")
        if value:
            with self._lock:
                self._cache[name] = (time.monotonic(), value)
        return value

    def forget(self, name: str) -> None:
        with self._lock:
            self._cache.pop(name, None)
