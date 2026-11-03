#!/usr/bin/env python3
"""
Apply a GACC's settings change submitted from the website's Criteria tab.

The website opens a GitHub issue titled "[criteria XX] ..." whose body holds a ```json block:
  { "gacc": "SW", "criteria_sets": {...}, "schedule": [...], "typical_peak_max_f": {...},
    "typical_max": {...}, "erc_fuel_model": "Y", "editors": [...],
    "confirm_add": [{date, psa, trigger, notes}], "confirm_remove": [...] }

Who may apply it:
  * repository admins (owner, organization members, collaborators) - any GACC, including its editor list
  * people listed in that GACC's "editors" - that GACC only, everything except the editor list

Writes config/gaccs/<CODE>.json and data/<CODE>/confirmed.csv, and criteria_result.md for the reply.
Environment: ISSUE_BODY, ISSUE_AUTHOR, ISSUE_ASSOC. Exit 0 = applied, 1 = rejected.
"""

import csv
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

RESULT = os.path.join(gaccs.ROOT, "criteria_result.md")
COND_LIMITS = {"sustained_mph": (1, 100), "gust_mph": (1, 150), "gust_hours": (1, 24), "rh_max": (1, 100),
               "temp_above_typical": (-30, 50), "erc_min": (0, 150), "haines_min": (2, 6)}
SN_LIMITS = {"default": (1, 30), "small_psa_station_count": (0, 30), "small_psa_needed": (1, 30)}


def fail(msg):
    with open(RESULT, "w") as fh:
        fh.write(f"**Not applied.** {msg}\n\nNothing was changed.\n")
    print("REJECTED:", msg)
    sys.exit(1)


def text(s, n):
    return re.sub(r"[\x00-\x1f<>`]", "", str(s or "")).strip()[:n]


def number(v, lo, hi, what, integer=False):
    if v is None or v == "":
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        fail(f"{what} isn't a number.")
    if not lo <= v <= hi:
        fail(f"{what} = {v:g} is outside {lo} to {hi}.")
    return int(round(v)) if integer else (int(v) if v == int(v) else round(v, 1))


