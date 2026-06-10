"""Tests for the brand-aware HVAC serial-number → year decoder.

Sample serials below are taken from real LEX ServiceTitan equipment records
where the install date is on file, so we can validate decoder output against
ground truth. Every decoder must hit ±1 year of the install date.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serial_decoder import decode_serial, unsupported_brand


def _year(serial, mfg, expected):
    res = decode_serial(mfg, serial)
    assert res is not None, f"Expected decode for {mfg} {serial}"
    y, conf, label = res
    assert abs(y - expected) <= 1, f"{mfg} {serial} decoded {y}, expected {expected}"


# ---- Carrier family ----

def test_carrier_modern_wwyy():
    _year("2614E22837", "Carrier", 2014)
    _year("4513X42108", "Carrier", 2013)
    _year("3917A21585", "Carrier", 2017)


def test_carrier_oem_family_recognized():
    # Bryant, Payne, ICP, Heil, Tempstar all share Carrier serial format
    _year("2514X35412", "Bryant", 2014)


# ---- Goodman family (Goodman / Amana / Daikin USA) ----

def test_goodman_yymm():
    _year("2206227441", "Daikin", 2022)
    _year("2110729240", "Daikin", 2021)
    _year("2011740339", "Amana", 2020)
    _year("2012199065", "Amana", 2020)


# ---- Trane / American Standard ----

def test_trane_yyww():
    _year("23173KCKHG", "Trane", 2023)   # year 23 week 17
    _year("22421SXUGG", "Trane", 2022)   # year 22 week 42
    _year("231615TXKF", "Trane", 2023)   # year 23 week 16
    _year("22381NFE5F", "Trane", 2022)   # year 22 week 38


def test_american_standard_same_format():
    _year("214930H63F", "American Standard", 2021)   # year 21 week 49


# ---- Lennox / Armstrong ----

def test_lennox_modern_wwyy():
    _year("1722128226", "Lennox", 2022)   # week 17 year 22


def test_armstrong_plant_prefix_yy():
    _year("5917K09492", "Armstrong", 2017)   # plant 59, year 17
    _year("5914F12177", "Armstrong", 2014)
    _year("7122B18747", "Lenox", 2022)   # Lenox typo also handled


# ---- Rheem / Ruud ----

def test_rheem_letter_wwyy():
    _year("W492022533", "Rheem", 2021)   # W, week 49, year 20 -> note real install was 2021 (mfg often year prior)
    _year("W512319852", "Rheem", 2024)   # W, week 51, year 23 (2023) -> install 2024
    _year("W342034329", "Rheem", 2020)


# ---- Unsupported brands ----

def test_unsupported_brands_return_none():
    assert decode_serial("York", "W1G3882134") is None
    assert decode_serial("Mitsubishi", "ABC123") is None
    assert unsupported_brand("York") is True
    assert unsupported_brand("Coleman") is True
    assert unsupported_brand("Mitsubishi") is True


# ---- Garbage input ----

def test_garbage_input_returns_none():
    assert decode_serial("", "") is None
    assert decode_serial("Carrier", "") is None
    assert decode_serial("Carrier", "N/A") is None
    assert decode_serial("Trane", "ABC") is None
