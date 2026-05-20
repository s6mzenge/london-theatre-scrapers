import { useState, useMemo } from 'react'
import { formatPrice } from '../lib/format.js'

// Search is the title/venue lookup tab. Filter input + three sort modes.
// Designed as the bare-minimum useful version; richer faceting (genre,
// price band, run-length filters) blocks on Tier 2 plumbing.

export default function Search({ data, onSelectShow }) {
  const [query, setQuery] = useState('')
  const [sortBy, setSortBy] = useState('title')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    let list = data.shows
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
  }, [data.shows, query, sortBy])

  return (
    <div className="stg-search">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">SEARCH · ALL SHOWS</div>
          <h1 className="stg-mast-h">Find a show.</h1>
        </div>
        <div className="stg-mast-date">
          {data.shows.length} shows in catalogue
        </div>
      </header>

      <div className="stg-search-controls">
        <input
          type="text"
          className="stg-search-input"
          placeholder="Search by title or venue…"
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
          <div
            key={show.id}
            className="stg-search-row"
            onClick={() => onSelectShow(show.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') onSelectShow(show.id)
            }}
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
          </div>
        ))}
        {filtered.length === 0 && (
          <div className="stg-empty">
            {query
              ? `No shows matched "${query}".`
              : 'No shows in this catalogue yet.'}
          </div>
        )}
      </div>
    </div>
  )
}
