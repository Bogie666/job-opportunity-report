"""Rebuild lean technician briefs from cached dossier JSON files.

Format:
  - Header (job type, customer, address, when)
  - Tech read (AI paragraph + Go win the call bullets)
  - Key facts (5-8 bullets max)
  - Opportunities (3-5 bullets, AI-extracted from history+memberships+equipment)
  - Heads up (only if relevant flags exist)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

try:
    from cad_home_age import lookup_free_cad
except Exception:  # Keep brief rebuild resilient if scraper deps/site are unavailable.
    lookup_free_cad = None

CENTRAL = timezone(timedelta(hours=-5))


def load_env(path: str, *, override: bool = False) -> None:
    if not os.path.exists(path):
        return
    for raw in open(path, errors="ignore"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


load_env("/workspace/openclaw/MOVING/credentials/MASTER.env")

HTML_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = HTML_TAG.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def fmt_appt_local(appt: dict | None) -> str:
    if not appt:
        return "Unscheduled"
    try:
        start = datetime.fromisoformat(appt["start"].replace("Z", "+00:00")).astimezone(CENTRAL)
        ws = datetime.fromisoformat(appt["arrivalWindowStart"].replace("Z", "+00:00")).astimezone(CENTRAL)
        we = datetime.fromisoformat(appt["arrivalWindowEnd"].replace("Z", "+00:00")).astimezone(CENTRAL)
        return f"{start.strftime('%a %b %d, %Y')} · arrival window {ws.strftime('%I:%M %p').lstrip('0')} – {we.strftime('%I:%M %p').lstrip('0')} CT"
    except Exception:
        return appt.get("start", "")


def years_old(install_iso: str | None) -> int | None:
    if not install_iso or install_iso[:4] in ("0001", "1900", "1990"):
        return None
    try:
        d = datetime.fromisoformat(install_iso.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - d).days // 365)
    except Exception:
        return None


def _system_zone_token(name: str) -> str | None:
    """Extract a system-pairing zone from a unit name.
    'Upstairs Condenser' / 'Upstairs Heat Pump/Air Handler' → 'upstairs'.
    Returns None if no clear zone keyword.
    """
    if not name:
        return None
    nl = name.lower()
    # Word-boundary match so 'den' doesn't match 'condenser', etc.
    for tok in ("upstairs", "downstairs", "main", "master", "bedroom", "garage", "attic",
                "first floor", "1st floor", "second floor", "2nd floor",
                "north", "south", "east", "west", "front", "back", "office", "shop", "barn",
                "guest", "bonus", "den", "living"):
        if re.search(rf"\b{re.escape(tok)}\b", nl):
            return tok
    # System number ("System 1", "Unit 2", "RTU #3")
    m = re.search(r"\b(?:system|unit|rtu|ahu)\s*#?\s*(\d+)", nl)
    if m:
        return f"unit-{m.group(1)}"
    return None


# Model-number prefix classifier: prefix → component role
# Sources: Goodman/Daikin/Carrier/Trane/Lennox/Mitsubishi/Fujitsu/Rheem published nomenclature.
# IMPORTANT: same manufacturer reuses short prefixes across product lines (e.g. Lennox ML14XC = AC,
# but ML180UH = furnace). The matcher uses LONGEST-PREFIX-WINS, so add the most-specific patterns.
MODEL_PREFIXES: list[tuple[str, str]] = [
    # --- Lennox / Armstrong / Ducane — disambiguated ---
    ("ML14XC", "ac-condenser"),
    ("ML17XC", "ac-condenser"),
    ("ML14XP", "heat-pump-condenser"),
    ("ML17XP", "heat-pump-condenser"),
    ("ML180",  "furnace"),
    ("ML193",  "furnace"),
    ("ML195",  "furnace"),
    ("ML196",  "furnace"),
    ("ML296",  "furnace"),
    ("ML80",   "furnace"),
    ("EL14XC", "ac-condenser"),
    ("EL15XC", "ac-condenser"),
    ("EL16XC", "ac-condenser"),
    ("EL16XP", "heat-pump-condenser"),
    ("EL180",  "furnace"),
    ("EL195",  "furnace"),
    ("EL196",  "furnace"),
    ("EL280",  "furnace"),
    ("EL296",  "furnace"),
    ("XC13", "ac-condenser"), ("XC14", "ac-condenser"), ("XC16", "ac-condenser"),
    ("XC17", "ac-condenser"), ("XC20", "ac-condenser"), ("XC21", "ac-condenser"), ("XC25", "ac-condenser"),
    ("XP13", "heat-pump-condenser"), ("XP14", "heat-pump-condenser"), ("XP15", "heat-pump-condenser"),
    ("XP16", "heat-pump-condenser"), ("XP17", "heat-pump-condenser"), ("XP19", "heat-pump-condenser"),
    ("XP20", "heat-pump-condenser"), ("XP21", "heat-pump-condenser"), ("XP25", "heat-pump-condenser"),
    ("SL18XC", "ac-condenser"), ("SL28XC", "ac-condenser"),
    ("SL280",  "furnace"), ("SL295", "furnace"), ("SL297", "furnace"), ("SLP98", "furnace"), ("SLP99", "furnace"),
    ("CBA25", "air-handler"), ("CBA27", "air-handler"), ("CBA32", "air-handler"), ("CBA38", "air-handler"),
    ("CBX25", "air-handler"), ("CBX27", "air-handler"), ("CBX32", "air-handler"), ("CBX40", "air-handler"),
    ("CB30M", "air-handler"),
    ("CHX", "coil"),     # Lennox cased coil (CHX35, CHX24)
    ("LH", "coil"),      # Lennox LH coil
    # --- Goodman / Amana / Daikin family ---
    ("DSXC", "ac-condenser"),
    ("DSZC", "heat-pump-condenser"),
    ("DSX",  "ac-condenser"),
    ("DSH",  "heat-pump-condenser"),
    ("DZ16", "heat-pump-condenser"),
    ("DZ18", "heat-pump-condenser"),
    ("DZ",   "heat-pump-condenser"),
    ("DX",   "heat-pump-condenser"),
    ("GSZ",  "heat-pump-condenser"),
    ("ASZ",  "heat-pump-condenser"),
    ("GSX",  "ac-condenser"),
    ("ASX",  "ac-condenser"),
    ("GLXS", "ac-condenser"),
    ("ANX",  "ac-condenser"),
    ("ARUF", "air-handler"),
    ("AVPT", "air-handler"),
    ("AWUF", "air-handler"),
    ("APH",  "air-handler"),
    ("ASPT", "air-handler"),
    ("MBR",  "air-handler"),
    ("MBVC", "air-handler"),
    ("GMVC", "furnace"), ("GMSS", "furnace"), ("GMES", "furnace"), ("GMS9", "furnace"),
    ("GCSS", "furnace"), ("GCVC", "furnace"), ("ACVC", "furnace"), ("AMS",  "furnace"),
    ("CHPF", "coil"), ("CAPF", "coil"), ("EEM", "coil"), ("CSCF", "coil"),
    # --- Carrier / Bryant / Payne ---
    ("25HCC", "heat-pump-condenser"),
    ("25HCB", "heat-pump-condenser"),
    ("25VNA", "heat-pump-condenser"),
    ("25H",   "heat-pump-condenser"),
    ("25V",   "heat-pump-condenser"),
    ("24ACC", "ac-condenser"),
    ("24AAA", "ac-condenser"),
    ("24ANB", "ac-condenser"),
    ("24A",   "ac-condenser"),
    ("FV4",   "air-handler"), ("FX4", "air-handler"), ("FE4", "air-handler"),
    ("58S",   "furnace"), ("58C", "furnace"), ("58T", "furnace"), ("58M", "furnace"),
    # --- Trane / American Standard ---
    ("4TWR", "heat-pump-condenser"),
    ("4TWZ", "heat-pump-condenser"),
    ("4A7",  "ac-condenser"),
    ("4A6",  "ac-condenser"),
    ("TTA",  "ac-condenser"),
    ("TWE",  "air-handler"), ("TAM", "air-handler"),
    ("TUD",  "furnace"), ("TUH", "furnace"), ("TDC", "furnace"),
    # --- ADP coils ---
    ("A36H", "coil"), ("A60H", "coil"),
    # --- Mitsubishi / Fujitsu / Daikin mini-splits ---
    ("MSZ", "mini-split-head"),
    ("MSY", "mini-split-head"),
    ("MUZ", "mini-split-outdoor"),
    ("MXZ", "mini-split-outdoor"),
    ("PUZ", "mini-split-outdoor"),
    ("PUY", "mini-split-outdoor"),
    ("ASU", "mini-split-head"),
    ("AOU", "mini-split-outdoor"),
    ("RXB", "mini-split-outdoor"),
    ("RXS", "mini-split-outdoor"),
    ("FTX", "mini-split-head"),
    ("CTX", "mini-split-head"),
    # --- Rheem / Ruud ---
    ("RA",  "ac-condenser"),
    ("RP",  "heat-pump-condenser"),
    ("RH",  "air-handler"),
    ("R96", "furnace"), ("R80", "furnace"),
    # --- Package units (commercial) ---
    ("RTU", "package-unit"),
    ("DSC", "package-unit"),
    ("DSG", "package-unit"),
    ("WPH", "package-unit"),
    ("WCH", "package-unit"),
    ("WSH", "package-unit"),
]


def _classify_by_model(model_or_serial: str) -> str | None:
    if not model_or_serial:
        return None
    m = re.sub(r"\s+", "", model_or_serial).strip().upper()  # strip internal spaces
    # Longest matching prefix wins
    best_role = None
    best_len = 0
    for prefix, role in MODEL_PREFIXES:
        if m.startswith(prefix) and len(prefix) > best_len:
            best_role = role
            best_len = len(prefix)
    return best_role


def _classify_by_name(type_field: str, name_field: str) -> str | None:
    """Layer-2 fallback: classify from ST type + name text."""
    blob = f"{type_field or ''} {name_field or ''}".lower()
    if "mini" in blob and "split" in blob:
        if "head" in blob or "indoor" in blob: return "mini-split-head"
        if "outdoor" in blob or "condens" in blob: return "mini-split-outdoor"
        return "mini-split"
    if "package" in blob or "rtu" in blob or "rooftop" in blob:
        return "package-unit"
    # "Heat Pump / Air Handler" combo unit (e.g. air-handler that pairs with HP condenser)
    if ("heat pump" in blob or "heatpump" in blob) and ("air handler" in blob or "ah " in blob or "/" in name_field):
        return "air-handler-heatpump"
    if "heat pump" in blob or "heatpump" in blob:
        # Distinguish HP condenser (outdoor) vs. HP interior
        if "condens" in blob or "outdoor" in blob:
            return "heat-pump-condenser"
        return "heat-pump-condenser"  # default — most ST "heatpump" entries are outdoor units
    if "air handler" in blob or " ah " in blob or blob.startswith("ah "):
        return "air-handler"
    if "furn" in blob:
        return "furnace"
    if "evaporator" in blob or "evap" in blob or "coil" in blob:
        return "coil"
    if "condens" in blob:
        return "ac-condenser"
    if "thermo" in blob:
        return "thermostat"
    if "water heater" in blob or "tankless" in blob:
        return "water-heater"
    if "panel" in blob or "breaker" in blob:
        return "electrical-panel"
    return None


# How each component contributes to system counts
SYSTEM_DEFINING_COMPONENTS = {
    "heat-pump-condenser":  "heat-pump",
    "ac-condenser":         "ac",
    "package-unit":         "package-unit",
    "mini-split-outdoor":   "mini-split",
}


def summarize_equipment(equipment: list[dict]) -> dict:
    """Return high-level summary: components, system rollup, and aging units.

    Uses a 3-layer classifier:
      1. Model-number prefix (most reliable).
      2. ST type + name text fallback.
      3. Component-to-system pairing by zone keyword in the name.
    """
    # Layer 1+2: classify every active component
    components: list[dict] = []
    for eq in equipment:
        if not eq.get("active"):
            continue
        type_field = eq.get("type")
        if isinstance(type_field, dict):
            type_field = type_field.get("name") or ""
        name_field = eq.get("name") or ""
        model = eq.get("modelNumber") or eq.get("model") or ""

        role = _classify_by_model(model) or _classify_by_name(type_field, name_field)
        zone = _system_zone_token(name_field)
        age = years_old(eq.get("installedOn"))
        components.append({
            "role": role or "unknown",
            "name": name_field.strip(),
            "type_field": (type_field or "").strip(),
            "model": model,
            "zone": zone,
            "age": age,
            "mfg": eq.get("manufacturer") or "",
        })

    # Layer 3: system pairing
    # Group by zone. Each zone with at least one outdoor unit = 1 system.
    # Indoor components (air handler / coil / furnace) attach to a system but don't multiply the count.
    OUTDOOR = {"heat-pump-condenser", "ac-condenser", "package-unit", "mini-split-outdoor"}
    systems: list[dict] = []
    zoned_outdoor = [c for c in components if c["zone"] and c["role"] in OUTDOOR]
    unzoned_outdoor = [c for c in components if not c["zone"] and c["role"] in OUTDOOR]

    used_indoor_ids: set[int] = set()
    for outdoor in zoned_outdoor:
        zone = outdoor["zone"]
        # Find indoor partners in the same zone
        partners = [c for c in components if c["zone"] == zone and c["role"] not in OUTDOOR and c["role"] != "unknown"]
        systems.append({"zone": zone, "outdoor": outdoor, "indoor": partners})
    # Unzoned outdoors: distribute unzoned indoor components evenly (1 air handler / furnace / coil per outdoor)
    unzoned_indoor = [c for c in components if not c["zone"] and c["role"] not in OUTDOOR and c["role"] != "unknown" and c["role"] != "thermostat"]
    for outdoor in unzoned_outdoor:
        # Pick at most one indoor of each role, removing as we go
        partners = []
        for role_needed in ("furnace", "air-handler", "air-handler-heatpump", "coil"):
            for c in unzoned_indoor:
                if c["role"] == role_needed and id(c) not in used_indoor_ids:
                    partners.append(c)
                    used_indoor_ids.add(id(c))
                    break
        systems.append({"zone": None, "outdoor": outdoor, "indoor": partners})

    # Determine system label for each
    def _sys_label(sys: dict) -> str:
        role = sys["outdoor"]["role"]
        if role == "heat-pump-condenser":
            return "Heat Pump System"
        if role == "ac-condenser":
            # If paired with a furnace indoor, it's split AC + furnace; otherwise AC + air handler
            has_furnace = any(p["role"] == "furnace" for p in sys["indoor"])
            return "AC + Furnace" if has_furnace else "AC System"
        if role == "package-unit":
            return "Package Unit (RTU)"
        if role == "mini-split-outdoor":
            return "Mini-Split System"
        return "HVAC System"

    # Build human-readable rollup
    system_rollup: list[str] = []
    sys_label_counts: dict[str, list[int]] = {}
    for s in systems:
        label = _sys_label(s)
        age = s["outdoor"]["age"]
        sys_label_counts.setdefault(label, []).append(age if age is not None else -1)

    for label, ages in sys_label_counts.items():
        valid = [a for a in ages if a >= 0]
        if valid:
            mn, mx = min(valid), max(valid)
            age_str = f"~{mn}y" if mn == mx else f"~{mn}-{mx}y"
            system_rollup.append(f"{len(ages)} × {label} ({age_str})")
        else:
            system_rollup.append(f"{len(ages)} × {label}")

    # Aging-unit list (for cross-trade signals)
    aging_units: list[str] = []
    for c in components:
        if c["age"] is not None and c["age"] >= 10:
            role_label = c["role"].replace("-", " ").title()
            aging_units.append(f"{role_label} {c['mfg']} {c['model']}".strip() + f" (~{c['age']}y)")

    # Detail breakdown: list non-redundant components for the brief's "Equipment" line
    # We want the SYSTEM rollup as the main line, and a secondary "components" line if helpful
    def _unknown_component_label(type_field: str) -> str:
        cleaned = (type_field or "").strip()
        if not cleaned or cleaned.lower() in {"equipment", "hvac equipment", "installed equipment", "unknown", "none", "n/a"}:
            return "Unclassified component (verify on arrival)"
        return cleaned

    component_counts: dict[str, list[int]] = {}
    for c in components:
        role_label = {
            "heat-pump-condenser": "Heat Pump Condenser",
            "ac-condenser":        "A/C Condenser",
            "air-handler":         "Air Handler",
            "air-handler-heatpump":"Air Handler (HP)",
            "furnace":             "Furnace",
            "coil":                "Coil",
            "package-unit":        "Package Unit",
            "mini-split-outdoor":  "Mini-Split Outdoor",
            "mini-split-head":     "Mini-Split Head",
            "mini-split":          "Mini-Split",
            "thermostat":          "Thermostat",
            "water-heater":        "Water Heater",
            "electrical-panel":    "Electrical Panel",
            "unknown":             _unknown_component_label(c["type_field"]),
        }.get(c["role"], c["role"])
        component_counts.setdefault(role_label, []).append(c["age"] if c["age"] is not None else -1)

    component_rollup: list[str] = []
    for key, ages in component_counts.items():
        valid = [a for a in ages if a >= 0]
        if valid:
            mn, mx = min(valid), max(valid)
            age_str = f"~{mn}y" if mn == mx else f"~{mn}-{mx}y"
            component_rollup.append(f"{len(ages)} × {key} ({age_str})")
        else:
            component_rollup.append(f"{len(ages)} × {key}")

    # Reconciliation safety: if the component evidence shows every condenser has a furnace,
    # the system rollup must say AC + Furnace for all of them. This catches unzoned pairing
    # misses where coils consumed slots before furnaces and produced "2 × AC System, 1 × AC + Furnace".
    ac_ages = [c["age"] if c["age"] is not None else -1 for c in components if c["role"] == "ac-condenser"]
    furnace_count = sum(1 for c in components if c["role"] == "furnace")
    coil_count = sum(1 for c in components if c["role"] == "coil")
    if ac_ages and furnace_count >= len(ac_ages) and coil_count >= len(ac_ages):
        valid = [a for a in ac_ages if a >= 0]
        if valid:
            mn, mx = min(valid), max(valid)
            age_str = f" (~{mn}y)" if mn == mx else f" (~{mn}-{mx}y)"
        else:
            age_str = ""
        system_rollup = [f"{len(ac_ages)} × AC + Furnace{age_str}"]

    return {
        "counts": system_rollup or component_rollup,    # main "Equipment" line — system view if we have one
        "components": component_rollup,                  # fallback / supplemental detail
        "systems": systems,                              # raw structured systems for downstream consumers
        "aging": aging_units,
    }


def summarize_memberships(memberships: list[dict]) -> dict:
    active = [m for m in memberships if (m.get("status") or "").lower() == "active"]
    canceled = [m for m in memberships if (m.get("status") or "").lower() == "canceled"]
    expired = [m for m in memberships if (m.get("status") or "").lower() == "expired"]
    suspended = [m for m in memberships if (m.get("status") or "").lower() == "suspended"]
    return {
        "active": active,
        "canceled": canceled,
        "expired": expired,
        "suspended": suspended,
    }


def membership_one_liner(ms: dict) -> str:
    if ms["active"]:
        m = ms["active"][0]
        return f"Active {m.get('billingFrequency','')} membership (since {(m.get('from') or '')[:10]})"
    if ms["suspended"]:
        return f"Suspended membership · {len(ms['canceled'])} canceled, {len(ms['expired'])} expired on file"
    if ms["expired"] or ms["canceled"]:
        bits = []
        if ms["expired"]:
            last = sorted(ms["expired"], key=lambda m: (m.get("to") or ""), reverse=True)[0]
            bits.append(f"last expired {(last.get('to') or '')[:10]}")
        if ms["canceled"]:
            last = sorted(ms["canceled"], key=lambda m: (m.get("cancellationDate") or ""), reverse=True)[0]
            bits.append(f"last canceled {(last.get('cancellationDate') or '')[:10]}")
        return "No active membership · " + ", ".join(bits)
    return "No membership on file"


def recent_meaningful_notes(notes: list[dict], limit: int = 8) -> list[str]:
    out = []
    # Reschedule/date-fragment patterns that the LLM has hallucinated as appointment dates
    DATE_FRAG_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:\s*[-–]\s*(?:rs|reschedule|moved|no show)\s*[-–]\s*\d{1,2}/\d{1,2})+\b", re.I)
    for n in sorted(notes, key=lambda n: n.get("createdOn") or "", reverse=True):
        text = strip_html(n.get("text"))
        if not text:
            continue
        low = text.lower()
        if "broccoli ai outbound" in low and ("voicemail left" in low or "outbound sms" in low):
            continue
        if low.startswith("https://") and len(text) < 120:
            continue
        # Scrub reschedule-style date fragments so LLM cannot grab them as the appt date
        text = DATE_FRAG_RE.sub("[reschedule history]", text)
        out.append(f"{(n.get('createdOn') or '')[:10]}: {text}")
        if len(out) >= limit:
            break
    return out


def past_visit_lines(past_jobs: list[dict], limit: int = 6) -> list[str]:
    rows = []
    for j in past_jobs[:limit]:
        when = (j.get("completedOn") or j.get("createdOn") or "")[:10]
        status = j.get("jobStatus") or ""
        summary = strip_html(j.get("summary") or j.get("summaryOfWork") or "")
        if len(summary) > 220:
            summary = summary[:220] + "…"
        rows.append(f"{when} ({status}): {summary}")
    return rows


def _title_zone(zone: str | None) -> str:
    if not zone:
        return ""
    return zone.replace("unit-", "unit ").replace("1st", "first").replace("2nd", "second").title()


def _equipment_model_zone_map(dossier: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for eq in dossier.get("installed_equipment") or []:
        if not eq.get("active"):
            continue
        zone = _system_zone_token(eq.get("name") or "")
        model = (eq.get("modelNumber") or eq.get("model") or "").strip().upper()
        if zone and model:
            out[re.sub(r"\s+", "", model)] = zone
    return out


def _system_scope_from_text(text: str, dossier: dict | None = None) -> str:
    zone = _system_zone_token(text or "")
    if not zone and dossier:
        compact = re.sub(r"\s+", "", (text or "").upper())
        for model, model_zone in _equipment_model_zone_map(dossier).items():
            if model and model in compact:
                zone = model_zone
                break
    return f"{_title_zone(zone)} system" if zone else "system not specified, verify"


def system_scoped_history(dossier: dict, limit: int = 6) -> list[str]:
    """Facts with system/zone attribution when ST text or model linkage supports it."""
    out: list[str] = []
    for j in dossier.get("past_jobs") or []:
        summary = strip_html(j.get("summary") or j.get("summaryOfWork") or "")
        low = summary.lower()
        if any(k in low for k in ("coil", "evap", "warranty", "iaq", "duct")):
            scope = _system_scope_from_text(summary, dossier)
            out.append(f"Past job {(j.get('completedOn') or j.get('createdOn') or '')[:10]}: {summary[:140]} · {scope}")
        if len(out) >= limit:
            break
    for e in dossier.get("estimates") or []:
        status = ((e.get("status") or {}).get("name") or "").upper()
        if status not in {"SOLD", "OPEN", "EXPIRED", "DISMISSED"}:
            continue
        blob = (e.get("name") or "") + " " + " ".join((r.get("text") or "") for r in _estimate_line_items(e))
        scope = _system_scope_from_text(blob, dossier)
        if any(k in blob.lower() for k in ("coil", "evap", "iaq", "air purifier", "merv", "polarized", "uv")):
            out.append(f"{status} Est #{e.get('id')}: {e.get('name') or 'estimate'} · ${float(e.get('subtotal') or 0):,.0f} · {scope}")
        if len(out) >= limit:
            break
    return out[:limit]


def history_anchored_opener(dossier: dict) -> str:
    """One sayable line backed by a real prior job/estimate; empty if no usable history."""
    jobs = dossier.get("past_jobs") or []
    for j in jobs:
        summary = strip_html(j.get("summary") or j.get("summaryOfWork") or "")
        low = summary.lower()
        if "coil" in low or "evap" in low:
            scope = _system_scope_from_text(summary, dossier)
            scope_phrase = "" if scope.startswith("system not") else f" on the {scope.replace(' system','').lower()} system"
            return f"Opener: 'Last time we were out, we replaced/handled the coil{scope_phrase}; I’ll use today’s visit to see what that opens up for comfort and air quality.'"
    for j in jobs:
        summary = strip_html(j.get("summary") or j.get("summaryOfWork") or "")
        low = summary.lower()
        if "water heater" in low or "tankless" in low:
            return "Opener: 'Last plumbing visit, we looked at the tankless/water-heater side; I’ll verify what’s on file today before reopening any water-quality or heater options.'"
        if summary.strip():
            first = summary.strip()[:90]
            return f"Opener: 'I saw our last visit notes: {first}. I’ll use that history to make today’s recommendations specific.'"
    for e in dossier.get("estimates") or []:
        status = ((e.get("status") or {}).get("name") or "").lower()
        if status in {"open", "expired", "dismissed", "sold"}:
            verb = {"open": "quoted", "expired": "quoted", "dismissed": "reviewed", "sold": "completed"}.get(status, "reviewed")
            return f"Opener: 'I saw we previously {verb} Est #{e.get('id')} ({e.get('name') or 'estimate'}); I’ll make today’s options line up with that history.'"
    return ""


def build_facts_block(dossier: dict) -> dict:
    job = dossier["job"]
    customer = dossier.get("customer") or {}
    location = dossier.get("location") or {}
    appt = dossier.get("appointment")
    addr = location.get("address") or {}
    address = ", ".join(b for b in [addr.get("street"), addr.get("city"), addr.get("state"), addr.get("zip")] if b)

    home_age = ""
    home_age_source = "ServiceTitan"
    n_systems = ""
    for cf in (location.get("customFields") or []):
        name = (cf.get("name") or "").lower()
        val = cf.get("value")
        if not val:
            continue
        # LEX has used variants: "Age of Home", "Age of HOME:", "Year Built".
        if "age of home" in name or "year built" in name or name.strip(": ") == "year built":
            home_age = str(val)
        elif "# of systems" in name or "systems" in name and "of" in name:
            n_systems = str(val)

    flags = []

    # Free CAD resolver: cache-first, no paid API. Use it to fill missing ST home age and verify ST values.
    cad_home_age = None
    if lookup_free_cad and address and os.environ.get("LEX_DISABLE_FREE_CAD_HOME_AGE", "").lower() not in ("1", "true", "yes"):
        try:
            cad_res = lookup_free_cad(address, use_cache=True)
            if cad_res.status == "found" and cad_res.year_built:
                cad_home_age = cad_res.year_built
                cad_source_label = {
                    "dallas_cad": "Dallas CAD",
                    "collin_cad_open_data": "Collin CAD open data",
                }.get(cad_res.source, cad_res.source.replace("_", " ").title())
                try:
                    st_year = int(re.search(r"\d{4}", home_age).group(0)) if home_age else None
                except Exception:
                    st_year = None
                if not st_year:
                    home_age = str(cad_home_age)
                    home_age_source = cad_source_label
                elif st_year == cad_home_age:
                    home_age_source = f"ServiceTitan, {cad_source_label} verified"
                else:
                    home_age = str(cad_home_age)
                    home_age_source = f"{cad_source_label}; ServiceTitan says {st_year}"
                    flags.append(f"DATA QUALITY: ServiceTitan home age ({st_year}) differs from {cad_source_label} year built ({cad_home_age}). Using CAD year for age-based cross-sell logic.")
        except Exception as exc:
            flags.append(f"CAD home-age lookup unavailable: {exc}")

    eq_sum = summarize_equipment(dossier.get("installed_equipment") or [])
    ms_sum = summarize_memberships(dossier.get("memberships") or [])

    last_visit = ""
    past = dossier.get("past_jobs") or []
    # Prefer the most recent COMPLETED visit; fall back to any past job
    completed = [j for j in past if (j.get("jobStatus") or "").lower() == "completed" and j.get("completedOn")]
    src_visit = completed[0] if completed else (past[0] if past else None)
    if src_visit:
        last_visit = f"{(src_visit.get('completedOn') or src_visit.get('createdOn') or '')[:10]} · {strip_html(src_visit.get('summary') or '')[:90]}"

    if customer.get("doNotService"):
        flags.append("DO NOT SERVICE flag on customer")
    bal = float(customer.get("balance") or 0)
    if bal:
        flags.append(f"Account balance: ${bal}")
    if appt and appt.get("specialInstructions"):
        si = strip_html(appt.get("specialInstructions"))
        if si:
            flags.append(f"Special instructions: {si}")
    if "elderly" in (strip_html(job.get("summary") or "") + " " + " ".join(strip_html(n.get("text") or "") for n in (dossier.get("location_notes") or []) + (dossier.get("customer_notes") or []))).lower():
        flags.append("Customer noted as elderly in past notes — call ahead, plan extra access time")

    # Commercial detection — multiple weak signals OR'd
    cust_name = (customer.get("name") or "").lower()
    cust_type = (customer.get("type") or "").lower()
    bu_blob = " ".join((cf.get("name") or "") for cf in (location.get("customFields") or []) if cf).lower()
    is_commercial = (
        cust_type in ("commercial", "business")
        or "billing account" in cust_name
        or "commercial" in (job.get("businessUnitName") or "").lower()
        or any(eq.get("type") and "rtu" in (eq.get("type", {}).get("name") if isinstance(eq.get("type"), dict) else str(eq.get("type") or "")).lower() for eq in (dossier.get("installed_equipment") or []))
        or any("package unit" in str((eq.get("type") or {}).get("name") if isinstance(eq.get("type"), dict) else eq.get("type") or "").lower() for eq in (dossier.get("installed_equipment") or []))
    )

    # Equipment data-quality flag: if any active equipment install year predates the home build year
    try:
        home_built_year = int(re.search(r"\d{4}", home_age).group(0))
    except Exception:
        home_built_year = None
    if home_built_year:
        stale_equipment_dates = []
        for eq in dossier.get("installed_equipment") or []:
            if not eq.get("active"):
                continue
            inst = eq.get("installedOn") or ""
            try:
                eq_year = int(inst[:4])
            except Exception:
                continue
            if eq_year and eq_year < home_built_year and eq_year > 1990:
                etype = (eq.get("type") or {}).get("name") if isinstance(eq.get("type"), dict) else (eq.get("type") or eq.get("name") or "")
                stale_equipment_dates.append(f"{etype} ({eq_year})")
        if stale_equipment_dates:
            flags.append(
                f"DATA QUALITY: equipment install dates predate the home build year ({home_built_year}) — {', '.join(stale_equipment_dates[:3])}. "
                "Treat equipment ages as suspect, verify nameplate on-site."
            )

    return {
        "customer": customer.get("name") or "",
        "address": address,
        "appt": fmt_appt_local(appt),
        "home_age": home_age,
        "home_age_source": home_age_source if home_age else "",
        "cad_home_built_year": cad_home_age,
        "n_systems": n_systems,
        "equipment_counts": eq_sum["counts"],
        "equipment_components": eq_sum.get("components") or [],
        "aging_units": eq_sum["aging"],
        "membership": membership_one_liner(ms_sum),
        "membership_detail": ms_sum,
        "last_visit": last_visit,
        "history_opener": history_anchored_opener(dossier),
        "system_scoped_history": system_scoped_history(dossier),
        "flags": flags,
        "reason": strip_html(job.get("summary") or ""),
        "is_commercial": is_commercial,
        "home_built_year": home_built_year,
    }


def cluster_opportunities(estimates: list[dict]) -> list[dict]:
    """Group estimates into ServiceTitan-style 'opportunities' (Good/Better/Best ladders).

    Strategy:
      1. Primary: ST projectId when populated (non-zero).
      2. Fallback for projectId == 0/None: cluster by (createdOn within 24h) AND
         adjacent estimate IDs (within ±10 of an existing cluster member) AND
         at least one tier keyword in any member's name.

    Returns a list of cluster dicts: {opportunity_id, members, sold_members, open_members,
    dismissed_members, status (won|open|dismissed|mixed), total_open_dollars,
    sold_dollars, anchor_estimate}.
    """
    from collections import defaultdict
    TIER_RE = re.compile(
        r"\b(budget|good|better|best|most popular|popular|premium|platinum|"
        r"gold|silver|bronze|tier|economy|deluxe|recommended|option [a-z]|"
        r"total wellness|standard|basic)\b",
        re.I,
    )

    # --- Phase 1: bucket by projectId when present ---
    grouped: dict = defaultdict(list)
    no_project: list[dict] = []
    for e in estimates:
        pid = e.get("projectId") or 0
        if pid:
            grouped[("proj", pid)].append(e)
        else:
            no_project.append(e)

    # --- Phase 2: cluster no_project estimates by (createdOn day, tier signal, ID proximity) ---
    no_project.sort(key=lambda e: (e.get("createdOn") or "", e.get("id") or 0))
    used: set[int] = set()
    cluster_counter = 0
    for i, e in enumerate(no_project):
        if id(e) in used:
            continue
        members = [e]
        used.add(id(e))
        created_i = (e.get("createdOn") or "")[:10]
        id_i = e.get("id") or 0
        # Look ahead within ±10 IDs and same created day
        for j in range(i + 1, len(no_project)):
            f = no_project[j]
            if id(f) in used:
                continue
            created_j = (f.get("createdOn") or "")[:10]
            id_j = f.get("id") or 0
            if created_i and created_j and created_i == created_j and abs(id_j - id_i) <= 10:
                members.append(f)
                used.add(id(f))
        # Only cluster if multiple members AND at least one tier keyword anywhere
        if len(members) >= 2 and any(TIER_RE.search(m.get("name") or "") for m in members):
            cluster_counter += 1
            grouped[("heur", cluster_counter)].extend(members)
        else:
            # Singleton — treat each as its own opportunity
            for m in members:
                cluster_counter += 1
                grouped[("solo", m.get("id"))].append(m)

    # --- Build cluster summaries ---
    out = []
    for key, members in grouped.items():
        sold = [m for m in members if (m.get("status") or {}).get("name","").lower() == "sold"]
        open_ = [m for m in members if (m.get("status") or {}).get("name","").lower() == "open"]
        dismissed = [m for m in members
                     if (m.get("status") or {}).get("name","").lower() in ("dismissed", "expired")]
        if sold:
            status = "won"
        elif open_ and not dismissed:
            status = "open"
        elif dismissed and not open_:
            status = "dismissed"
        else:
            status = "mixed"

        sold_total = sum(float(m.get("subtotal") or 0) for m in sold)
        # Anchor = highest-$ open estimate (the "preferred tier" the customer didn't take)
        anchor = max(open_, key=lambda m: float(m.get("subtotal") or 0)) if open_ else None
        out.append({
            "opportunity_id": key,
            "members": members,
            "sold_members": sold,
            "open_members": open_,
            "dismissed_members": dismissed,
            "status": status,
            "sold_dollars": sold_total,
            "open_dollars": sum(float(m.get("subtotal") or 0) for m in open_),
            "anchor_open_estimate": anchor,
        })
    return out


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return None


# SKU keyword → category. First match wins. Order matters: most-specific patterns first.
SKU_CATEGORIES: list[tuple[str, str]] = [
    # --- Specific repairs and small parts (must match BEFORE system replacement) ---
    ("cleaning service",                        r"\b(cleaning|clean condenser|coil cleaning|chemical wash|condenser clean)\b"),
    ("capacitor / start kit",                   r"\b(capacitor|start kit|start assist|hard start|compressor saver|run cap)\b"),
    ("control board / electronics",             r"\b(defrost board|circuit board|control board|control module|ignitor|igniter|sensor|flame sensor|pressure switch|gas valve)\b"),
    ("thermostat",                              r"\b(thermostat|t.?stat|ecobee|nest|honeywell stat|smart stat)\b"),
    ("float switch / safety",                   r"\b(float switch|safety switch|condensate switch|wet switch)\b"),
    ("drain / condensate",                      r"\b(p.?trap|drain pan|drain line|teflon coat|drain rebuild|condensate line|condensate drain|secondary drain)\b"),
    ("surge protection",                        r"\b(surge protector|surge protect|whole.?home surge|spd\b|l4 surge|l5 surge)\b"),
    ("filter drier",                            r"\b(filter drier|filter.dryer|liquid line drier)\b"),
    ("refrigerant / leak",                      r"\b(refrigerant|410a|r-?22|r-?410|r.?454|leak search|leak repair|recharge|freon|add charge)\b"),
    ("IAQ / air quality",                       r"\b(iaq|uv\b|reme|air scrubber|ionizer|purifier|merv|polarized|filtration|aprilaire|humidifier|dehumidifier|halo|air purif|media filter)\b"),
    ("water heater",                            r"\b(water heater|tankless|wh\b|hot water)\b"),
    ("electrical panel / service",              r"\b(electrical panel|breaker panel|service entrance|main service|sub.?panel|new panel|panel replacement|meter base)\b"),
    ("ev charger",                              r"\b(ev charger|ev charging|level 2 charger)\b"),
    ("plumbing repair / fixture",               r"\b(faucet|toilet|disposal|garbage disposal|shower|valve|pressure reducing|prv|expansion tank|hose bib|water leak|sewer|water filter|water filtration|h2 zero)\b"),
    ("re-pipe / supply lines",                  r"\b(re.?pipe|supply line|pex repipe|galvanized|copper repipe)\b"),
    ("gfci / outlets / wiring",                 r"\b(gfci|afci|outlet replace|receptacle|rewire|aluminum wiring|whole.?home wiring)\b"),
    ("maintenance / tune-up",                   r"\b(tune.?up|preventative maintenance|seasonal maintenance|psi inspection|esi inspection|hvac maint|spring tune)\b"),
    ("membership / club",                       r"\b(cool club|membership|club|silver plan|gold plan|platinum plan)\b"),
    ("trip / diagnostic fee",                   r"\b(trip charge|diagnostic fee|service call fee|dispatch fee)\b"),
    # --- System replacement / capital equipment: REQUIRES verb + capital-equipment noun ---
    # Must be an actual replacement/install of a major piece of equipment, not a small repair on it.
    ("system replacement / capital equipment",
     r"\b("
     r"new system|system install|complete install|complete system|change.?out|changeout|"
     r"(?:replace|install|new)\s+(?:compressor|condenser(?!\s+clean)|coil|evap(?:orator)?|furnace|air handler|"
        r"heat pump|mini.?split|package unit|condensing unit|outdoor unit|indoor unit)|"
     r"(?:condenser|coil|furnace|air handler|heat pump|compressor|evaporator)\s+replacement|"
     r"(?:condenser|coil|furnace|heat pump)\s+(?:and|\+)\s+(?:coil|furnace|condenser|air handler)"
     r")\b"),
    # --- Catch-alls (least specific, last) ---
    ("misc repair",                             r"\b(misc repair|repair|fix|service work)\b"),
]
_SKU_RE = [(label, re.compile(pat, re.I)) for label, pat in SKU_CATEGORIES]


def _categorize_item(text: str) -> str:
    # Plumbing water-treatment SKUs can include brand terms like HALO that otherwise look like IAQ.
    if re.search(r"\b(water filter|water filtration|whole home water|potable water|flow[- ]?tech|h2 zero|descale|anti-scale)\b", text or "", re.I):
        return "plumbing repair / fixture"
    for label, rx in _SKU_RE:
        if rx.search(text):
            return label
    return "other"




def _category_trade(category: str) -> str:
    """Route estimate categories to the trade that should lead the coaching."""
    if category in {"water heater", "plumbing repair / fixture", "re-pipe / supply lines"}:
        return "plumbing"
    if category in {"surge protection", "electrical panel / service", "ev charger", "gfci / outlets / wiring"}:
        return "electrical"
    if category in {
        "cleaning service", "capacitor / start kit", "control board / electronics", "thermostat",
        "float switch / safety", "drain / condensate", "filter drier", "refrigerant / leak",
        "IAQ / air quality", "system replacement / capital equipment",
    }:
        return "hvac"
    return "other"


def _estimate_line_items(e: dict) -> list[dict]:
    rows: list[dict] = []
    for idx, it in enumerate(e.get("items") or []):
        sku = it.get("sku") or {}
        name = sku.get("displayName") or sku.get("name") or it.get("description") or ""
        text = " ".join(s for s in (sku.get("displayName"), sku.get("name"), it.get("description")) if s)[:500]
        rows.append({
            "idx": idx,
            "id": it.get("id"),
            "sku_id": sku.get("id"),
            "name": name,
            "text": text,
            "category": _categorize_item(text),
            "total": float(it.get("total") or 0),
        })
    return rows


def _positive_sku_ids(e: dict) -> set:
    return {r.get("sku_id") for r in _estimate_line_items(e) if r.get("sku_id") is not None and float(r.get("total") or 0) > 0}


def _line_item_display_key(estimate_id, row: dict) -> tuple:
    """Stable key for QA: a concrete ST estimate line item may feed only one displayed figure."""
    return (estimate_id, row.get("id") if row.get("id") is not None else row.get("idx"))


def _estimate_categories(e: dict) -> set[str]:
    cats = {r["category"] for r in _estimate_line_items(e)}
    if not cats and e.get("name"):
        cats.add(_categorize_item(e.get("name") or ""))
    return cats or {"other"}


def _estimate_primary_category(e: dict) -> str:
    cats = [c for c in _estimate_categories(e) if c != "other"]
    if not cats:
        return "other"
    # Prefer capital equipment categories over accessories when choosing where the estimate belongs.
    priority = [
        "water heater", "electrical panel / service", "system replacement / capital equipment",
        "IAQ / air quality", "surge protection", "ev charger", "gfci / outlets / wiring",
    ]
    for p in priority:
        if p in cats:
            return p
    return cats[0]


EQUIPMENT_HISTORY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("water heater", ("water heater", "tankless", "navien", "hot water")),
    ("electrical panel", ("electrical panel", "breaker panel", "service panel", "main panel", "sub panel", "meter base")),
    ("ev charger", ("ev charger", "level 2 charger", "ev charging")),
    ("water softener", ("water softener", "softener")),
    ("boiler", ("boiler",)),
]


def equipment_implied_by_history(dossier: dict, installed_equipment_on_file: list[str] | None = None) -> list[dict]:
    """Equipment evidenced by estimates/invoices but absent from installed equipment.

    This keeps real sold/open opportunities from being treated as hallucinations while still
    forcing the narrative to mark them as not on file and verify on arrival.
    """
    installed_blob = " ".join(installed_equipment_on_file or _equipment_whitelist(dossier)).lower()
    found: dict[str, dict] = {}

    def scan(text: str, source: str, ref_id=None):
        low = text.lower()
        for label, kws in EQUIPMENT_HISTORY_PATTERNS:
            if any(k in installed_blob for k in kws):
                continue
            if any(k in low for k in kws):
                rec = found.setdefault(label, {"label": label, "sources": [], "keywords": []})
                rec["sources"].append({"source": source, "id": ref_id})
                for k in kws:
                    if k in low and k not in rec["keywords"]:
                        rec["keywords"].append(k)

    for e in dossier.get("estimates") or []:
        blob = (e.get("name") or "") + " " + " ".join((r.get("text") or "") for r in _estimate_line_items(e))
        scan(blob, "estimate", e.get("id"))
    for inv in dossier.get("past_invoices") or []:
        for it in inv.get("items") or []:
            blob = " ".join(str(it.get(k) or "") for k in ("skuName", "displayName", "description", "name"))
            scan(blob, "invoice", inv.get("id"))
    return list(found.values())


def analyze_estimate_history(estimates: list[dict], current_job_id: int | None = None, primary_trade: str | None = None) -> dict:
    """Compute sales-coach intelligence from full estimate history.

    Deterministic rules:
      - total = sold + open + dismissed + expired after suppressing dead leftovers.
      - suppressed is a subset annotation, not an addend.
      - stale category totals exclude line items already surfaced as the standalone largest stale.
      - primary-trade stale anchor is separate from largest-any-trade stale anchor.
    """
    from collections import defaultdict

    def status_of(e: dict) -> str:
        return ((e.get("status") or {}).get("name") or "").lower()

    raw_rows = []
    for e in estimates or []:
        if current_job_id and e.get("jobId") == current_job_id:
            continue
        if status_of(e) in ("open", "sold", "dismissed", "expired"):
            raw_rows.append(e)
    raw_rows.sort(key=lambda e: e.get("createdOn") or "")

    def _est_zone(e):
        blob = (e.get("name") or "") + " " + " ".join(
            (it.get("description") or "") + " " + ((it.get("sku") or {}).get("displayName") or "")
            for it in (e.get("items") or [])
        )
        return _system_zone_token(blob)

    # --- SUPERSESSION DETECTION (kept) ---
    sold_records = [(e, _estimate_categories(e), _est_zone(e), e.get("soldOn") or e.get("modifiedOn") or "")
                    for e in raw_rows if status_of(e) == "sold"]
    superseded_ids: dict = {}
    possibly_superseded_ids: dict = {}
    for e in raw_rows:
        if status_of(e) != "open":
            continue
        open_created = e.get("createdOn") or ""
        open_cats = _estimate_categories(e)
        open_zone = _est_zone(e)
        for sold_e, sold_cats, sold_zone, sold_when in sold_records:
            if sold_when <= open_created:
                continue
            cat_overlap = open_cats & sold_cats
            is_capital = "system replacement / capital equipment" in sold_cats and any(
                c not in ("trip / diagnostic fee", "maintenance / tune-up", "membership / club")
                for c in open_cats
            )
            if not cat_overlap and not is_capital:
                continue
            if open_zone and sold_zone and open_zone != sold_zone:
                possibly_superseded_ids[e.get("id")] = (
                    f"Possibly superseded by sold Est #{sold_e.get('id')} on {sold_when[:10]} "
                    f"(${float(sold_e.get('subtotal') or 0):,.0f} in '{sold_zone}'; this open is for '{open_zone}'). "
                    "Verify which system before re-pitching."
                )
                continue
            reason_cat = "system replacement" if is_capital and not cat_overlap else next(iter(cat_overlap), "")
            superseded_ids[e.get("id")] = (
                f"Superseded by sold Est #{sold_e.get('id')} on {sold_when[:10]} "
                f"(${float(sold_e.get('subtotal') or 0):,.0f} '{reason_cat}')."
            )
            break

    rows = [e for e in raw_rows if e.get("id") not in superseded_ids]

    # Discard tier-sibling estimates of won opportunities; keep large step-ups.
    opps = cluster_opportunities(rows)
    won_sibling_ids: set = set()
    for opp in opps:
        if opp["status"] == "won":
            max_sold = max((float(m.get("subtotal") or 0) for m in opp["sold_members"]), default=0.0)
            for m in opp["open_members"] + opp["dismissed_members"]:
                m_sub = float(m.get("subtotal") or 0)
                if max_sold > 0 and m_sub >= 5 * max_sold and m_sub >= 1000:
                    continue
                won_sibling_ids.add(m.get("id"))
    rows = [e for e in rows if e.get("id") not in won_sibling_ids]
    # Distinct estimate IDs removed from coaching. Final display count is reconciled later to
    # the same visible/headline grain and cannot consume estimates that are actively referenced.
    raw_suppressed_count = len(set(superseded_ids) | won_sibling_ids)

    sold_subtotals: list[float] = []
    dismissed_subtotals: list[float] = []
    expired_subtotals: list[float] = []
    open_subtotals: list[float] = []
    sold_decision_days: list[int] = []
    earliest = latest = None
    biggest_sold = None

    # Pick stale anchors from the cleaned rows.
    stale_open_rows = []
    for e in rows:
        if status_of(e) != "open":
            continue
        sub = float(e.get("subtotal") or 0)
        age = _days_since(e.get("createdOn")) or 0
        if sub >= 500 and age > 90:
            cat = _estimate_primary_category(e)
            item_names = [r["name"] for r in _estimate_line_items(e) if r.get("name")][:5]
            stale_open_rows.append({
                "id": e.get("id"), "subtotal": sub, "name": e.get("name") or "",
                "age_days": age, "category": cat, "trade": _category_trade(cat),
                "intent_state": "OPEN", "system_scope": _system_scope_from_text(
                    (e.get("name") or "") + " " + " ".join((r.get("text") or "") for r in _estimate_line_items(e)),
                    {"installed_equipment": []},
                ),
                "business_unit": e.get("businessUnitName") or "", "line_items": item_names,
                "estimate": e,
            })
    biggest_open_stale = max(stale_open_rows, key=lambda r: r["subtotal"], default=None)
    largest_stale_any_trade = biggest_open_stale
    primary_rows = [r for r in stale_open_rows if primary_trade and r["trade"] == primary_trade]
    largest_stale_primary_trade = max(primary_rows, key=lambda r: r["subtotal"], default=None)

    by_status_cat: dict[str, dict[str, dict]] = {
        "open": defaultdict(lambda: {"count": 0, "dollars": 0.0, "examples": [], "estimate_ids": set()}),
        "sold": defaultdict(lambda: {"count": 0, "dollars": 0.0, "examples": [], "estimate_ids": set()}),
        "dismissed": defaultdict(lambda: {"count": 0, "dollars": 0.0, "examples": [], "estimate_ids": set()}),
        "expired": defaultdict(lambda: {"count": 0, "dollars": 0.0, "examples": [], "estimate_ids": set()}),
    }
    cat_sequence: dict[str, list[str]] = defaultdict(list)
    display_line_item_sources: dict[tuple, set[str]] = defaultdict(set)

    largest_id = (biggest_open_stale or {}).get("id")
    surfaced_stale_estimates = {
        r["id"]: r for r in (largest_stale_primary_trade, largest_stale_any_trade) if r and r.get("id") is not None
    }
    for r in surfaced_stale_estimates.values():
        est = r.get("estimate") or {}
        for item in _estimate_line_items(est):
            display_line_item_sources[_line_item_display_key(r.get("id"), item)].add(f"largest_stale:{r.get('id')}")

    for e in rows:
        status = status_of(e)
        sub = float(e.get("subtotal") or 0)
        created = (e.get("createdOn") or "")[:10]
        sold_on = (e.get("soldOn") or "")[:10] if e.get("soldOn") else ""
        if created:
            earliest = min(earliest, created) if earliest else created
            latest = max(latest, created) if latest else created
        if status == "sold":
            sold_subtotals.append(sub)
            if sub > (biggest_sold["subtotal"] if biggest_sold else -1):
                biggest_sold = {"subtotal": sub, "name": e.get("name") or "", "sold_on": sold_on}
            if created and sold_on:
                try:
                    sold_decision_days.append((datetime.fromisoformat(sold_on) - datetime.fromisoformat(created)).days)
                except Exception:
                    pass
        elif status == "open":
            open_subtotals.append(sub)
        elif status == "dismissed":
            dismissed_subtotals.append(sub)
        elif status == "expired":
            expired_subtotals.append(sub)

        # Per-line-item categorization with attribution. If an estimate is surfaced as standalone
        # largest-stale, its open line-items are intentionally omitted from category rollups.
        if status == "open" and e.get("id") == largest_id:
            continue
        line_rows = _estimate_line_items(e)
        if not line_rows:
            line_rows = [{"category": _categorize_item(e.get("name") or ""), "total": sub, "name": e.get("name") or ""}]
        item_total = sum(float(r.get("total") or 0) for r in line_rows)
        cat_sub_this_est: dict[str, float] = {}
        for r in line_rows:
            cat = r["category"]
            line_total = float(r.get("total") or 0)
            if item_total > 0 and line_total > 0:
                attributed = line_total
            else:
                attributed = sub / max(len(line_rows), 1)
            cat_sub_this_est[cat] = cat_sub_this_est.get(cat, 0.0) + attributed
        for cat, attributed in cat_sub_this_est.items():
            bucket = by_status_cat[status][cat]
            bucket["count"] += 1
            bucket["dollars"] += attributed
            if len(bucket["examples"]) < 3:
                bucket["examples"].append((e.get("name") or "")[:60])
            if e.get("id") is not None:
                bucket["estimate_ids"].add(e.get("id"))
            cat_sequence[cat].append(status)
        for r in line_rows:
            display_line_item_sources[_line_item_display_key(e.get("id"), r)].add(f"category:{status}:{r['category']}")

    GENERIC_CATS = {"other", "repair (other)", "misc repair", "trip / diagnostic fee", "membership / club", "maintenance / tune-up"}
    repeat_declines: list[dict] = []
    always_buys: list[dict] = []
    prior_single_buys: list[dict] = []
    slow_yes: list[str] = []
    open_stale_high_value: list[dict] = []
    rows_by_id = {e.get("id"): e for e in rows if e.get("id") is not None}

    def _category_has_tier_overlap(anchor: dict | None, bucket_estimate_ids: set) -> bool:
        """Detect tier-stack overlap where category rollup and largest-stale describe the same SKU bundle.

        ServiceTitan gives each estimate line a unique line-item id, even when Good/Better/Best/Platinum
        tiers reuse the same SKUs. The displayed category figure must still avoid showing a parent bundle
        total plus its surfaced tier as separate dollars. We only apply this when the anchor's positive-SKU
        bundle is repeated across multiple open sibling estimates in the same category; this preserves small
        mixed IAQ stacks like Arun while fixing the 3-system Platinum stack.
        """
        if not anchor or not anchor.get("id"):
            return False
        anchor_est = anchor.get("estimate") or rows_by_id.get(anchor.get("id")) or {}
        anchor_skus = _positive_sku_ids(anchor_est)
        if not anchor_skus or len(bucket_estimate_ids) < 3:
            return False
        overlapping_siblings = 0
        repeated_skus: set = set()
        for est_id in bucket_estimate_ids:
            e = rows_by_id.get(est_id)
            if not e:
                continue
            overlap = anchor_skus & _positive_sku_ids(e)
            if overlap:
                overlapping_siblings += 1
                repeated_skus.update(overlap)
        return overlapping_siblings >= 3 and len(repeated_skus) >= 2

    for cat, seq in cat_sequence.items():
        if cat in GENERIC_CATS:
            continue
        n_sold = seq.count("sold")
        n_dismissed = seq.count("dismissed")
        n_open = seq.count("open")
        dismissed_d = by_status_cat["dismissed"][cat]["dollars"] + by_status_cat["expired"][cat]["dollars"]
        sold_d = by_status_cat["sold"][cat]["dollars"]
        open_d = by_status_cat["open"][cat]["dollars"]
        if n_dismissed >= 2 and n_sold == 0 and dismissed_d > 0:
            repeat_declines.append({"category": cat, "count": n_dismissed, "dollars": dismissed_d, "trade": _category_trade(cat)})
        if n_sold >= 2 and n_dismissed == 0 and sold_d >= 1000:
            always_buys.append({"category": cat, "count": n_sold, "dollars": sold_d, "trade": _category_trade(cat)})
        elif n_sold > 0 and sold_d > 0:
            prior_single_buys.append({"category": cat, "count": n_sold, "dollars": sold_d, "trade": _category_trade(cat)})
        if "sold" in seq and "dismissed" in seq and seq.index("dismissed") < seq.index("sold"):
            slow_yes.append(cat)
        if n_open and open_d >= 1000:
            bucket_estimate_ids = set(by_status_cat["open"][cat].get("estimate_ids") or [])
            displayed_open_d = open_d
            same_cat_anchor = None
            for anchor in (largest_stale_primary_trade, largest_stale_any_trade):
                if anchor and anchor.get("category") == cat:
                    same_cat_anchor = anchor
                    break
            if same_cat_anchor and _category_has_tier_overlap(same_cat_anchor, bucket_estimate_ids):
                displayed_open_d = max(0.0, displayed_open_d - float(same_cat_anchor.get("subtotal") or 0))
            if displayed_open_d >= 1000:
                open_stale_high_value.append({
                    "category": cat,
                    "count": n_open,
                    "dollars": round(displayed_open_d, 0),
                    "trade": _category_trade(cat),
                    "estimate_ids": sorted(bucket_estimate_ids),
                })

    repeat_declines.sort(key=lambda r: r["count"], reverse=True)
    always_buys.sort(key=lambda r: r["dollars"], reverse=True)
    prior_single_buys.sort(key=lambda r: r["dollars"], reverse=True)
    open_stale_high_value.sort(key=lambda r: r["dollars"], reverse=True)

    def _avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 0) if xs else 0.0

    price_ceiling = max(sold_subtotals) if sold_subtotals else 0.0
    opportunities_above_ceiling = []
    if price_ceiling:
        for r in stale_open_rows:
            if r["subtotal"] > 2 * price_ceiling:
                opportunities_above_ceiling.append({k: v for k, v in r.items() if k != "estimate"})

    total_estimates = len(rows)
    coached_ids: set = set()
    for anchor in (largest_stale_primary_trade, largest_stale_any_trade):
        if anchor and anchor.get("id") is not None:
            coached_ids.add(anchor.get("id"))
    for bucket in open_stale_high_value:
        coached_ids.update(bucket.get("estimate_ids") or [])
    coached_reference_count = len(coached_ids)
    suppressed_count = min(raw_suppressed_count, max(total_estimates - coached_reference_count, 0))
    coachable_count = total_estimates - suppressed_count
    assert coachable_count >= coached_reference_count, (
        "suppression count exceeds display-grain coachable estimate count"
    )
    display_attribution_violations = [
        {"line_item_key": key, "figures": sorted(figures)}
        for key, figures in display_line_item_sources.items()
        if len(figures) > 1
    ]
    if display_attribution_violations and os.environ.get("LEX_BRIEF_QA_ASSERT") == "1":
        raise AssertionError("estimate line item attributed to multiple displayed figures")
    return {
        "total_estimates": total_estimates,
        "history_span": {"first": earliest, "last": latest},
        "totals": {
            "open_count": len(open_subtotals), "open_dollars": round(sum(open_subtotals), 0),
            "sold_count": len(sold_subtotals), "sold_dollars": round(sum(sold_subtotals), 0),
            "dismissed_count": len(dismissed_subtotals), "dismissed_dollars": round(sum(dismissed_subtotals), 0),
            "expired_count": len(expired_subtotals), "expired_dollars": round(sum(expired_subtotals), 0),
        },
        "averages": {
            "avg_sold_ticket": _avg(sold_subtotals), "avg_dismissed_ticket": _avg(dismissed_subtotals),
            "avg_open_ticket": _avg(open_subtotals), "avg_decision_days": _avg([float(x) for x in sold_decision_days]),
        },
        "price_ceiling": price_ceiling,
        "biggest_sold": biggest_sold,
        "biggest_open_stale": {k: v for k, v in biggest_open_stale.items() if k != "estimate"} if biggest_open_stale else None,
        "largest_stale_any_trade": {k: v for k, v in largest_stale_any_trade.items() if k != "estimate"} if largest_stale_any_trade else None,
        "largest_stale_primary_trade": {k: v for k, v in largest_stale_primary_trade.items() if k != "estimate"} if largest_stale_primary_trade else None,
        "opportunities_above_ceiling": opportunities_above_ceiling,
        "repeat_declines": repeat_declines,
        "always_buys": always_buys,
        "prior_single_buys": prior_single_buys,
        "slow_yes_categories": slow_yes,
        "open_stale_high_value": open_stale_high_value,
        "suppressed_count": suppressed_count,
        "superseded_count": suppressed_count,  # backward-compatible display key
        "raw_suppressed_count": raw_suppressed_count,
        "coachable_count": coachable_count,
        "coached_reference_count": coached_reference_count,
        "display_attribution_violations": display_attribution_violations,
        "possibly_superseded": list(possibly_superseded_ids.values())[:5],
        "category_breakdown": {
            status: {cat: {"count": d["count"], "dollars": round(d["dollars"], 0), "examples": d["examples"], "estimate_ids": sorted(d.get("estimate_ids") or []), "trade": _category_trade(cat)}
                     for cat, d in cats.items()}
            for status, cats in by_status_cat.items()
        },
    }

def estimate_intel_facts(intel: dict) -> list[str]:
    """Distill the deterministic intel dict to short bullet facts for the LLM + fallback rendering."""
    facts: list[str] = []
    t = intel["totals"]
    a = intel["averages"]

    if intel["total_estimates"] == 0:
        return ["No prior estimate history on this customer."]

    span = intel["history_span"]
    if span["first"] and span["last"]:
        facts.append(f"Estimate history spans {span['first']} → {span['last']} ({intel['total_estimates']} estimates total).")

    if t["sold_count"]:
        facts.append(f"Lifetime sold: {t['sold_count']} estimates · ${t['sold_dollars']:,.0f}. Avg sold ticket ${a['avg_sold_ticket']:,.0f}; price ceiling (largest accepted) ${intel['price_ceiling']:,.0f}.")
        if a["avg_decision_days"] and a["avg_decision_days"] > 0:
            d = a["avg_decision_days"]
            tempo = "decides on-site / same-day" if d <= 1 else ("sleeps on it 2-14d" if d <= 14 else "long decision cycle (>2 weeks)")
            facts.append(f"Decision tempo: avg {d:.0f}d from quote to sold — {tempo}.")
    else:
        facts.append("Lifetime sold: $0 — customer has never bought off an estimate before. First-time-close opportunity.")

    if t["open_count"]:
        facts.append(f"Currently open: {t['open_count']} estimates · ${t['open_dollars']:,.0f}.")
    if t["dismissed_count"]:
        facts.append(f"Dismissed/declined lifetime: {t['dismissed_count']} estimates · ${t['dismissed_dollars']:,.0f}.")

    for rd in intel["repeat_declines"][:3]:
        facts.append(f"Repeat decline pattern: declined '{rd['category']}' {rd['count']}× — do NOT lead with this; change the pitch, bundle, or skip.")
    for ab in intel["always_buys"][:3]:
        facts.append(f"Repeated buy pattern: accepted '{ab['category']}' {ab['count']}× (${ab['dollars']:,.0f}) and never declined it — safe lead.")
    for pb in intel.get("prior_single_buys", [])[:3]:
        facts.append(f"Prior purchase, not repeat pattern: bought '{pb['category']}' before (${pb['dollars']:,.0f}) — use as context, not a reliable-buyer claim.")
    for cat in intel["slow_yes_categories"][:2]:
        facts.append(f"Slow-yes signal: previously declined '{cat}' but later bought it — plant seed today, don't hard-close.")
    for sv in intel["open_stale_high_value"][:3]:
        facts.append(f"Big stale opportunity: ${sv['dollars']:,.0f} of '{sv['category']}' estimates sitting open — re-quote with fresh pricing.")
    if intel["biggest_open_stale"]:
        bs = intel["biggest_open_stale"]
        facts.append(f"Largest stale open estimate: ${bs['subtotal']:,.0f} — '{bs['name']}' ({bs['age_days']}d old).")
    if intel["biggest_sold"]:
        bs = intel["biggest_sold"]
        facts.append(f"Largest historical sale: ${bs['subtotal']:,.0f} on {bs['sold_on']} — '{bs['name']}' (proof they spend at this level).")

    return facts


def summarize_estimates(estimates: list[dict], current_job_id: int | None = None) -> dict:
    """Bucket estimates into open / sold / dismissed with the fields the tech actually needs.

    Returns:
      {
        "open":      [{name, subtotal, age_days, sold_by, business_unit, items_short, id, job_id}],
        "sold":      [...  + sold_on, sold_by ],
        "dismissed": [...],
        "last_sold": "<one-line summary>" or "",
        "open_total": float,
      }
    """
    out = {"open": [], "sold": [], "dismissed": [], "last_sold": "", "open_total": 0.0}
    for e in estimates or []:
        # never echo back THIS visit's own estimate (if any)
        if current_job_id and e.get("jobId") == current_job_id:
            continue
        status = ((e.get("status") or {}).get("name") or "").lower()
        if not status:
            continue
        sub = float(e.get("subtotal") or 0)
        items = e.get("items") or []
        items_short = []
        for it in items[:4]:
            desc = (it.get("sku") or {}).get("displayName") or (it.get("sku") or {}).get("name") or ""
            qty = it.get("qty") or 1
            items_short.append(f"{qty:g}× {desc}" if qty and qty != 1 else desc)
        row = {
            "id": e.get("id"),
            "job_id": e.get("jobId"),
            "name": (e.get("name") or "").strip() or "(unnamed estimate)",
            "subtotal": sub,
            "business_unit": e.get("businessUnitName") or "",
            "created_on": (e.get("createdOn") or "")[:10],
            "modified_on": (e.get("modifiedOn") or "")[:10],
            "sold_on": (e.get("soldOn") or "")[:10] if e.get("soldOn") else "",
            "sold_by": e.get("soldBy"),
            "items_short": items_short,
            "age_days": _days_since(e.get("createdOn")),
        }
        if status == "open":
            out["open"].append(row)
            out["open_total"] += sub
        elif status == "sold":
            out["sold"].append(row)
        elif status in ("dismissed", "expired"):
            out["dismissed"].append(row)

    # newest first
    out["open"].sort(key=lambda r: r["created_on"], reverse=True)
    out["sold"].sort(key=lambda r: r["sold_on"] or r["modified_on"], reverse=True)
    out["dismissed"].sort(key=lambda r: r["modified_on"], reverse=True)

    if out["sold"]:
        s = out["sold"][0]
        out["last_sold"] = f"{s['sold_on'] or s['modified_on']} · ${s['subtotal']:,.0f} · {s['name']}"
    return out


def trade_from_jobtype(jt_name: str, bu_name: str) -> str:
    blob = f"{jt_name} {bu_name}".lower()
    if "psi" in blob or "plumb" in blob:
        return "plumbing"
    if "esi" in blob or "electric" in blob:
        return "electrical"
    return "hvac"


def cross_trade_signals(facts: dict, dossier: dict, trade: str) -> list[str]:
    """Heuristic cross-trade observations seeded from facts so the AI can include them."""
    signals: list[str] = []
    home_age_raw = facts.get("home_age") or ""
    try:
        year = int(re.search(r"\d{4}", home_age_raw).group(0))  # type: ignore[union-attr]
        home_built = year
    except Exception:
        home_built = None

    # Pull equipment-derived signals
    wh_age = None
    panel_age = None
    hvac_old = []
    for eq in dossier.get("installed_equipment") or []:
        if not eq.get("active"):
            continue
        raw = eq.get("type") or eq.get("name") or ""
        if isinstance(raw, dict):
            raw = raw.get("name") or ""
        kl = str(raw).lower() + " " + str(eq.get("name") or "").lower()
        age = years_old(eq.get("installedOn"))
        if any(w in kl for w in ("water heater", "tankless", "wh ")):
            if age is not None:
                wh_age = max(wh_age or 0, age)
        if any(w in kl for w in ("panel", "breaker", "service entrance")):
            if age is not None:
                panel_age = max(panel_age or 0, age)
        if any(w in kl for w in ("furnace", "condens", "air handler", "heat pump")) and age and age >= 12:
            hvac_old.append(f"{raw} (~{age}y)")

    if trade != "plumbing":
        if wh_age and wh_age >= 8:
            signals.append(f"Plumbing cross-sell: water heater on file is ~{wh_age}y old (typical life 8-12y) — quote inspection or replacement.")
        elif home_built and home_built < 1970:
            signals.append(f"Plumbing cross-sell: pre-1970 home ({home_built}) — high likelihood of galvanized supply, original cast iron drains, lead solder; offer full PSI + camera/scope.")
        elif home_built and home_built < 1985:
            signals.append(f"Plumbing cross-sell: home built {home_built} — original water heater age, potential galvanized branch lines, no PRV; offer PSI.")
        elif home_built and home_built < 2000:
            signals.append(f"Plumbing cross-sell: home built {home_built} — original water heater nearing end of life, expansion tank often missing; offer PSI.")
        elif home_built and home_built < 2015:
            signals.append(f"Plumbing cross-sell: home built {home_built} — water heater at typical replacement age, PRV/expansion tank often original; offer PSI checklist.")
    if trade != "electrical":
        if panel_age and panel_age >= 20:
            signals.append(f"Electrical cross-sell: panel on record is ~{panel_age}y old — offer ESI and panel evaluation.")
        elif home_built and home_built < 1975:
            signals.append(f"Electrical cross-sell: pre-1975 home ({home_built}) — risk of Federal Pacific, Zinsco, Pushmatic, aluminum branch, knob-and-tube remnants, undersized service; ESI is high-value.")
        elif home_built and home_built < 1990:
            signals.append(f"Electrical cross-sell: home built {home_built} — original panel age, likely no surge protection, partial GFCI/AFCI coverage; offer ESI.")
        elif home_built and home_built < 2005:
            signals.append(f"Electrical cross-sell: home built {home_built} — panel approaching mid-life, often no whole-home surge or modern AFCI coverage; offer ESI + surge.")
        elif home_built and home_built < 2020:
            signals.append(f"Electrical cross-sell: home built {home_built} — likely opportunity for whole-home surge and EV-readiness assessment; verify electrical equipment on arrival.")
    if trade != "hvac":
        if hvac_old:
            signals.append(f"HVAC cross-sell: aging equipment on file — {', '.join(hvac_old[:3])}. Worth a tune-up or replacement conversation.")
        # NOTE: do NOT fire "older home" HVAC cross-sell unless equipment is actually old.
        # Recently-replaced HVAC in a 1980s home is still recent HVAC — don't push duct/IAQ on age alone.

    # Membership cross-sell
    if facts.get("membership", "").lower().startswith("no active") or "expired" in facts.get("membership", "").lower() or "canceled" in facts.get("membership", "").lower() or "suspended" in facts.get("membership", "").lower():
        signals.append("Membership: not active right now — natural opening to re-enroll, especially since today is a maintenance/inspection visit.")

    # Behavioral cross-sell: SOLD reveals what they value; OPEN only means we quoted it.
    intel = facts.get("_intel") or {}
    sold_cats = set()
    open_cats = set()
    for status_key, target in (("sold", sold_cats), ("open", open_cats)):
        for cat in (intel.get("category_breakdown", {}).get(status_key, {}) or {}):
            if (intel["category_breakdown"][status_key][cat].get("dollars") or 0) > 0:
                target.add(cat)

    # IAQ buyer/quoted → health/air conversation; distinguish SOLD vs OPEN.
    if "IAQ / air quality" in sold_cats and trade != "plumbing":
        signals.append(
            "Behavior cross-sell: customer has bought IAQ (UV/Reme/scrubbers/purification) — "
            "they have invested in indoor environment + health. Natural opening for whole-home water quality "
            "conversation (same buying psychology; verify equipment before naming specific products)."
        )
    elif "IAQ / air quality" in open_cats and trade != "plumbing":
        signals.append(
            "Behavior cross-sell: we quoted IAQ/air-quality work and never closed it — warm re-open; "
            "do not call it customer interest unless a note says so. Use verified findings to connect air quality to water quality only after equipment verification."
        )
    # Surge buyer → cares about protection → whole-home surge upsell or panel hardening
    if "surge protection" in sold_cats and trade == "electrical":
        signals.append(
            "Behavior cross-sell: customer has invested in surge — likely receptive to whole-home "
            "GFCI/AFCI upgrade, panel hardening, and grounding/bonding improvements."
        )
    # Capital equipment buyer → just dropped serious money on HVAC → cross-sell IAQ + smart thermostat
    if "system replacement / capital equipment" in sold_cats and trade != "hvac":
        signals.append(
            "Behavior cross-sell: customer recently bought new system(s) — protect the investment angle: "
            "surge protection on outdoor units, dedicated circuit verification, water heater age check "
            "(if old water heater fails it can damage new HVAC)."
        )
    # Water heater buyer → cares about water → soft water, filtration, leak detection
    if "water heater" in sold_cats and trade != "plumbing":
        signals.append(
            "Behavior cross-sell: customer has invested in water heater — natural lead-in for water "
            "quality and leak detection conversation; verify equipment before naming specific products."
        )
    # Drain / plumbing repair history → mention preventative maintenance plan
    if "drain / condensate" in sold_cats and "membership / club" not in sold_cats and trade == "plumbing":
        signals.append(
            "Behavior cross-sell: drain history but no active plumbing maintenance plan — annual "
            "drain treatment and inspection package fits their pattern."
        )

    # Off-file-but-evidenced equipment: real opportunity, but force tech to verify before pitching.
    likely_equipment = equipment_implied_by_history(dossier, _equipment_whitelist(dossier))
    likely_labels = {str(x.get("label") or "").lower() for x in likely_equipment if isinstance(x, dict)}
    largest_any = (intel or {}).get("largest_stale_any_trade") or {}
    if trade != "plumbing" and "water heater" in likely_labels:
        est = f" Est #{largest_any.get('id')}" if largest_any.get("category") == "water heater" and largest_any.get("id") else ""
        amt = f" ${float(largest_any.get('subtotal') or 0):,.0f}" if largest_any.get("category") == "water heater" and largest_any.get("subtotal") else ""
        signals.append(
            f"Plumbing cross-sell: water heater/tankless opportunity{amt}{est} is supported by estimate history but is not on file — verify on arrival before discussing."
        )

    return signals


def _equipment_whitelist(dossier: dict) -> list[str]:
    """List of equipment types/names actually on file — narrative may ONLY reference these."""
    out: list[str] = []
    for eq in dossier.get("installed_equipment") or []:
        if not eq.get("active"):
            continue
        t = eq.get("type")
        if isinstance(t, dict):
            t = t.get("name")
        n = eq.get("name") or ""
        for v in (t, n):
            if v and str(v).strip() and str(v).strip().lower() not in [x.lower() for x in out]:
                out.append(str(v).strip())
    return out[:20]


def _cross_trade_section(text: str) -> str:
    match = re.search(
        r"(?is)(Cross-trade leads:\s*.*?)(?:\n\s*(?:##\s+)?(?:Key info|Estimate intel|Heads up|Go win the call:|Sales coach:|HVAC opportunities:|Plumbing opportunities:|Electrical opportunities:)|\Z)",
        text or "",
    )
    return match.group(1) if match else ""


def _named_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?is)({re.escape(heading)}:\s*.*?)(?:\n\s*(?:Go win the call:|Sales coach:|HVAC opportunities:|Plumbing opportunities:|Electrical opportunities:|Cross-trade leads:|Opener:|##\s+Key info|##\s+Estimate intel|##\s+Heads up)|\Z)",
        text or "",
    )
    return match.group(1) if match else ""


def _section_bullets(section: str) -> list[str]:
    return [line.strip(" -•\t") for line in (section or "").splitlines() if line.lstrip().startswith(("-", "•"))]


def _has_opportunity_tie(line: str) -> bool:
    low = line.lower()
    return bool(re.search(r"\b(opportunity|opens?|pitch|quote|re-quote|estimate|est #|sell|upsell|cross-trade|lead|financing|phase|bundle|iaq|surge|duct|water quality|drain safety|pan coat|replacement|membership|ceiling|declined|sold|bought|warm re-open)\b", low))


def _looks_procedure_only(line: str) -> bool:
    low = line.lower()
    procedure_terms = (
        "wash condenser", "flush drain", "measure delta", "superheat", "subcool",
        "test float", "clean condenser", "check amps", "inspect blower", "pm", "maintenance checklist",
        "complete full", "capture model", "document with photos", "clear condensate",
    )
    return any(t in low for t in procedure_terms) and not _has_opportunity_tie(line)


def _opener_grounded(opener: str, dossier: dict) -> bool:
    if not opener:
        return True
    hay = " ".join(
        [strip_html(j.get("summary") or j.get("summaryOfWork") or "") for j in dossier.get("past_jobs") or []]
        + [str(e.get("id") or "") + " " + (e.get("name") or "") for e in dossier.get("estimates") or []]
    ).lower()
    low = opener.lower()
    if m := re.search(r"est\s*#?\s*(\d+)", low):
        return m.group(1) in hay
    anchors = ["coil", "water heater", "tankless", "duct", "surge", "iaq", "maintenance", "plumbing", "electrical", "inspection", "fixture"]
    return any(a in low and a in hay for a in anchors)


def _strip_cross_trade_none_obvious_when_real_leads(text: str) -> str:
    """Remove the empty-state fallback when Cross-trade already has real bullets."""
    section = _cross_trade_section(text)
    if not section or "none obvious" not in section.lower():
        return text
    real_bullets = [
        line for line in section.splitlines()
        if line.lstrip().startswith("-") and "none obvious" not in line.lower()
    ]
    if not real_bullets:
        return text
    return re.sub(r"(?im)^\s*-\s*None obvious\s+[—-]\s+check during walkthrough\.?\s*$\n?", "", text).strip()


# Equipment categories that, if mentioned in a narrative WITHOUT being on file, are likely hallucinations
HALLUCINATION_WATCHLIST = {
    "water heater": ("water heater", "tankless"),
    "panel":         ("electrical panel", "breaker panel", "service panel"),
    "ev charger":    ("ev charger",),
    "softener":      ("water softener",),
    "boiler":        ("boiler",),
    "thermostat":    ("nest", "ecobee"),
}


def validate_narrative(ai_text: str, dossier: dict, facts: dict, appt_date_str: str, intel: dict) -> list[str]:
    """Return a list of violations. Empty list = passed."""
    violations: list[str] = []
    text_low = ai_text.lower()
    whitelist_low = " ".join(_equipment_whitelist(dossier)).lower()
    likely = facts.get("equipment_likely_present_not_on_file") or equipment_implied_by_history(dossier, _equipment_whitelist(dossier))
    likely_labels = {str(x.get("label") or "").lower() for x in likely if isinstance(x, dict)}

    for label, keywords in HALLUCINATION_WATCHLIST.items():
        mentioned = any(k in text_low for k in keywords)
        on_file = any(k in whitelist_low for k in keywords)
        if not mentioned or on_file:
            continue
        if label in likely_labels or (label == "panel" and "electrical panel" in likely_labels) or (label == "softener" and "water softener" in likely_labels):
            # It is real enough to coach from history, but must be framed as absent from ST equipment.
            evidence_sentence_ok = False
            for sent in re.split(r"[\n.]", text_low):
                if any(k in sent for k in keywords) and "not on file" in sent and "verify" in sent:
                    evidence_sentence_ok = True
                    break
            if not evidence_sentence_ok:
                violations.append(
                    f"Mention of {label} is supported by estimates/invoices but NOT installed equipment. Say it is not on file and verify on arrival."
                )
            continue
        violations.append(
            f"Removed mention of {label}: no {label} found on installed equipment or estimate/invoice evidence. Do not invent equipment."
        )

    # Commercial framing
    if facts.get("is_commercial"):
        if re.search(r"\bhome\b|\bhomeowner\b|\bresidence\b|\bhouse\b", text_low):
            violations.append("This is a COMMERCIAL property — never use 'home', 'house', 'homeowner', or 'residence'. Use 'property', 'building', or 'facility'.")

    # No-history sales-coach contradicts intel
    if intel.get("total_estimates", 0) > 0 and re.search(r"no estimate history|new customer", text_low):
        violations.append(
            f"Customer HAS estimate history ({intel['total_estimates']} estimates). Do not say 'no estimate history' or 'new customer'."
        )

    # Date scraping — any date string in narrative that isn't the appointment date
    appt_year_month = appt_date_str[:7] if appt_date_str else ""
    rogue_dates = re.findall(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s+\d{4})?", ai_text)
    rogue_months = re.findall(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", ai_text)
    # Strip any that match the appointment date (very tolerant: month name + day appears in appt string)
    appt_lc = (facts.get("appt") or "").lower()
    for m in rogue_months:
        if m.lower() not in appt_lc:
            violations.append(
                f"Removed month '{m}' from narrative. Never include dates other than the scheduled appointment date."
            )
    for d in rogue_dates:
        if d.lower() in appt_lc:
            continue
        # also accept matches that overlap appt date components
        if d.split(",")[0].lower() in appt_lc:
            continue
        violations.append(
            f"Removed date '{d}' from narrative. Never include dates other than the scheduled appointment date."
        )

    # Scope-contradiction: don't call an install/repair a 'maintenance visit'
    jt = (dossier.get("job", {}).get("jobNumber") or "").lower()
    bu = (dossier.get("job", {}).get("businessUnitName") or "").lower()
    job_type_lc = " ".join((bu, jt)).lower()
    if any(x in job_type_lc for x in ("install", "repair", "replace")) and "maintenance visit" in text_low:
        violations.append("This is NOT a maintenance visit — the job is an install/repair. Do not call it 'maintenance visit'.")

    # Cross-trade empty-state must replace, not append, when real leads exist.
    cross_section = _cross_trade_section(ai_text)
    if cross_section and "none obvious" in cross_section.lower():
        real_bullets = [
            line for line in cross_section.splitlines()
            if line.lstrip().startswith("-") and "none obvious" not in line.lower()
        ]
        lead_set_nonempty = bool(facts.get("cross_signals") or facts.get("cross_trade_signals_detected") or facts.get("equipment_likely_present_not_on_file"))
        if real_bullets or lead_set_nonempty:
            violations.append("Cross-trade leads contains real leads; remove the 'None obvious' fallback line.")

    # Sales-enablement validation: Go win must be opportunity setup, not PM procedure.
    go_section = _named_section(ai_text, "Go win the call")
    go_bullets = _section_bullets(go_section)
    if len(go_bullets) > 3:
        violations.append("Go win the call has more than 3 bullets; trim to opportunity setup only.")
    for line in go_bullets:
        if _looks_procedure_only(line):
            violations.append(f"Go win the call contains procedure-only line with no opportunity tie: '{line}'.")

    # OPEN/EXPIRED estimates are not customer interest/value proof.
    category_terms = {
        "IAQ / air quality": ("iaq", "air quality", "indoor environment", "air purifier", "uv", "scrubber"),
        "water heater": ("water heater", "tankless", "hot water"),
        "surge protection": ("surge",),
    }
    interest_phrases = re.compile(r"\b(shown interest|interested in|values?|invested in|receptive to)\b")
    interest_sentences = [s.lower() for s in re.split(r"(?<=[.!?])\s+|\n", ai_text or "") if interest_phrases.search(s.lower())]
    if interest_sentences:
        for cat, terms in category_terms.items():
            cat_data = intel.get("category_breakdown", {}) if intel else {}
            sold_d = ((cat_data.get("sold") or {}).get(cat) or {}).get("dollars", 0) or 0
            open_d = (((cat_data.get("open") or {}).get(cat) or {}).get("dollars", 0) or 0) + (((cat_data.get("expired") or {}).get(cat) or {}).get("dollars", 0) or 0)
            if sold_d == 0 and open_d > 0:
                for sentence in interest_sentences:
                    if any(t in sentence for t in terms):
                        violations.append(f"Interest/value overclaim: '{cat}' is backed only by OPEN/EXPIRED estimates. Phrase as quoted but never closed, not customer interest/values.")
                        break

    if "reliable buyer" in text_low:
        allowed = {str(x.get("category") or "").lower() for x in (intel.get("always_buys") or []) if x.get("count", 0) >= 2 and x.get("dollars", 0) >= 1000}
        if not allowed or not any(cat in text_low for cat in allowed):
            violations.append("Reliable buyer overclaim: only use 'Reliable buyer' for categories in always_buys with repeated sales; single/low-dollar purchases are not reliable patterns.")

    for opener in re.findall(r"(?im)^\s*Opener:\s*['\"].*?$", ai_text):
        if not _opener_grounded(opener, dossier):
            violations.append("History-anchored opener references a job/estimate/detail not found in ground truth.")

    return violations


def build_llm_context(facts: dict, dossier: dict, trade: str, cross_signals: list[str], intel: dict | None = None) -> dict:
    """Build prompt-fed context with explicit ceiling/trade/equipment evidence fields."""
    notes = recent_meaningful_notes(
        (dossier.get("job_notes") or []) + (dossier.get("location_notes") or []) + (dossier.get("customer_notes") or []),
        limit=12,
    )
    past = past_visit_lines(dossier.get("past_jobs") or [], limit=8)
    intel = intel or analyze_estimate_history(dossier.get("estimates") or [], dossier["job"].get("id"), primary_trade=trade)
    equipment_whitelist = _equipment_whitelist(dossier)
    likely_equipment = equipment_implied_by_history(dossier, equipment_whitelist)
    facts["equipment_likely_present_not_on_file"] = likely_equipment

    job = dossier["job"]
    is_commercial = facts.get("is_commercial", False)
    property_word = "property" if is_commercial else "home"
    person_word = "site contact" if is_commercial else "homeowner"

    return {
        "sales_enablement_principle": "This brief is a sales-enablement tool for a LEX technician. Your job is to help the tech raise average ticket and open cross-trade conversations using what we know about this customer, property, and history. The tech already knows how to perform the work. Do NOT output job procedures, PM checklists, or how-to steps. Every line must either (a) name a specific opportunity and how to frame it, or (b) name a specific thing to look for that OPENS an opportunity conversation. If a line would be true and useful with no brief at all, cut it.",
        "estimate_intent_state_rules": [
            "OPEN = we quoted it and have no customer decision; phrase as 'we quoted X and never closed it — warm, re-open it,' not interest.",
            "EXPIRED = it lapsed, often follow-up miss; phrase as lapsed/re-open, not customer interest.",
            "DISMISSED/DECLINED = customer said no; use soft-revisit framing and avoid identical ask.",
            "SOLD = customer bought/invested; only SOLD or explicit notes support 'values/interested/invested' language.",
        ],
        "system_scoped_history": facts.get("system_scoped_history") or system_scoped_history(dossier),
        "history_anchored_opener": facts.get("history_opener") or history_anchored_opener(dossier),
        "GROUND_TRUTH": {
            "appointment_local": facts["appt"],
            "scheduled_job_type": job.get("jobNumber") or "",
            "business_unit": job.get("businessUnitName") or "",
            "primary_trade_today": trade,
            "is_commercial_property": is_commercial,
            "property_word_to_use": property_word,
            "customer_word_to_use": person_word,
            "reason_for_visit": facts["reason"][:800],
            "installed_equipment_on_file": equipment_whitelist,
            "equipment_likely_present_not_on_file": likely_equipment,
            "home_age_or_build_year": facts["home_age"],
            "systems_on_file": facts["n_systems"],
            "equipment_systems_rollup": facts["equipment_counts"],
            "equipment_components_detail": facts.get("equipment_components") or [],
            "aging_equipment_10y_plus": facts["aging_units"],
            "membership_status": facts["membership"],
            "last_completed_visit": facts["last_visit"],
            "flags": facts["flags"],
        },
        "recent_notes": notes,
        "past_visits": past,
        "estimate_intel_facts": estimate_intel_facts(intel),
        "estimate_totals": intel["totals"],
        "estimate_price_ceiling": intel["price_ceiling"],
        "avg_sold_ticket": intel["averages"].get("avg_sold_ticket"),
        "opportunities_above_ceiling": intel.get("opportunities_above_ceiling") or [],
        "repeat_declines": [x for x in intel["repeat_declines"][:5] if x.get("trade") in (trade, "other")],
        "always_buys": [x for x in intel["always_buys"][:5] if x.get("trade") in (trade, "other")],
        "prior_single_buys": [x for x in intel.get("prior_single_buys", [])[:5] if x.get("trade") in (trade, "other")],
        "slow_yes_categories": intel["slow_yes_categories"][:5],
        "open_stale_high_value_categories": [x for x in intel["open_stale_high_value"][:5] if x.get("trade") == trade],
        "largest_stale_PRIMARY_TRADE": intel.get("largest_stale_primary_trade"),
        "largest_stale_ANY_TRADE": intel.get("largest_stale_any_trade"),
        # Backward-compatible key, now primary-trade only.
        "largest_single_stale_open_estimate": intel.get("largest_stale_primary_trade"),
        "cross_trade_signals_detected": cross_signals,
    }


def serialize_context_for_llm(context: dict, max_chars: int = 14000) -> str:
    """JSON serialize without blind mid-object cuts; trim low-priority lists with an omission marker."""
    work = json.loads(json.dumps(context, default=str))

    def dumps() -> str:
        return json.dumps(work, separators=(",", ":"), ensure_ascii=False)

    s = dumps()
    if len(s) <= max_chars:
        return s

    # Drop long contextual lists first; keep deterministic estimate summary intact as long as possible.
    for key in ("recent_notes", "past_visits"):
        if isinstance(work.get(key), list) and work[key]:
            omitted = len(work[key])
            work[key] = [f"[{omitted} {key.replace('_', ' ')} omitted for length]"]
            s = dumps()
            if len(s) <= max_chars:
                return s

    # Then trim estimate opportunity arrays from the tail and leave a clear marker.
    for key in ("opportunities_above_ceiling", "open_stale_high_value_categories", "repeat_declines", "always_buys"):
        if not isinstance(work.get(key), list):
            continue
        while len(dumps()) > max_chars and len(work[key]) > 1:
            work[key].pop()
        if len(dumps()) > max_chars and work[key]:
            omitted = len(work[key])
            work[key] = [f"[{omitted} estimates omitted for length]"]
        s = dumps()
        if len(s) <= max_chars:
            return s

    # Final fallback: remove verbose line items from stale estimate anchors, never cut JSON.
    for key in ("largest_stale_PRIMARY_TRADE", "largest_stale_ANY_TRADE", "largest_single_stale_open_estimate"):
        if isinstance(work.get(key), dict) and work[key].get("line_items"):
            n = len(work[key]["line_items"])
            work[key]["line_items"] = [f"[{n} estimates omitted for length]"]
            s = dumps()
            if len(s) <= max_chars:
                return s
    return dumps()




def _apply_offfile_equipment_verification_language(text: str, violations: list[str]) -> str:
    """Last-resort deterministic cleanup for supported-but-not-on-file equipment mentions."""
    out = text
    if any("Mention of water heater is supported" in v for v in violations):
        replacement = "Water heater/tankless is not on file; verify on arrival before discussing related estimates"
        out = re.sub(r"Verify the presence of a water heater[^.\n;]*", replacement, out, flags=re.I)
        out = re.sub(r"Verify the water heater[^.\n;]*", replacement, out, flags=re.I)
        if "water heater" in out.lower() and not re.search(r"water heater[^\n.]*not on file[^\n.]*verify", out, re.I):
            out += "\n- Cross-trade verification: water heater/tankless is not on file; verify on arrival before discussing related estimates."
    elif any("Removed mention of water heater" in v for v in violations):
        # If neither installed equipment nor history supports water-heater/tankless context, strip the sentence entirely.
        out = re.sub(r"[^.\n]*(?:water heater|tankless)[^.\n]*\. ?", "", out, flags=re.I)
        out = re.sub(r"\n?[-•]\s*[^\n]*(?:water heater|tankless)[^\n]*", "", out, flags=re.I)
    if any("Mention of panel is supported" in v for v in violations):
        replacement = "Electrical panel is not on file; verify brand, age, and condition on arrival"
        out = re.sub(r"Verify the presence and condition of the electrical panel[^.\n;]*", replacement, out, flags=re.I)
        if "panel" in out.lower() and not re.search(r"panel[^\n.]*not on file[^\n.]*verify", out, re.I):
            out += "\n- Electrical verification: electrical panel is not on file; verify brand, age, and condition on arrival."
    if any("Customer HAS estimate history" in v for v in violations):
        # Remove forbidden fallback language if the LLM included it despite non-zero totals.
        out = re.sub(r"\n?[-•]?\s*New customer[^\n.]*no estimate history[^\n.]*[.\n]?", "\n", out, flags=re.I)
        out = re.sub(r"New customer[^\n.]*no estimate history[^\n.]*\. ?", "", out, flags=re.I)
        out = re.sub(r"no estimate history in this trade", "estimate history is mostly cross-trade", out, flags=re.I)
    if any("Removed month" in v or "Removed date" in v for v in violations):
        # Drop narrative sentences that imported old/reschedule dates from notes.
        out = re.sub(
            r"[^.\n]*(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[^.\n]*\. ?",
            "",
            out,
            flags=re.I,
        )
    if any("Cross-trade leads contains real leads" in v for v in violations):
        out = _strip_cross_trade_none_obvious_when_real_leads(out)
    return out


def openrouter_brief(facts: dict, dossier: dict, trade: str, cross_signals: list[str]) -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return ""
    intel = analyze_estimate_history(dossier.get("estimates") or [], dossier["job"].get("id"), primary_trade=trade)
    facts["_intel"] = intel
    context = build_llm_context(facts, dossier, trade, cross_signals, intel)

    appt = dossier.get("appointment")
    appt_iso = (appt or {}).get("start") or ""
    property_word = "property" if facts.get("is_commercial", False) else "home"
    trade_focus = {
        "plumbing": "Speak to a plumber. Center on water heater, drains, supply lines, fixtures, leaks, pressure, water quality, PSI checklist.",
        "electrical": "Speak to an electrician. Center on panel age/brand, breaker condition, grounding/bonding, GFCI/AFCI, surge, EV readiness, ESI checklist.",
        "hvac": "Speak to an HVAC technician. Center on system count, equipment age, refrigerant type, recurring issues, IAQ, ducts, maintenance items.",
    }[trade]
    trade_section_title = {"hvac": "HVAC opportunities", "plumbing": "Plumbing opportunities", "electrical": "Electrical opportunities"}[trade]

    sys_msg = (
        "This brief is a sales-enablement tool for a LEX technician. Your job is to help the tech raise average ticket and open cross-trade conversations using what we know about this customer, property, and history. The tech already knows how to perform the work. Do NOT output job procedures, PM checklists, or how-to steps. Every line must either (a) name a specific opportunity and how to frame it, or (b) name a specific thing to look for that OPENS an opportunity conversation. If a line would be true and useful with no brief at all, cut it.\n\n"
        "You write a pre-visit brief for a LEX Air Conditioning field technician. "
        f"PRIMARY TRADE: {trade.upper()}. {trade_focus} "
        "Tone: direct, operator-focused, no fluff, no emojis, no markdown headers.\n\n"
        "GROUND TRUTH RULES (non-negotiable):\n"
        "1. The 'GROUND_TRUTH' object is authoritative. Never contradict it.\n"
        "2. Use ONLY equipment types listed in GROUND_TRUTH.installed_equipment_on_file as installed equipment. If equipment appears in GROUND_TRUTH.equipment_likely_present_not_on_file, you may mention it ONLY as 'not on file, verify on arrival.' NEVER imply it is installed/on file.\n"
        "3. Use GROUND_TRUTH.equipment_systems_rollup as the authoritative HVAC system count; do not double-count components.\n"
        "4. Use the property word from GROUND_TRUTH.property_word_to_use ('home' OR 'property'). If property_word_to_use is 'property', NEVER use 'home', 'house', 'homeowner', or 'residence'.\n"
        "5. Never invent dates. The only date you may reference is GROUND_TRUTH.appointment_local. Treat dates in notes or past_visits as context only.\n"
        "6. Never claim past service requests for trades unless past_visits clearly contains them.\n"
        "7. If scheduled_job_type or business_unit indicates Install / Repair / Replace, do NOT call this a maintenance visit.\n"
        "8. If estimate_totals shows any sold/open/dismissed/expired > 0, the customer HAS estimate history — never say no estimate history or new customer.\n"
        "9. If GROUND_TRUTH.flags contains a DATA QUALITY warning about equipment ages, treat equipment ages as suspect and do not anchor the read on them.\n\n"
        "Output exactly SIX sections separated by blank lines:\n\n"
        f"1) A single tight paragraph (3-5 sentences) for a {trade} technician. Summarize the {property_word}, what's installed, membership status, and what recent history says about this visit. Lead with sales-relevant {trade} facts and avoid unsupported interest/value claims.\n\n"
        "2) Include the exact history_anchored_opener line if present. It must start with 'Opener:' and be grounded in past_visits or estimates. Omit if empty.\n\n"
        f"3) Line starting with 'Go win the call:' then max 2-3 short bullets. Each bullet must be an opportunity setup trigger, not a task. If a line names a diagnostic step without tying it to a dollar opportunity, estimate, or cross-trade opening, drop it. BANNED as standalone: wash condensers, flush drains, measure delta-T, superheat/subcool, test float switches, PM checklist language.\n\n"
        "4) Line starting with 'Sales coach:' then 2-4 SHORT bullets about BUYING BEHAVIOR. Use estimate_price_ceiling, avg_sold_ticket, opportunities_above_ceiling, repeat_declines, always_buys, prior_single_buys, slow_yes_categories, and largest_stale_PRIMARY_TRADE. Sales coach is PRIMARY-TRADE ONLY: do not recommend or re-quote off-trade categories here. Coaching MUST reconcile against the ceiling: if a recommended estimate exceeds about 2x the price ceiling, frame it as a stretch using lowest-dollar high-value item, financing, or phasing — not a flat bare re-quote. Intent-state wording is mandatory: OPEN/EXPIRED means 'we quoted it and never closed it — warm re-open,' not 'customer is interested'; DISMISSED means soft-revisit/changed ask; SOLD supports bought/invested language. 'Reliable buyer' requires a repeated buy pattern from always_buys; prior_single_buys is not reliable. If no estimate history at all, write 'Sales coach: New customer — no estimate history. Focus on diagnostic credibility, set the price ceiling with one strong recommendation.'\n\n"
        f"5) Line starting with '{trade_section_title}:' then 2-4 short bullets covering {trade} upsells, renewals, IAQ/replacement/follow-up specific to this trade. Fuse diagnostic details to the opportunity as trigger→pitch pairs: if you see X, it opens Y, backed by Est #/history where present. If a customer has declined something before, name it so the tech doesn't repeat the same ask. Use system_scoped_history; if a system/zone is not specified, say 'system not specified, verify' instead of implying whole-home.\n\n"
        "6) Line starting with 'Cross-trade leads:' then 2-3 short bullets for other-trade opportunities. Use cross_trade_signals_detected and largest_stale_ANY_TRADE when it is off-trade. Off-file equipment must say 'not on file, verify on arrival.' Use trigger→pitch framing, not generic inspection checklists. The empty-state fallback is mutually exclusive: write 'Cross-trade leads: None obvious — check during walkthrough.' ONLY when cross_trade_signals_detected is empty AND there is no equipment_likely_present_not_on_file routed to cross-trade; never append 'None obvious' after any real cross-trade bullet."
    )

    text = _call_llm(key, sys_msg, context)
    violations = validate_narrative(text, dossier, facts, appt_iso[:10], intel)
    if violations:
        correction = "Your previous draft had these problems. Rewrite the brief to fix ALL of them:\n- " + "\n- ".join(violations)
        text = _call_llm(key, sys_msg + "\n\nIMPORTANT CORRECTIONS:\n" + correction, context)
        violations2 = validate_narrative(text, dossier, facts, appt_iso[:10], intel)
        if violations2:
            text = _apply_offfile_equipment_verification_language(text, violations2)
            violations2 = validate_narrative(text, dossier, facts, appt_iso[:10], intel)
        if violations2:
            text += "\n\n[DATA INTEGRITY] Narrative may contain inaccuracies — auto-checks flagged: " + "; ".join(violations2)
    return text

def _call_llm(key: str, sys_msg: str, context: dict) -> str:
    payload = {
        "model": os.environ.get("LEX_BRIEF_MODEL", "openai/gpt-4o"),
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": serialize_context_for_llm(context)},
        ],
        "temperature": 0.15,
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://lexairconditioning.com", "X-Title": "LEX Tech Lean Brief"},
    )
    try:
        with request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"].strip()
    except HTTPError as exc:
        return f"(LLM error: {exc.code} {exc.read().decode('utf-8','replace')[:200]})"
    except Exception as exc:
        return f"(LLM error: {exc})"



def _money(v: float | int | None) -> str:
    try:
        return f"${float(v):,.0f}"
    except Exception:
        return "$0"


def _first_nonempty(*vals: str) -> str:
    for v in vals:
        if v:
            return v
    return ""


def _arrival_window(appt: dict | None) -> str:
    if not appt:
        return "Unscheduled"
    try:
        ws = datetime.fromisoformat(appt["arrivalWindowStart"].replace("Z", "+00:00")).astimezone(CENTRAL)
        we = datetime.fromisoformat(appt["arrivalWindowEnd"].replace("Z", "+00:00")).astimezone(CENTRAL)
        return f"{ws.strftime('%-I:%M %p')} to {we.strftime('%-I:%M %p')} CT"
    except Exception:
        return fmt_appt_local(appt)


def _home_built_and_age(facts: dict) -> tuple[str, str]:
    raw = str(facts.get("home_age") or "")
    year = None
    try:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", raw)
        if m:
            year = int(m.group(1))
    except Exception:
        year = None
    if not year:
        year = facts.get("home_built_year") or facts.get("cad_home_built_year")
    if year:
        age = max(0, datetime.now(CENTRAL).year - int(year))
        return str(year), str(age)
    if raw:
        return raw, "unknown"
    return "unknown", "unknown"


def _text_blob(dossier: dict) -> str:
    parts = []
    job = dossier.get("job") or {}
    parts.append(strip_html(job.get("summary") or ""))
    for coll in ("job_notes", "location_notes", "customer_notes"):
        for n in dossier.get(coll) or []:
            parts.append(strip_html(n.get("text") or ""))
    for j in dossier.get("past_jobs") or []:
        parts.append(strip_html(j.get("summary") or ""))
        parts.append(strip_html(j.get("summaryOfWork") or ""))
    return "\n".join(p for p in parts if p)


def condition_triggers(dossier: dict) -> list[dict]:
    """Find historical/photo condition triggers the tech should verify before pitching."""
    blob = _text_blob(dossier)
    triggers = []
    patterns = [
        (r"photo|picture|image", "historical photo evidence"),
        (r"dirty\s+(?:evap(?:orator)?\s+)?coil|coil\s+(?:dirty|matted|impacted)", "dirty coil noted/photoed"),
        (r"dirty\s+blower|blower\s+(?:dirty|wheel|dust|buildup)", "dirty blower noted/photoed"),
        (r"duct\s+(?:dust|dirty|leak|leaking|seal|mold|growth)|return\s+(?:dust|dirty)", "duct/return condition noted"),
        (r"drain\s+(?:clog|backup|clear|overflow|pan)|float\s+switch", "drain/pan concern noted"),
        (r"iaq|air\s+quality|allerg|uv|reme|scrubber|purif", "IAQ/allergy history noted"),
        (r"weak\s+airflow|low\s+airflow|static\s+pressure", "airflow/static concern noted"),
    ]
    low = blob.lower()
    for pat, label in patterns:
        if re.search(pat, low, re.I):
            if "coil" in label:
                check = "coil, blower, drain pan"
                opp = "coil/blower cleaning, drain repair, IAQ if supported by findings"
            elif "blower" in label:
                check = "blower wheel and plenum"
                opp = "plenum-blower clean, IAQ refresh"
            elif "duct" in label or "return" in label:
                check = "return, duct interior, duct connections"
                opp = "duct clean/seal, plenum-blower clean, IAQ"
            elif "drain" in label:
                check = "drain, pan, safety switch, water marks"
                opp = "drain repair, safety switch, pan treatment"
            elif "IAQ" in label:
                check = "filter, return dust, coil/blower cleanliness, IAQ equipment"
                opp = "IAQ refresh or staged air-quality option"
            elif "airflow" in label:
                check = "static pressure, return sizing, blower, filter setup"
                opp = "airflow correction, duct/return improvements"
            else:
                check = "photos/notes condition on arrival"
                opp = "only pitch if condition is still present"
            triggers.append({"label": label, "check": check, "opportunity": opp})
    # Deduplicate by label while preserving order.
    seen = set(); out = []
    for t in triggers:
        if t["label"] not in seen:
            seen.add(t["label"]); out.append(t)
    return out[:3]


def _open_estimate_rows(dossier: dict, current_job_id: int | None = None) -> list[dict]:
    rows = []
    for e in dossier.get("estimates") or []:
        if current_job_id and e.get("jobId") == current_job_id:
            continue
        status = ((e.get("status") or {}).get("name") or "").lower()
        if status != "open":
            continue
        subtotal = float(e.get("subtotal") or 0)
        item_names = [r["name"] for r in _estimate_line_items(e) if r.get("name")][:4]
        rows.append({
            "id": e.get("id"),
            "subtotal": subtotal,
            "name": (e.get("name") or "").strip() or "Open estimate",
            "created_on": (e.get("createdOn") or "")[:10],
            "age_days": _days_since(e.get("createdOn")),
            "category": _estimate_primary_category(e),
            "trade": _category_trade(_estimate_primary_category(e)),
            "line_items": item_names,
        })
    rows.sort(key=lambda r: r["subtotal"], reverse=True)
    return rows


def opportunity_score(dossier: dict, facts: dict, intel: dict, trade: str) -> dict:
    """0-100 sales-potential score from Ryan's brief rubric. Measures potential, not promise."""
    all_open_rows = _open_estimate_rows(dossier, dossier.get("job", {}).get("id"))
    primary_open_rows = [r for r in all_open_rows if r.get("trade") == trade]
    # For the tech-facing HVAC/primary-trade brief, only primary-trade estimates become reopen targets.
    # Off-trade estimates are handled in Cross-Trade Handoff so we don't tell an HVAC tech to pitch plumbing.
    open_rows = primary_open_rows
    largest_open = open_rows[0] if open_rows else None
    score = 0
    parts: dict[str, int] = {}
    signals: list[str] = []

    # 1) Open Estimate Signals — 30 pts
    open_pts = 0
    if largest_open:
        if largest_open["subtotal"] >= 7500:
            open_pts += 12
        elif largest_open["subtotal"] >= 2500:
            open_pts += 8
        elif largest_open["subtotal"] > 0:
            open_pts += 4
        signals.append(f"{_money(largest_open['subtotal'])} open {largest_open['category']} estimate")
        if largest_open.get("trade") == trade:
            open_pts += 12
            signals.append("open estimate matches today's trade")
    if len(open_rows) > 1:
        open_pts += 6
        signals.append(f"{len(open_rows)} open estimates")
    parts["open_estimate_signals"] = min(open_pts, 30)

    # 2) Buying History — 25 pts
    buy_pts = 0
    t = intel.get("totals") or {}
    sold_count = int(t.get("sold_count") or 0)
    denom = sold_count + int(t.get("open_count") or 0) + int(t.get("dismissed_count") or 0) + int(t.get("expired_count") or 0)
    close_rate = (sold_count / denom) if denom else 0
    if denom:
        if close_rate >= 0.60:
            buy_pts += 10
            signals.append(f"{close_rate:.0%} lifetime close rate")
        elif close_rate >= 0.30:
            buy_pts += 6
        else:
            buy_pts += 2
    if intel.get("always_buys"):
        buy_pts += 8
        signals.append(f"repeat {intel['always_buys'][0]['category']} buyer")
    avg_sold = float((intel.get("averages") or {}).get("avg_sold_ticket") or 0)
    if avg_sold >= 1500:
        buy_pts += 7
        signals.append(f"avg sold ticket {_money(avg_sold)}")
    parts["buying_history"] = min(buy_pts, 25)

    # 3) Equipment & Home — 20 pts
    equip_pts = 0
    ages = []
    for s in facts.get("aging_units") or []:
        m = re.search(r"~(\d+)y", s)
        if m:
            ages.append(int(m.group(1)))
    primary_age = max(ages) if ages else 0
    if primary_age >= 15:
        equip_pts += 10
        signals.append(f"{primary_age}-yr equipment")
    elif primary_age >= 10:
        equip_pts += 6
        signals.append(f"{primary_age}-yr equipment")
    try:
        system_count = sum(int(m.group(1)) for x in facts.get("equipment_counts") or [] for m in [re.search(r"(\d+)\s*×", x)] if m)
    except Exception:
        system_count = 0
    built, age = _home_built_and_age(facts)
    older_home = str(age).isdigit() and int(age) >= 20
    if system_count >= 2 or older_home:
        equip_pts += 5
        signals.append("multiple systems" if system_count >= 2 else f"{age}-yr home")
    if intel.get("open_stale_high_value") or intel.get("largest_stale_primary_trade"):
        equip_pts += 5
        signals.append("deferred/recommended repair on file")
    parts["equipment_home"] = min(equip_pts, 20)

    # 4) Condition & Photo Triggers — 15 pts. Current-job points are reserved for onsite recompute.
    cond = condition_triggers(dossier)
    cond_pts = 5 if cond else 0
    if cond:
        signals.append(cond[0]["label"])
    parts["condition_photo_triggers"] = cond_pts

    # 5) Membership & Cross-Trade — 10 pts
    mem_pts = 0
    mem = (facts.get("membership") or "").lower()
    tenure_match = re.search(r"(\d+)\s*y", mem)
    is_active_recurring_member = (mem.startswith("active") or " active" in mem) and "onetime" not in mem
    if is_active_recurring_member:
        avg_sold = float((intel.get("averages") or {}).get("avg_sold_ticket") or 0)
        if avg_sold < 1500 or (tenure_match and int(tenure_match.group(1)) >= 2):
            mem_pts += 5
            signals.append("active member under-monetized")
    if facts.get("cross_signals"):
        mem_pts += 5
        signals.append("cross-trade evidence")
    parts["membership_cross_trade"] = min(mem_pts, 10)

    score = min(100, sum(parts.values()))
    tier = "🔴 PRIORITY" if score >= 70 else ("🟡 ELEVATED" if score >= 45 else "⚪️ STANDARD")
    return {"score": score, "tier": tier, "signals": signals[:6], "parts": parts, "largest_open": largest_open, "open_rows": open_rows, "condition_triggers": cond}


