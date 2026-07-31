"""Normalized call record + CallSource interface.

The rest of the pipeline (transcribe -> classify -> reconcile -> brief) only ever
sees a `Call`. Swapping ServiceTitan for 8x8 is a new adapter, nothing downstream
changes. Booking OUTCOME always comes from ServiceTitan regardless of source
(see reconcile.py) because the phone system never knows if a job booked.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class Call:
    id: str                         # source call id (string for portability)
    received_at: datetime           # tz-aware UTC
    direction: str                  # "Inbound" | "Outbound"
    from_number: str                # raw digits
    to_number: str
    duration_sec: int
    call_type: str | None           # ST: Booked | Unbooked | Excused | NotLead | Abandoned
    campaign_name: str | None
    campaign_category: str | None   # ST call category, e.g. "Existing Customer"
    agent_name: str | None
    customer_id: str | None         # ST customer id if attached
    customer_name: str | None
    job_number: str | None          # populated if the call row already had a job
    recording_available: bool
    raw: dict[str, Any] = field(default_factory=dict)  # original payload for debugging

    # populated later in the pipeline
    transcript: str | None = None
    reason_bucket: str | None = None       # A/B/C/D/E code
    reason_detail: str | None = None
    outcome: str | None = None             # booked_on_call | recovered | still_unbooked
    outcome_evidence: str | None = None
    recovered_job_number: str | None = None


class CallSource(Protocol):
    """Any phone/telephony backend implements these three."""

    def list_calls(self, day_start_utc: datetime, day_end_utc: datetime) -> list[Call]:
        """All calls whose received_at falls in [start, end)."""
        ...

    def get_recording(self, call_id: str) -> bytes | None:
        """Raw audio bytes (mp3), or None if unavailable."""
        ...

    def find_recovery(self, call: Call, window_hours: int) -> dict[str, Any] | None:
        """Look forward from call for a booked job / outbound callback that
        indicates the lead was actually recovered. Returns evidence dict or None.
        Always backed by ServiceTitan job data even for non-ST call sources."""
        ...
