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
//              LTP. wordmark and three tassels — all together. The side
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
            LTP. wordmark. */}
      <div className="stg-curtain-rise">
        <div className="stg-curtain-body" />

        <div className="stg-valance">
          <div className="stg-valance-body" />
          <div className="stg-curtain-wordmark">
            {/* Same wordmark SVG as <Brand /> in the sidebar, but rendered
                in gold against the velvet. Body uses currentColor (gold-
                light from .stg-curtain-wordmark); period sits inside the
                .stg-curtain-period group so it picks up brick the same
                way the text "." used to. */}
            <svg
              className="stg-curtain-wordmark-svg"
              viewBox="0 0 210 145"
              role="img"
              aria-label="STAGE"
              xmlns="http://www.w3.org/2000/svg"
            >
              <path
                fill="currentColor"
                d="M 133.40291,142.40186 c -0.32432,-0.54571 0.15315,-1.03794 1.36901,-1.4113 4.185,-1.28509 5.75756,-4.32136 6.41016,-12.37661 0.32342,-3.99219 0.55834,-77.985479 0.25685,-80.901965 -0.40238,-3.892447 -1.63096,-5.802038 -4.19913,-6.526736 -0.7312,-0.206333 -5.18158,-0.284423 -17.20574,-0.301907 -15.73164,-0.02287 -16.24075,-0.01022 -16.92823,0.422268 -0.38984,0.245218 -0.88299,0.809352 -1.0959,1.25363 -0.36571,0.763111 -0.38711,3.070336 -0.38711,41.721751 0,38.335269 0.0247,41.050449 0.39184,43.079339 0.84986,4.69643 2.00614,7.53438 3.96127,9.72242 1.44605,1.61831 3.04404,2.64915 5.44924,3.51525 1.53657,0.55331 1.81426,0.73045 1.81426,1.15736 v 0.50406 l -18.630412,-0.0494 c -19.393502,-0.0515 -23.922526,-0.19537 -29.839148,-0.94765 -9.237186,-1.17449 -15.679542,-3.05396 -28.134342,-8.20783 -9.385826,-3.8839 -13.529038,-5.50952 -17.02504,-6.67992 -4.66833,-1.56285 -9.7834876,-2.54717 -14.5918775,-2.80794 -2.6814417,-0.14541 -2.7945634,-0.17167 -2.7945634,-0.6488 0,-0.4158 0.224386,-0.56206 1.3698839,-0.89292 1.1023921,-0.31843 1.5680642,-0.60936 2.3848116,-1.48994 1.2906639,-1.39155 1.8444872,-2.91638 2.4467446,-6.73663 0.4639401,-2.94286 0.4676687,-3.28116 0.5452013,-49.461449 C 9.0519465,15.937774 9.0239134,14.130867 8.1357097,10.519363 7.3276622,7.2337756 5.4617162,4.5034177 2.7849135,2.6897775 1.9942376,2.1540633 1.3473217,1.6317632 1.3473217,1.529112 c 0,-0.285089 2.3138532,0.4423717 9.8631623,3.1009158 9.575468,3.3720702 10.480395,3.8321062 11.82528,6.0116102 1.554266,2.518824 1.419128,-2.0806034 1.557544,53.011463 0.135805,54.053029 0.0554,50.740819 1.316818,54.248039 0.703092,1.95488 2.898669,4.16586 5.748053,5.78842 2.430975,1.3843 7.142915,3.64815 10.237703,4.91875 9.187303,3.77189 19.004082,5.53342 27.449179,4.92548 8.919191,-0.64207 11.943828,-2.47484 12.652385,-7.66673 0.178107,-1.30506 0.229211,-13.16076 0.182737,-42.393852 -0.06089,-38.322666 -0.08603,-40.616285 -0.451131,-41.195707 -0.905435,-1.436965 -0.823515,-1.424743 -9.029294,-1.347132 -9.640646,0.09118 -11.882153,0.42028 -15.241996,2.237825 -4.046026,2.18875 -6.85656,6.096202 -9.078126,12.621217 -0.470806,1.382811 -0.864695,2.191998 -1.089911,2.239061 -0.320158,0.06694 -0.352671,-0.925978 -0.352671,-10.769831 V 36.415065 l 62.521506,0.0035 c 38.40629,0.0022 63.78961,0.08968 65.80922,0.22689 13.55432,0.920872 22.97406,5.316637 28.18227,13.151377 5.284,7.94878 6.04976,20.588037 1.82527,30.12684 -1.85603,4.190854 -5.64891,8.736379 -9.68458,11.606327 -2.40858,1.712861 -6.94409,4.030093 -10.13103,5.176041 -4.71962,1.697071 -11.87153,3.227118 -17.53451,3.75125 -2.98314,0.2761 -7.233,0.3094 -7.233,0.0567 0,-0.098 0.43852,-0.31282 0.97447,-0.47738 3.7065,-1.138093 7.37734,-4.057162 9.28992,-7.387382 1.72296,-3.000048 2.72215,-6.087511 3.15077,-9.735831 0.13992,-1.190952 0.27536,-2.1974 0.30096,-2.236546 0.0255,-0.03911 0.87057,0.129506 1.87769,0.374795 3.2038,0.780293 5.04611,1.022429 7.90086,1.038418 3.2013,0.01792 3.21239,0.01232 4.82912,-2.450425 1.22233,-1.861957 1.99386,-3.735959 2.68734,-6.527366 0.50283,-2.023985 0.60638,-2.973635 0.67101,-6.15419 0.0876,-4.311263 -0.21246,-6.713763 -1.2362,-9.896937 -2.27676,-7.07916 -6.81447,-11.886661 -13.31915,-14.11103 -3.81407,-1.304273 -5.62607,-1.520636 -12.72698,-1.519669 -5.82344,7.91e-4 -6.31203,0.03205 -6.96405,0.44547 -0.48877,0.309926 -0.77297,0.721834 -0.93813,1.359702 -0.16715,0.645488 -0.21394,13.390601 -0.15884,43.260678 0.0838,45.435033 0.0453,43.837693 1.15149,47.702013 0.99701,3.48271 3.33291,5.88138 6.67417,6.85352 1.17621,0.34221 1.64206,0.73119 1.46154,1.22043 -0.1192,0.32302 -1.79195,0.35958 -16.45565,0.35958 -10.74205,0 -16.36968,-0.0786 -16.45963,-0.22997 z M 88.471933,83.464361 V 42.227356 H 87.48562 86.499299 l -0.05542,41.327804 -0.05542,41.32779 1.041699,-0.0908 1.041708,-0.0908 z m 7.561762,-0.450106 V 42.087178 l -0.931522,0.07009 -0.931521,0.07009 -0.05542,40.856989 -0.05542,40.856983 h 0.986917 0.986917 z m 77.645775,9.771015 c 1.22436,-1.442745 2.42289,-3.529789 3.2418,-5.645082 l 0.61381,-1.5855 1.05817,0.103924 c 2.86872,0.281773 6.31408,0.06702 8.69162,-0.54174 2.0067,-0.513812 6.43985,-2.345886 6.22277,-2.571644 -0.0375,-0.03903 -0.55682,0.08589 -1.15394,0.277636 -4.01896,1.290548 -10.10141,1.638025 -14.01739,0.800772 -2.11573,-0.452346 -2.29133,-0.40213 -2.29859,0.657351 -0.0127,1.843221 -1.93751,7.141316 -3.33722,9.185493 -0.71003,1.036971 -0.14615,0.644593 0.97897,-0.681214 z"
              />
              <g className="stg-curtain-period">
                <path
                  fill="currentColor"
                  d="M 193.40518,143.43402 c -4.38903,-1.28518 -7.1268,-5.36179 -6.78326,-10.10042 0.23352,-3.22099 2.30355,-6.39005 5.11374,-7.82874 2.7986,-1.43276 6.86284,-1.07321 9.33272,0.82563 1.37489,1.05701 2.65916,2.78434 3.265,4.39137 0.68986,1.82993 0.69535,4.88111 0.012,6.69151 -1.03215,2.73457 -2.83185,4.66424 -5.29646,5.67892 -1.55765,0.64128 -4.09592,0.79498 -5.64378,0.34173 z"
                />
              </g>
            </svg>
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
