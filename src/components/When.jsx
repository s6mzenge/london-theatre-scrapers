import { useMemo } from 'react'
import { SectionHead } from './Cheapest.jsx'
import {
  todayISO,
  formatLongDate,
} from '../lib/dates.js'
import {
  computeBestNightsPicks,
  computeWeekdayDistribution,
  computeMonthlyCalendars,
} from '../lib/data.js'
import { Link } from '../lib/router.jsx'

// WHEN is the planning surface. Three questions, three answers:
//   1. "What's the next cheap night I can grab?"
//      → Best Nights picks: 4 hand-selected dates across the horizon.
//   2. "How does the floor move by day of week?"
//      → Strip plot: one dot per performance night, grouped by weekday.
//   3. "What do the next 3 months look like in one glance?"
//      → Real monthly calendars with proper weekday columns.
// Every clickable surface deep-links to /when/:date.

export default function When({ data }) {
  const today = todayISO()
  const picks = useMemo(
    () => computeBestNightsPicks(data, today, 90),
    [data, today],
  )
  const dist = useMemo(
    () => computeWeekdayDistribution(data, today, 60),
    [data, today],
  )
  const cal = useMemo(
    () => computeMonthlyCalendars(data, today, 90),
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

      <BestNights picks={picks.picks} floorFrom={picks.floorFrom} />
      <WeekdayStripPlot dist={dist} />
      <MonthlyCalendars cal={cal} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Best Nights — 4 hand-picked dates where the floor drops. The first one
// is "featured" (dark stock) to echo the Cheapest hero treatment.
// ---------------------------------------------------------------------------

function BestNights({ picks, floorFrom }) {
  if (!picks || picks.length === 0) return null
  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="BEST NIGHTS COMING UP · NEXT 90 DAYS"
        sub={
          picks.length === 1
            ? 'A hand-picked date where the floor drops'
            : `${picks.length} hand-picked dates where the floor drops`
        }
        statLabel="FLOOR FROM"
        stat={floorFrom != null ? `£${Math.round(floorFrom)}` : '—'}
      />
      <div className="stg-when-picks">
        {picks.map((p) => (
          <Link
            key={p.iso}
            href={`/when/${p.iso}`}
            className={`stg-when-pick ${p.featured ? 'featured' : ''}`}
          >
            <div className="stg-when-pick-tag">{p.tag}</div>
            <div className="stg-when-pick-dow">{p.dow}</div>
            <div className="stg-when-pick-dom">
              {String(p.dayOfMonth).padStart(2, '0')}
            </div>
            <div className="stg-when-pick-month">{p.monthYear}</div>
            <div className="stg-when-pick-foot">
              <div>
                <div className="stg-when-pick-from">FROM</div>
                <div className="stg-when-pick-price">
                  £{Math.round(p.floor)}
                </div>
              </div>
              <div className="stg-when-pick-count">
                {p.showCount} {p.showCount === 1 ? 'show' : 'shows'}
              </div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Strip plot — one dot per performance night, grouped into seven weekday
// columns. Cheap-cluster columns get a subtle brick tint and the dots go
// red; premium columns stay neutral with ink dots. A solid bar marks the
// median, so even when several weekdays share the same median value, the
// horizontal line makes the cluster obvious. Ties at the same price level
// fan out sideways into a small beeswarm rather than stacking invisibly.
// ---------------------------------------------------------------------------

function WeekdayStripPlot({ dist }) {
  if (!dist || !dist.cells || dist.cells.every((c) => c.median == null)) {
    return null
  }

  // Chart geometry. The viewBox is fixed; the SVG scales to its container
  // via width: 100%. Pixel values here are viewBox units, not screen px.
  const W = 580
  const H = 250
  const PAD_L = 38
  const PAD_R = 14
  const PAD_T = 10
  const PAD_B = 40
  const CHART_W = W - PAD_L - PAD_R
  const CHART_H = H - PAD_T - PAD_B
  const COL_W = CHART_W / 7

  // Y-axis scales to data, rounded up to the nearest £10 with headroom so
  // top dots don't kiss the chart edge.
  const yMaxRaw = (dist.maxOverall || 0) + 5
  const Y_MAX = Math.max(20, Math.ceil(yMaxRaw / 10) * 10)
  const yFor = (p) => PAD_T + (1 - p / Y_MAX) * CHART_H
  const colCenter = (i) => PAD_L + COL_W * (i + 0.5)

  const yTicks = []
  for (let p = 0; p <= Y_MAX; p += 10) yTicks.push(p)

  // Beeswarm-style horizontal offsets: when multiple nights share the same
  // floor (e.g. three Tuesdays at £6), fan them sideways around the column
  // centreline so each dot is countable.
  const offsetsFor = (prices) => {
    const groups = {}
    prices.forEach((p, i) => {
      const k = String(p)
      groups[k] = groups[k] || []
      groups[k].push(i)
    })
    const out = new Array(prices.length)
    Object.values(groups).forEach((g) => {
      const step = 5
      g.forEach((idx, j) => {
        out[idx] = (j - (g.length - 1) / 2) * step
      })
    })
    return out
  }

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`FLOOR PER NIGHT BY WEEKDAY · NEXT ${dist.windowDays} DAYS`}
        sub="Every dot is one performance night · bar marks the median"
        statLabel="FLOOR RANGE"
        stat={
          dist.minOverall != null && dist.maxOverall != null
            ? `£${Math.round(dist.minOverall)} – £${Math.round(dist.maxOverall)}`
            : '—'
        }
      />
      {dist.editorialLine && (
        <div className="stg-when-strip-editorial">{dist.editorialLine}</div>
      )}
      <svg
        className="stg-when-strip"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Floor price per performance night, grouped by weekday"
      >
        {dist.cells.map((c, i) =>
          c.cheap ? (
            <rect
              key={`bg-${i}`}
              x={PAD_L + COL_W * i}
              y={PAD_T}
              width={COL_W}
              height={CHART_H}
              className="stg-when-strip-bg-cheap"
            />
          ) : null,
        )}

        {yTicks.map((p) => (
          <line
            key={`grid-${p}`}
            x1={PAD_L}
            x2={W - PAD_R}
            y1={yFor(p)}
            y2={yFor(p)}
            className="stg-when-strip-gridline"
          />
        ))}
        {yTicks.map((p) => (
          <text
            key={`ylbl-${p}`}
            x={PAD_L - 6}
            y={yFor(p) + 3}
            textAnchor="end"
            className="stg-when-strip-ytick"
          >
            £{p}
          </text>
        ))}

        <line
          x1={PAD_L}
          x2={W - PAD_R}
          y1={H - PAD_B}
          y2={H - PAD_B}
          className="stg-when-strip-axis"
        />

        {dist.cells.map((c, i) => {
          const cx = colCenter(i)
          if (c.median == null) {
            return (
              <text
                key={`empty-${i}`}
                x={cx}
                y={H - PAD_B + 16}
                textAnchor="middle"
                className="stg-when-strip-dow empty"
              >
                {c.dow}
              </text>
            )
          }
          const offs = offsetsFor(c.prices)
          const medY = yFor(c.median)
          return (
            <g key={`col-${i}`}>
              {c.prices.map((p, j) => (
                <circle
                  key={`d-${j}`}
                  cx={cx + offs[j]}
                  cy={yFor(p)}
                  r="2.8"
                  className={`stg-when-strip-dot ${c.cheap ? 'cheap' : 'prem'}`}
                />
              ))}
              <line
                x1={cx - 20}
                x2={cx + 20}
                y1={medY}
                y2={medY}
                className="stg-when-strip-median"
              />
              <text
                x={cx}
                y={H - PAD_B + 16}
                textAnchor="middle"
                className={`stg-when-strip-dow ${c.cheap ? 'cheap' : ''}`}
              >
                {c.dow}
              </text>
              <text
                x={cx}
                y={H - PAD_B + 30}
                textAnchor="middle"
                className="stg-when-strip-med-lbl"
              >
                £{Math.round(c.median)}
              </text>
            </g>
          )
        })}
      </svg>

      <div className="stg-when-strip-legend">
        <span className="stg-when-strip-legend-item">
          <span className="stg-when-strip-swatch cheap" />
          CHEAP DAY
        </span>
        <span className="stg-when-strip-legend-item">
          <span className="stg-when-strip-swatch prem" />
          PREMIUM DAY
        </span>
        <span className="stg-when-strip-legend-item">
          <span className="stg-when-strip-swatch-bar" />
          MEDIAN
        </span>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Monthly calendars — every month touched by the 90-day window gets its
// own grid with proper Mon-first weekday columns. In-window cells are
// links to /when/:date; past cells (current month, before today) are
// dimmed; dark cells (no shows) use a diagonal hatch so they can never
// be confused with the faintest price bucket.
// ---------------------------------------------------------------------------

function MonthlyCalendars({ cal }) {
  if (!cal || cal.months.length === 0) return null
  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`NEXT ${cal.windowDays} DAYS · CALENDAR`}
        sub="Every night as a real calendar · click any day to plan"
        statLabel="DARK NIGHTS"
        stat={`${cal.darkCount} / ${cal.inWindowCount}`}
      />
      <div className="stg-when-cals">
        {cal.months.map((m) => (
          <div key={`${m.year}-${m.month}`} className="stg-when-cal">
            <div className="stg-when-cal-head">
              {m.label} <em>{m.year}</em>
            </div>
            <div className="stg-when-cal-grid">
              {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((d, i) => (
                <div key={`h-${i}`} className="stg-when-cal-h">
                  {d}
                </div>
              ))}
              {Array.from({ length: m.firstDow }).map((_, i) => (
                <div
                  key={`blank-${i}`}
                  className="stg-when-cal-cell blank"
                  aria-hidden="true"
                />
              ))}
              {m.cells.map((c) => {
                const classes = ['stg-when-cal-cell']
                if (c.isDark) classes.push('dark')
                else if (c.isPast) classes.push('past')
                else if (c.bucket != null) classes.push(`b${c.bucket}`)
                else classes.push('past')
                if (c.isToday) classes.push('today')

                if (c.inWindow && !c.isDark) {
                  return (
                    <Link
                      key={c.iso}
                      href={`/when/${c.iso}`}
                      className={classes.join(' ')}
                      aria-label={`${c.iso}: floor £${Math.round(
                        c.floor,
                      )}, ${c.showCount} ${
                        c.showCount === 1 ? 'show' : 'shows'
                      }`}
                    >
                      {c.day}
                    </Link>
                  )
                }
                const label = c.isDark
                  ? `${c.iso}: dark, no shows`
                  : `${c.iso}: not in window`
                return (
                  <div
                    key={c.iso}
                    className={classes.join(' ')}
                    aria-label={label}
                  >
                    {c.day}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="stg-when-cal-legend">
        <span className="stg-when-cal-legend-item">
          <span className="stg-when-cal-legend-sw b1" />
          CHEAPEST
        </span>
        <span className="stg-when-cal-legend-item">
          <span className="stg-when-cal-legend-sw b3" />
          MID
        </span>
        <span className="stg-when-cal-legend-item">
          <span className="stg-when-cal-legend-sw b5" />
          PRICIEST
        </span>
        <span className="stg-when-cal-legend-item">
          <span className="stg-when-cal-legend-sw dark" />
          DARK
        </span>
      </div>

      <div className="stg-when-hint">
        Tip: every cell deep-links to <code>/when/YYYY-MM-DD</code> — copy
        a URL to share a date with someone.
      </div>
    </section>
  )
}
