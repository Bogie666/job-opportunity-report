#!/usr/bin/env python3
"""Send the selected report cards as a combined navy/gold email via SendGrid."""
from __future__ import annotations
import base64, io, json, os, re, sys, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from report_card_facts import load_default_env  # noqa: E402

load_default_env()

OUT_DIR = Path(sys.argv[1])  # e.g. out/20260612T194612Z_report_cards
SELECTED = json.loads(Path(sys.argv[2]).read_text())
RECIPIENTS = sys.argv[3].split(",")
WINDOW_LABEL = sys.argv[4]  # "Monday, June 15, 2026"

SUBJECT = f"HVAC Opportunity Report Cards — {WINDOW_LABEL} (Top {len(SELECTED)} jobs)"
FROM_EMAIL = os.environ["SENDGRID_FROM_EMAIL"]
FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "LEX Air Field Intelligence")
API_KEY = os.environ["SENDGRID_API_KEY"]

# Build combined HTML body
TOPBAR = "background:#1a3a5c;color:#fff;border-radius:14px 14px 0 0;padding:24px 28px;font-family:Arial,Helvetica,sans-serif;"
TOPBAR_H1 = "margin:6px 0 4px;font-size:26px;color:#fff;"
BRAND = "font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#DAA520;font-weight:700;"
SUB = "color:#dce6f2;font-size:13px;"
SUMMARY_CARD = "background:#fff;border:1px solid #e1e7ef;border-radius:0 0 14px 14px;padding:18px 24px;margin:0 0 22px;font-family:Arial,Helvetica,sans-serif;"
WRAPPER = "max-width:880px;margin:0 auto;padding:18px 12px 40px;background:#f4f6f9;font-family:Arial,Helvetica,sans-serif;color:#172033;"

summary_rows = []
for i, rec in enumerate(SELECTED, 1):
    summary_rows.append(
        f"<tr><td style='padding:6px 8px;border-bottom:1px solid #eef1f6;'><strong>{i}.</strong></td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f6;'>Job #{rec['job_number']}</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f6;'>{rec.get('customer','')}</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f6;text-align:right;'><strong>Score {rec['score']}</strong></td></tr>"
    )

body = (
    f"<div style='{WRAPPER}'>"
    f"<div style='{TOPBAR}'>"
    f"<div style='{BRAND}'>LEX AIR · FIELD INTELLIGENCE</div>"
    f"<h1 style='{TOPBAR_H1}'>HVAC Opportunity Report Cards</h1>"
    f"<div style='{SUB}'>{WINDOW_LABEL} · Top {len(SELECTED)} scored opportunities</div>"
    f"</div>"
    f"<div style='{SUMMARY_CARD}'>"
    f"<p style='margin:0 0 10px;'><strong>Pre-visit intelligence for Monday's HVAC schedule.</strong> "
    f"Cards are ranked by deterministic opportunity score (equipment age, open estimate dollars, "
    f"membership, photo signals, repair-note keywords). Read-only ServiceTitan pull. Verify on arrival.</p>"
    f"<table style='width:100%;border-collapse:collapse;font-size:14px;margin-top:10px;'>"
    f"{''.join(summary_rows)}"
    f"</table>"
    f"<p style='margin:14px 0 0;font-size:12px;color:#6b7280;'>This is a testing-phase product. "
    f"Each card includes photo-vision findings and AI coaching notes anchored to the customer's actual history. "
    f"MD and HTML source attached.</p>"
    f"</div>"
)

# Append each rendered card HTML (extract inner section to avoid nested <html>)
for rec in SELECTED:
    html_text = (OUT_DIR / f"{rec['job_number']}.html").read_text()
    # Strip outer html/body wrappers, keep just the styled section div
    m = re.search(r"<body[^>]*>(.*)</body>", html_text, re.S)
    inner = m.group(1) if m else html_text
    body += f"<div style='margin:18px 0;'>{inner}</div>"

body += "</div>"

# ZIP of MD + HTML
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    for rec in SELECTED:
        for ext in ("md", "html"):
            p = OUT_DIR / f"{rec['job_number']}.{ext}"
            zf.write(p, p.name)
zip_bytes = buf.getvalue()

attachments = [{
    "content": base64.b64encode(zip_bytes).decode("ascii"),
    "type": "application/zip",
    "filename": f"report_cards_{WINDOW_LABEL.replace(',','').replace(' ','_')}.zip",
    "disposition": "attachment",
}]

payload = {
    "personalizations": [{"to": [{"email": e.strip()} for e in RECIPIENTS]}],
    "from": {"email": FROM_EMAIL, "name": FROM_NAME},
    "subject": SUBJECT,
    "content": [
        {"type": "text/plain", "value": f"Top {len(SELECTED)} HVAC opportunity report cards for {WINDOW_LABEL}. See HTML version."},
        {"type": "text/html", "value": body},
    ],
    "attachments": attachments,
}

r = requests.post(
    "https://api.sendgrid.com/v3/mail/send",
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    json=payload, timeout=60,
)
print(f"SendGrid status: {r.status_code}")
print(f"X-Message-Id: {r.headers.get('X-Message-Id')}")
delivery_log = {
    "subject": SUBJECT,
    "recipients": RECIPIENTS,
    "from": FROM_EMAIL,
    "sent_at_utc": datetime.now(timezone.utc).isoformat(),
    "status_code": r.status_code,
    "x_message_id": r.headers.get("X-Message-Id"),
    "body_excerpt": r.text[:400],
}
log_path = OUT_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_sendgrid_delivery.json"
log_path.write_text(json.dumps(delivery_log, indent=2))
print(f"Log: {log_path}")

# Poll Activity API for delivered status
time.sleep(20)
for _ in range(6):
    msg_id = (r.headers.get("X-Message-Id") or "").strip()
    q = f'subject="{SUBJECT}"'
    ar = requests.get("https://api.sendgrid.com/v3/messages",
        headers={"Authorization": f"Bearer {API_KEY}"},
        params={"query": q, "limit": 10}, timeout=30)
    try:
        msgs = ar.json().get("messages", [])
        matching = [m for m in msgs if msg_id and m.get("msg_id","").startswith(msg_id)]
        statuses = [(m.get("to_email"), m.get("status"), m.get("last_event_time")) for m in (matching or msgs)]
        print("Activity:", statuses)
        if statuses and all(s[1] in {"delivered","not_delivered","bounce","dropped"} for s in statuses):
            break
    except Exception as e:
        print("activity poll err:", e)
    time.sleep(15)
