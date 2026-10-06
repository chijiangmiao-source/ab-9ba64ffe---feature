"""End-to-end HTTP smoke used by the verify container.

Covers, over the real network API:
  * health response,
  * a legal overlapping-jitter-window capture that freezes,
  * a cooling interval gap rejected at the earliest event with a rational
    clock witness and blocking guards,
  * an illegal model (closed guards touching/overlapping) -> 422,
  * a semantically equivalent retransmission -> same verdict replayed,
  * a same-id different-content submission -> 409 conflict, original kept.
Exits 0 on full success, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

MODEL = {
    "audit_id": "SMOKE-1",
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

failures: list[str] = []


def call(method: str, url: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def main(base: str) -> int:
    print("== health ==")
    s, b = call("GET", f"{base}/health")
    check("health 200", s == 200 and b.get("status") == "ok", str(b))

    print("== legal overlapping jitter window freezes ==")
    freeze_events = [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}]
    s, b = call("POST", f"{base}/api/reviews",
                {"model": MODEL, "events": freeze_events})
    check("freeze 200", s == 200, f"status={s}")
    check("frozen", b.get("status") == "frozen", b.get("reason", ""))
    check("two steps evidence", len(b.get("steps", [])) == 2)
    fp_freeze = b.get("stored", {}).get("fingerprint", "")

    print("== cooling interval gap rejected at earliest event ==")
    gap_events = [
        {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}]
    gap_model = json.loads(json.dumps(MODEL))
    gap_model["audit_id"] = "SMOKE-GAP"
    s, b = call("POST", f"{base}/api/reviews",
                {"model": gap_model, "events": gap_events})
    check("gap 200", s == 200)
    check("gap rejected", b.get("status") == "rejected", b.get("reason", ""))
    check("earliest event 0", b.get("earliest_event_index") == 0)
    x = b["failure"]["clock_values"]["x"]
    check("witness 9/2",
          f"{x['numerator']}/{x['denominator']}" == "9/2", x.get("text", ""))
    names = {g["transition"] for g in
             b["failure"].get("blocking_guards", [])}
    check("blocking guards listed",
          names == {"t_cooldown", "t_open"}, str(names))

    print("== illegal model: closed guards overlap at x=5 ==")
    bad = json.loads(json.dumps(gap_model))
    bad["audit_id"] = "SMOKE-BAD"
    bad["transitions"][0]["guards"][0]["upper"] = 5
    s, b = call("POST", f"{base}/api/reviews",
                {"model": bad,
                 "events": [{"event": "cmd", "relative_lower": 5,
                             "relative_upper": 5}]})
    check("illegal 422", s == 422, f"status={s}")
    check("invalid_model", b.get("status") == "invalid_model")
    check("overlap code",
          b.get("error", {}).get("details", {}).get("code")
          == "overlapping_guards")

    print("== semantic-equivalent retransmission replays verdict ==")
    body1 = {"model": gap_model, "events": gap_events}
    s1, b1 = call("POST", f"{base}/api/reviews", body1)
    # reorder object keys: semantically equivalent, canonical fingerprint
    body2 = {"events": gap_events,
             "model": {k: gap_model[k] for k in reversed(list(gap_model))}}
    s2, b2 = call("POST", f"{base}/api/reviews", body2)
    check("replay 200", s2 == 200)
    check("replay flag",
          bool(b2.get("replay", {}).get(
              "semantically_equivalent_retransmission")))
    check("same verdict", b2.get("status") == b1.get("status")
          and b2.get("reason") == b1.get("reason"))

    print("== same id, different content -> conflict, evidence retained ==")
    conflict_events = [
        {"event": "cmd", "relative_lower": 0, "relative_upper": 4},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}]
    s, b = call("POST", f"{base}/api/reviews",
                {"model": gap_model, "events": conflict_events})
    check("conflict 409", s == 409, f"status={s}")
    check("conflict status", b.get("status") == "conflict")
    check("original evidence retained",
          b.get("conflict", {}).get("original_evidence", {}).get("status")
          == "rejected")
    s, b = call("GET", f"{base}/api/reviews/SMOKE-GAP")
    check("GET keeps original", s == 200 and b.get("status") == "rejected")

    print("== reopen: forking reset vs never-reset provenance ==")
    fork_model = {
        "audit_id": "SMOKE-LINE",
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
    fork_events = [
        {"event": "cmd", "relative_lower": 2, "relative_upper": 8},
        {"event": "ack", "relative_lower": 1, "relative_upper": 1}]
    s, b = call("POST", f"{base}/api/reviews",
                {"model": fork_model, "events": fork_events})
    check("fork rejected", s == 200 and b.get("status") == "rejected")

    s, sel = call("GET", f"{base}/api/reviews/SMOKE-LINE/lineage")
    check("lineage choices 200", s == 200 and sel.get("status") == "select")
    check("lineage clocks listed", sel.get("clocks") == ["x", "y"],
          str(sel.get("clocks")))
    check("lineage only processed prefix",
          [e["event_index"] for e in sel.get("processed_events", [])] == [0])

    s, ln = call("GET",
                 f"{base}/api/reviews/SMOKE-LINE/lineage?clock=y&event=0")
    groups = ln.get("summary", []) if s == 200 else []
    cats = {g.get("source_category") for g in groups}
    check("lineage 200", s == 200, str(s))
    check("two disjoint source groups", cats == {"initial", "reset"},
          str(cats))
    check("two regions partitioned",
          ln.get("regions_total") == 2
          and sum(g.get("feasible_region_count", 0) for g in groups) == 2)
    reset_g = next((g for g in groups
                    if g.get("source_category") == "reset"), {})
    check("reset names event+transition",
          reset_g.get("reset_event_index") == 0
          and reset_g.get("reset_transition") == "toA")
    check("rational witnesses present",
          bool(reset_g.get("witness_before_event", {}).get("y", {})
               .get("text"))
          and reset_g.get("witness_after_event", {}).get("y", {})
          .get("text") == "0")

    s, bad_clk = call("GET",
                      f"{base}/api/reviews/SMOKE-LINE/lineage?clock=zz&event=0")
    check("unknown clock refused",
          s == 422 and bad_clk.get("error", {}).get("details", {})
          .get("code") == "unknown_clock")
    s, bad_ev = call("GET",
                     f"{base}/api/reviews/SMOKE-LINE/lineage?clock=y&event=1")
    check("event past first failure refused",
          s == 422 and bad_ev.get("error", {}).get("details", {})
          .get("code") == "event_beyond_processed_prefix")
    s, still = call("GET", f"{base}/api/reviews/SMOKE-LINE")
    check("frozen conclusion unchanged after refused queries",
          s == 200 and still.get("status") == "rejected"
          and still.get("earliest_event_index") == 0)
    s, nf = call("GET", f"{base}/api/reviews/NO-SUCH/lineage?clock=x&event=0")
    check("lineage of missing record 404",
          s == 404 and nf.get("status") == "not_found")

    if failures:
        print(f"\nSMOKE FAILURES: {failures}")
        return 1
    print("\nSMOKE: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    sys.exit(main(base.rstrip("/")))
