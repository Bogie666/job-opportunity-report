#!/usr/bin/env python3
"""Log realized outcomes for previously scored jobs so the opportunity scorer can be
calibrated against reality instead of hand-tuned weights.

Run this a few days to a week after a scoring run (e.g. weekly cron):

    python scripts/log_outcomes.py data/tech_briefs/<STAMP>_scoring

For every job in scores.tsv it pulls, read-only, the job's invoices and estimates
from ServiceTitan and appends one row per job to data/outcomes/outcomes.csv:

    run_stamp, logged_on, job_id, job_number, job_type, customer, score, drivers,
    selected (was emailed as a report card), max_equip_age, home_built_year,
    open_estimate_total_prior, sold_estimate_total_prior,
    invoice_total, invoice_count, sold_estimate_total_on_job,
    sold_estimate_count_on_job, sold_any

The file is append-only and idempotent per (run_stamp, job_id): re-running a stamp
skips jobs already logged. Once enough rows accumulate, score-vs-realized-revenue
is a plain spreadsheet/regression exercise — and the honest answer to whether
report-carded jobs actually convert better.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from client import ServiceTitanClient  # noqa: E402
from report_card_facts import load_default_env  # noqa: E402
from servicetitan_dossier import fetch_all  # noqa: E402

OUT_DEFAULT = ROOT / "data" / "outcomes" / "outcomes.csv"

FIELDS = [
    "run_stamp", "logged_on", "job_id", "job_number", "job_type", "customer",
    "score", "drivers", "selected", "max_equip_age", "home_built_year",
    "open_estimate_total_prior", "sold_estimate_total_prior",
    "invoice_total", "invoice_count",
    "sold_estimate_total_on_job", "sold_estimate_count_on_job", "sold_any",
]


def _read_scores(scoring_dir: Path) -> list[dict]:
    tsv = scoring_dir / "scores.tsv"
    if not tsv.exists():
        raise SystemExit(f"scores.tsv not found in {scoring_dir}")
    with tsv.open() as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    selected_path = scoring_dir / "selected.json"
    selected_ids: set[str] = set()
    if selected_path.exists():
        selected_ids = {str(r.get("job_id")) for r in json.loads(selected_path.read_text())}
    for r in rows:
        r["selected"] = str(r.get("job_id")) in selected_ids
    return rows


def _already_logged(out_path: Path) -> set[tuple[str, str]]:
    if not out_path.exists():
        return set()
    with out_path.open() as f:
        return {(r.get("run_stamp", ""), r.get("job_id", "")) for r in csv.DictReader(f)}


def _job_outcome(client: ServiceTitanClient, job_id: str) -> dict:
    invoices = fetch_all(client, "/accounting/v2/tenant/{tenant}/invoices", {"jobIds": job_id})
    estimates = fetch_all(client, "/sales/v2/tenant/{tenant}/estimates", {"jobId": job_id})
    # Defensive client-side filter in case the API ignores an unrecognized param.
    invoices = [inv for inv in invoices if str((inv.get("job") or {}).get("id") or inv.get("jobId") or job_id) == str(job_id)]
    estimates = [e for e in estimates if str(e.get("jobId") or job_id) == str(job_id)]
    invoice_total = sum(float(inv.get("total") or 0) for inv in invoices)
    sold = [
        e for e in estimates
        if ((e.get("status") or {}).get("name") or "").lower() == "sold"
    ]
    sold_total = sum(float(e.get("subtotal") or 0) for e in sold)
    return {
        "invoice_total": round(invoice_total, 2),
        "invoice_count": len(invoices),
        "sold_estimate_total_on_job": round(sold_total, 2),
        "sold_estimate_count_on_job": len(sold),
        "sold_any": bool(sold) or invoice_total > 0,
    }


def main(scoring_dir: Path, out_path: Path) -> None:
    load_default_env()
    client = ServiceTitanClient()
    run_stamp = scoring_dir.name.replace("_scoring", "")
    rows = _read_scores(scoring_dir)
    logged = _already_logged(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not out_path.exists()

    written = skipped = failed = 0
    with out_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        for r in rows:
            job_id = str(r.get("job_id") or "")
            if not job_id or (run_stamp, job_id) in logged:
                skipped += 1
                continue
            try:
                outcome = _job_outcome(client, job_id)
            except Exception as exc:
                print(f"  ! {job_id}: outcome pull failed: {exc}")
                failed += 1
                continue
            writer.writerow({
                "run_stamp": run_stamp,
                "logged_on": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "job_id": job_id,
                "job_number": r.get("job_number", ""),
                "job_type": r.get("job_type", ""),
                "customer": r.get("customer", ""),
                "score": r.get("score", ""),
                "drivers": r.get("drivers", ""),
                "selected": r.get("selected", False),
                "max_equip_age": r.get("max_equip_age", ""),
                "home_built_year": r.get("home_built_year", ""),
                "open_estimate_total_prior": r.get("open_estimate_total", ""),
                "sold_estimate_total_prior": r.get("sold_estimate_total", ""),
                **outcome,
            })
            written += 1
            print(f"  ✓ {job_id} invoice ${outcome['invoice_total']:,.0f} · sold est ${outcome['sold_estimate_total_on_job']:,.0f}")

    print(f"Logged {written} outcomes ({skipped} already logged/skipped, {failed} failed) → {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scoring_dir", help="A *_scoring dir produced by score_and_filter_hvac_briefs.py")
    ap.add_argument("--out", default=str(OUT_DEFAULT), help=f"Outcomes CSV (default {OUT_DEFAULT})")
    args = ap.parse_args()
    main(Path(args.scoring_dir), Path(args.out))
