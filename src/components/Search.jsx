import { useState, useMemo, useEffect } from 'react'
import { formatPrice } from '../lib/format.js'
import { todayISO } from '../lib/dates.js'
import { computeShowFilters } from '../lib/data.js'
import { ShowLink, Link, navigate } from '../lib/router.jsx'

// SHOWS is the title/venue lookup plus a row of curated filter chips
// (closing soon, opening soon, limited engagements, hidden gems,
// solo-seller exclusives). Filters compose with the text input, so
// a user can search "rock" within "exclusives" and get the subset.
//
// The active filter is stored in the URL (/shows?filter=foo) so deep
// links work; the chips switch the URL via navigate() and the
// useRoute hook upstream re-renders this component with the new
// `filter` prop.

const FILTER_DEFS = [
  { id: null, label: 'ALL' },
  { id: 'closing-soon', label: 'CLOSING SOON', set: 'closingSoon' },
  { id: 'opening-soon', label: 'OPENING SOON', set: 'openingSoon' },
  { id: 'limited', label: 'LIMITED RUN', set: 'limited' },
  { id: 'gems', label: 'HIDDEN GEMS', set: 'hiddenGems' },
  { id: 'exclusives', label: 'EXCLUSIVES', set: 'exclusives' },
]

// Editorial sub-line for each filter — keeps the SHOWS tab in
// editorial voice when a filter is active.
const FILTER_SUBLINES = {
  null: 'Find a show.',
  'closing-soon': 'Last chance — these runs end within 30 days.',
  'opening-soon': 'New runs opening in the next three weeks.',
  limited: 'Fewer than 30 performances left on the books.',
  gems: 'Cheap (sub-£25) and lightly covered — under-the-radar finds.',
  exclusives: 'Listed by only one seller in our coverage.',
}

const FILTER_HEADS = {
  null: 'SEARCH · ALL SHOWS',
  'closing-soon': 'SHOWS · CLOSING SOON',
  'opening-soon': 'SHOWS · OPENING SOON',
  limited: 'SHOWS · LIMITED RUNS',
  gems: 'SHOWS · HIDDEN GEMS',
  exclusives: 'SHOWS · SOLO-SELLER EXCLUSIVES',
}

export default function Search({ data, filter }) {
  const [query, setQuery] = useState('')
  const [sortBy, setSortBy] = useState('title')

  // The filter prop comes from the URL (?filter=…). On chip click
  // we push a new URL; the route hook re-renders with the new
  // prop. Same for clearing the filter.
  const setFilter = (next) => {
    if (next) {
      navigate(`/shows?filter=${next}`)
    } else {
      navigate('/shows')
    }
  }

  // Reset text search when the filter changes — the input was likely
  // tailored to the previous slice and most users won't want it to
  // persist across a filter switch.
  useEffect(() => {
    setQuery('')
  }, [filter])

  const today = todayISO()
  const filterSets = useMemo(() => computeShowFilters(data, today), [data, today])

  // Pre-compute counts for each chip so users see the slice size
  // before they click.
  const counts = useMemo(
    () => ({
      null: data.shows.length,
      'closing-soon': filterSets.closingSoon.size,
      'opening-soon': filterSets.openingSoon.size,
      limited: filterSets.limited.size,
      gems: filterSets.hiddenGems.size,
      exclusives: filterSets.exclusives.size,
    }),
    [data.shows.length, filterSets],
  )

  const filtered = useMemo(() => {
    let list = data.shows
    // Apply chip filter first.
    if (filter) {
      const def = FILTER_DEFS.find((d) => d.id === filter)
      const set = def ? filterSets[def.set] : null
      if (set) list = list.filter((s) => set.has(s.id))
    }
    // Then text search.
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter(
        (s) =>
          s.title.toLowerCase().includes(q) ||
          (s.venue || '').toLowerCase().includes(q),
      )
    }
    // Sort. For price sort, treat invalid (null or ≤0) prices as
    // "missing" and push them to the end rather than letting £0 wins
    // dominate the top.
    const priceOrNull = (s) =>
      typeof s.min_price_gbp === 'number' && s.min_price_gbp > 0
        ? s.min_price_gbp
        : Infinity
    return [...list].sort((a, b) => {
      if (sortBy === 'price') {
        return priceOrNull(a) - priceOrNull(b)
      }
      if (sortBy === 'venue') {
        return (a.venue || '').localeCompare(b.venue || '')
      }
      return a.title.localeCompare(b.title)
    })
  }, [data.shows, query, sortBy, filter, filterSets])

  const headKey = filter || 'null'
  const subKey = filter || 'null'

  return (
    <div className="stg-search">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">{FILTER_HEADS[headKey]}</div>
          <h1 className="stg-mast-h">{FILTER_SUBLINES[subKey]}</h1>
        </div>
        <div className="stg-mast-date">
          {filtered.length} of {data.shows.length} shows
        </div>
      </header>

      <div className="stg-chip-row">
        {FILTER_DEFS.map((def) => {
          const active = (filter || null) === def.id
          const count = counts[def.id || 'null']
          return (
            <button
              key={def.id || '__all'}
              type="button"
              className={`stg-chip ${active ? 'active' : ''}`}
              onClick={() => setFilter(def.id)}
            >
              <span className="stg-chip-lbl">{def.label}</span>
              <span className="stg-chip-count">{count}</span>
            </button>
          )
        })}
      </div>

      <div className="stg-search-controls">
        <input
          type="text"
          className="stg-search-input"
          placeholder={
            filter
              ? `Search within ${filter.replace('-', ' ')}…`
              : 'Search by title or venue…'
          }
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="stg-search-sort">
          {[
            ['title', 'A–Z'],
            ['price', 'CHEAPEST'],
            ['venue', 'VENUE'],
          ].map(([id, label]) => (
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

      <div className="stg-search-results">
        {filtered.map((show) => (
          <ShowLink
            key={show.id}
            id={show.id}
            className="stg-search-row"
          >
            <div className="stg-search-row-body">
              <div className="stg-search-row-title">{show.title}</div>
              <div className="stg-search-row-meta">
                {show.venue || 'Venue TBC'} · {show.source_count} sellers ·{' '}
                {show.performance_count} performances
              </div>
            </div>
            <div className="stg-search-row-price">
              {typeof show.min_price_gbp === 'number' && show.min_price_gbp > 0 ? (
                <>
                  <span className="stg-search-row-from">FROM</span>
                  <span className="stg-search-row-num">
                    {formatPrice(show.min_price_gbp)}
                  </span>
                </>
              ) : (
                <span className="stg-search-row-from">PRICE TBC</span>
              )}
            </div>
          </ShowLink>
        ))}
        {filtered.length === 0 && (
          <div className="stg-empty">
            {query
              ? `No shows matched "${query}"${
                  filter ? ` in this slice` : ''
                }.`
              : filter
                ? 'No shows in this slice right now.'
                : 'No shows in this catalogue yet.'}
          </div>
        )}
      </div>
    </div>
  )
}
