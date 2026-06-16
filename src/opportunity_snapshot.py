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
from opportunity_flags import home_age_score  # noqa: E402
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
    "or customer intent. Open estimates mean quoted/not closed, not proven interest. Treat large open "
    "estimate totals as very weak context and never customer interest. LEX leaves IAQ/options for many "
    "customers, so do not use old options as the reason to flag a call unless current facts support the same need. "
    "Demand calls with a known pain point should outrank comparable maintenance calls. Historical photos should "
    "only be mentioned when they show a potentially valuable visible finding. Recall, warranty, QC, callback, and "
    "recent-install issue calls are not opportunity calls and should be excluded before reaching this prompt. If evidence is mixed, say mixed.\n\n"
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


EXCLUDED_TYPE_RE = re.compile(
    r"\b(warranty|recall|callback|call\s*back|qc|quality\s*control|quality\s*check|redo|rework|return\s+visit)\b",
    re.I,
)
EXCLUDED_SUMMARY_RE = re.compile(
    r"\b(warranty\s+(?:call|issue|repair|return)|recall|callback|call\s*back|qc|quality\s*control|quality\s*check|redo|rework|return\s+visit|recent\s+install|just\s+installed|install\s+issue|installed\s+(?:earlier\s+)?this\s+year|installed\s+(?:at\s+the\s+)?beginning\s+of\s+20\d{2}|not\s+cooling\s+after\s+install|not\s+heating\s+after\s+install|install\s+warranty)\b",
    re.I,
)
MAINTENANCE_RE = re.compile(r"\b(maintenance|maint|tune\s*up|tune-up|seasonal|inspection|check\s*up)\b", re.I)
DEMAND_RE = re.compile(
    r"\b(demand|not\s+cool(?:ing)?|no\s+cool|not\s+heat(?:ing)?|no\s+heat|leak|leaking|noise|noisy|strange\s+sound|failed|failure|shut\s*(?:unit|system)?\s*off|warm\s+air|hot\s+air|freez(?:e|ing|en)|overflow|clog|backup|unsafe|burning\s+smell|sparking|tripping|breaker|outage)\b",
    re.I,
)
VALUABLE_PHOTO_RE = re.compile(
    r"\b(insulation|low\s+insulation|thin\s+insulation|kink(?:ed)?\s+duct(?:work)?|sag(?:ging)?\s+duct(?:work)?|duct(?:work)?\s+(?:leak|damage|disconnect|sealing|insulation|support|restricts)|duct\s+tape|deteriorat(?:e|ing)|dust\s+accumulation|dirty\s+(?:blower|return|coil|filter|duct)|biological|growth|rust|corrosion|water\s+(?:stain|damage)|murky\s+water|drain\s+(?:pan|pipe|residue)|overflow|unsafe\s+access|panel|breaker|double\s*tap|open\s+splice|water\s+heater|plumbing\s+leak)\b",
    re.I,
)


def is_excluded_opportunity_call(job_type: str = "", summary: str = "", business_unit: str = "") -> bool:
    """True for warranty/recall/QC/callback/recent-install work that should not be in opportunity batches.

    Generic warranty text in notes can refer to a past install/labor warranty, so
    the summary side requires stronger service-recovery phrasing than a bare word.
    """
    type_blob = " ".join([job_type or "", business_unit or ""])
    if EXCLUDED_TYPE_RE.search(type_blob):
        return True
    return bool(EXCLUDED_SUMMARY_RE.search(summary or ""))


def call_intent_type(job_type: str = "", summary: str = "") -> str:
    blob = " ".join([job_type or "", summary or ""])
    if DEMAND_RE.search(blob):
        return "demand"
    if MAINTENANCE_RE.search(blob):
        return "maintenance"
    return "standard"


