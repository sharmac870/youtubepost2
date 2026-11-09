"""Review Console and tracking dashboard (LLD Section 4.1 and Step 8).

One small FastAPI app serving a static single-page UI and a JSON API. In Azure it runs as a Container App
that scales to zero, behind Container Apps built-in authentication (Microsoft Entra ID): nothing reaches
this code before sign-in, and the app then checks the signed-in user against ALLOWED_USERS.

The console holds no platform credentials. Approving, retrying or unpublishing only changes state and puts
a message on a queue; the workers, which hold the tokens, do the rest.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..common.models import PubState, State
from ..common.queues import ALL_QUEUES, Q_DISCOVER, Q_PUBLISH, poison
from ..common.repo import ITEMS, StaleState, ist_day, now, quota_day
from ..common.runtime import Runtime, default
from ..discovery.service import CATEGORY_NAMES

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="youtubeposts2 console", docs_url=None, redoc_url=None, openapi_url=None)


def runtime() -> Runtime:
    return default()


# ----- security -----

@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    if path != "/healthz":
        rt = runtime()
        user = current_user(request, rt)
        if user is None:
            log.warning("refused %s %s: signed-in user %r (id %r) is not in ALLOWED_USERS", request.method, path,
                        request.headers.get("x-ms-client-principal-name"), request.headers.get("x-ms-client-principal-id"))
            return JSONResponse({"error": "not allowed"}, status_code=403)
        request.state.user = user
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # CSRF: a custom header can't be sent cross-site without a CORS preflight, which this app never
            # allows; browsers also say where a request came from in Sec-Fetch-Site. The Host header is not
            # compared, because behind Container Apps ingress and built-in auth it is not the public name.
            site = request.headers.get("sec-fetch-site", "same-origin")
            if request.headers.get("x-requested-with") != "ytp" or site not in ("same-origin", "none"):
                log.warning("refused %s %s: x-requested-with=%r sec-fetch-site=%r", request.method, path,
                            request.headers.get("x-requested-with"), site)
                return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    response = await call_next(request)
    response.headers.update({
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
                                   "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                                   "form-action 'self'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
        "Cache-Control": response.headers.get("Cache-Control", "no-store"),
    })
    return response


def current_user(request: Request, rt: Runtime) -> dict | None:
    s = rt.settings
    if s.console_dev_auth and s.backend == "local":
        return {"name": "developer@local", "admin": True}
    name = (request.headers.get("x-ms-client-principal-name") or "").lower()
    oid = (request.headers.get("x-ms-client-principal-id") or "").lower()
    if not name and not oid:
        return None
    allowed = {u.lower() for u in s.allowed_users}
    if not allowed or not ({name, oid} & allowed):   # fail closed: an empty allow-list admits nobody
        return None
    admins = {u.lower() for u in s.admin_users} or allowed
    return {"name": name or oid, "admin": bool({name, oid} & admins)}


def user(request: Request) -> dict:
    return request.state.user


def admin(request: Request) -> dict:
    if not request.state.user.get("admin"):
        raise HTTPException(403, "admin role required")
    return request.state.user


def pipeline(rt: Runtime):
    from ..pipeline import Pipeline, Services

    # The console never calls external services itself, so every factory is a guard.
    def nope(*_a, **_k):
        raise RuntimeError("the console does not call external services")

    return Pipeline(rt, Services(nope, nope, nope, nope, nope, nope, nope))


# ----- data shaping -----

def _rows(rt: Runtime, prefix: str) -> list[dict]:
    return [r for r in rt.store.query(ITEMS, rk_prefix=prefix)]


def _day(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(IST).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def snapshot(rt: Runtime) -> list[dict]:
    """Every item with its publications and latest metrics, in two range scans plus one metrics scan."""
    items = {r["PartitionKey"]: r for r in _rows(rt, "item") if r["RowKey"] == "item"}
    pubs: dict[str, dict] = defaultdict(dict)
    for row in _rows(rt, "pub:"):
        pubs[row["PartitionKey"]][row["platform"]] = row
    metrics: dict[str, dict] = defaultdict(dict)
    for row in rt.repo.all_metrics():
        metrics[row["PartitionKey"]][row["RowKey"]] = row
    out = []
    for cid, item in items.items():
        p = {}
        for platform in rt.settings.platforms:
            pub, m = pubs[cid].get(platform), metrics[cid].get(platform, {})
            if pub or platform in (item.get("platforms") or ""):
                p[platform] = {"state": pub["state"] if pub else "", "url": (pub or {}).get("url", ""),
                               "published_at": (pub or {}).get("published_at", ""),
                               "error": (pub or {}).get("last_error", ""), "views": int(m.get("views", 0) or 0),
                               "likes": int(m.get("likes", 0) or 0), "comments": int(m.get("comments", 0) or 0)}
        out.append({
            "id": cid, "title": item.get("title", ""), "state": item["state"], "category_id": item.get("category_id", ""),
            "category": CATEGORY_NAMES.get(item.get("category_id", ""), item.get("category_id", "")),
            "created_at": item.get("created_at", ""), "updated_at": item.get("updated_at", ""),
            "published_at": item.get("published_at", ""), "expires_at": item.get("expires_at", ""),
            "policy": item.get("policy", ""), "risk_flags": item.get("risk_flags", ""),
            "approved_by": item.get("approved_by", ""), "error": item.get("last_error", ""),
            "velocity": item.get("velocity", 0), "platforms": p,
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


# ----- API -----

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/me")
def me(u: dict = Depends(user)):
    rt = runtime()
    return {**u, "backend": rt.settings.backend, "channel": rt.settings.yt_channel_handle,
            "instagram": rt.settings.ig_username}


@app.get("/api/summary")
def summary(days: int = 30, u: dict = Depends(user)):
    rt = runtime()
    days = max(1, min(days, 365))
    start = (now() - timedelta(days=days - 1)).astimezone(IST).strftime("%Y-%m-%d")
    data = snapshot(rt)
    calendar = [(now() - timedelta(days=days - 1 - i)).astimezone(IST).strftime("%Y-%m-%d") for i in range(days)]
    posts = {p: dict.fromkeys(calendar, 0) for p in ("YOUTUBE", "INSTAGRAM")}
    views = {p: 0 for p in posts}
    likes = {p: 0 for p in posts}
    published = {p: 0 for p in posts}
    created = 0
    by_category: Counter = Counter()
    for item in data:
        if _day(item["created_at"]) >= start:
            created += 1
        for platform, pub in item["platforms"].items():
            day = _day(pub["published_at"])
            if pub["state"] == PubState.LIVE and day >= start:
                published[platform] += 1
                posts[platform][day] = posts[platform].get(day, 0) + 1
                views[platform] += pub["views"]
                likes[platform] += pub["likes"]
                by_category[item["category"] or "Other"] += 1
    states = Counter(i["state"] for i in data)
    top = sorted((i for i in data if any(p["state"] == PubState.LIVE for p in i["platforms"].values())),
                 key=lambda i: sum(p["views"] for p in i["platforms"].values()), reverse=True)[:5]
    return {
        "days": days, "calendar": calendar, "posts_per_day": posts, "published": published, "views": views,
        "likes": likes, "drafts_created": created, "states": states, "by_category": by_category.most_common(8),
        "awaiting_approval": states.get(State.AWAITING_APPROVAL, 0),
        "needs_attention": states.get(State.PARTIALLY_PUBLISHED, 0) + states.get(State.FAILED, 0),
        "in_flight": sum(states.get(s, 0) for s in (State.APPROVED, State.RENDERING, State.RENDERED, State.QA_PASSED,
                                                     State.PUBLISHING, State.DISCOVERED, State.SCRIPTED)),
        "top": [{"id": i["id"], "title": i["title"], "platforms": i["platforms"]} for i in top],
    }


@app.get("/api/items")
def items(state: str = "", q: str = "", platform: str = "", limit: int = 200, u: dict = Depends(user)):
    data = snapshot(runtime())
    states = {s for s in state.split(",") if s}
    if states:
        data = [i for i in data if i["state"] in states]
    if q:
        needle = q.lower()
        data = [i for i in data if needle in i["title"].lower() or needle in i["category"].lower()]
    if platform:
        data = [i for i in data if i["platforms"].get(platform, {}).get("state") == PubState.LIVE]
    return {"items": data[: max(1, min(limit, 1000))], "total": len(data)}


@app.get("/api/items/{cid}")
def item_detail(cid: str, u: dict = Depends(user)):
    rt = runtime()
    _check_id(cid)
    item = rt.repo.item(cid)
    if not item:
        raise HTTPException(404, "not found")
    version = int(item.get("script_version", 1) or 1)
    script = rt.repo.json_row(cid, f"script:{version:04d}")
    shot = next((i for i in snapshot_one(rt, cid)), None)
    return {
        "item": shot, "script": script, "script_versions": version, "trend": rt.repo.json_row(cid, "trend"),
        "policy": rt.repo.json_row(cid, "policy"), "render": rt.repo.json_row(cid, "render"),
        "qa": rt.repo.json_row(cid, "qa"),
        "attempts": [{k: v for k, v in a.items() if k not in ("PartitionKey", "etag")} for a in rt.repo.attempts(cid)],
        "audit": [{"at": e.get("at"), "actor": e.get("actor"), "action": e.get("action"),
                   "details": json.loads(e.get("details") or "{}")} for e in rt.repo.audit_events(cid)],
        "has_video": bool(item.get("media")), "can_edit": item["state"] in (State.AWAITING_APPROVAL, State.EXPIRED),
    }


def snapshot_one(rt: Runtime, cid: str) -> list[dict]:
    item = rt.repo.item(cid)
    pubs = rt.repo.pubs(cid)
    metrics = {r["RowKey"]: r for r in rt.store.query("metrics", pk=cid)}
    p = {}
    for platform, pub in pubs.items():
        m = metrics.get(platform, {})
        p[platform] = {"state": pub["state"], "url": pub.get("url", ""), "published_at": pub.get("published_at", ""),
                       "error": pub.get("last_error", ""), "attempts": pub.get("attempts", 0),
                       "views": int(m.get("views", 0) or 0), "likes": int(m.get("likes", 0) or 0),
                       "comments": int(m.get("comments", 0) or 0)}
    return [{"id": cid, "title": item.get("title", ""), "state": item["state"],
             "category": CATEGORY_NAMES.get(item.get("category_id", ""), ""), "created_at": item.get("created_at", ""),
             "expires_at": item.get("expires_at", ""), "approved_by": item.get("approved_by", ""),
             "error": item.get("last_error", ""), "platforms": p, "ref": item.get("ref", "")}]


def _check_id(cid: str) -> None:
    if not re.fullmatch(r"[0-9a-f-]{36}", cid):
        raise HTTPException(400, "bad id")


@app.get("/api/items/{cid}/video")
def video(cid: str, request: Request, u: dict = Depends(user)):
    rt = runtime()
    _check_id(cid)
    item = rt.repo.item(cid)
    if not item or not item.get("media"):
        raise HTTPException(404, "no video yet")
    name = item["media"]
    size = rt.media.size(name)
    start, end = 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if match and (match[1] or match[2]):
        if match[1]:
            start, end = int(match[1]), int(match[2]) if match[2] else size - 1
        else:
            start = max(0, size - int(match[2]))
        end = min(end, size - 1)
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=300"}
    status = 200
    if match:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(rt.media.stream(name, start, end - start + 1), status_code=status, media_type="video/mp4",
                             headers=headers)


@app.get("/api/items/{cid}/thumb")
def thumb(cid: str, u: dict = Depends(user)):
    rt = runtime()
    _check_id(cid)
    render = rt.repo.json_row(cid, "render")
    if not render or not render.get("thumb"):
        raise HTTPException(404, "no preview")
    data = b"".join(rt.media.stream(render["thumb"]))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


class Reason(BaseModel):
    reason: str = Field("", max_length=500)


class Edit(BaseModel):
    title: str | None = Field(None, max_length=95)
    description: str | None = Field(None, max_length=4500)
    caption: str | None = Field(None, max_length=2000)
    hook_text: str | None = Field(None, max_length=120)
    call_to_action: str | None = Field(None, max_length=120)
    scenes: list[dict] | None = None
    hashtags: list[str] | None = None


class Retry(BaseModel):
    platform: str | None = Field(None, pattern="^(YOUTUBE|INSTAGRAM)$")


class Unpublish(BaseModel):
    platform: str = Field(pattern="^(YOUTUBE|INSTAGRAM)$")
    reason: str = Field(min_length=3, max_length=500)


def _act(fn):
    try:
        fn()
    except StaleState as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)[:500]) from exc
    return {"ok": True}


@app.post("/api/items/{cid}/approve")
def approve(cid: str, u: dict = Depends(user)):
    _check_id(cid)
    return _act(lambda: pipeline(runtime()).approve(cid, u["name"]))


@app.post("/api/items/{cid}/reject")
def reject(cid: str, body: Reason, u: dict = Depends(user)):
    _check_id(cid)
    return _act(lambda: pipeline(runtime()).reject(cid, u["name"], body.reason or "rejected in console"))


@app.post("/api/items/{cid}/reopen")
def reopen(cid: str, u: dict = Depends(user)):
    _check_id(cid)
    return _act(lambda: pipeline(runtime()).reopen(cid, u["name"]))


@app.post("/api/items/{cid}/edit")
def edit(cid: str, body: Edit, u: dict = Depends(user)):
    _check_id(cid)
    changes = body.model_dump(exclude_none=True)
    return _act(lambda: pipeline(runtime()).edit(cid, u["name"], changes))


@app.post("/api/items/{cid}/retry")
def retry(cid: str, body: Retry, u: dict = Depends(admin)):
    _check_id(cid)
    return _act(lambda: pipeline(runtime()).retry(cid, u["name"], body.platform))


@app.post("/api/items/{cid}/unpublish")
def unpublish(cid: str, body: Unpublish, u: dict = Depends(admin)):
    _check_id(cid)
    rt = runtime()
    pub = rt.repo.pub(cid, body.platform)
    if not pub or pub["state"] != PubState.LIVE:
        raise HTTPException(409, "not live on that platform")
    rt.queues.send(Q_PUBLISH[body.platform], {"content_id": cid, "platform": body.platform, "action": "unpublish",
                                              "actor": u["name"], "reason": body.reason})
    rt.repo.audit(cid, u["name"], f"unpublish requested {body.platform}", {"reason": body.reason})
    return {"ok": True}


@app.post("/api/discover")
def discover_now(u: dict = Depends(user)):
    rt = runtime()
    key = f"manual-{now():%Y%m%d%H%M}"
    rt.queues.send(Q_DISCOVER, {"run_key": key, "kind": "manual", "actor": u["name"]})
    return {"ok": True, "run_key": key}


class Theme(BaseModel):
    theme: str = Field(min_length=3, max_length=200)
    terms: list[str] = Field(default_factory=list, max_length=4)
    hours: int = Field(48, ge=6, le=168)


@app.post("/api/create")
def create_from_theme(body: Theme, u: dict = Depends(user)):
    """Queues a one-off draft about the owner's theme. The worker finds coverage and writes the draft, which
    then waits in Review like any other."""
    terms = [t.strip()[:80] for t in body.terms if t.strip()]
    rt = runtime()
    key = f"theme-{now():%Y%m%d%H%M%S}"
    rt.queues.send(Q_DISCOVER, {"run_key": key, "kind": "theme", "theme": body.theme.strip(), "terms": terms,
                                "hours": body.hours, "actor": u["name"]})
    return {"ok": True, "run_key": key}


@app.get("/api/channel")
def channel(days: int = 30, u: dict = Depends(user)):
    from ..analytics.channel import summarise

    if days not in (7, 30, 90):
        raise HTTPException(422, "days must be 7, 30 or 90")
    snap = runtime().repo.channel_snapshot()
    if not snap:
        return {"ready": False}
    return {"ready": True, **summarise(snap, days)}


@app.get("/api/health")
def health(u: dict = Depends(user)):
    rt = runtime()
    s, repo = rt.settings, rt.repo
    flags = repo.flags()
    queues = {}
    for q in ALL_QUEUES:
        try:
            queues[q] = {"depth": rt.queues.depth(q), "dead": rt.queues.depth(poison(q))}
        except Exception as exc:  # noqa: BLE001
            queues[q] = {"depth": -1, "dead": -1, "error": str(exc)[:100]}
    return {
        "kill_switch": bool(flags.get("kill_switch", s.publish_kill_switch)),
        "auto_approve_categories": flags.get("auto_approve_categories", s.auto_approve_categories),
        "yt_privacy_status": flags.get("yt_privacy_status", s.yt_privacy_status),
        "credentials": {p: flags.get(f"credential_{p}", "OK") for p in s.platforms},
        "quota": {"used": repo.ledger(quota_day(), "youtube_units"), "limit": s.yt_daily_quota_units,
                  "stop_pct": s.yt_quota_stop_pct, "day": quota_day()},
        "spend": {"inr": round(repo.ledger(ist_day(), "spend_inr"), 2), "cap_inr": s.daily_spend_cap_inr,
                  "tokens": int(repo.ledger(ist_day(), "llm_tokens")),
                  "ai_video_usd": round(repo.ledger(ist_day(), "ai_video_usd"), 2), "ai_video_cap_usd": s.ai_clip_daily_usd},
        "queues": queues,
        "runs": [{k: r.get(k) for k in ("RowKey", "status", "kind", "started_at", "finished_at", "found", "created",
                                        "skipped", "error")} for r in repo.runs(12)],
        "categories": {c: CATEGORY_NAMES.get(c, c) for c in s.category_allowlist},
        "settings": {"model": s.anthropic_model, "top_n": s.top_n_topics, "region": s.region_code,
                     "platforms": s.platforms, "approval_ttl_hours": s.approval_ttl_hours},
        "admin": u.get("admin", False),
    }


class SettingsIn(BaseModel):
    kill_switch: bool | None = None
    auto_approve_categories: list[str] | None = None
    yt_privacy_status: str | None = Field(None, pattern="^(private|unlisted|public)$")
    clear_credential: str | None = Field(None, pattern="^(YOUTUBE|INSTAGRAM)$")


@app.post("/api/settings")
def change_settings(body: SettingsIn, u: dict = Depends(admin)):
    repo = runtime().repo
    if body.kill_switch is not None:
        repo.set_flag("kill_switch", body.kill_switch, u["name"])
    if body.auto_approve_categories is not None:
        repo.set_flag("auto_approve_categories", [c for c in body.auto_approve_categories if c.isdigit()], u["name"])
    if body.yt_privacy_status:
        repo.set_flag("yt_privacy_status", body.yt_privacy_status, u["name"])
    if body.clear_credential:
        repo.set_flag(f"credential_{body.clear_credential}", "OK", u["name"])
    return {"ok": True}


class Replay(BaseModel):
    queue: str


@app.post("/api/dlq/replay")
def replay(body: Replay, u: dict = Depends(admin)):
    from ..pipeline import replay_dead_letters

    if body.queue not in ALL_QUEUES:
        raise HTTPException(400, "unknown queue")
    return {"replayed": replay_dead_letters(runtime(), body.queue)}


# ----- static UI -----

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
