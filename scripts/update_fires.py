#!/usr/bin/env python3
"""
Record significant fire GROWTH days from the national WFIGS incident feed (NIFC).

The 7-Day product forecasts the chance of a new large fire or significant growth on existing fires
on a given day, so a fire "event" here is a fire-day: any wildfire that grew by at least
fire_min_acres (default 100) on that day, counted in the PSA where the fire started.

How growth is measured
  * Each nightly run (about 3 am) reads every active wildfire's current size from WFIGS and treats
    it as the size at the end of the previous day ("as of" day D, in the GACC's time zone).
  * Growth on day D = size at end of D - size at end of the day before (kept in
    data/<GACC>/fire_tracking.csv between runs).
  * A new fire discovered on D (or the day before, if it was reported late) counts its whole size as
    growth on its discovery day, which covers a new large fire's initial run.
  * If a run was missed, growth since the last check is put on D and the notes say so.
  * Fire sizes are only as current as the incident reports (IRWIN / ICS-209); a late report shifts
    growth to the next day.
  * Growth can only be measured from the first night this runs; earlier days can't be rebuilt.

Results go to data/<GACC>/fires.csv (one row per fire per day of growth). Rows typed by hand (no
source_id) are never changed. Type "exclude" in a row's notes to leave it out (it stays out).
Prescribed fires are skipped.

Also counts new wildfire starts per PSA per day (lightning / human / other) into
data/<GACC>/starts.csv, recounting the last week each night to catch late reports.

Usage: python scripts/update_fires.py [--today YYYY-MM-DD]     (MOCK_DIR=path reads wfigs.json)
"""

import argparse
import json
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_weather as vw  # noqa: E402  (PSA boundaries, point-in-PSA lookup, http with retries, csv io)

SVC = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/{}/FeatureServer/0/query"
LAYERS = ["WFIGS_Incident_Locations_Current", "WFIGS_Incident_Locations_YearToDate"]
COLS = ["date", "psa", "fire_name", "acres", "cause", "notes", "source_id", "total_acres"]
TRACK_COLS = ["source_id", "fire_name", "psa", "discovered", "prev_date", "prev_acres", "last_date", "last_acres"]
SIZE_FIELDS = ["IncidentSize", "DailyAcres", "CalculatedAcres", "FinalAcres", "DiscoveryAcres"]
START_COLS = ["date", "psa", "starts", "lightning", "human", "other"]
RECOUNT_DAYS = 7          # new starts are recounted for the last week each night, to catch late reports
DEFAULT_MIN = 100
KEEP_TRACK_DAYS = 90


def query(layer, where):
    """All features from a WFIGS layer matching where (paged), or None if the service refuses it."""
    feats, offset = [], 0
    while True:
        q = {"where": where, "outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json",
             "resultOffset": offset, "resultRecordCount": 2000, "orderByFields": "OBJECTID"}
        try:
            j = json.loads(vw.http(SVC.format(layer) + "?" + urllib.parse.urlencode(q), retries=3, timeout=120))
        except Exception as e:
            print(f"  {layer} ({where}): {e}")
            return None
        if "error" in j:
            print(f"  {layer} ({where}): {j['error'].get('message')}")
            return None
        batch = j.get("features") or []
        feats += batch
        if not batch or not j.get("exceededTransferLimit"):
            return feats
        offset += len(batch)


def mock(name, fallback):
    p = os.path.join(vw.MOCK_DIR, name)
    with open(p if os.path.exists(p) else os.path.join(vw.MOCK_DIR, fallback)) as fh:
        return json.load(fh).get("features", [])


def fetch():
    """Every active (or, failing that, year-to-date) wildfire incident with a location."""
    if vw.MOCK_DIR:
        return mock("wfigs.json", "wfigs.json")
    for layer in LAYERS:
        feats = query(layer, "1=1" if layer.endswith("Current") else "IncidentSize >= 1")
        if feats is not None:
            print(f"WFIGS {layer}: {len(feats)} incidents")
            return feats
    raise RuntimeError("couldn't read the WFIGS incident service")


def fetch_starts(today):
    """Wildfires discovered in the last RECOUNT_DAYS+1 days (any size, including ones already out)."""
    if vw.MOCK_DIR:
        return mock("wfigs_starts.json", "wfigs.json")
    since = (today - timedelta(days=RECOUNT_DAYS + 1)).isoformat()
    for where in [f"FireDiscoveryDateTime >= TIMESTAMP '{since} 00:00:00'", f"FireDiscoveryDateTime >= DATE '{since}'", "1=1"]:
        feats = query(LAYERS[1], where)
        if feats is not None:
            print(f"WFIGS new starts since {since}: {len(feats)} incidents")
            return feats
    raise RuntimeError("couldn't read new fire starts from WFIGS")


