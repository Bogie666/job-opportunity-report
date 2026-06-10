#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from servicetitan_dossier import load_env, fetch_all, build_lookups, pull_dossier, build_brief_markdown  # noqa: E402
from client import ServiceTitanClient  # noqa: E402

ENV_PATHS = [
    "/workspace/openclaw/MOVING/credentials/MASTER.env",
    "/workspace/apps/openclaw-credential-archive/20260526T032211Z/secrets/MOVING/credentials/MASTER.env",
    "/workspace/.secrets/hermes.env",
    str(ROOT / ".env"),
]

def load_all_env():
    for p in ENV_PATHS:
        load_env(p, override=(Path(p).resolve() == (ROOT / ".env").resolve() if Path(p).exists() else False))

def find_job(c: ServiceTitanClient, job_number: str) -> dict:
    # ServiceTitan's jobs endpoint filters exact job number via the parameter named "number".
    jobs = fetch_all(c, "/jpm/v2/tenant/{tenant}/jobs", {"number": job_number}, hard_limit=10)
    for job in jobs:
        if str(job.get("jobNumber")) == str(job_number) or str(job.get("id")) == str(job_number):
            return job
    raise SystemExit(f"No exact job found for number/id {job_number}. Returned {len(jobs)} candidate(s).")

def main(job_number: str):
    load_all_env()
    c = ServiceTitanClient()
    job = find_job(c, job_number)
    lookups = build_lookups(
        c,
        {job.get("businessUnitId")} if job.get("businessUnitId") else set(),
        {job.get("jobTypeId")} if job.get("jobTypeId") else set(),
        set(),
    )
    dossier = pull_dossier(c, job)
    brief, meta = build_brief_markdown(dossier, lookups)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{job_number}"
    out = ROOT / "data" / "tech_briefs" / stamp
    out.mkdir(parents=True, exist_ok=True)
    slug = f"{job.get('jobNumber')}_{meta['job_type'].replace(' ', '_').replace('/', '_')}"[:80]
    md_path = out / f"{slug}.md"
    json_path = out / f"{slug}.json"
    md_path.write_text(brief)
    json_path.write_text(json.dumps({"meta": meta, "dossier": dossier}, indent=2, default=str))
    (out / "index.json").write_text(json.dumps({"results":[{"job_id": job.get("id"), "job_number": job.get("jobNumber"), "markdown": str(md_path), "json": str(json_path)}]}, indent=2))
    print(out)
    print(md_path)
    print(json_path)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: pull_job_by_number.py <job_number>")
    main(sys.argv[1])
