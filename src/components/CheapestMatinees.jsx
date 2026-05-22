import { useState, useEffect, useMemo } from 'react'
import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// Matinées: the cheapest afternoon (pre-17:00) performances bucketed by
// *calendar* week (Mon–Sun). We default to today's week (clamped to
// today→Sun so past matinées don't take up rows) and let the user step
// forward week-by-week with the nav arrows. "Save vs evening" stays
// per-show within the visible week — i.e. if a show has both a matinée
// and an evening this week, we show the delta; otherwise we don't.

export default function CheapestMatinees({ matinees }) {
  // Bail if every week is empty — better than a navigator that walks
  // through dead air. A single empty week is still worth rendering
  // (we'll show a polite empty state inside) so the user can page
  // forward to find a populated one.
  const hasAny = useMemo(
    () => matinees && matinees.weeks?.some((w) => w.rows.length > 0),
    [matinees],
  )
  const [idx, setIdx] = useState(matinees?.defaultIdx ?? 0)

  // Re-anchor when the data refreshes (midnight rollover may shift
  // which week is "current"). Default back to the new defaultIdx.
  useEffect(() => {
    setIdx(matinees?.defaultIdx ?? 0)
  }, [matinees?.defaultIdx])

  if (!matinees || !matinees.weeks?.length || !hasAny) return null

  const total = matinees.weeks.length
  const clamped = Math.max(0, Math.min(idx, total - 1))
  const week = matinees.weeks[clamped]
  const prev = clamped > 0 ? matinees.weeks[clamped - 1] : null
  const next = clamped < total - 1 ? matinees.weeks[clamped + 1] : null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`MATINÉES · ${week.kindLabel}`}
        sub={
          week.totalPerfs > 0
            ? `${week.totalPerfs} afternoon performance` +
              `${week.totalPerfs === 1 ? '' : 's'}` +
              ` · the cheaper half of the day`
            : 'No matinées surfaced in this week'
        }
        statLabel="MATINÉE FLOOR"
        stat={week.floor != null ? formatPrice(week.floor) : '—'}
      />

      {/* Week navigator. Same arrow-anchor-arrow shape as the per-show
          month nav so the visual vocabulary stays consistent. Only
          renders when there's more than one week to page through. */}
      {total > 1 ? (
        <div className="stg-mat-weeknav">
          <button
            type="button"
            className="stg-mat-weeknav-arrow"
            onClick={() => setIdx(clamped - 1)}
            disabled={!prev}
            aria-label={
              prev
                ? `Previous week, ${prev.rangeLabel}`
                : 'No earlier week available'
            }
          >
            ←
          </button>
          <div className="stg-mat-weeknav-label">{week.rangeLabel}</div>
          <button
            type="button"
            className="stg-mat-weeknav-arrow"
            onClick={() => setIdx(clamped + 1)}
            disabled={!next}
            aria-label={
              next ? `Next week, ${next.rangeLabel}` : 'No later week available'
            }
          >
            →
          </button>
        </div>
      ) : (
        <div className="stg-mat-weeknav single">
          <div className="stg-mat-weeknav-label">{week.rangeLabel}</div>
        </div>
      )}

      {week.rows.length === 0 ? (
        <div className="stg-mat-empty">
          No matinées listed for {week.rangeLabel.toLowerCase()}.
        </div>
      ) : (
        <div className="stg-matinee-list">
          {week.rows.map((row) => (
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
      )}
    </section>
  )
}
