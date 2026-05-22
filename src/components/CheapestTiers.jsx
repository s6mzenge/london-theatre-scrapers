import { Fragment, useState } from 'react'
import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// Budget tiers: four price-band tiles. Each tile is a button that
// expands a drill-down listing every show in that band, cheapest
// first. Mirrors the SELLERS tab pattern (click card → see what's
// under it). Only one tier expanded at a time; click an active tile
// again to collapse.
//
// The drill is rendered *inside* the grid, immediately after the
// active tile, with `grid-column: 1 / -1` in CSS so it spans the full
// row on multi-column desktops/tablets. On a single-column mobile
// layout this puts the drill directly under the tapped tile — no more
// scrolling past every other tile to find it.

export default function CheapestTiers({ tiers }) {
  const [activeId, setActiveId] = useState(null)
  if (!tiers || tiers.tiers.length === 0) return null
  const totalShows = tiers.tiers.reduce((s, t) => s + t.count, 0)

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="BY BUDGET · NEXT 30 DAYS"
        sub={
          `${totalShows} shows have at least one performance under that ceiling` +
          ` · click a tile for the full list`
        }
      />

      <div className="stg-tier-grid">
        {tiers.tiers.map((tier) => {
          const isActive = activeId === tier.id
          const showDrill = isActive && tier.shows && tier.shows.length > 0
          return (
            <Fragment key={tier.id}>
              <TierTile
                tier={tier}
                isActive={isActive}
                onClick={() =>
                  setActiveId(isActive ? null : tier.id)
                }
              />
              {showDrill && <TierDrill tier={tier} />}
            </Fragment>
          )
        })}
      </div>
    </section>
  )
}

function TierTile({ tier, isActive, onClick }) {
  if (!tier.headline) {
    return (
      <div className="stg-tier-tile empty">
        <div className="stg-tier-tile-lbl">{tier.label}</div>
        <div className="stg-tier-tile-count">—</div>
        <div className="stg-tier-tile-sub">
          No shows in this band right now
        </div>
      </div>
    )
  }
  return (
    <button
      type="button"
      className={`stg-tier-tile ${isActive ? 'active' : ''}`}
      onClick={onClick}
      aria-expanded={isActive}
    >
      <div className="stg-tier-tile-lbl">{tier.label}</div>
      <div className="stg-tier-tile-count">{tier.count}</div>
      <div className="stg-tier-tile-sub">
        {tier.count === 1 ? 'show' : 'shows'} in this band
      </div>
      <div className="stg-tier-tile-rule" />
      <div className="stg-tier-tile-headline-lbl">
        {isActive ? 'CLICK TO COLLAPSE' : 'CHEAPEST IN BAND'}
      </div>
      <div className="stg-tier-tile-headline">{tier.headline.show.title}</div>
      <div className="stg-tier-tile-headline-meta">
        {tier.headline.show.venue} · {tier.headline.dayLabel} ·{' '}
        <span className="stg-tier-tile-headline-price">
          {formatPrice(tier.headline.price)}
        </span>
      </div>
    </button>
  )
}

// Drill-down mirrors the DayDrill row pattern (rank · body · price)
// minus the "vs other dates" middle column — every row in this list
// is already "this show's cheapest in the next 30 days", so a
// comparison column would be redundant.
function TierDrill({ tier }) {
  return (
    <div className="stg-tier-drill">
      <div className="stg-tier-drill-head">
        <div className="stg-tier-drill-eye">
          ALL {tier.count} SHOWS · <b>{tier.label}</b>
        </div>
        <div className="stg-tier-drill-r-lbl">CHEAPEST IN WINDOW</div>
      </div>
      {tier.shows.map((row, idx) => (
        <ShowLink
          key={row.show.id}
          id={row.show.id}
          className="stg-tier-drill-row"
        >
          <div className="stg-tier-drill-rank">
            {String(idx + 1).padStart(2, '0')}
          </div>
          <div className="stg-tier-drill-body">
            <div className="stg-tier-drill-title">{row.show.title}</div>
            <div className="stg-tier-drill-meta">
              {row.show.venue} · cheapest on {row.dayLabel}
            </div>
          </div>
          <div className="stg-tier-drill-price">
            {formatPrice(row.price)}
          </div>
        </ShowLink>
      ))}
    </div>
  )
}
