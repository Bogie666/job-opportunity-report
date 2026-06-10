"""Email a folder of pre-job tech briefs (markdown) to a recipient.
Renders each brief inline as styled HTML in the email body, and attaches the raw .md files.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

import markdown as md  # type: ignore

BRIEF_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else None
RECIPIENT = sys.argv[2] if len(sys.argv) > 2 else "rymint82@gmail.com"
# Support comma-separated list of recipients
RECIPIENTS = [r.strip() for r in RECIPIENT.split(",") if r.strip()]

if not BRIEF_DIR or not BRIEF_DIR.exists():
    raise SystemExit(f"Brief dir not found: {BRIEF_DIR}")


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    for raw in open(path, errors="ignore"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env("/workspace/openclaw/MOVING/credentials/MASTER.env")

STYLE = """
<style>
  body { font-family: Arial, Helvetica, sans-serif; color: #172033; line-height: 1.45; max-width: 820px; }
  h1 { color: #1a3a5c; font-size: 18px; margin: 0 0 6px 0; }
  h2 { color: #1a3a5c; font-size: 14px; margin: 14px 0 4px 0; border-bottom: 1px solid #d4d9e0; padding-bottom: 2px; }
  h3 { color: #1a3a5c; font-size: 13px; margin: 10px 0 4px 0; }
  ul { margin: 4px 0 6px 18px; padding: 0; }
  li { margin: 2px 0; }
  hr { border: none; border-top: 1px solid #d4d9e0; margin: 18px 0; }
  .brief { padding: 14px 18px; border: 1px solid #e3e7ee; border-radius: 6px; margin: 18px 0; background: #ffffff; }
  .tech-read { background: #f5f7fb; padding: 10px 12px; border-left: 4px solid #DAA520; margin: 8px 0 12px 0; }
  .tech-read p:first-child { margin-top: 0; }
  .tech-read p:last-child { margin-bottom: 0; }
  code { font-family: Menlo, Consolas, monospace; font-size: 12px; background: #f1f3f7; padding: 0 3px; }
</style>
"""


def render_brief(md_text: str) -> str:
    if md_text.startswith("## Tech read"):
        # Split tech-read block from the rest
        lines = md_text.splitlines()
        # find first '# Pre-Job Brief' line
        split_idx = None
        for i, line in enumerate(lines):
            if line.startswith("# Pre-Job Brief"):
                split_idx = i
                break
        if split_idx is not None:
            head_md = "\n".join(lines[1:split_idx]).strip()
            body_md = "\n".join(lines[split_idx:])
            head_html = md.markdown(head_md, extensions=["fenced_code"])
            body_html = md.markdown(body_md, extensions=["fenced_code", "tables"])
            return f'<div class="brief"><div class="tech-read"><strong>Tech read</strong>{head_html}</div>{body_html}</div>'
    return f'<div class="brief">{md.markdown(md_text, extensions=["fenced_code", "tables"])}</div>'


def attach(path: Path, mime: str) -> dict:
    return {
        "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        "type": mime,
        "filename": path.name,
        "disposition": "attachment",
    }


def main() -> None:
    md_files = sorted(BRIEF_DIR.glob("*.md"))
    if not md_files:
        raise SystemExit(f"No markdown briefs in {BRIEF_DIR}")

    sections = [render_brief(p.read_text()) for p in md_files]
    intro = f"""
    <p>Below are {len(md_files)} prototype pre-job briefs for jobs scheduled tomorrow. Each blends booking-stage CSR notes, installed equipment history, prior visit history (including invoice line items), customer/location notes, and membership status. The shaded "Tech read" at the top of each brief is an AI distillation of the structured source data below it.</p>
    <h3 style="color:#1a3a5c;">Test sample</h3>
    <p>This batch spans HVAC maintenance, plumbing safety inspection, and electrical safety inspection to test breadth across business units. The assigned technician name shows "TBD" because dispatch for tomorrow has not been committed yet.</p>
    <p style="color:#5c6675;font-size:12px;">All data pulled read-only from ServiceTitan. No production records modified.</p>
    <hr/>
    """

    html_body = f"<html><head>{STYLE}</head><body>" + intro + "".join(sections) + "</body></html>"

    text = f"Attached are {len(md_files)} prototype pre-job technician briefs for jobs scheduled tomorrow. View the HTML version of this email for inline-rendered briefs."

    subject = f"LEX Pre-Job Technician Briefs Prototype — {len(md_files)} jobs (tomorrow {datetime.now().strftime('%a %b %d, %Y')} test)"

    key = os.environ["SENDGRID_API_KEY"]
    payload = {
        "personalizations": [{"to": [{"email": addr} for addr in RECIPIENTS]}],
        "from": {
            "email": os.environ["SENDGRID_FROM_EMAIL"],
            "name": os.environ.get("SENDGRID_FROM_NAME", "LEX Reporting"),
        },
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html", "value": html_body},
        ],
        "attachments": [attach(p, "text/markdown") for p in md_files],
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=data,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            print("status", resp.status, resp.headers.get("X-Message-Id"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        print("HTTPError", exc.code, body)
        raise

    print("Sent inline + attached", len(md_files), "briefs to", ", ".join(RECIPIENTS))


if __name__ == "__main__":
    main()
