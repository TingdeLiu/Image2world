import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { describe, it } from 'node:test'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import {
  SURFACE_CLASSES,
  SURFACE_PHYSICS,
  isSurfaceClass,
  surfaceMeshes,
  type SurfaceClass,
} from './surfaces.ts'

/**
 * Stand in for a collider GLB as three's GLTFLoader hands it over: a root, a
 * `world` group holding one named mesh per class, matching what
 * `labeled_collider_scene` in build_collider.py writes.
 */
function loadedCollider(names: string[], { groupOffsetY = 0 } = {}) {
  const root = new THREE.Object3D()
  const world = new THREE.Object3D()
  world.name = 'world'
  world.position.set(0, groupOffsetY, 0)
  root.add(world)
  for (const name of names) {
    const mesh = new THREE.Mesh(new THREE.BufferGeometry())
    mesh.name = name
    world.add(mesh)
  }
  return root
}

describe('isSurfaceClass', () => {
  it('accepts every class the backend can emit', () => {
    for (const name of SURFACE_CLASSES) {
      assert.equal(isSurfaceClass(name), true)
    }
  })

  it('rejects the unnamed node older worlds carry', () => {
    assert.equal(isSurfaceClass('geometry_0'), false)
    assert.equal(isSurfaceClass('world'), false)
  })
})

describe('SURFACE_PHYSICS', () => {
  it('covers every class, so no surface falls back to engine defaults', () => {
    for (const name of SURFACE_CLASSES) {
      assert.ok(SURFACE_PHYSICS[name], `missing physics for ${name}`)
    }
  })

  it('grips underfoot and slides on walls', () => {
    assert.ok(
      SURFACE_PHYSICS.floor.friction > SURFACE_PHYSICS.wall.friction,
      'walking on the floor must not be slipperier than brushing a wall',
    )
    // Rapier averages both colliders' friction by default, which would leave a
    // 0.02 wall resolving to ~0.26 against the character capsule and still
    // snagging. Min is what makes the low coefficient mean anything.
    assert.equal(SURFACE_PHYSICS.wall.frictionCombineRule, 1, 'wall should combine with Min')
    assert.equal(SURFACE_PHYSICS.ramp.frictionCombineRule, 1, 'ramp should combine with Min')
  })

  it('never bounces — a reconstructed room is plaster, not rubber', () => {
    for (const name of SURFACE_CLASSES) {
      assert.equal(SURFACE_PHYSICS[name].restitution, 0)
    }
  })
})

describe('surfaceMeshes', () => {
  it('extracts one mesh per class from a classified collider', () => {
    const found = surfaceMeshes(loadedCollider([...SURFACE_CLASSES]))
    assert.deepEqual(
      found.map((entry) => entry.surface),
      [...SURFACE_CLASSES],
    )
  })

  it('returns nothing for a pre-split world, so the caller can fall back', () => {
    assert.deepEqual(surfaceMeshes(loadedCollider(['geometry_0'])), [])
  })

  it('shares geometry rather than copying it', () => {
    const root = loadedCollider(['floor'])
    const source = root.getObjectByName('floor') as THREE.Mesh
    const [{ mesh }] = surfaceMeshes(root)
    assert.notEqual(mesh, source, 'the wrapper should be new')
    assert.equal(mesh.geometry, source.geometry, 'the geometry should not be cloned')
  })

  it('bakes the node hierarchy into each mesh, since they get separate bodies', () => {
    // Splitting one collider across five rigid bodies drops the shared parent,
    // so any transform it carried has to come along or the pieces drift apart.
    const [{ mesh }] = surfaceMeshes(loadedCollider(['wall'], { groupOffsetY: 2.5 }))
    assert.equal(mesh.position.y, 2.5)
  })

  it('ignores classes the backend left out of this world', () => {
    const partial: SurfaceClass[] = ['floor', 'wall']
    const found = surfaceMeshes(loadedCollider(partial))
    assert.deepEqual(found.map((entry) => entry.surface), partial)
  })
})

/**
 * The tests above build the scene graph by hand. This one reads a collider the
 * Python side actually wrote, through the same loader the viewer uses, so the
 * two halves of the contract are checked against each other rather than
 * against an assumption.
 *
 * Worlds are hundreds of megabytes and stay out of git, so this skips itself
 * wherever they are absent — CI included.
 */
describe('a collider GLB from the backend', () => {
  const worldsDir = path.join(process.cwd(), 'public', 'worlds')

  function findClassifiedCollider(): string | undefined {
    if (!fs.existsSync(worldsDir)) return undefined
    for (const slug of fs.readdirSync(worldsDir)) {
      const glb = path.join(worldsDir, slug, 'output', 'world', '0-world.glb')
      if (!fs.existsSync(glb)) continue
      // Node names live in the GLB's JSON chunk, which is plain text near the
      // front of the file — enough to tell a classified collider from a legacy
      // one without parsing hundreds of megabytes of them.
      const head = fs.readFileSync(glb).subarray(0, 4096).toString('latin1')
      if (SURFACE_CLASSES.every((name) => head.includes(`"${name}"`))) return glb
    }
    return undefined
  }

  async function parse(file: string): Promise<THREE.Group> {
    const buffer = fs.readFileSync(file)
    const bytes = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength)
    return new Promise((resolve, reject) => {
      new GLTFLoader().parse(bytes as ArrayBuffer, '', (gltf) => resolve(gltf.scene), reject)
    })
  }

  it('yields one usable rigid body per class', async (t) => {
    const file = findClassifiedCollider()
    if (!file) return t.skip('no classified world collider on this machine')

    const found = surfaceMeshes(await parse(file))
    assert.deepEqual(
      found.map((entry) => entry.surface).sort(),
      [...SURFACE_CLASSES].sort(),
      'every class the backend wrote should come back',
    )
    for (const { surface, mesh } of found) {
      const position = mesh.geometry.getAttribute('position')
      assert.ok(position && position.count > 0, `${surface} has no geometry to collide with`)
      assert.ok(SURFACE_PHYSICS[surface], `${surface} has no physics`)
    }
  })
})
