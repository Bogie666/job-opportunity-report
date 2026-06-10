#!/usr/bin/env python3
"""Score HVAC tech-brief dossiers for opportunity potential and pick the ones worth
sending an email report card on. Pure-deterministic scoring — no LLM calls.

Outputs scoring TSV + a JSON list of the chosen job IDs.

Heuristics (tweakable):
- HVAC equipment 10+ yrs → +25
- Equipment 15+ yrs → +35
- Active membership → +10
- 1+ open estimate → +10 each (cap 30)
- Open estimate total ≥ $5k → +20
- Sold estimate history ≥ $10k → +15
- Photos available (contact sheet) → +10
- Booking-note repair recommendation keywords → +15
- "Older system / replace" booking-note signals → +10
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from serial_decoder import decode_serial  # noqa: E402  brand-aware decoder


def _status(e):
    s = e.get("status")
    if isinstance(s, dict):
        return (s.get("name") or "").strip().lower()
    return str(s or "").strip().lower()


def _money(rows, status_filter):
    return sum(float(r.get("subtotal") or 0) for r in rows if _status(r) == status_filter)


YEAR_RE = re.compile(r"(20\d{2}|19\d{2})")


def _equip_ages(equipment):
    """Return ages (years) of active equipment.

    Priority: installedOn date → brand-aware serial decoder.
    Naive WWYY-on-everything regex removed — it mis-decoded Trane/Lennox/Rheem
    YYWW serials as 20-year-old equipment (e.g. Trane '22085' → 2008 instead
    of 2022). See src/serial_decoder.py for the brand-aware rules.
    """
    now_year = datetime.now(timezone.utc).year
    ages = []
    for eq in equipment or []:
        if not eq.get("active", True):
            continue
        year = None
        iso = eq.get("installedOn") or ""
        m = YEAR_RE.search(str(iso))
        if m:
            year = int(m.group(1))
        if not year:
            mfg = eq.get("manufacturer")
            if isinstance(mfg, dict):
                mfg = mfg.get("name") or ""
            serial = str(eq.get("serialNumber") or "")
            decoded = decode_serial(str(mfg or ""), serial)
            if decoded:
                year = decoded[0]
        if year and 1990 <= year <= now_year:
            ages.append(now_year - year)
    return ages


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
    equipment = dossier.get("installed_equipment") or []
    memberships = dossier.get("memberships") or []
    estimates = dossier.get("estimates") or []

    score = 0
    drivers = []

    ages = _equip_ages(equipment)
    max_age = max(ages) if ages else None
    if max_age is not None:
        if max_age >= 15:
            score += 35
            drivers.append(f"equip {max_age}y (15+)")
        elif max_age >= 10:
            score += 25
            drivers.append(f"equip {max_age}y (10+)")

    if not ages or max_age is None:
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

    active_m = any(str(m.get("status") or "").lower() == "active" for m in memberships)
    if active_m:
        score += 10
        drivers.append("active membership")

    open_rows = [e for e in estimates if _status(e) == "open"]
    sold_rows = [e for e in estimates if _status(e) == "sold"]
    if open_rows:
        bump = min(len(open_rows) * 5, 30)
        score += bump
        drivers.append(f"{len(open_rows)} open est (+{bump})")
    open_total = sum(float(e.get("subtotal") or 0) for e in open_rows)
    if open_total >= 5000:
        score += 20
        drivers.append(f"open ${int(open_total)}")
    sold_total = sum(float(e.get("subtotal") or 0) for e in sold_rows)
    if sold_total >= 10000:
        score += 15
        drivers.append(f"sold ${int(sold_total)}")

    if photos_have_sheet:
        score += 10
        drivers.append("photos avail")

    if REPAIR_KEYWORDS.search(summary):
        score += 15
        drivers.append("note: repair signals")

    return {
        "job_number": str(job.get("jobNumber") or job.get("id") or ""),
        "job_type": meta.get("job_type") or "",
        "customer": meta.get("customer") or (dossier.get("customer") or {}).get("name") or "",
        "score": score,
        "drivers": ", ".join(drivers),
        "has_photos": photos_have_sheet,
        "max_equip_age": max_age,
        "open_estimate_count": len(open_rows),
        "open_estimate_total": int(open_total),
        "sold_estimate_total": int(sold_total),
        "active_membership": active_m,
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
        if exclude_re and exclude_re.search(rec.get("job_type", "")):
            rec["excluded_reason"] = "call-type filter"
            excluded.append(rec)
            continue
        scored.append(rec)

    scored.sort(key=lambda r: (-r["score"], r["job_number"]))

    out_dir = run_dir.parent / (run_dir.name + "_scoring")
    out_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = out_dir / "scores.tsv"
    fields = [
        "job_number", "score", "job_type", "customer", "drivers", "has_photos",
        "max_equip_age", "open_estimate_count", "open_estimate_total", "sold_estimate_total",
        "active_membership", "job_id", "dossier_json",
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
