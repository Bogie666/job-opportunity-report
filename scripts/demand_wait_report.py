"""Demand-call wait-time report — combined LEX + Lyons.

Repeatable KPI tool. Two metrics per demand job:
  (1) booking_to_arrival = Technician Arrived - Job Booked      (total customer wait) [PRIMARY]
  (2) sched_to_arrival   = Technician Arrived - appointment.start (punctuality)      [PROVISIONAL]

Member vs non-member is read from the demand job-type name suffix and QA'd
against job.membershipId. Only the 7 'Demand -' job types are counted.

NOTE: the ServiceTitan /jpm jobs `jobTypeIds` query filter is unreliable (the API
has returned all job types regardless), so we over-pull and filter to DEMAND_IDS
client-side. Do not trust server-side jobType filtering here.

NOTE: metric (2) is PROVISIONAL — arrival-vs-scheduled-start has returned a
suspiciously uniform ~0.6h across all segments, suggesting ST auto-stamps arrival
off the window rather than a true onsite event. Reported but flagged; validate
the `start` source before putting punctuality on a live dashboard.

Usage:
    python -m scripts.demand_wait_report [--days 30] [--json] [--quiet]

Outputs (under out/demand_wait/):
    raw_<days>d.jsonl       per-job records (booked/arrived/apptStart/deltas)
    summary_<days>d.json    machine-readable KPI summary (for dashboard ingestion)
    summary_<days>d.txt     exec-readable text summary
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.client import ServiceTitanClient

# jobTypeId -> (name, trade, segment)  — the 7 true demand types (LEX + Lyons shared tenant)
DEMAND = {
    9987:    ("Demand - Commercial",          "commercial", None),
    509:     ("Demand - Electrical - Member",  "electrical", "member"),
    515:     ("Demand - Electrical - NON Member", "electrical", "nonmember"),
    508:     ("Demand - HVAC - Member",        "hvac",       "member"),
    460:     ("Demand - HVAC - NON Member",    "hvac",       "nonmember"),
    521:     ("Demand - Plumbing - Member",    "plumbing",   "member"),
    1753637: ("Demand - Plumbing - NON Member", "plumbing", "nonmember"),
}
DEMAND_IDS = set(DEMAND)

OUT = Path(__file__).resolve().parent.parent / "out" / "demand_wait"


# ---------- helpers ----------------------------------------------------------

def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def fmt_hrs(h):
    if h is None:
        return "n/a"
    return f"{h:.1f} hrs" if h < 24 else f"{h/24:.1f} days ({h:.0f} hrs)"


def stats(vals):
    vals = sorted(v for v in vals if v is not None and v >= 0)
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": statistics.mean(vals),
        "median": statistics.median(vals),
        "p90": vals[int(len(vals) * 0.9) - 1] if len(vals) >= 10 else max(vals),
        "min": vals[0],
        "max": vals[-1],
    }


# ---------- extract ----------------------------------------------------------

def fetch_demand_jobs(c, start_iso):
    """Over-pull then filter to DEMAND_IDS (server-side jobTypeIds filter unreliable)."""
    ids = ",".join(map(str, DEMAND_IDS))
    page, out = 1, []
    while True:
        r = c.get("/jpm/v2/tenant/{tenant}/jobs",
                  {"jobTypeIds": ids, "createdOnOrAfter": start_iso,
                   "pageSize": 500, "page": page, "includeTotal": "true"})
        j = r.json()
        out.extend(j.get("data", []))
        if not j.get("hasMore"):
            break
        page += 1
    return [j for j in out if j.get("jobTypeId") in DEMAND_IDS]


def fetch_appt_starts(c, appt_ids):
    starts = {}
    ids = list(appt_ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        r = c.get("/jpm/v2/tenant/{tenant}/appointments",
                  {"ids": ",".join(map(str, chunk)), "pageSize": 500})
        for a in r.json().get("data", []):
            starts[a["id"]] = parse(a.get("start"))
    return starts


def build_rows(c, jobs, sched, log):
    rows = []
    total = len(jobs)
    for i, j in enumerate(jobs, 1):
        jid = j["id"]
        name, trade, seg = DEMAND[j["jobTypeId"]]
        h = c.get(f"/jpm/v2/tenant/{{tenant}}/jobs/{jid}/history")
        hist = h.json().get("history", [])
        booked = arrived = None
        for e in hist:
            et = e.get("eventType")
            if et == "Job Booked" and booked is None:
                booked = parse(e.get("date"))
            elif et == "Technician Arrived":
                d = parse(e.get("date"))
                if arrived is None or (d and d < arrived):
                    arrived = d  # earliest arrival
        appt_start = sched.get(j.get("firstAppointmentId"))
        rows.append({
            "jobId": jid, "jobNumber": j.get("jobNumber"),
            "jobTypeId": j["jobTypeId"], "trade": trade, "seg": seg,
            "membershipId": j.get("membershipId"),
            "booked": booked.isoformat() if booked else None,
            "arrived": arrived.isoformat() if arrived else None,
            "apptStart": appt_start.isoformat() if appt_start else None,
            "bta_hrs": (arrived - booked).total_seconds() / 3600 if (booked and arrived) else None,
            "sta_hrs": (arrived - appt_start).total_seconds() / 3600 if (appt_start and arrived) else None,
        })
        if log and i % 100 == 0:
            print(f"    history {i}/{total}", flush=True)
    return rows


# ---------- summarize --------------------------------------------------------

def segment_block(rows, key):
    valid = [r for r in rows if r.get(key) is not None and r[key] >= 0]
    block = {"overall": stats([r[key] for r in valid])}
    for seg in ("member", "nonmember"):
        block[seg] = stats([r[key] for r in valid if r["seg"] == seg])
    block["commercial"] = stats([r[key] for r in valid if r["seg"] is None])
    block["by_trade"] = {
        tr: stats([r[key] for r in valid if r["trade"] == tr])
        for tr in ("hvac", "plumbing", "electrical", "commercial")
    }
    return block


def summarize(rows, days):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "scope": "combined LEX + Lyons",
        "demand_jobs": len(rows),
        "jobs_with_arrival": sum(1 for r in rows if r.get("arrived")),
        "booking_to_arrival": segment_block(rows, "bta_hrs"),      # PRIMARY
        "sched_start_to_arrival": segment_block(rows, "sta_hrs"),  # PROVISIONAL
        "qa": {
            "missing_arrival": sum(1 for r in rows if not r.get("arrived")),
            "missing_booked": sum(1 for r in rows if not r.get("booked")),
            "member_type_no_membershipId": sum(1 for r in rows if r["seg"] == "member" and not r.get("membershipId")),
            "nonmember_type_has_membershipId": sum(1 for r in rows if r["seg"] == "nonmember" and r.get("membershipId")),
        },
    }


def render_text(s):
    L = []
    L.append(f"=== DEMAND-CALL WAIT TIME — trailing {s['window_days']}d — {s['scope']} ===")
    L.append(f"Generated {s['generated_at']}")
    L.append(f"Demand jobs: {s['demand_jobs']}   |   with completed arrival: {s['jobs_with_arrival']}")

    def blk(title, b, provisional=False):
        L.append(f"\n--- {title}{'  [PROVISIONAL — needs source validation]' if provisional else ''} ---")
        o = b["overall"]
        if not o:
            L.append("  no data"); return
        L.append(f"  n={o['n']}   MEAN {fmt_hrs(o['mean'])}   MEDIAN {fmt_hrs(o['median'])}   P90 {fmt_hrs(o['p90'])}")
        L.append(f"  range {fmt_hrs(o['min'])} -> {fmt_hrs(o['max'])}")
        L.append("  member vs non-member:")
        for seg in ("member", "nonmember", "commercial"):
            ss = b.get(seg)
            if ss:
                L.append(f"    {seg:11s} n={ss['n']:4d}  mean {fmt_hrs(ss['mean']):>18s}  median {fmt_hrs(ss['median'])}")
        L.append("  by trade:")
        for tr, ts in b["by_trade"].items():
            if ts:
                L.append(f"    {tr:11s} n={ts['n']:4d}  mean {fmt_hrs(ts['mean']):>18s}  median {fmt_hrs(ts['median'])}")

    blk("BOOKING -> ARRIVAL (total customer wait)", s["booking_to_arrival"])
    blk("SCHEDULED START -> ARRIVAL (punctuality)", s["sched_start_to_arrival"], provisional=True)

    q = s["qa"]
    L.append("\n--- QA ---")
    L.append(f"  missing Technician Arrived: {q['missing_arrival']} (canceled/no-show/not-yet-run)")
    L.append(f"  missing Job Booked:         {q['missing_booked']}")
    L.append(f"  'Member' type but no membershipId:      {q['member_type_no_membershipId']}")
    L.append(f"  'NON Member' type but has membershipId: {q['nonmember_type_has_membershipId']}")
    return "\n".join(L)


# ---------- main -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Demand-call wait-time KPI (combined LEX+Lyons)")
    ap.add_argument("--days", type=int, default=30, help="trailing window in days (default 30)")
    ap.add_argument("--reuse-raw", action="store_true", help="skip API pull, re-summarize existing raw_<days>d.jsonl")
    ap.add_argument("--quiet", action="store_true", help="suppress progress logging")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    raw_path = OUT / f"raw_{args.days}d.jsonl"
    log = not args.quiet

    if args.reuse_raw and raw_path.exists():
        rows = [json.loads(l) for l in raw_path.read_text().splitlines() if l.strip()]
        rows = [r for r in rows if r.get("jobTypeId") in DEMAND_IDS]
        if log:
            print(f"[reuse] {len(rows)} demand rows from {raw_path}", flush=True)
    else:
        c = ServiceTitanClient()
        start_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        if log:
            print(f"[+] fetching demand jobs, trailing {args.days}d since {start_iso}", flush=True)
        jobs = fetch_demand_jobs(c, start_iso)
        if log:
            print(f"[+] {len(jobs)} true demand jobs", flush=True)
        appt_ids = {j.get("firstAppointmentId") for j in jobs if j.get("firstAppointmentId")}
        if log:
            print(f"[+] fetching {len(appt_ids)} appointment starts", flush=True)
        sched = fetch_appt_starts(c, appt_ids)
        rows = build_rows(c, jobs, sched, log)
        raw_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        if log:
            print(f"[+] raw -> {raw_path}", flush=True)

    summary = summarize(rows, args.days)
    (OUT / f"summary_{args.days}d.json").write_text(json.dumps(summary, indent=2))
    text = render_text(summary)
    (OUT / f"summary_{args.days}d.txt").write_text(text + "\n")
    print(text)
    print(f"\n[+] json    -> {OUT / f'summary_{args.days}d.json'}")
    print(f"[+] text    -> {OUT / f'summary_{args.days}d.txt'}")


if __name__ == "__main__":
    main()
