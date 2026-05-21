// Reads unified.json and computes the three aggregations the home view
// renders: tonight (price-sorted), this week (day-floor strip + per-show
// cheapest-night ranking), and this month (calendar heatmap + insights).
//
// All aggregations operate on UnifiedShow / UnifiedPerformance from
// dedupe.py. None of them require fields beyond Tier 1 (what's already
// in unified.json today). When Tier 2 fields land (genre, run-status,
// promotions, etc.), the components read them through `deriveGenre` /
// `deriveOnOffer` / `deriveRunStatus` helpers below — they degrade
// gracefully to undefined until plumbing arrives.

import {
  addDaysISO,
  dayOfMonth,
  dowShort,
  formatShortDate,
  monthLabel,
  parseISO,
} from './dates.js'

// unified.json is committed to public/data/ by the scrape workflow,
// so Vite copies it into dist/data/unified.json at build time and the
// site serves it from the same origin. No cross-origin fetch, no
// CORS preflight, no dependency on external caches — the data ships
// with the bundle, and a Pages redeploy after every scrape is what
// makes it fresh.
const DATA_URL = `${import.meta.env.BASE_URL || '/'}data/unified.json.gz`

export async function loadUnifiedData() {
  const r = await fetch(DATA_URL, { cache: 'default' })
  if (!r.ok) {
    throw new Error(`Failed to fetch ${DATA_URL}: HTTP ${r.status}`)
  }
  // Decompress in JS rather than relying on Content-Encoding: gzip.
  // Cloudflare Pages doesn't honor that header from _headers files for
  // static assets, so the browser gets raw gzip bytes back and chokes
  // on them as JSON. DecompressionStream is supported in all evergreen
  // browsers since 2023, so this works everywhere we care about.
  const decompressed = r.body.pipeThrough(new DecompressionStream('gzip'))
  const text = await new Response(decompressed).text()
  return JSON.parse(text)
}

// Extract the top-level scrape metadata: when was it generated, and what
// did each source contribute? Used by the sidebar's "last updated" badge
// and the Sellers tab's per-seller timestamps.
export function extractMeta(data) {
  return {
    generatedAt: data?.generated_at || null,
    showCount: data?.show_count ?? data?.shows?.length ?? 0,
    performanceCount: data?.performance_count ?? null,
    sourceSummary: data?.source_summary || {},
    coverageDistribution: data?.coverage_distribution || {},
  }
}

// =============================================================
// Per-performance helpers
// =============================================================

// Some scrapers (notably lovetheatre) emit price_from = 0.0 to mean
// "price unknown" rather than a real free seat. Dedupe propagates this
// up into perf.min_price = 0, which would otherwise dominate every
// aggregation ("cheapest tonight: £0"). We treat any non-positive
// number as missing data throughout.
function validPrice(p) {
  return typeof p === 'number' && p > 0 && Number.isFinite(p)
}

// Recompute the effective cheapest price + seller for a performance,
// ignoring sources with invalid prices. Returns null when no seller has
// usable data, in which case the performance should be skipped entirely.
function effectiveCheapest(perf) {
  if (!perf.sources) return null
  let best = null
  let sellerCount = 0
  for (const [sid, info] of Object.entries(perf.sources)) {
    if (info && validPrice(info.price_from)) {
      sellerCount++
      if (!best || info.price_from < best.price) {
        best = { seller: sid, price: info.price_from }
      }
    }
  }
  if (!best) return null
  return { ...best, sellerCount }
}

function priceRangeLabel(perf, effectiveMin) {
  if (!validPrice(perf.max_price)) return null
  if (effectiveMin == null || perf.max_price === effectiveMin) return null
  return `£${Math.round(effectiveMin)}–£${Math.round(perf.max_price)}`
}

// =============================================================
// Per-day drill-down helpers
// Used by the week strip + month calendar so each future date has a
// "top 5 cheapest shows on this day" list with a "vs other dates"
// context for each row.
// =============================================================

// Map of show.id -> [{ iso, price }, ...] for all future performances
// (today or later) with a valid effective price. Built once per
// aggregation pass and shared with both the week + month views.
function buildShowFuturePrices(data, today) {
  const map = new Map()
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date < today) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      let arr = map.get(show.id)
      if (!arr) {
        arr = []
        map.set(show.id, arr)
      }
      arr.push({ iso: perf.date, price: eff.price })
    }
  }
  return map
}

