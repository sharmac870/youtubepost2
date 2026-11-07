"""News-led stories, Create from theme and channel analytics (ported from the YoutubePosts repo)."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ytp.analytics.channel import summarise
from ytp.common.config import Settings
from ytp.common.models import State
from ytp.common.queues import ALL_QUEUES
from ytp.common.runtime import build
from ytp.console import app as console
from ytp.discovery.stories import (Item, NICHES, StoryFinder, _public_https, cluster, parse_feed, story_fingerprint)
from ytp.fakes import fake_fetch, fake_services
from ytp.pipeline import Pipeline


def trends(rt):
    return {i["PartitionKey"]: rt.repo.json_row(i["PartitionKey"], "trend") for i in rt.repo.items()}


def test_one_news_story_and_one_youtube_trend_per_run(pipeline, rt):
    result = pipeline.discover("2026-11-07-slot1")
    assert len(result["created"]) == 2
    origins = sorted(t["origin"] for t in trends(rt).values())
    assert origins == ["news", "youtube"]
    news = next(t for t in trends(rt).values() if t["origin"] == "news")
    assert news["niche"] == "cyber" and "Ransomware" in news["title"]
    assert news["velocity_score"] >= 4                       # four or more distinct outlets
    assert "backup provider" in news["brief"]                # article facts from the publisher feed link
    assert "evil.example" not in json.dumps(news)            # URLs in untrusted summaries are stripped
    pipeline.work(ALL_QUEUES)
    assert sum(i["state"] == State.AWAITING_APPROVAL for i in rt.repo.items()) == 2


def test_news_story_is_not_repeated_in_the_next_run(pipeline, rt):
    pipeline.discover("2026-11-07-slot1")
    pipeline.discover("2026-11-07-slot2")
    titles = [t["title"] for t in trends(rt).values() if t["origin"] == "news"]
    assert len(titles) == 2 and len(set(titles)) == 2       # second run fell through to the tech niche


def test_news_can_be_turned_off(tmp_path):
    rt = build(Settings(_env_file=None, backend="local", data_dir=tmp_path, news_topics_per_run=0))
    Pipeline(rt, fake_services()).discover("k")
    assert {t["origin"] for t in trends(rt).values()} == {"youtube"}


def test_create_from_theme_through_the_console(tmp_path, monkeypatch):
    rt = build(Settings(_env_file=None, backend="local", data_dir=tmp_path, console_dev_auth=True))
    monkeypatch.setattr(console, "runtime", lambda: rt)
    client = TestClient(console.app)
    h = {"x-requested-with": "ytp"}
    assert client.post("/api/create", json={"theme": "x"}, headers=h).status_code == 422
    r = client.post("/api/create", json={"theme": "Monsoon arrival", "terms": ["monsoon Kerala"]}, headers=h)
    assert r.status_code == 200
    pipeline = Pipeline(rt, fake_services())
    pipeline.work(ALL_QUEUES)
    items = rt.repo.items()
    assert len(items) == 1 and items[0]["state"] == State.AWAITING_APPROVAL
    trend = rt.repo.json_row(items[0]["PartitionKey"], "trend")
    assert trend["origin"] == "theme" and trend["theme"] == "Monsoon arrival" and "Monsoon" in trend["title"]
    actions = [e["action"] for e in rt.repo.audit_events(items[0]["PartitionKey"])]
    assert "requested from theme" in actions


def test_theme_with_no_coverage_or_on_the_deny_list_creates_nothing(pipeline, rt):
    assert pipeline.discover_theme({"run_key": "t1", "theme": "quantum gardening tips"})["status"] == "not_found"
    assert pipeline.discover_theme({"run_key": "t2", "theme": "exit poll results"})["status"] == "denied"
    assert rt.repo.items() == []
    assert {r["RowKey"]: r["status"] for r in rt.repo.runs()} == {"t1": "FAILED", "t2": "SKIPPED"}


def test_clustering_keeps_different_events_apart():
    now = datetime.now(timezone.utc)
    items = [Item(t, "", o, now) for t, o in [
        ("Ransomware attack hits Indian payments firm", "A"), ("Indian payments firm hit by ransomware attack", "B"),
        ("Ransomware attack on payments firm disrupts UPI", "C"), ("ISRO launches new satellite", "D"),
        ("ISRO satellite launch succeeds", "E")]]
    stories = sorted(cluster(items, NICHES["cyber"]), key=lambda s: s.score, reverse=True)
    assert len(stories[0].sources) == 3 and "ISRO" not in stories[0].headline
    assert story_fingerprint("Ransomware hits payments firm") == story_fingerprint("payments firm: ransomware hits")


def test_feed_parsing_strips_outlet_suffix_and_markup():
    items = parse_feed(fake_fetch("https://news.google.com/rss/search", {"q": "ransomware"}))
    assert items[0].source == "The Hindu" and not items[0].title.endswith("The Hindu")


def test_fetcher_refuses_private_addresses():
    assert _public_https("https://www.bleepingcomputer.com/feed/")
    for bad in ("http://example.com", "https://169.254.169.254/latest", "https://localhost/", "https://[::1]/",
                "https://metadata/", "https://example.com:8080/"):
        assert not _public_https(bad), bad


def test_finder_survives_a_failing_source():
    def broken(url, params=None, limit=0):
        raise OSError("down")
    assert StoryFinder(fetch=broken).stories(NICHES["cyber"], 36, datetime.now(timezone.utc)) == []


def test_channel_analytics_refresh_is_throttled_and_counts_quota(pipeline, rt):
    assert pipeline.refresh_channel() is True
    assert pipeline.refresh_channel() is False               # fresh enough
    assert rt.repo.ledger(__import__("ytp.common.repo", fromlist=["quota_day"]).quota_day(), "youtube_units") >= 3
    view = summarise(rt.repo.channel_snapshot(), 30)
    assert view["channel"]["subscribers"] == 1200
    assert view["published"] == 10 and view["shorts"] + view["long_videos"] == 10
    assert len(view["per_day"]) == 31 and view["top"][0]["views"] == 10000


def test_channel_endpoint(tmp_path, monkeypatch):
    rt = build(Settings(_env_file=None, backend="local", data_dir=tmp_path, console_dev_auth=True))
    monkeypatch.setattr(console, "runtime", lambda: rt)
    client = TestClient(console.app)
    assert client.get("/api/channel?days=7").json() == {"ready": False}
    Pipeline(rt, fake_services()).refresh_channel()
    r = client.get("/api/channel?days=7").json()
    assert r["ready"] and r["published"] == 3 and client.get("/api/channel?days=5").status_code == 422


def test_summary_ignores_old_videos():
    now = datetime.now(timezone.utc)
    snap = {"videos": [{"id": "a", "t": "old", "p": (now - timedelta(days=40)).isoformat(), "s": 30, "v": 5, "l": 0, "c": 0}]}
    assert summarise(snap, 30)["published"] == 0
