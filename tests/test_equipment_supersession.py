"""Supersession classifier tests: stale replaced-equipment records must stop firing
false aged-equipment flags, while genuine remaining older systems keep theirs."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from opportunity_flags import classified_equipment_score, classify_aged_equipment
from customer_opportunity_report_card import build_report_card


def _dossier(equipment, summary="Maintenance visit", estimates=None, n_systems=None):
    location = {"address": {"street": "1 Elm", "city": "Dallas", "state": "TX", "zip": "75001"}}
    if n_systems is not None:
        location["customFields"] = [{"name": "# of Systems", "value": str(n_systems)}]
    return {
        "job": {"id": 9, "jobNumber": "9", "summary": summary},
        "customer": {"name": "Test"},
        "location": location,
        "memberships": [],
        "estimates": estimates or [],
        "installed_equipment": equipment,
    }


def _unit(dossier, name):
    cls = classify_aged_equipment(dossier)
    return next(u for u in cls["units"] if u["name"] == name)


def _cond(name, year, model="", mfg="Goodman"):
    return {"name": name, "type": "A/C Condenser", "manufacturer": mfg, "model": model,
            "installedOn": f"{year}-05-01T00:00:00Z" if year else None, "active": True}


def test_same_zone_recent_install_supersedes_old_unit():
    d = _dossier([_cond("Upstairs Condenser", 2012), _cond("Upstairs Condenser", 2024, mfg="Trane")])
    old = _unit(d, "Upstairs Condenser")
    assert old["supersession"] == "superseded"
    assert "same-zone" in old["supersession_reason"]
    assert "2024" in (old["superseded_by"] or "")


def test_old_unit_in_unreplaced_zone_remains():
    d = _dossier([_cond("Upstairs Condenser", 2012), _cond("Downstairs Condenser", 2024)])
    assert _unit(d, "Upstairs Condenser")["supersession"] == "remaining"
    assert "other zones" in _unit(d, "Upstairs Condenser")["supersession_reason"]


def test_system_count_accounts_for_all_systems_demotes_leftovers():
    # 1 system on file, recent install present → the dateless/old extra is stale.
    d = _dossier([_cond("Condenser", 2012), _cond("New Condenser", 2024)], n_systems=1)
    assert _unit(d, "Condenser")["supersession"] == "superseded"
    assert "accounted for" in _unit(d, "Condenser")["supersession_reason"]


def test_system_count_leaves_room_keeps_remaining():
    # 3 systems, 2 recent installs → the old third system is genuinely remaining.
    d = _dossier([
        _cond("Condenser A", 2024), _cond("Condenser B", 2024), _cond("Condenser C", 2010),
    ], n_systems=3)
    assert _unit(d, "Condenser C")["supersession"] == "remaining"


def test_tonnage_match_with_sold_capital_estimate_supersedes():
    est = [{"id": 1, "status": {"name": "Sold"}, "subtotal": 9000,
            "name": "Complete System Replacement", "soldOn": "2024-06-15"}]
    d = _dossier([_cond("Condenser", 2010, model="GSX130361"),
                  _cond("New Condenser", 2024, model="GSX140361")], estimates=est)
    assert _unit(d, "Condenser")["supersession"] == "superseded"


def test_tonnage_match_without_confirmation_is_ambiguous():
    d = _dossier([_cond("Condenser", 2010, model="GSX130361"),
                  _cond("New Condenser", 2024, model="GSX140361")])
    assert _unit(d, "Condenser")["supersession"] == "ambiguous"


def test_tonnage_mismatch_leans_remaining():
    d = _dossier([_cond("Condenser", 2010, model="GSX130361"),
                  _cond("New Condenser", 2024, model="GSX140481")])
    assert _unit(d, "Condenser")["supersession"] == "remaining"


def test_old_furnace_not_demoted_by_new_condenser():
    # A replaced AC does not imply a replaced furnace — pairing is per component family.
    d = _dossier([
        {"name": "Furnace", "type": "Furnace", "manufacturer": "Carrier",
         "installedOn": "2008-01-01T00:00:00Z", "active": True},
        _cond("New Condenser", 2024),
    ])
    assert _unit(d, "Furnace")["supersession"] == "remaining"


def test_no_recent_installs_means_everything_remains():
    d = _dossier([_cond("Condenser", 2010)])
    assert _unit(d, "Condenser")["supersession"] == "remaining"


def test_undated_old_unit_zone_paired_is_superseded():
    undated = _cond("Upstairs Condenser", None)
    d = _dossier([undated, _cond("Upstairs Condenser", 2024, mfg="Trane")])
    cls = classify_aged_equipment(d)
    old = next(u for u in cls["units"] if u["year"] is None)
    assert old["supersession"] == "superseded"


def test_booking_note_pulls_one_demoted_unit_back_to_ambiguous():
    d = _dossier(
        [_cond("Upstairs Condenser", 2012), _cond("Upstairs Condenser", 2024, mfg="Trane")],
        summary="Customer says the other unit is 14 years old",
    )
    old = _unit(d, "Upstairs Condenser")
    assert old["supersession"] == "ambiguous"
    assert "booking notes" in old["supersession_reason"]


# --------------------------------------------------------------------------- integration

def test_report_card_demotes_superseded_unit_to_record_note():
    bundle = {
        "meta": {"customer": "Test", "job_type": "HVAC Maintenance", "business_unit": "HVAC"},
        "dossier": _dossier([_cond("Upstairs Condenser", 2012), _cond("Upstairs Condenser", 2024, mfg="Trane")]),
    }
    md = build_report_card(bundle, use_llm=False)
    assert "Stale record likely" in md
    assert "appears superseded" in md
    # The old unit must NOT fire a red replacement flag…
    assert "⚠ FLAG: A/C Condenser (Goodman)" not in md
    # …and must not become the remaining-older-system primary opportunity.
    assert "Remaining older" not in md


def test_report_card_keeps_full_flag_for_genuine_remaining_system():
    bundle = {
        "meta": {"customer": "Test", "job_type": "HVAC Maintenance", "business_unit": "HVAC"},
        "dossier": _dossier([_cond("Upstairs Condenser", 2012), _cond("Downstairs Condenser", 2024)]),
    }
    md = build_report_card(bundle, use_llm=False)
    assert "⚠ FLAG: A/C Condenser (Goodman) ~14 yrs" in md
    assert "Remaining older Goodman system" in md
    assert "Stale record likely" not in md


def test_report_card_renders_verify_flag_for_ambiguous_unit():
    bundle = {
        "meta": {"customer": "Test", "job_type": "HVAC Maintenance", "business_unit": "HVAC"},
        "dossier": _dossier([_cond("Condenser", 2010, model="GSX130361"),
                             _cond("New Condenser", 2024, model="GSX140361")]),
    }
    md = build_report_card(bundle, use_llm=False)
    assert "⚠ FLAG (verify):" in md
    assert "confirm which systems are still in service" in md


def test_scorer_ignores_superseded_and_reduces_ambiguous():
    from score_and_filter_hvac_briefs import score_bundle

    superseded = {"meta": {}, "dossier": _dossier(
        [_cond("Upstairs Condenser", 2012), _cond("Upstairs Condenser", 2024, mfg="Trane")])}
    rec = score_bundle(superseded, photos_have_sheet=False)
    assert "likely superseded, ignored" in rec["drivers"]
    assert rec["score"] == 0
    assert rec["max_equip_age"] == 2  # the recent unit, not the stale 14-yr record

    remaining = {"meta": {}, "dossier": _dossier(
        [_cond("Upstairs Condenser", 2012), _cond("Downstairs Condenser", 2024)])}
    assert score_bundle(remaining, photos_have_sheet=False)["score"] == 25

    ambiguous = {"meta": {}, "dossier": _dossier(
        [_cond("Condenser", 2010, model="GSX130361"), _cond("New Condenser", 2024, model="GSX140361")])}
    assert score_bundle(ambiguous, photos_have_sheet=False)["score"] == 10
