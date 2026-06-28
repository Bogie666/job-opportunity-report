from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_snapshot import build_snapshot, is_excluded_opportunity_call, valuable_photo_lines


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
    assert any("Current call intent" in s or "Replacement-age" in s for s in snap["signals"])
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


def test_open_estimates_are_weak_without_current_need():
    bundle = {
        "meta": {"customer": "Open Only", "job_type": "Standard Tune Up", "business_unit": "HVAC"},
        "dossier": {
            "job": {"id": 3, "jobNumber": "3", "summary": "Seasonal maintenance. No concerns noted."},
            "customer": {"name": "Open Only"},
            "memberships": [],
            "estimates": [
                {"id": 10, "status": {"name": "Open"}, "subtotal": 25000, "name": "IAQ option"},
                {"id": 11, "status": {"name": "Open"}, "subtotal": 18000, "name": "Good better best option"},
            ],
            "installed_equipment": [],
        },
    }
    _md, snap = build_snapshot(bundle, use_llm=False)
    assert snap["grade"] in {"C", "D"}
    assert snap["staffing"] != "Strong sales technician recommended"
    assert not any("interest" in s.lower() for s in snap["signals"])


def test_warranty_recall_calls_are_excluded():
    assert is_excluded_opportunity_call("Demand - HVAC - Warranty", "recent install not cooling")
    bundle = {
        "meta": {"customer": "Warranty Customer", "job_type": "Demand - HVAC - Warranty", "business_unit": "HVAC"},
        "dossier": {
            "job": {"id": 4, "jobNumber": "4", "summary": "Warranty call. Recent install not cooling."},
            "customer": {"name": "Warranty Customer"},
            "memberships": [{"status": "Active", "billingFrequency": "Monthly"}],
            "estimates": [{"id": 12, "status": {"name": "Sold"}, "subtotal": 20000, "name": "Recent install"}],
            "installed_equipment": [],
        },
    }
    md, snap = build_snapshot(bundle, use_llm=False)
    assert snap["source"] == "excluded"
    assert snap["grade"] == "D"
    assert "Excluded from opportunity triage" in md


def test_valuable_photo_findings_only_surface_specific_opportunities():
    vision = {
        "findings": [
            {"finding": "General photo of condenser present", "indexes": [1], "confidence": "high"},
            {"finding": "Possible low insulation visible around attic furnace", "indexes": [3], "confidence": "medium"},
            {"finding": "Kinked ductwork restricts airflow", "indexes": [5], "confidence": "high"},
            {"finding": "Dirty blower wheel with matted dust on fins", "indexes": [6], "confidence": "high"},
        ]
    }
    lines = valuable_photo_lines(vision)
    assert len(lines) == 3
    assert "image 3" in lines[0]
    assert "image 5" in lines[1]
    assert "blower wheel" in lines[2]


def test_valuable_photo_findings_include_manager_feedback_targets():
    vision = {
        "findings": [
            {"finding": "Very dirty undersized 1-inch filter with bypass dust", "indexes": [2], "confidence": "high"},
            {"finding": "Dust and biological growth-like spotting inside return plenum", "indexes": [4], "confidence": "medium"},
            {"finding": "Rusty evaporator coil casing and corrosion at coil end plate", "indexes": [7], "confidence": "high"},
            {"finding": "Rafter tops showing through thin attic insulation", "indexes": [9], "confidence": "medium"},
        ]
    }
    lines = valuable_photo_lines(vision, max_items=5)
    joined = "\n".join(lines).lower()
    assert "1-inch filter" in joined or "undersized" in joined
    assert "plenum" in joined and "biological" in joined
    assert "rusty evaporator coil" in joined or "corrosion" in joined
    assert "rafter" in joined and "insulation" in joined


def test_demand_outweighs_similar_maintenance():
    base_dossier = {
        "customer": {"name": "Intent Test"},
        "location": {"customFields": [{"name": "Age of Home", "value": "2014"}]},
        "memberships": [{"status": "Active", "billingFrequency": "Monthly"}],
        "estimates": [],
        "installed_equipment": [
            {"name": "Condenser", "type": "A/C Condenser", "manufacturer": "Carrier", "installedOn": "2016-01-01T00:00:00Z", "active": True}
        ],
    }
    demand = {"meta": {"customer": "Intent Test", "job_type": "Demand - HVAC - Member"}, "dossier": dict(base_dossier, job={"id": 5, "jobNumber": "5", "summary": "No cool. System blowing warm air."})}
    maint = {"meta": {"customer": "Intent Test", "job_type": "Maintenance - HVAC - Member"}, "dossier": dict(base_dossier, job={"id": 6, "jobNumber": "6", "summary": "Seasonal maintenance. No concerns noted."})}
    _md_d, snap_d = build_snapshot(demand, use_llm=False)
    _md_m, snap_m = build_snapshot(maint, use_llm=False)
    grades = {"D": 0, "C": 1, "B": 2, "A": 3, "A+": 4}
    assert grades[snap_d["grade"]] >= grades[snap_m["grade"]]
    assert any("Current call intent" in s or "Demand call" in s for s in snap_d["signals"])