// "vs other dates" label for a show on a specific date: the price range
// across this show's other future performances. Returns "£12–£28",
// "£18" when all other dates share one price, or null when the show
// has no other future dates on file.
function otherDatesRangeLabel(showId, dateIso, showFuturePrices) {
  const all = showFuturePrices.get(showId)
  if (!all) return null
  let lo = Infinity
  let hi = -Infinity
  let count = 0
  for (const x of all) {
    if (x.iso === dateIso) continue
    count++
    if (x.price < lo) lo = x.price
    if (x.price > hi) hi = x.price
  }
  if (count === 0) return null
  return lo === hi
    ? `£${Math.round(lo)}`
    : `£${Math.round(lo)}–£${Math.round(hi)}`
}

// Top-N cheapest shows on `dateIso`. Dedups per show (so a show with
// both a matinee and an evening on the same day only appears once,
// using the cheaper performance), then returns the shape the
// DayDrill panel renders.
function cheapestShowsForDate(
  perfsOnDay,
  dateIso,
  showFuturePrices,
  limit = 5,
) {
  const byShow = new Map()
  for (const item of perfsOnDay) {
    const existing = byShow.get(item.show.id)
    if (!existing || item.effPrice < existing.effPrice) {
      byShow.set(item.show.id, item)
    }
  }
  return [...byShow.values()]
    .sort((a, b) => a.effPrice - b.effPrice)
    .slice(0, limit)
    .map((item) => ({
      show: {
        id: item.show.id,
        title: item.show.title,
        venue: item.show.venue,
        genre: deriveGenre(item.show),
      },
      perf: {
        time: item.perf.time || '',
        minPrice: item.effPrice,
      },
      otherDatesRange: otherDatesRangeLabel(
        item.show.id,
        dateIso,
        showFuturePrices,
      ),
    }))
}

// =============================================================
// Derived (Tier-2) field shims
// These are best-effort and return undefined when the underlying signal
// isn't in unified.json yet. Components hide the badge/tag accordingly.
// =============================================================

function deriveGenre(show) {
  return show.genre || show.category || undefined
}

function deriveOnOffer(show) {
  return show.on_offer || show.has_promotion || undefined
}

// =============================================================
// Top-level entry point
// =============================================================

export function computeAggregations(data, today) {
  const showFuturePrices = buildShowFuturePrices(data, today)
  return {
    tonight: aggregateTonight(data, today),
    week: aggregateWeek(data, today, showFuturePrices),
    month: aggregateMonth(data, today, showFuturePrices),
  }
}

// =============================================================
// Tonight: all performances for `today`, sorted ascending by effective
// min price. Performances with no valid seller data are dropped.
// =============================================================

function aggregateTonight(data, today) {
  const items = []
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date !== today) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      items.push({
        show: {
          id: show.id,
          title: show.title,
          venue: show.venue,
          genre: deriveGenre(show),
          onOffer: deriveOnOffer(show),
        },
        perf: {
          time: perf.time || '',
          minPrice: eff.price,
          maxPrice: validPrice(perf.max_price) ? perf.max_price : null,
          cheapestSeller: eff.seller,
          sellerCount: eff.sellerCount,
          priceRange: priceRangeLabel(perf, eff.price),
        },
      })
    }
  }
  items.sort((a, b) => a.perf.minPrice - b.perf.minPrice)

  const floor = items.length > 0 ? items[0].perf.minPrice : null
  const showIdsTonight = new Set(items.map((i) => i.show.id))
  const showsUnder35 = new Set(
    items.filter((i) => i.perf.minPrice < 35).map((i) => i.show.id),
  )

  return {
    dateLabel: formatShortDate(today),
    performances: items,
    floor,
    totalShowsCount: showIdsTonight.size,
    underThirtyFiveCount: showsUnder35.size,
  }
}

// =============================================================
// Week: 7-day window starting today.
// Computes per-day floor (cheapest price across all shows on that day)
// AND per-show cheapest performance in the window (with "vs other
// nights" range so the absolute price has context).
// =============================================================

// 5-bucket price thresholds for heatmap colouring, derived from the
// actual distribution rather than hard-coded so it adapts to whatever
// price profile we're looking at.
function computePercentiles(prices) {
  if (prices.length === 0) return null
  const sorted = [...prices].sort((a, b) => a - b)
  const pct = (p) =>
    sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))]
  return [pct(0.2), pct(0.4), pct(0.6), pct(0.8)]
}

function priceBucket(price, percentiles) {
  if (price == null || percentiles == null) return 5
  if (price <= percentiles[0]) return 1
  if (price <= percentiles[1]) return 2
  if (price <= percentiles[2]) return 3
  if (price <= percentiles[3]) return 4
  return 5
}

