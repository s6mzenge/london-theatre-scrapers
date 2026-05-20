#!/usr/bin/env node
/**
 * copy-data.mjs
 *
 * Dev-convenience script that makes the bundled example (or a locally
 * dedupe.py-generated unified.json) available at /data/unified.json
 * so `npm run dev` has something to render.
 *
 * In PRODUCTION on Cloudflare Pages, this script is a no-op: VITE_DATA_URL
 * is set in CF's env vars, the site fetches at runtime from
 * raw.githubusercontent.com, and no local file is needed.
 *
 * Lookup order for local dev (first hit wins):
 *   1. <repo-root>/dedupe_output/unified.json
 *      Output of running `python analysis/dedupe.py data/ --out dedupe_output/`
 *      locally — matches the path the GitHub Actions workflow uses.
 *   2. <repo-root>/unified/unified.json
 *      Matches the data-branch layout, in case you've checked out the
 *      `data` branch as a sibling worktree.
 *   3. <repo-root>/data/unified.json
 *      Legacy / explicit override path.
 *   4. web/public/data/unified.example.json
 *      Bundled sample (committed). Always present, so the site always
 *      renders something.
 *
 * Safe to run repeatedly. Runs as `prebuild` from package.json.
 */

import {
  existsSync,
  mkdirSync,
  copyFileSync,
  writeFileSync,
} from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(here, '..')
const repoRoot = resolve(webRoot, '..')

const candidates = [
  resolve(repoRoot, 'dedupe_output', 'unified.json'),
  resolve(repoRoot, 'unified', 'unified.json'),
  resolve(repoRoot, 'data', 'unified.json'),
]
const exampleSource = resolve(
  webRoot,
  'public',
  'data',
  'unified.example.json',
)
const target = resolve(webRoot, 'public', 'data', 'unified.json')

// Production short-circuit: if a runtime data URL is configured, the
// site fetches at runtime and we don't need a local file at all.
if (process.env.VITE_DATA_URL) {
  console.log(
    `[copy-data] VITE_DATA_URL is set (${process.env.VITE_DATA_URL})`,
  )
  console.log('[copy-data] skipping local copy; site will fetch at runtime')
  process.exit(0)
}

mkdirSync(dirname(target), { recursive: true })

let used = null
for (const candidate of candidates) {
  if (existsSync(candidate)) {
    copyFileSync(candidate, target)
    used = candidate
    break
  }
}

if (used) {
  console.log(`[copy-data] copied local dedupe output: ${used}`)
} else if (existsSync(exampleSource)) {
  copyFileSync(exampleSource, target)
  console.log('[copy-data] no local dedupe output found in:')
  for (const c of candidates) console.log(`              ${c}`)
  console.log(`[copy-data] using bundled example: ${exampleSource}`)
} else {
  const stub = {
    generated_at: null,
    show_count: 0,
    performance_count: 0,
    shows: [],
  }
  writeFileSync(target, JSON.stringify(stub, null, 2))
  console.log(`[copy-data] no data anywhere; wrote empty stub to ${target}`)
}
