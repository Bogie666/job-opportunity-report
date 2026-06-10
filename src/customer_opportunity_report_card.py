#!/usr/bin/env python3
"""Render a Customer Opportunity Report Card from a cached ServiceTitan dossier.

Architecture (facts by rules, narrative by LLM, validated by rules):
  1. _derive()      — deterministic facts: ages, dollars, estimate intel, memberships,
                      tiered equipment/home-age flags. Never produced by a model.
  2. sections       — narrative slots (buying behavior, opportunities, coaching, score).
                      Primary path is report_card_llm.generate_sections(): structured
                      JSON synthesis audited against the facts context (every dollar,
                      year, and ID must exist in context; equipment whitelist enforced).
                      When no API key is set, the call fails, or validation cannot be
                      satisfied, deterministic_sections() fills the same slots.
  3. render_markdown — one renderer for both paths, so the locked navy/gold section
                      format is identical regardless of how the slots were filled.

The header facts, equipment-age line, and ⚠ FLAG bullets are always rendered by
Python directly — the model can neither drop nor alter them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_flags import equipment_age_flag, home_age_flags  # noqa: E402
from report_card_facts import (  # noqa: E402
    _estimate_categories,
    _money,
    analyze_estimate_history,
    build_facts_block,
    cross_trade_signals,
    membership_one_liner,
    trade_from_jobtype,
)
from serial_decoder import decode_serial, unsupported_brand  # noqa: E402

import report_card_llm  # noqa: E402


def _date(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.year < 2000:
            return "unknown"
        return dt.strftime("%B %Y")
    except Exception:
        return iso[:10]


def _year(iso: str | None) -> int | None:
    if not iso:
        return None
    m = re.search(r"(20\d{2}|19\d{2})", str(iso))
    return int(m.group(1)) if m else None


def _status(e: dict) -> str:
    return ((e.get("status") or {}).get("name") or "").strip()


def _sold_estimates(estimates: list[dict]) -> list[dict]:
    return [e for e in estimates if _status(e).lower() == "sold"]


def _open_estimates(estimates: list[dict]) -> list[dict]:
    return [e for e in estimates if _status(e).lower() == "open"]


def _money_sum(rows: list[dict]) -> float:
    return sum(float(e.get("subtotal") or 0) for e in rows)


def _category_counts(estimates: list[dict]) -> Counter:
    """Count estimates per coaching bucket using the shared SKU categorizer
    (replaces the old hardcoded model-number keyword lists)."""
    c: Counter = Counter()
    bucket_of = {
        "IAQ / air quality": "IAQ",
        "system replacement / capital equipment": "HVAC equipment",
        "surge protection": "Electrical",
        "electrical panel / service": "Electrical",
        "gfci / outlets / wiring": "Electrical",
        "ev charger": "Electrical",
        "water heater": "Water/plumbing",
        "plumbing repair / fixture": "Water/plumbing",
        "re-pipe / supply lines": "Water/plumbing",
    }
    for e in estimates:
        for cat in _estimate_categories(e):
            bucket = bucket_of.get(cat)
            if bucket:
                c[bucket] += 1
    return c


def _sold_capital_systems(sold: list[dict]) -> list[dict]:
    return [e for e in sold if "system replacement / capital equipment" in _estimate_categories(e)]


def _active_membership(memberships: list[dict]) -> dict | None:
    for m in memberships:
        if str(m.get("status") or "").lower() == "active":
            return m
    for m in memberships:
        if m.get("active") and not m.get("cancellationDate") and str(m.get("status") or "").lower() not in {"expired", "canceled", "cancelled"}:
            return m
    return memberships[0] if memberships else None


_SYSTEM_COMPONENT_RE = re.compile(
    r"condenser|heat pump|air handler|furnace|evap(orator)? coil|\bcoil\b|package unit|rtu|mini[- ]?split",
    re.I,
)


def _remaining_older_equipment(dossier: dict) -> list[dict]:
    """Older / missing-date system components that likely represent remaining systems
    when newer replacements are also on file. Brand-agnostic: the signal is the
    contrast between recent installs and old/undated system components."""
    equipment = [eq for eq in (dossier.get("installed_equipment") or []) if eq.get("active", True)]
    now_year = datetime.now(timezone.utc).year
    recent_years = {
        _year(eq.get("installedOn")) for eq in equipment
        if _year(eq.get("installedOn")) and _year(eq.get("installedOn")) >= now_year - 4
    }
    if not recent_years:
        return []
    older = []
    for eq in equipment:
        installed_year = _year(eq.get("installedOn"))
        if installed_year is not None and installed_year < 2000:
            installed_year = None  # ST placeholder dates
        if installed_year and installed_year >= now_year - 6:
            continue
        type_obj = eq.get("type")
        type_name = (type_obj.get("name") if isinstance(type_obj, dict) else type_obj) or ""
        name = str(eq.get("name") or type_name or "Equipment")
        blob = f"{type_name} {name} {eq.get('model') or ''}"
        if not _SYSTEM_COMPONENT_RE.search(blob):
            continue
        older.append({
            "name": name,
            "manufacturer": str(eq.get("manufacturer") or ""),
            "model": str(eq.get("model") or ""),
            "installed_year": installed_year,
        })
    return older


def _job_summary_age_signal(dossier: dict) -> str | None:
    text = " ".join(str((dossier.get("job") or {}).get(k) or "") for k in ["summary", "summaryOfWork"])
    m = re.search(r"other\s+is\s+(\d{1,2})\s*years", text, re.I)
    if m:
        return f"booking notes say the third/other system is about {m.group(1)} years old"
    m = re.search(r"Age of the unit\?\s*(\d{1,2})\s*yrs", text, re.I)
    if m:
        if "oldest system" in text.lower():
            return f"booking notes say the oldest system is about {m.group(1)} years old"
        return f"booking notes say the unit is about {m.group(1)} years old"
    m = re.search(r"\b(\d{1,2})\s*yrs?\s*old\b", text, re.I)
    if m:
        return f"booking notes say the system age is about {m.group(1)} years old"
    m = re.search(r"Age of HVAC System:\s*([^\n<]+)", text, re.I)
    if m and "other" in m.group(1).lower():
        return f"booking notes list system ages as {m.group(1).strip()}"
    return None


def _last_major_purchase(sold: list[dict]) -> str:
    if not sold:
        return "No sold estimates found in pulled estimate data."
    latest = max(sold, key=lambda e: e.get("soldOn") or e.get("createdOn") or "")
    return f"Last major sold estimate found: {_date(latest.get('soldOn') or latest.get('createdOn'))} ({latest.get('name') or 'sold work'}, {_money(latest.get('subtotal'))})."


def _photo_summary(vision: dict | None) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    gaps: list[str] = []
    if not vision:
        gaps.append("No usable photo-vision summary attached to this report card; capture current equipment and panel photos.")
        return positives, gaps
    note = vision.get("photo_source_note") or "Photos available"
    if "historical" in note.lower():
        gaps.append("Photo set is historical, so use it as verify-on-arrival evidence rather than a pitch by itself.")
    findings = vision.get("findings") or []
    lower = " ".join(str(f.get("finding") or "") for f in findings).lower()
    if re.search(r"\b(new|clean|well[- ]maintained|maintained)\b", lower):
        positives.append("Newer equipment appears clean/maintained in available photos.")
    if "electrical" not in lower and "panel" not in lower:
        gaps.append("No electrical panel photo signal found; capture current panel photos for surge/capacity/safety review.")
    # Medium-confidence historical photo findings should not outrank hard ST history.
    for f in findings[:4]:
        conf = str(f.get("confidence") or "").lower()
        finding = str(f.get("finding") or "").strip()
        if finding and conf in {"medium", "low"}:
            gaps.append(f"Verify photo finding before recommending: {finding}.")
    return positives, gaps


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _call_issue(job: dict) -> str | None:
    text = str(job.get("summary") or job.get("summaryOfWork") or "")
    m = re.search(r"1\)\s*Issue Going on\?\s*(.+?)(?:\n\s*2\)|$)", text, re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    clean = _strip_html(text)
    m = re.search(r"(?:called to|get|get pricing for|pricing for)\s+(.+?)(?:\.\s| Customer | Appointment |$)", clean, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def _repair_followup_items(job: dict) -> list[str]:
    """Fallback heuristic: note-backed repair follow-up items from booking summaries.
    The LLM path reads the raw booking note instead; this keeps the offline path
    from collapsing to a generic age template on pricing/confirmation calls."""
    clean = _strip_html(str(job.get("summary") or job.get("summaryOfWork") or ""))
    lower = clean.lower()
    items: list[str] = []
    if "ductwork" in lower or "duct" in lower:
        if any(t in lower for t in ["kink", "sealing", "seal", "repair"]):
            items.append("ductwork kinks / duct sealing repair")
    if any(t in lower for t in ["biological growth", "auv", "uv", "air scrub", "iaq"]):
        items.append("biological growth / AUV-IAQ recommendation")
    if "fan motor" in lower or "over amp" in lower or "over-amp" in lower or "overamping" in lower:
        items.append("outdoor condenser fan motor over-amping")
    return items


def _photo_finding_lines(vision: dict | None) -> list[str]:
    if not vision:
        return []
    lines = []
    for f in vision.get("findings") or []:
        finding = str(f.get("finding") or "").strip()
        if not finding:
            continue
        idx = f.get("indexes") or []
        idx_text = f"images {', '.join(str(x) for x in idx)}" if idx else "photo evidence"
        conf = str(f.get("confidence") or "unknown").lower()
        bucket = str(f.get("bucket") or "photo finding")
        lines.append(f"Verify {idx_text}: {finding} ({bucket}, {conf} confidence).")
    return lines


def _equipment_age_summary(dossier: dict, age_signal: str | None) -> tuple[str | None, list[dict]]:
    """Return (CUSTOMER PROFILE equipment-age line, list of tiered flag dicts).

    Prefers structured installedOn dates, falls back to serial decode, then to the
    booking-note age signal. Always verify-on-arrival — ST install dates are
    routinely wrong/placeholder. Flag thresholds are per equipment class
    (opportunity_flags.EQUIPMENT_AGE_TIERS), with an urgent tier past typical
    service life."""
    equipment = dossier.get("installed_equipment") or []
    now_year = datetime.now(timezone.utc).year
    parts: list[str] = []
    flags: list[dict] = []
    seen = set()
    for eq in equipment:
        if not eq.get("active", True):
            continue
        type_obj = eq.get("type")
        type_name = (type_obj.get("name") if isinstance(type_obj, dict) else type_obj) or eq.get("name") or "Equipment"
        type_name = str(type_name).strip() or "Equipment"
        mfg = str(eq.get("manufacturer") or "").strip()
        installed_year = _year(eq.get("installedOn"))
        # Treat 1900-01-01 / 1990-01-01 ServiceTitan placeholders as missing
        if installed_year is not None and installed_year < 2000:
            installed_year = None
        serial = str(eq.get("serialNumber") or "")
        decoded_year = None
        decoder_conf = None
        decoded_label = None
        if not installed_year and serial:
            res = decode_serial(mfg, serial)
            if res:
                decoded_year, decoder_conf, decoded_label = res
        year = installed_year or decoded_year
        label = f"{type_name} ({mfg})".strip() if mfg else type_name
        if year and 2000 <= year <= now_year:
            age = now_year - year
            if installed_year:
                source = f"installed {year}"
            else:
                source = f"{decoded_label} {year}, {decoder_conf} confidence"
            key = (type_name.lower(), year)
            if key in seen:
                continue
            seen.add(key)
            parts.append(f"{label} ~{age} yrs ({source})")
            flag = equipment_age_flag(f"{type_name} {eq.get('name') or ''}", age, source=source, display_label=label)
            if flag:
                flags.append(flag)
        else:
            key = (type_name.lower(), "nodate")
            if key in seen:
                continue
            seen.add(key)
            if serial and unsupported_brand(mfg):
                parts.append(f"{label} install date missing; serial decoder does not yet support {mfg} — verify nameplate year")
            elif serial:
                parts.append(f"{label} install date missing and serial format did not decode — verify nameplate year")
            else:
                parts.append(f"{label} install date missing, verify nameplate")
    summary = None
    if parts:
        summary = "Equipment age on file: " + "; ".join(parts) + "."
    elif age_signal:
        summary = f"Equipment age on file: no installed-equipment dates; {age_signal}."
    # Honor a booking-note "~12 yrs old" signal even when ST equipment dates are missing.
    if not flags and age_signal:
        m = re.search(r"(\d{1,2})\s*(?:yrs|years)", age_signal)
        if m and int(m.group(1)) >= 10:
            yrs = int(m.group(1))
            flags.append({
                "id": "hvac_note", "kind": "equipment_age", "label": "HVAC system", "age": yrs,
                "threshold": 10, "severity": "flag", "source": "booking notes",
                "text": (
                    f"⚠ FLAG: HVAC system ~{yrs} yrs per booking notes — over 10-yr replacement "
                    "threshold; verify nameplate and discuss replacement planning."
                ),
            })
    # Dedupe flag texts while preserving order.
    deduped, seen_flags = [], set()
    for f in flags:
        if f["text"] not in seen_flags:
            deduped.append(f)
            seen_flags.add(f["text"])
    return summary, deduped


def _confidence_score(sold_total: float, sold_count: int, active_recurring_membership: bool, older_equipment: bool, open_count: int) -> tuple[str, str, str, str]:
    if sold_count == 0 and sold_total <= 0:
        relationship = "B" if active_recurring_membership else "C"
        return relationship, "Unknown-Low", "Moderate", "No sold estimate history in pulled data; purchase likelihood should be treated as unproven until the tech confirms urgency and presents repair options."
    if sold_total >= 30000 and active_recurring_membership:
        relationship = "A+"
    elif sold_total >= 10000 or active_recurring_membership:
        relationship = "A"
    else:
        relationship = "B"
    likelihood = "High" if sold_total >= 20000 and active_recurring_membership else ("Medium-High" if sold_count >= 2 else "Medium")
    risk = "Low" if sold_total >= 20000 and active_recurring_membership else "Moderate"
    if open_count > 5 and not older_equipment:
        likelihood = "Medium-High"
    return relationship, likelihood, risk, "Supported by pulled sold estimates, membership status, and open estimate signals."


# --------------------------------------------------------------------------- derive

def _derive(bundle: dict, vision: dict | None, lifetime_revenue: float | None) -> dict:
    """Everything deterministic the card needs, computed once and shared by the
    LLM path, the fallback path, and the renderer."""
    dossier = bundle.get("dossier") or bundle
    meta = bundle.get("meta") or {}
    facts = build_facts_block(dossier)
    job = dossier.get("job") or {}
    customer = dossier.get("customer") or {}
    memberships = dossier.get("memberships") or []
    estimates = dossier.get("estimates") or []
    trade = facts.get("trade") or trade_from_jobtype(meta.get("job_type", ""), meta.get("business_unit", ""))
    facts["trade"] = trade
    intel = analyze_estimate_history(estimates, job.get("id"), primary_trade=trade)
    facts["_intel"] = intel
    cross_signals = cross_trade_signals(facts, dossier, trade)
    facts["cross_signals"] = cross_signals

    sold = _sold_estimates(estimates)
    open_rows = _open_estimates(estimates)
    sold_total = _money_sum(sold)
    sold_capital = _sold_capital_systems(sold)
    active_m = _active_membership(memberships)
    active_recurring_m = bool(active_m) and str(active_m.get("billingFrequency") or "").lower() not in {"onetime", "one time", "one-time"}
    age_signal = _job_summary_age_signal(dossier)
    older = _remaining_older_equipment(dossier)
    eq_age_line, eq_flags = _equipment_age_summary(dossier, age_signal)
    flags = list(eq_flags) + home_age_flags(facts.get("home_built_year"))

    membership_line = membership_one_liner({
        "active": [active_m] if active_m and str(active_m.get("status") or "").lower() == "active" else [],
        "canceled": [m for m in memberships if str(m.get("status") or "").lower() == "canceled"],
        "expired": [m for m in memberships if str(m.get("status") or "").lower() == "expired"],
        "suspended": [m for m in memberships if str(m.get("status") or "").lower() == "suspended"],
    }) if memberships else "Membership not found in pulled data"

    relationship, likelihood, risk, score_rationale = _confidence_score(
        sold_total if lifetime_revenue is None else lifetime_revenue,
        len(sold), active_recurring_m, bool(older), len(open_rows),
    )

    recent_system_text = ""
    if sold_capital:
        cap = max(sold_capital, key=lambda e: float(e.get("subtotal") or 0))
        if float(cap.get("subtotal") or 0) >= 5000:
            recent_system_text = f"Major HVAC investment on file: {cap.get('name') or 'system replacement'} sold {_date(cap.get('soldOn') or cap.get('createdOn'))} for {_money(cap.get('subtotal'))}."

    return {
        "dossier": dossier,
        "meta": meta,
        "facts": facts,
        "intel": intel,
        "trade": trade,
        "cross_signals": cross_signals,
        "vision": vision,
        "job": job,
        "customer": customer,
        "sold": sold,
        "open_rows": open_rows,
        "sold_total": sold_total,
        "sold_capital": sold_capital,
        "active_recurring_m": active_recurring_m,
        "older": older,
        "age_signal": age_signal,
        "eq_age_line": eq_age_line,
        "flags": flags,
        "call_issue": _call_issue(job),
        "repair_followups": _repair_followup_items(job),
        "photo_lines": _photo_finding_lines(vision),
        "cats_open": _category_counts(open_rows),
        "membership_line": membership_line,
        "revenue_display": _money(lifetime_revenue) if lifetime_revenue is not None else f"{_money(sold_total)}+ known sold estimates",
        "cust_since": _date(customer.get("createdOn")),
        "recent_system_text": recent_system_text,
        "fallback_grade": (relationship, likelihood, risk, score_rationale),
    }


# --------------------------------------------------------------------------- fallback sections

def _fallback_primary(d: dict) -> str:
    trade_label = str(d["trade"] or "").lower()
    call_issue = d["call_issue"]
    if trade_label == "plumbing":
        primary = "Current plumbing findings / safety and reliability"
        if call_issue:
            primary = f"Current plumbing demand issue ({call_issue})"
    elif trade_label == "electrical":
        primary = "Current electrical findings / protection and safety"
    else:
        primary = "Current HVAC findings / comfort reliability"
        if d["repair_followups"]:
            primary = "Repair estimate follow-up: " + "; ".join(d["repair_followups"])
        else:
            older = d["older"]
            if older:
                manufacturers = [eq.get("manufacturer") for eq in older if eq.get("manufacturer")]
                brand = Counter(manufacturers).most_common(1)[0][0] if manufacturers else "older"
                primary = f"Remaining older {brand} system"
            elif d["age_signal"]:
                primary = "Older HVAC system"
            if d["age_signal"]:
                primary += f" ({d['age_signal']})"
    return primary


def deterministic_sections(d: dict) -> dict:
    """Rule-based slot filling — used when the LLM path is disabled or rejected.
    Generic by design: anything customer-specific belongs to the LLM path, which
    reasons from this job's actual notes/history instead of canned templates."""
    trade_label = str(d["trade"] or "").lower()
    sold, open_rows = d["sold"], d["open_rows"]
    photo_lines = d["photo_lines"]
    positives, photo_gaps = _photo_summary(d["vision"])
    primary = _fallback_primary(d)

    buying: list[str] = []
    if d["sold_capital"]:
        buying.append(f"Has approved major HVAC equipment work ({len(d['sold_capital'])} capital-equipment sold estimate signal(s)).")
    if sold:
        buying.append(f"Pulled estimate history shows {len(sold)} sold estimate(s), totaling {_money(d['sold_total'])} in known sold estimate value.")
    if d["cats_open"].get("IAQ"):
        buying.append(f"Has {d['cats_open']['IAQ']} open/not-closed IAQ-style estimate signal(s); do not lead with IAQ unless today's findings make it relevant.")
    buying.append(f"{_last_major_purchase(sold)}")
    if sold:
        buying.append("Buying pattern: willing to invest when the need is clear and options are tied to comfort, reliability, or protecting prior investment.")
    else:
        buying.append("Buying pattern: unproven in pulled data; lead with the needed repair and only add options that are directly supported by today's findings.")

    # Opportunity 1 bullets per branch
    opp1: list[str] = []
    if "Repair estimate follow-up" in primary:
        opp1 = [
            "Treat this as a pricing/confirmation visit for already identified recommendations, not a generic age-based discovery call.",
            "Reconfirm each prior finding with measurements/photos before presenting pricing.",
            "Present a clear repair path first, then good/better options only where the current evidence supports them.",
            "If the booking note mentions an evaluation/diagnostic fee arrangement, address it directly up front.",
        ]
    elif "Current HVAC findings" in primary:
        opp1 = [
            "Diagnose the current issue/maintenance findings first and connect any recommendation to measured evidence.",
            "Verify equipment age, condition, airflow, drains, blower/coil cleanliness, and customer comfort concerns.",
            "Frame options around reliability, comfort, and preventing repeat calls.",
            "If an older system is confirmed, discuss planning before failure rather than pressure to replace today.",
        ]
    elif trade_label == "plumbing":
        opp1 = [
            "Diagnose the plumbing demand issue first and separate must-do repair from optional upgrades.",
            "Verify fixture condition, shutoffs, supply/drain issues, water heater age, pressure, and visible leak risk.",
            "Frame options around safety, water damage prevention, reliability, and code-compliant repair.",
            "If equipment age or pressure issues are confirmed, document future options without overbuilding the ticket.",
        ]
    elif trade_label == "electrical":
        opp1 = [
            "Diagnose the electrical demand issue first and connect recommendations to observed panel/circuit evidence.",
            "Verify panel condition, breaker/circuit load, surge protection, grounding/bonding, and any visible safety concerns.",
            "Frame options around safety, equipment protection, reliability, and preventing repeat nuisance issues.",
            "If surge protection or panel capacity gaps are confirmed, present the cleanest practical option first.",
        ]
    else:
        opp1 = [
            "Verify the age, condition, and served area of the remaining older system.",
            "Compare reliability and efficiency against any newer systems already installed.",
            "Frame as future planning before failure, not pressure to replace today.",
            "Ask whether that zone is as comfortable as the areas served by the newer systems.",
        ]

    opportunities = [{"title": primary, "priority": "HIGH", "bullets": opp1}]

    if trade_label == "plumbing":
        opportunities.append({
            "title": "Water safety / protection options", "priority": "MEDIUM",
            "bullets": [
                "Check water heater age and condition, shutoff condition, visible supply/drain issues, and any early moisture damage.",
                "Offer good/better options only if supported by what is on-site; document findings with photos.",
            ],
        })
        opportunities.append({
            "title": "Membership / approval handling", "priority": "LOW-MEDIUM",
            "bullets": [
                "Confirm the decision-maker and payment/approval path from ServiceTitan notes before expanding scope.",
                "If the repair proceeds, offer membership only as a savings/protection option tied to today's repair, not as proof of prior buying behavior.",
            ],
        })
        secondary = "Water heater age / under-sink and supply protection review"
    elif trade_label == "electrical":
        opportunities.append({
            "title": "Electrical protection review", "priority": "MEDIUM-HIGH",
            "bullets": [
                "Capture current electrical panel photos.",
                "Review surge protection and panel capacity if today's issue or prior quote history supports it.",
                "Route to install/service follow-up only if panel condition, capacity, or protection gaps are found.",
            ],
        })
        opportunities.append({
            "title": "Evidence-based next step", "priority": "MEDIUM",
            "bullets": ["Keep optional recommendations tied to the actual circuit/panel findings from today's visit."],
        })
        secondary = "Electrical panel assessment / surge protection review"
    else:
        has_insulation = any("insulation" in line.lower() for line in photo_lines)
        has_duct_support = any("duct" in line.lower() and any(t in line.lower() for t in ["laying", "support", "hung", "strap", "sagging", "kink"]) for line in photo_lines)
        if has_insulation or has_duct_support:
            bullets = []
            if has_insulation:
                bullets.append("Prior photos show joists/framing visible through insulation; verify depth and coverage today and document with wide attic photos.")
            if has_duct_support:
                bullets.append("Prior photos show ductwork support/restriction concerns; verify whether flex duct is strapped/hung correctly or laying on insulation with sag/kinks.")
            bullets.append("If still present, position this as comfort, efficiency, airflow, and system-protection work tied to today's duct/IAQ repair discussion.")
            opportunities.append({"title": "Attic insulation and duct support verification", "priority": "MEDIUM-HIGH", "bullets": bullets})
            secondary = "Attic insulation / duct support verification"
        else:
            opportunities.append({
                "title": "Electrical evaluation / protection review", "priority": "MEDIUM-HIGH",
                "bullets": [
                    "Capture current electrical panel photos.",
                    "Review surge protection and panel capacity, especially if the customer has already invested in HVAC equipment.",
                    "Route to electrical if panel condition, capacity, or surge protection gaps are found.",
                ],
            })
            secondary = "Electrical panel assessment / surge protection review"
        opportunities.append({
            "title": "Comfort-based maintenance conversation", "priority": "MEDIUM",
            "bullets": [
                "Ask about comfort, hot/cold spots, humidity, and peak-season performance only if today's HVAC scope supports it.",
                "Use answers to connect findings to verified equipment, airflow, or maintenance issues.",
                "Only revisit IAQ if open IAQ history or current blower/duct/return findings clearly support it.",
            ],
        })

    coaching: list[str] = []
    if sold or open_rows:
        coaching.append("Weight customer history and current-call evidence above stale/open estimates.")
    else:
        coaching.append("No sold or open estimate history was pulled; do not infer willingness to buy from nonexistent history.")
    coaching.append("Photo findings from historical images should become verification prompts, not confident recommendations.")
    coaching.append("Keep recommendations tied to today's verified issue before surfacing cross-trade recommendations aggressively.")

    relationship, likelihood, risk, score_rationale = d["fallback_grade"]
    return {
        "call_reason": d["call_issue"],
        "customer_profile_extra": [],
        "buying_behavior": buying,
        "visual_inspection": {"positives": positives, "verify_first": photo_gaps[:5] + photo_lines[:6]},
        "primary_opportunities": opportunities,
        "coaching_notes": coaching,
        "overall": {
            "relationship": relationship,
            "likelihood": likelihood,
            "risk": risk,
            "secondary_opportunity": secondary,
            "rationale": score_rationale,
        },
        "source": "deterministic",
    }


