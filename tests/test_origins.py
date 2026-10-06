"""Tests for frozen/rejected reopen clock-origin (reset provenance).

Covers:
  * exact rational before/after witnesses for initial vs reset sources,
  * non-overlapping per-source region summaries,
  * the same clock reset by different feasible transitions is *not* merged by
    displayed value (both post-reset values are 0),
  * forking reset vs never-reset paths at a rejected first-failure event,
  * refusal for unknown clock, events beyond the first failure, non-integer
    selection, missing parameters, records without traceable region evidence,
  * read-only behaviour: the stored frozen/rejected conclusion is unchanged
    and remains readable.
"""

import importlib
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient


def _model(audit_id="ORG-1", *, open_resets=("y",)):
    return {
        "audit_id": audit_id,
        "locations": ["armed", "cooling", "open", "done"],
        "clocks": ["x", "y"],
        "initial_location": "armed",
        "final_locations": ["done"],
        "transitions": [
            {"id": "t_cooldown", "source": "armed", "target": "cooling",
             "event": "cmd",
             "guards": [{"clock": "x", "lower": 0, "upper": 4}],
             "resets": ["y"]},
            {"id": "t_open", "source": "armed", "target": "open",
             "event": "cmd",
             "guards": [{"clock": "x", "lower": 5, "upper": 10}],
             "resets": list(open_resets)},
            {"id": "t_ack_c", "source": "cooling", "target": "done",
             "event": "ack",
             "guards": [{"clock": "y", "lower": 1, "upper": 3}],
             "resets": []},
            {"id": "t_ack_o", "source": "open", "target": "done",
             "event": "ack",
             "guards": [{"clock": "y", "lower": 1, "upper": 3}],
             "resets": []},
        ],
    }


