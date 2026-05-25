#!/usr/bin/env node
// Gzip-compress dist/data/*.json after Vite build.
//
// Two files are committed in public/data/ and need this treatment:
//   * unified.json       — the catalogue + per-perf prices (the main bundle)
//   * price_history.json — append-only per-perf snapshot log
//
// Both are >> 1 MiB raw and gzip to a small fraction of that. Cloudflare
// Pages caps individual files at 25 MiB; unified.json is ~25-33 MiB raw
// today and trending up, so gzipping is genuinely required, not just an
// optimisation. Browsers decompress transparently when the response
// carries Content-Encoding: gzip (set via public/_headers), but on this
// site we explicitly use DecompressionStream in src/lib/data.js because
// CF Pages doesn't honour that header from _headers for static assets.
import { readFileSync, writeFileSync, unlinkSync, existsSync } from 'node:fs'
import { gzipSync } from 'node:zlib'

const fmt = b => (b / 1024 / 1024).toFixed(2) + ' MiB'

function gzipOne(srcPath, { required }) {
  if (!existsSync(srcPath)) {
    if (required) {
      console.error(`[gzip-data] ${srcPath} not found - did Vite copy it from public/?`)
      process.exit(1)
    }
    console.log(`[gzip-data] ${srcPath} not present, skipping (optional)`)
    return
  }
  const raw = readFileSync(srcPath)
  const gz = gzipSync(raw, { level: 9 })
  writeFileSync(srcPath + '.gz', gz)
  unlinkSync(srcPath)
  const pct = ((gz.length / raw.length) * 100).toFixed(1)
  console.log(`[gzip-data] ${srcPath}: ${fmt(raw.length)} -> ${fmt(gz.length)} (${pct}%)`)
}

// unified.json is required; the build is broken without it.
gzipOne('dist/data/unified.json', { required: true })

// price_history.json is optional on the very first deploy after this
// feature lands (the workflow will start producing it on the next
// scrape), so we don't hard-fail if it's absent yet.
gzipOne('dist/data/price_history.json', { required: false })
