import { useState, useMemo } from 'react'
import { formatPrice } from '../lib/format.js'
import { todayISO } from '../lib/dates.js'
import {
  computeVenues,
  computeVenueDetail,
  slugifyVenue,
} from '../lib/data.js'
import { Link, ShowLink } from '../lib/router.jsx'

// Venues tab: grid of every theatre with show count + floor price.
// Active venues (those with at least one upcoming show) sort first;
// historical venues (everything currently archival) trail. Click
// any card to go to /venues/:slug for the per-venue catalogue page.

export default function Venues({ data }) {
  const [query, setQuery] = useState('')
  const today = todayISO()
  const venues = useMemo(() => computeVenues(data, today), [data, today])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return venues
    return venues.filter((v) => v.displayName.toLowerCase().includes(q))
  }, [venues, query])

  const activeCount = venues.filter((v) => v.showsWithUpcoming > 0).length

  return (
    <div className="stg-venues">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">VENUES · LONDON STAGES</div>
          <h1 className="stg-mast-h">Browse by theatre.</h1>
        </div>
        <div className="stg-mast-date">
          {activeCount} {activeCount === 1 ? 'venue' : 'venues'} active ·{' '}
          {venues.length} tracked
        </div>
      </header>

      <div className="stg-search-controls">
        <input
          type="text"
          className="stg-search-input"
          placeholder="Search by theatre name…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      <div className="stg-venue-grid">
        {filtered.map((venue) => (
          <Link
            key={venue.slug}
            href={`/venues/${encodeURIComponent(venue.slug)}`}
            className={`stg-venue-card ${
              venue.showsWithUpcoming === 0 ? 'inactive' : ''
            }`}
          >
            <div className="stg-venue-name">{venue.displayName}</div>
            <div className="stg-venue-counts">
              <span>
                <b>{venue.showsWithUpcoming}</b>{' '}
                {venue.showsWithUpcoming === 1 ? 'show' : 'shows'}
              </span>
              {venue.perfsThisWeek > 0 && (
                <span>
                  <b>{venue.perfsThisWeek}</b> this week
                </span>
              )}
            </div>
            <div className="stg-venue-foot">
              {venue.floor != null ? (
                <>
                  <span className="stg-venue-floor-lbl">FROM</span>
                  <span className="stg-venue-floor-val">
                    {formatPrice(venue.floor)}
                  </span>
                </>
              ) : (
                <span className="stg-venue-floor-lbl dim">DARK</span>
              )}
            </div>
          </Link>
        ))}
        {filtered.length === 0 && (
          <div className="stg-empty stg-venue-empty">
            No venues matched &ldquo;{query}&rdquo;.
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Per-venue detail (/venues/:slug). Renders every upcoming show
// at the venue, cheapest first.
// ---------------------------------------------------------------------------

export function VenueDetail({ data, slug }) {
  const today = todayISO()
  const venues = useMemo(() => computeVenues(data, today), [data, today])
  const venue = useMemo(
    () => venues.find((v) => v.slug === slug),
    [venues, slug],
  )
  const rows = useMemo(
    () => (venue ? computeVenueDetail(venue, today) : []),
    [venue, today],
  )

  if (!venue) {
    return (
      <div className="stg-state">
        <div className="stg-state-eyebrow">VENUE NOT FOUND</div>
        <div className="stg-state-msg">
          No venue with slug <b>{slug}</b>.
        </div>
        <div className="stg-state-hint">
          The venue may have been renamed in the catalogue, or the slug
          may be wrong. Try the VENUES tab for the current list.
        </div>
      </div>
    )
  }

  const floor = rows.length > 0 && rows[0].floor != null ? rows[0].floor : null

  return (
    <div className="stg-venue-detail">
      <Link href="/venues" className="stg-back">
        ← BACK TO VENUES
      </Link>
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">VENUE</div>
          <h1 className="stg-mast-h">{venue.displayName}</h1>
        </div>
        <div className="stg-mast-date">
          {rows.length} {rows.length === 1 ? 'show' : 'shows'} upcoming ·{' '}
          from {floor != null ? formatPrice(floor) : '—'}
        </div>
      </header>

      {rows.length === 0 ? (
        <div className="stg-empty">
          No upcoming shows at this venue in the current catalogue.
        </div>
      ) : (
        <div className="stg-search-results">
          {rows.map((row) => (
            <ShowLink
              key={row.show.id}
              id={row.show.id}
              className="stg-search-row"
            >
              <div className="stg-search-row-body">
                <div className="stg-search-row-title">{row.show.title}</div>
                <div className="stg-search-row-meta">
                  {row.runLength}{' '}
                  {row.runLength === 1 ? 'performance' : 'performances'} ·
                  runs {formatRange(row.firstIso, row.lastIso)}
                </div>
              </div>
              <div className="stg-search-row-price">
                {row.floor != null ? (
                  <>
                    <span className="stg-search-row-from">FROM</span>
                    <span className="stg-search-row-num">
                      {formatPrice(row.floor)}
                    </span>
                  </>
                ) : (
                  <span className="stg-search-row-from">PRICE TBC</span>
                )}
              </div>
            </ShowLink>
          ))}
        </div>
      )}
    </div>
  )
}

// Inline format helper — local to VenueDetail rows. Compact label
// for a run window ("12 JUN – 4 SEP").
function formatRange(firstIso, lastIso) {
  if (!firstIso || !lastIso) return '—'
  const first = new Date(`${firstIso}T12:00:00`)
  const last = new Date(`${lastIso}T12:00:00`)
  const firstM = first
    .toLocaleDateString('en-GB', { month: 'short' })
    .toUpperCase()
  const lastM = last
    .toLocaleDateString('en-GB', { month: 'short' })
    .toUpperCase()
  if (
    first.getFullYear() === last.getFullYear() &&
    first.getMonth() === last.getMonth()
  ) {
    return `${first.getDate()}–${last.getDate()} ${lastM}`
  }
  return `${first.getDate()} ${firstM} – ${last.getDate()} ${lastM}`
}

export { slugifyVenue }
