#!/usr/bin/env python3
"""Render selected report cards (MD + HTML) for the top-N selected jobs."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_card_facts import load_default_env  # noqa: E402
from customer_opportunity_report_card import build_report_card  # noqa: E402
from render_report_card_email import render as render_html  # noqa: E402

load_default_env()

run_dir = Path(sys.argv[1])
selected_path = Path(sys.argv[2])
out_dir = Path(sys.argv[3])
out_dir.mkdir(parents=True, exist_ok=True)

vision_summary_path = run_dir.parent / (run_dir.name + "_photos") / "vision" / "selected_summary.json"
vision_all = json.loads(vision_summary_path.read_text()) if vision_summary_path.exists() else []
vision_by_job = {}
items = vision_all if isinstance(vision_all, list) else (vision_all.get("jobs") or vision_all.get("results") or [])
for rec in items:
    jid = str(rec.get("job_id") or rec.get("jobNumber") or rec.get("job_number") or "")
    if jid:
        vision_by_job[jid] = rec

def extract(md_text, label, default=""):
    m = re.search(rf"\*\*{re.escape(label)}:\*\*\s*([^\n]+)", md_text)
    return m.group(1).strip() if m else default

selected = json.loads(selected_path.read_text())
for rec in selected:
    job_id = str(rec["job_id"])
    job_no = rec["job_number"]
    bundle = json.loads(Path(rec["dossier_json"]).read_text())
    vision = vision_by_job.get(job_id) or vision_by_job.get(job_no)
    md_text = build_report_card(bundle, vision=vision)
    md_path = out_dir / f"{job_no}.md"
    md_path.write_text(md_text)

    customer = extract(md_text, "Customer", rec.get("customer") or "")
    call_type = extract(md_text, "Call Type", rec.get("job_type") or "")
    primary = extract(md_text, "Primary Opportunity")
    secondary = extract(md_text, "Secondary Opportunity")
    photo_qa = extract(md_text, "Photo QA", "Historical photos reviewed")
    action_bar = extract(md_text, "Likelihood to Purchase", "Verify on arrival")

    html_out = render_html(
        markdown_text=md_text,
        job_number=str(job_no),
        customer=customer,
        call_type=call_type,
        primary=primary,
        secondary=secondary,
        photo_qa=photo_qa,
        action_bar=action_bar,
        standalone=True,
    )
    (out_dir / f"{job_no}.html").write_text(html_out)
    print(f"{job_no} -> {md_path}")

print("DONE", out_dir)