def _best_fit_offer(scorecard: dict, intel: dict, trade: str) -> str:
    largest = scorecard.get("largest_open") or intel.get("largest_stale_primary_trade") or intel.get("largest_stale_any_trade")
    if largest:
        cat = largest.get("category") or "open estimate"
        name = largest.get("name") or cat
        if trade == (largest.get("trade") or trade):
            return f"Reopen {name} ({_money(largest.get('subtotal'))}) if today's findings still support it."
        return f"Do today's work first; if walkthrough supports it, hand off {name} ({_money(largest.get('subtotal'))})."
    if trade == "hvac":
        return "Use verified findings to offer the smallest credible HVAC improvement: cleaning, drain safety, IAQ refresh, or repair option."
    return "Use verified findings to make one specific repair or safety recommendation."


def _say_line(scorecard: dict, facts: dict, trade: str) -> str:
    largest = scorecard.get("largest_open")
    if largest:
        return f"We had quoted {largest.get('name')} before and it never closed. If today's issue points back to that, I can refresh it and give you a practical starting point."
    trig = (scorecard.get("condition_triggers") or [{}])[0]
    if trig:
        return f"I saw this was noted before: {trig['label']}. If it is still present today, I can show you the cleanest way to handle it without overbuilding the ticket."
    return "Once I verify what is actually happening today, I will show you the must-do fix first and any optional improvements separately."


