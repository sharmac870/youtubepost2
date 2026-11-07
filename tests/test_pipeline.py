"""End to end through every stage with fakes, plus the mandatory idempotency tests (LLD Section 13)."""
from ytp.common.models import PubState, State
from ytp.common.queues import ALL_QUEUES, Q_PUBLISH, poison
from ytp.common.repo import quota_day


def drafts(rt):
    return [i for i in rt.repo.items() if i["state"] == State.AWAITING_APPROVAL]


def run_to_approval(pipeline, rt, key="2026-11-07-slot1"):
    result = pipeline.discover(key)
    assert result["status"] == "ok" and len(result["created"]) == 2
    pipeline.work(ALL_QUEUES)
    items = drafts(rt)
    assert len(items) == 2, [i["state"] for i in rt.repo.items()]
    return items


def test_full_pipeline_publishes_once_to_both_platforms(pipeline, rt, services):
    items = run_to_approval(pipeline, rt)
    cid = items[0]["PartitionKey"]
    pipeline.approve(cid, "reviewer@example.com")
    pipeline.work(ALL_QUEUES)
    item = rt.repo.item(cid)
    assert item["state"] == State.PUBLISHED, item
    pubs = rt.repo.pubs(cid)
    assert {p: r["state"] for p, r in pubs.items()} == {"YOUTUBE": PubState.LIVE, "INSTAGRAM": PubState.LIVE}
    yt, ig = services.extra["youtube"], services.extra["instagram"]
    assert len(yt.uploads) == 1 and len(ig.media) == 1
    meta = next(iter(yt.sessions.values()))["meta"]
    assert meta.title.endswith("#Shorts") and "AI tools" in meta.description and item["ref"] in meta.tags
    assert rt.repo.ledger(quota_day(), "youtube_units") >= 1600
    # The other draft is untouched until someone approves it.
    assert rt.repo.item(items[1]["PartitionKey"])["state"] == State.AWAITING_APPROVAL
    actions = [e["action"] for e in rt.repo.audit_events(cid)]
    assert "AWAITING_APPROVAL->APPROVED" in actions and "PUBLISHING->PUBLISHED" in actions


def test_double_trigger_of_a_run_is_harmless(pipeline, rt):
    pipeline.discover("2026-11-07-slot2")
    assert pipeline.discover("2026-11-07-slot2")["status"] == "duplicate"
    assert len(rt.repo.items()) == 2


def test_topics_are_not_repeated_within_the_dedupe_window(pipeline, rt):
    first = pipeline.discover("k1")["created"]
    second = pipeline.discover("k2")["created"]
    titles = [rt.repo.item(c)["title"] for c in first + second]
    assert len(titles) == len(set(titles))


def test_redelivered_messages_do_nothing(pipeline, rt, services):
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    # Replay every stage's message: all are dropped by the state checks.
    for queue, body in [("q-script", {"content_id": cid}), ("q-render", {"content_id": cid}),
                        (Q_PUBLISH["YOUTUBE"], {"content_id": cid, "platform": "YOUTUBE"}),
                        (Q_PUBLISH["INSTAGRAM"], {"content_id": cid, "platform": "INSTAGRAM"})]:
        rt.queues.send(queue, body)
    counts = pipeline.work(ALL_QUEUES)
    assert counts == {"dropped": 4}
    assert len(services.extra["youtube"].uploads) == 1 and len(services.extra["instagram"].media) == 1


def test_crash_after_youtube_insert_resumes_the_session_not_a_new_upload(pipeline, rt, services):
    yt = services.extra["youtube"]
    yt.crash_after_upload = True
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)                       # crashes after the upload, before the DB write
    assert rt.repo.pub(cid, "YOUTUBE")["state"] == PubState.UPLOADING
    rt.queues.send(Q_PUBLISH["YOUTUBE"], {"content_id": cid, "platform": "YOUTUBE"})   # redelivery
    pipeline.work(ALL_QUEUES)
    assert len(yt.uploads) == 1
    assert rt.repo.pub(cid, "YOUTUBE")["state"] == PubState.LIVE
    assert rt.repo.item(cid)["state"] == State.PUBLISHED


def test_lost_youtube_session_reconciles_by_ref_tag_before_inserting(pipeline, rt, services):
    yt = services.extra["youtube"]
    yt.crash_after_upload = True
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    yt.sessions.clear()                             # session expired: only reconciliation can find the video
    rt.queues.send(Q_PUBLISH["YOUTUBE"], {"content_id": cid, "platform": "YOUTUBE"})
    pipeline.work(ALL_QUEUES)
    assert len(yt.uploads) == 1
    assert rt.repo.pub(cid, "YOUTUBE")["platform_media_id"] == next(iter(yt.uploads))


