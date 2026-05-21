import { SectionHead } from './Cheapest.jsx'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// Weekend: up to three day-cards showing the cheapest shows on Fri,
// Sat and Sun (whichever of those nights are still ahead). On a
// Tue–Thu morning we look forward to the coming weekend; on Sat we
// show Sat + Sun; on Sun we only show Sun. The aggregator handles
// the windowing — this component just renders what it's given.

export default function CheapestWeekend({ weekend }) {
  if (!weekend || !weekend.days || weekend.days.length === 0) return null

  // If every weekend day is dark (no perfs), suppress the whole section
  // — better than three empty cards.
  const anyLit = weekend.days.some((d) => !d.isDark)
  if (!anyLit) return null

  const dayCount = weekend.days.length
  const subLabel =
    dayCount === 3
      ? 'Friday · Saturday · Sunday — cheapest seat per night'
      : dayCount === 2
        ? 'Two nights left this weekend'
        : 'The last night of the weekend'

  return (
    <section className="stg-section">
      <SectionHead
        eyebrow="THIS WEEKEND"
        sub={subLabel}
        statLabel="WEEKEND FLOOR"
        stat={
          weekend.weekendFloor != null
            ? formatPrice(weekend.weekendFloor)
            : '—'
        }
      />

      <div className={`stg-weekend-grid cards-${dayCount}`}>
        {weekend.days.map((day) => (
          <WeekendDayCard key={day.iso} day={day} />
        ))}
      </div>
    </section>
  )
}

function WeekendDayCard({ day }) {
  if (day.isDark) {
    return (
      <div className="stg-weekend-card dark">
        <div className="stg-weekend-card-head">
          <div className="stg-weekend-dow">{day.dow}</div>
          <div className="stg-weekend-dom">{day.dayOfMonth}</div>
        </div>
        <div className="stg-weekend-empty">
          No shows surfaced for this night.
        </div>
      </div>
    )
  }
  const [hero, ...rest] = day.topShows

  return (
    <div className="stg-weekend-card">
      <div className="stg-weekend-card-head">
        <div className="stg-weekend-dow">{day.dow}</div>
        <div className="stg-weekend-dom">{day.dayOfMonth}</div>
        <div className="stg-weekend-floor">
          <div className="stg-weekend-floor-lbl">FROM</div>
          <div className="stg-weekend-floor-val">
            {formatPrice(day.floor)}
          </div>
        </div>
      </div>

      <ShowLink id={hero.show.id} className="stg-weekend-hero">
        <div className="stg-weekend-hero-title">{hero.show.title}</div>
        <div className="stg-weekend-hero-meta">
          {hero.show.venue}
          {hero.time && ` · ${hero.time}`}
        </div>
        <div className="stg-weekend-hero-price">
          {formatPrice(hero.price)}
        </div>
      </ShowLink>

      {rest.length > 0 && (
        <div className="stg-weekend-rest">
          {rest.map((row) => (
            <ShowLink
              key={`${row.show.id}-${row.time}`}
              id={row.show.id}
              className="stg-weekend-row"
            >
              <div className="stg-weekend-row-body">
                <div className="stg-weekend-row-title">{row.show.title}</div>
                <div className="stg-weekend-row-time">{row.time}</div>
              </div>
              <div className="stg-weekend-row-price">
                {formatPrice(row.price)}
              </div>
            </ShowLink>
          ))}
        </div>
      )}

      <div className="stg-weekend-card-foot">
        {day.showCount} {day.showCount === 1 ? 'show' : 'shows'} playing
      </div>
    </div>
  )
}
