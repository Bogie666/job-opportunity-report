import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import rebuild_lean_briefs as r  # noqa: E402

FIXTURE_DIR = ROOT / "data" / "tech_briefs" / "20260529T001049Z"
KIM_3_SYSTEM_FIXTURE_DIR = ROOT / "data" / "tech_briefs" / "20260529T013704Z"


def load_dossier(filename):
    bundle = json.loads((FIXTURE_DIR / filename).read_text())
    return bundle["dossier"], bundle["meta"]


def load_kim_3_system_dossier():
    bundle = json.loads((KIM_3_SYSTEM_FIXTURE_DIR / "404387434_HVAC_Maint_-_3_System.json").read_text())
    return bundle["dossier"], bundle["meta"]


def test_arun_estimate_math_is_ceiling_aware_and_reconciled():
    dossier, _ = load_dossier("398189085_HVAC_Maint_-_1_System.json")
    intel = r.analyze_estimate_history(dossier["estimates"], dossier["job"]["id"], primary_trade="hvac")

    assert round(intel["price_ceiling"]) == 382
    assert "avg_sold_ticket" in intel["averages"]
    assert intel["opportunities_above_ceiling"]
    assert all(o["subtotal"] > 2 * intel["price_ceiling"] for o in intel["opportunities_above_ceiling"])

    total = intel["total_estimates"]
    totals = intel["totals"]
    assert total == totals["sold_count"] + totals["open_count"] + totals["dismissed_count"] + totals["expired_count"]
    assert intel["suppressed_count"] <= total
    assert intel["coachable_count"] == total - intel["suppressed_count"]
    assert intel["coachable_count"] >= intel["coached_reference_count"]
    assert intel["suppressed_count"] <= total - intel["coached_reference_count"]

    rendered = r.render_lean_markdown(
        dossier,
        "",
        {**r.build_facts_block(dossier), "_intel": intel, "trade": "hvac", "cross_signals": []},
        {"job_types": {dossier["job"]["jobTypeId"]: "HVAC Maint - 1 System"}, "business_units": {dossier["job"]["businessUnitId"]: "LEX Maintenance "}},
    )
    assert "# LEX / Lyons Tech Brief" in rendered
    assert "**SCORE:**" in rendered
    assert "**Open quoted, never closed — reopen targets**" in rendered
    assert "- **$3,079:** Platinum upgrade" in rendered
    assert "- **Buying ceiling:** $382 largest prior sold → Phase it" in rendered
    # Category rollups shown alongside standalone largest-stale must not exceed open dollars.
    shown_categories = sum(x["dollars"] for x in intel["open_stale_high_value"])
    largest = (intel["biggest_open_stale"] or {}).get("subtotal", 0)
    assert shown_categories + largest <= totals["open_dollars"]


def test_kimberly_equipment_and_trade_routing_regressions():
    dossier, _ = load_dossier("412566474_ESI_-_Electrical_Safety_Inspection.json")
    facts = r.build_facts_block(dossier)
    intel = r.analyze_estimate_history(dossier["estimates"], dossier["job"]["id"], primary_trade="electrical")

    assert round(intel["price_ceiling"]) == 3983
    assert facts["equipment_counts"] == ["3 × AC + Furnace"]

    implied = r.equipment_implied_by_history(dossier, r._equipment_whitelist(dossier))
    assert any(x["label"] == "water heater" for x in implied)

    assert intel["largest_stale_any_trade"]["category"] == "water heater"
    # Electrical has no surviving primary-trade stale anchor after suppression; the key point is
    # that the off-trade water heater does not become the primary-trade sales-coach anchor.
    assert intel["largest_stale_primary_trade"] is None or intel["largest_stale_primary_trade"]["category"] != "water heater"

    bad = "Re-quote the tankless water heater."
    violations = r.validate_narrative(bad, dossier, facts | {"equipment_likely_present_not_on_file": implied}, "2026-05-29", intel)
    assert violations, "Likely-but-not-on-file equipment must still require verify language"

    ok = "Cross-trade leads:\n- Tankless water heater is not on file, verify on arrival before re-quoting Est #399677547."
    assert not r.validate_narrative(ok, dossier, facts | {"equipment_likely_present_not_on_file": implied}, "2026-05-29", intel)

    contradictory = "Cross-trade leads:\n- Tankless water heater is not on file, verify on arrival before re-quoting Est #399677547.\n- None obvious — check during walkthrough."
    assert any("None obvious" in v for v in r.validate_narrative(
        contradictory,
        dossier,
        facts | {"equipment_likely_present_not_on_file": implied, "cross_signals": ["IAQ cross-trade"]},
        "2026-05-29",
        intel,
    ))
    cleaned = r._strip_cross_trade_none_obvious_when_real_leads(contradictory)
    assert "Tankless water heater" in cleaned
    assert "None obvious" not in cleaned

    empty = "Cross-trade leads: None obvious — check during walkthrough."
    assert r._strip_cross_trade_none_obvious_when_real_leads(empty) == empty

    rendered = r.render_lean_markdown(
        dossier,
        "",
        {**facts, "_intel": intel, "trade": "electrical", "equipment_likely_present_not_on_file": implied},
        {"job_types": {dossier["job"]["jobTypeId"]: "ESI - Electrical Safety Inspection"}, "business_units": {dossier["job"]["businessUnitId"]: "Electrical Maintenance"}},
    )
    assert "Stale opportunity: $11,321 of 'water heater'" not in rendered
    assert "**Largest stale opportunity**" in rendered
    assert "- **Amount:** $19,350" in rendered
    assert "- **Target:** Supreme" in rendered
    assert "water heater" in rendered
    assert "- **Buying ceiling:** $3,983 largest prior sold → In range" in rendered