def test_crash_after_instagram_publish_finds_the_post_instead_of_reposting(pipeline, rt, services):
    ig = services.extra["instagram"]
    ig.crash_after_publish = True
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    assert rt.repo.pub(cid, "INSTAGRAM")["state"] != PubState.LIVE
    rt.queues.send(Q_PUBLISH["INSTAGRAM"], {"content_id": cid, "platform": "INSTAGRAM"})
    pipeline.work(ALL_QUEUES)
    assert len(ig.media) == 1
    assert rt.repo.pub(cid, "INSTAGRAM")["state"] == PubState.LIVE


def test_kill_switch_defers_publishing(pipeline, rt, services):
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    rt.repo.set_flag("kill_switch", True, "admin")
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    assert rt.repo.item(cid)["state"] == State.PUBLISHING
    assert not services.extra["youtube"].uploads and not services.extra["instagram"].media
    assert rt.queues.depth(Q_PUBLISH["YOUTUBE"]) == 1   # waiting, not lost


def test_quota_budget_defers_youtube_but_not_instagram(pipeline, rt, services):
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    rt.repo.ledger_add(quota_day(), "youtube_units", 7000)
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    assert not services.extra["youtube"].uploads
    assert rt.repo.pub(cid, "INSTAGRAM")["state"] == PubState.LIVE
    assert rt.repo.item(cid)["state"] == State.PUBLISHING


def test_permanent_failure_on_one_platform_is_partial_and_retryable(pipeline, rt, services):
    from ytp.common.errors import Permanent

    ig = services.extra["instagram"]
    original = ig.create_container
    ig.create_container = lambda *a, **k: (_ for _ in ()).throw(Permanent("media rejected"))
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    assert rt.repo.item(cid)["state"] == State.PARTIALLY_PUBLISHED
    ig.create_container = original
    pipeline.retry(cid, "admin", "INSTAGRAM")
    pipeline.work(ALL_QUEUES)
    assert rt.repo.item(cid)["state"] == State.PUBLISHED
    assert len(services.extra["youtube"].uploads) == 1


def test_auth_failure_marks_credential_revoked_and_stops(pipeline, rt, services):
    from ytp.common.errors import AuthError

    yt = services.extra["youtube"]
    yt.verify_channel = lambda expected: (_ for _ in ()).throw(AuthError("invalid_grant"))
    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    pipeline.approve(cid, "r")
    pipeline.work(ALL_QUEUES)
    assert rt.repo.flag("credential_YOUTUBE") == "REVOKED"
    assert rt.repo.pub(cid, "YOUTUBE")["state"] != PubState.LIVE


def test_poison_after_five_deliveries(pipeline, rt):
    rt.queues.send("q-render", {"content_id": "nope"})
    for _ in range(6):
        msgs = rt.queues.receive("q-render", 0, 1)
        if msgs:
            pipeline.process("q-render", msgs[0]) if msgs[0].dequeue_count > 5 else None
    assert rt.queues.depth(poison("q-render")) == 1


def test_outbox_rows_are_relayed_if_send_failed(pipeline, rt):
    sent = []
    real = rt.queues.send
    rt.queues.send = lambda q, b, delay_s=0: (_ for _ in ()).throw(ConnectionError("queue down"))
    pipeline.discover("k-outbox")
    rt.queues.send = lambda q, b, delay_s=0: (sent.append(q), real(q, b, delay_s))
    assert rt.repo.relay_outbox(older_than_s=0) == 2
    assert sent == ["q-script", "q-script"]
    assert rt.repo.relay_outbox(older_than_s=0) == 0


def test_expired_drafts_and_edit_flow(pipeline, rt):
    from ytp.common.repo import iso

    cid = run_to_approval(pipeline, rt)[0]["PartitionKey"]
    rt.repo.update_fields(cid, {"expires_at": iso().replace("20", "19", 1)})
    pipeline.sweep()
    assert rt.repo.item(cid)["state"] == State.EXPIRED
    script = pipeline.edit(cid, "editor", {"title": "A sharper, human-edited title for today"})
    item = rt.repo.item(cid)
    assert item["state"] == State.AWAITING_APPROVAL and item["script_version"] == 2
    assert script.title.startswith("A sharper")


def test_switching_a_platform_off_releases_items_waiting_on_it(pipeline, rt):
    items = run_to_approval(pipeline, rt, key="2026-11-07-slot9")
    cid = items[0]["PartitionKey"]
    rt.repo.set_flag("credential_INSTAGRAM", "REVOKED", "test")
    pipeline.approve(cid, "reviewer@example.com")
    pipeline.work(ALL_QUEUES)
    assert rt.repo.item(cid)["state"] == State.PUBLISHING          # YouTube live, Instagram stuck
    assert rt.repo.pub(cid, "YOUTUBE")["state"] == PubState.LIVE
    rt.settings.platforms = ["YOUTUBE"]
    pipeline.sweep()
    assert rt.repo.item(cid)["state"] == State.PUBLISHED
    # The deferred Instagram message is dropped instead of retried, and nothing is posted there.
    assert rt.repo.pub(cid, "INSTAGRAM")["state"] != PubState.LIVE
