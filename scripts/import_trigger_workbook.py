#!/usr/bin/env python3
"""
Load the "GACC trigger starting points" workbook (one tab per GACC) into config/gaccs/<CODE>.json.

Each tab's trigger table replaces the triggers of the criteria set currently in use for that GACC,
and the settings at the top of the tab replace stations needed, typical max and ERC fuel model.
Tabs that aren't GACC codes (like Instructions) are skipped. Needs openpyxl.

Usage: python scripts/import_trigger_workbook.py GACC-trigger-starting-points.xlsx [--dry-run]
"""

import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

LABELS = {
    "stations that must hit (most psas)": ("sn", "default"),
    "a small psa has this many stations or fewer": ("sn", "small_psa_station_count"),
    "stations that must hit in a small psa": ("sn", "small_psa_needed"),
    "typical peak-season max at 1,000 ft (°f)": ("tm", "base_f_at_1000ft"),
    "typical max drops by (°f per 1,000 ft)": ("tm", "drop_per_1000ft"),
    "erc fuel model (v, w, x, y or z)": ("fm", None),
    "notes for this gacc (optional)": ("note", None),
}


def num(v, what, lo, hi, integer=False):
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace("%", "").replace("°", "").strip())
    except ValueError:
        raise ValueError(f"{what}: '{v}' isn't a number")
    if not lo <= x <= hi:
        raise ValueError(f"{what}: {x:g} is outside {lo}-{hi}")
    return int(round(x)) if integer or x == int(x) else round(x, 1)


def read_tab(ws, code):
    settings, triggers, problems = {}, [], []
    hdr = None
    for row in ws.iter_rows(values_only=True):
        a = str(row[0] or "").strip()
        key = LABELS.get(a.lower())
        if key:
            settings[key] = row[3] if len(row) > 3 else None
        if a == "Letter":
            hdr = True
            continue
        if hdr:
            cells = list(row[:12]) + [None] * (12 - len(row[:12]))
            letter = str(cells[0] or "").strip().upper()
            if not letter and not str(cells[1] or "").strip():
                continue
            where = f"{code} trigger '{letter or cells[1]}'"
            try:
                if not re.fullmatch(r"[A-Z]", letter):
                    raise ValueError(f"{where}: letter must be a single letter A-Z")
                manual = bool(re.search(r"hand|manual", str(cells[2] or ""), re.I))
                t = {"letter": letter, "name": str(cells[1] or letter).strip()[:40],
                     "color": "red" if re.search(r"red", str(cells[10] or ""), re.I) else "orange",
                     "method": "manual" if manual else "auto",
                     "description": str(cells[11] or "").strip()[:400],
                     "wind": None, "rh_max": None, "temp_above_typical": None, "erc_min": None}
                if not manual:
                    sus, gust = num(cells[3], f"{where} sustained wind", 1, 100), num(cells[4], f"{where} gusts", 1, 150)
                    if sus is not None or gust is not None:
                        t["wind"] = {"sustained_mph": sus, "gust_mph": gust, "gust_hours": num(cells[5], f"{where} gust hours", 1, 24, True) or 2}
                    t["rh_max"] = num(cells[6], f"{where} RH", 1, 100)
                    t["temp_above_typical"] = num(cells[7], f"{where} temperature", -30, 50)
                    t["erc_min"] = num(cells[8], f"{where} ERC", 0, 150)
                    if not (t["wind"] or t["rh_max"] is not None or t["temp_above_typical"] is not None or t["erc_min"] is not None):
                        problems.append(f"{where}: automatic but no thresholds filled in; set to 'By hand' for now")
                        t["method"] = "manual"
                if any(x["letter"] == letter for x in triggers):
                    raise ValueError(f"{where}: letter used twice")
                triggers.append(t)
            except ValueError as e:
                problems.append(str(e))
    return settings, triggers, problems


def main():
    from openpyxl import load_workbook
    path, dry = sys.argv[1], "--dry-run" in sys.argv
    wb = load_workbook(path, data_only=True)
    report = []
    # full wording tab: (gacc, letter) -> wording; risk-level wording kept too
    wording, levels = {}, {}
    if "Trigger wording" in wb.sheetnames:
        for row in wb["Trigger wording"].iter_rows(min_row=5, values_only=True):
            code, letter, name, how, text = [str(v or "").strip() for v in (list(row[:5]) + [None] * 5)[:5]]
            if not re.fullmatch(r"[A-Z]{2}", code.upper()) or not text:
                continue
            if "risk level" in how.lower() or (not letter and name.lower() in ("little or no risk", "low risk", "moderate risk")):
                levels.setdefault(code.upper(), {})[name] = text[:600]
            elif re.fullmatch(r"[A-Za-z]", letter):
                wording[(code.upper(), letter.upper())] = {"name": name, "how": how, "text": text[:600]}
    for ws in wb.worksheets:
        code = ws.title.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            continue
        settings, triggers, problems = read_tab(ws, code)
        cfg = gaccs.load_config(code)
        today = date.today()
        set_name, _ = gaccs.criteria_for(today, cfg)
        cs = cfg["criteria_sets"][set_name]
        if triggers:
            cs["triggers"] = triggers
        for (g, L), w in wording.items():       # wording tab wins for definitions; new letters are added by hand
            if g != code:
                continue
            t = next((x for x in cs["triggers"] if x["letter"] == L), None)
            if t is None:
                t = {"letter": L, "name": (w["name"] or L)[:40], "color": "orange", "method": "manual", "description": "",
                     "wind": None, "rh_max": None, "temp_above_typical": None, "erc_min": None}
                cs["triggers"].append(t)
                problems.append(f"{code} {L} {t['name']}: added from the wording tab as 'By hand'; convert to automatic thresholds if it should be checked automatically")
            t["description"] = w["text"]
            if w["name"]:
                t["name"] = w["name"][:40]
        if levels.get(code):
            cfg["risk_level_wording"] = levels[code]
        sn = cs.setdefault("stations_needed", {})
        for (kind, k), v in settings.items():
            try:
                if kind == "sn" and v not in (None, ""):
                    sn[k] = num(v, f"{code} {k}", 0 if k == "small_psa_station_count" else 1, 30, True)
                elif kind == "tm" and v not in (None, ""):
                    cfg.setdefault("typical_max", {})[k] = num(v, f"{code} {k}", 0 if "drop" in k else 60, 8 if "drop" in k else 130)
                elif kind == "fm" and v not in (None, ""):
                    fm = str(v).strip().upper()
                    if fm not in ("V", "W", "X", "Y", "Z"):
                        raise ValueError(f"{code} ERC fuel model '{v}' must be V, W, X, Y or Z")
                    cfg["erc_fuel_model"] = fm
                elif kind == "note" and v not in (None, ""):
                    cs["description"] = str(v).strip()[:200]
            except ValueError as e:
                problems.append(str(e))
        if cs.get("description", "").startswith("Starting criteria (copied from the Southwest defaults)"):
            cs["description"] = f"{cfg['name']} triggers (from the trigger starting-points workbook, {today.isoformat()})."
        if not dry:
            gaccs.save_config(code, cfg)
        report.append(f"{code}: {len(triggers)} triggers ({', '.join(t['letter'] + ' ' + t['name'] + ('*' if t['method'] == 'manual' else '') for t in triggers)})"
                      + "".join(f"\n    ! {p}" for p in problems))
    print("\n".join(report))
    print("(* = verified by hand)" + ("  [dry run: nothing saved]" if dry else ""))


if __name__ == "__main__":
    main()
