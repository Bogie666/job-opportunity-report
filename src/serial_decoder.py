"""Brand-aware HVAC serial-number → year-of-manufacture decoder.

Returns (year, confidence, source_label) or None.

Confidence levels:
  high   = brand has a well-known stable format and decoded cleanly
  medium = brand format known but multiple format generations exist
  low    = guess based on weak signal (e.g. unknown brand fallback)

Always reject implausible years (< 1990 or > current year + 1).
Always recommend "verify nameplate" — even high-confidence decoders are wrong
about ~5-10% of the time due to refurbished units, mid-format-change models,
or relabeled OEM equipment.

Coverage (Phase 1, 2026-06-09):
  Carrier family       (Carrier, Bryant, Payne, ICP, Heil, Tempstar,
                        Day & Night, Comfortmaker, Arcoaire, KeepRite)
  Goodman family       (Goodman, Amana, Daikin USA, Janitrol)
  Trane family         (Trane, American Standard)
  Lennox family        (Lennox, Armstrong Air, AirEase, Ducane,
                        Concord, Allied, Magic-Pak)
  Rheem family         (Rheem, Ruud, Weatherking, Eemax)
  ADP                  (evaporator coils)

Not yet supported (returns None, labels "verify nameplate"):
  York / Coleman / Luxaire / Champion / Fraser-Johnston / Guardian
  Mitsubishi / Fujitsu / LG / Samsung / Daikin minisplits
  Nortek / Nordyne / Maytag / Frigidaire / Tappan
  Older pre-1990 equipment with reused year codes
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

CURRENT_YEAR = datetime.now(timezone.utc).year
MIN_VALID = 1990
MAX_VALID = CURRENT_YEAR + 1


def _norm_brand(mfg: str) -> str:
    """Collapse brand variants and OEM family members to a single family key."""
    b = (mfg or "").strip().lower()
    if not b or b in {"n/a", "na", "unknown", "filter", "standard supply"}:
        return ""
    # Carrier family — Carrier, Bryant, Payne, Heil, Tempstar, Comfortmaker, Day & Night, Arcoaire, ICP, KeepRite
    if any(t in b for t in ["carrier", "bryant", "payne", "heil", "tempstar",
                            "comfortmaker", "day & night", "day and night",
                            "arcoaire", "keeprite", "icp"]):
        return "carrier"
    # Goodman family — Goodman, Amana, Daikin USA, Janitrol, Alumacoil (Goodman OEM coil)
    if any(t in b for t in ["goodman", "amana", "janitrol", "alumacoil"]):
        return "goodman"
    if "daikin" in b:
        return "goodman"   # Daikin USA uses Goodman serial format
    # Trane family — Trane, American Standard
    if "trane" in b or "american standard" in b:
        return "trane"
    # Lennox family — Lennox, Armstrong Air, AirEase, Ducane, Concord, Allied, Magic-Pak
    if any(t in b for t in ["lennox", "lenox", "armstrong", "airease", "ducane",
                            "concord", "magic-pak", "magicpak"]):
        return "lennox"
    # Rheem family — Rheem, Ruud, Weatherking, Eemax
    if any(t in b for t in ["rheem", "ruud", "weatherking", "eemax"]):
        return "rheem"
    if "adp" in b:
        return "adp"
    # Known unsupported — return so caller can label correctly
    if any(t in b for t in ["york", "coleman", "luxaire", "champion",
                            "fraser-johnston", "fraser johnston", "guardian",
                            "evcon", "johnson controls"]):
        return "york_unsupported"
    if any(t in b for t in ["mitsubishi", "fujitsu", "samsung", "lg "]):
        return "minisplit_unsupported"
    if any(t in b for t in ["nortek", "nordyne", "maytag", "frigidaire", "tappan"]):
        return "nortek_unsupported"
    return ""  # unknown


def _valid_year(y: int | None) -> int | None:
    if y is None:
        return None
    if MIN_VALID <= y <= MAX_VALID:
        return y
    return None


def _decode_carrier(serial: str):
    """Carrier family format: WWYY... (week then 2-digit year).
    Example: 3822J02530 -> week 38, year 2022."""
    m = re.match(r"^(\d{2})(\d{2})", serial)
    if not m:
        return None
    wk, yy = int(m.group(1)), int(m.group(2))
    if 1 <= wk <= 53:
        return _valid_year(2000 + yy), "high"
    return None


def _decode_goodman(serial: str):
    """Goodman/Amana/Daikin USA format: YYMM... or YYWW... (year first).
    Example: 2206227441 -> year 2022, month/week 06.
    Newer 10-digit format starts with YYMM."""
    m = re.match(r"^(\d{2})(\d{2})", serial)
    if not m:
        return None
    yy, mm = int(m.group(1)), int(m.group(2))
    # YY must be plausible; MM/WW gate sanity-checks against month or week ranges
    if 1 <= mm <= 53:
        return _valid_year(2000 + yy), "high"
    return None


def _decode_trane(serial: str):
    """Trane / American Standard formats:
      Modern (2010+): YY + WW + 6 alphanumerics.
        e.g. 22421SXUGG -> year 22, week 42; 23173KCKHG -> year 23, week 17.
        Note: the 5th char may be digit or letter — we validate by week range
        rather than character class.
    Older formats use letter year codes; not supported.
    """
    m = re.match(r"^(\d{2})(\d{2})", serial)
    if not m:
        return None
    yy, wk = int(m.group(1)), int(m.group(2))
    # Sanity: year 10-40, week 1-53
    if 10 <= yy <= 40 and 1 <= wk <= 53:
        return _valid_year(2000 + yy), "high"
    return None


def _decode_lennox(serial: str):
    """Lennox / Armstrong / Allied Air formats:
      A) Lennox modern: WWYY + plant letter + 5-digit unique.
         e.g. 1722128226 -> week 17, year 22 (2022).
      B) Armstrong Air / Allied plant-prefix: [plant_2digits] + YY + letter + 5 unique.
         e.g. 5917K09492 -> plant 59, year 17 (2017).
      Lennox/Armstrong share a parent (Allied Air Enterprises) and use both formats.
      We try format A first (Lennox modern); fall back to format B for Armstrong.
    """
    m = re.match(r"^(\d{2})(\d{2})", serial)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    # Format A: WWYY (Lennox modern). Week 1-53, year plausible.
    if 1 <= a <= 53 and 0 <= b <= 40:
        y = _valid_year(2000 + b)
        if y:
            return y, "high"
    # Format B: plant-prefix + YY (Armstrong Air). First two digits are plant
    # code (50-99 range typical), positions 3-4 are year.
    if 50 <= a <= 99 and 0 <= b <= 40:
        y = _valid_year(2000 + b)
        if y:
            return y, "medium"
    return None


def _decode_rheem(serial: str):
    """Rheem / Ruud format: letter prefix + WWYY + 5-digit unique.
    Example: W151419911 -> letter W, week 15, year 14 (2014).
    Some older units have YYWW after the letter; we use WWYY as the dominant
    modern format. If WW > 53 we fall back to YYWW."""
    m = re.match(r"^[A-Za-z](\d{2})(\d{2})", serial)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        # WWYY interpretation
        if 1 <= a <= 53:
            return _valid_year(2000 + b), "high"
        # YYWW fallback
        if 1 <= b <= 53:
            return _valid_year(2000 + a), "medium"
    # All-numeric WWYY (rare)
    m = re.match(r"^(\d{2})(\d{2})", serial)
    if m:
        wk, yy = int(m.group(1)), int(m.group(2))
        if 1 <= wk <= 53:
            return _valid_year(2000 + yy), "medium"
    return None


def _decode_adp(serial: str):
    """ADP (Advanced Distributor Products) evaporator coils.
    Format: 7-digit prefix may carry YY in positions 5-6 OR positions 3-4.
    Example: 7117L44444 -> year 17 (2017). Position 3-4 = year."""
    # Position 3-4 as YY
    m = re.match(r"^.{2}(\d{2})", serial)
    if m:
        yy = int(m.group(1))
        y = _valid_year(2000 + yy)
        if y:
            return y, "medium"
    return None


DECODERS = {
    "carrier": _decode_carrier,
    "goodman": _decode_goodman,
    "trane": _decode_trane,
    "lennox": _decode_lennox,
    "rheem": _decode_rheem,
    "adp": _decode_adp,
}


def decode_serial(manufacturer: str, serial: str) -> tuple[int, str, str] | None:
    """Return (year, confidence, source_label) or None if not decodable.

    source_label describes how we arrived at the year so the report card can
    show provenance (e.g. "Trane YY+DOY", "Carrier WWYY")."""
    fam = _norm_brand(manufacturer)
    s = re.sub(r"\s+", "", str(serial or "").strip())
    if not s or len(s) < 4 or s.lower() in {"n/a", "na"}:
        return None
    if fam in {"york_unsupported", "minisplit_unsupported", "nortek_unsupported", ""}:
        return None
    decoder = DECODERS.get(fam)
    if not decoder:
        return None
    out = decoder(s)
    if not out:
        return None
    year, conf = out
    if year is None:
        return None
    return year, conf, f"{fam} serial decode"


def supported_brands() -> list[str]:
    return ["Carrier family (Carrier, Bryant, Payne, Heil, Tempstar, Comfortmaker, Day & Night, Arcoaire, ICP, KeepRite)",
            "Goodman family (Goodman, Amana, Daikin USA, Janitrol)",
            "Trane family (Trane, American Standard)",
            "Lennox family (Lennox, Armstrong Air, AirEase, Ducane, Concord, Magic-Pak)",
            "Rheem family (Rheem, Ruud, Weatherking, Eemax)",
            "ADP (evap coils)"]


def unsupported_brand(manufacturer: str) -> bool:
    """True if we recognize the brand as known-unsupported (so report card can say so
    instead of just 'unknown')."""
    fam = _norm_brand(manufacturer)
    return fam in {"york_unsupported", "minisplit_unsupported", "nortek_unsupported"}
