import io
import logging

import pytest

from ytp.common import logs
from ytp.common.models import State
from ytp.common.repo import StaleState
from ytp.common.store import Conflict


def test_tokens_never_reach_the_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(logs.RedactingFilter())
    handler.setFormatter(logs.JsonFormatter())
    log = logging.getLogger("redaction-test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    secrets = ["ya29.a0AfB_byC-secretAccessToken123", "1//0gLongRefreshTokenValue_abcdefghijk",
               "sk-ant-api03-supersecretkey1234567890", "AIzaSyD-1234567890abcdefghijklmnopqrstuv",
               "IGQWRPa1ZAabcdefghijklmnopqrstuvwxyz0123456789ABCDEF", "hunter2secret"]
    log.info("Authorization: Bearer %s", secrets[0])
    log.info("refresh_token=%s&client_secret=%s", secrets[1], secrets[5])
    log.info("key %s and %s", secrets[2], secrets[3])
    log.info("{'access_token': '%s'}", secrets[4])
    log.info("https://x.blob.core.windows.net/media/a.mp4?sv=1&sig=abcdef%2Bsecret")
    try:
        raise ValueError(f"upstream said access_token={secrets[0]}")
    except ValueError:
        log.exception("failed")
    output = stream.getvalue()
    for secret in secrets + ["abcdef%2Bsecret"]:
        assert secret not in output
    assert "[REDACTED]" in output


def test_optimistic_concurrency(rt):
    store = rt.store
    row = store.insert("t", {"PartitionKey": "p", "RowKey": "r", "v": 1})
    store.update("t", {**row, "v": 2})
    with pytest.raises(Conflict):
        store.update("t", {**row, "v": 3})       # stale etag
    with pytest.raises(Conflict):
        store.insert("t", {"PartitionKey": "p", "RowKey": "r"})


def test_transactions_are_atomic(rt):
    store = rt.store
    store.insert("t", {"PartitionKey": "p", "RowKey": "a"})
    with pytest.raises(Conflict):
        store.transact("t", [("insert", {"PartitionKey": "p", "RowKey": "b"}),
                             ("insert", {"PartitionKey": "p", "RowKey": "a"})])
    assert store.get("t", "p", "b") is None


def test_illegal_and_stale_transitions(pipeline, rt):
    pipeline.discover("k")
    cid = rt.repo.items()[0]["PartitionKey"]
    with pytest.raises(ValueError):
        rt.repo.transition(cid, State.DISCOVERED, State.PUBLISHED, "x")
    with pytest.raises(StaleState):
        rt.repo.transition(cid, State.APPROVED, State.RENDERING, "x")


def test_azure_backend_refuses_dev_auth():
    from ytp.common.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, backend="azure", storage_account="a", key_vault_uri="https://v/",
                 console_dev_auth=True).check()
