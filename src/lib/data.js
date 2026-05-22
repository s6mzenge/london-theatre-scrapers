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
  dowMondayFirst,
  dowShort,
  formatShortDate,
  monthLabel,
  parseISO,
  todayISO,
} from './dates.js'

// unified.json is committed to public/data/ by the scrape workflow,
// so Vite copies it into dist/data/unified.json at build time and the
// site serves it from the same origin. No cross-origin fetch, no
// CORS preflight, no dependency on external caches — the data ships
// with the bundle, and a Pages redeploy after every scrape is what
// makes it fresh.
const DATA_URL = `${(import.meta.env && import.meta.env.BASE_URL) || '/'}data/unified.json.gz`

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
  const data = JSON.parse(text)
  // The unified.json that ships with the build can include shows whose
  // entire run is in the past — the scrape doesn't actively prune them,
  // and a show that closed last week is still a valid catalogue entry
  // from the scrape's point of view. For the site, though, "past shows"
  // are noise: they pad the Search list, inflate Sellers' win counts
  // with stale data, and clutter the show-detail lookup. We hide them
  // here, once, at the data-loading layer, so every downstream consumer
  // (Search, Sellers, the Cheapest Tonight/Week/Month aggregations, the
  // App-level show lookup) automatically sees only upcoming shows
  // without having to add its own filter.
  return filterToUpcomingShows(data, todayISO())
}

// A show counts as upcoming if at least one of its performances is dated
// today or later. We keep the show's past performances intact on the
// surviving entries so ShowDetail's per-show calendar can still render
// the full run with past dates greyed out (it already treats them as
// non-clickable `isPast` cells). Only when *every* performance is in the
// past do we drop the show entirely.
//
// show_count is rewritten to match the filtered length so the metadata
// the sidebar reads stays internally consistent. performance_count and
// source_summary are left alone — they describe what the scrape covered,
// which is a separate concept from what the site chooses to display.
function filterToUpcomingShows(data, today) {
  if (!data || !Array.isArray(data.shows)) return data
  const upcoming = data.shows.filter((show) =>
    (show.performances || []).some((p) => p.date >= today),
  )
  return {
    ...data,
    shows: upcoming,
    show_count: upcoming.length,
  }
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
export function validPrice(p) {
  return typeof p === 'number' && p > 0 && Number.isFinite(p)
}

// Recompute the effective cheapest price + seller for a performance,
// ignoring sources with invalid prices. Returns null when no seller has
// usable data, in which case the performance should be skipped entirely.
export function effectiveCheapest(perf) {
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
    weekend: aggregateWeekend(data, today),
    matinees: aggregateMatineesWeek(data, today),
    week: aggregateWeek(data, today, showFuturePrices),
    tiers: aggregatePriceTiers(data, today),
    spreads: aggregateSpreads(data, today),
    closingSoon: aggregateClosingSoon(data, today),
    openingSoon: aggregateOpeningSoon(data, today),
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

// =============================================================
// Weekend: the next Fri+Sat+Sun within the 7-day window.
// On a Friday morning all three nights are ahead; on a Sunday
// only Sun itself remains. We show whatever's still upcoming so
// the section silently degrades from "three cards" down to "one".
// On a Mon-Thu morning we show the coming weekend.
// =============================================================

function aggregateWeekend(data, today) {
  // JS getDay() : 0 = Sun .. 6 = Sat. We want Fri=5, Sat=6, Sun=0.
  const todayJsDow = parseISO(today).getDay()
  // Days until next Friday (0 if today is Friday).
  const daysToFri = (5 - todayJsDow + 7) % 7
  // If we're already past Friday in the current week (Sat=6 or Sun=0),
  // pick *this* weekend's remaining days rather than next Friday.
  let weekendIsos
  if (todayJsDow === 0) {
    weekendIsos = [today] // Sunday — Sat is past, Fri is past
  } else if (todayJsDow === 6) {
    weekendIsos = [today, addDaysISO(today, 1)] // Sat + Sun
  } else if (todayJsDow === 5) {
    weekendIsos = [today, addDaysISO(today, 1), addDaysISO(today, 2)]
  } else {
    const friIso = addDaysISO(today, daysToFri)
    weekendIsos = [friIso, addDaysISO(friIso, 1), addDaysISO(friIso, 2)]
  }

  const byIso = Object.fromEntries(
    weekendIsos.map((iso) => [iso, { iso, perfs: [], floor: null }]),
  )

  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (!byIso[perf.date]) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      byIso[perf.date].perfs.push({ show, perf, effPrice: eff.price })
    }
  }

  const days = weekendIsos.map((iso) => {
    const slot = byIso[iso]
    if (slot.perfs.length === 0) {
      return {
        iso,
        dow: dowShort(iso),
        dayOfMonth: dayOfMonth(iso),
        isDark: true,
        floor: null,
        topShows: [],
      }
    }
    slot.perfs.sort((a, b) => a.effPrice - b.effPrice)
    const dedupedByShow = new Map()
    for (const item of slot.perfs) {
      if (!dedupedByShow.has(item.show.id)) {
        dedupedByShow.set(item.show.id, item)
      }
    }
    const top = [...dedupedByShow.values()].slice(0, 3).map((item) => ({
      show: {
        id: item.show.id,
        title: item.show.title,
        venue: item.show.venue,
      },
      time: item.perf.time || '',
      price: item.effPrice,
    }))
    return {
      iso,
      dow: dowShort(iso),
      dayOfMonth: dayOfMonth(iso),
      isDark: false,
      floor: slot.perfs[0].effPrice,
      topShows: top,
      showCount: dedupedByShow.size,
    }
  })

  const litFloors = days.filter((d) => !d.isDark).map((d) => d.floor)
  const weekendFloor =
    litFloors.length > 0 ? Math.min(...litFloors) : null

  return { days, weekendFloor }
}

