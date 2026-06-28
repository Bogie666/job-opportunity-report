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

# Job types excluded from ROI attribution: these are lead-driven / internal sales
# (e.g. Comfort Advisor equipment replacements), not opportunities the snapshot
# surfaced from unassigned demand. Including them inflates conversion and revenue.
EXCLUDE_JOB_TYPES = {"tech lead - equipment"}


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


MATURITY_DAYS = 14  # days after scheduled_date before realized revenue is considered settled


def build_metrics(
    rows: list[dict],
    date_from: str | None = None,
    date_to: str | None = None,
    as_of: str | None = None,
    maturity_days: int = MATURITY_DAYS,
) -> dict:
    """Compute deduped ROI metrics, optionally restricted to [date_from, date_to] inclusive.

    Dates are scheduled_date strings (YYYY-MM-DD). Excludes lead-driven job types.

    Maturity: a flagged opportunity's revenue is considered "matured" (settled)
    once `maturity_days` have elapsed since its scheduled_date relative to `as_of`
    (defaults to today, America/Chicago). Matured-only sub-metrics let executives
    distinguish settled performance from still-accruing recent weeks.
    """
    from datetime import date as _date
    from zoneinfo import ZoneInfo as _ZoneInfo

    as_of_date = _date.fromisoformat(as_of) if as_of else datetime.now(_ZoneInfo("America/Chicago")).date()

    def is_matured(r: dict) -> bool:
        d = (r.get("scheduled_date") or "").strip()
        if not d:
            return False
        try:
            return (as_of_date - _date.fromisoformat(d)).days >= maturity_days
        except ValueError:
            return False

    jobs = dedupe_by_job(rows)
    if date_from:
        jobs = [r for r in jobs if (r.get("scheduled_date") or "") >= date_from]
    if date_to:
        jobs = [r for r in jobs if (r.get("scheduled_date") or "") <= date_to]
    excluded = [r for r in jobs if (r.get("job_type") or "").strip().lower() in EXCLUDE_JOB_TYPES]
    jobs = [r for r in jobs if (r.get("job_type") or "").strip().lower() not in EXCLUDE_JOB_TYPES]
    excluded_rev = round(sum(num(r.get("booked_or_sold_revenue")) for r in excluded), 2)
    won = [r for r in jobs if num(r.get("booked_or_sold_revenue")) > 0]
    won.sort(key=lambda r: -num(r.get("booked_or_sold_revenue")))

    uj, uw = len(jobs), len(won)
    tot = round(sum(num(r["booked_or_sold_revenue"]) for r in jobs), 2)

    matured = [r for r in jobs if is_matured(r)]
    fresh = [r for r in jobs if not is_matured(r)]
    m_won = [r for r in matured if num(r.get("booked_or_sold_revenue")) > 0]
    m_uj, m_uw = len(matured), len(m_won)
    m_tot = round(sum(num(r["booked_or_sold_revenue"]) for r in matured), 2)
    f_tot = round(sum(num(r["booked_or_sold_revenue"]) for r in fresh), 2)

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_from": date_from,
        "window_to": date_to,
        "as_of": as_of_date.isoformat(),
        "maturity_days": maturity_days,
        "unique_opportunities": uj,
        "converted": uw,
        "conversion_rate": round(uw / uj, 4) if uj else 0.0,
        "total_revenue": tot,
        "revenue_per_opportunity": round(tot / uj, 2) if uj else 0.0,
        "avg_ticket_on_converted": round(tot / uw, 2) if uw else 0.0,
        # Matured (settled >= maturity_days) vs fresh (still accruing)
        "matured_opportunities": m_uj,
        "matured_converted": m_uw,
        "matured_conversion_rate": round(m_uw / m_uj, 4) if m_uj else 0.0,
        "matured_revenue": m_tot,
        "matured_revenue_per_opportunity": round(m_tot / m_uj, 2) if m_uj else 0.0,
        "matured_avg_ticket_on_converted": round(m_tot / m_uw, 2) if m_uw else 0.0,
        "fresh_opportunities": len(fresh),
        "fresh_revenue": f_tot,
        "all_jobs_matured": len(fresh) == 0,
        "excluded_job_types": sorted(EXCLUDE_JOB_TYPES),
        "excluded_opportunities": len(excluded),
        "excluded_revenue": excluded_rev,
        "by_date": bucket(jobs, "scheduled_date"),
        "by_grade": bucket(jobs, "grade"),
        "by_job_type": bucket(jobs, "job_type"),
        "top_converted": [
            {
                "date": r["scheduled_date"],
                "job_number": r.get("job_number"),
                "customer": r.get("customer"),
                "job_type": r.get("job_type"),
                "revenue": num(r["booked_or_sold_revenue"]),
                "matured": is_matured(r),
            }
            for r in won[:10]
        ],
    }


def main() -> int:
    rows = list(csv.DictReader(OUTCOMES_CSV.open(newline="")))
    metrics = build_metrics(rows)
    uj = metrics["unique_opportunities"]
    uw = metrics["converted"]
    tot = metrics["total_revenue"]
    excluded_n = metrics["excluded_opportunities"]
    excluded_rev = metrics["excluded_revenue"]
    by_date = metrics["by_date"]
    by_grade = metrics["by_grade"]
    by_type = metrics["by_job_type"]

    JSON_OUT.write_text(json.dumps(metrics, indent=2, default=str))

    def pct(x):
        return f"{x*100:.0f}%"

    excluded = metrics["excluded_opportunities"]  # backward-compat name used below

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
    if excluded:
        L.append(f"Excluded {excluded} lead-driven job(s) (${excluded_rev:,.0f}) of type: {', '.join(sorted(EXCLUDE_JOB_TYPES))}. These are internal/Comfort Advisor sales, not snapshot-surfaced opportunities.")
    L.append("")
    L.append(f"## Matured vs Fresh (settle window: {metrics['maturity_days']} days, as of {metrics['as_of']})")
    L.append("")
    L.append(f"- Matured (settled): {metrics['matured_opportunities']} opps | {metrics['matured_converted']} won ({pct(metrics['matured_conversion_rate'])}) | ${metrics['matured_revenue']:,.0f} | ${metrics['matured_avg_ticket_on_converted']:,.0f} avg ticket")
    L.append(f"- Fresh (still accruing): {metrics['fresh_opportunities']} opps | ${metrics['fresh_revenue']:,.0f} booked so far")
    if not metrics["all_jobs_matured"]:
        L.append("- Fresh-week revenue typically rises as ServiceTitan invoices settle; treat matured figures as the reliable run rate.")
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
