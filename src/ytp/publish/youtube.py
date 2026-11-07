"""YouTubePublisher (LLD Sections 5.4 and Step 7).

Exactly-once rules:
- an attempt row is written before any side effect;
- the resumable session URI is persisted as soon as YouTube returns it, so a crashed upload resumes
  instead of starting a second video;
- if the session is gone or the outcome is unknown, reconcile() looks for the item's `ref-<id>` tag among
  the channel's latest uploads before any new videos.insert.

The resumable protocol is implemented directly over HTTPS (no googleapiclient), which keeps the image
small and makes every step explicit and testable.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from ..common.errors import AuthError, Permanent, Transient, UnknownOutcome, http_client, raise_for_http, with_retries

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024
DISCLOSURE = "Narration and visuals created with AI tools."


class GoogleToken:
    """TokenManager for Google: refresh-token grant, in-memory cache, refresh 5 minutes before expiry."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, client: httpx.Client | None = None):
        self._id, self._secret, self._refresh = client_id, client_secret, refresh_token
        self._client = client or http_client(connect=10, read=30)
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def bearer(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            response = self._client.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": self._id,
                                                           "client_secret": self._secret,
                                                           "refresh_token": self._refresh})
            if response.status_code == 400 and "invalid_grant" in response.text:
                raise AuthError("YouTube refresh token is invalid or revoked (invalid_grant): run the bootstrap again")
            raise_for_http(response, "google token")
            data = response.json()
            self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""


@dataclass
class YouTubeMeta:
    title: str
    description: str
    tags: list[str]
    category_id: str
    privacy: str
    language: str = "en-IN"


def build_metadata(script, ref: str, privacy: str, category_id: str) -> YouTubeMeta:
    title = script.title[:92].rstrip() + " #Shorts"
    sources = [str(u) for u in script.sources]
    description = script.description.strip()
    if sources:
        description += "\n\nSources:\n" + "\n".join(f"- {u}" for u in sources)
    description += "\n\n" + " ".join(f"#{h}" for h in script.hashtags[:5]) + " #Shorts"
    description += f"\n\n{DISCLOSURE}"
    tags, total = [], 0
    for tag in ["shorts", ref, *script.tags]:
        if tag and tag not in tags and total + len(tag) + 1 <= 480:
            tags.append(tag)
            total += len(tag) + 1
    return YouTubeMeta(title, description.encode()[:4900].decode(errors="ignore"), tags,
                       category_id if category_id in ("17", "20", "22", "24", "25", "26", "27", "28") else "24",
                       privacy, script.language.value if hasattr(script.language, "value") else str(script.language))


