import json
import threading
from datetime import datetime, timezone

import pytest

import token_usage as tu

NOW = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)
STAMP = "2026-09-14T01:00:00Z"


def event(kind, payload, stamp=STAMP):
    return {"timestamp": stamp, "type": kind, "payload": payload}


def meta(thread="thread"):
    return event("session_meta", {"id": thread})


def context(turn="turn", model="gpt-5.6-luna", effort="low"):
    return event("turn_context", {"turn_id": turn, "model": model, "effort": effort})


def response(identity="response", turn="turn", thread="thread", stamp=STAMP, **overrides):
    usage = dict(input_tokens=100, cached_input_tokens=80, cache_write_input_tokens=0,
                 output_tokens=20, reasoning_output_tokens=5, total_tokens=120)
    usage.update(overrides)
    return event("token_usage_record", dict(response_id=identity, thread_id=thread,
        turn_id=turn, usage=usage, turn_token_usage={"total_tokens": 999999},
        thread_token_usage={"total_tokens": 9999999}), stamp)


def write(path, records, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


@pytest.fixture
def ledger(tmp_path):
    store = tu.TokenUsageStore(tmp_path / "app" / "usage.sqlite3")
    root = tmp_path / "codex"
    path = root / "sessions" / "session.jsonl"
    collector = tu.TokenUsageCollector(store, root)
    return store, collector, path


def report(store, period="all"):
    return store.report(period, NOW)


def scan(collector):
    return list(collector.scan())


def test_model_effort_switches_versions_future_models_and_multiple_responses(ledger):
    store, collector, path = ledger
    combos = [("gpt-5.6-luna", "low"), ("gpt-5.6-luna", "high"),
              ("gpt-5.6-sol", "low"), ("future-model-v2", "ultra"), ("old-retired-model", "medium")]
    records = [meta()]
    for i, (model, effort) in enumerate(combos):
        records += [context(str(i), model, effort), response(str(i), str(i))]
    records.append(response("extra", "0"))
    write(path, records)
    scan(collector)
    result = report(store)
    assert result.total_tokens == 720 and result.responses == 6
    assert {(g.model, g.effort) for g in result.groups} == set(combos)
    group = next(g for g in result.groups if (g.model, g.effort) == combos[0])
    assert (group.input_tokens, group.output_tokens, group.total_tokens) == (200, 40, 240)
    assert group.cached_input_tokens == 160 and group.reasoning_output_tokens == 10
    rows, count = store.turns("all", *combos[0], now=NOW)
    assert count == 1 and rows[0]["responses"] == 2 and rows[0]["total_tokens"] == 240


def test_refresh_restart_archive_copy_and_new_response_are_idempotent(ledger):
    store, collector, path = ledger
    records = [meta(), context(), response()]
    write(path, records)
    scan(collector)
    assert scan(collector)[-1].bytes_read == 0
    archived = collector.root / "archived_sessions" / path.name
    archived.parent.mkdir()
    path.rename(archived)
    write(path.with_name("fork.jsonl"), [meta("fork"), *records[1:]])
    scan(tu.TokenUsageCollector(tu.TokenUsageStore(store.path), collector.root))
    assert report(store).total_tokens == 120
    write(archived, [response("new")], "a")
    scan(collector)
    assert report(store).total_tokens == 240
    archived.unlink()
    scan(collector)
    assert report(store).total_tokens == 240


def test_null_details_unknown_context_and_late_context(ledger):
    store, collector, path = ledger
    r = response()
    r["payload"]["usage"].pop("cached_input_tokens")
    write(path, [meta(), r])
    scan(collector)
    result = report(store)
    assert result.groups[0].model == "" and result.groups[0].cached_input_tokens is None
    assert result.issues["unknown"] == 1 and result.issues["missing_details"] == 1
    write(path, [context()], "a")
    scan(collector)
    assert report(store).groups[0].model == "gpt-5.6-luna"
    assert report(store).issues["unknown"] == 0


def test_conflicting_response_excluded_without_overwrite(ledger):
    store, collector, path = ledger
    write(path, [meta(), context(), response(), response(output_tokens=30, total_tokens=130)])
    scan(collector)
    result = report(store)
    assert result.total_tokens == 0 and result.issues["conflict"] == 1
    scan(collector)
    assert report(store).issues["conflict"] == 1


@pytest.mark.parametrize("overrides", [dict(input_tokens=-1), dict(input_tokens=True),
    dict(output_tokens="20"), dict(total_tokens=999), dict(reasoning_output_tokens=21),
    dict(cached_input_tokens=101), dict(total_tokens=None)])
def test_invalid_usage_not_fabricated(ledger, overrides):
    store, collector, path = ledger
    write(path, [meta(), context(), response(**overrides)])
    scan(collector)
    assert report(store).responses == 0 and report(store).issues["invalid_usage"] == 1


def test_legacy_cumulative_events_never_added_and_modern_side_events_not_gaps(ledger):
    store, collector, path = ledger
    legacy = event("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 120}, "last_token_usage": {"total_tokens": 120}}})
    write(path, [meta(), context(), response(), legacy, legacy, context("old"),
                 event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 400}}})])
    scan(collector)
    result = report(store)
    assert result.total_tokens == 120 and result.issues["legacy"] == 1