def first(a, *keys):
    for k in keys:
        if a.get(k) not in (None, ""):
            return a[k]
    return None


def acres_of(a):
    vals = []
    for k in SIZE_FIELDS:
        try:
            if a.get(k) not in (None, ""):
                vals.append(float(a[k]))
        except (TypeError, ValueError):
            pass
    return max(vals) if vals else None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def incidents(feats, locate):
    """{source_id: {...}} for wildfires with a size, discovery time and a PSA."""
    out = {}
    for f in feats:
        a = f.get("attributes") or {}
        if str(a.get("IncidentTypeCategory") or "WF").upper() in ("RX", "CX"):
            continue
        acres, disc = acres_of(a), first(a, "FireDiscoveryDateTime", "CreatedOnDateTime_dt", "CreatedOnDateTime")
        g = f.get("geometry") or {}
        lon, lat = g.get("x"), g.get("y")
        if lon is None or lat is None:
            lat, lon = first(a, "InitialLatitude", "POOLatitude"), first(a, "InitialLongitude", "POOLongitude")
        if acres is None or disc is None or lat is None or lon is None:
            continue
        psa = locate(float(lat), float(lon))
        if not psa:
            continue
        sid = str(first(a, "UniqueFireIdentifier", "IrwinID", "GlobalID", "OBJECTID"))
        name = str(first(a, "IncidentName") or "Unnamed").strip()
        out[sid] = {"sid": sid, "psa": psa, "acres": acres, "disc_ms": float(disc),
                    "name": name.title() if name.isupper() else name, "cause": str(first(a, "FireCause") or "").strip()}
    return out


def update_area(label, fires_path, track_path, fires_now, tz, min_acres, today):
    as_of = (today - timedelta(days=1)).isoformat()          # the run at ~3 am measures the day that just ended
    track = {r["source_id"]: r for r in vw.read_csv(track_path)}
    rows = vw.read_csv(fires_path)
    hand = [r for r in rows if not (r.get("source_id") or "").strip()]
    # rows from the earlier version (fire start days, no total_acres) are replaced by growth days
    auto = {(r["source_id"], r["date"]): r for r in rows if (r.get("source_id") or "").strip() and (r.get("total_acres") or "").strip()}
    excluded = {k for k, r in auto.items() if "exclude" in (r.get("notes") or "").lower()}
    hand_keys = {(r.get("date"), (r.get("psa") or "").upper(), (r.get("fire_name") or "").strip().lower()) for r in hand}
    events = 0

    def put_event(f, day, growth, note):
        nonlocal events
        key = (f["sid"], day)
        if key in excluded or (day, f["psa"], f["name"].lower()) in hand_keys:
            return
        if growth >= min_acres:
            auto[key] = {"date": day, "psa": f["psa"], "fire_name": f["name"], "acres": int(round(growth)), "cause": f["cause"],
                         "notes": note,
                         "source_id": f["sid"], "total_acres": int(round(f["acres"]))}
            events += 1
        elif key in auto:                                       # recomputed below the limit (e.g. a second run that morning)
            del auto[key]

    for sid, f in fires_now.items():
        disc_day = datetime.fromtimestamp(f["disc_ms"] / 1000, tz=timezone.utc).astimezone(tz).date().isoformat()
        t = track.get(sid) or {"source_id": sid, "discovered": disc_day, "prev_date": "", "prev_acres": "", "last_date": "", "last_acres": ""}
        if t["last_date"] and t["last_date"] < as_of:            # first run for this day: yesterday's "last" becomes "prev"
            t["prev_date"], t["prev_acres"] = t["last_date"], t["last_acres"]
        prev_a, prev_d = fnum(t.get("prev_acres")), t.get("prev_date") or ""
        if prev_a is None and (date.fromisoformat(as_of) - timedelta(days=1)).isoformat() <= disc_day <= as_of:
            # new fire (discovered that day, or the day before and reported late): its whole size is growth on its discovery day.
            # A fire discovered after midnight is just tracked; the next run counts its first day.
            put_event(f, disc_day, f["acres"], f"new fire; {int(round(f['acres'])):,} ac by end of first report")
        if prev_a is not None and prev_d and prev_d < as_of:
            gap = (date.fromisoformat(as_of) - date.fromisoformat(prev_d)).days
            note = f"grew {int(round(f['acres'] - prev_a)):,} ac; {int(round(f['acres'])):,} ac total" + \
                   ("" if gap == 1 else f" (growth since {prev_d}; no check on the days between)")
            put_event(f, as_of, f["acres"] - prev_a, note)
        t.update({"fire_name": f["name"], "psa": f["psa"], "last_date": as_of, "last_acres": round(f["acres"], 1)})
        track[sid] = t

    cutoff = (today - timedelta(days=KEEP_TRACK_DAYS)).isoformat()
    track = {k: v for k, v in track.items() if (v.get("last_date") or "9999") >= cutoff}
    vw.write_csv(track_path, sorted(track.values(), key=lambda r: r["source_id"]), TRACK_COLS)
    out = sorted(hand + list(auto.values()), key=lambda r: (r.get("date", ""), (r.get("psa") or "").upper(), r.get("fire_name", "")))
    vw.write_csv(fires_path, out, COLS)
    print(f"{label}: {len(fires_now)} active wildfires tracked; {events} with {min_acres:g}+ ac growth on {as_of} "
          f"(or new); {len(hand)} rows typed by hand")


