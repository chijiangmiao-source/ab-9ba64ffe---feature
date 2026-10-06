"""HTTP API tests: verdicts, replay, conflict, invalid model, health."""

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_STORE", str(tmp_path / "evidence.json"))
    monkeypatch.setenv("HEALTH_ACK", "alive")
    from app import main
    importlib.reload(main)
    return TestClient(main.app)


MODEL = {
    "audit_id": "API-1",
    "locations": ["armed", "cooling", "open", "done"],
    "clocks": ["x", "y"],
    "initial_location": "armed",
    "final_locations": ["done"],
    "transitions": [
        {"id": "t_cooldown", "source": "armed", "target": "cooling",
         "event": "cmd",
         "guards": [{"clock": "x", "lower": 0, "upper": 4}],
         "resets": ["y"]},
        {"id": "t_open", "source": "armed", "target": "open", "event": "cmd",
         "guards": [{"clock": "x", "lower": 5, "upper": 10}],
         "resets": ["y"]},
        {"id": "t_ack_c", "source": "cooling", "target": "done",
         "event": "ack",
         "guards": [{"clock": "y", "lower": 1, "upper": 3}], "resets": []},
        {"id": "t_ack_o", "source": "open", "target": "done", "event": "ack",
         "guards": [{"clock": "y", "lower": 1, "upper": 3}], "resets": []},
    ],
}


def payload(events):
    return {"model": MODEL, "events": events}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_gap_submission_rejected(client):
    r = client.post("/api/reviews", json=payload([
        {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}]))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["earliest_event_index"] == 0
    assert body["stored"]["fingerprint"]
    assert len(body["steps"]) == 1


def test_freezing_submission(client):
    r = client.post("/api/reviews", json=payload([
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}]))
    assert r.status_code == 200
    assert r.json()["status"] == "frozen"


def test_semantic_equivalent_retransmission_replays(client):
    p = payload([{"event": "cmd", "relative_lower": 5,
                  "relative_upper": 10}])
    r1 = client.post("/api/reviews", json=p)
    b1 = r1.json()
    assert b1["status"] == "rejected"  # not final after one event
    # reorder JSON keys + whitespace -> semantically identical bytes content
    p2 = {"events": list(reversed(p["events"])),
          "model": dict(reversed(list(p["model"].items())))}
    # NOTE events order must stay identical; rebuild canonically instead
    p2 = {"model": {k: MODEL[k] for k in reversed(list(MODEL))},
          "events": p["events"]}
    r2 = client.post("/api/reviews", json=p2)
    b2 = r2.json()
    assert b2["status"] == "rejected"
    assert b2["replay"]["semantically_equivalent_retransmission"] is True
    assert b2["replay"]["original_fingerprint"] == b1["stored"]["fingerprint"]
    # same verdict/evidence content
    assert b2["reason"] == b1["reason"]
    assert b2["earliest_event_index"] == b1["earliest_event_index"]


def test_same_id_different_content_conflicts_and_keeps_original(client):
    p = payload([{"event": "cmd", "relative_lower": 5,
                  "relative_upper": 10}])
    b1 = client.post("/api/reviews", json=p).json()
    p2 = payload([{"event": "cmd", "relative_lower": 0,
                   "relative_upper": 4}])
    p2["model"] = {**MODEL}
    r = client.post("/api/reviews", json=p2)
    assert r.status_code == 409
    b2 = r.json()
    assert b2["status"] == "conflict"
    assert b2["conflict"]["original_fingerprint"] == b1["stored"]["fingerprint"]
    assert b2["conflict"]["incoming_fingerprint"] != \
        b1["stored"]["fingerprint"]
    assert b2["conflict"]["original_evidence"]["status"] == b1["status"]
    # GET still returns the original retained evidence
    g = client.get("/api/reviews/API-1").json()
    assert g["status"] == b1["status"]


def test_invalid_overlap_model_reported(client):
    import copy
    bad = copy.deepcopy(MODEL)
    bad["transitions"][0]["guards"][0]["upper"] = 5  # [0,5] vs [5,10]
    r = client.post("/api/reviews", json={"model": bad, "events": [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 5}]})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_model"
    assert body["error"]["details"]["code"] == "overlapping_guards"


def test_bad_json_and_missing_fields(client):
    r = client.post("/api/reviews", content="not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/reviews", json={"events": []})
    assert r.status_code == 422


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "联锁捕获复核" in r.text


# ---- clock provenance reopen endpoint --------------------------------------

FORK_MODEL = {
    "audit_id": "API-FORK",
    "locations": ["armed", "A", "B", "done"],
    "clocks": ["x", "y"],
    "initial_location": "armed",
    "final_locations": ["done"],
    "transitions": [
        {"id": "toA", "source": "armed", "target": "A", "event": "cmd",
         "guards": [{"clock": "x", "lower": 0, "upper": 4}],
         "resets": ["y"]},
        {"id": "toB", "source": "armed", "target": "B", "event": "cmd",
         "guards": [{"clock": "x", "lower": 5, "upper": 10}],
         "resets": []},
    ],
}


