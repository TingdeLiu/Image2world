import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import * as THREE from 'three'

import { applyWorldTransform, worldTransform } from './worldTransform.ts'

/**
 * The splat and the collision mesh are placed by different components from the
 * same numbers. When those numbers drift the app does not fail -- you walk into
 * walls that are not drawn where they are, and clicking to free an object cuts
 * geometry from the wrong place and reports success. So these tests pin the
 * conventions themselves, not just the arithmetic:
 *
 *   SHARP is +Y-down, so its worlds are turned over by pi about X.
 *   Nerfstudio is Z-up, so it passes -pi/2 explicitly and `flipY` must not win.
 *   `groundPlaneOffset` is in metres and is applied after the metric scale.
 */

const HALF_PI = Math.PI / 2

describe('worldTransform', () => {
  it('leaves a world alone by default', () => {
    assert.deepEqual(worldTransform({}), {
      rotation: [0, 0, 0],
      position: [0, 0, 0],
      scale: [1, 1, 1],
    })
  })

  it('turns a SHARP world over about X', () => {
    assert.deepEqual(worldTransform({ flipY: true }).rotation, [Math.PI, 0, 0])
  })

  it('lets an explicit rotationX override flipY', () => {
    // Nerfstudio worlds are Z-up and pass -pi/2. A caller that also sets flipY
    // must not end up rotated by pi: the explicit angle is the whole answer.
    const transform = worldTransform({ flipY: true, rotationX: -HALF_PI })
    assert.deepEqual(transform.rotation, [-HALF_PI, 0, 0])
  })

  it('honours rotationX of zero rather than falling back to flipY', () => {
    assert.deepEqual(worldTransform({ flipY: true, rotationX: 0 }).rotation, [0, 0, 0])
  })

  it('only ever rotates about X', () => {
    const { rotation } = worldTransform({ rotationX: 1.234 })
    assert.equal(rotation[1], 0)
    assert.equal(rotation[2], 0)
  })

  it('lifts the world by the ground offset, in metres', () => {
    assert.deepEqual(worldTransform({ groundPlaneOffset: 0.42 }).position, [0, 0.42, 0])
  })

  it('scales uniformly by the metric scale factor', () => {
    assert.deepEqual(worldTransform({ metricScaleFactor: 2.5 }).scale, [2.5, 2.5, 2.5])
  })

  it('does not scale the ground offset', () => {
    // The offset is already measured in metres by the collider build, so
    // multiplying it by the scale here would double-apply the calibration.
    const transform = worldTransform({ groundPlaneOffset: 0.42, metricScaleFactor: 3 })
    assert.deepEqual(transform.position, [0, 0.42, 0])
  })
})

describe('applyWorldTransform', () => {
  it('places the splat and the collider identically', () => {
    const input = { flipY: true, groundPlaneOffset: 0.3, metricScaleFactor: 1.8 }
    const splat = new THREE.Object3D()
    const collider = new THREE.Object3D()

    applyWorldTransform(splat, worldTransform(input))
    applyWorldTransform(collider, worldTransform(input))
    splat.updateMatrixWorld(true)
    collider.updateMatrixWorld(true)

    assert.deepEqual(splat.matrixWorld.elements, collider.matrixWorld.elements)
  })

  it('sends a point through the transform the way the renderer will', () => {
    const object = new THREE.Object3D()
    applyWorldTransform(object, worldTransform({ flipY: true, groundPlaneOffset: 2, metricScaleFactor: 3 }))
    object.updateMatrixWorld(true)

    // Scale, then the pi flip about X (y and z negate), then lift by 2.
    const point = new THREE.Vector3(1, 1, 1).applyMatrix4(object.matrixWorld)
    assert.ok(Math.abs(point.x - 3) < 1e-9, `x was ${point.x}`)
    assert.ok(Math.abs(point.y - -1) < 1e-9, `y was ${point.y}`)
    assert.ok(Math.abs(point.z - -3) < 1e-9, `z was ${point.z}`)
  })
})
