import type * as THREE from 'three'

export const DROP_TARGET_LAYER = 2

/**
 * Two different things share the drop-target layer.
 *
 * The world's collision mesh is on it so objects can be dropped onto real
 * surfaces. So is `GroundPlane`'s helper — an invisible 200 m square at y=0
 * that catches drops which miss the room entirely. The placement editor wants
 * both. Anything asking "what did the user click *in the world*" wants only the
 * first, and the helper is large enough that every downward click finds it:
 * without this distinction, a click near the floor is a coin toss between the
 * two, and a click at the sky is silently a hit on a plane 200 m wide.
 */
const COLLIDER_DROP_TARGET = 'colliderDropTarget'

export function markColliderDropTarget(object: THREE.Object3D): void {
  object.userData[COLLIDER_DROP_TARGET] = true
}

export function isColliderDropTarget(object: THREE.Object3D): boolean {
  return object.userData[COLLIDER_DROP_TARGET] === true
}
