// Reads unified.json and computes the three aggregations the home view
// renders: tonight (price-sorted), this week (day-floor strip + per-show
// cheapest-night ranking), and this month (calendar heatmap + insights).
//
// All aggregations operate on UnifiedShow / UnifiedPerformance from
// dedupe.py. None of them require fields beyond Tier 1 (what's already
// in unified.json today). When Tier 2 fields land (genre, run-status,
// promotions, etc.), the components read them through `deriveGenre` /
// `deriveOnOffer` / `deriveRunStatus` helpers below Ã¢â‚¬â€ they degrade
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
// CORS preflight, no dependency on external caches Ã¢â‚¬â€ the data ships
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
// aggregation ("cheapest tonight: Ã‚£0"). We treat any non-positive
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
  return `Ã‚£${Math.round(effectiveMin)}Ã¢â‚¬â€œÃ‚£${Math.round(perf.max_price)}`
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
  return {
    tonight: aggregateTonight(data, today),
    week: aggregateWeek(data, today),
    month: aggregateMonth(data, today),
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

function aggregateWeek(data, today) {
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
    dayFloors.push(d.floor)
  }

  const percentiles = computePercentiles(dayFloors)
  for (const d of days) {
    if (!d.isDark) d.bucket = priceBucket(d.floor, percentiles)
  }

  // Absolute week floor
  let weekFloor = { price: null, dayOfWeek: 'Ã¢â‚¬â€' }
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
            ? `Ã‚£${Math.round(lo)}`
            : `Ã‚£${Math.round(lo)}Ã¢â‚¬â€œÃ‚£${Math.round(hi)}`
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
  const label = `${dayOfMonth(startIso)}Ã¢â‚¬â€œ${dayOfMonth(endIso)} ${parseISO(endIso)
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

function aggregateMonth(data, today) {
  const todayDate = parseISO(today)
  const year = todayDate.getFullYear()
  const month = todayDate.getMonth()

  const firstOfMonth = new Date(year, month, 1, 12)
  const lastOfMonth = new Date(year, month + 1, 0, 12)
  const firstDow = (firstOfMonth.getDay() + 6) % 7 // 0=Mon
  const totalDays = lastOfMonth.getDate()

  // Aggregate every performance in this month by date (using effective
  // cheapest price across valid sources, ignoring Ã‚£0 anomalies)
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
  let monthFloor = { price: null, dayLabel: 'Ã¢â‚¬â€', iso: null }
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
    week.push({
      iso,
      dayOfMonth: d,
      isPast,
      isToday,
      isDark,
      floor: isDark ? null : slot.floor,
      bucket: isDark ? null : priceBucket(slot.floor, percentiles) - 1,
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
      value: `${formatShortDate(cheapestWeeknight.iso)} Ã‚· Ã‚£${Math.round(cheapestWeeknight.price)}`,
      sub: cheapestWeeknight.show
        ? `${cheapestWeeknight.show.title}, ${cheapestWeeknight.show.venue}`
        : 'in this month',
      showId: cheapestWeeknight.show?.id,
    })
  }

  // 2) Biggest range Ã¢â‚¬â€ show with the widest min-max in this month
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
      sub: `Ã‚£${Math.round(biggestRange.show.min_price_gbp)}Ã¢â‚¬â€œÃ‚£${Math.round(biggestRange.show.max_price_gbp)} Ã‚· matinees cheapest`,
      showId: biggestRange.show.id,
    })
  }

  // 3) Activity Ã¢â‚¬â€ how many shows are running in this month
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
