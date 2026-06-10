"""Connection verification: confirm OAuth, tenant ID, app key, and a basic API
read all work end-to-end. Prints a PE-style summary; never logs secret values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from client import ServiceTitanClient, STConfig

# Lightweight read-only probes. Ordered so the first that returns 200 confirms
# the integration is healthy. Each lives under tenant scope.
PROBES = [
    ("/settings/v2/tenant/{tenant}/employees",  {"pageSize": 1, "page": 1}),
    ("/settings/v2/tenant/{tenant}/technicians", {"pageSize": 1, "page": 1}),
    ("/jpm/v2/tenant/{tenant}/job-types",       {"pageSize": 1, "page": 1}),
    ("/dispatch/v2/tenant/{tenant}/business-hours", None),
]


def mask(s: str, keep: int = 4) -> str:
    if not s: return "(empty)"
    if len(s) <= keep * 2: return "*" * len(s)
    return s[:keep] + "..." + s[-keep:]


def main() -> int:
    cfg = STConfig.from_env()
    print("=" * 70)
    print("ServiceTitan Connection Verification")
    print("=" * 70)
    print(f"  Environment   : {cfg.env}")
    print(f"  Auth URL      : {cfg.auth_url}")
    print(f"  API base      : {cfg.api_base}")
    print(f"  Tenant ID     : {cfg.tenant_id}")
    print(f"  Client ID     : {mask(cfg.client_id)}")
    print(f"  App Key       : {mask(cfg.app_key)}")
    print()

    client = ServiceTitanClient(cfg)
    # Force a fresh token fetch so we surface auth errors immediately.
    print("[1/3] Requesting OAuth access token...")
    try:
        _ = client.token
    except Exception as e:
        print(f"  FAIL: {e}")
        return 2
    print(f"  OK   token acquired (cached to data/token_cache.json, expires in ~15min)")
    print()

    print("[2/3] Probing tenant-scoped endpoints (read-only)...")
    healthy = False
    sample = None
    sample_endpoint = None
    for path, params in PROBES:
        r = client.get(path, params)
        line = f"  {r.status_code:>3}  GET {path}"
        if params: line += f"  params={params}"
        print(line)
        if r.status_code == 200 and not healthy:
            healthy = True
            sample_endpoint = path
            try:
                sample = r.json()
            except Exception:
                sample = {"_raw": r.text[:200]}
        elif r.status_code >= 400:
            try:
                err = r.json()
                if isinstance(err, dict):
                    msg = err.get("title") or err.get("message") or err.get("error_description") or ""
                    if msg: print(f"        -> {msg[:200]}")
            except Exception:
                pass

    print()
    print("[3/3] Result")
    if not healthy:
        print("  FAIL: no probe returned 200. Check tenant_id, app key permissions, "
              "and which scopes are enabled on the integration app.")
        return 3

    print(f"  OK   connection verified via {sample_endpoint}")
    if isinstance(sample, dict):
        keys = list(sample.keys())[:8]
        total = sample.get("totalCount") or sample.get("totalRecords")
        page_size = sample.get("pageSize")
        has_more = sample.get("hasMore")
        print(f"       response keys: {keys}")
        if total is not None:    print(f"       totalCount   : {total}")
        if page_size is not None: print(f"       pageSize     : {page_size}")
        if has_more is not None: print(f"       hasMore      : {has_more}")
        data = sample.get("data")
        if isinstance(data, list) and data:
            row = data[0]
            keep = list(row.keys())[:10] if isinstance(row, dict) else []
            print(f"       sample row keys: {keep}")
    # Persist the raw sample for inspection
    out = Path(__file__).resolve().parent.parent / "data" / "connection_probe.json"
    out.write_text(json.dumps({"endpoint": sample_endpoint, "sample": sample}, indent=2, default=str))
    print(f"       sample saved -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
