import pytest
from fastapi.testclient import TestClient

from ytp.common.config import Settings
from ytp.common.models import State
from ytp.common.queues import ALL_QUEUES
from ytp.common.runtime import build
from ytp.console import app as console
from ytp.fakes import fake_services
from ytp.pipeline import Pipeline


def make(tmp_path, monkeypatch, **kw):
    settings = Settings(_env_file=None, backend="local", data_dir=tmp_path, **{"platforms": ["YOUTUBE", "INSTAGRAM"], **kw})
    rt = build(settings)
    monkeypatch.setattr(console, "runtime", lambda: rt)
    return rt, TestClient(console.app)


def test_sign_in_is_required_and_allow_list_fails_closed(tmp_path, monkeypatch):
    _, client = make(tmp_path, monkeypatch)
    assert client.get("/api/summary").status_code == 403                     # no identity header
    who = {"x-ms-client-principal-name": "someone@outlook.com", "x-ms-client-principal-id": "abc"}
    assert client.get("/api/summary", headers=who).status_code == 403        # empty allow-list admits nobody
    assert client.get("/healthz").status_code == 200


def test_allowed_user_and_csrf(tmp_path, monkeypatch):
    rt, client = make(tmp_path, monkeypatch, allowed_users=["abc"])
    who = {"x-ms-client-principal-name": "me@outlook.com", "x-ms-client-principal-id": "abc"}
    r = client.get("/api/summary?days=7", headers=who)
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("default-src 'self'")
    assert client.post("/api/discover", headers=who).status_code == 403      # no CSRF header
    assert client.post("/api/discover", headers={**who, "x-requested-with": "ytp"}).status_code == 200
    # Behind ingress the Host header is internal; same-origin browser requests must still pass.
    same = {**who, "x-requested-with": "ytp", "origin": "https://console.example", "host": "localhost:8000",
            "sec-fetch-site": "same-origin"}
    assert client.post("/api/discover", headers=same).status_code == 200
    cross = {**same, "sec-fetch-site": "cross-site"}
    assert client.post("/api/discover", headers=cross).status_code == 403


def test_review_flow_through_the_api(tmp_path, monkeypatch):
    rt, client = make(tmp_path, monkeypatch, console_dev_auth=True)
    pipeline = Pipeline(rt, fake_services())
    pipeline.discover("k")
    pipeline.work(ALL_QUEUES)
    items = client.get("/api/items?state=AWAITING_APPROVAL").json()["items"]
    assert len(items) == 2
    cid = items[0]["id"]
    detail = client.get(f"/api/items/{cid}").json()
    assert detail["can_edit"] and detail["script"]["script"]["scenes"]
    h = {"x-requested-with": "ytp"}
    r = client.post(f"/api/items/{cid}/edit", json={"title": "Edited title that is long enough"}, headers=h)
    assert r.status_code == 200
    assert client.post(f"/api/items/{cid}/approve", json={}, headers=h).status_code == 200
    assert client.post(f"/api/items/{cid}/approve", json={}, headers=h).status_code == 409   # already approved
    pipeline.work(ALL_QUEUES)
    assert rt.repo.item(cid)["state"] == State.PUBLISHED
    video = client.get(f"/api/items/{cid}/video", headers={"range": "bytes=0-99"})
    assert video.status_code == 206 and len(video.content) == 100
    summary = client.get("/api/summary?days=7").json()
    assert summary["published"] == {"YOUTUBE": 1, "INSTAGRAM": 1}
    assert client.post("/api/settings", json={"kill_switch": True}, headers=h).status_code == 200
    assert client.get("/api/health").json()["kill_switch"] is True
