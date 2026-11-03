# National 7-Day Significant Fire Potential Verification

Archives the national 7-Day Significant Fire Potential forecast every day for every PSA and all
seven lead days, split by GACC, and verifies it against each GACC's **own** High Risk trigger
criteria, hand-confirmed trigger days, and recorded fires. Each GACC sets its own triggers,
thresholds; nothing is a national blend. Trigger and criteria changes are made by the site owner
(people email Krista King); anyone can confirm Lightning, Recreation and other by-hand trigger days.

The website has a GACC bar at the top: **National** (every GACC together, with a GACC-by-GACC
comparison) followed by each GACC. Every GACC gets the full set of tabs: Forecast archive,
Forecast evolution, Map, Weather check, Weather data, Verification, Analysis, Confirm triggers, Criteria, Export.

## What's where

| Path | What it is |
|---|---|
| `index.html` | the website (GitHub Pages) |
| `config/gaccs.json` | list of GACCs and their PSAs (updated automatically) |
| `config/gaccs/<GACC>.json` | that GACC's criteria sets, triggers, schedule, typical max temps, ERC fuel model |
| `data/<GACC>/forecasts.csv` | forecast archive (issue date × lead day × PSA) |
| `data/<GACC>/observed.csv` | weather check results per PSA per day |
| `data/<GACC>/confirmed.csv` | hand-confirmed trigger days (Lightning, Recreation, ...), saved from the website by anyone |
| `data/<GACC>/stations.csv` | the RAWS used for that GACC (all RAWS inside its PSAs), with the typical max each is checked against |
| `data/<GACC>/station_obs.csv` | each station's daily values and which triggers it met (last 120 days; shown on the Weather data tab) |
| `data/<GACC>/fires.csv` | significant fires (you or the GACC edit this) |
| `data/<GACC>/obs_raw/` | saved hourly RAWS and ERC per day, for re-checking |
| `data/raw/` | untouched forecast pulls |
| `scripts/` | archiver, weather check, settings saver, shared GACC definitions |
| `.github/workflows/archive.yml` | nightly job |
| `.github/workflows/apply-criteria.yml` | applies settings changes sent from the website |

## One-time setup

1. **Create a new repository** on GitHub (for example `fire-7day-national`), Public, empty.
2. **Upload everything** from this folder, including the hidden `.github` folder (on a Mac,
   Cmd+Shift+. in Finder shows it). `.github/workflows` must contain `archive.yml` and
   `apply-criteria.yml`.
3. **Settings → Actions → General → Workflow permissions →** "Read and write permissions" → Save.
4. **Settings → General → Features →** make sure **Issues** is ticked.
5. **Settings → Pages →** Branch `main`, folder `/ (root)` → Save.
6. **Actions → Archive 7-Day Fire Potential → Run workflow.** The first run downloads the PSA
   boundaries and every RAWS in the country and back-checks 10 days for every GACC, so it can take
   a while (expect 20–60 minutes). Later nightly runs are faster.

## Schedule

Runs at about **3 am Mountain time** (09:05 / 10:05 UTC so it hits 3 am in both MDT and MST),
before any GACC issues, so it saves the previous day's finished national product. Each GACC's
weather "day" runs midnight to midnight in that GACC's time zone (set in its settings file).

## Each GACC's criteria

On the **Criteria** tab (pick the GACC first):

- **Criteria sets** – each set lists that GACC's High Risk triggers. A trigger has a **letter**
  (matching the letter on the 7-day map), a **name**, a **map color** (orange or red), and is
  either **checked automatically** or **confirmed by hand**.
- **Automatic triggers** combine any of these station conditions, all of which must be met at the
  same station: wind (sustained speed, or gusts for a number of hours, with RH checked in the same
  hours), RH at or below, max temperature above the typical peak-season max, ERC at least. A PSA meets the trigger when
  enough stations hit it (normally 2, or 1 in PSAs with 3 or fewer stations; editable).
- **Hand-verified triggers** (like Lightning and Recreation) count only on days recorded on the
  **Confirm triggers** tab, where people pick the trigger, date and PSAs and describe the observed
  conditions that met it.