// =============================================================
// Matinées: cheapest afternoon performances over the next 7 days.
// Defined as a performance whose start time is before 17:00.
// We also compute, per show, the cheapest *evening* performance
// across the same window so the card can show "save vs evening".
// =============================================================

function aggregateMatineesWeek(data, today) {
  const windowEnd = addDaysISO(today, 7)
  const matineeRows = []
  const eveningByShow = new Map()

  for (const show of data.shows) {
    let cheapestMat = null
    for (const perf of show.performances || []) {
      if (perf.date < today || perf.date >= windowEnd) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      const hour = perf.time ? parseInt(perf.time.split(':')[0], 10) : null
      if (hour == null) continue
      if (hour < 17) {
        if (!cheapestMat || eff.price < cheapestMat.price) {
          cheapestMat = {
            show,
            perf,
            price: eff.price,
            seller: eff.seller,
          }
        }
      } else {
        const prev = eveningByShow.get(show.id)
        if (!prev || eff.price < prev) {
          eveningByShow.set(show.id, eff.price)
        }
      }
    }
    if (cheapestMat) {
      matineeRows.push(cheapestMat)
    }
  }

  matineeRows.sort((a, b) => a.price - b.price)

  // Aggregate stats — total matinée performances in the window
  // (counted across shows) and the absolute floor.
  let totalMatPerfs = 0
  let floor = null
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date < today || perf.date >= windowEnd) continue
      const hour = perf.time ? parseInt(perf.time.split(':')[0], 10) : null
      if (hour == null || hour >= 17) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      totalMatPerfs++
      if (floor == null || eff.price < floor) floor = eff.price
    }
  }

  return {
    rows: matineeRows.slice(0, 8).map((r) => {
      const evening = eveningByShow.get(r.show.id) ?? null
      const savings =
        evening != null && evening > r.price
          ? Math.round(evening - r.price)
          : null
      return {
        show: {
          id: r.show.id,
          title: r.show.title,
          venue: r.show.venue,
        },
        date: r.perf.date,
        dayLabel: formatShortDate(r.perf.date).toUpperCase(),
        time: r.perf.time || '',
        price: r.price,
        eveningPrice: evening,
        savings,
      }
    }),
    totalPerfs: totalMatPerfs,
    floor,
  }
}

