"""Tenant / brand registry.

v1 note: LEX, Lyons, and ETX all share ONE ServiceTitan tenant (1498628772),
so 'tenant' here is really a reporting configuration, not a separate ST account.
Built as a list so the real Champions rollout (separate tenants) is a config add,
not a rewrite. Each entry drives one daily brief.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Tenant:
    key: str                      # short id, e.g. "lex_portfolio"
    display_name: str             # brief title
    source: str                   # "servicetitan" | "eightbyeight"
    st_tenant_id: str             # ServiceTitan numeric tenant
    recipients: list[str]         # email recipients for the brief
    timezone: str = "America/Chicago"
    reconcile_window_hours: int = 72
    # brand_filter: None = ALL brands/campaigns (v1 decision). Later: list of
    # campaign-name substrings to restrict to (e.g. ["lex","lyons","etx"]).
    brand_filter: list[str] | None = None
    # ST call categories that are auto-classed bucket A (not-a-true-lead) and
    # skipped BEFORE transcription to save cost. Surfaced in the brief as noise.
    prefilter_categories: list[str] = field(default_factory=lambda: ["Existing Customer"])
    extra: dict[str, Any] = field(default_factory=dict)


TENANTS: list[Tenant] = [
    Tenant(
        key="lex_portfolio",
        display_name="LEX / Lyons / ETX — Unbooked Calls",
        source="servicetitan",
        st_tenant_id="1498628772",
        recipients=["ryan@lexairconditioning.com"],
        timezone="America/Chicago",
        reconcile_window_hours=72,
        brand_filter=None,          # v1: ALL brands in the tenant
    ),
]


def get_tenant(key: str) -> Tenant:
    for t in TENANTS:
        if t.key == key:
            return t
    raise KeyError(f"unknown tenant {key!r}; known: {[t.key for t in TENANTS]}")
