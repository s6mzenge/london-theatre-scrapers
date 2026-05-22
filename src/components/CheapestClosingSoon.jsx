import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink, Link } from '../lib/router.jsx'

// Closing soon: shows whose last future performance falls within
// 30 days of today, sorted by urgency. Each row shows days
// remaining as a small countdown badge plus the cheapest seat in
// the remaining run. A "see all" link routes through to the
// /shows?filter=closing-soon catalogue slice.
//
// Edge case: when the last performance is *today*, daysLeft is 0.
// Rendering a "0 DAYS LEFT" badge reads like the show has already
// closed, which is the opposite of the truth — it's the most urgent
// state we have. We swap to a "FINAL DAY" badge for that row.

export default function CheapestClosingSoon({ closingSoon }) {
  if (!closingSoon || closingSoon.rows.length === 0) return null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="CLOSING SOON · LAST CHANCE"
        sub={`${closingSoon.totalCount} ${
          closingSoon.totalCount === 1 ? 'show ends' : 'shows end'
        } in the next ${closingSoon.windowDays} days`}
        action={
          closingSoon.totalCount > closingSoon.rows.length && (
            <Link
              href="/shows?filter=closing-soon"
              className="stg-section-action-link"
            >
              SEE ALL {closingSoon.totalCount} →
            </Link>
          )
        }
      />

      <div className="stg-urgent-list">
        {closingSoon.rows.map((row) => {
          const isFinalDay = row.daysLeft === 0
          return (
            <ShowLink
              key={row.show.id}
              id={row.show.id}
              className="stg-urgent-row"
            >
              <div
                className={
                  'stg-urgent-badge' +
                  (row.daysLeft <= 7 ? ' critical' : '') +
                  (isFinalDay ? ' final' : '')
                }
              >
                {isFinalDay ? (
                  <>
                    <div className="stg-urgent-badge-num">FINAL</div>
                    <div className="stg-urgent-badge-lbl">DAY</div>
                  </>
                ) : (
                  <>
                    <div className="stg-urgent-badge-num">{row.daysLeft}</div>
                    <div className="stg-urgent-badge-lbl">
                      {row.daysLeft === 1 ? 'DAY LEFT' : 'DAYS LEFT'}
                    </div>
                  </>
                )}
              </div>
              <div className="stg-urgent-body">
                <div className="stg-urgent-title">{row.show.title}</div>
                <div className="stg-urgent-meta">
                  {row.show.venue} · ends {row.lastLabel} ·{' '}
                  {row.remaining}{' '}
                  {row.remaining === 1 ? 'performance' : 'performances'} left
                </div>
              </div>
              <div className="stg-urgent-price">
                {row.floor != null ? (
                  <>
                    <span className="stg-urgent-price-lbl">FROM</span>
                    <span className="stg-urgent-price-num">
                      {formatPrice(row.floor)}
                    </span>
                  </>
                ) : (
                  <span className="stg-urgent-price-lbl">PRICE TBC</span>
                )}
              </div>
            </ShowLink>
          )
        })}
      </div>
    </section>
  )
}
