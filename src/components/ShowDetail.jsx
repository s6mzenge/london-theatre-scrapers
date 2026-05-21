import { useState, useMemo, useEffect } from 'react'
import { formatPrice, sellerLabel } from '../lib/format.js'
import {
  formatShortDate,
  parseISO,
  todayISO,
} from '../lib/dates.js'
import { validPrice } from '../lib/data.js'

// Per-show detail page. Replaces the old date×seller table with three
// editorial blocks that share the homepage's calendar visual language:
//
//   1. WHO'S CHEAPEST   — per-seller scoreboard: cheapest-frequency bars
//                         with "NEVER · BEST £X" fallback for sellers
//                         who never undercut but are still listed.
//   2. PERFORMANCES     — show-scoped month calendar(s). Each performance
//                         date shows the floor price + a compact time
//                         indicator. Same hairline grid + cream-selection
//                         + ink top-rule-for-today as the home page, but
//                         scoped to this show's run.
//   3. Drill-down panel — for the selected date, every performance
//                         renders a horizontal "price spread" strip:
//                         dots positioned by price on a show-wide
//                         £min→£max axis, cheapest dot filled brick.
//                         Solo (single-seller) performances get an
//                         italic editorial line instead.

// ---------------------------------------------------------------------------
// Description cleanup — unchanged from the old component. Some scrapers
// ship descriptions as raw markdown; we strip syntax and flow as prose.
// ---------------------------------------------------------------------------

