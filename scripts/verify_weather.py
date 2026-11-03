#!/usr/bin/env python3
"""
Check observed weather against each GACC's own High Risk trigger definitions, PSA by PSA.

Each GACC (config/gaccs/<CODE>.json) defines its triggers in named criteria sets, and a
schedule says which set is in use from which date. A trigger is either:
  * "auto"   - checked here from RAWS (wind / gusts / RH / temperature above typical / ERC)
               and the nearest afternoon sounding (high-elevation Haines), or
  * "manual" - confirmed by hand on the website (data/<CODE>/confirmed.csv), like Lightning.

A PSA meets an auto trigger when enough of its stations meet every station condition (not the
PSA average), plus any PSA-level condition (Haines). Each GACC's "day" runs midnight to
midnight in that GACC's time zone. Results: data/<CODE>/observed.csv.

Data sources (public, no login): FEMS climatology GraphQL API (RAWS hourly + NFDRS ERC),
Iowa Environmental Mesonet upper-air archive (00Z soundings), NPSG service (PSA boundaries).

Usage:
    python scripts/verify_weather.py                  # check unchecked days in the last 10, every GACC
    python scripts/verify_weather.py --gacc SW        # one GACC only
    python scripts/verify_weather.py --rescore        # recompute saved days with current settings
    MOCK_DIR=path python scripts/verify_weather.py    # offline testing
"""

import argparse
import csv
import gzip
import json
import math
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

DATA = gaccs.DATA
PSA_GEO = os.path.join(DATA, "psa_polygons.geojson")
RAWS_LIST = os.path.join(DATA, "stations_raws.csv")
MOCK_DIR = os.environ.get("MOCK_DIR")
PSA_SERVICE = "https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer/0/query"
FEMS_GQL = "https://fems.fs2c.usda.gov/api/climatology/graphql"
IEM = "https://mesonet.agron.iastate.edu"
REFRESH_DAYS = 30

# Upper-air sites (IEM ids) with approximate locations; the nearest one with data is used per PSA.
SOUNDINGS = {
    "KUIL": (47.94, -124.55), "KSLE": (44.91, -123.00), "KOTX": (47.68, -117.63), "KMFR": (42.37, -122.87),
    "KOAK": (37.73, -122.22), "KVBG": (34.75, -120.57), "KNKX": (32.85, -117.12), "KREV": (39.57, -119.80),
    "KBOI": (43.57, -116.21), "KLKN": (40.86, -115.74), "KSLC": (40.77, -111.95), "KVEF": (36.05, -115.18),
    "KFGZ": (35.23, -111.82), "KTUS": (32.23, -110.96), "KABQ": (35.04, -106.62), "KEPZ": (31.87, -106.70),
    "KGJT": (39.12, -108.53), "KDNR": (39.77, -104.87), "KRIW": (43.06, -108.48), "KTFX": (47.46, -111.38),
    "KGGW": (48.21, -106.63), "KUNR": (44.07, -103.21), "KBIS": (46.77, -100.75), "KABR": (45.46, -98.41),
    "KLBF": (41.13, -100.68), "KDDC": (37.76, -99.97), "KAMA": (35.23, -101.71), "KMAF": (31.95, -102.19),
    "KDRT": (29.37, -100.92), "KOUN": (35.18, -97.44), "KFWD": (32.83, -97.30), "KCRP": (27.78, -97.51),
    "KBRO": (25.92, -97.42), "KTOP": (39.07, -95.63), "KOAX": (41.32, -96.37), "KMPX": (44.85, -93.57),
    "KINL": (48.57, -93.38), "KGRB": (44.48, -88.13), "KDVN": (41.61, -90.58), "KILX": (40.15, -89.34),
    "KSGF": (37.24, -93.40), "KLZK": (34.84, -92.26), "KSHV": (32.45, -93.84), "KLCH": (30.13, -93.22),
    "KLIX": (30.34, -89.83), "KJAN": (32.32, -90.08), "KBMX": (33.17, -86.77), "KBNA": (36.25, -86.56),
    "KFFC": (33.36, -84.57), "KTLH": (30.45, -84.30), "KJAX": (30.48, -81.70), "KTBW": (27.70, -82.40),
    "KMFL": (25.75, -80.38), "KKEY": (24.55, -81.79), "KXMR": (28.48, -80.55), "KCHS": (32.90, -80.03),
    "KMHX": (34.78, -76.88), "KGSO": (36.10, -79.94), "KRNK": (37.21, -80.41), "KIAD": (38.98, -77.49),
    "KWAL": (37.93, -75.47), "KOKX": (40.87, -72.86), "KALB": (42.69, -73.83), "KBUF": (42.94, -78.73),
    "KPIT": (40.53, -80.22), "KILN": (39.42, -83.82), "KDTX": (42.70, -83.47), "KAPX": (44.91, -84.72),
    "KGYX": (43.89, -70.26), "KCAR": (46.87, -68.02), "KCHH": (41.67, -69.97),
    "PANC": (61.16, -150.00), "PAFA": (64.82, -147.88), "PAMC": (62.96, -155.61), "PAOM": (64.50, -165.43),
}

