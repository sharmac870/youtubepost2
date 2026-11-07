"""TrendDiscoveryService (LLD Sections 4.1, 5.1 and Step 4).

Only the official YouTube Data API is used: videos.list with chart=mostPopular and regionCode=IN, one
call per allow-listed category (1 quota unit each). Trending videos are a topic signal only: nothing of
theirs (video, audio, transcript, thumbnail) is downloaded or reused.

Optional context comes from Google News RSS search, which Google publishes for feed readers; its
headlines and links become the script's citable sources.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from ..common.config import Settings
from ..common.errors import Permanent, Transient, http_client, raise_for_http, with_retries
from ..common.models import TrendPayload, sanitize
from ..common.repo import Repo, quota_day
from ..common.secrets import Secrets
from ..policy.gate import deny_matches

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
STOPWORDS = set("""a an the and or but of in on at to for from with by is are was were be been it its this that these
those as into about over after before new latest video official full live episode ep part trailer teaser song songs
lyrical lyric shorts short reel reels viral today news hindi english tamil telugu vs v s ka ki ke hai se ko mein
2024 2025 2026 2027 hd 4k 1080p watch now day highlights""".split())
CATEGORY_NAMES = {"1": "Film & Animation", "2": "Autos", "10": "Music", "15": "Pets", "17": "Sports", "19": "Travel",
                  "20": "Gaming", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
                  "25": "News & Politics", "26": "Howto & Style", "27": "Education", "28": "Science & Technology"}


class TrendSource(Protocol):
    def fetch_trending(self, region: str, category_id: str, max_results: int) -> list[dict]: ...


class NewsSource(Protocol):
    def search(self, query: str, limit: int = 6) -> list[dict]: ...


def key_phrases(title: str, tags: list[str] | None = None) -> list[str]:
    text = unicodedata.normalize("NFKC", title).lower()
    text = re.sub(r"#\w+|@\w+|https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    words = [w for w in text.split() if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]
    seen: list[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return seen[:6]


def fingerprint(title: str, tags: list[str] | None = None) -> str:
    """SHA-256 of the normalised key phrases (LLD 5.1 step 3). Word order does not matter."""
    phrases = sorted(key_phrases(title, tags)[:5])
    return hashlib.sha256(" ".join(phrases).encode()).hexdigest()[:32]


def similar(a: str, b: str) -> bool:
    x, y = set(key_phrases(a)), set(key_phrases(b))
    return bool(x and y) and len(x & y) / len(x | y) >= 0.5


def velocity(view_count: int, published_at: datetime, at: datetime) -> float:
    hours = max((at - published_at).total_seconds() / 3600, 1.0)
    return view_count / hours


class YouTubeTrends:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or http_client(connect=5, read=30)

    def fetch_trending(self, region, category_id, max_results):
        params = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": region,
                  "videoCategoryId": category_id, "maxResults": max_results, "key": self.api_key}

        def call():
            try:
                response = self.client.get(f"{API}/videos", params=params)
            except httpx.TransportError as exc:
                raise Transient(f"videos.list: {exc.__class__.__name__}") from exc
            if response.status_code == 404 or (response.status_code == 400 and "notFound" in response.text):
                return {"items": []}  # this category has no chart in the region
            raise_for_http(response, "videos.list")
            return response.json()

        return with_retries(call, attempts=3, base=2).get("items", [])


class GoogleNews:
    URL = "https://news.google.com/rss/search"

    def __init__(self, client: httpx.Client | None = None, hl: str = "en-IN", gl: str = "IN"):
        self.client = client or http_client(connect=5, read=20)
        self.hl, self.gl = hl, gl

    def search(self, query, limit=6):
        try:
            response = self.client.get(self.URL, params={"q": f"{query} when:2d", "hl": self.hl, "gl": self.gl,
                                                         "ceid": f"{self.gl}:{self.hl.split('-')[0]}"})
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except Exception as exc:  # noqa: BLE001 - context is optional
            log.info("news search failed for %r: %s", query, exc)
            return []
        out = []
        for item in root.iter("item"):
            title = sanitize(item.findtext("title") or "", 200)
            link = (item.findtext("link") or "").strip()
            source = sanitize(item.findtext("source") or "", 80)
            published = item.findtext("pubDate") or ""
            try:
                published = parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published = ""
            if title and link.startswith("https://"):
                out.append({"title": title, "source": source, "url": link, "published": published})
            if len(out) >= limit:
                break
        return out


class TrendDiscoveryService:
    def __init__(self, settings: Settings, repo: Repo, source: TrendSource | None = None,
                 news: NewsSource | None = None, secrets: Secrets | None = None, stories=None):
        self.settings = settings
        self.repo = repo
        self._source, self._secrets = source, secrets
        self.news = news if news is not None else (GoogleNews() if settings.news_context else None)
        self._stories = stories

    @property
    def source(self) -> TrendSource:
        if self._source is None:
            self._source = YouTubeTrends((self._secrets or Secrets()).get("youtube-api-key"))
        return self._source

    @property
    def stories(self):
        if self._stories is None:
            from .stories import StoryFinder

            self._stories = StoryFinder(fetch_articles=self.settings.fetch_articles)
        return self._stories

    def news_candidates(self, at: datetime, wanted: int) -> list[TrendPayload]:
        """The best story of each niche, in hierarchy order, until `wanted` are found (ported from YoutubePosts)."""
        from .stories import configured_niches, overlap, significant_words, to_payload

        out: list[TrendPayload] = []
        for niche in configured_niches(self.settings.news_niches):
            if len(out) >= wanted:
                break

            def skip(story, chosen=out):
                text = f"{story.headline} {' '.join(i.title for i in story.items[:5])}"
                if deny_matches(text, self.settings.deny_topics):
                    return True
                if self.repo.topic_seen(_story_fp(story.headline), self.settings.topic_dedupe_days):
                    return True
                words = significant_words(story.headline)
                return any(overlap(words, significant_words(c.title)) >= 0.5 for c in chosen)

            found = self.stories.stories(niche, self.settings.news_lookback_hours, at, skip)
            if found:
                story = found[0]
                story.article_text = self.stories.article_text(story)
                out.append(to_payload(story, at))
                log.info("news niche %s: %r (%d outlets)", niche.key, story.headline, len(story.sources))
        return out

    def candidates(self, at: datetime) -> list[TrendPayload]:
        out: list[TrendPayload] = []
        seen_ids: set[str] = set()
        for category in self.settings.category_allowlist:
            if category == "10":
                continue  # Music: high copyright risk, never used (LLD 11.1)
            try:
                rows = self.source.fetch_trending(self.settings.region_code, category,
                                                  self.settings.max_results_per_category)
            except Permanent as exc:
                log.warning("category %s skipped: %s", category, exc)
                continue
            finally:
                self.repo.ledger_add(quota_day(at), "youtube_units", 1)
            for row in rows:
                trend = self._payload(row, category, at)
                if trend and trend.source_video_id not in seen_ids:
                    seen_ids.add(trend.source_video_id)
                    out.append(trend)
        out.sort(key=lambda t: t.velocity_score, reverse=True)
        return out

    def _payload(self, row: dict, category: str, at: datetime) -> TrendPayload | None:
        snippet, stats = row.get("snippet", {}), row.get("statistics", {})
        if snippet.get("liveBroadcastContent") not in (None, "none"):
            return None
        if snippet.get("channelId") in self.settings.deny_channel_ids:
            return None
        try:
            published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
            views = int(stats.get("viewCount", 0))
            trend = TrendPayload(
                source_video_id=row["id"], title=snippet.get("title", ""), description=snippet.get("description", ""),
                channel_id=snippet.get("channelId", ""), channel_title=snippet.get("channelTitle", ""),
                category_id=snippet.get("categoryId") or category, view_count=views,
                like_count=int(stats.get("likeCount", 0) or 0), published_at=published,
                velocity_score=round(velocity(views, published, at), 2), tags=snippet.get("tags", [])[:30])
        except (KeyError, ValueError) as exc:
            log.info("skipping malformed trend %s: %s", row.get("id"), exc)
            return None
        if not key_phrases(trend.title):
            return None
        trend.fingerprint = fingerprint(trend.title, trend.tags)
        return trend

    def run(self, run_key: str, kind: str = "scheduled", at: datetime | None = None) -> dict:
        at = at or datetime.now(timezone.utc)
        if not self.repo.claim_run(run_key, kind):
            log.info("run %s already claimed; exiting", run_key)
            return {"status": "duplicate", "run": run_key}
        created, skipped, found = [], 0, 0
        try:
            news_n = min(self.settings.news_topics_per_run, self.settings.top_n_topics)
            chosen: list[TrendPayload] = []
            if news_n:
                try:
                    chosen = self.news_candidates(at, news_n)
                except Exception as exc:  # noqa: BLE001 - news-led stories are optional
                    log.warning("news-led discovery failed: %s", exc)
            found = len(chosen)
            yt_wanted = self.settings.top_n_topics - len(chosen)
            candidates = self.candidates(at) if yt_wanted > 0 else []
            found += len(candidates)
            yt_chosen: list[TrendPayload] = []
            for trend in candidates:
                if len(yt_chosen) >= yt_wanted:
                    break
                if deny_matches(f"{trend.title} {' '.join(trend.tags)}", self.settings.deny_topics):
                    skipped += 1
                    continue
                if self.repo.topic_seen(trend.fingerprint, self.settings.topic_dedupe_days) or \
                        any(similar(trend.title, c.title) for c in chosen + yt_chosen):
                    skipped += 1
                    continue
                yt_chosen.append(trend)
            chosen += yt_chosen
            for trend in chosen:
                if self.news is not None and not trend.news:
                    trend.news = self.news.search(" ".join(key_phrases(trend.title)[:4]))
                content_id = self.repo.create_item(trend.model_dump(mode="json"), run_key, self.settings.platforms)
                self.repo.mark_topic(trend.fingerprint, content_id)
                created.append(content_id)
            self.repo.finish_run(run_key, "SUCCEEDED", found=found, created=len(created), skipped=skipped)
        except Exception as exc:
            self.repo.finish_run(run_key, "FAILED", error=str(exc)[:500], found=found, created=len(created))
            raise
        log.info("run %s: %d candidates, %d created, %d skipped", run_key, found, len(created), skipped)
        return {"status": "ok", "run": run_key, "found": found, "created": created, "skipped": skipped}


    def run_theme(self, run_key: str, theme: str, terms: list[str], hours: int, actor: str,
                  at: datetime | None = None) -> dict:
        """One draft about a theme the owner typed in the console ("Create from theme", from YoutubePosts' Studio).
        It goes through the same script, policy, approval and publishing steps as everything else."""
        from .stories import overlap, significant_words, theme_niche, to_payload

        at = at or datetime.now(timezone.utc)
        if not self.repo.claim_run(run_key, "theme"):
            return {"status": "duplicate", "run": run_key}
        try:
            if deny_matches(f"{theme} {' '.join(terms)}", self.settings.deny_topics):
                self.repo.finish_run(run_key, "SKIPPED", error="theme matches the deny list", found=0, created=0)
                return {"status": "denied", "run": run_key}
            niche = theme_niche(theme, terms)
            words = significant_words(f"{theme} {' '.join(terms)}")

            def skip(story):
                return deny_matches(story.headline, self.settings.deny_topics)

            found = self.stories.stories(niche, hours, at, skip)
            # Prefer stories that actually share words with the theme, then the most covered.
            found.sort(key=lambda s: (overlap(words, significant_words(s.headline)) > 0, s.score), reverse=True)
            if not found:
                self.repo.finish_run(run_key, "FAILED", error="no recent coverage for this theme", found=0, created=0)
                return {"status": "not_found", "run": run_key}
            story = found[0]
            story.article_text = self.stories.article_text(story)
            trend = to_payload(story, at, origin="theme", theme=theme)
            content_id = self.repo.create_item(trend.model_dump(mode="json"), run_key, self.settings.platforms)
            self.repo.audit(content_id, actor, "requested from theme", {"theme": trend.theme})
            self.repo.mark_topic(trend.fingerprint, content_id)
            self.repo.finish_run(run_key, "SUCCEEDED", found=len(found), created=1, skipped=0)
            return {"status": "ok", "run": run_key, "created": [content_id]}
        except Exception as exc:
            self.repo.finish_run(run_key, "FAILED", error=str(exc)[:500], found=0, created=0)
            raise


def _story_fp(headline: str) -> str:
    from .stories import story_fingerprint

    return story_fingerprint(headline)


def run_key_for(at: datetime, slots_per_day: int = 4) -> str:
    slot = at.hour * slots_per_day // 24 + 1
    return f"{at:%Y-%m-%d}-slot{slot}"


def is_recent(published: str, hours: int = 72) -> bool:
    try:
        return datetime.fromisoformat(published) >= datetime.now(timezone.utc) - timedelta(hours=hours)
    except ValueError:
        return False
