#!/usr/bin/env python3
"""Score HVAC tech-brief dossiers for opportunity potential and pick the ones worth
sending an email report card on. Pure-deterministic scoring — no LLM calls. This
scorer is the gate that decides which jobs get LLM report-card synthesis spend.

Outputs scoring TSV + a JSON list of the chosen job IDs. Pair with
scripts/log_outcomes.py, which joins these scores against realized revenue after
the visits so the weights below can be calibrated against reality.

Heuristics (tweakable — see src/opportunity_flags.py for the age-tier rules):
- Equipment past per-class replacement threshold → +25 (urgent tier → +35)
- Home-age tier: 10-30 yrs → +5, 30-45 yrs → +10, 45+ yrs → +15
- Demand/problem call intent → +25 and tie-breaks ahead of maintenance
- Active membership → +10
- Open estimate context → +0 to +3 tie-breaker only
- Sold estimate history ≥ $10k → +15
- Valuable photo findings are added after vision analysis, not from photo availability alone
- Booking-note repair recommendation keywords → +10
- "Older system" booking-note age signal (no equipment dates) → +20/+30
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_flags import (  # noqa: E402
    classified_equipment_score,
    classify_aged_equipment,
    home_age_flags,
    home_age_score,
)
from report_card_facts import build_facts_block  # noqa: E402
from opportunity_snapshot import is_excluded_opportunity_call, call_intent_type  # noqa: E402


def _status(e):
    s = e.get("status")
    if isinstance(s, dict):
        return (s.get("name") or "").strip().lower()
    return str(s or "").strip().lower()


def _money(rows, status_filter):
    return sum(float(r.get("subtotal") or 0) for r in rows if _status(r) == status_filter)


YEAR_RE = re.compile(r"(20\d{2}|19\d{2})")


def _home_built_year(dossier):
    """Resolved home build year using the same fact builder/CAD chain as report cards."""
    try:
        facts = build_facts_block(dossier)
        year = facts.get("home_built_year") or facts.get("cad_home_built_year")
        if year:
            return int(year)
    except Exception:
        pass
    location = dossier.get("location") or {}
    for cf in location.get("customFields") or []:
        name = (cf.get("name") or "").lower()
        val = str(cf.get("value") or "")
        if not val:
            continue
        if "age of home" in name or "year built" in name:
            m = YEAR_RE.search(val)
            if m:
                return int(m.group(1))
    return None


REPAIR_KEYWORDS = re.compile(
    r"recommended (hvac )?repairs|leaking|leak|capacitor|fan motor|blower motor|compressor|coil|"
    r"replace|biological growth|auv|uv|amp|over[- ]?amp|short cycling|not cool|not heat|warm air|"
    r"freezing|froze|frozen|burning smell|loud noise|noisy",
    re.I,
)
OLDER_KEYWORDS = re.compile(r"(\d{1,2})\s*(?:yrs|years)\s*(?:old)?", re.I)


def score_bundle(bundle, photos_have_sheet):
    dossier = bundle.get("dossier") or bundle
    meta = bundle.get("meta") or {}
    job = dossier.get("job") or {}
    summary = str(job.get("summary") or "")
    memberships = dossier.get("memberships") or []
    estimates = dossier.get("estimates") or []
    job_type = meta.get("job_type") or ""
    business_unit = meta.get("business_unit") or ""
    excluded = is_excluded_opportunity_call(job_type, summary, business_unit)

    score = 0
    drivers = []

    intent_type = call_intent_type(job_type, summary)
    if intent_type == "demand":
        score += 25
        drivers.append("demand/problem call (+25)")
    elif intent_type == "maintenance":
        drivers.append("maintenance call (+0, needs supporting signals)")

    # Supersession-aware equipment scoring: aged records that were likely already
    # replaced (stale ST records next to a recent install) score 0 instead of
    # producing false aged-equipment points; ambiguous ones score a reduced 10.
    classification = classify_aged_equipment(dossier)
    known_ages = [
        u["age"] for u in classification["units"]
        if u["age"] is not None and u.get("supersession") != "superseded"
    ]
    max_age = max(known_ages, default=None)
    flag_points, flag_note, stale_count = classified_equipment_score(classification)
    if flag_points:
        score += flag_points
        drivers.append(flag_note)
    if stale_count:
        drivers.append(f"{stale_count} aged record(s) likely superseded, ignored")

    if not any(u["age"] is not None for u in classification["units"]):
        m = OLDER_KEYWORDS.search(summary)
        if m:
            try:
                yrs = int(m.group(1))
                if yrs >= 15:
                    score += 30
                    drivers.append(f"notes ~{yrs}y old")
                elif yrs >= 10:
                    score += 20
                    drivers.append(f"notes ~{yrs}y old")
            except Exception:
                pass

    built_year = _home_built_year(dossier)
    home_flags = home_age_flags(built_year)
    home_points = home_age_score(home_flags)
    if home_points:
        score += home_points
        drivers.append(f"home built {built_year} (+{home_points})")

    active_m = any(str(m.get("status") or "").lower() == "active" for m in memberships)
    if active_m:
        score += 10
        drivers.append("active membership")

    open_rows = [e for e in estimates if _status(e) == "open"]
    sold_rows = [e for e in estimates if _status(e) == "sold"]
    open_total = sum(float(e.get("subtotal") or 0) for e in open_rows)
    open_bump = 0
    if open_rows and (flag_points or intent_type == "demand"):
        open_bump = 1 if open_total < 10000 else 2 if open_total < 30000 else 3
        score += open_bump
        drivers.append(f"open quote context only (+{open_bump})")
    elif open_rows:
        drivers.append(f"{len(open_rows)} open quote(s), no score")
    sold_total = sum(float(e.get("subtotal") or 0) for e in sold_rows)
    if sold_total >= 10000:
        score += 15
        drivers.append(f"sold ${int(sold_total)}")

    if photos_have_sheet:
        drivers.append("photos available, vision findings score separately")

    if REPAIR_KEYWORDS.search(summary) and intent_type != "demand":
        score += 10
        drivers.append("note: repair signals (+10)")

    return {
        "job_number": str(job.get("jobNumber") or job.get("id") or ""),
        "job_type": meta.get("job_type") or "",
        "customer": meta.get("customer") or (dossier.get("customer") or {}).get("name") or "",
        "score": score,
        "drivers": ", ".join(drivers),
        "has_photos": photos_have_sheet,
        "max_equip_age": max_age,
        "home_built_year": built_year,
        "open_estimate_count": len(open_rows),
        "open_estimate_total": int(open_total),
        "sold_estimate_total": int(sold_total),
        "active_membership": active_m,
        "call_intent": intent_type,
        "excluded": excluded,
        "excluded_reason": "warranty/recall/QC/callback/recent-install/follow-up/tech-lead" if excluded else "",
    }


def main(run_dir: Path, manifest_path: Path, threshold: int = 35, top_n: int = 10, exclude_patterns: list[str] | None = None):
    manifest = json.loads(manifest_path.read_text())
    by_job = {str(item["job_id"]): item for item in manifest["manifest"]}
    exclude_re = re.compile("|".join(re.escape(p) for p in (exclude_patterns or [])), re.I) if exclude_patterns else None

    scored = []
    excluded = []
    for jf in sorted(run_dir.glob("*.json")):
        if jf.name == "index.json":
            continue
        bundle = json.loads(jf.read_text())
        dossier = bundle.get("dossier") or bundle
        job_id = str((dossier.get("job") or {}).get("id"))
        has_sheet = bool(by_job.get(job_id, {}).get("contact_sheet"))
        rec = score_bundle(bundle, has_sheet)
        rec["dossier_json"] = str(jf)
        rec["job_id"] = job_id
        if rec.get("excluded"):
            excluded.append(rec)
            continue
        if exclude_re and exclude_re.search(rec.get("job_type", "")):
            rec["excluded_reason"] = "call-type filter"
            excluded.append(rec)
            continue
        scored.append(rec)

    intent_priority = {"demand": 0, "standard": 1, "maintenance": 2}
    scored.sort(key=lambda r: (-r["score"], intent_priority.get(r.get("call_intent"), 1), r["job_number"]))

    out_dir = run_dir.parent / (run_dir.name + "_scoring")
    out_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = out_dir / "scores.tsv"
    fields = [
        "job_number", "score", "job_type", "customer", "drivers", "has_photos",
        "max_equip_age", "home_built_year", "open_estimate_count", "open_estimate_total",
        "sold_estimate_total", "active_membership", "call_intent", "excluded_reason", "job_id", "dossier_json",
    ]
    with tsv_path.open("w") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
        w.writeheader()
        for r in scored:
            w.writerow({k: r.get(k, "") for k in fields})

    # Select for emailing: above threshold AND at most top_n
    selected = [r for r in scored if r["score"] >= threshold][:top_n]
    (out_dir / "selected.json").write_text(json.dumps(selected, indent=2))

    print(f"Scored {len(scored)} jobs. Excluded by call-type filter: {len(excluded)}. Above threshold {threshold}: {sum(1 for r in scored if r['score'] >= threshold)}. Selected for email: {len(selected)}")
    if excluded:
        print(f"Excluded job types: {sorted({r['job_type'] for r in excluded})}")
    for r in scored[:20]:
        print(f"  {r['score']:>3}  {r['job_number']}  {r['job_type']:<35}  drivers: {r['drivers']}")
    print(f"\nTSV: {tsv_path}\nSelected: {out_dir / 'selected.json'}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--threshold", type=int, default=35)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--exclude", action="append", default=[], help="Job type substring to exclude (repeatable). E.g. --exclude 'Follow Up' --exclude 'Recall'")
    args = ap.parse_args()
    main(Path(args.run_dir), Path(args.manifest), args.threshold, args.top_n, exclude_patterns=args.exclude)