def valuable_photo_lines(vision: dict | None, *, max_items: int = 3) -> list[str]:
    """Return only photo findings that are specific, visible, and potentially valuable."""
    if not isinstance(vision, dict):
        return []
    out: list[str] = []
    for f in vision.get("findings") or []:
        if not isinstance(f, dict):
            continue
        text = _norm_spaces(f.get("finding") or f.get("verify") or f.get("description") or "")
        if not text or not VALUABLE_PHOTO_RE.search(text):
            continue
        conf = str(f.get("confidence") or "").lower()
        if conf and conf not in {"high", "medium", "med"}:
            continue
        idx = f.get("indexes") or f.get("images") or f.get("image_indexes") or []
        if isinstance(idx, (str, int)):
            idx = [idx]
        idx_text = f"image {', '.join(str(x) for x in idx)}" if idx else "photo evidence"
        line = f"Visible opportunity: {text} identified in {idx_text}."
        if line not in out:
            out.append(line)
        if len(out) >= max_items:
            break
    return out


def red_flag_texts(d: dict, *, max_items: int = 3) -> list[str]:
    texts = []
    for f in d.get("flags") or []:
        if f.get("kind") != "equipment_age":
            continue
        txt = str(f.get("text") or "").replace("⚠ FLAG: ", "").replace("⚠ FLAG (verify): ", "VERIFY: ").replace("—", "-").replace("–", "-").strip()
        if txt and txt not in texts:
            texts.append(txt)
    return texts[:max_items]


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
        "photo_vision": {
            "valuable_findings": valuable_photo_lines(d.get("vision")),
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
    intent_type = call_intent_type(d.get("meta", {}).get("job_type") or "", summary)
    if call_issue:
        add(22 if intent_type == "demand" else 8, f"Current call intent: {call_issue}.")
    elif intent_type == "demand":
        add(18, "Demand call with a known pain point in the booking context.")
    elif intent_type == "maintenance":
        add(2, "Maintenance visit; opportunity depends on supporting age, home, photo, and relationship signals.")

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

    eq_flags = [f for f in flags if f.get("kind") == "equipment_age"]
    if eq_flags:
        labels = "; ".join(red_flag_texts(d, max_items=2))
        add(22, f"Replacement-age asset signal: {labels}")
    elif d.get("age_signal"):
        add(10, d["age_signal"].capitalize() + ".")

    if open_total >= 10000:
        add(2, f"Prior open quote context totals about {_money(open_total)}, useful only if today's findings support the same need.")

    photo_hits = valuable_photo_lines(d.get("vision"))
    if photo_hits:
        add(12, photo_hits[0])

    home_year = (d.get("facts") or {}).get("home_built_year") or (d.get("facts") or {}).get("home_age")
    try:
        home_age = 2026 - int(str(home_year))
    except Exception:
        home_age = None
    home_flags = [f for f in flags if f.get("kind") == "home_age"]
    hp = home_age_score(home_flags)
    if home_age and home_age >= 10:
        add(hp or 3, f"Home age/context supports a broader evaluation: built {home_year} ({home_age} yrs).")

    def _signal_rank(s: str) -> int:
        low = s.lower()
        if "current call intent" in low or "demand call" in low:
            return 0
        if "replacement-age" in low:
            return 1
        if "visible opportunity" in low or "image" in low or "photo" in low:
            return 2
        if "active member" in low:
            return 3
        if "prior buying" in low:
            return 4
        if "intent signal" in low or "multiple intent" in low:
            return 5
        if "home age" in low:
            return 6
        if "open quote" in low:
            return 9
        return 7
    signals = sorted(signals, key=_signal_rank)

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
        s = re.sub(r"\bupsell\b", "sales conversation", str(s), flags=re.I)
        s = re.sub(r"\bHigh open estimate total(?: of)? ([^,.]+)(?: with [^.]+)?(?:,?\s*(?:suggesting|indicating)[^.]*)?", r"Prior open quote context totals \1, useful only if today's findings support it", s, flags=re.I)
        s = re.sub(r"\bMultiple open estimates? totaling ([^,.]+)\s*(?:suggest|indicate)[^.]*", r"Prior open quote context totals \1, useful only if today's findings support it", s, flags=re.I)
        s = re.sub(r"\bopen estimates? (?:totaling|worth) ([^,.]+),?\s*(?:indicating|suggesting) (?:potential )?(?:interest|ongoing interest)[^.]*(\.)?", r"prior open quote context totals \1, useful only if today's findings support it.", s, flags=re.I)
        s = re.sub(r"\bMultiple open estimates across trades in additional services\b", "Prior open quote context exists across trades", s, flags=re.I)
        s = re.sub(r"\bleverag(?:e|ing) the high open estimate total and ", "use ", s, flags=re.I)
        s = re.sub(r",?\s*(?:indicating|suggesting) (?:potential )?(?:interest|ongoing interest)\b", "", s, flags=re.I)
        s = re.sub(r"\bstrong potential for additional sales\b", "possible opportunity if field findings support it", s, flags=re.I)
        return re.sub(r"\s+", " ", s).strip()

    cleaned = dict(snapshot)
    cleaned["headline"] = clean_salesy(re.sub(r"\s*(?:and|/)\s*IAQ (?:upsell|opportunities?)", "", str(cleaned.get("headline") or ""), flags=re.I))
    cleaned["manager_note"] = clean_salesy(re.sub(r"\s*(?:and|/)\s*IAQ (?:upsell|opportunities?)", "", str(cleaned.get("manager_note") or ""), flags=re.I))
    signals = [clean_salesy(s) for s in (cleaned.get("signals") or []) if not stale_category_pitch(str(s))]
    if len(signals) < 2:
        signals = _fallback_snapshot(d).get("signals") or signals

    # Ensure high-value photo and equipment-flag facts survive LLM brevity, while
    # weak open-estimate context stays last and disposable.
    for line in valuable_photo_lines(d.get("vision")):
        if not any("image" in s.lower() or "photo" in s.lower() for s in signals):
            signals.append(line)
            break
    cleaned["signals"] = signals[:4]
    cleaned["red_flags"] = red_flag_texts(d)
    cleaned["photo_findings"] = valuable_photo_lines(d.get("vision"))
    cleaned["call_intent"] = call_intent_type(d.get("meta", {}).get("job_type") or "", (d.get("job") or {}).get("summary") or "")
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


def build_snapshot(bundle: dict, *, use_llm: bool | None = None, vision: dict | None = None) -> tuple[str, dict]:
    d = _derive(bundle, vision, None)
    if is_excluded_opportunity_call(d.get("meta", {}).get("job_type") or "", (d.get("job") or {}).get("summary") or "", d.get("meta", {}).get("business_unit") or ""):
        snapshot = {
            "grade": "D",
            "staffing": "Low opportunity signal",
            "headline": "Excluded from opportunity triage: warranty, recall, QC, callback, or recent-install issue context.",
            "signals": ["This work belongs in service recovery / operational follow-up, not sales-opportunity dispatch."],
            "manager_note": "Excluded by call-type policy.",
            "source": "excluded",
            "red_flags": red_flag_texts(d),
            "photo_findings": valuable_photo_lines(d.get("vision")),
            "call_intent": call_intent_type(d.get("meta", {}).get("job_type") or "", (d.get("job") or {}).get("summary") or ""),
        }
        return render_markdown(snapshot, d), snapshot
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
    ap.add_argument("--vision-json", help="Optional photo-vision JSON for this job")
    ap.add_argument("--no-llm", action="store_true", help="Use deterministic signal-cluster fallback only")
    ap.add_argument("--out", help="Output markdown path")
    args = ap.parse_args()
    load_default_env()
    bundle = json.loads(Path(args.json_path).read_text())
    vision = json.loads(Path(args.vision_json).read_text()) if args.vision_json else None
    md, snapshot = build_snapshot(bundle, use_llm=False if args.no_llm else None, vision=vision)
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
