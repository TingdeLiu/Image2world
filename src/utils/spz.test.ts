import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { describe, it } from 'node:test'

import { encodeSpz, writeSpzLevels } from './spz.ts'

/**
 * The compression itself is Spark's; what this project owns is which files
 * survive it. `full_res.ply` has to stay -- `/api/extract-prop` cuts props out
 * of it and it is the only lossless copy left -- while the decimated levels
 * exist purely to be downloaded and have no reader once their .spz is written.
 * Getting that backwards either bloats every world or quietly breaks prop
 * extraction on all of them.
 */

/** A binary gaussian-splat PLY in the layout SHARP emits. */
function buildSplatPly(vertexCount: number) {
  const properties = [
    'x', 'y', 'z',
    'f_dc_0', 'f_dc_1', 'f_dc_2',
    'opacity',
    'scale_0', 'scale_1', 'scale_2',
    'rot_0', 'rot_1', 'rot_2', 'rot_3',
  ]
  const header = [
    'ply',
    'format binary_little_endian 1.0',
    `element vertex ${vertexCount}`,
    ...properties.map((name) => `property float ${name}`),
    'end_header',
  ].join('\n') + '\n'

  const body = Buffer.alloc(vertexCount * properties.length * 4)
  for (let vertex = 0; vertex < vertexCount; vertex += 1) {
    const values = [
      vertex * 0.25, vertex * -0.5, vertex * 0.125,
      0.4, 0.1, -0.2,
      2.0,
      -3.0, -3.1, -3.2,
      1, 0, 0, 0,
    ]
    values.forEach((value, slot) => {
      body.writeFloatLE(value, (vertex * properties.length + slot) * 4)
    })
  }

  return Buffer.concat([Buffer.from(header, 'ascii'), body])
}

function tempWorldDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'imageworld-spz-'))
}

describe('encodeSpz', () => {
  it('compresses a splat PLY', async () => {
    const ply = buildSplatPly(4096)
    const spz = await encodeSpz(ply)

    assert.ok(spz, 'expected an encoded buffer')
    assert.ok(spz.length < ply.length, `expected ${spz.length} < ${ply.length}`)
  })

  it('returns undefined rather than throwing on input it cannot read', async () => {
    assert.equal(await encodeSpz(Buffer.from('not a ply at all')), undefined)
  })
})

describe('writeSpzLevels', () => {
  it('keeps the full-resolution PLY and drops the detail levels it replaces', async () => {
    const dir = tempWorldDir()
    try {
      const fullRes = buildSplatPly(4096)
      const lod = buildSplatPly(512)
      fs.writeFileSync(path.join(dir, '0-world-full_res.ply'), fullRes)
      fs.writeFileSync(path.join(dir, '0-world-500k.ply'), lod)

      const written = await writeSpzLevels(dir, 0, [
        { level: 'full_res', ply: fullRes },
        { level: '500k', ply: lod },
      ])

      assert.equal(written, 2)
      assert.ok(fs.existsSync(path.join(dir, '0-world-full_res.spz')))
      assert.ok(fs.existsSync(path.join(dir, '0-world-500k.spz')))
      assert.ok(
        fs.existsSync(path.join(dir, '0-world-full_res.ply')),
        'the lossless original is what props are cut from; it must survive',
      )
      assert.equal(
        fs.existsSync(path.join(dir, '0-world-500k.ply')),
        false,
        'a detail level with an .spz has no remaining reader',
      )
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('leaves a level alone when it cannot be compressed', async () => {
    const dir = tempWorldDir()
    try {
      const damaged = Buffer.from('ply\nformat binary_little_endian 1.0\nend_header\n')
      fs.writeFileSync(path.join(dir, '0-world-100k.ply'), damaged)

      const written = await writeSpzLevels(dir, 0, [{ level: '100k', ply: damaged }])

      assert.equal(written, 0)
      assert.equal(fs.existsSync(path.join(dir, '0-world-100k.spz')), false)
      assert.ok(
        fs.existsSync(path.join(dir, '0-world-100k.ply')),
        'a level that failed to compress still has to be servable',
      )
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('writes each level under the version index it was given', async () => {
    const dir = tempWorldDir()
    try {
      const ply = buildSplatPly(1024)
      const written = await writeSpzLevels(dir, 3, [{ level: 'full_res', ply }])

      assert.equal(written, 1)
      assert.ok(fs.existsSync(path.join(dir, '3-world-full_res.spz')))
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })
})
