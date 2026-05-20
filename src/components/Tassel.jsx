// Gold tassel: a thin cord, a round head, and six fanned fringe strands.
// Three of these hang from the valance — flush left, flush right, and one
// at the centre swag point. Width scales with viewport via the parent's
// CSS clamp.

export default function Tassel({ position }) {
  return (
    <div className={`stg-tassel stg-tassel-${position}`}>
      <svg viewBox="0 0 20 60" preserveAspectRatio="xMidYMin meet">
        {/* Cord */}
        <line
          x1="10"
          y1="0"
          x2="10"
          y2="14"
          stroke="var(--color-gold)"
          strokeWidth="1.5"
        />
        {/* Head — outer + inner highlight for a touch of depth */}
        <ellipse cx="10" cy="19" rx="5" ry="6.5" fill="var(--color-gold)" />
        <ellipse
          cx="10"
          cy="17.5"
          rx="3"
          ry="4"
          fill="var(--color-gold-light)"
          opacity="0.7"
        />
        {/* Six fanned fringe strands */}
        <line x1="10" y1="24" x2="5" y2="54" stroke="var(--color-gold)" strokeWidth="1.2" />
        <line x1="10" y1="24" x2="7.5" y2="57" stroke="var(--color-gold)" strokeWidth="1.2" />
        <line x1="10" y1="24" x2="9.5" y2="58" stroke="var(--color-gold)" strokeWidth="1.2" />
        <line x1="10" y1="24" x2="10.5" y2="58" stroke="var(--color-gold-light)" strokeWidth="1" />
        <line x1="10" y1="24" x2="12.5" y2="57" stroke="var(--color-gold)" strokeWidth="1.2" />
        <line x1="10" y1="24" x2="15" y2="54" stroke="var(--color-gold)" strokeWidth="1.2" />
      </svg>
    </div>
  )
}
