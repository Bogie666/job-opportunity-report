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
