import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// Matinées: the cheapest afternoon (pre-17:00) performances over the
// next seven days. For each row we also show the "save vs evening"
// delta — how much cheaper this matinée is than the cheapest evening
// performance of the same show in the same window. Editorial point:
// matinées are systematically cheaper than evenings.

export default function CheapestMatinees({ matinees }) {
  if (!matinees || matinees.rows.length === 0) return null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="MATINÉES · THIS WEEK"
        sub={`${matinees.totalPerfs} afternoon performances · ` +
          `the cheaper half of the day`}
        statLabel="MATINÉE FLOOR"
        stat={
          matinees.floor != null ? formatPrice(matinees.floor) : '—'
        }
      />

      <div className="stg-matinee-list">
        {matinees.rows.map((row) => (
          <ShowLink
            key={`${row.show.id}-${row.date}-${row.time}`}
            id={row.show.id}
            className="stg-matinee-row"
          >
            <div className="stg-matinee-when">
              <div className="stg-matinee-day">{row.dayLabel}</div>
              <div className="stg-matinee-time">{row.time}</div>
            </div>
            <div className="stg-matinee-body">
              <div className="stg-matinee-title">{row.show.title}</div>
              <div className="stg-matinee-venue">{row.show.venue}</div>
            </div>
            <div className="stg-matinee-vs">
              {row.savings != null ? (
                <>
                  <span className="stg-matinee-vs-lbl">
                    SAVE VS EVENING
                  </span>
                  <span className="stg-matinee-vs-num">
                    £{row.savings}
                  </span>
                </>
              ) : (
                <span
                  className="stg-matinee-vs-lbl dim"
                  aria-hidden="true"
                />
              )}
            </div>
            <div className="stg-matinee-price">
              {formatPrice(row.price)}
            </div>
          </ShowLink>
        ))}
      </div>
    </section>
  )
}
