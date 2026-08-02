'use client'

import { RigidBody, CuboidCollider } from '@react-three/rapier'
import { useDebugStore } from '../../store/debug'
import { WorldRenderMode } from '../../types/world'

interface SceneBoundaryProps {
  bounds?: {
    min: [number, number, number]
    max: [number, number, number]
  }
}

// Stand slightly outside the reconstruction so the player can walk right up to
// the edge and look at it, rather than being stopped short of the geometry.
const MARGIN = 0.25
const THICKNESS = 0.5
// Tall enough that jumping cannot clear it. Fly mode moves the camera directly
// rather than through physics, so editing still sees the whole scene.
const EXTRA_HEIGHT = 6

/**
 * Fence off the reconstructed area.
 *
 * Single-view capture only produces geometry for surfaces the camera saw, so a
 * room's walls exist only on the side facing it. Without a fence you walk
 * straight through the unreconstructed side and end up in empty space -- still
 * standing, because the ground plane is 200 m across, but with the world
 * floating behind you.
 */
export function SceneBoundary({ bounds }: SceneBoundaryProps) {
  const worldRenderMode = useDebugStore((s) => s.worldRenderMode)

  // Object-only mode deliberately hides the world, so a fence around it would
  // trap the player in an empty box.
  if (!bounds || worldRenderMode === WorldRenderMode.ObjectOnly) return null

  const [minX, minY, minZ] = bounds.min
  const [maxX, maxY, maxZ] = bounds.max

  const x0 = minX - MARGIN
  const x1 = maxX + MARGIN
  const z0 = minZ - MARGIN
  const z1 = maxZ + MARGIN
  const halfWidth = (x1 - x0) / 2 + THICKNESS
  const halfDepth = (z1 - z0) / 2 + THICKNESS
  const centerX = (x0 + x1) / 2
  const centerZ = (z0 + z1) / 2

  // Span from below the floor to well above the ceiling.
  const height = Math.max(maxY - minY, 2) + EXTRA_HEIGHT
  const halfHeight = height / 2
  const centerY = minY + halfHeight - 1

  return (
    <RigidBody type="fixed" colliders={false}>
      <CuboidCollider args={[THICKNESS, halfHeight, halfDepth]} position={[x0 - THICKNESS, centerY, centerZ]} />
      <CuboidCollider args={[THICKNESS, halfHeight, halfDepth]} position={[x1 + THICKNESS, centerY, centerZ]} />
      <CuboidCollider args={[halfWidth, halfHeight, THICKNESS]} position={[centerX, centerY, z0 - THICKNESS]} />
      <CuboidCollider args={[halfWidth, halfHeight, THICKNESS]} position={[centerX, centerY, z1 + THICKNESS]} />
    </RigidBody>
  )
}
