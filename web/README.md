# STAGE. — London theatre, for less.

A daily price-comparison aggregator for London theatre tickets. Reads
the consolidated `unified.json` produced by your existing scraper +
dedupe pipeline and surfaces the cheapest seat across today, this
week, and this month.

This folder (`web/`) is the React/Vite frontend. The Python scrapers,
GitHub Actions workflow, and `analysis/dedupe.py` already live in the
repo. They write to a separate `data` branch (force-pushed each scrape
to keep `main` history clean), and the site reads from there at
runtime — no rebuild needed when scrapes refresh.

---

## Quick start

```bash
cd web
npm install
npm run dev          # opens http://localhost:5173
```

You'll see the bundled example data (6 real shows, 241 performances —
sliced from a real scrape on 19 May 2026) immediately. To point dev at
your live data instead, set `VITE_DATA_URL` in a `.env.local`:

```bash
# web/.env.local  (gitignored)
VITE_DATA_URL=https://raw.githubusercontent.com/s6mzenge/london-theatre-scrapers/data/unified/unified.json
```

Restart vite and the site will fetch real data on every page load.

---

## Architecture

The site is a static SPA. The build produces a `dist/` folder of HTML
+ JS + CSS that Cloudflare Pages serves directly — no server, no API.

**Data is fetched at runtime, not baked into the build.** Your existing
GitHub Actions workflow (`.github/workflows/scrape.yml`) force-pushes
`unified.json` to the `data` branch as part of every scrape. The site
reads that file from `raw.githubusercontent.com` on every page load.

| Trigger | What happens |
|---|---|
| Push to `main` (code change) | CF Pages rebuilds + redeploys (~1–2 min) |
| Push to `data` branch (scrape) | **Nothing on CF.** Site picks up fresh data within ~5 min via GitHub's raw cache |
| Manual CF cache purge | Forces all browsers to refetch immediately |

This decoupling is the whole point. Your scrape workflow already
documents this URL pattern in its `publish` job comments:

```
https://raw.githubusercontent.com/<owner>/<repo>/data/unified/unified.json
https://raw.githubusercontent.com/<owner>/<repo>/data/scrapers/<name>.json
```

The site uses only the first one.

### Data flow

```
[manual trigger / cron-job.org webhook]
     │
     ▼
  .github/workflows/scrape.yml
     │
     │  matrix: 7 scrapers run in parallel
     ▼
  data/<source>.json (artifacts)
     │
     │  combine + dedupe jobs
     ▼
  dedupe_output/unified.json (artifact)
     │
     │  publish job: force-push to `data` branch
     ▼
  raw.githubusercontent.com/<owner>/<repo>/data/unified/unified.json
     │
     │  fetched on every page load
     │  (Cloudflare edge cache + GitHub 5-min raw cache)
     ▼
  STAGE. site (running on Cloudflare Pages, built from `main`)
```

### Size note

The real `unified.json` is currently ~24 MB raw, ~3 MB gzipped.
`raw.githubusercontent.com` serves it gzipped, and Cloudflare's edge
caches the gzipped response. First page load fetches once per
edge-cache window; subsequent loads on the same edge are instant.
Parsing 24 MB of JSON in the browser takes ~200–500ms on a modern
laptop, ~1–2 s on mid-range mobile.

If this becomes a real bottleneck, the easiest wins (in order):

1. Have `analysis/dedupe.py` also emit a slimmed `unified-lite.json`
   without the `field_provenance` / `match_confidence` / `description`
   fields, and point `VITE_DATA_URL` at that.
2. Split the file: a `shows.json` with metadata only (loaded
   immediately) and per-show `performances/<id>.json` files (lazy-loaded
   when a user opens the detail view).
3. Move to Cloudflare R2 with a Worker that serves a compact
   pre-processed version.

None of these are needed for a first launch.

---

## Repository layout (with `web/` added)

```
repo-root/                                  (main branch)
├── .github/workflows/scrape.yml            (existing)
├── analysis/
│   ├── dedupe.py                           (existing)
│   └── overrides.yaml                      (existing)
├── *_scraper.py                            (7 scrapers — existing)
├── smoke_test.py                           (existing)
├── requirements.txt                        (existing)
├── README.md                               (existing)
└── web/                                    ← new
    ├── package.json
    ├── vite.config.js
    ├── index.html
    ├── public/
    │   ├── favicon.svg
    │   ├── _redirects
    │   └── data/
    │       └── unified.example.json        (committed sample, 6 shows)
    ├── scripts/
    │   └── copy-data.mjs                   (dev-only fallback)
    ├── src/
    │   ├── main.jsx
    │   ├── App.jsx
    │   ├── index.css
    │   ├── components/
    │   │   ├── Curtain.jsx
    │   │   ├── Tassel.jsx
    │   │   ├── Sidebar.jsx
    │   │   ├── Cheapest.jsx
    │   │   ├── CheapestTonight.jsx
    │   │   ├── CheapestWeek.jsx
    │   │   ├── CheapestMonth.jsx
    │   │   ├── Search.jsx
    │   │   ├── Sellers.jsx
    │   │   ├── ShowDetail.jsx
    │   │   └── TicketIcon.jsx
    │   └── lib/
    │       ├── data.js                     (aggregation logic)
    │       ├── dates.js
    │       └── format.js
    └── README.md                           (this file)
```

