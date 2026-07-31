"""Daily orchestrator: pull -> prefilter -> transcribe -> classify -> reconcile -> brief -> deliver.

Usage:
  python -m unbooked_analysis.run_daily --tenant lex_portfolio [--date YYYY-MM-DD]
                                        [--dry-run] [--no-send] [--limit N]

--date defaults to YESTERDAY in the tenant timezone.
--dry-run  : do everything, write artifacts, but do NOT call SendGrid (prints subject).
--no-send  : alias for --dry-run.
--limit N  : only process first N unbooked calls (for cheap testing).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/workspace/apps/lex-servicetitan-reporting")

from unbooked_analysis.tenants import get_tenant
from unbooked_analysis.sources.servicetitan import ServiceTitanCallSource
from unbooked_analysis import transcribe as T
from unbooked_analysis import classify as C
from unbooked_analysis import brief as B
from unbooked_analysis import deliver as D

OUT_DIR = Path("/workspace/apps/lex-servicetitan-reporting/data/unbooked_analysis")


def _day_bounds_utc(date_local, tz):
    start_local = datetime.combine(date_local, time.min, tzinfo=ZoneInfo(tz))
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def run(tenant_key: str, date_str: str | None, dry_run: bool, limit: int | None):
    tenant = get_tenant(tenant_key)
    tz = tenant.timezone

    if date_str:
        date_local = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        date_local = (datetime.now(ZoneInfo(tz)) - timedelta(days=1)).date()
    day_label = date_local.strftime("%a %b %-d, %Y")
    start_utc, end_utc = _day_bounds_utc(date_local, tz)

    print(f"[{tenant.key}] {day_label}  window {start_utc}..{end_utc}")

    src = ServiceTitanCallSource(tenant.st_tenant_id)

    # 1. Pull all calls, keep INBOUND UNBOOKED (true-lead denominator excludes
    #    Excused/NotLead/Abandoned; those are not shown).
    all_calls = src.list_calls(start_utc, end_utc)
    if tenant.brand_filter:
        bf = [b.lower() for b in tenant.brand_filter]
        all_calls = [c for c in all_calls
                     if c.campaign_name and any(b in c.campaign_name.lower() for b in bf)]
    unbooked = [c for c in all_calls
                if c.direction == "Inbound" and c.call_type == "Unbooked"]
    print(f"  pulled {len(all_calls)} calls; {len(unbooked)} inbound unbooked")

    if limit:
        unbooked = unbooked[:limit]
        print(f"  --limit -> {len(unbooked)} calls")

    # 2. Prefilter noise categories (bucket A, no transcription spend).
    to_process = []
    for c in unbooked:
        cat = (c.campaign_category or "").lower()
        if any(pf.lower() in cat for pf in tenant.prefilter_categories):
            c.reason_bucket = "A"
            c.reason_detail = f"ST category: {c.campaign_category} (pre-filtered)"
        else:
            to_process.append(c)
    print(f"  pre-filtered {len(unbooked)-len(to_process)} noise; {len(to_process)} to transcribe")

    # 3-4. Transcribe + classify the real candidates.
    for i, c in enumerate(to_process, 1):
        audio = src.get_recording(c.id)
        if audio:
            c.transcript = T.transcribe(audio, filename=f"{c.id}.mp3")
        res = C.classify(c)
        c.reason_bucket = res["bucket"]
        c.reason_detail = res["detail"]
        print(f"    [{i}/{len(to_process)}] call {c.id} {c.from_number} -> "
              f"{res['bucket']} ({res.get('confidence','?')}) {res['detail'][:50]}")

    # 5. Reconcile outcomes for ALL unbooked (even prefiltered — a noise call can
    #    still have booked; but only B-E count as leaks anyway).
    #    Outcomes: booked_on_call | recovered (confirmed w/ job#) | callback_unverified
    #              (staff called back, no linked job — verify manually) | still_unbooked
    for c in unbooked:
        rec = src.find_recovery(c, tenant.reconcile_window_hours)
        if c.job_number:
            c.outcome = "booked_on_call"
            c.recovered_job_number = c.job_number
        elif rec:
            tier = rec.get("tier")
            if tier == "direct_link":
                c.outcome = "booked_on_call"
            elif rec.get("confidence") == "unverified":
                # phone-only callback, no job link -> its own bucket, NOT a confirmed recovery
                c.outcome = "callback_unverified"
            else:
                c.outcome = "recovered"
            c.recovered_job_number = rec.get("job_number")
            c.outcome_evidence = rec.get("note")
        else:
            c.outcome = "still_unbooked"

    booked = sum(1 for c in unbooked if c.outcome == "booked_on_call")
    recovered = sum(1 for c in unbooked if c.outcome == "recovered")
    callback_unverified = sum(1 for c in unbooked if c.outcome == "callback_unverified")
    still = sum(1 for c in unbooked if c.outcome == "still_unbooked")
    print(f"  outcomes: booked_on_call={booked} recovered={recovered} "
          f"callback_unverified={callback_unverified} still_unbooked={still}")

    # 6. Build brief.
    subject, html = B.build_brief(tenant.display_name, day_label, unbooked)

    # Persist artifacts.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date_local.strftime("%Y%m%d")
    (OUT_DIR / f"{tenant.key}_{stamp}.html").write_text(html)
    (OUT_DIR / f"{tenant.key}_{stamp}.json").write_text(json.dumps([{
        "id": c.id, "from": c.from_number, "customer": c.customer_name,
        "campaign": c.campaign_name, "category": c.campaign_category,
        "bucket": c.reason_bucket, "detail": c.reason_detail,
        "outcome": c.outcome, "recovered_job": c.recovered_job_number,
        "evidence": c.outcome_evidence,
    } for c in unbooked], indent=2))
    print(f"  wrote artifacts to {OUT_DIR}/{tenant.key}_{stamp}.*")

    # 7. Deliver.
    print(f"  SUBJECT: {subject}")
    if dry_run:
        print("  DRY-RUN: not sending.")
    else:
        res = D.send_email(tenant.recipients, subject, html)
        print(f"  SendGrid: {res}")
    return subject, html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="lex_portfolio")
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    run(a.tenant, a.date, a.dry_run or a.no_send, a.limit)


if __name__ == "__main__":
    main()