function cleanDescription(md) {
  if (!md || typeof md !== 'string') return []
  let text = md.replace(/\r\n/g, '\n').trim()
  text = text.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
  text = text.replace(/\*\*([^*]+)\*\*/g, '$1')
  text = text.replace(/\*([^*\n]+)\*/g, '$1')
  text = text.replace(/^#{1,6}\s+/gm, '')
  text = text.replace(/\s+#{1,6}\s+/g, ' ')
  text = text.replace(/^\s*[-*]\s+/gm, '')
  return text
    .split(/\n\n+/)
    .map((p) => p.replace(/\n/g, ' ').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
}

// ---------------------------------------------------------------------------
// Aggregation. Walks the show's performances once and computes everything
// the three sections need: scoreboard rows, per-month calendar grids,
// the show-wide price axis bounds, and a default-selected date.
// ---------------------------------------------------------------------------

// All sellers' prices for a single performance, sorted ascending,
// invalid-£0 entries dropped. The drill-down strip and the scoreboard
// both read off this shape.
function effectiveSellersAsc(perf) {
  if (!perf.sources) return []
  const items = []
  for (const [sid, info] of Object.entries(perf.sources)) {
    if (info && validPrice(info.price_from)) {
      items.push({
        sellerId: sid,
        price: info.price_from,
        bookUrl: info.book_url || null,
      })
    }
  }
  items.sort((a, b) => a.price - b.price)
  return items
}

// Compact label for the calendar cell's time indicator. ":00" minutes
// drop so "13:00 + 18:30" reads as "13·18:30"; mixed minutes keep
// their suffix ("14:30·19:00"). 3+ performances on one day fall back
// to "3 PERFS" to keep the cell legible.
function formatTimesCompact(timeStrings) {
  const cleaned = timeStrings.filter((t) => t && /^\d{1,2}:\d{2}$/.test(t))
  if (cleaned.length === 0) return null
  if (cleaned.length > 2) return `${cleaned.length} PERFS`
  return cleaned
    .map((t) => {
      const [hh, mm] = t.split(':')
      return mm === '00' ? hh : `${hh}:${mm}`
    })
    .join('·')
}

function formatRunLabel(firstIso, lastIso) {
  const first = parseISO(firstIso)
  const last = parseISO(lastIso)
  const firstM = first
    .toLocaleDateString('en-GB', { month: 'short' })
    .toUpperCase()
  const lastM = last
    .toLocaleDateString('en-GB', { month: 'short' })
    .toUpperCase()
  if (
    first.getFullYear() === last.getFullYear() &&
    first.getMonth() === last.getMonth()
  ) {
    return `${first.getDate()}–${last.getDate()} ${lastM}`
  }
  return `${first.getDate()} ${firstM} – ${last.getDate()} ${lastM}`
}

// Build one month-grid (DOW-headers + 4–6 weeks of 7 cells). Pads
// leading/trailing cells. Each cell carries the per-date aggregate
// the calendar component needs (floor + times) when it's a perf day.
function buildMonth(year, month, byDate, today) {
  const firstOfMonth = new Date(year, month, 1, 12)
  const lastOfMonth = new Date(year, month + 1, 0, 12)
  const firstDow = (firstOfMonth.getDay() + 6) % 7 // 0 = Mon
  const totalDays = lastOfMonth.getDate()
  const label = firstOfMonth.toLocaleDateString('en-GB', {
    month: 'long',
    year: 'numeric',
  })

  const weeks = []
  let week = []
  for (let i = 0; i < firstDow; i++) week.push({ padding: true })

  for (let d = 1; d <= totalDays; d++) {
    const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    const dateInfo = byDate[iso]
    const isPerf = !!dateInfo
    let floor = null
    let times = null
    if (isPerf) {
      const validFloors = dateInfo.performances
        .map((p) => (p.sellers.length > 0 ? p.sellers[0].price : Infinity))
        .filter(Number.isFinite)
      floor = validFloors.length > 0 ? Math.min(...validFloors) : null
      times = formatTimesCompact(
        dateInfo.performances.map((p) => p.perf.time),
      )
    }
    week.push({
      iso,
      dayOfMonth: d,
      isPast: iso < today,
      isToday: iso === today,
      isPerf,
      floor,
      times,
    })
    if (week.length === 7) {
      weeks.push(week)
      week = []
    }
  }
  while (week.length > 0 && week.length < 7) week.push({ padding: true })
  if (week.length === 7) weeks.push(week)
  return { year, month, label, weeks }
}

function computeAnalysis(performances, today) {
  const sorted = [...performances].sort((a, b) => {
    if (a.date !== b.date) return a.date.localeCompare(b.date)
    return (a.time || '').localeCompare(b.time || '')
  })

  // One pass: pre-compute every performance's ascending seller list.
  // The rest of the analysis is just bookkeeping over this.
  const enriched = sorted.map((perf) => ({
    perf,
    sellers: effectiveSellersAsc(perf),
  }))

  // Axis bounds — the show-wide min and max effective price. Every
  // spread strip is plotted against this same axis so spreads are
  // comparable across performances.
  let axisMin = Infinity
  let axisMax = -Infinity
  for (const { sellers } of enriched) {
    for (const s of sellers) {
      if (s.price < axisMin) axisMin = s.price
      if (s.price > axisMax) axisMax = s.price
    }
  }
  if (!Number.isFinite(axisMin)) axisMin = 0
  if (!Number.isFinite(axisMax)) axisMax = 0

  // Scoreboard — for each seller, count performances where they're
  // tied-cheapest, and remember their best-ever price. The "tied"
  // case matters: if SeatPlan and TTD both list a show at £6, both
  // get credit. Sellers who never reach the floor still appear, with
  // their lowest historical price shown as context.
  const sellerStats = {}
  let countedPerfs = 0
  for (const { sellers } of enriched) {
    if (sellers.length === 0) continue
    countedPerfs++
    const cheapestPrice = sellers[0].price
    for (const s of sellers) {
      if (!sellerStats[s.sellerId]) {
        sellerStats[s.sellerId] = { cheapestCount: 0, bestPrice: Infinity }
      }
      if (s.price === cheapestPrice) sellerStats[s.sellerId].cheapestCount++
      if (s.price < sellerStats[s.sellerId].bestPrice) {
        sellerStats[s.sellerId].bestPrice = s.price
      }
    }
  }
  const scoreboard = Object.entries(sellerStats)
    .map(([sellerId, stats]) => ({
      sellerId,
      cheapestCount: stats.cheapestCount,
      bestPrice: stats.bestPrice,
      totalCount: countedPerfs,
    }))
    // Winners first (sorted by frequency, then by best price as
    // tiebreaker); never-cheapest sellers after, sorted by best price.
    .sort((a, b) => {
      if (a.cheapestCount !== b.cheapestCount) {
        return b.cheapestCount - a.cheapestCount
      }
      return a.bestPrice - b.bestPrice
    })

  // Group performances by date — the drill-down + calendar both need
  // this lookup.
  const byDate = {}
  for (const item of enriched) {
    if (item.sellers.length === 0) continue
    if (!byDate[item.perf.date]) {
      byDate[item.perf.date] = { iso: item.perf.date, performances: [] }
    }
    byDate[item.perf.date].performances.push(item)
  }

  // Empty case — no scrape data we can render. Bail with a flag.
  if (countedPerfs === 0) {
    return {
      isEmpty: true,
      scoreboard,
      months: [],
      byDate,
      axisMin,
      axisMax,
      defaultDateIso: null,
      runLabel: '—',
      totalPerformances: 0,
    }
  }

  // Build one calendar per month spanned by the run (inclusive on
  // both ends). Most West End shows fit in 1–3 months so this is
  // cheap; for longer runs the calendars stack vertically.
  const firstIso = enriched[0].perf.date
  const lastIso = enriched[enriched.length - 1].perf.date
  const firstDate = parseISO(firstIso)
  const lastDate = parseISO(lastIso)
  const months = []
  let cy = firstDate.getFullYear()
  let cm = firstDate.getMonth()
  while (
    cy < lastDate.getFullYear() ||
    (cy === lastDate.getFullYear() && cm <= lastDate.getMonth())
  ) {
    months.push(buildMonth(cy, cm, byDate, today))
    cm++
    if (cm > 11) {
      cm = 0
      cy++
    }
  }

  // Default selection: today if it has a perf, otherwise the next
  // upcoming perf, otherwise (entire run in the past) the last perf.
  let defaultDateIso = null
  if (byDate[today]) {
    defaultDateIso = today
  } else {
    for (const item of enriched) {
      if (item.perf.date >= today) {
        defaultDateIso = item.perf.date
        break
      }
    }
    if (!defaultDateIso) {
      defaultDateIso = enriched[enriched.length - 1].perf.date
    }
  }

  // Default visible month: whichever calendar contains the default-
  // selected date. So the page opens with the selected date already
  // on screen rather than the user having to page forward to find it.
  const defaultDate = parseISO(defaultDateIso)
  let defaultMonthIdx = months.findIndex(
    (m) => m.year === defaultDate.getFullYear() && m.month === defaultDate.getMonth(),
  )
  if (defaultMonthIdx < 0) defaultMonthIdx = 0

  return {
    isEmpty: false,
    scoreboard,
    months,
    byDate,
    axisMin,
    axisMax,
    defaultDateIso,
    defaultMonthIdx,
    runLabel: formatRunLabel(firstIso, lastIso),
    totalPerformances: countedPerfs,
  }
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function ShowDetail({ show, onBack }) {
  const today = todayISO()
  const analysis = useMemo(
    () => computeAnalysis(show.performances || [], today),
    [show, today],
  )
  const [selectedDateIso, setSelectedDateIso] = useState(
    analysis.defaultDateIso,
  )
  const [currentMonthIdx, setCurrentMonthIdx] = useState(
    analysis.defaultMonthIdx ?? 0,
  )

  // Re-anchor when the user navigates between shows — the default for
  // the new show might be entirely different, and the old iso may not
  // exist on this show at all.
  useEffect(() => {
    setSelectedDateIso(analysis.defaultDateIso)
    setCurrentMonthIdx(analysis.defaultMonthIdx ?? 0)
  }, [analysis.defaultDateIso, analysis.defaultMonthIdx])

  const descParagraphs = useMemo(
    () => cleanDescription(show.description),
    [show.description],
  )
  const selectedDate =
    selectedDateIso && analysis.byDate[selectedDateIso]
      ? analysis.byDate[selectedDateIso]
      : null

  return (
    <div className="stg-show">
      <button type="button" className="stg-back" onClick={onBack}>
        ← BACK
      </button>

      <header className="stg-mast stg-mast-show">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">{show.venue || 'Venue TBC'}</div>
          <h1 className="stg-mast-h">{show.title}</h1>
          <div className="stg-show-summary">
            {show.performance_count} performances · {show.source_count} sellers
            {typeof show.min_price_gbp === 'number' &&
              show.min_price_gbp > 0 && (
                <>
                  {' '}· from <b>{formatPrice(show.min_price_gbp)}</b>
                </>
              )}
            {typeof show.max_price_gbp === 'number' &&
              show.max_price_gbp > 0 && (
                <> up to {formatPrice(show.max_price_gbp)}</>
              )}
          </div>
        </div>
      </header>

      {descParagraphs.length > 0 && (
        <div className="stg-show-desc">
          {descParagraphs.map((p, i) => (
            <p key={i}>{p}</p>
          ))}
        </div>
      )}

      {analysis.isEmpty ? (
        <div className="stg-empty">No seller data for this show yet.</div>
      ) : (
        <>
          <Scoreboard analysis={analysis} />
          <PerformancesSection
            analysis={analysis}
            selectedDateIso={selectedDateIso}
            onSelectDate={setSelectedDateIso}
            currentMonthIdx={currentMonthIdx}
            onChangeMonth={setCurrentMonthIdx}
          />
          {selectedDate && (
            <DrillDown
              date={selectedDate}
              axisMin={analysis.axisMin}
              axisMax={analysis.axisMax}
            />
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Scoreboard section
// ---------------------------------------------------------------------------

function Scoreboard({ analysis }) {
  const { scoreboard, totalPerformances, axisMin } = analysis
  if (scoreboard.length === 0) return null

  const winners = scoreboard.filter((r) => r.cheapestCount > 0)
  const maxCount = winners.length > 0 ? winners[0].cheapestCount : 1
  const topSeller = scoreboard[0]

  return (
    <section className="stg-section stg-show-section">
      <div className="stg-section-head">
        <div className="stg-section-left">
          <div className="stg-section-eyebrow">WHO&rsquo;S CHEAPEST</div>
          <div className="stg-section-sub">
            Across this show&rsquo;s {totalPerformances} tracked performance
            {totalPerformances === 1 ? '' : 's'}
          </div>
        </div>
        <div className="stg-section-stat-wrap">
          <div className="stg-section-stat-lbl">SHOW FLOOR</div>
          <div className="stg-section-stat">
            {formatPrice(axisMin)} ·{' '}
            {sellerLabel(topSeller.sellerId).toUpperCase()}
          </div>
        </div>
      </div>

      <div className="stg-show-scoreboard">
        {scoreboard.map((row) => {
          const isWinner = row.cheapestCount > 0
          const barPct = isWinner ? (row.cheapestCount / maxCount) * 100 : 0
          return (
            <div
              key={row.sellerId}
              className={`stg-sb-row ${isWinner ? '' : 'fade'}`}
            >
              <div className="stg-sb-seller">
                {sellerLabel(row.sellerId).toUpperCase()}
              </div>
              <div className="stg-sb-bar-wrap">
                <div
                  className={`stg-sb-bar ${isWinner ? '' : 'zero'}`}
                  style={isWinner ? { width: `${barPct}%` } : undefined}
                />
              </div>
              <div className={`stg-sb-meta ${isWinner ? '' : 'dim'}`}>
                {isWinner ? (
                  <>
                    CHEAPEST ON{' '}
                    <b>
                      {row.cheapestCount} / {row.totalCount}
                    </b>
                  </>
                ) : (
                  <>
                    NEVER · BEST <b>{formatPrice(row.bestPrice)}</b>
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Performances section: 1+ month calendar(s) stacked vertically
// ---------------------------------------------------------------------------

function PerformancesSection({
  analysis,
  selectedDateIso,
  onSelectDate,
  currentMonthIdx,
  onChangeMonth,
}) {
  const months = analysis.months
  // Clamp in case state ever gets ahead of the data (e.g. analysis
  // recomputed with fewer months while a higher index was stored).
  const idx = Math.max(0, Math.min(currentMonthIdx, months.length - 1))
  const current = months[idx]
  const prev = idx > 0 ? months[idx - 1] : null
  const next = idx < months.length - 1 ? months[idx + 1] : null

  return (
    <section className="stg-section stg-show-section">
      <div className="stg-section-head">
        <div className="stg-section-left">
          <div className="stg-section-eyebrow">PERFORMANCES</div>
          <div className="stg-section-sub">
            Click a date for the per-seller breakdown
          </div>
        </div>
        <div className="stg-section-stat-wrap">
          <div className="stg-section-stat-lbl">RUN</div>
          <div className="stg-section-stat stg-show-runlabel">
            {analysis.runLabel}
          </div>
        </div>
      </div>

      {/* Month navigator. Only renders the controls when there's more
          than one month in the run — single-month runs just show the
          month label centred without dead arrows flanking it. */}
      {months.length > 1 ? (
        <div className="stg-show-monthnav">
          <button
            type="button"
            className="stg-show-monthnav-arrow"
            onClick={() => onChangeMonth(idx - 1)}
            disabled={!prev}
            aria-label={prev ? `Previous month, ${prev.label}` : 'No previous month'}
          >
            ←
          </button>
          <div className="stg-show-monthnav-label">
            {current.label.toUpperCase()}
          </div>
          <button
            type="button"
            className="stg-show-monthnav-arrow"
            onClick={() => onChangeMonth(idx + 1)}
            disabled={!next}
            aria-label={next ? `Next month, ${next.label}` : 'No next month'}
          >
            →
          </button>
        </div>
      ) : (
        <div className="stg-show-monthnav single">
          <div className="stg-show-monthnav-label">
            {current.label.toUpperCase()}
          </div>
        </div>
      )}

      <div className="stg-show-cal-block">
        <ShowCalendar
          weeks={current.weeks}
          selectedDateIso={selectedDateIso}
          onSelectDate={onSelectDate}
        />
      </div>
    </section>
  )
}

function ShowCalendar({ weeks, selectedDateIso, onSelectDate }) {
  return (
    <div className="stg-show-cal">
      {['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'].map((d) => (
        <div key={d} className="stg-show-cal-dow">
          {d}
        </div>
      ))}
      {weeks.flat().map((cell, idx) => {
        if (cell.padding) {
          return (
            <div key={`pad-${idx}`} className="stg-show-cal-c padding" />
          )
        }
        const isSelected = cell.iso === selectedDateIso
        const clickable = cell.isPerf && !cell.isPast
        const classes = [
          'stg-show-cal-c',
          cell.isPerf ? 'perf' : 'dark',
          cell.isPast ? 'past' : '',
          cell.isToday ? 'today' : '',
          isSelected ? 'sel' : '',
          clickable ? '' : 'nonclick',
        ]
          .filter(Boolean)
          .join(' ')

        const body = (
          <>
            <div className="stg-show-cal-d">{cell.dayOfMonth}</div>
            {cell.isPerf && (
              <div className="stg-show-cal-foot">
                <div className="stg-show-cal-p">
                  {formatPrice(cell.floor, { whole: true })}
                </div>
                {cell.times && (
                  <div className="stg-show-cal-times">{cell.times}</div>
                )}
              </div>
            )}
          </>
        )

        if (clickable) {
          return (
            <button
              key={cell.iso}
              type="button"
              className={classes}
              onClick={() => onSelectDate(cell.iso)}
              aria-pressed={isSelected}
              aria-label={`${cell.iso}: from £${Math.round(cell.floor)}${
                cell.times ? `, ${cell.times}` : ''
              }`}
            >
              {body}
            </button>
          )
        }
        return (
          <div key={cell.iso} className={classes}>
            {body}
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Drill-down: per-performance spread strips for the selected date
// ---------------------------------------------------------------------------

function DrillDown({ date, axisMin, axisMax }) {
  const dateLabel = formatShortDate(date.iso).toUpperCase()
  const count = date.performances.length

  return (
    <div className="stg-show-drill">
      <div className="stg-show-drill-head">
        <div className="stg-show-drill-eye">
          <b>{dateLabel}</b> · {count} PERFORMANCE
          {count === 1 ? '' : 'S'}
        </div>
        <div className="stg-show-drill-r-lbl">PRICE SPREAD</div>
      </div>

      {date.performances.map((p, i) => (
        <PerformanceBlock
          key={`${p.perf.date}-${p.perf.time}-${i}`}
          performance={p}
          axisMin={axisMin}
          axisMax={axisMax}
        />
      ))}
    </div>
  )
}

// Sellers tied at the exact same price collapse to one tick; close-but-
// not-tied prices get separate ticks (e.g. £12 LOVE and £13 TTD both
// render). Labels may visually overlap on very tight spreads — if that
// becomes a real problem in production we can move to a collision-
// merging pass here.
function groupByExactPrice(sellers) {
  const groups = []
  for (const s of sellers) {
    const last = groups[groups.length - 1]
    if (last && s.price === last.price) {
      last.sellers.push(s)
    } else {
      groups.push({ price: s.price, sellers: [s] })
    }
  }
  return groups
}

function PerformanceBlock({ performance, axisMin, axisMax }) {
  const { perf, sellers } = performance
  if (sellers.length === 0) return null
  const cheapest = sellers[0]
  const axisRange = axisMax - axisMin
  const groups = groupByExactPrice(sellers)
  const isSolo = sellers.length === 1

  return (
    <div className="stg-show-perf">
      <div className="stg-show-perf-top">
        <div className="stg-show-perf-time">{perf.time || '—'}</div>
        <div className="stg-show-perf-floor">
          FROM
          <b>{formatPrice(cheapest.price)}</b>
          <span className="stg-show-perf-via">
            {sellerLabel(cheapest.sellerId).toUpperCase()}
          </span>
        </div>
        {cheapest.bookUrl && (
          <a
            className="stg-show-perf-book"
            href={cheapest.bookUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            BOOK →
          </a>
        )}
      </div>

      {isSolo ? (
        <div className="stg-show-solo">
          <span className="stg-show-solo-dot" />
          Only {sellerLabel(cheapest.sellerId)} listing this performance ·{' '}
          <span className="stg-show-solo-price">
            {formatPrice(cheapest.price)}
          </span>
        </div>
      ) : (
        <>
          {/* Axis with dots only — no per-dot labels. Trying to label
              each dot caused collisions when prices were close (£19.25,
              £19.50, £20 on a £15–£48 axis bunch into ~10% of the
              strip width). The labels now live in the seller list below,
              which flex-wraps cleanly. The dots' job is purely spatial:
              show where each seller sits on the show-wide axis. */}
          <div className="stg-show-spread">
            <div className="stg-show-spread-axis" />
            {groups.map((group, gi) => {
              const pct =
                axisRange > 0
                  ? ((group.price - axisMin) / axisRange) * 100
                  : 50
              const isCheap = group.price === cheapest.price
              const label = group.sellers
                .map((s) => sellerLabel(s.sellerId))
                .join(' · ')
              const linkSeller = group.sellers[0]
              return (
                <a
                  key={gi}
                  className={`stg-show-spread-tick ${isCheap ? 'cheap' : ''}`}
                  style={{ left: `${pct}%` }}
                  href={linkSeller.bookUrl || '#'}
                  target={linkSeller.bookUrl ? '_blank' : undefined}
                  rel={linkSeller.bookUrl ? 'noopener noreferrer' : undefined}
                  aria-label={`${label} at £${Math.round(group.price)}`}
                >
                  <span className="stg-show-spread-dot" />
                </a>
              )
            })}
          </div>

          {/* Axis end-markers — tiny captions anchoring "what 0% and
              100% mean". Without these the bare axis line is opaque;
              with them the user reads "ah, the cheap end is £15, the
              expensive end is £48, this performance bunches near the
              cheap end." */}
          <div className="stg-show-spread-ends">
            <span>{formatPrice(axisMin)} SHOW FLOOR</span>
            <span>{formatPrice(axisMax)} CEILING</span>
          </div>

          {/* Sorted seller list — every seller as a clickable entry,
              cheapest first. flex-wrap means narrow viewports just
              break to a second line instead of overlapping. Each
              entry deep-links to that seller's booking page. */}
          <div className="stg-show-spread-list">
            {groups.map((group, gi) => {
              const isCheap = group.price === cheapest.price
              const linkSeller = group.sellers[0]
              const sellerNames = group.sellers
                .map((s) => sellerLabel(s.sellerId).toUpperCase())
                .join(' · ')
              return (
                <a
                  key={gi}
                  className={`stg-show-spread-item ${isCheap ? 'cheap' : ''}`}
                  href={linkSeller.bookUrl || '#'}
                  target={linkSeller.bookUrl ? '_blank' : undefined}
                  rel={linkSeller.bookUrl ? 'noopener noreferrer' : undefined}
                  aria-label={`Book ${sellerNames} at £${Math.round(group.price)}`}
                >
                  <span className="stg-show-spread-item-price">
                    {formatPrice(group.price)}
                  </span>
                  <span className="stg-show-spread-item-seller">
                    {sellerNames}
                  </span>
                </a>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