@pytest.mark.parametrize("marker", [event("event_msg", {"type": "model_rerouted", "turn_id": "turn"}),
                                  context(effort="high")])
def test_ambiguous_turn_attribution_is_unknown(ledger, marker):
    store, collector, path = ledger
    write(path, [meta(), context(), response(), marker])
    scan(collector)
    result = report(store)
    assert result.total_tokens == 120 and result.groups[0].model == ""
    assert result.issues["unknown"] == 1


def test_partial_line_and_malformed_complete_line(ledger):
    store, collector, path = ledger
    write(path, [meta(), context()])
    original = path.stat().st_size
    raw = json.dumps(response()).encode()
    with path.open("ab") as f:
        f.write(raw[:40])
    scan(collector)
    with store.connection() as db:
        assert db.execute("SELECT offset FROM sources").fetchone()[0] == original
    with path.open("ab") as f:
        f.write(raw[40:] + b"\nnot json\n")
    scan(collector)
    assert report(store).total_tokens == 120 and report(store).issues["bad_lines"] == 1
    scan(collector)
    assert report(store).issues["bad_lines"] == 1


def test_read_error_retries_and_preserves_totals(ledger, monkeypatch):
    store, collector, path = ledger
    write(path, [meta(), context(), response()])
    scan(collector)
    original = tu.open_shared
    monkeypatch.setattr(tu, "open_shared", lambda p: (_ for _ in ()).throw(PermissionError()))
    scan(collector)
    assert report(store).total_tokens == 120 and report(store).issues["read_errors"] == 1
    monkeypatch.setattr(tu, "open_shared", original)
    scan(collector)
    assert report(store).issues["read_errors"] == 0


def test_transaction_rollback_and_resume(ledger, monkeypatch):
    store, collector, path = ledger
    write(path, [meta(), context(), response()])
    original = collector._record
    def fail_after_insert(db, record, state):
        original(db, record, state)
        if record["type"] == "token_usage_record":
            raise RuntimeError("simulated interruption")
    monkeypatch.setattr(collector, "_record", fail_after_insert)
    with pytest.raises(RuntimeError):
        scan(collector)
    assert report(store).total_tokens == 0
    monkeypatch.setattr(collector, "_record", original)
    scan(collector)
    assert report(store).total_tokens == 120


def test_stopping_between_batches_resumes_context_and_cursor(ledger, monkeypatch):
    store, collector, path = ledger
    monkeypatch.setattr(tu, "BATCH_LINES", 2)
    write(path, [meta(), context(), *[response(str(i)) for i in range(9)]])
    stop = threading.Event()
    generator = collector.scan(stop=stop)
    next(generator)
    stop.set()
    list(generator)
    assert report(store).responses == 0
    scan(tu.TokenUsageCollector(store, collector.root))
    assert report(store).responses == 9 and report(store).issues["unknown"] == 0


