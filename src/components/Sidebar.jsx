import { useEffect, useState } from 'react'
import { relativeTime } from '../lib/dates.js'
import { Link } from '../lib/router.jsx'
import Brand from './Brand.jsx'

// Tabs are anchors (via the router's <Link>) instead of buttons so the
// browser's standard "open in new tab / new window" affordances work
// — cmd-click, middle-click, and right-click all behave the way users
// expect from a real navigation surface. Plain left-clicks are still
// intercepted for SPA pushState navigation.
const TABS = [
  { id: 'cheapest', href: '/', label: 'CHEAPEST' },
  { id: 'today', href: '/today', label: 'TODAY · STACKED' },
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
  // Drawer open/close — only relevant on mobile. On desktop the
  // sidebar is permanently visible and `open` is ignored by CSS.
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)

  // Lock body scroll while the drawer is open so the page underneath
  // doesn't move under the user's finger. Escape closes too.
  useEffect(() => {
    if (!open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = prev
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Resizing across the mobile/desktop breakpoint while the drawer is
  // open would leave us in a weird half-state (scrim hidden by CSS but
  // body scroll still locked). Reset on the way out.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const mq = window.matchMedia('(min-width: 641px)')
    const handler = () => mq.matches && setOpen(false)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  return (
    <>
      {/* Mobile-only top bar. Hidden on desktop via CSS. */}
      <button
        type="button"
        className="stg-mobile-topbar"
        onClick={() => setOpen(true)}
        aria-label="Open navigation menu"
        aria-expanded={open}
        aria-controls="stg-sidebar"
      >
        <span className="stg-mobile-burger" aria-hidden="true">
          <span /><span /><span />
        </span>
        <Brand />
      </button>

      {/* Scrim behind the drawer — tap to close. Mobile-only. */}
      {open && (
        <div
          className="stg-sidebar-scrim"
          onClick={close}
          aria-hidden="true"
        />
      )}

      <aside
        id="stg-sidebar"
        className={`stg-sidebar ${open ? 'open' : ''}`}
      >
        <div className="stg-sb-top">
          <Brand />
          <button
            type="button"
            className="stg-sidebar-close"
            onClick={close}
            aria-label="Close navigation menu"
          >
            ×
          </button>
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
                onClick={close}
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
            onClick={close}
            className={`stg-sb-foot-link ${
              activeRoute.name === 'data' ? 'active' : ''
            }`}
          >
            DATA &amp; METHODOLOGY
          </Link>
        </div>
      </aside>
    </>
  )
}
