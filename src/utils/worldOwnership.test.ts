import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { after, describe, it } from 'node:test'

/**
 * Who can see which world.
 *
 * The rule that needs pinning is the one for worlds with no owner. They are
 * everything generated before ownership was recorded, and the tempting answer
 * -- hide what nobody claims -- would make an operator's whole library vanish
 * the moment they switch authentication on. They are shared instead, and that
 * is a decision, not an oversight.
 *
 * `worldsScanner` resolves its directory from `process.cwd()` at import, so the
 * fixture is a temporary tree the process moves into before importing it.
 */

const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'imageworld-owners-'))
const ORIGINAL_CWD = process.cwd()

function world(slug: string, owner?: string) {
  const dir = path.join(ROOT, 'public', 'worlds', slug)
  fs.mkdirSync(path.join(dir, 'output', 'world'), { recursive: true })
  fs.writeFileSync(
    path.join(dir, 'project.json'),
    JSON.stringify({ slug, display_name: slug, ...(owner ? { owner } : {}) }, null, 2),
  )
}

world('ada-only', 'ada')
world('grace-only', 'grace')
world('from-before-ownership')

process.chdir(ROOT)
const { readWorlds } = await import('./worldsScanner.ts')

after(() => {
  process.chdir(ORIGINAL_CWD)
  fs.rmSync(ROOT, { recursive: true, force: true })
})

const slugs = (viewer?: string) => readWorlds(viewer).map((entry) => entry.slug).sort()

describe('readWorlds', () => {
  it('returns everything when there is no viewer', () => {
    // An instance with no authentication configured passes no viewer, and has
    // always shown every world on the disk.
    assert.deepEqual(slugs(), ['ada-only', 'from-before-ownership', 'grace-only'])
  })

  it('hides another account\'s worlds', () => {
    assert.deepEqual(slugs('ada'), ['ada-only', 'from-before-ownership'])
    assert.deepEqual(slugs('grace'), ['from-before-ownership', 'grace-only'])
  })

  it('shows unowned worlds to everyone rather than to no one', () => {
    assert.ok(slugs('someone-new').includes('from-before-ownership'))
  })

  it('carries the owner through to the project it reports', () => {
    const entry = readWorlds('ada').find((candidate) => candidate.slug === 'ada-only')
    assert.equal(entry?.project.owner, 'ada')
  })

  it('leaves an unowned world without an owner rather than inventing one', () => {
    const entry = readWorlds().find((candidate) => candidate.slug === 'from-before-ownership')
    assert.equal(entry?.project.owner, undefined)
  })
})
