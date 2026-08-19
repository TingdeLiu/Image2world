import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import * as THREE from 'three'

import { DROP_TARGET_LAYER, isColliderDropTarget, markColliderDropTarget } from './dropTargets.ts'

/**
 * `GroundPlane`'s invisible 200 m helper shares the drop-target layer with the
 * real collision mesh, so a raycast that only filters by layer cannot tell them
 * apart -- and the helper is wide enough that a click anywhere finds it. That is
 * how clicking to free an object ended up cutting geometry out of a plane
 * instead of the room. Only the collider carries the mark.
 */

describe('collider drop targets', () => {
  it('does not treat an unmarked object as one', () => {
    assert.equal(isColliderDropTarget(new THREE.Object3D()), false)
  })

  it('recognises an object once it is marked', () => {
    const collider = new THREE.Object3D()
    markColliderDropTarget(collider)
    assert.equal(isColliderDropTarget(collider), true)
  })

  it('does not spread the mark to children', () => {
    // The ground helper is a sibling of the collider under the same group; a
    // mark that propagated would put them back on equal footing.
    const parent = new THREE.Object3D()
    const child = new THREE.Object3D()
    parent.add(child)
    markColliderDropTarget(parent)

    assert.equal(isColliderDropTarget(child), false)
  })

  it('leaves the rest of userData alone', () => {
    const object = new THREE.Object3D()
    object.userData.surface = 'floor'
    markColliderDropTarget(object)

    assert.equal(object.userData.surface, 'floor')
  })

  it('only accepts the mark itself, not any truthy value', () => {
    const impostor = new THREE.Object3D()
    impostor.userData.colliderDropTarget = 'yes'
    assert.equal(isColliderDropTarget(impostor), false)
  })

  it('keeps the shared layer where both kinds of target live', () => {
    const object = new THREE.Object3D()
    object.layers.enable(DROP_TARGET_LAYER)
    assert.equal(object.layers.isEnabled(DROP_TARGET_LAYER), true)
    assert.equal(object.layers.isEnabled(0), true, 'the default layer stays on')
  })
})
