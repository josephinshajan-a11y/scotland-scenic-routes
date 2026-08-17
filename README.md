# Scotland's most picturesque running routes

**Reboot Data Analyst assessment — Josephin Shajan**

100 trail-running routes collected from AllTrails on 31 July 2026, scored on how picturesque they are, and analysed.

---

## Start here

| If you want | Open |
|---|---|
| The findings | `docs/insights_report.docx` |
| The reasoning and limitations | `docs/methodology.docx` |
| Charts you can interact with | `Scotland_Picturesque_Routes.html` — any browser, nothing to install |
| The data in a spreadsheet | `reboot_output/Scotland_picturesque_routes.xlsx` — 7 tabs |
| The code | `alltrails_scotland.py` |

**The headline:** Glencoe Lochan ranks first. More usefully, how highly runners *rate* a route correlates with how much they *photograph* it at just 0.13 — star ratings say the run was good, not that the place was beautiful.

---

## Contents

```
alltrails_scotland.py                 collection, cleaning, scoring, charts, tests
build_workbook.py                     builds the Excel workbook from the outputs
Scotland_Picturesque_Routes.ipynb     notebook source
Scotland_Picturesque_Routes.html      notebook with charts, opens in any browser
requirements.txt

docs/
  insights_report.docx                findings
  methodology.docx                    choices and limitations

reboot_output/
  Scotland_picturesque_routes.xlsx    7-tab workbook
  data_raw/
    listings.json                     the 100 route URLs found
    trail_details.json                every field collected per route
    reviews.json                      421 review texts
  data_processed/
    routes_clean.csv                  cleaned dataset
    routes_scored.csv                 with all five signals and the index
    summary_top20.csv                 the ranking
    summary_by_difficulty.csv
    summary_by_route_type.csv
    summary_tag_counts.csv
    summary_correlations.csv
    summary_sensitivity.csv
  outputs/
    qa_report.md                      cleaning actions and field completeness
    auto_findings.md                  every figure quoted in the report, computed
    figures/                          five charts
```

## Reproducing the analysis

Everything downstream of collection reruns from the raw data in a few seconds:

```bash
pip3 install -r requirements.txt
python3 alltrails_scotland.py --test          # 22 checks on the scoring maths
python3 alltrails_scotland.py --from reparse  # rebuild all outputs from raw data
```

## Re-running the collection

```bash
python3 -m playwright install chromium
python3 alltrails_scotland.py --check         # is the site reachable?
python3 alltrails_scotland.py                 # full run, ~40 minutes
```

**Note on access.** AllTrails returns HTTP 403 to an automated browser. During collection I worked around this by attaching to a Chrome instance launched normally, with the site loaded by hand first:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile" &
python3 alltrails_scotland.py --browser cdp
```

`--check` distinguishes the four failure modes that all look like an empty result: wrong URL, refused request, links present but not matching the expected pattern, and no links at all. Each needs a different fix.

## A note on what's included

`data_raw/` holds the collection output. The collector also saves the raw HTML of every page it visits, which is what allowed the parser to be rewritten offline without going back to the site. Those files are a working cache rather than a dataset, and run to several hundred megabytes, so they are **not** included in this zip. Everything needed to verify the analysis is here; the HTML is available on request.

## Approach in brief

Collection uses Playwright driving a real browser, because AllTrails renders client-side and returns an empty shell to a plain HTTP fetch. Routes were gathered by sweeping AllTrails' regional trail-running pages rather than one national listing — a national list is ordered by popularity, so the first hundred routes would have been almost entirely Edinburgh and Glasgow.

"Picturesque" isn't a field on AllTrails, so the index is a weighted composite of five percentile-ranked signals: photos per review (25%), scenery language in reviews (25%), AllTrails' view tag (20%), shrunk star rating (15%), and elevation per km (15%).

Validation includes 22 unit tests on the scoring maths, range and duplicate checks logged in `qa_report.md`, and a sensitivity analysis recomputing the ranking under seven different weightings. That last check caught a real error: an earlier version gave 30% of the weight to a tag signal built against tags AllTrails doesn't actually use, and removing it changed the ranking by nothing at all.

Full reasoning and limitations in `docs/methodology.docx`.
