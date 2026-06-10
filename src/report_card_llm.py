#!/usr/bin/env python3
"""LLM synthesis layer for the Customer Opportunity Report Card.

Division of labor:
  - Python computes every fact (ages, dollars, estimate states, flags) and renders
    the locked markdown/HTML layout.
  - The LLM fills the *narrative* section slots (buying behavior, opportunity
    selection and framing, coaching notes, score rationale) as structured JSON.
  - A deterministic validator audits the JSON against the facts context: every
    dollar figure, year, and long ID in the output must exist in the context, and
    equipment may only be referenced per the on-file/likely-but-verify whitelist.
    One correction retry, then offending lines are scrubbed; if the result is
    structurally unusable the caller falls back to the deterministic renderer.

Nothing in this module is required at import time for the deterministic path —
no API key means generate_sections() simply returns None.
"""
from __future__ import annotations

import json
import os
import re
import sys
from urllib import request

from report_card_facts import (
    HALLUCINATION_WATCHLIST,
    _equipment_whitelist,
    build_llm_context,
    equipment_implied_by_history,
    serialize_context_for_llm,
)

PRIORITIES = ("HIGH", "MEDIUM-HIGH", "MEDIUM", "LOW-MEDIUM", "LOW")
RELATIONSHIPS = ("A+", "A", "B", "C")
LIKELIHOODS = ("High", "Medium-High", "Medium", "Low", "Unknown-Low")
RISKS = ("Low", "Moderate", "High")

SCHEMA_DOC = """Return STRICT JSON only (no markdown fences, no commentary):
{
  "call_reason": "one short plain sentence: what the customer actually called about, from reason_for_visit",
  "customer_profile_extra": ["0-2 extra profile bullets grounded in history (optional)"],
  "buying_behavior": ["2-5 bullets describing how THIS customer buys, each grounded in the estimate intel"],
  "visual_inspection": {
    "positives": ["0-3 bullets, only if photo findings genuinely support them"],
    "verify_first": ["1-6 verify-on-arrival bullets built from the photo findings and documentation gaps"]
  },
  "primary_opportunities": [
    {"title": "short opportunity title", "priority": "HIGH|MEDIUM-HIGH|MEDIUM|LOW-MEDIUM|LOW",
     "bullets": ["3-4 specific, evidence-tied coaching bullets"]}
  ],
  "coaching_notes": ["2-4 bullets for the AI/dispatch layer: what to weight, what not to overclaim"],
  "overall": {
    "relationship": "A+|A|B|C",
    "likelihood": "High|Medium-High|Medium|Low|Unknown-Low",
    "risk": "Low|Moderate|High",
    "secondary_opportunity": "one short line naming the #2 opportunity",
    "rationale": "one sentence tying the grades to the evidence"
  }
}
primary_opportunities must contain 1-3 entries ordered best-first; the first entry is the headline."""

SYS_MSG = (
    "You write the narrative slots of a Customer Opportunity Report Card for a LEX Air field "
    "technician and dispatch manager. The card is sales-enablement: surface the best revenue "
    "opportunities for this specific visit without hallucinating, service-first in tone.\n\n"
    "GROUND TRUTH RULES (non-negotiable):\n"
    "1. The provided context is the ONLY source of facts. Every dollar figure, year, age, count, "
    "estimate/invoice/job ID you write must be copied exactly from the context. Never compute, "
    "estimate, or round new numbers.\n"
    "2. Equipment: only items in GROUND_TRUTH.installed_equipment_on_file are installed. Items in "
    "equipment_likely_present_not_on_file may be mentioned ONLY with 'not on file, verify on arrival'. "
    "Never invent equipment.\n"
    "3. Estimate intent states: OPEN/EXPIRED = we quoted it and never closed it (warm re-open, NOT "
    "customer interest). DISMISSED = customer said no (soft revisit, change the ask). Only SOLD "
    "supports invested/values/bought language.\n"
    "4. mandatory_flags are already printed on the card by the system. Do not restate them verbatim; "
    "build opportunity framing around them. Never contradict or soften them.\n"
    "5. photo findings marked historical are verification prompts, not current conditions. "
    "Phrase them as 'verify on arrival'. Low/medium confidence findings must stay verify-first.\n"
    "6. If booking notes show the customer called for specific identified repairs, the #1 opportunity "
    "is a pricing/confirmation visit for those repairs — not a generic age-based discovery call.\n"
    "7. De-prioritize stale open estimates when stronger current evidence exists; say why.\n"
    "8. If estimate totals are all zero, buying behavior is unproven: say so plainly and keep "
    "likelihood at Medium or below. Never say 'no estimate history' when totals are non-zero.\n"
    "9. If GROUND_TRUTH says this is a commercial property, never use home/house/homeowner.\n"
    "10. Keep every bullet specific to THIS customer. If a bullet would be true for any random "
    "customer, cut it.\n\n" + SCHEMA_DOC
)


