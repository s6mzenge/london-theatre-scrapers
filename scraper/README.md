# Scrapers

Seven independent scrapers for London theatre ticket sites, plus a dedupe step that consolidates them into one canonical catalogue. Each scraper is a standalone Python script — no shared imports — so you can run, edit, or pull any one in isolation.

## Sources

| Scraper | Site | Approach |
|---|---|---|
| `todaytix_scraper.py` | [todaytix.com/london](https://www.todaytix.com/london) | Playwright (listing) + requests (detail) |
| `londontheatre_scraper.py` | [londontheatre.co.uk](https://www.londontheatre.co.uk/) | Playwright (listing) + requests (detail) |
| `olt_scraper.py` | [officiallondontheatre.com](https://officiallondontheatre.com/) | requests + BeautifulSoup |
| `lovetheatre_scraper.py` | [lovetheatre.com](https://www.lovetheatre.com/) | requests + BeautifulSoup |
| `seatplan_scraper.py` | [seatplan.com/london](https://seatplan.com/london/) | requests + BeautifulSoup |
| `londontheatredirect_scraper.py` | [londontheatredirect.com](https://www.londontheatredirect.com/) | requests + regex |
| `ttd_scraper.py` | [theatreticketsdirect.co.uk](https://www.theatreticketsdirect.co.uk/) | requests + BeautifulSoup |

## Setup

Requires **Python 3.10+** (scrapers use `X | None` union syntax).

```bash
pip install -r requirements.txt
playwright install chromium       # only needed for todaytix + londontheatre
```

## Running a scraper

From the repo root:

```bash
python scraper/olt_scraper.py                                    # full scrape
python scraper/olt_scraper.py --limit 5                          # test mode
python scraper/olt_scraper.py --out scraper/data/olt.json        # custom path
python scraper/olt_scraper.py --concurrency 24                   # tune workers
```

All scrapers share the same core CLI. Run any with `--help` for the full flag set.

The two Playwright scrapers add `--headed` for debugging selectors.

## Running dedupe

```bash
python scraper/analysis/dedupe.py scraper/data/ \
  --out dedupe_output \
  --overrides scraper/analysis/overrides.yaml
```

The pipeline has five stages:

1. **Normalize** titles and venues
2. **Apply VENUE_ALIASES** registry (canonical venue names)
3. **Fuzzy match** within each venue (rapidfuzz; within-source-dup guard active)
4. **Orphan rescue** (title-only attach for records missing venue; ambiguity guard refuses multi-match cases)
5. **Manual overrides** from `overrides.yaml`

Output goes to `dedupe_output/`:

- `unified.json` — the canonical catalogue (what the website reads)
- `report.txt` — human-readable matching summary
- `review.json` — borderline merge pairs flagged for review

## Output schema

Each scraper writes a single JSON file:

```jsonc
{
  "scraped_at": "2026-05-19T08:30:00+00:00",
  "source": "https://...",
  "show_count": 127,
  "performance_count": 4123,    // or "showtime_count" for todaytix/londontheatre
  "report": {
    "succeeded_show_count": 127,
    "failed_show_count": 0,
    "warnings": [],
    "failures": []
  },
  "shows": [ { ... } ]
}
```

Per-show schema differs by source — each site exposes a different mix of fields (ratings, cast, FAQs, weekly schedules, badges, etc.) and the scrapers preserve the source-specific shape rather than flattening.

## Manual matching overrides

When dedupe matches the wrong pair of shows or fails to match a true duplicate, edit `analysis/overrides.yaml`. The file documents the schema for `force_merge` and `force_split` rules. Re-run dedupe and check `review.json` to confirm.
