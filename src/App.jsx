import { useState, useEffect, useCallback } from 'react'
import Curtain from './components/Curtain.jsx'
import Sidebar from './components/Sidebar.jsx'
import Cheapest from './components/Cheapest.jsx'
import When from './components/When.jsx'
import WhenDate from './components/WhenDate.jsx'
import Search from './components/Search.jsx'
import Venues, { VenueDetail } from './components/Venues.jsx'
import Sellers from './components/Sellers.jsx'
import Data from './components/Data.jsx'
import ShowDetail from './components/ShowDetail.jsx'
import { loadUnifiedData } from './lib/data.js'
import { useRoute } from './lib/router.jsx'

const CURTAIN_KEY = 'stg-curtain-seen'

export default function App() {
  // The URL is now the source of truth for "what page am I on". Tab
  // switches and show-detail entry/exit all go through navigate(),
  // which updates the URL and re-renders the tree below. Previously
  // this was a useState pair (`view` + `selectedShowId`); the route
  // object replaces both.
  const route = useRoute()
  const [data, setData] = useState(null)
  const [loadError, setLoadError] = useState(null)

  // Curtain is one-per-session. We honour prefers-reduced-motion by
  // skipping the animation entirely for users who've asked the OS for
  // less motion.
  const [curtainVisible, setCurtainVisible] = useState(() => {
    try {
      if (typeof window === 'undefined') return false
      const seen = sessionStorage.getItem(CURTAIN_KEY) === 'yes'
      if (seen) return false
      const reducedMotion = window.matchMedia(
        '(prefers-reduced-motion: reduce)',
      ).matches
      return !reducedMotion
    } catch {
      return true
    }
  })

  // Load unified.json once on mount
  useEffect(() => {
    loadUnifiedData()
      .then(setData)
      .catch((err) => {
        console.error('[STAGE] failed to load unified data', err)
        setLoadError(err.message || 'Unknown error')
      })
  }, [])

  const handleCurtainDone = useCallback(() => {
    try {
      sessionStorage.setItem(CURTAIN_KEY, 'yes')
    } catch {
      /* sessionStorage unavailable; that's fine */
    }
    setCurtainVisible(false)
  }, [])

  // "← BACK" from a show detail walks the browser history rather than
  // navigating to a fixed URL. That way the user lands wherever they
  // came from (Cheapest, Shows catalogue, Sellers, a different show)
  // with their scroll position restored.
  const handleBack = useCallback(() => window.history.back(), [])

  const selectedShow =
    data && route.name === 'show'
      ? data.shows.find((s) => s.id === route.id) || null
      : null

  return (
    <div className="stg-app">
      {curtainVisible && <Curtain onComplete={handleCurtainDone} />}

      <Sidebar activeRoute={route} lastScrapedAt={data?.generated_at} />

      <main className="stg-main">
        {loadError && (
          <div className="stg-state stg-state-error">
            <div className="stg-state-eyebrow">DATA UNAVAILABLE</div>
            <div className="stg-state-msg">{loadError}</div>
            <div className="stg-state-hint">
              The build may have run without a unified.json present. Check
              that the scrapers have committed public/data/unified.json,
              then redeploy.
            </div>
          </div>
        )}

        {!data && !loadError && (
          <div className="stg-state">
            <div className="stg-state-eyebrow">LOADING</div>
            <div className="stg-state-msg">Reading tonight's listings…</div>
          </div>
        )}

        {data && route.name === 'show' && selectedShow && (
          <ShowDetail show={selectedShow} onBack={handleBack} />
        )}

        {/* Show URL points at an id we don't have — past run, bad link,
            data refreshed mid-session. We render a small not-found
            state so the user sees something instead of falling through
            to whichever component happens to match next. */}
        {data && route.name === 'show' && !selectedShow && (
          <div className="stg-state">
            <div className="stg-state-eyebrow">SHOW NOT FOUND</div>
            <div className="stg-state-msg">
              No show with id <b>{route.id}</b> in the current catalogue.
            </div>
            <div className="stg-state-hint">
              It may have finished its run, or the link may be out of
              date. Try the SHOWS tab for what's on.
            </div>
          </div>
        )}

        {data && route.name === 'cheapest' && <Cheapest data={data} />}

        {data && route.name === 'when' && <When data={data} />}

        {data && route.name === 'when-date' && (
          <WhenDate data={data} dateIso={route.date} />
        )}

        {data && route.name === 'shows' && (
          <Search data={data} filter={route.filter} />
        )}

        {data && route.name === 'venues' && <Venues data={data} />}

        {data && route.name === 'venue' && (
          <VenueDetail data={data} slug={route.slug} />
        )}

        {data && route.name === 'sellers' && <Sellers data={data} />}

        {data && route.name === 'data' && <Data data={data} />}
      </main>
    </div>
  )
}
