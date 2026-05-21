import { useMemo } from 'react'
import { computeMethodology } from '../lib/data.js'
import { sellerLabel } from '../lib/format.js'
import { relativeTime } from '../lib/dates.js'

// DATA. The transparency page — what's tracked, how fresh, what the
// dedupe pipeline did to merge sources, and why some shows appear
// as "PRICE TBC". The page is intentionally text-heavy; it's the
// methodology document, not a deal-finding surface.

export default function Data({ data }) {
  const meta = useMemo(() => computeMethodology(data), [data])

  return (
    <div className="stg-data">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">DATA · METHODOLOGY</div>
          <h1 className="stg-mast-h">What we&rsquo;re comparing.</h1>
        </div>
        <div className="stg-mast-date">
          {meta.showCount} shows · {meta.performanceCount?.toLocaleString()}{' '}
          performances
        </div>
      </header>

      {/* Coverage section */}
      <section className="stg-section">
        <div className="stg-section-head">
          <div className="stg-section-left">
            <div className="stg-section-eyebrow">COVERAGE · BY SELLER COUNT</div>
            <div className="stg-section-sub">
              How many sellers have each show listed
            </div>
          </div>
          <div className="stg-section-stat-wrap">
            <div className="stg-section-stat-lbl">WELL-COVERED</div>
            <div className="stg-section-stat">
              {Math.round(
                (meta.wellCoveredCount / meta.totalShowsInCoverage) * 100,
              )}
              %
            </div>
          </div>
        </div>

        <div className="stg-coverage-bars">
          {meta.coverageRows.map((row) => {
            const pct = (row.count / meta.totalShowsInCoverage) * 100
            return (
              <div key={row.sellers} className="stg-coverage-row">
                <div className="stg-coverage-lbl">
                  {row.sellers} {row.sellers === 1 ? 'SELLER' : 'SELLERS'}
                </div>
                <div className="stg-coverage-bar-wrap">
                  <div
                    className={`stg-coverage-bar bucket-${
                      6 - Math.min(5, row.sellers)
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="stg-coverage-num">{row.count}</div>
              </div>
            )
          })}
        </div>
        <div className="stg-data-note">
          Shows with three or more sellers get a meaningful price comparison;
          one- and two-seller shows still appear in the catalogue but their
          &ldquo;cheapest&rdquo; figure is whatever the single seller quoted.
        </div>
      </section>

      {/* Per-seller freshness */}
      <section className="stg-section">
        <div className="stg-section-head">
          <div className="stg-section-left">
            <div className="stg-section-eyebrow">SELLERS · LAST SEEN</div>
            <div className="stg-section-sub">
              When each source was last scraped and how many shows they listed
            </div>
          </div>
        </div>

        <div className="stg-data-seller-list">
          {meta.sellerRows.map((s) => (
            <div key={s.sellerId} className="stg-data-seller-row">
              <div className="stg-data-seller-name">
                {sellerLabel(s.sellerId)}
              </div>
              <div className="stg-data-seller-count">
                {s.showCount?.toLocaleString() || '—'}{' '}
                <span className="stg-data-seller-count-lbl">shows</span>
              </div>
              <div className="stg-data-seller-when">
                {relativeTime(s.scrapedAt)}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Dedupe stats */}
      <section className="stg-section">
        <div className="stg-section-head">
          <div className="stg-section-left">
            <div className="stg-section-eyebrow">DEDUPE · MERGE PIPELINE</div>
            <div className="stg-section-sub">
              How the five sources collapse into one catalogue
            </div>
          </div>
        </div>

        <div className="stg-data-stages">
          <div className="stg-data-stage">
            <div className="stg-data-stage-num">
              {meta.normClusters != null
                ? meta.normClusters.toLocaleString()
                : '—'}
            </div>
            <div className="stg-data-stage-lbl">
              EXACT-NORMALISED CLUSTERS
            </div>
            <div className="stg-data-stage-sub">
              Identical title+venue across sources after lowercasing,
              de-punctuating and stripping noise words
            </div>
          </div>
          <div className="stg-data-stage">
            <div className="stg-data-stage-num">
              {meta.fuzzyMergesCount.toLocaleString()}
            </div>
            <div className="stg-data-stage-lbl">FUZZY MERGES</div>
            <div className="stg-data-stage-sub">
              Near-matches collapsed by token-set similarity ≥ 92
              ({'"'}Cursed Child One Part{'"'} ↔ {'"'}Cursed Child{'"'})
            </div>
          </div>
          <div className="stg-data-stage">
            <div className="stg-data-stage-num">
              {meta.priceTbcCount.toLocaleString()}
            </div>
            <div className="stg-data-stage-lbl">PRICE TBC SHOWS</div>
            <div className="stg-data-stage-sub">
              In the catalogue but no seller surfaced a valid price —
              usually awaiting an on-sale date
            </div>
          </div>
        </div>
      </section>

      {/* Methodology prose */}
      <section className="stg-section stg-data-prose">
        <div className="stg-section-head">
          <div className="stg-section-left">
            <div className="stg-section-eyebrow">METHODOLOGY</div>
            <div className="stg-section-sub">
              How the cheapest figure is decided
            </div>
          </div>
        </div>

        <div className="stg-data-paragraphs">
          <p>
            <b>Effective cheapest.</b> For every performance we walk each
            seller&rsquo;s listed &ldquo;from&rdquo; price, drop anything
            non-positive (a few sources emit £0 to mean &ldquo;price
            unknown&rdquo; rather than &ldquo;free&rdquo;), and take the
            minimum. That minimum drives every aggregation on the site —
            the Tonight floor, the day-of-week heatmap, the venue
            cheapest-from, all of it.
          </p>
          <p>
            <b>Cheapest-wins counts.</b> A seller &ldquo;wins&rdquo; a
            performance when their valid price is tied for the lowest.
            Ties give credit to every winner, so a Saturday matinée
            listed at £24 on both LOVE and TTD counts as one win each —
            not half a win.
          </p>
          <p>
            <b>Floors are snapshot floors.</b> The site rebuilds nightly
            against the latest scrape. A &ldquo;floor&rdquo; means: this
            was the cheapest seat any seller surfaced at the time of the
            scrape. It does not factor in fees, dynamic surge pricing
            that hits at checkout, or release-window stock changes
            between the scrape and your booking.
          </p>
          <p>
            <b>Catalogue vs. site listings.</b> The catalogue numbers in
            the SELLERS tab count shows each source emitted at scrape
            time. The site itself hides any show whose entire run is in
            the past, so the catalogue figure will usually be higher
            than what you see in SHOWS.
          </p>
          <p>
            <b>Source code.</b> The full scraping, dedupe and rendering
            pipeline is open. See the project repository for scraper
            specifics and the dedupe pipeline&rsquo;s override file.
          </p>
        </div>
      </section>
    </div>
  )
}
