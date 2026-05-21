import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink, Link } from '../lib/router.jsx'

// Opening soon: shows whose first future performance is in the
// next ~3 weeks. We surface the preview-week floor specifically
// because that's typically the cheapest the show will ever be —
// when there's a real saving vs the rest of the run, the row
// shows it explicitly.

export default function CheapestOpeningSoon({ openingSoon }) {
  if (!openingSoon || openingSoon.rows.length === 0) return null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="OPENING SOON · CATCH PREVIEWS"
        sub={`${openingSoon.totalCount} ${
          openingSoon.totalCount === 1 ? 'show opens' : 'shows open'
        } in the next ${openingSoon.windowDays} days`}
        action={
          openingSoon.totalCount > openingSoon.rows.length && (
            <Link
              href="/shows?filter=opening-soon"
              className="stg-section-action-link"
            >
              SEE ALL {openingSoon.totalCount} →
            </Link>
          )
        }
      />

      <div className="stg-opening-grid">
        {openingSoon.rows.map((row) => (
          <ShowLink
            key={row.show.id}
            id={row.show.id}
            className="stg-opening-card"
          >
            <div className="stg-opening-when">
              {row.daysOut === 0
                ? 'OPENS TODAY'
                : row.daysOut === 1
                  ? 'OPENS TOMORROW'
                  : `OPENS IN ${row.daysOut} DAYS`}
              <div className="stg-opening-date">{row.firstLabel}</div>
            </div>
            <div className="stg-opening-title">{row.show.title}</div>
            <div className="stg-opening-venue">{row.show.venue}</div>
            <div className="stg-opening-foot">
              {row.previewFloor != null && (
                <div className="stg-opening-price">
                  <span className="stg-opening-price-lbl">PREVIEW FROM</span>
                  <span className="stg-opening-price-num">
                    {formatPrice(row.previewFloor)}
                  </span>
                </div>
              )}
              {row.previewSavings != null && (
                <div className="stg-opening-savings">
                  £{row.previewSavings} less
                  <br />
                  than the rest of the run
                </div>
              )}
            </div>
          </ShowLink>
        ))}
      </div>
    </section>
  )
}
