"""Capability map: probe one read-only GET per ServiceTitan API category to
determine which scopes are enabled on this integration app. Outputs a markdown
matrix to context/servicetitan/capability_map.md.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from client import ServiceTitanClient

# (Category, Module, GET path template, params)
# Picked the cheapest list endpoint per category that exists in the docs.
PROBES = [
    ("Accounting",       "accounting",  "/accounting/v2/tenant/{tenant}/invoices",                  {"pageSize": 1, "page": 1}),
    ("Accounting",       "accounting",  "/accounting/v2/tenant/{tenant}/payments",                  {"pageSize": 1, "page": 1}),
    ("Accounting",       "accounting",  "/accounting/v2/tenant/{tenant}/gl-accounts",               {"pageSize": 1, "page": 1}),
    ("CRM",              "crm",         "/crm/v2/tenant/{tenant}/customers",                        {"pageSize": 1, "page": 1}),
    ("CRM",              "crm",         "/crm/v2/tenant/{tenant}/locations",                        {"pageSize": 1, "page": 1}),
    ("CRM",              "crm",         "/crm/v2/tenant/{tenant}/leads",                            {"pageSize": 1, "page": 1}),
    ("CRM",              "crm",         "/crm/v2/tenant/{tenant}/bookings",                         {"pageSize": 1, "page": 1}),
    ("Dispatch",         "dispatch",    "/dispatch/v2/tenant/{tenant}/appointment-assignments",     {"pageSize": 1, "page": 1}),
    ("Dispatch",         "dispatch",    "/dispatch/v2/tenant/{tenant}/zones",                       {"pageSize": 1, "page": 1}),
    ("Equipment Systems","equipmentsystems","/equipmentsystems/v2/tenant/{tenant}/installed-equipment", {"pageSize": 1, "page": 1}),
    ("Forms",            "forms",       "/forms/v2/tenant/{tenant}/submissions",                    {"pageSize": 1, "page": 1}),
    ("Inventory",        "inventory",   "/inventory/v2/tenant/{tenant}/purchase-orders",            {"pageSize": 1, "page": 1}),
    ("Inventory",        "inventory",   "/inventory/v2/tenant/{tenant}/adjustments",                {"pageSize": 1, "page": 1}),
    ("Inventory",        "inventory",   "/inventory/v2/tenant/{tenant}/vendors",                    {"pageSize": 1, "page": 1}),
    ("JPM (Jobs)",       "jpm",         "/jpm/v2/tenant/{tenant}/jobs",                             {"pageSize": 1, "page": 1}),
    ("JPM (Jobs)",       "jpm",         "/jpm/v2/tenant/{tenant}/projects",                         {"pageSize": 1, "page": 1}),
    ("JPM (Jobs)",       "jpm",         "/jpm/v2/tenant/{tenant}/appointments",                     {"pageSize": 1, "page": 1}),
    ("Marketing",        "marketing",   "/marketing/v2/tenant/{tenant}/campaigns",                  {"pageSize": 1, "page": 1}),
    ("Marketing Reputation","marketingreputation","/marketingreputation/v2/tenant/{tenant}/reviews", {"pageSize": 1, "page": 1}),
    ("Memberships",      "memberships", "/memberships/v2/tenant/{tenant}/memberships",              {"pageSize": 1, "page": 1}),
    ("Memberships",      "memberships", "/memberships/v2/tenant/{tenant}/membership-types",         {"pageSize": 1, "page": 1}),
    ("Memberships",      "memberships", "/memberships/v2/tenant/{tenant}/invoice-templates",        {"pageSize": 1, "page": 1}),
    ("Payroll",          "payroll",     "/payroll/v2/tenant/{tenant}/gross-pay-items",              {"pageSize": 1, "page": 1}),
    ("Payroll",          "payroll",     "/payroll/v2/tenant/{tenant}/timesheet-codes",              {"pageSize": 1, "page": 1}),
    ("Pricebook",        "pricebook",   "/pricebook/v2/tenant/{tenant}/services",                   {"pageSize": 1, "page": 1}),
    ("Pricebook",        "pricebook",   "/pricebook/v2/tenant/{tenant}/materials",                  {"pageSize": 1, "page": 1}),
    ("Pricebook",        "pricebook",   "/pricebook/v2/tenant/{tenant}/equipment",                  {"pageSize": 1, "page": 1}),
    ("Reporting",        "reporting",   "/reporting/v2/tenant/{tenant}/report-categories",          None),
    ("Sales & Estimates","sales",       "/sales/v2/tenant/{tenant}/estimates",                      {"pageSize": 1, "page": 1}),
    ("Service Agreements","serviceagreements","/service-agreements/v2/tenant/{tenant}/service-agreements", {"pageSize": 1, "page": 1}),
    ("Settings",         "settings",    "/settings/v2/tenant/{tenant}/employees",                   {"pageSize": 1, "page": 1}),
    ("Settings",         "settings",    "/settings/v2/tenant/{tenant}/technicians",                 {"pageSize": 1, "page": 1}),
    ("Settings",         "settings",    "/settings/v2/tenant/{tenant}/business-units",              {"pageSize": 1, "page": 1}),
    ("Settings",         "settings",    "/settings/v2/tenant/{tenant}/tag-types",                   {"pageSize": 1, "page": 1}),
    ("Settings",         "settings",    "/settings/v2/tenant/{tenant}/user-roles",                  {"pageSize": 1, "page": 1}),
    ("Task Management",  "taskmanagement","/taskmanagement/v2/tenant/{tenant}/data/tasks",          {"pageSize": 1, "page": 1}),
    ("Telecom",          "telecom",     "/telecom/v2/tenant/{tenant}/calls",                        {"pageSize": 1, "page": 1}),
    ("Telecom",          "telecom",     "/telecom/v3/tenant/{tenant}/calls",                        {"pageSize": 1, "page": 1}),
]


def status_emoji(code: int) -> str:
    if code == 200: return "✅"
    if code == 403: return "🔒"
    if code == 404: return "❓"
    return "❌"


def reason(code: int, body: dict | str | None) -> str:
    if code == 200: return "enabled"
    if code == 403:
        if isinstance(body, dict):
            t = body.get("title") or body.get("message") or ""
            if "Scope" in t: return "scope not enabled"
            return t[:80] or "forbidden"
        return "forbidden"
    if code == 404: return "endpoint not found / wrong path"
    if isinstance(body, dict):
        return (body.get("title") or body.get("message") or "")[:80]
    return f"HTTP {code}"


def main() -> int:
    client = ServiceTitanClient()
    out_dir = Path("/workspace/context/servicetitan")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = {}
    rows = []
    for cat, mod, path, params in PROBES:
        r = client.get(path, params)
        try:
            body = r.json()
        except Exception:
            body = r.text[:200]
        total = None
        if isinstance(body, dict):
            total = body.get("totalCount")
        rows.append({
            "category": cat,
            "module": mod,
            "endpoint": path,
            "status": r.status_code,
            "total": total,
            "reason": reason(r.status_code, body),
        })
        raw[path] = {"status": r.status_code, "body": body}
        time.sleep(0.15)  # be polite

    # Write raw + markdown
    (out_dir / "capability_probe_raw.json").write_text(json.dumps(raw, indent=2, default=str))

    md = ["# ServiceTitan API Capability Map",
          "",
          f"Generated by `lex-servicetitan-reporting/src/capability_map.py`. ",
          "Each row = a single GET probe against this tenant's integration app.",
          "",
          "| Status | Category | Endpoint | Total rows | Notes |",
          "|---|---|---|---:|---|"]
    by_status: dict[int, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        md.append(f"| {status_emoji(r['status'])} {r['status']} "
                  f"| {r['category']} "
                  f"| `{r['endpoint']}` "
                  f"| {r['total'] if r['total'] is not None else ''} "
                  f"| {r['reason']} |")
    md.append("")
    md.append("## Summary")
    md.append("")
    for code, n in sorted(by_status.items()):
        md.append(f"- **{status_emoji(code)} {code}**: {n} endpoints")
    md.append("")
    md.append("Legend: ✅ enabled · 🔒 scope not enabled (request from ST admin) · ❓ endpoint path needs verification · ❌ other error")
    (out_dir / "capability_map.md").write_text("\n".join(md))

    # Console summary
    print(f"Probed {len(rows)} endpoints. Breakdown:")
    for code, n in sorted(by_status.items()): print(f"  {status_emoji(code)} {code}: {n}")
    print(f"\nReports:")
    print(f"  /workspace/context/servicetitan/capability_map.md")
    print(f"  /workspace/context/servicetitan/capability_probe_raw.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
