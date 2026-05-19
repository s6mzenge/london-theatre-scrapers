# London Theatre Scrapers

Seven independent scrapers for London theatre ticket sites. Each produces a single self-contained JSON file with the full show catalogue, performance calendars, and per-source metadata (offers, last-minute slices, ratings, FAQs, etc.).

## Sources

| Scraper | Site | Approach |
|---|---|---|
| `todaytix_scraper.py` | [todaytix.com/london](https://www.todaytix.com/london) | Playwright (listing) + requests (detail) |
| `londontheatre_scraper.py` | [londontheatre.co.uk](https://www.londontheatre.co.uk/) | Playwright (listing) + requests (detail) |
| `olt_scraper.py` | [officiallondontheatre.com](https://officiallondontheatre.com/) | requests + BeautifulSoup |
| `lovetheatre_scraper.py` | [lovetheatre.com](https://www.lovetheatre.com/) | requests + BeautifulSoup |
| `seatplan_scraper.py` | [seatplan.com/london](https://seatplan.com/london/) | requests + BeautifulSoup |
| `londontheatredirect_scraper.py` | [londontheatredirect.com](https://www.londontheatredirect.com/) | requests + regex (React props) |
| `ttd_scraper.py` | [theatreticketsdirect.co.uk](https://www.theatreticketsdirect.co.uk/) | requests + BeautifulSoup |

Each scraper is a standalone script — no shared imports, no relative imports. You can run, edit, or pull any one of them in isolation.

## Setup

Requires **Python 3.10+** (the scrapers use `X | None` type union syntax).

```bash
# Clone
git clone https://github.com/<you>/london-theatre-scrapers.git
cd london-theatre-scrapers

# Virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Dependencies
pip install -r requirements.txt

# Playwright Chromium — only needed for todaytix and londontheatre
playwright install chromium
```

If you don't care about TodayTix or LondonTheatre right now, you can skip `playwright install chromium` and just pass `--skip-browser` to the smoke test.

## Quick check that it all works

```bash
python smoke_test.py
```

This runs each scraper with `--limit 2` (fetch only two shows each) and prints a single summary table at the end. A full run takes about 2–3 minutes; nothing here writes to your real `data/` outputs — smoke results land in `data/smoke/`.

Useful flags:

```bash
python smoke_test.py --limit 5           # bump the per-scraper limit
python smoke_test.py --only olt          # one scraper by name
python smoke_test.py --only olt --only ttd
python smoke_test.py --skip-browser      # skip the two Playwright scrapers
python smoke_test.py --timeout 600       # per-scraper timeout (default 300s)
```

Exit code is `0` if every scraper succeeded, `1` if any failed.

## Running a single scraper for real

Every scraper has the same core CLI:

```bash
python olt_scraper.py                              # full scrape
python olt_scraper.py --limit 5                    # test with 5 shows
python olt_scraper.py --out data/olt.json          # custom path
python olt_scraper.py --concurrency 24             # parallelism for detail fetches
```

Most also support:

- `--no-tag-lists` — skip the filter-slice listings (offers, last-minute, etc.) and only fetch the master catalogue.
- `--dry-run` — fetch but don't write.

The two Playwright scrapers add:

- `--headed` — show the browser window (useful for debugging selectors).

Run any scraper with `--help` to see the full flag set; they differ slightly per source.

## Output

Each scraper writes a single JSON file with a consistent top-level shape:

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

The per-show schema differs by source — each site exposes a different mix of fields (ratings, cast, FAQs, weekly schedules, badges, etc.) and the scrapers preserve the source-specific shape rather than flattening to a lowest common denominator. The `report` block is the same shape everywhere and is the right place to look for partial-run diagnostics.

## Layout

```
.
├── README.md
├── requirements.txt
├── .gitignore
├── smoke_test.py
├── data/                       # output JSON lands here (gitignored)
│   └── .gitkeep
├── todaytix_scraper.py
├── londontheatre_scraper.py
├── olt_scraper.py
├── lovetheatre_scraper.py
├── seatplan_scraper.py
├── londontheatredirect_scraper.py
└── ttd_scraper.py
```

The `data/` directory is committed (via `.gitkeep`) but its JSON contents are gitignored — scrape outputs stay local.
