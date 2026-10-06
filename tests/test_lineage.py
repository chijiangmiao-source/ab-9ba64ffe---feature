"""Clock provenance (last-reset lineage) tests.

When a frozen or rejected capture is reopened, a chosen clock at an already
processed event must be attributed, over every still-feasible region, either
to the initial instant or to the concrete transition that last reset it.
These tests pin:

  * source groups are disjoint and partition the feasible regions,
  * forking reset vs. never-reset paths are kept apart even when the clock
    displays the same value (0) on both,
  * two different reset transitions are never merged by displayed value,
  * witnesses before/after the event are exact rationals inside the zones,
  * queries past the first failing event, for unknown clocks, or against
    records without traceable evidence are refused without changing verdict.
"""

from fractions import Fraction

from app.engine import (build_lineage_trace, lineage_summary,
                        LineageQueryError, parse_capture, parse_model,
                        review, Zone)


def fr(node):
    return Fraction(node["numerator"], node["denominator"])


# A capture rejected at event 0: the window straddles the (4,5) gap, so two
# covered pieces remain feasible -- one transition resets y, the other does
# not.  At t=5..8 the initial clock y equals the reset value (0..), and at the
# boundary witnesses the displayed value is 0 on both sides: provenance must
# still split.
FORK_MODEL = {
    "audit_id": "FORK",
    "locations": ["armed", "A", "B", "done"],
    "clocks": ["x", "y"],
    "initial_location": "armed",
    "final_locations": ["done"],
    "transitions": [
        {"id": "toA", "source": "armed", "target": "A", "event": "e0",
         "guards": [{"clock": "x", "lower": 0, "upper": 4}],
         "resets": ["y"]},
        {"id": "toB", "source": "armed", "target": "B", "event": "e0",
         "guards": [{"clock": "x", "lower": 5, "upper": 10}],
         "resets": []},
    ],
}
FORK_EVENTS = [{"event": "e0", "relative_lower": 2,
                "relative_upper": 8}]


def trace_of(model_json, events_json):
    m = parse_model(model_json)
    evs = parse_capture(events_json)
    verdict = review(m, evs)
    return m, evs, verdict, build_lineage_trace(m, evs, verdict)


def test_forking_reset_and_unreset_paths_kept_apart():
    _, _, verdict, tr = trace_of(FORK_MODEL, FORK_EVENTS)
    assert verdict["status"] == "rejected"
    assert verdict["earliest_event_index"] == 0
    s = lineage_summary(tr, "y", 0)
    assert s["status"] == "ok"
    assert s["regions_total"] == 2
    cats = {g["source_category"] for g in s["summary"]}
    assert cats == {"initial", "reset"}

    by_cat = {g["source_category"]: g for g in s["summary"]}
    reset_g = by_cat["reset"]
    init_g = by_cat["initial"]
    assert reset_g["reset_event_index"] == 0
    assert reset_g["reset_event"] == "e0"
    assert reset_g["reset_transition"] == "toA"
    assert reset_g["feasible_region_count"] == 1
    assert init_g["feasible_region_count"] == 1

    # groups are non-overlapping partitions of the feasible regions
    ids = [rid for g in s["summary"] for rid in g["region_ids"]]
    assert len(ids) == len(set(ids)) == s["regions_total"]
    assert {tuple(p) for p in reset_g["paths"]} == {("toA",)}
    assert {tuple(p) for p in init_g["paths"]} == {("toB",)}


def test_witnesses_are_exact_and_distinguish_equal_displayed_value():
    # The reset branch yields y == 0 after the event; the never-reset branch
    # inherits a positive value.  The two sources remain separate groups even
    # though a reset value of zero is never confused with an inherited zero.
    _, _, _, tr = trace_of(FORK_MODEL, FORK_EVENTS)
    s = lineage_summary(tr, "y", 0)
    for g in s["summary"]:
        for ev in g["traceable_zone_evidence"]:
            before = fr(ev["witness_before_event"]["y"])
            after = fr(ev["witness_after_event"]["y"])
            assert before >= 0 and after >= 0
        # the after-witness of a reset source is exactly 0
        if g["source_category"] == "reset":
            assert fr(g["witness_after_event"]["y"]) == 0
            assert g["reset_transition"] == "toA"
    # two groups despite reset value 0: the never-reset group's after value
    # is the inherited (positive) value, never conflated with the reset.
    init_after = fr(next(g for g in s["summary"]
                         if g["source_category"] == "initial")
                    ["witness_after_event"]["y"])
    assert init_after > 0