And on the `data` branch (force-pushed each scrape, single commit):

```
scrapers/<name>.json     (7 raw per-source outputs)
unified/unified.json     ← the site reads this
unified/review.json
unified/report.txt
```

---

## Cloudflare Pages deployment

### One-time setup

1. Push the `web/` folder to `main`.
2. In the Cloudflare dashboard: **Workers & Pages** → **Create application**
   → **Pages** → **Connect to Git** → select `london-theatre-scrapers`.
3. Configure the build:

   | Setting | Value |
   |---|---|
   | **Production branch** | `main` |
   | **Framework preset** | `Vite` (or `None`) |
   | **Build command** | `npm run build` |
   | **Build output directory** | `dist` |
   | **Root directory** | `web` |

4. Under **Environment variables**, add both:

   | Variable | Value |
   |---|---|
   | `NODE_VERSION` | `20` |
   | `VITE_DATA_URL` | `https://raw.githubusercontent.com/<your-username>/london-theatre-scrapers/data/unified/unified.json` |

   Substitute `<your-username>` for your GitHub handle. The repo is
   public, so no auth token is needed; raw.githubusercontent.com sends
   `Access-Control-Allow-Origin: *` so browser fetches work without
   CORS workarounds.

5. **Settings → Builds & deployments → Preview deployments**: exclude
   the `data` branch from previews (otherwise every scrape would
   trigger a useless preview build of stale frontend code on the
   wrong branch). Cloudflare's "include / exclude" rules under "Branch
   build controls" handle this — add `data` to the exclude list.

6. Click **Save and Deploy**.

### When something changes

| Change | Action needed |
|---|---|
| You edit `web/` source | `git push` to main; CF auto-rebuilds |
| Scraper produces new data | None — site picks up within ~5 min from GitHub raw |
| You want data refresh *now* | CF dashboard → **Caching → Purge Cache** |
| You change `VITE_DATA_URL` | CF dashboard → trigger a rebuild |
| Your scraper schema changes | Update `src/lib/data.js` to read the new fields |

---

## Local development

```bash
cd web
npm install
npm run dev
```

By default, this uses the bundled example data
(`public/data/unified.example.json`, 6 real shows trimmed from a real
scrape). To override:

**Option A — point at live GitHub data:**

```bash
# web/.env.local
VITE_DATA_URL=https://raw.githubusercontent.com/<your-username>/london-theatre-scrapers/data/unified/unified.json
```

**Option B — run dedupe locally and use that output:**

```bash
# From repo root:
mkdir -p data
python todaytix_scraper.py --out data/todaytix.json
# ... or download a prior workflow's all-scrapers artifact into data/ ...
python analysis/dedupe.py data/ --out dedupe_output/ \
       --overrides analysis/overrides.yaml

# Then in another terminal:
cd web && npm run dev
```

`scripts/copy-data.mjs` looks in `dedupe_output/unified.json` first,
then `unified/unified.json`, then `data/unified.json`, then falls back
to the bundled example. The first hit wins.

---

## Data shape

The site reads the `UnifiedShow` schema produced by `analysis/dedupe.py`:

```json
{
  "generated_at": "2026-05-19T20:25:00Z",
  "show_count": 284,
  "performance_count": 12389,
  "shows": [
    {
      "id": "harry-potter-and-the-cursed-child__palace",
      "title": "Harry Potter and the Cursed Child",
      "venue": "Palace Theatre",
      "min_price_gbp": 15,
      "max_price_gbp": 130,
      "performance_count": 144,
      "source_count": 8,
      "sources": [
        {
          "source": "seatplan",
          "source_id": "...",
          "url": "https://...",
          "performance_count": 144,
          "confidence": 100
        }
      ],
      "description": "...",
      "performances": [
        {
          "date": "2026-05-20",
          "time": "14:00",
          "min_price": 17.0,
          "max_price": 95,
          "currency": "GBP",
          "any_available": true,
          "sources": {
            "londontheatredirect": {
              "price_from": 34.0,
              "price_to": null,
              "currency": "GBP",
              "book_url": "https://...",
              "available": true
            }
          }
        }
      ]
    }
  ]
}
```

