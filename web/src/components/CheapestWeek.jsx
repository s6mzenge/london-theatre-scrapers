import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'

export default function CheapestWeek({ week, onSelectShow }) {
  if (!week || !week.days.length) return null

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

      {/* Day-floor heatmap strip: 7 cells, one per night */}
      <div className="stg-weekstrip">
        {week.days.map((day) => (
          <div
            key={day.iso}
            className={`stg-weekcell bucket-${day.bucket} ${day.isToday ? 'today' : ''}`}
            title={
              day.isDark
                ? 'Dark — no shows'
                : `${day.showCount} shows from ${formatPrice(day.floor)}`
            }
          >
            <div className="stg-weekcell-top">
              <div className="stg-weekcell-dow">{day.dow}</div>
              <div className="stg-weekcell-d">{day.dayOfMonth}</div>
            </div>
            <div>
              <div className="stg-weekcell-price">
                {day.isDark ? '—' : formatPrice(day.floor, { whole: true })}
              </div>
              <div className="stg-weekcell-lbl">
                {day.isDark
                  ? 'DARK'
                  : `${day.showCount} SHOW${day.showCount === 1 ? '' : 'S'}`}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Per-show "cheapest this week" ranking */}
      <div className="stg-bestshows">
        {week.bestPerShow.slice(0, 5).map((entry, idx) => (
          <div
            key={entry.show.id}
            className="stg-bestshow"
            onClick={() => onSelectShow(entry.show.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ')
                onSelectShow(entry.show.id)
            }}
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
          </div>
        ))}
      </div>
    </section>
  )
}
