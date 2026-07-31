"""6-bucket reason classifier for unbooked calls.

Mutually exclusive taxonomy (exactly one bucket per call so daily counts add up):
  A  not_a_true_lead        - existing cust / warranty / recall / membership / spam / vendor
  B  process_failure        - AI punt-to-callback, CSR never booked, dropped, rang out/vm  [OUR fault]
  C  capacity               - wanted sooner than offered / no slot in window
  D  customer_declined      - price objection / booked-then-canceled / chose competitor
  E  geo_scope              - out of area / out of scope (commercial, service we don't offer)

Uses OpenAI chat completions (gpt-4o) with strict JSON output. Falls back to the
ST call category for the not-a-true-lead case when no transcript exists.
"""
from __future__ import annotations
import json
import os
import requests

from .sources.base import Call

OPENAI_CHAT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o"

BUCKETS = {
    "A": "not_a_true_lead",
    "B": "process_failure",
    "C": "capacity",
    "D": "customer_declined",
    "E": "geo_scope",
}

SYSTEM = """You are an operations analyst for an HVAC/plumbing/electrical home-services company.
You classify why an inbound phone lead did NOT get booked, from the call transcript.

Assign EXACTLY ONE bucket (the single dominant reason):

A = not_a_true_lead: existing customer, warranty/recall/membership callback, wrong number,
    spam/robocall, vendor, recruiter, or any call that was never a real new-service opportunity.
B = process_failure (OUR fault, recoverable): the caller wanted service and we failed to book
    on the call for an internal reason — AI/booking-bot collected everything then punted to a
    "someone will call you back" / "on-call manager" queue WITHOUT creating an appointment;
    a human CSR took the info but never booked; the call dropped/disconnected mid-qualify;
    the call rang out / went to voicemail / was abandoned in queue.
C = capacity: a real lead we could not schedule because we had no slot when they needed it
    (wanted same-day/sooner than offered, no availability in their window).
D = customer_declined: real lead who chose not to book — price/diagnostic-fee objection,
    booked then canceled / cold feet, chose a competitor, still shopping.
E = geo_scope: out of our service area, or out of scope (commercial we don't do, a service
    we don't offer).

Rules:
- Pick the SINGLE dominant reason. If a call could be B and D, ask: did WE fail to book
  (B) or did the CUSTOMER decline a real offer (D)? The callback-punt pattern is ALWAYS B.
- Base it on what actually happened, not the disposition tag.
- Respond ONLY with JSON: {"bucket":"A|B|C|D|E","detail":"<=15 word reason","confidence":0.0-1.0}"""


def _fallback_from_category(call: Call) -> dict:
    cat = (call.campaign_category or "").lower()
    if "existing" in cat or "warranty" in cat or "recall" in cat or "membership" in cat:
        return {"bucket": "A", "detail": f"ST category: {call.campaign_category}", "confidence": 0.6}
    return {"bucket": "B", "detail": "no transcript; uncoded new-lead call (review)", "confidence": 0.3}


def classify(call: Call) -> dict:
    """Returns {'bucket','bucket_name','detail','confidence'}."""
    # No transcript (recording missing / too short) -> deterministic fallback.
    if not call.transcript or call.transcript.startswith("[transcription failed"):
        res = _fallback_from_category(call)
        res["bucket_name"] = BUCKETS[res["bucket"]]
        return res

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")

    ctx = (f"Campaign: {call.campaign_name}\nST category: {call.campaign_category}\n"
           f"Duration: {call.duration_sec}s\nAgent: {call.agent_name}\n"
           f"Transcript:\n{call.transcript[:6000]}")

    body = {
        "model": MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": ctx},
        ],
    }
    r = requests.post(OPENAI_CHAT, headers={"Authorization": f"Bearer {key}"},
                      json=body, timeout=90)
    if r.status_code != 200:
        res = _fallback_from_category(call)
        res["detail"] += f" [classifier HTTP {r.status_code}]"
        res["bucket_name"] = BUCKETS[res["bucket"]]
        return res
    try:
        parsed = json.loads(r.json()["choices"][0]["message"]["content"])
        b = parsed.get("bucket", "B").upper()[:1]
        if b not in BUCKETS:
            b = "B"
        return {"bucket": b, "bucket_name": BUCKETS[b],
                "detail": parsed.get("detail", "")[:120],
                "confidence": float(parsed.get("confidence", 0.5))}
    except Exception as e:
        res = _fallback_from_category(call)
        res["detail"] += f" [parse error: {e}]"
        res["bucket_name"] = BUCKETS[res["bucket"]]
        return res
