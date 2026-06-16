#!/usr/bin/env python3
"""Short manager-facing opportunity snapshot.

This is intentionally separate from customer_opportunity_report_card.py. The full
report card remains the detailed artifact; this module produces a concise service
manager triage view: grade, recommended staffing, one sentence, and a few bullets.

Design principle: judge signal convergence like a sales manager. Deterministic
facts are guardrails; the LLM, when available, synthesizes the whole picture
without relying on brittle hard-coded trigger rules.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customer_opportunity_report_card import _derive  # noqa: E402
from report_card_facts import load_default_env, serialize_context_for_llm, strip_html  # noqa: E402
import report_card_llm  # noqa: E402

GRADES = ("A+", "A", "B", "C", "D")
STAFFING = (
    "Strong sales technician recommended",
    "Good opportunity, assign capable technician",
    "Standard dispatch",
    "Low opportunity signal",
)

SCHEMA_DOC = """Return STRICT JSON only:
{
  "grade": "A+|A|B|C|D",
  "staffing": "Strong sales technician recommended|Good opportunity, assign capable technician|Standard dispatch|Low opportunity signal",
  "headline": "one concise service-manager sentence, no fluff",
  "signals": ["2-4 short bullets, each tied to evidence from the call/customer/home/history"],
  "manager_note": "optional one short sentence with why this deserves attention or why it does not"
}
"""

SYS_MSG = (
    "You are writing a very short Service Manager Opportunity Snapshot for LEX. "
    "The purpose is dispatch triage: flag calls where there is evidence money may be made "
    "and the call deserves a strong technician with sales skill or manager review.\n\n"
    "Think like a sales manager, not a rigid rules engine. Do NOT use one hard trigger like "
    "'system not working' or 'past estimate mentioned' as the decision. Evaluate convergence "
    "across need, equipment age/condition, home age, customer relationship, membership, buying "
    "history, prior estimate behavior, call intent, appointment type, trade context, photos, and "
    "operational clues. Many different combinations can create opportunity; one signal alone may "
    "not.\n\n"
    "Keep it high level and concise. No long report-card sections. No repair checklist. No generic "
    "'check this' fluff. No coaching scripts. Do not recommend specific add-ons, stale estimate "
    "categories, or product pitches like IAQ unless current-call evidence clearly makes that the "
    "manager-level reason to flag the call. This snapshot answers only: should a service manager "
    "pay attention and consider a stronger sales-capable technician?\n\n"
    "Ground truth rules: use only facts in the context. Do not invent dollar amounts, equipment, years, "
    "or customer intent. Open estimates mean quoted/not closed, not proven interest. Historical photos "
    "are weak unless the broader context supports attention. If evidence is mixed, say mixed.\n\n"
    + SCHEMA_DOC
)


def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", strip_html(s or "")).strip()


def _status(e: dict) -> str:
    return ((e.get("status") or {}).get("name") or "").strip().lower()


def _money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except Exception:
        return "$0"


def _prompt_context(d: dict) -> dict:
    dossier = d["dossier"]
    job = dossier.get("job") or {}
    customer = dossier.get("customer") or {}
    facts = d.get("facts") or {}
    intel = d.get("intel") or {}
    estimates = dossier.get("estimates") or []
    sold = [e for e in estimates if _status(e) == "sold"]
    open_est = [e for e in estimates if _status(e) == "open"]
    return {
        "job": {
            "job_number": job.get("jobNumber") or job.get("id"),
            "job_type": d.get("meta", {}).get("job_type") or "",
            "business_unit": d.get("meta", {}).get("business_unit") or "",
            "reason_for_visit": d.get("call_issue") or facts.get("reason") or _norm_spaces(job.get("summary"))[:500],
            "raw_booking_summary_excerpt": _norm_spaces(job.get("summary"))[:1200],
            "appointment": d.get("meta", {}).get("appointment") or facts.get("appt") or "",
        },
        "customer": {
            "name": d.get("meta", {}).get("customer") or customer.get("name") or "",
            "relationship": d.get("membership_line") or "",
            "customer_since": d.get("cust_since") or "",
            "known_sold_revenue": d.get("revenue_display") or "",
            "prior_sold_count": len(sold),
            "largest_sold": _money(max([float(e.get("subtotal") or 0) for e in sold] or [0])),
            "open_estimate_count": len(open_est),
            "open_estimate_total": _money(sum(float(e.get("subtotal") or 0) for e in open_est)),
        },
        "home_and_equipment": {
            "address": facts.get("address") or d.get("meta", {}).get("address") or "",
            "home_built_year": facts.get("home_built_year") or facts.get("home_age") or "",
            "equipment_age_line": d.get("eq_age_line") or "",
            "mandatory_flags": [f.get("text") for f in d.get("flags") or [] if f.get("text")],
            "record_notes": d.get("stale_lines") or [],
        },
        "history_signals": {
            "estimate_intel_summary": {
                "price_ceiling": intel.get("price_ceiling"),
                "biggest_sold": intel.get("biggest_sold"),
                "biggest_open_stale": intel.get("biggest_open_stale"),
                "always_buys": intel.get("always_buys"),
                "prior_single_buys": intel.get("prior_single_buys"),
                "open_stale_high_value": intel.get("open_stale_high_value"),
                "category_breakdown": intel.get("category_breakdown"),
            },
            "cross_trade_signals": d.get("cross_signals") or [],
            "past_jobs_excerpt": [
                {
                    "jobNumber": pj.get("jobNumber"),
                    "completedOn": pj.get("completedOn"),
                    "summary": _norm_spaces(pj.get("summary") or pj.get("summaryOfWork"))[:250],
                }
                for pj in (dossier.get("past_jobs") or [])[:6]
            ],
        },
    }


def _call_openrouter(context: dict) -> dict | None:
    if not report_card_llm.llm_enabled():
        return None
    model = os.environ.get("LEX_OPPORTUNITY_SNAPSHOT_MODEL") or os.environ.get("LEX_REPORT_CARD_MODEL") or "openai/gpt-4o"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_MSG},
            {"role": "user", "content": serialize_context_for_llm(context, max_chars=14000)},
        ],
        "temperature": 0.15,
        "response_format": {"type": "json_object"},
    }
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
        raw = body["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        print(f"opportunity_snapshot: LLM unavailable ({exc})", file=sys.stderr)
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _pick_grade(score: int) -> str:
    if score >= 90:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 55:
        return "B"
    if score >= 35:
        return "C"
    return "D"


def _fallback_snapshot(d: dict) -> dict:
    """Soft signal-cluster fallback. This is deliberately not a single-trigger rules engine.

    It collects independent evidence categories, then grades based on convergence.
    """
    summary = _norm_spaces((d["dossier"].get("job") or {}).get("summary"))
    lower = summary.lower()
    sold_count = len(d.get("sold") or [])
    open_total = sum(float(e.get("subtotal") or 0) for e in (d.get("open_rows") or []))
    flags = d.get("flags") or []
    score = 0
    signals: list[str] = []

    def add(points: int, text: str):
        nonlocal score
        if text and text not in signals:
            score += points
            signals.append(text)

    if d.get("active_recurring_m"):
        add(14, f"Active member with established relationship ({d.get('membership_line')}).")
    elif d.get("membership_line") and "No active" not in d.get("membership_line"):
        add(6, d.get("membership_line"))

    if sold_count:
        add(14, f"Prior buying history: {sold_count} sold estimate(s), largest known sold { _money(max(float(e.get('subtotal') or 0) for e in d.get('sold') or [])) }.")

    call_issue = d.get("call_issue") or ""
    if call_issue:
        add(12, f"Current call intent: {call_issue}.")

    # Intent / trust / operational clues are weighted as a cluster, not a rule.
    intent_terms = []
    if any(t in lower for t in ["wants", "requested", "specific", "questions about", "estimate", "quote", "pricing"]):
        intent_terms.append("customer is already asking questions or requesting continuity")
    if any(t in lower for t in ["team visit", "doing a team visit"]):
        intent_terms.append("team-visit context")
    if any(t in lower for t in ["not cooling", "not heating", "leak", "no cool", "no heat", "shut", "failed", "warm air"]):
        intent_terms.append("active comfort or failure pressure")
    if len(intent_terms) >= 2:
        add(16, "Multiple intent signals align: " + ", ".join(intent_terms[:3]) + ".")
    elif intent_terms:
        add(6, "Intent signal: " + intent_terms[0] + ".")

    if flags:
        labels = "; ".join(
            (f.get("text") or "").replace("⚠ FLAG: ", "").replace("—", "-").replace("–", "-")
            for f in flags[:2]
        )
        add(18, f"Replacement-age asset signal: {labels}")
    elif d.get("age_signal"):
        add(10, d["age_signal"].capitalize() + ".")

    if open_total >= 10000:
        add(8, f"Prior open quote history totals about {_money(open_total)}, useful context if today uncovers the same need.")

    home_year = (d.get("facts") or {}).get("home_built_year") or (d.get("facts") or {}).get("home_age")
    try:
        home_age = 2026 - int(str(home_year))
    except Exception:
        home_age = None
    if home_age and home_age >= 10:
        add(5, f"Home age/context supports a broader evaluation: built {home_year} ({home_age} yrs).")

    score = min(100, score)
    grade = _pick_grade(score)
    if grade in {"A+", "A"}:
        staffing = "Strong sales technician recommended"
    elif grade == "B":
        staffing = "Good opportunity, assign capable technician"
    elif grade == "C":
        staffing = "Standard dispatch"
    else:
        staffing = "Low opportunity signal"
    headline = "High-opportunity call worth manager attention." if grade in {"A+", "A"} else (
        "Some opportunity signals are present, but the call needs normal field validation." if grade == "B" else
        "Limited evidence of a premium opportunity from pulled data."
    )
    return {
        "grade": grade,
        "staffing": staffing,
        "headline": headline,
        "signals": signals[:4] or ["No strong opportunity cluster found in pulled ServiceTitan data."],
        "manager_note": "Grade reflects convergence of relationship, need, asset age, history, and call-intent signals, not a single hard trigger.",
        "source": "deterministic",
    }


def normalize_snapshot(raw: dict | None, fallback: dict) -> dict:
    if not isinstance(raw, dict):
        return fallback
    grade = str(raw.get("grade") or "").strip().upper()
    if grade not in GRADES:
        grade = fallback["grade"]
    staffing = str(raw.get("staffing") or "").strip()
    if staffing not in STAFFING:
        staffing = fallback["staffing"]
    signals = raw.get("signals") or []
    if isinstance(signals, str):
        signals = [signals]
    signals = [str(s).strip() for s in signals if str(s or "").strip()][:4]
    if len(signals) < 2:
        signals = fallback["signals"]
    return {
        "grade": grade,
        "staffing": staffing,
        "headline": str(raw.get("headline") or fallback["headline"]).strip(),
        "signals": signals,
        "manager_note": str(raw.get("manager_note") or fallback.get("manager_note") or "").strip(),
        "source": "llm",
    }


def _sanitize_snapshot(snapshot: dict, d: dict) -> dict:
    """Keep the short snapshot from drifting into stale product/category pitching.

    This does not decide opportunity via a hard rule. It simply prevents the compact
    manager triage note from turning old estimate categories into recommendations
    unless the current job context itself supports that category.
    """
    current_blob = " ".join([
        str((d.get("job") or {}).get("summary") or ""),
        str(d.get("call_issue") or ""),
        " ".join(d.get("photo_lines") or []),
    ]).lower()
    gated_terms = ["iaq", "uv", "duct clean", "water treatment", "surge"]

    def stale_category_pitch(s: str) -> bool:
        low = s.lower()
        return any(term in low and term not in current_blob for term in gated_terms)

    def clean_salesy(s: str) -> str:
        return re.sub(r"\bupsell\b", "sales conversation", str(s), flags=re.I)

    cleaned = dict(snapshot)
    cleaned["headline"] = clean_salesy(re.sub(r"\s*(?:and|/)\s*IAQ (?:upsell|opportunities?)", "", str(cleaned.get("headline") or ""), flags=re.I))
    cleaned["manager_note"] = clean_salesy(re.sub(r"\s*(?:and|/)\s*IAQ (?:upsell|opportunities?)", "", str(cleaned.get("manager_note") or ""), flags=re.I))
    signals = [clean_salesy(s) for s in (cleaned.get("signals") or []) if not stale_category_pitch(str(s))]
    if len(signals) < 2:
        signals = _fallback_snapshot(d).get("signals") or signals
    cleaned["signals"] = signals[:4]
    return cleaned


def render_markdown(snapshot: dict, d: dict) -> str:
    meta = d.get("meta") or {}
    job = d.get("dossier", {}).get("job") or {}
    clean = lambda s: str(s or "").replace("—", "-").replace("–", "-").strip()
    lines = [
        "# SERVICE MANAGER OPPORTUNITY SNAPSHOT",
        "",
        f"**Grade:** {clean(snapshot['grade'])}",
        f"**Staffing:** {clean(snapshot['staffing'])}",
        f"**Customer:** {clean(meta.get('customer') or '')}",
        f"**Job #:** {clean(job.get('jobNumber') or job.get('id'))}",
        f"**Call Type:** {clean(meta.get('job_type') or '')}",
        "",
        f"**Summary:** {clean(snapshot['headline'])}",
        "",
        "**Why it is flagged:**",
    ]
    lines.extend(f"- {clean(s)}" for s in snapshot.get("signals") or [])
    if snapshot.get("manager_note"):
        lines += ["", f"**Manager note:** {clean(snapshot['manager_note'])}"]
    return "\n".join(lines).strip() + "\n"


def build_snapshot(bundle: dict, *, use_llm: bool | None = None) -> tuple[str, dict]:
    d = _derive(bundle, None, None)
    fallback = _fallback_snapshot(d)
    enabled = report_card_llm.llm_enabled() if use_llm is None else use_llm
    raw = None
    if enabled:
        raw = _call_openrouter(_prompt_context(d))
    snapshot = normalize_snapshot(raw, fallback) if enabled else fallback
    snapshot = _sanitize_snapshot(snapshot, d)
    md = render_markdown(snapshot, d)
    return md, snapshot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", help="Cached job dossier JSON")
    ap.add_argument("--no-llm", action="store_true", help="Use deterministic signal-cluster fallback only")
    ap.add_argument("--out", help="Output markdown path")
    args = ap.parse_args()
    load_default_env()
    bundle = json.loads(Path(args.json_path).read_text())
    md, snapshot = build_snapshot(bundle, use_llm=False if args.no_llm else None)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        (out.with_suffix(".json")).write_text(json.dumps(snapshot, indent=2))
        print(out)
    else:
        print(md)


if __name__ == "__main__":
    main()