def test_replaced_and_truncated_files_do_not_erase_old_usage(ledger):
    store, collector, path = ledger
    write(path, [meta(), context(), response()])
    scan(collector)
    path.unlink()
    write(path, [meta(), context(), response("replacement")])
    scan(collector)
    assert report(store).responses == 2
    path.write_bytes(b"")
    scan(collector)
    write(path, [meta(), context(), response("after-truncate")])
    scan(collector)
    assert report(store).responses == 3


def test_period_boundaries_and_roundtrip_totals(ledger):
    store, collector, path = ledger
    stamps = ["2026-08-31T15:59:59Z", "2026-08-31T16:00:00Z", "2026-09-07T15:59:59Z",
              "2026-09-07T16:00:00Z", "2026-09-13T15:59:59Z", "2026-09-13T16:00:00Z",
              "2026-09-14T15:59:59Z", "2026-09-14T16:00:00Z"]
    write(path, [meta(), context(), *[response(str(i), stamp=s) for i, s in enumerate(stamps)]])
    scan(collector)
    assert report(store, "today").total_tokens == 240
    assert report(store, "week").total_tokens == 480
    assert report(store, "month").total_tokens == 720
    for period in ("today", "week", "month", "all"):
        r = report(store, period)
        rows, _ = store.turns(period, "gpt-5.6-luna", "low", now=NOW)
        assert r.total_tokens == sum(row["total_tokens"] for row in rows)
    assert tu.period_bounds("week", datetime(2026, 10, 1, tzinfo=timezone.utc))[0] == datetime(2026, 9, 24, 16, tzinfo=timezone.utc).timestamp()


def test_no_sensitive_content_is_saved(ledger):
    store, collector, path = ledger
    records = [meta(), context(), event("response_item", {"type": "message", "content": "DO_NOT_STORE_PROMPT"}), response()]
    records[1]["payload"]["developer_instructions"] = "DO_NOT_STORE_INSTRUCTIONS"
    records[-1]["payload"]["secret"] = "DO_NOT_STORE_CREDENTIAL"
    write(path, records)
    scan(collector)
    with store.connection() as db:
        dump = "\n".join(db.iterdump())
    assert "DO_NOT_STORE" not in dump


def test_turn_pagination(ledger):
    store, collector, path = ledger
    records = [meta()]
    for i in range(105):
        records += [context(str(i)), response(str(i), str(i))]
    write(path, records)
    scan(collector)
    first, count = store.turns("all", "gpt-5.6-luna", "low", now=NOW)
    second, _ = store.turns("all", "gpt-5.6-luna", "low", page=1, now=NOW)
    assert count == 105 and len(first) == 100 and len(second) == 5
    assert len({r["turn_id"] for r in first + second}) == 105


def test_fork_with_embedded_parent_metadata_and_inherited_history(ledger):
    store, collector, path = ledger
    native_count = event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 120}}})
    original = [meta("parent"), context("parent-turn"), response("parent-response", "parent-turn", "parent"), native_count]
    write(path, original)
    write(path.with_name("child.jsonl"), [meta("child"), *original,
        context("child-turn", "gpt-5.6-sol", "ultra"),
        response("child-response", "child-turn", "child"),
        event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 240}}})])
    scan(collector)
    result = report(store)
    assert result.total_tokens == 240 and result.issues["unknown"] == 0 and result.issues["legacy"] == 0
    assert {(g.model,g.effort) for g in result.groups} == {("gpt-5.6-luna","low"),("gpt-5.6-sol","ultra")}


def test_in_place_rewrite_after_unchanged_prefix_is_detected(ledger):
    store, collector, path = ledger
    padding = event("response_item", {"type": "message", "content": "x" * 3000})
    write(path, [meta(), context(), padding, response("original")])
    scan(collector)
    write(path, [meta(), context(), padding, response("replaced"), response("extra")])
    scan(collector)
    assert report(store).responses == 3