# --------------------------------------------------------------------------- context

def build_report_card_context(d: dict) -> dict:
    """Assemble the prompt context from the deterministic derivation dict `d`.

    `d` is produced by customer_opportunity_report_card._derive and carries:
    dossier, facts, intel, trade, cross_signals, vision, flags, header fields.
    """
    context = build_llm_context(d["facts"], d["dossier"], d["trade"], d["cross_signals"], d["intel"])
    vision = d.get("vision") or {}
    context["report_card"] = {
        "lifetime_revenue_display": d.get("revenue_display") or "",
        "customer_since": d.get("cust_since") or "",
        "membership_line": d.get("membership_line") or "",
        "equipment_age_line": d.get("eq_age_line") or "",
        "mandatory_flags": [f["text"] for f in d.get("flags") or []],
        "booking_note_repair_signals": d.get("repair_followups") or [],
        "photo_source_note": vision.get("photo_source_note") or "No usable photo-vision summary attached",
        "photo_findings": [
            {
                "indexes": f.get("indexes") or [],
                "finding": str(f.get("finding") or ""),
                "bucket": str(f.get("bucket") or ""),
                "confidence": str(f.get("confidence") or "unknown"),
            }
            for f in (vision.get("findings") or [])
            if str(f.get("finding") or "").strip()
        ][:10],
    }
    return context


# --------------------------------------------------------------------------- parsing

def _strip_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    return cleaned.strip()


def parse_sections(text: str) -> dict | None:
    try:
        raw = json.loads(_strip_fences(text))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_sections(raw)


def _str_list(val, limit: int) -> list[str]:
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if str(x or "").strip()][:limit]


def _pick_enum(val, allowed: tuple, default: str) -> str:
    v = str(val or "").strip()
    for a in allowed:
        if v.lower() == a.lower():
            return a
    return default


def normalize_sections(raw: dict) -> dict | None:
    """Coerce model output into the canonical sections shape; None if unusable."""
    opps = []
    for o in raw.get("primary_opportunities") or []:
        if not isinstance(o, dict):
            continue
        title = str(o.get("title") or "").strip()
        bullets = _str_list(o.get("bullets"), 5)
        if not title or not bullets:
            continue
        opps.append({
            "title": title,
            "priority": _pick_enum(o.get("priority"), PRIORITIES, "MEDIUM"),
            "bullets": bullets,
        })
    opps = opps[:3]
    buying = _str_list(raw.get("buying_behavior"), 6)
    if not opps or not buying:
        return None
    vis = raw.get("visual_inspection") or {}
    if not isinstance(vis, dict):
        vis = {}
    overall = raw.get("overall") or {}
    if not isinstance(overall, dict):
        overall = {}
    return {
        "call_reason": str(raw.get("call_reason") or "").strip() or None,
        "customer_profile_extra": _str_list(raw.get("customer_profile_extra"), 2),
        "buying_behavior": buying,
        "visual_inspection": {
            "positives": _str_list(vis.get("positives"), 4),
            "verify_first": _str_list(vis.get("verify_first"), 8),
        },
        "primary_opportunities": opps,
        "coaching_notes": _str_list(raw.get("coaching_notes"), 4),
        "overall": {
            "relationship": _pick_enum(overall.get("relationship"), RELATIONSHIPS, "B"),
            "likelihood": _pick_enum(overall.get("likelihood"), LIKELIHOODS, "Medium"),
            "risk": _pick_enum(overall.get("risk"), RISKS, "Moderate"),
            "secondary_opportunity": str(overall.get("secondary_opportunity") or "").strip(),
            "rationale": str(overall.get("rationale") or "").strip(),
        },
        "source": "llm",
    }


