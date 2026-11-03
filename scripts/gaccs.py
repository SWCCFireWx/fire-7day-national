"""
Shared GACC definitions and helpers for the national 7-Day verification site.

PSA codes in the national service start with the GACC's two-letter prefix (SW05, NR12, ...).
Each GACC has its own settings file, config/gaccs/<CODE>.json, created with defaults the
first time that GACC appears and then edited from the website's Criteria tab.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config", "gaccs")
INDEX_PATH = os.path.join(ROOT, "config", "gaccs.json")
DATA = os.path.join(ROOT, "data")

# prefix -> (center, name, time zone used for "the day", typical peak-season max at 1,000 ft (F))
KNOWN = {
    "NW": ("NWCC", "Northwest", "America/Los_Angeles", 96),
    "NC": ("ONCC", "Northern California", "America/Los_Angeles", 100),
    "SC": ("OSCC", "Southern California", "America/Los_Angeles", 104),
    "GB": ("GBCC", "Great Basin", "America/Boise", 102),
    "NR": ("NRCC", "Northern Rockies", "America/Denver", 94),
    "RM": ("RMCC", "Rocky Mountain", "America/Denver", 98),
    "SW": ("SWCC", "Southwest", "America/Denver", 106),
    "SA": ("SACC", "Southern Area", "America/Chicago", 98),
    "EA": ("EACC", "Eastern Area", "America/Chicago", 90),
    "AK": ("AICC", "Alaska", "America/Anchorage", 80),
}
# states searched for RAWS (stations are then assigned to PSAs by location)
STATES = ["AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
          "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
          "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR", "AK"]


def gacc_of(psa):
    m = re.match(r"[A-Z]+", str(psa or "").upper())
    return m.group(0) if m else "XX"


# Written trigger definitions provided so far (others start as placeholders to fill in).
LIGHTNING_DEF = {
    "RM": "Dry lightning with a coverage greater than 15% in combination with dry fuels that results in a greater than 20% chance of significant fires.",
    "NR": "Dry lightning with a coverage greater than 15% in combination with dry fuels that results in a greater than 20% chance of significant fires.",
}
RECREATION_GACCS = {"NW", "AK", "EA", "RM"}


def default_triggers(code=""):
    t = [
        {"letter": "W", "name": "Wind", "color": "orange", "method": "auto",
         "description": "Sustained winds of 20 mph or greater or consistent gusts to 35 mph or greater, with RH 15% or less.",
         "wind": {"sustained_mph": 20, "gust_mph": 35, "gust_hours": 2},
         "rh_max": 15, "temp_above_typical": None, "erc_min": None, "haines_min": None},
        {"letter": "B", "name": "Burn Environment", "color": "orange", "method": "auto",
         "description": "Temperatures much above typical peak fire season maximum values, with RH 15% or less and elevated low-level instability.",
         "wind": None, "rh_max": 15, "temp_above_typical": 5, "erc_min": None, "haines_min": 5},
        {"letter": "L", "name": "Lightning", "color": "red", "method": "manual",
         "description": LIGHTNING_DEF.get(code, "A greater than 20% chance of significant fires due to thunderstorms."),
         "wind": None, "rh_max": None, "temp_above_typical": None, "erc_min": None, "haines_min": None},
    ]
    if code in RECREATION_GACCS:
        t.append({"letter": "R", "name": "Recreation", "color": "orange", "method": "manual",
                  "description": "A greater than 20% chance of significant fires due to human activity and very dry background conditions.",
                  "wind": None, "rh_max": None, "temp_above_typical": None, "erc_min": None, "haines_min": None})
    return t


def default_config(code, gacc_field=""):
    center, name, tz, tmax = KNOWN.get(code, (code + "CC", gacc_field or code, "America/Denver", 98))
    return {
        "_readme": "Settings for this GACC. Edit from the website's Criteria tab.",
        "code": code, "center": center, "name": name, "timezone": tz,
        "editors": [],
        "criteria_sets": {
            "Standard": {
                "description": "Starting criteria (copied from the Southwest defaults). Replace with this GACC's triggers.",
                "stations_needed": {"default": 2, "small_psa_station_count": 3, "small_psa_needed": 1},
                "triggers": default_triggers(code),
            }
        },
        "schedule": [{"from": "2026-01-01", "set": "Standard", "note": "Starting criteria"}],
        "typical_max": {"base_f_at_1000ft": tmax, "drop_per_1000ft": 3.5},
        "typical_peak_max_f": {},
        "erc_fuel_model": "Y",
        "afternoon_hours": {"start": 10, "end": 20},
        "dry_rh_context": 20,
    }


def config_path(code):
    return os.path.join(CONFIG_DIR, f"{code}.json")


def load_config(code, create=True, gacc_field=""):
    p = config_path(code)
    if not os.path.exists(p):
        if not create:
            return None
        os.makedirs(CONFIG_DIR, exist_ok=True)
        save_config(code, default_config(code, gacc_field))
    with open(p) as fh:
        return json.load(fh)


def save_config(code, cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(config_path(code), "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


def gacc_dir(code):
    p = os.path.join(DATA, code)
    os.makedirs(p, exist_ok=True)
    return p


def update_index(psas_by_gacc):
    """config/gaccs.json lists the GACCs and their PSAs; the website reads it to build the GACC bar."""
    idx = {"gaccs": []}
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as fh:
            idx = json.load(fh)
    known = {g["code"]: g for g in idx.get("gaccs", [])}
    for code, psas in psas_by_gacc.items():
        cfg = load_config(code)
        g = known.get(code, {"code": code})
        g.update({"name": cfg.get("name", code), "center": cfg.get("center", ""),
                  "psas": sorted(set(g.get("psas", [])) | set(psas))})
        known[code] = g
    order = list(KNOWN)
    idx["gaccs"] = sorted(known.values(), key=lambda g: (order.index(g["code"]) if g["code"] in order else 99, g["code"]))
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w") as fh:
        json.dump(idx, fh, indent=1)
        fh.write("\n")
    return idx


def criteria_for(d, cfg):
    """(set name, set dict) in use on date d: the last schedule entry starting on or before d."""
    sets = {k: v for k, v in (cfg.get("criteria_sets") or {}).items() if not k.startswith("_")}
    if not sets:
        raise RuntimeError(f"{cfg.get('code')}: no criteria sets")
    sched = sorted((e for e in cfg.get("schedule") or [] if e.get("set") in sets), key=lambda e: e["from"])
    name = sched[0]["set"] if sched else next(iter(sets))
    for e in sched:
        if e["from"] <= d.isoformat():
            name = e["set"]
    return name, sets[name]
