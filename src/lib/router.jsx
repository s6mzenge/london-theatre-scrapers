// Tiny hand-rolled router for STAGE. We have three top-level tabs and
// one parameterized detail page — not enough surface area to justify
// pulling in react-router-dom (~12 kB gzipped on top of React). This
// file is ~70 lines and exposes everything the rest of the app needs:
//
//   useRoute()             → current route as a plain object
//   navigate(path, opts)   → push (default) or replace a new URL
//   <Link href="/x">       → anchor that intercepts plain left-clicks
//                            but preserves modifier-click / middle-click
//                            so "open in new tab" still works.
//
// Cloudflare Pages already rewrites every path to index.html via the
// _redirects file, so deep-links like /shows/hamilton boot React on
// the right page without any extra config. Vite's dev server has the
// same SPA fallback on by default, so dev and prod behave the same.
//
// State flow:
//   navigate(path) → history.pushState → notify subscribers → useRoute
//                    fires setState → React re-renders with new route.
//   Browser back  → popstate event → setState directly → same render.

import { useState, useEffect } from 'react'

// ---------------------------------------------------------------------------
// URL <-> route object
// ---------------------------------------------------------------------------

// Route shape:
//   { name: 'cheapest' }
//   { name: 'today' }
//   { name: 'when' }
//   { name: 'when-date', date: 'YYYY-MM-DD' }
//   { name: 'shows', filter?: string }
//   { name: 'show', id: string }
//   { name: 'venues' }
//   { name: 'venue', slug: string }
//   { name: 'sellers' }
//   { name: 'data' }
//
// Anything unrecognised maps back to 'cheapest' so a stale or mistyped
// URL doesn't dead-end the user — they see the home page instead of a
// blank screen.

function parsePath(pathname, search) {
  const p = pathname.replace(/\/+$/, '') || '/'
  if (p === '/') return { name: 'cheapest' }
  if (p === '/today') return { name: 'today' }
  if (p === '/when') return { name: 'when' }
  const whenDate = p.match(/^\/when\/(\d{4}-\d{2}-\d{2})$/)
  if (whenDate) return { name: 'when-date', date: whenDate[1] }
  if (p === '/shows') {
    // ?filter=closing-soon | opening-soon | limited | gems | exclusives
    const params = new URLSearchParams(search || '')
    const filter = params.get('filter')
    return { name: 'shows', filter: filter || null }
  }
  if (p === '/sellers') return { name: 'sellers' }
  if (p === '/venues') return { name: 'venues' }
  const venueMatch = p.match(/^\/venues\/(.+)$/)
  if (venueMatch) return { name: 'venue', slug: decodeURIComponent(venueMatch[1]) }
  if (p === '/data') return { name: 'data' }
  const m = p.match(/^\/shows\/(.+)$/)
  if (m) return { name: 'show', id: decodeURIComponent(m[1]) }
  return { name: 'cheapest' }
}

// ---------------------------------------------------------------------------
// Subscription — useRoute() reads, navigate() writes, listeners get notified
// ---------------------------------------------------------------------------

const listeners = new Set()

function currentRoute() {
  if (typeof window === 'undefined') return { name: 'cheapest' }
  return parsePath(window.location.pathname, window.location.search)
}

function notify() {
  const r = currentRoute()
  for (const fn of listeners) fn(r)
}

export function useRoute() {
  const [route, setRoute] = useState(currentRoute)
  useEffect(() => {
    listeners.add(setRoute)
    // popstate covers the back/forward buttons. The browser restores
    // scroll position automatically on popstate, which is the whole
    // reason we don't auto-scroll inside navigate() on a back-nav.
    const onPop = () => setRoute(currentRoute())
    window.addEventListener('popstate', onPop)
    return () => {
      listeners.delete(setRoute)
      window.removeEventListener('popstate', onPop)
    }
  }, [])
  return route
}

export function navigate(path, { replace = false, scroll = false } = {}) {
  if (typeof window === 'undefined') return
  // Already on this URL — clicking the active tab shouldn't pollute
  // the history stack with no-op entries. Compare pathname + search
  // so /shows?filter=foo → /shows?filter=bar still pushes correctly.
  const current = window.location.pathname + window.location.search
  if (path === current) return
  const method = replace ? 'replaceState' : 'pushState'
  window.history[method]({}, '', path)
  // Caller opts in to scroll-to-top. We don't do this by default
  // because tab-to-tab nav shouldn't yank the user back to the top —
  // only show-detail entry does (matches the previous useState-based
  // behaviour in App.jsx).
  if (scroll) window.scrollTo({ top: 0, behavior: 'auto' })
  notify()
}

// ---------------------------------------------------------------------------
// <Link> — anchor with SPA click interception
// ---------------------------------------------------------------------------
// Preserves modifier-click and middle-click so users can still open in
// a new tab. Plain left-clicks become pushState navigations. Any
// onClick passed by the parent runs first; if it preventDefault()s,
// we bail out and let the anchor behave normally.

export function Link({
  href,
  className,
  children,
  scroll = false,
  onClick,
  ...rest
}) {
  const handleClick = (e) => {
    if (onClick) onClick(e)
    if (e.defaultPrevented) return
    // Let the browser handle anything that isn't a plain left-click:
    // cmd/ctrl-click opens in new tab, shift-click in new window,
    // middle-click in new tab, right-click shows the context menu.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    if (e.button !== undefined && e.button !== 0) return
    e.preventDefault()
    navigate(href, { scroll })
  }
  return (
    <a href={href} className={className} onClick={handleClick} {...rest}>
      {children}
    </a>
  )
}

// ---------------------------------------------------------------------------
// <ShowLink> — convenience wrapper for clickable show entries
// ---------------------------------------------------------------------------
// Almost every list in the app links to a show detail page. Centralising
// the href construction here means:
//
//   1. URL scheme changes (e.g. /shows/:id → /show/:id) happen in one
//      place instead of six.
//   2. Show-detail entry scrolls to top by default — matches the old
//      useState-based behaviour without each callsite remembering to
//      pass `scroll`.
//   3. The id is URL-encoded once, here, so a show id with a slash or
//      a space doesn't blow up the path.

export function ShowLink({ id, ...rest }) {
  return <Link href={`/shows/${encodeURIComponent(id)}`} scroll {...rest} />
}