// =============================================================
// Price tiers: a 4-tile budget breakdown over the next 30 days.
// For each tier we count distinct shows that have at least one
// performance with an effective floor inside that band, and pick
// one headline show (the cheapest sub-floor of each tier).
// =============================================================

const TIER_DEFS = [
  { id: 'under20', label: 'UNDER £20', min: 0, max: 20 },
  { id: '20to30', label: '£20–£30', min: 20, max: 30 },
  { id: '30to40', label: '£30–£40', min: 30, max: 40 },
  { id: '40plus', label: 'OVER £40', min: 40, max: Infinity },
]

function aggregatePriceTiers(data, today) {
  const windowEnd = addDaysISO(today, 30)
  // Per show, the cheapest effective price in the window.
  const showBest = new Map()
  for (const show of data.shows) {
    let best = Infinity
    let bestPerf = null
    for (const perf of show.performances || []) {
      if (perf.date < today || perf.date >= windowEnd) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      if (eff.price < best) {
        best = eff.price
        bestPerf = perf
      }
    }
    if (best < Infinity) {
      showBest.set(show.id, { show, price: best, perf: bestPerf })
    }
  }

  const tiers = TIER_DEFS.map((def) => {
    const inTier = []
    for (const entry of showBest.values()) {
      if (entry.price >= def.min && entry.price < def.max) {
        inTier.push(entry)
      }
    }
    inTier.sort((a, b) => a.price - b.price)
    const top = inTier[0]
    // Materialise every show in the band, sorted cheapest first — the
    // drill-down panel below the tiles renders the full list, not just
    // the headline. The headline lives on the closed tile as a teaser.
    const shows = inTier.map((e) => ({
      show: {
        id: e.show.id,
        title: e.show.title,
        venue: e.show.venue,
      },
      price: e.price,
      dayLabel: formatShortDate(e.perf.date).toUpperCase(),
    }))
    return {
      id: def.id,
      label: def.label,
      count: inTier.length,
      headline: top
        ? {
            show: {
              id: top.show.id,
              title: top.show.title,
              venue: top.show.venue,
            },
            price: top.price,
            dayLabel: formatShortDate(top.perf.date).toUpperCase(),
          }
        : null,
      shows,
    }
  })

  return { tiers, windowDays: 30 }
}

// =============================================================
// Widest spreads: performances where the gap between the cheapest
// seller and the most expensive seller is largest, *in absolute £*.
// Each row is one performance with multiple sellers in agreement
// on the date+time but different on the price. This is the
// editorial "where shopping around pays off" surface.
//
// We restrict to next 30 days so the section stays relevant, and
// dedupe by show so one show with many wide-spread performances
// only appears once (with its widest spread surfaced).
// =============================================================

function aggregateSpreads(data, today) {
  const windowEnd = addDaysISO(today, 30)
  const perShowBest = new Map()

  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date < today || perf.date >= windowEnd) continue
      if (!perf.sources) continue
      const sellers = []
      for (const [sid, info] of Object.entries(perf.sources)) {
        if (info && validPrice(info.price_from)) {
          sellers.push({ sellerId: sid, price: info.price_from })
        }
      }
      if (sellers.length < 2) continue
      sellers.sort((a, b) => a.price - b.price)
      const cheap = sellers[0]
      const dear = sellers[sellers.length - 1]
      const spread = dear.price - cheap.price
      if (spread < 5) continue // ignore noise
      const prev = perShowBest.get(show.id)
      if (!prev || spread > prev.spread) {
        perShowBest.set(show.id, {
          show,
          perf,
          spread,
          cheap,
          dear,
          sellersCount: sellers.length,
          pct: dear.price > 0 ? Math.round((spread / dear.price) * 100) : 0,
        })
      }
    }
  }

  const rows = [...perShowBest.values()]
    .sort((a, b) => b.spread - a.spread)
    .slice(0, 8)
    .map((r) => ({
      show: { id: r.show.id, title: r.show.title, venue: r.show.venue },
      dayLabel: formatShortDate(r.perf.date).toUpperCase(),
      time: r.perf.time || '',
      cheap: r.cheap,
      dear: r.dear,
      spread: r.spread,
      pct: r.pct,
      sellersCount: r.sellersCount,
    }))

  return { rows }
}

