import type * as THREE from 'three'

/**
 * Where a world's geometry sits, in one place.
 *
 * The gaussians and the collision mesh are two files that have to land on top
 * of each other exactly. They are placed by different components, and until
 * this module existed each computed the placement itself — the same three lines
 * written twice, with nothing tying them together.
 *
 * Drift between them does not crash. Walking still works; the collider is just
 * somewhere the splat is not, and clicking to free an object cuts geometry from
 * the wrong place and reports success. That failure reads as "the segmenter
 * picked the wrong thing", which is why it could go unnoticed for a long time.
 *
 * So both callers spread the *same* object into their props rather than each
 * building their own. Two components can no longer disagree about the numbers,
 * because there is only one set of them.
 */
export interface WorldTransformInput {
  /** SHARP's +Y points down, so its worlds are turned over. */
  flipY?: boolean
  /** Explicit rotation, overriding `flipY`. Nerfstudio worlds are Z-up. */
  rotationX?: number
  /** Metres to lift the world so its floor lands on y=0. Applied after scale. */
  groundPlaneOffset?: number
  /** Reconstruction units per metre. */
  metricScaleFactor?: number
}

/** Ready to spread into an `<RigidBody>`, a `<group>`, or a `<primitive>`. */
export interface WorldTransform {
  rotation: [number, number, number]
  position: [number, number, number]
  scale: [number, number, number]
}

export function worldTransform({
  flipY,
  rotationX,
  groundPlaneOffset,
  metricScaleFactor,
}: WorldTransformInput): WorldTransform {
  const scale = metricScaleFactor ?? 1
  return {
    rotation: [rotationX ?? (flipY ? Math.PI : 0), 0, 0],
    position: [0, groundPlaneOffset ?? 0, 0],
    scale: [scale, scale, scale],
  }
}

/**
 * Put the transform on a plain `Object3D`, the way React Three Fiber does when
 * the same object is spread into a component's props.
 *
 * This exists so the tests can reason about the placement without a renderer.
 */
export function applyWorldTransform(object: THREE.Object3D, transform: WorldTransform): void {
  object.position.set(...transform.position)
  object.rotation.set(...transform.rotation)
  object.scale.set(...transform.scale)
}
