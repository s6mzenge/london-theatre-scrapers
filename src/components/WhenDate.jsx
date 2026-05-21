import { useState, useMemo } from 'react'
import { formatPrice, sellerLabel } from '../lib/format.js'
import { formatLongDate, parseISO, todayISO } from '../lib/dates.js'
import { computeForDate } from '../lib/data.js'
import { ShowLink, Link } from '../lib/router.jsx'

// Per-date page (/when/:date). Lists every performance on a single
// ISO date, sortable by price / time / venue. The Cheapest tab
// already shows top-5-per-date drills; this is the "give me
// everything" surface for serious planners.

const SORTS = [
  { id: 'price', label: 'CHEAPEST' },
  { id: 'time', label: 'TIME' },
  { id: 'venue', label: 'VENUE' },
  { id: 'title', label: 'A–Z' },
]

export default function WhenDate({ data, dateIso }) {
  const [sortBy, setSortBy] = useState('price')

  const rows = useMemo(() => computeForDate(data, dateIso), [data, dateIso])

  const sorted = useMemo(() => {
    const list = [...rows]
    if (sortBy === 'time') {
      list.sort((a, b) => (a.time || '').localeCompare(b.time || ''))
    } else if (sortBy === 'venue') {
      list.sort((a, b) => (a.show.venue || '').localeCompare(b.show.venue || ''))
    } else if (sortBy === 'title') {
      list.sort((a, b) => a.show.title.localeCompare(b.show.title))
    }
    // 'price' is the default and rows already come price-sorted.
    return list
  }, [rows, sortBy])

  // Validate the date — anything off-window collapses to a friendly state.
  let dateObj = null
  try {
    dateObj = parseISO(dateIso)
  } catch {
    /* invalid date */
  }
  const isValid = dateObj && !Number.isNaN(dateObj.getTime())
  const today = todayISO()
  const isPast = isValid && dateIso < today

  if (!isValid) {
    return (
      <div className="stg-state">
        <div className="stg-state-eyebrow">BAD DATE</div>
        <div className="stg-state-msg">
          <b>{dateIso}</b> isn&rsquo;t a recognisable date.
        </div>
        <div className="stg-state-hint">
          Use YYYY-MM-DD, e.g. <code>/when/2026-06-14</code>.
        </div>
      </div>
    )
  }

  const floor = rows.length > 0 ? rows[0].price : null
  const showCount = new Set(rows.map((r) => r.show.id)).size

  return (
    <div className="stg-whendate">
      <Link href="/when" className="stg-back">
        ← BACK TO WHEN
      </Link>
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">
            {isPast ? 'PAST DATE · ARCHIVAL VIEW' : 'EVERY SHOW · ONE DATE'}
          </div>
          <h1 className="stg-mast-h">{formatLongDate(dateIso)}</h1>
        </div>
        <div className="stg-mast-date">
          {showCount} {showCount === 1 ? 'show' : 'shows'} · from{' '}
          {floor != null ? formatPrice(floor) : '—'}
        </div>
      </header>

      <div className="stg-search-controls">
        <div className="stg-search-sort">
          {SORTS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              className={`stg-sort-btn ${sortBy === id ? 'active' : ''}`}
              onClick={() => setSortBy(id)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="stg-empty">
          {isPast
            ? 'No performances were recorded for this date.'
            : 'No performances surfaced for this date in the current scrape.'}
        </div>
      ) : (
        <div className="stg-search-results">
          {sorted.map((row) => (
            <ShowLink
              key={`${row.show.id}-${row.time}`}
              id={row.show.id}
              className="stg-search-row"
            >
              <div className="stg-search-row-body">
                <div className="stg-search-row-title">{row.show.title}</div>
                <div className="stg-search-row-meta">
                  {row.show.venue}
                  {row.time && ` · ${row.time}`} · {row.sellerCount}{' '}
                  {row.sellerCount === 1 ? 'seller' : 'sellers'}{' '}
                  · via {sellerLabel(row.cheapestSeller)}
                </div>
              </div>
              <div className="stg-search-row-price">
                <span className="stg-search-row-from">FROM</span>
                <span className="stg-search-row-num">
                  {formatPrice(row.price)}
                </span>
              </div>
            </ShowLink>
          ))}
        </div>
      )}
    </div>
  )
}
