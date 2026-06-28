#!/usr/bin/env python3
"""Refresh realized revenue outcomes for jobs emailed in manager snapshots.

Reads data/outcomes/manager_snapshot_flagged_jobs.csv, pulls read-only
ServiceTitan invoices/estimates for each flagged job, and rewrites
manager_snapshot_outcomes.csv with latest known revenue. Safe to run daily.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from client import ServiceTitanClient  # noqa: E402
from report_card_facts import load_default_env  # noqa: E402
from servicetitan_dossier import fetch_all  # noqa: E402

OUTCOMES_DIR = ROOT / "data" / "outcomes"
FLAGGED_LEDGER = OUTCOMES_DIR / "manager_snapshot_flagged_jobs.csv"
OUTCOMES_CSV = OUTCOMES_DIR / "manager_snapshot_outcomes.csv"
SUMMARY_JSON = OUTCOMES_DIR / "manager_snapshot_outcomes_summary.json"

OUTCOME_FIELDS = [
    "run_id", "scheduled_date", "selected_rank", "job_id", "job_number", "customer", "job_type",
    "score", "grade", "staffing", "drivers", "recipients", "subject",
    "job_status", "completed_on", "invoice_total", "invoice_count", "invoice_ids",
    "sold_estimate_total_on_job", "sold_estimate_count_on_job", "sold_estimate_ids",
    "booked_or_sold_revenue", "has_realized_revenue", "last_refreshed_utc",
]


def read_flagged(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def num(x: Any) -> float:
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def invoice_amount(inv: dict[str, Any]) -> float:
    for key in ("total", "totalAmount", "amount", "balance", "subtotal"):
        if key in inv and inv.get(key) is not None:
            return num(inv.get(key))
    summary = inv.get("summary") if isinstance(inv.get("summary"), dict) else {}
    for key in ("total", "totalAmount", "invoiceTotal"):
        if key in summary:
            return num(summary.get(key))
    return 0.0


def status_name(e: dict[str, Any]) -> str:
    s = e.get("status")
    if isinstance(s, dict):
        return str(s.get("name") or "")
    return str(s or "")


def get_job(client: ServiceTitanClient, job_id: str) -> dict[str, Any]:
    r = client.get(f"/jpm/v2/tenant/{{tenant}}/jobs/{job_id}")
    if r.status_code != 200:
        return {"id": job_id, "_status_error": f"{r.status_code} {r.text[:200]}"}
    return r.json()


def job_outcome(client: ServiceTitanClient, job_id: str) -> dict[str, Any]:
    job = get_job(client, job_id)
    invoices = fetch_all(client, "/accounting/v2/tenant/{tenant}/invoices", {"jobIds": job_id}, hard_limit=100)
    invoices = [
        inv for inv in invoices
        if str((inv.get("job") or {}).get("id") or inv.get("jobId") or job_id) == str(job_id)
    ]
    estimates = fetch_all(client, "/sales/v2/tenant/{tenant}/estimates", {"jobId": job_id}, hard_limit=100)
    estimates = [e for e in estimates if str(e.get("jobId") or job_id) == str(job_id)]
    sold = [e for e in estimates if status_name(e).lower() == "sold"]
    invoice_total = sum(invoice_amount(inv) for inv in invoices)
    sold_total = sum(num(e.get("subtotal") or e.get("total") or e.get("amount")) for e in sold)
    booked_or_sold = max(invoice_total, sold_total)
    return {
        "job_status": status_name(job) or str(job.get("jobStatus") or job.get("status") or ""),
        "completed_on": job.get("completedOn") or job.get("completedDate") or "",
        "invoice_total": round(invoice_total, 2),
        "invoice_count": len(invoices),
        "invoice_ids": ",".join(str(inv.get("id") or inv.get("invoiceId") or "") for inv in invoices if inv.get("id") or inv.get("invoiceId")),
        "sold_estimate_total_on_job": round(sold_total, 2),
        "sold_estimate_count_on_job": len(sold),
        "sold_estimate_ids": ",".join(str(e.get("id") or "") for e in sold if e.get("id")),
        "booked_or_sold_revenue": round(booked_or_sold, 2),
        "has_realized_revenue": booked_or_sold > 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(FLAGGED_LEDGER))
    ap.add_argument("--out", default=str(OUTCOMES_CSV))
    ap.add_argument("--limit", type=int, default=0, help="Optional max rows for smoke tests")
    args = ap.parse_args()

    load_default_env()
    flagged = read_flagged(Path(args.ledger))
    if args.limit:
        flagged = flagged[: args.limit]
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    if not flagged:
        print(f"No flagged manager snapshot jobs found at {args.ledger}")
        return 0

    client = ServiceTitanClient()
    rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for r in flagged:
        job_id = str(r.get("job_id") or "")
        if not job_id:
            continue
        try:
            outcome = job_outcome(client, job_id)
        except Exception as exc:
            failed.append({"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"})
            outcome = {
                "job_status": "OUTCOME_PULL_FAILED",
                "completed_on": "",
                "invoice_total": 0,
                "invoice_count": 0,
                "invoice_ids": "",
                "sold_estimate_total_on_job": 0,
                "sold_estimate_count_on_job": 0,
                "sold_estimate_ids": "",
                "booked_or_sold_revenue": 0,
                "has_realized_revenue": False,
            }
        rows.append({
            **{k: r.get(k, "") for k in [
                "run_id", "scheduled_date", "selected_rank", "job_id", "job_number", "customer",
                "job_type", "score", "grade", "staffing", "drivers", "recipients", "subject",
            ]},
            **outcome,
            "last_refreshed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        print(f"{r.get('scheduled_date')} #{r.get('job_number')} rank {r.get('selected_rank')}: ${outcome['booked_or_sold_revenue']:,.0f}")

    out = Path(args.out)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTCOME_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in OUTCOME_FIELDS})

    def is_true(x: Any) -> bool:
        return str(x).lower() in {"true", "1"}

    # Per-run view keeps every run (a re-run on the same day is a legitimate row in history).
    by_run: dict[str, dict[str, Any]] = {}
    for r in rows:
        run = str(r.get("run_id") or "")
        item = by_run.setdefault(run, {"scheduled_date": r.get("scheduled_date"), "jobs": 0, "jobs_with_revenue": 0, "revenue": 0.0})
        item["jobs"] += 1
        if is_true(r.get("has_realized_revenue")):
            item["jobs_with_revenue"] += 1
        item["revenue"] += num(r.get("booked_or_sold_revenue"))

    # Headline metrics dedupe by job_id so same-day re-runs and multi-day re-flags
    # never double-count a single opportunity's realized revenue.
    by_job: dict[str, dict[str, Any]] = {}
    for r in rows:
        jid = str(r.get("job_id") or "")
        if not jid:
            continue
        prev = by_job.get(jid)
        # keep the record with the highest realized revenue for the job
        if prev is None or num(r.get("booked_or_sold_revenue")) > num(prev.get("booked_or_sold_revenue")):
            by_job[jid] = r

    unique_jobs = len(by_job)
    unique_with_revenue = sum(1 for r in by_job.values() if is_true(r.get("has_realized_revenue")))
    dedup_total = sum(num(r.get("booked_or_sold_revenue")) for r in by_job.values())
    raw_total = sum(num(r.get("booked_or_sold_revenue")) for r in rows)
    conv = round(unique_with_revenue / unique_jobs, 4) if unique_jobs else 0.0
    rev_per_flag = round(dedup_total / unique_jobs, 2) if unique_jobs else 0.0
    avg_ticket = round(dedup_total / unique_with_revenue, 2) if unique_with_revenue else 0.0

    summary = {
        "refreshed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Deduped headline (count each unique opportunity once)
        "unique_flagged_jobs": unique_jobs,
        "unique_jobs_with_revenue": unique_with_revenue,
        "total_booked_or_sold_revenue": round(dedup_total, 2),
        "conversion_rate": conv,
        "revenue_per_flagged_job": rev_per_flag,
        "avg_ticket_on_converted": avg_ticket,
        # Raw ledger counts (all rows incl. re-runs) for reconciliation
        "flagged_rows": len(rows),
        "raw_revenue_with_rerun_dupes": round(raw_total, 2),
        "outcomes_csv": str(out),
        "failed": failed,
        "by_run": by_run,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
