"""News-led story finding, ported from the YoutubePosts repo (src/trends/fetcher.py).

A story is a group of headlines about the same event. Its score is how many distinct outlets cover it
(plus a small bonus for size). Niches are tried in the order the owner configures (cyber, tech, business,
breaking news by default); a niche counts only when its best story is covered by enough outlets.

The same finder serves the console's "Create from theme" form: the owner's search terms become a one-off
niche with a minimum of one outlet.

Everything fetched here is untrusted data. Headlines and summaries are sanitised into TrendPayload, and
the only article text kept is a capped, URL-free excerpt for the script writer's facts. Nothing from a
YouTube video (audio, transcript, frames) is used.
"""
from __future__ import annotations

import hashlib
import html
import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlparse

import httpx

from ..common.errors import http_client
from ..common.models import TrendPayload, sanitize

log = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 3000
MAX_FEED_BYTES = 3_000_000
MAX_PAGE_BYTES = 2_000_000


@dataclass(frozen=True)
class Niche:
    key: str
    label: str
    queries: tuple[str, ...]                 # Google News searches
    feeds: tuple[str, ...] = ()              # publisher RSS feeds with direct article links
    sections: tuple[str, ...] = ()           # Google News sections; "" is Top Stories
    category_id: str = "25"                  # YouTube category the Short is filed under
    keywords: tuple[str, ...] = ()           # a story must mention one of these to count
    min_sources: int = 3


CYBER_WORDS = (
    "cyber", "hack", "hacker", "hacked", "breach", "ransomware", "malware", "vulnerability", "zero-day",
    "0-day", "exploit", "phishing", "cve", "patch", "spyware", "botnet", "ddos", "leak", "leaked",
    "backdoor", "trojan", "infostealer", "stolen data", "data theft", "cert-in", "deepfake",
    "jailbreak", "prompt injection", "security flaw", "scam", "credential", "upi fraud", "digital arrest",
)
TECH_WORDS = (
    "ai", "openai", "anthropic", "google", "apple", "microsoft", "nvidia", "meta", "chip", "gpu", "model",
    "robot", "iphone", "android", "tesla", "spacex", "isro", "quantum", "startup", "software", "app", "llm",
    "tech", "5g", "upi", "semiconductor",
)

NICHES: dict[str, Niche] = {n.key: n for n in (
    Niche("cyber", "Cybersecurity",
          ("cybersecurity India", "data breach", "ransomware attack", "zero-day vulnerability", "cyber fraud India",
           "AI security"),
          feeds=("https://feeds.feedburner.com/TheHackersNews", "https://www.bleepingcomputer.com/feed/",
                 "https://www.securityweek.com/feed/"),
          category_id="28", keywords=CYBER_WORDS),
    Niche("tech", "Tech & AI", ("artificial intelligence", "OpenAI", "Nvidia", "smartphone launch India", "ISRO"),
          feeds=("https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml"),
          sections=("TECHNOLOGY",), category_id="28", keywords=TECH_WORDS),
    Niche("business", "Business & Economy", ("Sensex Nifty", "RBI", "Indian economy", "startup funding India"),
          sections=("BUSINESS",), category_id="25"),
    Niche("viral", "Breaking", ("India news",), sections=("",), category_id="25", min_sources=2),
)}


@dataclass
class Item:
    title: str
    url: str
    source: str
    published: datetime | None
    summary: str = ""


@dataclass
class Story:
    niche: Niche
    headline: str
    items: list[Item]
    score: float
    key_words: set[str] = field(default_factory=set)
    article_text: str = ""

    @property
    def sources(self) -> list[str]:
        return sorted({outlet(i.source) for i in self.items if i.source})


# ----- parsing -----


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_feed(data: bytes, default_source: str = "") -> list[Item]:
    """RSS 2.0 and Atom. Entity expansion is not a risk: ElementTree does not fetch external entities."""
    root = ET.fromstring(data)
    atom = "{http://www.w3.org/2005/Atom}"
    items = []
    for node in root.iter("item"):
        title = strip_html(node.findtext("title", ""))
        source = node.findtext("source") or default_source
        if node.find("source") is not None and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]   # Google News appends " - Outlet"
        items.append(Item(title, (node.findtext("link") or "").strip(), strip_html(source),
                          parse_date(node.findtext("pubDate")), strip_html(node.findtext("description", ""))[:600]))
    for node in root.iter(f"{atom}entry"):
        link = node.find(f"{atom}link")
        items.append(Item(strip_html(node.findtext(f"{atom}title", "")), link.get("href", "") if link is not None else "",
                          default_source, parse_date(node.findtext(f"{atom}updated") or node.findtext(f"{atom}published")),
                          strip_html(node.findtext(f"{atom}summary", "") or node.findtext(f"{atom}content", ""))[:600]))
    return [i for i in items if i.title]


