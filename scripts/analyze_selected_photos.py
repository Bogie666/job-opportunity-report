#!/usr/bin/env python3
"""Run photo-vision on a hand-picked subset of jobs (by job_id), reusing the
existing manifest. Avoids the cost of analyzing all 66 contact sheets when only
the top opportunity candidates need vision review.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

# Reuse the existing analyzer's helpers
import analyze_photo_sheets_openrouter as az


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--selected", required=True, help="JSON file with list of dicts containing 'job_id' field")
    args = ap.parse_args()

    import os
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("Missing OPENROUTER_API_KEY")

    mp = Path(args.manifest)
    data = json.loads(mp.read_text())
    out_dir = mp.parent / "vision"
    out_dir.mkdir(exist_ok=True)
    wanted = {str(r.get("job_id")) for r in json.loads(Path(args.selected).read_text())}
    print(f"Selected jobs to vision-review: {sorted(wanted)}")

    results = []
    for rec in data["manifest"]:
        job_id = str(rec["job_id"])
        if job_id not in wanted:
            continue
        sheet = rec.get("contact_sheet")
        if not sheet:
            no = {"job_id": job_id, "photo_source_note": "No usable photos", "findings": [], "top_verify_today": "", "brief_insert": ""}
            (out_dir / f"{job_id}.json").write_text(json.dumps(no, indent=2))
            results.append(no)
            print(job_id, "no photos")
            continue
        sheet_path = (ROOT / sheet) if not Path(sheet).is_absolute() else Path(sheet)
        print("analyzing", job_id, sheet_path)
        text = az.call_vision(key, sheet_path, job_id)
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:].strip()
        try:
            parsed = json.loads(cleaned)
        except Exception:
            parsed = {"job_id": job_id, "raw": text, "parse_error": True}
        parsed.setdefault("job_id", job_id)
        (out_dir / f"{job_id}.json").write_text(json.dumps(parsed, indent=2))
        results.append(parsed)
        print(job_id, "findings", len(parsed.get("findings") or []))

    (out_dir / "selected_summary.json").write_text(json.dumps(results, indent=2))
    print(out_dir / "selected_summary.json")


if __name__ == "__main__":
    main()
