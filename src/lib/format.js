// Display formatting for prices. We aggressively drop the ".00" since
// the site is heavily numeric and a forest of trailing zeroes reads as
// noise. The `whole` opt rounds aggressively for heatmap cells where
// space is at a premium.

export function formatPrice(value, { whole = false } = {}) {
  if (value == null || Number.isNaN(value)) return '—'
  if (whole) {
    return `£${Math.round(value)}`
  }
  if (Number.isInteger(value)) return `£${value}`
  // Strip trailing .00 (the formatter sometimes gives us 35.00)
  const fixed = value.toFixed(2)
  if (fixed.endsWith('.00')) return `£${parseInt(fixed, 10)}`
  return `£${fixed}`
}

export function formatRange(min, max) {
  if (min == null && max == null) return null
  if (min == null) return `up to ${formatPrice(max)}`
  if (max == null || min === max) return formatPrice(min)
  return `${formatPrice(min)}–${formatPrice(max)}`
}

// Best-effort prettifier for seller identifiers used in the underlying
// data. We display them in small caps so even unpolished ids look fine,
// but a few have established casings worth honouring.
const SELLER_LABELS = {
  seatplan: 'SeatPlan',
  todaytix: 'TodayTix',
  lovetheatre: 'LOVEtheatre',
  olt: 'Official London Theatre',
  ttd: 'Theatre Tickets Direct',
}

export function sellerLabel(id) {
  return SELLER_LABELS[id] || id
}
