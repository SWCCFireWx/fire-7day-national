# National 7-Day Significant Fire Potential Verification

Archives the national 7-Day Significant Fire Potential forecast every day for every PSA and all
seven lead days, split by GACC, and verifies it against each GACC's **own** High Risk trigger
criteria, hand-confirmed trigger days, and recorded fires. Each GACC sets its own triggers,
thresholds; nothing is a national blend. Anyone can propose changes, which are emailed to the site owner for review.

The website has a GACC bar at the top: **National** (every GACC together, with a GACC-by-GACC
comparison) followed by each GACC. Every GACC gets the full set of tabs: Forecast archive,
Forecast evolution, Map, Weather check, Scorecard, Analysis, Criteria, Export.

## What's where

| Path | What it is |
|---|---|
| `index.html` | the website (GitHub Pages) |
| `config/gaccs.json` | list of GACCs and their PSAs (updated automatically) |
| `config/gaccs/<GACC>.json` | that GACC's criteria sets, triggers, schedule, typical max temps, ERC fuel model |
| `data/<GACC>/forecasts.csv` | forecast archive (issue date × lead day × PSA) |
| `data/<GACC>/observed.csv` | weather check results per PSA per day |
| `data/<GACC>/confirmed.csv` | hand-confirmed trigger days (e.g. Lightning), edited from the website |
| `data/<GACC>/fires.csv` | significant fires (you or the GACC edit this) |
| `data/<GACC>/obs_raw/` | saved hourly RAWS, ERC and Haines per day, for re-checking |
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
  hours), RH at or below, max temperature above the typical peak-season max, ERC at least. Haines
  (nearest 00Z sounding) can be added as a PSA-level condition. A PSA meets the trigger when
  enough stations hit it (normally 2, or 1 in PSAs with 3 or fewer stations; editable).
- **Hand-verified triggers** (like Lightning and Recreation) count only on days recorded under
  **Confirmed days**, where people pick the trigger, date and PSAs and describe the observed
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

### Proposing and applying changes

Every GACC's triggers are visible to anyone: the **Criteria** tab shows the triggers in use in
plain language, and in **National** it lists every GACC's triggers on one page.

**Anyone can propose a change or report observed days** (no GitHub account needed):

1. Pick the GACC and go to the Criteria tab. The **request form** is already filled in with the
   triggers in use, one row per trigger, like a spreadsheet. Change any cell, add or remove a row.
2. Hand-verified days (Lightning, Recreation, ...) go under **Confirmed days** with the observed
   conditions.
3. Enter a name and contact, then click **Email to Krista**. An email to **krista.king@usda.gov**
   opens with everything written out plus a code block. (Long requests are copied to the
   clipboard to paste in.)
4. Prefer Excel? **Download as Excel form** gives the same form pre-filled; fill in the yellow
   cells and email the file as an attachment.

**To apply a request (site owner):**

1. Open the site's **Criteria** tab, expand **Site owner tools**, and either click **Load Excel
   form** and pick the returned file, or paste the whole email and click **Load request**. It
   switches to the right GACC and fills in the changes.
2. Review them, then click **Save on GitHub** and **Submit new issue**.
3. The automatic step validates and saves the settings, re-checks that GACC's saved days and
   replies on the issue.

Only the repository owner (and any collaborators) can save; anyone else's GitHub issue is
rejected. (Each GACC's settings file also has an `editors` list, empty by default, that can grant a
specific GitHub user rights to save that one GACC directly if that's ever wanted.)

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
PSAs (in National, quick buttons pick one GACC). Sections include the scorecard, scores by month,
misses and false alarms, the color-coded forecast grid, forecast evolution, observed weather,
confirmed days, fires and each GACC's criteria.

## Data sources (public, no login)

- 7-Day forecast and PSA boundaries: NPSG `outlooks_forecast` service.
- RAWS hourly observations and NFDRS ERC: FEMS Climatology API (public window is about 2 weeks,
  so the nightly run saves what it pulls).
- Soundings: Iowa Environmental Mesonet upper-air archive (00Z, about 75 US sites).

## Notes

- PSA codes start with the GACC's two-letter prefix (SW05, NR12, ...); a new prefix in the
  forecast gets its own settings file automatically.
- The Alaska and Hawaii PSAs are included only if the national service carries them.
- To re-check every saved day after bulk edits to settings files, run the archive workflow with
  **rescore** checked.
