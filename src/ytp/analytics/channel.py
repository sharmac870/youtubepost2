"""Whole-channel analytics for the dashboard, ported from YoutubePosts' Studio (studio_core/youtube.py).

The sweep job calls `ChannelStats.snapshot` with the read-only API key at most every few hours and saves
the result in the metrics table; the console only reads that row, so it never holds a YouTube key.
Quota: about 1 (channels) + 1-2 (playlistItems) + 1-2 (videos) units per refresh.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from ..common.errors import Transient, http_client, raise_for_http

API = "https://www.googleapis.com/youtube/v3"
SHORT_MAX_SECONDS = 180
MAX_PAGES = 4          # 200 uploads in 90 days is plenty for this channel
KEEP_VIDEOS = 150      # keeps the stored row well under the 64 KB table property limit


def parse_duration(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    d, h, m, s = (int(g or 0) for g in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def channel_query(text: str) -> dict:
    text = (text or "").strip()
    if re.fullmatch(r"UC[\w-]{22}", text):
        return {"id": text}
    handle = text if text.startswith("@") else f"@{text}"
    if not re.fullmatch(r"@[\w.\-]{3,100}", handle):
        raise ValueError("channel must be a handle like @chansharma870 or a UC… id")
    return {"forHandle": handle}


class ChannelStats:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or http_client(connect=5, read=30)
        self.units = 0

    def _get(self, resource: str, params: dict) -> dict:
        try:
            response = self.client.get(f"{API}/{resource}", params={**params, "key": self.api_key})
        except httpx.TransportError as exc:
            raise Transient(f"{resource}: {exc.__class__.__name__}") from exc
        self.units += 1
        raise_for_http(response, resource)
        return response.json()

    def snapshot(self, handle: str, days: int = 90, at: datetime | None = None) -> dict:
        at = at or datetime.now(timezone.utc)
        since = at - timedelta(days=days)
        items = self._get("channels", {"part": "snippet,statistics,contentDetails", **channel_query(handle)}).get("items", [])
        if not items:
            raise ValueError(f"no channel found for {handle}")
        ch = items[0]
        stats = ch.get("statistics", {})
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        ids, token = [], None
        for _ in range(MAX_PAGES):
            params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = self._get("playlistItems", params)
            older = False
            for it in data.get("items", []):
                when = parse_time(it.get("contentDetails", {}).get("videoPublishedAt", ""))
                if when and when >= since:
                    ids.append(it["contentDetails"]["videoId"])
                elif when:
                    older = True
            token = data.get("nextPageToken")
            if older or not token:   # newest first, so stop at the first older page
                break
        videos = []
        for start in range(0, len(ids), 50):
            data = self._get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[start:start + 50])})
            videos += [compact_video(v) for v in data.get("items", [])]
        videos.sort(key=lambda v: v["p"], reverse=True)
        return {
            "channel": {"id": ch["id"], "title": ch["snippet"].get("title", "")[:100],
                        "handle": ch["snippet"].get("customUrl", "")[:100],
                        "subscribers": None if stats.get("hiddenSubscriberCount") else int(stats.get("subscriberCount", 0)),
                        "total_views": int(stats.get("viewCount", 0)), "total_videos": int(stats.get("videoCount", 0))},
            "fetched_at": at.isoformat(), "videos": videos[:KEEP_VIDEOS],
        }


def compact_video(v: dict) -> dict:
    sn, st = v.get("snippet", {}), v.get("statistics", {})
    seconds = parse_duration(v.get("contentDetails", {}).get("duration", ""))
    return {"id": v.get("id", ""), "t": sn.get("title", "")[:80], "p": sn.get("publishedAt", ""), "s": seconds,
            "v": int(st.get("viewCount", 0)), "l": int(st.get("likeCount", 0) or 0),
            "c": int(st.get("commentCount", 0) or 0)}


def summarise(snapshot: dict, days: int, at: datetime | None = None) -> dict:
    """The dashboard view of a stored snapshot for the last `days` days."""
    at = at or datetime.now(timezone.utc)
    since = (at - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    videos = [v for v in snapshot.get("videos", []) if (parse_time(v["p"]) or at) >= since]
    by_day = Counter((parse_time(v["p"]) or at).date().isoformat() for v in videos)
    per_day, day = [], since.date()
    while day <= at.date():
        per_day.append({"date": day.isoformat(), "count": by_day.get(day.isoformat(), 0)})
        day += timedelta(days=1)
    views = sum(v["v"] for v in videos)
    shorts = sum(1 for v in videos if 0 < v["s"] <= SHORT_MAX_SECONDS)
    top = sorted(videos, key=lambda v: v["v"], reverse=True)[:8]
    return {
        "channel": snapshot.get("channel", {}), "fetched_at": snapshot.get("fetched_at", ""), "days": days,
        "published": len(videos), "shorts": shorts, "long_videos": len(videos) - shorts, "views": views,
        "average_views": round(views / len(videos)) if videos else 0,
        "likes": sum(v["l"] for v in videos), "comments": sum(v["c"] for v in videos), "per_day": per_day,
        "top": [{"id": v["id"], "title": v["t"], "published_at": v["p"], "views": v["v"], "likes": v["l"],
                 "is_short": 0 < v["s"] <= SHORT_MAX_SECONDS, "url": f"https://www.youtube.com/watch?v={v['id']}"}
                for v in top],
    }
