"""Deterministic opportunity-trigger rules shared by the report card and the job scorer.

These are the rules that intentionally stay rules (not LLM judgment):
  - tiered equipment-age replacement thresholds, per equipment class
  - home-age tiers that open cross-trade / replacement-cycle conversations

Each rule emits a structured flag dict:
  {"id", "kind", "label", "age", "threshold", "severity", "source", "text"}
severity: "urgent" (past typical service life) | "flag" (over replacement threshold)
        | "info" (home-age tier trigger).
The renderer prints equipment flags verbatim with the red "⚠ FLAG:" prefix and the
LLM layer receives them as mandatory context it may explain but never alter or drop —
flag text itself is always rendered deterministically by Python, never by the model.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Per-class replacement thresholds. Order matters: first matching class wins, so
# more specific patterns (tankless before generic water heater, furnace before the
# broad HVAC pattern) come first.
EQUIPMENT_AGE_TIERS: list[tuple[str, re.Pattern, str, int, int]] = [
    # (id, pattern on type/name blob, label, flag_years, urgent_years)
    ("tankless_wh", re.compile(r"tankless", re.I), "Tankless water heater", 15, 20),
    ("tank_wh", re.compile(r"water heater|\bwh\b", re.I), "Water heater", 8, 12),
    ("furnace", re.compile(r"furnace", re.I), "Furnace", 15, 20),
    ("hvac", re.compile(
        r"condenser|heat pump|air handler|evap(orator)? coil|\bcoil\b|package unit|rtu|mini[- ]?split|a/?c\b",
        re.I), "HVAC", 10, 15),
]


def _now_year() -> int:
    return datetime.now(timezone.utc).year


def equipment_age_flag(type_blob: str, age_years: int | None, source: str | None = None,
                       display_label: str | None = None) -> dict | None:
    """Tiered replacement flag for one piece of equipment, or None if below threshold.

    `type_blob` is the ST type + name text used for classification; `display_label`
    (e.g. "Furnace (Carrier)") overrides the class label in the rendered text;
    `source` describes where the age came from (installed date / serial decode /
    booking note) so the card shows how trustworthy the trigger is.
    """
    if age_years is None:
        return None
    for rule_id, pattern, label, flag_years, urgent_years in EQUIPMENT_AGE_TIERS:
        if not pattern.search(type_blob or ""):
            continue
        if age_years < flag_years:
            return None
        shown = display_label or label
        src = f" (age from {source})" if source else ""
        if age_years >= urgent_years:
            return {
                "id": rule_id, "kind": "equipment_age", "label": shown, "age": age_years,
                "threshold": urgent_years, "severity": "urgent", "source": source or "",
                "text": (
                    f"⚠ FLAG: {shown} ~{age_years} yrs — past typical {urgent_years}-yr service life; "
                    f"replacement planning is overdue, verify nameplate and lead with replacement options{src}."
                ),
            }
        return {
            "id": rule_id, "kind": "equipment_age", "label": shown, "age": age_years,
            "threshold": flag_years, "severity": "flag", "source": source or "",
            "text": (
                f"⚠ FLAG: {shown} ~{age_years} yrs — over {flag_years}-yr replacement threshold; "
                f"verify nameplate and discuss replacement planning{src}."
            ),
        }
    return None


# (id, min_age, max_age_exclusive, tier label, trigger text)
HOME_AGE_TIERS: list[tuple[str, int, int | None, str, str]] = [
    ("home_first_failure", 10, 15, "10-15 yrs",
     "builder-grade HVAC and water heater are entering their first failure window — "
     "verify equipment ages and open the replacement-planning conversation early."),
    ("home_first_cycle", 15, 30, "15-30 yrs",
     "first full HVAC/water-heater replacement cycle; ductwork and IAQ typically degrade in this window — "
     "verify equipment dates, duct condition, and water heater age."),
    ("home_second_cycle", 30, 45, "30-45 yrs",
     "second HVAC replacement cycle; panel capacity for modern loads, original supply lines, and "
     "thin attic insulation are common findings — a cross-trade walkthrough is high-value."),
    ("home_legacy", 45, None, "45+ yrs",
     "original-era plumbing supply/drain lines, undersized or obsolete electrical panels, and "
     "below-code insulation are likely — a full cross-trade inspection is justified."),
]


def home_age_flags(built_year: int | None, now_year: int | None = None) -> list[dict]:
    """Home-age tier triggers. Returns [] when build year is unknown or home is young."""
    if not built_year:
        return []
    now = now_year or _now_year()
    if built_year < 1700 or built_year > now:
        return []
    age = now - built_year
    for rule_id, lo, hi, tier, trigger in HOME_AGE_TIERS:
        if age >= lo and (hi is None or age < hi):
            return [{
                "id": rule_id, "kind": "home_age", "label": f"Home age tier {tier}",
                "age": age, "threshold": lo, "severity": "info", "source": "home build year",
                "text": f"Home age trigger ({tier}, built {built_year}): {trigger}",
            }]
    return []


def equipment_flag_score(flags: list[dict]) -> int:
    """Scorer points for equipment-age flags: strongest single flag wins."""
    best = 0
    for f in flags:
        if f.get("kind") != "equipment_age":
            continue
        best = max(best, 35 if f.get("severity") == "urgent" else 25)
    return best


def home_age_score(flags: list[dict]) -> int:
    """Scorer points for the home-age tier."""
    points = {"home_first_failure": 5, "home_first_cycle": 5, "home_second_cycle": 10, "home_legacy": 15}
    return max((points.get(f.get("id"), 0) for f in flags if f.get("kind") == "home_age"), default=0)


# ===========================================================================
# Aged-equipment supersession classifier
#
# ServiceTitan equipment records routinely keep replaced units active, so a home
# with a 2024 install often still shows the 2012 unit it replaced — which used to
# fire a false replacement flag. This classifier decides, per old unit, whether it
# was LIKELY REPLACED (stale record), is a GENUINE REMAINING system, or is
# AMBIGUOUS. Nothing is hidden: superseded units render as a verify/record note
# instead of a red flag, ambiguous units render as a softened verify-first flag,
# and only remaining units keep the full flag + scorer points.
#
# Signal ladder (strongest first):
#   1. Zone pairing — old + recent unit of the same component family share a zone
#      token ("Upstairs Condenser") → superseded. Old unit in a zone with NO
#      recent install while other zones were replaced → remaining.
#   2. "# of systems" custom field arithmetic — if recent installs already account
#      for every system on file, leftover old units are stale; if the count leaves
#      room, they are remaining.
#   3. Tonnage pairing — same capacity as a recent same-family install is a
#      probable replacement pair; confirmed (→ superseded) when a sold capital
#      estimate matches the install year, otherwise ambiguous. A different
#      capacity leans remaining.
#   4. Booking-note counter-signal — if the CSR noted an old system age and the
#      ladder demoted every old outdoor unit, the best candidate is pulled back to
#      ambiguous: someone at the house believes an old unit is still running.
# ===========================================================================

RECENT_INSTALL_WINDOW_YEARS = 5

ROLE_FAMILY = {
    "ac-condenser": "outdoor", "heat-pump-condenser": "outdoor",
    "package-unit": "outdoor", "mini-split-outdoor": "outdoor",
    "furnace": "indoor-heat",
    "air-handler": "indoor-air", "air-handler-heatpump": "indoor-air",
    "coil": "coil",
    "mini-split-head": "mini-head",
    "water-heater": "water-heater",
    "electrical-panel": "electrical-panel",
}

# Families the "# of systems" custom field can arbitrate (one unit per system).
_SYSTEM_COUNT_FAMILIES = {"outdoor", "indoor-heat", "indoor-air", "coil", "mini-head"}

# Nominal capacity codes embedded in most HVAC model numbers (kBTU: 1.5–5 ton,
# and 060 doubles as a 60k BTU furnace size — fine for same-family comparison).
_TONNAGE_RE = re.compile(r"(018|024|030|036|042|048|060)")

_YEAR_IN_TEXT_RE = re.compile(r"(20\d{2}|19\d{2})")

_NOTE_AGE_RE = re.compile(
    r"(\d{1,2})\s*(?:yrs?|years?)\s*old|age of the unit\?\s*\d{1,2}|oldest\s+system|other\s+is\s+\d{1,2}\s*years",
    re.I,
)


def _tonnage_from_model(model: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", str(model or ""))
    m = _TONNAGE_RE.search(digits)
    return int(m.group(1)) if m else None


def _n_systems_on_file(dossier: dict) -> int | None:
    for cf in ((dossier.get("location") or {}).get("customFields") or []):
        name = (cf.get("name") or "").lower()
        if "# of systems" in name or ("systems" in name and "of" in name):
            m = re.search(r"\d+", str(cf.get("value") or ""))
            if m and 1 <= int(m.group(0)) <= 12:
                return int(m.group(0))
    return None


def _sold_capital_years(dossier: dict) -> set[int]:
    """Years in which a sold system-replacement/capital estimate closed."""
    from report_card_facts import _estimate_categories
    years: set[int] = set()
    for e in dossier.get("estimates") or []:
        if ((e.get("status") or {}).get("name") or "").lower() != "sold":
            continue
        if "system replacement / capital equipment" in _estimate_categories(e):
            m = _YEAR_IN_TEXT_RE.search(str(e.get("soldOn") or e.get("createdOn") or ""))
            if m:
                years.add(int(m.group(1)))
    return years


def _booking_note_mentions_old_system(dossier: dict) -> bool:
    text = " ".join(str((dossier.get("job") or {}).get(k) or "") for k in ("summary", "summaryOfWork"))
    return bool(_NOTE_AGE_RE.search(text))


def classify_aged_equipment(dossier: dict, now_year: int | None = None) -> dict:
    """Classify every active equipment record for supersession.

    Returns {"units": [...], "by_index": {installed_equipment index: unit},
    "n_systems": int|None, "has_recent": bool}. Each unit carries identity fields
    (label/manufacturer/model/serial), age fields (year/age/age_source), the tier
    flag from equipment_age_flag (or None), and:
      supersession: "superseded" | "remaining" | "ambiguous" | None (not an old unit)
      supersession_reason / superseded_by: human-readable evidence.
    """
    from report_card_facts import _classify_by_model, _classify_by_name, _system_zone_token
    from serial_decoder import decode_serial, unsupported_brand

    now = now_year or _now_year()
    units: list[dict] = []
    for idx, eq in enumerate(dossier.get("installed_equipment") or []):
        if not eq.get("active", True):
            continue
        type_obj = eq.get("type")
        type_name = (type_obj.get("name") if isinstance(type_obj, dict) else type_obj) or eq.get("name") or "Equipment"
        type_name = str(type_name).strip() or "Equipment"
        name = str(eq.get("name") or "")
        mfg = str(eq.get("manufacturer") or "").strip()
        model = str(eq.get("modelNumber") or eq.get("model") or "")
        serial = str(eq.get("serialNumber") or "")
        role = _classify_by_model(model) or _classify_by_name(type_name, name) or "unknown"

        year = None
        age_source = None
        m = _YEAR_IN_TEXT_RE.search(str(eq.get("installedOn") or ""))
        if m and int(m.group(1)) >= 2000:  # 1900/1990 ST placeholder dates = missing
            year = int(m.group(1))
            age_source = f"installed {year}"
        if not year and serial:
            res = decode_serial(mfg, serial)
            if res:
                decoded_year, conf, lbl = res
                if 2000 <= decoded_year <= now:
                    year = decoded_year
                    age_source = f"{lbl} {year}, {conf} confidence"
        age = (now - year) if year else None
        label = f"{type_name} ({mfg})" if mfg else type_name
        units.append({
            "index": idx, "type_name": type_name, "name": name, "label": label,
            "manufacturer": mfg, "model": model, "serial": serial,
            "role": role, "family": ROLE_FAMILY.get(role),
            "zone": _system_zone_token(name), "tonnage": _tonnage_from_model(model),
            "year": year, "age": age, "age_source": age_source,
            "serial_unsupported": bool(serial) and unsupported_brand(mfg),
            "is_recent": bool(year) and year >= now - RECENT_INSTALL_WINDOW_YEARS,
            "aged_flag": equipment_age_flag(f"{type_name} {name}", age, source=age_source, display_label=label),
            "supersession": None, "supersession_reason": None, "superseded_by": None,
        })

    n_systems = _n_systems_on_file(dossier)
    capital_years = _sold_capital_years(dossier)

    def _mark(u: dict, status: str, reason: str, by: dict | None = None) -> None:
        u["supersession"] = status
        u["supersession_reason"] = reason
        if by is not None:
            u["superseded_by"] = f"{by['label']}" + (f" installed {by['year']}" if by.get("year") else "")

    fam_groups: dict[str, list[dict]] = {}
    for u in units:
        if u["family"]:
            fam_groups.setdefault(u["family"], []).append(u)

    for fam, members in fam_groups.items():
        recent = [u for u in members if u["is_recent"]]
        # Old candidates: units past their replacement threshold, plus undated units
        # (old ST records routinely lack dates while the new replacements have them).
        candidates = [u for u in members if not u["is_recent"] and (u["aged_flag"] or u["year"] is None)]
        if not candidates:
            continue
        fam_label = fam.replace("-", " ")
        if not recent:
            for u in candidates:
                _mark(u, "remaining", f"no recent {fam_label} install on file that could have replaced it")
            continue

        # --- 1. zone pairing ---
        recent_zones = {r["zone"] for r in recent if r["zone"]}
        unresolved: list[dict] = []
        for u in candidates:
            if u["zone"] and u["zone"] in recent_zones:
                r = next(r for r in recent if r["zone"] == u["zone"])
                _mark(u, "superseded", f"a recent same-zone {fam_label} install is on file for '{u['zone']}'", by=r)
            elif u["zone"] and recent_zones:
                _mark(u, "remaining", f"recent installs cover other zones ({', '.join(sorted(recent_zones))}), not '{u['zone']}'")
            else:
                unresolved.append(u)
        if not unresolved:
            continue

        capital_near = any(
            r["year"] and any(abs(r["year"] - cy) <= 1 for cy in capital_years) for r in recent
        )

        # --- 2. system-count arithmetic ---
        slots = (n_systems - len(recent)) if (n_systems and fam in _SYSTEM_COUNT_FAMILIES) else None
        if slots is not None:
            if slots <= 0:
                newest = max(recent, key=lambda r: r["year"] or 0)
                for u in unresolved:
                    _mark(u, "superseded", f"all {n_systems} system(s) on file are accounted for by recent installs", by=newest)
            elif len(unresolved) <= slots:
                for u in unresolved:
                    _mark(u, "remaining", f"the {n_systems}-system count on file leaves room for a remaining older unit beyond the {len(recent)} recent install(s)")
            else:
                # More old units than open slots: demote capacity-matched pairs first.
                for u in unresolved:
                    if u["tonnage"] and any(r["tonnage"] == u["tonnage"] for r in recent):
                        r = next(r for r in recent if r["tonnage"] == u["tonnage"])
                        _mark(u, "superseded", "same capacity as a recent install and the system count cannot support all old units", by=r)
                rest = [u for u in unresolved if u["supersession"] is None]
                status = "remaining" if len(rest) <= slots else "ambiguous"
                for u in rest:
                    _mark(u, status, f"records show more old {fam_label} units than the {n_systems}-system count supports; cannot tell which were replaced")
            continue

        # --- 3. tonnage pairing (no system count available) ---
        recent_tons = [r["tonnage"] for r in recent if r["tonnage"]]
        for u in unresolved:
            ton_match = bool(u["tonnage"]) and u["tonnage"] in recent_tons
            all_recent_tons_known = bool(recent_tons) and len(recent_tons) == len(recent)
            if ton_match and capital_near:
                r = next(r for r in recent if r["tonnage"] == u["tonnage"])
                _mark(u, "superseded", "same capacity as a recent install and a sold system-replacement estimate matches the install date", by=r)
            elif ton_match:
                _mark(u, "ambiguous", "a recent install with the same capacity is on file and may be its replacement")
            elif u["tonnage"] and all_recent_tons_known:
                _mark(u, "remaining", "different capacity than the recent install(s) — likely a separate system")
            else:
                _mark(u, "ambiguous", "a recent same-type install is on file but records cannot confirm which unit it replaced")

    # --- 4. booking-note counter-signal: if the CSR noted an old system age but the
    # ladder demoted every old outdoor unit, pull the best candidate back to
    # ambiguous — someone at the house believes an old unit is still running.
    outdoor_candidates = [
        u for u in units
        if u["family"] == "outdoor" and not u["is_recent"] and (u["aged_flag"] or u["year"] is None)
    ]
    if outdoor_candidates and _booking_note_mentions_old_system(dossier):
        if all(u["supersession"] == "superseded" for u in outdoor_candidates):
            best = min(outdoor_candidates, key=lambda u: (u["year"] is not None, u["year"] or 0))
            _mark(
                best, "ambiguous",
                f"booking notes mention an older system still in service, contradicting records ({best['supersession_reason']})"
                if best["supersession_reason"] else "booking notes mention an older system still in service",
            )

    return {
        "units": units,
        "by_index": {u["index"]: u for u in units},
        "n_systems": n_systems,
        "has_recent": any(u["is_recent"] for u in units),
    }


def classified_equipment_score(classification: dict) -> tuple[int, str, int]:
    """Scorer points honoring supersession: remaining aged units score full tier
    points (urgent 35 / flag 25, strongest single wins), ambiguous score 10,
    likely-superseded score 0. Returns (points, driver_note, stale_aged_count)."""
    best, note, stale = 0, "", 0
    for u in classification.get("units") or []:
        f = u.get("aged_flag")
        if u.get("supersession") == "superseded":
            if f:
                stale += 1
            continue
        if not f:
            continue
        if u.get("supersession") == "ambiguous":
            pts = 10
        else:
            pts = 35 if f.get("severity") == "urgent" else 25
        if pts > best:
            best = pts
            tag = u.get("supersession") or "aged"
            note = f"{f['label']} {f['age']}y ({tag}, +{pts})"
    return best, note, stale
