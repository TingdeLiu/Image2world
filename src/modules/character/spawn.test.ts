import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  CAMERA_EYE_OFFSET,
  CHARACTER_BODY_SPAWN,
  CHARACTER_BODY_SPAWN_POSITION,
  CHARACTER_HEIGHT,
  CHARACTER_SPAWN,
  CHARACTER_SPAWN_CLEARANCE,
  CHARACTER_SPAWN_POSITION,
  characterBodySpawn,
  characterBodySpawnPosition,
} from './spawn.ts'

/**
 * The camera sits at the top of the capsule and the rigid body is placed by its
 * centre, so the two spawn heights differ by half the character's height. Get
 * that wrong in either direction and the player either starts with their head
 * in the ceiling or spawns waist-deep in the floor and is pushed out.
 *
 * The x/z of a spawn point comes from the walkable map, which works in the
 * viewer's floor plane: [x, z], not [x, y]. It is the same pair of numbers
 * either way, which is exactly why swapping them goes unnoticed.
 */

describe('spawn heights', () => {
  it('puts the eye half a body above the body centre', () => {
    assert.equal(CAMERA_EYE_OFFSET, CHARACTER_HEIGHT / 2)
    assert.equal(CHARACTER_SPAWN.y - CHARACTER_BODY_SPAWN.y, CAMERA_EYE_OFFSET)
  })

  it('starts the capsule clear of the floor', () => {
    // The body's lowest point is its centre minus half the height. Landing it
    // exactly on y=0 lets the solver resolve the contact by shoving the player
    // through the floor, so it starts a little above.
    const lowestPoint = CHARACTER_BODY_SPAWN.y - CHARACTER_HEIGHT / 2
    assert.ok(lowestPoint > 0, `capsule bottom was ${lowestPoint}`)
    assert.equal(lowestPoint, CHARACTER_SPAWN_CLEARANCE)
  })

  it('keeps the tuple forms in step with the objects', () => {
    assert.deepEqual(CHARACTER_SPAWN_POSITION, [CHARACTER_SPAWN.x, CHARACTER_SPAWN.y, CHARACTER_SPAWN.z])
    assert.deepEqual(
      CHARACTER_BODY_SPAWN_POSITION,
      [CHARACTER_BODY_SPAWN.x, CHARACTER_BODY_SPAWN.y, CHARACTER_BODY_SPAWN.z],
    )
  })
})

describe('characterBodySpawn', () => {
  it('falls back to the origin default when a world records no spawn point', () => {
    assert.deepEqual(characterBodySpawn(undefined), CHARACTER_BODY_SPAWN)
  })

  it('reads a spawn point as [x, z] on the floor plane, not [x, y]', () => {
    const spawn = characterBodySpawn([2.5, -4])
    assert.equal(spawn.x, 2.5)
    assert.equal(spawn.z, -4)
    assert.equal(spawn.y, CHARACTER_BODY_SPAWN.y, 'height never comes from the map')
  })

  it('keeps the recorded height when the spawn point sits at the origin', () => {
    // [0, 0] is a legitimate answer from the walkable map, not a missing value.
    const spawn = characterBodySpawn([0, 0])
    assert.deepEqual([spawn.x, spawn.z], [0, 0])
    assert.equal(spawn.y, CHARACTER_BODY_SPAWN.y)
  })

  it('returns the tuple form in x, y, z order', () => {
    assert.deepEqual(characterBodySpawnPosition([1, 2]), [1, CHARACTER_BODY_SPAWN.y, 2])
  })
})
