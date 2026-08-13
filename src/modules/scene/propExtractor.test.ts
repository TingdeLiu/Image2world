import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import * as THREE from 'three'
import { applyWorldTransform, worldTransform } from '../collider/worldTransform.ts'

/**
 * Clicking to free an object depends on one assumption: that undoing the
 * collider's transform gives a point in the same space the splat lives in.
 *
 * That used to be an assumption these tests could only restate, because they
 * described the placement themselves rather than importing it — two hand-copied
 * transforms tested against a third. `worldTransform` is now the only place the
 * placement is computed, and `WorldCollider` and `SplatRenderer` both spread its
 * result, so agreement between them is structural rather than tested.
 *
 * What is left to test is the transform itself: the numbers it produces, and
 * what those numbers mean once React Three Fiber puts them on an object.
 */

const TRANSFORMS: Array<[string, Parameters<typeof worldTransform>[0]]> = [
  ['SHARP defaults (flip_y)', { flipY: true, groundPlaneOffset: 1.299, metricScaleFactor: 1 }],
  ['no flip, no offset', { flipY: false, groundPlaneOffset: 0, metricScaleFactor: 1 }],
  ['scaled world', { flipY: true, groundPlaneOffset: -0.4, metricScaleFactor: 2.5 }],
  ['nerfstudio z-up', { rotationX: -Math.PI / 2, groundPlaneOffset: 0.75, metricScaleFactor: 0.8 }],
]

const SAMPLES = [
  new THREE.Vector3(0, 0, 0),
  new THREE.Vector3(1.5, -0.25, 3),
  new THREE.Vector3(-2.5372, 0.1943, 10.296), // a real prop centre
]

/** A node placed the way R3F places one given these props. */
function placed(input: Parameters<typeof worldTransform>[0]): THREE.Object3D {
  const node = new THREE.Object3D()
  applyWorldTransform(node, worldTransform(input))
  node.updateWorldMatrix(true, true)
  return node
}

describe('worldTransform', () => {
  it('turns flipY into a half turn about X, and lets rotationX override it', () => {
    assert.equal(worldTransform({ flipY: true }).rotation[0], Math.PI)
    assert.equal(worldTransform({ flipY: false }).rotation[0], 0)
    assert.equal(worldTransform({}).rotation[0], 0)
    // Nerfstudio worlds are Z-up and pass an explicit angle; it has to win even
    // when flipY is also set, or a video-reconstructed world lies on its side.
    assert.equal(worldTransform({ flipY: true, rotationX: -Math.PI / 2 }).rotation[0], -Math.PI / 2)
  })

  it('defaults to an identity placement', () => {
    assert.deepEqual(worldTransform({}), {
      rotation: [0, 0, 0],
      position: [0, 0, 0],
      scale: [1, 1, 1],
    })
  })

  it('keeps the ground offset out of the scale', () => {
    // This is a contract, not an implementation detail. `groundPlaneOffset` is
    // the group's own position, so it lands in metres *after* the children are
    // scaled -- which is why `finalize/route.ts` multiplies the collider's raw
    // offset by the metric scale before writing it. Move the offset inside the
    // scale here and that route starts producing floors below the world.
    const node = placed({ groundPlaneOffset: 2, metricScaleFactor: 3 })
    const origin = node.localToWorld(new THREE.Vector3(0, 0, 0))
    assert.equal(origin.y, 2, 'the offset should not be multiplied by the scale')

    const above = node.localToWorld(new THREE.Vector3(0, 1, 0))
    assert.equal(above.y, 5, 'a point one unit up should land three metres above the offset')
  })
})

describe('collider space and splat space', () => {
  for (const [name, input] of TRANSFORMS) {
    it(`place a point identically under ${name}`, () => {
      // Both components spread one `worldTransform` result, so this compares
      // two nodes built from the same call. It is a guard on the spreading
      // staying lossless, not on the two components agreeing.
      const splat = placed(input)
      const collider = placed(input)

      for (const point of SAMPLES) {
        const viaSplat = splat.localToWorld(point.clone())
        const viaCollider = collider.localToWorld(point.clone())
        assert.ok(
          viaSplat.distanceTo(viaCollider) < 1e-9,
          `${point.toArray()} lands at ${viaSplat.toArray()} as a gaussian but ${viaCollider.toArray()} as collision geometry`,
        )
      }
    })
  }
})

describe('a degenerate transform', () => {
  it('does not silently collapse the scene to the origin', () => {
    // metricScaleFactor is read from a manifest; zero would map every click to
    // the same gaussian, which is worse than failing.
    const node = placed({ groundPlaneOffset: 0, metricScaleFactor: 0 })
    const recovered = node.worldToLocal(new THREE.Vector3(1, 2, 3))
    assert.ok(
      !Number.isFinite(recovered.x) || recovered.length() > 1e6 || Number.isNaN(recovered.x),
      'a zero scale should blow up rather than quietly return a plausible point',
    )
  })
})
