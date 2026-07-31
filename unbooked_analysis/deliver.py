"""SendGrid delivery for the morning brief.

Creds in /workspace/context/company/secrets/sendgrid.env (SENDGRID_-prefixed).
FROM = SENDGRID_FROM_EMAIL (alerts@lexairconditioning.com).
"""
from __future__ import annotations
import os
import requests


def send_email(to_emails: list[str], subject: str, html: str) -> dict:
    key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "alerts@lexairconditioning.com")
    from_name = os.environ.get("SENDGRID_FROM_NAME", "LEX Ops Alerts")
    if not key:
        raise RuntimeError("SENDGRID_API_KEY not set")
    payload = {
        "personalizations": [{"to": [{"email": e} for e in to_emails]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    r = requests.post("https://api.sendgrid.com/v3/mail/send",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=30)
    return {"status": r.status_code, "ok": r.status_code in (200, 201, 202),
            "body": r.text[:300] if r.status_code >= 400 else ""}
