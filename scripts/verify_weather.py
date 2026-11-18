#!/usr/bin/env python3
"""
Check observed weather against each GACC's own High Risk trigger definitions, PSA by PSA.

Each GACC (config/gaccs/<CODE>.json) defines its triggers in named criteria sets, and a
schedule says which set is in use from which date. A trigger is either:
  * "auto"   - checked here from RAWS (wind / gusts / RH / temperature above typical / ERC), or
  * "manual" - confirmed by hand on the website (data/<CODE>/confirmed.csv), like Lightning.

A PSA meets an auto trigger when enough of its stations meet every station condition (not the
PSA average). Each GACC's "day" runs midnight to
midnight in that GACC's time zone. Results: data/<CODE>/observed.csv.

Data sources (public, no login): FEMS climatology GraphQL API (RAWS hourly + NFDRS ERC and BI),
Iowa Environmental Mesonet (ASOS/AWOS airport observations), NPSG service (PSA boundaries).

ASOS/AWOS sites inside a PSA are checked exactly like RAWS (wind, RH, temperature) and count toward
the stations needed; they have no ERC or BI. A GACC can turn them off with "use_asos": false.

Usage:
    python scripts/verify_weather.py                  # check unchecked days in the last 10, every GACC
    python scripts/verify_weather.py --gacc SW        # one GACC only
    python scripts/verify_weather.py --rescore        # recompute saved days with current settings
    MOCK_DIR=path python scripts/verify_weather.py    # offline testing
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys
import time
import urllib.error
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
ASOS_LIST = os.path.join(DATA, "stations_asos.csv")
IEM = "https://mesonet.agron.iastate.edu"
KT_TO_MPH = 1.15078
MOCK_DIR = os.environ.get("MOCK_DIR")
PSA_SERVICE = "https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer/0/query"
FEMS_GQL = "https://fems.fs2c.usda.gov/api/climatology/graphql"
REFRESH_DAYS = 30

ST_KEEP_DAYS = 120   # days of per-station results kept in data/<GACC>/station_obs.csv (the website's Weather data tab)
ST_COLS = ["date", "station_id", "name", "type", "psa", "hours", "max_temp_f", "min_rh", "max_wind_mph", "max_gust_mph",
           "erc", "bi", "typical_max_f", "triggers_hit"]
OUT_COLS = ["date", "psa", "criteria_set", "raws_reporting", "asos_reporting", "stations_needed", "observed",
            "trigger_hits", "hit_stations", "max_temp_f", "min_rh", "max_wind_mph",
            "max_gust_mph", "erc", "erc_stations", "bi", "bi_stations", "dry_stations", "sources_missing", "checked_at_utc"]


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
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:       # the request itself is wrong; retrying won't help
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    detail = ""
                raise RuntimeError(f"{url[:120]} failed: HTTP {e.code} {detail}".strip())
            last = e
            time.sleep(4 * (i + 1))
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
        elif "nfdrMinMax" in t or "nfdrsObs" in t:
            v = body.get("variables") or {}
            start = v.get("startDate") or v.get("startDateRange")
            name = f"fems_{'nfdrmm' if 'nfdrMinMax' in t else 'nfdr'}_{start}.json"
        else:
            start = t.split('startDateTimeRange: "')[1][:13]
            name = f"fems_obs_{start}.json"
    elif "geojson/network" in url:
        name = "iem_" + url.rsplit("/", 1)[1]
    elif "asos.py" in url:
        name = f"iem_asos_{q['year1'][0]}-{int(q['month1'][0]):02d}-{int(q['day1'][0]):02d}T{int(q['hour1'][0]):02d}.csv"
    else:
        raise RuntimeError("no mock for " + url)
    p = os.path.join(MOCK_DIR, name)
    if not os.path.exists(p):
        raise RuntimeError(f"mock missing: {name}")
    with open(p) as fh:
        return fh.read()


def gql(query, variables=None):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    payload = json.loads(http(FEMS_GQL, body))
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


def load_asos(psas):
    """ASOS/AWOS sites (Iowa Environmental Mesonet station networks) that sit inside a PSA."""
    if stale(ASOS_LIST):
        locate, rows = make_locator(psas), {}
        nets = [f"{st}_ASOS" for st in gaccs.STATES] + ["AWOS"]       # AWOS: Iowa's separate network
        for net in nets:
            try:
                j = json.loads(http(f"{IEM}/geojson/network/{net}.geojson", retries=2, timeout=60))
            except Exception as e:
                print(f"  ASOS list {net}: {e}")
                continue
            for f in j.get("features") or []:
                p, g = f.get("properties") or {}, f.get("geometry") or {}
                try:
                    lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                if str(p.get("online", True)).lower() in ("false", "0"):
                    continue
                psa = locate(lat, lon)
                sid = str(p.get("sid") or p.get("id") or "").strip()
                if psa and sid:
                    el = num(p.get("elevation"))
                    rows["ASOS-" + sid] = {"station_id": "ASOS-" + sid, "name": str(p.get("sname") or sid).title(), "lat": lat, "lon": lon,
                                           "elev_ft": "" if el is None else round(el * 3.28084), "state": net.split("_")[0][:2],
                                           "psa": psa, "gacc": gaccs.gacc_of(psa), "type": "ASOS"}
        if rows:
            write_csv(ASOS_LIST, list(rows.values()), ["station_id", "name", "lat", "lon", "elev_ft", "state", "psa", "gacc", "type"])
        elif not os.path.exists(ASOS_LIST):
            print("  no ASOS/AWOS stations found; continuing with RAWS only")
            return []
    return read_csv(ASOS_LIST) if os.path.exists(ASOS_LIST) else []


def fetch_asos_obs(d, tz, ids):
    """Hourly ASOS/AWOS values for the local day: one entry per local hour (max temp, min RH, max wind/gust in mph)."""
    s, e = local_day_utc(d, tz)
    e = e + timedelta(seconds=1)
    per = defaultdict(dict)                       # station -> hour -> [hr, t, rh, ws, g]
    sids = [i[5:] for i in ids]                   # strip "ASOS-"
    for i in range(0, len(sids), 40):
        q = [("station", x) for x in sids[i:i + 40]] + [("data", k) for k in ("tmpf", "relh", "sknt", "gust")] + [
            ("year1", s.year), ("month1", s.month), ("day1", s.day), ("hour1", s.hour), ("minute1", 0),
            ("year2", e.year), ("month2", e.month), ("day2", e.day), ("hour2", e.hour), ("minute2", 0),
            ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"), ("missing", "empty"),
            ("trace", "empty"), ("direct", "no"), ("report_type", 3), ("report_type", 4)]
        text = http(f"{IEM}/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q), retries=3, timeout=180)
        for row in csv.DictReader(io.StringIO(text)):
            try:
                hr = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(tz)
            except (KeyError, ValueError):
                continue
            if hr.date() != d:
                continue
            t, rh = num(row.get("tmpf")), num(row.get("relh"))
            ws, g = num(row.get("sknt")), num(row.get("gust"))
            ws = None if ws is None else round(ws * KT_TO_MPH, 1)
            g = None if g is None else round(g * KT_TO_MPH, 1)
            h = per["ASOS-" + row.get("station", "")].setdefault(hr.hour, [hr.hour, None, None, None, None])
            if t is not None:
                h[1] = t if h[1] is None else max(h[1], t)
            if rh is not None:
                h[2] = rh if h[2] is None else min(h[2], rh)
            if ws is not None:
                h[3] = ws if h[3] is None else max(h[3], ws)
            if g is not None:
                h[4] = g if h[4] is None else max(h[4], g)
    return {k: [v[h] for h in sorted(v)] for k, v in per.items()}


# ------------------------------------------------------------------ daily data
def local_day_utc(d, tz):
    s = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    return s, s + timedelta(days=1) - timedelta(seconds=1)


def paged(query_fn, key, variables=None):
    out, page = [], 0
    while True:
        v = dict(variables, page=page) if variables is not None else None
        d = gql(query_fn(page), v)[key]
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


# FEMS fire danger. The daily max query (nfdrMinMax) is tried first, then the hourly one (nfdrsObs).
# dateTimeFormat is a GraphQL enum, so values go in as variables, not typed into the query text.
NFDR_MINMAX = """query NfdrMinMax($startDate: Date!, $endDate: Date, $stationIds: String, $fuelModels: String,
  $hasHistoricData: TriState, $page: Int, $perPage: Int) {
  nfdrMinMax(startDate: $startDate, endDate: $endDate, stationIds: $stationIds, fuelModels: $fuelModels,
    hasHistoricData: $hasHistoricData, page: $page, per_page: $perPage) {
    _metadata { page page_count total_count }
    data { station_id summary_date nfdr_type energy_release_component_max burning_index_max } } }"""
NFDR_HOURLY = """query NfdrsObs($fuelModels: String!, $startDateRange: Date, $endDateRange: Date, $stationIds: String,
  $hasHistoricData: TriState, $dateTimeFormat: DateTimeFormat, $page: Int, $perPage: Int) {
  nfdrsObs(fuelModels: $fuelModels, startDateRange: $startDateRange, endDateRange: $endDateRange,
    stationIds: $stationIds, hasHistoricData: $hasHistoricData, dateTimeFormat: $dateTimeFormat,
    page: $page, per_page: $perPage) {
    _metadata { page page_count total_count }
    data { station_id observation_time_lst nfdr_type energy_release_component burning_index } } }"""
_nfdr = {"mode": None, "off": None}     # which query works; why ERC/BI is off for this run


def _observed(r):
    t = str(r.get("nfdr_type") or "O").strip().upper()
    return not t.startswith("F")         # skip forecast rows


def _nfdr_rows(mode, d, chunk, fuel_model):
    if mode == "minmax":
        v = {"startDate": d.isoformat(), "endDate": d.isoformat(), "stationIds": chunk,
             "fuelModels": fuel_model, "hasHistoricData": "ALL", "perPage": 5000}
        rows = paged(lambda p: NFDR_MINMAX, "nfdrMinMax", v)
        return [(r.get("station_id"), r.get("energy_release_component_max"), r.get("burning_index_max"))
                for r in rows if _observed(r) and str(r.get("summary_date") or d.isoformat())[:10] == d.isoformat()]
    v = {"fuelModels": fuel_model, "startDateRange": d.isoformat(), "endDateRange": d.isoformat(),
         "stationIds": chunk, "hasHistoricData": "ALL", "dateTimeFormat": "LocalStationTime", "perPage": 5000}
    rows = paged(lambda p: NFDR_HOURLY, "nfdrsObs", v)
    return [(r.get("station_id"), r.get("energy_release_component"), r.get("burning_index"))
            for r in rows if _observed(r) and str(r.get("observation_time_lst") or d.isoformat())[:10] == d.isoformat()]


def fetch_erc(d, ids, fuel_model):
    """Daily max ERC and BI per RAWS: ({id: erc}, {id: bi})."""
    if _nfdr["off"]:
        raise RuntimeError(_nfdr["off"])
    out, bi = {}, {}
    for i in range(0, len(ids), 60):
        chunk = ",".join(str(x) for x in ids[i:i + 60])
        if _nfdr["mode"]:
            rows = _nfdr_rows(_nfdr["mode"], d, chunk, fuel_model)
        else:
            errs = []
            for mode in ("minmax", "hourly"):
                try:
                    rows = _nfdr_rows(mode, d, chunk, fuel_model)
                    _nfdr["mode"] = mode
                    print(f"FEMS ERC/BI: using the {'daily max' if mode == 'minmax' else 'hourly'} query")
                    break
                except Exception as e:
                    errs.append(f"{mode}: {e}")
            else:
                _nfdr["off"] = "FEMS ERC/BI unavailable this run (" + " | ".join(errs)[:400] + ")"
                print(_nfdr["off"])
                raise RuntimeError(_nfdr["off"])
        for sid, e_, b_ in rows:
            sid = str(sid)
            v = num(e_)
            if v is not None and 0 <= v < 200:
                out[sid] = max(out.get(sid, v), v)
            b = num(b_)
            if b is not None and 0 <= b < 500:
                bi[sid] = max(bi.get(sid, b), b)
    return out, bi


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


def station_hits(trig, hours, summ, typ, erc, bi=None):
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
    if trig.get("bi_min") is not None:
        used = True
        if bi is None or bi < float(trig["bi_min"]):
            return False
    return used


def evaluate(g, cfg, d, saved, stations, psas):
    set_name, cs = gaccs.criteria_for(d, cfg)
    SN, aft = cs.get("stations_needed") or {}, cfg.get("afternoon_hours") or {"start": 10, "end": 20}
    autos = [t for t in cs.get("triggers", []) if t.get("method") == "auto"]
    meta = {str(s["station_id"]): s for s in stations}
    obs = dict(saved.get("raws") or {})
    if cfg.get("use_asos", True):
        obs.update(saved.get("asos") or {})
    ercs, bis = saved.get("erc") or {}, saved.get("bi") or {}
    per = defaultdict(list)
    for sid, hours in obs.items():
        m = meta.get(sid)
        if m:
            per[m["psa"]].append((m, hours, station_summary(hours, aft)))
    rows, st_rows = [], []
    for psa in sorted(p for p in psas if gaccs.gacc_of(p) == g):
        sts, n = per.get(psa, []), len(per.get(psa, []))
        need = int(SN.get("small_psa_needed", 1)) if 0 < n <= int(SN.get("small_psa_station_count", 3)) else int(SN.get("default", 2))
        observed, counts, names = "", [], []
        hits_by = defaultdict(str)
        for t in autos:
            hit = []
            for m, hours, s in sts:
                if station_hits(t, hours, s, typical_max(cfg, psa, m.get("elev_ft")), ercs.get(str(m["station_id"])), bis.get(str(m["station_id"]))):
                    hit.append(m["name"]); hits_by[str(m["station_id"])] += t["letter"]
            counts.append(f"{t['letter']}={len(hit)}")
            if hit:
                names.append(f"{t['letter']}: " + "; ".join(hit))
            if n and len(hit) >= need:
                observed += t["letter"]
        vals = lambda k: [s[k] for _, _, s in sts if s[k] is not None]
        e = sorted(ercs[str(m["station_id"])] for m, _, _ in sts if str(m["station_id"]) in ercs)
        b = sorted(bis[str(m["station_id"])] for m, _, _ in sts if str(m["station_id"]) in bis)
        med = lambda a: "" if not a else round((a[len(a) // 2] + a[(len(a) - 1) // 2]) / 2, 1)
        n_asos = sum(1 for m, _, _ in sts if m.get("type") == "ASOS")
        rows.append({
            "date": d.isoformat(), "psa": psa, "criteria_set": set_name, "raws_reporting": n - n_asos, "asos_reporting": n_asos, "stations_needed": need,
            "observed": observed, "trigger_hits": ";".join(counts), "hit_stations": " | ".join(names),
            "max_temp_f": max(vals("maxt"), default=""), "min_rh": min(vals("minrh"), default=""),
            "max_wind_mph": max(vals("maxws"), default=""), "max_gust_mph": max(vals("maxg"), default=""),
            "erc": med(e), "erc_stations": len(e), "bi": med(b), "bi_stations": len(b),
            "dry_stations": sum(1 for v in vals("minrh") if v < float(cfg.get("dry_rh_context", 20))),
            "sources_missing": " ".join(saved.get("missing", [])),
            "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        for m, hours, sm in sts:
            sid = str(m["station_id"])
            st_rows.append({"date": d.isoformat(), "station_id": sid, "name": m.get("name", ""), "type": m.get("type") or "RAWS", "psa": psa,
                            "hours": len(hours), "max_temp_f": "" if sm["maxt"] is None else sm["maxt"],
                            "min_rh": "" if sm["minrh"] is None else sm["minrh"],
                            "max_wind_mph": "" if sm["maxws"] is None else sm["maxws"],
                            "max_gust_mph": "" if sm["maxg"] is None else sm["maxg"],
                            "erc": "" if ercs.get(sid) is None else ercs[sid], "bi": "" if bis.get(sid) is None else bis[sid],
                            "typical_max_f": round(typical_max(cfg, psa, m.get("elev_ft")), 1), "triggers_hit": hits_by.get(sid, "")})
    return rows, st_rows


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
    stations = [{**s, "type": "RAWS"} for s in load_raws(psas)]
    try:
        asos = load_asos(psas)
    except Exception as e:
        print(f"ASOS/AWOS station list unavailable: {e}")
        asos = []
    stations += asos
    codes = sorted({gaccs.gacc_of(p) for p in psas})
    if args.gacc:
        codes = [c for c in codes if c == args.gacc.upper()]
    print(f"{len(psas)} PSAs in {len(codes)} GACCs, {len(stations) - len(asos)} RAWS, {len(asos)} ASOS/AWOS")
    failures = 0

    for g in codes:
        cfg = gaccs.load_config(g)
        tz = ZoneInfo(cfg.get("timezone", "America/Denver"))
        gdir = gaccs.gacc_dir(g)
        raw_dir = os.path.join(gdir, "obs_raw")
        os.makedirs(raw_dir, exist_ok=True)
        out_path = os.path.join(gdir, "observed.csv")
        st_path = os.path.join(gdir, "station_obs.csv")
        st_results = defaultdict(list)
        for r in read_csv(st_path):
            st_results[r["date"]].append(r)
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
            # days saved while FEMS ERC/BI was down: try them again for up to 30 days
            no_erc = {dt for dt, rows in results.items() if not any(x.get("erc") or x.get("bi") for x in rows)}
            retry = {r["date"] for r in existing if ("erc" in (r.get("sources_missing") or "").split() or r["date"] in no_erc)
                     and 0 < (today - date.fromisoformat(r["date"])).days <= 30}
            todo = sorted(set(todo) | retry)
            if not os.path.exists(st_path) or (existing and ("haines" in existing[0] or "asos_reporting" not in existing[0])):
                # first run with station results, results made with the old Haines check, or before ASOS/BI: redo every saved day
                todo = sorted(set(todo) | {f[:-8] for f in os.listdir(raw_dir) if f.endswith(".json.gz")})
        g_st = [s for s in stations if s.get("gacc") == g]
        ids = [s["station_id"] for s in g_st if s.get("type") != "ASOS"]
        asos_ids = [s["station_id"] for s in g_st if s.get("type") == "ASOS"] if cfg.get("use_asos", True) else []
        for ds in todo:
            d = date.fromisoformat(ds)
            rp = os.path.join(raw_dir, f"{ds}.json.gz")
            if os.path.exists(rp):
                with gzip.open(rp, "rt") as fh:
                    saved = json.load(fh)
                changed = False
                if asos_ids and "asos" not in saved:          # days saved before ASOS/AWOS was added (the archive is public)
                    try:
                        saved["asos"] = fetch_asos_obs(d, tz, asos_ids)
                        changed = True
                    except Exception as e:
                        print(f"{g} {ds} ASOS/AWOS backfill: {e}")
                no_nfdr = "bi" not in saved or "erc" in saved.get("missing", []) or not saved.get("erc")
                if ids and no_nfdr and (today - d).days <= 30 and not _nfdr["off"]:   # ERC/BI missing; FEMS keeps recent days
                    try:
                        erc, bi = fetch_erc(d, ids, cfg.get("erc_fuel_model", "Y"))
                        if erc or bi or "bi" not in saved:
                            saved["erc"], saved["bi"] = erc, bi
                            saved["missing"] = [m for m in saved.get("missing", []) if m != "erc"]
                            changed = True
                    except Exception as e:
                        if not _nfdr["off"]:
                            print(f"{g} {ds} ERC/BI backfill: {e}")
                if changed:
                    with gzip.open(rp, "wt") as fh:
                        json.dump(saved, fh, separators=(",", ":"))
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
                if asos_ids:
                    try:
                        saved["asos"] = fetch_asos_obs(d, tz, asos_ids)
                    except Exception as e:
                        print(f"{g} {ds} ASOS/AWOS: {e}")
                        saved["asos"] = {}
                        missing.append("asos")
                try:
                    saved["erc"], saved["bi"] = fetch_erc(d, ids, cfg.get("erc_fuel_model", "Y"))
                except Exception as e:
                    if _nfdr["off"] != str(e):
                        print(f"{g} {ds} ERC/BI: {e}")
                    saved["erc"], saved["bi"] = {}, {}
                    missing.append("erc")
                saved["missing"] = missing
                with gzip.open(rp, "wt") as fh:
                    json.dump(saved, fh, separators=(",", ":"))
            rows, srows = evaluate(g, cfg, d, saved, stations, psas)
            results[ds], st_results[ds] = rows, srows
            met = " ".join(f"{r['psa']}:{r['observed']}" for r in rows if r["observed"]) or "none"
            log(g, ds, f"ok; criteria={rows[0]['criteria_set'] if rows else ''}; met={met}")
            print(f"{g} {ds}: {len(rows)} PSAs; met {met}")
        write_csv(out_path, [r for k in sorted(results) for r in sorted(results[k], key=lambda x: x["psa"])], OUT_COLS)
        keep_from = (today - timedelta(days=ST_KEEP_DAYS)).isoformat()   # station detail for recent days; obs_raw keeps everything
        write_csv(st_path, [r for k in sorted(st_results) if k >= keep_from for r in sorted(st_results[k], key=lambda x: (x["psa"], x["name"]))], ST_COLS)
        # this GACC's station list (RAWS and ASOS/AWOS inside its PSAs), with the typical max each one is checked against
        g_use = [st for st in g_st if st.get("type") != "ASOS" or cfg.get("use_asos", True)]
        write_csv(os.path.join(gdir, "stations.csv"),
                  sorted(({**st, "typical_max_f": round(typical_max(cfg, st["psa"], st.get("elev_ft")), 1)} for st in g_use), key=lambda x: (x["psa"], x["name"])),
                  ["station_id", "name", "type", "lat", "lon", "elev_ft", "state", "psa", "typical_max_f"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