def render_opportunity_markdown(dossier: dict, ai_text: str, facts: dict, lookups: dict) -> str:
    job = dossier["job"]
    jt_name = lookups["job_types"].get(job.get("jobTypeId"), str(job.get("jobTypeId") or ""))
    bu_name = lookups["business_units"].get(job.get("businessUnitId"), str(job.get("businessUnitId") or ""))
    trade = facts.get("trade") or trade_from_jobtype(jt_name, bu_name)
    intel = facts.get("_intel") or analyze_estimate_history(dossier.get("estimates") or [], job.get("id"), primary_trade=trade)
    scorecard = opportunity_score(dossier, facts | {"cross_signals": facts.get("cross_signals") or []}, intel, trade)
    built, age = _home_built_and_age(facts)
    signals = scorecard["signals"][:3] or ["verify findings", "protect customer experience", "log opportunities"]
    equipment = "; ".join((facts.get("equipment_counts") or []) + (facts.get("equipment_components") or [])[:4]) or "No installed equipment on file — verify on arrival"
    last_visit = facts.get("last_visit") or "No completed prior visit found in pulled history."
    history = []
    for h in facts.get("system_scoped_history") or []:
        clean = re.sub(r"^[-•]\s*", "", h).strip()
        if clean and clean not in history:
            history.append(clean)
    if not history:
        for n in recent_meaningful_notes((dossier.get("job_notes") or []) + (dossier.get("location_notes") or []) + (dossier.get("customer_notes") or []), 3):
            history.append(re.sub(r"^[-•]\s*", "", n).strip())
    history = history[:3]

    open_rows = scorecard.get("open_rows") or []
    largest_open = scorecard.get("largest_open")
    largest_stale = intel.get("largest_stale_primary_trade") or intel.get("largest_stale_any_trade") or largest_open
    ceiling = float(intel.get("price_ceiling") or 0)
    avg_sold = float((intel.get("averages") or {}).get("avg_sold_ticket") or 0)
    target_amount = float((largest_open or largest_stale or {}).get("subtotal") or 0)
    ceiling_tag = "No prior buying ceiling" if not ceiling else ("Phase it" if target_amount and target_amount > 1.5 * ceiling else "In range")

    md = []
    md.append(f"# LEX / Lyons Tech Brief · {jt_name}")
    md.append("")
    md.append(f"**SCORE:** {scorecard['score']} {scorecard['tier']} Top signals: {' · '.join(signals)}")
    md.append("")
    job_number = job.get("jobNumber") or job.get("number") or job.get("id") or ""
    md.append(f"**Job #:** {job_number}  **Job:** {jt_name} · {trade.upper()} · {bu_name}  **Arrival:** {_arrival_window(dossier.get('appointment'))}")
    md.append(f"**Customer:** {facts.get('customer') or ''} · {facts.get('membership') or 'Membership unknown'}")
    md.append(f"**Address:** {facts.get('address') or ''} · built {built} ({age} yrs)")
    md.append("")
    md.append("## SYSTEM ON FILE")
    md.append(equipment)
    md.append("")
    md.append("## LAST VISIT")
    md.append(last_visit)
    md.append("")
    md.append("## HISTORY / NOTES")
    if history:
        md.extend(f"- {h}" for h in history)
    else:
        md.append("- No relevant history notes found in pulled data.")
    md.append("")
    md.append("## 💰 ESTIMATE INTELLIGENCE")
    if open_rows:
        md.append("**Open quoted, never closed — reopen targets**")
        for r in open_rows[:3]:
            desc = r.get("name") or r.get("category") or "Open estimate"
            date = r.get("created_on") or "date unknown"
            est_id = f" Est #{r['id']}" if r.get("id") else ""
            md.append(f"- **{_money(r['subtotal'])}:** {desc}{est_id} · {date}")
    else:
        md.append("**Open quoted, never closed:** none found")
    if largest_stale:
        md.append("")
        md.append("**Largest stale opportunity**")
        md.append(f"- **Amount:** {_money(largest_stale.get('subtotal'))}")
        md.append(f"- **Target:** {largest_stale.get('name') or largest_stale.get('category')}")
        md.append(f"- **Category:** {largest_stale.get('category') or 'uncategorized'}")
    else:
        md.append("**Largest stale opportunity:** none found")
    md.append("")
    md.append("**Customer buying pattern**")
    md.append(f"- **Buying ceiling:** {_money(ceiling)} largest prior sold → {ceiling_tag}")
    md.append(f"- **Avg sold ticket:** {_money(avg_sold)}")
    md.append("")
    md.append("## 🔍 VERIFY TODAY")
    triggers = scorecard.get("condition_triggers") or []
    if triggers:
        t = triggers[0]
        md.append(f"**Trigger:** {t['label']} → check {t['check']}")
        md.append(f"**If still present → opportunity:** {t['opportunity']}")
    elif largest_open:
        md.append(f"**Trigger:** open estimate on file → check whether today's issue supports {largest_open.get('category')}")
        md.append("**If still present → opportunity:** reopen the estimate with current findings and fresh pricing")
    else:
        md.append("**Trigger:** no specific historical condition found → verify age, cleanliness, airflow, drain safety, and customer comfort concern")
        md.append("**If still present → opportunity:** make one specific recommendation and document future options")
    md.append("")
    md.append("## 🎯 LIKELY TO BUY + WHAT TO SAY")
    md.append(f"**Best-fit offer:** {_best_fit_offer(scorecard, intel, trade)}")
    md.append(f"**Say:** \"{_say_line(scorecard, facts, trade)}\"")
    md.append("")
    md.append("## 🔧 CROSS-TRADE HANDOFF")
    cross = facts.get("cross_signals") or []
    if cross:
        for c in cross[:3]:
            target = "Plumbing" if "plumb" in c.lower() or "water" in c.lower() else ("Electrical" if "electric" in c.lower() or "panel" in c.lower() or "surge" in c.lower() else "appropriate trade")
            md.append(f"- **Evidence:** {c} → loop in {target}")
    else:
        md.append("None flagged this visit")
    if facts.get("flags"):
        md.append("")
        md.append("## HEADS UP")
        md.extend(f"- {f}" for f in facts["flags"])
    md.append("")
    md.append("---")
    md.append("Score measures sales potential, not a promise. Do great work first. Verify all findings on site before offering anything.")
    return "\n".join(md).strip()