# --------------------------------------------------------------------------- render

def render_markdown(d: dict, sections: dict) -> str:
    meta, facts, job = d["meta"], d["facts"], d["job"]
    customer = d["customer"]
    sold = d["sold"]
    md: list[str] = []
    md.append("# CUSTOMER OPPORTUNITY REPORT CARD")
    md.append("")
    md.append(f"**Customer:** {meta.get('customer') or facts.get('customer') or customer.get('name') or ''}")
    md.append(f"**Job #:** {job.get('jobNumber') or job.get('id')}")
    md.append(f"**Call Type:** {meta.get('job_type') or ''}")
    md.append(f"**Lifetime Revenue:** {d['revenue_display']}")
    md.append(f"**Customer Since:** {d['cust_since']}")
    md.append("")

    md.append("## CUSTOMER PROFILE")
    if sold or d["active_recurring_m"]:
        md.append(f"- Strong existing customer relationship: {d['membership_line']}.")
    else:
        md.append(f"- Relationship signal is limited in pulled data: {d['membership_line']}.")
    call_reason = sections.get("call_reason") or d["call_issue"]
    if call_reason:
        md.append(f"- Reason for call: {call_reason.rstrip('.')}.")
    if d["recent_system_text"]:
        md.append(f"- {d['recent_system_text']}")
    elif sold:
        md.append(f"- Prior sold work on file: {len(sold)} sold estimate(s), largest known sold ticket {_money(max(float(e.get('subtotal') or 0) for e in sold))}.")
    md.append("- Treat this as a trust-based maintenance and planning conversation, not a cold sales call.")
    if facts.get("address"):
        built = facts.get("home_built_year") or facts.get("home_age") or "unknown"
        try:
            age = datetime.now(timezone.utc).year - int(str(built))
        except Exception:
            age = "unknown"
        md.append(f"- Home on file: {facts.get('address')} · built {built} ({age} yrs).")
    for extra in sections.get("customer_profile_extra") or []:
        md.append(f"- {extra}")
    if d["eq_age_line"]:
        md.append(f"- {d['eq_age_line']}")
    # Flags are always rendered deterministically — the LLM cannot drop or alter them.
    for flag in d["flags"]:
        md.append(f"- {flag['text']}")
    md.append("")

    md.append("## BUYING BEHAVIOR")
    for line in sections.get("buying_behavior") or []:
        md.append(f"- {line}")
    md.append("")

    md.append("## VISUAL INSPECTION REVIEW")
    md.append("**Positive findings**")
    positives = (sections.get("visual_inspection") or {}).get("positives") or []
    if positives:
        md.extend(f"- {p}" for p in positives)
    else:
        md.append("- Use the visit to confirm current equipment condition and separate maintenance items from true sales opportunities.")
    md.append("")
    md.append("**Documentation gaps / verify-first items**")
    for g in (sections.get("visual_inspection") or {}).get("verify_first") or []:
        md.append(f"- {g}")
    md.append("")

    md.append("## PRIMARY OPPORTUNITIES")
    for i, opp in enumerate(sections.get("primary_opportunities") or [], start=1):
        md.append(f"### {i}. {opp['title']} ({opp['priority']} PRIORITY)")
        md.extend(f"- {b}" for b in opp["bullets"])
        md.append("")

    md.append("## AI AGENT COACHING NOTES")
    for line in sections.get("coaching_notes") or []:
        md.append(f"- {line}")
    md.append("")

    overall = sections.get("overall") or {}
    opportunities = sections.get("primary_opportunities") or []
    primary_title = opportunities[0]["title"] if opportunities else ""
    md.append("## OVERALL CUSTOMER SCORE")
    md.append(f"**Customer Relationship:** {overall.get('relationship', 'B')}")
    md.append(f"**Likelihood to Purchase:** {overall.get('likelihood', 'Medium')}")
    md.append(f"**Primary Opportunity:** {primary_title}")
    md.append(f"**Secondary Opportunity:** {overall.get('secondary_opportunity') or (opportunities[1]['title'] if len(opportunities) > 1 else '')}")
    rationale = overall.get("rationale") or ""
    risk_line = f"**Risk Level:** {overall.get('risk', 'Moderate')}."
    if rationale:
        risk_line += f" {rationale.rstrip('.')}."
    md.append(risk_line)
    return "\n".join(md).strip() + "\n"


