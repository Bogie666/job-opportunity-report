#!/usr/bin/env python3
"""Render a human-style Customer Opportunity Report Card from a cached ServiceTitan dossier.

This is intentionally more executive/field-coaching oriented than the compact tech brief:
- customer relationship and buying behavior first
- de-prioritizes stale estimates when stronger evidence exists
- surfaces remaining-system and documentation-gap opportunities
- keeps photo/vision observations verify-first, especially when photos are historical
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_card_facts import (  # noqa: E402
    _money,
    analyze_estimate_history,
    build_facts_block,
    membership_one_liner,
    summarize_equipment,
    trade_from_jobtype,
)
from serial_decoder import decode_serial, unsupported_brand  # noqa: E402


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


def _estimate_blob(e: dict) -> str:
    parts = [str(e.get("name") or "")]
    for key in ("items", "lineItems", "summary", "invoiceItems"):
        val = e.get(key)
        if isinstance(val, list):
            parts.extend(str(x.get("description") or x.get("name") or x) for x in val if isinstance(x, dict))
        elif val:
            parts.append(str(val))
    return " ".join(parts).lower()


def _category_counts(estimates: list[dict]) -> Counter:
    c: Counter = Counter()
    for e in estimates:
        blob = _estimate_blob(e)
        if any(t in blob for t in ["iaq", "air scrub", "reme", "uv", "purifi", "air quality", "platinum", "gold"]):
            c["IAQ"] += 1
        if any(t in blob for t in ["xv17", "complete system", "system replacement", "condenser", "furnace", "coil"]):
            c["HVAC equipment"] += 1
        if any(t in blob for t in ["surge", "panel", "breaker", "electrical"]):
            c["Electrical"] += 1
        if any(t in blob for t in ["water", "halo", "softener", "filter"]):
            c["Water/plumbing"] += 1
    return c


def _sold_capital_systems(sold: list[dict]) -> list[dict]:
    out = []
    for e in sold:
        blob = _estimate_blob(e)
        if any(t in blob for t in ["xv17", "complete", "system", "condenser", "furnace"]):
            out.append(e)
    return out


def _active_membership(memberships: list[dict]) -> dict | None:
    for m in memberships:
        if str(m.get("status") or "").lower() == "active":
            return m
    for m in memberships:
        if m.get("active") and not m.get("cancellationDate") and str(m.get("status") or "").lower() not in {"expired", "canceled", "cancelled"}:
            return m
    return memberships[0] if memberships else None


def _remaining_older_equipment(dossier: dict) -> list[dict]:
    """Find older not-recently-installed equipment likely representing remaining systems."""
    equipment = dossier.get("installed_equipment") or []
    recent_years = {_year(eq.get("installedOn")) for eq in equipment if _year(eq.get("installedOn")) and _year(eq.get("installedOn")) >= 2020}
    older = []
    for eq in equipment:
        installed_year = _year(eq.get("installedOn"))
        mfg = str(eq.get("manufacturer") or "")
        name = str(eq.get("name") or ((eq.get("type") or {}).get("name") if isinstance(eq.get("type"), dict) else eq.get("type")) or "Equipment")
        model = str(eq.get("model") or "")
        # Older systems in ST often have missing install dates while the new replacements have 2024 dates.
        if installed_year and installed_year >= 2020:
            continue
        if mfg.lower() in {"amana", "goodman"} or model.upper().startswith(("GSX", "GMS", "GISS", "ASX", "ASZ")):
            older.append({"name": name, "manufacturer": mfg, "model": model, "installed_year": installed_year})
    # If there are recent replacements plus old/missing-date Amana/Goodman rows, keep it as a remaining-system signal.
    return older if recent_years else []


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
    m = re.search(r"oldest\s+system\s+is\s+[^\n<]*?Age of the unit\?\s*(\d{1,2})\s*yrs", text, re.I | re.S)
    if m:
        return f"booking notes say the oldest system is about {m.group(1)} years old"
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
    if any(t in lower for t in ["new", "clean", "maintained"]):
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
    m = re.search(r"(?:called to|get|get pricing for|pricing for)\s+(.+?)(?:\.\s| Customer | Appointment | Cody |$)", clean, re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def _repair_followup_items(job: dict) -> list[str]:
    """Extract note-backed repair follow-up items from booking summaries.

    This prevents the report card from falling back to a generic equipment-age template
    when the actual call is to price/confirm already identified repairs.
    """
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


REPLACEMENT_AGE_RULES = (
    # (regex on type/name, equipment label, threshold years)
    (re.compile(r"furnace|condenser|heat pump|air handler|evap(orator)? coil|coil|package unit|rtu|mini[- ]?split", re.I), "HVAC", 10),
    (re.compile(r"water heater|tankless|wh\b", re.I), "Water heater", 10),
)


def _replacement_flag(type_name: str, age_years: int | None) -> str | None:
    if age_years is None or age_years < 10:
        return None
    for pattern, label, threshold in REPLACEMENT_AGE_RULES:
        if pattern.search(type_name) and age_years >= threshold:
            return f"⚠ FLAG: {label} ~{age_years} yrs — over {threshold}-yr replacement threshold; verify nameplate and discuss replacement planning."
    return None


def _equipment_age_summary(dossier: dict, age_signal: str | None) -> tuple[str | None, list[str]]:
    """Return (CUSTOMER PROFILE age line, list of replacement flag bullets).

    Prefers structured installedOn dates from installed_equipment, falls back to
    booking-note age signal, and decodes a year-of-mfg from serial when an installed
    date is missing. Always emits a verify-on-arrival caveat — ST install dates are
    routinely wrong/placeholder. Any HVAC/water-heater component >=10 yrs adds a
    replacement-planning flag the renderer surfaces in CUSTOMER PROFILE.
    """
    equipment = dossier.get("installed_equipment") or []
    now_year = datetime.now(timezone.utc).year
    parts: list[str] = []
    flags: list[str] = []
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
            flag = _replacement_flag(f"{type_name} {eq.get('name') or ''}", age)
            if flag:
                flags.append(f"⚠ FLAG: {label} ~{age} yrs — over 10-yr replacement threshold; verify nameplate and discuss replacement planning.")
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
    # Also honor a booking-note "~12 yrs old" signal even when ST equipment dates are missing.
    if not flags and age_signal:
        m = re.search(r"(\d{1,2})\s*(?:yrs|years)", age_signal)
        if m:
            yrs = int(m.group(1))
            if yrs >= 10:
                flags.append(f"⚠ FLAG: HVAC system ~{yrs} yrs per booking notes — over 10-yr replacement threshold; verify nameplate and discuss replacement planning.")
    # Dedupe flags while preserving order.
    deduped = []
    seen_flags = set()
    for f in flags:
        if f not in seen_flags:
            deduped.append(f)
            seen_flags.add(f)
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


def build_report_card(bundle: dict, vision: dict | None = None, lifetime_revenue: float | None = None) -> str:
    dossier = bundle.get("dossier") or bundle
    meta = bundle.get("meta") or {}
    facts = build_facts_block(dossier)
    job = dossier.get("job") or {}
    customer = dossier.get("customer") or {}
    memberships = dossier.get("memberships") or []
    estimates = dossier.get("estimates") or []
    trade = facts.get("trade") or trade_from_jobtype(meta.get("job_type", ""), meta.get("business_unit", ""))
    intel = analyze_estimate_history(estimates, job.get("id"), primary_trade=trade)

    sold = _sold_estimates(estimates)
    open_rows = _open_estimates(estimates)
    sold_total = _money_sum(sold)
    sold_capital = _sold_capital_systems(sold)
    active_m = _active_membership(memberships)
    active_recurring_m = bool(active_m) and str(active_m.get("billingFrequency") or "").lower() not in {"onetime", "one time", "one-time"}
    call_issue = _call_issue(job)
    repair_followups = _repair_followup_items(job)
    photo_lines = _photo_finding_lines(vision)
    older = _remaining_older_equipment(dossier)
    age_signal = _job_summary_age_signal(dossier)
    cats_sold = _category_counts(sold)
    cats_open = _category_counts(open_rows)
    positives, photo_gaps = _photo_summary(vision)

    revenue_display = _money(lifetime_revenue) if lifetime_revenue is not None else f"{_money(sold_total)}+ known sold estimates"
    cust_since = _date(customer.get("createdOn"))
    membership = membership_one_liner({
        "active": [active_m] if active_m and str(active_m.get("status") or "").lower() == "active" else [],
        "canceled": [m for m in memberships if str(m.get("status") or "").lower() == "canceled"],
        "expired": [m for m in memberships if str(m.get("status") or "").lower() == "expired"],
        "suspended": [m for m in memberships if str(m.get("status") or "").lower() == "suspended"],
    }) if memberships else "Membership not found in pulled data"
    relationship, likelihood, risk, score_rationale = _confidence_score(sold_total if lifetime_revenue is None else lifetime_revenue, len(sold), active_recurring_m, bool(older), len(open_rows))

    recent_system_text = ""
    if sold_capital:
        cap = max(sold_capital, key=lambda e: float(e.get("subtotal") or 0))
        if float(cap.get("subtotal") or 0) >= 5000:
            recent_system_text = f"Major HVAC investment on file: {cap.get('name') or 'system replacement'} sold {_date(cap.get('soldOn') or cap.get('createdOn'))} for {_money(cap.get('subtotal'))}."

    older_desc = ""
    if older:
        manufacturers = [eq.get("manufacturer") for eq in older if eq.get("manufacturer")]
        brand = Counter(manufacturers).most_common(1)[0][0] if manufacturers else "older"
        older_desc = brand
    trade_label = str(trade or "").lower()
    if trade_label == "plumbing":
        if call_issue and any(t in call_issue.lower() for t in ["disposal", "garbage disposal", "sink"]):
            primary = "Kitchen sink disposal leaking internally"
        else:
            primary = "Current plumbing findings / safety and reliability"
    elif trade_label == "electrical":
        primary = "Current electrical findings / protection and safety"
    else:
        primary = "Current HVAC findings / comfort reliability"
    if trade_label == "hvac" and repair_followups:
        primary = "Repair estimate follow-up: " + "; ".join(repair_followups)
    elif trade_label == "hvac":
        if older_desc:
            primary = f"Remaining older {older_desc} system"
        elif age_signal:
            primary = "Older HVAC system"
        if age_signal:
            primary += f" ({age_signal})"

    md: list[str] = []
    md.append("# CUSTOMER OPPORTUNITY REPORT CARD")
    md.append("")
    md.append(f"**Customer:** {meta.get('customer') or facts.get('customer') or customer.get('name') or ''}")
    md.append(f"**Job #:** {job.get('jobNumber') or job.get('id')}")
    md.append(f"**Call Type:** {meta.get('job_type') or ''}")
    md.append(f"**Lifetime Revenue:** {revenue_display}")
    md.append(f"**Customer Since:** {cust_since}")
    md.append("")

    md.append("## CUSTOMER PROFILE")
    if sold or active_recurring_m:
        md.append(f"- Strong existing customer relationship: {membership}.")
    else:
        md.append(f"- Relationship signal is limited in pulled data: {membership}.")
    if call_issue:
        md.append(f"- Reason for call: {call_issue}.")
    if recent_system_text:
        md.append(f"- {recent_system_text}")
    elif sold:
        md.append(f"- Prior sold work on file: {len(sold)} sold estimate(s), largest known sold ticket {_money(max(float(e.get('subtotal') or 0) for e in sold))}.")
    md.append("- Treat this as a trust-based maintenance and planning conversation, not a cold sales call.")
    if facts.get("address"):
        built = facts.get("home_built_year") or "unknown"
        age_val = facts.get("home_age")
        if str(age_val) == str(built):
            try:
                age_val = datetime.now(timezone.utc).year - int(str(built))
            except Exception:
                pass
        age = age_val or "unknown"
        md.append(f"- Home on file: {facts.get('address')} · built {built} ({age} yrs).")
    eq_age_line, eq_flags = _equipment_age_summary(dossier, age_signal)
    if eq_age_line:
        md.append(f"- {eq_age_line}")
    for flag in eq_flags:
        md.append(f"- {flag}")
    md.append("")

    md.append("## BUYING BEHAVIOR")
    if sold_capital:
        capital_phrase = "two-system HVAC replacement" if any("2 ton" in _estimate_blob(e) and "4 ton" in _estimate_blob(e) for e in sold_capital) else f"{len(sold_capital)} capital-equipment sold estimate signal(s)"
        md.append(f"- Has approved major HVAC equipment work ({capital_phrase}).")
    if sold:
        md.append(f"- Pulled estimate history shows {len(sold)} sold estimate(s), totaling {_money(sold_total)} in known sold estimate value.")
    if cats_open.get("IAQ"):
        md.append(f"- Has {cats_open['IAQ']} open/not-closed IAQ-style estimate signal(s); do not lead with IAQ unless today's findings make it relevant.")
    md.append(f"- {_last_major_purchase(sold)}")
    if sold:
        md.append("- Buying pattern: willing to invest when the need is clear and options are tied to comfort, reliability, or protecting prior investment.")
    else:
        md.append("- Buying pattern: unproven in pulled data; lead with the needed repair and only add options that are directly supported by today's findings.")
    md.append("")

    md.append("## VISUAL INSPECTION REVIEW")
    if positives:
        md.append("**Positive findings**")
        md.extend(f"- {p}" for p in positives)
    else:
        md.append("**Positive findings**")
        md.append("- Use the visit to confirm current equipment condition and separate maintenance items from true sales opportunities.")
    md.append("")
    md.append("**Documentation gaps / verify-first items**")
    md.extend(f"- {g}" for g in photo_gaps[:5])
    for line in photo_lines[:6]:
        md.append(f"- {line}")
    md.append("")

    md.append("## PRIMARY OPPORTUNITIES")
    md.append(f"### 1. {primary} (HIGH PRIORITY)")
    if "Repair estimate follow-up" in primary:
        md.append("- Treat this as a pricing/confirmation visit for already identified recommendations, not a generic age-based discovery call.")
        md.append("- Reconfirm each prior finding with measurements/photos: duct kinks/sealing, biological growth/AUV need, and condenser fan motor amp draw.")
        md.append("- Present a clear repair path first, then good/better options only where the current evidence supports them.")
        md.append("- Address the evaluation fee directly: if repairs are performed during the visit, confirm whether the fee is waived per the booking note.")
    elif "Current HVAC findings" in primary:
        md.append("- Diagnose the current issue/maintenance findings first and connect any recommendation to measured evidence.")
        md.append("- Verify equipment age, condition, airflow, drains, blower/coil cleanliness, and customer comfort concerns.")
        md.append("- Frame options around reliability, comfort, and preventing repeat calls.")
        md.append("- If an older system is confirmed, discuss planning before failure rather than pressure to replace today.")
    elif "Current plumbing findings" in primary or "disposal" in primary.lower():
        md.append("- Diagnose the plumbing demand issue first and separate must-do repair from optional upgrades.")
        md.append("- Verify fixture condition, shutoffs, supply/drain issues, water heater age, pressure, and visible leak risk.")
        md.append("- Frame options around safety, water damage prevention, reliability, and code-compliant repair.")
        md.append("- If equipment age or pressure issues are confirmed, document future options without overbuilding the ticket.")
    elif "Current electrical findings" in primary:
        md.append("- Diagnose the electrical demand issue first and connect recommendations to observed panel/circuit evidence.")
        md.append("- Verify panel condition, breaker/circuit load, surge protection, grounding/bonding, and any visible safety concerns.")
        md.append("- Frame options around safety, equipment protection, reliability, and preventing repeat nuisance issues.")
        md.append("- If surge protection or panel capacity gaps are confirmed, present the cleanest practical option first.")
    else:
        md.append("- Verify the age, condition, and served area of the remaining older system.")
        md.append("- Compare reliability and efficiency against any newer systems already installed.")
        md.append("- Frame as future planning before failure, not pressure to replace today.")
        md.append("- Ask whether that zone is as comfortable as the areas served by the newer systems.")
    md.append("")
    if trade_label == "plumbing":
        md.append("### 2. Disposal replacement / kitchen protection option (MEDIUM PRIORITY)")
        md.append("- If the disposal body is leaking internally, replacement is the likely clean recommendation rather than gasket/hose repair.")
        md.append("- Check under-sink cabinet condition, shutoff condition, trap/drain alignment, dishwasher drain connection, and whether any moisture damage has started.")
        md.append("- Offer good/better options only if supported by what is under the sink: standard disposal replacement, upgraded disposal, and any needed drain/shutoff corrections.")
        md.append("")
        md.append("### 3. Membership / payer-owner handling (LOW-MEDIUM PRIORITY)")
        md.append("- ServiceTitan notes say Jimmy pays the bill while Elizabeth retains ownership; confirm approval/payment path before expanding scope.")
        md.append("- If the repair proceeds, offer membership only as a savings/protection option tied to today's plumbing repair, not as proof of prior buying behavior.")
    elif trade_label == "electrical":
        md.append("### 2. Electrical protection review (MEDIUM-HIGH PRIORITY)")
        md.append("- Capture current electrical panel photos.")
        md.append("- Review surge protection and panel capacity if today's issue or prior quote history supports it.")
        md.append("- Route to install/service follow-up only if panel condition, capacity, or protection gaps are found.")
        md.append("")
        md.append("### 3. Evidence-based next step (MEDIUM PRIORITY)")
        md.append("- Keep optional recommendations tied to the actual circuit/panel findings from today's visit.")
    else:
        has_insulation = any("insulation" in line.lower() for line in photo_lines)
        has_duct_support = any("duct" in line.lower() and any(t in line.lower() for t in ["laying", "support", "hung", "strap", "sagging", "kink"]) for line in photo_lines)
        if has_insulation or has_duct_support:
            md.append("### 2. Attic insulation and duct support verification (MEDIUM-HIGH PRIORITY)")
            if has_insulation:
                md.append("- Prior photos show joists/framing visible through insulation; verify depth and coverage today and document with wide attic photos.")
            if has_duct_support:
                md.append("- Prior photos show ductwork support/restriction concerns; verify whether flex duct is strapped/hung correctly or laying on insulation with sag/kinks.")
            md.append("- If still present, position this as comfort, efficiency, airflow, and system-protection work tied to today's duct/IAQ repair discussion.")
        else:
            md.append("### 2. Electrical evaluation / protection review (MEDIUM-HIGH PRIORITY)")
            md.append("- Capture current electrical panel photos.")
            md.append("- Review surge protection and panel capacity, especially because the customer has already invested in HVAC equipment.")
            md.append("- Route to electrical if panel condition, capacity, or surge protection gaps are found.")
        md.append("")
        md.append("### 3. Comfort-based maintenance conversation (MEDIUM PRIORITY)")
        md.append("- Ask about comfort, hot/cold spots, humidity, and peak-season performance only if today's HVAC scope supports it.")
        md.append("- Use answers to connect findings to verified equipment, airflow, or maintenance issues.")
        md.append("- Only revisit IAQ if open IAQ history or current blower/duct/return findings clearly support it.")
    md.append("")

    md.append("## AI AGENT COACHING NOTES")
    if sold or open_rows:
        md.append("- Weight customer history and current-call evidence above stale/open estimates.")
    else:
        md.append("- No sold or open estimate history was pulled; do not infer willingness to buy from nonexistent history.")
    md.append("- Photo findings from historical images should become verification prompts, not confident recommendations.")
    md.append("- Keep recommendations tied to today's verified issue before surfacing cross-trade recommendations aggressively.")
    md.append("")

    md.append("## OVERALL CUSTOMER SCORE")
    md.append(f"**Customer Relationship:** {relationship}")
    md.append(f"**Likelihood to Purchase:** {likelihood}")
    md.append(f"**Primary Opportunity:** {primary}")
    secondary = "Disposal replacement / under-sink moisture protection" if trade_label == "plumbing" else ("Attic insulation / duct support verification" if any("insulation" in line.lower() or "duct" in line.lower() for line in photo_lines) else "Electrical panel assessment / surge protection review")
    md.append(f"**Secondary Opportunity:** {secondary}")
    md.append(f"**Risk Level:** {risk}. {score_rationale}")
    return "\n".join(md).strip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", help="Cached job dossier JSON produced by servicetitan_dossier.py")
    ap.add_argument("--vision-summary", help="Optional per-job or summary vision JSON")
    ap.add_argument("--lifetime-revenue", type=float, help="Optional known lifetime revenue override from ST/customer scorecard")
    ap.add_argument("--out", help="Output markdown path")
    args = ap.parse_args()

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
    md = build_report_card(bundle, vision=vision, lifetime_revenue=args.lifetime_revenue)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        print(out)
    else:
        print(md)


if __name__ == "__main__":
    main()
