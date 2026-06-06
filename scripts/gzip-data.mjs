#!/usr/bin/env node
// Gzip-compress dist/data/*.json after Vite build.
//
// Only ONE file needs build-time gzipping now:
//   * unified.json — the catalogue + per-perf prices (the main bundle).
//     Committed raw in public/data/, ~25-33 MiB and trending up. Vite
//     copies it to dist/data/unified.json; we gzip it here and drop the
//     raw so only the .gz ships.
//
// Price history is NO LONGER handled here. It used to be a single
// price_history.json that we gzipped at build time, but it crossed
// GitHub's hard 100 MB-per-file push limit. It's now stored as
// per-month shards committed ALREADY gzipped under
// public/data/price_history/<YYYY-MM>.json.gz, plus a tiny plaintext
// index.json manifest (see scraper/analysis/update_price_history.py).
// Vite copies that whole directory into dist/data/price_history/ verbatim,
// so there is nothing to compress here — the shards are serve-ready as-is,
// and the client decompresses each with DecompressionStream exactly like
// unified.json.gz (src/lib/data.js).
//
// Cloudflare Pages caps individual files at 25 MiB; unified.json.gz and
// every monthly shard are comfortably under that. Browsers decompress
// transparently when the response carries Content-Encoding: gzip, but on
// this site we explicitly use DecompressionStream in src/lib/data.js
// because CF Pages doesn't honour that header from _headers for static
// assets.
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
