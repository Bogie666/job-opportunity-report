from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from score_and_filter_hvac_briefs import score_bundle


def _bundle(job_type: str, summary: str, estimates=None):
    return {
        "meta": {"customer": "Score Test", "job_type": job_type, "business_unit": "HVAC"},
        "dossier": {
            "job": {"id": 10, "jobNumber": "10", "summary": summary},
            "customer": {"name": "Score Test"},
            "memberships": [],
            "estimates": estimates or [],
            "installed_equipment": [],
        },
    }


def test_scoring_excludes_warranty_recall_recent_install():
    rec = score_bundle(_bundle("Demand - HVAC - Warranty", "Recent install not cooling"), False)
    assert rec["excluded"] is True
    assert rec["excluded_reason"] == "warranty/recall/QC/callback/recent-install"


def test_open_estimates_are_tie_breaker_not_primary_score():
    estimates = [{"id": 1, "status": {"name": "Open"}, "subtotal": 60000, "name": "IAQ options"}]
    rec = score_bundle(_bundle("HVAC Maint - 1 System", "Seasonal maintenance. No concerns noted.", estimates), False)
    assert rec["score"] == 0
    assert "no score" in rec["drivers"]


def test_demand_problem_call_scores_above_similar_maintenance():
    demand = score_bundle(_bundle("Demand - HVAC", "No cool, blowing warm air"), False)
    maint = score_bundle(_bundle("HVAC Maint - 1 System", "Seasonal maintenance. No concerns noted."), False)
    assert demand["call_intent"] == "demand"
    assert demand["score"] > maint["score"]