def _submit(client, model, events):
    return client.post("/api/reviews", json={"model": model,
                                             "events": events})


def test_lineage_select_choices_from_real_record(client):
    r = _submit(client, FORK_MODEL, [
        {"event": "cmd", "relative_lower": 2, "relative_upper": 8}])
    assert r.json()["status"] == "rejected"
    s = client.get("/api/reviews/API-FORK/lineage").json()
    assert s["status"] == "select"
    assert s["clocks"] == ["x", "y"]
    assert [e["event_index"] for e in s["processed_events"]] == [0]
    assert s["processed_events"][0]["region_count"] == 2


def test_lineage_summary_forking_reset_and_unreset(client):
    _submit(client, FORK_MODEL, [
        {"event": "cmd", "relative_lower": 2, "relative_upper": 8}])
    r = client.get("/api/reviews/API-FORK/lineage?clock=y&event=0")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict_status"] == "rejected"
    assert body["regions_total"] == 2
    cats = {g["source_category"] for g in body["summary"]}
    assert cats == {"initial", "reset"}
    reset_g = next(g for g in body["summary"]
                   if g["source_category"] == "reset")
    assert reset_g["reset_transition"] == "toA"
    assert reset_g["reset_event"] == "cmd"
    assert reset_g["witness_after_event"]["y"]["text"] == "0"
    # partition
    ids = [i for g in body["summary"] for i in g["region_ids"]]
    assert len(ids) == len(set(ids)) == 2


def test_lineage_frozen_record_readable_and_stable(client):
    frozen_model = {**MODEL, "audit_id": "API-FROZEN-LINE"}
    events = [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}]
    _submit(client, frozen_model, events)
    r = client.get("/api/reviews/API-FROZEN-LINE/lineage?clock=y&event=1")
    body = r.json()
    assert r.status_code == 200
    assert body["verdict_status"] == "frozen"
    g = body["summary"][0]
    # y is reset by the cmd transition and stays so into the ack event
    assert g["source_category"] == "reset"
    assert g["reset_transition"] == "t_open"
    assert g["reset_event_index"] == 0
    # repeated reads are identical (stable grouping)
    body2 = client.get(
        "/api/reviews/API-FROZEN-LINE/lineage?clock=y&event=1").json()
    assert body2["summary"] == body["summary"]


def test_lineage_query_does_not_change_frozen_conclusion(client):
    frozen_model = {**MODEL, "audit_id": "API-IMMUT-LINE"}
    events = [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}]
    _submit(client, frozen_model, events)
    before = client.get("/api/reviews/API-IMMUT-LINE").json()
    for url in ("/api/reviews/API-IMMUT-LINE/lineage",
                "/api/reviews/API-IMMUT-LINE/lineage?clock=zzz&event=0",
                "/api/reviews/API-IMMUT-LINE/lineage?clock=y&event=9"):
        client.get(url)
    after = client.get("/api/reviews/API-IMMUT-LINE").json()
    for key in ("status", "reason", "earliest_event_index", "steps",
                "failure", "final_states"):
        assert after.get(key) == before.get(key)
    assert after["fingerprint"] == before["fingerprint"]


def test_lineage_unknown_clock_and_future_event_refused(client):
    _submit(client, FORK_MODEL, [
        {"event": "cmd", "relative_lower": 2, "relative_upper": 8},
        {"event": "ack", "relative_lower": 1, "relative_upper": 1}])
    r = client.get("/api/reviews/API-FORK/lineage?clock=zzz&event=0")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["code"] == "unknown_clock"
    # event 1 follows the rejection's first failure at event 0
    r = client.get("/api/reviews/API-FORK/lineage?clock=y&event=1")
    assert r.status_code == 422
    d = r.json()["error"]["details"]
    assert d["code"] == "event_beyond_processed_prefix"
    assert d["processed_event_indices"] == [0]
    # the original rejected conclusion is still readable unchanged
    g = client.get("/api/reviews/API-FORK").json()
    assert g["status"] == "rejected"
    assert g["earliest_event_index"] == 0


def test_lineage_unknown_record_404(client):
    r = client.get("/api/reviews/NOPE/lineage?clock=x&event=0")
    assert r.status_code == 404
    assert r.json()["status"] == "not_found"


def test_lineage_legacy_record_without_trace_refused(client):
    from app import main
    from app.storage import _LOCK
    legacy_id = "API-LEGACY-NO-TRACE"
    # submit normally, then emulate a record written before provenance existed
    _submit(client, {**MODEL, "audit_id": legacy_id}, [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}])
    with _LOCK:
        main.store._data[legacy_id]["lineage_trace"] = None
    r = client.get(f"/api/reviews/{legacy_id}/lineage?clock=y&event=0")
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "lineage_query_rejected"
    assert body["error"]["details"]["code"] == "missing_lineage_trace"
    # the old frozen conclusion is still readable
    g = client.get(f"/api/reviews/{legacy_id}").json()
    assert g["status"] == "frozen"
