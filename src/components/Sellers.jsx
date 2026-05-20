import { useState, useMemo } from 'react'
import { formatPrice, sellerLabel } from '../lib/format.js'
import { relativeTime } from '../lib/dates.js'

// Sellers tab: a per-seller leaderboard with two stats per card.
//
// Primary stat — cheapest-wins count: how many performances across the
// catalogue have this seller as the cheapest option? This is what
// matters most for a deal-finder audience ("if I want a deal, check X").
//
// Secondary stats — from data.source_summary: how many shows this seller
// scraped, and when they were last scraped. These tell you how
// comprehensive a source is and how fresh its data is.
//
// Click a seller card to see the list of shows where it wins.

const KNOWN_SELLERS = [
  'seatplan',
  'todaytix',
  'lovetheatre',
  'olt',
  'ttd',
]

export default function Sellers({ data, onSelectShow }) {
  const [active, setActive] = useState(null)

  const cheapestPerSeller = useMemo(() => {
    const out = {}
    for (const sid of KNOWN_SELLERS) out[sid] = new Map()

    for (const show of data.shows) {
      const wins = {}
      for (const perf of show.performances || []) {
        if (!perf.sources) continue
        let cheapestSid = null
        let cheapestPrice = Infinity
        for (const [sid, info] of Object.entries(perf.sources)) {
          // Skip sources with invalid prices (e.g. lovetheatre's £0
          // "price unknown" sentinel). See lib/data.js#validPrice.
          if (
            info &&
            typeof info.price_from === 'number' &&
            info.price_from > 0 &&
            info.price_from < cheapestPrice
          ) {
            cheapestPrice = info.price_from
            cheapestSid = sid
          }
        }
        if (cheapestSid) {
          wins[cheapestSid] = (wins[cheapestSid] || 0) + 1
        }
      }
      for (const [sid, count] of Object.entries(wins)) {
        if (!out[sid]) out[sid] = new Map()
        out[sid].set(show.id, { show, wins: count })
      }
    }

    const final = {}
    for (const [sid, map] of Object.entries(out)) {
      final[sid] = Array.from(map.values()).sort((a, b) => b.wins - a.wins)
    }
    return final
  }, [data])

  const sourceSummary = data.source_summary || {}
  const activeRows = active ? cheapestPerSeller[active] || [] : []

  return (
    <div className="stg-sellers">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">SELLERS · COMPARED NIGHTLY</div>
          <h1 className="stg-mast-h">Who&rsquo;s cheapest where.</h1>
        </div>
        <div className="stg-mast-date">
          {KNOWN_SELLERS.length} sellers tracked
        </div>
      </header>

      <div className="stg-seller-grid">
        {KNOWN_SELLERS.map((sid) => {
          const count = cheapestPerSeller[sid]?.length || 0
          const summary = sourceSummary[sid]
          return (
            <button
              key={sid}
              type="button"
              className={`stg-seller-card ${active === sid ? 'active' : ''}`}
              onClick={() => setActive(active === sid ? null : sid)}
            >
              <div className="stg-seller-label">{sellerLabel(sid)}</div>
              <div className="stg-seller-count">{count}</div>
              <div className="stg-seller-sub">
                shows cheapest here at&nbsp;least&nbsp;once
              </div>
              {summary && (
                <div className="stg-seller-meta">
                  {summary.show_count != null && (
                    <span>scrapes {summary.show_count} shows</span>
                  )}
                  {summary.scraped_at && (
                    <span> &middot; updated {relativeTime(summary.scraped_at)}</span>
                  )}
                </div>
              )}
            </button>
          )
        })}
      </div>

      {active && (
        <div className="stg-seller-detail">
          <div className="stg-seller-detail-head">
            Shows where <b>{sellerLabel(active)}</b> wins
          </div>
          <div className="stg-search-results">
            {activeRows.map((row) => (
              <div
                key={row.show.id}
                className="stg-search-row"
                onClick={() => onSelectShow(row.show.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ')
                    onSelectShow(row.show.id)
                }}
              >
                <div className="stg-search-row-body">
                  <div className="stg-search-row-title">{row.show.title}</div>
                  <div className="stg-search-row-meta">
                    {row.show.venue} · cheapest on {row.wins}{' '}
                    {row.wins === 1 ? 'performance' : 'performances'}
                  </div>
                </div>
                <div className="stg-search-row-price">
                  {typeof row.show.min_price_gbp === 'number' &&
                  row.show.min_price_gbp > 0 ? (
                    <>
                      <span className="stg-search-row-from">FROM</span>
                      <span className="stg-search-row-num">
                        {formatPrice(row.show.min_price_gbp)}
                      </span>
                    </>
                  ) : (
                    <span className="stg-search-row-from">PRICE TBC</span>
                  )}
                </div>
              </div>
            ))}
            {activeRows.length === 0 && (
              <div className="stg-empty">
                No performances yet where {sellerLabel(active)} is cheapest.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