function aggregateWeek(data, today, showFuturePrices) {
  const days = []
  for (let i = 0; i < 7; i++) {
    const iso = addDaysISO(today, i)
    days.push({
      iso,
      dow: dowShort(iso),
      dayOfMonth: dayOfMonth(iso),
      isToday: iso === today,
      perfs: [],
      floor: null,
      showCount: 0,
      isDark: false,
      bucket: 5,
      cheapestShows: [],
    })
  }
  const isoToDay = Object.fromEntries(days.map((d) => [d.iso, d]))

  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (!isoToDay[perf.date]) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      isoToDay[perf.date].perfs.push({ show, perf, effPrice: eff.price })
    }
  }

  const dayFloors = []
  for (const d of days) {
    if (d.perfs.length === 0) {
      d.isDark = true
      continue
    }
    d.floor = Math.min(...d.perfs.map((x) => x.effPrice))
    d.showCount = new Set(d.perfs.map((x) => x.show.id)).size
    d.cheapestShows = cheapestShowsForDate(
      d.perfs,
      d.iso,
      showFuturePrices,
      5,
    )
    dayFloors.push(d.floor)
  }

  const percentiles = computePercentiles(dayFloors)
  for (const d of days) {
    if (!d.isDark) d.bucket = priceBucket(d.floor, percentiles)
  }

  // Absolute week floor
  let weekFloor = { price: null, dayOfWeek: '—' }
  for (const d of days) {
    if (d.floor != null && (weekFloor.price == null || d.floor < weekFloor.price)) {
      weekFloor = { price: d.floor, dayOfWeek: d.dow }
    }
  }

  // Per-show: which performance is cheapest in the window, and what's the
  // range of *other* prices that show has this week?
  const showsInWindow = {}
  for (const d of days) {
    for (const item of d.perfs) {
      const id = item.show.id
      if (
        !showsInWindow[id] ||
        item.effPrice < showsInWindow[id].cheapest.effPrice
      ) {
        showsInWindow[id] = {
          show: item.show,
          cheapest: item,
          allPrices: [],
        }
      }
    }
  }
  for (const d of days) {
    for (const item of d.perfs) {
      if (showsInWindow[item.show.id]) {
        showsInWindow[item.show.id].allPrices.push(item.effPrice)
      }
    }
  }

  const bestPerShow = Object.values(showsInWindow)
    .map(({ show, cheapest, allPrices }) => {
      const others = allPrices.filter((p) => p > cheapest.effPrice)
      let otherNightsRange = null
      if (others.length > 0) {
        const lo = Math.min(...others)
        const hi = Math.max(...others)
        otherNightsRange =
          lo === hi
            ? `£${Math.round(lo)}`
            : `£${Math.round(lo)}–£${Math.round(hi)}`
      }
      return {
        show: { id: show.id, title: show.title, venue: show.venue },
        cheapestPerf: {
          minPrice: cheapest.effPrice,
          dayLabel: formatShortDate(cheapest.perf.date),
          timeLabel: cheapest.perf.time,
        },
        otherNightsRange,
      }
    })
    .sort((a, b) => a.cheapestPerf.minPrice - b.cheapestPerf.minPrice)

  const startIso = days[0].iso
  const endIso = days[6].iso
  const label = `${dayOfMonth(startIso)}–${dayOfMonth(endIso)} ${parseISO(endIso)
    .toLocaleDateString('en-GB', { month: 'short' })
    .toUpperCase()}`

  return {
    days,
    weekFloor,
    bestPerShow,
    label,
  }
}

// =============================================================
// Month: calendar grid for the current month with per-day floor +
// three insight cards.
// =============================================================

