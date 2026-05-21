// Exercise the new aggregations against real data
import { computeAggregations, computeVenues, computeShowFilters,
         computeDayOfWeekHeatmap, computeLongCalendar, computeForDate,
         computeMethodology, computeVenueDetail } from './src/lib/data.js'
import fs from 'fs'

const data = JSON.parse(fs.readFileSync('public/data/unified.json'))
// Filter to upcoming (same as loadUnifiedData does)
const today = '2026-05-22'
const upcoming = data.shows.filter(s => (s.performances || []).some(p => p.date >= today))
const filtered = { ...data, shows: upcoming, show_count: upcoming.length }

console.log('=== Cheapest aggregations ===')
const agg = computeAggregations(filtered, today)
console.log('Tonight perfs:', agg.tonight.performances.length, 'floor:', agg.tonight.floor)
console.log('Weekend days:', agg.weekend.days.length, 'floor:', agg.weekend.weekendFloor)
console.log('  Weekend days dows:', agg.weekend.days.map(d => `${d.dow} ${d.dayOfMonth} (dark=${d.isDark})`).join(', '))
console.log('Matinées rows:', agg.matinees.rows.length, 'totalPerfs:', agg.matinees.totalPerfs, 'floor:', agg.matinees.floor)
console.log('  Top matinée:', agg.matinees.rows[0]?.show?.title, '@', agg.matinees.rows[0]?.price, '(savings:', agg.matinees.rows[0]?.savings, ')')
console.log('Tiers:', agg.tiers.tiers.map(t => `${t.label}=${t.count}`).join(', '))
console.log('Spreads rows:', agg.spreads.rows.length)
console.log('  Top spread:', agg.spreads.rows[0]?.show?.title, '£' + agg.spreads.rows[0]?.spread, '(' + agg.spreads.rows[0]?.pct + '%)')
console.log('Closing soon rows:', agg.closingSoon.rows.length, 'total:', agg.closingSoon.totalCount)
console.log('  Most urgent:', agg.closingSoon.rows[0]?.show?.title, agg.closingSoon.rows[0]?.daysLeft, 'days left')
console.log('Opening soon rows:', agg.openingSoon.rows.length, 'total:', agg.openingSoon.totalCount)
console.log('  Next to open:', agg.openingSoon.rows[0]?.show?.title, 'in', agg.openingSoon.rows[0]?.daysOut, 'days')
console.log()

console.log('=== Venues ===')
const venues = computeVenues(filtered, today)
console.log('Total venues:', venues.length)
console.log('Active venues:', venues.filter(v => v.showsWithUpcoming > 0).length)
console.log('Top 3 by show count:', venues.slice(0,3).map(v => `${v.displayName} (${v.showCount} shows, from £${v.floor})`).join('\n  '))
console.log('Sample slug:', venues[0]?.slug)
const detail = computeVenueDetail(venues[0], today)
console.log('First venue\'s detail rows:', detail.length, 'cheapest first:', detail[0]?.show?.title, 'from £' + detail[0]?.floor)
console.log()

console.log('=== Show filters (chips) ===')
const sf = computeShowFilters(filtered, today)
console.log('  closing-soon:', sf.closingSoon.size)
console.log('  opening-soon:', sf.openingSoon.size)
console.log('  limited:', sf.limited.size)
console.log('  hidden gems:', sf.hiddenGems.size)
console.log('  exclusives:', sf.exclusives.size)
console.log()

console.log('=== When ===')
const dow = computeDayOfWeekHeatmap(filtered, today, 60)
console.log('  DOW cells:', dow.cells.map(c => `${c.dow}=£${Math.round(c.median ?? 0)} (n=${c.count})`).join(', '))
console.log('  Cheapest weekday:', dow.cheapest?.dow, '£' + dow.cheapest?.median)
console.log('  Priciest weekday:', dow.priciest?.dow, '£' + dow.priciest?.median)
const longCal = computeLongCalendar(filtered, today, 90)
console.log('  Long calendar cells:', longCal.cells.length)
console.log('  Dark days:', longCal.cells.filter(c => c.isDark).length)
console.log('  Today cell:', longCal.cells[0]?.iso, 'isToday=' + longCal.cells[0]?.isToday)
const forDate = computeForDate(filtered, '2026-06-14')
console.log('  June 14 perfs:', forDate.length, 'cheapest:', forDate[0]?.show?.title, '@£' + forDate[0]?.price)
console.log()

console.log('=== Methodology ===')
const m = computeMethodology(filtered)
console.log('  Coverage rows:', m.coverageRows.map(r => `${r.sellers}=${r.count}`).join(', '))
console.log('  Well-covered:', m.wellCoveredCount, '/', m.totalShowsInCoverage)
console.log('  Sellers:', m.sellerRows.map(s => `${s.sellerId}=${s.showCount}`).join(', '))
console.log('  Fuzzy merges:', m.fuzzyMergesCount, 'norm clusters:', m.normClusters)
console.log('  Price TBC shows:', m.priceTbcCount)
