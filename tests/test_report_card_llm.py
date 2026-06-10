"""Offline tests for the LLM synthesis layer: parsing, the grounded-facts
validator, scrubbing, and the deterministic fallback contract. No network."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import report_card_llm as rcl
from customer_opportunity_report_card import _derive, build_report_card


def _bundle():
    return {
        "meta": {"customer": "Cody Hodges", "job_type": "Demand - HVAC - Member", "business_unit": "HVAC"},
        "dossier": {
            "job": {
                "id": 424675208,
                "jobNumber": "424675208",
                "summary": "Customer called to get pricing for recommended HVAC repairs from invoice 422437465. Kinks in ductwork, biological growth, condenser fan motor over-amping. Age of the unit? 12 yrs",
            },
            "customer": {"name": "Cody Hodges"},
            "memberships": [{"status": "Active", "billingFrequency": "Monthly", "from": "2025-02-12"}],
            "estimates": [
                {"id": 555001, "status": {"name": "Sold"}, "subtotal": 1292, "name": "HVAC repair", "soldOn": "2025-05-01"},
                {"id": 555002, "status": {"name": "Open"}, "subtotal": 4800, "name": "IAQ package", "createdOn": "2025-01-15"},
            ],
            "installed_equipment": [],
        },
    }


def _good_sections():
    return {
        "call_reason": "Pricing for previously recommended HVAC repairs from invoice 422437465",
        "customer_profile_extra": [],
        "buying_behavior": ["Sold history shows 1 estimate at $1,292; treat IAQ quote of $4,800 as quoted-never-closed."],
        "visual_inspection": {"positives": [], "verify_first": ["Capture current equipment photos on arrival."]},
        "primary_opportunities": [
            {"title": "Repair pricing confirmation visit", "priority": "HIGH",
             "bullets": ["Reconfirm the duct kinks and fan motor amp draw before presenting pricing."]},
        ],
        "coaching_notes": ["Weight booking-note repairs over the stale open IAQ estimate."],
        "overall": {"relationship": "A", "likelihood": "Medium-High", "risk": "Moderate",
                    "secondary_opportunity": "IAQ re-open only if findings support it",
                    "rationale": "Active monthly membership and a sold repair history support the grade"},
    }


def _ctx_blob(d):
    return rcl.serialize_context_for_llm(rcl.build_report_card_context(d), max_chars=16000)


def test_parse_sections_strips_fences_and_normalizes():
    text = "```json\n" + json.dumps(_good_sections()) + "\n```"
    sections = rcl.parse_sections(text)
    assert sections is not None
    assert sections["primary_opportunities"][0]["priority"] == "HIGH"
    assert sections["source"] == "llm"


def test_normalize_rejects_missing_required_slots():
    bad = _good_sections()
    bad["primary_opportunities"] = []
    assert rcl.normalize_sections(bad) is None
    bad2 = _good_sections()
    bad2["buying_behavior"] = []
    assert rcl.normalize_sections(bad2) is None


def test_validator_passes_grounded_sections():
    d = _derive(_bundle(), None, None)
    blob = _ctx_blob(d)
    sections = rcl.normalize_sections(_good_sections())
    assert rcl.validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob) == []


def test_validator_rejects_invented_dollar_year_and_id():
    d = _derive(_bundle(), None, None)
    blob = _ctx_blob(d)
    sections = rcl.normalize_sections(_good_sections())
    sections["buying_behavior"].append("Customer spent $99,123 on a system in 2011 (invoice 999999321).")
    violations = rcl.validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob)
    messages = " ".join(v["message"] for v in violations)
    assert "$99,123" in messages
    assert "2011" in messages
    assert "999999321" in messages


def test_validator_rejects_equipment_hallucination():
    d = _derive(_bundle(), None, None)
    blob = _ctx_blob(d)
    sections = rcl.normalize_sections(_good_sections())
    sections["primary_opportunities"][0]["bullets"].append("Quote a tankless water heater replacement while on site.")
    violations = rcl.validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob)
    assert any("water heater" in v["message"] for v in violations)


def test_validator_rejects_no_history_claim_when_history_exists():
    d = _derive(_bundle(), None, None)
    blob = _ctx_blob(d)
    sections = rcl.normalize_sections(_good_sections())
    sections["coaching_notes"].append("New customer with no estimate history, build credibility first.")
    violations = rcl.validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob)
    assert any("estimate history" in v["message"] for v in violations)


def test_scrub_removes_offending_bullets_but_keeps_card():
    d = _derive(_bundle(), None, None)
    blob = _ctx_blob(d)
    sections = rcl.normalize_sections(_good_sections())
    sections["buying_behavior"].append("Customer spent $99,123 in 2011.")
    violations = rcl.validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob)
    scrubbed = rcl.scrub_sections(sections, violations)
    assert scrubbed is not None
    assert all("$99,123" not in b for b in scrubbed["buying_behavior"])
    assert rcl.validate_sections(scrubbed, d["dossier"], d["facts"], d["intel"], blob) == []


def test_scrub_returns_none_when_required_slots_collapse():
    d = _derive(_bundle(), None, None)
    blob = _ctx_blob(d)
    sections = rcl.normalize_sections(_good_sections())
    sections["primary_opportunities"][0]["title"] = "Sell the $77,000 mega system"
    violations = rcl.validate_sections(sections, d["dossier"], d["facts"], d["intel"], blob)
    assert rcl.scrub_sections(sections, violations) is None


def test_generate_sections_with_stub_llm_and_correction_retry():
    d = _derive(_bundle(), None, None)
    calls = []

    def fake_llm(sys_msg, blob):
        calls.append(sys_msg)
        bad = _good_sections()
        if len(calls) == 1:
            bad["buying_behavior"] = ["Customer spent $99,123 on HVAC in 2011."]
            return json.dumps(bad)
        return json.dumps(_good_sections())

    sections = rcl.generate_sections(d, llm_call=fake_llm)
    assert sections is not None
    assert len(calls) == 2
    assert "IMPORTANT CORRECTIONS" in calls[1]
    assert all("$99,123" not in b for b in sections["buying_behavior"])


def test_generate_sections_returns_none_on_garbage():
    d = _derive(_bundle(), None, None)
    assert rcl.generate_sections(d, llm_call=lambda s, b: "not json at all") is None


def test_build_report_card_renders_llm_sections(monkeypatch):
    import customer_opportunity_report_card as corc
    monkeypatch.setattr(corc.report_card_llm, "generate_sections",
                        lambda d, llm_call=None: rcl.normalize_sections(_good_sections()))
    md = build_report_card(_bundle(), use_llm=True)
    assert "Repair pricing confirmation visit" in md
    assert "**Customer Relationship:** A" in md
    # Deterministic parts still rendered by Python regardless of the LLM:
    assert "# CUSTOMER OPPORTUNITY REPORT CARD" in md
    assert "FLAG" in md  # booking-note 12 yrs → HVAC replacement flag survives the LLM path


def test_build_report_card_falls_back_when_llm_unusable(monkeypatch):
    import customer_opportunity_report_card as corc
    monkeypatch.setattr(corc.report_card_llm, "generate_sections", lambda d, llm_call=None: None)
    md = build_report_card(_bundle(), use_llm=True)
    assert "# CUSTOMER OPPORTUNITY REPORT CARD" in md
    assert "Repair estimate follow-up" in md  # deterministic fallback primary
