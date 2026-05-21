import { SectionHead } from './Cheapest.jsx'
import { formatPrice, sellerLabel } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

export default function CheapestTonight({ tonight }) {
  if (!tonight || tonight.performances.length === 0) {
    return (
      <section className="stg-section">
        <SectionHead
          eyebrow="TONIGHT"
          sub="No performances found for today"
          statLabel="FLOOR"
          stat="—"
        />
        <div className="stg-empty">
          Nothing&rsquo;s playing tonight in the latest scrape. Check back
          tomorrow.
        </div>
      </section>
    )
  }

  const [hero, ...rest] = tonight.performances
  const alts = rest.slice(0, 4)

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`TONIGHT · ${tonight.dateLabel.toUpperCase()}`}
        sub={`${tonight.totalShowsCount} shows playing · ${tonight.underThirtyFiveCount} under £35 · cheapest below`}
        statLabel="FLOOR"
        stat={formatPrice(tonight.floor)}
      />

      <div className="stg-tonight-grid">
        {/* The dark hero card — the single cheapest seat in London tonight */}
        <ShowLink id={hero.show.id} className="stg-hero">
          <div>
            <div className="stg-hero-eyebrow">
              TONIGHT&rsquo;S CHEAPEST{hero.perf.time && ` · ${hero.perf.time}`}
            </div>
            <div className="stg-hero-title">{hero.show.title}</div>
            <div className="stg-hero-meta">
              {hero.show.venue} · {hero.perf.sellerCount} sellers compared
              {hero.perf.priceRange && ` · ${hero.perf.priceRange} range`}
            </div>
          </div>
          <div className="stg-hero-foot">
            <div className="stg-hero-tags">
              {hero.show.genre && (
                <span className="stg-tag stg-tag-dark-genre">
                  {hero.show.genre.toUpperCase()}
                </span>
              )}
              {hero.show.onOffer && (
                <span className="stg-tag stg-tag-offer">ON OFFER</span>
              )}
            </div>
            <div className="stg-hero-price">
              <div className="stg-hero-price-num">
                {formatPrice(hero.perf.minPrice)}
              </div>
              {hero.perf.cheapestSeller && (
                <div className="stg-hero-price-via">
                  VIA {sellerLabel(hero.perf.cheapestSeller).toUpperCase()}
                </div>
              )}
            </div>
          </div>
        </ShowLink>

        {/* Compact list of next-cheapest performances tonight */}
        <div className="stg-alt-list">
          {alts.map((item) => (
            <ShowLink
              key={`${item.show.id}-${item.perf.time}`}
              id={item.show.id}
              className="stg-alt"
            >
              <div className="stg-alt-body">
                <div className="stg-alt-title">{item.show.title}</div>
                <div className="stg-alt-meta">
                  {trimVenue(item.show.venue)}
                  {item.perf.time && ` · ${item.perf.time}`}
                  {item.show.genre && ` · ${item.show.genre.toUpperCase()}`}
                </div>
              </div>
              <div className="stg-alt-price">
                {formatPrice(item.perf.minPrice)}
              </div>
            </ShowLink>
          ))}
        </div>
      </div>
    </section>
  )
}

// Venue names typically include " Theatre" — keep the line lighter by
// dropping the suffix in compact cards.
function trimVenue(venue) {
  if (!venue) return ''
  return venue.replace(/ Theatre$/i, '')
}
