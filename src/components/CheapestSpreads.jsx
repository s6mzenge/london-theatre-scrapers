import { SectionHead } from './Cheapest.jsx'
import { formatPrice, sellerLabel } from '../lib/format.js'
import { ShowLink, Link } from '../lib/router.jsx'

// Widest spreads: performances in the next 30 days where the gap
// between the cheapest seller and the most-expensive listing for
// the same seat is largest. The visual is a horizontal axis with a
// brick segment showing the gap, anchored cheap-end ↔ dear-end.
// This is the section that justifies the site's whole premise.

export default function CheapestSpreads({ spreads }) {
  if (!spreads || spreads.rows.length === 0) return null

  // Use the widest spread in the list as the axis maximum so every
  // bar is comparable to the same reference. That way "row 1's bar
  // is wider than row 5's bar" is meaningful at a glance.
  const widest = Math.max(...spreads.rows.map((r) => r.spread))

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="WIDEST SPREADS · NEXT 30 DAYS"
        sub="Where comparing sellers actually pays off"
        statLabel="BIGGEST GAP"
        stat={formatPrice(widest)}
        action={
          <Link href="/sellers" className="stg-section-action-link">
            ALL SELLERS →
          </Link>
        }
      />

      <div className="stg-spread-list">
        {spreads.rows.map((row, idx) => (
          <ShowLink
            key={`${row.show.id}-${idx}`}
            id={row.show.id}
            className="stg-spread-row"
          >
            <div className="stg-spread-rank">
              {String(idx + 1).padStart(2, '0')}
            </div>

            <div className="stg-spread-body">
              <div className="stg-spread-title">{row.show.title}</div>
              <div className="stg-spread-meta">
                {row.show.venue} · {row.dayLabel}
                {row.time && ` · ${row.time}`}
              </div>

              <div className="stg-spread-bar">
                <div className="stg-spread-bar-track">
                  <div
                    className="stg-spread-bar-fill"
                    style={{
                      width: `${Math.max(8, (row.spread / widest) * 100)}%`,
                    }}
                  />
                </div>
                <div className="stg-spread-bar-ends">
                  <span>
                    {formatPrice(row.cheap.price)}{' '}
                    <b>{sellerLabel(row.cheap.sellerId).toUpperCase()}</b>
                  </span>
                  <span>
                    {formatPrice(row.dear.price)}{' '}
                    <b>{sellerLabel(row.dear.sellerId).toUpperCase()}</b>
                  </span>
                </div>
              </div>
            </div>

            <div className="stg-spread-stat">
              <div className="stg-spread-stat-lbl">YOU SAVE</div>
              <div className="stg-spread-stat-val">
                {formatPrice(row.spread)}
              </div>
              <div className="stg-spread-stat-pct">{row.pct}% OFF</div>
            </div>
          </ShowLink>
        ))}
      </div>
    </section>
  )
}
