import { useMemo, useState, useEffect } from 'react'
import { todayISO, formatLongDate } from '../lib/dates.js'
import { effectiveCheapest } from '../lib/data.js'
import { formatPrice } from '../lib/format.js'
import { ShowLink } from '../lib/router.jsx'

// TODAY · STACKED — a time-sorted vertical feed of every performance
// happening today. Modelled on the cinemahub mobile timeline:
//
//   18:30 ─┬─ [Hamilton           Lyceum         from £24   BOOK]
//          │
//          ├─ [Six                Vaudeville     from £18   BOOK]
//          │
//   19:00 ─┴─ [Operation Mincemeat Fortune        from £15   BOOK]
//
// Spine runs continuously down the left gutter; time labels sit ON the
// line with paper-coloured padding that "cuts through" it. Sessions
// sharing a start time fold under one label. Performances whose start
// time is in the past stay visible but dim (theatre is a long game —
// you might be checking whether you missed the 14:00 by ten minutes).
//
// Why no horizontal Gantt view: the scrape doesn't carry runtime data,
// so we can't render bars of meaningful length. A pure vertical stack
// is the honest visual for what we know.

function timeStrToMinutes(t) {
  if (typeof t !== 'string') return null
  const m = /^(\d{1,2}):(\d{2})/.exec(t)
  if (!m) return null
  const h = parseInt(m[1], 10)
  const mins = parseInt(m[2], 10)
  if (!Number.isFinite(h) || !Number.isFinite(mins)) return null
  return h * 60 + mins
}

function getNowMinutes() {
  const d = new Date()
  return d.getHours() * 60 + d.getMinutes()
}

// Walk every show's performances, keep today's, attach the cheapest
// seller info, sort by start time, then collapse runs of identical
// times into "groups" so the timeline shares one label per minute.
function buildTodayFeed(data, today) {
  const sessions = []
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date !== today) continue
      if (!perf.time) continue
      const startMin = timeStrToMinutes(perf.time)
      if (startMin == null) continue
      const eff = effectiveCheapest(perf)
      // Pull the cheapest seller's book_url so the BOOK affordance
      // actually points at a real seller — falling back to null lets
      // the card render without a book button at all.
      let bookUrl = null
      if (eff && perf.sources && perf.sources[eff.seller]) {
        bookUrl = perf.sources[eff.seller].book_url || null
      }
      sessions.push({
        show: { id: show.id, title: show.title, venue: show.venue },
        time: perf.time,
        startMin,
        floor: eff ? eff.price : null,
        bookUrl,
      })
    }
  }
  sessions.sort((a, b) => {
    if (a.startMin !== b.startMin) return a.startMin - b.startMin
    // Same time → cheapest first so the user's eye starts on the
    // best deal in the cluster.
    const aPrice = a.floor != null ? a.floor : Infinity
    const bPrice = b.floor != null ? b.floor : Infinity
    if (aPrice !== bPrice) return aPrice - bPrice
    return a.show.title.localeCompare(b.show.title)
  })

  const groups = []
  for (const s of sessions) {
    const last = groups[groups.length - 1]
    if (last && last.time === s.time) {
      last.sessions.push(s)
    } else {
      groups.push({ time: s.time, startMin: s.startMin, sessions: [s] })
    }
  }
  return { groups, totalSessions: sessions.length }
}

