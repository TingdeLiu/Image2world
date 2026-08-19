import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, it } from 'node:test'

import { buildWalkableMap } from './walkableMap.ts'

/**
 * The walkable map is the minimap's only source, and it is optional by design:
 * a world without one still generates. That makes silent degradation the whole
 * risk here -- if this swallows the wrong things, a broken backend looks like a
 * world that simply has no minimap, and nobody finds out for weeks.
 *
 * The timeout is the other half. Passing a caller's signal used to *replace* the
 * timeout instead of joining it, so the interactive path -- the only one that
 * passes a signal -- had no timeout at all and a wedged backend left the
 * generation stream at 86% forever.
 */

const realFetch = globalThis.fetch
const workDirs: string[] = []

afterEach(() => {
  globalThis.fetch = realFetch
  for (const dir of workDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

function worldDir({ withCollider = true } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'imageworld-walkable-'))
  workDirs.push(dir)
  if (withCollider) fs.writeFileSync(path.join(dir, '0-world.glb'), Buffer.from('glTF stand-in'))
  return dir
}

const META = {
  cell_size: 0.25,
  cols: 40,
  rows: 40,
  origin: [-5, -5] as [number, number],
  standable_area: 18.5,
  walkable_area: 12.25,
  metric_scale_factor: 1,
}

interface Captured {
  url?: string
  form?: FormData
  signal?: AbortSignal
}

function stubBackend(
  captured: Captured,
  respond: () => Response | Promise<Response> = () =>
    Response.json({ mask_png: Buffer.from('fake png').toString('base64'), ...META }),
) {
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    captured.url = String(url)
    captured.form = init.body as FormData
    captured.signal = init.signal ?? undefined
    return respond()
  }) as typeof globalThis.fetch
}

describe('buildWalkableMap', () => {
  it('does nothing when the world has no collision mesh to map', async () => {
    let called = false
    globalThis.fetch = (async () => { called = true; return Response.json({}) }) as typeof globalThis.fetch

    assert.equal(await buildWalkableMap('http://backend', worldDir({ withCollider: false })), undefined)
    assert.equal(called, false, 'a world with no collider has no floor to map')
  })

  it('writes the mask and the metadata beside the collider', async () => {
    const dir = worldDir()
    stubBackend({})

    const meta = await buildWalkableMap('http://backend', dir)

    assert.deepEqual(meta, META)
    assert.ok(fs.existsSync(path.join(dir, '0-world-walkable.png')))
    const written = JSON.parse(
      fs.readFileSync(path.join(dir, '0-world-walkable.json'), 'utf8'),
    ) as Record<string, unknown>
    assert.equal(written.mask_png, undefined, 'the base64 image does not belong in the sidecar')
    assert.deepEqual(written, META)
  })

  it('defaults to the SHARP orientation and sends no explicit rotation', async () => {
    const captured: Captured = {}
    stubBackend(captured)

    await buildWalkableMap('http://backend/', worldDir())

    assert.equal(captured.form?.get('flip_y'), 'true')
    assert.equal(captured.form?.get('rotation_x'), null)
    assert.equal(captured.form?.get('metric_scale'), '1')
  })

  it('passes a nerfstudio world its explicit rotation and calibrated scale', async () => {
    const captured: Captured = {}
    stubBackend(captured)

    await buildWalkableMap('http://backend', worldDir(), {
      flipY: false,
      rotationX: -Math.PI / 2,
      metricScale: 1.75,
    })

    assert.equal(captured.form?.get('flip_y'), 'false')
    assert.equal(captured.form?.get('rotation_x'), String(-Math.PI / 2))
    assert.equal(captured.form?.get('metric_scale'), '1.75')
  })

  it('sends a spawn point only when it has one', async () => {
    const withSpawn: Captured = {}
    stubBackend(withSpawn)
    await buildWalkableMap('http://backend', worldDir(), { spawn: [1.5, -2.5] })
    assert.equal(withSpawn.form?.get('spawn_x'), '1.5')
    assert.equal(withSpawn.form?.get('spawn_z'), '-2.5')

    const without: Captured = {}
    stubBackend(without)
    await buildWalkableMap('http://backend', worldDir())
    assert.equal(without.form?.get('spawn_x'), null)
  })

  it('writes under the version index it was given', async () => {
    const dir = worldDir({ withCollider: false })
    fs.writeFileSync(path.join(dir, '2-world.glb'), Buffer.from('glTF stand-in'))
    stubBackend({})

    await buildWalkableMap('http://backend', dir, { index: 2 })

    assert.ok(fs.existsSync(path.join(dir, '2-world-walkable.png')))
    assert.ok(fs.existsSync(path.join(dir, '2-world-walkable.json')))
  })

  it('loses the minimap rather than the world when the backend errors', async () => {
    const dir = worldDir()
    stubBackend({}, () => new Response('voxel grid too large', { status: 500 }))

    assert.equal(await buildWalkableMap('http://backend', dir), undefined)
    assert.equal(fs.existsSync(path.join(dir, '0-world-walkable.json')), false)
  })

  it('degrades when the backend answers without a mask', async () => {
    stubBackend({}, () => Response.json(META))
    assert.equal(await buildWalkableMap('http://backend', worldDir()), undefined)
  })

  it('applies a timeout even when the caller supplies its own signal', async () => {
    // The regression: joining used to be a replacement, leaving the one path
    // that passes a signal with no timeout at all.
    const captured: Captured = {}
    // A real fetch rejects when its signal aborts; a backend that never answers
    // is only distinguishable from a hang by that rejection arriving.
    stubBackend(captured, () => new Promise((_resolve, reject) => {
      captured.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    const caller = new AbortController()

    const pending = buildWalkableMap('http://backend', worldDir(), {
      signal: caller.signal,
      timeoutMs: 30,
    })

    assert.equal(await pending, undefined, 'a wedged backend must not hang the generation')
    assert.equal(captured.signal?.aborted, true)
    assert.equal(caller.signal.aborted, false, 'the timeout fired, not the caller')
  })

  it('lets a cancellation through instead of reporting a missing minimap', async () => {
    const caller = new AbortController()
    stubBackend({}, () => {
      caller.abort()
      throw new DOMException('Aborted', 'AbortError')
    })

    await assert.rejects(
      buildWalkableMap('http://backend', worldDir(), { signal: caller.signal }),
      /abort/i,
    )
  })
})
