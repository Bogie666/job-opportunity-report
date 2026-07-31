"""Executive morning brief — PE-style HTML.

Structure (per Ryan's format decision — bucketed breakdown, iterate later):
  1. Top sheet: date, total unbooked, true leaks (still-unbooked B-E), recovered KPI, noise (A)
  2. Recoverable hot list (still-unbooked B + price-objection D) with phone + campaign
  3. Bucketed breakdown, each call: name/phone/campaign/category + one-line reason + outcome
"""
from __future__ import annotations
from datetime import datetime
from collections import defaultdict

from .sources.base import Call

BUCKET_LABEL = {
    "A": "Not a true lead (noise)",
    "B": "Lost — process failure 🔴",
    "C": "Lost — capacity / availability",
    "D": "Lost — customer declined",
    "E": "Lost — geo / scope mismatch",
}
BUCKET_ORDER = ["B", "C", "D", "E", "A"]  # leaks first, noise last


def _fmt_phone(d: str) -> str:
    d = "".join(c for c in (d or "") if c.isdigit())
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return d or "(unknown)"


def _outcome_badge(o: str | None) -> str:
    return {
        "booked_on_call": '<span style="color:#0a7d33;font-weight:600;">✅ Booked on call</span>',
        "recovered": '<span style="color:#b8860b;font-weight:600;">🟡 Recovered</span>',
        "still_unbooked": '<span style="color:#c41820;font-weight:600;">🔴 Still unbooked</span>',
    }.get(o or "", o or "—")


def build_brief(display_name: str, day_label: str, calls: list[Call]) -> tuple[str, str]:
    """Returns (subject, html)."""
    # Outcome tallies
    total = len(calls)
    still = [c for c in calls if c.outcome == "still_unbooked"]
    recovered = [c for c in calls if c.outcome == "recovered"]
    booked = [c for c in calls if c.outcome == "booked_on_call"]

    # True leaks = still-unbooked in B-E (A is noise)
    true_leaks = [c for c in still if c.reason_bucket in ("B", "C", "D", "E")]
    process_leaks = [c for c in true_leaks if c.reason_bucket == "B"]

    # Recoverable hot list = still-unbooked B + price-objection D
    hot = [c for c in still if c.reason_bucket == "B"
           or (c.reason_bucket == "D" and "price" in (c.reason_detail or "").lower())]

    subject = (f"Unbooked Calls — {day_label}: {len(true_leaks)} true leaks, "
               f"{len(process_leaks)} process failures, {len(recovered)} recovered")

    def call_row(c: Call) -> str:
        return (
            f'<tr>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{c.customer_name or "—"}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-family:monospace;">{_fmt_phone(c.from_number)}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{c.campaign_name or "—"}'
            f'{f"<br><span style=color:#888;font-size:11px;>{c.campaign_category}</span>" if c.campaign_category else ""}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{c.reason_detail or "—"}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;white-space:nowrap;">{_outcome_badge(c.outcome)}'
            f'{f"<br><span style=color:#888;font-size:11px;>job {c.recovered_job_number}</span>" if c.recovered_job_number else ""}</td>'
            f'</tr>'
        )

    # Hot list block
    hot_block = ""
    if hot:
        rows = "".join(
            f'<li style="margin:4px 0;"><b>{c.customer_name or "Caller"}</b> — '
            f'<span style="font-family:monospace;">{_fmt_phone(c.from_number)}</span> — '
            f'{c.campaign_name or ""} — <i>{c.reason_detail or ""}</i></li>'
            for c in hot)
        hot_block = (
            '<div style="background:#fff4f4;border-left:4px solid #c41820;padding:12px 16px;margin:16px 0;">'
            f'<div style="font-weight:700;color:#c41820;margin-bottom:6px;">☎️ CALL THESE BACK NOW ({len(hot)})</div>'
            f'<ul style="margin:0;padding-left:18px;">{rows}</ul></div>'
        )

    # Bucketed sections
    by_bucket: dict[str, list[Call]] = defaultdict(list)
    for c in calls:
        by_bucket[c.reason_bucket or "B"].append(c)

    sections = ""
    for b in BUCKET_ORDER:
        group = by_bucket.get(b, [])
        if not group:
            continue
        rows = "".join(call_row(c) for c in group)
        sections += (
            f'<h3 style="margin:22px 0 6px;color:#1a2e44;">{BUCKET_LABEL[b]} '
            f'<span style="color:#888;font-weight:400;">({len(group)})</span></h3>'
            '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
            '<tr style="text-align:left;color:#555;">'
            '<th style="padding:6px 10px;">Customer</th><th style="padding:6px 10px;">Phone</th>'
            '<th style="padding:6px 10px;">Campaign</th><th style="padding:6px 10px;">Reason</th>'
            '<th style="padding:6px 10px;">Outcome</th></tr>'
            f'{rows}</table>'
        )

    def kpi(label, val, color):
        return (f'<td style="text-align:center;padding:14px;background:{color};border-radius:8px;">'
                f'<div style="font-size:28px;font-weight:700;color:#fff;">{val}</div>'
                f'<div style="font-size:11px;color:#fff;opacity:.9;text-transform:uppercase;letter-spacing:.5px;">{label}</div></td>')

    html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#222;max-width:760px;margin:0 auto;padding:8px;">
<div style="border-bottom:3px solid #1a2e44;padding-bottom:10px;margin-bottom:8px;">
  <div style="font-size:20px;font-weight:700;color:#1a2e44;">{display_name}</div>
  <div style="color:#888;font-size:13px;">Daily Unbooked-Call Analysis · {day_label}</div>
</div>
<table style="width:100%;border-collapse:separate;border-spacing:8px 0;margin:12px 0;"><tr>
  {kpi("Unbooked total", total, "#5a6b7d")}
  {kpi("True leaks", len(true_leaks), "#c41820")}
  {kpi("Process failures", len(process_leaks), "#8b0000")}
  {kpi("Recovered", len(recovered), "#b8860b")}
  {kpi("Noise (excl.)", len(by_bucket.get("A", [])), "#8a99a8")}
</tr></table>
<p style="font-size:13px;color:#555;margin:6px 0;">
  <b>{len(true_leaks)}</b> genuinely lost opportunities after reconciliation.
  <b>{len(process_leaks)}</b> were our own process failures (recoverable).
  <b>{len(recovered)}</b> calls looked unbooked but a job was booked/called back within the window
  (not counted as leaks). <b>{len(booked)}</b> booked directly on the call.
</p>
{hot_block}
{sections}
<div style="margin-top:24px;padding-top:10px;border-top:1px solid #eee;color:#aaa;font-size:11px;">
  Generated {datetime.now().strftime('%Y-%m-%d %H:%M %Z')} · reconciliation window applied ·
  outcomes verified against ServiceTitan job data.
</div>
</body></html>"""
    return subject, html