GAP_EVENTS = [
    {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
    {"event": "ack", "relative_lower": 2, "relative_upper": 2},
]
FREEZE_EVENTS = [
    {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
    {"event": "ack", "relative_lower": 1, "relative_upper": 3},
]


# ---- engine level -----------------------------------------------------------

from app.engine import (OriginQueryError, origin_query, parse_capture,
                        parse_model, review)  # noqa: E402


def _f(text_frac):
    return Fraction(text_frac)


def test_rejected_event_splits_equal_zero_resets_into_two_sources():
    # At the first failing event the window [4,6] meets both closed guard
    # cells (x in [0,4] and x in [5,10]); both transitions reset y to 0.
    # The two displayed post-reset values are equal (0) yet the sources are
    # distinct and must stay in separate, non-overlapping groups.
    m = parse_model(_model())
    evs = parse_capture(GAP_EVENTS)
    verdict = review(m, evs)
    assert verdict["status"] == "rejected"
    out = origin_query(m, evs, verdict, "y", 0)
    sources = [(g["origin"], g["reset_transition"])
               for g in out["origins"]]
    assert sources == [("reset", "t_cooldown"), ("reset", "t_open")]
    assert out["regions_total"] == 2
    assert out["entry_cells_pairwise_disjoint"] is True
    for g in out["origins"]:
        assert g["feasible_region_count"] == 1
        assert g["witness_after"]["clock_value"]["text"] == "0"
        before = g["witness_before"]["clock_value"]
        assert Fraction(before["numerator"], before["denominator"]) > 0
    # the groups' entered guard cells are really disjoint: one on x<=4,
    # the other on x>=5.
    locs = {rg["location"] for g in out["origins"] for rg in g["regions"]}
    assert locs == {"cooling", "open"}


def test_unreset_clock_is_initial_source_with_straddling_witness():
    m = parse_model(_model())
    evs = parse_capture(GAP_EVENTS)
    verdict = review(m, evs)
    out = origin_query(m, evs, verdict, "x", 0)
    assert len(out["origins"]) == 1
    g = out["origins"][0]
    assert g["origin"] == "initial"
    assert g["reset_transition"] is None
    # one substitutable rational value before and after the event (unreset)
    assert g["witness_before"]["clock_value"] == \
        g["witness_after"]["clock_value"]
    # both feasible cells (cooling/open) are covered by the single source
    assert g["feasible_region_count"] == 2


def test_forking_reset_and_unreset_paths_are_kept_separate():
    # t_open no longer resets y: window [0,8] meets both cells at the failing
    # event; y is reset on the cooling branch but inherited from the initial
    # instant on the open branch.
    m = parse_model(_model(open_resets=()))
    evs = parse_capture([
        {"event": "cmd", "relative_lower": 0, "relative_upper": 8},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}])
    verdict = review(m, evs)
    assert verdict["status"] == "rejected"
    out = origin_query(m, evs, verdict, "y", 0)
    kinds = {g["origin"]: g for g in out["origins"]}
    assert set(kinds) == {"initial", "reset"}
    assert kinds["reset"]["reset_transition"] == "t_cooldown"
    assert kinds["reset"]["witness_after"]["clock_value"]["text"] == "0"
    init = kinds["initial"]
    iv = Fraction(init["witness_before"]["clock_value"]["numerator"],
                  init["witness_before"]["clock_value"]["denominator"])
    assert iv > 0  # the never-reset branch carries positive elapsed time
    assert out["entry_cells_pairwise_disjoint"] is True


def test_frozen_verdict_traces_reset_and_inherited_clocks():
    m = parse_model(_model())
    evs = parse_capture(FREEZE_EVENTS)
    verdict = review(m, evs)
    assert verdict["status"] == "frozen"

    y = origin_query(m, evs, verdict, "y", 1)["origins"]
    assert len(y) == 1 and y[0]["origin"] == "reset"
    assert y[0]["reset_event_index"] == 0
    assert y[0]["reset_transition"] == "t_open"

    x = origin_query(m, evs, verdict, "x", 1)["origins"]
    assert len(x) == 1 and x[0]["origin"] == "initial"


def test_admission_refusals_at_engine_level():
    m = parse_model(_model())
    evs = parse_capture(GAP_EVENTS)
    verdict = review(m, evs)

    with pytest.raises(OriginQueryError) as ei:
        origin_query(m, evs, verdict, "no_such_clock", 0)
    assert ei.value.code == "unknown_clock"

    with pytest.raises(OriginQueryError) as ei:
        origin_query(m, evs, verdict, "y", 1)  # past first failure
    assert ei.value.code == "event_beyond_first_failure"

    with pytest.raises(OriginQueryError) as ei:
        origin_query(m, evs, verdict, "y", 5)
    assert ei.value.code == "event_not_processed"

    with pytest.raises(OriginQueryError) as ei:
        origin_query(m, evs, verdict, "y", -1)
    assert ei.value.code == "event_not_processed"


# ---- API level --------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_STORE", str(tmp_path / "evidence.json"))
    monkeypatch.setenv("HEALTH_ACK", "alive")
    from app import main
    importlib.reload(main)
    return TestClient(main.app)


def _submit(client, events, audit_id="ORG-1", model=None):
    return client.post("/api/reviews",
                       json={"model": model or _model(audit_id),
                             "events": events})


def test_origins_endpoint_on_rejected_capture(client):
    r = _submit(client, GAP_EVENTS)
    assert r.json()["status"] == "rejected"
    g = client.get("/api/reviews/ORG-1/origins?clock=y&event=0").json()
    assert g["status"] == "clock_origins"
    assert [o["reset_transition"] for o in g["origins"]] == \
        ["t_cooldown", "t_open"]
    assert g["verdict_status"] == "rejected"


def test_unknown_clock_refused_and_verdict_unchanged(client):
    _submit(client, GAP_EVENTS)
    r = client.get("/api/reviews/ORG-1/origins?clock=zz&event=0")
    assert r.status_code == 422
    b = r.json()
    assert b["status"] == "origin_query_refused"
    assert b["error"]["code"] == "unknown_clock"
    # original frozen/rejected conclusion still readable, untouched
    g = client.get("/api/reviews/ORG-1").json()
    assert g["status"] == "rejected"
    assert g["earliest_event_index"] == 0


def test_event_beyond_first_failure_refused(client):
    _submit(client, GAP_EVENTS)
    r = client.get("/api/reviews/ORG-1/origins?clock=y&event=1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "event_beyond_first_failure"


def test_bad_and_missing_selection_refused(client):
    _submit(client, FREEZE_EVENTS)
    r = client.get("/api/reviews/ORG-1/origins?clock=y&event=nope")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "event_not_processed"
    r = client.get("/api/reviews/ORG-1/origins?clock=y")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_selection"


def test_unknown_audit_returns_404(client):
    r = client.get("/api/reviews/NOPE/origins?clock=y&event=0")
    assert r.status_code == 404


def test_invalid_model_record_has_no_traceable_regions(client):
    bad = _model("ORG-BAD")
    bad["transitions"][0]["guards"][0]["upper"] = 5  # touching guards
    r = client.post("/api/reviews", json={"model": bad, "events": [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 5}]})
    assert r.status_code == 422
    q = client.get("/api/reviews/ORG-BAD/origins?clock=y&event=0")
    assert q.status_code == 422
    assert q.json()["error"]["code"] == "no_traceable_regions"
    # old conclusion still readable
    assert client.get("/api/reviews/ORG-BAD").json()["status"] == \
        "invalid_model"


def test_legacy_record_without_payload_is_refused_but_readable(client,
                                                               monkeypatch):
    # Simulate a historical record written before provenance replay existed:
    # it carries a verdict but no original payload/traceable evidence.
    _submit(client, GAP_EVENTS)
    from app import main
    rec = main.store.lookup("ORG-1")
    rec.pop("payload", None)
    main.store._data["ORG-1"] = rec
    q = client.get("/api/reviews/ORG-1/origins?clock=y&event=0")
    assert q.status_code == 422
    assert q.json()["error"]["code"] == "no_traceable_regions"
    assert client.get("/api/reviews/ORG-1").json()["status"] == "rejected"


def test_replay_and_origins_coexist(client):
    # a semantically equivalent retransmission still replays; the persisted
    # record keeps its payload so a later origin query is answerable.
    _submit(client, FREEZE_EVENTS)
    again = client.post("/api/reviews",
                        json={"events": FREEZE_EVENTS, "model": _model()})
    assert again.json()["replay"][
        "semantically_equivalent_retransmission"] is True
    g = client.get("/api/reviews/ORG-1/origins?clock=y&event=0").json()
    assert g["status"] == "clock_origins"
    assert g["origins"][0]["reset_transition"] == "t_open"


# ---- differential provenance invariant --------------------------------------

def _scalar_provenance(model, events, clock_index, target):
    """Concrete delay-grid simulation tracking each clock's last reset.

    Traces trajectories that actually FIRE through the queried ``target``
    event (earlier events always fire uniquely; at a coverage-gap event some
    grid points gap out and are dropped).  Returns provenance keys plus the
    target event's concrete pre-fire and post-fire valuations:
      ("initial",) or ("reset", event_index, transition_id)
    """
    from itertools import product
    grids = []
    for w in events:
        pts = {w.lo, w.hi}
        for q in range(1, 4):
            pts.add(w.lo + (w.hi - w.lo) * q / 4)
        grids.append(sorted(pts))

    traced = []
    for deltas in product(*grids):
        clocks = [Fraction(0)] * len(model.clocks)
        provenance = {c: ("initial",) for c in range(len(model.clocks))}
        loc = model.initial
        ok = True
        for k, d in enumerate(deltas):
            if k > target:
                break
            clocks = [c + d for c in clocks]
            pre_values = {model.clocks[i]: clocks[i]
                          for i in range(len(clocks))}
            ev = events[k].event
            enabled = [t for t in model.transitions
                       if t.source == loc and t.event == ev
                       and all(g.lo <= clocks[g.clock] <= g.hi
                               for g in t.guards)]
            if len(enabled) != 1:
                ok = False
                break
            t = enabled[0]
            for r in t.resets:
                clocks[r] = Fraction(0)
                provenance[r] = ("reset", k, t.id)
            loc = t.target
            if k == target:
                post_frac = {model.clocks[i]: clocks[i]
                             for i in range(len(clocks))}
                traced.append((provenance[clock_index],
                               pre_values, post_frac))
                break
    return traced


def _satisfies(values, constraints):
    v = {"t0": Fraction(0), **values}
    for c in constraints:
        lhs, rhs = [s.strip() for s in c["lhs"].split("-")]
        b = Fraction(c["bound"]["numerator"], c["bound"]["denominator"])
        d = v[lhs] - v[rhs]
        if c["op"] == "<":
            if not d < b:
                return False
        elif not d <= b:
            return False
    return True


def test_provenance_matches_independent_scalar_simulation():
    import random
    import test_invariants as ti

    rng = random.Random(54321)
    checked = 0
    distinct_reset_groups_seen = 0
    for _ in range(400):
        scen = ti._scenario(rng)
        if scen is None:
            continue
        model, windows, _mj, _ej = scen
        verdict = review(model, windows)
        if verdict["status"] != "rejected":
            continue
        k = verdict["earliest_event_index"]
        if verdict["failure"]["kind"] not in ("uncovered_time",
                                              "no_transition"):
            continue
        for ci in range(len(model.clocks)):
            try:
                out = origin_query(model, windows, verdict,
                                   model.clocks[ci], k)
            except OriginQueryError:
                continue
            traced = _scalar_provenance(model, windows, ci, k)
            # group provenance key -> group object
            gmap = {}
            for g in out["origins"]:
                key = (("initial",) if g["origin"] == "initial"
                       else ("reset", g["reset_event_index"],
                             g["reset_transition"]))
                gmap[key] = g
            reset_groups = [g for g in out["origins"]
                            if g["origin"] == "reset"]
            if len(reset_groups) > 1:
                distinct_reset_groups_seen += 1
            for key, pre_vals, post_vals in traced:
                assert key in gmap, (key, list(gmap), _mj, _ej)
                g = gmap[key]
                # the concrete pre-event valuation must fit some region's
                # entered guard cell and the post-event zone constraints
                cells = [cc for rg in g["regions"]
                         for cc in [rg["entry_cell_constraints"]]]
                assert any(_satisfies(pre_vals, cc) for cc in cells), \
                    (key, pre_vals, _mj, _ej)
                zones = [rg["zone_constraints"] for rg in g["regions"]]
                assert any(_satisfies(post_vals, zc) for zc in zones)
            checked += 1
    assert checked > 20, "too few rejected scenarios cross-checked"
    assert distinct_reset_groups_seen > 0, \
        "random search never exercised multiple distinct reset groups"


def test_no_transition_event_has_no_traceable_continuation():
    # Event name with no outgoing transition from the reachable location:
    # the review rejects at that event and the origin reopen must be refused
    # rather than fabricating a reset source (no region actually moves).
    m = parse_model(_model("ORG-NONE"))
    evs = parse_capture([
        {"event": "unhandled", "relative_lower": 0, "relative_upper": 1}])
    verdict = review(m, evs)
    assert verdict["status"] == "rejected"
    assert verdict["earliest_event_index"] == 0
    assert verdict["failure"]["blocking_guards"] == []
    with pytest.raises(OriginQueryError) as ei:
        origin_query(m, evs, verdict, "y", 0)
    assert ei.value.code == "no_traceable_regions"
