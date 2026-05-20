import { useMemo } from 'react'
import { formatPrice, sellerLabel } from '../lib/format.js'
import { formatShortDate } from '../lib/dates.js'

// Per-show detail page. Shows description + a date×seller price grid.
// Each cell links directly to the seller's booking page for that
// performance (target=_blank). The cheapest cell per row is highlighted.

export default function ShowDetail({ show, onBack }) {
  const performances = useMemo(() => {
    return [...(show.performances || [])].sort((a, b) => {
      if (a.date !== b.date) return a.date.localeCompare(b.date)
      return (a.time || '').localeCompare(b.time || '')
    })
  }, [show])

  // Discover the set of sellers present across this show's performances,
  // so the grid only renders columns for sellers that actually appear.
  const sellers = useMemo(() => {
    const set = new Set()
    for (const p of performances) {
      if (p.sources) for (const k of Object.keys(p.sources)) set.add(k)
    }
    return Array.from(set).sort()
  }, [performances])

  return (
    <div className="stg-show">
      <button type="button" className="stg-back" onClick={onBack}>
        ← BACK
      </button>

      <header className="stg-mast stg-mast-show">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">
            {show.venue || 'Venue TBC'}
          </div>
          <h1 className="stg-mast-h">{show.title}</h1>
          <div className="stg-show-summary">
            {show.performance_count} performances · {show.source_count} sellers
            {typeof show.min_price_gbp === 'number' && show.min_price_gbp > 0 && (
              <>
                {' '}· from <b>{formatPrice(show.min_price_gbp)}</b>
              </>
            )}
            {typeof show.max_price_gbp === 'number' && show.max_price_gbp > 0 && (
              <> up to {formatPrice(show.max_price_gbp)}</>
            )}
          </div>
        </div>
      </header>

      {show.description && (
        <div className="stg-show-desc">{show.description}</div>
      )}

      {sellers.length === 0 ? (
        <div className="stg-empty">No seller data for this show yet.</div>
      ) : (
        <div className="stg-show-grid-wrap">
          <table
            className="stg-show-grid"
            style={{ '--seller-count': sellers.length }}
          >
            <thead>
              <tr>
                <th className="stg-show-grid-h-date">DATE</th>
                <th className="stg-show-grid-h-time">TIME</th>
                {sellers.map((sid) => (
                  <th key={sid} className="stg-show-grid-h-seller">
                    {sellerLabel(sid).toUpperCase()}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {performances.map((p, idx) => {
                // Recompute the cheapest valid price per row, ignoring
                // £0 anomalies (the dedupe-level p.min_price can be
                // polluted by lovetheatre's "price unknown" sentinel).
                let rowCheapest = Infinity
                for (const sid of sellers) {
                  const info = p.sources?.[sid]
                  if (
                    info &&
                    typeof info.price_from === 'number' &&
                    info.price_from > 0 &&
                    info.price_from < rowCheapest
                  ) {
                    rowCheapest = info.price_from
                  }
                }
                return (
                  <tr
                    key={`${p.date}-${p.time}-${idx}`}
                    className="stg-show-grid-row"
                  >
                    <td className="stg-show-grid-c-date">
                      {formatShortDate(p.date)}
                    </td>
                    <td className="stg-show-grid-c-time">{p.time || '—'}</td>
                    {sellers.map((sid) => {
                      const info = p.sources?.[sid]
                      const validFrom =
                        info &&
                        typeof info.price_from === 'number' &&
                        info.price_from > 0
                      if (!validFrom) {
                        return (
                          <td
                            key={sid}
                            className="stg-show-grid-c-cell empty"
                          >
                            —
                          </td>
                        )
                      }
                      const isCheapest = info.price_from === rowCheapest
                      return (
                        <td
                          key={sid}
                          className={`stg-show-grid-c-cell ${isCheapest ? 'cheapest' : ''}`}
                        >
                          {info.book_url ? (
                            <a
                              href={info.book_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="stg-show-grid-c-link"
                            >
                              {formatPrice(info.price_from)}
                            </a>
                          ) : (
                            <span>{formatPrice(info.price_from)}</span>
                          )}
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
