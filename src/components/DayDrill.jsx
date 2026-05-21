import { formatPrice } from '../lib/format.js'
import { formatShortDate } from '../lib/dates.js'

// Per-day drill-down panel. Sits flush below the week strip OR the
// month calendar — same component for both surfaces, so the page
// reads as a coherent "click a date, see what's on" surface.
//
// Rows mirror the existing `.stg-bestshow` row pattern (rank · title ·
// vs-context · price) so the drill-down doesn't introduce a new
// component vocabulary; it's "the same row, sliced by date".

export default function DayDrill({ day, onSelectShow }) {
  const dateLabel = formatShortDate(day.iso).toUpperCase()
  const shows = day.cheapestShows || []

  return (
    <div className="stg-day-drill">
      <div className="stg-day-drill-head">
        <div className="stg-day-drill-eye">
          CHEAPEST ON <b>{dateLabel}</b>
        </div>
        <div className="stg-day-drill-r-lbl">PRICES FROM</div>
      </div>

      {shows.length === 0 ? (
        <div className="stg-day-drill-empty">
          {day.isDark
            ? 'No shows playing this day.'
            : 'No availability surfaced for this day.'}
        </div>
      ) : (
        shows.map((entry, idx) => (
          <div
            key={entry.show.id}
            className="stg-day-drill-row"
            onClick={() => onSelectShow(entry.show.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onSelectShow(entry.show.id)
              }
            }}
          >
            <div className="stg-day-drill-rank">
              {String(idx + 1).padStart(2, '0')}
            </div>
            <div className="stg-day-drill-body">
              <div className="stg-day-drill-title">{entry.show.title}</div>
              <div className="stg-day-drill-meta">
                {entry.show.venue}
                {entry.perf.time && ` · ${entry.perf.time}`}
                {entry.show.genre && ` · ${entry.show.genre.toUpperCase()}`}
              </div>
            </div>
            {entry.otherDatesRange ? (
              <div className="stg-day-drill-vs">
                vs other dates
                <br />
                <b>{entry.otherDatesRange}</b>
              </div>
            ) : (
              <div className="stg-day-drill-vs" aria-hidden="true" />
            )}
            <div className="stg-day-drill-price">
              {formatPrice(entry.perf.minPrice)}
            </div>
          </div>
        ))
      )}
    </div>
  )
}
