from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_snapshot import build_snapshot


def test_opportunity_snapshot_is_short_and_manager_facing():
    bundle = {
        "meta": {"customer": "Test Customer", "job_type": "Demand - HVAC - Member", "business_unit": "HVAC"},
        "dossier": {
            "job": {
                "id": 425222384,
                "jobNumber": "425222384",
                "summary": "Demand Call - Member. Wants Pierre and has questions about estimates from March. Issue Going on? upstairs unit not cooling blowing warm air customer shut the system off. Age of the unit? 12-13 yrs old. How many units? 2",
            },
            "customer": {"name": "Test Customer"},
            "location": {"address": {"street": "1 Main", "city": "Dallas", "state": "TX", "zip": "75225"}, "customFields": [{"name": "Age of Home", "value": "2013"}]},
            "memberships": [{"status": "Active", "billingFrequency": "Monthly", "from": "2025-04-07"}],
            "estimates": [
                {"id": 1, "status": {"name": "Sold"}, "subtotal": 4855, "name": "Prior condenser", "soldOn": "2023-07-12"},
                {"id": 2, "status": {"name": "Open"}, "subtotal": 14417, "name": "Prior HVAC option", "createdOn": "2026-03-23"},
            ],
            "installed_equipment": [
                {"name": "Upstairs Condenser", "type": "A/C Condenser", "manufacturer": "Carrier", "installedOn": "2014-01-01T00:00:00Z", "active": True},
                {"name": "Upstairs Coil", "type": "Evaporator Coil", "manufacturer": "Carrier", "installedOn": "2014-01-01T00:00:00Z", "active": True},
            ],
        },
    }
    md, snap = build_snapshot(bundle, use_llm=False)
    assert "# SERVICE MANAGER OPPORTUNITY SNAPSHOT" in md
    assert "CUSTOMER PROFILE" not in md
    assert "PRIMARY OPPORTUNITIES" not in md
    assert len(md.splitlines()) <= 18
    assert snap["grade"] in {"A+", "A"}
    assert snap["staffing"] == "Strong sales technician recommended"
    assert any("Multiple intent signals" in s for s in snap["signals"])
    assert "single hard trigger" in snap["manager_note"]


def test_opportunity_snapshot_low_signal_stays_conservative():
    bundle = {
        "meta": {"customer": "Test Customer", "job_type": "Standard Tune Up", "business_unit": "HVAC"},
        "dossier": {
            "job": {"id": 2, "jobNumber": "2", "summary": "Seasonal tune up. No concerns noted."},
            "customer": {"name": "Test Customer"},
            "memberships": [],
            "estimates": [],
            "installed_equipment": [],
        },
    }
    md, snap = build_snapshot(bundle, use_llm=False)
    assert snap["grade"] in {"C", "D"}
    assert snap["staffing"] in {"Standard dispatch", "Low opportunity signal"}
    assert "Strong sales technician recommended" not in md
