from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_flags import (
    equipment_age_flag,
    equipment_flag_score,
    home_age_flags,
    home_age_score,
)


def test_hvac_condenser_flags_at_10_urgent_at_15():
    assert equipment_age_flag("A/C Condenser", 9) is None
    flag = equipment_age_flag("A/C Condenser", 10)
    assert flag and flag["severity"] == "flag" and flag["threshold"] == 10
    urgent = equipment_age_flag("A/C Condenser", 15)
    assert urgent and urgent["severity"] == "urgent" and urgent["threshold"] == 15
    assert "service life" in urgent["text"]


def test_furnace_uses_longer_thresholds():
    assert equipment_age_flag("Furnace", 12) is None
    flag = equipment_age_flag("Furnace", 16)
    assert flag and flag["severity"] == "flag" and flag["threshold"] == 15
    assert equipment_age_flag("Furnace", 21)["severity"] == "urgent"


def test_tank_water_heater_flags_at_8_urgent_at_12():
    assert equipment_age_flag("Water Heater", 7) is None
    assert equipment_age_flag("Water Heater", 8)["severity"] == "flag"
    assert equipment_age_flag("Water Heater", 12)["severity"] == "urgent"


def test_tankless_outranks_generic_water_heater_pattern():
    # "Tankless water heater" must hit the tankless tier (15), not the tank tier (8).
    assert equipment_age_flag("Tankless Water Heater", 10) is None
    assert equipment_age_flag("Tankless Water Heater", 16)["id"] == "tankless_wh"


def test_display_label_and_source_render_into_text():
    flag = equipment_age_flag("Furnace", 16, source="installed 2010", display_label="Furnace (Carrier)")
    assert "Furnace (Carrier)" in flag["text"]
    assert "installed 2010" in flag["text"]


def test_unclassified_equipment_never_flags():
    assert equipment_age_flag("Thermostat", 25) is None


def test_home_age_tiers():
    now = 2026
    assert home_age_flags(now - 5, now) == []
    assert home_age_flags(now - 12, now)[0]["id"] == "home_first_failure"
    assert home_age_flags(now - 20, now)[0]["id"] == "home_first_cycle"
    assert home_age_flags(now - 40, now)[0]["id"] == "home_second_cycle"
    assert home_age_flags(now - 55, now)[0]["id"] == "home_legacy"
    assert home_age_flags(None, now) == []
    assert home_age_flags(now + 2, now) == []  # bad data


def test_scoring_helpers():
    flags = [equipment_age_flag("A/C Condenser", 12), equipment_age_flag("Water Heater", 13)]
    assert equipment_flag_score(flags) == 35  # urgent water heater dominates
    assert equipment_flag_score([equipment_age_flag("A/C Condenser", 11)]) == 25
    assert home_age_score(home_age_flags(2026 - 50, 2026)) == 15
    assert home_age_score(home_age_flags(2026 - 35, 2026)) == 10
    assert home_age_score([]) == 0
