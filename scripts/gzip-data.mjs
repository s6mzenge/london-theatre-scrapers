#!/usr/bin/env node
// Gzip-compress dist/data/unified.json after Vite build.
// Cloudflare Pages caps individual files at 25 MiB; the raw unified.json
// is ~33 MiB but gzips to ~3 MiB. Browsers decompress transparently when
// the response carries Content-Encoding: gzip (set via public/_headers).
import { readFileSync, writeFileSync, unlinkSync, existsSync } from 'node:fs'
import { gzipSync } from 'node:zlib'

const src = 'dist/data/unified.json'
if (!existsSync(src)) {
  console.error(`[gzip-data] ${src} not found - did Vite copy it from public/?`)
  process.exit(1)
}

const raw = readFileSync(src)
const gz = gzipSync(raw, { level: 9 })
writeFileSync(src + '.gz', gz)
unlinkSync(src)

const fmt = b => (b / 1024 / 1024).toFixed(2) + ' MiB'
const pct = ((gz.length / raw.length) * 100).toFixed(1)
console.log(`[gzip-data] ${fmt(raw.length)} -> ${fmt(gz.length)} (${pct}%)`)
