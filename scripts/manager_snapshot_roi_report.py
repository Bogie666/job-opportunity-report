#!/usr/bin/env python3
"""Executive ROI report for the Service Manager Opportunity Snapshot program.

Reads data/outcomes/manager_snapshot_outcomes.csv (the nightly revenue-refreshed
ledger), dedupes by job_id so same-day re-runs never double-count, and emits:
  - data/outcomes/manager_snapshot_roi_report.md   (executive summary)
  - data/outcomes/manager_snapshot_roi_report.json  (machine-readable metrics)

Read-only. Safe to run any time after the nightly refresh.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "outcomes"
OUTCOMES_CSV = OUT / "manager_snapshot_outcomes.csv"
MD = OUT / "manager_snapshot_roi_report.md"
JSON_OUT = OUT / "manager_snapshot_roi_report.json"


def num(x) -> float:
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def is_true(x) -> bool:
    return str(x).lower() in {"true", "1"}


def dedupe_by_job(rows: list[dict]) -> list[dict]:
    by_job: dict[str, dict] = {}
    for r in rows:
        jid = r.get("job_id") or ""
        if not jid:
            continue
        prev = by_job.get(jid)
        if prev is None or num(r.get("booked_or_sold_revenue")) > num(prev.get("booked_or_sold_revenue")):
            by_job[jid] = r
    return list(by_job.values())


def bucket(rows: list[dict], key: str):
    out = defaultdict(lambda: {"jobs": 0, "won": 0, "rev": 0.0})
    for r in rows:
        k = (r.get(key) or "?").strip() or "?"
        out[k]["jobs"] += 1
        if is_true(r.get("has_realized_revenue")):
            out[k]["won"] += 1
        out[k]["rev"] += num(r.get("booked_or_sold_revenue"))
    for v in out.values():
        v["rev"] = round(v["rev"], 2)
        v["conv"] = round(v["won"] / v["jobs"], 4) if v["jobs"] else 0.0
        v["rev_per_job"] = round(v["rev"] / v["jobs"], 2) if v["jobs"] else 0.0
    return dict(out)


def main() -> int:
    rows = list(csv.DictReader(OUTCOMES_CSV.open(newline="")))
    jobs = dedupe_by_job(rows)
    won = [r for r in jobs if num(r.get("booked_or_sold_revenue")) > 0]
    won.sort(key=lambda r: -num(r.get("booked_or_sold_revenue")))

    uj, uw = len(jobs), len(won)
    tot = round(sum(num(r["booked_or_sold_revenue"]) for r in jobs), 2)

    by_date = bucket(jobs, "scheduled_date")
    by_grade = bucket(jobs, "grade")
    by_type = bucket(jobs, "job_type")

    metrics = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unique_opportunities": uj,
        "converted": uw,
        "conversion_rate": round(uw / uj, 4) if uj else 0.0,
        "total_revenue": tot,
        "revenue_per_opportunity": round(tot / uj, 2) if uj else 0.0,
        "avg_ticket_on_converted": round(tot / uw, 2) if uw else 0.0,
        "by_date": by_date,
        "by_grade": by_grade,
        "by_job_type": by_type,
        "top_converted": [
            {
                "date": r["scheduled_date"],
                "job_number": r.get("job_number"),
                "customer": r.get("customer"),
                "job_type": r.get("job_type"),
                "revenue": num(r["booked_or_sold_revenue"]),
            }
            for r in won[:10]
        ],
    }
    JSON_OUT.write_text(json.dumps(metrics, indent=2, default=str))

    def pct(x):
        return f"{x*100:.0f}%"

    L = []
    L.append("# Service Manager Opportunity Snapshot — ROI Report")
    L.append("")
    L.append(f"Generated {metrics['generated_utc']} | Source: nightly revenue-refreshed outcomes ledger")
    L.append("")
    L.append("## Program Headline")
    L.append("")
    L.append(f"- Unique opportunities flagged: {uj}")
    L.append(f"- Converted to revenue: {uw} ({pct(metrics['conversion_rate'])})")
    L.append(f"- Total booked/sold revenue: ${tot:,.0f}")
    L.append(f"- Revenue per flagged opportunity: ${metrics['revenue_per_opportunity']:,.0f}")
    L.append(f"- Average ticket on converted jobs: ${metrics['avg_ticket_on_converted']:,.0f}")
    L.append("")
    L.append("Note: metrics dedupe by job_id; same-day re-runs do not double-count.")
    L.append("")
    L.append("## Daily Trend")
    L.append("")
    for d in sorted(by_date):
        v = by_date[d]
        L.append(f"- {d}: {v['jobs']} opps | {v['won']} won ({pct(v['conv'])}) | ${v['rev']:,.0f}")
    L.append("")
    L.append("## By Grade")
    L.append("")
    for g in sorted(by_grade):
        v = by_grade[g]
        L.append(f"- {g}: {v['jobs']} opps | {v['won']} won ({pct(v['conv'])}) | ${v['rev']:,.0f} | ${v['rev_per_job']:,.0f}/opp")
    L.append("")
    L.append("## By Job Type")
    L.append("")
    for t in sorted(by_type, key=lambda k: -by_type[k]["rev"]):
        v = by_type[t]
        L.append(f"- {t}: {v['jobs']} opps | {v['won']} won ({pct(v['conv'])}) | ${v['rev']:,.0f}")
    L.append("")
    L.append("## Top Converted Opportunities")
    L.append("")
    for r in metrics["top_converted"]:
        L.append(f"- ${r['revenue']:,.0f} | {r['date']} | #{r['job_number']} | {r['customer']} | {r['job_type']}")
    L.append("")
    MD.write_text("\n".join(L))
    print(f"Wrote {MD}")
    print(f"Wrote {JSON_OUT}")
    print(f"Headline: {uj} opps, {uw} won ({pct(metrics['conversion_rate'])}), ${tot:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
