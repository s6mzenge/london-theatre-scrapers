import { useMemo } from 'react'
import CheapestTonight from './CheapestTonight.jsx'
import CheapestWeekend from './CheapestWeekend.jsx'
import CheapestMatinees from './CheapestMatinees.jsx'
import CheapestWeek from './CheapestWeek.jsx'
import CheapestTiers from './CheapestTiers.jsx'
import CheapestSpreads from './CheapestSpreads.jsx'
import CheapestClosingSoon from './CheapestClosingSoon.jsx'
import CheapestOpeningSoon from './CheapestOpeningSoon.jsx'
import CheapestMonth from './CheapestMonth.jsx'
import { todayISO, formatLongDate } from '../lib/dates.js'
import { computeAggregations } from '../lib/data.js'

export default function Cheapest({ data }) {
  const today = todayISO()
  const agg = useMemo(() => computeAggregations(data, today), [data, today])

  return (
    <div className="stg-cheapest">
      <header className="stg-mast">
        <div className="stg-mast-left">
          <div className="stg-mast-eyebrow">CHEAPEST · LONDON</div>
          <h1 className="stg-mast-h">What&rsquo;s on, for less.</h1>
        </div>
        <div className="stg-mast-date">{formatLongDate(today)}</div>
      </header>

      <CheapestTonight tonight={agg.tonight} />
      <CheapestWeekend weekend={agg.weekend} />
      <CheapestMatinees matinees={agg.matinees} />
      <CheapestWeek week={agg.week} />
      <CheapestTiers tiers={agg.tiers} />
      <CheapestSpreads spreads={agg.spreads} />
      <CheapestClosingSoon closingSoon={agg.closingSoon} />
      <CheapestOpeningSoon openingSoon={agg.openingSoon} />
      <CheapestMonth month={agg.month} />
    </div>
  )
}

// Section header used by all sub-views. Eyebrow + sub-line on the
// left; small stat label + figure on the right.
export function SectionHead({ eyebrow, sub, statLabel, stat, action }) {
  return (
    <div className="stg-section-head">
      <div className="stg-section-left">
        <div className="stg-section-eyebrow">{eyebrow}</div>
        <div className="stg-section-sub">{sub}</div>
      </div>
      {(statLabel || stat || action) && (
        <div className="stg-section-stat-wrap">
          {statLabel && <div className="stg-section-stat-lbl">{statLabel}</div>}
          {stat && <div className="stg-section-stat">{stat}</div>}
          {action && <div className="stg-section-action">{action}</div>}
        </div>
      )}
    </div>
  )
}
