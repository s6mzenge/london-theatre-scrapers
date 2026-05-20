import { relativeTime } from '../lib/dates.js'

const TABS = [
  { id: 'cheapest', label: 'CHEAPEST' },
  { id: 'search', label: 'SEARCH' },
  { id: 'sellers', label: 'SELLERS' },
]

export default function Sidebar({ activeView, onChangeView, lastScrapedAt }) {
  return (
    <aside className="stg-sidebar">
      <div className="stg-sb-top">
        <span className="stg-brand">
          STAGE<span className="stg-period">.</span>
        </span>
      </div>

      <nav className="stg-nav">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`stg-tab ${activeView === tab.id ? 'active' : ''}`}
            onClick={() => onChangeView(tab.id)}
          >
            {activeView === tab.id && <span className="stg-tab-mark" />}
            {tab.label}
          </button>
        ))}
      </nav>

      <div className="stg-sb-foot">
        {lastScrapedAt ? (
          <>
            <div className="stg-sb-foot-lbl">SCRAPED</div>
            <div className="stg-sb-foot-val">{relativeTime(lastScrapedAt)}</div>
          </>
        ) : (
          <>London &middot; nightly</>
        )}
      </div>
    </aside>
  )
}