def clean_trigger(t, where):
    letter = str(t.get("letter", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]", letter):
        fail(f"{where}: trigger letter '{letter}' must be a single letter A-Z.")
    name = text(t.get("name"), 40) or letter
    method = t.get("method")
    if method not in ("auto", "manual"):
        fail(f"{where}: trigger {letter} must be checked automatically or by hand.")
    color = t.get("color") if t.get("color") in ("orange", "red") else "orange"
    out = {"letter": letter, "name": name, "color": color, "method": method, "description": text(t.get("description"), 400)}
    w = t.get("wind")
    out["wind"] = None
    if w:
        out["wind"] = {"sustained_mph": number(w.get("sustained_mph"), *COND_LIMITS["sustained_mph"], f"{where} {letter} sustained wind"),
                       "gust_mph": number(w.get("gust_mph"), *COND_LIMITS["gust_mph"], f"{where} {letter} gusts"),
                       "gust_hours": number(w.get("gust_hours") or 1, *COND_LIMITS["gust_hours"], f"{where} {letter} gust hours", True)}
        if out["wind"]["sustained_mph"] is None and out["wind"]["gust_mph"] is None:
            out["wind"] = None
    for k in ("rh_max", "temp_above_typical", "erc_min", "haines_min"):
        out[k] = number(t.get(k), *COND_LIMITS[k], f"{where} {letter} {k.replace('_', ' ')}", k == "haines_min")
    if method == "auto" and not (out["wind"] or out["rh_max"] is not None or out["temp_above_typical"] is not None or out["erc_min"] is not None):
        fail(f"{where}: automatic trigger {letter} needs at least one station condition (wind, RH, temperature or ERC).")
    return out


def main():
    body = os.environ.get("ISSUE_BODY", "")
    author = os.environ.get("ISSUE_AUTHOR", "").strip()
    admin = os.environ.get("ISSUE_ASSOC", "") in ("OWNER", "MEMBER", "COLLABORATOR")
    m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        fail("Couldn't find the settings block. Please save from the website rather than editing the issue text.")
    try:
        sub = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        fail(f"The settings block isn't valid ({e}).")
    g = str(sub.get("gacc", "")).upper()
    if not re.fullmatch(r"[A-Z]{2,4}", g) or not os.path.exists(gaccs.config_path(g)):
        fail(f"Unknown GACC '{g}'.")
    cfg = gaccs.load_config(g, create=False)
    editors = [e.lower() for e in cfg.get("editors", [])]
    if not admin and author.lower() not in editors:
        fail(f"@{author} isn't on the {cfg.get('name', g)} editor list. Ask the repository owner to add you on the Criteria tab.")

    # ---- editor list (admins only)
    if "editors" in sub:
        new_ed = sorted({text(e, 39).lstrip("@") for e in sub.get("editors") or [] if re.fullmatch(r"@?[A-Za-z0-9-]{1,39}", str(e).strip())}, key=str.lower)
        if [e.lower() for e in new_ed] != sorted(editors):
            if not admin:
                fail("Only repository admins can change the editor list.")
            cfg["editors"] = new_ed

    # ---- criteria sets
    sets_in = sub.get("criteria_sets")
    if not isinstance(sets_in, dict) or not sets_in or len(sets_in) > 20:
        fail("Between 1 and 20 criteria sets are required.")
    sets = {}
    for raw_name, s in sets_in.items():
        name = text(raw_name, 40)
        if not name or name.startswith("_") or name in sets or not isinstance(s, dict):
            fail(f"'{raw_name}' isn't a usable set name (empty, duplicate or malformed).")
        trig = s.get("triggers") or []
        if not isinstance(trig, list) or len(trig) > 12:
            fail(f"Set '{name}': up to 12 triggers.")
        clean = [clean_trigger(t, f"Set '{name}'") for t in trig]
        if len({t["letter"] for t in clean}) != len(clean):
            fail(f"Set '{name}': two triggers use the same letter.")
        sn = s.get("stations_needed") or {}
        sets[name] = {"description": text(s.get("description"), 200),
                      "stations_needed": {k: number(sn.get(k), *v, f"Set '{name}' stations {k.replace('_', ' ')}", True) for k, v in SN_LIMITS.items()},
                      "triggers": clean}
        if None in sets[name]["stations_needed"].values():
            fail(f"Set '{name}': every 'stations needed' value is required.")

    # ---- schedule
    sched, seen = [], set()
    for e in sub.get("schedule") or []:
        frm = str((e or {}).get("from", ""))
        try:
            date.fromisoformat(frm)
        except ValueError:
            fail(f"Schedule date '{frm}' isn't valid.")
        st = text(e.get("set"), 40)
        if st not in sets:
            fail(f"Schedule entry on {frm} uses set '{st}', which doesn't exist.")
        if frm in seen:
            fail(f"Two schedule entries start on {frm}.")
        seen.add(frm)
        sched.append({"from": frm, "set": st, "note": text(e.get("note"), 200)})
    if not sched:
        fail("The schedule needs at least one entry.")
    sched.sort(key=lambda e: e["from"])

    # ---- typical max, fuel model
    tpm = {}
    for psa, v in (sub.get("typical_peak_max_f") or {}).items():
        if psa.startswith("_") or v in (None, ""):
            continue
        if gaccs.gacc_of(psa) != g:
            fail(f"{psa} isn't a {g} PSA.")
        tpm[psa.upper()] = number(v, 40, 130, f"Typical max for {psa}")
    tm = sub.get("typical_max") or cfg.get("typical_max") or {}
    typical = {"base_f_at_1000ft": number(tm.get("base_f_at_1000ft"), 60, 130, "Typical max at 1,000 ft") or 98,
               "drop_per_1000ft": number(tm.get("drop_per_1000ft"), 0, 8, "Typical max drop per 1,000 ft") or 0}
    fm = str(sub.get("erc_fuel_model", cfg.get("erc_fuel_model", "Y"))).upper()
    if fm not in ("V", "W", "X", "Y", "Z"):
        fail("ERC fuel model must be V, W, X, Y or Z.")

    # ---- confirmed manual trigger days
    letters = {t["letter"] for s in sets.values() for t in s["triggers"]}

    def conf(key):
        out = []
        for it in sub.get(key) or []:
            d = str((it or {}).get("date", ""))
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                fail(f"Confirmed day '{d}' isn't a valid date.")
            if dd > date.today():
                fail(f"Confirmed day {d} is in the future.")
            psa, tr = str(it.get("psa", "")).upper(), str(it.get("trigger", "")).upper()
            if gaccs.gacc_of(psa) != g:
                fail(f"{psa} isn't a {g} PSA.")
            if key == "confirm_add" and tr not in letters:
                fail(f"Trigger '{tr}' isn't defined for {g}.")
            out.append({"date": d, "psa": psa, "trigger": tr, "notes": text(it.get("notes"), 200).replace(",", ";")})
        return out
    add, rem = conf("confirm_add"), conf("confirm_remove")
    cpath = os.path.join(gaccs.gacc_dir(g), "confirmed.csv")
    rows = []
    if os.path.exists(cpath):
        with open(cpath, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("date")]
    key = lambda r: (r["date"], r["psa"].upper(), r["trigger"].upper())
    gone = {key(r) for r in rem}
    keep = {key(r): r for r in rows if key(r) not in gone}
    for r in add:
        keep[key(r)] = r
    if add or rem:
        with open(cpath, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "psa", "trigger", "notes"], extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(keep.values(), key=key))

    cfg.update({"criteria_sets": sets, "schedule": sched, "typical_peak_max_f": dict(sorted(tpm.items())),
                "typical_max": typical, "erc_fuel_model": fm})
    gaccs.save_config(g, cfg)

    today = date.today().isoformat()
    active = next((e["set"] for e in reversed(sched) if e["from"] <= today), sched[0]["set"])
    lines = [f"**Applied to {cfg.get('name', g)}** by @{author}. The website shows the changes within a few minutes.", "",
             f"- In use today: **{active}** ({', '.join(t['letter'] + ' ' + t['name'] for t in sets[active]['triggers'])})",
             f"- Sets: {', '.join(sets)}",
             "- Schedule: " + "; ".join(f"{e['set']} from {e['from']}" for e in sched)]
    if add:
        lines.append("- Confirmed days added: " + "; ".join(f"{r['date']} {r['psa']} {r['trigger']}" for r in add))
    if rem:
        lines.append("- Confirmed days removed: " + "; ".join(f"{r['date']} {r['psa']} {r['trigger']}" for r in rem))
    if "editors" in sub and admin:
        lines.append("- Editors: " + (", ".join("@" + e for e in cfg["editors"]) or "none"))
    with open(RESULT, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"GACC={g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