export default function Today({ data }) {
  const today = todayISO()
  const { groups, totalSessions } = useMemo(
    () => buildTodayFeed(data, today),
    [data, today],
  )

  // Re-tick "now" once a minute so cards dim live as the day rolls
  // past start times. Cheap — one setState per minute.
  const [nowMin, setNowMin] = useState(getNowMinutes)
  useEffect(() => {
    const id = setInterval(() => setNowMin(getNowMinutes()), 60 * 1000)
    return () => clearInterval(id)
  }, [])

  // Figure out where the first not-yet-started group lives so we can
  // drop a "NOW" tick between past and future. Skipped if everything
  // is past (whole day is dim) or everything is future (no past to
  // separate from).
  const firstFutureIdx = useMemo(() => {
    for (let i = 0; i < groups.length; i++) {
      if (groups[i].startMin >= nowMin) return i
    }
    return -1
  }, [groups, nowMin])

  const upcoming = groups.filter((g) => g.startMin >= nowMin)
    .reduce((acc, g) => acc + g.sessions.length, 0)

  return (
    <div className="stg-today">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">TODAY · STACKED</div>
          <h1 className="stg-mast-h">{formatLongDate(today)}</h1>
          {totalSessions > 0 && (
            <div className="stg-show-summary">
              {totalSessions} performance{totalSessions === 1 ? '' : 's'} ·{' '}
              {upcoming} still ahead
            </div>
          )}
        </div>
      </header>

      {groups.length === 0 ? (
        <div className="stg-state">
          <div className="stg-state-eyebrow">NO PERFORMANCES</div>
          <div className="stg-state-msg">
            Nothing scheduled for today across the catalogue.
          </div>
        </div>
      ) : (
        <div className="stg-tody-feed">
          {groups.map((group, gi) => {
            const isPastGroup = group.startMin < nowMin
            const isFirstGroup = gi === 0
            const isLastGroup = gi === groups.length - 1
            const insertNowBefore =
              firstFutureIdx > 0 && gi === firstFutureIdx
            return (
              <div key={`${group.time}-${gi}`} className="stg-tody-group">
                {insertNowBefore && (
                  <NowTick nowMin={nowMin} />
                )}
                {group.sessions.map((s, si) => {
                  const isFirstCard = si === 0
                  const isLastCard = si === group.sessions.length - 1
                  const isVeryFirst = isFirstGroup && isFirstCard
                  const isVeryLast = isLastGroup && isLastCard
                  // Line geometry:
                  //  · isVeryFirst → upper half hidden (line starts at centre)
                  //  · isVeryLast  → lower half hidden (line ends at centre)
                  //  · last card in group, not last group → extend
                  //    8px below to bridge the inter-group spacer.
                  const lineTop = isVeryFirst ? '50%' : '0'
                  const lineBottom = isVeryLast
                    ? '50%'
                    : isLastCard
                      ? '-14px'
                      : '-10px'
                  return (
                    <div
                      key={`${s.show.id}-${si}-${s.time}`}
                      className={[
                        'stg-tody-row',
                        isPastGroup ? 'past' : '',
                        !isLastCard ? 'has-gap' : '',
                        isLastCard && !isLastGroup ? 'has-gap-group' : '',
                      ]
                        .filter(Boolean)
                        .join(' ')}
                    >
                      <div className="stg-tody-spine">
                        <div
                          className="stg-tody-spine-line"
                          style={{ top: lineTop, bottom: lineBottom }}
                          aria-hidden="true"
                        />
                        {isFirstCard && (
                          <div className="stg-tody-time">{group.time}</div>
                        )}
                      </div>
                      <div className="stg-tody-card">
                        <ShowLink
                          id={s.show.id}
                          className="stg-tody-card-body"
                        >
                          <div className="stg-tody-card-title">
                            {s.show.title}
                          </div>
                          <div className="stg-tody-card-meta">
                            <span className="stg-tody-card-venue">
                              {s.show.venue || 'Venue TBC'}
                            </span>
                            {s.floor != null && (
                              <span className="stg-tody-card-price">
                                from {formatPrice(s.floor)}
                              </span>
                            )}
                          </div>
                        </ShowLink>
                        {s.bookUrl && !isPastGroup && (
                          <a
                            className="stg-tody-card-book"
                            href={s.bookUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            aria-label={`Book ${s.show.title}`}
                          >
                            BOOK
                          </a>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// Thin "NOW" marker that drops between the last past group and the
// next future one. The label sits on the spine like a time stamp,
// but with a brick fill so it reads as the current cursor, not a
// scheduled time.
function NowTick({ nowMin }) {
  const hh = String(Math.floor(nowMin / 60)).padStart(2, '0')
  const mm = String(nowMin % 60).padStart(2, '0')
  return (
    <div className="stg-tody-row now">
      <div className="stg-tody-spine">
        <div
          className="stg-tody-spine-line"
          style={{ top: 0, bottom: 0 }}
          aria-hidden="true"
        />
        <div className="stg-tody-now-pill">NOW · {hh}:{mm}</div>
      </div>
      <div className="stg-tody-now-rule" aria-hidden="true" />
    </div>
  )
}
