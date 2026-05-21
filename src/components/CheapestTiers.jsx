import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// Budget tiers: four price-band tiles showing the shape of the
// catalogue. Each tile shows how many shows have at least one
// performance in that band, plus the headline (cheapest) show in
// that band. The intent is the user thinks in budget ("I have £30")
// not in deciles.

export default function CheapestTiers({ tiers }) {
  if (!tiers || tiers.tiers.length === 0) return null
  const totalShows = tiers.tiers.reduce((s, t) => s + t.count, 0)

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="BY BUDGET · NEXT 30 DAYS"
        sub={`${totalShows} shows have at least one performance under that ceiling`}
      />

      <div className="stg-tier-grid">
        {tiers.tiers.map((tier) => (
          <TierTile key={tier.id} tier={tier} />
        ))}
      </div>
    </section>
  )
}

function TierTile({ tier }) {
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
    <ShowLink id={tier.headline.show.id} className="stg-tier-tile">
      <div className="stg-tier-tile-lbl">{tier.label}</div>
      <div className="stg-tier-tile-count">{tier.count}</div>
      <div className="stg-tier-tile-sub">
        {tier.count === 1 ? 'show' : 'shows'} in this band
      </div>
      <div className="stg-tier-tile-rule" />
      <div className="stg-tier-tile-headline-lbl">CHEAPEST IN BAND</div>
      <div className="stg-tier-tile-headline">{tier.headline.show.title}</div>
      <div className="stg-tier-tile-headline-meta">
        {tier.headline.show.venue} · {tier.headline.dayLabel} ·{' '}
        <span className="stg-tier-tile-headline-price">
          {formatPrice(tier.headline.price)}
        </span>
      </div>
    </ShowLink>
  )
}