function aggregateMonth(data, today, showFuturePrices) {
  const todayDate = parseISO(today)
  const year = todayDate.getFullYear()
  const month = todayDate.getMonth()

  const firstOfMonth = new Date(year, month, 1, 12)
  const lastOfMonth = new Date(year, month + 1, 0, 12)
  const firstDow = (firstOfMonth.getDay() + 6) % 7 // 0=Mon
  const totalDays = lastOfMonth.getDate()

  // Aggregate every performance in this month by date (using effective
  // cheapest price across valid sources, ignoring £0 anomalies)
  const dayFloors = {}
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      const d = parseISO(perf.date)
      if (d.getFullYear() !== year || d.getMonth() !== month) continue
      if (!dayFloors[perf.date]) {
        dayFloors[perf.date] = {
          floor: Infinity,
          showCount: new Set(),
          perfs: [],
        }
      }
      const slot = dayFloors[perf.date]
      slot.floor = Math.min(slot.floor, eff.price)
      slot.showCount.add(show.id)
      slot.perfs.push({ show, perf, effPrice: eff.price })
    }
  }

  const monthFloorPrices = Object.values(dayFloors)
    .map((s) => s.floor)
    .filter((p) => p !== Infinity)
  const percentiles = computePercentiles(monthFloorPrices)

  // Absolute month floor
  let monthFloor = { price: null, dayLabel: '—', iso: null }
  for (const [iso, slot] of Object.entries(dayFloors)) {
    if (
      slot.floor !== Infinity &&
      (monthFloor.price == null || slot.floor < monthFloor.price)
    ) {
      monthFloor = {
        price: slot.floor,
        dayLabel: formatShortDate(iso).toUpperCase(),
        iso,
      }
    }
  }

  // Build week rows
  const weeks = []
  let week = []
  for (let i = 0; i < firstDow; i++) week.push({ padding: true })
  for (let d = 1; d <= totalDays; d++) {
    const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    const slot = dayFloors[iso]
    const isPast = iso < today
    const isToday = iso === today
    const isDark = !slot || slot.floor === Infinity
    // Pre-compute the per-day drill-down list for any clickable cell
    // (anything that isn't past, padding, or dark).
    const cheapestShows =
      !isDark && !isPast
        ? cheapestShowsForDate(slot.perfs, iso, showFuturePrices, 5)
        : []
    week.push({
      iso,
      dayOfMonth: d,
      isPast,
      isToday,
      isDark,
      floor: isDark ? null : slot.floor,
      bucket: isDark ? null : priceBucket(slot.floor, percentiles) - 1,
      cheapestShows,
    })
    if (week.length === 7) {
      weeks.push(week)
      week = []
    }
  }
  while (week.length > 0 && week.length < 7) week.push({ padding: true })
  if (week.length === 7) weeks.push(week)

  // Insights
  const insights = []

  // 1) Cheapest weeknight (Mon-Thu)
  let cheapestWeeknight = null
  for (const [iso, slot] of Object.entries(dayFloors)) {
    if (iso < today) continue
    const dowJS = parseISO(iso).getDay()
    if (dowJS >= 1 && dowJS <= 4) {
      if (
        slot.floor !== Infinity &&
        (!cheapestWeeknight || slot.floor < cheapestWeeknight.price)
      ) {
        const winning = slot.perfs.find((p) => p.effPrice === slot.floor)
        cheapestWeeknight = {
          price: slot.floor,
          iso,
          show: winning?.show,
        }
      }
    }
  }
  if (cheapestWeeknight) {
    insights.push({
      label: 'CHEAPEST WEEKNIGHT',
      value: `${formatShortDate(cheapestWeeknight.iso)} · £${Math.round(cheapestWeeknight.price)}`,
      sub: cheapestWeeknight.show
        ? `${cheapestWeeknight.show.title}, ${cheapestWeeknight.show.venue}`
        : 'in this month',
      showId: cheapestWeeknight.show?.id,
    })
  }

  // 2) Biggest range — show with the widest min-max in this month
  let biggestRange = null
  for (const show of data.shows) {
    if (!validPrice(show.min_price_gbp) || !validPrice(show.max_price_gbp)) continue
    const range = show.max_price_gbp - show.min_price_gbp
    const hasInMonth = (show.performances || []).some((p) => {
      if (p.date < today) return false
      const d = parseISO(p.date)
      return d.getFullYear() === year && d.getMonth() === month
    })
    if (!hasInMonth) continue
    if (!biggestRange || range > biggestRange.range) {
      biggestRange = { range, show }
    }
  }
  if (biggestRange) {
    insights.push({
      label: 'BIGGEST RANGE',
      value: biggestRange.show.title,
      sub: `£${Math.round(biggestRange.show.min_price_gbp)}–£${Math.round(biggestRange.show.max_price_gbp)} · matinees cheapest`,
      showId: biggestRange.show.id,
    })
  }

  // 3) Activity — how many shows are running in this month
  const showsThisMonth = new Set()
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      const d = parseISO(perf.date)
      if (d.getFullYear() === year && d.getMonth() === month) {
        showsThisMonth.add(show.id)
        break
      }
    }
  }
  insights.push({
    label: 'PLAYING THIS MONTH',
    value: `${showsThisMonth.size} shows`,
    sub: `across ${Object.keys(dayFloors).length} dates with availability`,
  })

  return {
    label: monthLabel(today),
    weeks,
    monthFloor,
    insights,
  }
}
