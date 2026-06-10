"""ServiceTitan job dossier builder for Customer Opportunity Report Cards.

For each scheduled job in a target date window, pulls:
  - Job summary, type, business unit, scheduled appointment window
  - Customer + location (name, address, tags, custom fields)
  - Installed equipment at the location (HVAC focus)
  - Membership status
  - Recent customer + location + job notes
  - Past job history at the same location (last N)
  - Past invoice line items at the same location to surface recent work/issues

Generates:
  - JSON dossier per job for the report-card renderer
  - Lightweight markdown only as a local pull artifact/reference

Read-only ServiceTitan. No production records are modified.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

from client import ServiceTitanClient  # noqa: E402

CENTRAL = timezone(timedelta(hours=-5))  # CDT (May)
OUT_DIR = BASE / "data" / "tech_briefs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PAST_JOB_LIMIT = 20
NOTES_LIMIT = 6


def load_env(path: str, *, override: bool = False) -> None:
    if not os.path.exists(path):
        return
    for raw in open(path, errors="ignore"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def central_window(days_ahead: int = 1) -> tuple[str, str, datetime]:
    now_local = datetime.now(CENTRAL)
    start_local = (now_local + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        start_local,
    )


def fetch_all(c: ServiceTitanClient, path: str, params: dict[str, Any] | None = None, hard_limit: int = 500) -> list[dict]:
    out: list[dict] = []
    page = 1
    params = dict(params or {})
    while True:
        params["page"] = page
        params["pageSize"] = min(50, hard_limit - len(out))
        r = c.get(path, params=params)
        if r.status_code != 200:
            break
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        items = data.get("data", []) if isinstance(data, dict) else []
        out.extend(items)
        if not data.get("hasMore") or len(out) >= hard_limit:
            break
        page += 1
    return out


def get_json(c: ServiceTitanClient, path: str, params: dict[str, Any] | None = None) -> Any:
    r = c.get(path, params=params)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def list_jobs(c: ServiceTitanClient, start_utc: str, end_utc: str) -> list[dict]:
    return fetch_all(c, "/jpm/v2/tenant/{tenant}/jobs", {
        "firstAppointmentStartsOnOrAfter": start_utc,
        "firstAppointmentStartsBefore": end_utc,
        "jobStatus": "Scheduled",
    }, hard_limit=500)


def build_lookups(c: ServiceTitanClient, business_unit_ids: set[int], job_type_ids: set[int], employee_ids: set[int]) -> dict[str, dict[int, str]]:
    bus, jts, emps = {}, {}, {}
    
    # Fetch ALL business units (not just requested IDs) to ensure pagination
    if business_unit_ids:
        items = fetch_all(c, "/settings/v2/tenant/{tenant}/business-units")
        bus = {i["id"]: i.get("name") for i in items}
    
    # Fetch ALL job types with full pagination (not ID-filtered) to catch all types including newer ones
    if job_type_ids:
        items = fetch_all(c, "/jpm/v2/tenant/{tenant}/job-types")
        jts = {i["id"]: i.get("name") for i in items}
    
    if employee_ids:
        ids = ",".join(str(i) for i in employee_ids)
        items = fetch_all(c, "/settings/v2/tenant/{tenant}/employees", {"ids": ids})
        emps = {i["id"]: i.get("name") for i in items}
        items = fetch_all(c, "/settings/v2/tenant/{tenant}/technicians", {"ids": ids})
        emps.update({i["id"]: i.get("name") for i in items if i.get("name")})
    
    return {"business_units": bus, "job_types": jts, "employees": emps}


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\r", "")
    # strip simple HTML
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</?(div|p|span|b|u|i|h\d|ul|ol|li|a|strong|em)[^>]*>", "", s, flags=re.IGNORECASE)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def pull_dossier(c: ServiceTitanClient, job: dict) -> dict:
    job_id = job["id"]
    customer_id = job.get("customerId")
    location_id = job.get("locationId")

    appts = fetch_all(c, "/jpm/v2/tenant/{tenant}/appointments", {"jobId": job_id})
    appt = next((a for a in appts if a["id"] == job.get("firstAppointmentId")), appts[0] if appts else None)

    assignments = []
    if appt:
        assignments = fetch_all(c, "/dispatch/v2/tenant/{tenant}/appointment-assignments", {"appointmentIds": appt["id"]})

    customer = get_json(c, f"/crm/v2/tenant/{{tenant}}/customers/{customer_id}") if customer_id else None
    location = get_json(c, f"/crm/v2/tenant/{{tenant}}/locations/{location_id}") if location_id else None
    customer_notes = fetch_all(c, f"/crm/v2/tenant/{{tenant}}/customers/{customer_id}/notes") if customer_id else []
    location_notes = fetch_all(c, f"/crm/v2/tenant/{{tenant}}/locations/{location_id}/notes") if location_id else []
    job_notes = fetch_all(c, f"/jpm/v2/tenant/{{tenant}}/jobs/{job_id}/notes")
    history_data = get_json(c, f"/jpm/v2/tenant/{{tenant}}/jobs/{job_id}/history") or {}
    history = (history_data or {}).get("history", []) if isinstance(history_data, dict) else []

    equipment = fetch_all(c, "/equipmentsystems/v2/tenant/{tenant}/installed-equipment", {"locationIds": location_id}, hard_limit=50) if location_id else []
    memberships = fetch_all(c, "/memberships/v2/tenant/{tenant}/memberships", {"customerIds": customer_id}, hard_limit=20) if customer_id else []

    past_jobs = fetch_all(c, "/jpm/v2/tenant/{tenant}/jobs", {"locationId": location_id}, hard_limit=PAST_JOB_LIMIT * 3) if location_id else []
    past_jobs = [j for j in past_jobs if j["id"] != job_id]
    past_jobs.sort(key=lambda j: j.get("completedOn") or j.get("createdOn") or "", reverse=True)
    past_jobs = past_jobs[:PAST_JOB_LIMIT]

    past_invoices = fetch_all(c, "/accounting/v2/tenant/{tenant}/invoices", {
        "jobIds": ",".join(str(j["id"]) for j in past_jobs[:8])
    }, hard_limit=50) if past_jobs else []

    # Past + open estimates at this location (re-pitch / declined-options intelligence)
    estimates = fetch_all(
        c, "/sales/v2/tenant/{tenant}/estimates",
        {"locationId": location_id, "active": "True"},
        hard_limit=60,
    ) if location_id else []

    return {
        "job": job,
        "appointment": appt,
        "assignments": assignments,
        "customer": customer,
        "location": location,
        "customer_notes": customer_notes,
        "location_notes": location_notes,
        "job_notes": job_notes,
        "history": history,
        "installed_equipment": equipment,
        "memberships": memberships,
        "past_jobs": past_jobs,
        "past_invoices": past_invoices,
        "estimates": estimates,
    }


def fmt_appt_local(appt: dict | None) -> str:
    if not appt:
        return "Unscheduled"
    try:
        start = datetime.fromisoformat(appt["start"].replace("Z", "+00:00")).astimezone(CENTRAL)
        win_start = datetime.fromisoformat(appt["arrivalWindowStart"].replace("Z", "+00:00")).astimezone(CENTRAL)
        win_end = datetime.fromisoformat(appt["arrivalWindowEnd"].replace("Z", "+00:00")).astimezone(CENTRAL)
        return f"{start.strftime('%a %b %d, %Y')}  arrival window {win_start.strftime('%I:%M %p').lstrip('0')} – {win_end.strftime('%I:%M %p').lstrip('0')} CT"
    except Exception:
        return appt.get("start", "")


def equipment_summary(equipment: list[dict]) -> list[str]:
    rows = []
    for eq in equipment:
        if not eq.get("active"):
            continue
        raw_name = eq.get("name") or eq.get("type") or "Equipment"
        if isinstance(raw_name, dict):
            raw_name = raw_name.get("name") or "Equipment"
        raw_type = eq.get("type")
        if isinstance(raw_type, dict):
            raw_type = raw_type.get("name")
        name = str(raw_name).strip() or str(raw_type or "Equipment")
        mfg = eq.get("manufacturer") or ""
        model = eq.get("model") or ""
        sn = eq.get("serialNumber") or ""
        installed = (eq.get("installedOn") or "")[:10]
        age = ""
        if installed and installed > "1900":
            try:
                years = (datetime.now(timezone.utc) - datetime.fromisoformat(installed + "T00:00:00+00:00")).days // 365
                age = f"~{years}y old"
            except Exception:
                pass
        bits = [b for b in [mfg, model] if b]
        line = f"- {name}"
        if bits:
            line += f" — {' '.join(bits)}"
        if sn:
            line += f" (SN {sn})"
        meta = []
        if installed and installed > "1900":
            meta.append(f"installed {installed}")
        if age:
            meta.append(age)
        if eq.get("predictedReplacementDate") and eq["predictedReplacementDate"] > "1990":
            meta.append(f"predicted replace {eq['predictedReplacementDate'][:10]}")
        if meta:
            line += f"  [{'; '.join(meta)}]"
        rows.append(line)
    return rows


def membership_summary(memberships: list[dict]) -> list[str]:
    rows = []
    for m in memberships:
        status = m.get("status") or ""
        bf = m.get("billingFrequency") or ""
        f = (m.get("from") or "")[:10]
        t = (m.get("to") or "") or (m.get("cancellationDate") or "")
        t = t[:10] if t else ""
        line = f"- {status} {bf} membership ({f} → {t or 'open'})"
        if m.get("memo"):
            line += f" — {m['memo']}"
        rows.append(line)
    return rows


def notes_summary(notes: list[dict], limit: int = NOTES_LIMIT) -> list[str]:
    notes_sorted = sorted(notes, key=lambda n: n.get("createdOn") or "", reverse=True)
    rows = []
    for n in notes_sorted:
        text = clean_text(n.get("text"))
        if not text:
            continue
        low = text.lower()
        # Skip noisy auto-logged outreach with no substance
        if "broccoli ai outbound" in low and "voicemail left" in low:
            continue
        if low.startswith("https://") and len(text) < 120:
            continue
        when = (n.get("createdOn") or "")[:10]
        rows.append(f"- {when}: {text}")
        if len(rows) >= limit:
            break
    return rows


def past_jobs_summary(past_jobs: list[dict], past_invoices: list[dict], job_types: dict[int, str]) -> list[str]:
    inv_by_job: dict[int, list[dict]] = {}
    for inv in past_invoices:
        jb = inv.get("job") or {}
        if jb and jb.get("id"):
            inv_by_job.setdefault(jb["id"], []).append(inv)
    rows = []
    for j in past_jobs:
        when = (j.get("completedOn") or j.get("createdOn") or "")[:10]
        jt = job_types.get(j.get("jobTypeId"), str(j.get("jobTypeId") or ""))
        status = j.get("jobStatus") or ""
        summary = clean_text(j.get("summary")) or clean_text(j.get("summaryOfWork")) or ""
        if len(summary) > 280:
            summary = summary[:280] + "…"
        line = f"- {when} · {jt} · {status}"
        if summary:
            line += f"\n  {summary}"
        invs = inv_by_job.get(j["id"]) or []
        skus = []
        for inv in invs:
            for it in inv.get("items", [])[:6]:
                name = it.get("skuName") or it.get("displayName") or it.get("description") or ""
                if name:
                    skus.append(name.strip())
        skus = [s for s in dict.fromkeys(skus) if s][:8]
        if skus:
            line += f"\n  Invoice items: {', '.join(skus)}"
        rows.append(line)
    return rows


def build_brief_markdown(dossier: dict, lookups: dict) -> tuple[str, dict]:
    job = dossier["job"]
    customer = dossier["customer"] or {}
    location = dossier["location"] or {}
    appt = dossier["appointment"]
    bu_name = lookups["business_units"].get(job.get("businessUnitId"), str(job.get("businessUnitId") or ""))
    jt_name = lookups["job_types"].get(job.get("jobTypeId"), str(job.get("jobTypeId") or ""))

    tech_lines = []
    for a in dossier["assignments"]:
        emp_id = a.get("technicianId") or a.get("employeeId")
        name = lookups["employees"].get(emp_id, str(emp_id))
        tech_lines.append(f"- {name}")
    if not tech_lines:
        tech_lines.append("- TBD (not yet dispatched)")

    cust_name = customer.get("name") or ""
    addr = location.get("address") or {}
    addr_line = ", ".join([b for b in [addr.get("street"), addr.get("city"), addr.get("state"), addr.get("zip")] if b])

    loc_fields = []
    for cf in (location.get("customFields") or []):
        if cf.get("value"):
            loc_fields.append(f"{cf['name']}: {cf['value']}")

    flags = []
    if customer.get("doNotService"):
        flags.append("⚠️ DO NOT SERVICE flag on customer")
    if (customer.get("balance") or 0) and float(customer.get("balance") or 0) != 0:
        flags.append(f"⚠️ Account balance: ${customer.get('balance')}")
    if appt and appt.get("specialInstructions"):
        flags.append(f"Special instructions: {clean_text(appt['specialInstructions'])}")

    md = []
    md.append(f"# Pre-Job Brief · {jt_name}")
    md.append(f"**Job #{job['jobNumber']}** · {bu_name}")
    md.append(f"**When:** {fmt_appt_local(appt)}")
    md.append(f"**Customer:** {cust_name}")
    md.append(f"**Address:** {addr_line}")
    if loc_fields:
        md.append(f"**Location:** {' · '.join(loc_fields)}")
    md.append("")
    md.append("## Technician")
    md.extend(tech_lines)

    if flags:
        md.append("")
        md.append("## Heads up")
        for f in flags:
            md.append(f"- {f}")

    md.append("")
    md.append("## Reason for visit")
    md.append(clean_text(job.get("summary")) or "(no summary on the job)")

    eq_rows = equipment_summary(dossier["installed_equipment"])
    if eq_rows:
        md.append("")
        md.append("## Installed equipment at this location")
        md.extend(eq_rows[:10])

    mem_rows = membership_summary(dossier["memberships"])
    if mem_rows:
        md.append("")
        md.append("## Memberships")
        md.extend(mem_rows[:6])

    jn = notes_summary(dossier["job_notes"], limit=NOTES_LIMIT)
    if jn:
        md.append("")
        md.append("## Recent job notes")
        md.extend(jn)

    ln = notes_summary(dossier["location_notes"], limit=NOTES_LIMIT)
    if ln:
        md.append("")
        md.append("## Recent location notes")
        md.extend(ln)

    cn = notes_summary(dossier["customer_notes"], limit=NOTES_LIMIT)
    if cn:
        md.append("")
        md.append("## Recent customer notes")
        md.extend(cn)

    pj = past_jobs_summary(dossier["past_jobs"], dossier["past_invoices"], lookups["job_types"])
    if pj:
        md.append("")
        md.append(f"## Past visits at this address (last {len(pj)})")
        md.extend(pj)

    md.append("")
    md.append("---")
    md.append("Read-only data pulled from ServiceTitan. Verify findings on site.")
    return "\n".join(md), {
        "job_id": job["id"],
        "job_number": job["jobNumber"],
        "job_type": jt_name,
        "business_unit": bu_name,
        "customer": cust_name,
        "address": addr_line,
        "appointment": fmt_appt_local(appt),
    }


def openrouter_narrative(brief_md: str, dossier: dict) -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    sys_msg = (
        "You write a pre-job brief for an HVAC/plumbing/electrical technician at LEX Air Conditioning. "
        "Tone: direct, operator-focused, no fluff, no emojis, no markdown headers. "
        "Use ONLY facts in the provided brief. Output exactly two sections separated by a blank line:\n"
        "1) A single tight paragraph (3-5 sentences) summarizing the home, equipment age, membership status, "
        "and key history relevant to today's visit.\n"
        "2) A 'Go win the call:' line followed by 3 short bullet points (each a single actionable thing the tech "
        "should check, verify, or pitch — grounded in the data). Examples: 'Confirm warranty coil install before "
        "leaving', 'Pitch membership renewal — last annual expired Feb 2024', 'Inspect 2010 G61MPV furnaces, due "
        "for replacement per record'."
    )
    payload = {
        "model": "openai/gpt-4o",
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": brief_md[:12000]},
        ],
        "temperature": 0.3,
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://lexairconditioning.com",
            "X-Title": "LEX Tech Pre-Job Brief",
        },
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
    except HTTPError as exc:
        return f"(LLM narrative skipped: {exc.code} {exc.read().decode('utf-8', errors='replace')[:200]})"
    except Exception as exc:
        return f"(LLM narrative skipped: {exc})"


def _trade_from_names(jt_name: str, bu_name: str) -> str:
    blob = f"{jt_name} {bu_name}".lower()
    if "psi" in blob or "plumb" in blob:
        return "plumbing"
    if "esi" in blob or "electric" in blob:
        return "electrical"
    return "hvac"


def main(target_job_ids: list[int] | None = None, days_ahead: int = 1, max_jobs: int = 2, trade_filter: str | None = None) -> None:
    load_env("/workspace/openclaw/MOVING/credentials/MASTER.env")
    load_env(str(BASE / ".env"), override=True)

    c = ServiceTitanClient()
    start_utc, end_utc, start_local = central_window(days_ahead)
    print(f"Window: {start_local.strftime('%a %b %d %Y')} CT  ({start_utc} → {end_utc})")

    jobs = list_jobs(c, start_utc, end_utc)
    print(f"Scheduled jobs in window: {len(jobs)}")

    # Build lookups for the candidate set first so trade filtering can use human-readable BU/job type.
    all_bu_ids = {j["businessUnitId"] for j in jobs if j.get("businessUnitId")}
    all_jt_ids = {j["jobTypeId"] for j in jobs if j.get("jobTypeId")}
    lookups = build_lookups(c, all_bu_ids, all_jt_ids, set())

    if target_job_ids:
        jobs = [j for j in jobs if j["id"] in target_job_ids]
    else:
        if trade_filter:
            wanted = trade_filter.lower().strip()
            jobs = [
                j for j in jobs
                if _trade_from_names(
                    lookups["job_types"].get(j.get("jobTypeId"), str(j.get("jobTypeId") or "")),
                    lookups["business_units"].get(j.get("businessUnitId"), str(j.get("businessUnitId") or "")),
                ) == wanted
            ]
            print(f"Scheduled {wanted.upper()} jobs in window: {len(jobs)}")
        # Spread across business units to test breadth
        by_bu: dict[int, list[dict]] = {}
        for j in jobs:
            by_bu.setdefault(j.get("businessUnitId") or 0, []).append(j)
        picked: list[dict] = []
        # round-robin across BUs
        while len(picked) < max_jobs and any(by_bu.values()):
            for bu_id in list(by_bu.keys()):
                if not by_bu[bu_id]:
                    continue
                picked.append(by_bu[bu_id].pop(0))
                if len(picked) >= max_jobs:
                    break
        jobs = picked

    if not jobs:
        print("No matching jobs.")
        return

    # Narrow lookups to selected jobs; keep existing dict object shape.
    bu_ids = {j["businessUnitId"] for j in jobs if j.get("businessUnitId")}
    jt_ids = {j["jobTypeId"] for j in jobs if j.get("jobTypeId")}
    lookups = build_lookups(c, bu_ids, jt_ids, set())

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUT_DIR / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for job in jobs:
        print(f"\n→ Building brief for job {job['jobNumber']} (id {job['id']})")
        dossier = pull_dossier(c, job)
        # Extend tech lookup with assignment ids
        more_emp = {a.get("technicianId") or a.get("employeeId") for a in dossier["assignments"] if a.get("technicianId") or a.get("employeeId")}
        if more_emp:
            lookups2 = build_lookups(c, set(), set(), more_emp)
            lookups["employees"].update(lookups2["employees"])
        brief, meta = build_brief_markdown(dossier, lookups)
        narrative = openrouter_narrative(brief, dossier) if os.environ.get("LEX_BRIEF_USE_LLM") == "1" else None
        if narrative:
            brief = f"## Tech read (AI-summarized)\n{narrative}\n\n" + brief

        slug = f"{job['jobNumber']}_{meta['job_type'].replace(' ', '_').replace('/', '_')}"[:80]
        md_path = run_dir / f"{slug}.md"
        json_path = run_dir / f"{slug}.json"
        md_path.write_text(brief)
        json_path.write_text(json.dumps({"meta": meta, "dossier": dossier}, indent=2, default=str))
        results.append({"job_id": job["id"], "job_number": job["jobNumber"], "markdown": str(md_path), "json": str(json_path)})

    index_path = run_dir / "index.json"
    index_path.write_text(json.dumps({"window_start_local": start_local.isoformat(), "results": results}, indent=2))
    print(f"\nArtifacts: {run_dir}")
    for r in results:
        print(" -", r["markdown"])


if __name__ == "__main__":
    argv = sys.argv[1:]
    skip_next = False
    positional_ids = []
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in {"--days-ahead", "--max-jobs", "--trade"}:
            skip_next = True
            continue
        if arg.isdigit():
            positional_ids.append(int(arg))
    target_ids = positional_ids
    days_ahead = 1
    max_jobs = 5
    trade_filter = None
    for i, arg in enumerate(argv):
        if arg == "--days-ahead" and i + 1 < len(argv):
            days_ahead = int(argv[i + 1])
        elif arg == "--max-jobs" and i + 1 < len(argv):
            max_jobs = int(argv[i + 1])
        elif arg == "--trade" and i + 1 < len(argv):
            trade_filter = argv[i + 1]
    main(target_ids or None, days_ahead=days_ahead, max_jobs=max_jobs, trade_filter=trade_filter)
