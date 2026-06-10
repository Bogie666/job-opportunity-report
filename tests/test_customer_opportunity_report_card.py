from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customer_opportunity_report_card import build_report_card


def _load_pat_fixture():
    p = ROOT / "data" / "tech_briefs" / "20260609T145452Z_421545885" / "421545885_HVAC_Maint_-_3_System.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def test_pat_report_card_prioritizes_remaining_system_over_iaq():
    bundle = _load_pat_fixture()
    if bundle is None:
        return
    vision = {
        "photo_source_note": "Historical photos from prior location jobs; verify on arrival",
        "findings": [
            {"finding": "low insulation in attic", "confidence": "medium"},
            {"finding": "ductwork appears poorly sealed and potentially leaking", "confidence": "medium"},
        ],
    }
    md = build_report_card(bundle, vision=vision, lifetime_revenue=39755)
    assert "# CUSTOMER OPPORTUNITY REPORT CARD" in md
    assert "**Lifetime Revenue:** $39,755" in md
    assert "Remaining older" in md
    assert "Amana" in md
    assert "do not lead with IAQ" in md
    assert md.index("Remaining older") < md.index("Only revisit IAQ")
    assert "Photo set is historical" in md
    assert "Capture current electrical panel photos" in md or "capture current panel photos" in md


def test_report_card_marks_historical_photo_findings_verify_first():
    bundle = _load_pat_fixture()
    if bundle is None:
        return
    md = build_report_card(bundle, vision={
        "photo_source_note": "Historical photos from prior location jobs; verify on arrival",
        "findings": [{"finding": "rust visible on drain pan", "confidence": "medium"}],
    })
    assert "Verify photo finding before recommending: rust visible on drain pan" in md
    assert "not confident recommendations" in md


def test_equipment_age_summary_always_appears_in_customer_profile():
    bundle = {
        "meta": {"customer": "Cody Hodges", "job_type": "Demand - HVAC - Member", "business_unit": "HVAC"},
        "dossier": {
            "job": {
                "id": 424675208,
                "jobNumber": "424675208",
                "summary": "Customer called to get pricing for recommended HVAC repairs from invoice 422437465. Kinks in ductwork, biological growth, condenser fan motor over-amping. Age of the unit? 12 yrs",
            },
            "customer": {"name": "Cody Hodges"},
            "memberships": [{"status": "Active", "billingFrequency": "Monthly"}],
            "estimates": [{"status": {"name": "Sold"}, "subtotal": 1292, "name": "HVAC repair", "soldOn": "2025-05-01"}],
            "installed_equipment": [
                {"name": "Furnace", "type": None, "manufacturer": "Carrier", "model": "314aav066110", "serialNumber": "1414a17715", "installedOn": "2015-04-01T05:00:00Z", "active": True},
                {"name": "Evap coil", "type": "Evaporator Coil", "manufacturer": "Bryant", "serialNumber": "2514X35412", "installedOn": None, "active": True},
                {"name": "Condenser", "type": "A/C Condenser", "manufacturer": "Bryant", "serialNumber": "2814E19469", "installedOn": None, "active": True},
            ],
        },
    }
    md = build_report_card(bundle)
    assert "Equipment age on file" in md
    assert "Furnace" in md
    assert "Carrier" in md
    # Either decoded year or "install date missing, verify nameplate" must appear for Bryant components
    assert ("serial decode" in md) or ("install date missing" in md)
    # 10+ yr HVAC components must trigger replacement flag
    assert "FLAG" in md and "10-yr replacement threshold" in md


def test_water_heater_10yr_replacement_flag():
    bundle = {
        "meta": {"customer": "Test", "job_type": "Demand - Plumbing - Member", "business_unit": "Plumbing"},
        "dossier": {
            "job": {"id": 1, "jobNumber": "1", "summary": "Water heater leaking"},
            "customer": {"name": "Test"},
            "memberships": [{"status": "Active", "billingFrequency": "Monthly"}],
            "estimates": [],
            "installed_equipment": [
                {"name": "Water heater", "type": "Water Heater", "manufacturer": "Rheem", "installedOn": "2014-06-01T05:00:00Z", "active": True},
            ],
        },
    }
    md = build_report_card(bundle)
    assert "Water Heater" in md or "Water heater" in md
    assert "10-yr replacement threshold" in md


def test_hvac_repair_followup_outranks_generic_age_and_uses_photo_specifics():
    bundle = {
        "meta": {"customer": "Cody Hodges", "job_type": "Demand - HVAC - Member", "business_unit": "HVAC"},
        "dossier": {
            "job": {
                "id": 424675208,
                "jobNumber": "424675208",
                "summary": "Customer called to get pricing for recommended HVAC repairs from invoice 422437465. The technician identified kinks in multiple ductwork sections, biological growth suggesting an AUV system, and an outdoor condenser fan motor over-amping. Age of the unit? 12 yrs",
            },
            "customer": {"name": "Cody Hodges"},
            "memberships": [{"status": "Active", "billingFrequency": "Monthly", "from": "2025-02-12"}],
            "estimates": [{"status": {"name": "Sold"}, "subtotal": 1292, "name": "HVAC repair", "soldOn": "2025-05-01"}],
            "installed_equipment": [],
        },
    }
    vision = {
        "photo_source_note": "Historical photos from prior location jobs; verify on arrival",
        "findings": [
            {"indexes": [5, 11], "finding": "ceiling joists visible through insulation", "bucket": "insulation/attic", "confidence": "high"},
            {"indexes": [6, 10], "finding": "flex duct laying directly on insulation", "bucket": "duct support", "confidence": "high"},
        ],
    }
    md = build_report_card(bundle, vision=vision)
    assert "Repair estimate follow-up" in md
    assert "ductwork kinks / duct sealing repair" in md
    assert "biological growth / AUV-IAQ recommendation" in md
    assert "outdoor condenser fan motor over-amping" in md
    assert "ceiling joists visible through insulation" in md
    assert "flex duct laying directly on insulation" in md
    assert "Attic insulation and duct support verification" in md
