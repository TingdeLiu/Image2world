import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { WorldObjectAsset } from '../../types/world.ts'
import {
  OBJECT_RESET_ORIGIN,
  OBJECT_SCALE,
  createDefaultPlacements,
  getInitialPlacements,
  gridPosition,
} from './placements.ts'

/**
 * The grid is where objects land before anyone has moved them, so it has to be
 * in front of the spawn point and never on top of itself. `OBJECT_SCALE` is
 * shared with the generation pipeline, which divides measured metric sizes by
 * it -- changing one side alone silently resizes every prop ever made.
 */

function asset(id: string): WorldObjectAsset {
  return {
    id,
    assetId: `${id}-asset`,
    sourceWorldSlug: 'demo-office',
    baseObjectId: id,
    name: id,
    url: `/worlds/demo-office/output/objects/${id}.glb`,
    sfxUrls: [],
    complete: true,
  }
}

describe('gridPosition', () => {
  it('centres a single object on the reset origin', () => {
    assert.deepEqual(gridPosition(0, 1), OBJECT_RESET_ORIGIN)
  })

  it('centres a full row about the origin', () => {
    const [left, middle, right] = [0, 1, 2].map((index) => gridPosition(index, 3))
    assert.equal(middle[0], OBJECT_RESET_ORIGIN[0])
    assert.equal(left[0], OBJECT_RESET_ORIGIN[0] - 1)
    assert.equal(right[0], OBJECT_RESET_ORIGIN[0] + 1)
    assert.equal(left[2], right[2], 'one row shares one depth')
  })

  it('wraps to a new row every three objects, further from the camera', () => {
    const first = gridPosition(0, 6)
    const fourth = gridPosition(3, 6)
    assert.equal(fourth[0], first[0], 'the fourth object starts a new row')
    assert.ok(fourth[2] < first[2], 'later rows sit further along -z')
  })

  it('never stacks two objects in the same place', () => {
    const total = 7
    const seen = new Set<string>()
    for (let index = 0; index < total; index += 1) {
      seen.add(gridPosition(index, total).join(','))
    }
    assert.equal(seen.size, total)
  })

  it('keeps every object at the reset height', () => {
    for (let index = 0; index < 5; index += 1) {
      assert.equal(gridPosition(index, 5)[1], OBJECT_RESET_ORIGIN[1])
    }
  })

  it('survives a total of zero rather than dividing by it', () => {
    const position = gridPosition(0, 0)
    assert.ok(position.every(Number.isFinite), `got ${position.join(',')}`)
  })
})

describe('createDefaultPlacements', () => {
  it('gives every object an identity transform and rigidbody physics', () => {
    const placements = createDefaultPlacements([asset('chair'), asset('lamp')])

    assert.equal(placements.length, 2)
    for (const placement of placements) {
      assert.equal(placement.physics, 'rigidbody')
      assert.deepEqual(placement.rotation, [0, 0, 0])
      assert.deepEqual(placement.scale, [1, 1, 1])
    }
    assert.deepEqual(placements.map((p) => p.objectId), ['chair', 'lamp'])
    assert.deepEqual(placements.map((p) => p.assetId), ['chair-asset', 'lamp-asset'])
  })
})

describe('getInitialPlacements', () => {
  it('prefers what the user saved', () => {
    const saved = createDefaultPlacements([asset('chair')])
    saved[0].position = [9, 9, 9]

    assert.deepEqual(getInitialPlacements([asset('chair')], saved), saved)
  })

  it('falls back to the grid when nothing was saved', () => {
    const placements = getInitialPlacements([asset('chair')], undefined)
    assert.deepEqual(placements[0].position, gridPosition(0, 1))
  })

  it('treats an empty saved list as a saved list, not as missing', () => {
    // A world where every prop was deleted must stay empty on reload.
    assert.deepEqual(getInitialPlacements([asset('chair')], []), [])
  })
})

describe('OBJECT_SCALE', () => {
  it('stays at the value the generation pipeline divides by', () => {
    assert.equal(OBJECT_SCALE, 0.5)
  })
})
