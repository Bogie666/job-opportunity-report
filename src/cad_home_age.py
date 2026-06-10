
#!/usr/bin/env python3
"""Free public CAD home age resolver for LEX tech briefs.

Phase 1: Dallas CAD scraper (free, no API key) with JSON cache.
Returns build year + source metadata. Intended as a cache-first layer before paid APIs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, quote_plus
from urllib.request import Request, build_opener

BASE = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE / "data" / "cad_home_age_cache.json"

UA = "Mozilla/5.0 (compatible; LEX-TechBriefHomeAge/0.1; +https://lexairconditioning.com)"

DALLAS_CAD = "https://www.dallascad.org/SearchAddr.aspx"
COLLIN_SOCRATA = "https://data.texas.gov/resource/nne4-8riu.json"
TRUEPRODIGY_API = "https://prod-container.trueprodigyapi.com"
TRUEPRODIGY_SOURCES = {
    "denton_cad_trueprodigy": {
        "office": "Denton",
        "host": "www.dentoncad.com",
        "city_hints": {"ARGYLE", "AUBREY", "BARTONVILLE", "CARROLLTON", "COPPER CANYON", "CORINTH", "CROSS ROADS", "DENTON", "DOUBLE OAK", "FLOWER MOUND", "FRISCO", "HACKBERRY", "HEBRON", "HICKORY CREEK", "HIGHLAND VILLAGE", "JUSTIN", "KRUGERVILLE", "KRUM", "LAKE DALLAS", "LAKEWOOD VILLAGE", "LEWISVILLE", "LITTLE ELM", "NEW FAIRVIEW", "NORTHLAKE", "OAK POINT", "PILOT POINT", "PONDER", "PROSPER", "ROANOKE", "SANGER", "SHADY SHORES", "THE COLONY", "TROPHY CLUB"},
    },
    "rockwall_cad_trueprodigy": {
        "office": "Rockwall",
        "host": "www.rockwallcad.com",
        "city_hints": {"FATE", "HEATH", "MCLENDON-CHISHOLM", "MOBILE CITY", "ROCKWALL", "ROWLETT", "ROYSE CITY", "WYLIE"},
    },
}
COLLIN_CITY_HINTS = {
    "ALLEN", "ANNA", "BLUE RIDGE", "CARROLLTON", "CELINA", "DALLAS", "FAIRVIEW",
    "FARMERSVILLE", "FRISCO", "JOSEPHINE", "LAVON", "LOWRY CROSSING", "LUCAS",
    "MCKINNEY", "MELISSA", "MURPHY", "NEVADA", "NEW HOPE", "PARKER", "PLANO",
    "PRINCETON", "PROSPER", "RICHARDSON", "ROYSE CITY", "SACHSE", "ST PAUL",
    "VAN ALSTYNE", "WESTMINSTER", "WESTON", "WYLIE",
}
DALLAS_CITY_CODES = {
    "ADDISON": "1", "BALCH SPRINGS": "2", "CARROLLTON": "3", "CEDAR HILL": "6",
    "COCKRELL HILL": "7", "COMBINE": "9", "COPPELL": "10", "DALLAS": "12",
    "DESOTO": "15", "DUNCANVILLE": "16", "FARMERS BRANCH": "17", "FERRIS": "18",
    "GARLAND": "20", "GLENN HEIGHTS": "22", "GRAND PRAIRIE": "24", "GRAPEVINE": "28",
    "HIGHLAND PARK": "29", "HUTCHINS": "30", "IRVING": "31", "LANCASTER": "32",
    "LEWISVILLE": "33", "MESQUITE": "34", "NO TOWN": "37", "OVILLA": "38",
    "RICHARDSON": "39", "ROWLETT": "40", "SACHSE": "42", "SEAGOVILLE": "43",
    "SUNNYVALE": "45", "UNIVERSITY PARK": "46", "WILMER": "48", "WYLIE": "49",
}

STREET_SUFFIXES = {
    "ST", "STREET", "DR", "DRIVE", "RD", "ROAD", "LN", "LANE", "CT", "COURT", "CIR", "CIRCLE",
    "AVE", "AVENUE", "BLVD", "BOULEVARD", "PKWY", "PARKWAY", "PL", "PLACE", "WAY", "TER", "TERRACE",
    "TRL", "TRAIL", "HWY", "HIGHWAY", "LOOP", "BEND", "CV", "COVE", "PASS", "PATH", "RUN",
}
DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}

@dataclass
class CadResult:
    input_address: str
    normalized_address: str
    source: str
    status: str
    year_built: int | None = None
    effective_year_built: int | None = None
    account: str | None = None
    matched_address: str | None = None
    owner: str | None = None
    confidence: str = "none"
    detail_url: str | None = None
    error: str | None = None

class FormParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.inputs=[]; self.selects=[]; self.cur=None
    def handle_starttag(self, tag, attrs):
        d=dict(attrs)
        if tag == 'input': self.inputs.append(d)
        elif tag == 'select': self.cur={'name':d.get('name'), 'options':[]}; self.selects.append(self.cur)
        elif tag == 'option' and self.cur is not None: self.cur['options'].append(d)
    def handle_endtag(self, tag):
        if tag == 'select': self.cur=None

def _text(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()

def parse_address(address: str) -> dict[str, str]:
    # Accept "street, city, state, zip" from ServiceTitan.
    parts = [p.strip() for p in address.split(',')]
    street = parts[0] if parts else address
    city = parts[1].strip().upper() if len(parts) > 1 else ""
    m = re.match(r"^\s*(\d+[A-Za-z]?)\s+(.*)$", street.strip())
    num = m.group(1) if m else ""
    rest = (m.group(2) if m else street).upper().replace('.', '')
    toks = rest.split()
    direction = ""
    if toks and toks[0] in DIRECTIONS:
        direction = toks.pop(0)
    # Drop unit/suite fragment and terminal street suffix for DCAD's street-name field.
    stop_words = {"APT", "UNIT", "STE", "SUITE", "#"}
    cleaned=[]
    for t in toks:
        if t in stop_words: break
        cleaned.append(t)
    while cleaned and cleaned[-1] in STREET_SUFFIXES:
        cleaned.pop()
    st_name = " ".join(cleaned)
    norm = f"{num} {direction + ' ' if direction else ''}{st_name}, {city}".strip()
    return {"number": num, "direction": direction, "street_name": st_name, "city": city, "normalized": norm}

def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try: return json.loads(CACHE_PATH.read_text())
        except Exception: return {}
    return {}

def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))

def _cache_key(address: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", address.upper()).strip().encode()).hexdigest()

def lookup_dallas_cad(address: str, use_cache: bool = True) -> CadResult:
    parsed = parse_address(address)
    result = CadResult(address, parsed['normalized'], "dallas_cad", "not_found")
    if not parsed['number'] or not parsed['street_name']:
        result.status = "bad_input"; result.error = "Could not parse street number/name"; return result
    if parsed['city'] and parsed['city'] not in DALLAS_CITY_CODES:
        result.status = "not_applicable"; result.error = f"City {parsed['city']} not in Dallas CAD city list"; return result

    key = "dallas_cad:" + _cache_key(address)
    cache = _load_cache() if use_cache else {}
    if use_cache and key in cache:
        return CadResult(**cache[key])

    opener = build_opener()
    headers = {"User-Agent": UA}
    try:
        form_html = opener.open(Request(DALLAS_CAD, headers=headers), timeout=25).read().decode('utf-8', 'replace')
        p = FormParser(); p.feed(form_html)
        data = {i['name']: i.get('value','') for i in p.inputs if i.get('name')}
        for s in p.selects: data.setdefault(s['name'], '')
        data.update({
            'txtAddrNum': parsed['number'],
            'listStDir': parsed['direction'],
            'txtStName': parsed['street_name'],
            'listCity': DALLAS_CITY_CODES.get(parsed['city'], ''),
            'cmdSubmit': 'Search',
            'AcctTypeCheckList1:chkAcctType:0': 'on',
            'AcctTypeCheckList1:chkAcctType:1': 'on',
            'AcctTypeCheckList1:chkAcctType:2': 'on',
        })
        search_html = opener.open(Request(DALLAS_CAD, data=urlencode(data).encode(), headers={**headers, 'Content-Type':'application/x-www-form-urlencoded', 'Referer': DALLAS_CAD}), timeout=35).read().decode('utf-8', 'replace')
        if 'SearchError.aspx' in search_html:
            result.status = 'error'; result.error = 'Dallas CAD returned SearchError'; return result
        links = re.findall(r'href="(AcctDetailRes\.aspx\?ID=([0-9A-Za-z]+))"', search_html, re.I)
        if not links:
            # Some single-result pages keep the link escaped or generate relative href without quotes.
            links = [(m, re.search(r'ID=([0-9A-Za-z]+)', m).group(1)) for m in re.findall(r'AcctDetailRes\.aspx\?ID=[0-9A-Za-z]+', search_html, re.I)]
        if not links:
            result.status = 'not_found'; return result
        # Prefer first residential result. For exact ST addresses this has been deterministic.
        rel, acct = links[0]
        detail_url = urljoin(DALLAS_CAD, rel.replace('&amp;', '&'))
        detail_html = opener.open(Request(detail_url, headers={**headers, 'Referer': DALLAS_CAD}), timeout=35).read().decode('utf-8', 'replace')
        detail_text = _text(detail_html)
        y = re.search(r'\bYear Built\b\s*(\d{4})', detail_text, re.I)
        ey = re.search(r'\bEffective Year Built\b\s*(\d{4})', detail_text, re.I)
        loc = re.search(r'Address:\s*([^\n]+?)\s+Neighborhood:', detail_text, re.I)
        own = re.search(r'Owner \(Current \d{4}\)\s+(.+?)\s+Multi-Owner', detail_text, re.I)
        result.status = 'found' if y else 'found_no_year'
        result.year_built = int(y.group(1)) if y else None
        result.effective_year_built = int(ey.group(1)) if ey else None
        result.account = acct
        result.detail_url = detail_url
        result.matched_address = loc.group(1).strip() if loc else None
        result.owner = own.group(1).strip()[:120] if own else None
        result.confidence = 'high' if result.year_built else 'low'
        if use_cache:
            cache[key] = asdict(result); _save_cache(cache)
        time.sleep(0.25)  # be polite to public CAD
        return result
    except Exception as exc:
        result.status = 'error'; result.error = str(exc); return result

def _soql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def lookup_collin_socrata(address: str, use_cache: bool = True) -> CadResult:
    """Resolve year built from Collin CAD's free data.texas.gov export.

    Collin CAD links this public Socrata dataset from its website. It includes
    situs address fields and imprvyearbuilt, so it is a no-key fallback for
    McKinney/Plano/Allen/Frisco/Collin-side addresses.
    """
    parsed = parse_address(address)
    result = CadResult(address, parsed['normalized'], "collin_cad_open_data", "not_found")
    if not parsed['number'] or not parsed['street_name']:
        result.status = "bad_input"; result.error = "Could not parse street number/name"; return result
    if parsed['city'] and parsed['city'] not in COLLIN_CITY_HINTS:
        result.status = "not_applicable"; result.error = f"City {parsed['city']} not in Collin CAD city hint list"; return result

    key = "collin_cad_open_data:" + _cache_key(address)
    cache = _load_cache() if use_cache else {}
    if use_cache and key in cache:
        return CadResult(**cache[key])

    where_parts = [
        f"situsbldgnum={_soql_quote(parsed['number'])}",
        f"situsstreetname={_soql_quote(parsed['street_name'])}",
    ]
    if parsed['city']:
        where_parts.append(f"situscity={_soql_quote(parsed['city'])}")
    params = {
        "$limit": "3",
        "$order": "propyear DESC",
        "$select": "propyear,propid,geoid,situsconcat,situsconcatshort,situsbldgnum,situsstreetname,situsstreetsuffix,situscity,situszip,ownername,imprvyearbuilt,imprvmainarea,propsubtype",
        "$where": " AND ".join(where_parts),
    }
    url = COLLIN_SOCRATA + "?" + urlencode(params)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    try:
        rows = json.loads(build_opener().open(Request(url, headers=headers), timeout=30).read().decode("utf-8", "replace"))
        if not rows:
            result.status = "not_found"
        else:
            row = rows[0]
            year_raw = row.get("imprvyearbuilt")
            year = int(year_raw) if str(year_raw or "").isdigit() else None
            result.status = "found" if year else "found_no_year"
            result.year_built = year
            result.effective_year_built = year
            result.account = str(row.get("propid") or row.get("geoid") or "") or None
            result.matched_address = row.get("situsconcat") or row.get("situsconcatshort")
            result.owner = (row.get("ownername") or "")[:120] or None
            result.detail_url = f"https://data.texas.gov/resource/nne4-8riu.json?propid={quote_plus(str(row.get('propid') or ''))}" if row.get("propid") else None
            result.confidence = "high" if year and len(rows) == 1 else ("medium" if year else "low")
        if use_cache:
            cache[key] = asdict(result); _save_cache(cache)
        time.sleep(0.15)
        return result
    except Exception as exc:
        result.status = "error"; result.error = str(exc); return result


def _trueprodigy_headers(host: str, path: str = "/property-search/") -> dict[str, str]:
    origin = f"https://{host}"
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin + path,
        "Cache-Control": "no-cache",
    }


def _trueprodigy_token(office: str, host: str) -> str:
    headers = _trueprodigy_headers(host)
    payload = json.dumps({"office": office}).encode()
    body = json.loads(build_opener().open(Request(
        TRUEPRODIGY_API + "/trueprodigy/cadpublic/auth/token",
        data=payload,
        headers=headers,
        method="POST",
    ), timeout=30).read().decode("utf-8", "replace"))
    return body["user"]["token"]


def lookup_trueprodigy_cad(address: str, source: str, use_cache: bool = True) -> CadResult:
    cfg = TRUEPRODIGY_SOURCES[source]
    parsed = parse_address(address)
    result = CadResult(address, parsed['normalized'], source, "not_found")
    if not parsed['number'] or not parsed['street_name']:
        result.status = "bad_input"; result.error = "Could not parse street number/name"; return result
    if parsed['city'] and parsed['city'] not in cfg["city_hints"]:
        result.status = "not_applicable"; result.error = f"City {parsed['city']} not in {cfg['office']} CAD city hint list"; return result

    key = source + ":" + _cache_key(address)
    cache = _load_cache() if use_cache else {}
    if use_cache and key in cache:
        return CadResult(**cache[key])

    headers = _trueprodigy_headers(cfg["host"])
    try:
        token = _trueprodigy_token(cfg["office"], cfg["host"])
        query = " ".join(x for x in [parsed["number"], parsed["street_name"]] if x)
        # True Prodigy public search returns property identity/address reliably.
        # The improvement endpoint with actualYearBuilt/effYearBuilt is protected
        # from this sandbox by a 403, so this connector records found_no_year
        # rather than using weaker proxies like account create/deed dates.
        payload = {
            "pYear": {"operator": "=", "value": "2026"},
            "fullTextSearch": {"operator": "match", "value": query},
        }
        url = TRUEPRODIGY_API + "/public/property/searchfulltext?page=1&pageSize=5"
        rows_body = json.loads(build_opener().open(Request(
            url,
            data=json.dumps(payload).encode(),
            headers={**headers, "Authorization": token},
            method="POST",
        ), timeout=35).read().decode("utf-8", "replace"))
        rows = rows_body.get("results") or []
        if not rows:
            result.status = "not_found"
        else:
            # Prefer exact street number + street name; fallback to first row.
            exact = [r for r in rows if str(r.get("streetNum") or "").upper() == parsed["number"].upper() and str(r.get("streetName") or "").upper() == parsed["street_name"].upper()]
            row = exact[0] if exact else rows[0]
            result.status = "found_no_year"
            result.account = str(row.get("pid") or row.get("pAccountID") or "") or None
            result.matched_address = row.get("fullSitus") or row.get("streetPrimary") or row.get("addrDeliveryLine")
            result.owner = (row.get("displayName") or row.get("name") or "")[:120] or None
            result.detail_url = f"https://{cfg['host']}/property-detail/{row.get('pid')}" if row.get("pid") else None
            result.confidence = "medium" if exact else "low"
            # Keep account creation date out of year_built; it is not reliable enough for tech guidance.
            result.error = "Property found, but public search result did not expose year built; improvement endpoint blocked from sandbox"
        if use_cache:
            cache[key] = asdict(result); _save_cache(cache)
        time.sleep(0.15)
        return result
    except Exception as exc:
        result.status = "error"; result.error = str(exc); return result


def lookup_free_cad(address: str, use_cache: bool = True) -> CadResult:
    # Free-source chain. Try county-specific no-key sources before any paid API.
    parsed = parse_address(address)
    candidates = []
    if not parsed['city'] or parsed['city'] in DALLAS_CITY_CODES:
        candidates.append(lookup_dallas_cad)
    if not parsed['city'] or parsed['city'] in COLLIN_CITY_HINTS:
        candidates.append(lookup_collin_socrata)
    for source, cfg in TRUEPRODIGY_SOURCES.items():
        if not parsed['city'] or parsed['city'] in cfg["city_hints"]:
            candidates.append(lambda addr, use_cache=True, source=source: lookup_trueprodigy_cad(addr, source, use_cache=use_cache))
    if lookup_dallas_cad not in candidates:
        candidates.append(lookup_dallas_cad)
    if lookup_collin_socrata not in candidates:
        candidates.append(lookup_collin_socrata)

    last = None
    first_property_match = None
    for fn in candidates:
        res = fn(address, use_cache=use_cache)
        last = res
        if res.status == "found":
            return res
        if res.status == "found_no_year" and first_property_match is None:
            first_property_match = res
    return first_property_match or last or CadResult(address, parsed['normalized'], "free_cad", "not_found")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('address', nargs='*')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--no-cache', action='store_true')
    args = ap.parse_args()
    address = ' '.join(args.address).strip()
    if not address:
        raise SystemExit('Usage: cad_home_age.py "9228 Moss Haven Drive, Dallas, TX, 75231"')
    res = lookup_free_cad(address, use_cache=not args.no_cache)
    print(json.dumps(asdict(res), indent=2) if args.json else f"{res.status}: {res.year_built} ({res.source}, {res.confidence}) {res.detail_url or ''}")

if __name__ == '__main__':
    main()
