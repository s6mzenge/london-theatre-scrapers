import { useMemo, useState } from 'react'
import { SectionHead } from './Cheapest.jsx'
import DayDrill from './DayDrill.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// Legend bar widths, in pixels, ramping cheapest → priciest. The same
// width-by-bucket scale used inline on each calendar cell's tick.
const LEGEND_WIDTHS = [30, 24, 18, 14, 10]

export default function CheapestMonth({ month }) {
  // Month default is "nothing selected" rather than today — this keeps
  // the calendar the visual centerpiece and avoids visually duplicating
  // the week strip's already-open today drill-down on first paint.
  const [selectedIso, setSelectedIso] = useState(null)

  // Flatten the week rows once so we can look up the selected cell by iso.
  const flatCells = useMemo(
    () => (month ? month.weeks.flat() : []),
    [month],
  )

  if (!month || !month.weeks.length) return null

  const selectedDay =
    selectedIso != null
      ? flatCells.find((c) => !c.padding && c.iso === selectedIso) || null
      : null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`THIS MONTH · ${month.label.toUpperCase()}`}
        sub="Floor price per day across all shows"
        statLabel="MONTH FLOOR"
        stat={
          month.monthFloor.price != null
            ? `${formatPrice(month.monthFloor.price)} · ${month.monthFloor.dayLabel}`
            : '—'
        }
      />

      {/* Calendar grid: hairline-divided cells (no full-cell fills).
          Each non-dark, non-past cell renders a brick "tick" under
          its price — width + opacity encode the bucket. Click any
          future date to open its drill-down below. */}
      <div className="stg-cal">
        <div className="stg-cal-dow">MON</div>
        <div className="stg-cal-dow">TUE</div>
        <div className="stg-cal-dow">WED</div>
        <div className="stg-cal-dow">THU</div>
        <div className="stg-cal-dow">FRI</div>
        <div className="stg-cal-dow">SAT</div>
        <div className="stg-cal-dow">SUN</div>

        {flatCells.map((cell, idx) => {
          if (cell.padding) {
            return <div key={`pad-${idx}`} className="stg-calcell padding" />
          }
          const clickable = !cell.isPast && !cell.isDark
          const isSelected = cell.iso === selectedIso
          const classes = [
            'stg-calcell',
            cell.bucket != null ? `bucket-${cell.bucket + 1}` : 'dark',
            cell.isPast ? 'past' : '',
            cell.isToday ? 'today' : '',
            isSelected ? 'sel' : '',
            clickable ? '' : 'nonclick',
          ]
            .filter(Boolean)
            .join(' ')

          // Use a real <button> for clickable cells, a plain <div>
          // for past/dark/padding so screen readers don't announce
          // them as actionable.
          if (clickable) {
            return (
              <button
                key={cell.iso}
                type="button"
                className={classes}
                onClick={() => setSelectedIso(cell.iso)}
                aria-pressed={isSelected}
                aria-label={`${cell.iso}: floor £${Math.round(cell.floor)}`}
              >
                <div className="stg-calcell-d">{cell.dayOfMonth}</div>
                <div className="stg-calcell-foot">
                  <div className="stg-calcell-p">
                    {formatPrice(cell.floor, { whole: true })}
                  </div>
                  {cell.bucket != null && (
                    <div
                      className={`stg-calcell-tick bucket-${cell.bucket + 1}`}
                    />
                  )}
                </div>
              </button>
            )
          }

          return (
            <div key={cell.iso} className={classes}>
              <div className="stg-calcell-d">{cell.dayOfMonth}</div>
              <div className="stg-calcell-foot">
                <div className="stg-calcell-p">
                  {cell.isDark ? '—' : formatPrice(cell.floor, { whole: true })}
                </div>
                {cell.bucket != null && (
                  <div
                    className={`stg-calcell-tick bucket-${cell.bucket + 1}`}
                  />
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div className="stg-cal-legend">
        <span className="stg-cal-legend-lbl">CHEAPEST</span>
        {LEGEND_WIDTHS.map((w, i) => (
          <span
            key={i}
            className={`stg-cal-legend-bar bucket-${i + 1}`}
            style={{ width: `${w}px` }}
          />
        ))}
        <span className="stg-cal-legend-lbl">PRICIEST</span>
      </div>

      {selectedDay && <DayDrill day={selectedDay} />}

      {/* Three insight cards — the "interesting aggregations". Cards
          whose insight is tied to a specific show render as ShowLink
          anchors so they get the full link UX (middle-click, cmd-click,
          right-click → open in new tab). Cards without a showId stay
          as plain divs. */}
      <div className="stg-insights">
        {month.insights.map((ins, i) => {
          if (ins.showId) {
            return (
              <ShowLink
                key={i}
                id={ins.showId}
                className="stg-insight clickable"
              >
                <div className="stg-insight-lbl">{ins.label}</div>
                <div className="stg-insight-val">{ins.value}</div>
                <div className="stg-insight-sub">{ins.sub}</div>
              </ShowLink>
            )
          }
          return (
            <div key={i} className="stg-insight">
              <div className="stg-insight-lbl">{ins.label}</div>
              <div className="stg-insight-val">{ins.value}</div>
              <div className="stg-insight-sub">{ins.sub}</div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