def test_kim_three_system_component_and_iaq_attribution_regression():
    dossier, _ = load_kim_3_system_dossier()
    assert dossier["job"]["id"] == 404387434

    facts = r.build_facts_block(dossier)
    assert facts["equipment_counts"] == ["3 × AC + Furnace"]
    component_line = ", ".join(facts["equipment_components"])
    assert "3 × Furnace" in component_line
    assert "3 × Coil" in component_line
    assert "3 × A/C Condenser" in component_line
    assert "× Equipment" not in component_line

    intel = r.analyze_estimate_history(dossier["estimates"], dossier["job"]["id"], primary_trade="hvac")
    assert intel["totals"]["open_count"] == 15
    assert intel["totals"]["open_dollars"] == 71136
    assert intel["largest_stale_primary_trade"]["id"] == 397747622
    assert intel["largest_stale_primary_trade"]["subtotal"] == 10800

    iaq = next(x for x in intel["open_stale_high_value"] if x["category"] == "IAQ / air quality")
    assert iaq["dollars"] == 27864
    assert 397747622 not in iaq["estimate_ids"]
    assert intel["display_attribution_violations"] == []

    rendered = r.render_lean_markdown(
        dossier,
        "",
        {**facts, "_intel": intel, "trade": "hvac", "cross_signals": []},
        {"job_types": {dossier["job"]["jobTypeId"]: "HVAC Maint - 3 System"}, "business_units": {dossier["job"]["businessUnitId"]: "LEX Maintenance "}},
    )
    assert "3 × Furnace" in rendered
    assert "3 × Coil" in rendered
    assert "3 × A/C Condenser" in rendered
    assert "× Equipment" not in rendered
    assert "**Open quoted, never closed — reopen targets**" in rendered
    assert "- **$10,800:** Platinum" in rendered
    assert "**Largest stale opportunity**" in rendered
    assert "- **Amount:** $10,800" in rendered
    assert "- **Target:** Platinum" in rendered
    assert "- **Buying ceiling:** $2,045 largest prior sold → Phase it" in rendered


def test_sales_prompt_and_validator_block_procedure_only_and_open_interest_overclaim():
    dossier, _ = load_kim_3_system_dossier()
    facts = r.build_facts_block(dossier)
    intel = r.analyze_estimate_history(dossier["estimates"], dossier["job"]["id"], primary_trade="hvac")
    bad = """Kim has shown interest in air quality because IAQ estimates are open.

Go win the call:
- Wash condensers, flush drains, measure delta-T, superheat/subcool, and test float switches.
- Check static on the master return; if high, open duct sealing tied to open IAQ Est #397747622.

Sales coach:
- Reliable buyer: accepts water heater category.

HVAC opportunities:
- Inspect drain pans and coils.

Cross-trade leads:
- None obvious — check during walkthrough."""
    violations = r.validate_narrative(bad, dossier, {**facts, "cross_signals": []}, "2026-05-29", intel)
    assert any("procedure-only" in v for v in violations)
    assert any("OPEN/EXPIRED estimates" in v for v in violations)
    assert any("Reliable buyer" in v for v in violations)


def test_kim_three_system_context_has_scoped_history_and_non_interest_states():
    dossier, _ = load_kim_3_system_dossier()
    facts = r.build_facts_block(dossier)
    intel = r.analyze_estimate_history(dossier["estimates"], dossier["job"]["id"], primary_trade="hvac")
    context = r.build_llm_context(facts, dossier, "hvac", [], intel)

    assert context["sales_enablement_principle"].startswith("This brief is a sales-enablement tool")
    assert any("upstairs" in x.lower() and "coil" in x.lower() for x in context["system_scoped_history"])
    primary = context["largest_stale_PRIMARY_TRADE"]
    assert primary["id"] == 397747622
    assert primary["intent_state"] == "OPEN"
    assert primary["system_scope"] == "system not specified, verify"
    assert any("OPEN = we quoted" in x for x in context["estimate_intent_state_rules"])

    opener = facts["history_opener"]
    assert opener.startswith("Opener:")
    assert "replaced" in opener.lower() or "quoted" in opener.lower() or "sold" in opener.lower()


def test_structured_context_trimming_never_blind_cuts_json():
    dossier, _ = load_dossier("412566474_ESI_-_Electrical_Safety_Inspection.json")
    facts = r.build_facts_block(dossier)
    intel = r.analyze_estimate_history(dossier["estimates"], dossier["job"]["id"], primary_trade="electrical")
    context = r.build_llm_context(facts, dossier, "electrical", ["x"], intel)
    payload = r.serialize_context_for_llm(context, max_chars=1200)
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    assert "estimates omitted for length" in payload