# --------------------------------------------------------------------------- validation

def _walk_strings(sections: dict):
    """Yield (path, text, container, key/index) for every narrative string."""
    if sections.get("call_reason"):
        yield ("call_reason", sections["call_reason"], sections, "call_reason")
    for key in ("customer_profile_extra", "buying_behavior", "coaching_notes"):
        for i, s in enumerate(sections.get(key) or []):
            yield (f"{key}[{i}]", s, sections[key], i)
    vis = sections.get("visual_inspection") or {}
    for key in ("positives", "verify_first"):
        for i, s in enumerate(vis.get(key) or []):
            yield (f"visual_inspection.{key}[{i}]", s, vis[key], i)
    for j, opp in enumerate(sections.get("primary_opportunities") or []):
        yield (f"primary_opportunities[{j}].title", opp["title"], opp, "title")
        for i, s in enumerate(opp.get("bullets") or []):
            yield (f"primary_opportunities[{j}].bullets[{i}]", s, opp["bullets"], i)
    overall = sections.get("overall") or {}
    for key in ("secondary_opportunity", "rationale"):
        if overall.get(key):
            yield (f"overall.{key}", overall[key], overall, key)


_DOLLAR_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*([kK])?")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_LONG_ID_RE = re.compile(r"\b(\d{6,})\b")


def _allowed_number_sets(context_blob: str) -> tuple[set[int], set[str], set[str]]:
    """Sets of (dollar-ish ints, year strings, long-id strings) present in the context."""
    amounts: set[int] = set()
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", context_blob):
        try:
            amounts.add(int(round(float(tok.replace(",", "")))))
        except Exception:
            continue
    years = set(_YEAR_RE.findall(context_blob))
    long_ids = set(_LONG_ID_RE.findall(context_blob.replace(",", "")))
    return amounts, years, long_ids


def validate_sections(sections: dict, dossier: dict, facts: dict, intel: dict, context_blob: str) -> list[dict]:
    """Audit narrative strings against ground truth. Returns violation dicts:
    {"path", "message"} — path identifies the offending string for scrubbing."""
    violations: list[dict] = []
    amounts, years, long_ids = _allowed_number_sets(context_blob)

    whitelist_low = " ".join(_equipment_whitelist(dossier)).lower()
    likely = facts.get("equipment_likely_present_not_on_file")
    if likely is None:
        likely = equipment_implied_by_history(dossier, _equipment_whitelist(dossier))
    likely_labels = {str(x.get("label") or "").lower() for x in likely if isinstance(x, dict)}

    total_estimates = (intel or {}).get("total_estimates", 0)
    is_commercial = bool(facts.get("is_commercial"))

    for path, text, _, _ in _walk_strings(sections):
        low = text.lower()
        for m in _DOLLAR_RE.finditer(text):
            try:
                val = float(m.group(1).replace(",", ""))
            except Exception:
                continue
            if m.group(2):
                val *= 1000
            if int(round(val)) not in amounts:
                violations.append({"path": path, "message": f"Dollar figure {m.group(0).strip()} does not exist in the facts context — only use exact figures from context."})
        for y in _YEAR_RE.findall(text):
            if y not in years:
                violations.append({"path": path, "message": f"Year {y} does not exist in the facts context — never invent dates/years."})
        for ident in _LONG_ID_RE.findall(text.replace(",", "")):
            if ident not in long_ids:
                violations.append({"path": path, "message": f"ID/number {ident} does not exist in the facts context — only cite real estimate/invoice/job IDs."})
        for label, keywords in HALLUCINATION_WATCHLIST.items():
            mentioned = any(k in low for k in keywords)
            if not mentioned or any(k in whitelist_low for k in keywords):
                continue
            if label in likely_labels:
                if "not on file" not in low or "verify" not in low:
                    violations.append({"path": path, "message": f"{label} is evidenced by history but NOT on installed equipment — this line must say 'not on file' and 'verify on arrival'."})
            else:
                violations.append({"path": path, "message": f"Mention of {label}: no {label} on installed equipment or in estimate/invoice evidence. Do not invent equipment."})
        if total_estimates > 0 and re.search(r"no estimate history|new customer", low):
            violations.append({"path": path, "message": f"Customer HAS estimate history ({total_estimates} estimates) — never say 'no estimate history' or 'new customer'."})
        if is_commercial and re.search(r"\bhome\b|\bhomeowner\b|\bresidence\b|\bhouse\b", low):
            violations.append({"path": path, "message": "Commercial property — never use 'home', 'house', 'homeowner', or 'residence'."})
    return violations


