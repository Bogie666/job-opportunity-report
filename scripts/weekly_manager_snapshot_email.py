#!/usr/bin/env python3
"""Weekly executive ROI email for the Service Manager Opportunity Snapshot program.

Default window: the most recently completed Mon-Sun week (relative to America/Chicago).
Reads the nightly revenue-refreshed outcomes ledger, computes deduped ROI metrics
for the window (and program-to-date), renders a navy/gold HTML email, and sends
via SendGrid.

Usage:
  python scripts/weekly_manager_snapshot_email.py                 # last full week
  python scripts/weekly_manager_snapshot_email.py --from 2026-06-17 --to 2026-06-27
  python scripts/weekly_manager_snapshot_email.py --no-send       # render only, no email
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from manager_snapshot_roi_report import build_metrics, num  # noqa: E402

try:
    from report_card_facts import load_env  # noqa: E402
except Exception:  # pragma: no cover
    def load_env(path: str, override: bool = False) -> None:
        p = Path(path)
        if not p.exists():
            return
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if override or k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")

OUTCOMES_DIR = ROOT / "data" / "outcomes"
OUTCOMES_CSV = OUTCOMES_DIR / "manager_snapshot_outcomes.csv"
OUT_DIR = ROOT / "out"
CT = ZoneInfo("America/Chicago")

NAVY = "#0b1f3a"
GOLD = "#c8a04b"
INK = "#1a1a1a"
MUTED = "#6b7280"
LINE = "#e5e7eb"

DEFAULT_RECIPIENTS = "ryan@lexairconditioning.com,cory@lexairconditioning.com"


def load_all_env() -> None:
    for p in [
        "/workspace/openclaw/MOVING/credentials/MASTER.env",
        "/workspace/apps/openclaw-credential-archive/20260526T032211Z/secrets/MOVING/credentials/MASTER.env",
        "/workspace/.secrets/hermes.env",
        "/workspace/apps/lex-monthly-insights/.env",
        str(ROOT / ".env"),
    ]:
        load_env(p, override=(p in {str(ROOT / ".env"), "/workspace/apps/lex-monthly-insights/.env"}))


def last_full_week() -> tuple[str, str]:
    """Return (monday, sunday) ISO dates for the most recently completed week."""
    today = datetime.now(CT).date()
    # Monday of the current week
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday.isoformat(), last_sunday.isoformat()


def esc(s: Any) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("—", "-").replace("–", "-")
    )


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def kpi_card(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div style="font-size:12px;color:{MUTED};margin-top:2px;">{esc(sub)}</div>' if sub else ""
    return (
        f'<td style="padding:14px 16px;background:#fff;border:1px solid {LINE};border-radius:10px;text-align:center;vertical-align:top;">'
        f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED};">{esc(label)}</div>'
        f'<div style="font-size:24px;font-weight:700;color:{NAVY};margin-top:4px;">{esc(value)}</div>'
        f'{sub_html}</td>'
    )


def bucket_rows(b: dict, name_key: str, sort_by_rev: bool = True) -> str:
    items = list(b.items())
    if sort_by_rev:
        items.sort(key=lambda kv: -kv[1]["rev"])
    else:
        items.sort(key=lambda kv: kv[0])
    out = []
    for k, v in items:
        out.append(
            f'<tr>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};color:{INK};">{esc(k)}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};text-align:center;color:{INK};">{v["jobs"]}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};text-align:center;color:{INK};">{v["won"]} ({pct(v["conv"])})</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};text-align:right;font-weight:600;color:{NAVY};">${v["rev"]:,.0f}</td>'
            f'</tr>'
        )
    return "".join(out)


def render_html(wk: dict, ptd: dict, d_from: str, d_to: str) -> str:
    def fmt_range(a: str, b: str) -> str:
        da = datetime.fromisoformat(a).strftime("%b %-d")
        db = datetime.fromisoformat(b).strftime("%b %-d, %Y")
        return f"{da} - {db}"

    week_label = fmt_range(d_from, d_to)
    excl_note = ""
    if wk["excluded_opportunities"]:
        excl_note = (
            f'<div style="font-size:12px;color:{MUTED};margin-top:8px;">'
            f'Excludes {wk["excluded_opportunities"]} lead-driven job(s) '
            f'(${wk["excluded_revenue"]:,.0f}, {", ".join(wk["excluded_job_types"])}) - '
            f'internal/Comfort Advisor sales, not snapshot-surfaced demand.</div>'
        )

    # Maturity banner: flag when the week's revenue is still settling.
    maturity_note = ""
    if not wk["all_jobs_matured"]:
        maturity_note = (
            f'<div style="margin-top:12px;padding:10px 14px;background:#fff8e6;border:1px solid {GOLD};border-left:4px solid {GOLD};border-radius:8px;font-size:12px;color:#7a5b12;">'
            f'<strong>Still accruing:</strong> {wk["fresh_opportunities"]} of this week\'s opportunities are inside the '
            f'{wk["maturity_days"]}-day settle window. Matured (settled) revenue so far: '
            f'<strong>${wk["matured_revenue"]:,.0f}</strong> across {wk["matured_opportunities"]} opps '
            f'({pct(wk["matured_conversion_rate"])} conversion). Totals will rise as ServiceTitan invoices finalize.</div>'
        )

    top_rows = []
    for r in wk["top_converted"]:
        top_rows.append(
            f'<tr>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};text-align:right;font-weight:700;color:{NAVY};">${r["revenue"]:,.0f}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};color:{INK};">{esc(r["customer"])}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};color:{MUTED};">{esc(r["job_type"])}</td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid {LINE};color:{MUTED};">{esc(r["date"])}</td>'
            f'</tr>'
        )
    top_html = "".join(top_rows) or f'<tr><td colspan="4" style="padding:10px;color:{MUTED};">No converted opportunities in this window.</td></tr>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:20px 0;"><tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:96%;background:#fff;border-radius:14px;overflow:hidden;border:1px solid {LINE};">
  <tr><td style="background:{NAVY};padding:22px 26px;">
    <div style="color:{GOLD};font-size:12px;letter-spacing:.14em;text-transform:uppercase;font-weight:600;">LEX Air Conditioning - Service Manager Program</div>
    <div style="color:#fff;font-size:21px;font-weight:700;margin-top:4px;">Weekly Opportunity Snapshot ROI</div>
    <div style="color:#c7d2e3;font-size:13px;margin-top:3px;">{week_label}</div>
  </td></tr>
  <tr><td style="padding:22px 26px;">
    <div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED};font-weight:600;margin-bottom:10px;">This Week</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="8"><tr>
      {kpi_card("Opportunities", str(wk["unique_opportunities"]))}
      {kpi_card("Conversion", pct(wk["conversion_rate"]), f'{wk["converted"]} won')}
      {kpi_card("Revenue", f'${wk["total_revenue"]:,.0f}')}
      {kpi_card("Avg Ticket", f'${wk["avg_ticket_on_converted"]:,.0f}')}
    </tr></table>
    {excl_note}
    {maturity_note}

    <div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED};font-weight:600;margin:22px 0 8px;">By Job Type</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {LINE};border-radius:8px;border-collapse:separate;overflow:hidden;">
      <tr style="background:#f9fafb;"><th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Type</th>
      <th style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Opps</th>
      <th style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Won</th>
      <th align="right" style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Revenue</th></tr>
      {bucket_rows(wk["by_job_type"], "type")}
    </table>

    <div style="font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED};font-weight:600;margin:22px 0 8px;">Top Converted Opportunities</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {LINE};border-radius:8px;border-collapse:separate;overflow:hidden;">
      <tr style="background:#f9fafb;"><th align="right" style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Revenue</th>
      <th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Customer</th>
      <th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Type</th>
      <th align="left" style="padding:8px 10px;font-size:11px;text-transform:uppercase;color:{MUTED};">Date</th></tr>
      {top_html}
    </table>

    <div style="margin-top:24px;padding:16px 18px;background:#f9fafb;border:1px solid {LINE};border-radius:10px;">
      <div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED};font-weight:600;margin-bottom:6px;">Program To Date</div>
      <div style="font-size:14px;color:{INK};line-height:1.6;">
        {ptd["unique_opportunities"]} opportunities &nbsp;|&nbsp; {pct(ptd["conversion_rate"])} conversion &nbsp;|&nbsp;
        <strong style="color:{NAVY};">${ptd["total_revenue"]:,.0f}</strong> booked/sold &nbsp;|&nbsp;
        ${ptd["revenue_per_opportunity"]:,.0f}/opp
      </div>
    </div>
  </td></tr>
  <tr><td style="padding:14px 26px;background:{NAVY};">
    <div style="color:#c7d2e3;font-size:11px;line-height:1.5;">
      Metrics dedupe by job_id (same-day re-runs never double-count). Revenue reflects realized invoices and sold estimates from ServiceTitan as of the nightly outcome refresh.
    </div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def sendgrid_send(html: str, subject: str, recipients: list[str]) -> dict[str, Any]:
    api_key = os.environ["SENDGRID_API_KEY"]
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "alerts@lexairconditioning.com")
    from_name = os.environ.get("SENDGRID_FROM_NAME", "LEX Service Manager Program")
    payload = {
        "personalizations": [{"to": [{"email": e} for e in recipients]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": "Weekly Opportunity Snapshot ROI (HTML email)."},
            {"type": "text/html", "value": html},
        ],
    }
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=60,
    )
    x_msg_id = r.headers.get("X-Message-Id")
    print(f"SendGrid status: {r.status_code} | X-Message-Id: {x_msg_id}")
    if r.status_code >= 300:
        raise RuntimeError(f"SendGrid failed: {r.status_code} {r.text[:500]}")
    return {"status_code": r.status_code, "x_message_id": x_msg_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--recipients", default=os.environ.get("WEEKLY_ROI_RECIPIENTS", DEFAULT_RECIPIENTS))
    ap.add_argument("--no-send", action="store_true")
    args = ap.parse_args()

    load_all_env()
    d_from, d_to = (args.d_from, args.d_to)
    if not (d_from and d_to):
        d_from, d_to = last_full_week()

    rows = list(csv.DictReader(OUTCOMES_CSV.open(newline="")))
    wk = build_metrics(rows, date_from=d_from, date_to=d_to)
    ptd = build_metrics(rows)

    html = render_html(wk, ptd, d_from, d_to)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    html_path = OUT_DIR / f"{stamp}_weekly_roi_{d_from}_{d_to}.html"
    html_path.write_text(html)
    print(f"Rendered {html_path}")
    print(f"Window {d_from}..{d_to}: {wk['unique_opportunities']} opps, {wk['converted']} won "
          f"({pct(wk['conversion_rate'])}), ${wk['total_revenue']:,.0f}")

    subject = f"Weekly Opportunity Snapshot ROI | {d_from} to {d_to} | ${wk['total_revenue']:,.0f}"
    recipients = [e.strip() for e in args.recipients.split(",") if e.strip()]

    if args.no_send:
        print("--no-send set; skipping SendGrid.")
        return 0
    if wk["unique_opportunities"] == 0:
        print("No opportunities in window; skipping send.")
        return 0

    delivery = sendgrid_send(html, subject, recipients)
    print(f"Sent to {', '.join(recipients)} | {delivery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