# --------------------------------------------------------------------------- entry

def build_report_card(bundle: dict, vision: dict | None = None, lifetime_revenue: float | None = None, use_llm: bool | None = None) -> str:
    d = _derive(bundle, vision, lifetime_revenue)
    sections = None
    enabled = report_card_llm.llm_enabled() if use_llm is None else use_llm
    if enabled:
        sections = report_card_llm.generate_sections(d)
    if sections is None:
        sections = deterministic_sections(d)
    return render_markdown(d, sections)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", help="Cached job dossier JSON produced by servicetitan_dossier.py")
    ap.add_argument("--vision-summary", help="Optional per-job or summary vision JSON")
    ap.add_argument("--lifetime-revenue", type=float, help="Optional known lifetime revenue override from ST/customer scorecard")
    ap.add_argument("--no-llm", action="store_true", help="Force the deterministic fallback path")
    ap.add_argument("--out", help="Output markdown path")
    args = ap.parse_args()

    from report_card_facts import load_default_env
    load_default_env()

    bundle = json.loads(Path(args.json_path).read_text())
    vision = None
    if args.vision_summary:
        raw = json.loads(Path(args.vision_summary).read_text())
        if "findings" in raw:
            vision = raw
        else:
            job_id = str((bundle.get("dossier") or bundle).get("job", {}).get("id") or (bundle.get("dossier") or bundle).get("job", {}).get("jobNumber"))
            for rec in raw.get("jobs") or raw.get("results") or raw.get("summary") or []:
                if str(rec.get("job_id") or rec.get("jobNumber") or rec.get("job_number")) == job_id:
                    vision = rec
                    break
    md = build_report_card(bundle, vision=vision, lifetime_revenue=args.lifetime_revenue, use_llm=False if args.no_llm else None)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        print(out)
    else:
        print(md)


if __name__ == "__main__":
    main()