class YouTubeClient:
    def __init__(self, token: GoogleToken, client: httpx.Client | None = None, quota: Callable[[int], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = token
        self.client = client or http_client(connect=10, read=120)
        self.quota = quota or (lambda units: None)
        self.sleep = sleep

    def _headers(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self.token.bearer()}", **extra}

    def _get(self, path: str, params: dict, units: int = 1) -> dict:
        def call():
            try:
                response = self.client.get(f"{API}/{path}", params=params, headers=self._headers())
            except httpx.TransportError as exc:
                raise Transient(f"{path}: {exc.__class__.__name__}") from exc
            if response.status_code == 401:
                self.token.invalidate()
                response = self.client.get(f"{API}/{path}", params=params, headers=self._headers())
            if response.status_code == 403 and "quotaExceeded" in response.text:
                raise Permanent("YouTube quotaExceeded")
            raise_for_http(response, path)
            return response.json()

        self.quota(units)
        return with_retries(call, attempts=3, base=2, sleep=self.sleep)

    def channel(self) -> dict:
        items = self._get("channels", {"part": "snippet,contentDetails", "mine": "true"}).get("items", [])
        if not items:
            raise AuthError("the OAuth token has no YouTube channel")
        return items[0]

    def verify_channel(self, expected_handle: str) -> dict:
        channel = self.channel()
        handle = (channel["snippet"].get("customUrl") or "").lower()
        if expected_handle and handle != expected_handle.lower():
            raise AuthError(f"token belongs to {handle!r}, expected {expected_handle!r}: refusing to upload")
        return channel

    def find_by_ref(self, uploads_playlist: str, ref: str) -> str | None:
        """reconcile(): the newest 50 uploads, then their tags (owner-only), looking for ref-<id>."""
        items = self._get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist,
                                            "maxResults": 50}).get("items", [])
        ids = [i["contentDetails"]["videoId"] for i in items]
        if not ids:
            return None
        for video in self._get("videos", {"part": "snippet", "id": ",".join(ids)}).get("items", []):
            if ref in (video.get("snippet", {}).get("tags") or []):
                return video["id"]
        return None

    def start_session(self, meta: YouTubeMeta, size: int) -> str:
        body = {"snippet": {"title": meta.title, "description": meta.description, "tags": meta.tags,
                            "categoryId": meta.category_id, "defaultLanguage": meta.language.split("-")[0],
                            "defaultAudioLanguage": meta.language.split("-")[0]},
                "status": {"privacyStatus": meta.privacy, "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia": True}}
        try:
            response = self.client.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                                        headers=self._headers(**{"X-Upload-Content-Type": "video/mp4",
                                                                 "X-Upload-Content-Length": str(size),
                                                                 "Content-Type": "application/json; charset=UTF-8"}),
                                        content=json.dumps(body))
        except httpx.TransportError as exc:
            raise Transient(f"videos.insert session: {exc.__class__.__name__}") from exc
        if response.status_code == 403 and ("uploadLimitExceeded" in response.text or "quotaExceeded" in response.text):
            raise Permanent(f"YouTube upload limit: {response.text[:200]}")
        raise_for_http(response, "videos.insert session")
        location = response.headers.get("location")
        if not location:
            raise Transient("videos.insert session: no Location header")
        return location

    def session_status(self, session: str, size: int) -> tuple[str, int | dict]:
        """('done', video) | ('resume', next_offset) | ('gone', 0)."""
        try:
            response = self.client.put(session, headers=self._headers(**{"Content-Range": f"bytes */{size}",
                                                                         "Content-Length": "0"}))
        except httpx.TransportError as exc:
            raise Transient(f"upload status: {exc.__class__.__name__}") from exc
        if response.status_code in (200, 201):
            return "done", response.json()
        if response.status_code == 308:
            rng = response.headers.get("range")
            return "resume", (int(rng.split("-")[1]) + 1) if rng else 0
        if response.status_code in (404, 410):
            return "gone", 0
        raise_for_http(response, "upload status")
        return "gone", 0

    def upload_from(self, session: str, path: Path, offset: int, size: int) -> dict:
        retries = 0
        with open(path, "rb") as handle:
            while True:
                handle.seek(offset)
                chunk = handle.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    response = self.client.put(session, content=chunk, headers=self._headers(**{
                        "Content-Range": f"bytes {offset}-{end}/{size}", "Content-Type": "video/mp4"}))
                except httpx.TransportError:
                    retries += 1
                    if retries > 10:
                        raise UnknownOutcome("upload interrupted repeatedly")
                    self.sleep(min(60, 2 ** retries))
                    state, value = self.session_status(session, size)
                    if state == "done":
                        return value  # type: ignore[return-value]
                    if state == "gone":
                        raise UnknownOutcome("upload session lost")
                    offset = int(value)  # type: ignore[arg-type]
                    continue
                if response.status_code in (200, 201):
                    return response.json()
                if response.status_code == 308:
                    rng = response.headers.get("range")
                    offset = int(rng.split("-")[1]) + 1 if rng else offset + len(chunk)
                    retries = 0
                    continue
                if response.status_code >= 500:
                    retries += 1
                    if retries > 10:
                        raise UnknownOutcome(f"upload failing with {response.status_code}")
                    self.sleep(min(60, 2 ** retries))
                    state, value = self.session_status(session, size)
                    if state == "done":
                        return value  # type: ignore[return-value]
                    if state == "gone":
                        raise UnknownOutcome("upload session lost")
                    offset = int(value)  # type: ignore[arg-type]
                    continue
                raise_for_http(response, "upload chunk")

    def set_privacy(self, video_id: str, privacy: str) -> None:
        self.quota(50)
        response = self.client.put(f"{API}/videos", params={"part": "status"}, headers=self._headers(),
                                   json={"id": video_id, "status": {"privacyStatus": privacy,
                                                                    "selfDeclaredMadeForKids": False,
                                                                    "containsSyntheticMedia": True}})
        raise_for_http(response, "videos.update")

    def stats(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(ids), 50):
            data = self._get("videos", {"part": "statistics,status", "id": ",".join(ids[i:i + 50])})
            for v in data.get("items", []):
                s = v.get("statistics", {})
                out[v["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                "comments": int(s.get("commentCount", 0)),
                                "privacy": v.get("status", {}).get("privacyStatus", "")}
        return out
