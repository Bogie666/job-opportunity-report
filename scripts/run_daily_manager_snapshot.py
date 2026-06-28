#!/usr/bin/env python3
"""Daily Service Manager Opportunity Snapshot runner.

Same-day HVAC only by default. Pulls scheduled ServiceTitan jobs, scores all HVAC
jobs deterministically, runs photo vision only on the selected top opportunities,
renders the compact manager snapshot email, and sends via SendGrid.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from servicetitan_dossier import (  # noqa: E402
    main as pull_dossiers,
    load_env,
    pull_dossier,
    build_lookups,
    build_brief_markdown,
)
from client import ServiceTitanClient  # noqa: E402
from score_and_filter_hvac_briefs import main as score_jobs  # noqa: E402
from opportunity_snapshot import build_snapshot  # noqa: E402
from render_manager_snapshot_email import render as render_email  # noqa: E402

DEFAULT_RECIPIENTS = "anthony@lexairconditioning.com,necey@lexairconditioning.com,john@lexairconditioning.com,ryan@lexairconditioning.com"
OUT_DIR = ROOT / "out"
LOG_DIR = ROOT / "logs"
OUTCOMES_DIR = ROOT / "data" / "outcomes"
FLAGGED_LEDGER = OUTCOMES_DIR / "manager_snapshot_flagged_jobs.csv"


def load_all_env() -> None:
    for p in [
        "/workspace/openclaw/MOVING/credentials/MASTER.env",
        "/workspace/apps/openclaw-credential-archive/20260526T032211Z/secrets/MOVING/credentials/MASTER.env",
        "/workspace/.secrets/hermes.env",
        "/workspace/apps/lex-monthly-insights/.env",
        str(ROOT / ".env"),
    ]:
        load_env(p, override=(p in {str(ROOT / ".env"), "/workspace/apps/lex-monthly-insights/.env"}))


def newest_dir(parent: Path, before: set[Path] | None = None) -> Path:
    before = before or set()
    dirs = [p for p in parent.iterdir() if p.is_dir() and p not in before]
    if not dirs:
        dirs = [p for p in parent.iterdir() if p.is_dir()]
    if not dirs:
        raise RuntimeError(f"No run dirs found in {parent}")
    return max(dirs, key=lambda p: p.stat().st_mtime)


def run_cmd(cmd: list[str], *, timeout: int = 1800) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True, timeout=timeout)


def load_json(path: Path, fallback: Any = None) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text())


def esc_text(s: Any) -> str:
    return str(s or "").replace("—", "-").replace("–", "-")


def _fresh_job(client: ServiceTitanClient, job_id: str) -> dict[str, Any] | None:
    resp = client.get(f"/jpm/v2/tenant/{{tenant}}/jobs/{job_id}")
    if resp.status_code != 200:
        print(f"fresh customer check skipped for {job_id}: job lookup HTTP {resp.status_code}")
        return None
    return resp.json()


def _refresh_bundle_if_customer_changed(
    bundle: dict[str, Any],
    *,
    client: ServiceTitanClient,
    refreshed_dir: Path | None = None,
) -> dict[str, Any]:
    """Re-pull selected dossiers if the job's customer/location changed after the initial batch pull.

    Same-day jobs can be booked against a prior homeowner/customer record, then corrected by dispatch/CSR
    before or during the appointment. The manager snapshot email should reflect the current ServiceTitan
    customer at send time, not the stale customer from the initial pull.
    """
    dossier = bundle.get("dossier") or bundle
    cached_job = dossier.get("job") or {}
    job_id = str(cached_job.get("id") or "")
    if not job_id:
        return bundle
    fresh = _fresh_job(client, job_id)
    if not fresh:
        return bundle
    changed = any(
        str(cached_job.get(key) or "") != str(fresh.get(key) or "")
        for key in ("customerId", "locationId")
    )
    if not changed:
        return bundle

    old_customer = ((dossier.get("customer") or {}).get("name") or "").strip()
    fresh_dossier = pull_dossier(client, fresh)
    bu_ids = {fresh.get("businessUnitId")} if fresh.get("businessUnitId") else set()
    jt_ids = {fresh.get("jobTypeId")} if fresh.get("jobTypeId") else set()
    emp_ids = {
        a.get("technicianId") or a.get("employeeId")
        for a in fresh_dossier.get("assignments", [])
        if a.get("technicianId") or a.get("employeeId")
    }
    lookups = build_lookups(client, bu_ids, jt_ids, emp_ids)
    brief, meta = build_brief_markdown(fresh_dossier, lookups)
    new_customer = ((fresh_dossier.get("customer") or {}).get("name") or meta.get("customer") or "").strip()
    refreshed_bundle = {"meta": meta, "dossier": fresh_dossier, "refresh_note": {
        "reason": "ServiceTitan customer/location changed after initial batch pull",
        "old_customer": old_customer,
        "new_customer": new_customer,
        "old_customer_id": cached_job.get("customerId"),
        "new_customer_id": fresh.get("customerId"),
        "old_location_id": cached_job.get("locationId"),
        "new_location_id": fresh.get("locationId"),
    }}
    if refreshed_dir:
        refreshed_dir.mkdir(parents=True, exist_ok=True)
        safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(fresh.get("jobNumber") or job_id))
        out = refreshed_dir / f"{safe_job}_refreshed_customer.json"
        out.write_text(json.dumps(refreshed_bundle, indent=2, default=str))
    print(f"Refreshed selected job {job_id}: customer changed {old_customer!r} -> {new_customer!r}")
    return refreshed_bundle


def build_selected_records(
    selected_path: Path,
    vision_dir: Path | None,
    *,
    refresh_client: ServiceTitanClient | None = None,
    refreshed_dir: Path | None = None,
) -> list[dict[str, Any]]:
    selected = load_json(selected_path, [])
    records: list[dict[str, Any]] = []
    for rec in selected:
        dossier_path = Path(rec["dossier_json"])
        bundle = load_json(dossier_path, {})
        if refresh_client:
            bundle = _refresh_bundle_if_customer_changed(bundle, client=refresh_client, refreshed_dir=refreshed_dir)
        job_id = str(rec.get("job_id") or "")
        vision = load_json((vision_dir / f"{job_id}.json"), None) if vision_dir else None
        md, snapshot = build_snapshot(bundle, vision=vision)
        meta = bundle.get("meta") or {}
        records.append({
            "job_id": job_id,
            "job_number": rec.get("job_number") or meta.get("job_number"),
            "customer": (meta.get("customer") or rec.get("customer") or "").strip(),
            "job_type": rec.get("job_type") or meta.get("job_type"),
            "appointment": meta.get("appointment") or "",
            "score": rec.get("score"),
            "drivers": rec.get("drivers"),
            "snapshot": snapshot,
            "snapshot_markdown": md,
            "dossier_json": str(dossier_path),
        })
    return records


def sendgrid_lookup(api_key: str, subject: str, recipients: list[str], x_msg_id: str | None) -> list[dict[str, Any]]:
    q = f'subject="{subject}"'
    try:
        r = requests.get(
            "https://api.sendgrid.com/v3/messages",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"query": q, "limit": 25},
            timeout=45,
        )
        if r.status_code != 200:
            return [{"error": f"messages http {r.status_code}: {r.text[:200]}"}]
        msgs = r.json().get("messages", [])
    except Exception as exc:
        return [{"error": f"messages exception: {type(exc).__name__}: {exc}"}]
    if x_msg_id:
        matching = [m for m in msgs if str(m.get("msg_id") or "").startswith(x_msg_id)]
        if matching:
            msgs = matching
    wanted = {r.lower() for r in recipients}
    out = []
    for m in msgs:
        if wanted and str(m.get("to_email") or "").lower() not in wanted:
            continue
        out.append({
            "to_email": m.get("to_email"),
            "status": m.get("status"),
            "last_event_time": m.get("last_event_time"),
            "msg_id": m.get("msg_id"),
        })
    return out


def send_email(html: str, subject: str, recipients: list[str]) -> dict[str, Any]:
    api_key = os.environ["SENDGRID_API_KEY"]
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "alerts@lexairconditioning.com")
    from_name = os.environ.get("SENDGRID_FROM_NAME", "LEX Service Manager Triage")
    payload = {
        "personalizations": [{"to": [{"email": e} for e in recipients]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": "Service Manager Opportunity Snapshot attached in HTML email body."},
            {"type": "text/html", "value": html},
        ],
    }
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    print(f"SendGrid status: {r.status_code}")
    x_msg_id = r.headers.get("X-Message-Id")
    print(f"X-Message-Id: {x_msg_id}")
    if r.status_code >= 300:
        raise RuntimeError(f"SendGrid failed: {r.status_code} {r.text[:500]}")
    statuses: list[dict[str, Any]] = []
    for _ in range(8):
        time.sleep(15)
        statuses = sendgrid_lookup(api_key, subject, recipients, x_msg_id)
        print("Activity:", statuses)
        if statuses and all(s.get("status") in {"delivered", "not_delivered", "bounce", "dropped"} for s in statuses if "error" not in s):
            break
    return {"status_code": r.status_code, "x_message_id": x_msg_id, "activity": statuses}


def append_flagged_ledger(
    records: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    run_id: str,
    scheduled_date: str,
    delivery: dict[str, Any],
) -> None:
    """Append one row per emailed top-10 job for later revenue tracking."""
    import csv

    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    fields = [
        "run_id", "run_at_utc", "scheduled_date", "selected_rank", "job_id", "job_number",
        "customer", "job_type", "score", "grade", "staffing", "drivers", "recipients",
        "subject", "html_path", "records_path", "selected_path", "sendgrid_status_code",
        "sendgrid_x_message_id", "delivery_activity_json",
    ]
    existing = set()
    if FLAGGED_LEDGER.exists():
        with FLAGGED_LEDGER.open(newline="") as f:
            existing = {(r.get("run_id"), r.get("job_id")) for r in csv.DictReader(f)}
    is_new = not FLAGGED_LEDGER.exists()
    with FLAGGED_LEDGER.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if is_new:
            writer.writeheader()
        for idx, rec in enumerate(records, 1):
            key = (run_id, str(rec.get("job_id") or ""))
            if key in existing:
                continue
            snap = rec.get("snapshot") or {}
            writer.writerow({
                "run_id": run_id,
                "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "scheduled_date": scheduled_date,
                "selected_rank": idx,
                "job_id": rec.get("job_id"),
                "job_number": rec.get("job_number"),
                "customer": rec.get("customer"),
                "job_type": rec.get("job_type"),
                "score": rec.get("score"),
                "grade": snap.get("grade"),
                "staffing": snap.get("staffing"),
                "drivers": rec.get("drivers"),
                "recipients": ",".join(summary.get("recipients") or []),
                "subject": summary.get("subject"),
                "html_path": summary.get("html_path"),
                "records_path": summary.get("records_path"),
                "selected_path": summary.get("selected_path"),
                "sendgrid_status_code": delivery.get("status_code"),
                "sendgrid_x_message_id": delivery.get("x_message_id"),
                "delivery_activity_json": json.dumps(delivery.get("activity") or [], default=str),
            })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-ahead", type=int, default=int(os.environ.get("MANAGER_SNAPSHOT_DAYS_AHEAD", "0")), help="0=same-day, 1=tomorrow")
    ap.add_argument("--max-jobs", type=int, default=int(os.environ.get("MANAGER_SNAPSHOT_MAX_JOBS", "200")))
    ap.add_argument("--threshold", type=int, default=int(os.environ.get("MANAGER_SNAPSHOT_THRESHOLD", "35")))
    ap.add_argument("--top-n", type=int, default=int(os.environ.get("MANAGER_SNAPSHOT_TOP_N", "10")))
    ap.add_argument("--recipients", default=os.environ.get("MANAGER_SNAPSHOT_RECIPIENTS", DEFAULT_RECIPIENTS))
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--skip-vision", action="store_true")
    args = ap.parse_args()

    load_all_env()
    OUT_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    tech_dir = ROOT / "data" / "tech_briefs"
    before = {p for p in tech_dir.iterdir() if p.is_dir()} if tech_dir.exists() else set()

    pull_dossiers(days_ahead=args.days_ahead, max_jobs=args.max_jobs, trade_filter="hvac")
    run_dir = newest_dir(tech_dir, before)
    print(f"RUN_DIR={run_dir}")

    run_cmd([sys.executable, "scripts/fetch_job_photos.py", str(run_dir)], timeout=3600)
    photo_dir = run_dir.parent / f"{run_dir.name}_photos"
    manifest = photo_dir / "manifest.json"
    if not manifest.exists():
        raise RuntimeError(f"Missing photo manifest: {manifest}")

    score_jobs(run_dir, manifest, threshold=args.threshold, top_n=args.top_n)
    scoring_dir = run_dir.parent / f"{run_dir.name}_scoring"
    selected_path = scoring_dir / "selected.json"
    selected = load_json(selected_path, [])
    print(f"SELECTED={len(selected)} path={selected_path}")

    vision_dir = None
    if selected and not args.skip_vision:
        run_cmd([
            sys.executable,
            "scripts/analyze_selected_photos.py",
            "--manifest", str(manifest),
            "--selected", str(selected_path),
            "--briefs-dir", str(run_dir),
        ], timeout=2400)
        vision_dir = photo_dir / "vision"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = OUT_DIR / f"{stamp}_manager_snapshot_daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    records = build_selected_records(
        selected_path,
        vision_dir,
        refresh_client=ServiceTitanClient(),
        refreshed_dir=out_dir / "refreshed_selected_dossiers",
    )
    records_path = out_dir / "selected_records.json"
    records_path.write_text(json.dumps(records, indent=2, default=str))

    index = load_json(run_dir / "index.json", {})
    window_start = index.get("window_start_local") or "same-day"
    date_label = window_start[:10] if isinstance(window_start, str) else "today"
    title = f"HVAC Service Manager Opportunity Snapshot | {date_label}"
    html = render_email(records, title=title)
    html_path = out_dir / "manager_snapshot_email.html"
    html_path.write_text(html)

    recipients = [e.strip() for e in args.recipients.split(",") if e.strip()]
    subject = f"HVAC Service Manager Opportunity Snapshot | {date_label} | {len(records)} selected"
    delivery = {"skipped": True, "reason": "--no-send"}
    if not args.no_send:
        delivery = send_email(html, subject, recipients)

    summary = {
        "run_dir": str(run_dir),
        "photo_manifest": str(manifest),
        "selected_path": str(selected_path),
        "records_path": str(records_path),
        "html_path": str(html_path),
        "recipients": recipients,
        "subject": subject,
        "selected_count": len(records),
        "delivery": delivery,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    append_flagged_ledger(records, summary, run_id=stamp, scheduled_date=date_label, delivery=delivery)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