// =============================================================
// Closing soon: shows whose last future performance falls within
// `days` days from today. Sorted by remaining days ascending so
// the most urgent show is first.
// =============================================================

function aggregateClosingSoon(data, today, days = 30) {
  const horizon = addDaysISO(today, days)
  const rows = []
  for (const show of data.shows) {
    const future = (show.performances || []).filter(
      (p) => p.date >= today,
    )
    if (future.length === 0) continue
    const lastIso = future.reduce(
      (acc, p) => (p.date > acc ? p.date : acc),
      future[0].date,
    )
    if (lastIso > horizon) continue
    const remaining = future.length
    let floor = null
    for (const p of future) {
      const eff = effectiveCheapest(p)
      if (eff && (floor == null || eff.price < floor)) floor = eff.price
    }
    const daysLeft = Math.max(
      0,
      Math.round((parseISO(lastIso) - parseISO(today)) / (24 * 3600 * 1000)),
    )
    rows.push({
      show: {
        id: show.id,
        title: show.title,
        venue: show.venue,
      },
      lastIso,
      lastLabel: formatShortDate(lastIso).toUpperCase(),
      daysLeft,
      remaining,
      floor,
    })
  }
  rows.sort((a, b) => a.daysLeft - b.daysLeft)
  return { rows: rows.slice(0, 8), totalCount: rows.length, windowDays: days }
}

// =============================================================
// Opening soon: shows whose first future performance is within
// `days` days from today AND is later than today (i.e. they
// aren't already running). The first-week prices are typically
// previews — we surface that price specifically.
// =============================================================

function aggregateOpeningSoon(data, today, days = 21) {
  const horizon = addDaysISO(today, days)
  const rows = []
  for (const show of data.shows) {
    const future = (show.performances || []).filter(
      (p) => p.date >= today,
    )
    if (future.length === 0) continue
    const firstIso = future.reduce(
      (acc, p) => (p.date < acc ? p.date : acc),
      future[0].date,
    )
    // Skip shows that are already in progress (first future perf is today).
    if (firstIso <= today) continue
    if (firstIso > horizon) continue

    // Floor across first-week previews (first 7 days of the run).
    const previewEnd = addDaysISO(firstIso, 7)
    let previewFloor = null
    let runFloor = null
    for (const p of future) {
      const eff = effectiveCheapest(p)
      if (!eff) continue
      if (runFloor == null || eff.price < runFloor) runFloor = eff.price
      if (p.date < previewEnd) {
        if (previewFloor == null || eff.price < previewFloor) {
          previewFloor = eff.price
        }
      }
    }
    const daysOut = Math.max(
      0,
      Math.round((parseISO(firstIso) - parseISO(today)) / (24 * 3600 * 1000)),
    )
    rows.push({
      show: {
        id: show.id,
        title: show.title,
        venue: show.venue,
      },
      firstIso,
      firstLabel: formatShortDate(firstIso).toUpperCase(),
      daysOut,
      previewFloor,
      runFloor,
      previewSavings:
        previewFloor != null && runFloor != null && runFloor > previewFloor
          ? Math.round(runFloor - previewFloor)
          : null,
    })
  }
  rows.sort((a, b) => a.daysOut - b.daysOut)
  return { rows: rows.slice(0, 6), totalCount: rows.length, windowDays: days }
}

