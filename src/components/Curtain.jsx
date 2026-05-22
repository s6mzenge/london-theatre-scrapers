import { useEffect, useState } from 'react'
import Tassel from './Tassel.jsx'

// Rising red velvet intro animation. Plays once per session (App.jsx gates
// with sessionStorage). The velvet textures, valance shape, and animation
// timing all live in additions.css — this file just sets up the DOM
// structure and the skip / completion timing.
//
// Animation beats:
//   0–0.35s    Hold (audience settles, eye registers the curtain).
//   0.35–2.75s Rising panel lifts: main curtain body, valance with the
//              STAGE. wordmark and three tassels — all together. The side
//              drapes stay in place.
//   2.85–3.95s Side drapes part — left drape slides off to the left, right
//              to the right, leaving the page underneath cleanly revealed.
//   3.95s      onComplete fires, sessionStorage flag is set, component
//              unmounts on the next render.

const ANIM_TOTAL_MS = 4050 // 350ms hold + 2400ms rise + 100ms gap + 1100ms side-out + 100ms buffer
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
      {/* Rising panel — main body and valance with wordmark plus the
          left/right corner tassels. The gold trim and centre tassel
          deliberately sit OUTSIDE .stg-valance so they aren't subject
          to its swag clip-path:

          - The trim then sits exactly at the swag's edge instead of
            hovering above it (which had left a strip of darker velvet
            visible between the gold line and the swag's true bottom).

          - The centre tassel hangs FROM the trim line and extends
            downward into the curtain body, instead of hanging from the
            top of the valance with its cord crossing through the
            STAGE. wordmark. */}
      <div className="stg-curtain-rise">
        <div className="stg-curtain-body" />

        <div className="stg-valance">
          <div className="stg-valance-body" />
          <div className="stg-curtain-wordmark">
            STAGE<span className="stg-curtain-period">.</span>
          </div>
          <Tassel position="left" />
          <Tassel position="right" />
        </div>

        {/* Gold trim along the swag — sibling of .stg-valance, so the
            curve sits AT the clip-path edge. Path coordinates are
            chosen so the bezier traces the same shape as the polygon
            clip-path below: (0,60) → (50,100) → (100,60). */}
        <svg
          className="stg-valance-trim"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {/* Main gold trim curve following the swag's bottom edge */}
          <path
            d="M 0,60 Q 50,140 100,60"
            stroke="var(--color-gold)"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
            fill="none"
          />
          {/* Thin highlight just above the main trim */}
          <path
            d="M 0,58 Q 50,138 100,58"
            stroke="var(--color-gold-light)"
            strokeWidth="0.6"
            vectorEffect="non-scaling-stroke"
            fill="none"
          />
        </svg>

        {/* Centre tassel — sits at the swag's deepest dip. Outside the
            valance so it can hang DOWN past the swag's bottom edge
            into the curtain body, rather than being clipped to the
            valance area. */}
        <Tassel position="center" />
      </div>

      {/* Side drapes — gathered velvet panels with mid-height tiebacks.
          Stay in place during the main rise; sweep outward in beat 2. */}
      <div className="stg-side-drape stg-side-drape-left">
        <div className="stg-side-drape-body" />
        <div className="stg-side-drape-tieback">
          <SideTieback side="left" />
        </div>
      </div>
      <div className="stg-side-drape stg-side-drape-right">
        <div className="stg-side-drape-body" />
        <div className="stg-side-drape-tieback">
          <SideTieback side="right" />
        </div>
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

// Tieback decoration for each side drape — two gold cord curves
// wrapping the drape at mid-height plus a small tassel hanging at the
// gather point.
function SideTieback({ side }) {
  const isLeft = side === 'left'
  const wall = isLeft ? 5 : 95
  const gather = isLeft ? 90 : 10
  const tasselX = isLeft ? 84 : 16

  return (
    <svg viewBox="0 0 100 60" preserveAspectRatio="none">
      <path
        d={`M ${wall},5 Q 50,32 ${gather},30`}
        stroke="var(--color-gold)"
        strokeWidth="1.5"
        vectorEffect="non-scaling-stroke"
        fill="none"
      />
      <path
        d={`M ${wall},5 Q 50,40 ${gather},38`}
        stroke="var(--color-gold)"
        strokeWidth="1.5"
        vectorEffect="non-scaling-stroke"
        fill="none"
      />
      <ellipse cx={tasselX} cy="34" rx="4" ry="5" fill="var(--color-gold)" />
      <ellipse cx={tasselX} cy="33" rx="2" ry="3" fill="var(--color-gold-light)" opacity="0.7" />
      <line x1={tasselX} y1="39" x2={tasselX - 4} y2="55" stroke="var(--color-gold)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <line x1={tasselX} y1="39" x2={tasselX - 2} y2="58" stroke="var(--color-gold)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <line x1={tasselX} y1="39" x2={tasselX} y2="58" stroke="var(--color-gold-light)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <line x1={tasselX} y1="39" x2={tasselX + 2} y2="58" stroke="var(--color-gold)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <line x1={tasselX} y1="39" x2={tasselX + 4} y2="55" stroke="var(--color-gold)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
