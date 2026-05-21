import { relativeTime } from '../lib/dates.js'
import { Link } from '../lib/router.jsx'

// Tabs are anchors (via the router's <Link>) instead of buttons so the
// browser's standard "open in new tab / new window" affordances work
// — cmd-click, middle-click, and right-click all behave the way users
// expect from a real navigation surface. Plain left-clicks are still
// intercepted for SPA pushState navigation.
const TABS = [
  { id: 'cheapest', href: '/', label: 'CHEAPEST' },
  { id: 'when', href: '/when', label: 'WHEN' },
  { id: 'shows', href: '/shows', label: 'SHOWS' },
  { id: 'venues', href: '/venues', label: 'VENUES' },
  { id: 'sellers', href: '/sellers', label: 'SELLERS' },
]

// A tab counts as "active" when the current route lives under it. The
// SHOWS tab stays lit for the /shows/:id detail page too, because the
// detail page logically belongs under the catalogue. WHEN owns the
// per-date pages /when/:date; VENUES owns the per-venue pages
// /venues/:slug. Without this, deep-linking would leave no tab lit.
function isTabActive(tabId, route) {
  if (route.name === tabId) return true
  if (tabId === 'shows' && route.name === 'show') return true
  if (tabId === 'when' && route.name === 'when-date') return true
  if (tabId === 'venues' && route.name === 'venue') return true
  return false
}

export default function Sidebar({ activeRoute, lastScrapedAt }) {
  return (
    <aside className="stg-sidebar">
      <div className="stg-sb-top">
        <span className="stg-brand">
          STAGE<span className="stg-period">.</span>
        </span>
      </div>

      <nav className="stg-nav">
        {TABS.map((tab) => {
          const active = isTabActive(tab.id, activeRoute)
          return (
            <Link
              key={tab.id}
              href={tab.href}
              className={`stg-tab ${active ? 'active' : ''}`}
              aria-current={active ? 'page' : undefined}
            >
              {active && <span className="stg-tab-mark" />}
              {tab.label}
            </Link>
          )
        })}
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
        <Link
          href="/data"
          className={`stg-sb-foot-link ${
            activeRoute.name === 'data' ? 'active' : ''
          }`}
        >
          DATA &amp; METHODOLOGY
        </Link>
      </div>
    </aside>
  )
}
