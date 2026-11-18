#!/usr/bin/env python3
"""
Archive the national 7-Day Significant Fire Potential forecast, split by GACC.

Pulls Day 1 through Day 7 from the NPSG outlooks_forecast map service, keeps every PSA,
and appends one row per PSA per lead day to data/<GACC>/forecasts.csv. A raw snapshot of
each pull is saved to data/raw/<issue date>.json.

The forecast is issued in the morning, so a pull before --issued-by-hour (Mountain time)
is the previous day's product and is saved under yesterday's issue date.

Usage:
    python scripts/archive_forecast.py                       # normal run
    python scripts/archive_forecast.py --force               # replace that issue date if already archived
    python scripts/archive_forecast.py --mock DIR --date D   # offline testing
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

SERVICE = "https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer"
LOCAL_TZ = ZoneInfo("America/Denver")
N_DAYS = 7
RAW_DIR = os.path.join(gaccs.DATA, "raw")
LOG_PATH = os.path.join(gaccs.DATA, "pull_log.csv")

COLUMNS = ["issue_date", "lead_day", "valid_date", "psa", "gacc", "dryness_code", "dryness", "high_risk",
           "trigger", "category", "isvalid", "symbol", "service_timestamp", "pulled_at_utc"]
DRYNESS = {0: "No Data", 1: "Little or No Risk", 2: "Low Risk", 3: "Moderate Risk"}


def fetch_layer(layer_id, retries=4):
    params = {"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json"}
    url = f"{SERVICE}/{layer_id}/query?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fire-7day-verification-archiver"})
            with urllib.request.urlopen(req, timeout=90) as r:
                payload = json.load(r)
            if "error" in payload:
                raise RuntimeError(f"service error: {payload['error']}")
            return [f.get("attributes", {}) for f in payload.get("features", [])]
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"layer {layer_id} failed after {retries} tries: {last}")


def load_mock(d, layer_id):
    with open(os.path.join(d, f"layer{layer_id}.json")) as fh:
        return [f.get("attributes", {}) for f in json.load(fh).get("features", [])]


def get_ci(attrs, *names):
    lower = {k.lower(): v for k, v in attrs.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return "" if ms in (None, "") else str(ms)


def trigger_of(high_risk, symbol):
    """Map label letter (W, B, L, or any GACC-specific letter) or fall back to the service type."""
    s = str(symbol or "").strip().upper()
    if high_risk and len(s) == 1 and s.isalpha():
        return s
    if s in ("W", "B", "L"):
        return s
    if high_risk == "IGNITION":
        return "L"
    if high_risk == "CRITICAL":
        return "WB"
    return ""


def category(dcode, trig):
    if trig:
        return "High Risk - " + {"W": "Wind", "B": "Burn Environment", "L": "Lightning", "WB": "Wind or Burn"}.get(trig, trig)
    return DRYNESS.get(dcode, "No Data")


def read_csv(p):
    if not os.path.exists(p):
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(p, rows):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rows.sort(key=lambda r: (r["issue_date"], int(r["lead_day"]), r["psa"]))
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_pull(issue, pulled, n, note):
    new = not os.path.exists(LOG_PATH)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["issue_date", "pulled_at_utc", "rows", "note"])
        w.writerow([issue, pulled, n, note])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--mock")
    ap.add_argument("--date")
    ap.add_argument("--not-before-hour", type=int, default=None)
    ap.add_argument("--issued-by-hour", type=int, default=7)
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(LOCAL_TZ)
    if args.not_before_hour is not None and local_now.hour < args.not_before_hour:
        print(f"Local time {local_now:%H:%M %Z} is before {args.not_before_hour}:00; skipping this run.")
        return 0
    issue = date.fromisoformat(args.date) if args.date else local_now.date()
    if not args.date and local_now.hour < args.issued_by_hour:
        issue -= timedelta(days=1)
        print(f"Local time {local_now:%H:%M %Z} is before {args.issued_by_hour}:00, so this is the {issue} forecast.")
    issue_s, pulled = issue.isoformat(), now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    done = {r["issue_date"] for r in read_csv(LOG_PATH) if r.get("rows") not in (None, "", "0")}
    if issue_s in done and not args.force:
        print(f"{issue_s} already archived; keeping the first pull. Use --force to replace.")
        return 0

    by_gacc, raw, gacc_names = defaultdict(list), {"issue_date": issue_s, "pulled_at_utc": pulled, "layers": {}}, {}
    for layer_id in range(N_DAYS):
        lead = layer_id + 1
        feats = load_mock(args.mock, layer_id) if args.mock else fetch_layer(layer_id)
        raw["layers"][f"day{lead}"] = feats
        seen = set()
        for a in feats:
            psa = str(get_ci(a, "nat_code") or "").upper()
            if not psa or psa in seen:          # multipart polygons repeat a PSA
                continue
            seen.add(psa)
            g = gaccs.gacc_of(psa)
            gacc_names.setdefault(g, get_ci(a, "gacc", "gacc_name") or "")
            try:
                dcode = int(get_ci(a, "drynesscode"))
            except (TypeError, ValueError):
                dcode = 0
            hr = str(get_ci(a, "type") or "").strip().upper()
            hr = hr if hr in ("CRITICAL", "IGNITION") else ""
            sym = get_ci(a, "symbol") or ""
            trig = trigger_of(hr, sym)
            by_gacc[g].append({
                "issue_date": issue_s, "lead_day": lead, "valid_date": (issue + timedelta(days=lead - 1)).isoformat(),
                "psa": psa, "gacc": g, "dryness_code": dcode, "dryness": DRYNESS.get(dcode, "No Data"),
                "high_risk": hr, "trigger": trig, "category": category(dcode, trig),
                "isvalid": get_ci(a, "isvalid") if get_ci(a, "isvalid") is not None else "", "symbol": sym,
                "service_timestamp": ms_to_iso(get_ci(a, "timestampdate")), "pulled_at_utc": pulled,
            })

    total = sum(len(v) for v in by_gacc.values())
    if not total:
        log_pull(issue_s, pulled, 0, "no PSAs returned")
        print("ERROR: the service returned no PSAs", file=sys.stderr)
        return 1

    os.makedirs(RAW_DIR, exist_ok=True)
    with open(os.path.join(RAW_DIR, f"{issue_s}.json"), "w") as fh:
        json.dump(raw, fh, separators=(",", ":"))
    for g, rows in by_gacc.items():
        gaccs.load_config(g, gacc_field=gacc_names.get(g, ""))   # creates default settings for a new GACC
        p = os.path.join(gaccs.gacc_dir(g), "forecasts.csv")
        keep = [r for r in read_csv(p) if r["issue_date"] != issue_s]
        write_csv(p, keep + rows)
    gaccs.update_index({g: [r["psa"] for r in rows] for g, rows in by_gacc.items()})
    log_pull(issue_s, pulled, total, "forced replace" if args.force else "ok")
    print(f"Archived {issue_s}: " + ", ".join(f"{g} {len({r['psa'] for r in rows})} PSAs" for g, rows in sorted(by_gacc.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