class _TextExtractor(HTMLParser):
    """Paragraph text, skipping scripts, navigation and footers."""

    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "figure"}

    def __init__(self):
        super().__init__()
        self.depth_skip, self.in_p = 0, False
        self.paragraphs: list[str] = []
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth_skip += 1
        elif tag == "p" and not self.depth_skip:
            self.in_p, self._buffer = True, []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag == "p" and self.in_p:
            self.in_p = False
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            if len(text) > 60:
                self.paragraphs.append(text)

    def handle_data(self, data):
        if self.in_p and not self.depth_skip:
            self._buffer.append(data)


# ----- clustering -----

STOPWORDS = set("""
a an the and or but of to in on for with by at from as is are was were be been it its this that these those
after over into about new says say said how why what who will can could may might than more most just now
amid against up out off not no all you your we our they their his her he she them i report reports update
""".split())


def stem(word: str) -> str:
    if len(word) > 5:
        for suffix in ("ing", "ed", "es"):
            if word.endswith(suffix):
                return word[: -len(suffix)]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def significant_words(text: str) -> set[str]:
    return {stem(w) for w in re.findall(r"[a-z0-9][a-z0-9\-']+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def outlet(source: str) -> str:
    """'BleepingComputer' and 'bleepingcomputer.com' are the same outlet."""
    name = re.sub(r"\.(com|net|org|co\.uk|co\.in|in|io)$", "", source.lower().strip())
    return re.sub(r"[^a-z0-9]", "", name)


def matches(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = f" {text.lower()} "
    return any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", lowered) for k in keywords)


def word_weights(items: list[Item]) -> tuple[dict[str, float], set[str]]:
    """Inverse document frequency of each headline word, and the words too common today to identify an event."""
    counts: dict[str, int] = {}
    for item in items:
        for word in significant_words(item.title):
            counts[word] = counts.get(word, 0) + 1
    total = max(1, len(items))
    weights = {w: math.log((total + 1) / c) for w, c in counts.items()}
    generic = {w for w, c in counts.items() if c > max(4, total * 0.06)}
    return weights, generic


def similarity(words: set[str], seed: set[str], weights: dict[str, float]) -> float:
    shared = sum(weights.get(w, 0.0) for w in words & seed)
    smaller = min(sum(weights.get(w, 0.0) for w in words), sum(weights.get(w, 0.0) for w in seed))
    return shared / smaller if smaller else 0.0


def cluster(items: list[Item], niche: Niche, threshold: float = 0.2) -> list[Story]:
    """Groups headlines about the same event.

    Each headline is compared with a group's first headline (a growing union of words lets unrelated
    stories chain together), rare words like company names weigh far more than common ones, and a match
    needs two words that aren't common today or one rare one."""
    weights, generic = word_weights(items)
    rare = math.log((len(items) + 1) / 3)
    groups: list[tuple[set[str], list[Item]]] = []
    for item in items:
        words = significant_words(item.title)
        if len(words) < 2:
            continue
        for seed, members in groups:
            shared = (words & seed) - generic
            telling = len(shared) >= 2 or any(weights.get(w, 0.0) >= rare for w in shared)
            if telling and similarity(words, seed, weights) >= threshold:
                members.append(item)
                break
        else:
            groups.append((words, [item]))
    stories = []
    for seed, members in groups:
        sources = {outlet(m.source) for m in members if m.source}
        score = len(sources) + 0.1 * len(members)
        members.sort(key=lambda m: (similarity(significant_words(m.title), seed, weights), bool(m.summary)),
                     reverse=True)
        stories.append(Story(niche, members[0].title, members, score, key_words=seed - generic))
    return stories


def story_fingerprint(headline: str) -> str:
    words = sorted(significant_words(headline))[:6]
    return "n" + hashlib.sha256(" ".join(words).encode()).hexdigest()[:31]


def overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, min(len(a), len(b)))


# ----- finder -----


Fetch = Callable[[str, dict | None, int], bytes]


def _public_https(url: str) -> bool:
    """https to a named public host only: no IP literals, localhost or internal names (redirects included)."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or "." not in host or parts.port not in (None, 443):
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    return not re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host)


def http_fetch(client: httpx.Client | None = None) -> Fetch:
    client = client or http_client(connect=5, read=20)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ytp-trends/2.0)", "Accept-Language": "en-IN,en"}

    def fetch(url: str, params: dict | None = None, limit: int = MAX_FEED_BYTES) -> bytes:
        for _ in range(4):
            if not _public_https(url):
                raise ValueError("refusing a non-public or non-https address")
            with client.stream("GET", url, params=params, headers=headers, follow_redirects=False) as response:
                if response.is_redirect:
                    url, params = str(response.url.join(response.headers.get("location", ""))), None
                    continue
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        break
                return bytes(body[:limit])
        raise ValueError("too many redirects")

    return fetch


class StoryFinder:
    def __init__(self, fetch: Fetch | None = None, hl: str = "en-IN", gl: str = "IN", fetch_articles: bool = True):
        self.fetch = fetch or http_fetch()
        self.hl, self.gl = hl, gl
        self.fetch_articles = fetch_articles

    def _locale(self) -> dict:
        return {"hl": self.hl, "gl": self.gl, "ceid": f"{self.gl}:{self.hl.split('-')[0]}"}

    def _safe(self, name: str, fn) -> list[Item]:
        try:
            items = fn()
            log.info("%s: %d items", name, len(items))
            return items
        except Exception as exc:  # noqa: BLE001 - one bad source never stops discovery
            log.warning("%s failed: %s", name, exc.__class__.__name__)
            return []

    def collect(self, niche: Niche, hours: int, at: datetime) -> list[Item]:
        when = f"when:{hours}h" if hours < 48 else f"when:{max(1, hours // 24)}d"
        items: list[Item] = []
        for q in niche.queries:
            items += self._safe(f"news:{q}", lambda q=q: parse_feed(self.fetch(
                "https://news.google.com/rss/search", {"q": f"{q} {when}", **self._locale()}, MAX_FEED_BYTES)))
        for section in niche.sections:
            url = "https://news.google.com/rss" + (f"/headlines/section/topic/{section}" if section else "")
            items += self._safe(f"news:{section or 'top'}", lambda u=url: parse_feed(self.fetch(u, self._locale(), MAX_FEED_BYTES)))
        for url in niche.feeds:
            host = urlparse(url).netloc.replace("www.", "").replace("feeds.", "")
            items += self._safe(f"feed:{host}", lambda u=url, h=host: parse_feed(self.fetch(u, None, MAX_FEED_BYTES), h))
        cutoff = at - timedelta(hours=hours)
        fresh = [i for i in items if i.published is None or i.published >= cutoff]
        if niche.keywords:
            fresh = [i for i in fresh if matches(f"{i.title} {i.summary}", niche.keywords)]
        return fresh

    def stories(self, niche: Niche, hours: int, at: datetime, skip: Callable[[Story], bool] = lambda s: False) -> list[Story]:
        """This niche's stories that are covered widely enough, best first."""
        found = [s for s in cluster(self.collect(niche, hours, at), niche) if not skip(s)]
        found.sort(key=lambda s: s.score, reverse=True)
        return [s for s in found if len(s.sources) >= niche.min_sources]

    def article_text(self, story: Story) -> str:
        """The first directly linked article that is clearly about this event. Google News links are redirects
        to unknown hosts, so only publisher-feed links are read."""
        if not self.fetch_articles:
            return ""
        for item in story.items[:6]:
            if not item.url.startswith("https://") or "news.google.com" in item.url:
                continue
            if story.key_words and len(significant_words(item.title) & story.key_words) < min(2, len(story.key_words)):
                continue
            try:
                parser = _TextExtractor()
                parser.feed(self.fetch(item.url, None, MAX_PAGE_BYTES).decode("utf-8", errors="replace"))
                text = " ".join(parser.paragraphs)
                if len(text) > 400:
                    return text[:MAX_ARTICLE_CHARS]
            except Exception as exc:  # noqa: BLE001
                log.info("article fetch failed: %s", exc.__class__.__name__)
        return ""


def theme_niche(theme: str, terms: list[str]) -> Niche:
    queries = tuple(t.strip()[:80] for t in terms if t.strip())[:4] or (theme.strip()[:80],)
    return Niche("theme", sanitize(theme, 40) or "Theme", queries, category_id="25", min_sources=1)


def to_payload(story: Story, at: datetime, origin: str = "news", theme: str = "") -> TrendPayload:
    published = max((i.published for i in story.items if i.published), default=at)
    news = [{"title": sanitize(i.title, 200), "source": sanitize(i.source, 80), "url": i.url,
             "published": i.published.isoformat() if i.published else ""}
            for i in story.items if i.url.startswith("https://")][:8]
    lead = next((i.summary for i in story.items if i.summary), "")
    return TrendPayload(
        origin=origin, title=story.headline, description=lead, channel_title=story.niche.label,
        category_id=story.niche.category_id, published_at=published, velocity_score=round(story.score, 2),
        niche=story.niche.key, theme=theme, brief=story.article_text, news=news,
        tags=sorted(story.key_words)[:10], fingerprint=story_fingerprint(story.headline))


def configured_niches(keys: list[str], min_sources: int | None = None) -> list[Niche]:
    out = [NICHES[k] for k in keys if k in NICHES]
    return [replace(n, min_sources=min_sources) for n in out] if min_sources else out
