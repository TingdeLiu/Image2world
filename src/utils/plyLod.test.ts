import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { createBinaryPlyLod } from './plyLod.ts'

/**
 * The LOD writer rewrites a binary PLY in place: it must keep the header's
 * vertex count in step with the bytes it actually emits, and preserve whatever
 * follows the vertex table. SHARP stores its camera intrinsics there, and
 * losing them silently breaks object placement rather than failing outright.
 */

const SCALAR_BYTES: Record<string, number> = {
  char: 1, int8: 1, uchar: 1, uint8: 1,
  short: 2, int16: 2, ushort: 2, uint16: 2,
  int: 4, int32: 4, uint: 4, uint32: 4, float: 4, float32: 4,
  double: 8, float64: 8,
}

function buildPly({
  vertexCount,
  trailing = Buffer.alloc(0),
  newline = '\n',
  properties = ['property float x', 'property float y', 'property float z'],
  format = 'format binary_little_endian 1.0',
}: {
  vertexCount: number
  trailing?: Buffer
  newline?: string
  properties?: string[]
  format?: string
}) {
  const header = [
    'ply',
    format,
    `element vertex ${vertexCount}`,
    ...properties,
    'end_header',
  ].join(newline) + newline
  // Size the body from the declared types, not the property count -- a uchar
  // and a double are not both four bytes, and getting this wrong produces a
  // truncated file that the parser rightly refuses.
  const stride = properties.reduce((total, line) => {
    const type = line.split(/\s+/)[1]
    return total + (SCALAR_BYTES[type] ?? 4)
  }, 0)
  const body = Buffer.alloc(vertexCount * stride)
  for (let i = 0; i < vertexCount; i += 1) {
    // Tag each vertex with its own index so we can see which survived.
    body.writeFloatLE(i, i * stride)
  }
  return { buffer: Buffer.concat([Buffer.from(header, 'ascii'), body, trailing]), stride }
}

function readHeaderCount(out: Buffer) {
  const header = out.subarray(0, out.indexOf(Buffer.from('end_header'))).toString('ascii')
  return Number(header.match(/element vertex (\d+)/)?.[1])
}

function readVertexIds(out: Buffer, stride: number) {
  const start = out.indexOf(Buffer.from('end_header\n')) + 'end_header\n'.length
  const count = readHeaderCount(out)
  return Array.from({ length: count }, (_, i) => out.readFloatLE(start + i * stride))
}

describe('createBinaryPlyLod', () => {
  it('emits exactly as many vertices as the rewritten header claims', () => {
    const { buffer, stride } = buildPly({ vertexCount: 100 })
    const out = createBinaryPlyLod(buffer, 10)!
    assert.equal(readHeaderCount(out), 10)
    assert.equal(readVertexIds(out, stride).length, 10)
  })

  it('samples across the whole cloud rather than taking a prefix', () => {
    const { buffer, stride } = buildPly({ vertexCount: 100 })
    const ids = readVertexIds(createBinaryPlyLod(buffer, 10)!, stride)
    assert.ok(ids[0] < 10, `first sample ${ids[0]} should come from the start`)
    assert.ok(ids.at(-1)! > 89, `last sample ${ids.at(-1)} should come from the end`)
    assert.deepEqual([...ids].sort((a, b) => a - b), ids, 'samples should stay in order')
  })

  it('is deterministic, so regenerating a world reproduces the same LOD', () => {
    const { buffer } = buildPly({ vertexCount: 500 })
    assert.deepEqual(createBinaryPlyLod(buffer, 37), createBinaryPlyLod(buffer, 37))
  })

  it('preserves trailing metadata after the vertex table', () => {
    // Stands in for SHARP's intrinsic/image_size elements.
    const trailing = Buffer.from('CAMERA-INTRINSICS-PAYLOAD', 'ascii')
    const { buffer } = buildPly({ vertexCount: 50, trailing })
    const out = createBinaryPlyLod(buffer, 5)!
    assert.ok(out.subarray(out.length - trailing.length).equals(trailing))
  })

  it('returns a copy when the target is not smaller', () => {
    const { buffer } = buildPly({ vertexCount: 20 })
    for (const target of [20, 999]) {
      const out = createBinaryPlyLod(buffer, target)!
      assert.ok(out.equals(buffer))
      assert.notEqual(out, buffer, 'should not hand back the input buffer itself')
    }
  })

  it('always keeps at least one vertex for a nonsensical target', () => {
    const { buffer } = buildPly({ vertexCount: 20 })
    for (const target of [0, -5, 0.4]) {
      assert.equal(readHeaderCount(createBinaryPlyLod(buffer, target)!), 1, `target ${target}`)
    }
  })

  it('handles CRLF headers', () => {
    const { buffer } = buildPly({ vertexCount: 40, newline: '\r\n' })
    assert.equal(readHeaderCount(createBinaryPlyLod(buffer, 4)!), 4)
  })

  it('accounts for mixed property widths when striding', () => {
    const properties = [
      'property float x', 'property float y', 'property float z',
      'property uchar red', 'property double weight',
    ]
    const { buffer, stride } = buildPly({ vertexCount: 30, properties })
    assert.equal(stride, 4 * 3 + 1 + 8, 'fixture stride should follow the declared types')
    const out = createBinaryPlyLod(buffer, 3)!
    const headerLength = out.indexOf(Buffer.from('end_header\n')) + 'end_header\n'.length
    assert.equal(out.length, headerLength + 3 * stride)
  })

  it('declines formats it cannot safely rewrite', () => {
    const ascii = buildPly({ vertexCount: 10, format: 'format ascii 1.0' }).buffer
    assert.equal(createBinaryPlyLod(ascii, 5), undefined)

    const bigEndian = buildPly({ vertexCount: 10, format: 'format binary_big_endian 1.0' }).buffer
    assert.equal(createBinaryPlyLod(bigEndian, 5), undefined)

    const listProperty = buildPly({
      vertexCount: 10,
      properties: ['property float x', 'property list uchar int vertex_indices'],
    }).buffer
    assert.equal(createBinaryPlyLod(listProperty, 5), undefined)

    const unknownType = buildPly({
      vertexCount: 10,
      properties: ['property float x', 'property float128 y'],
    }).buffer
    assert.equal(createBinaryPlyLod(unknownType, 5), undefined)

    assert.equal(createBinaryPlyLod(Buffer.from('not a ply at all'), 5), undefined)
  })

  it('declines a truncated file rather than reading past the end', () => {
    const { buffer } = buildPly({ vertexCount: 100 })
    assert.equal(createBinaryPlyLod(buffer.subarray(0, buffer.length - 40), 10), undefined)
  })
})
