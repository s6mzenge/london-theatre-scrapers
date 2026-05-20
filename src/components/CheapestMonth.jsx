import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'

const LEGEND_BUCKETS = [
  'rgba(163, 50, 42, 0.55)',
  'rgba(163, 50, 42, 0.32)',
  'rgba(163, 50, 42, 0.18)',
  'rgba(163, 50, 42, 0.08)',
  'rgba(28, 26, 23, 0.03)',
]

export default function CheapestMonth({ month, onSelectShow }) {
  if (!month || !month.weeks.length) return null

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

      {/* Calendar heatmap: 4–6 week rows × 7 day columns */}
      <div className="stg-cal">
        <div className="stg-cal-dow">MON</div>
        <div className="stg-cal-dow">TUE</div>
        <div className="stg-cal-dow">WED</div>
        <div className="stg-cal-dow">THU</div>
        <div className="stg-cal-dow">FRI</div>
        <div className="stg-cal-dow">SAT</div>
        <div className="stg-cal-dow">SUN</div>

        {month.weeks.flat().map((cell, idx) => {
          if (cell.padding) {
            return <div key={`pad-${idx}`} className="stg-calcell padding" />
          }
          const classes = [
            'stg-calcell',
            cell.bucket != null ? `bucket-${cell.bucket + 1}` : '',
            cell.isPast ? 'past' : '',
            cell.isToday ? 'today' : '',
          ]
            .filter(Boolean)
            .join(' ')
          return (
            <div
              key={cell.iso}
              className={classes}
              title={
                cell.isDark
                  ? 'No shows'
                  : `Floor £${Math.round(cell.floor)} on ${cell.iso}`
              }
            >
              <div className="stg-calcell-d">{cell.dayOfMonth}</div>
              <div className="stg-calcell-p">
                {cell.isDark ? '—' : formatPrice(cell.floor, { whole: true })}
              </div>
            </div>
          )
        })}
      </div>

      <div className="stg-cal-legend">
        CHEAPEST
        {LEGEND_BUCKETS.map((bg, i) => (
          <span
            key={i}
            className="stg-legend-dot"
            style={{ background: bg }}
          />
        ))}
        PRICIEST
      </div>

      {/* Three insight cards — the "interesting aggregations" */}
      <div className="stg-insights">
        {month.insights.map((ins, i) => (
          <div
            key={i}
            className={`stg-insight ${ins.showId ? 'clickable' : ''}`}
            onClick={() => {
              if (ins.showId) onSelectShow(ins.showId)
            }}
            role={ins.showId ? 'button' : undefined}
            tabIndex={ins.showId ? 0 : undefined}
          >
            <div className="stg-insight-lbl">{ins.label}</div>
            <div className="stg-insight-val">{ins.value}</div>
            <div className="stg-insight-sub">{ins.sub}</div>
          </div>
        ))}
      </div>
    </section>
  )
}