// =============================================================
// VENUES — show count and floor price per venue. The grouping key
// is `venue_norm` (from dedupe), which collapses "Sondheim" /
// "The Sondheim" / "Sondheim Theatre" into one row; we use the
// most-common original `venue` string as the display name.
// =============================================================

export function computeVenues(data, today) {
  const byNorm = new Map()
  for (const show of data.shows) {
    const key = show.venue_norm || show.venue || '__unknown'
    let entry = byNorm.get(key)
    if (!entry) {
      entry = {
        key,
        venueNorm: show.venue_norm || null,
        displayNames: new Map(),
        shows: [],
      }
      byNorm.set(key, entry)
    }
    if (show.venue) {
      entry.displayNames.set(
        show.venue,
        (entry.displayNames.get(show.venue) || 0) + 1,
      )
    }
    entry.shows.push(show)
  }

  const venues = []
  for (const entry of byNorm.values()) {
    // Pick the most-common original spelling as the canonical name.
    let displayName = entry.venueNorm || 'Unknown venue'
    let bestCount = -1
    for (const [name, count] of entry.displayNames) {
      if (count > bestCount) {
        bestCount = count
        displayName = name
      }
    }

    let floor = null
    let showsWithUpcoming = 0
    let perfsThisWeek = 0
    const weekEnd = addDaysISO(today, 7)

    for (const show of entry.shows) {
      let hasFuture = false
      for (const perf of show.performances || []) {
        if (perf.date < today) continue
        hasFuture = true
        const eff = effectiveCheapest(perf)
        if (eff && (floor == null || eff.price < floor)) floor = eff.price
        if (perf.date < weekEnd) perfsThisWeek++
      }
      if (hasFuture) showsWithUpcoming++
    }

    venues.push({
      slug: slugifyVenue(displayName),
      venueNorm: entry.venueNorm,
      displayName,
      showCount: entry.shows.length,
      showsWithUpcoming,
      floor,
      perfsThisWeek,
      shows: entry.shows,
    })
  }

  venues.sort((a, b) => {
    // Active venues first (any upcoming shows), then by show count desc.
    if ((b.showsWithUpcoming > 0) !== (a.showsWithUpcoming > 0)) {
      return b.showsWithUpcoming > 0 ? 1 : -1
    }
    if (b.showCount !== a.showCount) return b.showCount - a.showCount
    return a.displayName.localeCompare(b.displayName)
  })

  return venues
}

export function slugifyVenue(name) {
  return name
    .toLowerCase()
    .replace(/['']/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

// Per-venue detail: every show at this venue, with its floor and run window.
export function computeVenueDetail(venue, today) {
  const showsWithMeta = venue.shows
    .map((show) => {
      const future = (show.performances || []).filter((p) => p.date >= today)
      if (future.length === 0) {
        return {
          show,
          isPast: true,
          floor: null,
          firstIso: null,
          lastIso: null,
        }
      }
      let floor = null
      let firstIso = future[0].date
      let lastIso = future[0].date
      for (const p of future) {
        if (p.date < firstIso) firstIso = p.date
        if (p.date > lastIso) lastIso = p.date
        const eff = effectiveCheapest(p)
        if (eff && (floor == null || eff.price < floor)) floor = eff.price
      }
      return {
        show,
        isPast: false,
        floor,
        firstIso,
        lastIso,
        runLength: future.length,
      }
    })
    .filter((row) => !row.isPast)
    .sort((a, b) => {
      // Cheapest first, fall back to alphabetical
      const ap = a.floor == null ? Infinity : a.floor
      const bp = b.floor == null ? Infinity : b.floor
      if (ap !== bp) return ap - bp
      return a.show.title.localeCompare(b.show.title)
    })

  return showsWithMeta
}

// =============================================================
// WHEN — three planning surfaces.
// =============================================================

// Average floor by day-of-week over the next N days. Returns a 7-cell
// array Mon..Sun with average + sample count per cell. We use the
// median rather than the mean because the floor distribution has a
// long right tail (one premium night skews the average up).
export function computeDayOfWeekHeatmap(data, today, days = 60) {
  const buckets = [[], [], [], [], [], [], []] // 0 = Mon ... 6 = Sun
  const horizon = addDaysISO(today, days)
  const dayFloors = new Map()

  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date < today || perf.date >= horizon) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      const existing = dayFloors.get(perf.date)
      if (existing == null || eff.price < existing) {
        dayFloors.set(perf.date, eff.price)
      }
    }
  }

  for (const [iso, floor] of dayFloors) {
    const dowMonFirst = dowMondayFirst(iso)
    buckets[dowMonFirst].push(floor)
  }

  const names = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
  const cells = buckets.map((arr, i) => {
    if (arr.length === 0) {
      return { dow: names[i], median: null, count: 0 }
    }
    const sorted = [...arr].sort((a, b) => a - b)
    const median = sorted[Math.floor(sorted.length / 2)]
    return { dow: names[i], median, count: arr.length }
  })

  // Find min/max for ranking + bar widths
  const valid = cells.filter((c) => c.median != null).map((c) => c.median)
  const min = valid.length ? Math.min(...valid) : null
  const max = valid.length ? Math.max(...valid) : null
  // Cheapest and priciest weekday for the editorial line
  let cheapest = null
  let priciest = null
  for (const c of cells) {
    if (c.median == null) continue
    if (!cheapest || c.median < cheapest.median) cheapest = c
    if (!priciest || c.median > priciest.median) priciest = c
  }

  return { cells, min, max, cheapest, priciest, windowDays: days }
}

