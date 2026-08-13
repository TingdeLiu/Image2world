import type { CoefficientCombineRule } from '@react-three/rapier'
import * as THREE from 'three'

// Rapier's CoefficientCombineRule members, inlined. Importing the enum as a
// value would pull the rapier wasm bundle in, and this module has to stay
// importable outside a browser so its GLB contract can be tested.
const COMBINE_MIN = 1 as CoefficientCombineRule
const COMBINE_MAX = 3 as CoefficientCombineRule

/**
 * The face classes `backend/tools/build_collider.py` writes as GLB node names.
 * Keep the two lists in sync — a name the viewer does not recognise falls back
 * to whole-mesh collision with Rapier's defaults.
 */
export const SURFACE_CLASSES = ['floor', 'elevated', 'ramp', 'wall', 'ceiling'] as const

export type SurfaceClass = (typeof SURFACE_CLASSES)[number]

export interface SurfacePhysics {
  friction: number
  frictionCombineRule: CoefficientCombineRule
  restitution: number
}

/**
 * How each surface should feel underfoot.
 *
 * The combine rule matters as much as the coefficient. Rapier averages the two
 * colliders' friction by default, so a 0.02 wall against the character capsule
 * (Rapier's default 0.5) still resolves to 0.26 — enough to snag a player who
 * brushes it. `Min` is what actually lets them slide along.
 *
 * Nothing here bounces: a reconstructed room is plaster and furniture, and any
 * restitution above zero reads as rubber.
 */
export const SURFACE_PHYSICS: Record<SurfaceClass, SurfacePhysics> = {
  // Grip, so walking starts and stops when the player expects it to.
  floor: { friction: 1, frictionCombineRule: COMBINE_MAX, restitution: 0 },
  elevated: { friction: 0.9, frictionCombineRule: COMBINE_MAX, restitution: 0 },
  // Past the 45 degree standable limit, so the player should slide off rather
  // than perch on what is usually reconstruction noise.
  ramp: { friction: 0.15, frictionCombineRule: COMBINE_MIN, restitution: 0 },
  wall: { friction: 0.02, frictionCombineRule: COMBINE_MIN, restitution: 0 },
  ceiling: { friction: 0.02, frictionCombineRule: COMBINE_MIN, restitution: 0 },
}

const SURFACE_CLASS_SET = new Set<string>(SURFACE_CLASSES)

export function isSurfaceClass(name: string): name is SurfaceClass {
  return SURFACE_CLASS_SET.has(name)
}

export interface SurfaceMesh {
  surface: SurfaceClass
  mesh: THREE.Mesh
}

/**
 * Pull one mesh per surface class out of a loaded collider GLB.
 *
 * The geometry is shared rather than copied — only the `Mesh` wrapper is new —
 * so splitting one collider into five costs nothing but object headers. Each
 * mesh is baked with its own world matrix so it stands alone under a rigid
 * body, whatever the GLB's node hierarchy was.
 *
 * Returns an empty array for worlds generated before the split, whose collider
 * is a single unnamed `geometry_0`.
 */
export function surfaceMeshes(root: THREE.Object3D): SurfaceMesh[] {
  const found: SurfaceMesh[] = []
  root.updateWorldMatrix(true, true)

  root.traverse((child) => {
    if (!(child instanceof THREE.Mesh) || !isSurfaceClass(child.name)) return
    const mesh = new THREE.Mesh(child.geometry)
    mesh.name = child.name
    mesh.applyMatrix4(child.matrixWorld)
    found.push({ surface: child.name, mesh })
  })

  return found
}