OUT_COLS = ["date", "psa", "criteria_set", "raws_reporting", "stations_needed", "observed", "possible",
            "trigger_hits", "hit_stations", "haines", "sounding", "max_temp_f", "min_rh", "max_wind_mph",
            "max_gust_mph", "erc", "erc_stations", "dry_stations", "sources_missing", "checked_at_utc"]


# ------------------------------------------------------------------ http
def http(url, body=None, retries=3, timeout=120):
    if MOCK_DIR:
        return mock_http(url, body)
    last = None
    for i in range(retries):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": "fire-7day-verification (github actions)",
                "Content-Type": "application/json", "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(4 * (i + 1))
    raise RuntimeError(f"{url[:120]} failed: {last}")


def mock_http(url, body):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "outlooks_forecast" in url:
        name = "psa.geojson"
    elif "fems" in url:
        t = body["query"]
        if "stationMetaData" in t:
            st = t.split('stateId: "')[1][:2]
            name = f"fems_stations_{st}.json"
        elif "nfdrsObs" in t:
            start = t.split('startDateRange: "')[1][:10]
            name = f"fems_nfdr_{start}.json"
        else:
            start = t.split('startDateTimeRange: "')[1][:13]
            name = f"fems_obs_{start}.json"
    elif "raob" in url:
        name = f"raob_{q['station'][0]}_{q['ts'][0]}.json"
    else:
        raise RuntimeError("no mock for " + url)
    p = os.path.join(MOCK_DIR, name)
    if not os.path.exists(p):
        raise RuntimeError(f"mock missing: {name}")
    with open(p) as fh:
        return fh.read()


def gql(query):
    payload = json.loads(http(FEMS_GQL, {"query": query}))
    if payload.get("errors"):
        raise RuntimeError(f"FEMS error: {str(payload['errors'])[:300]}")
    return payload["data"]


# ------------------------------------------------------------------ geometry
def point_in_ring(x, y, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def polys_of(geom):
    return geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]