// 90-day calendar: per-day floor as a list, for a long horizontal strip.
export function computeLongCalendar(data, today, days = 90) {
  const horizon = addDaysISO(today, days)
  const dayFloors = new Map()
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date < today || perf.date >= horizon) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      const slot = dayFloors.get(perf.date) || { floor: Infinity, count: 0 }
      slot.floor = Math.min(slot.floor, eff.price)
      slot.count++
      dayFloors.set(perf.date, slot)
    }
  }
  const floors = [...dayFloors.values()]
    .map((s) => s.floor)
    .filter((p) => Number.isFinite(p))
  const percentiles = computePercentiles(floors)

  const cells = []
  for (let i = 0; i < days; i++) {
    const iso = addDaysISO(today, i)
    const slot = dayFloors.get(iso)
    const d = parseISO(iso)
    cells.push({
      iso,
      dow: dowShort(iso),
      dayOfMonth: d.getDate(),
      month: d.getMonth(),
      monthShort: d
        .toLocaleDateString('en-GB', { month: 'short' })
        .toUpperCase(),
      isToday: iso === today,
      isWeekend: d.getDay() === 0 || d.getDay() === 6,
      isFirstOfMonth: d.getDate() === 1 || i === 0,
      isDark: !slot,
      floor: slot ? slot.floor : null,
      showCount: slot ? slot.count : 0,
      bucket: slot ? priceBucket(slot.floor, percentiles) : null,
    })
  }

  return { cells, windowDays: days }
}

// Per-date drill: every show on a specific ISO date, sorted cheapest first.
export function computeForDate(data, dateIso) {
  const items = []
  for (const show of data.shows) {
    for (const perf of show.performances || []) {
      if (perf.date !== dateIso) continue
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      const max = validPrice(perf.max_price) ? perf.max_price : null
      items.push({
        show: {
          id: show.id,
          title: show.title,
          venue: show.venue,
        },
        time: perf.time || '',
        price: eff.price,
        maxPrice: max,
        cheapestSeller: eff.seller,
        sellerCount: eff.sellerCount,
      })
    }
  }
  items.sort((a, b) => {
    if (a.price !== b.price) return a.price - b.price
    return a.time.localeCompare(b.time)
  })
  return items
}

// =============================================================
// SHOWS filter chips — curated catalogue slices
// Each returns a Set of show.id values; the Search component
// filters its full list against the active set.
// =============================================================