- Every trigger has a written **definition** (as worded in the GACC's trigger criteria), shown on
  the site, in the request form and in exports.
- **Switch sets** from any date (e.g. a "Hot season" set). Each day is checked with the set in use
  that day.
- **Typical peak-season max** – a base temperature at 1,000 ft and a drop per 1,000 ft, plus
  optional per-PSA values. **ERC fuel model** – NFDRS 2016 V, W, X, Y or Z.

Every GACC starts with W (wind), B (burn environment) and L (lightning, by hand) at the Southwest
values; Northwest, Alaska, Eastern Area and Rocky Mountain also start with R (recreation, by hand).
The Rocky Mountain and Northwest lightning definitions are filled in; replace the rest with each
GACC's own triggers.

### Confirming Lightning, Recreation and other by-hand days (anyone)

1. Pick the GACC in the top bar and open the **Confirm triggers** tab.
2. Pick the trigger and date,
   describe the observed conditions, tick the PSAs that hit, and click **Add confirmed day**.
3. Click **Save confirmed days**. GitHub opens with the days filled in; sign in (a free GitHub
   account is all that's needed) and click **Submit new issue**.
4. An automatic step checks the dates, PSAs and trigger letter, saves them to
   `data/<GACC>/confirmed.csv`, re-checks that GACC's saved days and replies on the issue. The days
   then count as observed in the map, charts, verification and exports.

These saves can only add or remove confirmed days; they can't change any criteria.

### Changing a GACC's triggers or criteria (owner only)

The Criteria tab shows each GACC's triggers word for word, with a note asking people to email
Krista King (krista.king@usda.gov) for changes. The owner makes them under **Site owner tools** on
the Criteria tab (trigger table, plus **More settings** for criteria sets and switching, stations
needed, typical temperatures and ERC fuel model), then clicks **Save changes** and submits the
GitHub issue while signed in as the repository owner. Criteria changes from anyone else are
rejected with a reply asking them to email Krista.

To let specific people change one GACC's criteria, add their GitHub usernames to that GACC's
`editors` list in `config/gaccs/<GACC>.json`. Setting `OPEN_EDITING = True` in
`scripts/apply_criteria.py` would let anyone change criteria. Every change is logged (the issue plus
the commit history), so it can be reviewed or undone.

**Internal use:** the site shows an "Internal use only" notice. GitHub Pages sites on free accounts
are public to anyone with the link, so the notice asks people not to share it; it doesn't make the
site private.

## Loading every GACC's triggers at once

`scripts/import_trigger_workbook.py` loads the "GACC trigger starting points" Excel workbook (one
tab per GACC) into the settings files: `pip install openpyxl`, then
`python scripts/import_trigger_workbook.py GACC-trigger-starting-points.xlsx` (add `--dry-run` to
preview). Commit the changed `config/gaccs/*.json` files.

## Fires

Add rows to `data/<GACC>/fires.csv` on GitHub:

```
date,psa,fire_name,acres,cause,notes
2026-07-14,NR12,Example Fire,1250,Lightning,
```

## Exporting

The **Export** tab creates an Excel workbook, a PDF report, or a CSV for any date range and any
PSAs (in National, quick buttons pick one GACC). Sections include the verification summary, verification by month,
misses and false alarms, the color-coded forecast grid, forecast evolution, observed weather,
confirmed days, fires and each GACC's criteria.

## Data sources (public, no login)

- 7-Day forecast and PSA boundaries: NPSG `outlooks_forecast` service.
- RAWS hourly observations and NFDRS ERC: FEMS Climatology API (public window is about 2 weeks,
  so the nightly run saves what it pulls).
- Map background: Esri Light Gray / Dark Gray Canvas tiles (Esri, HERE, Garmin, OpenStreetMap contributors), loaded by the viewer's browser and switched with light/dark mode. If they can't load, the map still works on a plain gray background.

## Notes

- PSA codes start with the GACC's two-letter prefix (SW05, NR12, ...); a new prefix in the
  forecast gets its own settings file automatically.
- The Alaska and Hawaii PSAs are included only if the national service carries them.
- To re-check every saved day after bulk edits to settings files, run the archive workflow with
  **rescore** checked.
