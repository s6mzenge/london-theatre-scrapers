# STAGE. — London theatre, for less.

A daily price-comparison aggregator for London West End tickets across six seller sources (TodayTix, Official London Theatre, LOVEtheatre, SeatPlan, LondonTheatreDirect, TheatreTicketsDirect).

The site shows you the cheapest seat for tonight, this week, and this month — across every seller — with direct booking links and per-source seat-plan metadata.

## How it works

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  GitHub Actions  │────▶│  public/data/    │────▶│  Cloudflare      │
│  (6 scrapers +   │     │  unified.json    │     │  Pages           │
│   dedupe)        │     │                  │     │  (React + Vite)  │
└──────────────────┘     └──────────────────┘     └──────────────────┘
```

1. **Scrapers** (`scraper/*.py`) run on demand via GitHub Actions
2. Each scraper crawls its source for shows, performances, and prices
3. A **dedupe** step matches shows across sources into one canonical catalogue
4. The unified `unified.json` is committed to `public/data/`
5. **Cloudflare Pages** rebuilds on the commit and ships the fresh data with the new deploy

## Quick start

### Frontend

```bash
npm install
npm run dev          # → http://localhost:5173
```

The dev server reads `public/data/unified.json` — whatever the last scrape committed.

### Scrapers (Python 3.10+)

```bash
pip install -r scraper/requirements.txt
playwright install chromium   # only needed for todaytix

# Run any single scraper:
python scraper/olt_scraper.py --out scraper/data/olt.json --limit 5
```

To do a full local scrape + dedupe end-to-end:

```bash
mkdir -p scraper/data dedupe_output
for s in scraper/*_scraper.py; do
  python "$s" --out "scraper/data/$(basename "$s" _scraper.py).json"
done
python scraper/analysis/dedupe.py scraper/data/ \
  --out dedupe_output \
  --overrides scraper/analysis/overrides.yaml
cp dedupe_output/unified.json public/data/unified.json
```

Or just trigger the GitHub Actions workflow — same thing, in parallel, on cloud machines.

## Repo structure

```
.
├── .github/workflows/
│   └── scrape.yml              # Manual-trigger scraper + dedupe + commit
├── public/
│   ├── _redirects              # CF Pages SPA fallback
│   ├── favicon.svg
│   └── data/
│       └── unified.json        # Auto-generated; the file the site reads
├── src/
│   ├── App.jsx                 # Main app
│   ├── components/             # Cheapest{Tonight,Week,Month}, Sellers, …
│   ├── lib/
│   │   ├── data.js             # Loads /data/unified.json + aggregations
│   │   ├── dates.js
│   │   └── format.js
│   ├── index.css
│   └── main.jsx
├── scraper/
│   ├── README.md               # Scraper-specific docs
│   ├── requirements.txt
│   ├── analysis/
│   │   ├── dedupe.py           # Cross-source matching pipeline
│   │   └── overrides.yaml      # Manual force-merge / force-split rules
│   ├── todaytix_scraper.py
│   ├── olt_scraper.py
│   ├── lovetheatre_scraper.py
│   ├── seatplan_scraper.py
│   ├── londontheatredirect_scraper.py
│   ├── ttd_scraper.py
│   └── lovetheatre_calendar.py # Helper, used by lovetheatre_scraper.py
├── index.html
├── package.json
├── vite.config.js
└── README.md
```

## Cloudflare Pages settings

| Setting | Value |
|---|---|
| Production branch | `main` |
| Build command | `npm run build` |
| Build output | `dist` |
| Root directory | *(blank, or `.`)* |

Every commit to `main` triggers a rebuild — including the `data: refresh from full scrape` commits from the workflow, which is the mechanism that propagates new data to the site.

## Costs

Fully free on GitHub Actions + Cloudflare Pages free tier. A full scrape uses ~5 minutes of Actions compute. Pages serves the ~3MB gzipped `unified.json` from its edge cache.

## Notes

- **Scraping politeness**: each scraper rate-limits per-source per the site's robots.txt and rough capacity. See per-scraper docstrings for specifics.
- **OLT proxy**: officiallondontheatre.com blocks cloud IPs. The OLT scraper routes through a Cloudflare Worker reverse proxy. Set `OLT_PROXY_URL` and `OLT_PROXY_TOKEN` as repo secrets, or set `skip_olt=true` when triggering the workflow.
- **Manual matching overrides**: when dedupe gets a borderline case wrong, edit `scraper/analysis/overrides.yaml` to force-merge or force-split specific records. Documented in that file.
