"""Command line: `python -m ytp <command>`.

  discover [--key K]         one discovery run (Container Apps cron job); the key makes double triggers harmless
  work --queues a,b [--loop] drain queues and exit when empty (event-driven Container Apps jobs)
  sweep                      relay the outbox, expire stale approvals, refresh post metrics (cron job)
  replay-dlq --queue Q       move dead-lettered messages back to their queue
  serve [--port 8000]        the console (dashboard + review)
  demo                       local only: fill a local database through the real pipeline with fakes
  bootstrap-youtube FILE     one-time OAuth (PKCE) for the channel; writes the refresh token to Key Vault
  set-secret NAME            read a secret from stdin and write it to Key Vault (never echoed)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .common import logs
from .common.config import get_settings
from .common.queues import ALL_QUEUES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ytp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--key")
    w = sub.add_parser("work")
    w.add_argument("--queues", default=",".join(q for q in ALL_QUEUES))
    w.add_argument("--loop", action="store_true", help="keep polling instead of exiting when idle")
    w.add_argument("--max-runtime", type=int, default=1500)
    sub.add_parser("sweep")
    r = sub.add_parser("replay-dlq")
    r.add_argument("--queue", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    demo = sub.add_parser("demo")
    demo.add_argument("--runs", type=int, default=3)
    b = sub.add_parser("bootstrap-youtube")
    b.add_argument("client_secret_json")
    k = sub.add_parser("set-secret")
    k.add_argument("name")
    args = parser.parse_args(argv)

    settings = get_settings()
    logs.setup(json_output=settings.backend == "azure")
    log = logging.getLogger("ytp")

    if args.command == "serve":
        import uvicorn

        uvicorn.run("ytp.console.app:app", host=args.host, port=args.port, proxy_headers=True,
                    forwarded_allow_ips="*")
        return 0
    if args.command == "bootstrap-youtube":
        from .bootstrap import bootstrap_youtube

        return bootstrap_youtube(args.client_secret_json, settings)
    if args.command == "set-secret":
        from .bootstrap import set_secret

        return set_secret(args.name, settings)

    from .common.runtime import build
    from .pipeline import Pipeline, due_run_key, replay_dead_letters as replay_dlq_cli

    rt = build(settings)
    if args.command == "demo":
        from .demo import run_demo

        print(json.dumps(run_demo(rt, args.runs), indent=1))
        return 0
    pipeline = Pipeline(rt)
    if args.command == "discover":
        result = pipeline.discover(args.key or due_run_key())
        # The script stage runs in the worker job; nudge it by leaving the messages on q-script.
        log.info("discovery: %s", json.dumps(result))
    elif args.command == "work":
        counts = pipeline.work([q.strip() for q in args.queues.split(",") if q.strip()], idle_exit=not args.loop,
                               max_runtime_s=args.max_runtime)
        log.info("worker finished: %s", counts)
    elif args.command == "sweep":
        log.info("sweep: %s", pipeline.sweep())
    elif args.command == "replay-dlq":
        log.info("replayed %d", replay_dlq_cli(rt, args.queue))
    return 0


if __name__ == "__main__":
    sys.exit(main())
