import { useState, useEffect, useMemo } from 'react'
import { SectionHead } from './Cheapest.jsx'
import DayDrill from './DayDrill.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

export default function CheapestWeek({ week }) {
  // Default the drill-down to today (the day in the window with isToday).
  // If today isn't in the window for some reason, fall back to the first
  // day with availability, else just the first day.
  const defaultIso = useMemo(() => {
    if (!week || !week.days.length) return null
    const today = week.days.find((d) => d.isToday)
    if (today) return today.iso
    const firstLit = week.days.find((d) => !d.isDark)
    return (firstLit || week.days[0]).iso
  }, [week])

  const [selectedIso, setSelectedIso] = useState(defaultIso)

  // Re-anchor if the data refreshes mid-session (e.g. midnight rollover
  // shifts the window forward by a day).
  useEffect(() => {
    setSelectedIso(defaultIso)
  }, [defaultIso])

  if (!week || !week.days.length) return null

  const selectedDay =
    week.days.find((d) => d.iso === selectedIso) || null

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow={`THIS WEEK · ${week.label}`}
        sub="Floor price across all London shows, by night"
        statLabel="WEEK FLOOR"
        stat={
          week.weekFloor.price != null
            ? `${formatPrice(week.weekFloor.price)} · ${week.weekFloor.dayOfWeek}`
            : '—'
        }
      />

      {/* Day-floor heatmap strip: 7 cells, one per night. Bucket is
          encoded as a brick-tinted bar at the bottom of each cell
          rather than a full cell fill — keeps the strip from
          dominating the page and lets the selected day's cream
          background stand out clearly. */}
      <div className="stg-weekstrip">
        {week.days.map((day) => {
          const isSelected = day.iso === selectedIso
          const classes = [
            'stg-weekcell',
            day.isDark ? 'dark' : `bucket-${day.bucket}`,
            day.isToday ? 'today' : '',
            isSelected ? 'sel' : '',
          ]
            .filter(Boolean)
            .join(' ')
          return (
            <button
              key={day.iso}
              type="button"
              className={classes}
              onClick={() => setSelectedIso(day.iso)}
              aria-pressed={isSelected}
              aria-label={
                day.isDark
                  ? `${day.dow} ${day.dayOfMonth}: dark, no shows`
                  : `${day.dow} ${day.dayOfMonth}: ${day.showCount} shows from ${formatPrice(day.floor)}`
              }
            >
              <div className="stg-weekcell-top">
                <div className="stg-weekcell-dow">{day.dow}</div>
                <div className="stg-weekcell-d">{day.dayOfMonth}</div>
              </div>
              <div className="stg-weekcell-bottom">
                <div className="stg-weekcell-price">
                  {day.isDark ? '—' : formatPrice(day.floor, { whole: true })}
                </div>
                <div className="stg-weekcell-lbl">
                  {day.isDark
                    ? 'DARK'
                    : `${day.showCount} SHOW${day.showCount === 1 ? '' : 'S'}`}
                </div>
              </div>
              <div className="stg-weekcell-bar" />
            </button>
          )
        })}
      </div>

      {selectedDay && <DayDrill day={selectedDay} />}

      {/* Per-show "cheapest this week" ranking — answers a different
          question than the per-day drill-down above (show-first
          ranking across the whole week vs date-first slice). */}
      <div className="stg-bestshows">
        {week.bestPerShow.slice(0, 5).map((entry, idx) => (
          <ShowLink
            key={entry.show.id}
            id={entry.show.id}
            className="stg-bestshow"
          >
            <div className="stg-bestshow-rank">
              {String(idx + 1).padStart(2, '0')}
            </div>
            <div className="stg-bestshow-body">
              <div className="stg-bestshow-title">{entry.show.title}</div>
              <div className="stg-bestshow-when">
                {entry.show.venue} · {entry.cheapestPerf.dayLabel}
                {entry.cheapestPerf.timeLabel &&
                  `, ${entry.cheapestPerf.timeLabel}`}
              </div>
            </div>
            {entry.otherNightsRange && (
              <div className="stg-bestshow-vs">
                vs other nights
                <br />
                <b>{entry.otherNightsRange}</b>
              </div>
            )}
            <div className="stg-bestshow-price">
              {formatPrice(entry.cheapestPerf.minPrice)}
            </div>
          </ShowLink>
        ))}
      </div>
    </section>
  )
}