def scrub_sections(sections: dict, violations: list[dict]) -> dict | None:
    """Remove strings that still violate after the correction retry.

    Required slots (first opportunity, buying behavior) becoming empty makes the
    output unusable → return None so the caller falls back to deterministic."""
    bad_paths = {v["path"] for v in violations}
    if not bad_paths:
        return sections
    # Collect removals as (container, key) — delete list items in reverse index order.
    list_removals: dict[int, tuple[list, list[int]]] = {}
    for path, _, container, key in list(_walk_strings(sections)):
        if path not in bad_paths:
            continue
        if isinstance(container, list):
            entry = list_removals.setdefault(id(container), (container, []))
            entry[1].append(key)
        elif isinstance(container, dict):
            if key == "title":
                # A hallucinated opportunity title invalidates the whole opportunity.
                container["bullets"] = []
                container["title"] = ""
            else:
                container[key] = "" if key != "call_reason" else None
    for container, idxs in list_removals.values():
        for i in sorted(set(idxs), reverse=True):
            del container[i]
    sections["primary_opportunities"] = [
        o for o in sections.get("primary_opportunities") or [] if o.get("title") and o.get("bullets")
    ]
    if not sections["primary_opportunities"] or not sections.get("buying_behavior"):
        return None
    return sections


# --------------------------------------------------------------------------- LLM call

def _default_llm_call(sys_msg: str, user_blob: str) -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    payload = {
        "model": os.environ.get("LEX_REPORT_CARD_MODEL") or os.environ.get("LEX_BRIEF_MODEL", "openai/gpt-4o"),
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_blob},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://lexairconditioning.com", "X-Title": "LEX Opportunity Report Card"},
    )
    with request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def llm_enabled() -> bool:
    if os.environ.get("LEX_REPORT_CARD_USE_LLM", "").lower() in ("0", "false", "no"):
        return False
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def generate_sections(d: dict, llm_call=None) -> dict | None:
    """Produce validated narrative sections, or None when the deterministic
    fallback should be used (no key, call failure, or unscrubbable output)."""
    call = llm_call or _default_llm_call
    try:
        context = build_report_card_context(d)
        blob = serialize_context_for_llm(context, max_chars=16000)
        sections = parse_sections(call(SYS_MSG, blob))
        if sections is None:
            print("report_card_llm: unparseable/incomplete LLM output — using deterministic fallback", file=sys.stderr)
            return None
        violations = validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob)
        if violations:
            correction = (
                "\n\nIMPORTANT CORRECTIONS — your previous draft had these problems. "
                "Rewrite the JSON and fix ALL of them:\n- "
                + "\n- ".join(sorted({v["message"] for v in violations}))
            )
            retry = parse_sections(call(SYS_MSG + correction, blob))
            if retry is not None:
                retry_violations = validate_sections(retry, d["dossier"], d["facts"], d["intel"], blob)
                if not retry_violations:
                    return retry
                sections, violations = retry, retry_violations
        if violations:
            print(
                "report_card_llm: scrubbing %d unresolved violation(s) after retry" % len(violations),
                file=sys.stderr,
            )
            sections = scrub_sections(sections, violations)
            if sections is None or validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob):
                print("report_card_llm: output unusable after scrub — using deterministic fallback", file=sys.stderr)
                return None
        return sections
    except Exception as exc:
        print(f"report_card_llm: synthesis failed ({exc}) — using deterministic fallback", file=sys.stderr)
        return None
