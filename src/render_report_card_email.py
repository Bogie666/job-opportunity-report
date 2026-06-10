#!/usr/bin/env python3
"""Render the locked Customer Opportunity Report Card email (navy/gold layout).

This is the canonical layout Ryan approved on 2026-06-09 (Job #424675208 corrected
version). All styling is INLINE because Gmail/Outlook strip <style> blocks,
especially when sections are concatenated into a combined email.
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

import markdown as md  # type: ignore


# ---- Inline style dictionaries (kept as constants for reuse) ----

WRAP = "max-width:860px;margin:0 auto;padding:22px 14px 34px;font-family:Arial,Helvetica,sans-serif;color:#172033;line-height:1.45;background:#f4f6f9;"
TOPBAR = "background:#1a3a5c;color:#ffffff;border-radius:14px 14px 0 0;padding:20px 24px;font-family:Arial,Helvetica,sans-serif;"
BRAND = "font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#DAA520;font-weight:700;"
TOPBAR_H1 = "margin:6px 0 2px;font-size:24px;color:#ffffff;font-family:Arial,Helvetica,sans-serif;"
SUB = "color:#dce6f2;font-size:13px;"
GOLDBAR = "background:#DAA520;color:#111827;padding:10px 24px;font-weight:700;letter-spacing:.03em;font-family:Arial,Helvetica,sans-serif;"
CARD = "background:#ffffff;border:1px solid #e1e7ef;border-radius:12px;padding:18px 22px;margin:14px 0;box-shadow:0 2px 8px rgba(26,58,92,.06);"
CARD_REPORT = "background:#ffffff;border:1px solid #e1e7ef;border-radius:0 0 14px 14px;padding:18px 22px;margin-top:0;box-shadow:0 2px 8px rgba(26,58,92,.06);"
META_GRID = "display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 18px;font-size:13px;"
FOOTER = "font-size:11px;color:#6b7280;text-align:center;margin-top:16px;"

FLAG_STYLE = "background:#fdecea;border-left:4px solid #c0392b;padding:6px 10px;border-radius:4px;color:#7a1c14;font-weight:700;display:block;margin:4px 0;"


# ---- Markdown -> HTML, with inline styles injected onto each element ----

def _inline_markdown(markdown_text: str) -> str:
    body_html = md.markdown(markdown_text, extensions=["fenced_code", "tables"])

    # H1
    body_html = re.sub(
        r"<h1>",
        '<h1 style="color:#1a3a5c;font-size:22px;margin:0 0 12px;font-family:Arial,Helvetica,sans-serif;">',
        body_html,
    )
    # H2 (section dividers — gold underline)
    body_html = re.sub(
        r"<h2>",
        '<h2 style="color:#1a3a5c;font-size:16px;margin:22px 0 8px;padding-bottom:5px;border-bottom:2px solid #DAA520;font-family:Arial,Helvetica,sans-serif;">',
        body_html,
    )
    # H3
    body_html = re.sub(
        r"<h3>",
        '<h3 style="color:#1a3a5c;font-size:14px;margin:16px 0 6px;font-family:Arial,Helvetica,sans-serif;">',
        body_html,
    )
    # UL / LI
    body_html = re.sub(r"<ul>", '<ul style="margin:6px 0 10px 20px;padding:0;">', body_html)
    body_html = re.sub(r"<li>", '<li style="margin:4px 0;">', body_html)
    # P
    body_html = re.sub(r"<p>", '<p style="margin:6px 0;">', body_html)
    # strong
    body_html = re.sub(r"<strong>", '<strong style="color:#111827;">', body_html)

    # Red flag callout: any list item containing "FLAG"
    def _flagify(m: re.Match) -> str:
        inner = m.group(1)
        # strip leading style on the li (we wrote it above)
        return f'<li style="{FLAG_STYLE}">⚠ {inner.lstrip("⚠ ").lstrip()}</li>'

    body_html = re.sub(
        r'<li style="margin:4px 0;">(\s*(?:⚠|&#9888;|&#x26A0;)?\s*FLAG[^<]*)</li>',
        _flagify,
        body_html,
    )

    return body_html


def render(
    *,
    markdown_text: str,
    job_number: str,
    customer: str,
    call_type: str,
    primary: str,
    secondary: str,
    photo_qa: str,
    action_bar: str,
    data_source: str = "Read-only ServiceTitan pull + historical photo review",
    standalone: bool = True,
) -> str:
    """Render one report card.

    standalone=True returns a full <html>...</html> document (single-job email).
    standalone=False returns only the inner section so multiple cards can be
    safely concatenated into a combined email body.
    """
    body_html = _inline_markdown(markdown_text)
    section = (
        f'<div style="{WRAP}">'
        f'  <div style="{TOPBAR}">'
        f'    <div style="{BRAND}">LEX AIR · FIELD INTELLIGENCE</div>'
        f'    <h1 style="{TOPBAR_H1}">Customer Opportunity Report Card</h1>'
        f'    <div style="{SUB}">Job #{html.escape(job_number)} · {html.escape(customer)} · {html.escape(call_type)}</div>'
        f'  </div>'
        f'  <div style="{GOLDBAR}">{html.escape(action_bar)}</div>'
        f'  <div style="{CARD}">'
        f'    <div style="{META_GRID}">'
        f'      <div><strong style="color:#111827;">Primary:</strong> {html.escape(primary)}</div>'
        f'      <div><strong style="color:#111827;">Secondary:</strong> {html.escape(secondary)}</div>'
        f'      <div><strong style="color:#111827;">Photo QA:</strong> {html.escape(photo_qa)}</div>'
        f'      <div><strong style="color:#111827;">Data:</strong> {html.escape(data_source)}</div>'
        f'    </div>'
        f'  </div>'
        f'  <div style="{CARD_REPORT}">{body_html}</div>'
        f'  <div style="{FOOTER}">Generated read-only from ServiceTitan. No production records modified.</div>'
        f'</div>'
    )
    if not standalone:
        return section
    return f'<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;background:#f4f6f9;">{section}</body></html>'


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", required=True)
    ap.add_argument("--job-number", required=True)
    ap.add_argument("--customer", required=True)
    ap.add_argument("--call-type", required=True)
    ap.add_argument("--primary", required=True)
    ap.add_argument("--secondary", required=True)
    ap.add_argument("--photo-qa", required=True)
    ap.add_argument("--action-bar", default="Customer Opportunity Report Card")
    ap.add_argument("--data-source", default="Read-only ServiceTitan pull + historical photo review")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    md_text = Path(args.markdown).read_text()
    html_doc = render(
        markdown_text=md_text,
        job_number=args.job_number,
        customer=args.customer,
        call_type=args.call_type,
        primary=args.primary,
        secondary=args.secondary,
        photo_qa=args.photo_qa,
        action_bar=args.action_bar,
        data_source=args.data_source,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc)
    print(out)


if __name__ == "__main__":
    main()
