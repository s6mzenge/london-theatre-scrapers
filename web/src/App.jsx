import { useState, useEffect, useCallback } from 'react'
import Curtain from './components/Curtain.jsx'
import Sidebar from './components/Sidebar.jsx'
import Cheapest from './components/Cheapest.jsx'
import Search from './components/Search.jsx'
import Sellers from './components/Sellers.jsx'
import ShowDetail from './components/ShowDetail.jsx'
import { loadUnifiedData } from './lib/data.js'

const CURTAIN_KEY = 'stg-curtain-seen'

export default function App() {
  const [view, setView] = useState('cheapest')
  const [selectedShowId, setSelectedShowId] = useState(null)
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

  const handleSelectShow = useCallback((id) => {
    setSelectedShowId(id)
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [])

  const handleBack = useCallback(() => setSelectedShowId(null), [])

  const handleChangeView = useCallback((v) => {
    setView(v)
    setSelectedShowId(null)
  }, [])

  const selectedShow =
    data && selectedShowId
      ? data.shows.find((s) => s.id === selectedShowId) || null
      : null

  return (
    <div className="stg-app">
      {curtainVisible && <Curtain onComplete={handleCurtainDone} />}

      <Sidebar
        activeView={view}
        onChangeView={handleChangeView}
        lastScrapedAt={data?.generated_at}
      />

      <main className="stg-main">
        {loadError && (
          <div className="stg-state stg-state-error">
            <div className="stg-state-eyebrow">DATA UNAVAILABLE</div>
            <div className="stg-state-msg">{loadError}</div>
            <div className="stg-state-hint">
              The build may have run without a unified.json present. Check
              that the scrapers have committed data/unified.json to the repo
              root, then redeploy.
            </div>
          </div>
        )}

        {!data && !loadError && (
          <div className="stg-state">
            <div className="stg-state-eyebrow">LOADING</div>
            <div className="stg-state-msg">Reading tonight's listings…</div>
          </div>
        )}

        {data && selectedShow && (
          <ShowDetail show={selectedShow} onBack={handleBack} />
        )}

        {data && !selectedShow && view === 'cheapest' && (
          <Cheapest data={data} onSelectShow={handleSelectShow} />
        )}

        {data && !selectedShow && view === 'search' && (
          <Search data={data} onSelectShow={handleSelectShow} />
        )}

        {data && !selectedShow && view === 'sellers' && (
          <Sellers data={data} onSelectShow={handleSelectShow} />
        )}
      </main>
    </div>
  )
}
