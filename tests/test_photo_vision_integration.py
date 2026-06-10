import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import integrate_photo_vision as pv  # noqa: E402


def test_recent_addressed_uses_specific_bucket_terms_and_not_broad_system_language():
    finding = {"bucket": "duct/IAQ", "confidence": "medium"}
    dossier = {
        "estimates": [
            {
                "name": "System diagnostic",
                "subtotal": 129,
                "status": {"name": "Sold"},
                "soldOn": "2026-06-01T10:00:00Z",
                "items": [{"sku": {"displayName": "System diagnostic only"}}],
            }
        ],
        "past_invoices": [],
    }

    addressed, evidence = pv.recent_addressed(finding, dossier)

    assert addressed is False
    assert evidence == "No matching recent invoice/estimate found in pulled data"


def test_recent_addressed_marks_open_estimate_as_gap_not_sold_addressed_signal():
    finding = {"bucket": "blower/plenum", "confidence": "medium"}
    dossier = {
        "estimates": [
            {
                "name": "Blower and plenum cleaning",
                "subtotal": 1742,
                "status": {"name": "Open"},
                "createdOn": "2026-05-30T10:00:00Z",
                "items": [{"sku": {"displayName": "Clean blower wheel and plenum"}}],
            }
        ],
        "past_invoices": [],
    }

    addressed, evidence = pv.recent_addressed(finding, dossier)

    assert addressed is False
    assert evidence.startswith("Related recent estimate exists but not confirmed sold")
    assert "$1,742" in evidence


def test_recent_addressed_marks_recent_sold_work_as_addressed_signal():
    finding = {"bucket": "drain/pan", "confidence": "high"}
    dossier = {
        "estimates": [
            {
                "name": "Condensate safety switch and drain correction",
                "subtotal": 650,
                "status": {"name": "Sold"},
                "soldOn": "2026-06-01T10:00:00Z",
                "items": [{"sku": {"displayName": "Drain pan float safety switch"}}],
            }
        ],
        "past_invoices": [],
    }

    addressed, evidence = pv.recent_addressed(finding, dossier)

    assert addressed is True
    assert evidence.startswith("sold $650")


def test_photo_section_is_inserted_after_history_and_updates_verify_today():
    md = """# LEX / Lyons Tech Brief · HVAC Maint

**SCORE:** 40 ⚪️ STANDARD Top signals: active member

## SYSTEM ON FILE
1 × AC + Furnace

## HISTORY / NOTES
- Past job note.

## 💰 ESTIMATE INTELLIGENCE
**Open quoted, never closed — reopen targets**
- None

## 🔍 VERIFY TODAY
**Trigger:** History only
**If still present → opportunity:** verify today

## 🎯 LIKELY TO BUY + WHAT TO SAY
**Best-fit offer:** Maintenance only
"""
    vision = {
        "photo_source_note": "Historical photos from prior location jobs; verify on arrival",
        "findings": [
            {
                "indexes": [2, 4],
                "finding": "low attic insulation visible behind furnace",
                "bucket": "insulation/attic",
                "confidence": "medium",
                "verify_wording": "Prior photos showed low attic insulation -> verify attic insulation today. If still present -> opportunity: insulation handoff.",
                "why_it_matters": "Low insulation can raise load and comfort complaints.",
            }
        ],
    }
    dossier = {"estimates": [], "past_invoices": []}

    inserted = pv.insert_after(md, "## HISTORY / NOTES", pv.section_text(vision, dossier))
    final = pv.replace_verify_top(inserted, vision)

    assert final.index("## HISTORY / NOTES") < final.index("## 📷 PHOTO / VISION OPPORTUNITIES") < final.index("## 💰 ESTIMATE INTELLIGENCE")
    assert "**Images:** #2, #4" in final
    assert "**Bucket / confidence:** insulation/attic; medium" in final
    assert "**Gap:** No matching recent invoice/estimate found in pulled data" in final
    assert "Prior photos showed low attic insulation visible behind furnace" in final
    assert "If still present → opportunity:** insulation handoff" in final


def test_integrate_main_writes_full_brief_with_photo_section(tmp_path):
    lean = tmp_path / "run_lean"
    raw = tmp_path / "run"
    vision_dir = tmp_path / "photos" / "vision"
    lean.mkdir()
    raw.mkdir()
    vision_dir.mkdir(parents=True)

    (lean / "123_HVAC.md").write_text("""# LEX / Lyons Tech Brief · HVAC

**SCORE:** 50 🟡 ELEVATED Top signals: open estimate

## HISTORY / NOTES
- Historical job note.

## 💰 ESTIMATE INTELLIGENCE
- Placeholder

## 🔍 VERIFY TODAY
**Trigger:** History
**If still present → opportunity:** verify
""")
    (raw / "123_HVAC.json").write_text(json.dumps({"dossier": {"estimates": [], "past_invoices": []}}))
    summary = vision_dir / "summary.json"
    summary.write_text(json.dumps([
        {
            "job_id": "123",
            "photo_source_note": "Historical photos from prior location jobs; verify on arrival",
            "findings": [
                {
                    "indexes": [1],
                    "finding": "dirty blower wheel",
                    "bucket": "blower/plenum",
                    "confidence": "high",
                    "verify_wording": "Prior photos showed dirty blower wheel -> verify blower today. If still present -> opportunity: blower cleaning.",
                    "why_it_matters": "Dirty blower reduces airflow and efficiency.",
                }
            ],
        }
    ]))

    pv.main(str(lean), str(summary), str(raw))

    out = tmp_path / "run_full" / "123_HVAC.md"
    assert out.exists()
    text = out.read_text()
    assert "## 📷 PHOTO / VISION OPPORTUNITIES" in text
    assert "**SCORE:** 55" in text
    assert "dirty blower wheel" in text