def start_list(feats, locate):
    """[(psa, discovery ms, cause group)] for every wildfire start with a location."""
    out = []
    for f in feats:
        a = f.get("attributes") or {}
        if str(a.get("IncidentTypeCategory") or "WF").upper() in ("RX", "CX"):
            continue
        disc = first(a, "FireDiscoveryDateTime", "CreatedOnDateTime_dt", "CreatedOnDateTime")
        g = f.get("geometry") or {}
        lon, lat = g.get("x"), g.get("y")
        if lon is None or lat is None:
            lat, lon = first(a, "InitialLatitude", "POOLatitude"), first(a, "InitialLongitude", "POOLongitude")
        if disc is None or lat is None or lon is None:
            continue
        psa = locate(float(lat), float(lon))
        if psa:
            c = str(first(a, "FireCause") or "").lower()
            out.append((psa, float(disc), "lightning" if c.startswith("natural") or "lightning" in c else "human" if c.startswith("human") else "other"))
    return out


def update_starts(label, path, starts, psa_codes, tz, today):
    days = [(today - timedelta(days=k)).isoformat() for k in range(RECOUNT_DAYS, 0, -1)]
    counts = {(d, p): {"date": d, "psa": p, "starts": 0, "lightning": 0, "human": 0, "other": 0} for d in days for p in psa_codes}
    for psa, ms, grp in starts:
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).date().isoformat()
        if (d, psa) in counts:
            counts[(d, psa)]["starts"] += 1
            counts[(d, psa)][grp] += 1
    keep = [r for r in vw.read_csv(path) if r.get("date") not in days]
    vw.write_csv(path, sorted(keep + list(counts.values()), key=lambda r: (r["date"], r["psa"])), START_COLS)
    yday = days[-1]
    n = sum(c["starts"] for c in counts.values() if c["date"] == yday)
    print(f"{label}: {n} new wildfire starts on {yday} ({RECOUNT_DAYS} days recounted)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()
    psas = vw.load_psas()
    feats = fetch()
    try:
        starts_feats = fetch_starts(date.fromisoformat(args.today) if args.today else date.today())
    except Exception as e:
        print(f"New starts not updated: {e}")
        starts_feats = None
    run_areas(psas, feats, starts_feats, args.today)
    return 0


# ------------------------------------------------------------------ national: one file per GACC
def run_areas(psas, feats, starts_feats, today_s):
    import gaccs
    locate = vw.make_locator(psas)
    allf = incidents(feats, locate)
    alls = start_list(starts_feats, locate) if starts_feats is not None else None
    for g in sorted({gaccs.gacc_of(p) for p in psas}):
        cfg = gaccs.load_config(g)
        tz = ZoneInfo(cfg.get("timezone", "America/Denver"))
        today = date.fromisoformat(today_s) if today_s else datetime.now(tz).date()
        gdir = gaccs.gacc_dir(g)
        update_area(g, os.path.join(gdir, "fires.csv"), os.path.join(gdir, "fire_tracking.csv"),
                    {k: v for k, v in allf.items() if gaccs.gacc_of(v["psa"]) == g}, tz,
                    float(cfg.get("fire_min_acres", DEFAULT_MIN) or DEFAULT_MIN), today)
        if alls is not None:
            update_starts(g, os.path.join(gdir, "starts.csv"), [x for x in alls if gaccs.gacc_of(x[0]) == g],
                          sorted(p for p in psas if gaccs.gacc_of(p) == g), tz, today)


if __name__ == "__main__":
    sys.exit(main())
