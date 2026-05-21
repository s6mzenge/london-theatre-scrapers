import { useMemo } from 'react'
import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import {
  todayISO,
  formatLongDate,
  parseISO,
} from '../lib/dates.js'
import {
  computeDayOfWeekHeatmap,
  computeLongCalendar,
} from '../lib/data.js'
import { Link } from '../lib/router.jsx'

// WHEN is the planning surface. Two questions:
//   1. "If I'm flexible on day of the week, when should I go?"
//      → Day-of-week heatmap.
//   2. "What does the next quarter look like as a single artefact?"
//      → 90-day calendar strip.
// Clicking any day in either view deep-links to /when/:date for the
// per-date page (rendered by WhenDate.jsx).

export default function When({ data }) {
  const today = todayISO()
  const dow = useMemo(
    () => computeDayOfWeekHeatmap(data, today, 60),
    [data, today],
  )
  const longCal = useMemo(
    () => computeLongCalendar(data, today, 90),
    [data, today],
  )

  return (
    <div className="stg-when">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">WHEN · PLAN A NIGHT</div>
          <h1 className="stg-mast-h">Pick a night, see what&rsquo;s on.</h1>
        </div>
        <div className="stg-mast-date">{formatLongDate(today)}</div>
      </header>

      <DayOfWeekHeatmap dow={dow} />
      <LongCalendarStrip longCal={longCal} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Day-of-week heatmap: seven cells, one per weekday, with each cell's
// height bar encoding median floor for that weekday over the window.
// ---------------------------------------------------------------------------

function DayOfWeekHeatmap({ dow }) {
  if (!dow || !dow.cheapest) return null
  const range = dow.max - dow.min || 1
  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`AVERAGE FLOOR BY WEEKDAY · NEXT ${dow.windowDays} DAYS`}
        sub={
          dow.cheapest && dow.priciest
            ? `${dow.cheapest.dow}s are typically cheapest · ` +
              `${dow.priciest.dow}s priciest`
            : 'Median cheapest seat by day of week'
        }
        statLabel="GAP"
        stat={dow.max != null ? `£${Math.round(dow.max - dow.min)}` : '—'}
      />

      <div className="stg-dow-grid">
        {dow.cells.map((cell) => {
          if (cell.median == null) {
            return (
              <div key={cell.dow} className="stg-dow-cell dark">
                <div className="stg-dow-cell-dow">{cell.dow}</div>
                <div className="stg-dow-cell-bar-wrap">
                  <div className="stg-dow-cell-bar zero" />
                </div>
                <div className="stg-dow-cell-price">—</div>
                <div className="stg-dow-cell-count">NO DATA</div>
              </div>
            )
          }
          // Invert: cheap = tall brick, expensive = short brick.
          const heightPct =
            80 - ((cell.median - dow.min) / range) * 60 + 20
          const isMin = cell === dow.cheapest
          const isMax = cell === dow.priciest
          return (
            <div
              key={cell.dow}
              className={`stg-dow-cell ${isMin ? 'min' : ''} ${
                isMax ? 'max' : ''
              }`}
            >
              <div className="stg-dow-cell-dow">{cell.dow}</div>
              <div className="stg-dow-cell-bar-wrap">
                <div
                  className="stg-dow-cell-bar"
                  style={{ height: `${heightPct}%` }}
                />
              </div>
              <div className="stg-dow-cell-price">
                {formatPrice(cell.median, { whole: true })}
              </div>
              <div className="stg-dow-cell-count">
                {cell.count} {cell.count === 1 ? 'NIGHT' : 'NIGHTS'}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// 90-day strip: every day for the next ~3 months as a tall, thin cell.
// Cells wrap to multiple rows so the whole thing fits a normal screen.
// Each cell is a <Link> to /when/:date.
// ---------------------------------------------------------------------------

function LongCalendarStrip({ longCal }) {
  // Group cells by month for the month-anchor row.
  const monthLabels = useMemo(() => {
    const labels = []
    let prev = null
    longCal.cells.forEach((cell, idx) => {
      if (cell.month !== prev) {
        labels.push({ idx, label: cell.monthShort })
        prev = cell.month
      }
    })
    return labels
  }, [longCal])

  if (!longCal || longCal.cells.length === 0) return null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`NEXT ${longCal.windowDays} DAYS · AT A GLANCE`}
        sub="Every night as a single column · click any day to plan"
        statLabel="DARK DAYS"
        stat={`${longCal.cells.filter((c) => c.isDark).length} / ${
          longCal.windowDays
        }`}
      />

      <div className="stg-longcal">
        <div className="stg-longcal-strip">
          {longCal.cells.map((cell) => (
            <Link
              key={cell.iso}
              href={`/when/${cell.iso}`}
              className={[
                'stg-longcal-cell',
                cell.isDark ? 'dark' : `bucket-${cell.bucket}`,
                cell.isWeekend ? 'weekend' : '',
                cell.isToday ? 'today' : '',
              ]
                .filter(Boolean)
                .join(' ')}
              aria-label={
                cell.isDark
                  ? `${cell.iso}: dark, no shows`
                  : `${cell.iso}: floor £${Math.round(cell.floor)} across ${
                      cell.showCount
                    } performances`
              }
            >
              <span className="stg-longcal-cell-bar" />
              <span className="stg-longcal-cell-dom">{cell.dayOfMonth}</span>
            </Link>
          ))}
        </div>
        <div className="stg-longcal-axis">
          {monthLabels.map((m) => (
            <span
              key={m.idx}
              className="stg-longcal-axis-tick"
              style={{ left: `calc(${(m.idx / longCal.windowDays) * 100}%)` }}
            >
              {m.label}
            </span>
          ))}
        </div>
        <div className="stg-longcal-legend">
          <span className="stg-longcal-legend-key">
            <span className="stg-longcal-swatch bucket-1" />
            CHEAPEST
          </span>
          <span className="stg-longcal-legend-key">
            <span className="stg-longcal-swatch bucket-5" />
            PRICIEST
          </span>
          <span className="stg-longcal-legend-key">
            <span className="stg-longcal-swatch dark" />
            DARK
          </span>
        </div>
      </div>

      <div className="stg-when-hint">
        Tip: every cell deep-links to <code>/when/YYYY-MM-DD</code> — copy
        a URL to share a date with someone.
      </div>
    </section>
  )
}
