"""InstagramPublisher client (LLD Sections 2.2, 5.4 and Step 7): REELS container with resumable upload.

- Graph API host and version come from configuration.
- The token goes in the Authorization header, never the URL; appsecret_proof is added when the app secret
  is configured.
- The container id is persisted the moment it is returned; before any retry the container's status_code
  decides what to do (PUBLISHED = done, FINISHED = only media_publish, ERROR/EXPIRED = new container).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path
from typing import Callable

import httpx

from ..common.errors import AuthError, Permanent, Transient, http_client

log = logging.getLogger(__name__)

TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613, 80001, 80002}
DISCLOSURE = "Narration and visuals created with AI tools."


def build_caption(script) -> str:
    tags = " ".join(f"#{h}" for h in script.hashtags[:8])
    text = f"{script.caption.strip()}\n\n{tags}\n\n{DISCLOSURE}"
    return text[:2200]


class InstagramClient:
    def __init__(self, token: str, user_id: str = "", host: str = "graph.instagram.com", version: str = "v24.0",
                 app_secret: str = "", client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.token = token
        self.base = f"https://{host}/{version}"
        self.app_secret = app_secret
        self.client = client or http_client(connect=10, read=60)
        self.sleep = sleep
        self.user_id = user_id

    def _params(self, params: dict | None = None) -> dict:
        params = dict(params or {})
        if self.app_secret:
            params["appsecret_proof"] = hmac.new(self.app_secret.encode(), self.token.encode(),
                                                 hashlib.sha256).hexdigest()
        return params

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _check(response: httpx.Response, what: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code < 400 and "error" not in data:
            return data
        error = data.get("error", {}) if isinstance(data, dict) else {}
        code = int(error.get("code", 0) or 0)
        message = f"{what}: HTTP {response.status_code} code {code} {str(error.get('message', response.text))[:200]}"
        if code == 190 or response.status_code == 401:
            raise AuthError(message)
        if code in TRANSIENT_CODES or response.status_code >= 500 or response.status_code == 429:
            raise Transient(message)
        raise Permanent(message)

    def _call(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None,
              attempts: int = 3) -> dict:
        for attempt in range(1, attempts + 1):
            try:
                response = self.client.request(method, f"{self.base}/{path}", params=self._params(params),
                                               data=data, headers=self._headers())
                return self._check(response, f"{method} {path}")
            except (httpx.TransportError, Transient) as exc:
                if attempt == attempts:
                    raise Transient(str(exc)) from exc
                self.sleep(5 * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    def me(self) -> dict:
        data = self._call("GET", "me", params={"fields": "user_id,username,id"})
        self.user_id = self.user_id or str(data.get("user_id") or data.get("id"))
        return data

    def uid(self) -> str:
        if not self.user_id:
            self.me()
        return self.user_id

    def publishing_room(self) -> int:
        data = self._call("GET", f"{self.uid()}/content_publishing_limit", params={"fields": "quota_usage,config"})
        row = (data.get("data") or [{}])[0]
        total = int((row.get("config") or {}).get("quota_total", 50) or 50)
        return total - int(row.get("quota_usage", 0) or 0)

    def create_container(self, caption: str, video_url: str = "") -> tuple[str, str]:
        """Returns (container_id, upload_uri). With video_url, Meta fetches the file (SAS fallback)."""
        data = {"media_type": "REELS", "caption": caption, "share_to_feed": "true"}
        if video_url:
            data["video_url"] = video_url
        else:
            data["upload_type"] = "resumable"
        # A POST that creates a container is not retried blindly; the caller persists the id first.
        result = self._call("POST", f"{self.uid()}/media", data=data, attempts=1)
        return str(result["id"]), str(result.get("uri", ""))

    def upload(self, upload_uri: str, path: Path) -> None:
        size = Path(path).stat().st_size
        uri = upload_uri or f"https://rupload.facebook.com/ig-api-upload/{self.base.rsplit('/', 1)[-1]}"
        for attempt in range(1, 4):
            try:
                with open(path, "rb") as handle:
                    response = self.client.post(uri, content=handle.read(), headers={
                        "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)},
                        timeout=httpx.Timeout(600, connect=10))
                data = self._check(response, "rupload")
                if data.get("success") is False:
                    raise Transient(f"rupload: {data}")
                return
            except (httpx.TransportError, Transient) as exc:
                if attempt == 3:
                    raise Transient(f"rupload failed: {exc}") from exc
                self.sleep(5 * 2 ** (attempt - 1))

    def container_status(self, container_id: str) -> tuple[str, str]:
        data = self._call("GET", container_id, params={"fields": "status_code,status"})
        return str(data.get("status_code", "")), str(data.get("status", ""))

    def publish(self, container_id: str) -> str:
        return str(self._call("POST", f"{self.uid()}/media_publish", data={"creation_id": container_id},
                              attempts=1)["id"])

    def find_media(self, caption: str) -> dict | None:
        """reconcile(): the account's latest media, matched on the caption we posted."""
        data = self._call("GET", f"{self.uid()}/media", params={"fields": "id,caption,permalink,timestamp",
                                                                "limit": 25})
        head = caption.strip()[:120]
        for media in data.get("data", []):
            if (media.get("caption") or "").strip()[:120] == head:
                return media
        return None

    def permalink(self, media_id: str) -> str:
        return str(self._call("GET", media_id, params={"fields": "permalink"}).get("permalink", ""))

    def stats(self, media_id: str) -> dict:
        data = self._call("GET", media_id, params={"fields": "like_count,comments_count,permalink"})
        out = {"likes": int(data.get("like_count", 0) or 0), "comments": int(data.get("comments_count", 0) or 0),
               "views": 0}
        try:
            insights = self._call("GET", f"{media_id}/insights", params={"metric": "views"}, attempts=1)
            for metric in insights.get("data", []):
                if metric.get("name") == "views":
                    out["views"] = int((metric.get("values") or [{}])[0].get("value", 0))
        except (Permanent, Transient):
            pass
        return out
