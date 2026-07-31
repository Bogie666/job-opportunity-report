"""8x8 call source — STUB.

When LEX transitions to 8x8, implement list_calls + get_recording against the
8x8 Call Recording + CDR/Analytics APIs. find_recovery still delegates to
ServiceTitan job data (booking outcome never lives in the phone system).

Wire when creds/endpoints arrive:
- 8x8 XCaaS / Analytics for Contact Center exposes call detail records (CDR).
- 8x8 Call Recording API serves the audio.
Populate the same normalized `Call` objects and the rest of the pipeline is unchanged.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from ..sources.base import Call


class EightByEightCallSource:
    def __init__(self, *args, **kwargs):
        self._st_recovery = kwargs.get("st_recovery")  # inject a ST source for find_recovery

    def list_calls(self, day_start_utc: datetime, day_end_utc: datetime) -> list[Call]:
        raise NotImplementedError("8x8 adapter not wired yet — provide creds/endpoints.")

    def get_recording(self, call_id: str) -> bytes | None:
        raise NotImplementedError("8x8 adapter not wired yet — provide creds/endpoints.")

    def find_recovery(self, call: Call, window_hours: int) -> dict[str, Any] | None:
        if self._st_recovery is not None:
            return self._st_recovery.find_recovery(call, window_hours)
        raise NotImplementedError("attach a ServiceTitan source for outcome reconciliation.")