def render_lean_markdown(dossier: dict, ai_text: str, facts: dict, lookups: dict) -> str:
    return render_opportunity_markdown(dossier, ai_text, facts, lookups)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: rebuild_lean_briefs.py <existing_brief_dir>")
    src = Path(sys.argv[1])
    if not src.exists():
        raise SystemExit(f"Dir not found: {src}")

    out_dir = src.parent / (src.name + "_lean")
    out_dir.mkdir(exist_ok=True)

    json_files = sorted(p for p in src.glob("*.json") if p.name != "index.json")
    for jf in json_files:
        bundle = json.loads(jf.read_text())
        dossier = bundle.get("dossier") or bundle
        meta = bundle.get("meta") or {}
        # Synthesize a lookup from meta
        lookups = {
            "job_types": {dossier["job"].get("jobTypeId"): meta.get("job_type", "")},
            "business_units": {dossier["job"].get("businessUnitId"): meta.get("business_unit", "")},
        }
        facts = build_facts_block(dossier)
        trade = trade_from_jobtype(meta.get("job_type", ""), meta.get("business_unit", ""))
        # Compute estimate intel up front so cross_trade_signals can use behavioral patterns
        facts["_intel"] = analyze_estimate_history(dossier.get("estimates") or [], dossier["job"].get("id"), primary_trade=trade)
        cross = cross_trade_signals(facts, dossier, trade)
        facts["trade"] = trade
        facts["cross_signals"] = cross
        ai = openrouter_brief(facts, dossier, trade, cross) if os.environ.get("LEX_BRIEF_USE_LLM") == "1" else ""
        md = render_lean_markdown(dossier, ai, facts, lookups)
        out_path = out_dir / jf.with_suffix(".md").name
        out_path.write_text(md)
        print("wrote", out_path)

    print(out_dir)


if __name__ == "__main__":
    main()