export function computeShowFilters(data, today) {
  const closingSoonDays = 30
  const openingSoonDays = 21
  const limitedThreshold = 30 // <30 future perfs = limited engagement
  const horizonClose = addDaysISO(today, closingSoonDays)
  const horizonOpen = addDaysISO(today, openingSoonDays)

  const closingSoon = new Set()
  const openingSoon = new Set()
  const limited = new Set()
  const exclusives = new Set()
  const hiddenGems = new Set()

  for (const show of data.shows) {
    const future = (show.performances || []).filter((p) => p.date >= today)
    if (future.length === 0) continue

    let firstIso = future[0].date
    let lastIso = future[0].date
    let floor = null
    let perfsWithSellers = 0
    let perfsWithSingleSeller = 0

    for (const perf of future) {
      if (perf.date < firstIso) firstIso = perf.date
      if (perf.date > lastIso) lastIso = perf.date
      const eff = effectiveCheapest(perf)
      if (!eff) continue
      if (floor == null || eff.price < floor) floor = eff.price
      perfsWithSellers++
      if (eff.sellerCount === 1) perfsWithSingleSeller++
    }

    // Closing soon: last future perf is within window.
    if (lastIso <= horizonClose) closingSoon.add(show.id)

    // Opening soon: first future perf is in the future (not today) and within window.
    if (firstIso > today && firstIso <= horizonOpen) openingSoon.add(show.id)

    // Limited engagement: fewer than threshold remaining performances.
    if (future.length < limitedThreshold) limited.add(show.id)

    // Exclusives: every performance with priced sellers has only one seller.
    if (perfsWithSellers > 0 && perfsWithSingleSeller === perfsWithSellers) {
      exclusives.add(show.id)
    }

    // Hidden gems: cheap (floor under £25) AND low coverage (source_count <= 2).
    if (floor != null && floor <= 25 && (show.source_count || 0) <= 2) {
      hiddenGems.add(show.id)
    }
  }

  return {
    closingSoon,
    openingSoon,
    limited,
    exclusives,
    hiddenGems,
  }
}

// =============================================================
// DATA / METHODOLOGY — coverage breakdown, scrape timings, dedupe stats.
// =============================================================

export function computeMethodology(data) {
  const coverage = data.coverage_distribution || {}
  const stages = data.stages || {}
  const summary = data.source_summary || {}

  const coverageRows = Object.entries(coverage)
    .map(([k, v]) => ({ sellers: parseInt(k, 10), count: v }))
    .sort((a, b) => b.sellers - a.sellers)

  const totalShowsInCoverage = coverageRows.reduce((s, r) => s + r.count, 0)
  const wellCoveredCount = coverageRows
    .filter((r) => r.sellers >= 3)
    .reduce((s, r) => s + r.count, 0)

  const sellerRows = Object.entries(summary)
    .map(([id, info]) => ({
      sellerId: id,
      showCount: info.show_count ?? null,
      scrapedAt: info.scraped_at ?? null,
    }))
    .sort((a, b) => (b.showCount || 0) - (a.showCount || 0))

  const fuzzyMerges = Array.isArray(stages.stage_3_fuzzy_merges)
    ? stages.stage_3_fuzzy_merges
    : []
  const normClusters = stages.stage_1_2_normalization_and_registry_clusters

  // PRICE TBC = shows whose all sources had invalid prices (no valid floor).
  let priceTbcCount = 0
  for (const show of data.shows) {
    if (!validPrice(show.min_price_gbp)) priceTbcCount++
  }

  return {
    coverageRows,
    totalShowsInCoverage,
    wellCoveredCount,
    sellerRows,
    fuzzyMergesCount: fuzzyMerges.length,
    normClusters: typeof normClusters === 'number' ? normClusters : null,
    priceTbcCount,
    showCount: data.show_count ?? data.shows.length,
    performanceCount: data.performance_count ?? null,
    generatedAt: data.generated_at,
  }
}