The site reads `data.shows[*]` only. Other top-level fields
(`source_summary`, `coverage_distribution`, `stages`) are diagnostic
and ignored. If you ever change the schema, `src/lib/data.js` is the
only file that touches these field names directly.

---

## Aggregation logic — three time horizons

All aggregations live in `src/lib/data.js`. None require fields beyond
what's already in `unified.json`.

### `aggregateTonight(data, today)`

Filters `data.shows[*].performances` where `date === today`, sorts by
`min_price` ascending, returns:
- `performances[]` — sorted list of `{ show, perf }`
- `floor` — single cheapest `min_price`
- `totalShowsCount` — unique shows playing tonight
- `underThirtyFiveCount` — unique shows with a sub-£35 performance

### `aggregateWeek(data, today)`

Builds a 7-day window starting today. For each day, computes the
*floor* (cheapest perf across all shows that day) and bucket
(percentile-based, 1=cheapest...5=priciest). Separately, per-show
aggregation: for each show with a performance in the window, finds
the cheapest perf AND captures the range of other prices that show
has this week — so the UI can show `vs other nights £32–£42`.

### `aggregateMonth(data, today)`

Builds a calendar grid for the current month, padded to whole weeks.
Each cell gets a floor + percentile bucket. Computes three insight
cards:

1. **CHEAPEST WEEKNIGHT** — lowest weeknight (Mon–Thu) floor and the
   show that wins it.
2. **BIGGEST RANGE** — show with the widest `min_price–max_price` spread
   that has performances this month. Wide range suggests bargain
   matinees vs premium evenings.
3. **PLAYING THIS MONTH** — count of distinct shows with at least one
   performance in this month.

---

## Component map

| Component | Purpose |
|---|---|
| `Curtain.jsx` | One-time rising-red-velvet intro. `sessionStorage` flag means it only plays once per browser session. Respects `prefers-reduced-motion`. |
| `Sidebar.jsx` | Ink sidebar with the **STAGE.** wordmark and three tabs (CHEAPEST / SEARCH / SELLERS). |
| `Cheapest.jsx` | Home view. Composes the three sections below. Exports `SectionHead` used by all of them. |
| `CheapestTonight.jsx` | Dark hero card + four alternative cards, sorted by price. |
| `CheapestWeek.jsx` | Day-floor heatmap strip (7 cells) + top-5 per-show "cheapest night this week" ranking. |
| `CheapestMonth.jsx` | Full month calendar heatmap (5-bucket brick opacity) + three insight cards. |
| `Search.jsx` | Search by title/venue with three sort modes. |
| `Sellers.jsx` | Per-seller leaderboard: how many performances does each seller win? |
| `ShowDetail.jsx` | Per-show date × seller price grid. Each cell links directly to the seller's booking page. |

---

## Design tokens

All in `:root` at the top of `src/index.css`:

| Token | Value | Role |
|---|---|---|
| `--color-paper` | `#ece6d6` | Main background |
| `--color-paper-warm` | `#fbf6e7` | Card backgrounds |
| `--color-ink` | `#1c1a17` | Sidebar, hero panel, primary text |
| `--color-brick` | `#a3322a` | Single signal/accent — prices, dots, active marks |
| `--color-velvet` / `--color-velvet-mid` / `--color-velvet-deep` | `#8a2424` / `#6b1818` / `#3d0a0a` | Curtain only |
| `--color-gold` / `--color-gold-light` | `#c9a050` / `#e6c876` | Valance trim, tassels, wordmark |
| `--font-display` | Bodoni Moda | Wordmark, prices, italic captions |
| `--font-shout` | Anton | Show titles, page headings |
| `--font-slab` | Roboto Slab | Wayfinding, meta lines, eyebrows |
| `--font-sans` | system stack | Micro-copy, tags |

---

## Known scope gaps

- **Routing / deep links.** Navigation is state-based, so the URL never
  changes. Adding `react-router-dom` plus the existing `_redirects` rule
  would enable shareable URLs like `/show/harry-potter-and-the-cursed-child__palace`.
- **Tier 2 data fields.** Genre, run-status ("FINAL WEEKS", "PREVIEWS"),
  cast, reviews, promotion labels, age guidance, and access info all
  exist in the individual scraper JSONs but are dropped by `dedupe.py`
  today. The UI has shims (`deriveGenre`, `deriveOnOffer`) that return
  `undefined` until plumbing lands — components hide badges rather
  than crash.
- **Mobile sidebar polish.** Below 640px the sidebar collapses to a
  horizontal bar across the top. Stopgap, not a finished design.
- **Search faceting.** Just title/venue right now. Genre, price-band,
  and run-length filters unblock once Tier 2 lands.
- **Bundle optimisation for 24MB JSON.** Acceptable for launch; revisit
  if mobile parse times prove painful.

---

## License

Internal project. Not for distribution.