def test_distinct_reset_transitions_not_merged_by_value():
    model = {
        "audit_id": "SV",
        "locations": ["s", "A", "B", "F"],
        "clocks": ["x", "y"],
        "initial_location": "s",
        "final_locations": ["F"],
        "transitions": [
            {"id": "rA", "source": "s", "target": "A", "event": "e0",
             "guards": [{"clock": "x", "lower": 0, "upper": 2}],
             "resets": ["y"]},
            {"id": "rB", "source": "s", "target": "B", "event": "e0",
             "guards": [{"clock": "x", "lower": 3, "upper": 5}],
             "resets": ["y"]},
        ],
    }
    _, _, _, tr = trace_of(
        model, [{"event": "e0", "relative_lower": 0, "relative_upper": 5}])
    s = lineage_summary(tr, "y", 0)
    resets = [g for g in s["summary"]
              if g["source_category"] == "reset"]
    assert len(resets) == 2
    assert {g["reset_transition"] for g in resets} == {"rA", "rB"}
    # both display y == 0 after the event
    assert all(fr(g["witness_after_event"]["y"]) == 0 for g in resets)


def test_never_reset_clock_is_initial_throughout():
    model = {
        "audit_id": "INIT",
        "locations": ["s", "A", "F"], "clocks": ["x", "y"],
        "initial_location": "s", "final_locations": ["F"],
        "transitions": [
            {"id": "g", "source": "s", "target": "A", "event": "e0",
             "guards": [{"clock": "x", "lower": 0, "upper": 10}],
             "resets": ["y"]},
            {"id": "f", "source": "A", "target": "F", "event": "e1",
             "guards": [{"clock": "y", "lower": 1, "upper": 3}],
             "resets": []}],
    }
    _, _, verdict, tr = trace_of(model, [
        {"event": "e0", "relative_lower": 5, "relative_upper": 10},
        {"event": "e1", "relative_lower": 1, "relative_upper": 3}])
    assert verdict["status"] == "frozen"
    # x is never reset: initial source at both processed events
    for k in (0, 1):
        s = lineage_summary(tr, "x", k)
        assert len(s["summary"]) == 1
        assert s["summary"][0]["source_category"] == "initial"
    # y is reset by g at event 0 and the label persists into event 1
    s1 = lineage_summary(tr, "y", 1)
    g = s1["summary"][0]
    assert g["source_category"] == "reset"
    assert g["reset_event_index"] == 0
    assert g["reset_transition"] == "g"
    assert g["feasible_region_count"] == 1


def test_witnesses_lie_inside_their_zones():
    def satisfies(values, constraints, clock):
        v = {"t0": Fraction(0), clock: values[clock]}
        for c in constraints:
            lhs, rhs = [x.strip() for x in c["lhs"].split("-")]
            if lhs not in v or rhs not in v:
                continue  # other-clock constraints; witness is full assignment
            b = fr(c["bound"])
            d = v[lhs] - v[rhs]
            if not (d < b if c["op"] == "<" else d <= b):
                return False
        return True

    _, _, _, tr = trace_of(FORK_MODEL, FORK_EVENTS)
    s = lineage_summary(tr, "y", 0)
    for g in s["summary"]:
        for ev in g["traceable_zone_evidence"]:
            bv = {"y": fr(ev["witness_before_event"]["y"])}
            av = {"y": fr(ev["witness_after_event"]["y"])}
            assert satisfies(bv, ev["guard_zone"], "y")
            assert satisfies(av, ev["post_reset_zone"], "y")
        # rational witness shape is preserved
        wb = g["witness_before_event"]["y"]
        wa = g["witness_after_event"]["y"]
        assert wb["text"] == f"{wb['numerator']}" or "/" in wb["text"]
        assert set(wa) >= {"numerator", "denominator", "decimal", "text"}


def test_query_past_first_failure_is_refused():
    _, _, _, tr = trace_of(FORK_MODEL, FORK_EVENTS + [
        {"event": "e1", "relative_lower": 1, "relative_upper": 1}])
    # event 1 exists in the capture but follows the first failure at event 0
    with __import__("pytest").raises(LineageQueryError) as ei:
        lineage_summary(tr, "y", 1)
    assert ei.value.details["code"] == "event_beyond_processed_prefix"
    assert ei.value.details["processed_event_indices"] == [0]
    # event 0 itself is still answerable
    assert lineage_summary(tr, "y", 0)["regions_total"] == 2


