#!/usr/bin/env python3
"""Render Service Manager Opportunity Snapshot selected records as navy/gold HTML.

Input is a JSON list like selected_records_v*.json where each item contains:
  job_number, job_type, customer, appointment, score, snapshot
The snapshot may include red_flags and photo_findings from src/opportunity_snapshot.py.
"""
from __future__ import annotations
import html
import json
import sys
from pathlib import Path

NAVY = "#1a3a5c"
DARK = "#0f2540"
GOLD = "#DAA520"
RED_BG = "#fdecea"
RED = "#c0392b"


def esc(x) -> str:
    return html.escape(str(x or ""))


def grade_color(grade: str) -> str:
    return {"A+": "#2E8B57", "A": "#2E8B57", "B": "#2a5a8c", "C": "#b8860b", "D": "#6B7280"}.get(grade, "#6B7280")


def render(records: list[dict], title: str = "Service Manager Opportunity Snapshots") -> str:
    cards = []
    for rec in records:
        snap = rec.get("snapshot") or rec
        grade = esc(snap.get("grade") or "")
        staffing = esc(snap.get("staffing") or "")
        flags = snap.get("red_flags") or rec.get("red_flags") or []
        photos = snap.get("photo_findings") or rec.get("photo_findings") or []
        signals = snap.get("signals") or snap.get("bullets") or []
        pill_html = "".join(
            f"<span style='display:inline-block;background:{RED_BG};color:{RED};border:1px solid #f5b7b1;border-radius:999px;padding:5px 9px;margin:0 6px 6px 0;font-size:12px;font-weight:700;'>⚠ {esc(flag)}</span>"
            for flag in flags[:3]
        )
        photo_html = "".join(f"<li>{esc(p)}</li>" for p in photos[:3])
        signal_html = "".join(f"<li>{esc(s)}</li>" for s in signals[:4])
        cards.append(f"""
        <div style='background:#fff;border:1px solid #dde3ea;border-radius:14px;margin:16px 0;padding:18px 20px;border-top:5px solid {grade_color(grade)};'>
          <div style='display:flex;justify-content:space-between;gap:14px;align-items:flex-start;'>
            <div>
              <div style='font-size:12px;color:#6B7280;text-transform:uppercase;letter-spacing:.08em;font-weight:700;'>Job #{esc(rec.get('job_number'))} · {esc(rec.get('job_type'))}</div>
              <h2 style='margin:4px 0 2px;font-size:20px;color:{NAVY};'>{esc(rec.get('customer'))}</h2>
              <div style='font-size:13px;color:#6B7280;'>{esc(rec.get('appointment'))}</div>
            </div>
            <div style='min-width:74px;text-align:center;background:{grade_color(grade)};color:#fff;border-radius:12px;padding:9px 10px;font-weight:800;font-size:22px;'>{grade}</div>
          </div>
          <div style='margin-top:12px;color:{DARK};font-weight:700;'>Staffing: {staffing}</div>
          <p style='margin:10px 0 8px;font-size:14px;line-height:1.45;color:#172033;'>{esc(snap.get('headline') or snap.get('summary'))}</p>
          {f"<div style='margin:8px 0 6px;'>{pill_html}</div>" if pill_html else ""}
          <ul style='margin:8px 0 0 18px;padding:0;font-size:14px;line-height:1.45;color:#172033;'>{signal_html}</ul>
          {f"<div style='margin-top:10px;background:#f8fafc;border-left:4px solid {GOLD};padding:9px 12px;border-radius:8px;'><div style='font-weight:700;color:{NAVY};margin-bottom:4px;'>Valuable photo findings</div><ul style='margin:0 0 0 18px;padding:0;font-size:13px;line-height:1.4;'>{photo_html}</ul></div>" if photo_html else ""}
          {f"<div style='margin-top:10px;font-size:13px;color:#374151;'><strong style='color:{NAVY};'>Manager note:</strong> {esc(snap.get('manager_note'))}</div>" if snap.get('manager_note') else ""}
        </div>
        """)
    return f"""<!doctype html><html><body style='margin:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#172033;'>
      <div style='max-width:920px;margin:0 auto;padding:20px 14px 36px;'>
        <div style='background:{DARK};color:#fff;border-radius:16px 16px 0 0;padding:24px 28px;border-bottom:5px solid {GOLD};'>
          <div style='color:{GOLD};font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;'>LEX AIR · SERVICE MANAGER TRIAGE</div>
          <h1 style='margin:6px 0 4px;font-size:28px;'>{esc(title)}</h1>
          <div style='color:#dce6f2;font-size:14px;'>Demand calls weighted above maintenance. Warranty, recall, QC, callback, and recent-install issue calls excluded. Open estimates are weak context only.</div>
        </div>
        <div style='background:#fff;border:1px solid #dde3ea;border-top:0;border-radius:0 0 16px 16px;padding:16px 22px;margin-bottom:18px;font-size:14px;line-height:1.45;'>
          Short dispatch scan for manager review. Red badges are deterministic equipment-age flags from the same serial decoder and supersession logic used by full report cards.
        </div>
        {''.join(cards)}
      </div>
    </body></html>"""


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("Usage: render_manager_snapshot_email.py selected_records.json output.html [title]")
    records = json.loads(Path(sys.argv[1]).read_text())
    title = sys.argv[3] if len(sys.argv) > 3 else "Service Manager Opportunity Snapshots"
    Path(sys.argv[2]).write_text(render(records, title))
    print(sys.argv[2])


if __name__ == "__main__":
    main()
