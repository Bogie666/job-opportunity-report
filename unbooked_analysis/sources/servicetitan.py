"""ServiceTitan Telecom call source + ST-backed reconciliation.

Reuses the reporting app's ServiceTitanClient (OAuth, token cache, retries).
- list_calls: /telecom/v2/tenant/{tenant}/calls  (campaignId param is IGNORED; we
  read row.leadCall.campaign client-side). createdOnOrAfter/createdBefore DO work.
- get_recording: /telecom/v2/tenant/{tenant}/calls/{id}/recording (v2 only; v3 404s)
- find_recovery: 3-tier — direct leadCallId link, customer job-after-call, phone fallback.
"""
from __future__ import annotations
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, "/workspace/apps/lex-servicetitan-reporting")
from src.client import ServiceTitanClient  # noqa: E402

from ..sources.base import Call  # noqa: E402


def _digits(s: str | None) -> str:
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def _parse_duration(s: str | None) -> int:
    try:
        h, m, sec = (s or "0:0:0").split(":")
        return int(h) * 3600 + int(m) * 60 + int(float(sec))
    except Exception:
        return 0


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # ST sometimes returns >6 fractional digits; trim
        s2 = re.sub(r"(\.\d{6})\d+", r"\1", s)
        dt = datetime.fromisoformat(s2)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ServiceTitanCallSource:
    def __init__(self, st_tenant_id: str, client: ServiceTitanClient | None = None):
        self.st_tenant_id = st_tenant_id
        self.client = client or ServiceTitanClient()

    # -- list -----------------------------------------------------------------
    def list_calls(self, day_start_utc: datetime, day_end_utc: datetime) -> list[Call]:
        params = {
            "pageSize": 500,
            "createdOnOrAfter": day_start_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "createdBefore": day_end_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        out: list[Call] = []
        page = 1
        while True:
            params["page"] = page
            r = self.client.get("/telecom/v2/tenant/{tenant}/calls", params=params)
            if r.status_code != 200:
                raise RuntimeError(f"list_calls HTTP {r.status_code}: {r.text[:300]}")
            d = r.json()
            for row in d.get("data", []):
                lc = row.get("leadCall") or {}
                camp = lc.get("campaign") or {}
                cust = lc.get("customer") or {}
                out.append(Call(
                    id=str(lc.get("id") or row.get("id")),
                    received_at=_parse_dt(lc.get("receivedOn")),
                    direction=lc.get("direction") or "",
                    from_number=_digits(lc.get("from")),
                    to_number=_digits(lc.get("to")),
                    duration_sec=_parse_duration(lc.get("duration")),
                    call_type=lc.get("callType"),
                    campaign_name=camp.get("name"),
                    campaign_category=(camp.get("category") or {}).get("name"),
                    agent_name=(lc.get("agent") or {}).get("name"),
                    customer_id=str(cust.get("id")) if cust.get("id") else None,
                    customer_name=cust.get("name"),
                    job_number=str(row.get("jobNumber")) if row.get("jobNumber") else None,
                    recording_available=bool(lc.get("recordingUrl")),
                    raw=row,
                ))
            if not d.get("hasMore"):
                break
            page += 1
            if page > 30:
                break
        return out

    # -- recording ------------------------------------------------------------
    def get_recording(self, call_id: str) -> bytes | None:
        r = self.client.get(f"/telecom/v2/tenant/{{tenant}}/calls/{call_id}/recording")
        if r.status_code == 200 and "audio" in r.headers.get("Content-Type", ""):
            return r.content
        return None

    # -- reconciliation -------------------------------------------------------
    def find_recovery(self, call: Call, window_hours: int) -> dict[str, Any] | None:
        window_end = call.received_at + timedelta(hours=window_hours)

        # Tier 1: a job directly linked to THIS call (booked on call, or stapled later).
        # If the call row already carried a jobNumber it booked on the call.
        if call.job_number:
            return {"tier": "direct_link", "job_number": call.job_number,
                    "note": "job attached to the original call"}

        # Search jobs whose leadCallId == this call id.
        r = self.client.get("/jpm/v2/tenant/{tenant}/jobs",
                             params={"pageSize": 50, "createdOnOrAfter":
                                     call.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
        if r.status_code == 200:
            for j in r.json().get("data", []):
                if str(j.get("leadCallId") or "") == call.id:
                    return {"tier": "leadcall_link", "job_number": str(j.get("jobNumber")),
                            "created_on": j.get("createdOn"),
                            "note": "job later linked to this call via leadCallId"}

        # Tier 2: customer on the call -> any job created after the call inside window.
        if call.customer_id:
            r = self.client.get("/jpm/v2/tenant/{tenant}/jobs",
                                 params={"customerId": int(call.customer_id), "pageSize": 50,
                                         "createdOnOrAfter":
                                         call.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
            if r.status_code == 200:
                for j in r.json().get("data", []):
                    created = _parse_dt(j.get("createdOn"))
                    if call.received_at < created <= window_end:
                        return {"tier": "customer_job", "job_number": str(j.get("jobNumber")),
                                "created_on": j.get("createdOn"),
                                "confidence": "confirmed",
                                "note": f"customer booked a job within {window_hours}h of the call"}

        # Tier 2b: no customer on the call -> resolve caller phone to a ST customer, then
        # check that customer for a job created within the window. Upgrades what used to be
        # a weak "outbound callback" guess into a CONFIRMED recovery with a real job number.
        if not call.customer_id and call.from_number:
            cust_id = self._customer_id_by_phone(call.from_number)
            if cust_id:
                r = self.client.get("/jpm/v2/tenant/{tenant}/jobs",
                                     params={"customerId": int(cust_id), "pageSize": 50,
                                             "createdOnOrAfter":
                                             call.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
                if r.status_code == 200:
                    for j in r.json().get("data", []):
                        created = _parse_dt(j.get("createdOn"))
                        if call.received_at < created <= window_end:
                            return {"tier": "phone_customer_job", "job_number": str(j.get("jobNumber")),
                                    "created_on": j.get("createdOn"),
                                    "confidence": "confirmed",
                                    "note": f"caller (matched by phone) booked a job within {window_hours}h"}

        # Tier 3: phone fallback — outbound callback to the same number after the call.
        # Weakest signal: proves staff called back, NOT that a job booked. Kept as a
        # separate low-confidence outcome so it never inflates the confirmed-recovery count.
        r = self.client.get("/telecom/v2/tenant/{tenant}/calls",
                             params={"pageSize": 200,
                                     "createdOnOrAfter":
                                     call.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                                     "createdBefore":
                                     window_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
        if r.status_code == 200:
            for row in r.json().get("data", []):
                lc = row.get("leadCall") or {}
                if (lc.get("direction") == "Outbound"
                        and _digits(lc.get("to")) == call.from_number):
                    return {"tier": "outbound_callback",
                            "confidence": "unverified",
                            "note": "staff placed an outbound callback to this number "
                                    "(no linked job found; verify manually)",
                            "low_confidence": True}
        return None

    def _customer_id_by_phone(self, phone_digits: str) -> str | None:
        """Resolve a 10-digit phone to a ServiceTitan customer id, or None."""
        if not phone_digits:
            return None
        r = self.client.get("/crm/v2/tenant/{tenant}/customers",
                            params={"phone": phone_digits, "pageSize": 1})
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                return str(data[0].get("id"))
        return None
