"""The stage handlers and the queue worker.

Each handler checks the item's state first, so redelivered or duplicate messages are harmless (LLD 5.3).
External services come from `Services`, so tests and local runs can swap in fakes.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .common import logs
from .common.errors import AuthError, Defer, Permanent, PipelineError, Transient, UnknownOutcome
from .common.models import Outcome, PubState, ScriptJSON, State, TrendPayload
from .common.queues import MAX_DELIVERIES, Q_DISCOVER, Q_PUBLISH, Q_RENDER, Q_SCRIPT, Message, poison
from .common.repo import PACIFIC, Repo, StaleState, iso, now, quota_day
from .common.runtime import Runtime
from .common.secrets import MissingSecret

log = logging.getLogger(__name__)

VISIBILITY = {Q_DISCOVER: 600, Q_SCRIPT: 600, Q_RENDER: 2400, Q_PUBLISH["YOUTUBE"]: 1800,
              Q_PUBLISH["INSTAGRAM"]: 1200}


@dataclass
class Services:
    """Factories for external services. `default_services` wires the real ones from Key Vault secrets."""
    trends: Callable[[], Any]
    news: Callable[[], Any]
    llm: Callable[[], Any]
    renderer: Callable[..., tuple[Path, Path, float]]
    youtube: Callable[[], Any]
    instagram: Callable[[], Any]
    qa: Callable[..., Any]
    extra: dict = field(default_factory=dict)
    stories: Callable[[], Any] | None = None     # news-led StoryFinder; None builds the real one
    channel: Callable[[], Any] | None = None     # ChannelStats for whole-channel analytics


def default_services(rt: Runtime) -> Services:
    from .discovery.service import GoogleNews, YouTubeTrends
    from .publish.instagram import InstagramClient
    from .publish.youtube import GoogleToken, YouTubeClient
    from .render import engine, qa

    s, sec = rt.settings, rt.secrets
    repo = rt.repo

    def youtube():
        token = GoogleToken(sec.get("yt-client-id"), sec.get("yt-client-secret"), sec.get("yt-refresh-token"))
        return YouTubeClient(token, quota=lambda units: repo.ledger_add(quota_day(), "youtube_units", units))

    def instagram():
        return InstagramClient(sec.get("ig-access-token"), s.ig_user_id, s.ig_api_host, s.meta_graph_version,
                               sec.get("meta-app-secret", required=False))

    def llm():
        import anthropic

        return anthropic.Anthropic(api_key=sec.get("anthropic-api-key"), max_retries=3, timeout=120)

    def stories():
        from .discovery.stories import StoryFinder

        return StoryFinder(fetch_articles=s.fetch_articles)

    def channel():
        from .analytics.channel import ChannelStats

        return ChannelStats(sec.get("youtube-api-key"))

    def stock():
        from .render.engine.footage import providers_from

        return providers_from(sec.get("pexels-api-key", required=False), sec.get("pixabay-api-key", required=False))

    def judge():
        if not s.footage_vision_check:
            return None
        from .common.repo import ist_day
        from .render.engine.footage import ClaudeJudge

        def charge(tin: int, tout: int) -> None:
            usd = tin / 1e6 * s.llm_price_in_per_mtok + tout / 1e6 * s.llm_price_out_per_mtok
            repo.ledger_add(ist_day(), "spend_inr", usd * s.usd_to_inr)
            repo.ledger_add(ist_day(), "llm_tokens", tin + tout)

        return ClaudeJudge(llm(), s.anthropic_model, s.footage_min_score, on_usage=charge)

    def images():
        from .render.engine.footage import FalImages

        key = sec.get("fal-api-key", required=False)
        return FalImages(key) if key else None

    def video():
        key = sec.get("fal-api-key", required=False)
        if not key or s.ai_clips == "off":
            return None
        from .common.repo import ist_day
        from .render.engine.footage import FalImages, FalVideo

        day = ist_day()
        return FalVideo(key, FalImages(key), s.video_model, s.video_clip_seconds, s.video_price_per_s,
                        s.ai_clip_max_usd_per_video,
                        budget_left=lambda: s.ai_clip_daily_usd - repo.ledger(day, "ai_video_usd"),
                        on_spend=lambda usd: repo.ledger_add(day, "ai_video_usd", usd))

    renderer = functools.partial(engine.render, style=s.render_style, mascot=s.mascot, providers=stock,
                                 judge=judge, images=images, video=video, ai_clips=s.ai_clips)

    return Services(trends=lambda: YouTubeTrends(sec.get("youtube-api-key")),
                    news=lambda: GoogleNews() if s.news_context else None, llm=llm, renderer=renderer,
                    youtube=youtube, instagram=instagram, qa=qa.check, stories=stories, channel=channel)


class Pipeline:
    def __init__(self, rt: Runtime, services: Services | None = None):
        self.rt = rt
        self.s = rt.settings
        self.repo: Repo = rt.repo
        self.services = services or default_services(rt)

    # ----- helpers -----

    def kill_switch(self) -> bool:
        return bool(self.repo.flag("kill_switch", self.s.publish_kill_switch))

    def auto_approve(self) -> list[str]:
        return list(self.repo.flag("auto_approve_categories", self.s.auto_approve_categories))

    def privacy(self) -> str:
        return str(self.repo.flag("yt_privacy_status", self.s.yt_privacy_status))

    def enabled_platforms(self, item: dict) -> list[str]:
        """The item's platforms that are still switched on (PLATFORMS); a platform switched off later is ignored."""
        return [p for p in (item.get("platforms") or "").split(",") if p and p in self.s.platforms]

    def script_of(self, content_id: str, item: dict | None = None) -> ScriptJSON:
        item = item or self.repo.item(content_id) or {}
        version = int(item.get("script_version", 1) or 1)
        data = self.repo.json_row(content_id, f"script:{version:04d}")
        if not data:
            raise Permanent("script missing")
        return ScriptJSON.model_validate(data["script"])

    def trend_of(self, content_id: str) -> TrendPayload:
        return TrendPayload.model_validate(self.repo.json_row(content_id, "trend"))

    # ----- stage: discovery -----

    def discover(self, run_key: str, kind: str = "scheduled") -> dict:
        return self._discovery().run(run_key, kind)

    def _discovery(self):
        from .discovery.service import TrendDiscoveryService

        stories = self.services.stories() if self.services.stories else None
        return TrendDiscoveryService(self.s, self.repo, self.services.trends(), self.services.news(), stories=stories)

    def discover_theme(self, body: dict) -> dict:
        return self._discovery().run_theme(body["run_key"], body.get("theme", ""), list(body.get("terms") or []),
                                           int(body.get("hours") or 48), body.get("actor", "console"))

    # ----- stage: script + policy -----

    def _engine(self):
        from .scripting.engine import ScriptGenEngine

        return ScriptGenEngine(self.s, self.repo, client=self.services.llm())

    def handle_script(self, body: dict) -> None:
        from .policy.gate import ContentPolicyGate

        cid = body["content_id"]
        item = self.repo.item(cid)
        if not item or item["state"] != State.DISCOVERED:
            raise StaleState(f"{cid} not DISCOVERED")
        trend = self.trend_of(cid)
        engine = self._engine()
        try:
            result = engine.generate(trend, idempotency_key=f"{cid}:script:1")
        except Permanent as exc:
            self.repo.transition(cid, State.DISCOVERED, State.REJECTED, "pipeline:script",
                                 {"reason": str(exc)[:300]}, fields={"last_error": str(exc)[:300]})
            return
        script = result.script
        script_row = {"PartitionKey": cid, "RowKey": "script:0001", "data": json.dumps({
            "script": json.loads(script.model_dump_json()), "model": result.model, "prompt_hash": result.prompt_hash,
            "output_hash": result.output_hash, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "attempts": result.attempts, "author": "pipeline", "at": iso()})}
        self.repo.transition(cid, State.DISCOVERED, State.SCRIPTED, "pipeline:script",
                             {"model": result.model, "attempts": result.attempts},
                             fields={"title": script.title, "script_version": 1}, rows=[("upsert", script_row)])

        def moderate(system, prompt, schema):
            text, usage = engine.complete(system, prompt, schema, self.s.moderation_model)
            engine._charge(usage)
            return text, usage

        gate = ContentPolicyGate(self.s.deny_topics, self.auto_approve, moderate, self.s.moderation_model)
        decision = gate.evaluate(script, trend)
        self.apply_decision(cid, decision)

    def apply_decision(self, cid: str, decision) -> None:
        row = {"PartitionKey": cid, "RowKey": "policy", "data": decision.model_dump_json()}
        details = {"outcome": decision.outcome, "reasons": decision.reasons, "flags": decision.flags}
        fields = {"policy": decision.outcome.value, "risk_flags": ",".join(decision.flags)}
        if decision.outcome == Outcome.REJECT:
            self.repo.transition(cid, State.SCRIPTED, State.REJECTED, "policy:gate", details, fields, [("upsert", row)])
        elif decision.outcome == Outcome.APPROVE:
            self.repo.transition(cid, State.SCRIPTED, State.APPROVED, "policy:gate", details,
                                 {**fields, "approved_by": "policy:auto", "approved_at": iso()}, [("upsert", row)],
                                 enqueue=[(Q_RENDER, "render", {"content_id": cid})])
        else:
            expires = iso(now() + timedelta(hours=self.s.approval_ttl_hours))
            self.repo.transition(cid, State.SCRIPTED, State.AWAITING_APPROVAL, "policy:gate", details,
                                 {**fields, "expires_at": expires}, [("upsert", row)],
                                 enqueue=self._preview(cid, 1))

    def _preview(self, cid: str, script_version: int) -> list[tuple[str, str, dict]]:
        """A preview render for a draft, so the reviewer watches the real video before approving."""
        if not self.s.preview_before_approval:
            return []
        return [(Q_RENDER, f"preview{script_version}", {"content_id": cid, "preview": True,
                                                        "script_version": script_version})]

    # ----- operator actions (called by the console) -----

    def approve(self, cid: str, actor: str) -> None:
        self.repo.transition(cid, {State.AWAITING_APPROVAL, State.EXPIRED}, State.APPROVED, actor, {},
                             {"approved_by": actor, "approved_at": iso()},
                             enqueue=[(Q_RENDER, "render", {"content_id": cid})])

    def reject(self, cid: str, actor: str, reason: str) -> None:
        self.repo.transition(cid, {State.AWAITING_APPROVAL, State.EXPIRED}, State.REJECTED, actor,
                             {"reason": reason[:500]}, {"last_error": f"rejected: {reason[:200]}"})

    def reopen(self, cid: str, actor: str) -> None:
        self.repo.transition(cid, State.REJECTED, State.AWAITING_APPROVAL, actor, {},
                             {"expires_at": iso(now() + timedelta(hours=self.s.approval_ttl_hours))})

    def edit(self, cid: str, actor: str, changes: dict) -> ScriptJSON:
        item = self.repo.item(cid)
        if not item or item["state"] not in (State.AWAITING_APPROVAL, State.EXPIRED):
            raise StaleState("only drafts awaiting approval can be edited")
        current = json.loads(self.script_of(cid, item).model_dump_json())
        allowed = {"title", "description", "caption", "hook_text", "scenes", "call_to_action", "hashtags", "tags"}
        merged = {**current, **{k: v for k, v in changes.items() if k in allowed}}
        script = ScriptJSON.model_validate(merged)
        version = int(item.get("script_version", 1)) + 1
        row = {"PartitionKey": cid, "RowKey": f"script:{version:04d}", "data": json.dumps({
            "script": json.loads(script.model_dump_json()), "model": "human-edit", "author": actor, "at": iso()})}
        state = State(item["state"])
        # An edit keeps the draft waiting for approval; an expired draft becomes a fresh one.
        self.repo.transition(cid, state, State.AWAITING_APPROVAL, actor,
                             {"edited_fields": sorted(set(changes) & allowed), "script_version": version},
                             {"script_version": version, "title": script.title,
                              "expires_at": iso(now() + timedelta(hours=self.s.approval_ttl_hours)),
                              "media": ""},
                             [("insert", row)], enqueue=self._preview(cid, version))
        return script

    def retry(self, cid: str, actor: str, platform: str | None = None) -> None:
        item = self.repo.item(cid)
        if not item:
            raise StaleState("not found")
        state = State(item["state"])
        pubs = self.repo.pubs(cid)
        targets = [p for p, row in pubs.items() if row["state"] == PubState.FAILED and (not platform or p == platform)]
        if state in (State.PARTIALLY_PUBLISHED, State.FAILED) and targets:
            rows = [("update", {**pubs[p], "state": PubState.PENDING.value, "last_error": ""}) for p in targets]
            self.repo.transition(cid, state, State.PUBLISHING, actor, {"retry": targets}, {"last_error": ""}, rows,
                                 enqueue=[(Q_PUBLISH[p], f"publish-{p.lower()}-retry{int(time.time())}",
                                           {"content_id": cid, "platform": p}) for p in targets])
        elif state == State.FAILED:
            self.repo.transition(cid, State.FAILED, State.APPROVED, actor, {"retry": "render"}, {"last_error": ""},
                                 enqueue=[(Q_RENDER, "render", {"content_id": cid})])
        else:
            raise StaleState(f"nothing to retry in state {state}")

    # ----- stage: render + QA -----

    def _render_media(self, cid: str, item: dict, version: int):
        """Renders the script version and runs QA. Returns (render, report), or raises Permanent."""
        script = self.script_of(cid, item)
        with tempfile.TemporaryDirectory(prefix="render-") as tmp:
            started = time.monotonic()
            video, thumb, seconds = self.services.renderer(script, item.get("category_id", ""), Path(tmp),
                                                           self.s.tts_voice, self.s.tts_rate, self.s.max_video_seconds)
            blob = f"{cid}/v{version}.mp4"
            self.rt.media.put(blob, video)
            self.rt.media.put(f"{cid}/v{version}.jpg", thumb, "image/jpeg")
            sha = hashlib.sha256(Path(video).read_bytes()).hexdigest()
            render = {"blob": blob, "thumb": f"{cid}/v{version}.jpg", "sha256": sha, "seconds": seconds,
                      "size": Path(video).stat().st_size, "render_s": round(time.monotonic() - started, 1),
                      "script_version": version, "engine": "pillow-ffmpeg", "licence_ref": "generated; synthesized music"}
            credits_file = Path(tmp) / "credits.json"
            if credits_file.exists():  # stock clips used (Pexels / Pixabay licence: free, no attribution needed)
                clips = json.loads(credits_file.read_text())
                if clips:
                    render["licence_ref"] = "stock footage (Pexels/Pixabay licence); synthesized music"
                    if any(c["provider"] == "fal-video" for c in clips):
                        render["licence_ref"] = ("stock footage (Pexels/Pixabay licence), AI-generated clips "
                                                 "(fal.ai); synthesized music")
                    render["clips"] = [{"provider": c["provider"], "id": c["id"], "url": c["url"]} for c in clips][:40]
            report = self.services.qa(Path(video), max_seconds=90)
        return render, report

    def handle_render(self, body: dict) -> None:
        if body.get("preview"):
            return self.handle_preview(body)
        cid = body["content_id"]
        item = self.repo.item(cid)
        if not item or item["state"] not in (State.APPROVED, State.RENDERING):
            raise StaleState(f"{cid} not ready to render")
        if item["state"] == State.APPROVED:
            item = self.repo.transition(cid, State.APPROVED, State.RENDERING, "pipeline:render")
        version = int(item.get("script_version", 1))
        render, qa = self.repo.json_row(cid, "render"), self.repo.json_row(cid, "qa")
        reused = bool(render and qa and render.get("script_version") == version and qa.get("passed")
                      and item.get("media") == render.get("blob"))
        if reused:
            report = SimpleNamespace(**{"problems": [], **qa})
            qa_row = {"PartitionKey": cid, "RowKey": "qa", "data": json.dumps(qa)}
        else:
            try:
                render, report = self._render_media(cid, item, version)
            except Permanent as exc:
                self.repo.transition(cid, State.RENDERING, State.FAILED, "pipeline:render", {"error": str(exc)},
                                     {"last_error": str(exc)[:300]})
                return
            qa_row = {"PartitionKey": cid, "RowKey": "qa", "data": json.dumps(report.__dict__)}
        row = {"PartitionKey": cid, "RowKey": "render", "data": json.dumps(render)}
        self.repo.transition(cid, State.RENDERING, State.RENDERED, "pipeline:render",
                             {"sha256": render["sha256"], "seconds": render["seconds"], "reused_preview": reused},
                             {"media": render["blob"]}, [("upsert", row)])
        if not report.passed:
            self.repo.transition(cid, State.RENDERED, State.FAILED, "pipeline:qa", {"problems": report.problems},
                                 {"last_error": "QA: " + "; ".join(report.problems)[:280]}, [("upsert", qa_row)])
            return
        self.repo.transition(cid, State.RENDERED, State.QA_PASSED, "pipeline:qa", {}, {}, [("upsert", qa_row)])
        self.start_publishing(cid, State.QA_PASSED)

    def handle_preview(self, body: dict) -> None:
        """Renders a draft's video before approval. No state change: Approve later reuses it if the script
        version is unchanged; an edit queues a fresh preview."""
        cid, version = body["content_id"], int(body.get("script_version", 1))
        item = self.repo.item(cid)
        if not item or item["state"] not in (State.AWAITING_APPROVAL, State.EXPIRED) \
                or int(item.get("script_version", 1)) != version:
            raise StaleState(f"{cid} preview v{version} no longer needed")
        existing = self.repo.json_row(cid, "render")
        if existing and existing.get("script_version") == version and item.get("media") == existing.get("blob"):
            return
        try:
            render, report = self._render_media(cid, item, version)
        except Permanent as exc:
            self.repo.audit(cid, "pipeline:preview", "preview failed", {"error": str(exc)[:300]})
            return
        rows = [("upsert", {"PartitionKey": cid, "RowKey": "render", "data": json.dumps(render)}),
                ("upsert", {"PartitionKey": cid, "RowKey": "qa", "data": json.dumps(report.__dict__)})]
        latest = self.repo.item(cid) or {}
        if latest.get("state") not in (State.AWAITING_APPROVAL, State.EXPIRED) or \
                int(latest.get("script_version", 1)) != version:
            return   # approved or edited meanwhile; the main render or a newer preview takes over
        self.repo.update_fields(cid, {"media": render["blob"]}, rows)
        self.repo.audit(cid, "pipeline:preview", "preview rendered",
                        {"seconds": render["seconds"], "qa": "passed" if report.passed else report.problems})

    def start_publishing(self, cid: str, from_state: State) -> None:
        item = self.repo.item(cid) or {}
        existing = self.repo.pubs(cid)
        rows, enqueue = [], []
        for platform in self.enabled_platforms(item):
            if platform in existing and existing[platform]["state"] == PubState.LIVE:
                continue
            rows.append(("upsert", {**self.repo.new_pub_row(cid, platform),
                                    **({k: existing[platform][k] for k in ("upload_session", "ig_container_id",
                                                                           "attempts")}
                                       if platform in existing else {})}))
            enqueue.append((Q_PUBLISH[platform], f"publish-{platform.lower()}", {"content_id": cid,
                                                                                 "platform": platform}))
        self.repo.transition(cid, from_state, State.PUBLISHING, "pipeline:publish", {"platforms": [e[2]["platform"]
                                                                                                 for e in enqueue]},
                             rows=rows, enqueue=enqueue)
        if not enqueue:
            self.finalize(cid)

    # ----- stage: publish -----

    def _publish_guard(self, cid: str, platform: str) -> tuple[dict, dict]:
        item = self.repo.item(cid)
        if not item or item["state"] not in (State.PUBLISHING, State.PARTIALLY_PUBLISHED):
            raise StaleState(f"{cid} not publishing")
        if platform not in self.s.platforms:   # switched off after this item was queued
            self.finalize(cid)
            raise StaleState(f"{platform} publishing is switched off")
        pub = self.repo.pub(cid, platform)
        if not pub or pub["state"] in (PubState.LIVE, PubState.FAILED, PubState.REMOVED):
            if pub and pub["state"] == PubState.LIVE:
                self.finalize(cid)
            raise StaleState(f"{cid} {platform} already {pub and pub['state']}")
        # Fail closed: kill switch, revoked credential, missing QA all stop publishing.
        if self.kill_switch():
            raise Defer("kill switch is on", 900)
        if self.repo.flag(f"credential_{platform}", "OK") == "REVOKED":
            raise Defer(f"{platform} credential is marked REVOKED (runbook R1)", 1800)
        qa = self.repo.json_row(cid, "qa")
        if not qa or not qa.get("passed"):
            raise Permanent("QA has not passed")
        return item, pub

    def _media_file(self, item: dict, folder: Path) -> Path:
        path = folder / "video.mp4"
        self.rt.media.download(item["media"], path)
        return path

    def handle_publish(self, body: dict) -> None:
        cid, platform = body["content_id"], body["platform"]
        item, pub = self._publish_guard(cid, platform)
        try:
            if platform == "YOUTUBE":
                self._publish_youtube(cid, item, pub)
            else:
                self._publish_instagram(cid, item, pub)
        except AuthError as exc:
            self.repo.set_flag(f"credential_{platform}", "REVOKED", "pipeline")
            log.error("P1: %s credential failed: %s", platform, exc)
            self._pub_error(cid, platform, exc)
            raise Defer(str(exc), 1800) from exc
        except MissingSecret as exc:
            fix = "run bootstrap-youtube" if platform == "YOUTUBE" else "store ig-access-token in Key Vault"
            reason = PipelineError(f"{platform.title()} is not connected yet ({exc}); {fix}")
            reason.error_class = "not_connected"
            log.error("P1: %s", reason)
            self._pub_error(cid, platform, reason)
            raise Defer(str(reason), 1800) from exc
        except Permanent as exc:
            self._pub_error(cid, platform, exc, failed=True)
            self.finalize(cid)
            return
        except (Transient, UnknownOutcome) as exc:
            self._pub_error(cid, platform, exc)
            raise
        self.finalize(cid)

    def _pub_error(self, cid: str, platform: str, exc: Exception, failed: bool = False) -> None:
        pub = self.repo.pub(cid, platform)
        if pub:
            fields: dict = {"last_error": str(exc)[:500], "error_class": getattr(exc, "error_class", "unknown")}
            if failed:
                fields["state"] = PubState.FAILED.value
            self.repo.save_pub(pub, **fields)
            self.repo.add_attempt(cid, platform, int(pub.get("attempts", 0)), "FAILED" if failed else "ERROR",
                                  error_code=getattr(exc, "error_class", ""), detail=str(exc))

    def _seconds_to_quota_reset(self) -> int:
        local = now().astimezone(PACIFIC)
        tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return int((tomorrow - local).total_seconds())

    def _publish_youtube(self, cid: str, item: dict, pub: dict) -> None:
        from .common.repo import short_ref
        from .publish.youtube import build_metadata

        yt = self.services.youtube()
        ref = short_ref(cid)
        number = int(pub.get("attempts", 0)) + 1
        had_attempt = number > 1
        session = pub.get("upload_session", "")
        used = self.repo.ledger(quota_day(), "youtube_units")
        budget = self.s.yt_daily_quota_units * self.s.yt_quota_stop_pct / 100
        if not session and used + self.s.yt_insert_cost > budget:
            raise Defer(f"YouTube quota at {used:.0f}/{self.s.yt_daily_quota_units} units", self._seconds_to_quota_reset())
        pub = self.repo.save_pub(pub, attempts=number, state=PubState.UPLOADING.value)
        self.repo.add_attempt(cid, "YOUTUBE", number, "STARTED")
        channel = yt.verify_channel(self.s.yt_channel_handle)
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        video_id = None
        with tempfile.TemporaryDirectory(prefix="yt-") as tmp:
            path = self._media_file(item, Path(tmp))
            size = path.stat().st_size
            if session:
                state, value = yt.session_status(session, size)
                if state == "done":
                    video_id = value["id"]
                elif state == "resume":
                    video_id = yt.upload_from(session, path, int(value), size)["id"]
                else:
                    session = ""
            if not video_id and not session and had_attempt:
                video_id = yt.find_by_ref(uploads, ref)       # reconcile before any new insert
                if video_id:
                    log.info("reconciled %s: already uploaded as %s", cid, video_id)
            if not video_id and not session:
                script = self.script_of(cid, item)
                meta = build_metadata(script, ref, self.privacy(), item.get("category_id", "24"))
                self.repo.ledger_add(quota_day(), "youtube_units", self.s.yt_insert_cost)
                session = yt.start_session(meta, size)
                pub = self.repo.save_pub(self.repo.pub(cid, "YOUTUBE"), upload_session=session)
                video_id = yt.upload_from(session, path, 0, size)["id"]
        pub = self.repo.pub(cid, "YOUTUBE")
        self.repo.save_pub(pub, state=PubState.LIVE.value, platform_media_id=video_id,
                           url=f"https://youtube.com/shorts/{video_id}", upload_session="", last_error="",
                           published_at=iso(), privacy=self.privacy())
        self.repo.add_attempt(cid, "YOUTUBE", number, "SUCCEEDED", 200, detail=video_id)
        self.repo.audit(cid, "pipeline:publish", "youtube LIVE", {"video_id": video_id})

    def _publish_instagram(self, cid: str, item: dict, pub: dict) -> None:
        from .publish.instagram import build_caption

        ig = self.services.instagram()
        script = self.script_of(cid, item)
        caption = build_caption(script)
        number = int(pub.get("attempts", 0)) + 1
        container = pub.get("ig_container_id", "")
        media_id = ""
        if container:   # reconcile: the container's status decides the next step
            try:
                status, detail = ig.container_status(container)
            except Permanent:
                status, detail = "EXPIRED", "container not found"
            if status == "PUBLISHED":
                found = ig.find_media(caption)
                if not found:
                    raise UnknownOutcome("container PUBLISHED but media not found yet")
                media_id = found["id"]
            elif status == "FINISHED":
                media_id = ig.publish(container)
            elif status == "IN_PROGRESS":
                pub = self.repo.save_pub(pub, state=PubState.PROCESSING.value)
                raise Defer("Instagram is still processing the video", 30)
            else:
                log.info("container %s is %s (%s); creating a new one", container, status, detail)
                pub = self.repo.save_pub(pub, ig_container_id="", ig_upload_uri="")
                container = ""
        if not media_id and not container:
            if ig.publishing_room() <= self.s.ig_publish_headroom:
                raise Defer("Instagram publishing limit nearly reached", 3600)
            pub = self.repo.save_pub(pub, attempts=number, state=PubState.UPLOADING.value)
            self.repo.add_attempt(cid, "INSTAGRAM", number, "STARTED")
            if self.s.ig_upload_mode == "sas_url":
                container, _ = ig.create_container(caption, self.rt.media.sas_url(item["media"], 15))
                pub = self.repo.save_pub(pub, ig_container_id=container)
            else:
                container, uri = ig.create_container(caption)
                pub = self.repo.save_pub(pub, ig_container_id=container, ig_upload_uri=uri)
                with tempfile.TemporaryDirectory(prefix="ig-") as tmp:
                    ig.upload(uri, self._media_file(item, Path(tmp)))
            pub = self.repo.save_pub(pub, state=PubState.PROCESSING.value)
            for _ in range(12):   # poll every 10 s for 2 minutes, then hand back to the queue
                status, detail = ig.container_status(container)
                if status == "FINISHED":
                    break
                if status == "ERROR":
                    raise Permanent(f"Instagram could not process the video: {detail}")
                ig.sleep(10)
            else:
                raise Defer("Instagram is still processing the video", 30)
            media_id = ig.publish(container)
        permalink = ""
        try:
            permalink = ig.permalink(media_id)
        except PipelineError:
            pass
        pub = self.repo.pub(cid, "INSTAGRAM")
        self.repo.save_pub(pub, state=PubState.LIVE.value, platform_media_id=media_id, url=permalink,
                           last_error="", published_at=iso())
        self.repo.add_attempt(cid, "INSTAGRAM", number, "SUCCEEDED", 200, detail=media_id)
        self.repo.audit(cid, "pipeline:publish", "instagram LIVE", {"media_id": media_id})

    def finalize(self, cid: str) -> None:
        item = self.repo.item(cid)
        if not item or item["state"] not in (State.PUBLISHING, State.PARTIALLY_PUBLISHED):
            return
        pubs = [self.repo.pub(cid, p) for p in self.enabled_platforms(item)]
        states = [p["state"] if p else PubState.PENDING for p in pubs]
        if any(s in (PubState.PENDING, PubState.UPLOADING, PubState.PROCESSING) for s in states):
            return
        live = sum(s == PubState.LIVE for s in states)
        target = State.PUBLISHED if live == len(states) else State.PARTIALLY_PUBLISHED if live else State.FAILED
        if target.value == item["state"]:
            return
        try:
            self.repo.transition(cid, State(item["state"]), target, "pipeline:publish", {"platforms": states},
                                 {"published_at": iso()} if live else {"last_error": "publishing failed"})
        except (StaleState, ValueError):
            pass

    # ----- operator: unpublish -----

    def handle_unpublish(self, body: dict) -> None:
        cid, platform = body["content_id"], body["platform"]
        pub = self.repo.pub(cid, platform)
        if not pub or pub["state"] != PubState.LIVE:
            raise StaleState("not live")
        if platform == "YOUTUBE":
            self.services.youtube().set_privacy(pub["platform_media_id"], "private")
            self.repo.save_pub(pub, state=PubState.REMOVED.value, privacy="private")
        else:
            # Instagram's API cannot delete Reels for most account types: the operator removes it in the app.
            self.repo.save_pub(pub, last_error="remove this Reel in the Instagram app (API deletion unavailable)")
        self.repo.audit(cid, body.get("actor", "console"), f"unpublish {platform}", {"reason": body.get("reason", "")})

    # ----- sweeper -----

    def sweep(self) -> dict:
        relayed = self.repo.relay_outbox()
        expired = 0
        for item in self.repo.items():
            if item["state"] == State.AWAITING_APPROVAL and item.get("expires_at", "9") < iso():
                try:
                    self.repo.transition(item["PartitionKey"], State.AWAITING_APPROVAL, State.EXPIRED, "pipeline:sweep")
                    expired += 1
                except StaleState:
                    pass
        for item in self.repo.items():   # items whose last platform message was lost or switched off
            if item["state"] in (State.PUBLISHING, State.PARTIALLY_PUBLISHED):
                self.finalize(item["PartitionKey"])
        previews = self.queue_missing_previews()
        refreshed = self.refresh_metrics()
        channel = self.refresh_channel()
        return {"relayed": relayed, "expired": expired, "previews": previews, "metrics": refreshed, "channel": channel}

    def queue_missing_previews(self) -> int:
        """Drafts written before previews existed, or whose preview message was lost, get one (at most hourly)."""
        if not self.s.preview_before_approval:
            return 0
        count, cutoff = 0, iso(now() - timedelta(hours=1))
        for item in self.repo.items():
            if item["state"] != State.AWAITING_APPROVAL or item.get("media") or item.get("preview_requested_at", "") > cutoff:
                continue
            cid, version = item["PartitionKey"], int(item.get("script_version", 1) or 1)
            self.repo.update_fields(cid, {"preview_requested_at": iso()})
            self.rt.queues.send(Q_RENDER, {"content_id": cid, "preview": True, "script_version": version})
            count += 1
        return count

    def refresh_channel(self, force: bool = False) -> bool:
        """Whole-channel analytics (all uploads, not only ours), at most every channel_stats_every_hours."""
        if not self.services.channel or not self.s.yt_channel_handle:
            return False
        row = self.repo.channel_snapshot()
        if row and not force and row.get("fetched_at", "") > iso(now() - timedelta(hours=self.s.channel_stats_every_hours)):
            return False
        stats = None
        try:
            stats = self.services.channel()
            snap = stats.snapshot(self.s.yt_channel_handle)
        except Exception as exc:  # noqa: BLE001 - analytics must never stop the sweep
            log.warning("channel analytics refresh failed: %s", exc)
            return False
        finally:
            if getattr(stats, "units", 0):
                self.repo.ledger_add(quota_day(), "youtube_units", stats.units)
        self.repo.save_channel_snapshot(snap)
        return True

    def refresh_metrics(self, days: int = 30) -> int:
        cutoff = iso(now() - timedelta(days=days))
        live: dict[str, list[tuple[str, dict]]] = {"YOUTUBE": [], "INSTAGRAM": []}
        for item in self.repo.items():
            if item["state"] not in (State.PUBLISHED, State.PARTIALLY_PUBLISHED) or item.get("updated_at", "") < cutoff:
                continue
            for platform, pub in self.repo.pubs(item["PartitionKey"]).items():
                if pub["state"] == PubState.LIVE and pub.get("platform_media_id"):
                    live[platform].append((item["PartitionKey"], pub))
        count = 0
        if live["YOUTUBE"] and self.repo.flag("credential_YOUTUBE", "OK") != "REVOKED":
            try:
                stats = self.services.youtube().stats([p["platform_media_id"] for _, p in live["YOUTUBE"]])
                for cid, pub in live["YOUTUBE"]:
                    if pub["platform_media_id"] in stats:
                        self.repo.save_metrics(cid, "YOUTUBE", **stats[pub["platform_media_id"]])
                        count += 1
            except AuthError:
                self.repo.set_flag("credential_YOUTUBE", "REVOKED", "pipeline:sweep")
            except PipelineError as exc:
                log.warning("YouTube metrics refresh failed: %s", exc)
        if live["INSTAGRAM"] and self.repo.flag("credential_INSTAGRAM", "OK") != "REVOKED":
            try:
                ig = self.services.instagram()
                for cid, pub in live["INSTAGRAM"]:
                    self.repo.save_metrics(cid, "INSTAGRAM", **ig.stats(pub["platform_media_id"]))
                    count += 1
            except AuthError:
                self.repo.set_flag("credential_INSTAGRAM", "REVOKED", "pipeline:sweep")
            except PipelineError as exc:
                log.warning("Instagram metrics refresh failed: %s", exc)
        return count

    # ----- worker -----

    def dispatch(self, queue: str, body: dict) -> None:
        if queue == Q_DISCOVER and body.get("kind") == "theme":
            self.discover_theme(body)
        elif queue == Q_DISCOVER:
            self.discover(body.get("run_key") or f"manual-{int(time.time())}", body.get("kind", "manual"))
        elif queue == Q_SCRIPT:
            self.handle_script(body)
        elif queue == Q_RENDER:
            self.handle_render(body)
        elif body.get("action") == "unpublish":
            self.handle_unpublish(body)
        else:
            self.handle_publish(body)

    def process(self, queue: str, msg: Message) -> str:
        cid = msg.body.get("content_id", "")
        logs.bind(queue=queue, content_id=cid, attempt=msg.dequeue_count)
        q = self.rt.queues
        try:
            if msg.dequeue_count > MAX_DELIVERIES:
                q.send(poison(queue), {**msg.body, "_last_queue": queue, "_dead_at": iso()})
                q.delete(queue, msg)
                self._dead_letter(cid, msg.body, queue)
                return "dead-lettered"
            self.dispatch(queue, msg.body)
            q.delete(queue, msg)
            return "done"
        except StaleState as exc:
            log.info("dropping message: %s", exc)
            q.delete(queue, msg)
            return "dropped"
        except Defer as exc:
            log.info("deferring %ss: %s", exc.delay_s, exc)
            q.send(queue, msg.body, delay_s=exc.delay_s)
            q.delete(queue, msg)
            if cid:
                try:
                    self.repo.update_fields(cid, {"last_error": f"waiting: {str(exc)[:200]}"})
                except StaleState:
                    pass
            return "deferred"
        except Exception as exc:  # noqa: BLE001 - redelivered after the visibility timeout
            log.exception("stage failed (delivery %d/%d): %s", msg.dequeue_count, MAX_DELIVERIES, exc)
            if cid:
                try:
                    self.repo.update_fields(cid, {"last_error": str(exc)[:300]})
                except StaleState:
                    pass
            return "error"
        finally:
            logs.clear()

    def _dead_letter(self, cid: str, body: dict, queue: str) -> None:
        log.error("P2: message dead-lettered from %s for %s", queue, cid)
        if not cid:
            return
        platform = body.get("platform")
        if platform:
            pub = self.repo.pub(cid, platform)
            if pub and pub["state"] != PubState.LIVE:
                self.repo.save_pub(pub, state=PubState.FAILED.value, last_error="dead-lettered after 5 deliveries")
            self.finalize(cid)
        else:
            item = self.repo.item(cid)
            if item and item["state"] in (State.RENDERING, State.APPROVED, State.RENDERED):
                try:
                    self.repo.transition(cid, State(item["state"]), State.FAILED, "pipeline:dlq",
                                         {"queue": queue}, {"last_error": "dead-lettered after 5 deliveries"})
                except (StaleState, ValueError):
                    pass

    def work(self, queues: list[str], idle_exit: bool = True, max_runtime_s: int = 1500, idle_sleep: float = 5) -> dict:
        started, counts = time.monotonic(), {}
        while time.monotonic() - started < max_runtime_s:
            busy = False
            for queue in queues:
                for msg in self.rt.queues.receive(queue, VISIBILITY.get(queue, 600), 1):
                    busy = True
                    outcome = self.process(queue, msg)
                    counts[outcome] = counts.get(outcome, 0) + 1
            if not busy:
                if idle_exit:
                    break
                time.sleep(idle_sleep)
        return counts


def replay_dead_letters(rt: Runtime, queue: str, limit: int = 32) -> int:
    count = 0
    for msg in rt.queues.receive(poison(queue), 60, limit):
        body = {k: v for k, v in msg.body.items() if not k.startswith("_")}
        rt.queues.send(queue, body)
        rt.queues.delete(poison(queue), msg)
        count += 1
    return count


def due_run_key(at: datetime | None = None) -> str:
    from .discovery.service import run_key_for

    return run_key_for((at or datetime.now(timezone.utc)), 4)
