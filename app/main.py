"""HTTP API for the interlock review service."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .engine import (ModelError, OriginQueryError, origin_query,
                     parse_capture, parse_model, review)
from .storage import Store

STORE_PATH = os.environ.get("EVIDENCE_STORE", "/data/evidence.json")
HEALTH_ACK = os.environ.get("HEALTH_ACK", "ok")

app = FastAPI(title="Propellant Isolation Interlock Reviewer", version="1.0.0")
store = Store(STORE_PATH)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _invalid_model(exc: ModelError, audit_id: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "invalid_model",
        "earliest_event_index": None,
        "reason": str(exc),
        "error": {"code": exc.details.get("code", "malformed_model"),
                  "message": str(exc),
                  "details": exc.details},
    }
    if audit_id:
        body["audit_id"] = audit_id
    return body


def _run(payload: Any) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, dict):
        return {"status": "invalid_model", "earliest_event_index": None,
                "reason": "request body must be an object",
                "error": {"code": "bad_request", "message": "object required",
                          "details": {}}}, 400

    model_payload = payload.get("model")
    if model_payload is None:
        model_payload = {k: v for k, v in payload.items() if k != "events"}
    events_payload = payload.get("events")
    audit_id = model_payload.get("audit_id") if isinstance(
        model_payload, dict) else None

    try:
        model = parse_model(model_payload)
    except ModelError as exc:
        body = _invalid_model(exc, audit_id if isinstance(audit_id, str)
                              else None)
        if isinstance(audit_id, str) and audit_id.strip():
            body = store.submit(audit_id.strip(), payload, body,
                                model_valid=False)
            if body.get("status") == "conflict":
                return body, 409
        return body, 422

    try:
        events = parse_capture(events_payload)
    except ModelError as exc:
        body = _invalid_model(exc, model.audit_id)
        body = store.submit(model.audit_id, payload, body, model_valid=False)
        if body.get("status") == "conflict":
            return body, 409
        return body, 422

    result = review(model, events)
    stored = store.submit(model.audit_id, payload, result, model_valid=True)
    code = 409 if stored.get("status") == "conflict" else 200
    return stored, code


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": HEALTH_ACK}


@app.post("/api/reviews")
async def create_review(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "invalid_model", "earliest_event_index": None,
             "reason": "request body must be valid JSON",
             "error": {"code": "bad_json", "message": "invalid JSON",
                       "details": {}}}, status_code=400)
    body, code = _run(payload)
    return JSONResponse(body, status_code=code)


@app.get("/api/reviews/{audit_id}")
def get_review(audit_id: str) -> JSONResponse:
    rec = store.lookup(audit_id)
    if rec is None:
        return JSONResponse({"status": "not_found",
                             "audit_id": audit_id}, status_code=404)
    body = dict(rec["result"])
    body["fingerprint"] = rec["fingerprint"]
    body["received_at"] = rec["received_at"]
    return JSONResponse(body)


def _origin_refusal(audit_id: str, code: str, message: str,
                    status_code: int = 422) -> JSONResponse:
    return JSONResponse(
        {"status": "origin_query_refused", "audit_id": audit_id,
         "reason": message,
         "error": {"code": code, "message": message, "details": {}}},
        status_code=status_code)


@app.get("/api/reviews/{audit_id}/origins")
def get_clock_origins(audit_id: str, clock: str | None = None,
                      event: str | None = None) -> JSONResponse:
    """Read-only reopen: per-source non-overlapping region summary.

    The stored frozen/rejected conclusion is never modified; a missing or
    non-traceable history record, an unknown clock, or an event beyond the
    first failure of a rejection is refused explicitly.
    """
    rec = store.lookup(audit_id)
    if rec is None:
        return JSONResponse({"status": "not_found",
                             "audit_id": audit_id}, status_code=404)

    if not clock or event is None or event == "":
        return _origin_refusal(
            audit_id, "missing_selection",
            "both 'clock' and 'event' (processed event index) must be "
            "selected", status_code=400)

    try:
        event_index = int(event)
    except (TypeError, ValueError):
        event_index = -1

    verdict = rec.get("result") or {}
    payload = rec.get("payload")
    if not rec.get("model_valid") or not isinstance(payload, dict):
        # Historical record without the original submission/traceable region
        # evidence: refuse rather than guess; the old conclusion stays intact.
        return _origin_refusal(
            audit_id, "no_traceable_regions",
            "this stored record has no traceable region evidence to replay; "
            "the original conclusion is retained")

    model_payload = payload.get("model")
    if model_payload is None:
        model_payload = {k: v for k, v in payload.items() if k != "events"}
    try:
        model = parse_model(model_payload)
        events = parse_capture(payload.get("events"))
    except ModelError as exc:
        return _origin_refusal(audit_id, "no_traceable_regions", str(exc))

    try:
        body = origin_query(model, events, verdict, clock, event_index)
    except OriginQueryError as exc:
        return _origin_refusal(audit_id, exc.code, str(exc))
    body["fingerprint"] = rec["fingerprint"]
    body["received_at"] = rec["received_at"]
    return JSONResponse(body)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(_HERE, "static", "index.html"),
              encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")),
          name="static")
