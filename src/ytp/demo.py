"""Local demo: runs the real pipeline end to end against fakes, so the console has data to show."""
from __future__ import annotations

import random
from datetime import timedelta

from .common.models import State
from .common.queues import ALL_QUEUES
from .common.repo import iso, now
from .common.runtime import Runtime
from .fakes import fake_services
from .pipeline import Pipeline


def run_demo(rt: Runtime, runs: int = 3) -> dict:
    if rt.settings.backend != "local":
        raise SystemExit("demo is for the local backend only")
    pipeline = Pipeline(rt, fake_services())
    stamp = now().strftime("%Y%m%d%H%M%S")
    for n in range(runs):
        pipeline.discover(f"demo-{stamp}-{n}", "demo")
        pipeline.work(ALL_QUEUES)
    rng = random.Random(3)
    drafts = [i for i in rt.repo.items() if i["state"] == State.AWAITING_APPROVAL]
    for item in drafts[: max(1, len(drafts) * 2 // 3)]:
        pipeline.approve(item["PartitionKey"], "demo@local")
    pipeline.work(ALL_QUEUES)
    # Spread publish dates over the past weeks so the charts have shape.
    for item in rt.repo.items():
        if item["state"] in (State.PUBLISHED, State.PARTIALLY_PUBLISHED):
            back = now() - timedelta(days=rng.randint(0, 20), hours=rng.randint(0, 20))
            for pub in rt.repo.pubs(item["PartitionKey"]).values():
                rt.repo.save_pub(pub, published_at=iso(back))
            rt.repo.update_fields(item["PartitionKey"], {"published_at": iso(back), "created_at": iso(back - timedelta(hours=3))})
    pipeline.refresh_metrics(days=60)
    counts: dict[str, int] = {}
    for item in rt.repo.items():
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return counts
