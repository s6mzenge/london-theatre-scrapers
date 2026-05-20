import { useEffect, useState } from 'react'
import Tassel from './Tassel.jsx'

// Rising-red-velvet intro animation. Renders once per session (App.jsx
// gates with sessionStorage). The actual velvet textures, valance shape,
// and rise animation are pure CSS in index.css — this file just sets up
// the structure and the skip / completion timing.

const ANIM_TOTAL_MS = 3050 // 350ms hold + 2600ms rise + 100ms buffer
const SKIP_VISIBLE_MS = 1700

export default function Curtain({ onComplete }) {
  const [done, setDone] = useState(false)
  const [showSkip, setShowSkip] = useState(false)

  useEffect(() => {
    const skipTimer = setTimeout(() => setShowSkip(true), SKIP_VISIBLE_MS)
    const completeTimer = setTimeout(() => {
      setDone(true)
      onComplete()
    }, ANIM_TOTAL_MS)
    return () => {
      clearTimeout(skipTimer)
      clearTimeout(completeTimer)
    }
  }, [onComplete])

  const handleSkip = () => {
    setDone(true)
    onComplete()
  }

  return (
    <div
      className={`stg-curtain ${done ? 'stg-curtain-done' : ''}`}
      aria-hidden="true"
    >
      <div className="stg-curtain-body" />
      <div className="stg-curtain-hem" />

      <div className="stg-valance">
        <div className="stg-valance-body" />
        <svg
          className="stg-valance-trim"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {/* Main gold trim curve matching the valance swag */}
          <path
            d="M 0,55 Q 50,128 100,55"
            stroke="var(--color-gold)"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
            fill="none"
          />
          {/* Thin highlight above */}
          <path
            d="M 0,53 Q 50,126 100,53"
            stroke="var(--color-gold-light)"
            strokeWidth="0.6"
            vectorEffect="non-scaling-stroke"
            fill="none"
          />
        </svg>

        <div className="stg-curtain-wordmark">
          STAGE<span className="stg-curtain-period">.</span>
        </div>

        <Tassel position="left" />
        <Tassel position="center" />
        <Tassel position="right" />
      </div>

      {showSkip && (
        <button
          type="button"
          className="stg-curtain-skip"
          onClick={handleSkip}
        >
          SKIP <span aria-hidden="true">↗</span>
        </button>
      )}
    </div>
  )
}
