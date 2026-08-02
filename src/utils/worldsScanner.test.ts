import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { parseIndexedName, sanitizePlacementProject } from './worldsScanner.ts'

/**
 * `sanitizePlacementProject` guards every path that writes scene.json -- the
 * editor's save endpoint and the scanner reading worlds off disk. A malformed
 * placement that slips through does not fail loudly; it puts an object at NaN
 * and the viewer renders an empty scene, so the interesting cases here are the
 * near-misses rather than obvious garbage.
 */

const validInstance = {
  instanceId: 'instance_chair',
  objectId: 'chair',
  assetId: 'world/chair/0',
  physics: 'rigidbody',
  position: [1, 2, 3],
  rotation: [0, 0, 0],
  scale: [1, 1, 1],
}

const minimalProject = { version: 1, instances: [validInstance] }

describe('sanitizePlacementProject', () => {
  it('accepts a well-formed project', () => {
    const result = sanitizePlacementProject(minimalProject)
    assert.equal(result?.instances.length, 1)
    assert.deepEqual(result?.instances[0].position, [1, 2, 3])
  })

  it('rejects anything that is not a versioned object', () => {
    for (const input of [undefined, null, 'scene', 42, [], {}]) {
      assert.equal(sanitizePlacementProject(input), undefined, `accepted ${JSON.stringify(input)}`)
    }
  })

  it('rejects a future or legacy schema version rather than guessing', () => {
    assert.equal(sanitizePlacementProject({ ...minimalProject, version: 2 }), undefined)
    assert.equal(sanitizePlacementProject({ ...minimalProject, version: '1' }), undefined)
  })

  it('drops individual malformed instances but keeps the valid ones', () => {
    const result = sanitizePlacementProject({
      version: 1,
      instances: [
        validInstance,
        { ...validInstance, instanceId: 123 },
        { ...validInstance, position: [1, 2] },
        { ...validInstance, position: [1, 2, 'three'] },
        { ...validInstance, physics: 'bouncy' },
        null,
      ],
    })
    assert.equal(result?.instances.length, 1)
    assert.equal(result?.instances[0].instanceId, 'instance_chair')
  })

  it('rejects non-finite coordinates, which would place an object nowhere', () => {
    for (const bad of [[NaN, 0, 0], [0, Infinity, 0], [0, 0, -Infinity]]) {
      const result = sanitizePlacementProject({
        version: 1,
        instances: [{ ...validInstance, position: bad }],
      })
      assert.equal(result?.instances.length, 0, `accepted ${JSON.stringify(bad)}`)
    }
  })

  it('keeps the spawn point only when it is a usable pair of numbers', () => {
    const withSpawn = (spawnPoint: unknown) =>
      sanitizePlacementProject({ ...minimalProject, spawnPoint })

    assert.deepEqual(withSpawn([-0.6, -4.1])?.spawnPoint, [-0.6, -4.1])
    for (const bad of [[1], [1, 2, 3], [1, NaN], ['1', '2'], 'centre', null]) {
      assert.equal(withSpawn(bad)?.spawnPoint, undefined, `accepted ${JSON.stringify(bad)}`)
    }
  })

  it('clamps shadow catcher opacity into range instead of discarding it', () => {
    const opacity = (shadowCatcherOpacity: unknown) =>
      sanitizePlacementProject({ ...minimalProject, shadowCatcherOpacity })?.shadowCatcherOpacity

    assert.equal(opacity(0.4), 0.4)
    assert.equal(opacity(5), 1)
    assert.equal(opacity(-2), 0)
    assert.equal(opacity('0.5'), undefined)
  })

  it('accepts only six-digit hex colours, normalised to lower case', () => {
    const colour = (shadowCatcherColor: unknown) =>
      sanitizePlacementProject({ ...minimalProject, shadowCatcherColor })?.shadowCatcherColor

    assert.equal(colour('#AABBCC'), '#aabbcc')
    for (const bad of ['#abc', 'aabbcc', '#gggggg', '#aabbccdd', 0x123456]) {
      assert.equal(colour(bad), undefined, `accepted ${JSON.stringify(bad)}`)
    }
  })

  it('defaults physics to rigidbody when omitted', () => {
    const { physics, ...withoutPhysics } = validInstance
    void physics
    const result = sanitizePlacementProject({ version: 1, instances: [withoutPhysics] })
    assert.equal(result?.instances[0].physics, 'rigidbody')
  })
})

/**
 * Asset discovery keys off filenames like `0-world-full_res.ply`, so the parser
 * decides which splat, collider and thumbnail a world actually loads.
 */
describe('parseIndexedName', () => {
  it('splits index, slug and extension', () => {
    assert.deepEqual(parseIndexedName('0-world-full_res.ply'), {
      index: 0,
      slug: 'world-full_res',
      extension: '.ply',
      name: '0-world-full_res.ply',
    })
  })

  it('lower-cases the extension so .PLY and .ply match the same set', () => {
    assert.equal(parseIndexedName('3-world.PLY')?.extension, '.ply')
  })

  it('keeps only the final extension for multi-dot names', () => {
    const parsed = parseIndexedName('0-world.tar.gz')
    assert.equal(parsed?.extension, '.gz')
    assert.equal(parsed?.slug, 'world.tar')
  })

  it('returns undefined for names without a leading index', () => {
    for (const name of ['world.ply', 'scene.json', '-world.ply']) {
      assert.equal(parseIndexedName(name), undefined, `parsed ${name}`)
    }
  })

  it('recognises hidden request sidecars, including a scope', () => {
    assert.deepEqual(parseIndexedName('.2-chair__model-request.json'), {
      index: 2,
      slug: 'chair',
      scope: 'model',
      extension: '.json',
      hidden: true,
      name: '.2-chair__model-request.json',
    })
  })
})
