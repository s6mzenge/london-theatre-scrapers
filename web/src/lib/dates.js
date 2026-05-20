// Date utilities operating on ISO-format date strings ("2026-05-19") to
// avoid timezone surprises. Display uses Intl.DateTimeFormat with en-GB.
// All parsing anchors at midday local time so DST edges don't shift the
// displayed day.

function pad(n) {
  return String(n).padStart(2, '0')
}

export function todayISO() {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function parseISO(iso) {
  return new Date(`${iso}T12:00:00`)
}

export function addDaysISO(iso, n) {
  const d = parseISO(iso)
  d.setDate(d.getDate() + n)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

export function formatLongDate(iso) {
  return parseISO(iso).toLocaleDateString('en-GB', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  })
}

export function formatShortDate(iso) {
  return parseISO(iso).toLocaleDateString('en-GB', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  })
}

export function dowShort(iso) {
  return parseISO(iso)
    .toLocaleDateString('en-GB', { weekday: 'short' })
    .toUpperCase()
}

export function dayOfMonth(iso) {
  return parseISO(iso).getDate()
}

export function monthLabel(iso) {
  return parseISO(iso).toLocaleDateString('en-GB', {
    month: 'long',
    year: 'numeric',
  })
}

// Returns 0=Mon ... 6=Sun (UK calendar convention; differs from JS getDay)
export function dowMondayFirst(iso) {
  const js = parseISO(iso).getDay() // 0 = Sunday
  return (js + 6) % 7
}

// Format an ISO datetime as a compact relative time ("3h ago", "2d ago").
// Used for the "last scraped" indicator. Falls back to "—" on bad input.
export function relativeTime(isoDatetime) {
  if (!isoDatetime) return '—'
  const then = new Date(isoDatetime)
  if (Number.isNaN(then.getTime())) return '—'
  const now = Date.now()
  const diffMs = now - then.getTime()
  if (diffMs < 0) return 'just now'
  const m = Math.round(diffMs / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.round(h / 24)
  if (d < 7) return `${d}d ago`
  const w = Math.round(d / 7)
  if (w < 5) return `${w}w ago`
  return then.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}