def test_not_final_rejection_allows_last_processed_event():
    model = {
        "audit_id": "NF", "locations": ["s", "A", "F"],
        "clocks": ["x", "y"], "initial_location": "s",
        "final_locations": ["F"],
        "transitions": [
            {"id": "g", "source": "s", "target": "A", "event": "e0",
             "guards": [{"clock": "x", "lower": 0, "upper": 10}],
             "resets": ["y"]}],
    }
    _, _, verdict, tr = trace_of(
        model, [{"event": "e0", "relative_lower": 5,
                 "relative_upper": 10}])
    assert verdict["failure"]["kind"] == "not_final"
    s = lineage_summary(tr, "y", 0)
    assert s["event_outcome"] == "propagated"
    assert s["terminal_outcome"] == "not_final"
    assert s["summary"][0]["reset_transition"] == "g"
    with __import__("pytest").raises(LineageQueryError):
        lineage_summary(tr, "y", 1)


def test_unknown_clock_refused():
    _, _, _, tr = trace_of(FORK_MODEL, FORK_EVENTS)
    with __import__("pytest").raises(LineageQueryError) as ei:
        lineage_summary(tr, "nope", 0)
    assert ei.value.details["code"] == "unknown_clock"


def test_missing_trace_evidence_refused():
    with __import__("pytest").raises(LineageQueryError) as ei:
        lineage_summary(None, "y", 0)
    assert ei.value.details["code"] == "missing_lineage_trace"
    with __import__("pytest").raises(LineageQueryError) as ei:
        lineage_summary({}, "y", 0)
    assert ei.value.details["code"] == "missing_lineage_trace"


def test_labels_match_independent_last_reset_along_paths():
    """Independent scalar check: a region's source equals the last transition
    on its concrete path that resets the clock (initial if none resets it)."""
    m = parse_model(FORK_MODEL)
    evs = parse_capture(FORK_EVENTS)
    verdict = review(m, evs)
    trace = build_lineage_trace(m, evs, verdict)
    tr_by_id = {t.id: t for t in m.transitions}
    for estep in trace["event_regions"]:
        for region in estep["regions"]:
            for path in region["paths"]:
                for ci, cname in enumerate(m.clocks):
                    label = region["clock_sources"][cname]
                    resetters = [tid for tid in path
                                 if ci in tr_by_id[tid].resets]
                    if resetters:
                        last = tr_by_id[resetters[-1]]
                        assert label["category"] == "reset"
                        assert label["transition"] == last.id
                    else:
                        assert label["category"] == "initial"


def test_lineage_regions_match_verdict_branches():
    """Source tagging stays in lock-step with the verdict's zone split.

    At propagated events the lineage fired transitions equal the verdict's
    enabled branches.  At the failing event the verdict emits no branches
    (it stops on the gap); there the lineage must equal the independently
    computed guard-covered transitions of the advanced zone -- the pieces
    still feasible on either side of the gap.
    """
    m = parse_model(FORK_MODEL)
    evs = parse_capture(FORK_EVENTS)
    verdict = review(m, evs)
    trace = build_lineage_trace(m, evs, verdict)
    for tstep, estep in zip(verdict["steps"], trace["event_regions"]):
        assert tstep["event_index"] == estep["event_index"]
        if estep["outcome"] == "propagated":
            region_ts = {c["transition"]
                         for r in estep["regions"]
                         for c in r["contributors"]}
            assert region_ts == {b["transition"]
                                 for b in tstep["branches"]}
    # failing event keeps both covered pieces (regions), gap excluded
    last = trace["event_regions"][-1]
    fired = {c["transition"] for r in last["regions"]
             for c in r["contributors"]}
    assert fired == {"toA", "toB"}
    # and it is exactly the guard boxes feasible against the advanced zone
    w = evs[0]
    adv = Zone.initial(m.n).restrict_elapse_window(w.lo, w.hi)
    feas = {t.id for t in m.transitions
            if adv.intersect(t.guard_zone(m.n)).is_satisfiable()}
    assert fired == feas