def bbox(geom):
    xs = [p[0] for poly in polys_of(geom) for p in poly[0]]
    ys = [p[1] for poly in polys_of(geom) for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


def make_locator(psas):
    boxes = {k: bbox(g) for k, g in psas.items()}

    def locate(lat, lon):
        for code, g in psas.items():
            x0, y0, x1, y1 = boxes[code]
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            for poly in polys_of(g):
                if poly and point_in_ring(lon, lat, poly[0]) and not any(point_in_ring(lon, lat, h) for h in poly[1:]):
                    return code
        return None
    return locate


def centroid(geom):
    pts = [p for poly in polys_of(geom) for p in poly[0]]
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def dist_km(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def stale(p):
    return not os.path.exists(p) or time.time() - os.path.getmtime(p) > REFRESH_DAYS * 86400


# ------------------------------------------------------------------ reference data
def thin(ring, step=0.004):
    out = []
    for x, y in ring:
        p = [round(x, 3), round(y, 3)]
        if not out or abs(p[0] - out[-1][0]) + abs(p[1] - out[-1][1]) >= step:
            out.append(p)
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out


def load_psas():
    if stale(PSA_GEO):
        params = {"where": "1=1", "outFields": "nat_code", "returnGeometry": "true", "maxAllowableOffset": "0.005",
                  "outSR": "4326", "f": "geojson"}
        gj = json.loads(http(PSA_SERVICE + "?" + urllib.parse.urlencode(params)))
        merged = defaultdict(list)
        for f in gj.get("features", []):
            props = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
            code, g = str(props.get("nat_code") or "").upper(), f.get("geometry")
            if code and g:
                merged[code].extend([[thin(r) for r in poly] for poly in polys_of(g)])
        if not merged:
            raise RuntimeError("no PSA polygons returned")
        feats = [{"type": "Feature", "properties": {"psa": k, "gacc": gaccs.gacc_of(k)},
                  "geometry": {"type": "MultiPolygon", "coordinates": v}} for k, v in sorted(merged.items())]
        with open(PSA_GEO, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": feats}, fh, separators=(",", ":"))
        for g in {f["properties"]["gacc"] for f in feats}:      # a smaller file per GACC for its own map
            with open(os.path.join(gaccs.gacc_dir(g), "psa_polygons.geojson"), "w") as fh:
                json.dump({"type": "FeatureCollection", "features": [f for f in feats if f["properties"]["gacc"] == g]}, fh, separators=(",", ":"))
    with open(PSA_GEO) as fh:
        return {f["properties"]["psa"]: f["geometry"] for f in json.load(fh)["features"]}


def load_raws(psas):
    if stale(RAWS_LIST):
        locate, rows = make_locator(psas), {}
        for st in gaccs.STATES:
            page = 0
            while True:
                q = f'''query {{ stationMetaData(stateId: "{st}", page: {page}, per_page: 1000) {{
                    _metadata {{ page page_count total_count }}
                    data {{ station_id station_name latitude longitude elevation state }} }} }}'''
                try:
                    d = gql(q)["stationMetaData"]
                except Exception as e:
                    print(f"  stations {st}: {e}")
                    break
                for s in d.get("data") or []:
                    try:
                        lat, lon = float(s["latitude"]), float(s["longitude"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    psa = locate(lat, lon)
                    if psa:
                        rows[str(s["station_id"])] = {"station_id": s["station_id"], "name": s.get("station_name", ""),
                                                      "lat": lat, "lon": lon, "elev_ft": s.get("elevation") or "",
                                                      "state": s.get("state", st), "psa": psa, "gacc": gaccs.gacc_of(psa)}
                page += 1
                if page >= int((d.get("_metadata") or {}).get("page_count") or 1):
                    break
        if not rows:
            raise RuntimeError("no RAWS stations found inside the PSAs")
        write_csv(RAWS_LIST, list(rows.values()), ["station_id", "name", "lat", "lon", "elev_ft", "state", "psa", "gacc"])
    return read_csv(RAWS_LIST)


# ------------------------------------------------------------------ daily data
def local_day_utc(d, tz):
    s = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    return s, s + timedelta(days=1) - timedelta(seconds=1)


def paged(query_fn, key):
    out, page = [], 0
    while True:
        d = gql(query_fn(page))[key]
        out.extend(d.get("data") or [])
        page += 1
        if page >= int((d.get("_metadata") or {}).get("page_count") or 1):
            return out


def fetch_raws_obs(d, tz, ids):
    s, e = local_day_utc(d, tz)
    out = defaultdict(list)       # station -> [[hour_local, temp, rh, wind, gust], ...]
    for i in range(0, len(ids), 60):
        chunk = ",".join(str(x) for x in ids[i:i + 60])
        rows = paged(lambda p: f'''query {{ weatherObs(attribute: "temperature",
            startDateTimeRange: "{s:%Y-%m-%dT%H:%M:%SZ}", endDateTimeRange: "{e:%Y-%m-%dT%H:%M:%SZ}",
            stationIds: "{chunk}", timeZone: "UTC", page: {p}, per_page: 5000) {{
            _metadata {{ page page_count total_count }}
            data {{ station_id observation_time temperature relative_humidity wind_speed peak_gust_speed }} }} }}''', "weatherObs")
        for o in rows:
            try:
                hr = datetime.fromisoformat(str(o.get("observation_time")).replace("Z", "+00:00")).astimezone(tz).hour
            except ValueError:
                continue
            out[str(o.get("station_id"))].append([hr, num(o.get("temperature")), num(o.get("relative_humidity")),
                                                  num(o.get("wind_speed")), num(o.get("peak_gust_speed"))])
    return dict(out)


def fetch_erc(d, ids, fuel_model):
    out = {}
    for i in range(0, len(ids), 60):
        chunk = ",".join(str(x) for x in ids[i:i + 60])
        rows = paged(lambda p: f'''query {{ nfdrsObs(stationIds: "{chunk}", fuelModels: "{fuel_model}", nfdrType: "O",
            startDateRange: "{d.isoformat()}", endDateRange: "{d.isoformat()}", startHour: 0, endHour: 24,
            dateTimeFormat: "LocalStationTime", page: {p}, per_page: 5000) {{
            _metadata {{ page page_count total_count }}
            data {{ station_id nfdr_date energy_release_component }} }} }}''', "nfdrsObs")
        for r in rows:
            v = num(r.get("energy_release_component"))
            if v is not None and 0 <= v < 200:
                sid = str(r.get("station_id"))
                out[sid] = max(out.get(sid, v), v)
    return out


def haines_high(profile):
    lv = {}
    for p in profile:
        try:
            pr = round(float(p.get("pres")))
        except (TypeError, ValueError):
            continue
        if pr in (700, 500) and p.get("tmpc") is not None:
            lv[pr] = p
    if 700 not in lv or 500 not in lv or lv[700].get("dwpc") is None:
        return None
    a = float(lv[700]["tmpc"]) - float(lv[500]["tmpc"])
    b = float(lv[700]["tmpc"]) - float(lv[700]["dwpc"])
    return (1 if a <= 17 else 2 if a <= 21 else 3) + (1 if b <= 14 else 2 if b <= 20 else 3)


_HAINES_CACHE = {}


def fetch_haines(d):
    """00Z the next UTC day (late afternoon/evening across the US)."""
    if d in _HAINES_CACHE:
        return _HAINES_CACHE[d]
    ts = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)).strftime("%Y%m%d%H%M")
    out = {}
    for sid in SOUNDINGS:
        try:
            j = json.loads(http(f"{IEM}/json/raob.py?" + urllib.parse.urlencode({"ts": ts, "station": sid}), retries=2, timeout=40))
            profs = j.get("profiles") or []
            h = haines_high(profs[0].get("profile") or []) if profs else None
            if h is not None:
                out[sid] = h
        except Exception:
            pass
    _HAINES_CACHE[d] = out
    return out


# ------------------------------------------------------------------ evaluation
def num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def typical_max(cfg, psa, elev):
    over = {k: v for k, v in (cfg.get("typical_peak_max_f") or {}).items() if not k.startswith("_")}
    if over.get(psa) not in (None, ""):
        return float(over[psa])
    tm = cfg.get("typical_max") or {}
    try:
        e = float(elev)
    except (TypeError, ValueError):
        e = 5000.0
    return float(tm.get("base_f_at_1000ft", 98)) - float(tm.get("drop_per_1000ft", 3.5)) * (e - 1000.0) / 1000.0


def station_summary(hours, aft):
    s = {"maxt": None, "minrh": None, "maxws": None, "maxg": None}
    for hr, t, rh, ws, g in hours:
        if rh is not None and not 0 <= rh <= 100:
            rh = None
        a = aft["start"] <= hr <= aft["end"]
        if a and t is not None and t < 135:
            s["maxt"] = t if s["maxt"] is None else max(s["maxt"], t)
        if a and rh is not None:
            s["minrh"] = rh if s["minrh"] is None else min(s["minrh"], rh)
        if ws is not None and ws < 120:
            s["maxws"] = ws if s["maxws"] is None else max(s["maxws"], ws)
        if g is not None and g < 150:
            s["maxg"] = g if s["maxg"] is None else max(s["maxg"], g)
    return s


def station_hits(trig, hours, summ, typ, erc):
    """True if this station meets every station-level condition of an auto trigger."""
    used = False
    w = trig.get("wind")
    rhmax = trig.get("rh_max")
    if w:
        used = True
        sus = gust = 0
        for hr, t, rh, ws, g in hours:
            if rhmax is not None and (rh is None or rh > rhmax):
                continue
            if w.get("sustained_mph") is not None and ws is not None and ws >= w["sustained_mph"]:
                sus += 1
            if w.get("gust_mph") is not None and g is not None and g >= w["gust_mph"]:
                gust += 1
        if not (sus >= 1 or gust >= int(w.get("gust_hours") or 1)):
            return False
    elif rhmax is not None:
        used = True
        if summ["minrh"] is None or summ["minrh"] > rhmax:
            return False
    if trig.get("temp_above_typical") is not None:
        used = True
        if summ["maxt"] is None or summ["maxt"] < typ + float(trig["temp_above_typical"]):
            return False
    if trig.get("erc_min") is not None:
        used = True
        if erc is None or erc < float(trig["erc_min"]):
            return False
    return used


def evaluate(g, cfg, d, saved, stations, psas):
    set_name, cs = gaccs.criteria_for(d, cfg)
    SN, aft = cs.get("stations_needed") or {}, cfg.get("afternoon_hours") or {"start": 10, "end": 20}
    autos = [t for t in cs.get("triggers", []) if t.get("method") == "auto"]
    meta = {str(s["station_id"]): s for s in stations}
    obs, ercs = saved.get("raws") or {}, saved.get("erc") or {}
    per = defaultdict(list)
    for sid, hours in obs.items():
        m = meta.get(sid)
        if m:
            per[m["psa"]].append((m, hours, station_summary(hours, aft)))
    rows = []
    for psa in sorted(p for p in psas if gaccs.gacc_of(p) == g):
        sts, n = per.get(psa, []), len(per.get(psa, []))
        need = int(SN.get("small_psa_needed", 1)) if 0 < n <= int(SN.get("small_psa_station_count", 3)) else int(SN.get("default", 2))
        hn, snd = None, ""
        if saved.get("haines"):
            cx, cy = centroid(psas[psa])
            best = min((k for k in saved["haines"] if k in SOUNDINGS), key=lambda k: dist_km(cy, cx, *SOUNDINGS[k]), default=None)
            if best:
                hn, snd = saved["haines"][best], best
        observed, possible, counts, names = "", "", [], []
        for t in autos:
            hit = [m["name"] for m, hours, s in sts if station_hits(t, hours, s, typical_max(cfg, psa, m.get("elev_ft")), ercs.get(str(m["station_id"])))]
            counts.append(f"{t['letter']}={len(hit)}")
            if hit:
                names.append(f"{t['letter']}: " + "; ".join(hit))
            if n and len(hit) >= need:
                if t.get("haines_min") is None or (hn is not None and hn >= float(t["haines_min"])):
                    observed += t["letter"]
                elif hn is None:
                    possible += t["letter"] + "?"
        vals = lambda k: [s[k] for _, _, s in sts if s[k] is not None]
        e = sorted(ercs[str(m["station_id"])] for m, _, _ in sts if str(m["station_id"]) in ercs)
        rows.append({
            "date": d.isoformat(), "psa": psa, "criteria_set": set_name, "raws_reporting": n, "stations_needed": need,
            "observed": observed, "possible": possible, "trigger_hits": ";".join(counts), "hit_stations": " | ".join(names),
            "haines": "" if hn is None else hn, "sounding": snd,
            "max_temp_f": max(vals("maxt"), default=""), "min_rh": min(vals("minrh"), default=""),
            "max_wind_mph": max(vals("maxws"), default=""), "max_gust_mph": max(vals("maxg"), default=""),
            "erc": "" if not e else round((e[len(e) // 2] + e[(len(e) - 1) // 2]) / 2, 1), "erc_stations": len(e),
            "dry_stations": sum(1 for v in vals("minrh") if v < float(cfg.get("dry_rh_context", 20))),
            "sources_missing": " ".join(saved.get("missing", [])),
            "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return rows


# ------------------------------------------------------------------ io
def read_csv(p):
    if not os.path.exists(p):
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(p, rows, cols):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log(g, d, msg):
    p = os.path.join(gaccs.gacc_dir(g), "verify_log.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "run_utc", "message"])
        w.writerow([d, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg])


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--gacc", help="only this GACC (e.g. SW)")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()

    psas = load_psas()
    stations = load_raws(psas)
    codes = sorted({gaccs.gacc_of(p) for p in psas})
    if args.gacc:
        codes = [c for c in codes if c == args.gacc.upper()]
    print(f"{len(psas)} PSAs in {len(codes)} GACCs, {len(stations)} RAWS")
    failures = 0

    for g in codes:
        cfg = gaccs.load_config(g)
        tz = ZoneInfo(cfg.get("timezone", "America/Denver"))
        gdir = gaccs.gacc_dir(g)
        raw_dir = os.path.join(gdir, "obs_raw")
        os.makedirs(raw_dir, exist_ok=True)
        out_path = os.path.join(gdir, "observed.csv")
        existing = read_csv(out_path)
        results = defaultdict(list)
        for r in existing:
            results[r["date"]].append(r)
        today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
        if args.rescore:
            todo = sorted(f[:-8] for f in os.listdir(raw_dir) if f.endswith(".json.gz"))
        else:
            todo = [(today - timedelta(days=k)).isoformat() for k in range(args.days, 0, -1)]
            todo = [d for d in todo if d not in results]
        g_st = [s for s in stations if s.get("gacc") == g]
        ids = [s["station_id"] for s in g_st]
        for ds in todo:
            d = date.fromisoformat(ds)
            rp = os.path.join(raw_dir, f"{ds}.json.gz")
            if os.path.exists(rp):
                with gzip.open(rp, "rt") as fh:
                    saved = json.load(fh)
            else:
                if not ids:
                    continue
                saved, missing = {}, []
                try:
                    saved["raws"] = fetch_raws_obs(d, tz, ids)
                except Exception as e:
                    print(f"{g} {ds} RAWS: {e}")
                    saved["raws"] = {}
                if not saved["raws"]:
                    log(g, ds, "no RAWS data; will retry next run")
                    failures += 1
                    continue
                try:
                    saved["erc"] = fetch_erc(d, ids, cfg.get("erc_fuel_model", "Y"))
                except Exception as e:
                    print(f"{g} {ds} ERC: {e}")
                    saved["erc"] = {}
                    missing.append("erc")
                saved["haines"] = fetch_haines(d)
                if not saved["haines"]:
                    missing.append("soundings")
                saved["missing"] = missing
                with gzip.open(rp, "wt") as fh:
                    json.dump(saved, fh, separators=(",", ":"))
            rows = evaluate(g, cfg, d, saved, stations, psas)
            results[ds] = rows
            met = " ".join(f"{r['psa']}:{r['observed']}" for r in rows if r["observed"]) or "none"
            log(g, ds, f"ok; criteria={rows[0]['criteria_set'] if rows else ''}; met={met}")
            print(f"{g} {ds}: {len(rows)} PSAs; met {met}")
        write_csv(out_path, [r for k in sorted(results) for r in sorted(results[k], key=lambda x: x["psa"])], OUT_COLS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
